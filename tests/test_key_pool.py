"""Tests for the round-robin KeyPoolManager (rotation + cooldown failover + env parsing)."""

from __future__ import annotations

import time

from btc_edge.ml.key_pool import KeyPoolManager


def test_round_robin_rotation():
    pool = KeyPoolManager(["a", "b", "c"])
    order = [pool.next_key() for _ in range(6)]
    assert order == ["a", "b", "c", "a", "b", "c"]


def test_cooldown_skips_key():
    pool = KeyPoolManager(["a", "b"])
    assert pool.next_key() == "a"
    pool.mark_bad("a")  # "a" now cooling down
    assert pool.next_key() == "b"  # fail over to "b"
    assert pool.next_key() == "b"  # "a" still cooling
    pool.mark_bad("b")  # now both cooling
    assert pool.next_key() is None


def test_cooldown_expires():
    pool = KeyPoolManager(["a"], cooldown_ms=1.0)
    pool.mark_bad("a")
    assert pool.next_key() is None
    time.sleep(0.02)
    assert pool.next_key() == "a"


def test_from_env_comma_separated(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEYS", "sk-a, sk-b,sk-c")
    pool = KeyPoolManager.from_env("DEEPSEEK")
    assert pool.keys == ["sk-a", "sk-b", "sk-c"]


def test_from_env_numbered(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEYS", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY_1", "n1")
    monkeypatch.setenv("DEEPSEEK_API_KEY_2", "n2")
    pool = KeyPoolManager.from_env("DEEPSEEK")
    assert pool.keys == ["n1", "n2"]


def test_from_env_single_fallback(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEYS", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "single")
    pool = KeyPoolManager.from_env("DEEPSEEK")
    assert pool.keys == ["single"]


def test_from_env_deduplicates(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEYS", "x, y")
    monkeypatch.setenv("DEEPSEEK_API_KEY_1", "x")  # duplicate of "x"
    pool = KeyPoolManager.from_env("DEEPSEEK")
    assert pool.keys == ["x", "y"]


def test_healthy_keys_excludes_cooldown():
    pool = KeyPoolManager(["a", "b"])
    pool.mark_bad("a")
    assert pool.healthy_keys == ["b"]
