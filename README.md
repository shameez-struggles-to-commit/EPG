# EPG Pipeline

Self-hosted IPTV Electronic Program Guide generator. Takes an Xtream Codes
subscription, matches every regular (linear) channel against free public EPG
sources, and publishes a single merged XMLTV guide that TiviMate (NVIDIA SHIELD)
and other players consume via one stable HTTPS URL.

**Output URL (share this with family members):**
```
https://shameez-struggles-to-commit.github.io/EPG/guide.xml.gz
```

---

## How it runs

The pipeline runs **entirely in GitHub's cloud** (GitHub Actions), not on the
home Mac. A fresh Linux runner runs daily at `04:00 UTC` (also on push to
`main` and manual `workflow_dispatch`), builds the guide, and deploys it to
GitHub Pages (free CDN). Nothing needs to run at home.

- **Cost:** $0 — the repo is public, so Actions minutes are unlimited.
- **Schedule:** daily (required — Indian sources publish only 1–2 days ahead).

## Architecture

```
GitHub Actions (cloud, daily 04:00 UTC)
│
├─ fetch_provider.py   → Xtream player_api: streams + categories + xmltv.php
│                         (captures epg_channel_id — the TiviMate match key)
├─ pk_scrapers.py      → Pakistani broadcasters (Geo / Hum / ARY) → JSON
├─ iptv-org/epg        → Node grabber, Indian sources (JioTV/TataPlay/DishTV/
│                         AirtelXstream/Zee5) → io_india.xml
├─ fetch_sources.py    → epg.pw global + epgshare01 country files (US locals,
│                         UK/IE, CA, EU, AU, ZA, PH, DK, TR, TH, …) + indexes
│
├─ build_mapping.py    → per-stream fallback-cascade candidate list (ordered),
│                         country-gated, with US call-sign matching
├─ build_pipeline.py   → pick first candidate with data, merge → guide.xml.gz
│
└─ deploy-pages        → publish public/guide.xml.gz to GitHub Pages
```

### EPG source layers (in priority order)

1. **PK scrapers** (`scrapers/pk_scrapers.py`) — Pakistani broadcasters no free
   aggregator covers: Har Pal Geo, Geo TV, Hum TV Europe, ARY Digital.
2. **iptv-org** — curated Indian grabbers (best India data).
3. **epgshare01** — rich per-country XMLTV (WebGrab+Plus based). The big win
   for US locals (via call-sign matching) and European channels.
4. **epg.pw** — broad worldwide base (15k+ channels).
5. **Provider's own `xmltv.php`** — long tail (US locals, UK, misc).

### Channel matching (the core problem)

TiviMate matches playlist `tvg-id` / `epg_channel_id` ↔ XMLTV `<channel id>`
**strictly**. The pipeline therefore:

- Sets each channel's **canonical id = the provider's `epg_channel_id`** when it
  is a real (non-junk) id — this is what TiviMate matches natively on an
  Xtream playlist. Otherwise it uses the raw stream name (TiviMate name
  fallback).
- Uses a **fallback cascade**: each stream maps to several candidate sources in
  priority order, and the first one with actual programme data wins. A channel
  whose top source is empty falls through to the next — this is what fixed the
  ~1,900 channels the old single-source mapping silently dropped.
- **Country-gates** the matching: a German stream only matches the German
  source file (no cross-country false positives).
- Matches **US locals by call sign** (`FOX: FL | Tampa | WTVT` → `WTVT-DT`).
- Uses a **symmetric Dice fuzzy match** (≥0.85, ≥2 shared tokens) as a last
  resort — it deliberately rejects subset false-positives (`DW Español` ≠ `DW`).
- Skips **non-linear channels** (24/7 restreams, VIP sports, radio, event hubs,
  adult) — no EPG applies to them.

## Repository layout

```
.github/workflows/build-epg.yml   GitHub Actions pipeline definition
scrapers/pk_scrapers.py           Pakistani broadcaster scrapers → JSON
pipeline/fetch_provider.py        Xtream API fetch (streams/categories/xmltv)
pipeline/fetch_sources.py         download epg.pw + epgshare01 + build indexes
pipeline/matcher.py               name normalization + Dice fuzzy + source index
pipeline/build_mapping.py         fallback-cascade mapping + call signs
pipeline/build_pipeline.py        merge layers → guide.xml.gz
data/                             build artifacts (gitignored)
public/                           deployed to GitHub Pages
```

## Credentials & secrets

Provider credentials and the ntfy token are stored as **GitHub repository
Secrets** (encrypted, only exposed to Actions runners):

| Secret | Purpose |
|---|---|
| `IPTV_SERVER` | Xtream host |
| `IPTV_USER` / `IPTV_PASS` | Xtream subscriber credentials |
| `NTFY_URL` / `NTFY_TOKEN` | self-hosted ntfy for health + coverage-drop alerts |

```sh
gh secret set IPTV_SERVER -b "host" --repo shameez-struggles-to-commit/EPG
```

## GitHub Pages setup (already done)

Pages deploys from Actions (`build_type: workflow`):
```sh
gh api repos/shameez-struggles-to-commit/EPG/pages -X PUT -f build_type=workflow
```

## Local development

```sh
git clone https://github.com/shameez-struggles-to-commit/EPG.git
cd EPG

# 1. fetch provider data (needs creds in env)
IPTV_SERVER=... IPTV_USER=... IPTV_PASS=... python3 pipeline/fetch_provider.py ./data

# 2. PK scrapers
python3 scrapers/pk_scrapers.py ./data/pk_epg.json

# 3. iptv-org India grab (clone + npm install, then)
#    npm run grab --- --sites=jiotv.com,... --output=data/io_india.xml

# 4. epg.pw + epgshare01 (set ESHARE_FILES to a subset for faster dev)
python3 pipeline/fetch_sources.py ./data

# 5. map + merge
python3 pipeline/build_mapping.py \
  --streams data/streams.json --sources-index data/sources_index.json \
  --io data/io_india.xml --provider-index data/provider_index.json \
  --callsigns data/call_signs.json -o data/mapping.json
python3 pipeline/build_pipeline.py \
  --streams data/streams.json --mapping data/mapping.json \
  --sources data/sources.json --io data/io_india.xml \
  --provider data/provider.xml --pk data/pk_epg.json \
  --out guide.xml.gz --coverage-out data/coverage.json
```

All scripts are stdlib-only and Python 3.9+ compatible.

## Coverage

Measured on the linear (regular) channel set only — 24/7 / VIP / radio / event
channels are excluded because no EPG applies to them. The exact number is
printed each build in `public/coverage.txt`; the last validated local run
covered ~2,700 linear channels across US, UK, EU, India, Pakistan and more.

## TiviMate integration (one-time)

Settings → EPG → EPG Sources → add the guide URL → assign it to the Xtream
playlist → update interval 24h. Channels whose `epg_channel_id` the provider
set match automatically; the rest match by name; any stragglers are fixed once
via long-press → "Assign EPG" (persistent).
