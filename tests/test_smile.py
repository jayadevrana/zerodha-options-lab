"""Validation of the forward/Black-76/smile/density layer.

The decisive test: build a synthetic chain from a KNOWN smile, run the full
pipeline (forward inference -> strike IVs -> fit -> density), and check
model-free identities: density mass = 1, martingale E[e^k] = 1, and
E_Q[any payoff priced at model mids] = its forward price (so any strategy
entered at mids has EV ~ 0 net of premium).
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import payoff as po, smile, timecal
from backend.pricing import black_price


R = 0.065
TAU_V = 5.0 / timecal.ANNUAL_WEIGHT_DAYS      # ~5 weighted days
TAU_C = 7.0 / 365.0
SPOT = 24400.0
DIV_Y = 0.012                                  # known dividend yield
F_TRUE = SPOT * math.exp((R - DIV_Y) * TAU_C)


def true_iv(K: float) -> float:
    """A known smile: 10% ATM, put skew, smile curvature."""
    k = math.log(K / F_TRUE)
    return 0.10 - 0.35 * k + 3.0 * k * k


def synth_rows():
    rows = []
    for K in range(23600, 25300, 50):
        ivk = true_iv(K)
        ce = black_price("CE", F_TRUE, K, TAU_V, R, ivk, t_disc=TAU_C)
        pe = black_price("PE", F_TRUE, K, TAU_V, R, ivk, t_disc=TAU_C)
        rows.append({"strike": float(K),
                     "CE": {"mid": ce, "bid": ce * 0.99, "ask": ce * 1.01},
                     "PE": {"mid": pe, "bid": pe * 0.99, "ask": pe * 1.01}})
    return rows


ROWS = synth_rows()


def approx(a, b, tol):
    assert abs(a - b) < tol, f"{a} != {b} (tol {tol})"


def test_forward_inference_recovers_dividends():
    fwd = smile.implied_forward(ROWS, SPOT, TAU_C, R)
    approx(fwd["forward"], F_TRUE, 0.5)        # within half a point
    # naive spot*e^{rT} forward would be off by the dividend carry
    naive = SPOT * math.exp(R * TAU_C)
    assert abs(naive - F_TRUE) > 4.0, "test setup: dividend gap should be visible"


def test_same_strike_iv_unification():
    F = smile.implied_forward(ROWS, SPOT, TAU_C, R)["forward"]
    from backend.pricing import implied_vol_black
    for rw in ROWS[::4]:
        ce_iv = implied_vol_black("CE", rw["CE"]["mid"], F, rw["strike"], TAU_V, R, t_disc=TAU_C)
        pe_iv = implied_vol_black("PE", rw["PE"]["mid"], F, rw["strike"], TAU_V, R, t_disc=TAU_C)
        if ce_iv and pe_iv:
            approx(ce_iv, pe_iv, 5e-4)          # parity: same strike, same IV


def test_fit_recovers_smile():
    F = smile.implied_forward(ROWS, SPOT, TAU_C, R)["forward"]
    pts = smile.strike_ivs(ROWS, F, TAU_V, TAU_C, R)
    fit = smile.fit_smile(pts, TAU_V)
    assert fit["model"] in ("svi", "quad"), fit
    for K in (23800, 24200, 24400, 24800, 25100):
        k = math.log(K / F)
        approx(smile.smile_iv_at(fit, k), true_iv(K), 0.004)   # within 0.4 volpts


def test_density_identities():
    F = smile.implied_forward(ROWS, SPOT, TAU_C, R)["forward"]
    pts = smile.strike_ivs(ROWS, F, TAU_V, TAU_C, R)
    fit = smile.fit_smile(pts, TAU_V)
    dens = smile.density(fit)
    approx(dens["raw_mass"], 1.0, 0.02)                 # integrates to ~1 pre-normalization
    approx(dens["martingale_drift"], 0.0, 0.005)        # E[e^k] = 1


def test_ev_q_of_mid_priced_strategy_is_zero():
    """Iron condor priced at model mids: E_Q[payoff] must be ~0 + carry."""
    F = smile.implied_forward(ROWS, SPOT, TAU_C, R)["forward"]
    pts = smile.strike_ivs(ROWS, F, TAU_V, TAU_C, R)
    fit = smile.fit_smile(pts, TAU_V)
    dens = smile.density(fit)

    def mid(K, kind):
        for rw in ROWS:
            if rw["strike"] == K:
                return rw[kind]["mid"]
        raise KeyError(K)

    legs = [
        po.Leg("PE", 23900.0, "x", +1, 1, 65, mid(23900, "PE")),
        po.Leg("PE", 24150.0, "x", -1, 1, 65, mid(24150, "PE")),
        po.Leg("CE", 24700.0, "x", -1, 1, 65, mid(24700, "CE")),
        po.Leg("CE", 24950.0, "x", +1, 1, 65, mid(24950, "CE")),
    ]
    res = smile.integrate_payoff(dens, F, lambda s: po.payoff_at(legs, s))
    # premium ~ few hundred rupees, carry over a week ~ premium*r*tau ~ Rs 2
    assert abs(res["ev"]) < 60, res          # < 1 tick/leg of numeric error
    assert res["cvar95"] is not None and res["cvar95"] > 0


def test_variance_time_weekend():
    import datetime as dt
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
    fri_close = dt.datetime(2026, 7, 10, 15, 30, tzinfo=IST)   # Friday
    mon_open = dt.datetime(2026, 7, 13, 9, 15, tzinfo=IST)     # Monday
    w = timecal.weighted_days_between(fri_close, mon_open)
    # Sat + Sun at weekend weight, zero residual Friday, zero Monday pre-open
    approx(w, 2 * timecal.WEEKEND_WEIGHT, 1e-6)
    tue = "2026-07-14"
    assert timecal.tau_var(tue, fri_close) < timecal.tau_cal(tue, fri_close), \
        "variance time must run slower than calendar over a weekend"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} smile/density tests passed.")
