from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from scripts.decision_log import latest_decisions

ORDER_TRUTH_LOG = Path("logs") / "order_truth.jsonl"
TRADE_JOURNAL_LOG = Path("logs") / "trade_journal.jsonl"
SHADOW_LOG = Path("logs") / "shadow_trades.jsonl"


def record_order_truth_event(root: Path, source: str, order: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    timestamp = _timestamp(now)
    event = {
        "timestamp": timestamp,
        "source": str(source or "unknown"),
        "stage": _order_stage(order),
        "symbol": _symbol(order),
        "side": str(order.get("side") or "").upper(),
        "action": order.get("action"),
        "status": order.get("status"),
        "broker_status": order.get("broker_status"),
        "broker_order_id": order.get("broker_order_id"),
        "client_order_id": order.get("client_order_id"),
        "quantity": _number_or_none(order.get("quantity")),
        "notional": _number_or_none(order.get("notional")),
        "current_price": _number_or_none(order.get("current_price")),
        "filled_quantity": _number_or_none(order.get("filled_quantity")),
        "filled_average_price": _number_or_none(order.get("filled_average_price")),
        "fill_status": order.get("fill_status"),
        "scheduled_for_market_open": bool(order.get("scheduled_for_market_open")),
        "target_fill_time": order.get("target_fill_time"),
        "reason": order.get("message") or order.get("reason"),
        "review_only": bool(order.get("review_only", True)),
    }
    _append_jsonl(root / ORDER_TRUTH_LOG, event)
    return event


def order_truth_snapshot(root: Path, *, limit: int = 80) -> dict[str, Any]:
    events = _jsonl_rows(root / ORDER_TRUTH_LOG)
    if not events:
        events = [_event_from_decision(row) for row in latest_decisions(root / "logs" / "decision_log.jsonl", limit=limit)]
        events = [event for event in events if event]
    sorted_events = sorted(events, key=lambda item: str(item.get("timestamp") or ""), reverse=True)[:limit]
    current_events = _current_order_events(sorted_events)
    summary = _stage_counts(current_events)
    active = [event for event in current_events if event.get("stage") in {"created", "submitted", "partial_fill", "queued"}]
    return {
        "ok": True,
        "status": "ready",
        "events": sorted_events,
        "current": current_events,
        "active": active[:20],
        "summary": {**summary, "total": len(sorted_events), "active": len(active)},
        "message": "Order truth center reconciles tickets, broker ids, fills, rejects, and queued market-open orders.",
    }


def record_trade_journal_entry(
    root: Path,
    source: str,
    decision: dict[str, Any],
    execution: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    entry_price = _number_or_none(execution.get("filled_average_price")) or _number_or_none(decision.get("current_price"))
    entry = {
        "timestamp": _timestamp(now),
        "source": str(source or "unknown"),
        "strategy": _strategy_name(decision),
        "symbol": _symbol(decision) or _symbol(execution),
        "side": str(decision.get("side") or execution.get("side") or "").upper(),
        "action": decision.get("action"),
        "confidence": _number_or_none(decision.get("confidence")),
        "probability_up": _number_or_none(decision.get("probability_up")),
        "edge_pct": _number_or_none(decision.get("edge_pct") or decision.get("edge")),
        "entry_price": entry_price,
        "quantity": _number_or_none(execution.get("quantity") or decision.get("quantity")),
        "notional": _number_or_none(execution.get("notional") or decision.get("notional")),
        "thesis": str(decision.get("reason") or execution.get("message") or ""),
        "risk_plan": _risk_plan(decision),
        "exit_plan": _exit_plan(decision),
        "broker_order_id": execution.get("broker_order_id"),
        "broker_status": execution.get("broker_status"),
        "fill_status": execution.get("fill_status"),
        "realized_pnl": _number_or_none(execution.get("realized_pnl")),
        "signals": list(decision.get("signals") or []),
        "review_only": bool(execution.get("review_only", decision.get("review_only", True))),
    }
    _append_jsonl(root / TRADE_JOURNAL_LOG, entry)
    return entry


def trade_journal_snapshot(root: Path, *, limit: int = 80) -> dict[str, Any]:
    entries = sorted(_jsonl_rows(root / TRADE_JOURNAL_LOG), key=lambda item: str(item.get("timestamp") or ""), reverse=True)[:limit]
    wins = [entry for entry in entries if _safe_float(entry.get("realized_pnl")) > 0]
    losses = [entry for entry in entries if _safe_float(entry.get("realized_pnl")) < 0]
    return {
        "ok": True,
        "status": "ready",
        "entries": entries,
        "summary": {
            "entries": len(entries),
            "wins": len(wins),
            "losses": len(losses),
            "net_pnl": round(sum(_safe_float(entry.get("realized_pnl")) for entry in entries), 2),
        },
    }


def strategy_scorecard(root: Path, *, limit: int = 250) -> dict[str, Any]:
    entries = _jsonl_rows(root / TRADE_JOURNAL_LOG)[-limit:]
    outcomes = _jsonl_rows(root / "logs" / "trade_outcomes.jsonl")[-limit:]
    buckets: dict[str, dict[str, Any]] = {}
    for entry in entries:
        strategy = str(entry.get("strategy") or "autopilot").strip() or "autopilot"
        bucket = buckets.setdefault(strategy, _empty_strategy(strategy))
        bucket["planned"] += 1
        if entry.get("broker_order_id"):
            bucket["submitted"] += 1
        pnl = _safe_float(entry.get("realized_pnl"))
        _apply_pnl(bucket, pnl)
    for outcome in outcomes:
        strategy = str(outcome.get("strategy") or outcome.get("action") or "postmortem").strip().lower()
        strategy = strategy.replace(" ", "_")
        bucket = buckets.setdefault(strategy, _empty_strategy(strategy))
        pnl = _safe_float(outcome.get("realized_pnl") or outcome.get("pnl"))
        _apply_pnl(bucket, pnl)
    strategies = [_finalize_strategy(bucket) for bucket in buckets.values()]
    strategies.sort(key=lambda item: (item["net_pnl"], item["win_rate_pct"], item["submitted"]), reverse=True)
    return {
        "ok": True,
        "status": "ready",
        "strategies": strategies,
        "summary": {
            "strategies": len(strategies),
            "net_pnl": round(sum(item["net_pnl"] for item in strategies), 2),
            "submitted": sum(int(item["submitted"]) for item in strategies),
        },
    }


def record_shadow_candidates(root: Path, orders: list[dict[str, Any]], *, now: datetime | None = None) -> list[dict[str, Any]]:
    timestamp = _timestamp(now)
    rows: list[dict[str, Any]] = []
    for order in orders[:20]:
        symbol = _symbol(order)
        side = str(order.get("side") or "").lower()
        price = _safe_float(order.get("current_price"))
        if not symbol or side not in {"buy", "sell"} or price <= 0:
            continue
        row = {
            "timestamp": timestamp,
            "symbol": symbol,
            "side": side,
            "action": order.get("action"),
            "entry_price": round(price, 4),
            "confidence": _number_or_none(order.get("confidence")),
            "probability_up": _number_or_none(order.get("probability_up")),
            "status": "shadow_open",
        }
        _append_jsonl(root / SHADOW_LOG, row)
        rows.append(row)
    return rows


def shadow_mode_snapshot(root: Path, quotes: dict[str, Any], *, min_age_minutes: int = 5, limit: int = 80) -> dict[str, Any]:
    rows = sorted(_jsonl_rows(root / SHADOW_LOG), key=lambda item: str(item.get("timestamp") or ""), reverse=True)[:limit]
    cutoff = datetime.now(UTC) - timedelta(minutes=max(1, min_age_minutes))
    items = [_shadow_outcome(row, quotes, cutoff) for row in rows]
    evaluated = [item for item in items if item.get("status") == "shadow_evaluated"]
    return {
        "ok": True,
        "status": "ready",
        "items": items,
        "summary": {
            "open": sum(1 for item in items if item.get("status") == "shadow_open"),
            "evaluated": len(evaluated),
            "wins": sum(1 for item in evaluated if item.get("outcome") == "win"),
            "losses": sum(1 for item in evaluated if item.get("outcome") == "loss"),
            "avg_return_pct": round(sum(_safe_float(item.get("return_pct")) for item in evaluated) / len(evaluated), 2) if evaluated else 0,
        },
        "message": "Shadow mode tracks what the bot would have done before trusting a strategy with execution.",
    }


def build_backtest(histories: dict[str, Any], *, max_symbols: int = 40) -> dict[str, Any]:
    rows = []
    for symbol, history in list(histories.items())[:max_symbols]:
        closes = [float(value) for value in getattr(history, "closes", []) if value is not None]
        volumes = [float(value) for value in getattr(history, "volumes", []) if value is not None]
        if len(closes) < 3:
            continue
        start = closes[-min(6, len(closes))]
        end = closes[-1]
        return_pct = ((end - start) / start) * 100 if start > 0 else 0
        drawdown_pct = _max_drawdown_pct(closes[-min(20, len(closes)) :])
        volume_ratio = (volumes[-1] / (sum(volumes[:-1]) / len(volumes[:-1]))) if len(volumes) > 1 and sum(volumes[:-1]) > 0 else 1
        rows.append(
            {
                "symbol": str(symbol).upper(),
                "return_pct": round(return_pct, 2),
                "max_drawdown_pct": round(drawdown_pct, 2),
                "volume_ratio": round(volume_ratio, 2),
                "verdict": "passes" if return_pct > 0 and drawdown_pct > -5 else "weak",
            }
        )
    rows.sort(key=lambda item: (item["return_pct"], item["volume_ratio"]), reverse=True)
    return {
        "ok": True,
        "status": "ready",
        "rows": rows,
        "summary": {
            "symbols_tested": len(rows),
            "passing": sum(1 for row in rows if row["verdict"] == "passes"),
            "best_symbol": rows[0]["symbol"] if rows else "none",
            "best_return_pct": rows[0]["return_pct"] if rows else 0,
        },
    }


def build_data_health(
    *,
    market_snapshot: dict[str, Any],
    quotes: dict[str, Any],
    account_state: dict[str, Any],
    market_gate: dict[str, Any] | None = None,
    order_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    checks = [
        _health_check("market_quotes", bool(quotes), 25, "Quotes are available." if quotes else "No quote data returned."),
        _health_check("news_feed", bool(market_snapshot.get("news")), 15, "News feed has recent items." if market_snapshot.get("news") else "News feed is empty."),
        _health_check("account", str(account_state.get("source")) == "alpaca" and _safe_float(account_state.get("available_cash")) >= 0, 25, "Alpaca account state is available."),
        _health_check("market_clock", bool((market_gate or {}).get("status")), 15, f"Market clock: {(market_gate or {}).get('status', 'unknown')}."),
    ]
    rejected = int(_safe_float((order_summary or {}).get("rejected")))
    submitted = int(_safe_float((order_summary or {}).get("submitted")))
    checks.append(_health_check("orders", rejected == 0 or submitted >= rejected, 20, "Order rejects are under control." if rejected == 0 else f"{rejected} recent reject(s)."))
    score = sum(check["weight"] for check in checks if check["ok"])
    status = "healthy" if score >= 80 else "degraded" if score >= 50 else "unsafe"
    return {
        "ok": status != "unsafe",
        "status": status,
        "score": score,
        "checks": checks,
        "risk_action": "normal" if status == "healthy" else "reduce_size" if status == "degraded" else "pause_execution",
    }


def _event_from_decision(row: dict[str, Any]) -> dict[str, Any] | None:
    source = str(row.get("source") or "")
    if source not in {"autopilot_order", "alpaca_stock_order", "stock_order_attempt", "stock_order_intent"} and not row.get("broker_order_id"):
        return None
    return {
        "timestamp": row.get("timestamp"),
        "source": source,
        "stage": _order_stage(row),
        "symbol": _symbol(row),
        "side": str(row.get("side") or "").upper(),
        "action": row.get("action"),
        "status": row.get("status"),
        "broker_status": row.get("broker_status"),
        "broker_order_id": row.get("broker_order_id"),
        "quantity": _number_or_none(row.get("quantity")),
        "filled_quantity": _number_or_none(row.get("filled_quantity")),
        "filled_average_price": _number_or_none(row.get("filled_average_price")),
        "fill_status": row.get("fill_status"),
        "reason": row.get("reason"),
        "review_only": bool(row.get("review_only", True)),
    }


def _order_stage(order: dict[str, Any]) -> str:
    status = str(order.get("status") or "").lower()
    broker_status = str(order.get("broker_status") or "").lower()
    fill_status = str(order.get("fill_status") or "").lower()
    if bool(order.get("scheduled_for_market_open")):
        return "queued"
    if fill_status == "filled" or broker_status == "filled":
        return "filled"
    if fill_status == "partially_filled" or broker_status == "partially_filled":
        return "partial_fill"
    if status in {"rejected", "not_configured", "live_not_allowed", "confirmation_required", "network_error"} or broker_status == "rejected":
        return "rejected"
    if status in {"canceled", "cancelled"} or broker_status in {"canceled", "cancelled"}:
        return "canceled"
    if order.get("broker_order_id") or status in {"submitted", "accepted", "new"} or broker_status in {"accepted", "new"}:
        return "submitted"
    if status == "recorded":
        return "created"
    return "created"


def _stage_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    counts = {stage: 0 for stage in ("created", "queued", "submitted", "partial_fill", "filled", "rejected", "canceled")}
    for event in events:
        stage = str(event.get("stage") or "created")
        counts[stage if stage in counts else "created"] += 1
    return counts


def _current_order_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest_by_key: dict[str, dict[str, Any]] = {}
    for event in sorted(events, key=lambda item: str(item.get("timestamp") or "")):
        latest_by_key[_order_identity(event)] = event
    return sorted(latest_by_key.values(), key=lambda item: str(item.get("timestamp") or ""), reverse=True)


def _order_identity(event: dict[str, Any]) -> str:
    broker_order_id = str(event.get("broker_order_id") or "").strip()
    if broker_order_id:
        return f"broker:{broker_order_id}"
    client_order_id = str(event.get("client_order_id") or "").strip()
    if client_order_id:
        return f"client:{client_order_id}"
    timestamp = str(event.get("timestamp") or "")
    source = str(event.get("source") or "")
    symbol = _symbol(event)
    action = str(event.get("action") or "")
    return f"event:{timestamp}:{source}:{symbol}:{action}"


def _strategy_name(decision: dict[str, Any]) -> str:
    for signal in decision.get("signals") or []:
        text = str(signal).strip().lower()
        if text.startswith("strategy "):
            return text.split(" ", 1)[1].strip().replace(" ", "_") or "autopilot"
    action = str(decision.get("action") or "autopilot").lower()
    if "profit" in action or "risk_exit" in action:
        return "exit_intelligence"
    if "growth" in action:
        return "quick_growth"
    if "buy" in action or "candidate" in action:
        return "momentum_breakout"
    return action.replace(" ", "_") or "autopilot"


def _risk_plan(decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "sizing_method": decision.get("sizing_method"),
        "notional": _number_or_none(decision.get("notional")),
        "kelly_fraction": _number_or_none(decision.get("kelly_fraction")),
        "stop_loss": _number_or_none(decision.get("stop_loss")),
        "risk_budget": _number_or_none(decision.get("risk_budget")),
    }


def _exit_plan(decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "take_profit": _number_or_none(decision.get("take_profit")),
        "stop_loss": _number_or_none(decision.get("stop_loss")),
        "profit_target_pct": _number_or_none(decision.get("profit_target_pct")),
        "stop_exit_pct": _number_or_none(decision.get("stop_exit_pct")),
        "window_minutes": _number_or_none(decision.get("exit_window_minutes")),
    }


def _empty_strategy(strategy: str) -> dict[str, Any]:
    return {"strategy": strategy, "planned": 0, "submitted": 0, "wins": 0, "losses": 0, "net_pnl": 0.0, "pnl_events": 0}


def _apply_pnl(bucket: dict[str, Any], pnl: float) -> None:
    if pnl == 0:
        return
    bucket["pnl_events"] += 1
    bucket["net_pnl"] = round(_safe_float(bucket.get("net_pnl")) + pnl, 2)
    if pnl > 0:
        bucket["wins"] += 1
    elif pnl < 0:
        bucket["losses"] += 1


def _finalize_strategy(bucket: dict[str, Any]) -> dict[str, Any]:
    outcomes = int(bucket.get("wins") or 0) + int(bucket.get("losses") or 0)
    return {
        **bucket,
        "win_rate_pct": round((int(bucket.get("wins") or 0) / outcomes) * 100, 1) if outcomes else 0,
        "avg_pnl": round(_safe_float(bucket.get("net_pnl")) / outcomes, 2) if outcomes else 0,
        "status": _strategy_status(bucket, outcomes),
    }


def _strategy_status(bucket: dict[str, Any], outcomes: int) -> str:
    if outcomes <= 0:
        return "collecting"
    win_rate = (int(bucket.get("wins") or 0) / outcomes) * 100
    if outcomes >= 3 and _safe_float(bucket.get("net_pnl")) < 0 and win_rate < 40:
        return "throttle"
    if outcomes >= 3 and _safe_float(bucket.get("net_pnl")) > 0:
        return "promote"
    return "watch"


def _shadow_outcome(row: dict[str, Any], quotes: dict[str, Any], cutoff: datetime) -> dict[str, Any]:
    started = _parse_time(row.get("timestamp"))
    symbol = _symbol(row)
    quote = quotes.get(symbol)
    if started is None or started > cutoff or quote is None:
        return {**row, "status": "shadow_open"}
    entry = _safe_float(row.get("entry_price"))
    latest = _safe_float(getattr(quote, "price", None))
    if entry <= 0 or latest <= 0:
        return {**row, "status": "shadow_open"}
    direction = 1 if str(row.get("side") or "buy").lower() == "buy" else -1
    return_pct = ((latest - entry) / entry) * 100 * direction
    return {**row, "status": "shadow_evaluated", "latest_price": round(latest, 4), "return_pct": round(return_pct, 2), "outcome": "win" if return_pct > 0 else "loss" if return_pct < 0 else "flat"}


def _max_drawdown_pct(closes: list[float]) -> float:
    peak = 0.0
    worst = 0.0
    for close in closes:
        peak = max(peak, close)
        if peak > 0:
            worst = min(worst, ((close - peak) / peak) * 100)
    return worst


def _health_check(name: str, ok: bool, weight: int, message: str) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "weight": weight, "message": message}


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


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


def _timestamp(now: datetime | None) -> str:
    value = now or datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _symbol(row: dict[str, Any]) -> str:
    return str(row.get("symbol") or "").strip().upper()


def _number_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default
