"""canonical url normalization (plan §3).

strip tracking parameters and fragments so the same article does not
arrive twice under different utm strings, and lowercase the scheme/host.
redirect resolution is deliberately deferred to acquisition (M2), where
the article is fetched anyway and the final url is free -- resolving at
poll time would cost one request per item for feeds we otherwise never
fetch item urls from.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_EXACT = {
    "fbclid",
    "gclid",
    "gbraid",
    "wbraid",
    "msclkid",
    "mc_cid",
    "mc_eid",
    "igshid",
    "si",  # youtube share-link tracking
}
_TRACKING_PREFIXES = ("utm_",)


def canonical_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return url
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        return url  # not ours to normalize
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_EXACT and not k.lower().startswith(_TRACKING_PREFIXES)
    ]
    # fragment always dropped: it never reaches the server and is a common
    # source of spurious duplicates
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(query), "")
    )
