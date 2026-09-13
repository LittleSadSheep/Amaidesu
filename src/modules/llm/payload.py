"""LLM 中立数据契约（占位）。

本模块预定承载 Engine 与 Client 之间的厂商无关数据模型
（Message / ToolSpec / ToolCall / Usage / Response）；契约定义在
重构的下一波任务填充。当前共享响应类型 ``LLMResponse`` 暂居
``client.py``。
"""
