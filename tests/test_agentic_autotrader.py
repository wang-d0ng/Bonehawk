from __future__ import annotations

import json
from pathlib import Path

from scripts.agentic_autotrader import (
    AgenticScanConfig,
    FakeXGBoostModel,
    load_xgboost_model,
    record_loss_postmortem,
    run_agentic_scan,
    run_loss_postmortem,
)
from scripts.quotes import Quote


def test_run_agentic_scan_ranks_narrative_price_dislocation_and_kelly_size() -> None:
    scan_result = {
        "scans": [
            {"symbol": "MSFT", "score": 64, "news_count": 3, "negative_news_count": 0, "insider_filing_count": 0, "reasons": ["recent news"]},
            {"symbol": "AAPL", "score": 18, "news_count": 1, "negative_news_count": 0, "insider_filing_count": 0, "reasons": ["quiet"]},
        ]
    }
    quotes = {
        "MSFT": Quote("MSFT", 100, previous_close=99.2),
        "AAPL": Quote("AAPL", 200, previous_close=203),
    }
    technicals = {
        "MSFT": {"volume_ratio": 2.4, "rsi_14": 58, "sma_5": 101, "sma_20": 96},
        "AAPL": {"volume_ratio": 0.8, "rsi_14": 44, "sma_5": 195, "sma_20": 199},
    }
    snapshot = {
        "news": [
            {"symbol": "MSFT", "title": "MSFT call buying surges as AI demand beats expectations", "url": "https://example.com/a", "published": ""},
            {"symbol": "MSFT", "title": "Analysts upgrade Microsoft after strong revenue growth", "url": "https://example.com/b", "published": ""},
        ],
        "social_items": [
            {"source": "reddit", "symbol": "MSFT", "text": "MSFT breakout volume and bullish unusual option flow", "url": "https://reddit.example/msft"},
            {"source": "x", "symbol": "MSFT", "text": "Top trader is long MSFT scalp setup", "url": "https://x.example/msft"},
        ],
        "insider_filings": [],
    }

    payload = run_agentic_scan(
        scan_result=scan_result,
        quotes=quotes,
        technicals=technicals,
        snapshot=snapshot,
        config=AgenticScanConfig(bankroll_usd=1000, max_trade_usd=1, max_kelly_fraction=0.05, window_minutes=15),
    )

    assert payload["agents"]["research"]["sources"]["reddit"] == 1
    assert payload["agents"]["research"]["sources"]["x"] == 1
    assert payload["opportunities"][0]["symbol"] == "MSFT"
    assert payload["opportunities"][0]["horizon_minutes"] == 15
    assert payload["opportunities"][0]["probability_up"] > 0.55
    assert payload["opportunities"][0]["edge_pct"] > 0
    assert 0 < payload["opportunities"][0]["kelly_fraction"] <= 0.05
    assert payload["opportunities"][0]["suggested_notional"] > 1
    assert payload["opportunities"][0]["suggested_notional"] <= 950
    assert payload["opportunities"][0]["sizing_method"] == "dynamic_account_probability"
    assert payload["opportunities"][0]["available_cash"] == 1000
    assert payload["opportunities"][0]["quantity_estimate"] > 0
    assert any("narrative" in reason.lower() for reason in payload["opportunities"][0]["reasons"])


def test_run_agentic_scan_uses_xgboost_model_when_supplied() -> None:
    payload = run_agentic_scan(
        scan_result={"scans": [{"symbol": "NVDA", "score": 30, "news_count": 0, "negative_news_count": 0, "insider_filing_count": 0}]},
        quotes={"NVDA": Quote("NVDA", 140, previous_close=139)},
        technicals={"NVDA": {"volume_ratio": 1.1, "rsi_14": 54, "sma_5": 141, "sma_20": 138}},
        snapshot={"news": [], "social_items": []},
        config=AgenticScanConfig(bankroll_usd=500, max_trade_usd=50),
        xgboost_model=FakeXGBoostModel(probability=0.78),
    )

    assert payload["agents"]["prediction"]["model"] == "xgboost"
    assert payload["opportunities"][0]["probability_up"] >= 0.74


def test_load_xgboost_model_returns_none_when_model_is_missing(tmp_path: Path) -> None:
    assert load_xgboost_model(tmp_path / "missing.json") is None


def test_run_agentic_scan_blocks_negative_narrative() -> None:
    payload = run_agentic_scan(
        scan_result={"scans": [{"symbol": "TSLA", "score": 72, "news_count": 2, "negative_news_count": 2, "insider_filing_count": 0}]},
        quotes={"TSLA": Quote("TSLA", 250, previous_close=260)},
        technicals={"TSLA": {"volume_ratio": 1.9, "rsi_14": 49, "sma_5": 245, "sma_20": 255}},
        snapshot={
            "news": [
                {"symbol": "TSLA", "title": "Tesla faces lawsuit after safety probe", "url": "https://example.com/tsla", "published": ""},
            ],
            "social_items": [{"source": "reddit", "symbol": "TSLA", "text": "bearish lawsuit risk and recall fear"}],
        },
        config=AgenticScanConfig(bankroll_usd=1000, max_trade_usd=100),
    )

    assert payload["opportunities"] == []
    assert payload["blocked"][0]["symbol"] == "TSLA"
    assert "negative" in payload["blocked"][0]["reason"].lower()


def test_run_agentic_scan_blocks_buy_candidates_in_down_market() -> None:
    payload = run_agentic_scan(
        scan_result={"scans": [{"symbol": "MSFT", "score": 80, "news_count": 4, "negative_news_count": 0, "insider_filing_count": 0}]},
        quotes={"MSFT": Quote("MSFT", 100, previous_close=99)},
        technicals={"MSFT": {"volume_ratio": 2.2, "rsi_14": 57, "sma_5": 101, "sma_20": 96}},
        snapshot={
            "market_trend": "DOWN",
            "news": [{"symbol": "MSFT", "title": "MSFT bullish breakout and growth beat", "url": "https://example.com", "published": ""}],
        },
        config=AgenticScanConfig(bankroll_usd=1000, max_trade_usd=100),
    )

    assert payload["opportunities"] == []
    assert "market trend is down" in payload["blocked"][0]["reason"].lower()


def test_run_agentic_scan_allows_down_market_when_paper_exploration_is_enabled() -> None:
    payload = run_agentic_scan(
        scan_result={"scans": [{"symbol": "MSFT", "score": 80, "news_count": 4, "negative_news_count": 0, "insider_filing_count": 0}]},
        quotes={"MSFT": Quote("MSFT", 100, previous_close=99)},
        technicals={"MSFT": {"volume_ratio": 2.2, "rsi_14": 57, "sma_5": 101, "sma_20": 96}},
        snapshot={
            "market_trend": "DOWN",
            "news": [{"symbol": "MSFT", "title": "MSFT bullish breakout and growth beat", "url": "https://example.com", "published": ""}],
        },
        config=AgenticScanConfig(bankroll_usd=1000, max_trade_usd=100, allow_downtrend=True),
    )

    assert payload["opportunities"][0]["symbol"] == "MSFT"
    assert "paper_downtrend_probe" in payload["opportunities"][0]["strategy_tags"]


def test_loss_postmortem_writes_five_agent_reviews(tmp_path: Path) -> None:
    outcome = {
        "symbol": "MSFT",
        "realized_pnl": -8.25,
        "entry_price": 100,
        "exit_price": 96,
        "reason": "stop hit",
        "signals": ["bullish narrative", "volume 2.2x"],
    }

    postmortem = run_loss_postmortem(outcome)
    row = record_loss_postmortem(tmp_path / "logs" / "postmortems.jsonl", postmortem)

    assert len(postmortem["agents"]) == 5
    assert {agent["name"] for agent in postmortem["agents"]} == {"research", "prediction", "risk", "execution", "portfolio"}
    assert row["symbol"] == "MSFT"
    assert "max_next_risk_fraction" in row["updates"]
    assert json.loads((tmp_path / "logs" / "postmortems.jsonl").read_text())["symbol"] == "MSFT"
