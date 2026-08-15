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
import re
import sys
import urllib.request
import ssl
from xml.sax.saxutils import quoteattr

UA_H = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'}
URL = 'https://epg.one/epg.xml.gz'

CYR_TO_LAT = {
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'h', 'ґ': 'g', 'д': 'd', 'е': 'e',
    'є': 'ie', 'ж': 'zh', 'з': 'z', 'и': 'y', 'і': 'i', 'ї': 'i', 'й': 'i',
    'к': 'k', 'л': 'l', 'м': 'm', 'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r',
    'с': 's', 'т': 't', 'у': 'u', 'ф': 'f', 'х': 'kh', 'ц': 'ts', 'ч': 'ch',
    'ш': 'sh', 'щ': 'shch', 'ь': '', 'ю': 'iu', 'я': 'ia', 'є': 'ie',
    ' ': '-', '_': '-',
}


def translit(s):
    return ''.join(CYR_TO_LAT.get(c, c) for c in (s or '').lower())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('out')
    ap.add_argument('--country', default='ua')
    args = ap.parse_args()

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(URL, headers=UA_H)
    with urllib.request.urlopen(req, timeout=300, context=ctx) as r:
        raw = r.read()
    txt = gzip.decompress(raw).decode('utf-8', errors='ignore')
    print(f'[epgone] downloaded {len(raw)} bytes gz, {len(txt)} chars')

    # UA channels are marked by a ' UA' display-name suffix (e.g.
    # '4ever Cinema HD UA'), not by channel-id suffix.
    keep_ids = set()
    out = ['<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="hermes-epgone">\n']
    for m in re.finditer(r'<channel\s+id="([^"]*)"[^>]*>(.*?)</channel>', txt, re.S):
        cid = m.group(1)
        body = m.group(2)
        if not re.search(r'<display-name[^>]*>[^<]*\bUA\s*</display-name>', body):
            continue
        keep_ids.add(cid)
        out.append(f'  <channel id={quoteattr(cid)}>{body}</channel>\n')

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
