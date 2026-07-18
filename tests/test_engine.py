"""Offline sanity checks for the pricing/payoff/analytics engines."""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import analytics, payoff as po, pricing


def approx(a, b, tol=1e-2):
    assert abs(a - b) < tol, f"{a} != {b} (tol {tol})"


def test_put_call_parity():
    S, K, T, r, sig = 24500, 24500, 7 / 365, 0.065, 0.14
    c = pricing.bs_price("CE", S, K, T, r, sig)
    p = pricing.bs_price("PE", S, K, T, r, sig)
    approx(c - p, S - K * math.exp(-r * T), 1e-6)


def test_iv_roundtrip():
    S, K, T, r = 24500, 24700, 5 / 365, 0.065
    for sig in (0.08, 0.14, 0.35, 0.9):
        px = pricing.bs_price("CE", S, K, T, r, sig)
        iv = pricing.implied_vol("CE", px, S, K, T, r)
        approx(iv, sig, 1e-4)
    px = pricing.bs_price("PE", S, K, T, r, 0.22)
    approx(pricing.implied_vol("PE", px, S, K, T, r), 0.22, 1e-4)


def _leg(kind, strike, side, price, lots=1, ls=75, iv=0.14):
    return po.Leg(kind=kind, strike=strike, expiry="2026-07-14", side=side,
                  lots=lots, lot_size=ls, entry_price=price, iv=iv)


def test_bull_call_spread():
    legs = [_leg("CE", 24450, +1, 164.0), _leg("CE", 24650, -1, 60.0)]
    ext = po.extremes(legs)
    approx(ext["max_loss"], -(164 - 60) * 75)          # net debit
    approx(ext["max_profit"], (200 - 104) * 75)        # width - debit
    bes = po.breakevens(legs)
    assert len(bes) == 1
    approx(bes[0], 24450 + 104, 0.01)


def test_short_straddle():
    legs = [_leg("CE", 24500, -1, 120.0), _leg("PE", 24500, -1, 110.0)]
    ext = po.extremes(legs)
    approx(ext["max_profit"], 230 * 75)
    assert ext["max_loss_unlimited"] is True
    bes = po.breakevens(legs)
    assert len(bes) == 2
    approx(bes[0], 24500 - 230, 0.01)
    approx(bes[1], 24500 + 230, 0.01)


def test_iron_condor_bounded():
    legs = [_leg("PE", 24000, +1, 20.0), _leg("PE", 24200, -1, 55.0),
            _leg("CE", 24800, -1, 60.0), _leg("CE", 25000, +1, 25.0)]
    ext = po.extremes(legs)
    credit = (55 - 20 + 60 - 25) * 75
    approx(ext["max_profit"], credit)
    approx(ext["max_loss"], credit - 200 * 75)
    assert not ext["max_profit_unlimited"] and not ext["max_loss_unlimited"]
    assert len(po.breakevens(legs)) == 2


def test_pop_and_ev_sane():
    legs = [_leg("CE", 24500, -1, 120.0), _leg("PE", 24500, -1, 110.0)]
    p = analytics.pop(legs, 24500, 0.14, 7 / 365, 0.065)
    # 230-pt breakevens vs a 475-pt 1SD move => ~37% POP; sanity band only
    assert 25 < p < 95, p
    ev = analytics.expected_value(legs, 24500, 0.14, 7 / 365, 0.065)
    assert -230 * 75 < ev < 230 * 75
    # long straddle POP must be the complement-ish (same breakevens)
    legs2 = [_leg("CE", 24500, +1, 120.0), _leg("PE", 24500, +1, 110.0)]
    p2 = analytics.pop(legs2, 24500, 0.14, 7 / 365, 0.065)
    approx(p + p2, 100.0, 0.2)


def test_t_plus_curve_between_intrinsic_and_entry():
    legs = [_leg("CE", 24500, +1, 120.0, iv=0.14)]
    grid = [24000, 24500, 25000]
    t0 = po.t_plus_curve(legs, grid, [3 / 365], 0.065)
    texp = po.expiry_curve(legs, grid)
    assert t0[1] > texp[1]          # ATM: time value keeps T+0 above expiry P&L
    assert t0[2] < texp[2] + 120 * 75


def test_net_premium_and_greeks():
    legs = [_leg("CE", 24500, -1, 120.0), _leg("PE", 24500, -1, 110.0)]
    approx(po.net_premium(legs), 230 * 75)
    g = po.net_greeks(legs, 24500, [7 / 365, 7 / 365], 0.065)
    assert g["theta"] > 0           # short straddle collects theta
    assert g["vega"] < 0
    assert abs(g["delta"]) < 15     # roughly delta-neutral ATM


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} engine tests passed.")
