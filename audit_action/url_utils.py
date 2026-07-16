"""
url_utils — URL <-> site mapping and URL file loading.

This module owns every URL-parse + site-lookup concern so the orchestrator
doesn't have to. The previous implementation had a critical bug:

    host = parsed.netloc.lower().lstrip("www.")

Python's ``str.lstrip`` treats its argument as a *character set* and strips
any combination of leading characters. So ``"west.com".lstrip("www.")`` returns
``"est.com"`` — every leading ``w`` and ``.`` is consumed. The fix is
``removeprefix`` (Python 3.9+), which strips the literal prefix only.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:  # pragma: no cover — type-checking only, avoids import cycle
    from .site_registry import SiteConfig


def strip_www(host: str) -> str:
    """
    Strip a leading ``www.`` from a hostname (lowercase).

    Uses ``removeprefix`` (Python 3.9+) so only the literal string ``"www."``
    is removed — characters like ``"w"`` and ``"."`` are *not* treated as a
    strip-set.

    Examples:
        >>> strip_www("www.example.com")
        'example.com'
        >>> strip_www("west.com")
        'west.com'
        >>> strip_www("WWW.example.com")
        'example.com'
    """
    return host.lower().removeprefix("www.")


def load_urls(path: Path) -> list[str]:
    """Read URLs from a file (one per line, '#' = comment)."""
    urls: list[str] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        urls.append(line)
    return urls


def url_to_site(url: str, sites: "dict[str, SiteConfig]") -> str | None:
    """
    Match a URL to a site slug by hostname or by site path prefix.

    Strategy (in order):
        1. Exact domain match (www. is stripped from both sides).
        2. Path-prefix fallback for file:// and absolute paths.
        3. Fuzzy hostname match on the bare domain (first label).

    The ``www.`` strip uses :func:`strip_www`, which is the prefix-safe
    replacement for the previous ``lstrip("www.")`` character-set call.
    """
    parsed = urlparse(url)
    host = strip_www(parsed.netloc)

    # 1) Match by domain
    for slug, cfg in sites.items():
        if cfg.domain and strip_www(cfg.domain) == host:
            return slug

    # 2) Fallback: path-based heuristic. Local files have file:// or
    #    absolute paths.
    for slug, cfg in sites.items():
        if cfg.path and url.startswith(str(cfg.path)):
            return slug
        if cfg.path and cfg.path.name in url:
            return slug

    # 3) Hostname fuzzy match (e.g. "porto-wine-tours.com" → "porto-sommelier")
    for slug, cfg in sites.items():
        if cfg.domain:
            d = strip_www(cfg.domain)
            if d.split(".")[0] in host:
                return slug

    return None


def url_to_local_file(url: str, cfg: "SiteConfig") -> Path | None:
    """
    Translate a public URL into a local file path under ``cfg.path``. Handles:
      - file:///abs/path
      - file://localhost/abs/path
      - https://domain.tld/...      → <cfg.path>/...
      - already absolute path       → as-is
    """
    parsed = urlparse(url)
    if parsed.scheme == "file":
        return Path(parsed.path)
    if parsed.scheme in ("", "file"):
        return Path(url)
    if parsed.scheme in ("http", "https"):
        if not cfg.path:
            return None
        # strip leading slash so we don't end up at site root + abs-path
        rel = parsed.path.lstrip("/")
        return cfg.path / rel
    return None
