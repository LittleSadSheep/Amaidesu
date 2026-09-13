"""Agent 实例化工厂（配置名 → 具体类）

配置名 → 具体类的唯一映射，是启动装配与 Dashboard 动态启停共享的
单一构造路径：所有 Agent 实例化都经 ``instantiate_agent``，组合根只
负责收集基础设施服务并透传，不自行 new Agent 类。

配置名映射：
- streamer   → StreamerAgent
- minecraft  → MinecraftAgent
- text_adv   → TextAdvGameAgent

Agent 间无分类层；每个 Agent 是一等公民。
"""

from __future__ import annotations

from typing import Any, Optional

from src.modules.agents.base import BaseAgent
from src.modules.logging import get_logger

# 已实现的 Agent 注册名
SUPPORTED_AGENTS: tuple[str, ...] = ("streamer", "minecraft", "text_adv")

_logger = get_logger("AgentFactory")


def instantiate_agent(
    name: str,
    config: Optional[dict[str, Any]],
    *,
    llm_manager: Any,
    prompt_manager: Any,
    event_bus: Any = None,
    tool_registry: Any = None,
    memory: Any = None,
    thinking_sink: Optional[Any] = None,
    speech_config: Optional[dict[str, Any]] = None,
    tts_engine: Optional[Any] = None,
    subtitle_service: Optional[Any] = None,
    session_manager: Optional[Any] = None,
    context_assembler_config: Optional[Any] = None,
    task_tracker: Optional[Any] = None,
) -> Optional[BaseAgent]:
    """按名实例化 Agent；未知名字返回 None。

    基础设施参数按 Agent 各自消费面透传（未列出的 Agent 忽略对应参数）：
    - streamer：memory / thinking_sink / speech_config / tts_engine /
      subtitle_service / session_manager / context_assembler_config
    - minecraft：thinking_sink / task_tracker；llm_profile 使用 Agent
      类默认值（``[llm_profiles.minecraft]`` 段）
    - text_adv：仅基础四件套

    ``thinking_sink`` 关键字参数透传给 StreamerAgent 与 MinecraftAgent
    （鸭子类型：任何带 ``on_thinking_delta`` 方法的对象）；缺省 None 时
    两 Agent 的思考流旁路整体短路，决策循环行为与无旁路完全一致。
    """
    config = config if isinstance(config, dict) else {}

    if name == "streamer":
        from src.agents.streamer.config import StreamerConfig
        from src.agents.streamer.streamer_agent import StreamerAgent

        try:
            cfg_obj = StreamerConfig.from_dict(config) if config else StreamerConfig()
        except Exception as exc:
            _logger.warning(f"解析 StreamerConfig 配置失败: {exc}; 使用默认配置")
            cfg_obj = StreamerConfig()
        _logger.info(
            f"StreamerAgent 配置就绪: "
            f"bot_name={cfg_obj.persona.bot_name!r}, "
            f"audience_salutation={cfg_obj.persona.audience_salutation!r}, "
            f"behavior_style={'<已注入>' if cfg_obj.persona.behavior_style else '<缺失>'}, "
            f"background.enabled={cfg_obj.background.enabled}, "
            f"background.light_tick_ms={cfg_obj.background.light_tick_ms}"
        )
        return StreamerAgent(
            config=cfg_obj,
            llm_manager=llm_manager,
            prompt_manager=prompt_manager,
            event_bus=event_bus,
            tool_registry=tool_registry,
            memory=memory,
            context_assembler_config=context_assembler_config,
            speech_config=speech_config,
            tts_engine=tts_engine,
            subtitle_service=subtitle_service,
            session_manager=session_manager,
            thinking_sink=thinking_sink,
        )

    if name == "minecraft":
        from src.agents.minecraft import MinecraftAgent
        from src.agents.minecraft.config import MinecraftConfig

        try:
            minecraft_cfg = MinecraftConfig(**config)
        except Exception as exc:
            _logger.warning(f"解析 MinecraftConfig 失败: {exc}; 使用默认配置")
            minecraft_cfg = MinecraftConfig()
        return MinecraftAgent(
            config=minecraft_cfg,
            llm_manager=llm_manager,
            prompt_manager=prompt_manager,
            event_bus=event_bus,
            tool_registry=tool_registry,
            thinking_sink=thinking_sink,
            task_tracker=task_tracker,
        )

    if name == "text_adv":
        from src.agents.text_adv import TextAdvConfig, TextAdvGameAgent
        from src.agents.text_adv.content_engine import StubContentEngine

        try:
            text_adv_cfg = TextAdvConfig(**config) if config else TextAdvConfig()
        except Exception as exc:
            _logger.warning(f"解析 TextAdvConfig 失败: {exc}; 使用默认配置")
            text_adv_cfg = TextAdvConfig()
        return TextAdvGameAgent(
            config=text_adv_cfg,
            content_engine=StubContentEngine(engine_kind="text_adv"),
            llm_manager=llm_manager,
            prompt_manager=prompt_manager,
            event_bus=event_bus,
        )

    return None


__all__ = ["SUPPORTED_AGENTS", "instantiate_agent"]
