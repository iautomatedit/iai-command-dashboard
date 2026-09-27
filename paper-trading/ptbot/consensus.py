"""Part B: multi-wallet consensus.

Wallet selection is evidence-based: a wallet is only tracked if its own
public trade history shows real recent activity. A social media claim is
not evidence. A single wallet's move is never a signal on its own.
"""
import time
from collections import defaultdict

from . import feeds

DAY = 86400


def activity_summary(trades, now=None):
    now = now or time.time()
    ts = sorted((int(t["timestamp"]) for t in trades), reverse=True)
    return {
        "n_trades_page": len(ts),
        "last_trade_age_h": round((now - ts[0]) / 3600, 1) if ts else None,
        "trades_24h": sum(1 for t in ts if now - t <= DAY),
        "trades_7d": sum(1 for t in ts if now - t <= 7 * DAY),
    }


def is_active(summary, max_idle_hours, min_trades_7d):
    return (
        summary["last_trade_age_h"] is not None
        and summary["last_trade_age_h"] <= max_idle_hours
        and summary["trades_7d"] >= min_trades_7d
    )


def verify_wallets(wallets, max_idle_hours, min_trades_7d, fetch=feeds.wallet_trades):
    """Return [(wallet, summary, active_bool)] from each wallet's real trades."""
    out = []
    for w in wallets:
        s = activity_summary(fetch(w, limit=100))
        out.append((w, s, is_active(s, max_idle_hours, min_trades_7d)))
    return out


def discover_candidates(condition_ids, top_n=15, fetch=feeds.market_trades):
    """Rank wallets by how many distinct recent BTC markets they traded in."""
    markets_per_wallet = defaultdict(set)
    for cid in condition_ids:
        try:
            for t in fetch(cid, limit=500):
                w = t.get("proxyWallet")
                if w:
                    markets_per_wallet[w.lower()].add(cid)
        except feeds.FetchError:
            continue
    ranked = sorted(markets_per_wallet.items(), key=lambda kv: len(kv[1]), reverse=True)
    return [(w, len(m)) for w, m in ranked[:top_n]]


def ingest_wallet(ledger, wallet, fetch=feeds.wallet_trades, now=None):
    """Store new trades for a wallet. First poll only sets a cursor so that
    historical trades never fire a signal retroactively."""
    now = int(now or time.time())
    row = ledger.db.execute("SELECT last_ts FROM wallet_cursor WHERE wallet=?", (wallet,)).fetchone()
    trades = fetch(wallet, limit=100)
    if row is None:
        last = max([int(t["timestamp"]) for t in trades] + [now])
        ledger.db.execute("INSERT INTO wallet_cursor VALUES (?,?)", (wallet, last))
        ledger.db.commit()
        return 0
    cursor = row["last_ts"]
    new = [t for t in trades if int(t["timestamp"]) > cursor]
    for t in new:
        ledger.db.execute(
            "INSERT OR IGNORE INTO wallet_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (wallet, int(t["timestamp"]), t.get("conditionId"), str(t.get("asset")),
             t.get("outcomeIndex"), t.get("outcome"), t.get("side"),
             float(t.get("price") or 0), float(t.get("size") or 0),
             t.get("slug"), t.get("title"), t.get("transactionHash")),
        )
    if new:
        ledger.db.execute("UPDATE wallet_cursor SET last_ts=? WHERE wallet=?",
                          (max(int(t["timestamp"]) for t in new), wallet))
    ledger.db.commit()
    return len(new)


def find_consensus(ledger, window_seconds, min_wallets, now=None):
    """Groups where >= min_wallets distinct wallets BOUGHT the same outcome of
    the same market within the window. SELLs are exits, not signals."""
    now = int(now or time.time())
    rows = ledger.db.execute(
        "SELECT * FROM wallet_events WHERE side='BUY' AND ts>=? ORDER BY ts",
        (now - window_seconds,),
    ).fetchall()
    groups = defaultdict(list)
    for r in rows:
        groups[(r["condition_id"], r["outcome_idx"])].append(r)
    out = []
    for (cid, idx), evs in groups.items():
        wallets = sorted({e["wallet"] for e in evs})
        if len(wallets) >= min_wallets:
            out.append({
                "condition_id": cid, "outcome_idx": idx,
                "outcome": evs[-1]["outcome"], "token_id": evs[-1]["token_id"],
                "slug": evs[-1]["slug"], "title": evs[-1]["title"],
                "wallets": wallets,
                "wallet_avg_price": sum(e["price"] for e in evs) / len(evs),
                "first_ts": evs[0]["ts"], "last_ts": evs[-1]["ts"],
            })
    return out


def consensus_prob(ledger, cost, min_history):
    """Probability estimate for a consensus signal.

    There is no honest prior that copying wallets beats the market, so
    until we have min_history settled consensus trades, p = cost (zero
    edge) and Kelly sizes the position at $0. The signal is still logged
    and tracked at 1 share-equivalent via unit_pnl, so the edge (or lack of
    it) is measured. After that, p = cost + observed average excess return.
    """
    hist = ledger.settled_unit_pnls("consensus")
    if len(hist) < min_history:
        return cost, len(hist)
    excess = sum(hist) / len(hist)
    return min(0.99, max(0.01, cost + excess)), len(hist)
