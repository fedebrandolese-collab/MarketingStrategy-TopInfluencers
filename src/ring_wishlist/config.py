"""Configuration and constants. Import this instead of reading os.environ directly.
Values that come from .env are functions (not constants) so tests can monkeypatch the environment."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
FIXTURES_DIR = DATA_DIR / "fixtures"
EXPORTS_DIR = DATA_DIR / "exports"
CACHE_DIR = DATA_DIR / "cache"
CONFIG_DIR = ROOT / "config"
QUERIES_FILE = CONFIG_DIR / "queries.yaml"


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def db_path() -> Path:
    return Path(env("DB_PATH", str(DATA_DIR / "wishlist.db")))


def dry_run() -> bool:
    """DRY_RUN=1 -> no network, no spend: adapters serve data/fixtures, LLM returns canned output."""
    return env("DRY_RUN", "0") == "1"


def source_name() -> str:
    """Which X adapter to use: 'x_api' | 'monid' | 'fixture'. Overridden to 'fixture' by DRY_RUN."""
    return "fixture" if dry_run() else env("SOURCE", "x_api")


MAP_URL = env("MAP_URL", "https://network.ri.fo")


def map_passphrase() -> str | None:
    return env("MAP_PASSPHRASE")


def carlo_handle() -> str | None:
    h = env("CARLO_X_HANDLE")
    return h.lstrip("@").lower() if h else None


def x_bearer_token() -> str | None:
    return env("X_BEARER_TOKEN")


def monid_api_key() -> str | None:
    return env("MONID_API_KEY")


def llm_backend() -> str:
    """'claude_cli' (Claude Code CLI, no key) | 'anthropic' (needs ANTHROPIC_API_KEY) | 'fixture'."""
    return "fixture" if dry_run() else env("LLM_BACKEND", "claude_cli")


def llm_model() -> str | None:
    return env("LLM_MODEL")


def budget_usd(provider: str) -> float:
    """Per-run spending cap for 'x_api' | 'monid' | 'anthropic'. Missing -> 10 USD.
    BUDGET_X_USD is accepted as an alias of BUDGET_X_API_USD."""
    names = [f"BUDGET_{provider.upper()}_USD"]
    if provider == "x_api":
        names.append("BUDGET_X_USD")
    for n in names:
        v = env(n)
        if v:
            return float(v)
    return 10.0


# ---- domain constants ----
COMPETITORS = [
    "Sandbar", "Oasis Devices", "4ff", "Ultrahuman", "Oura",
    "Humane", "Rabbit", "Limitless", "Plaud", "Omi", "Bee", "Friend", "Sesame", "Wispr Flow",
]
CATEGORIES = ("x_voice", "media", "vc", "founder_angel", "other")
SECTIONS = {  # dashboard / export section label per category
    "x_voice": "X voices", "media": "Media", "vc": "VCs", "founder_angel": "Founders & angels", "other": "Other",
}
STATUSES = ("candidate", "approved", "intro_asked", "ring_sent", "wearing", "posted", "rejected")
PATH_KINDS = ("direct", "via", "x_direct", "open_dm", "none")
EVIDENCE_KINDS = ("post", "bio", "profile_signal", "investment", "review", "article", "job", "map", "event")
SUPPORTS = ("accept", "wear", "love", "impact", "reach", "competitor", "us", "authenticity", "affinity")
EVENT_KINDS = ("fund_raised", "ring_unboxing", "joined_competitor", "left_competitor",
               "posted_about_product", "device_review", "other")
