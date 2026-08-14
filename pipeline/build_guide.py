#!/usr/bin/env python3
"""Build the final XMLTV guide.

Layers (in priority order for a channel):
  1. PK scrapers (custom, per-channel keys via overrides)
  2. iptv-org grabber output (India: guide XML from iptv-org/epg run)
  3. epg.pw global feed (worldwide base)
  4. provider's own xmltv.php (long tail)

Channel ids in the OUTPUT are the provider stream names themselves
(TiviMate matches playlist tvg-id == XMLTV channel id; we will generate the
playlist with tvg-id = stream name), guaranteeing a 1:1 match.

Usage:
  build_guide.py --streams streams.json --mapping mapping.json \
      --pw epgpw_global.xml [--io io_guide.xml] [--provider provider.xml] \
      --pk pk_epg.json --out guide.xml
"""
import argparse
import gzip
import json
import re
import sys
from collections import defaultdict
from xml.sax.saxutils import escape, quoteattr

CHANNEL_META_RE = re.compile(
    r'<channel id="(?P<id>[^"]*)"[^>]*>\s*(?:<display-name[^>]*>(?P<dn>[^<]*)</display-name>)?'
    r'(?:\s*<icon src="(?P<icon>[^"]*)")?', re.S)
PROG_RE = re.compile(r'<programme\s+(.*?)>(?P<body>.*?)</programme>', re.S)
ATTR_RE = re.compile(r'(\w+)="([^"]*)"')
TITLE_RE = re.compile(r'<title[^>]*>([^<]*)</title>')
DESC_RE = re.compile(r'<desc[^>]*>([^<]*)</desc>')
CAT_RE = re.compile(r'<category[^>]*>([^<]*)</category>')


def parse_xmltv(path, want_channels=None, key_by_display_name=False):
    """Streaming-ish parse: returns (channels{id:(dn,icon)}, progs{id:[(start,stop,title,desc,cat)]}).
    key_by_display_name=True re-keys both dicts by display-name (for feeds with empty ids)."""
    channels, progs = {}, defaultdict(list)
    if path.endswith('.gz'):
        import gzip as _gz
        data = _gz.open(path, 'rb').read().decode('utf-8', errors='ignore')
    else:
        data = open(path, 'r', errors='ignore').read()
    for m in CHANNEL_META_RE.finditer(data):
        key = m.group('dn') if key_by_display_name else m.group('id')
        if key:
            channels[key] = (m.group('dn') or m.group('id'), m.group('icon') or '')
    for m in PROG_RE.finditer(data):
        attrs = dict(ATTR_RE.findall(m.group(1)))
        ch = attrs.get('channel', '')
        body_ch = m.group('body')
        t = TITLE_RE.search(body_ch)
        d = DESC_RE.search(body_ch)
        c = CAT_RE.search(body_ch)
        entry = (attrs.get('start', ''), attrs.get('stop', ''),
                 t.group(1) if t else '', d.group(1) if d else '', c.group(1) if c else '')
        if key_by_display_name:
            dn = channels.get(ch, (ch, ''))[0] if ch in channels else None
            if dn is None:
                continue
            progs[dn].append(entry)
        else:
            if want_channels and ch not in want_channels:
                continue
            progs[ch].append(entry)
    return channels, progs


def fmt_out(start_iso, stop_iso, title, desc, cat):
    def xfmt(iso):
        # '20260814000000 +0000' or ISO → XMLTV 'YYYYMMDDHHMMSS +0000'
        if 'T' in iso or ':' in iso:
            iso = iso.replace('-', '').replace(':', '').replace('T', '').replace('Z', '').split('.')[0]
            return iso[:14] + ' +0000'
        return iso
    return (f'  <programme start={quoteattr(xfmt(start_iso))} stop={quoteattr(xfmt(stop_iso))}'
            f' channel="__CH__">\n    <title lang="en">{escape(title)}</title>\n'
            + (f'    <desc lang="en">{escape(desc[:500])}</desc>\n' if desc else '')
            + (f'    <category lang="en">{escape(cat)}</category>\n' if cat else '')
            + '  </programme>\n')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--streams', required=True)
    ap.add_argument('--mapping', required=True)
    ap.add_argument('--pw')
    ap.add_argument('--io')
    ap.add_argument('--provider')
    ap.add_argument('--pk')
    ap.add_argument('--out', required=True)
    args = ap.parse_args()

    streams = json.load(open(args.streams))
    mapping = json.load(open(args.mapping))  # stream_name -> {...source info}
    pk = json.load(open(args.pk)) if args.pk else {}

    layers = []
    if args.pw:
        layers.append(('epg.pw', args.pw))
    if args.io:
        layers.append(('iptv-org', args.io))
    if args.provider:
        layers.append(('provider', args.provider))

    parsed = {}
    for name, path in layers:
        ch, pr = parse_xmltv(path)
        parsed[name] = (ch, pr)
        print(f'[layer] {name}: {len(ch)} channels, programmes for {len(pr)}', file=sys.stderr)

    out_chans, out_progs = [], defaultdict(list)
    used_layers = defaultdict(int)

    for s in streams:
        name = s['name']
        mp = mapping.get(name)
        if not mp:
            continue
        src, sid = mp['source'], mp['source_id']
        # PK scrapers: source_id is a channel_key with programmes directly
        if src == 'pk':
            progs = pk.get(sid, [])
            for p in progs:
                out_progs[name].append(fmt_out(p['start'], p['stop'], p['title'], '', ''))
            if progs:
                out_chans.append((name, sid.replace('_', ' ').title(), ''))
                used_layers['pk'] += 1
            continue
        chmap, progs = parsed.get(src, ({}, defaultdict(list)))
        if sid not in progs and sid in chmap:
            continue  # matched channel but no programmes
        plist = progs.get(sid, [])
        dn, icon = chmap.get(sid, (sid, ''))
        if not plist:
            continue
        out_chans.append((name, dn, icon))
        for (st, sp, ti, de, ca) in plist:
            out_progs[name].append(fmt_out(st if ' ' in st or '+' in st else st, sp if ' ' in sp or '+' in sp else sp, ti, de, ca))
        used_layers[src] += 1

    with open(args.out, 'w') as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE tv SYSTEM "xmltv.dtd">\n<tv source-info-name="hermes-epg-pipeline">\n')
        seen = set()
        for name, dn, icon in out_chans:
            if name in seen:
                continue
            seen.add(name)
            f.write(f'  <channel id={quoteattr(name)}>\n    <display-name>{escape(dn or name)}</display-name>\n')
            if icon:
                f.write(f'    <icon src={quoteattr(icon)}/>\n')
            f.write('  </channel>\n')
        for name, plist in out_progs.items():
            for p in plist:
                f.write(p.replace('__CH__', name))
        f.write('</tv>\n')

    total_progs = sum(len(v) for v in out_progs.values())
    print(f'[done] channels with programmes: {len(out_progs)} | total programmes: {total_progs} | per-layer: {dict(used_layers)}')


if __name__ == '__main__':
    main()
