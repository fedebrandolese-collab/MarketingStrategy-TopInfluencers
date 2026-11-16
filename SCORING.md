# How the wishlist is scored

Each scheduled run re-scores every candidate from stored rows: evidence, events, X metrics, paths and the classification. Scoring makes no network or LLM call, so the same data always gives the same list.

## Three probabilities, from low priors

| | Question | Moved by |
|---|---|---|
| `p_accept` | Will they say yes to a ring? | wearing or reviewing devices (half weight), raising a fund, posting about ring_wishlist, a warm relationship with Carlo (a small nudge); lowered by a competitor tie |
| `p_wear` | Will they wear it every day? | wearing rings or other wearables, using voice tools (half weight), ring unboxings, device reviews |
| `p_love` | Will they love it and say so? | using voice tools, reviewing products publicly, posting about ring_wishlist (a quarter inside a launch wave), unboxings |

When we know nothing, a person gets the **prior** for their category. The priors are low on purpose: an unknown person is a poor bet for a scarce ring.

| Category | accept | wear | love |
|---|---|---|---|
| X voices | 0.40 | 0.30 | 0.30 |
| Media | 0.45 | 0.25 | 0.25 |
| VCs | 0.35 | 0.25 | 0.25 |
| Founders & angels | 0.40 | 0.30 | 0.30 |
| Other | 0.30 | 0.20 | 0.20 |

**Evidence.** Each quoted finding becomes rows. Wears a device: +1.0 wear, +0.5 accept. Uses voice tools: +1.0 love, +0.5 wear (half: dictation or a voice agent proves the interface, not the habit of wearing a device; a quote classify also files as "wears a device", like "Oura plus Wispr Flow daily", counts in full). Reviews products publicly: +1.0 love, +0.5 accept. Competitor tie: +1.0 competitor. A bio line counts ×0.8 and a paraphrase ×0.6; a finding the other way ("returned it after a week") subtracts.

**Diminishing returns.** For accept, wear and love, one post gives one value, its strongest quote (a re-classification that quotes the same post twice does not count twice). The posts then add on the log-odds scale with diminishing returns: the strongest counts in full, the second half, the third a quarter, and so on, so the sum never passes 2. From a 0.30 prior, one full item gives 0.54, two give 0.66, three give 0.71, and many approach 0.76. Supporting and contradicting posts are summed separately this way, then subtracted. A probability never goes below 0.02 or above 0.98.

Each fact counts once. A wear/love post adds 25% to accept, unless the post was already filed under accept. Profile rows that restate followers or relevance are cited, not added again. A competitor tie seen in evidence and as a "joined" event costs accept once: the larger penalty, at most 1.5.

**Events** add to that: fund raised, ring unboxing, device review, posted about ring_wishlist, joined a competitor. Each fades in a straight line: full on the day, half at 45 days, gone at 90. "Joined a competitor" does not fade until a later "left a competitor" event. An undated event counts half. Evidence rows do not fade.

**Launch-wave praise.** When ring_wishlist launches something, many accounts praise it at once: on 2026-03-20 (TinyNature and HumanPods), 57 accounts posted about ring_wishlist in one day, most of them replies to @carloAI. That shows they joined the launch, not that they used the product and loved it. So praise of ring_wishlist's products counts a quarter toward p_love when at least 10 accounts posted about ring_wishlist products within 7 days of it. That covers a quote on such a post and a "posted about ring_wishlist" event. A post is about ring_wishlist when its text names @RingWishlistAI, TinyNature, HumanPods, ring-wishlist.example or Ring Wishlist (bare "ring_wishlist" and ordinary-language matches do not count), or when classify filed it as a "posted about ring_wishlist" event. Other quotes from the same post count as that post. The wave is found from the stored posts, quotes and events every run, so the October ring launch will be treated the same way. p_accept keeps the full weight, since they did engage with ring_wishlist. An account that sells posts ("DM for paid partnership" in the bio or location field: `discover.sells_posts`, the same test as the enrichment gate's `paid_promo` and classify's guard) gets the quarter weight on all its ring_wishlist praise, whenever it was posted. The reasons say which rows were discounted and why.

## Impact and reach

**Impact** (0–1), the value if they love it, is built in three steps:
1. **Attention** = 0.6 × followers + 0.4 × engagement, both on a log scale. Followers: 1k scores 0.25, 10k 0.5, 100k 0.75, 1M 1. Engagement (likes, reposts, replies and quotes per post): 10 scores 0.26, 100 0.5, 1k 0.75.
2. **Role leverage** = base + weight × attention: media 0.25 + 0.75×, X voices 0.10 + 0.85×, founders 0.30 + 0.55×, VCs 0.40 + 0.45×, other 0.05 + 0.60×. At full attention that is 1.00, 0.95, 0.85, 0.85 (+0.15 for a VC's fresh fund) and 0.65. This encodes attention > network > funding: VCs start higher on network and money but top out lower.
3. Multiply by (0.5 + 0.5 × the classifier's audience relevance).

**Reach** (0–1) is the strength of the best path `paths.py` found. Someone Carlo knows directly reaches up to 0.95. A path via a connector is Carlo's strength with the connector × the connector's link. A mutual X follow with Carlo gives 0.35 and open DMs 0.15. With no path, reach is 0.05.

Reach is **information for Carlo on how to approach the person, not part of the score** (Carlo, 29 Sep: rings may go outside the map; quality over connection). One thing it does move: a real relationship makes a yes more likely. A direct map contact, or an introduction through a connector (`direct` or `via` path), adds 0.5 × its strength to p_accept on the log-odds scale. That is at most +0.475, for example 0.40 → 0.52. A follow or open DMs is not a relationship and adds nothing. No path takes nothing away and is not a flag. The reach reasons say "No path ... approach cold", and `factors.reach.no_path` is true.

## Community engagement: does their audience answer, per follower?

A big account whose posts nobody answers is worth less than a smaller one whose community replies and reposts. So the
score measures, for each person, **how many views, likes, replies and reposts their average post receives per follower**
(`src/ring_wishlist/engagement.py`), with the raw numbers and the arithmetic shown in the dashboard.

- **Which posts.** Their latest 30 original posts: no reposts, no replies, fetched newest first (`make sample`, about
  $0.018 a person). Not the posts enrich stores: those searches rank by engagement, so they are each person's best posts
  and would inflate the average, more for people who post a lot. Posts under a day old (their counts are still growing)
  and over 180 days old are left out. At least 5 posts are needed.
- **The average post.** For each count, the mean after leaving out the top and bottom 10% of posts (at least one each
  side), so one viral post cannot carry an account. Reposts include quotes.
- **Per follower.** Each average ÷ followers: view rate, like rate, reply rate (conversation), repost rate
  (amplification) and the engagement rate, ER = (likes + replies + reposts) ÷ followers.
- **Against accounts of the same size.** Rates fall as followers grow (a 5k account easily gets 2% ER, a 5M account
  rarely 0.1%), so a raw rate would favour small accounts. Each rate is divided by the rate typical for an account with
  as many followers: a log-log least-squares fit on every sampled account (rough X norms until 20 are sampled).
- **One number.** The ratios are combined as a weighted geometric mean: replies and reposts 0.3 each, likes and views
  0.2 each (a ring post that gets answered and reposted travels; a like is cheap). Then
  engagement = 0.5 + 0.25 × log2(index), between 0 and 1: typical for their size 0.5, twice typical 0.75, four times
  1.0, half 0.25, a quarter 0.
- **In the total** it multiplies by (0.7 + 0.6 × engagement): ×1.0 for a typical community, up to ×1.3 and down to ×0.7.
  Unknown (no sample yet, fewer than 5 recent posts, or followers unknown) counts as typical, ×1.0, and says so.
- **Example line** in the person panel: "Average post over their 30 latest original posts: 12,400 views, 310 likes, 42
  replies, 25 reposts. ÷ 85,000 followers: views 14.6%, likes 0.36%, replies 0.049%, reposts 0.029%; engagement rate
  0.44%. Typical for 85,000 followers: … This account: views x1.5, likes x2.0, replies x2.5, reposts x1.8. Weighted
  x2.00 → engagement = 0.5 + 0.25 x log2(2.00) = 0.75."

Impact keeps its own engagement term (absolute interactions per post: how many people act). Community engagement is the
relative one (per follower, against peers): how strongly their audience responds.

## Topic affinity: do they talk about this, and does anyone answer?

Someone who keeps discussing personal agents, and has a community that answers, is more likely to love the ring and say
so. Affinity (0–1) measures that from two independent signals.

**1. Measured from their own posts** (`src/ring_wishlist/affinity.py`, no LLM). Only the last 90 days count.
- A post is topical when it is about **personal agents** (weight 1.0: agents that act for one person, TinyNature, OpenClaw,
  Instinct...) or about the ring's other themes, **voice**, **AI wearables and rings**, **screen-free life** (weight 0.7).
  A passing mention ("I asked ChatGPT for a recipe") is not topical.
- **Volume** (50%): weighted topical posts on a log scale; 1 agent post 0.29, 3 give 0.58, 10 give 1.0.
- **Community** (35%): engagement on those posts, replies counted double because replies are conversation, blended with
  the *lift* over the person's usual posts (does their audience care more when they talk about this?).
- **Recency** (15%): full if the latest topical post is within 30 days, fading to 0 at 90.
- With fewer than 3 topical posts, community and recency are discounted pro rata: one post is an anecdote, not a voice
  (a single post with 100 engagements scores 0.19; three similar agent posts score 0.61).
- Lift compares geometric means, so one viral off-topic post does not make every topic post look ignored.

**2. The classifier's verdict** (`topic_voice`). Claude reads the dossier and answers whether the person *repeatedly*
discusses at least one of the four topics, quoting at least two different posts. Using a product once is not enough.

**Combined:** a "yes" raises affinity to at least 0.40; a "no" caps it at 0.30, and makes it 0 when nothing was
measured. With neither signal, affinity is unknown and listed as such.

**Where it counts:**
- p_love: +1.5 × affinity on the log-odds scale; p_accept: +0.5 × affinity. These sit outside the diminishing evidence
  sum, so a reviewer with many posts still gains from being a topic voice.
- Impact: with 3 or more topical posts, engagement is measured on the topical posts only (replies counted once, like the
  rest of impact), so a community that answers on the topic counts, not their general reach.
- Confidence: an affinity of 0.30 or more backs p_love even without other love evidence.
- The classifier's quotes are stored as `affinity` evidence and cited, never added to the probabilities a second time.
- A move of 0.10 or more writes a change row: "topic affinity 0.20 -> 0.55: new topical posts ...".

## The formula

```
total = 100 × p_accept × p_wear × p_love × (0.3 + 0.7 × impact) × (0.7 + 0.6 × engagement)
```

Community engagement (above) is ×1.0 for a community typical of the person's size and when it is unknown.

Why there is no reach term: Carlo, 29 Sep: rings may go outside the map; quality over connection. Until 30 Sep the total was also multiplied by (0.25 + 0.75 × reach).

The list is ranked by `total`, then by confidence. Because of the 0.3 floor, a small audience lowers a score but never erases someone who would love the ring. **Confidence** is the share of the three probabilities backed by their own evidence or events. Spillover alone does not count.

## Worked example: the fixture reviewer @ringnerd_test

The code's own output on a throwaway database fed with `data/fixtures/llm_classification.json`; `tests/test_score.py::test_worked_example_fixture_reviewer` keeps it true.

| | |
|---|---|
| p_accept | prior 0.40; accept rows #2, #6 (unboxing and Limitless review, 0.5 each, two posts) 0.5 + 0.25 = +0.75; the voice-agent post spills 25% +0.25; affinity 0.40 × 0.5 +0.20 → **0.689** |
| p_wear | prior 0.30; #1 Oura unboxing +1.00; #3 voice-agent post at half weight (0.5), as the second post × 0.5 = +0.25 → **0.599** |
| p_love | prior 0.30; #4 voice-agent post +1.00, #5 Limitless review as the second post +0.50; affinity 0.40 × 1.5 +0.60 → **0.778** |
| impact | 48k followers 0.67, ~1,078 engagements/post 0.76 → attention 0.71; 0.10 + 0.85 × 0.71 = 0.70; relevance 0.90 → × 0.95 → **0.665** |
| affinity | the classifier calls her a topic voice ("Smart rings plus a voice agent is the form factor", "Rings win on that") → floor **0.40** (measured affinity held out of this example because it depends on the date) |
| reach | mutual follow with @carlo_test → **0.35**. Shown for the approach, not scored; a follow is not a warm relationship, so p_accept gets no nudge |
| total | 100 × 0.689 × 0.599 × 0.778 × 0.766 = **24.58** (18.80 before the changes of 30 Sep, with reach × 0.5125); confidence 1.0; no flags; unknown: "whether they accept free devices" |

For anyone on the live list, `python -m ring_wishlist.score --explain <candidate_id>` prints the same breakdown.

## What makes the list move

Each run is compared with the previous scored run. A change row is written when a probability, impact or reach moves by 0.05 or more, when the total moves by a point or more, when the rank or the flags change, or when someone is new. The reason names only the evidence and events the previous run had not seen, plus any path change. If nothing is new, it says "re-scored with same evidence (dated events fade over 90 days)". If the previous run was scored under an older method (`SCORING_VERSION`), it says "scoring method changed on 2026-09-30 (...)" instead. Here is the next day's run of the example, with everyone re-classified as the schedule does (numbers from before topic affinity and the changes of 30 Sep; the reasons read the same):

| Scenario from the brief | Moved | Change log says |
|---|---|---|
| **Ring unboxing**: Rina posts "Unboxing my third smart ring this year" | wear 0.76→0.96, love 0.76→0.84, accept 0.70→0.79; total 15.8→25.0 | `p_wear 0.76 -> 0.96: new evidence: post 'Unboxing my third smart ring this year' (wear, #31); new events: posted a ring unboxing on 2026-09-28 (#1).` |
| **A fund raises**: VC Vera's "We just closed Fund IV" (28 days old, 69% strength) | accept 0.52→0.62, impact 0.60→0.70; total 5.5→7.2 | `p_accept 0.52 -> 0.62: new events: raised a fund on 2026-09-01 (#2).` |
| **Joins a competitor**: Frank posts "I'm joining Oura as VP Product" | accept 0.46→0.16; total 3.2→1.1; still listed | `p_accept 0.46 -> 0.16: new evidence: post 'I'm joining Oura as VP Product' (competitor, #55); new events: joined a competitor on 2026-09-28 (#3).` and `Flags: added competitor:Oura` |

## Flags and unknowns

Flags are shown and never hide anyone: `competitor:<name>`, `not_us`, `unverified_handle`. (`no_path` stopped being a flag on 30 Sep. It is now information in the reach reasons, and the change log does not report it as removed.) A competitor flag comes from a tie in the classification or evidence, or from an active "joined" event. It also comes from a VC whose fund is listed as a competitor investor, either by the map or by `config/competitor_ties.yaml`, aliases included (for example "a16z", which also matches "@a16z"). Finally, it comes from a person that file lists by handle, with the competitor and the kind of tie (founder, employee, investor, advisor) in the reason. A missing file means no extra ties. Competitor evidence and "joined" events also lower `p_accept`; a fund or file match is only a flag.

Unknowns are written out, never guessed. A probability with no evidence stays at its prior and is listed, for example `p_wear: no evidence, prior only`. So are `follower count unknown`, `US base unknown`, `handle not verified` and `no path computed in this run`, next to the classifier's own unknowns. A map contact with no X account scores on priors and reach alone, with confidence 0.

## The bar

The brief: "A name nobody can reach, or someone who has never worn a device or used a voice tool, does not belong on
the list however big they are." Both halves apply, as rules. A warm path is still not required and not scored (Carlo,
29 Sep: rings may go outside the network, and he may approach people cold), but everyone on the list needs at least one
real way to be contacted. A person makes the wishlist only if all three hold:

- **A person.** Their category is X voice, media, VC or founder/angel. `other` holds outlets, brands and accounts with
  no person behind them (`classify.not_a_person`).
- **Net positive evidence of wearing a device or using a voice tool.** The stored p_wear factor (`reasons_json` →
  `factors.p_wear`) is backed by a quote or an event, and its value is above its prior. Voice-tool quotes count, at
  half weight in the score. Evidence that leaves p_wear at or below the prior does not pass: a humour account mocking
  smart glasses, or someone who "used it maybe twice". A score stored without its prior (older runs) fails.
- **A real way to contact them** (`src/ring_wishlist/contacts.py`). The routes, strongest first:
  1. a warm path in the latest paths run: Carlo knows them (`direct`, "Carlo knows them") or a connector can introduce
     them (`via`, "Intro via Jane Doe");
  2. X DMs open (`x_users.can_dm = 1`), or they follow Carlo on X ("X DM: follows Carlo": X lets anyone they follow
     message them). A reply or a mention alone is not a way in;
  3. a public route found by research, in `config/contact_routes/*.yaml`, each with the page it was found on: email,
     contact form, press office, agent, newsletter reply, other social or LinkedIn, in the research's order;
  4. an email address in their X bio, with the X profile as its source.

  Weak routes (the research says so, LinkedIn, a sponsors-only inbox) read "(weak)" and come after the others. A route
  the research could not confirm reads "(unconfirmed)": it is listed last, is never the best route and is never enough
  on its own. A research verdict `not_contactable` wins over its listed routes and a bio email, unless there is a warm
  path or open DMs. A missing research file means no researched routes; a broken one is logged and skipped. Without
  any route the reason names what was looked at: "no way to contact found: X DMs closed, no warm path, <the research's
  reason>".

Why a rule and not a weight: a weight trades evidence against size, and the brief says "however big they are". Three
hand reviews (`docs/QA_TOP.md`) each rejected more than half of the people who reached the top 100 on reach alone, for
example @jeffbezos, outlets and crypto accounts. No weight on p_wear keeps a big enough audience out.

So the bar is a hard filter, like US only. It is on by default in the dashboard (Filters → "Meets the brief's bar"),
rank numbers count after it, and the count line says how many people are below it: "below the bar" for the device or
voice-tool half, "with no way to contact" for the people the list would show but for the reach half.
`weights.hidden_by` is the one filter source for the list, the check tab and the export. The check tab names the part
that fails: "not a person account", "no evidence that they wear a device or use a voice tool", "evidence is against
wearing" or "no way to contact found: ...", and shows the account's routes (a warm path, open DMs, a researched route,
an email in the bio, or none). The dashboard's "How to contact" column shows the best route; the person panel lists
every route with its source link and note.
`wishlist.json` holds only people who meet the bar, are not flagged `not_us` and are not rejected, numbered n = 1..k,
each with `contact: {best, routes: [{type, detail, source_url}]}`. `wishlist.csv` keeps every row, with `contact` (the
best route), `contactable`, `meets_bar` and `bar_reason`. On run 42 the device half leaves 65 people and reach 64: 16
have a warm path, 24 open X DMs and 24 a public route found by research. @iam_agg has none (X DMs closed, no warm path,
no published email or site). A shorter list with real evidence beats a hundred names.

## Limits

- The priors and weights are judgement, not fitted. Once rings ship, the status column (wearing, posted, rejected) gives outcomes to calibrate against.
- The classifier decides what counts. It can only cite the person's own posts and bio.
- Evidence never fades or disappears: an old review weighs like a new one.
- The probabilities are multiplied as if independent, which exaggerates the spread. Read `total` as a ranking, not a chance.
- "Left a competitor" cancels any earlier "joined" event, whatever the company. A tie to a company outside the brief's list is flagged under its own name, for Carlo to judge.
- Follower counts measure size, not the right audience.
- The launch-wave rule cannot tell a paid or coordinated post from a real fan posting on launch day: both count a
  quarter. Once rings ship, a recipient's own "posted" status is the better signal than a post in the wave.
- The classifier sometimes files a review of software (an agent app, a voice model) as a "device review" event, which
  adds to p_wear. Score does not second-guess the event kind.

## Changes of 30 September 2026

Previewed on a copy of the live database, comparing run 14 with the same data under the new method. Main's code on that
data reproduces run 14's top 30 exactly, so every move below comes from these changes.

1. **Voice tools count half toward p_wear** (`VOICE_WEAR_WEIGHT` 0.5). A voice-tool quote proved the interface, not the
   habit of wearing a device, but it counted like "wears an Oura". Which quotes are voice-only comes from the stored
   classifications (filed under voice_tool_user and never under wearable_user). For rows no classification names, it
   comes from the rows themselves: wear+love on one quote is voice_tool_user's pattern. Jesse Middleton (@srcasm) has
   four voice-tool posts and no device: p_wear 0.90 → 0.53. Andrew Yeung (@andruyeung), with Oura plus Wispr Flow,
   drops less: 0.90 → 0.63. Jesse keeps some p_wear from a "device review" event that is really a review of the
   Instinct agent app (see Limits).
2. **p_love spread: one post counts once, further posts diminish** (`EVIDENCE_DECAY` 0.5). A re-classification that
   quoted the same post twice counted it twice, and a linear sum capped at 3 put most of the top at the cap. With topic
   affinity added on top, p_love was near 0.95 for everyone. Now each post gives one value per probability, and the k-th
   strongest post counts 0.5^(k−1). In the top 30, p_love was min 0.85, quartiles 0.91–0.96, median 0.95, max 0.98
   (sd 0.038). It is now min 0.57, quartiles 0.80–0.89, median 0.86, max 0.95 (sd 0.079). The same rule applies to
   accept and wear, so the top-30 median p_wear falls too, from 0.86 to 0.59. A person with more posts still always
   scores higher, only by less.
3. **Launch-wave praise counts a quarter toward p_love** (`LAUNCH_WAVE_*`). The rule, given above under Launch-wave
   praise, finds one wave today: 2026-03-16 to 03-22, 64 accounts. The wave's "posted about ring_wishlist" events are older
   than 90 days and have already faded. The rule now affects three quotes (@jomangee, @ryan_ravenson, @minari250816),
   and it will apply in full to the October launch wave.
4. **Reach left the total** (Carlo, 29 Sep: rings may go outside the map; quality over connection). Reach is shown for
   the approach. A real relationship (`direct`/`via` path) adds `WARM_ACCEPT` 0.5 × strength to p_accept, and no path
   is no longer a flag. Ten of the new top 30 have no path, against eight before.
5. **`config/competitor_ties.yaml`**: researched competitor funds (with aliases) and people, flagged like the map's
   fund list. There are flags only, no penalty.

Top 10, run 14 → new method: srcasm, andruyeung, nandoprince93, scobleizer, emollick, raywongy, benln, parul_gautam7,
mattepstein, ericmigi → raywongy, morganlinton, rileybrown, andruyeung, ericmigi, hovinthenorth, emollick, scobleizer,
mattepstein, nandoprince93.
- Without the reach term, people with strong evidence but a weak or no path rise: raywongy 6 → 1, a Gizmodo reviewer
  with device reviews (reach 0.25), and morganlinton 14 → 2, whose p_wear rests on Fitbit, WHOOP and Oura posts (no
  path).
- A strong topic affinity holds p_love up: rileybrown 12 → 3, affinity 0.82.
- srcasm 1 → 11: his direct path (reach 0.81) no longer multiplies the total, and his p_wear rested on voice-tool posts.
  The path now only nudges p_accept: 0.73 → 0.80.
- Others whose p_wear rested on voice-tool posts move down: benln 7 → 12, parul_gautam7 8 → 20, kai_brokering
  23 → 41. So do people whose evidence was one post quoted twice: akashtiwari1007 22 → 24.

## Constants (`src/ring_wishlist/score.py`, line numbers)

| Constant | Value | Role |
|---|---|---|
| `PRIORS` (48) | table above | starting probabilities |
| `EVIDENCE_SLOPE`, `EVIDENCE_CAP` (55–56) | 1.0, 3.0 | log-odds per unit of evidence; bound on the sum (with `EVIDENCE_DECAY` it no longer binds for accept, wear, love) |
| `EVIDENCE_DECAY` (57) | 0.5 | accept/wear/love: one value per post, the k-th strongest post counts 0.5^(k−1), so the sum tends to 2 |
| `VOICE_WEAR_WEIGHT` (61) | 0.5 | weight of a voice-tool quote toward p_wear (a quote also filed as wearable counts in full) |
| `ACCEPT_SPILLOVER`, `_CAP` (63–64) | 0.25, 1.0 | share of a wear/love post that also counts for accept |
| `COMPETITOR_PENALTY`, `_CAP` (65–66) | 1.0, 1.5 | accept penalty per unit of competitor evidence |
| `P_MIN`, `P_MAX` (67) | 0.02, 0.98 | bounds on every probability |
| `EVENT_DECAY_DAYS` (69) | 90 | fade of events |
| `EVENT_EFFECTS` (72) | fund raised accept +0.6, impact +0.15 (VCs); ring unboxing wear +1.0, love +0.5; device review wear +0.6, love +0.8; posted about ring_wishlist love +1.0, accept +1.0; joined competitor accept −1.5 (no fade) | log-odds per event; impact added directly |
| `UNDATED_EVENT_WEIGHT` (82) | 0.5 | an event with no date |
| `ROLE_LEVERAGE` (93) | see Impact | base and weight per category |
| `FOLLOWERS_`, `ENGAGEMENT_`, `RELEVANCE_UNKNOWN` (100–102) | 0.25, 0.30, 0.30 | assumed when unknown, and listed as unknown |
| `IMPACT_EVIDENCE_STEP`, `_CAP` (103–104) | 0.05, 0.15 | impact per unit of other impact evidence (an article, a column) |
| `RING_WISHLIST_PRODUCT_RE` (113) | @RingWishlistAI, TinyNature, HumanPods, ring-wishlist.example, Ring Wishlist | what makes a post praise of ring_wishlist's products |
| `LAUNCH_WAVE_DAYS`, `_MIN_ACCOUNTS`, `_WEIGHT` (114–116) | 7, 10, 0.25 | a wave is 10+ accounts praising ring_wishlist within 7 days of a post; its weight toward p_love |
| `SCORING_VERSION` (120) | 2026-09-30 | stored per score; a change row names a method change instead of "same evidence" |
| `REACH_NONE` (124) | 0.05 | reach without a path |
| `CHANGE_THRESHOLD` (125) | 0.05 | smallest move of a probability, impact or reach that is logged |
| `TOTAL_CHANGE_THRESHOLD` (126) | 1.0 | smallest move of the total (points) that is logged, so the daily fade of old events stays quiet |
| `AFFINITY_FLOOR_IF_VOICE`, `AFFINITY_CAP_IF_NOT` (132–133) | 0.40, 0.30 | classifier yes / no bounds (no with nothing measured: 0) |
| `AFFINITY_LOVE`, `AFFINITY_ACCEPT` (134–135) | 1.5, 0.5 | log-odds per unit of affinity on p_love and p_accept, outside the evidence sum |
| `AFFINITY_MIN_TOPIC_POSTS` (139) | 3 | topical posts before impact uses topic engagement |
| `AFFINITY_CHANGE_THRESHOLD` (140) | 0.10 | smallest affinity move logged |
| `THEME_WEIGHTS`, `SCORE_WEIGHTS`, `VOLUME_FULL_POSTS`, `RECENCY_FULL_DAYS`, `WINDOW_DAYS` (affinity.py) | agents 1.0, others 0.7; 0.5/0.35/0.15; 10; 30; 90 | how measured affinity is built |

Evidence weights per finding live in `EVIDENCE_MAP` in `src/ring_wishlist/classify.py`. Path strengths live in `src/ring_wishlist/paths.py`.

## Tuning the weights live

The dashboard has one list. By default it is ranked with the default weights: every weight below at its default, or
the `default:` saved in `config/weights.yaml`. For a run scored by today's code that is the stored ranking (checked on
run 19). The **Manual weights** switch, next to search, shows the weights as sliders, pre-filled with the defaults.
**Run** re-ranks the latest scored run from its stored factors. It takes about 0.03 s on run 19. There is no pipeline
stage, no LLM and no API call, so it costs nothing. Switching it off returns the list to the defaults; switching it on
again shows the last slider values. Every view asks `weights.effective(st.session_state)` which weights are in use.
`src/ring_wishlist/weights.py` holds the formula:

```
total = 100 × p_accept^w_accept × p_wear^w_wear × p_love^w_love × (0.3 + 0.7 × impact)^w_impact
            × (0.7 + 0.6 × engagement)^w_engagement
            × (0.25 + 0.75 × reach)^w_connection × (0.3 + 0.7 × affinity)^w_affinity × w_category
```

- **Defaults.** Every weight is 1, except connection and affinity, which are 0. With the defaults the total is the
  formula above bit for bit. This was checked on all 670 people of run 14, re-scored with the current code on a copy of
  the database.
- **What a weight does.** 0 removes a factor, and 2 doubles its emphasis. Community engagement at 0 ranks as before
  30 Sep (no engagement term); at 2 the most engaged community counts ×1.69 and the least ×0.49. Connection brings back the reach term used
  before 30 Sep. Topic affinity is already inside accept and love, so its slider adds extra emphasis on top; an
  unknown affinity counts as 0.
- **Categories, not priorities.** The brief's order (attention > network > funding) lives inside impact as one number
  per role (`ROLE_LEVERAGE`). `reasons_json` stores impact as a value plus text. Network and funding are never separate
  terms: a role's `base` holds both. So there is one weight per category instead (X voices, media, VCs, founders &
  angels, other). A category weight multiplies that category's totals: 0 removes the category, and 2 doubles its
  totals.
- **Filters.** One Filters popover. Who is ranked: US only (on), hide competitor-tied (off: competitor ties are flagged, never removed), hide rejected (on), the
  brief's bar (on, see "The bar") and list size N (100); rank numbers count after these. The count line reads "Top N
  of K people who meet the bar · hidden: X not US, Y rejected, Z below the bar, W with no way to contact". Each
  hidden count is taken on the whole run, except W: the people who pass every other filter and the device half but have
  no way to be contacted. When fewer than N people qualify, the list shows them all. The rest (category, status, flag, minimum confidence
  or topic affinity) and search only hide rows, so ranks never renumber; with any of them on, the list shows every
  match, also outside the top N. Under manual weights a **vs default** column (↑/↓/=/new) compares with the default weights under
  the same filters.
- **Below the list.** The category mix of the top N, always; under manual weights also its change against the default
  and who entered or dropped out of the top N. Downloads of the top N as CSV and `wishlist.json` in the export's own
  shape (`export.reorder`, `export.csv_text`), under the list's filters, so they match the view.
- **Presets.** **Save weights**, in the Manual weights panel, writes a named preset to `config/weights.yaml`. "Use as
  default for scheduled runs" also writes `default:`, which `score.py` then applies to the stored total from the next
  run on, and which the list uses as its default. Its reason line names the weights. Without a `default:` key (the file
  ships without one), or when the file is missing or broken, the scheduled totals are today's.

## Checking an account live

The dashboard's **Check an account** tab answers "would this account make the list, and why?" for any X handle
(with or without @, or an x.com link). `src/ring_wishlist/check.py` does the work with the pipeline's own functions.

- **Cost first.** The preview itemises the adapters' own estimates before anything is called: the official X lookup
  ($0.010), the two post searches that enrich makes (20 + 10 posts × $0.0006 on monid, an upper bound), and one
  Claude classification (no dollar cost; it counts toward the Claude account usage). A typical check costs about
  **$0.03**. Nothing is paid before **Accept $X.XX and run**.
- **The run.** A check is its own `runs` row with stage `check`, so every paid call is guarded and lands in the cost
  ledger. It looks the handle up (if the account is missing, suspended or protected, it stops there). It then adds the
  person as a candidate (`discovered_via='manual'`), fetches their posts (`run_enrich(candidate_ids=...)`, past the
  gates) and classifies them. Topic affinity and paths are computed for this person only.
  `score.score_candidate` scores them with the current code against the latest scored run. It writes no `scores`,
  `paths` or `changes` rows, so a check never becomes the run the list, the export or the change log compare against.
- **The rank.** The account is placed among that run's people, re-ranked from their stored factors
  (`weights.rerank`). It uses the list's weights (`weights.effective`), and the default weights when those differ.
  The list's filters (US only, hide rejected, the bar, ...) apply to the others, through the list's own
  `weights.hidden_by`. The account itself is always placed, so a filtered-out account still shows where it would
  stand, with the reason. Below the bar, the reason names the part that fails: not a person account, no evidence that
  they wear a device or use a voice tool, evidence against wearing, or no way to contact them (with what was looked at).
- **The answer.** It shows the neighbours, then each of the three probabilities with its strongest quote (or "no
  evidence, prior only"). Then impact, topic affinity, flags and how to contact them: a warm path, open DMs, a
  researched route, an email in the bio, or none, which fails the bar on reach. Last comes one line on what would
  move them up: the lowest probability without evidence, since in a product the smallest factor is the biggest lever.
- **Free paths.** An account already known, with posts fetched in the last 30 days and a classification, can be shown
  from stored data at no cost. Every check is saved in `checks`, and past checks reopen for free, re-ranked under the
  current weights. **Keep off the wishlist** sets the status to rejected ("manual check: not for the list"). Otherwise
  the next scheduled run scores the person like anyone else.
