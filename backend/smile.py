"""Volatility smile fitting and the market-implied terminal density.

Pipeline (per expiry):
  1. Implied forward from put-call parity: F = K + e^{r tau}(C - P),
     median across the strikes nearest the spot (kills microstructure noise).
  2. One IV per strike, taken from the OTM side (puts below F, calls above —
     the liquid, information-carrying side), solved with Black-76 on F.
  3. Fit total variance w(k) = sigma(k)^2 * tau over log-moneyness k=ln(K/F):
     raw SVI  w(k) = a + b( rho(k-m) + sqrt((k-m)^2 + s^2) ),
     via Nelder-Mead with a butterfly-arbitrage penalty; weighted quadratic
     fallback when SVI is unstable; flat ATM variance as last resort.
  4. Risk-neutral density from the smile (Gatheral):
       g(k)  = (1 - k w'/(2w))^2 - (w'^2/4)(1/w + 1/4) + w''/2
       q(k)  = g(k)/sqrt(2 pi w) * exp(-d_minus(k)^2 / 2),
       d_minus = -k/sqrt(w) - sqrt(w)/2
     q(k) is the density of k = ln(S_T/F); S_T = F e^k. g(k) >= 0 is exactly
     the no-butterfly-arbitrage condition, so the fit penalty keeps q valid.

Correctness invariant used by tests and the live validator:
  under q, E[payoff of any strategy entered at mid prices] ~ 0.
"""
from __future__ import annotations

import math

from .pricing import implied_vol_black, norm_cdf


# ---------------------------------------------------------------- forward
def implied_forward(rows: list[dict], spot: float, tau_cal: float, r: float) -> dict:
    """rows: chain rows [{strike, CE:{mid,...}, PE:{mid,...}}, ...]."""
    cands = []
    usable = [rw for rw in rows
              if rw.get("CE", {}).get("mid", 0) > 0 and rw.get("PE", {}).get("mid", 0) > 0]
    usable.sort(key=lambda rw: abs(rw["strike"] - spot))
    for rw in usable[:5]:
        F = rw["strike"] + math.exp(r * tau_cal) * (rw["CE"]["mid"] - rw["PE"]["mid"])
        cands.append(F)
    if not cands:
        return {"forward": spot, "n_strikes": 0, "basis": 0.0}
    cands.sort()
    F = cands[len(cands) // 2]
    if not (0.9 * spot < F < 1.1 * spot):      # sanity clamp on bad quotes
        F = spot
    return {"forward": round(F, 2), "n_strikes": len(cands),
            "basis": round(F - spot, 2)}


# ---------------------------------------------------------------- strike IVs
def strike_ivs(rows: list[dict], F: float, tau_v: float, tau_c: float,
               r: float) -> list[dict]:
    """One (k, iv, weight) point per strike from the OTM side."""
    pts = []
    for rw in rows:
        K = rw["strike"]
        k = math.log(K / F)
        blend = abs(k) < 0.002                 # near-ATM: average both sides
        sides = (["PE", "CE"] if blend else (["PE"] if K < F else ["CE"]))
        ivs, spr = [], []
        for side in sides:
            cell = rw.get(side) or {}
            mid, bid, ask = cell.get("mid", 0), cell.get("bid", 0), cell.get("ask", 0)
            if mid <= 0:
                continue
            iv = implied_vol_black(side, mid, F, K, tau_v, r, t_disc=tau_c)
            if iv:
                ivs.append(iv)
                spr.append((ask - bid) / mid if (ask > bid > 0) else 0.25)
        if not ivs:
            continue
        iv = sum(ivs) / len(ivs)
        spread = max(min(sum(spr) / len(spr), 1.0), 0.01)
        pts.append({"strike": K, "k": k, "iv": iv, "w_fit": 1.0 / spread})
    return pts


# ---------------------------------------------------------------- SVI machinery
def _svi_w(k: float, p: list[float]) -> float:
    a, b, rho, m, s = p
    return a + b * (rho * (k - m) + math.sqrt((k - m) ** 2 + s * s))


def _svi_w1(k: float, p: list[float]) -> float:
    a, b, rho, m, s = p
    R = math.sqrt((k - m) ** 2 + s * s)
    return b * (rho + (k - m) / R)


def _svi_w2(k: float, p: list[float]) -> float:
    a, b, rho, m, s = p
    R = math.sqrt((k - m) ** 2 + s * s)
    return b * s * s / (R ** 3)


def _g_fn(k: float, w, w1, w2) -> float:
    """Gatheral's butterfly-arbitrage function; >= 0 means a valid density."""
    wk = max(w(k), 1e-12)
    a1 = 1.0 - k * w1(k) / (2.0 * wk)
    return a1 * a1 - (w1(k) ** 2 / 4.0) * (1.0 / wk + 0.25) + w2(k) / 2.0


def _nelder_mead(f, x0: list[float], steps: list[float], iters: int = 500):
    n = len(x0)
    simplex = [x0[:]] + [[x0[j] + (steps[j] if j == i else 0.0) for j in range(n)]
                         for i in range(n)]
    fx = [f(x) for x in simplex]
    for _ in range(iters):
        order = sorted(range(n + 1), key=lambda i: fx[i])
        simplex = [simplex[i] for i in order]
        fx = [fx[i] for i in order]
        if abs(fx[-1] - fx[0]) < 1e-12:
            break
        cen = [sum(x[j] for x in simplex[:-1]) / n for j in range(n)]
        xr = [cen[j] + (cen[j] - simplex[-1][j]) for j in range(n)]
        fr = f(xr)
        if fr < fx[0]:
            xe = [cen[j] + 2.0 * (cen[j] - simplex[-1][j]) for j in range(n)]
            fe = f(xe)
            simplex[-1], fx[-1] = (xe, fe) if fe < fr else (xr, fr)
        elif fr < fx[-2]:
            simplex[-1], fx[-1] = xr, fr
        else:
            xc = [cen[j] + 0.5 * (simplex[-1][j] - cen[j]) for j in range(n)]
            fc = f(xc)
            if fc < fx[-1]:
                simplex[-1], fx[-1] = xc, fc
            else:
                for i in range(1, n + 1):
                    simplex[i] = [simplex[0][j] + 0.5 * (simplex[i][j] - simplex[0][j])
                                  for j in range(n)]
                    fx[i] = f(simplex[i])
    best = min(range(n + 1), key=lambda i: fx[i])
    return simplex[best], fx[best]


def fit_smile(pts: list[dict], tau_v: float) -> dict:
    """Fit w(k); returns {'model', 'params', 'rmse_volpts', 'atm_iv'}."""
    ks = [p["k"] for p in pts]
    ws = [p["iv"] ** 2 * tau_v for p in pts]
    wt = [p["w_fit"] for p in pts]
    if not pts:
        return {"model": "none", "params": [], "rmse_volpts": None, "atm_iv": None}

    w_atm = min(zip(ks, ws), key=lambda t: abs(t[0]))[1]
    atm_iv = math.sqrt(max(w_atm, 1e-12) / tau_v)
    if len(pts) < 6:
        return {"model": "flat", "params": [w_atm], "rmse_volpts": None,
                "atm_iv": atm_iv, "tau_v": tau_v}

    kmin, kmax = min(ks), max(ks)
    pen_grid = [kmin + (kmax - kmin) * i / 24.0 for i in range(25)]

    def objective(p):
        a, b, rho, m, s = p
        if b < 0 or s <= 1e-6 or abs(rho) >= 0.999:
            return 1e18
        if a + b * s * math.sqrt(1 - rho * rho) < 0:      # w(k) > 0 everywhere
            return 1e18
        sse = sum(wgt * (_svi_w(k, p) - w) ** 2 for k, w, wgt in zip(ks, ws, wt))
        pen = sum(max(0.0, -_g_fn(k, lambda x: _svi_w(x, p),
                                  lambda x: _svi_w1(x, p),
                                  lambda x: _svi_w2(x, p))) ** 2
                  for k in pen_grid)
        return sse + 1e4 * w_atm * w_atm * pen

    span = max(ws) - min(ws) + 1e-10
    seeds = [
        [w_atm * 0.8, span / (kmax - kmin + 1e-9), -0.5, 0.0, 0.05],
        [w_atm * 0.5, 2.0 * w_atm / (abs(kmin) + 1e-6), -0.7, 0.01, 0.02],
        [w_atm, span, 0.0, 0.0, 0.1],
    ]
    best_p, best_f = None, 1e18
    for s0 in seeds:
        p, fval = _nelder_mead(objective, s0,
                               [abs(x) * 0.3 + 1e-4 for x in s0])
        if fval < best_f:
            best_p, best_f = p, fval

    def rmse(model_w):
        tot = sum((math.sqrt(max(model_w(k), 1e-12) / tau_v) - iv) ** 2
                  for k, iv in zip(ks, [p["iv"] for p in pts]))
        return math.sqrt(tot / len(ks)) * 100.0

    svi_ok = best_p is not None and best_f < 1e17
    if svi_ok:
        svi_rmse = rmse(lambda k: _svi_w(k, best_p))
        gmin = min(_g_fn(k, lambda x: _svi_w(x, best_p),
                         lambda x: _svi_w1(x, best_p),
                         lambda x: _svi_w2(x, best_p)) for k in pen_grid)
        if svi_rmse < 1.0 and gmin > -1e-8:
            return {"model": "svi", "params": [round(x, 8) for x in best_p],
                    "rmse_volpts": round(svi_rmse, 3), "atm_iv": atm_iv,
                    "tau_v": tau_v}

    # -------- weighted quadratic fallback: w = c0 + c1 k + c2 k^2, c2 >= 0
    S0 = sum(wt); S1 = sum(w * k for w, k in zip(wt, ks))
    S2 = sum(w * k * k for w, k in zip(wt, ks))
    S3 = sum(w * k ** 3 for w, k in zip(wt, ks))
    S4 = sum(w * k ** 4 for w, k in zip(wt, ks))
    T0 = sum(w * y for w, y in zip(wt, ws))
    T1 = sum(w * y * k for w, y, k in zip(wt, ws, ks))
    T2 = sum(w * y * k * k for w, y, k in zip(wt, ws, ks))
    det = (S0 * (S2 * S4 - S3 * S3) - S1 * (S1 * S4 - S2 * S3)
           + S2 * (S1 * S3 - S2 * S2))
    if abs(det) > 1e-18:
        c0 = (T0 * (S2 * S4 - S3 * S3) - S1 * (T1 * S4 - T2 * S3)
              + S2 * (T1 * S3 - T2 * S2)) / det
        c1 = (S0 * (T1 * S4 - T2 * S3) - T0 * (S1 * S4 - S2 * S3)
              + S2 * (S1 * T2 - S2 * T1)) / det
        c2 = (S0 * (S2 * T2 - S3 * T1) - S1 * (S1 * T2 - S3 * T0)
              + T0 * (S1 * S3 - S2 * S2)) / det
        c2 = max(c2, 0.0)
        quad = [c0, c1, c2]
        q_rmse = rmse(lambda k: quad[0] + quad[1] * k + quad[2] * k * k)
        if q_rmse < 2.0 and quad[0] > 0:
            return {"model": "quad", "params": [round(x, 8) for x in quad],
                    "rmse_volpts": round(q_rmse, 3), "atm_iv": atm_iv,
                    "tau_v": tau_v}

    return {"model": "flat", "params": [w_atm], "rmse_volpts": None,
            "atm_iv": atm_iv, "tau_v": tau_v}


# ---------------------------------------------------------------- density
def _w_funcs(fit: dict):
    model, p = fit["model"], fit["params"]
    if model == "svi":
        return (lambda k: _svi_w(k, p), lambda k: _svi_w1(k, p),
                lambda k: _svi_w2(k, p))
    if model == "quad":
        c0, c1, c2 = p
        return (lambda k: max(c0 + c1 * k + c2 * k * k, 1e-12),
                lambda k: c1 + 2 * c2 * k, lambda k: 2 * c2)
    w0 = p[0] if p else 1e-4
    return (lambda k: w0, lambda k: 0.0, lambda k: 0.0)


def density(fit: dict, n: int = 601) -> dict:
    """Discretized risk-neutral density of k=ln(S_T/F). Renormalized to 1;
    the raw mass and martingale drift are reported as fit diagnostics."""
    w, w1, w2 = _w_funcs(fit)
    w_atm = max(w(0.0), 1e-12)
    half = 7.0 * math.sqrt(w_atm)
    half = min(max(half, 0.02), 1.0)
    ks = [-half + 2 * half * i / (n - 1) for i in range(n)]
    dk = ks[1] - ks[0]
    q = []
    for k in ks:
        wk = max(w(k), 1e-12)
        d_minus = -k / math.sqrt(wk) - math.sqrt(wk) / 2.0
        g = max(_g_fn(k, w, w1, w2), 0.0)
        q.append(g / math.sqrt(2 * math.pi * wk) * math.exp(-0.5 * d_minus ** 2))
    mass = sum(q) * dk
    if mass <= 1e-9:
        raise ValueError("degenerate density")
    q = [x / mass for x in q]
    drift = sum(qi * math.exp(k) for qi, k in zip(q, ks)) * dk - 1.0
    return {"ks": ks, "q": q, "dk": dk,
            "raw_mass": round(mass, 6), "martingale_drift": round(drift, 6)}


def integrate_payoff(dens: dict, F: float, payoff_fn) -> dict:
    """E[payoff], P(payoff>0) and CVaR95 of loss under the implied density."""
    ks, q, dk = dens["ks"], dens["q"], dens["dk"]
    vals = [payoff_fn(F * math.exp(k)) for k in ks]
    probs = [qi * dk for qi in q]
    ev = sum(v * p for v, p in zip(vals, probs))
    pop = sum(p for v, p in zip(vals, probs) if v > 0)

    # CVaR95 of the loss distribution L = -payoff
    pairs = sorted(zip(vals, probs), key=lambda t: t[0])     # worst first
    acc, tail_ev, alpha = 0.0, 0.0, 0.05
    var95 = None
    for v, p in pairs:
        if acc + p >= alpha and var95 is None:
            take = alpha - acc
            tail_ev += v * take
            var95 = v
            acc += p
            break
        tail_ev += v * p
        acc += p
    cvar95 = -(tail_ev / alpha) if var95 is not None else None
    return {"ev": round(ev, 2), "pop": round(100.0 * min(max(pop, 0.0), 1.0), 1),
            "cvar95": round(cvar95, 2) if cvar95 is not None else None,
            "var95": round(-var95, 2) if var95 is not None else None}


def smile_iv_at(fit: dict, k: float) -> float:
    w, _, _ = _w_funcs(fit)
    return math.sqrt(max(w(k), 1e-12) / max(fit.get("tau_v", 1e-9), 1e-9))
