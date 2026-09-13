"""未知名 Collector 跳过 + warning 行为测试

覆盖：
- instantiate_collector 遇未知名：返回 None、记录 warning、不抛异常
- SUPPORTED_COLLECTORS 已不包含已退役的注册名（screen）
- 已知名仍正常实例化（冒烟）
"""

from __future__ import annotations

from typing import List, Optional

import pytest
from loguru import logger as _loguru_logger

from src.modules.collectors.factory import SUPPORTED_COLLECTORS, instantiate_collector


class _LoguruCapture:
    """内存里捕获 loguru 日志记录（项目用 loguru，pytest caplog 不适用）。

    每个测试用 fixture 实例化一次；测试结束后清理 sink，避免污染其它用例。
    """

    def __init__(self) -> None:
        self.records: List[dict] = []
        self._sink_id: Optional[int] = None

    def __enter__(self) -> "_LoguruCapture":
        def _sink(message) -> None:
            record = message.record
            self.records.append(
                {
                    "level": record["level"].name,
                    "message": record["message"],
                    "module": record["name"],
                }
            )

        self._sink_id = _loguru_logger.add(_sink, level="DEBUG")
        return self

    def __exit__(self, *exc_info) -> None:
        if self._sink_id is not None:
            _loguru_logger.remove(self._sink_id)
            self._sink_id = None


@pytest.fixture
def loguru_capture():
    """提供 _LoguruCapture 实例，自动管理 sink 生命周期。"""
    cap = _LoguruCapture()
    with cap:
        yield cap


def test_supported_collectors_does_not_include_screen() -> None:
    """退役采集器已从 SUPPORTED_COLLECTORS 中移除"""
    assert "screen" not in SUPPORTED_COLLECTORS
    # 已知有效名仍在册
    assert "console_input" in SUPPORTED_COLLECTORS
    assert "bili_danmaku" in SUPPORTED_COLLECTORS
    assert "bili_danmaku_official" in SUPPORTED_COLLECTORS
    assert "stt" in SUPPORTED_COLLECTORS


def test_instantiate_collector_unknown_returns_none_and_warns(loguru_capture) -> None:
    """遇未知名：返回 None、warning 日志、不抛异常"""
    result = instantiate_collector("nonexistent_collector_xyz")

    assert result is None
    # 警告包含原因上下文（采集器名 + 已退役提示）
    assert any(
        "nonexistent_collector_xyz" in record["message"] and "未知的 Collector" in record["message"]
        for record in loguru_capture.records
        if record["level"] == "WARNING"
    )


def test_instantiate_collector_legacy_screen_returns_none_and_warns(loguru_capture) -> None:
    """退役的 screen 名仍能被优雅跳过（残留段容忍）"""
    result = instantiate_collector("screen")

    assert result is None
    assert any(
        "screen" in record["message"] and "退役" in record["message"]
        for record in loguru_capture.records
        if record["level"] == "WARNING"
    )


def test_instantiate_collector_empty_string_returns_none_and_warns(loguru_capture) -> None:
    """空串视为未知名：返回 None + warning"""
    result = instantiate_collector("")

    assert result is None
    assert any(record["level"] == "WARNING" for record in loguru_capture.records)


def test_instantiate_collector_known_returns_instance(loguru_capture) -> None:
    """已知名仍正常实例化（冒烟，不触发 warning）"""
    instance = instantiate_collector("console_input")
    assert instance is not None
    assert instance.name == "console_input"
    # 不应有 WARNING 级别日志
    assert not any(record["level"] == "WARNING" for record in loguru_capture.records)
