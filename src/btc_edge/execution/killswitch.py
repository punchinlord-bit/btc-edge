"""Persisted, out-of-band kill switch.

The kill-switch state lives in a small JSON file so it can be flipped by *any*
external process (a Telegram bot command, a cron job, a manual edit) and survives
restarts. The trading loop reads this on every iteration, so the halt is truly
independent of the main process's health.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


class KillSwitch:
    def __init__(self, path: str | Path = "data/kill_switch.json") -> None:
        self.path = Path(path)

    def is_armed(self) -> bool:
        if not self.path.exists():
            return False
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return bool(data.get("armed", False))
        except (json.JSONDecodeError, OSError):
            return False

    def arm(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"armed": True}), encoding="utf-8")

    def reset(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"armed": False}), encoding="utf-8")

    def remove(self) -> None:
        if self.path.exists():
            os.remove(self.path)
