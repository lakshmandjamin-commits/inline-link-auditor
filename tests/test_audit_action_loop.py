"""
Tests for Audit-Action Loop (audit-action-loop.py).

Covers:
  - sites.yaml loading (PyYAML path AND minimal-parser fallback)
  - URL → site mapping (by domain, by path, fuzzy hostname)
  - R12 detection from audit results
  - Local product image lookup (jpg/jpeg/png/webp, first match wins)
  - Product code extraction from HTML
  - <img> injection into HTML (idempotent, anchor selection)
  - Queue append (de-dupe across re-runs)
  - Page-action decisioning (inject vs queue vs manual curation)
  - Fleet filtering (saraswati vs hanumanhermes)
  - End-to-end pipeline with a cached audit JSON (no Playwright needed)

All tests run without Playwright or a real audit tool — we feed in synthetic
audit JSON and assert on the resulting LoopReport.
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import importlib.util
from pathlib import Path
from unittest import mock

# Load the script as a module (hyphenated filenames need importlib).
SCRIPT_DIR = Path(__file__).resolve().parent.parent
SCRIPT_PATH = SCRIPT_DIR / "audit-action-loop.py"
assert SCRIPT_PATH.exists(), f"audit-action-loop.py not found at {SCRIPT_PATH}"
sys.path.insert(0, str(SCRIPT_DIR))
_spec = importlib.util.spec_from_file_location("audit_action_loop", SCRIPT_PATH)
aal = importlib.util.module_from_spec(_spec)
sys.modules["audit_action_loop"] = aal  # dataclasses + dataclass fields
_spec.loader.exec_module(aal)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_audit_result(url, page_type="review", r12=False, total_images=0,
                      status="ok", error=None, extra_violations=None):
    """Build a synthetic audit-result dict in the shape the audit CLI emits."""
    violations = []
    if r12:
        violations.append({
            "rule_id": "R12",
            "severity": "critical",
            "evidence_tier": "T1",
            "image_src": None,
            "description": "Page has zero images.",
            "suggestion": "Add at least one image.",
        })
    if extra_violations:
        violations.extend(extra_violations)
    out = {
        "url": url,
        "status": status,
        "page_type": page_type,
        "total_images": total_images,
        "violations": violations,
        "summary": {"critical": int(r12)},
    }
    if error is not None:
        out["error"] = error
    return out


def write_minimal_sites_yaml(path, sites_dict, queue_dir=None, log_dir=None):
    """Write a sites.yaml matching the script's expected schema."""
    data = {
        "sites": {
            slug: {
                "path": cfg["path"],
                "domain": cfg.get("domain", f"{slug}.example.com"),
                "fleet": cfg.get("fleet", "saraswati"),
                "languages": cfg.get("languages", ["en"]),
            }
            for slug, cfg in sites_dict.items()
        }
    }
    if queue_dir:
        data["queue"] = {"dir": queue_dir}
    if log_dir:
        data["logs"] = {"dir": log_dir}
    import yaml
    with open(path, "w") as f:
        yaml.safe_dump(data, f)


def make_fake_site(tmp, slug, files=None, images=None, fleet="saraswati",
                   domain=None):
    """Create a fake site directory with optional index.html files and images."""
    site_path = Path(tmp) / slug
    site_path.mkdir(parents=True, exist_ok=True)
    images_dir = site_path / "images"
    images_dir.mkdir(exist_ok=True)
    for fn in (images or []):
        (images_dir / fn).write_bytes(b"FAKEJPEG")
    for rel_path, content in (files or {}).items():
        p = site_path / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return SiteConfigFake(path=site_path, images_dir=images_dir,
                          domain=domain or f"{slug}.example.com", fleet=fleet)


class SiteConfigFake:
    def __init__(self, path, images_dir, domain, fleet):
        self.path = path
        self.images_dir = images_dir
        self.domain = domain
        self.fleet = fleet


# ---------------------------------------------------------------------------
# sites.yaml loading
# ---------------------------------------------------------------------------


class TestSitesYamlLoading(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.yaml_path = Path(self.tmp) / "sites.yaml"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_loads_via_pyyaml_when_available(self):
        write_minimal_sites_yaml(self.yaml_path, {
            "porto-sommelier": {"path": "/tmp/porto"},
            "madeira-trail-guide": {"path": "/tmp/madeira"},
        })
        sites = aal.load_site_registry(self.yaml_path)
        self.assertEqual(set(sites.keys()), {"porto-sommelier", "madeira-trail-guide"})
        self.assertEqual(sites["porto-sommelier"].domain, "porto-sommelier.example.com")
        self.assertEqual(sites["porto-sommelier"].fleet, "saraswati")
        self.assertEqual(sites["porto-sommelier"].images_dir, Path("/tmp/porto/images"))

    def test_loads_via_minimal_parser_when_pyyaml_missing(self):
        # Hand-craft YAML without PyYAML — tests the fallback parser.
        text = """
sites:
  porto-sommelier:
    path: /tmp/porto
    domain: porto-wine-tours.com
    fleet: saraswati
    languages:
      - en
  vinesandplates:
    path: /tmp/vines
    domain: www.vinesandplates.com
    fleet: hanumanhermes
"""
        self.yaml_path.write_text(text)
        # Simulate PyYAML missing
        with mock.patch.dict(sys.modules, {"yaml": None}):
            sites = aal.load_site_registry(self.yaml_path)
        self.assertEqual(set(sites.keys()), {"porto-sommelier", "vinesandplates"})
        self.assertEqual(sites["porto-sommelier"].fleet, "saraswati")
        self.assertEqual(sites["vinesandplates"].fleet, "hanumanhermes")

    def test_site_filter_selects_one_site(self):
        write_minimal_sites_yaml(self.yaml_path, {
            "porto-sommelier": {"path": "/tmp/porto"},
            "madeira-trail-guide": {"path": "/tmp/madeira"},
        })
        sites = aal.load_site_registry(self.yaml_path, site_filter="porto-sommelier")
        self.assertEqual(list(sites.keys()), ["porto-sommelier"])

    def test_fleet_filter_selects_by_fleet(self):
        write_minimal_sites_yaml(self.yaml_path, {
            "porto-sommelier": {"path": "/tmp/porto", "fleet": "saraswati"},
            "vinesandplates": {"path": "/tmp/vines", "fleet": "hanumanhermes"},
        })
        sites = aal.load_site_registry(self.yaml_path, fleet_filter="hanumanhermes")
        self.assertEqual(list(sites.keys()), ["vinesandplates"])

    def test_paused_sites_are_excluded(self):
        text = """
sites:
  active:
    path: /tmp/active
  paused:
    path: /tmp/paused
    paused: true
"""
        self.yaml_path.write_text(text)
        with mock.patch.dict(sys.modules, {"yaml": None}):
            sites = aal.load_site_registry(self.yaml_path)
        self.assertNotIn("paused", sites)

    def test_missing_file_raises_filenotfound(self):
        with self.assertRaises(FileNotFoundError):
            aal.load_site_registry(Path(self.tmp) / "does-not-exist.yaml")


# ---------------------------------------------------------------------------
# URL → site mapping
# ---------------------------------------------------------------------------


class TestUrlToSite(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.yaml_path = Path(self.tmp) / "sites.yaml"
        write_minimal_sites_yaml(self.yaml_path, {
            "porto-sommelier": {
                "path": str(Path(self.tmp) / "porto-wine-tours"),
                "domain": "porto-wine-tours.com",
                "fleet": "saraswati",
            },
            "vinesandplates": {
                "path": str(Path(self.tmp) / "vinesandplates"),
                "domain": "www.vinesandplates.com",
                "fleet": "hanumanhermes",
            },
        })
        self.sites = aal.load_site_registry(self.yaml_path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_matches_by_domain(self):
        slug = aal.url_to_site("https://porto-wine-tours.com/douro-tour", self.sites)
        self.assertEqual(slug, "porto-sommelier")

    def test_matches_by_www_stripped_domain(self):
        slug = aal.url_to_site("https://www.vinesandplates.com/cellar-doors", self.sites)
        self.assertEqual(slug, "vinesandplates")

    def test_unknown_host_returns_none(self):
        slug = aal.url_to_site("https://example.com/foo", self.sites)
        self.assertIsNone(slug)

    def test_path_prefix_fallback(self):
        path = str(Path(self.tmp) / "porto-wine-tours" / "tours" / "index.html")
        slug = aal.url_to_site(path, self.sites)
        self.assertEqual(slug, "porto-sommelier")

    def test_strip_www_does_not_strip_charset(self):
        # Regression: ``host.lstrip("www.")`` treats its arg as a *character set*
        # and would consume any leading 'w'/'.' characters. A site whose
        # hostname starts with 'w' but isn't 'www.' (e.g. west.com, wine.co)
        # would be silently mangled into 'est.com' / 'ine.co'. The fix uses
        # ``removeprefix`` so only the literal 'www.' is removed.
        self.assertEqual(aal.strip_www("www.vinesandplates.com"),
                         "vinesandplates.com")
        self.assertEqual(aal.strip_www("west.com"), "west.com")
        self.assertEqual(aal.strip_www("wine.co"), "wine.co")
        self.assertEqual(aal.strip_www("w.example.com"), "w.example.com")
        self.assertEqual(aal.strip_www("WWW.Example.com"), "example.com")
        self.assertEqual(aal.strip_www("plain.com"), "plain.com")

    def test_url_to_site_does_not_mangle_hostnames_with_w_prefix(self):
        # End-to-end check for the lstrip('www.') character-set bug.
        # A site whose domain starts with 'w' (e.g. westwood.tours) used to be
        # silently mangled into 'estwood.tours' because lstrip treats its arg
        # as a *character set*. Add such a site directly to the in-memory
        # registry so we can exercise url_to_site without depending on YAML
        # serialisation.
        from audit_action.site_registry import SiteConfig
        self.sites["westwood"] = SiteConfig(
            slug="westwood",
            path=Path(self.tmp) / "westwood",
            domain="westwood.tours",
            fleet="saraswati",
            images_dir=Path(self.tmp) / "westwood" / "images",
        )
        slug = aal.url_to_site("https://westwood.tours/foo/bar", self.sites)
        self.assertEqual(slug, "westwood")
        # www. prefix must still strip correctly.
        slug_www = aal.url_to_site("https://www.westwood.tours/foo/bar", self.sites)
        self.assertEqual(slug_www, "westwood")


# ---------------------------------------------------------------------------
# R12 detection
# ---------------------------------------------------------------------------


class TestR12Detection(unittest.TestCase):

    def test_detects_r12_violation(self):
        result = make_audit_result("https://x.test", r12=True)
        self.assertTrue(aal.has_r12_violation(result))

    def test_no_r12_when_violation_absent(self):
        result = make_audit_result("https://x.test", r12=False, total_images=3)
        self.assertFalse(aal.has_r12_violation(result))

    def test_ignores_other_violations(self):
        result = make_audit_result("https://x.test", extra_violations=[
            {"rule_id": "R5", "severity": "major", "description": "carousel"},
            {"rule_id": "R9", "severity": "major", "description": "no dim attrs"},
        ])
        self.assertFalse(aal.has_r12_violation(result))


# ---------------------------------------------------------------------------
# Product code extraction
# ---------------------------------------------------------------------------


class TestProductCodeExtraction(unittest.TestCase):

    def test_extracts_simple_viator_link(self):
        html = 'See <a href="https://www.viator.com/Paris-tours/217270P2?pid=P1">here</a>'
        self.assertEqual(aal.extract_product_codes(html), ["217270P2"])

    def test_extracts_with_destination_prefix(self):
        html = '<a href="https://www.viator.com/tours/d1234-162160P1?pid=P1">x</a>'
        self.assertEqual(aal.extract_product_codes(html), ["162160P1"])

    def test_extracts_multiple_codes_deduped(self):
        html = '''
            <a href="https://www.viator.com/foo/333P1?x=1">a</a>
            <a href="https://www.viator.com/bar/555P2?y=2">b</a>
            <a href="https://www.viator.com/baz/333P1?z=3">c</a>
        '''
        codes = aal.extract_product_codes(html)
        self.assertEqual(codes, ["333P1", "555P2"])

    def test_no_codes_when_no_viator_link(self):
        self.assertEqual(aal.extract_product_codes("<p>no links</p>"), [])


# ---------------------------------------------------------------------------
# Local image lookup
# ---------------------------------------------------------------------------


class TestFindLocalProductImage(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.images_dir = Path(self.tmp) / "images"
        self.images_dir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_finds_jpg(self):
        (self.images_dir / "217270P2.jpg").write_bytes(b"\xff\xd8\xff\xe0")
        html = '<a href="https://www.viator.com/tours/217270P2?pid=1">x</a>'
        self.assertEqual(aal.find_local_product_image(html, self.images_dir), "217270P2.jpg")

    def test_finds_png(self):
        (self.images_dir / "XYZ.png").write_bytes(b"\x89PNG\r\n")
        html = '<a href="https://www.viator.com/tours/XYZ?pid=1">x</a>'
        self.assertEqual(aal.find_local_product_image(html, self.images_dir), "XYZ.png")

    def test_returns_none_when_no_image(self):
        html = '<a href="https://www.viator.com/tours/AAA?pid=1">x</a>'
        self.assertIsNone(aal.find_local_product_image(html, self.images_dir))

    def test_returns_none_when_no_codes_in_html(self):
        self.assertIsNone(aal.find_local_product_image("<p>no links</p>", self.images_dir))

    def test_returns_none_when_images_dir_missing(self):
        html = '<a href="https://www.viator.com/tours/217270P2?pid=1">x</a>'
        self.assertIsNone(aal.find_local_product_image(html, Path(self.tmp) / "missing"))

    def test_first_matching_code_wins(self):
        (self.images_dir / "333P1.jpg").write_bytes(b"x")
        html = '''
            <a href="https://www.viator.com/tours/555P2?pid=1">b</a>
            <a href="https://www.viator.com/tours/333P1?pid=1">a</a>
        '''
        # order in HTML is 555P2 first, 333P1 second — neither has image by default
        # but with only 333P1.jpg present, must return 333P1.jpg
        self.assertEqual(aal.find_local_product_image(html, self.images_dir), "333P1.jpg")


# ---------------------------------------------------------------------------
# <img> injection
# ---------------------------------------------------------------------------


class TestInjectImage(unittest.TestCase):

    def test_injects_before_first_viator_link(self):
        html = '''<h1>Tour Review</h1>
<a href="https://www.viator.com/tours/217270P2?pid=1">Book now</a>'''
        out = aal.inject_image_into_html(html, "217270P2.jpg")
        self.assertIn('class="injected-product-image"', out)
        # <img> must come before the <a>
        img_pos = out.index('<img')
        a_pos = out.index('<a href="https://www.viator.com')
        self.assertLess(img_pos, a_pos)
        self.assertIn('src="/images/217270P2.jpg"', out)

    def test_injects_after_h1_when_no_viator_link(self):
        html = "<h1>Tour Review</h1><p>Some content here.</p>"
        out = aal.inject_image_into_html(html, "217270P2.jpg")
        h1_end = out.index("</h1>")
        img_pos = out.index("<img")
        self.assertGreater(img_pos, h1_end)

    def test_injects_into_body_when_no_h1(self):
        html = "<body><p>Just text.</p></body>"
        out = aal.inject_image_into_html(html, "X.jpg")
        self.assertIn('<img src="/images/X.jpg"', out)

    def test_idempotent_no_double_inject(self):
        html = '<h1>X</h1><a href="https://www.viator.com/tours/217270P2?pid=1">x</a>'
        once = aal.inject_image_into_html(html, "217270P2.jpg")
        twice = aal.inject_image_into_html(once, "217270P2.jpg")
        self.assertEqual(once, twice)
        self.assertEqual(twice.count('class="injected-product-image"'), 1)

    def test_alt_text_escaped(self):
        html = '<h1>X</h1><a href="https://www.viator.com/tours/ABC?pid=1">x</a>'
        out = aal.inject_image_into_html(html, "X.jpg", alt='Wine "Tasting" & Tours')
        self.assertIn("alt=\"Wine &quot;Tasting&quot; &amp; Tours\"", out)

    def test_inject_into_file_writes_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "page.html"
            original = '<h1>T</h1><a href="https://www.viator.com/tours/ABC?pid=1">x</a>'
            f.write_text(original)
            changed = aal.inject_image_into_file(f, "ABC.jpg")
            self.assertTrue(changed)
            self.assertIn('<img', f.read_text())
            # No leftover .tmp file
            self.assertFalse((f.parent / "page.html.tmp").exists())

    def test_inject_into_file_no_op_when_already_injected(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "page.html"
            html_with_img = '<h1>T</h1><img src="/images/ABC.jpg" class="injected-product-image">'
            f.write_text(html_with_img)
            changed = aal.inject_image_into_file(f, "ABC.jpg")
            self.assertFalse(changed)
            self.assertEqual(f.read_text(), html_with_img)


# ---------------------------------------------------------------------------
# Queue I/O
# ---------------------------------------------------------------------------


class TestQueueAppend(unittest.TestCase):

    def test_appends_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            q = Path(tmp) / "site-20260101.queue"
            n = aal.append_to_queue(q, ["217270P2", "DEF456"])
            self.assertEqual(n, 2)
            self.assertEqual(q.read_text().strip().splitlines(), ["217270P2", "DEF456"])

    def test_dedupes_against_existing_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            q = Path(tmp) / "site.queue"
            q.write_text("217270P2\n# comment\nDEF456\n")
            n = aal.append_to_queue(q, ["217270P2", "162160P1", "def456"])
            self.assertEqual(n, 1)  # only 162160P1 new
            lines = [ln for ln in q.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
            self.assertIn("162160P1", lines)
            self.assertIn("217270P2", lines)

    def test_appends_to_nonexistent_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            q = Path(tmp) / "queues" / "sub" / "site.queue"
            n = aal.append_to_queue(q, ["X"])
            self.assertEqual(n, 1)
            self.assertTrue(q.exists())


# ---------------------------------------------------------------------------
# URL → local file
# ---------------------------------------------------------------------------


class TestUrlToLocalFile(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = aal.SiteConfig(slug="x", path=Path(self.tmp))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_http_url_to_relative(self):
        f = aal.url_to_local_file("https://example.com/tours/douro/index.html", self.cfg)
        self.assertEqual(f, Path(self.tmp) / "tours" / "douro" / "index.html")

    def test_file_url_to_absolute(self):
        f = aal.url_to_local_file(f"file:///tmp/foo/bar.html", self.cfg)
        self.assertEqual(str(f), "/tmp/foo/bar.html")

    def test_absolute_path_passthrough(self):
        f = aal.url_to_local_file("/tmp/x/y.html", self.cfg)
        self.assertEqual(str(f), "/tmp/x/y.html")


# ---------------------------------------------------------------------------
# Page-action decisioning
# ---------------------------------------------------------------------------


class TestProcessPage(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.site_dir = Path(self.tmp) / "porto-wine-tours"
        self.site_dir.mkdir(parents=True)
        self.images_dir = self.site_dir / "images"
        self.images_dir.mkdir()
        (self.images_dir / "217270P2.jpg").write_bytes(b"\xff\xd8fake")

        self.yaml_path = Path(self.tmp) / "sites.yaml"
        write_minimal_sites_yaml(self.yaml_path, {
            "porto-sommelier": {
                "path": str(self.site_dir),
                "domain": "porto-wine-tours.com",
                "fleet": "saraswati",
            },
        })
        self.sites = aal.load_site_registry(self.yaml_path)
        self.queue_dir = Path(self.tmp) / "queues"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_inject_when_local_image_exists(self):
        page = self.site_dir / "douro" / "index.html"
        page.parent.mkdir(parents=True)
        page.write_text(
            '<h1>Douro Valley</h1>'
            '<a href="https://www.viator.com/tours/217270P2?pid=1">Book</a>'
        )
        url = "https://porto-wine-tours.com/douro/index.html"
        result = make_audit_result(url, r12=True)
        action, qfiles = aal.process_page(url, result, self.sites, self.queue_dir, dry_run=False)
        self.assertEqual(action.status, "injected")
        self.assertEqual(action.injected_filename, "217270P2.jpg")
        self.assertIn('class="injected-product-image"', page.read_text())

    def test_queue_when_no_local_image(self):
        page = self.site_dir / "lisbon" / "index.html"
        page.parent.mkdir(parents=True)
        page.write_text(
            '<h1>Lisbon</h1>'
            '<a href="https://www.viator.com/tours/NOIMG?pid=1">Book</a>'
        )
        url = "https://porto-wine-tours.com/lisbon/index.html"
        result = make_audit_result(url, r12=True)
        action, qfiles = aal.process_page(url, result, self.sites, self.queue_dir, dry_run=False)
        self.assertEqual(action.status, "queued")
        self.assertEqual(action.queued_codes, ["NOIMG"])
        self.assertEqual(len(qfiles), 1)
        queue_file = qfiles[0]
        self.assertTrue(queue_file.exists())
        self.assertIn("NOIMG", queue_file.read_text())

    def test_manual_curation_when_no_product_codes(self):
        page = self.site_dir / "no-link" / "index.html"
        page.parent.mkdir(parents=True)
        page.write_text("<h1>No link here</h1>")
        url = "https://porto-wine-tours.com/no-link/index.html"
        result = make_audit_result(url, r12=True)
        action, _ = aal.process_page(url, result, self.sites, self.queue_dir, dry_run=False)
        self.assertEqual(action.status, "manual_curation")
        self.assertIn("no product codes", action.reason)

    def test_manual_curation_when_page_file_missing(self):
        url = "https://porto-wine-tours.com/missing/index.html"
        result = make_audit_result(url, r12=True)
        action, _ = aal.process_page(url, result, self.sites, self.queue_dir, dry_run=False)
        self.assertEqual(action.status, "manual_curation")
        self.assertIn("page file not found", action.reason)

    def test_no_action_when_not_r12(self):
        page = self.site_dir / "good" / "index.html"
        page.parent.mkdir(parents=True)
        page.write_text("<h1>Good</h1><img src=\"/images/x.jpg\">")
        url = "https://porto-wine-tours.com/good/index.html"
        result = make_audit_result(url, r12=False, total_images=1)
        action, qfiles = aal.process_page(url, result, self.sites, self.queue_dir, dry_run=False)
        self.assertEqual(action.status, "ok")
        self.assertFalse(action.r12_violation)
        self.assertEqual(qfiles, [])

    def test_audit_error_preserved(self):
        url = "https://porto-wine-tours.com/error/index.html"
        result = make_audit_result(url, r12=False, status="timeout", error="page took too long")
        action, _ = aal.process_page(url, result, self.sites, self.queue_dir, dry_run=False)
        self.assertEqual(action.status, "audit_error")
        self.assertIn("timeout", action.reason)

    def test_dry_run_does_not_write(self):
        page = self.site_dir / "dryrun" / "index.html"
        page.parent.mkdir(parents=True)
        original = '<h1>X</h1><a href="https://www.viator.com/tours/217270P2?pid=1">x</a>'
        page.write_text(original)
        url = "https://porto-wine-tours.com/dryrun/index.html"
        result = make_audit_result(url, r12=True)
        action, qfiles = aal.process_page(url, result, self.sites, self.queue_dir, dry_run=True)
        self.assertEqual(action.status, "injected")
        self.assertEqual(action.injected_filename, "217270P2.jpg")
        # File on disk must NOT be modified
        self.assertEqual(page.read_text(), original)


# ---------------------------------------------------------------------------
# End-to-end with a cached audit (no Playwright)
# ---------------------------------------------------------------------------


class TestEndToEndWithCachedAudit(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.site_dir = Path(self.tmp) / "porto-wine-tours"
        self.site_dir.mkdir(parents=True)
        self.images_dir = self.site_dir / "images"
        self.images_dir.mkdir()
        (self.images_dir / "217270P2.jpg").write_bytes(b"\xff\xd8fake")

        # Two pages: one with local image, one without
        (self.site_dir / "good").mkdir()
        (self.site_dir / "good" / "index.html").write_text(
            '<h1>Good</h1><a href="https://www.viator.com/tours/217270P2?pid=1">x</a>'
        )
        (self.site_dir / "missing").mkdir()
        (self.site_dir / "missing" / "index.html").write_text(
            '<h1>Missing</h1><a href="https://www.viator.com/tours/NOIMG?pid=1">x</a>'
        )
        (self.site_dir / "no-link").mkdir()
        (self.site_dir / "no-link" / "index.html").write_text(
            '<h1>No Link</h1>'
        )

        self.yaml_path = Path(self.tmp) / "sites.yaml"
        write_minimal_sites_yaml(self.yaml_path, {
            "porto-sommelier": {
                "path": str(self.site_dir),
                "domain": "porto-wine-tours.com",
                "fleet": "saraswati",
            },
        })
        self.log_dir = Path(self.tmp) / "logs"
        self.queue_dir = Path(self.tmp) / "queues"

        self.urls = [
            "https://porto-wine-tours.com/good/index.html",
            "https://porto-wine-tours.com/missing/index.html",
            "https://porto-wine-tours.com/no-link/index.html",
        ]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_runs_with_cached_audit(self):
        cached = Path(self.tmp) / "cached-audit.json"
        cached.write_text(json.dumps([
            make_audit_result(self.urls[0], r12=True),
            make_audit_result(self.urls[1], r12=True),
            make_audit_result(self.urls[2], r12=True),
        ]))

        report = aal.run(
            urls=self.urls,
            sites_yaml=self.yaml_path,
            audit_dir=Path(self.tmp) / "audit",  # never opened — we use --no-audit
            queue_dir=self.queue_dir,
            log_dir=self.log_dir,
            no_audit=True,
            cached_audit=cached,
            dry_run=False,
        )

        self.assertEqual(report.url_count, 3)
        self.assertEqual(report.pages_with_r12, 3)
        self.assertEqual(report.pages_injected, 1)
        self.assertEqual(report.pages_queued, 1)
        self.assertEqual(len(report.pages_manual_curation), 1)

        # The "good" page must have an injected <img>
        good_html = (self.site_dir / "good" / "index.html").read_text()
        self.assertIn('class="injected-product-image"', good_html)

        # The queue file must contain NOIMG
        qfiles = list(self.queue_dir.glob("*.queue"))
        self.assertEqual(len(qfiles), 1)
        self.assertIn("NOIMG", qfiles[0].read_text())

        # A log file must exist
        logs = list(self.log_dir.glob("audit-action-*.json"))
        self.assertEqual(len(logs), 1)
        log = json.loads(logs[0].read_text())
        self.assertEqual(log["url_count"], 3)
        self.assertEqual(log["pages_injected"], 1)
        self.assertEqual(log["pages_queued"], 1)

    def test_dry_run_pipeline_does_not_write(self):
        cached = Path(self.tmp) / "cached.json"
        cached.write_text(json.dumps([
            make_audit_result(self.urls[0], r12=True),
            make_audit_result(self.urls[1], r12=True),
        ]))
        original_good = (self.site_dir / "good" / "index.html").read_text()

        report = aal.run(
            urls=self.urls[:2],
            sites_yaml=self.yaml_path,
            audit_dir=Path(self.tmp) / "audit",
            queue_dir=self.queue_dir,
            log_dir=self.log_dir,
            no_audit=True,
            cached_audit=cached,
            dry_run=True,
        )
        self.assertEqual(report.pages_injected, 1)
        self.assertEqual(report.pages_queued, 1)
        # File unchanged
        self.assertEqual((self.site_dir / "good" / "index.html").read_text(), original_good)
        # No queue file
        self.assertFalse(list(self.queue_dir.glob("*.queue")))

    def test_fleet_filter_excludes_other_sites(self):
        # Add a second site to the yaml
        write_minimal_sites_yaml(self.yaml_path, {
            "porto-sommelier": {
                "path": str(self.site_dir),
                "domain": "porto-wine-tours.com",
                "fleet": "saraswati",
            },
            "vinesandplates": {
                "path": str(Path(self.tmp) / "vinesandplates"),
                "domain": "www.vinesandplates.com",
                "fleet": "hanumanhermes",
            },
        })
        cached = Path(self.tmp) / "cached.json"
        cached.write_text(json.dumps([
            make_audit_result(self.urls[0], r12=True),
        ]))
        report = aal.run(
            urls=[self.urls[0]],
            sites_yaml=self.yaml_path,
            audit_dir=Path(self.tmp) / "audit",
            queue_dir=self.queue_dir,
            log_dir=self.log_dir,
            no_audit=True,
            cached_audit=cached,
            dry_run=False,
            fleet_filter="hanumanhermes",  # filter excludes porto
        )
        # Even though R12 is flagged, no Porto site is in scope →
        # URL can't map to a site → manual curation
        self.assertEqual(report.pages_injected, 0)
        self.assertEqual(report.pages_queued, 0)
        self.assertEqual(len(report.pages_manual_curation), 1)


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------


class TestCLI(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.site_dir = Path(self.tmp) / "porto-wine-tours"
        self.site_dir.mkdir(parents=True)
        self.images_dir = self.site_dir / "images"
        self.images_dir.mkdir()
        (self.images_dir / "217270P2.jpg").write_bytes(b"\xff\xd8fake")
        (self.site_dir / "good").mkdir()
        (self.site_dir / "good" / "index.html").write_text(
            '<h1>Good</h1><a href="https://www.viator.com/tours/217270P2?pid=1">x</a>'
        )

        self.yaml_path = Path(self.tmp) / "sites.yaml"
        write_minimal_sites_yaml(self.yaml_path, {
            "porto-sommelier": {
                "path": str(self.site_dir),
                "domain": "porto-wine-tours.com",
                "fleet": "saraswati",
            },
        })
        self.urls_file = Path(self.tmp) / "urls.txt"
        self.urls_file.write_text(
            "# comment line\n"
            "https://porto-wine-tours.com/good/index.html\n"
            "\n"
        )
        self.log_dir = Path(self.tmp) / "logs"
        self.queue_dir = Path(self.tmp) / "queues"
        self.cached_audit = Path(self.tmp) / "audit.json"
        self.cached_audit.write_text(json.dumps([
            make_audit_result("https://porto-wine-tours.com/good/index.html", r12=True),
        ]))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cli_no_audit_runs_end_to_end(self):
        report_path = Path(self.tmp) / "report.json"
        rc = aal.main([
            "--urls", str(self.urls_file),
            "--sites-yaml", str(self.yaml_path),
            "--queue-dir", str(self.queue_dir),
            "--log-dir", str(self.log_dir),
            "--no-audit",
            "--cached-audit", str(self.cached_audit),
            "--quiet",
            "--report", str(report_path),
        ])
        self.assertEqual(rc, 0)
        self.assertTrue(report_path.exists())
        rep = json.loads(report_path.read_text())
        self.assertEqual(rep["url_count"], 1)
        self.assertEqual(rep["pages_injected"], 1)
        # File on disk updated
        self.assertIn('class="injected-product-image"',
                      (self.site_dir / "good" / "index.html").read_text())

    def test_cli_rejects_no_audit_without_cached_audit(self):
        rc = aal.main([
            "--urls", str(self.urls_file),
            "--sites-yaml", str(self.yaml_path),
            "--queue-dir", str(self.queue_dir),
            "--log-dir", str(self.log_dir),
            "--no-audit",
        ])
        self.assertEqual(rc, 2)

    def test_cli_rejects_missing_urls_file(self):
        rc = aal.main([
            "--urls", str(Path(self.tmp) / "no-such-file.txt"),
            "--sites-yaml", str(self.yaml_path),
            "--queue-dir", str(self.queue_dir),
            "--log-dir", str(self.log_dir),
        ])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()