"""Tests for the local Ollama 7B execution gate (signal state + Y/N/H + fail-safe)."""

from __future__ import annotations

import asyncio

import httpx

from btc_edge.ml.ollama_gate import OllamaGate, compute_signal_state


def test_compute_signal_state():
    assert compute_signal_state(0.70, 0.10) == "ALIGNED_BULL"
    assert compute_signal_state(0.30, -0.10) == "ALIGNED_BEAR"
    assert compute_signal_state(0.70, -0.10) == "CONFLICT_TRAP"
    assert compute_signal_state(0.30, 0.10) == "CONFLICT_TRAP"
    assert compute_signal_state(0.50, 0.10) == "PIN_RISK"
    assert compute_signal_state(0.52, -0.05) == "PIN_RISK"


def test_parse_single_char():
    assert OllamaGate._parse("Y") == "Y"
    assert OllamaGate._parse("N") == "N"
    assert OllamaGate._parse("H") == "H"
    assert OllamaGate._parse("Yes") == "Y"
    assert OllamaGate._parse("Buy Yes please") == "H"  # first char "B" isn't Y/N/H
    assert OllamaGate._parse("") == "H"
    assert OllamaGate._parse("X") == "H"


def test_build_input():
    s = OllamaGate.build_input("ALIGNED_BULL", "BTC +0.10% since open")
    assert s == "SIGNAL_STATE: [ALIGNED_BULL] | TAPE: [BTC +0.10% since open]"


def test_chat_fails_safe_to_hold(monkeypatch):
    class FakeClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            raise httpx.TimeoutException("timeout")

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    gate = OllamaGate()
    verdict = asyncio.run(gate.chat("SIGNAL_STATE: [PIN_RISK] | TAPE: [x]"))
    assert verdict == "H"


def test_chat_parses_y(monkeypatch):
    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"content": "Y"}}

    class FakeClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    gate = OllamaGate()
    verdict = asyncio.run(gate.chat("SIGNAL_STATE: [ALIGNED_BULL] | TAPE: [x]"))
    assert verdict == "Y"
    assert gate.stats["num_verdicts"] == 1
    assert gate.stats["last_verdict"] == "Y"
    assert gate.stats["ready"] is True


def test_payload_options_and_keep_alive():
    gate = OllamaGate()
    payload = gate._payload("SIGNAL_STATE: [ALIGNED_BULL] | TAPE: [x]")
    assert payload["keep_alive"] == -1  # never unload the 7B
    assert payload["stream"] is False
    assert payload["options"]["temperature"] == 0.0
    assert payload["options"]["num_ctx"] == 512
    assert payload["options"]["num_gpu"] == 99
    assert payload["model"] == "qwen2.5-coder:7b-instruct-q4_K_M"
    assert payload["messages"][0]["role"] == "system"


def test_status_and_note_veto():
    gate = OllamaGate()
    assert gate.status()["ready"] is False
    assert gate.status()["num_vetoes"] == 0
    assert gate.status()["model"] == "qwen2.5-coder:7b-instruct-q4_K_M"
    gate.note_veto()
    gate.note_veto()
    assert gate.status()["num_vetoes"] == 2


def test_warmup_ok(monkeypatch):
    class FakeResp:
        def raise_for_status(self):
            pass

    class FakeClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    gate = OllamaGate()
    assert asyncio.run(gate.warmup()) is True
    assert gate.stats["ready"] is True


def test_warmup_fail(monkeypatch):
    class FakeClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    gate = OllamaGate()
    assert asyncio.run(gate.warmup()) is False
    assert gate.stats["ready"] is False
    assert gate.stats["last_error"]


def test_is_loaded(monkeypatch):
    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"models": [{"name": "qwen2.5-coder:7b-instruct-q4_K_M"}]}

    class FakeClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    gate = OllamaGate()
    assert asyncio.run(gate.is_loaded()) is True


def test_set_model_resets_stats():
    gate = OllamaGate()
    gate.stats["num_verdicts"] = 5
    gate.stats["num_vetoes"] = 3
    gate.stats["ready"] = True
    gate.set_model("qwen3.5:4b")
    assert gate.model == "qwen3.5:4b"
    assert gate.stats["num_verdicts"] == 0
    assert gate.stats["num_vetoes"] == 0
    assert gate.stats["ready"] is False


def test_base_url():
    assert OllamaGate().base_url() == "http://127.0.0.1:11434"
    assert OllamaGate(endpoint="http://127.0.0.1:11434/api/chat").base_url() == "http://127.0.0.1:11434"


def test_parse_keep_alive():
    from btc_edge.ml.ollama_gate import _parse_keep_alive

    assert _parse_keep_alive("-1") == -1
    assert _parse_keep_alive("0") == 0
    assert _parse_keep_alive("512") == 512
    assert _parse_keep_alive("5m") == "5m"
    assert _parse_keep_alive(-1) == -1


def test_payload_uses_custom_knobs():
    gate = OllamaGate(num_ctx=256, num_predict=1, temperature=0.1, num_gpu=20, keep_alive="5m")
    p = gate._payload("SIGNAL_STATE: [ALIGNED_BULL] | TAPE: [x]")
    assert p["keep_alive"] == "5m"
    assert p["options"]["num_ctx"] == 256
    assert p["options"]["num_predict"] == 1
    assert p["options"]["temperature"] == 0.1
    assert p["options"]["num_gpu"] == 20


def test_build_ollama_gate_from_settings(monkeypatch):
    from btc_edge.ml.ollama_gate import build_ollama_gate

    class FakeOllama:
        model = "qwen3.5:4b"
        endpoint = "http://127.0.0.1:11434/api/chat"
        num_ctx = 256
        num_predict = 2
        temperature = 0.0
        num_gpu = 50
        keep_alive = "10m"
        timeout_seconds = 1.5

    class FakeSettings:
        ollama = FakeOllama()

    for var in ("OLLAMA_MODEL", "OLLAMA_ENDPOINT", "OLLAMA_NUM_CTX", "OLLAMA_NUM_PREDICT",
                "OLLAMA_TEMPERATURE", "OLLAMA_NUM_GPU", "OLLAMA_KEEP_ALIVE", "OLLAMA_TIMEOUT"):
        monkeypatch.delenv(var, raising=False)

    gate = build_ollama_gate(FakeSettings())
    assert gate.model == "qwen3.5:4b"
    assert gate.num_ctx == 256
    assert gate.num_predict == 2
    assert gate.num_gpu == 50
    assert gate.keep_alive == "10m"
    assert gate.timeout == 1.5


def test_list_models(monkeypatch):
    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"models": [
                {"name": "qwen2.5-coder:3b-instruct-q4_K_M", "size": 1.93e9},   # too small
                {"name": "qwen2.5-coder:7b-instruct-q4_K_M", "size": 4.68e9},  # capable
                {"name": "huihui_ai/qwen2.5-abliterate:7b", "size": 4.68e9},   # capable
                {"name": "qwen3.5:4b", "size": 3.32e9},                        # too small
                {"name": "panelmaster:latest", "size": 8.99e9},                # too big
            ]}

    class FakeClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    gate = OllamaGate()
    models = asyncio.run(gate.list_models())
    # Only 7B-class models that fit the 6GB VRAM survive the filter.
    names = [m["name"] for m in models]
    assert "qwen2.5-coder:7b-instruct-q4_K_M" in names
    assert "huihui_ai/qwen2.5-abliterate:7b" in names
    assert "qwen2.5-coder:3b-instruct-q4_K_M" not in names
    assert "qwen3.5:4b" not in names
    assert "panelmaster:latest" not in names
    assert models[0]["size_gb"] == 4.68


def test_unload(monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            captured["json"] = json
            return None

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    gate = OllamaGate()
    asyncio.run(gate.unload("qwen2.5-coder:7b-instruct-q4_K_M"))
    assert captured["json"]["keep_alive"] == 0
    assert captured["json"]["model"] == "qwen2.5-coder:7b-instruct-q4_K_M"
