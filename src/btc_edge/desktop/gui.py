"""Native PySide6 desktop control panel.

Mirrors the live metrics, strategy toggles, and kill switch from the web state.
PySide6 is imported lazily so the rest of the codebase (and CI) runs without the
GUI toolkit installed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from btc_edge.web.app import EngineState

APP_ICON = Path(__file__).resolve().parent.parent / "web" / "static" / "app-icon.png"


class DesktopApp:
    def __init__(self, state: EngineState) -> None:
        self.state = state

    def run(self) -> None:
        """Launch the PySide6 window + system tray icon."""
        try:
            from PySide6.QtCore import QTimer
            from PySide6.QtGui import QIcon
            from PySide6.QtWidgets import (
                QApplication,
                QLabel,
                QMainWindow,
                QPushButton,
                QVBoxLayout,
                QWidget,
            )
        except ImportError as exc:  # pragma: no cover - requires PySide6
            raise RuntimeError(
                "PySide6 is not installed. Install with: pip install 'btc-edge[desktop]'"
            ) from exc

        app = QApplication.instance() or QApplication([])
        window = QMainWindow()
        window.setWindowTitle("BTC-Edge Control Panel")
        if APP_ICON.exists():
            icon = QIcon(str(APP_ICON))
            app.setWindowIcon(icon)
            window.setWindowIcon(icon)
        central = QWidget()
        layout = QVBoxLayout(central)

        self.mode_label = QLabel("Mode: paper")
        self.regime_label = QLabel("Regime: UNKNOWN")
        self.metrics_label = QLabel("No metrics yet")
        layout.addWidget(self.mode_label)
        layout.addWidget(self.regime_label)
        layout.addWidget(self.metrics_label)

        kill_btn = QPushButton("EMERGENCY KILL SWITCH")
        kill_btn.setStyleSheet("background-color: #b91c1c; color: white; padding: 12px; font-weight: bold;")
        kill_btn.clicked.connect(self._on_kill)
        layout.addWidget(kill_btn)

        window.setCentralWidget(central)
        window.resize(420, 300)
        window.show()

        timer = QTimer()
        timer.timeout.connect(self._refresh)
        timer.start(1000)

        self._setup_tray(app, window)

        app.exec()

    def _setup_tray(self, app: Any, window: Any) -> None:
        from PySide6.QtWidgets import QMenu, QSystemTrayIcon

        tray = QSystemTrayIcon()
        menu = QMenu()
        quit_action = menu.addAction("Quit")
        quit_action.triggered.connect(app.quit)
        tray.setContextMenu(menu)
        tray.show()

    def _on_kill(self) -> None:
        self.state.kill_switch = True

    def _refresh(self) -> None:
        s = self.state.snapshot()
        self.mode_label.setText(f"Mode: {s['mode']}")
        self.regime_label.setText(f"Regime: {s['regime']}")
        lines = []
        for name, m in s["strategy_metrics"].items():
            lines.append(f"{name}: net_ev={m.get('net_ev', 0):.4f} win={m.get('win_rate', 0):.1%}")
        self.metrics_label.setText("\n".join(lines) or "No metrics yet")
