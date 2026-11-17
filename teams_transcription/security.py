"""Public HTTPS/WSS destination policy for native Live; no caller authentication."""
import ipaddress
import socket
from urllib.parse import urlsplit


def validate_destination(url,allowed_hosts,resolver=socket.getaddrinfo):
    """Validate every HTTP/WebSocket destination, including every DNS answer."""
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in {'https','wss'} or parsed.username or parsed.password
                or parsed.port not in {None,443} or parsed.hostname not in allowed_hosts):
            raise ValueError()
        answers = resolver(parsed.hostname,443,type=socket.SOCK_STREAM)
        if not answers or any(not ipaddress.ip_address(answer[4][0]).is_global for answer in answers):
            raise ValueError()
        return parsed.hostname
    except (ValueError,TypeError,OSError):
        raise ValueError('egress_denied') from None
