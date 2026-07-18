"""Strategy generator: enumerate same-expiry NIFTY combos, score under a
physically-tilted implied density, rank top-N under capital/risk constraints.

Measure choices (deliberate):
  EV / POP   -> P-measure = market density with (a) VRP variance scaling
                w_P(k) = rho^2 * w_Q(k)  (rho = vrp_ratio, hist ~0.85-0.95)
                and (b) an Esscher mean-tilt for the user's directional view
                (drift set in index points to expiry).
  CVaR95     -> pure Q-measure (market-priced tails; conservative risk).

Under pure Q every mid-priced combo has EV ~ 0 by construction, so ranking
without a tilt is meaningless — the tilt IS the model of edge.

Vectorized scoring: instrument payoff matrix (M x G) built once per run;
a candidate's P&L curve is a signed sum of <= 8 rows. ~1e5 combos/sec.
"""
from __future__ import annotations

import math

import numpy as np

from . import costs as costs_mod, smile as smile_mod

MAX_CANDIDATES = 400_000          # enumeration safety valve


# ---------------------------------------------------------------- density
def tilted_density(fit: dict, vrp_ratio: float, view_points: float, F: float):
    """(ks, qP, qQ, dk): physical + risk-neutral densities on a shared grid."""
    dq = smile_mod.density(fit, n=601)
    ks = np.array(dq["ks"]); qQ = np.array(dq["q"]); dk = dq["dk"]

    if abs(vrp_ratio - 1.0) > 1e-9:
        fit_p = {"model": fit["model"], "tau_v": fit.get("tau_v"),
                 "params": _scale_params(fit, vrp_ratio ** 2)}
        qP = np.interp(ks, np.array(smile_mod.density(fit_p, n=601)["ks"]),
                       np.array(smile_mod.density(fit_p, n=601)["q"]),
                       left=0.0, right=0.0)
        m = float(np.sum(qP) * dk)
        qP = qP / m if m > 1e-12 else qQ.copy()
    else:
        qP = qQ.copy()

    # physical measure: zero expected drift on spot over a week unless viewed
    target_mean = 1.0 + view_points / F
    qP = _esscher_shift(ks, qP, dk, target_mean)
    return ks, qP, qQ, dk


def _scale_params(fit: dict, var_scale: float) -> list[float]:
    p = fit["params"]
    if fit["model"] == "svi":
        a, b, rho, m, s = p
        return [a * var_scale, b * var_scale, rho, m, s]
    if fit["model"] == "quad":
        return [x * var_scale for x in p]
    return [p[0] * var_scale] if p else p


def _esscher_shift(ks, q, dk, target_mean: float, iters: int = 60):
    """Exponential tilt q*e^{theta k} so that E[e^k] = target_mean."""
    if target_mean <= 0:
        return q
    lo, hi = -60.0, 60.0
    ek = np.exp(ks)

    def mean(theta):
        w = q * np.exp(theta * ks)
        z = np.sum(w) * dk
        return float(np.sum(w * ek) * dk / z) if z > 1e-300 else 1e9

    m0 = mean(0.0)
    if abs(m0 - target_mean) < 1e-9:
        return q
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        if mean(mid) < target_mean:
            lo = mid
        else:
            hi = mid
    theta = 0.5 * (lo + hi)
    w = q * np.exp(theta * ks)
    return w / (np.sum(w) * dk)


# ---------------------------------------------------------------- universe
def build_universe(chain: dict, band_points: float, otm_only: bool = True,
                   max_spread_pct: float = 6.0, min_oi: float = 0.0) -> list[dict]:
    """Tradeable instruments only. Deep-ITM strikes are dropped by default:
    their quotes go stale/crossed and they are synthetically identical to the
    liquid OTM side. Spread and OI filters kill phantom-arbitrage mids."""
    F = chain["smile"]["forward"]
    uni = []
    for rw in chain["rows"]:
        K = rw["strike"]
        if abs(K - F) > band_points:
            continue
        for kind in ("CE", "PE"):
            if otm_only:
                if kind == "CE" and K < F - 60:
                    continue
                if kind == "PE" and K > F + 60:
                    continue
            c = rw.get(kind)
            if not c or c.get("mid", 0) <= 0 or not c.get("tradingsymbol"):
                continue
            bid, ask, mid = c.get("bid", 0), c.get("ask", 0), c["mid"]
            if bid <= 0 or ask <= 0 or ask < bid:
                continue                                    # no market / crossed
            if (ask - bid) > max(mid * max_spread_pct / 100.0, 0.60):
                continue                                    # too wide to trust
            if min_oi > 0 and (c.get("oi", 0) or 0) < min_oi:
                continue
            uni.append({"kind": kind, "strike": K, "mid": mid,
                        "bid": bid, "ask": ask,
                        "iv": c.get("iv"), "tradingsymbol": c["tradingsymbol"],
                        "oi": c.get("oi", 0), "lot_size": c.get("lot_size", 65)})
    uni.sort(key=lambda u: (u["kind"], u["strike"]))
    return uni


def payoff_matrix(uni: list[dict], S: np.ndarray) -> np.ndarray:
    """Per-unit-qty P&L at expiry for each instrument across the S grid."""
    M = np.empty((len(uni), len(S)))
    for i, u in enumerate(uni):
        if u["kind"] == "CE":
            intr = np.maximum(S - u["strike"], 0.0)
        else:
            intr = np.maximum(u["strike"] - S, 0.0)
        M[i] = intr - u["mid"]
    return M


# ---------------------------------------------------------------- enumeration
def enumerate_structures(uni: list[dict], F: float, params: dict) -> list[dict]:
    """Each candidate: {'legs': [(idx, sign, ratio)], 'family': str}.
    ratio scales within the structure (for ratio spreads); lots applied later."""
    ce = [i for i, u in enumerate(uni) if u["kind"] == "CE"]
    pe = [i for i, u in enumerate(uni) if u["kind"] == "PE"]
    ks = lambda i: uni[i]["strike"]
    widths = params["widths"]
    max_legs = params["max_legs"]
    allow_naked = params["allow_naked"]
    out = []

    def K2i(pool, K):
        for i in pool:
            if abs(ks(i) - K) < 1e-9:
                return i
        return None

    # 1) singles
    for i in ce + pe:
        out.append({"legs": [(i, +1, 1)], "family": "Long " + uni[i]["kind"]})
        if allow_naked:
            out.append({"legs": [(i, -1, 1)], "family": "Short " + uni[i]["kind"]})

    # 2) verticals (both types, both directions, all widths)
    for pool, kind in ((ce, "CE"), (pe, "PE")):
        for i in pool:
            for w in widths:
                j = K2i(pool, ks(i) + w)
                if j is None:
                    continue
                out.append({"legs": [(i, +1, 1), (j, -1, 1)],
                            "family": f"{'Bull' if kind == 'CE' else 'Bear-hedge'} {kind} spread"})
                out.append({"legs": [(i, -1, 1), (j, +1, 1)],
                            "family": f"{'Bear' if kind == 'CE' else 'Bull'} {kind} credit spread"})

    # 3) straddles / strangles (long always; short only if naked allowed)
    for pi in pe:
        for ci in ce:
            gap_p, gap_c = F - ks(pi), ks(ci) - F
            if gap_p < -50 or gap_c < -50 or gap_p > 700 or gap_c > 700:
                continue
            name = "Straddle" if abs(ks(pi) - ks(ci)) < 1e-9 else "Strangle"
            out.append({"legs": [(pi, +1, 1), (ci, +1, 1)], "family": "Long " + name})
            if allow_naked:
                out.append({"legs": [(pi, -1, 1), (ci, -1, 1)], "family": "Short " + name})

    # 4) iron condors / iron flies (short body + long wings)
    if max_legs >= 4:
        for pi in pe:
            gp = F - ks(pi)
            if gp < -50 or gp > 600:
                continue
            for ci in ce:
                gc = ks(ci) - F
                if gc < -50 or gc > 600:
                    continue
                for wp in widths:
                    pw = K2i(pe, ks(pi) - wp)
                    if pw is None:
                        continue
                    for wc in widths:
                        cw = K2i(ce, ks(ci) + wc)
                        if cw is None:
                            continue
                        fam = ("Iron Fly" if abs(ks(pi) - ks(ci)) < 1e-9
                               else "Iron Condor")
                        out.append({"legs": [(pw, +1, 1), (pi, -1, 1),
                                             (ci, -1, 1), (cw, +1, 1)],
                                    "family": fam})
                        if len(out) > MAX_CANDIDATES:
                            return out

    # 5) butterflies (long body wings, short 2x middle) both types
    if max_legs >= 3:
        for pool, kind in ((ce, "CE"), (pe, "PE")):
            for i in pool:
                for w in widths:
                    lo, hi = K2i(pool, ks(i) - w), K2i(pool, ks(i) + w)
                    if lo is None or hi is None:
                        continue
                    out.append({"legs": [(lo, +1, 1), (i, -1, 2), (hi, +1, 1)],
                                "family": f"{kind} Butterfly"})

    # 6) broken-wing ratio (1 long near, 2 short far, 1 far-far long guard)
    if max_legs >= 3:
        for pool, kind, sgn in ((ce, "CE", +1), (pe, "PE", -1)):
            for i in pool:
                for w in widths:
                    j = K2i(pool, ks(i) + sgn * w)
                    if j is None:
                        continue
                    g = K2i(pool, ks(i) + sgn * (w + max(widths)))
                    legs = [(i, +1, 1), (j, -1, 2)]
                    fam = f"{kind} 1x2 Ratio"
                    if g is not None and max_legs >= 4:
                        legs.append((g, +1, 1))
                        fam += " (guarded)"
                    elif not allow_naked:
                        continue
                    out.append({"legs": legs, "family": fam})

    # 7) condor + extra short strangle overlay (6 legs) — richer combos
    if max_legs >= 6 and allow_naked:
        pass  # kept out by default: naked overlays explode risk; families 1-6 cover the space

    return out[:MAX_CANDIDATES]


# ---------------------------------------------------------------- scoring
def score_all(chain: dict, params: dict) -> dict:
    fit = chain["smile"]["fit"]
    F = chain["smile"]["forward"]
    if fit.get("model") not in ("svi", "quad", "flat"):
        raise ValueError("No usable smile fit — load the chain first")

    lot_size = chain.get("lot_size", 65)
    ks, qP, qQ, dk = tilted_density(fit, params["vrp_ratio"],
                                    params["view_points"], F)
    S = F * np.exp(ks)
    pP, pQ = qP * dk, qQ * dk

    uni = build_universe(chain, params["band_points"],
                         otm_only=params.get("otm_only", True),
                         max_spread_pct=params.get("max_spread_pct", 6.0),
                         min_oi=params.get("min_oi", 0.0))
    if len(uni) < 8:
        raise ValueError("Too few liquid strikes in band")
    P = payoff_matrix(uni, S)                      # (M, G) per unit qty

    # realistic fills: pay `slippage` of the half-spread on every leg
    slip = max(0.0, min(1.0, params.get("slippage", 0.35)))
    slip_cost_unit = [slip * max(u["ask"] - u["mid"], 0.0) +
                      0.0 for u in uni]            # buy side; sell computed below
    slip_cost_sell = [slip * max(u["mid"] - u["bid"], 0.0) for u in uni]

    cands = enumerate_structures(uni, F, params)
    budget = params["risk_budget"]
    lots_cap = params["lots_cap"]
    margin_cap = params["capital"] * params["margin_utilization"]

    results = []
    for c in cands:
        legs = c["legs"]
        if len(legs) > params["max_legs"]:
            continue
        pnl_unit = np.zeros(len(S))
        for idx, sign, ratio in legs:
            pnl_unit += sign * ratio * P[idx]
        pnl_lot = pnl_unit * lot_size              # one lot per ratio unit

        # exact bounded/unbounded check from tail slopes
        hi_slope = sum(sign * ratio for (i, sign, ratio) in legs
                       if uni[i]["kind"] == "CE")
        lo_slope = sum(-sign * ratio for (i, sign, ratio) in legs
                       if uni[i]["kind"] == "PE")
        unlimited_dn = lo_slope < 0                # loss grows as S -> 0
        unlimited_up = hi_slope < 0                # loss grows as S -> inf

        max_loss_1 = float(pnl_lot.min())          # on grid (±7 sd) — see naked note
        max_prof_1 = float(pnl_lot.max())
        if max_loss_1 >= 0 or max_prof_1 <= 0:
            continue                               # arb-looking / degenerate rows

        if (unlimited_dn or unlimited_up) and not params["allow_naked"]:
            continue

        # risk metric for sizing: bounded -> exact max loss; naked -> Q-CVaR99
        if unlimited_dn or unlimited_up:
            order = np.argsort(pnl_lot)
            cw = np.cumsum(pQ[order])
            tail = order[cw <= 0.01]
            risk_1 = -float(pnl_lot[tail].dot(pQ[tail]) / max(pQ[tail].sum(), 1e-12)) \
                if len(tail) else -max_loss_1
        else:
            risk_1 = -max_loss_1
        if risk_1 <= 0:
            continue

        lots = int(min(budget // risk_1, lots_cap))
        if lots < 1:
            continue

        # margin estimate: debit-only -> premium; short-containing -> hedged
        # SPAN floor per short lot (Kite basket verifies the top slice exactly)
        debit_1 = sum(sign * ratio * uni[i]["mid"] for (i, sign, ratio) in legs)
        short_lots = sum(ratio for (_, sign, ratio) in legs if sign < 0) * lots
        has_short = short_lots > 0
        if has_short:
            margin_est = max((1.15 * risk_1 + max(debit_1, 0) * lot_size) * lots,
                             28_000.0 * short_lots)
            if unlimited_dn or unlimited_up:
                margin_est = max(margin_est, 115_000.0 * lots)
        else:
            margin_est = max(debit_1, 0.0) * lot_size * lots
        if margin_est > margin_cap:
            lots = int(margin_cap // (margin_est / lots)) if margin_est > 0 else 0
            if lots < 1:
                continue
            margin_est = margin_est / max(lots, 1) * lots

        pnl = pnl_lot * lots
        n_orders = len(legs)
        exec_cost = sum((slip_cost_unit[i] if sign > 0 else slip_cost_sell[i])
                        * ratio for (i, sign, ratio) in legs) * lot_size * lots
        entry_cost = (20.0 * 1.18 * n_orders + exec_cost
                      + sum(abs(sign) * ratio * uni[i]["mid"] for (i, sign, ratio) in legs)
                      * lot_size * lots * (costs_mod.EXCH_TXN * 1.18
                                           + costs_mod.STT_SELL_PREMIUM * 0.5))
        pnl_net = pnl - entry_cost                 # expiry STT ~ small; folded below

        ev_p = float(pnl_net.dot(pP))
        pop_p = float(pP[pnl_net > 0].sum()) * 100.0
        ev_q = float(pnl_net.dot(pQ))

        order = np.argsort(pnl_net)
        cw = np.cumsum(pQ[order])
        k95 = np.searchsorted(cw, 0.05, side="right") + 1
        tail_idx = order[:max(k95, 1)]
        tail_p = pQ[tail_idx]
        cvar95 = -float(pnl_net[tail_idx].dot(tail_p) / max(tail_p.sum(), 1e-12))

        max_loss = float(pnl_net.min())
        max_prof = float(pnl_net.max())
        rr = max_prof / abs(max_loss) if max_loss < 0 else None
        if rr is not None and rr < params["min_rr"]:
            continue
        if pop_p < params["min_pop"]:
            continue

        # composite: tail-risk-adjusted EV, weighted by probability of profit
        alpha = params.get("pop_weight", 1.0)
        score = (ev_p / max(cvar95, budget * 0.05)) * (max(pop_p, 0.1) / 100.0) ** alpha
        results.append({
            "family": c["family"],
            "legs": [{"tradingsymbol": uni[i]["tradingsymbol"],
                      "kind": uni[i]["kind"], "strike": uni[i]["strike"],
                      "side": int(sign), "lots": int(ratio) * lots,
                      "lot_size": lot_size, "entry_price": uni[i]["mid"],
                      "iv": uni[i]["iv"], "expiry": chain["expiry"]}
                     for (i, sign, ratio) in legs],
            "lots": lots,
            "score": round(score, 4),
            "ev": round(ev_p, 0), "ev_q": round(ev_q, 0),
            "pop": round(pop_p, 1),
            "cvar95": round(cvar95, 0),
            "max_profit": round(max_prof, 0),
            "max_loss": round(max_loss, 0),
            "unlimited": bool(unlimited_dn or unlimited_up),
            "reward_risk": round(rr, 2) if rr else None,
            "margin_est": round(margin_est, 0),
            "entry_cost": round(entry_cost, 0),
        })

    results.sort(key=lambda r: r["score"], reverse=True)
    top = results[: params["top_n"]]
    for rank, r in enumerate(top, 1):
        r["rank"] = rank
    return {
        "candidates": top,
        "diagnostics": {
            "universe": len(uni), "enumerated": len(cands),
            "survivors": len(results),
            "forward": F, "vrp_ratio": params["vrp_ratio"],
            "view_points": params["view_points"],
            "fit": {"model": fit.get("model"), "rmse": fit.get("rmse_volpts")},
        },
    }
