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
FILLER = {'the', 'tv', 'channel', 'network'}

WORD_RE = re.compile(r'\w+', re.UNICODE)

# Cyrillic → Latin transliteration (channel-name matching bridge, e.g. provider
# "5 Kanal (5 канал)" vs source "5 Kanal" / "5 канал"). Letter-only: spaces and
# punctuation pass through so tokenization survives. Russian/Ukrainian subsets.
CYRILLIC_TO_LAT = {
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'h', 'ґ': 'g', 'д': 'd', 'е': 'e',
    'ё': 'e', 'є': 'ie', 'ж': 'zh', 'з': 'z', 'и': 'y', 'і': 'i', 'ї': 'i',
    'й': 'i', 'к': 'k', 'л': 'l', 'м': 'm', 'н': 'n', 'о': 'o', 'п': 'p',
    'р': 'r', 'с': 's', 'т': 't', 'у': 'u', 'ф': 'f', 'х': 'kh', 'ц': 'ts',
    'ч': 'ch', 'ш': 'sh', 'щ': 'shch', 'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e',
    'ю': 'iu', 'я': 'ia',
}


def cyr_to_lat(s):
    """Transliterate Cyrillic characters in s to Latin (others pass through)."""
    return ''.join(CYRILLIC_TO_LAT.get(c, c) for c in (s or '').lower())

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
    s = s.replace('&', ' and ')  # before punctuation strip: "&TV" -> "and tv"
    s = QUALITY_RE.sub(' ', s)
    s = COUNTRY_SUFFIX_RE.sub(' ', s)
    s = re.sub(r'[^\w\s]', ' ', s)
    # digit-concatenation split: "News18" <-> "News 18", "Mh1" <-> "Mh 1"
    s = re.sub(r'(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])', ' ', s)
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

# Radio allowlist (2026-08-19, user-approved "include radio"): stations whose
# names EXACTLY match (norm) a source channel that carries live programmes
# (21 verified in daily sources: BBC Radio 2/3/4 via epgshare01 BE2, Classic
# FM/Heart/Planet Rock via epg.pw, etc.). The dedicated 'bbcradio' fetcher
# covers the BBC locals separately (stream-name-keyed, bypasses this table).
RADIO_LINEAR_NORMS = {
    'bbc radio 1xtra', 'bbc radio 2', 'bbc radio 3', 'bbc radio 4',
    'bbc radio 4 extra', 'bbc radio london', 'classic fm', 'classic rock',
    'gold', 'greatest hits', 'heart 80s', 'heart 90s', 'heart dance',
    'kerrang', 'magic fm', 'planet rock', 'radio x', 'rte radio 1',
    'talksport', 'virgin radio',
}

# Channel-NAME patterns for event-only slots (2026-08-15 audit: these carry
# match-day feeds only — no published schedule exists, so chasing EPG for
# them is wasted effort). Matched against the channel name, not category.
# NOTE (2026-08-16): many provider names use PIPE separators ("Cymru TV | Event
# 1"), so every pattern allows optional "\s*\|?\s*" between words. Patterns
# that end in \d are anchored/word-bounded so real channels survive
# (e.g. "CINEMA 1 MD" is a covered Moldova channel, but "Cinema 1" is a
# Romanian rental slot).
EVENT_NAME_RES = (
    re.compile(r'^FA Cup \d+', re.I),
    re.compile(r'^FA Player \d+', re.I),
    re.compile(r'^National League \d+', re.I),
    re.compile(r'^Sky Sports\+\s*\|?\s*Event', re.I),
    re.compile(r'^Womens? Football', re.I),
    re.compile(r'^HBO Max UK\s*\|?\s*Event', re.I),
    re.compile(r'^Solid Sport\s*\|?\s*Event', re.I),
    re.compile(r'^Cymru TV\s*\|?\s*Event', re.I),
    re.compile(r'^\u02e2 \u1d3e \u1d56 \u02e1', re.I),          # 'ˢ ᴾ ᶠ ᴸ' SPFL slots
    re.compile(r'\(Event Only\)', re.I),
    re.compile(r'^Magenta Sport \d+', re.I),
    re.compile(r'^Primafila \d+', re.I),
    re.compile(r'^Sky Store Premiere', re.I),
    re.compile(r'^Stan AU\s*\|?\s*Event', re.I),
    re.compile(r'^Ligue 1 \d+', re.I),
    re.compile(r'GAA|LOI( |$)|Tyrone Gaa|Ulster Gaa|NIFL', re.I),
    # 2026-08-16 additions (verified: none of these are in the deployed guide)
    re.compile(r'^A La Carte \d+', re.I),      # FR rental slots
    re.compile(r'^Alquiler \d+', re.I),        # ES rental slots
    re.compile(r'^Amazon Prime \d+', re.I),    # AU event slots
    re.compile(r'^Friendly \d+', re.I),        # UK friendly-match feeds
    re.compile(r'^DL TV \d+', re.I),           # TH restream slots
    re.compile(r'^OHL TV \d+', re.I),          # CA hockey event feeds
    re.compile(r'^Cinema \d+$', re.I),         # RO rental slots ($: keeps "CINEMA 1 MD")
    re.compile(r'^Germany Besondere \d+', re.I),
    re.compile(r'Sky Sports Red Button', re.I),
    re.compile(r'^Premier Greyhound', re.I),
    re.compile(r'^MLS Soccer \d+', re.I),      # US league-pass match feeds
    re.compile(r'^MiLB TV', re.I),             # US minor-league event feeds
    re.compile(r'Paramount\+ Event', re.I),    # "US | Paramount+ Event NN"
    re.compile(r'^Peacock ', re.I),            # Peacock vault/news/SNL feeds
    re.compile(r'^MC \|', re.I),               # Music Choice audio loops
)

# Real LINEAR channels the provider misfiles under a non-linear category.
# Checked in is_non_linear BEFORE the category-keyword test, so e.g.
# "Willow Cricket HD" (cat "VIP | WIllow") and "EPL | LFC TV" (cat "EPL |
# Clubs TV") still match their real schedules. Names are scoped so the
# genuinely event-only siblings ("Willow 1 (Event Only)") stay dropped.
LINEAR_OVERRIDE_RES = (
    re.compile(r'\bWillow Cricket\b', re.I),   # not "Willow 1 (Event Only)"
    re.compile(r'\bbeIN Sports Xtra\b', re.I),
    re.compile(r'\bNBA Tv\b', re.I),
    re.compile(r'\bLFC TV\b', re.I),
    re.compile(r'\bMUTV\b', re.I),
    # 2026-08-19: real linear channel misfiled under VIP with a REAL provider
    # epg_id (beinsports.us, 68 progs in provider feed). Name-scoped.
    re.compile(r'^beIN Sport 1 FHD$', re.I),
)


def is_non_linear(category_name, channel_name=None):
    c = (category_name or '').lower()
    if 'radio' in c and channel_name and norm(channel_name) in RADIO_LINEAR_NORMS:
        return False  # allowlisted radio station with live source data
    if any(k in c for k in NON_LINEAR_KEYWORDS):
        # real LINEAR channels the provider misfiled under a non-linear
        # category (VIP/EPL/NBA League Pass) — force them through so their
        # epg_id (provider feed) or a source match can supply real schedules.
        if channel_name:
            for rx in LINEAR_OVERRIDE_RES:
                if rx.search(channel_name):
                    return False
        return True
    if channel_name:
        for rx in EVENT_NAME_RES:
            if rx.search(channel_name):
                return True
    return False


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
