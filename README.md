# intelligence-network

A **decentralized sensor network** with a daily digest. Anyone with Telegram
is a sensor: what they report is checked against their neighbours and against
official sources, corroborated readings become events, contributors earn
trust, and the whole thing rolls up into a Google Doc every morning.

Weather is the first topic and **Illinois** is the ground — resolved to
counties, ZIPs and ZIP+4, not more states. Any other topic is a YAML file
away (see *Topic packs*).

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
is upgraded the moment a neighbour agrees). Agreement with a station, a storm
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
`/digest you@example.com` · admin: `/admin stats|sensors|ban|unban|trust|broadcast`.

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
folder with a fixed-link **Latest** doc; every daily run adds
`YYYY-MM-DD <network> digest` and rewrites Latest. People subscribe by
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
sanity range, agreement tolerance, neighbour radius / time window, event
thresholds with severities, the subscription categories, and how official
products (NWS alerts, storm-report types, station fields) map onto metrics.
The parser, corroboration engine, subscriptions and digest all read the pack.
A second topic (air quality, river stages, road conditions…) is another YAML
file — no code change (there's a test that proves it).

## Reference feeds (all keyless)

| feed | sensor kind · trust | cadence | what |
|---|---|---|---|
| `nws_alerts` | authority · 1.0 | 5 min | api.weather.gov active alerts for IL, one row per county (SAME codes) |
| `iem_lsr` | official · 0.95 | 5 min | NWS Local Storm Reports (IEM), typed + magnitude + point |
| `iem_asos` | station · 0.9 | 60 min | 56 IL ASOS/AWOS stations: temp, dewpoint, RH, wind, gust, rain, pressure, visibility |
| `news` | — | daily | Google News / NWS-office RSS → LLM-triaged reading list |

Geo tables (`config/geo/`) are vendored from the Census gazetteer + ZCTA→county
relationship file: 102 counties with centroids, 1,396 ZCTAs with centroid and
county. ZIP+4 has no free geocode, so the +4 is kept as the finest grouping /
subscription key and located at its ZIP5 centroid unless the sensor shares a
location. Point→county uses api.weather.gov (cached) with a nearest-centroid
fallback.

## Setup (Mac mini)

```bash
cd ~/Projects/intelligence-network
uv sync                                  # digest-core comes from ../pc-insurance-digest
cp .env.example .env                     # fill in TELEGRAM_BOT_TOKEN + TELEGRAM_ADMIN_CHAT_ID
uv run intelnet init-db
uv run intelnet watch                    # pull the reference feeds once
uv run intelnet signal "rain 0.4in @62704"   # contribute from the terminal
uv run intelnet near 62704
uv run intelnet digest --html /tmp/d.html    # preview the digest
uv run intelnet drive init               # Google OAuth (secrets/README.md) → folder + Latest doc
uv run intelnet pipeline --run-type manual   # full run → Drive
bash scripts/install_launchd.sh          # bot · watch (5 min) · daily 01:10 · notify 08:00
uv run intelnet health
```

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
├── feeds/          nws_alerts · iem_lsr · iem_asos (ReferenceFeed base)
├── ingest/         news (digest-core IngestorBase)
├── watch.py        5-minute reference sweep
├── digest.py       DigestModel + HTML / text renderers
├── gdrive.py       Drive folder + Latest doc + daily docs + sharing
├── pipeline.py     daily run under the cross-digest lock; digest ping
├── llm.py          free-text parse, news triage, narrative (digest-core backends)
└── cli.py          intelnet …
```

Tests: `uv run pytest` — 107 hermetic tests (temp DB; Telegram, Drive, LLMs
and online geo all forced off).
