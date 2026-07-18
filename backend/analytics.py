"""Probability and risk analytics on top of the payoff engine.

Terminal price model: risk-neutral lognormal —
ln(S_T) ~ N( ln(S) + (r - sigma^2/2) * tau,  sigma^2 * tau ).
POP/EV are model estimates driven entirely by the IV you feed in.
"""
from __future__ import annotations

import math

from . import payoff as po
from .payoff import Leg
from .pricing import norm_cdf


def lognormal_cdf(x: float, spot: float, sigma: float, tau: float, r: float,
                  forward: float | None = None) -> float:
    """P(S_T <= x). Terminal mean = implied forward when given (correct for
    dividend-paying indices), else spot*e^{r tau}."""
    if x <= 0:
        return 0.0
    if tau <= 0 or sigma <= 0:
        return 1.0 if x >= spot else 0.0
    F = forward or spot * math.exp(r * tau)
    mu = math.log(F) - 0.5 * sigma * sigma * tau
    sd = sigma * math.sqrt(tau)
    return norm_cdf((math.log(x) - mu) / sd)


def sd_bands(spot: float, sigma: float, tau: float) -> dict:
    pts = spot * sigma * math.sqrt(max(tau, 0.0))
    bands = {}
    for n in (1, 2, 3):
        bands[str(n)] = {
            "points": round(n * pts, 1),
            "pct": round(100.0 * n * pts / spot, 2),
            "low": round(spot - n * pts, 1),
            "high": round(spot + n * pts, 1),
        }
    return bands


def _profit_intervals(legs: list[Leg], spot: float) -> list[tuple[float, float]]:
    """Intervals of terminal price where expiry payoff > 0, from exact breakevens."""
    hi_bound = spot * 4.0
    bes = po.breakevens(legs, 0.0, hi_bound)
    edges = [0.0] + bes + [hi_bound]
    intervals = []
    for a, b in zip(edges[:-1], edges[1:]):
        if b - a < 1e-9:
            continue
        mid = 0.5 * (a + b)
        if po.payoff_at(legs, mid) > 0:
            intervals.append((a, b))
    # merge touching intervals
    merged: list[list[float]] = []
    for a, b in intervals:
        if merged and abs(merged[-1][1] - a) < 1e-6:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def pop(legs: list[Leg], spot: float, sigma: float, tau: float, r: float,
        forward: float | None = None) -> float:
    """Probability the strategy is profitable at expiry (percent)."""
    if not legs:
        return 0.0
    total = 0.0
    for a, b in _profit_intervals(legs, spot):
        total += (lognormal_cdf(b, spot, sigma, tau, r, forward)
                  - lognormal_cdf(a, spot, sigma, tau, r, forward))
    return round(100.0 * max(0.0, min(1.0, total)), 1)


def expected_value(legs: list[Leg], spot: float, sigma: float, tau: float, r: float,
                   n: int = 400, forward: float | None = None) -> float:
    """E[payoff at expiry] under the lognormal terminal density (numeric)."""
    if tau <= 0 or sigma <= 0:
        return round(po.payoff_at(legs, spot), 2)
    F = forward or spot * math.exp(r * tau)
    sd = sigma * math.sqrt(tau)
    mu = math.log(F) - 0.5 * sigma * sigma * tau
    # integrate over ±5 sd in log space with the trapezoid rule
    zs = [-5.0 + 10.0 * i / (n - 1) for i in range(n)]
    total = 0.0
    prev_f = None
    for i, z in enumerate(zs):
        s = math.exp(mu + sd * z)
        f = po.payoff_at(legs, s) * math.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)
        if prev_f is not None:
            total += 0.5 * (f + prev_f) * (zs[i] - zs[i - 1])
        prev_f = f
    return round(total, 2)


def reward_risk(ext: dict) -> float | None:
    mp, ml = ext.get("max_profit"), ext.get("max_loss")
    if mp is None or ml is None or ml >= 0:
        return None
    if abs(ml) < 1e-9:
        return None
    return round(mp / abs(ml), 2)
