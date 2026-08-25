#!/usr/bin/env python3
"""Round 7 comprehensive prefilter-miss audit.

For every uncovered PK name, search each registry with FOUR strategies and
classify why the current prefilter (exact norm OR dice>=0.80 fuzzy with >=2
shared tokens) would keep or miss the candidate.

Registries:
  - iptv-org site channel files (current master): tv24.co.uk, tvpassport.com,
    streamingtvguides.com, tv.blue.ch, tvinsider.com, tvireland.ie,
    allente.se*, tvhebdo.com, foxtel.com.au
  - locally downloaded pipeline sources (sources_index.json, Aug-15)
"""
import json, re, sys, os, unicodedata
from collections import defaultdict

sys.path.insert(0, '/Users/shameez/workspace/epg/pipeline')
from matcher import SourceIndex, norm, dice_ratio
from build_mapping import NAME_ALIASES

AUDIT = '/Users/shameez/workspace/epg/audit_output'
REGS = '/Users/shameez/workspace/iptv-org-epg/sites'

deployed = json.load(open('/tmp/epg_issue/deployed_coverage_gaps.json'))
uncovered = deployed['per_country']['PK']['uncovered_names']

# ---- load registries ----
def load_site_reg(site):
    """Return {norm_name: [site_ids]} for a site's registry (region files merged)."""
    p = f'{REGS}/{site}'
    files = []
    if os.path.isdir(p):
        files = [f'{p}/{f}' for f in os.listdir(p) if f.endswith('.channels.xml')]
    else:
        files = [f'{p}.channels.xml']
    out = defaultdict(list)
    for f in files:
        if not os.path.exists(f):
            continue
        txt = open(f, encoding='utf-8', errors='ignore').read()
        for m in re.finditer(r'<channel\s+([^>]*)>([^<]*)</channel>', txt):
            sid = re.search(r'site_id="([^"]+)"', m.group(1))
            if not sid:
                continue
            dn = m.group(2).strip()
            n = norm(dn)
            if n:
                out[n].append(sid.group(1))
    return dict(out)

SITES = ['tv24.co.uk', 'tvpassport.com', 'streamingtvguides.com', 'tv.blue.ch',
         'tvinsider.com', 'tvireland.ie', 'tvhebdo.com', 'foxtel.com.au',
         'allente.se', 'cyta.com.cy']
regs = {s: load_site_reg(s) for s in SITES}
print("registries loaded:", {k: len(v) for k, v in regs.items()})

# local downloaded sources
src_idx = json.load(open('/Users/shameez/workspace/epg/data/sources_index.json'))
print("local sources:", list(src_idx.keys()))

# ---- provider index (prefilter provider side) ----
streams = json.load(open('/Users/shameez/workspace/epg/data/streams.json'))
prov_idx = SourceIndex()
prov_names = set()
for s in streams:
    n = s.get('name', '')
    if n:
        prov_idx.add(n, 'x')
        prov_names.add(norm(n))

def prefilter_keeps(dn, site=None):
    """Replicate make_iptvorg_channels keep decision for one source channel."""
    nname = norm(dn)
    if not nname:
        return False, 'empty-norm'
    if nname in prov_names:
        return True, 'exact'
    fz = prov_idx.fuzzy(dn, threshold=0.80, limit=1)
    if fz:
        return True, f'fuzzy-{fz[0][1]}({fz[0][0]:.2f})'
    return False, 'missed'

def levenshtein(a, b):
    if abs(len(a) - len(b)) > 3:
        return 99
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j-1] + 1, prev[j-1] + (ca != cb)))
        prev = cur
    return prev[-1]

print("\n" + "=" * 90)
print("PREFILTER-MISS AUDIT: 60 uncovered PK names x registries")
print("=" * 90)
report = []
for name in uncovered:
    nn = norm(name)
    alias = NAME_ALIASES.get(name) or NAME_ALIASES.get(nn)
    alias_nn = norm(alias) if alias else None
    rows = []
    # 1. exact
    for src, reg in list(regs.items()) + list(src_idx.items()):
        if nn in reg:
            rows.append(('EXACT', src, nn))
        elif alias_nn and alias_nn in reg:
            rows.append(('ALIAS', src, alias_nn))
    # 2. edit-distance/phonetic over registries (invisible-to-prefilter candidates)
    for src, reg in list(regs.items()):
        for rn in reg:
            if rn == nn:
                continue
            d = levenshtein(nn, rn)
            if d <= 2 and len(rn) > 3:
                # shared-token fuzzy score (what the prefilter uses)
                dice = dice_ratio(nn, rn)
                shared = len(set(nn.split()) & set(rn.split()))
                rows.append(('TYPO', src, rn, f'lev={d} dice={dice:.2f} shared={shared}'))
    if rows:
        report.append((name, rows))

n_miss = 0
for name, rows in report:
    print(f"\n{name!r}")
    for r in rows:
        kind = r[0]
        if kind in ('EXACT', 'ALIAS'):
            src, cand = r[1], r[2]
            kept, why = prefilter_keeps(cand)
            tag = 'KEPT' if kept else f'MISSED({why})'
            print(f"    {kind:6} {src:28} -> {cand!r:34} prefilter: {tag}")
        else:
            _, src, cand, meta = r
            kept, why = prefilter_keeps(cand)
            tag = 'KEPT' if kept else 'MISSED'
            if not kept:
                n_miss += 1
            print(f"    {kind:6} {src:28} -> {cand!r:34} {meta} prefilter: {tag}")

print(f"\nTOTAL typo-level prefilter misses: {n_miss}")
