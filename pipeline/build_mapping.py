#!/usr/bin/env python3
"""Build mapping.json: for each provider stream, an ORDERED list of candidate
(source, channel_id) pairs plus the canonical TiviMate channel id.

Design (v2):
  - Fallback cascade: a stream maps to *several* candidates in priority order,
    and build_pipeline picks the first with actual programme data.
  - Country gating: a stream only matches epgshare01 files for its own region
    (derived from the provider's category prefix), which removes cross-country
    false positives. iptv-org (India) is gated to IN streams. epg.pw (global)
    and the provider's own feed are ungated.
  - Non-linear channels (24/7, VIP, radio, event hubs, adult) are skipped
    entirely — no EPG applies to them.
  - Fuzzy matching uses the symmetric Dice coefficient with a 2-token minimum,
    which rejects subset false-positives ("DW Espanol" -> "DW").

Candidate priority (highest first):
  1. pk            custom Pakistani scrapers (PK_OVERRIDES)
  2. iptv-org      India grab
  3. epgshare01:*  US locals + EU country files (country-gated)
  4. epg.pw        broad worldwide base
  5. provider      the panel's own xmltv.php (epg_channel_id + name)
  6. fuzzy         low-confidence fallback (last)

Canonical id: the stream's epg_channel_id when real, else the raw stream name.
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from matcher import SourceIndex, norm, is_non_linear

TIER = {
    'pk': 0, 'iptv-org': 1, 'skyhawk': 2, 'dstv': 2, 'epgone': 2,
    'bein': 2, 'epgshare01': 3, 'epg.pw': 4, 'provider': 5,
}

PK_OVERRIDES = {
    'Har Pal Geo': 'geo_entertainment_pk',
    'Geo TV': 'geo_entertainment_pk',
    'Geo Kahani': 'geo_kahani_pk',
    'Geo News': 'geo_news_pk',
    'Hum TV Europe': 'hum_tv_europe_pk',
    'ARY Digital Asia': 'ary_digital_pk',
    'Express Entertainment': 'express_entertainment_pk',
    'ARY Zindagi': 'ary_zindagi_pk',   # headless scraper (JS-rendered site)
}

# Curated aliases: provider channel name -> source display-name that has live
# programme data (verified 2026-08-15). Applied BEFORE exact matching, after
# PK_OVERRIDES. Fixes naming-convention mismatches (parentheticals, regional
# suffixes, historical names) that norm() + fuzzy can't bridge.
NAME_ALIASES = {
    # India (provider name -> verified-live source name)
    'Sony (Set India)': 'Sony Entertainment Television',
    'Sony TV Asia': 'Sony Entertainment Television',  # same feed, Asia variant
    'Zee Cinema Asia': 'Zee Cinema',                  # IN4 has zee cinema uk/usa too
    'Zee Cinema ME': 'Zee Cinema',
    'TV 9 Gujarati': 'TV9 Gujarati',
    'TV 9 Kannada': 'TV9 Kannada',
    'TV 9 Marathi': 'TV9 Marathi',
    'TV 9 Telugu': 'TV9 Telugu',
    'News 18 Tamil': 'News 18 Tamilnadu',
    'News 18 Uttar Pradesh & Uttarakhand': 'News 18 India',
    'Sahara Samay': 'News 18 India',  # renamed channel; feed carries it
    'Manoranjan Movies': 'Manoranjan',
    'MTV Beats': 'MTV',              # tvepg 'mtv' if live; harmless if not
    'Food Food': 'Foodxp',           # rebranded; foodxp has live data
    'Khabrain Abhi Tak': 'News 18 India',
}

JUNK_EPG_RE = re.compile(
    r'(\.epg$|\.L$|\.UFC$|\.BRLIVE$|\.ESPN$|\.MLB$|\.NFL$|\.NBA$|\.NHL$|\.PPV$|'
    r'\.EVENT$|servicestatus|event3hour|^1\.L$)', re.I)

# provider category prefix -> ISO country code
CAT_COUNTRY = {
    'US': 'US', 'USA': 'US',
    'UK': 'UK', 'IRE': 'IE', 'GB': 'UK', 'ITV': 'UK', 'BBC': 'UK',
    'CA': 'CA', 'DE': 'DE', 'FR': 'FR', 'IT': 'IT', 'GR': 'GR',
    'RO': 'RO', 'ES': 'ES', 'ESP': 'ES', 'PL': 'PL', 'PT': 'PT',
    'AU': 'AU', 'ZA': 'ZA', 'PH': 'PH', 'DK': 'DK', 'TR': 'TR',
    'TH': 'TH', 'SE': 'SE', 'SW': 'SE', 'NL': 'NL', 'NO': 'NO',
    'FI': 'FI', 'CY': 'CY', 'NZ': 'NZ', 'BR': 'BR', 'CZ': 'CZ',
    'IN': 'IN', 'PK': 'PK', 'UKR': 'UA', 'BL': 'BE', 'BN': 'BD',
    'MX': 'MX', 'AR': 'AR', 'JP': 'JP', 'IL': 'IL', 'AE': 'AE',
    'SA': 'SA', 'AT': 'AT', 'BE': 'BE', 'BG': 'BG', 'CH': 'CH',
    'HR': 'HR', 'HU': 'HU', 'RS': 'RS', 'KR': 'KR', 'SG': 'SG',
    'ID': 'ID', 'MY': 'MY', 'RU': 'RU', 'AL': 'AL',
    'DX': 'AE', 'RSL': 'SA', 'MO': 'MA',
}

# ISO country code -> epgshare01 file suffixes to match against
# (incl. the EXTRA_COUNTRY_SOURCES cross-country files from fetch_sources)
COUNTRY_SOURCES = {
    'US': ['US_LOCALS1', 'US2', 'US_SPORTS1'],
    'UK': ['UK1', 'IE1', 'ASIANTELEVISION1'], 'IE': ['UK1', 'IE1'],
    'CA': ['CA2'], 'DE': ['DE1', 'CH1', 'AT1', 'BE2'], 'FR': ['FR1', 'CH1', 'BE2'],
    'IT': ['IT1', 'CH1', 'MT1'], 'GR': ['GR1'], 'RO': ['RO1', 'RO2', 'HU1'],
    'ES': ['ES1'], 'PL': ['PL1'], 'PT': ['PT1'],
    'AU': ['AU1'], 'ZA': ['ZA1', 'AL1'], 'PH': ['PH2'],
    'DK': ['DK1'], 'TR': ['TR1', 'TR3'], 'TH': ['TH1'],
    'SE': ['SE1'], 'NL': ['NL1', 'BE2'], 'NO': ['NO1'], 'FI': ['FI1'],
    'CY': ['CY1'], 'NZ': ['NZ1'], 'BR': ['BR1', 'BR2'], 'CZ': ['CZ1', 'SK1', 'HU1'],
    'IN': ['IN1', 'IN2', 'IN4'],
    'AE': ['AE1'], 'SA': ['AE1', 'SA2', 'BEIN1'], 'MA': ['AE1'],
    'QA': ['AE1', 'BEIN1'], 'EG': ['AE1'],
    'AL': ['AL1'], 'BA': ['AL1'], 'HR': ['AL1', 'HR1'], 'RS': ['AL1', 'RS1'],
    'MK': ['AL1'], 'SI': ['AL1'], 'ME': ['AL1'], 'XK': ['AL1'],
    'AT': ['AT1', 'CH1'], 'CH': ['CH1'], 'BE': ['BE2'],
    'HU': ['HU1'], 'SK': ['SK1'], 'MT': ['MT1'],
    'BD': ['ASIANTELEVISION1'], 'PK': ['ASIANTELEVISION1'],
}

# dedicated fetcher sources: which countries each may serve
FETCHER_COUNTRIES = {
    'skyhawk': {'UK', 'IE', 'DE', 'AT', 'CH', 'IT'},
    'dstv': {'ZA'},
    'epgone': {'UA'},
    'bein': {'QA', 'AE', 'SA', 'MA', 'EG'},
}


def is_real_epg_id(v):
    if not v:
        return False
    return not JUNK_EPG_RE.search(str(v).strip())


PROVIDER_CALLSIGN_RE = re.compile(r'\b([KW][A-Z]{2,3})\b')


def extract_callsign(name):
    """Extract a US call sign from a provider name like 'FOX: FL | Tampa | WTVT'."""
    matches = PROVIDER_CALLSIGN_RE.findall(name or '')
    return matches[-1].lower() if matches else None


def canonical_id(stream):
    eid = (stream.get('epg_channel_id') or '').strip()
    if is_real_epg_id(eid):
        return eid
    return stream.get('name', '')


def country_hint(cat_name):
    m = re.match(r'^([A-Za-z]{2,3})\s*\|', (cat_name or '').strip())
    return CAT_COUNTRY.get(m.group(1).upper()) if m else None


def rebuild_index(name_to_ids):
    idx = SourceIndex()
    for n, ids in name_to_ids.items():
        idx.by_name[n] = list(ids)
        for t in n.split():
            idx._token_index[t].add(n)
    idx._size = len(name_to_ids)
    return idx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--streams', required=True)
    ap.add_argument('--sources-index', required=True)
    ap.add_argument('--provider-index', default=None)
    ap.add_argument('--callsigns', default=None, help='fetch_sources call_signs.json')
    ap.add_argument('--fuzzy-threshold', type=float, default=0.85)
    ap.add_argument('-o', '--out', required=True)
    args = ap.parse_args()

    streams = json.load(open(args.streams))
    sources_index = json.load(open(args.sources_index))

    # US call-sign index (call sign -> [(source, id)]) for US-local matching
    cs_index = defaultdict(list)
    if args.callsigns:
        call_signs = json.load(open(args.callsigns))
        for src in ('epgshare01:US_LOCALS1', 'epgshare01:US2', 'epgshare01:US_SPORTS1'):
            for cs, ids in call_signs.get(src, {}).items():
                for i in ids:
                    cs_index[cs].append((src, i))

    def src_tier(s):
        if s.startswith('iptv-org') or s == 'tvepg':
            return TIER['iptv-org']
        if s in ('skyhawk', 'dstv', 'epgone', 'bein'):
            return TIER[s]
        if s.startswith('epgshare01'):
            return TIER['epgshare01']
        if s == 'epg.pw':
            return TIER['epg.pw']
        return 99

    name_sources = sorted(sources_index, key=src_tier)
    src_idx = {s: rebuild_index(m) for s, m in sources_index.items()}

    prov_by_name = {}
    if args.provider_index:
        pidx = json.load(open(args.provider_index))
        prov_by_name = {norm(dn): cid for cid, dn in pidx.get('ids', {}).items() if cid}

    mapping = {}
    stats = defaultdict(int)
    for s in streams:
        name = s.get('name', '')
        sid = str(s.get('stream_id', name))
        cat = s.get('cat_name', '')

        # skip non-linear channels entirely (no EPG applies)
        if is_non_linear(cat, name):
            stats['non-linear-skipped'] += 1
            continue

        cc = country_hint(cat)
        allowed_eshare = {f'epgshare01:{f}' for f in COUNTRY_SOURCES.get(cc, [])}

        cands = []

        # 1. PK override
        if name in PK_OVERRIDES:
            cands.append((TIER['pk'], 'pk', PK_OVERRIDES[name], 'override', 1.0))
            stats['pk'] += 1

        # 1b. curated alias (naming-convention bridge; append a candidate from
        # EVERY source that has the aliased name — the cascade then picks the
        # first one with live programmes)
        alias = NAME_ALIASES.get(name)
        if alias:
            for src in name_sources:
                if src.startswith('epgshare01') and src not in allowed_eshare:
                    continue
                if src in FETCHER_COUNTRIES and cc and cc not in FETCHER_COUNTRIES[src]:
                    continue
                if src.startswith('iptv-org') and cc and cc != 'IN':
                    continue
                if src == 'tvepg' and cc and cc != 'IN':
                    continue
                eids = src_idx[src].exact(alias)
                if eids:
                    cands.append((src_tier(src), src, eids[0], 'alias', 0.97))
                    stats['alias'] += 1

        # 1c. US call-sign match (very precise; US locals named "FOX: FL | Tampa | WTVT")
        cs = extract_callsign(name)
        if cs and cs in cs_index:
            src, i = cs_index[cs][0]
            cands.append((TIER['epgshare01'], src, i, 'callsign', 0.98))
            stats['callsign'] += 1

        # 2. name-based sources, exact (country-gated for epgshare01 /
        #    dedicated fetchers / iptv-org / tvepg)
        for src in name_sources:
            if src.startswith('epgshare01') and src not in allowed_eshare:
                continue
            if src in FETCHER_COUNTRIES and cc and cc not in FETCHER_COUNTRIES[src]:
                continue
            if src.startswith('iptv-org') and cc and cc != 'IN':
                continue
            if src == 'tvepg' and cc and cc != 'IN':
                continue
            eids = src_idx[src].exact(name)
            if eids:
                cands.append((src_tier(src), src, eids[0], 'exact', 1.0))
                stats[f'{src}:exact'] += 1

        # 3. provider (epg_channel_id then display-name)
        if is_real_epg_id(s.get('epg_channel_id')):
            cands.append((TIER['provider'], 'provider', str(s['epg_channel_id']).strip(),
                          'epg-id', 1.0))
            stats['provider:epg-id'] += 1
        else:
            pn = norm(name)
            if pn and pn in prov_by_name:
                cands.append((TIER['provider'], 'provider', prov_by_name[pn], 'name', 0.9))
                stats['provider:name'] += 1

        # 4. fuzzy (lowest trust, appended last, country-gated)
        for src in name_sources:
            if src.startswith('epgshare01') and src not in allowed_eshare:
                continue
            if src in FETCHER_COUNTRIES and cc and cc not in FETCHER_COUNTRIES[src]:
                continue
            if src.startswith('iptv-org') and cc and cc != 'IN':
                continue
            if src == 'tvepg' and cc and cc != 'IN':
                continue
            for sc, cn, cid in src_idx[src].fuzzy(name, threshold=args.fuzzy_threshold, limit=2):
                cands.append((50 + src_tier(src), src, cid, 'fuzzy', round(sc, 2)))
                stats[f'{src}:fuzzy'] += 1

        if not cands:
            stats['unmatched'] += 1
            continue

        # Sort by tier then confidence; Python's stable sort preserves the
        # insertion order (source priority) for same-tier ties — so UK1 beats
        # IE1 for a UK stream, epg-id beats provider-name, etc.
        cands.sort(key=lambda c: (c[0], -c[4]))
        seen = set()
        dedup = []
        for c in cands:
            key = (c[1], c[2])
            if key in seen:
                continue
            seen.add(key)
            dedup.append(c)
        mapping[sid] = {
            'name': name,
            'cat_name': cat,
            'country': cc,
            'canonical_id': canonical_id(s),
            'candidates': [{'source': c[1], 'source_id': c[2], 'method': c[3],
                            'confidence': c[4]} for c in dedup],
        }

    json.dump(mapping, open(args.out, 'w'), indent=1, ensure_ascii=False)
    n_cand = sum(len(v['candidates']) for v in mapping.values())
    print(f'[mapping] {len(mapping)} streams mapped | candidates {n_cand} '
          f'(avg {n_cand/max(1,len(mapping)):.1f}/stream)')
    print(f'[mapping] stats: {dict(stats)}')


if __name__ == '__main__':
    main()
