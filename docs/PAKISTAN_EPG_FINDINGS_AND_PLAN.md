# Pakistan EPG Expansion — Findings, Source Map, and Integration Plan

**Status:** Research and implementation plan only  
**Verified:** 2026-08-25  
**Repository:** `shameez-struggles-to-commit/EPG`  
**Purpose:** Give Hermes a concrete, repository-aware execution plan for materially improving Pakistani EPG coverage without weakening the existing safety/currency/matching guarantees.

---

## 1. Executive summary

The current EPG pipeline is already strong at aggregation, matching, fallback, validation, collision handling, and stale-data rejection. The Pakistan gap is not primarily a generic XMLTV-source problem anymore. The highest-value remaining work is to add **first-party Pakistani broadcaster schedules** as a dedicated source layer and let the existing cascade decide when those schedules are actually usable.

The strongest targets found and inspected are:

1. **PTV network TV guide** — highest upside; one integration may cover PTV Home, PTV News, PTV Global, PTV Bolan, PTV National, AJK TV, and potentially PTV Sports.
2. **Green Entertainment official schedule** — live official schedule page, but schedule content is client-loaded; endpoint/API discovery is required before implementation.
3. **BOL Entertainment** — official site exposes real recurring programme times; usable as a conservative partial EPG source.
4. **A Plus / TV One / Urdu 1 / Play / other domestic broadcasters** — valid discovery targets, but current schedule delivery needs to be confirmed before code is written.
5. **News channels (SAMAA, Dunya, 92 News, Dawn News, etc.)** — useful later, but lower priority because schedules are often repetitive, sparse, or programme-page based rather than true 24-hour grids.
6. **Legacy Pakistan TV-guide apps** — potentially valuable only as a lead to a surviving backend; do not rely on their published APK-era schedule data directly.

The recommended architectural change is to stop treating all Pakistani broadcaster logic as one growing `scrapers/pk_scrapers.py` file and instead create a small Pakistan source subsystem with common HTTP/time/XMLTV/health primitives.

Most importantly, **new Pakistani sources must not automatically override everything simply because they are first-party**. The existing repo currently gives `pk` tier 0. That is appropriate only when the fetched programme row is current, non-placeholder, internally consistent, and known to correspond to the provider stream variant. The integration should preserve the repo's existing principle: **blank is better than wrong**.

---

## 2. Current repository behavior that this plan must preserve

### 2.1 Existing Pakistan scrapers

Current `scrapers/pk_scrapers.py` already covers these broadcaster/feed keys:

- Geo Entertainment / Har Pal Geo
- Geo Kahani
- Geo News
- HUM TV
- HUM TV Europe
- HUM TV World SD
- HUM TV World HD
- ARY Digital
- Express Entertainment
- Aaj Entertainment
- ARY Zindagi

That is already more than the simplified README description of "7 custom Pakistani scrapers".

### 2.2 Current Pakistan override behavior

`pipeline/build_mapping.py` currently gives the Pakistani custom source the highest source tier (`pk: 0`) and includes explicit mappings such as:

- `Har Pal Geo` -> `geo_entertainment_pk`
- `Geo TV` -> `geo_entertainment_pk`
- `Geo Kahani` -> `geo_kahani_pk`
- `Geo News` -> `geo_news_pk`
- `Hum TV Europe` -> `hum_tv_europe_pk`
- `ARY Digital Asia` -> `ary_digital_pk`
- `Express Entertainment` -> `express_entertainment_pk`
- `Aaj Entertainment` -> `aaj_entertainment_pk`
- `ARY Zindagi` -> `ary_zindagi_pk`

This means a new Pakistan source is not just a fetcher addition. It affects the highest-priority candidate layer and therefore needs stronger per-channel validation than a low-priority generic source.

### 2.3 Existing safeguards to preserve

The current pipeline already protects against:

- stale programmes
- bad XML / gzip / HTML masquerading as feeds
- placeholder schedules
- source failures
- wrong-country matches
- overlapping/conflicting programme intervals
- canonical-id collisions
- generic fuzzy subset false positives
- duplicated provider `epg_channel_id` values
- event/non-linear channels receiving linear EPG
- high-priority candidates with no current data blocking usable lower-priority candidates

Any Pakistan expansion should plug into these controls, not bypass them.

---

## 3. What was actually inspected on the web

The research was intentionally focused on whether the schedule delivery is **usable by automation now**, not merely whether a broadcaster has a webpage.

### 3.1 PTV — strongest opportunity

Official PTV pages expose a central TV-guide structure and network navigation for multiple channels, including:

- PTV Home
- PTV News
- PTV Global
- PTV Bolan
- PTV National
- AJK Television
- PTV Sports

Relevant official pages discovered:

- `https://www.ptv.com.pk/ptvcorporate/tvguidemain`
- `https://www.ptv.com.pk/ptvhome/tvguide`
- related channel-specific TV-guide pages under the same site family

The guide pages expose weekday/day-selector structure, but the full schedule rows were not reliably present in the static HTML returned to a crawler. That strongly suggests the guide is populated dynamically by JavaScript using an internal request.

**Interpretation:** This is not a reason to abandon PTV. It is the opposite: if the same request model powers several PTV channels, one reverse-engineered adapter could unlock multiple channels with less brittleness than seven independent HTML scrapers.

### 3.2 Green Entertainment — official but client-loaded

Official schedule page:

- `https://greenentertainment.tv/schedule/`

The page exists and exposes schedule UI, but the raw page body presents a loading state (for example, `Loading schedule...`) rather than all schedule records in server-rendered HTML.

This implies the useful integration target is the page's client-side data request/API rather than regex scraping of the page shell.

**Current verdict:** high-value target, but implementation should wait until the actual request/response format is captured.

### 3.3 BOL Entertainment — usable as partial authoritative schedule

Official programme pages expose recurring programme times. Example site:

- `https://www.bolentertainment.com/tv-shows/`

Observed programme/time patterns include daily programmes and weekday/range-based recurring schedules (for example specific Monday, Tuesday, Thursday, Friday-Sunday, or daily slots).

This is **not necessarily a complete 24-hour EPG grid**, but it is still useful first-party schedule data.

Recommended treatment:

- generate programmes only for explicitly published slots
- expand recurring rules into concrete dates for a short horizon
- do **not** invent filler programmes or infer all unlisted intervals
- allow the normal fallback cascade to fill gaps from Sky/epgshare/epg.pw/provider if a trustworthy match exists

### 3.4 TopiDrama — discovery/validation layer, not primary EPG

Current Pakistan-focused site:

- `https://www.topidrama.pk/`

It links or identifies broadcaster schedule pages for a useful set of Pakistani channels, including existing sources and uncovered targets such as PTV Home, A Plus, TV One, Green, Urdu 1, BOL Entertainment, SAB/Play-type entertainment channels, etc.

Use it for:

- discovering current broadcaster domains
- checking whether a channel is still active
- validating names/branding
- discovering schedule URLs

Do not treat it as the authoritative programme source when a broadcaster's own schedule is available.

### 3.5 pakistani.pk — discovery/cross-check only

The site has channel taxonomy/schedule-style pages spanning entertainment and news channels, including many of the names missing from the current custom scraper layer.

However direct access is less reliable and the data quality/freshness model is not as trustworthy as first-party sources.

Use only for:

- channel discovery
- cross-validation
- last-resort evidence during source research

Do not make it a tier-0 feed.

### 3.6 Legacy Pakistan TV-guide applications

Older Android TV-guide applications advertised large Pakistani channel inventories and multi-day schedules, often including PTV, ARY, HUM, Geo, A Plus, Urdu 1, SAMAA, Hum Sitaray and others.

These apps are old enough that their visible app content should not be trusted directly in 2026.

Potential value remains in reverse-engineering only:

- APK network configuration
- API base URLs
- static JSON endpoints
- channel-id dictionaries
- still-live backend services

If a backend survives and returns current 2026 schedule data, it could become a powerful secondary source. Until then, this remains experimental.

### 3.7 Generic GitHub/XMLTV findings

A separate GitHub EPG project was inspected and found to use the same Sky Hawk schedule API family already present in this repository (`awk.epgsky.com/hawk/linear/schedule`).

Conclusion: apparent "new Pakistan EPG repositories" may simply repackage a source already consumed here. Avoid adding duplicates that create false source diversity.

---

## 4. Source-by-source implementation matrix

| Broadcaster/source | Current state | Expected method | Coverage value | Authority | Recommended action |
|---|---|---|---:|---|---|
| Geo Entertainment | Already integrated | HTML schedule | High | First-party | Keep; refactor later only |
| Geo Kahani | Already integrated | HTML schedule | High | First-party | Keep |
| Geo News | Already integrated | HTML schedule | Medium | First-party | Keep |
| HUM TV Asia | Already integrated | HTML schedule | High | First-party | Keep |
| HUM Europe/World feeds | Already integrated | HTML schedule | High for diaspora variants | First-party | Keep variant separation |
| ARY Digital | Already integrated | HTML schedule | High | First-party | Keep |
| ARY Zindagi | Already integrated | HTML schedule | Medium | First-party | Keep |
| Express Entertainment | Already integrated | HTML schedule | High | First-party | Keep |
| Aaj Entertainment | Already integrated | HTML schedule | Medium | First-party | Keep |
| **PTV network** | Not integrated | likely dynamic API/XHR | **Very high** | First-party | **Priority 1** |
| **Green Entertainment** | Not integrated | dynamic API/XHR | **High** | First-party | **Priority 2** |
| **BOL Entertainment** | Not integrated | recurring programme metadata | Medium | First-party | **Priority 3** |
| A Plus | Not integrated | TBD | Medium | First-party if found | Research after top 3 |
| TV One | Not integrated | TBD | Medium | First-party if found | Research after top 3 |
| Urdu 1 | Not integrated | TBD | Medium | First-party if found | Research after top 3 |
| HUM Sitaray | Not integrated | likely same broadcaster family / separate schedule | Medium | First-party | Investigate with HUM family |
| HUM Masala | Not integrated | likely broadcaster family / separate schedule | Medium | First-party | Investigate with HUM family |
| SAMAA | Not integrated | programme pages / possible dynamic schedule | Low-medium | First-party | Later |
| Dunya News | Not integrated | TBD | Low-medium | First-party | Later |
| 92 News | Not integrated | TBD | Low-medium | First-party | Later |
| Dawn News | Not integrated | TBD | Low-medium | First-party | Later |
| Aaj News | Not integrated as separate news feed | TBD | Low-medium | First-party | Later |
| Express News | Not integrated as separate news feed | TBD | Low-medium | First-party | Later |
| Legacy TV-guide backend | Unknown | API if still alive | Potentially very high | Third-party | Experimental only |
| TopiDrama | Active discovery layer | HTML | Discovery only | Third-party | Never primary if first-party exists |
| pakistani.pk | Unreliable/fallback discovery | HTML | Discovery only | Third-party | Never tier 0 |

---

## 5. Channel mapping strategy against the current provider pipeline

### 5.1 Do not rely on fuzzy matching for new Pakistan channels

For high-priority first-party Pakistani schedules, use explicit mappings or carefully curated aliases whenever possible.

Reason:

- provider names may contain `Asia`, `HD`, `PK`, `International`, `World`, or distributor-specific suffixes
- domestic and diaspora variants can have different schedules
- a fuzzy name hit can be semantically wrong even when text similarity is high

### 5.2 Proposed new canonical Pakistan source IDs

Suggested IDs for first-party schedules:

```text
ptv_home_pk
ptv_news_pk
ptv_global_pk
ptv_bolan_pk
ptv_national_pk
ajk_tv_pk
ptv_sports_pk

green_entertainment_pk
bol_entertainment_pk

a_plus_pk
tv_one_pk
urdu_1_pk
hum_sitaray_pk
hum_masala_pk
```

These IDs should be source-internal schedule identifiers, not provider stream IDs.

### 5.3 Proposed explicit override candidates

Only add an override after confirming the actual provider channel name from `streams.json` / coverage reports.

Likely examples to test, not blindly commit:

```text
PTV Home              -> ptv_home_pk
PTV News              -> ptv_news_pk
PTV Global            -> ptv_global_pk
PTV Bolan             -> ptv_bolan_pk
PTV National          -> ptv_national_pk
AJK TV                -> ajk_tv_pk
PTV Sports            -> ptv_sports_pk
Green Entertainment   -> green_entertainment_pk
Green TV              -> green_entertainment_pk
BOL Entertainment     -> bol_entertainment_pk
A Plus                -> a_plus_pk
A Plus Entertainment  -> a_plus_pk
TV One                -> tv_one_pk
Urdu 1                -> urdu_1_pk
HUM Sitaray            -> hum_sitaray_pk
HUM Masala             -> hum_masala_pk
```

Before adding any of these, compare them with the provider's exact current channel names and existing `coverage_gaps.json` output.

### 5.4 Feed-variant safety

Do not map a generic provider name to a domestic Pakistani schedule if it is clearly a diaspora feed.

Examples of dimensions that must remain distinct:

- HUM TV Pakistan vs HUM TV Europe vs HUM World SD/HD
- ARY Digital domestic vs ARY Digital Asia/UK if schedules materially differ
- PTV Home domestic vs any international relay if one exists

If the provider name is ambiguous, prefer a lower-priority compatible global source or leave it unmapped until verified.

---

## 6. Required change to source-authority policy

Today all custom Pakistani data effectively shares `pk` tier 0.

That is too coarse once partial sources such as BOL and potentially incomplete client APIs are added.

### Recommended model

Keep Pakistan first-party sources high priority, but add **source capability metadata**.

Example concept:

```python
PK_SOURCE_POLICY = {
    "ptv": {
        "authority": "first_party_full_grid",
        "requires_current_rows": True,
        "allow_partial_day": False,
    },
    "green": {
        "authority": "first_party_full_grid",
        "requires_current_rows": True,
        "allow_partial_day": False,
    },
    "bol": {
        "authority": "first_party_partial_grid",
        "requires_current_rows": True,
        "allow_partial_day": True,
    },
}
```

The exact representation can differ, but the behavior should be:

1. Full first-party grids get first opportunity.
2. Partial first-party grids win only for intervals they explicitly cover.
3. Empty/stale/placeholder first-party outputs never suppress a lower-priority usable candidate.
4. Ambiguous feed variants are never forced through a name-only override.

This is compatible with the existing `build_pipeline.py` fallback philosophy.

---

## 7. Proposed repository architecture

### 7.1 Near-term low-risk structure

Rather than immediately rewriting all working scrapers, add a Pakistan package for new sources first:

```text
pipeline/pakistan/
    __init__.py
    common.py
    ptv.py
    green.py
    bol.py
    registry.py

pipeline/fetch_pakistan.py
```

Leave the existing `scrapers/pk_scrapers.py` working during initial rollout.

After the new sources prove stable, migrate legacy Geo/HUM/ARY/Express/Aaj implementations into the package incrementally.

### 7.2 `common.py`

Centralize:

- retry/backoff
- browser-like headers
- optional request-session reuse
- content-type sanity checks
- HTML-vs-JSON detection
- Asia/Karachi -> UTC conversion
- day-of-week expansion
- recurring-rule expansion
- interval validation
- XMLTV serialization
- source health summary
- atomic output writes

### 7.3 `registry.py`

Represent each source declaratively where possible:

```python
SOURCE_REGISTRY = {
    "ptv": {
        "channels": [...],
        "kind": "api",
        "required": False,
        "authority": "full",
    },
    "green": {
        "channels": [...],
        "kind": "api",
        "required": False,
        "authority": "full",
    },
    "bol": {
        "channels": ["bol_entertainment_pk"],
        "kind": "recurring",
        "required": False,
        "authority": "partial",
    },
}
```

Do not initially mark new sources workflow-required. A broadcaster redesign must not break the entire daily EPG build.

---

## 8. Detailed implementation sequence

## Phase A — establish current Pakistan baseline

Before coding new sources:

1. Run current mapping/coverage against the latest provider stream list.
2. Extract all streams classified as `PK`.
3. Record for each:
   - provider stream ID
   - provider name
   - provider `epg_channel_id`
   - current winning source
   - current candidate list
   - programme count for next 24h / 72h / 7d
   - current uncovered status
4. Store a temporary audit artifact for comparison.

This baseline is essential. "Added a source" is not success unless the final guide improves.

### Baseline metrics

At minimum:

- number of PK linear streams
- number with >=1 current programme
- number with >=12h coverage next 24h
- number with >=48h coverage next 72h
- number with trusted first-party Pakistan source
- number falling back to Sky/epgshare/epg.pw/provider
- number completely uncovered
- number with conflicts/quarantine

---

## Phase B — PTV endpoint discovery

### Goal

Find the network request behind the official PTV TV-guide UI and prove it returns current schedule data.

### Research steps

1. Inspect page source and referenced JS bundles.
2. Search scripts for:
   - `tvguide`
   - `schedule`
   - `programme`
   - `program`
   - `channel`
   - `day`
   - `ajax`
   - API base paths
3. Identify XHR/fetch request structure.
4. Test at least PTV Home and one second PTV channel.
5. Verify whether channel/date are parameters or path segments.
6. Confirm response freshness using the current date.
7. Determine timezone semantics.
8. Determine whether stop times are explicit; if not, derive stop from next event only when ordering is trustworthy.

### Acceptance before coding

Do not implement until all are true:

- endpoint responds without a browser session or a reproducible session can be created
- at least two PTV channels return distinct schedules
- titles and times can be parsed deterministically
- schedule includes current/future rows
- no obvious geo/auth token requirement that is unsuitable for GitHub Actions

### PTV fetcher behavior

`ptv.py` should:

- fetch several PTV channels through the same adapter
- produce one source file with unique internal channel IDs
- reject a channel if its returned date/day does not match the request
- reject a channel-day with zero rows instead of creating fake coverage
- emit per-channel health counts

---

## Phase C — Green endpoint discovery

### Goal

Find the request used by `greenentertainment.tv/schedule/`.

### Research steps

1. Inspect page JS and network references.
2. Search bundles/source for `Loading schedule`, weekday labels, programme-card field names, and API URLs.
3. Test the endpoint directly.
4. Verify at least two dates/days to ensure the response is not a static homepage payload.
5. Determine whether schedule is Pakistan-time, UTC, or already timestamped.

### Acceptance before coding

- direct reproducible request
- non-empty current/future schedule
- programme titles and start times present
- stable enough for GitHub Actions

If Green's official backend currently returns no schedule for multiple days, do not integrate an empty source just because the endpoint exists.

---

## Phase D — BOL partial source

BOL can be implemented without pretending it has a complete grid.

### Parsing model

Convert recurring text like:

```text
Daily 10:00 PM
Tuesday 6:30 PM
Friday - Sunday 7:00 PM
```

into concrete upcoming datetimes in Asia/Karachi.

### Stop-time policy

Preferred order:

1. explicit end time if published
2. next explicitly published programme start if it is clearly the same day's linear sequence
3. otherwise a conservative bounded default only if the site identifies a programme duration
4. if duration is unknown, do not manufacture a long interval

A partial source should never create all-day fake coverage.

---

## Phase E — second-wave broadcaster research

Research these in order:

1. A Plus
2. TV One
3. HUM Sitaray
4. HUM Masala
5. Urdu 1
6. Play / SAB-type entertainment channels still active in the provider lineup
7. SAMAA
8. Dunya News
9. 92 News
10. Dawn News
11. Aaj News
12. Express News

For every source use this preferred hierarchy:

```text
first-party JSON/API
    > first-party structured server HTML
    > first-party recurring programme schedule
    > reputable third-party schedule
    > no integration
```

Do not lower standards simply to increase the nominal channel count.

---

## 9. Workflow integration plan

Once individual fetchers are proven locally:

### 9.1 GitHub Actions

Add a Pakistan fetch step that is **non-fatal initially**.

It should:

- run after dependency setup
- write to a deterministic data/output path
- preserve existing outputs if this is the expected repo convention only when cache reuse policy explicitly allows it
- print channel/programme counts
- produce a machine-readable health file

Suggested health output:

```json
{
  "ptv": {
    "status": "ok",
    "channels": 6,
    "programmes": 420,
    "newest_stop": "..."
  },
  "green": {
    "status": "empty",
    "channels": 1,
    "programmes": 0
  }
}
```

### 9.2 Source loading

Add Pakistan output to `fetch_sources.py` / source indexing in the same way current custom sources enter the mapping/build pipeline.

Avoid inventing a second matching engine specifically for Pakistan.

### 9.3 Tiering

Initial recommendation:

- full first-party PK schedules: same high priority as current `pk`
- partial PK schedules: high priority only where a concrete programme interval exists
- third-party Pakistan discovery feeds: lower tier, never tier 0

---

## 10. Validation and regression gates

Every rollout should prove improvement against the baseline.

### 10.1 Source-level validation

For each new source/channel:

- parse succeeds
- >=1 current/future programme
- no empty titles
- all stops > starts
- no absurd programme duration
- no duplicate identical rows
- timezone conversion verified manually on sample rows
- newest stop is sufficiently in the future

### 10.2 Mapping-level validation

For each mapped provider stream:

- expected source appears in candidate list
- wrong diaspora/domestic variants do not map
- exact/override mapping is documented
- no unrelated fuzzy candidate becomes higher priority

### 10.3 Final-guide validation

Compare before/after:

- PK streams with current EPG
- total PK programme rows
- median next-24h coverage hours
- uncovered PK channels
- fallback source distribution
- conflict/quarantine counts
- overlap removals

### 10.4 Required negative tests

Explicitly test that:

- `PTV Home` cannot accidentally map to `PTV Global`
- `PTV Sports` cannot map to a similarly named sports channel
- `Green Entertainment` cannot map to an unrelated "Green" channel
- `HUM TV Europe` retains Europe schedule rather than domestic HUM
- `ARY Digital Asia` is not silently replaced by a domestic schedule unless their lineups are verified equivalent
- an empty first-party schedule falls through to the next candidate
- a stale first-party schedule falls through to the next candidate
- a partial BOL schedule does not block fallback during uncovered hours

---

## 11. How to prove the improvement is real

A successful Pakistan expansion is not measured by scraper count.

Use a before/after report like:

| Metric | Before | After | Required direction |
|---|---:|---:|---|
| PK linear streams | N | N | same |
| PK streams with current EPG | N | N | increase |
| PK streams with first-party EPG | N | N | increase |
| Uncovered PK streams | N | N | decrease |
| Wrong/conflicting PK mappings | N | N | no increase |
| PK rows dropped as stale | N | N | explainable |
| PK quarantined conflicts | N | N | no unexplained increase |
| Median coverage next 24h | N h | N h | increase |
| Median coverage next 72h | N h | N h | increase |

For every newly covered channel, manually spot-check at least three programme rows against the broadcaster site.

---

## 12. Recommended commits / change boundaries

Keep the implementation reviewable.

Suggested sequence:

1. `audit: capture Pakistan coverage baseline`
2. `feat: add common Pakistan source framework`
3. `feat: add PTV guide source`
4. `mapping: add verified PTV channel mappings`
5. `test: add PTV source and variant regression checks`
6. `feat: add Green Entertainment source`
7. `mapping: add verified Green mapping`
8. `feat: add partial BOL Entertainment schedule source`
9. `audit: compare Pakistan coverage before and after`
10. only then consider migrating existing legacy Pakistan scrapers into the new package

Do not combine PTV + Green + BOL + refactor + matcher changes into one commit.

---

## 13. Immediate next actions for Hermes

Hermes should execute the following in order:

### Step 1 — baseline

Read the current provider stream data and latest coverage/mapping artifacts and produce the Pakistan baseline described above.

### Step 2 — PTV technical discovery

Investigate the official PTV guide's scripts/network model until the exact schedule request is known and tested for multiple channels.

Deliverable before code:

```text
PTV endpoint/path
required parameters
request headers/cookies if any
sample response schema
channel-id mapping
timezone semantics
observed horizon
failure behavior
```

### Step 3 — Green technical discovery

Do the same for Green.

### Step 4 — implementation proposal

Before modifying production code, produce a small diff plan identifying:

- new files
- existing files to edit
- new source IDs
- exact provider channel mappings
- new tests/gates
- workflow changes

### Step 5 — implement PTV first

Run the full existing build and compare final output against baseline.

### Step 6 — add Green only after PTV is stable

### Step 7 — add BOL as a partial source with interval-aware fallback

### Step 8 — second-wave discovery

Proceed through A Plus, TV One, HUM Sitaray, HUM Masala, Urdu 1, then news channels.

---

## 14. Explicit non-goals

Do not:

- replace the current working Pakistan scrapers merely for code neatness
- trust a schedule because a search engine snippet says one exists
- add an old APK's static schedule as current EPG
- add generic XMLTV repos that merely mirror Sky Hawk or another existing source
- create 24-hour filler for partial schedules
- force domestic schedules onto diaspora feeds
- weaken current currency/conflict/overlap gates for the sake of coverage percentage
- make a new source workflow-required until it has demonstrated stability

---

## 15. Decision summary

The current pipeline already has the hard orchestration pieces. The best return is to deepen the **Pakistan first-party source layer**, not redesign the global EPG engine.

Recommended priority:

```text
1. PTV network dynamic guide
2. Green Entertainment dynamic guide
3. BOL Entertainment conservative partial guide
4. A Plus / TV One / HUM Sitaray / HUM Masala / Urdu 1
5. Pakistani news channels
6. surviving legacy guide backends only if independently proven current
```

All new work should be judged against the existing final guide, not against scraper output in isolation.

The governing rule remains:

> **Prefer a verified blank over a confident-looking wrong programme.**

That principle is already embedded in the repository and should remain the defining constraint of the Pakistan expansion.
