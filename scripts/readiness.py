from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.trading_desk import order_truth_snapshot, trade_journal_snapshot

RISK_ACK_FILE = Path("state") / "risk_acknowledgement.json"
PACKAGED_SMOKE_FILE = Path("logs") / "packaged_smoke.json"
NOTARIZATION_FILE = Path("dist") / "notarization.json"

MIN_PAPER_SESSIONS = 3
MIN_PAPER_ORDERS = 10
MAX_REJECTION_RATE_PCT = 10.0
MAX_DRAWDOWN_PCT = 5.0


def record_risk_acknowledgement(
    root: Path,
    *,
    accepted: bool,
    actor: str = "local_user",
    now: datetime | None = None,
) -> dict[str, Any]:
    if not accepted:
        return {"ok": False, "status": "not_accepted", "message": "Risk acknowledgement must be accepted before setup can complete."}
    payload = {
        "accepted": True,
        "actor": _safe_actor(actor),
        "accepted_at": (now or datetime.now(UTC)).astimezone(UTC).isoformat(),
        "version": 1,
        "message": "User acknowledged that Bonehawk is not financial advice and automated trading can lose money.",
    }
    path = root / RISK_ACK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"ok": True, "status": "accepted", "acknowledgement": _public_ack(payload)}


def risk_acknowledgement_status(root: Path) -> dict[str, Any]:
    payload = _read_json(root / RISK_ACK_FILE)
    accepted = bool(payload.get("accepted"))
    return {
        "ok": accepted,
        "status": "pass" if accepted else "fail",
        "accepted": accepted,
        "accepted_at": payload.get("accepted_at", ""),
        "actor": payload.get("actor", ""),
        "message": "Risk acknowledgement is recorded." if accepted else "Accept the risk acknowledgement in first-run setup.",
    }


def build_paper_evidence_report(root: Path, *, min_sessions: int = MIN_PAPER_SESSIONS, min_orders: int = MIN_PAPER_ORDERS) -> dict[str, Any]:
    truth = order_truth_snapshot(root, limit=1000)
    events = truth.get("current") or truth.get("events") or []
    attempts = [_order_attempt(event) for event in events if _is_order_attempt(event)]
    attempts = [event for event in attempts if event]
    rejected = [event for event in attempts if event.get("stage") == "rejected"]
    filled = [event for event in attempts if event.get("stage") == "filled"]
    canceled = [event for event in attempts if event.get("stage") == "canceled"]
    sessions = sorted({str(event.get("session") or "") for event in attempts if event.get("session")})
    outcomes = _outcome_rows(root)
    pnl_values = [_safe_float(row.get("realized_pnl") or row.get("pnl")) for row in outcomes]
    wins = [value for value in pnl_values if value > 0]
    losses = [value for value in pnl_values if value < 0]
    rejection_rate = (len(rejected) / len(attempts) * 100) if attempts else 100.0
    max_drawdown = _max_drawdown(pnl_values)
    max_drawdown_pct = (max_drawdown / max(1000.0, sum(abs(value) for value in pnl_values), 1.0)) * 100
    net_pnl = round(sum(pnl_values), 2)
    checks = {
        "market_sessions": _check(len(sessions) >= min_sessions, f"{len(sessions)} market session(s) captured.", f"Run paper mode across at least {min_sessions} market sessions."),
        "sample_size": _check(len(attempts) >= min_orders, f"{len(attempts)} broker order attempt(s) captured.", f"Capture at least {min_orders} paper order attempts."),
        "rejection_rate": _check(rejection_rate <= MAX_REJECTION_RATE_PCT, f"Rejection rate is {rejection_rate:.1f}%.", f"Reduce rejected orders below {MAX_REJECTION_RATE_PCT:.1f}%."),
        "drawdown": _check(max_drawdown_pct <= MAX_DRAWDOWN_PCT, f"Max drawdown is {max_drawdown_pct:.2f}%.", f"Reduce drawdown below {MAX_DRAWDOWN_PCT:.1f}%."),
        "profitability": _check(bool(pnl_values) and net_pnl >= 0, f"Net paper P/L is ${net_pnl:.2f}.", "Collect non-negative paper P/L before live readiness."),
    }
    summary = {
        "market_sessions": len(sessions),
        "submitted": len(attempts),
        "filled": len(filled),
        "rejected": len(rejected),
        "canceled": len(canceled),
        "rejection_rate_pct": round(rejection_rate, 1) if attempts else 100.0,
        "win_rate_pct": round((len(wins) / (len(wins) + len(losses))) * 100, 1) if wins or losses else 0,
        "net_pnl": net_pnl,
        "max_drawdown": round(max_drawdown, 2),
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "outcomes": len(pnl_values),
    }
    failed = sum(1 for check in checks.values() if check["status"] == "fail")
    return {
        "ok": failed == 0,
        "status": "paper_ready" if failed == 0 else "collecting",
        "summary": summary,
        "checks": checks,
        "sessions": sessions,
        "message": "Paper evidence is based on local Order Truth and trade outcome logs.",
    }


def build_live_readiness_report(root: Path, *, diagnostics: dict[str, Any] | None = None) -> dict[str, Any]:
    diagnostics = diagnostics or {}
    paper = build_paper_evidence_report(root)
    ack = risk_acknowledgement_status(root)
    setup_ok = bool(diagnostics.get("ok")) and int((diagnostics.get("summary") or {}).get("failed", 0)) == 0
    checks = {
        "setup_diagnostics": _check(setup_ok, "Setup diagnostics are clean.", "Fix setup diagnostics before arming live mode."),
        "paper_evidence": _check(bool(paper.get("ok")), "Paper evidence meets live-readiness thresholds.", "Collect more paper evidence before live mode."),
        "risk_acknowledgement": _check(bool(ack.get("accepted")), "Risk acknowledgement is recorded.", "Accept the risk acknowledgement before live mode."),
    }
    failed = sum(1 for check in checks.values() if check["status"] == "fail")
    return {
        "ok": failed == 0,
        "status": "eligible" if failed == 0 else "locked",
        "checks": checks,
        "paper_evidence": paper,
        "risk_acknowledgement": ack,
        "message": "Live mode stays locked until setup, paper evidence, and risk acknowledgement all pass.",
    }


def build_public_release_report(root: Path, *, version: str = "") -> dict[str, Any]:
    paper = build_paper_evidence_report(root)
    ack = risk_acknowledgement_status(root)
    checks = {
        "paper_evidence": _check(bool(paper.get("ok")), "Paper evidence is strong enough for release review.", "Collect more paper evidence."),
        "risk_acknowledgement": _check(bool(ack.get("accepted")), "Risk acknowledgement flow has been completed locally.", "Complete the first-run risk acknowledgement."),
        "release_dmg": _release_dmg_check(root, version),
        "app_bundle": _check((root / "dist" / "Bonehawk.app").exists(), "Desktop app bundle is present.", "Build dist/Bonehawk.app."),
        "packaged_e2e": _packaged_smoke_check(root),
        "code_signing": _code_signing_check(root),
    }
    failed = sum(1 for check in checks.values() if check["status"] == "fail")
    return {
        "ok": failed == 0,
        "status": "public_release_ready" if failed == 0 else "not_ready",
        "summary": _summary(checks),
        "checks": checks,
        "paper_evidence": paper,
        "message": "Public release readiness requires paper evidence, packaged smoke testing, signing, notarization, and release artifacts.",
    }


def build_operational_health_report(
    root: Path,
    *,
    setup_diagnostics: dict[str, Any],
    trading_desk: dict[str, Any],
    background: dict[str, Any],
    market_hours: dict[str, Any],
) -> dict[str, Any]:
    data_health = trading_desk.get("data_health") or {}
    order_summary = ((trading_desk.get("order_truth") or {}).get("summary") or {})
    checks = {
        "setup": _check(bool(setup_diagnostics.get("ok")), "Setup diagnostics are clean.", "Fix setup diagnostics."),
        "data_health": _check(str(data_health.get("status")) in {"healthy", "degraded"}, f"Data health is {data_health.get('status', 'unknown')}.", "Recover market/account data feeds."),
        "orders": _check(int(_safe_float(order_summary.get("rejected"))) == 0, "No current rejected orders.", "Review rejected orders in Live View."),
        "background_loop": _check(bool(background.get("running")) and not background.get("last_error"), "Background paper loop is running.", "Start the background loop or clear its last error."),
        "market_hours": _check(bool(market_hours.get("status")), f"Market status is {market_hours.get('status', 'unknown')}.", "Refresh Alpaca market clock."),
        "risk_acknowledgement": _check(bool(risk_acknowledgement_status(root).get("accepted")), "Risk acknowledgement is recorded.", "Complete first-run risk acknowledgement."),
    }
    failed = sum(1 for check in checks.values() if check["status"] == "fail")
    warn = sum(1 for check in checks.values() if check["status"] == "warn")
    status = "healthy" if failed == 0 and warn == 0 else "degraded" if failed <= 1 else "unsafe"
    return {
        "ok": failed == 0,
        "status": status,
        "summary": _summary(checks),
        "checks": checks,
        "data_health": data_health,
        "order_summary": order_summary,
        "background": {
            "running": bool(background.get("running")),
            "runs": background.get("runs", 0),
            "last_error": background.get("last_error", ""),
        },
        "market_hours": market_hours,
        "message": "Operational health combines setup, data, order, loop, market, and risk acknowledgement status.",
    }


def _order_attempt(event: dict[str, Any]) -> dict[str, Any]:
    timestamp = _parse_timestamp(event.get("timestamp"))
    return {**event, "session": timestamp.date().isoformat() if timestamp else ""}


def _is_order_attempt(event: dict[str, Any]) -> bool:
    return bool(event.get("broker_order_id")) and bool(event.get("review_only") is False)


def _outcome_rows(root: Path) -> list[dict[str, Any]]:
    outcomes = _jsonl_rows(root / "logs" / "trade_outcomes.jsonl")
    journal = [row for row in trade_journal_snapshot(root, limit=1000).get("entries", []) if row.get("realized_pnl") is not None]
    return [*outcomes, *journal]


def _max_drawdown(values: list[float]) -> float:
    peak = 0.0
    equity = 0.0
    drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def _release_dmg_check(root: Path, version: str) -> dict[str, Any]:
    dist = root / "dist"
    pattern = f"Bonehawk-{version}-macOS-arm64.dmg" if version else "Bonehawk-*-macOS-arm64.dmg"
    matches = list(dist.glob(pattern)) if dist.exists() else []
    dmg = matches[0] if matches else None
    checksum = Path(str(dmg) + ".sha256") if dmg else None
    ok = bool(dmg and checksum and checksum.exists())
    return _check(ok, f"Release DMG and checksum are present: {dmg.name if dmg else 'missing'}.", "Build a DMG and .sha256 checksum.")


def _packaged_smoke_check(root: Path) -> dict[str, Any]:
    payload = _read_json(root / PACKAGED_SMOKE_FILE)
    ok = bool(payload.get("ok")) or str(payload.get("status")) == "pass"
    return _check(ok, "Packaged app smoke test passed.", "Run the packaged app smoke test and write logs/packaged_smoke.json.")


def _code_signing_check(root: Path) -> dict[str, Any]:
    payload = _read_json(root / NOTARIZATION_FILE)
    ok = bool(payload.get("signed")) and str(payload.get("status")) in {"accepted", "notarized"}
    return _check(ok, "Code signing and notarization receipt is present.", "Sign and notarize the macOS app, then save dist/notarization.json.")


def _summary(checks: dict[str, dict[str, Any]]) -> dict[str, int]:
    return {
        "passed": sum(1 for check in checks.values() if check["status"] == "pass"),
        "warn": sum(1 for check in checks.values() if check["status"] == "warn"),
        "failed": sum(1 for check in checks.values() if check["status"] == "fail"),
        "total": len(checks),
    }


def _check(ok: bool, message: str, recovery: str, *, warn: bool = False) -> dict[str, Any]:
    return {
        "status": "pass" if ok else "warn" if warn else "fail",
        "message": message if ok else recovery,
        "recovery": "" if ok else recovery,
    }


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _jsonl_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _parse_timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _safe_float(value: Any) -> float:
    try:
        if value is None or value == "":
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _safe_actor(value: str) -> str:
    actor = str(value or "local_user").strip()[:80]
    return actor or "local_user"


def _public_ack(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: payload.get(key) for key in ("accepted", "actor", "accepted_at", "version", "message")}
