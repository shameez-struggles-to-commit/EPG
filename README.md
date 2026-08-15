# EPG Pipeline

Generates a merged XMLTV electronic program guide and publishes it to one
stable URL for TV players.

**Guide URL:**
```
https://shameez-struggles-to-commit.github.io/EPG/guide.xml.gz
```

Runs automatically on a schedule. See `pipeline/` and `scrapers/` for the code.

## Development

```sh
git clone https://github.com/shameez-struggles-to-commit/EPG.git
cd EPG
python3 pipeline/fetch_provider.py ./data   # provider creds via env
python3 pipeline/fetch_sources.py ./data
python3 pipeline/build_mapping.py --streams data/streams.json --sources-index data/sources_index.json --provider-index data/provider_index.json --callsigns data/call_signs.json -o data/mapping.json
python3 pipeline/build_pipeline.py --streams data/streams.json --mapping data/mapping.json --sources data/sources.json --provider data/provider.xml --pk data/pk_epg.json --out guide.xml.gz
```
