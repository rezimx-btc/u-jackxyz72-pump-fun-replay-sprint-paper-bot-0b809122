"""Patient breakout bot for the pump.fun replay sprint (agent-fun/replay/v2).

My own design, written for this entry. Idea: most pump.fun curves die fast, a few
run hard and then dump. So I wait for a curve to prove it is alive -- sustained
buying across TWO consecutive windows, from several distinct wallets, price up
but not over-extended and sitting near its recent high -- then I risk a small
probe, add once only into strength, and get out hard on the first sign of
trouble. Nothing here chases a launch: a curve must be at least MIN_AGE slots
old before I touch it.

Deterministic by construction: no clock, no randomness, no I/O, no env reads.
All iteration is over insertion-ordered dicts or the batch's own event order.
Diagnostics go to stderr only; stdout carries replies.
"""

import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bot  # noqa: E402  (after the sys.path line: -P leaves the script directory off sys.path)

LAMPORTS = makefun_bot.LAMPORTS_PER_SOL
CURVE_FEE_BPS = 125  # protocol 95 + creator 30 bps, what a sell pays on the curve

# --- entry: two consecutive windows of sustained buying ---------------------
WIN = 60            # recent window, slots (~13 s)
PRIOR_WIN = 60      # the window right before it: confirmation, not a spike
MIN_NET_SOL = 0.3   # net SOL in (buys minus sells) over the recent window
MIN_PRIOR_NET_SOL = 0.05  # prior window must also be net positive
MIN_BUYS = 3        # buy trades in the recent window
MIN_BUYERS = 3      # distinct buying wallets in the recent window
MAX_TOP_SHARE = 0.60  # largest single buyer, share of the window's buy SOL
MIN_UP = 0.03       # price up vs window start, at least
MAX_UP = 1.50       # ...and at most (already ran too far)
NEAR_HIGH = 0.90    # price now, share of the max price over the last 2 windows
MIN_AGE = 300       # slots since CreateEvent before an entry is allowed
MIN_RQ_SOL = 3.0    # real SOL reserve at entry, at least (liquid enough to exit)
MAX_RQ_SOL = 90.0   # ...and at most (the move is not already over)
KEEP_SLOTS = 2 * WIN + 20  # tape kept per curve

# --- sizing ------------------------------------------------------------------
PROBE_SOL = 0.05    # first buy of a position
ADD_SOL = 0.05      # the one pyramid add
MAX_POSITION_SOL = 0.10  # most SOL at cost in one position
MIN_BUY_SOL = 0.01  # smallest buy worth the 105k-lamport tx fee
BUY_SLIP_BPS = 1500
SELL_SLIP_BPS = 10_000

# --- exits (whole position, first trigger wins) ------------------------------
STOP_LOSS = 0.12    # exit when liquidation value < cost * (1 - STOP_LOSS)
TRAIL_ARM = 0.10    # trailing stop arms once value >= cost * (1 + TRAIL_ARM)
TRAIL = 0.20        # ...then exit on a fall of TRAIL from the peak value
TAKE_PROFIT = 0.50  # bank it when value >= cost * (1 + TAKE_PROFIT)
FADE_WIN = 30       # exit when net SOL over the last FADE_WIN slots < -FADE_SOL
FADE_SOL = 0.4
MAX_HOLD = 1200     # slots held, then exit (unless already far up, see below)
HOLD_IF_UP = 0.25   # ...the time limit does not apply while up at least this much
STALE_SLOTS = 900   # exit when no trade has hit the coin for this long
COOLDOWN = 120      # slots to wait after an exit before re-entering the coin
MAX_ENTRIES = 2     # most entries per curve over the run

# --- pyramid -----------------------------------------------------------------
ADD_STEP = 0.02     # add only when price is this far above the last buy's price
ADD_MIN_FLOW_SOL = 0.2  # ...and the recent window still shows net inflow

# --- guardrail pacing ---------------------------------------------------------
TARGET_COINS = 25   # round-trip coins to aim for (bar is 20)
TARGET_FILLS = 200  # fills to aim for
PACE_SECS = 86_400  # the full 24 h window
PROBE_RT_SOL = 0.012  # probe round-trip size (>= 0.01 SOL bar, small loss if wrong)
MAX_PROBES = 1      # at most one probe round trip open at a time


class Tape:
    """Per-curve tape: what the bot has seen of one curve."""

    __slots__ = ("created", "creator", "trades", "entries", "last_exit",
                 "dev_sold", "last_trade_slot")

    def __init__(self):
        self.created = None       # slot of its CreateEvent, None until seen
        self.creator = None       # creator wallet from the CreateEvent
        self.trades = []          # (slot, is_buy, sol_lamports, user, price)
        self.entries = 0          # entries opened on this curve so far
        self.last_exit = -10 ** 18
        self.dev_sold = False     # the creator has sold at some point
        self.last_trade_slot = -1


class Held:
    """An open main position, as the bot tracks it."""

    __slots__ = ("opened", "peak", "invested", "exiting", "filled",
                 "last_add_price", "last_value")

    def __init__(self, opened, invested, last_add_price):
        self.opened = opened
        self.peak = 0             # best liquidation value seen (lamports)
        self.invested = invested  # SOL sent in buys (fees included)
        self.exiting = False
        self.filled = False       # some buy of this position filled
        self.last_add_price = last_add_price
        self.last_value = 0       # liquidation value at the latest batch


# --- module state (all of it reset per process run, hence deterministic) ------
tapes = {}
held = {}
probes = {}          # mint -> True once its probe buy has landed (selling next)
my_fills = 0
my_coins = set()     # mints with a filled order (membership only, never iterated)
first_time = None    # block time of the first event seen
last_time = None


def tape_of(mint):
    t = tapes.get(mint)
    if t is None:
        t = Tape()
        tapes[mint] = t
    return t


def price_of(curve):
    """Lamports per token unit, as a float ratio (comparisons only)."""
    vt = curve.virtual_token_reserves
    if vt <= 0:
        return 0.0
    return curve.virtual_quote_reserves / vt


def liquid_value(state, mint, tokens):
    """What selling `tokens` now would pay, curve + tx fees deducted (lamports)."""
    curve = state.curves.get(mint)
    if curve is None or tokens <= 0:
        return 0
    vq = curve.virtual_quote_reserves
    vt = curve.virtual_token_reserves
    q = (tokens * vq) // (vt + tokens)
    fee = -(-q * 95 // 10_000) + -(-q * 30 // 10_000)  # ceil, protocol + creator
    return q - fee - state.tx_fee_lamports()


def window_stats(t, now):
    """One pass over a tape: recent + prior window flow, prices, window high.

    Returns None when the tape cannot support a signal yet.
    """
    from_recent = now - WIN
    from_prior = now - WIN - PRIOR_WIN
    buy_sol = 0
    sell_sol = 0
    buys = 0
    prior_net = 0
    start_price = -1.0
    last_price = 0.0
    high = 0.0
    buyers = {}
    for (slot, is_buy, sol, user, price) in t.trades:
        if slot <= from_prior:
            start_price = price  # last trade at/before the prior window start
            continue
        if price > high:
            high = price
        last_price = price
        signed = sol if is_buy else -sol
        if slot <= from_recent:
            prior_net += signed
            if start_price < 0:
                start_price = price
        else:
            if is_buy:
                buy_sol += sol
                buys += 1
                buyers[user] = buyers.get(user, 0) + sol
            else:
                sell_sol += sol
    if last_price <= 0.0 or start_price <= 0.0:
        return None
    top = 0
    for v in buyers.values():
        if v > top:
            top = v
    return {
        "net": buy_sol - sell_sol,
        "prior_net": prior_net,
        "buys": buys,
        "buyers": len(buyers),
        "top_share": (top / buy_sol) if buy_sol > 0 else 1.0,
        "up": last_price / start_price - 1.0,
        "near_high": last_price / high if high > 0 else 0.0,
        "last_price": last_price,
    }


def net_flow(t, now, window):
    """Net SOL (buys minus sells) over the last `window` slots."""
    net = 0
    for i in range(len(t.trades) - 1, -1, -1):
        slot, is_buy, sol, _user, _price = t.trades[i]
        if slot <= now - window:
            break
        net += sol if is_buy else -sol
    return net


def entry_ok(state, mint, now):
    """The entry signal: mature curve, sustained inflow, bounded run-up, near high."""
    t = tapes[mint]
    curve = state.curves.get(mint)
    if curve is None or curve.complete or t.dev_sold:
        return None
    if t.created is None or now - t.created < MIN_AGE:
        return None
    rq_sol = curve.real_quote_reserves / LAMPORTS
    if rq_sol < MIN_RQ_SOL or rq_sol > MAX_RQ_SOL:
        return None
    if t.entries >= MAX_ENTRIES or now - t.last_exit < COOLDOWN:
        return None
    s = window_stats(t, now)
    if s is None:
        return None
    if s["net"] < int(MIN_NET_SOL * LAMPORTS):
        return None
    if s["prior_net"] < int(MIN_PRIOR_NET_SOL * LAMPORTS):
        return None
    if s["buys"] < MIN_BUYS or s["buyers"] < MIN_BUYERS:
        return None
    if s["top_share"] > MAX_TOP_SHARE:
        return None
    if s["up"] < MIN_UP or s["up"] > MAX_UP:
        return None
    if s["near_high"] < NEAR_HIGH:
        return None
    return s


def on_batch(state, batch):
    global my_fills, first_time, last_time
    now = batch["slot"]
    tx = state.tx_fee_lamports()
    caps = state.hello["caps"]
    max_orders = caps["max_orders_per_batch"]

    # --- fold this batch's fills and events into the tapes -------------------
    for f in batch["fills"]:
        if f["status"] == "filled" and not f["client_id"].startswith("_"):
            my_fills += 1
            my_coins.add(f["mint"])
    traded = []          # mints with a trade in this batch, in event order
    seen_traded = set()
    dev_sold_now = set()
    for e in batch["events"]:
        if e.get("block_time") is not None:
            bt = e["block_time"]
            if first_time is None:
                first_time = bt
            if last_time is None or bt > last_time:
                last_time = bt
        mint = e["mint"]
        if e["event"] == "CreateEvent":
            t = tape_of(mint)
            t.created = e["slot"]
            t.creator = e["creator"]
        elif e["event"] == "TradeEvent":
            t = tape_of(mint)
            sol = int(e["sol_amount"])
            vt = int(e["virtual_token_reserves"])
            price = (int(e["virtual_quote_reserves"]) / vt) if vt > 0 else 0.0
            t.trades.append((e["slot"], e["is_buy"], sol, e["user"], price))
            t.last_trade_slot = e["slot"]
            if not e["is_buy"] and t.creator is not None and e["user"] == t.creator:
                t.dev_sold = True
                dev_sold_now.add(mint)
            if mint not in seen_traded:
                seen_traded.add(mint)
                traded.append(mint)
    for mint in traded:
        t = tapes[mint]
        cut = 0
        while cut < len(t.trades) and t.trades[cut][0] <= now - KEEP_SLOTS:
            cut += 1
        if cut:
            del t.trades[:cut]

    pending_mints = {}
    for po in state.pending.values():
        pending_mints[po.order["mint"]] = pending_mints.get(po.order["mint"], 0) + 1

    orders = []

    # --- exits first ----------------------------------------------------------
    for mint in list(held.keys()):
        h = held[mint]
        pos = state.positions.get(mint)
        curve = state.curves.get(mint)
        if pos is None:
            if mint not in pending_mints:
                del held[mint]
                t = tape_of(mint)
                if h.filled:
                    t.last_exit = now
                else:
                    t.entries -= 1
            continue
        h.filled = True
        if curve is None or curve.complete:
            continue
        tokens = pos.token_amount
        value = liquid_value(state, mint, tokens)
        cost = pos.cost_lamports
        if value > h.peak:
            h.peak = value
        h.last_value = value
        t = tape_of(mint)
        exit_now = h.exiting
        if not exit_now and value * 100 < cost * (100 - int(STOP_LOSS * 100)):
            exit_now = True
        if (not exit_now and h.peak >= cost * (100 + int(TRAIL_ARM * 100)) // 100
                and value * 100 < h.peak * (100 - int(TRAIL * 100))):
            exit_now = True
        if not exit_now and value * 100 >= cost * (100 + int(TAKE_PROFIT * 100)):
            exit_now = True
        if not exit_now and net_flow(t, now, FADE_WIN) < -int(FADE_SOL * LAMPORTS):
            exit_now = True
        if not exit_now and mint in dev_sold_now:
            exit_now = True
        if (not exit_now and now - h.opened > MAX_HOLD
                and value * 100 < cost * (100 + int(HOLD_IF_UP * 100))):
            exit_now = True
        if not exit_now and now - t.last_trade_slot > STALE_SLOTS:
            exit_now = True
        if not exit_now:
            continue
        h.exiting = True
        if mint in pending_mints or len(orders) >= max_orders:
            continue
        orders.append({"side": "sell", "mint": mint, "token_amount": tokens,
                       "max_slippage_bps": SELL_SLIP_BPS})

    # --- probe round trips: sell the probe as soon as its buy has landed ------
    for mint in list(probes.keys()):
        pos = state.positions.get(mint)
        curve = state.curves.get(mint)
        if pos is None:
            if mint not in pending_mints:
                del probes[mint]
            continue
        if curve is None or curve.complete or mint in pending_mints:
            continue
        if len(orders) >= max_orders:
            break
        orders.append({"side": "sell", "mint": mint, "token_amount": pos.token_amount,
                       "max_slippage_bps": SELL_SLIP_BPS})
        probes[mint] = True

    # --- entries and the one pyramid add ---------------------------------------
    free = state.free_cash_lamports()
    slots_left = caps["max_open_positions"] - state.committed_position_count()
    for mint in traded:
        if len(orders) >= max_orders:
            break
        if mint in pending_mints or mint in probes:
            continue
        curve = state.curves.get(mint)
        if curve is None or curve.complete:
            continue
        h = held.get(mint)
        price = price_of(curve)
        rq_sol = curve.real_quote_reserves / LAMPORTS
        if h is None:
            if slots_left <= 0:
                continue
            sig = entry_ok(state, mint, now)
            if sig is None:
                continue
            want = PROBE_SOL
        else:
            if h.exiting or not h.filled:
                continue
            if h.invested >= MAX_POSITION_SOL - MIN_BUY_SOL + 1e-9:
                continue
            pos = state.positions.get(mint)
            if pos is None:
                continue
            if price < h.last_add_price * (1.0 + ADD_STEP):
                continue
            if h.last_value < pos.cost_lamports:
                continue
            if net_flow(tapes[mint], now, WIN) < int(ADD_MIN_FLOW_SOL * LAMPORTS):
                continue
            if rq_sol > MAX_RQ_SOL:
                continue
            want = min(ADD_SOL, MAX_POSITION_SOL - h.invested)
        cap_sol = state.max_buy_lamports(mint) / LAMPORTS
        amount = min(cap_sol, want)
        if amount > (free - tx) / LAMPORTS:
            amount = (free - tx) / LAMPORTS
        if amount < MIN_BUY_SOL:
            continue
        lamports = int(amount * LAMPORTS)
        orders.append({"side": "buy", "mint": mint, "quote_lamports": lamports,
                       "max_slippage_bps": BUY_SLIP_BPS})
        free -= lamports + tx
        if h is None:
            held[mint] = Held(now, amount, price)
            tape_of(mint).entries += 1
            slots_left -= 1
        else:
            h.invested += amount
            h.last_add_price = price

    # --- guardrail pacing: cheap probe round trips when the pace lags ---------
    if (len(probes) < MAX_PROBES and slots_left > 0 and first_time is not None
            and last_time is not None):
        pace = min(1.0, (last_time - first_time) / PACE_SECS)
        need_coins = len(my_coins) < math.ceil(TARGET_COINS * pace)
        need_fills = my_fills < math.ceil(TARGET_FILLS * pace)
        if need_coins or need_fills:
            min_rq = PROBE_RT_SOL * 10_000 / caps["max_order_bps_of_real_reserve"]
            for mint in traded:
                if len(orders) >= max_orders or len(probes) >= MAX_PROBES:
                    break
                if (mint in held or mint in probes or mint in pending_mints
                        or mint in state.positions):
                    continue
                if need_coins and not need_fills and mint in my_coins:
                    continue
                curve = state.curves.get(mint)
                if curve is None or curve.complete:
                    continue
                if curve.real_quote_reserves / LAMPORTS < min_rq * 1.5:
                    continue
                if free < int(PROBE_RT_SOL * LAMPORTS) + 2 * tx:
                    break
                orders.append({"side": "buy", "mint": mint,
                               "quote_lamports": int(PROBE_RT_SOL * LAMPORTS),
                               "max_slippage_bps": BUY_SLIP_BPS})
                probes[mint] = False
                free -= int(PROBE_RT_SOL * LAMPORTS) + tx
    return orders


if __name__ == "__main__":
    makefun_bot.run(on_batch)
