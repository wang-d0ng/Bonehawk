from __future__ import annotations

from scripts.market_intel import Position, Watchlist
from scripts.portfolio_sync import portfolio_sync_snapshot


def test_portfolio_sync_snapshot_combines_manual_stocks_and_crypto() -> None:
    watchlist = Watchlist(symbols=["AAPL", "BTC-USD"], positions=[Position("AAPL", 2, 100), Position("BTC-USD", 0.01, 60000)], risk={})

    snapshot = portfolio_sync_snapshot(watchlist)

    assert snapshot["stock_positions"][0]["symbol"] == "AAPL"
    assert snapshot["crypto_positions"][0]["symbol"] == "BTC-USD"
    assert snapshot["stock_sync"]["status"] == "watchlist"
    assert snapshot["crypto_sync"]["status"] == "watchlist"


def test_portfolio_sync_snapshot_reports_alpaca_source_when_connected() -> None:
    watchlist = Watchlist(symbols=["AAPL"], positions=[Position("AAPL", 2, 100)], risk={})
    alpaca = {
        "status": "connected",
        "positions": [{"symbol": "NVDA", "qty": "1"}],
        "account": {"portfolio_value": "1000"},
    }

    snapshot = portfolio_sync_snapshot(watchlist, alpaca_portfolio=alpaca)

    assert snapshot["stock_sync"]["status"] == "connected"
    assert snapshot["stock_positions"][0]["symbol"] == "NVDA"
    assert snapshot["alpaca_account"]["portfolio_value"] == "1000"


def test_portfolio_sync_snapshot_reports_alpaca_error_without_watchlist_positions() -> None:
    watchlist = Watchlist(symbols=["AAPL"], positions=[Position("AAPL", 2, 100)], risk={})
    alpaca = {"status": "error", "message": "Alpaca unavailable.", "positions": []}

    snapshot = portfolio_sync_snapshot(watchlist, alpaca_portfolio=alpaca)

    assert snapshot["stock_sync"]["status"] == "error"
    assert snapshot["stock_positions"] == []


def test_portfolio_sync_snapshot_handles_empty_watchlist() -> None:
    snapshot = portfolio_sync_snapshot(Watchlist(symbols=[], positions=[], risk={}))

    assert snapshot["stock_positions"] == []
    assert snapshot["crypto_positions"] == []
