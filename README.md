# EPG Pipeline

Generates a merged XMLTV electronic program guide and publishes it to one
stable URL for TV players.

**Guide URL:**
```
https://shameez-struggles-to-commit.github.io/EPG/guide.xml.gz
```

Runs automatically on a schedule. See `pipeline/` and `scrapers/` for the code.

## Sources (2026-08-15 audit)

- **Sky hawk API** (awk.epgsky.com) — UK/DE/IT, ~400 channels matched
- **DStv API** — South Africa (SuperSport/SABC/e.tv)
- **epg.one** — Ukrainian channels (UA subset)
- **al7omed/bein-epg** — beIN MENA sports
- **epg.pw** — worldwide base
- **epgshare01** — 43 country files (US locals + EU + MENA + India)
- **mitthu786/tvepg** + **iptv-org** India sites — India
- **iptv-org** — 24 validated non-India grabbers (tvpassport, magenta.tv,
  tv.blue.ch, cosmotetv, cyta, allente, …)
- **7 custom Pakistani scrapers** (harpalgeo, geo.tv, hum.tv, arydigital,
  expressentertainment, geokahani)
- Provider's own xmltv.php (long tail)

A currency gate drops any programme whose end time is already past at build
time, so dead upstream feeds can never pollute the guide (this is what killed
globetvapp — all its feeds went stale in late 2025).

## Development

```sh
git clone https://github.com/shameez-struggles-to-commit/EPG.git
cd EPG
python3 pipeline/fetch_provider.py ./data   # provider creds via env
python3 pipeline/fetch_sources.py ./data
python3 pipeline/build_mapping.py --streams data/streams.json --sources-index data/sources_index.json --provider-index data/provider_index.json --callsigns data/call_signs.json -o data/mapping.json
python3 pipeline/build_pipeline.py --streams data/streams.json --mapping data/mapping.json --sources data/sources.json --provider data/provider.xml --pk data/pk_epg.json --out guide.xml.gz
```
