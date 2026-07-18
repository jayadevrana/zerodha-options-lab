"""Options Lab — FastAPI backend.

Serves the strategy-builder frontend and the analysis API. The analytics
endpoints are pure (work from posted legs + spot) so the payoff engine also
functions when the market is closed or Kite is disconnected.
"""
from __future__ import annotations

import datetime as dt
import logging
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import (advisor, analytics, costs as costs_mod, generator,
               payoff as po, settings, smile as smile_mod, timecal)
from .kite_client import CLIENT, KiteError, now_ist

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("optionslab.app")
IST = ZoneInfo("Asia/Kolkata")

app = FastAPI(title="Options Lab", version="0.1")


# ---------------------------------------------------------------- models
class LegIn(BaseModel):
    kind: str                    # CE | PE | FUT
    strike: float = 0
    expiry: str
    side: int                    # +1 buy / -1 sell
    lots: int = 1
    lot_size: int = 75
    entry_price: float
    iv: float | None = None      # decimal (0.14) or percent (14) — normalized below
    tradingsymbol: str = ""


class AnalyzeIn(BaseModel):
    legs: list[LegIn]
    spot: float
    ref_iv: float | None = None          # percent; drives lognormal POP/SD/EV
    eval_date: str | None = None         # ISO datetime for the T+x curve
    spot_shift: float = 0.0              # not used server-side yet (slider is client-side)
    iv_shift: float = 0.0                # vol points, e.g. 2 = +2 IV points
    include_margin: bool = False
    include_costs: bool = True           # fold Zerodha costs into POP/EV
    smile: dict | None = None            # chain response's smile block (fit + forward)


class ExecuteIn(BaseModel):
    legs: list[LegIn]
    product: str = "NRML"
    confirm: str = Field(default="")


def _normalize_iv(v: float | None) -> float | None:
    if v is None:
        return None
    return v / 100.0 if v > 1.5 else v


def _to_legs(legs_in: list[LegIn]) -> list[po.Leg]:
    legs = []
    for l in legs_in:
        d = l.model_dump()
        d["iv"] = _normalize_iv(d.get("iv"))
        legs.append(po.leg_from_dict(d))
    if not legs:
        raise HTTPException(400, "No legs supplied")
    return legs


# ---------------------------------------------------------------- session
@app.get("/api/session")
def session():
    info = CLIENT.session_info()
    info["server_time"] = now_ist().isoformat(timespec="seconds")
    return info


@app.post("/api/login")
def login(body: dict | None = None):
    body = body or {}
    if body.get("request_token"):
        try:
            return CLIENT.manual_request_token(body["request_token"])
        except Exception as e:
            raise HTTPException(400, f"request_token exchange failed: {e}")
    return CLIENT.connect(force_fresh=bool(body.get("force")))


@app.get("/api/login_url")
def login_url():
    """Kite login URL for the one-time manual authorization."""
    return {"login_url": CLIENT.session_info().get("login_url", "")}


@app.get("/kite/redirect")
def kite_redirect(request_token: str = "", status: str = "", action: str = ""):
    """OAuth redirect target. Kite sends request_token here after the user
    authorizes the app in a browser; we exchange it and store today's token."""
    from fastapi.responses import HTMLResponse
    if not request_token:
        return HTMLResponse("<h3>No request_token in redirect.</h3>", status_code=400)
    try:
        info = CLIENT.manual_request_token(request_token)
    except Exception as e:
        return HTMLResponse(f"<h3>Token exchange failed:</h3><pre>{e}</pre>", status_code=400)
    ok = info.get("connected")
    body = (f"<h2 style='font-family:sans-serif'>{'✅ Connected as ' + info['user_id'] if ok else '❌ Failed'}</h2>"
            "<p style='font-family:sans-serif'>You can close this tab and return to Options Lab.</p>"
            "<script>setTimeout(()=>window.close(),1500)</script>")
    return HTMLResponse(body)


# ---------------------------------------------------------------- market data
@app.get("/api/market")
def market():
    try:
        mkt = CLIENT.spot_and_vix()
        return {**mkt, "expiries": CLIENT.expiries(),
                "lot_size": CLIENT.lot_size(),
                "cash": CLIENT.available_cash()}
    except KiteError as e:
        raise HTTPException(503, str(e))


@app.get("/api/chain")
def chain(expiry: str, width: float = 1500.0):
    try:
        return CLIENT.chain(expiry, width_points=width)
    except KiteError as e:
        raise HTTPException(503, str(e))


# ---------------------------------------------------------------- analytics
@app.post("/api/analyze")
def analyze(body: AnalyzeIn):
    legs = _to_legs(body.legs)
    spot = body.spot
    r = settings.RISK_FREE_RATE

    now = now_ist()
    # variance time drives every sigma*sqrt(T); calendar only discounts
    tau_now = [timecal.tau_var(l.expiry, now) for l in legs]
    tau_max = max(tau_now) if tau_now else 0.0

    ref_iv = _normalize_iv(body.ref_iv)
    if not ref_iv:
        leg_ivs = [l.iv for l in legs if l.iv]
        ref_iv = sum(leg_ivs) / len(leg_ivs) if leg_ivs else 0.14

    # forward for the terminal distribution (from smile block when supplied)
    smile_in = body.smile or {}
    forward = float(smile_in.get("forward") or 0) or None

    sd1_pts = spot * ref_iv * (tau_max ** 0.5) if tau_max > 0 else spot * 0.005
    grid = po.build_grid(spot, sd1_pts, po.strikes_of(legs))

    ext = po.extremes(legs)
    bes = po.breakevens(legs, 0.0, spot * 4.0)

    # evaluation moment for the "target" (pre-expiry) curve
    eval_at = now
    if body.eval_date:
        try:
            eval_at = dt.datetime.fromisoformat(body.eval_date)
            if eval_at.tzinfo is None:
                eval_at = eval_at.replace(tzinfo=IST)
        except ValueError:
            raise HTTPException(400, f"Bad eval_date: {body.eval_date}")
    tau_eval = [timecal.tau_var(l.expiry, eval_at) for l in legs]

    iv_shift = body.iv_shift / 100.0 if abs(body.iv_shift) > 0 else 0.0
    t0 = po.t_plus_curve(legs, grid, tau_eval, r, iv_shift)

    legs_dicts = [l.model_dump() for l in body.legs]
    entry_c = costs_mod.entry_costs(legs_dicts)

    def payoff_net(s):
        return (po.payoff_at(legs, s) - entry_c
                - costs_mod.expiry_costs_at(legs_dicts, s))

    out = {
        "grid": grid,
        "expiry_pnl": po.expiry_curve(legs, grid),
        "t0_pnl": t0,
        "breakevens": bes,
        **ext,
        "reward_risk": analytics.reward_risk(ext),
        "pop": analytics.pop(legs, spot, ref_iv, tau_max, r, forward),
        "expected_value": analytics.expected_value(legs, spot, ref_iv, tau_max, r,
                                                   forward=forward),
        "sd_bands": analytics.sd_bands(spot, ref_iv, tau_max),
        "greeks": po.net_greeks(legs, spot, tau_now, r),
        "net_premium": po.net_premium(legs),
        "time_value": po.time_value_split(legs, spot),
        "ref_iv_pct": round(ref_iv * 100, 2),
        "dte": round(tau_max * timecal.ANNUAL_WEIGHT_DAYS, 2),
        "multi_expiry": len({l.expiry for l in legs}) > 1,
        "costs": {"entry": entry_c,
                  "expiry_at_spot": round(costs_mod.expiry_costs_at(legs_dicts, spot), 2)},
        "forward": forward,
    }

    # smile-implied metrics: POP/EV/CVaR under the market's own density
    fit = (smile_in.get("fit") or {})
    same_expiry = len({l.expiry for l in legs}) == 1
    if forward and fit.get("model") in ("svi", "quad", "flat") and same_expiry \
            and smile_in.get("expiry") == legs[0].expiry and tau_max > 0:
        try:
            dens = smile_mod.density(fit)
            gross = smile_mod.integrate_payoff(dens, forward,
                                               lambda s: po.payoff_at(legs, s))
            net = smile_mod.integrate_payoff(dens, forward, payoff_net)
            out["smile_metrics"] = {
                "model": fit.get("model"), "rmse_volpts": fit.get("rmse_volpts"),
                "martingale_drift": dens["martingale_drift"],
                "gross": gross,
                "net": net if body.include_costs else None,
            }
        except Exception as e:
            out["smile_metrics"] = {"error": str(e)}

    if body.include_margin:
        try:
            out["margin"] = CLIENT.basket_margin(legs_dicts)
        except Exception as e:
            out["margin"] = {"total": None, "error": str(e)}
    return out


@app.post("/api/margin")
def margin(body: AnalyzeIn):
    try:
        return CLIENT.basket_margin([l.model_dump() for l in body.legs])
    except Exception as e:
        raise HTTPException(503, str(e))


# ---------------------------------------------------------------- generator
class GenerateIn(BaseModel):
    expiry: str
    capital: float = 1_000_000            # account size (Rs)
    risk_budget: float = 6_000            # max acceptable loss (Rs)
    view_points: float = 0.0              # expected NIFTY drift to expiry (pts)
    vrp_ratio: float = 0.90               # realized/implied vol prior (1.0 = pure market)
    min_pop: float = 0.0                  # percent
    min_rr: float = 0.0                   # min reward/risk
    max_legs: int = 6                     # 1..8
    lots_cap: int = 20
    band_points: float = 800.0            # strike search band around forward
    widths: list[float] = [50, 100, 150, 200, 300, 400]
    allow_naked: bool = False
    top_n: int = 100
    exact_margin_top: int = 15            # verify top-K margins via Kite basket API
    otm_only: bool = True                 # drop deep-ITM strikes (stale quotes)
    max_spread_pct: float = 6.0           # reject instruments wider than this
    min_oi: float = 0.0                   # optional OI floor
    slippage: float = 0.35                # fraction of half-spread paid per fill
    pop_weight: float = 1.0               # score *= (POP)^w — raise for safer picks


@app.post("/api/generate")
def generate(body: GenerateIn):
    try:
        chain_data = CLIENT.chain(body.expiry, width_points=body.band_points + 300)
    except KiteError as e:
        raise HTTPException(503, str(e))
    params = body.model_dump()
    params["max_legs"] = max(1, min(8, params["max_legs"]))
    params["margin_utilization"] = 0.95
    try:
        out = generator.score_all(chain_data, params)
    except ValueError as e:
        raise HTTPException(400, str(e))

    # exact SPAN margin for the head of the list (rate-limit friendly),
    # then drop anything whose true margin busts the capital cap and re-rank
    margin_cap = body.capital * 0.95
    kept = []
    checked = 0
    for cand in out["candidates"]:
        if checked < max(0, body.exact_margin_top):
            try:
                m = CLIENT.basket_margin(cand["legs"])
                if m.get("total"):
                    cand["margin_exact"] = round(m["total"], 0)
                checked += 1
                if cand["margin_exact"] > margin_cap:
                    continue
            except Exception:
                checked = body.exact_margin_top    # stop calling on failure
        kept.append(cand)
    for rank, cand in enumerate(kept, 1):
        cand["rank"] = rank
    out["candidates"] = kept
    out["spot"] = chain_data["spot"]
    out["atm_iv"] = chain_data["atm_iv"]
    out["smile"] = chain_data["smile"]
    out["claude_available"] = advisor.available()
    return out


@app.post("/api/advise")
def advise(body: dict):
    cand = body.get("candidate")
    if not cand:
        raise HTTPException(400, "candidate missing")
    try:
        text = advisor.advise(cand, body.get("market") or {})
    except Exception as e:
        raise HTTPException(503, f"Claude advisor failed: {e}")
    return {"advice": text}


# ---------------------------------------------------------------- execution
@app.post("/api/execute")
def execute(body: ExecuteIn):
    if body.confirm != "EXECUTE":
        raise HTTPException(400, 'Confirmation missing: send confirm="EXECUTE"')
    missing = [l.tradingsymbol or f"{l.strike}{l.kind}" for l in body.legs
               if not l.tradingsymbol]
    if missing:
        raise HTTPException(400, f"Legs without tradingsymbol: {missing}")
    try:
        results = CLIENT.execute_basket(
            [l.model_dump() for l in body.legs], product=body.product)
    except KiteError as e:
        raise HTTPException(503, str(e))
    return {"orders": results}


# ---------------------------------------------------------------- static UI
app.mount("/static", StaticFiles(directory=str(settings.FRONTEND_DIR)), name="static")


@app.get("/")
def index():
    return FileResponse(str(settings.FRONTEND_DIR / "index.html"))
