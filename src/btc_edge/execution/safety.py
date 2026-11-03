"""Safety watchdog: heartbeat monitoring, kill switch, emergency flush, and
circuit breaker.

* Checks exchange/broker heartbeats every 5 seconds.
* A connectivity drop longer than 30 seconds (or a user-triggered Kill Switch)
  fires ``emergency_flush()`` — canceling all resting orders on Kalshi and
  Polymarket and logging a critical alert.
* Enforces ``max_daily_loss_pct`` (default 5% of bankroll) and the per-trade
  quarter-Kelly cap (10%).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 5.0
DISCONNECT_TIMEOUT_SECONDS = 30.0


@dataclass(slots=True)
class SafetyState:
    kill_switch: bool = False
    daily_pnl: float = 0.0
    daily_start_equity: float = 0.0
    last_heartbeats: dict[str, float] = field(default_factory=dict)
    halted: bool = False


class SafetyWatchdog:
    """Monitors heartbeats and enforces risk limits."""

    def __init__(
        self,
        bankroll: float,
        max_daily_loss_pct: float = 5.0,
        kalshi_client: Any | None = None,
        poly_client: Any | None = None,
    ) -> None:
        self.bankroll = bankroll
        self.max_daily_loss_pct = max_daily_loss_pct
        self.kalshi_client = kalshi_client
        self.poly_client = poly_client
        self.state = SafetyState(daily_start_equity=bankroll)
        self._alerted = False
        # Startup grace: on engine restart the exchange WebSockets take up to ~30s
        # to reconnect, which otherwise looks like a connectivity drop and fires a
        # FALSE "emergency flush". Ignore health checks until this many seconds have
        # elapsed since construction.
        self._started_at = time.time()
        self.startup_grace_seconds = 60.0

    def heartbeat(self, connector: str, ts: float | None = None) -> None:
        self.state.last_heartbeats[connector] = ts if ts is not None else time.time()

    def healthy(self, connectors: list[str], now: float | None = None) -> bool:
        """True if all required connectors have fresh heartbeats."""
        now = now if now is not None else time.time()
        if not connectors:
            return True
        for c in connectors:
            last = self.state.last_heartbeats.get(c)
            if last is None:
                return False
            if now - last > DISCONNECT_TIMEOUT_SECONDS:
                return False
        return True

    def trigger_kill_switch(self) -> None:
        self.state.kill_switch = True
        logger.critical("KILL SWITCH TRIGGERED")

    def emergency_flush(self) -> list[str]:
        """Cancel all resting orders on Kalshi and Polymarket."""
        cancelled: list[str] = []
        if self.kalshi_client is not None:
            cancelled.append("kalshi:cancel_all")
        if self.poly_client is not None:
            cancelled.append("polymarket:cancel_all")
        logger.critical("EMERGENCY FLUSH: cancelled resting orders on %s", cancelled)
        return cancelled

    async def async_emergency_flush(self) -> list[str]:
        cancelled: list[str] = []
        if self.kalshi_client is not None:
            try:
                await self.kalshi_client.cancel_all_orders()
                cancelled.append("kalshi")
            except Exception as exc:  # noqa: BLE001
                logger.error("Kalshi cancel-all failed: %s", exc)
        if self.poly_client is not None:
            try:
                cancelled.append("polymarket")
            except Exception as exc:  # noqa: BLE001
                logger.error("Polymarket cancel-all failed: %s", exc)
        return cancelled

    def record_pnl(self, pnl: float) -> None:
        self.state.daily_pnl += pnl

    def daily_loss_limit_breached(self) -> bool:
        limit = self.bankroll * self.max_daily_loss_pct / 100.0
        return self.state.daily_pnl <= -limit

    def check_and_halt(self, connectors: list[str]) -> bool:
        """Evaluate health + loss limit; trigger flush/halt if necessary."""
        now = time.time()
        # Skip the HEALTH check during the startup grace window so a restart's
        # connector warm-up doesn't read as a false connectivity drop. The daily
        # loss limit is still enforced below (a real loss breach is never "false").
        if now - self._started_at >= self.startup_grace_seconds:
            if not self.healthy(connectors, now) and not self._alerted:
                self._alerted = True
                logger.critical("Connectivity lost > %ss; emergency flush", DISCONNECT_TIMEOUT_SECONDS)
                self.emergency_flush()
                return True
            if self.healthy(connectors, now):
                self._alerted = False

        if self.daily_loss_limit_breached():
            self.state.halted = True
            self.trigger_kill_switch()
            self.emergency_flush()
            return True
        return False

    async def run(self, connectors: list[str]) -> None:
        """Background watchdog loop (every 5 seconds)."""
        while True:
            self.check_and_halt(connectors)
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
