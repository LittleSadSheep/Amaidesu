"""LLM 观测层 - 用量记账的费用计算

单一费用口径：按 ``model.toml`` ``[[llm_models]]`` 的定价字段
（price_in / price_out，每百万 token）把一次调用的 token 消耗折算为费用。
引擎的 llm_usage 落库与请求历史记录共用本模块的计算结果，
保证两条账本的费用口径一致。

JSON 使用量账本（``TokenUsageManager``）的完整退役属后续任务；
本波仅把费用计算逻辑收拢到这里，原文件以薄委托引用。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

__all__ = ["calculate_cost", "get_model_price"]


def get_model_price(model_prices: Dict[str, Dict[str, float]], model_name: str) -> Optional[Dict[str, float]]:
    """按模型标识精确匹配价格配置（不做模糊匹配——误匹配会算错费用）

    Args:
        model_prices: 价格表（``{model_identifier: {price_in, price_out, ...}}``）
        model_name: 模型标识（[[llm_models]].model_identifier）

    Returns:
        价格配置字典，未登记则返回 None
    """
    return model_prices.get(model_name)


def calculate_cost(
    model_prices: Dict[str, Dict[str, float]],
    model_name: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> Dict[str, Any]:
    """计算 token 使用费用

    Args:
        model_prices: 价格表（``{model_identifier: {price_in, price_out, ...}}``）
        model_name: 模型名称
        prompt_tokens: 输入token数量
        completion_tokens: 输出token数量

    Returns:
        费用计算信息字典
    """
    price_config = get_model_price(model_prices, model_name)

    if not price_config:
        return {
            "has_price": False,
            "cost": 0.0,
            "cost_usd": 0.0,
            "price_in": 0.0,
            "price_out": 0.0,
            "message": f"模型 {model_name} 未找到价格配置",
        }

    # 价格单位：每1000000个token的价格（通常是美元）
    price_in = price_config.get("price_in", 0.0)
    price_out = price_config.get("price_out", 0.0)

    # 计算费用（转换为每token的价格）
    cost_in = (prompt_tokens / 1000000.0) * price_in
    cost_out = (completion_tokens / 1000000.0) * price_out
    total_cost = cost_in + cost_out

    return {
        "has_price": True,
        "cost": total_cost,
        "cost_usd": total_cost,  # 假设价格单位为美元
        "price_in": price_in,
        "price_out": price_out,
        "cost_in": cost_in,
        "cost_out": cost_out,
        "message": "费用计算成功",
    }
