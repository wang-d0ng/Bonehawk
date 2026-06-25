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

    def tickets(self):
        return {
            "tickets": [
                {"symbol": "MSFT", "side": "BUY", "status": "submitted", "broker_status": "accepted", "fill_status": "not_filled_yet", "broker_order_id": "paper-order"}
            ]
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
