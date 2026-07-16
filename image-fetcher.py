#!/usr/bin/env python3
"""
Fleet-Agnostic Image Fetcher
=============================

Component 1 of the Viator Image Pipeline (see /tmp/viator-image-pipeline-spec.md).

Downloads product hero images for affiliate fleet sites. Configured entirely
through sites.yaml — no Viator-specific paths are baked into the fetcher
itself. The Viator API is the DEFAULT source adapter, but any source that
implements the SourceAdapter protocol can be plugged in per site.

For each site in the config, for each product:
  - Query the source adapter for image metadata
  - Skip if no image available (status=missing)
  - Skip if image is AI-generated (Hypatia: trust gamble)
  - Skip if cached image on disk is younger than cache_max_age_days
  - Flag if the product is sunsetted (status=sunsetted) — written to a review queue
  - Otherwise download the largest cover image to {site}/images/{productCode}.jpg

Rate limiting is per-site (1 req/sec, 100/day defaults; both configurable).
Dry-run mode reports what would happen without downloading or burning tokens.

Usage
-----
    # Dry-run preview of all sites in sites.yaml
    python3 image-fetcher.py --config ~/.hermes/affiliate-crons/config/sites.yaml \\
                             --dry-run

    # Actually fetch
    python3 image-fetcher.py --config ~/.hermes/affiliate-crons/config/sites.yaml

    # Only one site
    python3 image-fetcher.py --config ... --site porto-sommelier

    # Custom config path (e.g. Hanumanhermes fleet)
    python3 image-fetcher.py --config ~/hanumanhermes/sites.yaml
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required. pip install pyyaml", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


log = logging.getLogger("image-fetcher")


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


# ---------------------------------------------------------------------------
# Config data classes
# ---------------------------------------------------------------------------


@dataclass
class ProductRef:
    """A single product reference for a site."""
    code: str
    # Optional override URL — if provided, skip the source adapter entirely.
    override_url: Optional[str] = None


@dataclass
class RateLimit:
    requests_per_second: float
    max_per_day: int


@dataclass
class Site:
    site_id: str
    name: str
    local_path: str
    source: str
    rate_limit: RateLimit
    products: list[ProductRef] = field(default_factory=list)


@dataclass
class FleetConfig:
    fleet_name: str
    sites: list[Site]
    cache_max_age_days: int = 30
    default_source: str = "viator"
    default_rate_limit: RateLimit = field(default_factory=lambda: RateLimit(1.0, 100))


# ---------------------------------------------------------------------------
# Source adapter protocol
# ---------------------------------------------------------------------------


class SourceAdapter:
    """Protocol every product-source adapter must implement.

    `get_images(product_code)` returns one of:
      {"status": "ok", "images": [{"url": ..., "is_cover": ..., "width": ...,
                                   "height": ..., "source": ...}, ...]}
      {"status": "missing"}      — no image available, skip + log
      {"status": "sunsetted"}    — product no longer exists, flag for review
    """

    def get_images(self, product_code: str) -> dict:  # pragma: no cover - protocol
        raise NotImplementedError


class ViatorAdapter(SourceAdapter):
    """Default adapter — queries Viator partner API for product images.

    Follows the same pattern as the existing fetch_product_images.py:
      GET /partner/products/{code}  →  images[] with variants[].
    Picks the LARGEST variant by area (Rule #46a from the prior implementation).
    """

    API_BASE = "https://api.viator.com/partner"

    def __init__(self, api_key: str, api_base: Optional[str] = None,
                 timeout: float = 15.0):
        self.api_key = api_key
        self.api_base = api_base or self.API_BASE
        self.timeout = timeout

    def get_images(self, product_code: str) -> dict:
        url = f"{self.api_base}/products/{product_code}"
        req = Request(url)
        req.add_header("exp-api-key", self.api_key)
        req.add_header("Accept", "application/json;version=2.0")
        req.add_header("Accept-Language", "en")
        req.add_header("Content-Type", "application/json")
        try:
            resp = urlopen(req, timeout=self.timeout)
            data = json.loads(resp.read().decode())
        except HTTPError as e:
            if e.code == 404:
                return {"status": "sunsetted"}
            log.warning("Viator API HTTP %s for %s", e.code, product_code)
            return {"status": "missing"}
        except (URLError, TimeoutError, json.JSONDecodeError) as e:
            log.warning("Viator API error for %s: %s", product_code, e)
            return {"status": "missing"}

        images = data.get("images") or []
        if not images:
            return {"status": "missing"}

        result = []
        for img in images:
            variants = img.get("variants") or []
            if not variants:
                continue
            # Pick the largest variant by area — Rule #46a.
            best = max(variants,
                       key=lambda v: (v.get("height") or 0) * (v.get("width") or 0))
            result.append({
                "url": best["url"],
                "width": best.get("width", 0),
                "height": best.get("height", 0),
                "is_cover": bool(img.get("isCover")),
                "source": img.get("imageSource", ""),
            })
        if not result:
            return {"status": "missing"}
        return {"status": "ok", "images": result}


# Registry — map source name → adapter class.
# Other fleets register their own adapters (e.g. Hanumanhermes's "custom_json").
ADAPTER_REGISTRY: dict[str, type[SourceAdapter]] = {
    "viator": ViatorAdapter,
}


def build_adapter(source_name: str, config_sources: dict) -> SourceAdapter:
    """Resolve a source name to an adapter instance.

    config_sources comes from sites.yaml's `sources:` block. For 'viator' we
    read the API key from ~/.hermes/.env (matches the existing fleet convention).
    Other adapters are free to read their own env vars.
    """
    if source_name == "viator":
        api_key = _load_viator_api_key()
        overrides = config_sources.get("viator", {}) or {}
        return ViatorAdapter(api_key=api_key, api_base=overrides.get("api_base"))
    raise ValueError(
        f"Unknown source adapter: '{source_name}'. "
        f"Registered: {sorted(ADAPTER_REGISTRY)}. "
        f"Add a custom adapter to image-fetcher.ADAPTER_REGISTRY and rerun."
    )


def _load_viator_api_key() -> str:
    """Load Viator API key from ~/.hermes/.env (matches fleet convention)."""
    env_path = os.path.expanduser("~/.hermes/.env")
    if not os.path.exists(env_path):
        raise SystemExit("ERROR: ~/.hermes/.env not found")
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("VIATOR_API_KEY="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                if val:
                    return val
    raise SystemExit("ERROR: VIATOR_API_KEY not found in ~/.hermes/.env")


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


def load_config(path: str) -> FleetConfig:
    """Load and validate an image-sites.yaml file.

    Keys starting with `_` are treated as comments and ignored.
    """
    with open(path) as f:
        data = yaml.safe_load(f) or {}

    fleet_block = data.get("fleet") or {}
    default_rl = RateLimit(
        requests_per_second=float(
            (fleet_block.get("default_rate_limit") or {}).get("requests_per_second", 1.0)
        ),
        max_per_day=int(
            (fleet_block.get("default_rate_limit") or {}).get("max_per_day", 100)
        ),
    )
    config = FleetConfig(
        fleet_name=fleet_block.get("name", "unnamed-fleet"),
        sites=[],
        cache_max_age_days=int(fleet_block.get("cache_max_age_days", 30)),
        default_source=fleet_block.get("default_source", "viator"),
        default_rate_limit=default_rl,
    )

    for entry in (data.get("sites") or []):
        # Skip comment keys
        if not isinstance(entry, dict):
            continue
        rl_block = entry.get("rate_limit") or {}
        if not isinstance(rl_block, dict):
            rl_block = {}
        rl = RateLimit(
            requests_per_second=float(rl_block.get("requests_per_second", default_rl.requests_per_second)),
            max_per_day=int(rl_block.get("max_per_day", default_rl.max_per_day)),
        )
        products = [
            ProductRef(code=p["code"], override_url=p.get("override_url"))
            for p in (entry.get("products") or [])
            if isinstance(p, dict) and p.get("code")
        ]
        site = Site(
            site_id=entry["name"],
            name=entry.get("display_name", entry["name"]),
            local_path=os.path.expanduser(entry["local_path"]),
            source=entry.get("source", config.default_source),
            rate_limit=rl,
            products=products,
        )
        config.sites.append(site)

    return config


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


class DailyCapExceeded(RuntimeError):
    """Raised when a site's daily request cap is exhausted."""


class RateLimiter:
    """Token-bucket-ish limiter honoring requests_per_second and a daily cap.

    `acquire(dry_run=True)` is a no-op — dry-run previews must not burn tokens.
    """

    def __init__(self, requests_per_second: float, max_per_day: int,
                 sleep_fn: Callable[[float], None] = time.sleep,
                 now_fn: Callable[[], float] = time.monotonic):
        self.rps = float(requests_per_second)
        self.max_per_day = int(max_per_day)
        self.min_interval = 1.0 / self.rps if self.rps > 0 else 0.0
        self._last_call_t: Optional[float] = None
        self._today_count = 0
        self._sleep = sleep_fn
        self._now = now_fn

    def acquire(self, dry_run: bool = False) -> None:
        if dry_run:
            return
        if self._today_count >= self.max_per_day:
            raise DailyCapExceeded(
                f"Daily cap of {self.max_per_day} reached; try again tomorrow."
            )
        now = self._now()
        if self._last_call_t is not None:
            wait = self.min_interval - (now - self._last_call_t)
            if wait > 0:
                self._sleep(wait)
        self._last_call_t = self._now()
        self._today_count += 1


# ---------------------------------------------------------------------------
# HTTP downloader (default)
# ---------------------------------------------------------------------------


def default_downloader(url: str, dest_path: str) -> tuple[bool, int]:
    """Download a URL to disk. Returns (success, bytes_written)."""
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urlopen(req, timeout=30)
        data = resp.read()
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with open(dest_path, "wb") as f:
            f.write(data)
        return True, len(data)
    except Exception as e:
        log.warning("Download failed for %s → %s: %s", url, dest_path, e)
        return False, 0


# ---------------------------------------------------------------------------
# Image fetcher
# ---------------------------------------------------------------------------


@dataclass
class SiteFetchResult:
    site_id: str
    downloaded: int = 0
    skipped_cache: int = 0
    skipped_missing: int = 0
    skipped_ai: int = 0
    flagged_sunsetted: int = 0
    would_download: int = 0  # dry-run only
    review_queue: list[str] = field(default_factory=list)


@dataclass
class ImageFetcher:
    """Drives the per-product decision + download for one site at a time.

    Dependency-injected so tests can substitute FakeAdapter / CapturingDownloader
    without touching the network.
    """
    adapter: SourceAdapter
    downloader: Callable[[str, str], tuple[bool, int]] = default_downloader
    cache_max_age_days: int = 30
    dry_run: bool = False
    now_fn: Callable[[], datetime] = datetime.now

    def _image_path(self, site: Site, product_code: str) -> str:
        return os.path.join(site.local_path, "images", f"{product_code}.jpg")

    def _is_ai_generated(self, images: list[dict]) -> bool:
        """Per Hypatia research: AI-generated documentary images are a trust gamble."""
        for img in images:
            src = (img.get("source") or "").upper()
            if "AI" in src and "GENERATED" in src:
                return True
        return False

    def _is_cache_fresh(self, path: str) -> bool:
        if not os.path.exists(path):
            return False
        age_seconds = time.time() - os.path.getmtime(path)
        return age_seconds < (self.cache_max_age_days * 86400)

    def fetch_site(self, site: Site) -> SiteFetchResult:
        result = SiteFetchResult(site_id=site.site_id)
        limiter = RateLimiter(
            requests_per_second=site.rate_limit.requests_per_second,
            max_per_day=site.rate_limit.max_per_day,
        )

        for pref in site.products:
            code = pref.code

            # 1. Direct URL override — bypass adapter
            if pref.override_url:
                images = [{"url": pref.override_url, "is_cover": True,
                           "width": 0, "height": 0, "source": "OVERRIDE"}]
                adapter_status = "ok"
            else:
                try:
                    limiter.acquire(dry_run=self.dry_run)
                except DailyCapExceeded as e:
                    log.warning("[%s] %s — stopping site", site.site_id, e)
                    break
                resp = self.adapter.get_images(code)
                adapter_status = resp.get("status", "missing")
                if adapter_status == "ok":
                    images = resp.get("images", [])
                else:
                    images = []

            # 2. Sunsetted → flag for review (don't download)
            if adapter_status == "sunsetted":
                result.flagged_sunsetted += 1
                result.review_queue.append(code)
                log.warning("[%s] %s: SUNSETTED → flagged for review",
                            site.site_id, code)
                continue

            # 3. No image → skip + log
            if adapter_status != "ok" or not images:
                result.skipped_missing += 1
                log.info("[%s] %s: no image available — skip",
                         site.site_id, code)
                continue

            # 4. AI-generated → skip per Hypatia research
            if self._is_ai_generated(images):
                result.skipped_ai += 1
                log.info("[%s] %s: AI-generated image — skip per Hypatia policy",
                         site.site_id, code)
                continue

            # 5. Cache hit → skip
            dest = self._image_path(site, code)
            if self._is_cache_fresh(dest):
                result.skipped_cache += 1
                log.debug("[%s] %s: cache fresh — skip", site.site_id, code)
                continue

            # 6. Pick the cover image (or first available).
            chosen = next((i for i in images if i.get("is_cover")), images[0])

            if self.dry_run:
                result.would_download += 1
                log.info("[%s] %s: would download %s",
                         site.site_id, code, chosen["url"])
                continue

            ok, size = self.downloader(chosen["url"], dest)
            if ok:
                result.downloaded += 1
                log.info("[%s] %s: downloaded %s (%d bytes)",
                         site.site_id, code, chosen["url"], size)
            else:
                result.skipped_missing += 1
                log.warning("[%s] %s: download failed — counted as missing",
                            site.site_id, code)

        return result


# ---------------------------------------------------------------------------
# Multi-site orchestrator
# ---------------------------------------------------------------------------


def run(config: FleetConfig, adapter: SourceAdapter,
        downloader: Callable[[str, str], tuple[bool, int]] = default_downloader,
        dry_run: bool = False,
        now_fn: Callable[[], datetime] = datetime.now) -> dict:
    """Process every site in the config. Returns an aggregate summary dict."""
    fetcher = ImageFetcher(
        adapter=adapter, downloader=downloader,
        cache_max_age_days=config.cache_max_age_days,
        dry_run=dry_run, now_fn=now_fn,
    )
    summary = {
        "fleet": config.fleet_name,
        "dry_run": dry_run,
        "sites_processed": 0,
        "total_downloaded": 0,
        "total_would_download": 0,
        "total_skipped_cache": 0,
        "total_skipped_missing": 0,
        "total_skipped_ai": 0,
        "total_flagged_sunsetted": 0,
        "review_queue": [],
        "per_site": {},
    }
    for site in config.sites:
        log.info("=== %s (%d products) ===", site.site_id, len(site.products))
        result = fetcher.fetch_site(site)
        summary["sites_processed"] += 1
        summary["total_downloaded"] += result.downloaded
        summary["total_would_download"] += result.would_download
        summary["total_skipped_cache"] += result.skipped_cache
        summary["total_skipped_missing"] += result.skipped_missing
        summary["total_skipped_ai"] += result.skipped_ai
        summary["total_flagged_sunsetted"] += result.flagged_sunsetted
        summary["review_queue"].extend(
            f"{site.site_id}:{code}" for code in result.review_queue
        )
        summary["per_site"][site.site_id] = {
            "downloaded": result.downloaded,
            "skipped_cache": result.skipped_cache,
            "skipped_missing": result.skipped_missing,
            "skipped_ai": result.skipped_ai,
            "flagged_sunsetted": result.flagged_sunsetted,
            "would_download": result.would_download,
        }
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _default_sites_yaml() -> str:
    """Default config path — Saraswati fleet's image-fetcher config lives here.

    NOTE: This is intentionally a different filename than config/sites.yaml
    (which is the fleet site-registry). The image fetcher needs a richer schema
    (products + per-site rate limits + source adapter) that doesn't belong in
    the shared registry.
    """
    return os.path.expanduser("~/.hermes/affiliate-crons/config/image-sites.yaml")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fleet-agnostic product image fetcher."
    )
    parser.add_argument(
        "--config", default=_default_sites_yaml(),
        help="Path to image-sites.yaml (default: ~/.hermes/affiliate-crons/config/image-sites.yaml)",
    )
    parser.add_argument(
        "--site", default=None,
        help="Only process this one site_id (default: all sites in config)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview what would be downloaded without writing files or burning rate-limit tokens.",
    )
    parser.add_argument(
        "--skip-network", action="store_true",
        help="In dry-run: skip the source-adapter HTTP calls entirely. Reports based on cache state alone.",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Only process the first N products per site (useful for previewing a config).",
    )
    parser.add_argument(
        "--summary-json", default=None,
        help="Write the run summary to this JSON path (useful for cron pipelines).",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        help="DEBUG, INFO, WARNING, ERROR (default: INFO).",
    )
    args = parser.parse_args(argv)

    _setup_logging(args.log_level)

    if not os.path.exists(args.config):
        log.error("Config not found: %s", args.config)
        return 2

    config = load_config(args.config)
    if args.site:
        config.sites = [s for s in config.sites if s.site_id == args.site]
        if not config.sites:
            log.error("Site '%s' not found in %s", args.site, args.config)
            return 2
    if args.limit:
        for s in config.sites:
            s.products = s.products[: args.limit]

    log.info("Fleet: %s — %d site(s)", config.fleet_name, len(config.sites))

    sources_block = {}
    with open(args.config) as f:
        raw = yaml.safe_load(f) or {}
    sources_block = raw.get("sources") or {}

    # Build one adapter per source, then dispatch.
    adapters: dict[str, SourceAdapter] = {}
    summary: dict = {
        "fleet": config.fleet_name,
        "dry_run": args.dry_run,
        "sites_processed": 0,
        "total_downloaded": 0,
        "total_would_download": 0,
        "total_skipped_cache": 0,
        "total_skipped_missing": 0,
        "total_skipped_ai": 0,
        "total_flagged_sunsetted": 0,
        "review_queue": [],
        "per_site": {},
    }

    # Optional: a no-network adapter wrapper that treats every product as
    # having an image. Lets dry-run preview the full fleet instantly.
    class _PreviewAdapter(SourceAdapter):
        def get_images(self, product_code):
            return {"status": "ok", "images": [
                {"url": f"<preview>{product_code}", "is_cover": True,
                 "width": 800, "height": 600, "source": "OPERATOR"}
            ]}

    for site in config.sites:
        if args.skip_network and args.dry_run:
            adapter = _PreviewAdapter()
        else:
            if site.source not in adapters:
                try:
                    adapters[site.source] = build_adapter(site.source, sources_block)
                except (ValueError, SystemExit) as e:
                    log.error("Cannot build adapter for %s: %s", site.site_id, e)
                    continue
            adapter = adapters[site.source]
        fetcher = ImageFetcher(
            adapter=adapter,
            cache_max_age_days=config.cache_max_age_days,
            dry_run=args.dry_run,
        )
        log.info("=== %s (%d products) ===", site.site_id, len(site.products))
        result = fetcher.fetch_site(site)
        summary["sites_processed"] += 1
        summary["total_downloaded"] += result.downloaded
        summary["total_would_download"] += result.would_download
        summary["total_skipped_cache"] += result.skipped_cache
        summary["total_skipped_missing"] += result.skipped_missing
        summary["total_skipped_ai"] += result.skipped_ai
        summary["total_flagged_sunsetted"] += result.flagged_sunsetted
        summary["review_queue"].extend(
            f"{site.site_id}:{code}" for code in result.review_queue
        )
        summary["per_site"][site.site_id] = {
            "downloaded": result.downloaded,
            "skipped_cache": result.skipped_cache,
            "skipped_missing": result.skipped_missing,
            "skipped_ai": result.skipped_ai,
            "flagged_sunsetted": result.flagged_sunsetted,
            "would_download": result.would_download,
        }

    log.info(
        "Done. sites=%d downloaded=%d would_download=%d "
        "skipped_cache=%d skipped_missing=%d skipped_ai=%d flagged=%d",
        summary["sites_processed"], summary["total_downloaded"],
        summary["total_would_download"], summary["total_skipped_cache"],
        summary["total_skipped_missing"], summary["total_skipped_ai"],
        summary["total_flagged_sunsetted"],
    )
    if summary["review_queue"]:
        log.warning("Review queue (sunsetted products): %s",
                    ", ".join(summary["review_queue"]))

    if args.summary_json:
        with open(args.summary_json, "w") as f:
            json.dump(summary, f, indent=2)
        log.info("Summary written to %s", args.summary_json)

    return 0


if __name__ == "__main__":
    sys.exit(main())