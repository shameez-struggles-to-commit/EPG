"""HTTPS-only transport for authenticated provider metadata and XMLTV guides."""

import urllib.error
import urllib.parse
import urllib.request

UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'}


def _parts(url):
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme.lower() != 'https' or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError
        # Force port parsing, which rejects malformed authorities.
        parsed.port
        if any(c in url for c in '\r\n\\'):
            raise ValueError
        return parsed
    except ValueError:
        raise ValueError('HTTPS metadata URL required') from None


def metadata_url(server, user, password, endpoint, *, action=None, proto='https'):
    """Build a provider URL without treating credentials as URL syntax."""
    if proto not in ('', 'https'):
        raise ValueError('HTTPS provider protocol required')
    parsed = _parts('https://' + server)
    if parsed.path or parsed.query or parsed.fragment or '@' in server:
        raise ValueError('Provider server must be a host or host:port')
    if endpoint not in ('player_api.php', 'xmltv.php'):
        raise ValueError('Unknown provider metadata endpoint')
    query = {'username': user, 'password': password}
    if action is not None:
        query['action'] = action
    return f'https://{server}/{endpoint}?{urllib.parse.urlencode(query)}'


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Permit clean cross-origin XMLTV redirects, never credential-bearing ones."""

    def __init__(self, allow_cross_origin_guide=False):
        self.allow_cross_origin_guide = allow_cross_origin_guide

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old = _parts(req.full_url)
        new = _parts(newurl)
        old_origin = (old.hostname.lower(), old.port or 443)
        new_origin = (new.hostname.lower(), new.port or 443)
        credentials = urllib.parse.parse_qs(old.query)
        secrets = set(getattr(req, '_provider_redirect_secrets', ()))
        secrets.update(value for key in ('username', 'password')
                       for value in credentials.get(key, ()) if value)
        if old_origin != new_origin:
            # Decode the entire path, not individual segments: credentials may be
            # embedded in filenames or split by encoded delimiters. Repeated
            # encoding must not conceal a credential from this check.
            path = new.netloc + new.path
            unsafe_path = False
            while True:
                if any(secret.casefold() in path.casefold() for secret in secrets):
                    unsafe_path = True
                    break
                decoded = urllib.parse.unquote(path)
                if decoded == path:
                    break
                path = decoded
            if (not self.allow_cross_origin_guide or new.query or new.fragment
                    or unsafe_path):
                raise ValueError('Unsafe HTTPS metadata redirect')
        # A fresh request cannot inherit Authorization, Cookie or custom headers.
        clean = urllib.request.Request(newurl, headers=UA)
        redirected = super().redirect_request(clean, fp, code, msg, headers, newurl)
        if redirected is not None:
            setattr(redirected, '_provider_redirect_secrets', frozenset(secrets))
        return redirected


def fetch(url, timeout=180, *, allow_cross_origin_guide=False):
    _parts(url)
    opener = urllib.request.build_opener(SafeRedirectHandler(allow_cross_origin_guide))
    try:
        with opener.open(urllib.request.Request(url, headers=UA), timeout=timeout) as response:
            return response.read()
    except Exception as exc:
        # urllib exceptions and their causes can include the full credential URL.
        status = f' (HTTP {exc.code})' if isinstance(exc, urllib.error.HTTPError) else ''
        raise RuntimeError(f'HTTPS metadata request failed{status}') from None
