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
from matcher import SourceIndex, norm, is_non_linear, cyr_to_lat

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
    'Aaj Entertainment': 'aaj_entertainment_pk',
    'ARY Zindagi': 'ary_zindagi_pk',
}

# Curated aliases: provider channel name -> source display-name that has live
# programme data (verified 2026-08-15). Applied BEFORE exact matching, after
# PK_OVERRIDES. Fixes naming-convention mismatches (parentheticals, regional
# suffixes, historical names) that norm() + fuzzy can't bridge.
NAME_ALIASES = {
    # India (provider name -> verified-live source name)
    'Sony (Set India)': 'Sony Entertainment Television',
    'Sony TV Asia': 'Sony TV',  # international/Asia feed = Sky/Virgin "SONY TV", not domestic SET
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
    # 2026-08-15 expansion — India/Pakistan/diaspora (verified live progs)
    'Maha Movie': 'Maha Movies',                  # singular/plural (tvepg/IN1)
    'Sankara TV': 'Sri Sankara',                  # honorific prefix (IN1)
    'Samachar Plus Rajasthan': 'Samachar Plus',   # regional suffix (IN1)
    'Victers TV': 'Kite Victers',                 # "Kite" = Kerala infra prefix (IN1)
    'ARY News': 'ATN ARY News',                   # ASIANTELEVISION1 ATN prefix
    'B4U Plus': 'ATN B4U Plus',                   # ASIANTELEVISION1 ATN prefix
    'ARY QTV': 'QTV Religious',                   # Sky UK display-name
    'News 18 Bengali': 'News18 Bangla',           # bengali->bangla synonym (tvepg)
    'Kalaignar Murasu TV': 'Murasu TV',           # tataplay/dishtv/airtel name
    'Channel i': 'Channel i (Bangladesh)',        # region parenthetical (tvpassport)
    'Channel24': 'Channels 24',                   # singular/plural (UK1)
    'EWTN US': 'EWTN',                            # region suffix (IE1)
    'SuperSport Schools': 'SuperSport School HD', # singular/plural (epg.pw)
    'Sky Cinema Highlight': 'Sky Cinema Highlights HD',  # singular/plural
    'Alfa Dramas': 'Alfa Drama',                  # singular/plural (AE1)
    # norm-form keys (catch every HD/SD/FHD variant of one channel)
    'star gold uk': 'Utsav Gold',                 # renamed 2023 (Sky/Virgin)
    'star plus uk': 'Utsav Plus',                 # renamed 2023
    'star bharat uk': 'Utsav Bharat',             # renamed 2023
    # Greece (2026-08-16): Latin provider names vs Greek-script source names.
    # Keys in norm() form so "ERT 1" / "ERT 1 HD" / "ERT 2 HD" etc. all hit.
    # 'HD' normalizes away on both sides, so 'ΕΡΤ1 HD' keys as 'ερτ1'.
    'ert 1': 'ΕΡΤ1 HD',
    'ert 2': 'ΕΡΤ2 HD',
    'ert 3': 'ΕΡΤ3 HD',
    'ert news': 'ΕΡΤ NEWS',
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
    'skyhawk': {'UK', 'IE', 'DE', 'AT', 'CH', 'IT', 'IN', 'PK', 'BD'},
    'dstv': {'ZA'},
    'epgone': {'UA'},
    'bein': {'QA', 'AE', 'SA', 'MA', 'EG'},
}

# iptv-org site -> countries it may serve. FIXES the old blanket
# "iptv-org = India only" gate which silently hid ALL non-India grabber
# outputs from their own countries' streams (found in the 2026-08-15
# matching audit: Orange Sport RO, Cablenet CY, TV4 Motor SE, OCS/Moselle FR,
# DE regional channels etc. all existed in grabs but were gated away).
# Sites not listed here fall back to IN-only (conservative; the five India
# grab sites dominate that list).
IPTV_ORG_COUNTRIES = {
    'jiotv': {'IN'}, 'tataplay': {'IN'}, 'dishtv': {'IN'},
    'airtelxstream': {'IN'}, 'zee5': {'IN'},
    'epg.112114.xyz': {'IN'},
    'tvpassport.com': {'US', 'CA', 'MX'},
    'tv24.co.uk': {'UK', 'IE'},
    'tvireland.ie': {'IE', 'UK'},
    'www.magenta.tv': {'DE'}, 'web.magentatv.de': {'DE'},
    'tv.blue.ch': {'CH', 'DE', 'FR', 'IT', 'AT'},
    'abc.net.au': {'AU'}, 'foxtel.com.au': {'AU'},
    'tvhebdo.com': {'CA'},
    'programetv.ro': {'RO'},
    'programacion-tv.elpais.com': {'ES'}, 'movistarplus.es': {'ES'},
    'programme-tv.net': {'FR'}, 'tvcesoir.fr': {'FR'},
    'meo.pt': {'PT'},
    'guidatv.sky.it': {'IT'},
    'cosmotetv.gr': {'GR'},
    'cyta.com.cy': {'CY'},
    'allente.se': {'SE'},
    'gigatv.3bbtv.co.th': {'TH'},
    'tvinsider.com': {'US', 'CA'},
}


def iptv_org_allowed(src, cc):
    """True if the iptv-org site may serve a stream of country cc."""
    site = src.split(':', 1)[1] if ':' in src else src
    countries = IPTV_ORG_COUNTRIES.get(site, {'IN'})  # default: India-only
    return cc is None or cc in countries


# Diaspora EXACT-match sources: Pakistani/Bangladeshi/Indian channels are
# carried on UK/US/EU satellite + DTH lineups (Samaa on UK Sky, ARY Musik on
# US TVPassport, Channel i on Indian feeds...). EXACT norm matches only —
# namesakes exist (Filmax is PL, KTN is KE, See TV is DK) so fuzzy is never
# allowed through this path. 2026-08-15 matching audit.
DIASPORA_EXACT = {
    'PK': ['epgshare01:UK1', 'epgshare01:IE1', 'epgshare01:US2',
           'epgshare01:AE1', 'epgshare01:IN1',
           'epgshare01:IN4', 'tvepg', 'iptv-org:tvpassport.com',
           'iptv-org:tv24.co.uk', 'iptv-org:tvireland.ie',
           'iptv-org:allente.se'],
    # NOTE: meo.pt excluded (Venus.ar = Arabic Venus, not PK Venus) and
    # AL1 excluded (ATV.al = Albanian ATV, not PK ATV) — single-word
    # namesake false positives found in the 2026-08-15 audit.
    'IN': ['epgshare01:AE1', 'epgshare01:UK1', 'epgshare01:ASIANTELEVISION1',
           'iptv-org:tvpassport.com', 'iptv-org:tv24.co.uk'],
    'BD': ['epgshare01:IN1', 'epgshare01:IN4', 'tvepg', 'epgshare01:UK1',
           'iptv-org:tvpassport.com', 'iptv-org:tv24.co.uk'],
    # Reverse diaspora: a Pakistani/Indian channel listed under a host-country
    # category (UK "Asian" / US / CA) should still reach its home-country
    # sources (EXACT matches only, never fuzzy).
    'UK': ['epgshare01:IN1', 'epgshare01:IN4', 'tvepg'],
    'US': ['epgshare01:IN1', 'epgshare01:IN4', 'tvepg', 'epgshare01:ASIANTELEVISION1'],
    'CA': ['epgshare01:IN1', 'epgshare01:IN4', 'tvepg', 'epgshare01:ASIANTELEVISION1'],
}


def diaspora_allowed(src, cc):
    """Exact-match-only: is this source a diaspora carrier for country cc?"""
    if not cc:
        return False
    return src in DIASPORA_EXACT.get(cc, [])


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


def epg_q(name):
    """Query transform for the epg.one source: transliterate Cyrillic and dedupe
    repeated tokens. Provider names like "5 Kanal (5 канал)" normalize to
    "5 kanal 5 kanal"; dedupe collapses that to "5 kanal" so it can hit the
    fetcher's transliterated display-name. Harmless for pure-Latin names."""
    toks = cyr_to_lat(norm(name)).split()
    return ' '.join(dict.fromkeys(toks))


# ---- US affiliate resolver (2026-08-16) ------------------------------------
# Provider names US locals like "ABC: AL Birmingham ABC 33" (network + state +
# city + brand, NO call sign). The pipeline/us_affiliates.json table (built by
# build_us_affiliates.py from Wikipedia per-network affiliate lists) maps
# "net|state|market" -> call signs; we then look the call sign up in the
# epgshare01 US_LOCALS1 index like a normal call-sign match.

US_AFF_STATE_CODES = ('AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD '
                      'MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC '
                      'SD TN TX UT VT VA WA WV WI WY DC').split()
US_AFF_STATE_NAMES = {
    'alabama': 'AL', 'alaska': 'AK', 'arizona': 'AZ', 'arkansas': 'AR',
    'california': 'CA', 'colorado': 'CO', 'connecticut': 'CT', 'delaware': 'DE',
    'florida': 'FL', 'georgia': 'GA', 'hawaii': 'HI', 'idaho': 'ID',
    'illinois': 'IL', 'indiana': 'IN', 'iowa': 'IA', 'kansas': 'KS',
    'kentucky': 'KY', 'louisiana': 'LA', 'maine': 'ME', 'maryland': 'MD',
    'massachusetts': 'MA', 'michigan': 'MI', 'minnesota': 'MN',
    'mississippi': 'MS', 'missouri': 'MO', 'montana': 'MT', 'nebraska': 'NE',
    'nevada': 'NV', 'new hampshire': 'NH', 'new jersey': 'NJ', 'new mexico': 'NM',
    'new york': 'NY', 'north carolina': 'NC', 'north dakota': 'ND', 'ohio': 'OH',
    'oklahoma': 'OK', 'oregon': 'OR', 'pennsylvania': 'PA', 'rhode island': 'RI',
    'south carolina': 'SC', 'south dakota': 'SD', 'tennessee': 'TN',
    'texas': 'TX', 'utah': 'UT', 'vermont': 'VT', 'virginia': 'VA',
    'washington': 'WA', 'west virginia': 'WV', 'wisconsin': 'WI', 'wyoming': 'WY',
}
US_AFF_NETS = 'ABC CBS NBC FOX CW'
US_AFF_STATE_CODE_ALT = '(' + '|'.join(US_AFF_STATE_CODES) + ')'
US_AFF_STATE_NAME_ALT = '(' + '|'.join(sorted(US_AFF_STATE_NAMES, key=len, reverse=True)) + ')'
US_AFF_BRAND_RE = re.compile(
    r'\s+(?:(?:ABC|CBS|NBC|FOX|CW)\s*\d+|\d+\s*(?:ABC|CBS|NBC|FOX|CW)'
    r'|(?:ABC|CBS|NBC|FOX|CW))\s*$', re.I)
US_AFF_CALL_RE = re.compile(r'\s+[KW][A-Z]{2,3}\s*$')


class AffiliateIndex:
    """net|state|market -> [callsign, ...] with fallbacks."""

    def __init__(self, table_path):
        self.by_key = {}
        self.by_city = {}
        if table_path and os.path.exists(table_path):
            data = json.load(open(table_path))
            self.by_key = {k: v for k, v in data.get('affiliates', {}).items()}
            self.single = data.get('state_single', {})
            for k in self.by_key:
                self.by_city.setdefault(k.split('|')[2], set()).add(k)

    def lookup(self, net, st, city):
        """Return (key, [callsigns]) or (key, None)."""
        key = f'{net}|{st}|{city}'
        css = self.by_key.get(key)
        if css is None and len(self.single.get(f'{net}|{st}', [])) == 1:
            css = self.single[f'{net}|{st}']
        if css is None:
            # market-contains fallback ("Raleigh" -> "Raleigh–Durham")
            prefix = f'{net}|{st}|'
            cands = [k for k in self.by_key
                     if k.startswith(prefix) and city in k.split('|')[2]]
            if len(cands) == 1:
                css = self.by_key[cands[0]]
                key = cands[0]
        if css is None:
            # nationally-unique city for this network ("Los Angeles" w/o state)
            cands = sorted(k for k in self.by_city.get(city, set())
                           if k.startswith(net + '|'))
            if len(cands) == 1:
                css = self.by_key[cands[0]]
                key = cands[0]
        return key, css


def resolve_affiliate(name, aff_idx):
    """Resolve "ABC: AL Birmingham ABC 33" -> (callsign list or None)."""
    if not aff_idx:
        return None
    # pipe format: "NBC: WY | Cheyenne | KCHY"
    if '|' in name:
        parts = [p.strip() for p in name.split('|')]
        m = re.match(r'^(ABC|CBS|NBC|FOX|CW)\s*:\s*' + US_AFF_STATE_CODE_ALT + r'$',
                     parts[0], re.I)
        if m and len(parts) >= 2:
            net, st = m.group(1).lower(), m.group(2).upper()
            city = re.sub(r'\s+', ' ', parts[1]).lower().strip()
            if city:
                _, css = aff_idx.lookup(net, st, city)
                return css
        return None
    m = re.match(r'^(ABC|CBS|NBC|FOX|CW)\s*:\s*' + US_AFF_STATE_CODE_ALT +
                 r'\s+(.+)$', name, re.I)
    if m:
        net, st, rest = m.group(1).lower(), m.group(2).upper(), m.group(3).strip()
        rest = US_AFF_BRAND_RE.sub('', rest).strip()
        rest = US_AFF_CALL_RE.sub('', rest).strip()
        city = re.sub(r'\s+', ' ', rest).lower().strip()
        if city:
            _, css = aff_idx.lookup(net, st, city)
            return css
    # spelled-out state: "ABC: Fairbanks ABC Alaska" / "ABC: Columbia Falls Montana"
    m2 = re.match(r'^(ABC|CBS|NBC|FOX|CW)\s*:\s*(.+)$', name, re.I)
    if m2:
        net, rest = m2.group(1).lower(), m2.group(2).strip()
        m3 = re.match(r'^(.*?)\s+' + US_AFF_STATE_NAME_ALT + r'$', rest, re.I)
        if m3:
            city_raw, stname = m3.group(1).strip(), m3.group(2).lower()
            st = US_AFF_STATE_NAMES.get(stname)
            if st:
                city = US_AFF_BRAND_RE.sub('', city_raw).strip()
                city = re.sub(r'\s+', ' ', city).lower().strip()
                if city:
                    _, css = aff_idx.lookup(net, st, city)
                    return css
        # no state anywhere: national-unique city ("ABC: Los Angeles ABC 7")
        rest = US_AFF_BRAND_RE.sub('', rest).strip()
        city = re.sub(r'\s+', ' ', rest).lower().strip()
        if city:
            cands = sorted(k for k in aff_idx.by_city.get(city, set())
                           if k.startswith(net + '|'))
            if len(cands) == 1:
                return aff_idx.by_key[cands[0]]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--streams', required=True)
    ap.add_argument('--sources-index', required=True)
    ap.add_argument('--provider-index', default=None)
    ap.add_argument('--callsigns', default=None, help='fetch_sources call_signs.json')
    ap.add_argument('--us-affiliates', default=None,
                    help='pipeline/us_affiliates.json (Wikipedia affiliate table)')
    ap.add_argument('--fuzzy-threshold', type=float, default=0.85)
    ap.add_argument('-o', '--out', required=True)
    args = ap.parse_args()

    streams = json.load(open(args.streams))
    sources_index = json.load(open(args.sources_index))
    aff_idx = AffiliateIndex(args.us_affiliates) if args.us_affiliates else None

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
        # first one with live programmes). Keys may be raw names or norm() form
        # (norm keys cover all quality/pipe variants of one channel). Diaspora
        # sources participate here too (e.g. PK "Food Food" -> "Foodxp" on
        # tvepg/IN4) — same fallback as the exact loop.
        alias = NAME_ALIASES.get(name) or NAME_ALIASES.get(norm(name))
        if alias:
            for src in name_sources:
                if src.startswith('epgshare01') and src not in allowed_eshare:
                    if not diaspora_allowed(src, cc):
                        continue
                if src in FETCHER_COUNTRIES and cc and cc not in FETCHER_COUNTRIES[src]:
                    continue
                if src.startswith('iptv-org') and not iptv_org_allowed(src, cc):
                    if not diaspora_allowed(src, cc):
                        continue
                if src == 'tvepg' and cc and cc != 'IN':
                    if not diaspora_allowed(src, cc):
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

        # 1c2. US affiliate resolution: "ABC: AL Birmingham ABC 33" carries no
        # call sign — resolve network+state+city -> call sign via the Wikipedia
        # affiliate table, then hit the epgshare01 call-sign index. ONLY fires
        # when the name has no extractable call sign (a name WITH one is either
        # already matched above or untrusted for this path).
        if cc in ('US', 'USA') and aff_idx and not cs:
            aff_css = resolve_affiliate(name, aff_idx)
            if aff_css:
                for acs in aff_css:
                    if acs in cs_index:
                        src, i = cs_index[acs][0]
                        cands.append((TIER['epgshare01'], src, i, 'affiliate', 0.98))
                        stats['affiliate'] += 1
                        break

        # 2. name-based sources, exact (country-gated for epgshare01 /
        #    dedicated fetchers / iptv-org / tvepg; diaspora-exact fallback)
        for src in name_sources:
            if src.startswith('epgshare01') and src not in allowed_eshare:
                if not diaspora_allowed(src, cc):
                    continue
            if src in FETCHER_COUNTRIES and cc and cc not in FETCHER_COUNTRIES[src]:
                continue
            if src.startswith('iptv-org') and not iptv_org_allowed(src, cc):
                if not diaspora_allowed(src, cc):
                    continue
            if src == 'tvepg' and cc and cc != 'IN':
                if not diaspora_allowed(src, cc):
                    continue
            qname = epg_q(name) if src == 'epgone' else name
            eids = src_idx[src].exact(qname)
            if eids:
                # classify: 'exact' if the source is allowed for this stream
                # through its normal gating, else 'diaspora' (exact-match
                # fallback to a foreign lineup that carries the channel)
                if src.startswith('epgshare01'):
                    normal = src in allowed_eshare
                elif src in FETCHER_COUNTRIES:
                    normal = cc is None or cc in FETCHER_COUNTRIES[src]
                elif src.startswith('iptv-org'):
                    normal = iptv_org_allowed(src, cc)
                elif src == 'tvepg':
                    normal = cc is None or cc == 'IN'
                else:
                    normal = True
                method = 'exact' if normal else 'diaspora'
                cands.append((src_tier(src), src, eids[0], method, 0.99))
                stats[f'{src}:{method}'] += 1

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
            if src.startswith('iptv-org') and not iptv_org_allowed(src, cc):
                continue
            if src == 'tvepg' and cc and cc != 'IN':
                continue
            fq = epg_q(name) if src == 'epgone' else name
            for sc, cn, cid in src_idx[src].fuzzy(fq, threshold=args.fuzzy_threshold, limit=2):
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
