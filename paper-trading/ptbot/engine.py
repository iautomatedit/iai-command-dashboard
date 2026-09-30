"""Main loop: pull real data, generate signals, log paper trades, settle them."""
import json
import os
import time
import traceback

from . import consensus, feeds, jev, strategy
from .ledger import Ledger


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


class Engine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.ledger = Ledger(cfg["db_path"], cfg["start_bankroll"])
        self.fee = cfg["taker_fee_rate"]
        self.window = None          # current market window info
        self.sigma = None
        self.sigma_at = 0
        self.last_consensus = 0
        self.last_settle = 0
        self.wallets = []
        # Temporal + complete sets failed the stress-tested backtest, so they
        # are off unless explicitly turned back on in config.
        on = {"temporal": False, "complete_set": False, "consensus": True}
        on.update(cfg.get("strategies", {}))
        self.enabled = on
        j = cfg.get("jev") or {}
        self.jev_on = bool(j.get("enabled"))
        self.jev_every = max(30, int(j.get("every_seconds", 60)))
        self.jev_timeout = float(j.get("timeout_s", 2.0))
        self.jev_model = str(j.get("model") or "jev-latest")
        self.jev_last = 0

    # ---------------------------------------------------------- market window
    def current_window(self, now):
        w = self.cfg["window_seconds"]
        start = int(now) // w * w
        if self.window and self.window["start"] == start:
            if self.window["start_price"] is None:
                self.window["start_price"] = self._start_price(start)
            return self.window
        slug = f"{self.cfg['market_slug_prefix']}{start}"
        ev = feeds.event_by_slug(slug)
        if not ev or not ev["markets"]:
            log(f"no Polymarket event found for slug {slug}")
            self.window = {"start": start, "market": None, "start_price": None, "slug": slug}
            return self.window
        m = ev["markets"][0]
        names = [str(o).lower() for o in m["outcomes"]]
        if names != ["up", "down"] or len(m["token_ids"]) != 2:
            log(f"unexpected market shape for {slug}: outcomes={m['outcomes']}")
            self.window = {"start": start, "market": None, "start_price": None, "slug": slug}
            return self.window
        self.window = {"start": start, "end": start + w, "slug": slug, "market": m,
                       "start_price": self._start_price(start)}
        log(f"tracking {slug}: {m['question']}")
        return self.window

    def _start_price(self, start):
        """BTC price at window open = open of the 1-minute candle at `start`.

        Note: Polymarket resolves these markets on the Chainlink BTC/USD
        stream, not Coinbase. The two track closely but are not identical,
        which is a real source of model error and is logged, not hidden.
        """
        try:
            for ts, o, *_ in feeds.btc_minute_candles(start, start + 60):
                if ts == start:
                    return o
        except feeds.FetchError as e:
            log(f"start price fetch failed: {e}")
        return None

    def refresh_sigma(self, now):
        if self.sigma and now - self.sigma_at < 300:
            return
        lb = self.cfg["temporal"]["vol_lookback_min"]
        try:
            closes = [c[4] for c in feeds.btc_minute_candles(int(now) - lb * 60, int(now))]
            s = strategy.realized_vol_per_sec(closes)
            if s:
                self.sigma, self.sigma_at = s, now
        except feeds.FetchError as e:
            log(f"vol refresh failed: {e}")

    # ---------------------------------------------------------- sizing helper
    def _stake(self, p, cost, ask_size):
        k = self.cfg["kelly"]
        stake = strategy.position_size(self.ledger.bankroll(), p, cost, k["multiplier"], k["max_fraction"])
        # Can't buy more than what is actually offered at the best ask.
        if ask_size is not None:
            stake = min(stake, round(ask_size * cost, 2))
        return max(0.0, stake)

    # ---------------------------------------------------------- one tick
    def tick(self):
        now = time.time()
        spot, src = feeds.btc_spot()
        win = self.current_window(now)
        m = win.get("market")
        if not m:
            return
        self.refresh_sigma(now)
        up_tok, down_tok = m["token_ids"]
        bu, bd = feeds.order_book(up_tok), feeds.order_book(down_tok)
        ask_up, ask_dn = bu["best_ask"], bd["best_ask"]
        cost_up = strategy.cost_per_share(ask_up[0], self.fee) if ask_up else None
        cost_dn = strategy.cost_per_share(ask_dn[0], self.fee) if ask_dn else None
        secs_left = int(win["end"] - now)
        fair_up = None
        if win["start_price"]:
            fair_up = strategy.fair_prob_up(spot, win["start_price"], self.sigma, secs_left)

        self.ledger.log_tick(
            ts=int(now), btc=spot, btc_source=src, slug=win["slug"], start_price=win["start_price"],
            secs_left=secs_left, sigma=self.sigma, fair_up=fair_up,
            ask_up=ask_up[0] if ask_up else None, ask_down=ask_dn[0] if ask_dn else None,
            bid_up=bu["best_bid"][0] if bu["best_bid"] else None,
            bid_down=bd["best_bid"][0] if bd["best_bid"] else None,
        )

        if self.jev_on:
            mid = None
            if ask_up and bu["best_bid"]:
                mid = (ask_up[0] + bu["best_bid"][0]) / 2
            elif ask_up:
                mid = ask_up[0]
            self.jev_step(win, now, spot, secs_left, fair_up, mid)

        books = {0: (up_tok, cost_up, ask_up), 1: (down_tok, cost_dn, ask_dn)}
        if self.enabled["temporal"]:
            self.check_temporal(win, m, fair_up, secs_left, books, spot)
        if self.enabled["complete_set"]:
            self.check_complete_set(win, m, books)

    # ---------------------------------------------------------- Jev challenge (measurement only)
    def jev_step(self, win, now, spot, secs_left, fair_up, market_up):
        if now - self.jev_last < self.jev_every or not win.get("start_price") or secs_left < 30:
            return
        self.jev_last = now
        closes = [c[4] for c in feeds.btc_minute_candles(int(now) - 16 * 60, int(now))]
        state = jev.build_state(win["start"], win["end"], now, win["start_price"], spot, closes)
        p, latency, err, answered_by = None, None, None, None
        try:
            p, latency, answered_by = jev.ask_up(state, os.environ.get("TYPESAFE_API_KEY", ""),
                                                 timeout=self.jev_timeout, model=self.jev_model)
        except jev.JevError as e:
            err = str(e)[:200]
        self.ledger.db.execute(
            "INSERT INTO jev_preds (ts, slug, end_ts, secs_left, jev_up, model_up, market_up, latency_ms, error, model) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (int(now), win["slug"], win["end"], secs_left, p, fair_up, market_up, latency, err, answered_by))
        self.ledger.db.commit()
        if err:
            log(f"JEV error: {err}")

    def settle_jev(self):
        rows = self.ledger.db.execute(
            "SELECT DISTINCT slug FROM jev_preds WHERE outcome IS NULL AND end_ts < ?", (int(time.time()) - 120,)).fetchall()
        for r in rows[:10]:
            ev = feeds.event_by_slug(r["slug"])
            idx = feeds.resolved_outcome_index(ev["markets"][0]) if ev and ev["markets"] else None
            if idx is not None:
                self.ledger.db.execute("UPDATE jev_preds SET outcome=? WHERE slug=?", (1 if idx == 0 else 0, r["slug"]))
        self.ledger.db.commit()

    # ---------------------------------------------------------- Part A.1
    def check_temporal(self, win, m, fair_up, secs_left, books, spot):
        t = self.cfg["temporal"]
        if not (t["min_secs_left"] <= secs_left <= t["max_secs_left"]):
            return
        sig = strategy.temporal_signal(fair_up, books[0][1], books[1][1], t["min_edge"])
        if not sig:
            return
        idx, p, cost, edge = sig
        cid = m["condition_id"]
        if self.ledger.has_position(cid, idx, "temporal"):
            return
        token, _, ask = books[idx]
        name = m["outcomes"][idx]
        details = {"spot": spot, "start_price": win["start_price"], "secs_left": secs_left,
                   "sigma": self.sigma, "ask": ask[0], "ask_size": ask[1], "fee_rate": self.fee}
        sid = self.ledger.log_signal("temporal", cid, win["slug"], idx, name, cost, p, edge, details)
        stake = self._stake(p, cost, ask[1])
        self.ledger.open_trade(sid, "temporal", cid, win["slug"], idx, name, token, cost, stake)
        log(f"TEMPORAL {name} p={p:.3f} cost={cost:.3f} edge={edge:+.3f} stake=${stake:.2f}")

    # ---------------------------------------------------------- Part A.2
    def check_complete_set(self, win, m, books):
        margin = self.cfg["complete_set"]["min_margin"]
        cid = m["condition_id"]
        # (a) Legged: we hold one side from an earlier signal; is the other side now cheap enough?
        for leg in self.ledger.open_trades(cid):
            if leg["set_id"] is not None or leg["shares"] <= 0:
                continue
            other = 1 - leg["outcome_idx"]
            token, cost_other, ask = books[other]
            locked = strategy.complete_set_gap(leg["cost"], cost_other, margin)
            if locked is None:
                continue
            shares = min(leg["shares"], ask[1])
            if shares < leg["shares"]:
                continue  # not enough size on the book to fully hedge; don't pretend
            name = m["outcomes"][other]
            sid = self.ledger.log_signal("complete_set", cid, win["slug"], other, name, cost_other, 1.0, locked,
                                         {"mode": "legged", "held_trade": leg["id"], "held_cost": leg["cost"]})
            self.ledger.open_trade(sid, "complete_set", cid, win["slug"], other, name, token,
                                   cost_other, round(shares * cost_other, 2), set_id=leg["id"])
            self.ledger.db.execute("UPDATE trades SET set_id=? WHERE id=?", (leg["id"], leg["id"]))
            self.ledger.db.commit()
            log(f"COMPLETE SET (legged) locked ${locked:.3f}/share on {shares:.1f} shares")

        # (b) Instant: both sides together cost under $1 right now.
        c0, c1 = books[0][1], books[1][1]
        locked = strategy.complete_set_gap(c0, c1, margin)
        if locked is None or self.ledger.has_position(cid, 0, "complete_set"):
            return
        shares = min(books[0][2][1], books[1][2][1])
        k = self.cfg["kelly"]
        budget = self.ledger.bankroll() * k["max_fraction"]
        shares = min(shares, budget / (c0 + c1))
        sid = self.ledger.log_signal("complete_set", cid, win["slug"], -1, "Up+Down", c0 + c1, 1.0, locked,
                                     {"mode": "instant", "cost_up": c0, "cost_down": c1})
        first = None
        for idx in (0, 1):
            tid = self.ledger.open_trade(sid, "complete_set", cid, win["slug"], idx, m["outcomes"][idx],
                                         books[idx][0], books[idx][1], round(shares * books[idx][1], 2))
            first = first or tid
            self.ledger.db.execute("UPDATE trades SET set_id=? WHERE id=?", (first, tid))
        self.ledger.db.commit()
        log(f"COMPLETE SET (instant) locked ${locked:.3f}/share on {shares:.1f} shares")

    # ---------------------------------------------------------- Part B
    def poll_consensus(self):
        c = self.cfg["consensus"]
        if not self.enabled["consensus"] or not self.wallets or time.time() - self.last_consensus < c["poll_seconds"]:
            return
        self.last_consensus = time.time()
        for w in self.wallets:
            try:
                consensus.ingest_wallet(self.ledger, w)
            except feeds.FetchError as e:
                log(f"wallet poll failed {w}: {e}")
        cooldown_since = int(time.time()) - c["window_seconds"]
        for g in consensus.find_consensus(self.ledger, c["window_seconds"], c["min_wallets"]):
            if self.ledger.recent_signal("consensus", g["condition_id"], g["outcome_idx"], cooldown_since):
                continue
            if self.ledger.has_position(g["condition_id"], g["outcome_idx"], "consensus"):
                continue
            book = feeds.order_book(g["token_id"])
            if not book["best_ask"]:
                # Usually a late buy on a near-certain outcome: nobody is left selling.
                # Log it anyway so the signal count stays honest, but there is no fill to trade.
                self.ledger.log_signal("consensus", g["condition_id"], g["slug"], g["outcome_idx"],
                                       g["outcome"], None, None, None, {**g, "no_fill": True})
                log(f"CONSENSUS {len(g['wallets'])} wallets -> {g['outcome']} on {g['slug']} "
                    f"(wallets avg {g['wallet_avg_price']:.3f}) NO FILL: empty ask side")
                continue
            ask, ask_size = book["best_ask"]
            cost = strategy.cost_per_share(ask, self.fee)
            p, n_hist = consensus.consensus_prob(self.ledger, cost, c["min_history_for_sizing"])
            details = {**g, "our_ask": ask, "ask_size": ask_size, "history_n": n_hist,
                       "entry_vs_wallets": ask - g["wallet_avg_price"]}
            sid = self.ledger.log_signal("consensus", g["condition_id"], g["slug"], g["outcome_idx"],
                                         g["outcome"], cost, p, p - cost, details)
            stake = self._stake(p, cost, ask_size)
            self.ledger.open_trade(sid, "consensus", g["condition_id"], g["slug"], g["outcome_idx"],
                                   g["outcome"], g["token_id"], cost, stake)
            log(f"CONSENSUS {len(g['wallets'])} wallets -> {g['outcome']} on {g['slug']} "
                f"ask={ask:.3f} (wallets avg {g['wallet_avg_price']:.3f}) stake=${stake:.2f}")

    # ---------------------------------------------------------- settlement
    def settle(self):
        if time.time() - self.last_settle < 60:
            return
        self.last_settle = time.time()
        if self.jev_on:
            try:
                self.settle_jev()
            except feeds.FetchError as e:
                log(f"jev settle lookup failed: {e}")
        cids = {r["condition_id"] for r in self.ledger.open_trades()}
        for cid in cids:
            try:
                idx = feeds.resolved_outcome_index(feeds.market_by_condition(cid))
            except feeds.FetchError as e:
                log(f"settle lookup failed {cid}: {e}")
                continue
            if idx is not None:
                n = self.ledger.settle_market(cid, idx)
                log(f"settled {n} paper trade(s) on {cid[:10]}.. winner idx={idx}")

    # ---------------------------------------------------------- loop
    def run(self, wallets):
        self.wallets = wallets
        on = ", ".join(k for k, v in self.enabled.items() if v) or "none"
        log(f"paper trading started. bankroll=${self.ledger.bankroll():.2f} wallets={len(wallets)} strategies: {on}"
            + (f" · jev challenge ON ({self.jev_model})" if self.jev_on else ""))
        if self.jev_on and self.jev_model == "jev-latest":
            log("jev: model is jev-latest; pin an exact version in config so the test isn't split across releases")
        if self.jev_on and not os.environ.get("TYPESAFE_API_KEY"):
            log("jev challenge is on but TYPESAFE_API_KEY is not set; predictions will be logged as errors")
        while True:
            for step in (self.tick, self.poll_consensus, self.settle):
                try:
                    step()
                except Exception as e:  # keep running through transient API errors
                    log(f"{step.__name__} error: {e}")
                    if self.cfg.get("debug"):
                        traceback.print_exc()
            time.sleep(self.cfg["poll_seconds"])
