#!/usr/bin/env python3
"""Exhaustive scan: ALL iptv-org/epg site registries x 60 uncovered PK names.
Reports exact-norm, alias, and token-aligned-typo candidates per name.
"""
import json, re, sys, os, glob
from collections import defaultdict

sys.path.insert(0, '/Users/shameez/workspace/epg/pipeline')
from matcher import norm, dice_ratio
from build_mapping import NAME_ALIASES

REGS = '/Users/shameez/workspace/iptv-org-epg/sites'
deployed = json.load(open('/tmp/epg_issue/deployed_coverage_gaps.json'))
# TRUE uncovered set (artifact truncates at 60): derive from PK linear streams
import re as _re
from build_mapping import is_non_linear as _inl
_streams = json.load(open('/Users/shameez/workspace/epg/data/streams.json'))
_cov = set(deployed['covered_names'])
uncovered = sorted({s['name'] for s in _streams
                    if _re.match(r'^PK\s*\|', s.get('cat_name', ''))
                    and not _inl(s.get('cat_name', ''), s.get('name', ''))
                    and s['name'] not in _cov})
print(f"true uncovered names: {len(uncovered)}", file=sys.stderr)

# one pass over ALL registry files, keyed by norm-name
name_to_sites = defaultdict(list)   # norm -> [(site, site_id, display)]
files = glob.glob(f'{REGS}/*/*.channels.xml')
print(f"scanning {len(files)} registry files", file=sys.stderr)
for f in files:
    site = f.split('/sites/')[1].split('/')[0]
    try:
        txt = open(f, encoding='utf-8', errors='ignore').read()
    except Exception:
        continue
    for m in re.finditer(r'<channel\s+([^>]*)>([^<]*)</channel>', txt):
        sid = re.search(r'site_id="([^"]+)"', m.group(1))
        if not sid:
            continue
        dn = m.group(2).strip()
        n = norm(dn)
        if n:
            name_to_sites[n].append((site, sid.group(1), dn))

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

def token_aligned(qtoks, ctoks):
    if len(qtoks) != len(ctoks):
        return False
    used, changed = set(), 0
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

print("=" * 100)
print("ALL iptv-org REGISTRIES x 60 uncovered PK names")
print("=" * 100)
for name in uncovered:
    nn = norm(name)
    alias = NAME_ALIASES.get(name) or NAME_ALIASES.get(nn)
    alias_nn = norm(alias) if alias else None
    rows = []
    for n_key, entries in name_to_sites.items():
        kind = None
        if n_key == nn:
            kind = 'EXACT'
        elif alias_nn and n_key == alias_nn:
            kind = 'ALIAS'
        elif token_aligned(nn.split(), n_key.split()) and dice_ratio(nn, n_key) >= 0.5:
            kind = 'TYPO'
        if kind:
            for site, sid, dn in entries[:2]:
                rows.append((kind, site, sid, dn))
    if rows:
        print(f"\n{name!r} (norm={nn!r})")
        for kind, site, sid, dn in rows[:6]:
            print(f"    {kind:6} {site:28} {sid!r:22} {dn!r}")
    else:
        print(f"\n{name!r} (norm={nn!r}) -> NOT IN ANY iptv-org REGISTRY")
