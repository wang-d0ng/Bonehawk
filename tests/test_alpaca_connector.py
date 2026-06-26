from __future__ import annotations

import json
from pathlib import Path

import httpx

from scripts.alpaca_connector import AlpacaConfig, AlpacaOrderRequest, AlpacaTradingClient


def test_alpaca_config_reads_env_without_leaking_secrets(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ALPACA_API_KEY=key-secret\n"
        "ALPACA_SECRET_KEY=secret-secret\n"
        "ALPACA_PAPER=true\n"
        "ALPACA_ALLOW_LIVE=false\n"
    )

    config = AlpacaConfig.from_env(env_file)
    snapshot = config.snapshot()

    assert config.is_configured is True
    assert config.paper is True
    assert snapshot["api_key"] == "set"
    assert snapshot["secret_key"] == "set"
    assert "key-secret" not in json.dumps(snapshot)
    assert "secret-secret" not in json.dumps(snapshot)


def test_alpaca_client_places_paper_order_and_returns_ticket_data() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url in {
            httpx.URL("https://paper-api.alpaca.markets/v2/orders"),
            httpx.URL("https://paper-api.alpaca.markets/v2/orders/order-123"),
        }
        assert request.headers["APCA-API-KEY-ID"] == "key"
        if request.method == "POST":
            body = json.loads(request.content.decode())
            assert body["symbol"] == "MSFT"
            assert body["notional"] == "25.00"
        return httpx.Response(
            200,
            headers={"X-Request-ID": "req-123"},
            json={"id": "order-123", "client_order_id": "client-123", "status": "accepted", "symbol": "MSFT"},
        )

    client = AlpacaTradingClient(
        AlpacaConfig(api_key="key", secret_key="secret", paper=True),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.place_order(AlpacaOrderRequest(symbol="MSFT", side="buy", notional=25))

    assert result["ok"] is True
    assert result["broker_order_id"] == "order-123"
    assert result["broker_status"] == "accepted"
    assert result["request_id"] == "req-123"
    assert len(calls) == 2


def test_alpaca_client_refreshes_accepted_order_and_reports_not_filled_yet() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"id": "order-123", "client_order_id": "client-123", "status": "accepted", "filled_qty": "0", "qty": "1"},
            )
        return httpx.Response(
            200,
            json={"id": "order-123", "client_order_id": "client-123", "status": "accepted", "filled_qty": "0", "qty": "1"},
        )

    client = AlpacaTradingClient(
        AlpacaConfig(api_key="key", secret_key="secret", paper=True),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.place_order(AlpacaOrderRequest(symbol="MSFT", side="buy", quantity=1))

    assert calls == ["POST /v2/orders", "GET /v2/orders/order-123"]
    assert result["ok"] is True
    assert result["broker_status"] == "accepted"
    assert result["filled_quantity"] == 0
    assert result["fill_status"] == "not_filled_yet"
    assert result["message"] == "Alpaca paper order accepted but not filled yet."


def test_alpaca_client_reports_fill_when_order_refresh_is_filled() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"id": "order-456", "status": "accepted", "filled_qty": "0", "qty": "1"})
        return httpx.Response(
            200,
            json={"id": "order-456", "status": "filled", "filled_qty": "1", "qty": "1", "filled_avg_price": "123.45"},
        )

    client = AlpacaTradingClient(
        AlpacaConfig(api_key="key", secret_key="secret", paper=True),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.place_order(AlpacaOrderRequest(symbol="MSFT", side="buy", quantity=1))

    assert result["ok"] is True
    assert result["broker_status"] == "filled"
    assert result["filled_quantity"] == 1
    assert result["fill_status"] == "filled"
    assert result["filled_average_price"] == 123.45
    assert result["message"] == "Alpaca paper order filled."


def test_alpaca_client_snapshot_loads_account_summary() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://paper-api.alpaca.markets/v2/account"
        return httpx.Response(200, json={"status": "ACTIVE", "buying_power": "1000", "cash": "500", "portfolio_value": "1500"})

    client = AlpacaTradingClient(
        AlpacaConfig(api_key="key", secret_key="secret", paper=True),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    payload = client.snapshot()

    assert payload["status"] == "connected"
    assert payload["account_status"] == "ACTIVE"
    assert payload["buying_power"] == "1000"


def test_alpaca_client_loads_open_positions() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://paper-api.alpaca.markets/v2/positions"
        return httpx.Response(200, json=[{"symbol": "AAPL", "qty": "2", "avg_entry_price": "100"}])

    client = AlpacaTradingClient(
        AlpacaConfig(api_key="key", secret_key="secret", paper=True),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    payload = client.get_positions()

    assert payload[0]["symbol"] == "AAPL"
    assert payload[0]["qty"] == "2"


def test_alpaca_client_get_clock_loads_market_clock() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://paper-api.alpaca.markets/v2/clock"
        return httpx.Response(
            200,
            json={
                "timestamp": "2026-06-26T13:25:00Z",
                "is_open": False,
                "next_open": "2026-06-26T13:30:00Z",
                "next_close": "2026-06-26T20:00:00Z",
            },
        )

    client = AlpacaTradingClient(
        AlpacaConfig(api_key="key", secret_key="secret", paper=True),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    payload = client.get_clock()

    assert payload["is_open"] is False
    assert payload["next_open"] == "2026-06-26T13:30:00Z"


def test_alpaca_client_loads_market_calendar() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://paper-api.alpaca.markets/v2/calendar?start=2026-06-26&end=2026-06-29"
        return httpx.Response(
            200,
            json=[
                {"date": "2026-06-26", "open": "09:30", "close": "16:00"},
                {"date": "2026-06-29", "open": "09:30", "close": "16:00"},
            ],
        )

    client = AlpacaTradingClient(
        AlpacaConfig(api_key="key", secret_key="secret", paper=True),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    payload = client.get_calendar("2026-06-26", "2026-06-29")

    assert len(payload) == 2
    assert payload[0]["date"] == "2026-06-26"


def test_alpaca_client_cancels_order_and_all_open_orders() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path == "/v2/orders/order-123":
            return httpx.Response(204, headers={"X-Request-ID": "cancel-123"})
        return httpx.Response(200, json=[{"id": "order-1", "status": 200}, {"id": "order-2", "status": 200}])

    client = AlpacaTradingClient(
        AlpacaConfig(api_key="key", secret_key="secret", paper=True),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    canceled = client.cancel_order("order-123")
    all_canceled = client.cancel_all_orders()

    assert calls == ["DELETE /v2/orders/order-123", "DELETE /v2/orders"]
    assert canceled["ok"] is True
    assert canceled["status"] == "cancel_requested"
    assert canceled["request_id"] == "cancel-123"
    assert all_canceled["ok"] is True
    assert all_canceled["canceled"] == 2


def test_alpaca_client_loads_asset_metadata() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://paper-api.alpaca.markets/v2/assets/AACB"
        return httpx.Response(200, json={"symbol": "AACB", "status": "active", "tradable": True, "fractionable": False})

    client = AlpacaTradingClient(
        AlpacaConfig(api_key="key", secret_key="secret", paper=True),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    payload = client.get_asset("aacb")

    assert payload["symbol"] == "AACB"
    assert payload["fractionable"] is False


def test_alpaca_client_snapshot_reports_account_error_safely() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "forbidden"})

    client = AlpacaTradingClient(
        AlpacaConfig(api_key="key", secret_key="secret", paper=True),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    payload = client.snapshot()

    assert payload["status"] == "error"
    assert "key" not in payload["message"]


def test_alpaca_client_blocks_unconfirmed_live_order() -> None:
    client = AlpacaTradingClient(AlpacaConfig(api_key="key", secret_key="secret", paper=False, allow_live=True))

    result = client.place_order(AlpacaOrderRequest(symbol="MSFT", side="buy", notional=25))

    assert result["ok"] is False
    assert result["status"] == "confirmation_required"


def test_alpaca_client_blocks_live_when_not_allowed_even_with_confirmation() -> None:
    client = AlpacaTradingClient(AlpacaConfig(api_key="key", secret_key="secret", paper=False, allow_live=False))

    result = client.place_order(AlpacaOrderRequest(symbol="MSFT", side="buy", notional=25), confirm="LIVE_ALPACA_ORDER")

    assert result["ok"] is False
    assert result["status"] == "live_not_allowed"


def test_alpaca_client_places_confirmed_live_order() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url in {
            httpx.URL("https://api.alpaca.markets/v2/orders"),
            httpx.URL("https://api.alpaca.markets/v2/orders/live-order"),
        }
        return httpx.Response(200, json={"id": "live-order", "status": "accepted"})

    client = AlpacaTradingClient(
        AlpacaConfig(api_key="key", secret_key="secret", paper=False, allow_live=True),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.place_order(AlpacaOrderRequest(symbol="AAPL", side="sell", quantity=1), confirm="LIVE_ALPACA_ORDER")

    assert result["ok"] is True
    assert result["mode"] == "live"
    assert result["broker_order_id"] == "live-order"


def test_alpaca_client_returns_validation_errors() -> None:
    client = AlpacaTradingClient(AlpacaConfig(api_key="key", secret_key="secret"))

    assert client.place_order(AlpacaOrderRequest(symbol="BAD*", side="buy", notional=1))["status"] == "invalid_symbol"
    assert client.place_order(AlpacaOrderRequest(symbol="MSFT", side="hold", notional=1))["status"] == "invalid_side"
    assert client.place_order(AlpacaOrderRequest(symbol="MSFT", side="buy", quantity=1, notional=1))["status"] == "invalid_size"
    assert client.place_order(AlpacaOrderRequest(symbol="MSFT", side="buy", notional=0))["status"] == "invalid_notional"
    assert client.place_order(AlpacaOrderRequest(symbol="MSFT", side="buy", notional=1, order_type="moon"))["status"] == "invalid_order_type"


def test_alpaca_client_reports_rejected_and_network_errors() -> None:
    rejected = AlpacaTradingClient(
        AlpacaConfig(api_key="key", secret_key="secret"),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(422, headers={"X-Request-ID": "reject-1"}, json={"message": "bad order"}))),
    )
    network = AlpacaTradingClient(
        AlpacaConfig(api_key="key", secret_key="secret"),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: (_ for _ in ()).throw(httpx.ConnectError("offline")))),
    )

    rejected_result = rejected.place_order(AlpacaOrderRequest(symbol="MSFT", side="buy", notional=1))
    network_result = network.place_order(AlpacaOrderRequest(symbol="MSFT", side="buy", notional=1))

    assert rejected_result["status"] == "rejected"
    assert rejected_result["request_id"] == "reject-1"
    assert network_result["status"] == "network_error"


def test_alpaca_client_reports_rate_limit_rejection() -> None:
    client = AlpacaTradingClient(
        AlpacaConfig(api_key="key", secret_key="secret"),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(429, headers={"X-Request-ID": "rate-1"}, json={"code": 42910000, "message": "rate limit exceeded"}))),
    )

    result = client.place_order(AlpacaOrderRequest(symbol="MSFT", side="buy", notional=1))

    assert result["ok"] is False
    assert result["status"] == "rate_limited"
    assert result["request_id"] == "rate-1"
    assert "rate limit" in result["detail"].lower()


def test_alpaca_client_reports_missing_config_without_calling_network() -> None:
    client = AlpacaTradingClient(AlpacaConfig())

    result = client.place_order(AlpacaOrderRequest(symbol="MSFT", side="buy", notional=25))

    assert result["ok"] is False
    assert result["status"] == "not_configured"
