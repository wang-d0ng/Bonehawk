from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from scripts.agentic_autotrader import AgenticScanConfig, load_xgboost_model, run_agentic_scan, run_pending_loss_postmortems
from scripts.alpaca_connector import LIVE_CONFIRM_PHRASE, AlpacaOrderRequest, AlpacaTradingClient
from scripts.decision_log import record_decisions
from scripts.growth_scanner import build_growth_candidates
from scripts.market_intel import MarketIntelClient, Watchlist
from scripts.market_scanner import scan_market
from scripts.market_universe import combine_symbols, load_market_universe
from scripts.quotes import YahooQuoteClient
from scripts.trade_ideas import build_market_trend, build_trade_ideas


@dataclass(frozen=True)
class AutopilotConfig:
    enabled: bool = False
    mode: str = "paper"
    broker: str = "alpaca"
    allow_live: bool = False
    max_trade_usd: float = 25
    max_daily_loss_usd: float = 20
    max_open_positions: int = 3
    min_confidence: int = 55
    symbols_per_run: int = 40
    scan_window_minutes: int = 15
    max_kelly_fraction: float = 0.05
    min_probability: float = 0.56
    paper_trade_downtrend: bool = True
    strategies: tuple[str, ...] = ("trend_following", "momentum_breakout", "risk_exit")

    def snapshot(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["strategies"] = list(self.strategies)
        payload["live_ready"] = self.mode == "live" and self.allow_live and self.broker == "alpaca"
        return payload


class AutopilotEngine:
    def __init__(
        self,
        root: Path,
        config: AutopilotConfig,
        intel_client: MarketIntelClient | None = None,
        quote_client: YahooQuoteClient | None = None,
        alpaca_client: AlpacaTradingClient | None = None,
    ) -> None:
        self.root = root
        self.config = config
        self.intel_client = intel_client or MarketIntelClient()
        self.quote_client = quote_client or getattr(self.intel_client, "quote_client", YahooQuoteClient())
        self.alpaca_client = alpaca_client

    def snapshot(self) -> dict[str, Any]:
        broker = self.alpaca_client.snapshot() if self.alpaca_client else {"status": "not_configured", "message": "Alpaca client not loaded."}
        return {
            "ok": True,
            "status": "enabled" if self.config.enabled else "disabled",
            "config": self.config.snapshot(),
            "broker": broker,
            "notice": "Autopilot is paper-first. Live mode requires separate Alpaca live keys and explicit confirmation gates.",
        }

    def scan(self, watchlist: Watchlist) -> dict[str, Any]:
        scan_payload = self._build_scan(watchlist)
        decisions = self._build_orders(scan_payload, watchlist)
        record_decisions(self.root / "logs" / "decision_log.jsonl", "autopilot_scan", decisions["orders"])
        return {
            "ok": True,
            "status": "scanned",
            "mode": self.config.mode,
            "config": self.config.snapshot(),
            **scan_payload,
            **decisions,
            "notice": "Autopilot scan is review-only until you run paper execution.",
        }

    def execute(self, watchlist: Watchlist, confirm: str = "") -> dict[str, Any]:
        if not self.config.enabled:
            return {
                "ok": False,
                "status": "disabled",
                "config": self.config.snapshot(),
                "orders": [],
                "executed": [],
                "blocked": [{"status": "disabled", "reason": "Autopilot is disabled in config/autopilot.json."}],
            }
        if self.config.mode == "live":
            if not self.config.allow_live:
                return {
                    "ok": False,
                    "status": "live_not_allowed",
                    "config": self.config.snapshot(),
                    "orders": [],
                    "executed": [],
                    "blocked": [{"status": "live_not_allowed", "reason": "Autopilot live permission is locked."}],
                }
            if confirm != LIVE_CONFIRM_PHRASE:
                return {
                    "ok": False,
                    "status": "confirmation_required",
                    "config": self.config.snapshot(),
                    "orders": [],
                    "executed": [],
                    "blocked": [{"status": "confirmation_required", "reason": f"Live autopilot execution requires {LIVE_CONFIRM_PHRASE}."}],
                }
        scan_payload = self._build_scan(watchlist)
        decisions = self._build_orders(scan_payload, watchlist)
        executed: list[dict[str, Any]] = []
        if self.config.broker != "alpaca":
            decisions["blocked"].append({"status": "broker_disabled", "reason": "Only Alpaca paper execution is implemented."})
        elif not self.alpaca_client:
            decisions["blocked"].append({"status": "broker_missing", "reason": "Alpaca client is not available."})
        else:
            for order in decisions["orders"]:
                bracket_prices = _broker_bracket_prices(order, self.config)
                request = AlpacaOrderRequest(
                    symbol=order["symbol"],
                    side=order["side"],
                    notional=order["notional"],
                    order_type="market",
                    time_in_force="day",
                    stop_loss=bracket_prices.get("stop_loss"),
                    take_profit=bracket_prices.get("take_profit"),
                )
                response = self.alpaca_client.place_order(request, confirm=confirm)
                executed.append(
                    {
                        **response,
                        "symbol": response.get("symbol") or order.get("symbol"),
                        "side": response.get("side") or order.get("side"),
                        "notional": response.get("notional") or order.get("notional"),
                    }
                )
        decision_rows = [_decision_from_execution(item) for item in executed] if executed else decisions["orders"]
        record_decisions(self.root / "logs" / "decision_log.jsonl", "autopilot_order", decision_rows)
        submitted = [item for item in executed if item.get("ok")]
        rejected = [item for item in executed if not item.get("ok")]
        status = _execution_status(executed)
        return {
            "ok": bool(executed) and not rejected,
            "status": status,
            "mode": self.config.mode,
            "config": self.config.snapshot(),
            **scan_payload,
            **decisions,
            "executed": executed,
            "execution_summary": {
                "submitted": len(submitted),
                "rejected": len(rejected),
                "planned": len(decisions["orders"]),
                "blocked": len(decisions["blocked"]),
                "message": _execution_message(status, submitted, rejected, decisions["orders"]),
            },
            "notice": "Paper execution submitted to Alpaca when configured. Check tickets for order IDs and statuses.",
        }

    def _build_scan(self, watchlist: Watchlist) -> dict[str, Any]:
        symbols = _autopilot_symbols(self.root, watchlist, self.config.symbols_per_run)
        active_watchlist = replace(watchlist, symbols=symbols)
        snapshot = self.intel_client.snapshot(active_watchlist)
        scan_result = scan_market(active_watchlist, snapshot)
        quote_symbols = list(dict.fromkeys([*symbols[: self.config.symbols_per_run], "SPY", "QQQ"]))
        quotes = self.quote_client.get_quotes(quote_symbols)
        histories = self.quote_client.get_histories(quote_symbols)
        technicals = {symbol: history.technicals() for symbol, history in histories.items()}
        market_trend = build_market_trend(technicals)
        ideas = build_trade_ideas(scan_result, quotes, active_watchlist.positions, active_watchlist.risk, technicals=technicals, market_trend=market_trend, max_ideas=12)
        growth = build_growth_candidates(scan_result, quotes, technicals, market_trend=market_trend, max_candidates=12)
        bankroll_usd = self._bankroll_usd()
        agentic_scan = run_agentic_scan(
            scan_result=scan_result,
            quotes=quotes,
            technicals=technicals,
            snapshot={**snapshot, "market_trend": market_trend},
            config=AgenticScanConfig(
                bankroll_usd=bankroll_usd,
                max_trade_usd=self.config.max_trade_usd,
                max_kelly_fraction=self.config.max_kelly_fraction,
                window_minutes=self.config.scan_window_minutes,
                min_probability=self.config.min_probability,
                allow_downtrend=self.config.mode == "paper" and self.config.paper_trade_downtrend,
            ),
            xgboost_model=load_xgboost_model(self.root / "models" / "xgboost_short_window.json"),
        )
        postmortems = run_pending_loss_postmortems(self.root)
        if postmortems:
            agentic_scan = {**agentic_scan, "postmortems": postmortems}
        return {
            "summary": scan_result["summary"],
            "market_trend": market_trend,
            "ideas": ideas,
            "growth_candidates": growth,
            "agentic_scan": agentic_scan,
        }

    def _build_orders(self, scan_payload: dict[str, Any], watchlist: Watchlist) -> dict[str, list[dict[str, Any]]]:
        held_symbols = {position.symbol for position in watchlist.positions}
        orders: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        open_slots = max(0, self.config.max_open_positions - len(held_symbols))

        ranked = _rank_candidate_orders(
            scan_payload.get("ideas", []),
            scan_payload.get("growth_candidates", []),
            scan_payload.get("agentic_scan", {}),
            self.config,
        )
        for candidate in ranked:
            if len(orders) >= open_slots:
                blocked.append({**candidate, "status": "blocked", "reason": "Max open position limit reached."})
                continue
            if candidate["confidence"] < self.config.min_confidence:
                blocked.append({**candidate, "status": "blocked", "reason": "Confidence is below autopilot minimum."})
                continue
            if candidate["symbol"] in held_symbols:
                blocked.append({**candidate, "status": "blocked", "reason": "Already in configured positions."})
                continue
            order = {
                **candidate,
                "side": "buy",
                "notional": round(_order_notional(candidate, self.config), 2),
                "status": "planned",
                "source": "autopilot",
                "review_only": True,
            }
            orders.append(order)
        return {"orders": orders, "blocked": blocked}

    def _bankroll_usd(self) -> float:
        fallback = max(self.config.max_trade_usd, self.config.max_trade_usd * max(1, self.config.max_open_positions))
        if not self.alpaca_client or not hasattr(self.alpaca_client, "get_account"):
            return fallback
        try:
            account = self.alpaca_client.get_account()
        except Exception:
            return fallback
        cash = _safe_float(account.get("cash"))
        buying_power = _safe_float(account.get("buying_power"))
        portfolio_value = _safe_float(account.get("portfolio_value"))
        candidates = [value for value in (cash, buying_power, portfolio_value) if value > 0]
        return min(candidates) if candidates else fallback


def load_autopilot_config(path: Path) -> AutopilotConfig:
    if not path.exists():
        return AutopilotConfig()
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError:
        return AutopilotConfig()
    if not isinstance(raw, dict):
        return AutopilotConfig()
    strategies = raw.get("strategies", AutopilotConfig().strategies)
    if not isinstance(strategies, (list, tuple)):
        strategies = AutopilotConfig().strategies
    return AutopilotConfig(
        enabled=bool(raw.get("enabled", False)),
        mode=_choice(raw.get("mode"), {"paper", "live"}, "paper"),
        broker=_choice(raw.get("broker"), {"alpaca"}, "alpaca"),
        allow_live=bool(raw.get("allow_live", False)),
        max_trade_usd=_clamp_float(raw.get("max_trade_usd"), 1, 1000, 25),
        max_daily_loss_usd=_clamp_float(raw.get("max_daily_loss_usd"), 1, 5000, 20),
        max_open_positions=int(_clamp_float(raw.get("max_open_positions"), 0, 25, 3)),
        min_confidence=int(_clamp_float(raw.get("min_confidence"), 35, 95, 55)),
        symbols_per_run=int(_clamp_float(raw.get("symbols_per_run"), 1, 250, 40)),
        scan_window_minutes=int(_clamp_float(raw.get("scan_window_minutes"), 1, 30, 15)),
        max_kelly_fraction=_clamp_float(raw.get("max_kelly_fraction"), 0, 0.25, 0.05),
        min_probability=_clamp_float(raw.get("min_probability"), 0.5, 0.95, 0.56),
        paper_trade_downtrend=bool(raw.get("paper_trade_downtrend", True)),
        strategies=tuple(str(item) for item in strategies if str(item).strip()),
    )


def save_autopilot_config(path: Path, config: AutopilotConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = config.snapshot()
    payload.pop("live_ready", None)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def update_autopilot_config(config: AutopilotConfig, setting: Any, value: Any, confirm: str = "") -> tuple[AutopilotConfig, dict[str, Any]]:
    key = str(setting or "").strip()
    if key == "enabled":
        return replace(config, enabled=_truthy(value)), {"ok": True, "status": "updated", "message": "Autopilot enabled setting updated."}
    if key == "mode":
        mode = _choice(value, {"paper", "live"}, "")
        if not mode:
            return config, {"ok": False, "status": "invalid_value", "message": "Autopilot mode must be paper or live."}
        if mode == "live" and confirm != "LIVE_ALPACA_AUTOPILOT":
            return config, {"ok": False, "status": "confirmation_required", "message": "Live autopilot requires confirmation.", "confirm_phrase": "LIVE_ALPACA_AUTOPILOT"}
        return replace(config, mode=mode), {"ok": True, "status": "updated", "message": f"Autopilot mode set to {mode}."}
    if key == "allow_live":
        allow_live = _truthy(value)
        if allow_live and confirm != "ALLOW_LIVE_ALPACA":
            return config, {"ok": False, "status": "confirmation_required", "message": "Live Alpaca permission requires confirmation.", "confirm_phrase": "ALLOW_LIVE_ALPACA"}
        return replace(config, allow_live=allow_live), {"ok": True, "status": "updated", "message": "Autopilot live permission updated."}
    if key == "max_trade_usd":
        return replace(config, max_trade_usd=_clamp_float(value, 1, 1000, config.max_trade_usd)), {"ok": True, "status": "updated", "message": "Max trade size updated."}
    if key == "max_open_positions":
        return replace(config, max_open_positions=int(_clamp_float(value, 0, 25, config.max_open_positions))), {"ok": True, "status": "updated", "message": "Max open positions updated."}
    if key == "min_confidence":
        return replace(config, min_confidence=int(_clamp_float(value, 35, 95, config.min_confidence))), {"ok": True, "status": "updated", "message": "Minimum confidence updated."}
    if key == "scan_window_minutes":
        return replace(config, scan_window_minutes=int(_clamp_float(value, 1, 30, config.scan_window_minutes))), {"ok": True, "status": "updated", "message": "Short-window scan horizon updated."}
    if key == "max_kelly_fraction":
        return replace(config, max_kelly_fraction=_clamp_float(value, 0, 0.25, config.max_kelly_fraction)), {"ok": True, "status": "updated", "message": "Kelly cap updated."}
    if key == "min_probability":
        return replace(config, min_probability=_clamp_float(value, 0.5, 0.95, config.min_probability)), {"ok": True, "status": "updated", "message": "Prediction threshold updated."}
    if key == "paper_trade_downtrend":
        return replace(config, paper_trade_downtrend=_truthy(value)), {"ok": True, "status": "updated", "message": "Paper downtrend exploration updated."}
    return config, {"ok": False, "status": "invalid_setting", "message": "Unknown autopilot setting."}


def _autopilot_symbols(root: Path, watchlist: Watchlist, limit: int) -> list[str]:
    universe_path = root / "config" / "market_universe.json"
    if not universe_path.exists():
        universe_path = root / "config" / "market_universe.example.json"
    universe = load_market_universe(universe_path)
    return combine_symbols(watchlist.symbols, universe, limit=max(1, limit))


def _rank_candidate_orders(ideas: list[dict[str, Any]], growth: list[dict[str, Any]], agentic_scan: dict[str, Any], config: AutopilotConfig) -> list[dict[str, Any]]:
    by_symbol: dict[str, dict[str, Any]] = {}
    for opportunity in agentic_scan.get("opportunities", []):
        symbol = str(opportunity.get("symbol") or "").upper()
        if not symbol:
            continue
        by_symbol[symbol] = {
            "symbol": symbol,
            "action": "AUTO_BUY_CANDIDATE",
            "confidence": int(opportunity.get("confidence") or 0),
            "current_price": opportunity.get("current_price"),
            "stop_loss": opportunity.get("stop_loss"),
            "take_profit": opportunity.get("take_profit"),
            "suggested_notional": opportunity.get("suggested_notional"),
            "probability_up": opportunity.get("probability_up"),
            "edge_pct": opportunity.get("edge_pct"),
            "kelly_fraction": opportunity.get("kelly_fraction"),
            "reason": opportunity.get("reason"),
            "signals": list(opportunity.get("signals") or []),
        }
    for idea in ideas:
        if idea.get("action") != "BUY_REVIEW":
            continue
        symbol = str(idea.get("symbol") or "").upper()
        if not symbol:
            continue
        existing = by_symbol.get(symbol)
        if existing and int(existing.get("confidence") or 0) >= int(idea.get("confidence") or 0):
            existing["signals"] = list(dict.fromkeys([*(existing.get("signals") or []), *(idea.get("signals") or [])]))
            continue
        by_symbol[symbol] = {
            **(existing or {}),
            "symbol": symbol,
            "action": "BUY_REVIEW",
            "confidence": int(idea.get("confidence") or 0),
            "current_price": idea.get("current_price"),
            "stop_loss": idea.get("stop_loss") or (existing or {}).get("stop_loss"),
            "take_profit": idea.get("take_profit") or (existing or {}).get("take_profit"),
            "reason": idea.get("reason"),
            "signals": list(dict.fromkeys([*((existing or {}).get("signals") or []), *(idea.get("signals") or [])])),
        }
    for candidate in growth:
        if candidate.get("action") not in {"WATCH_FAST_GROWTH", "WATCH"}:
            continue
        symbol = str(candidate.get("symbol") or "").upper()
        if not symbol:
            continue
        existing = by_symbol.get(symbol, {"confidence": 0, "signals": []})
        score = max(int(existing.get("confidence") or 0), int(candidate.get("momentum_score") or 0))
        by_symbol[symbol] = {
            **existing,
            "symbol": symbol,
            "action": "BUY_REVIEW",
            "confidence": score,
            "current_price": existing.get("current_price") or candidate.get("current_price"),
            "reason": existing.get("reason") or candidate.get("reason"),
            "signals": list(dict.fromkeys([*(existing.get("signals") or []), *(candidate.get("signals") or [])])),
        }
    ranked = sorted(by_symbol.values(), key=lambda item: int(item.get("confidence") or 0), reverse=True)
    return ranked[: max(1, config.max_open_positions + 4)]


def _order_notional(candidate: dict[str, Any], config: AutopilotConfig) -> float:
    suggested = _safe_float(candidate.get("suggested_notional"), config.max_trade_usd)
    return max(1, min(config.max_trade_usd, suggested))


def _broker_bracket_prices(order: dict[str, Any], config: AutopilotConfig) -> dict[str, float | None]:
    if config.mode == "paper":
        return {"stop_loss": None, "take_profit": None}
    return {"stop_loss": order.get("stop_loss"), "take_profit": order.get("take_profit")}


def _execution_status(executed: list[dict[str, Any]]) -> str:
    if not executed:
        return "no_orders_submitted"
    success_count = sum(1 for item in executed if item.get("ok"))
    if success_count == len(executed):
        return "executed"
    if success_count:
        return "partially_executed"
    return "orders_rejected"


def _execution_message(status: str, submitted: list[dict[str, Any]], rejected: list[dict[str, Any]], orders: list[dict[str, Any]]) -> str:
    if submitted and not rejected:
        return f"Submitted {len(submitted)} Alpaca paper order(s)."
    if submitted and rejected:
        return f"Submitted {len(submitted)} order(s); {len(rejected)} rejected."
    if rejected:
        return f"Alpaca rejected {len(rejected)} planned order(s)."
    if orders:
        return "Orders were planned but not sent."
    return "No paper orders met the agent rules."


def _decision_from_execution(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": item.get("symbol"),
        "action": f"{item.get('side', 'ORDER')}_ALPACA",
        "confidence": None,
        "current_price": None,
        "quantity": item.get("quantity") or item.get("notional"),
        "status": item.get("status"),
        "broker_order_id": item.get("broker_order_id"),
        "reason": item.get("message"),
        "signals": [f"broker_status {item.get('broker_status') or item.get('status')}", f"notional {item.get('notional') or 'n/a'}"],
        "review_only": item.get("review_only", True),
    }


def _choice(value: Any, choices: set[str], default: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in choices else default


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _clamp_float(value: Any, minimum: float, maximum: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default
