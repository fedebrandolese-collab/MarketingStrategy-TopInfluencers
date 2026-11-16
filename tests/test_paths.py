"""Workstream F: handle resolution, paths and export. Offline: FixtureSource + data/fixtures via map_conn."""
import csv
import json
import re

import pytest

from wearable_wishlist import config, db
from wearable_wishlist.export import engagement_factor, engagement_json, run_export
from wearable_wishlist.paths import MUTUAL_FOLLOW_CAP, firm_in_text, run_paths, warmth_ranks
from wearable_wishlist.resolve_handles import (company_in_text, map_text_handles, run_resolve_handles, score_hit,
                                     seed_user)
from wearable_wishlist.sources.base import XUser, persist_follows, persist_posts, persist_users

NOW = "2026-09-29T12:00:00Z"
SEED = "seed_test"


@pytest.fixture
def seeded(map_conn, fixture_source, monkeypatch):
    """map fixtures + the operator's X graph from the fixture source + map candidates."""
    monkeypatch.setenv("SEED_X_HANDLE", SEED)
    operator_user = fixture_source.get_user_by_handle(SEED)
    persist_users(map_conn, [operator_user], "fixture")
    persist_follows(map_conn, operator_user.x_id, fixture_source.get_following(SEED), "following", "fixture")
    persist_follows(map_conn, operator_user.x_id, fixture_source.get_followers(SEED), "followers", "fixture")
    persist_users(map_conn, fixture_source.get_users_by_handles(["techwriter_test", "founder_test", "eu_test"]), "fixture")
    persist_posts(map_conn, fixture_source.search_posts("wearable"), "fixture")
    run_id = db.new_run(map_conn, "test")
    run_resolve_handles(map_conn, run_id, fixture_source)
    return map_conn, run_id


def _add_map_person(conn, **p):
    row = {"group_name": "capital", "company": None, "role": None, "sector": "vc", "tags": "", "warmth": 1.0, "intros_made": 0,
           "intros_got": 0, "interactions": 0, "sent": 0, "received": 0, "meetings": 0, "channels": "", "first_contact": None,
           "last_contact": None, "imported_at": db.utcnow()}
    row.update(p)
    db.upsert(conn, "map_people", row, keys=("id",))
    conn.commit()


def _candidate(conn, **where):
    cond = " AND ".join(f"{k}=?" for k in where)
    return db.one(conn, f"SELECT * FROM candidates WHERE {cond}", tuple(where.values()))


def _paths(conn, run_id, candidate_id):
    return db.rows(conn, "SELECT * FROM paths WHERE run_id=? AND candidate_id=? ORDER BY rank", (run_id, candidate_id))


# ---------------- resolve_handles ----------------

def test_resolve_does_not_invent_and_seeds_map_candidates(map_conn, fixture_source):
    run_id = db.new_run(map_conn, "test")
    res = run_resolve_handles(map_conn, run_id, fixture_source)
    # none of the 9 fixture map people exist in the X fixtures: nothing stored, nothing guessed
    assert res["resolved"] == 0
    assert db.one(map_conn, "SELECT COUNT(*) AS n FROM map_x_handles")["n"] == 0
    assert res["people"] == 9 and res["searched"] == 9
    # every processed map person becomes a candidate with map_person_id and the sector -> category mapping
    cands = db.rows(map_conn, "SELECT * FROM candidates")
    assert len(cands) == 9 and all(c["discovered_via"] == "map" and c["map_person_id"] for c in cands)
    by_id = {c["map_person_id"]: c for c in cands}
    assert by_id[391]["category"] == "vc"                 # Roberto Veronese, sector vc
    assert by_id[51]["category"] == "founder_angel"       # Adrian Aoun, sector angel
    assert by_id[5243]["category"] == "media"             # Daniel Cooper, press
    assert by_id[51]["x_id"] is None and by_id[51]["handle"] is None
    assert by_id[391]["linkedin_url"] == "https://www.linkedin.com/in/veronese"
    # idempotent: a second run does not duplicate candidates
    run_resolve_handles(map_conn, db.new_run(map_conn, "test"), fixture_source)
    assert db.one(map_conn, "SELECT COUNT(*) AS n FROM candidates")["n"] == 9


def test_resolve_picks_corroborated_match_and_skips_parody_and_weak(map_conn, fixture_source):
    # 'Vera Capital' exists in the X fixtures with company and role in the bio -> resolved
    _add_map_person(map_conn, id=9001, name="Vera Capital", company="Example Ventures", role="Partner", sector="vc",
                    tags="vc,x_following", warmth=50)
    # 'Rina Wearable' has a parody twin in the fixtures -> the real one wins
    _add_map_person(map_conn, id=9002, name="Rina Wearable", company="WearableNerd", role="Reviewer", sector="press", tags="press", warmth=5)
    # 'Wearable' is a substring of several fixture names but similar to none -> nothing stored
    _add_map_person(map_conn, id=9003, name="Wearable", company="Nothing Corp", role="VC", sector="vc", tags="vc", warmth=3)
    run_id = db.new_run(map_conn, "test")
    res = run_resolve_handles(map_conn, run_id, fixture_source)
    handles = {r["person_id"]: r for r in db.rows(map_conn, "SELECT * FROM map_x_handles")}
    assert set(handles) == {9001, 9002}
    assert handles[9001]["handle"] == "vc_test" and handles[9001]["x_id"] == "4" and handles[9001]["confidence"] >= 0.8
    assert handles[9002]["handle"] == "wearablenerd_test" and handles[9002]["x_id"] == "2"
    assert res["resolved"] == 2 and res["skipped_low_confidence"] >= 1
    # the resolved handle lands on the candidate row, the found users are persisted
    c = _candidate(map_conn, map_person_id=9001)
    assert c["handle"] == "vc_test" and c["x_id"] == "4" and c["category"] == "vc"
    assert db.one(map_conn, "SELECT x_id FROM x_users WHERE handle='vc_test'")
    assert _candidate(map_conn, map_person_id=9003)["x_id"] is None


def test_score_hit_gates_on_name():
    person = {"name": "Roberto Veronese", "company": "Openwell", "role": "Founder/Advisor", "sector": "vc"}
    wrong = XUser(x_id="77", handle="marco_v", name="Marco Veronese", bio="Founder at Openwell. VC investor.", verified=True)
    conf, reasons = score_hit(person, wrong)
    assert conf < 0.6 and "name too different" in reasons
    exact = XUser(x_id="78", handle="rveronese", name="Roberto Veronese", bio="Co-founder Openwell", verified=False)
    conf2, _ = score_hit(person, exact)
    assert conf2 >= 0.6
    parody = XUser(x_id="79", handle="rveronese_parody", name="Roberto Veronese", bio="Parody account. Openwell founder.", verified=False)
    assert score_hit(person, parody)[0] < 0.6


@pytest.mark.parametrize("company,bio,firm", [
    # QA runs 14 and 39: investors, press, board seats and past employers are not "same firm"
    ("a16z", "Founder @agentsky (a16z backed)", None),
    ("a16z", "Founder of an a16z-backed startup", None),
    ("a16z", "Building Acme. Backed by Sequoia, a16z and YC", None),
    ("TechCrunch", "AI Insider / Reporter as featured in Axios • The Information • NYT • Techcrunch", None),
    ("Mashable", "Senior Editor @gizmodo | Prev: Deputy Tech Editor @inversedotcom, Senior Correspondent @mashable", None),
    ("Fast Company", "The automated life | Ex-Microsoft, Rackspace, Fast Company | Wrote eight books", None),
    ("The Verge", "Tech reviews and news @business. Previously @verge for a long time before that.", None),
    ("Snowflake", "Open-sourcerer. formerly Snowflake", None),
    ("Deel", "AI Apps investing @ A16Z; Boards of Ethos, Krea, Deel, Clutch", None),
    ("Time", "Director of Teaching @NewmarkJSchool | Past @Time", None),
    ("a16z", "stealth consumer bio | prev founding team @a16z @speedrun", None),
    ("a16z", "Founder @acme, a16z speedrun alum", None),
    # a current employer still counts
    ("Gizmodo", "Senior Editor @gizmodo | Prev: Deputy Tech Editor @inversedotcom", "gizmodo"),
    ("a16z", "Ex-Google, now partner @a16z", "a16z"),
    ("a16z", "Partner @a16z, ex-Google", "a16z"),
    ("Acme Robotics", "CEO @ Acme Robotics, backed by YC", "acme robotics"),
    ("Flybridge", "GP @Flybridge. $1M-$3M checks into founders", "flybridge"),
])
def test_firm_in_text_skips_investors_press_boards_and_past_employers(company, bio, firm):
    assert firm_in_text(company, bio) == firm


def test_company_in_text():
    assert company_in_text("Example Ventures", "Partner @ExampleVentures. Consumer hardware.") == "exampleventures"
    assert company_in_text("Fractal Capital", "GP at Fractal, investing in devices") == "fractal"
    assert company_in_text("True Ventures", "I love true stories") is None
    assert company_in_text("Align Fund", "Align your life") is None


class SpySource:
    """Wraps the fixture source, records every paid-style call; `name` makes the stage treat it as paid or free."""
    def __init__(self, inner, name="fixture", error=None):
        self.inner, self.name, self.error, self.queries = inner, name, error, []

    def search_users(self, query, max_results=10):
        self.queries.append(query)
        if self.error:
            raise self.error
        return self.inner.search_users(query, max_results)

    def get_user_by_handle(self, handle):
        self.queries.append("@" + handle)
        if self.error:
            raise self.error
        return self.inner.get_user_by_handle(handle)


def _seed_graph(conn, fixture_source):
    operator_user = fixture_source.get_user_by_handle(SEED)
    persist_users(conn, [operator_user], "fixture")
    persist_follows(conn, operator_user.x_id, fixture_source.get_following(SEED), "following", "fixture")   # 2,3,4,5,8
    persist_follows(conn, operator_user.x_id, fixture_source.get_followers(SEED), "followers", "fixture")   # 2,4,8


def test_seed_follow_list_resolves_tagged_people_free(map_conn, fixture_source, monkeypatch):
    monkeypatch.setenv("SEED_X_HANDLE", "@Seed_Test")        # the real format: '@', mixed case
    _seed_graph(map_conn, fixture_source)
    # seed_test follows founder_test ('Frank Founder'); vc_test ('Vera Capital') follows seed_test
    _add_map_person(map_conn, id=9101, name="Frank Founder", company="VoiceThing", role="Founder", sector="founder",
                    tags="founder,x_following", warmth=10)
    _add_map_person(map_conn, id=9102, name="Vera Capital", company="Example Ventures", role="Partner", sector="vc",
                    tags="vc,x_follower", warmth=40)
    _add_map_person(map_conn, id=9103, name="Frank Miller", sector="vc", tags="vc,x_following", warmth=5)   # namesake
    _add_map_person(map_conn, id=9104, name="Frank", sector="vc", tags="x_following", warmth=4)             # one word
    spy = SpySource(fixture_source, name="x_api")
    res = run_resolve_handles(map_conn, db.new_run(map_conn, "test"), spy, max_paid_lookups=0)
    h = {r["person_id"]: r for r in db.rows(map_conn, "SELECT * FROM map_x_handles")}
    assert h[9101]["handle"] == "founder_test" and h[9101]["x_id"] == "5" and h[9101]["method"] == "seed_follow_list"
    assert h[9102]["handle"] == "vc_test" and h[9102]["x_id"] == "4" and h[9102]["method"] == "seed_follow_list"
    assert h[9101]["confidence"] >= 0.8 and h[9102]["confidence"] >= 0.9     # company + role words in the bio add up
    assert 9103 not in h and 9104 not in h                                   # never invent
    assert res["per_method"]["seed_follow_list_tagged"] == 2 and res["paid_lookups"] == 0 and spy.queries == []
    assert res["seed"] == {"handle": "seed_test", "found": True, "following": 5, "followers": 3}
    frank = _candidate(map_conn, map_person_id=9101)
    assert frank["x_id"] == "5" and frank["handle"] == "founder_test" and frank["handle_verified"] == "likely_real"
    # with paid lookups allowed, only the people the lists could not resolve are searched
    spy2 = SpySource(fixture_source, name="x_api")
    run_resolve_handles(map_conn, db.new_run(map_conn, "test"), spy2, max_paid_lookups=20)
    assert not any("Frank Founder" in q or "Vera Capital" in q for q in spy2.queries)
    assert any(q.startswith("frank miller") for q in spy2.queries)


def test_seed_follow_list_direction_and_strict_rule(map_conn, fixture_source, monkeypatch):
    monkeypatch.setenv("SEED_X_HANDLE", SEED)
    _seed_graph(map_conn, fixture_source)
    # tagged x_follower, but Tom is only followed by the operator: not matched on the tag's list, only by the strict rule
    _add_map_person(map_conn, id=9201, name="Tom Writer", company="GadgetDaily", role="Senior Editor", sector="press",
                    tags="press,x_follower", warmth=3)
    # untagged, exact name + company in the bio: strict rule
    _add_map_person(map_conn, id=9202, name="Rina Wearable", company="WearableNerd", role="Reviewer", sector="press", tags="press", warmth=2)
    # untagged, not in the operator's lists at all (Elke is neither followed by nor following the operator): nothing
    _add_map_person(map_conn, id=9203, name="Elke Uropa", company="Wearables Weekly", sector="press", tags="press", warmth=2)
    res = run_resolve_handles(map_conn, db.new_run(map_conn, "test"), SpySource(fixture_source, "x_api"), max_paid_lookups=0)
    h = {r["person_id"]: r for r in db.rows(map_conn, "SELECT * FROM map_x_handles")}
    assert h[9201]["handle"] == "techwriter_test" and h[9201]["method"] == "seed_follow_list+strict"
    assert h[9202]["handle"] == "wearablenerd_test" and h[9202]["method"] == "seed_follow_list+strict"
    assert 9203 not in h
    assert res["per_method"]["seed_follow_list_tagged"] == 1 and res["per_method"]["seed_follow_list_untagged"] == 1


def test_seed_user_accepts_real_handle_formats(conn, fixture_source):
    persist_users(conn, [fixture_source.get_user_by_handle(SEED)], "fixture")
    for h in ("@Seed_Test", "seed_test", "https://x.com/Seed_Test/", "twitter.com/seed_test"):
        assert seed_user(conn, h)["x_id"] == "1"
    assert seed_user(conn, "@nobody_here") is None


def test_paid_fallback_is_bounded_and_survives_403(map_conn, fixture_source):
    from wearable_wishlist.sources.x_api import XApiAuthError
    denied = SpySource(fixture_source, name="x_api",
                       error=XApiAuthError("X API users/search: 403 forbidden (endpoint not available to this token)"))
    res = run_resolve_handles(map_conn, db.new_run(map_conn, "test"), denied)
    assert res["paid_lookups"] == 1 and "403" in res["paid_stopped"] and len(denied.queries) == 1
    # no crawled lists: the 4 people the map tags as X contacts wait for the crawl instead of being bought
    assert res["deferred_to_seed_lists"] == 4
    assert res["resolved"] == 0 and res["candidates_upserted"] == 9          # the stage carried on
    counting = SpySource(fixture_source, name="x_api")
    res = run_resolve_handles(map_conn, db.new_run(map_conn, "test"), counting, max_paid_lookups=2)
    assert res["paid_lookups"] == 2 and len(counting.queries) == 2 and res["not_searched_cap"] >= 1


def test_map_person_found_by_discovery_is_one_candidate(map_conn, fixture_source, monkeypatch):
    from wearable_wishlist.discover import upsert_candidate
    monkeypatch.setenv("SEED_X_HANDLE", SEED)
    _add_map_person(map_conn, id=9101, name="Frank Founder", company="VoiceThing", role="Founder", sector="founder",
                    tags="founder,x_following", warmth=10)
    run_resolve_handles(map_conn, db.new_run(map_conn, "test"), fixture_source, max_paid_lookups=0)   # no lists yet
    map_row = _candidate(map_conn, map_person_id=9101)
    assert map_row["x_id"] is None
    now = db.utcnow()
    map_conn.execute("INSERT INTO status (candidate_id, status, updated_at) VALUES (?,?,?)", (map_row["candidate_id"], "approved", now))
    disc_id, created = upsert_candidate(map_conn, "5", "founder_test", "Frank Founder", "query:voice", "hint:founder", now)
    assert created and disc_id != map_row["candidate_id"]
    map_conn.execute("INSERT INTO evidence (candidate_id, kind, supports, url, text, source, fetched_at) VALUES (?,?,?,?,?,?,?)",
                     (disc_id, "post", "love", "https://x.com/founder_test/status/401", "Screens are the bug.", "fixture", now))
    map_conn.commit()
    _seed_graph(map_conn, fixture_source)
    res = run_resolve_handles(map_conn, db.new_run(map_conn, "test"), fixture_source, max_paid_lookups=0)
    rows = db.rows(map_conn, "SELECT * FROM candidates WHERE x_id='5' OR map_person_id=9101")
    assert len(rows) == 1 and res["candidates_merged"] == 1
    one = rows[0]
    assert one["candidate_id"] == disc_id and one["map_person_id"] == 9101 and one["discovered_via"] == "query:voice"
    assert "hint:founder" in one["notes"] and "map: founder" in one["notes"]
    assert db.one(map_conn, "SELECT status FROM status WHERE candidate_id=?", (disc_id,))["status"] == "approved"
    assert db.one(map_conn, "SELECT COUNT(*) AS n FROM evidence WHERE candidate_id=?", (disc_id,))["n"] == 1
    # discovery seeing the account again finds the same single row
    assert upsert_candidate(map_conn, "5", "founder_test", "Frank Founder", "seed", None, now) == (disc_id, False)


def test_resolve_keeps_other_stages_notes(map_conn, fixture_source, monkeypatch):
    """run_enrich's 'enriched:' marker (and discover's segments) must survive resolve, or enrichment is bought again."""
    from wearable_wishlist.discover import upsert_candidate
    monkeypatch.setenv("SEED_X_HANDLE", SEED)
    _seed_graph(map_conn, fixture_source)
    _add_map_person(map_conn, id=9101, name="Frank Founder", company="VoiceThing", sector="founder", tags="x_following", warmth=10)
    now = db.utcnow()
    cid, _ = upsert_candidate(map_conn, "5", "founder_test", "Frank Founder", "seed", "hint:founder kw:angel", now)
    map_conn.execute("UPDATE candidates SET notes=notes || ' | enriched:2026-09-29 posts=30' WHERE candidate_id=?", (cid,))
    adrian_map_row = _candidate(map_conn, map_person_id=51)
    assert adrian_map_row is None
    run_resolve_handles(map_conn, db.new_run(map_conn, "test"), fixture_source, max_paid_lookups=0)
    adrian = _candidate(map_conn, map_person_id=51)            # a map-only row enriched later...
    map_conn.execute("UPDATE candidates SET notes=notes || ' | enriched:2026-09-28 posts=0' WHERE candidate_id=?",
                     (adrian["candidate_id"],))
    map_conn.commit()
    for _ in range(2):                                         # ...and re-runs keep every foreign segment, one map: segment
        run_resolve_handles(map_conn, db.new_run(map_conn, "test"), fixture_source, max_paid_lookups=0)
    frank = db.one(map_conn, "SELECT * FROM candidates WHERE candidate_id=?", (cid,))
    assert frank["map_person_id"] == 9101
    segs = frank["notes"].split(" | ")
    assert "enriched:2026-09-29 posts=30" in segs and "hint:founder kw:angel" in segs
    assert sum(s.startswith("map:") for s in segs) == 1
    notes = _candidate(map_conn, map_person_id=51)["notes"].split(" | ")
    assert "enriched:2026-09-28 posts=0" in notes and sum(s.startswith("map:") for s in notes) == 1


def test_map_text_handles_confirmed_by_profile(map_conn, fixture_source):
    assert map_text_handles({"name": "Scott Webster / Andrew Allen", "role": "Editor, AndroidGuys (@swebster77)"}) == ["swebster77"]
    assert map_text_handles({"name": "Greg", "role": "YouTuber, youtube.com/@gregtoope; alt dan@engadget.com"}) == []
    persist_users(map_conn, fixture_source.get_users_by_handles(["wearablenerd_test", "techwriter_test"]), "fixture")
    _add_map_person(map_conn, id=9301, name="Rina Wearable", role="Reviewer (@WearableNerd_Test)", sector="press", tags="press", warmth=2)
    # the handle in the text belongs to someone else: rejected, not guessed
    _add_map_person(map_conn, id=9302, name="Paula Press", role="Host (@techwriter_test)", sector="press", tags="press", warmth=2)
    # no profile to check, but the handle carries the name: stored as unconfirmed, x_id unknown
    _add_map_person(map_conn, id=9303, name="Jason Howell", role="Host, AI Inside / YouTube @JasonHowell", sector="press",
                    tags="press", warmth=2)
    res = run_resolve_handles(map_conn, db.new_run(map_conn, "test"), SpySource(fixture_source, "x_api"), max_paid_lookups=0)
    h = {r["person_id"]: r for r in db.rows(map_conn, "SELECT * FROM map_x_handles")}
    assert h[9301]["method"] == "map_text" and h[9301]["x_id"] == "2" and h[9301]["confidence"] == 0.98
    assert 9302 not in h
    assert h[9303]["handle"] == "jasonhowell" and h[9303]["x_id"] is None and h[9303]["confidence"] < 1
    assert res["per_method"]["map_text"] == 2
    assert _candidate(map_conn, map_person_id=9303)["handle"] == "jasonhowell"


def test_map_text_paid_lookup_confirms_or_disproves(map_conn, fixture_source):
    _add_map_person(map_conn, id=9401, name="Frank Founder", role="Angel (@founder_test)", sector="angel", tags="", warmth=3)
    _add_map_person(map_conn, id=9402, name="Jane Nosuch", role="Writer (@janenosuch)", sector="press", tags="", warmth=3)
    spy = SpySource(fixture_source, name="x_api")
    res = run_resolve_handles(map_conn, db.new_run(map_conn, "test"), spy, max_paid_lookups=5)
    h = {r["person_id"]: r for r in db.rows(map_conn, "SELECT * FROM map_x_handles")}
    assert h[9401]["x_id"] == "5" and h[9401]["confidence"] == 0.98 and h[9401]["method"] == "map_text"
    assert 9402 not in h                                    # X says the handle does not exist: disproved, not stored
    assert "@founder_test" in spy.queries and "@janenosuch" in spy.queries and res["paid_lookups"] <= 5


# ---------------- paths ----------------

def test_direct_path_for_map_person(seeded):
    conn, run_id = seeded
    res = run_paths(conn, run_id, now=NOW)
    assert res["candidates"] >= 9 and res["seed_found"]
    adrian = _candidate(conn, map_person_id=51)
    ps = _paths(conn, run_id, adrian["candidate_id"])
    assert ps[0]["how"] == "direct" and ps[0]["strength"] >= 0.6
    assert re.fullmatch(r"met · 3 meetings · 84 touches · last \d+d ago", ps[0]["you_rel"])
    obj = json.loads(ps[0]["json"])
    assert {"how", "you", "via", "warmth", "target", "strength"} <= set(obj)
    assert obj["warmth"] == 3 and obj["via"] is None
    # silent 90+ days reads as faded and is weaker than a fresh relationship of similar warmth
    marwan = _candidate(conn, map_person_id=254)
    mp = _paths(conn, run_id, marwan["candidate_id"])[0]
    assert "faded" in mp["you_rel"] and mp["strength"] < ps[0]["strength"]


def test_via_path_from_map_intro_edge(seeded):
    conn, run_id = seeded
    run_paths(conn, run_id, now=NOW)
    josh = _candidate(conn, map_person_id=360)          # Josh Constine, introduced by Marwan (edge 254 -> 360, imessage)
    ps = _paths(conn, run_id, josh["candidate_id"])
    via = [p for p in ps if p["how"] == "via"]
    assert via and via[0]["via_person_id"] == 254 and via[0]["via_name"] == "Marwan Refaat"
    assert via[0]["target_rel"] == "introduced them (map, imessage)"
    assert via[0]["you_rel"].startswith("in touch · 217 touches · last") and "faded" in via[0]["you_rel"]
    obj = json.loads(via[0]["json"])
    assert obj["warmth"] == 3 and obj["target"] == via[0]["target_rel"]
    assert 0.3 < via[0]["strength"] < 0.9
    # Max Marchione introduced Marwan (83 -> 254 vc_sheet): Marwan's candidate has a via path through Max
    marwan = _candidate(conn, map_person_id=254)
    v = [p for p in _paths(conn, run_id, marwan["candidate_id"]) if p["how"] == "via"]
    assert any(p["via_person_id"] == 83 and p["target_rel"] == "introduced them (map, vc_sheet)" for p in v)


def test_mutual_follow_with_connector_is_low_strength(seeded):
    conn, run_id = seeded
    # pretend Marwan's X account is vc_test (x_id 4); founder_test (5) and vc_test follow each other in the fixtures
    db.upsert(conn, "map_x_handles", {"person_id": 254, "handle": "vc_test", "x_id": "4", "confidence": 0.9,
                                      "method": "manual", "resolved_at": db.utcnow()}, keys=("person_id",))
    for a, b in (("4", "5"), ("5", "4")):
        db.upsert(conn, "x_follows", {"src_x_id": a, "dst_x_id": b, "source": "fixture", "fetched_at": db.utcnow()}, keys=("src_x_id", "dst_x_id"))
    conn.execute("INSERT INTO candidates (x_id, handle, name, category, discovered_via, first_seen, last_seen) VALUES ('5','founder_test','Frank Founder','founder_angel','query:test',?,?)", (db.utcnow(), db.utcnow()))
    conn.commit()
    run_paths(conn, run_id, now=NOW)
    frank = _candidate(conn, x_id="5")
    ps = _paths(conn, run_id, frank["candidate_id"])
    assert ps[0]["how"] == "via" and ps[0]["target_rel"] == "follow each other" and ps[0]["via_person_id"] == 254
    assert ps[0]["strength"] <= MUTUAL_FOLLOW_CAP
    assert any(p["how"] == "open_dm" and p["strength"] == 0.15 for p in ps)
    # two replies from Frank to Marwan's handle make it a conversation: a real path, above the follow cap
    for i in (1, 2):
        db.upsert(conn, "x_posts", {"post_id": f"90{i}", "x_id": "5", "handle": "founder_test", "text": "re: wearables",
                                    "in_reply_to_x_id": "4", "mentions": "[]", "created_at": NOW}, keys=("post_id",))
    conn.commit()
    run_paths(conn, run_id, now=NOW)
    ps = _paths(conn, run_id, frank["candidate_id"])
    assert ps[0]["how"] == "via" and ps[0]["target_rel"] == "X conversation · 2 posts" and ps[0]["strength"] > MUTUAL_FOLLOW_CAP
    assert "follow each other" in ps[0]["evidence"]


def test_x_direct_open_dm_and_none(seeded):
    conn, run_id = seeded
    now = db.utcnow()
    conn.execute("INSERT INTO candidates (x_id, handle, name, category, discovered_via, first_seen, last_seen) VALUES "
                 "('2','wearablenerd_test','Rina Wearable','x_voice','query:test',?,?), ('3','techwriter_test','Tom Writer','media','query:test',?,?)",
                 (now, now, now, now))
    conn.commit()
    run_paths(conn, run_id, now=NOW)
    rina = _paths(conn, run_id, _candidate(conn, x_id="2")["candidate_id"])       # mutual follow with the operator, DMs open
    assert rina[0]["how"] == "x_direct" and rina[0]["you_rel"] == "follow each other on X" and 0.3 < rina[0]["strength"] <= 0.55
    assert [p["how"] for p in rina] == ["x_direct", "open_dm"]
    tom = _paths(conn, run_id, _candidate(conn, x_id="3")["candidate_id"])        # the operator follows him, no follow back, DMs closed
    assert [p["how"] for p in tom] == ["none"] and tom[0]["strength"] == 0.05
    # the map's x_follower tag counts as x_direct for a map person (Nik Shevchenko, 407)
    nik = _paths(conn, run_id, _candidate(conn, map_person_id=407)["candidate_id"])
    assert any(p["how"] == "x_direct" and "x_follower" in p["you_rel"] for p in nik)
    assert len(nik) <= 4 and all(nik[i]["strength"] >= nik[i + 1]["strength"] for i in range(len(nik) - 1))


def test_warmth_buckets_are_quartiles():
    people = [{"id": i, "warmth": 100 - i} for i in range(1, 9)] + [{"id": 99, "warmth": 0}]
    r = warmth_ranks(people)
    assert [r[i]["bucket"] for i in range(1, 9)] == [3, 3, 2, 2, 1, 1, 0, 0]
    assert 99 not in r and r[1]["pct"] == 1.0 and r[8]["pct"] == 0.0


# ---------------- export ----------------

def test_export_shape(seeded, tmp_path):
    conn, run_id = seeded
    now = db.utcnow()
    conn.execute("INSERT INTO candidates (x_id, handle, name, category, discovered_via, first_seen, last_seen) VALUES "
                 "('2','wearablenerd_test','Rina Wearable','x_voice','query:test',?,?)", (now, now))
    conn.commit()
    run_paths(conn, run_id, now=NOW)
    rina = _candidate(conn, x_id="2")["candidate_id"]
    adrian = _candidate(conn, map_person_id=51)["candidate_id"]
    wears = {"p_wear": {"value": 0.8, "prior": 0.3, "backed": True}}   # Rina's wearables: net positive evidence
    for cid, rank, total, factors in ((rina, 1, 31.2, wears), (adrian, 2, 12.5, {})):
        db.upsert(conn, "scores", {"run_id": run_id, "candidate_id": cid, "p_accept": 0.7, "p_wear": 0.8, "p_love": 0.6, "impact": 0.7,
                                   "reach": 0.6, "confidence": 1.0, "total": total, "rank": rank, "flags": json.dumps(["unverified_handle"]),
                                   "reasons_json": json.dumps({"reasons": ["wears two wearables"], "evidence_ids": [1], "factors": factors})},
                  keys=("run_id", "candidate_id"))
    conn.execute("INSERT INTO evidence (candidate_id, kind, supports, url, date, text, source, fetched_at) VALUES (?,?,?,?,?,?,?,?)",
                 (rina, "post", "wear", "https://x.com/wearablenerd_test/status/101", "2026-09-20", "Unboxing the new Oura Wearable 5", "fixture", now))
    conn.execute("INSERT INTO classifications (candidate_id, run_id, json, model, created_at) VALUES (?,?,?,?,?)",
                 (rina, run_id, json.dumps({"summary": "Wearables reviewer who already wears two wearables."}), "fixture", now))
    conn.execute("INSERT INTO status (candidate_id, status, updated_at) VALUES (?,?,?)", (rina, "approved", now))
    conn.execute("INSERT INTO changes (run_id, candidate_id, field, old_value, new_value, reason) VALUES (?,?,?,?,?,?)",
                 (run_id, rina, "new_candidate", None, "31.2", "New: posted an Oura unboxing on 2026-09-20"))
    conn.commit()

    res = run_export(conn, run_id, out_dir=tmp_path)   # the wishlist: only people who meet the brief's bar
    assert res["rows"] >= 10 and res["scored"] == 2 and res["changes"] == 1 and (res["wishlist"], res["meets_bar"]) == (1, 1)
    bar_rows = json.loads((tmp_path / "wishlist.json").read_text())
    assert [(r["n"], r["name"], r["meets_bar"], r["bar_reason"]) for r in bar_rows] == [(1, "Rina Wearable", True, "")]
    with (tmp_path / "wishlist.csv").open(newline="", encoding="utf-8") as f:
        by_name = {r["name"]: r for r in csv.DictReader(f)}   # the csv keeps every row, with the bar's columns
    assert len(by_name) == res["rows"] and by_name["Rina Wearable"]["meets_bar"] == "True"
    assert by_name["Adrian Aoun"]["meets_bar"] == "False" and "no evidence that they wear" in by_name["Adrian Aoun"]["bar_reason"]

    res = run_export(conn, run_id, out_dir=tmp_path, bar=False)   # without the bar: US only and not rejected
    assert res["wishlist"] == res["rows"] and not res["bar"]
    rows = json.loads((tmp_path / "wishlist.json").read_text())
    assert isinstance(rows, list) and [r["n"] for r in rows] == list(range(1, len(rows) + 1))
    required = {"n", "name", "role", "section", "handle", "score", "p_accept", "p_wear", "p_love", "impact", "reach",
                "confidence", "flags", "status", "summary", "reasons", "evidence", "paths"}
    assert all(required <= set(r) for r in rows)
    top = rows[0]
    assert top["name"] == "Rina Wearable" and top["score"] == 31.2 and top["status"] == "approved" and top["section"] == "X voices"
    assert top["role"] == "Reviewing wearables since 2019 · 48k on X"
    assert top["flags"] == ["unverified_handle"] and top["reasons"] == ["wears two wearables"]
    assert top["evidence"][0]["kind"] == "post" and top["evidence"][0]["supports"] == "wear"
    assert top["summary"].startswith("Wearables reviewer")
    assert top["paths"] and {"how", "you", "via", "warmth", "target", "strength"} <= set(top["paths"][0])
    assert rows[1]["name"] == "Adrian Aoun" and rows[1]["paths"][0]["how"] == "direct" and rows[1]["paths"][0]["warmth"] == 3
    assert rows[1]["role"].startswith("angel + advisor (LP in ~100 funds) · Foundational Industries")
    assert all(r["score"] is None and r["status"] == "candidate" for r in rows[2:])
    assert {r["section"] for r in rows} <= set(config.SECTIONS.values())
    csv_text = (tmp_path / "wishlist.csv").read_text().splitlines()
    assert csv_text[0].startswith("n,rank,name,handle,section") and len(csv_text) == len(rows) + 1
    changes = (tmp_path / "changes.csv").read_text().splitlines()
    assert changes[0].startswith("change_id,run_id,candidate_id,name") and "Oura unboxing" in changes[1]


ENGAGEMENT_KPIS = {
    "value": 0.746, "why": None, "short": "ER 0.44% · 2.0× peers", "n": 20, "trimmed": 2, "first": "2026-07-01",
    "last": "2026-09-28", "fetched_at": "2026-09-30T10:00:00Z", "source": "monid", "followers": 85000,
    "per_post": {"views": 12400.0, "likes": 310.0, "replies": 42.0, "reposts": 25.0, "interactions": 377.0},
    "rates": {"views": 0.146, "likes": 0.00365, "replies": 0.000494, "reposts": 0.000294, "engagement": 0.00444},
    "typical": {"views": 0.098, "likes": 0.0018, "replies": 0.0002, "reposts": 0.00016, "engagement": 0.0022},
    "vs_size": {"views": 1.49, "likes": 2.03, "replies": 2.47, "reposts": 1.84, "engagement": 2.0},
    "index": 1.98, "per_view": 0.0304, "posts_per_week": 4.2, "benchmark": {"accounts": 118, "fitted": True}}
ENGAGEMENT_CSV = ["engagement", "er", "er_vs_size", "views_per_post", "likes_per_post", "replies_per_post",
                  "reposts_per_post", "view_rate", "like_rate", "reply_rate", "repost_rate"]


def test_export_engagement(seeded, tmp_path):
    """wishlist.json: an engagement object per person, null when unknown (too few posts, or scored before it existed);
    wishlist.csv: its flat columns after impact, blank when unknown."""
    conn, run_id = seeded
    now = db.utcnow()
    conn.execute("INSERT INTO candidates (x_id, handle, name, category, discovered_via, first_seen, last_seen) VALUES "
                 "('2','wearablenerd_test','Rina Wearable','x_voice','query:test',?,?)", (now, now))
    conn.commit()
    rina = _candidate(conn, x_id="2")["candidate_id"]
    adrian = _candidate(conn, map_person_id=51)["candidate_id"]
    known = {"value": 0.746, "short": "ER 0.44% · 2.0× peers", "reasons": ["÷ 85,000 followers"], "kpis": ENGAGEMENT_KPIS}
    too_few = {"value": None, "short": "unknown", "reasons": [], "kpis": {"value": None, "why": "only 3 posts (5 needed)"}}
    for cid, rank, column, fac in ((rina, 1, 0.746, known), (adrian, 2, None, too_few)):
        db.upsert(conn, "scores", {"run_id": run_id, "candidate_id": cid, "total": 30.0 - rank, "rank": rank, "flags": "[]",
                                   "engagement": column, "reasons_json": json.dumps({"factors": {"engagement": fac}})},
                  keys=("run_id", "candidate_id"))
    conn.commit()
    run_export(conn, run_id, out_dir=tmp_path, bar=False)
    rows = {r["candidate_id"]: r for r in json.loads((tmp_path / "wishlist.json").read_text())}
    k = ENGAGEMENT_KPIS
    assert rows[rina]["engagement"] == {"value": 0.746, "short": "ER 0.44% · 2.0× peers", "followers": 85000,
                                        "per_post": k["per_post"], "rates": k["rates"], "vs_size": k["vs_size"],
                                        "index": 1.98, "n": 20, "first": "2026-07-01", "last": "2026-09-28"}
    assert all(r["engagement"] is None for cid, r in rows.items() if cid != rina) and adrian in rows
    header = (tmp_path / "wishlist.csv").read_text().splitlines()[0].split(",")
    assert header[header.index("impact") + 1:header.index("reach")] == ENGAGEMENT_CSV
    with (tmp_path / "wishlist.csv").open(newline="", encoding="utf-8") as f:
        by_name = {r["name"]: r for r in csv.DictReader(f)}
    assert [by_name["Rina Wearable"][c] for c in ENGAGEMENT_CSV] == [
        "0.746", "0.00444", "1.98", "12400.0", "310.0", "42.0", "25.0", "0.146", "0.00365", "0.000494", "0.000294"]
    assert [by_name["Adrian Aoun"][c] for c in ENGAGEMENT_CSV] == [""] * len(ENGAGEMENT_CSV)
    assert engagement_factor(json.dumps({"factors": {"engagement": known}})) == known == engagement_factor({"factors": {"engagement": known}})
    assert engagement_factor(None) == engagement_factor("not json") == engagement_factor({"factors": {"engagement": 0.5}}) == {}
    assert engagement_json(float("nan"), known) is None and engagement_json(None, None) is None
    assert engagement_json(None, {"value": 0.5}) == {"value": 0.5, "short": "unknown", **dict.fromkeys(
        ("followers", "per_post", "rates", "vs_size", "index", "n", "first", "last"))}   # a value without KPIs


def test_export_falls_back_to_latest_scored_run(seeded, tmp_path):
    conn, run_id = seeded
    run_paths(conn, run_id, now=NOW)
    adrian = _candidate(conn, map_person_id=51)["candidate_id"]
    db.upsert(conn, "scores", {"run_id": run_id, "candidate_id": adrian, "total": 9.0, "rank": 1, "flags": "[]"}, keys=("run_id", "candidate_id"))
    conn.commit()
    later = db.new_run(conn, "export")
    res = run_export(conn, later, out_dir=tmp_path, bar=False)
    assert res["score_run"] == run_id and res["path_run"] == run_id
    rows = json.loads((tmp_path / "wishlist.json").read_text())
    assert rows[0]["name"] == "Adrian Aoun" and rows[0]["score"] == 9.0 and rows[0]["paths"]
