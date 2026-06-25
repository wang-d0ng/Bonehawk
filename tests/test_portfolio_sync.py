from __future__ import annotations

from scripts.market_intel import Position, Watchlist
from scripts.portfolio_sync import portfolio_sync_snapshot


class FakeCryptoClient:
    def holdings(self):
        return {"results": [{"asset_code": "BTC", "total_quantity": "0.01"}, {"asset_code": "ETH", "quantity_available_for_trading": "0.5"}]}


def test_portfolio_sync_snapshot_combines_manual_stocks_and_crypto() -> None:
    watchlist = Watchlist(symbols=["AAPL"], positions=[Position("AAPL", 2, 100)], risk={})

    snapshot = portfolio_sync_snapshot(watchlist, crypto_client=FakeCryptoClient())

    assert snapshot["stock_positions"][0]["symbol"] == "AAPL"
    assert snapshot["crypto_positions"][0] == {"symbol": "BTC-USD", "quantity": 0.01}
    assert snapshot["stock_sync"]["status"] == "manual_only"
    assert snapshot["crypto_sync"]["status"] == "connected"


def test_portfolio_sync_snapshot_reports_crypto_errors() -> None:
    class BrokenClient:
        def holdings(self):
            raise RuntimeError("nope")

    snapshot = portfolio_sync_snapshot(Watchlist(symbols=[], positions=[], risk={}), crypto_client=BrokenClient())

    assert snapshot["crypto_sync"]["status"] == "error"
    assert "nope" not in snapshot["crypto_sync"]["message"]
