"""兼容薄壳 - 旧 ``src.modules.llm.manager`` 导入路径的符号重导出。

LLM 模块按 engine / bootstrap / clients 拆分后，本文件仅保留旧路径
的兼容重导出，供尚未迁移的消费方（simulator 等）与既有测试使用；
新代码应从符号所属模块直接导入。本薄壳的退役属后续清理任务。
"""

from __future__ import annotations

from src.modules.llm.bootstrap import ClientType, ProfileNames, _ResolvedModel, _ResolvedProfile
from src.modules.llm.client import LLMResponse
from src.modules.llm.clients.openai.compat import normalize_tool_calls_for_protocol
from src.modules.llm.engine import LLMManager, RetryConfig

__all__ = [
    "LLMManager",
    "LLMResponse",
    "RetryConfig",
    "ProfileNames",
    "ClientType",
    "_ResolvedModel",
    "_ResolvedProfile",
    "normalize_tool_calls_for_protocol",
]
