from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts.readiness import (
    build_live_readiness_report,
    build_operational_health_report,
    build_paper_evidence_report,
    build_public_release_report,
    record_risk_acknowledgement,
    risk_acknowledgement_status,
)
from scripts.trading_desk import record_order_truth_event


def test_paper_evidence_report_marks_multi_session_paper_data_ready(tmp_path: Path) -> None:
    _seed_paper_evidence(tmp_path)

    payload = build_paper_evidence_report(tmp_path)

    assert payload["ok"] is True
    assert payload["status"] == "paper_ready"
    assert payload["summary"]["market_sessions"] == 3
    assert payload["summary"]["submitted"] == 10
    assert payload["summary"]["rejection_rate_pct"] == 10.0
    assert payload["summary"]["net_pnl"] > 0
    assert payload["checks"]["market_sessions"]["status"] == "pass"
    assert payload["checks"]["sample_size"]["status"] == "pass"
    assert payload["checks"]["rejection_rate"]["status"] == "pass"


def test_live_readiness_requires_paper_evidence_and_risk_acknowledgement(tmp_path: Path) -> None:
    locked = build_live_readiness_report(tmp_path, diagnostics={"ok": True, "summary": {"failed": 0}})

    assert locked["ok"] is False
    assert locked["status"] == "locked"
    assert locked["checks"]["paper_evidence"]["status"] == "fail"
    assert locked["checks"]["risk_acknowledgement"]["status"] == "fail"

    _seed_ready_install(tmp_path)
    _seed_paper_evidence(tmp_path)
    record_risk_acknowledgement(tmp_path, accepted=True, actor="tester", now=datetime(2026, 6, 26, 12, 0, tzinfo=UTC))

    ready = build_live_readiness_report(tmp_path, diagnostics={"ok": True, "summary": {"failed": 0}})

    assert ready["ok"] is True
    assert ready["status"] == "eligible"
    assert ready["checks"]["paper_evidence"]["status"] == "pass"
    assert ready["checks"]["risk_acknowledgement"]["status"] == "pass"
    assert ready["paper_evidence"]["status"] == "paper_ready"


def test_risk_acknowledgement_records_no_secret_values(tmp_path: Path) -> None:
    payload = record_risk_acknowledgement(tmp_path, accepted=True, actor="Bento", now=datetime(2026, 6, 26, 12, 0, tzinfo=UTC))
    status = risk_acknowledgement_status(tmp_path)

    assert payload["ok"] is True
    assert status["status"] == "pass"
    assert status["accepted"] is True
    assert "secret" not in json.dumps(status).lower()


def test_public_release_report_requires_packaged_smoke_and_notarization(tmp_path: Path) -> None:
    _seed_ready_install(tmp_path)
    _seed_paper_evidence(tmp_path)
    record_risk_acknowledgement(tmp_path, accepted=True, actor="tester", now=datetime(2026, 6, 26, 12, 0, tzinfo=UTC))
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "dist" / "Bonehawk.app" / "Contents").mkdir(parents=True)
    (tmp_path / "dist" / "Bonehawk-0.1.2-macOS-arm64.dmg").write_bytes(b"dmg")
    (tmp_path / "dist" / "Bonehawk-0.1.2-macOS-arm64.dmg.sha256").write_text("abc  dmg\n")

    missing = build_public_release_report(tmp_path, version="0.1.2")

    assert missing["ok"] is False
    assert missing["checks"]["packaged_e2e"]["status"] == "fail"
    assert missing["checks"]["code_signing"]["status"] == "fail"

    (tmp_path / "logs").mkdir(exist_ok=True)
    (tmp_path / "logs" / "packaged_smoke.json").write_text(json.dumps({"ok": True, "status": "pass"}))
    (tmp_path / "dist" / "notarization.json").write_text(json.dumps({"signed": True, "status": "accepted"}))

    ready = build_public_release_report(tmp_path, version="0.1.2")

    assert ready["ok"] is True
    assert ready["checks"]["packaged_e2e"]["status"] == "pass"
    assert ready["checks"]["code_signing"]["status"] == "pass"


def test_operational_health_report_combines_runtime_signals(tmp_path: Path) -> None:
    _seed_ready_install(tmp_path)
    record_risk_acknowledgement(tmp_path, accepted=True, actor="tester", now=datetime(2026, 6, 26, 12, 0, tzinfo=UTC))

    payload = build_operational_health_report(
        tmp_path,
        setup_diagnostics={"ok": True, "summary": {"failed": 0}},
        trading_desk={"data_health": {"status": "healthy", "score": 95}, "order_truth": {"summary": {"active": 1, "rejected": 0}}},
        background={"running": True, "last_error": "", "runs": 4},
        market_hours={"status": "market_open", "can_execute": True},
    )

    assert payload["ok"] is True
    assert payload["status"] == "healthy"
    assert payload["checks"]["setup"]["status"] == "pass"
    assert payload["checks"]["background_loop"]["status"] == "pass"


def _seed_ready_install(root: Path) -> None:
    (root / ".env").write_text(
        "ALPACA_API_KEY=paper-key\n"
        "ALPACA_SECRET_KEY=paper-secret\n"
        "ALPACA_PAPER=true\n"
        "ALPACA_ALLOW_LIVE=false\n"
        "BONEHAWK_SETUP_COMPLETE=true\n"
    )
    (root / "config").mkdir(exist_ok=True)
    (root / "config" / "autopilot.json").write_text(json.dumps({"enabled": True, "mode": "paper"}))
    (root / "README.md").write_text("This is trading software, not financial advice, and can lose money quickly.\n")
    (root / "dist").mkdir(exist_ok=True)


def _seed_paper_evidence(root: Path) -> None:
    start = datetime(2026, 6, 22, 14, 0, tzinfo=UTC)
    for index in range(10):
        timestamp = start + timedelta(days=index // 4, minutes=index)
        status = "rejected" if index == 8 else "submitted"
        broker_status = "rejected" if index == 8 else "filled"
        fill_status = "rejected" if index == 8 else "filled"
        record_order_truth_event(
            root,
            "autopilot_order",
            {
                "symbol": f"T{index}",
                "side": "BUY",
                "status": status,
                "broker_status": broker_status,
                "broker_order_id": f"paper-{index}",
                "filled_quantity": 0 if index == 8 else 1,
                "filled_average_price": 100 + index,
                "fill_status": fill_status,
                "review_only": False,
            },
            now=timestamp,
        )
    outcomes = [5, -2, 6, 4, -1, 3, 2, 1, -1, 1]
    rows = [
        {
            "timestamp": (start + timedelta(days=index // 4, minutes=index + 10)).isoformat(),
            "symbol": f"T{index}",
            "realized_pnl": pnl,
            "strategy": "momentum_breakout",
            "review_only": False,
        }
        for index, pnl in enumerate(outcomes)
    ]
    path = root / "logs" / "trade_outcomes.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
