#!/usr/bin/env python3
"""Fetch EPG from epg.one (RU/CIS mega-feed) and emit an XMLTV file.

Validated 2026-08-15 global source audit: epg.one/epg.xml.gz has 3,256
channels / ~695k programmes; the 215 UA-tagged channels make it the best
Ukrainian EPG source found. TLS cert is expired → insecure download.

For our pipeline we keep ONLY channels whose id/tag is Ukrainian ('ua'), plus
optionally any channel name matching our uncovered UKR list (transliteration:
Cyrillic provider names like '5 канал' vs Latin source ids like '5-kanal').

Output: XMLTV .xml filtered to UA channels.
Usage: fetch_epgone.py <out.xml> [--country ua]
"""

import argparse
import gzip
import html
import os
import re
import sys
import time
import urllib.request
import ssl
from xml.sax.saxutils import escape, quoteattr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from matcher import cyr_to_lat

UA_H = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'}
URL = 'https://epg.one/epg.xml.gz'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('out')
    ap.add_argument('--country', default='ua')
    args = ap.parse_args()

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(URL, headers=UA_H)
    raw = None
    last_err = None
    # 2026-08-16: epg.one is unreachable from GitHub runners (connect timeout on
    # EVERY attempt; each hung ~150s on the old 300s timeout and burned ~11 min
    # per run). Hard-cap: 30s connect timeout, 2 retries with short backoff.
    # Worst case ~90s of futility instead of ~11 min. Site is also intermittently
    # unreachable from the Mac — treat as best-effort bonus source.
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
                raw = r.read()
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f'[epgone] attempt {attempt + 1} FAILED: {e} (retrying)', file=sys.stderr)
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
    if raw is None:
        raise last_err
    txt = gzip.decompress(raw).decode('utf-8', errors='ignore')
    print(f'[epgone] downloaded {len(raw)} bytes gz, {len(txt)} chars')

    # UA channels are marked by a ' UA' display-name suffix (e.g.
    # '4ever Cinema HD UA'), not by channel-id suffix. Any display-name may
    # carry the suffix (original fetcher matched on any of them).
    # 2026-08-16 fix: strip the ' UA' suffix and emit a Cyrillic→Latin
    # transliterated display-name FIRST (the matcher indexes the first
    # display-name), keeping the original as a second entry. This is what lets
    # provider names like "5 Kanal (5 канал)" match (via the epg_q() query
    # transform in build_mapping).
    keep_ids = set()
    out = ['<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="hermes-epgone">\n']
    for m in re.finditer(r'<channel\s+id="([^"]*)"[^>]*>(.*?)</channel>', txt, re.S):
        cid = m.group(1)
        body = m.group(2)
        dns = [html.unescape(x) for x in re.findall(r'<display-name[^>]*>([^<]*)</display-name>', body)]
        if not dns or not any(re.search(r'\bUA\s*$', d) for d in dns):
            continue
        keep_ids.add(cid)
        base = re.sub(r'\s+UA\s*$', '', dns[0]).strip()
        lat = cyr_to_lat(base)
        names = [f'    <display-name>{escape(lat)}</display-name>']
        if lat.lower() != base.lower():
            names.append(f'    <display-name>{escape(base)}</display-name>')
        out.append(f'  <channel id={quoteattr(cid)}>\n' + '\n'.join(names) + '\n  </channel>\n')

    n_ch = len(keep_ids)
    n_p = 0
    for m in re.finditer(r'<programme\s+(.*?)>(.*?)</programme>', txt, re.S):
        attrs = m.group(1)
        chm = re.search(r'channel="([^"]*)"', attrs)
        if not chm or chm.group(1) not in keep_ids:
            continue
        out.append(f'  <programme {attrs}>{m.group(2)}</programme>\n')
        n_p += 1

    out.append('</tv>\n')
    with open(args.out, 'w', encoding='utf-8') as f:
        f.writelines(out)
    print(f'[epgone] {n_ch} {args.country} channels, {n_p} programmes -> {args.out}')

    # translit mapping hint for matching: print a few id samples
    sample = list(keep_ids)[:8]
    print(f'[epgone] sample ids: {sample}')


if __name__ == '__main__':
    main()
