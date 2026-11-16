"""Stage: verify. Every candidate headed for the wishlist is looked up on the official X API and compared with what
the crawl saw. Same numeric id = same account (handles get renamed and squatted; ids do not). Combined with the
account signals and the classifier's assessment, this sets candidates.handle_verified:

  verified     official lookup confirms the id AND (X verified badge or account older than 2 years with 2k+ followers)
               AND nothing parody-like in name or bio AND the classifier did not call it suspicious
  likely_real  official lookup confirms the id, but the stronger signals are missing
  suspicious   parody/fan/fake words, id mismatch, or the classifier's own verdict
  unknown      the official lookup found nothing (renamed, suspended, protected) or was not run

Cost: $0.010 per user on the X API, batched 100 per request. One evidence row per candidate records the check."""
from __future__ import annotations

import datetime as dt
import logging
import re
import sqlite3

from . import costs, db
from .sources.base import norm_handle

log = logging.getLogger("ring_wishlist.verify")
RECHECK_DAYS = 7   # a handle confirmed on the official API this recently is not bought again: x_users' own cache is
                   # lost once a monid scrape re-saves the profile, so the authenticity rows are the record
PARODY = re.compile(r"\b(parody|fan ?account|fanpage|not affiliated|satire|impersonat|fake|unofficial|commentary account)\b", re.I)


def _age_years(created_at: str | None) -> float | None:
    if not created_at:
        return None
    try:
        d = dt.datetime.strptime(created_at[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None
    return (dt.datetime.now(dt.timezone.utc) - d).days / 365.25


def pick_candidates(conn: sqlite3.Connection, top: int) -> list[dict]:
    """Classified candidates in a real category and not rejected by hand, best first (latest score, else followers)."""
    return db.rows(conn, """
        SELECT c.candidate_id, c.x_id, c.handle, c.handle_verified, c.name,
               (SELECT total FROM scores s WHERE s.candidate_id=c.candidate_id ORDER BY run_id DESC LIMIT 1) AS last_total,
               u.followers
        FROM candidates c LEFT JOIN x_users u ON u.x_id=c.x_id
        WHERE c.handle IS NOT NULL AND c.category IS NOT NULL AND c.category != 'other'
          AND c.candidate_id NOT IN (SELECT candidate_id FROM status WHERE status='rejected')
        ORDER BY COALESCE(last_total, -1) DESC, COALESCE(u.followers, 0) DESC
        LIMIT ?""", (top,))


def recently_checked(conn: sqlite3.Connection, source_name: str, days: int = RECHECK_DAYS) -> set[int]:
    """Candidates whose handle this source confirmed within `days` (their authenticity evidence row)."""
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {r["candidate_id"] for r in db.rows(conn, """
        SELECT DISTINCT candidate_id FROM evidence
        WHERE kind='profile_signal' AND supports='authenticity' AND source=? AND fetched_at >= ?""", (source_name, cutoff))}


def run_verify(conn: sqlite3.Connection, run_id: int, source, top: int = 100) -> dict:
    picked = pick_candidates(conn, top)
    fresh = recently_checked(conn, source.name)
    cands = [c for c in picked if c["candidate_id"] not in fresh]   # the top N stays the top N; only the lookups shrink
    stats = {"checked": 0, "verified": 0, "likely_real": 0, "suspicious": 0, "unknown": 0, "id_mismatch": 0,
             "skipped_recent": len(picked) - len(cands)}
    if not cands:
        log.info("verify: %s", stats)
        return stats
    handles = [norm_handle(c["handle"]) for c in cands]
    print(f"[cost] {source.name} verify: {len(handles)} handles")
    official = {u.handle: u for u in source.get_users_by_handles(handles)}
    now = db.utcnow()
    for c in cands:
        h = norm_handle(c["handle"])
        u = official.get(h)
        stats["checked"] += 1
        text = f"{(u.name if u else c['name']) or ''} {(u.bio if u else '') or ''}"
        llm_verdict = c["handle_verified"]
        if u is None:
            verdict, why = "unknown", "official X lookup found no account with this handle"
        elif c["x_id"] and str(u.x_id) != str(c["x_id"]):
            verdict, why = "suspicious", f"handle now belongs to a different account (id {u.x_id}, crawl saw {c['x_id']})"
            stats["id_mismatch"] += 1
        elif PARODY.search(text) or llm_verdict == "suspicious":
            verdict, why = "suspicious", "parody or fan signals in name/bio, or the classifier flagged it"
        else:
            age = _age_years(u.created_at)
            badge = bool(u.verified) and (u.verified_type or "none") != "none"
            strong = badge or ((age or 0) >= 2 and (u.followers or 0) >= 2000)
            verdict = "verified" if strong else "likely_real"
            why = (f"official X lookup confirms id {u.x_id}; badge={u.verified_type or 'none'}; "
                   f"account age {age:.1f}y; {u.followers or 0:,} followers" if age is not None else
                   f"official X lookup confirms id {u.x_id}; badge={u.verified_type or 'none'}")
        stats[verdict] += 1
        conn.execute("UPDATE candidates SET handle_verified=?, handle_verification_method=? WHERE candidate_id=?",
                     (verdict, f"x_api id check + signals{' + llm' if llm_verdict else ''}", c["candidate_id"]))
        conn.execute("""INSERT OR IGNORE INTO evidence (candidate_id, kind, supports, polarity, weight, url, date, text, source, fetched_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?)""",
                     (c["candidate_id"], "profile_signal", "authenticity", -1 if verdict == "suspicious" else 1,
                      0.8 if verdict == "verified" else 0.5, f"https://x.com/{h}", now[:10], f"handle check: {verdict}. {why}",
                      source.name, now))
    conn.commit()
    stats["cost_usd"] = costs.spent(conn, source.name, run_id)
    log.info("verify: %s", stats)
    return stats
