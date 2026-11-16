"""One-off repair (kept for the record): early follower rows were stored with X ids that lost precision in transit
(JSON numbers above 2^53, e.g. 929930489032355840 -> ...800). The exact id is in each raw row's `id_str`.
Rewrites every table that references the truncated id, then drops the stale x_users row. Idempotent."""
from __future__ import annotations

import json
import sys

from . import db

TABLES = [("candidates", "x_id"), ("x_follows", "src_x_id"), ("x_follows", "dst_x_id"), ("x_posts", "x_id"),
          ("x_posts", "in_reply_to_x_id"), ("map_x_handles", "x_id")]


def run(conn) -> dict:
    pairs = {}
    for x_id, raw in conn.execute("SELECT x_id, raw_json FROM x_users WHERE raw_json IS NOT NULL").fetchall():
        try:
            o = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        exact = o.get("id_str") or (str(o["id"]) if isinstance(o.get("id"), str) else None)
        num = o.get("id")
        if exact and isinstance(num, (int, float)) and str(int(num)) != exact:
            pairs[str(int(num))] = exact
    fixed = 0
    for bad, good in pairs.items():
        if bad == good:
            continue
        for table, col in TABLES:
            try:
                cur = conn.execute(f"UPDATE OR IGNORE {table} SET {col}=? WHERE {col}=?", (good, bad))
                fixed += cur.rowcount
                conn.execute(f"DELETE FROM {table} WHERE {col}=?", (bad,))   # rows that collided with an exact copy
            except Exception as e:  # noqa: BLE001
                print("skip", table, col, e)
        # candidates: a truncated row and an exact row may both exist; keep the exact one
        conn.execute("DELETE FROM x_users WHERE x_id=?", (bad,))
    conn.commit()
    return {"truncated_ids": len(pairs), "references_fixed": fixed}


if __name__ == "__main__":
    print(run(db.connect()))
    sys.exit(0)
