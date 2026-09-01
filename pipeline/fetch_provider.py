#!/usr/bin/env python3
"""Fetch provider data (streams, categories, xmltv) and write build artifacts.

Runs in GitHub Actions (credentials from secrets) and locally (credentials from
env vars). Outputs to <outdir>/:
  - streams.json      enriched stream list (name, category, stream_id, icon,
                      epg_channel_id) — epg_channel_id is THE field TiviMate
                      uses to match an Xtream channel to an EPG source, so it
                      must be captured, not dropped.
  - provider.xml      the panel's own xmltv.php (a proxied community feed)
  - provider_index.json  {ids: {id: display-name}, names_with_progs: [...]}
"""

import json
import os
import sys
import time
import urllib.request

UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'}


def fetch(url, timeout=180):
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=timeout).read()


def fetch_with_retry(url, timeout=180, attempts=3, delay=2, fetcher=fetch):
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return fetcher(url, timeout=timeout)
        except Exception as error:  # noqa: BLE001
            last_error = error
            if attempt < attempts:
                print(
                    f'Fetch attempt {attempt}/{attempts} failed: {error}; retrying',
                    file=sys.stderr,
                )
                time.sleep(delay)
    raise last_error


def main():
    outdir = sys.argv[1] if len(sys.argv) > 1 else './data'
    os.makedirs(outdir, exist_ok=True)

    server = os.environ.get('IPTV_SERVER', '').strip()
    user = os.environ.get('IPTV_USER', '').strip()
    passw = os.environ.get('IPTV_PASS', '').strip()
    if not (server and user and passw):
        print('FATAL: set IPTV_SERVER / IPTV_USER / IPTV_PASS', file=sys.stderr)
        sys.exit(2)

    # Prefer https (the panel exposes https_port 443); fall back to http.
    bases = []
    proto = os.environ.get('IPTV_PROTO', '')
    if proto:
        bases.append(f'{proto}://{server}')
    else:
        bases.append(f'https://{server}')
        bases.append(f'http://{server}')

    auth = f'username={user}&password={passw}'
    base = None
    last_err = None
    for b in bases:
        try:
            resp = json.loads(fetch(f'{b}/player_api.php?{auth}', timeout=30))
            if resp.get('user_info', {}).get('auth') == 1:
                base = b
                print(f'Auth OK on {b}: status={resp["user_info"].get("status")} '
                      f'tz={resp.get("server_info", {}).get("timezone")}')
                break
        except Exception as e:  # noqa: BLE001
            last_err = e
    if base is None:
        print(f'AUTH FAILED on all bases (last error: {last_err})', file=sys.stderr)
        sys.exit(1)

    # Streams + categories
    streams = json.loads(fetch(f'{base}/player_api.php?{auth}&action=get_live_streams', timeout=120))
    cats = json.loads(fetch(f'{base}/player_api.php?{auth}&action=get_live_categories', timeout=45))
    json.dump(streams, open(f'{outdir}/streams_raw.json', 'w'))
    json.dump(cats, open(f'{outdir}/categories_raw.json', 'w'))
    print(f'Streams: {len(streams)} | Categories: {len(cats)}')

    # Provider XMLTV is a required fallback source. Retry transient panel
    # failures here and stop early if all attempts fail.
    try:
        provider_xml = fetch_with_retry(
            f'{base}/xmltv.php?{auth}', timeout=240, attempts=3, delay=5
        )
    except Exception as e:  # noqa: BLE001
        print(f'FATAL: provider XMLTV fetch failed after retries: {e}', file=sys.stderr)
        sys.exit(1)
    if not provider_xml:
        print('FATAL: provider XMLTV response was empty', file=sys.stderr)
        sys.exit(1)
    open(f'{outdir}/provider.xml', 'wb').write(provider_xml)
    print(f'Provider XMLTV: {len(provider_xml)} bytes')

    # Enriched streams.json — keep epg_channel_id!
    cat_map = {c['category_id']: c['category_name'] for c in cats}
    out = []
    for s in streams:
        out.append({
            'name': s.get('name', ''),
            'cat_name': cat_map.get(s.get('category_id'), ''),
            'category_id': s.get('category_id'),
            'stream_id': s.get('stream_id'),
            'icon': s.get('stream_icon', ''),
            'epg_channel_id': s.get('epg_channel_id') or '',
        })
    json.dump(out, open(f'{outdir}/streams.json', 'w'), ensure_ascii=False)
    print(f'streams.json: {len(out)} channels')

    # Provider index (display-name <-> id, and which ids actually have programmes)
    if provider_xml:
        try:
            import re
            txt = provider_xml.decode('utf-8', errors='ignore')
            chan_re = re.compile(r'<channel id="([^"]*)">\s*<display-name[^>]*>([^<]*)</display-name>', re.S)
            chans = {m.group(1): m.group(2) for m in chan_re.finditer(txt) if m.group(1)}
            prog_chans = set(re.findall(r'<programme[^>]*?channel="([^"]*)"', txt))
            names_with_progs = sorted({chans[c] for c in prog_chans if c in chans and chans[c].strip()})
            json.dump({'ids': chans, 'names_with_progs': names_with_progs},
                      open(f'{outdir}/provider_index.json', 'w'), ensure_ascii=False)
            print(f'provider_index: {len(chans)} named channels, {len(names_with_progs)} with programmes')
        except Exception as e:  # noqa: BLE001
            print(f'provider_index build failed: {e}', file=sys.stderr)


if __name__ == '__main__':
    main()
