"""Claude verdicts through the locally installed Claude Code CLI.

Uses the user's subscription (`claude -p`, headless print mode) — no API key,
no per-token billing. Gracefully absent when the CLI isn't installed.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

CLAUDE_BIN = shutil.which("claude")


def _clean_env() -> dict:
    """Fresh env for the CLI. When this server is launched from inside a
    Claude Code session, inherited CLAUDE_*/ANTHROPIC_* vars redirect the
    nested CLI's auth and break subscription login — strip them. A long-lived
    CLAUDE_CODE_OAUTH_TOKEN from the project .env (created once with
    `claude setup-token`) is passed through so daily CLI-token expiry never
    breaks the advisor."""
    env = {}
    for k, v in os.environ.items():
        ku = k.upper()
        if ku.startswith(("CLAUDE", "ANTHROPIC")) or ku in ("BAGGAGE", "AI_AGENT"):
            continue
        env[k] = v
    from . import settings
    tok = settings._load_env_file().get("CLAUDE_CODE_OAUTH_TOKEN") \
        or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if tok:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = tok
    return env


def available() -> bool:
    return CLAUDE_BIN is not None


def advise(candidate: dict, market: dict) -> str:
    if not available():
        raise RuntimeError("claude CLI not found on PATH")
    prompt = f"""You are a senior NIFTY options risk officer. A quant generator proposed this weekly-expiry position. Give a tight verdict.

MARKET: {json.dumps(market)}
CANDIDATE: {json.dumps(candidate)}

Numbers you can trust: EV/POP are under a VRP-tilted market-implied density; CVaR95 is under the pure market density; costs included.

Reply in <=180 words, plain text, four sections:
VERDICT: take / skip / downsize, one sentence why.
KEY RISK: the single scenario that hurts most (be concrete: level + day).
INVALIDATION: what market change means exit or re-evaluate.
EXECUTION: order sequence, fill discipline, margin note."""
    out = subprocess.run(
        [CLAUDE_BIN, "-p", prompt, "--output-format", "text"],
        capture_output=True, text=True, timeout=180,
        stdin=subprocess.DEVNULL, env=_clean_env(), cwd=os.path.expanduser("~"))
    text = (out.stdout or "").strip()
    if not text:
        raise RuntimeError((out.stderr or "claude CLI returned nothing").strip()[:400])
    if "not logged in" in text.lower():
        raise RuntimeError(
            "Claude CLI session expired. Quick fix: open Terminal, run `claude`, "
            "let it refresh (or /login). Permanent fix: run `claude setup-token` "
            "once and add CLAUDE_CODE_OAUTH_TOKEN=<token> to options-lab/.env")
    return text
