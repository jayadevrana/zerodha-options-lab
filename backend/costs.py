"""Zerodha NFO options cost model (2026 schedule, all configurable).

Entry costs are fixed at order time. Exit-at-expiry costs depend on the
terminal spot (STT on intrinsic for long ITM legs), so total cost is a
FUNCTION of S_T — it must be subtracted inside the payoff before POP/EV,
not bolted on afterwards.
"""
from __future__ import annotations

BROKERAGE_PER_ORDER = 20.0        # flat per executed F&O order
STT_SELL_PREMIUM = 0.001          # 0.1% of premium on sells
STT_EXERCISE_INTRINSIC = 0.00125  # 0.125% of intrinsic on long ITM at expiry
EXCH_TXN = 0.0003503              # NSE options: 0.03503% of premium
SEBI = 0.000001                   # Rs 10 / crore
STAMP_BUY = 0.00003               # 0.003% of premium on buys
GST = 0.18                        # on brokerage + exchange + SEBI


def entry_costs(legs: list[dict]) -> float:
    """Fixed costs paid when the basket is executed (rupees)."""
    total = 0.0
    for l in legs:
        qty = int(l["lots"]) * int(l["lot_size"])
        turnover = float(l["entry_price"]) * qty
        exch = turnover * EXCH_TXN
        sebi = turnover * SEBI
        total += BROKERAGE_PER_ORDER + exch + sebi + GST * (BROKERAGE_PER_ORDER + exch + sebi)
        if int(l["side"]) < 0:
            total += turnover * STT_SELL_PREMIUM
        else:
            total += turnover * STAMP_BUY
    return round(total, 2)


def expiry_costs_at(legs: list[dict], s_t: float) -> float:
    """Costs realized at expiry settlement, as a function of terminal spot.
    Long ITM legs pay STT on intrinsic; short legs and OTM legs pay nothing."""
    total = 0.0
    for l in legs:
        if int(l["side"]) <= 0 or l["kind"] == "FUT":
            continue
        qty = int(l["lots"]) * int(l["lot_size"])
        if l["kind"] == "CE":
            intr = max(s_t - float(l["strike"]), 0.0)
        else:
            intr = max(float(l["strike"]) - s_t, 0.0)
        total += intr * qty * STT_EXERCISE_INTRINSIC
    return total


def cost_summary(legs: list[dict], spot: float) -> dict:
    return {
        "entry": entry_costs(legs),
        "expiry_at_spot": round(expiry_costs_at(legs, spot), 2),
        "note": "expiry STT varies with terminal spot; folded into net payoff",
    }
