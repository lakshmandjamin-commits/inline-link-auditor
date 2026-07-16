"""
Tests for Fleet-Agnostic Image Fetcher (image-fetcher.py).

Covers:
  - sites.yaml parsing and validation
  - Source adapter protocol (Viator default, pluggable)
  - Per-product fetch decision (cache hit, AI skip, sunsetted flag, missing image)
  - Rate limiting (1 req/sec, daily cap)
  - Dry-run mode
  - Per-site directory creation
  - Multi-fleet config (Saraswati + Hanumanhermes)

Tests use real code with synthetic in-memory fixtures and a FakeSourceAdapter
that replaces the network — no mocks-of-mocks, no monkeypatching private
methods. The fetcher's adapter is dependency-injected so it is testable
without the network.
"""

import os
import sys
import json
import time
import tempfile
import shutil
import unittest
from pathlib import Path
from datetime import datetime, timedelta

# Make the script importable as a module. The script's filename uses a hyphen
# (image-fetcher.py) per spec, but Python module names require underscores, so
# we load it explicitly via importlib regardless of which style the file uses.
import importlib.util
SCRIPT_DIR = Path(__file__).resolve().parent.parent
_HYPHEN = SCRIPT_DIR / "image-fetcher.py"
_UNDERSCORE = SCRIPT_DIR / "image_fetcher.py"
if _UNDERSCORE.exists():
    _MODULE_PATH = _UNDERSCORE
elif _HYPHEN.exists():
    _MODULE_PATH = _HYPHEN
else:
    raise ImportError(f"image-fetcher module not found in {SCRIPT_DIR}")
sys.path.insert(0, str(SCRIPT_DIR))
_spec = importlib.util.spec_from_file_location("image_fetcher", _MODULE_PATH)
image_fetcher = importlib.util.module_from_spec(_spec)
sys.modules["image_fetcher"] = image_fetcher  # required for @dataclass on Py3.9
_spec.loader.exec_module(image_fetcher)


# ---------------------------------------------------------------------------
# Fake adapter — implements the SourceAdapter protocol
# ---------------------------------------------------------------------------


class FakeAdapter:
    """In-memory SourceAdapter that returns canned data per product code."""

    def __init__(self, mapping=None, missing=None, ai_generated=None, sunsetted=None):
        # product_code -> list[{"url": str, "is_cover": bool, "width": int, "height": int, "source": str}]
        self.mapping = mapping or {}
        self.missing = set(missing or [])
        self.ai_generated = set(ai_generated or [])
        self.sunsetted = set(sunsetted or [])
        self.calls = []  # record of (product_code,) for each call

    def get_images(self, product_code):
        self.calls.append(product_code)
        if product_code in self.missing:
            return {"status": "missing"}
        if product_code in self.sunsetted:
            return {"status": "sunsetted"}
        if product_code in self.ai_generated:
            # Image is present but flagged as AI-generated — skip per Hypatia research
            return {"status": "ok", "images": [
                {"url": f"https://example.test/{product_code}_ai.jpg",
                 "is_cover": True, "width": 800, "height": 600,
                 "source": "AI_GENERATED"}
            ]}
        return {"status": "ok", "images": self.mapping.get(product_code, [])}


class CapturingDownloader:
    """Records downloads without hitting network."""

    def __init__(self):
        self.downloaded = []  # list[(url, dest_path)]

    def __call__(self, url, dest_path):
        self.downloaded.append((url, dest_path))
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        Path(dest_path).write_bytes(b"FAKEJPEG")
        return True, len(b"FAKEJPEG")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write_minimal_sites_yaml(path, sites):
    """Write a minimal sites.yaml — used by many tests."""
    data = {
        "fleet": {
            "name": "test-fleet",
            "default_source": "viator",
            "default_rate_limit": {"requests_per_second": 1.0, "max_per_day": 100},
            "cache_max_age_days": 30,
        },
        "sources": {
            "viator": {
                "adapter": "image_fetcher.ViatorAdapter",
                "api_base": "https://api.viator.com/partner",
            },
        },
        "sites": sites,
    }
    with open(path, "w") as f:
        import yaml
        yaml.safe_dump(data, f)


def make_site(site_id, local_path, products=None, rate_limit=None, source="viator"):
    return {
        "name": site_id,
        "local_path": local_path,
        "products": products or [],
        "rate_limit": rate_limit or {"requests_per_second": 1.0, "max_per_day": 100},
        "source": source,
    }


# ---------------------------------------------------------------------------
# sites.yaml parsing
# ---------------------------------------------------------------------------


class TestSitesYamlLoading(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_loads_sites_yaml_with_multiple_sites(self):
        sites_yaml = os.path.join(self.tmp, "sites.yaml")
        write_minimal_sites_yaml(sites_yaml, [
            make_site("porto-sommelier", os.path.join(self.tmp, "porto"),
                      products=[{"code": "P1"}, {"code": "P2"}]),
            make_site("madeira-trail-guide", os.path.join(self.tmp, "madeira"),
                      products=[{"code": "M1"}]),
        ])
        config = image_fetcher.load_config(sites_yaml)
        self.assertEqual(config.fleet_name, "test-fleet")
        self.assertEqual(len(config.sites), 2)
        site_ids = {s.site_id for s in config.sites}
        self.assertEqual(site_ids, {"porto-sommelier", "madeira-trail-guide"})

    def test_loads_per_site_rate_limit(self):
        sites_yaml = os.path.join(self.tmp, "sites.yaml")
        write_minimal_sites_yaml(sites_yaml, [
            make_site("site-a", os.path.join(self.tmp, "a"),
                      rate_limit={"requests_per_second": 2.0, "max_per_day": 200}),
        ])
        config = image_fetcher.load_config(sites_yaml)
        site = config.sites[0]
        self.assertEqual(site.rate_limit.requests_per_second, 2.0)
        self.assertEqual(site.rate_limit.max_per_day, 200)

    def test_defaults_applied_when_rate_limit_missing(self):
        sites_yaml = os.path.join(self.tmp, "sites.yaml")
        write_minimal_sites_yaml(sites_yaml, [
            {"name": "site-a", "local_path": "/tmp/x", "products": []},
        ])
        config = image_fetcher.load_config(sites_yaml)
        site = config.sites[0]
        # Fleet defaults: 1 req/sec, 100/day
        self.assertEqual(site.rate_limit.requests_per_second, 1.0)
        self.assertEqual(site.rate_limit.max_per_day, 100)

    def test_handles_six_saraswati_sites(self):
        """Saraswati fleet has 6 sites — config must scale to that."""
        sites_yaml = os.path.join(self.tmp, "sites.yaml")
        site_ids = ["porto-sommelier", "madeira-trail-guide", "tenerife-outdoor-guide",
                    "lapland-adventure-guide", "san-juan-excursions", "yogyakarta-temple-tours"]
        sites = [
            make_site(sid, os.path.join(self.tmp, sid), products=[{"code": f"P{i}"}])
            for i, sid in enumerate(site_ids)
        ]
        write_minimal_sites_yaml(sites_yaml, sites)
        config = image_fetcher.load_config(sites_yaml)
        self.assertEqual(len(config.sites), 6)

    def test_handles_eight_hanumanhermes_sites(self):
        """Hanumanhermes fleet has 8 sites with different product sources."""
        sites_yaml = os.path.join(self.tmp, "sites.yaml")
        site_ids = ["onsenexperiences", "glaciericetours", "tropicaltrails",
                    "desertsafariguide", "alpinehiking", "northernlightsportal",
                    "wildlifewalks", "heritageroutes"]
        sites = [
            make_site(sid, os.path.join(self.tmp, sid),
                      products=[{"code": f"H{i}"}], source="viator")
            for i, sid in enumerate(site_ids)
        ]
        write_minimal_sites_yaml(sites_yaml, sites)
        config = image_fetcher.load_config(sites_yaml)
        self.assertEqual(len(config.sites), 8)

    def test_per_site_source_override_supported(self):
        """Two sites can use different product sources in one config."""
        sites_yaml = os.path.join(self.tmp, "sites.yaml")
        write_minimal_sites_yaml(sites_yaml, [
            make_site("viator-site", os.path.join(self.tmp, "v"), source="viator"),
            make_site("custom-site", os.path.join(self.tmp, "c"), source="custom_json"),
        ])
        config = image_fetcher.load_config(sites_yaml)
        sources = {s.site_id: s.source for s in config.sites}
        self.assertEqual(sources, {"viator-site": "viator", "custom-site": "custom_json"})

    def test_doc_underscore_keys_ignored(self):
        """Keys starting with _ are treated as comments — load_config ignores them."""
        sites_yaml = os.path.join(self.tmp, "sites.yaml")
        # Manually write a config with _doc keys
        data = {
            "fleet": {"_doc": "this is a comment", "name": "x"},
            "sources": {"viator": {"_doc": "API key in .env", "api_base": "https://x"}},
            "sites": [
                {"_doc": "first site",
                 "name": "site-a", "local_path": "/tmp/x", "products": [{"code": "P1"}]}
            ],
        }
        import yaml
        with open(sites_yaml, "w") as f:
            yaml.safe_dump(data, f)
        config = image_fetcher.load_config(sites_yaml)
        self.assertEqual(config.fleet_name, "x")
        self.assertEqual(len(config.sites), 1)
        self.assertEqual(config.sites[0].site_id, "site-a")


# ---------------------------------------------------------------------------
# Source adapter
# ---------------------------------------------------------------------------


class TestSourceAdapter(unittest.TestCase):

    def test_viator_adapter_class_exists(self):
        """Default adapter must be ViatorAdapter and export the protocol interface."""
        self.assertTrue(hasattr(image_fetcher, "ViatorAdapter"))
        adapter = image_fetcher.ViatorAdapter(api_key="dummy")
        # Must implement get_images(product_code) -> dict
        self.assertTrue(callable(getattr(adapter, "get_images", None)))

    def test_adapter_resolution_from_config(self):
        """Config can name adapters; the fetcher resolves them by name."""
        # Both 'viator' and any custom name registered in config.sources should resolve.
        self.assertIn("viator", image_fetcher.ADAPTER_REGISTRY)
        self.assertIs(image_fetcher.ADAPTER_REGISTRY["viator"], image_fetcher.ViatorAdapter)


# ---------------------------------------------------------------------------
# Fetch decision logic (per product)
# ---------------------------------------------------------------------------


class TestFetchDecision(unittest.TestCase):
    """The fetcher decides for each product: download, skip (cache), skip (AI), skip (missing), flag (sunsetted)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.adapter = FakeAdapter(mapping={
            "OK1": [
                {"url": "https://example.test/ok1.jpg", "is_cover": True,
                 "width": 800, "height": 600, "source": "OPERATOR"},
            ],
            "OK2": [
                {"url": "https://example.test/ok2.jpg", "is_cover": True,
                 "width": 800, "height": 600, "source": "OPERATOR"},
            ],
        }, missing=["MISSING1"], ai_generated=["AI1"], sunsetted=["DEAD1"])
        self.downloader = CapturingDownloader()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_fetcher(self, products, dry_run=False, cache_max_age_days=30):
        site_path = os.path.join(self.tmp, "site")
        os.makedirs(site_path, exist_ok=True)
        site = image_fetcher.Site(
            site_id="test-site",
            name="Test Site",
            local_path=site_path,
            source="fake",
            rate_limit=image_fetcher.RateLimit(1.0, 100),
            products=[image_fetcher.ProductRef(code=p) for p in products],
        )
        fetcher = image_fetcher.ImageFetcher(
            adapter=self.adapter,
            downloader=self.downloader,
            cache_max_age_days=cache_max_age_days,
            dry_run=dry_run,
            now_fn=lambda: datetime(2026, 7, 16, 12, 0, 0),
        )
        return fetcher.fetch_site(site)

    def test_downloads_image_for_product_with_images(self):
        result = self._run_fetcher(["OK1"])
        self.assertEqual(result.downloaded, 1)
        self.assertEqual(len(self.downloader.downloaded), 1)
        url, dest = self.downloader.downloaded[0]
        self.assertEqual(url, "https://example.test/ok1.jpg")
        self.assertTrue(dest.endswith("OK1.jpg"))

    def test_skips_product_with_no_image_available(self):
        result = self._run_fetcher(["MISSING1"])
        self.assertEqual(result.downloaded, 0)
        self.assertEqual(result.skipped_missing, 1)
        self.assertEqual(len(self.downloader.downloaded), 0)

    def test_skips_ai_generated_image(self):
        """Per Hypatia research, AI-generated documentary images are a trust gamble."""
        result = self._run_fetcher(["AI1"])
        self.assertEqual(result.downloaded, 0)
        self.assertEqual(result.skipped_ai, 1)
        self.assertEqual(len(self.downloader.downloaded), 0)

    def test_flags_sunsetted_product_for_review(self):
        result = self._run_fetcher(["DEAD1"])
        self.assertEqual(result.downloaded, 0)
        self.assertEqual(result.flagged_sunsetted, 1)
        self.assertIn("DEAD1", result.review_queue)

    def test_skips_cached_image_younger_than_30_days(self):
        # Pre-place a fresh cached file
        site_path = os.path.join(self.tmp, "site")
        img_dir = os.path.join(site_path, "images")
        os.makedirs(img_dir, exist_ok=True)
        cached = os.path.join(img_dir, "OK1.jpg")
        Path(cached).write_bytes(b"OLDJPEG")
        # Backdate by 10 days — under the 30-day threshold
        ten_days_ago = time.time() - 10 * 86400
        os.utime(cached, (ten_days_ago, ten_days_ago))

        result = self._run_fetcher(["OK1"])
        self.assertEqual(result.downloaded, 0)
        self.assertEqual(result.skipped_cache, 1)

    def test_refreshes_cached_image_older_than_30_days(self):
        site_path = os.path.join(self.tmp, "site")
        img_dir = os.path.join(site_path, "images")
        os.makedirs(img_dir, exist_ok=True)
        cached = os.path.join(img_dir, "OK1.jpg")
        Path(cached).write_bytes(b"STALE")
        # Backdate by 31 days — over the threshold
        thirty_one_days_ago = time.time() - 31 * 86400
        os.utime(cached, (thirty_one_days_ago, thirty_one_days_ago))

        result = self._run_fetcher(["OK1"])
        self.assertEqual(result.downloaded, 1)


# ---------------------------------------------------------------------------
# Dry-run mode
# ---------------------------------------------------------------------------


class TestDryRun(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.adapter = FakeAdapter(mapping={
            "P1": [{"url": "https://x.test/p1.jpg", "is_cover": True,
                    "width": 800, "height": 600, "source": "OPERATOR"}],
        })
        self.downloader = CapturingDownloader()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dry_run_does_not_download(self):
        site_path = os.path.join(self.tmp, "site")
        os.makedirs(site_path, exist_ok=True)
        site = image_fetcher.Site(
            site_id="dry", name="dry", local_path=site_path,
            source="fake",
            rate_limit=image_fetcher.RateLimit(1.0, 100),
            products=[image_fetcher.ProductRef(code="P1")],
        )
        fetcher = image_fetcher.ImageFetcher(
            adapter=self.adapter, downloader=self.downloader,
            cache_max_age_days=30, dry_run=True,
            now_fn=lambda: datetime(2026, 7, 16, 12, 0, 0),
        )
        result = fetcher.fetch_site(site)
        # Dry-run reports what would happen but writes nothing
        self.assertEqual(result.would_download, 1)
        self.assertEqual(result.downloaded, 0)
        self.assertEqual(len(self.downloader.downloaded), 0)
        self.assertFalse(os.path.exists(os.path.join(site_path, "images", "P1.jpg")))

    def test_dry_run_still_calls_adapter_to_get_image_metadata(self):
        """Dry-run still needs to know what would be downloaded — must call adapter."""
        site_path = os.path.join(self.tmp, "site")
        os.makedirs(site_path, exist_ok=True)
        site = image_fetcher.Site(
            site_id="dry", name="dry", local_path=site_path,
            source="fake",
            rate_limit=image_fetcher.RateLimit(1.0, 100),
            products=[image_fetcher.ProductRef(code="P1")],
        )
        fetcher = image_fetcher.ImageFetcher(
            adapter=self.adapter, downloader=self.downloader,
            cache_max_age_days=30, dry_run=True,
            now_fn=lambda: datetime(2026, 7, 16, 12, 0, 0),
        )
        fetcher.fetch_site(site)
        self.assertIn("P1", self.adapter.calls)


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class TestRateLimiter(unittest.TestCase):

    def test_token_bucket_paces_at_one_request_per_second(self):
        rl = image_fetcher.RateLimiter(requests_per_second=1.0, max_per_day=100)
        # Three tokens consumed back-to-back must take ~2 seconds
        t0 = time.monotonic()
        for _ in range(3):
            rl.acquire()
        elapsed = time.monotonic() - t0
        self.assertGreaterEqual(elapsed, 1.9)

    def test_daily_cap_blocks_after_limit(self):
        rl = image_fetcher.RateLimiter(requests_per_second=100.0, max_per_day=2)
        rl.acquire()
        rl.acquire()
        with self.assertRaises(image_fetcher.DailyCapExceeded):
            rl.acquire()

    def test_dry_run_skips_rate_limit_consumption(self):
        """Dry-run shouldn't burn real rate-limit tokens — so a dry-run + real-run pair works."""
        rl = image_fetcher.RateLimiter(requests_per_second=1.0, max_per_day=100)
        # Dry-run should be no-op on the limiter
        rl.acquire(dry_run=True)
        rl.acquire(dry_run=True)
        # Now a real call still has the full budget available instantly...
        t0 = time.monotonic()
        rl.acquire(dry_run=False)
        # ...should not have to wait the full 1 second
        self.assertLess(time.monotonic() - t0, 0.5)


# ---------------------------------------------------------------------------
# Per-site directory + naming
# ---------------------------------------------------------------------------


class TestFileOutput(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_creates_images_subdirectory_under_site_path(self):
        site_path = os.path.join(self.tmp, "site")
        os.makedirs(site_path, exist_ok=True)
        site = image_fetcher.Site(
            site_id="x", name="x", local_path=site_path, source="fake",
            rate_limit=image_fetcher.RateLimit(1.0, 100),
            products=[image_fetcher.ProductRef(code="P1")],
        )
        adapter = FakeAdapter(mapping={
            "P1": [{"url": "https://x.test/p1.jpg", "is_cover": True,
                    "width": 800, "height": 600, "source": "OPERATOR"}],
        })
        downloader = CapturingDownloader()
        fetcher = image_fetcher.ImageFetcher(
            adapter=adapter, downloader=downloader,
            cache_max_age_days=30, dry_run=False,
            now_fn=lambda: datetime(2026, 7, 16, 12, 0, 0),
        )
        fetcher.fetch_site(site)
        # File should live at {site}/images/{productCode}.jpg
        expected = os.path.join(site_path, "images", "P1.jpg")
        self.assertTrue(os.path.exists(expected), f"Expected {expected} to exist")


# ---------------------------------------------------------------------------
# Multi-site orchestration
# ---------------------------------------------------------------------------


class TestMultiSiteOrchestration(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fetches_for_each_site_independently(self):
        sites_yaml = os.path.join(self.tmp, "sites.yaml")
        site_a = os.path.join(self.tmp, "a")
        site_b = os.path.join(self.tmp, "b")
        write_minimal_sites_yaml(sites_yaml, [
            make_site("a", site_a, products=[{"code": "PA"}]),
            make_site("b", site_b, products=[{"code": "PB"}]),
        ])
        adapter = FakeAdapter(mapping={
            "PA": [{"url": "https://x.test/pa.jpg", "is_cover": True,
                    "width": 800, "height": 600, "source": "OPERATOR"}],
            "PB": [{"url": "https://x.test/pb.jpg", "is_cover": True,
                    "width": 800, "height": 600, "source": "OPERATOR"}],
        })
        downloader = CapturingDownloader()
        config = image_fetcher.load_config(sites_yaml)
        summary = image_fetcher.run(
            config=config, adapter=adapter, downloader=downloader,
            dry_run=False,
            now_fn=lambda: datetime(2026, 7, 16, 12, 0, 0),
        )
        self.assertEqual(summary["sites_processed"], 2)
        self.assertEqual(summary["total_downloaded"], 2)
        self.assertTrue(os.path.exists(os.path.join(site_a, "images", "PA.jpg")))
        self.assertTrue(os.path.exists(os.path.join(site_b, "images", "PB.jpg")))

    def test_summary_aggregates_decisions_across_sites(self):
        sites_yaml = os.path.join(self.tmp, "sites.yaml")
        site_a = os.path.join(self.tmp, "a")
        write_minimal_sites_yaml(sites_yaml, [
            make_site("a", site_a, products=[
                {"code": "OK"}, {"code": "MISS"}, {"code": "AI"}, {"code": "DEAD"}
            ]),
        ])
        adapter = FakeAdapter(
            mapping={"OK": [{"url": "https://x.test/ok.jpg", "is_cover": True,
                             "width": 800, "height": 600, "source": "OPERATOR"}]},
            missing=["MISS"],
            ai_generated=["AI"],
            sunsetted=["DEAD"],
        )
        downloader = CapturingDownloader()
        config = image_fetcher.load_config(sites_yaml)
        summary = image_fetcher.run(
            config=config, adapter=adapter, downloader=downloader,
            dry_run=False,
            now_fn=lambda: datetime(2026, 7, 16, 12, 0, 0),
        )
        self.assertEqual(summary["total_downloaded"], 1)
        self.assertEqual(summary["total_skipped_missing"], 1)
        self.assertEqual(summary["total_skipped_ai"], 1)
        self.assertEqual(summary["total_flagged_sunsetted"], 1)


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


class TestCLI(unittest.TestCase):

    def test_cli_module_exposes_main(self):
        self.assertTrue(callable(getattr(image_fetcher, "main", None)))


if __name__ == "__main__":
    unittest.main()