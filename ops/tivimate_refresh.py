"""Private companion playlist delivery; output contains counts, never URLs."""


import time
import html
import re
from urllib.parse import urlsplit


def validate_playlist(blob, guide_channel_ids, expected_entries):
    lines = blob.decode('utf-8').splitlines()
    if not lines or lines[0] != '#EXTM3U':
        raise ValueError('Invalid playlist header')
    ids = []
    pending = False
    for line in lines[1:]:
        if not line:
            continue
        if line.startswith('#EXTINF:'):
            if pending:
                raise ValueError('Playlist entry has no stream URL')
            matches = re.findall(r'tvg-id="([^"]*)"', line)
            if len(matches) != 1 or not matches[0]:
                raise ValueError('Invalid playlist ID')
            ids.append(html.unescape(matches[0]))
            pending = True
        elif line.startswith('#'):
            raise ValueError('Unexpected playlist directive')
        else:
            url = urlsplit(line)
            if not pending or url.scheme != 'https' or not url.netloc or url.username:
                raise ValueError('Invalid playlist stream entry')
            pending = False
    if pending or len(ids) != expected_entries or len(ids) != len(set(ids)):
        raise ValueError('Playlist size or identity check failed')
    if not guide_channel_ids.issubset(set(ids)):
        raise ValueError('Published guide IDs are absent from the proposed playlist')
    return set(ids)


def exact_roundtrip(expected, received):
    return expected == received


def validate_continuity(previous, candidate, minimum_retained=0.95):
    def ids(blob):
        return {html.unescape(x) for x in re.findall(r'tvg-id="([^"]+)"', blob.decode('utf-8'))}
    old, new = ids(previous), ids(candidate)
    if not old or len(old & new) < len(old) * minimum_retained:
        raise ValueError('Provider lineup identity changed unexpectedly')


def publish_verified(expected, fetch_current, send, fetch_release, release, sleep=time.sleep):
    current = fetch_current()
    if fetch_release() != release:
        raise ValueError('Guide release changed; retaining the previous playlist')
    if exact_roundtrip(expected, current):
        return 'unchanged'
    send(expected)
    # Never issue a second write merely because the readback is delayed.
    for attempt in range(4):
        try:
            fresh = fetch_current()
        except Exception:
            fresh = None
        if fresh is not None and exact_roundtrip(expected, fresh):
            if fetch_release() != release:
                # Restore only our own write. Never overwrite a third party's
                # newer playlist. The last accepted version is the rollback anchor.
                if fetch_current() != expected:
                    raise ValueError('Guide changed and playlist changed externally; recovery required')
                send(current)
                for recovery_attempt in range(4):
                    if fetch_current() == current:
                        raise ValueError('Guide release changed; previous playlist restored and verified')
                    if recovery_attempt < 3:
                        sleep(15)
                raise ValueError('Guide changed; playlist rollback could not be verified')
            return 'updated'
        if attempt < 3:
            sleep(15)
    raise ValueError('Playlist write has not been verified; exact readback differs')


def fetch_bytes(url):
    import urllib.request
    class HTTPSOnly(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, target):
            if urlsplit(target).scheme != 'https':
                raise ValueError('HTTPS redirect required')
            return super().redirect_request(req, fp, code, msg, headers, target)
    if urlsplit(url).scheme != 'https':
        raise ValueError('HTTPS URL required')
    request = urllib.request.Request(url, headers={'User-Agent': 'EPG-playlist-refresh'})
    with urllib.request.build_opener(HTTPSOnly()).open(request, timeout=120) as response:
        data = response.read(32 * 1024 * 1024 + 1)
    if len(data) > 32 * 1024 * 1024:
        raise ValueError('Download exceeds limit')
    return data


def run_command(args, **kwargs):
    import subprocess
    result = subprocess.run(args, capture_output=True, timeout=600, **kwargs)
    if result.returncode:
        # Captured logs may contain provider credentials. Never emit them.
        raise ValueError('Required subprocess failed')
    return result.stdout


def run_refresh(config, dry_run=False):
    import fcntl
    import json
    import os
    from pathlib import Path
    import shutil
    import sys
    import tempfile
    from urllib.parse import urljoin
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))
    from stable_identity import load_seed
    from release_manifest import verify_manifest

    os.umask(0o077)
    work = Path(config['work_dir'])
    work.mkdir(parents=True, exist_ok=True)
    seed = load_seed(Path(config['identity_file']).read_text().strip())
    if seed is None:
        raise ValueError('Private identity seed is required')
    guide_url = config['guide_url']
    manifest_url = urljoin(guide_url, 'release.json')
    def current_release():
        return json.loads(fetch_bytes(manifest_url + '?check=' + str(time.time_ns())))
    with (work / '.refresh.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        release = current_release()
        guide = fetch_bytes(guide_url + '?check=' + str(time.time_ns()))
        ids = verify_manifest(release, guide, seed)
        # An isolated private directory prevents partial fetches contaminating
        # a later attempt. No credentials or generated playlist are public.
        with tempfile.TemporaryDirectory(prefix='refresh-', dir=work) as temp:
            directory = Path(temp)
            auth = json.loads(Path(config['auth_file']).read_text())
            env = dict(os.environ, IPTV_SERVER=auth['server_info']['url'],
                       IPTV_USER=auth['user_info']['username'],
                       IPTV_PASS=auth['user_info']['password'],
                       EPG_IDENTITY_FILE=config['identity_file'])
            env.pop('EPG_IDENTITY_SEED', None)
            repo = Path(config['repo_dir'])
            run_command([sys.executable, str(repo / 'pipeline/fetch_provider.py'), temp], env=env)
            streams = json.loads((directory / 'streams.json').read_text())
            if len(streams) < config.get('minimum_entries', 5000):
                raise ValueError('Provider lineup is implausibly small')
            target = directory / 'tivimate.m3u'
            # Generator needs identity seed, but not credentials in its environment.
            generator_env = dict(os.environ, EPG_IDENTITY_FILE=config['identity_file'])
            generator_env.pop('EPG_IDENTITY_SEED', None)
            for key in ('IPTV_USER', 'IPTV_PASS', 'IPTV_SERVER'):
                generator_env.pop(key, None)
            run_command([sys.executable, str(repo / 'pipeline/make_tivimate_m3u.py'),
                         str(directory / 'streams.json'), config['auth_file'], str(target)], env=generator_env)
            expected = target.read_bytes()
            validate_playlist(expected, ids, len(streams))
            if current_release() != release:
                raise ValueError('Guide release changed during generation')
            if dry_run:
                return {'outcome': 'dry-run', 'entries': len(streams), 'guide_ids': len(ids)}
            gh = config.get('gh') or shutil.which('gh')
            gist = config['gist_id']; filename = config['gist_file']
            if not gh or not re.fullmatch(r'[0-9a-f]{20,40}', gist):
                raise ValueError('Invalid delivery configuration')
            def fetch_current():
                data = json.loads(run_command([gh, 'api', 'gists/' + gist]))
                item = data['files'][filename]
                if item.get('truncated'):
                    url = item['raw_url']
                    if urlsplit(url).hostname != 'gist.githubusercontent.com':
                        raise ValueError('Unexpected playlist readback host')
                    return fetch_bytes(url + '?check=' + str(time.time_ns()))
                return item['content'].encode('utf-8')
            previous = fetch_current()
            validate_continuity(previous, expected, config.get('minimum_retained_fraction', 0.95))
            (work / 'previous.m3u').write_bytes(previous)
            def send(blob):
                payload = json.dumps({'description': 'TiviMate companion playlist (auto-updated)',
                                      'files': {filename: {'content': blob.decode('utf-8')}}}).encode()
                run_command([gh, 'api', '-X', 'PATCH', 'gists/' + gist, '--input', '-'], input=payload)
            result = publish_verified(expected, fetch_current, send, current_release, release)
            (work / 'tivimate.m3u').write_bytes(expected)
            state = {'outcome': result, 'entries': len(streams), 'guide_ids': len(ids),
                     'run_id': release['run_id'], 'guide_sha256': release['guide_sha256']}
            (work / 'verified-release.json').write_text(json.dumps(state, indent=2) + '\n')
            return state


def main(argv=None):
    import argparse
    import json
    import os
    from pathlib import Path
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default=str(Path(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes'))) / 'tivimate-refresh.json'))
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = run_refresh(json.loads(Path(args.config).read_text()), dry_run=args.dry_run)
    except Exception as error:
        # Only our controlled messages are emitted. Other exceptions may include URLs.
        print('FAIL: playlist refresh did not complete (%s)' % type(error).__name__)
        return 1
    print('OK %s: %s entries, %s bound to guide' % (result['outcome'], result['entries'], result['guide_ids']))
    print('{"wakeAgent": false}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
