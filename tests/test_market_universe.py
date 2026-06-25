from __future__ import annotations

from pathlib import Path

import httpx

from scripts.market_universe import (
    build_market_universe_payload,
    combine_symbols,
    fetch_nasdaqtrader_universe,
    load_market_universe,
    market_universe_snapshot,
)


def test_load_market_universe_defaults_when_missing(tmp_path: Path) -> None:
    symbols = load_market_universe(tmp_path / "missing.json")

    assert "SPY" in symbols
    assert "AAPL" in symbols


def test_load_market_universe_parses_json_and_limit(tmp_path: Path) -> None:
    path = tmp_path / "market_universe.json"
    path.write_text('{"symbols": ["aapl", "msft", "nvda"], "max_scan_symbols": 2}')

    symbols = load_market_universe(path)

    assert symbols == ["AAPL", "MSFT"]


def test_combine_symbols_keeps_priority_first_and_dedupes() -> None:
    combined = combine_symbols(["AAPL", "MSFT"], ["MSFT", "NVDA"], limit=3)

    assert combined == ["AAPL", "MSFT", "NVDA"]


def test_build_market_universe_payload_dedupes_and_sets_scan_cap() -> None:
    payload = build_market_universe_payload(["aapl", "MSFT", "AAPL"], max_scan_symbols=25)

    assert payload == {"max_scan_symbols": 25, "symbols": ["AAPL", "MSFT"]}


def test_market_universe_snapshot_reports_scan_cap_and_limitations(tmp_path: Path) -> None:
    path = tmp_path / "market_universe.json"
    path.write_text('{"symbols": ["aapl", "msft", "nvda"], "max_scan_symbols": 2}')

    payload = market_universe_snapshot(path, sample_limit=1)

    assert payload["status"] == "loaded"
    assert payload["total_symbols"] == 3
    assert payload["scan_symbols"] == 2
    assert payload["sample_symbols"] == ["AAPL"]
    assert payload["execution"]["alpaca_trading_api"] == "stock_and_crypto_orders"
    assert payload["execution"]["alpaca_paper_trading"] == "default_order_path"


def test_fetch_nasdaqtrader_universe_parses_listed_and_otherlisted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "nasdaqlisted" in str(request.url):
            return httpx.Response(200, text="Symbol|Security Name|Test Issue|ETF\nAAPL|Apple Inc|N|N\nTEST|Test Issue|Y|N\n")
        return httpx.Response(200, text="ACT Symbol|Security Name|Test Issue|ETF\nMSFT|Microsoft Corp|N|N\nSPY|ETF|N|Y\n")

    symbols = fetch_nasdaqtrader_universe(http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    assert symbols == ["AAPL", "MSFT"]
