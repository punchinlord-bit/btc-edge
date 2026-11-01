"""Seed venue ensembles from real, already-settled Kalshi markets.

The live ensembles start empty and otherwise learn only from future live
settlements (one sample per 15-minute window — very slow). To give Kalshi's
model a head start we fetch settled ``KXBTC15M`` markets and reconstruct the
real BTC move (``floor_strike`` -> ``expiration_value``) plus the up/down
``result`` for each window, then pre-train the venue's ensemble with those
``(features, outcome)`` pairs.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def settled_market_to_sample(market: dict[str, Any]) -> tuple[dict[str, float], float] | None:
    """Convert one settled Kalshi market into a ``(features, outcome)`` sample.

    ``floor_strike`` is the BTC reference (BRTI) at window open and
    ``expiration_value`` is the BRTI at settlement, so their relative move is the
    same ``window_delta_pct`` the live loop computes. ``result`` is ``yes`` (up
    won) or ``no`` (down won).
    """
    result = market.get("result")
    strike = _num(market.get("floor_strike"))
    expiry = _num(market.get("expiration_value"))
    if result not in ("yes", "no") or strike <= 0 or expiry <= 0:
        return None
    outcome = 1.0 if result == "yes" else 0.0
    delta_pct = (expiry - strike) / strike * 100.0
    # Neutral values for features we cannot reconstruct post-hoc; the only live
    # signal we recover is the realized BTC move, which is the dominant feature.
    features = {
        "window_delta_pct": delta_pct,
        "option_implied_prob": 0.5,
        "book_imbalance": 0.0,
        "micro_acceleration": 0.0,
        "news_sentiment": 0.0,
        "time_remaining": 0.0,
        "regime_dampening": 0.0,
        "regime_amplifying": 0.0,
        "contract_price": 0.5,
        "realized_vol": 0.0,
        "implied_vol": 0.0,
    }
    return features, outcome


def markets_to_samples(markets: list[dict[str, Any]]) -> list[tuple[dict[str, float], float]]:
    """Map settled markets to valid ``(features, outcome)`` seed samples."""
    samples: list[tuple[dict[str, float], float]] = []
    for m in markets:
        sample = settled_market_to_sample(m)
        if sample is not None:
            samples.append(sample)
    return samples


async def seed_ensemble_from_kalshi(ens: Any, client: Any, tf: int, limit: int = 200) -> int:
    """Fetch settled Kalshi markets and pre-train ``ens`` with them.

    Returns the number of samples seeded (0 when offline / no data).
    """
    try:
        markets = await client.get_settled_btc_markets(limit=limit)
    except Exception as exc:  # noqa: BLE001 - seeding is best-effort
        logger.warning("Kalshi history seed fetch failed: %s", exc)
        return 0

    samples = markets_to_samples(markets)
    if not samples:
        logger.info("No settled Kalshi markets available to seed the model.")
        return 0

    for features, outcome in samples:
        ens.update(features, outcome, tf)
    ens.retrain_lgbm()
    logger.info(
        "Seeded Kalshi ensemble with %d settled windows (%d up).",
        len(samples),
        sum(1 for _, o in samples if o >= 0.5),
    )
    return len(samples)
