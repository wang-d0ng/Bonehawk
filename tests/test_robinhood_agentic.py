from __future__ import annotations

from pathlib import Path

from scripts.robinhood_agentic import (
    AGENTIC_TRADING_MCP_URL,
    AgenticTradingConfig,
    agentic_trading_snapshot,
)


def test_agentic_trading_snapshot_detects_codex_mcp_config(tmp_path: Path) -> None:
    codex_config = tmp_path / "config.toml"
    codex_config.write_text(f'[mcp_servers.robinhood-trading]\nurl = "{AGENTIC_TRADING_MCP_URL}"\n')
    config = AgenticTradingConfig(codex_config_path=codex_config)

    payload = agentic_trading_snapshot(config)

    assert payload["status"] == "configured"
    assert payload["mcp_url"] == AGENTIC_TRADING_MCP_URL
    assert payload["codex_mcp_configured"] is True
    assert payload["capabilities"]["stock_order_place"] == "requires_authenticated_mcp"


def test_agentic_trading_snapshot_reports_missing_config(tmp_path: Path) -> None:
    config = AgenticTradingConfig(codex_config_path=tmp_path / "missing.toml")

    payload = agentic_trading_snapshot(config)

    assert payload["status"] == "not_configured"
    assert payload["codex_mcp_configured"] is False
    assert "codex mcp add robinhood-trading" in payload["setup"]["codex_cli_command"]


def test_agentic_trading_config_from_env_hides_secret_like_values(tmp_path: Path, monkeypatch) -> None:
    codex_config = tmp_path / "config.toml"
    monkeypatch.setenv("ROBINHOOD_AGENTIC_MCP_URL", AGENTIC_TRADING_MCP_URL)
    monkeypatch.setenv("BONEHAWK_STOCK_TRADING_MODE", "review")

    config = AgenticTradingConfig.from_env(codex_config_path=codex_config)
    payload = agentic_trading_snapshot(config)

    assert payload["stock_trading_mode"] == "review"
    assert "secret" not in str(payload).lower()
