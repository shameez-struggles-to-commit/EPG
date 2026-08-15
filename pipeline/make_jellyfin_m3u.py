#!/usr/bin/env python3
"""Regenerate the Jellyfin M3U tuner playlist for the Gateway IPTV provider.

Sources channels from player_api.php (get_live_streams — get.php returns an
EMPTY body on this provider) and writes an M3U with tvg-id/tvg-logo/group
attributes to the Jellyfin media dir. Run after provider lineup changes:

    python3 pipeline/make_jellyfin_m3u.py [--out /path/out.m3u]

Then in Jellyfin: Dashboard > Live TV > tuners > Gateway IPTV (local) > Refresh.
Auth comes from data/auth.json (same file the EPG pipeline uses).
"""
import argparse
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
AUTH = os.path.join(HERE, '..', 'data', 'auth.json')
DEFAULT_OUT = ('/Volumes/plex_temporary_directory/Docker_Config/'
               'jellyfin/media/gateway_live.m3u')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=DEFAULT_OUT)
    args = ap.parse_args()

    auth = json.load(open(AUTH))
    server = auth['server_info']['url']
    user = auth['user_info']['username']
    password = auth['user_info']['password']

    url = (f'http://{server}/player_api.php?username={user}'
           f'&password={password}&action=get_live_streams')
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    streams = json.load(urllib.request.urlopen(req, timeout=60))
    print(f'fetched {len(streams)} live streams from {server}')

    cat_url = (f'http://{server}/player_api.php?username={user}'
               f'&password={password}&action=get_live_categories')
    req = urllib.request.Request(cat_url, headers={'User-Agent': 'Mozilla/5.0'})
    cats = {c['category_id']: c['category_name']
            for c in json.load(urllib.request.urlopen(req, timeout=60))}
    print(f'fetched {len(cats)} categories')

    lines = ['#EXTM3U']
    for s in streams:
        sid = s.get('stream_id')
        if not sid:
            continue
        attrs = (
            f"tvg-id=\"{s.get('epg_channel_id') or ''}\" "
            f"tvg-name=\"{s.get('name', '')}\" "
            f"tvg-logo=\"{s.get('stream_icon') or ''}\" "
            f"group-title=\"{cats.get(s.get('category_id'), '')}\""
        )
        lines.append(f'#EXTINF:-1 {attrs},{s.get("name", "")}')
        lines.append(f'http://{server}/live/{user}/{password}/{sid}.ts')

    with open(args.out, 'w') as f:
        f.write('\n'.join(lines))
    print(f'wrote {(len(lines) - 1) // 2} channels -> {args.out}')


if __name__ == '__main__':
    sys.exit(main())
