#!/usr/bin/env python3
"""Companion M3U whose tvg-id matches the EPG guide's channel ids EXACTLY.

AUDIT-4 F-01: the guide's XMLTV channel ids and the user's playlist tvg-ids
must agree, or TiviMate's tvg-id binding silently falls back to (ambiguous)
display-name matching. This script reads the SAME streams.json the pipeline
uses and applies the SAME canonical-id logic (including the duplicate-epg-id
collision split: keeper keeps the shared id, others move to
'xtream:<stream_id>'), then writes an M3U playlist.

Stream URLs use the provider's standard /live/<user>/<pass>/<id>.ts form;
auth comes from data/auth.json (never printed).

Usage: make_tivimate_m3u.py <streams.json> <auth.json> <out.m3u> [--collision-split file.json]
  --collision-split: optional JSON written by build_mapping (stream_name ->
    replacement cid); when absent the split is recomputed identically.
"""
import argparse
import json
import os
import re
import sys
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from build_mapping import build_identity_map, canonical_id  # noqa: E402


def m3u_text(value):
    """Safe one-line M3U attribute/display text."""
    value = re.sub(r'[\r\n\u2028\u2029\x00-\x1f]', ' ', str(value or ''))
    return value.replace('&', '&amp;').replace('"', '&quot;')


def m3u_url_text(value):
    return quote(str(value or ''), safe='')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('streams')
    ap.add_argument('auth')
    ap.add_argument('out')
    ap.add_argument('--collision-split', default=None)
    args = ap.parse_args()

    streams = json.load(open(args.streams))
    if args.collision_split and os.path.exists(args.collision_split):
        # mapping.json carries the split indirectly; a flat name->cid file is
        # simpler — recompute for now (deterministic, same inputs).
        pass
    identity = build_identity_map(streams)

    auth = json.load(open(args.auth))
    server = auth['server_info']['url']
    user = auth['user_info']['username']
    password = auth['user_info']['password']

    # category names for group titles
    cats = {}
    try:
        import urllib.request
        url = (f'http://{server}/player_api.php?username={user}'
               f'&password={password}&action=get_live_categories')
        cats = {c['category_id']: c['category_name']
                for c in json.load(urllib.request.urlopen(
                    urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'}),
                    timeout=60))}
    except Exception as e:  # noqa: BLE001
        print(f'[m3u] categories unavailable ({e}); continuing without groups')

    lines = ['#EXTM3U']
    n = 0
    scheme = 'https'
    eu = m3u_url_text(user); ep = m3u_url_text(password)
    for s in streams:
        sid = s.get('stream_id')
        name = s.get('name', '')
        if not sid or not name:
            continue
        # SAME rule as the guide: immutable stream identity map.
        cid = identity.get(str(sid)) or canonical_id(s)
        attrs = (
            f'tvg-id="{m3u_text(cid)}" '
            f'tvg-name="{m3u_text(name)}" '
            f'tvg-logo="{m3u_text(s.get("icon") or s.get("stream_icon") or "")}" '
            f'group-title="{m3u_text(cats.get(s.get("category_id"), s.get("cat_name") or ""))}"'
        )
        lines.append(f'#EXTINF:-1 {attrs},{m3u_text(name)}')
        lines.append(f'{scheme}://{server}/live/{eu}/{ep}/{m3u_url_text(sid)}.ts')
        n += 1

    with open(args.out, 'w') as f:
        f.write('\n'.join(lines) + '\n')
    n_split = sum(1 for cid in identity.values() if cid.startswith('xtream:'))
    print(f'[m3u] {n} channels ({n_split} on xtream:<stream_id> ids) -> {args.out}')


if __name__ == '__main__':
    sys.exit(main())
