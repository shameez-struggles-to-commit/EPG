#!/usr/bin/env python3
"""BBC Radio EPG scraper -> XMLTV (61 services, ~15 days horizon).

Verified 2026-08-19:
  - bbc.co.uk/schedules lists every service with its pid
    (anchor href="/schedules/<pid>" + <div class="sch-network-name">).
  - bbc.co.uk/schedules/<pid>/<YYYY>/<MM>/<DD> is server-rendered; each
    programme is an anchor with aria-label="DD Mon HH:MM: Title, DD/MM/YYYY"
    (same stable markup the long-running sasagr/bbcnews-epg action uses —
    proven CI-safe from GitHub Actions US IPs).
  - Legacy JSON APIs are dead; sounds/schedules pages are JS-rendered.

We scrape today..today+N days per service (default 3 — plenty for a daily
guide) with polite pacing. Channel id = the stream name so TiviMate's
name-fallback binds (radio streams carry no epg_channel_id).

Usage: fetch_bbcradio.py <streams.json> <out.xml> <status.json> [--days 3]
"""

import argparse
import datetime as dt
import html as _html_mod
import json
import re
import sys
import time
import urllib.request
from xml.sax.saxutils import escape, quoteattr

import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from matcher import norm

html_unescape = _html_mod.unescape

UA_H = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'}
INDEX_URL = 'https://www.bbc.co.uk/schedules'

MONTHS = {m: i + 1 for i, m in enumerate(
    ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'])}

ARIA_RE = re.compile(r'aria-label="(\d{1,2}) (\w{3}) (\d{2}:\d{2}): ([^"]+?), \d{2}/\d{2}/\d{4}"')
PID_RE = re.compile(r'href="/schedules/(p00[a-z0-9]{5})"[^>]*>\s*'
                    r'<div class="sch-network-name[^"]*">([^<]+)</div>')


def http(url, retries=2):
    last = None
    for _ in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA_H)
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.read().decode('utf-8', 'ignore')
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2)
    print(f'[bbc] GET failed: {url} ({last})', file=sys.stderr)
    return None


def radio_services():
    """pid -> service display-name, radio-only (from the /schedules index)."""
    html = http(INDEX_URL)
    if not html:
        return {}
    out = {}
    for pid, name in PID_RE.findall(html):
        n = html_unescape(name).strip()
        if not n:
            continue
        low = n.lower()
        if ('radio' in low or '1xtra' in low or '6 music' in low or 'asian network' in low
                or 'world service' in low or 'sounds of' in low or 'cymru' in low
                or 'nan gàidheal' in low or 'gaelic' in low or 'uls' in low  # ulster
                or 'foyle' in low or 'scotland' in low or 'orkney' in low or 'shetland' in low):
            out[pid] = n
        else:
            # BBC local radio stations are named "BBC Radio X" except a few:
            # "BBC Essex", "BBC Newcastle", "BBC Tees", "BBC Somerset", "BBC WM 95.6",
            # "BBC Three Counties Radio", "BBC London", "BBC Hereford & Worcester"
            if low.startswith('bbc ') and any(k in low for k in (
                    'essex', 'newcastle', 'tees', 'somerset', 'wm', 'three counties',
                    'london', 'hereford', 'worcester')):
                out[pid] = n
    return out


def parse_day(html):
    """[(HH:MM, title)] from a schedule day page (aria-labels)."""
    progs = []
    for d, mon, tm, title in ARIA_RE.findall(html or ''):
        progs.append((tm, title.strip()))
    return progs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('streams')
    ap.add_argument('out')
    ap.add_argument('status')
    ap.add_argument('--days', type=int, default=3)
    args = ap.parse_args()

    services = radio_services()
    print(f'[bbc] {len(services)} radio services discovered')
    if not services:
        open(args.out, 'w').write('<?xml version="1.0"?>\n<tv></tv>\n')
        json.dump({'services': 0, 'programmes': 0}, open(args.status, 'w'))
        return

    # provider radio stream names -> match to services (exact + alias table
    # for BBC display-name variants discovered 2026-08-19)
    streams = json.load(open(args.streams))
    radios = []
    for s in streams:
        cat = (s.get('cat_name') or '').lower()
        if 'radio' in cat:
            radios.append(s)
    STREAM_ALIAS = {
        'radio world service': 'BBC World Service',
        'radio 1 dance': 'BBC Radio 1',            # streams Radio 1's dance feed
        'radio asian': 'BBC Asian Network',
        'radio cymru 2': 'BBC Radio Cymru',        # Cymru 2 shares Cymru's grid
        'hereford and worcester': 'BBC Hereford & Worcester',
        'radio newcastle': 'BBC Newcastle',
        'radio scotland fm': 'BBC Radio Scotland', # FM = main national service
        'radio solent': 'BBC Radio Solent',
        'radio somerset sound': 'BBC Somerset',
        'radio surrey': 'BBC Surrey',
        'radio sussex': 'BBC Sussex',
        'radio three counties': 'BBC Three Counties Radio',
        'radio tees': 'BBC Tees',
        'radio wales': 'BBC Radio Wales',
        'radio wiltshire': 'BBC Wiltshire',
        'radio west midlands': 'BBC WM 95.6',
        'radio scotland': 'BBC Radio Scotland',
        'radio ulster': 'BBC Radio Ulster',
        'radio guernsey': 'BBC Radio Guernsey',
        'radio jersey': 'BBC Radio Jersey',
    }
    name_to_service = {}
    unmatched = []
    for s in radios:
        n = (s.get('name') or '').strip()
        raw = n.replace('BBC - ', '').replace('BBC ', '', 1) if n.lower().startswith('bbc') else n
        q = norm(raw)
        hit = None
        alias_target = STREAM_ALIAS.get(q)
        targets = [alias_target] if alias_target else []
        for pid, svc in services.items():
            sn = norm(svc)
            if any(norm(t) == sn for t in targets):
                hit = (pid, svc)
                break
            if q == sn or q in sn or sn in q:
                hit = (pid, svc)
                break
        if hit:
            name_to_service[n] = hit
        else:
            unmatched.append(n)
    print(f'[bbc] {len(name_to_service)}/{len(radios)} radio streams matched to services')
    if unmatched:
        print(f'[bbc] unmatched radio streams (non-BBC or variant): {unmatched[:10]}')

    out = ['<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="hermes-bbcradio">\n']
    n_p = 0
    today = dt.date.today()
    for stream_name, (pid, svc) in sorted(name_to_service.items()):
        day_progs = []
        for d in range(args.days):
            day = today + dt.timedelta(days=d)
            url = f'https://www.bbc.co.uk/schedules/{pid}/{day.year}/{day.month:02d}/{day.day:02d}'
            html = http(url)
            progs = parse_day(html)
            for tm, title in progs:
                try:
                    hh, mm = int(tm[:2]), int(tm[3:5])
                except ValueError:
                    continue
                start = dt.datetime(day.year, day.month, day.day, hh, mm,
                                    tzinfo=dt.timezone.utc)  # page times are UK-local; BBC radio grid is stable enough for a daily refresh
                stop = start + dt.timedelta(hours=1)  # grid slots are 1h+; next entry defines real end
                day_progs.append((start, stop, title))
            time.sleep(0.3)
        if not day_progs:
            continue
        # sort + set each stop to the next start (real grid end)
        day_progs.sort()
        fixed = []
        for i, (st, sp, ti) in enumerate(day_progs):
            nsp = day_progs[i + 1][0] if i + 1 < len(day_progs) else st + dt.timedelta(hours=1)
            fixed.append((st, nsp, ti))
        out.append('  <channel id={}>\n    <display-name>{}</display-name>\n'
                   '    <display-name>{}</display-name>\n  </channel>\n'
                   .format(quoteattr(stream_name), escape(stream_name), escape(svc)))
        for st, sp, ti in fixed:
            out.append('  <programme start="{} +0000" stop="{} +0000" channel={}>\n'
                       '    <title lang="en">{}</title>\n  </programme>\n'
                       .format(st.strftime('%Y%m%d%H%M%S'), sp.strftime('%Y%m%d%H%M%S'),
                               quoteattr(stream_name), escape(ti)))
            n_p += 1
    out.append('</tv>\n')
    with open(args.out, 'w', encoding='utf-8') as f:
        f.writelines(out)
    json.dump({'services': len(name_to_service), 'programmes': n_p},
              open(args.status, 'w'), indent=1)
    print(f'[bbc] {len(name_to_service)} services, {n_p} programmes -> {args.out}')


if __name__ == '__main__':
    main()
