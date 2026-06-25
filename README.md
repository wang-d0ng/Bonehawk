# bonehawk

bonehawk is an Alpaca-first AI trading dashboard. It scans a broad stock universe, builds reviewable trade ideas, sends Telegram alerts, records buy/sell tickets, and can submit paper orders through Alpaca when keys are configured.

Use paper mode first. This project is trading software, not financial advice, and fast automated trading can lose money quickly.

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp env.template .env
cp config/autopilot.example.json config/autopilot.json
```

Add Alpaca paper keys to `.env`:

```bash
ALPACA_API_KEY=your-paper-key
ALPACA_SECRET_KEY=your-paper-secret
ALPACA_PAPER=true
ALPACA_ALLOW_LIVE=false
```

Run the dashboard:

```bash
python scripts/dashboard.py
```

Then open `http://127.0.0.1:8765`.

## First Run Setup

On first launch, Bonehawk opens a setup wizard when Alpaca paper keys or `config/autopilot.json` are missing. The wizard saves values into `.env`, creates `config/autopilot.json`, keeps live Alpaca permission off, and marks `BONEHAWK_SETUP_COMPLETE=true` when setup is complete.

Optional Telegram settings:

```bash
TELEGRAM_BOT_TOKEN=
ALLOWED_CHAT_IDS=
```

Telegram smoke test:

```bash
bash scripts/telegram.sh "Bonehawk Alpaca smoke test"
```

Telegram autopilot control:

```bash
python scripts/telegram_autopilot.py --once
python scripts/telegram_autopilot.py --loop
```

Only chats listed in `ALLOWED_CHAT_IDS` can control the bot. Supported commands:

- `/bh status`
- `/bh scan`
- `/bh run` for paper-mode execution only
- `/bh tickets`
- `/bh enable` or `/bh disable`
- `/bh paper-mode`
- `/bh size 25`
- `/bh positions 3`
- `/bh confidence 55`

## Market Scanner

To customize tracked assets:

```bash
cp config/watchlist.example.json config/watchlist.json
```

To scan beyond your selected watchlist:

```bash
cp config/market_universe.example.json config/market_universe.json
python scripts/build_market_universe.py --max-scan-symbols 250
```

`config/market_universe.json` holds the broader stock list. `max_scan_symbols` controls how many symbols the dashboard checks per refresh so public quote/news feeds do not get overloaded.

## Paper Cycle

Run a safe stock paper cycle:

```bash
python scripts/paper_cycle.py
```

Send the paper decision to Telegram too:

```bash
python scripts/paper_cycle.py --notify
```

The paper cycle scans the configured market universe, picks the strongest review-only setup, logs it in `memory/TRADE-LOG.md`, and does not place a live order.

## Alpaca Orders

Buy/Sell buttons in the dashboard create tickets. The `Record Ticket` button logs intent only. The `Send Live` button calls Alpaca:

- With `ALPACA_PAPER=true`, Alpaca receives a paper market order.
- With `ALPACA_PAPER=false`, Bonehawk also requires `ALPACA_ALLOW_LIVE=true` and the confirmation phrase `LIVE_ALPACA_ORDER`.

Order responses appear as top-right notifications and are logged in the Tickets tab with status, broker order id, and message details.

## Alpaca Autopilot

The Overview tab shows portfolio, scanner, news, risk flags, and Alpaca autopilot state in one place. Autopilot scans the broader stock universe, builds trade candidates from scanner/news, optional social feeds, momentum, volume, RSI, and SPY/QQQ trend checks, then sizes paper orders from available Alpaca cash, stock price, calibrated probability, edge, and stop distance.

Autopilot flow:

1. News and social research feeds go into Agent 1: Sentiment.
2. Alpaca account/order data and quote history go into Agent 2: Technical.
3. Agent 3: Portfolio Manager combines sentiment, technical probability, open positions, available cash, stock price, and dynamic Kelly-style risk sizing.
4. Agent 4: Executor submits eligible paper orders through Alpaca and records broker status/order IDs.
5. Performance Report summarizes submitted, rejected, planned, and blocked orders.
6. Telegram Alert is the notification channel; email is not used.

Default risk settings live in `config/autopilot.json`:

- `enabled`: whether Run Paper can submit paper orders
- `mode`: `paper` by default
- `max_trade_usd`: legacy fallback only; dynamic sizing does not use it as the trade decision
- `max_open_positions`: cap for planned open positions
- `min_confidence`: safety gate before an order is planned
- `scan_window_minutes`: short-window scan horizon, clamped to 1-30 minutes
- `max_kelly_fraction`: safety ceiling; the bot still chooses the actual fraction from account and market data
- `min_probability`: safety gate for calibrated probability before an order is planned
- `paper_trade_downtrend`: lets paper mode test small down-market probes; live mode still blocks downtrend buys
- `allow_live`: must remain `false` until paper testing is stable

Passive research can use public RSS/news by default and optional social feed templates from `.env`. Reddit RSS is opt-in with `BONEHAWK_REDDIT_RSS=true`; X/Twitter requires an allowed RSS/API bridge through `BONEHAWK_X_RSS_TEMPLATE`.

The prediction agent will use an optional XGBoost model at `models/xgboost_short_window.json` when the `ml` extra is installed. Without that model, it uses the built-in transparent fallback.

When Bonehawk is open, the background paper loop auto-runs Scan + Run Paper every 10 seconds. This loop is paper-only, has Start/Stop controls in the AI Desk, and refuses to run if autopilot is switched to live mode.

Live Alpaca autopilot is locked behind two gates: `mode=live` requires `LIVE_ALPACA_AUTOPILOT`, and `allow_live=true` requires `ALLOW_LIVE_ALPACA`.

## Desktop App

Run Bonehawk as a desktop app:

```bash
python scripts/desktop_app.py
```

Build a Mac desktop app bundle:

```bash
python scripts/build_desktop_app.py
```

The build output is `dist/Bonehawk.app`. The desktop app icon lives at `assets/app_icon.png`, with the macOS bundle icon generated at `assets/app_icon.icns`.

## Settings Commands

The Settings tab exposes the operational commands you are likely to need from the app, including Telegram test, Telegram autopilot once, Telegram autopilot loop, daily loop, and tests. It uses an allowlist instead of a free-form shell box, redacts likely sensitive output, and requires typed confirmation for guarded commands.

UI profiles are selectable in Settings:

- `Clean`: simpler control desk profile and the default
- `Arcade`: the neon cabinet profile
- `Algo Desk`: black CRT trading terminal profile inspired by the arcade algo desk reference
- `Classic`: quieter operational dashboard styling

## Daily Telegram Alerts

Copy the schedule example and adjust local times:

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

## Tests

```bash
python -m pytest
```
