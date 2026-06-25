from __future__ import annotations

import json
from pathlib import Path

from scripts.robinhood_agentic import AGENTIC_TRADING_MCP_URL
from scripts.robinhood_stock_connector import (
    LIVE_STOCK_CONFIRMATION,
    RobinhoodStockOrderConnector,
    StockConnectorConfig,
    StockOrderRequest,
)


def _configured_codex(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(f'[mcp_servers.robinhood-trading]\nurl = "{AGENTIC_TRADING_MCP_URL}"\n')
    return path


def test_stock_connector_blocks_when_disabled(tmp_path: Path) -> None:
    config = StockConnectorConfig(codex_config_path=_configured_codex(tmp_path))
    connector = RobinhoodStockOrderConnector(config)

    payload = connector.place_order(StockOrderRequest("MSFT", "BUY", 1), confirm=LIVE_STOCK_CONFIRMATION)

    assert payload["ok"] is False
    assert payload["status"] == "connector_disabled"
    assert payload["review_only"] is True


def test_stock_connector_requires_live_modes_and_confirmation(tmp_path: Path) -> None:
    config = StockConnectorConfig(connector_mode="codex_mcp", stock_trading_mode="review", app_trading_mode="live", codex_config_path=_configured_codex(tmp_path))
    connector = RobinhoodStockOrderConnector(config)

    stock_mode = connector.place_order(StockOrderRequest("MSFT", "BUY", 1), confirm=LIVE_STOCK_CONFIRMATION)
    confirmation = RobinhoodStockOrderConnector(
        StockConnectorConfig(connector_mode="codex_mcp", stock_trading_mode="live", app_trading_mode="live", codex_config_path=_configured_codex(tmp_path))
    ).place_order(StockOrderRequest("MSFT", "BUY", 1), confirm="")

    assert stock_mode["status"] == "stock_mode_not_live"
    assert confirmation["status"] == "confirmation_required"


def test_stock_connector_invokes_codex_bridge_when_live(tmp_path: Path, monkeypatch) -> None:
    calls = []

    def fake_run(args, text, capture_output, check, timeout):
        calls.append(args)
        output_path = Path(args[args.index("--output-last-message") + 1])
        output_path.write_text(json.dumps({"ok": True, "status": "submitted", "broker_order_id": "order-1", "message": "submitted"}))
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr("scripts.robinhood_stock_connector.subprocess.run", fake_run)
    config = StockConnectorConfig(
        connector_mode="codex_mcp",
        stock_trading_mode="live",
        app_trading_mode="live",
        codex_config_path=_configured_codex(tmp_path),
        codex_path="codex-test",
    )
    connector = RobinhoodStockOrderConnector(config)

    payload = connector.place_order(StockOrderRequest("MSFT", "BUY", 2), confirm=LIVE_STOCK_CONFIRMATION)

    assert payload["ok"] is True
    assert payload["status"] == "submitted"
    assert payload["broker_order_id"] == "order-1"
    assert payload["review_only"] is False
    assert calls[0][0] == "codex-test"
    assert "--skip-git-repo-check" in calls[0]
    assert "--output-schema" in calls[0]


def test_stock_connector_returns_safe_failure_detail(tmp_path: Path, monkeypatch) -> None:
    def fake_run(args, text, capture_output, check, timeout):
        return type("Result", (), {"returncode": 2, "stdout": "oauth failed token=abc123", "stderr": "not logged in api_key=key123"})()

    monkeypatch.setattr("scripts.robinhood_stock_connector.subprocess.run", fake_run)
    config = StockConnectorConfig(
        connector_mode="codex_mcp",
        stock_trading_mode="live",
        app_trading_mode="live",
        codex_config_path=_configured_codex(tmp_path),
        codex_path="codex-test",
    )
    connector = RobinhoodStockOrderConnector(config)

    payload = connector.place_order(StockOrderRequest("MSFT", "SELL", 1), confirm=LIVE_STOCK_CONFIRMATION)

    assert payload["ok"] is False
    assert payload["status"] == "connector_failed"
    assert payload["returncode"] == 2
    assert "not logged in" in payload["detail"]
    assert "abc123" not in json.dumps(payload)
    assert "key123" not in json.dumps(payload)


def test_stock_connector_diagnose_reports_current_gates(tmp_path: Path, monkeypatch) -> None:
    def fake_run(args, text, capture_output, check, timeout):
        return type("Result", (), {"returncode": 0, "stdout": "Name Status\ncontext7 enabled\nrobinhood-trading enabled OAuth", "stderr": ""})()

    monkeypatch.setattr("scripts.robinhood_stock_connector.subprocess.run", fake_run)
    config = StockConnectorConfig(
        connector_mode="disabled",
        stock_trading_mode="paper",
        app_trading_mode="paper",
        codex_config_path=_configured_codex(tmp_path),
        codex_path="codex-test",
    )
    connector = RobinhoodStockOrderConnector(config)

    payload = connector.diagnose()

    assert payload["ok"] is False
    assert payload["status"] == "blocked"
    assert payload["checks"][0]["status"] == "blocked"
    assert payload["codex_mcp_list"]["ok"] is True
    assert "robinhood-trading" in payload["codex_mcp_list"]["output"]
    assert "context7" not in payload["codex_mcp_list"]["output"]


def test_project_env_file_controls_ui_stock_settings(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BONEHAWK_STOCK_ORDER_CONNECTOR", "disabled")
    env_file = tmp_path / ".env"
    env_file.write_text("BONEHAWK_STOCK_ORDER_CONNECTOR=codex_mcp\nBONEHAWK_STOCK_TRADING_MODE=live\nTRADING_MODE=live\n")

    config = StockConnectorConfig.from_project_env(env_file, codex_config_path=_configured_codex(tmp_path))

    assert config.connector_mode == "codex_mcp"
    assert config.stock_trading_mode == "live"
    assert config.app_trading_mode == "live"
