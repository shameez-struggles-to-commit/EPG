#!/usr/bin/env python3
"""Channel-name matching primitives for the EPG pipeline.

Provides:
  - norm(): aggressive but safe channel-name normalization (strip quality
    tokens, accents, filler words; tokenize). Used on BOTH provider stream
    names and source display-names so they compare cleanly.
  - token_set_ratio(): proper fuzzywuzzy-style token-set ratio (difflib-based)
    — a real improvement over the old Jaccard overlap, which was too weak to
    catch anything but near-exact matches.
  - SourceIndex: builds a display-name -> channel-id index for one EPG source,
    with an inverted token index so fuzzy matching is O(candidates) not O(N).

Deliberately stdlib-only so it runs on the bare GitHub runner (and 3.9+).
"""

import re
import unicodedata
from collections import defaultdict
from difflib import SequenceMatcher

# Quality/format tokens stripped from names ("BBC One | FHD |" -> "bbc one").
QUALITY_RE = re.compile(r'\b(?:fhd|uhd|qhd|hd|sd|4k|1080p?|720p?|hevc|h265|h264)\b', re.I)

# Filler words dropped as standalone tokens. IMPORTANT: do NOT add regional
# markers (east/west/us/usa/uk/ca) or network discriminators here — dropping
# them collapses genuinely different channels ("BBC One East" vs "BBC One West",
# "Sky News UK" vs "Sky News"). Only truly meaningless words belong here.
FILLER = {'the', 'tv', 'channel', 'network', 'and'}

WORD_RE = re.compile(r'\w+', re.UNICODE)

# Dotted ISO-3166-alpha-2 country suffix on a display-name ("Aaj Tak HD.in",
# "Colors HD.us"). globetvapp and similar aggregators append these; strip them
# so the name normalizes cleanly. NOT applied to standalone regional words
# ("BBC One UK") — only the dot-attached form.
COUNTRY_SUFFIX_RE = re.compile(
    r'\.(in|pk|uk|us|ca|de|fr|it|gr|ro|es|pl|pt|au|za|nl|se|no|fi|dk|tr|th|ie|nz|'
    r'br|cz|mx|ar|jp|il|ae|sa|at|be|bg|ch|hr|hu|rs|kr|sg|id|my|ru|al|ua|bd|eg|ng|'
    r'ke|cy|ee|is|lu|lv|lt|mt|sk|si|qa|kw|om|bh|jo|lb|iq|ir|af|np|lk|mv|mm|kh|la|'
    r'vn|tw|hk|mo|cn|tw|ph|az|ge|am|kz|uz|tm|kg|tj|mn|et|tz|ug|gh|zm|zw|mu|mg|bw|'
    r'na|sz|ls|mw|mz|ao|cm|ci|sn|ml|bf|ne|td|sd|ss|so|dj|er|rw|bi|cd|cg|ga|gq)\b',
    re.I)


def norm(s):
    """Normalize a channel name for comparison. Returns a token string."""
    if s is None:
        return ''
    s = unicodedata.normalize('NFKD', str(s))
    # strip combining marks (é -> e, ü -> u) while keeping non-Latin scripts intact
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = QUALITY_RE.sub(' ', s)
    s = COUNTRY_SUFFIX_RE.sub(' ', s)
    s = re.sub(r'[^\w\s]', ' ', s)
    toks = [t for t in WORD_RE.findall(s) if t not in FILLER]
    return ' '.join(toks)


def _ratio(a, b):
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def token_sort_ratio(a, b):
    """Ratio of the two names after sorting their tokens (order-insensitive)."""
    return _ratio(' '.join(sorted(a.split())), ' '.join(sorted(b.split())))


def token_set_ratio(a, b):
    """fuzzywuzzy-style token-set ratio in [0,1].

    NOTE: kept for reference; NOT used by the matcher because its subset bias
    scores "DW Espanol" -> "DW" as 1.0 (a false positive for channel matching).
    Use dice_ratio() instead.
    """
    ta = a.split()
    tb = b.split()
    inter = set(ta) & set(tb)
    if not inter:
        return 0.0
    inter_sorted = ' '.join(sorted(inter))
    return max(
        _ratio(inter_sorted, ' '.join(sorted(ta))),
        _ratio(inter_sorted, ' '.join(sorted(tb))),
    )


def dice_ratio(a, b):
    """Symmetric Dice coefficient over token sets, in [0,1].

    Rewards near-identical token sets WITHOUT rewarding subset relationships
    ("dw espanol" vs "dw" = 2*1/3 = 0.67, not 1.0). This is the safe fuzzy
    score for channel matching.
    """
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return 0.0
    return 2 * len(sa & sb) / (len(sa) + len(sb))


# Category names (lowercased) that indicate NON-linear channels: 24/7 restreams,
# VIP sports restreams, radio, event hubs, adult. No real EPG exists for these,
# so they are excluded from matching (per the user's requirement).
NON_LINEAR_KEYWORDS = (
    '24/7', 'vip', 'radio', 'for adults', 'event', 'flo', 'epl', 'efl',
    'nfl', 'nba', 'mlb', 'nhl', 'nrl', 'ufc', 'ppv', 'fifa+', 'espn+',
    'adult',
)


def is_non_linear(category_name):
    c = (category_name or '').lower()
    return any(k in c for k in NON_LINEAR_KEYWORDS)


class SourceIndex:
    """Index of one source's channels: normalized display-name -> channel id(s).

    Supports exact lookup and a bounded fuzzy scan using an inverted
    token->names index (only names sharing a token with the query are scored).
    """

    def __init__(self):
        self.by_name = defaultdict(list)   # norm(display-name) -> [channel_id, ...]
        self._token_index = defaultdict(set)  # token -> {norm(display-name), ...}
        self._size = 0

    def add(self, display_name, channel_id):
        n = norm(display_name)
        if not n or not channel_id:
            return
        self.by_name[n].append(channel_id)
        for t in n.split():
            self._token_index[t].add(n)
        self._size += 1

    def __len__(self):
        return self._size

    def exact(self, name):
        """Return [channel_id, ...] for an exact normalized match, else []."""
        return self.by_name.get(norm(name), [])

    def fuzzy(self, name, threshold=0.85, limit=3, min_common=2):
        """Return [(score, norm_name, channel_id), ...] above threshold, best first.

        Uses the symmetric Dice coefficient and requires >= min_common shared
        tokens, so single-word subset matches ("DW Espanol" -> "DW") are
        rejected.
        """
        n = norm(name)
        if not n:
            return []
        toks = n.split()
        # Candidate names = union of names sharing any token with the query.
        # Prefer the rarest token first to keep the candidate set small, but
        # always include the first token (most discriminative for "X News").
        cand = set()
        for t in sorted(toks, key=lambda x: len(self._token_index.get(x, set())))[:3]:
            cand |= self._token_index.get(t, set())
        cand |= self._token_index.get(toks[0], set())
        out = []
        for cn in cand:
            if cn == n:
                continue  # exact handled separately
            if len(set(toks) & set(cn.split())) < min_common:
                continue
            sc = dice_ratio(n, cn)
            if sc >= threshold:
                out.append((sc, cn, self.by_name[cn][0]))
        out.sort(key=lambda x: (-x[0], x[1]))
        return out[:limit]
