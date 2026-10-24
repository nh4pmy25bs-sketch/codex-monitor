"""Validate server-issued Tailscale authentication URLs; no client-login control."""
import re
from urllib.parse import urlparse

class AuthCancelled(Exception):
    pass


def auth_url(text):
    if isinstance(text, bytes):
        text = text.decode('utf-8', 'replace')
    for candidate in re.findall(r'https://[^\s<>"\x1b]+', str(text or '')):
        candidate = candidate.rstrip('.,);\'')
        try:
            u = urlparse(candidate)
            if (u.scheme == 'https' and u.hostname == 'login.tailscale.com'
                    and u.port in (None, 443) and not u.username and not u.password
                    and not u.fragment and re.fullmatch(r'/a/[A-Za-z0-9_-]{6,256}', u.path)):
                return candidate
        except ValueError:
            pass
    return None

