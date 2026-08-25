#!/usr/bin/env python3
"""Drive getepg.com's Livewire v2 API: load PK channel list, then per-channel schedules."""
import html as html_mod
import json
import re
import sys
import time
import urllib.request
import http.cookiejar

BASE = 'https://getepg.com'
UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36'}


class Session:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def get(self, path):
        req = urllib.request.Request(BASE + path, headers=UA)
        return self.opener.open(req, timeout=40).read().decode('utf-8', errors='replace')

    def post_livewire(self, comp, payload, csrf):
        url = f'{BASE}/livewire/message/{comp}'
        body = json.dumps(payload).encode()
        hdr = dict(UA)
        hdr.update({'Content-Type': 'application/json',
                    'X-CSRF-TOKEN': csrf, 'X-Requested-With': 'XMLHttpRequest',
                    'Accept': 'application/json'})
        req = urllib.request.Request(url, data=body, headers=hdr)
        return self.opener.open(req, timeout=60).read().decode('utf-8', errors='replace')


def extract_component(page, name):
    """Find wire:initial-data for a component by name, return (fingerprint, serverMemo)."""
    m = re.search(r'wire:initial-data="([^"]+)"[^>]*>', page)
    # find all components first
    for mm in re.finditer(r'wire:initial-data="([^"]+)"', page):
        raw = html_mod.unescape(mm.group(1))
        data = json.loads(raw)
        if data['fingerprint']['name'] == name:
            return data['fingerprint'], data['serverMemo']
    return None, None


def main():
    s = Session()
    page = s.get('/')
    # csrf from XSRF-TOKEN cookie
    csrf = None
    for c in s.jar:
        if c.name == 'XSRF-TOKEN':
            csrf = urllib.parse.unquote(c.value)
    print('csrf found:', bool(csrf), file=sys.stderr)

    fp, memo = extract_component(page, 'home-page-index')
    print('fingerprint id:', fp['id'], file=sys.stderr)

    # call loadChannelForCountry('PK')
    payload = {
        'fingerprint': fp,
        'serverMemo': memo,
        'updates': [{'type': 'callMethod', 'payload': {
            'id': fp['id'], 'method': 'loadChannelForCountry', 'params': ['PK']}}],
    }
    resp = s.post_livewire('home-page-index', payload, csrf)
    j = json.loads(resp)
    html = (j.get('effects') or {}).get('html') or ''
    open('/Users/shameez/workspace/epg/audit_output/getepg_pk_channels.html', 'w').write(html)
    print('effects html bytes:', len(html), file=sys.stderr)
    # parse channels: look for links/rows in the returned html
    rows = re.findall(r'<a[^>]*href="([^"]+)"[^>]*>([^<]{2,60})</a>', html)
    print(f'links in html: {len(rows)}', file=sys.stderr)
    for href, label in rows[:10]:
        print('  ', href, '|', label, file=sys.stderr)
    # also any wire:click="showChannelProgram..." style handlers
    clicks = re.findall(r'wire:click="[^"]*"', html)[:10]
    print('wire:click samples:', clicks, file=sys.stderr)
    open('/Users/shameez/workspace/epg/audit_output/getepg_pk_effect.json', 'w').write(resp)


if __name__ == '__main__':
    main()
