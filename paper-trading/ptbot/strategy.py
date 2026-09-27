"""Part A: strategy math. Pure functions, no I/O, fully unit tested."""
import math


def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def realized_vol_per_sec(closes, bar_seconds=60):
    """Stdev of log returns between bars, scaled to per-second."""
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(rets) < 10:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) / math.sqrt(bar_seconds)


def fair_prob_up(spot, start_price, sigma_per_sec, seconds_left):
    """P(price at window end >= start price), driftless lognormal model.

    This is the 'where odds should be given where BTC is right now' number
    that temporal arbitrage compares against the market's quoted odds.
    """
    if seconds_left <= 0:
        return 1.0 if spot >= start_price else 0.0
    if not sigma_per_sec or sigma_per_sec <= 0:
        return None
    z = math.log(spot / start_price) / (sigma_per_sec * math.sqrt(seconds_left))
    return norm_cdf(z)


def cost_per_share(ask, fee_rate):
    """All-in cost to buy one $1-payout share at the ask, including taker fee."""
    return ask * (1.0 + fee_rate)


def kelly_fraction(p, cost):
    """f* = (p*b - q) / b, with b = net odds per $1 staked = (1 - cost) / cost.

    Algebraically equal to (p - cost) / (1 - cost). Returns 0 when there is
    no edge; never negative (we do not short in this paper book).
    """
    if p is None or cost <= 0 or cost >= 1:
        return 0.0
    b = (1.0 - cost) / cost
    q = 1.0 - p
    f = (p * b - q) / b
    return max(0.0, f)


def position_size(bankroll, p, cost, kelly_mult, max_frac):
    """Fractional Kelly stake in dollars, capped. Never a fixed bet size."""
    f = kelly_fraction(p, cost) * kelly_mult
    return round(bankroll * min(f, max_frac), 2)


def temporal_signal(fair_up, cost_up, cost_down, min_edge):
    """Return (outcome_idx, p, cost, edge) for the side with the biggest gap
    between model probability and all-in market cost, or None.

    outcome_idx 0 = Up, 1 = Down.
    """
    if fair_up is None:
        return None
    cands = []
    if cost_up is not None:
        cands.append((0, fair_up, cost_up, fair_up - cost_up))
    if cost_down is not None:
        cands.append((1, 1.0 - fair_up, cost_down, (1.0 - fair_up) - cost_down))
    cands = [c for c in cands if c[3] >= min_edge]
    return max(cands, key=lambda c: c[3]) if cands else None


def complete_set_gap(cost_held, cost_other, min_margin):
    """Locked profit per share if we complete a set now, else None.

    A complete set (one Up + one Down share) always pays exactly $1.
    """
    if cost_held is None or cost_other is None:
        return None
    locked = 1.0 - cost_held - cost_other
    return locked if locked >= min_margin else None
