#!/usr/bin/env python3
"""Generate filtered *.channels.xml files for iptv-org/epg grabbers.

The iptv-org sites carry thousands of channels (dstv.com alone lists ~3k);
grabbing everything is slow and wasteful. This script downloads each site's
channels.xml, keeps only channels whose normalized name matches a provider
stream (exact, or fuzzy dice >= 0.80), and writes a filtered file the grabber
consumes via --channels=<file>.

Validated site list from the 2026-08-15 global source audit (all produced
real programme data in test grabs).

Usage: make_iptvorg_channels.py <streams.json> <outdir>
Emits: <outdir>/<site>.channels.xml (filtered) + prints per-site keep counts.
"""

import json
import os
import re
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from matcher import SourceIndex, norm

UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'}
BASE = 'https://raw.githubusercontent.com/iptv-org/epg/master/sites/{dir}/{site}.channels.xml'

# Validated grabbers (2026-08-15 audit). India sites are grabbed unfiltered
# in the existing workflow step (full-lineup grabs there are cheap and the
# coverage is broad); this list is the NON-India expansion.
SITES = [
    'tvpassport.com',      # US incl. no-callsign locals
    'tv24.co.uk',          # UK
    'tvireland.ie',        # IE
    'programtv.onet.pl',   # PL
    'www.magenta.tv',      # DE
    'web.magentatv.de',    # DE
    'tv.blue.ch',          # CH (DE/FR/IT)
    'abc.net.au',          # AU
    'foxtel.com.au',       # AU
    'tvhebdo.com',         # CA
    'programetv.ro',       # RO
    'programacion-tv.elpais.com',  # ES
    'movistarplus.es',     # ES
    'programme-tv.net',    # FR
    'tvcesoir.fr',         # FR
    'meo.pt',              # PT
    'guidatv.sky.it',      # IT
    'cosmotetv.gr',        # GR
    'cyta.com.cy',         # CY
    'allente.se',          # SE
    'epg.112114.xyz',      # IN (AIO mirror)
    'gigatv.3bbtv.co.th',  # TH
    'tvinsider.com',       # US
    'dstv.com',            # ZA/Africa
]

# sites that ship REGION-SUFFIXED channels files instead of <site>.channels.xml.
# Value: list of region suffixes to merge ([] = all regions).
REGION_FILES = {
    'abc.net.au': [],            # merge all abc.net.au_* files (AU regional)
    'allente.se': ['_se'],
    'dstv.com': ['_za'],         # South Africa only (matches our ZA gap)
}


def github_dir_listing(site):
    """Return file names in the site's dir via the GitHub contents API."""
    url = f'https://api.github.com/repos/iptv-org/epg/contents/sites/{site}'
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        return [x['name'] for x in json.loads(r.read().decode('utf-8'))]


def http_get(url, timeout=60, retries=2):
    last = None
    for _ in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode('utf-8', errors='ignore')
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5)
    raise last


def main():
    streams_json = sys.argv[1]
    outdir = sys.argv[2]
    os.makedirs(outdir, exist_ok=True)

    streams = json.load(open(streams_json))
    idx = SourceIndex()
    names = set()
    for s in streams:
        n = s.get('name', '')
        if n:
            idx.add(n, 'x')
            names.add(norm(n))

    for site in SITES:
        d = site
        # determine which channels files to merge (plain vs region-suffixed)
        plain_url = BASE.format(dir=d, site=site)
        files = [('plain', plain_url)]
        if site in REGION_FILES:
            try:
                listing = github_dir_listing(d)
                suffixes = REGION_FILES[site]
                files = [('region', f'https://raw.githubusercontent.com/'
                                    f'iptv-org/epg/master/sites/{d}/{f}')
                         for f in listing
                         if f.startswith(site + '_') and f.endswith('.channels.xml')
                         and (not suffixes or any(s in f for s in suffixes))]
                files.sort(key=lambda x: x[1])
            except Exception as e:  # noqa: BLE001
                print(f'[filter] {site}: dir listing FAILED ({e}); trying plain',
                      file=sys.stderr)
                files = [('plain', plain_url)]
        keep = []
        total = 0
        for kind, url in files:
            try:
                txt = http_get(url)
            except Exception as e:  # noqa: BLE001
                print(f'[filter] {site}: download FAILED: {e}', file=sys.stderr)
                continue
            for m in re.finditer(r'<channel\s+([^>]*)>([^<]*)</channel>', txt):
                total += 1
                dn = m.group(2).strip()
                nname = norm(dn)
                if not nname:
                    continue
                if nname in names or idx.fuzzy(dn, threshold=0.80, limit=1):
                    keep.append(m.group(0))
        out = os.path.join(outdir, f'{site}.channels.xml')
        with open(out, 'w', encoding='utf-8') as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n<channels>\n')
            for k in keep:
                f.write(f'  {k}\n')
            f.write('</channels>\n')
        print(f'[filter] {site}: kept {len(keep)}/{total} channels ({len(files)} files)')


if __name__ == '__main__':
    main()
