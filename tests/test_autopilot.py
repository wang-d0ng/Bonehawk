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
    def __init__(self, cash: float = 1000, buying_power: float | None = None, portfolio_value: float | None = None) -> None:
        self.orders = []
        self.cash = cash
        self.buying_power = cash if buying_power is None else buying_power
        self.portfolio_value = cash if portfolio_value is None else portfolio_value

    def snapshot(self) -> dict:
        return {"status": "configured", "paper": True}

    def get_account(self) -> dict:
        return {"cash": str(self.cash), "buying_power": str(self.buying_power), "portfolio_value": str(self.portfolio_value)}

    def get_positions(self) -> list[dict]:
        return []

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


class ProfitTakingQuoteClient(FakeQuoteClient):
    def get_quotes(self, symbols):
        return {symbol: Quote(symbol, 105, previous_close=104) for symbol in symbols}

    def get_histories(self, symbols):
        return {
            symbol: PriceHistory(
                symbol,
                closes=[96, 98, 100, 102, 104, 105, 105.2, 105.1, 105.05, 105],
                volumes=[100, 100, 100, 100, 120, 140, 180, 220, 260, 300],
            )
            for symbol in symbols
        }


class ProfitablePositionAlpacaClient(FakeAlpacaClient):
    def get_positions(self) -> list[dict]:
        return [
            {
                "symbol": "MSFT",
                "qty": "2",
                "avg_entry_price": "100",
                "cost_basis": "200",
                "current_price": "105",
                "market_value": "210",
                "unrealized_pl": "10",
                "unrealized_plpc": "0.05",
                "change_today": "0.01",
            }
        ]


class PartlyReservedPositionAlpacaClient(FakeAlpacaClient):
    def get_positions(self) -> list[dict]:
        return [
            {
                "symbol": "MSFT",
                "qty": "2",
                "qty_available": "1.25",
                "avg_entry_price": "100",
                "cost_basis": "200",
                "current_price": "105",
                "market_value": "210",
                "unrealized_pl": "10",
                "unrealized_plpc": "0.05",
                "change_today": "0.01",
            }
        ]


class HighPrecisionAvailablePositionAlpacaClient(FakeAlpacaClient):
    def get_positions(self) -> list[dict]:
        return [
            {
                "symbol": "MSFT",
                "qty": "2",
                "qty_available": "1.234567899",
                "avg_entry_price": "100",
                "cost_basis": "200",
                "current_price": "105",
                "market_value": "210",
                "unrealized_pl": "10",
                "unrealized_plpc": "0.05",
                "change_today": "0.01",
            }
        ]


class ReservedPositionAlpacaClient(FakeAlpacaClient):
    def get_positions(self) -> list[dict]:
        return [
            {
                "symbol": "MSFT",
                "qty": "2",
                "qty_available": "0",
                "avg_entry_price": "100",
                "cost_basis": "200",
                "current_price": "105",
                "market_value": "210",
                "unrealized_pl": "10",
                "unrealized_plpc": "0.05",
                "change_today": "0.01",
            }
        ]


class RejectingProfitablePositionAlpacaClient(ProfitablePositionAlpacaClient):
    def place_order(self, request, confirm: str = "") -> dict:
        self.orders.append((request, confirm))
        return {
            "ok": False,
            "status": "rejected",
            "symbol": request.symbol,
            "side": request.side.upper(),
            "quantity": request.quantity,
            "notional": request.notional,
            "message": "paper order rejected",
            "review_only": True,
        }


class NonFractionableAlpacaClient(FakeAlpacaClient):
    def get_asset(self, symbol: str) -> dict:
        return {"symbol": symbol.upper(), "status": "active", "tradable": True, "fractionable": False}


def test_load_autopilot_config_defaults_to_paper_disabled(tmp_path: Path) -> None:
    config = load_autopilot_config(tmp_path / "missing.json")

    assert config.enabled is False
    assert config.mode == "paper"
    assert config.broker == "alpaca"
    assert config.allow_live is False


def test_load_autopilot_config_clamps_risky_values(tmp_path: Path) -> None:
    path = tmp_path / "autopilot.json"
    path.write_text(json.dumps({"enabled": True, "max_trade_usd": 5000, "max_open_positions": 999, "min_confidence": 5, "scan_window_minutes": 30}))

    config = load_autopilot_config(path)

    assert config.enabled is True
    assert config.max_trade_usd == 1000
    assert config.max_open_positions == 25
    assert config.min_confidence == 35
    assert config.scan_window_minutes == 5


def test_autopilot_profit_exit_sells_position_before_new_buys(tmp_path: Path) -> None:
    alpaca = ProfitablePositionAlpacaClient()
    engine = AutopilotEngine(
        root=tmp_path,
        config=AutopilotConfig(enabled=True, mode="paper", max_trade_usd=25, min_confidence=40, scan_window_minutes=5),
        intel_client=FakeIntelClient(["MSFT"]),
        quote_client=ProfitTakingQuoteClient(),
        alpaca_client=alpaca,
    )

    scan = engine.scan(Watchlist(symbols=["MSFT"], positions=[], risk={}, aliases={}))
    execution = engine.execute(Watchlist(symbols=["MSFT"], positions=[], risk={}, aliases={}))

    assert scan["orders"][0]["side"] == "sell"
    assert scan["orders"][0]["action"] == "AUTO_SELL_PROFIT_TAKE"
    assert scan["orders"][0]["quantity"] == 2
    assert scan["orders"][0]["notional"] == 210
    assert scan["orders"][0]["exit_window_minutes"] == 5
    assert scan["orders"][0]["profit_target_pct"] <= scan["orders"][0]["unrealized_pnl_pct"]
    assert execution["executed"][0]["side"] == "SELL"
    assert alpaca.orders[0][0].symbol == "MSFT"
    assert alpaca.orders[0][0].side == "sell"
    assert alpaca.orders[0][0].quantity == 2
    assert alpaca.orders[0][0].notional is None


def test_autopilot_profit_exit_uses_qty_available_for_sell(tmp_path: Path) -> None:
    alpaca = PartlyReservedPositionAlpacaClient()
    engine = AutopilotEngine(
        root=tmp_path,
        config=AutopilotConfig(enabled=True, mode="paper", max_trade_usd=25, min_confidence=40, scan_window_minutes=5),
        intel_client=FakeIntelClient(["MSFT"]),
        quote_client=ProfitTakingQuoteClient(),
        alpaca_client=alpaca,
    )

    payload = engine.execute(Watchlist(symbols=["MSFT"], positions=[], risk={}, aliases={}))

    assert payload["orders"][0]["quantity"] == 1.25
    assert payload["orders"][0]["held_quantity"] == 2
    assert payload["orders"][0]["available_quantity"] == 1.25
    assert alpaca.orders[0][0].quantity == 1.25


def test_autopilot_profit_exit_floors_qty_available_for_alpaca(tmp_path: Path) -> None:
    alpaca = HighPrecisionAvailablePositionAlpacaClient()
    engine = AutopilotEngine(
        root=tmp_path,
        config=AutopilotConfig(enabled=True, mode="paper", max_trade_usd=25, min_confidence=40, scan_window_minutes=5),
        intel_client=FakeIntelClient(["MSFT"]),
        quote_client=ProfitTakingQuoteClient(),
        alpaca_client=alpaca,
    )

    payload = engine.execute(Watchlist(symbols=["MSFT"], positions=[], risk={}, aliases={}))

    assert payload["orders"][0]["available_quantity"] == 1.23456789
    assert payload["orders"][0]["quantity"] == 1.23456789
    assert alpaca.orders[0][0].quantity == 1.23456789


def test_autopilot_profit_exit_blocks_when_qty_is_reserved(tmp_path: Path) -> None:
    alpaca = ReservedPositionAlpacaClient()
    engine = AutopilotEngine(
        root=tmp_path,
        config=AutopilotConfig(enabled=True, mode="paper", max_trade_usd=25, min_confidence=40, scan_window_minutes=5),
        intel_client=FakeIntelClient(["MSFT"]),
        quote_client=ProfitTakingQuoteClient(),
        alpaca_client=alpaca,
    )

    payload = engine.execute(Watchlist(symbols=["MSFT"], positions=[], risk={}, aliases={}))

    assert payload["orders"] == []
    assert payload["executed"] == []
    assert any(item["status"] == "reserved" and item["symbol"] == "MSFT" for item in payload["blocked"])
    assert alpaca.orders == []


def test_autopilot_profit_exit_cooldown_blocks_duplicate_sell(tmp_path: Path) -> None:
    alpaca = ProfitablePositionAlpacaClient()
    engine = AutopilotEngine(
        root=tmp_path,
        config=AutopilotConfig(enabled=True, mode="paper", max_trade_usd=25, min_confidence=40, scan_window_minutes=5),
        intel_client=FakeIntelClient(["MSFT"]),
        quote_client=ProfitTakingQuoteClient(),
        alpaca_client=alpaca,
    )

    first = engine.execute(Watchlist(symbols=["MSFT"], positions=[], risk={}, aliases={}))
    second = engine.execute(Watchlist(symbols=["MSFT"], positions=[], risk={}, aliases={}))

    assert first["executed"][0]["side"] == "SELL"
    assert second["executed"] == []
    assert second["orders"] == []
    cooldown = next(item for item in second["blocked"] if item["status"] == "cooldown")
    assert "recent" in cooldown["reason"].lower()
    assert len(alpaca.orders) == 1


def test_autopilot_rejected_exit_attempt_cools_down_duplicate_sell(tmp_path: Path) -> None:
    alpaca = RejectingProfitablePositionAlpacaClient()
    engine = AutopilotEngine(
        root=tmp_path,
        config=AutopilotConfig(enabled=True, mode="paper", max_trade_usd=25, min_confidence=40, scan_window_minutes=5),
        intel_client=FakeIntelClient(["MSFT"]),
        quote_client=ProfitTakingQuoteClient(),
        alpaca_client=alpaca,
    )

    first = engine.execute(Watchlist(symbols=["MSFT"], positions=[], risk={}, aliases={}))
    second = engine.execute(Watchlist(symbols=["MSFT"], positions=[], risk={}, aliases={}))

    assert first["executed"][0]["status"] == "rejected"
    assert second["executed"] == []
    assert any(item["status"] == "cooldown" for item in second["blocked"])
    assert len(alpaca.orders) == 1


def test_autopilot_scan_generates_paper_buy_plan(tmp_path: Path) -> None:
    config = AutopilotConfig(enabled=True, mode="paper", max_trade_usd=1, min_confidence=40)
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
    assert payload["orders"][0]["notional"] > 1
    assert payload["orders"][0]["notional"] <= payload["account_state"]["available_cash"]
    assert payload["orders"][0]["sizing_method"] == "dynamic_account_probability"
    assert payload["orders"][0]["quantity_estimate"] > 0
    assert payload["orders"][0]["review_only"] is True
    assert payload["agentic_scan"]["opportunities"]
    assert "kelly" in " ".join(payload["orders"][0]["signals"]).lower()
    assert "available_cash" in payload["agentic_scan"]["opportunities"][0]


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


def test_autopilot_buy_uses_whole_quantity_for_non_fractionable_assets(tmp_path: Path) -> None:
    alpaca = NonFractionableAlpacaClient(cash=10000)
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
    assert alpaca.orders[0][0].quantity >= 1
    assert float(alpaca.orders[0][0].quantity).is_integer()
    assert alpaca.orders[0][0].notional is None


def test_autopilot_blocks_non_fractionable_buy_when_cash_cannot_buy_one_share(tmp_path: Path) -> None:
    alpaca = NonFractionableAlpacaClient(cash=5)
    engine = AutopilotEngine(
        root=tmp_path,
        config=AutopilotConfig(enabled=True, mode="paper", max_trade_usd=25, min_confidence=40),
        intel_client=FakeIntelClient(["MSFT"]),
        quote_client=FakeQuoteClient(),
        alpaca_client=alpaca,
    )

    payload = engine.execute(Watchlist(symbols=["MSFT"], positions=[], risk={}, aliases={}))

    assert payload["executed"] == []
    assert any(item["status"] == "whole_share_required" for item in payload["blocked"])
    assert alpaca.orders == []


def test_autopilot_dynamic_sizing_allocates_available_cash_across_orders(tmp_path: Path) -> None:
    alpaca = FakeAlpacaClient(cash=30, buying_power=300, portfolio_value=1000)
    engine = AutopilotEngine(
        root=tmp_path,
        config=AutopilotConfig(enabled=True, mode="paper", max_trade_usd=1, min_confidence=40, max_open_positions=5),
        intel_client=FakeIntelClient(["MSFT", "NVDA", "AAPL"]),
        quote_client=FakeQuoteClient(),
        alpaca_client=alpaca,
    )

    payload = engine.scan(Watchlist(symbols=["MSFT", "NVDA", "AAPL"], positions=[], risk={}, aliases={}))
    total_notional = sum(order["notional"] for order in payload["orders"])

    assert payload["account_state"]["available_cash"] == 30
    assert payload["orders"]
    assert total_notional <= 30
    assert all(order["notional"] <= 30 for order in payload["orders"])
    assert all(order["sizing_method"] == "dynamic_account_probability" for order in payload["orders"])


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
