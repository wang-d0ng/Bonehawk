# bonehawk

bonehawk is a Robinhood + Alpaca version of the `Opus 4.7 Trading Bot — Setup Guide` project. It keeps the guide's structure: wrapper scripts, Git-backed memory files, routine prompts, and Telegram notifications. Robinhood remains available for crypto and guarded MCP stock tickets; Alpaca is the paper-first broker path for automated stock execution.

Important: this uses Robinhood's official **Crypto Trading API**, which supports programmatic crypto market data, account access, and crypto order placement. It is built for `BTC-USD`. It does not use Robinhood's private, undocumented stock endpoints.

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp env.template .env
```

Fill in `.env` with Robinhood Crypto API credentials, Alpaca paper API credentials, and Telegram settings.

Read-only smoke tests:

```bash
python scripts/robinhood.py account
python scripts/robinhood.py quote BTC-USD
python scripts/robinhood.py position
python scripts/robinhood.py orders
```

Telegram test:

```bash
bash scripts/telegram.sh "Robinhood swing bot smoke test"
```

Run tests:

```bash
python -m pytest
```

Run a safe paper cycle:

```bash
python scripts/paper_cycle.py
```

Send the paper decision to Telegram too:

```bash
python scripts/paper_cycle.py --notify
```

Alpaca autopilot paper trading:

```bash
cp config/autopilot.example.json config/autopilot.json
```

Add Alpaca paper keys to `.env`:

```bash
ALPACA_API_KEY=your-paper-key
ALPACA_SECRET_KEY=your-paper-secret
ALPACA_PAPER=true
ALPACA_ALLOW_LIVE=false
```

Then open the dashboard and use the **Autopilot** tab. `Scan` builds a paper plan; `Run Paper` submits paper orders to Alpaca when keys are configured.

On first launch, the dashboard opens a setup wizard if Alpaca paper keys or the local autopilot config are missing. The wizard saves values into `.env`, creates `config/autopilot.json`, keeps live Alpaca permission off, and marks `BONEHAWK_SETUP_COMPLETE=true` once the required paper setup exists. Leave optional Robinhood or Telegram fields blank to keep existing values unchanged.

Run the local dashboard:

```bash
python scripts/dashboard.py
```

Then open `http://127.0.0.1:8765`.

Run Bonehawk as a desktop app:

```bash
python scripts/desktop_app.py
```

Build a Mac desktop app bundle:

```bash
python scripts/build_desktop_app.py
```

The build output is `dist/Bonehawk.app`. If the desktop packages are missing, run `python -m pip install -r requirements.txt` inside the project virtual environment first.

The desktop app icon lives at `assets/app_icon.png`, with the macOS bundle icon generated at `assets/app_icon.icns`.

To customize monitored assets, copy the example watchlist:

```bash
cp config/watchlist.example.json config/watchlist.json
```

Edit `config/watchlist.json` with manually tracked positions, aliases, and risk settings. The market scanner is not limited to this file.

To scan beyond your selected watchlist, copy or build the market universe:

```bash
cp config/market_universe.example.json config/market_universe.json
python scripts/build_market_universe.py --max-scan-symbols 250
```

`config/market_universe.json` holds the broader stock list. `max_scan_symbols` controls how many symbols the dashboard checks per refresh so public quote/news feeds do not get overloaded.

## Trading Commands

```bash
python scripts/robinhood.py account
python scripts/robinhood.py position
python scripts/robinhood.py quote BTC-USD
python scripts/robinhood.py orders
python scripts/robinhood.py buy --usd 50
python scripts/robinhood.py sell --pct 30
python scripts/robinhood.py stop --base 0.001 --stop-price 60000 --limit 59700
python scripts/robinhood.py cancel ORDER_ID
python scripts/robinhood.py cancel-all
python scripts/robinhood.py close
```

`TRADING_MODE=paper` blocks order placement commands. Set `TRADING_MODE=live` only after read-only smoke tests pass and you understand the risk.

The dashboard also includes a Paper/Live mode switch. Switching to live requires confirmation and updates only `TRADING_MODE` in `.env`.

The dashboard Command Center exposes these README setup, smoke-test, scanner, Telegram, MCP, and trading commands as buttons. It uses an allowlist instead of a free-form shell box, redacts likely sensitive output, and requires typed confirmation for live/destructive commands.

## Robinhood Notes

- Official docs: <https://docs.robinhood.com/crypto/trading/>
- Auth uses `x-api-key`, `x-signature`, and `x-timestamp`.
- The signature message is `api_key + timestamp + path + method + body`.
- The crypto API is available to Robinhood Crypto customers in the United States.
- Rate limit guidance from Robinhood: 100 requests/minute per user account, 300/minute burst.

## Robinhood Integration

Run a sanitized read-only smoke check:

```bash
python scripts/robinhood_smoke.py
```

The dashboard includes a Robinhood tab with:

- Crypto API connection status
- Masked crypto account number
- Crypto holdings
- Open crypto orders
- BTC/ETH/SOL quotes
- Current trading mode guard

The Robinhood Crypto API does not place stock orders. Stock trading remains review-only until a supported stock broker or Robinhood Agentic Trading connector is added.

## Robinhood Agentic Trading

bonehawk supports Robinhood's official Agentic Trading MCP setup for stocks. Add the MCP server to Codex:

```bash
codex mcp add robinhood-trading --url https://agent.robinhood.com/mcp/trading
```

Then authenticate and complete Robinhood's desktop onboarding:

```bash
codex mcp login robinhood-trading
```

The dashboard includes an Agentic tab that checks whether the Robinhood Trading MCP URL is configured in Codex and whether the stock-order connector is callable. Buy/Sell buttons first create review-only stock tickets. Live stock orders require all of these gates:

- `codex mcp login robinhood-trading` has completed OAuth/onboarding.
- `TRADING_MODE=live`
- `BONEHAWK_STOCK_TRADING_MODE=live`
- `BONEHAWK_STOCK_ORDER_CONNECTOR=codex_mcp`
- The live ticket confirmation phrase: `LIVE_STOCK_ORDER`

Keep `BONEHAWK_STOCK_TRADING_MODE=review` and `BONEHAWK_STOCK_ORDER_CONNECTOR=disabled` while testing.

If a live stock send fails, use `Agentic -> Connector Diagnostics`. It performs a safe preflight only, shows the current mode gates, and prints a redacted `codex mcp list` result so you can see whether the issue is disabled settings, Paper mode, missing OAuth/onboarding, or the Codex bridge.

## Stock Trading Note

Robinhood's public HTTP docs expose crypto trading. Robinhood also advertises Agentic Trading through a Robinhood MCP/agentic account surface for stocks. This project does not use private app endpoints. Stock execution is routed through the guarded Robinhood Agentic/MCP connector bridge when explicitly enabled.

## Market Scanner

The dashboard now includes:

- Stock quote snapshots for tracked positions
- Portfolio value, daily move, and unrealized P&L estimates
- News scanning through Google News RSS
- SEC insider filing matching with ticker/alias safeguards
- Broad-market scans from `config/market_universe.json`
- Telegram scanner alerts for symbols that need review
- Trade ideas with review action, current price, stop, target, and Telegram delivery
- Quick-growth candidates for short-term review using news, momentum, volume, RSI, and market trend filters
- Technical filters using moving averages, RSI, volume spike ratio, and SPY/QQQ market trend
- Decision logging in `logs/decision_log.jsonl`
- Portfolio sync status that separates manual stock positions from Robinhood Crypto holdings

Dashboard tabs:

- Portfolio
- Commands
- Trade Ideas
- Growth
- Stocks
- Autopilot
- Agentic
- Robinhood
- Scanner
- News
- Tickets
- Logs
- Settings

Scanner alerts are review signals only. They do not place live stock orders.

## Alpaca Autopilot

The Autopilot tab scans the broader stock universe, builds trade candidates from scanner/news, momentum, volume, RSI, and SPY/QQQ trend checks, then applies risk limits before creating paper orders.

Default risk settings live in `config/autopilot.json`:

- `enabled`: whether Run Paper can submit paper orders
- `mode`: `paper` by default
- `max_trade_usd`: dollar size for each paper order
- `max_open_positions`: cap for planned open positions
- `min_confidence`: minimum score before an order is planned
- `allow_live`: must remain `false` until paper testing is stable

Live Alpaca autopilot is intentionally locked behind two gates: `mode=live` requires `LIVE_ALPACA_AUTOPILOT`, and `allow_live=true` requires `ALLOW_LIVE_ALPACA`. Keep both off while testing.

## Daily Telegram Alerts

Copy the schedule example and adjust the local times:

```bash
cp config/daily_schedule.example.json config/daily_schedule.json
```

Send one alert immediately:

```bash
python scripts/daily_scheduler.py --once morning
python scripts/daily_scheduler.py --once midday
python scripts/daily_scheduler.py --once end_of_day
```

Run the scheduler loop:

```bash
python scripts/daily_scheduler.py --loop
```

Morning sends trade ideas, midday sends scanner alerts, and end of day sends a portfolio summary.

## Safety

This project can place live crypto orders if configured. AI trading can lose money quickly. Keep the account budget small, use read-only keys while testing, and never commit `.env`.
