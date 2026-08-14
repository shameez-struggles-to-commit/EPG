# EPG Pipeline

Self-hosted IPTV Electronic Program Guide generator. Takes an Xtream Codes
subscription, scrapes/matches EPG data from free public sources, and publishes
a single merged XMLTV guide that TiviMate (NVIDIA SHIELD) and other players
consume via one stable HTTPS URL.

**Current output URL (share this with family members):**
```
https://shameez-struggles-to-commit.github.io/EPG/guide.xml.gz
```

---

## How it runs (where the compute happens)

The pipeline runs **entirely in GitHub's cloud** — it does NOT run on the home
server / Mac mini. GitHub Actions spins up a fresh Linux runner on schedule,
builds the guide, and deploys it to GitHub Pages (free CDN). Nothing needs to
be running at home for this to keep updating.

- **Schedule:** daily at `04:00 UTC` (also on every push to `main`, and manual
  `workflow_dispatch`).
- **Cost:** $0. The repo is public, so GitHub Actions minutes are unlimited
  (the 2,000 min/month cap only applies to *private* repos).
- **Output:** `guide.xml.gz` served from GitHub Pages with a permanent URL.
  Family members in any country point TiviMate at the same URL.

## Architecture

```
GitHub Actions (cloud, daily 04:00 UTC)
│
├─ 1. fetch_provider.py   → Xtream player_api: streams + categories + xmltv.php
│                           (credentials from repo Secrets)
├─ 2. pk_scrapers.py      → scrape Pakistani broadcaster sites (see below)
├─ 3. iptv-org/epg        → Node grabber, Indian sources (JioTV/TataPlay/
│                           DishTV/AirtelXstream/Zee5)
├─ 4. epg.pw global feed  → 15,586 channels / 1.19M programmes (worldwide base)
│
├─ build_mapping.py       → match provider channel names → source channel ids
│                           (overrides + exact + iptv-org alt_names + fuzzy)
├─ build_pipeline.py      → merge all layers → guide.xml.gz
│                           Layer priority: PK > iptv-org > epg.pw > provider
│
└─ deploy-pages           → publish public/guide.xml.gz to GitHub Pages
```

### EPG source layers (in priority order)

1. **PK scrapers** (`scrapers/pk_scrapers.py`) — custom Python scrapers for
   Pakistani broadcasters that NO free aggregator covers:
   - Har Pal Geo (Geo Entertainment) — `harpalgeo.tv/tv-schedule`
   - Hum TV Asia — `hum.tv/schedule`
   - Hum TV Europe — `hum.tv/schedule-europe`
   - ARY Digital — `arydigital.tv/schedule`
   - (Geo Kahani noted as TODO — different DOM)
2. **iptv-org/epg** — the reference open-source grabber (3,209★). Currently
   runs 5 Indian sources; the repo has 251 site grabbers available for
   expansion (see "Expansion roadmap").
3. **epg.pw** — free global XMLTV feed (AI-aggregated, 15k+ channels).
4. **Provider's own `xmltv.php`** — 1,952 channels, ~1,039 with real
   programme linkage (US locals, misc long-tail).

## Repository layout

```
.github/workflows/build-epg.yml   GitHub Actions pipeline definition
scrapers/pk_scrapers.py           Pakistani broadcaster scrapers → JSON
pipeline/fetch_provider.py        Xtream API fetch (streams/categories/xmltv)
pipeline/matcher.py               name normalization + fuzzy matching
pipeline/build_mapping.py         channel-name → source-id mapping
pipeline/build_pipeline.py        merges layers → guide.xml.gz
pipeline/streams.json             provider channel snapshot (build artifact)
pipeline/mapping.json             mapping snapshot (build artifact)
public/                           deployed to GitHub Pages
```

## Credentials & secrets

Provider credentials and the ntfy token are stored as **GitHub repository
Secrets** (encrypted, only exposed to Actions runners — never in the repo):

| Secret | Purpose |
|---|---|
| `IPTV_SERVER` | Xtream host (e.g. `gtvprem.com`) |
| `IPTV_USER` / `IPTV_PASS` | Xtream subscriber credentials |
| `NTFY_URL` / `NTFY_TOKEN` | self-hosted ntfy for weekly health ping |

Set them with the `gh` CLI:
```sh
gh secret set IPTV_SERVER -b "gtvprem.com" --repo shameez-struggles-to-commit/EPG
```

## GitHub Pages setup (already done)

Pages is configured to deploy from Actions (`build_type: workflow`), enabled via:
```sh
gh api repos/shameez-struggles-to-commit/EPG/pages -X PUT -f build_type=workflow
```

## Validation (verified working)

Deployed guide was downloaded and validated 2026-08-14:
- XMLTV well-formed ✓
- 1,554 channels / 192,065 programmes (6.5 MB gzipped)
- Pakistan: Har Pal Geo (162), Hum TV Europe (28), ARY Digital (34), Geo News (130)
- India: Star Plus (80), Sony SAB (148), Zee TV (224), Colors (93)
- UK: BBC One (60)

## Current coverage & known gaps

- **8,511 total streams** in the provider lineup.
- **1,554 covered** with programme data.
- **~7,000 of the uncovered are NOT real channels** — 24/7 movie restreams,
  VIP sports restreams (ESPN+/Fifa+/FloCollege/FloHockey), radio, and live
  event hubs (EPL/NFL/MLB/NBA/NHL). No EPG data exists anywhere for these.
- **~3,454 real linear channels remain uncovered**, dominated by:
  - US locals ~596 (NBC/ABC/CBS/Fox/PBS/CW affiliates by city)
  - European linear ~1,155 (IT 192, DE 176, GR 170, RO 168, ES 160, FR 153,
    PT 69, PL 67, TR 33, plus TH/AU/ZA/NL/BE/CH/etc.)
  - Pakistani news/sports ~87 (Samaa, Dawn, Dunya, Bol, 92 News, etc.)
  - Assorted sports (EFL leagues, Sky Sports+, national leagues)

## Expansion roadmap (next session)

The highest-yield remaining work, in priority order:

1. **epgshare01.online country files** — pre-generated WebGrab+Plus XMLTV for
   ~60 countries including `US_LOCALS1`, `DE1`, `IT1`, `GR1`, `RO1/RO2`, `ES1`,
   `FR1`, `PT1`, `PL1`, `TR1/TR3`. One download + match per country, far
   cheaper than running 15 grabbers. This is the fastest path to closing the
   European + US-locals gap.
2. **iptv-org grabbers for uncovered regions** — `tvtv.us`, `tvpassport.com`,
   `zap2it.com`, `directv.com` (US locals); `guidatv.sky.it`,
   `mediasetinfinity.mediaset.it`, `raiplay.it` (IT); `movistarplus.es`,
   `orangetv.orange.es` (ES); `cosmotetv.gr`, `digea.gr` (GR);
   `programetv.ro` (RO); `france.tv`, `canalplus.com` (FR); `nostv.pt`,
   `rtp.pt`, `meo.pt` (PT); `programtv.onet.pl` (PL); `tvplus.com.tr`,
   `turksatkablo.com.tr`, `digiturk.com.tr` (TR).
3. **More PK news scrapers** — Samaa, Dawn, Dunya, Bol News, 92 News, Aaj,
   Abb Takk, Express News, GNN, etc. (many have schedule pages; verify each).
4. **Matcher improvements** — the fuzzy tier only caught 69 channels; a manual
   override table for the ~100 top real channels would close the long tail.
   iptv-org `alt_names` are already used; transliteration (Devanagari→Latin)
   not yet.

### Schedule/freshness consideration (important)

Indian EPG sources only publish **1–2 days ahead** (JioTV 2d, TataPlay 1d).
If the build ran weekly, Indian channels would be blank for 5–6 days of the
week. Because the repo is public (unlimited Actions minutes), daily runs are
free and keep the short-horizon sources fresh. Recommended: keep daily.

## Local development

```sh
git clone https://github.com/shameez-struggles-to-commit/EPG.git
# fetch provider data (needs creds in env)
IPTV_SERVER=... IPTV_USER=... IPTV_PASS=... python3 pipeline/fetch_provider.py ./data
# run PK scrapers
python3 scrapers/pk_scrapers.py ./data/pk_epg.json
# clone iptv-org/epg, npm install, then:
#   npm run grab --- --sites=jiotv.com,... --output=./data/io_india.xml
# download epg.pw global: https://epg.pw/xmltv/epg.xml.gz
# build mapping + guide
python3 pipeline/build_mapping.py --streams data/streams.json \
  --pw-index data/epgpw_index.json --iptvorg data/channels.csv \
  --pk data/pk_epg.json --provider-index data/provider_index.json -o data/mapping.json
python3 pipeline/build_pipeline.py --streams data/streams.json \
  --mapping data/mapping.json --pw data/epgpw.xml.gz --io data/io_india.xml \
  --provider data/provider.xml --pk data/pk_epg.json --out guide.xml.gz
```

Note: `gh` CLI is at `/opt/homebrew/bin/gh` (add to PATH). Git credentials
live in the macOS keychain (account `shameez-struggles-to-commit`).

## Related research

Deep research reports (market survey, technical mechanics, tooling, TiviMate
integration) are in `~/workspace/iptv-epg-research/` on the home server.
