from __future__ import annotations

import json
from pathlib import Path

from scripts.autopilot import AutopilotConfig, AutopilotEngine, load_autopilot_config
from scripts.market_intel import Watchlist
from scripts.quotes import PriceHistory, Quote


class FakeIntelClient:
    def __init__(self, symbols: list[str]) -> None:
        self.symbols = symbols

    def snapshot(self, watchlist: Watchlist) -> dict:
        return {
            "symbols": watchlist.symbols,
            "news": [
                {"symbol": symbol, "title": f"{symbol} announces expansion", "url": "https://example.com", "published": ""}
                for symbol in self.symbols
                for _ in range(3)
            ],
            "insider_filings": [],
            "risk_flags": [],
            "capabilities": {},
        }


class FakeQuoteClient:
    def get_quotes(self, symbols):
        return {symbol: Quote(symbol, 100, previous_close=97) for symbol in symbols}

    def get_histories(self, symbols):
        return {
            symbol: PriceHistory(
                symbol,
                closes=[80, 82, 84, 86, 88, 90, 92, 95, 97, 100],
                volumes=[100] * 9 + [260],
            )
            for symbol in symbols
        }


class DowntrendQuoteClient(FakeQuoteClient):
    def get_histories(self, symbols):
        histories = {}
        for symbol in symbols:
            if symbol in {"SPY", "QQQ"}:
                histories[symbol] = PriceHistory(
                    symbol,
                    closes=[140, 139, 138, 137, 136, 135, 134, 133, 132, 131, 130, 129, 128, 127, 126, 125, 124, 123, 122, 121, 120, 119, 118, 117, 116],
                    volumes=[100] * 24 + [160],
                )
            else:
                histories[symbol] = PriceHistory(
                    symbol,
                    closes=[80, 82, 84, 86, 88, 90, 92, 95, 97, 100],
                    volumes=[100] * 9 + [260],
                )
        return histories


class FakeAlpacaClient:
    def __init__(self) -> None:
        self.orders = []

    def snapshot(self) -> dict:
        return {"status": "configured", "paper": True}

    def place_order(self, request, confirm: str = "") -> dict:
        self.orders.append((request, confirm))
        return {
            "ok": True,
            "status": "submitted",
            "broker_status": "accepted",
            "broker_order_id": f"paper-{request.symbol}",
            "symbol": request.symbol,
            "side": request.side.upper(),
            "quantity": request.quantity,
            "notional": request.notional,
            "message": "paper order accepted",
            "review_only": False,
        }


def test_load_autopilot_config_defaults_to_paper_disabled(tmp_path: Path) -> None:
    config = load_autopilot_config(tmp_path / "missing.json")

    assert config.enabled is False
    assert config.mode == "paper"
    assert config.broker == "alpaca"
    assert config.allow_live is False


def test_load_autopilot_config_clamps_risky_values(tmp_path: Path) -> None:
    path = tmp_path / "autopilot.json"
    path.write_text(json.dumps({"enabled": True, "max_trade_usd": 5000, "max_open_positions": 999, "min_confidence": 5}))

    config = load_autopilot_config(path)

    assert config.enabled is True
    assert config.max_trade_usd == 1000
    assert config.max_open_positions == 25
    assert config.min_confidence == 35


def test_autopilot_scan_generates_paper_buy_plan(tmp_path: Path) -> None:
    config = AutopilotConfig(enabled=True, mode="paper", max_trade_usd=25, min_confidence=40)
    engine = AutopilotEngine(
        root=tmp_path,
        config=config,
        intel_client=FakeIntelClient(["MSFT"]),
        quote_client=FakeQuoteClient(),
        alpaca_client=FakeAlpacaClient(),
    )

    payload = engine.scan(Watchlist(symbols=["MSFT"], positions=[], risk={}, aliases={}))

    assert payload["ok"] is True
    assert payload["mode"] == "paper"
    assert payload["orders"][0]["symbol"] == "MSFT"
    assert payload["orders"][0]["side"] == "buy"
    assert 0 < payload["orders"][0]["notional"] <= 25
    assert payload["orders"][0]["review_only"] is True
    assert payload["agentic_scan"]["opportunities"]
    assert "kelly" in " ".join(payload["orders"][0]["signals"]).lower()


def test_autopilot_execute_places_paper_orders_and_records_decisions(tmp_path: Path) -> None:
    alpaca = FakeAlpacaClient()
    engine = AutopilotEngine(
        root=tmp_path,
        config=AutopilotConfig(enabled=True, mode="paper", max_trade_usd=25, min_confidence=40),
        intel_client=FakeIntelClient(["MSFT"]),
        quote_client=FakeQuoteClient(),
        alpaca_client=alpaca,
    )

    payload = engine.execute(Watchlist(symbols=["MSFT"], positions=[], risk={}, aliases={}))

    assert payload["ok"] is True
    assert payload["executed"][0]["broker_order_id"] == "paper-MSFT"
    assert alpaca.orders[0][0].symbol == "MSFT"
    assert alpaca.orders[0][0].stop_loss is None
    assert alpaca.orders[0][0].take_profit is None
    assert payload["orders"][0]["stop_loss"] is not None
    assert (tmp_path / "logs" / "decision_log.jsonl").exists()


def test_autopilot_paper_mode_can_submit_downtrend_probe_orders(tmp_path: Path) -> None:
    alpaca = FakeAlpacaClient()
    engine = AutopilotEngine(
        root=tmp_path,
        config=AutopilotConfig(enabled=True, mode="paper", max_trade_usd=25, min_confidence=40, paper_trade_downtrend=True),
        intel_client=FakeIntelClient(["MSFT"]),
        quote_client=DowntrendQuoteClient(),
        alpaca_client=alpaca,
    )

    payload = engine.execute(Watchlist(symbols=["MSFT"], positions=[], risk={}, aliases={}))

    assert payload["market_trend"] == "DOWN"
    assert payload["ok"] is True
    assert payload["executed"][0]["broker_order_id"] == "paper-MSFT"
    assert alpaca.orders[0][0].symbol == "MSFT"
    assert "paper_downtrend_probe" in " ".join(payload["orders"][0]["signals"])


def test_autopilot_live_mode_blocks_downtrend_entries(tmp_path: Path) -> None:
    alpaca = FakeAlpacaClient()
    engine = AutopilotEngine(
        root=tmp_path,
        config=AutopilotConfig(enabled=True, mode="live", allow_live=True, max_trade_usd=25, min_confidence=40),
        intel_client=FakeIntelClient(["MSFT"]),
        quote_client=DowntrendQuoteClient(),
        alpaca_client=alpaca,
    )

    payload = engine.scan(Watchlist(symbols=["MSFT"], positions=[], risk={}, aliases={}))

    assert payload["market_trend"] == "DOWN"
    assert payload["orders"] == []
    assert payload["agentic_scan"]["opportunities"] == []
    assert alpaca.orders == []


def test_autopilot_blocks_when_disabled(tmp_path: Path) -> None:
    engine = AutopilotEngine(
        root=tmp_path,
        config=AutopilotConfig(enabled=False),
        intel_client=FakeIntelClient(["MSFT"]),
        quote_client=FakeQuoteClient(),
        alpaca_client=FakeAlpacaClient(),
    )

    payload = engine.execute(Watchlist(symbols=["MSFT"], positions=[], risk={}, aliases={}))

    assert payload["ok"] is False
    assert payload["status"] == "disabled"


def test_autopilot_blocks_live_mode_without_execution_confirmation(tmp_path: Path) -> None:
    engine = AutopilotEngine(
        root=tmp_path,
        config=AutopilotConfig(enabled=True, mode="live", allow_live=True),
        intel_client=FakeIntelClient(["MSFT"]),
        quote_client=FakeQuoteClient(),
        alpaca_client=FakeAlpacaClient(),
    )

    payload = engine.execute(Watchlist(symbols=["MSFT"], positions=[], risk={}, aliases={}))

    assert payload["ok"] is False
    assert payload["status"] == "confirmation_required"


def test_autopilot_risk_blocks_orders_above_position_limit(tmp_path: Path) -> None:
    engine = AutopilotEngine(
        root=tmp_path,
        config=AutopilotConfig(enabled=True, mode="paper", max_open_positions=0),
        intel_client=FakeIntelClient(["MSFT"]),
        quote_client=FakeQuoteClient(),
        alpaca_client=FakeAlpacaClient(),
    )

    payload = engine.scan(Watchlist(symbols=["MSFT"], positions=[], risk={}, aliases={}))

    assert payload["orders"] == []
    assert any(item["status"] == "blocked" for item in payload["blocked"])
