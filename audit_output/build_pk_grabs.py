#!/usr/bin/env python3
"""Round 7: build filtered iptv-org channel files for PK diaspora audits."""
import re, sys

REGS = '/Users/shameez/workspace/iptv-org-epg/sites'
OUT = '/Users/shameez/workspace/epg/audit_output'

# (site, [site_ids to include]) — curated PK diaspora candidates
CURATED = {
    'tv24.co.uk': ['92-news', 'dunya-news', 'geo-entertainment', 'geo-news',
                   'geo-tv', 'hum-europe', 'hum-europe-1', 'hum-masala-europe',
                   'islam-channel-urdu', 'samaa', 'samaa-tv', 'tv-one'],
    'streamingtvguides.com': ['AAJENT', 'ARYDI', 'ARYNEWS', 'ARYZAUQ', 'DUNYA',
                              'GEONWS', 'GEOTV', 'HUMST', 'HUMTV', 'MASALA',
                              'MUSIK', 'PTVGLB', 'QTVPAK'],
    'tvpassport.com': ['ary-musik/13698', 'ary-qtv/13700', 'ary-zauq/13701',
                       'atn--ary-digital-canada/13626', 'atn--ary-news/13699',
                       'atn--tv-18-urdu/14216'],
}

# loose scan patterns to catch candidates curation missed (applied to
# tvpassport only — tv24/streamingtvguides already enumerated)
LOOSE = re.compile(
    r'(\bhum\b|hum tv|geo|dun[yi]a|92 news|ptv|samaa|islam|urdu|pakistan|'
    r'khyber|lahore|dawn|bol tv|aaj|ary|zee tv|b4u|madani|sitaray|masala|'
    r'awaz|rohi|pashto|sindh)', re.I)

for site, ids in CURATED.items():
    txt = open(f'{REGS}/{site}/{site}.channels.xml', encoding='utf-8',
               errors='ignore').read()
    rows = re.findall(r'<channel\s+([^>]*)>([^<]*)</channel>', txt)
    by_id = {}
    for attrs, dn in rows:
        m = re.search(r'site_id="([^"]+)"', attrs)
        if m:
            by_id[m.group(1)] = (attrs, dn.strip())
    want = set(ids)
    if site == 'tvpassport.com':
        # add loose-scan candidates
        for attrs, dn in rows:
            m = re.search(r'site_id="([^"]+)"', attrs)
            if m and LOOSE.search(dn) and len(dn) < 60:
                want.add(m.group(1))
    keep = [f'<channel site="{site}" site_id="{sid}" lang="en" xmltv_id="">{dn}</channel>'
            for sid in sorted(want) if sid in by_id for attrs, dn in [by_id[sid]]]
    with open(f'{OUT}/{site}.pk.channels.xml', 'w', encoding='utf-8') as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<channels>\n')
        for k in keep:
            f.write(f'  {k}\n')
        f.write('</channels>\n')
    print(f'{site}: {len(keep)} candidates -> {OUT}/{site}.pk.channels.xml')
    for k in keep:
        print('   ', k[8:].split('>')[0].split('"')[1], '->', k.split('>')[1].split('<')[0])
