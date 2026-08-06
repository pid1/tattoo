"""canonical url rules (plan §3): tracking params and fragments stripped,
host lowercased, real query params preserved."""

from tattoo.urlnorm import canonical_url


def test_utm_params_stripped():
    url = "https://example.com/post?utm_source=rss&utm_medium=feed&id=42"
    assert canonical_url(url) == "https://example.com/post?id=42"


def test_click_ids_stripped():
    url = "https://example.com/a?fbclid=xyz&gclid=abc&si=share123"
    assert canonical_url(url) == "https://example.com/a"


def test_fragment_dropped():
    assert canonical_url("https://example.com/a#section-2") == "https://example.com/a"


def test_host_and_scheme_lowercased_path_preserved():
    assert canonical_url("HTTPS://Example.COM/Path/A") == "https://example.com/Path/A"


def test_meaningful_query_preserved():
    url = "https://www.youtube.com/watch?v=abc123"
    assert canonical_url(url) == url


def test_non_http_left_alone():
    assert canonical_url("mailto:x@example.com") == "mailto:x@example.com"


def test_empty_and_whitespace():
    assert canonical_url("") == ""
    assert canonical_url("  https://example.com/a  ") == "https://example.com/a"
