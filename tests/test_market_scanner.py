from __future__ import annotations

from scripts.market_intel import NewsItem, Position, Watchlist
from scripts.market_scanner import (
    NEGATIVE_KEYWORDS,
    build_alert_message,
    match_insider_filings,
    scan_market,
    score_symbol,
)


def test_match_insider_filings_uses_symbol_and_aliases() -> None:
    filings = [
        {"title": "4 - APPLE INC (Issuer)", "url": "https://sec.gov/aapl"},
        {"title": "4 - Unrelated Corp", "url": "https://sec.gov/x"},
    ]

    matched = match_insider_filings("AAPL", ["APPLE INC"], filings)

    assert matched == [filings[0]]


def test_match_insider_filings_avoids_short_ticker_substring_false_positives() -> None:
    filings = [
        {"title": "4 - Gamma Holdings makes quarterly filing", "url": "https://sec.gov/nope"},
        {"title": "4 - MASTERCARD INCORPORATED (Issuer)", "url": "https://sec.gov/ma"},
    ]

    matched = match_insider_filings("MA", ["MASTERCARD INCORPORATED"], filings)

    assert matched == [filings[1]]


def test_match_insider_filings_matches_long_ticker_as_token_only() -> None:
    filings = [
        {"title": "4 - MEGAAPL HOLDINGS LLC", "url": "https://sec.gov/nope"},
        {"title": "4 - AAPL (Issuer)", "url": "https://sec.gov/aapl"},
    ]

    matched = match_insider_filings("AAPL", [], filings)

    assert matched == [filings[1]]


def test_score_symbol_prioritizes_insiders_news_and_negative_language() -> None:
    news = [
        NewsItem("AAPL", "Apple beats estimates", "https://example.com/1", ""),
        NewsItem("AAPL", f"Apple faces {NEGATIVE_KEYWORDS[0]} probe", "https://example.com/2", ""),
    ]
    filings = [{"title": "4 - APPLE INC (Issuer)", "url": "https://sec.gov/aapl"}]

    scan = score_symbol(
        symbol="AAPL",
        aliases=["APPLE INC"],
        news=news,
        insider_filings=filings,
        position_value=1200,
        total_value=1500,
        max_single_position_pct=50,
    )

    assert scan.symbol == "AAPL"
    assert scan.score >= 80
    assert scan.rating == "ACTION_REVIEW"
    assert any("insider" in reason.lower() for reason in scan.reasons)
    assert any("negative" in reason.lower() for reason in scan.reasons)
    assert any("concentration" in reason.lower() for reason in scan.reasons)


def test_scan_market_returns_sorted_scans_and_alerts() -> None:
    watchlist = Watchlist(
        symbols=["MSFT", "AAPL"],
        aliases={"AAPL": ["APPLE INC"], "MSFT": ["MICROSOFT CORP"]},
        positions=[Position("AAPL", 10, 100), Position("MSFT", 1, 100)],
        risk={"max_single_position_pct": 40},
    )
    snapshot = {
        "news": [{"symbol": "AAPL", "title": "Apple lawsuit expands", "url": "https://example.com", "published": ""}],
        "insider_filings": [{"title": "4 - APPLE INC (Issuer)", "url": "https://sec.gov/aapl"}],
    }

    result = scan_market(watchlist, snapshot)

    assert result["scans"][0]["symbol"] == "AAPL"
    assert result["alerts"]
    assert result["alerts"][0]["symbol"] == "AAPL"


def test_build_alert_message_is_concise() -> None:
    watchlist = Watchlist(symbols=["AAPL"], aliases={"AAPL": ["APPLE INC"]}, positions=[], risk={})
    result = scan_market(
        watchlist,
        {
            "news": [{"symbol": "AAPL", "title": "Apple shares jump", "url": "https://example.com", "published": ""}],
            "insider_filings": [{"title": "4 - APPLE INC (Issuer)", "url": "https://sec.gov/aapl"}],
        },
    )

    message = build_alert_message(result)

    assert "Market Scanner" in message
    assert "AAPL" in message
