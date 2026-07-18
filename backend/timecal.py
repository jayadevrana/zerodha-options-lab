"""Variance-time clock for Indian index options.

Implied volatility should accrue on *trading* time, not calendar time: a
weekly option loses far less value over Sat+Sun than calendar Black-Scholes
claims. We weight trading days 1.0 and non-trading days WEEKEND_WEIGHT, and
measure intraday time as the fraction of the NSE session remaining.

tau_var  -> use wherever sigma*sqrt(T) appears (pricing, density, POP)
tau_cal  -> use only for discounting e^{-rT} (negligible at weekly horizon)
"""
from __future__ import annotations

import datetime as dt
import json
from zoneinfo import ZoneInfo

from . import settings

IST = ZoneInfo("Asia/Kolkata")

SESSION_OPEN = dt.time(9, 15)
SESSION_CLOSE = dt.time(15, 30)
SESSION_SECONDS = (15 * 3600 + 30 * 60) - (9 * 3600 + 15 * 60)   # 6h15m

WEEKEND_WEIGHT = 0.30          # empirical weekend variance ratio for NIFTY
HOLIDAY_FILE = settings.STATE_DIR / "holidays.json"
DEFAULT_HOLIDAYS = ["2026-01-26", "2026-05-01", "2026-10-02", "2026-12-25"]

# effective weighted days per year: 252 trading + 113 non-trading * weight
ANNUAL_WEIGHT_DAYS = 252.0 + (365.0 - 252.0) * WEEKEND_WEIGHT


def _holidays() -> set[str]:
    if HOLIDAY_FILE.exists():
        try:
            return set(json.loads(HOLIDAY_FILE.read_text()))
        except Exception:
            pass
    return set(DEFAULT_HOLIDAYS)


def is_trading_day(d: dt.date) -> bool:
    return d.weekday() < 5 and d.isoformat() not in _holidays()


def _day_weight(d: dt.date) -> float:
    return 1.0 if is_trading_day(d) else WEEKEND_WEIGHT


def _session_fraction_remaining(t: dt.datetime) -> float:
    """Fraction of the day's variance still ahead at wall-clock t (same day)."""
    tt = t.timetz()
    if tt.replace(tzinfo=None) <= SESSION_OPEN:
        return 1.0
    if tt.replace(tzinfo=None) >= SESSION_CLOSE:
        return 0.0
    open_dt = t.replace(hour=9, minute=15, second=0, microsecond=0)
    return 1.0 - (t - open_dt).total_seconds() / SESSION_SECONDS


def weighted_days_between(now: dt.datetime, expiry_close: dt.datetime) -> float:
    """Sum of day weights from `now` to expiry 15:30, intraday-aware."""
    if now >= expiry_close:
        return 0.0
    total = 0.0
    d = now.date()
    while d <= expiry_close.date():
        w = _day_weight(d)
        start_frac = _session_fraction_remaining(now) if d == now.date() else 1.0
        end_frac = (_session_fraction_remaining(expiry_close)
                    if d == expiry_close.date() else 0.0)
        total += w * max(start_frac - end_frac, 0.0)
        d += dt.timedelta(days=1)
    return total


def tau_var(expiry: str, at: dt.datetime | None = None) -> float:
    """Variance time (years) to expiry-day 15:30 IST."""
    at = at or dt.datetime.now(IST)
    d = dt.date.fromisoformat(expiry)
    close = dt.datetime(d.year, d.month, d.day, 15, 30, tzinfo=IST)
    return weighted_days_between(at, close) / ANNUAL_WEIGHT_DAYS


def tau_cal(expiry: str, at: dt.datetime | None = None) -> float:
    """Plain calendar time (years) to expiry 15:30 IST — for discounting."""
    at = at or dt.datetime.now(IST)
    d = dt.date.fromisoformat(expiry)
    close = dt.datetime(d.year, d.month, d.day, 15, 30, tzinfo=IST)
    return max((close - at).total_seconds(), 0.0) / (365.0 * 24 * 3600)
