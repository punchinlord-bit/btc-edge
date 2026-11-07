"""Out-of-band alerting via Telegram with strict dual-chat routing.

Routing rules (hard gates — no exceptions):
  - Admin DM (8462870321 by default): ALL routine traffic
      • 15-minute model evaluations
      • shadow-trading ledger updates
      • Ollama narrator / background diagnostic ticks
      • reconciler scorecards, nightly analyzer, health
  - Main group (-5462613076 by default): ONLY parsed DUNECALLS /
    high-conviction whale signals. Nothing else ever lands there.

Call sites that used to do ``alerter.send(text)`` for diagnostics keep working
and now land exclusively in the Admin DM. Explicit DUNECALLS go through
``send_dune_call`` (or ``send(..., to_group=True)`` after the content gate).
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

# Production defaults (override via env)
DEFAULT_ADMIN_CHAT_ID = "8462870321"
DEFAULT_GROUP_CHAT_ID = "-5462613076"

# Strict content gate: message must look like a real DUNECALLS signal
_DUNECALLS_RE = re.compile(
    r"(?i)^\s*DUNECALLS\b|DUNE\s*CALL|high[-_ ]conviction|whale\s+wager",
)


class TelegramAlerter:
    """Dual-target Telegram alerter.

    ``send`` / ``send_sync`` / ``send_audio`` always target the Admin DM.
    ``send_dune_call`` is the only public path that can reach the group, and
    only after the content gate passes.
    """

    def __init__(
        self,
        bot_token: str = "",
        admin_chat_id: str = "",
        group_chat_id: str = "",
        # Back-compat: single chat_id treated as admin
        chat_id: str = "",
    ) -> None:
        self.bot_token = (bot_token or "").strip()
        # Prefer explicit admin; fall back to legacy TELEGRAM_CHAT_ID; then default
        self.admin_chat_id = (
            (admin_chat_id or chat_id or DEFAULT_ADMIN_CHAT_ID).strip()
        )
        self.group_chat_id = (group_chat_id or DEFAULT_GROUP_CHAT_ID).strip()

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token) and bool(self.admin_chat_id)

    @property
    def group_enabled(self) -> bool:
        return bool(self.bot_token) and bool(self.group_chat_id)

    def _is_dune_call(self, text: str) -> bool:
        """Content gate — only real DUNECALLS / high-conviction signals pass."""
        if not text or not text.strip():
            return False
        return bool(_DUNECALLS_RE.search(text))

    async def _post(self, chat_id: str, text: str) -> bool:
        import httpx

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    url,
                    json={
                        "chat_id": chat_id,
                        "text": text[:4000],
                        "disable_web_page_preview": False,
                    },
                )
                resp.raise_for_status()
                return True
        except Exception as exc:  # noqa: BLE001 - alerting must never crash the engine
            logger.warning("Telegram alert failed (chat=%s): %s", chat_id, exc)
            return False

    async def send(self, text: str, *, to_group: bool = False) -> bool:
        """Send a message.

        Default (to_group=False): Admin DM only.
        to_group=True: only succeeds if the content gate passes (DUNECALLS);
        otherwise the message is forced to Admin DM and a warning is logged.
        """
        if not self.enabled:
            return False

        if to_group:
            if not self._is_dune_call(text):
                logger.warning(
                    "Blocked non-DUNECALLS message from group chat; "
                    "routing to Admin DM instead"
                )
                return await self._post(self.admin_chat_id, text)
            if not self.group_enabled:
                logger.warning("Group chat not configured; dropping DUNECALLS")
                return False
            return await self._post(self.group_chat_id, text)

        # Strict default: Admin DM only
        return await self._post(self.admin_chat_id, text)

    async def send_dune_call(self, text: str) -> bool:
        """Explicit path for live DUNECALLS / high-conviction whale signals.

        Only this method (or send(..., to_group=True) after the gate) is allowed
        to post into the main group. Everything else is forced to Admin DM.
        """
        if not self._is_dune_call(text):
            logger.warning(
                "send_dune_call rejected: message does not match DUNECALLS pattern"
            )
            # Still deliver to admin so the signal is not lost
            if self.enabled:
                await self._post(self.admin_chat_id, f"[REJECTED-GROUP]\n{text}")
            return False
        return await self.send(text, to_group=True)

    async def send_audio(self, audio_path: str, caption: str = "") -> bool:
        """Send an audio file to the Admin DM only (never the group)."""
        if not self.enabled:
            return False
        import httpx

        url = f"https://api.telegram.org/bot{self.bot_token}/sendAudio"
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                with open(audio_path, "rb") as fh:
                    data: dict[str, str] = {"chat_id": self.admin_chat_id}
                    if caption:
                        data["caption"] = caption
                    files = {
                        "audio": (os.path.basename(audio_path), fh, "audio/mpeg")
                    }
                    resp = await client.post(url, data=data, files=files)
                resp.raise_for_status()
                return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Telegram audio send failed: %s", exc)
            return False

    def send_sync(self, text: str, *, to_group: bool = False) -> bool:
        import asyncio

        try:
            return asyncio.run(self.send(text, to_group=to_group))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Telegram alert (sync) failed: %s", exc)
            return False


def build_alerter(settings=None) -> TelegramAlerter:
    """Factory used by the engine and dashboard.

    Env precedence:
      TELEGRAM_BOT_TOKEN          (required)
      TELEGRAM_ADMIN_CHAT_ID      (preferred for diagnostics)
      TELEGRAM_GROUP_CHAT_ID      (preferred for DUNECALLS)
      TELEGRAM_CHAT_ID            (legacy fallback → treated as admin)
    """
    return TelegramAlerter(
        bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        admin_chat_id=os.getenv("TELEGRAM_ADMIN_CHAT_ID", ""),
        group_chat_id=os.getenv("TELEGRAM_GROUP_CHAT_ID", ""),
        chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),  # legacy → admin
    )


# ---------------------------------------------------------------------------
# Production verification / dry-run test
# Run:  python -m btc_edge.execution.alerts
#   or: python src/btc_edge/execution/alerts.py
# ---------------------------------------------------------------------------

async def _run_routing_tests() -> None:
    """Simulate the two required cases and print results to console."""
    print("=" * 60)
    print("BTC-EDGE Telegram routing verification")
    print("=" * 60)

    # Capture what would be posted without hitting the network
    posted: list[tuple[str, str]] = []

    class _CaptureAlerter(TelegramAlerter):
        async def _post(self, chat_id: str, text: str) -> bool:
            posted.append((chat_id, text[:120]))
            print(f"  → would POST to chat_id={chat_id!r}: {text[:80]!r}...")
            return True

    alerter = _CaptureAlerter(
        bot_token="dummy-token",
        admin_chat_id=DEFAULT_ADMIN_CHAT_ID,
        group_chat_id=DEFAULT_GROUP_CHAT_ID,
    )

    # --- Case 1: routine 15-minute Ollama diagnostic tick ---
    print("\n[1] Routine 15-minute Ollama diagnostic tick")
    posted.clear()
    diagnostic = (
        "Ollama 7B gate heartbeat | model=qwen2.5-coder:7b | state=ready | "
        "last_verdict=HOLD | latency=812ms | num_verdicts=14"
    )
    ok = await alerter.send(diagnostic)
    assert ok, "diagnostic send should succeed"
    assert len(posted) == 1, "exactly one destination"
    assert posted[0][0] == DEFAULT_ADMIN_CHAT_ID, "must land in Admin DM"
    assert posted[0][0] != DEFAULT_GROUP_CHAT_ID, "must NOT land in group"
    print("  PASS — diagnostic routed exclusively to Admin DM")

    # --- Case 2: live DUNECALLS signal ---
    print("\n[2] Live DUNECALLS signal (with partner Kalshi link)")
    posted.clear()
    dune_msg = (
        "DUNECALLS\n"
        "BTC 15m UP @ 62¢ | model 0.91 | edge +4.8%\n"
        "Kalshi: https://kalshi.com/markets/kxbtc15m\n"
        "Size: 12 contracts | conviction: HIGH"
    )
    ok = await alerter.send_dune_call(dune_msg)
    assert ok, "DUNECALLS send should succeed"
    assert len(posted) == 1, "exactly one destination"
    assert posted[0][0] == DEFAULT_GROUP_CHAT_ID, "must land in main group"
    print("  PASS — DUNECALLS formatted and dispatched to group")

    # --- Case 3: content gate blocks non-DUNECALLS from group ---
    print("\n[3] Content gate: non-DUNECALLS forced away from group")
    posted.clear()
    ok = await alerter.send("shadow ledger update: +0.00 PnL", to_group=True)
    assert ok
    assert posted[0][0] == DEFAULT_ADMIN_CHAT_ID, "gate must redirect to admin"
    print("  PASS — non-DUNECALLS blocked from group, redirected to Admin DM")

    print("\n" + "=" * 60)
    print("ALL ROUTING CHECKS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    import asyncio
    asyncio.run(_run_routing_tests())
