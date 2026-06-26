from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts.quotes import PriceHistory, Quote
from scripts.trading_desk import (
    build_backtest,
    build_data_health,
    order_truth_snapshot,
    record_order_truth_event,
    record_shadow_candidates,
    record_trade_journal_entry,
    shadow_mode_snapshot,
    strategy_scorecard,
    trade_journal_snapshot,
)


def test_order_truth_records_lifecycle_stage_and_summary(tmp_path: Path) -> None:
    event = record_order_truth_event(
        tmp_path,
        "autopilot_order",
        {
            "symbol": "MSFT",
            "side": "BUY",
            "status": "submitted",
            "broker_status": "accepted",
            "broker_order_id": "order-1",
            "filled_quantity": 0,
            "fill_status": "not_filled_yet",
            "notional": 25,
        },
        now=datetime(2026, 6, 26, 13, 0, tzinfo=UTC),
    )

    snapshot = order_truth_snapshot(tmp_path)

    assert event["stage"] == "submitted"
    assert snapshot["summary"]["submitted"] == 1
    assert snapshot["events"][0]["broker_order_id"] == "order-1"
    assert snapshot["events"][0]["notional"] == 25


def test_order_truth_summary_uses_latest_broker_state(tmp_path: Path) -> None:
    record_order_truth_event(
        tmp_path,
        "autopilot_order",
        {
            "symbol": "MSFT",
            "side": "BUY",
            "status": "submitted",
            "broker_status": "accepted",
            "broker_order_id": "order-1",
            "filled_quantity": 0,
            "fill_status": "not_filled_yet",
        },
        now=datetime(2026, 6, 26, 13, 0, tzinfo=UTC),
    )
    record_order_truth_event(
        tmp_path,
        "alpaca_order_reconcile",
        {
            "symbol": "MSFT",
            "side": "BUY",
            "status": "submitted",
            "broker_status": "filled",
            "broker_order_id": "order-1",
            "filled_quantity": 1,
            "filled_average_price": 101.25,
            "fill_status": "filled",
        },
        now=datetime(2026, 6, 26, 13, 2, tzinfo=UTC),
    )

    snapshot = order_truth_snapshot(tmp_path)

    assert snapshot["summary"]["submitted"] == 0
    assert snapshot["summary"]["filled"] == 1
    assert snapshot["summary"]["active"] == 0
    assert snapshot["current"][0]["stage"] == "filled"
    assert snapshot["current"][0]["filled_average_price"] == 101.25
    assert len(snapshot["events"]) == 2


def test_trade_journal_and_strategy_scorecard_track_pnl(tmp_path: Path) -> None:
    record_trade_journal_entry(
        tmp_path,
        "autopilot_order",
        {
            "symbol": "MSFT",
            "side": "buy",
            "action": "AUTO_BUY_CANDIDATE",
            "confidence": 72,
            "probability_up": 0.64,
            "current_price": 100,
            "notional": 25,
            "reason": "Momentum and narrative aligned.",
            "signals": ["strategy momentum_breakout", "volume 2.1x"],
        },
        {
            "ok": True,
            "broker_order_id": "order-1",
            "broker_status": "filled",
            "fill_status": "filled",
            "filled_quantity": 0.25,
            "filled_average_price": 100,
            "realized_pnl": 3.5,
        },
        now=datetime(2026, 6, 26, 13, 1, tzinfo=UTC),
    )

    journal = trade_journal_snapshot(tmp_path)
    scorecard = strategy_scorecard(tmp_path)

    assert journal["summary"]["entries"] == 1
    assert journal["entries"][0]["thesis"] == "Momentum and narrative aligned."
    assert scorecard["strategies"][0]["strategy"] == "momentum_breakout"
    assert scorecard["strategies"][0]["wins"] == 1
    assert scorecard["strategies"][0]["net_pnl"] == 3.5


def test_shadow_mode_records_and_evaluates_candidates(tmp_path: Path) -> None:
    started = datetime.now(UTC) - timedelta(minutes=7)
    record_shadow_candidates(
        tmp_path,
        [
            {
                "symbol": "MSFT",
                "side": "buy",
                "action": "AUTO_BUY_CANDIDATE",
                "current_price": 100,
                "confidence": 70,
                "probability_up": 0.62,
            }
        ],
        now=started,
    )

    snapshot = shadow_mode_snapshot(tmp_path, {"MSFT": Quote("MSFT", 102, previous_close=100)}, min_age_minutes=1)

    assert snapshot["summary"]["evaluated"] == 1
    assert snapshot["summary"]["wins"] == 1
    assert snapshot["items"][0]["outcome"] == "win"
    assert snapshot["items"][0]["return_pct"] == 2.0


def test_backtest_and_data_health_return_actionable_scores(tmp_path: Path) -> None:
    backtest = build_backtest(
        {
            "MSFT": PriceHistory("MSFT", closes=[90, 91, 92, 94, 97, 100], volumes=[100, 120, 140, 160, 190, 230]),
            "SPY": PriceHistory("SPY", closes=[100, 99, 98, 97, 96, 95], volumes=[100] * 6),
        }
    )
    health = build_data_health(
        market_snapshot={"news": [{"symbol": "MSFT"}], "risk_flags": []},
        quotes={"MSFT": Quote("MSFT", 100, previous_close=99)},
        account_state={"source": "alpaca", "available_cash": 500},
        market_gate={"status": "market_open"},
        order_summary={"rejected": 0, "submitted": 2},
    )

    assert backtest["summary"]["symbols_tested"] == 2
    assert backtest["summary"]["best_symbol"] == "MSFT"
    assert health["status"] == "healthy"
    assert health["score"] >= 80


def test_order_truth_snapshot_can_fallback_from_decision_log(tmp_path: Path) -> None:
    log_path = tmp_path / "logs" / "decision_log.jsonl"
    log_path.parent.mkdir(parents=True)
    log_path.write_text(
        json.dumps(
            {
                "timestamp": "2026-06-26T13:00:00+00:00",
                "source": "autopilot_order",
                "symbol": "NVDA",
                "side": "buy",
                "status": "submitted",
                "broker_status": "accepted",
                "broker_order_id": "fallback-order",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    snapshot = order_truth_snapshot(tmp_path)

    assert snapshot["summary"]["submitted"] == 1
    assert snapshot["events"][0]["broker_order_id"] == "fallback-order"
