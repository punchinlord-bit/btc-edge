"""Autonomous health checker: keeps the BTC-Edge engine alive.

Runs as a SEPARATE process from the engine (so it can restart the engine when it
dies). Every ``CHECK_SECONDS`` it probes:

* ``GET /api/health`` — is the web/engine process responding?
* ``GET /api/btc-15m/ops`` — are any of the 7 Command Deck components DEAD?

If the engine is unreachable for ``FAIL_THRESHOLD`` consecutive probes (crash /
hang / OOM / idle-tier sleep), it kills any stale engine process and relaunches
it, then pushes a Telegram alert. It NEVER touches orders, balances, or risk —
it only restarts a dead process.

Run with the venv python so ``sys.executable`` is the engine interpreter:
    .venv\\Scripts\\python.exe scripts/health_checker.py
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

CHECK_SECONDS = int(os.getenv("HC_CHECK_SECONDS", "30"))
FAIL_THRESHOLD = int(os.getenv("HC_FAIL_THRESHOLD", "3"))  # ~90s of consecutive failure
HEALTH_URL = "http://127.0.0.1:8080/api/health"
OPS_URL = "http://127.0.0.1:8080/api/btc-15m/ops"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
ENGINE_SCRIPT = "scripts/run_engine.py"


def _log(msg: str) -> None:
    print(f"[health-checker {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _telegram(text: str) -> None:
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000]}).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as exc:  # noqa: BLE001 - alerting must never crash the checker
        _log(f"telegram alert failed: {exc}")


def _fetch(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
        return None


def _engine_pids() -> list[int]:
    """Return PIDs of any python process running the engine script."""
    pids: list[int] = []
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["wmic", "process", "where", "name='python.exe'", "get", "processid,commandline"],
                capture_output=True, text=True, timeout=15,
            )
            for line in out.stdout.splitlines():
                if "run_engine.py" in line:
                    parts = line.strip().split()
                    if parts and parts[-1].isdigit():
                        pids.append(int(parts[-1]))
        except Exception:  # noqa: BLE001
            pass
    return pids


def _restart_engine() -> None:
    _log("restarting engine…")
    for pid in _engine_pids():
        with contextlib.suppress(Exception):
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=15)
    time.sleep(3)
    log_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        subprocess.Popen(
            [sys.executable, ENGINE_SCRIPT],
            cwd=log_dir,
            stdout=open(os.path.join(log_dir, "server_out.txt"), "a"),  # noqa: SIM115
            stderr=open(os.path.join(log_dir, "server_err.txt"), "a"),  # noqa: SIM115
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        _telegram("♻️ BTC-Edge engine was DOWN — health-checker restarted it.")
    except Exception as exc:  # noqa: BLE001
        _log(f"restart failed: {exc}")


def _dead_components() -> list[str]:
    data = _fetch(OPS_URL)
    if not data:
        return []
    return [
        c["name"]
        for c in data.get("components", [])
        if c["status"] in ("DEAD", "ERROR")
    ]


def main() -> None:
    _log("health-checker started (watching engine + Command Deck)")
    fails = 0
    alerted_dead: set[str] = set()
    while True:
        healthy = _fetch(HEALTH_URL) is not None
        dead = _dead_components() if healthy else []
        if healthy:
            fails = 0
            for name in dead:
                if name not in alerted_dead:
                    alerted_dead.add(name)
                    _telegram(f"🚨 BTC-Edge component DEAD: {name}")
            for name in list(alerted_dead):
                if name not in dead:
                    alerted_dead.discard(name)
            _log(f"OK (dead={dead or 'none'})")
        else:
            fails += 1
            _log(f"engine unreachable ({fails}/{FAIL_THRESHOLD})")
            if fails >= FAIL_THRESHOLD:
                _restart_engine()
                fails = 0
        time.sleep(CHECK_SECONDS)


if __name__ == "__main__":
    main()
