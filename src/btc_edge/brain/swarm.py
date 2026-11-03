"""The 20-agent swarm "Brain": dynamic-shrinkage consensus with quorum gating.

Ports the sub-second swarm plan to Python/numpy. 20 deterministic agents split
into 4 archetypes (5 Tape Snipers, 5 Strike Pinners, 5 Mean Reverters, 5 Book
Watchers) stored as a flat weight matrix for vectorized dot-product inference.
Every tick we compute a Brier-weighted consensus P_up plus a quorum count and
dispersion IQR, and apply *dynamic* shrinkage (information surge) instead of a
static shrink toward the market mid.
"""

from __future__ import annotations

import numpy as np

FEATURES: tuple[str, ...] = (
    "micro_velocity_500ms",
    "micro_velocity_2s",
    "micro_acceleration",
    "realized_vol_10s",
    "realized_vol_60s",
    "strike_distance_zscore",
    "orderbook_imbalance",
    "spread_cents",
    "time_remaining_norm",
    "news_sentiment",
)
NUM_FEATURES = len(FEATURES)

ARCHETYPES: tuple[str, ...] = ("tape_sniper", "strike_pinner", "mean_reverter", "book_watcher")
AGENTS_PER_ARCHETYPE = 5
NUM_AGENTS = len(ARCHETYPES) * AGENTS_PER_ARCHETYPE

# Individualized learning rates, sorted low->high across the 20 agents.
_ALPHAS = np.linspace(0.002, 0.05, NUM_AGENTS)

# Information-surge bounds (how far a volatility breakout de-weights the market).
SURGE_FLOOR = 0.1
SURGE_CEIL = 0.85
DEFAULT_VOL_THRESHOLD = 1.0


def sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30.0, 30.0)))


def feature_vector(feats: dict) -> np.ndarray:
    """Map a feature dict to the fixed-order numeric vector (zero for missing)."""
    return np.asarray([float(feats.get(f, 0.0)) for f in FEATURES], dtype=float)


def _archetype_weights(archetype: str, rng: np.random.Generator) -> np.ndarray:
    w = np.zeros(NUM_FEATURES)
    idx = {f: i for i, f in enumerate(FEATURES)}

    def add(f: str, base: float) -> None:
        w[idx[f]] = base * rng.uniform(0.7, 1.3)

    if archetype == "tape_sniper":
        add("micro_velocity_500ms", 0.8)
        add("micro_acceleration", 0.9)
        add("micro_velocity_2s", 0.5)
    elif archetype == "strike_pinner":
        add("strike_distance_zscore", 1.0)
        add("time_remaining_norm", 0.3)
    elif archetype == "mean_reverter":
        add("strike_distance_zscore", -0.6)
        add("realized_vol_60s", 0.4)
    elif archetype == "book_watcher":
        add("orderbook_imbalance", 1.0)
        add("spread_cents", -0.4)
    return w


class Swarm:
    """20-agent ensemble with dynamic shrinkage, quorum and dispersion gating."""

    def __init__(self, seed: int = 0, vol_threshold: float = DEFAULT_VOL_THRESHOLD) -> None:
        self.vol_threshold = vol_threshold
        rng = np.random.default_rng(seed)
        self.archetypes: list[str] = []
        W_rows: list[np.ndarray] = []
        biases: list[float] = []
        for a in ARCHETYPES:
            for _ in range(AGENTS_PER_ARCHETYPE):
                self.archetypes.append(a)
                W_rows.append(_archetype_weights(a, rng))
                biases.append(float(rng.uniform(-0.1, 0.1)))
        self._W = np.vstack(W_rows)  # (20, NUM_FEATURES)
        self._bias = np.asarray(biases, dtype=float)
        self._alpha = _ALPHAS.copy()
        self.brier_ema = np.full(NUM_AGENTS, 0.25, dtype=float)  # coin-flip Brier start
        self.samples = np.zeros(NUM_AGENTS, dtype=int)

    def predict(self, feats: dict) -> np.ndarray:
        """Vectorized P_up for all 20 agents."""
        x = feature_vector(feats)
        z = self._W @ x + self._bias
        return sigmoid(z)

    def information_surge(self, feats: dict) -> float:
        """Clamped volatility-breakout signal in [SURGE_FLOOR, SURGE_CEIL]."""
        accel = abs(float(feats.get("micro_acceleration", 0.0)))
        return float(np.clip(accel / self.vol_threshold, SURGE_FLOOR, SURGE_CEIL))

    def shrinkage_weight(self, feats: dict) -> float:
        """Dynamic market weight: a breakout de-weights the market immediately.

        Combines the information surge with agent sample maturity so a 3-sigma
        micro_acceleration spike overrides market-mid shrinkage even when the
        swarm has few live samples.
        """
        surge = self.information_surge(feats)
        avg_samples = float(self.samples.mean())
        maturity = 20.0 / (20.0 + avg_samples)  # prior strength 20
        return (1.0 - surge) * maturity

    def consensus(
        self,
        feats: dict,
        market_mid: float,
        min_edge: float = 0.01,
        fee: float = 0.0,
    ) -> dict:
        """Brier-weighted trimmed consensus + quorum + dispersion."""
        p = self.predict(feats)  # (20,)
        mid = float(np.clip(market_mid, 0.0, 1.0))

        # Brier-weighted trimmed consensus: drop the 2 extremes on each side.
        order = np.argsort(p)
        trimmed = order[2 : NUM_AGENTS - 2]
        wts = 1.0 / (self.brier_ema[trimmed] + 1e-3)  # lower Brier => higher weight
        p_consensus = float(np.sum(p[trimmed] * wts) / np.sum(wts))

        # Dynamic shrinkage toward the market mid.
        shrink = self.shrinkage_weight(feats)
        p_consensus = (1.0 - shrink) * p_consensus + shrink * mid

        # Dispersion IQR over the full agent population.
        q25, q75 = np.percentile(p, [25, 75])
        dispersion_iqr = float(q75 - q25)

        # Net edge (consensus vs market mid, after fees).
        net_edge = p_consensus - mid - fee

        # Quorum: agents whose after-fee edge exceeds min_edge, split by direction
        # so an "up" entry and a "down" entry each gate on the right quorum.
        edge_after_fees = p - mid - fee
        quorum_up = int(np.sum(edge_after_fees >= min_edge))
        quorum_down = int(np.sum(edge_after_fees <= -min_edge))
        # Consensus-direction quorum (the side the trimmed consensus actually calls).
        quorum = quorum_up if p_consensus >= mid else quorum_down

        # Top archetype driving the signal (max mean edge).
        archetype_edge: dict[str, float] = {}
        for a in ARCHETYPES:
            mask = np.asarray([arch == a for arch in self.archetypes], dtype=bool)
            archetype_edge[a] = float(edge_after_fees[mask].mean())
        top_archetype = max(archetype_edge, key=archetype_edge.get)

        return {
            "consensus_prob": p_consensus,
            "market_mid": mid,
            "net_edge": net_edge,
            "quorum": quorum,
            "quorum_up": quorum_up,
            "quorum_down": quorum_down,
            "quorum_total": NUM_AGENTS,
            "dispersion_iqr": dispersion_iqr,
            "shrinkage": shrink,
            "top_archetype": top_archetype,
            "p_up": p.tolist(),
        }

    def update(self, feats: dict, outcome: float) -> None:
        """Online SGD (log-loss) + Brier EMA for every agent."""
        p = self.predict(feats)
        grad = p - outcome  # d(logloss)/dz
        x = feature_vector(feats)
        # Per-agent learning rate, vectorized over the weight matrix.
        self._W -= (self._alpha * grad)[:, None] * x[None, :]
        self._bias -= self._alpha * grad
        # Brier EMA.
        err = (p - outcome) ** 2
        self.brier_ema = 0.95 * self.brier_ema + 0.05 * err
        self.samples += 1

