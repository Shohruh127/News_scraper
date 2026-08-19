"""Pure URL policy and metadata extraction for article images (media.py).

Delegates image fetching and decoding entirely to Telegram's Bot API (sendPhoto with URL).
Performs pure URL validation (no DNS lookup, no HTTP request) to reject unsafe/private URLs.
"""

import ipaddress
import logging
import re
from urllib.parse import urljoin, urlparse

log = logging.getLogger(__name__)

# Meta tag regexes in priority order:
# 1. og:image:secure_url
# 2. og:image
# 3. twitter:image / twitter:image:src
_OG_SECURE_RE = re.compile(
    r'<meta\s+[^>]*(?:property|name)=["\']og:image:secure_url["\'][^>]*content=["\']([^"\']+)["\']'
    r'|<meta\s+[^>]*content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']og:image:secure_url["\']',
    re.IGNORECASE,
)
_OG_IMAGE_RE = re.compile(
    r'<meta\s+[^>]*(?:property|name)=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']'
    r'|<meta\s+[^>]*content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']og:image["\']',
    re.IGNORECASE,
)
_TWITTER_IMAGE_RE = re.compile(
    r'<meta\s+[^>]*(?:property|name)=["\'](?:twitter:image|twitter:image:src)["\'][^>]*content=["\']([^"\']+)["\']'
    r'|<meta\s+[^>]*content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\'](?:twitter:image|twitter:image:src)["\']',
    re.IGNORECASE,
)


def is_safe_literal_ip(hostname: str) -> bool:
    """Check whether a literal IP address hostname is public and safe."""
    try:
        # Strip square brackets for IPv6 literals e.g. [::1]
        ip_str = hostname.strip("[]")
        ip = ipaddress.ip_address(ip_str)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False
        return True
    except ValueError:
        # Not a literal IP address; domain name validation applies
        return True


def validate_image_url(url: str | None, max_length: int = 2048) -> str | None:
    """Pure URL policy validation function (no DNS lookup, no HTTP request).

    Accepts:
    - HTTP and HTTPS URLs with valid hosts.
    - Capped at max_length (default 2048).

    Rejects:
    - Non-HTTP/HTTPS schemes (file:, javascript:, data:, ftp:, etc.).
    - Empty, missing, or whitespace URLs.
    - URLs containing credentials (username/password).
    - Missing or invalid hosts.
    - 'localhost', '.local', or local domain variants.
    - Literal private, loopback, link-local, multicast, or reserved IPs.
    """
    if not url or not isinstance(url, str) or not url.strip():
        return None

    cleaned = url.strip()
    if len(cleaned) > max_length:
        return None

    try:
        parsed = urlparse(cleaned)
    except Exception:
        return None

    if parsed.scheme.lower() not in ("http", "https"):
        return None

    if not parsed.netloc:
        return None

    # Reject user credentials in URL
    if parsed.username or parsed.password:
        return None

    hostname = parsed.hostname
    if not hostname:
        return None

    host_lower = hostname.lower()

    # Reject local/private domain names
    if (
        host_lower == "localhost"
        or host_lower.endswith(".localhost")
        or host_lower.endswith(".local")
    ):
        return None

    # Reject private/loopback/reserved literal IPs
    if not is_safe_literal_ip(host_lower):
        return None

    return cleaned


def extract_image_url_from_html(html: str, base_url: str = "") -> str | None:
    """Extract og:image or twitter:image from HTML, resolving relative URLs and validating."""
    if not html or not html.strip():
        return None

    # Search in precedence order
    candidate = None
    for pattern in (_OG_SECURE_RE, _OG_IMAGE_RE, _TWITTER_IMAGE_RE):
        match = pattern.search(html)
        if match:
            candidate = match.group(1) or match.group(2)
            if candidate and candidate.strip():
                candidate = candidate.strip()
                break

    if not candidate:
        return None

    # Resolve relative URL against base_url
    if base_url:
        candidate = urljoin(base_url, candidate)

    return validate_image_url(candidate)


def get_safe_image_log_host(url: str | None) -> str:
    """Extract host safely without query parameters or credentials for logging."""
    if not url:
        return "none"
    try:
        parsed = urlparse(url)
        return parsed.hostname or "unknown"
    except Exception:
        return "invalid"
