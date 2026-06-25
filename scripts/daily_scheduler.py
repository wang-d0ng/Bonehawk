#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time as time_module
from datetime import datetime, time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.dashboard import DashboardService

DEFAULT_SCHEDULE = {"morning": "09:00", "midday": "12:30", "end_of_day": "16:10"}


def parse_schedule(raw: dict[str, str]) -> dict[str, time]:
    merged = {**DEFAULT_SCHEDULE, **raw}
    return {name: _parse_time(value) for name, value in merged.items()}


def due_alerts(now: time, schedule: dict[str, time], sent: set[str]) -> list[str]:
    return [name for name, at_time in schedule.items() if name not in sent and now >= at_time]


def run_alert(kind: str, service: DashboardService) -> dict[str, Any]:
    if kind == "morning":
        return service.trade_idea_alerts()
    if kind == "midday":
        return service.scanner_alerts()
    if kind == "end_of_day":
        return _send_end_of_day(service)
    raise ValueError(f"Unknown alert kind: {kind}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Send scheduled trading bot Telegram alerts.")
    parser.add_argument("--once", choices=["morning", "midday", "end_of_day"])
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--schedule", default=str(ROOT / "config" / "daily_schedule.json"))
    parser.add_argument("--interval-seconds", type=int, default=60)
    args = parser.parse_args()

    service = DashboardService()
    if args.once:
        print(json.dumps(run_alert(args.once, service), indent=2))
        return
    if not args.loop:
        parser.error("Use --once KIND or --loop")

    schedule = parse_schedule(_read_schedule(Path(args.schedule)))
    sent_today: set[str] = set()
    current_day = datetime.now().date()
    while True:
        now = datetime.now()
        if now.date() != current_day:
            current_day = now.date()
            sent_today = set()
        for kind in due_alerts(now.time(), schedule, sent_today):
            run_alert(kind, service)
            sent_today.add(kind)
        time_module.sleep(args.interval_seconds)


def _send_end_of_day(service: DashboardService) -> dict[str, Any]:
    intel = service.market_intel()
    performance = intel.get("portfolio_performance", {})
    message = (
        "End of day portfolio summary:\n"
        f"Value: ${performance.get('total_value', 0)}\n"
        f"P&L: ${performance.get('unrealized_pnl', 0)} ({performance.get('unrealized_pnl_pct', 0)}%)\n"
        "Review only. No live order was placed."
    )
    result = subprocess.run(
        ["bash", str(service.root / "scripts" / "telegram.sh"), message],
        cwd=service.root,
        text=True,
        capture_output=True,
        check=False,
    )
    return {"ok": result.returncode == 0, "message": message, "stdout": result.stdout, "stderr": result.stderr, "returncode": result.returncode}


def _read_schedule(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    return {str(key): str(value) for key, value in payload.items()}


def _parse_time(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(int(hour), int(minute))


if __name__ == "__main__":
    main()
