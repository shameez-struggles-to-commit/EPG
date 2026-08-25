#!/usr/bin/env python3
"""Round 7 final per-stream classification of the 107 PK streams.

States (per ChatGPT mandate): full / partial / blank / rejected_wrong_feed.
Baseline = DEPLOYED guide (run 32858428529). Projected = after integrating
the proven-but-not-yet-wired fixes (Sling, Madani, mjunoon) + this round's
tv24 Dunya News winner + correctness firewall removals.
"""
import json, gzip, re, sys
sys.path.insert(0, '/Users/shameez/workspace/epg/pipeline')
from build_mapping import build_collision_split, is_non_linear

streams = json.load(open('/Users/shameez/workspace/epg/data/streams.json'))
identity = build_collision_split(streams)
pk = [s for s in streams if re.match(r'^PK\s*\|', s.get('cat_name', ''))]
lin = [s for s in pk if not is_non_linear(s.get('cat_name', ''), s.get('name', ''))]
assert len(lin) == 107, len(lin)

d = gzip.open('/tmp/epg_issue/guide.xml.gz', 'rt', errors='ignore').read()
ch_progs = {}
for m in re.finditer(r'<programme[^>]*channel="([^"]+)"[^>]*>((?:(?!</programme>).)*)</programme>', d, re.S):
    t = re.search(r'<title[^>]*>([^<]+)</title>', m.group(2))
    ch_progs.setdefault(m.group(1), []).append(t.group(1) if t else '?')

GENERIC = re.compile(
    r'news headlines|news bulletin|news flash|news update|^news$|^tv one$|'
    r'to be announced|^tba$|no match|^grace tv$|^ptv live$|placeholder', re.I)

# wrong-feed streams: (stream name fragment, reason)
WRONG = {
    'ATV': 'Austria feed (Der letzte Bulle/MediaShop = German) via epg.pw',
    'Capital TV': 'UK Capital radio feed (Aimee Vivian/Sonny Jay) via epg.pw',
    'Hum Masala': 'news-like rows (News @ 7) via UK1; Sling proves real=cooking',
    'Grace Network': 'all rows titled "Grace TV" = channel-name filler',
}
GENERIC_ONLY = {'92 News': 'hourly "News Headlines/Bulletin" (identity-correct, no real titles)'}

PROVEN_FULL = {   # names that get REAL full schedules from proven-but-unwired sources
    'Hum Sitaray': 'Sling HUMST-F (Round 5) + streamingtvguides TVONEGL-class cross-confirm (315 rows)',
    'TV One Global': 'Sling TVONEGL (Round 5: 23 real drama rows; STG TVONEGL cross-confirms, 303 rows)',
    'Madani Channel Urdu': 'UK1 Madani.Chnl.uk (116 rows, real Islamic programming titles)',
    'Hum Masala': 'Sling HUMMA real cooking shows (repair wrong-feed)',
}
PROVEN_PARTIAL = {
    'Abb Takk': 'mjunoon real named-show intervals (partial)',
    'News One': 'mjunoon real named-show intervals (partial)',
    'Duniya News': 'tv24 dunya-news: 78 rows but 46 generic bulletins + 32 named shows = partial',
}
FIX_TO_PARTIAL = {'92 News': 'tv24 92-news branded generic hourly rows (better identity, still generic)'}

rows = []
for s in lin:
    name = s['name']
    sid = str(s.get('stream_id', '')).strip()
    cid = identity.get(sid, s.get('epg_channel_id') or name)
    progs = ch_progs.get(cid, [])
    if name in WRONG:
        st = f'rejected_wrong_feed: {WRONG[name]}'
    elif name in GENERIC_ONLY:
        st = f'generic: {GENERIC_ONLY[name]}'
    elif not progs:
        st = 'blank'
    else:
        real = [t for t in progs if not GENERIC.search(t)]
        if len(real) >= 8 and len(real) >= 0.5 * len(progs):
            st = f'full ({len(progs)} rows)'
        elif real:
            st = f'partial ({len(progs)} rows, {len(real)} real titles)'
        else:
            st = f'generic-junk ({len(progs)} rows)'
    # projected
    if name in PROVEN_FULL:
        pr = 'FULL' if name != 'Hum Masala' else 'FULL (repaired)'
    elif name in PROVEN_PARTIAL:
        pr = 'PARTIAL'
    elif name in WRONG:
        pr = 'REJECTED (wrong-feed removed)'
    elif name in GENERIC_ONLY or st.startswith('generic'):
        pr = 'BLANK'  # generic rows are not "real named timed programmes" (mandate def)
    elif st.startswith('partial'):
        pr = 'PARTIAL'
    else:
        pr = st.split(' ')[0].upper()
    rows.append((name, s.get('cat_name', ''), cid, st, pr))

# count baseline states
def state_of(st):
    if st.startswith('full'): return 'full'
    if st.startswith('partial'): return 'partial'
    if st.startswith('rejected'): return 'rejected'
    if st.startswith('generic'): return 'generic'
    return 'blank'

from collections import Counter
base = Counter(state_of(r[3]) for r in rows)
proj = Counter(r[4] for r in rows)
print("BASELINE (deployed):", dict(base), "| streams:", len(rows))
print("PROJECTED:", dict(proj))
print()
print(f"{'NAME':36} {'BASELINE':34} PROJECTED")
for name, cat, cid, st, pr in sorted(rows):
    print(f"{name[:36]:36} {st[:34]:34} {pr}")
