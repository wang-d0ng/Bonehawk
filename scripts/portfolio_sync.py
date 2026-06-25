from __future__ import annotations

from dataclasses import asdict
from typing import Any

from scripts.market_intel import Watchlist


def portfolio_sync_snapshot(watchlist: Watchlist, crypto_client: Any | None = None) -> dict[str, Any]:
    stock_positions = [asdict(position) for position in watchlist.positions if not position.symbol.endswith("-USD")]
    crypto_positions: list[dict[str, float | str]] = []
    crypto_sync = {"status": "not_configured", "message": "Robinhood Crypto client not connected."}

    if crypto_client is not None:
        try:
            crypto_positions = _parse_crypto_holdings(crypto_client.holdings())
            crypto_sync = {"status": "connected", "message": f"Loaded {len(crypto_positions)} crypto holding(s)."}
        except Exception:
            crypto_sync = {"status": "error", "message": "Could not load Robinhood Crypto holdings."}

    return {
        "stock_sync": {
            "status": "manual_only",
            "message": "Stock holdings are read from config/watchlist.json until a supported stock broker connector is added.",
        },
        "crypto_sync": crypto_sync,
        "stock_positions": stock_positions,
        "crypto_positions": crypto_positions,
    }


def _parse_crypto_holdings(payload: Any) -> list[dict[str, float | str]]:
    rows = payload.get("results", []) if isinstance(payload, dict) else []
    positions: list[dict[str, float | str]] = []
    for row in rows:
        asset_code = str(row.get("asset_code", "")).upper()
        quantity = row.get("quantity_available_for_trading") or row.get("total_quantity") or "0"
        try:
            parsed_quantity = float(quantity)
        except (TypeError, ValueError):
            parsed_quantity = 0
        if asset_code and parsed_quantity > 0:
            positions.append({"symbol": f"{asset_code}-USD", "quantity": parsed_quantity})
    return positions
