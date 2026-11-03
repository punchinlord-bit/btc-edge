"""Tests for the parallel dual-AI adversarial grader (approve/veto/fallback + 429 failover)."""

from __future__ import annotations

import asyncio
import json

import httpx

from btc_edge.ml.adversarial_grader import AdversarialGrader
from btc_edge.ml.key_pool import KeyPoolManager


def _content(data: dict) -> dict:
    return {"choices": [{"message": {"content": json.dumps(data)}}]}


def _fake_client_factory(handler):
    """Build a fake httpx.AsyncClient whose ``post`` delegates to ``handler(url, headers)``.

    ``handler`` returns (status_code, json_body) or raises httpx.TimeoutException.
    """

    class FakeResponse:
        def __init__(self, status_code, body):
            self.status_code = status_code
            self._body = body

        def json(self):
            return self._body

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError("http", request=None, response=self)

    class FakeClient:
        def __init__(self, timeout=None):
            self._timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            status, body = handler(url, headers)
            return FakeResponse(status, body)

    return FakeClient


def _ds_approve(approved=True):
    return _content({"approve": approved, "confidence": 0.9, "quantReason": "edge supported"})


def _oai_approve(approved=True):
    return _content({"approve": approved, "riskScore": 0.1, "vetoReason": "ok"})


def _telemetry(**overrides):
    base = dict(elapsed_seconds=600.0, z_score=1.5, true_prob=0.7, market_ask=0.6,
                ev_edge=0.1, order_book_imbalance=0.2, swarm_consensus=0.9)
    base.update(overrides)
    return AdversarialGrader.build_telemetry(**base)


def test_dual_approval(monkeypatch):
    def handler(url, headers):
        if "deepseek" in url:
            return (200, _ds_approve(True))
        return (200, _oai_approve(True))

    monkeypatch.setattr(httpx, "AsyncClient", _fake_client_factory(handler))
    grader = AdversarialGrader(
        deepseek_pool=KeyPoolManager(["ds"]), openai_pool=KeyPoolManager(["oai"])
    )
    verdict = asyncio.run(grader.grade(_telemetry(), z_score=1.5, swarm_consensus=0.9))
    assert verdict.approved is True
    assert verdict.source == "dual"


def test_openai_veto_blocks(monkeypatch):
    def handler(url, headers):
        if "deepseek" in url:
            return (200, _ds_approve(True))
        return (200, _oai_approve(False))

    monkeypatch.setattr(httpx, "AsyncClient", _fake_client_factory(handler))
    grader = AdversarialGrader(
        deepseek_pool=KeyPoolManager(["ds"]), openai_pool=KeyPoolManager(["oai"])
    )
    verdict = asyncio.run(grader.grade(_telemetry(), z_score=1.5, swarm_consensus=0.9))
    assert verdict.approved is False


def test_deepseek_veto_blocks(monkeypatch):
    def handler(url, headers):
        if "deepseek" in url:
            return (200, _ds_approve(False))
        return (200, _oai_approve(True))

    monkeypatch.setattr(httpx, "AsyncClient", _fake_client_factory(handler))
    grader = AdversarialGrader(
        deepseek_pool=KeyPoolManager(["ds"]), openai_pool=KeyPoolManager(["oai"])
    )
    verdict = asyncio.run(grader.grade(_telemetry(), z_score=1.5, swarm_consensus=0.9))
    assert verdict.approved is False


def test_openai_timeout_fallback_approve(monkeypatch):
    def handler(url, headers):
        if "deepseek" in url:
            return (200, _ds_approve(True))
        raise httpx.TimeoutException("timeout")

    monkeypatch.setattr(httpx, "AsyncClient", _fake_client_factory(handler))
    grader = AdversarialGrader(
        deepseek_pool=KeyPoolManager(["ds"]), openai_pool=KeyPoolManager(["oai"])
    )
    # zScore >= 1.85 and quorum >= 0.90 -> fallback approve
    verdict = asyncio.run(grader.grade(_telemetry(), z_score=1.9, swarm_consensus=0.95))
    assert verdict.approved is True
    assert verdict.source == "fallback"


def test_openai_timeout_fallback_veto(monkeypatch):
    def handler(url, headers):
        if "deepseek" in url:
            return (200, _ds_approve(True))
        raise httpx.TimeoutException("timeout")

    monkeypatch.setattr(httpx, "AsyncClient", _fake_client_factory(handler))
    grader = AdversarialGrader(
        deepseek_pool=KeyPoolManager(["ds"]), openai_pool=KeyPoolManager(["oai"])
    )
    # zScore below fallback threshold -> veto
    verdict = asyncio.run(grader.grade(_telemetry(), z_score=1.5, swarm_consensus=0.9))
    assert verdict.approved is False


def test_key_rotation_on_429(monkeypatch):
    seen: list[str] = []

    def handler(url, headers):
        if "deepseek" in url:
            key = headers["Authorization"].split(" ")[1]
            seen.append(key)
            if key == "bad":
                return (429, {})
            return (200, _ds_approve(True))
        return (200, _oai_approve(True))

    monkeypatch.setattr(httpx, "AsyncClient", _fake_client_factory(handler))
    grader = AdversarialGrader(
        deepseek_pool=KeyPoolManager(["bad", "good"]), openai_pool=KeyPoolManager(["oai"])
    )
    verdict = asyncio.run(grader.grade(_telemetry(), z_score=1.5, swarm_consensus=0.9))
    assert verdict.approved is True
    # "bad" returned 429 -> rotated to "good"
    assert "bad" in seen and "good" in seen
    assert "bad" not in grader.deepseek_pool.healthy_keys


def test_deepseek_only_mode_no_openai(monkeypatch):
    def handler(url, headers):
        if "deepseek" in url:
            return (200, _ds_approve(True))
        return (500, {})  # OpenAI should never be called in DeepSeek-only mode

    monkeypatch.setattr(httpx, "AsyncClient", _fake_client_factory(handler))
    # Empty OpenAI pool -> DeepSeek-only mode.
    grader = AdversarialGrader(
        deepseek_pool=KeyPoolManager(["ds"]), openai_pool=KeyPoolManager([])
    )
    verdict = asyncio.run(grader.grade(_telemetry(), z_score=1.5, swarm_consensus=0.9))
    assert verdict.approved is True
    assert verdict.source == "deepseek"
