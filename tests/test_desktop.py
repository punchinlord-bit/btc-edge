"""Tests for the desktop operator actions and confirmation gate."""

from __future__ import annotations

from btc_edge.desktop.automation import OperatorActions
from btc_edge.desktop.gui_fallback import ConfirmationGate, PendingOrder
from btc_edge.execution.killswitch import KillSwitch
from btc_edge.web.app import EngineState


def test_operator_halt_arms_persisted_killswitch(tmp_path):
    state = EngineState()
    ks = KillSwitch(tmp_path / "ks.json")
    actions = OperatorActions(state, ks)
    result = actions.halt()
    assert result["kill_switch"] is True
    assert state.kill_switch is True
    assert ks.is_armed() is True


def test_operator_resume_clears_persisted_killswitch(tmp_path):
    state = EngineState()
    ks = KillSwitch(tmp_path / "ks.json")
    actions = OperatorActions(state, ks)
    actions.halt()
    result = actions.resume()
    assert result["kill_switch"] is False
    assert state.kill_switch is False
    assert ks.is_armed() is False


def test_operator_snapshot_reflects_state(tmp_path):
    state = EngineState()
    state.regime = "AMPLIFYING"
    ks = KillSwitch(tmp_path / "ks.json")
    actions = OperatorActions(state, ks)
    snap = actions.snapshot()
    assert snap["regime"] == "AMPLIFYING"
    assert snap["kill_switch"] is False


def test_hotkeys_map_to_actions(tmp_path):
    state = EngineState()
    ks = KillSwitch(tmp_path / "ks.json")
    actions = OperatorActions(state, ks)
    hk = actions.hotkeys()
    assert set(hk) == {"Ctrl+Shift+K", "Ctrl+Shift+R"}
    hk["Ctrl+Shift+K"]()
    assert state.kill_switch is True
    hk["Ctrl+Shift+R"]()
    assert state.kill_switch is False


def test_confirmation_gate_blocks_large_orders():
    gate = ConfirmationGate(threshold_usd=50.0)
    small = PendingOrder(side="up", action="buy", price=0.55, size=10.0)  # $5.50
    large = PendingOrder(side="up", action="buy", price=0.55, size=200.0)  # $110
    assert not gate.requires_confirmation(small)
    assert gate.requires_confirmation(large)
    assert gate.submit(large, user_confirmed=False) is False
    assert gate.submit(large, user_confirmed=True) is True
