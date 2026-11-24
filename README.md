# Patient Breakout Bot — Pump.fun Replay Sprint

[![make.fun · u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122](https://make.fun/badge/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122.svg)](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122?utm_source=github&utm_medium=readme&utm_campaign=u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122)
[![rank](https://make.fun/badge/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/rezimx-btc/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/rank.svg)](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122)

A paper-trading bot for the [Pump.fun Replay Sprint](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122)
(`u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122`, harness
`pumpfun-replay/v2`). It trades 10 SOL of paper money over a 24 h replay of
pump.fun bonding-curve events. Ranking is by net PnL; a prize needs PnL > 0
after all fees.

## Strategy (summary)

Patient breakout with confirmation + hard exits — see [STRATEGY.md](STRATEGY.md)
for the full write-up. In short: buy only a curve that is at least 300 slots
old and shows sustained buying across **two** consecutive 60-slot windows
(net inflow ≥ 0.30 SOL recent, ≥ 0.05 SOL prior), from ≥ 3 distinct buyers,
price up 3–150% and ≥ 90% of its 120-slot high, real SOL reserve 3–90, creator
never sold. Probe 0.05 SOL, one 0.05 SOL add into strength, cap 0.10 SOL per
position, max 5 positions. Exit whole on stop-loss (−12%), trailing stop
(−20% from peak, armed at +10%), take-profit (+50%), fade, creator sell, max
hold 1200 slots, or staleness. Cheap 0.012 SOL probe round trips keep the
guardrail pace (≥ 20 coins with ≥ 0.01 SOL round trips) when entries lag.

This is my own design, written in Python (stdlib only) for this entry. It is
not a copy of any of the kit's house pilots: unlike the momentum pilot it
demands *sustained* inflow in both windows instead of *accelerating* inflow,
caps a position at 0.10 SOL instead of 2 SOL, banks wins at +50%, and exits
on creator sells and staleness; unlike the sniper pilot it never trades
launches (min age 300 slots); unlike the reversion pilot it never buys a dip.

## Repo

- `main.py` — the bot (stdlib only; `vendor/makefun_bot.py` is the kit's SDK
  shim, copied byte for byte)
- `STRATEGY.md` — the strategy, parameters and the reason for each choice
- `LICENSE` — MIT
- `.makefun/owner.json` — entry owner wallet

## Grade locally

Docker is not available on the grading VM used here, so grade with the
process driver (Python 3.13 is what the driver looks for; on this machine a
`python3.13` shim pointing at the system Python 3.12 was enough — the bot
uses stdlib features present in both):

```sh
node <kit>/cli/makefun.mjs grade-local --kit <kit> --bot <repo> --driver process
node <kit>/cli/makefun.mjs grade-local --kit <kit> --bot <repo> --driver process --stress 8 --stress-jobs 4
```

The grader runs the bot twice; the two action logs must match byte for byte.
`--driver docker` fails on this machine (`docker is not runnable: spawnSync
docker ENOENT`).

## Dev results (frozen dev window, 24 h, 2–3 Nov 2026)

From `grade-local --driver process` on the v1 config (the config shipped
here). Reported honestly: **this entry loses money on the dev window.**

| Metric | Value |
|---|---|
| Net PnL | **−0.357647431 SOL** (−3.58% return) |
| 90% interval | −0.673950928 … −0.074967605 SOL |
| Fills / coins / round trips | 271 / 73 / 113 |
| Win rate (round trips) | 19.5% |
| Max drawdown | 4.08% (cap 50%) |
| Rejected / failed orders | 2 / 7 |
| Fees | 0.191642865 SOL curve + 0.029190000 SOL tx |
| Net PnL at 2x fees | −0.572137652 SOL |
| Guardrails | **met** — 73 coins hold a ≥ 0.01 SOL round trip (bar: 20), drawdown ok |
| Prize eligibility | ranked yes, prize **no** (below hold cash) |
| Top coins | 3 coins made 52% of the gains; without them −0.51 SOL |
| Determinism | L1 regrade reproduced byte for byte; re-running `grade-local` gave the identical `action_log_sha256` (`caf85ca5…5570b3b`) |
| Stress (8 block surrogates) | median −0.18, p10 −0.34, p90 +0.11 SOL; **6 of 8 lost money**; dev score above 0 of 8 — warning: half or more copies lost money |

Tuning tried (one parameter at a time, two rounds, both reverted):

1. Stop-loss 12% → 6%: **−0.402724604 SOL**, win rate 16.8% — worse. The
   tighter stop cut winners that dip first.
2. Max run-up 150% → 60%: **−0.432641373 SOL**, win rate 18.2% — worse.
   Capping the run-up removed the entries that produced the big winners.

The shipped config is the best of the three. The honest read: on this dev
window the breakout entries win only ~1 in 5 round trips and the gains are
concentrated in 3 coins; the strategy is not profitable here and the final
window is new data, so nothing about a prize is promised.

## Smoke test

```sh
PYTHONHASHSEED=0 python3 -P -s main.py < /dev/null   # exit 1, clean ProtocolError: input ended before hello
```

With no harness input the bot cannot run — it fails neatly with a
`ProtocolError` (exit 1), it does not hang or exit 0.
