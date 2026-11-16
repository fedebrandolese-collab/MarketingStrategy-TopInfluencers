"""Contact routes (src/ring_wishlist/contacts.py) and the reach half of the brief's bar: the loader, the route order and labels,
the bar, the export fields, the count line and the check tab. Offline: research comes from tmp yaml files."""
import csv
import json
import logging
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ring_wishlist import check, contacts, db, export, weights  # noqa: E402
from app import data  # noqa: E402
from scripts.seed_demo_db import seed  # noqa: E402

TOM_YAML = """
people:
  - handle: "@TechWriter_Test"
    name: Tom Writer
    verdict: contactable
    routes:
      - {type: linkedin, detail: "https://www.linkedin.com/in/tomwriter/", source_url: "https://gadgetdaily.example/tom", note: "Linked from his author page"}
      - {type: email, detail: "tom@gadgetdaily.example", source_url: "https://gadgetdaily.example/tom", note: "On his author page"}
    reason: His outlet publishes his address.
"""


def _yaml(d: Path, name: str, text: str) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")
    return d


# ---------------------------------------------------------------- the loader

def test_loader_reads_every_file_skips_broken_ones_and_matches_handle_or_name(tmp_path, caplog):
    assert contacts.load_routes(tmp_path / "missing") == {}             # no directory: no researched routes
    d = _yaml(tmp_path / "routes", "batch_1.yaml", TOM_YAML)
    _yaml(d, "batch_2.yaml", """
people:
  - handle: https://x.com/Tom_Other
    name: Tom Other
    verdict: not_contactable
    routes: []
    reason: "X DMs closed, no warm path, no site or email."
  - name: "Nadia  No-Handle"
    verdict: contactable
    routes: [{type: contact form, detail: "https://nadia.example/contact", source_url: "https://nadia.example/contact"}]
  - {handle: techwriter_test, verdict: contactable, routes: [{type: agent, detail: "Big Agency, https://big.example", source_url: "https://big.example"}]}
""")
    _yaml(d, "batch_3.yaml", "people: [unclosed")                      # broken: logged and skipped
    _yaml(d, "batch_4.yaml", "just: a mapping\n")                       # no people list: skipped
    with caplog.at_level(logging.WARNING, logger="ring_wishlist.contacts"):
        idx = contacts.load_routes(d)
    assert "batch_3.yaml" in caplog.text and "batch_4.yaml" in caplog.text
    tom = contacts.researched_for(idx, "TECHWRITER_TEST")
    assert tom["verdict"] == "contactable" and tom["files"] == ["batch_1.yaml", "batch_2.yaml"]   # two files, merged
    assert [r["type"] for r in tom["routes"]] == ["linkedin", "email", "agent"]
    assert contacts.researched_for(idx, "@tom_other")["verdict"] == "not_contactable"             # an x.com link
    nadia = contacts.researched_for(idx, None, "nadia no-handle")                                 # by name
    assert nadia["routes"][0]["type"] == "contact_form"
    assert contacts.researched_for(idx, "nobody") is None and contacts.researched_for({}, "techwriter_test") is None


# ---------------------------------------------------------------- one person's routes

def test_routes_best_first_warm_dm_research_bio_then_weak_and_unconfirmed():
    research = {"verdict": "contactable", "routes": [
        {"type": "linkedin", "detail": "https://linkedin.com/in/x", "source_url": "https://x.example", "note": "weak fallback"},
        {"type": "press_office", "detail": "press@x.example", "source_url": "https://x.example", "note": "Unconfirmed: site expired"},
        {"type": "email", "detail": "sponsors@show.example", "source_url": "https://show.example", "note": "Sponsor inbox"},
        {"type": "contact_form", "detail": "https://x.example/contact", "source_url": "https://x.example/contact", "note": None},
    ]}
    paths = [{"how": "x_direct", "you_rel": "follow each other on X", "strength": 0.35},
             {"how": "via", "via_name": "Jane Doe", "target_rel": "introduced them in the map", "strength": 0.5},
             {"how": "direct", "you_rel": "met · 3 meetings", "strength": 0.8}]
    c = contacts.contact_for(paths, can_dm=1, bio="Writer. Reach me: hi@x.example", handle="@Xperson", researched=research)
    assert c["contactable"] and c["contact_reason"] == ""
    assert [r["label"] for r in c["routes"]] == [
        "Intro via Jane Doe", "Carlo knows them", "X DMs open", "Contact form on x.example", "Email in X bio: hi@x.example",
        "LinkedIn (weak)", "Email: sponsors@show.example (weak)", "Press email: press@x.example (unconfirmed)"]
    assert c["best"] == "Intro via Jane Doe" and c["routes"][2]["source_url"] == "https://x.com/xperson"
    assert contacts.export_routes(c)[3] == {"type": "contact_form", "detail": "https://x.example/contact",
                                            "source_url": "https://x.example/contact"}
    # a follower of Carlo can be messaged on X even with DMs closed; a reply alone is not a way in
    assert contacts.contact_for([{"how": "x_direct", "you": "follows you on X"}], can_dm=0)["best"] == "X DM: follows Carlo"
    assert not contacts.contact_for([{"how": "x_direct", "you": "replied to you on X · 2 posts"}], can_dm=0)["contactable"]
    # an unconfirmed route is never the best route, nor enough alone
    only = contacts.contact_for([], can_dm=0, researched={"verdict": "contactable", "routes": [research["routes"][1]]})
    assert not only["contactable"] and only["best"] == contacts.NONE_LABEL and "only unconfirmed" in only["contact_reason"]


def test_labels_say_how_and_where():
    lab = contacts.route_label
    assert lab({"type": "email", "detail": "Max Tech business email in YouTube 'Channel details' (View email address)",
                "source_url": "https://www.youtube.com/@MaxTechOfficial/about"}) == "Business email on the YouTube About page"
    assert lab({"type": "press_office", "detail": "https://www.bryanjohnson.com/c", "source_url": "https://www.bryanjohnson.com/c",
                "note": "Press Inquiries email, obscured by Cloudflare"}) == "Press email on bryanjohnson.com/c"
    assert lab({"type": "agent", "detail": "UTA Speakers, https://www.unitedtalent.com/speakers/"}) == "Agency: UTA Speakers"
    assert lab({"type": "other_social", "detail": "https://www.instagram.com/someone/", "note": "Weak: message request"}) == \
        "instagram.com/someone (weak)"
    assert lab({"type": "newsletter_reply", "detail": "https://modelrouting.substack.com"}) == \
        "Newsletter reply: modelrouting.substack.com"
    assert contacts.bio_emails("Tips: a.b+c@news.example.com. @handle not@mail") == ["a.b+c@news.example.com"]


def test_not_contactable_says_why():
    none = contacts.contact_for([{"how": "none"}], can_dm=0, bio="Editor @Outlet", handle="z")
    assert (none["contactable"], none["best"], none["routes"]) == (False, contacts.NONE_LABEL, [])
    assert none["contact_reason"] == "no way to contact found: X DMs closed, no warm path, no public route researched yet"
    no = {"verdict": "not_contactable", "routes": [], "reason": "X DMs closed, no warm path, no published email or site."}
    c = contacts.contact_for([], can_dm=0, bio="mail me: z@z.example", researched=no)   # the research overrides a bio email
    assert not c["contactable"]
    assert c["contact_reason"] == "no way to contact found: X DMs closed, no warm path, no published email or site"
    assert contacts.contact_for([], can_dm=1, researched=no)["best"] == "X DMs open"          # open DMs still count
    assert contacts.contact_for([{"how": "via", "via": "Ann"}], can_dm=None, researched=no)["best"] == "Intro via Ann"
    assert "X DMs unknown" in contacts.contact_for([], can_dm=None)["contact_reason"]


# ---------------------------------------------------------------- the bar

def test_the_bar_needs_a_way_to_contact_them():
    reason = "no way to contact found: X DMs closed, no warm path, not researched"
    row = {"category": "media", "wear_evidence": True, "contactable": False, "flags": [], "status": "candidate"}
    assert weights.meets_device_bar(row) and not weights.meets_bar(row) and weights.hidden_by(row) == ["bar"]
    assert weights.meets_bar({**row, "contactable": True}) and weights.hidden_by({**row, "contactable": True}) == []
    assert not weights.meets_bar({"category": "media", "wear_evidence": True})              # unknown reach fails
    assert not weights.meets_bar({**row, "contactable": float("nan")})
    assert weights.bar_reason("media", "for", reason) == reason and weights.bar_reason("media", "for") == ""
    assert weights.bar_reason("other", "none", False) == "; ".join(
        [weights.BAR_NOT_PERSON, weights.BAR_NO_EVIDENCE, weights.BAR_NO_CONTACT])
    df = pd.DataFrame([{**row, "candidate_id": 1, "p_accept": .9, "p_wear": .9, "p_love": .9, "impact": 1.0},
                       {**row, "candidate_id": 2, "contactable": True, "p_accept": .5, "p_wear": .5, "p_love": .5, "impact": .5}])
    assert list(weights.rerank(df)["candidate_id"]) == [2]
    assert list(weights.rerank(df, filters={"bar": False})["candidate_id"]) == [1, 2]


# ---------------------------------------------------------------- the list, the export, the check tab (demo data)

@pytest.fixture
def demo(tmp_path):
    """The demo with Tom (media, wears a device, X DMs closed) left without any path: no way to contact him."""
    p = tmp_path / "demo.db"
    seed(p, quiet=True)
    conn = db.connect(p)
    tom = int(db.one(conn, "SELECT candidate_id FROM candidates WHERE handle='techwriter_test'")["candidate_id"])
    conn.execute("DELETE FROM paths WHERE candidate_id=?", (tom,))
    conn.commit()
    yield p, conn, tom
    conn.close()


def test_ranked_table_and_count_line_hide_people_with_no_way_to_contact(demo, monkeypatch):
    p, conn, tom = demo
    df = data.ranked_table(conn, 2, precise=True)
    t = df[df["candidate_id"] == tom].iloc[0]
    assert (t["contact"], bool(t["contactable"]), t["contact_routes"]) == (contacts.NONE_LABEL, False, [])
    assert t["bar_reason"] == t["contact_reason"] == "no way to contact found: X DMs closed, no warm path, no public route researched yet"
    rina = df[df["handle"] == "ringnerd_test"].iloc[0]
    assert rina["contact"] == "Intro via Marwan Refaat" and [r["label"] for r in rina["contact_routes"]][:2] == [
        "Intro via Marwan Refaat", "X DMs open"]
    lst = data.rank_list(df)
    assert tom not in set(lst["ranked"]["candidate_id"]) and len(lst["ranked"]) == 2
    assert lst["hidden"] == {"not US": 1, "below the bar": 4, data.NO_CONTACT_WORD: 1}
    assert list(data.table_view(lst["ranked"]).columns) == data.TABLE_COLUMNS and "contact" in data.TABLE_COLUMNS

    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("DB_PATH", str(p))
    at = AppTest.from_file(str(ROOT / "app" / "dashboard.py"), default_timeout=60)
    at.run()
    assert not at.exception
    assert any(c.value.startswith("**2 people who meet the bar** · hidden: 1 not US, 4 below the bar, 1 with no way to "
                                  "contact · ") for c in at.caption)
    assert list(at.dataframe[0].value["contact"]) == ["Intro via Marwan Refaat", "Intro via Adrian Aoun"]
    at = AppTest.from_file(str(ROOT / "app" / "dashboard.py"), default_timeout=60)
    at.session_state["tf_bar"] = False
    at.query_params["person"] = str(tom)
    at.run()
    assert not at.exception
    assert any("no way to contact found: X DMs closed" in w.value for w in at.warning)
    assert any("How to contact them, best first" in m.value for m in at.markdown)

    # research lands: Tom is back, with his best public route and every route in his panel
    d = _yaml(p.parent / "routes", "batch_1.yaml", TOM_YAML)
    monkeypatch.setattr(contacts, "ROUTES_DIR", d)
    df = data.ranked_table(conn, 2, precise=True)
    t = df[df["candidate_id"] == tom].iloc[0]
    assert t["contactable"] and t["contact"] == "Email: tom@gadgetdaily.example" and t["bar_reason"] == ""
    assert [r["label"] for r in data.person_detail(conn, 2, tom)["contact"]["routes"]] == [
        "Email: tom@gadgetdaily.example", "LinkedIn (weak)"]
    assert data.rank_list(df)["hidden"] == {"not US": 1, "below the bar": 4}


def test_export_carries_contact_and_drops_people_with_no_way_to_contact(demo, tmp_path, monkeypatch):
    p, conn, tom = demo
    res = export.run_export(conn, 2, out_dir=tmp_path / "out")
    items = json.loads((tmp_path / "out" / "wishlist.json").read_text())
    assert [i["handle"] for i in items] == ["ringnerd_test", "vc_test"] and res["wishlist"] == 2
    assert items[0]["contact"]["best"] == "Intro via Marwan Refaat"
    assert items[0]["contact"]["routes"][:2] == [
        {"type": "warm", "detail": "Intro via Marwan Refaat", "source_url": None},
        {"type": "x_dm", "detail": "X DMs open to everyone", "source_url": "https://x.com/ringnerd_test"}]
    with (tmp_path / "out" / "wishlist.csv").open(newline="", encoding="utf-8") as f:
        rows = {r["handle"]: r for r in csv.DictReader(f)}
    assert {"contact", "contactable"} <= set(export.CSV_COLUMNS)
    assert (rows["techwriter_test"]["contact"], rows["techwriter_test"]["contactable"], rows["techwriter_test"]["meets_bar"]) == (
        contacts.NONE_LABEL, "False", "False")
    assert rows["techwriter_test"]["bar_reason"].startswith("no way to contact found: X DMs closed, no warm path")
    assert rows["ringnerd_test"]["contact"] == "Intro via Marwan Refaat" and rows["ringnerd_test"]["contactable"] == "True"

    monkeypatch.setattr(contacts, "ROUTES_DIR", _yaml(tmp_path / "routes", "batch_1.yaml", TOM_YAML))
    export.run_export(conn, 2, out_dir=tmp_path / "out")
    items = json.loads((tmp_path / "out" / "wishlist.json").read_text())
    tom_item = next(i for i in items if i["handle"] == "techwriter_test")
    assert tom_item["contact"] == {"best": "Email: tom@gadgetdaily.example", "routes": [
        {"type": "email", "detail": "tom@gadgetdaily.example", "source_url": "https://gadgetdaily.example/tom"},
        {"type": "linkedin", "detail": "https://www.linkedin.com/in/tomwriter/", "source_url": "https://gadgetdaily.example/tom"}]}


def test_check_tab_says_how_to_contact_and_when_reach_fails(demo):
    from app import check_tab
    p, conn, tom = demo
    contact = check.contact_of(conn, tom, [])
    assert not contact["contactable"]
    person = check.with_bar({"candidate_id": tom, "category": "media", "wear_evidence": True, "bar_reason": ""}, [], contact)
    assert person["contactable"] is False and person["bar_reason"] == contact["contact_reason"]
    table = data.ranked_table(conn, 2, precise=True)
    rv = check.rank_view(table, {**table[table["candidate_id"] == tom].iloc[0].to_dict(), **person})
    assert rv["hidden_by"] == ["bar"] and not rv["in_list"]
    text = check_tab._contact_lines({"paths": [], "name": "Tom Writer"}, contact)
    assert text.startswith("**How to contact** · no way to contact found: X DMs closed") and "fail the brief's bar on reach" in text
    warm = [{"how": "via", "via": "Marwan Refaat", "you": "met · warm", "target": "replied to each other on X", "strength": 0.55}]
    ok = check.contact_of(conn, tom, warm)
    assert ok["best"] == "Intro via Marwan Refaat" and check.with_bar(person, [], ok)["bar_reason"] == ""
    assert check_tab._contact_lines({"paths": warm, "name": "Tom Writer"}, ok).startswith(
        "**How to contact** · Intro via Marwan Refaat · good")


def test_repo_research_files_parse():
    """config/contact_routes as committed: every file parses and every person has a verdict and a reason."""
    idx = contacts.load_routes(ROOT / "config" / "contact_routes")
    people = {id(e): e for e in idx.values()}.values()
    assert all(e["verdict"] in ("contactable", "not_contactable") for e in people)
    assert all(e["routes"] for e in people if e["verdict"] == "contactable")
    assert {r["type"] for e in people for r in e["routes"]} <= set(contacts.RESEARCHED_TYPES)   # every type has a label
    assert all(contacts.contact_for([], can_dm=0, researched=e)["contactable"] == (e["verdict"] == "contactable")
               for e in people)


def test_strip_reach_keeps_the_person_and_drops_dm_remarks():
    from ring_wishlist.contacts import strip_reach
    cases = {
        "New York tech analyst who tested 10 AI wearables; skeptical of the category and cannot be DMed.":
            "New York tech analyst who tested 10 AI wearables; skeptical of the category.",
        "Reviewer with 6.1M followers; only one topical post, and he doesn't follow Carlo, and DMs are closed.":
            "Reviewer with 6.1M followers; only one topical post.",
        "Analyst; caveats: paid Qualcomm travel, no voice or agent focus, not following Carlo, DMs closed.":
            "Analyst; caveats: paid Qualcomm travel, no voice or agent focus.",
        "Founder who praises voice products; no wearable evidence, can't be DMed, and does not follow Carlo.":
            "Founder who praises voice products; no wearable evidence.",
        "Angel who wears an Oura; no ring-specific posts, and hard to reach by DM.":
            "Angel who wears an Oura; no ring-specific posts.",
        "YouTuber who wears an Oura Ring; mainly Apple and audio focused, DMs closed.":
            "YouTuber who wears an Oura Ring; mainly Apple and audio focused.",
        "Editor who reviews wearables; he may not accept free devices, and DMs are closed.":
            "Editor who reviews wearables; he may not accept free devices.",
        "Plain summary with nothing about reach.": "Plain summary with nothing about reach.",
    }
    for raw, want in cases.items():
        assert strip_reach(raw) == want, raw
    assert strip_reach(None) == ""
