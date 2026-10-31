"""CLI for safe mouse automation (operator's own desktop).

Example:
    python scripts/run_mouse.py click 100 200
    python scripts/run_mouse.py halt 320 540 --safe-zone 0,0,800,600
"""

from __future__ import annotations

import argparse
import sys

from btc_edge.desktop.mouse_automation import DashboardMouseActions, MouseAutomation


def main() -> int:
    parser = argparse.ArgumentParser(description="BTC-Edge mouse automation")
    parser.add_argument("action", choices=["move", "click", "double", "scroll", "halt", "resume", "open"])
    parser.add_argument("x", type=int, help="target X coordinate")
    parser.add_argument("y", type=int, help="target Y coordinate")
    parser.add_argument("--lines", type=int, default=0, help="scroll lines (positive=up)")
    parser.add_argument("--safe-zone", help="x1,y1,x2,y2 rectangle; actions outside are blocked")
    args = parser.parse_args()

    zone = None
    if args.safe_zone:
        x1, y1, x2, y2 = (int(v) for v in args.safe_zone.split(","))
        zone = (x1, y1, x2, y2)

    auto = MouseAutomation(safe_zone=zone)
    dash = DashboardMouseActions(auto)

    ok = False
    if args.action == "move":
        ok = auto.move_to(args.x, args.y)
    elif args.action == "click":
        ok = auto.click_at(args.x, args.y)
    elif args.action == "double":
        ok = auto.double_click_at(args.x, args.y)
    elif args.action == "scroll":
        ok = auto.scroll_at(args.x, args.y, args.lines)
    elif args.action == "halt":
        ok = dash.halt(args.x, args.y)
    elif args.action == "resume":
        ok = dash.resume(args.x, args.y)
    elif args.action == "open":
        ok = dash.open_dashboard(args.x, args.y)

    if not ok:
        print("Action blocked (outside safe zone).", file=sys.stderr)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
