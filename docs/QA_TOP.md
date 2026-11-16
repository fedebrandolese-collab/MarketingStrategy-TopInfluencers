# QA of the top 60, run 14

Reviewed 2026-09-29 against the brief, one dossier at a time (`python -m ring_wishlist.dossier <handle> --run 14`). Each person was
checked for: a person and not an org, US base, quoted evidence for accept, wear and love, competitor ties, an authentic
handle, and a real path.

**Result: 2 keep, 17 fix, 41 reject.** The 41 rejects have status `rejected` in the live DB. Each has a one-line
`QA run 14:` note, written with `app/data.py set_status`. A fix means the person may belong, but part of the record is
wrong or missing. The reason names what to fix.

| Rank | Handle | Verdict | Reason |
|---:|---|---|---|
| 1 | @srcasm | fix | Warm Flybridge GP, daily voice agents; p_wear 0.90 comes from voice quotes only, no wearable. |
| 2 | @andruyeung | keep | Warm map contact; wears an Oura, dictates daily with Wispr Flow, posts his favourite products. |
| 3 | @nandoprince93 | keep | 9to5Mac video lead, Manhattan; Apple Watch and AirPods "all day, every day"; met Carlo. |
| 4 | @scobleizer | fix | Wears a ring even in the shower. Only path is a mutual follow; Fast Company was a past employer. |
| 5 | @emollick | fix | Uses voice mode, no wearable; open DMs only, no relationship. |
| 6 | @raywongy | fix | Gizmodo wearables reviewer; best path goes through ex-employer Mashable; the real one is a Gizmodo colleague (0.19). |
| 7 | @benln | fix | Dictates daily with Wispr Flow (usage; no investment found); no wearable; open DMs only. |
| 8 | @parul_gautam7 | reject | Bio and location sell collabs; sponsored-style posts; location hidden. |
| 9 | @mattepstein | reject | Runs a marketing agency that sells posts; skeptical of wearables; open DMs only. |
| 10 | @ericmigi | reject | Pebble founder shipping the Index 01 voice ring, a direct competitor; not flagged. |
| 11 | @chrisgpt | reject | Pseudonymous AI-news account; the TechCrunch "path" is "as featured in", not a job. |
| 12 | @rileybrown | fix | Daily Wispr Flow user, builds agents; no wearable; open DMs only. |
| 13 | @doctorcalf | reject | Anonymous account (empty bio, name "string"), 3.5k followers, identity and base unknown. |
| 14 | @morganlinton | fix | Strongest wear evidence here (four devices a night) but no path; "Tahoe" is US, read as unknown. |
| 15 | @ericbuess | fix | Wears a Limitless pendant, carries a Rabbit R1 for voice agents; 5k followers; open DMs only. |
| 16 | @phonenurd | reject | One-year-old phone-leaks account; one Ultrahuman review; no relationship. |
| 17 | @ryanmorrisonjer | reject | UK-based (Hemel Hempstead). |
| 18 | @vadimyuryev | reject | No path; argues AirPods and glasses beat AI pins. |
| 19 | @flyosity | reject | The voice evidence is "downloaded Wispr Flow"; no wearable; open DMs only. |
| 20 | @hovinthenorth | reject | No path; the wear quotes show abandonment ("used it maybe twice"). |
| 21 | @jomangee | reject | New Zealand; already uses HumanPods and TinyNature; 1.9k followers. |
| 22 | @akashtiwari1007 | reject | India-based; no path. |
| 23 | @kai_brokering | fix | Founds VoiceOS, a Wispr Flow-style dictation rival: flag for Carlo. Open DMs only. |
| 24 | @kamath_sutra | fix | Voice-agent founder who uses a Pocket recorder; the "path" is a one-way follow. |
| 25 | @xfreeze | reject | Grok fan-promotion account, empty bio, no path. |
| 26 | @aryanxmahajan | reject | Agency marketing threads; wear rests on stale rows ("built a voice agent"). |
| 27 | @gesorameshack | reject | Sells collabs; no wear evidence; path is one reply; display name and handle differ. |
| 28 | @datachaz | reject | Location field sells collabs; no wearable; no path. |
| 29 | @techradar | reject | Outlet (Future plc), not a person. |
| 30 | @itsolelehmann | reject | No path; no wearable; bio ("eu/acc") suggests Europe. |
| 31 | @zivreichert | reject | LocalGlobe / Phoenix Court partner (map): London, not US. |
| 32 | @testingcatalog | reject | Berlin AI-news outlet, not a person. |
| 33 | @ritwikpavan | fix | Relays hardware news; one glasses quote, no love evidence; mutual follow only. |
| 34 | @iam_agg | reject | Hacks Humane pins, 2.5k followers; no path; location unknown. |
| 35 | @gregisenberg | reject | No wearable or voice-tool evidence; open DMs only. |
| 36 | @socialwithaayan | reject | AI-tools promo creator (March TinyNature wave); no wear evidence; path is one reply. |
| 37 | @steventey | reject | No path; voice evidence only. |
| 38 | @indianidle | reject | India-based per the map; no personal wear evidence. Keep as a press contact, not a ring. |
| 39 | @matiasschrank | reject | Spanish-language promo account; path is a one-way follow. |
| 40 | @mkbhd | reject | No path; one post in the dossier. |
| 41 | @stufflistings | reject | India-based giveaway and sponsored-post account. |
| 42 | @illscience | fix | a16z led Sesame's Series A: add the flag. Praises Sesame voice. Deel path is a board seat. |
| 43 | @hasantoxr | reject | Location field sells sponsorships; no wear evidence. |
| 44 | @bgr | reject | Outlet, not a person. |
| 45 | @signulll | fix | Uses MacWhisper, no wearable, dislikes Meta glasses; open DMs only. |
| 46 | @venturetwins | reject | No wear or voice-tool evidence; a16z led Sesame's Series A (unflagged). |
| 47 | @raz0r | reject | Toronto. |
| 48 | @chriswelch | fix | Warm Bloomberg reviewer; only two posts, no personal wear or voice quote: enrich first. |
| 49 | @kyliebytes | fix | SF writer who wears an unnamed wearable "constantly"; no path: find one or drop her. |
| 50 | @jeremycaplan | fix | Warm NYC press contact; only two posts, no wear or voice evidence: enrich first. |
| 51 | @ryan_ravenson | reject | 540 followers: brings no attention, network or funding; already a customer. |
| 52 | @ianonymous3000 | reject | Audits wearables as a privacy critic; no path. |
| 53 | @viipin8 | reject | Sells collabs; no wearable. |
| 54 | @kevinafischer | reject | No path; no wearable; the "voice" quote is not tool use. |
| 55 | @tankots | reject | Wispr Flow CEO (flag correct); no path. |
| 56 | @andersdoestech | reject | Copenhagen. |
| 57 | @michaelsikand | reject | Finance influencer; no wear evidence; no path. |
| 58 | @aaravgarg | reject | 2.2k followers, open DMs only; the flag says Omi employee, but he left Omi. |
| 59 | @brianroemmele | fix | Voice-first advocate with no quoted wear or voice use; mutual follow only. |
| 60 | @cosmopolitanuk | reject | UK magazine brand. |

**Handles.** No parody or impersonation was found in the top 60: every handle is verified or likely real on the official
API. The problems are orgs, promo accounts and anonymous accounts.

**Category mix.** The 19 keep-or-fix names are 7 X voices, 5 media, 5 founders or angels and 2 VCs, one of them with an
unflagged Sesame tie. VCs are the thin part. The map holds 131 capital contacts with real paths, and they need evidence
(enrich, classify) before any of them can outrank cold X accounts.

## Mistakes that repeat

1. **Stale evidence counts after the model changes its mind.** 84 scored people carry 232 post/bio rows that their latest
   classification no longer cites. For example, @signulll's question "what is everyone using for voice dictation on mac?"
   still counts as daily voice use, and @jeremycaplan's newsletter link counts for accept and love. Stale rows also
   count a post twice when the latest answer quotes it with a different span (@srcasm). **Guard added:** classify now deletes those rows when it re-classifies
   a post that is back in the dossier. Rows already in the DB clear on the next forced re-classify of those people.
2. **Orgs, outlets, aggregators and fan pages filed as media or X voices.** In the top 60: @techradar, @testingcatalog,
   @bgr, @cosmopolitanuk, @xfreeze. Across all 521 classified accounts, 45 are affected. The classifier's own summary says
   "outlet" or "brand account", but the schema has no field for it. **Guard added** (`classify.not_a_person`): it checks
   the X business or government badge, a competitor's own brand account, organisation phrases in the bio, or the summary's
   own words. It never fires when the bio has first person or a personal role. Such an account gets category `other`, plus
   one wear row and one accept row against it.
3. **Accounts that sell posts.** In the top 60: @parul_gautam7, @viipin8, @datachaz, @hasantoxr and @gesorameshack, with
   the call to action often in the location field. Across the DB, 52 are affected. **Guard added**
   (`discover.sells_posts`, bio and location): `enrich_gate` skips them as `paid_promo`, and classify adds a love row
   against them.
4. **Dropped devices counted in favour.** "I bought the Rabbit R1 and have used it maybe twice" counted as wear, and "I'm
   returning my Fitbit Air" as accept and love. **Guard added:** such a quote under wearable or voice use now counts
   against wear and accept. A public return stays review evidence.
5. **Non-US people in the top.** 10 of the 60. `not_us` is only a flag in `score.py`, so it lowers nobody's rank. Carlo's
   map entry says "TechCrunch (India)" for @indianidle, yet us_based was null. **Guard added:** a non-US place in the map
   role or company sets us_based=false when the location field says nothing. Owner of `score.py`: consider making
   `not_us` a gate, since the audience is US only.
6. **Weak paths (paths.py, not mine).** In the top 60, the best path is none for 17 people, open DMs for 21, and only an
   X follow or reply for 13. A one-way follow scores 0.30, and a single reply scores 0.40, above a mutual follow (0.35).
   The brief says a follow is not a path. "Same firm" bio matches link past employers ("Ex-… Fast Company", "Prev …
   @mashable"), press mentions ("as featured in … TechCrunch") and board seats ("Boards of … Deel").
7. **Wear credited from voice use alone** (@srcasm, @emollick, @benln, @rileybrown, @signulll). This is the first fix
   listed in the README, owned by `score.py`.
8. **Competitor ties missed.** Two cases are outside the brief's list: Pebble's Index 01 ring (@ericmigi) and VoiceOS
   (@kai_brokering). One is a fund-level tie: a16z led Sesame's Series A, but a16z is not in `map_competitor_funds`.
   Also, @aaravgarg's former Omi role is flagged as a current job.

## Prompt suggestions for `src/ring_wishlist/prompts/classify.md` (not edited: the data agent is classifying with it)

- Add an `account_type` field (person / outlet / company / aggregator / fan_page) and a `sells_posts` field. The model
  already writes both down in the summary.
- These do not count as voice-tool use: building or selling voice agents, a question ("what is everyone using…"), or
  "downloaded X". A dropped device ("used it twice", "returned it") is evidence against wear.
- Read the map entry's role and company for location ("TechCrunch (India)").
- competitor_tie: add a `former` kind ("prev built @OmiDotMe", "I am leaving @omidotme"). Name direct competitors outside
  the list (a voice ring such as Pebble Index 01). Flag a VC whose fund backs a listed competitor (a16z → Sesame).

# Run 30 review

Reviewed 2026-09-29 with `python -m ring_wishlist.dossier <handle> --run 30`. The list is the top 100 of run 30 under the
dashboard's default filters (us_based not 0, status not rejected), minus the 22 people already reviewed above, plus
@himmelmeer22 and @kai_brokering, which the data agent asked about. That makes 80 people. Each was checked for five things:
a real person and the right one, a US base or no evidence either way, quoted evidence for accept, daily wear and
love-and-say-so, competitor ties, and an authentic handle. Carlo changed one rule since run 14: rings may go to people
outside his network, so "no path" is no longer a reason to reject. The path is noted only when it helps.

**Result: 23 keep, 15 fix, 42 reject.** The 42 rejects now have status `rejected` in the live DB, each with a
`QA run 30:` note, written with `app/data.py set_status`. "Fix" means the person may belong but part of the record is wrong
or missing, and the reason says what. No web lookups were paid for. Three web searches settled the ties:
[Sandbar's investors](https://techcrunch.com/2026/03/10/sandbar-secures-23m-series-a-for-its-ai-note-taking-ring/)
(betaworks backed the pre-seed; SPC is not listed),
[VoiceOS](https://www.ycombinator.com/launches/PP3-voiceos-the-siri-for-productivity), and
[Nick Vasilescu](https://www.nickvasilescu.com/) (Orgo; no Limitless role or investment found).

**The names the data agent asked about.** @matt503ea5sf9z5: reject, an anonymous account with no device use.
@himmelmeer22: reject, based in Hangzhou. @mommypotamus: reject, a blog feed written in a brand voice for a wellness
audience; she has worn an Oura for years but uses no voice or AI tools. @kai_brokering: flag him, not reject him. VoiceOS is
desktop voice software that goes beyond dictation, so it competes with Wispr Flow, not with the ring, and it could be a
partner. @nickvasiles: keep. He uses a Limitless pendant, which is not a tie. @joannastern: fix. Whether her employer lets
her keep devices is unknown; she may take a loan and not a gift. She is also on record that AI gadgets "all suck right now",
so she would be an honest, high-stakes reviewer.

| Rank | Handle | Verdict | Reason |
|---:|---|---|---|
| 1 | @joannastern | fix | Tested ~10 AI wearables, wears Meta Ray-Bans; love rests on a car post. Unknown: can she keep devices? |
| 2 | @pogue | keep | CBS correspondent, 1.2M followers; writes how he uses his Apple Watch; tested AI Siri hard. |
| 6 | @nickvasiles | keep | SF founder; always-on Limitless pendant and Meta Ray-Bans daily. Usage only: no Limitless job or investment found. |
| 7 | @apollozac | keep | 9to5Mac senior editor; wears Apple Watch Ultra and AirPods, says what he loves; path via Fernando Silva. |
| 8 | @anshelsag | keep | San Diego wearables analyst in Carlo's map; wears Meta Oakley glasses and a WHOOP; Oura use, no tie. |
| 10 | @simonw | keep | SF, 228k; talks to his tools by voice and blogs everything he uses; no wearable. |
| 13 | @nikillinit | keep | NYC healthcare newsletter, 66k; tracks his sleep, tried Meta Ray-Bans, writes about wearables often. |
| 14 | @petergyang | keep | 285k; tests personal agents and voice modes weekly; no wearable; location unknown. |
| 16 | @benspringwater | keep | SF founder (Matter); praises Sesame and dictation apps in public (usage, not a tie); no wearable. |
| 18 | @andrewwarner | keep | Austin interviewer, 40k; daily dictation user who compares apps in public; no wearable. |
| 19 | @priymrj | fix | Founds TypeVoice, a dictation app (Wispr Flow rival): flag. 'sf / blr' is split, not US. |
| 20 | @mommypotamus | reject | Blog feed in a brand voice (third-person bio, 28 following); wellness audience; no voice or AI use. |
| 24 | @onejkmolina | keep | 239k; wears an Ultrahuman ring and praises it (usage, no tie); marketer audience; location unknown. |
| 25 | @kai_brokering | fix | VoiceOS is a desktop voice layer beyond dictation: a Wispr Flow rival, not a ring rival. Flag added. |
| 26 | @damonchen | reject | Lives in Shanghai (school runs from Shanghai station, Mar 2026); the summary said so, us_based stayed empty. |
| 27 | @jxnlco | fix | OpenAI staff hiring for ChatGPT dictation; OpenAI builds devices too: flag for Carlo. No wearable. |
| 28 | @jaykapoornyc | keep | NYC GP backing physical AI; wears an Apple Watch, posts product delight; a VC, which the list lacks. |
| 29 | @cocktailpeanut | reject | Pseudonymous Pinokio developer; identity and base unknown; wear rests on two January voice posts. |
| 30 | @paularambles | keep | SF, 49k; builds whimsical AI, posts Tolan voice-companion milestones; uses voice products; no wearable. |
| 31 | @mattswider | keep | NYC reviewer, 837k; wears a smart ring and Meta glasses daily; path is only an ex-TechRadar link. |
| 32 | @shl0ms | fix | Pseudonymous, 91k; dictates with a Sandbar ring and loves it. Identify the person and base first. |
| 33 | @auntiepaca | reject | Crypto persona with a 'twin' account and AU/CN flags; Oura praise only; no voice or AI use. |
| 34 | @huang_song_ | fix | Founder-CEO of Typeless, a dictation app (Wispr Flow rival): flag. Voice evidence is his own product. |
| 37 | @jenniferjolly | keep | Seattle USA Today and Today Show columnist, in touch with Carlo; uses Siri; no wearable quote. |
| 39 | @rezkhere | reject | AI-tools promo creator: location field advertises Seedance; March TinyNature wave; no wearable. |
| 40 | @kenwheeler | keep | NJ developer, 70k; uses ChatGPT voice mode daily; hacks a Friend pendant and an Oura per the summary. |
| 41 | @rahul_j_mathur | reject | India-based: location 'BOM 🏡 BLR 🏢' (home Mumbai, office Bangalore); DeVC is an Indian fund. |
| 42 | @attacomsian | reject | 4.3k followers; sells Flit, his own Wispr Flow-style dictation app; no wearable. |
| 43 | @jessiexiao_eth | reject | Crypto ecosystem-fund principal; tried Meta Display glasses once in a store and panned them; no worn device. |
| 44 | @sashakaletsky | fix | Warm VC (met Carlo); no wearable, only voice use is one AI-run interview: enrich before a ring. |
| 46 | @bran_don_gell | fix | COO of Every, which sells Monologue, a Wispr Flow rival: flag added. Daily voice user; no wearable. |
| 47 | @greengart | fix | Devices analyst in Carlo's map who reviews in public; no quote of a device he wears daily: enrich. |
| 49 | @sawyerhood | keep | SF engineer, 18.5k; dictates code with Wispr Flow and built his own dictation tool; no wearable. |
| 50 | @synthpotato | reject | Games-only reviewer site; AirPods is the only device; no AI or voice interest. |
| 51 | @dominikmartn | reject | 3.8k followers; voice evidence is his own app Aside (a dictation rival); no wearable. |
| 52 | @andilosing | reject | Runs his business from Germany ('documents you need in Germany'), travels; one dictation post; no wearable. |
| 53 | @linusekenstam | reject | No device or voice-tool use (praises voice models only); bio sells partnerships. |
| 54 | @f3dericobartoli | reject | Ecommerce CRO agency; one voice-mode tip, no worn device; skeptical of agents. |
| 55 | @kleffew94 | reject | Crypto agent developer; voice evidence describes a product he tried, no own device or daily voice use. |
| 56 | @irabukht | keep | SF founder, 20k; manages her agents by voice with Wispr Flow; marketer audience, no wearable. |
| 57 | @alexeheath | fix | Sources host, 483k; wrote of 'a new AI smart ring I really want'; no quoted device use: enrich. |
| 58 | @sabrinaa_ortiz | fix | Warm (two meetings); map role is stale, now The Deep View; hands-on glasses tests, no daily device. |
| 59 | @daniel_nguyenx | reject | No device or voice-tool use: E Ink picks and model tests only; base unknown. |
| 60 | @anitakirkovska | reject | Only evidence is one live agent demo; no worn device or daily voice use. |
| 61 | @garlotic | reject | Spanish-language AI-growth creator for brands (March TinyNature wave); no wearable or voice use. |
| 62 | @outsource_ | reject | Local-model builder and paid Qwen ambassador; no worn device or voice use quoted. |
| 63 | @rubenhassid | reject | Only voice evidence is Wispr Flow in a tools listicle; no worn device. |
| 64 | @jason | fix | 1.8M investor-host; wants to fund no-camera glasses; no quoted device or voice use: enrich. |
| 65 | @bryan_johnson | keep | LA, 2.2M; wears a sleep ring in his daily protocol; love and accept evidence thin. |
| 66 | @mil000 | reject | Mocks AI agents and pendants; AirPods only; a hostile fit. |
| 67 | @davidlinthicum | fix | Uses Plaud note-takers (competitor product) in promo-style reviews: check for sponsorship. Enterprise audience. |
| 69 | @viditb | reject | Voice evidence is his own app's feature; skeptical of voice UIs and glasses; 5k followers. |
| 70 | @kevintwohy | reject | Designed Sandbar's Stream ring (paid design work) and promotes it: direct competitor tie. |
| 71 | @aunder | keep | Warm (Adrian Aoun intro); Adverb VC, ex-Slack CPO; uses Claude hands-free on her Apple Watch. |
| 73 | @karpathy | keep | 4.2M; dictates to LLMs ('switch to /voice and just ramble'); no wearable; location 'Earth'. |
| 74 | @kevin_parker_ai | reject | Promo account: 'Influencer Marketing BIZ' email, ghostwriter, 670k posts; no device use. |
| 75 | @rosenzone | keep | Tested Sandbar's voice ring and raved in public (May); retail-newsletter audience; may have been seeded. |
| 76 | @morellifit | reject | Longevity and peptide seller; wear quote is generic advice; skeptical of Oura and Whoop; no voice use. |
| 78 | @ash_twtz | reject | 6.9k AI-hype account; the one voice quote is a reply praising a product; base unknown. |
| 79 | @omoreyy___ | reject | Backup account ('@omoreyy_ backup') of a crypto community manager; no device or voice use. |
| 80 | @dheemanthredy | reject | Builds voice models ('Made-in-India'); only voice evidence is his own model's demo; no wearable. |
| 81 | @labstamil | reject | Tamil film and culture brand account; nothing on devices or voice. |
| 82 | @cryptodaaddy | reject | Crypto promo account (271k posts); the one voice post reads as a HeyElsa promo. |
| 84 | @joshconstine | fix | SignalFire, intro via Marwan Refaat; skeptical of wearables, no device or voice use quoted: enrich. |
| 87 | @arjundundoo | keep | Warmest VC here (FirstMark, met Carlo 12 days ago); daily Wispr Flow user; 378 followers. |
| 88 | @yabhishekhd | reject | Tech-news account with a press email; news, not personal device or voice use. |
| 89 | @zodchiii | reject | Pseudonymous curator; no device, voice or ring evidence at all. |
| 90 | @mrjlwilliams | reject | 3.5k followers; promotes Ultrahuman with an affiliate code (JW70); phone content. |
| 91 | @hjluks | reject | Orthopedics audience; the one wear quote is ambiguous ('wearing them'); no AI or voice use. |
| 92 | @nexusben | fix | 9to5Google editor in Carlo's map; reviews phones, no personal wear or voice quote: enrich. |
| 93 | @thedanielbudai | reject | Ecommerce agency owner, 1.3k followers; no device or voice use. |
| 94 | @sumanth_077 | reject | Sells growth for devtools (promo); no device, voice or ring evidence at all. |
| 95 | @borthwick | reject | betaworks backed Sandbar's pre-seed; he loves his Stream ring. Direct competitor tie, unflagged until now. |
| 96 | @perksverse | reject | Promo account ('Available for projects - DM'); no device or voice evidence. |
| 97 | @ditzikow | keep | NYC SPC partner; daily Wispr Flow user; hosted Sandbar at a demo night, no SPC investment found. |
| 98 | @matt503ea5sf9z5 | reject | Anonymous (3.2k followers, location 'Hallmark Channel Movie'); no device or voice use. |
| 99 | @minafahmi | reject | Sandbar co-founder and CEO (the flag says employee): a direct competitor. |
| 101 | @himmelmeer22 | reject | Hangzhou, China (location field); 420 followers. |
| 102 | @kodjima33 | reject | Omi founder (config, map): a direct competitor. Flagged in scores; the candidates row has no flag. |
| 103 | @sarthaksavvy | reject | No device or voice use; skeptical of AI gadgets; base unknown. |

**Handles.** Every handle checks out on the official API, and none is a parody. The problems are of other kinds: a
backup account (@omoreyy___), anonymous or pseudonymous accounts (@matt503ea5sf9z5, @zodchiii, @cocktailpeanut, @shl0ms),
a blog feed (@mommypotamus) and a brand account (@labstamil).

**Category mix of the 38 keep-or-fix names:** 11 media, 11 X voices, 9 founders or angels, and 7 VCs. Two of the VCs are
warm: @arjundundoo (FirstMark, met 12 days ago) and @aunder (Adverb, an intro through Adrian Aoun). @sashakaletsky and
@joshconstine are warm too, but they have no wear evidence yet.

## Mistakes that repeat in run 30

1. **Voice-dictation founders carry no flag.** Six are in the top 100: @huang_song_ (Typeless), @priymrj (TypeVoice),
   @attacomsian (Flit), @dominikmartn (Aside), @kai_brokering (VoiceOS, also noted in run 14) and @bran_don_gell (Every,
   which sells Monologue). They sell what Wispr Flow sells, and Wispr Flow is on the brief's list. **Guard added**
   (`classify.builds_voice_rival`): when the summary or bio names the person as the builder of a dictation app ("founder of
   Flit, a Mac dictation app"), the guard sets the competitor flag in Wispr Flow's category. Users of such apps are left
   alone. The guard fires on 4 of the 602 classified people and on nobody wrongly. VoiceOS and Every are named only in
   config (`config/competitor_ties.yaml`, `people`), because their summaries do not say "dictation app". The flag is for
   Carlo to see; it never rejects anyone. Several of these founders could also be partners.
2. **Non-US people with us_based empty.** @damonchen's summary said "posts place him in China, not the US" (his posts show
   school runs in Shanghai), yet us_based stayed null because the model left the field empty. @rahul_j_mathur's location
   "BOM 🏡 BLR 🏢SFO" read as nothing, and @priymrj's "sf / blr" read as US. **Guards added:** an unhedged clause of the
   summary that places the person outside the US (`classify.summary_non_us`) now sets us_based=false when the location
   field and the map say nothing. It fires on 28 people: 27 were already non-US, and the new one is @damonchen. A hedged
   or split clause ("likely India-based", "split across India and SF") stays unknown. The Indian city codes BLR, BOM, NCR
   and HYD now count as places in the location pre-check, and SFO and JFK count as US. "sf / blr" is therefore split, not
   US. Not guarded: a country that shows up only in a post (@andilosing: "the documents you need in Germany to run an
   unprofitable business").
3. **Promo accounts still get past `discover.sells_posts`.** In the top 100: @kevin_parker_ai ("Influencer Marketing BIZ"),
   @perksverse ("Available for projects - DM"), @rezkhere (location "Try Seedance 2.0 →"), @linusekenstam ("collab@… for
   partnerships 🤝"), @sumanth_077 ("growth engines for AI-native devtools"), @garlotic ("real growth for creators &
   brands") and @cryptodaaddy. There is no guard yet, because `discover.py` belongs to the discovery agent. Phrases its
   owner could add: "influencer marketing", "available for projects", "for partnerships", "growth for … brands",
   "ambassador @", and a third-party "Try … →" call to action in the location field.
4. **Fund ties missed outside the `vc` category.** Betaworks is in Carlo's map as a Sandbar investor, and @borthwick's bio
   is "@betaworks". Two things stop the match in `score.py`: it only checks funds for people in the `vc` category, and it
   matches one-word fund names case-sensitively ("@betaworks" does not match "Betaworks"). The fix here is in config: a
   `people` entry for him. The owner of `score.py` should run `fund_matches` on every bio, and match @handles
   case-insensitively.
5. **Off-topic praise counts as love and accept.** @joannastern's love score rests on a car ("I drove the Xiaomi SU7 Max…
   I miss it every day") and an iPhone first look. @synthpotato's and @labstamil's scores come from game and film reviews.
   A prompt suggestion follows at the end of this section. This is not guarded: it is the model's judgment.
6. **Evidence that shows building or promoting is filed as use.** @dheemanthredy's only "voice use" is a demo of his own
   voice model, @viditb's is his own app's voice search, and @huang_song_'s is promotion of Typeless. This is the same
   mistake as run 14's "built a voice agent". @davidlinthicum's and @mrjlwilliams's competitor-product posts read as
   sponsored: an Ultrahuman affiliate code, JW70.
7. **Stale or partial competitor records.** @kodjima33 (Omi founder) is flagged in `scores.flags`, but his candidates row
   says competitor_flag=0. @minafahmi is filed as a Sandbar "employee", but he is its co-founder and CEO. @sabrinaa_ortiz's
   map role still says CNET, but she now reports for The Deep View.

**Prompt suggestions for `src/ring_wishlist/prompts/classify.md`** (not edited here):
- Love and accept evidence must be about a device, a wearable, a voice tool or an agent, not a car, a game or a film.
- Evidence of voice use must be the person using a tool. A demo or promotion of their own voice product does not count.
- When the posts place someone outside the US, fill in us_based. Do not only say so in the summary.
- Treat building a dictation or voice-input app as a competitor tie of kind "rival", in Wispr Flow's category.

The guards run on stored classifications with `reapply-guards` and need no LLM call. They were not applied to the live DB
in this review.

# Run 39 review

Reviewed 2026-09-29 with `python -m ring_wishlist.dossier <handle> --run 39`. The list is the top 100 of run 39 under the
dashboard's default filters (us_based not 0, status not rejected), minus the people already reviewed above: 41 new
entrants, checked for the same five things as run 30. Carlo's rule stands: rings may go outside his network, so "no path"
is never a reason to reject. The famous names were held to the brief's bar: fame is not evidence.

**Result: 8 keep, 8 fix, 25 reject.** The 25 rejects now have status `rejected` in the live DB, each with a
`QA run 39:` note, written with `app/data.py set_status`. No paid calls. Three web searches and one page fetch settled two ties:
[Forerunner holds 9.3% of Oura and plans to sell it at the IPO](https://dealroom.co/news/155264-ouras-ipo-set-to-raise-up-to-2-2b-with-forerunner-selling-its-entire-sta)
(it backed Oura's Series B and took a board seat, [VentureBeat](https://venturebeat.com/business/smart-ring-startup-oura-raises-28-million-from-googles-gradient-ventures-square-and-forerunner-ventures)),
and [Alexis Ohanian is an angel in Willow Voice](https://techcrunch.com/2025/11/12/willows-voice-keyboard-lets-you-type-across-all-your-ios-apps-and-actually-edit-what-you-said/).

| Rank | Handle | Verdict | Reason |
|---:|---|---|---|
| 4 | @snazzylabs | keep | Salt Lake City reviewer, 301k; wears an Apple Watch, bought an Oura Ring 5 (usage, no tie); no voice. |
| 5 | @alexfinn | keep | 476k AI creator; hours of ChatGPT Voice on walks through AirPods ('Ambient AI. Walk around.'); location unknown. |
| 28 | @clairevo | fix | SF host of How I AI, 68k; agent-heavy, but no worn device or voice use quoted: enrich. |
| 30 | @lenadroid | keep | AI devrel director, 24k; switched to voice-first dictation ('not going back'); US rests on a San Jose keynote. |
| 33 | @nickadobos | keep | Seattle, Browser Company prompt engineer, 55k; walks and 'yaps' to ChatGPT voice; no wearable. |
| 37 | @chongkydudut | reject | Pseudonymous crypto promoter; the wearable posts are Sleepagotchi shilling; no voice use. |
| 38 | @businessbarista | keep | Morning Brew co-founder, 316k; pays for and praises Wispr Flow (usage, not a tie); no wearable; location empty. |
| 45 | @jjenglert | reject | Rochester NY agent educator, 37k; no worn device or voice use in the record. |
| 51 | @exm7777 | reject | Pseudonymous agency account ('Machina'); one phone-dictation post; no device use; location unknown. |
| 54 | @nbtjacklyn | keep | Tech video host, Forbes 30u30; tried the Meta Ray-Ban Display, 'OBSESSED' with voice-memo replies; base unknown. |
| 55 | @danshipper | fix | NYC, CEO of Every, which sells Monologue (a Wispr Flow rival): flag missing. No quoted device or voice use. |
| 60 | @johncoogan | fix | Pasadena TBPN host; praised Meta Ray-Ban Display hardware; TBPN is OpenAI-owned (OpenAI builds devices): flag. |
| 61 | @alexandr_wang | reject | Meta's Chief AI Officer: the evidence is launches of Meta's own Muse and glasses; won't praise a rival. |
| 62 | @casey | reject | 1.87M, but no worn device or voice use: a laptop deal and one Quest 3 scan. Fame alone. |
| 65 | @zebird0 | keep | SF, ex-a16z speedrun; WHOOP every day for two years; stealth consumer-bio startup, not yet known. |
| 68 | @mikefutia | reject | Ecom-ads promo (location 'Join 600+ marketing teams'); no device or voice use. |
| 70 | @leerob | fix | Iowa, ML at SpaceXAI, 290k; the only evidence is one Slack voice-notes quip: enrich. |
| 71 | @thsottiaux | fix | OpenAI Codex lead, 761k; talks to ChatGPT all day, his employer's product; OpenAI builds devices: flag. |
| 75 | @quxiaoyin | reject | Real founder (Agentsky), but posts are agent-market takes and her own launches; no device or voice use. |
| 76 | @alexisohanian | fix | Florida, 776; dictates with Willow Voice but is a Willow angel (Wispr Flow rival): flag. No wearable. |
| 78 | @satyanadella | reject | Microsoft CEO: the only voice use is his own M365 Copilot; praising a startup's ring is implausible. |
| 80 | @coinmaster100x | reject | Crypto KOL and exchange ambassador (location 'Space'); paid-style promos; no device or voice use. |
| 81 | @minchoi | reject | AI news curator relaying others' posts, runs #ad posts; no first-hand device or voice use. |
| 82 | @ridark_eth | reject | Pseudonymous crypto and AI creator (location 'Ai'); no device or voice use. |
| 86 | @deedydas | reject | Menlo partner who led Wispr Flow's $280M round: direct competitor tie (flag correct). |
| 88 | @imyadav31 | reject | Sells posts ('Collabs: DM'); credit-card-points creator; one Ray-Ban Meta unboxing; base unknown. |
| 89 | @svpino | reject | Sells posts ('Collaborations' link as location); the only voice item is his own 2024 build demo. |
| 90 | @wheelieinvestor | reject | Pseudonymous trading persona promoting his own agent; no device or voice use. |
| 91 | @ahuja_priyank | reject | LA creator who sells posts ('DM for collabs'); the only voice post reads as a Typeless promo. |
| 93 | @charmiekapoor | fix | Designer (ex-Razorpay, Microsoft); bought an Ultrahuman ring and panned it in public; base and voice use unknown. |
| 94 | @josethevrtech | reject | 4.8k followers; VR sysadmin posting on glasses form factors; no worn device or voice use. |
| 95 | @teslaconomics | reject | Pseudonymous Tesla and xAI fan account (location 'Mars'); no device or voice evidence. |
| 96 | @airkatakana | reject | Pseudonymous commentator on Japan and Korea (location 'EN/JP'); no device or voice use. |
| 97 | @ogdukeneurosurg | keep | Chapel Hill; Duke neurosurgeon and TV producer, 241k; Oura review ('100 other things I love'); no voice use. |
| 98 | @gary_pesola | fix | Warm SF VC (met Carlo); bio says investing at NVIDIA, map says Adverb Capital; one wearables mention. |
| 99 | @marcusyul | reject | 'Powering brands through AI' promo account, 553k; no device or voice use; only a congrats reply to Carlo. |
| 100 | @kellenxbt | reject | Crypto creator posting paid-looking promos and giveaways; no device or voice use. |
| 101 | @kirstenagreen | reject | Forerunner founder: the fund holds 9.3% of Oura, to be sold at the IPO. Direct competitor tie, unflagged. |
| 102 | @peterdiamandis | reject | Fame alone: two link-only topical posts, no worn device or voice use in the record. |
| 103 | @cb_doge | reject | Anonymous Musk-fan account (empty bio), 1.9M; no device or voice evidence. |
| 104 | @r0ck3t23 | reject | Anonymous two-year-old account summarizing Musk and Altman talks; no device or voice use. |

**What repeats in run 39.**
1. Crypto, anonymous, aggregator and post-selling accounts are 16 of the 25 rejects: the sells-posts signal lowers love (@svpino, @imyadav31) but removes nobody.
2. Ties the flag misses: Forerunner's Oura stake (@kirstenagreen), Every's Monologue for its CEO (@danshipper), Ohanian's Willow angel check, and OpenAI and Meta.
3. Praise of one's own employer's product counts as love and voice use (Copilot, Muse, ChatGPT); @quxiaoyin's path reads "a16z backed" as "same firm".

# Run 40 review

Reviewed 2026-09-29 with `python -m ring_wishlist.dossier <handle> --run 40`. The list is the 25 people in run 40's top 100
under the dashboard's default filters who had not been reviewed above, checked for the same five things as runs 30 and
39. Carlo's rule stands: quality over connection, so "no path" is never a reason to reject. The consistency rules of the
earlier reviews apply: competitor founders and employees, and VCs whose fund led a competitor's round, are rejected; so
are crypto, anonymous, aggregator and post-selling accounts, and famous people with no device or voice evidence or who
only praise their employer.

**Result: 1 keep, 2 fix, 22 reject.** The 22 rejects now have status `rejected` in the live DB, each with a
`QA run 40:` note, written with `app/data.py set_status`. No paid calls. Two web searches and one page fetch:
[Ben Thompson moved back to the US from Taiwan in 2025](https://stratechery.com/about/), and
[Saba Karim is ex-Techstars and now runs Favs](https://www.sabakarim.com/about), with no competitor tie found.

| Rank | Handle | Verdict | Reason |
|---:|---|---|---|
| 80 | @sabakarimm | keep | LA founder, ex-Techstars VC, 51k; smart-ring wearer, 'Voice. First.'; cheered WHOOP's screenless design and Sandbar's ring. |
| 81 | @damidefi | reject | Crypto and AI account ('NFA \| DYOR'), location empty; one agent news post; no device or voice use. |
| 82 | @johnkitover_ | reject | Self-declared Chicago mayoral candidate; spam-like campaign account (1.6k, ~100 posts/day, nine alt handles); one Bee review. |
| 83 | @vortex_promos | reject | AI and Web3 promo account (location 'Youtube'), operator unnamed; no device or voice use. |
| 85 | @andrewcurran_ | reject | AI news relayer, 100k, location only 'West coast'; no worn device or voice use in the record. |
| 86 | @kirillk_web3 | reject | Crypto and AI influencer (location 'Private Telegram'); one Meta-glasses post; no device or voice use. |
| 87 | @seeedstudio | reject | Shenzhen hardware brand, not a person; the one post sells its own reSpeaker Clip wearable. |
| 89 | @zevweb3 | reject | Crypto creator for OKX ('DM for collabs'); the smart-ring post is a Sleepagotchi promo; location 'Web3'. |
| 91 | @donnymac | reject | Qualcomm CMO, 7.5k; the one post promotes Snapdragon inside Meta devices, his employer's chips; no personal use. |
| 92 | @deantak | fix | Warm (16 touches, one meeting); GamesBeat editor covering Plaud, Xreal, Snap Specs; link-only posts: enrich for hands-on use. |
| 93 | @vkhosla | reject | Khosla Ventures led Rabbit's two 2023 rounds: direct competitor tie (flag correct); no worn device either. |
| 94 | @aakashgupta | reject | Product-growth newsletter seller, 331k; viral business threads; no worn device or voice use. |
| 95 | @benthompson | fix | US again since 2025 (left Taipei); agent-pilled, built his own agent; Vision Pro review missing from the record. |
| 96 | @successsparksx | reject | Nine-month-old 'AI Girl' persona, contact email in another name; templated listicles; its ring_wishlist post reads as promo. |
| 97 | @stevehou | reject | NYC research head, 60k; one post doubting AI pins; no worn device or voice use. |
| 98 | @anatolikopadze | reject | AI-lecture curator reposting others' content, 159k, location empty; no device or voice use. |
| 99 | @avischiffmann | reject | Founder of Friend (bio links friend.com): direct competitor (flag correct). |
| 100 | @sairahul1 | reject | AI listicle curator ('masterclass' threads), 149k, location empty; no device or voice use. |
| 102 | @joherkhan | reject | Oakland founder, 6.3k; texts a family agent; no worn device or voice use; curating a tech-flops exhibit. |
| 103 | @trackyourheart | reject | Anonymous cardiology-education account; its 'ring' and 'OMI' hits are medical terms; no device or voice use. |
| 105 | @ai_sentience | reject | Pseudonymous ('Alan Mathison', location 'The Singularity'), 4.3k; shares a local Wispr Flow clone, no own use. |
| 106 | @10x_er | reject | Anonymous persona ('10x Engineer', location 'remote'); one screen-free wish post; no device or voice use. |
| 107 | @randomrecruiter | reject | Pseudonymous recruiter-meme persona (@juicebox_work), 63k; one agent joke; no device or voice use. |
| 108 | @hubermanlab | reject | 1.9M, DMs closed; tried neurostim prototypes, no daily wearable or voice use; device mentions are sponsor reads. |
| 109 | @p1nosaur | reject | SF founder of solderable (AI PCB design), 4.3k; hires for AI-wearable PCBs; no worn device or voice use. |

**What repeats in run 40.**
1. Crypto, anonymous, persona, aggregator and brand accounts are 13 of the 22 rejects; a campaign account (@johnkitover_) joins them.
2. Both competitor flags were right (Khosla Ventures for Rabbit, Schiffmann for Friend) and no unflagged tie turned up.
   Employer praise again counts as topic evidence (@donnymac on Snapdragon). @sabakarimm praised Sandbar's voice ring in
   March: praise, not a tie, but a pitch to her should expect the comparison.
3. Keyword noise still ranks people: @trackyourheart's 'ring' and 'OMI' are cardiology terms, and @seeedstudio is a
   company. Two real fits need enrichment, not rejection: @benthompson (US again since 2025) and @deantak (warm, media).
4. After these rejects, 22 people nobody has reviewed backfill ranks 79 to 100 of the default view, among them
   @theroaringkitty, @marionawfal, @jeffbezos and a row with an empty handle (candidate 2588, Stephen Hamel).

## Run 42 check (lead)

| # | Handle | Verdict | Reason |
|---|---|---|---|
| 65 | @gssahni | keep | CMO of Altimetrik (NJ), wears an Oura ring daily; small audience, so last on the list. |

## Run 54 review (lead): 24 people who newly met the bar after steps A and B

Step A fetched posts for 75 vetted people never enriched (person, US or unknown, contactable; 52 more are map contacts with
no X account, so nothing to fetch). Step B searched devices and voice tools only for 130 people whose first evidence search
found nothing. 24 then met the bar. Each was checked against the quoted evidence: 15 kept, 9 rejected with a
`QA run 54:` note.

| # | Handle | Verdict | Reason |
|---|---|---|---|
| 3 | @jewiss | keep | Tech writer and NRV partner, 50k; Oura Ring 5 and Apple Watch; location "UK & Tampa" (US part-time), intro via Sabrina Ortiz. |
| 7 | @danshipper | keep | CEO of Every, 142k, NYC; dictates daily and leaves voice mode on while writing. |
| 8 | @brianroemmele | reject | Building his own voice device (GrokPod), a rival; location unclear. |
| 16 | @michaelmiraflor | keep | LA/NYC strategist, 36k; Ray-Ban Meta captures, Whoop, voice notes. |
| 25 | @jesselyu | reject | Founder and CEO of Rabbit, a competitor (flag correct). |
| 34 | @sandraleow | keep | Creative technologist, 46k; talks to her agent by voice, pipes her Whoop into it; location unknown. |
| 36 | @illscience | keep | a16z AI apps investor, SF; transcribes his kitchen with a voice setup; intro via Daisy Zhao. |
| 39 | @alexeheath | keep | Host of Sources (ex-Verge), 484k; writes by dictating in Wispr; covers AI rings and wants one. |
| 40 | @chriswelch | keep | Bloomberg tech reviewer (ex-Verge), Brooklyn; reviews earbuds and wearables; Carlo knows him. |
| 46 | @mhdfaran | reject | AI-tools curator; the one voice post is a tool plug, not his own use. |
| 53 | @flowersslop | reject | Anonymous account. |
| 60 | @maxmarchione | keep | Founder of Superpower, 35k; Apple Watch to leave the phone, Oura; intro via Roberto Veronese. |
| 61 | @aunysillyme | reject | A text chat with Claude is not voice use; brand ambassador and consultant. |
| 62 | @shishirshelke1 | keep | Tech creator, 28k; wears AirPods Pro daily; location unknown, small fit. |
| 63 | @wesroth | keep | AI news creator, San Diego; keeps ChatGPT voice mode open for hours. |
| 65 | @jasonhowell | keep | Host of AI Inside and Android Faithful, Petaluma; reviews display glasses; Carlo knows him. |
| 68 | @alexrkonrad | reject | The only evidence is his wife dictating with Wispr Flow, not him. |
| 72 | @creatine_cycle | keep | MOTS podcast host, SF; AirPods at the gym, Oura sleep scores. |
| 73 | @kanavtwt | keep | Engineer and creator, 20k; set up an assistant entirely by voice; location unknown, weak fit. |
| 75 | @futurebylea | reject | Sells promotion ('4collabs' in bio); posts are mostly tool promotions. |
| 77 | @joeheadhunter | reject | Evidence is "add voice" to a generated video, not use of a voice tool. |
| 80 | @evankirstel | keep | B2B tech TV host, Boston, 385k; dictates half his day; enterprise audience. |
| 81 | @alexia | keep | Dream Machine VC, 147k; posts her voice-to-text slips (weak); Carlo knows her. |
| 87 | @gauravvohra | reject | Advises Wispr Flow (ties to Sesame and Limitless too); weak device fit. |

## 30 September: competitor ties are flagged, not removed

Federico's rule, from the brief: anyone tied to a competitor is flagged, never removed. Sixteen people rejected above for the tie itself are back as candidates, with their old note kept in the status note: @ericmigi, @jesselyu, @attacomsian, @dominikmartn, @kevintwohy, @borthwick, @minafahmi, @kodjima33, @deedydas, @aaravgarg, @tankots, @gauravvohra, @vkhosla, @avischiffmann, @brianroemmele, @kirstenagreen. They rank by their score like anyone else and carry the flag; those without device evidence or a way to contact them stay below the bar. Rejections for other reasons stay: @venturetwins (no evidence), @alexandr_wang (won't praise a rival; Meta is not on the brief's list), @mrjlwilliams (affiliate promotion). The ties this review found but the flag missed are now in `config/competitor_ties.yaml`: @kirstenagreen (Oura), @alexisohanian (Willow Voice), @danshipper (Monologue), @brianroemmele (GrokPod).

## Run 71 review (three parallel reviewers): 98 people who newly met the bar after step C

Step C searched for people's own posts about wearing a device or using a voice tool (the 20 `evidence` queries). 98
new people met the bar on run 71. Each was checked against their own posts: 58 kept, 29 rejected with a `QA run 71:`
note, 11 marked not in the US (us_based set to 0, a `QA run 71: not in the US` note; they stay visible with US only off).
Competitor ties are flagged, not rejected (30 Sep); one tie the flag missed was added to config/competitor_ties.yaml
(@_seanliu, formerly at Sesame). Ranks are run 71's.

| # | Handle | Verdict | Reason |
|---|---|---|---|
| 3 | @mweinbach | keep | Max Weinbach, Creative Strategies analyst, Connecticut, 307k; built a Codex/Claude skill on Apple dictation, wears a Whoop, asks Siri through AirPods, uses ChatGPT Live voice. Caveat: one #ad thread (23 Jul, health-data app). |
| 7 | @nathie | keep | Nathaniel de Jong, VR/AR YouTuber (800k subs), 67k on X; has worn Meta Ray-Ban Display since launch day (Sep 2025), asks Meta AI by voice, still testing it in May 2026; record location empty, his LinkedIn says US (Dutch-born). |
| 9 | @jameygannon | keep | Jamey Gannon, Brooklyn AI creative director, 32k; wears Meta glasses in public and films with them (Jul-Aug 2026), 'wispr flow maxxing'; asked 'is it going to be a ring?'. No tie. |
| 10 | @brianroemmele | keep | Brian Roemmele, 489k, voice-first commentator; uses Grok voice mode daily through his own GrokPod (Apple Watch as the brain). TIE (flag correct): building GrokPod, a rival voice device. Location 'transcendence' (unknown). Caveat: 26 Sep post asks for donations to a GrokPod crypto 'CA address'. |
| 13 | @_simonsmith | not US | Klick Health EVP Generative AI, 5.4k; real ChatGPT Voice with AirPods use, owns Meta Ray-Bans; but based in Toronto, Canada (Klick HQ; LinkedIn and press), record location empty. |
| 18 | @shantanugoel | not US | Engineer (Trade Desk, ex-Google/Qualcomm), 17k; own Meta glasses and Fitbit Air, but posts place him in India (Hyderabad stories, Gadkari E20 fuel jokes, Zoho gov-infra, Hindi posts); location field is a URL. |
| 21 | @thdxr | keep | Dax Raad, builds OpenCode at Anomaly, Miami, 175k devs; dictates all prompts into OpenCode with local Parakeet models ('completely changed things for me'), asks Siri through his AirPods. Possible light tie: team's own dictation tool 'hex (how we do voice prompting)' (19 Jul), open source, not a listed rival. |
| 23 | @ericmigi | keep | Pebble founder, Palo Alto, 41k; uses his Index 01 voice ring many times a day and dictates with Wispr Flow. Pebble tie flagged (flag text says 'not in the brief's list', but Pebble is on it). |
| 26 | @alliekmiller | keep | Allie K. Miller, NYC AI-business voice, 110k on X; dictates nearly all prompts with Wispr Flow (fn key), dictates to Otter on 40-min walks, EarPods; talks to Claude Code by voice. |
| 27 | @omooretweets | keep | Olivia Moore, a16z consumer partner, 153k, SF per her posts (location field empty); talks to Claude by voice mode, voice-dictates to agents, 'fully converted' to wearables, uses Oura APIs. TIES (flag correct): a16z-backed Sesame and Limitless. Intro via Daisy Zhao. |
| 29 | @milesdeutscher | reject | AI and crypto listicle account, 682k: 'bookmark this' threads, trading-bot prompts and guide plugs; the voice-prompting posts sit among the plugs. Australian (Melbourne) per press; record location empty. |
| 30 | @ashwinreads | not US | MD, founder of GLP-1 Digest, 12k; real Oura + voice-mode agent use, but posts point to the UK (plans Waitrose grocery access for his agent, 'cheesed off'); also writes for Every (Monologue maker), unflagged weak tie. |
| 32 | @pbteja1998 | not US | Bhanu Teja, SiteGPT/Squad founder, 59k; own Whoop piped into OpenClaw (the agent chat is via Telegram text). Lives in India: 6 Jun post counts net worth in rupees (Rs 5Cr) and $600/month costs 'because of where I'm currently living'. |
| 33 | @bilawalsidhu | keep | Bilawal Sidhu, ATX/SF creator (ex-Google Maps AR), 119k; built an AR hack on his own Meta Ray-Bans; talks to his agent and his God's Eye View map by voice. |
| 39 | @mikepat711 | reject | Pseudonymous meme persona ('Mike P', bio 'Shitposting, PhD. Meme Researcher', Tesla referral link); voice use is real (Grok voice mode, voice-prompting Muse), but no identifiable person; location unknown. |
| 40 | @jesselyu | keep | Jesse Lyu, founder/CEO of Rabbit, Los Angeles, 30k; relies on his r1/OS3 by voice for life and work, hands-free coding via DLAM. TIE (flag correct): Rabbit, and all device evidence is his own product. |
| 41 | @baconbrix | keep | Evan Bacon, SF engineer at SpaceXAI (ex-Expo), 63k; wears Meta Ray-Bans (put Codex pets on them); the Grok Voice posts are his employer's product, so voice use is weaker. No tie. |
| 42 | @theaaron | keep | Aaron Makelky, Casper WY, AI trainer and ex-teacher, 3.7k; dictates on iOS with Wispr Flow, uses Grok Bot voice mode and gave his Hermes agent a voice (OpenHome); small audience, no wearable. |
| 44 | @legendaryy | reject | Pseudonymous 'Legendary' (no real name), 52k; Oura and Apple Watch posts look real, but the feed is AI-news threads plus a #TriaTreasure crypto-card promo; no identifiable person. |
| 48 | @chiragbarjatya | not US | India-based fitness founder (bio: 'solve India's obesity problem'), 102k; real Whoop wearer and one voice-agent post, but posts are Indian civic and diet topics. |
| 50 | @ycocerious | not US | Punarv, founder of Nue Health (India), 3k; wears a Whoop, but 'the Bryan Johnson of India' and India launch posts place him in India. |
| 54 | @9mmsmg | reject | Pseudonymous political/humor account '9mmSMG', 262k, inflammatory immigration posts; Whoop and ChatGPT voice use real but no identifiable person; hostile to AI glasses. |
| 55 | @_seanliu | keep | Xiaoan (Sean) Liu, CU Boulder PhD (ex-Google XR), 5.7k, viral builds; runs his open-source VisionClaw agent on his own Ray-Ban Metas and talks to it; Boulder, CO. TIE MISSING: former Sesame (bio 'prev. Multimodal AI @Google @Sesame'). |
| 58 | @kingbootoshi | reject | Pseudonymous persona 'BOOTOSHI' with a cartoon character brand (Cyboorg), agency email, Discord giveaways; voice-prompts agents daily (25h of recordings), but no identifiable person; location unknown. |
| 59 | @coryetzkorn | keep | Cory Etzkorn, NYC founder of Soulmate (voice-first dating app), 5.7k; designs by walking Williamsburg with AirPods in a ChatGPT voice conversation, long voice chats daily. Small audience; Soulmate is not a device rival. |
| 61 | @basicappleguy | reject | Faceless 'Basic Apple Guy' persona (Apple renders and mockups), 420k; Apple Watch use is real but no identifiable person; posts point to Canada (Costco Canada tip, 'shovelling snow'). |
| 62 | @gabriel1 | keep | Gabriel, SF co-founder of Energy (computer-work agents, not a competitor), ex-OpenAI/Midjourney, 126k; prompts by voice for 10-15 minutes at a time, runs a local transcription model. |
| 64 | @vibemarketer_ | reject | Faceless 'J.B.' AI-marketing curator, 37k, location unknown; feed is 'Hormozi/Isenberg says' repackaging and tool plugs; the Wispr Flow post is a '6 months free, go grab it' plug. |
| 65 | @manosaie | keep | Manosai, NJ independent AI evaluator (ex-Figma, Google, Microsoft), 3k; leaves ChatGPT voice mode on for hours on walks, used Wispr Flow; small audience; follows Carlo. |
| 67 | @networkchuck | keep | NetworkChuck (Chuck Keith), Dallas tech YouTuber, 269k; built a phone line to his server to talk to Claude Code by voice, tried AirPods 5 at the Apple event. Weak: one voice project, sponsored YouTube videos. |
| 72 | @scotthickle | keep | Scott Hickle, cofounder/CEO of Throne (gut-health sensor), Austin, 10k; analysed 427 days of his own Whoop data. Note: his co-founder John Capodilupo co-founded Whoop (not on the brief's list, so no flag). |
| 73 | @zarazhangrui | keep | Zara Zhang, SF builder (Harvard '17), 89k; brain-dumps to AI in voice mode and builds realtime voice tools; no wearable. |
| 79 | @mattiapomelli | keep | Mattia, founder of Sleek Design, 16k; dictates tasks and posts with superwhisper, then installed Wispr Flow; location 'Nomad' (unknown); no wearable; posts mostly plug his own product. |
| 82 | @jacob_posel | keep | Jacob Posel, SF founder of HQ (AI context layer), 29.5k; walks around the house with AirPods in, talking to Codex voice mode as his orchestrator. |
| 86 | @coldemailchris | keep | Christian, LA founder and CEO of Pipeline (outbound agency), 21k; 'Wispr Flow... I almost talk more than I type now' in a daily-tools list; recycled B2B listicle feed, weak fit. |
| 87 | @srsiskind | keep | Sarah Rose Siskind, NYC, CEO of HelloSci, science comedy writer, 4.6k; wore her Oura while giving birth (viral, 2.7k likes), dictates emails. |
| 88 | @danielfazio | keep | Daniel Fazio, cofounder of ListKit/Client Ascension, 17.7k B2B/info-product audience; wears a Whoop and had Claude analyse his Whoop data after buying an Eight Sleep. Location unknown; weak fit, crude tone. |
| 91 | @jamesoncamp | keep | James Camp, LA founder (2 exits, advisor to Stan), 95k; stopped typing, 766K words written by voice (Wispr Flow in his tool list). No tie. |
| 92 | @attacomsian | reject | Engagement-bait growth account ('drop your product link', 'I'll feature the top 3', reply counts) with tool plugs (Monid, TinyFish); voice evidence is his Flit launch thread; no wearable; location unknown. Tie flagged (Flit, a Wispr Flow rival). |
| 95 | @orangie | reject | Memecoin trader/YouTuber, NYC, 428k; bio link is a fomo referral, runs SOL/BNB giveaways, 'buy coins' feed. Whoop and Meta Ray-Ban Display use are real, but crypto shill account. |
| 99 | @fousey | keep | Yousef Erakat (Fousey), creator, 891k; swapped his Oura ring for a Google Fitbit Air (May 2026); one Feastables giveaway and a self-admitted history of 'negative PR'; record location empty (US creator). |
| 100 | @juampitech | not US | Growth at Firecrawl, 10.5k; builds on his own Ray-Ban Meta, but location field is the Argentine flag and posts say 'As an Argentine' and 'my local currency'. |
| 101 | @bscholl | keep | Blake Scholl, founder/CEO of Boom Supersonic, Denver, 201k; wears an Oura ('Oura thinks I slept through a meeting'), uses voice text, Apple Watch opinions. Aviation audience, weak topical fit. |
| 102 | @homegymcoop | keep | Coop Mitchell (Garage Gym Reviews), 37k; dictates with Wispr Flow at his treadmill desk through a Shure mic; fitness audience; location empty. |
| 103 | @dominikmartn | not US | Designer and founder of Aside (dictation app, tie flagged), 3.8k; used Wispr Flow, now dictates with Aside; hiring posts ask for 'europe timezone overlap', so based in Europe; no wearable. |
| 104 | @heshie | keep | Heshie Brody, health-tech CTO, Brooklyn, 4.6k; dictates with Wispr daily (with a Wispr pedal), talks to his computer to run agents; cancelled Whoop in May. Small audience. |
| 106 | @tankots | reject | Wispr Flow CEO, 36k; every voice post is company marketing (Porsche contest, launches), no first-person use or worn device in the record. Tie flagged correctly; the reject is for evidence. |
| 107 | @hraness | keep | Builder at Zo Computer (ex-Stripe/Venmo), 12.9k; swapped his keyboard for a mic and Wispr Flow; NYC (subway posts), hosts agent demo nights. Name not on profile, but a public speaker, not an anon. |
| 109 | @typelessdotcom | reject | Brand account of the Typeless dictation app (bio 'Founder & CEO @huang_song_'), SF; product replies, no person behind it. |
| 114 | @protosphinx | keep | Shashank Dixit, Deskera founder, 65k, SF per Crunchbase (record location empty); uses a Pocket voice recorder daily ('a dedicated device is just easier'); meme-heavy feed. Pocket is a Plaud-like rival he praises, no stake stated. |
| 115 | @isausmanov | reject | Isa, 1.5k; the only voice evidence is dictating into his own hackathon 'agentic phone' (a product demo); location split SF / Munich; no wearable. |
| 116 | @theo | keep | Theo Browne, CEO of T3, YouTuber, SF, 395k devs; wears an Apple Watch (heart-rate alert post, a joke). Weak: says he is 'so, so tired of voice-to-text' and prefers typing. |
| 117 | @kevintwohy | keep | Kevin Twohy, California design-studio founder, 15k; designed Sandbar's Stream ring interface and says he likes wearing it more and more. Sandbar tie flagged correctly. |
| 118 | @gothburz | reject | Peter Girnus, Austin satirist, 193k; the Whoop line is inside a first-person satire persona (HELOC to buy SpaceX), like all his 'I am the...' posts; no real device use. |
| 119 | @realmeetkevin | keep | Kevin Paffrath (Meet Kevin), Ventura County CA, 495k, 2M YouTube; films with his own Meta glasses ('not sponsored'). Markets/politics audience, weak fit. |
| 122 | @beffjezos | keep | Guillaume Verdon (Beff Jezos), Extropic founder, 305k; 'wispr-flow-pilled... can't go back' (Feb 2026, after seeing an ad); location a joke ('Dyson Swarm'), US company; one voice post. |
| 123 | @jeffreytucker | reject | Brownstone president, 278k; 'the voice ring[s] in my head' is a remembered slogan, not a device; no device or voice-tool use. |
| 124 | @onlysabrena | reject | Adult-content persona (OnlyFans link in bio, 'ur twitter gf misses you' bait posts), location 'ur wifi range'; Oura posts are real but the account is engagement bait. |
| 125 | @mbym | keep | Mark Moriarty, Stripe corp strategy, SF (from Cork), 1.8k; wears Meta Ray-Bans as an ambient audio player, bookmarks podcasts with AirPods; tiny audience. |
| 126 | @armanddoma | keep | Armand Domalewski, SF podcast co-host and USF AI grad student, 85k; wine tanks his Oura sleep score, uses the Muse agent; political audience, one device post. |
| 127 | @justjake | reject | Jake Cooper, Railway founder, SF, 32k; evidence unclear: says life is better after ditching the Apple Watch, and '"Yea I love my Whoop"' is a quoted caption on an image, not clearly his own use. |
| 128 | @eodhappycaptain | reject | Pseudonymous 'Happy Captain' Army humor account, 63k, no real name; the only device is a six-year-old Garmin; viral meme posts, no voice use. |
| 130 | @thegregyang | keep | Greg Yang, xAI cofounder (on leave, fighting Lyme), 95k; on his second Oura ring, tracks HRV and resting HR; location unknown; xAI not on the brief's list. |
| 131 | @willcb | reject | Will Brown, Prime Intellect researcher, SF/NYC, 51k; only evidence is a joke ('running Kimi K3 on my Oura Ring rn'); no real device or voice use. |
| 132 | @brianedean | not US | Brian Dean (Backlinko, Exploding Topics), 125k; Oura wearer, but lives in Berlin, Germany per web profiles and interviews; record location empty. |
| 134 | @katiemiller | keep | Katie Miller, DC podcaster, 252k; her Whoop tracked her pregnancy sleep; political audience, her AI posts attack ChatGPT and praise Grok. |
| 137 | @bproudpatriot | not US | UK political account ('Benjamin' with UK flag), 955k; Apple Watch story is a letter to the NHS; posts on UK immigration and Manchester; inflammatory. |
| 138 | @jerryteixeira | keep | Jerry Teixeira, USA home-strength creator (46, jiu-jitsu), 81k; tracks recovery with his Oura; sells his own program, fitness audience, weak topical fit. |
| 139 | @hayleycaronia | keep | Hayley Caronia, Florida political commentator (Bongino Report), 67k; Oura logged kneeling at mass as pilates, AirPods on the treadmill; bio links a shoutout.fans page. |
| 140 | @michelletandler | keep | Michelle Tandler, NYC political commentator, 98k; checks her Oura ring app (post criticises its gender field). Politics audience, weak fit. |
| 143 | @benaveryisgood | reject | Comedian-podcaster, 65k; the one Meta-glasses post is a joke caption, not use; shock-humor feed with antisemitic jokes ('fight against philosemitism'), a brand risk; location 'Lemonparty'. |
| 145 | @owl_posting | keep | Owl Posting biotech writer and podcaster (pen name 'owl'), OpenAI Foundation health data, 18k; wears an Oura, tried 8sleep; SF/NYC. |
| 146 | @dreinstein9999 | reject | Pseudonymous crypto airdrop account ('Vitalik Einstein'), 41k; the Whoop post thanks @useTria for its crypto card (a plug); presale/whitelist shilling; posts hint Germany. |
| 147 | @brianstelter | keep | Brian Stelter, CNN chief media analyst, NYC, 1M; 'I love my @WHOOP'; posts are media news, weak topical fit. |
| 148 | @hunteryharris | keep | Hunter Harris, NYC, Hung Up film and TV newsletter and podcasts, 154k; Oura logged Hamnet as sleep; pop-culture audience, one device post. |
| 149 | @minafahmi | keep | Mina Fahmi, Sandbar co-founder, NYC, 7k; speaks into the Stream ring daily. TIE (flag correct): Sandbar, and the device is his own product. |
| 150 | @pegasuswitchvt | reject | Faceless 18+ VTuber persona, 11k, sponsor code in bio (GamerSupps); one Apple Watch heart-rate mention. |
| 151 | @daltonc | keep | Dalton Caldwell, SF, Standard Capital, YC partner emeritus, 346k; uses his Meta Ray-Bans instead of AirPods for music and calls (Oct 2025). |
| 153 | @nicholasmoreano | keep | Nicholas Moreano, Chicago Bears beat reporter, 28.7k; films his coverage on his own Ray-Ban Metas. Email in location field; football audience, weak fit. |
| 154 | @deedydas | keep | Deedy Das, Menlo partner, SF, 256k; Wispr is his 'daily driver' for replies on walks home. Wispr Flow investor tie flagged correctly (led the $280M round). |
| 155 | @megannyvold | keep | Megan Nyvold, humor creator under her own name, 14.6k; wears an Oura (Mar-Aug 2026), wore a Friend pendant (use, not a tie), feeds her wearables to chat; US (Global Entry). |
| 156 | @kirstenkrull | keep | Kirsten Krull, Minnesota Wild in-game host and podcast co-host, 26k; shares her Oura data from a game. Sports audience, weak fit. |
| 157 | @aaravgarg | reject | 20-year-old ex-Omi builder, 2.2k; the only use is a one-off demo tweet sent from the Omi device while he worked there; record holds 2 posts. Omi tie flagged, but it should say former (he left in Sep 2026). |
| 158 | @ruthiepolinsky | keep | Ruthie Polinsky, Chicago CHSN sports host and reporter, 21k; her Oura tells her to chill after Bears and Bulls games; one device post, sports audience. |
| 162 | @notsoeasymoney | reject | Pseudonymous crypto trading streamer 'Easy', 151k; fomo referral link, memecoin explainers; the Whoop post plugs a Moodring crypto device. |
| 164 | @charlieledgerx | reject | One-year-old joke persona ('Charlie Ledger'), 1.8k, sex-joke engagement posts; Whoop jokes; no identifiable person. |
| 165 | @gauravvohra | reject | No own use: the voice post is about founders' voice notes that Apple transcribes for him; bio 'advisor @wisprflow' is the tie (flagged), not use. SF, 6.6k; Carlo knows him. |
| 166 | @cybersahu | not US | Founder/CEO of Dreamspan, Bangalore (made in Bangalore, Rs 100 crore deal); says his Whoop is in a drawer and his Oura on the nightstand, i.e. he stopped wearing them. |
| 167 | @bigsargesportz | keep | 'Big Sarge', Houston sports reporter and podcast co-host (Big Sarge Media LLC), 11k; wears Meta glasses at Texans and Rockets pressers, plans to cover the season with them; real name not in record, weak fit. |
| 168 | @itsredic | keep | LA TCG creator under a stage name (REDIC, skits on IG/TikTok), Slabz video editor, 8.5k; wears Gen 2 Meta glasses to card shows. Weak: collector audience, GM posts, Slabz referral link. |
| 169 | @saracivian | keep | Sara Civian, Boston hockey journalist (Bleacher Report, Substack, podcast), 85k; wears an Oura (charger, app-update posts). Email in bio; sports audience, weak fit. |
| 170 | @ivan_nomadz | reject | Ivan, CEO of NOMADZ (Solana travel app), 10k; real Whoop and Ray-Ban Meta use, but the feed is app and SOL-staking promos; posts from Dubai, Lisbon and Prospera, no US base. |
| 171 | @thickles__ | reject | Streamer persona that sells sponsorship ('Powered by @TheRogueEnergy', '@comfrt ambassador', sponsored Zenni shoutout) and asks fans for an Oura 5 on her Throne wishlist; adult-leaning audience. |
| 172 | @emerome | keep | em (emerome), LA streamer/YouTuber (Canadian in LA), 35k; tracks HRV on her own Oura. Business email in bio; gaming audience, finds Meta glasses invasive; weak fit. |
| 173 | @ryanhammer09 | keep | Ryan Hammer, college-hoops podcaster (Blue Blooded), NJ, 77k, CAA-managed; wears an Oura ring daily; sports audience, weak topical fit. |
| 174 | @growing_daniel | reject | Pseudonymous shitpost account ('Daniel', joke bio and link), 450k SF; Oura and Whoop posts are sleep jokes, and he mocks Oura's company; no identifiable person. |
| 175 | @lydiamoynihan | keep | Lydia Moynihan, NY Post columnist, NYC, 69k; wears an Oura (sleep score, strain posts). Politics audience, weak fit. |
| 177 | @stocktrader_max | reject | Pseudonymous stock-tips account, 29k, funnelling to his Whop group and pumping $HIMS; Whoop use is real but the persona is a funnel. |
