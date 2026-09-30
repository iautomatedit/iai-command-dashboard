"""Step D: honest report straight from the ledger."""
import math
import time

SOURCES = ("temporal", "complete_set", "consensus")


def _stats(xs):
    n = len(xs)
    if n == 0:
        return None
    mean = sum(xs) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in xs) / (n - 1)) if n > 1 else 0.0
    return {"n": n, "mean": mean, "se": sd / math.sqrt(n) if n > 1 else float("inf")}


def verdict(stats, min_n=30):
    if not stats or stats["n"] < min_n:
        n = stats["n"] if stats else 0
        return f"INSUFFICIENT DATA ({n} settled, need {min_n}+). No conclusion either way."
    lo = stats["mean"] - 2 * stats["se"]
    hi = stats["mean"] + 2 * stats["se"]
    if lo > 0:
        return f"Positive edge so far (95% range {lo:+.4f} to {hi:+.4f} per $1 share). Keep running; one good stretch is not proof."
    if hi < 0:
        return f"NEGATIVE edge (95% range {lo:+.4f} to {hi:+.4f} per $1 share). Strategy loses money after fees."
    return f"NO DETECTABLE EDGE (95% range {lo:+.4f} to {hi:+.4f} per $1 share, includes zero)."


def build(ledger, wallets):
    db = ledger.db
    started = int(ledger.meta("started_at"))
    ticks = db.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM ticks").fetchone()
    btc_srcs = [r[0] for r in db.execute("SELECT DISTINCT btc_source FROM ticks")]
    markets = db.execute("SELECT COUNT(DISTINCT slug) FROM ticks").fetchone()[0]
    hours = ((ticks[2] or started) - (ticks[1] or started)) / 3600

    lines = [
        "REAL DATA SOURCES CONFIRMED:",
        f"  BTC feed: {', '.join(btc_srcs) or 'none recorded yet'} ({ticks[0]} ticks logged)",
        f"  Polymarket: Gamma (markets), CLOB (order books), Data API (wallet trades); {markets} BTC markets observed",
        f"  Wallets tracked: {len(wallets)}",
    ]
    lines += [f"    {w}" for w in wallets]
    lines.append(f"  Tracked period: {hours:.1f} hours of live data since {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(started))}")
    lines.append("")
    lines.append("SIGNALS GENERATED:")
    for s in SOURCES:
        n = db.execute("SELECT COUNT(*) FROM signals WHERE source=?", (s,)).fetchone()[0]
        lines.append(f"  {s:13s} {n}")
    lines.append("")
    lines.append("PAPER TRADING RESULTS:")
    start = float(ledger.meta("start_bankroll"))
    total_pnl = 0.0
    for s in SOURCES:
        rows = db.execute("SELECT * FROM trades WHERE source=?", (s,)).fetchall()
        settled = [r for r in rows if r["status"] == "settled"]
        open_n = len(rows) - len(settled)
        pnl = sum(r["pnl"] for r in settled)
        staked = sum(r["stake"] for r in settled)
        total_pnl += pnl
        wins = sum(r["won"] for r in settled)
        st = _stats([r["unit_pnl"] for r in settled])
        lines.append(f"  [{s}] trades: {len(settled)} settled, {open_n} open")
        if settled:
            roi = (pnl / staked * 100) if staked else 0.0
            lines.append(f"    win rate: {wins}/{len(settled)} ({wins / len(settled) * 100:.1f}%)")
            lines.append(f"    Kelly-sized P&L: ${pnl:+.2f} on ${staked:.2f} staked (ROI {roi:+.1f}%)")
            lines.append(f"    avg P&L per $1-payout share (after fees): {st['mean']:+.4f}")
        lines.append(f"    verdict: {verdict(st)}")
    from . import jev as _jev
    js = _jev.challenge_stats(db)
    if js:
        lines.append("")
        lines.append("JEV CHALLENGE (measurement only, never trades):")
        lines.append(f"  asked {js['asked']}, answered {js['answered']}, errors {js['errors']}, "
                     f"settled {js['settled']}" + (f", avg latency {js['avg_latency_ms']:.0f} ms" if js["avg_latency_ms"] else ""))
        for name, st in (("vs Polymarket price", js["vs_market"]), ("vs our model", js["vs_model"])):
            if st:
                lines.append(f"  {name}: Brier Jev {st['brier_jev']:.4f} vs {st['brier_other']:.4f} over "
                             f"{st['n_windows']} windows (diff 95% range {st['lo']:+.4f} to {st['hi']:+.4f}; negative = Jev better)")
        lines.append(f"  verdict: {js['verdict_label']}")
    lines.append("")
    lines.append(f"  Paper bankroll: ${start:.2f} start -> ${start + total_pnl:.2f} realized "
                 f"({total_pnl:+.2f}), ${ledger.bankroll():.2f} free after open positions")
    lines.append("")
    lines.append("  Assumptions: entries fill at the real best ask at signal time (no slippage beyond")
    lines.append("  top-of-book size), taker fee modeled at the configured rate, all positions held to")
    lines.append("  Polymarket's official resolution. Consensus entries are at OUR ask, after the wallets")
    lines.append("  already moved the price, which is the realistic copy-trading fill.")
    return "\n".join(lines)
