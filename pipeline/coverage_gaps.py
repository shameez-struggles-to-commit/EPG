#!/usr/bin/env python3
"""Audit final-guide coverage by linear provider stream (not candidate names).

Public coverage_gaps.json retains the historic aggregate keys, now measured
against --guide. Without a readable guide measured fields are null/unavailable;
legacy build_covered_channels is explicitly separate. --private-out writes
stream IDs/names/canonical IDs and must stay local (never Pages or artifacts).
"""

import argparse
import datetime as dt
import gzip
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from matcher import SourceIndex, is_non_linear
from build_mapping import build_identity_map
from guide_health import measure_guide

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


def _totals(entries):
    values = list(entries)
    result: dict = {'linear': len(values)}
    for key in ('candidate', 'published', 'active', 'future'):
        result[key] = sum(bool(v[key]) for v in values) if all(v[key] is not None for v in values) else None
    for hours in (24, 48):
        key = 'usable_hours_next_%sh' % hours
        result[key] = round(sum(v[key] for v in values), 4) if all(v[key] is not None for v in values) else None
    return result


def measured_coverage(streams, mapping, guide_path, now):
    """One linear stream entry per denominator unit; XMLTV IDs never count as streams."""
    linear = [s for s in streams if not is_non_linear(s.get('cat_name', ''), s.get('name', ''))]
    identity = build_identity_map(streams)
    guide_available = False
    per_id = {}
    reason = None
    if guide_path:
        try:
            _, per_id = measure_guide(guide_path, now)
            guide_available = True
        except (OSError, ValueError, UnicodeError, ET.ParseError) as exc:
            reason = type(exc).__name__  # never put a possibly private path in the public report
    else:
        reason = 'guide_not_supplied'
    rows = {}
    for stream in linear:
        sid = str(stream.get('stream_id') or '').strip()
        if not sid or sid in rows:
            raise ValueError('linear streams require unique nonempty stream_id')
        candidate = bool(mapping.get(sid, {}).get('candidates'))
        # Prefer the exact ID selected by the build mapping, falling back to the
        # full-lineup collision map for entries omitted from a partial mapping.
        cid = mapping.get(sid, {}).get('canonical_id') or identity.get(sid)
        info = per_id.get(cid) if guide_available else None
        rows[sid] = {
            'name': stream.get('name', ''), 'country': country_of(stream.get('cat_name', '')),
            'class': classify_uncovered(stream.get('name', ''), stream.get('cat_name', '')),
            'canonical_id': cid, 'candidate': candidate,
            'published': bool(info) if guide_available else None,
            'active': info['active'] if info else (False if guide_available else None),
            'future': info['future'] if info else (False if guide_available else None),
            'usable_hours_next_24h': info['usable_hours_next_24h'] if info else (0 if guide_available else None),
            'usable_hours_next_48h': info['usable_hours_next_48h'] if info else (0 if guide_available else None),
        }
    summary = _totals(rows.values())
    summary.update({'status': 'available' if guide_available else 'unavailable',
                    'measured_at': now.isoformat(),
                    'reason': reason, 'denominator': 'linear_stream_entries',
                    'per_country': {cc: _totals(v for v in rows.values() if v['country'] == cc)
                                    for cc in sorted({v['country'] for v in rows.values()})},
                    'per_class': {cl: _totals(v for v in rows.values() if v['class'] == cl)
                                  for cl in sorted({v['class'] for v in rows.values()})}})
    return summary, rows


def load_json(path):
    with open(path, encoding='utf-8') as handle:
        return json.load(handle)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--streams', required=True)
    ap.add_argument('--mapping', required=True)
    ap.add_argument('--sources', required=True)
    ap.add_argument('--sources-index', required=True)
    ap.add_argument('--coverage', default=None)
    ap.add_argument('--out', required=True)
    ap.add_argument('--prev-url', default=None)
    ap.add_argument('--guide', help='final built XMLTV guide (.xml.gz); without it measurements are unavailable')
    ap.add_argument('--private-out', help='local-only per-stream diagnostics; NEVER publish/upload')
    ap.add_argument('--now', help='UTC measurement instant in ISO 8601 (default: current UTC)')
    args = ap.parse_args(argv)
    now = dt.datetime.fromisoformat(args.now) if args.now else dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        ap.error('--now must include timezone')

    streams = load_json(args.streams)
    mapping = load_json(args.mapping)
    manifest = load_json(args.sources)
    sources_index = load_json(args.sources_index)
    coverage = load_json(args.coverage) if args.coverage and os.path.exists(args.coverage) else {}

    # rebuild SourceIndex per source (shared with build_mapping logic)
    idx = {}
    for s, name_to_ids in sources_index.items():
        i = SourceIndex()
        for n, ids in name_to_ids.items():
            for cid in ids:
                i.add(n, cid)
        idx[s] = i

    # Count individual linear stream entries, never a same-name proxy.
    measured, stream_rows = measured_coverage(streams, mapping, args.guide, now)
    mapped_names = {r['name'] for r in stream_rows.values() if r['candidate']}
    published_names = {r['name'] for r in stream_rows.values() if r['published']} if measured['status'] == 'available' else set()
    lin = [s for s in streams
           if not is_non_linear(s.get('cat_name', ''), s.get('name', ''))]
    by_country = defaultdict(lambda: {'linear': 0, 'covered': 0, 'uncovered': []})
    for s in lin:
        cc = country_of(s.get('cat_name', ''))
        sid = str(s['stream_id'])
        by_country[cc]['linear'] += 1
        if stream_rows[sid]['published']:
            by_country[cc]['covered'] += 1
        elif measured['status'] == 'available':
            by_country[cc]['uncovered'].append(s['name'])
    per_country = {
        cc: {'linear': v['linear'],
             'covered': v['covered'] if measured['status'] == 'available' else None,
             'uncovered': v['linear'] - v['covered'] if measured['status'] == 'available' else None,
             'uncovered_names': sorted(set(v['uncovered'])) if measured['status'] == 'available' else []}
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
        if measured['status'] != 'available' or stream_rows[str(s['stream_id'])]['published'] or n in seen_names:
            continue
        seen_names.add(n)
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
    if args.prev_url and measured['status'] == 'available':
        try:
            req = urllib.request.Request(args.prev_url, headers={'User-Agent': 'Mozilla/5.0'})
            prev = json.loads(urllib.request.urlopen(req, timeout=60).read())
            if prev.get('measured', {}).get('status') != 'available':
                raise ValueError('previous report has no measured guide coverage')
            prev_covered = set(prev.get('covered_names', []))
            trend = {
                'prev_covered': len(prev_covered),
                'new_covered': len(published_names - prev_covered),
                'lost': len(prev_covered - published_names),
            }
        except Exception as e:  # noqa: BLE001
            trend = {'error': str(e)[:100]}

    out = {
        'generated': dt.datetime.now(dt.timezone.utc).isoformat(),
        'total_streams': len(streams),
        'linear_streams': len(lin),
        'linear_unique_names': len({s['name'] for s in lin}),
        'covered_names': sorted(published_names) if measured['status'] == 'available' else None,
        'candidate_names': sorted(mapped_names),
        'covered_channels': measured['published'],  # legacy key, now measured linear streams
        'build_covered_channels': coverage.get('covered_channels'),  # separate historical build statistic
        'measured': measured,
        'per_country': per_country,
        'uncovered_with_hits': uncovered_detail,
        'per_source': per_source,
        'trend': trend,
    }
    if args.private_out:
        private_path = os.path.realpath(args.private_out)
        if (private_path == os.path.realpath(args.out) or 'public' in private_path.split(os.sep)):
            ap.error('--private-out must not be the public report or under a public/ directory')
        with open(args.private_out, 'w', encoding='utf-8') as handle:
            json.dump({'generated': now.isoformat(), 'guide_status': measured['status'],
                       'mapping_provenance': 'supplied_mapping', 'streams': stream_rows},
                      handle, indent=1, ensure_ascii=False)
    with open(args.out, 'w', encoding='utf-8') as handle:
        json.dump(out, handle, indent=1, ensure_ascii=False)

    # human summary
    if measured['status'] == 'available':
        print(f'[gaps] published {measured["published"]}/{measured["linear"]} linear streams; '
              f'active {measured["active"]}, future {measured["future"]}, '
              f'usable next24 {measured["usable_hours_next_24h"]:.1f} stream-hours')
    else:
        print(f'[gaps] measured coverage UNAVAILABLE ({measured["reason"]}); '
              f'{measured["candidate"]} candidate-bearing of {measured["linear"]} linear streams')
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
