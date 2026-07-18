"""Strategy leg model and payoff engine.

A strategy is a list of legs. Expiry payoff is piecewise-linear with kinks
only at strikes, so max profit / max loss / breakevens are solved exactly
from the segment structure instead of a brute-force scan.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import pricing


@dataclass
class Leg:
    kind: str                 # CE | PE | FUT
    strike: float             # 0 for FUT
    expiry: str               # YYYY-MM-DD
    side: int                 # +1 buy, -1 sell
    lots: int
    lot_size: int
    entry_price: float
    iv: float | None = None   # decimal, per-leg (captures skew)
    tradingsymbol: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def qty(self) -> int:
        return self.lots * self.lot_size

    @property
    def signed_qty(self) -> int:
        return self.side * self.qty

    def value_at_expiry(self, S: float) -> float:
        if self.kind == "FUT":
            return S
        return pricing.intrinsic(self.kind, S, self.strike)

    def pnl_at_expiry(self, S: float) -> float:
        if self.kind == "FUT":
            return (S - self.entry_price) * self.signed_qty
        return (self.value_at_expiry(S) - self.entry_price) * self.signed_qty


def leg_from_dict(d: dict) -> Leg:
    return Leg(
        kind=str(d["kind"]).upper(),
        strike=float(d.get("strike") or 0),
        expiry=str(d.get("expiry") or ""),
        side=1 if int(d.get("side", 1)) >= 0 else -1,
        lots=max(1, int(d.get("lots", 1))),
        lot_size=int(d.get("lot_size", 75)),
        entry_price=float(d.get("entry_price") or 0),
        iv=(float(d["iv"]) if d.get("iv") not in (None, "", 0) else None),
        tradingsymbol=str(d.get("tradingsymbol") or ""),
    )


def payoff_at(legs: list[Leg], S: float) -> float:
    return sum(l.pnl_at_expiry(S) for l in legs)


def _slopes(legs: list[Leg]) -> tuple[float, float]:
    """Payoff slope (rupees per point) below all strikes and above all strikes."""
    lo = hi = 0.0
    for l in legs:
        if l.kind == "FUT":
            lo += l.signed_qty
            hi += l.signed_qty
        elif l.kind == "CE":
            hi += l.signed_qty          # calls kick in above strike
        elif l.kind == "PE":
            lo += -l.signed_qty         # long put gains as S falls
    return lo, hi


def strikes_of(legs: list[Leg]) -> list[float]:
    return sorted({l.strike for l in legs if l.kind in ("CE", "PE") and l.strike > 0})


def extremes(legs: list[Leg]) -> dict:
    """Exact max profit / max loss using kink evaluation + tail slopes."""
    ks = strikes_of(legs)
    lo_slope, hi_slope = _slopes(legs)
    pts = ks or [0.0]
    values = [payoff_at(legs, k) for k in pts]
    vals_at_zero = payoff_at(legs, 0.0)

    max_p, min_p = max(values + [vals_at_zero]), min(values + [vals_at_zero])
    # S=0 is a hard floor, so only the upside slope can make P&L unbounded
    max_unlimited = hi_slope > 1e-9
    min_unlimited = hi_slope < -1e-9
    return {
        "max_profit": None if max_unlimited else round(max_p, 2),
        "max_loss": None if min_unlimited else round(min_p, 2),
        "max_profit_unlimited": max_unlimited,
        "max_loss_unlimited": min_unlimited,
    }


def breakevens(legs: list[Leg], s_min: float = 0.0, s_max: float | None = None) -> list[float]:
    """Exact zero crossings of the piecewise-linear expiry payoff."""
    ks = strikes_of(legs)
    lo_slope, hi_slope = _slopes(legs)
    if s_max is None:
        top = (max(ks) if ks else 0.0) or 1.0
        s_max = top * 3.0
    nodes = [s_min] + [k for k in ks if s_min < k < s_max] + [s_max]

    bes: list[float] = []
    for a, b in zip(nodes[:-1], nodes[1:]):
        pa, pb = payoff_at(legs, a), payoff_at(legs, b)
        if abs(pa) < 1e-9 and abs(pb) < 1e-9:
            continue                      # flat-at-zero segment: boundary handled by neighbours
        if pa == 0.0:
            bes.append(a)
        if pa * pb < 0:
            bes.append(a + (b - a) * (-pa) / (pb - pa))
    if abs(payoff_at(legs, nodes[-1])) < 1e-9 and abs(hi_slope) < 1e-9:
        pass
    out = sorted({round(x, 2) for x in bes if x > 0})
    return out


def expiry_curve(legs: list[Leg], grid: list[float]) -> list[float]:
    return [round(payoff_at(legs, s), 2) for s in grid]


def t_plus_curve(legs: list[Leg], grid: list[float], tte_by_leg: list[float],
                 r: float, iv_shift: float = 0.0) -> list[float]:
    """Mark-to-model P&L curve at some evaluation time before expiry.

    tte_by_leg[i] is the remaining time (years) of legs[i] at the evaluation
    moment. Each leg is re-priced with its own IV (shifted by iv_shift, in
    IV points e.g. 0.02 = +2 vol points).
    """
    out = []
    for s in grid:
        total = 0.0
        for leg, tte in zip(legs, tte_by_leg):
            if leg.kind == "FUT":
                total += (s - leg.entry_price) * leg.signed_qty
                continue
            sigma = max(0.01, (leg.iv or 0.15) + iv_shift)
            px = pricing.bs_price(leg.kind, s, leg.strike, max(tte, 0.0), r, sigma)
            total += (px - leg.entry_price) * leg.signed_qty
        out.append(round(total, 2))
    return out


def net_greeks(legs: list[Leg], spot: float, tte_by_leg: list[float], r: float) -> dict:
    tot = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}
    for leg, tte in zip(legs, tte_by_leg):
        if leg.kind == "FUT":
            tot["delta"] += 1.0 * leg.signed_qty
            continue
        g = pricing.greeks(leg.kind, spot, leg.strike, max(tte, 0.0), r, leg.iv or 0.15)
        for k in tot:
            tot[k] += g[k] * leg.signed_qty
    return {k: round(v, 4) for k, v in tot.items()}


def net_premium(legs: list[Leg]) -> float:
    """Positive = net credit received, negative = net debit paid."""
    return round(-sum(l.entry_price * l.signed_qty for l in legs if l.kind != "FUT"), 2)


def time_value_split(legs: list[Leg], spot: float) -> dict:
    intr = tv = 0.0
    for l in legs:
        if l.kind == "FUT":
            continue
        iv_ = pricing.intrinsic(l.kind, spot, l.strike)
        intr += iv_ * l.signed_qty
        tv += (l.entry_price - iv_) * l.signed_qty
    return {"intrinsic": round(intr, 2), "time_value": round(tv, 2)}


def build_grid(spot: float, sd_points: float, strikes: list[float],
               n: int = 241, span_sd: float = 3.5) -> list[float]:
    """Price grid ±span_sd standard deviations around spot, strike-aware."""
    span = max(sd_points * span_sd, spot * 0.02)
    lo = max(1.0, spot - span)
    hi = spot + span
    if strikes:
        lo = min(lo, min(strikes) - spot * 0.005)
        hi = max(hi, max(strikes) + spot * 0.005)
        lo = max(1.0, lo)
    step = (hi - lo) / (n - 1)
    grid = [lo + i * step for i in range(n)]
    # make sure exact strikes & spot are on the grid so kinks render sharply
    grid.extend([k for k in strikes if lo < k < hi])
    grid.append(spot)
    return sorted(set(round(g, 2) for g in grid))
