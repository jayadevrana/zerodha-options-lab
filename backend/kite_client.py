"""Zerodha Kite Connect client: session management, NIFTY option chain, margins.

Token strategy (in order):
  1. cached token in state/token.json (validated with profile())
  2. token from the legacy algo's config.local.yaml (its watchdog refreshes it)
  3. fresh headless login: kite.zerodha.com/api/login -> /api/twofa (TOTP)
     -> /connect/login redirect chase -> generate_session()
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
import threading
import time
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import requests
from kiteconnect import KiteConnect

from . import settings, smile as smile_mod, timecal
from .pricing import implied_vol_black

log = logging.getLogger("optionslab.kite")
IST = ZoneInfo("Asia/Kolkata")

TOKEN_FILE = settings.STATE_DIR / "token.json"
INSTRUMENTS_FILE = settings.STATE_DIR / "instruments_nifty.json"


class KiteError(Exception):
    pass


def now_ist() -> dt.datetime:
    return dt.datetime.now(IST)


def expiry_dt(expiry: str) -> dt.datetime:
    """Expiry date string -> 15:30 IST datetime."""
    d = dt.date.fromisoformat(expiry)
    h, m = settings.EXPIRY_CUTOFF
    return dt.datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


def tte_years(expiry: str, at: dt.datetime | None = None) -> float:
    """Calendar time to 15:30 IST on expiry day, in years (intraday-accurate)."""
    at = at or now_ist()
    seconds = (expiry_dt(expiry) - at).total_seconds()
    return max(seconds, 0.0) / (365.0 * 24 * 3600)


class HeadlessLogin:
    """Request-token fetch without a browser (TOTP 2FA)."""

    LOGIN_URL = "https://kite.zerodha.com/api/login"
    TWOFA_URL = "https://kite.zerodha.com/api/twofa"

    def __init__(self, creds: dict):
        self.creds = creds

    def fetch_access_token(self) -> str:
        import pyotp
        c = self.creds
        for k in ("KITE_API_KEY", "KITE_API_SECRET", "KITE_USER_ID",
                  "KITE_PASSWORD", "KITE_TOTP_SECRET"):
            if not c.get(k):
                raise KiteError(f"Missing credential {k} for auto-login")

        s = requests.Session()
        s.headers.update({"X-Kite-Version": "3",
                          "User-Agent": "Mozilla/5.0 (Macintosh) options-lab/1.0"})
        r = s.post(self.LOGIN_URL, data={
            "user_id": c["KITE_USER_ID"], "password": c["KITE_PASSWORD"]}, timeout=20)
        j = r.json()
        if j.get("status") != "success":
            raise KiteError(f"Kite login failed: {j.get('message')}")
        request_id = j["data"]["request_id"]

        r = s.post(self.TWOFA_URL, data={
            "user_id": c["KITE_USER_ID"], "request_id": request_id,
            "twofa_value": pyotp.TOTP(c["KITE_TOTP_SECRET"]).now(),
            "twofa_type": "totp"}, timeout=20)
        j = r.json()
        if j.get("status") != "success":
            raise KiteError(f"Kite TOTP step failed: {j.get('message')}")

        # authorized session -> connect flow redirects to the app's redirect
        # URL carrying request_token; that URL may be a dead localhost, so the
        # token is also mined from redirect history / connection errors.
        connect_url = (f"https://kite.zerodha.com/connect/login?"
                       f"api_key={c['KITE_API_KEY']}&v=3")
        request_token = ""
        try:
            r = s.get(connect_url, timeout=20, allow_redirects=True)
            for resp in list(r.history) + [r]:
                tok = self._token_from_url(resp.url) or self._token_from_url(
                    resp.headers.get("location", ""))
                if tok:
                    request_token = tok
        except requests.exceptions.ConnectionError as e:
            request_token = self._token_from_url(getattr(e.request, "url", "") or "")
        if not request_token:
            raise KiteError("Could not obtain request_token from connect flow")

        kite = KiteConnect(api_key=c["KITE_API_KEY"])
        data = kite.generate_session(request_token, api_secret=c["KITE_API_SECRET"])
        token = data.get("access_token") or ""
        if not token:
            raise KiteError("generate_session returned no access_token")
        log.info("Headless login OK for %s", data.get("user_id"))
        return token

    @staticmethod
    def _token_from_url(url: str) -> str:
        if not url or "request_token" not in url:
            return ""
        return (parse_qs(urlparse(url).query).get("request_token") or [""])[0]


class KiteClient:
    def __init__(self):
        self.creds = settings.load_credentials()
        self.kite = KiteConnect(api_key=self.creds["KITE_API_KEY"], timeout=15)
        self.connected = False
        self.user_id = ""
        self.login_error = ""
        self._lock = threading.Lock()
        self._instruments: list[dict] = []
        self._instruments_day = ""
        self._quote_cache: dict = {}

    # ---------- session ----------
    def _try_token(self, token: str) -> bool:
        if not token:
            return False
        try:
            self.kite.set_access_token(token)
            prof = self.kite.profile()
            self.user_id = prof.get("user_id", "")
            self.connected = True
            self.login_error = ""
            return True
        except Exception:
            return False

    def connect(self, force_fresh: bool = False) -> dict:
        with self._lock:
            if self.connected and not force_fresh:
                return self.session_info()

            candidates = []
            if not force_fresh:
                if TOKEN_FILE.exists():
                    try:
                        saved = json.loads(TOKEN_FILE.read_text())
                        if saved.get("date") == now_ist().date().isoformat():
                            candidates.append(saved.get("access_token", ""))
                    except Exception:
                        pass
                candidates.append(self.creds.get("KITE_ACCESS_TOKEN", ""))

            for tok in candidates:
                if self._try_token(tok):
                    self._save_token(tok)
                    log.info("Connected with existing token (user %s)", self.user_id)
                    return self.session_info()

            try:
                token = HeadlessLogin(self.creds).fetch_access_token()
            except Exception as e:
                self.login_error = str(e)
                log.error("Auto-login failed: %s", e)
                return self.session_info()
            if self._try_token(token):
                self._save_token(token)
            else:
                self.login_error = "Fresh token failed profile validation"
            return self.session_info()

    def _save_token(self, token: str):
        settings.STATE_DIR.mkdir(exist_ok=True)
        TOKEN_FILE.write_text(json.dumps(
            {"access_token": token, "date": now_ist().date().isoformat()}))

    def session_info(self) -> dict:
        return {"connected": self.connected, "user_id": self.user_id,
                "error": self.login_error,
                "login_url": self.kite.login_url() if self.creds["KITE_API_KEY"] else ""}

    def manual_request_token(self, request_token: str) -> dict:
        """Fallback: user pastes request_token from the Kite redirect URL."""
        data = self.kite.generate_session(
            request_token, api_secret=self.creds["KITE_API_SECRET"])
        tok = data.get("access_token", "")
        if self._try_token(tok):
            self._save_token(tok)
        return self.session_info()

    def _ensure(self):
        if not self.connected:
            self.connect()
        if not self.connected:
            raise KiteError(self.login_error or "Not connected to Kite")

    # ---------- instruments ----------
    def nifty_instruments(self) -> list[dict]:
        today = now_ist().date().isoformat()
        if self._instruments and self._instruments_day == today:
            return self._instruments
        if INSTRUMENTS_FILE.exists():
            try:
                blob = json.loads(INSTRUMENTS_FILE.read_text())
                if blob.get("date") == today:
                    self._instruments = blob["rows"]
                    self._instruments_day = today
                    return self._instruments
            except Exception:
                pass
        self._ensure()
        rows = self.kite.instruments("NFO")
        keep = []
        for r in rows:
            if r.get("name") != settings.UNDERLYING:
                continue
            if r.get("segment") not in ("NFO-OPT", "NFO-FUT"):
                continue
            exp = r.get("expiry")
            keep.append({
                "tradingsymbol": r["tradingsymbol"],
                "instrument_token": r["instrument_token"],
                "expiry": exp.isoformat() if hasattr(exp, "isoformat") else str(exp),
                "strike": float(r.get("strike") or 0),
                "type": r.get("instrument_type"),      # CE / PE / FUT
                "lot_size": int(r.get("lot_size") or 75),
            })
        settings.STATE_DIR.mkdir(exist_ok=True)
        INSTRUMENTS_FILE.write_text(json.dumps({"date": today, "rows": keep}))
        self._instruments = keep
        self._instruments_day = today
        log.info("Loaded %d NIFTY NFO instruments", len(keep))
        return keep

    def expiries(self) -> list[str]:
        today = now_ist().date().isoformat()
        exps = sorted({r["expiry"] for r in self.nifty_instruments()
                       if r["type"] in ("CE", "PE") and r["expiry"] >= today})
        return exps[:8]

    def lot_size(self) -> int:
        for r in self.nifty_instruments():
            if r["type"] in ("CE", "PE"):
                return r["lot_size"]
        return 75

    # ---------- quotes ----------
    def _quote(self, keys: list[str], ttl: float = 3.0) -> dict:
        self._ensure()
        now = time.time()
        missing = [k for k in keys
                   if k not in self._quote_cache or now - self._quote_cache[k][0] > ttl]
        for i in range(0, len(missing), 450):
            chunk = missing[i:i + 450]
            data = self.kite.quote(chunk)
            for k, v in data.items():
                self._quote_cache[k] = (now, v)
        return {k: self._quote_cache[k][1] for k in keys if k in self._quote_cache}

    def spot_and_vix(self) -> dict:
        q = self._quote([settings.SPOT_SYMBOL, settings.VIX_SYMBOL], ttl=2.0)
        spot = (q.get(settings.SPOT_SYMBOL) or {})
        vix = (q.get(settings.VIX_SYMBOL) or {})
        return {
            "spot": spot.get("last_price", 0.0),
            "spot_ohlc": spot.get("ohlc", {}),
            "vix": vix.get("last_price", 0.0),
            "vix_ohlc": vix.get("ohlc", {}),
            "ts": now_ist().isoformat(timespec="seconds"),
        }

    # ---------- option chain ----------
    def chain(self, expiry: str, width_points: float = 1500.0) -> dict:
        self._ensure()
        mkt = self.spot_and_vix()
        spot = mkt["spot"]
        rows = [r for r in self.nifty_instruments()
                if r["expiry"] == expiry and r["type"] in ("CE", "PE")]
        if not rows:
            raise KiteError(f"No NIFTY options for expiry {expiry}")
        rows = [r for r in rows if abs(r["strike"] - spot) <= width_points]
        keys = [f"NFO:{r['tradingsymbol']}" for r in rows]
        quotes = self._quote(keys)

        tau_c = timecal.tau_cal(expiry)
        tau_v = timecal.tau_var(expiry)
        r_rate = settings.RISK_FREE_RATE

        # pass 1: assemble cells with mids (IVs need the forward, so pass 2)
        by_strike: dict[float, dict] = {}
        for r_ in rows:
            q = quotes.get(f"NFO:{r_['tradingsymbol']}") or {}
            depth = q.get("depth") or {}
            bid = ((depth.get("buy") or [{}])[0].get("price") or 0.0)
            ask = ((depth.get("sell") or [{}])[0].get("price") or 0.0)
            ltp = q.get("last_price") or 0.0
            mid = 0.5 * (bid + ask) if bid > 0 and ask > 0 else ltp
            cell = {
                "tradingsymbol": r_["tradingsymbol"],
                "ltp": ltp, "bid": bid, "ask": ask, "mid": round(mid, 2),
                "oi": q.get("oi", 0) or 0,
                "volume": q.get("volume", 0) or 0,
                "iv": None,
                "lot_size": r_["lot_size"],
            }
            slot = by_strike.setdefault(r_["strike"], {"strike": r_["strike"]})
            slot[r_["type"]] = cell

        chain_rows = [by_strike[k] for k in sorted(by_strike)]

        # pass 2: implied forward -> Black-76 IVs on variance time
        fwd = smile_mod.implied_forward(chain_rows, spot, tau_c, r_rate)
        F = fwd["forward"]
        for rw in chain_rows:
            for side in ("CE", "PE"):
                cell = rw.get(side)
                if not cell or cell["mid"] <= 0:
                    continue
                iv = implied_vol_black(side, cell["mid"], F, rw["strike"],
                                       tau_v, r_rate, t_disc=tau_c)
                cell["iv"] = round(iv * 100, 2) if iv else None

        pts = smile_mod.strike_ivs(chain_rows, F, tau_v, tau_c, r_rate)
        fit = smile_mod.fit_smile(pts, tau_v) if tau_v > 0 else {
            "model": "none", "params": [], "rmse_volpts": None, "atm_iv": None}

        strikes = sorted(by_strike)
        atm = min(strikes, key=lambda k: abs(k - F)) if strikes else 0.0
        atm_iv = (round(fit["atm_iv"] * 100, 2)
                  if fit.get("atm_iv") else round(mkt["vix"], 2))

        return {
            "expiry": expiry, "spot": spot, "vix": mkt["vix"], "atm": atm,
            "atm_iv": atm_iv, "tau": tau_v, "tau_cal": tau_c,
            "dte": round(tau_c * 365.0, 2),
            "dte_var": round(tau_v * timecal.ANNUAL_WEIGHT_DAYS, 2),
            "lot_size": self.lot_size(),
            "rows": chain_rows,
            "smile": {"forward": F, "basis": fwd["basis"],
                      "fit": fit, "expiry": expiry},
            "ts": mkt["ts"],
        }

    # ---------- margins & orders ----------
    @staticmethod
    def _order_params(leg: dict, product: str = "NRML") -> dict:
        return {
            "exchange": "NFO",
            "tradingsymbol": leg["tradingsymbol"],
            "transaction_type": "BUY" if int(leg["side"]) > 0 else "SELL",
            "variety": "regular",
            "product": product,
            "order_type": "MARKET",
            "quantity": int(leg["lots"]) * int(leg["lot_size"]),
            "price": 0, "trigger_price": 0,
        }

    def basket_margin(self, legs: list[dict]) -> dict:
        self._ensure()
        params = [self._order_params(l) for l in legs if l.get("tradingsymbol")]
        if not params:
            return {"total": None}
        data = self.kite.basket_order_margins(params, consider_positions=False)
        final = (data or {}).get("final") or {}
        initial = (data or {}).get("initial") or {}
        return {
            "total": final.get("total"),
            "span": final.get("span"),
            "exposure": final.get("exposure"),
            "premium": final.get("option_premium"),
            "initial_total": initial.get("total"),
            "hedge_benefit": (round(initial.get("total", 0) - final.get("total", 0), 2)
                              if initial.get("total") and final.get("total") else None),
        }

    def available_cash(self) -> float | None:
        try:
            self._ensure()
            m = self.kite.margins(segment="equity") or {}
            avail = m.get("available") or {}
            return avail.get("live_balance") or avail.get("cash")
        except Exception:
            return None

    def execute_basket(self, legs: list[dict], product: str = "NRML") -> list[dict]:
        """Place market orders for all legs. SELL legs first (margin benefit)."""
        self._ensure()
        ordered = sorted(legs, key=lambda l: 0 if int(l["side"]) < 0 else 1)
        results = []
        for leg in ordered:
            p = self._order_params(leg, product)
            try:
                oid = self.kite.place_order(
                    variety="regular", exchange=p["exchange"],
                    tradingsymbol=p["tradingsymbol"],
                    transaction_type=p["transaction_type"],
                    quantity=p["quantity"], product=p["product"],
                    order_type=p["order_type"], validity="DAY")
                results.append({"tradingsymbol": p["tradingsymbol"],
                                "side": p["transaction_type"],
                                "order_id": oid, "status": "PLACED"})
            except Exception as e:
                results.append({"tradingsymbol": p["tradingsymbol"],
                                "side": p["transaction_type"],
                                "order_id": None, "status": f"REJECTED: {e}"})
        return results


CLIENT = KiteClient()
