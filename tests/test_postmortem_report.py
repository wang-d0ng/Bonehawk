from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts.postmortem_report import apply_loss_postmortem_report, build_loss_postmortem_report, load_active_postmortem_learnings


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")


def test_build_loss_postmortem_report_scans_recent_losing_trades(tmp_path: Path) -> None:
    now = datetime(2026, 6, 26, 12, 0, tzinfo=UTC)
    _write_jsonl(
        tmp_path / "logs" / "trade_outcomes.jsonl",
        [
            {
                "timestamp": (now - timedelta(hours=2)).isoformat(),
                "symbol": "MSFT",
                "source": "autopilot_order",
                "realized_pnl": -8.25,
                "reason": "stop hit after market trend down",
                "signals": ["market down", "volume 2.1x", "price failed confirmation"],
            },
            {
                "timestamp": (now - timedelta(hours=3)).isoformat(),
                "symbol": "MSFT",
                "source": "alpaca_stock_order",
                "realized_pnl": -4.75,
                "reason": "negative news reversed breakout",
                "signals": ["news lawsuit", "rsi 74", "market down"],
            },
            {
                "timestamp": (now - timedelta(hours=26)).isoformat(),
                "symbol": "TSLA",
                "realized_pnl": -99,
                "reason": "old loss",
                "signals": ["old"],
            },
            {
                "timestamp": (now - timedelta(hours=1)).isoformat(),
                "symbol": "AAPL",
                "realized_pnl": 12,
                "reason": "winner",
                "signals": ["ignored"],
            },
        ],
    )

    report = build_loss_postmortem_report(tmp_path, now=now)

    assert report["ok"] is True
    assert report["status"] == "ready"
    assert report["window_hours"] == 24
    assert report["summary"]["loss_count"] == 2
    assert report["summary"]["total_realized_pnl"] == -13.0
    assert report["summary"]["largest_loss"]["symbol"] == "MSFT"
    assert report["patterns"]["symbols"][0]["symbol"] == "MSFT"
    assert report["patterns"]["signals"][0]["signal"] == "market down"
    assert any(issue["kind"] == "repeated_symbol_loss" for issue in report["issues"])
    assert any(update["type"] == "cooldown_symbol" and update["symbol"] == "MSFT" for update in report["suggested_updates"])
    assert "does not place orders" in report["notice"].lower()


def test_apply_loss_postmortem_report_writes_reviewed_learning_records(tmp_path: Path) -> None:
    now = datetime(2026, 6, 26, 12, 0, tzinfo=UTC)
    _write_jsonl(
        tmp_path / "logs" / "trade_outcomes.jsonl",
        [
            {
                "timestamp": (now - timedelta(hours=1)).isoformat(),
                "symbol": "NVDA",
                "realized_pnl": -30,
                "reason": "position too large and stop hit",
                "signals": ["volume faded", "stop hit"],
            }
        ],
    )
    report = build_loss_postmortem_report(tmp_path, now=now)

    applied = apply_loss_postmortem_report(tmp_path, report, reviewer_notes="Confirmed: reduce risk after similar losses.", now=now)
    learning = load_active_postmortem_learnings(tmp_path, now=now)

    assert applied["ok"] is True
    assert applied["status"] == "applied"
    assert applied["learning"]["last_report_id"] == report["report_id"]
    assert learning["risk"]["max_next_risk_fraction"] == 0.5
    assert "NVDA" in learning["cooldown_symbols"]
    assert (tmp_path / "logs" / "postmortem_updates.jsonl").exists()
    assert "Confirmed: reduce risk" in (tmp_path / "memory" / "POSTMORTEM-LEARNINGS.md").read_text()
