"""Credential and app-settings loading.

Priority: process env vars > project .env. Secrets never live in code.
This project uses its own dedicated Zerodha account — it must never read
the legacy straddle-algo's config (a live algo runs on that account).
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = PROJECT_ROOT / "state"
FRONTEND_DIR = PROJECT_ROOT / "frontend"
ENV_FILE = PROJECT_ROOT / ".env"

RISK_FREE_RATE = float(os.environ.get("OPTIONS_LAB_RATE", "0.065"))
EXPIRY_CUTOFF = (15, 30)          # NSE options expire 15:30 IST
UNDERLYING = "NIFTY"
SPOT_SYMBOL = "NSE:NIFTY 50"
VIX_SYMBOL = "NSE:INDIA VIX"


def _load_env_file() -> dict:
    out = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load_credentials() -> dict:
    keys = ["KITE_API_KEY", "KITE_API_SECRET", "KITE_ACCESS_TOKEN",
            "KITE_USER_ID", "KITE_PASSWORD", "KITE_TOTP_SECRET"]
    envfile = _load_env_file()
    out = {}
    for k in keys:
        out[k] = os.environ.get(k) or envfile.get(k) or ""
    return out
