"""The runtime (src/ring_wishlist/ops.py) and the run_stages record pipeline.run keeps. Offline (conftest forces DRY_RUN=1: the
pipeline stages read fixtures) on the temp `conn` database; logs, backups, exports and config/schedule.yaml live in
tmp_path; requests.post fails the test, so nothing reaches the network."""
import datetime as dt
import json
import os
import re
import signal
import sqlite3
import time

import pytest
import requests

from ring_wishlist import config, costs, db, ops, pipeline, schedule

EVERY = dict(schedule.DEFAULT_EVERY)
MONID_STAGES = {"crawl", "discover", "expand", "enrich", "sample"}


def use_schedule(tmp_path, monkeypatch, stages=None, month_usd=None):
    p = tmp_path / "schedule.yaml"
    p.write_text(json.dumps({"daily_at": "06:00", "stages": stages or EVERY,
                             "budget": {"month_usd": month_usd or {"monid": 40, "x_api": 10, "anthropic": 20}}}))
    monkeypatch.setattr(schedule, "SCHEDULE_FILE", p)


@pytest.fixture(autouse=True)
def rails(tmp_path, monkeypatch):
    use_schedule(tmp_path, monkeypatch)
    monkeypatch.setattr(ops, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(ops, "BACKUPS_DIR", tmp_path / "backups")
    monkeypatch.setattr(config, "EXPORTS_DIR", tmp_path / "exports")
    monkeypatch.setenv("CARLO_X_HANDLE", "carlo_test")
    monkeypatch.setenv("RING_WISHLIST_NO_DESKTOP_NOTIFY", "1")
    monkeypatch.delenv("NOTIFY_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("BUDGET_IGNORE_MONTH", raising=False)
    monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("tests never call the network"))


@pytest.fixture
def cheap(tmp_path, monkeypatch):
    """Only paths and score are scheduled (on an empty database both take milliseconds)."""
    use_schedule(tmp_path, monkeypatch, stages={**{s: 0 for s in EVERY}, "paths": 1, "score": 1})


NOW = dt.datetime.now(dt.timezone.utc)      # one clock for the whole module: a test never straddles a second


def iso(hours_ago: float) -> str:
    return (NOW - dt.timedelta(hours=hours_ago)).strftime(ops.TS)


def stages_of(conn, run_id) -> dict:
    return {r["stage"]: r for r in db.rows(conn, "SELECT * FROM run_stages WHERE run_id=?", (run_id,))}


def status_of(conn, run_id) -> dict:
    return {s: r["status"] for s, r in stages_of(conn, run_id).items()}


def last_scheduled(conn) -> dict:
    return db.one(conn, "SELECT * FROM runs WHERE notes LIKE 'scheduled%' ORDER BY run_id DESC LIMIT 1")


def spend(conn, provider: str, usd: float) -> None:
    conn.execute("INSERT INTO costs (ts, provider, endpoint, units, unit_cost_usd, cost_usd) VALUES (?,?,?,?,?,?)",
                 (db.utcnow(), provider, "test", 1, usd, usd))
    conn.commit()


def boom(*a, **k):
    raise RuntimeError("boom")


# ---------------------------------------------------------------- pipeline.run records run_stages

def test_pipeline_records_every_stage(conn, monkeypatch):
    def paths(conn, run_id, seed_handle=None):
        costs.record(conn, "x_api", "test", 10, 0.025, run_id)             # this stage spends $0.25
        return {"paths": 3}
    monkeypatch.setattr("ring_wishlist.paths.run_paths", paths)
    res = pipeline.run("paths,score,export", notes="a test", skipped={"crawl": "monthly cap: monid spent $40.00"})
    rows = stages_of(conn, res["run_id"])
    assert {s: r["status"] for s, r in rows.items()} == {"crawl": "skipped", "paths": "ok", "score": "ok", "export": "ok"}
    assert rows["crawl"]["error"] == "monthly cap: monid spent $40.00" and rows["crawl"]["started_at"] is None
    assert rows["paths"]["cost_usd"] == 0.25 and rows["score"]["cost_usd"] == 0 and res["cost_usd"] == 0.25
    assert json.loads(rows["paths"]["result_json"]) == {"paths": 3} and rows["paths"]["error"] is None
    assert all(r["finished_at"] >= r["started_at"] and r["seconds"] >= 0 for s, r in rows.items() if s != "crawl")
    assert db.one(conn, "SELECT notes FROM runs WHERE run_id=?", (res["run_id"],))["notes"] == "a test"


def test_pipeline_records_failures_budget_stops_and_interruptions(conn, monkeypatch):
    monkeypatch.setattr("ring_wishlist.paths.run_paths", boom)                   # not fatal: the run goes on
    monkeypatch.setattr("ring_wishlist.score.run_score", boom)                   # fatal: export is not reached
    rid = pipeline.run("paths,score,export")["run_id"]
    rows = stages_of(conn, rid)
    assert {s: r["status"] for s, r in rows.items()} == {"paths": "failed", "score": "failed", "export": "skipped"}
    assert rows["paths"]["error"] == "boom" and rows["export"]["error"] == "not run: score failed"

    def over(*a, **k):
        raise costs.BudgetExceeded("monid: spent $40.00 this month, monthly cap is $40.00")
    monkeypatch.setattr("ring_wishlist.paths.run_paths", over)
    res = pipeline.run("paths,export")
    assert res["paths"]["budget_stop"] and status_of(conn, res["run_id"]) == {"paths": "failed", "export": "skipped"}
    assert stages_of(conn, res["run_id"])["export"]["error"] == "not run: paths was stopped by the budget guard"

    def ctrl_c(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr("ring_wishlist.paths.run_paths", ctrl_c)
    with pytest.raises(KeyboardInterrupt):
        pipeline.run("paths,export")
    rid = db.latest_run_id(conn)
    assert status_of(conn, rid) == {"paths": "failed", "export": "skipped"}
    assert stages_of(conn, rid)["paths"]["error"] == "interrupted: KeyboardInterrupt"
    assert db.one(conn, "SELECT finished_at FROM runs WHERE run_id=?", (rid,))["finished_at"]   # the run is closed


# ---------------------------------------------------------------- the scheduled run

def test_scheduled_run_does_the_due_stages_then_nothing(conn):
    assert ops.run() == ops.EXIT_OK                                       # a new database: every stage is due
    first = last_scheduled(conn)
    assert first["notes"] == "scheduled: 12 due" and first["finished_at"]
    assert status_of(conn, first["run_id"]) == {s: "ok" for s in schedule.STAGES}
    log_text = "".join(p.read_text() for p in ops.LOGS_DIR.glob("run-*.log"))
    assert "ring_wishlist.pipeline: stage score" in log_text and "[cost] fixture crawl" in log_text   # logging and prints

    two_days_ago = iso(48)
    conn.execute("UPDATE run_stages SET finished_at=? WHERE stage IN ('paths', 'score')", (two_days_ago,))
    conn.commit()
    assert ops.run() == ops.EXIT_OK                                       # only the stages due again
    second = last_scheduled(conn)
    assert second["stage"] == "paths,score" and second["notes"] == "scheduled: 2 due"
    rows = stages_of(conn, second["run_id"])
    assert rows["paths"]["status"] == rows["score"]["status"] == "ok"
    assert rows["crawl"]["status"] == "skipped" and rows["crawl"]["error"].startswith("not due: last ok 0.0 days ago")

    assert ops.run() == ops.EXIT_OK                                       # nothing due: still recorded, so health sees it
    third = last_scheduled(conn)
    assert third["notes"] == ops.NOTHING_DUE and set(status_of(conn, third["run_id"]).values()) == {"skipped"}
    assert ops.health(conn)["ok"] and ops.health(conn)["last_scheduled_run"]["run_id"] == third["run_id"]
    assert len(list(ops.BACKUPS_DIR.glob("wishlist-*.db"))) == 2 and ops.read_alerts() == []   # none when nothing ran


def test_capped_providers_are_skipped_and_the_free_stages_still_run(conn, monkeypatch):
    spend(conn, "monid", 40.0)                                            # monid at its monthly cap
    monkeypatch.setenv("BUDGET_IGNORE_MONTH", "1")                        # for manual runs only: a scheduled run keeps the caps
    assert ops.run() == ops.EXIT_OK and "BUDGET_IGNORE_MONTH" not in os.environ
    r = last_scheduled(conn)
    rows = stages_of(conn, r["run_id"])
    assert {s for s, x in rows.items() if x["status"] == "skipped"} == MONID_STAGES
    assert {s for s, x in rows.items() if x["status"] == "ok"} == set(schedule.STAGES) - MONID_STAGES
    assert rows["enrich"]["error"] == "monthly cap: monid spent $40.00 this month, cap $40.00 (config/schedule.yaml)"
    assert r["notes"] == "scheduled: 7 due, capped: monid"
    h = ops.health(conn)
    assert h["ok"] and h["warnings"] == ["monthly cap reached: monid spent $40.00 this month, cap $40.00 (config/schedule.yaml)"]
    assert ops.run() == ops.EXIT_OK                                       # the next day: the cap alert is not repeated
    caps = [a for a in ops.read_alerts() if a["message"].startswith("monthly cap reached: monid ")]
    assert len(caps) == 1 and caps[0]["level"] == "warning" and caps[0]["message"].endswith("skipped until the 1st")


def test_classify_counts_as_anthropic_only_on_that_backend(conn, monkeypatch):
    spend(conn, "anthropic", 25.0)
    monkeypatch.setattr(config, "llm_backend", lambda: "anthropic")
    assert ops.stage_providers("classify") == ("anthropic",)
    assert ops.plan(conn)["skipped"]["classify"].startswith("monthly cap: anthropic spent $25.00")
    monkeypatch.setattr(config, "llm_backend", lambda: "claude_cli")
    assert ops.stage_providers("classify") == () and "classify" in ops.plan(conn)["run"]
    assert ops.stage_providers("paths") == () and ops.stage_providers("verify") == ("x_api",)


def test_a_failed_stage_exits_3_with_an_error_alert(conn, cheap, monkeypatch):
    monkeypatch.setattr("ring_wishlist.paths.run_paths", boom)
    assert ops.run() == ops.EXIT_FAILED
    a = ops.read_alerts()[-1]
    assert a["level"] == "error"
    assert re.fullmatch(r"scheduled run \d+: stage paths failed: boom \(log: .+/run-\d{8}-\d{4}\.log\)", a["message"])
    h = ops.health(conn)
    assert not h["ok"] and h["problems"] == [f"stage paths failed in run {last_scheduled(conn)['run_id']}: boom"]


def test_a_budget_stop_exits_3_with_a_warning(conn, cheap, monkeypatch):
    monkeypatch.setattr("ring_wishlist.score.run_score", lambda conn, run_id: {"scored": 0, "budget_stop": True})
    assert ops.run() == ops.EXIT_FAILED
    a = ops.read_alerts()[-1]
    assert a["level"] == "warning" and "a budget stopped score before it finished" in a["message"]
    assert ops.health(conn)["ok"]                                         # the stage itself finished


def test_a_crash_exits_2_alerts_and_closes_the_run(conn, cheap, monkeypatch):
    def crash(stage, notes="", skipped=None, **kw):
        c = db.connect()
        rid = db.new_run(c, stage, notes)
        pipeline.stage_row(c, rid, "paths", "running", started_at=db.utcnow())
        c.close()
        raise RuntimeError("disk full")
    monkeypatch.setattr(pipeline, "run", crash)
    assert ops.run() == ops.EXIT_CRASH
    a = ops.read_alerts()[-1]
    assert a["level"] == "error" and a["message"].startswith("scheduled run crashed: RuntimeError: disk full (log: ")
    r = last_scheduled(conn)
    assert r["finished_at"] and stages_of(conn, r["run_id"])["paths"]["error"] == "crashed: RuntimeError: disk full"


def test_sigterm_during_a_run_records_the_stage_and_stops(conn, cheap, monkeypatch):
    def term(*a, **k):
        raise ops.Stop("SIGTERM")                                         # what the SIGTERM handler raises
    monkeypatch.setattr("ring_wishlist.paths.run_paths", term)
    with pytest.raises(ops.Stop):
        ops.run()
    r = last_scheduled(conn)
    rows = stages_of(conn, r["run_id"])
    assert rows["paths"]["status"] == "failed" and rows["paths"]["error"] == "interrupted: SIGTERM"
    assert rows["score"]["error"] == "not run: the run was interrupted during paths" and r["finished_at"]
    assert ops.read_alerts()[-1]["message"].startswith(f"scheduled run {r['run_id']}: stage paths failed: interrupted")
    assert ops.main(["run"]) == ops.EXIT_FAILED                          # `ops run` exits 3; the lock was released


def test_a_held_lock_exits_75(conn, cheap):
    b = ops.backup(conn)
    with ops.lock():
        assert ops.run() == ops.EXIT_LOCKED
        with pytest.raises(ops.Locked, match=f"pid {os.getpid()}"):
            ops.restore(b)
    assert db.one(conn, "SELECT COUNT(*) AS n FROM runs")["n"] == 0
    (ops.LOGS_DIR / ".ops.lock").write_text("999999\n")                   # a dead run's lock file does not block
    assert ops.run() == ops.EXIT_OK


def test_abandoned_work_is_closed(conn):
    q = conn.execute
    q("INSERT INTO runs (run_id, started_at, stage, notes) VALUES (1, ?, 'paths,score', 'scheduled: 2 due')", (iso(1),))
    q("INSERT INTO run_stages (run_id, stage, status, started_at, finished_at) VALUES (1, 'paths', 'ok', ?, ?)", (iso(1), iso(0.9)))
    q("INSERT INTO run_stages (run_id, stage, status, started_at) VALUES (1, 'score', 'running', ?)", (iso(0.9),))
    q("INSERT INTO runs (run_id, started_at, stage) VALUES (2, ?, 'paths,score')", (iso(24),))      # before run_stages
    q("INSERT INTO runs (run_id, started_at, stage) VALUES (3, ?, 'enrich')", (iso(2),))           # a manual run going on
    q("INSERT INTO run_stages (run_id, stage, status, started_at) VALUES (3, 'enrich', 'running', ?)", (iso(2),))
    q("INSERT INTO runs (run_id, started_at, finished_at, stage) VALUES (4, ?, ?, 'score')", (iso(30), iso(29)))
    q("INSERT INTO run_stages (run_id, stage, status) VALUES (4, 'score', 'running')")
    conn.commit()
    assert [r["run_id"] for r in ops.close_abandoned(conn)] == [1, 2]
    assert status_of(conn, 1) == {"paths": "ok", "score": "failed"} and stages_of(conn, 1)["score"]["error"] == ops.ABANDONED
    assert status_of(conn, 2) == {"paths": "failed", "score": "failed"}   # never taken for a success
    assert status_of(conn, 3) == {"enrich": "running"} and status_of(conn, 4) == {"score": "failed"}
    runs = {r["run_id"]: r for r in db.rows(conn, "SELECT * FROM runs")}
    assert runs[1]["finished_at"] == iso(0.9) and runs[1]["notes"] == "scheduled: 2 due [abandoned]"
    assert runs[2]["finished_at"] == iso(24) and runs[3]["finished_at"] is None
    ok = schedule.last_ok(conn)
    assert "score" not in ok and ok["paths"] == iso(0.9)
    assert ops.health(conn)["problems"] == [f"stage score failed in run 1: {ops.ABANDONED}"]


def test_a_scheduled_run_found_abandoned_is_alerted(conn, cheap):
    conn.execute("INSERT INTO runs (started_at, stage, notes) VALUES (?, 'paths', 'scheduled: 1 due')", (iso(0.5),))
    conn.commit()
    assert ops.run() == ops.EXIT_OK
    assert ops.read_alerts()[0]["message"].startswith("scheduled run(s) 1 abandoned")


# ---------------------------------------------------------------- backups, logs

def test_backups_rotate_to_the_newest_14(conn):
    ops.BACKUPS_DIR.mkdir(parents=True)
    t = time.time()
    for i in range(15):                                                    # 15 older backups, a day apart
        f = ops.BACKUPS_DIR / f"wishlist-200001{i + 1:02d}-0600.db"
        f.write_bytes(b"")
        os.utime(f, (t - (15 - i) * 86400,) * 2)
    (ops.BACKUPS_DIR / "notes.txt").write_text("not a backup")
    db.new_run(conn, "score", "in the backup")
    new = ops.backup(conn)
    left = {p.name for p in ops.BACKUPS_DIR.glob("wishlist-*.db")}
    assert len(left) == 14 and new.name in left and (ops.BACKUPS_DIR / "notes.txt").exists()
    assert not left & {"wishlist-20000101-0600.db", "wishlist-20000102-0600.db"}   # the two oldest went
    b = sqlite3.connect(new)
    assert b.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert b.execute("SELECT notes FROM runs").fetchone()[0] == "in the backup"
    b.close()
    assert re.fullmatch(r"wishlist-\d{8}-\d{4}\.db", new.name) and ops.backup(conn).name != new.name   # never overwritten


def test_restore_copies_a_backup_over_the_database(conn, tmp_path):
    db.new_run(conn, "score", "before the backup")
    b = ops.backup(conn)
    db.new_run(conn, "score", "after the backup")
    safety = ops.restore(b)
    assert [r["notes"] for r in db.rows(conn, "SELECT notes FROM runs")] == ["before the backup"]
    s = sqlite3.connect(safety)
    assert s.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2 and "before-restore" in safety.name
    s.close()
    assert ops.read_alerts()[-1]["level"] == "info"
    junk = tmp_path / "junk.db"
    junk.write_text("not a database")
    with pytest.raises(ValueError, match="not a healthy wishlist database"):
        ops.restore(junk)
    assert ops.main(["restore", str(junk)]) == 1


def test_old_run_logs_are_pruned():
    ops.LOGS_DIR.mkdir(parents=True)
    old = time.time() - 31 * 86400
    names = ["run-20260801-0600.log", "refresh-20260801-0700.log", "run-classify.log", "alerts.log", "run-20260929-0600.log"]
    for n in names:
        (ops.LOGS_DIR / n).write_text("x")
    for n in names[:4]:
        os.utime(ops.LOGS_DIR / n, (old, old))
    assert {p.name for p in ops.prune_logs()} == {"run-20260801-0600.log", "refresh-20260801-0700.log"}
    assert {p.name for p in ops.LOGS_DIR.iterdir()} == {"run-classify.log", "alerts.log", "run-20260929-0600.log"}


# ---------------------------------------------------------------- health

def add_run(conn, run_id, started, finished, notes, stages):
    conn.execute("INSERT INTO runs (run_id, started_at, finished_at, stage, notes) VALUES (?,?,?,?,?)",
                 (run_id, started, finished, ",".join(stages), notes))
    for st, (status, error) in stages.items():
        conn.execute("INSERT INTO run_stages (run_id, stage, status, error) VALUES (?,?,?,?)", (run_id, st, status, error))
    conn.commit()


def test_health_rule(conn):
    h = ops.health(conn)
    assert not h["ok"] and h["problems"] == ["no scheduled run in 36 h (last: never)"] and h["last_scheduled_run"] is None
    add_run(conn, 1, iso(3), iso(2), "scheduled: 2 due", {"paths": ("ok", None), "score": ("failed", "boom")})
    add_run(conn, 2, iso(1), iso(0.5), "", {"score": ("ok", None)})           # a manual run does not count
    h = ops.health(conn)
    assert not h["ok"] and h["problems"] == ["stage score failed in run 1: boom"]
    assert h["last_scheduled_run"]["run_id"] == 1 and [s["stage"] for s in h["last_scheduled_run"]["stages"]] == ["paths", "score"]
    add_run(conn, 3, iso(0.2), iso(0.2), ops.NOTHING_DUE, {})                 # nothing due: the scheduler is alive
    assert ops.health(conn)["ok"] and ops.health(conn)["problems"] == []
    add_run(conn, 4, iso(0.1), None, "scheduled: 1 due", {"score": ("running", None)})   # in progress: not yet counted
    assert ops.health(conn)["last_scheduled_run"]["run_id"] == 3
    later = ops.health(conn, now=NOW + dt.timedelta(hours=40))
    assert not later["ok"] and later["problems"] == [f"no scheduled run in 36 h (last: {iso(0.2)})"]
    spend(conn, "x_api", 10.0)                                                 # at the cap: a warning, still healthy
    h = ops.health(conn)
    assert h["ok"] and h["warnings"] == ["monthly cap reached: x_api spent $10.00 this month, cap $10.00 (config/schedule.yaml)"]

    class Damaged:                                                             # PRAGMA quick_check finds damage
        def __init__(self, c):
            self.c = c

        def execute(self, sql, params=()):
            if "quick_check" in sql:
                return self.c.execute("SELECT 'row 3 missing from index ix_x' UNION ALL SELECT 'wrong # of entries'")
            return self.c.execute(sql, params)
    h = ops.health(Damaged(conn))
    assert not h["ok"] and h["problems"] == ["database quick_check failed: row 3 missing from index ix_x; wrong # of entries"]


def test_health_and_plan_cli(conn, capsys):
    assert ops.main(["health"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert set(out) >= {"ok", "problems", "warnings", "last_scheduled_run"} and not out["ok"]
    assert ops.main(["plan"]) == 0
    text = capsys.readouterr().out
    assert "Would run: ingest, crawl" in text and "Next trigger: " in text and "monid $0.00 of $40.00" in text
    assert db.one(conn, "SELECT COUNT(*) AS n FROM runs")["n"] == 0                # plan and health write nothing


# ---------------------------------------------------------------- alerts

def test_alert_lines_have_a_fixed_format():
    line = ops.alert("warning", "stage enrich\tfailed:\n  boom")
    assert (ops.LOGS_DIR / "alerts.log").read_text() == line + "\n"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\twarning\tstage enrich failed: boom", line)
    assert ops.read_alerts() == [{"ts": line[:20], "level": "warning", "message": "stage enrich failed: boom"}]


def test_alerts_post_to_the_webhook_and_never_raise(monkeypatch):
    sent = []

    class Ok:
        status_code = 200
    monkeypatch.setattr(requests, "post", lambda url, json=None, timeout=None: sent.append((url, json, timeout)) or Ok())
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "https://hooks.example.test/T000/B000")
    ops.alert("error", "scheduled run 7: stage enrich failed: boom")
    assert sent == [("https://hooks.example.test/T000/B000",
                     {"text": "ring_wishlist wishlist error: scheduled run 7: stage enrich failed: boom"}, 10)]

    def down(*a, **k):
        raise requests.ConnectionError("no route to host")
    monkeypatch.setattr(requests, "post", down)
    ops.alert("error", "the webhook is down")                                # logged, never raised
    assert [a["message"] for a in ops.read_alerts()] == ["scheduled run 7: stage enrich failed: boom", "the webhook is down"]


def test_desktop_notification_only_on_macos_and_when_allowed(monkeypatch):
    calls = []
    monkeypatch.setattr(ops.subprocess, "run", lambda argv, **kw: calls.append(argv))
    monkeypatch.setattr(ops.platform, "system", lambda: "Darwin")
    monkeypatch.delenv("RING_WISHLIST_NO_DESKTOP_NOTIFY")
    ops.alert("error", 'a "quoted" failure')
    assert calls and calls[0][0] == "osascript" and calls[0][-2:] == ['a "quoted" failure', "ring_wishlist wishlist: error"]
    monkeypatch.setenv("RING_WISHLIST_NO_DESKTOP_NOTIFY", "1")
    ops.alert("error", "quiet")
    monkeypatch.delenv("RING_WISHLIST_NO_DESKTOP_NOTIFY")
    monkeypatch.setattr(ops.platform, "system", lambda: "Linux")
    ops.alert("error", "a server")
    assert len(calls) == 1


# ---------------------------------------------------------------- serve

def test_serve_runs_at_the_trigger_and_stops_on_sigterm(monkeypatch):
    calls = []
    monkeypatch.setattr(ops, "run", lambda: calls.append("run") or 0)
    now = dt.datetime.now()
    times = iter([now - dt.timedelta(minutes=1), now + dt.timedelta(hours=1)])  # due at once, then in an hour
    monkeypatch.setattr(schedule, "next_run_at", lambda sched=None, now=None: next(times))
    monkeypatch.setattr(ops.time, "sleep", lambda s: os.kill(os.getpid(), signal.SIGTERM))
    before = signal.getsignal(signal.SIGTERM)
    assert ops.serve() == ops.EXIT_OK
    assert calls == ["run"] and signal.getsignal(signal.SIGTERM) is before
