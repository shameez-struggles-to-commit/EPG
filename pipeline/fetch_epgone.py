#!/usr/bin/env python3
"""Fetch the epg.one RU/CIS mega-feed (via CI-safe mirrors) -> UA/MD XMLTV.

2026-08-19: epg.one is ALIVE again but (a) still TLS-broken + intermittent
from GitHub runners, and (b) the feed SHAPE CHANGED — channel ids are now
bare integers, so the old ' UA' display-name/id filter matches almost
nothing. Mirrors verified 2026-08-19 (all carry the same dataset):
  - raw.githubusercontent.com/Lorax121/epg_v2/main/data/EPG_LITE.xml.gz  (20MB, GH-raw = CI-safe, PRIMARY)
  - https://cdn.epg.one/epg.xml.gz   (49MB, insecure TLS, fallback)
  - https://epg.it999.ru/edem.xml.gz (49MB, fallback)

Keep-filter is now NAME-BASED and shape-independent: keep a channel when
(a) any display-name ends with ' UA' (old convention, when present), OR
(b) its name (translit-normalized) matches a provider stream name — which
also lets Moldovan channels through (provider 'MD' category) instead of
only Ukraine.

Output: XMLTV .xml with transliterated Latin display-name first (the
matcher indexes the first display-name), original second.
Usage: fetch_epgone.py <out.xml> <streams.json>
"""

import argparse
import gzip
import html
import json
import os
import re
import sys
import time
import urllib.request
import ssl
from xml.sax.saxutils import escape, quoteattr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from matcher import cyr_to_lat, norm

UA_H = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'}

MIRRORS = [
    'https://raw.githubusercontent.com/Lorax121/epg_v2/main/data/EPG_LITE.xml.gz',
    'https://cdn.epg.one/epg.xml.gz',
    'https://epg.it999.ru/edem.xml.gz',
]


def download():
    last_err = None
    for url in MIRRORS:
        for attempt in range(2):
            try:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                req = urllib.request.Request(url, headers=UA_H)
                with urllib.request.urlopen(req, timeout=120, context=ctx) as r:
                    raw = r.read()
                print(f'[epgone] {url}: {len(raw)} bytes')
                return raw
            except Exception as e:  # noqa: BLE001
                last_err = e
                print(f'[epgone] {url} attempt {attempt + 1} FAILED: {e}', file=sys.stderr)
                time.sleep(3)
    raise last_err


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('out')
    ap.add_argument('streams', help='provider streams.json (name-based keep-filter)')
    args = ap.parse_args()

    raw = download()
    txt = gzip.decompress(raw).decode('utf-8', errors='ignore')
    print(f'[epgone] decompressed {len(txt)} chars')

    # Provider stream names (translit + dedupe, same transform the matcher's
    # epg_q applies on the query side) — the keep-set for name-based filtering.
    streams = json.load(open(args.streams))
    want = set()
    for s in streams:
        cat = (s.get('cat_name') or '')
        cc_hint = cat.split('|')[0].strip().upper()
        if cc_hint in ('UKR', 'MD', 'RO'):
            toks = cyr_to_lat(norm(s.get('name', ''))).split()
            q = ' '.join(dict.fromkeys(toks))
            if q:
                want.add(q)
    print(f'[epgone] keep-filter: {len(want)} UKR/MD/RO provider names')

    def name_key(display):
        toks = cyr_to_lat(norm(display)).split()
        return ' '.join(dict.fromkeys(toks))

    keep_ids = set()
    out = ['<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="hermes-epgone">\n']
    for m in re.finditer(r'<channel\s+id="([^"]*)"[^>]*>(.*?)</channel>', txt, re.S):
        cid = m.group(1)
        body = m.group(2)
        dns = [html.unescape(x) for x in re.findall(r'<display-name[^>]*>([^<]*)</display-name>', body)]
        if not dns:
            continue
        keep = False
        base = None
        for d in dns:
            if re.search(r'\bUA\s*$', d):
                keep = True
                base = re.sub(r'\s+UA\s*$', '', d).strip()
                break
        if not keep:
            for d in dns:
                if name_key(d) in want:
                    keep = True
                    base = d.strip()
                    break
        if not keep or not base:
            continue
        keep_ids.add(cid)
        lat = cyr_to_lat(base)
        names = ['    <display-name>{}</display-name>'.format(escape(lat))]
        if lat.lower() != base.lower():
            names.append('    <display-name>{}</display-name>'.format(escape(base)))
        out.append('  <channel id={}>\n{}\n  </channel>\n'.format(quoteattr(cid), '\n'.join(names)))

    n_ch = len(keep_ids)
    n_p = 0
    for m in re.finditer(r'<programme\s+(.*?)>(.*?)</programme>', txt, re.S):
        attrs = m.group(1)
        chm = re.search(r'channel="([^"]*)"', attrs)
        if not chm or chm.group(1) not in keep_ids:
            continue
        out.append('  <programme {}>{}</programme>\n'.format(attrs, m.group(2)))
        n_p += 1

    out.append('</tv>\n')
    with open(args.out, 'w', encoding='utf-8') as f:
        f.writelines(out)
    print(f'[epgone] {n_ch} channels, {n_p} programmes -> {args.out}')


if __name__ == '__main__':
    main()
