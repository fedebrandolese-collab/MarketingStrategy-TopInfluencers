"""Classification on fixtures: the fixture LLM backend (DRY_RUN=1) or a monkeypatched llm.complete_json. No network."""
import json
import logging
import threading
import time

import pytest

from ring_wishlist import classify, config, costs, db, discover, llm
from ring_wishlist.sources.base import FixtureSource, XPost, persist_posts

FIXTURE = json.loads((config.FIXTURES_DIR / "llm_classification.json").read_text())


@pytest.fixture(autouse=True)
def _seed_env(monkeypatch):
    monkeypatch.setenv("CARLO_X_HANDLE", "carlo_test")


@pytest.fixture
def world(conn):
    """Crawl + discovery on the fixture graph: candidates with profiles and topical posts."""
    run_id = db.new_run(conn, "setup")
    src = FixtureSource()
    discover.run_crawl(conn, run_id, src, "carlo_test")
    discover.run_discover(conn, run_id, src)
    return conn


def cid(conn, handle):
    return db.one(conn, "SELECT candidate_id FROM candidates WHERE handle=?", (handle,))["candidate_id"]


def with_events(**extra):
    out = json.loads(json.dumps(FIXTURE))
    out["events"] = [
        {"kind": "ring_unboxing", "date": "2026-09-20", "url": "https://x.com/ringnerd_test/status/101", "text": "Unboxing the new Oura Ring 5"},
        {"kind": "device_review", "date": "2026-09-01", "url": "https://x.com/ringnerd_test/status/999999", "text": "invented"},
    ]
    out.update(extra)
    return out


def test_classify_ringnerd_writes_evidence_events_and_updates_candidate(world, monkeypatch):
    conn = world
    monkeypatch.setattr(llm, "complete_json", lambda prompt, schema, **kw: with_events())
    run_id = db.new_run(conn, "classify")
    rid = cid(conn, "ringnerd_test")
    stats = classify.run_classify(conn, run_id, candidate_ids=[rid])
    assert stats["classified"] == 1 and stats["errors"] == 0
    c = db.one(conn, "SELECT * FROM candidates WHERE candidate_id=?", (rid,))
    assert c["category"] == "x_voice" and c["us_based"] == 1 and c["competitor_flag"] == 0
    assert c["handle_verified"] == "verified" and c["handle_verification_method"] == "llm+signals"
    ev = db.rows(conn, "SELECT * FROM evidence WHERE candidate_id=?", (rid,))
    posts = {(e["supports"], e["url"]) for e in ev if e["kind"] == "post"}
    assert ("wear", "https://x.com/ringnerd_test/status/101") in posts
    assert ("love", "https://x.com/ringnerd_test/status/102") in posts and ("wear", "https://x.com/ringnerd_test/status/102") in posts
    assert ("love", "https://x.com/ringnerd_test/status/103") in posts
    assert {"us", "authenticity", "impact", "reach"} <= {e["supports"] for e in ev if e["kind"] == "profile_signal"}
    assert all(e["text"] for e in ev)
    events = db.rows(conn, "SELECT * FROM events WHERE candidate_id=?", (rid,))
    assert [(e["kind"], e["url"], e["date"]) for e in events] == [("ring_unboxing", "https://x.com/ringnerd_test/status/101", "2026-09-20")]
    k = json.loads(db.one(conn, "SELECT json FROM classifications WHERE candidate_id=? AND run_id=?", (rid, run_id))["json"])
    assert k["summary"] and k["_meta"]["dropped_events"][0]["why"] == "url not in dossier"
    assert k["_meta"]["posts_in_db"] == 3
    # idempotent within a run: re-applying does not duplicate evidence or events
    classify.run_classify(conn, run_id, candidate_ids=[rid])
    assert len(db.rows(conn, "SELECT * FROM evidence WHERE candidate_id=?", (rid,))) == len(ev)
    assert len(db.rows(conn, "SELECT * FROM events WHERE candidate_id=?", (rid,))) == 1


def test_evidence_whose_url_is_not_in_the_dossier_is_dropped(world, monkeypatch):
    conn = world
    run_id = db.new_run(conn, "classify")
    vid = cid(conn, "vc_test")
    # the fixture backend answers every candidate with ringnerd's posts: none of those urls is in vc_test's dossier
    stats = classify.run_classify(conn, run_id, candidate_ids=[vid])
    assert stats["classified"] == 1 and stats["evidence_dropped"] == 5          # 3 device fields + 2 topic_voice
    assert not db.rows(conn, "SELECT * FROM evidence WHERE candidate_id=? AND kind IN ('post','bio')", (vid,))
    meta = json.loads(db.one(conn, "SELECT json FROM classifications WHERE candidate_id=?", (vid,))["json"])["_meta"]
    assert {d["why"] for d in meta["dropped_evidence"]} == {"url not in dossier"}

    # mixed answer: its own post survives (twitter.com spelling tolerated), the invented url does not
    mixed = json.loads(json.dumps(FIXTURE))
    mixed["wearable_user"] = {"value": True, "evidence": [
        {"url": "https://twitter.com/vc_test/status/302", "quote": "Wearing a smart ring changed how I think", "date": "2026-05-15"},
        {"url": "https://x.com/vc_test/status/4242", "quote": "I love my ring", "date": "2026-09-01"}]}
    monkeypatch.setattr(llm, "complete_json", lambda prompt, schema, **kw: mixed)
    run2 = db.new_run(conn, "classify")
    classify.run_classify(conn, run2, candidate_ids=[vid])
    kept = db.rows(conn, "SELECT url, supports, text FROM evidence WHERE candidate_id=? AND kind='post'", (vid,))
    assert {r["url"] for r in kept} == {"https://x.com/vc_test/status/302"}
    assert {r["supports"] for r in kept} == {"wear", "accept"}


def test_failing_llm_call_is_logged_and_skipped(world, monkeypatch, caplog):
    conn = world

    def flaky(prompt, schema, **kw):
        if '"handle":"vc_test"' in prompt:
            raise llm.LLMError("claude CLI timed out after 120s")
        return FIXTURE

    monkeypatch.setattr(llm, "complete_json", flaky)
    run_id = db.new_run(conn, "classify")
    with caplog.at_level(logging.ERROR, logger="ring_wishlist.classify"):
        stats = classify.run_classify(conn, run_id, workers=3)
    assert stats["errors"] == 1
    assert stats["classified"] == stats["selected"] - 1 >= 5
    assert any("vc_test" in r.getMessage() and "skipped" in r.getMessage() for r in caplog.records)
    assert not db.rows(conn, "SELECT 1 FROM classifications WHERE candidate_id=?", (cid(conn, "vc_test"),))
    # the failed one is retried next run; the others are unchanged and skipped
    monkeypatch.setattr(llm, "complete_json", lambda prompt, schema, **kw: FIXTURE)
    again = classify.run_classify(conn, db.new_run(conn, "classify"))
    assert again["classified"] == 1 and again["skipped_unchanged"] == stats["classified"]


def test_calls_run_concurrently_and_writes_stay_on_the_calling_thread(world, monkeypatch):
    conn = world
    main = threading.get_ident()
    threads, active, peak = set(), [0], [0]
    lock = threading.Lock()

    def slow(prompt, schema, conn=None, run_id=None, **kw):
        assert threading.get_ident() != main
        with lock:
            threads.add(threading.get_ident())
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        costs.guard(conn, llm.PROVIDER, 0.0, run_id)                      # what llm.py does with the connection it gets
        costs.record(conn, llm.PROVIDER, "claude_cli/test", 1000, 0.0, run_id, "subscription, not billed")
        time.sleep(0.2)
        with lock:
            active[0] -= 1
        return FIXTURE

    monkeypatch.setattr(llm, "complete_json", slow)
    run_id = db.new_run(conn, "classify")
    t0 = time.time()
    stats = classify.run_classify(conn, run_id, workers=4)
    elapsed = time.time() - t0
    n = stats["classified"]
    assert n == stats["selected"] >= 6 and stats["errors"] == 0
    assert peak[0] > 1 and len(threads) > 1
    assert elapsed < n * 0.2                               # sequential would take n * 0.2 s
    ledger = db.rows(conn, "SELECT * FROM costs WHERE run_id=? AND provider=?", (run_id, llm.PROVIDER))
    assert len(ledger) == n                                 # relayed from the workers, written by the calling thread


def test_priority_order_and_skip_unchanged(world, monkeypatch):
    conn = world
    seen = []
    monkeypatch.setattr(llm, "complete_json", lambda prompt, schema, **kw: seen.append(json.loads(
        prompt.split("## Dossier", 1)[1].strip())["handle"]) or FIXTURE)
    expected = [s["handle"] for s in discover.candidate_priorities(conn)]
    r1 = classify.run_classify(conn, db.new_run(conn, "classify"), workers=1)
    assert seen == expected and r1["classified"] == len(expected)
    seen.clear()
    r2 = classify.run_classify(conn, db.new_run(conn, "classify"), workers=2)
    assert r2["classified"] == 0 and r2["skipped_unchanged"] == len(expected) and seen == []
    # a new post for one person: only that person is classified again
    persist_posts(conn, [XPost(post_id="777", x_id="4", handle="vc_test", text="Day 3 with my new ring", likes=5)], "fixture")
    r3 = classify.run_classify(conn, db.new_run(conn, "classify"))
    assert seen == ["vc_test"] and r3["classified"] == 1
    seen.clear()
    r4 = classify.run_classify(conn, db.new_run(conn, "classify"), force=True, max_candidates=2)
    assert r4["classified"] == 2 and seen == expected[:2]


def test_dossier_is_compact_and_ranks_evidence_first(world):
    conn = world
    rid = cid(conn, "ringnerd_test")
    long_text = "My smart ring review. " + "x" * 5000
    posts = [XPost(post_id="8000", x_id="2", handle="ringnerd_test", text=long_text, likes=1)]
    posts += [XPost(post_id=str(8100 + i), x_id="2", handle="ringnerd_test", text=f"lunch {i}", likes=0,
                    created_at=f"2026-08-{1 + i % 28:02d}T00:00:00Z", query="from:ringnerd_test -is:retweet") for i in range(40)]
    posts += [XPost(post_id="8200", x_id="2", handle="ringnerd_test", text="Dictating this with Wispr while walking", likes=2,
                    query=discover.enrich_queries("ringnerd_test")[0])]
    persist_posts(conn, posts, "fixture")
    d = classify.build_dossier(conn, rid)
    assert len(d["posts"]) == classify.MAX_DOSSIER_POSTS and d["posts_in_db"] == 45
    assert all(len(p["text"]) <= classify.MAX_POST_CHARS for p in d["posts"])
    by_url = {p["url"]: p for p in d["posts"]}
    assert by_url["https://x.com/ringnerd_test/status/8000"]["text"].endswith("…")
    # device/voice search hits and topic-query hits lead, recent context fills the rest
    leading = [p["found_by"] for p in d["posts"][:4]]
    assert leading.count("device/voice search") == 1 and leading.count("topic search") == 3
    assert d["posts"][-1]["found_by"] == "recent posts"
    prompt = classify.render_prompt(d)
    assert len(prompt) < 25_000 and "posts_in_db" not in prompt and '"query"' not in prompt
    assert "Oura, Ultrahuman" in prompt or "Sandbar" in prompt     # competitor list rendered


def test_location_and_badge_are_not_overridden_by_the_model(world):
    conn = world
    run_id = db.new_run(conn, "classify")
    classify.run_classify(conn, run_id, candidate_ids=[cid(conn, "eu_test"), cid(conn, "founder_test")])
    eu = db.one(conn, "SELECT * FROM candidates WHERE handle='eu_test'")
    assert eu["us_based"] == 0                               # "Berlin, Germany", whatever the model said
    founder = db.one(conn, "SELECT * FROM candidates WHERE handle='founder_test'")
    assert founder["us_based"] == 1                          # "Boulder, CO"
    assert founder["handle_verified"] != "verified"          # no badge in the profile


def test_parody_account_never_gets_a_real_category(world):
    conn = world
    run_id = db.new_run(conn, "classify")
    pid, _ = discover.upsert_candidate(conn, "6", "parody_test", "Rina Ring (parody)", "manual")
    conn.commit()
    classify.run_classify(conn, run_id, candidate_ids=[pid])
    c = db.one(conn, "SELECT * FROM candidates WHERE candidate_id=?", (pid,))
    assert c["handle_verified"] == "suspicious"
    assert c["category"] not in ("x_voice", "media", "vc", "founder_angel")


def test_budget_exceeded_stops_new_calls(world, monkeypatch):
    conn = world
    calls = []

    def broke(prompt, schema, **kw):
        calls.append(1)
        raise costs.BudgetExceeded("anthropic: cap reached")

    monkeypatch.setattr(llm, "complete_json", broke)
    stats = classify.run_classify(conn, db.new_run(conn, "classify"), workers=2)
    assert stats["budget_stop"] and stats["classified"] == 0
    assert len(calls) <= 2                                   # only the calls already in flight


# ---- topic_voice (docs/ARCHITECTURE.md "Topic affinity", point 2) ----------------------------------------------------

def with_topic_voice(value, evidence, topics=("voice",)):
    out = json.loads(json.dumps(FIXTURE))
    out["topic_voice"] = {"value": value, "topics": list(topics), "evidence": evidence}
    return out


def affinity_rows(conn, candidate_id):
    return db.rows(conn, "SELECT kind, polarity, weight, url, text FROM evidence WHERE candidate_id=? AND supports='affinity' "
                         "ORDER BY url", (candidate_id,))


def test_schema_and_prompt_ask_for_topic_voice():
    schema = classify.CLASSIFICATION_SCHEMA
    assert "topic_voice" in schema["required"]
    tv = schema["properties"]["topic_voice"]
    assert tv["required"] == ["value", "topics", "evidence"]
    assert tv["properties"]["topics"]["items"]["enum"] == ["personal_agents", "voice", "wearables", "screen_free"]
    assert classify.EVIDENCE_MAP["topic_voice"] == [("affinity", 1.0)] and "affinity" in config.SUPPORTS
    template = classify.load_prompt()[1]
    assert "`topic_voice`" in template and '"topic_voice": {"value": true|false|null' in template
    assert "at least two evidence items from two different posts" in template
    assert FIXTURE["topic_voice"]["value"] is True        # the fixture reviewer talks about voice agents and rings


def test_topic_voice_writes_affinity_evidence_and_is_stored(world, monkeypatch):
    conn = world
    rid = cid(conn, "ringnerd_test")
    run_id = db.new_run(conn, "classify")
    stats = classify.run_classify(conn, run_id, candidate_ids=[rid])      # fixture backend (DRY_RUN=1)
    assert stats["topic_voice"] == {"true": 1, "false": 0, "null": 0, "missing": 0}
    rows = affinity_rows(conn, rid)
    assert [(r["kind"], r["polarity"], r["weight"], r["url"]) for r in rows] == [
        ("post", 1, 1.0, "https://x.com/ringnerd_test/status/102"), ("post", 1, 1.0, "https://x.com/ringnerd_test/status/103")]
    k = json.loads(db.one(conn, "SELECT json FROM classifications WHERE candidate_id=? AND run_id=?", (rid, run_id))["json"])
    assert k["topic_voice"]["value"] is True and k["topic_voice"]["topics"] == ["voice", "wearables"]
    assert [e["url"] for e in k["topic_voice"]["evidence"]] == ["https://x.com/ringnerd_test/status/102",
                                                               "https://x.com/ringnerd_test/status/103"]

    # a bio quote is kind 'bio' at 0.8; topics are normalised and ordered personal agents first
    answer = with_topic_voice(True, [
        {"url": "https://x.com/ringnerd_test/status/101", "quote": "the voice notes feature is finally usable", "date": "2026-09-20"},
        {"url": "https://x.com/ringnerd_test/status/102", "quote": "Smart rings plus a voice agent is the form factor", "date": "2026-08-02"},
        {"url": "https://x.com/ringnerd_test", "quote": "Voice-first believer", "date": "2026-09-29"}],
        topics=["voice", "Personal agents", "telepathy"])
    monkeypatch.setattr(llm, "complete_json", lambda prompt, schema, **kw: answer)
    run2 = db.new_run(conn, "classify")
    classify.run_classify(conn, run2, candidate_ids=[rid])
    bio = [r for r in affinity_rows(conn, rid) if r["kind"] == "bio"]
    assert [(r["polarity"], r["weight"], r["url"], r["text"]) for r in bio] == [(1, 0.8, "https://x.com/ringnerd_test", "Voice-first believer")]
    k2 = json.loads(db.one(conn, "SELECT json FROM classifications WHERE candidate_id=? AND run_id=?", (rid, run2))["json"])
    assert k2["topic_voice"]["topics"] == ["personal_agents", "voice"]
    assert any("telepathy" in n for n in k2["_meta"]["notes"])


def test_topic_voice_false_is_negative_evidence(world, monkeypatch):
    conn = world
    vid = cid(conn, "vc_test")
    answer = with_topic_voice(False, [{"url": "https://x.com/vc_test/status/301", "quote": "We just closed Fund IV", "date": "2026-09-01"}],
                              topics=[])
    monkeypatch.setattr(llm, "complete_json", lambda prompt, schema, **kw: answer)
    stats = classify.run_classify(conn, db.new_run(conn, "classify"), candidate_ids=[vid])
    assert stats["topic_voice"]["false"] == 1
    assert [(r["kind"], r["polarity"], r["weight"], r["url"]) for r in affinity_rows(conn, vid)] == [
        ("post", -1, 1.0, "https://x.com/vc_test/status/301")]


def test_topic_voice_evidence_outside_the_dossier_is_dropped(world, monkeypatch):
    conn = world
    vid = cid(conn, "vc_test")
    # one real post, one invented: the invented one is dropped, and one post is not enough for 'true'
    answer = with_topic_voice(True, [
        {"url": "https://x.com/vc_test/status/302", "quote": "Wearing a smart ring changed how I think", "date": "2026-05-15"},
        {"url": "https://x.com/vc_test/status/9999", "quote": "Agents are the new apps", "date": "2026-09-02"}])
    monkeypatch.setattr(llm, "complete_json", lambda prompt, schema, **kw: answer)
    run_id = db.new_run(conn, "classify")
    stats = classify.run_classify(conn, run_id, candidate_ids=[vid])
    assert stats["topic_voice"]["null"] == 1
    k = json.loads(db.one(conn, "SELECT json FROM classifications WHERE candidate_id=? AND run_id=?", (vid, run_id))["json"])
    assert [e["url"] for e in k["topic_voice"]["evidence"]] == ["https://x.com/vc_test/status/302"]
    assert k["topic_voice"]["value"] is None and any("topic_voice true stored as null" in n for n in k["_meta"]["notes"])
    assert {d["item"]["url"] for d in k["_meta"]["dropped_evidence"] if d["field"] == "topic_voice"} == {"https://x.com/vc_test/status/9999"}
    assert not affinity_rows(conn, vid)

    # two different real posts (twitter.com spelling tolerated): 'true' stands, the invented url still never becomes evidence
    answer["topic_voice"]["evidence"].append(
        {"url": "https://twitter.com/vc_test/status/301", "quote": "personal AI agents", "date": "2026-09-01"})
    classify.run_classify(conn, db.new_run(conn, "classify"), candidate_ids=[vid])
    assert {r["url"] for r in affinity_rows(conn, vid)} == {"https://x.com/vc_test/status/301", "https://x.com/vc_test/status/302"}


def test_refresh_field_selects_only_classifications_without_it(world, monkeypatch):
    conn = world
    old = {k: v for k, v in FIXTURE.items() if k != "topic_voice"}          # an answer from before the field existed
    seen = []

    def answer(prompt, schema, **kw):
        seen.append(json.loads(prompt.split("## Dossier", 1)[1].strip())["handle"])
        return old

    monkeypatch.setattr(llm, "complete_json", answer)
    r1 = classify.run_classify(conn, db.new_run(conn, "classify"))
    everyone = list(seen)
    assert r1["classified"] == len(everyone) >= 6 and r1["topic_voice"]["missing"] == len(everyone)
    k = json.loads(db.one(conn, "SELECT json FROM classifications WHERE candidate_id=?", (cid(conn, "vc_test"),))["json"])
    assert "topic_voice" not in k                                          # absent, not a made-up null

    # one person already re-classified with the new field; one person never classified
    monkeypatch.setattr(llm, "complete_json", lambda prompt, schema, **kw: seen.append(
        json.loads(prompt.split("## Dossier", 1)[1].strip())["handle"]) or FIXTURE)
    classify.run_classify(conn, db.new_run(conn, "classify"), candidate_ids=[cid(conn, "ringnerd_test")])
    pid, _ = discover.upsert_candidate(conn, "6", "parody_test", "Rina Ring (parody)", "manual")
    persist_posts(conn, [XPost(post_id="778", x_id="2", handle="ringnerd_test", text="Voice agents, again", likes=3)], "fixture")
    conn.commit()
    seen.clear()

    r2 = classify.run_classify(conn, db.new_run(conn, "classify"), refresh_field="topic_voice")
    expected = [h for h in everyone if h != "ringnerd_test"]
    assert seen == expected                                    # priority order, without the one that has it (new post or not)
    assert r2["classified"] == len(expected) and r2["skipped_has_field"] == 1 and r2["skipped_not_classified"] >= 1
    assert r2["refresh_field"] == "topic_voice" and "parody_test" not in seen

    seen.clear()
    r3 = classify.run_classify(conn, db.new_run(conn, "classify"), refresh_field="topic_voice")
    assert r3["selected"] == 0 and seen == []                  # once only
    with pytest.raises(ValueError):
        classify.run_classify(conn, db.new_run(conn, "classify"), refresh_field="topic_vocie")


# ---- QA guards (docs/QA_TOP.md): mistakes that repeated in the top 60 of run 14

def _person(conn, x_id, handle, name, bio, location=None, verified_type=None, map_role=None):
    from ring_wishlist.sources.base import XUser, persist_users
    persist_users(conn, [XUser(x_id=x_id, handle=handle, name=name, bio=bio, location=location, verified_type=verified_type,
                               followers=80_000)], "test")
    pid, _ = discover.upsert_candidate(conn, x_id, handle, name, "manual")
    if map_role:
        db.upsert(conn, "map_people", {"id": 9000 + int(x_id), "group_name": "press", "name": name, "role": map_role,
                                       "company": "TechCrunch"}, keys=("id",))
        conn.execute("UPDATE candidates SET map_person_id=? WHERE candidate_id=?", (9000 + int(x_id), pid))
    conn.commit()
    return pid


def test_reclassification_prunes_rows_the_model_no_longer_cites(world, monkeypatch):
    conn = world
    rid = cid(conn, "ringnerd_test")
    classify.run_classify(conn, db.new_run(conn, "classify"), candidate_ids=[rid])          # fixture: Oura unboxing -> wear
    conn.execute("INSERT INTO evidence (candidate_id, kind, supports, polarity, weight, url, date, text, source, fetched_at) "
                 "VALUES (?, 'post', 'wear', 1, 1.0, 'https://x.com/ringnerd_test/status/777', '2025-01-01', "
                 "'an old post outside the dossier', 'llm:old', '2025-01-02')", (rid,))
    conn.commit()
    answer = json.loads(json.dumps(FIXTURE))
    answer["wearable_user"] = {"value": None, "evidence": []}                               # the model changed its mind
    monkeypatch.setattr(llm, "complete_json", lambda prompt, schema, **kw: answer)
    stats = classify.run_classify(conn, db.new_run(conn, "classify"), candidate_ids=[rid], force=True)
    wear = {r["url"] for r in db.rows(conn, "SELECT url FROM evidence WHERE candidate_id=? AND supports='wear'", (rid,))}
    assert "https://x.com/ringnerd_test/status/101" not in wear                               # re-read, no longer cited: gone
    assert "https://x.com/ringnerd_test/status/102" in wear                                   # still cited (voice)
    assert "https://x.com/ringnerd_test/status/777" in wear                                   # not seen again: kept
    assert stats["evidence_pruned"] == 2                                                      # wear + accept rows of post 101


def test_outlet_is_not_a_person_and_counts_against_wear_and_accept(world):
    conn = world
    oid = _person(conn, "901", "gadgetradar", "GadgetRadar", "A team of gadget obsessives. Part of @futureplc")
    stats = classify.run_classify(conn, db.new_run(conn, "classify"), candidate_ids=[oid])
    assert stats["not_a_person"] == 1
    assert db.one(conn, "SELECT category FROM candidates WHERE candidate_id=?", (oid,))["category"] == "other"
    against = db.rows(conn, "SELECT supports, polarity FROM evidence WHERE candidate_id=? AND kind='profile_signal' "
                            "AND text LIKE 'not a person%'", (oid,))
    assert sorted((r["supports"], r["polarity"]) for r in against) == [("accept", -1), ("wear", -1)]
    k = json.loads(db.one(conn, "SELECT json FROM classifications WHERE candidate_id=?", (oid,))["json"])
    assert "a team of" in k["not_a_person"]


def test_red_flag_files_crypto_promo_and_anonymous_accounts_as_other_and_reapplies_without_llm(world, monkeypatch):
    """discover.red_flag in classify: category 'other' plus a wear and an accept row against; reapply_guards reaches
    classifications stored before the guard, with no LLM call, and a second pass changes nothing."""
    conn = world
    crypto = _person(conn, "911", "degen_test", "Degen Dan", "Web3 degen | NFA | $SOL maxi")
    anon = _person(conn, "912", "matt503ea5sf9z5", "Matt", "recursive self improvement enjoyer")
    known = _person(conn, "913", "matt5039999", "Matt", "recursive self improvement enjoyer", map_role="Reporter")
    classify.run_classify(conn, db.new_run(conn, "classify"), candidate_ids=[crypto, anon, known])
    for pid, kind in ((crypto, "crypto"), (anon, "anonymous")):
        assert db.one(conn, "SELECT category FROM candidates WHERE candidate_id=?", (pid,))["category"] == "other"
        against = db.rows(conn, "SELECT supports, polarity FROM evidence WHERE candidate_id=? AND text LIKE 'red flag%'", (pid,))
        assert sorted((r["supports"], r["polarity"]) for r in against) == [("accept", -1), ("wear", -1)]
        k = json.loads(db.one(conn, "SELECT json FROM classifications WHERE candidate_id=?", (pid,))["json"])
        assert k["red_flag"].startswith(kind)
    assert not db.rows(conn, "SELECT 1 FROM evidence WHERE candidate_id=? AND text LIKE 'red flag%'", (known,))   # in the map
    # as stored before the guard existed: the model's category, no rows against
    row = db.one(conn, "SELECT run_id, json FROM classifications WHERE candidate_id=?", (crypto,))
    k = json.loads(row["json"])
    k.pop("red_flag"), k.update(category="x_voice")
    k["_meta"]["notes"] = [n for n in k["_meta"]["notes"] if not n.startswith("red flag")]
    conn.execute("UPDATE classifications SET json=? WHERE candidate_id=? AND run_id=?", (json.dumps(k), crypto, row["run_id"]))
    conn.execute("UPDATE candidates SET category='x_voice' WHERE candidate_id=?", (crypto,))
    conn.execute("DELETE FROM evidence WHERE candidate_id=? AND text LIKE 'red flag%'", (crypto,))
    conn.commit()
    monkeypatch.setattr(llm, "complete_json", lambda *a, **kw: pytest.fail("reapply_guards must not call the LLM"))
    stats = classify.reapply_guards(conn, db.new_run(conn, "classify"), candidate_ids=[crypto, anon, known])
    assert stats["red_flag_new"] == 1 and stats["category_changed"] == 1
    assert db.one(conn, "SELECT category FROM candidates WHERE candidate_id=?", (crypto,))["category"] == "other"
    assert len(db.rows(conn, "SELECT 1 FROM evidence WHERE candidate_id=? AND text LIKE 'red flag%'", (crypto,))) == 2
    again = classify.reapply_guards(conn, db.new_run(conn, "classify"), candidate_ids=[crypto, anon, known])
    assert again["red_flag_new"] == 0 and again["category_changed"] == 0


def test_not_a_person_reads_badge_bio_and_the_classifiers_own_words():
    assert classify.not_a_person({"verified_type": "business", "bio": "I review gadgets"}, "x", None) == "X 'business' badge"
    assert classify.not_a_person({"name": "Ultrahuman", "bio": ""}, "ultrahumanhq", None) == "Ultrahuman's own brand account"
    outlet = "BGR is a 128k-follower tech publication; it is an outlet account, not a personal user."
    assert classify.not_a_person({"bio": "Expert analysis of the leading stories in tech."}, "bgr", outlet)
    assert classify.not_a_person({"bio": "Muse in the wild: real use cases from people"}, "musecases", None)
    assert classify.not_a_person({"bio": ""}, "fanpage", "Fan/showcase account for Meta's Muse agent")
    # a person is never an outlet, whatever the summary says: first person or a personal role in the bio wins
    assert classify.not_a_person({"bio": "Tech reviews and news @business. Previously @verge"}, "chriswelch",
                                 "Bloomberg reviewer; writes for a tech publication") is None
    assert classify.not_a_person({"bio": "we can only see what we think is possible..."}, "brianroemmele",
                                 "489k-follower X voice who posts about voice-first AI devices") is None


def test_post_sellers_get_a_love_row_against(world):
    conn = world
    pid = _person(conn, "902", "aicollabs", "Parul Example", "300K+ AI Community | Sharing AI tools daily",
                  location="📩 DM for Collabs")
    stats = classify.run_classify(conn, db.new_run(conn, "classify"), candidate_ids=[pid])
    assert stats["sells_posts"] == 1
    rows = db.rows(conn, "SELECT supports, polarity FROM evidence WHERE candidate_id=? AND text LIKE 'sells posts%'", (pid,))
    assert [(r["supports"], r["polarity"]) for r in rows] == [("love", -1)]


def test_a_dropped_device_counts_against_wear():
    dossier = {"profile_url": "https://x.com/h", "posts": [{"url": "https://x.com/h/status/1"}]}
    cls = {"wearable_user": {"value": True, "evidence": [
        {"url": "https://x.com/h/status/1", "quote": "I bought the Rabbit R1 and have used it maybe twice.", "date": None}]}}
    rows = classify._evidence_rows(dossier, cls, None, "", "unknown", "", "2026-09-29T00:00:00Z")
    assert {(r["supports"], r["polarity"]) for r in rows} == {("wear", -1), ("accept", -1)}


def test_map_entry_outside_the_us_sets_us_based_false(world):
    conn = world
    pid = _person(conn, "903", "reporter_in", "Ivan Example", "Consumer tech reporter @TechCrunch",
                  map_role="Reporter, TechCrunch (India)")
    classify.run_classify(conn, db.new_run(conn, "classify"), candidate_ids=[pid])           # the fixture answer claims US
    assert db.one(conn, "SELECT us_based FROM candidates WHERE candidate_id=?", (pid,))["us_based"] == 0
    assert classify.map_non_us({"role": "Reporter, Washington Post", "company": "WaPo"}) is None


def test_summary_clause_outside_the_us_sets_us_based_false(world, monkeypatch):
    """QA of run 30: '@damonchen ... posts place him in China, not the US' kept us_based empty and ranked #26."""
    conn = world
    pid = _person(conn, "904", "founder_sh", "Dan Example", "💜 links")                      # no location, no map entry
    answer = json.loads(json.dumps(FIXTURE))
    answer.update(us_based=None, us_based_evidence=None,
                  summary="Founder with 99k followers who dictates daily; posts place him in China, not the US.")
    monkeypatch.setattr(llm, "complete_json", lambda prompt, schema, **kw: answer)
    classify.run_classify(conn, db.new_run(conn, "classify"), candidate_ids=[pid])
    assert db.one(conn, "SELECT us_based FROM candidates WHERE candidate_id=?", (pid,))["us_based"] == 0
    stats = classify.reapply_guards(conn, db.new_run(conn, "classify"), candidate_ids=[pid])  # stable on a second pass
    assert stats["reapplied"] == 1
    assert db.one(conn, "SELECT us_based FROM candidates WHERE candidate_id=?", (pid,))["us_based"] == 0
    assert classify.summary_non_us("Hangzhou-based founder of COLERE who posted about HumanPods")
    # hedged, split, an employer's city, or a US place in the same clause: unknown, not a guess
    for s in ("Tech news account (182k followers, likely India-based) covering phones",
              "Pre-seed VC; caveat: location split across India and SF, no wearable evidence",
              "NYC reporter who writes for a London-based outlet",
              "SF/London-based founder of a voice startup",
              "San Francisco founder-CEO of Typeless; not a ring user"):
        assert classify.summary_non_us(s) is None, s


def test_indian_city_codes_count_in_the_location_field():
    assert classify.precheck_us_based("sf / blr")[0] is None                                 # split, not SF only
    assert classify.precheck_us_based("BOM/SFO")[0] is None
    assert classify.precheck_us_based("BLR/GJ")[0] is False
    assert classify.precheck_us_based("Del Mar, CA")[0] is True


def test_voice_dictation_builders_are_flagged_in_wispr_flows_category(world, monkeypatch):
    """QA of run 30: founders of Typeless, TypeVoice, Flit and Aside had no competitor flag."""
    conn = world
    pid = _person(conn, "905", "flit_founder", "Atta Example", "Solo founder + AI. Activity Monitor for Mac")
    answer = json.loads(json.dumps(FIXTURE))
    answer["summary"] = "Solo founder of Flit, a Mac dictation app competing with Wispr Flow, and a daily dictation user."
    monkeypatch.setattr(llm, "complete_json", lambda prompt, schema, **kw: answer)
    classify.run_classify(conn, db.new_run(conn, "classify"), candidate_ids=[pid])
    c = db.one(conn, "SELECT competitor_flag, competitor_detail FROM candidates WHERE candidate_id=?", (pid,))
    assert c["competitor_flag"] == 1 and c["competitor_detail"].startswith("Wispr Flow rival")
    assert classify.builds_voice_rival(None, "Serial founder (vid.ai, tubemagic, TypeVoice dictation app) who runs agents")
    assert classify.builds_voice_rival("Founder & CEO. Building Typeless, a voice dictation app", None)
    # users of such apps are not builders
    assert classify.builds_voice_rival(None, "Founder of Matter who dictates with FluidVoice, a dictation app") is None
    assert classify.builds_voice_rival(None, "SF engineer who dictates with Wispr Flow; builds coding-agent tools") is None
    stats = classify.reapply_guards(conn, db.new_run(conn, "classify"), candidate_ids=[pid])
    assert stats["voice_rival_new"] == 0                                                     # already applied: idempotent
    assert db.one(conn, "SELECT competitor_flag FROM candidates WHERE candidate_id=?", (pid,))["competitor_flag"] == 1


def test_a_role_at_a_competitor_in_the_bio_is_flagged_whatever_the_model_said(world, monkeypatch):
    """Federico, 30 Sep: competitors are flagged, never removed. Enrichment no longer skips their people, so their flag
    must not rest on the model alone: a bio that states the tie ('Product lead @ouraring', 'http://friend.com @friend')
    sets it. The model's own answer, when it names the tie, is kept."""
    conn = world
    cora = cid(conn, "competitor_test")                                                      # "Product lead @ouraring"
    fran = _person(conn, "906", "fran_builds", "Fran Example", "http://friend.com @friend")
    stats = classify.run_classify(conn, db.new_run(conn, "classify"), candidate_ids=[cora, fran])   # the fixture: no tie
    assert stats["competitor_flagged"] == 2
    q = "SELECT category, competitor_flag, competitor_detail FROM candidates WHERE candidate_id=?"
    assert db.one(conn, q, (cora,)) == {"category": "x_voice", "competitor_flag": 1,           # flagged, not removed
                                        "competitor_detail": "Oura (role stated in the bio)"}
    assert db.one(conn, q, (fran,))["competitor_detail"] == "Friend (role stated in the bio)"
    stats = classify.reapply_guards(conn, db.new_run(conn, "classify"), candidate_ids=[cora, fran])
    assert stats["reapplied"] == 2 and stats["competitor_role_new"] == 0                    # already applied: idempotent
    answer = json.loads(json.dumps(FIXTURE))
    answer["competitor_tie"] = {"value": True, "which": "Oura", "kind": "employee", "evidence": []}
    monkeypatch.setattr(llm, "complete_json", lambda prompt, schema, **kw: answer)
    classify.run_classify(conn, db.new_run(conn, "classify"), candidate_ids=[cora])
    assert db.one(conn, q, (cora,))["competitor_detail"] == "Oura (employee)"



def test_reapply_guards_needs_no_llm_and_is_idempotent(world, monkeypatch):
    """The QA guards reach classifications stored before them, from the stored JSON alone."""
    conn = world
    oid = _person(conn, "903", "gadgetradar", "GadgetRadar", "A team of gadget obsessives. Part of @futureplc")
    rid = cid(conn, "ringnerd_test")
    classify.run_classify(conn, db.new_run(conn, "classify"), candidate_ids=[oid, rid])
    # as stored before the guards existed: the outlet kept the model's category and has no rows against it
    row = db.one(conn, "SELECT run_id, json FROM classifications WHERE candidate_id=?", (oid,))
    k = json.loads(row["json"])
    k.pop("not_a_person"), k.update(category="media")
    conn.execute("UPDATE classifications SET json=? WHERE candidate_id=? AND run_id=?", (json.dumps(k), oid, row["run_id"]))
    conn.execute("UPDATE candidates SET category='media' WHERE candidate_id=?", (oid,))
    conn.execute("DELETE FROM evidence WHERE candidate_id=? AND text LIKE 'not a person%'", (oid,))
    # an earlier answer filed post 101 for love; the latest answer read post 101 and does not cite it for love
    stale = ("https://x.com/ringnerd_test/status/101", "an earlier answer's quote")
    unseen = ("https://x.com/ringnerd_test/status/777", "a post the latest answer never saw")
    for url, text in (stale, unseen):
        conn.execute("INSERT INTO evidence (candidate_id, kind, supports, polarity, weight, url, date, text, source, fetched_at) "
                     "VALUES (?, 'post', 'love', 1, 1.0, ?, '2025-01-01', ?, 'llm:old', '2025-01-02')", (rid, url, text))
    conn.commit()
    monkeypatch.setattr(llm, "complete_json", lambda *a, **kw: pytest.fail("reapply_guards must not call the LLM"))
    stats = classify.reapply_guards(conn, db.new_run(conn, "classify"))
    assert stats["not_a_person_new"] == 1 and stats["category_changed"] == 1 and stats["evidence_pruned"] == 1
    assert db.one(conn, "SELECT category FROM candidates WHERE candidate_id=?", (oid,))["category"] == "other"
    against = db.rows(conn, "SELECT supports FROM evidence WHERE candidate_id=? AND text LIKE 'not a person%'", (oid,))
    assert sorted(r["supports"] for r in against) == ["accept", "wear"]
    texts = {r["text"] for r in db.rows(conn, "SELECT text FROM evidence WHERE candidate_id=? AND supports='love'", (rid,))}
    assert stale[1] not in texts and unseen[1] in texts
    wear = db.one(conn, "SELECT COUNT(*) AS n FROM evidence WHERE candidate_id=? AND supports='wear'", (rid,))["n"]
    assert wear > 0                                                             # the rows the answer cites stay
    k2 = json.loads(db.one(conn, "SELECT json FROM classifications WHERE candidate_id=? ORDER BY run_id DESC", (oid,))["json"])
    assert k2["_meta"]["llm_category"] == "media" and k2["category"] == "other"
    again = classify.reapply_guards(conn, db.new_run(conn, "classify"))       # a second pass changes nothing
    assert again["evidence_pruned"] == 0 and again["category_changed"] == 0 and again["not_a_person_new"] == 0
