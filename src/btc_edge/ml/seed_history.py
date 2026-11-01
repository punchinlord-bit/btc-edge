"""Seed venue ensembles from real, already-settled Kalshi markets.

The live ensembles start empty and otherwise learn only from future live
settlements (one sample per 15-minute window — very slow). To give Kalshi's
model a head start we fetch settled ``KXBTC15M`` markets and reconstruct the
real BTC move (``floor_strike`` -> ``expiration_value``) plus the up/down
``result`` for each window, then pre-train the venue's ensemble with those
``(features, outcome)`` pairs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
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


async def seed_ensemble_from_polymarket(
    ens: Any, gamma_base_url: str, tf: int, limit: int = 60
) -> int:
    """Warm-start the Polymarket ensemble from recently-resolved up/down markets.

    Polymarket has no cheap "list settled series" endpoint, so we walk back over
    the last ``limit`` deterministic 15m slugs and read each resolved market's
    outcome from the Gamma API. Only the up/down outcome (base rate) is seeded —
    Polymarket does not expose the reference BTC open/close that Kalshi does.
    """
    try:
        from btc_edge.connectors.poly_client import GammaClient, compute_window_ts, slug_for

        gamma = GammaClient(base_url=gamma_base_url)
        now = time.time()
        window_ts = compute_window_ts(now, tf)
        samples: list[tuple[dict[str, float], float]] = []
        for i in range(limit):
            past_ts = window_ts - i * tf
            slug = slug_for(tf, past_ts)
            try:
                events = await asyncio.to_thread(gamma.get_events, slug)
            except Exception:  # noqa: BLE001 - skip unresolvable windows
                continue
            outcome = _resolved_outcome(events)
            if outcome is None:
                continue
            features = {
                "window_delta_pct": 0.0,
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
            samples.append((features, outcome))
    except Exception as exc:  # noqa: BLE001 - seeding is best-effort
        logger.warning("Polymarket history seed fetch failed: %s", exc)
        return 0

    if not samples:
        logger.info("No resolved Polymarket markets available to seed the model.")
        return 0

    for features, outcome in samples:
        ens.update(features, outcome, tf)
    ens.retrain_lgbm()
    logger.info(
        "Seeded Polymarket ensemble with %d resolved windows (%d up).",
        len(samples),
        sum(1 for _, o in samples if o >= 0.5),
    )
    return len(samples)


def _resolved_outcome(events: list[dict[str, Any]]) -> float | None:
    """Return 1.0 (up won) / 0.0 (down won) for a resolved Polymarket market.

    Gamma returns ``outcomes`` / ``outcomePrices`` / ``clobTokenIds`` as
    *stringified* JSON arrays (e.g. ``'["Up","Down"]'`` / ``'["0","1"]'``), so we
    ``json.loads`` them first. The winning leg has price ``"1"`` (>= 0.99) and the
    loser ``"0"`` once the market is closed.
    """
    for event in events:
        for market in event.get("markets", []):
            if not (market.get("closed") or market.get("resolved")):
                continue
            prices_raw = market.get("outcomePrices") or market.get("outcome_prices")
            if prices_raw is None:
                continue
            if isinstance(prices_raw, str):
                try:
                    prices = json.loads(prices_raw)
                except json.JSONDecodeError:
                    continue
            else:
                prices = prices_raw
            try:
                vals = [float(p) for p in prices]
            except (TypeError, ValueError):
                continue
            if len(vals) < 2:
                continue
            if vals[0] >= 0.99 and vals[1] <= 0.01:
                return 1.0
            if vals[1] >= 0.99 and vals[0] <= 0.01:
                return 0.0
    return None
