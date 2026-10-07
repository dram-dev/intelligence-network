# intelligence-network — Claude context

User nickname: **"the intelligence network"** / **"intelnet"**. Third digest
project after **pc-insurance-digest** ("PC Digest") and **macro-ai-digest**
("the macro digest"); both live as siblings under `~/Projects`.

## What this project is

A **decentralized sensor network** (mesonet idea, scaled to people) plus the
classic curated / score-based digest. Anyone on Telegram is a sensor; their
readings are expressed in a **common data language** (topic packs), checked
against neighbors and official feeds (corroboration → trust → events), and
pushed to subscribers by category × area. The daily digest is a **Google Doc
in a Drive folder** (not a local file / not Obsidian) that people subscribe
to. Weather first; **Illinois** ground at county / ZIP / ZIP+4 grain — go
finer, not to more states.

Design rules the user set (2026-09-15):
- Telegram is the primary push AND pull mechanism.
- The digest is a Google location people can subscribe to, not a filesystem.
- The network must work for any topic with a common data language — nothing
  weather-specific in code; all of it in `config/topics/<topic>.yaml`.
- Geo ground = Illinois; granularity = counties + ZIP+4 (not more states).

## Shape

```
Telegram / CLI / JSON ──▶ language.parse (grammar; LLM fallback for prose)
        │                        │
        ▼                        ▼
  contrib.contribute ──▶ network.process: store → corroborate (both ways) → trust
        │                      → judge quality → open/join county event → score
        ▼
  subscriptions.fanout_{report,event} ──▶ delivery (outbox: send now, retry later)

alert-loop (~30 s) ─▶ nws_alerts: CAP threads ─▶ subscriptions.fanout_alert
        (county + ZIP subs, routed by severity; card per chat, edited in place;
         re-notify only when impact rises; closed when the alert ends)
feeds (iem_lsr · iem_asos · …, watch every 5 min) ──▶ same Signal table

pipeline (01:10, under digest_core.runlock) ──▶ digest.build → gdrive.publish
notify (08:00) ──▶ brief.fanout_brief (one morning brief per subscriber's county)
```

## Key facts / decisions

- **Signal** (`models.py`) is the unit: metric, canonical value (or flag=1),
  observed/received, Location (lat/lon, county_fips, zip5, zip9, precision),
  sensor id/kind, quality (`raw → corroborated | flagged | rejected`;
  `reference` for official), corroboration/contradiction counts,
  reference_agreement, group_key (one NWS alert = many county rows),
  evidence/raw JSON, event_id. UNIQUE(source, source_id) dedups re-delivery.
- **Sensor kinds**: human, bot, station (ASOS, trust 0.9), official (LSR per
  WFO, 0.95), authority (NWS alerts, 1.0). Reference kinds skip corroboration
  but settle nearby human readings (forward) and drive events.
- **Trust** = `(K·prior + agreements)/(K + agreements + disagreements)`, K=4,
  prior 0.5 unless `/admin trust` set `trust_prior`. Trusted ≥ 0.8 can push
  an event alone. **Trust v2** (`trust.py`): the record is per (sensor, topic) in
  `sensor_trust` (agree/disagree as floats, faded by a 180-day half-life to
  `updated_at`); `sensors.trust` is the pooled figure; `standing()` gives an 80%
  interval and reads "new · n checks" under 3 outcomes. Weights: reference 1, radar
  grid 0.5, a neighbor `independence()` = 0.3 at the same exact point (< 200 m, GPS
  points only; ZIP centers don't count), ÷(1 + n/3) for a pair that agreed n times
  before (`sensor_pairs`). Events verify on `witnesses()` (one per roof), and
  `event_attach_stats.mean_trust` uses the event topic's record.
- **Storms as stories** (`stories.py`, `story_brief.py`): a story clusters one storm across
  counties. Members (`story_members`, PK kind+ref): events (joined in
  `network._attach_to_event`, `Assessment.story`) and storm-based NWS warnings (only
  alerts with a polygon: `nws_alerts.join_story`; county-wide ones aren't storms);
  storm reports arrive through their events. Join rule: same topic, centroid within
  `STORY_KM` 60, active within `STORY_HOURS` 3; stories that meet merge into the older
  (`merged_into`, `current()`); idle 6 h → closed in the watch. Cards: a storied event's
  push goes to `subscriptions.fanout_story` (thread `story:<id>`, one card per storm per
  events subscriber, edits keyed by text hash, sounded reply only on escalation);
  `story_changed()` edits cards quietly as a story grows, merges or a warning ends.
  Brief: numbered facts (warnings in order, then events by severity, top 5) shared by
  card and brief; the LLM (summarizer backend, watch pass only, once per story version:
  `brief_at` = the `updated_at` it was written for) must cite existing facts and invent no
  numbers (`valid()`), else the template from the same facts. `summaries()` feeds the
  digest "Storms" section, the morning brief, and `storms.json` / the site's "Storms
  today" panel (11 public JSON docs now).
- **Interim design wave (the review's mockups)**: alert cards go out as Telegram **rich
  messages** (Bot API 10.1 `sendRichMessage`, HTML: `<h4>` event · header tags,
  `<h2>` "Your home is inside the warning.", arrival `<tg-time>`, `<mark>` impact chips,
  `<code>` tags, `<blockquote>` protective action, `<tg-map>` of the reader's place,
  `<footer>`), built by `subscriptions.format_alert_rich` + `locate()`/`card_tags()`; pack
  `alert_parameters` entries carry `show: header|chip|tag`, `chip`, `metric` (size words).
  `Bot.deliver/edit(rich=…)` fall back to the plain `text` on any 400 (never lose a
  warning); `TELEGRAM_RICH_MESSAGES`. Outbox rows carry `rich`. Card buttons
  (`card_markup`): 🗺 Map (inline `web_app`, app opened on `focus=<alert id>`; view-only:
  sendData needs a keyboard launch) · 📍 Report what I see (`rw:<hash>` → kv thread →
  an `asks` question under the card, timed now) · 🔕 Mute 1 hr (`mute:60` / `unmute`;
  `delivery.muted()` sends silently, never drops). Edits carry rich + buttons; ended cards
  drop the buttons. Report reply: "✅ Recorded: … at …" + "Corroborated: <reason>; radar
  estimates … (last hour)" (`grids.quick_look`, 6 s cap, informational). Pickers get
  "⌨️ Type it" (`t:` → ForceReply with the pack's `example`). App reports with
  `photo: true` → the next photo within 10 min joins as evidence (`photo:<chat>` kv).
  Morning brief rich (`brief.compose_rich`: list, readings table, `<details>`). **Chat
  sections** (Bot API 9.4 topics in private chats): `TELEGRAM_TOPICS=auto` uses them when
  getMe `has_topics_enabled` (BotFather setting; checked hourly); `delivery.thread_for`
  creates ⚠️ Alerts / 📍 My reports / ☀️ Morning brief per chat (`chat_topics`); replies go
  to the thread they came from. The **Mini App** was rebuilt to the mockups: bottom tabs
  Now · Report · Alerts · Me; a D3 map around home (±20 km) with Radar / Warnings /
  Reports / Gauges toggles, Census towns, rivers and highways (`site/assets/il-reference.json`
  from `scripts/build_map_layers.py`, no third-party tiles), the warning polygon, storm
  motion arrow, HOME, 5-mile scale, a sheet of what's near; radar is requested for
  half-degree cells, not the home; Alerts tab = the card design (band, headline, chips,
  action, footer) + Mute; Report = Where / When / sizes drawn to scale (`visual: size`) /
  Photo / Send. `reports.json` (people at ZIP centres) and `gauges.json` feed it.
- **Telegram Mini App** (`site/app.fragment.html` → `docs/app/index.html` via
  `export.render_app_page`): tabs Now (live NWS alerts for home, statewide map with
  storms, county-page link), Report (every pack's `quick_reports` from topics.json; at
  home or "where I am" via Telegram LocationManager / browser geolocation), Me (home,
  subscriptions, follow-ups, recent reports + how each was checked, trust by topic).
  Opened only from the report keyboard's `web_app` button (`bot.APP_BUTTON`; https
  `public_site_url` only), because `sendData` works only from keyboard buttons. The chat's
  state (`bot.app_state`) rides in the URL `#s=` fragment (base64url JSON; never sent to
  the server); changes return as `message.web_app_data` JSON handled by
  `bot.handle_web_app`: `{a: report, text, lat?, lon?}` · `{a: subs, add, remove}` ·
  `{a: home, place}` · `{a: followups, on}`, validated like typed input. No BotFather
  setup needed; the page exists once the nightly export pushes `docs/app/`.
- **Open data out** (`opendata.py`, written by `export_all` into docs/): `feeds/events.geojson`
  and `feeds/storms.geojson` (the week's verified events / storms), `feeds/cap.atom` (CAP 1.2
  messages in Atom; `incidents` ties an event's versions; category from each pack's
  `cap_category`; times as `-00:00`, never `Z`), `data/network.sqlite` (built fresh: people's
  readings + NWS storm reports + events + storms + alerts + sensors, 90 days; stations and
  gauges left to IEM/USGS so it stays small; dropped if > 20 MB) and `data/metadata.json`
  for Datasette Lite (`opendata.datasette_url()`, linked from the site). Privacy: people as
  `public_handle` + ZIP5/county; any place a person gave → its ZIP or county centre
  (`public_point`); no notes, photos, names, chat ids or ZIP+4. GitHub Pages sends
  `Access-Control-Allow-Origin: *`, which Datasette Lite needs.
- **Backups** (`backup.py`): the nightly pipeline's housekeeping copies the live DB with
  SQLite's online backup API to `BACKUP_DIR/network-YYYY-MM-DD.db.gz`, keeping `BACKUP_KEEP`
  (7). Point `BACKUP_DIR` at a synced folder for an off-machine copy. `/backups/` is gitignored.
- **Gridded truth** (`grids.py`, pack `reference_grids`): MRMS QPE via the NOAA
  mapservices ImageServer identify (mosaicRule picks the product by catalog name,
  renderingRule None → raw mm; with a raster function you get a colour class) and
  MRMS MESH via NCEP GRIB2 (template 3.0 grid, 5.41 PNG packing; Pillow decodes;
  cropped to the state; value = (R + X·2^E)/10^D; −3 = no radar, −1 = none). Each
  window is judged on its own valid time (the 24-h MESH refreshes every 30 min, the
  30-min one every 2). Verdicts agree / disagree / far (3× off and > 2× abs tol) /
  quiet, stored in `evidence.grid`; `network.settle_by_grid` applies them and may
  verify the event. `watch.grid_pass` runs each 5-min watch (off in tests:
  `GRID_CHECKS_ENABLED`) and sends "checked out" notes citing the radar.
- **Event score** = `severity × (1 + 0.5·ln(n_sensors)) × clamp(trust/0.5,
  0.4..2) × {1.25 official agree, 0.7 disagree, 1.0}`; push when score ≥
  `EVENT_PUSH_MIN_SCORE` (1.0) AND verified (≥2 sensors | official | trusted);
  re-push when score grows ×1.4. Idle 6h → closed.
- **Areas**: `il` ⊃ `il.<countyslug>` ⊃ `il.zip.<zip5>` ⊃ `il.zip.<zip9>`.
  County slug = lowercase alnum (`stclair`, `jodaviess`). Digest subscriptions
  are state-wide by construction. NWS alerts match by county containment: they
  also reach ZIP / ZIP+4 subscriptions whose ZCTA sits in an alerted county
  (`geo.zip5s_in_county`, one county per ZCTA). Events and reports match the
  reading's own keys.
- **ZIP+4** has no free geocode: kept as the finest key, located at the ZCTA5
  centroid unless the sensor shares a Telegram location. Vendored Census
  tables in `config/geo/` (102 counties, 1,396 ZCTAs). Point→county via
  api.weather.gov `/points` (cached in kv by geohash-6), nearest-centroid
  fallback; `GEO_ONLINE_LOOKUP=false` in tests.
- **Topic pack** (`config/topics/weather.yaml`): metrics (aliases, units w/
  factor or `expr` in x — sandboxed AST), `default_unit` (US-typed inches /
  mph / °F), `words` (hail chart), range, tolerance (abs/rel), radius_km,
  window_min, event thresholds+severity, categories, alert_routing,
  alert_support (which active alerts back a metric), lsr_types,
  station_fields. `topics.py` loads all packs; `find_metric` resolves aliases.
- **Personas**: contributor, grower/land manager, spotter/EM, subscriber,
  researcher/analyst, steward — see README "Who it's for".
- **LLM use** (all best-effort, `LLM_ENABLED=false` in tests): free-text
  parse (Ollama `local_qwen`), news triage (Ollama), digest narrative (MLX).
  Same shared servers as the other digests → the daily pipeline holds the
  cross-digest run lock (`pipeline_serialize("intelligence-network")`).
  The bot's per-message LLM parse does NOT take the run lock (interactive).
  **Narrative** (2026-10-06): written from `digest.narrative_facts` (~2 KB: alerts in effect,
  rivers, storms, what stood out, the stories, the places' forecasts, outlook risks, the
  digest's date), not the whole model (12 KB, three quarters reading list: MLX timed out at
  120 s twice); ~12 s on MLX. Prompt: paragraph 1 ≤ 30 words on what mattered, naming places
  (fits `LEDE_MAX`); paragraph 2 what today holds; the network only when people's readings are
  news. `llm.narrative` falls back once to the parser backend, told to answer `{"text": …}`
  (Ollama's format=json echoed the facts back); `<think>` stripped.
- **Google Drive**: `drive.file` scope; folder + Latest doc ids in `kv`;
  `publish()` creates/updates the day's doc and rewrites Latest;
  `add_reader(email)` shares the folder (`/digest email` on Telegram);
  `GDRIVE_PUBLIC_LINK` makes the folder anyone-with-link. Fake service in
  tests.
- **Quiet hours** apply only to the digest ping (`notify` 08:00). Warnings /
  events / reports are opt-in and never suppressed.
- **Telegram**: `telegram.Bot(TelegramNotifier)` adds `deliver` / `edit` (return a
  `Sent`: message id, retry_after, permanent) and `send_to` (bool, for replies).
  Times shown to people go through `tg_time()` (Bot API 9.5 `<tg-time>`: each
  reader's own zone; Illinois fallback text). Never log a request exception's text:
  its URL carries the token. New bot token (do not reuse the PC/macro bots). Admin =
  `TELEGRAM_ADMIN_CHAT_ID`. Open registration unless `NETWORK_JOIN_CODE`; auto-join
  on first reading. Rate limit `NETWORK_RATE_LIMIT` per 10 min; `/admin ban`. The
  listener answers the backlog queued while it was down (no skipping).
- **Getting started is all taps** (bot.py, 2026-10-01): before Start, the bot's description, profile
  line and a 7-command menu (`description()`, `short_description()`, `COMMAND_MENU`) are set from
  code at listener start where they differ (`ensure_profile`). /start joins (unless
  `NETWORK_JOIN_CODE`) and asks one thing, "Where are you?", with two buttons: 📍 Share my
  location, or 🗺 Pick my county (Telegram can't share a location from a computer): six
  alphabetical runs → the county (`hc:<run>`, `hs:<fips>`). A ZIP typed on its own also sets the
  place; /home with nothing asks again. Once a place is set (`settled`): "Your place: ZIP 62704 ·
  Sangamon County" with 🔔 Warnings for my area (weather.warnings at the home ZIP, else county)
  and ☀️ Morning brief at 8 AM (weather.digest), toggles (`go:warn`, `go:brief`; ✅ when on, tap
  again to stop), then the report keyboard in a follow-up (`Reply.then`: a message carries one
  keyboard). A bare /subscribe shows the same two buttons. /help is six lines with the rest in an
  expandable "More". The site's join section is "Sign up in three taps".
- **Delivery** (`delivery.py`): every push is an `outbox` row first (UNIQUE key ×
  chat = the dedup), sent at once, retried with backoff (429 → `retry_after`) until
  sent or stale; 400/403 → dropped. Rows are claimed before each attempt, so the
  bot, watch and alert loop never double-send. Paced ~1/s per chat, ≤ ~25/s overall.
  Actions: send · card (message id kept in `sent_messages`) · edit · reply.
- **NWS alert threads** (`feeds/nws_alerts.py`): CAP messages linked by
  `references` → `alert_threads` / `alert_ids`; rows' `group_key` = thread id, so an
  alert is listed, counted and carded once. A new version retires older rows. Impact
  tags come from the pack's `alert_parameters`; a rise (or severity/event up)
  re-notifies as a reply to the card. `/alerts/active` never lists cancels, so a
  thread ends at expiry or after `ABSENT_CONFIRM` (90 s) missing from the feed; an
  empty feed with ≥ 3 alerts in force is treated as a glitch. Severe/Extreme ends
  get a silent all-clear reply. Storm-based warnings carry their polygon and
  motion (`evidence.polygon` / `.motion`): each chat's card says inside/outside
  for its live location or home (`where_you_are`; "center of ZIP …" when home is
  a ZIP centroid) and the arrival time (`geo.storm_arrival`); inside-polygon chats
  send first (priority -1), and an update that newly covers a chat re-notifies.
- **Alert card design** (polish waves, 2026-09-30): one `<h4>` (event, title case); a top-level
  damage threat (DESTRUCTIVE/CATASTROPHIC) right under it; the reader's situation in bold ("Your
  home is inside the warned area." / "…outside, about 6 mi east of it" / "Includes Sangamon
  County, where your home is" for county-wide alerts) with a fixed arrival time (a relative
  `tg-time` read "41 minutes ago" on a card that stays up); the picture; threat + hazards
  ("Hail **1.75 in** (golf ball) · Wind **70 mph** · Tornado possible", never adjacent `<mark>`s);
  the instruction; a footer whose times carry the weekday when not today (`subscriptions.clock`).
  Buttons: Report what I see (full width, primary) / Live map · Mute 1 hour.
- **Card picture = a radar product** (`cardmap.py` + `nexrad.py`, pack `card_radar` /
  `alert_colours`): Telegram's `<tg-map>` ignored zoom, and IEM's mosaics are pre-coloured and
  ~1.2 km (RIDGE PNGs are downsampled too), so the card draws **one radar's raw lowest sweep**:
  NEXRAD Level III N0B (0.5° × 250 m super-res) from Unidata's public AWS bucket
  (`unidata-nexrad-level3`, keys `ILX_N0B_YYYY_MM_DD_HH_MM_SS`), nearest of the pack's sites.
  `nexrad.decode` (ICD 2620001: WMO header, PDB thresholds at halfwords 31–46, bzip2, packet 16;
  reflectivity min/step or generic float scale/offset) matches MetPy bin for bin; `sample`
  is bilinear in (azimuth, range) on values. QC (pack `quality`): same-scan dual-pol correlation
  coefficient N0C < 0.85 → dropped, except strong echoes inside a *solid* storm area (tornado
  debris; wind farms like Twin Groves E of Bloomington are stationary, CC ~0.5, up to 60 dBZ);
  holes under rain are filled from the rain around (normalized box filter, ≥ 35% rain around);
  lone specks go. Within 12 km of the radar (spokes, the blind cone) the readings around are
  smoothed in (`_near_site`); a distant second radar was tried and painted streaks. Colours
  from the pack's stops (translucent light rain). Labels never sit on the storm's track: its
  name goes behind it, the times beside it. Bump `cardmap.RENDER_VERSION` whenever the drawing
  changes (pictures and loops are cached by content name). Every render is looked at before
  it ships (stills, loop frames): see the samples workflow in memory. Dark map (site assets), warning outlined in its event
  colour, NWS motion → dashed track with 10-min times, the reader's blue dot + "storm ~7:04 PM",
  people's reports (cyan, at ZIP centres) and spotters' (white) from the last 2 h, legend,
  scale, "Lincoln radar · 6:35 PM". 1080×720 JPEG named by scene + radar key; scene kept as
  JSON. **Loop**: the card goes out with the still at once; an outbox edit (`<thread>:loop:…`,
  priority 7, not sent in the fan-out) swaps in a 45-min H.264 loop (`render_loop`, ffmpeg)
  rendered lazily at delivery (`cardmap.loop_bytes`), sent as an animation (`tg://video?id=`,
  InputMediaAnimation); `fallback=False`, so a refused loop leaves the still. Radar files cache
  in data/radar/. `CARD_MAPS`, `CARD_MAP_RADAR`, `CARD_MAP_LOOP` (all off in tests; the tests
  build a synthetic N0B file). IBM Plex Sans (OFL) vendored in config/fonts/. numpy is a dep.
- **Digest in Google Docs**: Docs import keeps only longhand inline styles (no `font:`
  shorthand, text-transform or letter-spacing), starts paragraphs at line-height 1, turns a
  top border into a rule, draws unset table borders as a grid, and turns any background
  outside a table cell into a highlight behind every line (the white bars of 30 Sep). It
  keeps the first font family and renders Google Fonts by name (Fraunces, IBM Plex).
  Page control (probed 3 Oct): no page break survives (any spelling); `page-break-after:avoid`
  becomes "keep with next" (`digest.KEEP`, on headings and notes), but not on an `<hr>` (a rule
  is its own line and can end a page alone) and not into a table. A table splits at a page's
  end, between rows or inside one; a paragraph straight after a table loses its space above
  (use a spacer paragraph); a link on a picture is dropped. Phones get a 256-px copy of every
  picture in a Doc. An export made seconds after the Doc is created sometimes renders before
  the fonts load (all Arial; 2 of 14 probes); the published PDFs come from the second pass and
  have been fine. `render_html()` is the Doc; `page=True` wraps it in the browser sheet (site copy).
  `tests/test_digest.py::test_the_doc_never_highlights_text` guards it. Numbers read as
  written: `display_unit` `decimals` / `fractions` (visibility 1/16 mi), 3 figures above 10,000.
- **Places**: `/home` or a one-off location share = home (sensors table); a live
  location share = `places` (kind live, expires with the share, max 24 h): readings
  land there and alert targeting uses it. Live ticks (edited messages) are silent.
- **Corrections**: an edited message withdraws the readings it produced (source_id
  `<chat>:<msg>:<n>`) and re-contributes; an event left empty closes.
- **Deep links**: `/start sub_<topic>_<category>_<area>` (county slug, ZIP, ZIP+4,
  `il`) joins, subscribes, and shows a `request_location` keyboard. The site's
  subscription builder emits these links.
- **Report keyboard**: each pack's `quick_reports` ({button, ask, choices:
  [[label, reading]]} or {button, send}) — readings are data-language text run
  through `_contribute`. Picks arrive as `callback_query` (`q:<topic>:<i>:<j>`),
  the picker message is the reading's message; `u:<msg>` undoes (withdraws).
  `nothing_here` has `scored: false`: an absence report, kept and counted, never
  corroborated, trusted, evented or pushed.
- **Site hero**: `site/assets/il-counties.geojson` (Census TIGERweb generalized
  500K counties, shoreline-clipped, D3 winding) and a ZIP → [fips, lat, lon] table
  are inlined into the page data by `export._page_geography` (not written as public
  JSON). The map is D3/SVG in Mercator; radar is IEM's NEXRAD WMS in EPSG:3857 cut
  to the same frame; alerts come live from api.weather.gov (snapshot fallback).
  The snapshot carries `feeds` (last good run per source) and `reference_sizes`.
  Grids that collapse to one column use `minmax(0, 1fr)` (plain `1fr` let a long
  code line push the phone layout to 1,240 px).
- **Masthead map: the day across every topic** (`notable.py` → snapshot `map`, `data/map.json`;
  drawn by the fragment's "masthead map" script). What stands out comes from the packs: a
  reading past its `event` threshold; a metric's `headline` reading (`{max: High, min: Low}`,
  `{rise: Rise}`: the state's highest/lowest, needing ≥ 3 sites, or the three biggest rises
  at one site, glitch-guarded like `connect._rise`); people's readings (by handle at the ZIP
  centre); NWS storm reports. A running amount (`accumulates`) headlines only as a total: a
  station's hourly amount is labelled a rate ("in/hr"). One place per site with all its
  notable readings; ≤ 14 labelled (headline readings first, then one per measure, ≤ 3 per
  topic, no twin labels within 50 km), the rest dots. Slow sources (soil station) show their
  latest day ≤ 8 days; `county_wide` metrics (USDM) are hatched areas; points outside the
  state's outline are dropped (`in_state`). Stories: kept items ≥ 0.8 relevance, placed by
  `connect.places_in` + `place_point` (a town's own point; an alias at its first county),
  grouped like threads (a sub-place joins a wider one only when they name a measure in
  common), ≤ 7 located + 3 statewide (one per subject first), each linked to ≤ 4 readings
  (the best of each measure it names: a rise, a total, or anything past threshold; a level
  that didn't move bears on nothing). Headlines name a measure only by `news_words` or a
  multi-word alias (one-word aliases matched "Pressure mounts for lawmakers"). Labels use the
  headline name, else the pack's `short`, else the label less "(…)"; scales read in words
  (`display_words`: corn "R4", drought "D2"). Page: layer chips (topic counts, News, Radar),
  rivers (inlined, thinned), screen-constant symbol sizes, Delaunay picking, hover = compact
  preview, click = kept card (trend sparkline, other readings, stories, links; a bottom sheet
  under 560 px), a story highlights its counties and draws arcs to its readings, the news
  list beside the map drives the same highlight; on wide screens the map is sticky and sized
  to the viewport. The readings and stories are as of the snapshot; alerts and radar are live.
- **The brief's map** (`daymap.py`): the masthead map as one picture for the 08:00 brief (a chat
  can't hover): 1080×1350, the alert cards' dark map with the site's dark topic colours; dots
  and labels as on the site (a rise's ▲ is drawn: IBM Plex has no glyph), numbered story squares
  with arcs to their readings, statewide stories in the key on the right, NWS alerts in their
  card colours (warnings stronger than watches), the reader's county outlined. No radar: the
  brief is about the last day. Built from a fresh `notable.build()` at brief time (fresher than
  the site's 01:10 snapshot), one per county among subscribers, named `dm-<hash>` in
  cardmap's folder so delivery uploads it like a card picture and reuses Telegram's file id;
  `CARD_MAPS` off → no picture. The brief lists the stories under the map's numbers
  (`connections(day)`: a short block each, "1 · Chicago area", the lead headline with its
  publisher and how many more, a "↳ value, place" line per reading, ⚠️ the alerts in force;
  Telegram showed `<code>` as plain text and one run-on bullet was hard to scan), and its
  "Across Illinois" is the map's labelled readings; a story's further headlines (`more`)
  stay out of "Also worth reading". The picture's ▲ is sized from the font's digits.
- **The digest's map** (the Google Doc and the site's digest.html): `daymap.LIGHT`, the same
  picture in the site's light colours on white (prints, sits on the page), drawn by
  `digest.build` from a fresh `notable.build()` (`model.day`, `model.picture`; both kept out of
  the narrative's JSON) and embedded as a base64 `data:` image, 384×480 px in the Doc (4 × 5 in)
  and 432×540 on the site (Drive's HTML import keeps it, sized, and the PDF/Word exports carry
  it: probed). Page 1 = masthead, lede, "Also as" links, the map, then the numbers: the lede is
  the narrative's first paragraph up to `LEDE_MAX` (200 chars, three lines), else the headline
  (the paragraph opens the body), whose "N NWS alerts in effect" is `vitals.alerts_active` (in
  effect now, as the numbers count them; it once counted the window's 11 over a grid saying 4);
  the first section drops its rule. Probed with ledes of 1–3
  lines and a long narrative: nothing splits and the map never leaves page 1 (it once did:
  the "Also as" line pushed it to page 2). No caption (the key says what marks are; a caption
  fell onto page 2 alone). The phone's blur: `model.picture_full` (PNG) goes up as the day
  folder's `map.png` (`gdrive.publish(extras=…)`), linked as "Full-size map" in the Doc's
  "Also as" line, and the brief's "Full digest" opens the site copy (`brief._digest_links`:
  site, then "Google Doc", "All digests"). Then "In the news,
  and what was measured there" (`notable.listing`, shared with the brief) and "What stood out"
  (`notable.standouts`: reading · where · why; a reading only a story ties in says "Tied to
  story 1"), which replaces the Doc's "Station extremes" (it gave a station's wettest hour as
  the day's rain); station-extremes.csv stays. "Worth reading" skips the listed stories.
- **Morning brief** (`brief.py`): replaces the digest-link ping. Per digest
  subscriber: county from live location, else home, else the state. It opens with the
  reader's day (`today()` + `_in_effect` + `high_water()`): the county's forecast and outlook
  risks, the alerts in effect there, the river gauges running high nearby; then the state's
  map and stories; then the rest of the county (alerts ended in 24 h, storms, network events,
  every metric people reported or whose reading crossed its event threshold — no weather
  code), the statewide summary, the digest's links (the site copy first, made for phones) +
  county page. Without a county: outlook risks and rivers in flood first. One forecast per
  county and one river survey per fan-out. Each brief carries "📣 Invite a neighbor"
  (`invite_markup`: Telegram's share sheet with t.me/<bot>?start=sub_weather_warnings_<county>).
  Works without Drive. Key `digest:<local date>` = once a day.
- **The day ahead** (`ahead.py`, weather pack `ahead`; `AHEAD_ENABLED`, off in tests): the NWS
  forecast for a point (`points_url` → the forecast URL, kept in kv as `nwsfc:<geohash6>`), from
  the next daytime period ("Today"/"Tonight" at 08:00, "Tuesday"/"Tuesday Night" at 01:10);
  periods that already ended are dropped (the API's cache served "This Afternoon" at 6:30 PM).
  Outlooks (SPC severe, level n of 5; WPC excessive rain, n of 4) are polygons with a level and a
  valid window: the issuance covering the forecast day's noon is used (at 01:10 Day 1 can be
  last night's → Day 2); holes respected; counties at risk by centroid. Icons from the pack's
  `icons` (first match). Digest: "The day ahead" after "What stood out" (risk lines + the
  pack's eight `places`, north to south). Bot: `/forecast [place]` (three periods from now; in
  the menu). fetch.py is the shared JSON fetch (twice, kept 30 min per process).
- **High water** (`rivers.py`, water pack `rivers`, same switch): NWS river gauges (NWPS):
  the state's, plus the far bank of a border river within `border_km` (5) of the state's
  outline (`geo.in_or_near_state`: the Wabash at Covington, Indiana, is not the border);
  tunnel/reservoir levels skipped. Gauges at action stage ("near flood stage") or above, now
  or forecast, get their flood stage (gauge detail), six-hour trend and the next five days of
  forecast (`stageflow`: the crest, or the stage it falls to; dates past 5 days read "Tue 13
  Oct"). Brief: gauges within `near_km` (40) of the county; digest: "High water" (gauge, now,
  flood stage, forecast) after "The day ahead".
- **County pages** (`export.render_county_pages`, `site/county.fragment.html`):
  `docs/county/<slug>.html` ×102 + `sitemap.xml` + `robots.txt`. Static HTML for
  search (subscribe links, readings, neighbors), a small script for live alerts
  and the map. Tokens come from the front page between `/* tokens … */` markers.
- **Questions after alerts** (`asks.py`, pack `alert_questions`: events, `when:
  ended|issued`, ask, reports = quick_report ids): when a card's alert ends, a chat
  inside the polygon (or county, with no polygon) and with a place gets one silent
  reply under the card (the Severe all-clear on top) with those report buttons.
  `questions` row per alert × chat; `COOLDOWN` 6 h per chat. Answers are readings at
  the chat's place dated to the storm (arrival from motion, else mid-alert).
  Callbacks `a:<q>` / `a:<q>:<i>` / `a:<q>:<i>:<j>`; picks use source ids
  `<chat>:<msg>:a<n>:<k>` so "Add another" and Undo (`u:<msg>:<q>`) both work.
  `when: issued` (river floods) asks with the first card. Outbox rows carry
  `markup_json` for these buttons.
- **Follow-ups** (`feedback.py`): silent notes, once per reading, ≤ `DAILY_MAX` (5) a
  day per chat. `confirmed` (a later reading settled yours: `Assessment.settled`
  from `_corroborate_back` / `_corroborate_forward`), `helped` (an event you're in
  was verified and pushed, reason new; the tipping sender excluded), `ahead` (a new
  NWS alert confirms raw readings in its counties/polygon from the metric's window
  before it: `network.settle_by_alert`, pack `alert_support`, event-level values
  only). Called from `contrib`, `feeds/base.after_store`, `nws_alerts.after_store`.
  `/followups off` (kv `followups:off:<chat>`) stops notes and questions.
- **Weekly measures** (`metrics.py`): activation, ask rate, median minutes to
  corroboration (`signals.settled_at`, set on first corroborated/flagged), counties
  with an active human sensor, alert card latency p50/p95 (outbox sent − thread
  opened, first fan-out only), messages per subscriber by kind. `intelnet metrics`,
  `/admin metrics [days]`, and Monday's notify sends them to the admin chat.
- **County flyer**: each county page has a scan-to-subscribe QR (desktop only) and a
  print-only flyer (`@media print`, one letter page, tear-off tabs). QR via
  qrcode-generator (cdnjs), drawn as dark-on-white SVG; it encodes
  `t.me/<bot>?start=sub_weather_warnings_<slug>`.
- **Pre-threading alert rows** (group_key = own CAP id, no thread) retire when their
  id leaves a full feed (`retire_unthreaded_alert_rows`), so old versions stop
  counting as active.
- **Accumulating metrics** (`accumulates: true`: rain, snow) compare only readings
  over the same period (`evidence.period`; ASOS `phour` is `1h`), so an hourly
  station amount never flags a storm total. MRMS QPE as a rain reference: not yet.
- **digest-core** is consumed as an editable path dep from
  `../pc-insurance-digest/packages/digest-core` (like macro). CI checks out
  both repos side by side.

## Schedule (Mac mini launchd; `scripts/install_launchd.sh`)

| job | when |
|---|---|
| `com.dr.intelnet.bot` | KeepAlive (SuccessfulExit=false) |
| `com.dr.intelnet.alerts` | KeepAlive: NWS alerts every `ALERT_POLL_SECONDS` (30) + outbox retries |
| `com.dr.intelnet.watch` | every 300 s (LSR + gated stations; alerts only if the alert loop's heartbeat is stale) |
| `com.dr.intelnet.daily` | 01:10 — third in the queue: macro 01:00 → PC 01:05 → this |
| `com.dr.intelnet.notify` | 08:00 morning brief (per county; Drive links when today's digest is up) |

## Topic packs (2026-09-15, wave 2)

Five packs: `weather`, `soil`, `water`, `agriculture`, `air`. `language.parse`
reads ALL packs at once (scope `*`); aliases must be unique across packs
(`tests/test_packs.py` enforces). Bare `events` = `weather.events`;
`soil.events` explicit; `*.events` expands to every pack. Extra mapping
sections in a pack (`usgs_parameters`, `awdb_elements`) land in
`Topic.mappings`. New feeds: `usgs_water` (hourly gate; since 2026-10-06 the USGS Water
Data API: `latest-continuous` in one request + `monitoring-locations` names/counties kept in kv
for a day; the legacy NWIS IV service answered 503 to most polls), `nrcs_scan` (AWDB, daily;
IL has one station), `usdm` (Drought Monitor county API, daily; aoi = comma-separated county
FIPS). Events anchor on the reading's observed time (`find_open_event(around=…)`), so
backfills/late readings join the right event.
Wave 3 sources (2026-10-06): `cocorahs` (the volunteer observers' morning 24-hour rain and new
snow, the pack's `cocorahs_fields`; hourly 7 AM–8 PM; evidence kind "observer", period 24h →
a total, not a rate (notable: a period under 6 h is a rate); county by outline
(`geo.county_at`); opens no events and pushes nothing: a report tells of a day that's over) and
`airnow` (EPA AirNow's public hourly file: PM2.5 and ozone at the state's ~67 monitors, the
air pack's `airnow_parameters`; county from the AQS id; labelled "<County> County air
monitor" (site names are agency codes); drives events like any official reading).

## Site + snapshot

`export.snapshot()` → 14 public JSON docs (anonymised: humans as `s-xxxxx` +
county only). `site/index.fragment.html` (Fraunces / IBM Plex; light+dark
tokens; D3 v7 from cdnjs) is wrapped into `docs/index.html` with the data
inlined; the same fragment publishes as a Claude artifact. `intelnet
demo-seed` builds the sample fortnight; `intelnet export --sample` rebuilds the
site from it. Pipeline stage 5 re-exports after each run; `SITE_AUTO_PUSH`
pushes `docs/`. Pages workflow in `.github/workflows/pages.yml` (needs a public
repo on the free plan).

## Public launch (2026-09-15)

Repo is **public**; GitHub Pages serves `docs/` at https://dram-dev.github.io/intelligence-network/
(+ `privacy.html`, `terms.html` rendered from `site/` with `{{PLACEHOLDERS}}` by
`export.render_static_pages`). `SITE_AUTO_PUSH=true` → the nightly run commits + pushes `docs/`.
Public identity = **ilintelligencenetwork@gmail.com** (`NETWORK_CONTACT_EMAIL`, Drive owner via
`GDRIVE_ACCOUNT`). Privacy rules the code enforces: report pushes, digest and site show a contributor
only as `public_handle()` + ZIP5/county (`Location.describe_public`); `/forget confirm` deletes a
person's sensor, readings, subscriptions, e-mail (+ Drive folder share). Google OAuth app: Branding
values in `secrets/README.md`; after "Publish app", re-run `drive init --remote` once.

## Status (2026-09-15)

Waves 1–2 built and tested. Live-verified feeds: NWS alerts, IEM LSR/ASOS,
USGS IV (278 IL sites), NRCS SCAN (Mason), USDM county stats, Google News.
Bot handle `@intelligence_network_bot` exists. **Turn-on checklist** =
`uv run intelnet setup`: token + admin chat id in `.env`, Google OAuth client
in `secrets/`, `intelnet drive init`, `bash scripts/install_launchd.sh`.

## Nightly check and news quality (2026-10-06)

`health.py` runs at the end of the nightly pipeline and sends the admin chat one message
(`health:<date>`, only when there's something): a digest without its narrative (LLM on), a
failed Drive publish / export / push (`git_push_docs` returns "pushed" | "nothing" | "failed"),
a feed failing ≥ 50% of its runs in a day (≥ 4), a feed past its cadence (`STALE_HOURS`; a
feed never run isn't named), a news feed heard in 90 days but not in 21. News: the
`news_feeds.yaml` `block` list (title patterns: location forecast pages, auto "% chance of"
posts, IndexBox, …) and `title_key` dedup at ingest (3 days) and at read
(`db.kept_items_since`). Tests are hermetic: conftest makes `fetch.get_json` raise and stubs
CoCoRaHS, AirNow and the USGS site list; a test fakes the source it reads.

## Next ideas

- A second topic pack (air quality / river gauges) to prove the language.
- Photo evidence: download + attach to the Drive doc (currently file_id only).
- A per-ZIP+4 mesh view once there are enough human sensors to matter.
- Public read-only web view of the mesh (stdlib server, like PC's `digest web`).

## How to run

```bash
uv sync && cp .env.example .env && uv run intelnet init-db
uv run intelnet watch · signal "rain 0.4in @62704" · near 62704 · digest --html out.html
uv run intelnet drive init · pipeline --run-type manual · health
uv run pytest
uvx ruff@0.15.0 check .     # CI's lint gate: zero findings, or every push (the nightly one too) fails CI
```
