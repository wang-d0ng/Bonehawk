from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from scripts.robinhood_agentic import AGENTIC_MCP_NAME, AGENTIC_TRADING_MCP_URL, _codex_config_has_robinhood_mcp

LIVE_STOCK_CONFIRMATION = "LIVE_STOCK_ORDER"


@dataclass(frozen=True)
class StockOrderRequest:
    symbol: str
    side: str
    quantity: float
    order_type: str = "market"
    time_in_force: str = "day"


@dataclass(frozen=True)
class StockConnectorConfig:
    connector_mode: str = "disabled"
    stock_trading_mode: str = "review"
    app_trading_mode: str = "paper"
    mcp_name: str = AGENTIC_MCP_NAME
    mcp_url: str = AGENTIC_TRADING_MCP_URL
    codex_config_path: Path = Path.home() / ".codex" / "config.toml"
    codex_path: str = "codex"
    timeout_sec: int = 180

    @classmethod
    def from_project_env(cls, env_path: Path | None = None, codex_config_path: Path | None = None) -> "StockConnectorConfig":
        values = _read_env_file(env_path) if env_path else {}
        return cls(
            connector_mode=_project_setting_value("BONEHAWK_STOCK_ORDER_CONNECTOR", values, "disabled").strip().lower() or "disabled",
            stock_trading_mode=_project_setting_value("BONEHAWK_STOCK_TRADING_MODE", values, "review").strip().lower() or "review",
            app_trading_mode=_project_setting_value("TRADING_MODE", values, "paper").strip().lower() or "paper",
            mcp_url=_env_value("ROBINHOOD_AGENTIC_MCP_URL", values, AGENTIC_TRADING_MCP_URL).strip() or AGENTIC_TRADING_MCP_URL,
            codex_config_path=codex_config_path or Path.home() / ".codex" / "config.toml",
            codex_path=_env_value("BONEHAWK_CODEX_PATH", values, "codex").strip() or "codex",
            timeout_sec=_int_env_value("BONEHAWK_STOCK_ORDER_TIMEOUT_SEC", values, 180),
        )


class RobinhoodStockOrderConnector:
    def __init__(self, config: StockConnectorConfig) -> None:
        self.config = config

    def snapshot(self) -> dict[str, Any]:
        configured = _codex_config_has_robinhood_mcp(self.config.codex_config_path, self.config.mcp_url)
        callable_now = (
            self.config.connector_mode == "codex_mcp"
            and configured
            and self.config.stock_trading_mode == "live"
            and self.config.app_trading_mode == "live"
        )
        return {
            "status": "callable" if callable_now else "blocked",
            "connector_mode": self.config.connector_mode,
            "mcp_name": self.config.mcp_name,
            "mcp_configured": configured,
            "stock_trading_mode": self.config.stock_trading_mode,
            "app_trading_mode": self.config.app_trading_mode,
            "confirmation_phrase": LIVE_STOCK_CONFIRMATION,
            "guardrails": [
                "Connector mode must be codex_mcp.",
                "TRADING_MODE and BONEHAWK_STOCK_TRADING_MODE must both be live.",
                f"Live order requests must include {LIVE_STOCK_CONFIRMATION}.",
            ],
        }

    def place_order(self, request: StockOrderRequest, confirm: str) -> dict[str, Any]:
        readiness = self.snapshot()
        if self.config.connector_mode != "codex_mcp":
            return _blocked("connector_disabled", "Set BONEHAWK_STOCK_ORDER_CONNECTOR=codex_mcp before live stock orders.", readiness)
        if not readiness["mcp_configured"]:
            return _blocked("mcp_not_configured", "Robinhood Trading MCP is not configured in Codex.", readiness)
        if self.config.app_trading_mode != "live":
            return _blocked("app_not_live", "Set TRADING_MODE=live before live stock orders.", readiness)
        if self.config.stock_trading_mode != "live":
            return _blocked("stock_mode_not_live", "Set BONEHAWK_STOCK_TRADING_MODE=live before live stock orders.", readiness)
        if confirm != LIVE_STOCK_CONFIRMATION:
            return _blocked("confirmation_required", "Live stock order requires exact confirmation.", readiness)
        return self._place_via_codex(request)

    def diagnose(self) -> dict[str, Any]:
        readiness = self.snapshot()
        checks = [
            _check("Connector enabled", self.config.connector_mode == "codex_mcp", "Set Stock connector to Codex MCP in Settings."),
            _check("MCP configured", bool(readiness["mcp_configured"]), "Add robinhood-trading MCP to Codex."),
            _check("App mode live", self.config.app_trading_mode == "live", "Set App trading mode to Live."),
            _check("Stock mode live", self.config.stock_trading_mode == "live", "Set Stock trading mode to Live."),
        ]
        mcp_list = self._codex_mcp_list()
        ready = all(item["ok"] for item in checks) and mcp_list["ok"]
        return {
            "ok": ready,
            "status": "ready" if ready else "blocked",
            "message": "Stock connector is ready." if ready else "Stock connector is blocked. Review the failed checks.",
            "readiness": readiness,
            "checks": checks,
            "codex_mcp_list": mcp_list,
            "review_only": True,
        }

    def _place_via_codex(self, request: StockOrderRequest) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="bonehawk-stock-order-") as temp_dir:
            temp_path = Path(temp_dir)
            schema_path = temp_path / "order_result.schema.json"
            output_path = temp_path / "order_result.json"
            schema_path.write_text(json.dumps(_output_schema()), encoding="utf-8")
            result = subprocess.run(
                [
                    self.config.codex_path,
                    "exec",
                    "--cd",
                    str(Path.cwd()),
                    "--skip-git-repo-check",
                    "--sandbox",
                    "read-only",
                    "--ask-for-approval",
                    "never",
                    "--output-schema",
                    str(schema_path),
                    "--output-last-message",
                    str(output_path),
                    _order_prompt(request, self.config.mcp_name),
                ],
                text=True,
                capture_output=True,
                check=False,
                timeout=self.config.timeout_sec,
            )
            if result.returncode != 0:
                return {
                    "ok": False,
                    "status": "connector_failed",
                    "message": "Robinhood connector call failed before placing a verified order.",
                    "detail": _safe_process_detail(result.stdout, result.stderr),
                    "returncode": result.returncode,
                    "review_only": False,
                }
            try:
                payload = json.loads(output_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return {
                    "ok": False,
                    "status": "connector_unreadable",
                    "message": "Robinhood connector did not return a readable order result.",
                    "detail": _safe_process_detail(result.stdout, result.stderr),
                    "review_only": False,
                }
            return _normalize_connector_result(payload, request)

    def _codex_mcp_list(self) -> dict[str, Any]:
        try:
            result = subprocess.run(
                [self.config.codex_path, "mcp", "list"],
                text=True,
                capture_output=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return {"ok": False, "status": "mcp_list_failed", "output": str(error), "returncode": 124}
        output = _relevant_mcp_output(_safe_process_detail(result.stdout, result.stderr, limit=1600), self.config.mcp_name)
        robinhood_present = self.config.mcp_name in output
        return {
            "ok": result.returncode == 0 and robinhood_present,
            "status": "found" if robinhood_present else "not_found",
            "output": output,
            "returncode": result.returncode,
        }


def _blocked(status: str, message: str, readiness: dict[str, Any]) -> dict[str, Any]:
    return {"ok": False, "status": status, "message": message, "readiness": readiness, "review_only": True}


def _check(label: str, ok: bool, fix: str) -> dict[str, str | bool]:
    return {"label": label, "ok": ok, "status": "ok" if ok else "blocked", "fix": "" if ok else fix}


def _normalize_connector_result(payload: dict[str, Any], request: StockOrderRequest) -> dict[str, Any]:
    ok = bool(payload.get("ok"))
    return {
        "ok": ok,
        "status": str(payload.get("status") or ("submitted" if ok else "failed")),
        "symbol": request.symbol,
        "side": request.side,
        "quantity": request.quantity,
        "order_type": request.order_type,
        "time_in_force": request.time_in_force,
        "broker_order_id": payload.get("broker_order_id"),
        "message": str(payload.get("message") or ""),
        "review_only": False,
    }


def _safe_process_detail(stdout: str, stderr: str, limit: int = 600) -> str:
    text = "\n".join(part for part in [stderr.strip(), stdout.strip()] if part)
    if not text:
        return "No connector output was returned."
    redacted = re.sub(r"(?i)(token|secret|password|api[_-]?key)=\S+", r"\1=[redacted]", text)
    redacted = re.sub(r"(?i)secret", "[redacted]", redacted)
    return redacted[-limit:]


def _relevant_mcp_output(output: str, mcp_name: str) -> str:
    lines = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("Name") or mcp_name in stripped:
            lines.append(stripped)
    return "\n".join(lines) if lines else output


def _order_prompt(request: StockOrderRequest, mcp_name: str) -> str:
    order = asdict(request)
    return (
        "Use only the configured Robinhood Trading MCP server to place this exact stock order. "
        "Do not run shell commands. Do not simulate success. If the MCP cannot place the order, return ok=false. "
        f"MCP server name: {mcp_name}. "
        f"Order JSON: {json.dumps(order, sort_keys=True)}. "
        "Return only JSON matching the provided schema."
    )


def _output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "ok": {"type": "boolean"},
            "status": {"type": "string"},
            "broker_order_id": {"type": ["string", "null"]},
            "message": {"type": "string"},
        },
        "required": ["ok", "status", "broker_order_id", "message"],
    }


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.strip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _env_value(key: str, file_values: dict[str, str], default: str) -> str:
    return os.getenv(key) or file_values.get(key) or default


def _project_setting_value(key: str, file_values: dict[str, str], default: str) -> str:
    return file_values.get(key) or os.getenv(key) or default


def _int_env_value(key: str, file_values: dict[str, str], default: int) -> int:
    try:
        return int(_env_value(key, file_values, str(default)))
    except ValueError:
        return default
