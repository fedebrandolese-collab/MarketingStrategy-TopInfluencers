"""Workstream A: map ingestion from the real raw export (exact counts), idempotency, fetch_map against a fake session,
byte-identical raw round-trip, stats and the CLI. Offline: nothing here touches the network."""
import json
import re
from pathlib import Path

import pytest

from wearable_wishlist import config, db, ingest_map

RAW = config.RAW_DIR
SAMPLE = Path(__file__).parent / "samples" / "a" / "map_payload_small.json"
TABLES = ("map_people", "map_edges", "map_competitor_funds", "map_linkedin", "map_monthly")
# Expected on the real export in data/raw (Sep 29 2026): 319 person records, 318 distinct ids (id 167 is in both groups).
EXPECTED = {"people_capital": 131, "people_press": 188, "people": 318, "edges": 75, "competitor_funds": 12, "linkedin": 46, "monthly": 66}


def table_counts(conn):
    return {t: db.one(conn, f"SELECT COUNT(*) AS n FROM {t}")["n"] for t in TABLES}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Hermetic: no passphrase from .env can leak in, and any attempt to open a real HTTP session fails loudly."""
    monkeypatch.delenv("MAP_PASSPHRASE", raising=False)

    def _boom(*a, **k):
        raise AssertionError("tests must not open a real requests.Session")
    monkeypatch.setattr(ingest_map.requests, "Session", _boom)


# ------------------------------------------------ fake HTTP session serving data/raw ------------------------------------------------

class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code, self._json, self.text = status_code, json_data, text

    def json(self):
        if self._json is None:
            raise ValueError("not JSON")
        return self._json


class FakeMapSession:
    """The map's HTTP surface: POST /login (form field p) sets the session; before that, API GETs serve the login page."""

    def __init__(self, passphrase="test-pass", raw_dir=None):
        self.passphrase, self.logged_in, self.calls = passphrase, False, []
        payload = ingest_map.load_raw(raw_dir)
        self.graphs = {g: payload[g] for g in ("capital", "press")}
        for g in self.graphs.values():   # the live API serves warmth as a float (0.0); the browser export printed 0
            for person in g["people"]:
                person["warmth"] = float(person["warmth"])
        self.timelines = payload["timelines"]

    def post(self, url, data=None, **kw):
        self.calls.append(("POST", url, dict(data or {})))
        if url.endswith("/login") and (data or {}).get("p") == self.passphrase:
            self.logged_in = True
            return FakeResponse(200, text="<html>graph</html>")
        return FakeResponse(401, text="<html>login</html>")

    def get(self, url, params=None, **kw):
        params = dict(params or {})
        self.calls.append(("GET", url, params))
        if not self.logged_in:
            return FakeResponse(200, text="<html>login page</html>")
        if url.endswith("/api/graph") and params.get("group") in self.graphs:
            return FakeResponse(200, json_data=self.graphs[params["group"]])
        if url.endswith("/api/timeline") and str(params.get("id")) in self.timelines:
            return FakeResponse(200, json_data=self.timelines[str(params["id"])])
        return FakeResponse(404, text="not found")


# ------------------------------------------------ raw files ------------------------------------------------

def test_load_raw_shape():
    payload = ingest_map.load_raw()
    assert set(payload) == {"exported_at", "capital", "press", "timelines"}
    for g in ("capital", "press"):
        assert payload[g]["group"] == g
        assert set(payload[g]) >= {"people", "edges", "monthly", "counts", "competitor_funds"}
    assert len(payload["capital"]["people"]) == 131 and len(payload["press"]["people"]) == 188
    assert len(payload["timelines"]) == 318
    assert payload["exported_at"].endswith("Z")


def test_save_raw_roundtrip_is_byte_identical(tmp_path):
    ingest_map.save_raw(ingest_map.load_raw(), raw_dir=tmp_path)
    for f in ingest_map.RAW_FILES.values():
        assert (tmp_path / f).read_bytes() == (RAW / f).read_bytes(), f
    assert not list(tmp_path.glob("*.tmp"))


def test_load_raw_missing_files(tmp_path):
    with pytest.raises(FileNotFoundError):
        ingest_map.load_raw(tmp_path)
    assert not ingest_map.raw_available(tmp_path) and ingest_map.raw_available()


# ------------------------------------------------ ingestion ------------------------------------------------

def test_run_ingest_from_real_raw(conn):
    counts = ingest_map.run_ingest(conn, run_id=None, from_raw=True)
    assert {k: counts[k] for k in EXPECTED} == EXPECTED
    assert counts["people_in_both_groups"] == [167] and counts["stale_people"] == 0
    assert counts["origin"].startswith("raw:")
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", counts["exported_at"])
    assert table_counts(conn) == {"map_people": 318, "map_edges": 75, "map_competitor_funds": 12, "map_linkedin": 46, "map_monthly": 66}
    by_group = {r["group_name"]: r["n"] for r in db.rows(conn, "SELECT group_name, COUNT(*) AS n FROM map_people GROUP BY group_name")}
    assert by_group == {"capital": 131, "press": 187}   # 188 press records, one of them (167) already stored under capital
    # spot checks against the export
    p = db.one(conn, "SELECT * FROM map_people WHERE id=391")
    assert p["name"] == "Roberto Veronese" and p["group_name"] == "capital" and p["sector"] == "vc" and p["warmth"] == 327.746
    assert p["tags"] == "linkedin,vc" and p["channels"] == "imessage,linkedin_msg" and p["last_contact"] == "2026-09-16T18:07:49Z"
    assert p["imported_at"] and re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", p["imported_at"])
    li = db.one(conn, "SELECT * FROM map_linkedin WHERE person_id=13")
    assert li["company"] == "Bessemer Venture Partners" and li["profile_url"] == "https://www.linkedin.com/in/isabellamandis"
    assert db.one(conn, "SELECT COUNT(*) AS n FROM map_linkedin WHERE person_id NOT IN (SELECT id FROM map_people)")["n"] == 0
    f = db.one(conn, "SELECT * FROM map_competitor_funds WHERE fund='Adjacent'")
    assert f["competitor"] == "Sandbar" and f["round"].startswith("co-led $23M Series A")
    assert db.one(conn, "SELECT COUNT(*) AS n FROM map_edges WHERE group_name='press'")["n"] == 1
    assert db.one(conn, "SELECT * FROM map_monthly WHERE group_name='capital' AND month='2025-01' AND source='imessage'")["n"] == 446
    assert db.one(conn, "SELECT COUNT(*) AS n FROM map_edges WHERE from_id NOT IN (SELECT id FROM map_people) OR to_id NOT IN (SELECT id FROM map_people)")["n"] == 0


def test_person_in_both_groups_stored_once(conn):
    ingest_map.run_ingest(conn)
    p = db.one(conn, "SELECT * FROM map_people WHERE id=167")
    assert p["name"] == "Andrew Yeung" and p["group_name"] == "capital" and p["sector"] == "angel"
    assert set(p["tags"].split(",")) == {"angel", "investor", "linkedin"}


def test_run_ingest_is_idempotent(conn):
    first = ingest_map.run_ingest(conn)
    before = table_counts(conn)
    tags_before = db.one(conn, "SELECT tags FROM map_people WHERE id=167")["tags"]
    second = ingest_map.run_ingest(conn, run_id=7)
    assert table_counts(conn) == before
    assert {k: second[k] for k in EXPECTED} == {k: first[k] for k in EXPECTED}
    assert second["stale_people"] == 0 and second["run_id"] == 7
    assert db.one(conn, "SELECT tags FROM map_people WHERE id=167")["tags"] == tags_before


def test_ingest_payload_edge_cases(conn):
    payload = json.loads(SAMPLE.read_text())
    counts = ingest_map.ingest_payload(conn, payload)
    assert {k: counts[k] for k in EXPECTED} == {"people_capital": 2, "people_press": 2, "people": 3, "edges": 2,
                                                "competitor_funds": 1, "linkedin": 1, "monthly": 2}
    assert counts["people_in_both_groups"] == [2]
    bo = db.one(conn, "SELECT * FROM map_people WHERE id=2")
    assert bo["group_name"] == "capital" and bo["sector"] == "angel" and bo["tags"] == "angel,investor,press"
    cy = db.one(conn, "SELECT * FROM map_people WHERE id=3")
    assert cy["channels"] is None and cy["last_contact"] is None and cy["warmth"] == 0
    assert db.rows(conn, "SELECT person_id FROM map_linkedin") == [{"person_id": 1}]
    # a refreshed export that drops a person: reported as stale, not deleted
    payload["press"]["people"] = payload["press"]["people"][:1]
    counts = ingest_map.ingest_payload(conn, payload)
    assert counts["stale_people"] == 1 and table_counts(conn)["map_people"] == 3
    assert ingest_map.ingest_payload(conn, payload)["stale_people"] == 1 and table_counts(conn)["map_people"] == 3


def test_run_ingest_refresh_needs_passphrase(conn, monkeypatch):
    monkeypatch.delenv("MAP_PASSPHRASE", raising=False)
    with pytest.raises(RuntimeError, match="MAP_PASSPHRASE"):
        ingest_map.run_ingest(conn, from_raw=False)
    assert table_counts(conn)["map_people"] == 0


# ------------------------------------------------ fetch_map (fake session, no network) ------------------------------------------------

def test_fetch_map_with_fake_session(tmp_path):
    fake = FakeMapSession("test-pass")
    payload = ingest_map.fetch_map("test-pass", url="https://map.test/", session=fake)
    assert fake.calls[0] == ("POST", "https://map.test/login", {"p": "test-pass"})
    assert fake.calls[1] == ("GET", "https://map.test/api/graph", {"group": "capital"})
    assert fake.calls[2] == ("GET", "https://map.test/api/graph", {"group": "press"})
    timeline_calls = [c for c in fake.calls if c[1] == "https://map.test/api/timeline"]
    assert len(timeline_calls) == 318 and len(fake.calls) == 3 + 318
    ids = [c[2]["id"] for c in timeline_calls]
    assert ids == sorted(ids) and len(set(ids)) == 318   # one call per distinct id, 167 not fetched twice
    assert set(payload) == {"capital", "press", "timelines", "exported_at"}
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", payload["exported_at"])
    raw = ingest_map.load_raw()
    assert payload["capital"] == raw["capital"] and payload["press"] == raw["press"] and payload["timelines"] == raw["timelines"]
    assert set(payload["timelines"]) == {str(i) for i in ids}
    # what fetch_map returns is exactly what save_raw persists: same four files as the export (modulo the timestamp)
    payload["exported_at"] = raw["exported_at"]
    ingest_map.save_raw(payload, raw_dir=tmp_path)
    for f in ingest_map.RAW_FILES.values():
        assert (tmp_path / f).read_bytes() == (RAW / f).read_bytes(), f


def test_fetch_map_then_ingest(conn):
    payload = ingest_map.fetch_map("test-pass", url="https://map.test", session=FakeMapSession("test-pass"))
    counts = ingest_map.ingest_payload(conn, payload, run_id=1)
    assert {k: counts[k] for k in EXPECTED} == EXPECTED


def test_fetch_map_wrong_passphrase():
    fake = FakeMapSession("test-pass")
    with pytest.raises(RuntimeError, match="login failed"):
        ingest_map.fetch_map("wrong", url="https://map.test", session=fake)
    assert len(fake.calls) == 1   # stops after the failed login, never walks the API


def test_fetch_map_detects_login_page_instead_of_json():
    class SilentLogin(FakeMapSession):   # some servers answer 200 to a bad passphrase and serve the login page on API calls
        def post(self, url, data=None, **kw):
            self.calls.append(("POST", url, dict(data or {})))
            return FakeResponse(200, text="<html>login</html>")
    with pytest.raises(RuntimeError, match="not JSON"):
        ingest_map.fetch_map("wrong", url="https://map.test", session=SilentLogin("test-pass"))


def test_fetch_map_refuses_real_session_in_dry_run(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")
    with pytest.raises(RuntimeError, match="DRY_RUN"):
        ingest_map.fetch_map("anything")   # no session injected -> refused before requests.Session() is touched


def test_fetch_map_requires_passphrase():
    fake = FakeMapSession("test-pass")
    with pytest.raises(RuntimeError, match="MAP_PASSPHRASE"):
        ingest_map.fetch_map("", session=fake)
    assert fake.calls == []


# ------------------------------------------------ stats and CLI ------------------------------------------------

def test_stats(conn):
    ingest_map.run_ingest(conn)
    st = ingest_map.stats(conn)
    assert st["people"] == {"total": 318, "capital": 131, "press": 187, "with_last_contact": 185}
    assert st["sectors"] == {"press": 187, "vc": 102, "angel": 20, "founder": 8, "connector": 1}   # 167 counts once, as angel; one other connector
    assert st["tags"]["press"] == 187 and st["tags"]["vc"] == 87 and st["tags"]["coverage"] == 102
    assert st["tagged"] == {"x_following": 24, "x_follower": 16, "competitor-investor": 4, "fresh-fund": 17}
    assert st["channels"] == {"gmail": 159, "meeting": 85, "linkedin_msg": 19, "imessage": 12}
    assert st["edges"] == {"total": 75, "by_source": {"transcript": 30, "vc_sheet": 26, "imessage": 11, "offered": 8}}
    assert st["competitor_funds"] == {"total": 12, "by_competitor": {"Sandbar": 7, "Ultrahuman": 5}}
    assert st["linkedin_connections"] == 46
    assert st["monthly"] == {"rows": 66, "from": "2024-12", "to": "2026-09"}
    assert st["warmth"]["min"] == 0 and st["warmth"]["max"] == 327.746 and st["warmth"]["people_above_zero"] == 185
    assert st["last_import"]


def test_stats_empty(conn):
    st = ingest_map.stats(conn)
    assert st["people"]["total"] == 0 and st["warmth"]["max"] is None and st["tagged"]["x_following"] == 0


def test_cli_from_raw(tmp_path, capsys):
    dbfile = tmp_path / "cli.db"
    assert ingest_map.main(["--db", str(dbfile)]) == 0
    out = capsys.readouterr().out
    first, rest = out.split("\n", 1)
    assert first.startswith("[ingest_map] run 1: 131 capital + 188 press people (318 rows, 1 in both groups), 75 edges, 12 competitor funds, 46 linkedin, 66 monthly")
    assert json.loads(rest)["tagged"]["x_following"] == 24
    c = db.connect(dbfile)
    assert db.one(c, "SELECT stage, finished_at FROM runs WHERE run_id=1")["stage"] == "ingest"
    assert db.one(c, "SELECT finished_at FROM runs WHERE run_id=1")["finished_at"]
    assert table_counts(c)["map_people"] == 318
    c.close()
