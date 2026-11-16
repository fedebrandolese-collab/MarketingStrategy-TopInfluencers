"""Check an account on demand (src/ring_wishlist/check.py): offline, on the demo DB of scripts/seed_demo_db.py (runs 1 and 2
scored), with the fixture sources and the fixture LLM (conftest forces DRY_RUN=1)."""
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ring_wishlist import check, db, export, score, weights  # noqa: E402
from ring_wishlist.sources.base import FixtureSource, XUser  # noqa: E402
from app import data  # noqa: E402
from scripts.seed_demo_db import seed  # noqa: E402

SCORED = 2   # the demo's latest scored run


@pytest.fixture
def demo(tmp_path):
    seed(tmp_path / "demo.db", quiet=True)
    c = db.connect(tmp_path / "demo.db")
    yield c
    c.close()


@pytest.fixture
def table(demo):
    return data.ranked_table(demo, SCORED, precise=True)


class Spy:
    """The fixture source, counting calls. `protected` makes the lookup return a protected account."""

    def __init__(self, protected: bool = False):
        self._src, self.calls, self.name, self._protected = FixtureSource(), [], "fixture", protected

    def __getattr__(self, attr):
        fn = getattr(self._src, attr)

        def wrapped(*a, **k):
            self.calls.append(attr)
            out = fn(*a, **k)
            if attr == "get_user_by_handle" and out is not None and self._protected:
                out = XUser(**{**out.__dict__, "raw": {"protected": True}})
            return out
        return wrapped


def n_rows(conn, table_name, where="1=1", params=()):
    return db.one(conn, f"SELECT COUNT(*) AS n FROM {table_name} WHERE {where}", params)["n"]


def test_parse_handle():
    for text in ("@IcoBrando02", "icobrando02", " https://x.com/icobrando02 ", "twitter.com/icobrando02/status/1?s=20",
                 "https://mobile.twitter.com/@icobrando02", "x.com/icobrando02/"):
        assert check.parse_handle(text) == "icobrando02", text
    for bad in ("", "   ", "@", "not a handle", "https://x.com/home", "sixteen_chars_xx", "https://example.com/x"):
        assert check.parse_handle(bad) is None, bad


def test_cost_estimate_is_itemised_from_the_adapters_estimators(demo, monkeypatch):
    from ring_wishlist.sources import monid, x_api
    for k in ("SOURCE_BULK", "SOURCE_VERIFY", "LLM_BACKEND"):
        monkeypatch.delenv(k, raising=False)
    est = check.estimate(demo, "icobrando02", live=True)
    look, ev, recent, smp, cls = est["items"]
    assert (look["step"], look["provider"], look["usd"]) == ("lookup", "x_api", x_api.estimate("users/by/username", 1))
    assert look["usd"] == pytest.approx(0.010)
    assert [(i["provider"], i["units"]) for i in (ev, recent)] == [("monid", 20), ("monid", 10)]   # run_enrich's split of 30
    assert ev["usd"] + recent["usd"] == pytest.approx(monid.estimate("tweets", 30))
    # the engagement sample: 30 latest original posts x $0.0006
    assert (smp["step"], smp["provider"], smp["units"]) == ("sample", "monid", 30)
    assert smp["usd"] == pytest.approx(monid.estimate("tweets", 30)) == pytest.approx(30 * 0.0006)
    assert (cls["step"], cls["provider"], cls["usd"]) == ("classify", "claude_cli", 0.0)
    assert "Claude account usage" in cls["note"]
    assert est["total_usd"] == pytest.approx(0.010 + monid.estimate("tweets", 60)) and est["total_usd"] < 0.05
    # a profile the official adapter bought within its cache window is served free
    demo.execute("INSERT INTO x_users (x_id, handle, source, fetched_at) VALUES ('99','icobrando02','x_api',?)", (db.utcnow(),))
    assert check.estimate(demo, "icobrando02", live=True)["items"][0]["usd"] == 0.0
    # DRY_RUN: the fixtures bill nothing, and the live price is still shown
    dry = check.estimate(demo, "icobrando02")
    assert dry["dry_run"] and dry["total_usd"] == 0.0 and dry["live_total_usd"] > 0


def test_full_check_on_a_fixture_handle(demo, table):
    before = {t: n_rows(demo, t) for t in ("scores", "paths", "changes")}
    bulk, verify, said = Spy(), Spy(), []
    ans = check.run_check(demo, "@Carlo_Test", table=table, sources=(bulk, verify), say=said.append)
    assert ans["ok"], ans.get("reason")
    assert verify.calls == ["get_user_by_handle"] and "search_posts" in bulk.calls
    assert bulk.calls.index("get_latest_originals") > bulk.calls.index("search_posts")   # the sample right after enrich
    cand = db.one(demo, "SELECT * FROM candidates WHERE handle='carlo_test'")
    # its engagement sample: a row even though the fixtures hold no post of @carlo_test
    smp = db.rows(demo, "SELECT * FROM engagement_samples WHERE x_id=?", (cand["x_id"],))
    assert [(r["run_id"], r["requested"], json.loads(r["post_ids"])) for r in smp] == [(ans["run_id"], check.SAMPLE_POSTS, [])]
    assert ans["steps"]["sample"] == {"posts": 0, "errors": 0, "budget_stop": False}
    assert cand["candidate_id"] == ans["candidate_id"] and cand["discovered_via"] == "manual"
    assert f"checked on demand: {db.utcnow()[:10]}" in cand["notes"] and "enriched:" in cand["notes"]
    assert cand["handle_verified"] in ("verified", "likely_real")
    run = db.one(demo, "SELECT * FROM runs WHERE run_id=?", (ans["run_id"],))
    assert run["stage"] == "check" and run["finished_at"]
    assert n_rows(demo, "classifications", "run_id=? AND candidate_id=?", (ans["run_id"], cand["candidate_id"])) == 1
    # nothing scored, no paths, no change log: the check never becomes the run anything compares against
    assert {t: n_rows(demo, t) for t in ("scores", "paths", "changes")} == before
    assert db.latest_run_id(demo, with_scores=True) == SCORED and data.latest_scored_run(demo)["run_id"] == SCORED
    later = db.new_run(demo, "export")   # an export-only run after the check still takes run 2's scores and paths
    assert export._pick_run(demo, "scores", later) == SCORED and export._pick_run(demo, "paths", later) == SCORED
    # the answer: rank among run 2's people under the default weights and filters, neighbours, the three chances
    rv = ans["rank_at_check"]
    others = weights.filter_rows(table, weights.DEFAULT_FILTERS)
    assert rv["n"] == len(others) + 1 and 1 <= rv["rank"] <= rv["n"] and rv["rank"] == rv["rank_default"]
    assert (rv["above"] or rv["below"]) and not rv["weights_differ"]
    assert [b["field"] for b in ans["because"]] == ["p_accept", "p_wear", "p_love"]
    assert all(b["note"] for b in ans["because"]) and ans["moves"]
    assert ans["person"]["total"] == pytest.approx(score.score_candidate(demo, cand["candidate_id"], SCORED)["total"], abs=1.0)
    # the checks row
    row = db.one(demo, "SELECT * FROM checks WHERE check_id=?", (ans["check_id"],))
    assert (row["handle"], row["candidate_id"], row["run_id"], row["compared_run_id"]) == \
           ("carlo_test", cand["candidate_id"], ans["run_id"], SCORED)
    assert (row["rank_current"], row["rank_default"], row["n"]) == (rv["rank"], rv["rank_default"], rv["n"])
    assert row["cost_usd"] == 0.0 and json.loads(row["result_json"])["handle"] == "carlo_test"
    assert any("Found @carlo_test" in m for m in said)
    # an ordinary candidate from then on: the next scheduled scoring includes them
    nxt = db.new_run(demo, "score")
    score.run_score(demo, nxt)
    assert n_rows(demo, "scores", "run_id=? AND candidate_id=?", (nxt, cand["candidate_id"])) == 1


@pytest.mark.parametrize("protected", [False, True])
def test_missing_or_protected_handle_stops_after_the_lookup(demo, table, protected):
    bulk, verify = Spy(), Spy(protected=protected)
    handle = "carlo_test" if protected else "nobody_test"
    n_cands = n_rows(demo, "candidates")
    ans = check.run_check(demo, handle, table=table, sources=(bulk, verify))
    assert not ans["ok"] and ("protected" if protected else "no active account") in ans["reason"]
    assert verify.calls == ["get_user_by_handle"] and bulk.calls == []
    assert n_rows(demo, "candidates") == n_cands and n_rows(demo, "classifications", "run_id=?", (ans["run_id"],)) == 0
    row = db.one(demo, "SELECT * FROM checks WHERE check_id=?", (ans["check_id"],))
    assert row["candidate_id"] is None and row["rank_current"] is None and row["handle"] == handle
    assert db.one(demo, "SELECT stage, finished_at FROM runs WHERE run_id=?", (ans["run_id"],))["finished_at"]


def _row(cid, handle, pa, pw, pl, impact=0.5, category="x_voice", flags=(), status="candidate", wear=True, contactable=True):
    return {"candidate_id": cid, "handle": handle, "name": handle.title(), "p_accept": pa, "p_wear": pw, "p_love": pl,
            "impact": impact, "reach": 0.05, "affinity": None, "confidence": 0.5, "category": category,
            "flags": list(flags), "status": status, "wear_evidence": wear, "contactable": contactable}


def test_rank_and_neighbours_under_default_and_custom_weights():
    table = pd.DataFrame([_row(1, "a", .9, .9, .9), _row(2, "b", .5, .5, .5), _row(3, "c", .3, .3, .3),
                          _row(4, "d", .5, .9, .5, category="vc"), _row(5, "e", .9, .9, .9, flags=["not_us"])])
    me = _row(9, "me", .6, .2, .6)   # 4.68 points: a 47.4, d 14.6, b 8.1, me, c 1.8 (e is not US: filtered out)
    rv = check.rank_view(table, me)
    assert (rv["rank"], rv["n"], rv["rank_default"]) == (4, 5, 4)
    assert (rv["above"]["handle"], rv["above"]["rank"], rv["below"]["handle"], rv["below"]["rank"]) == ("b", 3, "c", 5)
    assert rv["hidden_by"] == [] and rv["in_list"] and not rv["weights_differ"]
    # wear left out: me 23.4 is second behind a; b and d tie at 16.25 (b first on candidate_id); default rank unchanged
    custom = {**weights.DEFAULT_WEIGHTS, "wear": 0.0}
    rv = check.rank_view(table, me, custom, baseline=weights.DEFAULT_WEIGHTS)
    assert (rv["rank"], rv["rank_default"], rv["above"]["handle"], rv["below"]["handle"]) == (2, 4, "a", "b")
    assert rv["weights_differ"]
    # a category weight counts too: VCs at 0 send d to the bottom, me moves up to third
    rv = check.rank_view(table, me, {**weights.DEFAULT_WEIGHTS, "vc": 0.0})
    assert (rv["rank"], rv["above"]["handle"], rv["below"]["handle"]) == (3, "b", "c")
    # filtered out (not US): said so, and still placed among the list's people
    rv = check.rank_view(table, {**me, "flags": ["not_us"]})
    assert rv["hidden_by"] == ["us_only"] and (rv["rank"], rv["n"]) == (4, 5) and not rv["in_list"]
    rv = check.rank_view(table, {**me, "status": "rejected"}, filters={"us_only": False})
    assert rv["hidden_by"] == ["hide_rejected"] and rv["n"] == 6          # e is back once US only is off
    # below the brief's bar: said which part, and still placed among the list's people (the same filter as the list)
    rv = check.rank_view(table, {**me, "category": "other"})
    assert rv["hidden_by"] == ["bar"] and (rv["rank"], rv["n"]) == (4, 5) and not rv["in_list"]
    rv = check.rank_view(table, {**me, "wear_evidence": False, "flags": ["not_us"]})
    assert rv["hidden_by"] == ["us_only", "bar"] and rv["filters"]["bar"] is True
    no_ev = pd.DataFrame([_row(6, "f", .99, .99, .99, wear=False)])   # a big name without evidence never counts above
    assert check.rank_view(pd.concat([table, no_ev], ignore_index=True), me)["rank"] == 4
    assert check.rank_view(pd.concat([table, no_ev], ignore_index=True), me, filters={"bar": False})["rank"] == 5
    assert check.with_bar({"category": "vc"}, [{"field": "p_wear", "value": 0.2, "prior": 0.25, "backed": True}]) == \
        {"category": "vc", "wear": "against", "wear_evidence": False, "bar_reason": weights.BAR_AGAINST}   # saved before the bar
    # their own row in the table is replaced, not counted twice
    assert check.rank_view(table, {**me, "candidate_id": 2})["n"] == 4
    # a small list: outside the top n
    assert not check.rank_view(table, me, filters={"n": 3})["in_list"]


def test_free_show_makes_no_source_or_llm_call(demo, table, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the free Show must not call a source or the LLM")
    monkeypatch.setattr("ring_wishlist.pipeline._sources", boom)
    monkeypatch.setattr("ring_wishlist.sources.base.get_source", boom)
    monkeypatch.setattr("ring_wishlist.llm.complete_json", boom)
    demo.execute("UPDATE x_posts SET fetched_at=? WHERE x_id='2'", (db.utcnow(),))   # recent posts for @ringnerd_test
    k = check.known(demo, "ringnerd_test")
    assert k["fresh"] and k["posts"] > 0 and k["classified_at"]
    runs, ledger, scores = n_rows(demo, "runs"), n_rows(demo, "costs"), n_rows(demo, "scores")
    ans = check.show_stored(demo, "ringnerd_test", table=table)
    assert ans["ok"] and ans["mode"] == "stored" and ans["run_id"] is None and ans["cost_usd"] == 0.0
    assert (n_rows(demo, "runs"), n_rows(demo, "costs"), n_rows(demo, "scores")) == (runs, ledger, scores)
    assert ans["rank_at_check"]["rank"] == 1 and ans["because"][0]["quote"]["url"].startswith("https://x.com/")
    assert check.load_check(demo, ans["check_id"])["person"]["handle"] == "ringnerd_test"
    assert check.past_checks(demo)[0]["check_id"] == ans["check_id"]
    assert check.known(demo, "nobody_test") is None
    with pytest.raises(ValueError):
        check.show_stored(demo, "nobody_test", table=table)


def test_enrich_hook_fetches_the_checked_person_past_the_gates(demo):
    parody = db.one(demo, "SELECT candidate_id FROM candidates WHERE handle='parody_test'")["candidate_id"]
    run = db.new_run(demo, "check")
    from ring_wishlist.discover import run_enrich
    assert "parody_test" not in run_enrich(demo, run, FixtureSource(), top=80, posts_per_user=30)["handles"]   # gated
    got = run_enrich(demo, run, FixtureSource(), top=1, posts_per_user=30, candidate_ids=[parody])
    assert got["handles"] == ["parody_test"] and got["enriched"] == 1


def test_keep_off_the_wishlist_hides_them_and_says_why(demo, table):
    ans = check.run_check(demo, "carlo_test", table=table, sources=(Spy(), Spy()))
    data.set_status(demo, ans["candidate_id"], "rejected", note=check.KEEP_OFF_NOTE)
    person = {**ans["person"], "status": data.get_status(demo, ans["candidate_id"])["status"]}
    rv = check.rank_view(table, person)
    assert rv["hidden_by"][0] == "hide_rejected" and rv["rank"] == ans["rank_at_check"]["rank"]
    assert ("bar" in rv["hidden_by"]) == bool(person["bar_reason"])   # the bar, when they fail it, is said too
    assert data.get_status(demo, ans["candidate_id"])["note"] == "manual check: not for the list"
