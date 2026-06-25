from __future__ import annotations

from typing import Any

from scripts.quotes import Quote


def build_trade_ideas(
    scan_result: dict[str, Any],
    quotes: dict[str, Quote],
    positions: list[Any],
    risk: dict[str, float],
    technicals: dict[str, dict[str, float]] | None = None,
    market_trend: str = "UNKNOWN",
    max_ideas: int = 8,
) -> list[dict[str, Any]]:
    held = {position.symbol: position for position in positions}
    technicals = technicals or {}
    ideas = [
        _idea_for_scan(
            scan,
            quotes.get(scan.get("symbol", "")),
            held.get(scan.get("symbol", "")),
            risk,
            technicals.get(str(scan.get("symbol", "")).upper(), {}),
            market_trend,
        )
        for scan in scan_result.get("scans", [])
    ]
    return sorted(ideas, key=_idea_rank, reverse=True)[:max_ideas]


def build_market_trend(technicals: dict[str, dict[str, float]]) -> str:
    votes: list[bool] = []
    for symbol in ("SPY", "QQQ"):
        item = technicals.get(symbol, {})
        sma_5 = item.get("sma_5", 0)
        sma_20 = item.get("sma_20", 0)
        if sma_5 and sma_20:
            votes.append(sma_5 > sma_20)
    if not votes:
        return "UNKNOWN"
    if all(votes):
        return "UP"
    if not any(votes):
        return "DOWN"
    return "MIXED"


def build_trade_ideas_message(ideas: list[dict[str, Any]]) -> str:
    if not ideas:
        return "Trade ideas: no reviewable setups right now."
    lines = ["Trade ideas (review only):"]
    for idea in ideas[:5]:
        price = idea.get("current_price") or "n/a"
        stop = f" stop {idea['stop_loss']}" if idea.get("stop_loss") else ""
        target = f" target {idea['take_profit']}" if idea.get("take_profit") else ""
        lines.append(f"{idea['symbol']} {idea['action']} @ {price}{stop}{target} - {idea['reason']}")
    lines.append("No live stock order was placed.")
    return "\n".join(lines)


def _idea_for_scan(
    scan: dict[str, Any],
    quote: Quote | None,
    position: Any | None,
    risk: dict[str, float],
    technicals: dict[str, float],
    market_trend: str,
) -> dict[str, Any]:
    symbol = str(scan.get("symbol", "")).upper()
    if quote is None:
        return _idea(symbol, "NO_TRADE", 0, None, None, None, "No current price loaded yet.", scan, [])

    price = quote.price
    daily_change = quote.change_pct
    negative_count = int(scan.get("negative_news_count", 0))
    insider_count = int(scan.get("insider_filing_count", 0))
    score = int(scan.get("score", 0))
    stop_pct = float(risk.get("stop_loss_pct", 3))
    take_profit_pct = float(risk.get("take_profit_alert_pct", 20))
    daily_loss_pct = float(risk.get("daily_loss_alert_pct", 3))
    signals = _technical_signals(quote, technicals, market_trend)

    if position is not None:
        open_gain_pct = ((price - position.cost_basis) / position.cost_basis) * 100 if position.cost_basis else 0
        if negative_count or daily_change <= -abs(daily_loss_pct):
            return _idea(symbol, "SELL_REVIEW", 80, price, None, None, "Held position has risk pressure or a sharp daily drop.", scan, signals)
        if open_gain_pct >= take_profit_pct:
            return _idea(symbol, "TRIM_REVIEW", 74, price, None, None, f"Held position has a {open_gain_pct:.1f}% open gain.", scan, signals)
        return _idea(symbol, "HOLD_REVIEW", 45, price, None, None, "Held position has no sell/trim trigger.", scan, signals)

    if negative_count or insider_count:
        return _idea(symbol, "NO_TRADE", 30, price, None, None, "Scanner found risk or insider-filing noise; wait for a cleaner setup.", scan, signals)
    if market_trend == "DOWN":
        return _idea(symbol, "NO_TRADE", 35, price, None, None, "Broad market trend is down; wait for confirmation.", scan, signals)
    if score >= 30 and 0.2 <= daily_change <= 5 and _has_clean_technical_setup(technicals, market_trend):
        stop = price * (1 - stop_pct / 100)
        target = price * (1 + (stop_pct * 2) / 100)
        confidence = min(90, int(45 + (score / 2) + min(daily_change * 5, 15) + _technical_bonus(technicals)))
        return _idea(symbol, "BUY_REVIEW", confidence, price, stop, target, "Positive daily move with scanner, trend, and momentum support.", scan, signals)
    if daily_change > 5:
        return _idea(symbol, "NO_TRADE", 35, price, None, None, "Price already moved hard today; avoid chasing.", scan, signals)
    return _idea(symbol, "NO_TRADE", 20, price, None, None, "No clean buy or sell setup from current rules.", scan, signals)


def _idea(
    symbol: str,
    action: str,
    confidence: int,
    price: float | None,
    stop: float | None,
    target: float | None,
    reason: str,
    scan: dict[str, Any],
    signals: list[str],
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "action": action,
        "confidence": confidence,
        "current_price": round(price, 4) if price is not None else None,
        "stop_loss": round(stop, 4) if stop is not None else None,
        "take_profit": round(target, 4) if target is not None else None,
        "score": int(scan.get("score", 0)),
        "rating": str(scan.get("rating", "QUIET")),
        "reason": reason,
        "signals": signals,
        "review_only": True,
    }


def _idea_rank(idea: dict[str, Any]) -> tuple[int, int]:
    priority = {
        "SELL_REVIEW": 5,
        "TRIM_REVIEW": 4,
        "BUY_REVIEW": 3,
        "HOLD_REVIEW": 2,
        "NO_TRADE": 1,
    }
    return priority.get(str(idea.get("action")), 0), int(idea.get("confidence", 0))


def _has_clean_technical_setup(technicals: dict[str, float], market_trend: str) -> bool:
    if not technicals:
        return market_trend in {"UP", "MIXED", "UNKNOWN"}
    sma_5 = technicals.get("sma_5", 0)
    sma_20 = technicals.get("sma_20", 0)
    rsi = technicals.get("rsi_14", 50)
    volume_ratio = technicals.get("volume_ratio", 1)
    trend_ok = not sma_20 or sma_5 >= sma_20
    return trend_ok and 45 <= rsi <= 72 and volume_ratio >= 1


def _technical_bonus(technicals: dict[str, float]) -> int:
    bonus = 0
    if technicals.get("volume_ratio", 1) >= 1.5:
        bonus += 5
    if technicals.get("sma_5", 0) > technicals.get("sma_20", 0) > 0:
        bonus += 5
    rsi = technicals.get("rsi_14", 50)
    if 50 <= rsi <= 65:
        bonus += 5
    return bonus


def _technical_signals(quote: Quote, technicals: dict[str, float], market_trend: str) -> list[str]:
    signals = [f"day {quote.change_pct:.2f}%", f"market {market_trend.lower()}"]
    if technicals:
        signals.extend(
            [
                f"rsi {technicals.get('rsi_14', 50):.1f}",
                f"volume {technicals.get('volume_ratio', 1):.2f}x",
            ]
        )
        sma_5 = technicals.get("sma_5", 0)
        sma_20 = technicals.get("sma_20", 0)
        if sma_5 and sma_20:
            signals.append("above 20-day" if sma_5 > sma_20 else "below 20-day")
    return signals
