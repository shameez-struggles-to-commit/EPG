#!/usr/bin/env python3
"""Channel matcher: map provider stream names → source-feed channel entries.

Inputs (build time):
  - provider streams JSON (name, category)
  - epg.pw global index (id -> display-name)
  - iptv-org database (id, name, alt_names, country) — for fuzzy/alias matching
  - provider's own xmltv (optional long-tail layer)
  - PK scrapers output (channel_key -> programmes)

Strategy per provider stream:
  1. overrides.yaml (exact stream-name -> source key) — manual fixes, highest priority
  2. normalized exact match against source display-names (country-hint aware)
  3. iptv-org alt_names alias match
  4. fuzzy match (token-set ratio >= threshold) — logged for review

Output: mapping.json  {stream_name: {source, source_id, method, confidence}}
"""
import json
import re
import sys
import unicodedata
from collections import defaultdict

try:
    import yaml
except ImportError:
    yaml = None

WORD_RE = re.compile(r'\w+', re.UNICODE)
QUALITY_RE = re.compile(r'\b(fhd|uhd|hd|sd|4k|1080p?|720p?)\b', re.I)
FILLER = {'the', 'tv', 'channel', 'network', 'east', 'west', 'us', 'usa', 'uk', 'hd', 'sd', 'fhd', 'uhd'}


def norm(s):
    s = unicodedata.normalize('NFKD', s.lower())
    s = QUALITY_RE.sub(' ', s)
    s = re.sub(r'[^\w\s]', ' ', s)
    toks = [t for t in WORD_RE.findall(s) if t not in FILLER]
    return ' '.join(toks)


def token_set_ratio(a, b):
    """Simple Jaccard-ish token overlap score in [0,1]."""
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / max(1, len(sa | sb))


class Matcher:
    def __init__(self, pw_index, iptvorg_rows, overrides=None):
        # display-name -> [pw ids]
        self.pw_by_name = defaultdict(list)
        for cid, disp in pw_index.get('disp', {}).items():
            self.pw_by_name[norm(disp)].append(cid)
        # iptv-org rows: id, name, alt_names, country
        self.io_rows = iptvorg_rows
        self.io_by_name = defaultdict(list)
        for r in iptvorg_rows:
            names = [r['name']] + (r.get('alt_names') or [])
            for nm in names:
                if nm:
                    self.io_by_name[norm(nm)].append((r['id'], r.get('country')))
        self.overrides = overrides or {}

    def match(self, stream_name, country_hint=None):
        # 1. overrides
        if stream_name in self.overrides:
            return {'source': 'override', 'source_id': self.overrides[stream_name], 'method': 'override', 'confidence': 1.0}

        n = norm(stream_name)
        if not n:
            return None

        # 2. exact against epg.pw display-names (country-hint aware)
        if n in self.pw_by_name:
            return {'source': 'epg.pw', 'source_id': self.pw_by_name[n][0], 'method': 'exact', 'confidence': 1.0}

        # 3. iptv-org alias/name exact
        if n in self.io_by_name:
            cands = self.io_by_name[n]
            best = cands[0]
            for cid, cc in cands:
                if country_hint and cc == country_hint:
                    best = (cid, cc)
                    break
            return {'source': 'iptv-org', 'source_id': best[0], 'method': 'exact-alias', 'confidence': 0.95}

        # 4. fuzzy against epg.pw names (bounded: only tokens sharing first token or country hint)
        best_score, best_key = 0.0, None
        first_tok = n.split()[0]
        for cand in self.pw_keys_with_prefix(first_tok):
            sc = token_set_ratio(n, cand)
            if sc > best_score:
                best_score, best_key = sc, cand
        if best_score >= 0.75:
            return {'source': 'epg.pw', 'source_id': self.pw_by_name[best_key][0],
                    'method': 'fuzzy', 'confidence': round(best_score, 2)}
        return None

    def pw_keys_with_prefix(self, tok):
        return self._pw_keys.get(tok, ())

    def build_index(self):
        self._pw_keys = defaultdict(tuple)
        for k in self.pw_by_name:
            for t in set(k.split()):
                self._pw_keys[t] = self._pw_keys[t] + (k,)
