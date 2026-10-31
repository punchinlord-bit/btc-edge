"""Read-only AI assistant for the dashboard.

Explains the engine's live state and summarizes news in plain English. It is
**read-only by design**: it has no methods that touch orders, the router, or the
exchanges, so it can never place or cancel a trade.

Two modes:
  * Live — any OpenAI-compatible chat-completions endpoint (OpenAI *and* DeepSeek
    both expose ``POST /v1/chat/completions``); selected via ``ai.base_url``,
    ``ai.model``, and the ``AI_API_KEY`` env var.
  * Offline — a deterministic plain-English renderer used when no key is set, so
    the assistant always returns something useful.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are the BTC-Edge trading assistant. Explain the bot's current state and "
    "what it means in plain, non-technical language. Be calm, concrete, and never "
    "give financial advice phrased as a guarantee. You are read-only: you cannot "
    "place trades. Keep answers under 250 words."
)


@dataclass(slots=True)
class AssistantReply:
    text: str
    backend: str  # "openai-compatible" | "offline"


class AIAssistant:
    """Chat-completions client with a deterministic offline fallback."""

    def __init__(
        self,
        base_url: str = "https://api.openai.com/v1",
        api_key: str = "",
        model: str = "gpt-4o-mini",
        timeout: float = 20.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.api_key) and bool(self.base_url)

    async def chat(self, user_prompt: str, system: str = SYSTEM_PROMPT) -> str:
        """Send a chat request; raises on transport/HTTP error."""
        import httpx

        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.3,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()

    def offline_explain(self, snapshot: dict[str, Any]) -> str:
        mode = snapshot.get("mode", "paper")
        killed = snapshot.get("kill_switch", False)
        regime = snapshot.get("regime", "UNKNOWN")
        sentiment = snapshot.get("news_sentiment", 0.0)
        risk = snapshot.get("risk", {})

        lines = []
        if killed:
            lines.append("⚠️ The emergency kill switch is ON. Trading is halted until you reset it.")
        else:
            lines.append(f"Engine is running in {mode.upper()} mode. Trading is "
                         f"{'live' if mode == 'live' else 'simulated (paper)'}.")

        regime_txt = {
            "DAMPENING": "The market is calm and mean-reverting (pinning).",
            "AMPLIFYING": "The market is trending hard or in a cascade — higher risk.",
        }.get(regime, "Market regime is unknown.")
        lines.append(regime_txt)

        if sentiment > 0.15:
            lines.append("News is leaning bullish right now.")
        elif sentiment < -0.15:
            lines.append("News is leaning bearish right now.")
        else:
            lines.append("News is mixed or neutral.")

        if risk:
            at_risk = risk.get("at_risk_usd", 0.0)
            limit = risk.get("daily_loss_limit_usd", 0.0)
            lines.append(f"Currently ${at_risk:,.2f} is at risk, out of a "
                         f"${limit:,.2f} daily loss allowance.")

        signals = snapshot.get("signals", [])
        if signals:
            lines.append("Recent signals: " + "; ".join(signals[:4]))
        else:
            lines.append("No entry signal is active at the moment — waiting for a setup.")

        return "\n".join(lines)

    async def explain(self, snapshot: dict[str, Any]) -> AssistantReply:
        if not self.configured:
            return AssistantReply(text=self.offline_explain(snapshot), backend="offline")
        prompt = (
            "Here is the live engine state as JSON. Explain it in plain English "
            "and say whether the bot is currently inclined to add or reduce risk:\n\n"
            + json.dumps(snapshot, default=str)
        )
        try:
            text = await self.chat(prompt)
            return AssistantReply(text=text, backend="openai-compatible")
        except Exception as exc:  # noqa: BLE001 - degrade gracefully to offline
            logger.warning("AI assistant request failed (%s); using offline renderer", exc)
            return AssistantReply(text=self.offline_explain(snapshot), backend="offline")

    async def summarize_news(self, items: list[dict[str, Any]]) -> str:
        if not items:
            return "No recent Bitcoin headlines."
        if not self.configured:
            top = [i.get("title", "") for i in items[:5] if i.get("title")]
            return "Top Bitcoin headlines:\n- " + "\n- ".join(top)
        prompt = (
            "Summarize these Bitcoin headlines in 3-5 plain-English bullets and "
            "state the overall market tone:\n\n" + json.dumps(items[:15], default=str)
        )
        try:
            return await self.chat(prompt)
        except Exception as exc:  # noqa: BLE001
            logger.warning("News summary failed (%s)", exc)
            return "AI news summary is unavailable right now."


def build_assistant(settings) -> AIAssistant:
    import os

    return AIAssistant(
        base_url=settings.ai.base_url,
        api_key=os.getenv("AI_API_KEY", ""),
        model=settings.ai.model,
    )
