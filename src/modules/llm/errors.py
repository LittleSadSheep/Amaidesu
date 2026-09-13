"""LLM 错误分类（占位）。

本模块预定承载按错误类型的分类体系（Retryable / Fatal / Timeout /
Interrupted）：Client 产出分类、Engine 依分类决策重试或切换。
分类定义在重构的后续任务填充；当前各层仍以异常/错误字符串传递。
"""
