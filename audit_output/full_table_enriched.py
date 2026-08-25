#!/usr/bin/env python3
"""Enriched 107-row table with mandate-item-5 candidate fields."""
import json, gzip, re, sys, datetime as dt
sys.path.insert(0, '/Users/shameez/workspace/epg/pipeline')
from build_mapping import build_collision_split, is_non_linear

streams = json.load(open('/Users/shameez/workspace/epg/data/streams.json'))
identity = build_collision_split(streams)
d = gzip.open('/tmp/epg_issue/guide.xml.gz', 'rt', errors='ignore').read()
ch_rows = {}
for m in re.finditer(r'<programme start="(\d{14}) [+\-]\d{4}" stop="(\d{14}) [+\-]\d{4}" channel="([^"]+)"[^>]*>((?:(?!</programme>).)*)</programme>', d, re.S):
    t = re.search(r'<title[^>]*>([^<]+)</title>', m.group(4))
    ch_rows.setdefault(m.group(3), []).append((m.group(1), m.group(2), t.group(1) if t else '?'))

now_s = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d%H%M%S')
plus24 = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=24)).strftime('%Y%m%d%H%M%S')
GENERIC = re.compile(r'news headlines|news bulletin|news flash|news update|^news$|^tv one$|'
                     r'to be announced|^tba$|no match|^grace tv$|^ptv live$|placeholder', re.I)
WRONG = {'ATV', 'Capital TV', 'Hum Masala', 'Grace Network'}
GEN_ONLY = {'92 News'}
PFULL = {'Hum Sitaray', 'TV One Global', 'Madani Channel Urdu', 'Hum Masala'}
PPART = {'Abb Takk', 'News One', 'Duniya News'}

# curated candidate-audit column (this round's measured candidates, per stream name)
AUDIT = {
    'Duniya News': 'tv24 dunya-news | typo-alias fix | UK feed | verified (32 named rows, real Dunya shows) | yes → partial',
    '92 News': 'tv24 92-news | exact (already selected) | UK feed | verified identity, all-generic titles | already winning',
    'Hum Masala': 'UK1 HUM.Masala.uk = Hum NEWS content (rejected); tv24 empty; Sling HUMMA | US | verified cooking shows | yes → Sling',
    'Hum News': 'UK1 HUM.Masala.uk | exact | UK | plausible-verified (Hum News bulletins/shows) | already winning',
    'TV One Global': 'STG TVONEGL | typo-alias | US (Sling mirror) | verified (303 rows, real dramas; = Round-5 Sling feed) | yes → Sling',
    'Hum Sitaray': 'STG HUMST | exact | US (Sling mirror) | verified (315 rows, real dramas; = Round-5 Sling feed) | yes → Sling',
    'Madani Channel Urdu': 'UK1 Madani.Chnl.uk | alias needed | UK | same channel family, 116 real Islamic rows, pending eyeball | yes',
    'Samaa TV': 'tv24 samaa-tv empty (0 rows); samaa 87 rows but norm-mismatch | UK | n/a | no',
    'Geo News': 'tv24 geo-news 100 rows | exact | UK | redundant (first-party scraper wins) | no',
    'Geo TV': 'tv24 geo-tv 96 rows | exact | UK | redundant | no',
    'Hum TV Europe': 'tv24 hum-europe 14+68 rows | exact | UK | redundant (first-party scraper wins) | no',
    'Capital TV': 'epg.pw "Capital" | exact | UK | REJECTED wrong-feed (Capital radio DJs) | no',
    'Grace Network': 'tvepg/IN1 filler | fuzzy | unknown | REJECTED (all rows titled "Grace TV") | no',
    'ATV': 'epg.pw | exact | AT | REJECTED wrong-feed (German rows) | no',
    'ARY Digital Asia': 'tvpassport ATN-ARY Digital 30 rows | alias | CA | redundant (first-party wins) | no',
    'ARY News': 'tvpassport ATN-ARY News 98 rows | alias | CA | redundant | no',
    'ARY QTV': 'tvpassport ARY QTV 61 rows | exact | US | redundant (ASIANTELEVISION1 wins) | no',
    'ARY Musik': 'tvpassport ARY Musik 20 rows | exact | CA | redundant | no',
}

pk = [s for s in streams if re.match(r'^PK\s*\|', s.get('cat_name', ''))]
lin = [s for s in pk if not is_non_linear(s.get('cat_name', ''), s.get('name', ''))]
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
    elif name in GEN_ONLY:
        st = 'generic'
    elif not rows:
        st = 'blank'
    elif len(real) >= 8 and len(real) >= 0.5 * len(rows):
        st = 'full'
    elif real:
        st = 'partial'
    else:
        st = 'generic'
    if name in PFULL:
        pr = 'full' if name != 'Hum Masala' else 'full(repaired)'
    elif name in PPART:
        pr = 'partial'
    elif name in WRONG:
        pr = 'rejected'
    elif name in GEN_ONLY or st == 'generic':
        pr = 'blank'
    else:
        pr = st
    out.append((name, cat, st, len(rows), len(cur), len(cur24), newest[:8], pr, AUDIT.get(name, '')))

hdr = "| # | name | category | baseline | rows | current | next-24h | newest_stop | projected | candidate audit (this round) |"
sep = "|---|------|----------|----------|-----:|--------:|---------:|-------------|-----------|------------------------------|"
print(hdr)
print(sep)
for i, (name, cat, st, n, cur, c24, new, pr, aud) in enumerate(sorted(out), 1):
    print(f"| {i} | {name} | {cat.split('|')[0].strip()} | {st} | {n} | {cur} | {c24} | {new} | {pr} | {aud} |")

from collections import Counter
print("\nbaseline:", dict(Counter(r[2] for r in out)))
print("projected:", dict(Counter(r[7] for r in out)))
