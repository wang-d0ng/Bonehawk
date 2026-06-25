from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from scripts.quotes import Quote, YahooQuoteClient, compute_portfolio_performance

DEFAULT_SYMBOLS = ["AAPL", "MSFT", "NVDA", "TSLA", "BTC-USD"]
SEC_USER_AGENT = "market-intel-bot contact@example.com"
SOCIAL_USER_AGENT = "BonehawkMarketResearch/0.1"


@dataclass(frozen=True)
class Position:
    symbol: str
    quantity: float
    cost_basis: float


@dataclass(frozen=True)
class Watchlist:
    symbols: list[str]
    positions: list[Position]
    risk: dict[str, float]
    aliases: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class NewsItem:
    symbol: str
    title: str
    url: str
    published: str


@dataclass(frozen=True)
class RiskInput:
    positions: list[Position]
    quotes: dict[str, Quote]
    risk: dict[str, float]


class MarketIntelClient:
    def __init__(self, http_client: httpx.Client | None = None, quote_client: YahooQuoteClient | None = None) -> None:
        self.http_client = http_client or httpx.Client(timeout=12, follow_redirects=True)
        self.quote_client = quote_client or YahooQuoteClient()

    def snapshot(self, watchlist: Watchlist) -> dict[str, Any]:
        news = self.fetch_news(watchlist.symbols)
        social_items = self.fetch_social_items(watchlist.symbols)
        filings = self.fetch_recent_form4_filings()
        quote_symbols = [position.symbol for position in watchlist.positions]
        quotes = self.quote_client.get_quotes(quote_symbols)
        performance = compute_portfolio_performance(watchlist.positions, quotes)
        return {
            "symbols": watchlist.symbols,
            "positions": [asdict(position) for position in watchlist.positions],
            "quotes": {symbol: asdict(quote) | {"change_pct": round(quote.change_pct, 2)} for symbol, quote in quotes.items()},
            "portfolio_performance": performance,
            "news": [asdict(item) for item in news],
            "social_items": social_items,
            "insider_filings": filings,
            "risk_flags": compute_risk_flags(RiskInput(watchlist.positions, quotes, watchlist.risk)),
            "capabilities": {
                "broker": "Alpaca paper trading by default",
                "stock_trading": "Manual tickets and autopilot route through Alpaca",
                "live_orders": "Require Alpaca live permission and explicit confirmation",
            },
        }

    def fetch_news(self, symbols: list[str]) -> list[NewsItem]:
        items: list[NewsItem] = []
        for symbol in symbols:
            if symbol.endswith("-USD"):
                continue
            url = f"https://news.google.com/rss/search?q={quote(symbol + ' stock')}&hl=en-US&gl=US&ceid=US:en"
            try:
                response = self.http_client.get(url)
                response.raise_for_status()
            except httpx.HTTPError:
                continue
            items.extend(parse_yahoo_rss(symbol, response.text)[:8])
            if len(items) >= 80:
                break
        return items[:80]

    def fetch_social_items(self, symbols: list[str]) -> list[dict[str, str]]:
        templates = _social_feed_templates()
        if not templates:
            return []
        jobs = []
        for symbol in [item for item in symbols if not item.endswith("-USD")][:20]:
            for source, template in templates:
                jobs.append((symbol, source, template.format(symbol=quote(symbol), raw_symbol=symbol)))
        if not jobs:
            return []
        items: list[dict[str, str]] = []
        with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as pool:
            futures = {
                pool.submit(self._fetch_social_feed, symbol, source, url): (symbol, source)
                for symbol, source, url in jobs
            }
            for future in as_completed(futures):
                try:
                    items.extend(future.result())
                except (httpx.HTTPError, ET.ParseError, ValueError):
                    continue
        return items[:120]

    def _fetch_social_feed(self, symbol: str, source: str, url: str) -> list[dict[str, str]]:
        response = self.http_client.get(url, headers={"User-Agent": SOCIAL_USER_AGENT})
        response.raise_for_status()
        return [
            {
                "source": source,
                "symbol": item.symbol,
                "text": item.title,
                "url": item.url,
                "published": item.published,
            }
            for item in parse_yahoo_rss(symbol, response.text)[:8]
        ]

    def fetch_recent_form4_filings(self) -> list[dict[str, str]]:
        try:
            response = self.http_client.get(
                "https://www.sec.gov/cgi-bin/browse-edgar",
                params={"action": "getcurrent", "type": "4", "owner": "include", "count": "20", "output": "atom"},
                headers={"User-Agent": SEC_USER_AGENT},
            )
            response.raise_for_status()
        except httpx.HTTPError:
            return []
        return parse_form4_feed(response.text)


def load_watchlist(path: Path) -> Watchlist:
    if not path.exists():
        return Watchlist(symbols=DEFAULT_SYMBOLS.copy(), positions=[], risk={})
    raw = json.loads(path.read_text())
    symbols = [str(symbol).upper() for symbol in raw.get("symbols", DEFAULT_SYMBOLS)]
    aliases = {
        str(symbol).upper(): [str(alias).upper() for alias in values]
        for symbol, values in raw.get("aliases", {}).items()
        if isinstance(values, list)
    }
    positions = [
        Position(
            symbol=str(position.get("symbol", "")).upper(),
            quantity=float(position.get("quantity", 0)),
            cost_basis=float(position.get("cost_basis", 0)),
        )
        for position in raw.get("positions", [])
        if position.get("symbol")
    ]
    risk = {str(key): float(value) for key, value in raw.get("risk", {}).items()}
    return Watchlist(symbols=symbols, positions=positions, risk=risk, aliases=aliases)


def parse_yahoo_rss(symbol: str, xml_text: str) -> list[NewsItem]:
    root = ET.fromstring(xml_text)
    items: list[NewsItem] = []
    for item in root.findall(".//item"):
        title = _child_text(item, "title")
        url = _child_text(item, "link")
        published = _child_text(item, "pubDate")
        if title:
            items.append(NewsItem(symbol=symbol.upper(), title=title, url=url, published=published))
    return items


def parse_form4_feed(xml_text: str) -> list[dict[str, str]]:
    root = ET.fromstring(xml_text)
    filings: list[dict[str, str]] = []
    namespace = "{http://www.w3.org/2005/Atom}"
    for entry in root.findall(f".//{namespace}entry"):
        title = _child_text(entry, f"{namespace}title")
        updated = _child_text(entry, f"{namespace}updated")
        link = entry.find(f"{namespace}link")
        href = link.attrib.get("href", "") if link is not None else ""
        if title:
            filings.append({"title": title, "updated": updated, "url": href})
    return filings


def compute_risk_flags(data: RiskInput) -> list[str]:
    flags: list[str] = []
    total_value = 0.0
    values: dict[str, float] = {}
    for position in data.positions:
        quote_obj = data.quotes.get(position.symbol)
        price = quote_obj.price if quote_obj else position.cost_basis
        value = position.quantity * price
        values[position.symbol] = value
        total_value += value
        daily_loss_alert = data.risk.get("daily_loss_alert_pct")
        if daily_loss_alert and quote_obj and quote_obj.change_pct <= -abs(daily_loss_alert):
            flags.append(f"{position.symbol} is down {quote_obj.change_pct:.2f}% today.")
    if total_value <= 0:
        return ["No priced stock positions configured for risk assessment."]
    max_single = data.risk.get("max_single_position_pct", 25)
    for symbol, value in values.items():
        pct = (value / total_value) * 100
        if pct > max_single:
            flags.append(f"{symbol} concentration is {pct:.1f}% of configured portfolio.")
    return flags


def _child_text(element: ET.Element, name: str) -> str:
    child = element.find(name)
    if child is None or child.text is None:
        return ""
    return child.text.strip()


def _social_feed_templates() -> list[tuple[str, str]]:
    templates: list[tuple[str, str]] = []
    if _truthy(os.getenv("BONEHAWK_REDDIT_RSS", "")):
        templates.append(("reddit", "https://www.reddit.com/search.rss?q={symbol}%20stock&sort=new"))
    x_template = os.getenv("BONEHAWK_X_RSS_TEMPLATE", "").strip()
    if x_template:
        templates.append(("x", x_template))
    extra = os.getenv("BONEHAWK_SOCIAL_RSS_TEMPLATES", "").strip()
    if extra:
        try:
            parsed = json.loads(extra)
        except json.JSONDecodeError:
            parsed = []
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict) and item.get("source") and item.get("url"):
                    templates.append((str(item["source"]).lower(), str(item["url"])))
    return templates


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
