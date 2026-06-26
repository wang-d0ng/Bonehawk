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

    def get_clock(self) -> dict[str, Any]:
        if not self.config.is_configured:
            raise AlpacaError("Alpaca API keys are missing.")
        response = self.http_client.get(f"{self.config.trading_base_url}/v2/clock", headers=self.config.headers())
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    def get_calendar(self, start: str, end: str) -> list[dict[str, Any]]:
        if not self.config.is_configured:
            raise AlpacaError("Alpaca API keys are missing.")
        response = self.http_client.get(
            f"{self.config.trading_base_url}/v2/calendar",
            headers=self.config.headers(),
            params={"start": str(start), "end": str(end)},
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, list) else []

    def get_asset(self, symbol: str) -> dict[str, Any]:
        normalized_symbol = str(symbol or "").strip().upper()
        if not normalized_symbol:
            raise AlpacaError("Alpaca asset symbol is missing.")
        if not self.config.is_configured:
            raise AlpacaError("Alpaca API keys are missing.")
        response = self.http_client.get(f"{self.config.trading_base_url}/v2/assets/{normalized_symbol}", headers=self.config.headers())
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    def get_order(self, order_id: str) -> dict[str, Any]:
        normalized_id = str(order_id or "").strip()
        if not normalized_id:
            raise AlpacaError("Alpaca order id is missing.")
        if not self.config.is_configured:
            raise AlpacaError("Alpaca API keys are missing.")
        response = self.http_client.get(f"{self.config.trading_base_url}/v2/orders/{normalized_id}", headers=self.config.headers())
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        normalized_id = str(order_id or "").strip()
        if not normalized_id:
            return {"ok": False, "status": "invalid_order_id", "message": "Alpaca order id is missing.", "review_only": True}
        if not self.config.is_configured:
            return {"ok": False, "status": "not_configured", "message": "Alpaca keys are missing.", "review_only": True}
        try:
            response = self.http_client.delete(f"{self.config.trading_base_url}/v2/orders/{normalized_id}", headers=self.config.headers())
            request_id = response.headers.get("X-Request-ID")
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            return {
                "ok": False,
                "status": "cancel_rejected",
                "message": "Alpaca rejected the cancel request.",
                "detail": _safe_http_error(error),
                "request_id": error.response.headers.get("X-Request-ID"),
                "review_only": True,
            }
        except httpx.HTTPError as error:
            return {"ok": False, "status": "network_error", "message": "Alpaca cancel request failed.", "detail": _safe_error(error), "review_only": True}
        return {"ok": True, "status": "cancel_requested", "broker": "alpaca", "broker_order_id": normalized_id, "request_id": request_id, "review_only": False}

    def cancel_all_orders(self) -> dict[str, Any]:
        if not self.config.is_configured:
            return {"ok": False, "status": "not_configured", "message": "Alpaca keys are missing.", "review_only": True}
        try:
            response = self.http_client.delete(f"{self.config.trading_base_url}/v2/orders", headers=self.config.headers())
            request_id = response.headers.get("X-Request-ID")
            response.raise_for_status()
            payload = response.json() if response.content else []
        except httpx.HTTPStatusError as error:
            return {
                "ok": False,
                "status": "cancel_rejected",
                "message": "Alpaca rejected the cancel-all request.",
                "detail": _safe_http_error(error),
                "request_id": error.response.headers.get("X-Request-ID"),
                "review_only": True,
            }
        except httpx.HTTPError as error:
            return {"ok": False, "status": "network_error", "message": "Alpaca cancel-all request failed.", "detail": _safe_error(error), "review_only": True}
        rows = payload if isinstance(payload, list) else []
        return {"ok": True, "status": "cancel_requested", "broker": "alpaca", "canceled": len(rows), "orders": rows, "request_id": request_id, "review_only": False}

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
            if order_id := str(data.get("id") or "").strip():
                data = _merge_order_refresh(data, self._refresh_order(order_id))
        except httpx.HTTPStatusError as error:
            status = "rate_limited" if error.response.status_code == 429 else "rejected"
            message = "Alpaca rate limit hit. Waiting before more order requests." if status == "rate_limited" else "Alpaca rejected the order."
            return {
                "ok": False,
                "status": status,
                "message": message,
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
        fill = _order_fill_snapshot(data)
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
            "filled_quantity": fill["filled_quantity"],
            "filled_average_price": fill["filled_average_price"],
            "fill_status": fill["fill_status"],
            "submitted_at": data.get("submitted_at"),
            "filled_at": data.get("filled_at"),
            "request_id": request_id,
            "message": _order_message(self.config.paper, fill["fill_status"], data.get("status")),
            "review_only": False,
        }

    def _refresh_order(self, order_id: str) -> dict[str, Any]:
        try:
            return self.get_order(order_id)
        except (AlpacaError, httpx.HTTPError):
            return {}


class AlpacaError(RuntimeError):
    pass


def alpaca_order_fill_snapshot(order: dict[str, Any]) -> dict[str, Any]:
    return _order_fill_snapshot(order)


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


def _merge_order_refresh(submitted: dict[str, Any], refreshed: dict[str, Any]) -> dict[str, Any]:
    if not refreshed:
        return submitted
    merged = dict(submitted)
    merged.update({key: value for key, value in refreshed.items() if value is not None})
    return merged


def _order_fill_snapshot(order: dict[str, Any]) -> dict[str, Any]:
    broker_status = str(order.get("status") or "").strip().lower()
    filled_quantity = _safe_float(order.get("filled_qty"))
    order_quantity = _safe_float(order.get("qty"))
    fill_status = _fill_status(broker_status, filled_quantity, order_quantity)
    return {
        "filled_quantity": filled_quantity,
        "filled_average_price": _safe_float_or_none(order.get("filled_avg_price")),
        "fill_status": fill_status,
    }


def _fill_status(broker_status: str, filled_quantity: float, order_quantity: float) -> str:
    if broker_status == "filled" or (order_quantity > 0 and filled_quantity >= order_quantity):
        return "filled"
    if filled_quantity > 0:
        return "partially_filled"
    if broker_status in {"accepted", "new", "pending_new", "accepted_for_bidding", "pending_cancel", "pending_replace", "held"}:
        return "not_filled_yet"
    if broker_status in {"canceled", "cancelled", "expired", "rejected", "stopped", "suspended", "done_for_day"}:
        return broker_status
    return "not_filled_yet" if broker_status else "unknown"


def _order_message(paper: bool, fill_status: str, broker_status: Any) -> str:
    mode = "paper" if paper else "live"
    if fill_status == "filled":
        return f"Alpaca {mode} order filled."
    if fill_status == "partially_filled":
        return f"Alpaca {mode} order partially filled."
    if fill_status == "not_filled_yet":
        return f"Alpaca {mode} order accepted but not filled yet."
    if fill_status in {"canceled", "cancelled", "expired", "rejected", "stopped", "suspended", "done_for_day"}:
        return f"Alpaca {mode} order is {fill_status}."
    status = str(broker_status or "submitted").strip() or "submitted"
    return f"Alpaca {mode} order submitted with broker status {status}."


def _safe_float(value: Any) -> float:
    try:
        if value is None or value == "":
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _safe_float_or_none(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_http_error(error: httpx.HTTPStatusError) -> str:
    try:
        payload = error.response.json()
    except ValueError:
        payload = {"error": error.response.text[:240]}
    return str(payload)[:500]


def _safe_error(error: Exception) -> str:
    return str(error)[:500]
