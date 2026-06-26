#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.dashboard import DashboardService

BOT_API_BASE = "https://api.telegram.org"
COMMAND_PREFIXES = {"/bh", "/bonehawk", "/autopilot"}


@dataclass(frozen=True)
class CommandResponse:
    ok: bool
    status: str
    message: str


class TelegramAutopilotBot:
    def __init__(
        self,
        root: Path = ROOT,
        token: str = "",
        allowed_chat_ids: set[str] | None = None,
        service: Any | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.root = root
        env = _read_env(root / ".env")
        self.token = token or env.get("TELEGRAM_BOT_TOKEN", "")
        self.allowed_chat_ids = allowed_chat_ids if allowed_chat_ids is not None else _allowed_chat_ids(env.get("ALLOWED_CHAT_IDS", ""))
        self.service = service or DashboardService(root=root)
        self.http_client = http_client or httpx.Client(timeout=30, follow_redirects=True)
        self.offset_path = root / "logs" / "telegram_autopilot_offset.txt"

    @property
    def configured(self) -> bool:
        return bool(self.token and self.allowed_chat_ids)

    def poll_once(self, timeout: int = 0) -> dict[str, int | bool | str]:
        if not self.configured:
            return {"ok": False, "status": "not_configured", "processed": 0, "ignored": 0, "message": "Telegram token and allowed chat IDs are required."}
        updates = self._get_updates(timeout=timeout)
        processed = 0
        ignored = 0
        max_update_id: int | None = None
        for update in updates:
            update_id = _safe_int(update.get("update_id"))
            if update_id is not None:
                max_update_id = max(update_id, max_update_id or update_id)
            message = update.get("message") or update.get("edited_message") or {}
            chat_id = str((message.get("chat") or {}).get("id", ""))
            text = str(message.get("text") or "")
            command = parse_autopilot_command(text)
            if not command:
                ignored += 1
                continue
            if not is_allowed_chat(chat_id, self.allowed_chat_ids):
                ignored += 1
                continue
            response = handle_autopilot_command(text, self.service)
            self._send_message(chat_id, response.message)
            processed += 1
        if max_update_id is not None:
            self._write_offset(max_update_id + 1)
        return {"ok": True, "status": "polled", "processed": processed, "ignored": ignored}

    def _get_updates(self, timeout: int = 0) -> list[dict[str, Any]]:
        params = {"timeout": str(max(0, int(timeout))), "allowed_updates": json.dumps(["message", "edited_message"])}
        offset = self._read_offset()
        if offset is not None:
            params["offset"] = str(offset)
        response = self.http_client.get(f"{BOT_API_BASE}/bot{self.token}/getUpdates", params=params)
        response.raise_for_status()
        payload = response.json()
        result = payload.get("result", [])
        return result if isinstance(result, list) else []

    def _send_message(self, chat_id: str, message: str) -> None:
        if not chat_id:
            return
        text = _truncate_message(message)
        self.http_client.post(
            f"{BOT_API_BASE}/bot{self.token}/sendMessage",
            content=f"chat_id={quote_plus(str(chat_id))}&text={quote_plus(text)}&disable_web_page_preview=true",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ).raise_for_status()

    def _read_offset(self) -> int | None:
        if not self.offset_path.exists():
            return None
        return _safe_int(self.offset_path.read_text().strip())

    def _write_offset(self, offset: int) -> None:
        self.offset_path.parent.mkdir(parents=True, exist_ok=True)
        self.offset_path.write_text(str(offset))


def parse_autopilot_command(text: str) -> tuple[str, list[str]] | None:
    try:
        parts = shlex.split(str(text or "").strip())
    except ValueError:
        return None
    if not parts:
        return None
    prefix = parts[0].split("@", 1)[0].lower()
    if prefix not in COMMAND_PREFIXES:
        return None
    command = parts[1].lower() if len(parts) > 1 else "help"
    return command, parts[2:]


def is_allowed_chat(chat_id: object, allowed_chat_ids: set[str]) -> bool:
    return str(chat_id).strip() in {str(item).strip() for item in allowed_chat_ids if str(item).strip()}


def handle_autopilot_command(text: str, service: Any) -> CommandResponse:
    parsed = parse_autopilot_command(text)
    if not parsed:
        return CommandResponse(False, "ignored", _help_message())
    command, args = parsed
    if command in {"help", "start"}:
        return CommandResponse(True, "help", _help_message())
    if command == "status":
        return CommandResponse(True, "status", _format_status(service.autopilot()))
    if command == "scan":
        return CommandResponse(True, "scanned", _format_scan(service.autopilot_scan()))
    if command in {"run", "paper"}:
        snapshot = service.autopilot()
        if (snapshot.get("config") or {}).get("mode") != "paper":
            return CommandResponse(False, "live_blocked", "Bonehawk autopilot Telegram run is paper mode only. Switch the dashboard/autopilot back to paper first.")
        return CommandResponse(True, "executed", _format_execution(service.autopilot_execute()))
    if command == "report":
        return CommandResponse(True, "report", _format_report(service.report(window_minutes=_report_window_minutes(args))))
    if command in {"tickets", "orders"}:
        return CommandResponse(True, "tickets", _format_tickets(service.tickets()))
    if command == "positions":
        return CommandResponse(True, "positions", _format_positions(service.portfolio_sync()))
    if command in {"pause", "disable", "off"}:
        payload = service.set_autopilot_setting("enabled", False)
        if hasattr(service, "stop_autopilot_background"):
            service.stop_autopilot_background()
        return _setting_response(payload, "Autopilot paused.")
    if command in {"resume", "enable", "on"}:
        payload = service.set_autopilot_setting("enabled", True)
        if payload.get("ok") and hasattr(service, "start_autopilot_background"):
            service.start_autopilot_background()
        return _setting_response(payload, "Autopilot resumed.")
    if command == "kill":
        payload = service.set_autopilot_setting("enabled", False)
        if hasattr(service, "stop_autopilot_background"):
            service.stop_autopilot_background()
        return CommandResponse(bool(payload.get("ok")), "kill_switch", f"Bonehawk kill switch: {payload.get('message') or 'Autopilot stopped.'}")
    if command == "health":
        return CommandResponse(True, "health", _format_health(service.trading_desk()))
    if command == "desk":
        return CommandResponse(True, "desk", _format_desk(service.trading_desk()))
    if command == "paper-mode":
        return _setting_response(service.set_autopilot_setting("mode", "paper"), "Autopilot mode set to paper.")
    if command in {"size", "trade-size"}:
        return _number_setting(service, "max_trade_usd", args, "Max trade size")
    if command in {"max-positions", "position-limit"}:
        return _number_setting(service, "max_open_positions", args, "Max open positions")
    if command in {"confidence", "min-confidence"}:
        return _number_setting(service, "min_confidence", args, "Minimum confidence")
    return CommandResponse(False, "unknown_command", f"Bonehawk: unknown command '{command}'.\n\n{_help_message()}")


def _number_setting(service: Any, setting: str, args: list[str], label: str) -> CommandResponse:
    if not args:
        return CommandResponse(False, "missing_value", f"{label} needs a value.")
    try:
        value = float(args[0])
    except ValueError:
        return CommandResponse(False, "invalid_value", f"{label} must be a number.")
    if setting in {"max_open_positions", "min_confidence"}:
        value = int(value)
    return _setting_response(service.set_autopilot_setting(setting, value), f"{label} updated to {value}.")


def _setting_response(payload: dict[str, Any], fallback: str) -> CommandResponse:
    ok = bool(payload.get("ok"))
    return CommandResponse(ok, str(payload.get("status") or ("updated" if ok else "failed")), f"Bonehawk: {payload.get('message') or fallback}")


def _format_status(payload: dict[str, Any]) -> str:
    config = payload.get("config") or {}
    broker = payload.get("broker") or {}
    return (
        "Bonehawk Autopilot:\n"
        f"Status: {payload.get('status', 'unknown')}\n"
        f"Mode: {config.get('mode', 'paper')}\n"
        f"Enabled: {bool(config.get('enabled'))}\n"
        f"Broker: Alpaca {broker.get('status', 'unknown')}\n"
        f"Trade size: ${config.get('max_trade_usd', 0)}\n"
        f"Max positions: {config.get('max_open_positions', 0)}\n"
        f"Minimum confidence: {config.get('min_confidence', 0)}"
    )


def _format_scan(payload: dict[str, Any]) -> str:
    orders = payload.get("orders") or []
    blocked = payload.get("blocked") or []
    summary = payload.get("summary") or {}
    agentic = (payload.get("agentic_scan") or {}).get("summary") or {}
    top = agentic.get("top_symbol") or (orders[0].get("symbol") if orders else "none")
    return (
        "Bonehawk Scan:\n"
        f"Scanned {summary.get('symbols_scanned', 0)} symbols.\n"
        f"Planned: {len(orders)} | Blocked: {len(blocked)}\n"
        f"Top setup: {top}"
    )


def _format_execution(payload: dict[str, Any]) -> str:
    summary = payload.get("execution_summary") or {}
    lines = ["Bonehawk Paper Run:", str(summary.get("message") or payload.get("status") or "No status returned.")]
    executed = payload.get("executed") or []
    for item in executed[:5]:
        lines.append(
            f"{item.get('symbol', 'UNKNOWN')} {item.get('broker_status') or item.get('status', 'submitted')} "
            f"{item.get('fill_status') or ''} {item.get('broker_order_id') or ''}".strip()
        )
    return "\n".join(lines)


def _format_tickets(payload: dict[str, Any]) -> str:
    tickets = payload.get("tickets") or []
    if not tickets:
        return "Bonehawk Tickets: no tickets yet."
    lines = ["Bonehawk Tickets:"]
    for ticket in tickets[:6]:
        lines.append(
            f"{ticket.get('symbol', 'UNKNOWN')} {ticket.get('side', 'ORDER')} "
            f"{ticket.get('status', 'unknown')} {ticket.get('fill_status') or ticket.get('broker_status') or ''} "
            f"{ticket.get('broker_order_id') or ''}".strip()
        )
    return "\n".join(lines)


def _format_positions(payload: dict[str, Any]) -> str:
    positions = payload.get("positions") or []
    if not positions:
        positions = ((payload.get("performance") or {}).get("positions") or [])
    if not positions:
        return "Bonehawk Positions: no open positions found."
    lines = ["Bonehawk Positions:"]
    for position in positions[:8]:
        symbol = str(position.get("symbol") or "UNKNOWN").upper()
        quantity = _format_quantity(position.get("quantity"))
        price = _money(position.get("current_price"))
        pnl = _money(position.get("unrealized_pnl"))
        lines.append(f"{symbol} {quantity} @ {price} P/L {pnl}".strip())
    return "\n".join(lines)


def _format_health(payload: dict[str, Any]) -> str:
    health = payload.get("data_health") or {}
    lines = [
        f"Bonehawk Data health: {health.get('status', 'unknown')} {health.get('score', 0)}",
        f"Risk action: {health.get('risk_action', 'unknown')}",
    ]
    for check in (health.get("checks") or [])[:5]:
        mark = "OK" if check.get("ok") else "WARN"
        lines.append(f"{mark} {check.get('name', 'check')}: {check.get('message', '')}")
    return "\n".join(lines)


def _format_desk(payload: dict[str, Any]) -> str:
    health = payload.get("data_health") or {}
    truth = (payload.get("order_truth") or {}).get("summary") or {}
    journal = (payload.get("trade_journal") or {}).get("summary") or {}
    strategy = ((payload.get("strategy_scorecard") or {}).get("strategies") or [{}])[0]
    shadow = (payload.get("shadow_mode") or {}).get("summary") or {}
    backtest = (payload.get("backtest") or {}).get("summary") or {}
    return "\n".join(
        [
            "Bonehawk Desk:",
            f"Data health: {health.get('status', 'unknown')} {health.get('score', 0)}",
            f"Orders active/submitted/rejected: {truth.get('active', 0)}/{truth.get('submitted', 0)}/{truth.get('rejected', 0)}",
            f"Journal entries: {journal.get('entries', 0)} net {_money(journal.get('net_pnl'))}",
            f"Top strategy: {strategy.get('strategy', 'none')} {strategy.get('win_rate_pct', 0)}% win {_money(strategy.get('net_pnl'))}",
            f"Shadow W/L/open: {shadow.get('wins', 0)}/{shadow.get('losses', 0)}/{shadow.get('open', 0)}",
            f"Best backtest: {backtest.get('best_symbol', 'none')} {backtest.get('best_return_pct', 0)}%",
        ]
    )


def _format_report(payload: dict[str, Any]) -> str:
    portfolio = payload.get("portfolio") or {}
    trades = payload.get("trades") or []
    window = int(payload.get("window_minutes") or 10)
    lines = [
        f"Bonehawk Report - Last {window}m",
        f"Portfolio: {_money(portfolio.get('account_value', portfolio.get('total_value', 0)))}",
        f"Open P/L: {_money(portfolio.get('unrealized_pnl'))} ({_pct(portfolio.get('unrealized_pnl_pct'))})",
        f"Market trend: {payload.get('market_trend', 'UNKNOWN')}",
        f"Trades: {len(trades)}",
    ]
    if not trades:
        lines.append("No submitted, filled, or rejected trades in this window.")
        return "\n".join(lines)
    lines.append("Recent trades:")
    for trade in trades[:8]:
        lines.append(_format_report_trade(trade))
    return "\n".join(lines)


def _format_report_trade(trade: dict[str, Any]) -> str:
    symbol = str(trade.get("symbol") or "UNKNOWN").upper()
    side = str(trade.get("side") or "ORDER").upper()
    status = str(trade.get("category") or trade.get("status") or "unknown")
    quantity = _format_quantity(trade.get("quantity"))
    fill = str(trade.get("fill_status") or trade.get("broker_status") or "").strip()
    order_id = str(trade.get("broker_order_id") or "").strip()
    parts = [symbol, side, status]
    if quantity:
        parts.append(f"qty {quantity}")
    if fill:
        parts.append(fill)
    if order_id:
        parts.append(order_id)
    return " ".join(parts)


def _report_window_minutes(args: list[str]) -> int:
    if not args:
        return 10
    try:
        value = int(float(args[0]))
    except ValueError:
        return 10
    return max(1, min(120, value))


def _money(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    sign = "-" if number < 0 else ""
    return f"{sign}${abs(number):,.2f}"


def _pct(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    return f"{number:.2f}%"


def _format_quantity(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    return f"{number:g}" if number > 0 else ""


def _help_message() -> str:
    return (
        "Bonehawk Telegram commands:\n"
        "/bh status\n"
        "/bh scan\n"
        "/bh run  (paper only)\n"
        "/bh report\n"
        "/bh tickets | /bh orders\n"
        "/bh positions\n"
        "/bh health | /bh desk\n"
        "/bh pause | /bh resume | /bh kill\n"
        "/bh paper-mode\n"
        "/bh size 25\n"
        "/bh max-positions 3\n"
        "/bh confidence 55"
    )


def _read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if "=" not in line or line.strip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _allowed_chat_ids(raw: str) -> set[str]:
    return {item.strip() for item in str(raw or "").split(",") if item.strip()}


def _truncate_message(message: str) -> str:
    text = str(message or "")
    return text if len(text) <= 3900 else f"{text[:3890]}..."


def _safe_int(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Control Bonehawk Alpaca autopilot from authorized Telegram chats.")
    parser.add_argument("--once", action="store_true", help="Poll Telegram once and exit.")
    parser.add_argument("--loop", action="store_true", help="Poll Telegram continuously.")
    parser.add_argument("--interval-seconds", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=20)
    args = parser.parse_args()

    bot = TelegramAutopilotBot()
    if args.once:
        print(json.dumps(bot.poll_once(timeout=0), indent=2))
        return
    if not args.loop:
        parser.error("Use --once or --loop")
    while True:
        print(json.dumps(bot.poll_once(timeout=args.timeout), sort_keys=True))
        time.sleep(max(1, args.interval_seconds))


if __name__ == "__main__":
    main()
