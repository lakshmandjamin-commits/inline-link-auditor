"""Tests for product_registry.py — content bank parser + Viator API registry builder."""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

# Add workspace to path for direct import
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import product_registry as pr


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def sample_content_bank():
    """Minimal content bank with 3 products."""
    return {
        "site": {"slug": "testsite", "name": "Test Site", "domain": "testsite.com"},
        "products": [
            {
                "viator_code": "TEST001",
                "viator_id": "TEST001",
                "title": "Test Product One",
                "destId": 921,
                "viator_url": "https://www.viator.com/tours/Jerusalem/test/d921-TEST001?pid=P00299531&mcid=42383",
                "best_for": "testing",
            },
            {
                "viator_code": "TEST002",
                "viator_id": "TEST002",
                "title": "Test Product Two",
                "destId": 511,
                "viator_url": "https://www.viator.com/tours/Rome/test/d511-TEST002?pid=P00299531&mcid=42383",
            },
            {
                "viator_code": "TEST003",
                "viator_id": "TEST003",
                "title": "Test Product Three",
            },
        ],
    }


@pytest.fixture
def sample_yaml_path(sample_content_bank):
    """Write sample content bank to temp YAML file."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(sample_content_bank, f)
    yield f.name
    os.unlink(f.name)


# ── Content Bank Parsing ─────────────────────────────────────────────────

class TestParseContentBank:
    def test_extracts_all_product_codes(self, sample_yaml_path):
        products = pr.parse_content_bank(sample_yaml_path)
        assert len(products) == 3
        codes = [p["code"] for p in products]
        assert codes == ["TEST001", "TEST002", "TEST003"]

    def test_extracts_labels(self, sample_yaml_path):
        products = pr.parse_content_bank(sample_yaml_path)
        labels = {p["code"]: p["label"] for p in products}
        assert labels["TEST001"] == "Test Product One"
        assert labels["TEST002"] == "Test Product Two"

    def test_extracts_dest_ids(self, sample_yaml_path):
        products = pr.parse_content_bank(sample_yaml_path)
        dests = {p["code"]: p["destId"] for p in products}
        assert dests["TEST001"] == 921
        assert dests["TEST002"] == 511

    def test_extracts_content_url(self, sample_yaml_path):
        products = pr.parse_content_bank(sample_yaml_path)
        assert products[0]["content_url"] == (
            "https://www.viator.com/tours/Jerusalem/test/d921-TEST001"
            "?pid=P00299531&mcid=42383"
        )

    def test_missing_destId_defaults_none(self, sample_content_bank):
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(sample_content_bank, f)
            path = f.name
        try:
            products = pr.parse_content_bank(path)
            # TEST003 has no destId
            p3 = [p for p in products if p["code"] == "TEST003"][0]
            assert p3.get("destId") is None
        finally:
            os.unlink(path)

    def test_products_missing_key_graceful(self):
        """Content bank without 'products' key returns empty list."""
        cb = {"site": {"slug": "empty"}}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(cb, f)
            path = f.name
        try:
            products = pr.parse_content_bank(path)
            assert products == []
        finally:
            os.unlink(path)

    def test_file_not_found_raises(self):
        with pytest.raises(FileNotFoundError):
            pr.parse_content_bank("/nonexistent/path.yaml")


# ── Duration Formatting ──────────────────────────────────────────────────

class TestFormatDuration:
    def test_fixed_minutes(self):
        assert pr.format_duration({"fixedDurationInMinutes": 180}) == "3h 0m"

    def test_short_duration(self):
        assert pr.format_duration({"fixedDurationInMinutes": 45}) == "45m"

    def test_variable_duration(self):
        dur = {
            "variableDurationFromMinutes": 120,
            "variableDurationToMinutes": 240,
        }
        assert pr.format_duration(dur) == "2h 0m–4h 0m"

    def test_none_duration(self):
        assert pr.format_duration(None) == ""
        assert pr.format_duration({}) == ""

    def test_itinerary_duration(self):
        """Duration from itinerary.duration.fixedDurationInMinutes."""
        dur = {"fixedDurationInMinutes": 90}
        assert pr.format_duration(dur) == "1h 30m"


# ── Price Band ───────────────────────────────────────────────────────────

class TestPriceBand:
    def test_none_price(self):
        assert pr.price_band(None) == "Unknown"

    def test_budget(self):
        assert pr.price_band(25.0) == "$"

    def test_mid_range(self):
        assert pr.price_band(75.0) == "$$"

    def test_premium(self):
        assert pr.price_band(200.0) == "$$$"

    def test_luxury(self):
        assert pr.price_band(600.0) == "$$$$"


# ── Entry Builder ────────────────────────────────────────────────────────

class TestBuildEntry:
    def test_basic_entry(self):
        product_data = {
            "productCode": "TEST001",
            "title": "Test Product One",
            "destinations": [
                {"ref": "921", "primary": True, "name": "Jerusalem"}
            ],
            "reviews": {
                "combinedAverageRating": 4.5,
                "totalReviews": 120,
            },
            "pricing": {
                "summary": {"fromPrice": 49.99, "currency": "USD"},
            },
            "duration": {"fixedDurationInMinutes": 180},
            "productUrl": "https://www.viator.com/tours/Jerusalem/test/d921-TEST001",
        }
        info = product_data.copy()
        info.update(product_data.get("pricing", {}))
        entry = pr.build_entry("TEST001", product_data)
        assert entry["title"] == "Test Product One"
        assert entry["rating"] == 4.5
        assert entry["review_count"] == 120
        assert entry["price_band"] == "$"
        assert entry["destination_name"] == "Jerusalem"
        assert "pid=P00299531" in entry["canonical_url"]

    def test_destination_fallback(self):
        """First destination used when no primary."""
        product_data = {
            "productCode": "TEST002",
            "title": "Test Two",
            "destinations": [{"ref": "511", "name": "Rome"}],
            "reviews": {},
            "pricing": {},
            "duration": None,
        }
        entry = pr.build_entry("TEST002", product_data)
        assert entry["destination_name"] == "Rome"

    def test_missing_rating(self):
        product_data = {
            "productCode": "TEST003",
            "title": "Test Three",
            "destinations": [],
            "reviews": {},
            "pricing": {},
            "duration": None,
        }
        entry = pr.build_entry("TEST003", product_data)
        assert entry["rating"] is None
        assert entry["review_count"] == 0
        assert entry["destination_name"] == ""

    def test_price_band_from_pricing_summary(self):
        product_data = {
            "productCode": "TEST004",
            "title": "Luxury Tour",
            "destinations": [],
            "reviews": {},
            "pricing": {"summary": {"fromPrice": 599.0}},
            "duration": None,
        }
        entry = pr.build_entry("TEST004", product_data)
        assert entry["price_band"] == "$$$$"

    def test_price_band_missing(self):
        product_data = {
            "productCode": "TEST005",
            "title": "No Price",
            "destinations": [],
            "reviews": {},
            "pricing": {},
            "duration": None,
        }
        entry = pr.build_entry("TEST005", product_data)
        assert entry["price_band"] == "Unknown"


# ── Title Matching ───────────────────────────────────────────────────────

class TestTitleMatch:
    def test_exact_match(self):
        assert pr.titles_match("Jerusalem Day Tour", "Jerusalem Day Tour") is True

    def test_case_insensitive(self):
        assert pr.titles_match("JERUSALEM day tour", "Jerusalem Day Tour") is True

    def test_normalized_spacing(self):
        assert pr.titles_match(
            "Jerusalem  Day   Tour", "Jerusalem Day Tour"
        ) is True

    def test_substring_ok(self):
        """API title is a substring of content bank label."""
        assert pr.titles_match(
            "Best of Jerusalem Full Day Tour from Tel Aviv",
            "Best of Jerusalem Full Day Tour",
        ) is True

    def test_clearly_different(self):
        assert pr.titles_match("Rome Food Tour", "Paris Wine Tour") is False

    def test_empty_handling(self):
        assert pr.titles_match("", "Anything") is False


# ── Auth Guard ───────────────────────────────────────────────────────────

class TestAuthGuard:
    def test_blocks_overwrite_when_existing_and_no_key(self, tmp_path):
        registry_path = tmp_path / "products.json"
        registry_path.write_text('{"TEST001": {"title": "Old Data"}}')
        with patch.object(pr, "get_api_key", return_value=""):
            result = pr.auth_guard(str(registry_path), "")
            assert result is False

    def test_allows_new_when_no_existing(self, tmp_path):
        registry_path = tmp_path / "nonexistent.json"
        result = pr.auth_guard(str(registry_path), "")
        assert result is True

    def test_allows_overwrite_when_key_present(self, tmp_path):
        registry_path = tmp_path / "products.json"
        registry_path.write_text('{"old": "data"}')
        result = pr.auth_guard(str(registry_path), "valid-key")
        assert result is True


# ── Integration: Full Registry Build (mocked API) ────────────────────────

class TestFullBuild:
    def test_builds_registry_from_content_bank(self, sample_content_bank, tmp_path):
        """Full pipeline with mocked API responses."""
        # Write content bank
        cb_path = tmp_path / "test.yaml"
        with open(cb_path, "w") as f:
            yaml.dump(sample_content_bank, f)

        registry_path = tmp_path / "products.json"
        exceptions_path = tmp_path / "exceptions.json"

        # Mock API responses
        api_responses = {
            "TEST001": {
                "productCode": "TEST001",
                "title": "Test Product One",
                "destinations": [{"ref": "921", "primary": True, "name": "Jerusalem"}],
                "reviews": {"combinedAverageRating": 4.5, "totalReviews": 120},
                "pricing": {"summary": {"fromPrice": 49.99}},
                "duration": {"fixedDurationInMinutes": 180},
                "productUrl": "https://www.viator.com/tours/Jerusalem/d921-TEST001",
            },
            "TEST002": {
                "productCode": "TEST002",
                "title": "Test Product Two",
                "destinations": [{"ref": "511", "primary": True, "name": "Rome"}],
                "reviews": {"combinedAverageRating": 4.8, "totalReviews": 85},
                "pricing": {"summary": {"fromPrice": 129.0}},
                "duration": {"variableDurationFromMinutes": 120, "variableDurationToMinutes": 240},
            },
        }

        with patch.object(pr, "fetch_product", side_effect=lambda code, key, cache=None: (
            api_responses.get(code), None if code in api_responses else "Not found"
        )):
            with patch.object(pr, "fetch_pricing", return_value=None):
                registry, exceptions = pr.build_registry(
                    str(cb_path), str(registry_path), str(exceptions_path),
                    api_key="test-key", delay=0, limit=None,
                )

        assert len(registry) == 2  # TEST003 failed (no mock response)
        assert "TEST001" in registry
        assert registry["TEST001"]["rating"] == 4.5
        assert registry["TEST001"]["price_band"] == "$"
        assert registry["TEST001"]["destination_name"] == "Jerusalem"

        # Exceptions should have TEST003
        assert len(exceptions) == 1
        assert "TEST003" in exceptions

    def test_registry_is_json_serializable(self, sample_yaml_path, tmp_path):
        registry_path = tmp_path / "products.json"
        exceptions_path = tmp_path / "exceptions.json"

        api_responses = {
            "TEST001": {
                "productCode": "TEST001",
                "title": "Test Product One",
                "destinations": [{"ref": "921", "primary": True, "name": "Jerusalem"}],
                "reviews": {},
                "pricing": {},
                "duration": None,
            },
            "TEST002": {
                "productCode": "TEST002",
                "title": "Test Product Two",
                "destinations": [{"ref": "511", "name": "Rome"}],
                "reviews": {},
                "pricing": {},
                "duration": None,
            },
            "TEST003": {
                "productCode": "TEST003",
                "title": "Test Product Three",
                "destinations": [],
                "reviews": {},
                "pricing": {},
                "duration": None,
            },
        }

        with patch.object(pr, "fetch_product", side_effect=lambda code, key, cache=None: (
            api_responses.get(code), None if code in api_responses else "Not found"
        )):
            with patch.object(pr, "fetch_pricing", return_value=None):
                registry, exceptions = pr.build_registry(
                    sample_yaml_path, str(registry_path), str(exceptions_path),
                    api_key="test-key", delay=0,
                )

        # Verify JSON round-trip
        json_str = json.dumps(registry, indent=2)
        parsed = json.loads(json_str)
        assert parsed == registry

    def test_inactive_product_to_exceptions(self, sample_yaml_path, tmp_path):
        """INACTIVE products go to exceptions, not registry."""
        registry_path = tmp_path / "products.json"
        exceptions_path = tmp_path / "exceptions.json"

        api_responses = {
            "TEST001": {
                "productCode": "TEST001",
                "title": "Active Product",
                "status": "ACTIVE",
                "destinations": [],
                "reviews": {},
                "pricing": {},
            },
            "TEST002": {
                "productCode": "TEST002",
                "title": None,
                "status": "INACTIVE",
            },
            "TEST003": {
                "productCode": "TEST003",
                "title": "Another Active",
                "status": "ACTIVE",
                "destinations": [],
                "reviews": {},
                "pricing": {},
            },
        }

        with patch.object(pr, "fetch_product", side_effect=lambda code, key, cache=None: (
            api_responses.get(code), None if code in api_responses else "Not found"
        )):
            with patch.object(pr, "fetch_pricing", return_value=None):
                registry, exceptions = pr.build_registry(
                    sample_yaml_path, str(registry_path), str(exceptions_path),
                    api_key="test-key", delay=0,
                )

        assert len(registry) == 2
        assert "TEST001" in registry
        assert "TEST003" in registry
        assert "TEST002" not in registry
        assert "TEST002" in exceptions
        assert "INACTIVE" in exceptions["TEST002"]["error"]

    def test_pricing_from_schedules_endpoint(self, sample_yaml_path, tmp_path):
        """Pricing from availability/schedules populates price_band."""
        registry_path = tmp_path / "products.json"
        exceptions_path = tmp_path / "exceptions.json"

        api_responses = {
            "TEST001": {
                "productCode": "TEST001",
                "title": "Budget Tour",
                "status": "ACTIVE",
                "destinations": [{"ref": "921", "primary": True, "name": "Jerusalem"}],
                "reviews": {},
            },
            "TEST002": {
                "productCode": "TEST002",
                "title": "Luxury Tour",
                "status": "ACTIVE",
                "destinations": [],
                "reviews": {},
            },
        }

        with patch.object(pr, "fetch_product", side_effect=lambda code, key, cache=None: (
            api_responses.get(code), None
        )):
            with patch.object(pr, "fetch_pricing", side_effect=lambda code, key: (
                {"TEST001": 25.0, "TEST002": 599.0}.get(code)
            )):
                registry, exceptions = pr.build_registry(
                    sample_yaml_path, str(registry_path), str(exceptions_path),
                    api_key="test-key", delay=0, limit=2,
                )

        assert registry["TEST001"]["price_band"] == "$"
        assert registry["TEST002"]["price_band"] == "$$$$"

    def test_pricing_failure_graceful(self, sample_yaml_path, tmp_path):
        """When pricing endpoint fails, product still included with Unknown price."""
        registry_path = tmp_path / "products.json"
        exceptions_path = tmp_path / "exceptions.json"

        api_responses = {
            "TEST001": {
                "productCode": "TEST001",
                "title": "Some Tour",
                "status": "ACTIVE",
                "destinations": [],
                "reviews": {},
            },
        }

        with patch.object(pr, "fetch_product", side_effect=lambda code, key, cache=None: (
            api_responses.get(code), None
        )):
            with patch.object(pr, "fetch_pricing", return_value=None):
                registry, exceptions = pr.build_registry(
                    sample_yaml_path, str(registry_path), str(exceptions_path),
                    api_key="test-key", delay=0, limit=1,
                )

        assert "TEST001" in registry
        assert registry["TEST001"]["price_band"] == "Unknown"


# ── CLI Argument Parsing ────────────────────────────────────────────────

class TestExtractDestFromUrl:
    def test_standard_url(self):
        url = "https://www.viator.com/tours/Jerusalem/Day-Tour/d921-CODE"
        assert pr._extract_dest_from_url(url) == "Jerusalem"

    def test_multi_word_dest(self):
        url = "https://www.viator.com/tours/Ho-Chi-Minh-City/tour/d123-CODE"
        assert pr._extract_dest_from_url(url) == "Ho Chi Minh City"

    def test_empty_url(self):
        assert pr._extract_dest_from_url("") == ""
        assert pr._extract_dest_from_url(None) == ""

    def test_no_tours_path(self):
        assert pr._extract_dest_from_url("https://example.com/other") == ""

    def test_real_product_url(self):
        url = "https://www.viator.com/tours/Jerusalem/Best-of-Jerusalem-Tour-in-a-Day-From-Jerusalem/d921-26254P67?pid=P00299531&mcid=42383"
        assert pr._extract_dest_from_url(url) == "Jerusalem"


class TestExtractPrice:
    def test_search_api_shape(self):
        data = {"pricing": {"summary": {"fromPrice": 49.99}}}
        assert pr._extract_price(data) == 49.99

    def test_product_detail_shape_no_pricing(self):
        """Product detail API: pricingInfo has no fromPrice."""
        data = {"pricingInfo": {"type": "PER_PERSON"}}
        assert pr._extract_price(data) is None

    def test_empty(self):
        assert pr._extract_price({}) is None


class TestCLIArgs:
    def test_required_args(self):
        """Test arg parser with all required args."""
        args = pr.parse_args([
            "--site", "testsite",
            "--content-bank", "/path/to/cb.yaml",
            "--output", "/path/to/out.json",
        ])
        assert args.site == "testsite"
        assert args.content_bank == "/path/to/cb.yaml"
        assert args.output == "/path/to/out.json"
        assert args.delay == 500  # default

    def test_custom_delay_and_limit(self):
        args = pr.parse_args([
            "--site", "testsite",
            "--content-bank", "/path/to/cb.yaml",
            "--output", "/path/to/out.json",
            "--delay", "1000",
            "--limit", "5",
        ])
        assert args.delay == 1000
        assert args.limit == 5
