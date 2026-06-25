from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def record_decisions(path: Path, source: str, ideas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    timestamp = datetime.now(UTC).isoformat()
    rows = [_row(timestamp, source, idea) for idea in ideas]
    if not rows:
        return []
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    return rows


def latest_decisions(path: Path, limit: int = 50) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(rows))[:limit]


def _row(timestamp: str, source: str, idea: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": timestamp,
        "source": source,
        "symbol": idea.get("symbol"),
        "side": idea.get("side"),
        "action": idea.get("action"),
        "confidence": idea.get("confidence"),
        "current_price": idea.get("current_price"),
        "stop_loss": idea.get("stop_loss"),
        "take_profit": idea.get("take_profit"),
        "quantity": idea.get("quantity"),
        "status": idea.get("status"),
        "broker_status": idea.get("broker_status"),
        "broker_order_id": idea.get("broker_order_id"),
        "detail": idea.get("detail"),
        "request_id": idea.get("request_id"),
        "filled_quantity": idea.get("filled_quantity"),
        "filled_average_price": idea.get("filled_average_price"),
        "fill_status": idea.get("fill_status"),
        "reason": idea.get("reason"),
        "signals": idea.get("signals"),
        "review_only": idea.get("review_only", True),
    }
