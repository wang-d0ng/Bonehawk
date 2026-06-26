#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.alpaca_connector import LIVE_CONFIRM_PHRASE, AlpacaConfig, AlpacaOrderRequest, AlpacaTradingClient, alpaca_order_fill_snapshot
from scripts.autopilot import AutopilotConfig, AutopilotEngine, load_autopilot_config, save_autopilot_config, update_autopilot_config
from scripts.command_center import command_catalog, run_command
from scripts.decision_log import latest_decisions, record_decisions
from scripts.growth_scanner import build_growth_candidates, build_growth_candidates_message
from scripts.market_intel import MarketIntelClient, Position, Watchlist, load_watchlist
from scripts.market_scanner import build_alert_message, scan_market
from scripts.market_universe import combine_symbols, load_market_universe, market_universe_snapshot
from scripts.market_hours import evaluate_market_hours_gate, market_hours_state_after_gate
from scripts.postmortem_report import apply_loss_postmortem_report, build_loss_postmortem_report
from scripts.portfolio_sync import portfolio_sync_snapshot
from scripts.private_beta_check import build_private_beta_report
from scripts.quotes import CHART_RANGES, YahooQuoteClient, compute_alpaca_portfolio_performance
from scripts.readiness import (
    build_live_readiness_report,
    build_operational_health_report,
    build_paper_evidence_report,
    build_public_release_report,
    record_risk_acknowledgement,
    risk_acknowledgement_status,
)
from scripts.trade_ideas import build_market_trend, build_trade_ideas, build_trade_ideas_message
from scripts.trading_desk import (
    build_backtest,
    build_data_health,
    order_truth_snapshot,
    record_order_truth_event,
    record_trade_journal_entry,
    shadow_mode_snapshot,
    strategy_scorecard,
    trade_journal_snapshot,
)

UI_THEME_VALUES = {"retro", "clean", "arcade", "classic", "algo-desk"}
BACKGROUND_AUTOPILOT_INTERVAL_SECONDS = 10
MARKET_HOURS_STATE_FILE = Path("state") / "autopilot_market_hours.json"
EMERGENCY_LIQUIDATE_CONFIRM_PHRASE = "LIQUIDATE_ALL_POSITIONS"
SETUP_SECRET_KEYS = {
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
    "TELEGRAM_BOT_TOKEN",
    "ALLOWED_CHAT_IDS",
}


@dataclass(frozen=True)
class StockOrderTicket:
    symbol: str
    side: str
    quantity: float


class DashboardService:
    def __init__(
        self,
        root: Path = ROOT,
        intel_client: MarketIntelClient | None = None,
        quote_client: YahooQuoteClient | None = None,
        codex_config_path: Path | None = None,
        alpaca_client: Any | None = None,
    ) -> None:
        self.root = root
        self.intel_client = intel_client or MarketIntelClient()
        self.quote_client = quote_client or getattr(self.intel_client, "quote_client", YahooQuoteClient())
        self.codex_config_path = codex_config_path
        self.alpaca_client = alpaca_client
        self._background_lock = threading.RLock()
        self._background_cycle_lock = threading.Lock()
        self._background_stop = threading.Event()
        self._background_thread: threading.Thread | None = None
        self._background_runs = 0
        self._background_last_result: dict[str, Any] | None = None
        self._background_last_error = ""
        self._background_started_at = ""
        self._background_last_started_at = ""
        self._background_last_finished_at = ""
        self._ticket_refresh_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._last_order_reconciliation: dict[str, Any] = _empty_reconciliation_report()

    def status(self) -> dict[str, Any]:
        env = _read_env_presence(self.root / ".env")
        return {
            "mode": env.get("TRADING_MODE", "missing"),
            "ui_theme": _ui_theme_from_env(env),
            "env": env,
            "guardrails": [
                "Autopilot runs paper-first through Alpaca.",
                "Manual stock orders use Alpaca paper mode unless Alpaca live mode is explicitly enabled.",
                "Live Alpaca orders require ALPACA_ALLOW_LIVE=true and the LIVE_ALPACA_ORDER confirmation phrase.",
                "Dashboard order tickets always record the broker response and order id when Alpaca returns one.",
            ],
        }

    def set_trading_mode(self, mode: Any, confirm: str = "") -> dict[str, Any]:
        normalized = str(mode or "").strip().lower()
        current_mode = _read_env_presence(self.root / ".env").get("TRADING_MODE", "missing")
        if normalized not in {"paper", "live"}:
            return {"ok": False, "status": "invalid_mode", "mode": current_mode, "message": "Trading mode must be paper or live."}
        if normalized == "live" and confirm != "LIVE":
            return {
                "ok": False,
                "status": "confirmation_required",
                "mode": current_mode,
                "message": "Live trading mode requires confirmation.",
            }
        _write_env_value(self.root / ".env", "TRADING_MODE", normalized)
        return {
            "ok": True,
            "status": "updated",
            "mode": normalized,
            "message": f"Trading mode switched to {normalized}.",
        }

    def set_ui_theme(self, theme: Any) -> dict[str, Any]:
        normalized = str(theme or "").strip().lower()
        if normalized not in UI_THEME_VALUES:
            return {"ok": False, "status": "invalid_theme", "theme": _ui_theme_from_env(_read_env_presence(self.root / ".env")), "message": "UI style must be retro, clean, arcade, algo-desk, or classic."}
        _write_env_value(self.root / ".env", "BONEHAWK_UI_THEME", normalized)
        return {"ok": True, "status": "updated", "theme": normalized, "message": f"UI style switched to {normalized}."}

    def command_catalog(self) -> dict[str, Any]:
        return command_catalog()

    def setup_status(self) -> dict[str, Any]:
        env = _read_env_presence(self.root / ".env")
        autopilot_path = self.root / "config" / "autopilot.json"
        alpaca_ready = env.get("ALPACA_API_KEY") == "set" and env.get("ALPACA_SECRET_KEY") == "set"
        risk_ack = risk_acknowledgement_status(self.root)
        complete = alpaca_ready and autopilot_path.exists() and bool(risk_ack.get("accepted")) and env.get("BONEHAWK_SETUP_COMPLETE") == "true"
        return {
            "ok": True,
            "required": not complete,
            "complete": complete,
            "steps": {
                "alpaca": {
                    "status": "set" if alpaca_ready else "missing",
                    "message": "Alpaca paper keys are required for autopilot paper orders.",
                },
                "autopilot": {
                    "status": "set" if autopilot_path.exists() else "missing",
                    "message": "Autopilot config stores safety rails; trade size is decided dynamically from account and market data.",
                },
                "telegram": {
                    "status": "set" if env.get("TELEGRAM_BOT_TOKEN") == "set" and env.get("ALLOWED_CHAT_IDS") == "set" else "optional",
                    "message": "Telegram alerts are optional.",
                },
                "risk_acknowledgement": {
                    "status": "set" if risk_ack.get("accepted") else "missing",
                    "message": "Required acknowledgement that automated trading is not financial advice and can lose money.",
                },
            },
            "env": {key: env.get(key, "missing") for key in sorted(env) if key in SETUP_SECRET_KEYS or key.startswith("ALPACA_") or key == "BONEHAWK_SETUP_COMPLETE"},
        }

    def setup_diagnostics(self) -> dict[str, Any]:
        env = _read_env_presence(self.root / ".env")
        autopilot_path = self.root / "config" / "autopilot.json"
        client = self._alpaca_client()
        checks: dict[str, dict[str, Any]] = {
            "alpaca_keys": _diagnostic_check(
                env.get("ALPACA_API_KEY") == "set" and env.get("ALPACA_SECRET_KEY") == "set",
                "Alpaca keys are present.",
                "Add Alpaca paper API key and secret in setup.",
            ),
            "autopilot_config": _diagnostic_check(
                autopilot_path.exists(),
                "Autopilot config exists.",
                "Create config/autopilot.json through setup.",
            ),
            "telegram": _diagnostic_check(
                env.get("TELEGRAM_BOT_TOKEN") == "set" and env.get("ALLOWED_CHAT_IDS") == "set",
                "Telegram command channel is configured.",
                "Telegram is optional; add TELEGRAM_BOT_TOKEN and ALLOWED_CHAT_IDS to use phone controls.",
                warn=True,
            ),
            "risk_disclosure": _diagnostic_check(
                _risk_disclosure_present(self.root / "README.md"),
                "Risk disclosure is present.",
                "README.md must clearly say this is not financial advice and that trading can lose money.",
            ),
            "live_mode": _diagnostic_check(
                env.get("ALPACA_PAPER", "true") != "false" and env.get("ALPACA_ALLOW_LIVE", "false") not in {"true", "1", "yes", "on"},
                "Live trading is disabled.",
                "Set ALPACA_PAPER=true and ALPACA_ALLOW_LIVE=false for private beta testing.",
            ),
        }
        checks["alpaca_account"] = self._diagnose_alpaca_account(client)
        checks["market_clock"] = self._diagnose_market_clock(client)
        checks["market_calendar"] = self._diagnose_market_calendar(client)
        summary = _diagnostic_summary(checks)
        return {
            "ok": summary["failed"] == 0,
            "status": "ready" if summary["failed"] == 0 else "needs_attention",
            "summary": summary,
            "checks": checks,
            "env": {key: env.get(key, "missing") for key in sorted(env) if key in SETUP_SECRET_KEYS or key.startswith("ALPACA_") or key == "BONEHAWK_SETUP_COMPLETE"},
            "message": "Setup diagnostics completed without exposing secret values.",
        }

    def apply_setup(self, payload: dict[str, Any]) -> dict[str, Any]:
        validation = _validate_setup_payload(payload)
        if validation:
            return validation
        env_path = self.root / ".env"
        secret_updates = {
            "ALPACA_API_KEY": payload.get("alpaca_api_key"),
            "ALPACA_SECRET_KEY": payload.get("alpaca_secret_key"),
            "TELEGRAM_BOT_TOKEN": payload.get("telegram_bot_token"),
            "ALLOWED_CHAT_IDS": payload.get("allowed_chat_ids"),
        }
        for key, value in secret_updates.items():
            normalized = str(value or "").strip()
            if normalized:
                _write_env_value(env_path, key, normalized)
        _write_env_value(env_path, "ALPACA_PAPER", "true" if _setup_bool(payload.get("alpaca_paper"), default=True) else "false")
        _write_env_value(env_path, "ALPACA_ALLOW_LIVE", "false")
        _write_env_value(env_path, "TRADING_MODE", "paper")
        _write_env_value(env_path, "BONEHAWK_SETUP_COMPLETE", "true")
        record_risk_acknowledgement(self.root, accepted=True, actor="setup")

        autopilot_path = self.root / "config" / "autopilot.json"
        existing = load_autopilot_config(autopilot_path)
        save_autopilot_config(
            autopilot_path,
            AutopilotConfig(
                enabled=_setup_bool(payload.get("autopilot_enabled"), default=existing.enabled or True),
                mode="paper",
                broker="alpaca",
                allow_live=False,
                max_trade_usd=existing.max_trade_usd,
                max_daily_loss_usd=existing.max_daily_loss_usd,
                max_open_positions=int(float(payload.get("max_open_positions", existing.max_open_positions))),
                min_confidence=existing.min_confidence,
                symbols_per_run=existing.symbols_per_run,
                strategies=existing.strategies,
            ),
        )
        return {"ok": True, "status": "saved", "message": "Bonehawk setup saved locally.", "setup": self.setup_status()}

    def run_command(self, command_id: str, inputs: dict[str, Any] | None = None, confirm: str = "") -> dict[str, Any]:
        return run_command(self.root, command_id, inputs=inputs, confirm=confirm)

    def market_intel(self) -> dict[str, Any]:
        watchlist = self.watchlist()
        portfolio = self._alpaca_portfolio()
        snapshot_watchlist = _watchlist_with_portfolio_positions(watchlist, portfolio.get("watchlist_positions") or [])
        snapshot = self.intel_client.snapshot(snapshot_watchlist)
        snapshot["portfolio_source"] = {
            "status": portfolio.get("status", "watchlist"),
            "message": portfolio.get("message", "Using configured watchlist positions."),
        }
        if portfolio.get("performance"):
            snapshot["positions"] = portfolio.get("positions", [])
            snapshot["portfolio_performance"] = portfolio["performance"]
            snapshot["portfolio_account"] = portfolio.get("account", {})
        return snapshot

    def portfolio_sync(self) -> dict[str, Any]:
        return portfolio_sync_snapshot(self.watchlist(), alpaca_portfolio=self._alpaca_portfolio())

    def autopilot(self) -> dict[str, Any]:
        config, market_gate = self._sync_autopilot_market_hours(load_autopilot_config(self.root / "config" / "autopilot.json"))
        payload = self._autopilot_engine(config=config).snapshot()
        payload["market_hours"] = market_gate
        return self._with_autopilot_channels(payload)

    def autopilot_scan(self) -> dict[str, Any]:
        return self._with_autopilot_channels(self._autopilot_engine().scan(self._autopilot_watchlist()))

    def autopilot_execute(self, confirm: str = "", market_gate: dict[str, Any] | None = None) -> dict[str, Any]:
        config = load_autopilot_config(self.root / "config" / "autopilot.json")
        if market_gate is None:
            config, market_gate = self._sync_autopilot_market_hours(config)
        if not market_gate.get("can_execute", True):
            return self._with_autopilot_channels(
                {
                    "ok": False,
                    "status": market_gate.get("status", "market_closed"),
                    "config": config.snapshot(),
                    "orders": [],
                    "executed": [],
                    "blocked": [{"status": market_gate.get("status", "market_closed"), "reason": market_gate.get("message", "Market is closed.")}],
                    "market_hours": market_gate,
                    "message": market_gate.get("message", "Market is closed."),
                }
            )
        payload = self._autopilot_engine(config=config).execute(self._autopilot_watchlist(), confirm=confirm, market_gate=market_gate)
        return self._with_autopilot_channels(payload)

    def autopilot_background_status(self) -> dict[str, Any]:
        with self._background_lock:
            running = bool(self._background_thread and self._background_thread.is_alive() and not self._background_stop.is_set())
            return {
                "ok": True,
                "status": "running" if running else "stopped",
                "enabled": running,
                "running": running,
                "paper_only": True,
                "interval_seconds": BACKGROUND_AUTOPILOT_INTERVAL_SECONDS,
                "runs": self._background_runs,
                "in_cycle": self._background_cycle_lock.locked(),
                "started_at": self._background_started_at,
                "last_started_at": self._background_last_started_at,
                "last_finished_at": self._background_last_finished_at,
                "last_error": self._background_last_error,
                "last_result": self._background_last_result,
                "message": "Background autopilot scans and runs paper execution every 10 seconds while Bonehawk is open.",
            }

    def start_autopilot_background(self) -> dict[str, Any]:
        with self._background_lock:
            if self._background_thread and self._background_thread.is_alive() and not self._background_stop.is_set():
                already_running = True
            else:
                already_running = False
                self._background_stop.clear()
                self._background_started_at = _utc_now()
                self._background_thread = threading.Thread(target=self._autopilot_background_loop, name="bonehawk-autopilot-background", daemon=True)
                self._background_thread.start()
        return {**self.autopilot_background_status(), "status": "already_running" if already_running else "started"}

    def stop_autopilot_background(self) -> dict[str, Any]:
        with self._background_lock:
            self._background_stop.set()
            thread = self._background_thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1)
        return {**self.autopilot_background_status(), "status": "stopped"}

    def run_autopilot_background_cycle(self) -> dict[str, Any]:
        if not self._background_cycle_lock.acquire(blocking=False):
            payload = {
                "ok": False,
                "status": "cycle_already_running",
                "interval_seconds": BACKGROUND_AUTOPILOT_INTERVAL_SECONDS,
                "paper_only": True,
                "message": "Previous background paper cycle is still running.",
            }
            with self._background_lock:
                self._background_last_result = payload
            return payload

        started_at = _utc_now()
        result: dict[str, Any]
        try:
            with self._background_lock:
                self._background_last_started_at = started_at
                self._background_last_error = ""
            config = load_autopilot_config(self.root / "config" / "autopilot.json")
            config, market_gate = self._sync_autopilot_market_hours(config)
            if config.mode != "paper":
                result = {
                    "ok": False,
                    "status": "paper_only",
                    "interval_seconds": BACKGROUND_AUTOPILOT_INTERVAL_SECONDS,
                    "paper_only": True,
                    "market_hours": market_gate,
                    "message": "Background autopilot is paper-only and will not run while autopilot is in live mode.",
                }
            elif not market_gate.get("can_execute", True):
                result = {
                    "ok": False,
                    "status": market_gate.get("status", "market_closed"),
                    "interval_seconds": BACKGROUND_AUTOPILOT_INTERVAL_SECONDS,
                    "paper_only": True,
                    "market_hours": market_gate,
                    "message": market_gate.get("message", "Market is closed."),
                }
            elif not config.enabled:
                result = {
                    "ok": False,
                    "status": "disabled",
                    "interval_seconds": BACKGROUND_AUTOPILOT_INTERVAL_SECONDS,
                    "paper_only": True,
                    "market_hours": market_gate,
                    "message": "Autopilot is disabled in config/autopilot.json.",
                }
            else:
                scan = self.autopilot_scan()
                execution = self.autopilot_execute(confirm="", market_gate=market_gate)
                result = {
                    "ok": True,
                    "status": "cycle_completed",
                    "interval_seconds": BACKGROUND_AUTOPILOT_INTERVAL_SECONDS,
                    "paper_only": True,
                    "started_at": started_at,
                    "finished_at": _utc_now(),
                    "market_hours": market_gate,
                    "scan": _background_scan_summary(scan),
                    "execution": _background_execution_summary(execution),
                    "message": "Background scan and paper execution cycle completed.",
                }
                result["display"] = _background_display_payload(scan, execution, result, self.autopilot())
        except Exception as error:
            result = {
                "ok": False,
                "status": "cycle_failed",
                "interval_seconds": BACKGROUND_AUTOPILOT_INTERVAL_SECONDS,
                "paper_only": True,
                "started_at": started_at,
                "finished_at": _utc_now(),
                "message": "Background paper cycle failed.",
                "error": str(error),
            }
        finally:
            with self._background_lock:
                self._background_runs += 1
                result["runs"] = self._background_runs
                if isinstance(result.get("display"), dict):
                    result["display"].setdefault("background", {})["runs"] = self._background_runs
                self._background_last_finished_at = str(result.get("finished_at") or _utc_now())
                self._background_last_error = str(result.get("error") or "")
                self._background_last_result = result
            self._background_cycle_lock.release()
        return result

    def _autopilot_background_loop(self) -> None:
        while not self._background_stop.is_set():
            self.run_autopilot_background_cycle()
            if self._background_stop.wait(BACKGROUND_AUTOPILOT_INTERVAL_SECONDS):
                break

    def set_autopilot_setting(self, setting: Any, value: Any, confirm: str = "") -> dict[str, Any]:
        path = self.root / "config" / "autopilot.json"
        config = load_autopilot_config(path)
        next_config, payload = update_autopilot_config(config, setting, value, confirm=confirm)
        if payload.get("ok"):
            if str(setting or "").strip() in {"mode", "allow_live"} and (str(value).strip().lower() == "live" or _setup_bool(value, default=False)):
                readiness = self.live_readiness()
                if not readiness.get("ok"):
                    return {
                        "ok": False,
                        "status": "live_readiness_locked",
                        "message": readiness.get("message", "Live readiness checks are locked."),
                        "config": config.snapshot(),
                        "live_readiness": readiness,
                    }
                payload["live_readiness"] = readiness
            save_autopilot_config(path, next_config)
            payload["config"] = next_config.snapshot()
            if str(setting or "").strip() == "enabled" and not next_config.enabled:
                payload["background"] = self.stop_autopilot_background()
        else:
            payload["config"] = config.snapshot()
        return payload

    def stocks(self) -> dict[str, Any]:
        universe_path = self.root / "config" / "market_universe.json"
        if not universe_path.exists():
            universe_path = self.root / "config" / "market_universe.example.json"
        return market_universe_snapshot(universe_path)

    def scanner(self) -> dict[str, Any]:
        watchlist = self.scanner_watchlist()
        snapshot = self.intel_client.snapshot(watchlist)
        return scan_market(watchlist, snapshot)

    def trade_ideas(self) -> dict[str, Any]:
        watchlist = self.scanner_watchlist()
        snapshot = self.intel_client.snapshot(watchlist)
        scan_result = scan_market(watchlist, snapshot)
        symbols = _trade_quote_symbols(scan_result, watchlist.positions)
        quotes = self.quote_client.get_quotes(symbols)
        history_symbols = list(dict.fromkeys([*symbols, "SPY", "QQQ"]))
        histories = self.quote_client.get_histories(history_symbols)
        technicals = {symbol: history.technicals() for symbol, history in histories.items()}
        market_trend = build_market_trend(technicals)
        ideas = build_trade_ideas(scan_result, quotes, watchlist.positions, watchlist.risk, technicals=technicals, market_trend=market_trend)
        record_decisions(self.root / "logs" / "decision_log.jsonl", "dashboard", ideas)
        return {
            "summary": scan_result["summary"],
            "market_trend": market_trend,
            "scans": scan_result["scans"],
            "alerts": scan_result["alerts"],
            "ideas": ideas,
            "notice": "Trade ideas are review signals only. No live stock order was placed.",
        }

    def growth_candidates(self) -> dict[str, Any]:
        watchlist = self.scanner_watchlist()
        snapshot = self.intel_client.snapshot(watchlist)
        scan_result = scan_market(watchlist, snapshot)
        symbols = _trade_quote_symbols(scan_result, watchlist.positions, limit=40)
        quotes = self.quote_client.get_quotes(symbols)
        history_symbols = list(dict.fromkeys([*symbols, "SPY", "QQQ"]))
        histories = self.quote_client.get_histories(history_symbols)
        technicals = {symbol: history.technicals() for symbol, history in histories.items()}
        market_trend = build_market_trend(technicals)
        candidates = build_growth_candidates(scan_result, quotes, technicals, market_trend=market_trend)
        record_decisions(self.root / "logs" / "decision_log.jsonl", "growth", candidates)
        return {
            "summary": scan_result["summary"],
            "market_trend": market_trend,
            "candidates": candidates,
            "message": build_growth_candidates_message(candidates),
            "notice": "Growth candidates are review-only quick-return signals. No live order was placed.",
        }

    def stock_chart(self, symbol: Any, range_key: Any = "1d") -> dict[str, Any]:
        normalized_symbol = _normalize_stock_symbol(symbol)
        if not normalized_symbol:
            return {"ok": False, "status": "invalid_symbol", "message": "Choose a valid stock symbol."}
        normalized_range = str(range_key or "1d").strip().lower()
        if normalized_range not in CHART_RANGES:
            normalized_range = "1d"
        try:
            chart = self.quote_client.get_stock_chart(normalized_symbol, normalized_range)
        except Exception as error:
            return {"ok": False, "status": "chart_unavailable", "symbol": normalized_symbol, "message": str(error)}
        closes = [point.close for point in chart.points]
        return {
            "ok": True,
            "symbol": chart.symbol,
            "range": chart.range_key,
            "interval": chart.interval,
            "latest_price": round(chart.latest_price, 4),
            "change_pct": round(chart.change_pct, 2),
            "points": [
                {"timestamp": point.timestamp, "close": round(point.close, 4), "volume": point.volume}
                for point in chart.points
            ],
            "summary": {
                "point_count": len(chart.points),
                "high": round(max(closes), 4),
                "low": round(min(closes), 4),
            },
            "review_only": True,
            "notice": "Chart data is for review only. No order was placed.",
        }

    def stock_order_intent(self, symbol: Any, side: Any, quantity: Any) -> dict[str, Any]:
        request_or_error = _stock_order_request(symbol, side, quantity)
        if isinstance(request_or_error, dict):
            return request_or_error
        request = request_or_error
        quote = self.quote_client.get_quotes([request.symbol]).get(request.symbol)
        current_price = round(quote.price, 4) if quote else None
        reason = f"{request.side} intent captured from dashboard. No live stock order was placed."
        intent = {
            "symbol": request.symbol,
            "side": request.side.lower(),
            "action": f"{request.side}_INTENT",
            "confidence": None,
            "current_price": current_price,
            "quantity": request.quantity,
            "status": "recorded",
            "reason": reason,
            "signals": [f"quantity {request.quantity:g}", "stock order intent", "review only"],
            "review_only": True,
        }
        record_decisions(self.root / "logs" / "decision_log.jsonl", "stock_order_intent", [intent])
        record_order_truth_event(self.root, "stock_order_intent", intent)
        return {
            "ok": True,
            "status": "recorded",
            "symbol": request.symbol,
            "side": request.side,
            "quantity": request.quantity,
            "current_price": current_price,
            "review_only": True,
            "message": f"{request.side} ticket recorded for {request.quantity:g} {request.symbol}. No live stock order was placed.",
        }

    def stock_order(self, symbol: Any, side: Any, quantity: Any, confirm: str = "") -> dict[str, Any]:
        request_or_error = _stock_order_request(symbol, side, quantity)
        if isinstance(request_or_error, dict):
            return request_or_error
        request = request_or_error
        order = AlpacaOrderRequest(
            symbol=request.symbol,
            side=request.side.lower(),
            quantity=request.quantity,
            order_type="market",
            time_in_force="day",
        )
        result = self._alpaca_client().place_order(order, confirm=confirm)
        order_context = {
            "symbol": request.symbol,
            "side": request.side.lower(),
            "action": f"{request.side}_MANUAL_ORDER",
            "quantity": request.quantity,
            "reason": "Manual dashboard order.",
            "signals": ["manual dashboard order"],
            "review_only": bool(result.get("review_only", False)),
        }
        truth_row = {**result, "action": order_context["action"], "reason": result.get("message"), "quantity": result.get("quantity", request.quantity)}
        record_order_truth_event(self.root, "alpaca_stock_order" if result.get("ok") else "stock_order_attempt", truth_row)
        record_trade_journal_entry(self.root, "alpaca_stock_order" if result.get("ok") else "stock_order_attempt", order_context, truth_row)
        record_decisions(
            self.root / "logs" / "decision_log.jsonl",
            "alpaca_stock_order" if result.get("ok") else "stock_order_attempt",
            [
                {
                    "symbol": result.get("symbol") or request.symbol,
                    "action": f"{result.get('side') or request.side}_{'LIVE' if result.get('ok') else 'BLOCKED'}",
                    "confidence": None,
                    "current_price": None,
                    "quantity": result.get("quantity", request.quantity),
                    "status": result.get("status"),
                    "broker_status": result.get("broker_status"),
                    "broker_order_id": result.get("broker_order_id"),
                    "filled_quantity": result.get("filled_quantity"),
                    "filled_average_price": result.get("filled_average_price"),
                    "fill_status": result.get("fill_status"),
                    "reason": result.get("message"),
                    "signals": [
                        f"quantity {result.get('quantity', request.quantity)}",
                        f"broker_order_id {result.get('broker_order_id') or 'unknown'}",
                        f"broker_status {result.get('broker_status') or 'unknown'}",
                        f"fill_status {result.get('fill_status') or 'unknown'}",
                    ],
                    "review_only": bool(result.get("review_only", False)),
                }
            ],
        )
        return result

    def emergency_liquidate(self, confirm: str = "") -> dict[str, Any]:
        if str(confirm or "").strip() != EMERGENCY_LIQUIDATE_CONFIRM_PHRASE:
            return {
                "ok": False,
                "status": "confirmation_required",
                "confirm_phrase": EMERGENCY_LIQUIDATE_CONFIRM_PHRASE,
                "message": "Emergency liquidation requires typed confirmation.",
            }
        config_path = self.root / "config" / "autopilot.json"
        config = load_autopilot_config(config_path)
        save_autopilot_config(config_path, replace(config, enabled=False))
        self.stop_autopilot_background()
        client = self._alpaca_client()
        cancel_result = client.cancel_all_orders() if hasattr(client, "cancel_all_orders") else {"ok": False, "status": "unsupported", "message": "Cancel-all is not available for this broker client."}
        try:
            positions = client.get_positions()
        except Exception as error:
            return {
                "ok": False,
                "status": "positions_unavailable",
                "cancel_result": cancel_result,
                "message": "Could not load positions for emergency liquidation.",
                "detail": _safe_error_message(error),
            }
        submitted: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for position in positions:
            liquidation = _liquidation_position(position)
            if not liquidation["can_sell"]:
                skipped.append(liquidation)
                continue
            order = AlpacaOrderRequest(symbol=liquidation["symbol"], side="sell", quantity=liquidation["quantity"], order_type="market", time_in_force="day")
            result = client.place_order(order, confirm=LIVE_CONFIRM_PHRASE)
            row = {
                **result,
                "symbol": result.get("symbol") or liquidation["symbol"],
                "side": result.get("side") or "SELL",
                "quantity": result.get("quantity") or liquidation["quantity"],
                "action": "EMERGENCY_LIQUIDATE",
                "reason": result.get("message") or "Emergency liquidation order submitted.",
            }
            submitted.append(row)
            record_order_truth_event(self.root, "emergency_liquidation", row)
            record_trade_journal_entry(self.root, "emergency_liquidation", {"symbol": liquidation["symbol"], "side": "sell", "action": "EMERGENCY_LIQUIDATE", "reason": "Emergency liquidation requested.", "quantity": liquidation["quantity"]}, row)
        record_decisions(self.root / "logs" / "decision_log.jsonl", "emergency_liquidation", submitted + skipped)
        rejected = [item for item in submitted if not item.get("ok")]
        return {
            "ok": not rejected,
            "status": "liquidation_submitted" if submitted else "no_sellable_positions",
            "cancel_result": cancel_result,
            "submitted": submitted,
            "skipped": skipped,
            "summary": {
                "submitted": len([item for item in submitted if item.get("ok")]),
                "rejected": len(rejected),
                "skipped": len(skipped),
            },
            "message": "Autopilot stopped, open orders canceled when supported, and sell orders submitted for available positions.",
        }

    def tickets(self) -> dict[str, Any]:
        rows = latest_decisions(self.root / "logs" / "decision_log.jsonl", limit=200)
        tickets = [_ticket_from_decision(row) for row in rows]
        tickets = [ticket for ticket in tickets if ticket is not None]
        tickets = self._refresh_alpaca_ticket_statuses(tickets)
        return {"tickets": tickets, "summary": {"count": len(tickets)}}

    def live_orders(self) -> dict[str, Any]:
        tickets_payload = self.tickets()
        events = [_live_order_event(ticket) for ticket in tickets_payload.get("tickets", [])]
        summary = _live_order_summary(events)
        background = self.autopilot_background_status()
        return {
            "ok": True,
            "status": "ready",
            "events": events[:80],
            "summary": summary,
            "background": {
                "status": background.get("status"),
                "running": background.get("running"),
                "runs": background.get("runs"),
                "last_error": background.get("last_error"),
                "last_finished_at": background.get("last_finished_at"),
            },
            "reconciliation": self._last_order_reconciliation,
            "message": "Live View is watching recent buy/sell tickets and Alpaca broker responses.",
        }

    def reconcile_orders(self, limit: Any = 80) -> dict[str, Any]:
        limit_value = int(max(1, min(250, _safe_float(limit, 80))))
        snapshot = order_truth_snapshot(self.root, limit=limit_value)
        refreshable = _refreshable_order_events(snapshot.get("current") or snapshot.get("active") or [])
        if not refreshable:
            report = {
                "ok": True,
                "status": "nothing_to_reconcile",
                "checked_at": datetime.now(UTC).isoformat(),
                "summary": {"checked": 0, "refreshed": 0, "failed": 0, "terminal": 0, "active": len(snapshot.get("active") or [])},
                "refreshed": [],
                "failed": [],
                "order_truth": snapshot,
                "message": "No active Alpaca broker orders need reconciliation.",
            }
            self._last_order_reconciliation = _reconciliation_public_summary(report)
            return report
        client = self._alpaca_client()
        if not hasattr(client, "get_order"):
            report = {
                "ok": False,
                "status": "unsupported",
                "checked_at": datetime.now(UTC).isoformat(),
                "summary": {"checked": 0, "refreshed": 0, "failed": 0, "terminal": 0, "active": len(snapshot.get("active") or [])},
                "refreshed": [],
                "failed": [],
                "order_truth": snapshot,
                "message": "The current broker client does not support order status lookup.",
            }
            self._last_order_reconciliation = _reconciliation_public_summary(report)
            return report

        refreshed: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for event in refreshable[:12]:
            order_id = str(event.get("broker_order_id") or "").strip()
            try:
                order = client.get_order(order_id)
            except Exception as error:
                failed.append({"broker_order_id": order_id, "symbol": event.get("symbol"), "message": _safe_error_message(error)})
                continue
            row = _truth_row_with_alpaca_order(event, order)
            refreshed.append(record_order_truth_event(self.root, "alpaca_order_reconcile", row))

        next_snapshot = order_truth_snapshot(self.root, limit=limit_value)
        terminal = [item for item in refreshed if item.get("stage") in {"filled", "rejected", "canceled"}]
        report = {
            "ok": not failed,
            "status": "reconciled" if refreshed else "reconcile_failed",
            "checked_at": datetime.now(UTC).isoformat(),
            "summary": {
                "checked": len(refreshable[:12]),
                "refreshed": len(refreshed),
                "failed": len(failed),
                "terminal": len(terminal),
                "active": len(next_snapshot.get("active") or []),
            },
            "refreshed": refreshed,
            "failed": failed,
            "order_truth": next_snapshot,
            "message": "Active Alpaca order statuses refreshed.",
        }
        self._last_order_reconciliation = _reconciliation_public_summary(report)
        return report

    def loss_postmortem_report(self, window_hours: Any = 24) -> dict[str, Any]:
        hours = int(max(1, min(168, _safe_float(window_hours, 24))))
        return build_loss_postmortem_report(self.root, window_hours=hours)

    def apply_loss_postmortem_report(self, report_id: Any, reviewer_notes: Any = "") -> dict[str, Any]:
        report = self.loss_postmortem_report()
        expected_report_id = str(report.get("report_id") or "")
        submitted_report_id = str(report_id or "").strip()
        if not submitted_report_id:
            return {"ok": False, "status": "missing_report", "message": "Run a post-mortem report before applying learnings."}
        if submitted_report_id != expected_report_id:
            return {
                "ok": False,
                "status": "stale_report",
                "message": "The post-mortem report changed. Run the report again before applying learnings.",
                "report": report,
            }
        return apply_loss_postmortem_report(self.root, report, reviewer_notes=str(reviewer_notes or ""))

    def overview_metrics(self) -> dict[str, Any]:
        portfolio = self._alpaca_portfolio()
        performance = portfolio.get("performance") or _empty_portfolio_performance(str(portfolio.get("status") or "unknown"))
        market_trend = self._market_trend()
        return {
            "ok": True,
            "status": "ready",
            "updated_at": datetime.now(UTC).isoformat(),
            "portfolio": {
                "source_status": portfolio.get("status", "unknown"),
                "account_value": performance.get("account_value", performance.get("total_value", 0)),
                "total_value": performance.get("total_value", 0),
                "cash": performance.get("cash", 0),
                "buying_power": performance.get("buying_power", 0),
                "unrealized_pnl": performance.get("unrealized_pnl", 0),
                "unrealized_pnl_pct": performance.get("unrealized_pnl_pct", 0),
                "positions": len(performance.get("positions") or []),
            },
            "market_trend": market_trend,
            "message": "Overview metrics refreshed.",
        }

    def report(self, window_minutes: int = 10) -> dict[str, Any]:
        minutes = int(max(1, min(120, _safe_float(window_minutes, 10))))
        metrics = self.overview_metrics()
        live_orders = self.live_orders()
        events = _events_within_window(live_orders.get("events") or [], minutes)
        return {
            "ok": True,
            "status": "ready",
            "generated_at": datetime.now(UTC).isoformat(),
            "window_minutes": minutes,
            "portfolio": metrics.get("portfolio") or {},
            "market_trend": metrics.get("market_trend", "UNKNOWN"),
            "trades": events,
            "summary": {
                "trade_count": len(events),
                "submitted": sum(1 for event in events if event.get("category") == "submitted"),
                "filled": sum(1 for event in events if event.get("category") == "filled"),
                "rejected": sum(1 for event in events if event.get("category") == "rejected"),
            },
        }

    def trading_desk(self) -> dict[str, Any]:
        config, market_gate = self._sync_autopilot_market_hours(load_autopilot_config(self.root / "config" / "autopilot.json"))
        watchlist = self._autopilot_watchlist()
        snapshot = self.intel_client.snapshot(watchlist)
        symbols = _trade_quote_symbols({"scans": [{"symbol": symbol} for symbol in watchlist.symbols]}, watchlist.positions, limit=40)
        symbols = list(dict.fromkeys([*symbols, *[position.symbol for position in watchlist.positions], "SPY", "QQQ"]))
        quotes = self.quote_client.get_quotes(symbols)
        histories = self.quote_client.get_histories(symbols)
        account_state = self._autopilot_engine(config=config)._account_state()
        order_truth = order_truth_snapshot(self.root)
        health = build_data_health(
            market_snapshot=snapshot,
            quotes=quotes,
            account_state=account_state,
            market_gate=market_gate,
            order_summary=order_truth.get("summary") or {},
        )
        return {
            "ok": True,
            "status": "ready",
            "updated_at": datetime.now(UTC).isoformat(),
            "market_hours": market_gate,
            "data_health": health,
            "order_truth": order_truth,
            "trade_journal": trade_journal_snapshot(self.root),
            "strategy_scorecard": strategy_scorecard(self.root),
            "shadow_mode": shadow_mode_snapshot(self.root, quotes, min_age_minutes=config.scan_window_minutes),
            "backtest": build_backtest(histories),
            "message": "Trading desk snapshot updated.",
        }

    def private_beta_check(self) -> dict[str, Any]:
        return build_private_beta_report(self.root, version=_project_version(self.root / "pyproject.toml"))

    def paper_evidence(self) -> dict[str, Any]:
        return build_paper_evidence_report(self.root)

    def live_readiness(self) -> dict[str, Any]:
        return build_live_readiness_report(self.root, diagnostics=self.setup_diagnostics())

    def public_release_readiness(self) -> dict[str, Any]:
        return build_public_release_report(self.root, version=_project_version(self.root / "pyproject.toml"))

    def operational_health(self) -> dict[str, Any]:
        config, market_gate = self._sync_autopilot_market_hours(load_autopilot_config(self.root / "config" / "autopilot.json"))
        return build_operational_health_report(
            self.root,
            setup_diagnostics=self.setup_diagnostics(),
            trading_desk=self.trading_desk(),
            background=self.autopilot_background_status(),
            market_hours=market_gate,
        )

    def _market_trend(self) -> str:
        try:
            histories = self.quote_client.get_histories(["SPY", "QQQ"])
        except Exception:
            return "UNKNOWN"
        technicals = {symbol: history.technicals() for symbol, history in histories.items()}
        return build_market_trend(technicals)

    def _refresh_alpaca_ticket_statuses(self, tickets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        refreshable_indexes = [
            index
            for index, ticket in enumerate(tickets)
            if ticket.get("broker_order_id") and ticket.get("review_only") is False
        ][:12]
        if not refreshable_indexes:
            return tickets
        client = self._alpaca_client()
        if not hasattr(client, "get_order"):
            return tickets
        refreshed = list(tickets)
        now = time.monotonic()
        for index in refreshable_indexes:
            ticket = refreshed[index]
            order_id = str(ticket.get("broker_order_id") or "")
            cached = self._ticket_refresh_cache.get(order_id)
            if cached and now - cached[0] < 20:
                refreshed[index] = _ticket_with_alpaca_order(ticket, cached[1])
                continue
            try:
                order = client.get_order(order_id)
            except Exception:
                continue
            self._ticket_refresh_cache[order_id] = (now, order)
            refreshed[index] = _ticket_with_alpaca_order(ticket, order)
            record_order_truth_event(self.root, "alpaca_order_refresh", refreshed[index])
        return refreshed

    def _alpaca_client(self) -> Any:
        if self.alpaca_client is not None:
            return self.alpaca_client
        return AlpacaTradingClient(AlpacaConfig.from_env(self.root / ".env"))

    def _alpaca_portfolio(self) -> dict[str, Any]:
        try:
            client = self._alpaca_client()
            configured = bool(getattr(getattr(client, "config", None), "is_configured", True))
        except Exception:
            return {"status": "watchlist", "message": "Using configured watchlist positions until Alpaca portfolio data is available."}
        try:
            account = client.get_account()
        except Exception:
            if configured:
                return {
                    "status": "error",
                    "message": "Alpaca account data is unavailable. Check that the key pair matches the selected paper/live mode.",
                    "account": {},
                    "positions": [],
                    "watchlist_positions": [],
                    "performance": _empty_portfolio_performance("alpaca_error"),
                }
            return {"status": "watchlist", "message": "Using configured watchlist positions until Alpaca portfolio data is available."}
        try:
            raw_positions = client.get_positions()
        except Exception:
            raw_positions = []
            status = "partial"
            message = "Loaded Alpaca account value, but open positions are unavailable."
        else:
            status = "connected"
            message = f"Loaded {len(raw_positions)} Alpaca position(s)."
        performance = compute_alpaca_portfolio_performance(account, raw_positions)
        positions = performance.get("positions", [])
        return {
            "status": status,
            "message": message,
            "account": _public_alpaca_account(account),
            "positions": positions,
            "raw_positions": raw_positions,
            "watchlist_positions": [
                Position(
                    symbol=str(position.get("symbol") or "").upper(),
                    quantity=float(position.get("quantity") or 0),
                    cost_basis=float(position.get("cost_basis") or 0),
                )
                for position in positions
                if position.get("symbol")
            ],
            "performance": performance,
        }

    def _sync_autopilot_market_hours(self, config: AutopilotConfig) -> tuple[AutopilotConfig, dict[str, Any]]:
        state_path = self.root / MARKET_HOURS_STATE_FILE
        state = _read_json_object(state_path)
        clock = _alpaca_clock(self._alpaca_client())
        gate = evaluate_market_hours_gate(clock, enabled=config.enabled, state=state)
        next_config = config
        if gate.get("should_disable_autopilot"):
            next_config = replace(config, enabled=False)
            save_autopilot_config(self.root / "config" / "autopilot.json", next_config)
        elif gate.get("should_enable_autopilot"):
            next_config = replace(config, enabled=True)
            save_autopilot_config(self.root / "config" / "autopilot.json", next_config)
        next_state = market_hours_state_after_gate(gate, state)
        if next_state != state:
            _write_json_object(state_path, next_state)
        if next_config.enabled != config.enabled:
            gate = {**gate, "autopilot_enabled_before": config.enabled, "autopilot_enabled_after": next_config.enabled}
        return next_config, gate

    def _diagnose_alpaca_account(self, client: Any) -> dict[str, Any]:
        try:
            account = client.get_account()
        except Exception as error:
            return _diagnostic_check(False, "", f"Alpaca account check failed. Check key pair and paper/live mode. Detail: {_safe_error_message(error)}")
        return _diagnostic_check(bool(account), "Alpaca account is reachable.", "Alpaca returned an empty account payload.")

    def _diagnose_market_clock(self, client: Any) -> dict[str, Any]:
        if not hasattr(client, "get_clock"):
            return _diagnostic_check(False, "", "Alpaca clock is unavailable for this connector.", warn=True)
        try:
            clock = client.get_clock()
        except Exception as error:
            return _diagnostic_check(False, "", f"Market clock check failed. Detail: {_safe_error_message(error)}", warn=True)
        return _diagnostic_check(bool(clock), "Alpaca market clock is reachable.", "Alpaca returned an empty market clock.", warn=True)

    def _diagnose_market_calendar(self, client: Any) -> dict[str, Any]:
        if not hasattr(client, "get_calendar"):
            return _diagnostic_check(False, "", "Alpaca calendar is unavailable for this connector.", warn=True)
        today = datetime.now(UTC).date()
        try:
            calendar = client.get_calendar(today.isoformat(), (today + timedelta(days=14)).isoformat())
        except Exception as error:
            return _diagnostic_check(False, "", f"Market calendar check failed. Detail: {_safe_error_message(error)}", warn=True)
        return _diagnostic_check(bool(calendar), "Alpaca market calendar is reachable.", "Alpaca returned no upcoming trading sessions.", warn=True)

    def _autopilot_engine(self, config: AutopilotConfig | None = None) -> AutopilotEngine:
        return AutopilotEngine(
            root=self.root,
            config=config or load_autopilot_config(self.root / "config" / "autopilot.json"),
            intel_client=self.intel_client,
            quote_client=self.quote_client,
            alpaca_client=self._alpaca_client(),
        )

    def _with_autopilot_channels(self, payload: dict[str, Any]) -> dict[str, Any]:
        env = _read_env_presence(self.root / ".env")
        token_ready = env.get("TELEGRAM_BOT_TOKEN") == "set"
        chats_ready = env.get("ALLOWED_CHAT_IDS") == "set"
        telegram_status = "ready" if token_ready and chats_ready else "needs_setup"
        return {
            **payload,
            "data_sources": {
                "news": "RSS/news plus optional Reddit and X feed templates.",
                "market": "Alpaca account/orders with Yahoo market quotes and charts.",
                "execution": "Alpaca paper orders by default.",
            },
            "telegram": {
                "status": telegram_status,
                "bot_token": "set" if token_ready else env.get("TELEGRAM_BOT_TOKEN", "missing"),
                "chat_ids": "set" if chats_ready else env.get("ALLOWED_CHAT_IDS", "missing"),
                "channel": "Telegram",
                "message": "Telegram alerts are ready." if telegram_status == "ready" else "Add TELEGRAM_BOT_TOKEN and ALLOWED_CHAT_IDS in setup to enable Telegram alerts.",
            },
        }

    def decision_log(self) -> dict[str, Any]:
        rows = latest_decisions(self.root / "logs" / "decision_log.jsonl")
        return {"decisions": rows, "summary": {"count": len(rows)}}

    def trade_idea_alerts(self) -> dict[str, Any]:
        payload = self.trade_ideas()
        record_decisions(self.root / "logs" / "decision_log.jsonl", "telegram", payload["ideas"])
        message = build_trade_ideas_message(payload["ideas"])
        result = subprocess.run(
            ["bash", str(self.root / "scripts" / "telegram.sh"), message],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        return {
            "ok": result.returncode == 0,
            "message": message,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }

    def scanner_alerts(self) -> dict[str, Any]:
        scan_result = self.scanner()
        message = build_alert_message(scan_result)
        result = subprocess.run(
            ["bash", str(self.root / "scripts" / "telegram.sh"), message],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        return {
            "ok": result.returncode == 0,
            "message": message,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }

    def watchlist(self):
        watchlist_path = self.root / "config" / "watchlist.json"
        if not watchlist_path.exists():
            watchlist_path = self.root / "config" / "watchlist.example.json"
        return load_watchlist(watchlist_path)

    def scanner_watchlist(self):
        watchlist = self.watchlist()
        universe_path = self.root / "config" / "market_universe.json"
        if not universe_path.exists():
            universe_path = self.root / "config" / "market_universe.example.json"
        universe = load_market_universe(universe_path)
        symbols = combine_symbols(watchlist.symbols, universe, limit=len(watchlist.symbols) + len(universe))
        return type(watchlist)(symbols=symbols, positions=watchlist.positions, risk=watchlist.risk, aliases=watchlist.aliases)

    def _autopilot_watchlist(self):
        watchlist = self.scanner_watchlist()
        portfolio = self._alpaca_portfolio()
        if portfolio.get("status") in {"connected", "partial"}:
            return _watchlist_with_portfolio_positions(watchlist, portfolio.get("watchlist_positions") or [], replace_positions=True)
        if portfolio.get("status") == "error":
            return _watchlist_with_portfolio_positions(watchlist, [], replace_positions=True)
        return watchlist

    def paper_cycle(self, notify: bool = False) -> dict[str, Any]:
        args = [str(self.root / ".venv" / "bin" / "python"), str(self.root / "scripts" / "paper_cycle.py")]
        if notify:
            args.append("--notify")
        if not Path(args[0]).exists():
            args[0] = "python3"
        result = subprocess.run(args, cwd=self.root, text=True, capture_output=True, check=False)
        return {
            "ok": result.returncode == 0,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }


def json_response(payload: Any, status: int = 200) -> tuple[int, dict[str, str], bytes]:
    return status, {"Content-Type": "application/json"}, json.dumps(payload, indent=2).encode("utf-8")


def html_response() -> tuple[int, dict[str, str], bytes]:
    return 200, {"Content-Type": "text/html; charset=utf-8"}, HTML.encode("utf-8")


def make_handler(service: DashboardService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            if path == "/":
                self._send(*html_response())
            elif path == "/api/status":
                self._send(*json_response(service.status()))
            elif path == "/api/setup-status":
                self._send(*json_response(service.setup_status()))
            elif path == "/api/setup-diagnostics":
                self._send(*json_response(service.setup_diagnostics()))
            elif path == "/api/private-beta-check":
                self._send(*json_response(service.private_beta_check()))
            elif path == "/api/paper-evidence":
                self._send(*json_response(service.paper_evidence()))
            elif path == "/api/live-readiness":
                self._send(*json_response(service.live_readiness()))
            elif path == "/api/operational-health":
                self._send(*json_response(service.operational_health()))
            elif path == "/api/public-release-readiness":
                self._send(*json_response(service.public_release_readiness()))
            elif path == "/api/market-intel":
                self._send(*json_response(service.market_intel()))
            elif path == "/api/portfolio-sync":
                self._send(*json_response(service.portfolio_sync()))
            elif path == "/api/overview-metrics":
                self._send(*json_response(service.overview_metrics()))
            elif path == "/api/trading-desk":
                self._send(*json_response(service.trading_desk()))
            elif path == "/api/autopilot":
                self._send(*json_response(service.autopilot()))
            elif path == "/api/autopilot-background":
                self._send(*json_response(service.autopilot_background_status()))
            elif path == "/api/stocks":
                self._send(*json_response(service.stocks()))
            elif path == "/api/scanner":
                self._send(*json_response(service.scanner()))
            elif path == "/api/trade-ideas":
                self._send(*json_response(service.trade_ideas()))
            elif path == "/api/growth-candidates":
                self._send(*json_response(service.growth_candidates()))
            elif path == "/api/stock-chart":
                payload = service.stock_chart(_first_query_value(query, "symbol"), _first_query_value(query, "range") or "1d")
                status = 200 if payload.get("ok") else 400
                self._send(*json_response(payload, status=status))
            elif path == "/api/decision-log":
                self._send(*json_response(service.decision_log()))
            elif path == "/api/tickets":
                self._send(*json_response(service.tickets()))
            elif path == "/api/live-orders":
                self._send(*json_response(service.live_orders()))
            elif path == "/api/postmortem-report":
                self._send(*json_response(service.loss_postmortem_report(_first_query_value(query, "hours") or 24)))
            elif path == "/api/commands":
                self._send(*json_response(service.command_catalog()))
            else:
                self._send(*json_response({"error": "not found"}, status=404))

        def do_POST(self) -> None:
            if self.path == "/api/paper-cycle":
                self._send(*json_response(service.paper_cycle(notify=False)))
            elif self.path == "/api/setup":
                try:
                    payload = self._read_json_body()
                except ValueError as error:
                    self._send(*json_response({"ok": False, "status": "bad_request", "message": str(error)}, status=400))
                    return
                result = service.apply_setup(payload)
                status = 200 if result.get("ok") else 400
                self._send(*json_response(result, status=status))
            elif self.path == "/api/paper-cycle-notify":
                self._send(*json_response(service.paper_cycle(notify=True)))
            elif self.path == "/api/scanner-alerts":
                self._send(*json_response(service.scanner_alerts()))
            elif self.path == "/api/trade-idea-alerts":
                self._send(*json_response(service.trade_idea_alerts()))
            elif self.path == "/api/autopilot-scan":
                self._send(*json_response(service.autopilot_scan()))
            elif self.path == "/api/autopilot-run":
                try:
                    payload = self._read_json_body()
                except ValueError as error:
                    self._send(*json_response({"ok": False, "status": "bad_request", "message": str(error)}, status=400))
                    return
                result = service.autopilot_execute(confirm=str(payload.get("confirm", "")))
                status = 200 if result.get("ok") else 409 if result.get("status") in {"disabled", "confirmation_required"} else 400
                self._send(*json_response(result, status=status))
            elif self.path == "/api/autopilot-background":
                try:
                    payload = self._read_json_body()
                except ValueError as error:
                    self._send(*json_response({"ok": False, "status": "bad_request", "message": str(error)}, status=400))
                    return
                enabled = str(payload.get("enabled", "")).strip().lower() in {"1", "true", "yes", "on"}
                result = service.start_autopilot_background() if enabled else service.stop_autopilot_background()
                self._send(*json_response(result))
            elif self.path == "/api/trading-mode":
                try:
                    payload = self._read_json_body()
                except ValueError as error:
                    self._send(*json_response({"ok": False, "status": "bad_request", "message": str(error)}, status=400))
                    return
                result = service.set_trading_mode(payload.get("mode"), confirm=str(payload.get("confirm", "")))
                status = 200 if result.get("ok") else 409 if result.get("status") == "confirmation_required" else 400
                self._send(*json_response(result, status=status))
            elif self.path == "/api/autopilot-settings":
                try:
                    payload = self._read_json_body()
                except ValueError as error:
                    self._send(*json_response({"ok": False, "status": "bad_request", "message": str(error)}, status=400))
                    return
                result = service.set_autopilot_setting(payload.get("setting"), payload.get("value"), confirm=str(payload.get("confirm", "")))
                status = 200 if result.get("ok") else 409 if result.get("status") == "confirmation_required" else 400
                self._send(*json_response(result, status=status))
            elif self.path == "/api/ui-theme":
                try:
                    payload = self._read_json_body()
                except ValueError as error:
                    self._send(*json_response({"ok": False, "status": "bad_request", "message": str(error)}, status=400))
                    return
                result = service.set_ui_theme(payload.get("theme"))
                status = 200 if result.get("ok") else 400
                self._send(*json_response(result, status=status))
            elif self.path == "/api/commands/run":
                try:
                    payload = self._read_json_body()
                except ValueError as error:
                    self._send(*json_response({"ok": False, "status": "bad_request", "message": str(error)}, status=400))
                    return
                inputs = payload.get("inputs", {})
                if not isinstance(inputs, dict):
                    self._send(*json_response({"ok": False, "status": "bad_request", "message": "inputs must be an object."}, status=400))
                    return
                result = service.run_command(str(payload.get("id", "")), inputs=inputs, confirm=str(payload.get("confirm", "")))
                status = 200 if result.get("ok") else 409 if result.get("status") == "confirmation_required" else 400
                self._send(*json_response(result, status=status))
            elif self.path == "/api/stock-order-intent":
                try:
                    payload = self._read_json_body()
                except ValueError as error:
                    self._send(*json_response({"ok": False, "status": "bad_request", "message": str(error)}, status=400))
                    return
                result = service.stock_order_intent(payload.get("symbol"), payload.get("side"), payload.get("quantity"))
                status = 200 if result.get("ok") else 400
                self._send(*json_response(result, status=status))
            elif self.path == "/api/stock-order":
                try:
                    payload = self._read_json_body()
                except ValueError as error:
                    self._send(*json_response({"ok": False, "status": "bad_request", "message": str(error)}, status=400))
                    return
                result = service.stock_order(payload.get("symbol"), payload.get("side"), payload.get("quantity"), confirm=str(payload.get("confirm", "")))
                status = 200 if result.get("ok") else 409 if result.get("status") in {"confirmation_required", "live_not_allowed", "not_configured"} else 400
                self._send(*json_response(result, status=status))
            elif self.path == "/api/emergency-liquidate":
                try:
                    payload = self._read_json_body()
                except ValueError as error:
                    self._send(*json_response({"ok": False, "status": "bad_request", "message": str(error)}, status=400))
                    return
                result = service.emergency_liquidate(confirm=str(payload.get("confirm", "")))
                status = 200 if result.get("ok") else 409 if result.get("status") == "confirmation_required" else 400
                self._send(*json_response(result, status=status))
            elif self.path == "/api/reconcile-orders":
                try:
                    payload = self._read_json_body()
                except ValueError as error:
                    self._send(*json_response({"ok": False, "status": "bad_request", "message": str(error)}, status=400))
                    return
                result = service.reconcile_orders(limit=payload.get("limit", 80))
                status = 200 if result.get("ok") else 400
                self._send(*json_response(result, status=status))
            elif self.path == "/api/postmortem-apply":
                try:
                    payload = self._read_json_body()
                except ValueError as error:
                    self._send(*json_response({"ok": False, "status": "bad_request", "message": str(error)}, status=400))
                    return
                result = service.apply_loss_postmortem_report(payload.get("report_id"), reviewer_notes=payload.get("reviewer_notes", ""))
                status = 200 if result.get("ok") else 409 if result.get("status") == "stale_report" else 400
                self._send(*json_response(result, status=status))
            else:
                self._send(*json_response({"error": "not found"}, status=404))

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send(self, status: int, headers: dict[str, str], body: bytes) -> None:
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length > 16384:
                raise ValueError("Request body is too large.")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as error:
                raise ValueError("Request body must be JSON.") from error
            if not isinstance(payload, dict):
                raise ValueError("Request body must be a JSON object.")
            return payload

    return Handler


class DashboardHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], service: DashboardService, *, start_background: bool = False) -> None:
        self.service = service
        super().__init__(server_address, make_handler(service))
        if start_background:
            self.service.start_autopilot_background()

    def server_close(self) -> None:
        self.service.stop_autopilot_background()
        super().server_close()


def _alpaca_clock(client: Any) -> dict[str, Any] | None:
    if not hasattr(client, "get_clock"):
        return None
    try:
        clock = client.get_clock()
    except Exception:
        return None
    return clock if isinstance(clock, dict) else None


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json_object(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _diagnostic_check(ok: bool, message: str, recovery: str, *, warn: bool = False) -> dict[str, Any]:
    status = "pass" if ok else "warn" if warn else "fail"
    return {
        "status": status,
        "message": message if ok else recovery,
        "recovery": "" if ok else recovery,
    }


def _diagnostic_summary(checks: dict[str, dict[str, Any]]) -> dict[str, int]:
    return {
        "passed": sum(1 for check in checks.values() if check.get("status") == "pass"),
        "warn": sum(1 for check in checks.values() if check.get("status") == "warn"),
        "failed": sum(1 for check in checks.values() if check.get("status") == "fail"),
        "total": len(checks),
    }


def _risk_disclosure_present(path: Path) -> bool:
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8").lower()
    return "not financial advice" in text and "can lose money" in text


def _safe_error_message(error: Exception) -> str:
    return str(error)[:300]


def _liquidation_position(position: dict[str, Any]) -> dict[str, Any]:
    symbol = str(position.get("symbol") or "").upper()
    held_quantity = abs(_safe_float(position.get("qty")))
    raw_available = position.get("qty_available")
    available_quantity = abs(_safe_float(raw_available, held_quantity)) if raw_available not in {None, ""} else held_quantity
    quantity = round(min(held_quantity, available_quantity), 8)
    can_sell = bool(symbol and quantity > 0)
    return {
        "symbol": symbol,
        "side": "sell",
        "action": "EMERGENCY_LIQUIDATE" if can_sell else "EMERGENCY_SKIP_RESERVED",
        "quantity": quantity,
        "held_quantity": round(held_quantity, 8),
        "available_quantity": quantity,
        "status": "planned" if can_sell else "skipped",
        "reason": "Sellable position found for emergency liquidation." if can_sell else "No available shares to sell; the position may already be reserved by open orders.",
        "can_sell": can_sell,
        "review_only": True,
    }


def _project_version(path: Path) -> str:
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("version") and "=" in line:
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _read_env_presence(path: Path) -> dict[str, str]:
    keys = {
        "TELEGRAM_BOT_TOKEN": "missing",
        "ALLOWED_CHAT_IDS": "missing",
        "TRADING_MODE": "missing",
        "BONEHAWK_UI_THEME": "retro",
        "ALPACA_API_KEY": "missing",
        "ALPACA_SECRET_KEY": "missing",
        "ALPACA_PAPER": "true",
        "ALPACA_ALLOW_LIVE": "false",
        "BONEHAWK_SETUP_COMPLETE": "false",
    }
    if not path.exists():
        return keys
    for line in path.read_text().splitlines():
        if "=" not in line or line.strip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in keys:
            keys[key] = "set" if value.strip() else "blank"
            if key in {"TRADING_MODE", "BONEHAWK_UI_THEME", "ALPACA_PAPER", "ALPACA_ALLOW_LIVE", "BONEHAWK_SETUP_COMPLETE"} and value.strip():
                keys[key] = value.strip()
    return keys


def _ui_theme_from_env(env: dict[str, str]) -> str:
    theme = str(env.get("BONEHAWK_UI_THEME") or "retro").strip().lower()
    return theme if theme in UI_THEME_VALUES else "clean"


def _validate_setup_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    if not _setup_bool(payload.get("risk_acknowledged"), default=False):
        return {
            "ok": False,
            "status": "invalid_setup",
            "message": "Risk acknowledgement is required before Bonehawk setup can complete.",
        }
    for key in ("alpaca_api_key", "alpaca_secret_key", "telegram_bot_token", "allowed_chat_ids"):
        value = payload.get(key)
        if value is not None and len(str(value)) > 4096:
            return {"ok": False, "status": "invalid_setup", "message": "A setup value is too long."}
    for key in ("max_open_positions",):
        if key not in payload or payload.get(key) in {None, ""}:
            continue
        try:
            number = float(payload.get(key))
        except (TypeError, ValueError):
            return {"ok": False, "status": "invalid_setup", "message": f"{key} must be a number."}
        if key == "max_open_positions" and not 0 <= number <= 25:
            return {"ok": False, "status": "invalid_setup", "message": "Max open positions must be between 0 and 25."}
    return None


def _setup_bool(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _ticket_from_decision(row: dict[str, Any]) -> dict[str, Any] | None:
    source = str(row.get("source") or "")
    action = str(row.get("action") or "")
    ticket_sources = {"stock_order_intent", "alpaca_stock_order", "stock_order_attempt", "autopilot_order", "emergency_liquidation"}
    if source not in ticket_sources and "_INTENT" not in action and "_LIVE" not in action and "_ALPACA" not in action:
        return None
    status = str(row.get("status") or "").strip().lower()
    if not status:
        if source == "stock_order_intent" or "_INTENT" in action:
            status = "recorded"
        elif row.get("broker_order_id"):
            status = "submitted"
        else:
            status = "unknown"
    side = "SELL" if "SELL" in action.upper() else "BUY" if "BUY" in action.upper() else "ORDER"
    return {
        "timestamp": row.get("timestamp"),
        "source": source,
        "symbol": row.get("symbol"),
        "side": side,
        "action": action,
        "quantity": _ticket_quantity(row),
        "current_price": row.get("current_price"),
        "status": status,
        "broker_status": row.get("broker_status"),
        "broker_order_id": row.get("broker_order_id"),
        "filled_quantity": row.get("filled_quantity"),
        "filled_average_price": row.get("filled_average_price"),
        "fill_status": row.get("fill_status") or _ticket_signal_value(row, "fill_status"),
        "scheduled_for_market_open": row.get("scheduled_for_market_open"),
        "target_fill_time": row.get("target_fill_time"),
        "message": row.get("reason"),
        "review_only": row.get("review_only", True),
    }


def _ticket_with_alpaca_order(ticket: dict[str, Any], order: dict[str, Any]) -> dict[str, Any]:
    fill = alpaca_order_fill_snapshot(order)
    fill_status = fill["fill_status"]
    return {
        **ticket,
        "broker_status": order.get("status") or ticket.get("broker_status"),
        "filled_quantity": fill["filled_quantity"],
        "filled_average_price": fill["filled_average_price"],
        "fill_status": fill_status,
        "message": _refreshed_order_message(fill_status, ticket.get("message")),
    }


def _empty_reconciliation_report() -> dict[str, Any]:
    return {
        "status": "not_run",
        "checked_at": "",
        "summary": {"checked": 0, "refreshed": 0, "failed": 0, "terminal": 0, "active": 0},
    }


def _reconciliation_public_summary(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": report.get("status", "unknown"),
        "checked_at": report.get("checked_at", ""),
        "summary": report.get("summary") or {},
    }


def _refreshable_order_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    refreshable: list[dict[str, Any]] = []
    for event in events:
        order_id = str(event.get("broker_order_id") or "").strip()
        if not order_id or order_id in seen or bool(event.get("review_only", True)):
            continue
        stage = str(event.get("stage") or "").lower()
        fill_status = str(event.get("fill_status") or "").lower()
        broker_status = str(event.get("broker_status") or "").lower()
        if stage in {"filled", "rejected", "canceled"} or fill_status in {"filled", "rejected", "canceled", "cancelled", "expired"} or broker_status in {"filled", "rejected", "canceled", "cancelled", "expired"}:
            continue
        seen.add(order_id)
        refreshable.append(event)
    return refreshable


def _truth_row_with_alpaca_order(event: dict[str, Any], order: dict[str, Any]) -> dict[str, Any]:
    fill = alpaca_order_fill_snapshot(order)
    fill_status = fill["fill_status"]
    return {
        "symbol": order.get("symbol") or event.get("symbol"),
        "side": event.get("side") or order.get("side"),
        "action": event.get("action") or "ALPACA_ORDER_RECONCILE",
        "status": event.get("status") or "submitted",
        "broker_status": order.get("status") or event.get("broker_status"),
        "broker_order_id": order.get("id") or event.get("broker_order_id"),
        "client_order_id": order.get("client_order_id") or event.get("client_order_id"),
        "quantity": _safe_float(order.get("qty"), _safe_float(event.get("quantity"))),
        "notional": event.get("notional"),
        "current_price": event.get("current_price"),
        "filled_quantity": fill["filled_quantity"],
        "filled_average_price": fill["filled_average_price"],
        "fill_status": fill_status,
        "scheduled_for_market_open": event.get("scheduled_for_market_open"),
        "target_fill_time": event.get("target_fill_time"),
        "message": _refreshed_order_message(fill_status, event.get("reason")),
        "review_only": False,
    }


def _refreshed_order_message(fill_status: str, fallback: Any) -> str:
    if fill_status == "filled":
        return "Alpaca order filled."
    if fill_status == "partially_filled":
        return "Alpaca order partially filled."
    if fill_status in {"canceled", "cancelled", "expired", "rejected", "stopped", "suspended", "done_for_day"}:
        return f"Alpaca order is {fill_status}."
    return str(fallback or "Alpaca order status refreshed.")


def _live_order_event(ticket: dict[str, Any]) -> dict[str, Any]:
    status = str(ticket.get("status") or "unknown").lower()
    broker_status = str(ticket.get("broker_status") or "").lower()
    fill_status = str(ticket.get("fill_status") or "").lower()
    category = _live_order_category(status, broker_status, fill_status, bool(ticket.get("broker_order_id")))
    side = str(ticket.get("side") or "ORDER").upper()
    symbol = str(ticket.get("symbol") or "UNKNOWN").upper()
    return {
        "timestamp": ticket.get("timestamp"),
        "source": ticket.get("source"),
        "symbol": symbol,
        "side": side,
        "direction": side.lower() if side in {"BUY", "SELL"} else "order",
        "action": ticket.get("action"),
        "quantity": ticket.get("quantity"),
        "current_price": ticket.get("current_price"),
        "status": status,
        "broker_status": ticket.get("broker_status"),
        "broker_order_id": ticket.get("broker_order_id"),
        "filled_quantity": ticket.get("filled_quantity"),
        "filled_average_price": ticket.get("filled_average_price"),
        "fill_status": ticket.get("fill_status"),
        "scheduled_for_market_open": ticket.get("scheduled_for_market_open"),
        "target_fill_time": ticket.get("target_fill_time"),
        "message": ticket.get("message"),
        "review_only": ticket.get("review_only", True),
        "category": category,
        "compact_label": f"{symbol} {side} {category}",
        "progress_pct": _live_order_progress(category),
        "age_seconds": _event_age_seconds(ticket),
    }


def _live_order_category(status: str, broker_status: str, fill_status: str, has_order_id: bool) -> str:
    rejected_statuses = {"rejected", "not_configured", "live_not_allowed", "confirmation_required", "invalid_symbol", "invalid_side", "invalid_quantity", "invalid_size"}
    canceled_statuses = {"canceled", "cancelled", "expired", "done_for_day"}
    if fill_status == "filled" or broker_status == "filled":
        return "filled"
    if fill_status in canceled_statuses or broker_status in canceled_statuses or status in canceled_statuses:
        return "canceled"
    if status in rejected_statuses or broker_status == "rejected":
        return "rejected"
    if has_order_id or status in {"submitted", "accepted", "new"}:
        return "submitted"
    if status == "recorded":
        return "recorded"
    return "blocked" if status not in {"unknown", ""} else "unknown"


def _live_order_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    categories = {"submitted": 0, "rejected": 0, "filled": 0, "canceled": 0, "recorded": 0, "blocked": 0, "unknown": 0}
    for event in events:
        category = str(event.get("category") or "unknown")
        categories[category if category in categories else "unknown"] += 1
    categories["total"] = len(events)
    categories["flow"] = [
        {"category": category, "count": categories[category], "pct": round((categories[category] / len(events)) * 100, 1) if events else 0}
        for category in ("rejected", "canceled", "filled", "submitted", "recorded", "blocked", "unknown")
        if categories[category]
    ]
    return categories


def _live_order_progress(category: str) -> int:
    return {
        "filled": 100,
        "rejected": 100,
        "canceled": 100,
        "submitted": 62,
        "recorded": 35,
        "blocked": 18,
    }.get(category, 10)


def _event_age_seconds(ticket: dict[str, Any]) -> int | None:
    timestamp = _event_timestamp(ticket)
    if timestamp is None:
        return None
    return max(0, int((datetime.now(UTC) - timestamp).total_seconds()))


def _ticket_quantity(row: dict[str, Any]) -> float | None:
    quantity = row.get("quantity")
    if quantity is not None:
        try:
            return float(quantity)
        except (TypeError, ValueError):
            return None
    for signal in row.get("signals") or []:
        text = str(signal)
        if text.startswith("quantity "):
            try:
                return float(text.split(" ", 1)[1])
            except ValueError:
                return None
    return None


def _ticket_signal_value(row: dict[str, Any], prefix: str) -> str | None:
    needle = f"{prefix} "
    for signal in row.get("signals") or []:
        text = str(signal)
        if text.startswith(needle):
            return text.split(" ", 1)[1]
    return None


def _write_env_value(path: Path, key: str, value: str) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    updated: list[str] = []
    replaced = False
    for line in lines:
        if "=" not in line or line.strip().startswith("#"):
            updated.append(line)
            continue
        current_key, _current_value = line.split("=", 1)
        if current_key.strip() == key and not replaced:
            updated.append(f"{key}={value}")
            replaced = True
        elif current_key.strip() == key:
            continue
        else:
            updated.append(line)
    if not replaced:
        if updated and updated[-1].strip():
            updated.append("")
        updated.append(f"{key}={value}")
    path.write_text("\n".join(updated) + "\n")


def _trade_quote_symbols(scan_result: dict[str, Any], positions: list[Any], limit: int = 30) -> list[str]:
    symbols: list[str] = []
    for position in positions:
        if not position.symbol.endswith("-USD"):
            symbols.append(position.symbol)
    for scan in scan_result.get("scans", []):
        symbol = str(scan.get("symbol", "")).upper()
        if symbol and not symbol.endswith("-USD"):
            symbols.append(symbol)
        if len(dict.fromkeys(symbols)) >= limit:
            break
    return list(dict.fromkeys(symbols))[:limit]


def _watchlist_with_portfolio_positions(watchlist: Watchlist, positions: list[Position], *, replace_positions: bool = False) -> Watchlist:
    if not positions:
        return Watchlist(symbols=watchlist.symbols, positions=[] if replace_positions else watchlist.positions, risk=watchlist.risk, aliases=watchlist.aliases)
    symbols = list(dict.fromkeys([*watchlist.symbols, *[position.symbol for position in positions]]))
    return Watchlist(symbols=symbols, positions=positions, risk=watchlist.risk, aliases=watchlist.aliases)


def _public_alpaca_account(account: dict[str, Any]) -> dict[str, Any]:
    allowed = ("status", "portfolio_value", "cash", "buying_power", "equity", "currency")
    return {key: account.get(key) for key in allowed if key in account}


def _empty_portfolio_performance(source: str) -> dict[str, Any]:
    return {
        "source": source,
        "positions": [],
        "total_cost": 0,
        "total_value": 0,
        "account_value": 0,
        "cash": 0,
        "buying_power": 0,
        "unrealized_pnl": 0,
        "unrealized_pnl_pct": 0,
    }


def _first_query_value(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key, [])
    return values[0] if values else ""


def _normalize_stock_symbol(symbol: Any) -> str:
    value = str(symbol or "").strip().upper()
    if not value or len(value) > 12:
        return ""
    if not all(character.isalnum() or character in {".", "-"} for character in value):
        return ""
    return value


def _stock_order_request(symbol: Any, side: Any, quantity: Any) -> StockOrderTicket | dict[str, Any]:
    normalized_symbol = _normalize_stock_symbol(symbol)
    if not normalized_symbol:
        return {"ok": False, "status": "invalid_symbol", "message": "Choose a valid stock symbol."}
    normalized_side = str(side or "").strip().upper()
    if normalized_side not in {"BUY", "SELL"}:
        return {"ok": False, "status": "invalid_side", "message": "Choose Buy or Sell."}
    try:
        normalized_quantity = float(quantity)
    except (TypeError, ValueError):
        return {"ok": False, "status": "invalid_quantity", "message": "Quantity must be a number."}
    if normalized_quantity <= 0 or normalized_quantity > 1_000_000:
        return {"ok": False, "status": "invalid_quantity", "message": "Quantity must be greater than 0."}
    return StockOrderTicket(symbol=normalized_symbol, side=normalized_side, quantity=normalized_quantity)


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _events_within_window(events: list[dict[str, Any]], minutes: int) -> list[dict[str, Any]]:
    cutoff = datetime.now(UTC) - timedelta(minutes=minutes)
    recent = [event for event in events if (_event_timestamp(event) or datetime.min.replace(tzinfo=UTC)) >= cutoff]
    return sorted(recent, key=lambda event: _event_timestamp(event) or datetime.min.replace(tzinfo=UTC), reverse=True)


def _event_timestamp(event: dict[str, Any]) -> datetime | None:
    raw = str(event.get("timestamp") or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _background_scan_summary(scan: dict[str, Any]) -> dict[str, Any]:
    orders = scan.get("orders") or []
    blocked = scan.get("blocked") or []
    summary = scan.get("summary") or {}
    return {
        "ok": bool(scan.get("ok")),
        "status": scan.get("status", "unknown"),
        "orders": len(orders),
        "blocked": len(blocked),
        "symbols_scanned": summary.get("symbols_scanned", 0),
        "top_symbols": [str(item.get("symbol", "")).upper() for item in orders[:5] if item.get("symbol")],
    }


def _background_execution_summary(execution: dict[str, Any]) -> dict[str, Any]:
    executed = execution.get("executed") or []
    blocked = execution.get("blocked") or []
    orders = execution.get("orders") or []
    summary = execution.get("execution_summary") or {}
    submitted = int(summary.get("submitted") or len([item for item in executed if item.get("ok")]))
    rejected = int(summary.get("rejected") or len([item for item in executed if not item.get("ok")]))
    return {
        "ok": bool(execution.get("ok")),
        "status": execution.get("status", "unknown"),
        "submitted": submitted,
        "rejected": rejected,
        "planned": int(summary.get("planned") or len(orders)),
        "blocked": int(summary.get("blocked") or len(blocked)),
        "scheduled_for_market_open": len([item for item in executed if item.get("scheduled_for_market_open")]),
        "message": summary.get("message") or execution.get("message") or execution.get("notice") or "",
        "order_ids": [str(item.get("broker_order_id")) for item in executed[:5] if item.get("broker_order_id")],
    }


def _background_display_payload(scan: dict[str, Any], execution: dict[str, Any], result: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    source = execution or scan
    execution_summary = execution.get("execution_summary") or {}
    return {
        "ok": bool(execution.get("ok")),
        "status": execution.get("status", result.get("status", "unknown")),
        "mode": execution.get("mode") or scan.get("mode") or "paper",
        "config": source.get("config") or scan.get("config") or snapshot.get("config") or {},
        "broker": snapshot.get("broker") or {},
        "summary": source.get("summary") or scan.get("summary") or {},
        "market_trend": source.get("market_trend") or scan.get("market_trend") or "unknown",
        "agentic_scan": source.get("agentic_scan") or scan.get("agentic_scan") or {},
        "orders": _compact_background_rows(source.get("orders") or scan.get("orders") or [], limit=12),
        "blocked": _compact_background_rows(source.get("blocked") or scan.get("blocked") or [], limit=12),
        "executed": _compact_background_rows(execution.get("executed") or [], limit=12),
        "execution_summary": {
            "submitted": execution_summary.get("submitted", result.get("execution", {}).get("submitted", 0)),
            "rejected": execution_summary.get("rejected", result.get("execution", {}).get("rejected", 0)),
            "planned": execution_summary.get("planned", result.get("execution", {}).get("planned", 0)),
            "blocked": execution_summary.get("blocked", result.get("execution", {}).get("blocked", 0)),
            "message": execution_summary.get("message", result.get("execution", {}).get("message", "")),
        },
        "data_sources": source.get("data_sources") or scan.get("data_sources") or {},
        "telegram": source.get("telegram") or scan.get("telegram") or {},
        "market_hours": source.get("market_hours") or execution.get("market_hours") or result.get("market_hours") or {},
        "notice": source.get("notice") or scan.get("notice") or result.get("message", ""),
        "background": {
            "status": result.get("status", "unknown"),
            "started_at": result.get("started_at", ""),
            "finished_at": result.get("finished_at", ""),
            "scan_status": result.get("scan", {}).get("status", "unknown"),
            "run_status": result.get("execution", {}).get("status", "unknown"),
            "scan_orders": result.get("scan", {}).get("orders", 0),
            "submitted": result.get("execution", {}).get("submitted", 0),
            "runs": result.get("runs", 0),
        },
    }


def _compact_background_rows(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    allowed = {
        "symbol",
        "side",
        "action",
        "current_price",
        "confidence",
        "probability_up",
        "edge",
        "expected_return_pct",
        "notional",
        "quantity",
        "quantity_estimate",
        "held_quantity",
        "available_quantity",
        "stop_loss",
        "take_profit",
        "kelly_fraction",
        "profit_target_pct",
        "stop_exit_pct",
        "unrealized_pnl",
        "unrealized_pnl_pct",
        "exit_window_minutes",
        "reason",
        "signals",
        "scheduled_for_market_open",
        "target_fill_time",
        "schedule_reason",
        "status",
        "broker_status",
        "broker_order_id",
        "fill_status",
        "filled_quantity",
        "filled_average_price",
        "message",
        "detail",
        "review_only",
    }
    compacted: list[dict[str, Any]] = []
    for row in rows[:limit]:
        compacted.append({key: value for key, value in row.items() if key in allowed})
    return compacted


HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" href="data:,">
  <title>bonehawk</title>
  <style>
    :root {
      color-scheme: dark;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
      --ink: #f4f4f5;
      --muted: #9ca3af;
      --page: #0a0a0b;
      --panel: #151516;
      --panel-raised: #1a1a1c;
      --line: rgba(255,255,255,0.09);
      --line-soft: rgba(255,255,255,0.06);
      --black: #0f0f10;
      --blue: #7aa2ff;
      --green: #39d98a;
      --red: #ff5c64;
      --amber: #f6c453;
      --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
      --font-display: "Chakra Petch", "Eurostile", "Bank Gothic", system-ui, sans-serif;
      --side: 244px;
      --title: 34px;
      --radius: 6px;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background:
        linear-gradient(180deg, rgba(122,162,255,0.05), transparent 28%),
        repeating-linear-gradient(0deg, rgba(255,255,255,0.025) 0 1px, transparent 1px 4px),
        var(--page);
      color: var(--ink);
      font-family: var(--mono);
      text-shadow: 0 0 8px rgba(244,244,245,0.12);
    }
    body.theme-retro {
      --ink: #e6f2df;
      --muted: #8a9987;
      --page: #050607;
      --panel: #0e1211;
      --panel-raised: #151b18;
      --line: rgba(152,255,182,0.20);
      --line-soft: rgba(152,255,182,0.10);
      --blue: #98ffb6;
      --green: #98ffb6;
      --red: #ff5c64;
      --amber: #f3b35b;
      background:
        radial-gradient(circle at 15% 0%, rgba(152,255,182,0.15), transparent 30rem),
        radial-gradient(circle at 85% 2%, rgba(243,179,91,0.12), transparent 24rem),
        linear-gradient(135deg, #07100b, var(--page));
      text-shadow: 0 0 10px rgba(152,255,182,0.12);
    }
    body.theme-retro::after {
      content: "";
      position: fixed;
      inset: 0;
      z-index: 90;
      pointer-events: none;
      background:
        linear-gradient(rgba(255,255,255,0.025) 50%, rgba(0,0,0,0.045) 50%) 0 0 / 100% 4px,
        radial-gradient(circle at center, transparent 58%, rgba(0,0,0,0.32));
      mix-blend-mode: screen;
      opacity: 0.58;
    }
    body.theme-retro .arcade-grid {
      background:
        linear-gradient(rgba(152,255,182,0.08) 1px, transparent 1px),
        linear-gradient(90deg, rgba(152,255,182,0.05) 1px, transparent 1px);
      background-size: 36px 36px;
      mask-image: linear-gradient(to bottom, rgba(0,0,0,0.42), transparent 72%);
    }
    body.theme-retro h1,
    body.theme-retro h2,
    body.theme-retro .brand .sub {
      color: var(--green);
      text-shadow: 0 0 12px rgba(152,255,182,0.38);
    }
    body.theme-retro .brand,
    body.theme-retro .topbar,
    body.theme-retro .titlebar {
      background: linear-gradient(90deg, rgba(152,255,182,0.08), rgba(5,6,7,0.92));
    }
    body.theme-retro .terminal-mark {
      background: var(--green);
      color: #041008;
      box-shadow: 4px 4px 0 rgba(243,179,91,0.92), 0 0 22px rgba(152,255,182,0.32);
    }
    body.theme-retro button.primary,
    body.theme-retro .mode-option.active,
    body.theme-retro .range-control button.active {
      background: var(--amber);
      border-color: var(--amber);
      color: #160d02;
      box-shadow: 0 0 18px rgba(243,179,91,0.24), inset -2px -2px 0 rgba(0,0,0,0.38);
    }
    body.theme-retro .mode-option.live.active {
      background: var(--red);
      border-color: var(--red);
      color: #fff7f7;
    }
    body.theme-retro aside,
    body.theme-retro .topbar,
    body.theme-retro .metric,
    body.theme-retro .data-row,
    body.theme-retro .panel-block,
    body.theme-retro .command-card,
    body.theme-retro .chart-drawer,
    body.theme-retro .ticket-drawer,
    body.theme-retro .toast,
    body.theme-retro pre,
    body.theme-retro .search-box,
    body.theme-retro .market-state {
      border-color: rgba(152,255,182,0.24);
      box-shadow: 0 0 0 1px rgba(152,255,182,0.07), 0 20px 54px rgba(0,0,0,0.22), inset 0 0 18px rgba(152,255,182,0.025);
    }
    body.theme-retro .tab.active {
      background: rgba(152,255,182,0.09);
      color: var(--green);
      border-color: rgba(152,255,182,0.58);
      box-shadow: inset 4px 0 0 var(--amber), 0 0 18px rgba(152,255,182,0.16);
    }
    body.theme-retro .ticker strong,
    body.theme-retro .metric-value,
    body.theme-retro .symbol-link {
      color: var(--green);
    }
    body.theme-retro .pill.buy,
    body.theme-retro .pill.hold {
      background: rgba(152,255,182,0.10);
      color: var(--green);
      border-color: rgba(152,255,182,0.42);
    }
    body.theme-retro .pill.sell {
      background: rgba(255,92,100,0.10);
      color: var(--red);
      border-color: rgba(255,92,100,0.52);
    }
    body.theme-retro .pill.trim,
    body.theme-retro .pill.watch {
      background: rgba(243,179,91,0.10);
      color: var(--amber);
      border-color: rgba(243,179,91,0.52);
    }
    body.theme-arcade {
      --page: #080317;
      --panel: #160d2b;
      --panel-raised: #21123f;
      --line: rgba(255, 49, 214, 0.34);
      --line-soft: rgba(0, 229, 255, 0.2);
      --blue: #00e5ff;
      --green: #00ff9c;
      --red: #ff2f87;
      --amber: #ffe14d;
      background:
        radial-gradient(circle at 50% -18%, rgba(255,49,214,0.24), transparent 34%),
        radial-gradient(circle at 88% 10%, rgba(0,229,255,0.13), transparent 26%),
        linear-gradient(180deg, rgba(16,3,43,0.9), rgba(5,2,14,0.98)),
        var(--page);
    }
    body.theme-arcade::after {
      content: "";
      position: fixed;
      inset: 0;
      z-index: 90;
      pointer-events: none;
      background: repeating-linear-gradient(to bottom, rgba(255,255,255,0.035) 0 1px, rgba(0,0,0,0.11) 1px 3px, transparent 3px 5px);
      mix-blend-mode: soft-light;
    }
    body.theme-arcade h1, body.theme-arcade h2, body.theme-arcade .brand .sub {
      color: var(--blue);
      text-shadow: 0 0 8px rgba(0,229,255,0.72), 0 0 22px rgba(255,49,214,0.28);
    }
    body.theme-arcade .brand, body.theme-arcade .topbar {
      background: linear-gradient(90deg, rgba(255,49,214,0.12), rgba(0,229,255,0.05));
    }
    body.theme-arcade .terminal-mark {
      background: var(--red);
      color: #14031d;
      box-shadow: 4px 4px 0 var(--blue), 0 0 22px rgba(255,47,135,0.55);
    }
    body.theme-arcade aside,
    body.theme-arcade .metric,
    body.theme-arcade .data-row,
    body.theme-arcade .command-card,
    body.theme-arcade .chart-drawer,
    body.theme-arcade .ticket-drawer,
    body.theme-arcade .toast {
      box-shadow: 0 0 0 1px rgba(0,229,255,0.11), 0 0 24px rgba(255,49,214,0.13), inset 0 0 18px rgba(0,229,255,0.04);
    }
    body.theme-arcade .tab.active,
    body.theme-arcade .mode-option.active,
    body.theme-arcade .range-control button.active {
      box-shadow: 0 0 18px rgba(0,255,156,0.34), inset 0 0 0 1px rgba(255,255,255,0.18);
    }
    body.theme-algo-desk {
      --ink: #e0e0e0;
      --muted: #7aa17a;
      --page: #050505;
      --panel: #0a0a0a;
      --panel-raised: #101510;
      --line: rgba(57,255,20,0.45);
      --line-soft: rgba(57,255,20,0.24);
      --blue: #39ff14;
      --green: #00ff41;
      --red: #ff003c;
      --amber: #ffbf00;
      background:
        linear-gradient(180deg, rgba(57,255,20,0.06), rgba(5,5,5,0.96) 34%),
        radial-gradient(circle at 82% 8%, rgba(255,191,0,0.12), transparent 22%),
        #050505;
      text-shadow: 0 0 8px rgba(57,255,20,0.26);
    }
    body.theme-algo-desk::after {
      content: "";
      position: fixed;
      inset: 0;
      z-index: 90;
      pointer-events: none;
      background:
        repeating-linear-gradient(to bottom, rgba(255,255,255,0.05) 0 1px, rgba(0,0,0,0.24) 1px 3px, transparent 3px 6px),
        linear-gradient(90deg, rgba(255,0,60,0.035), transparent 18%, rgba(0,255,65,0.025) 52%, transparent 76%, rgba(0,40,255,0.035));
      mix-blend-mode: screen;
      opacity: 0.38;
    }
    body.theme-algo-desk .arcade-grid {
      background:
        linear-gradient(rgba(57,255,20,0.13) 1px, transparent 1px),
        linear-gradient(90deg, rgba(57,255,20,0.09) 1px, transparent 1px);
      background-size: 32px 32px;
      mask-image: linear-gradient(to bottom, rgba(0,0,0,0.52), transparent 78%);
    }
    body.theme-algo-desk h1,
    body.theme-algo-desk h2,
    body.theme-algo-desk .brand .sub {
      color: var(--green);
      text-shadow: 0 0 8px rgba(0,255,65,0.82), 0 0 22px rgba(57,255,20,0.28);
    }
    body.theme-algo-desk .brand,
    body.theme-algo-desk .topbar {
      background: linear-gradient(90deg, rgba(57,255,20,0.11), rgba(0,0,0,0.78));
    }
    body.theme-algo-desk .terminal-mark {
      background: var(--green);
      color: #050505;
      box-shadow: 4px 4px 0 var(--red), 0 0 18px rgba(57,255,20,0.72);
    }
    body.theme-algo-desk button.primary,
    body.theme-algo-desk .mode-option.active,
    body.theme-algo-desk .range-control button.active {
      background: var(--green);
      border-color: var(--green);
      color: #050505;
      box-shadow: 0 0 18px rgba(57,255,20,0.45), inset -2px -2px 0 rgba(0,0,0,0.45);
    }
    body.theme-algo-desk aside,
    body.theme-algo-desk .metric,
    body.theme-algo-desk .data-row,
    body.theme-algo-desk .command-card,
    body.theme-algo-desk .chart-drawer,
    body.theme-algo-desk .ticket-drawer,
    body.theme-algo-desk .toast,
    body.theme-algo-desk pre {
      border-color: rgba(57,255,20,0.5);
      box-shadow: 0 0 0 1px rgba(57,255,20,0.18), 0 0 24px rgba(57,255,20,0.13), inset 0 0 20px rgba(57,255,20,0.05);
    }
    body.theme-algo-desk .tab.active {
      background: rgba(57,255,20,0.13);
      color: var(--green);
      border-color: var(--green);
      box-shadow: inset 4px 0 0 var(--amber), 0 0 18px rgba(57,255,20,0.22);
    }
    body.theme-algo-desk .ticker strong,
    body.theme-algo-desk .metric-value,
    body.theme-algo-desk .symbol-link {
      color: var(--green);
    }
    body.theme-algo-desk .pill.buy,
    body.theme-algo-desk .pill.hold {
      background: rgba(0,255,65,0.1);
      color: var(--green);
      border-color: rgba(0,255,65,0.58);
    }
    body.theme-algo-desk .pill.sell {
      background: rgba(255,0,60,0.1);
      color: var(--red);
      border-color: rgba(255,0,60,0.62);
    }
    body.theme-algo-desk .pill.trim,
    body.theme-algo-desk .pill.watch {
      background: rgba(255,191,0,0.1);
      color: var(--amber);
      border-color: rgba(255,191,0,0.62);
    }
    body.theme-classic {
      --ink: #f4f4f5;
      --muted: #9ca3af;
      --page: #0a0a0b;
      --panel: #151516;
      --panel-raised: #1a1a1c;
      --line: rgba(255,255,255,0.09);
      --line-soft: rgba(255,255,255,0.06);
      --blue: #7aa2ff;
      --green: #39d98a;
      --red: #ff5c64;
      --amber: #f6c453;
    }
    body.theme-clean {
      --ink: #f5f7fb;
      --muted: #9aa4b2;
      --page: #090b10;
      --panel: #11151c;
      --panel-raised: #171c24;
      --line: rgba(255,255,255,0.10);
      --line-soft: rgba(255,255,255,0.06);
      --blue: #8fb4ff;
      --green: #42d88b;
      --red: #f87171;
      --amber: #fbbf24;
      background:
        linear-gradient(180deg, rgba(143,180,255,0.06), transparent 240px),
        var(--page);
      text-shadow: none;
    }
    body.theme-clean .arcade-grid {
      display: none;
    }
    body.theme-clean aside,
    body.theme-clean .topbar,
    body.theme-clean .metric,
    body.theme-clean .data-row,
    body.theme-clean .command-card,
    body.theme-clean .chart-drawer,
    body.theme-clean .ticket-drawer,
    body.theme-clean .toast,
    body.theme-clean pre,
    body.theme-clean .search-box,
    body.theme-clean .market-state {
      box-shadow: none;
    }
    body.theme-clean .brand,
    body.theme-clean .topbar {
      background: rgba(9,11,16,0.9);
    }
    body.theme-clean .terminal-mark {
      background: var(--blue);
      color: #07111f;
      box-shadow: none;
    }
    body.theme-clean .tab.active {
      background: rgba(143,180,255,0.11);
      border-color: rgba(143,180,255,0.48);
      box-shadow: inset 3px 0 0 var(--green);
    }
    body.theme-clean button {
      box-shadow: none;
    }
    body.theme-clean button.primary,
    body.theme-clean .mode-option.active,
    body.theme-clean .range-control button.active {
      background: var(--blue);
      border-color: var(--blue);
      color: #07111f;
    }
    body.theme-clean .mode-option.live.active {
      background: var(--red);
      border-color: var(--red);
      color: #fff;
    }
    body.menu-open { overflow: hidden; }
    .arcade-grid {
      position: fixed;
      inset: 0;
      pointer-events: none;
      z-index: 0;
      background:
        linear-gradient(rgba(57,217,138,0.08) 1px, transparent 1px),
        linear-gradient(90deg, rgba(122,162,255,0.07) 1px, transparent 1px);
      background-size: 44px 44px;
      mask-image: linear-gradient(to bottom, rgba(0,0,0,0.2), transparent 56%);
    }
    .app-shell { position: relative; z-index: 1; min-height: 100vh; display: grid; grid-template-columns: var(--side) minmax(0, 1fr); grid-template-rows: var(--title) minmax(0, calc(100vh - var(--title))); }
    .titlebar { grid-column: 1 / -1; display: flex; align-items: center; justify-content: space-between; height: var(--title); padding: 0 12px 0 14px; border-bottom: 1px solid var(--line); background: rgba(5,6,7,0.9); color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; font-size: 11px; }
    .window-dots { display: flex; gap: 8px; align-items: center; }
    .dot { width: 10px; height: 10px; border: 1px solid var(--line); border-radius: 50%; background: var(--panel); }
    .dot:nth-child(1) { background: color-mix(in srgb, var(--red) 42%, var(--panel)); }
    .dot:nth-child(2) { background: color-mix(in srgb, var(--amber) 42%, var(--panel)); }
    .dot:nth-child(3) { background: color-mix(in srgb, var(--green) 38%, var(--panel)); }
    .titlebar strong { color: var(--ink); font-family: var(--font-display); font-weight: 650; letter-spacing: 0.12em; }
    aside { grid-column: 1; grid-row: 2; min-height: 0; padding: 18px 12px; background: linear-gradient(180deg, rgba(14,18,17,0.96), rgba(5,6,7,0.94)), repeating-linear-gradient(0deg, transparent 0 15px, rgba(255,255,255,0.02) 15px 16px); color: #ffffff; border-right: 1px solid var(--line); display: flex; flex-direction: column; overflow: auto; box-shadow: none; }
    body.sidebar-collapsed .app-shell { grid-template-columns: 1fr; }
    body.sidebar-collapsed aside { position: fixed; inset: var(--title) auto 0 0; width: var(--side); z-index: 70; transform: translateX(-104%); transition: transform 160ms ease; }
    body.sidebar-collapsed.menu-open aside { transform: translateX(0); }
    .sidebar-backdrop { position: fixed; inset: 0; z-index: 65; background: rgba(0,0,0,0.56); display: none; }
    body.menu-open .sidebar-backdrop { display: block; }
    main { grid-column: 2; grid-row: 2; min-width: 0; min-height: 0; padding: 0; display: grid; grid-template-rows: 58px minmax(0, 1fr); background: linear-gradient(rgba(255,255,255,0.018) 1px, transparent 1px) 0 0 / 28px 28px, linear-gradient(90deg, rgba(255,255,255,0.014) 1px, transparent 1px) 0 0 / 28px 28px, transparent; }
    body.sidebar-collapsed main { grid-column: 1; }
    h1 { font-family: var(--font-display); font-size: clamp(34px, 4vw, 52px); line-height: 1; margin: 0; text-transform: uppercase; letter-spacing: 0; }
    h2 { font-size: 13px; margin: 0; text-transform: uppercase; color: var(--muted); letter-spacing: 0.08em; }
    h3 { font-size: 13px; margin: 0; }
    button { min-height: 34px; border: 1px solid var(--line); background: var(--panel-raised); color: var(--ink); border-radius: var(--radius); padding: 0 12px; cursor: pointer; font-family: var(--mono); font-weight: 850; text-transform: uppercase; letter-spacing: 0.07em; box-shadow: inset -2px -2px 0 rgba(0,0,0,0.28), inset 2px 2px 0 rgba(255,255,255,0.035); transition: 140ms ease; }
    button:hover { border-color: rgba(57,217,138,0.5); color: var(--green); }
    button:disabled { cursor: wait; opacity: 0.62; }
    button.primary { background: #f4f4f5; color: #111113; border-color: #f4f4f5; }
    a { color: var(--blue); text-decoration: none; }
    pre { white-space: pre-wrap; overflow-wrap: anywhere; background: #0d0d0e; color: #e5e7eb; border: 2px solid var(--line); border-radius: 3px; padding: 14px; margin: 10px 0 0; font-size: 12px; line-height: 1.5; }
    .brand { min-height: 52px; display: grid; grid-template-columns: 34px minmax(0, 1fr); align-items: center; gap: 10px; border-bottom: 1px solid var(--line); padding: 0 6px 18px; }
    .brand-main { display: flex; align-items: center; gap: 10px; min-width: 0; flex: 1; }
    .brand h1 { font-size: 19px; line-height: 1; letter-spacing: 0.06em; }
    .brand-copy { min-width: 0; }
    .menu-pin { min-height: 26px; min-width: 42px; padding: 0 7px; font-size: 10px; }
    .terminal-mark { width: 34px; height: 34px; display: grid; place-items: center; border-radius: 0; border: 1px solid var(--line); background: var(--panel-raised); color: var(--green); font-family: var(--font-display); font-weight: 900; box-shadow: inset 0 0 18px rgba(152,255,182,0.14); }
    .brand .sub { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; margin-top: 5px; }
    .nav-section { margin-top: 18px; }
    .nav-label { margin: 0 6px 8px; color: var(--muted); opacity: 0.7; font-size: 10px; letter-spacing: 0.09em; text-transform: uppercase; }
    .nav { display: grid; gap: 5px; padding: 0; }
    .tab { width: 100%; min-height: 36px; display: grid; grid-template-columns: 22px minmax(0, 1fr) auto; align-items: center; gap: 9px; padding: 0 9px; text-align: left; background: transparent; color: var(--muted); border-color: transparent; box-shadow: none; }
    .tab:hover, .tab.active { border-color: var(--line); color: var(--ink); background: rgba(255,255,255,0.04); }
    .tab.active { box-shadow: inset 3px 0 0 var(--green); }
    .nav-glyph { width: 18px; height: 18px; display: grid; place-items: center; border: 1px solid var(--line); color: var(--muted); font-size: 9px; line-height: 1; }
    .tab.active .nav-glyph { border-color: var(--green); color: var(--green); }
    .nav-kbd { color: var(--muted); font-size: 10px; letter-spacing: 0.05em; }
    .rail-status { margin-top: auto; border-top: 1px solid var(--line); padding: 14px 6px 0; display: grid; gap: 8px; color: var(--muted); font-size: 12px; }
    .topbar { min-height: 58px; z-index: 20; display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 0 22px; background: rgba(14,18,17,0.72); border-bottom: 1px solid var(--line); backdrop-filter: blur(16px); }
    .toolbar, .top-actions { display: flex; flex-wrap: wrap; gap: 8px; justify-content: flex-end; align-items: center; }
    .status-strip { display: flex; align-items: center; gap: 10px; min-width: 0; flex-wrap: wrap; }
    .status { display: inline-flex; align-items: center; gap: 7px; min-height: 28px; padding: 0 10px; border: 1px solid var(--line); border-radius: 999px; background: rgba(5,6,7,0.45); color: var(--muted); font-size: 11px; letter-spacing: 0.03em; white-space: nowrap; }
    .status b { color: var(--ink); font-weight: 550; }
    .led { width: 7px; height: 7px; border-radius: 50%; background: var(--green); box-shadow: 0 0 14px rgba(152,255,182,0.65); }
    .led.warn { background: var(--amber); box-shadow: 0 0 14px rgba(243,179,91,0.55); }
    .btn { min-height: 34px; display: inline-flex; align-items: center; justify-content: center; gap: 8px; padding: 0 12px; }
    .btn.danger { border-color: rgba(255,92,100,0.52); color: color-mix(in srgb, var(--red) 84%, white); }
    .btn.icon { width: 34px; padding: 0; }
    .menu-toggle { min-width: 40px; padding: 0 10px; }
    .search-box { height: 36px; border: 2px solid var(--line); background: var(--panel); color: var(--ink); border-radius: 3px; display: flex; align-items: center; padding: 0 12px; gap: 8px; box-shadow: inset 0 0 0 1px rgba(255,255,255,0.03); }
    .search-box input { width: 100%; background: transparent; border: 0; outline: 0; color: var(--ink); font-family: var(--mono); font-size: 13px; }
    .view-heading { min-width: 0; }
    .market-state { border: 1px solid var(--line); background: rgba(14,18,17,0.66); border-radius: var(--radius); padding: 8px 13px; font-size: 12px; color: var(--muted); margin-bottom: 10px; }
    .status-line { min-height: 22px; font-size: 12px; color: var(--muted); }
    .workspace { min-height: 0; overflow: auto; padding: 20px 22px 26px; }
    .page-head { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 18px; align-items: end; margin-bottom: 16px; }
    .head-tools { display: grid; gap: 8px; justify-items: end; min-width: min(430px, 42vw); }
    .eyebrow { margin-bottom: 8px; color: var(--amber); font-size: 11px; text-transform: uppercase; letter-spacing: 0.09em; }
    .subtitle { max-width: 76ch; margin: 10px 0 0; color: var(--muted); line-height: 1.55; }
    .command-line { display: flex; align-items: center; width: 100%; min-width: 0; height: 38px; padding: 0 12px; border: 1px solid var(--line); border-radius: var(--radius); background: rgba(14,18,17,0.82); color: var(--muted); font-size: 12px; }
    .command-line span { color: var(--green); margin-right: 8px; }
    .command-line code { color: var(--muted); font-family: var(--mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .ticker-tape { display: flex; gap: 24px; overflow-x: auto; border-bottom: 1px solid var(--line); padding: 10px 0; margin-bottom: 22px; scrollbar-width: none; }
    .ticker-tape::-webkit-scrollbar { display: none; }
    .ticker { display: inline-flex; align-items: center; gap: 8px; flex: 0 0 auto; font-family: var(--mono); font-size: 12px; }
    .ticker strong { color: var(--ink); }
    .tab-panel { display: none; }
    .tab-panel.active { display: grid; gap: 14px; }
    .overview-flow { display: grid; gap: 16px; }
    .overview-compact { display: grid; gap: 14px; }
    .risk-news-rail { display: grid; gap: 14px; align-content: start; }
    .overview-actions { justify-content: flex-start; }
    .section-head { display: flex; align-items: end; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
    .metric-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
    .metric { background: linear-gradient(180deg, rgba(21,27,24,0.92), rgba(14,18,17,0.92)); border: 1px solid var(--line); border-radius: var(--radius); padding: 14px; min-height: 112px; display: flex; flex-direction: column; justify-content: space-between; box-shadow: inset 0 1px 0 rgba(255,255,255,0.035); }
    .metric-label, .panel-title, th { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; }
    .metric-value { font-family: var(--font-display); font-size: 30px; line-height: 1; letter-spacing: 0; font-weight: 760; margin-top: 8px; overflow-wrap: anywhere; }
    .metric-note { color: var(--muted); opacity: 0.72; font-size: 12px; margin-top: 8px; line-height: 1.4; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .data-list { display: grid; gap: 8px; }
    .data-row { background: rgba(5,6,7,0.38); border: 1px solid var(--line); border-radius: var(--radius); padding: 11px 12px; display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 14px; align-items: center; transition: background 140ms ease, border-color 140ms ease; }
    .data-row:hover { background: var(--panel-raised); border-color: rgba(255,255,255,0.14); }
    .data-title { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; font-weight: 720; min-width: 0; }
    .data-sub { color: var(--muted); font-size: 12px; margin-top: 4px; overflow-wrap: anywhere; }
    .right-stack { display: grid; justify-items: end; gap: 5px; white-space: nowrap; }
    .live-grid { display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(280px, 0.65fr); gap: 12px; align-items: start; }
    .live-mini-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
    .live-mini-card { min-height: 72px; padding: 10px; border: 1px solid var(--line); border-radius: var(--radius); background: rgba(5,6,7,0.42); display: grid; align-content: space-between; }
    .live-mini-label { color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: 0.08em; }
    .live-mini-value { font-family: var(--font-display); font-size: 24px; line-height: 1; letter-spacing: 0; }
    .live-flow { min-height: 40px; display: flex; align-items: stretch; gap: 5px; padding: 5px; border: 1px solid var(--line); border-radius: var(--radius); background: rgba(5,6,7,0.38); overflow: hidden; }
    .flow-segment { min-width: 46px; flex-basis: var(--w); flex-grow: 1; display: grid; place-items: center; border: 1px solid var(--line); border-radius: 2px; color: var(--ink); font-size: 10px; font-weight: 900; text-transform: uppercase; letter-spacing: 0.06em; }
    .flow-segment.filled, .flow-segment.submitted, .flow-segment.recorded { background: rgba(57,217,138,0.15); border-color: rgba(57,217,138,0.36); color: var(--green); }
    .flow-segment.rejected { background: rgba(255,92,100,0.14); border-color: rgba(255,92,100,0.38); color: var(--red); }
    .flow-segment.blocked { background: rgba(246,196,83,0.14); border-color: rgba(246,196,83,0.36); color: var(--amber); }
    .live-event-stack, .live-side-stack { display: grid; gap: 7px; }
    .live-event { min-height: 54px; display: grid; grid-template-columns: 8px minmax(0, 1fr) auto; gap: 10px; align-items: center; padding: 8px 9px; border: 1px solid var(--line); border-radius: var(--radius); background: rgba(5,6,7,0.36); }
    .live-event.buy { border-color: rgba(57,217,138,0.22); }
    .live-event.sell { border-color: rgba(255,92,100,0.24); }
    .live-event-rail { align-self: stretch; border-radius: 999px; background: var(--muted); box-shadow: 0 0 12px rgba(255,255,255,0.08); }
    .live-event.buy .live-event-rail { background: var(--green); box-shadow: 0 0 13px rgba(152,255,182,0.42); }
    .live-event.sell .live-event-rail { background: var(--red); box-shadow: 0 0 13px rgba(255,92,100,0.38); }
    .live-event-main { min-width: 0; display: grid; gap: 5px; }
    .live-event-title { display: flex; flex-wrap: wrap; gap: 7px; align-items: center; font-weight: 850; }
    .live-event-detail { color: var(--muted); font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .live-event-meta { display: grid; justify-items: end; gap: 5px; min-width: 112px; }
    .live-progress { width: 96px; height: 6px; border: 1px solid var(--line); border-radius: 999px; overflow: hidden; background: rgba(0,0,0,0.38); }
    .live-progress span { display: block; width: var(--w); height: 100%; background: var(--green); }
    .live-event.sell .live-progress span, .live-event.rejected .live-progress span { background: var(--red); }
    .live-side-card { min-height: 58px; padding: 10px; border: 1px solid var(--line); border-radius: var(--radius); background: rgba(5,6,7,0.38); display: grid; gap: 6px; }
    .live-side-card b { color: var(--ink); font-size: 13px; }
    .pill { display: inline-flex; align-items: center; min-height: 24px; border-radius: 999px; padding: 2px 8px; background: rgba(5,6,7,0.48); color: var(--muted); border: 1px solid var(--line); font-family: var(--mono); font-size: 10px; font-weight: 800; text-transform: uppercase; letter-spacing: 0.07em; white-space: nowrap; }
    .pill.buy, .pill.hold { background: rgba(57,217,138,0.12); color: var(--green); border-color: rgba(57,217,138,0.3); }
    .pill.sell { background: rgba(255,92,100,0.12); color: var(--red); border-color: rgba(255,92,100,0.32); }
    .pill.trim, .pill.watch { background: rgba(246,196,83,0.12); color: var(--amber); border-color: rgba(246,196,83,0.34); }
    .pill.quiet, .pill.no { background: rgba(255,255,255,0.04); color: var(--muted); }
    .mode-switch { display: inline-grid; grid-template-columns: repeat(var(--switch-count, 2), minmax(68px, 1fr)); border: 2px solid var(--line); border-radius: 3px; background: #0d0d0e; padding: 3px; gap: 3px; }
    .mode-option { min-height: 28px; min-width: 68px; border-color: transparent; background: transparent; color: var(--muted); box-shadow: none; }
    .mode-option.active { background: var(--green); color: #07130c; border-color: var(--green); }
    .mode-option.live.active { background: var(--red); color: #fff7f7; border-color: var(--red); }
    .scorebar { width: 126px; height: 6px; background: #2a2a2c; border-radius: 999px; overflow: hidden; }
    .scorebar span { display: block; height: 100%; background: var(--green); border-radius: inherit; }
    .positive { color: var(--green); }
    .negative { color: var(--red); }
    .muted { color: var(--muted); font-size: 12px; }
    .ok { color: var(--green); }
    .error { color: var(--red); }
    .two-col { display: grid; grid-template-columns: minmax(0, 1fr) minmax(320px, 0.55fr); gap: 12px; align-items: start; }
    .desk-grid { display: grid; grid-template-columns: minmax(0, 1.24fr) minmax(340px, 0.76fr); gap: 12px; }
    .panel-block { min-width: 0; display: grid; gap: 10px; padding: 14px; border: 1px solid var(--line); border-radius: var(--radius); background: linear-gradient(180deg, rgba(21,27,24,0.88), rgba(14,18,17,0.88)); box-shadow: inset 0 1px 0 rgba(255,255,255,0.035); }
    .panel-block h2 { color: var(--ink); font-weight: 650; }
    .panel-sub { margin-top: 4px; color: var(--muted); opacity: 0.72; font-size: 12px; line-height: 1.4; }
    .risk-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
    .risk-cell { min-height: 70px; padding: 10px; border: 1px solid var(--line); border-radius: var(--radius); background: rgba(5,6,7,0.40); }
    .risk-cell span { display: block; color: var(--muted); opacity: 0.72; font-size: 10px; text-transform: uppercase; letter-spacing: 0.08em; }
    .risk-cell b { display: block; margin-top: 9px; color: var(--ink); font-family: var(--font-display); font-size: 20px; letter-spacing: 0; }
    .readiness { display: grid; gap: 8px; }
    .check { display: grid; grid-template-columns: 18px minmax(0, 1fr) auto; align-items: center; gap: 8px; min-height: 38px; padding: 0 10px; border: 1px solid var(--line); border-radius: var(--radius); background: rgba(5,6,7,0.42); color: var(--muted); font-size: 12px; }
    .check::before { content: ""; width: 8px; height: 8px; border-radius: 50%; background: var(--green); box-shadow: 0 0 11px rgba(152,255,182,0.50); }
    .check.warn::before { background: var(--amber); box-shadow: 0 0 11px rgba(243,179,91,0.48); }
    .check b { color: var(--ink); font-weight: 550; }
    .empty { padding: 18px; color: var(--muted); background: var(--panel); border: 2px dashed var(--line); border-radius: 3px; }
    .symbol-cloud { display: flex; flex-wrap: wrap; gap: 6px; align-items: flex-start; }
    .symbol-chip { min-width: 58px; border: 2px solid var(--line); background: var(--panel); border-radius: 3px; padding: 6px 8px; font-family: var(--mono); font-size: 11px; font-weight: 800; text-align: center; }
    .symbol-link { min-height: 24px; border: 0; background: transparent; color: var(--ink); padding: 0; box-shadow: none; text-decoration: underline; text-decoration-color: rgba(57,217,138,0.45); text-underline-offset: 4px; }
    .symbol-link:hover { color: var(--green); border: 0; }
    .stock-controls { display: inline-flex; align-items: center; flex-wrap: wrap; gap: 6px; }
    .stock-actions { display: inline-flex; gap: 4px; align-items: center; }
    .trade-btn { min-height: 23px; min-width: 38px; padding: 0 6px; font-size: 10px; border-width: 1px; }
    .trade-btn.buy { color: var(--green); border-color: rgba(57,217,138,0.45); }
    .trade-btn.sell { color: var(--red); border-color: rgba(255,92,100,0.45); }
    .chart-drawer { position: fixed; right: 22px; bottom: 22px; z-index: 40; width: min(680px, calc(100vw - 44px)); max-height: calc(100vh - 44px); overflow: auto; background: rgba(21,21,22,0.98); border: 2px solid rgba(122,162,255,0.65); border-radius: 3px; box-shadow: 0 20px 70px rgba(0,0,0,0.55), 0 0 28px rgba(57,217,138,0.14); padding: 14px; display: grid; gap: 12px; }
    .chart-drawer[hidden] { display: none; }
    .ticket-drawer { position: fixed; left: 262px; bottom: 22px; z-index: 41; width: min(360px, calc(100vw - 44px)); background: rgba(21,21,22,0.98); border: 2px solid rgba(57,217,138,0.58); border-radius: 3px; box-shadow: 0 18px 54px rgba(0,0,0,0.48); padding: 14px; display: grid; gap: 12px; }
    .ticket-drawer[hidden] { display: none; }
    .ticket-form { display: grid; gap: 9px; }
    .ticket-form label { display: grid; gap: 5px; color: var(--muted); font-size: 11px; text-transform: uppercase; }
    .ticket-form input { height: 36px; border: 2px solid var(--line); background: #0d0d0e; color: var(--ink); border-radius: 3px; padding: 0 10px; font-family: var(--mono); }
    .ticket-actions { display: flex; gap: 8px; justify-content: flex-end; }
    .toast-stack { position: fixed; top: 18px; right: 18px; z-index: 80; display: grid; gap: 10px; width: min(420px, calc(100vw - 36px)); pointer-events: none; }
    .toast { pointer-events: auto; background: rgba(21,21,22,0.98); border: 2px solid var(--line); border-radius: 3px; padding: 12px; display: grid; gap: 7px; box-shadow: 0 18px 44px rgba(0,0,0,0.45), inset -2px -2px 0 rgba(0,0,0,0.28); animation: toast-in 180ms ease-out; }
    .toast.ok { border-color: rgba(57,217,138,0.62); }
    .toast.error { border-color: rgba(255,92,100,0.7); }
    .toast.warn { border-color: rgba(246,196,83,0.7); }
    .toast-head { display: flex; justify-content: space-between; gap: 10px; align-items: start; }
    .toast-title { font-size: 12px; font-weight: 900; text-transform: uppercase; }
    .toast-body { color: var(--muted); font-size: 12px; line-height: 1.45; overflow-wrap: anywhere; }
    .toast-close { min-height: 22px; min-width: 26px; padding: 0 6px; border-width: 1px; box-shadow: none; }
    @keyframes toast-in { from { opacity: 0; transform: translateY(-8px); } to { opacity: 1; transform: translateY(0); } }
    .setup-modal { position: fixed; inset: 0; z-index: 120; display: grid; place-items: center; padding: 20px; background: rgba(2,2,8,0.78); backdrop-filter: blur(8px); }
    .setup-modal[hidden] { display: none; }
    .setup-card { width: min(760px, 100%); max-height: calc(100vh - 40px); overflow: auto; background: rgba(21,21,22,0.98); border: 2px solid rgba(0,229,255,0.48); border-radius: 3px; box-shadow: 0 24px 80px rgba(0,0,0,0.62), 0 0 34px rgba(255,49,214,0.16); padding: 18px; display: grid; gap: 14px; }
    .setup-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
    .setup-field { display: grid; gap: 5px; color: var(--muted); font-size: 11px; text-transform: uppercase; }
    .setup-field input { height: 36px; border: 2px solid var(--line); background: #0d0d0e; color: var(--ink); border-radius: 3px; padding: 0 10px; font-family: var(--mono); }
    .setup-field.full { grid-column: 1 / -1; }
    .setup-steps { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
    .setup-step { border: 2px solid var(--line-soft); background: rgba(255,255,255,0.03); border-radius: 3px; padding: 10px; min-height: 78px; }
    .setup-actions { display: flex; flex-wrap: wrap; gap: 8px; justify-content: flex-end; }
    .chart-head { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 12px; align-items: start; }
    .chart-title { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
    .range-control { display: flex; flex-wrap: wrap; gap: 6px; }
    .range-control button { min-height: 28px; min-width: 50px; padding: 0 8px; }
    .range-control button.active { background: var(--green); border-color: var(--green); color: #07130c; }
    .chart-canvas-wrap { position: relative; background: #0d0d0e; border: 2px solid var(--line); border-radius: 3px; padding: 10px; }
    #stock-chart-canvas { width: 100%; height: 280px; display: block; }
    .chart-tooltip { position: absolute; z-index: 5; min-width: 142px; pointer-events: none; background: rgba(10,10,11,0.96); color: var(--ink); border: 2px solid rgba(57,217,138,0.62); border-radius: 3px; padding: 8px 9px; font-size: 11px; line-height: 1.45; box-shadow: 0 12px 34px rgba(0,0,0,0.45); transform: translate(-50%, calc(-100% - 12px)); }
    .chart-tooltip[hidden] { display: none; }
    .chart-stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
    .chart-stat { border: 2px solid var(--line-soft); background: rgba(255,255,255,0.03); padding: 9px; border-radius: 3px; }
    .command-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
    .settings-command-groups { display: grid; gap: 12px; }
    .settings-command-groups .command-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .settings-command-groups .command-card { min-height: 132px; }
    .command-section { display: grid; gap: 8px; }
    .command-card { background: var(--panel); border: 2px solid var(--line); border-radius: 3px; padding: 14px; display: grid; gap: 10px; align-content: space-between; min-height: 176px; }
    .command-card.danger { border-color: rgba(255,92,100,0.38); }
    .command-code { color: var(--muted); font-size: 11px; line-height: 1.45; overflow-wrap: anywhere; }
    .command-actions { display: flex; justify-content: space-between; gap: 8px; align-items: center; }
    .command-card button { min-width: 82px; }
    .tabs { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
    .loop-strip { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 12px; align-items: center; }
    .loop-actions { display: flex; flex-wrap: wrap; gap: 8px; justify-content: flex-end; align-items: center; }
    .agent-grid, .pipeline { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
    .agent-node { min-height: 128px; padding: 11px; border: 1px solid var(--line); border-radius: var(--radius); background: rgba(5,6,7,0.38); display: grid; gap: 8px; align-content: start; }
    .agent-node h2 { color: var(--ink); font-weight: 650; }
    .bar { height: 5px; margin-top: 12px; border: 1px solid var(--line); background: rgba(0,0,0,0.32); overflow: hidden; }
    .bar > i { display: block; height: 100%; width: var(--w); background: linear-gradient(90deg, var(--green), var(--amber)); }
    .opportunity-table { overflow: auto; border: 1px solid var(--line); border-radius: var(--radius); }
    .opportunity-table table { width: 100%; min-width: 900px; border-collapse: collapse; font-variant-numeric: tabular-nums; }
    .opportunity-table th, .opportunity-table td { padding: 10px 11px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: middle; }
    .opportunity-table th { position: sticky; top: 0; z-index: 1; background: rgba(14,18,17,0.98); }
    .opportunity-table td { color: var(--muted); font-size: 12px; }
    .opportunity-table tr:last-child td { border-bottom: 0; }
    .opportunity-table .market { max-width: 310px; color: var(--ink); font-weight: 560; line-height: 1.35; }
    .opportunity-table .num { color: var(--ink); text-align: right; white-space: nowrap; }
    @media (max-width: 1180px) {
      :root { --side: 82px; }
      .brand { grid-template-columns: 1fr; justify-items: center; padding-inline: 0; }
      .brand-copy, .nav-label, .nav-text, .nav-kbd, .menu-pin, .rail-status { display: none; }
      .tab { grid-template-columns: 1fr; justify-items: center; padding: 0; }
      .nav-glyph { width: 28px; height: 28px; font-size: 10px; }
      .metric-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .desk-grid, .two-col, .live-grid { grid-template-columns: 1fr; }
      .agent-grid, .pipeline { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .command-grid, .settings-command-groups .command-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .page-head { grid-template-columns: 1fr; }
      .head-tools { justify-items: stretch; min-width: 0; }
    }
    @media (max-width: 760px) {
      body { overflow: auto; }
      .app-shell { display: block; min-height: 100vh; }
      .titlebar { position: sticky; top: 0; z-index: 75; }
      aside { position: fixed; inset: var(--title) auto 0 0; width: 244px; z-index: 70; transform: translateX(-104%); transition: transform 160ms ease; }
      body.menu-open aside { transform: translateX(0); }
      body.sidebar-collapsed aside { width: 244px; }
      main { min-height: calc(100vh - var(--title)); display: grid; grid-template-rows: auto minmax(0, 1fr); }
      .topbar { align-items: stretch; flex-direction: column; padding: 12px; }
      .status-strip, .top-actions, .toolbar, .tabs { justify-content: flex-start; }
      .workspace { padding: 14px; overflow: visible; }
      .page-head { gap: 12px; }
      h1 { font-size: 36px; }
      .metric-grid, .agent-grid, .pipeline, .risk-grid, .command-grid, .settings-command-groups .command-grid, .live-mini-grid { grid-template-columns: 1fr; }
      .data-row { grid-template-columns: 1fr; }
      .live-event { grid-template-columns: 7px minmax(0, 1fr); }
      .live-event-meta { grid-column: 2; justify-items: start; min-width: 0; }
      .live-event-detail { white-space: normal; }
      .right-stack { justify-items: start; white-space: normal; }
      .chart-stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .ticket-drawer { left: 14px; right: 14px; bottom: 14px; width: auto; }
      .chart-drawer { left: 14px; right: 14px; bottom: 14px; width: auto; }
      .toast-stack { left: 14px; right: 14px; width: auto; }
      .setup-grid, .setup-steps { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body data-theme="retro" class="theme-retro">
  <div class="arcade-grid" aria-hidden="true"></div>
  <div class="sidebar-backdrop" onclick="closeSidebar()" aria-hidden="true"></div>
  <div class="app-shell">
    <header class="titlebar">
      <div class="window-dots" aria-hidden="true">
        <span class="dot"></span>
        <span class="dot"></span>
        <span class="dot"></span>
      </div>
      <strong>Bonehawk Trader</strong>
      <span id="titlebar-session">Session 01 / Alpaca</span>
    </header>
    <aside>
      <div class="brand">
        <div class="terminal-mark">BH</div>
        <div class="brand-copy">
          <h1>bonehawk</h1>
          <div class="sub">AI trading terminal</div>
        </div>
      </div>
      <button class="menu-pin" onclick="expandSidebar()">Lock rail</button>
      <div class="nav-section">
        <div class="nav-label">Trading</div>
        <nav class="nav" aria-label="Trading sections">
          <button class="tab active" data-title="AI Desk" onclick="showTab('overview-panel', this)"><span class="nav-glyph">AI</span><span class="nav-text">AI Desk</span><span class="nav-kbd">01</span></button>
          <button class="tab" data-title="Signals" onclick="showTab('ideas-panel', this)"><span class="nav-glyph">SG</span><span class="nav-text">Signals</span><span class="nav-kbd">02</span></button>
          <button class="tab" data-title="Growth" onclick="showTab('growth-panel', this)"><span class="nav-glyph">GR</span><span class="nav-text">Growth</span><span class="nav-kbd">03</span></button>
          <button class="tab" data-title="Stocks" onclick="showTab('stocks-panel', this)"><span class="nav-glyph">ST</span><span class="nav-text">Stocks</span><span class="nav-kbd">04</span></button>
          <button class="tab" data-title="Tickets" onclick="showTab('tickets-panel', this)"><span class="nav-glyph">TK</span><span class="nav-text">Tickets</span><span class="nav-kbd">05</span></button>
          <button class="tab" data-title="Live View" onclick="showTab('live-panel', this)"><span class="nav-glyph">LV</span><span class="nav-text">Live View</span><span class="nav-kbd">06</span></button>
        </nav>
      </div>
      <div class="nav-section">
        <div class="nav-label">System</div>
        <nav class="nav" aria-label="System sections">
          <button class="tab" data-title="Logs" onclick="showTab('logs-panel', this)"><span class="nav-glyph">LG</span><span class="nav-text">Logs</span><span class="nav-kbd">07</span></button>
          <button class="tab" data-title="Settings" onclick="showTab('settings-panel', this)"><span class="nav-glyph">⚙</span><span class="nav-text">Settings</span><span class="nav-kbd">08</span></button>
        </nav>
      </div>
      <div class="rail-status">
        <div id="rail-mode">Mode loading</div>
        <div id="rail-updated-side">Waiting for market data</div>
      </div>
    </aside>
    <main>
      <div class="topbar">
        <div class="status-strip">
          <span class="status"><i class="led"></i><b>Backend</b><span id="backend-state">Online</span></span>
          <span class="status"><i class="led"></i><b>Alpaca</b><span id="broker-state">Paper</span></span>
          <span class="status"><i class="led warn"></i><b>Live</b><span id="liveState">Disarmed</span></span>
          <span class="status"><b>Updated</b><span id="rail-updated">Waiting</span></span>
        </div>
        <div class="top-actions">
          <button id="menu-toggle" class="btn icon menu-toggle" data-action onclick="toggleSidebar(event)" title="Toggle menu">☰</button>
          <div class="mode-switch" role="group" aria-label="Trading mode">
            <button data-action data-mode-option="paper" class="mode-option" onclick="setTradingMode('paper')">Paper</button>
            <button data-action data-mode-option="live" class="mode-option live" onclick="setTradingMode('live')">Live</button>
          </div>
          <button class="btn" data-action onclick="runPaper(false)">Paper AI</button>
          <button class="btn danger" data-action onclick="setTradingMode('live')">Live Alpaca</button>
          <button class="btn icon" data-action onclick="setTradingMode('paper')" title="Disarm live trading">□</button>
          <button class="btn primary" data-action onclick="scanAutopilot()">Run Scan</button>
        </div>
      </div>
      <div class="workspace">
        <div class="page-head">
          <div>
            <div class="eyebrow">Observe-only intelligence / dynamic account sizing</div>
            <h1 id="view-title">AI Desk</h1>
            <p class="subtitle">Autopilot weighs market data, news, model probability, account cash, price, edge, and risk before it plans paper trades.</p>
            <div id="ui-status" class="status-line">Loading market data...</div>
          </div>
          <div class="head-tools">
            <div class="command-line"><span>&gt;</span><code id="commandText">scan --broker alpaca --mode paper --risk dynamic</code></div>
            <div class="search-box">
              <span class="muted">Search</span>
              <input id="symbol-search" type="text" placeholder="NVDA, BTC, SPY..." oninput="filterVisibleRows(this.value)" onkeydown="openTypedSymbol(event)">
            </div>
          </div>
        </div>
        <div id="market-state" class="market-state">Market data live</div>
        <div id="ticker-tape" class="ticker-tape"></div>

      <section id="overview-panel" class="tab-panel active">
        <div class="section-head">
          <div>
            <h2>Overview</h2>
            <div id="symbols" class="muted"></div>
          </div>
          <div class="toolbar overview-actions">
            <button data-action onclick="scanAutopilot()">Scan</button>
            <button data-action class="primary" onclick="runAutopilotPaper()">Run Paper</button>
          </div>
        </div>
        <div class="overview-flow">
          <div id="portfolio-panel" class="overview-compact">
            <div id="metric-grid" class="metric-grid"></div>
            <div class="two-col">
              <div class="panel-block">
                <h2>Positions</h2>
                <div id="position-list" class="data-list"></div>
              </div>
              <div class="panel-block">
                <h2>Sync</h2>
                <div id="portfolio-sync" class="data-list"></div>
              </div>
            </div>
          </div>
          <div id="autopilot-panel" class="overview-compact">
            <div id="autopilot-metrics" class="metric-grid"></div>
            <div class="panel-block loop-strip">
              <div>
                <h2>Background Paper Loop</h2>
                <div id="autopilot-background-detail" class="panel-sub">Auto-runs Scan + Run Paper every 10 seconds while Bonehawk is open. Paper mode only.</div>
              </div>
              <div class="loop-actions">
                <span id="autopilot-background-status" class="pill trim">Starting</span>
                <button data-action onclick="setAutopilotBackground(true)">Start</button>
                <button data-action onclick="setAutopilotBackground(false)">Stop</button>
              </div>
            </div>
            <div class="panel-block">
              <div class="section-head">
                <div>
                  <h2>Trading Desk</h2>
                  <div class="panel-sub">Order truth, journal, strategy scorecard, shadow mode, backtest, and data confidence.</div>
                </div>
                <span id="desk-health-pill" class="pill quiet">Loading</span>
              </div>
              <div id="desk-metrics" class="metric-grid"></div>
              <div class="two-col">
                <div>
                  <h2>Order Truth</h2>
                  <div id="desk-order-truth" class="data-list"></div>
                </div>
                <div>
                  <h2>Strategy Scorecard</h2>
                  <div id="desk-scorecard" class="data-list"></div>
                </div>
              </div>
              <div class="two-col">
                <div>
                  <h2>Shadow Mode</h2>
                  <div id="desk-shadow" class="data-list"></div>
                </div>
                <div>
                  <h2>Backtest</h2>
                  <div id="desk-backtest" class="data-list"></div>
                </div>
              </div>
            </div>
            <div class="desk-grid">
              <div class="panel-block">
                <div class="section-head">
                  <div>
                    <h2>Agent Pipeline</h2>
                    <div id="autopilot-context" class="panel-sub">Market, narrative, prediction, and risk agents update this desk before orders are planned.</div>
                  </div>
                </div>
                <div class="pipeline">
                  <div class="agent-node">
                    <h2>News Data</h2>
                    <div id="agent-scan" class="data-list"></div>
                    <div class="bar" aria-hidden="true"><i style="--w: 76%"></i></div>
                  </div>
                  <div class="agent-node">
                    <h2>Agent 1: Sentiment</h2>
                    <div id="agent-research" class="data-list"></div>
                    <div class="bar" aria-hidden="true"><i style="--w: 68%"></i></div>
                  </div>
                  <div class="agent-node">
                    <h2>Agent 2: Technical</h2>
                    <div id="agent-prediction" class="data-list"></div>
                    <div class="bar" aria-hidden="true"><i style="--w: 72%"></i></div>
                  </div>
                  <div class="agent-node">
                    <h2>Agent 3: Portfolio Manager</h2>
                    <div id="agent-risk" class="data-list"></div>
                    <div class="bar" aria-hidden="true"><i style="--w: 62%"></i></div>
                  </div>
                  <div class="agent-node">
                    <h2>Agent 4: Executor</h2>
                    <div id="agent-execution" class="data-list"></div>
                    <div class="bar" aria-hidden="true"><i style="--w: 50%"></i></div>
                  </div>
                  <div class="agent-node">
                    <h2>Post-Mortem Agents</h2>
                    <div id="agent-postmortem" class="data-list"></div>
                    <div class="bar" aria-hidden="true"><i style="--w: 35%"></i></div>
                  </div>
                  <div class="agent-node">
                    <h2>Performance Report</h2>
                    <div id="agent-performance" class="data-list"></div>
                    <div class="bar" aria-hidden="true"><i style="--w: 58%"></i></div>
                  </div>
                  <div class="agent-node">
                    <h2>Telegram Alert</h2>
                    <div id="agent-telegram" class="data-list"></div>
                    <div class="bar" aria-hidden="true"><i style="--w: 46%"></i></div>
                  </div>
                </div>
              </div>
              <div class="panel-block">
                <h2>Risk Guard</h2>
                <div class="panel-sub">Sizing is decided from cash, buying power, stock price, projected probability, edge, and stop distance.</div>
                <div class="risk-grid">
                  <div class="risk-cell"><span>Account Cash</span><b id="risk-cash">...</b></div>
                  <div class="risk-cell"><span>Buying Power</span><b id="risk-buying-power">...</b></div>
                  <div class="risk-cell"><span>Max Kelly</span><b id="risk-kelly">...</b></div>
                  <div class="risk-cell"><span>Open Slots</span><b id="risk-slots">...</b></div>
                </div>
                <h2>Live Readiness</h2>
                <div id="autopilot-risk" class="readiness"></div>
              </div>
            </div>
            <div class="panel-block">
              <div class="section-head">
                <div>
                  <h2>Loss Post-Mortem</h2>
                  <div class="panel-sub">Scans losing trades from the last 24 hours, then applies reviewed learnings only when approved.</div>
                </div>
                <div class="toolbar">
                  <button data-action onclick="runLossPostmortem()">Run Post-Mortem</button>
                  <button data-action class="primary" onclick="applyPostmortemLearnings()">Apply Learnings</button>
                </div>
              </div>
              <div id="postmortem-summary" class="data-list"></div>
              <pre id="postmortem-output">No post-mortem report yet.</pre>
            </div>
            <div class="panel-block">
              <div class="section-head">
                <div>
                  <h2>Opportunities</h2>
                  <div class="panel-sub">Paper tickets planned by the current scan, ranked by model score and guardrails.</div>
                </div>
                <div class="tabs" aria-label="Opportunity filters">
                  <span class="pill buy">Stocks</span>
                  <span class="pill quiet">1-5m</span>
                  <span class="pill trim">Telegram</span>
                </div>
              </div>
              <div id="autopilot-orders" class="opportunity-table"></div>
            </div>
            <div class="panel-block">
              <h2>Execution Output</h2>
              <pre id="autopilot-output">No autopilot run yet.</pre>
            </div>
          </div>
          <div id="scanner-panel" class="overview-compact">
            <div class="two-col">
              <div class="panel-block">
                <h2>Market Scanner</h2>
                <div id="scanner" class="data-list"></div>
              </div>
              <div id="risk-news-rail" class="risk-news-rail">
                <div class="panel-block">
                  <h2>Risk Flags</h2>
                  <div id="risk" class="data-list"></div>
                </div>
                <div id="news-panel" class="panel-block">
                  <h2>News</h2>
                  <div id="news" class="data-list"></div>
                </div>
                <div class="panel-block">
                  <h2>Insider Filings</h2>
                  <div id="insiders" class="data-list"></div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>

      <section id="ideas-panel" class="tab-panel">
        <div class="section-head">
          <h2>Trade Ideas</h2>
          <div id="idea-context" class="muted"></div>
        </div>
        <div id="trade-ideas" class="data-list"></div>
      </section>

      <section id="growth-panel" class="tab-panel">
        <div class="section-head">
          <h2>Quick Growth</h2>
          <div id="growth-context" class="muted"></div>
        </div>
        <div id="growth-metrics" class="metric-grid"></div>
        <div id="growth-candidates" class="data-list"></div>
      </section>

      <section id="stocks-panel" class="tab-panel">
        <div class="section-head">
          <h2>Stocks</h2>
          <div id="stocks-status" class="muted"></div>
        </div>
        <div id="stocks-metrics" class="metric-grid"></div>
        <div class="two-col">
          <div class="panel-block">
            <h2>Scan Universe</h2>
            <div id="stock-symbols" class="symbol-cloud"></div>
          </div>
          <div class="panel-block">
            <h2>Execution</h2>
            <div id="stock-execution" class="data-list"></div>
          </div>
        </div>
      </section>

      <section id="tickets-panel" class="tab-panel">
        <div class="section-head">
          <h2>Buy / Sell Tickets</h2>
          <div id="tickets-status" class="muted"></div>
        </div>
        <div id="tickets-list" class="data-list"></div>
      </section>

      <section id="live-panel" class="tab-panel">
        <div class="section-head">
          <h2>Live Order View</h2>
          <div class="toolbar" style="justify-content:flex-end">
            <div id="live-order-status" class="muted"></div>
            <button data-action onclick="runOrderReconciliation()">Reconcile</button>
          </div>
        </div>
        <div class="live-grid">
          <div class="panel-block">
            <div class="section-head">
              <div>
                <h2>Order Radar</h2>
                <div class="panel-sub">Compact broker events, fills, rejects, and review tickets.</div>
              </div>
              <span class="pill quiet">5s refresh</span>
            </div>
            <div id="live-order-summary" class="live-mini-grid"></div>
            <div id="live-order-flow" class="live-flow"></div>
            <div id="live-order-feed" class="live-event-stack"></div>
          </div>
          <div class="panel-block">
            <h2>Background Loop</h2>
            <div id="live-order-background" class="live-side-stack"></div>
          </div>
        </div>
      </section>

      <section id="logs-panel" class="tab-panel">
        <div class="two-col">
          <div class="panel-block">
            <h2>Decision Log</h2>
            <div id="decision-log" class="data-list"></div>
          </div>
          <div class="panel-block">
            <h2>Paper Cycle Output</h2>
            <pre id="paper">No run yet.</pre>
          </div>
        </div>
      </section>

      <section id="settings-panel" class="tab-panel">
        <div class="two-col">
          <div class="panel-block">
            <h2>Status</h2>
            <div id="status" class="data-list"></div>
          </div>
          <div class="panel-block">
            <h2>Autopilot Controls</h2>
            <div id="autopilot-settings" class="data-list"></div>
          </div>
          <div class="panel-block">
            <h2>Private Beta Hardening</h2>
            <div class="panel-sub">Setup recovery, release readiness, and emergency exit controls.</div>
            <div class="toolbar" style="justify-content:flex-start;margin:10px 0">
              <button data-action onclick="runSetupDiagnostics()">Diagnostics</button>
              <button data-action onclick="runPrivateBetaCheck()">Beta Check</button>
              <button data-action onclick="runOrderReconciliation()">Reconcile Orders</button>
              <button data-action class="danger" onclick="runEmergencyLiquidation()">Liquidate</button>
            </div>
            <div id="hardening-status" class="data-list"></div>
          </div>
          <div class="panel-block">
            <h2>Interface</h2>
            <div id="ui-theme-settings" class="data-list"></div>
          </div>
          <div class="panel-block">
            <h2>App Commands</h2>
            <div id="command-status" class="muted"></div>
            <div id="command-groups" class="settings-command-groups"></div>
          </div>
          <div class="panel-block">
            <h2>Command Output</h2>
            <pre id="command-output">No command run yet.</pre>
          </div>
          <div class="panel-block">
            <h2>Capabilities</h2>
            <div id="capabilities" class="data-list"></div>
          </div>
        </div>
      </section>
      </div>
    </main>
  </div>
  <div id="setup-modal" class="setup-modal" hidden>
    <div class="setup-card">
      <div class="section-head">
        <div>
          <h2>First Run Setup</h2>
          <div id="setup-status-line" class="muted">Bonehawk needs Alpaca paper keys before autopilot can run.</div>
        </div>
        <button onclick="hideSetupModal()">Later</button>
      </div>
      <div id="setup-steps" class="setup-steps"></div>
      <form id="setup-form" onsubmit="submitSetup(event)">
        <div class="setup-grid">
          <label class="setup-field">
            Alpaca API key
            <input id="setup-alpaca-api-key" type="password" autocomplete="off" placeholder="Paper API key">
          </label>
          <label class="setup-field">
            Alpaca secret key
            <input id="setup-alpaca-secret-key" type="password" autocomplete="off" placeholder="Paper secret key">
          </label>
          <label class="setup-field">
            Max open positions
            <input id="setup-max-open-positions" type="number" min="0" max="25" step="1" value="3">
          </label>
          <label class="setup-field">
            Telegram bot token
            <input id="setup-telegram-token" type="password" autocomplete="off" placeholder="Optional">
          </label>
          <label class="setup-field">
            Telegram chat IDs
            <input id="setup-chat-ids" type="text" autocomplete="off" placeholder="Optional comma-separated IDs">
          </label>
        </div>
        <label class="setup-field">
          <input id="setup-risk-acknowledged" type="checkbox">
          Risk acknowledgement
          <span class="muted">I understand Bonehawk is not financial advice, automated trading can lose money, and live mode is my responsibility.</span>
        </label>
        <div class="setup-actions">
          <button type="button" onclick="hideSetupModal()">Skip for now</button>
          <button type="submit" class="primary" data-action>Save Setup</button>
        </div>
      </form>
    </div>
  </div>
  <div id="toast-stack" class="toast-stack" aria-live="polite" aria-atomic="false"></div>
  <div id="stock-chart-drawer" class="chart-drawer" hidden>
    <div class="chart-head">
      <div>
        <div class="chart-title">
          <h2 id="stock-chart-title">Stock Chart</h2>
          <span id="stock-chart-range-pill" class="pill quiet">1D</span>
        </div>
        <div id="stock-chart-subtitle" class="muted">Click a symbol to load chart data.</div>
      </div>
      <button onclick="closeStockChart()">Close</button>
    </div>
    <div id="chart-range-buttons" class="range-control" aria-label="Chart range">
      <button onclick="setStockChartRange('1d')" data-chart-range="1d">1D</button>
      <button onclick="setStockChartRange('1w')" data-chart-range="1w">1W</button>
      <button onclick="setStockChartRange('1m')" data-chart-range="1m">1M</button>
      <button onclick="setStockChartRange('3m')" data-chart-range="3m">3M</button>
      <button onclick="setStockChartRange('1y')" data-chart-range="1y">1Y</button>
    </div>
    <div id="stock-chart-stats" class="chart-stats"></div>
    <div class="chart-canvas-wrap">
      <canvas id="stock-chart-canvas" width="960" height="360" onmousemove="showChartTooltip(event)" onmouseleave="hideChartTooltip()"></canvas>
      <div id="stock-chart-tooltip" class="chart-tooltip" hidden></div>
    </div>
    <div id="stock-chart-status" class="muted">Review-only chart. No order placed.</div>
  </div>
  <div id="stock-ticket-drawer" class="ticket-drawer" hidden>
    <div class="chart-head">
      <div>
        <div class="chart-title">
          <h2 id="stock-ticket-title">Stock Ticket</h2>
          <span id="stock-ticket-side" class="pill quiet">Buy</span>
        </div>
        <div id="stock-ticket-note" class="muted">Paper ticket by default. Live Alpaca orders need confirmation.</div>
      </div>
      <button onclick="closeStockTicket()">Close</button>
    </div>
    <div class="ticket-form">
      <label>
        Shares
        <input id="stock-ticket-quantity" type="number" inputmode="decimal" min="0.0001" step="0.0001" value="1">
      </label>
      <label>
        Live confirmation
        <input id="stock-ticket-confirm" type="text" autocomplete="off" placeholder="LIVE_ALPACA_ORDER">
      </label>
      <div class="ticket-actions">
        <button onclick="closeStockTicket()">Cancel</button>
        <button class="primary" data-action onclick="submitStockTicket()">Record Ticket</button>
        <button data-action onclick="submitLiveStockTicket()">Send Live</button>
      </div>
    </div>
  </div>
  <script>
    let selectedChartSymbol = '';
    let selectedChartRange = '1d';
    let chartPlotPoints = [];
    let pendingStockTicket = {symbol: '', side: 'BUY'};
    let setupDismissed = false;
    let lastBackgroundRunRendered = 0;
    let lastPostmortemReportId = '';
    const ESSENTIAL_COMMAND_IDS = new Set(['telegram-test', 'telegram-autopilot-once', 'telegram-autopilot-loop', 'daily-loop', 'pytest', 'packaged-smoke']);

    async function getJson(url, options) {
      const res = await fetch(url, options);
      const data = await res.json();
      if (!res.ok) throw new Error(data.message || data.error || `Request failed: ${res.status}`);
      return data;
    }
    function escapeHtml(value) {
      return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    }
    function safeUrl(value) {
      try {
        const url = new URL(String(value || ''), window.location.origin);
        return ['http:', 'https:'].includes(url.protocol) ? escapeHtml(url.href) : '#';
      } catch {
        return '#';
      }
    }
    function money(value) {
      const number = Number(value || 0);
      return `$${number.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
    }
    function pct(value) {
      const number = Number(value || 0);
      const cls = number >= 0 ? 'positive' : 'negative';
      return `<span class="${cls}">${number.toFixed(2)}%</span>`;
    }
    function pill(text, extra = '') {
      return `<span class="pill ${extra}">${escapeHtml(text)}</span>`;
    }
    function humanize(value) {
      return escapeHtml(String(value || 'unknown').replaceAll('_', ' '));
    }
    function symbolButton(symbol) {
      const value = String(symbol || '').toUpperCase();
      return `<button class="symbol-link" data-symbol="${escapeHtml(value)}" onclick="openStockChart(this.dataset.symbol)">${escapeHtml(value)}</button>`;
    }
    function stockActionButtons(symbol) {
      const value = String(symbol || '').toUpperCase();
      return `<span class="stock-actions"><button class="trade-btn buy" data-action data-stock-action="BUY" data-stock-symbol="${escapeHtml(value)}">Buy</button><button class="trade-btn sell" data-action data-stock-action="SELL" data-stock-symbol="${escapeHtml(value)}">Sell</button></span>`;
    }
    function stockSymbolControls(symbol) {
      return `<span class="stock-controls">${symbolButton(symbol)}${stockActionButtons(symbol)}</span>`;
    }
    function actionClass(action) {
      const value = String(action || '').toLowerCase();
      if (value.includes('buy') || value.includes('hold')) return 'buy';
      if (value.includes('sell')) return 'sell';
      if (value.includes('trim') || value.includes('watch')) return 'trim';
      return 'no';
    }
    function metric(label, value, note) {
      return `<div class="metric"><div><div class="metric-label">${escapeHtml(label)}</div><div class="metric-value">${value}</div></div>${note ? `<div class="metric-note">${note}</div>` : ''}</div>`;
    }
    function row(title, sub = '', right = '') {
      return `<div class="data-row"><div><div class="data-title">${title}</div>${sub ? `<div class="data-sub">${sub}</div>` : ''}</div>${right ? `<div class="right-stack">${right}</div>` : ''}</div>`;
    }
    function settingSwitch(options, activeValue, handler) {
      return `<div class="mode-switch" style="--switch-count: ${options.length}">${options.map(option => `<button data-action class="mode-option ${option.danger ? 'live' : ''} ${option.value === activeValue ? 'active' : ''}" onclick="${handler}('${escapeHtml(option.value)}')">${escapeHtml(option.label)}</button>`).join('')}</div>`;
    }
    function empty(text) {
      return `<div class="empty">${escapeHtml(text)}</div>`;
    }
    function showOrderToast(result, fallbackTitle = 'Stock ticket') {
      const ok = Boolean(result?.ok);
      const status = String(result?.status || (ok ? 'ok' : 'failed'));
      const kind = ok ? 'ok' : status.includes('blocked') || status.includes('required') || status.includes('disabled') ? 'warn' : 'error';
      const title = ok
        ? `${escapeHtml(result.side || '')} ${escapeHtml(result.symbol || '')} ${result.review_only === false ? 'sent' : 'recorded'}`
        : `${fallbackTitle} ${kind === 'warn' ? 'blocked' : 'failed'}`;
      const lines = [
        result?.message || '',
        `Status: ${status}`,
        result?.symbol ? `Symbol: ${result.symbol}` : '',
        result?.side ? `Side: ${result.side}` : '',
        result?.quantity ? `Quantity: ${result.quantity}` : '',
        result?.current_price ? `Price: ${money(result.current_price)}` : '',
        result?.broker_order_id ? `Order ID: ${result.broker_order_id}` : '',
        result?.broker_status ? `Broker: ${result.broker_status}` : '',
        result?.fill_status ? `Fill: ${result.fill_status}` : '',
        Number.isFinite(Number(result?.filled_quantity)) ? `Filled Qty: ${result.filled_quantity}` : '',
        result?.filled_average_price ? `Avg Fill: ${money(result.filled_average_price)}` : '',
        result?.detail ? `Detail: ${result.detail}` : '',
        ok ? (result?.review_only === false ? 'Live connector response' : 'Review-only ticket') : 'Order was not sent'
      ].filter(Boolean);
      showToast(title, lines.join(' · '), kind);
    }
    function showToast(title, body, kind = 'ok') {
      const stack = document.getElementById('toast-stack');
      const id = `toast-${Date.now()}-${Math.random().toString(16).slice(2)}`;
      const node = document.createElement('div');
      node.className = `toast ${kind}`;
      node.id = id;
      node.innerHTML = `<div class="toast-head"><div class="toast-title">${escapeHtml(title)}</div><button class="toast-close" onclick="dismissToast('${id}')">x</button></div><div class="toast-body">${escapeHtml(body)}</div>`;
      stack.prepend(node);
      window.setTimeout(() => dismissToast(id), 9000);
    }
    function dismissToast(id) {
      const node = document.getElementById(id);
      if (node) node.remove();
    }
    function setStatus(text, kind) {
      const node = document.getElementById('ui-status');
      node.textContent = text;
      node.className = `status-line ${kind || ''}`;
    }
    function setBusy(isBusy) {
      document.querySelectorAll('button[data-action]').forEach(button => button.disabled = isBusy);
    }
    function showTab(id, button) {
      document.querySelectorAll('.tab-panel').forEach(panel => panel.classList.remove('active'));
      document.querySelectorAll('.tab').forEach(tab => tab.classList.remove('active'));
      document.getElementById(id).classList.add('active');
      button.classList.add('active');
      const title = button.dataset.title || button.textContent.trim();
      document.getElementById('view-title').textContent = title;
      const commandText = document.getElementById('commandText');
      if (commandText) commandText.textContent = commandForView(id);
      closeSidebar();
    }
    function commandForView(id) {
      const commands = {
        'overview-panel': 'scan --broker alpaca --mode paper --risk dynamic',
        'ideas-panel': 'signals --window 1-5m --rank edge',
        'growth-panel': 'growth --new --fast-moving --paper-only',
        'stocks-panel': 'universe --alpaca --available',
        'tickets-panel': 'tickets --orders --broker-response',
        'live-panel': 'orders --live-view --broker-response',
        'logs-panel': 'logs --decisions --execution',
        'settings-panel': 'settings --connectors --telegram'
      };
      return commands[id] || 'bonehawk --status';
    }
    function initSidebar() {
      const collapsed = window.localStorage.getItem('bonehawk-sidebar-collapsed') === 'true';
      document.body.classList.toggle('sidebar-collapsed', collapsed);
      document.body.classList.remove('menu-open');
    }
    function toggleSidebar(event) {
      if (event) event.stopPropagation();
      const collapsed = document.body.classList.contains('sidebar-collapsed');
      const open = document.body.classList.contains('menu-open');
      const overlayMode = window.matchMedia('(max-width: 860px)').matches;
      if (open) {
        document.body.classList.add('sidebar-collapsed');
        document.body.classList.remove('menu-open');
        window.localStorage.setItem('bonehawk-sidebar-collapsed', 'true');
        return;
      }
      if (overlayMode) {
        document.body.classList.add('menu-open');
        document.body.classList.add('sidebar-collapsed');
        window.localStorage.setItem('bonehawk-sidebar-collapsed', 'true');
        return;
      }
      if (collapsed) {
        expandSidebar();
        return;
      }
      document.body.classList.add('sidebar-collapsed');
      document.body.classList.remove('menu-open');
      window.localStorage.setItem('bonehawk-sidebar-collapsed', 'true');
    }
    function closeSidebar() {
      document.body.classList.remove('menu-open');
    }
    function expandSidebar() {
      document.body.classList.remove('sidebar-collapsed');
      document.body.classList.remove('menu-open');
      window.localStorage.setItem('bonehawk-sidebar-collapsed', 'false');
    }
    async function refreshStatus() {
      const [data, setup, diagnostics, beta, paperEvidence, liveReadiness, publicRelease, operationalHealth] = await Promise.all([
        getJson('/api/status'),
        getJson('/api/setup-status'),
        getJson('/api/setup-diagnostics'),
        getJson('/api/private-beta-check'),
        getJson('/api/paper-evidence'),
        getJson('/api/live-readiness'),
        getJson('/api/public-release-readiness'),
        getJson('/api/operational-health')
      ]);
      const mode = String(data.mode || 'unknown');
      document.getElementById('rail-mode').textContent = `Mode ${mode}`;
      document.getElementById('market-state').textContent = `Market data live · mode ${mode.toUpperCase()}`;
      document.getElementById('broker-state').textContent = mode.toLowerCase() === 'live' ? 'Live' : 'Paper';
      document.getElementById('liveState').textContent = mode.toLowerCase() === 'live' ? 'Armed' : 'Disarmed';
      document.getElementById('titlebar-session').textContent = `Session 01 / ${mode.toUpperCase()}`;
      setModeButtons(data.mode || 'missing');
      applyUiTheme(data.ui_theme || data.env?.BONEHAWK_UI_THEME || 'retro');
      document.getElementById('status').innerHTML = Object.entries(data.env).map(([k,v]) => row(escapeHtml(k), '', pill(v))).join('');
      renderUiThemeSettings(data);
      renderHardeningStatus(diagnostics, beta, paperEvidence, liveReadiness, publicRelease, operationalHealth);
      renderSetupModal(setup);
    }
    function renderSetupModal(data) {
      const modal = document.getElementById('setup-modal');
      const required = Boolean(data.required);
      if (!required || setupDismissed) {
        modal.hidden = true;
      } else {
        modal.hidden = false;
      }
      const steps = data.steps || {};
      document.getElementById('setup-status-line').textContent = required ? 'Add Alpaca paper keys to unlock autopilot paper orders.' : 'Setup complete.';
      document.getElementById('setup-steps').innerHTML = Object.entries(steps).map(([key, step]) => `
        <div class="setup-step">
          <div class="data-title">${escapeHtml(key)} ${pill(step.status || 'missing', step.status === 'set' ? 'buy' : step.status === 'missing' ? 'trim' : 'quiet')}</div>
          <div class="data-sub">${escapeHtml(step.message || '')}</div>
        </div>
      `).join('');
    }
    function hideSetupModal() {
      setupDismissed = true;
      document.getElementById('setup-modal').hidden = true;
    }
    async function submitSetup(event) {
      event.preventDefault();
      const payload = {
        alpaca_api_key: document.getElementById('setup-alpaca-api-key').value,
        alpaca_secret_key: document.getElementById('setup-alpaca-secret-key').value,
        alpaca_paper: true,
        telegram_bot_token: document.getElementById('setup-telegram-token').value,
        allowed_chat_ids: document.getElementById('setup-chat-ids').value,
        autopilot_enabled: true,
        max_open_positions: document.getElementById('setup-max-open-positions').value,
        risk_acknowledged: document.getElementById('setup-risk-acknowledged').checked
      };
      await runAction('Saving setup...', async () => {
        const data = await getJson('/api/setup', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(payload)
        });
        ['setup-alpaca-api-key', 'setup-alpaca-secret-key', 'setup-telegram-token'].forEach(id => document.getElementById(id).value = '');
        setupDismissed = false;
        renderSetupModal(data.setup);
        await refreshStatus();
        await refreshIntel();
        showToast('Setup saved', data.message || 'Bonehawk setup saved locally.', 'ok');
      }, false, false);
    }
    async function refreshCommands() {
      const data = await getJson('/api/commands');
      renderCommands(data);
    }
    function setModeButtons(mode) {
      const active = String(mode || '').toLowerCase();
      document.querySelectorAll('[data-mode-option]').forEach(button => {
        button.classList.toggle('active', button.dataset.modeOption === active);
      });
    }
    async function refreshIntel() {
      setStatus('Refreshing market data...', 'muted');
      const [data, trades, growth, sync, logs, tickets, stocks, autopilot, background, liveOrders, tradingDesk] = await Promise.all([getJson('/api/market-intel'), getJson('/api/trade-ideas'), getJson('/api/growth-candidates'), getJson('/api/portfolio-sync'), getJson('/api/decision-log'), getJson('/api/tickets'), getJson('/api/stocks'), getJson('/api/autopilot'), getJson('/api/autopilot-background'), getJson('/api/live-orders'), getJson('/api/trading-desk')]);
      renderPortfolio(data, trades, sync);
      renderTicker(trades);
      renderTradeIdeas(trades);
      renderGrowthCandidates(growth);
      renderStocks(stocks);
      renderAutopilot(autopilot);
      renderAutopilotBackground(background);
      renderTradingDesk(tradingDesk);
      renderAutopilotSettings(autopilot);
      renderScanner(trades, data);
      renderNews(data);
      renderLogs(logs);
      renderTickets(tickets);
      renderLiveOrders(liveOrders);
      renderSettings(data);
      const updated = new Date().toLocaleTimeString();
      document.getElementById('rail-updated').textContent = updated;
      document.getElementById('rail-updated-side').textContent = `Updated ${updated}`;
      setStatus(`Updated. Scanner checked ${trades.summary?.symbols_scanned || 0} symbols. Market trend: ${trades.market_trend || 'unknown'}.`, 'ok');
    }
    async function refreshOverviewMetrics() {
      try {
        const data = await getJson('/api/overview-metrics');
        const portfolio = data.portfolio || {};
        const source = String(portfolio.source_status || 'unknown');
        document.getElementById('metric-grid').innerHTML = [
          metric('Portfolio value', money(portfolio.account_value ?? portfolio.total_value), source === 'connected' ? 'Alpaca account value' : `Source ${source}`),
          metric('Open P&L', `${money(portfolio.unrealized_pnl)} ${pct(portfolio.unrealized_pnl_pct)}`, source === 'connected' ? 'Alpaca open positions' : 'Latest available positions'),
          metric('Market trend', escapeHtml(data.market_trend || 'unknown'), 'SPY/QQQ technical vote'),
          metric('Positions', String(portfolio.positions || 0), 'Open Alpaca positions')
        ].join('');
        const updated = new Date().toLocaleTimeString();
        document.getElementById('rail-updated').textContent = updated;
        document.getElementById('rail-updated-side').textContent = `Updated ${updated}`;
      } catch (error) {
        const node = document.getElementById('rail-updated-side');
        if (node) node.textContent = 'Metrics refresh paused';
      }
    }
    function renderTicker(trades) {
      const items = (trades.ideas || []).slice(0, 12);
      document.getElementById('ticker-tape').innerHTML = items.map(item => {
        const signal = (item.signals || []).find(value => String(value).startsWith('day ')) || '';
        const change = Number(String(signal).replace('day ', '').replace('%', '')) || 0;
        return `<div class="ticker">${stockSymbolControls(item.symbol)}<span>${item.current_price ? money(item.current_price) : 'n/a'}</span><span class="${change >= 0 ? 'positive' : 'negative'}">${change >= 0 ? '+' : ''}${change.toFixed(2)}%</span></div>`;
      }).join('');
    }
    function renderPortfolio(data, trades, sync) {
      const performance = data.portfolio_performance || {};
      const source = data.portfolio_source || {};
      const displayValue = performance.account_value ?? performance.total_value;
      document.getElementById('symbols').textContent = data.symbols.join(', ');
      document.getElementById('metric-grid').innerHTML = [
        metric('Portfolio value', money(displayValue), source.status === 'connected' ? 'Alpaca account value' : 'Watchlist estimate'),
        metric('Open P&L', `${money(performance.unrealized_pnl)} ${pct(performance.unrealized_pnl_pct)}`, source.status === 'connected' ? 'Alpaca open positions' : 'Configured positions only'),
        metric('Market trend', escapeHtml(trades.market_trend || 'unknown'), `${trades.summary?.symbols_scanned || 0} symbols scanned`),
        metric('Alerts', String(trades.summary?.alerts || 0), 'Review-only scanner alerts')
      ].join('');
      const positions = performance.positions || [];
      document.getElementById('position-list').innerHTML = positions.map(position => row(
        stockSymbolControls(position.symbol),
        `Qty ${position.quantity} · cost ${money(position.cost_basis)} · price ${money(position.current_price)}`,
        `${money(position.market_value)}<span>${pct(position.unrealized_pnl_pct)}</span>`
      )).join('') || empty(source.status === 'connected' ? 'No open Alpaca positions.' : 'No priced stock positions configured.');
      document.getElementById('portfolio-sync').innerHTML = [
        row('Stock sync', escapeHtml(sync.stock_sync?.message || ''), pill(sync.stock_sync?.status || 'unknown')),
        row('Crypto sync', escapeHtml(sync.crypto_sync?.message || ''), pill(sync.crypto_sync?.status || 'unknown'))
      ].join('');
    }
    function renderTradeIdeas(trades) {
      document.getElementById('idea-context').textContent = `Market ${trades.market_trend || 'unknown'} · ${trades.summary?.symbols_scanned || 0} scanned`;
      document.getElementById('trade-ideas').innerHTML = (trades.ideas || []).map(idea => {
        const action = escapeHtml(idea.action);
        const stops = [idea.current_price ? `price ${money(idea.current_price)}` : 'price n/a', idea.stop_loss ? `stop ${money(idea.stop_loss)}` : '', idea.take_profit ? `target ${money(idea.take_profit)}` : ''].filter(Boolean).join(' · ');
        const signals = (idea.signals || []).map(signal => pill(signal, 'quiet')).join('');
        const score = Math.max(0, Math.min(100, Number(idea.confidence || 0)));
        return row(
          `${stockSymbolControls(idea.symbol)}${pill(action, actionClass(action))}`,
          `${escapeHtml(idea.reason)} · ${stops}<div class="data-sub">${signals}</div>`,
          `<div>${score}/100</div><div class="scorebar"><span style="width:${score}%"></span></div>`
        );
      }).join('') || empty('No trade ideas loaded.');
    }
    function renderGrowthCandidates(growth) {
      const candidates = growth.candidates || [];
      document.getElementById('growth-context').textContent = `Market ${growth.market_trend || 'unknown'} · ${growth.summary?.symbols_scanned || 0} scanned`;
      const top = candidates[0] || {};
      document.getElementById('growth-metrics').innerHTML = [
        metric('Candidates', String(candidates.length), 'Quick-return review signals'),
        metric('Top score', String(top.momentum_score || 0), escapeHtml(top.symbol || 'none')),
        metric('Market trend', escapeHtml(growth.market_trend || 'unknown'), 'SPY/QQQ technical vote'),
        metric('Safety', 'Review only', 'No live order placed')
      ].join('');
      document.getElementById('growth-candidates').innerHTML = candidates.map(candidate => {
        const score = Math.max(0, Math.min(100, Number(candidate.momentum_score || 0)));
        const signals = (candidate.signals || []).map(signal => pill(signal, 'quiet')).join('');
        return row(
          `${stockSymbolControls(candidate.symbol)}${pill(candidate.action, actionClass(candidate.action))}`,
          `${escapeHtml(candidate.reason)} · price ${candidate.current_price ? money(candidate.current_price) : 'n/a'} · day ${pct(candidate.day_change_pct)}<div class="data-sub">${signals}</div>`,
          `<div>${score}/100</div><div class="scorebar"><span style="width:${score}%"></span></div>`
        );
      }).join('') || empty('No quick-growth candidates loaded.');
    }
    function renderStocks(data) {
      const sample = data.sample_symbols || [];
      document.getElementById('stocks-status').textContent = `${data.total_symbols || 0} stock symbols loaded`;
      document.getElementById('stocks-metrics').innerHTML = [
        metric('Universe', String(data.total_symbols || 0), escapeHtml(data.source || 'market_universe')),
        metric('Active scan', String(data.scan_symbols || 0), `Cap ${escapeHtml(data.max_scan_symbols || 0)}`),
        metric('Broker', 'Alpaca', humanize(data.execution?.alpaca_trading_api)),
        metric('Manual orders', humanize(data.execution?.alpaca_paper_trading), 'Buy/Sell tickets use Alpaca')
      ].join('');
      document.getElementById('stock-symbols').innerHTML = sample.map(symbol => `<div class="symbol-chip">${stockSymbolControls(symbol)}</div>`).join('') || empty('No stock symbols loaded.');
      document.getElementById('stock-execution').innerHTML = [
        row('Alpaca Trading API', 'Stock orders, account reads, and paper/live execution.', pill(humanize(data.execution?.alpaca_trading_api), 'buy')),
        row('Alpaca paper trading', 'Default path for manual tickets and autopilot orders.', pill(humanize(data.execution?.alpaca_paper_trading), 'quiet'))
      ].join('');
    }
    function renderAutopilot(data) {
      const config = data.config || {};
      const broker = data.broker || {};
      const marketHours = data.market_hours || {};
      document.getElementById('autopilot-metrics').innerHTML = [
        metric('Status', humanize(data.status), `Mode ${escapeHtml(config.mode || 'paper')}`),
        metric('Broker', 'Alpaca', humanize(broker.status || 'unknown')),
        metric('Sizing', 'Dynamic', `${config.max_open_positions || 0} max open positions`),
        metric('Agent window', `${escapeHtml(config.scan_window_minutes || 5)}m`, 'Cash, price, probability, and edge'),
        metric('Market clock', humanize(marketHours.status || 'unknown'), marketHours.next_open ? `Next open ${escapeHtml(marketHours.next_open)}` : 'Alpaca clock pending'),
        metric('Live gate', config.live_ready ? 'Ready' : 'Locked', config.allow_live ? 'Live permission on' : 'Paper-first')
      ].join('');
      document.getElementById('autopilot-orders').innerHTML = empty('Run Scan to build a fresh paper plan.');
      renderRiskGuard(data);
      renderAutopilotAgents(data);
    }
    function renderTradingDesk(data) {
      const health = data.data_health || {};
      const truth = (data.order_truth || {}).summary || {};
      const journal = (data.trade_journal || {}).summary || {};
      const scorecard = data.strategy_scorecard || {};
      const strategies = scorecard.strategies || [];
      const shadow = (data.shadow_mode || {}).summary || {};
      const backtest = (data.backtest || {}).summary || {};
      const healthPill = document.getElementById('desk-health-pill');
      if (healthPill) {
        healthPill.textContent = `${humanize(health.status || 'unknown')} ${health.score || 0}`;
        healthPill.className = `pill ${health.status === 'healthy' ? 'buy' : health.status === 'degraded' ? 'trim' : 'sell'}`;
      }
      document.getElementById('desk-metrics').innerHTML = [
        metric('Data confidence', `${health.score || 0}/100`, humanize(health.risk_action || 'unknown')),
        metric('Active orders', String(truth.active || 0), `${truth.submitted || 0} submitted · ${truth.rejected || 0} rejected`),
        metric('Journal P&L', money(journal.net_pnl || 0), `${journal.wins || 0}W / ${journal.losses || 0}L`),
        metric('Best backtest', escapeHtml(backtest.best_symbol || 'none'), `${Number(backtest.best_return_pct || 0).toFixed(2)}%`)
      ].join('');
      document.getElementById('desk-order-truth').innerHTML = [
        row('Lifecycle', `${truth.created || 0} created · ${truth.queued || 0} queued · ${truth.filled || 0} filled`, pill(`${truth.total || 0} total`, 'quiet')),
        row('Broker outcomes', `${truth.submitted || 0} submitted · ${truth.partial_fill || 0} partial · ${truth.canceled || 0} canceled`, pill(`${truth.rejected || 0} rejected`, truth.rejected ? 'sell' : 'buy')),
        row('Data health action', escapeHtml(health.risk_action || 'unknown'), pill(health.status || 'unknown', health.status === 'healthy' ? 'buy' : 'trim'))
      ].join('');
      document.getElementById('desk-scorecard').innerHTML = strategies.slice(0, 4).map(item => row(
        escapeHtml(item.strategy || 'strategy'),
        `${item.submitted || 0} submitted · ${item.wins || 0}W/${item.losses || 0}L · avg ${money(item.avg_pnl || 0)}`,
        `${Number(item.win_rate_pct || 0).toFixed(1)}% ${pill(item.status || 'collecting', item.status === 'throttle' ? 'sell' : item.status === 'promote' ? 'buy' : 'quiet')}`
      )).join('') || empty('No strategy outcomes recorded yet.');
      document.getElementById('desk-shadow').innerHTML = [
        row('Shadow outcomes', `${shadow.evaluated || 0} evaluated · ${shadow.open || 0} open`, pill(`${shadow.wins || 0}W/${shadow.losses || 0}L`, shadow.wins >= shadow.losses ? 'buy' : 'trim')),
        row('Average return', `${Number(shadow.avg_return_pct || 0).toFixed(2)}%`, pill('paperless', 'quiet'))
      ].join('');
      document.getElementById('desk-backtest').innerHTML = [
        row('Symbols tested', `${backtest.symbols_tested || 0}`, pill(`${backtest.passing || 0} pass`, backtest.passing ? 'buy' : 'quiet')),
        row('Best setup', escapeHtml(backtest.best_symbol || 'none'), `${Number(backtest.best_return_pct || 0).toFixed(2)}%`)
      ].join('');
    }
    function renderAutopilotBackground(data) {
      const statusNode = document.getElementById('autopilot-background-status');
      const detailNode = document.getElementById('autopilot-background-detail');
      if (!statusNode || !detailNode) return;
      const running = Boolean(data.running || data.enabled);
      const last = data.last_result || {};
      const scan = last.scan || {};
      const execution = last.execution || {};
      const marketHours = last.market_hours || {};
      const statusText = running ? 'Running' : 'Stopped';
      statusNode.textContent = statusText;
      statusNode.className = `pill ${running ? 'buy' : 'trim'}`;
      const pieces = [
        running ? `Auto-runs Scan + Run Paper every ${data.interval_seconds || 10} seconds.` : 'Background paper loop is stopped.',
        `Runs: ${data.runs || 0}`,
        last.status ? `Last: ${last.status}` : '',
        marketHours.status ? `market ${marketHours.status}` : '',
        scan.orders !== undefined ? `planned ${scan.orders}` : '',
        execution.submitted !== undefined ? `submitted ${execution.submitted}` : '',
        data.last_error ? `error ${data.last_error}` : ''
      ].filter(Boolean);
      detailNode.textContent = pieces.join(' · ');
      postBackgroundAutopilotResult(data);
    }
    function postBackgroundAutopilotResult(data) {
      const last = data.last_result || {};
      const display = last.display || null;
      const runNumber = Number(last.runs || data.runs || 0);
      if (!display || !runNumber || runNumber === lastBackgroundRunRendered) return;
      lastBackgroundRunRendered = runNumber;
      renderAutopilotPlan(display);
      document.getElementById('autopilot-output').textContent = formatBackgroundAutopilotOutput(last);
      (display.executed || []).forEach(item => showOrderToast(item, 'Background paper order'));
      refreshLiveOrders();
      setStatus(`Background loop posted run ${runNumber}: ${last.execution?.submitted || 0} paper order(s) submitted.`, last.ok ? 'ok' : 'muted');
    }
    function renderAutopilotAgents(data) {
      const agentic = data.agentic_scan || {};
      const agents = agentic.agents || {};
      const executed = data.executed || [];
      const orders = data.orders || [];
      const blocked = [...(data.blocked || []), ...(agentic.blocked || [])];
      const dataSources = data.data_sources || {};
      const telegram = data.telegram || {};
      const executionSummary = data.execution_summary || {};
      const sources = agents.research?.sources || {};
      const sourceText = Object.entries(sources).map(([source, count]) => `${source}: ${count}`).join(' · ') || 'No social/RSS sources loaded yet.';
      document.getElementById('agent-scan').innerHTML = [
        row('News Data', escapeHtml(dataSources.news || 'RSS/news plus optional social feeds.'), pill(agents.research?.status || 'waiting', 'quiet')),
        row('Market Data', escapeHtml(dataSources.market || 'Alpaca plus quote history.'), pill(data.market_trend || 'waiting', data.market_trend === 'DOWN' ? 'trim' : 'quiet')),
        row('Universe', `${data.summary?.symbols_scanned || agents.scan?.symbols_scanned || 0} symbols scanned`, pill(`${agentic.summary?.opportunities || 0} opps`, agentic.summary?.opportunities ? 'buy' : 'quiet'))
      ].join('');
      document.getElementById('agent-research').innerHTML = [
        row('Sources', sourceText, pill(agents.research?.status || 'waiting', 'quiet')),
        row('Sentiment', escapeHtml(agents.research?.method || 'Waiting for scan.'), pill('LLM/local', 'quiet'))
      ].join('');
      document.getElementById('agent-prediction').innerHTML = [
        row('Market model', escapeHtml(agents.prediction?.model || 'waiting'), pill(agents.prediction?.status || 'idle', 'quiet')),
        row('Calibration', escapeHtml(agents.prediction?.llm_calibration || 'Waiting for scan.'), pill('probability', 'quiet'))
      ].join('');
      document.getElementById('agent-risk').innerHTML = [
        row('Portfolio rule', `${orders.length} planned · ${blocked.length} blocked`, pill(agentic.summary?.top_symbol || 'none', 'quiet')),
        row('Dynamic sizing', escapeHtml(agents.risk?.method || 'waiting'), pill(agents.risk?.safety_ceiling_fraction ? `${Number(agents.risk.safety_ceiling_fraction * 100).toFixed(1)}% safety` : 'idle', 'quiet')),
        row('Bankroll', money(agents.risk?.bankroll_usd || 0), pill(`${orders.length} planned`, orders.length ? 'buy' : 'quiet'))
      ].join('');
      document.getElementById('agent-execution').innerHTML = [
        row('Submitted', `${executed.length} paper order${executed.length === 1 ? '' : 's'}`, pill(data.status || 'idle', executed.length ? 'buy' : 'quiet')),
        row('Broker', executed[0]?.broker_order_id || 'No order id yet', pill(data.mode || 'paper', data.mode === 'live' ? 'sell' : 'buy')),
        row('Execution path', escapeHtml(dataSources.execution || 'Alpaca paper orders by default.'), pill('Alpaca', 'quiet'))
      ].join('');
      document.getElementById('agent-postmortem').innerHTML = (agentic.postmortems || []).slice(0, 3).map(item =>
        row(`${escapeHtml(item.symbol || 'loss')}`, `${escapeHtml(String(item.realized_pnl || ''))} realized P&L`, pill('reviewed', 'trim'))
      ).join('') || row('Loss review', 'No new loss post-mortems.', pill(agents.postmortem?.status || 'ready', 'quiet'));
      document.getElementById('agent-performance').innerHTML = [
        row('Report', escapeHtml(executionSummary.message || data.notice || 'Run Scan or Run Paper to build a performance report.'), pill(data.status || 'idle', 'quiet')),
        row('Counts', `${executionSummary.submitted || 0} submitted · ${executionSummary.rejected || 0} rejected · ${executionSummary.planned || orders.length} planned`, pill(`${blocked.length} blocked`, blocked.length ? 'trim' : 'quiet'))
      ].join('');
      document.getElementById('agent-telegram').innerHTML = [
        row('Channel', 'Telegram', pill(telegram.status || 'needs_setup', telegram.status === 'ready' ? 'buy' : 'trim')),
        row('Setup', escapeHtml(telegram.message || 'Add Telegram setup values to enable alerts.'), pill(telegram.chat_ids === 'set' ? 'chat set' : 'chat missing', telegram.chat_ids === 'set' ? 'buy' : 'quiet'))
      ].join('');
    }
    function renderRiskGuard(data) {
      const config = data.config || {};
      const broker = data.broker || {};
      const agentic = data.agentic_scan || {};
      const risk = agentic.agents?.risk || {};
      const orders = data.orders || [];
      const blocked = [...(data.blocked || []), ...(agentic.blocked || [])];
      const bankroll = Number(risk.bankroll_usd || data.account?.cash || data.account?.buying_power || 0);
      const buyingPower = Number(risk.buying_power_usd || data.account?.buying_power || bankroll || 0);
      const maxKelly = Number(risk.safety_ceiling_fraction || config.max_kelly_fraction || 0);
      const slots = Math.max(0, Number(config.max_open_positions || 0) - orders.length);
      document.getElementById('risk-cash').textContent = bankroll ? money(bankroll) : 'Waiting';
      document.getElementById('risk-buying-power').textContent = buyingPower ? money(buyingPower) : 'Waiting';
      document.getElementById('risk-kelly').textContent = maxKelly ? `${(maxKelly * 100).toFixed(1)}%` : 'Locked';
      document.getElementById('risk-slots').textContent = Number.isFinite(slots) ? String(slots) : '...';
      document.getElementById('autopilot-risk').innerHTML = [
        checkRow('Alpaca key', broker.api_key === 'set' ? 'Trading connector can authenticate.' : 'Add paper keys in setup.', pill(broker.api_key || 'missing', broker.api_key === 'set' ? 'buy' : 'trim'), broker.api_key === 'set' ? '' : 'warn'),
        checkRow('Paper mode', 'Default execution stays in Alpaca paper trading.', pill(String(broker.paper ?? true), 'quiet')),
        checkRow('Live gate', config.live_ready ? 'Live mode is available but still guarded.' : 'Live trading remains locked.', pill(config.live_ready ? 'ready' : 'locked', config.live_ready ? 'buy' : 'trim'), config.live_ready ? '' : 'warn'),
        checkRow('Blocked ideas', `${blocked.length} setup${blocked.length === 1 ? '' : 's'} rejected by guardrails.`, pill(`${blocked.length}`, blocked.length ? 'trim' : 'buy'), blocked.length ? 'warn' : '')
      ].join('');
    }
    function checkRow(label, detail, badge, tone = '') {
      return `<div class="check ${tone}"><div><b>${escapeHtml(label)}</b><div class="data-sub">${escapeHtml(detail)}</div></div>${badge}</div>`;
    }
    function renderAutopilotPlan(data) {
      const orders = data.orders || [];
      const agentic = data.agentic_scan || {};
      const blocked = [...(data.blocked || []), ...(agentic.blocked || [])];
      document.getElementById('autopilot-orders').innerHTML = orders.length ? `
        <table>
          <thead>
            <tr>
              <th>Market</th>
              <th>Side</th>
              <th>Price</th>
              <th>Probability</th>
              <th>Edge</th>
              <th>Size</th>
              <th>Guardrail</th>
              <th>Ticket</th>
            </tr>
          </thead>
          <tbody>
            ${orders.map(order => {
        const symbol = escapeHtml(order.symbol || 'UNKNOWN');
        const side = String(order.side || order.action || 'BUY').toUpperCase();
        const score = Math.max(0, Math.min(100, Number(order.confidence || 0)));
        const probability = order.probability_up ? pct(Number(order.probability_up) * 100) : `${score}/100`;
        const edge = order.edge_pct !== undefined ? pct(Number(order.edge_pct)) : (order.edge ? pct(Number(order.edge) * 100) : (order.expected_return_pct ? pct(Number(order.expected_return_pct)) : 'n/a'));
        const shareSize = order.quantity || order.quantity_estimate;
        const size = order.notional ? `${money(order.notional)}${shareSize ? ` · ${Number(shareSize).toFixed(4)} sh` : ''}` : (shareSize ? `${Number(shareSize).toFixed(4)} sh` : 'n/a');
        const guardrail = [
          order.stop_loss ? `stop ${money(order.stop_loss)}` : '',
          order.kelly_fraction ? `kelly ${(Number(order.kelly_fraction) * 100).toFixed(2)}%` : '',
          order.take_profit ? `target ${money(order.take_profit)}` : '',
          order.profit_target_pct !== undefined ? `profit target ${Number(order.profit_target_pct).toFixed(2)}%` : '',
          order.unrealized_pnl_pct !== undefined ? `open ${Number(order.unrealized_pnl_pct).toFixed(2)}%` : '',
          order.held_quantity !== undefined ? `available ${Number(order.available_quantity || 0).toFixed(4)} / held ${Number(order.held_quantity || 0).toFixed(4)}` : ''
        ].filter(Boolean).join(' · ') || 'pass';
        return `
              <tr>
                <td class="market">${stockSymbolControls(symbol)}<div class="data-sub">${escapeHtml(order.reason || '')}</div></td>
                <td>${pill(side, actionClass(side))}</td>
                <td class="num">${order.current_price ? money(order.current_price) : 'n/a'}</td>
                <td class="num">${probability}</td>
                <td class="num">${edge}</td>
                <td class="num">${size}</td>
                <td>${escapeHtml(guardrail)}</td>
                <td>${stockActionButtons(symbol)}</td>
              </tr>`;
      }).join('')}
          </tbody>
        </table>
      ` : empty('No autopilot orders met the risk rules.');
      renderRiskGuard(data);
      renderAutopilotAgents(data);
    }
    function formatAutopilotOutput(data) {
      const executed = data.executed || [];
      const orders = data.orders || [];
      const blocked = [...(data.blocked || []), ...(data.agentic_scan?.blocked || [])];
      const lines = [
        executed.length
          ? `Submitted ${executed.length} Alpaca ${escapeHtml(data.mode || 'paper')} order${executed.length === 1 ? '' : 's'}.`
          : `No orders submitted. ${data.status || 'No status returned.'}`,
        `Trend: ${data.market_trend || 'unknown'} | Scanned: ${data.summary?.symbols_scanned || 0} | Planned: ${orders.length} | Blocked: ${blocked.length}`,
        `Telegram: ${data.telegram?.status || 'needs_setup'}`,
      ];
      if (executed.length) {
        lines.push('');
        lines.push('Submitted orders:');
        executed.slice(0, 6).forEach(item => {
          const size = item.notional ? money(item.notional) : (item.quantity ? `${Number(item.quantity).toFixed(4)} sh` : '');
          lines.push(`- ${item.symbol || 'UNKNOWN'} ${item.side || 'ORDER'} ${size} | ${item.broker_status || item.status || 'submitted'} | ${item.broker_order_id || 'no order id'}`);
        });
      } else if (orders.length) {
        lines.push('');
        lines.push('Planned but not submitted:');
        orders.slice(0, 6).forEach(item => {
          const size = item.notional ? money(item.notional) : (item.quantity ? `${Number(item.quantity).toFixed(4)} sh` : money(0));
          lines.push(`- ${item.symbol || 'UNKNOWN'} ${item.side || 'ORDER'} ${size} | ${item.reason || 'planned'}`);
        });
      }
      if (blocked.length) {
        lines.push('');
        lines.push('Top blocks:');
        blocked.slice(0, 5).forEach(item => {
          lines.push(`- ${item.symbol || 'Blocked'}: ${item.reason || item.status || 'blocked'}`);
        });
      }
      return lines.join('\\n');
    }
    function formatBackgroundAutopilotOutput(last) {
      const display = last.display || {};
      const scan = last.scan || {};
      const execution = last.execution || {};
      const header = [
        `Background loop run #${last.runs || 0}`,
        `Scan: ${scan.status || 'unknown'} · planned ${scan.orders ?? 0} · blocked ${scan.blocked ?? 0}`,
        `Run Paper: ${execution.status || 'unknown'} · submitted ${execution.submitted ?? 0} · rejected ${execution.rejected ?? 0}`,
        last.finished_at ? `Finished: ${last.finished_at}` : ''
      ].filter(Boolean).join('\\n');
      const detail = formatAutopilotOutput(display);
      return `${header}\\n\\n${detail}`;
    }
    async function scanAutopilot() {
      await runAction('Scanning Alpaca autopilot setups...', async () => {
        const data = await getJson('/api/autopilot-scan', {method: 'POST'});
        renderAutopilotPlan(data);
        document.getElementById('autopilot-output').textContent = formatAutopilotOutput(data);
        setStatus(`Autopilot planned ${data.orders?.length || 0} paper order(s).`, 'ok');
      }, false, false);
    }
    async function runAutopilotPaper() {
      await runAction('Running Alpaca autopilot paper execution...', async () => {
        const response = await fetch('/api/autopilot-run', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({confirm: ''})
        });
        const data = await response.json();
        renderAutopilotPlan(data);
        document.getElementById('autopilot-output').textContent = formatAutopilotOutput(data);
        (data.executed || []).forEach(item => showOrderToast(item, 'Autopilot order'));
        setStatus(data.ok ? 'Autopilot paper execution submitted.' : data.message || data.status || 'Autopilot did not submit orders.', data.ok ? 'ok' : 'error');
        await refreshTickets();
        await refreshLiveOrders();
      }, true, false);
    }
    async function runLossPostmortem() {
      await runAction('Running 24h loss post-mortem...', async () => {
        const data = await getJson('/api/postmortem-report?hours=24');
        renderLossPostmortem(data);
        setStatus(`Post-mortem found ${data.summary?.loss_count || 0} losing trade(s).`, data.summary?.loss_count ? 'ok' : 'muted');
      }, false, false);
    }
    async function applyPostmortemLearnings() {
      if (!lastPostmortemReportId) {
        await runLossPostmortem();
      }
      if (!lastPostmortemReportId) {
        setStatus('Run a post-mortem report before applying learnings.', 'error');
        return;
      }
      const reviewerNotes = window.prompt('Reviewer notes for the learning update', '') || '';
      await runAction('Applying reviewed post-mortem learnings...', async () => {
        const data = await getJson('/api/postmortem-apply', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({report_id: lastPostmortemReportId, reviewer_notes: reviewerNotes})
        });
        renderAppliedPostmortem(data);
        setStatus(data.message || 'Post-mortem learnings applied.', data.ok ? 'ok' : 'error');
      }, true, false);
    }
    function renderLossPostmortem(data) {
      lastPostmortemReportId = data.report_id || '';
      const summary = data.summary || {};
      const patterns = data.patterns || {};
      const issues = data.issues || [];
      const updates = data.suggested_updates || [];
      document.getElementById('postmortem-summary').innerHTML = [
        row('Losses', `${summary.loss_count || 0} losing trade(s) · total ${money(summary.total_realized_pnl || 0)} · average ${money(summary.average_loss || 0)}`, pill(`${data.window_hours || 24}h`, 'trim')),
        row('Largest loss', summary.largest_loss?.symbol ? `${escapeHtml(summary.largest_loss.symbol)} · ${money(summary.largest_loss.realized_pnl || 0)} · ${escapeHtml(summary.largest_loss.reason || '')}` : 'No loss trades in the window.', pill(data.status || 'ready', 'quiet')),
        row('Top pattern', (patterns.symbols || [])[0] ? `${escapeHtml(patterns.symbols[0].symbol)} · ${patterns.symbols[0].count} loss(es)` : 'No repeated symbol pattern.', pill(`${issues.length} issue${issues.length === 1 ? '' : 's'}`, issues.length ? 'trim' : 'buy')),
        row('Suggested update', updates.length ? updates.map(update => update.type === 'cooldown_symbol' ? `Cooldown ${update.symbol}` : update.type).join(' · ') : 'No system update suggested.', pill('review first', 'quiet'))
      ].join('');
      document.getElementById('postmortem-output').textContent = formatLossPostmortem(data);
    }
    function renderAppliedPostmortem(data) {
      const learning = data.learning || {};
      const cooldowns = Object.keys(learning.cooldown_symbols || {});
      document.getElementById('postmortem-summary').innerHTML = [
        row('Applied', escapeHtml(data.message || 'Learning update written.'), pill(data.status || 'applied', 'buy')),
        row('Cooldowns', cooldowns.length ? cooldowns.join(', ') : 'No symbol cooldowns.', pill(learning.active_until || 'active', 'trim')),
        row('Risk throttle', `${Number(learning.risk?.max_next_risk_fraction || 1).toFixed(2)}x next sizing budget`, pill(learning.risk?.require_price_confirmation ? 'confirmation on' : 'confirmation off', 'quiet'))
      ].join('');
      document.getElementById('postmortem-output').textContent = JSON.stringify(data, null, 2);
    }
    function formatLossPostmortem(data) {
      const summary = data.summary || {};
      const patterns = data.patterns || {};
      const lines = [
        `Loss Post-Mortem`,
        `Report: ${data.report_id || 'unknown'}`,
        `Window: ${data.window_hours || 24} hours`,
        `Loss trades: ${summary.loss_count || 0}`,
        `Total P/L: ${money(summary.total_realized_pnl || 0)}`,
        `Average loss: ${money(summary.average_loss || 0)}`,
        ''
      ];
      if ((patterns.symbols || []).length) {
        lines.push('Repeated symbols:');
        patterns.symbols.slice(0, 5).forEach(item => lines.push(`- ${item.symbol}: ${item.count} loss(es), ${money(item.realized_pnl || 0)}`));
        lines.push('');
      }
      if ((patterns.signals || []).length) {
        lines.push('Common signals:');
        patterns.signals.slice(0, 6).forEach(item => lines.push(`- ${item.signal}: ${item.count}`));
        lines.push('');
      }
      if ((data.issues || []).length) {
        lines.push('Issues found:');
        data.issues.forEach(issue => lines.push(`- [${issue.severity}] ${issue.title} ${issue.detail || ''}`));
        lines.push('');
      }
      if ((data.suggested_updates || []).length) {
        lines.push('Suggested system updates:');
        data.suggested_updates.forEach(update => lines.push(`- ${update.type}${update.symbol ? ` ${update.symbol}` : ''}${update.until ? ` until ${update.until}` : ''}`));
      } else {
        lines.push('No update suggested.');
      }
      return lines.join('\\n');
    }
    async function setAutopilotBackground(enabled) {
      await runAction(enabled ? 'Starting background paper loop...' : 'Stopping background paper loop...', async () => {
        const data = await getJson('/api/autopilot-background', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({enabled})
        });
        renderAutopilotBackground(data);
        setStatus(enabled ? 'Background paper loop is running every 10 seconds.' : 'Background paper loop stopped.', data.running ? 'ok' : 'muted');
      }, false, false);
    }
    async function refreshAutopilotBackgroundStatus() {
      try {
        const data = await getJson('/api/autopilot-background');
        renderAutopilotBackground(data);
      } catch {
        return;
      }
    }
    function renderScanner(trades, data) {
      document.getElementById('scanner').innerHTML = (trades.scans || []).map(scan => {
        const score = Math.max(0, Math.min(100, Number(scan.score || 0)));
        return row(
          `${stockSymbolControls(scan.symbol)}${pill(scan.rating, scan.rating === 'QUIET' ? 'quiet' : 'watch')}`,
          escapeHtml((scan.reasons || []).slice(0, 2).join(' ')),
          `<div>${score}/100</div><div class="scorebar"><span style="width:${score}%"></span></div>`
        );
      }).join('') || empty('No scanner data.');
      document.getElementById('risk').innerHTML = (data.risk_flags || []).map(flag => row(escapeHtml(flag), '', pill('risk', 'trim'))).join('') || empty('No risk flags.');
    }
    function renderNews(data) {
      document.getElementById('news').innerHTML = (data.news || []).map(item => row(
        `<a href="${safeUrl(item.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.title)}</a>`,
        escapeHtml(item.published || ''),
        pill(item.symbol || 'news')
      )).join('') || empty('No news loaded.');
      document.getElementById('insiders').innerHTML = (data.insider_filings || []).map(item => row(
        `<a href="${safeUrl(item.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.title)}</a>`,
        escapeHtml(item.updated || ''),
        pill('Form 4', 'trim')
      )).join('') || empty('No filings loaded.');
    }
    function renderLogs(logs) {
      document.getElementById('decision-log').innerHTML = (logs.decisions || []).map(item => row(
        `${stockSymbolControls(item.symbol)}${pill(item.action, actionClass(item.action))}`,
        `${escapeHtml(item.source)} · ${escapeHtml(item.reason || '')}`,
        escapeHtml(item.timestamp || '')
      )).join('') || empty('No decisions logged yet.');
    }
    function renderTickets(data) {
      const tickets = data.tickets || [];
      document.getElementById('tickets-status').textContent = `${tickets.length} ticket${tickets.length === 1 ? '' : 's'} tracked`;
      document.getElementById('tickets-list').innerHTML = tickets.map(ticket => {
        const status = String(ticket.status || 'unknown');
        const action = String(ticket.side || ticket.action || 'ORDER');
        const detail = [
          ticket.quantity ? `Qty ${ticket.quantity}` : '',
          ticket.current_price ? `price ${money(ticket.current_price)}` : '',
          ticket.broker_order_id ? `order ${ticket.broker_order_id}` : '',
          ticket.broker_status ? `broker ${ticket.broker_status}` : '',
          ticket.fill_status ? `fill ${ticket.fill_status}` : '',
          Number.isFinite(Number(ticket.filled_quantity)) ? `filled ${ticket.filled_quantity}` : '',
          ticket.review_only === false ? 'live connector' : 'review ticket'
        ].filter(Boolean).join(' · ');
        const message = [escapeHtml(ticket.message || ''), detail].filter(Boolean).join(' · ');
        return row(
          `${stockSymbolControls(ticket.symbol)}${pill(action, actionClass(action))}`,
          `${escapeHtml(ticket.source || 'ticket')} · ${message}`,
          `${pill(status, status === 'submitted' || status === 'recorded' ? 'buy' : 'trim')}<span>${escapeHtml(ticket.timestamp || '')}</span>`
        );
      }).join('') || empty('No buy or sell tickets yet.');
    }
    function renderLiveOrders(data) {
      const events = data.events || [];
      const summary = data.summary || {};
      const background = data.background || {};
      const reconciliation = data.reconciliation || {};
      const reconcileSummary = reconciliation.summary || {};
      const statusNode = document.getElementById('live-order-status');
      if (!statusNode) return;
      const total = summary.total ?? events.length;
      statusNode.textContent = `${total} events · ${summary.filled || 0} filled · ${summary.rejected || 0} rejected · ${summary.submitted || 0} submitted`;
      document.getElementById('live-order-summary').innerHTML = [
        liveMiniCard('Filled', summary.filled || 0, 'complete', 'filled'),
        liveMiniCard('Open', summary.submitted || 0, 'submitted', 'submitted'),
        liveMiniCard('Rejected', summary.rejected || 0, 'blocked', 'rejected'),
        liveMiniCard('Canceled', summary.canceled || 0, 'closed', 'canceled'),
        liveMiniCard('Tickets', summary.recorded || 0, 'review only', 'recorded')
      ].join('');
      const flow = summary.flow || [];
      document.getElementById('live-order-flow').innerHTML = flow.length ? flow.map(segment => {
        const category = String(segment.category || 'unknown').toLowerCase();
        const pct = Math.max(8, Math.min(100, Number(segment.pct || 0)));
        return `<div class="flow-segment ${escapeHtml(category)}" style="--w:${pct}%">${escapeHtml(category)} ${Number(segment.count || 0)}</div>`;
      }).join('') : '<div class="flow-segment unknown" style="--w:100%">idle</div>';
      document.getElementById('live-order-feed').innerHTML = events.slice(0, 30).map(liveOrderEventHtml).join('') || empty('No order events yet.');
      document.getElementById('live-order-background').innerHTML = [
        liveSideCard('Loop', background.running ? 'Background scan and paper run is active.' : 'Background scan and paper run is stopped.', pill(background.status || 'unknown', background.running ? 'buy' : 'trim')),
        liveSideCard('Runs', `${background.runs || 0} completed`, background.last_finished_at ? escapeHtml(background.last_finished_at) : ''),
        liveSideCard('Reconcile', `${reconcileSummary.refreshed || 0} refreshed · ${reconcileSummary.terminal || 0} terminal`, reconciliation.checked_at ? escapeHtml(reconciliation.checked_at) : 'Not run yet.'),
        background.last_error ? liveSideCard('Last error', background.last_error, pill('error', 'sell')) : liveSideCard('Last error', 'No background loop error reported.', pill('clear', 'buy'))
      ].join('');
    }
    function liveMiniCard(label, value, note, category) {
      return `<div class="live-mini-card ${escapeHtml(category || '')}">
        <div class="live-mini-label">${escapeHtml(label)}</div>
        <div class="live-mini-value">${escapeHtml(value)}</div>
        <div class="metric-note">${escapeHtml(note)}</div>
      </div>`;
    }
    function liveOrderEventHtml(event) {
      const category = String(event.category || event.status || 'unknown').toLowerCase();
      const direction = String(event.direction || '').toLowerCase();
      const side = String(event.side || event.action || 'ORDER').toUpperCase();
      const rowClass = category === 'rejected' ? 'rejected sell' : direction === 'sell' ? 'sell' : direction === 'buy' ? 'buy' : '';
      const qty = event.quantity ? `qty ${formatCompactNumber(event.quantity)}` : '';
      const filledQty = Number.isFinite(Number(event.filled_quantity)) ? `filled ${formatCompactNumber(event.filled_quantity)}` : '';
      const avg = event.filled_average_price ? `avg ${money(event.filled_average_price)}` : '';
      const broker = event.broker_status ? `broker ${event.broker_status}` : '';
      const orderId = event.broker_order_id ? `order ${shortOrderId(event.broker_order_id)}` : 'no order id';
      const schedule = event.scheduled_for_market_open ? `queued for open ${event.target_fill_time || ''}` : '';
      const age = formatAge(event.age_seconds);
      const detail = [event.source || 'order', event.message || '', schedule, qty, filledQty, avg, broker, orderId].filter(Boolean).join(' · ');
      const progress = Math.max(0, Math.min(100, Number(event.progress_pct || 0)));
      return `<div class="live-event ${escapeHtml(rowClass)}">
        <span class="live-event-rail"></span>
        <div class="live-event-main">
          <div class="live-event-title">
            ${stockSymbolControls(event.symbol)}
            ${pill(side, actionClass(side))}
            ${pill(category, liveOrderClass(category))}
          </div>
          <div class="live-event-detail">${escapeHtml(detail)}</div>
        </div>
        <div class="live-event-meta">
          <div class="live-progress" title="${progress}%"><span style="--w:${progress}%"></span></div>
          <span class="muted">${escapeHtml(age || event.timestamp || '')}</span>
        </div>
      </div>`;
    }
    function liveSideCard(title, body, badge) {
      return `<div class="live-side-card"><b>${escapeHtml(title)}</b><div class="muted">${escapeHtml(body)}</div><div>${badge || ''}</div></div>`;
    }
    function formatCompactNumber(value) {
      const number = Number(value || 0);
      if (!Number.isFinite(number)) return '';
      if (Math.abs(number) >= 1000) return number.toLocaleString(undefined, {maximumFractionDigits: 0});
      return number.toLocaleString(undefined, {maximumFractionDigits: 4});
    }
    function shortOrderId(value) {
      const text = String(value || '');
      return text.length > 10 ? text.slice(0, 8) : text;
    }
    function formatAge(seconds) {
      const value = Number(seconds);
      if (!Number.isFinite(value)) return '';
      if (value < 60) return `${Math.max(0, Math.round(value))}s ago`;
      if (value < 3600) return `${Math.round(value / 60)}m ago`;
      return `${Math.round(value / 3600)}h ago`;
    }
    function liveOrderClass(category) {
      const value = String(category || '').toLowerCase();
      if (value === 'submitted' || value === 'filled' || value === 'recorded') return 'buy';
      if (value === 'rejected') return 'sell';
      if (value === 'blocked' || value === 'canceled') return 'trim';
      return 'quiet';
    }
    async function refreshLiveOrders() {
      try {
        const data = await getJson('/api/live-orders');
        renderLiveOrders(data);
      } catch (error) {
        const node = document.getElementById('live-order-status');
        if (node) node.textContent = error.message || 'Live order feed unavailable.';
      }
    }
    async function refreshTickets() {
      const tickets = await getJson('/api/tickets');
      renderTickets(tickets);
    }
    async function openStockChart(symbol, range) {
      const normalized = String(symbol || '').trim().toUpperCase();
      if (!normalized) return;
      selectedChartSymbol = normalized;
      selectedChartRange = range || selectedChartRange || '1d';
      document.getElementById('stock-chart-drawer').hidden = false;
      await loadStockChart();
    }
    function closeStockChart() {
      document.getElementById('stock-chart-drawer').hidden = true;
    }
    async function setStockChartRange(range) {
      selectedChartRange = range || '1d';
      if (selectedChartSymbol) await loadStockChart();
    }
    async function loadStockChart() {
      updateChartRangeButtons();
      document.getElementById('stock-chart-title').textContent = `${selectedChartSymbol} Chart`;
      document.getElementById('stock-chart-range-pill').textContent = selectedChartRange.toUpperCase();
      document.getElementById('stock-chart-status').textContent = 'Loading chart data...';
      try {
        const chart = await getJson(`/api/stock-chart?symbol=${encodeURIComponent(selectedChartSymbol)}&range=${encodeURIComponent(selectedChartRange)}`);
        renderStockChart(chart);
      } catch (error) {
        document.getElementById('stock-chart-status').textContent = error.message || 'Chart unavailable.';
        drawStockChart([]);
      }
    }
    function updateChartRangeButtons() {
      document.querySelectorAll('[data-chart-range]').forEach(button => {
        button.classList.toggle('active', button.dataset.chartRange === selectedChartRange);
      });
    }
    function renderStockChart(chart) {
      const points = chart.points || [];
      document.getElementById('stock-chart-subtitle').textContent = `${points.length} points · interval ${chart.interval || 'n/a'} · review only`;
      document.getElementById('stock-chart-stats').innerHTML = [
        chartStat('Latest', money(chart.latest_price)),
        chartStat('Move', `${Number(chart.change_pct || 0).toFixed(2)}%`),
        chartStat('High', money(chart.summary?.high)),
        chartStat('Low', money(chart.summary?.low))
      ].join('');
      document.getElementById('stock-chart-status').textContent = chart.notice || 'Review-only chart. No order placed.';
      drawStockChart(points);
    }
    function chartStat(label, value) {
      return `<div class="chart-stat"><div class="metric-label">${escapeHtml(label)}</div><div class="metric-value">${value}</div></div>`;
    }
    function drawStockChart(points) {
      const canvas = document.getElementById('stock-chart-canvas');
      const ctx = canvas.getContext('2d');
      const width = canvas.width;
      const height = canvas.height;
      chartPlotPoints = [];
      hideChartTooltip();
      ctx.clearRect(0, 0, width, height);
      ctx.fillStyle = '#0d0d0e';
      ctx.fillRect(0, 0, width, height);
      const cleanPoints = (points || []).filter(point => Number.isFinite(Number(point.close)) && Number.isFinite(Number(point.timestamp)));
      const values = cleanPoints.map(point => Number(point.close));
      if (cleanPoints.length < 2) {
        drawChartAxes(ctx, width, height, 0, 1, []);
        ctx.fillStyle = '#9ca3af';
        ctx.font = '18px monospace';
        ctx.fillText('No chart data loaded', 82, 56);
        return;
      }
      const min = Math.min(...values);
      const max = Math.max(...values);
      const span = Math.max(0.01, max - min);
      const pad = {left: 72, right: 24, top: 24, bottom: 48};
      drawChartAxes(ctx, width, height, min, max, cleanPoints, pad);
      ctx.strokeStyle = values[values.length - 1] >= values[0] ? '#39d98a' : '#ff5c64';
      ctx.lineWidth = 4;
      ctx.beginPath();
      cleanPoints.forEach((point, index) => {
        const value = Number(point.close);
        const x = pad.left + (index / (cleanPoints.length - 1)) * (width - pad.left - pad.right);
        const y = height - pad.bottom - ((value - min) / span) * (height - pad.top - pad.bottom);
        chartPlotPoints.push({x, y, price: value, timestamp: Number(point.timestamp)});
        if (index === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.stroke();
    }
    function drawChartAxes(ctx, width, height, min, max, points, pad = {left: 72, right: 24, top: 24, bottom: 48}) {
      ctx.strokeStyle = 'rgba(255,255,255,0.16)';
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(pad.left, pad.top);
      ctx.lineTo(pad.left, height - pad.bottom);
      ctx.lineTo(width - pad.right, height - pad.bottom);
      ctx.stroke();
      ctx.fillStyle = '#9ca3af';
      ctx.font = '13px monospace';
      ctx.textAlign = 'right';
      ctx.textBaseline = 'middle';
      const yTicks = 4;
      for (let tick = 0; tick <= yTicks; tick += 1) {
        const ratio = tick / yTicks;
        const y = pad.top + ratio * (height - pad.top - pad.bottom);
        const price = max - ratio * Math.max(0.01, max - min);
        ctx.strokeStyle = 'rgba(255,255,255,0.07)';
        ctx.beginPath();
        ctx.moveTo(pad.left, y);
        ctx.lineTo(width - pad.right, y);
        ctx.stroke();
        ctx.fillText(`$${price.toFixed(2)}`, pad.left - 9, y);
      }
      if (!points.length) return;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      const xIndexes = [0, Math.floor((points.length - 1) / 3), Math.floor(((points.length - 1) * 2) / 3), points.length - 1];
      [...new Set(xIndexes)].forEach(index => {
        const x = pad.left + (index / (points.length - 1)) * (width - pad.left - pad.right);
        const label = formatChartTime(points[index].timestamp);
        ctx.strokeStyle = 'rgba(255,255,255,0.07)';
        ctx.beginPath();
        ctx.moveTo(x, pad.top);
        ctx.lineTo(x, height - pad.bottom);
        ctx.stroke();
        ctx.fillStyle = '#9ca3af';
        ctx.fillText(label, x, height - pad.bottom + 14);
      });
    }
    function formatChartTime(timestamp) {
      const date = new Date(Number(timestamp) * 1000);
      const options = selectedChartRange === '1d'
        ? {month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'}
        : {month: 'short', day: 'numeric'};
      return new Intl.DateTimeFormat(undefined, options).format(date);
    }
    function showChartTooltip(event) {
      if (!chartPlotPoints.length) return;
      const canvas = document.getElementById('stock-chart-canvas');
      const rect = canvas.getBoundingClientRect();
      const canvasX = (event.clientX - rect.left) * (canvas.width / rect.width);
      const nearest = chartPlotPoints.reduce((best, point) => Math.abs(point.x - canvasX) < Math.abs(best.x - canvasX) ? point : best, chartPlotPoints[0]);
      const scaleX = rect.width / canvas.width;
      const scaleY = rect.height / canvas.height;
      const tooltip = document.getElementById('stock-chart-tooltip');
      tooltip.hidden = false;
      tooltip.style.left = `${10 + nearest.x * scaleX}px`;
      tooltip.style.top = `${10 + nearest.y * scaleY}px`;
      tooltip.innerHTML = `<strong>${escapeHtml(selectedChartSymbol)}</strong><br>${escapeHtml(formatChartHoverTime(nearest.timestamp))}<br>${money(nearest.price)}`;
    }
    function hideChartTooltip() {
      const tooltip = document.getElementById('stock-chart-tooltip');
      if (tooltip) tooltip.hidden = true;
    }
    function formatChartHoverTime(timestamp) {
      const date = new Date(Number(timestamp) * 1000);
      return new Intl.DateTimeFormat(undefined, {dateStyle: 'medium', timeStyle: 'short'}).format(date);
    }
    function openStockTicket(symbol, side) {
      const normalizedSymbol = String(symbol || '').trim().toUpperCase();
      const normalizedSide = String(side || 'BUY').trim().toUpperCase() === 'SELL' ? 'SELL' : 'BUY';
      if (!normalizedSymbol) return;
      pendingStockTicket = {symbol: normalizedSymbol, side: normalizedSide};
      document.getElementById('stock-ticket-title').textContent = `${normalizedSymbol} ${normalizedSide}`;
      const sidePill = document.getElementById('stock-ticket-side');
      sidePill.textContent = normalizedSide;
      sidePill.className = `pill ${normalizedSide === 'BUY' ? 'buy' : 'sell'}`;
      document.getElementById('stock-ticket-note').textContent = 'Review-only ticket. No live stock order will be placed.';
      document.getElementById('stock-ticket-quantity').value = '1';
      document.getElementById('stock-ticket-confirm').value = '';
      document.getElementById('stock-ticket-drawer').hidden = false;
      document.getElementById('stock-ticket-quantity').focus();
    }
    function closeStockTicket() {
      document.getElementById('stock-ticket-drawer').hidden = true;
    }
    async function submitStockTicket() {
      const quantity = document.getElementById('stock-ticket-quantity').value;
      await submitStockIntent(pendingStockTicket.symbol, pendingStockTicket.side, quantity);
    }
    async function submitLiveStockTicket() {
      const quantity = document.getElementById('stock-ticket-quantity').value;
      const confirm = document.getElementById('stock-ticket-confirm').value;
      await runAction(`Sending ${pendingStockTicket.side} order for ${pendingStockTicket.symbol}...`, async () => {
        const response = await fetch('/api/stock-order', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({symbol: pendingStockTicket.symbol, side: pendingStockTicket.side, quantity, confirm})
        });
        const result = await response.json();
        document.getElementById('paper').textContent = JSON.stringify(result, null, 2);
        showOrderToast(result, 'Live order');
        setStatus(result.message || `${pendingStockTicket.side} order handled.`, response.ok && result.ok ? 'ok' : 'error');
        await refreshTickets();
        await refreshLiveOrders();
        if (response.ok && result.ok) closeStockTicket();
      }, true, false);
    }
    async function submitStockIntent(symbol, side, quantity) {
      await runAction(`Recording ${side} ticket for ${symbol}...`, async () => {
        const response = await fetch('/api/stock-order-intent', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({symbol, side, quantity})
        });
        const result = await response.json();
        document.getElementById('paper').textContent = JSON.stringify(result, null, 2);
        showOrderToast(result, 'Stock ticket');
        setStatus(result.message || `${side} ticket recorded.`, response.ok && result.ok ? 'ok' : 'error');
        await refreshTickets();
        await refreshLiveOrders();
        if (response.ok && result.ok) closeStockTicket();
      }, true, false);
    }
    document.addEventListener('click', event => {
      const button = event.target.closest('[data-stock-action]');
      if (!button) return;
      event.preventDefault();
      event.stopPropagation();
      openStockTicket(button.dataset.stockSymbol, button.dataset.stockAction);
    });
    function renderSettings(data) {
      document.getElementById('capabilities').innerHTML = Object.entries(data.capabilities || {}).map(([key, value]) => row(escapeHtml(key), escapeHtml(value))).join('');
    }
    function renderHardeningStatus(diagnostics, beta, paperEvidence = {}, liveReadiness = {}, publicRelease = {}, operationalHealth = {}) {
      const diagnosticSummary = diagnostics.summary || {};
      const betaSummary = beta.summary || {};
      const paperSummary = paperEvidence.summary || {};
      const publicSummary = publicRelease.summary || {};
      const operationalSummary = operationalHealth.summary || {};
      document.getElementById('hardening-status').innerHTML = [
        row('Setup diagnostics', `${diagnosticSummary.passed || 0} pass · ${diagnosticSummary.warn || 0} warn · ${diagnosticSummary.failed || 0} fail`, pill(diagnostics.status || 'unknown', diagnostics.ok ? 'buy' : 'trim')),
        row('Private beta', `${betaSummary.passed || 0} pass · ${betaSummary.warn || 0} warn · ${betaSummary.failed || 0} fail`, pill(beta.status || 'unknown', beta.ok ? 'buy' : 'sell')),
        row('Paper Evidence', `${paperSummary.market_sessions || 0} sessions · ${paperSummary.submitted || 0} orders · ${Number(paperSummary.rejection_rate_pct || 0).toFixed(1)}% rejected`, pill(paperEvidence.status || 'collecting', paperEvidence.ok ? 'buy' : 'trim')),
        row('Live Readiness', liveReadiness.message || 'Live readiness not checked yet.', pill(liveReadiness.status || 'locked', liveReadiness.ok ? 'buy' : 'sell')),
        row('Public Release', `${publicSummary.passed || 0} pass · ${publicSummary.failed || 0} fail`, pill(publicRelease.status || 'not_ready', publicRelease.ok ? 'buy' : 'trim')),
        row('Operational Health', `${operationalSummary.passed || 0} pass · ${operationalSummary.failed || 0} fail`, pill(operationalHealth.status || 'unknown', operationalHealth.ok ? 'buy' : 'trim')),
        row('Order reconciliation', 'Refreshes active Alpaca order IDs into the order truth stream.', pill('manual', 'quiet')),
        row('Emergency exit', 'Requires LIQUIDATE_ALL_POSITIONS and disables autopilot before sell orders.', pill('guarded', 'sell'))
      ].join('');
    }
    function normalizeUiTheme(theme) {
      const value = String(theme || 'retro').toLowerCase();
      return ['retro', 'clean', 'arcade', 'algo-desk', 'classic'].includes(value) ? value : 'clean';
    }
    function applyUiTheme(theme) {
      const nextTheme = normalizeUiTheme(theme);
      document.body.dataset.theme = nextTheme;
      document.body.classList.toggle('theme-retro', nextTheme === 'retro');
      document.body.classList.toggle('theme-clean', nextTheme === 'clean');
      document.body.classList.toggle('theme-arcade', nextTheme === 'arcade');
      document.body.classList.toggle('theme-algo-desk', nextTheme === 'algo-desk');
      document.body.classList.toggle('theme-classic', nextTheme === 'classic');
    }
    function renderUiThemeSettings(status) {
      const env = status.env || {};
      const theme = normalizeUiTheme(status.ui_theme || env.BONEHAWK_UI_THEME || 'retro');
      document.getElementById('ui-theme-settings').innerHTML = row(
        'UI style',
        'Retro matches the schematic. Clean keeps the earlier quieter control desk available.',
        settingSwitch([{label: 'Retro', value: 'retro'}, {label: 'Clean', value: 'clean'}, {label: 'Arcade', value: 'arcade'}, {label: 'Algo Desk', value: 'algo-desk'}, {label: 'Classic', value: 'classic'}], theme, 'setUiTheme')
      );
    }
    function renderAutopilotSettings(data) {
      const config = data.config || {};
      const kellyPct = Number((config.max_kelly_fraction || 0.05) * 100);
      const dailyLossPct = Math.max(2, Math.min(5, kellyPct / 2));
      document.getElementById('autopilot-settings').innerHTML = [
        row(
          'Autopilot',
          'Enabled lets Run Paper submit planned paper orders to Alpaca.',
          settingSwitch([{label: 'Off', value: 'false'}, {label: 'On', value: 'true'}], String(Boolean(config.enabled)), 'setAutopilotEnabled')
        ),
        row(
          'Autopilot mode',
          'Paper mode uses Alpaca paper trading. Live requires two confirmation gates.',
          settingSwitch([{label: 'Paper', value: 'paper'}, {label: 'Live', value: 'live', danger: true}], String(config.mode || 'paper'), 'setAutopilotMode')
        ),
        row(
          'Allow live Alpaca',
          'This must stay off until paper trading proves stable.',
          settingSwitch([{label: 'Off', value: 'false'}, {label: 'On', value: 'true', danger: true}], String(Boolean(config.allow_live)), 'setAutopilotAllowLive')
        ),
        row(
          'Dynamic sizing',
          `Bot decides dollars from Alpaca cash, buying power, stock price, probability, edge, and stop distance. Safety rails: ${escapeHtml(config.max_open_positions || 0)} max open positions · daily halt only after ${dailyLossPct.toFixed(1)}% cash drawdown.`,
          '<button data-action onclick="editAutopilotRisk()">Edit Safety</button>'
        ),
        row(
          'Agentic scan',
          `Window ${escapeHtml(config.scan_window_minutes || 5)}m · safety ceiling ${kellyPct.toFixed(1)}% · min probability ${Number((config.min_probability || 0.56) * 100).toFixed(1)}%`,
          '<button data-action onclick="editAutopilotAgentic()">Edit</button>'
        ),
        row(
          'Paper downtrend probes',
          'Paper mode can submit tiny test orders even when SPY/QQQ trend is down. Live mode stays blocked.',
          settingSwitch([{label: 'Off', value: 'false'}, {label: 'On', value: 'true'}], String(Boolean(config.paper_trade_downtrend)), 'setAutopilotPaperDowntrend')
        )
      ].join('');
    }
    async function runSetupDiagnostics() {
      await runAction('Running setup diagnostics...', async () => {
        const data = await getJson('/api/setup-diagnostics');
        document.getElementById('command-output').textContent = JSON.stringify(data, null, 2);
        setStatus(data.ok ? 'Setup diagnostics passed.' : 'Setup diagnostics need attention.', data.ok ? 'ok' : 'error');
        const beta = await getJson('/api/private-beta-check');
        renderHardeningStatus(data, beta);
      }, false, false);
    }
    async function runPrivateBetaCheck() {
      await runAction('Running private beta readiness check...', async () => {
        const data = await getJson('/api/private-beta-check');
        document.getElementById('command-output').textContent = JSON.stringify(data, null, 2);
        setStatus(data.ok ? 'Private beta check passed.' : 'Private beta check needs attention.', data.ok ? 'ok' : 'error');
        const diagnostics = await getJson('/api/setup-diagnostics');
        renderHardeningStatus(diagnostics, data);
      }, false, false);
    }
    async function runOrderReconciliation() {
      await runAction('Reconciling Alpaca order statuses...', async () => {
        const data = await getJson('/api/reconcile-orders', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({limit: 120})
        });
        document.getElementById('command-output').textContent = JSON.stringify(data, null, 2);
        setStatus(data.message || 'Order reconciliation finished.', data.ok ? 'ok' : 'error');
        await refreshTickets();
        await refreshLiveOrders();
      }, false, false);
    }
    async function runEmergencyLiquidation() {
      const confirm = window.prompt('Type LIQUIDATE_ALL_POSITIONS to cancel open orders, disable autopilot, and sell all available positions.', '');
      if (confirm === null) return;
      await runAction('Submitting emergency liquidation...', async () => {
        const response = await fetch('/api/emergency-liquidate', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({confirm})
        });
        const data = await response.json();
        document.getElementById('command-output').textContent = JSON.stringify(data, null, 2);
        setStatus(data.message || 'Emergency liquidation handled.', response.ok && data.ok ? 'ok' : 'error');
        await refreshTickets();
        await refreshLiveOrders();
        await refreshStatus();
      }, true, false);
    }
    function renderCommands(data) {
      const commands = (data.commands || []).filter(command => ESSENTIAL_COMMAND_IDS.has(command.id));
      const groups = commands.reduce((acc, command) => {
        const group = command.group || 'Commands';
        acc[group] = acc[group] || [];
        acc[group].push(command);
        return acc;
      }, {});
      document.getElementById('command-status').textContent = `${commands.length} app actions ready`;
      document.getElementById('command-groups').innerHTML = Object.entries(groups).map(([group, items]) => `
        <div class="command-section">
          <h2>${escapeHtml(group)}</h2>
          <div class="command-grid">
            ${items.map(commandCard).join('')}
          </div>
        </div>
      `).join('') || empty('No Settings commands are available.');
    }
    function commandCard(command) {
      const danger = command.requires_confirmation ? 'danger' : '';
      const inputCount = (command.inputs || []).length;
      const detail = [command.description, inputCount ? `${inputCount} input${inputCount === 1 ? '' : 's'}` : '', command.requires_confirmation ? `confirm ${command.confirm_phrase}` : ''].filter(Boolean).join(' · ');
      return `
        <div class="command-card ${danger}">
          <div>
            <div class="data-title"><strong>${escapeHtml(command.label)}</strong>${command.requires_confirmation ? pill('guarded', 'trim') : pill(command.action || 'run', 'quiet')}</div>
            <div class="data-sub">${escapeHtml(detail)}</div>
          </div>
          <div class="command-code">${escapeHtml(command.command)}</div>
          <div class="command-actions">
            ${pill(command.source || 'README', 'quiet')}
            <button data-action onclick="runReadmeCommand('${escapeHtml(command.id)}')">Run</button>
          </div>
        </div>
      `;
    }
    function filterVisibleRows(query) {
      const value = String(query || '').trim().toLowerCase();
      document.querySelectorAll('.data-row').forEach(row => {
        row.style.display = !value || row.textContent.toLowerCase().includes(value) ? '' : 'none';
      });
    }
    function openTypedSymbol(event) {
      if (event.key !== 'Enter') return;
      const symbol = String(event.target.value || '').trim().split(/\\s+/)[0];
      if (symbol) openStockChart(symbol);
    }
    async function runPaper(notify) {
      if (!notify) {
        await runAutopilotPaper();
        return;
      }
      await runAction(notify ? 'Running paper cycle and sending Telegram...' : 'Running paper cycle...', async () => {
        const data = await getJson(notify ? '/api/paper-cycle-notify' : '/api/paper-cycle', {method: 'POST'});
        document.getElementById('paper').textContent = data.stdout || data.stderr || JSON.stringify(data, null, 2);
        setStatus(data.ok ? 'Paper cycle finished.' : 'Paper cycle failed. See output panel.', data.ok ? 'ok' : 'error');
      }, false);
    }
    async function sendScannerAlerts() {
      await runAction('Sending scanner alert to Telegram...', async () => {
        const data = await getJson('/api/scanner-alerts', {method: 'POST'});
        document.getElementById('paper').textContent = data.message + "\\n\\n" + (data.stdout || data.stderr || '');
        setStatus(data.ok ? 'Scanner alert sent.' : 'Telegram send failed. See output panel.', data.ok ? 'ok' : 'error');
      }, false);
    }
    async function sendTradeIdeas() {
      await runAction('Sending trade ideas to Telegram...', async () => {
        const data = await getJson('/api/trade-idea-alerts', {method: 'POST'});
        document.getElementById('paper').textContent = data.message + "\\n\\n" + (data.stdout || data.stderr || '');
        setStatus(data.ok ? 'Trade ideas sent.' : 'Telegram send failed. See output panel.', data.ok ? 'ok' : 'error');
      }, false);
    }
    async function runReadmeCommand(id) {
      const catalog = await getJson('/api/commands');
      const command = (catalog.commands || []).find(item => item.id === id);
      if (!command) {
        setStatus('Command not found.', 'error');
        return;
      }
      const inputs = {};
      for (const input of command.inputs || []) {
        const value = window.prompt(input.label, input.default || '');
        if (value === null) return;
        inputs[input.name] = value;
      }
      let confirm = '';
      if (command.requires_confirmation) {
        const value = window.prompt(`Type ${command.confirm_phrase} to run ${command.label}.`, '');
        if (value === null) return;
        confirm = value;
      }
      await runAction(`Running ${command.label}...`, async () => {
        const result = await getJson('/api/commands/run', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({id, inputs, confirm})
        });
        document.getElementById('command-output').textContent = formatCommandResult(result);
        setStatus(result.ok ? `${command.label} finished.` : `${command.label} failed.`, result.ok ? 'ok' : 'error');
      }, false);
    }
    function formatCommandResult(result) {
      const parts = [
        `status: ${result.status || 'unknown'}`,
        `returncode: ${result.returncode ?? 'n/a'}`,
        '',
        'stdout:',
        result.stdout || '',
        '',
        'stderr:',
        result.stderr || ''
      ];
      if (result.pid) parts.splice(2, 0, `pid: ${result.pid}`);
      return parts.join('\\n');
    }
    async function setTradingMode(mode) {
      const nextMode = String(mode || '').toLowerCase();
      if (nextMode === 'live') {
        const ok = window.confirm('Switch bonehawk to LIVE mode? Alpaca live orders still need Alpaca live permission and confirmation.');
        if (!ok) return;
      }
      await runAction(`Switching to ${nextMode.toUpperCase()} mode...`, async () => {
        const data = await getJson('/api/trading-mode', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({mode: nextMode, confirm: nextMode === 'live' ? 'LIVE' : ''})
        });
        setModeButtons(data.mode);
        await refreshStatus();
        await refreshIntel();
        setStatus(data.message || `Trading mode switched to ${nextMode}.`, data.ok ? 'ok' : 'error');
      }, false);
    }
    async function setSettingsTradingMode(mode) {
      await setTradingMode(mode);
    }
    async function setUiTheme(theme) {
      const value = String(theme || 'retro').toLowerCase();
      await runAction(`Switching UI style to ${value.toUpperCase()}...`, async () => {
        const data = await getJson('/api/ui-theme', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({theme: value})
        });
        applyUiTheme(data.theme);
        await refreshStatus();
        setStatus(data.message || 'UI style updated.', data.ok ? 'ok' : 'error');
      }, false, false);
    }
    async function setAutopilotEnabled(value) {
      await setAutopilotSetting('enabled', value, '');
    }
    async function setAutopilotMode(mode) {
      const value = String(mode || '').toLowerCase();
      let confirm = '';
      if (value === 'live') {
        const typed = window.prompt('Type LIVE_ALPACA_AUTOPILOT to switch autopilot mode to live.', '');
        if (typed === null) return;
        confirm = typed;
      }
      await setAutopilotSetting('mode', value, confirm);
    }
    async function setAutopilotAllowLive(value) {
      const next = String(value || '').toLowerCase();
      let confirm = '';
      if (next === 'true') {
        const typed = window.prompt('Type ALLOW_LIVE_ALPACA to unlock live Alpaca permission.', '');
        if (typed === null) return;
        confirm = typed;
      }
      await setAutopilotSetting('allow_live', next, confirm);
    }
    async function setAutopilotPaperDowntrend(value) {
      await setAutopilotSetting('paper_trade_downtrend', value, '');
    }
    async function editAutopilotRisk() {
      const snapshot = await getJson('/api/autopilot');
      const config = snapshot.config || {};
      const maxPositions = window.prompt('Max open positions', config.max_open_positions || 3);
      if (maxPositions === null) return;
      await setAutopilotSetting('max_open_positions', maxPositions, '', true);
    }
    async function editAutopilotAgentic() {
      const snapshot = await getJson('/api/autopilot');
      const config = snapshot.config || {};
      const windowMinutes = window.prompt('Scan window in minutes (1-5)', config.scan_window_minutes || 5);
      if (windowMinutes === null) return;
      const kellyPct = window.prompt('Kelly safety ceiling (%)', Number((config.max_kelly_fraction || 0.05) * 100).toFixed(1));
      if (kellyPct === null) return;
      const minProbabilityPct = window.prompt('Minimum prediction probability (%)', Number((config.min_probability || 0.56) * 100).toFixed(1));
      if (minProbabilityPct === null) return;
      await setAutopilotSetting('scan_window_minutes', windowMinutes, '', false);
      await setAutopilotSetting('max_kelly_fraction', Number(kellyPct) / 100, '', false);
      await setAutopilotSetting('min_probability', Number(minProbabilityPct) / 100, '', true);
    }
    async function setAutopilotSetting(setting, value, confirm, refresh = true) {
      await runAction(`Updating autopilot ${setting}...`, async () => {
        const data = await getJson('/api/autopilot-settings', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({setting, value, confirm})
        });
        const autopilot = await getJson('/api/autopilot');
        renderAutopilot(autopilot);
        renderAutopilotSettings(autopilot);
        setStatus(data.message || 'Autopilot setting updated.', data.ok ? 'ok' : 'error');
      }, refresh, false);
    }
    async function runAction(label, action, refreshAfter = true, disableButtons = true) {
      if (disableButtons) setBusy(true);
      setStatus(label, 'muted');
      try {
        await action();
        if (refreshAfter) await refreshIntel();
      } catch (error) {
        setStatus(error.message || 'Something failed.', 'error');
        document.getElementById('paper').textContent = error.stack || String(error);
      } finally {
        if (disableButtons) setBusy(false);
      }
    }
    async function refreshAll() {
      await runAction('Refreshing dashboard...', async () => {
        await refreshStatus();
        await refreshCommands();
        await refreshIntel();
      }, false, false);
    }
    initSidebar();
    refreshAll();
    window.setInterval(refreshAutopilotBackgroundStatus, 10000);
    window.setInterval(refreshOverviewMetrics, 5000);
    window.setInterval(refreshLiveOrders, 5000);
  </script>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Run local trading bot dashboard.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    server = DashboardHTTPServer((args.host, args.port), DashboardService(), start_background=True)
    print(f"Dashboard running at http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
