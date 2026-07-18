# Zerodha Options Lab

Options analytics lab for Indian markets on Zerodha Kite: a FastAPI backend with Black-Scholes pricing, IV smile fitting, a payoff engine, strategy generator, trade-cost model and advisor, plus a web frontend and offline tests.

## Features

- **Black-Scholes pricing & Greeks** — theoretical value, delta, gamma, theta, vega for NIFTY options with a configurable risk-free rate.
- **IV smile fitting** — fits an implied-volatility smile across the chain and exposes the forward and fitted curve to downstream analytics.
- **Payoff engine** — pure, market-independent P&L / payoff computation from posted legs and spot, so it works when the market is closed or Kite is disconnected.
- **Probability & expectancy** — lognormal POP, standard deviation and expected-value estimates, optionally net of costs.
- **Strategy generator** — proposes multi-leg option structures from a view on the underlying.
- **Trade-cost model** — folds Zerodha brokerage, taxes and charges into POP/EV so numbers reflect real fills.
- **Advisor** — turns analytics into plain-language commentary on a proposed structure.
- **Web frontend** — a strategy-builder UI (HTML/CSS/JS) served directly by the backend.
- **Live Kite integration** — TOTP-based auto-login, market snapshot, and option-chain endpoints via KiteConnect.
- **Offline tests** — unit tests for the smile fitter, payoff engine and generator that run without any live connection.

## Stack

- **Backend:** Python 3.12, FastAPI, Uvicorn, NumPy, KiteConnect, pyotp, PyYAML
- **Frontend:** vanilla HTML / CSS / JavaScript
- **Tests:** pytest-style modules under `tests/`

## Getting started

```bash
# 1. configure credentials (dedicated Zerodha account)
cp .env.example .env
#   then fill in KITE_API_KEY / KITE_API_SECRET / KITE_USER_ID / etc.

# 2. install + run
pip install -r requirements.txt
uvicorn backend.app:app --host 127.0.0.1 --port 8420
#   or simply: ./run.sh
```

Then open http://127.0.0.1:8420/ for the strategy builder. The analytics endpoints
(`/api/analyze`, `/api/generate`, `/api/advise`) are pure and work offline from posted
legs + spot; the `/api/market` and `/api/chain` endpoints require a live Kite session.

Run the offline tests with:

```bash
python -m pytest tests/
```

## Notes

Trading automation is infrastructure, not financial advice. No profit guarantees. Test
in dry-run / paper before going live, and point the credentials at a dedicated account —
never one already running another live algo.

## Author

Built by [Jayadev Rana](https://jayadevrana.in) — @bluealgocapital · [YouTube](https://www.youtube.com/@jayadevrana3657) · [GitHub](https://github.com/jayadevrana)
