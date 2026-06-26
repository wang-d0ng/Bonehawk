from __future__ import annotations

import json
from pathlib import Path

import httpx

from scripts.telegram_autopilot import (
    TelegramAutopilotBot,
    handle_autopilot_command,
    is_allowed_chat,
    parse_autopilot_command,
)


class FakeTelegramService:
    def __init__(self, mode: str = "paper") -> None:
        self.mode = mode
        self.executed = False
        self.scanned = False
        self.background_started = False
        self.background_stopped = False
        self.settings: list[tuple[str, object, str]] = []

    def autopilot(self):
        return {
            "ok": True,
            "status": "enabled",
            "config": {"enabled": True, "mode": self.mode, "max_trade_usd": 25, "max_open_positions": 3, "min_confidence": 55},
            "broker": {"status": "connected", "paper": self.mode == "paper"},
            "telegram": {"status": "ready"},
        }

    def autopilot_scan(self):
        self.scanned = True
        return {
            "ok": True,
            "status": "scanned",
            "mode": self.mode,
            "summary": {"symbols_scanned": 40},
            "orders": [{"symbol": "MSFT", "notional": 25}],
            "blocked": [],
            "agentic_scan": {"summary": {"opportunities": 1, "top_symbol": "MSFT"}},
        }

    def autopilot_execute(self, confirm: str = ""):
        self.executed = True
        return {
            "ok": True,
            "status": "executed",
            "mode": self.mode,
            "summary": {"symbols_scanned": 40},
            "orders": [{"symbol": "MSFT", "notional": 25}],
            "blocked": [],
            "executed": [{"symbol": "MSFT", "broker_order_id": "paper-order", "broker_status": "accepted", "fill_status": "not_filled_yet"}],
            "execution_summary": {"submitted": 1, "rejected": 0, "planned": 1, "blocked": 0, "message": "Submitted 1 paper order."},
        }

    def set_autopilot_setting(self, setting, value, confirm: str = ""):
        self.settings.append((setting, value, confirm))
        return {"ok": True, "status": "updated", "message": f"{setting} updated.", "config": {str(setting): value}}

    def start_autopilot_background(self):
        self.background_started = True
        return {"ok": True, "status": "started", "running": True}

    def stop_autopilot_background(self):
        self.background_stopped = True
        return {"ok": True, "status": "stopped", "running": False}

    def tickets(self):
        return {
            "tickets": [
                {"symbol": "MSFT", "side": "BUY", "status": "submitted", "broker_status": "accepted", "fill_status": "not_filled_yet", "broker_order_id": "paper-order"}
            ]
        }

    def portfolio_sync(self):
        return {
            "positions": [
                {"symbol": "MSFT", "quantity": 2, "current_price": 100, "unrealized_pnl": 4.25},
                {"symbol": "NVDA", "quantity": 1, "current_price": 120, "unrealized_pnl": -1.5},
            ],
            "performance": {"account_value": 1250.5, "unrealized_pnl": 2.75},
        }

    def trading_desk(self):
        return {
            "ok": True,
            "status": "ready",
            "data_health": {"status": "healthy", "score": 90, "risk_action": "normal"},
            "order_truth": {"summary": {"active": 1, "submitted": 1, "rejected": 0, "filled": 0}},
            "trade_journal": {"summary": {"entries": 3, "wins": 2, "losses": 1, "net_pnl": 5.5}},
            "strategy_scorecard": {"strategies": [{"strategy": "momentum_breakout", "win_rate_pct": 66.7, "net_pnl": 5.5}]},
            "shadow_mode": {"summary": {"open": 1, "evaluated": 4, "wins": 3, "losses": 1, "avg_return_pct": 0.8}},
            "backtest": {"summary": {"symbols_tested": 12, "passing": 7, "best_symbol": "MSFT", "best_return_pct": 2.4}},
        }

    def report(self, window_minutes: int = 10):
        return {
            "ok": True,
            "status": "ready",
            "window_minutes": window_minutes,
            "portfolio": {
                "account_value": 1250.5,
                "unrealized_pnl": -8.25,
                "unrealized_pnl_pct": -0.66,
                "source_status": "connected",
            },
            "market_trend": "UP",
            "trades": [
                {
                    "timestamp": "2026-06-25T16:20:00Z",
                    "symbol": "MSFT",
                    "side": "BUY",
                    "category": "submitted",
                    "quantity": 1,
                    "broker_order_id": "paper-order",
                    "fill_status": "not_filled_yet",
                }
            ],
            "summary": {"trade_count": 1},
        }


def test_parse_autopilot_command_accepts_bonehawk_aliases() -> None:
    assert parse_autopilot_command("/bh scan") == ("scan", [])
    assert parse_autopilot_command("/bonehawk size 50") == ("size", ["50"])
    assert parse_autopilot_command("/autopilot@BonehawkBot status") == ("status", [])
    assert parse_autopilot_command("hello there") is None


def test_allowed_chat_requires_allowlist_match() -> None:
    assert is_allowed_chat("123", {"123", "456"}) is True
    assert is_allowed_chat(123, {"123"}) is True
    assert is_allowed_chat("999", {"123"}) is False


def test_handle_autopilot_command_runs_paper_scan_and_execution() -> None:
    service = FakeTelegramService()

    scan = handle_autopilot_command("/bh scan", service)
    run = handle_autopilot_command("/bh run", service)

    assert service.scanned is True
    assert service.executed is True
    assert "Scanned 40 symbols" in scan.message
    assert "Submitted 1 paper order" in run.message
    assert "paper-order" in run.message


def test_handle_autopilot_command_blocks_live_execution_from_telegram() -> None:
    service = FakeTelegramService(mode="live")

    response = handle_autopilot_command("/bh run", service)

    assert service.executed is False
    assert response.ok is False
    assert "paper mode only" in response.message


def test_handle_autopilot_command_updates_paper_safe_settings() -> None:
    service = FakeTelegramService()

    response = handle_autopilot_command("/bh size 50", service)
    disabled = handle_autopilot_command("/bh disable", service)

    assert response.ok is True
    assert disabled.ok is True
    assert service.settings == [("max_trade_usd", 50.0, ""), ("enabled", False, "")]


def test_handle_autopilot_command_pauses_resumes_and_kills_background() -> None:
    service = FakeTelegramService()

    paused = handle_autopilot_command("/bh pause", service)
    resumed = handle_autopilot_command("/bh resume", service)
    killed = handle_autopilot_command("/bh kill", service)

    assert paused.ok is True
    assert resumed.ok is True
    assert killed.ok is True
    assert service.background_started is True
    assert service.background_stopped is True
    assert service.settings == [("enabled", False, ""), ("enabled", True, ""), ("enabled", False, "")]


def test_handle_autopilot_command_reports_orders_positions_and_health() -> None:
    service = FakeTelegramService()

    orders = handle_autopilot_command("/bh orders", service)
    positions = handle_autopilot_command("/bh positions", service)
    health = handle_autopilot_command("/bh health", service)
    desk = handle_autopilot_command("/bh desk", service)

    assert orders.ok is True
    assert "paper-order" in orders.message
    assert "MSFT 2" in positions.message
    assert "Data health: healthy 90" in health.message
    assert "Best backtest: MSFT" in desk.message


def test_handle_autopilot_command_sends_recent_report() -> None:
    service = FakeTelegramService()

    response = handle_autopilot_command("/bh report", service)

    assert response.ok is True
    assert "Bonehawk Report" in response.message
    assert "Last 10m" in response.message
    assert "Portfolio: $1,250.50" in response.message
    assert "Open P/L: -$8.25 (-0.66%)" in response.message
    assert "Market trend: UP" in response.message
    assert "MSFT BUY submitted" in response.message
    assert "paper-order" in response.message


def test_telegram_bot_processes_only_allowed_chat_and_sends_reply(tmp_path: Path) -> None:
    sent_payloads: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getUpdates"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [
                        {"update_id": 7, "message": {"chat": {"id": 999}, "text": "/bh status"}},
                        {"update_id": 8, "message": {"chat": {"id": 123}, "text": "/bh status"}},
                    ],
                },
            )
        sent_payloads.append(dict(request.content.decode().split("&")[index].split("=", 1) for index in range(len(request.content.decode().split("&")))))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    bot = TelegramAutopilotBot(
        root=tmp_path,
        token="bot-token",
        allowed_chat_ids={"123"},
        service=FakeTelegramService(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = bot.poll_once()

    assert result["processed"] == 1
    assert result["ignored"] == 1
    assert (tmp_path / "logs" / "telegram_autopilot_offset.txt").read_text() == "9"
    assert len(sent_payloads) == 1
    assert sent_payloads[0]["chat_id"] == "123"
    assert "Autopilot%3A" in sent_payloads[0]["text"]
