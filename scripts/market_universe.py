from __future__ import annotations

import json
from pathlib import Path

import httpx

DEFAULT_UNIVERSE = [
    "SPY",
    "QQQ",
    "DIA",
    "IWM",
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "META",
    "GOOGL",
    "TSLA",
    "AMD",
    "NFLX",
    "AVGO",
    "JPM",
    "V",
    "MA",
    "UNH",
    "LLY",
    "XOM",
]


def load_market_universe(path: Path) -> list[str]:
    if not path.exists():
        return DEFAULT_UNIVERSE.copy()
    raw = json.loads(path.read_text())
    symbols = _normalize_symbols(raw.get("symbols", DEFAULT_UNIVERSE))
    limit = int(raw.get("max_scan_symbols", len(symbols)))
    return symbols[:limit]


def market_universe_snapshot(path: Path, sample_limit: int = 120) -> dict[str, object]:
    if path.exists():
        raw = json.loads(path.read_text())
        symbols = _normalize_symbols(raw.get("symbols", DEFAULT_UNIVERSE))
        max_scan_symbols = int(raw.get("max_scan_symbols", len(symbols)))
        status = "loaded"
        source = raw.get("source", "configured_market_universe")
    else:
        symbols = DEFAULT_UNIVERSE.copy()
        max_scan_symbols = len(symbols)
        status = "default"
        source = "default_market_universe"

    scan_symbols = symbols[:max_scan_symbols]
    return {
        "status": status,
        "source": source,
        "total_symbols": len(symbols),
        "scan_symbols": len(scan_symbols),
        "max_scan_symbols": max_scan_symbols,
        "sample_symbols": scan_symbols[:sample_limit],
        "execution": {
            "alpaca_trading_api": "stock_and_crypto_orders",
            "alpaca_paper_trading": "default_order_path",
        },
    }


def combine_symbols(priority: list[str], universe: list[str], limit: int) -> list[str]:
    combined: list[str] = []
    seen: set[str] = set()
    for symbol in [*priority, *universe]:
        normalized = symbol.upper()
        if normalized in seen:
            continue
        seen.add(normalized)
        combined.append(normalized)
        if len(combined) >= limit:
            break
    return combined


def build_market_universe_payload(symbols: list[str], max_scan_symbols: int) -> dict[str, object]:
    return {
        "max_scan_symbols": max_scan_symbols,
        "symbols": _normalize_symbols(symbols),
    }


def fetch_nasdaqtrader_universe(http_client: httpx.Client | None = None) -> list[str]:
    client = http_client or httpx.Client(timeout=15, follow_redirects=True)
    symbols: list[str] = []
    for url, field in [
        ("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt", "Symbol"),
        ("https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt", "ACT Symbol"),
    ]:
        response = client.get(url)
        response.raise_for_status()
        symbols.extend(_parse_symbol_directory(response.text, field))
    return sorted(dict.fromkeys(symbols))


def _parse_symbol_directory(text: str, field: str) -> list[str]:
    rows = [line.strip() for line in text.splitlines() if "|" in line]
    if not rows:
        return []
    headers = rows[0].split("|")
    symbol_idx = headers.index(field)
    test_idx = headers.index("Test Issue") if "Test Issue" in headers else None
    etf_idx = headers.index("ETF") if "ETF" in headers else None
    symbols: list[str] = []
    for row in rows[1:]:
        parts = row.split("|")
        if len(parts) <= symbol_idx:
            continue
        if test_idx is not None and len(parts) > test_idx and parts[test_idx] == "Y":
            continue
        if etf_idx is not None and len(parts) > etf_idx and parts[etf_idx] == "Y":
            continue
        symbol = parts[symbol_idx].strip().upper()
        if symbol and "$" not in symbol and "." not in symbol:
            symbols.append(symbol)
    return symbols


def _normalize_symbols(symbols: object) -> list[str]:
    if not isinstance(symbols, list):
        symbols = DEFAULT_UNIVERSE
    normalized: list[str] = []
    seen: set[str] = set()
    for symbol in symbols:
        value = str(symbol).strip().upper()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized
