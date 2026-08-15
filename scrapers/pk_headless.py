#!/usr/bin/env python3
"""Headless-browser scraper: ARY Zindagi (/schedule) — JS-rendered.

ARY Zindagi's schedule page is built client-side (curl sees an empty shell;
2026-08-15 probe: 33 time slots render after JS execution). This scraper uses
Playwright to render the page and parse the day panes.

DOM (verified 2026-08-15):
  <div class="tab-pane ..." id="Monday">  (one pane per day, all in DOM)
    <div class="Extra cursor-pointer">
      <img src=".../api/images/xxx.jpg" alt="Salam Zindagi">
      <div class="scheduleContent"><p class="timings">09:00 AM</p></div>
    </div>
  </div>

Titles come from the img alt; times from .timings (12h clock). Durations are
not published — assume 1h (same convention as the hum.tv scraper).

Runs in GitHub Actions (workflow installs playwright + chromium). When
Playwright is unavailable locally, the scraper reports a clear SKIP status
rather than failing the PK scrape step.

Usage: pk_headless.py <out.json>   (channel key: ary_zindagi_pk)
"""

import datetime as dt
import json
import os
import re
import sys

DAYS = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
URL = 'https://aryzindagi.tv/schedule'
# matches hum.tv scraper's Asia/Karachi assumption
KARACHI = dt.timezone(dt.timedelta(hours=5))


def to_utc(date_str, hhmm):
    """'2026-08-17' + '09:00 AM' -> UTC ISO string."""
    m = re.match(r'^(\d{1,2}):(\d{2})\s*(AM|PM)$', hhmm, re.I)
    if not m:
        raise ValueError(hhmm)
    hh, mm = int(m.group(1)), int(m.group(2))
    ap = m.group(3).upper()
    if ap == 'PM' and hh != 12:
        hh += 12
    elif ap == 'AM' and hh == 12:
        hh = 0
    local = dt.datetime.fromisoformat(date_str).replace(tzinfo=KARACHI) + \
        dt.timedelta(hours=hh, minutes=mm)
    return local.astimezone(dt.timezone.utc).isoformat()


def next_dow_date(dow):
    """Date of the next occurrence of weekday dow (0=Monday) from today."""
    today = dt.date.today()
    delta = (dow - today.weekday()) % 7
    if delta == 0:
        delta = 7  # next week's instance for today's weekday
    return (today + dt.timedelta(days=delta)).isoformat()


def scrape():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError('playwright not installed (SKIP: headless scraper '
                           'runs in CI only)')

    exe = os.environ.get('CHROMIUM_EXECUTABLE')
    progs = []
    with sync_playwright() as p:
        kw = {'headless': True}
        if exe:
            kw['executable_path'] = exe
        b = p.chromium.launch(**kw)
        try:
            pg = b.new_context(
                user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                           'AppleWebKit/537.36 (KHTML, like Gecko) '
                           'Chrome/126.0 Safari/537.36').new_page()
            pg.goto(URL, wait_until='domcontentloaded', timeout=45000)
            pg.wait_for_timeout(5000)
            for di, day in enumerate(DAYS):
                # pane may be absent for some day
                pane = pg.query_selector(f'#{day.capitalize()}')
                if not pane:
                    continue
                date_s = next_dow_date(di)
                items = pane.query_selector_all('.Extra.cursor-pointer')
                for it in items:
                    img = it.query_selector('img')
                    t = it.query_selector('.timings')
                    if not img or not t:
                        continue
                    title = (img.get_attribute('alt') or '').strip()
                    tm = t.text_content().strip()
                    if not title or not re.match(r'^\d{1,2}:\d{2}\s*(AM|PM)$', tm, re.I):
                        continue
                    start = to_utc(date_s, tm)
                    stop = (dt.datetime.fromisoformat(start) +
                            dt.timedelta(hours=1)).isoformat()
                    progs.append((title, start, stop))
        finally:
            b.close()
    return progs


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else '/tmp/pk_headless.json'
    try:
        progs = scrape()
        out = {'ary_zindagi_pk': [
            {'title': t, 'start': s, 'stop': e} for t, s, e in progs]}
        json.dump(out, open(out_path, 'w'), indent=1)
        print(json.dumps({c: len(p) for c, p in out.items()}))
        return 0 if progs else 1
    except RuntimeError as e:
        print(f'[SKIP] {e}', file=sys.stderr)
        # write empty result so the workflow merge doesn't crash
        json.dump({'ary_zindagi_pk': []}, open(out_path, 'w'))
        return 0
    except Exception as e:  # noqa: BLE001
        print(f'[FAIL] aryzindagi.tv: {e}', file=sys.stderr)
        json.dump({'ary_zindagi_pk': []}, open(out_path, 'w'))
        return 1


if __name__ == '__main__':
    sys.exit(main())
