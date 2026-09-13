"""OpenAI 兼容适配端。

一厂商一目录：``client.py`` 是协议适配实现，``compat.py`` 收纳 OpenAI
协议专属的转换工具（鉴权/URL 配置、tool_calls 形状规整）。
"""

from src.modules.llm.clients.openai.client import OpenAIClient

__all__ = ["OpenAIClient"]
