#!/usr/bin/env python3
"""Fetch EPG from DStv's free Umbraco JSON API (South Africa + Africa).

Validated 2026-08-15 global source audit:
  GET https://www.dstv.com/umbraco/api/TvGuide/GetProgrammes?d=YYYY-MM-DD&country=zaf
  → {"Total": N, "Channels": [ {"Name": "M-Net", "Programmes": [
       {"Title": ..., "StartDate": "2026-08-15T18:30:00+02:00" (SAST ISO),
        "EndDate": ..., "Synopsis": ...} ]} ]}

Note: channel names use DStv's own naming (SuperSport Premier League etc.);
matching happens downstream via build_mapping name matching.

Output: XMLTV .xml with channel id = 'dstv:' + slugified channel name.
Usage: fetch_dstv.py <out.xml> [--days N] [--country zaf]
"""

import argparse
import datetime as dt
import json
import re
import sys
import time
import urllib.request
from xml.sax.saxutils import escape, quoteattr

UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'}
API = 'https://www.dstv.com/umbraco/api/TvGuide/GetProgrammes?d={date}&country={country}'


def http_json(url, timeout=60, retries=2):
    last = None
    for _ in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode('utf-8', errors='ignore'))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2)
    raise last


def slug(name):
    s = re.sub(r'[^a-z0-9]+', '-', (name or '').lower()).strip('-')
    return 'dstv:' + s


def norm_iso(ts):
    """DStv sends SAST ISO times like 2026-08-15T18:30:00+02:00 (sometimes
    without offset). Normalize to UTC XMLTV stamp."""
    ts = (ts or '').strip()
    if not ts:
        return ''
    if ts.endswith('Z'):
        ts = ts[:-1] + '+00:00'
    try:
        d = dt.datetime.fromisoformat(ts)
    except ValueError:
        return ''
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone(dt.timedelta(hours=2)))  # SAST
    return d.astimezone(dt.timezone.utc).strftime('%Y%m%d%H%M%S') + ' +0000'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('out')
    ap.add_argument('--days', type=int, default=2)
    ap.add_argument('--country', default='zaf')
    args = ap.parse_args()

    base = dt.datetime.now(dt.timezone.utc).date()
    channels = {}   # cid -> name
    progs = {}      # cid -> [(start, stop, title, desc)]
    for d in range(args.days):
        date_s = (base + dt.timedelta(days=d)).strftime('%Y-%m-%d')
        try:
            data = http_json(API.format(date=date_s, country=args.country))
        except Exception as e:  # noqa: BLE001
            print(f'[dstv] {date_s} FAILED: {e}', file=sys.stderr)
            continue
        n_progs = 0
        for ch in data.get('Channels', []):
            name = ch.get('Name') or ''
            if not name:
                continue
            cid = slug(name)
            channels[cid] = name
            for p in ch.get('Programmes', []) or []:
                st = norm_iso(p.get('StartTime'))
                sp = norm_iso(p.get('EndTime'))
                ti = p.get('Title') or ''
                if not st or not sp or not ti:
                    continue
                progs.setdefault(cid, []).append(
                    (st, sp, ti, ''))
                n_progs += 1
        print(f'[dstv] {date_s}: {len(data.get("Channels", []))} channels, {n_progs} programmes')
        time.sleep(0.5)

    with open(args.out, 'w', encoding='utf-8') as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write('<tv generator-info-name="hermes-dstv">\n')
        for cid, name in channels.items():
            if cid in progs:
                f.write(f'  <channel id={quoteattr(cid)}>\n')
                f.write(f'    <display-name>{escape(name)}</display-name>\n')
                f.write('  </channel>\n')
        for cid, plist in progs.items():
            for (st, sp, ti, de) in plist:
                f.write(f'  <programme start={quoteattr(st)} stop={quoteattr(sp)} '
                        f'channel={quoteattr(cid)}>\n')
                f.write(f'    <title lang="en">{escape(ti)}</title>\n')
                if de:
                    f.write(f'    <desc lang="en">{escape(de)}</desc>\n')
                f.write('  </programme>\n')
        f.write('</tv>\n')

    n_p = sum(len(v) for v in progs.values())
    print(f'[dstv] {len(progs)} channels, {n_p} programmes -> {args.out}')


if __name__ == '__main__':
    main()
