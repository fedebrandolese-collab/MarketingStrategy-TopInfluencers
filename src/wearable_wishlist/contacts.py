"""Contact routes: how the operator can get a message to each person. The reach half of the brief's bar (SCORING.md "The bar").

The brief: "A name nobody can reach, or someone who has never worn a device or used a voice tool, does not belong on
the list however big they are." the operator may approach people cold, but everyone on the list needs at least one real way to
be contacted. Routes, strongest first:

  warm        a real relationship path in the latest paths run: the operator knows them (paths.how 'direct', "the operator knows
              them") or a connector can introduce them ('via', "Intro via Jane Doe").
  dm          X DMs open (x_users.can_dm = 1), or they follow the operator on X (an x_direct path that says so: X lets anyone
              they follow message them). "X DMs open", "X DM: follows the operator".
  researched  public routes from config/contact_routes/*.yaml, in the research's order, each with the page it was found
              on: email, contact_form, press_office, agent, newsletter_reply, other_social, linkedin.
  bio email   an email address in their X bio (source: the X profile).
  weak        researched routes marked weak: the note says so, LinkedIn, or a sponsors-only inbox. Labelled "(weak)".
Routes whose note says "Unconfirmed" come last, labelled "(unconfirmed)": never the best route, never enough alone.

A yaml verdict `not_contactable` (the research looked and found no real way in) makes a person not contactable unless
there is a warm path or open DMs; the routes it lists and a bio email then do not count.

The yaml shape: people: [{handle, name, verdict: contactable|not_contactable, routes: [{type, detail, source_url,
note}], reason}]. A missing directory or file means no researched routes; a broken file is logged and skipped. People
are matched by X handle (case and @ ignored), else by name. A person in several files gets every route found.

contact_for() is pure (paths in either shape: `paths` rows or paths.candidate_paths objects); contacts_for(conn) gives
every candidate's routes from the database in two queries.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import config, db

log = logging.getLogger("wearable_wishlist.contacts")

ROUTES_DIR = config.CONFIG_DIR / "contact_routes"
RESEARCHED_TYPES = ("email", "contact_form", "press_office", "agent", "newsletter_reply", "other_social", "linkedin")  # labelled
NONE_LABEL = "No way to contact found"
NO_CONTACT = "no way to contact found"
LABEL_MAX = 60
EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")
FOLLOWS_SEED_RE = re.compile(r"\bfollows? (?:you|each other)\b")
_X_URL_RE = re.compile(r"^(?:https?://)?(?:www\.|mobile\.)?(?:x|twitter)\.com/@?([A-Za-z0-9_]{1,15})", re.I)


# ---------------------------------------------------------------- small helpers

_REACH_CLAUSE = re.compile(
    r"(?:\s*[,;])?\s*(?:and\s+)?(?:(?:he|she|they)\s+)?"
    r"(?:(?:doesn['’]t|does not|don['’]t|do not)\s+follow the operator|not following the operator|"
    r"(?:can['’]t|cannot|can not)\s+be\s+DM['’]?ed|DMs? (?:are |is )?(?:closed|open)|hard to reach by DM|not reachable by DM)",
    re.I)


def strip_reach(text: Any) -> str:
    """A classifier summary without its remarks on DMs or following the operator ("…, and DMs are closed", "cannot be DMed"):
    how to contact someone is its own column, from warm paths, DMs and researched routes, so the why line stays about
    the person. The stored classification keeps the full text."""
    t = _REACH_CLAUSE.sub("", str(text or ""))
    t = re.sub(r"\s+([.;,])", r"\1", t)
    t = re.sub(r"[,;]\s*(?=\.|$)", "", t)
    t = re.sub(r"\s+and\s*(?=\.|$)", "", t)
    return " ".join(t.split())


def norm_handle(h: Any) -> str | None:
    """'@JoannaStern', 'joannastern' or an x.com link -> 'joannastern'."""
    s = str(h or "").strip()
    if not s:
        return None
    m = _X_URL_RE.match(s)
    return (m.group(1) if m else s.lstrip("@")).lower() or None


def _norm_name(n: Any) -> str | None:
    s = " ".join(str(n or "").casefold().split())
    return s or None


def _clip(text: Any, n: int = LABEL_MAX) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _host(url: Any) -> str | None:
    m = re.match(r"^(?:https?://)?(?:www\.)?([^/\s?#]+)", str(url or "").strip())
    return m.group(1).lower() if m and "." in m.group(1) else None


def _x_url(handle: str | None) -> str | None:
    return f"https://x.com/{handle}" if handle else None


def bio_emails(bio: Any) -> list[str]:
    """Email addresses written in a bio, in order, without repeats."""
    out: list[str] = []
    for m in EMAIL_RE.finditer(str(bio or "")):
        e = m.group(0).rstrip(".")
        if e.lower() not in {x.lower() for x in out}:
            out.append(e)
    return out


# ---------------------------------------------------------------- researched routes: config/contact_routes/*.yaml

def _route(raw: Any) -> dict | None:
    if not isinstance(raw, Mapping):
        return None
    typ = re.sub(r"[\s-]+", "_", str(raw.get("type") or "").strip().lower())
    detail = " ".join(str(raw.get("detail") or "").split())
    url = str(raw.get("source_url") or "").strip() or None
    if not typ or not (detail or url):
        return None
    return {"type": typ, "detail": detail, "source_url": url, "note": " ".join(str(raw.get("note") or "").split()) or None}


def _entry(raw: Any, file: str) -> dict | None:
    if not isinstance(raw, Mapping):
        return None
    handle, name = norm_handle(raw.get("handle")), _norm_name(raw.get("name"))
    if not handle and not name:
        return None
    routes = [r for r in (_route(x) for x in (raw.get("routes") or [])) if r]
    verdict = str(raw.get("verdict") or "").strip().lower().replace(" ", "_").replace("-", "_")
    if verdict not in ("contactable", "not_contactable"):
        verdict = "contactable" if routes else "not_contactable"
    return {"handle": handle, "name": name, "verdict": verdict, "routes": routes,
            "reason": " ".join(str(raw.get("reason") or "").split()) or None, "files": [file]}


def _merge(a: dict, b: dict) -> dict:
    """One person in two files: every route (no repeats), contactable when either says so, the later reason."""
    seen = {(r["type"], r["detail"].lower(), r["source_url"]) for r in a["routes"]}
    routes = a["routes"] + [r for r in b["routes"] if (r["type"], r["detail"].lower(), r["source_url"]) not in seen]
    verdict = "contactable" if "contactable" in (a["verdict"], b["verdict"]) else "not_contactable"
    return {**a, "handle": a["handle"] or b["handle"], "name": a["name"] or b["name"], "verdict": verdict,
            "routes": routes, "reason": b["reason"] or a["reason"], "files": a["files"] + b["files"]}


_CACHE: dict[str, tuple[tuple, dict]] = {}


def load_routes(directory: Path | str | None = None) -> dict[str, dict]:
    """{'h:<handle>' or 'n:<name>': entry} from every *.yaml / *.yml in the directory (default config/contact_routes).
    Missing directory or files: {}. A file that does not parse, or has no `people:` list, is logged and skipped.
    Re-read only when a file changes."""
    import yaml
    d = Path(directory) if directory else ROUTES_DIR
    files = sorted([*d.glob("*.yaml"), *d.glob("*.yml")]) if d.is_dir() else []
    sig = tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in files)
    hit = _CACHE.get(str(d))
    if hit and hit[0] == sig:
        return hit[1]
    people: list[dict] = []
    for p in files:
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8"))
            items = raw.get("people") if isinstance(raw, Mapping) else raw
            if not isinstance(items, list):
                raise ValueError("no `people:` list")
        except Exception as e:  # noqa: BLE001 - one broken batch must not hide the others
            log.warning("contact routes: skipping %s (%s)", p.name, e)
            continue
        people += [e for e in (_entry(x, p.name) for x in items) if e]
    index: dict[str, dict] = {}
    for e in people:
        keys = [k for k in (f"h:{e['handle']}" if e["handle"] else None, f"n:{e['name']}" if e["name"] else None) if k]
        prev = next((index[k] for k in keys if k in index), None)
        merged = _merge(prev, e) if prev else e
        for k in keys + ([f"h:{merged['handle']}"] if merged["handle"] else []) + ([f"n:{merged['name']}"] if merged["name"] else []):
            index[k] = merged
    _CACHE[str(d)] = (sig, index)
    return index


def researched_for(index: Mapping[str, dict] | None, handle: Any = None, name: Any = None) -> dict | None:
    """The research entry for a person: by X handle, else by name."""
    if not index:
        return None
    h, n = norm_handle(handle), _norm_name(name)
    return (index.get(f"h:{h}") if h else None) or (index.get(f"n:{n}") if n else None)


# ---------------------------------------------------------------- routes of one person (pure)

def _short_url(u: Any) -> str:
    return re.sub(r"^(?:https?://)?(?:www\.)?", "", str(u or "").strip()).rstrip("/")


def _is_url(s: Any) -> bool:
    return bool(re.match(r"^(?:https?://|www\.)\S+$", str(s or "").strip()))


def is_weak(r: Mapping[str, Any]) -> bool:
    """A weak route: the research says so, LinkedIn (a message to a stranger), or a sponsors-only inbox or form."""
    text = f"{r.get('detail') or ''} {r.get('note') or ''}"
    return r.get("type") == "linkedin" or bool(re.search(r"\bweak\b|\bsponsor", text, re.I))


def is_unconfirmed(r: Mapping[str, Any]) -> bool:
    """The research could not confirm the route is theirs: listed last, never the best route, never enough alone."""
    return bool(re.search(r"\bunconfirmed\b", str(r.get("note") or ""), re.I))


def researched_label(r: Mapping[str, Any]) -> str:
    """A short label saying how: 'Email: a@b.com', 'Contact form on joannastern.com', 'Press email on bryanjohnson.com/c',
    'Business email on the YouTube About page' (an address shown only on that page), 'Agency: UTA Speakers', 'LinkedIn'."""
    t, d, src, note = r.get("type"), r.get("detail") or "", r.get("source_url"), str(r.get("note") or "")
    addr = (EMAIL_RE.findall(d) or [None])[0]
    if t == "email":
        if addr:
            return f"Email: {addr}"
        what = "Business email" if "business" in d.lower() else "Email"
        where = ("the YouTube About page" if re.search(r"youtube\.com/.*/about", str(src or ""), re.I)
                 else _short_url(src) if src else None)
        return f"{what} on {where}" if where else what
    if t == "contact_form":
        host = _host(d) or _host(src)
        return f"Contact form on {host}" if host else "Contact form"
    if t == "press_office":
        if addr:
            return f"Press email: {addr}"
        if _is_url(d):
            return f"{'Press email' if 'email' in note.lower() else 'Press office'} on {_short_url(d)}"
        return f"Press office: {d}" if d else "Press office"
    if t == "agent":
        name = re.sub(r",?\s*https?://\S+", "", d).strip(" ,") or d
        return f"Agency: {name}" if name else "Agency"
    if t == "newsletter_reply":
        return f"Newsletter reply: {_short_url(d) if _is_url(d) else d}" if d else "Reply to their newsletter"
    if t == "other_social":
        return (_short_url(d) if _is_url(d) else d) or "Other social"
    if t == "linkedin":
        return "LinkedIn"
    return f"{str(t).replace('_', ' ').capitalize()}: {d}" if d else str(t).replace("_", " ").capitalize()


def route_label(r: Mapping[str, Any]) -> str:
    """researched_label, clipped, with '(weak)' or '(unconfirmed)' after it."""
    tag = " (unconfirmed)" if is_unconfirmed(r) else " (weak)" if is_weak(r) else ""
    return _clip(researched_label(r), LABEL_MAX - len(tag)) + tag


def _p(p: Mapping[str, Any], *keys: str) -> Any:
    return next((p.get(k) for k in keys if p.get(k) not in (None, "")), None)


def _dm_state(can_dm: Any) -> int | None:
    try:
        return None if can_dm is None or can_dm != can_dm else int(can_dm)
    except (TypeError, ValueError):
        return None


def contact_for(paths: Iterable[Mapping[str, Any]] | None = None, *, can_dm: Any = None, bio: Any = None,
                handle: Any = None, researched: Mapping[str, Any] | None = None) -> dict:
    """{routes (best first), best (a short label), contactable, contact_reason ('' when contactable)} for one person.
    paths: `paths` rows (how, via_name, you_rel, target_rel) or path objects (how, via, you, target), best first.
    Order: warm paths, X DMs, researched routes, an email in the X bio, then weak researched routes, then unconfirmed
    ones (which never make a person contactable). Each route: kind, type, label, detail, source_url, note, weak."""
    handle = norm_handle(handle)
    x_url = _x_url(handle)
    warm: list[dict] = []
    dm: list[dict] = []
    for p in paths or []:
        how = p.get("how")
        if how == "direct":
            warm.append({"kind": "warm", "type": "warm", "label": "the operator knows them", "detail": "the operator knows them",
                         "source_url": None, "note": _p(p, "you_rel", "you"), "weak": False})
        elif how == "via":
            via = _p(p, "via_name", "via") or "a contact"
            rel = _p(p, "target_rel", "target")
            warm.append({"kind": "warm", "type": "warm", "label": _clip(f"Intro via {via}"), "detail": f"Intro via {via}",
                         "source_url": None, "note": f"{via} → them: {rel}" if rel else None, "weak": False})
        elif how == "x_direct" and FOLLOWS_SEED_RE.search(str(_p(p, "you_rel", "you") or _p(p, "target_rel", "target") or "")):
            dm.append({"kind": "dm", "type": "x_dm", "label": "X DM: follows the operator", "detail": "follows the operator on X",
                       "source_url": x_url, "note": "X lets the operator message anyone who follows him", "weak": False})
    dms = _dm_state(can_dm)
    if dms == 1:
        dm.insert(0, {"kind": "dm", "type": "x_dm", "label": "X DMs open", "detail": "X DMs open to everyone",
                      "source_url": x_url, "note": None, "weak": False})
    res = researched or {}
    open_research = res.get("verdict") != "not_contactable"
    found = [{"kind": "researched", "type": r["type"], "label": route_label(r), "detail": r.get("detail") or "",
              "source_url": r.get("source_url"), "note": r.get("note"), "weak": is_weak(r),
              "unconfirmed": is_unconfirmed(r)} for r in (res.get("routes") or [])] if open_research else []
    known = {a.lower() for r in found for a in EMAIL_RE.findall(r["detail"])}
    bio_routes = [{"kind": "bio_email", "type": "email", "label": _clip(f"Email in X bio: {e}"), "detail": e,
                   "source_url": x_url, "note": "written in their X bio", "weak": False}
                  for e in bio_emails(bio) if e.lower() not in known] if open_research else []
    seen: set[str] = set()
    warm = [r for r in warm if not (r["label"] in seen or seen.add(r["label"]))]
    routes = (warm + dm[:1] + [r for r in found if not r["weak"] and not r["unconfirmed"]] + bio_routes
              + [r for r in found if r["weak"] and not r["unconfirmed"]] + [r for r in found if r["unconfirmed"]])
    if any(not r.get("unconfirmed") for r in routes):
        return {"routes": routes, "best": _clip(routes[0]["label"]), "contactable": True, "contact_reason": ""}
    dm_text = "X DMs closed" if dms == 0 else "X DMs unknown"
    if routes:
        research = "only unconfirmed routes found"
    elif not res:
        research = "no public route researched yet"
    elif res.get("verdict") == "not_contactable":
        research = res.get("reason") or "research found no public route"
    else:
        research = res.get("reason") or "research listed no route"
    research = re.sub(r"^(?:\s*(?:x dms (?:are )?(?:closed|unknown)|no warm (?:path|intro))\s*[,;:.]\s*)+", "", research,
                      flags=re.I).strip().rstrip(".") or "no public route found"
    return {"routes": routes, "best": NONE_LABEL, "contactable": False,
            "contact_reason": f"{NO_CONTACT}: {dm_text}, no warm path, {research}"}


def export_routes(contact: Mapping[str, Any]) -> list[dict]:
    """wishlist.json's contact.routes: [{type, detail, source_url}]."""
    return [{"type": r["type"], "detail": r.get("detail") or r.get("label"), "source_url": r.get("source_url")}
            for r in contact.get("routes") or []]


# ---------------------------------------------------------------- every candidate, from the database

def latest_paths(conn: sqlite3.Connection, run_id: int | None = None) -> dict[int, list[dict]]:
    """Paths per candidate, best first: this run's when it has rows for them, else the latest run's that does."""
    if run_id is None:
        run_id = -1
    rows = db.rows(conn, """
        WITH pick AS (
            SELECT candidate_id, COALESCE(MAX(CASE WHEN run_id = ? THEN run_id END), MAX(run_id)) AS rid
            FROM paths GROUP BY candidate_id)
        SELECT p.candidate_id, p.rank, p.how, p.strength, p.via_name, p.you_rel, p.target_rel
        FROM paths p JOIN pick ON pick.candidate_id = p.candidate_id AND p.run_id = pick.rid
        ORDER BY p.candidate_id, COALESCE(p.rank, 99), p.strength DESC""", (run_id,))
    out: dict[int, list[dict]] = {}
    for r in rows:
        out.setdefault(int(r["candidate_id"]), []).append(r)
    return out


def contacts_for(conn: sqlite3.Connection, run_id: int | None = None, *,
                 paths_by_cid: Mapping[int, list[dict]] | None = None,
                 index: Mapping[str, dict] | None = None) -> dict[int, dict]:
    """{candidate_id: contact_for(...)} for every candidate. paths_by_cid: already loaded paths (else latest_paths);
    index: already loaded research (else load_routes())."""
    paths_by_cid = latest_paths(conn, run_id) if paths_by_cid is None else paths_by_cid
    index = load_routes() if index is None else index
    users = db.rows(conn, "SELECT c.candidate_id, c.handle, c.name, u.can_dm, u.bio FROM candidates c "
                          "LEFT JOIN x_users u ON u.x_id = c.x_id")
    return {int(u["candidate_id"]): contact_for(paths_by_cid.get(int(u["candidate_id"])), can_dm=u["can_dm"], bio=u["bio"],
                                                handle=u["handle"], researched=researched_for(index, u["handle"], u["name"]))
            for u in users}
