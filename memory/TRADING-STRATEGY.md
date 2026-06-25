# Trading Strategy

Robinhood adaptation of the BTC swing strategy from the Opus 4.7 setup guide.

## Scope

- Platform: Robinhood Crypto Trading API
- Asset: BTC-USD spot only
- Hold period: 1-7 days
- One open BTC position at a time
- No leverage, no margin, no options, no altcoins
- Max two entries per rolling 7 days

## Risk

- Starting equity: set in `memory/PROJECT-CONTEXT.md`
- A grade: risk up to 1.0% of equity
- B grade: risk up to 0.5% of equity
- Below B: skip
- Quarterly drawdown halt: 15% from starting equity

## Setups

1. `catalyst_driven_breakout`
2. `sentiment_extreme_reversion`
3. `funding_flip_divergence`
4. `onchain_accumulation_base`

## Buy-Side Gate

All must pass:

- Research report has grade A or B.
- Playbook setup matches one of the four setups above.
- Current BTC position is zero.
- Rolling 7-day entries plus this one is at most 2.
- Stop price is at a documented technical level.
- Stop is at least 0.5% below entry.
- Target is at least 2R.
- Account is not in cooldown or drawdown halt.

## Management

- At +1R: move stop to breakeven plus 0.2% buffer.
- At +1.5R: sell 30%.
- At +2R: sell 30% of remaining and move stop to +1R below current.
- Runner: trail using max of 3x ATR below current price or recent 4h swing low.
