#!/usr/bin/env python3
"""Master EPG pipeline — runs all layers and builds guide.xml.gz.

Layer priority (first match wins per channel):
  1. PK scrapers (Geo, Hum, ARY)
  2. iptv-org grabber output (India: JioTV/TataPlay/DishTV/Airtel/Zee5)
  3. epg.pw global feed (worldwide base, 15k+ channels)
  4. Provider's own xmltv.php (US locals + misc long tail)

Usage: python3 build_pipeline.py --streams streams.json --mapping mapping.json \
  --pw epgpw.xml.gz --io io_guide.xml --provider provider.xml --pk pk_epg.json \
  --out guide.xml.gz
"""
import argparse
import gzip
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

# Add pipeline dir for matcher import
sys.path.insert(0, str(Path(__file__).parent))

# -- XMLTV parsing ---------------------------------------------------------
CHAN_RE = re.compile(
    r'<channel\s+id="([^"]*)"[^>]*>\s*<display-name[^>]*>([^<]*)</display-name>'
    r'(?:\s*<icon\s+src="([^"]*)"[^>]*/>)?\s*</channel>', re.S)
PROG_RE = re.compile(r'<programme\s+(.*?)>(.*?)</programme>', re.S)
ATTR_RE = re.compile(r'(\w+)="([^"]*)"')
TITLE_RE = re.compile(r'<title[^>]*>([^<]*)</title>')
DESC_RE = re.compile(r'<desc[^>]*>([^<]*)</desc>')
CAT_RE = re.compile(r'<category[^>]*>([^<]*)</category>')


def parse_xmltv(path):
    """Returns (channels: id->(dn,icon), progs: id->[(start,stop,title,desc,cat)])."""
    if path.endswith('.gz'):
        data = gzip.open(path, 'rb').read().decode('utf-8', errors='ignore')
    else:
        data = open(path, 'r', errors='ignore').read()
    channels = {}
    for m in CHAN_RE.finditer(data):
        channels[m.group(1)] = (m.group(2) or m.group(1), m.group(3) or '')
    progs = defaultdict(list)
    for m in PROG_RE.finditer(data):
        attrs = dict(ATTR_RE.findall(m.group(1)))
        ch = attrs.get('channel', '')
        if not ch or ch not in channels:
            continue
        t = TITLE_RE.search(m.group(2))
        d = DESC_RE.search(m.group(2))
        c = CAT_RE.search(m.group(2))
        progs[ch].append((attrs.get('start', ''), attrs.get('stop', ''),
                          t.group(1) if t else '', d.group(1) if d else '', c.group(1) if c else ''))
    return channels, progs


def fmt_time(iso):
    if 'T' in iso or ':' in iso:
        iso = iso.replace('-', '').replace(':', '').replace('T', '').replace('Z', '').split('.')[0]
        return iso[:14] + ' +0000'
    return iso


def fmt_prog(start, stop, channel_id, title, desc, cat):
    s = f'  <programme start={quoteattr(fmt_time(start))} stop={quoteattr(fmt_time(stop))} channel={quoteattr(channel_id)}>\n'
    s += f'    <title lang="en">{escape(title)}</title>\n'
    if desc:
        s += f'    <desc lang="en">{escape(desc[:500])}</desc>\n'
    if cat:
        s += f'    <category lang="en">{escape(cat)}</category>\n'
    s += '  </programme>\n'
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--streams', required=True)
    ap.add_argument('--mapping', required=True)
    ap.add_argument('--pw', help='epg.pw global XMLTV (.xml or .xml.gz)')
    ap.add_argument('--io', help='iptv-org grabber output XML')
    ap.add_argument('--provider', help='provider xmltv.php XML')
    ap.add_argument('--pk', help='PK scrapers JSON')
    ap.add_argument('--out', required=True)
    args = ap.parse_args()

    streams = json.load(open(args.streams))
    mapping = json.load(open(args.mapping))
    pk = json.load(open(args.pk)) if args.pk else {}

    # Parse XMLTV layers
    layers = {}
    for name, path in [('epg.pw', args.pw), ('iptv-org', args.io), ('provider', args.provider)]:
        if path and Path(path).exists():
            ch, pr = parse_xmltv(path)
            layers[name] = (ch, pr)
            print(f'[layer] {name}: {len(ch)} channels, {sum(len(v) for v in pr.values())} programmes', file=sys.stderr)

    # Build normalized display-name -> channel-id indexes for iptv-org layer
    import unicodedata as _ud
    def _norm(s):
        s = _ud.normalize('NFKD', (s or '').lower())
        s = re.sub(r'\b(fhd|uhd|hd|sd|4k)\b.*$', '', s)
        s = re.sub(r'[^\w\s]', ' ', s)
        drop = {'the', 'tv', 'channel', 'network'}
        return ' '.join(t for t in re.findall(r'\w+', s) if t not in drop)

    io_name_index = {}
    if 'iptv-org' in layers:
        for cid, (dn, _) in layers['iptv-org'][0].items():
            io_name_index[_norm(dn)] = cid

    out_chans = {}   # name -> (display_name, icon)
    out_progs = defaultdict(list)
    used = defaultdict(int)
    pk_names = {v['source_id'] for v in mapping.values() if v['source'] == 'pk'}

    for s in streams:
        name = s['name']
        mp = mapping.get(name)

        # Layer 1: PK scrapers (highest priority)
        if mp and mp['source'] == 'pk':
            progs = pk.get(mp['source_id'], [])
            if progs:
                out_chans[name] = (mp['source_id'].replace('_', ' ').title(), s.get('icon', ''))
                for p in progs:
                    out_progs[name].append((p['start'], p['stop'], p['title'], '', ''))
                used['pk'] += 1
            continue

        matched = False
        # Layer 2: iptv-org (display-name match — works for all channels, not just mapped)
        if 'iptv-org' in layers and not matched:
            chmap, progs = layers['iptv-org']
            cid = io_name_index.get(_norm(name))
            if cid and cid in progs:
                out_chans[name] = chmap.get(cid, (name, s.get('icon', '')))
                for (st, sp, ti, de, ca) in progs[cid]:
                    out_progs[name].append((st, sp, ti, de, ca))
                used['iptv-org'] += 1
                matched = True

        # Layer 3: epg.pw (via mapping)
        if mp and mp['source'] == 'epg.pw' and 'epg.pw' in layers and not matched:
            chmap, progs = layers['epg.pw']
            sid = mp['source_id']
            if sid in progs:
                out_chans[name] = chmap.get(sid, (name, s.get('icon', '')))
                for (st, sp, ti, de, ca) in progs[sid]:
                    out_progs[name].append((st, sp, ti, de, ca))
                used['epg.pw'] += 1
                matched = True

        # Layer 4: provider (via mapping)
        if mp and mp['source'] == 'provider' and 'provider' in layers and not matched:
            chmap, progs = layers['provider']
            sid = mp['source_id']
            if sid in progs:
                out_chans[name] = chmap.get(sid, (name, s.get('icon', '')))
                for (st, sp, ti, de, ca) in progs[sid]:
                    out_progs[name].append((st, sp, ti, de, ca))
                used['provider'] += 1
                matched = True

    # Write output
    total = sum(len(v) for v in out_progs.values())
    with gzip.open(args.out, 'wt', encoding='utf-8') if args.out.endswith('.gz') else open(args.out, 'w', encoding='utf-8') as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE tv SYSTEM "xmltv.dtd">\n')
        f.write('<tv source-info-name="hermes-epg-pipeline" generator-info-name="hermes-epg">\n')
        for name, (dn, icon) in out_chans.items():
            f.write(f'  <channel id={quoteattr(name)}>\n')
            f.write(f'    <display-name>{escape(dn or name)}</display-name>\n')
            if icon:
                f.write(f'    <icon src={quoteattr(icon)}/>\n')
            f.write('  </channel>\n')
        for name, plist in out_progs.items():
            for (st, sp, ti, de, ca) in plist:
                f.write(fmt_prog(st, sp, name, ti, de, ca))
        f.write('</tv>\n')

    print(f'[done] channels: {len(out_chans)} | programmes: {total} | layers: {dict(used)}')


if __name__ == '__main__':
    main()
