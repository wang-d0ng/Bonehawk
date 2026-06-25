from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from scripts.robinhood import (
    RobinhoodConfig,
    RobinhoodCryptoClient,
    RobinhoodError,
    build_parser,
    canonical_body,
    cmd_buy,
    cmd_account_number,
    cmd_cancel,
    cmd_cancel_all,
    cmd_close,
    cmd_orders,
    cmd_position,
    cmd_quote,
    cmd_sell,
    cmd_stop,
    decimal_floor,
    extract_holding_quantity,
    main,
    query,
)


EXAMPLE_PRIVATE_KEY = "xQnTJVeQLmw1/Mg2YimEViSpw/SdJcgNXZ5kQkAXNPU="
EXAMPLE_API_KEY = "rh-api-6148effc-c0b1-486c-8940-a1d099456be6"


def config(trading_mode: str = "paper") -> RobinhoodConfig:
    return RobinhoodConfig(
        api_key=EXAMPLE_API_KEY,
        private_key_base64=EXAMPLE_PRIVATE_KEY,
        account_number="acct-1",
        api_version="v2",
        trading_mode=trading_mode,
    )


def test_authorization_signature_matches_robinhood_doc_example() -> None:
    client = RobinhoodCryptoClient(config(), clock=lambda: 1698708981)
    body = str(
        {
            "client_order_id": "131de903-5a9c-4260-abc1-28d562a5dcf0",
            "side": "buy",
            "symbol": "BTC-USD",
            "type": "market",
            "market_order_config": {"asset_quantity": "0.1"},
        }
    )

    headers = client.authorization_headers("POST", "/api/v1/crypto/trading/orders/", body)

    assert headers["x-api-key"] == EXAMPLE_API_KEY
    assert headers["x-timestamp"] == "1698708981"
    assert headers["x-signature"] == "q/nEtxp/P2Or3hph3KejBqnw5o9qeuQ+hYRnB56FaHbjDsNUY9KhB1asMxohDnzdVFSD7StaTqjSd9U9HvaRAw=="


def test_query_repeats_list_params_and_omits_none() -> None:
    assert query({"symbol": ["BTC-USD", "ETH-USD"], "cursor": None}) == "?symbol=BTC-USD&symbol=ETH-USD"


def test_canonical_body_is_compact_json_for_signing() -> None:
    assert canonical_body({"side": "buy", "market_order_config": {"asset_quantity": "0.1"}}) == (
        '{"side":"buy","market_order_config":{"asset_quantity":"0.1"}}'
    )


def test_request_sends_signed_body_and_parses_response() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    client = RobinhoodCryptoClient(config(), http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    result = client.request("POST", "/api/v2/crypto/trading/orders/?account_number=acct-1", {"side": "buy"})

    assert result == {"ok": True}
    assert requests[0].headers["x-api-key"] == EXAMPLE_API_KEY
    assert json.loads(requests[0].content.decode()) == {"side": "buy"}


def test_account_number_auto_discovers_from_accounts_response(capsys: pytest.CaptureFixture[str]) -> None:
    client = RobinhoodCryptoClient(
        RobinhoodConfig(EXAMPLE_API_KEY, EXAMPLE_PRIVATE_KEY, "wrong-env-value", "v2", "paper"),
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"results": [{"account_number": "crypto-acct-123"}]})
            )
        ),
    )

    cmd_account_number(client, type("Args", (), {})())

    assert capsys.readouterr().out.strip() == "crypto-acct-123"


def test_request_raises_safe_error_without_leaking_headers() -> None:
    client = RobinhoodCryptoClient(
        config(),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(403, json={"detail": "nope"}))),
    )

    with pytest.raises(RobinhoodError, match="Robinhood API error 403"):
        client.account()


def test_missing_credentials_are_rejected() -> None:
    with pytest.raises(RobinhoodError, match="ROBINHOOD_API_KEY"):
        RobinhoodCryptoClient(RobinhoodConfig("", "", None, "v2", "paper"))


def test_live_trading_guard_blocks_order_commands_in_paper_mode() -> None:
    client = RobinhoodCryptoClient(config(trading_mode="paper"))

    with pytest.raises(RobinhoodError, match="TRADING_MODE"):
        client.place_order("buy", "market", "BTC-USD", {"asset_quantity": "0.1"})


def test_place_order_uses_v2_account_scoped_path_when_live() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "order-1", "state": "open"})

    client = RobinhoodCryptoClient(
        config(trading_mode="live"),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.place_order("buy", "market", "BTC-USD", {"asset_quantity": Decimal("0.001")})

    assert result["id"] == "order-1"
    assert requests[0].url.path == "/api/v2/crypto/trading/orders/"
    assert requests[0].url.params["account_number"] == "acct-1"
    assert json.loads(requests[0].content.decode())["market_order_config"]["asset_quantity"] == "0.001"


def test_decimal_floor_and_holding_extraction() -> None:
    payload = {"results": [{"asset_code": "BTC", "quantity_available_for_trading": "0.123456789"}]}

    assert extract_holding_quantity(payload, "BTC") == Decimal("0.123456789")
    assert decimal_floor(Decimal("0.123456789"), "0.00000001") == Decimal("0.12345678")


def test_buy_by_usd_uses_quote_to_compute_asset_quantity() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "best_bid_ask" in str(request.url):
            return httpx.Response(200, json={"results": [{"symbol": "BTC-USD", "ask": "50000", "bid": "49990"}]})
        return httpx.Response(200, json={"id": "buy-1"})

    client = RobinhoodCryptoClient(
        config(trading_mode="live"),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    args = type("Args", (), {"usd": Decimal("50"), "base": None, "symbol": "BTC-USD"})()

    cmd_buy(client, args)

    body = json.loads(requests[-1].content.decode())
    assert body["market_order_config"]["asset_quantity"] == "0.00100000"


def test_read_only_commands_call_expected_paths(capsys: pytest.CaptureFixture[str]) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"results": [{"asset_code": "BTC", "bid": "1", "ask": "2"}]})

    client = RobinhoodCryptoClient(config(), http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    cmd_position(client, type("Args", (), {})())
    cmd_quote(client, type("Args", (), {"symbol": "BTC-USD"})())
    cmd_orders(client, type("Args", (), {"status": "open"})())

    assert "/api/v2/crypto/trading/holdings/" in paths
    assert "/api/v2/crypto/marketdata/best_bid_ask/" in paths
    assert "/api/v2/crypto/trading/orders/" in paths
    assert "BTC" in capsys.readouterr().out


def test_sell_by_percent_places_market_sell() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "holdings" in str(request.url):
            return httpx.Response(200, json={"results": [{"asset_code": "BTC", "quantity_available_for_trading": "0.02"}]})
        return httpx.Response(200, json={"id": "sell-1"})

    client = RobinhoodCryptoClient(
        config(trading_mode="live"),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    cmd_sell(client, type("Args", (), {"pct": Decimal("25"), "base": None, "symbol": "BTC-USD"})())

    body = json.loads(requests[-1].content.decode())
    assert body["side"] == "sell"
    assert body["market_order_config"]["asset_quantity"] == "0.00500000"


def test_cancel_and_cancel_all_use_cancel_endpoint() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/orders/"):
            return httpx.Response(200, json={"results": [{"id": "one"}, {"id": "two"}]})
        return httpx.Response(200, json={"success": True})

    client = RobinhoodCryptoClient(
        config(trading_mode="live"),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    cmd_cancel(client, type("Args", (), {"order_id": "single"})())
    cmd_cancel_all(client, type("Args", (), {})())

    assert "/api/v2/crypto/trading/orders/single/cancel/" in paths
    assert "/api/v2/crypto/trading/orders/one/cancel/" in paths
    assert "/api/v2/crypto/trading/orders/two/cancel/" in paths


def test_close_is_noop_without_btc_holding(capsys: pytest.CaptureFixture[str]) -> None:
    client = RobinhoodCryptoClient(
        config(trading_mode="live"),
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"results": []}))
        ),
    )

    cmd_close(client, type("Args", (), {"symbol": "BTC-USD"})())

    assert "no BTC available" in capsys.readouterr().out


def test_parser_accepts_expected_commands() -> None:
    parser = build_parser()

    args = parser.parse_args(["stop", "--base", "0.1", "--stop-price", "60000", "--limit", "59700"])

    assert args.cmd == "stop"
    assert args.symbol == "BTC-USD"


def test_main_prints_clean_error_for_missing_credentials(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("scripts.robinhood.load_dotenv", lambda: None)
    monkeypatch.delenv("ROBINHOOD_API_KEY", raising=False)
    monkeypatch.delenv("ROBINHOOD_PRIVATE_KEY_BASE64", raising=False)
    monkeypatch.setattr("sys.argv", ["robinhood.py", "account"])

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 1
    assert "Missing required env var" in capsys.readouterr().err


def test_stop_rejects_limit_above_stop_for_sell_stop_limit() -> None:
    client = RobinhoodCryptoClient(config(trading_mode="live"))
    args = type("Args", (), {"base": Decimal("0.01"), "stop_price": Decimal("60000"), "limit": Decimal("61000"), "symbol": "BTC-USD"})()

    with pytest.raises(RobinhoodError, match="limit"):
        cmd_stop(client, args)
