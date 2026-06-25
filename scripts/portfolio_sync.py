from __future__ import annotations

from dataclasses import asdict

from scripts.market_intel import Watchlist


def portfolio_sync_snapshot(watchlist: Watchlist, alpaca_portfolio: dict[str, object] | None = None) -> dict[str, object]:
    alpaca_portfolio = alpaca_portfolio or {}
    alpaca_status = str(alpaca_portfolio.get("status") or "")
    alpaca_positions = alpaca_portfolio.get("positions") if alpaca_status in {"connected", "partial", "error"} else None
    stock_positions = (
        [dict(position) for position in alpaca_positions or [] if not str(position.get("symbol", "")).endswith("-USD")]
        if alpaca_positions is not None
        else [asdict(position) for position in watchlist.positions if not position.symbol.endswith("-USD")]
    )
    crypto_positions = [asdict(position) for position in watchlist.positions if position.symbol.endswith("-USD")]
    stock_status = alpaca_status if alpaca_positions is not None else "watchlist"

    return {
        "stock_sync": {
            "status": stock_status,
            "message": alpaca_portfolio.get("message") if alpaca_positions is not None else "Stock holdings are read from config/watchlist.json until Alpaca portfolio data is available.",
        },
        "crypto_sync": {
            "status": "watchlist",
            "message": "Digital asset rows are read from config/watchlist.json; Alpaca remains the broker path.",
        },
        "alpaca_account": alpaca_portfolio.get("account", {}),
        "stock_positions": stock_positions,
        "crypto_positions": crypto_positions,
    }
