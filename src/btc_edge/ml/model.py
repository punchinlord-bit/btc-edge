"""Online probability ensemble: Logistic Regression + LightGBM.

The ensemble combines a streaming SGD logistic regression (updated on every
settlement) with a LightGBM gradient-boosting classifier (retrained on the
accumulated history), producing the ``prob_estimate`` consumed by strategies.
When insufficient training data exists, it gracefully falls back to the
option-implied probability.
"""

from __future__ import annotations

from typing import Any

import numpy as np

FEATURE_ORDER = [
    "window_delta_pct",
    "option_implied_prob",
    "book_imbalance",
    "micro_acceleration",
    "news_sentiment",
    "time_remaining_norm",
    "regime_dampening",
    "regime_amplifying",
    "contract_price",
    "realized_vol_norm",
    "implied_vol_norm",
]


def feature_vector(features: dict[str, float], timeframe_seconds: int = 300) -> np.ndarray:
    """Map a feature dict to the fixed-order numeric vector."""
    vec = []
    for k in FEATURE_ORDER:
        if k == "time_remaining_norm":
            tr = float(features.get("time_remaining", 0.0))
            vec.append(tr / max(timeframe_seconds, 1.0))
        elif k == "realized_vol_norm":
            vec.append(float(features.get("realized_vol", 0.0)))
        elif k == "implied_vol_norm":
            vec.append(float(features.get("implied_vol", 0.0)))
        else:
            vec.append(float(features.get(k, 0.0)))
    return np.asarray(vec, dtype=float)


def feature_dict(features: dict[str, float], timeframe_seconds: int = 300) -> dict[str, float]:
    """Map raw features to a ``{FEATURE_ORDER[i]: normalized_value}`` dict.

    This is the normalized representation the promotion gate consumes: a logistic
    ``config`` keyed by the same names can be dotted against it directly.
    """
    vec = feature_vector(features, timeframe_seconds)
    return {name: float(vec[i]) for i, name in enumerate(FEATURE_ORDER)}


class OnlineLogisticRegression:
    """Streaming logistic regression via SGD (log-loss)."""

    def __init__(self, learning_rate: float = 0.01) -> None:
        from sklearn.linear_model import SGDClassifier

        self.model = SGDClassifier(
            loss="log_loss",
            learning_rate="constant",
            eta0=learning_rate,
            random_state=0,
        )
        self.fitted = False

    def partial_fit(self, X: np.ndarray, y: np.ndarray) -> None:
        if X.shape[0] == 0:
            return
        classes = np.array([0, 1])
        self.model.partial_fit(X, y, classes=classes)
        self.fitted = True

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        return self.model.predict_proba(X)[:, 1]


class LightGBMEnsemble:
    """LightGBM classifier retrained on accumulated (feature, outcome) pairs."""

    def __init__(self, num_leaves: int = 31, learning_rate: float = 0.05) -> None:
        self.num_leaves = num_leaves
        self.learning_rate = learning_rate
        self._booster: Any = None
        self._n_samples = 0

    def fit(self, X: np.ndarray, y: np.ndarray) -> None:
        import lightgbm as lgb

        if X.shape[0] < 2 or len(np.unique(y)) < 2:
            return
        dset = lgb.Dataset(X, label=y)
        params = {
            "objective": "binary",
            "metric": "binary_logloss",
            "num_leaves": self.num_leaves,
            "learning_rate": self.learning_rate,
            "verbose": -1,
        }
        self._booster = lgb.train(params, dset, num_boost_round=50)
        self._n_samples = X.shape[0]

    def predict(self, X: np.ndarray) -> np.ndarray:
        if self._booster is None:
            return np.full(X.shape[0], 0.5)
        return self._booster.predict(X)

    @property
    def trained(self) -> bool:
        return self._booster is not None


class ProbabilityEnsemble:
    """Combined logistic + LightGBM probability estimator.

    Produces a raw ``prob_estimate`` and an *adaptive* market calibration: the
    raw estimate is shrunk toward the market's implied probability by a weight
    derived from the model's Brier score versus the market's Brier score, so an
    overconfident/unskilled model defers to the market while a genuinely
    predictive model keeps more weight.
    """

    def __init__(
        self,
        lgbm_weight: float = 0.4,
        min_shrink: float = 0.3,
        max_shrink: float = 0.6,
    ) -> None:
        self.logreg = OnlineLogisticRegression()
        self.lgbm = LightGBMEnsemble()
        self.lgbm_weight = lgbm_weight
        self.min_shrink = min_shrink
        self.max_shrink = max_shrink
        self._history_X: list[np.ndarray] = []
        self._history_y: list[int] = []
        # Calibration tracking: the model's prediction and the market's implied
        # probability at each resolved window (aligned with ``_history_y``).
        self._hist_pred: list[float] = []
        self._hist_market: list[float] = []

    def predict(self, features: dict[str, float], timeframe_seconds: int = 300) -> float:
        x = feature_vector(features, timeframe_seconds).reshape(1, -1)
        if not self.logreg.fitted and not self.lgbm.trained:
            return float(features.get("option_implied_prob", 0.5))

        prob = 0.0
        weight = 0.0
        if self.logreg.fitted:
            prob += (1.0 - self.lgbm_weight) * float(self.logreg.predict_proba(x)[0])
            weight += (1.0 - self.lgbm_weight)
        if self.lgbm.trained:
            prob += self.lgbm_weight * float(self.lgbm.predict(x)[0])
            weight += self.lgbm_weight
        if weight <= 0:
            return float(features.get("option_implied_prob", 0.5))
        return float(np.clip(prob / weight, 0.0, 1.0))

    def update(self, features: dict[str, float], outcome: float, timeframe_seconds: int = 300) -> None:
        """Online-update the logistic regressor and accumulate history for LGBM."""
        x = feature_vector(features, timeframe_seconds)
        y = 1 if outcome >= 0.5 else 0
        # Record the pre-update prediction and the market's implied probability
        # (from ``contract_price``) so the calibrator can score the model against
        # the market on this resolved window.
        pred = float(self.predict(features, timeframe_seconds))
        market = float(np.clip(float(features.get("contract_price", 0.5)), 0.01, 0.99))
        self._hist_pred.append(pred)
        self._hist_market.append(market)
        self.logreg.partial_fit(x.reshape(1, -1), np.array([y]))
        self._history_X.append(x)
        self._history_y.append(y)

    def shrinkage_weight(self) -> float:
        """Fraction of weight to give the market when combining probabilities.

        Compares the model's Brier score against the market's Brier score over the
        accumulated history. With little history (or a market that scores near
        zero) the model is unproven, so we default to ``max_shrink`` (heavy market
        weight). A model that demonstrably beats the market earns a lower weight,
        down to ``min_shrink``.
        """
        n = len(self._history_y)
        if n < 2:
            return self.max_shrink

        model_brier = sum((p - y) ** 2 for p, y in zip(self._hist_pred, self._history_y, strict=True)) / n
        market_brier = sum((m - y) ** 2 for m, y in zip(self._hist_market, self._history_y, strict=True)) / n
        if market_brier <= 1e-9:
            return self.max_shrink

        # Fractional Brier improvement of the model over the market, clamped to
        # [0, 1] (a model worse than the market earns zero skill).
        skill = max(0.0, min(1.0, 1.0 - model_brier / market_brier))
        # Shrink the noisy skill estimate toward 0 (no skill) proportional to the
        # number of observed samples (a weak Bayesian prior).
        prior_strength = 20.0
        skill = skill * n / (n + prior_strength)
        return self.max_shrink - skill * (self.max_shrink - self.min_shrink)

    def calibrate(self, raw_prob: float, market_prob: float) -> float:
        """Shrink ``raw_prob`` toward ``market_prob`` by the adaptive weight."""
        w = self.shrinkage_weight()
        return float(np.clip((1.0 - w) * raw_prob + w * market_prob, 0.01, 0.99))

    def retrain_lgbm(self) -> None:
        if len(self._history_X) >= 4:
            X = np.vstack(self._history_X)
            y = np.asarray(self._history_y)
            self.lgbm.fit(X, y)

    def logreg_weights(self) -> dict[str, float]:
        """Serialize the online logistic weights as ``{feature: weight, 'bias': b}``.

        Empty (``{}``) when the logistic model has not been fitted yet.
        """
        if not self.logreg.fitted:
            return {}
        coef = self.logreg.model.coef_[0]
        intercept = float(self.logreg.model.intercept_[0])
        out: dict[str, float] = {"bias": intercept}
        for i, name in enumerate(FEATURE_ORDER):
            out[name] = float(coef[i])
        return out

    def proposed_logreg_weights(
        self, features: dict[str, float], outcome: float, timeframe_seconds: int = 300
    ) -> dict[str, float]:
        """Weights the logistic model *would* have after one more ``partial_fit``.

        Returns the hypothetical challenger config without mutating the live model,
        so the promotion gate can compare champion vs. challenger before committing.
        """
        import copy

        clone = copy.deepcopy(self.logreg.model)
        x = feature_vector(features, timeframe_seconds).reshape(1, -1)
        y = np.array([1 if outcome >= 0.5 else 0])
        try:
            clone.partial_fit(x, y, classes=np.array([0, 1]))
        except Exception:  # noqa: BLE001 - fall back to current weights on any fit error
            return self.logreg_weights() or {"bias": 0.0}
        coef = clone.coef_[0]
        intercept = float(clone.intercept_[0])
        out: dict[str, float] = {"bias": intercept}
        for i, name in enumerate(FEATURE_ORDER):
            out[name] = float(coef[i])
        return out
