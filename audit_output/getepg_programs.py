#!/usr/bin/env python3
"""GetEPG schedule probe: call show-channel-program.loadprogram(channel_id)."""
import html
import http.cookiejar
import json
import re
import sys
import urllib.parse
import urllib.request

BASE = 'https://getepg.com'
UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
      'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36')


def fresh_session():
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    with opener.open(urllib.request.Request(BASE + '/', headers={'User-Agent': UA}), timeout=60) as r:
        page = r.read().decode('utf-8', errors='replace')
    csrf = re.search(r'<meta\s+name=["\']csrf-token["\']\s+content=["\']([^"\']+)', page, re.I)
    csrf = html.unescape(csrf.group(1)) if csrf else None
    comps = {}
    for m in re.finditer(r'wire:initial-data="([^"]+)"', page):
        data = json.loads(html.unescape(m.group(1)))
        comps[data['fingerprint']['name']] = data
    return opener, csrf, comps


def call(opener, csrf, comp, method, params):
    fp = comp['fingerprint']
    memo = comp['serverMemo']
    payload = {
        'fingerprint': fp,
        'serverMemo': memo,
        'updates': [{'type': 'callMethod',
                     'payload': {'id': fp['id'], 'method': method, 'params': params}}],
    }
    body = json.dumps(payload, separators=(',', ':')).encode()
    hdr = {'User-Agent': UA, 'Content-Type': 'application/json',
           'X-CSRF-TOKEN': csrf, 'X-Requested-With': 'XMLHttpRequest',
           'X-Livewire': 'true', 'Accept': 'text/html, application/xhtml+xml',
           'Referer': BASE + '/', 'Origin': BASE}
    req = urllib.request.Request(
        BASE + '/livewire/message/' + fp['name'], data=body, headers=hdr)
    with opener.open(req, timeout=90) as r:
        return json.loads(r.read().decode('utf-8', errors='replace'))


if __name__ == '__main__':
    channel = sys.argv[1] if len(sys.argv) > 1 else 'PTVHome.pk'
    opener, csrf, comps = fresh_session()
    try:
        j = call(opener, csrf, comps['show-channel-program'], 'loadprogram', [channel])
        d = (j.get('serverMemo') or {}).get('data') or {}
        programs = d.get('programs')
        print('channel:', channel, '| programs type:', type(programs).__name__,
              '| len:', len(programs) if hasattr(programs, '__len__') else '?')
        out = json.dumps(j, ensure_ascii=False)
        open(f'/Users/shameez/workspace/epg/audit_output/getepg_prog_{channel}.json', 'w').write(out)
        if isinstance(programs, list) and programs:
            print('sample:', json.dumps(programs[0], ensure_ascii=False)[:500])
        else:
            eh = (j.get('effects') or {}).get('html') or ''
            print('effects html bytes:', len(eh))
            txt = re.sub(r'<[^>]+>', ' ', eh)
            txt = re.sub(r'\s+', ' ', txt).strip()
            print('effects text:', txt[:400])
    except urllib.error.HTTPError as e:
        print('FAILED', e.code, e.read().decode('utf-8', errors='replace')[:300])
