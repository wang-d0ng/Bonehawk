# Trading Strategy

Bonehawk scans a broad stock universe, ranks review-only trade ideas, and routes approved paper orders through Alpaca.

## Scope

- Broker: Alpaca
- Default mode: paper trading
- Assets: US stocks from `config/market_universe.json`
- Order style: market order tickets with risk limits
- Automation: autopilot can submit paper orders only when enabled

## Risk

- Use paper trading until execution and alerts are stable.
- Keep `ALPACA_ALLOW_LIVE=false` until live trading is deliberately unlocked.
- Cap each planned order with `max_trade_usd`.
- Cap exposure with `max_open_positions`.
- Skip orders below `min_confidence`.

## Setups

- Momentum plus volume expansion
- Quick-growth news catalyst
- RSI and moving-average confirmation
- SPY/QQQ trend alignment

## Execution Gates

- Manual tickets: `Record Ticket` logs intent only.
- Alpaca paper orders: `Send Live` with `ALPACA_PAPER=true` submits to Alpaca paper trading.
- Alpaca live orders: require `ALPACA_PAPER=false`, `ALPACA_ALLOW_LIVE=true`, and `LIVE_ALPACA_ORDER`.
- Autopilot live mode: requires `LIVE_ALPACA_AUTOPILOT` and `ALLOW_LIVE_ALPACA`.
