"""Paths: who in Carlo's network can reach each candidate. A real path means a real relationship, not a follow.

Kinds (best first, up to MAX_PATHS per candidate, written to `paths` with the export-shape object in `json`):
  direct    the candidate IS a map person: strength from warmth rank, meetings and recency of last_contact
            (faded when silent 90+ days). you_rel like "met · 3 meetings · 84 touches · last 12d ago".
  via       Carlo -> connector (map person with warmth > 0) -> candidate. The connector-candidate link is one of:
            a map intro edge, the same company (map vs map, or the connector's firm in the candidate's bio),
            an X conversation (2+ replies/mentions between the connector's resolved handle and the candidate).
            A mutual X follow between them is shown as "follow each other" and capped at MUTUAL_FOLLOW_CAP:
            it never justifies reach above 0.3 on its own. An observed conversation is worth as much as sharing a
            company (0.6, more with more posts and when both sides wrote): through a well-ranked connector it clears
            the follow cap, so a real relationship always outranks a follow.
  x_direct  Carlo and the candidate follow each other (x_follows in both directions), the candidate follows Carlo,
            replied to or mentioned him, or the map tags them x_follower. Carlo following them alone is not a path.
  open_dm   x_users.can_dm: the reachability floor.
  none      nothing known.
strength = how well Carlo can ask the connector (0..0.95) x how real the connector-candidate link is.
`warmth` in a path is the connector's warmth-rank quartile (3 = top) among map people with warmth > 0.
Labels (`you` = Carlo -> via or target, `target` = via -> target) are short phrases a person reads in a second.
The seed is found through resolve_handles.seed_user (CARLO_X_HANDLE as '@carloAI', 'carloAI' or an x.com URL).

CLI: python -m ring_wishlist.paths [--run N]
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import re
import sqlite3
from collections import Counter, defaultdict

from . import config, db
from .resolve_handles import GENERIC_COMPANY_WORDS, STOP_STEMS, company_in_text, norm_text, seed_user, tags_of
from .sources.base import norm_handle

log = logging.getLogger("ring_wishlist.paths")

MAX_PATHS = 4
FADE_DAYS = 90
MAX_RELATIONSHIP = 0.95
MUTUAL_FOLLOW_CAP = 0.3
STRENGTH_OPEN_DM = 0.15
STRENGTH_NONE = 0.05
KIND_ORDER = {"direct": 0, "via": 1, "x_direct": 2, "open_dm": 3, "none": 4}
# worth of a connector -> candidate link before multiplying by Carlo -> connector
LINK = {"intro": 0.85, "offered": 0.7, "introduced_by": 0.75, "same_company": 0.6, "same_firm_bio": 0.5,
        "conversation": 0.6, "mutual_follow": MUTUAL_FOLLOW_CAP}
CONVERSATION_MAX = 0.75           # more posts and a two-way exchange raise the conversation link up to this
# sharing one of these employers (or platforms) says nothing about knowing each other
BIG_EMPLOYERS = {"youtube", "google", "amazon", "apple", "meta", "facebook", "microsoft", "openai", "forbes", "freelance",
                 "independent", "self employed", "stealth", "x", "twitter", "linkedin", "tiktok", "instagram", "substack",
                 "medium", "tesla", "nvidia", "none", "n a"}
# a firm named right after these words is an investor, a tool or a publication, not an employer
NOT_EMPLOYMENT = re.compile(r"(?:backed|funded|invested|investors?|portfolio|by|using|featured|seen|on|via)\s*$")
# ... or right before these ('a16z backed', 'a16z-backed', 'YC portfolio', 'a16z speedrun alum'; not 'Acme, backed by YC',
# where Acme is still the employer)
NOT_EMPLOYMENT_AFTER = re.compile(r"^\s*(?:[a-z0-9]+\s+)?(?:backed(?!\s+by)|funded(?!\s+by)|portfolio|alum|alumni|alumnus|alumna)\b")
# ... or anywhere earlier in the same clause of the bio, which then lists investors, press or past employers ('as featured
# in Axios • The Information', 'backed by Sequoia, a16z and YC', 'Prev: editor @inputmag, correspondent @mashable',
# 'Ex-Microsoft, Rackspace, Fast Company', 'Boards of Ethos, Krea, Deel'), unless 'now' or 'currently' comes after it.
# QA runs 14 and 39 (docs/QA_TOP.md): these made past employers, press mentions, board seats and a16z's portfolio "same firm".
NOT_EMPLOYMENT_CLAUSE = re.compile(r"\b(?:backed by|funded by|investors|portfolio|featured|as seen|ex|former|formerly|prev|"
                                   r"previously|past|alum|alumni|alumnus|alumna|boards? of|board member|bod)\b"
                                   r"(?!.*\b(?:now|currently|today)\b)")
CLAUSE_SPLIT = re.compile(r"\s*(?:\||•|·|;|\n|\.\s|→|//|—|\s-\s)\s*")


# ---------------- time and warmth helpers ----------------

def parse_ts(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            pass
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def days_since(s: str | None, now: dt.datetime) -> int | None:
    d = parse_ts(s)
    return None if d is None else max(0, (now - d).days)


def warmth_ranks(people: list[dict]) -> dict[int, dict]:
    """For map people with warmth > 0: rank (1 = warmest), n, pct (1 = warmest .. 0 = coldest), bucket 0..3 (quartile)."""
    warm = sorted((p for p in people if (p.get("warmth") or 0) > 0), key=lambda p: (-p["warmth"], p["id"]))
    n = len(warm)
    out = {}
    for i, p in enumerate(warm):
        rank = i + 1
        out[p["id"]] = {"rank": rank, "n": n, "pct": 1.0 if n <= 1 else 1 - i / (n - 1), "bucket": 3 - ((rank - 1) * 4) // n}
    return out


def warmth_bucket(ranks: dict, person_id: int | None) -> int:
    return int(ranks.get(person_id, {}).get("bucket", 0)) if person_id is not None else 0


def relationship(p: dict, ranks: dict, now: dt.datetime, linkedin: dict | None = None) -> tuple[float, str, str]:
    """(strength 0..0.95, label, evidence) of Carlo's relationship with map person `p`."""
    r = ranks.get(p["id"])
    meetings = p.get("meetings") or 0
    touches = p.get("interactions") or 0
    intros = (p.get("intros_made") or 0) + (p.get("intros_got") or 0)
    d = days_since(p.get("last_contact"), now)
    s = 0.35 + 0.35 * r["pct"] if r else 0.2
    if meetings >= 3:
        s += 0.15
    elif meetings >= 1:
        s += 0.10
    if intros > 0:
        s += 0.05
    if d is None:
        s *= 0.8
    elif d <= 30:
        s += 0.10
    elif d <= FADE_DAYS:
        s += 0.05
    elif d <= 365:
        s *= 0.75
    else:
        s *= 0.6
    if linkedin:
        s += 0.03
    s = round(min(s, MAX_RELATIONSHIP), 3)

    parts = ["met" if meetings >= 1 else ("in touch" if touches > 0 else "no contact logged")]
    if meetings >= 1:
        parts.append(f"{meetings} meeting{'s' if meetings != 1 else ''}")
    if touches > 0:
        parts.append(f"{touches} touches")
    if d is not None:
        parts.append(f"last {d}d ago" + (" · faded" if d > FADE_DAYS else ""))
    label = " · ".join(parts)

    ev = [f"map #{p['id']} {p.get('sector') or '?'} at {p.get('company') or '?'}",
          f"warmth {float(p.get('warmth') or 0):.1f}" + (f" (rank {r['rank']}/{r['n']})" if r else " (cold)")]
    if p.get("channels"):
        ev.append(f"channels {p['channels']}")
    if intros:
        ev.append(f"intros made {p.get('intros_made') or 0} / got {p.get('intros_got') or 0}")
    if linkedin:
        ev.append(f"linkedin connected {linkedin.get('connected_on') or '?'}")
    return s, label, " · ".join(ev)


# ---------------- everything the computation needs, loaded once ----------------

class Context:
    def __init__(self, conn: sqlite3.Connection, seed_handle: str | None, now: dt.datetime):
        self.seed_handle = seed_handle
        self.now = now
        self.people = {p["id"]: p for p in db.rows(conn, "SELECT * FROM map_people")}
        self.ranks = warmth_ranks(list(self.people.values()))
        self.linkedin = {r["person_id"]: r for r in db.rows(conn, "SELECT * FROM map_linkedin")}
        self.handles = {r["person_id"]: r for r in db.rows(conn, "SELECT * FROM map_x_handles WHERE x_id IS NOT NULL")}
        self.edges_from: dict[int, list] = defaultdict(list)
        self.edges_to: dict[int, list] = defaultdict(list)
        for e in db.rows(conn, "SELECT from_id, to_id, source FROM map_edges"):
            self.edges_from[e["from_id"]].append((e["to_id"], e["source"]))
            self.edges_to[e["to_id"]].append((e["from_id"], e["source"]))
        self.fund_competitors: dict[str, set] = defaultdict(set)
        for f in db.rows(conn, "SELECT fund, competitor FROM map_competitor_funds"):
            self.fund_competitors[norm_text(f["fund"])].add(f["competitor"])
        self.x_users = {r["x_id"]: r for r in db.rows(conn, "SELECT * FROM x_users")}
        self.xid_by_handle = {r["handle"]: x for x, r in self.x_users.items()}
        self.follows = {(r["src_x_id"], r["dst_x_id"]) for r in db.rows(conn, "SELECT src_x_id, dst_x_id FROM x_follows")}
        self.seed = seed_user(conn, seed_handle)
        # conversations: unordered pair of x_ids -> [(author_x_id, post_id, 'reply'|'mention')]
        self.conv: dict[frozenset, list] = defaultdict(list)
        for post in db.rows(conn, "SELECT post_id, x_id, in_reply_to_x_id, mentions FROM x_posts "
                                  "WHERE in_reply_to_x_id IS NOT NULL OR (mentions IS NOT NULL AND mentions NOT IN ('', '[]'))"):
            others: dict[str, str] = {}
            for h in db.jloads(post["mentions"], []) or []:
                x = self.xid_by_handle.get(norm_handle(str(h)))
                if x:
                    others[x] = "mention"
            if post["in_reply_to_x_id"]:
                others[str(post["in_reply_to_x_id"])] = "reply"
            others.pop(str(post["x_id"]), None)
            for o, kind in others.items():
                self.conv[frozenset((str(post["x_id"]), o))].append((str(post["x_id"]), post["post_id"], kind))
        self.connectors = sorted((p for p in self.people.values() if (p.get("warmth") or 0) > 0), key=lambda p: -p["warmth"])
        self.companies = {pid: [c for c in (p.get("company"), (self.linkedin.get(pid) or {}).get("company")) if c]
                          for pid, p in self.people.items()}

    def mutual(self, a: str | None, b: str | None) -> bool:
        return bool(a and b and (a, b) in self.follows and (b, a) in self.follows)

    def fund_note(self, company: str | None) -> str:
        hits = {c for k, cs in self.fund_competitors.items() if company_in_text(k, company) or company_in_text(company, k) for c in cs}
        return f" (fund backs {', '.join(sorted(hits))})" if hits else ""


def _first(name: str | None) -> str:
    return (name or "").strip().split(" ")[0] or "they"


def _path(how, strength, you, via, via_id, warmth, target, evidence) -> dict:
    return {"how": how, "strength": round(float(strength), 3), "you": you, "via": via, "via_person_id": via_id,
            "warmth": int(warmth), "target": target, "evidence": evidence}


def firm_keys(company: str | None) -> set[str]:
    """Strict forms of a firm name for relationship links: the full name and its no-space form, per '/'-separated part,
    parentheticals dropped ('George Buhnici (YouTube)' -> {'george buhnici', 'georgebuhnici'}). No single-word stems
    ('Creator Ventures' never matches 'creator') and no big employers or platforms."""
    keys: set[str] = set()
    for part in re.split(r"\s*(?:/|,|&|\||;| and )\s*", re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", company or "")):
        name = norm_text(part)
        words = name.split()
        if not name or name in BIG_EMPLOYERS or (len(words) == 1 and name in STOP_STEMS | GENERIC_COMPANY_WORDS):
            continue
        if len(name) >= 4:
            keys.add(name)
        if len(words) > 1 and len("".join(words)) >= 6:
            keys.add("".join(words))
    return keys


def firm_in_text(company: str | None, text: str | None) -> str | None:
    """The firm key found as a whole word in `text` where it reads as an employer: not right after backed-by/using/via
    (NOT_EMPLOYMENT), not right before 'backed'/'portfolio'/'alum' (NOT_EMPLOYMENT_AFTER), and not after an investor, press,
    board or past-employer word earlier in the same clause (NOT_EMPLOYMENT_CLAUSE: 'as featured in', 'ex-', 'prev',
    'formerly'). Clauses are split on | • · ; '. ' → // and ' - '; a short clause with no @handle right after such a clause
    continues its list ('as featured in Axios • The Information • NYT • Techcrunch')."""
    keys = sorted(firm_keys(company), key=len, reverse=True)
    carry = False
    for clause in CLAUSE_SPLIT.split(text or ""):
        t = norm_text(clause)
        if not t:
            continue
        listed = carry and "@" not in clause and len(t.split()) <= 3
        for k in keys:
            for m in re.finditer(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])", t):
                before, after = t[:m.start()], t[m.end():]
                if (listed or NOT_EMPLOYMENT.search(before[-25:]) or NOT_EMPLOYMENT_AFTER.search(after)
                        or NOT_EMPLOYMENT_CLAUSE.search(before)):
                    continue
                return k
        carry = listed or bool(NOT_EMPLOYMENT_CLAUSE.search(t))
    return None


def firm_label(company: str) -> str:
    """'TechCrunch (Week in Voice AI newsletter)' -> 'TechCrunch' for labels."""
    return re.sub(r"\s*[\(\[][^)\]]*[\)\]]", "", company or "").strip() or company


def _same_company(ctx: Context, a_id: int, b_id: int) -> str | None:
    for ca in ctx.companies.get(a_id, []):
        for cb in ctx.companies.get(b_id, []):
            if firm_in_text(ca, cb) or firm_in_text(cb, ca):
                return ca
    return None


def connector_links(ctx: Context, c: dict, t_map: dict | None, t_xid: str | None, t_bio: str | None) -> list[tuple]:
    """All real links between connector `c` and the candidate: (kind, link_strength, target_label, evidence)."""
    links = []
    if t_map:
        made = [src for to, src in ctx.edges_from.get(c["id"], []) if to == t_map["id"]]
        if made:
            offered_only = all(s == "offered" for s in made)
            srcs = "+".join(sorted(set(made)))
            links.append(("offered" if offered_only else "intro",
                          LINK["offered"] if offered_only else LINK["intro"],
                          "offered to introduce them (map)" if offered_only else f"introduced them (map, {srcs})",
                          f"map edge {c['id']}->{t_map['id']} ({srcs})"))
        got = [src for frm, src in ctx.edges_to.get(c["id"], []) if frm == t_map["id"]]
        if got:
            srcs = "+".join(sorted(set(got)))
            links.append(("introduced_by", LINK["introduced_by"],
                          f"{_first(t_map['name'])} introduced {_first(c['name'])} to you (map, {srcs})",
                          f"map edge {t_map['id']}->{c['id']} ({srcs})"))
        if (co := _same_company(ctx, c["id"], t_map["id"])):
            links.append(("same_company", LINK["same_company"], f"same company ({firm_label(co)})" + ctx.fund_note(co),
                          f"map company match: {co}"))
    elif t_bio:
        for co in ctx.companies.get(c["id"], []):
            if (k := firm_in_text(co, t_bio)):
                links.append(("same_firm_bio", LINK["same_firm_bio"], f"same firm ({firm_label(co)}, per their bio)" + ctx.fund_note(co),
                              f"'{k}' in candidate bio matches connector company {co}"))
                break
    c_x = ctx.handles.get(c["id"])
    if c_x and t_xid and c_x["x_id"] != t_xid:
        conv = ctx.conv.get(frozenset((str(c_x["x_id"]), str(t_xid))), [])
        if len(conv) >= 2:
            n = len(conv)
            two_way = len({a for a, _, _ in conv}) > 1
            link_s = min(CONVERSATION_MAX, LINK["conversation"] + 0.05 * (n - 2) + (0.05 if two_way else 0.0))
            links.append(("conversation", link_s, f"X conversation · {n} posts",
                          f"{n} replies/mentions {'both ways ' if two_way else ''}between @{c_x['handle']} and the candidate "
                          f"(posts {', '.join(p for _, p, _ in conv[:5])})"))
        if ctx.mutual(c_x["x_id"], t_xid):
            links.append(("mutual_follow", LINK["mutual_follow"], "follow each other", f"@{c_x['handle']} and the candidate follow each other on X"))
    return links


def x_direct_path(ctx: Context, t_xid: str | None, t_tags: set[str], own_bucket: int) -> dict | None:
    signals = []  # (strength, label, evidence)
    if ctx.seed and t_xid and t_xid != ctx.seed["x_id"]:
        theirs = [(p, k) for a, p, k in ctx.conv.get(frozenset((str(ctx.seed["x_id"]), str(t_xid))), []) if a == str(t_xid)]
        if theirs:
            n = len(theirs)
            verb = "replied to you" if any(k == "reply" for _, k in theirs) else "mentioned you"
            signals.append((min(0.5, 0.4 + 0.05 * (n - 1)), f"{verb} on X · {n} post{'s' if n != 1 else ''}",
                            f"posts {', '.join(p for p, _ in theirs[:5])} reply to or mention @{ctx.seed['handle']}"))
        if ctx.mutual(ctx.seed["x_id"], t_xid):
            signals.append((0.35, "follow each other on X", f"mutual follow with @{ctx.seed['handle']} in x_follows"))
        elif (t_xid, ctx.seed["x_id"]) in ctx.follows:
            signals.append((0.3, "follows you on X", f"follows @{ctx.seed['handle']} in x_follows"))
    if "x_follower" in t_tags and not any(l.startswith("follow") for _, l, _ in signals):
        signals.append((0.3, "follows you on X (map tag x_follower)", "map tag x_follower"))
    if not signals:
        return None
    signals.sort(key=lambda s: -s[0])
    strength = min(0.55, signals[0][0] + (0.05 if len(signals) > 1 else 0))
    return _path("x_direct", strength, " · ".join(l for _, l, _ in signals), None, None, own_bucket, None,
                 "; ".join(e for _, _, e in signals))


def candidate_paths(ctx: Context, cand: dict) -> list[dict]:
    t_map = ctx.people.get(cand["map_person_id"]) if cand.get("map_person_id") else None
    t_xid = cand.get("x_id") or ((ctx.handles.get(t_map["id"]) or {}).get("x_id") if t_map else None)
    t_x = ctx.x_users.get(t_xid) if t_xid else None
    t_bio = (t_x or {}).get("bio")
    t_tags = tags_of(t_map["tags"]) if t_map else set()
    own_bucket = warmth_bucket(ctx.ranks, t_map["id"]) if t_map else 0
    paths: list[dict] = []

    if t_map:
        s, label, ev = relationship(t_map, ctx.ranks, ctx.now, ctx.linkedin.get(t_map["id"]))
        paths.append(_path("direct", s, label, None, None, own_bucket, None, ev))

    for c in ctx.connectors:
        if t_map and c["id"] == t_map["id"]:
            continue
        links = connector_links(ctx, c, t_map, t_xid, t_bio)
        if not links:
            continue
        links.sort(key=lambda l: -l[1])
        kind, link_s, target, _ = links[0]
        you_s, you_label, you_ev = relationship(c, ctx.ranks, ctx.now, ctx.linkedin.get(c["id"]))
        strength = round(you_s * link_s, 3)
        if kind == "mutual_follow":
            strength = min(strength, MUTUAL_FOLLOW_CAP)
        evidence = "; ".join(l[3] for l in links) + " · connector: " + you_ev
        paths.append(_path("via", strength, you_label, c["name"], c["id"], warmth_bucket(ctx.ranks, c["id"]), target, evidence))

    if (xd := x_direct_path(ctx, t_xid, t_tags, own_bucket)):
        paths.append(xd)
    if t_x and t_x.get("can_dm") == 1:
        paths.append(_path("open_dm", STRENGTH_OPEN_DM, "DMs open, no relationship", None, None, 0, None, "x_users.can_dm"))
    if not paths:
        paths.append(_path("none", STRENGTH_NONE, "no known path yet", None, None, 0, None,
                           "not in the map, no connector link, no mutual follow or reply, DMs closed or unknown"))
    paths.sort(key=lambda p: (-p["strength"], KIND_ORDER[p["how"]]))
    return paths[:MAX_PATHS]


def run_paths(conn: sqlite3.Connection, run_id: int, seed_handle: str | None = None, now: str | None = None) -> dict:
    now_dt = parse_ts(now) if now else dt.datetime.now(dt.timezone.utc)
    seed_handle = seed_handle or config.carlo_handle()
    ctx = Context(conn, seed_handle, now_dt)
    if not ctx.seed:
        log.warning("paths: seed @%s not in x_users (run the crawl stage); x_direct only from the map's x_follower tag", seed_handle)
    else:
        log.info("paths: seed @%s (x_id %s): %d follow edges loaded", ctx.seed["handle"], ctx.seed["x_id"], len(ctx.follows))
    cands = db.rows(conn, "SELECT * FROM candidates")
    best = Counter()
    total = 0
    real = 0
    for cand in cands:
        paths = candidate_paths(ctx, cand)
        conn.execute("DELETE FROM paths WHERE run_id=? AND candidate_id=?", (run_id, cand["candidate_id"]))
        for i, p in enumerate(paths, 1):
            conn.execute(
                "INSERT INTO paths (run_id, candidate_id, rank, how, strength, via_person_id, via_name, you_rel, target_rel, evidence, json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, cand["candidate_id"], i, p["how"], p["strength"], p["via_person_id"], p["via"], p["you"], p["target"],
                 p["evidence"], db.jdumps(p)))
        total += len(paths)
        best[paths[0]["how"]] += 1
        if paths[0]["how"] in ("direct", "via", "x_direct") and paths[0]["strength"] > MUTUAL_FOLLOW_CAP:
            real += 1
        log.debug("paths %s (%s): %s", cand.get("name"), cand.get("handle"), [(p["how"], p["strength"], p["via"]) for p in paths])
    conn.commit()
    out = {"candidates": len(cands), "paths": total, "best_by_how": dict(best), "with_real_path": real,
           "connectors": len(ctx.connectors), "seed_found": bool(ctx.seed)}
    log.info("paths done: %s", out)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Compute who in Carlo's network can reach each candidate")
    ap.add_argument("--run", type=int, default=None, help="run id to write into (default: a new 'paths' run)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    conn = db.connect()
    run_id = a.run or db.new_run(conn, "paths")
    print(run_paths(conn, run_id))
    if not a.run:
        db.finish_run(conn, run_id)


if __name__ == "__main__":
    main()
