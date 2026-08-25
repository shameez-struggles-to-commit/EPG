#!/usr/bin/env python3
"""Investigation I: Xtream per-stream EPG audit (get_short_epg + get_simple_data_table).

Queries both panel actions for all 107 PK linear streams, records rows, decodes
base64 titles/descriptions, classifies currency + genericity + identity.
Polite: 0.25s between requests. Writes audit_output/xtream_perstream.json.
"""
import json, re, sys, time, base64, urllib.request, datetime as dt

sys.path.insert(0, '/Users/shameez/workspace/epg/pipeline')
from build_mapping import is_non_linear

BASE = 'https://gtvprem.com/player_api.php?username=Live4County&password=XwsWJs4VhU'
UA = {'User-Agent': 'Mozilla/5.0'}
GEN_TITLE = re.compile(
    r'news headlines|news bulletin|news flash|news update|^news$|headline news|'
    r'to be announced|^tba$|no match|^tv one$|live|^epg$|^test$|^program(me)?$|'
    r'^movie$|^drama$|^series$|^show$|^music$|^news broadcast$', re.I)


def b64_maybe(s):
    if not s:
        return s
    if re.match(r'^[A-Za-z0-9+/=]+$', s) and len(s) % 4 == 0 and len(s) >= 8:
        try:
            d = base64.b64decode(s).decode('utf-8')
            if d.isprintable() or any('\u00e0' <= c <= '\u017f' or c in 'ضصثقفغعهخحجد' for c in d):
                return d
        except Exception:
            pass
    return s


def q(action, sid):
    url = f'{BASE}&action={action}&stream_id={sid}'
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.status, r.read().decode('utf-8', errors='replace')


def parse_rows(data, action):
    """Return list of dicts {title, desc, start, end, start_ts}."""
    out = []
    try:
        j = json.loads(data)
    except Exception:
        return out, 'unparseable'
    lst = j.get('epg_listings') or []
    for e in lst:
        if not isinstance(e, dict):
            continue
        title = b64_maybe(str(e.get('title') or e.get('name') or ''))
        desc = b64_maybe(str(e.get('description') or e.get('descr') or ''))
        st = e.get('start_timestamp') or e.get('start') or ''
        en = e.get('stop_timestamp') or e.get('end') or ''
        out.append({'title': title, 'desc': desc, 'start': str(st), 'end': str(en)})
    return out, 'ok'


def classify(rows):
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    cur = fut = 0
    real = 0
    newest = 0
    for r in rows:
        try:
            st = float(r['start'])
            en = float(r['end'])
        except Exception:
            continue
        newest = max(newest, en)
        if en >= now:
            fut += 1
            if st <= now:
                cur += 1
        if not GEN_TITLE.search(r['title']):
            real += 1
    return cur, fut, real, newest


def main():
    streams = json.load(open('/Users/shameez/workspace/epg/data/streams.json'))
    pk = [s for s in streams if re.match(r'^PK\s*\|', s.get('cat_name', ''))]
    lin = [s for s in pk if not is_non_linear(s.get('cat_name', ''), s.get('name', ''))]
    print(f'PK linear: {len(lin)}', file=sys.stderr)
    results = []
    n_short_impl = n_simple_impl = 0
    for i, s in enumerate(sorted(lin, key=lambda x: x['name'])):
        sid = s['stream_id']
        row = {'stream_id': sid, 'name': s['name'], 'cat': s['cat_name'],
               'epg_channel_id': s.get('epg_channel_id', '')}
        for action in ['get_short_epg', 'get_simple_data_table']:
            key = action
            try:
                status, body = q(action, sid)
                rows, parse = parse_rows(body, action)
                if rows or '<html' in body.lower():
                    if action == 'get_short_epg':
                        n_short_impl += 1
                    else:
                        n_simple_impl += 1
                cur, fut, real, newest = classify(rows)
                row[key] = {'status': status, 'rows': len(rows), 'parse': parse,
                            'current': cur, 'future': fut, 'real_titles': real,
                            'newest': int(newest), 'sample': [r['title'] for r in rows[:4]]}
            except Exception as e:
                row[key] = {'error': f'{type(e).__name__}: {e}'[:120]}
        results.append(row)
        if (i + 1) % 20 == 0:
            print(f'  {i + 1}/{len(lin)} done', file=sys.stderr)
        time.sleep(0.25)
    json.dump(results, open('/Users/shameez/workspace/epg/audit_output/xtream_perstream.json', 'w'),
              indent=1, ensure_ascii=False)
    print(f'short_epg implemented-ish: {n_short_impl}, simple_data_table: {n_simple_impl}',
          file=sys.stderr)


if __name__ == '__main__':
    main()
