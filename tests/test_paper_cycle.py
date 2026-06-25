from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.paper_cycle import (
    PaperDecision,
    build_decision,
    extract_account_snapshot,
    extract_btc_position,
    extract_quote,
    format_decision,
    latest_research_report,
    record_decision,
)


def test_extracts_account_position_and_quote_payloads() -> None:
    account = extract_account_snapshot(
        {"results": [{"buying_power": "125.50", "buying_power_currency": "USD", "status": "active"}]}
    )
    position = extract_btc_position(
        {"results": [{"asset_code": "BTC", "quantity_available_for_trading": "0.01234567", "total_quantity": "0.02"}]}
    )
    quote = extract_quote({"results": [{"symbol": "BTC-USD", "bid": "60000", "ask": "60020"}]})

    assert account.buying_power_usd == 125.50
    assert account.status == "active"
    assert position.quantity_btc == 0.01234567
    assert quote.mid_price == 60010


def test_latest_research_report_returns_newest_json(tmp_path: Path) -> None:
    report_dir = tmp_path / "research-reports"
    report_dir.mkdir()
    older = report_dir / "2026-06-22-12.json"
    newer = report_dir / "2026-06-23-00.json"
    older.write_text(json.dumps({"decision": "HOLD"}))
    newer.write_text(json.dumps({"trade_ideas": [{"grade": "A"}]}))

    report = latest_research_report(report_dir)

    assert report == {"trade_ideas": [{"grade": "A"}]}


def test_build_decision_holds_when_drawdown_halt_is_active() -> None:
    decision = build_decision(
        account=extract_account_snapshot({"results": [{"buying_power": "1000", "status": "active"}]}),
        position=extract_btc_position({"results": []}),
        quote=extract_quote({"results": [{"symbol": "BTC-USD", "bid": "60000", "ask": "60010"}]}),
        research={"trade_ideas": [{"grade": "A", "entry": 60010, "stop": 57000, "target": 66000}]},
        project_context="DRAWDOWN_HALT=true",
        now=datetime(2026, 6, 23, tzinfo=timezone.utc),
    )

    assert decision.action == "HOLD"
    assert "drawdown halt" in decision.reason.lower()


def test_build_decision_manages_existing_position() -> None:
    decision = build_decision(
        account=extract_account_snapshot({"results": [{"buying_power": "500", "status": "active"}]}),
        position=extract_btc_position({"results": [{"asset_code": "BTC", "quantity_available_for_trading": "0.01"}]}),
        quote=extract_quote({"results": [{"symbol": "BTC-USD", "bid": "60000", "ask": "60020"}]}),
        research=None,
        project_context="DRAWDOWN_HALT=false",
        now=datetime(2026, 6, 23, tzinfo=timezone.utc),
    )

    assert decision.action == "MANAGE_REVIEW"
    assert decision.paper_order is None


def test_build_decision_creates_buy_candidate_from_a_grade_research() -> None:
    decision = build_decision(
        account=extract_account_snapshot({"results": [{"buying_power": "1000", "status": "active"}]}),
        position=extract_btc_position({"results": []}),
        quote=extract_quote({"results": [{"symbol": "BTC-USD", "bid": "60000", "ask": "60020"}]}),
        research={
            "trade_ideas": [
                {
                    "grade": "A",
                    "playbook_setup": "catalyst_driven_breakout",
                    "entry": 60020,
                    "stop": 57000,
                    "target": 67060,
                    "thesis": "test thesis",
                }
            ]
        },
        project_context="DRAWDOWN_HALT=false",
        now=datetime(2026, 6, 23, tzinfo=timezone.utc),
    )

    assert decision.action == "PAPER_BUY_CANDIDATE"
    assert decision.paper_order is not None
    assert decision.paper_order["side"] == "buy"
    assert decision.paper_order["size_usd"] == 190
    assert decision.paper_order["risk_pct"] == 0.01


def test_build_decision_skips_bad_risk_reward() -> None:
    decision = build_decision(
        account=extract_account_snapshot({"results": [{"buying_power": "1000", "status": "active"}]}),
        position=extract_btc_position({"results": []}),
        quote=extract_quote({"results": [{"symbol": "BTC-USD", "bid": "60000", "ask": "60020"}]}),
        research={"trade_ideas": [{"grade": "B", "entry": 60020, "stop": 59500, "target": 60500}]},
        project_context="DRAWDOWN_HALT=false",
        now=datetime(2026, 6, 23, tzinfo=timezone.utc),
    )

    assert decision.action == "HOLD"
    assert "2R" in decision.reason


def test_record_decision_appends_to_trade_log(tmp_path: Path) -> None:
    trade_log = tmp_path / "TRADE-LOG.md"
    decision = PaperDecision(
        timestamp=datetime(2026, 6, 23, 1, 2, tzinfo=timezone.utc),
        action="HOLD",
        symbol="BTC-USD",
        price=60010,
        reason="No trade idea.",
        paper_order=None,
    )

    record_decision(trade_log, decision)

    content = trade_log.read_text()
    assert "2026-06-23T01:02:00+00:00 - Paper Cycle" in content
    assert "Action:** HOLD" in content


def test_format_decision_is_telegram_friendly() -> None:
    decision = PaperDecision(
        timestamp=datetime(2026, 6, 23, tzinfo=timezone.utc),
        action="PAPER_BUY_CANDIDATE",
        symbol="BTC-USD",
        price=60010,
        reason="A-grade paper setup.",
        paper_order={"side": "buy", "size_usd": 190, "stop": 57000, "target": 67060, "risk_pct": 0.01},
    )

    text = format_decision(decision)

    assert "PAPER_BUY_CANDIDATE BTC-USD" in text
    assert "$190.00" in text
    assert "No live order" in text
