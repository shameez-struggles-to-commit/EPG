#!/usr/bin/env python3
"""Master merge: turn the provider streams + mapping + source feeds into the
final XMLTV guide.

Key behaviors:
  - Fallback cascade: for each stream, walk its ordered candidate list and use
    the first source that actually has programmes (fixes the ~1,900 dropped
    channels caused by single-source mapping).
  - Canonical channel id = epg_channel_id when real, else raw stream name
    (matches TiviMate's native Xtream matching / name-fallback).
  - Time normalization: every programme time is converted to a true UTC instant
    (offsets are APPLIED, never silently discarded).
  - Dedupe by canonical id (quality variants of the same channel share one id).

Inputs (see workflow):
  --streams streams.json --mapping mapping.json --sources sources.json
  --io io_india.xml --provider provider.xml --pk pk_epg.json --out guide.xml.gz
"""

import argparse
import datetime as dt
import gzip
import json
import os
import re
import sys
from collections import defaultdict
from xml.sax.saxutils import escape, quoteattr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from matcher import norm as _norm_name
from matcher import is_non_linear

# -- XMLTV parsing ---------------------------------------------------------
CHAN_BLOCK_RE = re.compile(r'<channel\s+id="(?P<id>[^"]*)"[^>]*>(?P<body>.*?)</channel>', re.S)
DN_RE = re.compile(r'<display-name[^>]*>([^<]*)</display-name>')
ICON_RE = re.compile(r'<icon\s+src="([^"]*)"')
PROG_RE = re.compile(r'<programme\s+(.*?)>(.*?)</programme>', re.S)
ATTR_RE = re.compile(r'(\w+)="([^"]*)"')
TITLE_RE = re.compile(r'<title[^>]*>([^<]*)</title>')
DESC_RE = re.compile(r'<desc[^>]*>([^<]*)</desc>')
CAT_RE = re.compile(r'<category[^>]*>([^<]*)</category>')
XMLTV_TS_RE = re.compile(r'^(\d{14})\s*([+-]\d{4})?$')


def read(path):
    if path.endswith('.gz'):
        return gzip.open(path, 'rb').read().decode('utf-8', errors='ignore')
    return open(path, 'r', errors='ignore').read()


def norm_time(t):
    """Convert any XMLTV/ISO timestamp to a UTC instant 'YYYYMMDDHHMMSS +0000'.

    Applies the offset (never discards it). Bare XMLTV times (no offset) are
    assumed already-UTC (the common convention for epg.pw / epgshare01).
    """
    t = (t or '').strip()
    if not t:
        return t
    # ISO-8601 (has 'T', or dash+colon like 2026-08-14 15:00:00)
    if 'T' in t or ('-' in t and ':' in t):
        iso = t.replace('Z', '+00:00')
        try:
            d = dt.datetime.fromisoformat(iso)
        except ValueError:
            return t
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.astimezone(dt.timezone.utc).strftime('%Y%m%d%H%M%S') + ' +0000'
    m = XMLTV_TS_RE.match(t)
    if not m:
        return t
    base = m.group(1)
    off = m.group(2) or '+0000'
    try:
        y = int(base[0:4])
        mo = int(base[4:6])
        d = int(base[6:8])
        h = int(base[8:10])
        mi = int(base[10:12])
        se = int(base[12:14])
        sign = 1 if off[0] == '+' else -1
        oh, om = int(off[1:3]), int(off[3:5])
        delta = dt.timedelta(hours=sign * oh, minutes=sign * om)
        local = dt.datetime(y, mo, d, h, mi, se)
        utc = local - delta
    except (ValueError, IndexError):
        return t
    return utc.strftime('%Y%m%d%H%M%S') + ' +0000'


def parse_xmltv(path, needed_ids, min_stop=None):
    """Parse one XMLTV file, keeping only channels/programmes in needed_ids.

    Returns (channels {id:(dn,icon)}, progs {id:[(start,stop,title,desc,cat)]}).
    If min_stop is set (YYYYMMDD string), programmes whose stop is older are
    dropped — the currency gate that prevents stale upstream feeds (like the
    dead globetvapp) from polluting the guide.
    """
    data = read(path)
    channels = {}
    for m in CHAN_BLOCK_RE.finditer(data):
        cid = m.group('id')
        if not cid or cid not in needed_ids:
            continue
        body = m.group('body')
        dn = DN_RE.search(body)
        ic = ICON_RE.search(body)
        channels[cid] = (dn.group(1) if dn else cid, ic.group(1) if ic else '')
    progs = defaultdict(list)
    n_stale = 0
    for m in PROG_RE.finditer(data):
        attrs = dict(ATTR_RE.findall(m.group(1)))
        ch = attrs.get('channel', '')
        if ch not in needed_ids:
            continue
        st, sp = attrs.get('start', ''), attrs.get('stop', '')
        if min_stop and sp:
            # compare on the raw digits before any offset application
            if sp[:8] < min_stop:
                n_stale += 1
                continue
        body = m.group(2)
        t = TITLE_RE.search(body)
        d = DESC_RE.search(body)
        c = CAT_RE.search(body)
        progs[ch].append((st, sp,
                          t.group(1) if t else '', d.group(1) if d else '',
                          c.group(1) if c else ''))
    if n_stale:
        print(f'[currency] {os.path.basename(path)}: dropped {n_stale} stale programmes')
    return channels, progs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--streams', required=True)
    ap.add_argument('--mapping', required=True)
    ap.add_argument('--sources', required=True, help='fetch_sources sources.json manifest')
    ap.add_argument('--provider', help='provider xmltv.php XML')
    ap.add_argument('--pk', help='PK scrapers JSON')
    ap.add_argument('--out', required=True)
    ap.add_argument('--coverage-out', default=None)
    ap.add_argument('--min-stop', default=None,
                    help='Currency gate: drop programmes whose stop date '
                         '(YYYYMMDD) is older. Default: today UTC.')
    args = ap.parse_args()

    min_stop = args.min_stop or dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d')

    streams = json.load(open(args.streams))
    mapping = json.load(open(args.mapping))
    pk = json.load(open(args.pk)) if args.pk and os.path.exists(args.pk) else {}
    manifest = json.load(open(args.sources))

    # source label -> list of file paths (a source may span several files
    # when a grab was split, e.g. programtv.onet.pl_a/_b — they merge here)
    src_files = defaultdict(list)
    for m in manifest:
        src_files[m['source']].append(m['file'])
    if args.provider:
        src_files['provider'] = [args.provider]

    # collect candidate ids per source
    need = defaultdict(set)
    for sid, mp in mapping.items():
        for c in mp['candidates']:
            need[c['source']].add(c['source_id'])

    # parse each source once, keep only needed ids
    channels, progs = {}, {}
    for src, ids in need.items():
        if src == 'pk':
            continue
        paths = src_files.get(src)
        if not paths:
            print(f'[warn] no file for source {src}', file=sys.stderr)
            continue
        try:
            ch_all, pr_all = {}, defaultdict(list)
            for path in paths:
                ch, pr = parse_xmltv(path, ids, min_stop=min_stop)
                ch_all.update(ch)
                for k, v in pr.items():
                    pr_all[k].extend(v)
            channels[src] = ch_all
            progs[src] = pr_all
            print(f'[layer] {src}: {len(ch_all)} channels, {sum(len(v) for v in pr_all.values())} programmes')
        except Exception as e:  # noqa: BLE001
            print(f'[layer] {src} FAILED: {e}', file=sys.stderr)

    # win: canonical_id -> (display_name, icon, [(start,stop,title,desc,cat)])
    used = defaultdict(int)
    out_chans = {}
    out_progs = defaultdict(list)
    no_data = defaultdict(int)

    for s in streams:
        sid = str(s.get('stream_id', s.get('name', '')))
        mp = mapping.get(sid)
        if not mp:
            continue
        cid = mp['canonical_id']
        if not cid:
            continue
        picked = None
        for c in mp['candidates']:
            src, source_id = c['source'], c['source_id']
            if src == 'pk':
                if pk.get(source_id):
                    picked = (src, source_id, c['method'])
                    break
                continue
            pr = progs.get(src, {}).get(source_id)
            if pr:
                picked = (src, source_id, c['method'])
                break
        if not picked:
            no_data['none'] += 1
            continue
        src, source_id, method = picked

        # programmes
        plist = []
        if src == 'pk':
            for p in pk[source_id]:
                plist.append((p['start'], p['stop'], p['title'], '', ''))
        else:
            plist = progs[src][source_id]

        # display-name: stream name first, then the source channel's name
        dn, icon = '', s.get('icon', '')
        if src != 'pk':
            dn, _icon = channels.get(src, {}).get(source_id, ('', ''))
            icon = icon or _icon
        display_names = [s.get('name', '')]
        if dn and _norm_name(dn) != _norm_name(s.get('name', '')):
            display_names.append(dn)

        if cid not in out_chans:
            out_chans[cid] = (display_names, icon)
        out_progs[cid].extend(plist)
        used[src] += 1

    # Pre-pass: apply the currency gate + dedupe in memory, then drop channels
    # left with zero programmes (an empty <channel> is dead weight).
    final_progs = {}
    seen = set()
    stale_writes = 0
    for cid, plist in out_progs.items():
        keep = []
        for (st, sp, ti, de, ca) in plist:
            nst, nsp = norm_time(st), norm_time(sp)
            if nsp and nsp[:8] < min_stop:
                stale_writes += 1
                continue
            key = (nst, cid)
            if key in seen:
                continue
            seen.add(key)
            keep.append((nst, nsp, ti, de, ca))
        if keep:
            final_progs[cid] = keep
    if stale_writes:
        print(f'[currency] dropped {stale_writes} stale programmes at write time')
    empty_ids = [cid for cid in out_chans if cid not in final_progs]
    for cid in empty_ids:
        del out_chans[cid]
    if empty_ids:
        print(f'[currency] dropped {len(empty_ids)} channels left with 0 programmes')

    total = 0
    with gzip.open(args.out, 'wt', encoding='utf-8') if args.out.endswith('.gz') \
            else open(args.out, 'w', encoding='utf-8') as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write('<!DOCTYPE tv SYSTEM "xmltv.dtd">\n')
        f.write('<tv generator-info-name="hermes-epg-pipeline" '
                'source-info-name="merged: pk + iptv-org + epgshare01 + epg.pw + provider">\n')
        for cid, (dns, icon) in out_chans.items():
            f.write(f'  <channel id={quoteattr(cid)}>\n')
            for dn in dns:
                f.write(f'    <display-name>{escape(dn)}</display-name>\n')
            if icon:
                f.write(f'    <icon src={quoteattr(icon)}/>\n')
            f.write('  </channel>\n')
        for cid, plist in final_progs.items():
            for (nst, nsp, ti, de, ca) in plist:
                f.write(f'  <programme start={quoteattr(nst)} '
                        f'stop={quoteattr(nsp)} channel={quoteattr(cid)}>\n')
                f.write(f'    <title lang="en">{escape(ti)}</title>\n')
                if de:
                    f.write(f'    <desc lang="en">{escape(de[:500])}</desc>\n')
                if ca:
                    f.write(f'    <category lang="en">{escape(ca)}</category>\n')
                f.write('  </programme>\n')
                total += 1
        f.write('</tv>\n')

    print(f'[done] channels: {len(out_chans)} | programmes: {total} | per-source: {dict(used)}')

    if args.coverage_out:
        linear = [s for s in streams if not is_non_linear(s.get('cat_name', ''), s.get('name', ''))]
        linear_names = {s['name'] for s in linear}
        cov = {
            'total_streams': len(streams),
            'linear_streams': len(linear),
            'linear_unique_names': len(linear_names),
            'covered_channels': len(out_chans),
            'programmes': total,
            'per_source': dict(used),
            'no_data': dict(no_data),
        }
        json.dump(cov, open(args.coverage_out, 'w'), indent=1)
        denom = max(1, cov['linear_unique_names'])
        print(f'[coverage] {cov["covered_channels"]}/{denom} linear unique names covered '
              f'({100*cov["covered_channels"]/denom:.1f}%)')


if __name__ == '__main__':
    main()
