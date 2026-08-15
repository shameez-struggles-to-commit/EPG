#!/usr/bin/env python3
"""One-off generator: build the US network-affiliate callsign table from
Wikipedia's per-network affiliate lists.

Output: pipeline/us_affiliates.json
  {
    "affiliates": { "abc|al|birmingham": ["wbma", ...], ... },
    "state_single": { "abc|me": ["wmtw"], ... },   # states with exactly 1 affiliate
  }

Keys are lowercase "network|state|city". Networks: abc, cbs, nbc, fox, cw.
Run manually when the provider lineup changes; the JSON is committed and
consumed by build_mapping.py (--us-affiliates).

Used to resolve provider locals named "ABC: AL Birmingham ABC 33" (network +
state + city + brand, NO call sign) to a call sign so the epgshare01
US_LOCALS1 call-sign index can serve them.
"""

import json
import re
import sys
import urllib.request

UA = {'User-Agent': 'Mozilla/5.0 (EPG research; no contact)'}

NETWORKS = {
    'abc': 'https://en.wikipedia.org/wiki/List_of_ABC_television_affiliates_(table)',
    'cbs': 'https://en.wikipedia.org/wiki/List_of_CBS_television_affiliates_(table)',
    'nbc': 'https://en.wikipedia.org/wiki/List_of_NBC_television_affiliates_(table)',
    'fox': 'https://en.wikipedia.org/wiki/List_of_Fox_television_affiliates_(table)',
    'cw': 'https://en.wikipedia.org/wiki/List_of_The_CW_affiliates_(table)',
}

STATES = {
    'alabama': 'AL', 'alaska': 'AK', 'arizona': 'AZ', 'arkansas': 'AR',
    'california': 'CA', 'colorado': 'CO', 'connecticut': 'CT', 'delaware': 'DE',
    'florida': 'FL', 'georgia': 'GA', 'hawaii': 'HI', 'idaho': 'ID',
    'illinois': 'IL', 'indiana': 'IN', 'iowa': 'IA', 'kansas': 'KS',
    'kentucky': 'KY', 'louisiana': 'LA', 'maine': 'ME', 'maryland': 'MD',
    'massachusetts': 'MA', 'michigan': 'MI', 'minnesota': 'MN',
    'mississippi': 'MS', 'missouri': 'MO', 'montana': 'MT', 'nebraska': 'NE',
    'nevada': 'NV', 'new hampshire': 'NH', 'new jersey': 'NJ', 'new mexico': 'NM',
    'new york': 'NY', 'north carolina': 'NC', 'north dakota': 'ND', 'ohio': 'OH',
    'oklahoma': 'OK', 'oregon': 'OR', 'pennsylvania': 'PA', 'rhode island': 'RI',
    'south carolina': 'SC', 'south dakota': 'SD', 'tennessee': 'TN', 'texas': 'TX',
    'utah': 'UT', 'vermont': 'VT', 'virginia': 'VA', 'washington': 'WA',
    'west virginia': 'WV', 'wisconsin': 'WI', 'wyoming': 'WY',
    'district of columbia': 'DC', 'washington, d.c.': 'DC', 'puerto rico': 'PR',
}

CALLSIGN_RE = re.compile(r'\b([KW][A-Z]{2,3})\b')
# Subchannel rows ("KNOE-DT2" station, or channel cell "68.2") carry ANOTHER
# network's affiliation on a host station — resolving them to the host call
# sign would serve the wrong network's EPG. Skip them.
SUBCHANNEL_RE = re.compile(r'-(?:DT|LD|TV)\s*\d*[2-9]\d*', re.I)
CHANNEL_SUB_RE = re.compile(r'\b\d+\.\d*[2-9]\d*\b')


def clean(txt):
    """Strip HTML tags, refs, and collapse whitespace."""
    t = re.sub(r'<[^>]+>', '', txt)
    t = re.sub(r'&#\d+;|&#x[0-9a-fA-F]+;', ' ', t)
    t = t.replace('&amp;', '&').replace('&nbsp;', ' ')
    return re.sub(r'\s+', ' ', t).strip()


def parse_table(html):
    """Parse the main wikitable with rowspan carry-forward.

    Returns list of rows; each row = [market, state, station, ...] (7 cols).
    """
    tables = re.findall(r'<table class="wikitable.*?</table>', html, re.S)
    if not tables:
        return []
    table = tables[0]
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table, re.S)
    pending = []   # {col, left, value}: rowspan cells still occupying rows
    out = []
    for row in rows:
        cells = re.findall(r'<t[dh]\b([^>]*)>(.*?)</t[dh]>', row, re.S)
        if not cells:
            continue
        active = sorted(pending, key=lambda p: p['col'])
        ai = 0
        merged = []
        for attrs, content in cells:
            # inject rowspan carry values whose column position is reached
            while ai < len(active) and active[ai]['col'] == len(merged):
                merged.append(active[ai]['value'])
                active[ai]['left'] -= 1
                ai += 1
            text = clean(content)
            merged.append(text)
            rs = re.search(r'rowspan="(\d+)"', attrs)
            if rs and int(rs.group(1)) > 1:
                pending.append({'col': len(merged) - 1,
                                'left': int(rs.group(1)) - 1, 'value': text})
        while ai < len(active) and active[ai]['col'] == len(merged):
            merged.append(active[ai]['value'])
            active[ai]['left'] -= 1
            ai += 1
        pending = [p for p in pending if p['left'] > 0]
        out.append(merged)
    return out


def extract_callsign(station_text):
    m = CALLSIGN_RE.search(station_text or '')
    if not m:
        return None
    return m.group(1).lower()


def main():
    affiliates = {}
    state_single = {}
    for net, url in NETWORKS.items():
        try:
            req = urllib.request.Request(url, headers=UA)
            html = urllib.request.urlopen(req, timeout=60).read().decode(
                'utf-8', errors='ignore')
        except Exception as e:  # noqa: BLE001
            print(f'[{net}] FAILED: {e}', file=sys.stderr)
            continue
        rows = parse_table(html)
        net_rows = 0
        state_count = {}
        for r in rows:
            if len(r) < 4:
                continue
            market, state, station, channel = r[0], r[1], r[2], r[3]
            if SUBCHANNEL_RE.search(station) or CHANNEL_SUB_RE.search(channel):
                continue  # subchannel-hosted affiliation -> wrong-network risk
            cs = extract_callsign(station)
            if not cs or not market or not state:
                continue
            st = STATES.get(state.lower().split(',')[0].strip().lower())
            if not st:
                continue
            key = f'{net}|{st}|{market.lower().strip()}'
            affiliates.setdefault(key, []).append(cs)
            state_count.setdefault(st, set()).add(cs)
            net_rows += 1
        for st, css in state_count.items():
            if len(css) == 1:
                state_single[f'{net}|{st}'] = sorted(css)
        print(f'[{net}] {net_rows} rows parsed, {len(state_count)} states')

    out = {'affiliates': affiliates, 'state_single': state_single}
    dest = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'us_affiliates.json')
    json.dump(out, open(dest, 'w'), indent=1, sort_keys=True)
    print(f'wrote {dest}: {len(affiliates)} affiliate keys, '
          f'{len(state_single)} single-affiliate states')


if __name__ == '__main__':
    import os
    main()
