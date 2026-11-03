"""Tests for the DeepSeek oracle (parsing, caching, graceful fallback)."""

from __future__ import annotations

import asyncio

import pytest

from btc_edge.ml.deepseek_oracle import DeepSeekOracle, _parse_p_up


def test_parse_p_up_pure_json():
    assert _parse_p_up('{"p_up": 0.62, "confidence": 0.8}') == pytest.approx(0.62)


def test_parse_p_up_embedded_json():
    assert _parse_p_up('Here is my estimate: {"p_up": 0.71}') == pytest.approx(0.71)


def test_parse_p_up_regex_fallback():
    assert _parse_p_up('I think "p_up": 0.55 is right.') == pytest.approx(0.55)


def test_parse_p_up_clamps_out_of_range():
    assert _parse_p_up('{"p_up": 1.5}') == 1.0
    assert _parse_p_up('{"p_up": -0.2}') == 0.0


def test_parse_p_up_unparseable():
    assert _parse_p_up("no number here") is None
    assert _parse_p_up("") is None


def test_oracle_unconfigured_query_is_noop():
    oracle = DeepSeekOracle(api_key="")
    assert not oracle.configured
    assert asyncio.run(oracle.query({})) is None


def test_build_prompt_includes_features():
    snap = {"window_delta_pct": 0.1, "contract_price": 0.5, "btc_price": 100_000.0}
    prompt = DeepSeekOracle.build_prompt(snap)
    assert "window_delta_pct" in prompt
    assert "100000" in prompt
