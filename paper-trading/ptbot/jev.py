"""Jev challenge: does TypeSafe's Jev forecast BTC 15m markets better than
Polymarket's own price?

Measurement only. Jev's answer is logged and scored after Polymarket settles
the market; it never opens a paper trade. A model earns a trading role only
after it beats the market price on accuracy, and so far no model we have
tested has.

API contract (from TypeSafe's official langchain-typesafe client):
  POST https://api.typesafe.ai/v1/systemone
  Authorization: Bearer $TYPESAFE_API_KEY
  {"model": "jev-latest", "state": {...},
   "questions": {"up": {"type": "noul", "instructions": "..."}}}
  -> {"model": "...", "answers": {"up": {"type": "noul", "noul": 0.63}}, "usage": {...}}

The API key is read from the TYPESAFE_API_KEY environment variable and is
never written to config, logs, the ledger or the dashboard.
"""
import datetime as dt
import json
import math
import os
import time
import urllib.error
import urllib.request

ENDPOINT = "https://api.typesafe.ai/v1/systemone"

# One narrow judgment, per TypeSafe's guidance. Question IDs are not sent to the
# model, so the full meaning lives in the instructions. Nested state is
# referenced with backticked paths.
UP_QUESTION = (
    "When this 15-minute Polymarket window closes at `window.closes_at_utc`, the "
    "Chainlink BTC/USD price will be at or above `window.price_to_beat`, so the "
    "market resolves Up. Judge only from the supplied BTC price history."
)


class JevError(RuntimeError):
    pass


def build_state(start_ts, end_ts, now, price_to_beat, spot, closes):
    """Blind state: BTC facts only, no Polymarket price. Jev must bring its own
    read; showing it the market price would let it copy the market."""
    iso = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rets = [b / a - 1 for a, b in zip(closes, closes[1:]) if a > 0]
    vol = (math.sqrt(sum(r * r for r in rets) / len(rets)) if rets else None)
    return {
        "window": {
            "opened_at_utc": iso(start_ts), "closes_at_utc": iso(end_ts),
            "seconds_remaining": int(end_ts - now), "price_to_beat": round(price_to_beat, 2),
        },
        "btc": {
            "spot_now": round(spot, 2),
            "change_since_open_pct": round((spot / price_to_beat - 1) * 100, 4),
            "last_minute_closes": [round(c, 2) for c in closes[-15:]],
            "typical_one_minute_move_pct": round(vol * 100, 4) if vol is not None else None,
        },
        "notes": "Prices from Coinbase BTC-USD. Resolution uses Chainlink BTC/USD, which tracks it closely.",
    }


def ask_up(state, api_key, timeout=2.0, model="jev-latest", _open=urllib.request.urlopen):
    """Return (probability_up, latency_ms). Raises JevError on any failure."""
    if not api_key:
        raise JevError("TYPESAFE_API_KEY is not set")
    body = json.dumps({"model": model, "state": state,
                       "questions": {"up": {"type": "noul", "instructions": UP_QUESTION}}}).encode()
    req = urllib.request.Request(ENDPOINT, data=body, headers={
        "Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
        "User-Agent": "ptbot-jev-challenge/1.0"})
    t0 = time.perf_counter()
    try:
        with _open(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise JevError(f"HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise JevError(str(e)) from e
    latency = (time.perf_counter() - t0) * 1000
    try:
        p = float(data["answers"]["up"]["noul"])
    except (KeyError, TypeError, ValueError) as e:
        raise JevError(f"unexpected response shape: {str(data)[:200]}") from e
    if not 0.0 <= p <= 1.0:
        raise JevError(f"probability out of range: {p}")
    return p, latency


# ------------------------------------------------------------------ scoring

def paired_brier(rows):
    """rows: [(window_slug, p_jev, p_compare, outcome)].

    Returns Brier scores and a paired difference test clustered by window:
    predictions inside one window are highly correlated, so each window
    counts once. Negative mean diff = Jev more accurate.
    """
    if not rows:
        return None
    bj = sum((j - y) ** 2 for _, j, _, y in rows) / len(rows)
    bc = sum((c - y) ** 2 for _, _, c, y in rows) / len(rows)
    per_window = {}
    for w, j, c, y in rows:
        per_window.setdefault(w, []).append((j - y) ** 2 - (c - y) ** 2)
    diffs = [sum(v) / len(v) for v in per_window.values()]
    n = len(diffs)
    mean = sum(diffs) / n
    se = (math.sqrt(sum((d - mean) ** 2 for d in diffs) / (n - 1)) / math.sqrt(n)) if n > 1 else float("inf")
    return {"n_preds": len(rows), "n_windows": n, "brier_jev": bj, "brier_other": bc,
            "diff": mean, "lo": mean - 2 * se, "hi": mean + 2 * se}


def verdict(stats, min_windows=200):
    if not stats or stats["n_windows"] < min_windows:
        n = stats["n_windows"] if stats else 0
        return "collecting", f"Collecting data ({n}/{min_windows} windows)"
    if stats["hi"] < 0:
        return "beats", "Jev beats the market price"
    if stats["lo"] > 0:
        return "worse", "Dead: less accurate than the market price"
    return "tie", "Dead: no better than the market price"


def challenge_stats(con):
    """Scores from the ledger: Jev vs Polymarket's price, and Jev vs our model,
    on predictions whose markets have settled."""
    try:
        rows = con.execute("SELECT slug, jev_up, model_up, market_up, outcome, latency_ms, error FROM jev_preds").fetchall()
    except Exception:
        return None
    if not rows:
        return None
    ok = [r for r in rows if r[1] is not None]
    done = [r for r in ok if r[4] is not None]
    vs_market = paired_brier([(r[0], r[1], r[3], r[4]) for r in done if r[3] is not None])
    vs_model = paired_brier([(r[0], r[1], r[2], r[4]) for r in done if r[2] is not None])
    lat = [r[5] for r in ok if r[5] is not None]
    kind, label = verdict(vs_market)
    return {"asked": len(rows), "answered": len(ok), "errors": len(rows) - len(ok), "settled": len(done),
            "avg_latency_ms": sum(lat) / len(lat) if lat else None,
            "vs_market": vs_market, "vs_model": vs_model, "verdict": kind, "verdict_label": label}
