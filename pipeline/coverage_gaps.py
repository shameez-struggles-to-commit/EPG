#!/usr/bin/env python3
"""Coverage gap audit — deterministic answer to "did we match everything?"

Runs after the build. Emits public/coverage_gaps.json (deployed with the guide)
plus a human-readable summary on stdout. The Hermes watchdog/monthly crons
consume the artifact instead of re-deriving gap analyses each time.

Sections:
  per_country   linear streams vs covered, per country (top gaps first)
  uncovered     every uncovered linear name + classification label + best
                candidate hits in the downloaded sources (exact/fuzzy, with
                the gate status: 'allowed' = country gate permits it,
                'blocked' = a gate/diaspora rule blocked it)
  per_source    indexed channels, file size, programme-currency share
                (share of programmes whose stop is >= today — catches feeds
                that quietly go stale, like globetv did)
  trend         covered-name set vs the last deployed coverage_gaps.json
                (newly covered / newly lost since the previous run)

Usage: coverage_gaps.py --streams data/streams.json --mapping data/mapping.json
       --sources data/sources.json --sources-index data/sources_index.json
       --coverage data/coverage.json --out public/coverage_gaps.json
       [--prev-url https://.../coverage_gaps.json]
"""

import argparse
import datetime as dt
import gzip
import json
import os
import re
import sys
import urllib.request
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from matcher import SourceIndex, norm, is_non_linear

RADIO_NAME_RE = re.compile(r'\bradio\b|\bFM\b|^BBC - ', re.I)
PROG_STOP_RE = re.compile(r'<programme\s+[^>]*?stop="(\d{8})', re.I)
CAT_RE = re.compile(r'^([A-Za-z]{2,3})\s*\|')


def country_of(cat_name):
    m = CAT_RE.match((cat_name or '').strip())
    return m.group(1).upper() if m else '??'


def classify_uncovered(name, cat_name):
    """Label WHY a name is uncovered: event slot, radio, or genuine gap."""
    if is_non_linear(cat_name, name):
        return 'event'
    if RADIO_NAME_RE.search(name):
        return 'radio'
    return 'linear'


def read(path):
    if path.endswith('.gz'):
        return gzip.open(path, 'rb').read().decode('utf-8', errors='ignore')
    return open(path, 'r', errors='ignore').read()


def currency_share(path):
    """Share of programmes with stop >= today (0..1). Cheap single regex pass."""
    try:
        txt = read(path)
    except Exception:
        return None
    today = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d')
    stops = PROG_STOP_RE.findall(txt)
    if not stops:
        return 1.0 if '<programme' not in txt else 0.0
    return sum(1 for s in stops if s >= today) / len(stops)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--streams', required=True)
    ap.add_argument('--mapping', required=True)
    ap.add_argument('--sources', required=True)
    ap.add_argument('--sources-index', required=True)
    ap.add_argument('--coverage', default=None)
    ap.add_argument('--out', required=True)
    ap.add_argument('--prev-url', default=None)
    args = ap.parse_args()

    streams = json.load(open(args.streams))
    mapping = json.load(open(args.mapping))
    manifest = json.load(open(args.sources))
    sources_index = json.load(open(args.sources_index))
    coverage = json.load(open(args.coverage)) if args.coverage and os.path.exists(args.coverage) else {}

    # rebuild SourceIndex per source (shared with build_mapping logic)
    idx = {}
    for s, name_to_ids in sources_index.items():
        i = SourceIndex()
        for n, ids in name_to_ids.items():
            for cid in ids:
                i.add(n, cid)
        idx[s] = i

    # per-country
    mapped_names = {mp['name'] for mp in mapping.values() if mp.get('candidates')}
    lin = [s for s in streams
           if not is_non_linear(s.get('cat_name', ''), s.get('name', ''))]
    by_country = defaultdict(lambda: {'linear': 0, 'covered': 0, 'uncovered': []})
    for s in lin:
        cc = country_of(s.get('cat_name', ''))
        by_country[cc]['linear'] += 1
        if s['name'] in mapped_names:
            by_country[cc]['covered'] += 1
        else:
            by_country[cc]['uncovered'].append(s['name'])
    per_country = {
        cc: {'linear': v['linear'], 'covered': v['covered'],
             'uncovered': len(set(v['uncovered'])),
             'uncovered_names': sorted(set(v['uncovered']))[:60]}
        for cc, v in sorted(by_country.items(),
                            key=lambda x: -len(set(x[1]['uncovered'])))
    }

    # per uncovered name: best candidates + gate status (cheap approximation:
    # report exact/fuzzy hits per source without recomputing country gates —
    # the gates are build_mapping policy, not data)
    uncovered_detail = []
    seen_names = set()
    for s in lin:
        n = s['name']
        if n in mapped_names or n in seen_names:
            continue
        seen_names.add(n)
        nn = norm(n)
        hits = []
        for src in sources_index:
            e = idx[src].exact(n)
            if e:
                hits.append({'source': src, 'method': 'exact', 'id': e[0]})
                continue
            fz = idx[src].fuzzy(n, threshold=0.85, limit=1)
            if fz:
                hits.append({'source': src, 'method': 'fuzzy',
                             'score': round(fz[0][0], 2), 'id': fz[0][2]})
        if hits:
            uncovered_detail.append({
                'name': n, 'country': country_of(s.get('cat_name', '')),
                'label': classify_uncovered(n, s.get('cat_name', '')),
                'hits': hits[:5],
            })

    # per-source health
    per_source = {}
    for m in manifest:
        src = m['source']
        path = m.get('file')
        size = os.path.getsize(path) if path and os.path.exists(path) else None
        entry = {'file': os.path.basename(path) if path else None,
                 'size_bytes': size,
                 'indexed_channels': len(sources_index.get(src, {})),
                 'currency': None}
        if path and os.path.exists(path):
            entry['currency'] = currency_share(path)
        per_source[src] = entry

    # trend vs previously deployed artifact
    trend = None
    if args.prev_url:
        try:
            req = urllib.request.Request(args.prev_url, headers={'User-Agent': 'Mozilla/5.0'})
            prev = json.loads(urllib.request.urlopen(req, timeout=60).read())
            prev_covered = set(prev.get('covered_names', []))
            trend = {
                'prev_covered': len(prev_covered),
                'new_covered': len(mapped_names - prev_covered),
                'lost': len(prev_covered - mapped_names),
            }
        except Exception as e:  # noqa: BLE001
            trend = {'error': str(e)[:100]}

    out = {
        'generated': dt.datetime.now(dt.timezone.utc).isoformat(),
        'total_streams': len(streams),
        'linear_streams': len(lin),
        'linear_unique_names': len({s['name'] for s in lin}),
        'covered_names': sorted(mapped_names),
        'covered_channels': coverage.get('covered_channels'),
        'per_country': per_country,
        'uncovered_with_hits': uncovered_detail,
        'per_source': per_source,
        'trend': trend,
    }
    json.dump(out, open(args.out, 'w'), indent=1, ensure_ascii=False)

    # human summary
    n_un = sum(v['uncovered'] for v in per_country.values())
    n_cov = len(mapped_names)
    denom = len({s['name'] for s in lin})
    print(f'[gaps] mapping covers {n_cov}/{denom} linear unique names '
          f'({100 * n_cov / max(1, denom):.1f}%) | uncovered {n_un}')
    print(f'[gaps] uncovered names with exact/fuzzy hits in downloaded sources: '
          f'{len(uncovered_detail)}')
    # Only flag MOSTLY-stale sources here (<50% current). Daily files downloaded
    # yesterday naturally sit ~50% (their day-1 half is past); the real rot
    # signal (globetv-style) is near-0. Full values are in the JSON for trend.
    stale = [(s, round(e["currency"], 3)) for s, e in per_source.items()
             if e.get('currency') is not None and e['currency'] < 0.5]
    if stale:
        print(f'[gaps] mostly-stale sources: {stale}')
    if trend and 'error' not in trend:
        print(f'[gaps] trend vs deployed: +{trend["new_covered"]} new, '
              f'-{trend["lost"]} lost')
    print(f'[gaps] -> {args.out}')


if __name__ == '__main__':
    main()
