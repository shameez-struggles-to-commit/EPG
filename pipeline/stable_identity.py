"""Private bootstrap assignments keep client IDs stable across provider edits.

The seed contains only exceptions to xtream:<stream_id>. It is delivered as a
base64-gzip JSON secret in CI and as a private local file on the playlist host.
Never publish the seed or provider lineup. Absence is explicit legacy mode for
old callers; production entrypoints must call require_seed().
"""
import base64
import gzip
import hashlib
import io
import json
import os
import re
from pathlib import Path

MAX_SEED_BYTES = 2 * 1024 * 1024


def load_seed(encoded=None):
    if encoded is None:
        encoded = os.environ.get('EPG_IDENTITY_SEED')
        filename = os.environ.get('EPG_IDENTITY_FILE')
        if not encoded and filename:
            encoded = Path(filename).read_text(encoding='ascii').strip()
    if not encoded:
        return None
    try:
        if len(encoded) > MAX_SEED_BYTES:
            raise ValueError()
        packed = base64.b64decode(encoded, validate=True)
        with gzip.GzipFile(fileobj=io.BytesIO(packed)) as f:
            raw = f.read(MAX_SEED_BYTES + 1)
        if len(raw) > MAX_SEED_BYTES:
            raise ValueError()
        seed = json.loads(raw)
        if set(seed) != {'schema', 'overrides'} or seed['schema'] != 1:
            raise ValueError()
        overrides = seed['overrides']
        if not isinstance(overrides, dict):
            raise ValueError()
        for sid, cid in overrides.items():
            if not isinstance(sid, str) or not re.fullmatch(r'[1-9][0-9]*', sid):
                raise ValueError()
            if (not isinstance(cid, str) or not cid.strip() or cid != cid.strip()
                    or cid.startswith('xtream:') or len(cid) > 512
                    or any(ord(c) < 32 for c in cid)):
                raise ValueError()
        if len(set(overrides.values())) != len(overrides):
            raise ValueError()
        return seed
    except Exception:
        raise ValueError('Invalid private identity seed') from None


def require_seed():
    seed = load_seed()
    if seed is None:
        raise ValueError('Private identity seed is required')
    return seed


def seed_digest(seed):
    raw = json.dumps(seed, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()
    return hashlib.sha256(raw).hexdigest()


def stable_map(streams, seed):
    output = {}
    for stream in streams:
        sid = str(stream.get('stream_id') or '')
        if not re.fullmatch(r'[1-9][0-9]*', sid) or sid in output:
            raise ValueError('Missing or duplicate provider stream ID')
        output[sid] = seed['overrides'].get(sid, 'xtream:' + sid)
    if len(set(output.values())) != len(output):
        raise ValueError('Duplicate client channel ID')
    return output
