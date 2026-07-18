"""Black-Scholes-Merton pricing, greeks, and implied-volatility solver.

Pure math — no market/data dependencies. All rates/vols are decimals
(0.065 = 6.5%), time is in years.
"""
from __future__ import annotations

import math

SQRT_2PI = math.sqrt(2.0 * math.pi)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT_2PI


def _d1_d2(S: float, K: float, T: float, r: float, sigma: float):
    st = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / st
    return d1, d1 - st


def intrinsic(kind: str, S: float, K: float) -> float:
    if kind == "CE":
        return max(S - K, 0.0)
    if kind == "PE":
        return max(K - S, 0.0)
    raise ValueError(f"intrinsic() only for CE/PE, got {kind}")


def bs_price(kind: str, S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Theoretical price of a European CE/PE. Falls back to intrinsic at T<=0."""
    if T <= 0 or sigma <= 0:
        return intrinsic(kind, S, K)
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    if kind == "CE":
        return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)
    return K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)


def greeks(kind: str, S: float, K: float, T: float, r: float, sigma: float) -> dict:
    """Per-unit greeks. theta is per calendar day, vega/rho per 1% move."""
    if T <= 0 or sigma <= 0:
        d = 0.0
        if kind == "CE" and S > K:
            d = 1.0
        elif kind == "PE" and S < K:
            d = -1.0
        return {"delta": d, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}

    d1, d2 = _d1_d2(S, K, T, r, sigma)
    pdf = norm_pdf(d1)
    disc = math.exp(-r * T)
    gamma = pdf / (S * sigma * math.sqrt(T))
    vega = S * pdf * math.sqrt(T) / 100.0

    if kind == "CE":
        delta = norm_cdf(d1)
        theta = (-S * pdf * sigma / (2 * math.sqrt(T)) - r * K * disc * norm_cdf(d2)) / 365.0
        rho = K * T * disc * norm_cdf(d2) / 100.0
    else:
        delta = norm_cdf(d1) - 1.0
        theta = (-S * pdf * sigma / (2 * math.sqrt(T)) + r * K * disc * norm_cdf(-d2)) / 365.0
        rho = -K * T * disc * norm_cdf(-d2) / 100.0

    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega, "rho": rho}


# ---------------------------------------------------------------- Black-76
# Index options must be priced off the *implied forward*, not spot+r with
# zero dividends. Using the wrong forward splits same-strike CE/PE IVs apart
# (put-call parity forces them equal) and tilts every downstream number.

def black_price(kind: str, F: float, K: float, T: float, r: float, sigma: float,
                t_disc: float | None = None) -> float:
    """Black-76: option on the forward F. T drives variance, t_disc discounting."""
    disc = math.exp(-r * (T if t_disc is None else t_disc))
    if T <= 0 or sigma <= 0:
        return disc * (max(F - K, 0.0) if kind == "CE" else max(K - F, 0.0))
    st = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * st * st) / st
    d2 = d1 - st
    if kind == "CE":
        return disc * (F * norm_cdf(d1) - K * norm_cdf(d2))
    return disc * (K * norm_cdf(-d2) - F * norm_cdf(-d1))


def black_greeks(kind: str, F: float, K: float, T: float, r: float, sigma: float,
                 spot: float | None = None) -> dict:
    """Greeks under Black-76. delta/gamma are w.r.t. spot when spot is given
    (dF/dS = F/S for a carry-linked forward); theta per weighted trading day
    is handled by the caller's clock. vega per 1 vol point."""
    S = spot or F
    dF_dS = F / S
    if T <= 0 or sigma <= 0:
        d = 1.0 if (kind == "CE" and F > K) else (-1.0 if (kind == "PE" and F < K) else 0.0)
        return {"delta": d * dF_dS, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}
    disc = math.exp(-r * T)
    st = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * st * st) / st
    d2 = d1 - st
    pdf = norm_pdf(d1)
    delta_F = disc * (norm_cdf(d1) if kind == "CE" else norm_cdf(d1) - 1.0)
    gamma_F = disc * pdf / (F * st)
    vega = disc * F * pdf * math.sqrt(T) / 100.0
    theta = (-disc * F * pdf * sigma / (2 * math.sqrt(T))
             - (r * K * disc * norm_cdf(d2) if kind == "CE"
                else -r * K * disc * norm_cdf(-d2))) / 365.0
    rho = (K * T * disc * norm_cdf(d2) if kind == "CE"
           else -K * T * disc * norm_cdf(-d2)) / 100.0
    return {"delta": delta_F * dF_dS, "gamma": gamma_F * dF_dS * dF_dS,
            "theta": theta, "vega": vega, "rho": rho}


def implied_vol_black(kind: str, price: float, F: float, K: float, T: float,
                      r: float, t_disc: float | None = None) -> float | None:
    """IV from a market price under Black-76 (bisection on undiscounted value)."""
    if T <= 0 or price <= 0 or F <= 0 or K <= 0:
        return None
    disc = math.exp(-r * (T if t_disc is None else t_disc))
    target = price / disc                    # undiscounted forward value
    floor = max(F - K, 0.0) if kind == "CE" else max(K - F, 0.0)
    ceil = F if kind == "CE" else K
    if target <= floor + 1e-9 or target >= ceil - 1e-9:
        return None

    def undisc(sig):
        st = sig * math.sqrt(T)
        d1 = (math.log(F / K) + 0.5 * st * st) / st
        d2 = d1 - st
        if kind == "CE":
            return F * norm_cdf(d1) - K * norm_cdf(d2)
        return K * norm_cdf(-d2) - F * norm_cdf(-d1)

    a, b = 1e-4, 5.0
    fa, fb = undisc(a) - target, undisc(b) - target
    if fa * fb > 0:
        return None
    for _ in range(80):
        m = 0.5 * (a + b)
        fm = undisc(m) - target
        if abs(fm) < 1e-8 or (b - a) < 1e-7:
            return m
        if fa * fm <= 0:
            b, fb = m, fm
        else:
            a, fa = m, fm
    return 0.5 * (a + b)


def implied_vol(kind: str, price: float, S: float, K: float, T: float, r: float,
                lo: float = 0.005, hi: float = 5.0) -> float | None:
    """Back out IV from a market price. Newton-Raphson, bisection fallback.

    Returns None when the price is outside no-arbitrage bounds (below
    intrinsic/discounted floor or above the underlying) or T<=0.
    """
    if T <= 0 or price <= 0 or S <= 0 or K <= 0:
        return None
    disc_K = K * math.exp(-r * T)
    floor = max(S - disc_K, 0.0) if kind == "CE" else max(disc_K - S, 0.0)
    ceil = S if kind == "CE" else disc_K
    if price <= floor + 1e-9 or price >= ceil - 1e-9:
        return None

    # Newton from a Brenner-Subrahmanyam style seed
    sigma = max(0.05, math.sqrt(2.0 * math.pi / T) * price / S)
    for _ in range(50):
        theo = bs_price(kind, S, K, T, r, sigma)
        diff = theo - price
        if abs(diff) < 1e-6:
            return max(sigma, 1e-4)
        d1, _ = _d1_d2(S, K, T, r, sigma)
        vega = S * norm_pdf(d1) * math.sqrt(T)
        if vega < 1e-10:
            break
        step = diff / vega
        sigma -= step
        if sigma <= lo or sigma >= hi:
            break
        if abs(step) < 1e-8:
            return max(sigma, 1e-4)

    # bisection fallback — price is monotone in sigma
    a, b = lo, hi
    fa = bs_price(kind, S, K, T, r, a) - price
    fb = bs_price(kind, S, K, T, r, b) - price
    if fa * fb > 0:
        return None
    for _ in range(100):
        m = 0.5 * (a + b)
        fm = bs_price(kind, S, K, T, r, m) - price
        if abs(fm) < 1e-6 or (b - a) < 1e-6:
            return max(m, 1e-4)
        if fa * fm <= 0:
            b, fb = m, fm
        else:
            a, fa = m, fm
    return max(0.5 * (a + b), 1e-4)
