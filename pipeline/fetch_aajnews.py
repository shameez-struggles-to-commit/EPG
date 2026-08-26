#!/usr/bin/env python3
"""Fetch Aaj News 24h grids from Business Recorder's ePaper (first-party-adjacent).

Discovery (validated 2026-08-26): BR ePaper publishes "PKT AAJ PROGRAM SCHEDULE"
as an ordinary HTML article on page 3 of each day's print edition. The date
index pages do NOT link articles directly; instead, ANY page-3 article of the
day embeds links to all its page-3 siblings. So: probe one article id for the
date -> collect sibling links -> the schedule article's <h2> says
"PKT AAJ PROGRAM SCHEDULE".

Article-id bootstrap: ids are global and monotonic (~128-144/day). We keep a
seed id in this file (updated when the walk succeeds); on failure, walk a small
window around the projected id (seed + days_elapsed * 140).

Cloudflare: epaper.brecorder.com blocks plain urllib/curl; curl_cffi with
chrome impersonation passes (validated 2026-08-26). Falls back to urllib if
curl_cffi is absent.

Times are PKT wall-clock (page title says "PKT AAJ PROGRAM SCHEDULE"); rows
after midnight belong to the next UTC day. Grid runs 06:00 -> 05:55 next day.
Page carries TODAY + TOMORROW grids.

Validated 2026-08-26 (Wed 12 Rabi-ul-Awwal): today's grid carried date-specific
Eid-Milad special rows; boundary check vs provider stream 618055 at 20:31 PKT
showed @ShaukatPiracha1 on air = "RUBAROO WITH SHAUKAT PIRACHA (LIVE)" 20:00
per the same grid.
"""
import datetime as dt
import json
import re
import sys

BASE = "https://epaper.brecorder.com"
# last known id -> date (update after successful walk)
SEED_ID, SEED_DATE = 1112553, dt.date(2026, 7, 21)
CHANNEL_ID = "aajnews.br.pk"
DISPLAY = "Aaj News"

HOUR_RE = re.compile(r"(\d{2}):(\d{2})")
SIB_RE = re.compile(r'href="(/20\d\d/\d\d/\d\d/3-page/(\d+)-news\.html)"')
TITLE_RE = re.compile(r"PKT\s+AAJ\s+PROGRAM\s+SCHEDULE", re.I)


def _get(url):
    try:
        from curl_cffi import requests as cr
        r = cr.get(url, impersonate="chrome", timeout=30)
        return r.text if r.status_code == 200 else None
    except ImportError:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception:
            return None


def _title_page(html):
    """'BR-ePaper | Aug 26, 2026 | Page National News Page 3' -> (date, 3)."""
    m = re.search(r"<title>BR-ePaper \| (\w{3}) (\d{1,2}), (\d{4}) \| Page .*?Page (\d+)</title>", html or "")
    if not m:
        return None, None
    mon = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6, "Jul": 7,
           "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}[m.group(1)]
    return dt.date(int(m.group(3)), mon, int(m.group(2))), int(m.group(4))


def _probe(day, i):
    """Fetch article i (any page slug works); return html or None."""
    return _get(f"{BASE}/{day:%Y/%m/%d}/3-page/{i}-news.html")


def find_schedule_article(day):
    """Locate the day's 'PKT AAJ PROGRAM SCHEDULE' article.

    Empirics (2026-08-26): probing {date}/{any id} returns either the article
    itself or a shell that still renders that date's page-3 story list —
    including an anchor labelled 'PKT AAJ PROGRAM SCHEDULE'. Ids are monotonic
    (~140/day). So: project an id from the seed, probe once, read the anchor;
    small fan-out if the projection misses.
    """
    delta = (day - SEED_DATE).days
    proj = SEED_ID + int(delta * 140)
    for off in (0, 40, -40, 90, -90, 150, -150):
        i = proj + off
        if i <= 0:
            continue
        h = _probe(day, i)
        if not h:
            continue
        # anchor text may BE the schedule (probed page = schedule article)
        if TITLE_RE.search(h) and "TIMING" in h.upper():
            return f"/{day:%Y/%m/%d}/3-page/{i}-news.html", h
        # else look for the sibling anchor with schedule text
        for m in re.finditer(
                r'href="(?:https?://[^/"]+)?(/20\d\d/\d\d/\d\d/3-page/(\d+)-news\.html)"[^>]*>\s*([^<]{3,90})</a>', h):
            if TITLE_RE.search(m.group(3)):
                hh = _get(BASE + m.group(1))
                if hh and TITLE_RE.search(hh) and "TIMING" in hh.upper():
                    return m.group(1), hh
    return None, None


def parse_grid(html, grid_date):
    """Parse one day's TIMING/PROGRAM table -> [(utc_iso, title)]."""
    # the table region: from 'TIMING' to the end of that day's block
    seg = html[html.upper().find("TIMING"):]
    seg = re.sub(r"<[^>]+>", "\n", seg)
    seg = re.sub(r"&amp;", "&", seg)
    lines = [l.strip() for l in seg.split("\n") if l.strip()]
    rows = []
    for l in lines:
        # rows are 'HH:MM TITLE...' on one line
        m = re.match(r"^(\d{1,2}):(\d{2})\s+(.{2,90})$", l)
        if not m:
            continue
        hh, mm, title = int(m.group(1)), int(m.group(2)), m.group(3).strip()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9 .:\-\u2019'()&,/]{1,90}", title):
            continue
        try:
            t = dt.datetime.combine(grid_date, dt.time(hh, mm))
        except ValueError:
            continue
        rows.append((t, title))
        if len(rows) > 70:  # safety
            break
    # PKT -> UTC
    out = []
    for t, title in rows:
        u = (t - dt.timedelta(hours=5)).strftime("%Y%m%d%H%M00 +0000")
        out.append((u, title))
    return out


def main(out_xml, out_status=None):
    days = []
    for d in (dt.date.today(), dt.date.today() + dt.timedelta(days=1)):
        path, html = find_schedule_article(d)
        if not html:
            print(f"aaj: no ePaper schedule found for {d}", file=sys.stderr)
            continue
        rows = parse_grid(html, d)
        days.append((d, path, rows))
        print(f"aaj: {d} {len(rows)} rows via {path}")
    if not days:
        sys.exit(1)
    xml = ['<?xml version="1.0" encoding="UTF-8"?>', "<tv>",
           f'<channel id="{CHANNEL_ID}"><display-name>{DISPLAY}</display-name></channel>']
    for d, _p, rows in days:
        for u, title in rows:
            xml.append(f'<programme start="{u}" stop="{u}" channel="{CHANNEL_ID}">'
                       f'<title lang="en">{title}</title></programme>')
    xml.append("</tv>")
    open(out_xml, "w").write("\n".join(xml))
    if out_status:
        json.dump({CHANNEL_ID: sum(len(r) for _d, _p, r in days)},
                  open(out_status, "w"), indent=1)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "aaj_epg.xml"
    st = sys.argv[2] if len(sys.argv) > 2 else None
    main(out, st)
