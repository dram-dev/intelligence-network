# intelligence-network

**Live site:** https://dram-dev.github.io/intelligence-network/ · **Bot:** [@intelligence_network_bot](https://t.me/intelligence_network_bot) ·
[Privacy](https://dram-dev.github.io/intelligence-network/privacy.html) · [Terms](https://dram-dev.github.io/intelligence-network/terms.html)

A **decentralized sensor network** with a daily digest. Anyone with Telegram
is a sensor: what they report is checked against their neighbors and against
official sources, corroborated readings become events, contributors earn
trust, and the whole thing rolls up into a Google Doc every morning.

**Illinois** is the ground — resolved to counties, ZIPs and ZIP+4, not more
states. Eight topics ship: **weather, soil, water, agriculture, air, quake,
nature, markets** — each a YAML topic pack, and the same grammar reads all of
them in one message (`hail quarter; soil temp 55; corn at dent`). A ninth topic
is another file. Three further categories — **landuse, emergency, research** —
exist only in the reading list (`config/news_topics.yaml`): nobody reports a
permit hearing, but triage needs somewhere to file one.

The public face is a static site (`docs/`, GitHub Pages) with the join and
subscription instructions, a live data explorer (network graph, county mesh,
activity, events), the Drive digest links, collaboration entry points and a
public-data catalog. It rebuilds from the network's own anonymised snapshot
after every daily run.

```
   people on Telegram ─┐                                  ┌─▶ Telegram pushes (warnings, events,
   automated sensors ──┼──▶ common data language ──▶ network engine ─┤    reports — by category × area)
   NWS alerts / LSR ───┤     (parse · normalize)   (corroborate ·    ├─▶ /near /alerts /latest (pull)
   ASOS stations ──────┘                            trust · events)  └─▶ daily digest → Google Drive
```

Sibling projects: [pc-insurance-digest](https://github.com/dram-dev/pc-insurance-digest)
and [macro-ai-digest](https://github.com/dram-dev/macro-ai-digest). This one
reuses their shared `digest-core` (DB base, ingest registry, LLM backends,
Telegram transport, the cross-digest run lock) and runs third in the same
overnight queue on the Mac mini.

## The idea, in one paragraph

A [mesonet](https://www.mesonet.org/) is worth more than its stations because
the stations check and fill in for each other. Here the stations are people
(plus the official feeds as very trusted sensors). Every reading has the same
shape — a **Signal**: metric, canonical value, where, when, who, and the
network's assessment of it — so a phone report of "golf-ball hail @62704-1234"
and an ASOS wind gust and an NWS Tornado Warning all land in one table and can
be compared. Two independent sensors agreeing within the metric's tolerance,
distance and time window corroborate each other (both ways — a first report
is upgraded the moment a neighbor agrees). Agreement with a station, a storm
report or an active warning settles it outright. Corroboration moves each
sensor's trust (shrunk toward a prior, so nobody is condemned by one bad
reading). Readings past a threshold open a county event whose score is
`severity × corroboration × trust × official-agreement`; only verified events
are pushed. The digest reports the events and the official picture — and the
network's own vitals: coverage, corroboration rate, who contributed, and which
counties need sensors.

## Talking to the bot

Just type what you see. The grammar is one reading per clause:

```
<metric> <value>[unit] [@<location>] [at <time> | <N>m ago] [#tag] [-- note]

rain 1.25in                      hail quarter @62704-1234
gust 62 mph @cook                temp 91F at 3:15pm
tornado @sangamon -- on the ground west of town
1.5 inches of rain; gusts 45     (several readings: ';' or newlines)
```

* bare numbers take the metric's default unit (inches / mph / °F — what
  people type here); named hail sizes follow the NWS chart
* flag reports are readings by themselves: `flooding`, `trees down`, `power out`
* `@` sets the location (ZIP, ZIP+4, county, `lat,lon`, or a place name);
  with no `@`, your home applies (`/home 62704-1234` or share your location)
* plain sentences ("golf-ball hail here 5 min ago") go through the local LLM
* automated sensors post JSON: `/signal {"metric":"rain_mm","value":1.2,"unit":"in","location":"62704"}`

Commands: `/join [code]` · `/home` · `/me` · `/near [place] [6h]` ·
`/alerts [county]` · `/latest` · `/network` · `/topics` ·
`/subscribe <category> [area]` · `/unsubscribe …|all` · `/subs` ·
`/digest you@example.com` · `/privacy` · `/forget confirm` (deletes your record, readings,
subscriptions and e-mail) · admin: `/admin stats|sensors|ban|unban|trust|broadcast`.

Other people only ever see a contributor as a pseudonymous handle (`s-3f9a1`) with a ZIP code and
county — never a name, username, ZIP+4 or coordinates (report pushes, the digest, the site).

The bot answers every reading with what it understood, how it compares with
nearby sensors / official sources, and your trust.

### Subscriptions

`category × area`. Categories come from the topic pack:

| category | what you get |
|---|---|
| `weather.warnings` | NWS Severe/Extreme alerts (tornado, severe t-storm, flash flood…) |
| `weather.alerts` | every NWS alert (watches, advisories, statements) |
| `weather.events` | network-detected, *verified* events near you |
| `weather.reports` | every accepted contribution near you (raw) |
| `weather.digest` | the daily digest link when it lands (08:00) |

Areas form a hierarchy — `il` ⊃ `il.cook` ⊃ `il.zip.60601` ⊃
`il.zip.60601-2001` — type them as `il`, `cook`, `60601`, `60601-2001`.
Default is your home county. Each push is delivered once per chat.

Warnings and events are never quiet-hour suppressed (you opted in); the
digest ping honours `NOTIFY_QUIET_*`.

## The digest lives in Google Drive

Not on disk. `intelnet drive init` (one-time, browser consent) creates a Drive
folder with a fixed-link **Latest** doc; every daily run opens a folder named
for the date, writes `YYYY-MM-DD <network> digest` into it and rewrites Latest.
That day folder carries the digest in four formats — the Google Doc, a **PDF**
and **Word** file exported from it, and the raw **HTML** page — plus one
**CSV** per table (events, official alerts, county activity, station extremes,
contributors, reading list, network vitals), so the same numbers can be read,
printed or loaded into a spreadsheet. Re-publishing a day replaces the files in
place, so links keep working. People subscribe by
having the folder — anyone-with-link (`GDRIVE_PUBLIC_LINK=true`), or
`/digest you@example.com` which shares the folder with them (Google sends the
invitation) — plus the Telegram `digest` category for the link. Scope is
`drive.file`: the app sees only what it created.

The digest has two halves: the curated, scored roll-up (events ranked by
score, the NWS alert recap, station extremes, a triaged reading list) and the
network itself (vitals, contributions by county, contributor leaderboard,
sensors wanted). An optional LLM narrative opens it.

## Topic packs — the common data language

`config/topics/weather.yaml` declares every metric a sensor can report:
aliases, typed units and their conversion to the canonical unit, named sizes,
sanity range, agreement tolerance, neighbor radius / time window, event
thresholds with severities, the subscription categories, and how official
products (NWS alerts, storm-report types, station fields) map onto metrics.
The parser, corroboration engine, subscriptions and digest all read the pack.
A second topic (air quality, river stages, road conditions…) is another YAML
file — no code change (there's a test that proves it).

## Topics (the data language, one YAML each)

| pack | metrics | flags | reference feeds |
|---|---|---|---|
| **weather** | temp, dewpoint, RH, pressure, wind, gust, rain, snow, hail (NWS size words), visibility | tornado, funnel, flooding, wind damage, outage, lightning | NWS alerts · storm reports · ASOS stations |
| **soil** | moisture (VWC), soil temp, pH, organic matter, infiltration, compaction (psi), earthworms | erosion, cover crop, crusting | NRCS SCAN |
| **water** | stage, discharge, water temp, turbidity, dissolved O₂, pH, conductance, nitrate, well depth | ponding, tile running, fish kill, algal bloom, bank erosion | USGS gauges (~280 IL sites) |
| **agriculture** | corn stage (V/R words), soy stage, condition (NASS scale), yield, planting/harvest %, drought category (D0–D4) | drought stress, crop damage, pests, disease, field work | U.S. Drought Monitor |
| **air** | PM2.5, AQI, ozone | smoke, odor, open burning | — (AirNow/PurpleAir need keys) |

Every pack declares aliases, typed units → canonical, named sizes/levels,
sanity ranges, agreement tolerance, neighbor radius/window, event thresholds,
subscription channels and how official products map onto metrics. Aliases
are unique across packs (a test enforces it), so `temp`, `soil temp` and
`water temp` never collide.

## Reference feeds (all keyless)

| feed | sensor kind · trust | cadence | what |
|---|---|---|---|
| `nws_alerts` | authority · 1.0 | 5 min | api.weather.gov active alerts for IL, one row per county (SAME codes) |
| `iem_lsr` | official · 0.95 | 5 min | NWS Local Storm Reports (IEM), typed + magnitude + point |
| `iem_asos` | station · 0.9 | 60 min | 56 IL ASOS/AWOS stations: temp, dewpoint, RH, wind, gust, rain, pressure, visibility |
| `usgs_water` | station · 0.95 | 60 min | USGS NWIS instantaneous stage / discharge / water temp, ~280 IL sites |
| `nrcs_scan` | station · 0.9 | daily | NRCS SCAN soil moisture + temperature by depth (Illinois has one station, Mason) |
| `usdm` | authority · 1.0 | daily | U.S. Drought Monitor county D0–D4 coverage → drought category |
| `ams_grain` | official · 0.95 | 6 h | USDA AMS Market News (report 3192): spot corn, soybean and wheat basis and cash bids for twelve Illinois trading districts, each on the county that stands for it. Needs a free `USDA_MARS_KEY`; without one the feed stays quiet |
| `usgs_quake` | authority · 1.0 | 15 min | USGS earthquakes M2.0+ in and around Illinois (the box reaches the Wabash Valley and New Madrid zones), magnitude and community intensity, on the nearest county within 150 km |
| `news` | — | daily | 63 reading-list feeds → LLM-triaged: state agencies (IEPA, IDOA, IDNR, IDPH, IEMA), Extension + farmdoc, the Illinois farm press, river and lake groups, Illinois EPA air-quality Action Days (14 areas), Google News proxies, and the reading-list-only categories: land use and siting (data centres, CO2 pipelines, solar and wind, the Commerce Commission), emergency response, and research |
| `nws_statements` | — | daily | NWS Public Information Statements (damage surveys, storm totals) from LOT, ILX, DVN, LSX, PAH |

Geo tables (`config/geo/`) are vendored from the Census gazetteer + ZCTA→county
relationship file: 102 counties with centroids, 1,396 ZCTAs with centroid and
county. ZIP+4 has no free geocode, so the +4 is kept as the finest grouping /
subscription key and located at its ZIP5 centroid unless the sensor shares a
location. Point→county uses api.weather.gov (cached) with a nearest-centroid
fallback.

## Who it's for

| person | what they do here | where they start |
|---|---|---|
| **Contributor** (anyone with a phone) | reports readings; earns trust as neighbors agree | `/join`, `/home`, type a reading |
| **Grower / land manager** | soil, crop, tile and pond readings; subscribes to `soil.events`, `agriculture.events` for the county | `/subscribe *.events <county>` |
| **Spotter / emergency manager** | hail, wind, flooding with photos; wants warnings and verified events fast | `/subscribe warnings`, `/subscribe events` |
| **Subscriber** (reads, rarely reports) | the digest link each morning; alerts for the home county | `/subscribe digest`, `/digest you@…` |
| **Researcher / analyst** | pulls the public JSON snapshot; proposes datasets and topic packs | the site's *Public data* section, `docs/data/*.json` |
| **Steward** (admin) | vouches (`/admin trust`), suspends, broadcasts; runs `intelnet setup` | `.env`, `intelnet health` |

## Setup (Mac mini)

```bash
cd ~/Projects/intelligence-network
uv sync                                  # digest-core comes from ../pc-insurance-digest
cp .env.example .env                     # fill in TELEGRAM_BOT_TOKEN + TELEGRAM_ADMIN_CHAT_ID
uv run intelnet setup                    # the turn-on checklist, checked for real
uv run intelnet init-db
uv run intelnet watch                    # pull the reference feeds once
uv run intelnet signal "rain 0.4in @62704"   # contribute from the terminal
uv run intelnet near 62704
uv run intelnet digest --html /tmp/d.html    # preview the digest
uv run intelnet drive init               # Google OAuth (secrets/README.md) → folder + Latest doc
uv run intelnet pipeline --run-type manual   # full run → Drive → docs/ snapshot + site
uv run intelnet demo-seed --reset && uv run intelnet export --sample   # preview the site on a seeded fortnight
bash scripts/install_launchd.sh          # bot · watch (5 min) · daily 01:10 · notify 08:00
uv run intelnet health
```

## The site (docs/)

`site/index.fragment.html` is the page; `intelnet export` inlines the
anonymised snapshot (`docs/data/*.json`) into `docs/index.html`, which GitHub
Pages serves (Settings → Pages → Source: GitHub Actions; `.github/workflows/
pages.yml`). Set `SITE_AUTO_PUSH=true` and the daily run commits + pushes
`docs/` itself. Pages needs a public repo on the free plan.

The page carries: join + subscription builder (copies the exact `/subscribe`
command), a browser-side port of the grammar to try readings, the network
graph (topics · metrics · sensors · counties · events · feeds; corroboration
links), a county mesh, readings-per-day by topic, events ranked by score, the
Drive digest links, collaboration entry points (Telegram group, Discussions,
"propose a dataset / topic pack" issue templates, contributor board, how trust
works), and a filterable public-data + research catalog
(`config/public_sources.yaml`).

Telegram: make a **new** bot with @BotFather (one bot = one poll consumer),
put its token in `.env`, and your own chat id (from @userinfobot) as the admin.

## Schedule

| job | when | notes |
|---|---|---|
| `com.dr.intelnet.bot` | always (KeepAlive) | Telegram long-poll listener |
| `com.dr.intelnet.watch` | every 5 min | alerts + storm reports; stations hourly; pushes |
| `com.dr.intelnet.daily` | 01:10 | queued behind macro 01:00 and PC 01:05 on the shared run lock |
| `com.dr.intelnet.notify` | 08:00 | digest ping to `digest` subscribers |

## Layout

```
src/intelnet/
├── language.py     grammar → ParsedSignal; JSON form; cheatsheet (from the pack)
├── topics.py       topic packs (metrics, units, tolerances, thresholds, categories)
├── geo.py          counties / ZCTA / ZIP+4, geohash, area keys, NWS point lookup
├── models.py       Signal, Sensor
├── db.py           SQLite (sensors, signals, events, subscriptions, notify ledger, digests)
├── network.py      corroboration (both ways), trust, events + score, mesh, gaps
├── contrib.py      the contribution path: parse → store → assess → fan-out → ack
├── subscriptions.py  category × area matching; alert / event / report / digest pushes
├── bot.py          Telegram commands + listener
├── feeds/          nws_alerts · iem_lsr · iem_asos · usgs_water · nrcs_scan · usdm_drought
├── export.py       public JSON snapshot (anonymised) + docs/index.html build
├── demo.py         seeded fortnight through the real engine (sample data)
├── setup_check.py  `intelnet setup` — the turn-on checklist
├── ingest/         news (digest-core IngestorBase)
├── watch.py        5-minute reference sweep
├── digest.py       DigestModel + HTML / text renderers
├── gdrive.py       Drive folder + Latest doc + per-day folders (doc/pdf/docx/html/csv) + sharing
├── pipeline.py     daily run under the cross-digest lock; digest ping
├── llm.py          free-text parse, news triage, narrative (digest-core backends)
└── cli.py          intelnet …
```

Tests: `uv run pytest` — hermetic (temp DB; Telegram, Drive, LLMs and online
geo all forced off).
