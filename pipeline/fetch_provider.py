#!/usr/bin/env python3
"""Fetch provider data (streams, categories, xmltv) and save to artifacts.
Runs in GitHub Actions — credentials from secrets."""
import json
import os
import sys
import urllib.request

UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}
SERVER = os.environ.get('IPTV_SERVER', '')
USER = os.environ.get('IPTV_USER', '')
PASS = os.environ.get('IPTV_PASS', '')


def fetch(url, timeout=120):
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=timeout).read()


def main():
    outdir = sys.argv[1] if len(sys.argv) > 1 else './data'
    os.makedirs(outdir, exist_ok=True)

    base = f'http://{SERVER}'
    auth = f'username={USER}&password={PASS}'

    # 1. Auth check
    auth_resp = json.loads(fetch(f'{base}/player_api.php?{auth}', timeout=30))
    if auth_resp.get('user_info', {}).get('auth') != 1:
        print('AUTH FAILED', file=sys.stderr)
        sys.exit(1)
    print(f'Auth OK: {auth_resp["user_info"]["status"]}')

    # 2. Streams + categories
    streams = json.loads(fetch(f'{base}/player_api.php?{auth}&action=get_live_streams', timeout=120))
    cats = json.loads(fetch(f'{base}/player_api.php?{auth}&action=get_live_categories', timeout=30))
    json.dump(streams, open(f'{outdir}/streams_raw.json', 'w'))
    json.dump(cats, open(f'{outdir}/categories_raw.json', 'w'))
    print(f'Streams: {len(streams)} | Categories: {len(cats)}')

    # 3. Provider XMLTV
    try:
        xmltv = fetch(f'{base}/xmltv.php?{auth}', timeout=180)
        open(f'{outdir}/provider.xml', 'wb').write(xmltv)
        print(f'Provider XMLTV: {len(xmltv)} bytes')
    except Exception as e:
        print(f'Provider XMLTV fetch failed: {e}', file=sys.stderr)

    # 4. Build enriched streams.json
    cat_map = {c['category_id']: c['category_name'] for c in cats}
    out = [{'name': s['name'], 'cat_name': cat_map.get(s.get('category_id'), ''),
            'stream_id': s['stream_id'], 'icon': s.get('stream_icon', '')} for s in streams]
    json.dump(out, open(f'{outdir}/streams.json', 'w'))
    print(f'streams.json: {len(out)} channels')


if __name__ == '__main__':
    main()
