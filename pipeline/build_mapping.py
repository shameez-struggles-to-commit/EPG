#!/usr/bin/env python3
"""Build mapping.json for the provider's streams.

Combines:
  - PK overrides (scraped channels -> stream names)
  - epg.pw global index exact/fuzzy matching
  - iptv-org database alias matching
  - provider xmltv long-tail matching

Usage: build_mapping.py --streams streams.json --pw-index epgpw_index.json \
        --iptvorg channels.csv --pk pk_epg.json [--overrides overrides.yaml] -o mapping.json
"""
import argparse
import csv
import json
import re
import sys
from collections import defaultdict

sys.path.insert(0, '/dev/null')  # no-op
from matcher import Matcher, norm  # noqa: E402  (run from pipeline/ dir)


PK_OVERRIDES = {
    # stream_name (provider) -> PK scraper channel key
    'Har Pal Geo': 'geo_entertainment_pk',
    'Geo TV': 'geo_entertainment_pk',          # Geo TV == Har Pal Geo overseas feed
    'Hum TV Europe': 'hum_tv_europe_pk',
    'ARY Digital Asia': 'ary_digital_pk',
}

CATEGORY_HINTS = {
    'PK |': 'PK', 'IN |': 'IN', 'US |': 'US', 'UK |': 'GB', 'DE |': 'DE', 'IT |': 'IT',
    'FR |': 'FR', 'ES |': 'ES', 'GR |': 'GR', 'RO |': 'RO', 'PL |': 'PL', 'PT |': 'PT',
    'ZA |': 'ZA', 'CA |': 'CA', 'NL |': 'NL', 'PH |': 'PH', 'TH |': 'TH', 'DK |': 'DK',
    'SE |': 'SE', 'NO |': 'NO', 'FI |': 'FI', 'AU |': 'AU', 'TR |': 'TR', 'RU |': 'RU',
    'UA |': 'UA', 'IL |': 'IL', 'JP |': 'JP', 'AR |': 'AR', 'BR |': 'BR', 'MX |': 'MX',
    'AE |': 'AE', 'SA |': 'SA', 'AF |': 'AF', 'AL |': 'AL',
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--streams', required=True)
    ap.add_argument('--pw-index', required=True)
    ap.add_argument('--iptvorg', required=True)
    ap.add_argument('--pk', default=None)
    ap.add_argument('--provider-index', default=None)
    ap.add_argument('-o', '--out', required=True)
    args = ap.parse_args()

    streams = json.load(open(args.streams))
    cats = {c['category_id']: c['category_name'] for c in json.load(open('/tmp/xc_cats.json'))} \
        if False else {}
    pw_index = json.load(open(args.pw_index))
    rows = []
    with open(args.iptvorg, newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            rows.append({'id': row['id'], 'name': row['name'],
                         'alt_names': [a for a in (row.get('alt_names') or '').split('|') if a],
                         'country': row['country']})

    m = Matcher(pw_index, rows)
    m.build_index()

    # provider xmltv long-tail: display-name (normalized) -> pseudo-id 'prov:<display-name>'
    prov_by_name = {}
    if args.provider_index:
        pidx = json.load(open(args.provider_index))
        for dn in pidx.get('names_with_progs', []):
            prov_by_name[norm(dn)] = f'prov:{dn}'
        print(f'[matcher] provider names with programmes: {len(prov_by_name)}', file=sys.stderr)
    print(f'[matcher] pw names: {len(m.pw_by_name)} | iptv-org names: {len(m.io_by_name)}', file=sys.stderr)

    mapping = {}
    stats = defaultdict(int)
    for s in streams:
        name = s['name']
        if name in PK_OVERRIDES:
            mapping[name] = {'source': 'pk', 'source_id': PK_OVERRIDES[name], 'method': 'override', 'confidence': 1.0}
            stats['pk-override'] += 1
            continue
        hint = None
        cat = s.get('cat_name', '')
        for k, v in CATEGORY_HINTS.items():
            if k in cat:
                hint = v
                break
        r = m.match(name, hint)
        if r:
            mapping[name] = r
            stats[r['method']] += 1
            continue
        pn = norm(name)
        if pn in prov_by_name:
            mapping[name] = {'source': 'provider', 'source_id': prov_by_name[pn],
                             'method': 'provider-name', 'confidence': 0.9}
            stats['provider-name'] += 1
        else:
            stats['unmatched'] += 1
    json.dump(mapping, open(args.out, 'w'), indent=1)
    print(f'[mapping] {len(mapping)} mapped | stats: {dict(stats)}')


if __name__ == '__main__':
    main()
