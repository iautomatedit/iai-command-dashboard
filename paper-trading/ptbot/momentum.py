"""Time-series momentum (Moskowitz, Ooi & Pedersen 2012) on daily crypto.

Rule: at each rebalance, if the asset's trailing `lookback` return is
positive, hold it; otherwise go flat (or short, if allowed). Position size
is scaled so the position's expected volatility hits `target_vol`, capped at
`max_leverage` (1.0 = no leverage, realistic for spot).

Honesty rules baked in:
- No look-ahead. The signal is computed from closes up to day d and earns
  day d+1's return.
- Trading cost charged on every change in position.
- Always compared with plain buy-and-hold of the same asset. Beating a flat
  line means nothing if holding the coin did better.
- Every lookback in the grid is shown, not just the best one. If only one
  setting works, that is curve fitting, not an edge.
- Results split into first half / second half of the sample.
"""
import datetime as dt
import math
import time

from . import feeds
from .backtest import Cache

DAY = 86400


# ------------------------------------------------------------------ math

def _sharpe(rets):
    if len(rets) < 30:
        return None
    m = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1))
    return (m / sd) * math.sqrt(365) if sd > 0 else None


def _max_drawdown(rets):
    peak, eq, mdd = 1.0, 1.0, 0.0
    for r in rets:
        eq *= 1 + r
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    return mdd


def _cagr(rets):
    if not rets:
        return None
    eq = 1.0
    for r in rets:
        eq *= 1 + r
    years = len(rets) / 365
    return eq ** (1 / years) - 1 if eq > 0 and years > 0 else -1.0


def metrics(rets, positions=None):
    out = {
        "days": len(rets),
        "total": math.prod(1 + r for r in rets) - 1 if rets else 0.0,
        "cagr": _cagr(rets),
        "vol": (math.sqrt(sum((r - sum(rets) / len(rets)) ** 2 for r in rets) / (len(rets) - 1)) * math.sqrt(365)
                if len(rets) > 1 else None),
        "sharpe": _sharpe(rets),
        "max_dd": _max_drawdown(rets),
    }
    if positions is not None:
        out["in_market"] = sum(1 for p in positions if abs(p) > 1e-9) / len(positions) if positions else 0
    return out


def simulate(closes, lookback, rebalance_days=7, vol_window=30, target_vol=0.5,
             max_leverage=1.0, allow_short=False, cost=0.005):
    """closes: [(ts, close)] daily ascending.
    Returns (strategy_daily_returns, buyhold_daily_returns, positions), all
    aligned to the same days (starting once enough history exists)."""
    px = [c for _, c in closes]
    rets = [px[i] / px[i - 1] - 1 for i in range(1, len(px))]   # rets[i-1] = return of day i
    start = max(lookback, vol_window + 1)
    strat, bh, positions = [], [], []
    pos = 0.0
    for d in range(start, len(px) - 1):
        if (d - start) % rebalance_days == 0:
            mom = px[d] / px[d - lookback] - 1
            window = rets[d - vol_window:d]              # returns of days d-vol_window+1 .. d
            m = sum(window) / len(window)
            vol = math.sqrt(sum((r - m) ** 2 for r in window) / (len(window) - 1)) * math.sqrt(365)
            size = min(max_leverage, target_vol / vol) if vol > 0 else 0.0
            sign = 1.0 if mom > 0 else (-1.0 if allow_short else 0.0)
            new = sign * size
            turnover = abs(new - pos)
            pos = new
        else:
            turnover = 0.0
        r_next = rets[d]                                   # return of day d+1
        strat.append(pos * r_next - cost * turnover)
        bh.append(r_next)
        positions.append(pos)
    return strat, bh, positions


def sharpe_se(sharpe, years):
    """Approximate standard error of an annualized Sharpe ratio (Lo 2002)."""
    if sharpe is None or years <= 0:
        return None
    return math.sqrt((1 + 0.5 * sharpe ** 2) / years)


# ------------------------------------------------------------------ report

def _fmt(m):
    pct = lambda x: f"{x * 100:+.1f}%" if x is not None else "n/a"
    sh = f"{m['sharpe']:.2f}" if m["sharpe"] is not None else "n/a"
    s = f"CAGR {pct(m['cagr']):>8s}  Sharpe {sh:>5s}  max drawdown {pct(m['max_dd']):>7s}  vol {m['vol'] * 100 if m['vol'] is not None else 0:.0f}%"
    if "in_market" in m:
        s += f"  in market {m['in_market'] * 100:.0f}%"
    return s


def verdict(strat_halves, bh_halves, full_s, full_bh, years):
    s1, s2 = (h["sharpe"] or 0 for h in strat_halves)
    b1, b2 = (h["sharpe"] or 0 for h in bh_halves)
    se = sharpe_se(full_s["sharpe"], years) or float("inf")
    better_both = s1 > b1 and s2 > b2
    shallower = full_s["max_dd"] > full_bh["max_dd"]
    if (full_s["sharpe"] or 0) - 2 * se <= 0:
        base = f"Sharpe {full_s['sharpe'] or 0:.2f} is within 2 standard errors ({se:.2f}) of zero: NOT statistically distinguishable from no edge."
    else:
        base = f"Sharpe {full_s['sharpe']:.2f} is more than 2 standard errors ({se:.2f}) above zero."
    if better_both:
        rel = "Beat buy-and-hold on risk-adjusted return in BOTH halves."
    elif s1 > b1 or s2 > b2:
        rel = "Beat buy-and-hold in only ONE half: inconsistent."
    else:
        rel = "Did NOT beat buy-and-hold on risk-adjusted return in either half. Just holding was as good or better."
    dd = ("Smaller worst drawdown than buy-and-hold." if shallower
          else "Worst drawdown as bad or worse than buy-and-hold.")
    return f"{base} {rel} {dd}"


def report(asset, closes, grid, params):
    first = dt.datetime.fromtimestamp(closes[0][0], dt.timezone.utc).date()
    last = dt.datetime.fromtimestamp(closes[-1][0], dt.timezone.utc).date()
    lines = [f"=== {asset}: {len(closes)} daily closes, {first} to {last} ===",
             f"rebalance every {params['rebalance_days']}d, vol target {params['target_vol'] * 100:.0f}%, "
             f"max leverage {params['max_leverage']}, short {'allowed' if params['allow_short'] else 'off (long/flat)'}, "
             f"cost {params['cost'] * 100:.2f}% per unit traded"]
    results = {}
    for lb in grid:
        s, b, pos = simulate(closes, lb, **params)
        results[lb] = (s, b, pos)
    # buy-and-hold on the longest-lookback sample so every row is comparable
    common = min(len(r[0]) for r in results.values())
    bh = results[max(grid)][1][-common:]
    lines.append(f"  buy & hold     {_fmt(metrics(bh))}")
    for lb in grid:
        s, _, pos = results[lb]
        lines.append(f"  momentum {lb:>3d}d  {_fmt(metrics(s[-common:], pos[-common:]))}")
    lines.append("")
    main = params.get("_main", 365) if params.get("_main", 365) in grid else max(grid)
    s, _, pos = results[main]
    s, pos = s[-common:], pos[-common:]
    half = len(s) // 2
    sh = [metrics(s[:half], pos[:half]), metrics(s[half:], pos[half:])]
    bhh = [metrics(bh[:half]), metrics(bh[half:])]
    lines.append(f"  split test, {main}d lookback:")
    lines.append(f"    first half   momentum {_fmt(sh[0])}")
    lines.append(f"                 buy&hold {_fmt(bhh[0])}")
    lines.append(f"    second half  momentum {_fmt(sh[1])}")
    lines.append(f"                 buy&hold {_fmt(bhh[1])}")
    lines.append(f"  verdict: {verdict(sh, bhh, metrics(s), metrics(bh), len(s) / 365)}")
    return "\n".join(lines)


# ------------------------------------------------------------------ data

def load_daily(product, start_ts, end_ts, cache_dir, pause=0.2):
    """Coinbase daily candles, paged 300 at a time. Completed days only."""
    cache = Cache(cache_dir)
    today = int(time.time()) // DAY * DAY
    end_ts = min(end_ts, today)                     # never include the unfinished day
    out = {}
    t = start_ts // DAY * DAY
    while t < end_ts:
        chunk_end = min(t + 300 * DAY, end_ts)
        # past chunks never change, so cache them; the latest chunk is refetched
        fetch = lambda t=t, e=chunk_end: [[r[0], r[4]] for r in feeds.candles(product, t, e, DAY)]
        rows = cache.get(f"daily:{product}:{t}:{chunk_end}", fetch) if chunk_end < today else fetch()
        for ts, close in rows:
            if ts < today:
                out[int(ts)] = float(close)
        t = chunk_end
        time.sleep(pause)
    return sorted(out.items())


def run(assets, start, cfg, cache_dir, log=print):
    p = cfg.get("momentum", {})
    params = {"rebalance_days": p.get("rebalance_days", 7), "vol_window": p.get("vol_window", 30),
              "target_vol": p.get("target_vol", 0.5), "max_leverage": p.get("max_leverage", 1.0),
              "allow_short": p.get("allow_short", False), "cost": p.get("cost", 0.005)}
    grid = p.get("lookbacks", [30, 90, 180, 365])
    start_ts = int(dt.datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp())
    out = []
    for a in assets:
        log(f"loading {a} daily prices since {start}...")
        closes = load_daily(a, start_ts, int(time.time()), cache_dir)
        if len(closes) < max(grid) + 100:
            out.append(f"=== {a}: only {len(closes)} days of data, not enough for a {max(grid)}d lookback ===")
            continue
        out.append(report(a, closes, grid, params))
    out.append("")
    out.append("A backtest is the best case. No slippage beyond the cost setting, no exchange outages,")
    out.append("and past regimes (2017, 2021 bubbles) may not repeat.")
    return "\n\n".join(out)
