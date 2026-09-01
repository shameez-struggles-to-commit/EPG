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
from source_registry import iptv_org_sources, load_source_registry

UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'}
IPTV_ORG_EPG_REF = os.environ.get(
    'IPTV_ORG_EPG_REF', '51fcb160fe9a521cb8d4081edf4ead94ac48f712'
)
BASE = ('https://raw.githubusercontent.com/iptv-org/epg/'
        + IPTV_ORG_EPG_REF + '/sites/{dir}/{site}.channels.xml')


# Filtered sites and region-file rules come from config/sources.json.
def filtered_sites():
    load_source_registry()  # validate once before any network work
    return iptv_org_sources(filtered=True)


def github_dir_listing(site):
    """Return file names in the site's dir via the GitHub contents API."""
    url = (f'https://api.github.com/repos/iptv-org/epg/contents/sites/{site}'
           f'?ref={IPTV_ORG_EPG_REF}')
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

    for source in filtered_sites():
        site = source['site']
        d = site
        # determine which channels files to merge (plain vs region-suffixed)
        plain_url = BASE.format(dir=d, site=site)
        files = [('plain', plain_url)]
        if 'region_suffixes' in source:
            try:
                listing = github_dir_listing(d)
                suffixes = source['region_suffixes']
                files = [('region', f'https://raw.githubusercontent.com/'
                                    f'iptv-org/epg/{IPTV_ORG_EPG_REF}/sites/{d}/{f}')
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
