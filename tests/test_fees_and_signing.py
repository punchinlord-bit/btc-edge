"""Tests for fee models, Kalshi signing, and Polymarket slug discovery."""

from __future__ import annotations

import base64

import pytest

from btc_edge.backtest import fee_models
from btc_edge.connectors.kalshi_client import (
    KalshiSigner,
    TokenBucket,
    _strip_query,
    parse_orderbook,
)
from btc_edge.connectors.poly_client import (
    _parse_json_list,
    compute_window_ts,
    slug_for,
)

# ---------------------------------------------------------------- fee models

def test_polymarket_taker_fee_formula():
    # 0.07 * shares * p * (1 - p)
    assert fee_models.polymarket_taker_fee(100.0, 0.5) == pytest.approx(100 * 0.07 * 0.5 * 0.5)
    assert fee_models.polymarket_taker_fee(10.0, 0.97) == pytest.approx(10 * 0.07 * 0.97 * 0.03)


def test_kalshi_taker_fee_rounding():
    # ceil(0.07 * contracts * p * (1-p) * 100) / 100
    raw = 0.07 * 10 * 0.5 * 0.5 * 100
    expected = -(-raw // 1) / 100  # ceil
    assert fee_models.kalshi_taker_fee(10.0, 0.5) == pytest.approx(expected)


def test_maker_fees_zero():
    assert fee_models.polymarket_maker_fee(100, 0.5) == 0.0
    assert fee_models.kalshi_maker_fee(100, 0.5) == 0.0
    assert fee_models.maker_fee("polymarket", 10, 0.5) == 0.0


def test_min_order_size():
    assert fee_models.apply_min_order("polymarket", 4.0) == 0.0
    assert fee_models.apply_min_order("polymarket", 5.0) == 5.0
    assert fee_models.apply_min_order("kalshi", 1.0) == 1.0


def test_slippage_and_cap():
    assert fee_models.effective_fill_price(0.5, is_maker=True) == 0.5
    assert fee_models.effective_fill_price(0.5, is_maker=False) == pytest.approx(0.51)
    assert fee_models.cap_fill_size(100.0, 30.0) == 30.0


# ------------------------------------------------------------------ signing

def test_strip_query():
    assert _strip_query("/portfolio/balance?a=1&b=2") == "/portfolio/balance"
    assert _strip_query("/markets") == "/markets"


def test_ed25519_signing():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()

    signer = KalshiSigner("ACCESS_KEY_123", pem)
    assert signer.key_type == "ed25519"

    req = signer.sign("GET", "/portfolio/balance?currency=USD")
    assert req.headers["KALSHI-ACCESS-KEY"] == "ACCESS_KEY_123"
    assert req.headers["KALSHI-ACCESS-TIMESTAMP"].isdigit()
    sig = base64.b64decode(req.headers["KALSHI-ACCESS-SIGNATURE"])
    assert len(sig) == 64  # Ed25519 signature length


def test_rsa_pss_signing():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()

    signer = KalshiSigner("ACCESS_KEY_456", pem)
    assert signer.key_type == "rsa"
    req = signer.sign("POST", "/portfolio/orders")
    assert "KALSHI-ACCESS-SIGNATURE" in req.headers


def test_signing_uses_query_stripped_path():
    """Verify the signature payload excludes query params (critical rule)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    signer = KalshiSigner("K", pem)

    # Two paths differing only in query params must be treated identically.
    # We verify indirectly by checking that _strip_query is applied in sign().
    req = signer.sign("GET", "/portfolio/balance?foo=bar")
    # Reconstruct the signed message and confirm it matches the stripped path.
    ts = int(req.headers["KALSHI-ACCESS-TIMESTAMP"])
    message = f"{ts}GET/portfolio/balance".encode()
    key.public_key().verify(base64.b64decode(req.headers["KALSHI-ACCESS-SIGNATURE"]), message)


def test_token_bucket_rate_limit():
    import asyncio

    async def _run():
        bucket = TokenBucket(rate_per_sec=1.0)
        # First acquire is immediate; the second must wait ~1s.
        t0 = asyncio.get_event_loop().time()
        await bucket.acquire()
        await bucket.acquire()
        elapsed = asyncio.get_event_loop().time() - t0
        assert elapsed >= 0.9

    asyncio.run(_run())


# ------------------------------------------------------------ orderbook parse

def test_parse_orderbook_uses_fp_sizes():
    payload = {
        "orderbook": {
            "yes_bid": [{"price": 60, "size": 100}, {"price": 59, "size": 50}],
            "yes_ask": [{"price": 62, "size": 80}, {"price": 63, "size": 40}],
            "yes_bid_size_fp": 100.5,
            "yes_ask_size_fp": 80.25,
        }
    }
    book = parse_orderbook(payload, "KXBTC15M")
    assert book.best_bid == 60.0
    assert book.best_ask == 62.0
    assert book.best_bid_size == 100.5
    assert book.best_ask_size == 80.25


# ------------------------------------------------------------------ polymarket

def test_compute_window_ts():
    assert compute_window_ts(1700000123, 300) == (1700000123 // 300) * 300
    assert compute_window_ts(1700000123, 600) == (1700000123 // 600) * 600
    assert compute_window_ts(1700000123, 900) == (1700000123 // 900) * 900


def test_slug_for():
    assert slug_for(300, 1700000100) == "btc-updown-5m-1700000100"
    assert slug_for(600, 1700000100) == "btc-updown-10m-1700000100"
    assert slug_for(900, 1700000100) == "btc-updown-15m-1700000100"


def test_parse_json_list():
    assert _parse_json_list('["1", "2"]') == ["1", "2"]
    assert _parse_json_list(["1", "2"]) == ["1", "2"]
    assert _parse_json_list(None) == []
