from __future__ import annotations

import json
from pathlib import Path

from scripts.decision_log import latest_decisions, record_decisions


def test_record_decisions_appends_jsonl_rows(tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"

    rows = record_decisions(
        path,
        source="dashboard",
        ideas=[{"symbol": "AAPL", "action": "TRIM_REVIEW", "current_price": 294, "reason": "Open gain.", "quantity": 2}],
    )

    assert len(rows) == 1
    saved = json.loads(path.read_text().strip())
    assert saved["source"] == "dashboard"
    assert saved["symbol"] == "AAPL"
    assert saved["quantity"] == 2
    assert saved["review_only"] is True


def test_latest_decisions_returns_newest_first(tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"
    record_decisions(path, source="first", ideas=[{"symbol": "AAPL", "action": "HOLD_REVIEW"}])
    record_decisions(path, source="second", ideas=[{"symbol": "MSFT", "action": "BUY_REVIEW"}])

    rows = latest_decisions(path, limit=1)

    assert rows[0]["source"] == "second"
    assert rows[0]["symbol"] == "MSFT"
    assert rows[0]["action"] == "BUY_REVIEW"


def test_latest_decisions_handles_missing_log(tmp_path: Path) -> None:
    assert latest_decisions(tmp_path / "missing.jsonl") == []
