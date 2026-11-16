"""The schedule (src/wearable_wishlist/schedule.py): due stages, monthly spend and caps, next trigger."""
import datetime as dt

from wearable_wishlist import db, schedule as S

NOW = dt.datetime(2026, 10, 15, 6, 0, tzinfo=dt.timezone.utc)


def iso(days_ago: float) -> str:
    return (NOW - dt.timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def stage_row(conn, run_id, stage, status, days_ago):
    conn.execute("INSERT INTO runs (run_id, started_at, finished_at, stage) VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
                 (run_id, iso(days_ago), iso(days_ago), stage))
    conn.execute("INSERT INTO run_stages (run_id, stage, status, started_at, finished_at) VALUES (?,?,?,?,?)",
                 (run_id, stage, status, iso(days_ago), iso(days_ago)))
    conn.commit()


def test_load_fills_defaults_and_ignores_junk(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text("daily_at: '25:99'\nstages: {discover: 3, bogus: 1, crawl: x}\nbudget: {month_usd: {monid: 5}}\n")
    s = S.load(p)
    assert s["daily_at"] == S.DEFAULT_DAILY_AT and s["stages"]["discover"] == 3.0 and s["stages"]["crawl"] == 7
    assert "bogus" not in s["stages"] and s["budget"]["month_usd"]["monid"] == 5.0
    assert S.load(tmp_path / "missing.yaml")["stages"] == S.DEFAULT_EVERY
    assert S.load()["stages"]["discover"] == 7                        # the repo's config/schedule.yaml


def test_due_uses_the_last_successful_run_with_slack(conn):
    stage_row(conn, 1, "discover", "ok", 6.9)          # every 7: due within the 3 h slack
    stage_row(conn, 2, "expand", "ok", 3)              # every 14: not due
    stage_row(conn, 3, "crawl", "failed", 1)           # failed: never ok -> due
    stage_row(conn, 4, "score", "ok", 0.5)             # every 1: not due yet
    conn.execute("INSERT INTO runs (run_id, started_at, finished_at, stage) VALUES (5, ?, ?, 'paths,score')", (iso(2), iso(2)))
    conn.commit()
    d = {r["stage"]: r for r in S.due(conn, S.load(), NOW)}
    assert d["discover"]["due"] and not d["expand"]["due"] and d["crawl"]["due"] and not d["score"]["due"]
    assert d["crawl"]["reason"] == "never ran successfully"
    assert d["paths"]["due"] and d["paths"]["age_days"] == 2.0      # a run from before run_stages still counts
    assert [r["stage"] for r in S.due(conn, S.load(), NOW)] == S.STAGES


def test_month_spend_and_caps(conn):
    for ts, provider, usd in (("2026-10-02T00:00:00Z", "monid", 30.0), ("2026-10-10T00:00:00Z", "monid", 12.0),
                              ("2026-09-30T23:00:00Z", "monid", 99.0), ("2026-10-03T00:00:00Z", "x_api", 1.0)):
        conn.execute("INSERT INTO costs (ts, provider, endpoint, units, unit_cost_usd, cost_usd) VALUES (?,?,?,?,?,?)",
                     (ts, provider, "e", 1, usd, usd))
    conn.commit()
    assert S.month_spend(conn, NOW) == {"monid": 42.0, "x_api": 1.0}
    caps = S.capped(conn, S.load(), NOW)
    assert set(caps) == {"monid"} and "$42.00" in caps["monid"]


def test_next_run_at_is_today_or_tomorrow():
    s = {"daily_at": "06:00", "stages": S.DEFAULT_EVERY, "budget": {"month_usd": S.DEFAULT_MONTH_USD}}
    nxt = S.next_run_at(s)
    now = dt.datetime.now()
    assert nxt > now and nxt - now <= dt.timedelta(days=1) and (nxt.hour, nxt.minute) == (6, 0)


def test_daily_at_unquoted_yaml_time_is_read_as_the_time(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text("daily_at: 17:30\n")                  # YAML 1.1 sexagesimal: the integer 1050
    assert S.load(p)["daily_at"] == "17:30"
    p.write_text("daily_at: '1050'\n")
    assert S.load(p)["daily_at"] == S.DEFAULT_DAILY_AT  # not a time of day
