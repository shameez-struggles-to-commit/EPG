#!/usr/bin/env python3
"""Fetch Nordic EPG from the Allente public JSON API (no auth) -> XMLTV.

Verified 2026-08-19: allente.{dk,no,fi}/api/epg/refetch-epg-data?Start=YYYY-MM-DD
returns {channels:[{id,name,image,url,programs:[{title,eventStart,eventEnd,
shortDescription,...}]}]} — ISO-8601 UTC offsets, no auth, US-IP friendly.

Each country endpoint returns that country's full linear lineup for ONE day;
we fetch today + tomorrow (the guide's horizon). Channels are keyed by their
Allente id but names are what our matcher uses, so ids just need uniqueness.

Usage: fetch_allente.py <out.xml> [--days 2]
"""

import argparse
import datetime as dt
import json
import re
import sys
import urllib.request
from xml.sax.saxutils import escape, quoteattr

UA_H = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'}
SITES = ['allente.dk', 'allente.no', 'allente.fi']


def fetch_json(url):
    req = urllib.request.Request(url, headers=UA_H)
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode('utf-8'))


def xmltv_ts(iso):
    """'2026-08-19T01:10:00+00:00' -> ('20260819011000 +0000', ...) or None."""
    if not iso:
        return ''
    try:
        d = dt.datetime.fromisoformat(iso.replace('Z', '+00:00'))
    except ValueError:
        return ''
    off = d.strftime('%z') or '+0000'
    return d.strftime('%Y%m%d%H%M%S') + ' ' + (off if len(off) == 5 else off[:3] + off[4:])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('out')
    ap.add_argument('--days', type=int, default=2)
    args = ap.parse_args()

    out = ['<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="hermes-allente">\n']
    n_ch = n_p = 0
    for site in SITES:
        cc = site.split('.')[1]  # dk / no / fi
        seen = set()
        for day in range(args.days):
            d = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=day)).strftime('%Y-%m-%d')
            try:
                j = fetch_json(f'https://www.{site}/api/epg/refetch-epg-data?Start={d}')
            except Exception as e:  # noqa: BLE001
                print(f'[allente] {site} {d} FAILED: {e}', file=sys.stderr)
                continue
            for c in j.get('channels', []):
                cid = f'{cc}:{c.get("id")}'
                name = (c.get('name') or '').strip()
                if not name or not c.get('programs'):
                    continue
                if cid not in seen:
                    seen.add(cid)
                    img = c.get('image') or ''
                    out.append(f'  <channel id={quoteattr(cid)}>\n'
                               f'    <display-name>{escape(name)}</display-name>\n'
                               + (f'    <icon src={quoteattr(img)}/>\n' if img else '')
                               + '  </channel>\n')
                    n_ch += 1
                for p in c['programs']:
                    st, sp = xmltv_ts(p.get('eventStart')), xmltv_ts(p.get('eventEnd'))
                    if not st or not sp:
                        continue
                    title = (p.get('title') or '').strip()
                    if not title:
                        continue
                    desc = (p.get('shortDescription') or '').strip()
                    out.append(f'  <programme start={quoteattr(st)} stop={quoteattr(sp)} '
                               f'channel={quoteattr(cid)}>\n'
                               f'    <title lang="en">{escape(title)}</title>\n'
                               + (f'    <desc lang="en">{escape(desc[:500])}</desc>\n' if desc else '')
                               + '  </programme>\n')
                    n_p += 1
        print(f'[allente] {site}: {len(seen)} channels total')
    out.append('</tv>\n')
    with open(args.out, 'w', encoding='utf-8') as f:
        f.writelines(out)
    print(f'[allente] {n_ch} channels, {n_p} programmes -> {args.out}')


if __name__ == '__main__':
    main()
