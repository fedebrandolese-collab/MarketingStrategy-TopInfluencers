"""The engagement sample (src/ring_wishlist/sample.py): offline, on the demo DB of scripts/seed_demo_db.py (run 2 scored, three
people on its wishlist) with the fixture source or small fakes (conftest forces DRY_RUN=1). No network, no spend."""
import dataclasses
import datetime as dt
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ring_wishlist import costs, db, discover, export, pipeline, sample, weights  # noqa: E402
from ring_wishlist.sources import monid  # noqa: E402
from ring_wishlist.sources.base import FixtureSource, XPost, persist_posts  # noqa: E402
from ring_wishlist.sources.monid import MonidSource  # noqa: E402
from ring_wishlist.sources.x_api import XApiSource  # noqa: E402
from scripts.seed_demo_db import seed  # noqa: E402

SCORED = 2   # the demo's latest scored run
WISHLIST = ["ringnerd_test", "vc_test", "techwriter_test"]   # run 2's people who meet the bar, in rank order (US only)
MEETS_BAR = WISHLIST + ["eu_test"]                            # US or not: Elke (Berlin, rank 6) meets the bar too


@pytest.fixture(autouse=True)
def caps(monkeypatch):
    monkeypatch.setenv("BUDGET_MONID_USD", "10")        # tests must not depend on the .env caps
    monkeypatch.setenv("BUDGET_X_API_USD", "100")


@pytest.fixture
def demo(tmp_path):
    seed(tmp_path / "demo.db", quiet=True)
    c = db.connect(tmp_path / "demo.db")
    yield c
    c.close()


def cids(conn):
    return {r["handle"]: r["candidate_id"] for r in db.rows(conn, "SELECT candidate_id, handle FROM candidates")}


def newest_samples(conn):
    """{x_id: the newest engagement_samples row, post_ids parsed}."""
    out = {}
    for r in db.rows(conn, "SELECT * FROM engagement_samples ORDER BY fetched_at"):
        out[r["x_id"]] = {**r, "post_ids": json.loads(r["post_ids"])}
    return out


def ago(days):
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- who is sampled

def test_selection_is_everyone_who_meets_the_bar_then_the_best_scored_people(demo):
    c = cids(demo)
    ids, counts = sample.select(demo, SCORED, top=2)       # the top 2 meet the bar already; tom (rank 5) does too
    assert ids == [c[h] for h in MEETS_BAR] and counts == {"bar": 4, "top": 2}
    # run 2 ranks rina, vera, adrian, frank, tom, elke (not US), cora (other: not a person), parody (US unknown)
    ids, counts = sample.select(demo, SCORED, top=150)
    assert ids == [c[h] for h in MEETS_BAR + ["adrianaoun_test", "founder_test", "parody_test"]]
    assert counts == {"bar": 4, "top": 7}                   # was 3 and 6: elke counts in both, US or not (30 Sep)
    demo.execute("INSERT INTO status (candidate_id, status) VALUES (?, 'rejected')", (c["founder_test"],))
    assert c["founder_test"] not in sample.select(demo, SCORED, top=150)[0]   # rejected by hand


def test_selection_covers_non_us_people_who_meet_the_bar_and_wishlist_json_stays_us_only(demo):
    """Federico, 30 Sep: the US only filter must be usable. With it off the dashboard shows Elke (Berlin, meets the bar),
    so she is sampled for engagement numbers like everyone on the list; export's wishlist.json keeps US only on."""
    c = cids(demo)
    rows = export.rows_for_run(demo, SCORED)
    elke = next(r for r in rows if r["candidate_id"] == c["eu_test"])
    assert "not_us" in elke["flags"] and weights.hidden_by(elke) == ["us_only"]        # meets the bar; US only hides her
    assert [r["candidate_id"] for r in export.wishlist_rows(rows)] == [c[h] for h in WISHLIST]   # export's default
    ids, counts = sample.select(demo, SCORED, top=0)
    assert c["eu_test"] in ids and ids == [c[h] for h in MEETS_BAR] and counts == {"bar": 4, "top": 0}


def test_run_skips_people_without_x_and_recent_samples_and_a_check_fetches_anyway(demo):
    c = cids(demo)
    x = {r["handle"]: r["x_id"] for r in db.rows(demo, "SELECT handle, x_id FROM candidates")}
    for handle, days in (("vc_test", 20), ("techwriter_test", 3)):
        demo.execute("INSERT INTO engagement_samples (x_id, fetched_at, source, requested, post_ids) VALUES (?,?,?,?,?)",
                     (x[handle], ago(days), "fixture", 30, "[]"))
    run = db.new_run(demo, "sample")        # a new run with no scores: the latest scored run (2) is used
    st = sample.run_sample(demo, run, FixtureSource(), top=150)
    # adrian has no X account; tom was sampled 3 days ago; vera's sample is 20 days old, so she is fetched again
    assert (st["bar"], st["top"], st["skipped_no_x"], st["skipped_recent"]) == (4, 7, 1, 1)   # was 3, 6: elke joins
    assert (st["selected"], st["accounts"], st["posts"], st["errors"], st["cost"]) == (5, 5, 8, 0, 0.0)   # + elke's 1
    got = newest_samples(demo)
    assert got[x["ringnerd_test"]]["post_ids"] == ["101", "102", "103"]           # newest first
    assert (got[x["vc_test"]]["run_id"], got[x["vc_test"]]["requested"], got[x["vc_test"]]["source"]) == (run, 30, "fixture")
    assert got[x["techwriter_test"]]["post_ids"] == [] and got[x["eu_test"]]["run_id"] == run   # untouched; not US, sampled
    # the next run within the refresh window buys nothing
    again = sample.run_sample(demo, db.new_run(demo, "sample"), FixtureSource(), top=150)
    assert (again["accounts"], again["skipped_recent"]) == (0, 6)
    # a manual check fetches its person whatever the refresh window says
    chk = sample.run_sample(demo, db.new_run(demo, "check"), FixtureSource(), candidate_ids=[c["techwriter_test"]])
    assert (chk["bar"], chk["top"], chk["accounts"], chk["skipped_recent"]) == (None, None, 1, 0)
    assert newest_samples(demo)[x["techwriter_test"]]["post_ids"] == ["201", "202"]


# ---------------------------------------------------------------- what is stored

class Latest:
    """Answers get_latest_originals with fixed posts, in the order given. Like x_api, it first persists them on its own
    with no query (over the stored one)."""
    name = "fixture"

    def __init__(self, conn, posts):
        self.conn, self.posts = conn, posts

    def get_latest_originals(self, handle, max_results):
        persist_posts(self.conn, [dataclasses.replace(p, query=None) for p in self.posts], "x_api")
        return [dataclasses.replace(p) for p in self.posts]


def _post(post_id, created, x_id="2", handle="ringnerd_test", **kw):
    return XPost(post_id=post_id, x_id=x_id, handle=handle, text=kw.pop("text", f"post {post_id}"),
                 created_at=created, **kw)


def test_existing_queries_are_kept_and_new_posts_get_the_sample_query(demo):
    c = cids(demo)
    topic = '"smart ring" -is:retweet lang:en'
    evidence = discover.enrich_queries("ringnerd_test")[0]
    demo.execute("UPDATE x_posts SET query=? WHERE post_id='101'", (topic,))
    demo.execute("UPDATE x_posts SET query='latest_status' WHERE post_id='102'")
    persist_posts(demo, [_post("902", "2026-09-28T10:00:00Z", in_reply_to_x_id="1", query=evidence)], "monid")
    src = Latest(demo, [
        _post("101", "2026-09-20T15:00:00Z", likes=999, views=123456),            # stored, found by a topic query
        _post("901", "2026-09-29T10:00:00Z", likes=5),                            # new
        _post("902", "2026-09-28T10:00:00Z", in_reply_to_x_id="1"),               # a stray reply (stored by enrich)
        _post("102", "2026-08-02T15:00:00Z"),                                     # stored, the crawl's latest_status
        _post("903", "2026-09-27T10:00:00Z", text="RT @someone: a repost"),       # a stray repost
        _post("904", "2026-09-26T10:00:00Z", x_id="3", handle="techwriter_test"),  # someone else's
    ])
    st = sample.run_sample(demo, db.new_run(demo, "sample"), src, candidate_ids=[c["ringnerd_test"]])
    assert (st["accounts"], st["posts"]) == (1, 3)
    assert newest_samples(demo)["2"]["post_ids"] == ["901", "101", "102"]        # newest first, originals only
    q = {r["post_id"]: r for r in db.rows(demo, "SELECT post_id, query, likes, views FROM x_posts")}
    assert (q["101"]["query"], q["102"]["query"], q["902"]["query"]) == (topic, "latest_status", evidence)
    new_query = "from:ringnerd_test -filter:retweets -filter:replies"
    assert q["901"]["query"] == sample.SAMPLE_QUERY.format(handle="ringnerd_test") == new_query
    assert discover.query_kind(new_query) == "recent" and not discover.post_is_topical(new_query, "post 901")
    assert (q["101"]["likes"], q["101"]["views"]) == (999, 123456)                # counts are refreshed


def test_a_row_even_for_zero_posts_and_none_for_a_failed_fetch(demo):
    c = cids(demo)

    class Flaky(FixtureSource):
        def get_latest_originals(self, handle, max_results):
            if handle == "vc_test":
                raise RuntimeError("provider HTTP 500")
            return [] if handle == "ringnerd_test" else super().get_latest_originals(handle, max_results)

    st = sample.run_sample(demo, db.new_run(demo, "sample"), Flaky(), candidate_ids=[c[h] for h in WISHLIST], posts=20)
    assert (st["accounts"], st["empty"], st["errors"], st["posts"]) == (2, 1, 1, 2)
    got = newest_samples(demo)
    assert (got["2"]["post_ids"], got["2"]["requested"]) == ([], 20)             # an empty account is remembered
    assert "4" not in got                                                         # failed: no row
    nxt = sample.run_sample(demo, db.new_run(demo, "sample"), FixtureSource(), top=0)
    assert nxt["skipped_recent"] == 2 and newest_samples(demo)["4"]["post_ids"] == ["301", "302"]   # retried


# ---------------------------------------------------------------- cost

class PricedFixture(FixtureSource):
    """Priced as monid (discover.estimate_cost goes by the source name): $0.0006 per post. Says when it fetches."""
    name = "monid"

    def get_latest_originals(self, handle, max_results):
        print(f"fetch @{handle}")
        return super().get_latest_originals(handle, max_results)


def test_projected_cost_is_printed_first_and_the_budget_stops_before_going_over(demo, capsys):
    ids = [cids(demo)[h] for h in WISHLIST]
    per = monid.estimate("tweets", 30)
    assert per == pytest.approx(30 * 0.0006)
    st = sample.run_sample(demo, db.new_run(demo, "sample"), PricedFixture(), candidate_ids=ids)
    out = capsys.readouterr().out.splitlines()
    assert out[0] == (f"[cost] monid sample: ~{costs.fmt(3 * per)} for 3 people x 30 latest original posts "
                      f"(~{costs.fmt(per)} each, cap $10.00)")
    assert out[1:] == [f"fetch @{h}" for h in WISHLIST] and st["accounts"] == 3 and not st["budget_stop"]
    # a stage budget for 2.5 people: the third is never fetched
    st = sample.run_sample(demo, db.new_run(demo, "sample"), PricedFixture(), candidate_ids=ids, budget_usd=per * 2.5)
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith(f"[cost] monid sample: ~{costs.fmt(2 * per)} for 2 people x 30") and len(out) == 3
    assert (st["accounts"], st["skipped_budget"], st["budget_stop"]) == (2, 1, True)


# ---------------------------------------------------------------- the adapters

class FakeResp:
    def __init__(self, status, payload):
        self.status_code, self._payload = status, payload

    def json(self):
        return self._payload


class FakeSession:
    """POST /v1/run answers 202 RUNNING, then GET /v1/runs/:id answers the completed payload (as in test_monid)."""
    def __init__(self, completed):
        self.completed, self.posts = completed, []

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append((url, json))
        return FakeResp(202, {"runId": "R1", "status": "RUNNING"})

    def get(self, url, headers=None, timeout=None):
        return FakeResp(200, self.completed)


def _tweet(i, user, x_id, created, **kw):
    return {"type": "tweet", "id": str(i), "text": kw.pop("text", f"post {i}"), "createdAt": created, "likeCount": 10,
            "retweetCount": 1, "replyCount": 2, "quoteCount": 0, "viewCount": 1000,
            "author": {"id": x_id, "userName": user, "name": user.title(), "followers": 5000}, **kw}


TWEETS = [_tweet(1, "Rina", "2", "Mon Sep 28 10:00:00 +0000 2026"),
          _tweet(2, "rina", "2", "Mon Sep 28 09:00:00 +0000 2026", isReply=True, inReplyToUserId="9"),
          _tweet(3, "rina", "2", "Mon Sep 28 08:00:00 +0000 2026", isReply=True),
          _tweet(4, "rina", "2", "Mon Sep 28 07:00:00 +0000 2026", text="RT @x: a repost"),
          _tweet(5, "rina", "2", "Mon Sep 28 06:00:00 +0000 2026", isRetweet=True),
          _tweet(6, "someone", "7", "Mon Sep 28 05:00:00 +0000 2026"),
          _tweet(7, "rina", "2", "Sun Sep 27 10:00:00 +0000 2026")]
COMPLETED = {"runId": "R1", "status": "COMPLETED", "providerResponse": {"httpStatus": 200}, "billedUnits": 7,
             "cost": {"value": 4200, "unit": "MICRO_DOLLAR"}, "output": TWEETS}


def test_monid_latest_originals_body_filter_authors_and_ledger(conn):
    s = MonidSource(conn=conn, run_id=db.new_run(conn, "test"), session=FakeSession(COMPLETED), api_key="k", poll_interval=0)
    posts = s.get_latest_originals("@Rina", 30)
    body = s.session.posts[0][1]["input"]["body"]
    assert body == {"searchTerms": ["from:rina -filter:retweets -filter:replies"], "maxItems": 30, "sort": "Latest"}
    assert [p.post_id for p in posts] == ["1", "7"]           # own originals only, newest first
    assert db.one(conn, "SELECT followers FROM x_users WHERE x_id='2'")["followers"] == 5000   # authors cached
    assert costs.spent(conn, "monid") == pytest.approx(0.0042)


def test_monid_prefetch_then_latest_originals_is_free(conn, capsys):
    s = MonidSource(conn=conn, run_id=db.new_run(conn, "test"), session=FakeSession(COMPLETED), api_key="k", poll_interval=0)
    stats = s.prefetch_latest_originals(["rina", "Someone"], 30, workers=2)
    assert (stats["fetched"], stats["failed"]) == (2, 0) and len(s.session.posts) == 2
    assert costs.spent(conn, "monid") == pytest.approx(2 * 0.0042)
    assert [p.post_id for p in s.get_latest_originals("rina", 30)] == ["1", "7"]
    assert [p.post_id for p in s.get_latest_originals("someone", 30)] == ["6"]
    assert len(s.session.posts) == 2 and "already bought by the prefetch" in capsys.readouterr().out
    s.get_latest_originals("rina", 30)                         # served once: a later call buys again
    assert len(s.session.posts) == 3


def test_run_sample_uses_the_monid_prefetch(demo):
    ids = [cids(demo)[h] for h in WISHLIST]
    output = [_tweet(90 + int(x), h, x, "Mon Sep 28 10:00:00 +0000 2026")
              for h, x in (("ringnerd_test", "2"), ("vc_test", "4"), ("techwriter_test", "3"))]
    run = db.new_run(demo, "sample")
    s = MonidSource(conn=demo, run_id=run, session=FakeSession({**COMPLETED, "output": output, "billedUnits": 3}),
                    api_key="k", poll_interval=0)
    st = sample.run_sample(demo, run, s, candidate_ids=ids)
    assert st["prefetch"]["fetched"] == 3 and len(s.session.posts) == 3        # one run each, all in the prefetch
    assert (st["accounts"], st["posts"]) == (3, 3) and st["cost"] == pytest.approx(3 * 0.0042)
    assert {x: r["post_ids"] for x, r in newest_samples(demo).items()} == {"2": ["92"], "4": ["94"], "3": ["93"]}


def test_x_api_latest_originals_is_the_timeline_without_reposts_and_replies(conn, monkeypatch):
    monkeypatch.setenv("X_BEARER_TOKEN", "test-token-not-real")
    body = json.loads((ROOT / "tests" / "samples" / "b" / "user_tweets.json").read_text())   # a post and a reply
    calls = []

    class Session:
        def request(self, method, url, params=None, headers=None, timeout=None):
            calls.append((url, dict(params or {})))
            return SimpleNamespace(status_code=200, headers={}, json=lambda: body, text="")

    posts = XApiSource(conn=conn, session=Session()).get_latest_originals("2244994945", 30)
    assert [u for u, _ in calls] == ["https://api.x.com/2/users/2244994945/tweets"]
    assert (calls[0][1]["exclude"], calls[0][1]["max_results"]) == ("retweets,replies", 30)
    assert [p.post_id for p in posts] == ["1841000000000000201"]                 # the stray reply is dropped


# ---------------------------------------------------------------- the pipeline stage

def test_pipeline_runs_the_sample_stage_between_paths_and_score(tmp_path, monkeypatch):
    assert pipeline.STAGES.index("paths") < pipeline.STAGES.index("sample") < pipeline.STAGES.index("score")
    seed(tmp_path / "demo.db", quiet=True)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "demo.db"))
    assert pipeline.main(["run", "--stage", "sample", "--sample-top", "2", "--sample-posts", "5"]) == 0
    c = db.connect(tmp_path / "demo.db")
    rows = db.rows(c, "SELECT x_id, requested, run_id FROM engagement_samples")
    c.close()
    # the wishlist's three and elke (not US, meets the bar), whatever --sample-top says
    assert sorted(r["x_id"] for r in rows) == ["2", "3", "4", "7"] and {r["requested"] for r in rows} == {5}
