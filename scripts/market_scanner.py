from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from scripts.market_intel import NewsItem, Position, Watchlist

NEGATIVE_KEYWORDS = [
    "lawsuit",
    "probe",
    "investigation",
    "downgrade",
    "recall",
    "misses",
    "fraud",
    "sec",
    "doj",
    "bankruptcy",
]


@dataclass(frozen=True)
class SymbolScan:
    symbol: str
    score: int
    rating: str
    reasons: list[str]
    news_count: int
    insider_filing_count: int
    negative_news_count: int
    alert: bool


def scan_market(watchlist: Watchlist, snapshot: dict[str, Any]) -> dict[str, Any]:
    news = [_news_from_payload(item) for item in snapshot.get("news", [])]
    filings = [dict(item) for item in snapshot.get("insider_filings", [])]
    position_values = _position_values(watchlist.positions, snapshot)
    total_value = sum(position_values.values())
    max_single = watchlist.risk.get("max_single_position_pct", 25)

    scans = [
        score_symbol(
            symbol=symbol,
            aliases=watchlist.aliases.get(symbol, []),
            news=[item for item in news if item.symbol == symbol],
            insider_filings=filings,
            position_value=position_values.get(symbol, 0),
            total_value=total_value,
            max_single_position_pct=max_single,
        )
        for symbol in watchlist.symbols
    ]
    scans = sorted(scans, key=lambda item: item.score, reverse=True)
    alerts = [scan for scan in scans if scan.alert]
    return {
        "scans": [asdict(scan) for scan in scans],
        "alerts": [asdict(scan) for scan in alerts],
        "summary": {
            "symbols_scanned": len(scans),
            "alerts": len(alerts),
            "top_symbol": scans[0].symbol if scans else None,
        },
    }


def score_symbol(
    symbol: str,
    aliases: list[str],
    news: list[NewsItem],
    insider_filings: list[dict[str, str]],
    position_value: float,
    total_value: float,
    max_single_position_pct: float,
) -> SymbolScan:
    matched_filings = match_insider_filings(symbol, aliases, insider_filings)
    negative_news = [item for item in news if _has_negative_keyword(item.title)]
    reasons: list[str] = []
    score = 0

    if news:
        points = min(30, len(news) * 10)
        score += points
        reasons.append(f"{len(news)} recent news item(s).")

    if matched_filings:
        points = min(45, len(matched_filings) * 30)
        score += points
        reasons.append(f"{len(matched_filings)} recent insider filing match(es).")

    if negative_news:
        points = min(30, len(negative_news) * 20)
        score += points
        reasons.append(f"{len(negative_news)} negative-risk headline(s).")

    if total_value > 0 and position_value > 0:
        pct = (position_value / total_value) * 100
        if pct > max_single_position_pct:
            score += 20
            reasons.append(f"Position concentration is {pct:.1f}% of configured portfolio.")

    score = min(100, score)
    rating = "ACTION_REVIEW" if score >= 70 else "WATCH" if score >= 35 else "QUIET"
    if not reasons:
        reasons.append("No notable scanner signal.")
    return SymbolScan(
        symbol=symbol,
        score=score,
        rating=rating,
        reasons=reasons,
        news_count=len(news),
        insider_filing_count=len(matched_filings),
        negative_news_count=len(negative_news),
        alert=rating == "ACTION_REVIEW" or bool(matched_filings),
    )


def match_insider_filings(symbol: str, aliases: list[str], filings: list[dict[str, str]]) -> list[dict[str, str]]:
    normalized_symbol = symbol.upper()
    alias_terms = [alias.upper() for alias in aliases if alias.strip()]
    matched = []
    for filing in filings:
        title = filing.get("title", "").upper()
        alias_match = any(term in title for term in alias_terms)
        symbol_match = len(normalized_symbol) >= 3 and _matches_ticker_token(normalized_symbol, title)
        if alias_match or symbol_match:
            matched.append(filing)
    return matched


def build_alert_message(scan_result: dict[str, Any]) -> str:
    alerts = scan_result.get("alerts", [])
    if not alerts:
        return "Market Scanner: no ACTION_REVIEW alerts."
    lines = ["Market Scanner alerts:"]
    for alert in alerts[:5]:
        reason = "; ".join(alert.get("reasons", [])[:2])
        lines.append(f"{alert['symbol']} score {alert['score']}/100 ({alert['rating']}): {reason}")
    lines.append("Review only. No live stock order was placed.")
    return "\n".join(lines)


def _news_from_payload(item: Any) -> NewsItem:
    if isinstance(item, NewsItem):
        return item
    return NewsItem(
        symbol=str(item.get("symbol", "")).upper(),
        title=str(item.get("title", "")),
        url=str(item.get("url", "")),
        published=str(item.get("published", "")),
    )


def _position_values(positions: list[Position], snapshot: dict[str, Any]) -> dict[str, float]:
    performance_positions = snapshot.get("portfolio_performance", {}).get("positions", [])
    if performance_positions:
        return {
            str(position.get("symbol", "")).upper(): float(position.get("market_value", 0))
            for position in performance_positions
            if position.get("symbol")
        }
    values: dict[str, float] = {}
    for position in positions:
        values[position.symbol] = values.get(position.symbol, 0) + (position.quantity * position.cost_basis)
    return values


def _has_negative_keyword(title: str) -> bool:
    lowered = title.lower()
    return any(keyword in lowered for keyword in NEGATIVE_KEYWORDS)


def _matches_ticker_token(symbol: str, text: str) -> bool:
    return re.search(rf"(^|[^A-Z0-9]){re.escape(symbol)}([^A-Z0-9]|$)", text) is not None
