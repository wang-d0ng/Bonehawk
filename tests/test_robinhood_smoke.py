from __future__ import annotations

import json

from scripts import robinhood_smoke
from scripts.robinhood import RobinhoodConfig


def test_robinhood_smoke_prints_sanitized_snapshot(monkeypatch, capsys) -> None:
    monkeypatch.setattr(robinhood_smoke.RobinhoodConfig, "from_env", lambda path: RobinhoodConfig("", "", None, "v2", "paper"))

    robinhood_smoke.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "not_configured"
