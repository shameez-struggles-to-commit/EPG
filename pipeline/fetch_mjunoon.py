#!/usr/bin/env python3
"""Fetch mjunoon.tv's REAL programme rows (partial grid, PK news channels).

mjunoon's POST /v2/tv-guide {day,date} serves 6 news channels whose rows are
~90% placeholder ("X News Headlines" q3h). This fetcher keeps ONLY the real
named shows (explicit pro_name != headlines pattern) with their explicit
start/end times — a partial first-party-ish grid, never filler. Under the
partial-source policy: these rows win only for their intervals; they must
not block other candidates and must not manufacture coverage.

Auth: bearer token from POST /v2/auth/login using the credentials the
site's own web bundle ships (same as the public Streamlink plugin).

Validated 2026-08-25: endpoint works from residential US; real rows for
Samaa/Abb Takk/News One with explicit times; horizon >= 7 days.
"""
import datetime as dt
import json
import sys
import urllib.request

API = "https://cdn2.mjunoon.tv:9191"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"
LOGIN = {"email": "rashid@convexinteractive.com", "password": "webapi123789"}

# slug -> (xmltv id, display name)
CHANNELS = {
    "samaa-tv-live": ("samaa.mjunoon.pk", "Samaa TV"),
    "abb-takk-news-live": ("abbtakk.mjunoon.pk", "Abb Takk"),
    "newsone-news-headlines": ("newsone.mjunoon.pk", "News One"),
    "waseb-live": ("waseb.mjunoon.pk", "Waseb TV"),
    "khyber-news-live": ("khybernews.mjunoon.pk", "Khyber News"),
    "ktn-news-live": ("ktnnews.mjunoon.pk", "KTN News"),
}
PLACEHOLDER = ("headline", "news headlines")


def post(path, body, token=None, timeout=25):
    h = {"User-Agent": UA, "Content-Type": "application/json",
         "Origin": "https://www.mjunoon.tv", "Referer": "https://www.mjunoon.tv/"}
    if token:
        h["Authorization"] = "bearer " + token
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(), headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def is_real(name):
    n = str(name or "").lower()
    return n and not any(p in n for p in PLACEHOLDER)


def main(out_xml, days=3, out_status=None):
    tok = post("/v2/auth/login", LOGIN).get("token")
    if not tok:
        print("mjunoon: login failed"); sys.exit(1)
    rows_all, status = [], {}
    today = dt.date.today()
    for i in range(days):
        d = today + dt.timedelta(days=i)
        j = post("/v2/tv-guide", {"day": d.strftime("%A"), "date": d.isoformat()}, tok)
        for grp in (j.get("data") or {}).get("episodes") or []:
            if not grp:
                continue
            slug = grp[0].get("slug")
            if slug not in CHANNELS:
                continue
            xid = CHANNELS[slug][0]
            for e in grp:
                if not is_real(e.get("pro_name")):
                    continue
                st, en = str(e.get("start_time", "")), str(e.get("end_time", ""))
                if not st or not en:
                    continue
                try:
                    s = dt.datetime.fromisoformat(f"{d.isoformat()}T{st}")
                    x = dt.datetime.fromisoformat(f"{d.isoformat()}T{en}")
                except ValueError:
                    continue
                if x <= s:
                    continue
                rows_all.append((slug, s, x, str(e.get("pro_name"))))
                status.setdefault(slug, 0)
                status[slug] += 1
    xml = ['<?xml version="1.0" encoding="UTF-8"?>', "<tv>"]
    have = {slug for slug, *_ in rows_all}
    for slug, (xid, disp) in CHANNELS.items():
        if slug in have:
            xml.append(f'<channel id="{xid}"><display-name>{disp}</display-name></channel>')
    for slug, s, x, name in rows_all:
        xml.append(f'<programme start="{s.strftime("%Y%m%d%H%M00 +0000")}" '
                   f'stop="{x.strftime("%Y%m%d%H%M00 +0000")}" channel="{CHANNELS[slug][0]}">'
                   f'<title lang="en">{esc(name)}</title></programme>')
    xml.append("</tv>")
    open(out_xml, "w").write("\n".join(xml))
    if out_status:
        json.dump({CHANNELS[k][0]: v for k, v in status.items()}, open(out_status, "w"), indent=1)
    print(f"mjunoon: {sum(status.values())} real rows across {len(status)} channels -> {out_xml}")
    for k, v in status.items():
        print(f"  {k}: {v}")


def esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "data/mjunoon.xml"
    st = sys.argv[2] if len(sys.argv) > 2 else "data/mjunoon_status.json"
    main(out, 3, st)
