"""Workstream E: scoring + change log. Synthetic rows only, offline (DRY_RUN=1 via conftest)."""
import datetime as dt
import sys
import types

import pytest

from wearable_wishlist import db, score, weights

NOW = dt.datetime.now(dt.timezone.utc)


def iso(days_ago: float = 0.0) -> str:
    return (NOW - dt.timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def day(days_ago: float = 0.0) -> str:
    return (NOW - dt.timedelta(days=days_ago)).strftime("%Y-%m-%d")


CLS_VOICE = {"category": "x_voice", "us_based": True, "audience_relevance": 0.9,
             "handle_authenticity": {"value": "verified", "reasons": "blue verified since 2015"},
             "summary": "Wearables reviewer who already wears two wearables.", "unknowns": ["whether they accept free devices"]}
CLS_VC = {"category": "vc", "us_based": True, "audience_relevance": 0.7,
          "handle_authenticity": {"value": "likely_real", "reasons": "bio links to the fund site"}, "summary": "Consumer hardware VC.", "unknowns": []}
CLS_MEDIA = {"category": "media", "us_based": False, "us_based_evidence": "location says Berlin", "audience_relevance": 0.6,
             "handle_authenticity": {"value": "unknown", "reasons": "no signal either way"},
             "competitor_tie": {"value": True, "which": "Oura", "kind": "employee", "evidence": []}, "summary": "Editor at a gadget site.", "unknowns": []}
CLS_FOUNDER = {"category": "founder_angel", "us_based": True, "audience_relevance": 0.5,
               "handle_authenticity": {"value": "likely_real", "reasons": "old account"}, "summary": "Founder, angel.", "unknowns": []}


def add_user(conn, x_id, handle, followers, bio="", verified=1, can_dm=1, created="2015-01-01T00:00:00Z"):
    db.upsert(conn, "x_users", {"x_id": x_id, "handle": handle, "name": handle, "bio": bio, "verified": verified, "created_at": created,
                                "followers": followers, "can_dm": can_dm, "source": "test", "fetched_at": iso(3)}, keys=("x_id",))


def add_candidate(conn, x_id, handle, name, category, us_based=1, handle_verified="verified", competitor_flag=0,
                  competitor_detail=None, map_person_id=None) -> int:
    cur = conn.execute(
        "INSERT INTO candidates (x_id, handle, name, category, us_based, map_person_id, handle_verified, competitor_flag, competitor_detail,"
        " discovered_via, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (x_id, handle, name, category, us_based, map_person_id, handle_verified, competitor_flag, competitor_detail, "manual", iso(3), iso(3)))
    return int(cur.lastrowid)


def add_evidence(conn, cid, supports, polarity=1, weight=1.0, text="", kind="post", url=None, fetched_days_ago=3.0, date=None) -> int:
    cur = conn.execute(
        "INSERT INTO evidence (candidate_id, kind, supports, polarity, weight, url, date, text, source, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (cid, kind, supports, polarity, weight, url or f"https://x.com/c{cid}/status/{abs(hash(text)) % 10**6}", date or day(10), text, "test",
         iso(fetched_days_ago)))
    return int(cur.lastrowid)


def add_event(conn, cid, kind, days_ago, text="", url=None, fetched_days_ago=3.0) -> int:
    cur = conn.execute("INSERT INTO events (candidate_id, kind, date, url, text, source, fetched_at) VALUES (?,?,?,?,?,?,?)",
                       (cid, kind, day(days_ago), url or f"https://x.com/c{cid}/status/{kind}", text, "test", iso(fetched_days_ago)))
    return int(cur.lastrowid)


def add_post(conn, post_id, x_id, likes=400, reposts=60, replies=30, quotes=5):
    db.upsert(conn, "x_posts", {"post_id": post_id, "x_id": x_id, "created_at": iso(10), "text": "a post", "likes": likes, "reposts": reposts,
                                "replies": replies, "quotes": quotes, "source": "test", "fetched_at": iso(3)}, keys=("post_id",))


@pytest.fixture(autouse=True)
def affinity(monkeypatch):
    """score.py imports topic_affinity lazily from wearable_wishlist.affinity (workstream AF). Every test here gets a fake module instead:
    by default it knows nobody (computed affinity unknown), so these tests do not depend on AF's implementation.
    A test sets `affinity.data[candidate_id] = aff(...)` to control the computed values; `affinity.calls` records the calls."""
    fake = types.SimpleNamespace(data={}, calls=[])

    def topic_affinity(conn, candidate_ids=None, now=None, window_days=90):
        fake.calls.append({"candidate_ids": candidate_ids, "now": now, "window_days": window_days})
        return {cid: v for cid, v in fake.data.items() if candidate_ids is None or cid in candidate_ids}

    mod = types.ModuleType("wearable_wishlist.affinity")
    mod.topic_affinity = topic_affinity
    monkeypatch.setitem(sys.modules, "wearable_wishlist.affinity", mod)
    return fake


@pytest.fixture(autouse=True)
def no_competitor_ties(monkeypatch, tmp_path):
    """The repo's config/competitor_ties.yaml must not leak into these tests: a missing file means no extra ties."""
    monkeypatch.setattr(score, "COMPETITOR_TIES_FILE", tmp_path / "missing_competitor_ties.yaml")


@pytest.fixture(autouse=True)
def no_weights_file(monkeypatch, tmp_path):
    """A `default:` saved in config/weights.yaml must not leak into these tests: a missing file means built-in weights."""
    monkeypatch.setattr(score, "WEIGHTS_FILE", tmp_path / "missing_weights.yaml")


def test_missing_weights_file_keeps_todays_totals(conn):
    run1, _, _ = seed(conn)
    score.run_score(conn, run1)
    for cid, r in scores_of(conn, run1).items():
        assert r["total"] == round(100 * r["p_accept"] * r["p_wear"] * r["p_love"] * (0.3 + 0.7 * r["impact"]), 2)
        assert r["total"] == weights.weighted_total(dict(r, category=db.jloads(r["reasons_json"])["category"]))
        f = score.explain(conn, run1, cid)["per_factor"]["total"]
        assert f["reasons"][0].startswith("total = 100 x ") and f["reasons"][0].endswith("(reach is not in the total).")


def test_weights_default_reweights_the_scheduled_total(conn, tmp_path, monkeypatch):
    p = tmp_path / "weights.yaml"
    weights.save_preset("the operator live", {"impact": 2.0, "media": 0.0, "connection": 1.0}, as_default=True, path=p)
    monkeypatch.setattr(score, "WEIGHTS_FILE", p)
    run1, ids, _ = seed(conn)
    score.run_score(conn, run1)
    s = scores_of(conn, run1)
    w = weights.load_file(p)["default"]
    for cid, r in s.items():
        cat = db.jloads(r["reasons_json"])["category"]
        assert r["total"] == weights.weighted_total(dict(r, category=cat), w)
        assert (r["total"] == 0) == (cat == "media")
    reason = score.explain(conn, run1, ids["a"])["per_factor"]["total"]["reasons"][0]
    assert "(0.3 + 0.7 x" in reason and ")^2" in reason and "(0.25 + 0.75 x reach 0.70)" in reason
    assert "config/weights.yaml default 'the operator live'" in reason and "reach is not in the total" not in reason
    # the path now counts, so A (via path, reach 0.7) leads B (no path) by more than the p_accept nudge alone
    assert s[ids["a"]]["total"] / s[ids["b"]]["total"] > s[ids["a"]]["p_accept"] / s[ids["b"]]["p_accept"] * 1.5


def aff(value, topical=0, topic_eng=None, top=(), reason="Keeps explaining personal agents; replies follow.", last=None):
    """A topic_affinity() entry in the contract's shape (docs/ARCHITECTURE.md, Topic affinity)."""
    return {"score": value, "topical_posts": topical, "agent_posts": topical, "weighted_posts": float(topical),
            "topic_eng_avg": topic_eng, "overall_eng_avg": None, "lift": None, "replies_per_topic_post": None,
            "last_topical": last or (top[0][1] if top else None),
            "top_posts": [{"url": u, "date": d, "text": t, "engagement": 120, "replies": 30} for (u, d, t) in top],
            "components": {"volume": 0.5, "community": 0.5, "recency": 0.5}, "reason": reason}


def with_topic_voice(cls, value, topics=("personal_agents",)):
    return dict(cls, topic_voice={"value": value, "topics": list(topics), "evidence": []})


def add_path(conn, run_id, cid, how, strength, via_name=None, rank=1):
    conn.execute("INSERT INTO paths (run_id, candidate_id, rank, how, strength, via_name, you_rel, target_rel) VALUES (?,?,?,?,?,?,?,?)",
                 (run_id, cid, rank, how, strength, via_name, "met · warm", "introduced them (map)"))


def add_classification(conn, cid, run_id, cls):
    db.upsert(conn, "classifications", {"candidate_id": cid, "run_id": run_id, "json": db.jdumps(cls), "model": "fixture", "created_at": iso(3)},
              keys=("candidate_id", "run_id"))


def seed(conn):
    """Four candidates: two identical X voices (A has a path, B has none), a VC at a fund that backs Sandbar, a flagged media person."""
    run1 = db.new_run(conn, "score", "test run 1")
    add_user(conn, "10", "alpha", 48000, bio="Reviewing wearables since 2019.")
    add_user(conn, "11", "bravo", 48000, bio="Reviewing wearables since 2019.")
    add_user(conn, "12", "charlie_vc", 30000, bio="Partner at Lux Capital. Consumer hardware, voice, agents.")
    add_user(conn, "13", "delta_media", 120000, bio="Senior editor. Product lead @ouraring before that.", verified=0, created="2026-08-01T00:00:00Z")
    a = add_candidate(conn, "10", "alpha", "Alpha Voice", "x_voice")
    b = add_candidate(conn, "11", "bravo", "Bravo Voice", "x_voice")
    c = add_candidate(conn, "12", "charlie_vc", "Charlie Capital", "vc", handle_verified="likely_real")
    d = add_candidate(conn, "13", "delta_media", "Delta Media", "media", us_based=0, handle_verified=None, competitor_flag=1,
                      competitor_detail="employee at Oura")
    ev = {}
    for cid in (a, b):
        ev[cid] = [
            add_evidence(conn, cid, "wear", 1, 0.8, "Unboxing smart wearable number three", url=f"https://x.com/c{cid}/status/1", date=day(12)),
            add_evidence(conn, cid, "love", 1, 0.6, "Wearables win on that, the pendant lost", url=f"https://x.com/c{cid}/status/2", date=day(40)),
            add_evidence(conn, cid, "love", -1, 0.5, "Returned the pendant after a week", url=f"https://x.com/c{cid}/status/3", date=day(60)),
            add_evidence(conn, cid, "impact", 1, 0.9, "audience relevance 0.9", kind="profile_signal", url=f"https://x.com/c{cid}"),
        ]
        add_event(conn, cid, "wearable_unboxing", days_ago=5, text="Unboxing the new wearable", url=f"https://x.com/c{cid}/status/1")
    add_post(conn, "p1", "10")
    add_post(conn, "p2", "11")
    add_post(conn, "p3", "12", likes=1500, reposts=250, replies=200, quotes=50)
    ev[c] = [add_evidence(conn, c, "wear", 1, 0.7, "Wears a wearable, talks to her AI", kind="bio", url="https://x.com/charlie_vc")]
    ev[d] = [add_evidence(conn, d, "competitor", 1, 1.0, "Product lead @ouraring", kind="bio", url="https://x.com/delta_media"),
             add_evidence(conn, d, "love", 1, 0.8, "Reviewed the Humane Pin successor", kind="review", url="https://x.com/delta_media/status/9")]
    for cid, cls in ((a, CLS_VOICE), (b, CLS_VOICE), (c, CLS_VC), (d, CLS_MEDIA)):
        add_classification(conn, cid, run1, cls)
    db.upsert(conn, "map_competitor_funds", {"fund": "Lux Capital", "competitor": "Sandbar", "round": "small investor, Mar 2026"},
              keys=("fund", "competitor"))
    add_path(conn, run1, a, "via", 0.7, via_name="Marwan Refaat")
    conn.commit()
    return run1, {"a": a, "b": b, "c": c, "d": d}, ev


def scores_of(conn, run_id):
    return {r["candidate_id"]: dict(r, flags=db.jloads(r["flags"], [])) for r in db.rows(conn, "SELECT * FROM scores WHERE run_id=?", (run_id,))}


def changes_of(conn, run_id, cid=None):
    if cid is None:
        return db.rows(conn, "SELECT * FROM changes WHERE run_id=? ORDER BY change_id", (run_id,))
    return db.rows(conn, "SELECT * FROM changes WHERE run_id=? AND candidate_id=? ORDER BY change_id", (run_id, cid))


# ---------------------------------------------------------------- tests

def test_first_run_scores_ranks_paths_and_flags(conn):
    run1, ids, _ = seed(conn)
    counts = score.run_score(conn, run1)
    assert counts["scored"] == 4 and counts["new_candidates"] == 4 and counts["previous_run_id"] is None
    s = scores_of(conn, run1)
    assert len(s) == 4
    for r in s.values():
        assert 0.0 <= r["total"] <= 100.0
        for k in ("p_accept", "p_wear", "p_love", "impact", "reach", "confidence"):
            assert 0.0 <= r[k] <= 1.0, k
    assert sorted(r["rank"] for r in s.values()) == [1, 2, 3, 4]
    a, b, c, d = (s[ids[k]] for k in "abcd")
    # identical people, only the path differs: reach is information, not in the total; A's real (via) relationship only
    # nudges p_accept by WARM_ACCEPT x strength, and B's missing path costs nothing and is not a flag
    assert (a["p_wear"], a["p_love"], a["impact"]) == (b["p_wear"], b["p_love"], b["impact"])
    assert a["reach"] == 0.7 and b["reach"] == 0.05
    assert score._logit(a["p_accept"]) - score._logit(b["p_accept"]) == pytest.approx(score.WARM_ACCEPT * 0.7, abs=0.01)
    assert b["total"] == round(100 * b["p_accept"] * b["p_wear"] * b["p_love"] * (0.3 + 0.7 * b["impact"]), 2)
    assert a["total"] > b["total"] and a["rank"] < b["rank"]
    assert "no_path" not in a["flags"] + b["flags"]
    ex_b = score.explain(conn, run1, ids["b"])
    assert ex_b["per_factor"]["reach"]["no_path"] and any("does not lower the score" in r for r in ex_b["per_factor"]["reach"]["reasons"])
    assert any(r.startswith("Warm relationship (via path through Marwan Refaat, strength 0.70)")
               for r in score.explain(conn, run1, ids["a"])["per_factor"]["p_accept"]["reasons"])
    # evidence moved the probabilities off the priors, with mixed polarity on love
    assert a["p_wear"] > score.PRIORS["x_voice"]["wear"]
    assert a["confidence"] == round(2 / 3, 3)   # wear and love backed, accept prior only
    # VC at a fund that backs Sandbar: flagged with fund and round as detail, still scored
    assert "competitor:Sandbar" in c["flags"] and c["total"] > 0
    ex = score.explain(conn, run1, ids["c"])
    assert any("Lux Capital" in r and "Sandbar" in r for r in ex["reasons"])
    assert ex["per_factor"]["flags"]["details"][0]["round"] == "small investor, Mar 2026"
    # media person tied to Oura, not US, unverified handle: flagged on all three, still scored and ranked
    assert {"competitor:Oura", "not_us", "unverified_handle"} <= set(d["flags"]) and d["total"] > 0
    assert d["p_accept"] < score.PRIORS["media"]["accept"]   # competitor evidence lowers accept
    # first run: every candidate is new
    assert {r["field"] for r in changes_of(conn, run1)} == {"new_candidate"}


def test_second_run_writes_changes_with_reasons(conn):
    run1, ids, _ = seed(conn)
    score.run_score(conn, run1)
    conn.execute("UPDATE runs SET started_at=?, finished_at=? WHERE run_id=?", (iso(2), iso(1.9), run1))
    before = scores_of(conn, run1)
    # between runs: B posts an unboxing (new evidence + event), C raises a fund, E joins the list
    add_evidence(conn, ids["b"], "wear", 1, 1.0, "Unboxing the Oura Wearable 5 on day one", url="https://x.com/bravo/status/77",
                 fetched_days_ago=0.0, date=day(1))
    add_event(conn, ids["b"], "posted_about_product", days_ago=1, text="wearable_wishlist's wearable looks like the one I asked for", fetched_days_ago=0.0)
    add_event(conn, ids["c"], "fund_raised", days_ago=1, text="We just closed Fund IV", fetched_days_ago=0.0)
    add_user(conn, "14", "echo", 15000, bio="Founder. Angel in 30 companies.")
    e = add_candidate(conn, "14", "echo", "Echo Founder", "founder_angel", handle_verified="likely_real")
    run2 = db.new_run(conn, "score", "test run 2")
    add_classification(conn, e, run2, CLS_FOUNDER)
    conn.commit()

    counts = score.run_score(conn, run2)
    assert counts["scored"] == 5 and counts["previous_run_id"] == run1 and counts["new_candidates"] == 1
    after = scores_of(conn, run2)
    assert sorted(r["rank"] for r in after.values()) == [1, 2, 3, 4, 5]
    b0, b1 = before[ids["b"]], after[ids["b"]]
    assert b1["p_wear"] - b0["p_wear"] >= 0.05 and b1["p_love"] > b0["p_love"] and b1["p_accept"] > b0["p_accept"]

    ch_b = changes_of(conn, run2, ids["b"])
    fields_b = {r["field"] for r in ch_b}
    assert {"p_wear", "total"} <= fields_b
    wear_row = next(r for r in ch_b if r["field"] == "p_wear")
    assert "Unboxing the Oura Wearable 5" in wear_row["reason"] and "#" in wear_row["reason"]
    love_row = next(r for r in ch_b if r["field"] == "p_love")
    assert "posted about wearable_wishlist" in love_row["reason"]
    assert float(wear_row["old_value"]) == b0["p_wear"] and float(wear_row["new_value"]) == b1["p_wear"]
    # C: fund raised -> accept and impact move, reason names the event
    ch_c = changes_of(conn, run2, ids["c"])
    assert any(r["field"] in ("p_accept", "impact") and "raised a fund" in r["reason"] for r in ch_c)
    # E: new candidate
    ch_e = changes_of(conn, run2, e)
    assert len(ch_e) == 1 and ch_e[0]["field"] == "new_candidate" and "Echo Founder" in ch_e[0]["reason"]
    # A: nothing new; only a rank row is allowed, and it says why
    ch_a = changes_of(conn, run2, ids["a"])
    assert {r["field"] for r in ch_a} <= {"rank"}
    for r in ch_a:
        assert "same evidence" in r["reason"]
    # explain of the scored run cites evidence ids and reasons
    ex = score.explain(conn, run2, ids["b"])
    assert ex["factors"]["total"] == b1["total"] and ex["reasons"] and ex["evidence_ids"]
    new_ev_id = db.one(conn, "SELECT evidence_id FROM evidence WHERE url='https://x.com/bravo/status/77'")["evidence_id"]
    assert new_ev_id in ex["evidence_ids"]
    # idempotent: re-scoring the same run rewrites the same rows, no duplicate changes
    n_changes = len(changes_of(conn, run2))
    score.run_score(conn, run2)
    assert len(scores_of(conn, run2)) == 5 and len(changes_of(conn, run2)) == n_changes
    assert scores_of(conn, run2)[ids["b"]]["total"] == b1["total"]


def test_explain_cites_evidence_and_handles_unscored(conn):
    run1, ids, ev = seed(conn)
    score.run_score(conn, run1)
    ex = score.explain(conn, run1, ids["a"])
    assert set(ex["factors"]) == {"p_accept", "p_wear", "p_love", "impact", "reach", "affinity", "engagement", "confidence",
                                  "total", "rank"}
    assert set(ev[ids["a"]]) <= set(ex["evidence_ids"])
    assert any(r.startswith("p_wear:") for r in ex["reasons"]) and any("Marwan Refaat" in r for r in ex["reasons"])
    assert "whether they accept free devices" in ex["unknowns"] and any("p_accept" in u for u in ex["unknowns"])
    # a candidate without classification, evidence or map link is not scored, explain says so
    add_user(conn, "99", "nobody", 10)
    cid = add_candidate(conn, "99", "nobody", "No Body", None)
    conn.commit()
    ex2 = score.explain(conn, run1, cid)
    assert ex2["factors"] == {} and "Not scored" in ex2["reasons"][0]


def test_joined_competitor_persists_until_left(conn):
    run1 = db.new_run(conn, "score")
    add_user(conn, "20", "joiner", 5000)
    cid = add_candidate(conn, "20", "joiner", "Jo Iner", "founder_angel", handle_verified="likely_real")
    add_classification(conn, cid, run1, CLS_FOUNDER)
    add_event(conn, cid, "joined_competitor", days_ago=200, text="Joined Oura as head of product")
    conn.commit()
    score.run_score(conn, run1)
    s = scores_of(conn, run1)[cid]
    assert "competitor:Oura" in s["flags"] and s["p_accept"] < score.PRIORS["founder_angel"]["accept"] and s["total"] > 0
    add_event(conn, cid, "left_competitor", days_ago=100, text="Left Oura")
    conn.commit()
    run2 = db.new_run(conn, "score")
    score.run_score(conn, run2)
    s2 = scores_of(conn, run2)[cid]
    assert "competitor:Oura" not in s2["flags"] and s2["p_accept"] == score.PRIORS["founder_angel"]["accept"]
    flag_change = next(r for r in changes_of(conn, run2, cid) if r["field"] == "flags")
    assert "removed competitor:Oura" in flag_change["reason"]


def test_map_person_without_x_gets_low_prior_score(conn):
    run1 = db.new_run(conn, "score")
    db.upsert(conn, "map_people", {"id": 5243, "group_name": "press", "name": "Daniel Cooper", "company": "Engadget", "sector": "press",
                                   "warmth": 2.3}, keys=("id",))
    cid = add_candidate(conn, None, None, "Daniel Cooper", None, us_based=None, handle_verified=None, map_person_id=5243)
    conn.commit()
    score.run_score(conn, run1)
    s = scores_of(conn, run1)[cid]
    assert s["p_accept"] == score.PRIORS["media"]["accept"] and s["confidence"] == 0.0
    assert "unverified_handle" in s["flags"] and not {"no_path", "not_us"} & set(s["flags"])
    ex = score.explain(conn, run1, cid)
    assert "US base unknown" in ex["unknowns"] and "follower count unknown" in ex["unknowns"]


def test_helpers():
    assert score.decay(0) == 1.0 and score.decay(45) == 0.5 and score.decay(90) == 0.0 and score.decay(400) == 0.0
    assert score.log_followers(100) == 0.0 and score.log_followers(10_000) == 0.5 and score.log_followers(10**7) == 1.0
    assert score.log_followers(None) == score.FOLLOWERS_UNKNOWN
    funds = [{"fund": "Adjacent", "competitor": "Sandbar", "round": "A"}, {"fund": "Kindred Ventures", "competitor": "Sandbar", "round": "A"}]
    assert [f["fund"] for f in score.fund_matches("Partner @kindredventures, adjacent to hardware", funds)] == ["Kindred Ventures"]
    assert [f["fund"] for f in score.fund_matches("GP at Adjacent", funds)] == ["Adjacent"]
    assert score.fund_matches("", funds) == []
    assert score._competitor_name("Product lead @ouraring") == "Oura"
    assert score._competitor_name("joined Wispr Flow") == "Wispr Flow"
    assert score._competitor_name("my friend and I") is None


# ---------------------------------------------------------------- regression tests for the fixes of 2026-09-29

def _p(prior: float, logit_shift: float) -> float:
    return round(score._sigmoid(score._logit(prior) + logit_shift), 3)


def test_change_reasons_ignore_reclassified_evidence(conn):
    """classify re-upserts every row it re-classifies, which refreshes fetched_at: that must not read as new evidence."""
    run1 = db.new_run(conn, "score")
    add_user(conn, "30", "rerun", 20000)
    cid = add_candidate(conn, "30", "rerun", "Re Run", "x_voice")
    add_evidence(conn, cid, "wear", 1, 1.0, "Old wearable post from last month", url="https://x.com/rerun/status/1", date=day(30))
    conn.commit()
    score.run_score(conn, run1)
    conn.execute("UPDATE runs SET started_at=?, finished_at=? WHERE run_id=?", (iso(2), iso(1.9), run1))
    db.upsert(conn, "evidence", {"candidate_id": cid, "kind": "post", "supports": "wear", "url": "https://x.com/rerun/status/1",
                                 "text": "Old wearable post from last month", "polarity": 1, "weight": 1.0, "fetched_at": iso(0)},
              keys=("candidate_id", "kind", "supports", "url", "text"))
    add_evidence(conn, cid, "wear", 1, 1.0, "Brand new wearable unboxing", url="https://x.com/rerun/status/2", fetched_days_ago=0.0, date=day(1))
    run2 = db.new_run(conn, "score")
    conn.commit()
    score.run_score(conn, run2)
    wear = next(r for r in changes_of(conn, run2, cid) if r["field"] == "p_wear")
    assert "Brand new wearable unboxing" in wear["reason"] and "Old wearable post" not in wear["reason"]


def test_accept_spillover_counts_each_quote_once(conn):
    """A quote classify already filed under accept does not spill over again; a quote on wear and love spills once."""
    run1 = db.new_run(conn, "score")
    cid = add_candidate(conn, None, None, "Spill Over", "x_voice")
    for supports, w in (("wear", 1.0), ("accept", 0.5)):          # wearable_user -> wear 1.0 + accept 0.5 (classify.EVIDENCE_MAP)
        add_evidence(conn, cid, supports, 1, w, "Wearing my wearable", url="https://x.com/s/status/1")
    for supports in ("wear", "love"):                               # voice_tool_user -> wear 1.0 + love 1.0
        add_evidence(conn, cid, supports, 1, 1.0, "Talking to my AI all day", url="https://x.com/s/status/2")
    conn.commit()
    score.run_score(conn, run1)
    s = scores_of(conn, run1)[cid]
    assert s["p_accept"] == _p(0.40, 0.5 + score.ACCEPT_SPILLOVER * 1.0)   # was 0.5 + 0.25 * (2.0 + 1.0)


def test_spillover_alone_is_not_reported_as_prior_only(conn):
    run1 = db.new_run(conn, "score")
    cid = add_candidate(conn, None, None, "Only Wears", "x_voice")
    add_evidence(conn, cid, "wear", 1, 1.0, "Wears a wearable daily", url="https://x.com/w/status/1")
    conn.commit()
    score.run_score(conn, run1)
    ex = score.explain(conn, run1, cid)
    acc = ex["per_factor"]["p_accept"]
    assert ex["factors"]["p_accept"] > score.PRIORS["x_voice"]["accept"] and not acc["backed"]
    assert not any("this is the prior only" in r for r in acc["reasons"])
    assert "p_accept: no direct evidence (prior plus spillover from wear/love evidence)" in ex["unknowns"]


def test_impact_profile_signals_are_not_double_counted(conn):
    """Followers and audience relevance are read directly; classify's profile_signal impact rows restate them."""
    run1 = db.new_run(conn, "score")
    add_user(conn, "40", "plain", 48000)
    add_user(conn, "41", "restated", 48000)
    a = add_candidate(conn, "40", "plain", "Plain", "x_voice")
    b = add_candidate(conn, "41", "restated", "Restated", "x_voice")
    for cid in (a, b):
        add_evidence(conn, cid, "wear", 1, 1.0, "Wears a wearable daily", url=f"https://x.com/c{cid}/status/1")
    add_evidence(conn, b, "impact", 1, 0.9, "audience relevance 0.90", kind="profile_signal", url="https://x.com/restated")
    add_evidence(conn, b, "impact", 1, 0.78, "48,000 followers", kind="profile_signal", url="https://x.com/restated")
    conn.commit()
    score.run_score(conn, run1)
    s = scores_of(conn, run1)
    assert s[a]["impact"] == s[b]["impact"]
    add_evidence(conn, b, "impact", 1, 1.0, "Column in a national paper on wearables", kind="article", url="https://news.example/col")
    run2 = db.new_run(conn, "score")
    conn.commit()
    score.run_score(conn, run2)
    assert scores_of(conn, run2)[b]["impact"] == round(s[a]["impact"] + score.IMPACT_EVIDENCE_STEP * 1.0, 3)


def test_competitor_tie_counted_once(conn):
    """Competitor evidence and a joined_competitor event describe the same fact: the larger penalty counts, not the sum."""
    run1 = db.new_run(conn, "score")
    cid = add_candidate(conn, None, None, "Now At Oura", "founder_angel", competitor_flag=1, competitor_detail="Oura (employee)")
    add_evidence(conn, cid, "competitor", 1, 1.0, "I'm joining Oura as VP Product", url="https://x.com/o/status/1")
    add_event(conn, cid, "joined_competitor", days_ago=1, text="Joined Oura as VP Product", url="https://x.com/o/status/1")
    conn.commit()
    score.run_score(conn, run1)
    s = scores_of(conn, run1)[cid]
    assert s["p_accept"] == _p(0.40, -1.5) and "competitor:Oura" in s["flags"]   # was -1.0 - 1.5
    assert any("counted once" in r for r in score.explain(conn, run1, cid)["reasons"])


def test_undated_event_counts_half_even_when_refetched(conn):
    """fetched_at is refreshed by every re-classification, so it cannot stand in for the event's date."""
    run1 = db.new_run(conn, "score")
    cid = add_candidate(conn, None, None, "No Date", "x_voice")
    conn.execute("INSERT INTO events (candidate_id, kind, date, url, text, source, fetched_at) VALUES (?,?,?,?,?,?,?)",
                 (cid, "wearable_unboxing", None, "https://x.com/n/status/1", "Unboxing", "test", iso(0)))
    add_classification(conn, cid, run1, CLS_VOICE)                  # classify writes events together with a classification
    conn.commit()
    score.run_score(conn, run1)
    s = scores_of(conn, run1)[cid]
    assert s["p_wear"] == _p(0.30, score.UNDATED_EVENT_WEIGHT * 1.0)
    assert any("undated" in r for r in score.explain(conn, run1, cid)["reasons"])


def test_path_note_compares_with_the_path_the_previous_run_used(conn):
    """A score-only run has no paths rows and reuses the latest path; the next run must not report a path change."""
    run1 = db.new_run(conn, "all")
    cid = add_candidate(conn, None, None, "Warm Path", "media")
    add_evidence(conn, cid, "wear", 1, 0.5, "Tried a wearable", url="https://x.com/p/status/1")
    add_path(conn, run1, cid, "via", 0.7, via_name="Marwan Refaat")
    conn.commit()
    score.run_score(conn, run1)
    run2 = db.new_run(conn, "score")
    score.run_score(conn, run2)
    add_evidence(conn, cid, "wear", 1, 1.0, "Wears the wearable every day now", url="https://x.com/p/status/2")
    run3 = db.new_run(conn, "score")
    conn.commit()
    score.run_score(conn, run3)
    rows = changes_of(conn, run3, cid)
    assert rows and scores_of(conn, run3)[cid]["reach"] == 0.7
    assert not any("path:" in r["reason"] for r in rows), [r["reason"] for r in rows]


def test_worked_example_fixture_reviewer(conn):
    """The worked example in SCORING.md: @wearablenerd_test through the real classify stage (fixture LLM backend) and score.
    The fixture classification says topic_voice true (posts 102 and 103); computed affinity is pinned unknown by the fake,
    so affinity is the classifier floor 0.40. Since 30 Sep: the second post counts half, the voice-agent post counts half
    toward wear, and reach is no longer in the total: p_accept 0.689, p_wear 0.599, p_love 0.778, total 24.58 (was 18.80)."""
    import json

    from wearable_wishlist import classify, config

    fx = config.FIXTURES_DIR
    users = {u["x_id"]: u for u in json.loads((fx / "x_users.json").read_text())}
    for xid in ("1", "2"):
        u = users[xid]
        add_user(conn, xid, u["handle"], u["followers"], bio=u["bio"], verified=int(u["verified"]), can_dm=int(u["can_dm"]),
                 created=u["created_at"])
        conn.execute("UPDATE x_users SET location=?, verified_type=?, name=? WHERE x_id=?", (u["location"], u["verified_type"], u["name"], xid))
    for p in json.loads((fx / "x_posts.json").read_text()):
        if p["x_id"] == "2":
            db.upsert(conn, "x_posts", {k: p.get(k) for k in ("post_id", "x_id", "handle", "created_at", "text", "likes", "reposts",
                                                              "replies", "quotes", "url")} | {"source": "fixture", "fetched_at": iso(1)},
                      keys=("post_id",))
    run1 = db.new_run(conn, "all")
    rina = add_candidate(conn, "2", "wearablenerd_test", "Rina Wearable", None, us_based=None, handle_verified=None)
    conn.commit()
    assert classify.run_classify(conn, run1, candidate_ids=[rina], seed_handle="seed_test")["classified"] == 1
    add_path(conn, run1, rina, "x_direct", 0.35)                    # what paths.py gives a mutual follow with the seed
    conn.commit()
    stored = db.jloads(db.one(conn, "SELECT json FROM classifications WHERE candidate_id=?", (rina,))["json"], {})
    assert stored["topic_voice"]["value"] is True                   # precondition: the fixture carries the classifier verdict
    score.run_score(conn, run1)
    s = scores_of(conn, run1)[rina]
    assert (s["p_accept"], s["p_wear"], s["p_love"], s["impact"], s["reach"], s["affinity"]) == (0.689, 0.599, 0.778, 0.665, 0.35, 0.4)
    # reach 0.35 (a mutual follow) is shown, not scored; a follow is not a warm relationship, so p_accept gets no nudge
    assert s["total"] == 24.58 and s["confidence"] == 1.0 and s["flags"] == []
    # the second post counts EVIDENCE_DECAY; the two supports='affinity' rows add nothing on top
    assert s["p_love"] == _p(0.30, 1.0 + score.EVIDENCE_DECAY * 1.0 + score.AFFINITY_LOVE * 0.4)
    # wear: the Oura unboxing in full, the voice-agent post (#3, classify's voice_tool_user) at half weight, as the second post
    assert s["p_wear"] == _p(0.30, 1.0 + score.EVIDENCE_DECAY * score.VOICE_WEAR_WEIGHT)
    ex = score.explain(conn, run1, rina)
    voice = db.one(conn, "SELECT evidence_id FROM evidence WHERE candidate_id=? AND supports='wear' AND text LIKE 'Smart wearables%'", (rina,))
    assert ex["per_factor"]["p_wear"]["voice_tool_ids"] == [voice["evidence_id"]]
    f = ex["per_factor"]["affinity"]
    aff_rows = [r["evidence_id"] for r in db.rows(conn, "SELECT evidence_id FROM evidence WHERE candidate_id=? AND supports='affinity'", (rina,))]
    assert f["source"] == "classifier" and f["bound"] == "floor" and len(aff_rows) == 2 and f["evidence_ids"] == aff_rows


def test_worked_example_with_real_affinity_module(conn, monkeypatch):
    """Integration with workstream AF once src/wearable_wishlist/affinity.py exists (skipped before): the real topic_affinity is called
    with the run's reference time, returns the contract's shape for the fixture reviewer, and only raises her score."""
    monkeypatch.delitem(sys.modules, "wearable_wishlist.affinity", raising=False)
    real = pytest.importorskip("wearable_wishlist.affinity")
    import json

    from wearable_wishlist import config
    fx = config.FIXTURES_DIR
    u = next(u for u in json.loads((fx / "x_users.json").read_text()) if u["x_id"] == "2")
    add_user(conn, "2", u["handle"], u["followers"], bio=u["bio"])
    for p in json.loads((fx / "x_posts.json").read_text()):
        if p["x_id"] == "2":
            db.upsert(conn, "x_posts", {k: p.get(k) for k in ("post_id", "x_id", "handle", "created_at", "text", "likes", "reposts",
                                                              "replies", "quotes", "url")} | {"source": "fixture", "fetched_at": iso(1)},
                      keys=("post_id",))
    run1 = db.new_run(conn, "score")
    rina = add_candidate(conn, "2", "wearablenerd_test", "Rina Wearable", "x_voice")
    add_evidence(conn, rina, "love", 1, 1.0, "Smart wearables plus a voice agent is the form factor", url="https://x.com/wearablenerd_test/status/102")
    conn.commit()
    got, status = score._computed_affinity(conn, [rina], db.one(conn, "SELECT * FROM runs WHERE run_id=?", (run1,)))
    assert status == "ok" and real.topic_affinity
    entry = got.get(rina)
    assert entry is not None and {"score", "topical_posts", "topic_eng_avg", "top_posts", "reason"} <= set(entry)
    score.run_score(conn, run1)
    s = scores_of(conn, run1)[rina]
    assert s["p_love"] >= _p(0.30, 1.0) and (s["affinity"] is None or 0.0 <= s["affinity"] <= 1.0)
    if entry["score"] is not None:
        assert s["affinity"] == round(entry["score"], 3) and s["p_love"] == _p(0.30, 1.0 + score.AFFINITY_LOVE * s["affinity"])


# ---------------------------------------------------------------- topic affinity (docs/ARCHITECTURE.md, "Topic affinity")

def _twins(conn, run_id, cls=CLS_VOICE, x_ids=("50", "51")):
    """Two identical X voices with the same love and wear evidence; only what the test adds differs."""
    ids = []
    for i, xid in enumerate(x_ids):
        add_user(conn, xid, f"twin{i}", 20000)
        cid = add_candidate(conn, xid, f"twin{i}", f"Twin {i}", "x_voice")
        add_evidence(conn, cid, "love", 1, 1.0, "Reviewed the new pendant in depth", url=f"https://x.com/twin{i}/status/1")
        add_evidence(conn, cid, "wear", 1, 1.0, "Wears a wearable daily", url=f"https://x.com/twin{i}/status/2")
        add_classification(conn, cid, run_id, cls)
        ids.append(cid)
    return ids


def test_affinity_raises_p_love_p_accept_and_total(conn, affinity):
    run1 = db.new_run(conn, "score")
    a, b = _twins(conn, run1)
    top = (("https://x.com/twin0/status/9", day(3), "Your personal agent should live on your hand, not in a tab"),)
    affinity.data[a] = aff(0.6, topical=2, top=top)
    conn.commit()
    score.run_score(conn, run1)
    s = scores_of(conn, run1)
    assert affinity.calls and affinity.calls[0]["window_days"] == score.AFFINITY_WINDOW_DAYS
    assert affinity.calls[0]["now"] == db.one(conn, "SELECT started_at FROM runs WHERE run_id=?", (run1,))["started_at"]
    assert s[a]["affinity"] == 0.6 and s[b]["affinity"] is None
    assert s[a]["p_love"] == _p(0.30, 1.0 + score.AFFINITY_LOVE * 0.6) and s[b]["p_love"] == _p(0.30, 1.0)
    spill = score.ACCEPT_SPILLOVER * 2                              # two quotes, not filed under accept
    assert s[a]["p_accept"] == _p(0.40, spill + score.AFFINITY_ACCEPT * 0.6) and s[b]["p_accept"] == _p(0.40, spill)
    assert s[a]["p_wear"] == s[b]["p_wear"] and s[a]["impact"] == s[b]["impact"]
    assert s[a]["total"] > s[b]["total"] and s[a]["rank"] < s[b]["rank"]
    ex_a, ex_b = score.explain(conn, run1, a), score.explain(conn, run1, b)
    f = ex_a["per_factor"]["affinity"]
    assert f["value"] == 0.6 and f["source"] == "computed" and f["reason"].startswith("Keeps explaining personal agents")
    assert f["top_posts"][0]["url"] == top[0][0] and f["top_posts"][0]["date"] == top[0][1] and "personal agent" in f["top_posts"][0]["text"]
    assert ex_a["factors"]["affinity"] == 0.6 and any(r.startswith("affinity:") for r in ex_a["reasons"])
    assert "topic affinity unknown (no posts stored)" in ex_b["unknowns"]
    assert not any("topic affinity unknown" in u for u in ex_a["unknowns"])
    assert "p_accept: no direct evidence (prior plus spillover from wear/love evidence and topic affinity)" in ex_a["unknowns"]


def test_affinity_sits_outside_the_evidence_sum(conn, affinity):
    """Four love posts (their diminishing sum 1.875): affinity still adds its own log-odds, and P_MAX bounds the result."""
    run1 = db.new_run(conn, "score")
    cid = add_candidate(conn, None, None, "Capped", "x_voice")
    for i in range(4):
        add_evidence(conn, cid, "love", 1, 1.0, f"Glowing review number {i}", url=f"https://x.com/cap/status/{i}")
    affinity.data[cid] = aff(1.0)
    conn.commit()
    score.run_score(conn, run1)
    assert scores_of(conn, run1)[cid]["p_love"] == _p(0.30, score.diminishing([1.0] * 4) + score.AFFINITY_LOVE)
    assert _p(0.30, score.EVIDENCE_CAP + score.AFFINITY_LOVE) <= score.P_MAX


def test_classifier_floor_and_cap(conn, affinity):
    run1 = db.new_run(conn, "score")
    cases = {   # name: (topic_voice value or None, computed score or None, expected affinity, source, bound)
        "floor_raises": (True, 0.2, 0.4, "computed+classifier", "floor"),
        "floor_alone": (True, None, 0.4, "classifier", "floor"),
        "floor_below": (True, 0.7, 0.7, "computed+classifier", None),
        "cap_lowers": (False, 0.8, 0.3, "computed+classifier", "cap"),
        "cap_alone": (False, None, 0.0, "classifier", None),   # a "no" with nothing measured is 0, never above unknown
        "cap_above": (False, 0.1, 0.1, "computed+classifier", None),
        "both_unknown": (None, None, None, None, None),
    }
    ids = {}
    for name, (verdict, computed, *_rest) in cases.items():
        cid = add_candidate(conn, None, None, name, "x_voice")
        add_classification(conn, cid, run1, with_topic_voice(CLS_VOICE, verdict) if verdict is not None else CLS_VOICE)
        if computed is not None:
            affinity.data[cid] = aff(computed)
        ids[name] = cid
    conn.commit()
    score.run_score(conn, run1)
    s = scores_of(conn, run1)
    for name, (verdict, computed, want, source, bound) in cases.items():
        f = score.explain(conn, run1, ids[name])["per_factor"]["affinity"]
        assert s[ids[name]]["affinity"] == want, name
        assert (f["source"], f["bound"], f["verdict"]) == (source, bound, verdict), name
        assert s[ids[name]]["p_love"] == _p(0.30, score.AFFINITY_LOVE * (want or 0.0)), name
    # no love evidence at all: a known affinity >= 0.3 backs p_love, below that it only moves the prior
    ex = score.explain(conn, run1, ids["floor_alone"])
    assert ex["per_factor"]["p_love"]["backed"] and ex["factors"]["confidence"] == round(1 / 3, 3)
    assert not score.explain(conn, run1, ids["cap_alone"])["per_factor"]["p_love"]["backed"]
    low = score.explain(conn, run1, ids["cap_above"])
    assert not low["per_factor"]["p_love"]["backed"] and "p_love: no direct evidence (prior plus topic affinity)" in low["unknowns"]
    assert score.explain(conn, run1, ids["both_unknown"])["factors"]["confidence"] == 0.0
    assert "floor" in score.explain(conn, run1, ids["floor_raises"])["per_factor"]["affinity"]["reason"]


def test_affinity_evidence_rows_are_cited_not_counted(conn, affinity):
    """supports='affinity' rows quote the posts the computed score already measures: cited, never summed into a probability."""
    run1 = db.new_run(conn, "score")
    a, b = _twins(conn, run1, cls=with_topic_voice(CLS_VOICE, True))
    rows = [add_evidence(conn, a, "affinity", 1, 1.0, "Agents need a body, and the wearable is it", url="https://x.com/twin0/status/5"),
            add_evidence(conn, a, "affinity", 1, 0.8, "Building a voice agent in public", kind="bio", url="https://x.com/twin0")]
    add_evidence(conn, b, "affinity", -1, 1.0, "Posted once about agents", url="https://x.com/twin1/status/5")
    for cid in (a, b):
        affinity.data[cid] = aff(0.5)
    conn.commit()
    score.run_score(conn, run1)
    s = scores_of(conn, run1)
    for k in ("p_accept", "p_wear", "p_love", "impact", "affinity", "total"):
        assert s[a][k] == s[b][k], k
    assert s[a]["p_love"] == _p(0.30, 1.0 + score.AFFINITY_LOVE * 0.5)
    ex = score.explain(conn, run1, a)
    assert ex["per_factor"]["affinity"]["evidence_ids"] == rows and set(rows) <= set(ex["evidence_ids"])
    assert any("cited, not added again" in r for r in ex["per_factor"]["affinity"]["reasons"])
    assert not set(rows) & set(ex["per_factor"]["p_love"]["evidence_ids"] + ex["per_factor"]["p_accept"]["evidence_ids"])


def test_topic_engagement_needs_three_topic_posts(conn, affinity):
    run1 = db.new_run(conn, "score")
    a, b = _twins(conn, run1)
    for cid, xid in ((a, "50"), (b, "51")):
        add_post(conn, f"p{xid}", xid, likes=40, reposts=5, replies=4, quotes=1)     # 50 engagements per post over all posts
    affinity.data[a] = aff(0.5, topical=score.AFFINITY_MIN_TOPIC_POSTS, topic_eng=5000.0)
    affinity.data[b] = aff(0.5, topical=score.AFFINITY_MIN_TOPIC_POSTS - 1, topic_eng=5000.0)
    conn.commit()
    score.run_score(conn, run1)
    s = scores_of(conn, run1)
    assert s[a]["impact"] > s[b]["impact"]
    fa, fb = (score.explain(conn, run1, c)["per_factor"]["impact"] for c in (a, b))
    assert fa["engagement"] == {"source": "topic", "avg": 5000.0} and fb["engagement"] == {"source": "all", "avg": 50.0}
    assert any("per topical post over 3 topical posts" in r for r in fa["reasons"])
    assert any("per post over all 1 posts (only 2 topical" in r for r in fb["reasons"])
    attention = 0.6 * score.log_followers(20000) + 0.4 * score.log_engagement(5000.0)
    assert s[a]["impact"] == round((0.10 + 0.85 * attention) * (0.5 + 0.5 * 0.9), 3)


def test_affinity_change_row_on_a_010_move(conn, affinity):
    run1 = db.new_run(conn, "score")
    cid, other = _twins(conn, run1)
    old_post = ("https://x.com/twin0/status/20", day(30), "Agents will leave the phone")
    affinity.data[cid] = aff(0.20, topical=2, top=(old_post,))
    affinity.data[other] = aff(0.20, topical=2)
    conn.commit()
    score.run_score(conn, run1)
    # next run: a new topical post lifts cid by 0.10; other moves 0.09, below the threshold
    new_post = ("https://x.com/twin0/status/21", day(1), "Spent the week talking to my wearable instead of my laptop")
    affinity.data[cid] = aff(0.30, topical=3, top=(new_post, old_post))
    affinity.data[other] = aff(0.29, topical=2)
    run2 = db.new_run(conn, "score")
    score.run_score(conn, run2)
    row = next(r for r in changes_of(conn, run2, cid) if r["field"] == "affinity")
    assert (float(row["old_value"]), float(row["new_value"])) == (0.2, 0.3)
    assert row["reason"].startswith("topic affinity 0.20 -> 0.30: new topical posts: 'Spent the week talking to my wearable")
    assert new_post[0] in row["reason"] and old_post[0] not in row["reason"] and "3 topical posts in 90 days (was 2)" in row["reason"]
    assert not any(r["field"] == "affinity" for r in changes_of(conn, run2, other))
    # the classifier's verdict alone: unknown -> 0.40 floor, and p_love's row names the affinity move, not "same evidence"
    run3 = db.new_run(conn, "score")
    del affinity.data[other]
    add_classification(conn, other, run3, with_topic_voice(CLS_VOICE, True))
    add_evidence(conn, other, "affinity", 1, 1.0, "Hosts a weekly space on voice agents", url="https://x.com/twin1/status/30")
    conn.commit()
    score.run_score(conn, run3)
    rows = {r["field"]: r for r in changes_of(conn, run3, other)}
    assert rows["affinity"]["reason"].startswith("topic affinity 0.29 -> 0.40: ")
    assert "classifier verdict: no verdict -> topic voice" in rows["affinity"]["reason"]
    assert "Hosts a weekly space on voice agents" in rows["affinity"]["reason"]
    assert "p_love" not in rows or "topic affinity 0.29 -> 0.40" in rows["p_love"]["reason"]


def test_affinity_unknown_when_the_module_is_missing_or_fails(conn, monkeypatch):
    """Before affinity.py exists (or if it raises), scoring goes on: the classifier verdict alone, everything else unknown."""
    run1 = db.new_run(conn, "score")
    a = add_candidate(conn, None, None, "Verdict Only", "x_voice")
    b = add_candidate(conn, None, None, "Nothing Known", "x_voice")
    add_classification(conn, a, run1, with_topic_voice(CLS_VOICE, True))
    add_classification(conn, b, run1, CLS_VOICE)
    conn.commit()
    monkeypatch.setitem(sys.modules, "wearable_wishlist.affinity", None)       # makes `from .affinity import topic_affinity` raise ImportError
    score.run_score(conn, run1)
    s = scores_of(conn, run1)
    assert s[a]["affinity"] == 0.4 and s[b]["affinity"] is None
    assert s[b]["p_love"] == score.PRIORS["x_voice"]["love"]
    assert "topic affinity unknown (affinity module not available)" in score.explain(conn, run1, b)["unknowns"]

    def broken(conn, candidate_ids=None, now=None, window_days=90):
        raise RuntimeError("bug in affinity")
    mod = types.ModuleType("wearable_wishlist.affinity")
    mod.topic_affinity = broken
    monkeypatch.setitem(sys.modules, "wearable_wishlist.affinity", mod)
    run2 = db.new_run(conn, "score")
    score.run_score(conn, run2)
    assert scores_of(conn, run2)[a]["affinity"] == 0.4
    assert "topic affinity unknown (affinity computation failed)" in score.explain(conn, run2, b)["unknowns"]


# ---------------------------------------------------------------- fixes of 2026-09-30: voice vs wear, p_love spread, launch wave

def _cls_with(cls, **fields):
    """A classification whose wearable_user / voice_tool_user blocks quote the given (url, quote) pairs."""
    out = dict(cls)
    for field, quotes in fields.items():
        out[field] = {"value": True, "evidence": [{"url": u, "quote": q, "date": day(10), "verbatim": True} for u, q in quotes]}
    return out


def test_voice_tool_quotes_count_half_toward_wear(conn):
    """Voice-tool use proves the interface, not the habit of wearing a device: half weight on p_wear, full on p_love.
    The @srcasm case: four voice-tool posts and no device end below one Oura post plus the same voice use (@andruyeung)."""
    run1 = db.new_run(conn, "score")
    voice = add_candidate(conn, None, None, "Voice Only", "x_voice")
    both = add_candidate(conn, None, None, "Oura And Wispr", "x_voice")
    same = add_candidate(conn, None, None, "One Quote Both", "x_voice")
    vq = [(f"https://x.com/v/status/{i}", f"Dictated this with Wispr Flow, day {i}") for i in range(4)]
    bq = [(u.replace("/v/", "/b/"), q) for u, q in vq]
    for (u, q), (ub, _) in zip(vq, bq):                              # classify's voice_tool_user rows: wear 1.0 + love 1.0
        for supports in ("wear", "love"):
            add_evidence(conn, voice, supports, 1, 1.0, q, url=u)
            add_evidence(conn, both, supports, 1, 1.0, q, url=ub)
    oura = ("https://x.com/b/status/9", "Oura every night, eight months now")
    for supports, w in (("wear", 1.0), ("accept", 0.5)):            # wearable_user rows: wear 1.0 + accept 0.5
        add_evidence(conn, both, supports, 1, w, oura[1], url=oura[0])
    add_classification(conn, voice, run1, _cls_with(CLS_VOICE, voice_tool_user=vq))
    add_classification(conn, both, run1, _cls_with(CLS_VOICE, wearable_user=[oura], voice_tool_user=bq))
    # one quote filed under both fields ("Oura plus Wispr Flow daily"): it shows the device, so it counts in full
    q = ("https://x.com/s/status/1", "Oura plus Wispr Flow daily")
    for supports, w in (("wear", 1.0), ("love", 1.0), ("accept", 0.5)):
        add_evidence(conn, same, supports, 1, w, q[1], url=q[0])
    add_classification(conn, same, run1, _cls_with(CLS_VOICE, wearable_user=[q], voice_tool_user=[q]))
    conn.commit()
    score.run_score(conn, run1)
    s = scores_of(conn, run1)
    half = score.VOICE_WEAR_WEIGHT
    assert s[voice]["p_wear"] == _p(0.30, score.diminishing([half] * 4))                     # 0.94: less than one device quote
    assert score.diminishing([half] * 4) < 1.0
    assert s[both]["p_wear"] == _p(0.30, score.diminishing([1.0] + [half] * 4)) > s[voice]["p_wear"]
    assert s[voice]["p_love"] == s[both]["p_love"] == _p(0.30, score.diminishing([1.0] * 4))  # love is untouched
    assert s[same]["p_wear"] == _p(0.30, 1.0)
    ex = score.explain(conn, run1, voice)["per_factor"]["p_wear"]
    assert len(ex["voice_tool_ids"]) == 4 and any("voice-tool quote(s)" in r and "not a device worn" in r for r in ex["reasons"])
    assert score.explain(conn, run1, same)["per_factor"]["p_wear"]["voice_tool_ids"] == []


def test_voice_tool_fallback_without_a_classification(conn):
    """Rows no stored classification names: wear+love on one quote (voice_tool_user's pattern) is voice; wear+accept is a device."""
    run1 = db.new_run(conn, "score")
    cid = add_candidate(conn, None, None, "No Cls", "x_voice")
    for supports in ("wear", "love"):
        add_evidence(conn, cid, supports, 1, 1.0, "Talking to my agent all day", url="https://x.com/n/status/1")
    for supports, w in (("wear", 1.0), ("accept", 0.5)):
        add_evidence(conn, cid, supports, 1, w, "My wearable tracks my sleep", url="https://x.com/n/status/2")
    add_evidence(conn, cid, "wear", 1, 1.0, "Bought a second wearable", url="https://x.com/n/status/3")   # wear alone: a device
    conn.commit()
    score.run_score(conn, run1)
    assert scores_of(conn, run1)[cid]["p_wear"] == _p(0.30, score.diminishing([1.0, 1.0, score.VOICE_WEAR_WEIGHT]))


def test_one_post_counts_once_and_returns_diminish(conn):
    """Two quotes of the same post (re-classification picks a different excerpt) are one fact; the k-th post counts
    EVIDENCE_DECAY**(k-1), so more quotes keep separating people instead of all hitting the cap."""
    run1 = db.new_run(conn, "score")
    one = add_candidate(conn, None, None, "One Post", "x_voice")
    dup = add_candidate(conn, None, None, "Same Post Twice", "x_voice")
    many = add_candidate(conn, None, None, "Four Posts", "x_voice")
    mixed = add_candidate(conn, None, None, "Mixed", "x_voice")
    add_evidence(conn, one, "love", 1, 1.0, "Best review I wrote this year", url="https://x.com/o/status/1")
    add_evidence(conn, dup, "love", 1, 1.0, "Best review I wrote this year", url="https://x.com/d/status/1")
    add_evidence(conn, dup, "love", 1, 1.0, "I wrote the best review this year", url="https://x.com/D/status/1/")
    for i in range(4):
        add_evidence(conn, many, "love", 1, 1.0, f"Review number {i}", url=f"https://x.com/m/status/{i}")
    add_evidence(conn, mixed, "love", 1, 1.0, "Loved it", url="https://x.com/x/status/1")
    add_evidence(conn, mixed, "love", 1, 0.6, "It was fine", url="https://x.com/x/status/2")
    add_evidence(conn, mixed, "love", -1, 0.5, "Returned the pendant", url="https://x.com/x/status/3")
    conn.commit()
    score.run_score(conn, run1)
    s = scores_of(conn, run1)
    assert s[dup]["p_love"] == s[one]["p_love"] == _p(0.30, 1.0)
    assert s[many]["p_love"] == _p(0.30, 1.0 + 0.5 + 0.25 + 0.125) and s[many]["p_love"] > s[one]["p_love"]
    assert s[mixed]["p_love"] == _p(0.30, 1.0 + score.EVIDENCE_DECAY * 0.6 - 0.5)
    assert score.diminishing([1.0] * 20) < 2.0
    assert score.post_key("https://X.com/D/status/1/") == score.post_key("https://x.com/d/status/1") == "1"
    assert any("from 4 post(s)" in r for r in score.explain(conn, run1, many)["per_factor"]["p_love"]["reasons"])


def test_method_change_is_named_in_the_change_log(conn):
    """A run scored under an older SCORING_VERSION: moves without new evidence name the method change, not 'same evidence'."""
    run1 = db.new_run(conn, "score")
    cid = add_candidate(conn, None, None, "Voice Heavy", "x_voice")
    for i in range(4):
        for supports in ("wear", "love"):
            add_evidence(conn, cid, supports, 1, 1.0, f"Dictating with Wispr {i}", url=f"https://x.com/vh/status/{i}")
    conn.commit()
    score.run_score(conn, run1)
    row = db.one(conn, "SELECT reasons_json FROM scores WHERE run_id=? AND candidate_id=?", (run1, cid))
    assert db.jloads(row["reasons_json"], {})["version"] == score.SCORING_VERSION
    old = dict(db.jloads(row["reasons_json"], {}), version=None)             # as stored before versions existed
    conn.execute("UPDATE scores SET p_wear=0.9, p_love=0.9, reasons_json=? WHERE run_id=? AND candidate_id=?", (db.jdumps(old), run1, cid))
    run2 = db.new_run(conn, "score")
    conn.commit()
    score.run_score(conn, run2)
    rows = {r["field"]: r["reason"] for r in changes_of(conn, run2, cid)}
    assert "scoring method changed" in rows["p_wear"] and "same evidence" not in rows["p_wear"]


def test_competitor_ties_yaml_funds_aliases_and_people(conn, tmp_path, monkeypatch):
    """config/competitor_ties.yaml: its funds join the map's (aliases like '@a16z' match), its people are flagged by handle
    with competitor and kind. Flags only, like a map fund match. A missing file means no extra ties."""
    assert score.load_competitor_ties() == {"funds": [], "people": {}}
    ties = tmp_path / "competitor_ties.yaml"
    ties.write_text(
        "funds:\n"
        "  - {fund: Andreessen Horowitz, aliases: [a16z], competitor: Sesame, round: Series A, source_url: https://example.com/s}\n"
        "people:\n"
        "  - {handle: '@OonaAtOura', name: Oona Founder, competitor: Oura, kind: founder, source_url: https://example.com/o}\n")
    monkeypatch.setattr(score, "COMPETITOR_TIES_FILE", ties)
    run1 = db.new_run(conn, "score")
    add_user(conn, "70", "vic_vc", 30000, bio="Partner @a16z. Consumer hardware.")
    add_user(conn, "71", "oonaatoura", 9000)
    add_user(conn, "72", "other_vc", 30000, bio="Partner at Plain Ventures.")
    vc = add_candidate(conn, "70", "vic_vc", "Vic Capital", "vc", handle_verified="likely_real")
    fo = add_candidate(conn, "71", "oonaatoura", "Oona Founder", "founder_angel", handle_verified="likely_real")
    other = add_candidate(conn, "72", "other_vc", "Other Capital", "vc", handle_verified="likely_real")
    for cid, cls in ((vc, CLS_VC), (fo, CLS_FOUNDER), (other, CLS_VC)):
        add_classification(conn, cid, run1, cls)
    conn.commit()
    score.run_score(conn, run1)
    s = scores_of(conn, run1)
    assert "competitor:Sesame" in s[vc]["flags"] and "competitor:Oura" in s[fo]["flags"]
    assert not any(f.startswith("competitor:") for f in s[other]["flags"])
    assert s[fo]["p_accept"] == score.PRIORS["founder_angel"]["accept"]          # a flag, not a penalty
    reasons = score.explain(conn, run1, fo)["per_factor"]["flags"]["reasons"]
    assert any("Oona Founder is a founder of Oura" in r and "https://example.com/o" in r for r in reasons)
    assert any("Andreessen Horowitz" in r for r in score.explain(conn, run1, vc)["per_factor"]["flags"]["reasons"])


def _WEARABLE_WISHLIST_post(conn, post_id, x_id, days_ago, text):
    db.upsert(conn, "x_posts", {"post_id": post_id, "x_id": x_id, "created_at": iso(days_ago), "text": text,
                                "url": f"https://x.com/u{x_id}/status/{post_id}", "likes": 1, "reposts": 0, "replies": 0, "quotes": 0,
                                "source": "test", "fetched_at": iso(1)}, keys=("post_id",))


def test_launch_wave_praise_counts_less_toward_love(conn):
    """Praise of wearable_wishlist's products posted while 10+ accounts do the same (within 7 days) counts LAUNCH_WAVE_WEIGHT toward
    p_love, quote and posted_about_product event alike; accept keeps it. The same praise alone, weeks later, counts in full.
    A bio that sells posts (discover's paid_promo) gets the wave weight whenever it posted."""
    run1 = db.new_run(conn, "score")
    for i in range(score.LAUNCH_WAVE_MIN_ACCOUNTS - 1):                    # 9 other accounts reply to the launch
        _WEARABLE_WISHLIST_post(conn, str(9000 + i), f"w{i}", 5 + i % 3, f"@WearableAI @WearableWishlistAI tinyNature is the future {i}")
    add_user(conn, "60", "waver", 5000)
    add_user(conn, "61", "organic", 5000)
    add_user(conn, "62", "promo", 5000, bio="AI news daily. DM for paid partnership.")
    ids = {}
    for key, xid, days_ago in (("wave", "60", 5), ("organic", "61", 30), ("promo", "62", 30)):
        cid = add_candidate(conn, xid, key, key.title(), "x_voice")
        url = f"https://x.com/{key}/status/{700 + int(xid)}"
        add_evidence(conn, cid, "love", 1, 1.0, "Cannot stop thinking about @WearableWishlistAI's TinyNature", url=url, date=day(days_ago))
        add_evidence(conn, cid, "love", 1, 1.0, "this is top!!!", url=url, date=day(days_ago))   # same post, no product name
        add_event(conn, cid, "posted_about_product", days_ago=days_ago, text="Praised TinyNature", url=url)
        ids[key] = cid
    conn.commit()
    ctx = score._load(conn, run1)
    assert ctx["wave"]["days"] and ctx["wave"]["paid_promo"] == {ids["promo"]}
    score.run_score(conn, run1)
    s = scores_of(conn, run1)
    wv = score.LAUNCH_WAVE_WEIGHT
    assert s[ids["wave"]]["p_love"] == _p(0.30, wv * 1.0 + wv * 1.0 * score.decay(5))
    assert s[ids["organic"]]["p_love"] == _p(0.30, 1.0 + 1.0 * score.decay(30))
    assert s[ids["promo"]]["p_love"] == _p(0.30, wv * 1.0 + wv * 1.0 * score.decay(30))
    # accept is not discounted: the event's +1.0 and the post's spill count in full
    assert s[ids["wave"]]["p_accept"] == _p(0.40, score.ACCEPT_SPILLOVER * 1.0 + 1.0 * score.decay(5))
    ex = score.explain(conn, run1, ids["wave"])["per_factor"]["p_love"]
    assert len(ex["launch_wave_ids"]) == 2
    assert any("launch-wave praise" in r and "accounts praised wearable_wishlist" in r for r in ex["reasons"])
    assert not score.explain(conn, run1, ids["organic"])["per_factor"]["p_love"]["launch_wave_ids"]
    # one account fewer is no wave; and ordinary-language lookalikes are not wearable_wishlist
    conn.execute("DELETE FROM x_posts WHERE post_id='9000'")
    assert score._load(conn, run1)["wave"]["days"] == {}
    assert score.WEARABLE_WISHLIST_PRODUCT_RE.search("the most ordinary TTS I have tried") is None
    assert all(score.WEARABLE_WISHLIST_PRODUCT_RE.search(t) for t in ("@WearableWishlistAI", "Wearable Wishlist", "HumanPods", "#tinyNature", "wearable-wishlist.example"))


def test_rejected_people_rank_last_and_leave_the_export_and_verify(conn, tmp_path):
    """QA rejections (status 'rejected') stay scored but rank after everyone, are not exported and are not re-verified."""
    from wearable_wishlist import export, verify
    run1 = db.new_run(conn, "score")
    best = add_candidate(conn, None, "best", "Best But Rejected", "x_voice")
    kept = add_candidate(conn, None, "kept", "Kept", "x_voice")
    for i in range(3):
        add_evidence(conn, best, "love", 1, 1.0, f"Loved it {i}", url=f"https://x.com/b/status/{i}")
    add_evidence(conn, kept, "love", 1, 0.5, "It was fine", url="https://x.com/k/status/1")
    conn.execute("INSERT INTO status (candidate_id, status) VALUES (?, 'rejected')", (best,))
    conn.commit()
    score.run_score(conn, run1)
    s = scores_of(conn, run1)
    assert s[best]["total"] > s[kept]["total"] and s[kept]["rank"] == 1 and s[best]["rank"] == 2
    rows = export.build_rows(conn, run1, None)
    assert [r["candidate_id"] for r in rows] == [kept] and rows[0]["n"] == 1
    assert [c["candidate_id"] for c in verify.pick_candidates(conn, 10)] == [kept]


def test_single_word_fund_matches_its_handle_in_any_case():
    from wearable_wishlist.score import fund_matches
    funds = [{"fund": "Betaworks", "competitor": "Sandbar"}, {"fund": "Adjacent", "competitor": "Sandbar"}]
    assert [f["fund"] for f in fund_matches("CEO @betaworks, NYC", funds)] == ["Betaworks"]
    assert fund_matches("adjacent to the park", funds) == []          # a plain lower-case word is still not the fund
    assert [f["fund"] for f in fund_matches("GP at Adjacent", funds)] == ["Adjacent"]
