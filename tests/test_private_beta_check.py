from __future__ import annotations

import json
from pathlib import Path

from scripts.readiness import record_risk_acknowledgement
from scripts.private_beta_check import build_private_beta_report


def test_private_beta_report_marks_ready_install_with_release_asset(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "ALPACA_API_KEY=paper-key\n"
        "ALPACA_SECRET_KEY=paper-secret\n"
        "ALPACA_PAPER=true\n"
        "ALPACA_ALLOW_LIVE=false\n"
        "BONEHAWK_SETUP_COMPLETE=true\n"
    )
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "autopilot.json").write_text(json.dumps({"enabled": False, "mode": "paper"}))
    (tmp_path / "README.md").write_text("This project is trading software, not financial advice, and can lose money quickly.\n")
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "Bonehawk-0.1.2-macOS-arm64.dmg").write_bytes(b"dmg")
    (tmp_path / "dist" / "Bonehawk-0.1.2-macOS-arm64.dmg.sha256").write_text("abc  dmg\n")
    (tmp_path / "tests").mkdir()
    record_risk_acknowledgement(tmp_path, accepted=True, actor="tester")

    payload = build_private_beta_report(tmp_path, version="0.1.2")

    assert payload["ok"] is True
    assert payload["summary"]["failed"] == 0
    assert payload["checks"]["release_dmg"]["status"] == "pass"
    assert payload["checks"]["live_mode"]["status"] == "pass"
    assert payload["checks"]["risk_acknowledgement"]["status"] == "pass"


def test_private_beta_report_blocks_missing_disclaimer_and_live_mode(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "ALPACA_API_KEY=paper-key\n"
        "ALPACA_SECRET_KEY=paper-secret\n"
        "ALPACA_PAPER=false\n"
        "ALPACA_ALLOW_LIVE=true\n"
    )
    (tmp_path / "README.md").write_text("No disclosure here.\n")

    payload = build_private_beta_report(tmp_path, version="0.1.2")

    assert payload["ok"] is False
    assert payload["checks"]["risk_disclosure"]["status"] == "fail"
    assert payload["checks"]["live_mode"]["status"] == "fail"
    assert payload["checks"]["risk_acknowledgement"]["status"] == "fail"
