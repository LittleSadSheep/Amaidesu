"""look_at_screen 工具 —— 屏幕快照异步工具

定位：
- 屏幕画面 = **快照型** → 工具内异步调用（gather 等齐结果）
- 任何 Agent 都可调用（公共工具，放 ``tools/perception/``）
- 后端（屏幕采集 / 文本识别）通过 Protocol 注入
- 后端缺失时**优雅降级**：返回成功 + 空文本 + 警告 block（不抛）

数据流：
    Agent → ToolRegistry.invoke("look_at_screen")
        → LookAtScreenProvider.invoke(invocation)        (async)
        → ScreenCapture.capture(region)                  (PIL Image or None)
        → TextReader.read(image, question=...)           (async; str 或空，可选)
        → ToolExecutionResult (text content + image block)

落地形态：
- 后端注入即可用：测试用 ``FakeScreenCapture`` + ``FakeTextReader`` 跑通感知-推进闭环
- 生产环境：注入基于 pyautogui / mss / dxcam 的真实采集后端（与屏幕采集器同源，
  但本工具只暴露**调用即看**的同步接口，不做变化检测轮询）

设计要点（判别口诀）：
- ✅ 只暴露"能力契约"（ToolSpec + ToolProvider）
- ✅ 后端可换可 mock（Protocol 注入）
- ❌ 不内置采集器逻辑（流型归 collectors/screen/，本工具只快照）
- ❌ 不依赖具体游戏（任何需要"看屏幕"的 Agent 都可用）
"""

from __future__ import annotations

import asyncio
import base64
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Protocol, Tuple

from pydantic import Field

from src.modules.config.schemas.base import BaseConfig
from src.modules.llm.bootstrap import ProfileNames
from src.modules.logging import get_logger
from src.modules.prompts.manager import PromptManager
from src.modules.tools.models import (
    ResultBlock,
    ToolExecutionResult,
    ToolInvocation,
    ToolSpec,
)
from src.modules.tools.provider import BaseToolProvider

logger = get_logger("look_at_screen")

# VLM 调用的默认超时（秒）。下游契约重写任务可能改为可配置，本模块先以常量
# 形式钉住，避免调用点散落魔数。失败/超时一律降级为空串，不抛。
DEFAULT_VLM_TIMEOUT_S: float = 15.0

# LlmVisionTextReader 的 VLM 调用模板键（由 Task 3 迁移到 vision/prompts/）。
SCREEN_VLM_PROMPT_KEY = "screen_vlm_prompt"
SCREEN_VLM_SYSTEM_KEY = "screen_vlm_system"


# ---------------------------------------------------------------------------
# 后端协议（依赖注入点；测试用 Fake 实现，生产用 PIL/mss/pyautogui）
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ScreenCaptureResult:
    """一次屏幕采集的返回值（图像 + 元数据）。

    Attributes:
        image: 图像数据（bytes 形态 PNG / JPEG；None = 后端不可用）
        width: 像素宽（image=None 时可为 0）
        height: 像素高
        mime_type: 图像 MIME（如 ``"image/png"``；image=None 时为空）
        region: 实际采集区域 ``[x1, y1, x2, y2]``；None 表示全屏
        captured_at_ms: 采集时刻（Unix 毫秒）
    """

    image: Optional[bytes] = None
    width: int = 0
    height: int = 0
    mime_type: str = ""
    region: Optional[List[int]] = None
    captured_at_ms: int = 0


class ScreenCapture(Protocol):
    """屏幕采集后端协议（依赖注入点）。

    生产实现可基于 pyautogui / mss / dxcam；测试用 ``FakeScreenCapture``。
    不存在该协议的方法视为"后端不可用" → 工具返回空快照（不抛）。
    """

    def capture(
        self,
        region: Optional[Tuple[int, int, int, int]] = None,
    ) -> ScreenCaptureResult:
        """截取屏幕快照。

        Args:
            region: 可选区域 ``(x1, y1, x2, y2)``；None = 全屏

        Returns:
            :class:`ScreenCaptureResult`；image=None 表示后端不可用
        """
        ...


class TextReader(Protocol):
    """图像→文本 协议（OCR / VLM 均可实现，异步）。

    可选注入：不注入时 ``look_at_screen`` 只返回图像块，不含文本。
    异步契约是 VLM/OCR 等 I/O 调用的硬要求，避免阻塞事件循环。
    """

    async def read(
        self,
        image_bytes: bytes,
        *,
        mime_type: str = "image/png",
        question: Optional[str] = None,
    ) -> str:
        """从图像提取文本（OCR / VLM 描述）。

        Args:
            image_bytes: 图像字节（PNG/JPEG 等）
            mime_type: 图像 MIME
            question: 调用方对本次识别的提问（如"屏幕上显示什么"）；
                None = 实现自行决定使用默认提示

        Returns:
            提取的文本（空串表示无可读文本或识别失败）
        """
        ...


# ---------------------------------------------------------------------------
# 工具规格
# ---------------------------------------------------------------------------

# 提供者标识统一来源（ToolSpec.provider / 追溯用），避免字面量重复
PROVIDER_NAME = "vision"

LOOK_AT_SCREEN_SPEC = ToolSpec(
    name="look_at_screen",
    description=(
        "截取屏幕快照（同步工具，调用即看）。返回当前屏幕的文本内容"
        "（来自注入的 OCR/VLM reader）+ 图像块（base64）。"
        "无屏幕采集后端时返回成功 + 空内容（不抛异常，Agent 可继续）。"
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "region": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "可选截图区域 [x1, y1, x2, y2]；缺省=全屏",
            },
            "max_width": {
                "type": "integer",
                "description": "图像缩放最大宽度（像素，0=不缩放；省 token 用）",
                "minimum": 0,
            },
        },
        "required": [],
    },
    kind="sync",
    provider=PROVIDER_NAME,
    output_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "OCR/VLM 提取的文本"},
            "image": {"type": "string", "description": "图像 base64（PNG）"},
            "width": {"type": "integer"},
            "height": {"type": "integer"},
            "backend_available": {"type": "boolean"},
        },
    },
)


def build_look_at_screen_spec() -> ToolSpec:
    """构造 look_at_screen ToolSpec（工厂方法，便于将来参数化）。"""
    return LOOK_AT_SCREEN_SPEC


# ---------------------------------------------------------------------------
# Provider（注册到 ToolRegistry）
# ---------------------------------------------------------------------------


class LookAtScreenProvider(BaseToolProvider):
    """``look_at_screen`` 工具的 ToolProvider（Provider 协议）。

    通过构造器注入屏幕采集 / 文本读取后端；测试可传 ``None`` 表示优雅降级。

    Example:
        >>> provider = LookAtScreenProvider(
        ...     config={"default_max_width": 1280},
        ...     screen_capture=PyautoguiCapture(),
        ... )
        >>> registry.register_provider(provider)
    """

    # 工具分类（provider=提供者名、category=分组、tools.toml 段=配置地址，三者正交）
    category = "vision"

    class ConfigSchema(BaseConfig):
        """look_at_screen 配置（默认最大图像宽度；省 token 用）

        TOML 段位：[tools.vision].config
        """

        type: str = "vision"
        default_max_width: int = Field(
            default=1280,
            ge=0,
            description="图像缩放最大宽度（像素，0=不缩放；省 token 用）",
        )

    def __init__(
        self,
        config: Dict[str, Any],
        *,
        screen_capture: Optional[ScreenCapture] = None,
        text_reader: Optional[TextReader] = None,
    ) -> None:
        # 配置转 typed（config: dict 必填；空 dict = 全部默认；失败 log+raise）
        self._config_raw = dict(config) if config is not None else {}
        self.typed_config = self.ConfigSchema.from_dict(self._config_raw)
        self._default_max_width = int(self.typed_config.default_max_width)

        self._capture = screen_capture
        self._reader = text_reader
        self._call_count = 0

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    def list_tools(self) -> Iterable[ToolSpec]:
        return [LOOK_AT_SCREEN_SPEC]

    @property
    def call_count(self) -> int:
        """测试用：累计调用次数。"""
        return self._call_count

    async def invoke(self, invocation: ToolInvocation) -> ToolExecutionResult:
        """执行 look_at_screen：截屏 + 可选 OCR + 返回 ResultBlocks。"""
        self._call_count += 1
        started_ms = int(time.time() * 1000)
        # 注册名（vision_look_at_screen）即调用方使用的名；结果回显它保持溯源一致
        tool_name = invocation.tool_name

        args = invocation.arguments or {}
        region_raw = args.get("region")
        region: Optional[Tuple[int, int, int, int]] = None
        if isinstance(region_raw, (list, tuple)) and len(region_raw) == 4:
            try:
                region = (int(region_raw[0]), int(region_raw[1]), int(region_raw[2]), int(region_raw[3]))
            except (TypeError, ValueError):
                region = None

        max_width_raw = args.get("max_width")
        try:
            max_width = int(max_width_raw) if max_width_raw is not None else 0
        except (TypeError, ValueError):
            max_width = 0
        if max_width <= 0:
            max_width = self._default_max_width

        # 采集后端不可用 → 优雅降级（不抛，返回成功 + 空文本 + 警告块）
        if self._capture is None:
            return ToolExecutionResult(
                tool_name=tool_name,
                success=True,
                content="(no screen capture backend installed; returning empty snapshot)",
                blocks=[
                    ResultBlock(
                        kind="text",
                        text=(
                            "[look_at_screen] 后端 ScreenCapture 未注入；"
                            "返回空快照。请在生产 wiring 处注入 pyautogui/mss/dxcam 后端；"
                            "测试场景下注入 FakeScreenCapture 即可。"
                        ),
                    ),
                ],
                structured_content={"text": "", "backend_available": False},
                timestamp_ms=int(time.time() * 1000),
                duration_ms=int(time.time() * 1000) - started_ms,
            )

        # 调用采集后端（捕获异常 → 失败 result，不抛）
        try:
            result = self._capture.capture(region=region)
        except Exception as exc:  # noqa: BLE001 - 边界处兜底
            logger.warning(f"look_at_screen 采集失败: {exc}", exc_info=True)
            return ToolExecutionResult(
                tool_name=tool_name,
                success=False,
                error_message=f"ScreenCapture.capture 失败: {type(exc).__name__}: {exc}",
                timestamp_ms=int(time.time() * 1000),
                duration_ms=int(time.time() * 1000) - started_ms,
            )

        # 采集后端返回 None（场景：无显示/无权限）→ 同样优雅降级
        if result.image is None:
            return ToolExecutionResult(
                tool_name=tool_name,
                success=True,
                content="(screen capture returned empty)",
                blocks=[
                    ResultBlock(
                        kind="text",
                        text="[look_at_screen] 屏幕采集后端返回空图像（可能无显示/无权限）。",
                    ),
                ],
                structured_content={"text": "", "backend_available": True, "image_empty": True},
                timestamp_ms=int(time.time() * 1000),
                duration_ms=int(time.time() * 1000) - started_ms,
            )

        # 可选 OCR/VLM 文本提取（异步：避免阻塞事件循环；reader.read 内部负责超时/降级）
        text = ""
        if self._reader is not None:
            try:
                question = args.get("question")
                if isinstance(question, str):
                    question = question.strip() or None
                text = await self._reader.read(
                    result.image,
                    mime_type=result.mime_type or "image/png",
                    question=question,
                )
            except Exception as exc:  # noqa: BLE001 - 边界处兜底
                logger.warning(f"look_at_screen TextReader 失败: {exc}", exc_info=True)
                text = ""

        # 缩放：当前不真做缩放，只在文本里声明 max_width
        #    简化原则：宁可不缩放也别误删信息。
        # 组装 result
        blocks: List[ResultBlock] = []
        if text:
            blocks.append(ResultBlock(kind="text", text=text))
        if result.image:
            encoded = base64.b64encode(result.image).decode("ascii")
            blocks.append(
                ResultBlock(
                    kind="image",
                    data=encoded,
                    mime_type=result.mime_type or "image/png",
                )
            )
        if not blocks:
            # 既无文本也无图像（极端情况）→ 放一个空文本兜底
            blocks.append(ResultBlock(kind="text", text=""))

        return ToolExecutionResult(
            tool_name=tool_name,
            success=True,
            content=text or "(no text extracted)",
            blocks=blocks,
            structured_content={
                "text": text,
                "width": int(result.width),
                "height": int(result.height),
                "region": list(result.region) if result.region else None,
                "max_width": int(max_width),
                "backend_available": True,
                "captured_at_ms": int(result.captured_at_ms),
            },
            timestamp_ms=int(time.time() * 1000),
            duration_ms=int(time.time() * 1000) - started_ms,
        )


# ---------------------------------------------------------------------------
# Fake 后端（测试 / 默认无依赖时使用）
# ---------------------------------------------------------------------------


class FakeScreenCapture:
    """测试用 ScreenCapture，可注入预置的截图结果序列。

    Example:
        >>> cap = FakeScreenCapture()
        >>> cap.queue_png(b"\\x89PNG...fake bytes...", width=1920, height=1080)
        >>> provider = LookAtScreenProvider(screen_capture=cap)
    """

    def __init__(self) -> None:
        self._queue: List[ScreenCaptureResult] = []
        self.calls: List[Optional[Tuple[int, int, int, int]]] = []

    def queue(self, result: ScreenCaptureResult) -> None:
        """入队一个采集结果（下次 capture 调用返回）。"""
        self._queue.append(result)

    def queue_png(self, image_bytes: bytes, *, width: int = 1920, height: int = 1080) -> None:
        """便捷方法：入队一个 PNG 图像。"""
        self.queue(
            ScreenCaptureResult(
                image=image_bytes,
                width=width,
                height=height,
                mime_type="image/png",
                captured_at_ms=int(time.time() * 1000),
            )
        )

    def capture(
        self,
        region: Optional[Tuple[int, int, int, int]] = None,
    ) -> ScreenCaptureResult:
        self.calls.append(region)
        if self._queue:
            return self._queue.pop(0)
        # 缺省：返回空 result（代表无显示/无图像）
        return ScreenCaptureResult()


class FakeTextReader:
    """测试用 TextReader，可注入预置的文本结果序列（异步契约）。"""

    def __init__(self) -> None:
        self._queue: List[str] = []
        self.calls = 0

    def queue_text(self, text: str) -> None:
        self._queue.append(text)

    async def read(
        self,
        image_bytes: bytes,
        *,
        mime_type: str = "image/png",
        question: Optional[str] = None,
    ) -> str:
        self.calls += 1
        if self._queue:
            return self._queue.pop(0)
        return ""


# ---------------------------------------------------------------------------
# VLM 实现（生产路径）：构造注入 llm_manager + prompt_manager
# ---------------------------------------------------------------------------


class LlmVisionTextReader:
    """通过 LLMManager.generate_vision 把图像转文本（异步，15s 超时降级）。

    降级语义（与 LookAtScreenProvider 的"空快照"约定一致）：
    - 成功 → 返回 response.content（已 strip）
    - 超时 → 返回 ``""`` + warning 日志
    - success=False → 返回 ``""`` + warning 日志
    - 异常 → 返回 ``""`` + warning 日志

    不抛、不缓存、不重试；契约面留扩展点给下游重写。

    Example:
        >>> reader = LlmVisionTextReader(llm_manager=llm_mgr, prompt_manager=prompt_mgr)
        >>> text = await reader.read(image_bytes, question="屏幕上有几个选项？")
    """

    def __init__(
        self,
        *,
        llm_manager: Any,
        prompt_manager: Optional[PromptManager] = None,
        timeout_s: float = DEFAULT_VLM_TIMEOUT_S,
    ) -> None:
        self._llm_manager = llm_manager
        self._prompt_manager = prompt_manager
        self._timeout_s = float(timeout_s)

    def _render_user_prompt(self, question: Optional[str]) -> str:
        """user prompt：优先用调用方传入的 question，否则渲染默认模板。"""
        if question:
            return question
        if self._prompt_manager is None:
            return "描述屏幕上正在显示的内容。"
        try:
            rendered = self._prompt_manager.render(SCREEN_VLM_PROMPT_KEY)
        except Exception as exc:  # noqa: BLE001 - 模板缺失/变量不匹配时降级到内置默认
            logger.warning(f"LlmVisionTextReader 渲染 {SCREEN_VLM_PROMPT_KEY} 失败: {exc}; 改用内置默认")
            return "描述屏幕上正在显示的内容。"
        return rendered or "描述屏幕上正在显示的内容。"

    def _render_system_prompt(self) -> Optional[str]:
        """system 模板（如注入 prompt_manager）：渲染 screen_vlm_system。"""
        if self._prompt_manager is None:
            return None
        try:
            return self._prompt_manager.render(SCREEN_VLM_SYSTEM_KEY)
        except Exception as exc:  # noqa: BLE001 - 模板缺失时降级为 None（系统无 system 也可调）
            logger.warning(f"LlmVisionTextReader 渲染 {SCREEN_VLM_SYSTEM_KEY} 失败: {exc}; system 留空")
            return None

    async def read(
        self,
        image_bytes: bytes,
        *,
        mime_type: str = "image/png",
        question: Optional[str] = None,
    ) -> str:
        """调一次 VLM；成功返回 content，失败/超时/异常返回 ``""``。"""
        prompt = self._render_user_prompt(question)
        system = self._render_system_prompt()
        try:
            response = await asyncio.wait_for(
                self._llm_manager.generate_vision(
                    prompt,
                    [image_bytes],
                    profile=ProfileNames.VISION,
                    system=system,
                ),
                timeout=self._timeout_s,
            )
        except asyncio.TimeoutError:
            logger.warning(f"LlmVisionTextReader VLM 调用超时 (>{self._timeout_s:.1f}s); 降级为空文本")
            return ""
        except Exception as exc:  # noqa: BLE001 - 边界处兜底（不抛）
            logger.warning(f"LlmVisionTextReader VLM 调用异常: {exc}", exc_info=True)
            return ""

        if not getattr(response, "success", False):
            err = getattr(response, "error", None)
            logger.warning(f"LlmVisionTextReader VLM 返回失败 (error={err!r}); 降级为空文本")
            return ""

        content = getattr(response, "content", None) or ""
        return content.strip()


__all__ = [
    "ScreenCapture",
    "ScreenCaptureResult",
    "TextReader",
    "LookAtScreenProvider",
    "LOOK_AT_SCREEN_SPEC",
    "build_look_at_screen_spec",
    "FakeScreenCapture",
    "FakeTextReader",
    "LlmVisionTextReader",
    "DEFAULT_VLM_TIMEOUT_S",
    "SCREEN_VLM_PROMPT_KEY",
    "SCREEN_VLM_SYSTEM_KEY",
]
