"""Event-driven fee-aware backtester.

Replays synthetic (or historical) windows, sampling decision points across each
window, building realistic market contexts (features + CLOB books), running all
strategies, simulating fills with exact fees/slippage/depth caps, and settling
contracts at resolution. Produces per-strategy statistics (PnL, win rate, net EV,
Brier score).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from btc_edge.backtest import fee_models
from btc_edge.features import indicators, regime
from btc_edge.features.regime import RegimeClassifier, RegimeInputs
from btc_edge.historical.bootstrapper import WindowRecord
from btc_edge.strategies.base import MarketContext, Strategy, StrategyDecision

# Sample decision points (seconds remaining) across each window.
SAMPLE_TIMES_REMAINING = [
    300.0, 270.0, 240.0, 210.0, 180.0, 150.0, 120.0, 105.0, 90.0,
    75.0, 60.0, 45.0, 30.0, 20.0, 15.0, 10.0, 5.0,
]

DEFAULT_IMPLIED_VOL = 1.0


@dataclass(slots=True)
class Fill:
    strategy: str
    window_ts: int
    venue: str
    side: str
    action: str
    price: float
    size: float
    fee: float
    is_maker: bool
    prob_estimate: float


@dataclass(slots=True)
class StrategyStats:
    strategy: str = ""
    fills: list[Fill] = field(default_factory=list)
    gross_pnl: float = 0.0
    total_fees: float = 0.0
    net_pnl: float = 0.0
    wins: int = 0
    losses: int = 0
    brier_sum: float = 0.0
    brier_count: int = 0

    @property
    def n_fills(self) -> int:
        return len(self.fills)

    @property
    def win_rate(self) -> float:
        total = self.wins + self.losses
        return self.wins / total if total else 0.0

    @property
    def net_ev(self) -> float:
        return self.net_pnl / self.n_fills if self.n_fills else 0.0

    @property
    def brier_score(self) -> float:
        return self.brier_sum / self.brier_count if self.brier_count else 0.0

    def record(self, pnl: float, fee: float, won: bool, prob: float, outcome: float) -> None:
        self.gross_pnl += pnl
        self.total_fees += fee
        self.net_pnl += pnl - fee
        if won:
            self.wins += 1
        else:
            self.losses += 1
        self.brier_sum += (prob - outcome) ** 2
        self.brier_count += 1


def _settle_payoff(side: str, outcome: float, size: float) -> float:
    """Settlement cash flow of holding ``size`` contracts of ``side``.

    ``outcome`` is the "up" contract settlement (1.0 win / 0.0 loss).
    """
    if side == "up":
        return outcome * size
    return (1.0 - outcome) * size


def _price_at(rec: WindowRecord, ts: float) -> float:
    """Linear-interpolate the synthetic price path at time ``ts``."""
    path = rec.price_path
    if not path:
        return rec.open_price
    if ts <= path[0][0]:
        return path[0][1]
    if ts >= path[-1][0]:
        return path[-1][1]
    for i in range(1, len(path)):
        t0, p0 = path[i - 1]
        t1, p1 = path[i]
        if t0 <= ts <= t1:
            if t1 == t0:
                return p1
            frac = (ts - t0) / (t1 - t0)
            return p0 + frac * (p1 - p0)
    return path[-1][1]


def build_context(
    rec: WindowRecord,
    time_remaining: float,
    implied_vol: float = DEFAULT_IMPLIED_VOL,
    market_noise: float = 0.0,
) -> MarketContext:
    """Construct a MarketContext at ``time_remaining`` seconds before expiry."""
    tf = rec.timeframe_seconds
    now_ts = rec.window_ts + tf - time_remaining
    spot = _price_at(rec, now_ts)

    window_delta = indicators.window_delta_pct(spot, rec.open_price)
    # The settlement is the mean of the final 60s of the path. Project the final
    # settlement by blending the already-elapsed (locked) samples with the live
    # spot for the remaining seconds, then price the digital option against that
    # projected reference (the "Locked-In Settlement Trajectory").
    projected = _projected_settlement(rec, now_ts, spot)
    opt_prob = indicators.option_implied_prob(
        spot=projected, strike=rec.open_price, sigma=implied_vol,
        time_remaining_seconds=time_remaining,
    )

    # Market mid = fair value + mean-reverting noise (creates arb opportunities).
    mid = min(0.99, max(0.01, opt_prob + market_noise))
    spread = 0.02
    # Tilt the synthetic book depth with the window-delta momentum so that
    # book_imbalance is informative for the confluence strategies.
    momentum_tilt = max(-1.0, min(1.0, window_delta * 2.0))
    bid_base = 50.0 * (1.0 + momentum_tilt)
    ask_base = 50.0 * (1.0 - momentum_tilt)
    bids = [(round(max(0.01, mid - spread / 2 - i * 0.01), 4), round(bid_base + i * 20, 2)) for i in range(3)]
    asks = [(round(min(0.99, mid + spread / 2 + i * 0.01), 4), round(ask_base + i * 20, 2)) for i in range(3)]

    bid_sizes = [b[1] for b in bids]
    ask_sizes = [a[1] for a in asks]
    imbalance = indicators.book_imbalance(bid_sizes, ask_sizes, levels=3)

    # Micro acceleration from recent 1m candle deltas plus aggressive-trade
    # divergence (scaled by the book imbalance direction).
    candle_deltas = _recent_candle_deltas(rec, now_ts, n=5)
    accel = indicators.micro_acceleration(
        candle_deltas, aggressive_trade_delta_divergence=imbalance * 0.5
    )

    # Realized vol from trailing path window.
    rv = _realized_vol_near(rec, now_ts, lookback=60)
    rv_iv = rv / implied_vol if implied_vol > 0 else 0.0
    cls = RegimeClassifier().classify(RegimeInputs(rv_iv_ratio=rv_iv))

    return MarketContext(
        venue=rec.venue,
        timeframe_seconds=tf,
        window_ts=rec.window_ts,
        window_open_price=rec.open_price,
        current_spot=spot,
        time_remaining=time_remaining,
        contract_price=mid,
        prob_estimate=opt_prob,
        window_delta_pct=window_delta,
        option_implied_prob=opt_prob,
        book_imbalance=imbalance,
        micro_acceleration=accel,
        regime=cls.value,
        bids=bids,
        asks=asks,
        realized_vol=rv,
        implied_vol=implied_vol,
    )


def _recent_candle_deltas(rec: WindowRecord, now_ts: float, n: int = 5) -> list[float]:
    candles = [c for c in rec.candles_1m if c["ts"] <= now_ts]
    closes = [c["close"] for c in candles[-n - 1:]]
    deltas = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0:
            deltas.append((closes[i] - closes[i - 1]) / closes[i - 1])
    return deltas


def _realized_vol_near(rec: WindowRecord, now_ts: float, lookback: float = 60.0) -> float:
    pts = [p for t, p in rec.price_path if now_ts - lookback <= t <= now_ts]
    if len(pts) < 2:
        return 0.0
    return regime.realized_vol_from_prices(pts, sample_hz=1.0)


def _projected_settlement(
    rec: WindowRecord, now_ts: float, spot: float, settle_seconds: float = 60.0
) -> float:
    """Blend the locked settlement samples with the live spot to estimate the
    final 60-second settlement average."""
    settle_start = rec.window_ts + rec.timeframe_seconds - settle_seconds
    settle_end = rec.window_ts + rec.timeframe_seconds
    if now_ts < settle_start:
        return spot
    locked = [p for t, p in rec.price_path if settle_start <= t <= now_ts]
    if not locked:
        return spot
    locked_avg = sum(locked) / len(locked)
    elapsed = max(0.0, now_ts - settle_start)
    remaining = max(0.0, settle_end - now_ts)
    return (locked_avg * elapsed + spot * remaining) / settle_seconds


class Simulator:
    """Runs strategies over a list of windows and settles fills."""

    def __init__(
        self,
        strategies: list[Strategy],
        sample_times: list[float] | None = None,
        implied_vol: float = DEFAULT_IMPLIED_VOL,
        market_noise_sigma: float = 0.03,
        seed: int = 7,
    ) -> None:
        self.strategies = strategies
        self.sample_times = sample_times if sample_times is not None else SAMPLE_TIMES_REMAINING
        self.implied_vol = implied_vol
        self.market_noise_sigma = market_noise_sigma
        self._rng = __import__("random").Random(seed)

    def run(self, windows: list[WindowRecord]) -> dict[str, StrategyStats]:
        stats: dict[str, StrategyStats] = {s.name: StrategyStats(strategy=s.name) for s in self.strategies}

        for rec in windows:
            for time_remaining in self.sample_times:
                if time_remaining >= rec.timeframe_seconds:
                    continue
                noise = self._rng.gauss(0.0, self.market_noise_sigma)
                # Mean-revert noise toward zero so market tracks fair value.
                noise *= 0.8
                ctx = build_context(rec, time_remaining, implied_vol=self.implied_vol, market_noise=noise)
                for strategy in self.strategies:
                    decisions = strategy.evaluate(ctx)
                    for d in decisions:
                        self._execute(stats[strategy.name], rec, d)

        return stats

    def _execute(self, stat: StrategyStats, rec: WindowRecord, d: StrategyDecision) -> None:
        is_maker = d.order_type == "maker"
        fill_price = fee_models.effective_fill_price(d.price, is_maker)
        size = fee_models.apply_min_order(rec.venue, d.size)
        if size <= 0:
            return

        f = fee_models.fee(rec.venue, size, fill_price, is_maker)

        # Holding direction: buy -> +size, sell -> -size.
        signed_size = size if d.action == "buy" else -size
        payoff = _settle_payoff(d.side, rec.result, signed_size)
        cost = signed_size * fill_price
        pnl = payoff - cost

        won = (pnl - f) > 0
        stat.fills.append(Fill(
            strategy=stat.strategy,
            window_ts=rec.window_ts,
            venue=rec.venue,
            side=d.side,
            action=d.action,
            price=fill_price,
            size=size,
            fee=f,
            is_maker=is_maker,
            prob_estimate=d.prob_estimate,
        ))
        stat.record(pnl, f, won, d.prob_estimate, rec.result if d.side == "up" else (1.0 - rec.result))

