#!/usr/bin/env python3
"""Fetch Pakistani-channel EPG from Sling's public CMS endpoints.

Two endpoints (both anonymous, no auth):
  1. Channel inventory:  {CMS}/cms/publish3/domain/summary/ums/1.json
  2. Per-channel 24h guide: {CMS}/cms/publish3/channel/schedule/24/{yymmddHHMM}/1/{guid}.json
     -> schedule.scheduleList[] with schedule_start/schedule_stop (epoch
        STRINGS), title, grid_title, duration (secs).

The paid/free wall applies to streams only; guide metadata is public.
Validated 2026-08-25: anonymous access, real titles, UTC epochs.

Output: XMLTV file + JSON status summary. Only channels in PK_GUIDS are
fetched (curated; the 1,552-channel summary is fetched once to resolve
call signs -> guid).
"""
import datetime as dt
import json
import os
import sys
import urllib.request

CMS = "https://cbd46b77.cdn.cms.movetv.com"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"
HEADERS = {"User-Agent": UA, "accept": "application/json",
           "origin": "https://watch.sling.com", "referer": "https://watch.sling.com/"}

# Curated: callsign -> (xmltv id, display name, note)
PK_CHANNELS = {
    "HUMST-F":   ("humsitaray.sling.pk", "Hum Sitaray", "net-new"),
    "HUMMA-F":   ("hummasala.sling.pk", "HUM Masala", "correction: real cooking grid"),
    "TVONEGL-F": ("tvoneglobal.sling.pk", "TV One Global", "net-new"),
    "QTVPAK-F":  ("aryqtv.sling.pk", "ARY QTV", "cross-check/correction"),
    "GEOTV-F":   ("geotv.sling.pk", "Geo TV", "cross-check"),
    "GEONWS-F":  ("geonews.sling.pk", "Geo News", "cross-check"),
    "ARYDI-F":   ("arydigital.sling.pk", "ARY Digital", "cross-check"),
    "ARYNEWS-F": ("arynews.sling.pk", "ARY News", "cross-check"),
    "HUMTV-F":   ("humtv.sling.pk", "HUM TV", "cross-check"),
    "HUMNW-F":   ("humnews.sling.pk", "HUM News", "cross-check"),
}


def get_json(url, timeout=30):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def resolve_callsigns():
    """summary -> {callsign: guid}; falls back to PK_CHANNELS keys."""
    out = {}
    try:
        d = get_json(f"{CMS}/cms/publish3/domain/summary/ums/1.json", timeout=60)
        for c in d.get("channels") or []:
            t = (c.get("title") or "").upper()
            g = c.get("channel_guid")
            if t and g:
                out.setdefault(t, g)
    except Exception as e:
        print(f"WARN: summary fetch failed ({e}); using known guids", file=sys.stderr)
    known = {
        "HUMST-F": "d563a4c2a9864bef8581cc8dfb9d0781",
        "HUMMA-F": "a1e19d148289430c93b448768ab2f04c",
        "TVONEGL-F": "d68d89e6fd034e3385c36af881631557",
        "QTVPAK-F": "3186d3ba7c244d869ae482efbbde634e",
        "GEOTV-F": "f7aa32b64f3a4d29a0589d8f48dfaaf0",
        "GEONWS-F": "7bcb3d81007c433cb3489bdc11c63baf",
        "ARYDI-F": "1d45b2bfa7884a1bb5111817f33d63ef",
        "ARYNEWS-F": "c1a0a6cf35f04469aea5ab5e61c920b0",
        "HUMTV-F": "cf60062cac484bc1b3d26fd4c5a938ac",
        "HUMNW-F": "10c6bb51b7174440a12d7c36e52582f3",
    }
    for k, v in known.items():
        out.setdefault(k, v)
    return out


def fetch_channel(guid):
    stamp = dt.datetime.utcnow().strftime("%y%m%d%H%M")
    d = get_json(f"{CMS}/cms/publish3/channel/schedule/24/{stamp}/1/{guid}.json")
    sch = d.get("schedule") or {}
    rows = sch.get("scheduleList") or []
    return sch.get("title") or "", rows


def fmt(ts):
    return dt.datetime.utcfromtimestamp(int(ts)).strftime("%Y%m%d%H%M00 +0000")


def main(out_xml, out_status):
    guids = resolve_callsigns()
    blocks, status = [], {}
    for callsign, (xid, disp, note) in PK_CHANNELS.items():
        guid = guids.get(callsign)
        if not guid:
            status[callsign] = {"status": "no-guid"}
            continue
        try:
            title, rows = fetch_channel(guid)
        except Exception as e:
            status[callsign] = {"status": "error", "error": str(e)[:120]}
            continue
        rows = [r for r in rows if r.get("title") and "announce" not in str(r.get("title", "")).lower()]
        for r in rows:
            st, sp = r.get("schedule_start"), r.get("schedule_stop")
            if not st or not sp or int(sp) <= int(st):
                continue
            blocks.append(
                f'<programme start="{fmt(st)}" stop="{fmt(sp)}" channel="{xid}">'
                f'<title lang="en">{esc(str(r.get("title")))}</title></programme>')
        status[callsign] = {"status": "ok", "title": title, "programmes": len(rows),
                            "guid": guid, "note": note}
        print(f"{callsign}: {title!r} {len(rows)} rows")
    xml = ['<?xml version="1.0" encoding="UTF-8"?>', "<tv>"]
    for callsign, (xid, disp, note) in PK_CHANNELS.items():
        if status.get(callsign, {}).get("programmes", 0) > 0:
            xml.append(f'<channel id="{xid}"><display-name>{esc(disp)}</display-name></channel>')
    xml += blocks + ["</tv>"]
    with open(out_xml, "w") as f:
        f.write("\n".join(xml))
    with open(out_status, "w") as f:
        json.dump(status, f, indent=1)
    ok = sum(1 for v in status.values() if v.get("status") == "ok" and v.get("programmes", 0) > 0)
    print(f"sling: {ok}/{len(PK_CHANNELS)} channels with programmes -> {out_xml}")


def esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


if __name__ == "__main__":
    out_xml = sys.argv[1] if len(sys.argv) > 1 else "data/sling.xml"
    out_status = sys.argv[2] if len(sys.argv) > 2 else "data/sling_status.json"
    main(out_xml, out_status)
