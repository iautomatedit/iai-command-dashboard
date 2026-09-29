"""CLI: python -m ptbot {check|discover|verify|run|report|backtest|momentum}"""
import argparse
import json
import os
import sys
import time

from . import backtest, consensus, feeds, momentum, report
from .engine import Engine
from .ledger import Ledger

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_cfg(path):
    with open(path) as f:
        cfg = json.load(f)
    if not os.path.isabs(cfg["db_path"]):
        cfg["db_path"] = os.path.join(HERE, cfg["db_path"])
    return cfg


def cmd_check(cfg, args):
    """Step 0: confirm every real data source responds. Exit 1 if any fail."""
    ok = True
    try:
        px, src = feeds.btc_spot()
        print(f"[OK]   BTC spot: ${px:,.2f} from {src}")
    except feeds.FetchError as e:
        ok = False
        print(f"[FAIL] BTC spot: {e}")
    w = cfg["window_seconds"]
    slug = f"{cfg['market_slug_prefix']}{int(time.time()) // w * w}"
    try:
        ev = feeds.event_by_slug(slug)
        if not ev:
            raise feeds.FetchError(f"no event for slug {slug}")
        m = ev["markets"][0]
        print(f"[OK]   Polymarket Gamma: {m['question']} outcomes={m['outcomes']}")
        b = feeds.order_book(m["token_ids"][0])
        print(f"[OK]   Polymarket CLOB book ({m['outcomes'][0]}): bid={b['best_bid']} ask={b['best_ask']}")
        t = feeds.market_trades(m["condition_id"], limit=5)
        print(f"[OK]   Polymarket Data API: {len(t)} recent trades on this market")
    except (feeds.FetchError, KeyError, IndexError) as e:
        ok = False
        print(f"[FAIL] Polymarket: {e}")
    print("[OK]   Credentials: none used. HTTP layer is GET-only against a public-data allowlist;")
    print("       no API keys, wallet keys, signing, or order placement exist in this code.")
    sys.exit(0 if ok else 1)


def recent_btc_condition_ids(cfg, n_windows):
    w = cfg["window_seconds"]
    now = int(time.time()) // w * w
    cids = []
    for i in range(1, n_windows + 1):
        ev = feeds.event_by_slug(f"{cfg['market_slug_prefix']}{now - i * w}")
        if ev and ev["markets"]:
            cids.append(ev["markets"][0]["condition_id"])
    return cids


def cmd_discover(cfg, args):
    """Find wallets that actually trade these markets, then verify activity
    from each wallet's own public history."""
    c = cfg["consensus"]
    cids = recent_btc_condition_ids(cfg, args.windows)
    print(f"scanned {len(cids)} recent BTC markets")
    cands = consensus.discover_candidates(cids, top_n=args.top)
    checked = consensus.verify_wallets([w for w, _ in cands], c["max_idle_hours"], c["min_trades_7d"],
                                       c.get("min_directional", 0.8))
    markets = dict(cands)
    print(f"{'wallet':44s} {'mkts':>4s} {'last(h)':>7s} {'24h':>4s} {'7d':>4s} {'1-side':>6s} use")
    for w, s, ok in checked:
        print(f"{w:44s} {markets[w]:4d} {str(s['last_trade_age_h']):>7s} {s['trades_24h']:4d} {s['trades_7d']:4d} "
              f"{str(s['directional']):>6s} {'YES' if ok else 'no'}")
    print("1-side = share of markets where the wallet bought only one outcome. Two-sided bots are market makers, not signals.")
    chosen = [w for w, _, a in checked if a][: args.pick]
    if args.write and chosen:
        raw = json.load(open(args.config))
        raw["consensus"]["wallets"] = chosen
        json.dump(raw, open(args.config, "w"), indent=2)
        print(f"wrote {len(chosen)} active wallets to {args.config}")


def active_wallets(cfg):
    c = cfg["consensus"]
    good = []
    for w, s, active in consensus.verify_wallets(c["wallets"], c["max_idle_hours"], c["min_trades_7d"],
                                                 c.get("min_directional", 0.8)):
        print(f"{'ACTIVE ' if active else 'DROPPED'} {w} last trade {s['last_trade_age_h']}h ago, "
              f"{s['trades_7d']} trades in 7d, one-sided in {s['directional']} of markets")
        if active:
            good.append(w.lower())
    return good


def cmd_verify(cfg, args):
    active_wallets(cfg)


def cmd_run(cfg, args):
    wallets = active_wallets(cfg)
    if 0 < len(wallets) < cfg["consensus"]["min_wallets"]:
        print("fewer active wallets than min_wallets; consensus check disabled for this run")
        wallets = []
    Engine(cfg).run(wallets)


def cmd_backtest(cfg, args):
    bt = cfg.setdefault("backtest", {})
    for flag, key in (("max_age", "max_price_age_s"), ("spread", "half_spread"), ("delay", "entry_delay_s")):
        if getattr(args, flag) is not None:
            bt[key] = getattr(args, flag)
    try:
        print(backtest.run(cfg, args.days, os.path.join(HERE, "bt_cache")))
    except feeds.FetchError as e:
        print(f"[FAIL] {e}")
        sys.exit(1)


def cmd_momentum(cfg, args):
    try:
        print(momentum.run(args.assets.split(","), args.start, cfg, os.path.join(HERE, "bt_cache")))
    except feeds.FetchError as e:
        print(f"[FAIL] {e}")
        sys.exit(1)


def cmd_report(cfg, args):
    print(report.build(Ledger(cfg["db_path"], cfg["start_bankroll"]), cfg["consensus"]["wallets"]))


def main():
    ap = argparse.ArgumentParser(prog="ptbot", description="Paper-trading research bot. Never trades.")
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    d = sub.add_parser("discover")
    d.add_argument("--windows", type=int, default=24, help="recent 15m markets to scan")
    d.add_argument("--top", type=int, default=40)
    d.add_argument("--pick", type=int, default=5)
    d.add_argument("--write", action="store_true", help="save active wallets into config")
    sub.add_parser("verify")
    sub.add_parser("run")
    sub.add_parser("report")
    b = sub.add_parser("backtest")
    b.add_argument("--days", type=float, default=7, help="days of past markets to replay")
    b.add_argument("--max-age", dest="max_age", type=int, help="ignore market prices older than N seconds")
    b.add_argument("--spread", type=float, help="assumed half spread added to the price, e.g. 0.02")
    b.add_argument("--delay", type=int, help="fill N seconds after the signal (latency test)")
    mo = sub.add_parser("momentum", help="time-series momentum backtest on daily prices")
    mo.add_argument("--assets", default="BTC-USD,ETH-USD")
    mo.add_argument("--start", default="2016-06-01", help="YYYY-MM-DD")
    args = ap.parse_args()
    cfg = load_cfg(args.config)
    {"check": cmd_check, "discover": cmd_discover, "verify": cmd_verify,
     "run": cmd_run, "report": cmd_report, "backtest": cmd_backtest,
     "momentum": cmd_momentum}[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
