from __future__ import annotations

from scripts.robinhood import RobinhoodConfig
from scripts.robinhood_integration import mask_value, robinhood_snapshot


class FakeRobinhoodClient:
    def account(self):
        return {"results": [{"account_number": "123456789", "status": "active"}]}

    def holdings(self):
        return {"results": [{"asset_code": "BTC", "total_quantity": "0.01", "quantity_available_for_trading": "0.009"}]}

    def orders(self, **filters):
        return {"results": [{"id": "order-abcdef123456", "symbol": "BTC-USD", "side": "buy", "type": "market", "state": "open"}]}

    def quote(self, *symbols):
        return {"results": [{"symbol": "BTC-USD", "bid": "62500", "ask": "62510"}]}


def test_mask_value_masks_sensitive_identifiers() -> None:
    assert mask_value("123456789") == "*****6789"
    assert mask_value(None) == "missing"


def test_robinhood_snapshot_summarizes_read_only_connection() -> None:
    config = RobinhoodConfig("key", "private", "123456789", "v2", "paper")

    payload = robinhood_snapshot(config, FakeRobinhoodClient())

    assert payload["status"] == "connected"
    assert payload["account"]["account_number"] == "*****6789"
    assert payload["holdings"][0]["symbol"] == "BTC-USD"
    assert payload["orders"][0]["id"].endswith("3456")
    assert "abcdef" not in payload["orders"][0]["id"]
    assert payload["quotes"][0]["ask"] == "62510"
    assert payload["capabilities"]["stock_trading"] == "not_supported_by_crypto_api"


def test_robinhood_snapshot_handles_missing_credentials() -> None:
    config = RobinhoodConfig("", "", None, "v2", "paper")

    payload = robinhood_snapshot(config, None)

    assert payload["status"] == "not_configured"


def test_robinhood_snapshot_reports_configured_but_uninitialized_client() -> None:
    config = RobinhoodConfig("key", "private", "acct", "v2", "paper")

    payload = robinhood_snapshot(config, None)

    assert payload["status"] == "error"


def test_robinhood_snapshot_hides_runtime_error_detail() -> None:
    class BrokenClient:
        def account(self):
            raise RuntimeError("secret backend detail")

    config = RobinhoodConfig("key", "private", "acct", "v2", "paper")

    payload = robinhood_snapshot(config, BrokenClient())

    assert payload["status"] == "error"
    assert "secret backend detail" not in payload["message"]
