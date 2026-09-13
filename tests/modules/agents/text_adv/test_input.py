"""键鼠注入后端测试——只测假件与配置断言，不真点键鼠。"""

from __future__ import annotations

import pyautogui

from src.agents.text_adv.input import (
    FakeInputBackend,
    PyAutoGuiInputBackend,
)


class TestFakeInputBackend:
    def test_hold_records_down_then_up(self) -> None:
        backend = FakeInputBackend()
        backend.hold("ctrl", 500)
        assert backend.calls == ["keyDown:ctrl", "keyUp:ctrl"]

    def test_press_records_call(self) -> None:
        backend = FakeInputBackend()
        backend.press("space")
        assert backend.calls == ["press:space"]

    def test_click_records_with_default_button(self) -> None:
        backend = FakeInputBackend()
        backend.click(100, 200)
        assert backend.calls == ["click:100,200,left"]

    def test_click_records_custom_button(self) -> None:
        backend = FakeInputBackend()
        backend.click(100, 200, button="right")
        assert backend.calls == ["click:100,200,right"]

    def test_sequence_order_preserved(self) -> None:
        backend = FakeInputBackend()
        backend.press("space")
        backend.hold("ctrl", 100)
        backend.click(1, 2)
        assert backend.calls == [
            "press:space",
            "keyDown:ctrl",
            "keyUp:ctrl",
            "click:1,2,left",
        ]


class TestPyAutoGuiInputBackendConfig:
    def test_init_disables_failsafe_and_pause(self) -> None:
        PyAutoGuiInputBackend()
        assert pyautogui.FAILSAFE is False
        assert pyautogui.PAUSE == 0
