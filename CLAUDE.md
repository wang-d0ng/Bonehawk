# Robinhood Swing Bot Rules

You are operating a Robinhood Crypto BTC swing bot. Follow these rules every session.

- Asset: `BTC-USD` only.
- Broker wrapper: use `python scripts/robinhood.py ...` for account, position, quote, orders, and orders.
- Notifications: use `bash scripts/telegram.sh "..."`
- Memory: read `memory/PROJECT-CONTEXT.md`, `memory/TRADING-STRATEGY.md`, and `memory/TRADE-LOG.md` before trading.
- No undocumented Robinhood private endpoints.
- No leverage, options, margin, or altcoins.
- Never write API keys or balances into files outside local `.env` and committed memory snapshots.
- Before any live order, confirm `TRADING_MODE=live`, account status is active, open position state matches the trade log, and the protective stop plan is valid.
- If a buy fills and stop placement fails, immediately close the position and send a critical Telegram alert.
