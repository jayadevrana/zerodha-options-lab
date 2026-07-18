"""Generator sanity on a synthetic chain: constraints must hold, ranking
must be ordered, and every mid-priced candidate must satisfy the Q-measure
invariant EV_Q ~ -entry_costs (no phantom edge under the market's own density)."""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import generator, smile, timecal
from backend.pricing import black_price

R = 0.065
TAU_V = 5.0 / timecal.ANNUAL_WEIGHT_DAYS
TAU_C = 7.0 / 365.0
SPOT = 24400.0
F = SPOT * math.exp((R - 0.012) * TAU_C)


def true_iv(K):
    k = math.log(K / F)
    return 0.10 - 0.35 * k + 3.0 * k * k


def synth_chain():
    rows = []
    for K in range(23400, 25500, 50):
        ivk = true_iv(K)
        ce = black_price("CE", F, K, TAU_V, R, ivk, t_disc=TAU_C)
        pe = black_price("PE", F, K, TAU_V, R, ivk, t_disc=TAU_C)
        rows.append({"strike": float(K),
                     "CE": {"mid": round(ce, 2), "bid": round(ce * .99, 2),
                            "ask": round(ce * 1.01, 2), "iv": round(ivk * 100, 2),
                            "tradingsymbol": f"NIFTY{K}CE", "oi": 1000, "lot_size": 65},
                     "PE": {"mid": round(pe, 2), "bid": round(pe * .99, 2),
                            "ask": round(pe * 1.01, 2), "iv": round(ivk * 100, 2),
                            "tradingsymbol": f"NIFTY{K}PE", "oi": 1000, "lot_size": 65}})
    pts = smile.strike_ivs(
        [{"strike": r["strike"],
          "CE": {**r["CE"], "bid": r["CE"]["mid"] * .99, "ask": r["CE"]["mid"] * 1.01},
          "PE": {**r["PE"], "bid": r["PE"]["mid"] * .99, "ask": r["PE"]["mid"] * 1.01}}
         for r in rows], F, TAU_V, TAU_C, R)
    fit = smile.fit_smile(pts, TAU_V)
    return {"expiry": "2026-07-14", "spot": SPOT, "lot_size": 65, "rows": rows,
            "smile": {"forward": F, "basis": F - SPOT, "fit": fit}}


PARAMS = dict(capital=1_000_000, risk_budget=6_000, view_points=0.0,
              vrp_ratio=0.90, min_pop=0.0, min_rr=0.0, max_legs=6,
              lots_cap=20, band_points=800.0,
              widths=[50, 100, 150, 200, 300, 400],
              allow_naked=False, top_n=100, margin_utilization=0.95)


def test_generator_constraints_and_ranking():
    out = generator.score_all(synth_chain(), dict(PARAMS))
    cands = out["candidates"]
    assert len(cands) >= 50, f"too few candidates: {len(cands)}"
    scores = [c["score"] for c in cands]
    assert scores == sorted(scores, reverse=True), "ranking not ordered"
    assert cands[0]["rank"] == 1 and cands[-1]["rank"] == len(cands)
    for c in cands:
        assert not c["unlimited"], "naked leaked through allow_naked=False"
        assert len(c["legs"]) <= PARAMS["max_legs"]
        # sized to budget: gross loss within budget; net may exceed by costs only
        assert -c["max_loss"] <= PARAMS["risk_budget"] + c["entry_cost"] + 1, c
        assert c["margin_est"] <= PARAMS["capital"] * 0.95 + 1
        assert c["max_profit"] > 0
        # Q-invariant: EV under pure market density ~ -entry costs
        assert abs(c["ev_q"] + c["entry_cost"]) < 1500, \
            f"phantom Q-edge: {c['family']} ev_q={c['ev_q']} cost={c['entry_cost']}"


def test_view_tilt_changes_leaderboard():
    bull = generator.score_all(synth_chain(), {**PARAMS, "view_points": 200.0})
    bear = generator.score_all(synth_chain(), {**PARAMS, "view_points": -200.0})
    top_bull = bull["candidates"][0]
    top_bear = bear["candidates"][0]
    # a +200 view must not produce the same best structure as a -200 view
    assert top_bull["legs"] != top_bear["legs"], "view tilt had no effect"

    def delta_sign(c):
        s = 0.0
        for l in c["legs"]:
            direction = 1 if l["kind"] == "CE" else -1
            s += direction * l["side"] * l["lots"]
        return s
    # loose sanity: bull pick should not be strongly short-delta and vice versa
    assert delta_sign(top_bull) >= delta_sign(top_bear) - 1e-9


def test_vrp_neutral_kills_short_premium_edge():
    pure = generator.score_all(synth_chain(), {**PARAMS, "vrp_ratio": 1.0})
    # with no VRP and no view, nothing should show meaningfully positive EV
    best_ev = max(c["ev"] for c in pure["candidates"])
    assert best_ev < 1500, f"EV should be ~<=0 under pure Q, got {best_ev}"


if __name__ == "__main__":
    for name in ["test_generator_constraints_and_ranking",
                 "test_view_tilt_changes_leaderboard",
                 "test_vrp_neutral_kills_short_premium_edge"]:
        globals()[name]()
        print("PASS", name)
    print("\n3 generator tests passed.")
