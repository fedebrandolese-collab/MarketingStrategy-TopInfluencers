-- wearable_wishlist wishlist: SQLite schema. All timestamps ISO-8601 UTC ("2026-09-29T10:00:00Z").
-- Booleans: 1 / 0 / NULL (NULL = unknown, never guessed).
-- Handles are stored lowercase without "@". x_id is the X numeric id as TEXT.

CREATE TABLE IF NOT EXISTS runs (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  stage TEXT,                 -- 'all' or a single stage name
  notes TEXT,
  cost_usd REAL DEFAULT 0
);

-- ---------- the operator's network map (read-only mirror of network.ri.fo) ----------
CREATE TABLE IF NOT EXISTS map_people (
  id INTEGER PRIMARY KEY,     -- id from the map
  group_name TEXT NOT NULL,   -- 'capital' | 'press'
  name TEXT NOT NULL,
  company TEXT, role TEXT, sector TEXT, tags TEXT,   -- tags: comma separated as in the map
  warmth REAL, intros_made INTEGER, intros_got INTEGER, interactions INTEGER,
  sent INTEGER, received INTEGER, meetings INTEGER, channels TEXT,
  first_contact TEXT, last_contact TEXT,
  imported_at TEXT
);
CREATE TABLE IF NOT EXISTS map_edges (
  from_id INTEGER NOT NULL, to_id INTEGER NOT NULL, source TEXT, group_name TEXT,
  PRIMARY KEY (from_id, to_id, source, group_name)
);
CREATE TABLE IF NOT EXISTS map_competitor_funds (
  fund TEXT NOT NULL, competitor TEXT NOT NULL, round TEXT,
  PRIMARY KEY (fund, competitor)
);
CREATE TABLE IF NOT EXISTS map_linkedin (
  person_id INTEGER PRIMARY KEY,
  company TEXT, position TEXT, connected_on TEXT, profile_url TEXT
);
CREATE TABLE IF NOT EXISTS map_monthly (
  group_name TEXT, month TEXT, source TEXT, n INTEGER,
  PRIMARY KEY (group_name, month, source)
);
-- X handle resolved for a map person (the operator's contacts), so paths can use the X side.
CREATE TABLE IF NOT EXISTS map_x_handles (
  person_id INTEGER PRIMARY KEY,
  handle TEXT, x_id TEXT,
  confidence REAL,            -- 0..1
  method TEXT,                -- 'user_search+llm' | 'manual' | 'tag' | ...
  resolved_at TEXT
);

-- ---------- X data (from x_api or monid adapters; source column says which) ----------
CREATE TABLE IF NOT EXISTS x_users (
  x_id TEXT PRIMARY KEY,
  handle TEXT NOT NULL,
  name TEXT, bio TEXT, location TEXT, url TEXT,
  verified INTEGER, verified_type TEXT, created_at TEXT,
  followers INTEGER, following INTEGER, posts_count INTEGER,
  can_dm INTEGER, profile_image TEXT,
  raw_json TEXT, source TEXT, fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_x_users_handle ON x_users(handle);
CREATE TABLE IF NOT EXISTS x_follows (
  src_x_id TEXT NOT NULL, dst_x_id TEXT NOT NULL,   -- src follows dst
  source TEXT, fetched_at TEXT,
  PRIMARY KEY (src_x_id, dst_x_id)
);
CREATE TABLE IF NOT EXISTS x_posts (
  post_id TEXT PRIMARY KEY,
  x_id TEXT NOT NULL, handle TEXT,
  created_at TEXT, text TEXT,
  likes INTEGER DEFAULT 0, reposts INTEGER DEFAULT 0, replies INTEGER DEFAULT 0, quotes INTEGER DEFAULT 0, views INTEGER,
  url TEXT, in_reply_to_x_id TEXT, mentions TEXT,   -- mentions: JSON list of handles
  query TEXT,                                       -- the discovery query that surfaced it, if any
  raw_json TEXT, source TEXT, fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_x_posts_x_id ON x_posts(x_id);

-- ---------- Candidates (people, not posts) ----------
CREATE TABLE IF NOT EXISTS candidates (
  candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
  x_id TEXT UNIQUE,           -- NULL only for people without a known X account (e.g. from the map)
  handle TEXT,
  name TEXT,
  category TEXT,              -- 'x_voice' | 'media' | 'vc' | 'founder_angel' | 'other' | NULL
  us_based INTEGER,           -- 1/0/NULL
  map_person_id INTEGER,      -- link to map_people when the candidate IS in the operator's map
  linkedin_url TEXT,
  handle_verified TEXT,       -- 'verified' | 'likely_real' | 'suspicious' | 'unknown'
  handle_verification_method TEXT,
  competitor_flag INTEGER DEFAULT 0,
  competitor_detail TEXT,
  discovered_via TEXT,        -- 'query:<q>' | 'following:<handle>' | 'map' | 'seed' | 'manual'
  first_seen TEXT, last_seen TEXT,
  notes TEXT
);
CREATE INDEX IF NOT EXISTS ix_candidates_handle ON candidates(handle);

-- Evidence: every justification is a row here (a post, an investment, a review, a bio line...).
CREATE TABLE IF NOT EXISTS evidence (
  evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
  candidate_id INTEGER NOT NULL,
  kind TEXT NOT NULL,         -- 'post' | 'bio' | 'profile_signal' | 'investment' | 'review' | 'article' | 'job' | 'map' | 'event'
  supports TEXT NOT NULL,     -- 'accept' | 'wear' | 'love' | 'impact' | 'reach' | 'competitor' | 'us' | 'authenticity'
  polarity INTEGER DEFAULT 1, -- +1 supports, -1 contradicts
  weight REAL DEFAULT 1.0,    -- 0..1 strength
  url TEXT, date TEXT, text TEXT,
  source TEXT, fetched_at TEXT,
  UNIQUE (candidate_id, kind, supports, url, text)
);

-- Raw LLM classification output per candidate per run (JSON per docs/ARCHITECTURE.md).
CREATE TABLE IF NOT EXISTS classifications (
  candidate_id INTEGER NOT NULL, run_id INTEGER NOT NULL,
  json TEXT NOT NULL, model TEXT, created_at TEXT,
  PRIMARY KEY (candidate_id, run_id)
);

-- Dated events that move scores (fund raised, wearable unboxing, joined competitor, posted about wearable_wishlist...).
CREATE TABLE IF NOT EXISTS events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  candidate_id INTEGER NOT NULL,
  kind TEXT NOT NULL,         -- 'fund_raised' | 'wearable_unboxing' | 'joined_competitor' | 'left_competitor' | 'posted_about_product' | 'device_review' | 'other'
  date TEXT, url TEXT, text TEXT, source TEXT, fetched_at TEXT,
  UNIQUE (candidate_id, kind, url)
);

-- ---------- Scores, per run ----------
CREATE TABLE IF NOT EXISTS scores (
  run_id INTEGER NOT NULL, candidate_id INTEGER NOT NULL,
  p_accept REAL, p_wear REAL, p_love REAL, impact REAL, reach REAL,
  affinity REAL,              -- topic affinity 0..1 (src/wearable_wishlist/affinity.py): discusses personal agents / voice / wearables, and a community answers
  engagement REAL,            -- community engagement 0..1 (src/wearable_wishlist/engagement.py): the average post's views, likes, replies
                              -- and reposts per follower against accounts of the same size; NULL = unknown (no sample)
  confidence REAL,            -- share of factors backed by evidence rather than priors
  total REAL, rank INTEGER,
  flags TEXT,                 -- JSON list, e.g. ["competitor:Oura"]
  reasons_json TEXT,          -- JSON: per-factor short reasons + evidence ids
  PRIMARY KEY (run_id, candidate_id)
);
CREATE TABLE IF NOT EXISTS changes (
  change_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL, candidate_id INTEGER NOT NULL,
  field TEXT NOT NULL,        -- 'total' | 'rank' | 'p_accept' | ... | 'flags' | 'new_candidate'
  old_value TEXT, new_value TEXT,
  reason TEXT NOT NULL        -- human sentence: "Posted an Oura unboxing on 2026-09-20"
);

-- ---------- Paths (who in the operator's network can reach the candidate) ----------
CREATE TABLE IF NOT EXISTS paths (
  path_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL, candidate_id INTEGER NOT NULL,
  rank INTEGER,               -- 1 = best path for this candidate
  how TEXT NOT NULL,          -- 'direct' | 'via' | 'x_direct' | 'open_dm' | 'none'
  strength REAL,              -- 0..1
  via_person_id INTEGER, via_name TEXT,
  you_rel TEXT,               -- label of the operator -> via (or the operator -> target for direct), e.g. "met · 3 meetings · warm"
  target_rel TEXT,            -- label of via -> target, e.g. "introduced them in the map"
  evidence TEXT,              -- why we believe the relationship is real
  json TEXT                   -- full path object in the map's wishlist shape
);

-- ---------- Status (the only table the dashboard writes) ----------
CREATE TABLE IF NOT EXISTS status (
  candidate_id INTEGER PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'candidate',  -- candidate|approved|intro_asked|wearable_sent|wearing|posted|rejected
  owner TEXT, note TEXT, updated_at TEXT
);

-- ---------- Cost ledger ----------
CREATE TABLE IF NOT EXISTS costs (
  cost_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, run_id INTEGER,
  provider TEXT NOT NULL,     -- 'x_api' | 'monid' | 'anthropic'
  endpoint TEXT, units INTEGER, unit_cost_usd REAL, cost_usd REAL, note TEXT
);

-- ---------- On-demand account checks (dashboard "Check an account", src/wearable_wishlist/check.py) ----------
-- A paid check is its own runs row (stage 'check') with the ledger rows under it; it writes no scores, paths or changes,
-- so it never becomes the run the list compares against. A free "Show" from stored data has run_id NULL and cost 0.
CREATE TABLE IF NOT EXISTS checks (
  check_id INTEGER PRIMARY KEY AUTOINCREMENT,
  handle TEXT NOT NULL,
  candidate_id INTEGER,       -- NULL when X has no such account
  run_id INTEGER,             -- the check's runs row; NULL for a free Show
  compared_run_id INTEGER,    -- the scored run it was ranked against
  total REAL,                 -- score.py's total (scheduled weights)
  rank_current INTEGER,       -- rank under the dashboard's weights at check time
  rank_default INTEGER,       -- rank under the scheduled default weights
  n INTEGER,                  -- people in the list it was ranked in (filters applied, the account included)
  cost_usd REAL DEFAULT 0,
  result_json TEXT,           -- the whole answer, so a past check reopens for free
  created_at TEXT NOT NULL
);

-- ---------- Community engagement samples (src/wearable_wishlist/engagement.py computes, src/wearable_wishlist/sample.py fetches) ----------
-- A person's latest original posts (no reposts, no replies, newest first) as fetched on one date. The KPIs average these,
-- not the engagement-ranked posts enrich stores (those are their best posts). x_posts holds each post's counts; the newest
-- sample per x_id is the one used.
CREATE TABLE IF NOT EXISTS engagement_samples (
  x_id TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  source TEXT,
  requested INTEGER,          -- posts asked for (maxItems); fewer returned means the account has no more
  post_ids TEXT NOT NULL,     -- JSON list of post_id, newest first
  run_id INTEGER,
  PRIMARY KEY (x_id, fetched_at)
);

-- ---------- Per-stage outcome of every run (pipeline.run writes it; schedule.py, ops.py and the System tab read it) ----------
CREATE TABLE IF NOT EXISTS run_stages (
  run_id INTEGER NOT NULL,
  stage TEXT NOT NULL,
  status TEXT NOT NULL,       -- 'ok' | 'failed' | 'skipped' | 'running'
  started_at TEXT, finished_at TEXT,
  seconds REAL,
  cost_usd REAL,              -- what this stage added to the costs ledger
  result_json TEXT,           -- the stage's stats
  error TEXT,                 -- the exception when failed; why when skipped (e.g. a monthly cap reached)
  PRIMARY KEY (run_id, stage)
);
