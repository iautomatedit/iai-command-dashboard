import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ptbot import backtest, consensus, dashboard, engine, feeds, http, jev, momentum, publish, report, strategy  # noqa: E402
from ptbot.ledger import Ledger  # noqa: E402


def tmp_ledger(bankroll=1000):
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    return Ledger(path, bankroll)


class StrategyMath(unittest.TestCase):
    def test_kelly_matches_textbook_formula(self):
        p, cost = 0.6, 0.5
        b = (1 - cost) / cost
        self.assertAlmostEqual(strategy.kelly_fraction(p, cost), (p * b - (1 - p)) / b)
        self.assertAlmostEqual(strategy.kelly_fraction(p, cost), 0.2)

    def test_kelly_zero_without_edge(self):
        self.assertEqual(strategy.kelly_fraction(0.5, 0.5), 0.0)
        self.assertEqual(strategy.kelly_fraction(0.4, 0.5), 0.0)
        self.assertEqual(strategy.kelly_fraction(None, 0.5), 0.0)

    def test_position_size_scales_with_confidence_and_caps(self):
        small = strategy.position_size(1000, 0.55, 0.5, 0.25, 0.05)
        big = strategy.position_size(1000, 0.60, 0.5, 0.25, 0.05)
        self.assertLess(small, big)
        self.assertEqual(strategy.position_size(1000, 0.99, 0.5, 1.0, 0.05), 50.0)

    def test_fair_prob(self):
        self.assertAlmostEqual(strategy.fair_prob_up(100, 100, 1e-4, 600), 0.5)
        self.assertGreater(strategy.fair_prob_up(100.2, 100, 1e-4, 600), 0.5)
        self.assertEqual(strategy.fair_prob_up(99, 100, 1e-4, 0), 0.0)
        self.assertIsNone(strategy.fair_prob_up(100, 100, None, 600))

    def test_realized_vol(self):
        closes = [100 * (1.001 if i % 2 else 0.999) for i in range(30)]
        self.assertGreater(strategy.realized_vol_per_sec(closes), 0)
        self.assertIsNone(strategy.realized_vol_per_sec([100, 101]))

    def test_temporal_signal_picks_mispriced_side(self):
        self.assertEqual(strategy.temporal_signal(0.70, 0.55, 0.47, 0.04)[0], 0)
        self.assertEqual(strategy.temporal_signal(0.30, 0.72, 0.55, 0.04)[0], 1)
        self.assertIsNone(strategy.temporal_signal(0.52, 0.51, 0.51, 0.04))

    def test_complete_set(self):
        self.assertAlmostEqual(strategy.complete_set_gap(0.45, 0.50, 0.01), 0.05)
        self.assertIsNone(strategy.complete_set_gap(0.50, 0.505, 0.01))


class LedgerSettlement(unittest.TestCase):
    def test_pnl_uses_real_payouts(self):
        L = tmp_ledger()
        sid = L.log_signal("temporal", "c1", "s", 0, "Up", 0.5, 0.6, 0.1)
        L.open_trade(sid, "temporal", "c1", "s", 0, "Up", "t", 0.5, 20.0)
        L.open_trade(sid, "temporal", "c1", "s", 1, "Down", "t2", 0.4, 10.0)
        self.assertAlmostEqual(L.bankroll(), 970.0)
        L.settle_market("c1", 0)
        rows = {r["outcome"]: r for r in L.db.execute("SELECT * FROM trades")}
        self.assertAlmostEqual(rows["Up"]["pnl"], 20.0)       # 40 shares pay $40
        self.assertAlmostEqual(rows["Down"]["pnl"], -10.0)
        self.assertAlmostEqual(rows["Up"]["unit_pnl"], 0.5)
        self.assertAlmostEqual(L.bankroll(), 1010.0)

    def test_report_is_honest_with_no_data(self):
        out = report.build(tmp_ledger(), [])
        self.assertIn("INSUFFICIENT DATA", out)
        self.assertIn("SIGNALS GENERATED", out)

    def test_verdict_negative(self):
        st = report._stats([-0.05 + 0.01 * ((i % 3) - 1) for i in range(60)])
        self.assertIn("NEGATIVE", report.verdict(st))


class Consensus(unittest.TestCase):
    def trade(self, ts, side="BUY", cid="m1", idx=0, tx=None):
        return {"timestamp": ts, "side": side, "conditionId": cid, "asset": f"tok{idx}",
                "outcomeIndex": idx, "outcome": "Yes" if idx == 0 else "No", "price": 0.4,
                "size": 10, "slug": "mkt", "title": "Market", "transactionHash": tx or f"0x{ts}{side}{idx}"}

    def test_activity_filter(self):
        now = time.time()
        fresh = [{"timestamp": now - 3600 * i} for i in range(20)]
        stale = [{"timestamp": now - 86400 * 30}]
        self.assertTrue(consensus.is_active(consensus.activity_summary(fresh, now), 24, 10))
        self.assertFalse(consensus.is_active(consensus.activity_summary(stale, now), 24, 10))
        self.assertFalse(consensus.is_active(consensus.activity_summary([], now), 24, 10))

    def test_single_wallet_is_never_a_signal(self):
        L, now = tmp_ledger(), 1_000_000
        history = {"a": [self.trade(now - 5000)], "b": [self.trade(now - 5000)]}
        fetch = lambda w, limit=100: history[w]
        for w in history:
            consensus.ingest_wallet(L, w, fetch, now=now - 4000)   # first poll only sets cursor
        self.assertEqual(consensus.find_consensus(L, 900, 2, now=now), [])
        history["a"].append(self.trade(now - 60))
        for w in history:
            consensus.ingest_wallet(L, w, fetch, now=now)
        self.assertEqual(consensus.find_consensus(L, 900, 2, now=now), [])

    def test_two_wallets_same_side_is_signal_opposite_is_not(self):
        L, now = tmp_ledger(), 1_000_000
        history = {"a": [], "b": [], "c": []}
        fetch = lambda w, limit=100: history[w]
        for w in history:
            consensus.ingest_wallet(L, w, fetch, now=now - 1000)
        history["a"].append(self.trade(now - 300, idx=0))
        history["b"].append(self.trade(now - 120, idx=0))
        history["c"].append(self.trade(now - 100, idx=1))
        history["c"].append(self.trade(now - 90, side="SELL", idx=0))
        for w in history:
            consensus.ingest_wallet(L, w, fetch, now=now)
        sigs = consensus.find_consensus(L, 900, 2, now=now)
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0]["wallets"], ["a", "b"])
        self.assertEqual(consensus.find_consensus(L, 900, 3, now=now), [])
        # outside the timeframe -> no signal
        self.assertEqual(consensus.find_consensus(L, 900, 2, now=now + 2000), [])

    def test_two_sided_market_maker_is_not_directional(self):
        now = time.time()
        mm = [{"timestamp": now, "side": "BUY", "conditionId": f"m{i}", "outcomeIndex": j}
              for i in range(10) for j in (0, 1)]
        picker = [{"timestamp": now, "side": "BUY", "conditionId": f"m{i}", "outcomeIndex": i % 2}
                  for i in range(10)] * 2
        self.assertEqual(consensus.directional_score(mm), 0.0)
        self.assertEqual(consensus.directional_score(picker), 1.0)
        s_mm = consensus.activity_summary(mm * 5, now)
        self.assertFalse(consensus.is_active(s_mm, 24, 10, 0.8))
        self.assertTrue(consensus.is_active(consensus.activity_summary(picker, now), 24, 10, 0.8))

    def test_consensus_sizes_zero_until_history(self):
        L = tmp_ledger()
        p, n = consensus.consensus_prob(L, 0.55, 20)
        self.assertEqual((p, n), (0.55, 0))
        self.assertEqual(strategy.kelly_fraction(p, 0.55), 0.0)


class ReadOnly(unittest.TestCase):
    def test_blocks_non_allowlisted_hosts(self):
        with self.assertRaises(http.FetchError):
            http.get_json("https://example.com/order")

    def test_outbound_writes_only_in_known_modules_with_fixed_hosts(self):
        src_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ptbot")
        senders = {fn for fn in os.listdir(src_dir) if fn.endswith(".py")
                   and "data=" in open(os.path.join(src_dir, fn)).read()
                   and "urllib.request.Request" in open(os.path.join(src_dir, fn)).read()}
        self.assertEqual(senders, {"jev.py", "publish.py"})
        self.assertEqual(jev.ENDPOINT, "https://api.typesafe.ai/v1/systemone")

    def test_no_write_verbs_or_key_handling_in_source(self):
        src_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ptbot")
        banned = ["method=\"POST\"", "method='POST'", "private_key", "privateKey", "eth_sign",
                  "post_order", "create_order", "api_secret"]
        for fn in os.listdir(src_dir):
            if fn.endswith(".py"):
                with open(os.path.join(src_dir, fn)) as f:
                    text = f.read()
                for b in banned:
                    self.assertNotIn(b, text, f"{b} found in {fn}")


class EngineToggles(unittest.TestCase):
    def test_dead_strategies_off_by_default(self):
        fd, db = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        cfg = EngineTick().cfg(db)
        del cfg["strategies"]
        e = engine.Engine(cfg)
        self.assertEqual(e.enabled, {"temporal": False, "complete_set": False, "consensus": True})


class ConsensusNoFill(unittest.TestCase):
    def test_signal_logged_even_when_ask_side_is_empty(self):
        fd, db = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        e = engine.Engine(EngineTick().cfg(db))
        e.wallets = ["a", "b"]
        group = {"condition_id": "c", "outcome_idx": 1, "outcome": "Down", "token_id": "t", "slug": "s",
                 "title": "T", "wallets": ["a", "b"], "wallet_avg_price": 0.96, "first_ts": 1, "last_ts": 2}
        with mock.patch.object(consensus, "ingest_wallet"), \
             mock.patch.object(consensus, "find_consensus", return_value=[group]), \
             mock.patch.object(feeds, "order_book", return_value={"best_bid": (0.99, 10), "best_ask": None}):
            e.poll_consensus()
            e.last_consensus = 0
            e.poll_consensus()   # same group again: cooldown, no duplicate
        sigs = e.ledger.db.execute("SELECT * FROM signals").fetchall()
        self.assertEqual(len(sigs), 1)
        self.assertIsNone(sigs[0]["cost"])
        self.assertEqual(e.ledger.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 0)


class EngineTick(unittest.TestCase):
    def cfg(self, db):
        return {"db_path": db, "start_bankroll": 1000, "poll_seconds": 1,
                "market_slug_prefix": "btc-updown-15m-", "window_seconds": 900,
                "taker_fee_rate": 0.02,
                "strategies": {"temporal": True, "complete_set": True, "consensus": True},
                "temporal": {"min_edge": 0.04, "min_secs_left": 60, "max_secs_left": 840, "vol_lookback_min": 60},
                "complete_set": {"min_margin": 0.01},
                "kelly": {"multiplier": 0.25, "max_fraction": 0.05},
                "consensus": {"wallets": [], "min_wallets": 2, "window_seconds": 900, "poll_seconds": 60,
                              "max_idle_hours": 24, "min_trades_7d": 10, "min_history_for_sizing": 20}}

    def test_tick_logs_temporal_trade_then_completes_set_then_settles(self):
        fd, db = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        now = 1_800_000_000 - 1_800_000_000 % 900 + 300   # 5 min into a window, 10 min left
        start = now - 300
        market = {"condition_id": "0xabc", "slug": f"btc-updown-15m-{start}", "question": "BTC Up or Down",
                  "outcomes": ["Up", "Down"], "outcome_prices": [0.5, 0.5], "token_ids": ["UP", "DN"],
                  "closed": False, "end_date": None}
        books = {"UP": {"best_bid": (0.50, 100), "best_ask": (0.52, 500)},
                 "DN": {"best_bid": (0.46, 100), "best_ask": (0.48, 500)}}
        closes = [(start - 3600 + 60 * i, 0, 0, 0, 100000 * (1.0005 if i % 2 else 0.9995)) for i in range(60)]

        def candles(a, b):
            return [(start, 100000.0, 0, 0, 100000.0)] if b - a == 60 else closes

        E = engine.Engine(self.cfg(db))
        with mock.patch.object(engine.time, "time", return_value=now), \
             mock.patch.object(feeds, "btc_spot", return_value=(100400.0, "coinbase")), \
             mock.patch.object(feeds, "event_by_slug", return_value={"slug": market["slug"], "markets": [market]}), \
             mock.patch.object(feeds, "btc_minute_candles", side_effect=candles), \
             mock.patch.object(feeds, "order_book", side_effect=lambda t: books[t]):
            E.tick()
            trades = E.ledger.db.execute("SELECT * FROM trades").fetchall()
            self.assertEqual(len(trades), 1)
            t = trades[0]
            self.assertEqual((t["source"], t["outcome"]), ("temporal", "Up"))
            self.assertGreater(t["stake"], 0)
            self.assertAlmostEqual(t["cost"], 0.52 * 1.02)

            # BTC reverses, Down gets cheap: legged complete set should fire.
            books["DN"] = {"best_bid": (0.40, 100), "best_ask": (0.42, 5000)}
            books["UP"] = {"best_bid": (0.55, 100), "best_ask": (0.57, 5000)}
            E.tick()
            srcs = [r["source"] for r in E.ledger.db.execute("SELECT source FROM trades ORDER BY id")]
            self.assertEqual(srcs, ["temporal", "complete_set"])

        closed = dict(market, closed=True, outcome_prices=[0.0, 1.0])
        with mock.patch.object(feeds, "market_by_condition", return_value=closed):
            E.settle()
        rows = E.ledger.db.execute("SELECT * FROM trades").fetchall()
        self.assertTrue(all(r["status"] == "settled" for r in rows))
        combined = sum(r["pnl"] for r in rows)
        per_share = 1 - rows[0]["cost"] - rows[1]["cost"]
        self.assertAlmostEqual(combined, per_share * rows[0]["shares"], places=1)


class Backtest(unittest.TestCase):
    START = 1_800_000_000 - 1_800_000_000 % 900

    def cfg(self):
        c = EngineTick().cfg(":memory:")
        c["backtest"] = {"half_spread": 0.01, "max_stake_usd": 100, "max_price_age_s": 300}
        return c

    def candles(self, drift_per_min=0.0):
        """60 min of noisy pre-roll, then a window with the given drift."""
        out, px = {}, 100000.0
        for i in range(-61, 16):
            ts = self.START + 60 * i
            px *= (1.0004 if i % 2 else 0.9996) * (1 + (drift_per_min if i >= 0 else 0))
            out[ts] = (ts, px, px, px, px)
        return out

    def market(self, up_price_fn, winner):
        hist_up = [(self.START + 60 * i, up_price_fn(i)) for i in range(0, 15)]
        hist_dn = [(t, round(1 - p, 4)) for t, p in hist_up]
        return {"start": self.START, "end": self.START + 900, "condition_id": "c", "slug": "s",
                "winner_idx": winner, "hist": [hist_up, hist_dn]}

    def test_price_at_respects_time_and_staleness(self):
        h = [(100, 0.4), (160, 0.5)]
        self.assertIsNone(backtest._price_at(h, 99, 300))
        self.assertEqual(backtest._price_at(h, 159, 300), 0.4)
        self.assertEqual(backtest._price_at(h, 160, 300), 0.5)
        self.assertIsNone(backtest._price_at(h, 1000, 300))

    def test_stale_market_during_btc_rally_trades_up_and_wins(self):
        c = self.candles(drift_per_min=0.0015)
        r = backtest.simulate([self.market(lambda i: 0.5, winner=0)], c, self.cfg())
        temporal = [t for t in r["trades"] if t["source"] == "temporal"]
        self.assertEqual(len(temporal), 1)
        self.assertEqual(temporal[0]["idx"], 0)
        self.assertGreater(temporal[0]["pnl"], 0)

    def test_market_that_tracks_the_model_gives_no_temporal_trades(self):
        c = self.candles(drift_per_min=0.0015)
        cfg = self.cfg()
        s0 = c[self.START][1]

        def fair(i):
            t = self.START + 60 * i
            if t - 60 not in c or i == 0:
                return 0.5
            closes = [c[x][4] for x in range(t - 60 - 3600, t, 60) if x in c]
            sig = strategy.realized_vol_per_sec(closes)
            return round(strategy.fair_prob_up(c[t - 60][4], s0, sig, self.START + 900 - t), 4)
        r = backtest.simulate([self.market(fair, winner=0)], c, cfg)
        self.assertEqual([t for t in r["trades"] if t["source"] == "temporal"], [])

    def test_no_look_ahead_future_candles_do_not_change_early_decisions(self):
        cfg = self.cfg()
        cfg["temporal"]["max_secs_left"] = 840
        c1 = self.candles()
        c2 = dict(c1)
        for ts in range(self.START + 300, self.START + 900, 60):   # rewrite the future
            c2[ts] = (ts, 1.0, 1.0, 1.0, 1.0)
        m = self.market(lambda i: 0.5, winner=0)
        early = lambda r: [x for x in r["calib"]][:5]
        self.assertEqual(early(backtest.simulate([m], c1, cfg)), early(backtest.simulate([m], c2, cfg)))

    def test_entry_delay_fills_at_the_later_price(self):
        c = self.candles(drift_per_min=0.0015)
        cfg = self.cfg()
        stale = backtest.simulate([self.market(lambda i: 0.5, winner=0)], c, cfg)
        k = ([t for t in stale["trades"] if t["source"] == "temporal"][0]["ts"] - self.START) // 60
        # market stale at 0.5 until the signal minute, then reprices to 0.97 one print later
        m = self.market(lambda i: 0.5 if i <= k else 0.97, winner=0)
        t0 = [t for t in backtest.simulate([m], c, cfg)["trades"] if t["source"] == "temporal"][0]
        cfg["backtest"]["entry_delay_s"] = 60
        t1 = [t for t in backtest.simulate([m], c, cfg)["trades"] if t["source"] == "temporal"][0]
        self.assertEqual((t0["idx"], t1["idx"]), (0, 0))
        self.assertLess(t0["cost"], 0.6)
        self.assertGreater(t1["cost"], 0.95)       # the edge was a stale print
        self.assertLess(t1["unit_pnl"], t0["unit_pnl"])

    def test_skips_unresolved_and_missing_data(self):
        c = self.candles()
        ms = [dict(self.market(lambda i: 0.5, 0), winner_idx=None),
              dict(self.market(lambda i: 0.5, 0), hist=[[], []])]
        r = backtest.simulate(ms, c, self.cfg())
        self.assertEqual(r["skipped"]["no_winner"], 1)
        self.assertEqual(r["skipped"]["no_history"], 1)
        self.assertEqual(r["trades"], [])

    def test_summary_reports_calibration_and_verdict(self):
        c = self.candles(drift_per_min=0.0015)
        r = backtest.simulate([self.market(lambda i: 0.5, winner=0)], c, self.cfg())
        out = backtest.summarize(r, self.cfg(), 1, 1)
        self.assertIn("CALIBRATION", out)
        self.assertIn("INSUFFICIENT DATA", out)

    def test_load_with_mocked_apis_and_cache(self):
        cfg = self.cfg()
        now = self.START + 900 * 5
        calls = {"events": 0}

        def event(slug):
            calls["events"] += 1
            return {"slug": slug, "markets": [{"condition_id": "c" + slug, "slug": slug, "question": "q",
                    "outcomes": ["Up", "Down"], "outcome_prices": [1.0, 0.0], "token_ids": ["u", "d"],
                    "closed": True, "end_date": None}]}
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(feeds, "event_by_slug", side_effect=event), \
             mock.patch.object(feeds, "price_history", return_value=[(self.START, 0.5)]), \
             mock.patch.object(feeds, "btc_minute_candles_range", return_value=[(self.START, 1, 1, 1, 1)]), \
             mock.patch.object(backtest.time, "sleep"):
            ms, cs = backtest.load(cfg, 1, d, log=lambda *_: None, now=now)
            first = calls["events"]
            backtest.load(cfg, 1, d, log=lambda *_: None, now=now)
        self.assertGreater(len(ms), 0)
        self.assertTrue(all(m["winner_idx"] == 0 for m in ms))
        self.assertTrue(all(m["end"] <= now - 1800 for m in ms))
        self.assertEqual(calls["events"], first)   # second run served from cache


class Momentum(unittest.TestCase):
    def series(self, fn, n=900):
        return [(1_500_000_000 + 86400 * i, fn(i)) for i in range(n)]

    def test_uptrend_stays_long_downtrend_goes_flat(self):
        up = self.series(lambda i: 100 * 1.002 ** i * (1.01 if i % 2 else 0.99))
        down = self.series(lambda i: 100 * 0.998 ** i * (1.01 if i % 2 else 0.99))
        _, _, pos_up = momentum.simulate(up, 90)
        _, _, pos_dn = momentum.simulate(down, 90)
        self.assertTrue(all(p > 0 for p in pos_up))
        self.assertTrue(all(p == 0 for p in pos_dn))

    def test_no_look_ahead(self):
        base = self.series(lambda i: 100 * (1.01 if i % 3 else 0.985) ** (i % 50))
        s1, _, p1 = momentum.simulate(base, 90, cost=0)
        cut = 500
        changed = base[:cut] + [(t, c * 3) for t, c in base[cut:]]   # rewrite the future
        s2, _, p2 = momentum.simulate(changed, 90, cost=0)
        start = max(90, 31)
        k = cut - start - 1              # last index whose position used only data before `cut`
        self.assertEqual(p1[:k], p2[:k])
        self.assertEqual(s1[:k - 1], s2[:k - 1])

    def test_costs_are_charged_on_turnover(self):
        zig = self.series(lambda i: 100 * (1.3 if (i // 60) % 2 else 1.0) * (1.01 if i % 2 else 0.99))
        free, _, _ = momentum.simulate(zig, 30, cost=0)
        paid, _, _ = momentum.simulate(zig, 30, cost=0.01)
        self.assertLess(sum(paid), sum(free))

    def test_leverage_cap(self):
        calm = self.series(lambda i: 100 * 1.001 ** i * (1.0005 if i % 2 else 0.9995))
        _, _, pos = momentum.simulate(calm, 90, target_vol=5.0, max_leverage=1.0)
        self.assertLessEqual(max(pos), 1.0)

    def test_metrics_and_verdict(self):
        m = momentum.metrics([0.01, -0.005] * 200)
        self.assertGreater(m["sharpe"], 0)
        self.assertLess(m["max_dd"], 0)
        flat = momentum.metrics([0.0001, -0.0001] * 200)
        v = momentum.verdict([flat, flat], [m, m], flat, m, 1.1)
        self.assertIn("NOT", v)

    def test_report_shows_every_lookback_and_benchmark(self):
        closes = self.series(lambda i: 100 * 1.001 ** i * (1.02 if i % 2 else 0.98), n=1200)
        params = {"rebalance_days": 7, "vol_window": 30, "target_vol": 0.5, "max_leverage": 1.0,
                  "allow_short": False, "cost": 0.005}
        out = momentum.report("TEST", closes, [30, 90, 365], params)
        for s in ("buy & hold", "momentum  30d", "momentum  90d", "momentum 365d", "split test", "verdict"):
            self.assertIn(s, out)


class Dashboard(unittest.TestCase):
    def cfg(self, db):
        c = EngineTick().cfg(db)
        c["consensus"]["wallets"] = ["0xAAA", "0xBBB"]
        return c

    def test_state_without_ledger(self):
        cfg = self.cfg("/nonexistent/none.sqlite")
        del cfg["strategies"]
        st = dashboard.ledger_state(cfg)
        self.assertFalse(st["has_db"])
        self.assertFalse(st["enabled"]["temporal"])

    def test_state_reads_ledger_and_detects_consensus(self):
        L = tmp_ledger()
        now = int(time.time())
        L.log_tick(ts=now - 5, btc=1.0, btc_source="coinbase", slug="s", start_price=1.0, secs_left=100,
                   sigma=1e-5, fair_up=0.5, ask_up=0.51, ask_down=0.5, bid_up=0.49, bid_down=0.48)
        sid = L.log_signal("consensus", "c1", "s", 0, "Up", 0.5, 0.5, 0.0)
        L.open_trade(sid, "consensus", "c1", "s", 0, "Up", "t", 0.5, 0.0)
        L.settle_market("c1", 0)
        for w in ("0xaaa", "0xbbb"):
            L.db.execute("INSERT INTO wallet_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (w, now - 60, "m9", "tok", 1, "Down", "BUY", 0.4, 5, "slug", "title", "tx" + w))
        L.db.commit()
        path = L.db.execute("PRAGMA database_list").fetchone()[2]
        st = dashboard.ledger_state(self.cfg(path), now=now)
        self.assertTrue(st["has_db"] and st["running"])
        self.assertEqual(st["sources"]["consensus"]["settled"], 1)
        self.assertEqual(st["trades"][0]["unit_pnl"], 0.5)
        self.assertEqual(len(st["consensus_live"]), 1)
        self.assertEqual(st["consensus_live"][0]["outcome"], "Down")

    def test_momentum_state_shape_and_signal(self):
        def fake(product, s, e, g):
            return [(t, 100 * 1.002 ** i, 0, 0, 100 * 1.002 ** i * (1.01 if i % 2 else 0.99))
                    for i, t in enumerate(range(1_400_000_000, 1_400_000_000 + 1500 * 86400, 86400)) if s <= t < e]
        with tempfile.TemporaryDirectory() as d, mock.patch.object(feeds, "candles", side_effect=fake), \
             mock.patch.object(momentum.time, "sleep"):
            out = dashboard.momentum_state(self.cfg(":memory:"), d, assets=("TEST-USD",), start="2014-05-13")
        a = out["assets"]["TEST-USD"]
        self.assertEqual(set(a["lookbacks"]), {"30", "90", "180", "365"})
        self.assertEqual(a["lookbacks"]["365"]["signal"], "HOLD")
        self.assertEqual(len(a["lookbacks"]["365"]["curve"]), len(a["buyhold"]["curve"]))

    def test_ridge_hindsight_pair_and_lab_stats(self):
        L = tmp_ledger()
        start = 1_800_000_000 - 1_800_000_000 % 900
        for i, (u, d) in enumerate([(0.50, 0.52), (0.40, 0.62), (0.70, 0.31), (0.55, 0.47)]):
            L.log_tick(ts=start + 60 * i, btc=1.0, btc_source="c", slug=f"btc-updown-15m-{start}", start_price=1.0,
                       secs_left=900 - 60 * i, sigma=1e-5, fair_up=0.5, ask_up=u, ask_down=d, bid_up=None, bid_down=None)
        r = dashboard.ridge_data(L.db, start + 300)
        self.assertEqual(len(r), 1)
        self.assertAlmostEqual(r[0]["pair"], 0.40 + 0.31)      # cheapest Up + cheapest Down, hindsight
        self.assertTrue(r[0]["live"])
        self.assertEqual([p[1] for p in r[0]["points"]], [0.50, 0.40, 0.31, 0.47])
        st = dashboard.lab_stats(L.db, start + 300)
        self.assertEqual(st["uptime_days"], 1)
        self.assertEqual(st["streak_days"], 1)

    def test_lab_endpoint_counts_logged_work(self):
        import threading, urllib.request, json as _json
        from http.server import ThreadingHTTPServer
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "results"))
            with open(os.path.join(d, "results", "lab_log.jsonl"), "w") as f:
                f.write('{"kind": "backtest", "entry_delay_s": 0}\n{"kind": "backtest", "entry_delay_s": 60}\nnot json\n')
            srv = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler(self.cfg(os.path.join(d, "x.sqlite")), d))
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                lab = _json.loads(urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/api/lab").read())
            finally:
                srv.shutdown()
                srv.server_close()
        self.assertEqual(lab, {"backtest_runs": 2, "stress_tested": True, "momentum_runs": 0})

    def test_server_serves_page_and_api_on_localhost(self):
        import threading, urllib.request, json as _json
        from http.server import ThreadingHTTPServer
        with tempfile.TemporaryDirectory() as d:
            srv = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler(self.cfg(os.path.join(d, "x.sqlite")), d))
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{srv.server_address[1]}"
            try:
                page = urllib.request.urlopen(base + "/").read().decode()
                self.assertIn("Paper Desk", page)
                self.assertEqual(_json.loads(urllib.request.urlopen(base + "/api/state").read())["has_db"], False)
                self.assertIsNone(_json.loads(urllib.request.urlopen(base + "/api/backtest").read()))
            finally:
                srv.shutdown()
                srv.server_close()


class JevChallenge(unittest.TestCase):
    class FakeResp:
        def __init__(self, body): self.body = body
        def read(self): return self.body
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def test_state_is_blind_to_market_price(self):
        st = jev.build_state(1_800_000_000, 1_800_000_900, 1_800_000_300, 100000.0, 100200.0,
                             [100000.0 + i * 10 for i in range(16)])
        self.assertEqual(st["window"]["seconds_remaining"], 600)
        self.assertAlmostEqual(st["btc"]["change_since_open_pct"], 0.2)
        self.assertEqual(len(st["btc"]["last_minute_closes"]), 15)
        self.assertNotIn("ask", json.dumps(st).lower())        # never shown the market price

    def test_request_shape_and_parse(self):
        sent = {}
        def opener(req, timeout):
            sent["url"], sent["auth"] = req.full_url, req.get_header("Authorization")
            sent["body"] = json.loads(req.data.decode())
            return self.FakeResp(b'{"model": "jev-1", "answers": {"up": {"type": "noul", "noul": 0.62}}}')
        p, ms = jev.ask_up({"window": {}}, "KEY", _open=opener)
        self.assertEqual(p, 0.62)
        self.assertEqual(sent["url"], "https://api.typesafe.ai/v1/systemone")
        self.assertEqual(sent["auth"], "Bearer KEY")
        self.assertEqual(sent["body"]["model"], "jev-latest")
        self.assertEqual(sent["body"]["questions"]["up"]["type"], "noul")

    def test_errors(self):
        with self.assertRaises(jev.JevError):
            jev.ask_up({}, "")
        bad = lambda req, timeout: self.FakeResp(b'{"answers": {}}')
        with self.assertRaises(jev.JevError):
            jev.ask_up({}, "K", _open=bad)
        oob = lambda req, timeout: self.FakeResp(b'{"answers": {"up": {"noul": 1.4}}}')
        with self.assertRaises(jev.JevError):
            jev.ask_up({}, "K", _open=oob)

    def test_paired_brier_clusters_by_window(self):
        rows = [(f"w{i // 10}", 0.9, 0.6, 1) for i in range(100)]   # 10 windows x 10 preds
        st = jev.paired_brier(rows)
        self.assertEqual((st["n_preds"], st["n_windows"]), (100, 10))
        self.assertLess(st["brier_jev"], st["brier_other"])
        self.assertLess(st["diff"], 0)
        self.assertEqual(jev.verdict(st)[0], "collecting")          # 10 windows is not enough
        many = [(f"w{i}", 0.9 if i % 2 else 0.2, 0.6 if i % 2 else 0.4, i % 2) for i in range(400)]
        self.assertEqual(jev.verdict(jev.paired_brier(many))[0], "beats")
        worse = [(f"w{i}", 0.5, 0.9 if i % 2 else 0.1, i % 2) for i in range(400)]
        self.assertEqual(jev.verdict(jev.paired_brier(worse))[0], "worse")

    def test_engine_logs_and_settles_predictions(self):
        fd, db = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        cfg = EngineTick().cfg(db)
        cfg["jev"] = {"enabled": True, "every_seconds": 60}
        e = engine.Engine(cfg)
        start = 1_800_000_000 - 1_800_000_000 % 900
        win = {"start": start, "end": start + 900, "slug": f"btc-updown-15m-{start}", "start_price": 100000.0}
        with mock.patch.object(feeds, "btc_minute_candles", return_value=[(start, 1, 1, 1, 100000.0)] * 16), \
             mock.patch.object(jev, "ask_up", return_value=(0.7, 120.0)), \
             mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "K"}):
            e.jev_step(win, start + 300, 100100.0, 600, 0.66, 0.64)
            e.jev_step(win, start + 310, 100100.0, 590, 0.66, 0.64)   # inside every_seconds: skipped
        rows = e.ledger.db.execute("SELECT * FROM jev_preds").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["jev_up"], rows[0]["model_up"], rows[0]["market_up"]), (0.7, 0.66, 0.64))
        closed = {"slug": "s", "markets": [{"condition_id": "c", "slug": "s", "question": "q", "outcomes": ["Up", "Down"],
                  "outcome_prices": [1.0, 0.0], "token_ids": ["a", "b"], "closed": True, "end_date": None}]}
        with mock.patch.object(feeds, "event_by_slug", return_value=closed), \
             mock.patch.object(engine.time, "time", return_value=start + 2000):
            e.settle_jev()
        self.assertEqual(e.ledger.db.execute("SELECT outcome FROM jev_preds").fetchone()[0], 1)
        st = jev.challenge_stats(e.ledger.db)
        self.assertEqual((st["asked"], st["settled"], st["verdict"]), (1, 1, "collecting"))

    def test_jev_errors_are_logged_not_fatal(self):
        fd, db = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        cfg = EngineTick().cfg(db)
        cfg["jev"] = {"enabled": True}
        e = engine.Engine(cfg)
        win = {"start": 0, "end": 900, "slug": "s", "start_price": 1.0}
        with mock.patch.object(feeds, "btc_minute_candles", return_value=[(0, 1, 1, 1, 1.0)] * 16), \
             mock.patch.object(jev, "ask_up", side_effect=jev.JevError("HTTP 401")):
            e.jev_step(win, 300, 1.0, 600, 0.5, 0.5)
        r = e.ledger.db.execute("SELECT jev_up, error FROM jev_preds").fetchone()
        self.assertIsNone(r[0])
        self.assertIn("401", r[1])


class PublishGuards(unittest.TestCase):
    def test_sync_off_without_config(self):
        cfg = EngineTick().cfg(":memory:")
        self.assertFalse(publish.Syncer(cfg, "/tmp").enabled)
        self.assertIsNone(publish.Syncer(cfg, "/tmp").maybe_sync())

    def test_only_https_vercel_app_hosts(self):
        self.assertEqual(publish.check_url("https://ptbot-desk.vercel.app/x"), "https://ptbot-desk.vercel.app")
        for bad in ("http://ptbot-desk.vercel.app", "https://evil.com", "https://vercel.app.evil.com", "", None):
            with self.assertRaises(publish.SyncError):
                publish.check_url(bad)


if __name__ == "__main__":
    unittest.main()
