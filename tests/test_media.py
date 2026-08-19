"""Tests for pure media URL policy and metadata extraction (apps/digest/media.py)."""

import logging

from apps.digest import media


def test_extract_image_url_precedence():
    """og:image:secure_url takes precedence over og:image and twitter:image."""
    html = """
    <html>
    <head>
        <meta property="twitter:image" content="https://example.com/twitter.jpg">
        <meta property="og:image" content="https://example.com/og.jpg">
        <meta property="og:image:secure_url" content="https://example.com/secure.jpg">
    </head>
    </html>
    """
    assert media.extract_image_url_from_html(html) == "https://example.com/secure.jpg"


def test_extract_image_url_og_and_twitter_fallbacks():
    """Falls back to og:image and twitter:image when secure_url is absent."""
    html_og = (
        '<html><head><meta property="og:image" content="https://example.com/og.jpg"></head></html>'
    )
    assert media.extract_image_url_from_html(html_og) == "https://example.com/og.jpg"

    html_tw = (
        '<html><head><meta name="twitter:image" content="https://example.com/tw.jpg"></head></html>'
    )
    assert media.extract_image_url_from_html(html_tw) == "https://example.com/tw.jpg"


def test_extract_image_url_resolves_relative_urls():
    """Relative URLs are resolved against base_url."""
    html = '<html><head><meta property="og:image" content="/assets/cover.png"></head></html>'
    res = media.extract_image_url_from_html(html, base_url="https://example.com/blog/post-1")
    assert res == "https://example.com/assets/cover.png"


def test_extract_image_url_returns_none_for_missing_or_invalid():
    """Returns None when no meta tag exists or scheme is invalid."""
    assert media.extract_image_url_from_html("<html><body>No image</body></html>") is None

    html_js = '<html><head><meta property="og:image" content="javascript:alert(1)"></head></html>'
    assert media.extract_image_url_from_html(html_js) is None


def test_validate_image_url_accepts_public_http_and_https():
    """validate_image_url accepts public HTTP and HTTPS URLs."""
    assert (
        media.validate_image_url("https://images.example.com/photo.jpg")
        == "https://images.example.com/photo.jpg"
    )
    assert (
        media.validate_image_url("http://cdn.example.org/images/cover.png?size=large")
        == "http://cdn.example.org/images/cover.png?size=large"
    )


def test_validate_image_url_rejects_unsafe_and_private_urls():
    """validate_image_url rejects loopback, private IPs, localhost, credentials, and non-http."""
    # Literal private and loopback IPs
    assert media.validate_image_url("http://127.0.0.1/image.png") is None
    assert media.validate_image_url("http://[::1]/image.png") is None
    assert media.validate_image_url("http://192.168.1.1/image.png") is None
    assert media.validate_image_url("http://10.0.0.5/image.png") is None
    assert media.validate_image_url("http://172.16.0.1/image.png") is None

    # Localhost and local domain names
    assert media.validate_image_url("http://localhost/image.png") is None
    assert media.validate_image_url("http://app.localhost/image.png") is None
    assert media.validate_image_url("http://service.local/image.png") is None

    # Embedded credentials
    assert media.validate_image_url("http://user:pass@example.com/image.png") is None

    # Non-HTTP schemes
    assert media.validate_image_url("ftp://example.com/image.png") is None
    assert media.validate_image_url("file:///etc/passwd") is None
    assert media.validate_image_url("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAA") is None

    # Empty, whitespace, oversized
    assert media.validate_image_url("") is None
    assert media.validate_image_url("   ") is None
    assert media.validate_image_url("https://example.com/" + "a" * 2500, max_length=2048) is None


def test_get_safe_image_log_host_never_leaks_query_params():
    """get_safe_image_log_host returns only the hostname, never query params or tokens."""
    url = "https://cdn.example.com/images/123.jpg?token=secret123&signature=abc456"
    host = media.get_safe_image_log_host(url)
    assert host == "cdn.example.com"
    assert "token" not in host
    assert "secret123" not in host
    assert "signature" not in host

    assert media.get_safe_image_log_host(None) == "none"
    assert media.get_safe_image_log_host("") == "none"


def test_safe_logging_in_action(caplog):
    """Logging image host does not print URL parameters."""
    with caplog.at_level(logging.INFO):
        url = "https://secret-cdn.com/img.jpg?access_key=super_secret"
        host = media.get_safe_image_log_host(url)
        logging.getLogger("test").info("Processing image host: %s", host)

    assert "secret-cdn.com" in caplog.text
    assert "access_key" not in caplog.text
    assert "super_secret" not in caplog.text
