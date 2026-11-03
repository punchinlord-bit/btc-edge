"""Tests for the deterministic PreGraderGate (permissive edge gate + settlement freeze)."""

from __future__ import annotations

import math

from btc_edge.features.time_decay_gate import PreGraderGate


def _gate(elapsed, spot=100000.0, strike=100000.0, rvps=0.0001, ask=0.5,
          quorum=0.5, true_prob=0.5, ticks=()):
    return PreGraderGate().evaluate(
        elapsed, spot, strike, rvps, ask, quorum, true_prob, ticks
    )


def test_allows_positive_edge():
    # model true_prob (0.65) > ask (0.55) -> non-negative edge -> allow in Phase 2
    r = _gate(600.0, ask=0.55, true_prob=0.65)
    assert r.allow is True
    assert r.phase == "theta"


def test_blocks_negative_edge():
    # model true_prob (0.45) < ask (0.55) -> negative edge -> block
    r = _gate(600.0, ask=0.55, true_prob=0.45)
    assert r.allow is False


def test_phase3_allows_no_whipsaw():
    spot = 100000.0
    rvps = 0.0002
    remaining = 900.0 - 780.0
    move = 2.0 * spot * rvps * math.sqrt(remaining)
    strike = spot - move
    ticks = [spot - 30, spot - 15, spot]  # moving away from strike
    r = _gate(780.0, spot=spot, strike=strike, rvps=rvps, ask=0.70, true_prob=0.80,
              ticks=ticks)
    assert r.allow is True
    assert r.phase == "sniper"


def test_phase3_rejects_whipsaw():
    spot = 100000.0
    rvps = 0.0002
    remaining = 900.0 - 780.0
    move = 2.0 * spot * rvps * math.sqrt(remaining)
    strike = spot - move
    ticks = [spot + 30, spot + 15, spot]  # moving back toward strike
    r = _gate(780.0, spot=spot, strike=strike, rvps=rvps, ask=0.70, true_prob=0.80,
              ticks=ticks)
    assert r.allow is False


def test_phase3_rejects_high_ask():
    spot = 100000.0
    rvps = 0.0002
    remaining = 900.0 - 780.0
    move = 2.0 * spot * rvps * math.sqrt(remaining)
    strike = spot - move
    r = _gate(780.0, spot=spot, strike=strike, rvps=rvps, ask=0.90, true_prob=0.95,
              ticks=[spot - 30, spot - 15, spot])
    assert r.allow is False  # ask > 0.84


def test_phase4_settlement_freeze():
    r = _gate(850.0, spot=100000.0, strike=99000.0, ask=0.5, true_prob=0.9)
    assert r.allow is False
    assert r.phase == "freeze"
