"""Handle verification: same numeric id on the official lookup, badge/age/followers, parody signals."""
from ring_wishlist import db
from ring_wishlist.sources.base import XUser
from ring_wishlist.verify import run_verify


class FakeOfficial:
    name = "x_api"

    def __init__(self, users):
        self.users = {u.handle: u for u in users}

    def get_users_by_handles(self, handles):
        return [self.users[h] for h in handles if h in self.users]


def add(conn, cid, handle, x_id, category="x_voice", verdict=None):
    conn.execute("INSERT INTO candidates (candidate_id, x_id, handle, name, category, handle_verified) VALUES (?,?,?,?,?,?)",
                 (cid, x_id, handle, handle.title(), category, verdict))


def test_verify_verdicts(conn):
    add(conn, 1, "realvoice", "10")
    add(conn, 2, "newish", "20")
    add(conn, 3, "renamed", "30")
    add(conn, 4, "gone", "40")
    add(conn, 5, "funny", "50")
    add(conn, 6, "skipme", "60", category="other")
    conn.commit()
    official = FakeOfficial([
        XUser(x_id="10", handle="realvoice", name="Real Voice", bio="Reviews wearables", verified=True, verified_type="blue",
              created_at="2015-01-01T00:00:00Z", followers=50000),
        XUser(x_id="20", handle="newish", name="New Ish", bio="builder", verified=False, verified_type="none",
              created_at="2026-01-01T00:00:00Z", followers=300),
        XUser(x_id="99", handle="renamed", name="Someone Else", bio="", verified=False, created_at="2020-01-01T00:00:00Z"),
        XUser(x_id="50", handle="funny", name="Ring Guy (parody)", bio="Parody account", verified=True, verified_type="blue",
              created_at="2016-01-01T00:00:00Z", followers=90000),
    ])
    run_id = db.new_run(conn, "verify")
    stats = run_verify(conn, run_id, official, top=10)
    got = {r["handle"]: r["handle_verified"] for r in db.rows(conn, "SELECT handle, handle_verified FROM candidates")}
    assert got["realvoice"] == "verified"
    assert got["newish"] == "likely_real"
    assert got["renamed"] == "suspicious" and stats["id_mismatch"] == 1
    assert got["gone"] == "unknown"
    assert got["funny"] == "suspicious"
    assert got["skipme"] is None, "category 'other' is not verified"
    ev = db.rows(conn, "SELECT candidate_id, text FROM evidence WHERE supports='authenticity'")
    assert len(ev) == 5 and all(e["text"].startswith("handle check:") for e in ev)


class CountingOfficial(FakeOfficial):
    def __init__(self, users):
        super().__init__(users)
        self.asked = []

    def get_users_by_handles(self, handles):
        self.asked += list(handles)
        return super().get_users_by_handles(handles)


def test_verify_skips_handles_confirmed_this_week(conn):
    add(conn, 1, "realvoice", "10")
    add(conn, 2, "newish", "20")
    conn.commit()
    users = [XUser(x_id="10", handle="realvoice", name="Real Voice", bio="", verified=True, verified_type="blue",
                   created_at="2015-01-01T00:00:00Z", followers=50000)]
    first = CountingOfficial(users)
    run_verify(conn, db.new_run(conn, "verify"), first, top=10)
    assert sorted(first.asked) == ["newish", "realvoice"]
    add(conn, 3, "latecomer", "30")
    conn.commit()
    again = CountingOfficial(users)
    stats = run_verify(conn, db.new_run(conn, "verify"), again, top=10)
    assert again.asked == ["latecomer"], "handles confirmed this week are not bought again"
    assert stats["skipped_recent"] == 2 and stats["checked"] == 1
