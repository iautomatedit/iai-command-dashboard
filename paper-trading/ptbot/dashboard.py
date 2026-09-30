"""Local dashboard: python3 -m ptbot dashboard

Serves one page on http://127.0.0.1:<port> (your machine only). Reads the
paper ledger read-only, so it can run while the bot is running. Like the
rest of this package it only ever GETs public market data; it has no way to
place a trade.
"""
import json
import math
import os
import sqlite3
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import consensus, feeds, jev, momentum
from .report import SOURCES, _stats, verdict

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "dashboard.html")
DAY = 86400


# ------------------------------------------------------------------ ledger → JSON

def _ro(db_path):
    if not os.path.exists(db_path):
        return None
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _rows(con, q, args=()):
    try:
        return [dict(r) for r in con.execute(q, args).fetchall()]
    except sqlite3.OperationalError:
        return []


def _one(con, q, args=()):
    r = _rows(con, q, args)
    return r[0] if r else None


def ledger_state(cfg, now=None):
    now = int(now or time.time())
    on = {"temporal": False, "complete_set": False, "consensus": True}
    on.update(cfg.get("strategies", {}))
    wallets = [w.lower() for w in cfg.get("consensus", {}).get("wallets", [])]
    base = {"now": now, "enabled": on, "wallets_cfg": wallets, "has_db": False,
            "min_wallets": cfg.get("consensus", {}).get("min_wallets", 2),
            "window_seconds": cfg.get("consensus", {}).get("window_seconds", 900)}
    con = _ro(cfg["db_path"])
    if con is None:
        return base
    try:
        meta = {r["k"]: r["v"] for r in _rows(con, "SELECT k, v FROM meta")}
        start = float(meta.get("start_bankroll", cfg["start_bankroll"]))
        last = _one(con, "SELECT * FROM ticks ORDER BY ts DESC LIMIT 1")
        realized = (_one(con, "SELECT COALESCE(SUM(pnl),0) v FROM trades WHERE status='settled'") or {"v": 0})["v"]
        tied = (_one(con, "SELECT COALESCE(SUM(stake),0) v FROM trades WHERE status='open'") or {"v": 0})["v"]

        sources = {}
        for s in SOURCES:
            settled = _rows(con, "SELECT won, pnl, stake, unit_pnl FROM trades WHERE status='settled' AND source=?", (s,))
            st = _stats([r["unit_pnl"] for r in settled])
            sources[s] = {
                "signals": (_one(con, "SELECT COUNT(*) n FROM signals WHERE source=?", (s,)) or {"n": 0})["n"],
                "signals_24h": (_one(con, "SELECT COUNT(*) n FROM signals WHERE source=? AND ts>=?", (s, now - DAY)) or {"n": 0})["n"],
                "open": (_one(con, "SELECT COUNT(*) n FROM trades WHERE status='open' AND source=?", (s,)) or {"n": 0})["n"],
                "settled": len(settled),
                "wins": sum(r["won"] for r in settled),
                "pnl": sum(r["pnl"] for r in settled),
                "staked": sum(r["stake"] for r in settled),
                "mean": st["mean"] if st else None,
                "verdict": verdict(st),
            }

        eq, bal = [], start
        started = int(meta.get("started_at", now))
        eq.append({"ts": started, "v": start})
        for r in _rows(con, "SELECT ts_close, pnl FROM trades WHERE status='settled' ORDER BY ts_close"):
            bal += r["pnl"]
            eq.append({"ts": r["ts_close"], "v": round(bal, 2)})

        ticks = _rows(con, "SELECT ts, btc, fair_up, ask_up, bid_up, slug, secs_left, start_price "
                           "FROM ticks WHERE ts>=? ORDER BY ts", (now - 3600,))
        # thin to at most ~360 points so the page stays light
        step = max(1, len(ticks) // 360)
        ticks = ticks[::step] + ([ticks[-1]] if ticks and (len(ticks) - 1) % step else [])

        events = _rows(con, "SELECT * FROM wallet_events ORDER BY ts DESC LIMIT 40")
        per_wallet = {w: {"wallet": w, "events_24h": 0, "last_ts": None} for w in wallets}
        for r in _rows(con, "SELECT wallet, COUNT(*) n, MAX(ts) last FROM wallet_events WHERE ts>=? GROUP BY wallet", (now - DAY,)):
            per_wallet.setdefault(r["wallet"], {"wallet": r["wallet"]}).update(events_24h=r["n"], last_ts=r["last"])

        class _L:  # adapter so consensus.find_consensus can use the read-only connection
            db = con
        try:
            live_groups = consensus.find_consensus(_L, cfg["consensus"]["window_seconds"],
                                                   cfg["consensus"]["min_wallets"], now=now)
        except sqlite3.OperationalError:
            live_groups = []

        base.update({
            "has_db": True,
            "started_at": started,
            "last_tick": last,
            "running": bool(last and now - last["ts"] < max(90, 6 * cfg["poll_seconds"])),
            "kpis": {"start_bankroll": start, "realized_pnl": realized, "free_bankroll": start + realized - tied,
                     "open_positions": sum(v["open"] for v in sources.values()),
                     "signals_24h": sum(v["signals_24h"] for v in sources.values()),
                     "ticks": (_one(con, "SELECT COUNT(*) n FROM ticks") or {"n": 0})["n"]},
            "sources": sources,
            "equity": eq,
            "ticks": ticks,
            "signals": _rows(con, "SELECT id, ts, source, slug, outcome, cost, p, edge FROM signals ORDER BY ts DESC LIMIT 25"),
            "trades": _rows(con, "SELECT id, source, ts_open, slug, outcome, cost, stake, status, won, pnl, unit_pnl "
                                 "FROM trades ORDER BY ts_open DESC LIMIT 25"),
            "wallets": list(per_wallet.values()),
            "wallet_events": events,
            "consensus_live": live_groups,
            "ridge": ridge_data(con, now),
            "lab_stats": lab_stats(con, now),
            "jev": jev.challenge_stats(con),
        })
        return base
    finally:
        con.close()


def ridge_data(con, now, windows=10, window_s=900):
    """Per recent 15m window: the cheap side's ask over time, and the hindsight
    pair cost (cheapest Up ask + cheapest Down ask seen in that window)."""
    rows = _rows(con, "SELECT ts, slug, ask_up, ask_down FROM ticks WHERE ts>=? ORDER BY ts",
                 (now - windows * window_s,))
    by = {}
    for r in rows:
        by.setdefault(r["slug"], []).append(r)
    out = []
    for slug, rs in by.items():
        try:
            start = int(slug.rsplit("-", 1)[1])
        except (ValueError, IndexError):
            continue
        ups = [r["ask_up"] for r in rs if r["ask_up"] is not None]
        dns = [r["ask_down"] for r in rs if r["ask_down"] is not None]
        pts, last_bucket = [], None
        for r in rs:
            b = (r["ts"] - start) // 20            # one point per 20s keeps it light
            if b == last_bucket:
                continue
            last_bucket = b
            sides = [x for x in (r["ask_up"], r["ask_down"]) if x is not None]
            pts.append([r["ts"] - start, min(sides) if sides else None])
        out.append({"slug": slug, "start": start, "points": pts,
                    "min_up": min(ups) if ups else None, "min_down": min(dns) if dns else None,
                    "pair": (min(ups) + min(dns)) if ups and dns else None,
                    "live": now < start + window_s})
    return sorted(out, key=lambda w: w["start"], reverse=True)[:windows]


def lab_stats(con, now):
    days = [r["d"] for r in _rows(con, "SELECT DISTINCT date(ts, 'unixepoch', 'localtime') d FROM ticks ORDER BY d")]
    import datetime as _dt
    streak, day = 0, _dt.date.fromtimestamp(now)
    have = set(days)
    while day.isoformat() in have:
        streak += 1
        day -= _dt.timedelta(days=1)
    return {"uptime_days": len(days), "streak_days": streak,
            "settled": (_one(con, "SELECT COUNT(*) n FROM trades WHERE status='settled'") or {"n": 0})["n"],
            "consensus_signals": (_one(con, "SELECT COUNT(*) n FROM signals WHERE source='consensus'") or {"n": 0})["n"]}


def read_lab_log(here):
    path = os.path.join(here, "results", "lab_log.jsonl")
    out = []
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


# ------------------------------------------------------------------ momentum → JSON

def _weekly_curve(ts_list, rets):
    out, eq = [], 1.0
    for i, (t, r) in enumerate(zip(ts_list, rets)):
        eq *= 1 + r
        if i % 7 == 0 or i == len(rets) - 1:
            out.append([t, round(eq, 5)])
    return out


def _clean(m):
    return {k: (None if isinstance(v, float) and (math.isnan(v) or math.isinf(v)) else v) for k, v in m.items()}


def momentum_state(cfg, cache_dir, assets=("BTC-USD", "ETH-USD"), start="2016-06-01"):
    p = cfg.get("momentum", {})
    params = {"rebalance_days": p.get("rebalance_days", 7), "vol_window": p.get("vol_window", 30),
              "target_vol": p.get("target_vol", 0.5), "max_leverage": p.get("max_leverage", 1.0),
              "allow_short": p.get("allow_short", False), "cost": p.get("cost", 0.005)}
    grid = p.get("lookbacks", [30, 90, 180, 365])
    import datetime as dt
    start_ts = int(dt.datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp())
    out = {"params": params, "grid": grid, "assets": {}, "fetched_at": int(time.time())}
    for a in assets:
        closes = momentum.load_daily(a, start_ts, int(time.time()), cache_dir)
        if len(closes) < max(grid) + 100:
            out["assets"][a] = {"error": f"only {len(closes)} days of data"}
            continue
        px = [c for _, c in closes]
        runs = {lb: momentum.simulate(closes, lb, **params) for lb in grid}
        common = min(len(r[0]) for r in runs.values())
        ts = [t for t, _ in closes][-common:]                # day each return is earned
        bh = runs[max(grid)][1][-common:]
        bh_m = momentum.metrics(bh)
        rets = [px[i] / px[i - 1] - 1 for i in range(len(px) - params["vol_window"], len(px))]
        mean = sum(rets) / len(rets)
        vol_now = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) * math.sqrt(365)
        a_out = {"last_close": px[-1], "last_ts": closes[-1][0], "vol_now": vol_now,
                 "size_now": min(params["max_leverage"], params["target_vol"] / vol_now) if vol_now > 0 else 0,
                 "buyhold": {"metrics": _clean(bh_m), "curve": _weekly_curve(ts, bh)}, "lookbacks": {}}
        for lb in grid:
            s, _, pos = runs[lb]
            s, pos = s[-common:], pos[-common:]
            trend = px[-1] / px[-1 - lb] - 1
            half = len(s) // 2
            a_out["lookbacks"][str(lb)] = {
                "trend": trend,
                "signal": "HOLD" if trend > 0 else ("SHORT" if params["allow_short"] else "CASH"),
                "position_now": pos[-1],
                "metrics": _clean(momentum.metrics(s, pos)),
                "halves": [_clean(momentum.metrics(s[:half])), _clean(momentum.metrics(s[half:]))],
                "bh_halves": [_clean(momentum.metrics(bh[:half])), _clean(momentum.metrics(bh[half:]))],
                "curve": _weekly_curve(ts, s),
            }
        out["assets"][a] = a_out
    return out


# ------------------------------------------------------------------ server

class _Cache:
    def __init__(self, ttl):
        self.ttl, self.val, self.at, self.lock = ttl, None, 0, threading.Lock()

    def get(self, fn):
        with self.lock:
            if self.val is None or time.time() - self.at > self.ttl:
                self.val, self.at = fn(), time.time()
            return self.val


def make_handler(cfg, here):
    mom_cache = _Cache(3600)
    cache_dir = os.path.join(here, "bt_cache")
    bt_path = os.path.join(here, "results", "backtest_last.json")

    def load_momentum():
        try:
            return momentum_state(cfg, cache_dir)
        except feeds.FetchError as e:
            return {"error": str(e)}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype):
            data = body if isinstance(body, bytes) else body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _json(self, obj):
            self._send(200, json.dumps(obj, default=str), "application/json")

        def do_GET(self):
            path = self.path.split("?")[0]
            try:
                if path in ("/", "/index.html"):
                    with open(STATIC, "rb") as f:
                        self._send(200, f.read(), "text/html; charset=utf-8")
                elif path == "/api/state":
                    self._json(ledger_state(cfg))
                elif path == "/api/momentum":
                    self._json(mom_cache.get(load_momentum))
                elif path == "/api/lab":
                    log = read_lab_log(here)
                    last = None
                    if os.path.exists(bt_path):
                        with open(bt_path) as f:
                            last = json.load(f)
                    runs = [e for e in log if e.get("kind") == "backtest"]
                    self._json({
                        "backtest_runs": max(len(runs), 1 if last else 0),
                        "stress_tested": any((e.get("entry_delay_s") or 0) > 0 for e in runs)
                                         or bool(last and (last["assumptions"].get("entry_delay_s") or 0) > 0),
                        "momentum_runs": sum(1 for e in log if e.get("kind") == "momentum"),
                    })
                elif path == "/api/backtest":
                    if os.path.exists(bt_path):
                        with open(bt_path) as f:
                            self._json(json.load(f))
                    else:
                        self._json(None)
                else:
                    self._send(404, "not found", "text/plain")
            except Exception as e:  # never crash the server over one bad request
                self._send(500, json.dumps({"error": str(e)}), "application/json")

    return H


def serve(cfg, here, port=8765, open_browser=True):
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(cfg, here))
    url = f"http://127.0.0.1:{port}"
    print(f"dashboard running at {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
