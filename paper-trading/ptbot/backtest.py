"""Backtest of Part A (temporal arbitrage + complete sets + Kelly) on past
Polymarket BTC 15-minute markets.

Honesty rules baked in:
- No look-ahead. At minute t the model only sees Coinbase candles that had
  closed by t, and the market price last printed at or before t.
- Polymarket publishes price history, not historical order books. We model
  the ask as price + half_spread (configurable) and cap stakes, because the
  real size on the book is unknown. Real fills would likely be worse.
- Winners come from Polymarket's own resolution, not our BTC feed.
- Results are split into first half / second half. An edge that only shows
  up in one half is not an edge.

Wallet consensus (Part B) is not backtested: copying wallets after the fact
needs the book at the moment they traded, which is not published.
"""
import hashlib
import json
import os
import time

from . import feeds, strategy
from .report import _stats, verdict


# ------------------------------------------------------------------ simulate

def _price_at(hist, t, max_age):
    """Last price printed at or before t, if not older than max_age seconds."""
    lo, hi, best = 0, len(hist) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if hist[mid][0] <= t:
            best = hist[mid]
            lo = mid + 1
        else:
            hi = mid - 1
    if best is None or t - best[0] > max_age:
        return None
    return best[1]


def simulate(markets, candles, cfg):
    """markets: [{start, end, condition_id, slug, winner_idx, hist: [hist_up, hist_down]}]
    candles: {ts: (ts, open, high, low, close)} for Coinbase 1-minute bars.
    Returns {"trades": [...], "calib": [...], "skipped": {...}, "bankroll": float}.
    """
    bt, t_cfg, k = cfg["backtest"], cfg["temporal"], cfg["kelly"]
    fee, margin = cfg["taker_fee_rate"], cfg["complete_set"]["min_margin"]
    lookback = t_cfg["vol_lookback_min"]
    bankroll = float(cfg["start_bankroll"])
    trades, calib = [], []
    skipped = {"no_start_price": 0, "no_winner": 0, "no_history": 0}

    def cost(p):
        if p is None:
            return None
        return strategy.cost_per_share(min(0.99, p + bt["half_spread"]), fee)

    delay = bt.get("entry_delay_s", 0)

    def fill_cost(m, idx, t):
        """Cost if the order lands `delay` seconds after the signal. With a
        delay, the fill uses the price printed by then, whatever it is."""
        if not delay:
            return None
        return cost(_price_at(m["hist"][idx], t + delay, bt["max_price_age_s"]))

    skipped["no_fill_price"] = 0

    def stake_for(p, c):
        s = strategy.position_size(bankroll, p, c, k["multiplier"], k["max_fraction"])
        return min(s, bt["max_stake_usd"])

    for m in sorted(markets, key=lambda m: m["start"]):
        if m.get("winner_idx") is None:
            skipped["no_winner"] += 1
            continue
        if not m["hist"][0] or not m["hist"][1]:
            skipped["no_history"] += 1
            continue
        start_bar = candles.get(m["start"])
        if not start_bar:
            skipped["no_start_price"] += 1
            continue
        s0 = start_bar[1]
        held = []  # open legs in this window

        for t in range(m["start"] + 60, m["end"], 60):
            last_bar = candles.get(t - 60)
            if not last_bar:
                continue
            spot = last_bar[4]
            closes = [candles[x][4] for x in range(t - 60 - lookback * 60, t, 60) if x in candles]
            sigma = strategy.realized_vol_per_sec(closes)
            secs_left = m["end"] - t
            fair_up = strategy.fair_prob_up(spot, s0, sigma, secs_left)
            p_up = _price_at(m["hist"][0], t, bt["max_price_age_s"])
            p_dn = _price_at(m["hist"][1], t, bt["max_price_age_s"])
            if fair_up is not None and p_up is not None:
                calib.append((fair_up, p_up, 1 if m["winner_idx"] == 0 else 0))
            costs = {0: cost(p_up), 1: cost(p_dn)}

            # A1 temporal
            if t_cfg["min_secs_left"] <= secs_left <= t_cfg["max_secs_left"]:
                sig = strategy.temporal_signal(fair_up, costs[0], costs[1], t_cfg["min_edge"])
                if sig and not any(h["source"] == "temporal" and h["idx"] == sig[0] for h in held):
                    idx, p, c, edge = sig
                    if delay:
                        c = fill_cost(m, idx, t)
                    if c is None:
                        skipped["no_fill_price"] += 1
                    else:
                        stake = stake_for(p, c)
                        held.append({"source": "temporal", "idx": idx, "cost": c, "stake": stake,
                                     "shares": stake / c, "ts": t, "p": p, "edge": edge, "set": False})
                        bankroll -= stake

            # A2 complete set, legged
            for h in list(held):
                if h["set"] or h["shares"] <= 0:
                    continue
                other = 1 - h["idx"]
                locked = strategy.complete_set_gap(h["cost"], costs[other], margin)
                if locked is not None:
                    c = fill_cost(m, other, t) if delay else costs[other]
                    if c is None:
                        continue
                    locked = 1.0 - h["cost"] - c   # what the delayed fill actually locks, may be < 0
                    stake = round(h["shares"] * c, 2)
                    h["set"] = True
                    held.append({"source": "complete_set", "idx": other, "cost": c, "stake": stake,
                                 "shares": h["shares"], "ts": t, "p": 1.0, "edge": locked, "set": True})
                    bankroll -= stake

            # A2 complete set, instant
            locked = strategy.complete_set_gap(costs[0], costs[1], margin)
            if locked is not None and not any(h["source"] == "complete_set" for h in held):
                budget = min(bankroll * k["max_fraction"], bt["max_stake_usd"])
                shares = budget / (costs[0] + costs[1])
                for idx in (0, 1):
                    stake = round(shares * costs[idx], 2)
                    held.append({"source": "complete_set", "idx": idx, "cost": costs[idx], "stake": stake,
                                 "shares": shares, "ts": t, "p": 1.0, "edge": locked, "set": True})
                    bankroll -= stake

        # settle on Polymarket's official result
        for h in held:
            won = 1 if h["idx"] == m["winner_idx"] else 0
            payout = h["shares"] * won
            bankroll += payout
            trades.append({**h, "slug": m["slug"], "won": won,
                           "pnl": payout - h["stake"], "unit_pnl": won - h["cost"]})
    return {"trades": trades, "calib": calib, "skipped": skipped, "bankroll": bankroll}


# ------------------------------------------------------------------ report

def brier(pairs):
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs) if pairs else None


def summarize(result, cfg, n_markets, days):
    trades = result["trades"]
    lines = [f"BACKTEST: {days} day(s), {n_markets} BTC 15m markets loaded",
             f"  skipped: {result['skipped']}",
             f"  assumptions: ask = last price + {cfg['backtest']['half_spread']:.3f}, "
             f"fee {cfg['taker_fee_rate'] * 100:.1f}%, stake cap ${cfg['backtest']['max_stake_usd']}, "
             f"prices older than {cfg['backtest']['max_price_age_s']}s ignored, "
             f"entry delay {cfg['backtest'].get('entry_delay_s', 0)}s", ""]

    cal = result["calib"]
    if cal:
        bm = brier([(f, y) for f, _, y in cal])
        bk = brier([(p, y) for _, p, y in cal])
        lines.append(f"CALIBRATION over {len(cal)} market-minutes (lower is better):")
        lines.append(f"  our model Brier {bm:.4f}   Polymarket price Brier {bk:.4f}")
        lines.append("  -> " + ("model is MORE accurate than the market price: a gap to exploit may exist"
                                if bm < bk else
                                "market price is at least as accurate as our model: no informational edge"))
        lines.append("")

    for src in ("temporal", "complete_set"):
        ts = [t for t in trades if t["source"] == src]
        lines.append(f"[{src}] {len(ts)} trades")
        if ts:
            wins = sum(t["won"] for t in ts)
            pnl = sum(t["pnl"] for t in ts)
            staked = sum(t["stake"] for t in ts)
            lines.append(f"  win rate {wins}/{len(ts)} ({wins / len(ts) * 100:.1f}%), "
                         f"P&L ${pnl:+.2f} on ${staked:.2f} staked")
            ts_sorted = sorted(ts, key=lambda t: t["ts"])
            half = len(ts_sorted) // 2
            for label, part in (("first half", ts_sorted[:half]), ("second half", ts_sorted[half:])):
                st = _stats([t["unit_pnl"] for t in part])
                if st:
                    lines.append(f"  {label}: {st['n']} trades, avg {st['mean']:+.4f} per $1 share")
        lines.append(f"  verdict: {verdict(_stats([t['unit_pnl'] for t in ts]))}")
        lines.append("")

    if trades:
        tot = sum(t["pnl"] for t in trades)
        lines.append(f"ALL COMBINED (a legged set = temporal leg + its hedge): P&L ${tot:+.2f} "
                     f"on ${sum(t['stake'] for t in trades):.2f} staked")
        lines.append("")
    start = float(cfg["start_bankroll"])
    lines.append(f"Paper bankroll: ${start:.2f} -> ${result['bankroll']:.2f} ({result['bankroll'] - start:+.2f})")
    lines.append("A backtest is an upper bound. Live fills, latency and competition make it worse, not better.")
    return "\n".join(lines)


# ------------------------------------------------------------------ data loading

class Cache:
    """Disk cache so re-running a backtest doesn't re-download everything.
    Only finished, resolved markets are cached, since those never change."""

    def __init__(self, path):
        self.path = path
        os.makedirs(path, exist_ok=True)

    def get(self, key, fn):
        f = os.path.join(self.path, hashlib.sha1(key.encode()).hexdigest() + ".json")
        if os.path.exists(f):
            with open(f) as fh:
                return json.load(fh)
        val = fn()
        if val is not None:
            with open(f, "w") as fh:
                json.dump(val, fh)
        return val


def load(cfg, days, cache_dir, log=print, now=None):
    w = cfg["window_seconds"]
    now = int(now or time.time())
    last_start = (now - 1800) // w * w - w  # only windows that closed 30+ min ago
    first_start = last_start - int(days * 86400) + w
    cache = Cache(cache_dir)
    markets = []
    starts = list(range(first_start, last_start + 1, w))
    fails = 0
    for i, start in enumerate(starts):
        slug = f"{cfg['market_slug_prefix']}{start}"
        try:
            ev = cache.get("event:" + slug, lambda: feeds.event_by_slug(slug) or {"markets": []})
        except feeds.FetchError as e:
            fails += 1
            log(f"  {slug}: {e}")
            if fails >= 5 and not markets:
                raise feeds.FetchError("Polymarket unreachable (5 straight failures). Run `python3 -m ptbot check`.")
            continue
        fails = 0
        if not ev["markets"]:
            continue
        m = ev["markets"][0]
        winner = feeds.resolved_outcome_index(m)
        if winner is None or len(m["token_ids"]) != 2:
            continue
        hist = []
        for tok in m["token_ids"]:
            try:
                h = cache.get(f"hist:{tok}", lambda tok=tok: feeds.price_history(tok, start - 3600, start + w))
            except feeds.FetchError as e:
                log(f"  history {slug}: {e}")
                h = []
            hist.append([tuple(x) for x in h])
        markets.append({"start": start, "end": start + w, "condition_id": m["condition_id"],
                        "slug": slug, "winner_idx": winner, "hist": hist})
        if (i + 1) % 50 == 0:
            log(f"  loaded {i + 1}/{len(starts)} windows")
        time.sleep(0.05)

    lookback = cfg["temporal"]["vol_lookback_min"] * 60
    rows = cache.get(f"candles:{first_start}:{last_start}",
                     lambda: feeds.btc_minute_candles_range(first_start - lookback - 60, last_start + w))
    candles = {int(r[0]): tuple(r) for r in rows}
    return markets, candles


def run(cfg, days, cache_dir, log=print):
    log(f"loading {days} day(s) of markets and BTC candles (cached after first run)...")
    markets, candles = load(cfg, days, cache_dir, log)
    result = simulate(markets, candles, cfg)
    return summarize(result, cfg, len(markets), days)
