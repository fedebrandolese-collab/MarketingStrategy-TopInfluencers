"""Dashboard data layer (app/data.py) against a seeded temp DB built by scripts/seed_demo_db.py.
Offline: conftest forces DRY_RUN=1; the seed reads only data/fixtures."""
import json
import os
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ring_wishlist import check, config, contacts, db, weights  # noqa: E402
from app import data  # noqa: E402
from scripts.seed_demo_db import seed  # noqa: E402


@pytest.fixture(scope="module")
def demo_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("demo") / "demo.db"
    counts = seed(p, quiet=True)
    assert counts["candidates"] == 8
    return p


@pytest.fixture
def conn(demo_path):
    c = db.connect(demo_path)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def no_password_gate(monkeypatch):
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)   # a .env with a password must not gate the page tests


def _by_handle(df: pd.DataFrame, handle: str) -> pd.Series:
    rows = df[df["handle"] == handle]
    assert len(rows) == 1, handle
    return rows.iloc[0]


# ---------------------------------------------------------------- seed shape

def test_seed_covers_the_demo_requirements(conn):
    assert db.one(conn, "SELECT COUNT(DISTINCT run_id) AS n FROM scores")["n"] == 2
    cats = {r["category"] for r in db.rows(conn, "SELECT DISTINCT category FROM candidates")}
    assert {"x_voice", "media", "vc", "founder_angel"} <= cats
    assert db.one(conn, "SELECT COUNT(*) AS n FROM candidates WHERE competitor_flag=1")["n"] == 1
    assert db.one(conn, "SELECT COUNT(*) AS n FROM candidates WHERE us_based=0")["n"] == 1
    assert db.one(conn, "SELECT COUNT(*) AS n FROM candidates WHERE handle_verified='suspicious'")["n"] == 1
    assert db.one(conn, "SELECT COUNT(*) AS n FROM candidates WHERE map_person_id IS NOT NULL")["n"] == 1
    hows = {r["how"] for r in db.rows(conn, "SELECT DISTINCT how FROM paths")}
    assert hows == set(config.PATH_KINDS)
    assert db.one(conn, "SELECT COUNT(*) AS n FROM evidence WHERE url IS NULL OR url=''")["n"] == 0
    assert db.one(conn, "SELECT COUNT(*) AS n FROM map_people")["n"] >= 8
    assert db.one(conn, "SELECT COUNT(*) AS n FROM x_users")["n"] == 8
    assert db.one(conn, "SELECT COUNT(*) AS n FROM x_posts")["n"] == 11
    assert db.one(conn, "SELECT COUNT(*) AS n FROM costs")["n"] > 0


def test_seed_is_rebuilt_from_scratch(tmp_path):
    p = tmp_path / "again.db"
    seed(p, quiet=True)
    c1 = seed(p, quiet=True)
    assert c1["candidates"] == 8 and c1["scores"] == 15


# ---------------------------------------------------------------- runs

def test_latest_scored_run(conn):
    run = data.latest_scored_run(conn)
    assert run and run["run_id"] == 2
    assert run["n_candidates"] == 8
    assert run["previous_run_id"] == 1
    assert run["date"] == "2026-09-29"
    assert run["cost_usd"] == pytest.approx(0.12 + 0.70 + 8 * 0.012, abs=1e-6)
    runs = data.scored_runs(conn)
    assert [r["run_id"] for r in runs] == [2, 1]


# ---------------------------------------------------------------- ranked table

def test_ranked_table_shape_and_order(conn):
    df = data.ranked_table(conn, 2)
    assert list(df.columns) == data.RANKED_COLUMNS
    assert len(df) == 8
    assert list(df["rank"]) == list(range(1, 9))
    assert df["total"].is_monotonic_decreasing
    assert set(df["section"]) <= set(config.SECTIONS.values()) | {data.UNCLASSIFIED_SECTION}
    for col in ("p_accept", "p_wear", "p_love", "impact", "reach", "confidence"):
        assert df[col].between(0, 1).all(), col
    assert all(isinstance(f, list) for f in df["flags"])
    assert df["best_path"].str.len().gt(0).all()
    assert df["summary"].str.len().gt(0).all()
    assert set(df["status"]) <= set(config.STATUSES)


def test_ranked_table_rows(conn):
    df = data.ranked_table(conn, 2)
    rina = _by_handle(df, "ringnerd_test")
    assert rina["rank"] == 1 and rina["section"] == "X voices" and rina["status"] == "approved"
    assert rina["best_path"].startswith("via Marwan Refaat")
    assert rina["x_url"] == "https://x.com/ringnerd_test"
    assert "Wearables reviewer" in rina["summary"]
    cora = _by_handle(df, "competitor_test")
    assert cora["flags"] == ["competitor:Oura"] and cora["flags_text"] == "competitor:Oura"
    assert _by_handle(df, "eu_test")["flags"] == ["not_us"]
    assert _by_handle(df, "parody_test")["flags"] == ["unverified_handle"]
    adrian = _by_handle(df, "adrianaoun_test")
    assert adrian["best_path"] == "you know them · met · 3 meetings · last contact 12 days ago"
    assert adrian["section"] == "Founders & angels" and adrian["handle_verified"] == "likely_real"
    assert _by_handle(df, "techwriter_test")["section"] == "Media"
    assert _by_handle(df, "vc_test")["section"] == "VCs"
    assert _by_handle(df, "founder_test")["status"] == "candidate"   # no status row -> default
    assert rina["why"] == rina["summary"] and rina["handle_verified"] == "verified"


def test_why_falls_back_when_not_researched(tmp_path):
    p = tmp_path / "why.db"
    seed(p, quiet=True)
    conn = db.connect(p)
    try:
        cid = int(_by_handle(data.ranked_table(conn, 2), "vc_test")["candidate_id"])
        conn.execute("DELETE FROM classifications WHERE candidate_id=?", (cid,))
        conn.execute("UPDATE scores SET reasons_json='{\"summary\": \"From the scorer.\"}' WHERE run_id=2 AND candidate_id=?", (cid,))
        conn.commit()
        assert _by_handle(data.ranked_table(conn, 2), "vc_test")["why"] == "From the scorer."
        conn.execute("UPDATE scores SET reasons_json=NULL WHERE candidate_id=?", (cid,))
        conn.commit()
        assert _by_handle(data.ranked_table(conn, 2), "vc_test")["why"] == data.NOT_RESEARCHED
    finally:
        conn.close()


def test_rejected_hidden_by_default(tmp_path):
    """'Hide rejected' (on by default) leaves rejected people out of the ranking; the view filters only hide rows."""
    p = tmp_path / "rej.db"
    seed(p, quiet=True)
    conn = db.connect(p)
    try:
        cid = int(_by_handle(data.ranked_table(conn, 2), "vc_test")["candidate_id"])
        data.set_status(conn, cid, "rejected", note="QA: fund not investing in hardware")
        df = data.ranked_table(conn, 2, precise=True)
    finally:
        conn.close()
    no_file = tmp_path / "weights.yaml"
    lst = data.rank_list(df, {}, no_file)
    assert "vc_test" not in set(lst["ranked"]["handle"]) and lst["hidden"] == {"not US": 1, "rejected": 1, "below the bar": 4}
    assert "vc_test" not in set(data.apply_filters(lst["ranked"], query="vc_test")["handle"])   # search does not resurrect them
    shown = data.rank_list(df, {"tf_hide_rejected": False}, no_file)
    assert "vc_test" in set(shown["ranked"]["handle"]) and shown["hidden"] == {"not US": 1, "below the bar": 4}
    assert set(data.apply_filters(shown["ranked"], statuses=["rejected"])["handle"]) == {"vc_test"}


def test_default_view_is_the_scheduled_ranking(conn, tmp_path):
    """Manual weights off: the list is weights.rerank with weights.effective({}) after the hard filters, rank counted
    after them (for a run scored by today's code that is the stored order). The sliders count only with the switch on."""
    df = data.ranked_table(conn, 2, precise=True)
    no_file = tmp_path / "weights.yaml"
    lst = data.rank_list(df, {}, no_file)
    expected = weights.rerank(df, weights.effective({}, no_file)[0], weights.DEFAULT_FILTERS)
    assert list(lst["ranked"]["candidate_id"]) == list(expected["candidate_id"])
    assert list(lst["ranked"]["rank"]) == list(range(1, 4)) and len(lst["top"]) == 3   # the bar, US only: 3 of 8
    assert not lst["manual"] and not lst["differs"] and set(lst["ranked"]["change"]) == {"="}
    assert lst["label"] == weights.DEFAULT_LABEL and lst["hidden"] == {"not US": 1, "below the bar": 4}
    assert len(data.rank_list(df, {"tf_bar": False}, no_file)["ranked"]) == 7
    assert data.rank_list(df, {"tw_wear": 2.0}, no_file)["weights"] == weights.DEFAULT_WEIGHTS   # switch off: ignored
    on = data.rank_list(df, {"manual_weights": True, "tw_media": 0.0, "tf_n": 3}, no_file)
    assert on["manual"] and on["differs"] and on["n"] == 3 and len(on["top"]) == 3
    assert int(_by_handle(on["ranked"], "techwriter_test")["rank"]) == len(on["ranked"])     # media weight 0: last
    tv = data.table_view(on["top"])
    assert list(tv.columns[:2]) == ["rank", "change"] and all(c in ("=", "new") or c[0] in "↑↓" for c in tv["change"])
    weights.save_preset("Carlo", {"impact": 2.0}, as_default=True, path=no_file)
    saved = data.rank_list(df, {}, no_file)                           # a saved default is what the list shows
    assert saved["weights"]["impact"] == 2.0 and not saved["differs"] and "Carlo" in saved["default_label"]
    assert list(saved["ranked"]["candidate_id"]) == list(
        weights.rerank(df, saved["weights"], weights.DEFAULT_FILTERS)["candidate_id"])


def _non_us_below_the_bar(p: Path) -> dict[str, int]:
    """The demo where US only hides nobody on its own, as on the real list: Elke (not US) loses her device evidence and
    Frank (below the bar: no device evidence) is flagged not US too. Returns {handle: candidate_id}."""
    seed(p, quiet=True)
    conn = db.connect(p)
    try:
        df = data.ranked_table(conn, 2)
        ids = {h: int(c) for h, c in zip(df["handle"], df["candidate_id"])}
        conn.execute("UPDATE scores SET reasons_json=NULL WHERE run_id=2 AND candidate_id=?", (ids["eu_test"],))
        conn.execute("UPDATE scores SET flags='[\"not_us\"]' WHERE run_id=2 AND candidate_id=?", (ids["founder_test"],))
        conn.commit()
    finally:
        conn.close()
    return ids


def test_filter_effects_say_what_each_toggle_adds(tmp_path):
    """Each hidden number is what switching that filter off adds: the people it alone leaves out. A filter whose people
    other filters hide too says so, instead of promising people the toggle cannot bring back."""
    p = tmp_path / "overlap.db"
    _non_us_below_the_bar(p)
    conn = db.connect(p)
    try:
        df = data.ranked_table(conn, 2, precise=True)
    finally:
        conn.close()
    no_file = tmp_path / "weights.yaml"
    lst = data.rank_list(df, {}, no_file)
    e = lst["effects"]
    assert e["not US"] == {"key": "us_only", "on": True, "matches": 2, "alone": 0, "also": {"below the bar": 2}}
    assert e["below the bar"] == {"key": "bar", "on": True, "matches": 5, "alone": 3, "also": {"not US": 2}}
    assert e["competitor-tied"] == {"key": "hide_competitor", "on": False, "matches": 1, "alone": 0,   # off: Cora is a
                                    "also": {"below the bar": 1}}                                      # brand, below the bar
    assert list(e) == ["not US", "competitor-tied", "below the bar"] and lst["hidden"] == {"not US": 0, "below the bar": 3}
    assert data.hidden_text(e) == "hidden: 0 not US (2 non-US people are also below the bar), 3 below the bar"
    assert data.hidden_help(e).splitlines()[2:] == [
        "- US only matches 2 non-US people: 0 hidden by it alone, 2 also below the bar",
        "- The bar matches 5 people below the bar: 3 hidden by it alone, 2 also not US"]
    off = data.rank_list(df, {"tf_us_only": False}, no_file)   # the line said 0: switching US only off adds nobody
    assert list(off["ranked"]["candidate_id"]) == list(lst["ranked"]["candidate_id"]) and len(off["ranked"]) == 3
    assert data.hidden_text(off["effects"]) == "hidden: 5 below the bar"
    no_bar = data.rank_list(df, {"tf_bar": False, "tf_hide_competitor": True}, no_file)
    assert data.hidden_text(no_bar["effects"]) == "hidden: 2 not US, 1 competitor-tied"
    assert len(no_bar["ranked"]) == 8 - 3
    one = {"not US": {"key": "us_only", "on": True, "matches": 1, "alone": 0, "also": {"rejected": 1, "below the bar": 1}}}
    assert data.hidden_text(one) == "hidden: 0 not US (1 non-US person is also rejected or below the bar)"
    assert data.hidden_text({}) == data.hidden_help({}) == "" and data.filter_effects(df.iloc[0:0]) == {}


def test_ranked_table_wear_evidence_and_the_bar(tmp_path):
    """wear_evidence: net positive p_wear evidence from reasons_json (value above the stored prior, backed by a quote or
    an event); bar_reason says which part of the brief's bar a row fails ('' when it meets it)."""
    p = tmp_path / "bar.db"
    seed(p, quiet=True)
    conn = db.connect(p)
    try:
        df = data.ranked_table(conn, 2)
        assert df["wear_evidence"].map(type).eq(bool).all()
        got = {h: (bool(w), r) for h, w, r in zip(df["handle"], df["wear_evidence"], df["bar_reason"])}
        assert got["ringnerd_test"] == (True, "") and got["vc_test"] == (True, "") and got["techwriter_test"] == (True, "")
        assert got["competitor_test"] == (True, weights.BAR_NOT_PERSON)          # a brand's account: not a person
        assert got["founder_test"] == (False, weights.BAR_NO_EVIDENCE)           # prior only
        assert got["eu_test"] == (True, "")                                      # meets the bar; US only hides them
        cid = int(_by_handle(df, "vc_test")["candidate_id"])
        stored = db.jloads(db.one(conn, "SELECT reasons_json FROM scores WHERE run_id=2 AND candidate_id=?", (cid,))["reasons_json"])
        stored["factors"]["p_wear"].update(value=0.21, prior=0.25, backed=True)   # mocks a device: evidence against
        conn.execute("UPDATE scores SET reasons_json=? WHERE run_id=2 AND candidate_id=?", (db.jdumps(stored), cid))
        conn.commit()
        vera = _by_handle(data.ranked_table(conn, 2), "vc_test")
        assert not vera["wear_evidence"] and vera["bar_reason"] == weights.BAR_AGAINST == "evidence is against wearing"
        del stored["factors"]["p_wear"]["prior"]                                   # no prior stored: fails the bar
        stored["factors"]["p_wear"]["value"] = 0.9
        conn.execute("UPDATE scores SET reasons_json=? WHERE run_id=2 AND candidate_id=?", (db.jdumps(stored), cid))
        conn.commit()
        assert not _by_handle(data.ranked_table(conn, 2), "vc_test")["wear_evidence"]
        run1 = data.ranked_table(conn, 1, precise=True)
        assert set(run1.loc[run1["wear_evidence"], "handle"]) == {"ringnerd_test", "vc_test", "techwriter_test", "competitor_test"}
    finally:
        conn.close()


def test_filters(conn):
    df = data.ranked_table(conn, 2)
    opts = data.filter_options(df)
    assert "X voices" in opts["sections"] and "competitor:Oura" in opts["flags"] and "approved" in opts["statuses"]
    assert set(data.apply_filters(df, sections=["Media"])["handle"]) == {"techwriter_test"}
    assert set(data.apply_filters(df, flags=["not_us", "unverified_handle"])["handle"]) == {"eu_test", "parody_test"}
    assert set(data.apply_filters(df, statuses=["approved"])["handle"]) == {"ringnerd_test"}
    assert len(data.apply_filters(df, min_confidence=0.9)) == 2
    assert set(data.apply_filters(df, query="oura")["handle"]) >= {"competitor_test"}
    assert set(data.apply_filters(df, query="Marwan")["handle"]) == {"ringnerd_test"}
    assert len(data.apply_filters(df)) == 8


# ---------------------------------------------------------------- topic affinity

def test_ranked_table_includes_affinity(conn):
    assert data.RANKED_COLUMNS.index("affinity") == data.RANKED_COLUMNS.index("p_love") + 1
    df = data.ranked_table(conn, 2)
    assert _by_handle(df, "ringnerd_test")["affinity"] == 0.82
    assert _by_handle(df, "founder_test")["affinity"] == 0.71
    assert pd.isna(_by_handle(df, "adrianaoun_test")["affinity"])        # no posts stored: unknown, not 0
    assert df["affinity"].dropna().between(0, 1).all() and df["affinity"].notna().sum() == 7
    old = data.ranked_table(conn, 1)                                      # run 1 predates affinity: every value NULL
    assert list(old.columns) == data.RANKED_COLUMNS and old["affinity"].isna().all()
    assert "affinity" not in data.TABLE_COLUMNS                           # lives in the person panel, not the table
    assert len(data.table_view(old)) == 7                                 # eu_test only joins in run 2


def test_affinity_filter(conn):
    df = data.ranked_table(conn, 2)
    assert set(data.apply_filters(df, min_affinity=0.6)["handle"]) == {"ringnerd_test", "vc_test", "founder_test"}
    assert len(data.apply_filters(df, min_affinity=0.0)) == 8              # 0 keeps the unknowns
    assert "adrianaoun_test" not in set(data.apply_filters(df, min_affinity=0.05)["handle"])
    assert len(data.apply_filters(data.ranked_table(conn, 1), min_affinity=0.1)) == 0
    assert set(data.apply_filters(df, min_affinity=0.5, sections=["X voices"])["handle"]) == {"ringnerd_test", "eu_test"}


def test_person_detail_affinity(conn):
    df = data.ranked_table(conn, 2)
    rina = data.person_detail(conn, 2, int(_by_handle(df, "ringnerd_test")["candidate_id"]))
    a = rina["affinity"]
    assert a["value"] == 0.82 and a["measured"] and a["source_label"] == "own posts + classifier"
    assert a["reason"].startswith("3 of her posts since July")
    assert [p["url"] for p in a["top_posts"]] == [f"https://x.com/ringnerd_test/status/{i}" for i in ("102", "103", "101")]
    assert a["top_posts"][0]["date"] == "2026-08-02" and "voice agent" in a["top_posts"][0]["text"]
    assert a["top_posts"][0]["replies"] == 140 and not a["posts_from_evidence"]
    assert a["classifier"] is True and "personal agents" in a["topics"]
    assert rina["factors"]["affinity"]["value"] == 0.82 and rina["factors"]["affinity"]["reasons"] == [a["reason"]]
    adrian = data.person_detail(conn, 2, int(_by_handle(df, "adrianaoun_test")["candidate_id"]))
    assert adrian["affinity"]["value"] is None and not adrian["affinity"]["measured"]
    assert "unknown" in adrian["affinity"]["reason"] and adrian["affinity"]["top_posts"] == []
    old = data.person_detail(conn, 1, int(_by_handle(df, "ringnerd_test")["candidate_id"]))   # run without affinity
    assert old["affinity"]["value"] is None and old["affinity"]["reason"] is None and old["affinity"]["top_posts"] == []


def test_affinity_is_null_safe(tmp_path):
    """NULL column, reasons_json without the factor, factor without the column, url-only posts, classifier evidence."""
    p = tmp_path / "aff.db"
    seed(p, quiet=True)
    conn = db.connect(p)
    try:
        df = data.ranked_table(conn, 2)
        cid = int(_by_handle(df, "vc_test")["candidate_id"])
        conn.execute("UPDATE scores SET affinity=NULL, reasons_json='{\"factors\": {}}' WHERE run_id=2 AND candidate_id=?", (cid,))
        conn.commit()
        assert pd.isna(_by_handle(data.ranked_table(conn, 2), "vc_test")["affinity"])
        a = data.person_detail(conn, 2, cid)["affinity"]
        assert a["value"] is None and a["reason"] is None and a["top_posts"] == [] and a["source_label"] is None
        conn.execute("UPDATE scores SET reasons_json=NULL WHERE run_id=2 AND candidate_id=?", (cid,))
        assert data.person_detail(conn, 2, cid)["affinity"]["value"] is None
        stored = {"factors": {"affinity": {"value": 0.4, "source": "classifier", "reasons": ["Talks about agents weekly."],
                                           "top_posts": ["https://x.com/vc_test/status/301", None, {"text": ""}]}}}
        conn.execute("UPDATE scores SET reasons_json=? WHERE run_id=2 AND candidate_id=?", (db.jdumps(stored), cid))
        conn.commit()
        assert _by_handle(data.ranked_table(conn, 2), "vc_test")["affinity"] == 0.4   # column NULL -> the factor's value
        a = data.person_detail(conn, 2, cid)["affinity"]
        assert a["value"] == 0.4 and a["source_label"] == "classifier verdict" and a["reason"] == "Talks about agents weekly."
        assert a["top_posts"] == [{"url": "https://x.com/vc_test/status/301", "date": None, "text": None, "engagement": None, "replies": None}]
        conn.execute("UPDATE scores SET reasons_json=? WHERE run_id=2 AND candidate_id=?",
                     (db.jdumps({"affinity": {"value": 0.5, "reason": "r"}}), cid))
        conn.execute("INSERT INTO evidence (candidate_id, kind, supports, polarity, weight, url, date, text, source) "
                     "VALUES (?, 'post', 'affinity', 1, 1.0, 'https://x.com/vc_test/status/301', '2026-09-01', 'personal AI agents', 'llm')", (cid,))
        conn.commit()
        a = data.person_detail(conn, 2, cid)["affinity"]
        assert a["value"] == 0.5 and a["posts_from_evidence"] and a["top_posts"][0]["text"] == "personal AI agents"
    finally:
        conn.close()
    legacy = sqlite3.connect(str(p))          # a database opened without db.init, the column dropped: still reads
    legacy.row_factory = sqlite3.Row
    try:
        legacy.execute("ALTER TABLE scores DROP COLUMN affinity")
        df = data.ranked_table(legacy, 2)
        assert len(df) == 8 and _by_handle(df, "ringnerd_test")["affinity"] == 0.82   # from the stored factor
        assert data.person_detail(legacy, 2, int(_by_handle(df, "ringnerd_test")["candidate_id"]))["affinity"]["value"] == 0.82
    finally:
        legacy.close()


# ---------------------------------------------------------------- community engagement

# reasons_json factor "engagement" as score.py stores it (the data contract), for @ringnerd_test in run 2
ENGAGEMENT = {
    "value": 0.746, "short": "ER 0.44% · 2.0× peers",
    "reasons": ["Average post over 20 latest posts (1 Jul to 28 Sep, 2 left out at each end of each count).",
                "÷ 85,000 followers: 0.44% engagement rate.", "Typical for 85,000 followers: 0.22% (118 accounts).",
                "Index 1.98 → engagement = 0.75."],
    "kpis": {"value": 0.746, "why": None, "short": "ER 0.44% · 2.0× peers", "n": 20, "trimmed": 2, "first": "2026-07-01",
             "last": "2026-09-28", "fetched_at": "2026-09-30T10:00:00Z", "source": "monid", "followers": 85000,
             "per_post": {"views": 12400.0, "likes": 310.0, "replies": 42.0, "reposts": 25.0, "interactions": 377.0},
             "rates": {"views": 0.146, "likes": 0.0037, "replies": 0.000494, "reposts": 0.000294, "engagement": 0.00444},
             "typical": {"views": 0.098, "likes": 0.0018, "replies": 0.0002, "reposts": 0.00016, "engagement": 0.0022},
             "vs_size": {"views": 1.49, "likes": 2.03, "replies": 2.47, "reposts": 1.84, "engagement": 2.0},
             "index": 1.98, "per_view": 0.0304, "posts_per_week": 4.2, "benchmark": {"accounts": 118, "fitted": True}},
}
TOO_FEW = "only 3 of their latest posts are 1-180 days old (5 needed)"


def _with_engagement(p: Path, column: float | None = 0.746, factor: dict | None = None) -> tuple[int, int]:
    """A seeded DB where Rina (run 2) has the engagement factor and column; everyone else has none, like older runs.
    Returns (rina, vera) candidate ids."""
    seed(p, quiet=True)
    conn = db.connect(p)
    try:
        df = data.ranked_table(conn, 2)
        rina, vera = (int(_by_handle(df, h)["candidate_id"]) for h in ("ringnerd_test", "vc_test"))
        stored = db.jloads(db.one(conn, "SELECT reasons_json FROM scores WHERE run_id=2 AND candidate_id=?", (rina,))["reasons_json"])
        stored["factors"]["engagement"] = ENGAGEMENT if factor is None else factor
        conn.execute("UPDATE scores SET engagement=?, reasons_json=? WHERE run_id=2 AND candidate_id=?",
                     (column, db.jdumps(stored), rina))
        conn.commit()
    finally:
        conn.close()
    return rina, vera


def test_ranked_table_includes_engagement(tmp_path, monkeypatch):
    assert data.RANKED_COLUMNS.index("engagement") == data.RANKED_COLUMNS.index("impact") + 1
    p = tmp_path / "eng.db"
    rina, vera = _with_engagement(p)
    conn = db.connect(p)
    try:
        df = data.ranked_table(conn, 2)
        r = _by_handle(df, "ringnerd_test")
        assert (r["engagement"], r["engagement_short"], r["er"], r["er_vs_size"]) == (0.75, "ER 0.44% · 2.0× peers", 0.00444, 1.98)
        rest = df[df["candidate_id"] != rina]                       # no factor stored: unknown, not 0
        assert rest["engagement"].isna().all() and set(rest["engagement_short"]) == {"unknown"}
        assert rest["er"].isna().all() and rest["er_vs_size"].isna().all()
        assert _by_handle(data.ranked_table(conn, 2, precise=True), "ringnerd_test")["engagement"] == 0.746
        old = data.ranked_table(conn, 1)                            # run 1 predates it
        assert old["engagement"].isna().all() and set(old["engagement_short"]) == {"unknown"}
        tv = data.table_view(df)
        assert list(tv.columns).index("engagement_short") == list(tv.columns).index("impact") + 1
        assert tv.loc[tv["candidate_id"] == rina, "engagement_short"].item() == "ER 0.44% · 2.0× peers"
        assert data.table_view(df.drop(columns="engagement_short"))["engagement_short"].eq("unknown").all()
        conn.execute("UPDATE scores SET engagement=NULL WHERE run_id=2 AND candidate_id=?", (rina,))
        conn.commit()                                               # column NULL: the stored factor carries the value
        assert _by_handle(data.ranked_table(conn, 2), "ringnerd_test")["engagement"] == 0.75
        precise = data.ranked_table(conn, 2, precise=True)
    finally:
        conn.close()
    seen: dict[int, object] = {}                                    # the list re-ranks with it, as with impact and affinity
    real = weights.weighted_total

    def spy(factors, w=None):
        seen[int(factors["candidate_id"])] = factors["engagement"]
        return real(factors, w)
    monkeypatch.setattr(weights, "weighted_total", spy)
    data.rank_list(precise, {"tf_bar": False}, tmp_path / "weights.yaml")
    assert seen[rina] == 0.746 and all(pd.isna(v) for c, v in seen.items() if c != rina) and vera in seen


def test_engagement_is_null_safe(tmp_path):
    """A factor that says why it is unknown, a column without a factor, a database without the column."""
    p = tmp_path / "why.db"
    rina, _ = _with_engagement(p, column=None, factor={"value": None, "short": "unknown", "reasons": [],
                                                        "kpis": {"value": None, "why": TOO_FEW, "n": 3}})
    conn = db.connect(p)
    try:
        r = _by_handle(data.ranked_table(conn, 2), "ringnerd_test")
        assert pd.isna(r["engagement"]) and r["engagement_short"] == "unknown" and pd.isna(r["er"])
        e = data.person_detail(conn, 2, rina)["engagement"]
        assert not e["known"] and e["unknown"] == f"Community engagement unknown: {TOO_FEW}" and e["table"] is None
        conn.execute("UPDATE scores SET engagement=0.5, reasons_json='{}' WHERE run_id=2 AND candidate_id=?", (rina,))
        conn.commit()
        r = _by_handle(data.ranked_table(conn, 2), "ringnerd_test")
        assert r["engagement"] == 0.5 and r["engagement_short"] == "unknown"   # no summary stored for it
        e = data.person_detail(conn, 2, rina)["engagement"]
        assert e["known"] and e["headline"] == "score factor 0.50 (0.50 = typical for their size)" and e["table"] is None
    finally:
        conn.close()
    p2 = tmp_path / "legacy.db"
    _with_engagement(p2)
    legacy = sqlite3.connect(str(p2))          # opened without db.init, the column dropped: the factor still reads
    legacy.row_factory = sqlite3.Row
    try:
        legacy.execute("ALTER TABLE scores DROP COLUMN engagement")
        r = _by_handle(data.ranked_table(legacy, 2), "ringnerd_test")
        assert r["engagement"] == 0.75 and r["engagement_short"] == "ER 0.44% · 2.0× peers"
    finally:
        legacy.close()


def test_engagement_view_formats_the_kpis():
    e = data.engagement_view(0.746, ENGAGEMENT)
    assert e["known"] and e["value"] == 0.75 and e["short"] == "ER 0.44% · 2.0× peers"
    assert e["headline"] == "ER 0.44% · 2.0× peers · score factor 0.75 (0.50 = typical for their size)"
    t = e["table"]
    assert list(t.index) == ["Average post (20 latest posts)", "÷ 85,000 followers", "Typical for this size", "vs size"]
    assert list(t.columns) == ["Views", "Likes", "Replies", "Reposts", "Engagement"]
    assert [list(t.iloc[i]) for i in range(4)] == [
        ["12,400", "310", "42", "25", "377"],
        ["14.6%", "0.37%", "0.049%", "0.029%", "0.44%"],               # >=1%: 1 decimal, >=0.1%: 2, else 3
        ["9.8%", "0.18%", "0.020%", "0.016%", "0.22%"],
        ["1.5×", "2.0×", "2.5×", "1.8×", "2.0×"]]
    assert e["reasons"] == ENGAGEMENT["reasons"] and e["unknown"] is None
    assert e["pace"] == "4.2 posts a week · 30 interactions per 1,000 views"
    k = ENGAGEMENT["kpis"]
    no_views = {**ENGAGEMENT, "kpis": {**k, "per_view": None, **{g: {**k[g], "views": None} for g in
                                                                     ("per_post", "rates", "typical", "vs_size")}}}
    e = data.engagement_view(None, no_views)                            # views unknown: no Views column
    assert e["value"] == 0.75 and "Views" not in e["table"] and e["pace"] == "4.2 posts a week"
    one_gap = {**ENGAGEMENT, "kpis": {**k, "typical": {**k["typical"], "views": None}}}
    assert data.engagement_view(None, one_gap)["table"].loc["Typical for this size", "Views"] == data.NO_VALUE
    unknown = data.engagement_view(None, {"value": None, "short": "unknown", "reasons": ["x"], "kpis": {"why": TOO_FEW}})
    assert not unknown["known"] and unknown["short"] == "unknown" and unknown["unknown"].endswith(TOO_FEW)
    for fac in (None, {}, {"value": None, "short": "unknown", "kpis": None}):
        e = data.engagement_view(None, fac)
        assert e["unknown"] == "Community engagement unknown: no sample of their latest posts yet" and e["table"] is None
    assert data.engagement_view(None, 0.6)["value"] == 0.6                # a bare number is the value
    assert [data.rate_pct(x) for x in (0.146, 0.01, 0.0044, 0.001, 0.000494, None)] == [
        "14.6%", "1.0%", "0.44%", "0.10%", "0.049%", None]
    assert [data.count_text(x) for x in (1234567, 12400.0, 42.4, 4.5, 3.0, None)] == [
        "1,234,567", "12,400", "42", "4.5", "3", None]
    assert data.times_text(1.49) == "1.5×" and data.times_text(None) is None


def test_person_detail_engagement(tmp_path):
    p = tmp_path / "panel.db"
    rina, vera = _with_engagement(p)
    conn = db.connect(p)
    try:
        d = data.person_detail(conn, 2, rina)
        assert d["engagement"]["short"] == "ER 0.44% · 2.0× peers" and d["engagement"]["table"] is not None
        assert d["factors"]["engagement"] == {"value": 0.75, "label": "community engagement",
                                              "reasons": ENGAGEMENT["reasons"], "evidence_ids": []}
        v = data.person_detail(conn, 2, vera)
        assert not v["engagement"]["known"] and v["factors"]["engagement"]["value"] is None
        assert v["engagement"]["unknown"] == "Community engagement unknown: no sample of their latest posts yet"
        assert not data.person_detail(conn, 1, rina)["engagement"]["known"]   # run 1 predates it
    finally:
        conn.close()


def test_affinity_helpers():
    assert data.affinity_source_label("computed") == "own posts" and data.affinity_source_label(None) is None
    assert data.affinity_source_label("computed+classifier") == "own posts + classifier"
    assert data.affinity_source_label("topic_voice") == "classifier verdict"
    assert data.affinity_posts(None) == [] and len(data.affinity_posts([f"https://x.com/a/status/{i}" for i in range(5)])) == 3
    assert data.change_row_text({"field": "affinity", "old_value": "0.2", "new_value": "0.55",
                                 "reason": "topic affinity 0.20 -> 0.55: two posts on voice agents"}).startswith("topic affinity 0.20")
    assert data.FIELD_SHORT["affinity"] == "topic"


# ---------------------------------------------------------------- changes

def test_changes_since_previous(conn):
    ch = data.changes_since_previous(conn, 2)
    assert ch["previous_run_id"] == 1
    assert ch["counts"] == {"up": 2, "down": 3, "new": 1, "other": 0}
    assert ch["summary"] == "since last run: 2 up · 3 down · 1 new"
    assert {e["handle"] for e in ch["up"]} == {"ringnerd_test", "founder_test"}
    assert {e["handle"] for e in ch["down"]} == {"techwriter_test", "competitor_test", "parody_test"}
    assert [e["handle"] for e in ch["new"]] == ["eu_test"]
    for group in ("up", "down", "new"):
        for e in ch[group]:
            assert e["reasons"] and all(isinstance(r, str) and r for r in e["reasons"])
    rina = ch["up"][0]
    assert rina["handle"] == "ringnerd_test" and rina["delta_total"] > 0
    assert any("unboxing" in r.lower() for r in rina["reasons"])
    cora = next(e for e in ch["down"] if e["handle"] == "competitor_test")
    assert "flags" in cora["changed_fields"] and cora["old_rank"] == 6 and cora["new_rank"] == 7


def test_ranked_table_precise_and_change_column(conn):
    """The list re-ranks from unrounded stored factors; table_view carries the move against the default ('change')."""
    run_id = data.latest_scored_run(conn)["run_id"]
    df = data.ranked_table(conn, run_id, precise=True)
    stored = {r["candidate_id"]: r for r in db.rows(conn, "SELECT * FROM scores WHERE run_id=?", (run_id,))}
    for r in df.to_dict("records"):
        for k in ("p_accept", "p_wear", "p_love", "impact", "reach", "total"):
            assert r[k] == stored[r["candidate_id"]][k]
    assert list(data.ranked_table(conn, run_id).columns) == list(df.columns)
    ranked = weights.rerank(df, {"impact": 2.0}, {"n": 5, "us_only": False})
    tv = data.table_view(ranked[ranked["rank"] <= 5])
    assert list(tv.columns[:2]) == ["rank", "change"] and len(tv) == min(5, len(ranked))
    assert list(tv["rank"]) == sorted(tv["rank"]) and all(c in ("=", "new") or c[0] in "↑↓" for c in tv["change"])
    assert list(data.table_view(ranked.iloc[0:0]).columns) == list(tv.columns)
    assert set(data.table_view(data.ranked_table(conn, run_id))["change"]) == {""}   # not re-ranked: no move shown


def test_changes_first_run(conn):
    ch = data.changes_since_previous(conn, 1)
    assert ch["previous_run_id"] is None
    assert ch["counts"] == {"up": 0, "down": 0, "new": 0, "other": 0}
    assert ch["summary"] == "first scored run"


# ---------------------------------------------------------------- person detail

def test_person_detail_rina(conn):
    df = data.ranked_table(conn, 2)
    cid = int(_by_handle(df, "ringnerd_test")["candidate_id"])
    d = data.person_detail(conn, 2, cid)
    assert d["name"] == "Rina Ring" and d["rank"] == 1 and d["section"] == "X voices"
    assert d["summary"].startswith("Wearables reviewer")
    assert d["unknowns"] == ["whether they accept free devices"]
    assert len(d["evidence"]) == 8 and all(e["url"].startswith("https://") for e in d["evidence"])
    assert [p["how"] for p in d["paths"]] == ["via", "x_direct", "open_dm"]
    assert d["paths"][0]["line"].startswith("you → Marwan Refaat → Rina Ring")
    assert "strength 0.55" in d["paths"][0]["line"]
    assert d["best_path"].startswith("via Marwan Refaat")
    for f in ("p_accept", "p_wear", "p_love"):
        assert d["factors"][f]["reasons"], f
        assert 0 <= d["factors"][f]["value"] <= 1
    assert d["factors"]["p_wear"]["evidence_ids"]
    assert d["reasons"]
    assert [e["kind"] for e in d["events"]] == ["ring_unboxing", "device_review"]
    assert d["flags"] == []
    assert d["status"]["status"] == "approved" and d["status"]["note"]
    assert any(c["field"] == "total" for c in d["changes"])
    assert d["classification"]["category"] == "x_voice"


def test_person_detail_flags_and_direct_path(conn):
    df = data.ranked_table(conn, 2)
    cora = data.person_detail(conn, 2, int(_by_handle(df, "competitor_test")["candidate_id"]))
    assert cora["flags"][0]["flag"] == "competitor:Oura" and "Oura" in cora["flags"][0]["explanation"]
    parody = data.person_detail(conn, 2, int(_by_handle(df, "parody_test")["candidate_id"]))
    assert parody["flags"][0]["flag"] == "unverified_handle" and "suspicious" in parody["flags"][0]["explanation"]
    elke = data.person_detail(conn, 2, int(_by_handle(df, "eu_test")["candidate_id"]))
    assert elke["flags"][0]["flag"] == "not_us" and "Berlin" in elke["flags"][0]["explanation"]
    adrian = data.person_detail(conn, 2, int(_by_handle(df, "adrianaoun_test")["candidate_id"]))
    assert adrian["map_person"]["name"] == "Adrian Aoun"
    assert adrian["paths"][0]["how"] == "direct" and adrian["paths"][0]["line"].startswith("you → Adrian Aoun · direct")
    assert adrian["x_user"] is None and "location" in adrian["unknowns"]
    assert data.person_detail(conn, 2, 999999) is None


def test_path_sentences_cover_every_kind():
    assert data.path_short(None) == "cold: no warm intro"   # neutral: how to approach, not a verdict
    assert data.path_short({"how": "none"}) == data.COLD
    assert data.path_short({"how": "direct", "you_rel": "met · 3 meetings · 84 touches · last 11d ago · faded"}) \
        == "you know them · met · 3 meetings · last contact 11 days ago · gone quiet"
    assert data.path_short({"how": "via", "via_name": "Max", "target_rel": "same company"}) == "via Max, same company"
    assert data.path_short({"how": "via", "via_name": "Jane Doe", "target_rel": "introduced them (map, transcript)"}) \
        == "via Jane Doe, introduced them"
    assert data.path_short({"how": "via", "via_name": "Ivan", "target_rel": "same firm (TechCrunch, per their bio)"}) \
        == "via Ivan, same firm (TechCrunch)"
    assert data.path_short({"how": "x_direct"}) == "on X · follow each other"
    assert data.path_short({"how": "x_direct", "target_rel": "follow each other on X"}) == "on X · follow each other"
    assert data.path_short({"how": "open_dm"}) == "cold: DMs open"
    assert data.plain_rel("in touch · 1 touches · last 1d ago", keep_touches=True) == "in touch · 1 interactions · last contact 1 day ago"
    assert [data.strength_word(s) for s in (0.9, 0.5, 0.3, 0.1, None)] == ["strong", "good", "weak", "very weak", "unknown strength"]
    line = data.path_line({"how": "via", "via_name": "Max", "you_rel": "warm", "target_rel": "intro (map)", "strength": 0.7}, "Zed")
    assert line == "you → Max → Zed · strength 0.70 · you→Max: warm · Max→Zed: intro (map)"
    assert data.path_line(None, "Zed").endswith("cold: no warm intro")
    assert "DMs open" in data.path_line({"how": "open_dm", "strength": 0.15}, "Zed")


def test_parse_reasons_is_tolerant():
    r = data.parse_reasons('{"factors": {"p_wear": {"reasons": ["a", "b"], "evidence_ids": [3, "4"]}}, "reasons": ["top"], "evidence_ids": [3]}')
    assert r["overall"] == ["top"] and r["factors"]["p_wear"]["reasons"] == ["a", "b"] and r["factors"]["p_wear"]["evidence_ids"] == [3, 4]
    r = data.parse_reasons('{"p_accept": "prior 0.4", "p_love": ["x", {"text": "y"}]}')
    assert r["factors"]["p_accept"]["reasons"] == ["prior 0.4"] and r["factors"]["p_love"]["reasons"] == ["x", "y"]
    assert r["overall"] == ["prior 0.4", "x", "y"]
    assert data.parse_reasons('["only", "list"]')["overall"] == ["only", "list"]
    assert data.parse_reasons(None) == {"overall": [], "factors": {}, "evidence_ids": []}
    assert data.parse_reasons("not json")["overall"] == []


# ---------------------------------------------------------------- costs

def test_run_cost_summary(conn):
    c = data.run_cost_summary(conn, 2)
    assert c["total_usd"] == pytest.approx(0.916, abs=1e-6)
    assert c["label"] == "$0.92"
    assert set(c["by_provider"]) == {"x_api", "anthropic"}
    assert c["rows"] and c["rows"][0]["cost_usd"] >= c["rows"][-1]["cost_usd"]
    assert data.run_cost_summary(conn, None)["total_usd"] == 0.0
    assert data.run_cost_summary(conn, 12345)["total_usd"] == 0.0


# ---------------------------------------------------------------- status (the only write)

def test_set_status_persists(tmp_path):
    p = tmp_path / "status.db"
    seed(p, quiet=True)
    conn = db.connect(p)
    try:
        df = data.ranked_table(conn, 2)
        cid = int(_by_handle(df, "founder_test")["candidate_id"])
        row = data.set_status(conn, cid, "ring_sent", note="Sent via Max", owner="federico")
        assert row["status"] == "ring_sent" and row["note"] == "Sent via Max" and row["owner"] == "federico"
        assert row["updated_at"] and row["updated_at"].endswith("Z")
        row2 = data.set_status(conn, cid, "wearing")              # None keeps note/owner
        assert row2["note"] == "Sent via Max" and row2["owner"] == "federico"
        row3 = data.set_status(conn, cid, "wearing", note="")     # "" clears
        assert row3["note"] is None
        assert _by_handle(data.ranked_table(conn, 2), "founder_test")["status"] == "wearing"
        assert data.person_detail(conn, 2, cid)["status"]["status"] == "wearing"
        with pytest.raises(ValueError):
            data.set_status(conn, cid, "not-a-status")
        with pytest.raises(ValueError):
            data.set_status(conn, 999999, "approved")
        assert db.one(conn, "SELECT COUNT(*) AS n FROM status")["n"] == 3
    finally:
        conn.close()


# ---------------------------------------------------------------- empty database

def test_empty_db_is_handled(tmp_path):
    conn = db.connect(tmp_path / "empty.db")
    try:
        assert data.latest_scored_run(conn) is None
        df = data.ranked_table(conn, None)
        assert len(df) == 0 and list(df.columns) == data.RANKED_COLUMNS
        assert data.filter_options(df) == {"sections": [], "flags": [], "statuses": []}
        assert data.changes_since_previous(conn, None)["summary"] == "no scored run yet"
        assert data.run_cost_summary(conn, None)["label"] == "$0.0000"
    finally:
        conn.close()


# ---------------------------------------------------------------- display helpers (table, flags, paths, changes)

def test_table_view_and_labels(conn):
    df = data.ranked_table(conn, 2)
    tv = data.table_view(df)
    assert list(tv.columns) == data.TABLE_COLUMNS and len(tv) == 8 and list(tv.index) == list(range(8))
    rina = tv[tv["person"] == "Rina Ring · @ringnerd_test ✓"].iloc[0]
    assert rina["p_wear"] == 85 and rina["p_accept"] == 70 and rina["p_love"] == 70   # probabilities in whole percents
    assert rina["status_label"] == "approved" and rina["flags_short"] == "" and rina["category_label"] == "X voice"
    assert rina["why"].startswith("Wearables reviewer")
    assert tv[tv["person"].str.startswith("Cora")].iloc[0]["flags_short"] == "⚑ Oura"
    assert "Rina Ring (parody) · @parody_test (suspicious)" in set(tv["person"])
    assert "Adrian Aoun · @adrianaoun_test (likely real)" in set(tv["person"])
    assert "intro asked" in set(tv["status_label"])
    no_path = df.copy()
    no_path.at[0, "flags"] = ["no_path", "not_us"]
    assert data.table_view(no_path).iloc[0]["flags_short"] == "not US"      # the path column already says "cold: no warm intro"
    assert data.handle_label("x", None) == "@x (unchecked)" and data.handle_label(None, "verified") == "no X handle"
    assert data.person_label("Ann", None, None) == "Ann · no X handle" and data.person_label("@ann", "ann", "verified") == "@ann ✓"
    assert data.category_label("founder_angel") == "Founder / angel" and data.category_label(None) == "Not classified"
    assert data.flag_short("not_us") == "not US" and data.flag_short("unverified_handle") == "unverified handle"
    assert data.flag_short("no_path") == "cold (no warm intro)" and data.flag_short("competitor") == "⚑ competitor"
    assert data.status_label("ring_sent") == "ring sent" and data.status_label(None) == "candidate"
    assert list(data.table_view(df.iloc[0:0]).columns) == data.TABLE_COLUMNS


def test_flags_cell_marks_competitor_ties():
    """The brief: anyone tied to a direct competitor is flagged, never removed. The cell is short and unmistakable (every
    tie behind one ⚑, before the other flags); the note in brackets stays for the person panel and the Flag filter."""
    long_tie = "competitor:Wispr Flow rival (VoiceOS, not in the brief's list)"
    assert data.flags_short([long_tie]) == "⚑ Wispr Flow rival"
    assert data.flags_short(["competitor:Sesame", "competitor:Limitless"]) == "⚑ Sesame, Limitless"
    assert data.flags_short(["not_us", "competitor:Ultrahuman", "competitor:Ultrahuman"]) == "⚑ Ultrahuman · not US"
    assert data.flags_short(["unverified_handle", "not_us"]) == "unverified handle · not US"
    assert data.flags_short(["no_path"]) == data.flags_short([]) == data.flags_short(None) == ""
    assert data.flag_short(long_tie) == "⚑ Wispr Flow rival" and data.flag_short("competitor:Oura") == "⚑ Oura"
    assert data.flag_short(long_tie, full=True) == "⚑ Wispr Flow rival (VoiceOS, not in the brief's list)"
    assert data.flag_short("competitor:Pebble (not in the brief's list)") == "⚑ Pebble"
    assert data.flag_explanation(long_tie).startswith("Tied to competitor Wispr Flow rival (VoiceOS, not in the brief's list)")
    assert data.table_view(pd.DataFrame([{
        "rank": 1, "candidate_id": 1, "name": "Ann", "handle": "ann", "handle_verified": "verified", "category": "vc",
        "section": "VCs", "why": "w", "p_accept": .5, "p_wear": .5, "p_love": .5, "impact": .5, "engagement_short": "unknown",
        "contact": "X DMs open", "status": "candidate", "flags": ["competitor:Sesame", "competitor:Limitless"]}]
    )).iloc[0]["flags_short"] == "⚑ Sesame, Limitless"
    order = data.TABLE_COLUMNS                                             # right after the category, not off-screen
    assert order.index("flags_short") == order.index("category_label") + 1


def test_no_flag_filter_option(conn):
    df = data.ranked_table(conn, 2)
    assert data.filter_options(df)["flags"][-1] == data.NO_FLAG
    clean = data.apply_filters(df, flags=[data.NO_FLAG])
    assert set(clean["handle"]) == {"ringnerd_test", "vc_test", "adrianaoun_test", "founder_test", "techwriter_test"}
    assert len(data.apply_filters(df, flags=[data.NO_FLAG, "not_us"])) == 6


def test_path_view_every_kind():
    via = data.path_view({"how": "via", "via_name": "Max", "you_rel": "met · last 3d ago", "target_rel": "intro (map)",
                          "strength": 0.7, "json_obj": {"warmth": 3}, "evidence": "map_edges 1->2"}, "Zed")
    assert via["head"] == "you → Max → Zed" and via["labels"] == ["you → Max: met · last contact 3 days ago", "Max → Zed: intro"]
    assert via["strength"] == 0.7 and via["warmth"] == 3 and via["evidence"] == "map_edges 1->2"
    assert via["strength_word"] == "strong" and data.path_view(None, "Zed")["strength_word"] == "cold"
    assert data.path_view({"how": "direct", "you_rel": "met", "strength": 0.9}, "Zed")["head"] == "you → Zed"
    assert data.path_view({"how": "x_direct", "strength": 0.4}, "Zed")["labels"] == ["follow each other"]
    assert data.path_view({"how": "open_dm", "strength": 0.15}, "Zed")["kind"] == "cold"
    assert data.path_view({"how": "x_direct", "strength": 0.4}, "Zed")["head"] == "you ↔ Zed"
    assert data.path_view(None, "Zed")["head"] == "you → Zed"   # no crossed-out arrow: cold is a way in


def test_change_headlines(conn):
    ch = data.changes_since_previous(conn, 2)
    rina = data.change_headline(ch["up"][0])
    assert rina.startswith("Rina Ring (@ringnerd_test) · score ") and "(+" in rina and "wear" in rina
    tom = data.change_headline(next(e for e in ch["down"] if e["handle"] == "techwriter_test"))
    assert "rank #4 → #5" in tom
    assert data.change_headline(ch["new"][0]) == "Elke Uropa (@eu_test) · new, enters at #6"
    assert data.change_row_text({"field": "p_wear", "old_value": "0.7", "new_value": "0.85", "reason": "unboxing"}) == "p_wear 0.7 → 0.85: unboxing"
    assert data.change_row_text({"field": "p_wear", "reason": "p_wear 0.70 -> 0.85: new evidence."}) == "p_wear 0.70 -> 0.85: new evidence."


def test_history_in_plain_words(conn):
    h = data.history_text
    assert h({"field": "p_accept", "old_value": "0.756", "new_value": "0.85",
              "reason": "p_accept 0.76 -> 0.85: new evidence: post 'Talking…' (wear, #2340); bio 'GP' (competitor, against, #2350)."}) \
        == "Accepts 76% → 85%: new evidence: post 'Talking…' (wear); bio 'GP' (competitor, against)."
    assert h({"field": "rank", "old_value": "10", "new_value": "15", "reason": "Rank 10 -> 15: other candidates moved."}) \
        == "Rank #10 → #15: other candidates moved."
    assert h({"field": "total", "old_value": "33.71", "new_value": "38.94", "reason": "Total 33.7 -> 38.9: x (#1, #2)"}, brief=True) \
        == "Score 33.7 → 38.9"
    assert h({"field": "affinity", "old_value": None, "new_value": "0.708", "reason": "topic affinity unknown -> 0.71: posts"}) \
        == "Topic affinity unknown → 0.71: posts"
    assert h({"field": "new_candidate", "reason": "New on the list: Ann (VCs), total 33.7, rank 1."}) \
        == "New on the list: Ann (VCs), score 33.7, rank 1."
    assert data.plain_reason("Reviewed a device (event #91, weight 0.58): +0.35 (#605, #607).") \
        == "Reviewed a device (weight 0.58): +0.35."
    df = data.ranked_table(conn, 2)
    rina = data.person_detail(conn, 2, int(_by_handle(df, "ringnerd_test")["candidate_id"]))
    assert rina["history"][0]["run_id"] == 2 and rina["history"][0]["when"] == "2026-09-29 07:00 UTC"
    lines = rina["history"][0]["lines"]
    assert lines[0].startswith("Score ") and ":" not in lines[0]       # the factor lines below explain the score
    assert any(line.startswith("Wears daily 70% → 85%") and "unboxing" in line for line in lines)
    eu = data.person_detail(conn, 2, int(_by_handle(df, "eu_test")["candidate_id"]))
    assert eu["history"][0]["lines"] and "#" not in " ".join(eu["history"][0]["lines"])
    assert data.person_history(conn, 999999) == []


def test_evidence_groups(conn):
    df = data.ranked_table(conn, 2)
    d = data.person_detail(conn, 2, int(_by_handle(df, "ringnerd_test")["candidate_id"]))
    g = data.evidence_groups(d["evidence"])
    assert set(g) >= {"accept", "wear", "love"} and sum(len(v) for v in g.values()) <= len(d["evidence"])
    dup = [{"supports": "wear", "text": "I wear it", "url": "u"}, {"supports": "wear", "text": "I  wear it", "url": "u"},
           {"supports": "love", "text": "I wear it", "url": "u"}]
    assert {k: len(v) for k, v in data.evidence_groups(dup).items()} == {"wear": 1, "love": 1}


def test_cost_since_previous_and_to_date(conn):
    c = data.run_cost_summary(conn, 2)
    assert c["since_previous_usd"] == pytest.approx(0.916, abs=1e-6)
    assert c["to_date_usd"] == pytest.approx(0.916 + 0.40 + 1.50 + 5.00 + 0.084, abs=1e-6)
    assert data.run_cost_summary(conn, 1)["since_previous_usd"] == pytest.approx(6.984, abs=1e-6)


def test_person_detail_reads_score_py_shape(tmp_path):
    """Real score.py stores unknowns and flag reasons inside reasons_json; the panel shows both."""
    p = tmp_path / "shape.db"
    seed(p, quiet=True)
    conn = db.connect(p)
    try:
        cid = int(_by_handle(data.ranked_table(conn, 2), "parody_test")["candidate_id"])
        stored = {"summary": "s", "unknowns": ["US base unknown", "who runs the account"],
                  "factors": {"p_accept": {"value": 0.3, "reasons": ["prior 0.30"], "evidence_ids": []},
                              "flags": {"value": ["unverified_handle"], "reasons": ["unverified_handle (suspicious): bio says PARODY."]}}}
        conn.execute("UPDATE scores SET reasons_json=? WHERE run_id=2 AND candidate_id=?", (db.jdumps(stored), cid))
        conn.commit()
        d = data.person_detail(conn, 2, cid)
        assert d["unknowns"] == ["who runs the account", "US base unknown"]          # classification first, no duplicates
        assert d["flags"][0]["scoring"] == ["unverified_handle (suspicious): bio says PARODY."]
        assert d["flags"][0]["short"] == "unverified handle"
        assert d["factors"]["p_accept"]["reasons"] == ["prior 0.30"]
    finally:
        conn.close()


# ---------------------------------------------------------------- the Streamlit page itself (AppTest, no server, no network)

DASHBOARD = str(ROOT / "app" / "dashboard.py")


def _app(monkeypatch, db_path, person: int | None = None, bar: bool | None = False):
    """The page on db_path. bar=False switches the brief's bar off, so tests of other features see all 7 US people of
    the demo; None leaves the page's default (on)."""
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("DB_PATH", str(db_path))
    at = AppTest.from_file(DASHBOARD, default_timeout=60)
    if bar is not None:
        at.session_state["tf_bar"] = bar
    if person is not None:
        at.query_params["person"] = str(person)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def test_dashboard_page_runs(demo_path, monkeypatch):
    at = _app(monkeypatch, demo_path, bar=None)   # the page as it opens: the brief's bar on
    assert at.title[0].value == "ring_wishlist ring wishlist"
    assert [t.label for t in at.tabs] == ["Wishlist", "Check an account", "System"]
    assert at.toggle(key="tf_bar").value is True and at.toggle(key="tf_bar").label == (
        "Meets the brief's bar: a person who wears a device or uses a voice tool, and can be contacted")
    assert sum(int(m.value) for m in at.metric) == 3       # the category mix of the top N, no delta by default
    assert not any(m.delta for m in at.metric)
    assert any(e.label == "Run of 2026-09-29 · 8 people scored · since the last run: 2 up, 3 down, 1 new · what moved and why"
               for e in at.expander)
    assert any("Cost of this run: \\$0.92" in c.value for c in at.sidebar.caption)   # escaped: a bare $ pair is LaTeX
    assert any(c.value == "Ranked with the default weights (SCORING.md) · **Manual weights** to change them"
               for c in at.caption)
    assert not any(s.key and s.key.startswith("tw_") for s in at.slider)   # the weights stay hidden by default
    table = at.dataframe[0].value
    assert len(table) == 3 and table.iloc[0]["person"] == "Rina Ring · @ringnerd_test ✓"   # the bar and US only
    assert set(table["category_label"]) <= {"X voice", "Media", "VC", "Founder / angel"}
    assert table.iloc[0]["why"].startswith("Wearables reviewer") and table.iloc[0]["contact"] == "Intro via Marwan Refaat"
    assert any(c.value.startswith("**3 people who meet the bar** · hidden: 1 not US, 4 below the bar · ") for c in at.caption)
    assert any(m.value == "3 people meet the bar: a shorter list with real evidence beats a hundred names." for m in at.markdown)
    assert not at.selectbox   # no row selected -> no detail panel yet
    slider = next(s for s in at.slider if s.label == "Minimum topic affinity")
    slider.set_value(0.7).run()
    assert not at.exception
    assert set(at.dataframe[0].value["person"]) == {"Rina Ring · @ringnerd_test ✓"}   # Frank has no device evidence
    at.toggle(key="tf_bar").set_value(False).run()                                   # off: everyone ranked again
    assert not at.exception and any(c.value.startswith("**2 of 7 people** · 1 filter on · hidden: 1 not US · ")
                                    for c in at.caption)
    assert set(at.dataframe[0].value["person"]) == {"Rina Ring · @ringnerd_test ✓", "Frank Founder · @founder_test (likely real)"}


def test_dashboard_count_line_top_n_of_k_and_nobody_meets_the_bar(tmp_path, monkeypatch):
    assert data.list_count(100, 150, 100, True) == "Top 100 of 150 people who meet the bar"   # the page's lead
    assert data.list_count(66, 66, 100, True) == "66 people who meet the bar"
    assert data.list_count(100, 700, 100, False) == "Top 100 of 700 people"
    none = tmp_path / "nobody.db"   # its own path: the page caches the list per database
    seed(none, quiet=True)
    conn = db.connect(none)
    try:
        conn.execute("UPDATE scores SET reasons_json=NULL")   # runs scored before the prior was stored: nobody qualifies
        conn.commit()
    finally:
        conn.close()
    at = _app(monkeypatch, none, bar=None)
    assert not at.dataframe and any("Nobody in this run meets the brief's bar" in i.value for i in at.info)


def test_dashboard_manual_weights(demo_path, monkeypatch):
    """The switch shows the sliders at the defaults; Run re-ranks with the move against the default; off returns the
    default ranking and keeps the slider values for the next time."""
    at = _app(monkeypatch, demo_path)
    default_order = list(at.dataframe[0].value["person"])
    at.toggle(key="manual_weights").set_value(True).run()
    assert not at.exception and at.slider(key="tw_wear").value == 1.0 and at.slider(key="tw_x_voice").value == 1.0
    assert any(c.value.startswith("Manual weights: still the defaults") for c in at.caption)
    at.slider(key="tw_x_voice").set_value(0.0)
    next(b for b in at.button if b.label == "Run").click().run()
    assert not at.exception
    table = at.dataframe[0].value
    assert list(table["person"]) != default_order and table.iloc[-1]["category_label"] == "X voice"
    assert any(c.startswith("↑") for c in table["change"]) and any(c.startswith("↓") for c in table["change"])
    assert any(c.value.startswith("Ranked with manual weights: X voices 0") for c in at.caption)
    assert any("Entered the top 100 (0)" in h.value for h in at.markdown)   # all 7 fit in the top 100: nobody moves out
    at.toggle(key="manual_weights").set_value(False).run()                  # off: back to the default ranking
    assert not at.exception and list(at.dataframe[0].value["person"]) == default_order
    assert not any(s.key and s.key.startswith("tw_") for s in at.slider) and not any(m.delta for m in at.metric)
    at.toggle(key="manual_weights").set_value(True).run()                   # on again: the last slider values
    assert at.slider(key="tw_x_voice").value == 0.0 and list(at.dataframe[0].value["person"]) == list(table["person"])
    rina = table[table["person"].str.contains("@ringnerd_test")].iloc[0]
    assert int(rina["rank"]) > 1
    at.query_params["person"] = str(int(rina["candidate_id"]))
    at.run()
    assert not at.exception and at.subheader[0].value == f"#{int(rina['rank'])} · Rina Ring"   # the list's rank
    assert any(c.value.startswith("total = 100 x") and "Manual weights sliders" in c.value for c in at.caption)


def test_dashboard_hides_rejected_and_shows_them_on_request(tmp_path, monkeypatch):
    p = tmp_path / "rej.db"
    seed(p, quiet=True)
    conn = db.connect(p)
    try:
        data.set_status(conn, int(_by_handle(data.ranked_table(conn, 2), "vc_test")["candidate_id"]), "rejected")
    finally:
        conn.close()
    at = _app(monkeypatch, p)
    assert len(at.dataframe[0].value) == 6 and not any("@vc_test" in s for s in at.dataframe[0].value["person"])
    assert any("hidden: 1 not US, 1 rejected" in c.value for c in at.caption)
    at.toggle(key="tf_hide_rejected").set_value(False).run()
    assert not at.exception and len(at.dataframe[0].value) == 7


def test_dashboard_us_only_toggle_brings_non_us_people_in_and_ranks_again(tmp_path, monkeypatch):
    """Elke (flagged not US) meets the bar and, here, tops the run: US only (on) leaves her out; off, she is #1 with
    'not US' in her Flags cell and everyone below her moves down one; on again, the list is back."""
    p = tmp_path / "us_only.db"
    seed(p, quiet=True)
    conn = db.connect(p)
    try:
        elke = int(_by_handle(data.ranked_table(conn, 2), "eu_test")["candidate_id"])
        conn.execute("UPDATE scores SET p_accept=0.9, p_love=0.9, impact=0.9 WHERE run_id=2 AND candidate_id=?", (elke,))
        conn.commit()
    finally:
        conn.close()
    at = _app(monkeypatch, p, bar=None)   # the page as it opens: US only and the bar on
    assert at.toggle(key="tf_us_only").value is True
    table = at.dataframe[0].value
    on_order = list(table["candidate_id"])
    assert list(table["rank"]) == [1, 2, 3] and elke not in on_order and table.iloc[0]["person"].startswith("Rina Ring")
    assert any(c.value.startswith("**3 people who meet the bar** · hidden: 1 not US, 4 below the bar · ") for c in at.caption)
    at.toggle(key="tf_us_only").set_value(False).run()
    assert not at.exception
    table = at.dataframe[0].value
    assert list(table["rank"]) == [1, 2, 3, 4] and list(table["candidate_id"]) == [elke] + on_order
    assert table.iloc[0]["person"] == "Elke Uropa · @eu_test (likely real)" and table.iloc[0]["flags_short"] == "not US"
    assert any(c.value.startswith("**4 people who meet the bar** · hidden: 4 below the bar · ") for c in at.caption)
    assert sum(int(m.value) for m in at.metric) == 4                    # the category mix follows
    at.query_params["person"] = str(elke)
    at.run()
    assert not at.exception and at.subheader[0].value == "#1 · Elke Uropa"
    assert any(i.value.startswith("**not US**: Not US-based") for i in at.info)
    at.toggle(key="tf_us_only").set_value(True).run()
    assert not at.exception and list(at.dataframe[0].value["candidate_id"]) == on_order


def test_dashboard_count_line_says_what_each_toggle_adds(tmp_path, monkeypatch):
    """The real list's case: every non-US person is also below the bar, so US only hides nobody on its own. The count
    line says 0 and why (details in its help), and switching US only off indeed adds nobody."""
    p = tmp_path / "overlap_page.db"
    _non_us_below_the_bar(p)
    at = _app(monkeypatch, p, bar=None)
    line = next(c for c in at.caption if c.value.startswith("**3 people who meet the bar**"))
    assert line.value.startswith("**3 people who meet the bar** · hidden: 0 not US (2 non-US people are also below the "
                                 "bar), 3 below the bar · tick the box")
    assert line.proto.help.startswith("Each number is who that filter alone hides: switching it off in Filters adds")
    assert "- US only matches 2 non-US people: 0 hidden by it alone, 2 also below the bar" in line.proto.help
    shown = list(at.dataframe[0].value["candidate_id"])
    at.toggle(key="tf_us_only").set_value(False).run()
    assert not at.exception and list(at.dataframe[0].value["candidate_id"]) == shown
    assert any(c.value.startswith("**3 people who meet the bar** · hidden: 5 below the bar · ") for c in at.caption)
    assert "no other filter here leaves out" in at.toggle(key="tf_us_only").help


def test_dashboard_flags_column_cards_filter_and_panel(tmp_path, monkeypatch):
    """Flags sit right after Category; a competitor tie reads '⚑ <competitor>' in the table and next to the name on a
    card, with the full tie, its detail and the scorer's reason in the person panel. 'Hide competitor-tied' exists and is
    off by default: ties are flagged, never removed."""
    long_tie = "competitor:Wispr Flow rival (VoiceOS, not in the brief's list)"
    p = tmp_path / "ties.db"
    seed(p, quiet=True)
    conn = db.connect(p)
    try:
        df = data.ranked_table(conn, 2)
        vera, tom = (int(_by_handle(df, h)["candidate_id"]) for h in ("vc_test", "techwriter_test"))
        stored = db.jloads(db.one(conn, "SELECT reasons_json FROM scores WHERE run_id=2 AND candidate_id=?", (vera,))["reasons_json"])
        stored["factors"]["flags"] = {"value": [long_tie], "reasons": [
            f"{long_tie}: Vera Capital is an investor in Wispr Flow rival (VoiceOS, not in the brief's list) "
            "(config/competitor_ties.yaml, https://example.com/voiceos)."]}
        conn.execute("UPDATE scores SET flags=?, reasons_json=? WHERE run_id=2 AND candidate_id=?",
                     (db.jdumps([long_tie]), db.jdumps(stored), vera))
        conn.execute("UPDATE scores SET flags=? WHERE run_id=2 AND candidate_id=?",
                     (db.jdumps(["competitor:Sesame", "competitor:Limitless"]), tom))
        conn.commit()
    finally:
        conn.close()
    at = _app(monkeypatch, p, bar=None)   # both meet the bar: flagged, still on the list
    assert at.toggle(key="tf_hide_competitor").label == "Hide competitor-tied"
    assert at.toggle(key="tf_hide_competitor").value is False
    grid = at.dataframe[0]
    order = list(grid.proto.column_order)
    assert order[order.index("category_label") + 1] == "flags_short"
    assert json.loads(grid.proto.columns)["flags_short"]["help"].endswith(
        "Competitor ties are flagged, never removed (the brief). Filters → hide competitor-tied people if you need to.")
    cells = dict(zip(grid.value["candidate_id"], grid.value["flags_short"]))
    assert (cells[vera], cells[tom]) == ("⚑ Wispr Flow rival", "⚑ Sesame, Limitless") and len(cells) == 3
    flag_filter = next(m for m in at.multiselect if m.label == "Flag")
    assert "⚑ Wispr Flow rival (VoiceOS, not in the brief's list) (1)" in flag_filter.options
    at.toggle(key="tf_hide_competitor").set_value(True).run()             # on request only
    assert not at.exception and list(at.dataframe[0].value["candidate_id"]) == [
        c for c in grid.value["candidate_id"] if c not in (vera, tom)]
    assert any("hidden: 1 not US, 2 competitor-tied, 3 below the bar · " in c.value for c in at.caption)  # Cora: both
    at.toggle(key="tf_hide_competitor").set_value(False)
    at.session_state["view_mode"] = "Cards"
    at.run()
    firsts = [m.value.split("  \n")[0] for m in at.markdown if m.value.startswith("**#")]
    assert any("Vera Capital" in f and f.endswith("** · **⚑ Wispr Flow rival**") for f in firsts)
    at = _app(monkeypatch, p, person=vera, bar=None)
    box = next(w.value for w in at.warning if w.value.startswith("**⚑ Wispr Flow rival**"))
    assert "Tied to competitor Wispr Flow rival (VoiceOS, not in the brief's list)" in box
    assert "  \nVera Capital is an investor in Wispr Flow rival" in box and "competitor:" not in box


def test_dashboard_cold_people_are_shown_neutrally(tmp_path, monkeypatch):
    """Rings can go outside the network: no warm intro is fine when another route reaches them (open X DMs here). It is
    never a flag box or a score factor."""
    p = tmp_path / "cold.db"
    seed(p, quiet=True)
    conn = db.connect(p)
    try:
        cid = int(_by_handle(data.ranked_table(conn, 2), "vc_test")["candidate_id"])
        conn.execute("DELETE FROM paths WHERE candidate_id=?", (cid,))
        conn.execute("UPDATE scores SET flags='[\"no_path\"]' WHERE candidate_id=?", (cid,))
        conn.commit()
        df = data.ranked_table(conn, 2)
        assert "vc_test" in set(data.apply_filters(df)["handle"])                 # no default reachability filter
    finally:
        conn.close()
    at = _app(monkeypatch, p)
    table = at.dataframe[0].value
    vera = table[table["person"].str.contains("@vc_test")].iloc[0]
    assert vera["contact"] == "X DMs open" and vera["flags_short"] == "" and 0 <= vera["impact"] <= 1
    assert "impact" in table.columns and "reach" not in table.columns
    at = _app(monkeypatch, p, person=cid)
    text = "\n".join(m.value for m in at.markdown)
    assert "**How to contact:** X DMs open" in text and "Path strength" not in text and "path strength" not in text.lower()
    assert not any("warm intro" in b.value for b in list(at.info) + list(at.warning))


def test_dashboard_first_scored_run(tmp_path, monkeypatch):
    """A brand-new database with one scored run and nothing classified or pathed yet still reads."""
    p = tmp_path / "first.db"
    seed(p, quiet=True)
    conn = db.connect(p)
    try:
        for sql in ("DELETE FROM scores WHERE run_id=2", "DELETE FROM changes", "DELETE FROM classifications",
                    "DELETE FROM paths", "DELETE FROM status", "UPDATE scores SET reasons_json=NULL"):
            conn.execute(sql)
        conn.commit()
    finally:
        conn.close()
    at = _app(monkeypatch, p)
    assert any(e.label.endswith("first scored run") for e in at.expander)
    table = at.dataframe[0].value
    assert len(table) == 7 and set(table["why"]) == {data.NOT_RESEARCHED}
    assert set(table["contact"]) == {"X DMs open", contacts.NONE_LABEL}   # no paths yet: open DMs, or no way in
    at = _app(monkeypatch, p, person=int(table.iloc[0]["candidate_id"]))
    assert any("No change recorded" in c.value or "First scored run" in c.value for c in at.caption)


def test_dashboard_cards_view(demo_path, monkeypatch):
    at = _app(monkeypatch, demo_path)
    at.session_state["view_mode"] = "Cards"
    at.run()
    assert not at.exception and not at.dataframe
    assert any("Rina Ring · @ringnerd\\_test ✓" in m.value for m in at.markdown)   # markdown-escaped
    opens = [b for b in at.button if b.label == "Open"]
    assert len(opens) == 7
    opens[0].click().run()
    assert not at.exception
    assert at.subheader[0].value == "#1 · Rina Ring" and any(b.label == "← Back to the list" for b in at.button)
    next(b for b in at.button if b.label == "← Back to the list").click().run()
    assert not at.exception and not any(s.value.startswith("#") for s in at.subheader)
    assert len([b for b in at.button if b.label == "Open"]) == 7


def test_dashboard_topic_affinity_panel(demo_path, monkeypatch):
    conn = db.connect(demo_path)
    try:
        df = data.ranked_table(conn, 2)
        rina, adrian = (int(_by_handle(df, h)["candidate_id"]) for h in ("ringnerd_test", "adrianaoun_test"))
    finally:
        conn.close()
    at = _app(monkeypatch, demo_path, person=rina)
    text = "\n".join(m.value for m in at.markdown)
    assert "3 of her posts since July" in text
    assert "(https://x.com/ringnerd_test/status/102)" in text and "Best posts" in text
    assert any("own posts + classifier" in c.value for c in at.caption)
    at = _app(monkeypatch, demo_path, person=adrian)
    assert "unknown, not guessed" in "\n".join(m.value for m in at.markdown)


def test_dashboard_runs_without_affinity(tmp_path, monkeypatch):
    """A run scored before topic affinity existed: blank Topic column, disabled filter, 'not measured' in the panel."""
    p = tmp_path / "old.db"
    seed(p, quiet=True)
    conn = db.connect(p)
    try:
        conn.execute("UPDATE scores SET affinity=NULL, reasons_json='{}'")
        conn.commit()
        cid = int(_by_handle(data.ranked_table(conn, 2), "ringnerd_test")["candidate_id"])
    finally:
        conn.close()
    at = _app(monkeypatch, p, person=cid)
    assert len(at.dataframe[0].value) == 7
    assert next(s for s in at.slider if s.label == "Minimum topic affinity").disabled
    assert any("Not measured for this run" in c.value for c in at.caption)


def test_dashboard_engagement_column_panel_and_weight(tmp_path, monkeypatch):
    """One Engagement column (the one-cell summary, 'unknown' without a sample); in the person panel the table, the
    arithmetic and the pace, or one line when unknown; a slider once weights.py knows the key."""
    p = tmp_path / "eng_page.db"
    rina, vera = _with_engagement(p)
    at = _app(monkeypatch, p)
    cells = dict(zip(at.dataframe[0].value["candidate_id"], at.dataframe[0].value["engagement_short"]))
    assert cells[rina] == "ER 0.44% · 2.0× peers" and {v for c, v in cells.items() if c != rina} == {"unknown"}
    at.session_state["view_mode"] = "Cards"
    at.run()
    assert any("Engagement: ER 0.44% · 2.0× peers" in c.value for c in at.caption)
    at = _app(monkeypatch, p, person=rina)
    assert ("##### Community engagement\n\nER 0.44% · 2.0× peers · score factor 0.75 (0.50 = typical for their size)"
            in [m.value for m in at.markdown])
    shown = at.table[0].value
    assert list(shown.index) == list(data.engagement_view(None, ENGAGEMENT)["table"].index)
    assert list(shown.iloc[0]) == ["12,400", "310", "42", "25", "377"]
    assert any(c.value.startswith("Average post over 20 latest posts")    # the reasons already end with the pace
               and "posts a week ·" not in c.value for c in at.caption)
    at = _app(monkeypatch, p, person=vera)
    assert "Community engagement unknown: no sample of their latest posts yet" in [c.value for c in at.caption]
    assert not at.table
    at.toggle(key="manual_weights").set_value(True).run()
    assert not at.exception
    assert any(s.key == "tw_engagement" for s in at.slider) == ("engagement" in weights.LABELS)   # none until it has one
    monkeypatch.setitem(weights.LABELS, "engagement", "Community engagement")     # weights.py's key, as the lead adds it
    monkeypatch.setitem(weights.HELP, "engagement", "How much community engagement counts.")
    monkeypatch.setitem(weights.DEFAULT_WEIGHTS, "engagement", 1.0)
    at.run()
    labels = [s.label for s in at.slider if s.key and s.key.startswith("tw_")]
    assert not at.exception and at.slider(key="tw_engagement").value == weights.effective({})[0]["engagement"]
    assert labels[labels.index("Impact if they love it") + 1] == "Community engagement"


def test_check_tab_shows_community_engagement(tmp_path, monkeypatch):
    """A check's answer shows the person panel's engagement block, from the factor and value it carries."""
    from streamlit.testing.v1 import AppTest
    p = tmp_path / "check.db"
    seed(p, quiet=True)
    conn = db.connect(p)
    try:
        conn.execute("UPDATE x_posts SET fetched_at=? WHERE x_id='2'", (db.utcnow(),))   # a free Show for @ringnerd_test
        conn.commit()
        ids = [check.show_stored(conn, "ringnerd_test", table=data.ranked_table(conn, 2, precise=True))["check_id"]
               for _ in range(2)]
        for check_id, fac in zip(ids, (ENGAGEMENT, None)):
            ans = db.jloads(db.one(conn, "SELECT result_json FROM checks WHERE check_id=?", (check_id,))["result_json"])
            ans.pop("engagement", None)
            ans["person"].pop("engagement", None)
            if fac:
                ans["engagement"], ans["person"]["engagement"] = fac, fac["value"]
            conn.execute("UPDATE checks SET result_json=? WHERE check_id=?", (db.jdumps(ans), check_id))
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setenv("DB_PATH", str(p))
    at = AppTest.from_file(DASHBOARD, default_timeout=60)
    at.session_state["main_tab"] = "Check an account"
    at.session_state["ck_open"] = ids[0]
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert ("**Community engagement** · ER 0.44% · 2.0× peers · score factor 0.75 (0.50 = typical for their size)"
            in [m.value for m in at.markdown])
    assert list(at.table[0].value.iloc[3]) == ["1.5×", "2.0×", "2.5×", "1.8×", "2.0×"]
    at.session_state["ck_open"] = ids[1]
    at.run()
    assert not at.exception and not at.table
    assert "Community engagement unknown: no sample of their latest posts yet" in [c.value for c in at.caption]


def test_dashboard_detail_panel_and_status_persist(tmp_path, monkeypatch):
    p = tmp_path / "page.db"
    seed(p, quiet=True)
    conn = db.connect(p)
    cid = int(_by_handle(data.ranked_table(conn, 2), "founder_test")["candidate_id"])
    cora = int(_by_handle(data.ranked_table(conn, 2), "competitor_test")["candidate_id"])
    conn.close()
    at = _app(monkeypatch, p, person=cid)
    assert at.subheader[0].value == "#4 · Frank Founder"
    status = next(s for s in at.selectbox if s.label == "Status")
    assert status.value == "candidate"
    status.select("ring_sent").run()
    assert not at.exception
    next(t for t in at.text_area if t.label == "Note").input("Sent via Max").run()
    assert not at.exception
    conn = db.connect(p)
    try:
        row = data.get_status(conn, cid)
        assert row["status"] == "ring_sent" and row["note"] == "Sent via Max" and row["updated_at"]
    finally:
        conn.close()
    assert "ring sent" in list(at.dataframe[0].value["status_label"])
    at = _app(monkeypatch, p, person=cora)   # competitor ties are shown in a warning box, never hidden
    assert any(w.value.startswith("**⚑ Oura**: Tied to competitor Oura: product lead") for w in at.warning)


def test_dashboard_empty_states(tmp_path, monkeypatch):
    missing = tmp_path / "nothing" / "wishlist.db"
    at = _app(monkeypatch, missing)
    assert any("No database at" in i.value for i in at.info)
    assert any("make run" in m.value for m in at.markdown)
    assert not missing.exists()              # the page never creates a database
    empty = tmp_path / "empty.db"
    db.connect(empty).close()
    at = _app(monkeypatch, empty)
    assert any("is empty" in i.value for i in at.info) and not at.dataframe


def test_person_cell_links_to_their_x_profile():
    import re
    link = data.person_link("Jesse Middleton", "srcasm", "verified")
    assert link == "https://x.com/srcasm#Jesse Middleton · @srcasm ✓"
    assert re.match(data.PERSON_LINK_TEXT, link).group(1) == "Jesse Middleton · @srcasm ✓"   # what the table shows
    assert data.person_link("Ann Map", None, None) == "https://x.com/search?q=Ann%20Map&f=user#Ann Map · no X handle"
    assert data.person_link(None, None, None) is None
