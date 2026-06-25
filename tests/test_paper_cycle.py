from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import scripts.paper_cycle as paper_cycle
from scripts.paper_cycle import PaperDecision, build_decision, format_decision, money, record_decision


def test_build_decision_holds_when_no_buy_setup() -> None:
    decision = build_decision(
        ideas=[{"symbol": "MSFT", "action": "WATCH", "confidence": 70}],
        max_trade_usd=25,
        now=datetime(2026, 6, 23, tzinfo=timezone.utc),
    )

    assert decision.action == "HOLD"
    assert decision.symbol == "MARKET"
    assert decision.paper_order is None


def test_build_decision_creates_top_stock_paper_candidate() -> None:
    decision = build_decision(
        ideas=[
            {"symbol": "MSFT", "action": "BUY_REVIEW", "confidence": 61, "current_price": 120, "reason": "ok"},
            {"symbol": "NVDA", "action": "BUY_REVIEW", "confidence": 84, "current_price": 150, "reason": "better"},
        ],
        max_trade_usd=35,
        now=datetime(2026, 6, 23, tzinfo=timezone.utc),
    )

    assert decision.action == "PAPER_BUY_CANDIDATE"
    assert decision.symbol == "NVDA"
    assert decision.price == 150
    assert decision.paper_order is not None
    assert decision.paper_order["side"] == "buy"
    assert decision.paper_order["size_usd"] == 35
    assert decision.paper_order["confidence"] == 84


def test_record_decision_appends_to_trade_log(tmp_path: Path) -> None:
    trade_log = tmp_path / "TRADE-LOG.md"
    decision = PaperDecision(
        timestamp=datetime(2026, 6, 23, 1, 2, tzinfo=timezone.utc),
        action="HOLD",
        symbol="MARKET",
        price=0,
        reason="No trade idea.",
        paper_order=None,
    )

    record_decision(trade_log, decision)

    content = trade_log.read_text()
    assert "2026-06-23T01:02:00+00:00 - Paper Cycle" in content
    assert "Action:** HOLD" in content


def test_record_decision_includes_paper_order_payload(tmp_path: Path) -> None:
    trade_log = tmp_path / "TRADE-LOG.md"
    decision = PaperDecision(
        timestamp=datetime(2026, 6, 23, 1, 2, tzinfo=timezone.utc),
        action="PAPER_BUY_CANDIDATE",
        symbol="MSFT",
        price=120,
        reason="Test",
        paper_order={"side": "buy", "size_usd": 25},
    )

    record_decision(trade_log, decision)

    assert '"size_usd": 25' in trade_log.read_text()


def test_format_decision_is_telegram_friendly() -> None:
    decision = PaperDecision(
        timestamp=datetime(2026, 6, 23, tzinfo=timezone.utc),
        action="PAPER_BUY_CANDIDATE",
        symbol="NVDA",
        price=150,
        reason="High-confidence paper setup.",
        paper_order={"side": "buy", "size_usd": 35, "confidence": 84, "stop_loss": 142, "take_profit": 168},
    )

    text = format_decision(decision)

    assert "PAPER_BUY_CANDIDATE NVDA" in text
    assert "$35.00" in text
    assert "No live order" in text


def test_money_formats_missing_values_as_zero() -> None:
    assert money(None) == "$0.00"


def test_scanner_watchlist_combines_watchlist_and_universe(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "watchlist.json").write_text('{"symbols": ["aapl"], "positions": [], "risk": {}}')
    (config / "market_universe.json").write_text('{"symbols": ["msft", "aapl"], "max_scan_symbols": 2}')

    watchlist = paper_cycle._scanner_watchlist(tmp_path)

    assert watchlist.symbols == ["AAPL", "MSFT"]


def test_quote_symbols_dedupes_positions_and_scans() -> None:
    from scripts.market_intel import Position, Watchlist

    watchlist = Watchlist(symbols=["AAPL"], positions=[Position("AAPL", 1, 100), Position("BTC-USD", 0.1, 50000)], risk={})

    symbols = paper_cycle._quote_symbols({"scans": [{"symbol": "msft"}, {"symbol": "aapl"}]}, watchlist, limit=3)

    assert symbols == ["AAPL", "MSFT"]


def test_run_paper_cycle_builds_stock_decision_and_notifies(tmp_path: Path, monkeypatch, capsys) -> None:
    from scripts.quotes import PriceHistory, Quote

    root = tmp_path / "project"
    config = root / "config"
    config.mkdir(parents=True)
    (config / "watchlist.json").write_text('{"symbols": ["MSFT"], "positions": [], "risk": {}}')
    (config / "market_universe.json").write_text('{"symbols": ["MSFT"], "max_scan_symbols": 1}')
    (config / "autopilot.json").write_text('{"enabled": true, "mode": "paper", "max_trade_usd": 35}')

    class FakeQuoteClient:
        def get_quotes(self, symbols):
            return {symbol: Quote(symbol, 120, previous_close=118) for symbol in symbols}

        def get_histories(self, symbols):
            return {symbol: PriceHistory(symbol, closes=[100, 102, 104, 106, 108, 120], volumes=[100, 100, 100, 100, 100, 180]) for symbol in symbols}

    class FakeIntelClient:
        quote_client = FakeQuoteClient()

        def snapshot(self, watchlist):
            return {"symbols": watchlist.symbols, "news": [], "insider_filings": [], "risk_flags": []}

    notifications = []
    monkeypatch.setattr(paper_cycle, "MarketIntelClient", FakeIntelClient)
    monkeypatch.setattr(paper_cycle, "scan_market", lambda watchlist, snapshot: {"summary": {"symbols_scanned": 1}, "scans": [{"symbol": "MSFT"}], "alerts": []})
    monkeypatch.setattr(paper_cycle, "build_trade_ideas", lambda *args, **kwargs: [{"symbol": "MSFT", "action": "BUY_REVIEW", "confidence": 84, "current_price": 120, "reason": "test setup"}])
    monkeypatch.setattr(paper_cycle.subprocess, "run", lambda args, check: notifications.append(args))

    decision = paper_cycle.run_paper_cycle(tmp_path, notify=True, root=root)

    assert decision.action == "PAPER_BUY_CANDIDATE"
    assert decision.symbol == "MSFT"
    assert notifications[0][0] == "bash"
    assert "PAPER_BUY_CANDIDATE MSFT" in capsys.readouterr().out
    assert (tmp_path / "TRADE-LOG.md").exists()
