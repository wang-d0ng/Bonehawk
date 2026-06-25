#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.autopilot import load_autopilot_config
from scripts.market_intel import MarketIntelClient, Watchlist, load_watchlist
from scripts.market_scanner import scan_market
from scripts.market_universe import combine_symbols, load_market_universe
from scripts.quotes import YahooQuoteClient
from scripts.trade_ideas import build_market_trend, build_trade_ideas

DEFAULT_MEMORY = ROOT / "memory"


@dataclass(frozen=True)
class PaperDecision:
    timestamp: datetime
    action: str
    symbol: str
    price: float
    reason: str
    paper_order: dict[str, Any] | None


def build_decision(ideas: list[dict[str, Any]], max_trade_usd: float, now: datetime | None = None) -> PaperDecision:
    timestamp = now or datetime.now(timezone.utc)
    candidates = [idea for idea in ideas if str(idea.get("action") or "") == "BUY_REVIEW"]
    if not candidates:
        return PaperDecision(timestamp, "HOLD", "MARKET", 0, "No buy-rated stock setup passed the scanner rules.", None)

    best = sorted(candidates, key=lambda item: int(item.get("confidence") or 0), reverse=True)[0]
    symbol = str(best.get("symbol") or "UNKNOWN").upper()
    price = float(best.get("current_price") or 0)
    confidence = int(best.get("confidence") or 0)
    size_usd = max(1.0, min(float(max_trade_usd), 1000.0))
    paper_order = {
        "side": "buy",
        "size_usd": round(size_usd, 2),
        "confidence": confidence,
        "stop_loss": best.get("stop_loss"),
        "take_profit": best.get("take_profit"),
        "signals": list(best.get("signals") or []),
    }
    reason = f"Top paper setup scored {confidence}/100: {best.get('reason', 'No reason provided.')}"
    return PaperDecision(timestamp, "PAPER_BUY_CANDIDATE", symbol, price, reason, paper_order)


def format_decision(decision: PaperDecision) -> str:
    lines = [
        f"{decision.action} {decision.symbol}",
        f"Time: {decision.timestamp.isoformat()}",
        f"Paper price: {money(decision.price)}",
        f"Reason: {decision.reason}",
    ]
    if decision.paper_order:
        order = decision.paper_order
        lines.extend(
            [
                f"Paper order: {str(order.get('side', 'unknown')).upper()} ${float(order.get('size_usd', 0)):,.2f}",
                f"Confidence: {int(order.get('confidence') or 0)}/100",
                f"Stop: {money(order.get('stop_loss'))} | Target: {money(order.get('take_profit'))}",
            ]
        )
    lines.append("No live order was placed.")
    return "\n".join(lines)


def record_decision(trade_log: Path, decision: PaperDecision) -> None:
    trade_log.parent.mkdir(parents=True, exist_ok=True)
    block = [
        "",
        f"## {decision.timestamp.isoformat()} - Paper Cycle",
        "",
        f"**Action:** {decision.action}",
        f"**Symbol:** {decision.symbol}",
        f"**Paper price:** {money(decision.price)}",
        f"**Reason:** {decision.reason}",
    ]
    if decision.paper_order:
        block.append(f"**Paper order:** `{json.dumps(decision.paper_order, sort_keys=True)}`")
    block.append("")
    existing = trade_log.read_text() if trade_log.exists() else "# Trade Log\n"
    trade_log.write_text(existing.rstrip() + "\n" + "\n".join(block))


def run_paper_cycle(memory_dir: Path, notify: bool = False, root: Path = ROOT) -> PaperDecision:
    watchlist = _scanner_watchlist(root)
    intel_client = MarketIntelClient()
    quote_client = getattr(intel_client, "quote_client", YahooQuoteClient())
    snapshot = intel_client.snapshot(watchlist)
    scan_result = scan_market(watchlist, snapshot)
    symbols = _quote_symbols(scan_result, watchlist)
    quotes = quote_client.get_quotes(symbols)
    history_symbols = list(dict.fromkeys([*symbols, "SPY", "QQQ"]))
    histories = quote_client.get_histories(history_symbols)
    technicals = {symbol: history.technicals() for symbol, history in histories.items()}
    market_trend = build_market_trend(technicals)
    ideas = build_trade_ideas(scan_result, quotes, watchlist.positions, watchlist.risk, technicals=technicals, market_trend=market_trend)
    config = load_autopilot_config(root / "config" / "autopilot.json")
    decision = build_decision(ideas, max_trade_usd=config.max_trade_usd)
    record_decision(memory_dir / "TRADE-LOG.md", decision)
    message = format_decision(decision)
    print(message)

    if notify:
        subprocess.run(["bash", str(root / "scripts" / "telegram.sh"), message], check=False)

    return decision


def money(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0
    return f"${number:,.2f}"


def _scanner_watchlist(root: Path) -> Watchlist:
    watchlist_path = root / "config" / "watchlist.json"
    if not watchlist_path.exists():
        watchlist_path = root / "config" / "watchlist.example.json"
    watchlist = load_watchlist(watchlist_path)
    universe_path = root / "config" / "market_universe.json"
    if not universe_path.exists():
        universe_path = root / "config" / "market_universe.example.json"
    universe = load_market_universe(universe_path)
    symbols = combine_symbols(watchlist.symbols, universe, limit=len(watchlist.symbols) + len(universe))
    return Watchlist(symbols=symbols, positions=watchlist.positions, risk=watchlist.risk, aliases=watchlist.aliases)


def _quote_symbols(scan_result: dict[str, Any], watchlist: Watchlist, limit: int = 40) -> list[str]:
    symbols = [position.symbol for position in watchlist.positions if not position.symbol.endswith("-USD")]
    for scan in scan_result.get("scans", []):
        symbol = str(scan.get("symbol") or "").upper()
        if symbol and not symbol.endswith("-USD"):
            symbols.append(symbol)
        if len(dict.fromkeys(symbols)) >= limit:
            break
    return list(dict.fromkeys(symbols))[:limit]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a safe stock paper trading cycle.")
    parser.add_argument("--memory-dir", default=str(DEFAULT_MEMORY))
    parser.add_argument("--notify", action="store_true", help="Send the paper decision to Telegram.")
    args = parser.parse_args()
    run_paper_cycle(Path(args.memory_dir), notify=args.notify)


if __name__ == "__main__":
    main()
