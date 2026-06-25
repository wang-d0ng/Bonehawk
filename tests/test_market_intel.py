from __future__ import annotations

import json
from pathlib import Path

import httpx

from scripts.market_intel import (
    MarketIntelClient,
    NewsItem,
    Position,
    RiskInput,
    Watchlist,
    compute_risk_flags,
    load_watchlist,
    parse_form4_feed,
    parse_yahoo_rss,
)
from scripts.quotes import Quote


def test_load_watchlist_defaults_when_missing(tmp_path: Path) -> None:
    watchlist = load_watchlist(tmp_path / "missing.json")

    assert "BTC-USD" in watchlist.symbols
    assert watchlist.positions == []


def test_load_watchlist_parses_symbols_positions_and_risk(tmp_path: Path) -> None:
    path = tmp_path / "watchlist.json"
    path.write_text(
        json.dumps(
            {
                "symbols": ["aapl", "BTC-USD"],
                "aliases": {"aapl": ["Apple Inc"]},
                "positions": [{"symbol": "aapl", "quantity": 2, "cost_basis": 100}],
                "risk": {"max_single_position_pct": 20},
            }
        )
    )

    watchlist = load_watchlist(path)

    assert watchlist.symbols == ["AAPL", "BTC-USD"]
    assert watchlist.aliases == {"AAPL": ["APPLE INC"]}
    assert watchlist.positions[0].symbol == "AAPL"
    assert watchlist.risk["max_single_position_pct"] == 20


def test_parse_yahoo_rss_extracts_news_items() -> None:
    xml = """
    <rss><channel>
      <item>
        <title>Apple shares rise</title>
        <link>https://example.com/aapl</link>
        <pubDate>Wed, 24 Jun 2026 10:00:00 GMT</pubDate>
      </item>
    </channel></rss>
    """

    items = parse_yahoo_rss("AAPL", xml)

    assert items == [
        NewsItem(symbol="AAPL", title="Apple shares rise", url="https://example.com/aapl", published="Wed, 24 Jun 2026 10:00:00 GMT")
    ]


def test_parse_form4_feed_extracts_entries() -> None:
    xml = """
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>4 - APPLE INC (0000320193) (Issuer)</title>
        <updated>2026-06-24T11:00:00Z</updated>
        <link href="https://www.sec.gov/Archives/test"/>
      </entry>
    </feed>
    """

    filings = parse_form4_feed(xml)

    assert filings[0]["title"].startswith("4 - APPLE")
    assert filings[0]["url"] == "https://www.sec.gov/Archives/test"


def test_compute_risk_flags_identifies_concentration_and_missing_data() -> None:
    flags = compute_risk_flags(
        RiskInput(
            positions=[Position("AAPL", quantity=10, cost_basis=100), Position("MSFT", quantity=1, cost_basis=100)],
            quotes={"AAPL": Quote("AAPL", 120, previous_close=130), "MSFT": Quote("MSFT", 100, previous_close=101)},
            risk={"max_single_position_pct": 50, "daily_loss_alert_pct": 3},
        )
    )

    assert any("AAPL concentration" in flag for flag in flags)
    assert any("AAPL is down" in flag for flag in flags)


def test_market_intel_client_fetches_news_and_insiders_with_mock_transport(tmp_path: Path) -> None:
    watchlist = Watchlist(symbols=["AAPL"], positions=[], risk={})

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "news.google.com" in url:
            return httpx.Response(200, text="<rss><channel><item><title>AAPL news</title><link>https://example.com</link></item></channel></rss>")
        if "browse-edgar" in url:
            return httpx.Response(
                200,
                text=(
                    '<feed xmlns="http://www.w3.org/2005/Atom">'
                    '<entry><title>4 - APPLE INC</title><updated>2026-06-24T00:00:00Z</updated>'
                    '<link href="https://sec.gov/form4"/></entry></feed>'
                ),
            )
        return httpx.Response(404)

    class FakeQuoteClient:
        def get_quotes(self, symbols):
            return {"AAPL": Quote("AAPL", 120, previous_close=118)}

    client = MarketIntelClient(http_client=httpx.Client(transport=httpx.MockTransport(handler)), quote_client=FakeQuoteClient())

    snapshot = client.snapshot(watchlist)

    assert snapshot["symbols"] == ["AAPL"]
    assert snapshot["quotes"]["AAPL"]["price"] == 120
    assert snapshot["news"][0]["title"] == "AAPL news"
    assert snapshot["insider_filings"][0]["title"] == "4 - APPLE INC"


def test_market_intel_client_stops_news_fetching_after_limit() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        items = "".join(f"<item><title>News {index}</title><link>https://example.com/{index}</link></item>" for index in range(8))
        return httpx.Response(200, text=f"<rss><channel>{items}</channel></rss>")

    client = MarketIntelClient(http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    news = client.fetch_news([f"SYM{index}" for index in range(20)])

    assert len(news) == 80
    assert len(calls) == 10
