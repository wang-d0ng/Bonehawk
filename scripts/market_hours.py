from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

PREOPEN_QUEUE_WINDOW_MINUTES = 5


def evaluate_market_hours_gate(clock: dict[str, Any] | None, *, enabled: bool, state: dict[str, Any] | None = None, now: datetime | None = None) -> dict[str, Any]:
    if not clock:
        return {
            "status": "clock_unavailable",
            "can_execute": True,
            "queue_orders_for_market_open": False,
            "message": "Alpaca market clock is unavailable; market-hours automation was not changed.",
        }

    state = state or {}
    current = now or _parse_timestamp(clock.get("timestamp")) or datetime.now(UTC)
    next_open = _parse_timestamp(clock.get("next_open"))
    next_close = _parse_timestamp(clock.get("next_close"))
    minutes_to_open = _minutes_between(current, next_open)
    resume_at = next_open - timedelta(minutes=PREOPEN_QUEUE_WINDOW_MINUTES) if next_open else None
    auto_paused = bool(state.get("auto_paused"))

    payload = {
        "timestamp": current.isoformat(),
        "is_open": bool(clock.get("is_open")),
        "next_open": next_open.isoformat() if next_open else "",
        "next_close": next_close.isoformat() if next_close else "",
        "minutes_to_open": minutes_to_open,
        "resume_at": resume_at.isoformat() if resume_at else "",
        "queue_window_minutes": PREOPEN_QUEUE_WINDOW_MINUTES,
    }

    if payload["is_open"]:
        should_enable = auto_paused and not enabled
        return {
            **payload,
            "status": "market_open",
            "can_execute": True,
            "queue_orders_for_market_open": False,
            "should_enable_autopilot": should_enable,
            "should_disable_autopilot": False,
            "message": "Market is open. Autopilot can run normally.",
        }

    in_preopen_queue = next_open is not None and 0 <= (next_open - current).total_seconds() <= PREOPEN_QUEUE_WINDOW_MINUTES * 60
    if in_preopen_queue:
        should_enable = auto_paused and not enabled
        can_execute = enabled or should_enable
        return {
            **payload,
            "status": "preopen_queue" if can_execute else "preopen_user_disabled",
            "can_execute": can_execute,
            "queue_orders_for_market_open": can_execute,
            "should_enable_autopilot": should_enable,
            "should_disable_autopilot": False,
            "message": "Market opens within five minutes; autopilot can queue regular-hours orders for the open." if can_execute else "Market opens within five minutes, but autopilot was manually disabled.",
        }

    should_disable = enabled
    status = "market_closed_auto_paused" if auto_paused or should_disable else "market_closed_user_disabled"
    return {
        **payload,
        "status": status,
        "can_execute": False,
        "queue_orders_for_market_open": False,
        "should_enable_autopilot": False,
        "should_disable_autopilot": should_disable,
        "message": "Market is closed. Autopilot was turned off until five minutes before the next open." if status == "market_closed_auto_paused" else "Market is closed and autopilot is manually disabled.",
    }


def market_hours_state_after_gate(gate: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    previous = previous or {}
    if gate.get("should_disable_autopilot"):
        return {
            "auto_paused": True,
            "paused_at": gate.get("timestamp", ""),
            "resume_at": gate.get("resume_at", ""),
            "next_open": gate.get("next_open", ""),
            "reason": "market_closed",
        }
    if gate.get("should_enable_autopilot") or gate.get("status") == "market_open":
        return {
            **previous,
            "auto_paused": False,
            "resumed_at": gate.get("timestamp", ""),
            "next_open": gate.get("next_open", ""),
            "reason": gate.get("status", ""),
        }
    return previous


def _minutes_between(start: datetime, end: datetime | None) -> int | None:
    if end is None:
        return None
    return max(0, int(round((end - start).total_seconds() / 60)))


def _parse_timestamp(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
