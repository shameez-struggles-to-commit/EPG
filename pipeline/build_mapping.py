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
import hashlib
import json
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from matcher import SourceIndex, norm, is_non_linear, cyr_to_lat

TIER = {
    'pk': 0, 'iptv-org': 1, 'skyhawk': 2, 'dstv': 2, 'epgone': 2,
    'bein': 2, 'teamfixtures': 2, 'allente': 2, 'cyta': 2, 'greek': 2,
    'plutofast': 2, 'bbcradio': 2, 'epgshare01': 3, 'epg.pw': 4, 'provider': 5,
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
    'UK': 'UK', 'IRE': 'IE', 'GB': 'UK', 'ITV': 'UK', 'BBC': 'UK', 'SC': 'UK',
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
    'IT': ['IT1', 'CH1', 'MT1'], 'GR': ['GR1', 'CY1'], 'RO': ['RO1', 'RO2', 'HU1', 'DK1', 'ES1'],
    'ES': ['ES1', 'DK1'], 'PL': ['PL1', 'CZ1'], 'PT': ['PT1', 'GR1', 'PL1'],
    'AU': ['AU1'], 'ZA': ['ZA1', 'AL1'], 'PH': ['PH2'],
    'DK': ['DK1'], 'TR': ['TR1', 'TR3'], 'TH': ['TH1'],
    'SE': ['SE1'], 'NL': ['NL1', 'BE2'], 'NO': ['NO1'], 'FI': ['FI1'],
    'CY': ['CY1', 'GR1', 'TR1', 'TR3'], 'NZ': ['NZ1'], 'BR': ['BR1', 'BR2'], 'CZ': ['CZ1', 'SK1', 'HU1'],
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
    'epgone': {'UA', 'MD', 'RO'},
    'bein': {'QA', 'AE', 'SA', 'MA', 'EG'},
    'allente': {'DK', 'NO', 'FI', 'SE'},
    'cyta': {'CY', 'GR'},
    'greek': {'GR'},
    'plutofast': {'US', 'UK', 'CA', 'AU', 'DE', 'IN'},  # FAST loop channels (24/7 family)
    'teamfixtures': {'US', 'UK', 'ES', 'IT', 'SC'},    # team-dedicated channels (fixtures)
    'bbcradio': {'UK', 'IE'},                          # radio (name-matched only)
}

# provider country -> Sky territory prefix, for filtering skyhawk source IDs
# ("GB#2075" / "DE#9135" / "IT#522"). Mirrors fetch_skyhawk.COUNTRY_TERRITORY.
# A stream whose territory doesn't match the candidate's prefix must not take
# that candidate (else UK History gets Italian History — found by AUDIT-3).
SKY_TERRITORY = {
    'UK': 'GB', 'GB': 'GB', 'IRE': 'GB', 'IE': 'GB',
    'DE': 'DE', 'AT': 'DE', 'CH': 'DE',
    'IT': 'IT',
    'IN': 'GB', 'PK': 'GB', 'BD': 'GB',
}

# provider country -> Allente feed prefix ("dk:30001" / "no:50047" / "fi:40051").
# The allente source merges DK/NO/FI feeds under one index; duplicate names
# across feeds (e.g. "V Sport 1" exists in all three) resolve by prefix.
# AUDIT-4: Norwegian V Sport 1 was selecting the Finnish fi:40051 feed.
ALLENTE_PREFIX = {'DK': 'dk', 'NO': 'no', 'FI': 'fi', 'SE': 'se'}

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
    'tvpassport.com': {'US', 'CA', 'MX', 'DE'},
    'tv24.co.uk': {'UK', 'IE', 'DE'},
    'tvireland.ie': {'IE', 'UK'},
    'www.magenta.tv': {'DE', 'FR'}, 'web.magentatv.de': {'DE', 'FR'},
    'tv.blue.ch': {'CH', 'DE', 'FR', 'IT', 'AT', 'GR', 'RO'},
    'abc.net.au': {'AU'}, 'foxtel.com.au': {'AU'},
    'tvhebdo.com': {'CA', 'FR'},
    'programetv.ro': {'RO'},
    'programacion-tv.elpais.com': {'ES'}, 'movistarplus.es': {'ES'},
    'programme-tv.net': {'FR'}, 'tvcesoir.fr': {'FR'},
    'meo.pt': {'PT'}, 'nostv.pt': {'PT'},
    'guidatv.sky.it': {'IT'},
    'cosmotetv.gr': {'GR'},
    'cyta.com.cy': {'CY', 'GR'},
    'allente.se': {'SE'},
    'tv24.se': {'SE', 'DK', 'NO'},
    'mujtvprogram.cz': {'CZ', 'SK'},
    'tvmustra.hu': {'HU', 'PT'},
    'gigatv.3bbtv.co.th': {'TH'},
    'tv.trueid.net': {'TH'},
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


def build_collision_split(streams):
    """Return an immutable stream_id -> XMLTV channel-id map.

    Every stream with an empty/junk provider ID, and every member of a
    duplicated provider-ID group, gets `xtream:<stream_id>`. Keeping one
    arbitrary member on a shared provider ID is still unsafe because the
    provider ID is demonstrably reused across countries/channels. Unique,
    validated provider IDs remain unchanged for compatibility.
    """
    from collections import Counter
    counts = Counter((s.get('epg_channel_id') or '').strip() for s in streams
                     if is_real_epg_id((s.get('epg_channel_id') or '').strip()))
    identity = {}
    for s in streams:
        stream_id = str(s.get('stream_id') or '').strip()
        name = s.get('name', '')
        eid = (s.get('epg_channel_id') or '').strip()
        if not stream_id:
            # Provider streams normally always have stream_id. This fallback is
            # deliberately marked unstable rather than silently pretending a
            # display name is an identity.
            identity[name] = f'xtream:missing:{hashlib.sha256(name.encode()).hexdigest()[:16]}'
        elif not is_real_epg_id(eid) or counts.get(eid, 0) > 1:
            identity[stream_id] = f'xtream:{stream_id}'
        else:
            identity[stream_id] = eid
    return identity


def build_identity_map(streams):
    """Alias for the public identity-map operation (kept for callers)."""
    return build_collision_split(streams)


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


# Greek-script -> Latin transliteration (for the chrisliatas/greek-xmltv pack:
# display-names are Greek ('ΜΑΚΕΔΟΝΙΑ ΤV') while provider names are Latin
# ('Makedonia TV'). Applied on BOTH sides of the 'greek' source match.
GR2LAT = {
    'α': 'a', 'β': 'v', 'γ': 'g', 'δ': 'd', 'ε': 'e', 'ζ': 'z', 'η': 'i',
    'θ': 'th', 'ι': 'i', 'κ': 'k', 'λ': 'l', 'μ': 'm', 'ν': 'n', 'ξ': 'x',
    'ο': 'o', 'π': 'p', 'ρ': 'r', 'σ': 's', 'ς': 's', 'τ': 't', 'υ': 'y',
    'φ': 'f', 'χ': 'ch', 'ψ': 'ps', 'ω': 'o',
    'ά': 'a', 'έ': 'e', 'ί': 'i', 'ό': 'o', 'ύ': 'y', 'ή': 'i', 'ώ': 'o',
    'ΐ': 'i', 'ΰ': 'y', 'ϊ': 'i', 'ϋ': 'y',
}


def gr_translit(s):
    return ''.join(GR2LAT.get(c, c) for c in (s or '').lower())


def greek_q(name):
    """Query transform for the 'greek' source (Greek pack)."""
    return norm(gr_translit(name))


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


EVENT_ONLY_NAME_RE = re.compile(
    r'(?:\b(?:NBA|MLB|UFC|PPV)\s*\d{1,2}\s*[|:]|'
    r'\bNHL\s+Center\s+Ice\s*\d+|'
    r'\bNFL\s+(?:Sunday\s+Ticket|RedZone)\s*\d*|'
    r'\b(?:UEFA|EFL|FA\s+Cup)\s*\d+\s*[|:]|'
    r'\bSky\s+Sports\s*\+\s*\|?\s*Event\s*\d*|'
    r'\bPremier\s+Sports\s+GB\s*\|\s*Event\s*\d*|'
    r'^\s*\d{1,2}\s*\|\s*\d{1,2}:\d{2}\b|'
    r'\(Events?\s+Only\)|\(Event\s+Only\))', re.I)


def is_event_only_stream(name, category=''):
    if (re.search(r'\bUFC\b', category or '', re.I)
            and not re.search(r'^(?:UK\s*\|\s*)?UFC\s*TV\b', name or '', re.I)):
        return True
    return bool(EVENT_ONLY_NAME_RE.search(name or '') or
                re.search(r'\b(?:Events?\s+Only|Event\s+Only|Todays\s+Live\s+Events?)\b', category or '', re.I))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--streams', required=True)
    ap.add_argument('--sources-index', required=True)
    ap.add_argument('--provider-index', default=None)
    ap.add_argument('--callsigns', default=None, help='fetch_sources call_signs.json')
    ap.add_argument('--us-affiliates', default=None,
                    help='pipeline/us_affiliates.json (Wikipedia affiliate table)')
    ap.add_argument('--teams-claim', default=None,
                    help='team-fixture claim list JSON (names to let through is_non_linear)')
    ap.add_argument('--fuzzy-threshold', type=float, default=0.85)
    ap.add_argument('-o', '--out', required=True)
    args = ap.parse_args()

    streams = json.load(open(args.streams))
    sources_index = json.load(open(args.sources_index))
    aff_idx = AffiliateIndex(args.us_affiliates) if args.us_affiliates else None

    # Team-fixture claim list: names of team-dedicated channels the fixture
    # generator populated. These channels' categories contain "EPL"/"NFL" etc.
    # which is_non_linear() would normally drop; the claim list lets them
    # through so their fixtures reach the guide.
    teams_claim = set()
    teams_claim_names = set()
    if args.teams_claim and os.path.exists(args.teams_claim):
        raw_claim = json.load(open(args.teams_claim))
        # Current format is immutable stream IDs. Accept legacy name claims for
        # one migration cycle, but never use raw whitespace as identity.
        for x in raw_claim:
            sx = str(x).strip()
            if sx.isdigit():
                teams_claim.add(sx)
            else:
                teams_claim_names.add(sx)
    print(f'[mapping] team-fixture claim list: {len(teams_claim)} stream IDs, '
          f'{len(teams_claim_names)} legacy names')

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
        if s in ('skyhawk', 'dstv', 'epgone', 'bein', 'teamfixtures',
                 'allente', 'cyta', 'greek', 'plutofast', 'bbcradio'):
            return TIER[s]
        if s.startswith('epgshare01'):
            return TIER['epgshare01']
        if s == 'epg.pw':
            return TIER['epg.pw']
        return 99

    name_sources = sorted(sources_index, key=src_tier)
    src_idx = {s: rebuild_index(m) for s, m in sources_index.items()}

    # Dedicated-source rescue: a "non-linear" category (24/7 loops, radio) may
    # still get real EPG from a dedicated fetcher — but ONLY via an exact name
    # match, and ONLY for that one source. Build a name->source map for streams
    # that qualify, so the gate below can let them through with candidates
    # restricted to their dedicated source (no fuzzy, no provider fallback).
    dedicated_rescue = {}  # stream name -> source name
    if 'plutofast' in src_idx:
        for s in streams:
            nm = (s.get('name') or '').strip()
            if nm and '24/7' in (s.get('cat_name') or '').lower():
                if src_idx['plutofast'].exact(nm):
                    dedicated_rescue[nm] = 'plutofast'
    if 'bbcradio' in src_idx:
        for s in streams:
            nm = (s.get('name') or '').strip()
            if nm and 'radio' in (s.get('cat_name') or '').lower():
                if src_idx['bbcradio'].exact(nm):
                    dedicated_rescue[nm] = 'bbcradio'

    prov_by_name = {}
    provider_programme_names = set()
    if args.provider_index:
        pidx = json.load(open(args.provider_index))
        prov_by_name = {norm(dn): cid for cid, dn in pidx.get('ids', {}).items() if cid}
        provider_programme_names = {norm(dn) for dn in pidx.get('names_with_progs', []) if dn}

    mapping = {}
    stats = defaultdict(int)
    # AUDIT-4 F-01: split duplicated provider epg_ids onto stable synthetic
    # xtream:<stream_id> ids so unrelated streams stop merging into one
    # XMLTV channel (882 streams were affected).
    identity_map = build_identity_map(streams)
    stats['identity-synthetic-streams'] = sum(1 for x in identity_map.values() if x.startswith('xtream:'))
    print(f'[mapping] identity map: {len(identity_map)} streams; '
          f"{stats['identity-synthetic-streams']} synthetic xtream IDs")
    for s in streams:
        name = s.get('name', '')
        sid = str(s.get('stream_id', name))
        cat = s.get('cat_name', '')
        cc = country_hint(cat)

        # Dedicated-source rescue: a non-linear stream with an exact match in
        # its dedicated fetcher (plutofast for 24/7, bbcradio for radio) gets
        # candidates ONLY from that source — no fuzzy, no provider fallback.
        rescue_src = dedicated_rescue.get(name)
        team_claimed = sid in teams_claim or name.strip() in teams_claim_names

        # skip non-linear channels entirely (no EPG applies) — EXCEPT channels
        # the team-fixture generator claimed, a dedicated source rescued, or a
        # valid provider ID/name proves this is actually a linear channel.
        provider_linear_rescue = (
            is_real_epg_id(s.get('epg_channel_id'))
            and norm(name) in provider_programme_names
            and not is_event_only_stream(name, cat)
        )
        # Categoryless radio names are not safely matchable by global fuzzy
        # search. A reviewed bbcradio exact rescue may still pass above.
        unnamed_radio = (cc is None and re.search(r'\bradio\b', name, re.I)
                         and not rescue_src)
        if (is_non_linear(cat, name) or unnamed_radio or is_event_only_stream(name, cat)) and not team_claimed and not rescue_src and not provider_linear_rescue:
            stats['non-linear-skipped'] += 1
            continue

        allowed_eshare = {f'epgshare01:{f}' for f in COUNTRY_SOURCES.get(cc, [])}

        cands = []

        # rescued channels: ONLY the dedicated source, exact match, then skip
        # all other candidate paths (pk/alias/callsign/provider/fuzzy).
        if rescue_src:
            eids = src_idx[rescue_src].exact(name)
            if eids:
                cands.append((src_tier(rescue_src), rescue_src, eids[0], 'exact', 0.99))
                stats[f'{rescue_src}:exact'] += 1
            if not cands:
                stats['unmatched'] += 1
            # write mapping entry directly (or fall through to shared tail)
            if cands:
                mapping[sid] = {
                    'name': name, 'cat_name': cat, 'country': cc,
                    'canonical_id': identity_map.get(sid) or canonical_id(s),
                    'candidates': [{'source': c[1], 'source_id': c[2], 'method': c[3],
                                    'confidence': c[4]} for c in cands],
                }
            continue

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
                alias_diaspora = False
                if src.startswith('epgshare01') and src not in allowed_eshare:
                    if not diaspora_allowed(src, cc):
                        continue
                    alias_diaspora = True
                if src in FETCHER_COUNTRIES and cc and cc not in FETCHER_COUNTRIES[src]:
                    continue
                if src.startswith('iptv-org') and not iptv_org_allowed(src, cc):
                    if not diaspora_allowed(src, cc):
                        continue
                    alias_diaspora = True
                if src == 'tvepg' and cc and cc != 'IN':
                    if not diaspora_allowed(src, cc):
                        continue
                    alias_diaspora = True
                eids = src_idx[src].exact(alias)
                if src == 'epg.pw' and len(eids) > 1:
                    stats['epg.pw:ambiguous-alias'] += 1
                    continue
                if src == 'skyhawk':
                    terr = SKY_TERRITORY.get(cc)
                    if terr:
                        eids = [e for e in eids if isinstance(e, str) and e.startswith(terr + '#')]
                if eids:
                    alias_tier = src_tier(src) + (20 if alias_diaspora else 0)
                    cands.append((alias_tier, src, eids[0],
                                  'diaspora-alias' if alias_diaspora else 'alias', 0.97))
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
            qname = name.strip() if src == 'teamfixtures' else name
            if src == 'epgone':
                qname = epg_q(name)
            elif src == 'greek':
                qname = greek_q(name)
            eids = src_idx[src].exact(qname)
            if src == 'epg.pw' and len(eids) > 1:
                # epg.pw is a worldwide aggregate with opaque numeric IDs;
                # duplicate normalized names are different regions/variants.
                # Never choose eids[0] by file order.
                stats['epg.pw:ambiguous-exact'] += 1
                continue
            if src == 'skyhawk':
                # skyhawk source IDs carry a territory prefix ("GB#2075");
                # keep only candidates whose territory matches this stream's
                # country (else a UK "History" takes Italian "IT#522").
                terr = SKY_TERRITORY.get(cc)
                if terr:
                    eids = [e for e in eids if isinstance(e, str) and e.startswith(terr + '#')]
            elif src == 'allente':
                # allente merges DK/NO/FI feeds with country-prefixed IDs
                # ("no:50047"); keep only this stream's country feed.
                pref = ALLENTE_PREFIX.get(cc)
                if pref:
                    eids = [e for e in eids if isinstance(e, str) and e.startswith(pref + ':')]
            elif src == 'teamfixtures':
                # teamfixtures keys channels by the EXACT provider stream name.
                # norm() strips "tv", so "Real Madrid TV" would collide with the
                # "Real Madrid" team feed — require the exact claimed name.
                eids = [e for e in eids if e == qname]
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
                candidate_tier = src_tier(src) + (20 if method == 'diaspora' else 0)
                cands.append((candidate_tier, src, eids[0], method, 0.99))
                stats[f'{src}:{method}'] += 1

        # 3. provider (epg_channel_id then display-name)
        # AUDIT-4 F-03: a valid provider epg-id is the HIGHEST-trust identity
        # signal — it outranks name-based sources (the old tier put broad
        # epg.pw above the provider's own explicit id, letting a generic
        # name-match schedule beat the provider's actual feed). Tier 1.5:
        # below pk overrides, above every name source. Also: streams whose
        # provider id is a COLLISION id (split onto xtream:<sid>) must not
        # ALSO emit the shared id as a candidate.
        prov_cid = str(s.get('epg_channel_id') or '').strip()
        if is_real_epg_id(prov_cid) and identity_map.get(sid) == prov_cid:
            cands.append((1.5, 'provider', prov_cid, 'epg-id', 1.0))
            stats['provider:epg-id'] += 1
        elif is_real_epg_id(prov_cid) and identity_map.get(sid, '').startswith('xtream:'):
            # synthetic identity: the duplicated provider ID is not safe as a
            # shared candidate; source/name candidates may still provide data.
            stats['provider:epg-id-split'] += 1
        else:
            pn = norm(name)
            if pn and pn in prov_by_name:
                # AUDIT-4 F-03: provider-name fallback was NOT country-gated;
                # 'FOX SPORTS 2' in ES/BR categories took foxsports2.us.
                # Gate on the id's TLD suffix agreeing with the stream country
                # (when both are known) before accepting the fallback.
                cand_cid = prov_by_name[pn]
                tld = cand_cid.rsplit('.', 1)[-1].upper() if '.' in cand_cid else None
                cc_norm = {'UK': 'GB', 'IRE': 'IE', 'SC': 'GB', 'USA': 'US'}.get(cc, cc)
                tld_ok = (tld is None) or (tld == cc_norm) or (cc_norm is None)
                if tld_ok:
                    cands.append((TIER['provider'], 'provider', cand_cid, 'name', 0.9))
                    stats['provider:name'] += 1
                else:
                    stats['provider:name-country-rejected'] += 1

        # fuzzy (lowest trust, appended last, country-gated)
        for src in name_sources:
            # A missing country is not permission to search every regional
            # lineup. This was the Capital Radio -> Italian Radio Capital bug.
            if cc is None and (src in FETCHER_COUNTRIES or src.startswith('epgshare01')
                               or src.startswith('iptv-org') or src == 'tvepg'):
                continue
            if src == 'teamfixtures':
                continue  # team channels are exact-name only (norm strips "tv",
                # so fuzzy would claim "Real Madrid TV" for the "Real Madrid" feed)
            if src.startswith('epgshare01') and src not in allowed_eshare:
                continue
            if src in FETCHER_COUNTRIES and cc and cc not in FETCHER_COUNTRIES[src]:
                continue
            if src.startswith('iptv-org') and not iptv_org_allowed(src, cc):
                continue
            if src == 'tvepg' and cc and cc != 'IN':
                continue
            fq = name
            if src == 'epgone':
                fq = epg_q(name)
            elif src == 'greek':
                fq = greek_q(name)
            for sc, cn, cid in src_idx[src].fuzzy(fq, threshold=args.fuzzy_threshold, limit=2):
                if src == 'skyhawk':
                    terr = SKY_TERRITORY.get(cc)
                    if terr and (not isinstance(cid, str) or not cid.startswith(terr + '#')):
                        continue
                if src == 'allente':
                    pref = ALLENTE_PREFIX.get(cc)
                    if pref and (not isinstance(cid, str) or not cid.startswith(pref + ':')):
                        continue
                if src == 'epg.pw':
                    # AUDIT-7 P1-5: the same opaque-ID ambiguity that affects
                    # exact epg.pw names also affects fuzzy hits. When the
                    # matched source name bucket holds multiple IDs, file
                    # order must not pick the region.
                    ids = src_idx[src].by_name.get(cn) or [cid]
                    if len(ids) > 1:
                        stats['epg.pw:ambiguous-fuzzy'] += 1
                        continue
                cands.append((50 + src_tier(src), src, cid, 'fuzzy', round(sc, 2)))
                stats[f'{src}:fuzzy'] += 1

        if team_claimed:
            # Claimed team streams must never fall through to a broadcaster
            # schedule when the fixture source is empty/stale.
            cands = [c for c in cands if c[1] == 'teamfixtures']
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
            'canonical_id': identity_map.get(sid) or canonical_id(s),
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
