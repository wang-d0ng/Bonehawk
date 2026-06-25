# Bonehawk Rules

You are operating an Alpaca-first AI trading dashboard. Follow these rules every session.

## Core Safety

- Default to Alpaca paper trading.
- Never hardcode API keys, Telegram tokens, or account identifiers.
- Live Alpaca orders require `ALPACA_PAPER=false`, `ALPACA_ALLOW_LIVE=true`, and the confirmation phrase `LIVE_ALPACA_ORDER`.
- Autopilot live mode requires `LIVE_ALPACA_AUTOPILOT`; live permission requires `ALLOW_LIVE_ALPACA`.
- Scanner output and paper-cycle output are signals, not guarantees.

## Primary Files

- Broker wrapper: `scripts/alpaca_connector.py`
- Dashboard: `scripts/dashboard.py`
- Autopilot: `scripts/autopilot.py`
- Paper cycle: `scripts/paper_cycle.py`
- Market universe: `config/market_universe.json`
- Watchlist: `config/watchlist.json`

## Verification

- Run `python -m pytest` after broker, dashboard, setup, or trading-flow changes.
- Keep the setup wizard, `.env` template, README, and dashboard buttons aligned.
