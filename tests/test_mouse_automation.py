"""Tests for the mouse automation layer (using a fake pyautogui backend)."""

from __future__ import annotations

from btc_edge.desktop import mouse_automation as ma
from btc_edge.desktop.mouse_automation import (
    DashboardMouseActions,
    MouseAction,
    MouseAutomation,
)


class _FakeBox:
    def __init__(self, x: int, y: int) -> None:
        self.x = x
        self.y = y


class _FakePyAutoGUI:
    ImageNotFoundException = type("ImageNotFoundException", (Exception,), {})

    def __init__(self) -> None:
        self.moves: list[tuple[int, int]] = []
        self.clicks = 0
        self.double_clicks = 0
        self.scrolls: list[int] = []

    def moveTo(self, x: int, y: int, duration: float = 0.25) -> None:
        self.moves.append((x, y))

    def click(self) -> None:
        self.clicks += 1

    def doubleClick(self) -> None:
        self.double_clicks += 1

    def scroll(self, lines: int) -> None:
        self.scrolls.append(lines)

    def locateCenterOnScreen(self, image_path: str, confidence: float = 0.9) -> _FakeBox | None:
        return _FakeBox(15, 25)


def _make_automation(safe_zone=None) -> tuple[MouseAutomation, _FakePyAutoGUI]:
    fake = _FakePyAutoGUI()
    ma.pyautogui = fake  # inject fake backend
    return MouseAutomation(safe_zone=safe_zone), fake


def test_move_within_safe_zone():
    auto, fake = _make_automation(safe_zone=(0, 0, 100, 100))
    assert auto.move_to(50, 50) is True
    assert fake.moves == [(50, 50)]
    assert auto.last_position() == (50, 50)


def test_move_outside_safe_zone_blocked():
    auto, fake = _make_automation(safe_zone=(0, 0, 100, 100))
    assert auto.move_to(200, 200) is False
    assert fake.moves == []


def test_click_within_zone():
    auto, fake = _make_automation(safe_zone=(0, 0, 100, 100))
    assert auto.click_at(10, 10) is True
    assert fake.clicks == 1


def test_scroll_records_lines():
    auto, fake = _make_automation()
    assert auto.scroll_at(5, 5, 3) is True
    assert fake.scrolls == [3]


def test_execute_double_click_action():
    auto, fake = _make_automation()
    action = MouseAction(x=10, y=10, double_click=True)
    assert auto.execute(action) is True
    assert fake.double_clicks == 1


def test_execute_confirm_outside_zone_blocked():
    auto, fake = _make_automation(safe_zone=(0, 0, 50, 50))
    action = MouseAction(x=100, y=100, click=True, confirm=True)
    assert auto.execute(action) is False
    assert fake.clicks == 0


def test_dashboard_actions_delegate():
    auto, fake = _make_automation()
    dash = DashboardMouseActions(auto)
    assert dash.halt(20, 30) is True
    assert dash.resume(40, 50) is True
    assert fake.clicks == 2


def test_locate_center_returns_center():
    ma.pyautogui = _FakePyAutoGUI()
    assert ma.locate_center("assets/halt.png") == (15, 25)
