#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from dotenv import load_dotenv
from nacl.signing import SigningKey

BASE_URL = "https://trading.robinhood.com"
DEFAULT_SYMBOL = "BTC-USD"


class RobinhoodError(RuntimeError):
    pass


@dataclass(frozen=True)
class RobinhoodConfig:
    api_key: str
    private_key_base64: str
    account_number: str | None
    api_version: str
    trading_mode: str

    @classmethod
    def from_env(cls, dotenv_path: str | Path | None = None) -> "RobinhoodConfig":
        if dotenv_path is None:
            load_dotenv()
        else:
            load_dotenv(dotenv_path=dotenv_path, override=True)
        return cls(
            api_key=os.getenv("ROBINHOOD_API_KEY", ""),
            private_key_base64=os.getenv("ROBINHOOD_PRIVATE_KEY_BASE64", ""),
            account_number=os.getenv("ROBINHOOD_ACCOUNT_NUMBER") or None,
            api_version=os.getenv("ROBINHOOD_API_VERSION", "v2"),
            trading_mode=os.getenv("TRADING_MODE", "paper"),
        )

    def require_credentials(self) -> None:
        missing = []
        if not self.api_key:
            missing.append("ROBINHOOD_API_KEY")
        if not self.private_key_base64:
            missing.append("ROBINHOOD_PRIVATE_KEY_BASE64")
        if missing:
            raise RobinhoodError(f"Missing required env var(s): {', '.join(missing)}")

    def require_account_number(self) -> str:
        if not self.account_number:
            raise RobinhoodError("ROBINHOOD_ACCOUNT_NUMBER is required for v2 account-scoped calls")
        return self.account_number

    def require_live_trading(self) -> None:
        if self.trading_mode != "live":
            raise RobinhoodError("Order placement blocked because TRADING_MODE is not live")


class RobinhoodCryptoClient:
    def __init__(
        self,
        config: RobinhoodConfig,
        http_client: httpx.Client | None = None,
        clock: callable | None = None,
    ) -> None:
        config.require_credentials()
        self.config = config
        self.http_client = http_client or httpx.Client(timeout=15)
        self.clock = clock or time.time
        self.signing_key = SigningKey(base64.b64decode(config.private_key_base64))

    def authorization_headers(self, method: str, path: str, body: str = "") -> dict[str, str]:
        timestamp = str(int(self.clock()))
        method = method.upper()
        message = f"{self.config.api_key}{timestamp}{path}{method}{body}"
        signed = self.signing_key.sign(message.encode("utf-8"))
        return {
            "x-api-key": self.config.api_key,
            "x-signature": base64.b64encode(signed.signature).decode("utf-8"),
            "x-timestamp": timestamp,
        }

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        body_text = canonical_body(body) if body is not None else ""
        headers = self.authorization_headers(method, path, body_text)
        url = f"{BASE_URL}{path}"
        response = self.http_client.request(
            method,
            url,
            content=body_text if body is not None else None,
            headers={**headers, "Content-Type": "application/json"},
        )
        if response.status_code >= 400:
            raise RobinhoodError(safe_error(response))
        if not response.content:
            return {}
        return response.json()

    def account(self) -> Any:
        if self.config.api_version == "v2":
            return self.request("GET", "/api/v2/crypto/trading/accounts/")
        return self.request("GET", "/api/v1/crypto/trading/accounts/")

    def account_number(self) -> str:
        if self.config.account_number:
            return self.config.account_number
        account = extract_account(self.account())
        account_number = account.get("account_number")
        if not account_number:
            raise RobinhoodError("Could not auto-discover account_number from Robinhood accounts response")
        return str(account_number)

    def holdings(self, *asset_codes: str) -> Any:
        if self.config.api_version == "v2":
            params: dict[str, Any] = {"account_number": self.account_number()}
            if asset_codes:
                params["asset_code"] = list(asset_codes)
            return self.request("GET", f"/api/v2/crypto/trading/holdings/{query(params)}")
        return self.request("GET", f"/api/v1/crypto/trading/holdings/{query({'asset_code': list(asset_codes)})}")

    def quote(self, *symbols: str) -> Any:
        symbols = symbols or (DEFAULT_SYMBOL,)
        if self.config.api_version == "v2":
            return self.request("GET", f"/api/v2/crypto/marketdata/best_bid_ask/{query({'symbol': list(symbols)})}")
        return self.request("GET", f"/api/v1/crypto/marketdata/best_bid_ask/{query({'symbol': list(symbols)})}")

    def estimated_price(self, symbol: str, side: str, quantity: str) -> Any:
        path_prefix = "/api/v2/crypto/trading" if self.config.api_version == "v2" else "/api/v1/crypto/marketdata"
        return self.request("GET", f"{path_prefix}/estimated_price/{query({'symbol': symbol, 'side': side, 'quantity': quantity})}")

    def orders(self, **filters: Any) -> Any:
        if self.config.api_version == "v2":
            params = {"account_number": self.account_number(), **filters}
            return self.request("GET", f"/api/v2/crypto/trading/orders/{query(params)}")
        return self.request("GET", f"/api/v1/crypto/trading/orders/{query(filters)}")

    def place_order(self, side: str, order_type: str, symbol: str, order_config: dict[str, Any]) -> Any:
        self.config.require_live_trading()
        body = {
            "client_order_id": str(uuid.uuid4()),
            "side": side,
            "symbol": symbol,
            "type": order_type,
            f"{order_type}_order_config": stringify_numbers(order_config),
        }
        if self.config.api_version == "v2":
            path = f"/api/v2/crypto/trading/orders/{query({'account_number': self.account_number()})}"
        else:
            path = "/api/v1/crypto/trading/orders/"
        return self.request("POST", path, body)

    def cancel_order(self, order_id: str) -> Any:
        self.config.require_live_trading()
        version = self.config.api_version
        return self.request("POST", f"/api/{version}/crypto/trading/orders/{order_id}/cancel/")


def canonical_body(body: dict[str, Any]) -> str:
    return json.dumps(body, separators=(",", ":"), sort_keys=False)


def stringify_numbers(value: dict[str, Any]) -> dict[str, str]:
    return {key: str(item) for key, item in value.items() if item is not None}


def query(params: dict[str, Any]) -> str:
    pairs: list[tuple[str, str]] = []
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, list | tuple):
            pairs.extend((key, str(item)) for item in value if item is not None)
        else:
            pairs.append((key, str(value)))
    return f"?{urlencode(pairs)}" if pairs else ""


def safe_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except json.JSONDecodeError:
        payload = {"error": response.text}
    return f"Robinhood API error {response.status_code}: {json.dumps(payload)}"


def decimal_floor(value: Decimal, places: str) -> Decimal:
    return value.quantize(Decimal(places), rounding=ROUND_DOWN)


def extract_account(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict) and "results" in payload:
        results = payload.get("results") or []
        return results[0] if results else {}
    return payload if isinstance(payload, dict) else {}


def extract_holding_quantity(payload: Any, asset_code: str) -> Decimal:
    results = payload.get("results", []) if isinstance(payload, dict) else []
    for holding in results:
        if holding.get("asset_code") == asset_code:
            value = holding.get("quantity_available_for_trading") or holding.get("total_quantity") or "0"
            return Decimal(str(value))
    return Decimal("0")


def print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def cmd_account(client: RobinhoodCryptoClient, _args: argparse.Namespace) -> None:
    print_json(client.account())


def cmd_account_number(client: RobinhoodCryptoClient, _args: argparse.Namespace) -> None:
    account = extract_account(client.account())
    account_number = account.get("account_number")
    if not account_number:
        raise RobinhoodError("Could not find account_number in Robinhood accounts response")
    print(account_number)


def cmd_position(client: RobinhoodCryptoClient, _args: argparse.Namespace) -> None:
    print_json(client.holdings("BTC"))


def cmd_quote(client: RobinhoodCryptoClient, args: argparse.Namespace) -> None:
    print_json(client.quote(args.symbol))


def cmd_orders(client: RobinhoodCryptoClient, args: argparse.Namespace) -> None:
    filters = {"state": args.status} if args.status else {}
    print_json(client.orders(**filters))


def cmd_buy(client: RobinhoodCryptoClient, args: argparse.Namespace) -> None:
    if args.usd is None and args.base is None:
        raise RobinhoodError("buy requires --usd or --base")
    if args.base is not None:
        base = decimal_floor(Decimal(str(args.base)), "0.00000001")
    else:
        quote = client.quote(args.symbol)
        ask = Decimal(str(quote["results"][0]["ask"]))
        base = decimal_floor(Decimal(str(args.usd)) / ask, "0.00000001")
    print_json(client.place_order("buy", "market", args.symbol, {"asset_quantity": base}))


def cmd_sell(client: RobinhoodCryptoClient, args: argparse.Namespace) -> None:
    if args.pct is None and args.base is None:
        raise RobinhoodError("sell requires --pct or --base")
    if args.base is not None:
        base = Decimal(str(args.base))
    else:
        holding = extract_holding_quantity(client.holdings("BTC"), "BTC")
        base = holding * (Decimal(str(args.pct)) / Decimal("100"))
    base = decimal_floor(base, "0.00000001")
    print_json(client.place_order("sell", "market", args.symbol, {"asset_quantity": base}))


def cmd_stop(client: RobinhoodCryptoClient, args: argparse.Namespace) -> None:
    if Decimal(str(args.limit)) > Decimal(str(args.stop_price)):
        raise RobinhoodError("sell stop-limit should use --limit at or below --stop-price")
    print_json(
        client.place_order(
            "sell",
            "stop_limit",
            args.symbol,
            {
                "asset_quantity": decimal_floor(Decimal(str(args.base)), "0.00000001"),
                "stop_price": Decimal(str(args.stop_price)),
                "limit_price": Decimal(str(args.limit)),
                "time_in_force": "gtc",
            },
        )
    )


def cmd_cancel(client: RobinhoodCryptoClient, args: argparse.Namespace) -> None:
    print_json(client.cancel_order(args.order_id))


def cmd_cancel_all(client: RobinhoodCryptoClient, _args: argparse.Namespace) -> None:
    orders = client.orders(state="open")
    results = orders.get("results", []) if isinstance(orders, dict) else []
    canceled = [client.cancel_order(order["id"]) for order in results if order.get("id")]
    print_json({"canceled": canceled, "count": len(canceled)})


def cmd_close(client: RobinhoodCryptoClient, args: argparse.Namespace) -> None:
    holding = extract_holding_quantity(client.holdings("BTC"), "BTC")
    if holding <= 0:
        print_json({"closed": False, "reason": "no BTC available for trading"})
        return
    base = decimal_floor(holding, "0.00000001")
    print_json(client.place_order("sell", "market", args.symbol, {"asset_quantity": base}))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Robinhood Crypto Trading API wrapper")
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    subparsers.add_parser("account")
    subparsers.add_parser("account-number")
    subparsers.add_parser("position")
    quote = subparsers.add_parser("quote")
    quote.add_argument("symbol", nargs="?", default=DEFAULT_SYMBOL)

    orders = subparsers.add_parser("orders")
    orders.add_argument("status", nargs="?")

    buy = subparsers.add_parser("buy")
    buy.add_argument("--symbol", default=DEFAULT_SYMBOL)
    buy.add_argument("--usd", type=Decimal)
    buy.add_argument("--base", type=Decimal)

    sell = subparsers.add_parser("sell")
    sell.add_argument("--symbol", default=DEFAULT_SYMBOL)
    sell.add_argument("--pct", type=Decimal)
    sell.add_argument("--base", type=Decimal)

    stop = subparsers.add_parser("stop")
    stop.add_argument("--symbol", default=DEFAULT_SYMBOL)
    stop.add_argument("--base", required=True, type=Decimal)
    stop.add_argument("--stop-price", required=True, type=Decimal)
    stop.add_argument("--limit", required=True, type=Decimal)

    cancel = subparsers.add_parser("cancel")
    cancel.add_argument("order_id")
    subparsers.add_parser("cancel-all")

    close = subparsers.add_parser("close")
    close.add_argument("--symbol", default=DEFAULT_SYMBOL)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    handlers = {
        "account": cmd_account,
        "account-number": cmd_account_number,
        "position": cmd_position,
        "quote": cmd_quote,
        "orders": cmd_orders,
        "buy": cmd_buy,
        "sell": cmd_sell,
        "stop": cmd_stop,
        "cancel": cmd_cancel,
        "cancel-all": cmd_cancel_all,
        "close": cmd_close,
    }
    try:
        client = RobinhoodCryptoClient(RobinhoodConfig.from_env())
        handlers[args.cmd](client, args)
    except RobinhoodError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
