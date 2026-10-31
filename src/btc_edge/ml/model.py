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
    """Combined logistic + LightGBM probability estimator."""

    def __init__(self, lgbm_weight: float = 0.4) -> None:
        self.logreg = OnlineLogisticRegression()
        self.lgbm = LightGBMEnsemble()
        self.lgbm_weight = lgbm_weight
        self._history_X: list[np.ndarray] = []
        self._history_y: list[int] = []

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
        self.logreg.partial_fit(x.reshape(1, -1), np.array([y]))
        self._history_X.append(x)
        self._history_y.append(y)

    def retrain_lgbm(self) -> None:
        if len(self._history_X) >= 4:
            X = np.vstack(self._history_X)
            y = np.asarray(self._history_y)
            self.lgbm.fit(X, y)
