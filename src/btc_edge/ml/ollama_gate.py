"""Local Ollama 7B execution gate for the 15m BTC pipeline.

A fast, on-premise LLM gate that decides the trade direction from a compact signal
string. It is ASYNC with a hard 2.0s timeout and fails safe to ``H`` (Hold), so it
never blocks or destabilizes the sub-millisecond deterministic path — it only runs
on the (rare) surviving BUY entries.

Spec:
* Endpoint: http://127.0.0.1:11434/api/chat
* Model: qwen2.5-coder:7b-instruct-q4_K_M
* Options: temperature=0.0, num_ctx=512, num_predict=3, num_gpu=99
  (num_predict raised from 1 -> 3 + a system prompt so the 7B emits a clean Y/N/H
  token instead of a reasoning fragment like "It…")
* Input: "SIGNAL_STATE: [ALIGNED_BULL|ALIGNED_BEAR|CONFLICT_TRAP|PIN_RISK] | TAPE: [Note]"
* Output: single char 'Y' (Buy Yes) | 'N' (Buy No) | 'H' (Hold)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_ENDPOINT = "http://127.0.0.1:11434/api/chat"
DEFAULT_MODEL = "qwen2.5-coder:7b-instruct-q4_K_M"
# The 7B needs ~0.7-2.0s per verdict on this hardware (first-token dominated).
# A 400ms cap caused ~37% timeouts and made the gate veto everything; 2.0s lets it
# actually produce Y/N/H verdicts while still failing safe to Hold on a true hang.
TIMEOUT_SECONDS = 2.0
# Model-picker capability filter: only show models "high enough to actually work".
# 3B/4B are too small to reliably emit Y/N/H (benchmarked: always wrong), and
# ~9GB models (14B / panelmaster) can't load on the 6GB GPU. So keep only 7B-class
# q4 models that both fit and reason well enough for the gate.
MIN_CAPABLE_SIZE_GB = 4.0  # 7B q4 and up (~4.7GB); excludes 3B (1.9GB) / 4B (3.3GB)
MAX_FIT_SIZE_GB = 6.0      # must fit the 6GB VRAM; excludes 14B / panelmaster (~9GB)


def compute_signal_state(model_prob: float, delta_pct: float) -> str:
    """Classify the current signal into one of the four states."""
    if model_prob >= 0.55 and delta_pct >= 0.0:
        return "ALIGNED_BULL"
    if model_prob <= 0.45 and delta_pct <= 0.0:
        return "ALIGNED_BEAR"
    if (model_prob >= 0.55 and delta_pct < 0.0) or (model_prob <= 0.45 and delta_pct > 0.0):
        return "CONFLICT_TRAP"
    return "PIN_RISK"


SYSTEM_PROMPT = (
    "You are a trade execution gate. Reply with EXACTLY ONE character and nothing "
    "else: Y (buy YES/up), N (buy NO/down), or H (hold/no trade). Do not explain."
)


class OllamaGate:
    """Async Ollama chat client that returns a single Y/N/H verdict."""

    def __init__(
        self,
        endpoint: str = DEFAULT_ENDPOINT,
        model: str = DEFAULT_MODEL,
        timeout: float = TIMEOUT_SECONDS,
        num_ctx: int = 512,
        num_predict: int = 3,
        temperature: float = 0.0,
        num_gpu: int = 99,
        keep_alive: Any = -1,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.num_ctx = num_ctx
        self.num_predict = num_predict
        self.temperature = temperature
        self.num_gpu = num_gpu
        self.keep_alive = keep_alive  # -1 (int) = forever, 0 = unload, or "5m"/"30s"
        # Live status exposed to the dashboard (blinking panel indicator).
        self.stats: dict[str, Any] = {
            "ready": False,
            "last_verdict": "",
            "last_latency_ms": 0.0,
            "num_verdicts": 0,
            "num_vetoes": 0,
            "num_timeouts": 0,
            "last_error": "",
        }

    @property
    def configured(self) -> bool:
        return bool(self.endpoint)

    @staticmethod
    def build_input(signal_state: str, tape_note: str) -> str:
        return f"SIGNAL_STATE: [{signal_state}] | TAPE: [{tape_note}]"

    @staticmethod
    def _parse(text: str) -> str:
        c = (text or "").strip().upper()
        if not c:
            return "H"
        return c[0] if c[0] in ("Y", "N", "H") else "H"

    def status(self) -> dict[str, Any]:
        """Snapshot of the gate for the dashboard (no secrets)."""
        out = dict(self.stats)
        out["model"] = self.model
        out["endpoint"] = self.endpoint
        return out

    def note_veto(self) -> None:
        """Called by the trading loop when the gate's verdict vetoes a buy."""
        self.stats["num_vetoes"] += 1

    def _payload(self, input_str: str, num_predict: int | None = None) -> dict[str, Any]:
        np = self.num_predict if num_predict is None else num_predict
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": input_str},
            ],
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {
                "temperature": self.temperature,
                "num_ctx": self.num_ctx,
                "num_predict": np,
                "num_gpu": self.num_gpu,
            },
        }

    def set_model(self, model: str) -> None:
        """Switch the active model (dashboard picker). Resets the verdict counters."""
        self.model = model
        self.stats["ready"] = False
        self.stats["last_verdict"] = ""
        self.stats["num_verdicts"] = 0
        self.stats["num_vetoes"] = 0
        self.stats["num_timeouts"] = 0
        self.stats["last_error"] = ""

    def base_url(self) -> str:
        """Derive the Ollama base URL (for /api/tags, /api/ps, etc.)."""
        return self.endpoint.rsplit("/api/chat", 1)[0]

    async def unload(self, model: str | None = None) -> None:
        """Free VRAM by unloading a model (``keep_alive=0``)."""
        m = model or self.model
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    self.endpoint,
                    json={"model": m, "keep_alive": 0, "stream": False},
                )
            logger.info("Ollama unloaded model %s", m)
        except Exception as exc:  # noqa: BLE001 - best-effort unload
            logger.warning("Ollama unload failed for %s: %s", m, exc)

    async def list_models(self) -> list[dict[str, Any]]:
        """List *capable* installed Ollama models (7B-class, fits VRAM) via /api/tags.

        Smaller models (3B/4B) can't reliably emit Y/N/H and larger-than-VRAM
        models (14B/panelmaster at ~9GB) can't load on the 6GB card, so both are
        hidden from the picker.
        """
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(self.base_url() + "/api/tags")
                resp.raise_for_status()
                out: list[dict[str, Any]] = []
                for m in resp.json().get("models", []):
                    size_gb = round((m.get("size") or 0) / 1e9, 2)
                    if size_gb < MIN_CAPABLE_SIZE_GB or size_gb > MAX_FIT_SIZE_GB:
                        continue
                    out.append({
                        "name": m.get("name"),
                        "size_gb": size_gb,
                    })
                return out
        except Exception:  # noqa: BLE001
            return []

    async def warmup(self, timeout: float = 180.0) -> bool:
        """Pre-load the model so the first live gate call is already warm.

        Uses a long timeout because the first call must load ~4GB of weights into
        GPU/CPU. With ``keep_alive=-1`` the model then stays resident for the whole
        engine session.
        """
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    self.endpoint,
                    json=self._payload("SIGNAL_STATE: [PIN_RISK] | TAPE: [warmup]", num_predict=1),
                )
                resp.raise_for_status()
            self.stats["ready"] = True
            logger.info("Ollama model loaded (warmup OK): %s", self.model)
            return True
        except Exception as exc:  # noqa: BLE001
            self.stats["ready"] = False
            self.stats["last_error"] = str(exc)[:160]
            logger.warning("Ollama warmup failed: %s", exc)
            return False

    async def is_loaded(self) -> bool:
        """Check whether the model is currently resident via Ollama's /api/ps."""
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(self.base_url() + "/api/ps")
                resp.raise_for_status()
                models = resp.json().get("models", [])
            return any(m.get("name") == self.model for m in models)
        except Exception:  # noqa: BLE001
            return False

    async def chat(self, input_str: str) -> str:
        """Ask Ollama for Y/N/H; returns 'H' (Hold) on any timeout/error."""
        start = time.perf_counter()
        try:
            text = await asyncio.wait_for(self._post(input_str), timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001 - timeout, connection error, bad body -> HOLD
            self.stats["num_timeouts"] += 1
            self.stats["last_error"] = str(exc)[:160]
            logger.warning("Ollama gate failed (timeout/error) -> HOLD")
            return "H"
        verdict = self._parse(text)
        self.stats["ready"] = True
        self.stats["last_verdict"] = verdict
        self.stats["last_latency_ms"] = round((time.perf_counter() - start) * 1000.0, 1)
        self.stats["num_verdicts"] += 1
        return verdict

    async def _post(self, input_str: str) -> str:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(self.endpoint, json=self._payload(input_str))
            resp.raise_for_status()
            data = resp.json()
            return data["message"]["content"]


def _parse_keep_alive(value: Any) -> Any:
    """Coerce a keep-alive value: '-1' -> -1 (int), '0' -> 0, '512' -> 512; else string."""
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if s.lstrip("-").isdigit():
        return int(s)
    return s


def build_ollama_gate(settings: Any = None) -> OllamaGate:
    """Build the gate from env overrides, then ``settings.ollama``, then defaults.

    Every knob is configurable via ``OLLAMA_*`` env vars or the ``ollama:`` section
    of ``config/config.yaml``.
    """
    import os

    oll = getattr(settings, "ollama", None) if settings is not None else None

    def pick(env: str, attr: str, default: Any) -> Any:
        v = os.getenv(env)
        if v not in (None, ""):
            return v
        if oll is not None:
            vv = getattr(oll, attr, None)
            if vv is not None:
                return vv
        return default

    return OllamaGate(
        endpoint=str(pick("OLLAMA_ENDPOINT", "endpoint", DEFAULT_ENDPOINT)),
        model=str(pick("OLLAMA_MODEL", "model", DEFAULT_MODEL)),
        timeout=float(pick("OLLAMA_TIMEOUT", "timeout_seconds", TIMEOUT_SECONDS)),
        num_ctx=int(pick("OLLAMA_NUM_CTX", "num_ctx", 512)),
        num_predict=int(pick("OLLAMA_NUM_PREDICT", "num_predict", 3)),
        temperature=float(pick("OLLAMA_TEMPERATURE", "temperature", 0.0)),
        num_gpu=int(pick("OLLAMA_NUM_GPU", "num_gpu", 99)),
        keep_alive=_parse_keep_alive(pick("OLLAMA_KEEP_ALIVE", "keep_alive", -1)),
    )
