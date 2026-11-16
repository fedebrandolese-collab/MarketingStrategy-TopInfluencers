"""Dashboard tab "System": is the daily pipeline alive, and what will it do next? A ten-second read for Federico or
the operator's team, no terminal needed.

Top to bottom: the verdict (green healthy, red needs attention with the problems, amber not scheduled yet with the one
command that turns it on), the stages (how often, last success, next due, provider, paid or free), this month's spend
against the caps in config/schedule.yaml, the last 10 runs with a mark per stage (a failed stage opens its error), data
freshness, recent alerts (data/logs/alerts.log), the newest backup (data/backups) and the commands that operate it.
Read-only: nothing here runs a stage or spends money.

Entry point: render_system_tab(conn). The numbers come from app/system_data.py.
"""
from __future__ import annotations

import datetime as dt
import re
import sqlite3

import pandas as pd
import streamlit as st

from wearable_wishlist import config, schedule
from app import system_data as sd

_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>#|~$])")
BOXES = {"ok": st.success, "error": st.error, "warning": st.warning}
LEVELS = {"error": ":red[**error**]", "warning": ":orange[**warning**]", "info": ":blue[**info**]"}
STAGE_COLUMNS = {
    "stage": "Stage", "every": "Every",
    "last_ok": st.column_config.TextColumn("Last ok", help="The stage's last successful run."),
    "next_due": st.column_config.TextColumn(
        "Next due", help="The daily run that does it next. On hold: its provider reached the monthly cap."),
    "provider": "Provider",
    "paid": st.column_config.TextColumn(
        "Paid or free", help="classify spends only on the anthropic LLM backend (free on claude_cli)."),
}
OPERATE = """\
| To | Run from the repository root | Cost |
|---|---|---|
| See health, what is due and this month's spend | `make status` | free |
| See what the next run would do | `.venv/bin/python -m wearable_wishlist.ops plan` | free |
| Run once now: the due stages, as the schedule would | `make ops-run` | **paid** |
| Turn the daily run on or off | `make schedule-on` · `make schedule-off` | free |
| Change the cadence or the monthly budgets | edit `config/schedule.yaml` (the next run reads it; a new `daily_at` also needs `make schedule` and `make schedule-on` again) | free |
| List the database backups, newest first | `ls -t data/backups/` | free |
"""


def md(text) -> str:
    """As in dashboard.py: stored text (errors, alerts) shown literally, no markdown, HTML or LaTeX ($)."""
    return "" if text is None else _MD_SPECIAL.sub(r"\\\1", " ".join(str(text).split()))


# ---------------------------------------------------------------- sections, top to bottom

def verdict_section(conn: sqlite3.Connection, sched: dict, now: dt.datetime) -> None:
    level, head, bullets = sd.verdict(conn, sd.health(conn, sched, now), sched, now)
    BOXES[level](head + ("\n\n" + "\n".join(f"- {md(b)}" for b in bullets) if bullets else ""))
    st.caption(f"The rule: healthy when the latest scheduled run finished within the last {sd.HEALTHY_HOURS} hours and "
               "none of its stages failed.")


def stages_section(conn: sqlite3.Connection, sched: dict, now: dt.datetime) -> None:
    st.markdown("##### Stages")
    st.dataframe(pd.DataFrame(sd.stage_rows(conn, sched, now)), hide_index=True, height="content",
                 column_config=STAGE_COLUMNS)
    st.caption(f"One run a day at {sched['daily_at']} does only the stages that are due. The cadence is in "
               "config/schedule.yaml.")


def spend_section(conn: sqlite3.Connection, sched: dict, now: dt.datetime) -> None:
    s = sd.spend(conn, sched, now)
    st.markdown("##### Spend this month")
    for p in s["providers"]:
        text = f"**{md(p['provider'])}** · {md(sd.usd(p['used']))}"
        if p["share"] is None:
            st.markdown(text + " · no cap")
            continue
        text += f" of {md(sd.usd(p['cap']))}" + (" · **cap reached**: its stages are skipped until the 1st"
                                                  if p["capped"] else "")
        st.progress(p["share"], text=text)
    c1, c2, _ = st.columns([1, 1, 2])
    c1.metric("Today", sd.usd(s["today"]))
    c2.metric("This month", sd.usd(s["month"]))
    st.caption("Since the 1st (UTC), every paid call: scheduled runs, runs by hand and account checks. Reaching a cap "
               "skips that provider's stages until the 1st. The caps are in config/schedule.yaml.")


def runs_section(conn: sqlite3.Connection, now: dt.datetime) -> None:
    st.markdown(f"##### Last {sd.RUNS_SHOWN} runs")
    runs = sd.recent_runs(conn)
    if not runs:
        st.caption("No run yet.")
        return
    st.table(pd.DataFrame([{"Run": r["run_id"], "Started": sd.when(r["started_at"], now), "Trigger": r["trigger"],
                            "Stages": r["stages"], "Took": r["duration"], "Cost": sd.usd(r["cost_usd"])} for r in runs]),
             hide_index=True)   # a table, not a grid: a long stage list wraps instead of being cut
    for r in runs:
        for s in r["failed"]:
            with st.expander(f"Run {r['run_id']} · {s['stage']} failed"):
                st.code(s["error"] or "no error recorded", language=None, wrap_lines=True)
    st.caption(f"✓ ok · ✗ failed · – skipped · … running. Local time ({dt.datetime.now().astimezone().tzname()}). "
               "Runs from before the per-stage record list their stages without marks.")


def freshness_section(conn: sqlite3.Connection, now: dt.datetime) -> None:
    st.markdown("##### Data freshness")
    lines = [f"- {r['what']}: " + (f"{sd.ago(r['at'], now)} ({sd.when(r['at'], now)})" if r["at"] else "none yet")
             for r in sd.freshness(conn)]
    on_list = st.session_state.get("list_now")   # set by the Wishlist tab when it ranks the list
    if on_list:
        lines.append(f"- On the list now: {on_list['text']} (run {on_list['run_id']}, with your Wishlist filters)")
    st.markdown("\n".join(lines))


def alerts_section(now: dt.datetime) -> None:
    alerts = sd.read_alerts()
    if alerts is None:
        return
    st.markdown("##### Recent alerts")
    if not alerts:
        st.caption("None: data/logs/alerts.log is empty.")
        return
    st.markdown("\n".join(f"- {sd.when(a['at'], now)} · {LEVELS.get(a['level']) or md(a['level']) or '?'} · "
                          f"{md(a['message'])}" for a in alerts))


def backups_section(now: dt.datetime) -> None:
    b = sd.newest_backup()
    if b is None:
        return
    st.markdown("##### Backups")
    st.markdown(f"Newest: `{b['name']}`, {sd.ago(b['at'], now)} ({sd.when(b['at'], now)}) · {b['count']} in "
                "`data/backups`")


def render_system_tab(conn: sqlite3.Connection) -> None:
    now, sched = dt.datetime.now(dt.timezone.utc), schedule.load()
    verdict_section(conn, sched, now)
    stages_section(conn, sched, now)
    spend_section(conn, sched, now)
    runs_section(conn, now)
    freshness_section(conn, now)
    alerts_section(now)
    backups_section(now)
    st.markdown("##### How to operate")
    st.markdown(OPERATE)
    st.caption("Password gate on: DASHBOARD_PASSWORD is set, so every new browser session asks for it."
               if config.env("DASHBOARD_PASSWORD") else
               "Password gate off: anyone who can open this address sees the dashboard. Set DASHBOARD_PASSWORD to ask "
               "for a password.")
