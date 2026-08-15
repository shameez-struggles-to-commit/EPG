#!/usr/bin/env python3
"""Fetch EPG from Sky's hawk API (awk.epgsky.com) — UK(GB)/DE/IT, 1,500+
channels, no auth. Validated 2026-08-15 global source audit.

API (mirrors iptv-org/epg sites/sky.com/sky.com.config.js):
  GET https://awk.epgsky.com/hawk/linear/schedule/{YYYYMMDD}/{sid,sid,...}
      (up to 20 sids per request, comma-joined)
      header: X-SkyOTT-Territory: GB | DE | IT
  → {"schedule": [ {"sid": "2075", "events": [
        {"t": title, "st": epoch-sec, "d": duration-sec, "sy": synopsis,
         "eid": event-id} ]} ]}

Channel site_ids come from iptv-org/epg sites/sky.com/sky.com.channels.xml
(master branch!), format site_id="GB#2075" (territory#sid).

Matching: provider streams (UK/IE→GB, DE/AT/CH→DE, IT→IT categories) are
normalized and matched EXACTLY against normalized sky display-names.

Output: XMLTV .xml with channel id = sky site_id (e.g. GB#2075).
Usage: fetch_skyhawk.py <streams.json> <out.xml> [--days N]
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.request
from xml.sax.saxutils import escape, quoteattr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from matcher import norm
from build_mapping import NAME_ALIASES

UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'}
CHANNELS_URL = ('https://raw.githubusercontent.com/iptv-org/epg/master/'
                'sites/sky.com/sky.com.channels.xml')
SCHEDULE_URL = 'https://awk.epgsky.com/hawk/linear/schedule/{date}/{sids}'

TERRITORIES = ('GB', 'DE', 'IT')
MAX_SIDS_PER_REQUEST = 20

# provider category prefix → Sky territory
COUNTRY_TERRITORY = {
    'UK': 'GB', 'GB': 'GB', 'IRE': 'GB', 'IE': 'GB',
    'DE': 'DE', 'AT': 'DE', 'CH': 'DE',
    'IT': 'IT',
    # South Asian diaspora feeds are carried on Sky UK (Sony/Zee/Colors/B4U/
    # Utsav + ARY/Geo/Hum/PTC/AajTak) — map them to the GB lineup.
    'IN': 'GB', 'PK': 'GB', 'BD': 'GB',
}

NONLIN_RE = re.compile(
    r'24/7|vip|radio|for adults|event|flo|epl|efl|nfl|nba|mlb|nhl|nrl|ufc|ppv|'
    r'fifa|espn\+|adult', re.I)


def http_get(url, headers=None, timeout=30, retries=2):
    h = dict(UA)
    if headers:
        h.update(headers)
    last = None
    for _ in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5)
    raise last


def load_sky_channels():
    """Parse sky.com.channels.xml → [(site_id, territory, sid, display_name)]."""
    txt = http_get(CHANNELS_URL, timeout=60).decode('utf-8', errors='ignore')
    out = []
    for m in re.finditer(r'<channel\s+([^>]*)>([^<]*)</channel>', txt):
        attrs = m.group(1)
        site_id = re.search(r'site_id="([^"]*)"', attrs)
        if not site_id:
            continue
        parts = site_id.group(1).split('#')
        if len(parts) != 2 or parts[0] not in TERRITORIES:
            continue
        # skip UHD channels (different API header set; iptv-org does the same)
        if re.search(r'uhd|ultra\s*hd|4k', m.group(2), re.I):
            continue
        out.append((site_id.group(1), parts[0], parts[1], m.group(2).strip()))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('streams')
    ap.add_argument('out')
    ap.add_argument('--days', type=int, default=2)
    args = ap.parse_args()

    streams = json.load(open(args.streams))

    try:
        sky_channels = load_sky_channels()
    except Exception as e:  # noqa: BLE001
        print(f'[skyhawk] channels file FAILED: {e}', file=sys.stderr)
        sky_channels = []
    print(f'[skyhawk] sky lineup: {len(sky_channels)} channels')

    # norm name → site_id per territory
    by_t = {}
    for site_id, terr, sid, dn in sky_channels:
        nname = norm(dn)
        if nname:
            by_t.setdefault(terr, {})[nname] = site_id

    # match provider streams (dedup by site_id)
    targets = {}   # site_id -> display name
    for s in streams:
        cat = (s.get('cat_name') or '').strip()
        if NONLIN_RE.search(cat):
            continue
        m = re.match(r'^([A-Za-z]{2,3})\s*\|', cat)
        terr = COUNTRY_TERRITORY.get(m.group(1).upper()) if m else None
        if not terr:
            continue
        nname = norm(s.get('name', ''))
        sid_full = by_t.get(terr, {}).get(nname)
        if not sid_full:
            # diaspora alias fallback: "Sony TV Asia" -> "Sony TV", etc.
            alias = NAME_ALIASES.get(s.get('name', '')) or NAME_ALIASES.get(nname)
            if alias:
                sid_full = by_t.get(terr, {}).get(norm(alias))
        if sid_full and sid_full not in targets:
            targets[sid_full] = s.get('name', '')

    per_terr = {}
    for site_id in targets:
        t = site_id.split('#')[0]
        per_terr[t] = per_terr.get(t, 0) + 1
    print(f'[skyhawk] matched {len(targets)} provider channels {per_terr}')

    if not targets:
        with open(args.out, 'w', encoding='utf-8') as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv></tv>\n')
        return

    # batched schedule fetch: 20 sids per request per territory per day
    progs = {}   # site_id -> [(start, stop, title, desc)]
    by_terr_sids = {}
    for site_id in targets:
        terr, sid = site_id.split('#')
        by_terr_sids.setdefault(terr, []).append(sid)

    base = dt.datetime.now(dt.timezone.utc).date()
    for terr, sids in by_terr_sids.items():
        for d in range(args.days):
            date_s = (base + dt.timedelta(days=d)).strftime('%Y%m%d')
            for i in range(0, len(sids), MAX_SIDS_PER_REQUEST):
                batch = sids[i:i + MAX_SIDS_PER_REQUEST]
                url = SCHEDULE_URL.format(date=date_s, sids=','.join(batch))
                try:
                    data = json.loads(http_get(
                        url, headers={'X-SkyOTT-Territory': terr}, timeout=30))
                except Exception as e:  # noqa: BLE001
                    print(f'[skyhawk] {terr} {date_s} batch FAILED: {e}',
                          file=sys.stderr)
                    continue
                sid_to_site = {sid: f'{terr}#{sid}' for sid in batch}
                for sched in data.get('schedule', []):
                    site_id = sid_to_site.get(str(sched.get('sid')))
                    if not site_id:
                        continue
                    for ev in sched.get('events', []):
                        t = ev.get('t')
                        st = ev.get('st')
                        dur = ev.get('d')
                        if not t or st is None or not dur:
                            continue
                        start = dt.datetime.fromtimestamp(int(st), tz=dt.timezone.utc)
                        stop = start + dt.timedelta(seconds=int(dur))
                        progs.setdefault(site_id, []).append((
                            start.strftime('%Y%m%d%H%M%S') + ' +0000',
                            stop.strftime('%Y%m%d%H%M%S') + ' +0000',
                            t, ev.get('sy') or ''))
                time.sleep(0.4)  # be polite

    with open(args.out, 'w', encoding='utf-8') as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write('<tv generator-info-name="hermes-skyhawk">\n')
        for site_id, dn in targets.items():
            if site_id in progs:
                f.write(f'  <channel id={quoteattr(site_id)}>\n')
                f.write(f'    <display-name>{escape(dn)}</display-name>\n')
                f.write('  </channel>\n')
        for site_id, plist in progs.items():
            for (st, sp, ti, de) in plist:
                f.write(f'  <programme start={quoteattr(st)} stop={quoteattr(sp)} '
                        f'channel={quoteattr(site_id)}>\n')
                f.write(f'    <title lang="en">{escape(ti)}</title>\n')
                if de:
                    f.write(f'    <desc lang="en">{escape(de[:500])}</desc>\n')
                f.write('  </programme>\n')
        f.write('</tv>\n')

    n_p = sum(len(v) for v in progs.values())
    print(f'[skyhawk] {len(progs)} channels, {n_p} programmes -> {args.out}')


if __name__ == '__main__':
    main()
