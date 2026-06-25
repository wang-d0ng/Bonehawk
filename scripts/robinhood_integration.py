from __future__ import annotations

from typing import Any

from scripts.robinhood import RobinhoodConfig


def robinhood_snapshot(config: RobinhoodConfig, client: Any | None) -> dict[str, Any]:
    configured = bool(config.api_key and config.private_key_base64)
    base = {
        "status": "not_configured" if not configured else "unknown",
        "message": "Robinhood Crypto API credentials are missing." if not configured else "Robinhood Crypto API credentials are configured.",
        "api_version": config.api_version,
        "trading_mode": config.trading_mode,
        "configured_account_number": mask_value(config.account_number),
        "capabilities": {
            "crypto_market_data": "supported",
            "crypto_account_read": "supported",
            "crypto_order_read": "supported",
            "crypto_order_place": "blocked_unless_trading_mode_live",
            "stock_trading": "not_supported_by_crypto_api",
        },
        "account": {},
        "holdings": [],
        "orders": [],
        "quotes": [],
    }
    if not configured:
        return base
    if client is None:
        return {**base, "status": "error", "message": "Robinhood client could not be initialized."}
    try:
        account_payload = client.account()
        holdings_payload = client.holdings()
        orders_payload = client.orders(state="open")
        quote_payload = client.quote("BTC-USD", "ETH-USD", "SOL-USD")
    except Exception:
        return {**base, "status": "error", "message": "Robinhood read-only smoke check failed."}

    return {
        **base,
        "status": "connected",
        "message": "Robinhood read-only crypto connection is working.",
        "account": _account_summary(account_payload),
        "holdings": _holdings_summary(holdings_payload),
        "orders": _orders_summary(orders_payload),
        "quotes": _quotes_summary(quote_payload),
    }


def mask_value(value: Any, visible: int = 4) -> str:
    if value is None or value == "":
        return "missing"
    text = str(value)
    if len(text) <= visible:
        return "*" * len(text)
    return f"{'*' * (len(text) - visible)}{text[-visible:]}"


def _account_summary(payload: Any) -> dict[str, Any]:
    account = _first_result(payload)
    return {
        "account_number": mask_value(account.get("account_number")),
        "status": account.get("status") or account.get("state") or "unknown",
    }


def _holdings_summary(payload: Any) -> list[dict[str, Any]]:
    rows = payload.get("results", []) if isinstance(payload, dict) else []
    holdings = []
    for row in rows:
        asset = str(row.get("asset_code", "")).upper()
        if not asset:
            continue
        holdings.append(
            {
                "symbol": f"{asset}-USD",
                "total_quantity": str(row.get("total_quantity") or "0"),
                "available_quantity": str(row.get("quantity_available_for_trading") or row.get("total_quantity") or "0"),
            }
        )
    return holdings


def _orders_summary(payload: Any) -> list[dict[str, Any]]:
    rows = payload.get("results", []) if isinstance(payload, dict) else []
    return [
        {
            "id": mask_value(row.get("id")),
            "symbol": row.get("symbol"),
            "side": row.get("side"),
            "type": row.get("type"),
            "state": row.get("state"),
        }
        for row in rows[:10]
    ]


def _quotes_summary(payload: Any) -> list[dict[str, Any]]:
    rows = payload.get("results", []) if isinstance(payload, dict) else []
    return [
        {
            "symbol": row.get("symbol"),
            "bid": str(row.get("bid") or ""),
            "ask": str(row.get("ask") or ""),
        }
        for row in rows
        if row.get("symbol")
    ]


def _first_result(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        return payload["results"][0] if payload["results"] else {}
    return payload if isinstance(payload, dict) else {}
