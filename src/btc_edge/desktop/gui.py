"""Professional PySide6 desktop control panel for BTC-Edge.

A polished, dark-themed control surface for the operator's own engine: live
metrics, a strategy table, a signals feed, an emergency kill switch, app-level
safety hotkeys, and a system tray. PySide6 is imported lazily so the rest of
the codebase (and CI) runs without the GUI toolkit installed.

Install with: pip install 'btc-edge[desktop]'
"""

from __future__ import annotations

import time
import webbrowser
from pathlib import Path
from typing import Any

from btc_edge.desktop.automation import OperatorActions
from btc_edge.execution.killswitch import KillSwitch
from btc_edge.web.app import EngineState

APP_ICON = Path(__file__).resolve().parent.parent / "web" / "static" / "app-icon.png"

_STYLE = """
QMainWindow { background-color: #0b1220; }
QWidget { color: #e2e8f0; font-family: 'Segoe UI', 'Inter', sans-serif; font-size: 13px; }
QLabel#title { font-size: 22px; font-weight: 700; color: #f8fafc; }
QLabel#subtitle { color: #64748b; font-size: 12px; }
QLabel#badge { border-radius: 6px; padding: 4px 10px; font-weight: 600; background: #1e293b; color: #94a3b8; }
QFrame#card { background: #111827; border: 1px solid #1f2937; border-radius: 10px; }
QTableWidget { background: #111827; border: 1px solid #1f2937; border-radius: 8px; gridline-color: #1f2937; }
QHeaderView::section { background: #1e293b; border: none; padding: 6px; font-weight: 600; }
QListWidget { background: #111827; border: 1px solid #1f2937; border-radius: 8px; }
QPushButton { border-radius: 8px; padding: 10px 16px; font-weight: 600; }
QPushButton#kill { background: #b91c1c; color: white; }
QPushButton#kill:hover { background: #dc2626; }
QPushButton#resume { background: #065f46; color: white; }
QPushButton#resume:hover { background: #047857; }
QPushButton#neutral { background: #1e293b; color: #e2e8f0; }
QPushButton#neutral:hover { background: #334155; }
"""


class DesktopApp:
    """Native control panel mirroring the live engine state."""

    def __init__(
        self,
        state: EngineState,
        killswitch: KillSwitch | None = None,
        dashboard_url: str = "http://localhost:8080",
    ) -> None:
        if killswitch is None:
            from btc_edge.config import get_settings

            killswitch = KillSwitch(get_settings().safety.kill_switch_file)
        self._actions = OperatorActions(state, killswitch)
        self._dashboard_url = dashboard_url
        self._started_at = time.time()
        self._labels: dict[str, Any] = {}
        self._table: Any = None
        self._signals: Any = None

    def run(self) -> None:
        """Launch the PySide6 window + system tray icon."""
        try:
            from PySide6.QtCore import QTimer
            from PySide6.QtGui import QIcon, QKeySequence, QShortcut
            from PySide6.QtWidgets import (
                QAbstractItemView,
                QApplication,
                QFrame,
                QGridLayout,
                QHBoxLayout,
                QHeaderView,
                QLabel,
                QListWidget,
                QMainWindow,
                QPushButton,
                QTableWidget,
                QVBoxLayout,
                QWidget,
            )
        except ImportError as exc:  # pragma: no cover - requires PySide6
            raise RuntimeError(
                "PySide6 is not installed. Install with: pip install 'btc-edge[desktop]'"
            ) from exc

        app = QApplication.instance() or QApplication([])
        app.setStyleSheet(_STYLE)

        window = QMainWindow()
        window.setWindowTitle("BTC-Edge — Prediction Market Console")
        if APP_ICON.exists():
            icon = QIcon(str(APP_ICON))
            app.setWindowIcon(icon)
            window.setWindowIcon(icon)

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(12)

        title = QLabel("BTC-Edge Console")
        title.setObjectName("title")
        subtitle = QLabel("Autonomous 5m/10m/15m BTC prediction-market engine — paper-first")
        subtitle.setObjectName("subtitle")
        root.addWidget(title)
        root.addWidget(subtitle)

        badges = QHBoxLayout()
        self._labels["mode"] = self._badge("MODE: paper")
        self._labels["regime"] = self._badge("REGIME: UNKNOWN")
        self._labels["status"] = self._badge("RUNNING")
        self._labels["uptime"] = QLabel("uptime 0s")
        self._labels["uptime"].setObjectName("subtitle")
        badges.addWidget(self._labels["mode"])
        badges.addWidget(self._labels["regime"])
        badges.addWidget(self._labels["status"])
        badges.addStretch()
        badges.addWidget(self._labels["uptime"])
        root.addLayout(badges)

        card = QFrame()
        card.setObjectName("card")
        metrics = QGridLayout(card)
        metrics.setContentsMargins(16, 16, 16, 16)
        self._labels["sentiment"] = QLabel("News sentiment: 0.00")
        self._labels["at_risk"] = QLabel("At-risk (USD): $0.00")
        self._labels["strike"] = QLabel("Open strike: —")
        self._labels["countdown"] = QLabel("Window countdowns: —")
        metrics.addWidget(self._labels["sentiment"], 0, 0)
        metrics.addWidget(self._labels["at_risk"], 0, 1)
        metrics.addWidget(self._labels["strike"], 1, 0)
        metrics.addWidget(self._labels["countdown"], 1, 1)
        root.addWidget(card)

        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["Strategy", "Net EV", "Win rate"])
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        root.addWidget(self._table, stretch=2)

        self._signals = QListWidget()
        root.addWidget(self._signals, stretch=1)

        buttons = QHBoxLayout()
        kill_btn = QPushButton("⛔ EMERGENCY KILL SWITCH")
        kill_btn.setObjectName("kill")
        kill_btn.clicked.connect(self._actions.halt)
        resume_btn = QPushButton("▶ Resume")
        resume_btn.setObjectName("resume")
        resume_btn.clicked.connect(self._actions.resume)
        dash_btn = QPushButton("🌐 Open Dashboard")
        dash_btn.setObjectName("neutral")
        dash_btn.clicked.connect(self._open_dashboard)
        buttons.addWidget(kill_btn)
        buttons.addWidget(resume_btn)
        buttons.addStretch()
        buttons.addWidget(dash_btn)
        root.addLayout(buttons)

        window.setCentralWidget(central)
        window.resize(760, 560)
        window.show()

        kill_sc = QShortcut(QKeySequence("Ctrl+Shift+K"), window)
        kill_sc.activated.connect(self._actions.halt)
        resume_sc = QShortcut(QKeySequence("Ctrl+Shift+R"), window)
        resume_sc.activated.connect(self._actions.resume)

        self._setup_tray(app, window, icon)

        timer = QTimer()
        timer.timeout.connect(self._refresh)
        timer.start(1000)

        app.exec()

    def _badge(self, text: str) -> Any:
        from PySide6.QtWidgets import QLabel

        lbl = QLabel(text)
        lbl.setObjectName("badge")
        return lbl

    def _setup_tray(self, app: Any, window: Any, icon: Any) -> None:
        from PySide6.QtGui import QIcon
        from PySide6.QtWidgets import QMenu, QSystemTrayIcon

        tray = QSystemTrayIcon(QIcon(str(APP_ICON)) if APP_ICON.exists() else QIcon())
        tray.setToolTip("BTC-Edge Console")
        menu = QMenu()
        show_action = menu.addAction("Show console")
        show_action.triggered.connect(window.show)
        halt_action = menu.addAction("Halt (kill switch)")
        halt_action.triggered.connect(self._actions.halt)
        resume_action = menu.addAction("Resume")
        resume_action.triggered.connect(self._actions.resume)
        menu.addSeparator()
        quit_action = menu.addAction("Quit")
        quit_action.triggered.connect(app.quit)
        tray.setContextMenu(menu)
        tray.show()

    def _open_dashboard(self) -> None:
        webbrowser.open(self._dashboard_url)

    def _refresh(self) -> None:
        from PySide6.QtWidgets import QTableWidgetItem

        s = self._actions.snapshot()
        mode = s.get("mode", "paper")
        killed = bool(s.get("kill_switch", False))
        regime = s.get("regime", "UNKNOWN")

        self._labels["mode"].setText(f"MODE: {mode}")
        self._labels["regime"].setText(f"REGIME: {regime}")
        self._labels["status"].setText("HALTED" if killed else "RUNNING")
        self._labels["status"].setStyleSheet(
            "border-radius: 6px; padding: 4px 10px; font-weight: 600; "
            + ("background: #450a0a; color: #f87171;" if killed else "background: #052e16; color: #4ade80;")
        )
        self._labels["uptime"].setText(f"uptime {int(time.time() - self._started_at)}s")

        sentiment = float(s.get("news_sentiment", 0.0))
        self._labels["sentiment"].setText(f"News sentiment: {sentiment:+.2f}")
        at_risk = float(s.get("risk", {}).get("at_risk_usd", 0.0))
        self._labels["at_risk"].setText(f"At-risk (USD): ${at_risk:,.2f}")
        strike = s.get("open_strike")
        self._labels["strike"].setText(f"Open strike: {strike if strike is not None else '—'}")
        countdowns = s.get("window_countdowns", {})
        if countdowns:
            parts = [f"{k}:{int(v)}s" for k, v in sorted(countdowns.items())]
            self._labels["countdown"].setText("Window countdowns: " + "  ".join(parts))
        else:
            self._labels["countdown"].setText("Window countdowns: —")

        metrics = s.get("strategy_metrics", {})
        self._table.setRowCount(len(metrics))
        for row, (name, m) in enumerate(metrics.items()):
            self._table.setItem(row, 0, QTableWidgetItem(name))
            self._table.setItem(row, 1, QTableWidgetItem(f"{m.get('net_ev', 0.0):.4f}"))
            self._table.setItem(row, 2, QTableWidgetItem(f"{m.get('win_rate', 0.0):.1%}"))

        signals = s.get("signals", [])
        self._signals.clear()
        if signals:
            self._signals.addItems(signals)
        else:
            self._signals.addItem("No active signals — waiting for a setup.")
