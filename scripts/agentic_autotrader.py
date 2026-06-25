from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.quotes import Quote

POSITIVE_TERMS = {
    "accumulate",
    "beat",
    "beats",
    "breakout",
    "bull",
    "bullish",
    "buying",
    "call buying",
    "demand",
    "expansion",
    "growth",
    "long",
    "momentum",
    "raises",
    "strong",
    "surge",
    "surges",
    "upgrade",
    "upgraded",
}

NEGATIVE_TERMS = {
    "bear",
    "bearish",
    "bankruptcy",
    "downgrade",
    "fraud",
    "investigation",
    "lawsuit",
    "miss",
    "probe",
    "recall",
    "risk",
    "short",
}

INSTITUTIONAL_TERMS = {
    "13f",
    "flow",
    "hedge fund",
    "institutional",
    "top trader",
    "unusual option",
    "whale",
}


@dataclass(frozen=True)
class AgenticScanConfig:
    bankroll_usd: float = 100
    max_trade_usd: float = 25
    max_kelly_fraction: float = 0.05
    window_minutes: int = 15
    min_probability: float = 0.56
    min_edge_pct: float = 0.05
    reward_to_risk: float = 2
    max_opportunities: int = 8
    allow_downtrend: bool = False


@dataclass(frozen=True)
class ResearchSignal:
    symbol: str
    sentiment_score: float
    source_counts: dict[str, int]
    narrative_terms: list[str]
    item_count: int
    negative_count: int
    institutional_count: int


@dataclass(frozen=True)
class PredictionSignal:
    symbol: str
    probability_up: float
    model: str
    features: dict[str, float]
    llm_adjustment: float


@dataclass(frozen=True)
class RiskSignal:
    symbol: str
    edge_pct: float
    reward_pct: float
    loss_pct: float
    kelly_fraction: float
    suggested_notional: float


@dataclass(frozen=True)
class FakeXGBoostModel:
    probability: float

    def predict_proba(self, rows: list[list[float]]) -> list[list[float]]:
        probability = _clamp(float(self.probability), 0.01, 0.99)
        return [[1 - probability, probability] for _ in rows]


def run_agentic_scan(
    scan_result: dict[str, Any],
    quotes: dict[str, Quote],
    technicals: dict[str, dict[str, float]],
    snapshot: dict[str, Any],
    config: AgenticScanConfig | None = None,
    xgboost_model: Any | None = None,
) -> dict[str, Any]:
    normalized = _normalize_config(config or AgenticScanConfig())
    research_by_symbol = _research_signals(snapshot, scan_result)
    model_name = "xgboost" if xgboost_model is not None else "heuristic_fallback"
    market_trend = str(snapshot.get("market_trend") or "UNKNOWN").upper()
    opportunities: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []

    for scan in scan_result.get("scans", []):
        symbol = str(scan.get("symbol") or "").upper()
        if not symbol or symbol.endswith("-USD"):
            continue
        quote = quotes.get(symbol)
        if quote is None:
            blocked.append({"symbol": symbol, "status": "blocked", "reason": "No current market price loaded."})
            continue
        research = research_by_symbol.get(symbol, _empty_research_signal(symbol))
        prediction = _predict_symbol(symbol, scan, quote, technicals.get(symbol, {}), research, xgboost_model)
        risk = _kelly_size(symbol, quote, technicals.get(symbol, {}), prediction, normalized)
        decision = _decision(symbol, scan, quote, technicals.get(symbol, {}), research, prediction, risk, normalized, market_trend)
        if decision["status"] == "opportunity":
            opportunities.append(decision)
        else:
            blocked.append(decision)

    opportunities = sorted(opportunities, key=lambda item: (item["suggested_notional"], item["edge_pct"], item["probability_up"]), reverse=True)
    opportunities = opportunities[: normalized.max_opportunities]
    source_totals = _source_totals(research_by_symbol.values())
    return {
        "ok": True,
        "status": "scanned",
        "window_minutes": normalized.window_minutes,
        "agents": {
            "scan": {
                "status": "ready",
                "goal": "Rank 1-30 minute buy candidates with short-window payoff potential.",
                "symbols_scanned": len(scan_result.get("scans", [])),
            },
            "research": {
                "status": "ready",
                "sources": source_totals,
                "method": "RSS/news plus configured social items; X/Reddit require allowed feeds or API input.",
            },
            "prediction": {
                "status": "ready",
                "model": model_name,
                "llm_calibration": "local narrative calibration rules; external LLM can be added behind env-gated client.",
            },
            "risk": {
                "status": "ready",
                "method": "kelly_criterion_capped",
                "bankroll_usd": round(normalized.bankroll_usd, 2),
                "max_kelly_fraction": round(normalized.max_kelly_fraction, 4),
            },
            "postmortem": {
                "status": "ready",
                "agents": ["research", "prediction", "risk", "execution", "portfolio"],
            },
        },
        "opportunities": opportunities,
        "blocked": blocked[: max(12, normalized.max_opportunities)],
        "summary": {
            "opportunities": len(opportunities),
            "blocked": len(blocked),
            "top_symbol": opportunities[0]["symbol"] if opportunities else None,
        },
        "notice": "Agentic scan is a paper-first decision tool. It does not guarantee profit.",
    }


def load_xgboost_model(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        from xgboost import XGBClassifier
    except ImportError:
        return None
    try:
        model = XGBClassifier()
        model.load_model(path)
    except Exception:
        return None
    return model


def run_loss_postmortem(outcome: dict[str, Any]) -> dict[str, Any]:
    symbol = str(outcome.get("symbol") or "UNKNOWN").upper()
    loss = abs(_safe_float(outcome.get("realized_pnl")))
    signals = [str(signal) for signal in outcome.get("signals", []) if str(signal).strip()]
    timestamp = datetime.now(UTC).isoformat()
    agents = [
        {
            "name": "research",
            "finding": "Check whether the narrative was stale, crowded, or contradicted by negative headlines.",
            "evidence": _find_matching_signal(signals, ["narrative", "news", "reddit", "x", "rss"]),
        },
        {
            "name": "prediction",
            "finding": "Reduce confidence when price action fails to confirm the model probability.",
            "evidence": f"Entry {outcome.get('entry_price', 'n/a')} exit {outcome.get('exit_price', 'n/a')}",
        },
        {
            "name": "risk",
            "finding": "Throttle the next Kelly fraction after a realized loss.",
            "evidence": f"Realized loss {loss:.2f}",
        },
        {
            "name": "execution",
            "finding": "Review entry timing, spread, slippage, and whether the stop was too tight for volatility.",
            "evidence": str(outcome.get("reason") or "No execution reason recorded."),
        },
        {
            "name": "portfolio",
            "finding": "Check concentration and correlation before taking another same-theme trade.",
            "evidence": _find_matching_signal(signals, ["theme", "market", "sector", "portfolio"]),
        },
    ]
    return {
        "timestamp": timestamp,
        "symbol": symbol,
        "realized_pnl": _safe_float(outcome.get("realized_pnl")),
        "agents": agents,
        "updates": {
            "cooldown_symbol": symbol,
            "max_next_risk_fraction": 0.5,
            "require_price_confirmation": True,
            "notes": "Use this as a learning record; strategy weights are not auto-mutated without review.",
        },
    }


def record_loss_postmortem(path: Path, postmortem: dict[str, Any]) -> dict[str, Any]:
    row = dict(postmortem)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")
    return row


def run_pending_loss_postmortems(root: Path) -> list[dict[str, Any]]:
    outcomes_path = root / "logs" / "trade_outcomes.jsonl"
    postmortems_path = root / "logs" / "postmortems.jsonl"
    if not outcomes_path.exists():
        return []
    processed = _processed_postmortem_keys(postmortems_path)
    created: list[dict[str, Any]] = []
    for row in _jsonl_rows(outcomes_path):
        if _safe_float(row.get("realized_pnl")) >= 0:
            continue
        key = _outcome_key(row)
        if key in processed:
            continue
        postmortem = run_loss_postmortem({**row, "postmortem_key": key})
        postmortem["postmortem_key"] = key
        created.append(record_loss_postmortem(postmortems_path, postmortem))
    return created


def _decision(
    symbol: str,
    scan: dict[str, Any],
    quote: Quote,
    technicals: dict[str, float],
    research: ResearchSignal,
    prediction: PredictionSignal,
    risk: RiskSignal,
    config: AgenticScanConfig,
    market_trend: str,
) -> dict[str, Any]:
    negative_count = int(scan.get("negative_news_count", 0)) + research.negative_count
    if market_trend == "DOWN" and not config.allow_downtrend:
        return {"symbol": symbol, "status": "blocked", "reason": "Broad market trend is down; passive buy candidates are blocked."}
    if negative_count > 0 and research.sentiment_score < 0.05:
        return {"symbol": symbol, "status": "blocked", "reason": "Negative narrative risk is too high for passive buying."}
    if prediction.probability_up < config.min_probability:
        return {
            "symbol": symbol,
            "status": "blocked",
            "reason": "Prediction probability is below the passive trading threshold.",
            "probability_up": round(prediction.probability_up, 4),
        }
    if risk.edge_pct < config.min_edge_pct or risk.kelly_fraction <= 0 or risk.suggested_notional <= 0:
        return {
            "symbol": symbol,
            "status": "blocked",
            "reason": "Kelly edge is too small after risk adjustment.",
            "probability_up": round(prediction.probability_up, 4),
            "edge_pct": round(risk.edge_pct, 4),
        }
    price = quote.price
    strategy_tags = _strategy_tags(scan, quote, technicals, research)
    if market_trend == "DOWN" and config.allow_downtrend:
        strategy_tags = ["paper_downtrend_probe", *strategy_tags]
    confidence = int(_clamp((prediction.probability_up * 100) + min(8, risk.edge_pct * 4), 0, 95))
    signals = [
        f"prob {prediction.probability_up * 100:.1f}%",
        f"edge {risk.edge_pct:.2f}%",
        f"kelly {risk.kelly_fraction * 100:.2f}%",
        f"sentiment {research.sentiment_score:.2f}",
        f"volume {float(technicals.get('volume_ratio', 1)):.2f}x",
        *strategy_tags,
    ]
    return {
        "symbol": symbol,
        "status": "opportunity",
        "action": "AUTO_BUY_CANDIDATE",
        "horizon_minutes": config.window_minutes,
        "confidence": confidence,
        "current_price": round(price, 4),
        "probability_up": round(prediction.probability_up, 4),
        "edge_pct": round(risk.edge_pct, 4),
        "reward_pct": round(risk.reward_pct, 4),
        "loss_pct": round(risk.loss_pct, 4),
        "kelly_fraction": round(risk.kelly_fraction, 4),
        "suggested_notional": round(risk.suggested_notional, 2),
        "stop_loss": round(price * (1 - risk.loss_pct / 100), 4),
        "take_profit": round(price * (1 + risk.reward_pct / 100), 4),
        "sentiment_score": round(research.sentiment_score, 4),
        "source_counts": research.source_counts,
        "strategy_tags": strategy_tags,
        "model": prediction.model,
        "reason": "Narrative, price action, prediction probability, and Kelly risk all pass passive scan rules.",
        "reasons": [
            f"Narrative score {research.sentiment_score:.2f} across {research.item_count} item(s).",
            f"Market price is {quote.change_pct:.2f}% today versus short-window narrative.",
            f"Kelly-capped size is ${risk.suggested_notional:.2f}.",
        ],
        "signals": signals,
        "review_only": True,
    }


def _predict_symbol(
    symbol: str,
    scan: dict[str, Any],
    quote: Quote,
    technicals: dict[str, float],
    research: ResearchSignal,
    xgboost_model: Any | None,
) -> PredictionSignal:
    features = _features(scan, quote, technicals, research)
    model_probability = _xgboost_probability(xgboost_model, features)
    model_name = "xgboost" if model_probability is not None else "heuristic_fallback"
    base_probability = model_probability if model_probability is not None else _heuristic_probability(features)
    llm_adjustment = _local_narrative_calibration(features)
    probability = _clamp(base_probability + llm_adjustment, 0.01, 0.95)
    return PredictionSignal(
        symbol=symbol,
        probability_up=probability,
        model=model_name,
        features=features,
        llm_adjustment=llm_adjustment,
    )


def _kelly_size(symbol: str, quote: Quote, technicals: dict[str, float], prediction: PredictionSignal, config: AgenticScanConfig) -> RiskSignal:
    volume_ratio = _safe_float(technicals.get("volume_ratio"), 1)
    reward_pct = _clamp(0.35 + min(volume_ratio, 4) * 0.22, 0.35, 2.5)
    loss_pct = max(0.25, reward_pct / max(config.reward_to_risk, 0.5))
    edge_pct = (prediction.probability_up * reward_pct) - ((1 - prediction.probability_up) * loss_pct)
    reward_to_loss = reward_pct / loss_pct if loss_pct else 0
    raw_kelly = ((reward_to_loss * prediction.probability_up) - (1 - prediction.probability_up)) / reward_to_loss if reward_to_loss else 0
    kelly_fraction = _clamp(raw_kelly, 0, config.max_kelly_fraction)
    suggested = min(config.max_trade_usd, config.bankroll_usd * kelly_fraction)
    if suggested < 1:
        suggested = 0
    return RiskSignal(symbol, edge_pct, reward_pct, loss_pct, kelly_fraction, suggested)


def _research_signals(snapshot: dict[str, Any], scan_result: dict[str, Any]) -> dict[str, ResearchSignal]:
    symbols = {str(scan.get("symbol") or "").upper() for scan in scan_result.get("scans", []) if scan.get("symbol")}
    items = _research_items(snapshot)
    return {symbol: _research_signal(symbol, [item for item in items if _item_symbol(item) == symbol]) for symbol in symbols}


def _research_items(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in snapshot.get("news", []):
        items.append(
            {
                "source": str(item.get("source") or "rss").lower(),
                "symbol": str(item.get("symbol") or "").upper(),
                "text": str(item.get("title") or item.get("text") or ""),
                "url": str(item.get("url") or ""),
            }
        )
    for item in snapshot.get("social_items", []):
        items.append(
            {
                "source": str(item.get("source") or "social").lower(),
                "symbol": str(item.get("symbol") or "").upper(),
                "text": str(item.get("text") or item.get("title") or ""),
                "url": str(item.get("url") or ""),
            }
        )
    return [item for item in items if item["symbol"] and item["text"]]


def _research_signal(symbol: str, items: list[dict[str, Any]]) -> ResearchSignal:
    source_counts: dict[str, int] = {}
    positive = 0
    negative = 0
    institutional = 0
    narrative_terms: list[str] = []
    for item in items:
        source = str(item.get("source") or "unknown").lower()
        source_counts[source] = source_counts.get(source, 0) + 1
        text = str(item.get("text") or "").lower()
        matched_positive = [term for term in POSITIVE_TERMS if term in text]
        matched_negative = [term for term in NEGATIVE_TERMS if term in text]
        matched_institutional = [term for term in INSTITUTIONAL_TERMS if term in text]
        positive += len(matched_positive)
        negative += len(matched_negative)
        institutional += len(matched_institutional)
        narrative_terms.extend([*matched_positive, *matched_negative, *matched_institutional])
    denominator = max(1, positive + negative)
    sentiment = (positive - negative) / denominator
    if institutional and sentiment >= 0:
        sentiment += min(0.18, institutional * 0.04)
    return ResearchSignal(
        symbol=symbol,
        sentiment_score=_clamp(sentiment, -1, 1),
        source_counts=source_counts,
        narrative_terms=sorted(set(narrative_terms))[:12],
        item_count=len(items),
        negative_count=negative,
        institutional_count=institutional,
    )


def _features(scan: dict[str, Any], quote: Quote, technicals: dict[str, float], research: ResearchSignal) -> dict[str, float]:
    day_change = quote.change_pct
    normalized_price_action = math.tanh(day_change / 5)
    sentiment = research.sentiment_score
    return {
        "scan_score": _safe_float(scan.get("score")) / 100,
        "news_count": min(1, _safe_float(scan.get("news_count")) / 8),
        "negative_ratio": min(1, (_safe_float(scan.get("negative_news_count")) + research.negative_count) / max(1, research.item_count + 1)),
        "sentiment": sentiment,
        "source_count": min(1, sum(research.source_counts.values()) / 6),
        "institutional": min(1, research.institutional_count / 3),
        "day_change": _clamp(day_change / 10, -1, 1),
        "narrative_divergence": _clamp(sentiment - normalized_price_action, -1, 1),
        "volume_ratio": min(1, max(0, _safe_float(technicals.get("volume_ratio"), 1) - 1) / 3),
        "rsi_balance": 1 - min(1, abs(_safe_float(technicals.get("rsi_14"), 50) - 58) / 42),
        "trend": 1 if _safe_float(technicals.get("sma_5")) >= _safe_float(technicals.get("sma_20")) > 0 else 0,
    }


def _heuristic_probability(features: dict[str, float]) -> float:
    probability = 0.46
    probability += features["scan_score"] * 0.16
    probability += features["sentiment"] * 0.10
    probability += features["source_count"] * 0.04
    probability += features["institutional"] * 0.05
    probability += features["volume_ratio"] * 0.10
    probability += features["rsi_balance"] * 0.04
    probability += features["trend"] * 0.04
    probability += max(0, features["narrative_divergence"]) * 0.04
    probability -= features["negative_ratio"] * 0.18
    probability -= max(0, features["day_change"] - 0.5) * 0.06
    return _clamp(probability, 0.05, 0.92)


def _local_narrative_calibration(features: dict[str, float]) -> float:
    adjustment = 0.0
    if features["sentiment"] > 0.35 and features["narrative_divergence"] > 0:
        adjustment += 0.025
    if features["institutional"] > 0 and features["volume_ratio"] > 0.2:
        adjustment += 0.015
    if features["negative_ratio"] > 0:
        adjustment -= 0.035 + min(0.05, features["negative_ratio"] * 0.05)
    if features["day_change"] > 0.7:
        adjustment -= 0.025
    return adjustment


def _xgboost_probability(model: Any | None, features: dict[str, float]) -> float | None:
    if model is None:
        return None
    row = [[features[key] for key in sorted(features)]]
    try:
        probabilities = model.predict_proba(row)
        first = probabilities[0]
        return _clamp(float(first[1] if len(first) > 1 else first[0]), 0.01, 0.99)
    except (AttributeError, TypeError, ValueError, IndexError):
        return None


def _strategy_tags(scan: dict[str, Any], quote: Quote, technicals: dict[str, float], research: ResearchSignal) -> list[str]:
    tags: list[str] = []
    if quote.change_pct > 0 and _safe_float(technicals.get("volume_ratio"), 1) >= 1.5:
        tags.append("momentum_breakout")
    if research.sentiment_score > 0.25 and research.item_count >= 2:
        tags.append("narrative_momentum")
    if research.institutional_count:
        tags.append("institutional_flow")
    if _safe_float(scan.get("score")) >= 50:
        tags.append("scanner_alignment")
    return tags or ["watchlist_momentum"]


def _source_totals(research_signals: Any) -> dict[str, int]:
    totals: dict[str, int] = {}
    for signal in research_signals:
        for source, count in signal.source_counts.items():
            totals[source] = totals.get(source, 0) + count
    return totals


def _empty_research_signal(symbol: str) -> ResearchSignal:
    return ResearchSignal(symbol=symbol, sentiment_score=0, source_counts={}, narrative_terms=[], item_count=0, negative_count=0, institutional_count=0)


def _normalize_config(config: AgenticScanConfig) -> AgenticScanConfig:
    return AgenticScanConfig(
        bankroll_usd=_clamp(_safe_float(config.bankroll_usd, 100), 1, 10_000_000),
        max_trade_usd=_clamp(_safe_float(config.max_trade_usd, 25), 1, 100_000),
        max_kelly_fraction=_clamp(_safe_float(config.max_kelly_fraction, 0.05), 0, 0.25),
        window_minutes=int(_clamp(_safe_float(config.window_minutes, 15), 1, 30)),
        min_probability=_clamp(_safe_float(config.min_probability, 0.56), 0.5, 0.95),
        min_edge_pct=_clamp(_safe_float(config.min_edge_pct, 0.05), 0, 20),
        reward_to_risk=_clamp(_safe_float(config.reward_to_risk, 2), 0.5, 6),
        max_opportunities=int(_clamp(_safe_float(config.max_opportunities, 8), 1, 50)),
        allow_downtrend=bool(config.allow_downtrend),
    )


def _item_symbol(item: dict[str, Any]) -> str:
    return str(item.get("symbol") or "").upper()


def _find_matching_signal(signals: list[str], terms: list[str]) -> str:
    for signal in signals:
        lowered = signal.lower()
        if any(term in lowered for term in terms):
            return signal
    return "No matching signal recorded."


def _processed_postmortem_keys(path: Path) -> set[str]:
    return {str(row.get("postmortem_key") or _outcome_key(row)) for row in _jsonl_rows(path)}


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


def _outcome_key(row: dict[str, Any]) -> str:
    return "|".join([str(row.get("timestamp") or ""), str(row.get("symbol") or ""), str(row.get("broker_order_id") or ""), str(row.get("realized_pnl") or "")])


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))
