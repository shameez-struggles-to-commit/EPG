#!/usr/bin/env python3
"""Generate the full 107-stream audit table (mandate item 5 shape)."""
import json, gzip, re, sys, datetime as dt
sys.path.insert(0, '/Users/shameez/workspace/epg/pipeline')
from build_mapping import build_collision_split, is_non_linear

streams = json.load(open('/Users/shameez/workspace/epg/data/streams.json'))
identity = build_collision_split(streams)
mapping = json.load(open('/Users/shameez/workspace/epg/data/mapping.json'))

d = gzip.open('/tmp/epg_issue/guide.xml.gz', 'rt', errors='ignore').read()
ch_rows = {}
for m in re.finditer(r'<programme start="(\d{14}) [+\-]\d{4}" stop="(\d{14}) [+\-]\d{4}" channel="([^"]+)"[^>]*>((?:(?!</programme>).)*)</programme>', d, re.S):
    cid = m.group(3)
    t = re.search(r'<title[^>]*>([^<]+)</title>', m.group(4))
    ch_rows.setdefault(cid, []).append((m.group(1), m.group(2), t.group(1) if t else '?'))

now_s = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d%H%M%S')
plus24 = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=24)).strftime('%Y%m%d%H%M%S')

GENERIC = re.compile(
    r'news headlines|news bulletin|news flash|news update|^news$|^tv one$|'
    r'to be announced|^tba$|no match|^grace tv$|^ptv live$|placeholder', re.I)

WRONG = {'ATV': 'Austria feed via epg.pw', 'Capital TV': 'UK Capital radio via epg.pw',
         'Hum Masala': 'UK1 HUM.Masala.uk carries Hum NEWS rows', 'Grace Network': 'all rows "Grace TV"'}
GENERIC_ONLY = {'92 News': 'generic hourly bulletins'}
PROVEN_FULL = {'Hum Sitaray', 'TV One Global', 'Madani Channel Urdu', 'Hum Masala'}
PROVEN_PARTIAL = {'Abb Takk', 'News One', 'Duniya News'}

pk = [s for s in streams if re.match(r'^PK\s*\|', s.get('cat_name', ''))]
lin = [s for s in pk if not is_non_linear(s.get('cat_name', ''), s.get('name', ''))]
assert len(lin) == 107

out = []
for s in lin:
    name, cat = s['name'], s.get('cat_name', '')
    sid = str(s.get('stream_id', '')).strip()
    cid = identity.get(sid, s.get('epg_channel_id') or name)
    rows = ch_rows.get(cid, [])
    cur = [r for r in rows if r[1] >= now_s]
    cur24 = [r for r in rows if r[0] <= now_s <= r[1] or now_s <= r[0] <= plus24]
    newest = max((r[1] for r in rows), default='')
    real = [r for r in rows if not GENERIC.search(r[2])]
    if name in WRONG:
        st = 'rejected_wrong_feed'
    elif name in GENERIC_ONLY:
        st = 'generic'
    elif not rows:
        st = 'blank'
    elif len(real) >= 8 and len(real) >= 0.5 * len(rows):
        st = 'full'
    elif real:
        st = 'partial'
    else:
        st = 'generic'
    if name in PROVEN_FULL:
        pr = 'full' if name != 'Hum Masala' else 'full(repaired)'
    elif name in PROVEN_PARTIAL:
        pr = 'partial'
    elif name in WRONG:
        pr = 'rejected'
    elif name in GENERIC_ONLY or st == 'generic':
        pr = 'blank'
    else:
        pr = st
    # source/market from local mapping (Aug-15 snapshot — labelled)
    mp = mapping.get(sid, {})
    cands = mp.get('candidates', []) if isinstance(mp, dict) else []
    src = ','.join(sorted({c.get('source', '') for c in cands})) if cands else ''
    out.append((name, cat, st, len(rows), len(cur), len(cur24), newest[:8], src, pr))

hdr = ("| # | name | category | baseline | rows | current | next-24h | newest_stop | "
       "mapped_sources(Aug-15 snapshot) | projected |")
sep = "|---|------|----------|----------|-----:|--------:|---------:|-------------|----------------------------------|-----------|"
print(hdr)
print(sep)
for i, (name, cat, st, n, cur, c24, new, src, pr) in enumerate(sorted(out), 1):
    print(f"| {i} | {name} | {cat.split('|')[0].strip()} | {st} | {n} | {cur} | {c24} | {new} | {src[:40]} | {pr} |")

from collections import Counter
print("\nbaseline:", dict(Counter(r[2] for r in out)))
print("projected:", dict(Counter(r[8] for r in out)))
