"""LLM 调用引擎 - 编排层

一次 LLM 调用的编排：按用途 profile 从 ``model_list`` 选型、单模型重试、
慢调用告警与故障切换。

设计要点：

- **选型策略**（profile 级 ``selection_strategy.name``）：
    - ``sequential``：按 model_list 顺序，第一个起跑
    - ``balance``：按累计调用计数挑最闲的（最少调用的优先）
    - ``random``：随机选一个
- **故障切换**：当前模型失败切下一个；超 ``slow_threshold_ms`` 仅告警（不切）。
  成功后立即返回，不再尝试。
- **厂商无关**：本模块不 import 任何厂商适配端（``clients/<vendor>/``），
  provider 客户端构造与能力解析一律经 ``clients`` 包的调度表完成。

profile 解析与 provider 池/模型索引的装配在 :mod:`src.modules.llm.bootstrap`。
"""

from __future__ import annotations

import asyncio
import random
import time
import uuid
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple, Union

from pydantic import BaseModel

from src.modules.llm.bootstrap import (
    ProfileNames,
    _ResolvedProfile,
    build_resolved_profile,
    index_models,
    register_providers,
    resolve_profile_name,
    validate_profile_binding,
)
from src.modules.llm.client import LLMResponse
from src.modules.llm.clients import resolve_client_method
from src.modules.llm.payload import GenerateRequest, ImagePart, Message, Response, TextPart, ToolCall, ToolSpec, Usage
from src.modules.logging import get_logger
from src.modules.storage.repos import LLMRepo

__all__ = ["LLMManager", "LLMResponse", "RetryConfig"]


class RetryConfig(BaseModel):
    """LLM 调用重试配置（provider 维度全局默认；profile 不覆盖）"""

    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 10.0


# === 契约归一化与新旧响应适配（Engine 的固定职责：消费方输入 → payload → Client）===

_PAYLOAD_METHODS = frozenset({"generate", "generate_vision"})
"""走中立 payload 契约的客户端能力名（Client 返回 payload.Response）。"""


def _content_to_parts(content: Any) -> List[Any]:
    """OpenAI 风格 content（str / 分段列表）→ 中立 parts 列表"""
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        raise TypeError(f"不支持的消息 content 类型: {type(content).__name__}")
    parts: List[Any] = []
    for piece in content:
        if isinstance(piece, str):
            parts.append(piece)
        elif isinstance(piece, dict) and piece.get("type") == "text":
            parts.append(TextPart(text=str(piece.get("text", ""))))
        elif isinstance(piece, dict) and piece.get("type") == "image_url":
            image_url = piece.get("image_url")
            url = image_url.get("url", "") if isinstance(image_url, dict) else str(image_url or "")
            parts.append(ImagePart(image=url))
        else:
            raise TypeError(f"不支持的消息 content 片段: {type(piece).__name__}")
    return parts


def _normalize_generate_input(
    input: Any,
    *,
    system: Optional[str],
    tools: Optional[List[Any]],
    temperature: Optional[float],
    max_tokens: Optional[int],
) -> GenerateRequest:
    """消费方输入（str 或 OpenAI 风格 dict 列表或 Message 列表）→ 中立请求。

    dict 列表里的 system 消息折叠进请求的独立 system 参数（各厂商对
    system 的承载位置不同，由适配端翻译）。
    """
    messages: List[Message] = []
    system_texts: List[str] = []
    if isinstance(input, str):
        messages.append(Message(role="user", parts=[input]))
    elif isinstance(input, list):
        for item in input:
            if isinstance(item, Message):
                messages.append(item)
            elif isinstance(item, dict):
                role = str(item.get("role", "user"))
                content = item.get("content", "")
                if role == "system":
                    parts = _content_to_parts(content)
                    system_texts.append("".join(p if isinstance(p, str) else (p.text or "") for p in parts))
                    continue
                if role not in ("user", "assistant", "tool"):
                    raise ValueError(f"不支持的消息 role: {role!r}")
                messages.append(Message(role=role, parts=_content_to_parts(content)))
            else:
                raise TypeError(f"不支持的消息类型: {type(item).__name__}")
    else:
        raise TypeError(f"不支持的输入类型: {type(input).__name__}")

    merged_system = "\n\n".join(([system] if system else []) + system_texts) or None
    tool_specs: List[ToolSpec] = []
    for tool in tools or []:
        if isinstance(tool, ToolSpec):
            tool_specs.append(tool)
        elif isinstance(tool, dict):
            tool_specs.append(
                ToolSpec(
                    name=str(tool.get("name", "")),
                    description=str(tool.get("description", "")),
                    parameters=tool.get("parameters") or {},
                )
            )
        else:
            raise TypeError(f"不支持的 tools 元素类型: {type(tool).__name__}")

    return GenerateRequest(
        messages=messages,
        system=merged_system,
        tools=tool_specs,
        temperature=temperature,
        max_tokens=max_tokens,
    )


def _payload_response_to_legacy(resp: Response) -> LLMResponse:
    """payload.Response → 遗留 LLMResponse（记账/请求历史链路仍消费遗留形状）"""
    return LLMResponse(
        success=resp.success,
        content=resp.content,
        model=resp.model,
        usage=(
            {
                k: v
                for k, v in {
                    "prompt_tokens": resp.usage.prompt_tokens,
                    "completion_tokens": resp.usage.completion_tokens,
                    "total_tokens": resp.usage.total_tokens,
                    "cache_hit_tokens": resp.usage.cache_hit_tokens,
                    "cache_miss_tokens": resp.usage.cache_miss_tokens,
                }.items()
                if v is not None
            }
            if resp.usage is not None
            else None
        ),
        tool_calls=[
            {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": tc.arguments}}
            for tc in resp.tool_calls
        ],
        reasoning_content=resp.reasoning_content,
        error=resp.error,
        request_id=resp.request_id,
    )


def _legacy_response_to_payload(result: LLMResponse) -> Response:
    """遗留 LLMResponse → payload.Response（Engine 对外统一返回中立形状）"""
    tool_calls = [
        ToolCall(
            id=tc.get("id", ""),
            name=tc.get("function", {}).get("name", ""),
            arguments=tc.get("function", {}).get("arguments", {}),
        )
        for tc in (result.tool_calls or [])
        if isinstance(tc, dict)
    ]
    usage = None
    if result.usage is not None:
        usage = Usage(
            prompt_tokens=result.usage.get("prompt_tokens", 0),
            completion_tokens=result.usage.get("completion_tokens", 0),
            total_tokens=result.usage.get("total_tokens", 0),
            cache_hit_tokens=result.usage.get("cache_hit_tokens"),
            cache_miss_tokens=result.usage.get("cache_miss_tokens"),
        )
    return Response(
        success=result.success,
        content=result.content,
        tool_calls=tool_calls,
        usage=usage,
        model=result.model,
        reasoning_content=result.reasoning_content,
        error=result.error,
        request_id=result.request_id,
    )


class LLMManager:
    """LLM 管理器 - profile 驱动的多模型故障切换

    核心基础设施服务，与 EventBus 同级。

    职责：
    - 按 profile.model_list 调度模型选择（sequential / balance / random）
    - 实现故障切换（当前模型失败切下一个）与慢调用告警
    - 内置重试（仅在同一模型上重试，不跨模型）
    - Token 使用量统计 + llm_usage 落库

    provider 池/模型索引/profile 解析快照的构建委托给 bootstrap 模块。

    使用示例：
        ```python
        # 在 main.py 中初始化
        llm_manager = LLMManager()
        await llm_manager.setup(config["model"])

        # 调用（按用途 profile 名）
        response = await llm_manager.chat_messages(
            [{"role": "user", "content": "你好"}],
            client_type="planner",
        )
        ```
    """

    def __init__(self, llm_repo: Optional[LLMRepo] = None):
        self.logger = get_logger("LLMManager")
        # provider_name -> provider 配置 + 客户端实例（共享连接）
        self._providers: Dict[str, Tuple[Dict[str, Any], Any]] = {}
        # provider_name -> 客户端实例（仅客户端引用，便于快速查找）
        self._provider_clients: Dict[str, Any] = {}
        # model_name -> model 配置 + 关联 provider_name
        self._models: Dict[str, Tuple[Dict[str, Any], str]] = {}
        # profile_name -> _ResolvedProfile 快照
        self._profiles: Dict[str, _ResolvedProfile] = {}
        # profile_name -> 累计调用次数（balance 策略使用）
        self._profile_call_counts: Dict[str, int] = {}
        # profile_name -> 各 model 已调用次数（balance 策略使用）
        self._model_call_counts: Dict[str, Dict[str, int]] = {}
        self._config: Dict[str, Any] = {}
        self._token_manager = None
        self._retry_config = RetryConfig()
        # 注入后每次成功调用旁路写一条 llm_usage（失败降级不阻断调用）；None 时不落库
        self._llm_repo = llm_repo
        # 随机策略 RNG（lazy 创建，按 seed 决定是否固定）
        self._rng: Optional[random.Random] = None

    # === setup / 生命周期 ===

    async def setup(self, config: Dict[str, Any]) -> None:
        """从三层配置（providers / models / profiles）初始化 LLM 运行时。

        Args:
            config: ``config["model"]`` 子树，含：
                - ``llm_providers``: list[dict]，每个 provider 含 name/client_type/base_url/api_key 等
                - ``llm_models``: list[dict]，模型清单（name / model_identifier / api_provider / 价格）
                - ``llm_profiles``: dict[str, dict]，用途 profile；model_list 必填

        配置示例：
            ```toml
            [[llm_providers]]
            name = "deepseek"
            client_type = "<clients 调度表中注册的客户端类型>"
            base_url = "https://api.deepseek.com/v1"
            api_key = "sk-xxx"

            [[llm_models]]
            name = "ds-chat"
            model_identifier = "deepseek-chat"
            api_provider = "deepseek"

            [llm_profiles.planner]
            model_list = ["ds-chat"]
            selection_strategy = { name = "sequential" }
            hard_timeout_ms = 90000
            ```
        """
        self._config = config

        # 重置状态：setup 可重复调用（热重载 / 测试 re-setup 共用实例场景）
        self._providers.clear()
        self._provider_clients.clear()
        self._models.clear()
        self._profiles.clear()
        self._profile_call_counts.clear()
        self._model_call_counts.clear()
        self._rng = None

        provider_configs = config.get("llm_providers") or []
        if not provider_configs:
            raise ValueError("LLMManager.setup 收到空 llm_providers（至少需要 1 个 provider）")

        # provider 客户端注册（每个 provider 一个连接）与模型索引由 bootstrap 承担
        self._providers, self._provider_clients = register_providers(provider_configs, self.logger)
        self._models = index_models(config.get("llm_models") or [], self._providers)

        # 解析 llm_profiles 快照（封闭集合校验：未知用途在装配期硬错）
        profile_configs = config.get("llm_profiles") or {}
        for pname, pcfg in profile_configs.items():
            validate_profile_binding(pname)
            self._profiles[pname] = build_resolved_profile(pname, pcfg, self._models)
            self._profile_call_counts[pname] = 0
            self._model_call_counts[pname] = {}

        # 初始化 token manager
        # 必须函数体内 import：测试用 patch 拦截（src.modules.llm.clients.token_usage_manager.TokenUsageManager），
        # 顶部 import 会使 patch 失效（参见 tests/modules/llm/test_llm_manager.py）
        from src.modules.llm.clients.token_usage_manager import TokenUsageManager

        self._token_manager = TokenUsageManager(use_global=True)
        # 价格唯一来源 = model.toml [[llm_models]] 的定价字段
        # 价格表按 model_identifier 键入（费用查询用的是请求实际的 API 模型标识）
        self._token_manager.set_model_prices(
            {
                mcfg.get("model_identifier") or mname: {
                    "price_in": mcfg.get("price_in", 0.0),
                    "price_out": mcfg.get("price_out", 0.0),
                    "cache_price_in": mcfg.get("cache_price_in", 0.0),
                    "cache": mcfg.get("cache", ""),
                }
                for mname, (mcfg, _prov) in self._models.items()
                if mcfg.get("price_in", 0.0) > 0 or mcfg.get("price_out", 0.0) > 0
            }
        )

        self.logger.info(
            f"LLMManager 初始化完成，providers: {list(self._providers.keys())}, profiles: {list(self._profiles.keys())}"
        )

    # === 公共 API：generate / generate_vision（冻结的两个对外入口）

    async def generate(
        self,
        input: Union[str, List[Union[Message, Dict[str, Any]]]],
        *,
        profile: Optional[str] = None,
        system: Optional[str] = None,
        tools: Optional[List[Union[ToolSpec, Dict[str, Any]]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        on_delta: Optional[Callable[[str, str], None]] = None,
        interrupt: Optional[asyncio.Event] = None,
    ) -> Response:
        """执行一次 LLM 调用（对外唯一文本入口，返回中立 payload.Response）。

        签名宽是刻意的容错设计：``input`` 接受裸字符串（单轮用户消息）、
        中立 ``Message`` 列表或 OpenAI 风格 dict 列表——Engine 统一归一化
        到 payload 再调度，消费方不必为迁就契约改自己的输入形状。
        ``temperature`` / ``max_tokens`` 缺省时回退 profile 档位。
        """
        profile_name = self._resolve_profile_name(profile)
        request = _normalize_generate_input(
            input, system=system, tools=tools, temperature=temperature, max_tokens=max_tokens
        )
        result = await self._call_with_failover(
            profile_name,
            method="generate",
            request=request,
            on_delta=on_delta,
            interrupt_flag=interrupt,
        )
        return _legacy_response_to_payload(result)

    async def generate_vision(
        self,
        prompt: str,
        images: List[Any],
        *,
        profile: Optional[str] = None,
        system: Optional[str] = None,
        interrupt: Optional[asyncio.Event] = None,
    ) -> Response:
        """执行一次视觉调用（对外唯一视觉入口，返回中立 payload.Response）。

        签名宽是刻意的容错设计：``images`` 接受路径 / URL / 原始字节的
        混合列表，具体编码方式由适配端按厂商协议翻译。
        ``profile`` 缺省走 vision 档位。
        """
        if profile is None:
            profile_name = ProfileNames.VISION
        else:
            profile_name = self._resolve_profile_name(profile)
        request = _normalize_generate_input(prompt, system=system, tools=None, temperature=None, max_tokens=None)
        result = await self._call_with_failover(
            profile_name,
            method="generate_vision",
            request=request,
            images=images,
            interrupt_flag=interrupt,
        )
        return _legacy_response_to_payload(result)

    # === 公共 API（遗留，过渡期保留）：chat / chat_messages / chat_vision / stream_chat / call_tools / simple_*

    async def chat(
        self,
        prompt: str,
        *,
        client_type: Optional[str] = None,
        system_message: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        """聊天调用（按用途 profile 名走 model_list 选择 + 故障切换）"""
        profile_name = self._resolve_profile_name(client_type)
        messages = self._build_messages(prompt, system_message)
        return await self._call_with_failover(
            profile_name,
            method="chat",
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    async def chat_fast(self, prompt: str, **kwargs: Any) -> LLMResponse:
        """快速聊天（语义别名：等价 ``chat(client_type='replyer')``，保留向后兼容）"""
        return await self.chat(prompt, client_type=ProfileNames.REPLYER, **kwargs)

    async def chat_messages(
        self,
        messages: List[Dict[str, Any]],
        *,
        client_type: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        tools: Optional[List[Dict[str, Any]]] = None,
        on_delta: Optional[Callable[[str, str], None]] = None,
    ) -> LLMResponse:
        """聊天调用（messages 列表 + 可选 tools + 可选流式回调）"""
        profile_name = self._resolve_profile_name(client_type)
        return await self._call_with_failover(
            profile_name,
            method="chat",
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            on_delta=on_delta,
        )

    async def stream_chat(
        self,
        prompt: str,
        *,
        client_type: Optional[str] = None,
        system_message: Optional[str] = None,
        stop_event: Optional[asyncio.Event] = None,
    ) -> AsyncIterator[str]:
        """流式聊天调用（不参与故障切换——流式语义不允许多模型切换；单模型失败即停）"""
        profile_name = self._resolve_profile_name(client_type)
        resolved = self._get_profile(profile_name)
        if not resolved.models:
            raise ValueError(f"profile {profile_name!r} 无可用模型")
        first = resolved.models[0]
        client = self._provider_clients[first.provider_name]
        messages = self._build_messages(prompt, system_message)
        stream = resolve_client_method(client, "stream_chat")
        async for chunk in stream(
            messages=messages,
            model=first.model_identifier,
            stop_event=stop_event,
        ):
            yield chunk

    async def chat_vision(
        self,
        prompt: str,
        images: List[Any],
        *,
        client_type: Optional[str] = None,
        system_message: Optional[str] = None,
    ) -> LLMResponse:
        """视觉理解调用（默认走 vision profile，可通过 client_type 覆盖）"""
        # chat_vision 与 chat 的默认 profile 不同：默认走 vision，
        # 调用方可通过 client_type 显式指向其他 profile。
        if client_type is None:
            profile_name = ProfileNames.VISION
        else:
            profile_name = self._resolve_profile_name(client_type)
        messages = self._build_messages(prompt, system_message)
        return await self._call_with_failover(
            profile_name,
            method="vision",
            messages=messages,
            images=images,
        )

    async def call_tools(
        self,
        prompt: str,
        tools: List[Dict[str, Any]],
        *,
        client_type: Optional[str] = None,
        system_message: Optional[str] = None,
        on_delta: Optional[Callable[[str, str], None]] = None,
    ) -> LLMResponse:
        """工具调用（按用途 profile 走 model_list + 故障切换）"""
        profile_name = self._resolve_profile_name(client_type)
        messages = self._build_messages(prompt, system_message)
        response = await self._call_with_failover(
            profile_name,
            method="chat",
            messages=messages,
            tools=tools,
            on_delta=on_delta,
        )
        self.logger.warning(
            f"[诊断] call_tools 完成: profile={profile_name}, success={getattr(response, 'success', None)}, "
            f"error={getattr(response, 'error', None)!r}, "
            f"tool_calls={[tc.get('function', {}).get('name') for tc in (getattr(response, 'tool_calls', None) or []) if isinstance(tc, dict)]}, "
            f"content[:200]={(getattr(response, 'content', None) or '')[:200]!r}"
        )
        return response

    async def simple_chat(
        self,
        prompt: str,
        *,
        client_type: Optional[str] = None,
        system_message: Optional[str] = None,
    ) -> str:
        result = await self.chat(prompt, client_type=client_type, system_message=system_message)
        return result.content if result.success and result.content else f"错误: {result.error}"

    async def simple_vision(
        self,
        prompt: str,
        images: List[Any],
        *,
        client_type: Optional[str] = None,
    ) -> str:
        result = await self.chat_vision(prompt, images, client_type=client_type)
        return result.content if result.success and result.content else f"错误: {result.error}"

    # === 客户端 / profile 信息查询（兼容旧 API 形式）===

    def get_client(self, client_type: Optional[str] = None):
        """获取指定用途 profile 的首个模型对应 provider 客户端

        旧 API 形式保留（部分测试与外部探针依赖）；新代码应使用
        :func:`get_provider_client` 与 :func:`has_profile`。
        """
        profile_name = self._resolve_profile_name(client_type)
        resolved = self._get_profile(profile_name)
        if not resolved.models:
            raise ValueError(f"profile {profile_name!r} 无可用模型")
        return self._provider_clients[resolved.models[0].provider_name]

    def get_provider_client(self, provider_name: str):
        """按 provider name 获取共享客户端（不存在则抛 ValueError）"""
        if provider_name not in self._provider_clients:
            raise ValueError(f"provider {provider_name!r} 不存在（已注册: {sorted(self._provider_clients.keys())}）")
        return self._provider_clients[provider_name]

    def has_client(self, client_type: str) -> bool:
        """是否已配置指定用途 profile（兼容旧 API）"""
        return self.has_profile(client_type)

    def has_profile(self, profile_name: str) -> bool:
        """是否已配置指定用途 profile"""
        return profile_name in self._profiles

    def list_clients(self) -> List[str]:
        """列出所有已配置的用途 profile（兼容旧 API：返回 profile 名）"""
        return list(self._profiles.keys())

    def list_providers(self) -> List[str]:
        """列出所有已注册的 provider 名"""
        return list(self._providers.keys())

    def has_provider(self, provider_name: str) -> bool:
        """是否已注册指定 provider"""
        return provider_name in self._providers

    def list_models(self) -> List[str]:
        """列出所有已注册的 model 名"""
        return list(self._models.keys())

    def get_client_config(self, profile_name: str) -> Optional[Dict[str, Any]]:
        """获取指定用途 profile 的运行时配置（model_list / 阈值 / 温度等）"""
        resolved = self._profiles.get(profile_name)
        if resolved is None:
            return None
        return {
            "profile_name": resolved.profile_name,
            "hard_timeout_ms": resolved.hard_timeout_ms,
            "slow_threshold_ms": resolved.slow_threshold_ms,
            "selection_strategy": resolved.selection_strategy,
            "temperature": resolved.temperature,
            "max_tokens": resolved.max_tokens,
            "models": [
                {
                    "model_name": m.model_name,
                    "model_identifier": m.model_identifier,
                    "provider_name": m.provider_name,
                }
                for m in resolved.models
            ],
        }

    def get_client_info(self) -> Dict[str, Any]:
        """获取所有已注册 provider 的客户端信息（兼容旧 API：返回 profile 视角）"""
        info: Dict[str, Any] = {}
        for provider_name, client in self._provider_clients.items():
            info[provider_name] = {
                "client": client.__class__.__name__,
                "profiles": [
                    pname
                    for pname, resolved in self._profiles.items()
                    if any(m.provider_name == provider_name for m in resolved.models)
                ],
            }
        return info

    # === 内部：profile 解析 ===

    def _resolve_profile_name(self, client_type: Optional[str]) -> str:
        """把 ``client_type`` 参数解析为 profile 名（解析规则见 bootstrap 模块）。"""
        return resolve_profile_name(client_type, self._profiles, self.logger)

    def _get_profile(self, profile_name: str) -> _ResolvedProfile:
        if profile_name not in self._profiles:
            raise ValueError(f"profile {profile_name!r} 未配置。已配置的 profile: {list(self._profiles.keys())}")
        return self._profiles[profile_name]

    def _build_messages(
        self,
        prompt: str,
        system_message: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """构建消息列表"""
        messages = []
        if system_message:
            messages.append({"role": "system", "content": system_message})
        messages.append({"role": "user", "content": prompt})
        return messages

    # === 内部：模型选择（按 selection_strategy 排序）===

    def _select_models_for_call(self, profile: _ResolvedProfile) -> List[Any]:
        """按 profile.selection_strategy 返回本次尝试的模型顺序

        - sequential：原序（首个起跑）
        - balance：调用次数最少的优先，并列时按 model_list 原序稳定
        - random：随机打乱
        """
        models = list(profile.models)
        strategy = profile.selection_strategy
        if strategy == "sequential":
            return models
        if strategy == "balance":
            counts = self._model_call_counts.get(profile.profile_name, {})
            indexed = list(enumerate(models))
            indexed.sort(key=lambda pair: (counts.get(pair[1].model_name, 0), pair[0]))
            return [m for _, m in indexed]
        if strategy == "random":
            rng = self._get_rng(profile.seed)
            shuffled = list(models)
            rng.shuffle(shuffled)
            return shuffled
        # 防御：理论上 bootstrap 构建快照时已校验
        return models

    def _get_rng(self, seed: int) -> random.Random:
        """返回 random RNG。seed=0 表示不固定（每次新建）；>0 每次新建固定 RNG。

        固定 seed 时返回新实例，避免上次 shuffle 残留状态污染本次选择——
        这是 ``test_random_strategy_with_seed`` 等可重现测试的前提。
        """
        if seed == 0:
            return random.Random()
        return random.Random(seed)

    # === 内部：故障切换调用 ===

    async def _call_with_failover(
        self,
        profile_name: str,
        method: str,
        **kwargs: Any,
    ) -> LLMResponse:
        """按 model_list 顺序逐个尝试；当前模型失败（超时或异常）切下一个。

        Returns:
            LLMResponse：首个成功的模型响应；全部失败则返回 success=False 且
            ``error`` 字段列出尝试过的所有模型名。
        """
        profile = self._get_profile(profile_name)
        models_to_try = self._select_models_for_call(profile)

        request_id = f"req_{uuid.uuid4().hex[:12]}"
        start_time = time.time()
        self._profile_call_counts[profile_name] = self._profile_call_counts.get(profile_name, 0) + 1

        attempted_models: List[str] = []
        last_error: Optional[str] = None

        for idx, resolved_model in enumerate(models_to_try):
            attempted_models.append(resolved_model.model_name)
            client = self._provider_clients[resolved_model.provider_name]
            model_identifier = resolved_model.model_identifier
            provider_cfg = self._providers[resolved_model.provider_name][0]

            # balance 策略计数器：本次尝试（无论成败）计入该 model
            self._model_call_counts.setdefault(profile_name, {})[resolved_model.model_name] = (
                self._model_call_counts[profile_name].get(resolved_model.model_name, 0) + 1
            )

            response, error = await self._call_one_with_retry(
                method=method,
                client=client,
                provider_cfg=provider_cfg,
                model_identifier=model_identifier,
                model_name=resolved_model.model_name,
                profile_name=profile_name,
                request_id=request_id,
                start_time=start_time,
                slow_threshold_ms=profile.slow_threshold_ms,
                **kwargs,
            )

            if response is not None:
                response.request_id = request_id
                if idx > 0:
                    self.logger.warning(
                        f"[LLM 故障切换] profile={profile_name} 切到 {resolved_model.model_name}（之前尝试 {attempted_models[:-1]} 失败）"
                    )
                return response

            last_error = error
            self.logger.warning(
                f"[LLM 故障切换] profile={profile_name} 模型 {resolved_model.model_name} 调用失败，"
                f"尝试下一个：错误={error}"
            )

        # 全部失败
        self.logger.error(
            f"[LLM 全部失败] profile={profile_name} 尝试模型 {attempted_models} 均失败，最后错误={last_error}"
        )
        result = LLMResponse(
            success=False,
            content=None,
            error=f"全部模型失败 {attempted_models}: {last_error}",
            request_id=request_id,
        )
        self._record_request_history(
            request_id=request_id,
            client_type=profile_name,
            result=result,
            kwargs=kwargs,
            start_time=start_time,
        )
        return result

    async def _call_one_with_retry(
        self,
        *,
        method: str,
        client: Any,
        provider_cfg: Dict[str, Any],
        model_identifier: str,
        model_name: str,
        profile_name: str,
        request_id: str,
        start_time: float,
        slow_threshold_ms: int,
        **kwargs: Any,
    ) -> Tuple[Optional[LLMResponse], Optional[str]]:
        """单模型上的重试 + 慢调用告警 + 硬超时。

        Returns:
            (response, error)：成功 → (LLMResponse, None)；失败 → (None, 错误描述)
        """
        call_kwargs = dict(kwargs)
        call_kwargs["model"] = model_identifier
        # profile 生成参数为具体值（禁 None）：调用方未显式给值时用 profile 档位
        profile = self._get_profile(profile_name)
        if call_kwargs.get("temperature") is None:
            call_kwargs["temperature"] = profile.temperature
        if call_kwargs.get("max_tokens") is None:
            call_kwargs["max_tokens"] = profile.max_tokens

        max_retries = int(provider_cfg.get("max_retries", self._retry_config.max_retries) or 0)
        base_delay = float(provider_cfg.get("retry_delay", self._retry_config.base_delay) or 0.0)
        max_delay = self._retry_config.max_delay
        # max_retries=0 仍要给 1 次首次尝试；N>0 给 N 次（含首次）
        total_attempts = max(max_retries, 1)

        last_error: Optional[str] = None
        for attempt in range(total_attempts):
            attempt_start = time.time()
            try:
                # 能力解析走 clients 调度表，不在引擎层对客户端做 getattr 动态分派
                method_func = resolve_client_method(client, method)
                response = await method_func(**call_kwargs)
                if method in _PAYLOAD_METHODS:
                    # 中立 payload 契约路径：Client 返回 payload.Response，
                    # 记账/请求历史链路仍消费遗留形状，此处统一适配
                    response = _payload_response_to_legacy(response)
                attempt_elapsed_ms = int((time.time() - attempt_start) * 1000)
                if attempt_elapsed_ms >= slow_threshold_ms:
                    self.logger.warning(
                        f"[LLM 慢调用告警] profile={profile_name} model={model_name} "
                        f"耗时 {attempt_elapsed_ms}ms（阈值 {slow_threshold_ms}ms，不切换）"
                    )
                if response.success:
                    await self._post_success(
                        request_id=request_id,
                        profile_name=profile_name,
                        model_name=model_name,
                        method=method,
                        result=response,
                        kwargs=call_kwargs,
                        start_time=start_time,
                    )
                    return response, None
                last_error = response.error or "未知客户端错误"
            except asyncio.CancelledError:
                raise
            except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
                last_error = f"{type(exc).__name__}: {exc}"

            if attempt < total_attempts - 1:
                delay = min(base_delay * (2**attempt), max_delay)
                if delay > 0:
                    await asyncio.sleep(delay)

        return None, last_error or "未知错误"

    async def _post_success(
        self,
        *,
        request_id: str,
        profile_name: str,
        model_name: str,
        method: str,
        result: LLMResponse,
        kwargs: Dict[str, Any],
        start_time: float,
    ) -> None:
        """成功路径后置动作：token 记录 + llm_usage 落库 + 请求历史"""
        if result.usage and self._token_manager:
            self._token_manager.record_usage(
                model_name=result.model or model_name,
                prompt_tokens=result.usage.get("prompt_tokens", 0),
                completion_tokens=result.usage.get("completion_tokens", 0),
                total_tokens=result.usage.get("total_tokens", 0),
            )
        if result.usage and self._llm_repo:
            duration_ms = int((time.time() - start_time) * 1000)
            try:
                await self._persist_llm_usage(
                    profile_name=profile_name,
                    model_name=model_name,
                    method=method,
                    result=result,
                    duration_ms=duration_ms,
                )
            except Exception as exc:  # noqa: BLE001
                # 兜底（_persist_llm_usage 内部已 try/except；此处防止传播异常）
                self.logger.warning(f"llm_usage 落库包装失败: {exc}")
        self._record_request_history(
            request_id=request_id,
            client_type=profile_name,
            result=result,
            kwargs=kwargs,
            start_time=start_time,
        )

    async def _persist_llm_usage(
        self,
        *,
        profile_name: str,
        model_name: str,
        method: str,
        result: LLMResponse,
        duration_ms: int,
    ) -> None:
        """把一次成功调用的 token 消耗写入 ``llm_usage`` 表（``LLMRepo`` 注入时生效）。

        费用口径与请求历史一致（同走 ``TokenUsageManager._calculate_cost``）；
        任何写入失败只记 warning，绝不阻断 LLM 调用链。
        """
        try:
            usage = result.usage or {}
            cost = 0.0
            if self._token_manager is not None:
                cost_info = self._token_manager._calculate_cost(
                    result.model or model_name,
                    usage.get("prompt_tokens", 0),
                    usage.get("completion_tokens", 0),
                )
                cost = float(cost_info.get("cost", 0.0))
            provider_name = "unknown"
            if model_name in self._models:
                _, provider_name = self._models[model_name]
            await self._llm_repo.insert_llm_usage(
                model_name=result.model or model_name,
                provider_name=str(provider_name),
                request_type=method,
                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                completion_tokens=int(usage.get("completion_tokens", 0)),
                total_tokens=int(usage.get("total_tokens", 0)),
                cache_hit_tokens=int(usage.get("cache_hit_tokens", 0)),
                cache_miss_tokens=int(usage.get("cache_miss_tokens", 0)),
                cost=cost,
                duration_ms=duration_ms,
                profile_name=profile_name,
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(f"llm_usage 落库失败: {exc}")

    def _record_request_history(
        self,
        request_id: str,
        client_type: str,
        result: LLMResponse,
        kwargs: Dict[str, Any],
        start_time: float,
    ) -> None:
        """记录请求历史（成功 / 失败路径均调用）"""
        try:
            # 必须函数体内 import：测试用 patch 拦截该路径，顶部 import 会使 patch 失效
            from src.modules.llm.request_history_manager import (
                RequestRecord,
                TokenUsage,
                get_global_request_history_manager,
            )

            latency_ms = int((time.time() - start_time) * 1000)
            model_name = result.model or "unknown"

            request_params = {
                "messages": kwargs.get("messages"),
                "temperature": kwargs.get("temperature"),
                "max_tokens": kwargs.get("max_tokens"),
                "tools": kwargs.get("tools"),
            }
            if request_params["messages"] is None and kwargs.get("request") is not None:
                # 中立 payload 契约路径：消息以 GenerateRequest 承载
                request_params["messages"] = [m.model_dump() for m in kwargs["request"].messages]
                request_params["tools"] = [t.model_dump() for t in kwargs["request"].tools] or None
            request_params = {k: v for k, v in request_params.items() if v is not None}

            usage = None
            if result.usage:
                usage = TokenUsage(
                    prompt_tokens=result.usage.get("prompt_tokens", 0),
                    completion_tokens=result.usage.get("completion_tokens", 0),
                    total_tokens=result.usage.get("total_tokens", 0),
                )

            cost = 0.0
            if usage and self._token_manager:
                cost_info = self._token_manager._calculate_cost(
                    model_name, usage.prompt_tokens, usage.completion_tokens
                )
                cost = cost_info.get("cost", 0.0)

            record = RequestRecord(
                request_id=request_id,
                client_type=client_type,
                model_name=model_name,
                request_params=request_params,
                response_content=result.content,
                reasoning_content=result.reasoning_content,
                tool_calls=result.tool_calls or [],
                usage=usage,
                cost=cost,
                success=result.success,
                error=result.error,
                latency_ms=latency_ms,
            )

            history_manager = get_global_request_history_manager()
            history_manager.record_request(record)
        except Exception as e:  # noqa: BLE001
            self.logger.warning(f"记录请求历史失败: {e}")

    # === 清理 ===

    async def cleanup(self) -> None:
        """清理所有 provider 客户端资源（按 id 去重）"""
        cleaned: set[int] = set()
        for name, client in self._provider_clients.items():
            client_id = id(client)
            if client_id in cleaned:
                continue
            cleaned.add(client_id)
            try:
                await client.cleanup()
                self.logger.debug(f"已清理 provider '{name}' 客户端")
            except Exception as e:  # noqa: BLE001
                self.logger.warning(f"清理 provider '{name}' 客户端失败: {e}")
        self._provider_clients.clear()
        self._providers.clear()
        self._models.clear()
        self._profiles.clear()
        self._profile_call_counts.clear()
        self._model_call_counts.clear()

    # === 统计 ===

    def get_token_usage_summary(self) -> str:
        if self._token_manager:
            return self._token_manager.format_total_cost_summary()
        return "Token 管理器未初始化"
