from __future__ import annotations

from scripts.growth_scanner import build_growth_candidates, build_growth_candidates_message
from scripts.quotes import Quote


def test_build_growth_candidates_ranks_fast_clean_momentum() -> None:
    scan_result = {
        "scans": [
            {"symbol": "ABCD", "score": 40, "rating": "WATCH", "news_count": 3, "negative_news_count": 0, "insider_filing_count": 0},
            {"symbol": "RISK", "score": 80, "rating": "ACTION_REVIEW", "news_count": 4, "negative_news_count": 1, "insider_filing_count": 0},
            {"symbol": "FLAT", "score": 25, "rating": "QUIET", "news_count": 0, "negative_news_count": 0, "insider_filing_count": 0},
        ]
    }
    quotes = {
        "ABCD": Quote("ABCD", 12.5, previous_close=11.5),
        "RISK": Quote("RISK", 40, previous_close=37),
        "FLAT": Quote("FLAT", 20, previous_close=20),
    }
    technicals = {
        "ABCD": {"sma_5": 12.1, "sma_10": 11.7, "sma_20": 10.9, "rsi_14": 61, "volume_ratio": 2.4},
        "RISK": {"sma_5": 39, "sma_10": 38, "sma_20": 36, "rsi_14": 60, "volume_ratio": 2.2},
        "FLAT": {"sma_5": 20, "sma_10": 20, "sma_20": 20, "rsi_14": 50, "volume_ratio": 1},
    }

    candidates = build_growth_candidates(scan_result, quotes, technicals, market_trend="UP")

    assert candidates[0]["symbol"] == "ABCD"
    assert candidates[0]["action"] == "WATCH_FAST_GROWTH"
    assert candidates[0]["momentum_score"] > candidates[1]["momentum_score"]
    assert candidates[0]["review_only"] is True
    assert any("volume" in signal for signal in candidates[0]["signals"])


def test_build_growth_candidates_penalizes_chasing_overextended_moves() -> None:
    scan_result = {"scans": [{"symbol": "HOT", "score": 45, "rating": "WATCH", "news_count": 2, "negative_news_count": 0, "insider_filing_count": 0}]}
    quotes = {"HOT": Quote("HOT", 130, previous_close=100)}
    technicals = {"HOT": {"sma_5": 120, "sma_10": 110, "sma_20": 95, "rsi_14": 83, "volume_ratio": 3.2}}

    candidates = build_growth_candidates(scan_result, quotes, technicals, market_trend="UP")

    assert candidates[0]["action"] == "AVOID_CHASING"
    assert "overextended" in candidates[0]["reason"].lower()


def test_build_growth_candidates_blocks_down_market() -> None:
    scan_result = {"scans": [{"symbol": "ABCD", "score": 45, "rating": "WATCH", "news_count": 3, "negative_news_count": 0, "insider_filing_count": 0}]}
    quotes = {"ABCD": Quote("ABCD", 12.5, previous_close=11.5)}
    technicals = {"ABCD": {"sma_5": 12.1, "sma_10": 11.7, "sma_20": 10.9, "rsi_14": 61, "volume_ratio": 2.4}}

    candidates = build_growth_candidates(scan_result, quotes, technicals, market_trend="DOWN")

    assert candidates[0]["action"] == "WAIT"
    assert "broad market" in candidates[0]["reason"].lower()


def test_build_growth_candidates_message_is_review_only() -> None:
    message = build_growth_candidates_message(
        [
            {
                "symbol": "ABCD",
                "action": "WATCH_FAST_GROWTH",
                "momentum_score": 82,
                "current_price": 12.5,
                "day_change_pct": 8.7,
                "reason": "Fast clean momentum.",
            }
        ]
    )

    assert "quick-return" in message.lower()
    assert "ABCD" in message
    assert "review only" in message.lower()
