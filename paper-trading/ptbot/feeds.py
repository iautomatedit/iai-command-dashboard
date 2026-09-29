"""Real market data sources. All public, all read-only."""
import datetime as dt
import json

from .http import FetchError, get_json

COINBASE = "https://api.exchange.coinbase.com"
KRAKEN = "https://api.kraken.com"
BINANCE = "https://api.binance.com"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"


# ---------------------------------------------------------------- BTC spot

def btc_spot():
    """Return (price, source). Tries Coinbase, then Kraken, then Binance.

    Binance.com returns HTTP 451 from US IPs, so it is last on purpose.
    """
    errors = []
    try:
        d = get_json(f"{COINBASE}/products/BTC-USD/ticker")
        return float(d["price"]), "coinbase"
    except (FetchError, KeyError, ValueError) as e:
        errors.append(f"coinbase: {e}")
    try:
        d = get_json(f"{KRAKEN}/0/public/Ticker", {"pair": "XBTUSD"})
        (pair,) = d["result"].values()
        return float(pair["c"][0]), "kraken"
    except (FetchError, KeyError, ValueError) as e:
        errors.append(f"kraken: {e}")
    try:
        d = get_json(f"{BINANCE}/api/v3/ticker/price", {"symbol": "BTCUSDT"})
        return float(d["price"]), "binance"
    except (FetchError, KeyError, ValueError) as e:
        errors.append(f"binance: {e}")
    raise FetchError("all BTC feeds failed: " + " | ".join(errors))


def candles(product, start_ts, end_ts, granularity):
    """Coinbase candles as [(ts, open, high, low, close)] ascending. Max 300 per call."""
    iso = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat()
    rows = get_json(
        f"{COINBASE}/products/{product}/candles",
        {"granularity": granularity, "start": iso(start_ts), "end": iso(end_ts)},
    )
    # Coinbase row: [time, low, high, open, close, volume], newest first.
    return sorted((int(r[0]), float(r[3]), float(r[2]), float(r[1]), float(r[4])) for r in rows)


def btc_minute_candles(start_ts, end_ts):
    """Coinbase 1-minute candles as [(ts, open, high, low, close)] ascending."""
    return candles("BTC-USD", start_ts, end_ts, 60)


# ---------------------------------------------------------------- Polymarket

def _maybe_json(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return v
    return v


def normalize_market(m):
    """Gamma returns some list fields as JSON strings; normalize them."""
    return {
        "condition_id": m.get("conditionId"),
        "slug": m.get("slug"),
        "question": m.get("question"),
        "outcomes": _maybe_json(m.get("outcomes")) or [],
        "outcome_prices": [float(x) for x in (_maybe_json(m.get("outcomePrices")) or [])],
        "token_ids": _maybe_json(m.get("clobTokenIds")) or [],
        "closed": bool(m.get("closed")),
        "end_date": m.get("endDate"),
    }


def event_by_slug(slug):
    rows = get_json(f"{GAMMA}/events", {"slug": slug})
    if not rows:
        return None
    ev = rows[0]
    return {"slug": ev.get("slug"), "title": ev.get("title"),
            "markets": [normalize_market(m) for m in ev.get("markets", [])]}


def market_by_condition(condition_id):
    rows = get_json(f"{GAMMA}/markets", {"condition_ids": condition_id})
    return normalize_market(rows[0]) if rows else None


def resolved_outcome_index(market):
    """Index of the winning outcome once Polymarket has settled it, else None.

    Uses Polymarket's own settled prices (1/0), never our own spot estimate.
    """
    if not market or not market["closed"]:
        return None
    prices = market["outcome_prices"]
    for i, p in enumerate(prices):
        if p >= 0.999:
            return i
    return None


def order_book(token_id):
    """Best bid / best ask with size. Computed explicitly, not by list position."""
    d = get_json(f"{CLOB}/book", {"token_id": token_id})
    bids = [(float(x["price"]), float(x["size"])) for x in d.get("bids", [])]
    asks = [(float(x["price"]), float(x["size"])) for x in d.get("asks", [])]
    best_bid = max(bids) if bids else None
    best_ask = min(asks) if asks else None
    return {"best_bid": best_bid, "best_ask": best_ask}


def wallet_trades(wallet, limit=100):
    return get_json(f"{DATA}/trades", {"user": wallet, "limit": limit, "takerOnly": "false"})


def market_trades(condition_id, limit=500):
    return get_json(f"{DATA}/trades", {"market": condition_id, "limit": limit, "takerOnly": "false"})


def price_history(token_id, start_ts, end_ts, fidelity_min=1):
    """Polymarket CLOB price history as [(ts, price)] ascending.

    These are traded/mid prices, not the historical ask. Backtests must add
    a spread assumption on top, or fills will look better than reality.
    """
    d = get_json(f"{CLOB}/prices-history",
                 {"market": token_id, "startTs": int(start_ts), "endTs": int(end_ts), "fidelity": fidelity_min})
    return sorted((int(x["t"]), float(x["p"])) for x in d.get("history", []))


def btc_minute_candles_range(start_ts, end_ts, pause=0.15):
    """Coinbase caps a candle request at 300 bars; page through longer ranges."""
    import time as _time
    out = {}
    t = int(start_ts)
    while t < end_ts:
        chunk_end = min(t + 300 * 60, int(end_ts))
        for row in btc_minute_candles(t, chunk_end):
            out[row[0]] = row
        t = chunk_end
        _time.sleep(pause)
    return [out[k] for k in sorted(out)]
