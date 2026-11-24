# Strategy — Patient Breakout Bot

## The idea in one paragraph

Most pump.fun curves die fast; a few run hard and then dump. This bot does not
chase launches and does not buy dips. It waits until a curve has *proved* it is
alive — sustained buying across two consecutive windows, from several distinct
wallets, with the price up but not over-extended and sitting near its recent
high — then risks a small probe, adds once only into strength, and exits hard
on the first sign of trouble. Losers are cut small by construction: at most
0.10 SOL at cost in any one coin, at most 0.50 SOL exposed at once.

## Entry — "patient breakout with confirmation"

A curve may be bought only when **all** of these hold, evaluated on the batch
that carries its latest trade:

| Rule | Value | Why |
|---|---|---|
| Maturity | age ≥ 300 slots since CreateEvent | fresh launches are mostly bundled dumps; let the curve survive a little first |
| Recent window | 60 slots (~13 s) | short enough to catch a move early, long enough to see real flow |
| Net inflow, recent | ≥ 0.30 SOL (buys − sells) | buyers are actually putting SOL in |
| Net inflow, prior window | ≥ 0.05 SOL (the 60 slots before) | confirmation: this is sustained buying, not one lucky window |
| Buy trades, recent | ≥ 3 | not a single print |
| Distinct buyers, recent | ≥ 3 | not one wallet painting the tape |
| Largest buyer share | ≤ 60% of the window's buy SOL | no single wallet dominating the flow |
| Run-up | price +3% … +150% vs window start | up, but not already over-extended |
| Near high | price ≥ 90% of the max over the last 120 slots | buying a breakout, not a bounce off a lower level |
| Real SOL reserve | 3 … 90 SOL | liquid enough to exit, not so big the move is over |
| Creator sold | never | a creator who sold is a red flag; the coin is skipped for good |
| Cooldown / re-entry | 120 slots after an exit, max 2 entries per coin | no revenge-trading the same coin |

## Sizing and pyramid

- First buy (probe): **0.05 SOL**, capped by the harness's 1%-of-real-reserve
  per-reply limit (`max_buy_lamports`); skipped when the cap is under 0.01 SOL.
- One add of **0.05 SOL** only when the price is ≥ 2% above the last buy's
  price, the position is not underwater, and the recent window still shows
  ≥ 0.20 SOL net inflow. Position cap **0.10 SOL** at cost.
- Buys use 1500 bps slippage; sells use 10000 bps (a sell must get out).
- At most 5 open positions (the harness cap), so at most 0.50 SOL at cost is
  ever exposed. A 105,000-lamport tx fee is charged per order, so orders are
  never split: one whole buy, one whole sell.

## Exits — whole position, first trigger wins

| Trigger | Rule |
|---|---|
| Stop-loss | liquidation value < 88% of cost |
| Trailing stop | arms at +10%; exit on a 20% fall from the peak value |
| Take-profit | liquidation value ≥ 150% of cost — bank the win |
| Fade | net SOL over the last 30 slots < −0.40 SOL |
| Creator sell | any sell by the creator while held |
| Max hold | 1200 slots (~4–5 min), unless up ≥ 25% (then it may run) |
| Stale | no trade on the coin for 900 slots |

Liquidation value is the curve quote for selling the whole position
(`tokens·vq/(vt+tokens)`, integer math, exactly the curve's formula) minus
the 95 + 30 bps curve fees and the 105,000-lamport tx fee. A curve that
completes is closed by the replay itself; the bot never fights that.

## Guardrail pacing — cheap probe round trips

The run must end with a ≥ 0.01 SOL round trip on each of ≥ 20 distinct coins
and max drawdown ≤ 50%. The bot tracks its own fills and coins against a pace
(25 coins / 200 fills, spread over the 24 h window by block time). When it
lags the pace it opens a **0.012 SOL** probe on a liquid traded curve
(real SOL ≥ ~1.8) and sells it whole as soon as the buy lands. A probe costs
roughly 0.0005 SOL if it goes nowhere — cheap insurance for the guardrail —
and at most one probe is open at a time, so probes can never dominate PnL.

## What this is not

- Not the momentum pilot: that bot demands *accelerating* inflow (new half >
  old half), rides positions up to 2 SOL with a 30% trail, and has no
  take-profit, no creator-sell exit and no stale exit. This bot demands
  *sustained* inflow in both windows, caps a position at 0.10 SOL, banks wins
  at +50%, and exits on creator sells and staleness.
- Not the sniper pilot: no launch filter, no creator-buy window, no
  first-slots trading at all (min age 300 slots).
- Not the reversion pilot: it never buys a dip; every entry needs the price
  up and near its high.

## Determinism

No clock, no randomness, no environment reads, no network. All state is
per-process module state; iteration is over insertion-ordered dicts or the
batch's own event order. Two runs on the same data produce byte-identical
action logs (verified locally).
