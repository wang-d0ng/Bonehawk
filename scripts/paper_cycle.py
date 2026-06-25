#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from math import floor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.robinhood import RobinhoodConfig, RobinhoodCryptoClient, RobinhoodError

DEFAULT_MEMORY = ROOT / "memory"
SYMBOL = "BTC-USD"


@dataclass(frozen=True)
class AccountSnapshot:
    buying_power_usd: float
    status: str
    currency: str


@dataclass(frozen=True)
class PositionSnapshot:
    quantity_btc: float


@dataclass(frozen=True)
class QuoteSnapshot:
    symbol: str
    bid: float
    ask: float

    @property
    def mid_price(self) -> float:
        return (self.bid + self.ask) / 2


@dataclass(frozen=True)
class PaperDecision:
    timestamp: datetime
    action: str
    symbol: str
    price: float
    reason: str
    paper_order: dict[str, Any] | None


def extract_account_snapshot(payload: dict[str, Any]) -> AccountSnapshot:
    account = _first_result(payload)
    return AccountSnapshot(
        buying_power_usd=float(account.get("buying_power", 0)),
        status=str(account.get("status", "unknown")),
        currency=str(account.get("buying_power_currency", "USD")),
    )


def extract_btc_position(payload: dict[str, Any]) -> PositionSnapshot:
    for holding in payload.get("results", []):
        if holding.get("asset_code") == "BTC":
            quantity = holding.get("quantity_available_for_trading") or holding.get("total_quantity") or 0
            return PositionSnapshot(quantity_btc=float(quantity))
    return PositionSnapshot(quantity_btc=0.0)


def extract_quote(payload: dict[str, Any]) -> QuoteSnapshot:
    quote = _first_result(payload)
    return QuoteSnapshot(
        symbol=str(quote.get("symbol", SYMBOL)),
        bid=float(quote.get("bid", 0)),
        ask=float(quote.get("ask", 0)),
    )


def latest_research_report(report_dir: Path) -> dict[str, Any] | None:
    reports = sorted(report_dir.glob("*.json"))
    if not reports:
        return None
    return json.loads(reports[-1].read_text())


def build_decision(
    account: AccountSnapshot,
    position: PositionSnapshot,
    quote: QuoteSnapshot,
    research: dict[str, Any] | None,
    project_context: str,
    now: datetime | None = None,
) -> PaperDecision:
    timestamp = now or datetime.now(timezone.utc)
    price = quote.mid_price

    if "DRAWDOWN_HALT=true" in project_context:
        return PaperDecision(timestamp, "HOLD", quote.symbol, price, "Drawdown halt is active.", None)

    if account.status.lower() != "active":
        return PaperDecision(timestamp, "HOLD", quote.symbol, price, f"Account status is {account.status}.", None)

    if position.quantity_btc > 0:
        return PaperDecision(
            timestamp,
            "MANAGE_REVIEW",
            quote.symbol,
            price,
            f"Existing BTC position detected: {position.quantity_btc:.8f} BTC. Review management ladder.",
            None,
        )

    idea = _best_trade_idea(research)
    if idea is None:
        return PaperDecision(timestamp, "HOLD", quote.symbol, price, "No fresh A/B-grade research trade idea.", None)

    entry = float(idea.get("entry") or quote.ask)
    stop = float(idea.get("stop") or 0)
    target = float(idea.get("target") or 0)
    if entry <= 0 or stop <= 0 or target <= 0 or stop >= entry:
        return PaperDecision(timestamp, "HOLD", quote.symbol, price, "Research idea is missing valid entry, stop, or target.", None)

    risk_per_coin = entry - stop
    reward_per_coin = target - entry
    if reward_per_coin < risk_per_coin * 2:
        return PaperDecision(timestamp, "HOLD", quote.symbol, price, "Research idea does not meet 2R target requirement.", None)

    grade = str(idea.get("grade", "")).upper()
    risk_pct = 0.01 if grade == "A" else 0.005
    equity = account.buying_power_usd
    risk_usd = equity * risk_pct
    size_usd = floor((risk_usd / (risk_per_coin / entry)) / 10) * 10
    size_usd = max(0, min(size_usd, floor(account.buying_power_usd / 10) * 10))
    if size_usd <= 0:
        return PaperDecision(timestamp, "HOLD", quote.symbol, price, "Buying power is too small for risk-sized paper entry.", None)

    paper_order = {
        "side": "buy",
        "size_usd": size_usd,
        "entry": entry,
        "stop": stop,
        "target": target,
        "risk_pct": risk_pct,
        "playbook_setup": idea.get("playbook_setup", "unknown"),
    }
    reason = f"{grade}-grade paper setup: {idea.get('thesis', 'No thesis provided.')}"
    return PaperDecision(timestamp, "PAPER_BUY_CANDIDATE", quote.symbol, price, reason, paper_order)


def format_decision(decision: PaperDecision) -> str:
    lines = [
        f"{decision.action} {decision.symbol}",
        f"Time: {decision.timestamp.isoformat()}",
        f"Paper price: ${decision.price:,.2f}",
        f"Reason: {decision.reason}",
    ]
    if decision.paper_order:
        order = decision.paper_order
        lines.extend(
            [
                f"Paper order: {str(order.get('side', 'unknown')).upper()} ${float(order.get('size_usd', 0)):,.2f}",
                f"Entry ${float(order.get('entry', decision.price)):,.2f} | Stop ${float(order.get('stop', 0)):,.2f} | Target ${float(order.get('target', 0)):,.2f}",
                f"Risk: {float(order.get('risk_pct', 0)) * 100:.2f}% | Setup: {order.get('playbook_setup', 'unknown')}",
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
        f"**Paper price:** ${decision.price:,.2f}",
        f"**Reason:** {decision.reason}",
    ]
    if decision.paper_order:
        block.append(f"**Paper order:** `{json.dumps(decision.paper_order, sort_keys=True)}`")
    block.append("")
    existing = trade_log.read_text() if trade_log.exists() else "# Trade Log\n"
    trade_log.write_text(existing.rstrip() + "\n" + "\n".join(block))


def run_paper_cycle(memory_dir: Path, notify: bool = False) -> PaperDecision:
    client = RobinhoodCryptoClient(RobinhoodConfig.from_env())
    account = extract_account_snapshot(client.account())
    position = extract_btc_position(client.holdings("BTC"))
    quote = extract_quote(client.quote(SYMBOL))
    research = latest_research_report(memory_dir / "research-reports")
    project_context_path = memory_dir / "PROJECT-CONTEXT.md"
    project_context = project_context_path.read_text() if project_context_path.exists() else ""

    decision = build_decision(account, position, quote, research, project_context)
    record_decision(memory_dir / "TRADE-LOG.md", decision)
    message = format_decision(decision)
    print(message)

    if notify:
        subprocess.run(["bash", str(ROOT / "scripts" / "telegram.sh"), message], check=False)

    return decision


def _best_trade_idea(research: dict[str, Any] | None) -> dict[str, Any] | None:
    if not research:
        return None
    ideas = research.get("trade_ideas") or research.get("tradeIdeas") or []
    valid = [idea for idea in ideas if str(idea.get("grade", "")).upper() in {"A", "B"}]
    if not valid:
        return None
    return sorted(valid, key=lambda idea: 0 if str(idea.get("grade", "")).upper() == "A" else 1)[0]


def _first_result(payload: dict[str, Any]) -> dict[str, Any]:
    results = payload.get("results") if isinstance(payload, dict) else None
    if isinstance(results, list) and results:
        return results[0]
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a safe paper trading cycle.")
    parser.add_argument("--memory-dir", default=str(DEFAULT_MEMORY))
    parser.add_argument("--notify", action="store_true", help="Send the paper decision to Telegram.")
    args = parser.parse_args()

    try:
        run_paper_cycle(Path(args.memory_dir), notify=args.notify)
    except RobinhoodError as error:
        print(f"ERROR: {error}")
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
