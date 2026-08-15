#!/usr/bin/env python3
"""Download third-party EPG source feeds and emit a manifest + name index.

Sources:
  - epg.pw global XMLTV (broad worldwide base)
  - epgshare01 country files (rich US-locals + EU coverage; WebGrab+Plus based)
  - (iptv-org India grab is run separately in the workflow via npm — it needs
     the iptv-org/epg Node toolchain, not plain curl)

Outputs to <outdir>/:
  - epgpw_global.xml.gz
  - es_<NAME>.xml.gz            (one per epgshare01 file)
  - sources.json                manifest: [{source, file, kind}]
  - sources_index.json          {source: {norm_name: [channel_id, ...]}}

Idempotent: skips files already present (non-empty). Set ESHARE_FILES to a
comma list to download a subset (e.g. for local dev).
"""

import gzip
import json
import os
import re
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
# globetvapp/epg — free country XMLTV on GitHub (country dir capitalized, file lowercase)
GLOBETV_BASE = 'https://raw.githubusercontent.com/globetvapp/epg/main/{}/{}'
GLOBETV_FILES = {'india': 5}  # country -> number of numbered .xml.gz files

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
# (US locals + national, UK/IE, CA, then EU, then the rest.)
ESHARE_FILES = [
    'US_LOCALS1', 'US2', 'US_SPORTS1',
    'UK1', 'IE1',
    'CA2',
    'DE1', 'FR1', 'IT1', 'GR1', 'RO1', 'RO2', 'ES1', 'PL1', 'PT1',
    'AU1', 'ZA1',
    'PH2', 'DK1', 'TR1', 'TR3', 'TH1',
    'SE1', 'NL1', 'NO1', 'FI1', 'CY1', 'NZ1', 'BR1', 'BR2', 'CZ1',
    'IN1', 'IN2',
]


def download(url, dest, timeout=300):
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return os.path.getsize(dest)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    with open(dest, 'wb') as f:
        f.write(data)
    return len(data)


def read_xml(path):
    if path.endswith('.gz'):
        return gzip.open(path, 'rb').read().decode('utf-8', errors='ignore')
    return open(path, 'r', errors='ignore').read()


def build_index(path):
    """display-name -> [channel_id] index for one XMLTV file.

    Robust to channels where <icon>/<url> elements precede <display-name>
    (e.g. epgshare01 US locals use Gracenote imagery + url before the name).
    """
    txt = read_xml(path)
    idx = SourceIndex()
    for m in CHAN_BLOCK_RE.finditer(txt):
        dn = DN_RE.search(m.group('body'))
        if dn:
            idx.add(dn.group(1), m.group('id'))
    return idx


def build_callsign_index(path):
    """call-sign -> [channel_id] index for one XMLTV file (US locals)."""
    txt = read_xml(path)
    out = defaultdict(list)
    for m in CHAN_BLOCK_RE.finditer(txt):
        dn = DN_RE.search(m.group('body'))
        if dn:
            cs = call_sign(dn.group(1))
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

    # globetvapp/epg country files
    for country, nfiles in GLOBETV_FILES.items():
        cdir = country.capitalize()
        for i in range(1, nfiles + 1):
            dest = os.path.join(outdir, f'globetv_{country}{i}.xml.gz')
            url = GLOBETV_BASE.format(cdir, f'{country}{i}.xml.gz')
            try:
                n = download(url, dest)
                print(f'[globetv] {country}{i}: {n} bytes')
                manifest.append({'source': f'globetv:{country}{i}',
                                 'file': os.path.abspath(dest), 'kind': 'name'})
            except Exception as e:  # noqa: BLE001
                print(f'[globetv] {country}{i} FAILED: {e}', file=sys.stderr)

    # iptv-org per-site grabs (io_jiotv.xml, io_tataplay.xml, ...) — indexed as
    # separate sources so numeric site_ids can't collide across sites.
    for f in os.environ.get('IPTV_ORG_FILES', '').split(','):
        f = f.strip()
        if f and os.path.exists(f):
            site = os.path.basename(f).replace('io_', '').replace('.xml', '')
            manifest.append({'source': f'iptv-org:{site}', 'file': os.path.abspath(f), 'kind': 'name'})
            print(f'[iptv-org] {site}: {os.path.getsize(f)} bytes')

    json.dump(manifest, open(os.path.join(outdir, 'sources.json'), 'w'), indent=1)

    # name index (display-name -> channel id) per source
    index = {}
    callsigns = {}
    for m in manifest:
        if m['kind'] == 'name':
            try:
                idx = build_index(m['file'])
                index[m['source']] = {k: v for k, v in idx.by_name.items()}
                cs = build_callsign_index(m['file'])
                callsigns[m['source']] = {k: v for k, v in cs.items()}
                print(f'[index] {m["source"]}: {len(idx)} channels, {len(cs)} call signs')
            except Exception as e:  # noqa: BLE001
                print(f'[index] {m["source"]} FAILED: {e}', file=sys.stderr)
    json.dump(index, open(os.path.join(outdir, 'sources_index.json'), 'w'))
    json.dump(callsigns, open(os.path.join(outdir, 'call_signs.json'), 'w'))
    print(f'done: {len(manifest)} sources, {len(index)} indexed')


if __name__ == '__main__':
    main()
