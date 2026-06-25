from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

AGENTIC_TRADING_MCP_URL = "https://agent.robinhood.com/mcp/trading"
AGENTIC_MCP_NAME = "robinhood-trading"


@dataclass(frozen=True)
class AgenticTradingConfig:
    mcp_url: str = AGENTIC_TRADING_MCP_URL
    stock_trading_mode: str = "review"
    codex_config_path: Path = Path.home() / ".codex" / "config.toml"

    @classmethod
    def from_env(cls, codex_config_path: Path | None = None) -> "AgenticTradingConfig":
        return cls(
            mcp_url=os.getenv("ROBINHOOD_AGENTIC_MCP_URL", AGENTIC_TRADING_MCP_URL).strip() or AGENTIC_TRADING_MCP_URL,
            stock_trading_mode=os.getenv("BONEHAWK_STOCK_TRADING_MODE", "review").strip().lower() or "review",
            codex_config_path=codex_config_path or Path.home() / ".codex" / "config.toml",
        )


def agentic_trading_snapshot(config: AgenticTradingConfig | None = None) -> dict[str, Any]:
    config = config or AgenticTradingConfig.from_env()
    codex_configured = _codex_config_has_robinhood_mcp(config.codex_config_path, config.mcp_url)
    return {
        "status": "configured" if codex_configured else "not_configured",
        "message": _message(codex_configured),
        "mcp_name": AGENTIC_MCP_NAME,
        "mcp_url": config.mcp_url,
        "codex_mcp_configured": codex_configured,
        "stock_trading_mode": _normalize_trading_mode(config.stock_trading_mode),
        "capabilities": {
            "stock_market_data": "requires_authenticated_mcp",
            "stock_account_read": "requires_authenticated_mcp",
            "stock_order_read": "requires_authenticated_mcp",
            "stock_order_place": "requires_authenticated_mcp",
        },
        "setup": {
            "codex_cli_command": f"codex mcp add {AGENTIC_MCP_NAME} --url {config.mcp_url}",
            "codex_login_command": f"codex mcp login {AGENTIC_MCP_NAME}",
            "desktop_note": "Complete Robinhood Agentic account onboarding in a desktop browser.",
        },
        "guardrails": [
            "Use only Robinhood's official Trading MCP URL.",
            "Keep stock trading in review mode until the Agentic account is funded and tested.",
            "Live stock orders must go through the authenticated MCP, not the Crypto API wrapper.",
        ],
    }


def _codex_config_has_robinhood_mcp(path: Path, mcp_url: str) -> bool:
    try:
        text = path.read_text()
    except OSError:
        return False
    return AGENTIC_MCP_NAME in text and mcp_url in text


def _normalize_trading_mode(value: str) -> str:
    return value if value in {"review", "paper", "live"} else "review"


def _message(codex_configured: bool) -> str:
    if codex_configured:
        return "Robinhood Trading MCP is configured in Codex. Finish OAuth/onboarding before live stock trading."
    return "Robinhood Trading MCP is not configured in Codex yet."
