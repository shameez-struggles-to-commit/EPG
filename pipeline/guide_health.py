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


def inspect_guide(path, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    horizon = now + dt.timedelta(hours=24)
    with gzip.open(path, "rt", encoding="utf-8", errors="strict") as handle:
        root = ET.parse(handle).getroot()
    channels = root.findall("channel")
    channel_ids = {channel.get("id") for channel in channels if channel.get("id")}
    programmes = root.findall("programme")
    current_channels = set()
    for programme in programmes:
        try:
            start = parse_xmltv_time(programme.get("start"))
            stop = parse_xmltv_time(programme.get("stop"))
        except ValueError:
            continue
        channel_id = programme.get("channel")
        if stop > now and start < horizon and channel_id in channel_ids:
            current_channels.add(channel_id)
    return {
        "channels": len(channels),
        "programmes": len(programmes),
        "channels_next_24h": len(current_channels),
    }


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

