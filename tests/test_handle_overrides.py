"""Handle overrides (config/handle_overrides.yaml) in resolve_handles. Offline: fixture map + FixtureSource, tmp files."""
import yaml

from wearable_wishlist import db
from wearable_wishlist.resolve_handles import (HANDLE_OVERRIDES_FILE, OVERRIDE_CONFIDENCE, load_overrides, run_resolve_handles)

from test_paths import SpySource, _add_map_person, _candidate


def _write(tmp_path, rows, name="overrides.yaml"):
    p = tmp_path / name
    p.write_text(yaml.safe_dump({"checked": "2026-09-29", "overrides": rows}, sort_keys=False))
    return p


ROWS = [
    {"map_person_id": 5243, "name": "Daniel Cooper", "group": "press", "handle": "DanielWCooper", "x_id": "176412453",
     "confidence": "likely", "source_url": "https://x.com/danielwcooper", "match_reason": "exact name, Norwich",
     "non_us": True, "location": "Norwich, UK"},
    {"map_person_id": 360, "name": "Josh Constine", "group": "capital", "handle": "joshconstine", "x_id": "19563366",
     "confidence": "verified", "source_url": "https://x.com/JoshConstine", "match_reason": "bio 'Venture Partner @SignalFire'"},
    {"map_person_id": 272, "name": "Tammy Rogers", "group": "press", "handle": None, "status": "unknown",
     "source_url": "https://www.tomsguide.com/author/tammy-rogers", "match_reason": "no X account found"},
    {"map_person_id": 391, "name": "Somebody Else", "handle": "someone_else", "x_id": "999", "confidence": "verified"},
]


def _handles(conn):
    return {r["person_id"]: r for r in db.rows(conn, "SELECT * FROM map_x_handles")}


def test_load_overrides_validates_rows(tmp_path):
    bad = ROWS[:3] + [
        {"map_person_id": 1, "name": "No Id", "handle": "noid", "confidence": "verified"},                 # no x_id
        {"map_person_id": 2, "name": "Bad Conf", "handle": "badconf", "x_id": "12", "confidence": "sure"},  # confidence
        {"map_person_id": 3, "name": "Guess", "handle": None},                                             # no status
        {"map_person_id": 4, "name": "Same Account", "handle": "joshconstine", "x_id": "19563366", "confidence": "likely"},
        {"map_person_id": 5243, "name": "Daniel Cooper", "handle": "other", "x_id": "5", "confidence": "likely"},  # repeat
        {"map_person_id": 6, "name": "Bad Handle", "handle": "has space", "x_id": "6", "confidence": "likely"},
        "not a row",
    ]
    ov = load_overrides(_write(tmp_path, bad))
    assert set(ov) == {5243, 360, 272}
    assert ov[5243]["handle"] == "danielwcooper" and ov[5243]["x_id"] == "176412453" and ov[5243]["non_us"] == "Norwich, UK"
    assert ov[272]["handle"] is None and ov[272]["status"] == "unknown"
    assert load_overrides(tmp_path / "missing.yaml") == {}


def test_committed_overrides_file_is_valid():
    raw = yaml.safe_load(HANDLE_OVERRIDES_FILE.read_text())["overrides"]
    ov = load_overrides(HANDLE_OVERRIDES_FILE)
    assert len(ov) == len(raw) > 0                               # every row passes validation
    with_handle = [o for o in ov.values() if o["handle"]]
    assert with_handle and all(o["x_id"].isdigit() and o["confidence"] in OVERRIDE_CONFIDENCE for o in with_handle)
    assert any(o["status"] == "unknown" for o in ov.values())
    names = {o["name"]: o["handle"] for o in ov.values()}
    assert names["Tripp Mickle"] == "trippmickle" and names["Sabrina Ortiz"] == "sabrinaa_ortiz"


def test_overrides_resolve_before_paid_fallback_and_seed_candidates(map_conn, fixture_source, tmp_path):
    path = _write(tmp_path, ROWS)
    spy = SpySource(fixture_source, name="x_api")
    res = run_resolve_handles(map_conn, db.new_run(map_conn, "test"), spy, max_paid_lookups=20, overrides_path=path)
    h = _handles(map_conn)
    assert h[5243]["handle"] == "danielwcooper" and h[5243]["x_id"] == "176412453"
    assert h[5243]["method"] == "override_likely" and h[5243]["confidence"] == OVERRIDE_CONFIDENCE["likely"]
    assert h[360]["method"] == "override_verified" and h[360]["confidence"] == OVERRIDE_CONFIDENCE["verified"]
    assert 272 not in h and 391 not in h                         # unknown stays unknown; a name mismatch is not applied
    assert res["per_method"]["override"] == 2
    assert res["overrides"]["applied"] == 2 and res["overrides"]["unknown"] == 1 and res["overrides"]["name_mismatch"] == 1
    # the paid fallback never searched the overridden people, nor the ones already searched by hand (unknown)
    assert not any(q.startswith(("daniel cooper", "josh constine", "tammy rogers")) for q in spy.queries)
    assert any(q.startswith("roberto veronese") for q in spy.queries)   # everyone else still goes through the fallback
    # the handle lands on the map candidate, with the method, the confidence and the non-US marker in the notes
    dan = _candidate(map_conn, map_person_id=5243)
    assert dan["x_id"] == "176412453" and dan["handle"] == "danielwcooper" and dan["handle_verified"] == "likely_real"
    assert dan["handle_verification_method"] == "override_likely:0.85"
    assert "(override_likely, 0.85)" in dan["notes"] and "non-US (Norwich, UK)" in dan["notes"]
    assert _candidate(map_conn, map_person_id=360)["handle"] == "joshconstine"
    # idempotent: a second run keeps the rows, adds nothing, and still marks non-US
    res2 = run_resolve_handles(map_conn, db.new_run(map_conn, "test"), fixture_source, overrides_path=path)
    assert res2["overrides"]["kept"] == 2 and res2["overrides"]["applied"] == 0 and res2["per_method"]["override"] == 0
    assert db.one(map_conn, "SELECT COUNT(*) AS n FROM candidates WHERE map_person_id=5243")["n"] == 1
    assert _candidate(map_conn, map_person_id=5243)["notes"].count("map:") == 1


def test_override_replaces_fuzzy_match_survives_force_and_respects_manual(map_conn, fixture_source, tmp_path):
    now = db.utcnow()
    db.upsert(map_conn, "map_x_handles", {"person_id": 5243, "handle": "wrong_dan", "x_id": "77", "confidence": 0.7,
                                          "method": "seed_follow_list+strict", "resolved_at": now}, keys=("person_id",))
    db.upsert(map_conn, "map_x_handles", {"person_id": 360, "handle": "josh_by_hand", "x_id": "88", "confidence": 1.0,
                                          "method": "manual", "resolved_at": now}, keys=("person_id",))
    map_conn.commit()
    path = _write(tmp_path, ROWS)
    run_resolve_handles(map_conn, db.new_run(map_conn, "test"), fixture_source, overrides_path=path)
    h = _handles(map_conn)
    assert h[5243]["handle"] == "danielwcooper" and h[5243]["method"] == "override_likely"   # the override wins
    assert h[360]["handle"] == "josh_by_hand" and h[360]["method"] == "manual"                # manual is never touched
    run_resolve_handles(map_conn, db.new_run(map_conn, "test"), fixture_source, force=True, overrides_path=path)
    assert _handles(map_conn)[5243]["method"] == "override_likely"                            # --force does not undo it


def test_override_person_outside_the_selection_gets_a_candidate(map_conn, fixture_source, tmp_path):
    # warmth 0 and no X tag: not selected, so without the override no candidate row would be made
    _add_map_person(map_conn, id=9501, name="Paula Pogue", group_name="press", company="CBS", sector="press",
                    tags="coverage,press", warmth=0.0)
    path = _write(tmp_path, [{"map_person_id": 9501, "name": "Paula Pogue", "handle": "paulapogue", "x_id": "9501",
                              "confidence": "verified", "source_url": "https://x.com/paulapogue", "match_reason": "bio"}])
    res = run_resolve_handles(map_conn, db.new_run(map_conn, "test"), fixture_source, overrides_path=path)
    assert res["people"] == 9                                    # still not in the selected set
    c = _candidate(map_conn, map_person_id=9501)
    assert c and c["handle"] == "paulapogue" and c["x_id"] == "9501" and c["category"] == "media"


def test_overrides_file_is_not_applied_under_dry_run_by_default(map_conn, fixture_source):
    # conftest forces DRY_RUN=1: the committed file (live map ids, live X ids) must not leak into fixture runs
    res = run_resolve_handles(map_conn, db.new_run(map_conn, "test"), fixture_source)
    assert res["per_method"]["override"] == 0 and res["overrides"]["unknown"] == 0
    assert db.one(map_conn, "SELECT COUNT(*) AS n FROM map_x_handles")["n"] == 0
