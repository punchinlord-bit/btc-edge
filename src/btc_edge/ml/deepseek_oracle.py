"""DeepSeek oracle — an independent LLM "voter" for the prediction ensemble.

Adds a non-correlated signal to the bot's probability estimate: every
``refresh_interval`` seconds the engine sends DeepSeek a compact snapshot of live
features (price action, window delta, book, micro-acceleration, news, regime) and
asks it for a single ``p_up`` (probability BTC settles UP this 15m window).

The result is cached and blended into the ensemble's ``prob_estimate`` at a small
weight, so the LLM *informs* the model without ever dominating it. When no API key
is configured, or the call fails/times out, it degrades silently to ``None`` and the
model runs unchanged.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a quantitative Bitcoin short-horizon (15-minute) prediction oracle. "
    "Given a compact snapshot of live market data, estimate the probability that "
    "BTC settles UP (close price >= open price) at the end of the current 15-minute "
    "window. Reason about momentum vs mean-reversion, the order book, and news tone. "
    "Respond with ONLY a single JSON object: {\"p_up\": <0..1>, \"confidence\": <0..1>, "
    "\"reason\": \"<one short sentence>\"}. No markdown, no extra text."
)


def _parse_p_up(text: str) -> float | None:
    """Parse the LLM reply into a 0..1 probability (tolerant of stray text)."""
    if not text:
        return None
    try:
        data = json.loads(text.strip())
        p = data.get("p_up")
        if isinstance(p, (int, float)):
            return float(max(0.0, min(1.0, p)))
    except (json.JSONDecodeError, AttributeError):
        pass
    import re

    m = re.search(r'"p_up"\s*:\s*([0-9]*\.?[0-9]+)', text)
    if m:
        try:
            return float(max(0.0, min(1.0, float(m.group(1)))))
        except ValueError:
            return None
    return None


class DeepSeekOracle:
    """Cached, best-effort DeepSeek client producing a single ``p_up`` estimate."""

    def __init__(
        self,
        base_url: str = "https://api.deepseek.com/v1",
        api_key: str = "",
        model: str = "deepseek-chat",
        timeout: float = 20.0,
        weight: float = 0.15,
        ttl_seconds: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.weight = weight
        self.ttl_seconds = ttl_seconds
        self._latest: float | None = None
        self._latest_confidence: float | None = None
        self._reason: str = ""
        self._last_ts: float = 0.0
        self._error_count: int = 0

    @property
    def configured(self) -> bool:
        return bool(self.api_key) and bool(self.base_url)

    @property
    def latest(self) -> float | None:
        return self._latest

    @property
    def latest_confidence(self) -> float | None:
        return self._latest_confidence

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def error_count(self) -> int:
        return self._error_count

    def stale(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return (now - self._last_ts) >= self.ttl_seconds

    @staticmethod
    def build_prompt(snapshot: dict[str, Any]) -> str:
        """Render a compact live snapshot into the LLM prompt."""
        keys = (
            "window_delta_pct", "option_implied_prob", "micro_acceleration",
            "book_imbalance", "news_sentiment", "time_remaining", "regime",
            "realized_vol", "implied_vol", "contract_price", "best_bid", "best_ask",
            "btc_price", "window_open_price", "recent_prices",
        )
        subset = {k: snapshot.get(k) for k in keys}
        return (
            "Live BTC 15m snapshot (JSON):\n"
            + json.dumps(subset, default=str)
            + "\n\nEstimate p_up (probability the 15m window settles UP)."
        )

    async def query(self, snapshot: dict[str, Any]) -> float | None:
        """Call DeepSeek and cache the parsed ``p_up``; returns None on failure."""
        if not self.configured:
            return None
        import httpx

        prompt = self.build_prompt(snapshot)
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/chat/completions", json=payload, headers=headers
                )
                resp.raise_for_status()
                data = resp.json()
                text = data["choices"][0]["message"]["content"].strip()
        except Exception as exc:  # noqa: BLE001 - degrade gracefully
            self._error_count += 1
            logger.warning("DeepSeek oracle query failed: %s", exc)
            return None

        p = _parse_p_up(text)
        if p is None:
            self._error_count += 1
            logger.warning("DeepSeek oracle returned unparseable reply: %r", text[:200])
            return None
        self._latest = p
        self._last_ts = time.time()
        self._error_count = 0
        try:
            data = json.loads(text.strip())
            self._latest_confidence = float(data.get("confidence", 0.5))
            self._reason = str(data.get("reason", ""))
        except (json.JSONDecodeError, AttributeError):
            self._latest_confidence = 0.5
            self._reason = text
        return p


def build_oracle(settings) -> DeepSeekOracle:
    """Build the oracle from env + config.

    Enabled whenever ``AI_API_KEY`` is present (independent of the dashboard's
    ``ai.enabled`` toggle, which only governs the read-only assistant). With no key
    it returns a disabled oracle whose ``query`` is a no-op.
    """
    import os

    return DeepSeekOracle(
        base_url=os.getenv("AI_BASE_URL", settings.ai.base_url),
        api_key=os.getenv("AI_API_KEY", ""),
        model=os.getenv("AI_MODEL", settings.ai.model),
    )

