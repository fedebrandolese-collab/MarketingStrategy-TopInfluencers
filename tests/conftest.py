"""Shared test fixtures. Tests never touch the network: DRY_RUN=1 is forced here."""
import json
import os

os.environ["DRY_RUN"] = "1"

import pytest  # noqa: E402

from ring_wishlist import config, db  # noqa: E402

# Alerts (src/ring_wishlist/ops.py) never leave the machine in tests, even with a webhook in .env, and pop no notifications.
os.environ.pop("NOTIFY_WEBHOOK_URL", None)
os.environ["RING_WISHLIST_NO_DESKTOP_NOTIFY"] = "1"


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    c = db.connect(tmp_path / "test.db")
    yield c
    c.close()


def load_map_fixtures(conn):
    fx = config.FIXTURES_DIR
    now = db.utcnow()
    for p in json.loads((fx / "map_people.json").read_text()):
        db.upsert(conn, "map_people", {
            "id": p["id"], "group_name": p["group_name"], "name": p["name"], "company": p.get("company"), "role": p.get("role"),
            "sector": p.get("sector"), "tags": p.get("tags"), "warmth": p.get("warmth"), "intros_made": p.get("intros_made"),
            "intros_got": p.get("intros_got"), "interactions": p.get("interactions"), "sent": p.get("sent"), "received": p.get("received"),
            "meetings": p.get("meetings"), "channels": p.get("channels"), "first_contact": p.get("first_contact"),
            "last_contact": p.get("last_contact"), "imported_at": now}, keys=("id",))
    for e in json.loads((fx / "map_edges.json").read_text()):
        db.upsert(conn, "map_edges", {"from_id": e["from_id"], "to_id": e["to_id"], "source": e["source"], "group_name": e["group_name"]},
                  keys=("from_id", "to_id", "source", "group_name"))
    for f in json.loads((fx / "map_competitor_funds.json").read_text()):
        db.upsert(conn, "map_competitor_funds", {"fund": f["fund"], "competitor": f["competitor"], "round": f.get("round")}, keys=("fund", "competitor"))
    for pid, li in json.loads((fx / "map_linkedin.json").read_text()).items():
        db.upsert(conn, "map_linkedin", {"person_id": int(pid), "company": li.get("company"), "position": li.get("position"),
                                         "connected_on": li.get("connected_on"), "profile_url": li.get("profile_url")}, keys=("person_id",))
    conn.commit()
    return conn


@pytest.fixture
def map_conn(conn):
    return load_map_fixtures(conn)


@pytest.fixture
def fixture_source():
    from ring_wishlist.sources.base import FixtureSource
    return FixtureSource()


@pytest.fixture(autouse=True)
def no_research_routes(tmp_path_factory, monkeypatch):
    """Researched contact routes come from config/contact_routes in real runs; tests start with none, so the repo's
    research never changes a test. A test that needs some writes its own directory."""
    from ring_wishlist import contacts
    monkeypatch.setattr(contacts, "ROUTES_DIR", tmp_path_factory.getbasetemp() / "no_contact_routes")
