"""Print everything the system knows about one person: `python -m ring_wishlist.dossier <handle|candidate_id> [--run N]`.
Used to review the top of the list by hand before Carlo acts on it: every claim has to point to a row here."""
from __future__ import annotations

import argparse
import json
import sys
import textwrap

from . import db
from .sources.base import norm_handle


def _wrap(s: str | None, width: int = 110, indent: str = "    ") -> str:
    return textwrap.fill((s or "").replace("\n", " "), width=width, initial_indent=indent, subsequent_indent=indent)


def find(conn, key: str) -> dict | None:
    if key.isdigit():
        return db.one(conn, "SELECT * FROM candidates WHERE candidate_id=?", (int(key),))
    return db.one(conn, "SELECT * FROM candidates WHERE handle=?", (norm_handle(key),))


def render(conn, cand: dict, run_id: int | None = None) -> str:
    cid = cand["candidate_id"]
    run_id = run_id or db.latest_run_id(conn, with_scores=True)
    u = db.one(conn, "SELECT * FROM x_users WHERE x_id=?", (cand["x_id"],)) if cand.get("x_id") else None
    out = [f"== {cand.get('name') or ''} @{cand.get('handle')}  (candidate {cid}, category {cand.get('category')}, "
           f"US {cand.get('us_based')}, handle {cand.get('handle_verified')})"]
    if u:
        out.append(f"   {u.get('followers') or 0:,} followers · {u.get('following') or 0:,} following · {u.get('posts_count') or 0:,} posts · "
                   f"created {str(u.get('created_at'))[:10]} · location {u.get('location')!r} · badge {u.get('verified_type')} · DMs {u.get('can_dm')}")
        out.append(_wrap(u.get("bio")))
    if cand.get("competitor_flag"):
        out.append(f"   !! competitor tie: {cand.get('competitor_detail')}")
    if cand.get("map_person_id"):
        m = db.one(conn, "SELECT * FROM map_people WHERE id=?", (cand["map_person_id"],))
        if m:
            out.append(f"   in Carlo's map: {m['name']} · {m.get('role')} · {m.get('company')} · warmth {m.get('warmth')} · "
                       f"{m.get('interactions')} touches · {m.get('meetings')} meetings · last {str(m.get('last_contact'))[:10]}")
    cl = db.one(conn, "SELECT json, run_id FROM classifications WHERE candidate_id=? ORDER BY run_id DESC LIMIT 1", (cid,))
    if cl:
        j = db.jloads(cl["json"], {})
        out.append(f"\n   summary: {j.get('summary')}")
        if j.get("unknowns"):
            out.append(f"   unknowns: {'; '.join(j['unknowns'])}")
    sc = db.one(conn, "SELECT * FROM scores WHERE run_id=? AND candidate_id=?", (run_id, cid)) if run_id else None
    if sc:
        out.append(f"\n   score {sc['total']:.2f} (rank {sc['rank']}) · accept {sc['p_accept']:.2f} · wear {sc['p_wear']:.2f} · "
                   f"love {sc['p_love']:.2f} · impact {sc['impact']:.2f} · reach {sc['reach']:.2f} · confidence {sc['confidence']:.2f}")
        flags = db.jloads(sc.get("flags"), [])
        if flags:
            out.append(f"   flags: {', '.join(flags)}")
        reasons = db.jloads(sc.get("reasons_json"), {})
        if isinstance(reasons, dict):
            for k, v in reasons.items():
                if v:
                    out.append(f"   - {k}: {v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)[:300]}")
    ev = db.rows(conn, "SELECT * FROM evidence WHERE candidate_id=? ORDER BY supports, date DESC", (cid,))
    if ev:
        out.append(f"\n   evidence ({len(ev)}):")
        for e in ev:
            sign = "+" if (e.get("polarity") or 1) > 0 else "-"
            out.append(f"   {sign} [{e['supports']}/{e['kind']}] {str(e.get('date'))[:10]} {e.get('url') or ''}")
            out.append(_wrap(e.get("text"), indent="       "))
    evs = db.rows(conn, "SELECT * FROM events WHERE candidate_id=? ORDER BY date DESC", (cid,))
    if evs:
        out.append("\n   events:")
        for e in evs:
            out.append(f"   * {e['kind']} {str(e.get('date'))[:10]} {e.get('url') or ''} {e.get('text') or ''}"[:220])
    ps = db.rows(conn, "SELECT * FROM paths WHERE run_id=? AND candidate_id=? ORDER BY rank", (run_id, cid)) if run_id else []
    if ps:
        out.append("\n   paths:")
        for p in ps:
            out.append(f"   {p['rank']}. {p['how']} ({p['strength']:.2f}) you -[{p.get('you_rel')}]-> {p.get('via_name') or ''} "
                       f"-[{p.get('target_rel') or ''}]-> {cand.get('handle')}")
    st = db.one(conn, "SELECT * FROM status WHERE candidate_id=?", (cid,))
    out.append(f"\n   status: {(st or {}).get('status', 'candidate')}")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ring_wishlist.dossier")
    ap.add_argument("who", nargs="+", help="handle(s) or candidate id(s)")
    ap.add_argument("--run", type=int)
    a = ap.parse_args(argv)
    conn = db.connect()
    for key in a.who:
        c = find(conn, key)
        print(render(conn, c, a.run) if c else f"no candidate {key!r}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
