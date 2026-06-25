from __future__ import annotations

from datetime import time

from scripts.daily_scheduler import due_alerts, run_alert, parse_schedule


def test_parse_schedule_uses_defaults() -> None:
    schedule = parse_schedule({})

    assert schedule["morning"] == time(9, 0)
    assert schedule["midday"] == time(12, 30)
    assert schedule["end_of_day"] == time(16, 10)


def test_due_alerts_returns_not_sent_jobs() -> None:
    schedule = parse_schedule({"morning": "09:00", "midday": "12:30", "end_of_day": "16:10"})

    due = due_alerts(now=time(12, 45), schedule=schedule, sent={"morning"})

    assert due == ["midday"]


def test_due_alerts_waits_until_time() -> None:
    schedule = parse_schedule({"morning": "09:00"})

    assert due_alerts(now=time(8, 59), schedule=schedule, sent=set()) == []


def test_run_alert_routes_to_service_methods() -> None:
    calls = []

    class FakeService:
        def trade_idea_alerts(self):
            calls.append("morning")
            return {"ok": True}

        def scanner_alerts(self):
            calls.append("midday")
            return {"ok": True}

    assert run_alert("morning", FakeService())["ok"] is True
    assert run_alert("midday", FakeService())["ok"] is True
    assert calls == ["morning", "midday"]


def test_run_alert_rejects_unknown_kind() -> None:
    class FakeService:
        pass

    try:
        run_alert("bad", FakeService())
    except ValueError as error:
        assert "Unknown alert kind" in str(error)
    else:
        raise AssertionError("expected ValueError")
