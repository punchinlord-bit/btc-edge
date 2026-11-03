"""Round-robin API-key pool with per-key cooldown (429 / 5xx / timeout failover).

Reads keys from environment variables in three shapes (in priority order):

* comma-separated: ``DEEPSEEK_API_KEYS=sk-a,sk-b`` / ``OPENAI_API_KEYS=...``
* numbered:       ``DEEPSEEK_API_KEY_1=...``, ``DEEPSEEK_API_KEY_2=...``, ...
* single:         ``DEEPSEEK_API_KEY=...`` (fallback)

A key that returns HTTP 429, any 5xx, or trips the request timeout is marked
``cooldownUntil = now + 45000ms`` and the manager immediately fails over to the
next healthy key so the grader pipeline never crashes on a single bad key.
"""

from __future__ import annotations

import os
import time


class KeyPoolManager:
    """Stateful round-robin key pool with per-key cooldown windows."""

    def __init__(
        self,
        keys: list[str] | tuple[str, ...] = (),
        cooldown_ms: float = 45000.0,
    ) -> None:
        self._keys: list[str] = [k.strip() for k in keys if k and k.strip()]
        self._cooldown_ms = cooldown_ms
        self._cooldown_until: dict[str, float] = {}  # key -> monotonic deadline (ms)
        self._cursor = 0

    @property
    def has_keys(self) -> bool:
        return bool(self._keys)

    @property
    def keys(self) -> list[str]:
        return list(self._keys)

    @property
    def healthy_keys(self) -> list[str]:
        now_ms = time.monotonic() * 1000.0
        return [k for k in self._keys if now_ms >= self._cooldown_until.get(k, 0.0)]

    def next_key(self) -> str | None:
        """Return the next healthy key (round-robin), or ``None`` if all are cooling down."""
        if not self._keys:
            return None
        now_ms = time.monotonic() * 1000.0
        for _ in range(len(self._keys)):
            key = self._keys[self._cursor % len(self._keys)]
            self._cursor += 1
            if now_ms >= self._cooldown_until.get(key, 0.0):
                return key
        return None

    def mark_bad(self, key: str) -> None:
        """Put ``key`` on cooldown for ``cooldown_ms`` (called on 429/5xx/timeout)."""
        if key:
            self._cooldown_until[key] = time.monotonic() * 1000.0 + self._cooldown_ms

    def mark_good(self, key: str) -> None:
        """Clear a key's cooldown (optional, after a healthy response)."""
        self._cooldown_until.pop(key, None)

    def cooldown_remaining_ms(self, key: str) -> float:
        return max(0.0, self._cooldown_until.get(key, 0.0) - time.monotonic() * 1000.0)

    @staticmethod
    def from_env(prefix: str, cooldown_ms: float = 45000.0) -> KeyPoolManager:
        """Build a pool from env vars named after ``prefix`` (e.g. ``DEEPSEEK`` / ``OPENAI``).

        Reads, in order: ``{PREFIX}_API_KEYS`` (comma-separated), numbered
        ``{PREFIX}_API_KEY_1..N``, then the single ``{PREFIX}_API_KEY``.
        """
        keys: list[str] = []

        combined = os.getenv(f"{prefix}_API_KEYS", "")
        for part in combined.split(","):
            part = part.strip()
            if part:
                keys.append(part)

        i = 1
        while True:
            k = os.getenv(f"{prefix}_API_KEY_{i}", "")
            if not k.strip():
                break
            keys.append(k.strip())
            i += 1

        single = os.getenv(f"{prefix}_API_KEY", "")
        if single.strip():
            keys.append(single.strip())

        # De-duplicate while preserving order.
        seen: set[str] = set()
        deduped: list[str] = []
        for k in keys:
            if k not in seen:
                seen.add(k)
                deduped.append(k)

        return KeyPoolManager(deduped, cooldown_ms=cooldown_ms)
