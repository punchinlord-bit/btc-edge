"""Safe operator actions for the desktop control panel.

Translates high-level intents (halt, resume, snapshot) into the engine's
in-memory state and its *persisted* kill switch. This module is deliberately
free of any desktop hijacking, mouse automation, or network scanning: the
desktop app is a control surface for the operator's *own* engine, not an
attack tool.

Safety model (paper-first):
- ``halt`` arms the persisted kill switch (``KillSwitch.arm``) so the trading
  loop — which reads the on-disk file every iteration — halts out-of-band.
- ``resume`` clears both the in-memory flag and the persisted file.
- Live routing is never enabled here; it remains gated behind
  ``ALLOW_LIVE_TRADING == "TRUE"`` AND ``trading.mode == "live"``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from btc_edge.execution.killswitch import KillSwitch
from btc_edge.web.app import EngineState


class OperatorActions:
    """Bound operator intents to the engine's shared state + persisted kill switch."""

    def __init__(self, state: EngineState, killswitch: KillSwitch) -> None:
        self._state = state
        self._killswitch = killswitch

    def halt(self) -> dict[str, Any]:
        self._state.kill_switch = True
        self._killswitch.arm()
        return {"kill_switch": True}

    def resume(self) -> dict[str, Any]:
        self._state.kill_switch = False
        self._killswitch.reset()
        return {"kill_switch": False}

    def snapshot(self) -> dict[str, Any]:
        return self._state.snapshot()

    def hotkeys(self) -> dict[str, Callable[[], None]]:
        """App-level safety hotkeys the GUI binds to keyboard shortcuts."""
        return {
            "Ctrl+Shift+K": self.halt,
            "Ctrl+Shift+R": self.resume,
        }
