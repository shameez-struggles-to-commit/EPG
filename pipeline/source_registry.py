#!/usr/bin/env python3
"""Load and query the single EPG source registry."""

import argparse
import json
import os
import pathlib
import re
import xml.etree.ElementTree as ET


DEFAULT_REGISTRY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config",
    "sources.json",
)


def load_source_registry(path=DEFAULT_REGISTRY):
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if data.get("schema_version") != 1:
        raise ValueError("unsupported source registry schema")
    sources = data.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("source registry must contain active sources")
    required = {
        "name", "kind", "site", "countries", "filtered", "policy",
        "min_programmes", "output",
    }
    for source in sources:
        missing = required - set(source)
        if missing:
            raise ValueError("source %r missing %s" % (source.get("name"), sorted(missing)))
        if source["kind"] != "iptv-org":
            raise ValueError("unsupported source kind: %r" % source["kind"])
        if source["policy"] not in {"required", "optional"}:
            raise ValueError("invalid source policy: %r" % source["policy"])
        for key in ("name", "site"):
            if not isinstance(source[key], str) or not re.fullmatch(r"[A-Za-z0-9._-]+", source[key]):
                raise ValueError("%s must be a safe non-empty identifier" % key)
        if not isinstance(source["filtered"], bool):
            raise ValueError("filtered must be a boolean")
        if (not isinstance(source["countries"], list) or not source["countries"]
                or not all(isinstance(x, str) and re.fullmatch(r"[A-Z]{2,3}", x)
                           for x in source["countries"])):
            raise ValueError("countries must be a non-empty country-code list")
        if type(source["min_programmes"]) is not int or source["min_programmes"] < 1:
            raise ValueError("min_programmes must be an integer of at least 1")
        output = pathlib.PurePath(source["output"])
        if (output.name != source["output"]
                or not re.fullmatch(r"io_[A-Za-z0-9._-]+\.xml", source["output"])):
            raise ValueError("output must be a safe io_*.xml filename")
    for field in ("name", "site", "output"):
        values = [source[field] for source in sources]
        if len(values) != len(set(values)):
            raise ValueError("source registry contains duplicate %s values" % field)
    return data


def iptv_org_sources(path=DEFAULT_REGISTRY, filtered=None):
    sources = [
        source
        for source in load_source_registry(path)["sources"]
        if source["kind"] == "iptv-org"
    ]
    if filtered is not None:
        sources = [source for source in sources if source["filtered"] is filtered]
    return sources


def iptv_org_countries(path=DEFAULT_REGISTRY):
    return {
        source["name"]: set(source["countries"])
        for source in iptv_org_sources(path)
    }


def validate_iptv_org_outputs(data_dir, path=DEFAULT_REGISTRY):
    data_dir = pathlib.Path(data_dir)
    statuses = []
    failures = []
    for source in iptv_org_sources(path):
        output = data_dir / source["output"]
        programmes = 0
        reason = "ok"
        if not output.is_file() or output.stat().st_size == 0:
            reason = "missing_or_empty"
        else:
            try:
                root = ET.parse(output).getroot()
                if root.tag != "tv":
                    raise ValueError("root element is not tv")
                programmes = sum(1 for _ in root.iter("programme"))
                if programmes < source["min_programmes"]:
                    reason = "too_few_programmes"
            except (ET.ParseError, ValueError):
                reason = "invalid_xmltv"
        usable = reason == "ok"
        fatal = not usable and source["policy"] == "required"
        status = "ok" if usable else "failed" if fatal else "degraded"
        row = {
            "name": source["name"],
            "output": source["output"],
            "policy": source["policy"],
            "status": status,
            "reason": reason,
            "programmes": programmes,
            "usable": usable,
        }
        statuses.append(row)
        if fatal:
            failures.append(row)
    return statuses, failures


def registry_files(prefix="data", path=DEFAULT_REGISTRY, usable_only=False):
    sources = iptv_org_sources(path)
    if usable_only:
        statuses, _ = validate_iptv_org_outputs(prefix, path)
        usable = {row["output"] for row in statuses if row["usable"]}
        sources = [source for source in sources if source["output"] in usable]
    return [
        os.path.join(prefix, source["output"])
        for source in sources
    ]


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=("list-iptv-org", "outputs", "files", "validate-outputs"),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--filtered", action="store_true")
    mode.add_argument("--unfiltered", action="store_true")
    parser.add_argument("--prefix", default="data")
    parser.add_argument("--pairs", action="store_true")
    parser.add_argument("--usable-only", action="store_true")
    parser.add_argument("--status-out")
    parser.add_argument("--registry", default=DEFAULT_REGISTRY)
    args = parser.parse_args(argv)

    if args.command == "validate-outputs":
        statuses, failures = validate_iptv_org_outputs(args.prefix, args.registry)
        payload = {"sources": statuses, "required_failures": len(failures)}
        text = json.dumps(payload, indent=2, sort_keys=True)
        if args.status_out:
            pathlib.Path(args.status_out).write_text(text + "\n", encoding="utf-8")
        print(text)
        return 1 if failures else 0

    filtered = True if args.filtered else False if args.unfiltered else None
    sources = iptv_org_sources(args.registry, filtered=filtered)
    if args.command == "list-iptv-org":
        if args.pairs:
            print("\n".join(
                "%s|%s|%s|%s" % (
                    source["site"], source["output"], source["policy"],
                    source["min_programmes"]
                )
                for source in sources
            ))
        else:
            print(" ".join(source["site"] for source in sources))
    elif args.command == "outputs":
        print(" ".join(source["output"] for source in sources))
    else:
        print(",".join(registry_files(
            args.prefix, args.registry, usable_only=args.usable_only
        )))


if __name__ == "__main__":
    raise SystemExit(main())
