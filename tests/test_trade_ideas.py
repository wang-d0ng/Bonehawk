from __future__ import annotations

from scripts.market_intel import Position
from scripts.quotes import Quote
from scripts.trade_ideas import build_market_trend, build_trade_ideas, build_trade_ideas_message


def test_build_trade_ideas_recommends_buy_review_for_positive_clean_scanner_signal() -> None:
    scan_result = {
        "scans": [
            {
                "symbol": "MSFT",
                "score": 45,
                "rating": "WATCH",
                "reasons": ["Recent news."],
                "negative_news_count": 0,
                "insider_filing_count": 0,
            }
        ]
    }
    quotes = {"MSFT": Quote("MSFT", 500, previous_close=490)}

    technicals = {"MSFT": {"sma_5": 505, "sma_10": 498, "sma_20": 480, "rsi_14": 58, "volume_ratio": 1.8}}

    ideas = build_trade_ideas(scan_result, quotes, positions=[], risk={"stop_loss_pct": 3}, technicals=technicals, market_trend="UP")

    assert ideas[0]["symbol"] == "MSFT"
    assert ideas[0]["action"] == "BUY_REVIEW"
    assert ideas[0]["current_price"] == 500
    assert ideas[0]["stop_loss"] == 485
    assert ideas[0]["take_profit"] == 530
    assert any("volume" in signal for signal in ideas[0]["signals"])
    assert ideas[0]["review_only"] is True


def test_build_trade_ideas_recommends_trim_for_large_open_gain() -> None:
    scan_result = {"scans": [{"symbol": "AAPL", "score": 50, "rating": "WATCH", "reasons": [], "negative_news_count": 0, "insider_filing_count": 0}]}
    quotes = {"AAPL": Quote("AAPL", 294, previous_close=298)}
    positions = [Position("AAPL", quantity=1, cost_basis=190)]

    ideas = build_trade_ideas(scan_result, quotes, positions=positions, risk={"take_profit_alert_pct": 20})

    assert ideas[0]["action"] == "TRIM_REVIEW"
    assert "open gain" in ideas[0]["reason"]


def test_build_trade_ideas_recommends_sell_review_for_held_negative_risk() -> None:
    scan_result = {
        "scans": [
            {
                "symbol": "TSLA",
                "score": 75,
                "rating": "ACTION_REVIEW",
                "reasons": ["Negative-risk headline."],
                "negative_news_count": 1,
                "insider_filing_count": 0,
            }
        ]
    }
    quotes = {"TSLA": Quote("TSLA", 240, previous_close=260)}
    positions = [Position("TSLA", quantity=1, cost_basis=250)]

    ideas = build_trade_ideas(scan_result, quotes, positions=positions, risk={"daily_loss_alert_pct": 3})

    assert ideas[0]["action"] == "SELL_REVIEW"
    assert "risk" in ideas[0]["reason"]


def test_build_trade_ideas_marks_unpriced_symbols_no_trade() -> None:
    scan_result = {"scans": [{"symbol": "XYZ", "score": 30, "rating": "QUIET", "reasons": [], "negative_news_count": 0, "insider_filing_count": 0}]}

    ideas = build_trade_ideas(scan_result, quotes={}, positions=[], risk={})

    assert ideas[0]["action"] == "NO_TRADE"
    assert "price" in ideas[0]["reason"]


def test_build_trade_ideas_blocks_buy_when_market_trend_is_down() -> None:
    scan_result = {"scans": [{"symbol": "MSFT", "score": 60, "rating": "WATCH", "reasons": [], "negative_news_count": 0, "insider_filing_count": 0}]}
    quotes = {"MSFT": Quote("MSFT", 500, previous_close=490)}
    technicals = {"MSFT": {"sma_5": 505, "sma_10": 498, "sma_20": 480, "rsi_14": 58, "volume_ratio": 2}}

    ideas = build_trade_ideas(scan_result, quotes, positions=[], risk={}, technicals=technicals, market_trend="DOWN")

    assert ideas[0]["action"] == "NO_TRADE"
    assert "market trend" in ideas[0]["reason"]


def test_build_market_trend_uses_spy_and_qqq_technicals() -> None:
    trend = build_market_trend(
        {
            "SPY": {"sma_5": 510, "sma_20": 500},
            "QQQ": {"sma_5": 490, "sma_20": 500},
        }
    )

    assert trend == "MIXED"


def test_build_trade_ideas_message_lists_actions() -> None:
    message = build_trade_ideas_message(
        [
            {
                "symbol": "MSFT",
                "action": "BUY_REVIEW",
                "confidence": 70,
                "current_price": 500,
                "stop_loss": 485,
                "take_profit": 530,
                "reason": "Positive daily move.",
            }
        ]
    )

    assert "Trade ideas" in message
    assert "MSFT BUY_REVIEW" in message
    assert "stop 485" in message
