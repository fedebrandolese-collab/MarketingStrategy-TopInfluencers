"""The dashboard's System tab: app/system_data.py on temp DBs with fixture runs, run_stages, costs, alerts and backups
(the health rule, the verdict, stages, spend, runs, freshness), the page through AppTest, and the password gate.
Fixture times are built in local time, so day words like 'today 06:02' hold in any timezone."""
import datetime as dt
import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ring_wishlist import db, schedule  # noqa: E402
from app import system_data as sd  # noqa: E402
from scripts.seed_demo_db import seed  # noqa: E402

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 15, 12, 0).astimezone()          # local noon
SCHED = {"daily_at": "06:00", "stages": {**schedule.DEFAULT_EVERY, "export": 0},
         "budget": {"month_usd": {"monid": 40.0, "x_api": 10.0, "anthropic": 20.0}}}
ALL_OK = [(s, "ok", None) for s in schedule.STAGES]
DASHBOARD = str(ROOT / "app" / "dashboard.py")


@pytest.fixture(autouse=True)
def no_password_gate(monkeypatch):
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)   # a .env with a password must not gate these pages


def iso(t: dt.datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def add_run(conn, run_id, hours_ago, notes="", stage="all", minutes=5, stages=(), finished=True, now=NOW):
    """A runs row started `hours_ago` before now and lasting `minutes`; stages: (stage, status, error) run_stages rows."""
    start = now - dt.timedelta(hours=hours_ago)
    end = start + dt.timedelta(minutes=minutes)
    conn.execute("INSERT INTO runs (run_id, started_at, finished_at, stage, notes) VALUES (?,?,?,?,?)",
                 (run_id, iso(start), iso(end) if finished else None, stage, notes))
    for name, status, error in stages:
        conn.execute("INSERT INTO run_stages (run_id, stage, status, started_at, finished_at, error) VALUES (?,?,?,?,?,?)",
                     (run_id, name, status, iso(start), None if status == "running" else iso(end), error))
    conn.commit()


def add_cost(conn, ts: str, provider: str, usd: float, run_id=None):
    conn.execute("INSERT INTO costs (ts, run_id, provider, endpoint, units, unit_cost_usd, cost_usd) VALUES (?,?,?,?,?,?,?)",
                 (ts, run_id, provider, "e", 1, usd, usd))
    conn.commit()


# ---------------------------------------------------------------- words

def test_relative_time_and_money_words():
    assert [sd.ago(NOW - dt.timedelta(seconds=s), NOW) for s in (10, 300, 7200, 86400, 6.9 * 86400)] == [
        "just now", "5 min ago", "2 h ago", "1 day ago", "6 days ago"]
    assert sd.ago(None, NOW) == "never" and sd.ago("not a time", NOW) == "never"
    at_0602 = NOW.replace(hour=6, minute=2)
    assert sd.when(at_0602, NOW) == "today 06:02" and sd.when(iso(at_0602), NOW) == "today 06:02"   # the DB's UTC text
    assert sd.when(at_0602 - dt.timedelta(days=1), NOW) == "yesterday 06:02"
    assert sd.when(NOW.replace(hour=6, minute=0) + dt.timedelta(days=1), NOW) == "tomorrow 06:00"
    assert sd.when(dt.datetime(2026, 9, 28, 6, 2).astimezone(), NOW) == "28 Sep 06:02"
    assert sd.when(dt.datetime(2025, 9, 28, 6, 2).astimezone(), NOW) == "28 Sep 2025 06:02"
    assert sd.when(None, NOW) == "unknown"
    assert [sd.in_days(NOW + dt.timedelta(days=d), NOW) for d in (0, 1, 3)] == ["today", "tomorrow", "in 3 days"]
    assert [sd.duration(s) for s in (45, 750, 3900)] == ["45 s", "12 min", "1 h 05 min"]
    assert (sd.usd(0), sd.usd(0.0358), sd.usd(0.004), sd.usd(12.345)) == ("$0", "$0.04", "$0.0040", "$12.35")
    assert [sd.every_text(d) for d in (0, 1, 7.0, 0.5)] == ["by hand", "every day", "every 7 days", "every 0.5 days"]
    assert sd.is_scheduled("scheduled: discover due") and not sd.is_scheduled("Scheduled by hand") and not sd.is_scheduled(None)
    assert sd.error_line("HTTP 502\n  retry later") == "HTTP 502" and sd.error_line(" \n") == "no error recorded"
    assert sd.error_line("Traceback (most recent call last):\n  File 'x.py', line 1\nKeyError: 'id'\n") == "KeyError: 'id'"


# ---------------------------------------------------------------- the health rule and the verdict

def test_fallback_health_rule(conn):
    """Healthy when the latest scheduled run finished within 36 h and none of its stages failed; never scheduled, stale
    and a failed stage are problems; a provider at its monthly cap is only a warning."""
    h = sd.fallback_health(conn, SCHED, NOW)
    assert not h["ok"] and h["problems"] == ["never scheduled: no run yet"] and h["last_scheduled_run"] is None
    add_run(conn, 1, 72, notes="first try", stage="discover")
    assert sd.fallback_health(conn, SCHED, NOW)["problems"] == [
        "never scheduled: every run so far was started by hand (last run 1, 3 days ago)"]
    add_run(conn, 2, 30, notes="scheduled: discover due", stages=[("ingest", "ok", None), ("discover", "ok", None)])
    add_run(conn, 3, 1, notes="more posts")                                   # a run by hand changes nothing
    h = sd.fallback_health(conn, SCHED, NOW)
    assert h["ok"] and h["problems"] == [] == h["warnings"] and h["last_scheduled_run"]["run_id"] == 2
    stale = sd.fallback_health(conn, SCHED, NOW + dt.timedelta(hours=12))    # run 2 finished 42 h before
    assert not stale["ok"] and stale["problems"] == ["no scheduled run in 36 h (last: run 2, 1 day ago)"]
    add_run(conn, 4, 2, notes="scheduled: discover due", stages=[
        ("ingest", "ok", None), ("score", "ok", None),
        ("discover", "failed", "Traceback (most recent call last):\n  File 'monid.py'\nRuntimeError: HTTP  502 from monid")])
    add_run(conn, 5, 0.1, notes="scheduled: nothing due", finished=False)    # running now: run 4 is still the latest
    h = sd.fallback_health(conn, SCHED, NOW)
    assert not h["ok"] and h["problems"] == ["stage discover failed in run 4: RuntimeError: HTTP 502 from monid"]
    assert h["last_scheduled_run"]["run_id"] == 4 and [s["stage"] for s in h["last_scheduled_run"]["stages"]] == [
        "ingest", "discover", "score"]
    add_run(conn, 6, 1, notes="scheduled: all due", stages=ALL_OK)
    add_cost(conn, iso(NOW - dt.timedelta(days=1)), "monid", 45.0)
    h = sd.fallback_health(conn, SCHED, NOW)
    assert h["ok"] and h["warnings"] == ["monid spent $45.00 this month, cap $40.00 (config/schedule.yaml): its stages "
                                         "are skipped until the 1st"]


def test_fallback_health_when_scheduled_runs_never_finished(conn):
    add_run(conn, 1, 50, notes="by hand")
    add_run(conn, 2, 40, notes="scheduled: all due", finished=False)
    assert sd.fallback_health(conn, SCHED, NOW)["problems"] == ["no scheduled run in 36 h (last: run 2, not finished)"]


def test_health_uses_ops_health_when_it_exists(conn, monkeypatch):
    seen = {}

    def fake(c, sched=None, now=None):
        seen.update(sched=sched, now=now)
        return {"ok": True, "problems": [], "warnings": ["from ops"], "last_scheduled_run": None}
    monkeypatch.setitem(sys.modules, "ring_wishlist.ops", None)                     # no ops.py: the dashboard's own rule
    assert sd.ops_health() is None and sd.health(conn, SCHED, NOW)["source"] == "fallback"
    ops = types.ModuleType("ring_wishlist.ops")
    ops.health = fake
    monkeypatch.setitem(sys.modules, "ring_wishlist.ops", ops)
    h = sd.health(conn, SCHED, NOW)
    assert h["source"] == "ops.health" and h["warnings"] == ["from ops"] and seen == {"sched": SCHED, "now": NOW}

    def broken(c, sched=None, now=None):
        raise RuntimeError("boom")
    ops.health = broken
    h = sd.health(conn, SCHED, NOW)                                          # broken: the same rule, and it says so
    assert h["source"] == "fallback" and h["problems"] == ["never scheduled: no run yet"]
    assert h["warnings"] == ["ops.health failed (boom): this is the dashboard's own check"]


def test_verdict_lines(conn):
    level, head, bullets = sd.verdict(conn, sd.fallback_health(conn, SCHED, NOW), SCHED, NOW)
    assert (level, head) == ("warning", "● **Not scheduled yet**: turn it on with `make schedule-on` (daily at 06:00)")
    assert bullets == ["never scheduled: no run yet"]
    add_run(conn, 57, 6, notes="scheduled: all due", minutes=2, stages=ALL_OK)   # 06:00 to 06:02 today
    level, head, bullets = sd.verdict(conn, sd.fallback_health(conn, SCHED, NOW), SCHED, NOW)
    assert level == "ok" and bullets == []
    assert head == "● **Healthy**: last scheduled run today 06:02 (run 57), 12 stages ok · next run tomorrow 06:00"
    early = NOW.replace(hour=5)                                              # before today's trigger
    assert sd.verdict(conn, sd.fallback_health(conn, SCHED, early), SCHED, early)[1].endswith("· next run today 06:00")
    add_run(conn, 58, 1, notes="scheduled: discover due", stages=[
        ("ingest", "ok", None), ("discover", "failed", "HTTP 502"), ("sample", "skipped", "monid cap reached")])
    add_cost(conn, iso(NOW - dt.timedelta(hours=1)), "x_api", 10.0)
    level, head, bullets = sd.verdict(conn, sd.fallback_health(conn, SCHED, NOW), SCHED, NOW)
    assert (level, head) == ("error", "● **Needs attention** · next run tomorrow 06:00")
    assert bullets == ["stage discover failed in run 58: HTTP 502",
                       "▲ x_api spent $10.00 this month, cap $10.00 (config/schedule.yaml): its stages are skipped "
                       "until the 1st"]
    add_run(conn, 59, 0.5, notes="scheduled: ingest due", stages=[("ingest", "ok", None), ("sample", "skipped", "cap")])
    assert sd.verdict(conn, sd.fallback_health(conn, SCHED, NOW), SCHED, NOW)[1].startswith(
        "● **Healthy**: last scheduled run today 11:35 (run 59), 1 stage ok, 1 skipped · ")
    add_run(conn, 60, 0.2, notes="scheduled: nothing due")
    assert ", nothing was due · next run" in sd.verdict(conn, sd.fallback_health(conn, SCHED, NOW), SCHED, NOW)[1]


# ---------------------------------------------------------------- stages, spend, runs, freshness

def test_stage_rows(conn, monkeypatch):
    monkeypatch.setattr(sd.config, "llm_backend", lambda: "anthropic")
    add_run(conn, 1, 6, notes="scheduled: all due", minutes=2, stages=[("ingest", "ok", None), ("crawl", "failed", "x")])
    add_run(conn, 2, 3 * 24 + 6, notes="by hand", minutes=2, stages=[("discover", "ok", None)])
    add_cost(conn, iso(NOW - dt.timedelta(hours=1)), "x_api", 12.0)          # over its cap: resolve and verify wait
    rows = {r["stage"]: r for r in sd.stage_rows(conn, SCHED, NOW)}
    assert list(rows) == schedule.STAGES
    assert rows["ingest"] == {"stage": "ingest", "every": "every day", "last_ok": "5 h ago", "next_due": "tomorrow",
                              "provider": "–", "paid": "free"}
    assert rows["discover"] == {"stage": "discover", "every": "every 7 days", "last_ok": "3 days ago",
                                "next_due": "in 4 days", "provider": "monid", "paid": "paid"}
    assert (rows["crawl"]["last_ok"], rows["crawl"]["next_due"]) == ("never", "tomorrow")   # failed: due at the next run
    assert rows["expand"]["next_due"] == "tomorrow" and rows["expand"]["every"] == "every 14 days"
    assert rows["export"]["every"] == "by hand" and rows["export"]["next_due"] == "not scheduled"
    assert rows["resolve"]["next_due"] == rows["verify"]["next_due"] == "on hold: x_api cap"
    assert rows["classify"]["provider"] == "anthropic" and rows["classify"]["paid"] == "paid"
    monkeypatch.setattr(sd.config, "llm_backend", lambda: "claude_cli")          # free on the Claude Code login
    cli = {r["stage"]: r for r in sd.stage_rows(conn, SCHED, NOW)}["classify"]
    assert (cli["provider"], cli["paid"]) == ("Claude Code CLI", "free")
    early = {r["stage"]: r["next_due"] for r in sd.stage_rows(conn, SCHED, NOW.replace(hour=5))}
    assert early["expand"] == "today" and early["discover"] == "in 4 days"   # due stages run at today's 06:00


def test_spend(conn):
    add_cost(conn, iso(NOW - dt.timedelta(days=3)), "monid", 12.0)
    add_cost(conn, "2026-09-30T23:00:00Z", "monid", 99.0)                    # last month (UTC)
    add_cost(conn, iso(NOW - dt.timedelta(hours=1)), "x_api", 10.0)          # today, at its cap
    add_cost(conn, iso(NOW - dt.timedelta(hours=2)), "openai", 1.0)          # a provider without a cap
    s = sd.spend(conn, SCHED, NOW)
    assert s["providers"] == [
        {"provider": "monid", "used": 12.0, "cap": 40.0, "share": 0.3, "capped": False},
        {"provider": "x_api", "used": 10.0, "cap": 10.0, "share": 1.0, "capped": True},
        {"provider": "anthropic", "used": 0.0, "cap": 20.0, "share": 0.0, "capped": False},
        {"provider": "openai", "used": 1.0, "cap": None, "share": None, "capped": False}]
    assert s["today"] == 11.0 and s["month"] == 23.0
    zero = {**SCHED, "budget": {"month_usd": {"monid": 0.0}}}
    assert sd.spend(conn, zero, NOW)["providers"][0] == {"provider": "monid", "used": 12.0, "cap": 0.0, "share": 1.0,
                                                         "capped": True}


def test_recent_runs(conn):
    add_run(conn, 1, 50, stage="paths,score")                                # from before run_stages: no marks
    add_cost(conn, iso(NOW - dt.timedelta(hours=50)), "monid", 0.5, run_id=1)
    add_run(conn, 2, 1, notes="scheduled: discover due", finished=False, stages=[
        ("score", "running", None), ("ingest", "ok", None), ("sample", "skipped", "monid cap"),
        ("discover", "failed", "HTTP 502 from monid")])
    add_run(conn, 3, 0.5, notes="check @someone", stage="check", minutes=1)
    runs = sd.recent_runs(conn)
    assert [r["run_id"] for r in runs] == [3, 2, 1]
    new, sched_run, old = runs
    assert sched_run["stages"] == "ingest ✓ · discover ✗ · sample – · score …"   # pipeline order
    assert (sched_run["trigger"], sched_run["duration"]) == ("scheduled", "running")
    assert [(s["stage"], s["error"]) for s in sched_run["failed"]] == [("discover", "HTTP 502 from monid")]
    assert (old["stages"], old["trigger"], old["duration"], old["cost_usd"], old["failed"]) == (
        "paths · score", "by hand", "5 min", 0.5, [])
    assert (new["stages"], new["trigger"], new["duration"]) == ("check", "by hand", "1 min")
    assert len(sd.recent_runs(conn, 2)) == 2
    conn.execute("UPDATE runs SET finished_at=NULL WHERE run_id=1")
    assert sd.recent_runs(conn)[2]["duration"] == "not finished"


def test_freshness(conn):
    assert [(r["what"], r["at"]) for r in sd.freshness(conn)] == [
        ("Newest post fetched", None), ("Newest classification", None), ("Newest engagement sample", None),
        ("Newest score run", None)]
    add_run(conn, 7, 3)
    for ts in ("2026-10-14T08:00:00Z", "2026-10-15T08:00:00Z"):
        conn.execute("INSERT INTO x_posts (post_id, x_id, fetched_at) VALUES (?, '1', ?)", (ts, ts))
    conn.execute("INSERT INTO classifications (candidate_id, run_id, json, created_at) VALUES (1, 7, '{}', "
                 "'2026-10-13T08:00:00Z')")
    conn.execute("INSERT INTO engagement_samples (x_id, fetched_at, post_ids) VALUES ('1', '2026-10-12T08:00:00Z', '[]')")
    conn.execute("INSERT INTO scores (run_id, candidate_id, total) VALUES (7, 1, 50)")
    conn.commit()
    assert [(r["what"], r["at"]) for r in sd.freshness(conn)] == [
        ("Newest post fetched", "2026-10-15T08:00:00Z"), ("Newest classification", "2026-10-13T08:00:00Z"),
        ("Newest engagement sample", "2026-10-12T08:00:00Z"), ("Newest score run (run 7)", iso(NOW - dt.timedelta(
            hours=3) + dt.timedelta(minutes=5)))]


# ---------------------------------------------------------------- alerts.log and backups

def test_alerts_and_backups(tmp_path):
    log = tmp_path / "alerts.log"
    assert sd.read_alerts(log) is None
    log.write_text("")
    assert sd.read_alerts(log) == []
    lines = [f"2026-10-1{i}T06:05:00Z\t{lvl}\tmessage {i}" for i, lvl in enumerate(
        ("info", "warning", "ERROR", "info", "warning", "error"))]
    log.write_text("\n".join(lines[:3] + ["", "a line without tabs"] + lines[3:]) + "\n")
    alerts = sd.read_alerts(log)
    assert [a["message"] for a in alerts] == ["message 5", "message 4", "message 3", "a line without tabs", "message 2"]
    assert alerts[0] == {"at": "2026-10-15T06:05:00Z", "level": "error", "message": "message 5"}
    assert alerts[3] == {"at": None, "level": "", "message": "a line without tabs"} and alerts[4]["level"] == "error"
    assert len(sd.read_alerts(log, 2)) == 2
    folder = tmp_path / "backups"
    assert sd.newest_backup(folder) is None
    folder.mkdir()
    (folder / "notes.txt").write_text("not a backup")
    for i, name in enumerate(("wishlist-20261013-0600.db", "wishlist-20261015-0600.db", "wishlist-20261014-0600.db")):
        (folder / name).write_bytes(b"")
        os.utime(folder / name, (1_760_000_000 + i * 3600,) * 2)
    b = sd.newest_backup(folder)
    assert b == {"name": "wishlist-20261014-0600.db", "at": dt.datetime.fromtimestamp(1_760_007_200, UTC), "count": 3}


# ---------------------------------------------------------------- the page (AppTest, no server, no network)

def _app(monkeypatch, db_path, tab: str | None = None):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("DB_PATH", str(db_path))
    at = AppTest.from_file(DASHBOARD, default_timeout=60)
    if tab:
        at.session_state["main_tab"] = tab
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def test_system_tab_page(tmp_path, monkeypatch):
    p = tmp_path / "system.db"
    conn = db.connect(p)
    now = dt.datetime.now(UTC)
    try:
        add_run(conn, 1, 50, stage="all", now=now)
        add_run(conn, 2, 2, notes="scheduled: discover due", now=now, stages=[
            ("ingest", "ok", None), ("discover", "failed", "HTTP 502 from monid"), ("score", "ok", None)])
        add_cost(conn, iso(now - dt.timedelta(minutes=90)), "monid", 1.25, run_id=2)
    finally:
        conn.close()
    log = tmp_path / "alerts.log"
    log.write_text(f"{iso(now)}\terror\tstage discover failed in run 2: HTTP 502 from monid\n")
    (tmp_path / "backups").mkdir()
    (tmp_path / "backups" / "wishlist-20261015-0600.db").write_bytes(b"")
    monkeypatch.setattr(sd, "ops_health", lambda: None)         # the dashboard's own rule, whatever ops.py says
    monkeypatch.setattr(sd, "ALERTS_LOG", log)
    monkeypatch.setattr(sd, "BACKUPS_DIR", tmp_path / "backups")
    at = _app(monkeypatch, p, tab="System")
    assert [t.label for t in at.tabs] == ["Wishlist", "Check an account", "System"]
    assert at.error[0].value.startswith("● **Needs attention** · next run ")
    assert "- stage discover failed in run 2: HTTP 502 from monid" in at.error[0].value
    stages, runs = at.dataframe[0].value, at.table[0].value                  # the runs: a table, long stage lists wrap
    assert list(stages["stage"]) == schedule.STAGES and set(stages["paid"]) == {"paid", "free"}
    assert list(runs["Run"]) == [2, 1] and list(runs["Trigger"]) == ["scheduled", "by hand"]
    assert [s.replace(" ", " ") for s in runs["Stages"]] == ["ingest ✓ · discover ✗ · score ✓", "all"]
    assert runs.iloc[0]["Cost"] == "$1.25"
    assert [e.label for e in at.expander] == ["Run 2 · discover failed"] and at.code[0].value == "HTTP 502 from monid"
    assert [(m.label, m.value) for m in at.metric] == [("Today", "$1.25"), ("This month", "$1.25")]
    text = "\n".join(m.value for m in at.markdown)
    assert "- Newest post fetched: none yet" in text and "On the list now" not in text   # the Wishlist did not rank yet
    assert ":red[**error**] · stage discover failed in run 2: HTTP 502 from monid" in text
    assert "Newest: `wishlist-20261015-0600.db`, just now" in text
    assert "| Run once now: the due stages, as the schedule would | `make ops-run` | **paid** |" in text
    assert any(c.value.startswith("Password gate off") for c in at.caption)


def test_system_tab_on_the_demo(tmp_path, monkeypatch):
    """Never scheduled (every demo run was by hand): amber, with the command that turns it on; the list count comes from
    the Wishlist tab once it has ranked the list."""
    p = tmp_path / "demo.db"
    seed(p, quiet=True)
    monkeypatch.setattr(sd, "ops_health", lambda: None)
    monkeypatch.setattr(sd, "ALERTS_LOG", tmp_path / "no_alerts.log")
    monkeypatch.setattr(sd, "BACKUPS_DIR", tmp_path / "no_backups")
    at = _app(monkeypatch, p)                                                # the Wishlist first, as the page opens
    at.session_state["main_tab"] = "System"
    at.run()
    assert not at.exception and not at.error
    assert at.warning[0].value.startswith(
        "● **Not scheduled yet**: turn it on with `make schedule-on` (daily at 06:00)\n\n- never scheduled: every run so "
        "far was started by hand (last run 2, ")
    text = "\n".join(m.value for m in at.markdown)
    assert "- On the list now: 3 people who meet the bar (run 2, with your Wishlist filters)" in text
    assert "Recent alerts" not in text and "Backups" not in text              # no log, no backup: no section


def test_password_gate(tmp_path, monkeypatch):
    p = tmp_path / "gate.db"
    db.connect(p).close()
    at = _app(monkeypatch, p)                                                 # unset: no gate
    assert [t.label for t in at.tabs] == ["Wishlist", "Check an account", "System"] and not at.text_input
    monkeypatch.setenv("DASHBOARD_PASSWORD", "open sesame")
    at = _app(monkeypatch, p, tab="System")
    assert not at.tabs and not at.info and [t.label for t in at.text_input] == ["Password"] and not at.error
    at.text_input(key="gate_password").input("open sesame!").run()
    assert not at.exception and not at.tabs and [e.value for e in at.error] == ["Wrong password."]
    at.text_input(key="gate_password").input("open sesame").run()
    assert not at.exception and [t.label for t in at.tabs] == ["Wishlist", "Check an account", "System"]
    assert not at.text_input and any(c.value.startswith("Password gate on") for c in at.caption)
    at.run()                                                                  # remembered for the session
    assert [t.label for t in at.tabs] == ["Wishlist", "Check an account", "System"]
