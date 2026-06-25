from __future__ import annotations

import json
import re
from pathlib import Path

from scripts.dashboard import DashboardService, HTML, json_response
from scripts.decision_log import record_decisions
from scripts.market_intel import Watchlist


class FakeIntelClient:
    def __init__(self) -> None:
        self.quote_client = FakeQuoteClient()

    def snapshot(self, watchlist: Watchlist) -> dict:
        return {
            "symbols": watchlist.symbols,
            "news": [
                {"symbol": symbol, "title": f"{symbol} shares rise {index}", "url": "https://example.com", "published": ""}
                for symbol in watchlist.symbols
                for index in range(3)
            ],
            "insider_filings": [],
            "risk_flags": ["test flag"],
            "capabilities": {"stock_trading": "not connected"},
        }


class FakeQuoteClient:
    def get_quotes(self, symbols):
        from scripts.quotes import Quote

        return {symbol: Quote(symbol, 120, previous_close=118) for symbol in symbols}

    def get_histories(self, symbols):
        from scripts.quotes import PriceHistory

        return {symbol: PriceHistory(symbol, closes=[100, 101, 102, 103, 104, 105, 106, 108, 110, 120], volumes=[100] * 9 + [180]) for symbol in symbols}

    def get_stock_chart(self, symbol, range_key="1d"):
        from scripts.quotes import ChartPoint, StockChart

        points = [
            ChartPoint(timestamp=1000, close=118, volume=1000),
            ChartPoint(timestamp=1060, close=119, volume=1100),
            ChartPoint(timestamp=1120, close=120, volume=1200),
        ]
        return StockChart(symbol=symbol.upper(), range_key=range_key, interval="5m", latest_price=120, points=points)


class FakeStockConnector:
    def __init__(self) -> None:
        self.calls = []

    def snapshot(self):
        return {"status": "callable", "connector_mode": "test", "confirmation_phrase": "LIVE_STOCK_ORDER"}

    def diagnose(self):
        return {"ok": True, "status": "ready", "checks": [], "codex_mcp_list": {"ok": True, "output": "test"}}

    def place_order(self, request, confirm: str):
        self.calls.append((request, confirm))
        return {
            "ok": True,
            "status": "submitted",
            "symbol": request.symbol,
            "side": request.side,
            "quantity": request.quantity,
            "broker_order_id": "test-order",
            "message": "submitted",
            "review_only": False,
        }


class FakeAlpacaClient:
    def __init__(self) -> None:
        self.orders = []

    def snapshot(self):
        return {"status": "connected", "api_key": "set", "secret_key": "set", "paper": True}

    def place_order(self, request, confirm: str = ""):
        self.orders.append((request, confirm))
        return {
            "ok": True,
            "status": "submitted",
            "broker": "alpaca",
            "mode": "paper",
            "symbol": request.symbol,
            "side": request.side.upper(),
            "quantity": request.quantity,
            "notional": request.notional,
            "broker_order_id": "alpaca-paper-order",
            "broker_status": "accepted",
            "message": "Alpaca paper order submitted.",
            "review_only": False,
        }


def test_json_response_encodes_payload() -> None:
    status, headers, body = json_response({"ok": True})

    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body.decode()) == {"ok": True}


def test_dashboard_service_returns_status_without_secrets(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("ROBINHOOD_API_KEY=secret\nTRADING_MODE=paper\nBONEHAWK_STOCK_TRADING_MODE=review\nBONEHAWK_STOCK_ORDER_CONNECTOR=disabled\n")
    service = DashboardService(root=tmp_path, intel_client=FakeIntelClient())

    status = service.status()

    assert status["env"]["ROBINHOOD_API_KEY"] == "set"
    assert status["env"]["BONEHAWK_STOCK_TRADING_MODE"] == "review"
    assert status["env"]["BONEHAWK_STOCK_ORDER_CONNECTOR"] == "disabled"
    assert "secret" not in json.dumps(status)


def test_dashboard_service_setup_status_requires_first_run_when_alpaca_missing(tmp_path: Path) -> None:
    service = DashboardService(root=tmp_path, intel_client=FakeIntelClient())

    payload = service.setup_status()

    assert payload["required"] is True
    assert payload["complete"] is False
    assert payload["steps"]["alpaca"]["status"] == "missing"
    assert "secret-secret" not in json.dumps(payload)


def test_dashboard_service_apply_setup_writes_env_and_autopilot_config_without_leaking_values(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("ALPACA_SECRET_KEY=existing-secret\nTELEGRAM_BOT_TOKEN=existing-telegram\n")
    service = DashboardService(root=tmp_path, intel_client=FakeIntelClient())

    payload = service.apply_setup(
        {
            "alpaca_api_key": "paper-key",
            "alpaca_secret_key": "",
            "alpaca_paper": True,
            "telegram_bot_token": "",
            "allowed_chat_ids": "123,456",
            "autopilot_enabled": True,
            "max_trade_usd": 35,
            "max_open_positions": 4,
        }
    )

    env_text = env_file.read_text()
    assert payload["ok"] is True
    assert payload["setup"]["complete"] is True
    assert "paper-key" not in json.dumps(payload)
    assert "existing-secret" not in json.dumps(payload)
    assert "ALPACA_API_KEY=paper-key" in env_text
    assert "ALPACA_SECRET_KEY=existing-secret" in env_text
    assert "TELEGRAM_BOT_TOKEN=existing-telegram" in env_text
    assert "ALPACA_PAPER=true" in env_text
    assert "ALPACA_ALLOW_LIVE=false" in env_text
    assert "BONEHAWK_SETUP_COMPLETE=true" in env_text
    assert (tmp_path / "config" / "autopilot.json").exists()


def test_dashboard_service_apply_setup_rejects_invalid_risk_numbers(tmp_path: Path) -> None:
    service = DashboardService(root=tmp_path, intel_client=FakeIntelClient())

    payload = service.apply_setup({"alpaca_api_key": "key", "alpaca_secret_key": "secret", "max_trade_usd": "nope"})

    assert payload["ok"] is False
    assert payload["status"] == "invalid_setup"


def test_dashboard_service_updates_trading_mode_preserving_env_values(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("ROBINHOOD_API_KEY=secret\nTRADING_MODE=paper\nTELEGRAM_BOT_TOKEN=telegram-secret\n")
    service = DashboardService(root=tmp_path, intel_client=FakeIntelClient())

    payload = service.set_trading_mode("live", confirm="LIVE")

    assert payload["ok"] is True
    assert payload["mode"] == "live"
    assert "secret" not in json.dumps(payload)
    assert "ROBINHOOD_API_KEY=secret" in env_file.read_text()
    assert "TELEGRAM_BOT_TOKEN=telegram-secret" in env_file.read_text()
    assert "TRADING_MODE=live" in env_file.read_text()


def test_dashboard_service_blocks_live_mode_without_confirmation(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("TRADING_MODE=paper\n")
    service = DashboardService(root=tmp_path, intel_client=FakeIntelClient())

    payload = service.set_trading_mode("live")

    assert payload["ok"] is False
    assert payload["status"] == "confirmation_required"
    assert payload["mode"] == "paper"
    assert "TRADING_MODE=paper" in (tmp_path / ".env").read_text()


def test_dashboard_service_rejects_invalid_trading_mode(tmp_path: Path) -> None:
    service = DashboardService(root=tmp_path, intel_client=FakeIntelClient())

    payload = service.set_trading_mode("turbo")

    assert payload["ok"] is False
    assert payload["status"] == "invalid_mode"


def test_dashboard_service_updates_stock_setting_with_confirmation(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("ROBINHOOD_API_KEY=secret\nBONEHAWK_STOCK_ORDER_CONNECTOR=disabled\n")
    service = DashboardService(root=tmp_path, intel_client=FakeIntelClient())

    payload = service.set_stock_setting("BONEHAWK_STOCK_ORDER_CONNECTOR", "codex_mcp", confirm="ENABLE_STOCK_CONNECTOR")

    assert payload["ok"] is True
    assert payload["setting"] == "BONEHAWK_STOCK_ORDER_CONNECTOR"
    assert payload["value"] == "codex_mcp"
    assert "ROBINHOOD_API_KEY=secret" in env_file.read_text()
    assert "BONEHAWK_STOCK_ORDER_CONNECTOR=codex_mcp" in env_file.read_text()


def test_dashboard_service_updates_ui_theme(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("ROBINHOOD_API_KEY=secret\nBONEHAWK_UI_THEME=classic\n")
    service = DashboardService(root=tmp_path, intel_client=FakeIntelClient())

    payload = service.set_ui_theme("arcade")

    assert payload["ok"] is True
    assert payload["theme"] == "arcade"
    assert "ROBINHOOD_API_KEY=secret" in env_file.read_text()
    assert "BONEHAWK_UI_THEME=arcade" in env_file.read_text()


def test_dashboard_service_rejects_invalid_ui_theme(tmp_path: Path) -> None:
    service = DashboardService(root=tmp_path, intel_client=FakeIntelClient())

    payload = service.set_ui_theme("rainbow")

    assert payload["ok"] is False
    assert payload["status"] == "invalid_theme"


def test_dashboard_status_defaults_invalid_ui_theme_to_arcade(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("BONEHAWK_UI_THEME=rainbow\n")
    service = DashboardService(root=tmp_path, intel_client=FakeIntelClient())

    payload = service.status()

    assert payload["ui_theme"] == "arcade"


def test_dashboard_service_blocks_live_stock_setting_without_confirmation(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("BONEHAWK_STOCK_TRADING_MODE=review\n")
    service = DashboardService(root=tmp_path, intel_client=FakeIntelClient())

    payload = service.set_stock_setting("BONEHAWK_STOCK_TRADING_MODE", "live")

    assert payload["ok"] is False
    assert payload["status"] == "confirmation_required"
    assert "BONEHAWK_STOCK_TRADING_MODE=review" in (tmp_path / ".env").read_text()


def test_dashboard_service_rejects_unknown_stock_setting(tmp_path: Path) -> None:
    service = DashboardService(root=tmp_path, intel_client=FakeIntelClient())

    payload = service.set_stock_setting("ROBINHOOD_API_KEY", "nope")

    assert payload["ok"] is False
    assert payload["status"] == "invalid_setting"


def test_dashboard_service_command_catalog_exposes_readme_actions(tmp_path: Path) -> None:
    service = DashboardService(root=tmp_path, intel_client=FakeIntelClient())

    payload = service.command_catalog()
    ids = {item["id"] for item in payload["commands"]}

    assert {"setup-venv", "install-requirements", "robinhood-account", "robinhood-buy-usd", "daily-loop"}.issubset(ids)
    assert {"desktop-run", "desktop-build"}.issubset(ids)
    assert all("argv" not in item for item in payload["commands"])


def test_dashboard_service_runs_allowlisted_command_and_redacts_output(tmp_path: Path, monkeypatch) -> None:
    service = DashboardService(root=tmp_path, intel_client=FakeIntelClient())
    calls = []

    def fake_run(args, cwd, text, capture_output, check, timeout):
        calls.append(args)
        return type("Result", (), {"returncode": 0, "stdout": '{"account_number":"123456789","ok":true}', "stderr": ""})()

    monkeypatch.setattr("scripts.command_center.subprocess.run", fake_run)

    payload = service.run_command("robinhood-account")

    assert payload["ok"] is True
    assert calls[0][-1] == "account"
    assert "123456789" not in payload["stdout"]
    assert "account_number" in payload["stdout"]


def test_dashboard_service_blocks_live_command_without_confirmation(tmp_path: Path) -> None:
    service = DashboardService(root=tmp_path, intel_client=FakeIntelClient())

    payload = service.run_command("robinhood-buy-usd", inputs={"usd": "50"})

    assert payload["ok"] is False
    assert payload["status"] == "confirmation_required"


def test_dashboard_service_rejects_unknown_command(tmp_path: Path) -> None:
    service = DashboardService(root=tmp_path, intel_client=FakeIntelClient())

    payload = service.run_command("rm-rf")

    assert payload["ok"] is False
    assert payload["status"] == "unknown_command"


def test_dashboard_service_copy_env_command_does_not_overwrite_existing_secret(tmp_path: Path) -> None:
    (tmp_path / "env.template").write_text("TRADING_MODE=paper\n")
    (tmp_path / ".env").write_text("ROBINHOOD_API_KEY=secret\n")
    service = DashboardService(root=tmp_path, intel_client=FakeIntelClient())

    payload = service.run_command("copy-env")

    assert payload["ok"] is True
    assert payload["status"] == "skipped"
    assert (tmp_path / ".env").read_text() == "ROBINHOOD_API_KEY=secret\n"


def test_dashboard_service_robinhood_summary_without_secrets(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("ROBINHOOD_API_KEY=secret\nROBINHOOD_PRIVATE_KEY_BASE64=secret\nTRADING_MODE=paper\n")
    service = DashboardService(root=tmp_path, intel_client=FakeIntelClient())

    payload = service.robinhood()

    assert payload["status"] in {"error", "not_configured"}
    assert "secret" not in json.dumps(payload)


def test_dashboard_service_agentic_trading_reads_codex_config(tmp_path: Path) -> None:
    codex_config = tmp_path / "codex.toml"
    codex_config.write_text('[mcp_servers.robinhood-trading]\nurl = "https://agent.robinhood.com/mcp/trading"\n')
    service = DashboardService(root=tmp_path, intel_client=FakeIntelClient(), codex_config_path=codex_config)

    payload = service.agentic_trading()

    assert payload["status"] == "configured"
    assert payload["codex_mcp_configured"] is True
    assert payload["stock_trading_mode"] == "review"


def test_dashboard_service_market_intel_uses_watchlist(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "watchlist.json").write_text(json.dumps({"symbols": ["AAPL"]}))
    service = DashboardService(root=tmp_path, intel_client=FakeIntelClient())

    payload = service.market_intel()

    assert payload["symbols"] == ["AAPL"]
    assert payload["risk_flags"] == ["test flag"]


def test_dashboard_service_scanner_uses_market_snapshot(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "watchlist.json").write_text(json.dumps({"symbols": ["AAPL"], "aliases": {"AAPL": ["APPLE INC"]}}))
    (config / "market_universe.json").write_text(json.dumps({"symbols": ["MSFT"], "max_scan_symbols": 1}))
    service = DashboardService(root=tmp_path, intel_client=FakeIntelClient())

    payload = service.scanner()

    assert payload["summary"]["symbols_scanned"] == 2
    assert payload["scans"][0]["symbol"] == "AAPL"


def test_dashboard_service_stocks_reports_available_universe(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "market_universe.json").write_text(json.dumps({"symbols": ["aapl", "msft", "nvda"], "max_scan_symbols": 2}))
    service = DashboardService(root=tmp_path, intel_client=FakeIntelClient())

    payload = service.stocks()

    assert payload["status"] == "loaded"
    assert payload["total_symbols"] == 3
    assert payload["scan_symbols"] == 2
    assert payload["sample_symbols"] == ["AAPL", "MSFT"]
    assert payload["execution"]["robinhood_crypto_api"] == "stocks_not_supported"


def test_dashboard_service_growth_candidates_returns_review_only_rankings(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "watchlist.json").write_text(json.dumps({"symbols": ["ABCD"], "positions": [], "risk": {}}))
    (config / "market_universe.json").write_text(json.dumps({"symbols": ["ABCD"], "max_scan_symbols": 1}))
    service = DashboardService(root=tmp_path, intel_client=FakeIntelClient())

    payload = service.growth_candidates()

    assert payload["summary"]["symbols_scanned"] == 1
    assert payload["candidates"][0]["symbol"] == "ABCD"
    assert payload["candidates"][0]["review_only"] is True
    assert "No live order" in payload["notice"]


def test_dashboard_service_stock_chart_returns_range_payload(tmp_path: Path) -> None:
    service = DashboardService(root=tmp_path, intel_client=FakeIntelClient())

    payload = service.stock_chart("msft", "1w")

    assert payload["symbol"] == "MSFT"
    assert payload["range"] == "1w"
    assert payload["points"]
    assert payload["summary"]["point_count"] == len(payload["points"])
    assert payload["review_only"] is True


def test_dashboard_service_stock_chart_rejects_bad_symbol(tmp_path: Path) -> None:
    service = DashboardService(root=tmp_path, intel_client=FakeIntelClient())

    payload = service.stock_chart("../secret", "1d")

    assert payload["ok"] is False
    assert payload["status"] == "invalid_symbol"


def test_dashboard_service_records_stock_order_intent_without_live_order(tmp_path: Path) -> None:
    service = DashboardService(root=tmp_path, intel_client=FakeIntelClient())

    payload = service.stock_order_intent("msft", "buy", "2.5")

    assert payload["ok"] is True
    assert payload["symbol"] == "MSFT"
    assert payload["side"] == "BUY"
    assert payload["quantity"] == 2.5
    assert payload["review_only"] is True
    assert "No live stock order" in payload["message"]
    assert (tmp_path / "logs" / "decision_log.jsonl").exists()


def test_dashboard_service_tickets_lists_stock_order_tickets(tmp_path: Path) -> None:
    service = DashboardService(root=tmp_path, intel_client=FakeIntelClient(), stock_connector=FakeStockConnector())

    service.stock_order_intent("msft", "buy", "2")
    service.stock_order("aapl", "sell", "1", confirm="LIVE_STOCK_ORDER")
    payload = service.tickets()

    assert payload["summary"]["count"] == 2
    assert payload["tickets"][0]["symbol"] == "AAPL"
    assert payload["tickets"][0]["quantity"] == 1
    assert payload["tickets"][0]["status"] == "submitted"
    assert payload["tickets"][1]["symbol"] == "MSFT"
    assert payload["tickets"][1]["status"] == "recorded"


def test_dashboard_service_tickets_supports_legacy_signal_quantity(tmp_path: Path) -> None:
    log_path = tmp_path / "logs" / "decision_log.jsonl"
    record_decisions(
        log_path,
        "stock_order_intent",
        [{"symbol": "TSLA", "action": "SELL_INTENT", "reason": "legacy row", "signals": ["quantity 4"]}],
    )
    service = DashboardService(root=tmp_path, intel_client=FakeIntelClient())

    payload = service.tickets()

    assert payload["tickets"][0]["quantity"] == 4
    assert payload["tickets"][0]["side"] == "SELL"
    assert payload["tickets"][0]["status"] == "recorded"


def test_dashboard_service_stock_order_records_blocked_attempt_ticket(tmp_path: Path) -> None:
    service = DashboardService(root=tmp_path, intel_client=FakeIntelClient())

    result = service.stock_order("msft", "buy", "1", confirm="LIVE_STOCK_ORDER")
    tickets = service.tickets()

    assert result["ok"] is False
    assert tickets["tickets"][0]["source"] == "stock_order_attempt"
    assert tickets["tickets"][0]["status"] == "connector_disabled"


def test_dashboard_service_rejects_invalid_stock_order_intent(tmp_path: Path) -> None:
    service = DashboardService(root=tmp_path, intel_client=FakeIntelClient())

    bad_side = service.stock_order_intent("MSFT", "moon", "1")
    bad_quantity = service.stock_order_intent("MSFT", "buy", "0")

    assert bad_side["ok"] is False
    assert bad_side["status"] == "invalid_side"
    assert bad_quantity["ok"] is False
    assert bad_quantity["status"] == "invalid_quantity"


def test_dashboard_service_stock_order_calls_connector(tmp_path: Path) -> None:
    connector = FakeStockConnector()
    service = DashboardService(root=tmp_path, intel_client=FakeIntelClient(), stock_connector=connector)

    payload = service.stock_order("msft", "buy", "3", confirm="LIVE_STOCK_ORDER")

    assert payload["ok"] is True
    assert payload["status"] == "submitted"
    assert payload["broker_order_id"] == "test-order"
    assert connector.calls[0][0].symbol == "MSFT"
    assert connector.calls[0][0].quantity == 3
    assert connector.calls[0][1] == "LIVE_STOCK_ORDER"
    assert (tmp_path / "logs" / "decision_log.jsonl").exists()


def test_dashboard_service_stock_connector_diagnostics(tmp_path: Path) -> None:
    service = DashboardService(root=tmp_path, intel_client=FakeIntelClient(), stock_connector=FakeStockConnector())

    payload = service.stock_connector_diagnostics()

    assert payload["ok"] is True
    assert payload["status"] == "ready"


def test_dashboard_service_autopilot_snapshot_is_redacted(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "autopilot.json").write_text(json.dumps({"enabled": True, "mode": "paper", "max_trade_usd": 25}))
    service = DashboardService(root=tmp_path, intel_client=FakeIntelClient(), alpaca_client=FakeAlpacaClient())

    payload = service.autopilot()

    assert payload["status"] == "enabled"
    assert payload["config"]["mode"] == "paper"
    assert payload["broker"]["api_key"] == "set"
    assert "secret-secret" not in json.dumps(payload)


def test_dashboard_service_autopilot_scan_and_execute_paper_order(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "watchlist.json").write_text(json.dumps({"symbols": ["MSFT"], "positions": [], "risk": {}}))
    (config / "market_universe.json").write_text(json.dumps({"symbols": [], "max_scan_symbols": 0}))
    (config / "autopilot.json").write_text(json.dumps({"enabled": True, "mode": "paper", "max_trade_usd": 25, "min_confidence": 40}))
    alpaca = FakeAlpacaClient()
    service = DashboardService(root=tmp_path, intel_client=FakeIntelClient(), alpaca_client=alpaca)

    scan = service.autopilot_scan()
    executed = service.autopilot_execute()

    assert scan["orders"][0]["symbol"] == "MSFT"
    assert executed["ok"] is True
    assert executed["executed"][0]["broker_order_id"] == "alpaca-paper-order"
    assert alpaca.orders[0][0].notional == 25
    assert (tmp_path / "logs" / "decision_log.jsonl").exists()


def test_dashboard_service_updates_autopilot_setting_with_live_confirmation(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "autopilot.json").write_text(json.dumps({"enabled": True, "mode": "paper"}))
    service = DashboardService(root=tmp_path, intel_client=FakeIntelClient(), alpaca_client=FakeAlpacaClient())

    blocked = service.set_autopilot_setting("mode", "live")
    updated = service.set_autopilot_setting("mode", "live", confirm="LIVE_ALPACA_AUTOPILOT")

    assert blocked["ok"] is False
    assert blocked["status"] == "confirmation_required"
    assert updated["ok"] is True
    assert updated["config"]["mode"] == "live"


def test_dashboard_service_trade_ideas_returns_ranked_actions(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "watchlist.json").write_text(json.dumps({"symbols": ["MSFT"], "positions": [], "risk": {"stop_loss_pct": 3}}))
    (config / "market_universe.json").write_text(json.dumps({"symbols": [], "max_scan_symbols": 0}))
    service = DashboardService(root=tmp_path, intel_client=FakeIntelClient())

    payload = service.trade_ideas()

    assert payload["summary"]["symbols_scanned"] == 1
    assert payload["market_trend"] in {"UP", "MIXED", "UNKNOWN"}
    assert payload["ideas"][0]["symbol"] == "MSFT"
    assert payload["ideas"][0]["action"] == "BUY_REVIEW"
    assert (tmp_path / "logs" / "decision_log.jsonl").exists()


def test_dashboard_service_paper_cycle_uses_venv_python(tmp_path: Path, monkeypatch) -> None:
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    python_path = venv_bin / "python"
    python_path.write_text("")
    service = DashboardService(root=tmp_path, intel_client=FakeIntelClient())
    calls = []

    def fake_run(args, cwd, text, capture_output, check):
        calls.append(args)
        return type("Result", (), {"returncode": 0, "stdout": "paper ok", "stderr": ""})()

    monkeypatch.setattr("scripts.dashboard.subprocess.run", fake_run)

    result = service.paper_cycle(notify=True)

    assert result["ok"] is True
    assert result["stdout"] == "paper ok"
    assert calls[0][0] == str(python_path)
    assert calls[0][-1] == "--notify"


def test_dashboard_service_scanner_alerts_use_telegram(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "watchlist.json").write_text(json.dumps({"symbols": ["AAPL"], "aliases": {"AAPL": ["APPLE INC"]}}))
    (config / "market_universe.json").write_text(json.dumps({"symbols": []}))
    service = DashboardService(root=tmp_path, intel_client=FakeIntelClient())
    calls = []

    def fake_run(args, cwd, text, capture_output, check):
        calls.append(args)
        return type("Result", (), {"returncode": 0, "stdout": "sent", "stderr": ""})()

    monkeypatch.setattr("scripts.dashboard.subprocess.run", fake_run)

    result = service.scanner_alerts()

    assert result["ok"] is True
    assert calls[0][1].endswith("telegram.sh")


def test_dashboard_service_trade_idea_alerts_use_telegram(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "watchlist.json").write_text(json.dumps({"symbols": ["MSFT"], "positions": [], "risk": {"stop_loss_pct": 3}}))
    (config / "market_universe.json").write_text(json.dumps({"symbols": []}))
    service = DashboardService(root=tmp_path, intel_client=FakeIntelClient())
    calls = []

    def fake_run(args, cwd, text, capture_output, check):
        calls.append(args)
        return type("Result", (), {"returncode": 0, "stdout": "sent", "stderr": ""})()

    monkeypatch.setattr("scripts.dashboard.subprocess.run", fake_run)

    result = service.trade_idea_alerts()

    assert result["ok"] is True
    assert "Trade ideas" in result["message"]
    assert calls[0][1].endswith("telegram.sh")


def test_handler_serves_index_status_and_404(tmp_path: Path) -> None:
    from scripts.dashboard import make_handler

    handler_class = make_handler(DashboardService(root=tmp_path, intel_client=FakeIntelClient()))

    assert handler_class is not None


def test_json_response_supports_custom_status() -> None:
    status, _headers, body = json_response({"error": "nope"}, status=404)

    assert status == 404
    assert json.loads(body.decode()) == {"error": "nope"}


def test_dashboard_html_has_unique_ids_and_app_shell() -> None:
    ids = re.findall(r'id="([^"]+)"', HTML)

    assert len(ids) == len(set(ids))
    assert 'class="app-shell"' in HTML
    assert 'class="metric-grid"' in HTML
    assert 'id="ticker-tape"' in HTML
    assert 'class="terminal-mark"' in HTML
    assert 'id="menu-toggle"' in HTML
    assert 'class="sidebar-backdrop"' in HTML
    assert "toggleSidebar" in HTML
    assert "closeSidebar" in HTML
    assert "menu-open" in HTML
    assert "body.sidebar-collapsed" in HTML
    assert 'class="mode-switch"' in HTML
    assert 'id="setup-modal"' in HTML
    assert 'id="setup-form"' in HTML
    assert "/api/setup-status" in HTML
    assert "/api/setup" in HTML
    assert "renderSetupModal" in HTML
    assert "submitSetup" in HTML
    assert "arcade-grid" in HTML
    assert "/api/trading-mode" in HTML
    assert "/api/stock-settings" in HTML
    assert 'id="stock-settings"' in HTML
    assert "renderStockSettings" in HTML
    assert "setStockSetting" in HTML
    assert 'id="command-center-panel"' in HTML
    assert "/api/commands" in HTML
    assert "renderCommands" in HTML
    assert 'id="growth-panel"' in HTML
    assert 'id="tickets-panel"' in HTML
    assert "/api/tickets" in HTML
    assert "renderTickets" in HTML
    assert "/api/growth-candidates" in HTML
    assert "/api/stock-chart" in HTML
    assert "/api/stock-order-intent" in HTML
    assert "/api/stock-order" in HTML
    assert "/api/stock-connector-diagnostics" in HTML
    assert "diagnoseStockConnector" in HTML
    assert 'id="stock-connector-diagnostics"' in HTML
    assert 'id="stock-chart-drawer"' in HTML
    assert 'id="stock-chart-tooltip"' in HTML
    assert 'id="stock-ticket-drawer"' in HTML
    assert 'id="stock-ticket-quantity"' in HTML
    assert 'id="stock-ticket-confirm"' in HTML
    assert 'id="toast-stack"' in HTML
    assert 'data-theme="arcade"' in HTML
    assert 'id="ui-theme-settings"' in HTML
    assert "/api/ui-theme" in HTML
    assert "setUiTheme" in HTML
    assert "theme-classic" in HTML
    assert "showOrderToast" in HTML
    assert "dismissToast" in HTML
    assert "openStockChart" in HTML
    assert "stockActionButtons" in HTML
    assert "openStockTicket" in HTML
    assert "drawStockChart" in HTML
    assert "drawChartAxes" in HTML
    assert "showChartTooltip" in HTML
    assert "renderGrowthCandidates" in HTML
    assert "bonehawk" in HTML
    assert 'id="stocks-panel"' in HTML
    assert 'id="autopilot-panel"' in HTML
    assert 'id="autopilot-settings"' in HTML
    assert 'id="autopilot-output"' in HTML
    assert 'id="agentic-panel"' in HTML
    assert 'id="robinhood-panel"' in HTML
    assert "/api/agentic-trading" in HTML
    assert "/api/autopilot" in HTML
    assert "/api/autopilot-scan" in HTML
    assert "/api/autopilot-run" in HTML
    assert "/api/autopilot-settings" in HTML
    assert "/api/stocks" in HTML
    assert "/api/robinhood" in HTML
    assert "renderTradeIdeas" in HTML
    assert "renderAutopilot" in HTML
    assert "scanAutopilot" in HTML
    assert "runAutopilotPaper" in HTML
    assert "setAutopilotSetting" in HTML
