import pytest

from ring_wishlist import costs, db, schedule
from ring_wishlist.sources.base import persist_follows, persist_posts, persist_users


def test_schema_and_run(conn):
    run_id = db.new_run(conn, "all", "test")
    assert run_id >= 1
    db.finish_run(conn, run_id)
    assert db.one(conn, "SELECT finished_at FROM runs WHERE run_id=?", (run_id,))["finished_at"]


def test_fixture_source_and_persistence(conn, fixture_source):
    carlo = fixture_source.get_user_by_handle("@Carlo_Test")
    assert carlo and carlo.x_id == "1"
    following = fixture_source.get_following("carlo_test")
    assert {u.handle for u in following} >= {"ringnerd_test", "vc_test"}
    persist_follows(conn, carlo.x_id, following, "following", "fixture")
    assert db.one(conn, "SELECT COUNT(*) AS n FROM x_follows WHERE src_x_id='1'")["n"] == len(following)
    posts = fixture_source.search_posts('"smart ring" -is:retweet lang:en')
    assert posts and all("smart ring" in p.text.lower() for p in posts)
    persist_posts(conn, posts, "fixture")
    persist_users(conn, [carlo], "fixture")
    assert db.one(conn, "SELECT handle FROM x_users WHERE x_id='1'")["handle"] == "carlo_test"


def test_budget_guard(conn, monkeypatch):
    monkeypatch.setenv("BUDGET_X_API_USD", "0.05")
    run_id = db.new_run(conn, "crawl")
    costs.guard(conn, "x_api", 0.04, run_id)
    costs.record(conn, "x_api", "users/search", 4, costs.X_PRICES["user_read"], run_id)
    try:
        costs.guard(conn, "x_api", 0.02, run_id)
        assert False, "should have raised"
    except costs.BudgetExceeded:
        pass


def month_caps(tmp_path, monkeypatch, **caps):
    p = tmp_path / "schedule.yaml"
    p.write_text("budget:\n  month_usd:\n" + "".join(f"    {k}: {v}\n" for k, v in caps.items()))
    monkeypatch.setattr(schedule, "SCHEDULE_FILE", p)
    monkeypatch.delenv("BUDGET_IGNORE_MONTH", raising=False)


def test_budget_guard_monthly_cap(conn, monkeypatch, tmp_path):
    month_caps(tmp_path, monkeypatch, monid=5)
    monkeypatch.setenv("BUDGET_MONID_USD", "100")                    # the per-run cap is not the one that stops it
    costs.record(conn, "monid", "search", 1, 4.5, db.new_run(conn, "enrich"))          # an earlier run this month
    conn.execute("INSERT INTO costs (ts, provider, endpoint, units, unit_cost_usd, cost_usd) "
                 "VALUES ('2020-01-05T00:00:00Z', 'monid', 'search', 1, 99, 99)")        # another month: not counted
    run_id = db.new_run(conn, "enrich")
    costs.guard(conn, "monid", 0.5, run_id)                          # 4.50 + 0.50: exactly the cap
    with pytest.raises(costs.BudgetExceeded, match=r"spent \$4.50 this month.*monthly cap is \$5.00.*config/schedule.yaml"):
        costs.guard(conn, "monid", 0.6, run_id)
    monkeypatch.setenv("BUDGET_OTHER_USD", "100")
    costs.guard(conn, "other", 50.0, run_id)                         # a provider without a monthly cap
    monkeypatch.setenv("BUDGET_IGNORE_MONTH", "1")                   # a deliberate manual run
    costs.guard(conn, "monid", 0.6, run_id)


def test_monthly_cap_holds_in_classify_worker_threads(conn, monkeypatch, tmp_path):
    from ring_wishlist import classify, llm
    month_caps(tmp_path, monkeypatch, anthropic=2)
    monkeypatch.setenv("BUDGET_ANTHROPIC_USD", "100")
    costs.record(conn, llm.PROVIDER, "messages", 1, 1.5, db.new_run(conn, "classify"))
    run_id = db.new_run(conn, "classify")
    relay = classify._CostRelay(conn, run_id)                        # what llm.py gets in a classify worker thread
    costs.guard(relay, llm.PROVIDER, 0.2, run_id)
    costs.record(relay, llm.PROVIDER, "messages", 1, 0.2, run_id)    # relayed, not yet written
    with pytest.raises(costs.BudgetExceeded, match="monthly cap"):
        costs.guard(relay, llm.PROVIDER, 0.4, run_id)                # 1.5 + 0.2 relayed + 0.4 > 2
    assert relay.flush(conn) == 1


def test_map_fixtures(map_conn):
    assert db.one(map_conn, "SELECT COUNT(*) AS n FROM map_people")["n"] >= 8
    assert db.one(map_conn, "SELECT COUNT(*) AS n FROM map_competitor_funds")["n"] == 12


def test_open_dms_are_sticky_once_seen(conn):
    from ring_wishlist.sources.base import XUser, persist_users
    u = lambda dm: XUser(x_id="7", handle="dm_test", can_dm=dm)   # noqa: E731
    persist_users(conn, [u(True)], "monid")
    persist_users(conn, [u(False)], "monid")          # another scraper account read the same author as closed
    assert db.one(conn, "SELECT can_dm FROM x_users WHERE x_id='7'")["can_dm"] == 1
    persist_users(conn, [XUser(x_id="8", handle="closed_test", can_dm=False)], "monid")
    persist_users(conn, [XUser(x_id="8", handle="closed_test", can_dm=None)], "x_api")
    assert db.one(conn, "SELECT can_dm FROM x_users WHERE x_id='8'")["can_dm"] == 0
