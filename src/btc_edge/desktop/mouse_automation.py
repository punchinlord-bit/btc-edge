"""Precise mouse automation for the operator's own desktop.

This module translates high-level intents (click a button, hover a widget)
into mouse movements and clicks using PyAutoGUI. It is deliberately scoped to
the operator's own desktop — no autonomous "mouse hijacking" — and provides
safe, testable primitives for quick BTC movements when API access is blocked
or when interacting with legacy desktop applications.

Safety model:
- All movements are relative or absolute pixel coordinates (no screen scraping).
- A confirmation gate blocks actions outside a configured "safe zone."
- The module is free of any network scanning or remote execution logic.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

try:
    import pyautogui
except ImportError as _e:  # pragma: no cover - requires pyautogui
    pyautogui = None  # type: ignore[assignment]


@dataclass(slots=True)
class MouseAction:
    """A single mouse action with optional confirmation."""

    x: int
    y: int
    click: bool = False
    double_click: bool = False
    scroll: int = 0
    duration: float = 0.25
    confirm: bool = False


class MouseAutomation:
    """Safe mouse control for the operator's own desktop."""

    def __init__(self, safe_zone: tuple[int, int, int, int] | None = None) -> None:
        if pyautogui is None:
            raise RuntimeError(
                "PyAutoGUI is not installed. Install with: pip install pyautogui"
            )
        self.safe_zone = safe_zone
        self._last_move_ts = 0.0
        self._last_pos = (0, 0)

    def move_to(self, x: int, y: int, duration: float = 0.25) -> bool:
        if self._requires_confirmation(x, y):
            return False
        pyautogui.moveTo(x, y, duration=duration)
        self._last_move_ts = time.time()
        self._last_pos = (x, y)
        return True

    def click_at(self, x: int, y: int, duration: float = 0.25) -> bool:
        if not self.move_to(x, y, duration):
            return False
        pyautogui.click()
        return True

    def double_click_at(self, x: int, y: int, duration: float = 0.25) -> bool:
        if not self.move_to(x, y, duration):
            return False
        pyautogui.doubleClick()
        return True

    def scroll_at(self, x: int, y: int, lines: int, duration: float = 0.25) -> bool:
        if not self.move_to(x, y, duration):
            return False
        pyautogui.scroll(lines)
        return True

    def execute(self, action: MouseAction) -> bool:
        if action.confirm and not self._in_safe_zone(action.x, action.y):
            return False
        if action.double_click:
            return self.double_click_at(action.x, action.y, action.duration)
        if action.click:
            return self.click_at(action.x, action.y, action.duration)
        if action.scroll != 0:
            return self.scroll_at(action.x, action.y, action.scroll, action.duration)
        return self.move_to(action.x, action.y, action.duration)

    def _in_safe_zone(self, x: int, y: int) -> bool:
        if self.safe_zone is None:
            return True
        x1, y1, x2, y2 = self.safe_zone
        return x1 <= x <= x2 and y1 <= y <= y2

    def _requires_confirmation(self, x: int, y: int) -> bool:
        return not self._in_safe_zone(x, y)

    def last_position(self) -> tuple[int, int]:
        return self._last_pos

    def time_since_last_move(self) -> float:
        return time.time() - self._last_move_ts


class DashboardMouseActions:
    """Predefined coordinate-based actions for the BTC-Edge dashboard."""

    def __init__(self, automation: MouseAutomation) -> None:
        self._auto = automation

    def halt(self, x: int, y: int) -> bool:
        return self._auto.click_at(x, y)

    def resume(self, x: int, y: int) -> bool:
        return self._auto.click_at(x, y)

    def open_dashboard(self, x: int, y: int) -> bool:
        return self._auto.click_at(x, y)


def locate_center(image_path: str, confidence: float = 0.9) -> tuple[int, int] | None:
    """Find the center pixel of a UI element from a reference screenshot.

    Uses PyAutoGUI's template matching. ``image_path`` must point to a
    pre-captured PNG of the target element (store under
    ``src/btc_edge/desktop/assets/``). Returns ``(x, y)`` or ``None``.
    """
    if pyautogui is None:
        raise RuntimeError("PyAutoGUI is not installed")
    try:
        box = pyautogui.locateCenterOnScreen(image_path, confidence=confidence)
    except (pyautogui.ImageNotFoundException, OSError):
        return None
    return (int(box.x), int(box.y)) if box is not None else None
