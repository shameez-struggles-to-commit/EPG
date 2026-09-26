"""Credential-free manifest binds the public guide to one private ID seed."""
import argparse
import datetime as dt
import gzip
import hashlib
import io
import json
import re
import sys
from pathlib import Path
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent))
from stable_identity import require_seed, seed_digest

MAX_GUIDE_XML = 128 * 1024 * 1024


def guide_ids(blob):
    with gzip.GzipFile(fileobj=io.BytesIO(blob)) as f:
        xml = f.read(MAX_GUIDE_XML + 1)
    if len(xml) > MAX_GUIDE_XML:
        raise ValueError('Guide exceeds XML limit')
    root = ET.fromstring(xml)
    ids = [c.get('id') for c in root.findall('channel')]
    if root.tag != 'tv' or not ids or any(not x for x in ids) or len(ids) != len(set(ids)):
        raise ValueError('Invalid guide channel identities')
    return set(ids)


def create_manifest(blob, seed, revision, run_id):
    ids = guide_ids(blob)
    if not re.fullmatch(r'[0-9a-f]{40}', revision) or not str(run_id).isdigit():
        raise ValueError('Invalid release provenance')
    return {'schema': 1, 'guide_sha256': hashlib.sha256(blob).hexdigest(),
            'identity_sha256': seed_digest(seed), 'channels': len(ids),
            'revision': revision, 'run_id': str(run_id),
            'created_at': dt.datetime.now(dt.timezone.utc).isoformat()}


def verify_manifest(manifest, blob, seed):
    if (not isinstance(manifest, dict) or manifest.get('schema') != 1
            or manifest.get('identity_sha256') != seed_digest(seed)
            or manifest.get('guide_sha256') != hashlib.sha256(blob).hexdigest()):
        raise ValueError('Guide release or identity version does not match')
    ids = guide_ids(blob)
    if manifest.get('channels') != len(ids):
        raise ValueError('Guide release channel count does not match')
    return ids


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--guide', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--revision', required=True)
    ap.add_argument('--run-id', required=True)
    args = ap.parse_args()
    manifest = create_manifest(Path(args.guide).read_bytes(), require_seed(), args.revision, args.run_id)
    Path(args.out).write_text(json.dumps(manifest, indent=2) + '\n')
    print('Release manifest ready: %s channels' % manifest['channels'])


if __name__ == '__main__':
    main()
