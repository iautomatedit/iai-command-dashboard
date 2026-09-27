"""Part C: paper trading ledger (SQLite). Hypothetical positions only."""
import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS ticks (
  ts INTEGER, btc REAL, btc_source TEXT, slug TEXT, start_price REAL,
  secs_left INTEGER, sigma REAL, fair_up REAL,
  ask_up REAL, ask_down REAL, bid_up REAL, bid_down REAL
);
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY, ts INTEGER, source TEXT, condition_id TEXT,
  slug TEXT, outcome_idx INTEGER, outcome TEXT, cost REAL, p REAL,
  edge REAL, details TEXT
);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY, signal_id INTEGER, source TEXT, ts_open INTEGER,
  condition_id TEXT, slug TEXT, outcome_idx INTEGER, outcome TEXT,
  token_id TEXT, cost REAL, shares REAL, stake REAL, set_id INTEGER,
  status TEXT DEFAULT 'open', ts_close INTEGER, won INTEGER,
  payout REAL, pnl REAL, unit_pnl REAL
);
CREATE TABLE IF NOT EXISTS wallet_events (
  wallet TEXT, ts INTEGER, condition_id TEXT, token_id TEXT,
  outcome_idx INTEGER, outcome TEXT, side TEXT, price REAL, size REAL,
  slug TEXT, title TEXT, tx TEXT,
  PRIMARY KEY (wallet, tx, token_id, side)
);
CREATE TABLE IF NOT EXISTS wallet_cursor (wallet TEXT PRIMARY KEY, last_ts INTEGER);
"""


class Ledger:
    def __init__(self, path, start_bankroll):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        if self.meta("started_at") is None:
            self.set_meta("started_at", int(time.time()))
            self.set_meta("start_bankroll", start_bankroll)
        self.db.commit()

    # -- meta
    def meta(self, k):
        r = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r["v"] if r else None

    def set_meta(self, k, v):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, str(v)))
        self.db.commit()

    # -- bankroll: start + realized pnl - capital tied up in open positions
    def bankroll(self):
        start = float(self.meta("start_bankroll"))
        realized = self.db.execute("SELECT COALESCE(SUM(pnl),0) FROM trades WHERE status='settled'").fetchone()[0]
        tied = self.db.execute("SELECT COALESCE(SUM(stake),0) FROM trades WHERE status='open'").fetchone()[0]
        return start + realized - tied

    # -- writes
    def log_tick(self, **t):
        cols = ",".join(t)
        self.db.execute(f"INSERT INTO ticks ({cols}) VALUES ({','.join('?' * len(t))})", tuple(t.values()))
        self.db.commit()

    def log_signal(self, source, condition_id, slug, outcome_idx, outcome, cost, p, edge, details=None, ts=None):
        cur = self.db.execute(
            "INSERT INTO signals (ts,source,condition_id,slug,outcome_idx,outcome,cost,p,edge,details) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ts or int(time.time()), source, condition_id, slug, outcome_idx, outcome, cost, p, edge, json.dumps(details or {})),
        )
        self.db.commit()
        return cur.lastrowid

    def open_trade(self, signal_id, source, condition_id, slug, outcome_idx, outcome, token_id, cost, stake, set_id=None, ts=None):
        shares = stake / cost if cost > 0 else 0.0
        cur = self.db.execute(
            "INSERT INTO trades (signal_id,source,ts_open,condition_id,slug,outcome_idx,outcome,token_id,cost,shares,stake,set_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (signal_id, source, ts or int(time.time()), condition_id, slug, outcome_idx, outcome, token_id, cost, shares, stake, set_id),
        )
        self.db.commit()
        return cur.lastrowid

    def settle_market(self, condition_id, winning_idx, ts=None):
        rows = self.db.execute("SELECT * FROM trades WHERE condition_id=? AND status='open'", (condition_id,)).fetchall()
        for r in rows:
            won = 1 if r["outcome_idx"] == winning_idx else 0
            payout = r["shares"] * won
            self.db.execute(
                "UPDATE trades SET status='settled', ts_close=?, won=?, payout=?, pnl=?, unit_pnl=? WHERE id=?",
                (ts or int(time.time()), won, payout, payout - r["stake"], won - r["cost"], r["id"]),
            )
        self.db.commit()
        return len(rows)

    # -- reads
    def open_trades(self, condition_id=None):
        if condition_id:
            return self.db.execute("SELECT * FROM trades WHERE status='open' AND condition_id=?", (condition_id,)).fetchall()
        return self.db.execute("SELECT * FROM trades WHERE status='open'").fetchall()

    def has_position(self, condition_id, outcome_idx, source=None):
        q = "SELECT 1 FROM trades WHERE condition_id=? AND outcome_idx=?"
        args = [condition_id, outcome_idx]
        if source:
            q += " AND source=?"
            args.append(source)
        return self.db.execute(q, args).fetchone() is not None

    def recent_signal(self, source, condition_id, outcome_idx, since_ts):
        return self.db.execute(
            "SELECT 1 FROM signals WHERE source=? AND condition_id=? AND outcome_idx=? AND ts>=?",
            (source, condition_id, outcome_idx, since_ts),
        ).fetchone() is not None

    def settled_unit_pnls(self, source):
        return [r[0] for r in self.db.execute("SELECT unit_pnl FROM trades WHERE status='settled' AND source=?", (source,))]
