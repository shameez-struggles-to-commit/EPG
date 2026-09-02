#!/usr/bin/env python3
"""Download third-party EPG source feeds and emit a manifest + name index.

Sources:
  - epg.pw global XMLTV (broad worldwide base)
  - epgshare01 country files (rich US-locals + EU + MENA coverage; WebGrab+Plus)
  - mitthu786/tvepg (India AIO)
  - al7omed/bein-epg (beIN MENA, self-updating via GitHub Actions)
  - (iptv-org grabs are run separately in the workflow via npm — they need
     the iptv-org/epg Node toolchain, not plain curl)

globetvapp/epg was REMOVED 2026-08-15: all 248 upstream feeds went stale
(programme data ends 2025-09..2025-12), including india2.

Outputs to <outdir>/:
  - epgpw_global.xml.gz
  - es_<NAME>.xml.gz            (one per epgshare01 file)
  - bein_mena.xml               (al7omed/bein-epg)
  - sources.json                manifest: [{source, file, kind}]
  - sources_index.json          {source: {norm_name: [channel_id, ...]}}
  - call_signs.json             {source: {callsign: [channel_id, ...]}}

Idempotent: skips files already present (non-empty). Set ESHARE_FILES to a
comma list to download a subset (e.g. for local dev).
"""

import gzip
import html
import json
import os
import re
import ssl
import sys
import time
import urllib.request
from collections import defaultdict
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from matcher import SourceIndex

UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'}

EPG_PW_URL = 'https://epg.pw/xmltv/epg.xml.gz'
ESHARE_BASE = 'https://epgshare01.online/epgshare01/epg_ripper_{}.xml.gz'
# mitthu786/tvepg — India OTT EPG (JioTV/TataPlay/Zee5/SonyLIV/SunNXT), one AIO file
TVEPG_URL = 'https://raw.githubusercontent.com/mitthu786/tvepg/main/epg.xml.gz'
# al7omed/bein-epg — beIN MENA sports, self-updating via GitHub Actions
BEIN_URL = 'https://raw.githubusercontent.com/al7omed/bein-epg/main/docs/guide.xml'
# Shadow-GR-Official/cyta.cy-epg — Cyprus CyTA platform incl. the NOVA bouquet
# (Novacinema 1-4, Novasports Start/Prime/1-6/Extra, Novalife). Verified
# 2026-08-19: 118/118 usable, current. Net-new vs the iptv-org cyta grabber
# (which yields only ~7 channels).
CYTA_URL = 'https://raw.githubusercontent.com/Shadow-GR-Official/cyta.cy-epg/refs/heads/main/data/epg.xml'
# chrisliatas/greek-xmltv — Digea DTT (all regions) + ERT, daily GitHub
# release. Verified 2026-08-19: 101/101 usable, current. Greek-script names
# (matcher handles via gr_translit in build_mapping epilogue).
GREEK_URL = 'https://github.com/chrisliatas/greek-xmltv/releases/latest/download/xmltv_GREECE_el.xml.gz'
# i.mjh.nz PlutoTV US — FAST loop channels (24/7 family): ~15 of our 24/7
# streams have real episode schedules here (CSI Miami, Frasier, ...).
# Exact-name matching only, gated to 24/7-category streams (source 'plutofast').
PLUTOFAST_URL = 'https://i.mjh.nz/PlutoTV/us.xml.gz'

# Channel parsing (robust: icon/url may precede display-name).
CHAN_BLOCK_RE = re.compile(r'<channel\s+id="(?P<id>[^"]*)"[^>]*>(?P<body>.*?)</channel>', re.S)
DN_RE = re.compile(r'<display-name[^>]*>([^<]*)</display-name>')

# US call signs (K/W + 2-4 alnum chars), used to match provider "FOX: FL | Tampa | WTVT"
# against epgshare01 "WTVT-DT.us_locals1".
CALLSIGN_RE = re.compile(r'^[KW][A-Z0-9]{2,4}$')


def call_sign(display_name):
    """Return the lowercased call sign of a display-name like 'WTVT-DT', else None."""
    base = (display_name or '').split('-')[0].strip().upper()
    return base.lower() if CALLSIGN_RE.match(base) else None


# Country files, ordered roughly by expected yield for this provider's lineup.
# 2026-08-15 additions (global source audit): AE1 (UAE/MENA hub — DX/RSL/MO
# gaps), IN4, CH1, AT1, BE2, AL1 (SuperSport ZA), ASIANTELEVISION1 (ARY QTV/
# ATN News), MT1, HU1, SK1. Removed PH1/PK1 (0 programmes, verified).
ESHARE_FILES = [
    'US_LOCALS1', 'US2', 'US_SPORTS1',
    'UK1', 'IE1',
    'CA2',
    'DE1', 'FR1', 'IT1', 'GR1', 'RO1', 'RO2', 'ES1', 'PL1', 'PT1',
    'AU1', 'ZA1',
    'PH2', 'DK1', 'TR1', 'TR3', 'TH1',
    'SE1', 'NL1', 'NO1', 'FI1', 'CY1', 'NZ1', 'BR1', 'BR2', 'CZ1',
    'IN1', 'IN2', 'IN4',
    'AE1', 'AL1', 'CH1', 'AT1', 'BE2', 'ASIANTELEVISION1', 'MT1', 'HU1', 'SK1',
]

# epgshare01 file -> additional ISO country codes it may serve (beyond its
# own country). Files are matched against a stream's country via
# build_mapping.COUNTRY_SOURCES; entries here keep MENA/regional files usable.
EXTRA_COUNTRY_SOURCES = {
    'AE1': ['AE', 'SA', 'QA', 'KW', 'OM', 'BH', 'JO', 'EG', 'MO'],  # pan-Arab hub
    'AL1': ['AL', 'BA', 'HR', 'ME', 'MK', 'RS', 'SI', 'XK'],
    'CH1': ['CH', 'DE', 'AT'],
    'AT1': ['AT', 'DE'],
    'BE2': ['BE', 'NL', 'LU'],
    'ASIANTELEVISION1': ['UK', 'BD', 'IN', 'PK'],
    'MT1': ['MT', 'IT'],
    'HU1': ['HU', 'RO', 'SK', 'CZ'],
    'SK1': ['SK', 'CZ'],
    'AR1': ['AR', 'UY', 'PY', 'BO', 'EC', 'CO', 'CL', 'PE', 'VE'],
    'SA2': ['SA'],
    'BEIN1': ['QA', 'AE', 'SA', 'MO', 'EG'],
}


def download(url, dest, timeout=300, insecure=False):
    # Production builds must refresh sources. Reuse is an explicit local-dev
    # opt-in only (EPG_REUSE_CACHE=1); otherwise an old non-empty/truncated file
    # cannot silently masquerade as today's feed (AUDIT-5 F-10).
    if os.environ.get('EPG_REUSE_CACHE') == '1' and os.path.exists(dest) and os.path.getsize(dest) > 0:
        return os.path.getsize(dest)
    req = urllib.request.Request(url, headers=UA)
    ctx = None
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        data = r.read()
    if not data:
        raise ValueError(f'empty response from {url}')
    # Reject HTML/error pages regardless of URL suffix (AUDIT-7: the old
    # suffix-condition made this dead code for every real .xml/.gz feed).
    probe = data
    if url.endswith('.gz'):
        try:
            probe = gzip.decompress(data)
        except OSError as e:
            raise ValueError(f'invalid gzip response from {url}: {e}') from e
    head = probe[:65536].lstrip().lower()
    if head.startswith(b'<!doctype html') or head.startswith(b'<html') or b'<html' in head[:512]:
        raise ValueError(f'HTML response where XMLTV feed expected: {url}')
    if url.endswith('.xml') or url.endswith('.gz'):
        if b'<tv' not in head and b'<channel' not in head:
            raise ValueError(f'not an XMLTV response from {url}')
    # Write atomically so a failed download never leaves a partial final file.
    part = dest + '.part'
    with open(part, 'wb') as f:
        f.write(data)
    os.replace(part, dest)
    return len(data)


def host_of(url):
    return urlsplit(url).hostname or ''


class SourceFetchTracker:
    """Records per-source fetch outcomes for the fetch_status.json artifact.

    Allowlisted content only: source name, host, ok/failed/skipped, attempts,
    error summary. Never file contents or credentials."""

    def __init__(self, outdir):
        self.outdir = outdir
        self.entries = []

    def record(self, source, url, status, attempts=1, error=None):
        self.entries.append({
            'source': source,
            'host': host_of(url),
            'status': status,  # ok | failed | skipped
            'attempts': attempts,
            'error': (str(error)[:200] if error else None),
        })

    def write(self):
        path = os.path.join(self.outdir, 'fetch_status.json')
        tmp = path + '.tmp'
        json.dump(self.entries, open(tmp, 'w'), indent=1)
        os.replace(tmp, path)
        n_ok = sum(1 for e in self.entries if e['status'] == 'ok')
        n_failed = len(self.entries) - n_ok - sum(1 for e in self.entries if e['status'] == 'skipped')
        print(f'[status] fetch_status.json: {len(self.entries)} sources '
              f'({n_ok} ok, {n_failed} failed) -> {path}')


RETRY_ATTEMPTS = 3
RETRY_DELAY_S = 5.0


def fetch_source_with_retry(url, dest, attempts=RETRY_ATTEMPTS,
                            delay=RETRY_DELAY_S, download=None):
    """download() with bounded retry + exponential backoff.

    Transient-error hardening only — a sustained multi-minute host outage
    (e.g. epgshare01 2026-09-02) will still exhaust these attempts. The
    host circuit-breaker in main() prevents retry amplification across the
    many files that share one host. `download` is resolved at CALL time so
    tests (and future callers) can inject a fake via module patching."""
    if download is None:
        download = globals()['download']
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return download(url, dest), attempt
        except Exception as e:  # noqa: BLE001
            last_error = e
            if attempt < attempts:
                print(f'[retry] {host_of(url)} attempt {attempt}/{attempts} '
                      f'failed: {e}; retrying in {delay}s', file=sys.stderr)
                time.sleep(delay)
                delay *= 2
    raise last_error


def read_xml(path):
    if path.endswith('.gz'):
        return gzip.open(path, 'rb').read().decode('utf-8', errors='ignore')
    return open(path, 'r', errors='ignore').read()


def build_index(path, greek_translit=False):
    """display-name -> [channel_id] index for one XMLTV file.

    Robust to channels where <icon>/<url> elements precede <display-name>
    (e.g. epgshare01 US locals use Gracenote imagery + url before the name).
    Display-names are HTML-unescaped so '&amp;pictures' indexes as
    '&pictures' (norm 'pictures') and matches the provider's '& pictures'.
    greek_translit=True (chrisliatas pack) also adds a Latin-transliterated
    entry for Greek-script names so Latin queries can hit them.
    """
    txt = read_xml(path)
    idx = SourceIndex()
    for m in CHAN_BLOCK_RE.finditer(txt):
        dn = DN_RE.search(m.group('body'))
        if dn:
            name = html.unescape(dn.group(1))
            idx.add(name, m.group('id'))
            if greek_translit:
                lat = _gr_translit(name)
                if lat != name:
                    idx.add(lat, m.group('id'))
    return idx


GR2LAT = {
    'α': 'a', 'β': 'v', 'γ': 'g', 'δ': 'd', 'ε': 'e', 'ζ': 'z', 'η': 'i',
    'θ': 'th', 'ι': 'i', 'κ': 'k', 'λ': 'l', 'μ': 'm', 'ν': 'n', 'ξ': 'x',
    'ο': 'o', 'π': 'p', 'ρ': 'r', 'σ': 's', 'ς': 's', 'τ': 't', 'υ': 'y',
    'φ': 'f', 'χ': 'ch', 'ψ': 'ps', 'ω': 'o',
    'ά': 'a', 'έ': 'e', 'ί': 'i', 'ό': 'o', 'ύ': 'y', 'ή': 'i', 'ώ': 'o',
    'ΐ': 'i', 'ΰ': 'y', 'ϊ': 'i', 'ϋ': 'y',
}


def _gr_translit(s):
    return ''.join(GR2LAT.get(c, c) for c in (s or '').lower())


def build_callsign_index(path):
    """call-sign -> [channel_id] index for one XMLTV file (US locals)."""
    txt = read_xml(path)
    out = defaultdict(list)
    for m in CHAN_BLOCK_RE.finditer(txt):
        dn = DN_RE.search(m.group('body'))
        if dn:
            cs = call_sign(html.unescape(dn.group(1)))
            if cs:
                out[cs].append(m.group('id'))
    return out


def main():
    outdir = sys.argv[1] if len(sys.argv) > 1 else './data'
    os.makedirs(outdir, exist_ok=True)
    tracker = SourceFetchTracker(outdir)
    # Host circuit breaker: after BREAK_AFTER consecutive full-failure files
    # on one host, remaining files on that host are skipped without retry.
    # A host-wide outage (epgshare01 2026-09-02) then costs one probed file,
    # not 43 x 3 attempts of guaranteed-failing requests.
    host_failures = defaultdict(int)
    BREAK_AFTER = 3

    def guarded(source, url, dest):
        """Fetch one source with retry + circuit breaker; track the outcome."""
        host = host_of(url)
        if host_failures[host] >= BREAK_AFTER:
            tracker.record(source, url, 'skipped', attempts=0,
                           error=f'host breaker open ({host_failures[host]} consecutive failures)')
            print(f'[breaker] {source}: skipped, host {host} failing', file=sys.stderr)
            return False
        try:
            n, used = fetch_source_with_retry(url, dest)
            print(f'[{source}] {n} bytes')
            manifest.append({'source': source, 'file': os.path.abspath(dest), 'kind': 'name'})
            host_failures[host] = 0
            tracker.record(source, url, 'ok', attempts=used)
            return True
        except Exception as e:  # noqa: BLE001
            host_failures[host] += 1
            tracker.record(source, url, 'failed', attempts=RETRY_ATTEMPTS, error=e)
            print(f'[{source}] FAILED: {e}', file=sys.stderr)
            return False

    files = os.environ.get('ESHARE_FILES', '').split(',')
    files = [f.strip() for f in files if f.strip()] or ESHARE_FILES

    manifest = []
    # epg.pw
    pw_path = os.path.join(outdir, 'epgpw_global.xml.gz')
    guarded('epg.pw', EPG_PW_URL, pw_path)

    # epgshare01
    for name in files:
        dest = os.path.join(outdir, f'es_{name}.xml.gz')
        url = ESHARE_BASE.format(name)
        guarded(f'epgshare01:{name}', url, dest)

    # mitthu786/tvepg — India OTT EPG (one AIO file, 1500+ channels)
    tvepg_path = os.path.join(outdir, 'tvepg_india.xml.gz')
    guarded('tvepg', TVEPG_URL, tvepg_path)

    # al7omed/bein-epg — beIN MENA sports (39 channels, self-updating)
    bein_path = os.path.join(outdir, 'bein_mena.xml')
    guarded('bein', BEIN_URL, bein_path)

    # CyTA Cyprus pack (NOVA bouquet + Cypriot linears)
    cyta_path = os.path.join(outdir, 'cyta_pack.xml')
    guarded('cyta', CYTA_URL, cyta_path)

    # chrisliatas/greek-xmltv (Digea DTT + ERT, daily release)
    greek_path = os.path.join(outdir, 'greek_pack.xml.gz')
    guarded('greek', GREEK_URL, greek_path)

    # i.mjh.nz PlutoTV US (FAST 24/7 loop channels)
    pluto_path = os.path.join(outdir, 'plutofast.xml.gz')
    guarded('plutofast', PLUTOFAST_URL, pluto_path)

    # dedicated fetcher outputs (generated earlier by the workflow step):
    #   ALLENTE_FILE / TEAMS_FILE / BBCRADIO_FILE (name-indexed like the others)
    for env, src in (('ALLENTE_FILE', 'allente'), ('TEAMS_FILE', 'teamfixtures'),
                     ('BBCRADIO_FILE', 'bbcradio')):
        f = os.environ.get(env, '').strip()
        if f and os.path.exists(f):
            manifest.append({'source': src, 'file': os.path.abspath(f), 'kind': 'name'})
            print(f'[{src}] {os.path.getsize(f)} bytes')

    # iptv-org per-site grabs (io_jiotv.xml, io_tataplay.xml, ...) — indexed as
    # separate sources so numeric site_ids can't collide across sites.
    # Entries may override the source name: "name=path" (used when one site's
    # grab is split across several files, e.g. programtv.onet.pl_a/_b — all
    # files merge under one source name).
    for f in os.environ.get('IPTV_ORG_FILES', '').split(','):
        f = f.strip()
        if not f:
            continue
        src = None
        if '=' in f:
            src, f = f.split('=', 1)
            src = src.strip()
        if not os.path.exists(f):
            continue
        if src is None:
            site = os.path.basename(f).replace('io_', '').replace('.xml', '')
            src = f'iptv-org:{site}'
        manifest.append({'source': src, 'file': os.path.abspath(f), 'kind': 'name'})
        print(f'[iptv-org] {src}: {os.path.getsize(f)} bytes ({os.path.basename(f)})')

    # dedicated fetcher outputs, indexed under their own source names so
    # build_mapping country-gates them correctly:
    #   SKYHAWK_FILE=.../skyhawk.xml  DSTV_FILE=.../dstv.xml  EPGONE_FILE=...
    for env, src in (('SKYHAWK_FILE', 'skyhawk'), ('DSTV_FILE', 'dstv'),
                     ('EPGONE_FILE', 'epgone')):
        f = os.environ.get(env, '').strip()
        if f and os.path.exists(f):
            manifest.append({'source': src, 'file': os.path.abspath(f), 'kind': 'name'})
            print(f'[{src}] {os.path.getsize(f)} bytes')

    json.dump(manifest, open(os.path.join(outdir, 'sources.json'), 'w'), indent=1)

    # name index (display-name -> channel id) per source. Multiple manifest
    # entries may share one source name (split grabs) — their indexes MERGE.
    index = {}
    callsigns = {}
    for m in manifest:
        if m['kind'] == 'name':
            try:
                idx = build_index(m['file'], greek_translit=(m['source'] == 'greek'))
                if m['source'] in index:
                    for k, v in idx.by_name.items():
                        index[m['source']].setdefault(k, []).extend(v)
                else:
                    index[m['source']] = {k: list(v) for k, v in idx.by_name.items()}
                cs = build_callsign_index(m['file'])
                if m['source'] in callsigns:
                    for k, v in cs.items():
                        callsigns[m['source']].setdefault(k, []).extend(v)
                else:
                    callsigns[m['source']] = {k: list(v) for k, v in cs.items()}
                print(f'[index] {m["source"]}: {len(idx)} channels, {len(cs)} call signs')
            except Exception as e:  # noqa: BLE001
                print(f'[index] {m["source"]} FAILED: {e}', file=sys.stderr)
    json.dump(index, open(os.path.join(outdir, 'sources_index.json'), 'w'))
    json.dump(callsigns, open(os.path.join(outdir, 'call_signs.json'), 'w'))
    tracker.write()
    print(f'done: {len(manifest)} sources, {len(index)} indexed')


if __name__ == '__main__':
    main()
