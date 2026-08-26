#!/usr/bin/env python3
"""Fetch Madani Channel English schedule from Dawat-e-Islami (first-party).

URL: https://websites.dawateislami.net/html/fpc_eng/fpc.php
Server-rendered table, dated (e.g. "Wednesday 26th Aug 2026"), one full day,
31 rows, GMT + Pak Time columns, episode numbers, Live/Fresh/Repeat flags.
Updates daily (page header shows current Islamic + Gregorian date).

Times: we parse the PKT column (wall-clock Pakistan). Rows past midnight roll
to the next day. Horizon: today only -> refresh daily; yesterday's late rows
are dropped by the pipeline currency gate anyway.

Identity validated 2026-08-26: provider stream 618112 at 20:22 PKT showed
"Prophetic Radiance" on-air + English "Madani Channel" watermark; the same
schedule lists Prophetic Radiance Ep#10 (Live) 19:30-20:45 PKT that day.
"""
import datetime as dt
import json
import re
import sys
import urllib.request

URL = "https://websites.dawateislami.net/html/fpc_eng/fpc.php"
CHANNEL_ID = "madanieng.di.pk"
DISPLAY = "Madani Channel English"
DATE_RE = re.compile(r"((?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day),?\s+(\d{1,2})(?:st|nd|rd|th)\s+([A-Z][a-z]+)\s+(20\d\d)")
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*(AM|PM)", re.I)


def fetch_html():
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "ignore")


def parse(html):
    """-> (page_date, [(pkt_datetime, title)]) using the Pak Time column."""
    # rows look like: <td>N</td><td>12:00 AM</td><td>5:00 AM</td><td>TITLE ...</td>
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S)
    page_date = None
    out = []
    for row in rows:
        cells = [re.sub(r"<[^>]+>", "", c).strip()
                 for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)]
        if not cells:
            continue
        joined = " ".join(cells)
        m = DATE_RE.search(joined)
        if m and not page_date:
            months = {name: i for i, name in enumerate(
                ["January", "February", "March", "April", "May", "June", "July",
                 "August", "September", "October", "November", "December"], 1)}
            mon = next((v for k, v in months.items() if k.startswith(m.group(3)[:3])), None)
            if mon:
                try:
                    page_date = dt.date(int(m.group(4)), mon, int(m.group(2)))
                except ValueError:
                    pass
        # need >= 3 cells with two time-like values; take the SECOND time (Pak)
        times = TIME_RE.findall(joined)
        if len(times) >= 2 and page_date:
            hh, mm, ap = times[1]
            hh = int(hh) % 12 + (12 if ap.upper() == "PM" else 0)
            t = dt.datetime.combine(page_date, dt.time(hh, int(mm)))
            # title = longest non-time cell
            title = max((c for c in cells if not TIME_RE.fullmatch(c.strip())
                         and not c.strip().isdigit()), key=len, default="").strip()
            title = re.sub(r"\s+", " ", title)
            if title:
                out.append((t, title[:100]))
    # sort & dedupe
    out.sort(key=lambda x: x[0])
    return page_date, out


def main(out_xml, out_status=None):
    html = fetch_html()
    page_date, rows = parse(html)
    if not page_date or not rows:
        print("madani-eng: parse failed", file=sys.stderr)
        sys.exit(1)
    stale = (dt.date.today() - page_date).days
    print(f"madani-eng: page {page_date} ({stale}d old), {len(rows)} rows")
    xml = ['<?xml version="1.0" encoding="UTF-8"?>', "<tv>",
           f'<channel id="{CHANNEL_ID}"><display-name>{DISPLAY}</display-name></channel>']
    for t, title in rows:
        u = (t - dt.timedelta(hours=5)).strftime("%Y%m%d%H%M00 +0000")  # PKT->UTC
        xml.append(f'<programme start="{u}" stop="{u}" channel="{CHANNEL_ID}">'
                   f'<title lang="en">{title}</title></programme>')
    xml.append("</tv>")
    open(out_xml, "w").write("\n".join(xml))
    if out_status:
        json.dump({CHANNEL_ID: len(rows), "page_date": str(page_date)},
                  open(out_status, "w"), indent=1)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "madani_epg.xml"
    st = sys.argv[2] if len(sys.argv) > 2 else None
    main(out, st)
