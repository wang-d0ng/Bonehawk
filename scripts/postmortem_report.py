from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


LEARNING_PATH = Path("config") / "autopilot_learning.json"
LEARNING_LOG_PATH = Path("logs") / "postmortem_updates.jsonl"
LEARNING_MEMORY_PATH = Path("memory") / "POSTMORTEM-LEARNINGS.md"


def build_loss_postmortem_report(root: Path, *, window_hours: int = 24, now: datetime | None = None) -> dict[str, Any]:
    current = now or datetime.now(UTC)
    hours = int(_clamp(_safe_float(window_hours, 24), 1, 168))
    cutoff = current - timedelta(hours=hours)
    trades = _recent_losing_trades(root, cutoff)
    summary = _loss_summary(trades)
    patterns = _loss_patterns(trades)
    issues = _loss_issues(summary, patterns)
    suggested_updates = _suggested_updates(summary, patterns, issues, current)
    report_id = _report_id(current, hours, trades)
    return {
        "ok": True,
        "status": "ready",
        "report_id": report_id,
        "generated_at": current.isoformat(),
        "window_hours": hours,
        "summary": summary,
        "patterns": patterns,
        "issues": issues,
        "suggested_updates": suggested_updates,
        "trades": trades,
        "notice": "Post-mortem report is review-only and does not place orders. Apply learnings only after reviewing the report.",
    }


def apply_loss_postmortem_report(root: Path, report: dict[str, Any], *, reviewer_notes: str = "", now: datetime | None = None) -> dict[str, Any]:
    if not report.get("ok"):
        return {"ok": False, "status": "invalid_report", "message": "Run a valid post-mortem report before applying learnings."}
    if int((report.get("summary") or {}).get("loss_count") or 0) <= 0:
        return {"ok": False, "status": "no_losses", "message": "No losing trades were found in the report window."}
    current = now or datetime.now(UTC)
    learning = _build_learning_payload(report, reviewer_notes, current)
    learning_path = root / LEARNING_PATH
    learning_path.parent.mkdir(parents=True, exist_ok=True)
    learning_path.write_text(json.dumps(learning, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _append_jsonl(root / LEARNING_LOG_PATH, {"timestamp": current.isoformat(), "status": "applied", "learning": learning})
    _append_learning_memory(root / LEARNING_MEMORY_PATH, learning, report)
    return {
        "ok": True,
        "status": "applied",
        "learning": learning,
        "message": "Reviewed post-mortem learnings were written to system memory and autopilot guardrails.",
    }


def load_active_postmortem_learnings(root: Path, *, now: datetime | None = None) -> dict[str, Any]:
    path = root / LEARNING_PATH
    if not path.exists():
        return _empty_learning()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_learning()
    if not isinstance(raw, dict):
        return _empty_learning()
    current = now or datetime.now(UTC)
    active_until = _parse_timestamp(raw.get("active_until"))
    if active_until is not None and active_until < current:
        return _empty_learning()
    cooldown_symbols = {}
    for symbol, detail in (raw.get("cooldown_symbols") or {}).items():
        normalized = str(symbol or "").upper()
        if not normalized or not isinstance(detail, dict):
            continue
        until = _parse_timestamp(detail.get("until"))
        if until is not None and until < current:
            continue
        cooldown_symbols[normalized] = detail
    return {
        "active": bool(cooldown_symbols or raw.get("risk")),
        "last_report_id": raw.get("last_report_id"),
        "updated_at": raw.get("updated_at"),
        "active_until": raw.get("active_until"),
        "cooldown_symbols": cooldown_symbols,
        "risk": raw.get("risk") if isinstance(raw.get("risk"), dict) else {},
        "issues": raw.get("issues") if isinstance(raw.get("issues"), list) else [],
        "reviewer_notes": str(raw.get("reviewer_notes") or ""),
    }


def _recent_losing_trades(root: Path, cutoff: datetime) -> list[dict[str, Any]]:
    rows = [
        *_jsonl_rows(root / "logs" / "trade_outcomes.jsonl"),
        *_jsonl_rows(root / "logs" / "decision_log.jsonl"),
    ]
    trades: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        realized = _realized_pnl(row)
        if realized >= 0:
            continue
        timestamp = _parse_timestamp(row.get("timestamp"))
        if timestamp is None or timestamp < cutoff:
            continue
        key = str(row.get("outcome_key") or row.get("broker_order_id") or _fallback_trade_key(row))
        if key in seen:
            continue
        seen.add(key)
        trades.append(_clean_trade(row, realized, timestamp))
    return sorted(trades, key=lambda item: item["timestamp"], reverse=True)


def _loss_summary(trades: list[dict[str, Any]]) -> dict[str, Any]:
    total = round(sum(_safe_float(trade.get("realized_pnl")) for trade in trades), 2)
    largest = min(trades, key=lambda trade: _safe_float(trade.get("realized_pnl")), default=None)
    return {
        "loss_count": len(trades),
        "total_realized_pnl": total,
        "average_loss": round(total / len(trades), 2) if trades else 0,
        "largest_loss": largest or {},
        "symbols": len({trade.get("symbol") for trade in trades if trade.get("symbol")}),
    }


def _loss_patterns(trades: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    symbol_losses: dict[str, float] = defaultdict(float)
    symbol_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    signal_counts: Counter[str] = Counter()
    for trade in trades:
        symbol = str(trade.get("symbol") or "UNKNOWN").upper()
        symbol_counts[symbol] += 1
        symbol_losses[symbol] += _safe_float(trade.get("realized_pnl"))
        source_counts[str(trade.get("source") or "unknown")] += 1
        reason = _normalize_text(trade.get("reason"))
        if reason:
            reason_counts[reason] += 1
        for signal in trade.get("signals") or []:
            normalized_signal = _normalize_text(signal)
            if normalized_signal:
                signal_counts[normalized_signal] += 1
    symbols = [
        {"symbol": symbol, "count": count, "realized_pnl": round(symbol_losses[symbol], 2)}
        for symbol, count in symbol_counts.most_common()
    ]
    return {
        "symbols": symbols,
        "sources": [{"source": key, "count": value} for key, value in source_counts.most_common(8)],
        "reasons": [{"reason": key, "count": value} for key, value in reason_counts.most_common(8)],
        "signals": [{"signal": key, "count": value} for key, value in signal_counts.most_common(10)],
    }


def _loss_issues(summary: dict[str, Any], patterns: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for row in patterns.get("symbols", []):
        if int(row.get("count") or 0) >= 2:
            issues.append(
                {
                    "kind": "repeated_symbol_loss",
                    "severity": "high",
                    "title": f"{row['symbol']} lost more than once in the report window.",
                    "detail": f"{row['symbol']} produced {row['count']} losing trade(s), totaling ${_safe_float(row.get('realized_pnl')):.2f}.",
                }
            )
    signal_text = " ".join(row.get("signal", "") for row in patterns.get("signals", []))
    reason_text = " ".join(row.get("reason", "") for row in patterns.get("reasons", []))
    combined = f"{signal_text} {reason_text}"
    if any(term in combined for term in ("market down", "trend down", "bear", "negative")):
        issues.append(
            {
                "kind": "market_confirmation",
                "severity": "medium",
                "title": "Losses cluster around weak market or negative narrative signals.",
                "detail": "Require stronger price confirmation before new buys with similar market context.",
            }
        )
    if any(term in combined for term in ("stop", "failed", "faded", "slippage", "spread")):
        issues.append(
            {
                "kind": "execution_quality",
                "severity": "medium",
                "title": "Execution or confirmation quality appears in losing trades.",
                "detail": "Review entry timing, stop distance, spread, and whether volume faded after entry.",
            }
        )
    if int(summary.get("loss_count") or 0) >= 1:
        issues.append(
            {
                "kind": "risk_throttle",
                "severity": "medium",
                "title": "Recent losses justify a temporary risk throttle.",
                "detail": "Cut the next dynamic sizing budget until the next clean review window.",
            }
        )
    return issues


def _suggested_updates(summary: dict[str, Any], patterns: dict[str, list[dict[str, Any]]], issues: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    updates: list[dict[str, Any]] = []
    cooldown_until = (now + timedelta(hours=6)).isoformat()
    for row in patterns.get("symbols", []):
        if int(row.get("count") or 0) >= 2 or int(summary.get("loss_count") or 0) == 1:
            updates.append(
                {
                    "type": "cooldown_symbol",
                    "symbol": row.get("symbol"),
                    "until": cooldown_until,
                    "reason": "Recent loss pattern needs review before another buy.",
                }
            )
    if any(issue.get("kind") == "risk_throttle" for issue in issues):
        updates.append({"type": "risk_throttle", "max_next_risk_fraction": 0.5, "until": cooldown_until})
    if any(issue.get("kind") == "market_confirmation" for issue in issues):
        updates.append({"type": "require_price_confirmation", "enabled": True, "until": cooldown_until})
    return updates


def _build_learning_payload(report: dict[str, Any], reviewer_notes: str, now: datetime) -> dict[str, Any]:
    suggested = report.get("suggested_updates") if isinstance(report.get("suggested_updates"), list) else []
    until_values = [_parse_timestamp(update.get("until")) for update in suggested if isinstance(update, dict)]
    active_until = max([value for value in until_values if value is not None], default=None)
    active_until = active_until or (now + timedelta(hours=6))
    cooldown_symbols = {}
    for update in suggested:
        if not isinstance(update, dict) or update.get("type") != "cooldown_symbol":
            continue
        symbol = str(update.get("symbol") or "").upper()
        if not symbol:
            continue
        cooldown_symbols[symbol] = {
            "until": update.get("until") or active_until.isoformat(),
            "reason": str(update.get("reason") or "Reviewed post-mortem cooldown."),
        }
    risk_updates = [update for update in suggested if isinstance(update, dict) and update.get("type") == "risk_throttle"]
    confirmation_updates = [update for update in suggested if isinstance(update, dict) and update.get("type") == "require_price_confirmation"]
    max_next_risk_fraction = min([_safe_float(update.get("max_next_risk_fraction"), 1) for update in risk_updates], default=1)
    return {
        "version": 1,
        "last_report_id": report.get("report_id"),
        "updated_at": now.isoformat(),
        "active_until": active_until.isoformat(),
        "cooldown_symbols": cooldown_symbols,
        "risk": {
            "max_next_risk_fraction": _clamp(max_next_risk_fraction, 0.1, 1),
            "require_price_confirmation": bool(confirmation_updates),
        },
        "issues": report.get("issues") if isinstance(report.get("issues"), list) else [],
        "reviewer_notes": _limit_text(reviewer_notes, 1000),
    }


def _append_learning_memory(path: Path, learning: dict[str, Any], report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"## {learning.get('updated_at', '')}",
        "",
        f"- Report: {learning.get('last_report_id', '')}",
        f"- Window: {report.get('window_hours', 24)}h",
        f"- Losses: {(report.get('summary') or {}).get('loss_count', 0)}",
        f"- Total P&L: ${(report.get('summary') or {}).get('total_realized_pnl', 0)}",
        f"- Cooldowns: {', '.join(sorted((learning.get('cooldown_symbols') or {}).keys())) or 'none'}",
        f"- Risk throttle: {(learning.get('risk') or {}).get('max_next_risk_fraction', 1)}",
    ]
    if learning.get("reviewer_notes"):
        lines.append(f"- Reviewer notes: {learning['reviewer_notes']}")
    lines.append("")
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def _clean_trade(row: dict[str, Any], realized: float, timestamp: datetime) -> dict[str, Any]:
    return {
        "timestamp": timestamp.isoformat(),
        "symbol": str(row.get("symbol") or "UNKNOWN").upper(),
        "source": str(row.get("source") or "trade_outcome"),
        "action": row.get("action"),
        "side": row.get("side"),
        "realized_pnl": round(realized, 2),
        "reason": _limit_text(row.get("reason"), 300),
        "signals": [_limit_text(signal, 160) for signal in _as_string_list(row.get("signals"))[:12]],
        "broker_order_id": row.get("broker_order_id"),
        "entry_price": row.get("entry_price"),
        "exit_price": row.get("exit_price"),
    }


def _realized_pnl(row: dict[str, Any]) -> float:
    for key in ("realized_pnl", "pnl", "profit_loss", "net_pnl"):
        if key in row:
            return _safe_float(row.get(key))
    return 0


def _jsonl_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def _fallback_trade_key(row: dict[str, Any]) -> str:
    return "|".join([str(row.get("timestamp") or ""), str(row.get("symbol") or ""), str(row.get("realized_pnl") or "")])


def _report_id(now: datetime, hours: int, trades: list[dict[str, Any]]) -> str:
    symbols = "-".join(sorted({str(trade.get("symbol") or "UNKNOWN").upper() for trade in trades})[:4])
    basis = str(trades[0].get("timestamp") or now.isoformat()) if trades else now.isoformat()
    stamp = "".join(character for character in basis if character.isdigit())[:14] or now.strftime("%Y%m%d%H%M%S")
    return f"loss-report-{stamp}-{hours}h-{len(trades)}-{symbols or 'none'}"


def _parse_timestamp(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _as_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _limit_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text[:limit]


def _empty_learning() -> dict[str, Any]:
    return {"active": False, "cooldown_symbols": {}, "risk": {}, "issues": [], "reviewer_notes": ""}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))
