#!/usr/bin/env python3
"""Systematic name-variant sweep v4 (READ-ONLY audit).

Variant rules (symmetric, via loose_keys()):
  (a) singular/plural  (b) '&'/'and'/''  (c) trailing country words
  (d) tv/hd/sd tokens  (e) abbreviations  (f) digit-concat ("News 18"~"News18")
  (g) regional synonyms (bengali~bangla, oriya~odia, ...)
plus a conservative word-diff pass (subset/superset, >=2 shared tokens,
dropped words limited to regions/quality/filler).

Key design fixes vs earlier passes:
  - 'the' always dropped; tv/channel/network/and dropped only in the
    drop_filler variant (so "Food Network" != "Food Food", "Channel24" !=
    "TV24", but "& TV" == "and TV").
  - token list is sorted but NOT deduped (so "Food Food" keeps both words).
  - keys that are empty, all-numeric, or a single weak token are rejected.
"""
import gzip
import html
import json
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict

BASE = '/Users/shameez/workspace/epg'
sys.path.insert(0, os.path.join(BASE, 'pipeline'))
from matcher import norm, is_non_linear, dice_ratio, QUALITY_RE, COUNTRY_SUFFIX_RE, WORD_RE

TODAY = '20260814'

REGION_TAIL = {'asia', 'uk', 'us', 'usa', 'me', 'eu', 'europe', 'canada',
               'uae', 'india', 'pakistan', 'bangladesh', 'australia'}
INDIAN_STATES = {
    'punjab', 'haryana', 'himachal', 'uttarakhand', 'uttaranchal', 'uttranchal',
    'uttar', 'pradesh', 'madhya', 'chhattisgarh', 'chattisgarh', 'jharkhand',
    'bihar', 'rajasthan', 'gujarat', 'kerala', 'karnataka', 'maharashtra',
    'tamil', 'tamilnadu', 'andhra', 'telangana', 'odisha', 'orissa', 'west',
    'bengal', 'assam', 'northeast', 'north', 'east', 'jammu', 'kashmir',
    'ladakh', 'delhi', 'ncr', 'jk', 'mp', 'up', 'ne', 'haryana',
}
ABBREV = {'ent': 'entertainment', 'entertain': 'entertainment', 'set': 'sony'}
SYNONYMS = {'bengali': 'bangla', 'oriya': 'odia', 'gujrati': 'gujarati',
            'telgu': 'telugu'}
FILLER_COND = {'tv', 'channel', 'network', 'and'}   # dropped only in drop_filler variant
DROP_OK = (REGION_TAIL | INDIAN_STATES |
           {'tv', 'hd', 'sd', 'fhd', 'uhd', 'qhd', 'plus', 'max', 'channel',
            'network', 'world', 'international', 'global', 'prime', 'live',
            'digital', 'entertainment', 'the', 'and'})

SING_GUARD = {
    'news', 'sports', 'series', 'arts', 'plus', 'class', 'status', 'genius',
    'virus', 'boss', 'glass', 'press', 'cross', 'bass', 'brass', 'mass',
    'pass', 'kiss', 'bliss', 'address', 'business', 'progress', 'success',
    'express', 'access', 'process', 'miss', 'dismiss', 'less', 'unless',
    'yes', 'gas', 'bus', 'us', 'os', 'as', 'is', 'his', 'this', 'mars',
    'james', 'charles', 'wales', 'thomas', 'andrew', 'chris', 'jones',
    'williams', 'davis',
}

DIGIT_RE = re.compile(r'\d+|\D+')


def split_digits(toks):
    out = []
    for t in toks:
        out.extend(x for x in DIGIT_RE.findall(t) if x)
    return out


def base_tokens(name):
    s = unicodedata.normalize('NFKD', str(name or ''))
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = QUALITY_RE.sub(' ', s)
    s = COUNTRY_SUFFIX_RE.sub(' ', s)
    s = s.replace('&', ' and ')
    s = re.sub(r'[^\w\s]', ' ', s)
    toks = [t for t in WORD_RE.findall(s) if t != 'the']
    return split_digits(toks)


def sing(t):
    if t in SING_GUARD or len(t) <= 3 or t.endswith('ss') or t.endswith('us'):
        return t
    if t.endswith('s'):
        return t[:-1]
    return t


def usable_key(toks):
    if not toks:
        return False
    if all(t.isdigit() for t in toks):
        return False
    if len(toks) == 1:
        t = toks[0]
        return t == 'and' or (len(t) >= 4 and not t.isdigit())
    return True


def loose_keys(name):
    toks0 = base_tokens(name)
    out = set()
    for drop_filler in (True, False):
        toks = [t for t in toks0 if not (drop_filler and t in FILLER_COND)]
        for strip_region in (True, False):
            tt = list(toks)
            if strip_region:
                while tt and tt[-1] in REGION_TAIL:
                    tt.pop()
            for use_abbrev in (True, False):
                ttt = [ABBREV.get(t, t) if use_abbrev else t for t in tt]
                for use_sing in (False, True):
                    tttt = [sing(t) if use_sing else t for t in ttt]
                    for use_syn in (False, True):
                        ttttt = [SYNONYMS.get(t, t) if use_syn else t for t in tttt]
                        if usable_key(ttttt):
                            out.add(' '.join(sorted(ttttt)))
    return out


def tag_pair(pname, dn):
    tags = []
    pt = set(base_tokens(pname))
    dt = set(base_tokens(dn))
    nP, nD = norm(pname), norm(dn)
    if ('&' in pname) or ('&' in dn) or ('and' in pt) or ('and' in dt):
        tags.append('ampersand/and')
    if (pt & REGION_TAIL) or (dt & REGION_TAIL):
        tags.append('region-suffix')
    if nP != nD:
        nPd = ' '.join(base_tokens(pname))
        nDd = ' '.join(base_tokens(dn))
        if nPd == nDd:
            tags.append('digit-concat')
        sp = ' '.join(sorted(sing(t) for t in nP.split()))
        sd = ' '.join(sorted(sing(t) for t in nD.split()))
        if sp == sd:
            tags.append('singular/plural')
        sp_syn = ' '.join(sorted(SYNONYMS.get(t, t) for t in nP.split()))
        sd_syn = ' '.join(sorted(SYNONYMS.get(t, t) for t in nD.split()))
        if sp_syn == sd_syn and nP != nD:
            tags.append('synonym')
    if ({'set', 'sony'} & (pt | dt)) or ({'ent', 'entertainment'} & (pt | dt)):
        tags.append('abbreviation')
    if ('tv' in pt or 'tv' in dt or 'hd' in pt or 'hd' in dt or 'sd' in pt or 'sd' in dt):
        tags.append('tv/hd/sd-token')
    if not tags:
        tags.append('word-diff')
    return tags


CHAN_BLOCK_RE = re.compile(r'<channel\s+id="([^"]*)"[^>]*>(.*?)</channel>', re.S)
DN_RE = re.compile(r'<display-name[^>]*>([^<]*)</display-name>')
PROG_OPEN_RE = re.compile(r'<programme\b([^>]*)>')
CH_ATTR = re.compile(r'\bchannel="([^"]+)"')
STOP_ATTR = re.compile(r'\bstop="(\d{8})')


def read_path(p):
    if p.endswith('.gz'):
        return gzip.open(p, 'rb').read().decode('utf-8', 'ignore')
    return open(p, 'r', encoding='utf-8', errors='ignore').read()


def scan_source(path):
    data = read_path(path)
    dnames = {}
    for cid, body in CHAN_BLOCK_RE.findall(data):
        dnm = DN_RE.search(body)
        if not dnm:
            continue
        dn = html.unescape(dnm.group(1)).strip()
        if dn:
            dnames.setdefault(dn, cid)
    presence = Counter()
    current = set()
    for m in PROG_OPEN_RE.finditer(data):
        a = m.group(1)
        cm = CH_ATTR.search(a)
        if not cm:
            continue
        ch = cm.group(1)
        presence[ch] += 1
        sm = STOP_ATTR.search(a)
        if sm and sm.group(1) >= TODAY:
            current.add(ch)
    return {'dnames': dnames, 'presence': dict(presence), 'current': current}


def main():
    streams = json.load(open(os.path.join(BASE, 'data/streams.json')))
    mapping = json.load(open(os.path.join(BASE, 'data/mapping.json')))
    manifest = json.load(open(os.path.join(BASE, 'data/sources.json')))

    src_files = defaultdict(list)
    for m in manifest:
        src_files[m['source']].append(m['file'])

    mapped_names = {v['name'] for v in mapping.values()}
    CAT_RE = re.compile(r'^([A-Za-z]{2,3})\s*\|')

    def country_of(cat):
        mm = CAT_RE.match((cat or '').strip())
        return mm.group(1).upper() if mm else '??'

    by_cc = defaultdict(lambda: defaultdict(int))
    name_cat = {}
    for s in streams:
        if is_non_linear(s.get('cat_name', ''), s.get('name', '')):
            continue
        n = s['name']
        if n in mapped_names:
            continue
        cc = country_of(s.get('cat_name', ''))
        by_cc[cc][n] += 1
        name_cat[n] = s.get('cat_name', '')

    rich_sources = [
        'epg.pw', 'tvepg',
        'epgshare01:IN1', 'epgshare01:IN2', 'epgshare01:IN4',
        'epgshare01:ASIANTELEVISION1',
        'epgshare01:UK1', 'epgshare01:IE1', 'epgshare01:US2', 'epgshare01:AE1',
        'iptv-org:tvpassport.com', 'iptv-org:tv24.co.uk', 'iptv-org:tvireland.ie',
        'iptv-org:allente.se', 'iptv-org:epg.112114.xyz',
        'iptv-org:tvinsider.com', 'iptv-org:dstv.com',
    ]
    rich = {}
    for src in rich_sources:
        paths = [p for p in src_files.get(src, []) if os.path.exists(p)]
        if not paths:
            continue
        merged = {'dnames': {}, 'presence': {}, 'current': set()}
        for p in paths:
            r = scan_source(p)
            for dn, cid in r['dnames'].items():
                merged['dnames'].setdefault(dn, cid)
            merged['presence'].update(r['presence'])
            merged['current'] |= r['current']
        vkeys = defaultdict(list)
        for dn, cid in merged['dnames'].items():
            for k in loose_keys(dn):
                vkeys[k].append((dn, cid))
        rich[src] = {'vkeys': vkeys, 'dnames': merged['dnames'],
                     'presence': merged['presence'], 'current': merged['current']}
        print(f"  [rich] {src:32} {len(merged['dnames'])} dn", flush=True)

    all_names = {n for cc in by_cc for n in by_cc[cc]}
    results = {}
    for name in all_names:
        keys = loose_keys(name)
        matches = []
        for src in rich_sources:
            r = rich.get(src)
            if not r:
                continue
            for k in keys:
                for dn, cid in r['vkeys'].get(k, []):
                    nN, dN = norm(name), norm(dn)
                    if nN and nN == dN:
                        continue
                    matches.append({
                        'source': src, 'display_name': dn, 'source_id': cid,
                        'tags': tag_pair(name, dn),
                        'has_programmes': r['presence'].get(cid, 0) > 0,
                        'n_prog': r['presence'].get(cid, 0),
                        'has_current': cid in r['current'],
                        'dice': round(dice_ratio(nN, dN), 2) if (nN and dN) else None,
                    })
        seen = {}
        for mm in matches:
            key = (mm['source'], mm['source_id'])
            if key not in seen:
                seen[key] = mm
        results[name] = list(seen.values())

    # ---- conservative word-diff pass ----
    norm_key_by_src = {}
    for src in rich_sources:
        r = rich.get(src)
        if not r:
            continue
        d = defaultdict(list)
        for dn, cid in r['dnames'].items():
            toks = [t for t in base_tokens(dn)
                    if t not in ('the', 'tv', 'channel', 'network', 'and')]
            k = ' '.join(sorted(toks))
            if k:
                d[k].append((dn, cid))
        norm_key_by_src[src] = d

    for name in all_names:
        if any(m['has_programmes'] for m in results.get(name, [])):
            continue
        p_toks = set(t for t in base_tokens(name)
                     if t not in ('the', 'tv', 'channel', 'network', 'and'))
        if not p_toks:
            continue
        extras = []
        for src in rich_sources:
            d = norm_key_by_src.get(src)
            if not d:
                continue
            for nk, items in d.items():
                s_toks = set(nk.split())
                if not s_toks:
                    continue
                inter = p_toks & s_toks
                if not inter or len(inter) < 2:
                    continue
                if inter != p_toks and inter != s_toks:
                    continue
                diff = (p_toks | s_toks) - inter
                if diff and diff <= DROP_OK:
                    drop = sorted(diff)
                    for dn, cid in items:
                        extras.append({
                            'source': src, 'display_name': dn, 'source_id': cid,
                            'tags': ['word-diff', f'drop:{",".join(drop)}'],
                            'has_programmes': rich[src]['presence'].get(cid, 0) > 0,
                            'n_prog': rich[src]['presence'].get(cid, 0),
                            'has_current': cid in rich[src]['current'],
                            'dice': round(dice_ratio(norm(name), norm(dn)), 2),
                        })
        if extras:
            seen = {}
            for mm in extras:
                key = (mm['source'], mm['source_id'])
                if key not in seen:
                    seen[key] = mm
            results.setdefault(name, []).extend(seen.values())

    out = {}
    for name in all_names:
        cc = next((c for c in by_cc if name in by_cc[c]), '??')
        out[name] = {
            'country': cc,
            'cat_name': name_cat.get(name, ''),
            'streams': by_cc[cc].get(name, 0),
            'norm': norm(name),
            'matches': results.get(name, []),
        }
    os.makedirs(os.path.join(BASE, 'audit_output'), exist_ok=True)
    json.dump(out, open(os.path.join(BASE, 'audit_output/variant_matches.json'), 'w'),
              indent=1, ensure_ascii=False)

    def has_live(n):
        return any(m['has_programmes'] for m in out[n]['matches'])

    for label, ccs in [('INDIAN', ['IN']), ('PAKISTANI', ['PK']), ('BANGLADESHI', ['BN'])]:
        g = {n: v for n, v in out.items() if v['country'] in ccs}
        live = [n for n in g if has_live(n)]
        print(f"\n===== {label}: {len(g)} names, {len(live)} live =====")
        for n, v in sorted(g.items(), key=lambda x: -x[1]['streams']):
            lm = [m for m in v['matches'] if m['has_programmes']]
            if lm:
                print(f"  [LIVE] {n!r} (streams={v['streams']})")
                for m in lm[:4]:
                    print(f"        -> {m['source']:26} {m['display_name']!r} "
                          f"tags={m['tags']} prog={m['n_prog']} cur={m['has_current']}")

    other_live = []
    for cc in by_cc:
        if cc in ('IN', 'PK', 'BN'):
            continue
        for n in out:
            if out[n]['country'] == cc and has_live(n):
                other_live.append(n)
    print(f"\n===== OTHER: {len(other_live)} live =====")
    for n in sorted(other_live, key=lambda x: -out[x]['streams']):
        v = out[n]
        lm = [m for m in v['matches'] if m['has_programmes']]
        print(f"  [{v['country']}] {n!r} (streams={v['streams']})")
        for m in lm[:3]:
            print(f"        -> {m['source']:26} {m['display_name']!r} tags={m['tags']} prog={m['n_prog']}")

    tot_live = sum(1 for n in all_names if has_live(n))
    print(f"\n[out] {len(out)} names, {tot_live} with live variant match")


if __name__ == '__main__':
    main()
