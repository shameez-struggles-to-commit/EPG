# Name-Variant Sweep — Alias & norm() Gap Report

READ-ONLY audit of `/Users/shameez/workspace/epg` (run 2026-08-14).
Sweeps **all 1,520 currently-uncovered linear stream names** (streams with no
mapping candidate) against the 17 stable diaspora/South-Asian sources
(epg.pw, tvepg, epgshare01 IN1/IN2/IN4/ASIANTELEVISION1/UK1/IE1/US2/AE1,
iptv-org tvpassport/tv24/tvireland/allente/epg.112114.xyz/tvinsider/dstv),
confirming **programme presence** on every hit.

Headline: **~30 channels are fixable**, but the biggest single win is a tiny
`norm()` change (digit-split) that fixes ~13 of them at once. The Pakistani
gap is mostly *not* a naming problem — those channels simply have no EPG in
any downloaded source.

---

## 1. norm() rule changes (recommended — fix channels with no alias entry)

### 1a. Digit-concatenation split  ★ highest value / lowest risk
`norm()` tokenizes `News 18` → `["news","18"]` but `News18` → `["news18"]`, so
the two never compare equal. Insert a space at every letter↔digit boundary
before tokenizing:

```python
# in norm(), after lowercasing and quality/country stripping:
s = re.sub(r'(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])', ' ', s)
```

Channels this fixes (no alias needed), all confirmed to have live programmes:

| country | provider name | source name | source | prog |
|---|---|---|---|---|
| IN | News 18 Lokmat | News18 Lokmat | epgshare01:IN1 | 191 |
| IN | News 18 Gujarati | News18 Gujarati | epgshare01:IN1 | 189 |
| IN | News 18 Kannada | News18 Kannada | tvepg | 48 |
| IN | News 18 Punjab | NEWS18 PUNJAB | epgshare01:IN4 | 47 |
| IN | News 18 Bengali | News18 Bangla | tvepg | 35 |
| IN | News 7 Tamil | News7 Tamil | epgshare01:IN1 | 184 |
| IN | IBC 24 | IBC24 | epgshare01:IN1 | 186 |
| IN | Mh 1 Music | mh1 (Music) | epgshare01:IN1 | 71 |
| PK | MTA 1 World | MTA1 World HD | epg.pw | 145 |
| TR | TEVE 2 / TEVE 2 HD | Teve2 HD | epg.pw | 54 |
| AU | 7 Two | 7two | epg.pw | 201 |
| DE | ESports 1 | eSports1 | epg.pw | 24 |

Also supersedes the manual `NAME_ALIASES` entries `TV 9 Gujarati/Kannada/
Marathi/Telugu` (they are the same `TV 9`↔`TV9` gap).

### 1b. '&' / 'and' empty-norm bug  ★ fixes "& TV"
`norm()` does `re.sub(r'[^\w\s]',' ',s)` (turns `&` into a space) and puts
`'and'` in `FILLER`. So `& TV`, `And TV`, `&TV`, `&tv HD` **all normalize to
the empty string**, and the source `&TV` channels are dropped from
`sources_index.json` at index-build time (`SourceIndex.add` returns early on
empty norm). Two-line fix:

```python
s = s.replace('&', ' and ')          # BEFORE the punctuation strip
# remove 'and' from FILLER  (keep it as a real token)
```

Then `& TV` / `And TV` / `&TV` all normalize to `and` (non-empty) and match
each other exactly. Confirmed live: epg.pw `and TV` (102 prog), tvepg `And TV`
(42), tvepg `&tv HD` (34). This also makes the existing `& flix`/`& pictures`/
`& prive` matches robust instead of lucky.

### 1c. Optional: singular/plural with a stoplist
Strip a trailing `s` on tokens **except** a stoplist (`news, sports, series,
arts, plus, class, status, logos, …`). Fixes: `Maha Movie→Maha Movies`,
`SuperSport Schools→SuperSport School HD`, `Alfa Dramas→Alfa Drama`,
`Sky Cinema Highlight→Sky Cinema Highlights HD`, `Channel24→Channels 24`.
Risk note: without the stoplist this collapses `Logos TV → Logo` (wrong). If
you'd rather not touch this globally, use the explicit aliases in §2 instead.

### 1d. Optional: regional synonym map
`bengali→bangla`, `oriya→odia`, `gujrati→gujarati`, `telgu→telugu` applied as
tokens. Fixes `News 18 Bengali → News18 Bangla` cleanly (combine with 1a).

---

## 2. Proposed NAME_ALIASES entries (provider name → source display-name)

Add these to `build_mapping.py::NAME_ALIASES`. All targets verified to carry
live programmes (programme count in parentheses). Entries tagged `[digits]` are
redundant once you apply §1a — keep only if you skip that change.

### INDIAN (IN)
```python
'Maha Movie': 'Maha Movies',                 # singular/plural (tvepg/IN1, 20)
'& TV': 'And TV',                            # ampersand — needs §1b index rebuild too
'Tamil Vision': 'Tamil Vision (Canada)',     # region parenthetical (epg.pw, 73)
'News 18 Bengali': 'News18 Bangla',          # synonym bengali→bangla (tvepg, 35)  [needs §1a]
'News 18 Lokmat': 'News18 Lokmat',           # digit [needs §1a] (IN1, 191)
'IBC 24': 'IBC24',                           # digit [needs §1a] (IN1, 186)
'Mh 1 Music': 'mh1 (Music)',                 # digit [needs §1a] (IN1, 71)
'Asianet News Tamil': 'Asianet News',        # regional feed — REVIEW (IN1, 225)
'India News Uttar Pradesh': 'India News',    # regional feed — REVIEW (IN1, 30)
'Samachar Plus Rajasthan': 'Samachar Plus',  # regional feed — REVIEW (IN1, 184)
```

### PAKISTANI (PK)
```python
'MTA 1 World': 'MTA1 World HD',              # digit [needs §1a] (epg.pw, 145)
'Prime Canada TV': 'Prime Asia TV SD',       # region — REVIEW (epg.pw, 82)
```

### BANGLADESHI (BN)
```python
'Channel i': 'Channel i (Bangladesh)',       # region parenthetical (tvpassport, 7)
'Channel24': 'Channels 24',                  # singular/plural (UK1, 143)
```

### OTHER (diaspora / Europe)
```python
'Star Gold UK | HD |': 'Star Gold',          # region suffix (epg.pw, 57) — also covers SD/FHD variants
'EWTN US': 'EWTN',                           # region suffix (IE1, 161)
'SuperSport Schools': 'SuperSport School HD',# singular/plural (epg.pw, 65)
'Sky Cinema Highlight': 'Sky Cinema Highlights HD',  # singular/plural (epg.pw, 39)
'Alfa Dramas': 'Alfa Drama',                 # singular/plural (AE1, 84)
'TEVE 2': 'Teve2 HD',                        # digit [needs §1a] (epg.pw, 54)
'7 Two': '7two',                             # digit [needs §1a] (epg.pw, 201)
'ESports 1': 'eSports1',                     # digit [needs §1a] (epg.pw, 24)
```

---

## 3. Do NOT add (false positives found by the sweep)

| provider name | would match | why it's wrong |
|---|---|---|
| Raj News (IN) | RAJ NEWS TAMIL (IN4) | Raj News is Telugu; the Tamil feed is a different channel |
| Logos TV (ES) | Logo (US2) | singularization collapses two unrelated brands |
| News 1 (TH) | News 1 India (IN1) | Thai channel vs Indian channel (region "India" is a different network) |
| TV7 News (IT) | News7 Tamil | cross-country token collision |
| Bangla TV (BN) | Bengali (AE1) | "Bengali" is a generic UAE channel, not Bangla TV |
| KTN News (PK) | KTN (dstv) | KTN (Kenya) ≠ KTN News |
| M1 (UKR) | M-1 Global | MMA promotion ≠ Ukrainian M1 |
| Food Food TV (UK) | Food Network | `network` filler + word-dedup collision — use existing `Food Food→Foodxp` alias |

---

## 4. Why the Pakistani gap is mostly NOT name variants

~81 PK names are uncovered, but only 2 are name-variant fixes (MTA 1 World,
Prime Canada TV). The rest — PTV Home/News/Sports/World, A Sports, Bol
Entertainment/News, Geo Super/Tez, Hum Sitaray, Aaj, Dawn News (all variants),
8XM, Filmax, See TV, Sindh TV, Urdu 1, Khyber News, … — have **no channel with
programmes in any downloaded source**. UK1 carries `Dunya News`/`PTV Global`
but both have **0 programmes** (empty `<channel>` entries). The 7 custom PK
scrapers + epg.pw/ASIANTELEVISION1 only cover the ARY/Geo/HUM/Express/Samaa
core (already matched). Fixing the long tail requires a new Pakistani EPG
source, not aliases.

Same for BN: only `Channel i` (tvpassport) + `Channel24`(→Channels 24) +
`Bangla TV`(review) are present anywhere; the other ~31 BN names are absent.

---

## 5. Files

- `audit_output/variant_matches.json` — full per-name match detail (all 1,520
  uncovered names, every candidate source channel with programme counts).
- `audit_name_variants.py` — the sweep itself (re-runnable; read-only vs data).
