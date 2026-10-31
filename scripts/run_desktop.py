"""CLI entry point for the desktop control panel."""

from __future__ import annotations

import sys

from btc_edge.config import get_settings
from btc_edge.desktop.gui import DesktopApp
from btc_edge.execution.killswitch import KillSwitch
from btc_edge.web.app import engine_state


def main() -> int:
    settings = get_settings()
    killswitch = KillSwitch(settings.safety.kill_switch_file)
    dashboard_url = f"http://localhost:{settings.web.port}"
    app = DesktopApp(engine_state, killswitch, dashboard_url)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
