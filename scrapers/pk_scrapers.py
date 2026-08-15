#!/usr/bin/env python3
"""Pakistani broadcaster EPG scrapers → XMLTV.

Sources (verified live 2026-08-14):
  - harpalgeo.tv (Geo Entertainment)  — 7 day-tabs, hourly slots
  - geokahani.tv (Geo Kahani)         — same CMS as harpalgeo
  - hum.tv (Hum TV Asia)              — WordPress, schedule in day sections
  - arydigital.tv (ARY Digital)       — day-panel divs id="mon".."sun"

Each scraper returns: {channel_key: [(title, start_dt, end_dt), ...]}
Times are converted to UTC datetimes (source tz: Asia/Karachi).
"""
import re
import html as HTML
import datetime as dt
import zoneinfo
import urllib.request
import json
import sys

KHI = zoneinfo.ZoneInfo('Asia/Karachi')
UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'}

DAYS = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']


def fetch(url, timeout=45):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('utf-8', errors='ignore')


def to_utc(date_str, hhmm):
    """date_str 'YYYY-MM-DD' + 'HH:MM' in Karachi time → aware UTC datetime."""
    y, m, d = map(int, date_str.split('-'))
    hh, mm = map(int, hhmm.split(':'))
    return dt.datetime(y, m, d, hh, mm, tzinfo=KHI).astimezone(dt.timezone.utc)


def next_dow_date(dow_idx, today=None):
    """Date of the given weekday (0=Mon) on/after today (Karachi)."""
    t = today or dt.datetime.now(KHI).date()
    return t + dt.timedelta(days=(dow_idx - t.weekday()) % 7)


def parse12(s):
    """'12:00 am' / '1:30 pm' -> 'HH:MM' (24h). Returns None on failure."""
    m = re.match(r'(\d{1,2}):(\d{2})\s*(am|pm)', s.strip(), re.I)
    if not m:
        return None
    hh, mm, ap = int(m.group(1)), int(m.group(2)), m.group(3).lower()
    if ap == 'pm' and hh != 12:
        hh += 12
    if ap == 'am' and hh == 12:
        hh = 0
    return f'{hh:02d}:{mm:02d}'


# ---------------------------------------------------------------- Geo family
def _scrape_geo_like(base_url):
    """harpalgeo.tv / geokahani.tv tv-schedule page: <div id="tab0N"> day blocks,
    each entry: <span>HH:MM - HH:MM</span> ... title="Show Name"."""
    h = fetch(base_url)
    parts = re.split(r'<div id="tab0(\d)"', h)
    progs = []
    for k in range(1, len(parts) - 1, 2):
        daynum = int(parts[k])          # tab01=Monday ... tab07=Sunday
        content = parts[k + 1]
        date = next_dow_date(daynum - 1)
        # Geo slots are ONE continuous 12-hour-notation sequence per day:
        # 12:00-01:00 (midnight) ... 11:30-12:30 (noon) ... 12:30-01:30 (pm) ... 11:00-11:59
        # Parse with a running clock: hour h means (h % 12); pick the smallest
        # 12-hour wrap that keeps the slot at/after the previous end.
        spans = list(re.finditer(r'<span>(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})</span>', content))
        prev_end = 0  # minutes since midnight
        for idx, m in enumerate(spans):
            lo = m.end()
            hi = spans[idx + 1].start() if idx + 1 < len(spans) else len(content)
            t = re.search(r'title="([^"]+)"', content[lo:hi])
            if not t:
                continue
            sh, sm, eh, em = (int(m.group(i)) for i in (1, 2, 3, 4))
            sb = (sh % 12) * 60 + sm
            eb = (eh % 12) * 60 + em
            s_abs = sb
            while s_abs < prev_end - 15:
                s_abs += 720
            e_abs = eb
            while e_abs < s_abs:
                e_abs += 720
            prev_end = e_abs
            title = HTML.unescape(t.group(1)).strip()
            title = re.sub(r'\s*\((?:Repeat|New)\)\s*$', '', title, flags=re.I)
            try:
                start = to_utc(date.isoformat(), f'{s_abs // 60:02d}:{s_abs % 60:02d}')
                end = to_utc(date.isoformat(), f'{e_abs // 60:02d}:{e_abs % 60:02d}')
            except ValueError:
                continue
            progs.append((title, start, end))
    return progs


def scrape_harpalgeo():
    return {'geo_entertainment_pk': _scrape_geo_like('https://harpalgeo.tv/tv-schedule/')}


def scrape_geokahani():
    """geokahani.tv/program-guide — day h2 headers + showtime-schedule <li> items:
    <h2>Show Name (Rpt)</h2> ... <strong>12:00 am</strong> - <strong>12:59 am</strong>."""
    h = fetch('https://geokahani.tv/program-guide/')
    daymap = {'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
              'friday': 4, 'saturday': 5, 'sunday': 6}
    progs = []
    sections = re.split(
        r'<h2[^>]*>\s*(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*</h2>', h)
    for k in range(1, len(sections) - 1, 2):
        day = sections[k].lower()
        if day not in daymap:
            continue
        content = sections[k + 1]
        date = next_dow_date(daymap[day])
        items = re.findall(
            r'<h2>([^<]+)</h2>.*?<strong>([^<]+)</strong>\s*-\s*<strong>([^<]+)</strong>',
            content, re.S)
        for title, t1, t2 in items:
            title = title.strip()
            s, e = parse12(t1), parse12(t2)
            if not title or not s or not e:
                continue
            start = to_utc(date.isoformat(), s)
            end = to_utc(date.isoformat(), e)
            if end <= start:
                end += dt.timedelta(days=1)
            progs.append((title, start, end))
    return {'geo_kahani_pk': progs}


def scrape_express_entertainment():
    """expressentertainment.tv/etv-schedule — schedule_check <day> divs, each
    drama block has <h4 class="tw-drama-name">TITLE</h4> +
    <h5 class="tw-drama-time">HH:MM</h5> (24h, Pakistan time)."""
    h = fetch('https://www.expressentertainment.tv/etv-schedule/')
    daymap = {'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
              'friday': 4, 'saturday': 5, 'sunday': 6}
    progs = []
    parts = re.split(r'schedule_check\s+(\w+)', h)
    for k in range(1, len(parts) - 1, 2):
        day = parts[k].lower().strip()
        if day not in daymap:
            continue
        content = parts[k + 1]
        date = next_dow_date(daymap[day])
        titles = re.findall(r'<h4 class="tw-drama-name">(.*?)</h4>', content, re.S)
        times = re.findall(r'<h5 class="tw-drama-time">\s*(\d{1,2}):(\d{2})\s*</h5>', content)
        for i, ((hh, mm), raw_title) in enumerate(zip(times, titles)):
            title = re.sub(r'<span.*?</span>', '', raw_title, flags=re.S).strip()
            if not title:
                continue
            start = to_utc(date.isoformat(), f'{hh}:{mm}')
            if i + 1 < len(times):
                nh, nm = times[i + 1]
                end = to_utc(date.isoformat(), f'{nh}:{nm}')
            else:
                end = start + dt.timedelta(hours=1)
            if end <= start:
                end += dt.timedelta(days=1)
            progs.append((title, start, end))
    return {'express_entertainment_pk': progs}


# ---------------------------------------------------------------- Hum TV
def _scrape_humtv(url):
    """hum.tv schedule pages — day sections id="monday".. with inner_time entries."""
    h = fetch(url)
    progs = []
    for di, day in enumerate(DAYS):
        m = re.search(rf'id="{day}"(.+?)(?=id="(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)"|</main>|<footer)', h, re.S)
        if not m:
            continue
        seg = m.group(1)
        date = next_dow_date(di)
        for e in re.finditer(r'<div class="inner_time"[^>]*>(?:<a href="https://hum\.tv/dramas/[^"]*">)?([^<]+)(?:</a>)?</div>', seg):
            back = seg[:e.start()][-2500:]
            title = None
            alts = re.findall(r'alt="([^"]{2,80})"', back)
            href_m = re.search(r'<a href="https://hum\.tv/dramas/([^"/]+)/', e.group(0))
            # prefer slug from the time-anchor itself; else nearest preceding drama link
            if href_m:
                title = href_m.group(1).replace('-', ' ').title()
            else:
                prev = re.findall(r'<a href="https://hum\.tv/dramas/([^"/]+)/', back)
                if prev:
                    title = prev[-1].replace('-', ' ').title()
                elif alts:
                    cand = re.sub(r'\s*\d+x\d+.*$', '', alts[-1]).strip()
                    # reject poster filenames like "528.jpg (4)"
                    if cand and not re.search(r'\.jpe?g|\(\d+\)$|^\d+', cand, re.I):
                        title = cand
            if not title:
                continue
            tm = re.match(r'\s*(\d{1,2}):(\d{2})\s*(?:&#8211;|–|&ndash;|-)?', HTML.unescape(e.group(1)))
            if not tm:
                continue
            rng = re.search(r'(\d{1,2}:\d{2})\s*(?:&#8211;|–|&ndash;|-)\s*(\d{1,2}:\d{2})', HTML.unescape(e.group(1)))
            try:
                start = to_utc(date.isoformat(), tm.group(1) + ':' + tm.group(2))
                if rng:
                    end = to_utc(date.isoformat(), rng.group(2))
                else:
                    end = start + dt.timedelta(hours=1)
            except ValueError:
                continue
            if end <= start:
                end += dt.timedelta(days=1)
            progs.append((title, start, end))
    return progs


def scrape_humtv():
    return {'hum_tv_pk': _scrape_humtv('https://hum.tv/schedule/')}


def scrape_humtv_europe():
    return {'hum_tv_europe_pk': _scrape_humtv('https://hum.tv/schedule-europe/')}


# ---------------------------------------------------------------- ARY Digital
def scrape_arydigital():
    """arydigital.tv/schedule — panels id="mon".."sun", schedule-item blocks."""
    h = fetch('https://arydigital.tv/schedule')
    progs = []
    for di, pid in enumerate(['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']):
        m = re.search(rf'<div class="schedule-day[^"]*"[^>]*id="{pid}"[^>]*>(.*?)(?=<div class="schedule-day[^"]*"[^>]*id="(?:mon|tue|wed|thu|fri|sat|sun)"|<footer|$)', h, re.S)
        if not m:
            continue
        seg = m.group(1)
        date = next_dow_date(di)
        for it in re.finditer(
                r'class="schedule-timeblock-time">([^<]+)<.*?class="schedule-timeblock-end">([^<]+)<.*?class="schedule-title">([^<]+)<', seg, re.S):
            t_start, t_end, title = it.group(1).strip(), it.group(2).strip(), HTML.unescape(it.group(3)).strip()
            def parse12(x):
                mm = re.match(r'(\d{1,2}):(\d{2})\s*(AM|PM)', x, re.I)
                if not mm:
                    return None
                hh, mnt, ap = int(mm.group(1)), int(mm.group(2)), mm.group(3).upper()
                if ap == 'PM' and hh != 12:
                    hh += 12
                if ap == 'AM' and hh == 12:
                    hh = 0
                return f'{hh:02d}:{mnt:02d}'
            s, e2 = parse12(t_start), parse12(t_end)
            if not s or not e2:
                continue
            try:
                start = to_utc(date.isoformat(), s)
                end = to_utc(date.isoformat(), e2)
            except ValueError:
                continue
            if end <= start:
                end += dt.timedelta(days=1)
            progs.append((title, start, end))
    return {'ary_digital_pk': progs}


SCRAPERS = {
    'harpalgeo.tv': scrape_harpalgeo,
    'geokahani.tv': scrape_geokahani,
    'hum.tv': scrape_humtv,
    'hum.tv/europe': scrape_humtv_europe,
    'arydigital.tv': scrape_arydigital,
    'expressentertainment.tv': scrape_express_entertainment,
}


def main():
    out = {}
    for name, fn in SCRAPERS.items():
        try:
            res = fn()
            for ch, progs in res.items():
                out[ch] = [{'title': t, 'start': s.isoformat(), 'stop': e.isoformat()} for t, s, e in progs]
            print(f'[ok] {name}: ' + ', '.join(f'{c}={len(p)}' for c, p in res.items()), file=sys.stderr)
        except Exception as ex:
            print(f'[FAIL] {name}: {ex}', file=sys.stderr)
    json.dump(out, open(sys.argv[1] if len(sys.argv) > 1 else '/tmp/pk_epg.json', 'w'), indent=1)
    print(json.dumps({c: len(p) for c, p in out.items()}))


if __name__ == '__main__':
    main()
