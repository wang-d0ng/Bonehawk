from __future__ import annotations

import json
from pathlib import Path

from scripts.dashboard import DashboardService
from scripts.packaged_smoke import run_packaged_smoke


class SmokeAlpacaClient:
    def snapshot(self):
        return {"status": "connected", "api_key": "set", "secret_key": "set", "paper": True}

    def get_account(self):
        return {"status": "ACTIVE", "cash": "1000", "buying_power": "1000", "portfolio_value": "1000"}

    def get_positions(self):
        return []

    def get_clock(self):
        return {"timestamp": "2026-06-26T13:30:00Z", "is_open": True}

    def get_calendar(self, start: str, end: str):
        return [{"date": start, "open": "09:30", "close": "16:00"}]


def test_packaged_smoke_writes_pass_receipt_for_app_bundle(tmp_path: Path) -> None:
    _seed_project(tmp_path)
    (tmp_path / "dist" / "Bonehawk.app" / "Contents").mkdir(parents=True)
    service = DashboardService(root=tmp_path, alpaca_client=SmokeAlpacaClient())

    payload = run_packaged_smoke(tmp_path, service=service)

    assert payload["ok"] is True
    assert payload["checks"]["app_bundle"]["status"] == "pass"
    assert payload["checks"]["critical_routes"]["status"] == "pass"
    receipt = json.loads((tmp_path / "logs" / "packaged_smoke.json").read_text())
    assert receipt["ok"] is True


def test_packaged_smoke_fails_when_app_bundle_is_missing(tmp_path: Path) -> None:
    _seed_project(tmp_path)
    service = DashboardService(root=tmp_path, alpaca_client=SmokeAlpacaClient())

    payload = run_packaged_smoke(tmp_path, service=service)

    assert payload["ok"] is False
    assert payload["checks"]["app_bundle"]["status"] == "fail"


def _seed_project(root: Path) -> None:
    (root / ".env").write_text("ALPACA_API_KEY=paper-key\nALPACA_SECRET_KEY=paper-secret\nBONEHAWK_SETUP_COMPLETE=true\n")
    (root / "config").mkdir()
    (root / "config" / "autopilot.json").write_text(json.dumps({"enabled": True, "mode": "paper"}))
    (root / "README.md").write_text("This is trading software, not financial advice, and can lose money quickly.\n")
