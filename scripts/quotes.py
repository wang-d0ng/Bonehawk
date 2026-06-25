from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import quote

import httpx


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: float
    previous_close: float | None = None

    @property
    def change_pct(self) -> float:
        if not self.previous_close:
            return 0.0
        return ((self.price - self.previous_close) / self.previous_close) * 100


@dataclass(frozen=True)
class PriceHistory:
    symbol: str
    closes: list[float]
    volumes: list[float]

    def technicals(self) -> dict[str, float]:
        return {
            "sma_5": round(_sma(self.closes, 5), 4),
            "sma_10": round(_sma(self.closes, 10), 4),
            "sma_20": round(_sma(self.closes, 20), 4),
            "rsi_14": round(_rsi(self.closes, 14), 2),
            "volume_ratio": round(_volume_ratio(self.volumes, 10), 2),
        }


@dataclass(frozen=True)
class ChartPoint:
    timestamp: int
    close: float
    volume: float | None = None


@dataclass(frozen=True)
class StockChart:
    symbol: str
    range_key: str
    interval: str
    latest_price: float
    points: list[ChartPoint]

    @property
    def change_pct(self) -> float:
        if len(self.points) < 2:
            return 0.0
        first = self.points[0].close
        if first <= 0:
            return 0.0
        return ((self.points[-1].close - first) / first) * 100


CHART_RANGES: dict[str, tuple[str, str]] = {
    "1d": ("1d", "5m"),
    "1w": ("5d", "15m"),
    "1m": ("1mo", "1d"),
    "3m": ("3mo", "1d"),
    "1y": ("1y", "1wk"),
}


class YahooQuoteClient:
    def __init__(self, http_client: httpx.Client | None = None) -> None:
        self.http_client = http_client or httpx.Client(timeout=10, follow_redirects=True)

    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        quotes: dict[str, Quote] = {}
        for symbol in symbols:
            try:
                response = self.http_client.get(
                    f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol)}",
                    params={"range": "2d", "interval": "1d"},
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                response.raise_for_status()
                quotes[symbol.upper()] = parse_yahoo_chart(symbol, response.json())
            except (httpx.HTTPError, KeyError, TypeError, ValueError, IndexError):
                continue
        return quotes

    def get_histories(self, symbols: list[str], days: int = 90) -> dict[str, PriceHistory]:
        histories: dict[str, PriceHistory] = {}
        for symbol in symbols:
            try:
                response = self.http_client.get(
                    f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol)}",
                    params={"range": f"{days}d", "interval": "1d"},
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                response.raise_for_status()
                histories[symbol.upper()] = parse_history_chart(symbol, response.json())
            except (httpx.HTTPError, KeyError, TypeError, ValueError, IndexError):
                continue
        return histories

    def get_stock_chart(self, symbol: str, range_key: str = "1d") -> StockChart:
        normalized_range = range_key if range_key in CHART_RANGES else "1d"
        yahoo_range, interval = CHART_RANGES[normalized_range]
        response = self.http_client.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol)}",
            params={"range": yahoo_range, "interval": interval},
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()
        return parse_stock_chart(symbol, normalized_range, response.json(), interval=interval)


def parse_yahoo_chart(symbol: str, payload: dict[str, Any]) -> Quote:
    result = payload["chart"]["result"][0]
    meta = result.get("meta", {})
    price = float(meta.get("regularMarketPrice") or _last_close(result))
    previous_close = meta.get("chartPreviousClose") or meta.get("previousClose")
    if previous_close is None:
        closes = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
        if len(closes) >= 2:
            previous_close = closes[-2]
    return Quote(symbol=str(meta.get("symbol") or symbol).upper(), price=price, previous_close=float(previous_close) if previous_close else None)


def parse_history_chart(symbol: str, payload: dict[str, Any]) -> PriceHistory:
    result = payload["chart"]["result"][0]
    meta = result.get("meta", {})
    quote_data = result.get("indicators", {}).get("quote", [{}])[0]
    closes = [float(value) for value in quote_data.get("close", []) if value is not None]
    volumes = [float(value) for value in quote_data.get("volume", []) if value is not None]
    if not closes:
        raise ValueError("No close prices in chart payload")
    return PriceHistory(symbol=str(meta.get("symbol") or symbol).upper(), closes=closes, volumes=volumes)


def parse_stock_chart(symbol: str, range_key: str, payload: dict[str, Any], interval: str | None = None) -> StockChart:
    result = payload["chart"]["result"][0]
    meta = result.get("meta", {})
    quote_data = result.get("indicators", {}).get("quote", [{}])[0]
    timestamps = result.get("timestamp", [])
    closes = quote_data.get("close", [])
    volumes = quote_data.get("volume", [])
    points = [
        ChartPoint(timestamp=int(timestamp), close=float(close), volume=float(volumes[index]) if index < len(volumes) and volumes[index] is not None else None)
        for index, (timestamp, close) in enumerate(zip(timestamps, closes))
        if timestamp is not None and close is not None
    ]
    if not points:
        raise ValueError("No chart points in payload")
    latest_price = float(meta.get("regularMarketPrice") or points[-1].close)
    return StockChart(
        symbol=str(meta.get("symbol") or symbol).upper(),
        range_key=range_key,
        interval=interval or "",
        latest_price=latest_price,
        points=points,
    )


def compute_portfolio_performance(positions: list[Any], quotes: dict[str, Quote]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    total_cost = 0.0
    total_value = 0.0
    for position in positions:
        quote_obj = quotes.get(position.symbol)
        current_price = quote_obj.price if quote_obj else position.cost_basis
        cost_value = position.quantity * position.cost_basis
        market_value = position.quantity * current_price
        pnl = market_value - cost_value
        total_cost += cost_value
        total_value += market_value
        rows.append(
            {
                **asdict(position),
                "current_price": round(current_price, 4),
                "market_value": round(market_value, 2),
                "cost_value": round(cost_value, 2),
                "unrealized_pnl": round(pnl, 2),
                "unrealized_pnl_pct": round((pnl / cost_value) * 100, 2) if cost_value else 0,
                "day_change_pct": round(quote_obj.change_pct, 2) if quote_obj else 0,
            }
        )
    unrealized = total_value - total_cost
    return {
        "positions": rows,
        "total_cost": round(total_cost, 2),
        "total_value": round(total_value, 2),
        "unrealized_pnl": round(unrealized, 2),
        "unrealized_pnl_pct": round((unrealized / total_cost) * 100, 2) if total_cost else 0,
    }


def compute_alpaca_portfolio_performance(account: dict[str, Any], positions: list[dict[str, Any]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    total_cost = 0.0
    total_value = 0.0
    unrealized = 0.0
    for position in positions:
        symbol = str(position.get("symbol") or "").upper()
        if not symbol:
            continue
        quantity = _safe_float(position.get("qty"))
        average_entry = _safe_float(position.get("avg_entry_price"))
        cost_value = _safe_float(position.get("cost_basis"), quantity * average_entry)
        current_price = _safe_float(position.get("current_price"), average_entry)
        market_value = _safe_float(position.get("market_value"), quantity * current_price)
        pnl = _safe_float(position.get("unrealized_pl"), market_value - cost_value)
        pnl_pct = _safe_float(position.get("unrealized_plpc")) * 100
        day_change_pct = _safe_float(position.get("change_today")) * 100
        total_cost += cost_value
        total_value += market_value
        unrealized += pnl
        rows.append(
            {
                "symbol": symbol,
                "quantity": quantity,
                "cost_basis": round(average_entry, 4),
                "current_price": round(current_price, 4),
                "market_value": round(market_value, 2),
                "cost_value": round(cost_value, 2),
                "unrealized_pnl": round(pnl, 2),
                "unrealized_pnl_pct": round(pnl_pct, 2),
                "day_change_pct": round(day_change_pct, 2),
                "asset_class": position.get("asset_class"),
                "side": position.get("side"),
                "source": "alpaca",
            }
        )
    account_value = _safe_float(account.get("portfolio_value"), total_value)
    return {
        "source": "alpaca",
        "positions": rows,
        "total_cost": round(total_cost, 2),
        "total_value": round(total_value, 2),
        "account_value": round(account_value, 2),
        "cash": round(_safe_float(account.get("cash")), 2),
        "buying_power": round(_safe_float(account.get("buying_power")), 2),
        "unrealized_pnl": round(unrealized, 2),
        "unrealized_pnl_pct": round((unrealized / total_cost) * 100, 2) if total_cost else 0,
    }


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _last_close(result: dict[str, Any]) -> float:
    closes = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
    values = [value for value in closes if value is not None]
    if not values:
        raise ValueError("No close price in chart payload")
    return float(values[-1])


def _sma(values: list[float], period: int) -> float:
    if len(values) < period:
        return 0
    window = values[-period:]
    return sum(window) / period


def _rsi(closes: list[float], period: int) -> float:
    if len(closes) <= period:
        return 50
    deltas = [closes[index] - closes[index - 1] for index in range(1, len(closes))]
    recent = deltas[-period:]
    gains = [delta for delta in recent if delta > 0]
    losses = [-delta for delta in recent if delta < 0]
    average_gain = sum(gains) / period
    average_loss = sum(losses) / period
    if average_loss == 0:
        return 100
    relative_strength = average_gain / average_loss
    return 100 - (100 / (1 + relative_strength))


def _volume_ratio(volumes: list[float], period: int) -> float:
    if len(volumes) <= period:
        return 1
    baseline = volumes[-period - 1 : -1]
    average_volume = sum(baseline) / len(baseline) if baseline else 0
    if average_volume <= 0:
        return 1
    return volumes[-1] / average_volume
