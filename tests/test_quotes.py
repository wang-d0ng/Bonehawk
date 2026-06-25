from __future__ import annotations

import httpx

from scripts.market_intel import Position
from scripts.quotes import YahooQuoteClient, compute_alpaca_portfolio_performance, compute_portfolio_performance, parse_history_chart, parse_stock_chart, parse_yahoo_chart


def test_parse_yahoo_chart_extracts_quote() -> None:
    payload = {
        "chart": {
            "result": [
                {
                    "meta": {"symbol": "AAPL", "regularMarketPrice": 210.5, "chartPreviousClose": 200.0},
                    "indicators": {"quote": [{"close": [200.0, 210.5]}]},
                }
            ]
        }
    }

    quote = parse_yahoo_chart("AAPL", payload)

    assert quote.symbol == "AAPL"
    assert quote.price == 210.5
    assert quote.previous_close == 200.0
    assert quote.change_pct == 5.25


def test_yahoo_quote_client_fetches_quotes_with_mock_transport() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "chart": {
                    "result": [
                        {
                            "meta": {"symbol": "MSFT", "regularMarketPrice": 500, "chartPreviousClose": 490},
                            "indicators": {"quote": [{"close": [490, 500]}]},
                        }
                    ]
                }
            },
        )

    client = YahooQuoteClient(http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    quotes = client.get_quotes(["MSFT"])

    assert quotes["MSFT"].price == 500


def test_yahoo_quote_client_fetches_price_history_with_mock_transport() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "chart": {
                    "result": [
                        {
                            "meta": {"symbol": "MSFT"},
                            "timestamp": [1, 2, 3],
                            "indicators": {"quote": [{"close": [10, 11, 12], "volume": [100, 110, 220]}]},
                        }
                    ]
                }
            },
        )

    client = YahooQuoteClient(http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    histories = client.get_histories(["MSFT"])

    assert histories["MSFT"].closes == [10, 11, 12]
    assert histories["MSFT"].volumes == [100, 110, 220]


def test_yahoo_quote_client_fetches_stock_chart_with_mock_transport() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["range"] == "5d"
        assert request.url.params["interval"] == "15m"
        return httpx.Response(
            200,
            json={
                "chart": {
                    "result": [
                        {
                            "meta": {"symbol": "MSFT", "regularMarketPrice": 503},
                            "timestamp": [1000, 1060, 1120],
                            "indicators": {"quote": [{"close": [500, None, 503], "volume": [1000, 1200, 1300]}]},
                        }
                    ]
                }
            },
        )

    client = YahooQuoteClient(http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    chart = client.get_stock_chart("MSFT", "1w")

    assert chart.symbol == "MSFT"
    assert chart.range_key == "1w"
    assert chart.latest_price == 503
    assert [point.close for point in chart.points] == [500, 503]


def test_parse_stock_chart_rejects_empty_series() -> None:
    payload = {"chart": {"result": [{"meta": {"symbol": "AAPL"}, "timestamp": [], "indicators": {"quote": [{"close": []}]}}]}}

    try:
        parse_stock_chart("AAPL", "1d", payload)
    except ValueError as error:
        assert "No chart points" in str(error)
    else:
        raise AssertionError("Expected empty chart payload to fail")


def test_parse_history_chart_filters_empty_values_and_computes_indicators() -> None:
    history = parse_history_chart(
        "AAPL",
        {
            "chart": {
                "result": [
                    {
                        "meta": {"symbol": "AAPL"},
                        "indicators": {
                            "quote": [
                                {
                                    "close": [10, 11, None, 13, 14, 15, 16, 17, 18, 19, 20, 24, 25, 26, 27, 28],
                                    "volume": [100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 130, 140, 150, 160, 300],
                                }
                            ]
                        },
                    }
                ]
            }
        },
    )
    snapshot = history.technicals()

    assert history.symbol == "AAPL"
    assert snapshot["sma_5"] == 26
    assert snapshot["sma_10"] > snapshot["sma_20"] == 0
    assert snapshot["volume_ratio"] > 2
    assert 0 <= snapshot["rsi_14"] <= 100


def test_compute_portfolio_performance_uses_live_quotes() -> None:
    positions = [Position("AAPL", quantity=2, cost_basis=100), Position("MSFT", quantity=1, cost_basis=250)]
    quotes = {
        "AAPL": parse_yahoo_chart(
            "AAPL",
            {"chart": {"result": [{"meta": {"symbol": "AAPL", "regularMarketPrice": 120, "chartPreviousClose": 118}}]}},
        ),
        "MSFT": parse_yahoo_chart(
            "MSFT",
            {"chart": {"result": [{"meta": {"symbol": "MSFT", "regularMarketPrice": 240, "chartPreviousClose": 245}}]}},
        ),
    }

    performance = compute_portfolio_performance(positions, quotes)

    assert performance["total_cost"] == 450
    assert performance["total_value"] == 480
    assert performance["unrealized_pnl"] == 30
    assert performance["positions"][0]["unrealized_pnl"] == 40
    assert performance["positions"][1]["day_change_pct"] < 0


def test_compute_alpaca_portfolio_performance_uses_broker_values() -> None:
    account = {"portfolio_value": "1250.50", "cash": "900.25", "buying_power": "1800.00"}
    positions = [
        {
            "symbol": "AAPL",
            "qty": "2",
            "avg_entry_price": "100",
            "cost_basis": "200",
            "current_price": "120",
            "market_value": "240",
            "unrealized_pl": "40",
            "unrealized_plpc": "0.20",
            "change_today": "0.0125",
        }
    ]

    performance = compute_alpaca_portfolio_performance(account, positions)

    assert performance["source"] == "alpaca"
    assert performance["account_value"] == 1250.50
    assert performance["cash"] == 900.25
    assert performance["positions"][0]["cost_basis"] == 100
    assert performance["positions"][0]["cost_value"] == 200
    assert performance["positions"][0]["unrealized_pnl_pct"] == 20
    assert performance["positions"][0]["day_change_pct"] == 1.25
