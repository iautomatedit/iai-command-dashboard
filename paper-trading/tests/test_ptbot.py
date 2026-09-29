import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ptbot import backtest, consensus, engine, feeds, http, report, strategy  # noqa: E402
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

    def test_consensus_sizes_zero_until_history(self):
        L = tmp_ledger()
        p, n = consensus.consensus_prob(L, 0.55, 20)
        self.assertEqual((p, n), (0.55, 0))
        self.assertEqual(strategy.kelly_fraction(p, 0.55), 0.0)


class ReadOnly(unittest.TestCase):
    def test_blocks_non_allowlisted_hosts(self):
        with self.assertRaises(http.FetchError):
            http.get_json("https://example.com/order")

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


class EngineTick(unittest.TestCase):
    def cfg(self, db):
        return {"db_path": db, "start_bankroll": 1000, "poll_seconds": 1,
                "market_slug_prefix": "btc-updown-15m-", "window_seconds": 900,
                "taker_fee_rate": 0.02,
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


if __name__ == "__main__":
    unittest.main()
