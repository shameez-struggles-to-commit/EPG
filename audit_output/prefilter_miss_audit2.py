#!/usr/bin/env python3
"""Round 7 prefilter-miss audit v2 — token-aligned typo detection only."""
import json, re, sys, os
from collections import defaultdict

sys.path.insert(0, '/Users/shameez/workspace/epg/pipeline')
from matcher import SourceIndex, norm, dice_ratio
from build_mapping import NAME_ALIASES

REGS = '/Users/shameez/workspace/iptv-org-epg/sites'
deployed = json.load(open('/tmp/epg_issue/deployed_coverage_gaps.json'))
# TRUE uncovered set: the artifact truncates uncovered_names at 60. Derive the
# full set from the PK linear streams + deployed covered_names instead.
import sys as _sys, re as _re
_sys.path.insert(0, '/Users/shameez/workspace/epg/pipeline')
from build_mapping import is_non_linear as _inl
_streams = json.load(open('/Users/shameez/workspace/epg/data/streams.json'))
_cov = set(deployed['covered_names'])
uncovered = sorted({s['name'] for s in _streams
                    if _re.match(r'^PK\s*\|', s.get('cat_name', ''))
                    and not _inl(s.get('cat_name', ''), s.get('name', ''))
                    and s['name'] not in _cov})

def load_site_reg(site):
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
src_idx = json.load(open('/Users/shameez/workspace/epg/data/sources_index.json'))

streams = json.load(open('/Users/shameez/workspace/epg/data/streams.json'))
prov_idx = SourceIndex()
prov_names = set()
for s in streams:
    n = s.get('name', '')
    if n:
        prov_idx.add(n, 'x')
        prov_names.add(norm(n))

def lev(a, b):
    if abs(len(a) - len(b)) > 3:
        return 99
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j-1] + 1, prev[j-1] + (ca != cb)))
        prev = cur
    return prev[-1]

def token_aligned_typo(qtoks, ctoks):
    """Every query token near-matches exactly one candidate token (lev<=2),
    same token count, at least one differing token, dice >= 0.5."""
    if len(qtoks) != len(ctoks) or len(qtoks) < 1:
        return False
    used = set()
    changed = 0
    for q in qtoks:
        hit = None
        for i, c in enumerate(ctoks):
            if i in used:
                continue
            if q == c or (len(q) > 2 and len(c) > 2 and lev(q, c) <= 2):
                hit = i
                break
        if hit is None:
            return False
        if ctoks[hit] != q:
            changed += 1
        used.add(hit)
    return changed >= 1

def prefilter_keeps(dn):
    nname = norm(dn)
    if not nname:
        return False, 'empty-norm'
    if nname in prov_names:
        return True, 'exact'
    fz = prov_idx.fuzzy(dn, threshold=0.80, limit=1)
    if fz:
        return True, f'fuzzy({fz[0][0]:.2f})'
    return False, 'missed'

print("TOKEN-ALIGNED TYPO / ALIAS CANDIDATES (prefilter-visible-invisible)")
print("=" * 100)
total_missed = 0
for name in uncovered:
    nn = norm(name)
    qtoks = nn.split()
    alias = NAME_ALIASES.get(name) or NAME_ALIASES.get(nn)
    alias_nn = norm(alias) if alias else None
    found = []
    for src, reg in list(regs.items()) + list(src_idx.items()):
        if nn in reg:
            found.append((src, nn, 'EXACT'))
        elif alias_nn and alias_nn in reg:
            found.append((src, alias_nn, 'ALIAS'))
        for rn in reg:
            if rn == nn or rn == alias_nn:
                continue
            if token_aligned_typo(qtoks, rn.split()) and dice_ratio(nn, rn) >= 0.5:
                found.append((src, rn, 'TYPO'))
    if found:
        print(f"\n{name!r} (norm={nn!r})")
        for src, cand, kind in found:
            kept, why = prefilter_keeps(cand)
            if not kept:
                total_missed += 1
            tag = f'prefilter KEPT({why})' if kept else 'prefilter MISSED'
            print(f"    {kind:6} {src:26} -> {cand!r:38} {tag}")
print(f"\nTotal candidates prefilter would MISS: {total_missed}")
