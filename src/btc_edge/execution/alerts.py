"""Out-of-band alerting via Telegram.

Sends human-readable alerts when the bot goes down, is stuck, breaches a risk
limit, or needs attention. Uses the Telegram Bot API (``sendMessage``) over
HTTP; safe to call from anywhere including the safety watchdog.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class TelegramAlerter:
    def __init__(self, bot_token: str = "", chat_id: str = "") -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token) and bool(self.chat_id)

    async def send(self, text: str) -> bool:
        """Send a message; returns True on success, False otherwise."""
        if not self.enabled:
            return False
        import httpx

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json={"chat_id": self.chat_id, "text": text[:4000]})
                resp.raise_for_status()
                return True
        except Exception as exc:  # noqa: BLE001 - alerting must never crash the engine
            logger.warning("Telegram alert failed: %s", exc)
            return False

    def send_sync(self, text: str) -> bool:
        import asyncio

        try:
            return asyncio.run(self.send(text))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Telegram alert (sync) failed: %s", exc)
            return False


def build_alerter(settings) -> TelegramAlerter:
    import os

    return TelegramAlerter(
        bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
    )
