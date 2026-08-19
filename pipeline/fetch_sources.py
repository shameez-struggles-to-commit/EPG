#!/usr/bin/env python3
"""Download third-party EPG source feeds and emit a manifest + name index.

Sources:
  - epg.pw global XMLTV (broad worldwide base)
  - epgshare01 country files (rich US-locals + EU + MENA coverage; WebGrab+Plus)
  - mitthu786/tvepg (India AIO)
  - al7omed/bein-epg (beIN MENA, self-updating via GitHub Actions)
  - (iptv-org grabs are run separately in the workflow via npm — they need
     the iptv-org/epg Node toolchain, not plain curl)

globetvapp/epg was REMOVED 2026-08-15: all 248 upstream feeds went stale
(programme data ends 2025-09..2025-12), including india2.

Outputs to <outdir>/:
  - epgpw_global.xml.gz
  - es_<NAME>.xml.gz            (one per epgshare01 file)
  - bein_mena.xml               (al7omed/bein-epg)
  - sources.json                manifest: [{source, file, kind}]
  - sources_index.json          {source: {norm_name: [channel_id, ...]}}
  - call_signs.json             {source: {callsign: [channel_id, ...]}}

Idempotent: skips files already present (non-empty). Set ESHARE_FILES to a
comma list to download a subset (e.g. for local dev).
"""

import gzip
import html
import json
import os
import re
import ssl
import sys
import urllib.request
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from matcher import SourceIndex

UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'}

EPG_PW_URL = 'https://epg.pw/xmltv/epg.xml.gz'
ESHARE_BASE = 'https://epgshare01.online/epgshare01/epg_ripper_{}.xml.gz'
# mitthu786/tvepg — India OTT EPG (JioTV/TataPlay/Zee5/SonyLIV/SunNXT), one AIO file
TVEPG_URL = 'https://raw.githubusercontent.com/mitthu786/tvepg/main/epg.xml.gz'
# al7omed/bein-epg — beIN MENA sports, self-updating via GitHub Actions
BEIN_URL = 'https://raw.githubusercontent.com/al7omed/bein-epg/main/docs/guide.xml'
# Shadow-GR-Official/cyta.cy-epg — Cyprus CyTA platform incl. the NOVA bouquet
# (Novacinema 1-4, Novasports Start/Prime/1-6/Extra, Novalife). Verified
# 2026-08-19: 118/118 usable, current. Net-new vs the iptv-org cyta grabber
# (which yields only ~7 channels).
CYTA_URL = 'https://raw.githubusercontent.com/Shadow-GR-Official/cyta.cy-epg/refs/heads/main/data/epg.xml'
# chrisliatas/greek-xmltv — Digea DTT (all regions) + ERT, daily GitHub
# release. Verified 2026-08-19: 101/101 usable, current. Greek-script names
# (matcher handles via gr_translit in build_mapping epilogue).
GREEK_URL = 'https://github.com/chrisliatas/greek-xmltv/releases/latest/download/xmltv_GREECE_el.xml.gz'
# i.mjh.nz PlutoTV US — FAST loop channels (24/7 family): ~15 of our 24/7
# streams have real episode schedules here (CSI Miami, Frasier, ...).
# Exact-name matching only, gated to 24/7-category streams (source 'plutofast').
PLUTOFAST_URL = 'https://i.mjh.nz/PlutoTV/us.xml.gz'

# Channel parsing (robust: icon/url may precede display-name).
CHAN_BLOCK_RE = re.compile(r'<channel\s+id="(?P<id>[^"]*)"[^>]*>(?P<body>.*?)</channel>', re.S)
DN_RE = re.compile(r'<display-name[^>]*>([^<]*)</display-name>')

# US call signs (K/W + 2-4 alnum chars), used to match provider "FOX: FL | Tampa | WTVT"
# against epgshare01 "WTVT-DT.us_locals1".
CALLSIGN_RE = re.compile(r'^[KW][A-Z0-9]{2,4}$')


def call_sign(display_name):
    """Return the lowercased call sign of a display-name like 'WTVT-DT', else None."""
    base = (display_name or '').split('-')[0].strip().upper()
    return base.lower() if CALLSIGN_RE.match(base) else None


# Country files, ordered roughly by expected yield for this provider's lineup.
# 2026-08-15 additions (global source audit): AE1 (UAE/MENA hub — DX/RSL/MO
# gaps), IN4, CH1, AT1, BE2, AL1 (SuperSport ZA), ASIANTELEVISION1 (ARY QTV/
# ATN News), MT1, HU1, SK1. Removed PH1/PK1 (0 programmes, verified).
ESHARE_FILES = [
    'US_LOCALS1', 'US2', 'US_SPORTS1',
    'UK1', 'IE1',
    'CA2',
    'DE1', 'FR1', 'IT1', 'GR1', 'RO1', 'RO2', 'ES1', 'PL1', 'PT1',
    'AU1', 'ZA1',
    'PH2', 'DK1', 'TR1', 'TR3', 'TH1',
    'SE1', 'NL1', 'NO1', 'FI1', 'CY1', 'NZ1', 'BR1', 'BR2', 'CZ1',
    'IN1', 'IN2', 'IN4',
    'AE1', 'AL1', 'CH1', 'AT1', 'BE2', 'ASIANTELEVISION1', 'MT1', 'HU1', 'SK1',
]

# epgshare01 file -> additional ISO country codes it may serve (beyond its
# own country). Files are matched against a stream's country via
# build_mapping.COUNTRY_SOURCES; entries here keep MENA/regional files usable.
EXTRA_COUNTRY_SOURCES = {
    'AE1': ['AE', 'SA', 'QA', 'KW', 'OM', 'BH', 'JO', 'EG', 'MO'],  # pan-Arab hub
    'AL1': ['AL', 'BA', 'HR', 'ME', 'MK', 'RS', 'SI', 'XK'],
    'CH1': ['CH', 'DE', 'AT'],
    'AT1': ['AT', 'DE'],
    'BE2': ['BE', 'NL', 'LU'],
    'ASIANTELEVISION1': ['UK', 'BD', 'IN', 'PK'],
    'MT1': ['MT', 'IT'],
    'HU1': ['HU', 'RO', 'SK', 'CZ'],
    'SK1': ['SK', 'CZ'],
    'AR1': ['AR', 'UY', 'PY', 'BO', 'EC', 'CO', 'CL', 'PE', 'VE'],
    'SA2': ['SA'],
    'BEIN1': ['QA', 'AE', 'SA', 'MO', 'EG'],
}


def download(url, dest, timeout=300, insecure=False):
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return os.path.getsize(dest)
    req = urllib.request.Request(url, headers=UA)
    ctx = None
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        data = r.read()
    with open(dest, 'wb') as f:
        f.write(data)
    return len(data)


def read_xml(path):
    if path.endswith('.gz'):
        return gzip.open(path, 'rb').read().decode('utf-8', errors='ignore')
    return open(path, 'r', errors='ignore').read()


def build_index(path, greek_translit=False):
    """display-name -> [channel_id] index for one XMLTV file.

    Robust to channels where <icon>/<url> elements precede <display-name>
    (e.g. epgshare01 US locals use Gracenote imagery + url before the name).
    Display-names are HTML-unescaped so '&amp;pictures' indexes as
    '&pictures' (norm 'pictures') and matches the provider's '& pictures'.
    greek_translit=True (chrisliatas pack) also adds a Latin-transliterated
    entry for Greek-script names so Latin queries can hit them.
    """
    txt = read_xml(path)
    idx = SourceIndex()
    for m in CHAN_BLOCK_RE.finditer(txt):
        dn = DN_RE.search(m.group('body'))
        if dn:
            name = html.unescape(dn.group(1))
            idx.add(name, m.group('id'))
            if greek_translit:
                lat = _gr_translit(name)
                if lat != name:
                    idx.add(lat, m.group('id'))
    return idx


GR2LAT = {
    'α': 'a', 'β': 'v', 'γ': 'g', 'δ': 'd', 'ε': 'e', 'ζ': 'z', 'η': 'i',
    'θ': 'th', 'ι': 'i', 'κ': 'k', 'λ': 'l', 'μ': 'm', 'ν': 'n', 'ξ': 'x',
    'ο': 'o', 'π': 'p', 'ρ': 'r', 'σ': 's', 'ς': 's', 'τ': 't', 'υ': 'y',
    'φ': 'f', 'χ': 'ch', 'ψ': 'ps', 'ω': 'o',
    'ά': 'a', 'έ': 'e', 'ί': 'i', 'ό': 'o', 'ύ': 'y', 'ή': 'i', 'ώ': 'o',
    'ΐ': 'i', 'ΰ': 'y', 'ϊ': 'i', 'ϋ': 'y',
}


def _gr_translit(s):
    return ''.join(GR2LAT.get(c, c) for c in (s or '').lower())


def build_callsign_index(path):
    """call-sign -> [channel_id] index for one XMLTV file (US locals)."""
    txt = read_xml(path)
    out = defaultdict(list)
    for m in CHAN_BLOCK_RE.finditer(txt):
        dn = DN_RE.search(m.group('body'))
        if dn:
            cs = call_sign(html.unescape(dn.group(1)))
            if cs:
                out[cs].append(m.group('id'))
    return out


def main():
    outdir = sys.argv[1] if len(sys.argv) > 1 else './data'
    os.makedirs(outdir, exist_ok=True)

    files = os.environ.get('ESHARE_FILES', '').split(',')
    files = [f.strip() for f in files if f.strip()] or ESHARE_FILES

    manifest = []
    # epg.pw
    pw_path = os.path.join(outdir, 'epgpw_global.xml.gz')
    try:
        n = download(EPG_PW_URL, pw_path)
        print(f'[epg.pw] {n} bytes -> {pw_path}')
        manifest.append({'source': 'epg.pw', 'file': os.path.abspath(pw_path), 'kind': 'name'})
    except Exception as e:  # noqa: BLE001
        print(f'[epg.pw] FAILED: {e}', file=sys.stderr)

    # epgshare01
    for name in files:
        dest = os.path.join(outdir, f'es_{name}.xml.gz')
        url = ESHARE_BASE.format(name)
        try:
            n = download(url, dest)
            print(f'[epgshare01] {name}: {n} bytes')
            manifest.append({'source': f'epgshare01:{name}', 'file': os.path.abspath(dest), 'kind': 'name'})
        except Exception as e:  # noqa: BLE001
            print(f'[epgshare01] {name} FAILED: {e}', file=sys.stderr)

    # mitthu786/tvepg — India OTT EPG (one AIO file, 1500+ channels)
    tvepg_path = os.path.join(outdir, 'tvepg_india.xml.gz')
    try:
        n = download(TVEPG_URL, tvepg_path)
        print(f'[tvepg] {n} bytes')
        manifest.append({'source': 'tvepg', 'file': os.path.abspath(tvepg_path), 'kind': 'name'})
    except Exception as e:  # noqa: BLE001
        print(f'[tvepg] FAILED: {e}', file=sys.stderr)

    # al7omed/bein-epg — beIN MENA sports (39 channels, self-updating)
    bein_path = os.path.join(outdir, 'bein_mena.xml')
    try:
        n = download(BEIN_URL, bein_path)
        print(f'[bein] {n} bytes')
        manifest.append({'source': 'bein', 'file': os.path.abspath(bein_path), 'kind': 'name'})
    except Exception as e:  # noqa: BLE001
        print(f'[bein] FAILED: {e}', file=sys.stderr)

    # CyTA Cyprus pack (NOVA bouquet + Cypriot linears)
    cyta_path = os.path.join(outdir, 'cyta_pack.xml')
    try:
        n = download(CYTA_URL, cyta_path)
        print(f'[cyta] {n} bytes')
        manifest.append({'source': 'cyta', 'file': os.path.abspath(cyta_path), 'kind': 'name'})
    except Exception as e:  # noqa: BLE001
        print(f'[cyta] FAILED: {e}', file=sys.stderr)

    # chrisliatas/greek-xmltv (Digea DTT + ERT, daily release)
    greek_path = os.path.join(outdir, 'greek_pack.xml.gz')
    try:
        n = download(GREEK_URL, greek_path)
        print(f'[greek] {n} bytes')
        manifest.append({'source': 'greek', 'file': os.path.abspath(greek_path), 'kind': 'name'})
    except Exception as e:  # noqa: BLE001
        print(f'[greek] FAILED: {e}', file=sys.stderr)

    # i.mjh.nz PlutoTV US (FAST 24/7 loop channels)
    pluto_path = os.path.join(outdir, 'plutofast.xml.gz')
    try:
        n = download(PLUTOFAST_URL, pluto_path)
        print(f'[plutofast] {n} bytes')
        manifest.append({'source': 'plutofast', 'file': os.path.abspath(pluto_path), 'kind': 'name'})
    except Exception as e:  # noqa: BLE001
        print(f'[plutofast] FAILED: {e}', file=sys.stderr)

    # dedicated fetcher outputs (generated earlier by the workflow step):
    #   ALLENTE_FILE / TEAMS_FILE / BBCRADIO_FILE (name-indexed like the others)
    for env, src in (('ALLENTE_FILE', 'allente'), ('TEAMS_FILE', 'teamfixtures'),
                     ('BBCRADIO_FILE', 'bbcradio')):
        f = os.environ.get(env, '').strip()
        if f and os.path.exists(f):
            manifest.append({'source': src, 'file': os.path.abspath(f), 'kind': 'name'})
            print(f'[{src}] {os.path.getsize(f)} bytes')

    # iptv-org per-site grabs (io_jiotv.xml, io_tataplay.xml, ...) — indexed as
    # separate sources so numeric site_ids can't collide across sites.
    # Entries may override the source name: "name=path" (used when one site's
    # grab is split across several files, e.g. programtv.onet.pl_a/_b — all
    # files merge under one source name).
    for f in os.environ.get('IPTV_ORG_FILES', '').split(','):
        f = f.strip()
        if not f:
            continue
        src = None
        if '=' in f:
            src, f = f.split('=', 1)
            src = src.strip()
        if not os.path.exists(f):
            continue
        if src is None:
            site = os.path.basename(f).replace('io_', '').replace('.xml', '')
            src = f'iptv-org:{site}'
        manifest.append({'source': src, 'file': os.path.abspath(f), 'kind': 'name'})
        print(f'[iptv-org] {src}: {os.path.getsize(f)} bytes ({os.path.basename(f)})')

    # dedicated fetcher outputs, indexed under their own source names so
    # build_mapping country-gates them correctly:
    #   SKYHAWK_FILE=.../skyhawk.xml  DSTV_FILE=.../dstv.xml  EPGONE_FILE=...
    for env, src in (('SKYHAWK_FILE', 'skyhawk'), ('DSTV_FILE', 'dstv'),
                     ('EPGONE_FILE', 'epgone')):
        f = os.environ.get(env, '').strip()
        if f and os.path.exists(f):
            manifest.append({'source': src, 'file': os.path.abspath(f), 'kind': 'name'})
            print(f'[{src}] {os.path.getsize(f)} bytes')

    json.dump(manifest, open(os.path.join(outdir, 'sources.json'), 'w'), indent=1)

    # name index (display-name -> channel id) per source. Multiple manifest
    # entries may share one source name (split grabs) — their indexes MERGE.
    index = {}
    callsigns = {}
    for m in manifest:
        if m['kind'] == 'name':
            try:
                idx = build_index(m['file'], greek_translit=(m['source'] == 'greek'))
                if m['source'] in index:
                    for k, v in idx.by_name.items():
                        index[m['source']].setdefault(k, []).extend(v)
                else:
                    index[m['source']] = {k: list(v) for k, v in idx.by_name.items()}
                cs = build_callsign_index(m['file'])
                if m['source'] in callsigns:
                    for k, v in cs.items():
                        callsigns[m['source']].setdefault(k, []).extend(v)
                else:
                    callsigns[m['source']] = {k: list(v) for k, v in cs.items()}
                print(f'[index] {m["source"]}: {len(idx)} channels, {len(cs)} call signs')
            except Exception as e:  # noqa: BLE001
                print(f'[index] {m["source"]} FAILED: {e}', file=sys.stderr)
    json.dump(index, open(os.path.join(outdir, 'sources_index.json'), 'w'))
    json.dump(callsigns, open(os.path.join(outdir, 'call_signs.json'), 'w'))
    print(f'done: {len(manifest)} sources, {len(index)} indexed')


if __name__ == '__main__':
    main()
