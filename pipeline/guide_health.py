#!/usr/bin/env python3
"""Inspect a built XMLTV guide and compare it with the deployed guide."""

import argparse
import datetime as dt
import gzip
import json
import re
import xml.etree.ElementTree as ET


ABSOLUTE_MINIMUMS = {
    "channels": 1500,
    "programmes": 100000,
    "channels_next_24h": 2000,
}

THRESHOLDS = {
    "channels": 0.95,
    "programmes": 0.80,
    "channels_next_24h": 0.90,
}


def parse_xmltv_time(value):
    match = re.match(r"^(\d{14})\s*([+-]\d{4})?$", value or "")
    if not match:
        raise ValueError("invalid XMLTV timestamp: %r" % value)
    base = dt.datetime.strptime(match.group(1), "%Y%m%d%H%M%S")
    offset = match.group(2) or "+0000"
    sign = 1 if offset[0] == "+" else -1
    delta = dt.timedelta(hours=int(offset[1:3]), minutes=int(offset[3:5]))
    return (base - sign * delta).replace(tzinfo=dt.timezone.utc)


# Only explicit unknown-programme labels are excluded from *usable* hours;
# news, shopping and short genuine programme titles remain valid.
UNKNOWN_TITLES = {"to be announced", "tba", "no information", "no programme information",
                  "no program information", "no match", "no data"}


def union_hours(intervals):
    """Coalesce overlapping and adjacent UTC intervals, without double-counting."""
    total = dt.timedelta()
    end = None
    for start, stop in sorted(intervals):
        if end is None or start >= end:
            total += stop - start
            end = stop
        elif stop > end:
            total += stop - end
            end = stop
    return total.total_seconds() / 3600


def measure_guide(path, now=None):
    """Return legacy guide counts and per-ID structural horizon evidence.

    Per-ID results are private caller data; never put them in a public artifact.
    A published ID is a declared XMLTV channel, not merely a programme ref.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        raise ValueError('now must be timezone-aware')
    now = now.astimezone(dt.timezone.utc)
    intervals = {}
    active = set()
    future = set()
    intersecting = set()
    channels = set()
    channel_count = programme_count = 0
    # Iterparse avoids retaining the full ~200k-row guide tree.
    with gzip.open(path, 'rb') if str(path).endswith('.gz') else open(path, 'rb') as handle:
        for _, element in ET.iterparse(handle, events=('end',)):
            if element.tag == 'channel':
                channel_count += 1
                if element.get('id'):
                    channels.add(element.get('id'))
            elif element.tag == 'programme':
                programme_count += 1
                cid = element.get('channel')
                if cid:
                    try:
                        start = parse_xmltv_time(element.get('start'))
                        stop = parse_xmltv_time(element.get('stop'))
                    except ValueError:
                        pass
                    else:
                        if stop > start:
                            if start <= now < stop:
                                active.add(cid)
                            if stop > now:
                                future.add(cid)
                            if stop > now and start < now + dt.timedelta(hours=24):
                                intersecting.add(cid)
                            title = element.findtext('title')
                            if title and title.strip() and title.strip().casefold() not in UNKNOWN_TITLES:
                                for hours in (24, 48):
                                    a = max(start, now)
                                    b = min(stop, now + dt.timedelta(hours=hours))
                                    if a < b:
                                        intervals.setdefault(cid, {}).setdefault(hours, []).append((a, b))
            if element.tag in ('channel', 'programme'):
                element.clear()
    per_id = {cid: {
        'active': cid in active,
        'future': cid in future,
        'usable_hours_next_24h': union_hours(intervals.get(cid, {}).get(24, [])),
        'usable_hours_next_48h': union_hours(intervals.get(cid, {}).get(48, [])),
    } for cid in channels}
    health = {
        'channels': channel_count,
        'programmes': programme_count,
        'channels_next_24h': len(intersecting & channels),  # existing deployment gate
        'active_channels': len(active & channels),
        'future_channels': len(future & channels),
        'usable_hours_next_24h': sum(v['usable_hours_next_24h'] for v in per_id.values()),
        'usable_hours_next_48h': sum(v['usable_hours_next_48h'] for v in per_id.values()),
    }
    return health, per_id


def inspect_guide(path, now=None):
    return measure_guide(path, now)[0]


def compare_guides(candidate, previous, thresholds=None, absolute_minimums=None):
    thresholds = thresholds or THRESHOLDS
    absolute_minimums = ABSOLUTE_MINIMUMS if absolute_minimums is None else absolute_minimums
    failures = []
    for metric, minimum in absolute_minimums.items():
        value = candidate.get(metric, 0)
        if value < minimum:
            failures.append("%s is %s (absolute minimum %s)" % (
                metric.replace("channels_next_24h", "24h channels"), value, minimum
            ))
    for metric, ratio in thresholds.items():
        old = previous.get(metric, 0)
        new = candidate.get(metric, 0)
        if old and new < old * ratio:
            failures.append(
                "%s dropped to %s from %s (minimum %.0f%%)"
                % (metric.replace("channels_next_24h", "24h channels"), new, old, ratio * 100)
            )
    return failures


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("candidate")
    parser.add_argument("--previous")
    parser.add_argument("--json-out")
    args = parser.parse_args(argv)

    candidate = inspect_guide(args.candidate)
    payload = {"candidate": candidate, "failures": []}
    if args.previous:
        previous = inspect_guide(args.previous)
        payload["previous"] = previous
        payload["failures"] = compare_guides(candidate, previous)
    text = json.dumps(payload, indent=2, sort_keys=True)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    print(text)
    return 1 if payload["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

