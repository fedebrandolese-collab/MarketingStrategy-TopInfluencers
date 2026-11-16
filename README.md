# ring_wishlist ring wishlist

A system, not a list. It finds people who are actually talking about AI wearables, voice interfaces, personal agents,
smart rings and screen-free life. It scores each one on three probabilities: accepting a ring, wearing it every day,
and loving it and saying so. Impact weighs in too. The path through Carlo's network is shown as information on how to
approach each person, not scored: a warm relationship only nudges P(accept). It re-runs on a schedule and explains
every move. A local dashboard shows the ranked list, the reasons, the path and a status per person.

Built for the brief in [Federico_the_RING_WISHLIST_wishlist.md](Federico_the_RING_WISHLIST_wishlist.md).
The scoring method is in [SCORING.md](SCORING.md), the calls made and why in [DECISIONS.md](DECISIONS.md), spend in [COSTS.md](COSTS.md).

## Run it

```bash
make setup                      # venv + dependencies (needs uv)
cp .env.example .env            # MAP_PASSPHRASE, CARLO_X_HANDLE, X_BEARER_TOKEN, MONID_API_KEY, budgets
make test                       # everything offline, no network, no spend
make run                        # the whole pipeline; every paid batch prints its projected cost first
make dashboard                  # http://localhost:8501
make schedule                   # installs the daily run and the always-on dashboard, both off; make schedule-on starts them
make status                     # is it alive: what ran, what is due, spend against the monthly caps (free)
```

Running it for good (a new Mac or server, Docker, monitoring, backups, handover): [docs/OPERATIONS.md](docs/OPERATIONS.md).
One stage at a time: `python -m ring_wishlist.pipeline run --stage crawl` (or a comma list such as `discover,enrich,classify`).
`DRY_RUN=1 python -m ring_wishlist.pipeline run --stage all --seed carlo_test` runs every stage on fixtures with no network and no spend.
`python -m ring_wishlist.dossier <handle>` prints everything the system knows about one person, evidence and paths included.

## How it works

```
network.ri.fo ─ingest─▶ map tables ─────────────────────────────┐
                                                                  ▼
X via monid ─crawl─▶ Carlo's 1,211 following + 6,820 followers ─resolve─▶ X handles of Carlo's contacts
            ─discover─▶ 35 topic, competitor and author searches ─▶ people, not posts
            ─expand─▶ who the top 12 follow: accounts 3+ of them follow ─▶ people the network vouches for
            ─enrich─▶ targeted device/voice search per promising person
                                   │
                                   ▼
              classify (Claude): category, US base, wear/voice/review evidence quoted from real posts,
                                 competitor ties, handle authenticity, dated events, unknowns
                                   │
X official API ─verify─▶ same numeric account? badge, age, parody signals
                                   │
              paths: map warmth, meetings, intro edges, LinkedIn, X conversations (a follow alone is never a path)
                                   │
              score: P(accept) × P(wear daily) × P(love and say so) × impact (path shown, not scored), change log with reasons
                                   │
              export: wishlist.json = the people who meet the brief's bar (map's wishlist shape) + CSV ──▶ dashboard
```

| Stage | What it does | Paid? |
|---|---|---|
| ingest | Mirrors capital and press from network.ri.fo (131 + 188 people, 75 intro edges, 12 competitor funds, 46 LinkedIn links) | no |
| crawl | Carlo's following and followers with profiles and each account's latest post | monid |
| resolve | Finds the X handles of map contacts by matching them against Carlo's own follow lists (a real relationship), text handles in the map, then a bounded paid search | mostly free |
| discover | Runs [config/queries.yaml](config/queries.yaml) (topics, competitor products, ring_wishlist mentions, `authors`: posts with 200+ likes on voice AI, AI glasses, dictation, agents, wearables, screen time, and `evidence`: people's own first-person posts, 50+ or 100+ likes, about wearing a ring, Whoop, Apple Watch, Meta glasses or a pendant, or using dictation, voice mode or AirPods with AI) and aggregates posts into people | monid |
| expand | People our best people follow: the top 12 of the list (US or unknown, not rejected, no competitor flag, confidence 0.67+, 5k+ followers) are seeds; accounts followed by at least max(3, 20%) of them, with 5k+ followers and no parody, org, spam or brand signs, become candidates (`cofollow:k/n`). The network's vouching replaces the bio keywords, and ranks them for enrichment like strong topical evidence. `python -m ring_wishlist.expand` projects the cost | monid |
| enrich | For the most promising people, searches their own posts for devices, voice tools and screen-free life | monid |
| classify | One Claude call per person on a compact dossier; evidence must quote something in the dossier or it is dropped | Claude Code CLI |
| verify | Official X lookup: the handle still belongs to the same numeric account, plus badge, age and parody checks | X API |
| paths | Best routes from Carlo to each person, strongest first | no |
| score | Three probabilities, impact, confidence, flags (reach shown, not scored), and a change log against the previous run | no |
| export | `data/exports/wishlist.json`: the people who meet the brief's bar, in the map's wishlist shape (renders in its wishlist tab), each with `contact` (the best route and every route with its source); `wishlist.csv`: every scored person with `contact`, `contactable`, `meets_bar` and `bar_reason`; `changes.csv` | no |

## Data sources and what they cost

| Need | Official X API | monid (Apify and TikHub behind one wallet) |
|---|---|---|
| Follower or following rows | $0.010 each | $0.000225 each |
| Posts from search | $0.005 each | $0.0006 each |
| One profile | $0.010 | $0.0015 |
| Used here for | verification: same account id, badge, age | crawl, discovery, evidence |

Every paid call is estimated and printed before it runs. It is checked against the per-provider cap for the run
(`BUDGET_X_USD`, `BUDGET_MONID_USD` in `.env`) and written to the `costs` table with the actual amount billed.
Follow lists are reused for seven days and identical searches for twenty hours, so a crash never buys data twice.

## Privacy and safety

* Nothing contacts anyone. The X side is read-only by construction: an app-only token and scrapers, no write scopes.
* Map data and scrapes stay in this repository on this machine. Carlo approved sending the data to Claude for classification.
* `.env` holds the passphrase and keys and is git-ignored. The map is only re-read with `--refresh-map`.

## Layout

```
src/ring_wishlist/          pipeline.py (orchestrator) · ingest_map · discover (crawl, discover, enrich) · resolve_handles · classify
                     verify · paths · score · export · llm (Claude CLI / API backends) · dossier · config · db · costs
src/ring_wishlist/sources/  monid.py · x_api.py · base.py (shared protocol, offline fixtures)
src/ring_wishlist/prompts/  classify.md
app/                 dashboard.py (Streamlit) · data.py (testable data layer)
db/schema.sql        SQLite schema
config/queries.yaml  discovery queries and limits
data/                raw/ (map export) · fixtures/ · exports/ · wishlist.db (git-ignored) · logs/
docs/                ARCHITECTURE.md (module contracts) · INTEGRATION_NOTES.md
scripts/             refresh.sh · install_schedule.sh · launchd plist · seed_demo_db.py
tests/               offline tests per module
```

## How it was built

The build ran as parallel workstreams against one written contract ([docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)): map
ingestion, the two X adapters, discovery and classification, scoring, paths and export, the dashboard, and the LLM backend.
Each workstream owned its files and its offline tests; the orchestrator, the monid adapter and the verification stage
were written by the lead, and every paid run was started by hand.

## Status (30 September 2026): the list for Carlo

**The list is the 137 people who meet the brief's bar.** The brief says "a name nobody can reach, or someone who has never
worn a device or used a voice tool, does not belong on the list however big they are" and "a shorter list with real
evidence beats a hundred names with adjectives". So the bar is a rule, not a weight. A person makes the list only if they
are in a person category (X voice, media, VC, founder or angel), have net positive evidence that they wear a device or use
a voice tool, and have at least one real way to be contacted; they are US-based or unknown, and nobody rejected them by
hand. Every one of them has been reviewed by hand ([docs/QA_TOP.md](docs/QA_TOP.md)). Rings may go outside Carlo's network
(his decision of 29 September) and he may approach people cold, but never someone nobody can reach.

| Step | People |
|---|---|
| X accounts seen | 19,859: Carlo's 1,211 following and 6,820 followers, 55 topic, competitor, author and evidence searches, the follow lists of the top 12 |
| Candidates | 4,018 (a role or topic signal, a search hit, followed by 3+ of the top 12, a map contact, or checked by hand) |
| Classified by Claude, with quoted evidence | 1,340 |
| Scored (run 77) | 1,394 |
| Rejected in six hand reviews | 148, with reasons: outlets and brands, crypto and promo accounts, anonymous personas, evidence that was not their own use, famous with no device use (competitor ties are flagged, not rejected) |
| Wear a device or use a voice tool (US or unknown) | 191 |
| Meet the bar: and can be contacted | **137**, in `data/exports/wishlist.json` (the map's wishlist shape) and the dashboard |

**The 137:** 59 X voices, 30 media, 35 founders or angels, 13 VCs. 105 are confirmed US-based and 32 unknown. 18 are in Carlo's map. Every one was reviewed by hand. Carlo has about 100 rings: the list is ranked, so the top 100 are the first asks and the rest are the bench for anyone who says no.
Each has a way in: 23 a warm path, 105 open X DMs, 9 a public route found by research (`config/contact_routes/`: an email,
a contact form, a press office...); the researched routes stay listed for everyone as second routes. X reports "DMs open"
differently depending on the account that loads the page, so any sighting of open DMs counts (30 Sep). @iam_agg left the list: X DMs closed, no warm path, no published email or site. 15 carry a competitor flag (founders, investors or staff of Rabbit, Sandbar, Pebble, Sesame, Limitless, Wispr Flow and dictation-app rivals): flagged, never removed; Filters can hide them. Top ten: @joannastern, @alexfinn, @nathie,
@mweinbach, @snazzylabs, @rileybrown, @simonw, @thdxr, @jameygannon, @brianroemmele (flagged: his own GrokPod).

**From 64 to 79 (30 Sep).** 257 people already passed everything but device or voice evidence. Step A fetched posts for the
75 of them never enriched (the other 52 are map contacts with no X account); step B searched only devices and voice tools
for the 130 whose first search found nothing (it mixed in ChatGPT, Claude and agent, which crowd device posts out). 24 then
met the bar; a hand review kept 15 (@danshipper, @alexeheath, @michaelmiraflor, @chriswelch, @jasonhowell...) and rejected 9
(competitor ties, anonymous, promo, evidence that was not theirs; [docs/QA_TOP.md](docs/QA_TOP.md)). Cost $1.51.

**From 79 to 137 (30 Sep, step C).** New people, found by their own words: 20 `evidence` searches ("my Oura", "my Whoop",
"my Meta Ray-Bans", "Hey Meta", "I dictate", Wispr Flow, voice mode on a walk or a drive, talking to Claude Code, AirPods
with voice mode; min 50-100 likes) returned 1,319 posts by 988 authors. Free gates left 482 (no brands, crypto, promo,
non-US or small accounts); Claude read each on the search post (free) and 150 showed their own device or voice use, 95 of
them also contactable and US or unknown. Their posts were fetched (92 people), re-read, and 98 new people met the bar. Three
parallel hand reviews kept 58 and set aside 40: 29 rejected (anonymous personas, crypto and promo, brand accounts, evidence
that was a joke or not theirs) and 11 not in the US ([docs/QA_TOP.md](docs/QA_TOP.md), run 71). The 58 had their handles
confirmed on the official X API and their latest posts sampled for engagement. 36 of them rank in the top 100 (@nathie #3,
@mweinbach #4, @thdxr #8, @jameygannon #9, @bilawalsidhu #13, @alliekmiller #21); single-post fits from sports and politics
sit at the bottom. Cost $3.72 (monid $3.19, X API $0.53).

**Community engagement (new, 30 Sep).** For each of the 64, the average of their latest 30 original posts: views, likes,
replies and reposts, each ÷ followers, against the rate typical for an account their size (fitted on the 64). Replies and
reposts weigh most. It multiplies the score by ×0.7 (a quiet audience) to ×1.3 (a community that answers and reposts);
typical or unknown is ×1.0 (2 of the 64 post too little to measure). It moved the order, not the list: @pogue fell from #2 to
#12 (1.2M followers, but his average post gets 0.008% engagement, 0.2× accounts his size), @scobleizer #14 → #25 and
@anshelsag #10 → #27; @simonw rose #12 → #5 (3.6×), @mkbhd #13 → #6, @benln #22 → #11, @kai_brokering #26 → #14. The
person panel shows the raw numbers and the arithmetic; a "Community engagement" slider turns it up, down or off.

**First account: Andrew Yeung (@andruyeung), #32.** He names Oura among his favourite products (wear), dictates daily with
Wispr Flow (voice), throws New York tech parties (attention), and is a warm contact in Carlo's map (accept); his X audience answers less than
peers' (0.6×), so engagement lowered him from #11 (#32 of 137 now). Joanna Stern
(#1) has the strongest wear evidence (a year testing about ten AI wearables, Meta Ray-Bans), but she calls AI gadgets
"all suck right now", and whether her newsroom lets her keep a device is unknown: a loan, not a gift, and a high-stakes review.

**In the call, the dashboard** (`make dashboard`, http://localhost:8501):
* **Wishlist**: the 137, best first, each with the why, the three probabilities, impact, how to contact them (the best
  route), flags and status. Click a person for the quoted evidence with links, every contact route with its source, the
  warm paths and the change history.
* **Manual weights**: shows the weights as sliders; Run re-ranks in well under a second at no cost. Save a preset, or make it
  the default of the daily run.
* **Check an account**: any X handle Carlo names. It previews the cost (about $0.05, their latest posts included), then says where they would rank and why.

**Spend:** $26.05: monid $21.73, X API $4.32 of the $5 credit, Claude $0 (Claude Code CLI). Details in [COSTS.md](COSTS.md).

**Known limits**
1. Discovery is a sample. @simonw (#5) was found only when checked by hand: he is outside Carlo's graph, none of his posts
   made the searches, and none of the top 12 follow him among their latest 1,000 follows. The daily run widens the sample,
   and "Check an account" covers names Carlo has in mind.
2. The automatic filters catch 54 of the 69 junk accounts the hand reviews rejected, with no false hits on the 75 kept.
   New names after a daily run still deserve a quick look before a ring ships.
3. 32 of the 137 have an unknown US base.

**Next**
1. Carlo sets a status per person in the dashboard (approved, intro asked, ring sent...).
2. Carlo enables the daily schedule when he is ready (`make schedule` prints the command).
3. Seed the co-follow step with AI-builder voices as well as press, to reach people like @simonw on its own.
