"""CLI entry point for the desktop control panel."""

from __future__ import annotations

import sys

from btc_edge.desktop.gui import DesktopApp
from btc_edge.web.app import engine_state


def main() -> int:
    app = DesktopApp(engine_state)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
