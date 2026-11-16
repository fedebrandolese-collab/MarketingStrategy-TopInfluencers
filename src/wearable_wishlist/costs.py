"""Cost ledger and budget guard. EVERY paid call goes: estimate -> guard -> call -> record.
Two caps per provider: per run (.env BUDGET_<PROVIDER>_USD) and per calendar month (config/schedule.yaml
budget.month_usd, lifted by BUDGET_IGNORE_MONTH=1 for a deliberate manual run)."""
from __future__ import annotations

import sqlite3

from . import config, db, schedule

# X API pay-per-usage list prices (USD per resource), from docs.x.com/x-api/getting-started/pricing (Sep 2026).
X_PRICES = {
    "post_read": 0.005,    # search, user posts, mentions: per post returned
    "user_read": 0.010,    # user lookup / user search: per user returned
    "follow_read": 0.010,  # followers / following: per user returned
    "owned_read": 0.001,   # your own followers/following with your own app+user token
    "counts": 0.005,       # recent counts: per request
}
# Anthropic API rough prices per 1M tokens (input, output) for cost logging only.
ANTHROPIC_PRICES = {"claude-sonnet-5-5": (3.0, 15.0), "claude-opus-5-5": (15.0, 75.0), "claude-haiku-4-5-20251001": (1.0, 5.0)}


class BudgetExceeded(RuntimeError):
    pass


def spent(conn: sqlite3.Connection, provider: str, run_id: int | None = None) -> float:
    if run_id is None:
        r = conn.execute("SELECT COALESCE(SUM(cost_usd),0) FROM costs WHERE provider=?", (provider,)).fetchone()
    else:
        r = conn.execute("SELECT COALESCE(SUM(cost_usd),0) FROM costs WHERE provider=? AND run_id=?", (provider, run_id)).fetchone()
    return float(r[0] or 0.0)


def guard(conn: sqlite3.Connection, provider: str, projected_usd: float, run_id: int | None = None) -> None:
    """Raise BudgetExceeded if this run's spend for `provider` plus `projected_usd` would exceed the per-run cap, or this
    month's spend (schedule.month_spend) plus `projected_usd` would exceed the provider's monthly cap."""
    cap = config.budget_usd(provider)
    already = spent(conn, provider, run_id)
    if already + projected_usd > cap:
        raise BudgetExceeded(
            f"{provider}: spent {fmt(already)} this run, call would add {fmt(projected_usd)}, cap is {fmt(cap)} "
            f"(raise BUDGET_{provider.upper()}_USD in .env to allow)")
    month_cap = schedule.load()["budget"]["month_usd"].get(provider)
    if month_cap is None or config.env("BUDGET_IGNORE_MONTH") == "1":
        return
    month = schedule.month_spend(conn).get(provider, 0.0)
    if month + projected_usd > month_cap:
        raise BudgetExceeded(
            f"{provider}: spent {fmt(month)} this month, call would add {fmt(projected_usd)}, monthly cap is "
            f"{fmt(month_cap)} (budget.month_usd in config/schedule.yaml; BUDGET_IGNORE_MONTH=1 lifts it for a manual run)")


def record(conn: sqlite3.Connection, provider: str, endpoint: str, units: int, unit_cost_usd: float,
           run_id: int | None = None, note: str = "") -> float:
    cost = round(units * unit_cost_usd, 6)
    conn.execute(
        "INSERT INTO costs (ts, run_id, provider, endpoint, units, unit_cost_usd, cost_usd, note) VALUES (?,?,?,?,?,?,?,?)",
        (db.utcnow(), run_id, provider, endpoint, units, unit_cost_usd, cost, note))
    conn.commit()
    return cost


def summary(conn: sqlite3.Connection) -> list[dict]:
    return db.rows(conn, "SELECT provider, endpoint, SUM(units) AS units, SUM(cost_usd) AS cost_usd, COUNT(*) AS calls "
                         "FROM costs GROUP BY provider, endpoint ORDER BY cost_usd DESC")


def fmt(usd: float) -> str:
    return f"${usd:,.4f}" if usd < 0.01 else f"${usd:,.2f}"
