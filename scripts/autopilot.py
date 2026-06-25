from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from scripts.agentic_autotrader import AgenticScanConfig, load_xgboost_model, run_agentic_scan, run_pending_loss_postmortems
from scripts.alpaca_connector import LIVE_CONFIRM_PHRASE, AlpacaOrderRequest, AlpacaTradingClient
from scripts.decision_log import latest_decisions, record_decisions
from scripts.growth_scanner import build_growth_candidates
from scripts.market_intel import MarketIntelClient, Position, Watchlist
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
    scan_window_minutes: int = 5
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
        decisions = _apply_order_cooldown(self.root, decisions, self.config)
        executed: list[dict[str, Any]] = []
        if self.config.broker != "alpaca":
            decisions["blocked"].append({"status": "broker_disabled", "reason": "Only Alpaca paper execution is implemented."})
        elif not self.alpaca_client:
            decisions["blocked"].append({"status": "broker_missing", "reason": "Alpaca client is not available."})
        else:
            for order in decisions["orders"]:
                bracket_prices = _broker_bracket_prices(order, self.config)
                side = str(order.get("side") or "buy").lower()
                request = AlpacaOrderRequest(
                    symbol=order["symbol"],
                    side=side,
                    quantity=_safe_float(order.get("quantity"), 0) if side == "sell" else None,
                    notional=None if side == "sell" else order["notional"],
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
                        "quantity": response.get("quantity") or order.get("quantity"),
                        "notional": response.get("notional") or order.get("notional"),
                        "current_price": order.get("current_price"),
                        "action": order.get("action"),
                        "reason": order.get("reason"),
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
        open_positions = self._open_positions(watchlist)
        position_symbols = [position["symbol"] for position in open_positions]
        symbols = _autopilot_symbols(self.root, watchlist, self.config.symbols_per_run)
        active_watchlist = replace(watchlist, symbols=symbols, positions=_watchlist_positions(open_positions))
        account_state = self._account_state()
        snapshot = self.intel_client.snapshot(active_watchlist)
        scan_result = scan_market(active_watchlist, snapshot)
        quote_symbols = list(dict.fromkeys([*symbols[: self.config.symbols_per_run], *position_symbols, "SPY", "QQQ"]))
        quotes = self.quote_client.get_quotes(quote_symbols)
        histories = self.quote_client.get_histories(quote_symbols)
        technicals = {symbol: history.technicals() for symbol, history in histories.items()}
        market_trend = build_market_trend(technicals)
        ideas = build_trade_ideas(scan_result, quotes, active_watchlist.positions, active_watchlist.risk, technicals=technicals, market_trend=market_trend, max_ideas=12)
        growth = build_growth_candidates(scan_result, quotes, technicals, market_trend=market_trend, max_candidates=12)
        agentic_scan = run_agentic_scan(
            scan_result=scan_result,
            quotes=quotes,
            technicals=technicals,
            snapshot={**snapshot, "market_trend": market_trend},
            config=AgenticScanConfig(
                bankroll_usd=account_state["available_cash"],
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
        exit_candidates = _build_exit_candidates(open_positions, quotes, technicals, agentic_scan, self.config, market_trend)
        return {
            "summary": scan_result["summary"],
            "market_trend": market_trend,
            "ideas": ideas,
            "growth_candidates": growth,
            "agentic_scan": agentic_scan,
            "account_state": account_state,
            "open_positions": open_positions,
            "exit_candidates": exit_candidates,
        }

    def _build_orders(self, scan_payload: dict[str, Any], watchlist: Watchlist) -> dict[str, list[dict[str, Any]]]:
        held_symbols = {position.symbol for position in watchlist.positions}
        held_symbols.update(str(position.get("symbol") or "").upper() for position in scan_payload.get("open_positions", []))
        held_symbols.discard("")
        orders: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        open_slots = max(0, self.config.max_open_positions - len(held_symbols))
        remaining_cash = _safe_float((scan_payload.get("account_state") or {}).get("available_cash"), 0)

        for exit_candidate in scan_payload.get("exit_candidates", []):
            if exit_candidate.get("status") == "planned":
                orders.append(exit_candidate)
            else:
                blocked.append(exit_candidate)
        buy_order_count = 0

        ranked = _rank_candidate_orders(
            scan_payload.get("ideas", []),
            scan_payload.get("growth_candidates", []),
            scan_payload.get("agentic_scan", {}),
            self.config,
        )
        for candidate in ranked:
            if buy_order_count >= open_slots:
                blocked.append({**candidate, "status": "blocked", "reason": "Max open position limit reached."})
                continue
            if candidate["confidence"] < self.config.min_confidence:
                blocked.append({**candidate, "status": "blocked", "reason": "Confidence is below autopilot minimum."})
                continue
            if candidate["symbol"] in held_symbols:
                blocked.append({**candidate, "status": "blocked", "reason": "Already in configured positions."})
                continue
            notional = _order_notional(candidate, remaining_cash)
            if notional <= 0:
                blocked.append({**candidate, "status": "blocked", "reason": "Dynamic sizing found no available cash after price, probability, and risk checks."})
                continue
            order = {
                **candidate,
                "side": "buy",
                "notional": round(notional, 2),
                "quantity_estimate": round(notional / _safe_float(candidate.get("current_price"), 1), 6) if _safe_float(candidate.get("current_price"), 0) > 0 else 0,
                "sizing_method": candidate.get("sizing_method") or "dynamic_account_probability",
                "status": "planned",
                "source": "autopilot",
                "review_only": True,
            }
            orders.append(order)
            buy_order_count += 1
            remaining_cash = max(0, remaining_cash - notional)
        return {"orders": orders, "blocked": blocked}

    def _open_positions(self, watchlist: Watchlist) -> list[dict[str, Any]]:
        if self.alpaca_client and hasattr(self.alpaca_client, "get_positions"):
            try:
                positions = self.alpaca_client.get_positions()
            except Exception:
                positions = None
            if positions is not None:
                return [position for position in (_normalize_alpaca_position(item) for item in positions) if position]
        return [_normalize_watchlist_position(position) for position in watchlist.positions if position.quantity > 0]

    def _account_state(self) -> dict[str, float | str]:
        fallback = max(1, self.config.max_trade_usd * max(1, self.config.max_open_positions))
        if not self.alpaca_client or not hasattr(self.alpaca_client, "get_account"):
            return {
                "source": "config_fallback",
                "cash": fallback,
                "buying_power": fallback,
                "portfolio_value": fallback,
                "available_cash": fallback,
            }
        try:
            account = self.alpaca_client.get_account()
        except Exception:
            return {
                "source": "config_fallback",
                "cash": fallback,
                "buying_power": fallback,
                "portfolio_value": fallback,
                "available_cash": fallback,
            }
        cash = _safe_float(account.get("cash"))
        buying_power = _safe_float(account.get("buying_power"))
        portfolio_value = _safe_float(account.get("portfolio_value"))
        if cash > 0 and buying_power > 0:
            available_cash = min(cash, buying_power)
        else:
            available_cash = max(cash, buying_power, portfolio_value, fallback)
        return {
            "source": "alpaca",
            "cash": cash,
            "buying_power": buying_power,
            "portfolio_value": portfolio_value,
            "available_cash": available_cash,
        }


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
        scan_window_minutes=int(_clamp_float(raw.get("scan_window_minutes"), 1, 5, 5)),
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
        return replace(config, scan_window_minutes=int(_clamp_float(value, 1, 5, config.scan_window_minutes))), {"ok": True, "status": "updated", "message": "Short-window scan horizon updated."}
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


def _apply_order_cooldown(root: Path, decisions: dict[str, list[dict[str, Any]]], config: AutopilotConfig) -> dict[str, list[dict[str, Any]]]:
    cooldown_minutes = int(_clamp_float(config.scan_window_minutes, 1, 5, 5))
    recent = _recent_order_fingerprints(root / "logs" / "decision_log.jsonl", cooldown_minutes)
    if not recent:
        return decisions
    orders: list[dict[str, Any]] = []
    blocked = list(decisions.get("blocked", []))
    for order in decisions.get("orders", []):
        fingerprint = _order_fingerprint(order)
        if fingerprint in recent:
            blocked.append(
                {
                    **order,
                    "status": "cooldown",
                    "reason": f"Recent {order.get('side', 'order')} for {order.get('symbol')} was already submitted inside the {cooldown_minutes} minute window.",
                    "cooldown_minutes": cooldown_minutes,
                }
            )
            continue
        orders.append(order)
    return {"orders": orders, "blocked": blocked}


def _recent_order_fingerprints(path: Path, cooldown_minutes: int) -> set[tuple[str, str, str]]:
    now = datetime.now(UTC)
    cutoff = now - timedelta(minutes=cooldown_minutes)
    fingerprints: set[tuple[str, str, str]] = set()
    for row in latest_decisions(path, limit=200):
        if row.get("source") != "autopilot_order":
            continue
        if not _was_recent_order_attempt(row):
            continue
        timestamp = _parse_timestamp(row.get("timestamp"))
        if timestamp is None or timestamp < cutoff:
            continue
        fingerprint = _order_fingerprint(row)
        if all(fingerprint):
            fingerprints.add(fingerprint)
    return fingerprints


def _was_recent_order_attempt(row: dict[str, Any]) -> bool:
    symbol, side, action = _order_fingerprint(row)
    return bool(symbol and side and action)


def _parse_timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _order_fingerprint(row: dict[str, Any]) -> tuple[str, str, str]:
    symbol = str(row.get("symbol") or "").upper()
    side = str(row.get("side") or "").lower()
    action = str(row.get("action") or "").upper()
    if not side:
        action_text = action.lower()
        if "sell" in action_text:
            side = "sell"
        elif "buy" in action_text:
            side = "buy"
    return symbol, side, action


def _watchlist_positions(rows: list[dict[str, Any]]) -> list[Position]:
    return [
        Position(
            symbol=str(row.get("symbol") or "").upper(),
            quantity=_safe_float(row.get("quantity")),
            cost_basis=_safe_float(row.get("cost_basis")),
        )
        for row in rows
        if str(row.get("symbol") or "").strip() and _safe_float(row.get("quantity")) > 0
    ]


def _normalize_alpaca_position(row: dict[str, Any]) -> dict[str, Any] | None:
    symbol = str(row.get("symbol") or "").upper()
    quantity = abs(_safe_float(row.get("qty")))
    if not symbol or quantity < 0.000001:
        return None
    cost_basis = _safe_float(row.get("avg_entry_price"))
    current_price = _safe_float(row.get("current_price"), cost_basis)
    cost_value = _safe_float(row.get("cost_basis"), quantity * cost_basis)
    market_value = _safe_float(row.get("market_value"), quantity * current_price)
    unrealized_pnl = _safe_float(row.get("unrealized_pl"), market_value - cost_value)
    raw_pnl_pct = _safe_float(row.get("unrealized_plpc"))
    unrealized_pnl_pct = raw_pnl_pct * 100 if raw_pnl_pct else ((unrealized_pnl / cost_value) * 100 if cost_value else 0)
    return {
        "symbol": symbol,
        "quantity": quantity,
        "cost_basis": cost_basis,
        "current_price": current_price,
        "market_value": market_value,
        "cost_value": cost_value,
        "unrealized_pnl": unrealized_pnl,
        "unrealized_pnl_pct": unrealized_pnl_pct,
        "day_change_pct": _safe_float(row.get("change_today")) * 100,
        "asset_class": row.get("asset_class") or "us_equity",
        "side": str(row.get("side") or "long").lower(),
        "source": "alpaca",
    }


def _normalize_watchlist_position(position: Position) -> dict[str, Any]:
    quantity = max(0, _safe_float(position.quantity))
    cost_basis = _safe_float(position.cost_basis)
    return {
        "symbol": position.symbol.upper(),
        "quantity": quantity,
        "cost_basis": cost_basis,
        "current_price": cost_basis,
        "market_value": quantity * cost_basis,
        "cost_value": quantity * cost_basis,
        "unrealized_pnl": 0,
        "unrealized_pnl_pct": 0,
        "day_change_pct": 0,
        "asset_class": "configured_position",
        "side": "long",
        "source": "watchlist",
    }


def _build_exit_candidates(
    open_positions: list[dict[str, Any]],
    quotes: dict[str, Any],
    technicals: dict[str, dict[str, float]],
    agentic_scan: dict[str, Any],
    config: AutopilotConfig,
    market_trend: str,
) -> list[dict[str, Any]]:
    probabilities = _agentic_probability_lookup(agentic_scan)
    candidates: list[dict[str, Any]] = []
    for position in open_positions:
        symbol = str(position.get("symbol") or "").upper()
        quantity = _safe_float(position.get("quantity"))
        if not symbol or quantity < 0.000001:
            continue
        quote = quotes.get(symbol)
        current_price = quote.price if quote else _safe_float(position.get("current_price"), _safe_float(position.get("cost_basis")))
        cost_basis = _safe_float(position.get("cost_basis"))
        cost_value = quantity * cost_basis
        market_value = quantity * current_price
        unrealized_pnl = market_value - cost_value
        unrealized_pnl_pct = (unrealized_pnl / cost_value) * 100 if cost_value else _safe_float(position.get("unrealized_pnl_pct"))
        symbol_technicals = technicals.get(symbol, {})
        probability_up = probabilities.get(symbol, _exit_probability(quote, symbol_technicals, market_trend))
        profit_target_pct = _profit_target_pct(probability_up, symbol_technicals, config, market_trend)
        stop_exit_pct = _stop_exit_pct(probability_up, profit_target_pct)
        signals = [
            f"profit {unrealized_pnl_pct:.2f}%",
            f"target {profit_target_pct:.2f}%",
            f"prob {probability_up * 100:.1f}%",
            f"window {config.scan_window_minutes}m",
            f"market {market_trend.lower()}",
        ]
        if symbol_technicals:
            signals.extend(
                [
                    f"rsi {symbol_technicals.get('rsi_14', 50):.1f}",
                    f"volume {symbol_technicals.get('volume_ratio', 1):.2f}x",
                ]
            )
        if unrealized_pnl_pct >= profit_target_pct:
            candidates.append(
                _exit_order(
                    symbol=symbol,
                    quantity=quantity,
                    current_price=current_price,
                    notional=market_value,
                    action="AUTO_SELL_PROFIT_TAKE",
                    confidence=_exit_confidence(unrealized_pnl_pct, profit_target_pct, probability_up),
                    probability_up=probability_up,
                    unrealized_pnl=unrealized_pnl,
                    unrealized_pnl_pct=unrealized_pnl_pct,
                    profit_target_pct=profit_target_pct,
                    stop_exit_pct=stop_exit_pct,
                    reason="Open profit cleared the dynamic 1-5 minute target; closing the paper position to lock gains.",
                    signals=signals,
                    config=config,
                )
            )
            continue
        if unrealized_pnl_pct <= -abs(stop_exit_pct):
            candidates.append(
                _exit_order(
                    symbol=symbol,
                    quantity=quantity,
                    current_price=current_price,
                    notional=market_value,
                    action="AUTO_SELL_RISK_EXIT",
                    confidence=92,
                    probability_up=probability_up,
                    unrealized_pnl=unrealized_pnl,
                    unrealized_pnl_pct=unrealized_pnl_pct,
                    profit_target_pct=profit_target_pct,
                    stop_exit_pct=stop_exit_pct,
                    reason="Loss crossed the dynamic short-window stop; closing the paper position before risk expands.",
                    signals=signals,
                    config=config,
                )
            )
            continue
        candidates.append(
            {
                "symbol": symbol,
                "side": "sell",
                "action": "HOLD_POSITION",
                "confidence": _exit_confidence(unrealized_pnl_pct, profit_target_pct, probability_up),
                "current_price": round(current_price, 4),
                "quantity": round(quantity, 6),
                "quantity_estimate": round(quantity, 6),
                "notional": round(market_value, 2),
                "probability_up": round(probability_up, 4),
                "unrealized_pnl": round(unrealized_pnl, 2),
                "unrealized_pnl_pct": round(unrealized_pnl_pct, 2),
                "profit_target_pct": round(profit_target_pct, 2),
                "stop_exit_pct": round(stop_exit_pct, 2),
                "exit_window_minutes": config.scan_window_minutes,
                "status": "blocked",
                "reason": "Open position has not reached the dynamic profit target or risk-exit level.",
                "signals": signals,
                "source": "autopilot_exit",
                "review_only": True,
            }
        )
    return candidates


def _exit_order(
    *,
    symbol: str,
    quantity: float,
    current_price: float,
    notional: float,
    action: str,
    confidence: int,
    probability_up: float,
    unrealized_pnl: float,
    unrealized_pnl_pct: float,
    profit_target_pct: float,
    stop_exit_pct: float,
    reason: str,
    signals: list[str],
    config: AutopilotConfig,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "side": "sell",
        "action": action,
        "confidence": confidence,
        "current_price": round(current_price, 4),
        "quantity": round(quantity, 6),
        "quantity_estimate": round(quantity, 6),
        "notional": round(notional, 2),
        "probability_up": round(probability_up, 4),
        "edge_pct": round(max(0, unrealized_pnl_pct - profit_target_pct), 4),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "unrealized_pnl_pct": round(unrealized_pnl_pct, 2),
        "profit_target_pct": round(profit_target_pct, 2),
        "stop_exit_pct": round(stop_exit_pct, 2),
        "exit_window_minutes": config.scan_window_minutes,
        "status": "planned",
        "source": "autopilot_exit",
        "sizing_method": "close_position_quantity",
        "reason": reason,
        "signals": signals,
        "review_only": True,
    }


def _agentic_probability_lookup(agentic_scan: dict[str, Any]) -> dict[str, float]:
    rows = [*agentic_scan.get("opportunities", []), *agentic_scan.get("blocked", [])]
    lookup: dict[str, float] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        probability = _safe_float(row.get("probability_up"), 0)
        if symbol and probability > 0:
            lookup[symbol] = probability
    return lookup


def _exit_probability(quote: Any, technicals: dict[str, float], market_trend: str) -> float:
    probability = 0.5
    if quote:
        probability += _clamp_float(quote.change_pct, -5, 5, 0) / 100
    sma_5 = _safe_float(technicals.get("sma_5"))
    sma_20 = _safe_float(technicals.get("sma_20"))
    if sma_5 and sma_20:
        probability += 0.04 if sma_5 >= sma_20 else -0.04
    rsi = _safe_float(technicals.get("rsi_14"), 50)
    if rsi >= 72:
        probability -= 0.08
    elif 52 <= rsi <= 66:
        probability += 0.03
    elif rsi <= 40:
        probability -= 0.04
    if _safe_float(technicals.get("volume_ratio"), 1) >= 1.5:
        probability += 0.02
    if market_trend == "UP":
        probability += 0.02
    elif market_trend == "DOWN":
        probability -= 0.04
    return _clamp_float(probability, 0.05, 0.95, 0.5)


def _profit_target_pct(probability_up: float, technicals: dict[str, float], config: AutopilotConfig, market_trend: str) -> float:
    window = int(_clamp_float(config.scan_window_minutes, 1, 5, 5))
    target = 0.35 + ((window - 1) * 0.1)
    if probability_up >= 0.68:
        target += 0.45
    elif probability_up >= 0.6:
        target += 0.25
    elif probability_up <= 0.52:
        target -= 0.1
    if market_trend == "DOWN":
        target -= 0.1
    rsi = _safe_float(technicals.get("rsi_14"), 50)
    if rsi >= 72:
        target -= 0.15
    if _safe_float(technicals.get("volume_ratio"), 1) >= 2 and _safe_float(technicals.get("sma_5")) >= _safe_float(technicals.get("sma_20")):
        target += 0.1
    return round(_clamp_float(target, 0.25, 2.5, 0.65), 2)


def _stop_exit_pct(probability_up: float, profit_target_pct: float) -> float:
    stop = profit_target_pct * (1.1 if probability_up >= 0.6 else 0.85)
    if probability_up < 0.5:
        stop *= 0.8
    return round(_clamp_float(stop, 0.3, 3, 0.75), 2)


def _exit_confidence(unrealized_pnl_pct: float, profit_target_pct: float, probability_up: float) -> int:
    capture_bonus = max(0, unrealized_pnl_pct - profit_target_pct) * 5
    fade_bonus = max(0, 0.6 - probability_up) * 80
    return int(_clamp_float(62 + capture_bonus + fade_bonus, 35, 95, 62))


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
            "available_cash": opportunity.get("available_cash"),
            "risk_budget": opportunity.get("risk_budget"),
            "adaptive_cap_fraction": opportunity.get("adaptive_cap_fraction"),
            "quantity_estimate": opportunity.get("quantity_estimate"),
            "sizing_method": opportunity.get("sizing_method"),
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


def _order_notional(candidate: dict[str, Any], remaining_cash: float) -> float:
    suggested = _safe_float(candidate.get("suggested_notional"), 0)
    if suggested <= 0:
        confidence = _safe_float(candidate.get("confidence"), 0)
        price = _safe_float(candidate.get("current_price"), 0)
        if confidence <= 0 or price <= 0:
            return 0
        strength = _clamp_float((confidence - 40) / 60, 0, 1, 0)
        price_to_cash = price / max(price, remaining_cash)
        price_penalty = _clamp_float(1 - price_to_cash * 0.5, 0.25, 1, 0.5)
        suggested = remaining_cash * (0.03 + strength * 0.12) * price_penalty
        if confidence >= 50 and remaining_cash >= 1:
            suggested = max(1, suggested)
    if suggested <= 0 or remaining_cash <= 0:
        return 0
    notional = min(suggested, remaining_cash)
    return notional if notional >= 1 else 0


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
        "side": item.get("side"),
        "action": item.get("action") or f"{item.get('side', 'ORDER')}_ALPACA",
        "confidence": None,
        "current_price": None,
        "quantity": item.get("quantity") or item.get("notional"),
        "status": item.get("status"),
        "broker_status": item.get("broker_status"),
        "broker_order_id": item.get("broker_order_id"),
        "quantity": item.get("quantity"),
        "filled_quantity": item.get("filled_quantity"),
        "filled_average_price": item.get("filled_average_price"),
        "fill_status": item.get("fill_status"),
        "reason": item.get("message"),
        "signals": [
            f"broker_status {item.get('broker_status') or item.get('status')}",
            f"fill_status {item.get('fill_status') or 'unknown'}",
            f"notional {item.get('notional') or 'n/a'}",
        ],
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
