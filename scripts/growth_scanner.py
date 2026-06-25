from __future__ import annotations

from typing import Any

from scripts.quotes import Quote


def build_growth_candidates(
    scan_result: dict[str, Any],
    quotes: dict[str, Quote],
    technicals: dict[str, dict[str, float]],
    market_trend: str = "UNKNOWN",
    max_candidates: int = 12,
) -> list[dict[str, Any]]:
    candidates = [
        _candidate_for_scan(scan, quotes.get(str(scan.get("symbol", "")).upper()), technicals.get(str(scan.get("symbol", "")).upper(), {}), market_trend)
        for scan in scan_result.get("scans", [])
    ]
    candidates = [candidate for candidate in candidates if candidate["momentum_score"] > 0]
    return sorted(candidates, key=lambda item: item["momentum_score"], reverse=True)[:max_candidates]


def build_growth_candidates_message(candidates: list[dict[str, Any]]) -> str:
    if not candidates:
        return "Quick-return growth scanner: no reviewable momentum candidates right now."
    lines = ["Quick-return growth scanner (review only):"]
    for candidate in candidates[:5]:
        lines.append(
            f"{candidate['symbol']} {candidate['action']} score {candidate['momentum_score']}/100 "
            f"day {candidate['day_change_pct']}% @ {candidate.get('current_price', 'n/a')} - {candidate['reason']}"
        )
    lines.append("Review only. No live order was placed.")
    return "\n".join(lines)


def _candidate_for_scan(scan: dict[str, Any], quote: Quote | None, technicals: dict[str, float], market_trend: str) -> dict[str, Any]:
    symbol = str(scan.get("symbol", "")).upper()
    if quote is None:
        return _candidate(symbol, "WAIT", 0, None, 0, "No current price loaded yet.", scan, technicals, market_trend)

    day_change = quote.change_pct
    volume_ratio = float(technicals.get("volume_ratio", 1))
    rsi = float(technicals.get("rsi_14", 50))
    sma_5 = float(technicals.get("sma_5", 0))
    sma_20 = float(technicals.get("sma_20", 0))
    news_count = int(scan.get("news_count", 0))
    negative_count = int(scan.get("negative_news_count", 0))
    insider_count = int(scan.get("insider_filing_count", 0))
    scanner_score = int(scan.get("score", 0))

    if negative_count or insider_count:
        score = max(10, min(45, scanner_score - 20))
        return _candidate(symbol, "WAIT", score, quote.price, day_change, "Risk or filing noise is present; wait for a cleaner setup.", scan, technicals, market_trend)
    if market_trend == "DOWN":
        score = max(10, min(40, scanner_score))
        return _candidate(symbol, "WAIT", score, quote.price, day_change, "Broad market trend is down; quick-return setups need extra confirmation.", scan, technicals, market_trend)
    if day_change >= 18 or rsi >= 78:
        score = min(68, int(30 + min(day_change, 30) + min(volume_ratio * 5, 18)))
        return _candidate(symbol, "AVOID_CHASING", score, quote.price, day_change, "Move looks overextended; wait for a pullback or base.", scan, technicals, market_trend)

    score = 0
    score += min(28, max(0, day_change) * 3.2)
    score += min(24, max(0, volume_ratio - 1) * 12)
    score += min(18, news_count * 6)
    score += min(16, scanner_score * 0.25)
    if sma_5 and sma_20 and sma_5 > sma_20:
        score += 10
    if 52 <= rsi <= 68:
        score += 8
    elif 45 <= rsi < 52 or 68 < rsi <= 74:
        score += 3
    if market_trend == "UP":
        score += 6
    elif market_trend == "MIXED":
        score += 2

    score = int(max(0, min(100, score)))
    if score >= 72 and 1.5 <= day_change <= 14 and volume_ratio >= 1.5:
        action = "WATCH_FAST_GROWTH"
        reason = "Clean momentum with news, volume expansion, and trend support."
    elif score >= 55:
        action = "WATCH"
        reason = "Growth signal is forming, but it needs more confirmation."
    else:
        action = "WAIT"
        reason = "Momentum is not strong enough for a quick-return review."
    return _candidate(symbol, action, score, quote.price, day_change, reason, scan, technicals, market_trend)


def _candidate(
    symbol: str,
    action: str,
    score: int,
    price: float | None,
    day_change: float,
    reason: str,
    scan: dict[str, Any],
    technicals: dict[str, float],
    market_trend: str,
) -> dict[str, Any]:
    signals = [
        f"day {day_change:.2f}%",
        f"volume {float(technicals.get('volume_ratio', 1)):.2f}x",
        f"rsi {float(technicals.get('rsi_14', 50)):.1f}",
        f"news {int(scan.get('news_count', 0))}",
        f"market {market_trend.lower()}",
    ]
    return {
        "symbol": symbol,
        "action": action,
        "momentum_score": int(score),
        "current_price": round(price, 4) if price is not None else None,
        "day_change_pct": round(day_change, 2),
        "volume_ratio": round(float(technicals.get("volume_ratio", 1)), 2),
        "rsi_14": round(float(technicals.get("rsi_14", 50)), 2),
        "news_count": int(scan.get("news_count", 0)),
        "negative_news_count": int(scan.get("negative_news_count", 0)),
        "reason": reason,
        "signals": signals,
        "review_only": True,
    }
