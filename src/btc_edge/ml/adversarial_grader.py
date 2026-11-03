"""Parallel dual-AI adversarial grader (DeepSeek quant + OpenAI red-team).

Fired only after :class:`~btc_edge.features.time_decay_gate.PreGraderGate` passes.
DeepSeek and OpenAI are called in parallel (asyncio) with a hard 1600ms timeout
each. A trade is allowed only when BOTH approve; if OpenAI times out the trade may
still pass a stricter fallback (zScore >= 1.85 AND swarmConsensus >= 0.90). Each
provider rotates its keys via :class:`~btc_edge.ml.key_pool.KeyPoolManager`,
failing over on 429 / 5xx / timeout without crashing the pipeline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from btc_edge.ml.key_pool import KeyPoolManager

logger = logging.getLogger(__name__)

DEEPSEEK_SYSTEM = (
    "You are a ruthless quantitative execution engine grading a 15-minute BTC binary "
    "contract. Evaluate the exact JSON telemetry: elapsedSeconds, zScore, trueProb, "
    "marketAsk, evEdge, orderBookImbalance, and swarmConsensus. Approve (approve: true) "
    "ONLY if the mathematical edge is structurally supported by spot order flow and time "
    'decay. Output strict JSON: {"approve": boolean, "confidence": number, '
    '"quantReason": string}.'
)

OPENAI_SYSTEM = (
    "You are the Chief Risk Officer whose sole job is to protect bankroll survival and "
    "VETO bad trades. Assume this trade is a trap until proven otherwise. Inspect the "
    "telemetry for: 1) Whip-saw/mean-reversion exhaustion (BTC spiked too fast and is "
    "stalling), 2) Stale prediction market odds where spread/slippage destroys EV, 3) "
    "Insufficient time-decay cushion (zScore too low for remaining seconds). If ANY fatal "
    'flaw exists, set approve: false. Output strict JSON: {"approve": boolean, '
    '"riskScore": number, "vetoReason": string}.'
)

TIMEOUT_SECONDS = 1.6
OPENAI_TIMEOUT_FALLBACK_Z = 1.85
OPENAI_TIMEOUT_FALLBACK_QUORUM = 0.90


def _parse_json(text: str) -> dict[str, Any]:
    """Parse an LLM reply into a dict (tolerant of markdown fences / stray text)."""
    if not text:
        return {}
    stripped = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.DOTALL)
    if fence:
        stripped = fence.group(1)
    try:
        data = json.loads(stripped)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, ValueError):
        pass
    m = re.search(r"\{.*\}", stripped, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(0))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


@dataclass(slots=True)
class GraderDecision:
    approved: bool
    source: str  # "deepseek" | "openai"
    confidence: float = 0.0
    risk_score: float = 0.0
    reason: str = ""
    timed_out: bool = False


@dataclass(slots=True)
class GraderVerdict:
    approved: bool
    source: str  # "dual" | "fallback" | "veto"
    deepseek: GraderDecision | None = None
    openai: GraderDecision | None = None



class AdversarialGrader:
    """Two-LLM grader with key rotation, parallel execution and a strict veto rule."""

    def __init__(
        self,
        deepseek_pool: KeyPoolManager | None = None,
        openai_pool: KeyPoolManager | None = None,
        deepseek_base_url: str = "https://api.deepseek.com/v1",
        openai_base_url: str = "https://api.openai.com/v1",
        deepseek_model: str = "deepseek-chat",
        openai_model: str = "gpt-4o",
        timeout_seconds: float = TIMEOUT_SECONDS,
    ) -> None:
        self.deepseek_pool = deepseek_pool or KeyPoolManager()
        self.openai_pool = openai_pool or KeyPoolManager()
        self.deepseek_base_url = deepseek_base_url.rstrip("/")
        self.openai_base_url = openai_base_url.rstrip("/")
        self.deepseek_model = deepseek_model
        self.openai_model = openai_model
        self.timeout_seconds = timeout_seconds

    @property
    def configured(self) -> bool:
        return self.deepseek_pool.has_keys or self.openai_pool.has_keys

    @staticmethod
    def build_telemetry(
        *,
        elapsed_seconds: float,
        z_score: float,
        true_prob: float,
        market_ask: float,
        ev_edge: float,
        order_book_imbalance: float,
        swarm_consensus: float,
    ) -> dict[str, float]:
        return {
            "elapsedSeconds": float(elapsed_seconds),
            "zScore": float(z_score),
            "trueProb": float(true_prob),
            "marketAsk": float(market_ask),
            "evEdge": float(ev_edge),
            "orderBookImbalance": float(order_book_imbalance),
            "swarmConsensus": float(swarm_consensus),
        }

    async def grade(
        self, telemetry: dict[str, Any], *, z_score: float, swarm_consensus: float
    ) -> GraderVerdict:
        """Run both graders in parallel and apply the execution rule.

        DeepSeek-only mode: when the OpenAI pool has no keys, grade with DeepSeek
        alone (no red-team veto).
        """
        if not self.openai_pool.has_keys:
            deepseek_result = await self._grade_deepseek(telemetry)
            approved = deepseek_result is not None and deepseek_result.approved
            return GraderVerdict(
                approved, "deepseek" if approved else "veto", deepseek_result, None
            )

        deepseek_task = self._grade_deepseek(telemetry)
        openai_task = self._grade_openai(telemetry)
        deepseek_result, openai_result = await asyncio.gather(deepseek_task, openai_task)

        ds_ok = deepseek_result is not None and deepseek_result.approved
        oai_ok = openai_result is not None and openai_result.approved

        if ds_ok and oai_ok:
            return GraderVerdict(True, "dual", deepseek_result, openai_result)

        # OpenAI timed out (no verdict) -> stricter fallback gate.
        if ds_ok and openai_result is not None and openai_result.timed_out:
            if z_score >= OPENAI_TIMEOUT_FALLBACK_Z and swarm_consensus >= OPENAI_TIMEOUT_FALLBACK_QUORUM:
                return GraderVerdict(True, "fallback", deepseek_result, openai_result)
            return GraderVerdict(False, "veto", deepseek_result, openai_result)

        return GraderVerdict(False, "veto", deepseek_result, openai_result)

    async def _grade_deepseek(self, telemetry: dict[str, Any]) -> GraderDecision | None:
        return await self._grade_with_pool(
            pool=self.deepseek_pool,
            base_url=self.deepseek_base_url,
            model=self.deepseek_model,
            system=DEEPSEEK_SYSTEM,
            telemetry=telemetry,
            provider="deepseek",
        )

    async def _grade_openai(self, telemetry: dict[str, Any]) -> GraderDecision | None:
        return await self._grade_with_pool(
            pool=self.openai_pool,
            base_url=self.openai_base_url,
            model=self.openai_model,
            system=OPENAI_SYSTEM,
            telemetry=telemetry,
            provider="openai",
        )

    async def _grade_with_pool(
        self,
        *,
        pool: KeyPoolManager,
        base_url: str,
        model: str,
        system: str,
        telemetry: dict[str, Any],
        provider: str,
    ) -> GraderDecision | None:
        """Rotate keys, fail over on 429/5xx/timeout, parse the strict JSON verdict."""
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(telemetry)},
            ],
            "temperature": 0.0,
        }
        had_timeout = False
        attempts = max(1, len(pool.keys))
        for _ in range(attempts):
            key = pool.next_key()
            if key is None:
                break
            headers = {"Authorization": f"Bearer {key}"}
            try:
                async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                    resp = await client.post(
                        f"{base_url}/chat/completions", json=payload, headers=headers
                    )
                if resp.status_code == 429 or resp.status_code >= 500:
                    pool.mark_bad(key)
                    logger.warning("%s grader: %d -> key on cooldown", provider, resp.status_code)
                    continue
                resp.raise_for_status()
                data = resp.json()
                text = data["choices"][0]["message"]["content"].strip()
                verdict = _parse_json(text)
                if provider == "deepseek":
                    return GraderDecision(
                        approved=bool(verdict.get("approve", False)),
                        source="deepseek",
                        confidence=float(verdict.get("confidence", 0.0) or 0.0),
                        reason=str(verdict.get("quantReason", "")),
                    )
                return GraderDecision(
                    approved=bool(verdict.get("approve", False)),
                    source="openai",
                    risk_score=float(verdict.get("riskScore", 0.0) or 0.0),
                    reason=str(verdict.get("vetoReason", "")),
                )
            except (TimeoutError, httpx.TimeoutException):
                pool.mark_bad(key)
                had_timeout = True
                logger.warning("%s grader: timeout -> key on cooldown", provider)
                continue
            except Exception as exc:  # noqa: BLE001 - never crash the pipeline
                pool.mark_bad(key)
                logger.warning("%s grader error: %s", provider, exc)
                continue
        # All keys exhausted (rate-limited / 5xx / timeout / error).
        return GraderDecision(
            approved=False, source=provider, timed_out=had_timeout, reason="all keys failed"
        )


def build_adversarial_grader(
    deepseek_model: str = "deepseek-chat", openai_model: str = "gpt-4o"
) -> AdversarialGrader:
    """Build a grader from ``DEEPSEEK_*`` / ``OPENAI_*`` env keys.

    Falls back to the generic ``AI_API_KEY`` / ``AI_BASE_URL`` / ``AI_MODEL`` for the
    DeepSeek side, and leaves OpenAI empty when no ``OPENAI_*`` key is present (so the
    grader runs in DeepSeek-only mode).
    """
    import os

    ds_pool = KeyPoolManager.from_env("DEEPSEEK")
    if not ds_pool.has_keys:
        ai_key = os.getenv("AI_API_KEY", "")
        if ai_key:
            ds_pool = KeyPoolManager([ai_key])

    return AdversarialGrader(
        deepseek_pool=ds_pool,
        openai_pool=KeyPoolManager.from_env("OPENAI"),
        deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL")
        or os.getenv("AI_BASE_URL", "https://api.deepseek.com/v1"),
        openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        deepseek_model=os.getenv("DEEPSEEK_MODEL") or os.getenv("AI_MODEL", deepseek_model),
        openai_model=os.getenv("OPENAI_MODEL", openai_model),
    )

