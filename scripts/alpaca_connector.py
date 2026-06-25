from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
LIVE_BASE_URL = "https://api.alpaca.markets"
DATA_BASE_URL = "https://data.alpaca.markets"
LIVE_CONFIRM_PHRASE = "LIVE_ALPACA_ORDER"


@dataclass(frozen=True)
class AlpacaConfig:
    api_key: str = ""
    secret_key: str = ""
    paper: bool = True
    allow_live: bool = False
    base_url: str = ""
    data_base_url: str = DATA_BASE_URL
    timeout_sec: float = 20

    @classmethod
    def from_env(cls, path: Path | None = None) -> "AlpacaConfig":
        env = _read_env(path)
        paper = _env_bool(env.get("ALPACA_PAPER"), default=True)
        return cls(
            api_key=env.get("ALPACA_API_KEY", "").strip(),
            secret_key=env.get("ALPACA_SECRET_KEY", "").strip(),
            paper=paper,
            allow_live=_env_bool(env.get("ALPACA_ALLOW_LIVE"), default=False),
            base_url=(env.get("ALPACA_BASE_URL") or "").strip(),
            data_base_url=(env.get("ALPACA_DATA_BASE_URL") or DATA_BASE_URL).strip(),
            timeout_sec=_env_float(env.get("ALPACA_TIMEOUT_SEC"), default=20, minimum=3, maximum=120),
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key and self.secret_key)

    @property
    def trading_base_url(self) -> str:
        if self.base_url:
            return self.base_url.rstrip("/")
        return PAPER_BASE_URL if self.paper else LIVE_BASE_URL

    def headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.secret_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "status": "configured" if self.is_configured else "not_configured",
            "api_key": "set" if self.api_key else "missing",
            "secret_key": "set" if self.secret_key else "missing",
            "paper": self.paper,
            "allow_live": self.allow_live,
            "base_url": self.trading_base_url,
            "data_base_url": self.data_base_url.rstrip("/"),
            "confirmation_phrase": LIVE_CONFIRM_PHRASE,
        }


@dataclass(frozen=True)
class AlpacaOrderRequest:
    symbol: str
    side: str
    quantity: float | None = None
    notional: float | None = None
    order_type: str = "market"
    time_in_force: str = "day"
    asset_class: str = "stock"
    client_order_id: str | None = None
    stop_loss: float | None = None
    take_profit: float | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "symbol": self.symbol.upper(),
            "side": self.side.lower(),
            "type": self.order_type,
            "time_in_force": self.time_in_force,
        }
        if self.quantity is not None:
            payload["qty"] = _format_decimal(self.quantity)
        if self.notional is not None:
            payload["notional"] = f"{self.notional:.2f}"
        if self.client_order_id:
            payload["client_order_id"] = self.client_order_id
        if self.stop_loss is not None or self.take_profit is not None:
            payload["order_class"] = "bracket"
            if self.stop_loss is not None:
                payload["stop_loss"] = {"stop_price": _format_decimal(self.stop_loss)}
            if self.take_profit is not None:
                payload["take_profit"] = {"limit_price": _format_decimal(self.take_profit)}
        return payload


class AlpacaTradingClient:
    def __init__(self, config: AlpacaConfig, http_client: httpx.Client | None = None) -> None:
        self.config = config
        self.http_client = http_client or httpx.Client(timeout=config.timeout_sec, follow_redirects=True)

    def snapshot(self) -> dict[str, Any]:
        payload = self.config.snapshot()
        if not self.config.is_configured:
            payload["message"] = "Add ALPACA_API_KEY and ALPACA_SECRET_KEY to .env."
            return payload
        try:
            account = self.get_account()
        except Exception as error:
            payload["status"] = "error"
            payload["message"] = _safe_error(error)
            return payload
        payload.update(
            {
                "status": "connected",
                "account_status": account.get("status"),
                "buying_power": account.get("buying_power"),
                "cash": account.get("cash"),
                "portfolio_value": account.get("portfolio_value"),
            }
        )
        return payload

    def get_account(self) -> dict[str, Any]:
        if not self.config.is_configured:
            raise AlpacaError("Alpaca API keys are missing.")
        response = self.http_client.get(f"{self.config.trading_base_url}/v2/account", headers=self.config.headers())
        response.raise_for_status()
        return response.json()

    def get_positions(self) -> list[dict[str, Any]]:
        if not self.config.is_configured:
            raise AlpacaError("Alpaca API keys are missing.")
        response = self.http_client.get(f"{self.config.trading_base_url}/v2/positions", headers=self.config.headers())
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, list) else []

    def place_order(self, request: AlpacaOrderRequest, confirm: str = "") -> dict[str, Any]:
        validation = _validate_order_request(request)
        if validation:
            return validation
        if not self.config.is_configured:
            return {
                "ok": False,
                "status": "not_configured",
                "message": "Alpaca keys are missing. Add ALPACA_API_KEY and ALPACA_SECRET_KEY to .env.",
                "review_only": True,
            }
        if not self.config.paper:
            if not self.config.allow_live:
                return {
                    "ok": False,
                    "status": "live_not_allowed",
                    "message": "Live Alpaca orders are disabled. Keep paper mode on until you explicitly enable ALPACA_ALLOW_LIVE.",
                    "review_only": True,
                }
            if confirm != LIVE_CONFIRM_PHRASE:
                return {
                    "ok": False,
                    "status": "confirmation_required",
                    "message": f"Live Alpaca orders require confirmation phrase {LIVE_CONFIRM_PHRASE}.",
                    "review_only": True,
                }
        try:
            response = self.http_client.post(
                f"{self.config.trading_base_url}/v2/orders",
                headers=self.config.headers(),
                json=request.to_payload(),
            )
            request_id = response.headers.get("X-Request-ID")
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as error:
            return {
                "ok": False,
                "status": "rejected",
                "message": "Alpaca rejected the order.",
                "detail": _safe_http_error(error),
                "request_id": error.response.headers.get("X-Request-ID"),
                "review_only": True,
            }
        except httpx.HTTPError as error:
            return {
                "ok": False,
                "status": "network_error",
                "message": "Alpaca order request failed.",
                "detail": _safe_error(error),
                "review_only": True,
            }
        return {
            "ok": True,
            "status": "submitted",
            "broker": "alpaca",
            "mode": "paper" if self.config.paper else "live",
            "symbol": request.symbol.upper(),
            "side": request.side.upper(),
            "quantity": request.quantity,
            "notional": request.notional,
            "broker_order_id": data.get("id"),
            "client_order_id": data.get("client_order_id"),
            "broker_status": data.get("status"),
            "request_id": request_id,
            "message": f"Alpaca {'paper' if self.config.paper else 'live'} order submitted.",
            "review_only": False,
        }


class AlpacaError(RuntimeError):
    pass


def _validate_order_request(request: AlpacaOrderRequest) -> dict[str, Any] | None:
    symbol = request.symbol.strip().upper()
    if not symbol or len(symbol) > 18 or not all(character.isalnum() or character in {".", "-", "/"} for character in symbol):
        return {"ok": False, "status": "invalid_symbol", "message": "Choose a valid Alpaca symbol.", "review_only": True}
    side = request.side.strip().lower()
    if side not in {"buy", "sell"}:
        return {"ok": False, "status": "invalid_side", "message": "Choose buy or sell.", "review_only": True}
    if (request.quantity is None) == (request.notional is None):
        return {"ok": False, "status": "invalid_size", "message": "Use exactly one of quantity or notional.", "review_only": True}
    if request.quantity is not None and request.quantity <= 0:
        return {"ok": False, "status": "invalid_quantity", "message": "Quantity must be greater than 0.", "review_only": True}
    if request.notional is not None and request.notional <= 0:
        return {"ok": False, "status": "invalid_notional", "message": "Notional amount must be greater than 0.", "review_only": True}
    if request.order_type not in {"market", "limit", "stop", "stop_limit", "trailing_stop"}:
        return {"ok": False, "status": "invalid_order_type", "message": "Unsupported Alpaca order type.", "review_only": True}
    return None


def _read_env(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if "=" not in line or line.strip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _env_bool(value: str | None, default: bool) -> bool:
    if value is None or not str(value).strip():
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "paper"}


def _env_float(value: str | None, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value) if value is not None else default
    except ValueError:
        number = default
    return max(minimum, min(maximum, number))


def _format_decimal(value: float) -> str:
    return f"{value:.8f}".rstrip("0").rstrip(".")


def _safe_http_error(error: httpx.HTTPStatusError) -> str:
    try:
        payload = error.response.json()
    except ValueError:
        payload = {"error": error.response.text[:240]}
    return str(payload)[:500]


def _safe_error(error: Exception) -> str:
    return str(error)[:500]
