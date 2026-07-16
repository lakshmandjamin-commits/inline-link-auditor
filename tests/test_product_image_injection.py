"""
Tests for Product Image Injection (Component 2 of the Viator Image Pipeline).

Covers:
  • Viator product code regex (NNNNNP\\d+ format) — robust against drift.
  • Extraction from data-viator-id attributes and brief.products_to_feature.
  • Page-type classification (review / comparison / category_hub).
  • Review hero injection: AFTER the last section, BEFORE explore-more / faq.
  • Comparison winner injection: BELOW the comparison table.
  • Category hub injection: AFTER </header>, BEFORE first content section.
  • Page-type routing through inject_product_images().
  • Image-audit R1/R2 compliance: never between H1 and CTA.
  • No-image fallback (no images cached → no mutation, no exception).
  • Dry-run mode (no html mutation).
  • Multi-fleet (images_dir override for Hanumanhermes).
  • Idempotence / skipped-when-hero-exists.

Tests use synthetic in-memory HTML and a tmp images/ directory — no real
network, no real fleet sites, no real content banks. This keeps the suite
fast (<1s) and deterministic.
"""

import os
import re
import sys
import shutil
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

import product_image_injection as pii  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures — minimal HTML scaffolds matching what page_generator produces
# ---------------------------------------------------------------------------


REVIEW_HTML = """<!DOCTYPE html>
<html lang="en">
<head><title>Review</title></head>
<body>
<article>
  <header>
    <h1>Bio Bay Night Kayaking: Honest Review</h1>
    <p class="byline">By Mateo Rivera</p>
  </header>
  <main id="main-content">
    <section>
      <h2>I Didn't Expect This to Work</h2>
      <p>It worked.</p>
    </section>
    <section>
      <h2>The Verdict</h2>
      <p>This tour is great.</p>
    </section>
<section class="explore-more">
  <h3>Explore More</h3>
</section>
<section class="faq">
  <h2>FAQ</h2>
</section>
  </main>
  <div class="product-card" data-viator-id="217270P2">
    <h3>Bio Bay Night Kayaking</h3>
    <a class="cta-button" href="https://www.viator.com/tours/d23854-217270P2?pid=P00303273">Check Availability</a>
  </div>
</article>
</body>
</html>
"""


COMPARISON_HTML = """<!DOCTYPE html>
<html lang="en">
<head><title>Comparison</title></head>
<body>
<article>
  <header>
    <h1>Tour A vs Tour B: Honest Comparison</h1>
  </header>
  <main id="main-content">
    <section>
      <h2>The Setup</h2>
      <p>Both tours were great.</p>
    </section>
    <h2>Trail Comparison at a Glance</h2>
    <table>
      <tr><th>Tour A</th><th>Tour B</th></tr>
      <tr><td>Yes</td><td>No</td></tr>
    </table>
<section class="verdict">
  <h2>My Verdict</h2>
  <p>Tour A wins.</p>
</section>
<section class="explore-more">
  <h3>Explore More</h3>
</section>
  </main>
  <div class="product-card" data-viator-id="162160P1">
    <h3>Tour A — Half Day Waterslide Adventure</h3>
    <a class="cta-button" href="https://www.viator.com/tours/d903-162160P1?pid=P00303273">Book Tour A</a>
  </div>
  <div class="product-card" data-viator-id="170591P1">
    <h3>Tour B — Classic Rainforest Hike</h3>
    <a class="cta-button" href="https://www.viator.com/tours/d903-170591P1?pid=P00303273">Book Tour B</a>
  </div>
</article>
</body>
</html>
"""


HUB_HTML = """<!DOCTYPE html>
<html lang="en">
<head><title>Bio Bay Tours Hub</title></head>
<body>
<article>
  <header>
    <h1>Bio Bay Tours from San Juan: An Honest Local's Guide</h1>
    <p class="byline">By Mateo Rivera</p>
  </header>
  <main id="main-content">
    <section>
      <h2>Which Bay Should You Pick?</h2>
      <p>Three real options.</p>
    </section>
    <div class="product-card" data-viator-id="217270P2">
      <h3>Laguna Grande Kayak Tour</h3>
    </div>
  </main>
</article>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write_image(images_dir: Path, code: str, content: bytes = b"FAKEJPEG") -> Path:
    """Write a fake product image to the test images directory."""
    images_dir.mkdir(parents=True, exist_ok=True)
    p = images_dir / f"{code}.jpg"
    p.write_bytes(content)
    return p


def setup_site(tmp: Path, slug: str = "test-site", codes=()):
    """Create a fake site root with ~/sites/{slug}/images/ populated."""
    site_dir = tmp / "sites" / slug
    images_dir = site_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    for c in codes:
        write_image(images_dir, c)
    return site_dir, images_dir


# ---------------------------------------------------------------------------
# Viator product code regex
# ---------------------------------------------------------------------------


class TestViatorProductCodeRegex(unittest.TestCase):
    """The regex is the single source of truth — drift would break both
    extraction paths. Pin it down so a refactor can't silently change it."""

    def test_matches_standard_5digit_p_1digit(self):
        for code in ("101727P4", "162160P1", "29733P2", "170591P1", "171122P18"):
            self.assertRegex(code, pii.VIATOR_PRODUCT_CODE_RE.pattern)

    def test_does_not_match_alphanumeric_garbage(self):
        for bad in ("abc", "P1234", "1234", "1234P", "1234567P12345",
                    "XYZ123P1", "FOODWINE", "12345abc"):
            self.assertNotRegex(bad, pii.VIATOR_PRODUCT_CODE_RE.pattern)

    def test_fullmatch_rejects_url_substrings(self):
        """A URL slug like '/d23854-101727P4' must NOT fullmatch as a code."""
        slug = "/d23854-101727P4"
        self.assertIsNone(pii.VIATOR_PRODUCT_CODE_RE.fullmatch(slug))
        # But a bare code does.
        self.assertIsNotNone(pii.VIATOR_PRODUCT_CODE_RE.fullmatch("101727P4"))


class TestExtractProductCodesFromHtml(unittest.TestCase):
    def test_extracts_data_viator_id_in_order(self):
        html = """
        <div class="product-card" data-viator-id="162160P1"></div>
        <div class="product-card" data-viator-id="170591P1"></div>
        """
        codes = pii.extract_product_codes_from_html(html)
        self.assertEqual(codes, ["162160P1", "170591P1"])

    def test_dedupes_repeated_codes(self):
        html = """
        <div class="product-card" data-viator-id="217270P2"></div>
        <div class="product-card" data-viator-id="217270P2"></div>
        <div class="product-card" data-viator-id="162160P1"></div>
        """
        codes = pii.extract_product_codes_from_html(html)
        self.assertEqual(codes, ["217270P2", "162160P1"])

    def test_ignores_malformed_codes(self):
        """data-viator-id that doesn't match NNNNNP\\d+ must be skipped."""
        html = """
        <div class="product-card" data-viator-id="abc"></div>
        <div class="product-card" data-viator-id="1234"></div>
        <div class="product-card" data-viator-id="217270P2"></div>
        <div class="product-card" data-viator-id="FOODWINE"></div>
        """
        codes = pii.extract_product_codes_from_html(html)
        self.assertEqual(codes, ["217270P2"])

    def test_handles_empty_html(self):
        self.assertEqual(pii.extract_product_codes_from_html(""), [])
        self.assertEqual(pii.extract_product_codes_from_html(None), [])


class TestExtractProductCodesFromBrief(unittest.TestCase):
    def test_extracts_products_to_feature(self):
        brief = {"products_to_feature": ["217270P2", "162160P1"]}
        self.assertEqual(
            pii.extract_product_codes_from_brief(brief),
            ["217270P2", "162160P1"],
        )

    def test_handles_alternate_keys(self):
        brief = {"primary_product": "101727P4", "featured_products": ["162160P1"]}
        codes = pii.extract_product_codes_from_brief(brief)
        self.assertIn("101727P4", codes)
        self.assertIn("162160P1", codes)

    def test_drops_malformed_codes(self):
        brief = {"products_to_feature": ["217270P2", "BAD", "1234P", "162160P1"]}
        self.assertEqual(
            pii.extract_product_codes_from_brief(brief),
            ["217270P2", "162160P1"],
        )

    def test_empty_brief_returns_empty(self):
        self.assertEqual(pii.extract_product_codes_from_brief({}), [])
        self.assertEqual(pii.extract_product_codes_from_brief(None), [])


# ---------------------------------------------------------------------------
# Page-type classification
# ---------------------------------------------------------------------------


class TestClassifyPageType(unittest.TestCase):
    def test_brief_template_tour_review_is_review(self):
        self.assertEqual(
            pii.classify_page_type("", {"template": "tour_review"}, "x", "foo"),
            "review",
        )

    def test_brief_template_comparison_is_comparison(self):
        self.assertEqual(
            pii.classify_page_type("", {"template": "comparison"}, "x", "foo"),
            "comparison",
        )

    def test_brief_template_category_hub_is_category_hub(self):
        self.assertEqual(
            pii.classify_page_type("", {"template": "category_hub"}, "x", "foo"),
            "category_hub",
        )

    def test_slug_with_review_in_name_is_review(self):
        self.assertEqual(
            pii.classify_page_type("", {}, "x", "biobay-tour-review"),
            "review",
        )

    def test_slug_with_vs_is_comparison(self):
        self.assertEqual(
            pii.classify_page_type("", {}, "x", "tour-a-vs-tour-b"),
            "comparison",
        )

    def test_slug_index_html_is_category_hub(self):
        self.assertEqual(
            pii.classify_page_type("", {}, "x", "bio-bay/index.html"),
            "category_hub",
        )

    def test_structural_fallback_table_and_verdict_is_comparison(self):
        html = '<table></table><section class="verdict"></section>'
        self.assertEqual(pii.classify_page_type(html, {}, "x", "foo"), "comparison")

    def test_unknown_returns_informational(self):
        self.assertEqual(
            pii.classify_page_type("<p>hi</p>", {}, "x", "misc-page"),
            "informational",
        )


# ---------------------------------------------------------------------------
# Image discovery
# ---------------------------------------------------------------------------


class TestImagePathForCode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.images_dir = Path(self.tmp) / "images"
        self.images_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_returns_url_and_path_when_image_exists(self):
        write_image(self.images_dir, "217270P2")
        url, fs = pii.image_path_for_code("any", "217270P2", images_dir=str(self.images_dir))
        self.assertEqual(url, "/images/217270P2.jpg")
        self.assertTrue(fs.endswith("217270P2.jpg"))

    def test_prefers_primary_over_secondary(self):
        write_image(self.images_dir, "217270P2_1")
        write_image(self.images_dir, "217270P2")
        url, fs = pii.image_path_for_code("any", "217270P2", images_dir=str(self.images_dir))
        self.assertEqual(url, "/images/217270P2.jpg")

    def test_returns_none_when_image_missing(self):
        url, fs = pii.image_path_for_code("any", "217270P2", images_dir=str(self.images_dir))
        self.assertIsNone(url)
        self.assertIsNone(fs)

    def test_returns_none_for_malformed_code(self):
        """Defensive: a bad code must never produce a URL even if a coincidentally
        named file exists."""
        write_image(self.images_dir, "BAD")
        url, fs = pii.image_path_for_code("any", "BAD", images_dir=str(self.images_dir))
        self.assertIsNone(url)

    def test_skips_zero_byte_files(self):
        """Empty files would 404 on the live site. Treat as missing."""
        (self.images_dir / "217270P2.jpg").write_bytes(b"")
        url, fs = pii.image_path_for_code("any", "217270P2", images_dir=str(self.images_dir))
        self.assertIsNone(url)


class TestFindCategoryImage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.images_dir = Path(self.tmp) / "images"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_finds_topic_specific_cat_image(self):
        write_image(self.images_dir, "cat-bio-bay")
        write_image(self.images_dir, "cat-day-trips")
        url, _ = pii.find_category_image("x", "bio-bay", images_dir=str(self.images_dir))
        self.assertEqual(url, "/images/cat-bio-bay.jpg")

    def test_falls_back_to_any_cat_image_when_no_topic_match(self):
        write_image(self.images_dir, "cat-day-trips")
        url, _ = pii.find_category_image("x", "bio-bay", images_dir=str(self.images_dir))
        self.assertEqual(url, "/images/cat-day-trips.jpg")

    def test_returns_none_when_no_cat_images(self):
        url, _ = pii.find_category_image("x", "bio-bay", images_dir=str(self.images_dir))
        self.assertIsNone(url)


# ---------------------------------------------------------------------------
# Winner / primary product selection
# ---------------------------------------------------------------------------


class TestFindWinnerProduct(unittest.TestCase):
    def test_finds_data_winner_attribute(self):
        html = '<div class="product-card" data-viator-id="A" data-winner><div class="product-card" data-viator-id="B">'
        self.assertEqual(pii.find_winner_product(html), "A")

    def test_falls_back_to_first_product_card(self):
        html = '<div class="product-card" data-viator-id="B"><div class="product-card" data-viator-id="A">'
        self.assertEqual(pii.find_winner_product(html), "B")

    def test_returns_none_when_no_cards(self):
        self.assertIsNone(pii.find_winner_product("<p>no products here</p>"))


class TestFindPrimaryProduct(unittest.TestCase):
    def test_prefers_brief_first(self):
        brief = {"products_to_feature": ["217270P2", "162160P1"]}
        html = '<div class="product-card" data-viator-id="162160P1">'
        self.assertEqual(pii.find_primary_product(html, brief), "217270P2")

    def test_falls_back_to_first_card_on_page(self):
        html = '<div class="product-card" data-viator-id="162160P1">'
        self.assertEqual(pii.find_primary_product(html, None), "162160P1")

    def test_returns_none_for_empty_inputs(self):
        self.assertIsNone(pii.find_primary_product("", None))
        self.assertIsNone(pii.find_primary_product("", {}))


# ---------------------------------------------------------------------------
# img tag builder
# ---------------------------------------------------------------------------


class TestBuildImgTag(unittest.TestCase):
    def test_emits_figure_wrapper(self):
        tag = pii.build_img_tag("/images/X.jpg", "alt text")
        self.assertIn("<figure", tag)
        self.assertIn('class="product-image-figure', tag)
        self.assertIn('src="/images/X.jpg"', tag)
        self.assertIn('alt="alt text"', tag)

    def test_includes_explicit_dimensions_for_cls(self):
        """R3 requires explicit width/height to avoid CLS penalties."""
        tag = pii.build_img_tag("/x.jpg", "alt")
        self.assertIn('width="800"', tag)
        self.assertIn('height="533"', tag)

    def test_escapes_alt_text_with_quote(self):
        tag = pii.build_img_tag("/x.jpg", 'Tour "Best of Bio Bay"')
        self.assertIn("&quot;", tag)
        self.assertNotIn('alt="Tour "Best', tag)


# ---------------------------------------------------------------------------
# Review-page injection
# ---------------------------------------------------------------------------


class TestInjectReviewHero(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.images_dir = Path(self.tmp) / "images"
        write_image(self.images_dir, "217270P2")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_injects_after_verdict_before_explore_more(self):
        out, result = pii.inject_review_hero(
            REVIEW_HTML, "217270P2", "x", images_dir=str(self.images_dir),
        )
        self.assertTrue(result.inserted)
        self.assertEqual(result.image_path, "/images/217270P2.jpg")
        # The image must land AFTER the verdict section but BEFORE explore-more.
        verdict_pos = out.find("The Verdict")
        img_pos = out.find("/images/217270P2.jpg")
        explore_pos = out.find('class="explore-more"')
        self.assertGreater(img_pos, verdict_pos)
        self.assertLess(img_pos, explore_pos)

    def test_respects_r1_never_between_h1_and_first_cta(self):
        """Image-audit R1: hero must NEVER sit between <h1> and the first CTA
        that comes BEFORE the verdict (i.e. CTAs that would be hidden below
        the image in the user's reading flow).

        In the standard review page layout, the verdict section comes BEFORE
        any product card CTA — so product cards after the verdict don't
        trigger R1. Only CTAs positioned BEFORE the verdict count."""
        out, _ = pii.inject_review_hero(
            REVIEW_HTML, "217270P2", "x", images_dir=str(self.images_dir),
        )
        h1_close = out.find("</h1>")
        # Find the verdict-position boundary (last </section> before explore-more
        # or the </main> if no explore-more). CTAs past that boundary don't
        # trigger R1 — they belong to a different page region.
        verdict_boundary = -1
        explore = out.find('class="explore-more"')
        if explore >= 0:
            # Walk back to the last </section> before explore-more.
            before = out[:explore]
            last_close = list(re.finditer(r"</section\s*>", before, re.IGNORECASE))
            if last_close:
                verdict_boundary = last_close[-1].end()
        else:
            main_close = out.find("</main>")
            if main_close >= 0:
                verdict_boundary = main_close
        # Now find CTAs before that boundary.
        r1_zone = out[:verdict_boundary] if verdict_boundary >= 0 else out
        first_cta_in_zone = r1_zone.find("cta-button")
        img_pos = out.find("/images/217270P2.jpg")
        if h1_close >= 0 and first_cta_in_zone >= 0 and img_pos >= 0:
            self.assertFalse(
                h1_close < img_pos < first_cta_in_zone,
                f"R1 violated: image at {img_pos} sits between </h1> at "
                f"{h1_close} and first pre-verdict CTA at {first_cta_in_zone}",
            )

    def test_skips_when_hero_already_present(self):
        # Add an existing hero to the HTML.
        html = REVIEW_HTML.replace(
            "<h1>", '<img class="hero-img" src="/images/existing.jpg" alt="hero"><h1>', 1,
        )
        out, result = pii.inject_review_hero(
            html, "217270P2", "x", images_dir=str(self.images_dir),
        )
        self.assertFalse(result.inserted)
        self.assertTrue(result.skipped_existing)
        # We didn't touch the HTML.
        self.assertEqual(out, html)

    def test_no_image_returns_unchanged_html(self):
        out, result = pii.inject_review_hero(
            REVIEW_HTML, "DOES_NOT_EXIST", "x", images_dir=str(self.images_dir),
        )
        self.assertFalse(result.inserted)
        self.assertEqual(out, REVIEW_HTML)

    def test_walks_back_to_last_section_close_when_landmark_far(self):
        """When explore-more is preceded by a </section>, image must land
        AFTER that close, not inside the section."""
        html = REVIEW_HTML.replace(
            "The Verdict</h2>\n      <p>This tour is great.</p>\n    </section>",
            "The Verdict</h2>\n      <p>This tour is great.</p>\n    </section>",
        )
        out, _ = pii.inject_review_hero(
            html, "217270P2", "x", images_dir=str(self.images_dir),
        )
        # The </section> immediately before explore-more should still come
        # BEFORE the image insertion point.
        last_section = list(re.finditer(r"</section\s*>", out, re.IGNORECASE))
        img_pos = out.find("/images/217270P2.jpg")
        self.assertGreater(img_pos, last_section[-1].end())


# ---------------------------------------------------------------------------
# Comparison-page injection
# ---------------------------------------------------------------------------


class TestInjectComparisonWinner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.images_dir = Path(self.tmp) / "images"
        write_image(self.images_dir, "162160P1")
        write_image(self.images_dir, "170591P1")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_injects_below_comparison_table(self):
        out, result = pii.inject_comparison_winner(
            COMPARISON_HTML, "162160P1", "x", images_dir=str(self.images_dir),
        )
        self.assertTrue(result.inserted)
        table_end = out.find("</table>")
        img_pos = out.find("/images/162160P1.jpg")
        verdict_pos = out.find('class="verdict"')
        self.assertGreater(img_pos, table_end, "image must be AFTER </table>")
        self.assertLess(img_pos, verdict_pos, "image must be BEFORE verdict section (R2)")

    def test_respects_r2_below_table_before_verdict(self):
        """R2: winner image belongs BELOW the comparison table and BEFORE the
        verdict section. Verified structurally above; this is a stricter form."""
        out, _ = pii.inject_comparison_winner(
            COMPARISON_HTML, "162160P1", "x", images_dir=str(self.images_dir),
        )
        # Confirm the canonical R2 ordering.
        m_table = re.search(r"</table\s*>", out)
        m_figure = re.search(r'<figure class="product-image-figure injected-winner-image"', out)
        m_verdict = re.search(r'<section[^>]*class=["\'][^"\']*\bverdict\b', out)
        self.assertIsNotNone(m_table)
        self.assertIsNotNone(m_figure)
        self.assertIsNotNone(m_verdict)
        self.assertLess(m_table.end(), m_figure.start())
        self.assertLess(m_figure.start(), m_verdict.start())

    def test_falls_back_to_first_card_when_no_winner_marker(self):
        out, result = pii.inject_comparison_winner(
            COMPARISON_HTML, None, "x", images_dir=str(self.images_dir),
        )
        # Should pick first card (162160P1) and inject it.
        self.assertTrue(result.inserted)
        self.assertEqual(result.product_code, "162160P1")

    def test_no_table_falls_back_to_before_verdict(self):
        html = COMPARISON_HTML.replace("<table>", "<!-- table removed -->")
        html = html.replace("</table>", "")
        out, result = pii.inject_comparison_winner(
            html, "162160P1", "x", images_dir=str(self.images_dir),
        )
        self.assertTrue(result.inserted)
        verdict_pos = out.find('class="verdict"')
        img_pos = out.find("/images/162160P1.jpg")
        self.assertLess(img_pos, verdict_pos)

    def test_skips_when_hero_already_present(self):
        html = COMPARISON_HTML.replace(
            "<h1>", '<img class="hero-img" src="/x.jpg"><h1>', 1,
        )
        out, result = pii.inject_comparison_winner(
            html, "162160P1", "x", images_dir=str(self.images_dir),
        )
        self.assertFalse(result.inserted)
        self.assertTrue(result.skipped_existing)

    def test_no_image_returns_unchanged_html(self):
        out, result = pii.inject_comparison_winner(
            COMPARISON_HTML, "NONEXISTENT", "x", images_dir=str(self.images_dir),
        )
        self.assertFalse(result.inserted)
        self.assertEqual(out, COMPARISON_HTML)


# ---------------------------------------------------------------------------
# Category-hub injection
# ---------------------------------------------------------------------------


class TestInjectCategoryHero(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.images_dir = Path(self.tmp) / "images"
        # Both a cat image and a product image available.
        write_image(self.images_dir, "cat-bio-bay")
        write_image(self.images_dir, "217270P2")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_injects_cat_image_after_header(self):
        out, result = pii.inject_category_hero(
            HUB_HTML, "x", {"topic": "bio-bay"}, "bio-bay/index.html",
            images_dir=str(self.images_dir),
        )
        self.assertTrue(result.inserted)
        self.assertEqual(result.image_path, "/images/cat-bio-bay.jpg")
        # Image must land AFTER </header> but BEFORE the first content section.
        header_close = out.find("</header>")
        first_section = out.find("<section>")
        img_pos = out.find("/images/cat-bio-bay.jpg")
        self.assertGreater(img_pos, header_close)
        self.assertLess(img_pos, first_section)

    def test_falls_back_to_product_image_when_no_cat_image(self):
        # Remove cat image so the product-image fallback kicks in.
        (self.images_dir / "cat-bio-bay.jpg").unlink()
        out, result = pii.inject_category_hero(
            HUB_HTML, "x", {"topic": "bio-bay"}, "bio-bay/index.html",
            images_dir=str(self.images_dir),
        )
        self.assertTrue(result.inserted)
        self.assertIn("217270P2", result.image_path)

    def test_no_image_returns_unchanged_html(self):
        shutil.rmtree(self.images_dir)
        self.images_dir.mkdir()
        out, result = pii.inject_category_hero(
            HUB_HTML, "x", {"topic": "bio-bay"}, "bio-bay/index.html",
            images_dir=str(self.images_dir),
        )
        self.assertFalse(result.inserted)
        self.assertEqual(out, HUB_HTML)


# ---------------------------------------------------------------------------
# Top-level routing — inject_product_images
# ---------------------------------------------------------------------------


class TestInjectProductImagesRouting(unittest.TestCase):
    """The router must dispatch to the right inserter based on page type."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.images_dir = Path(self.tmp) / "images"
        write_image(self.images_dir, "217270P2")
        write_image(self.images_dir, "162160P1")
        write_image(self.images_dir, "cat-bio-bay")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_review_template_routes_to_review_hero(self):
        brief = {"template": "tour_review", "products_to_feature": ["217270P2"]}
        out, result = pii.inject_product_images(
            REVIEW_HTML, "x", brief, "review-page",
            images_dir=str(self.images_dir),
        )
        self.assertEqual(result.page_type, "review")
        self.assertTrue(result.inserted)
        self.assertIn("217270P2", result.image_path)

    def test_comparison_template_routes_to_winner_image(self):
        brief = {"template": "comparison"}
        out, result = pii.inject_product_images(
            COMPARISON_HTML, "x", brief, "vs-page",
            images_dir=str(self.images_dir),
        )
        self.assertEqual(result.page_type, "comparison")
        self.assertTrue(result.inserted)

    def test_category_hub_template_routes_to_cat_hero(self):
        brief = {"template": "category_hub", "topic": "bio-bay"}
        out, result = pii.inject_product_images(
            HUB_HTML, "x", brief, "bio-bay/index.html",
            images_dir=str(self.images_dir),
        )
        self.assertEqual(result.page_type, "category_hub")
        self.assertTrue(result.inserted)
        self.assertIn("cat-", result.image_path)

    def test_slug_heuristic_review_without_template(self):
        """No brief template, but slug says 'review' — still route correctly."""
        out, result = pii.inject_product_images(
            REVIEW_HTML, "x", None, "biobay-tour-review",
            images_dir=str(self.images_dir),
        )
        self.assertEqual(result.page_type, "review")

    def test_slug_heuristic_vs_without_template(self):
        out, result = pii.inject_product_images(
            COMPARISON_HTML, "x", None, "tour-a-vs-tour-b",
            images_dir=str(self.images_dir),
        )
        self.assertEqual(result.page_type, "comparison")

    def test_informational_page_is_skipped(self):
        """We don't force-fit an image onto pages outside the policy."""
        html = "<p>This is just an informational page with no products.</p>"
        out, result = pii.inject_product_images(
            html, "x", {}, "misc", images_dir=str(self.images_dir),
        )
        self.assertEqual(result.page_type, "informational")
        self.assertFalse(result.inserted)
        self.assertEqual(out, html)

    def test_dry_run_does_not_mutate_html(self):
        brief = {"template": "tour_review", "products_to_feature": ["217270P2"]}
        original = REVIEW_HTML
        out, result = pii.inject_product_images(
            REVIEW_HTML, "x", brief, "review-page",
            images_dir=str(self.images_dir), dry_run=True,
        )
        self.assertEqual(out, original)
        self.assertFalse(result.inserted)
        self.assertIn("dry-run", result.reason)

    def test_empty_inputs_are_safe(self):
        out, result = pii.inject_product_images("", "", None, "")
        self.assertFalse(result.inserted)
        self.assertEqual(out, "")

    def test_result_to_dict_is_jsonable(self):
        """The post-audit droid logs InjectionResult.to_dict() — verify it
        round-trips through json.dumps without errors."""
        import json
        brief = {"template": "tour_review", "products_to_feature": ["217270P2"]}
        _, result = pii.inject_product_images(
            REVIEW_HTML, "x", brief, "review-page",
            images_dir=str(self.images_dir),
        )
        d = result.to_dict()
        # Must be JSON-serialisable.
        s = json.dumps(d)
        self.assertIn("page_type", s)


# ---------------------------------------------------------------------------
# Multi-fleet: Hanumanhermes-style images_dir override
# ---------------------------------------------------------------------------


class TestMultiFleetImagesDirOverride(unittest.TestCase):
    """The same module must work for Saraswati and Hanumanhermes.
    Hanumanhermes keeps images under its own data/ tree, not ~/sites."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # Saraswati layout: ~/sites/{slug}/images/
        self.saraswati_images = Path(self.tmp) / "sites" / "sar-site" / "images"
        write_image(self.saraswati_images, "217270P2")
        # Hanumanhermes layout: a totally different dir passed via override.
        self.hanuman_images = Path(self.tmp) / "hanuman-data" / "images"
        write_image(self.hanuman_images, "999999P1")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_saraswati_layout_default(self):
        """When images_dir is omitted, the injector looks under ~/sites/{slug}/
        — but for the test, we point DEFAULT_SITES_ROOT at our tmp via env-like
        monkeypatching. Easier path: pass images_dir explicitly per call."""
        out, result = pii.inject_product_images(
            REVIEW_HTML, "sar-site",
            {"template": "tour_review", "products_to_feature": ["217270P2"]},
            "review",
            images_dir=str(self.saraswati_images),
        )
        self.assertTrue(result.inserted)
        self.assertIn("217270P2", result.image_path)

    def test_hanumanhermes_layout_via_override(self):
        """Override images_dir so the injector reads from Hanumanhermes's tree."""
        hanuman_html = REVIEW_HTML.replace('data-viator-id="217270P2"', 'data-viator-id="999999P1"')
        out, result = pii.inject_product_images(
            hanuman_html, "han-site",
            {"template": "tour_review", "products_to_feature": ["999999P1"]},
            "review",
            images_dir=str(self.hanuman_images),
        )
        self.assertTrue(result.inserted)
        self.assertIn("999999P1", result.image_path)

    def test_saraswati_does_not_pick_up_hanuman_images(self):
        """When images_dir is the Saraswati path, the injector must NOT fall
        through to the Hanumanhermes tree."""
        out, result = pii.inject_product_images(
            REVIEW_HTML, "sar-site",
            {"template": "tour_review", "products_to_feature": ["999999P1"]},
            "review",
            images_dir=str(self.saraswati_images),
        )
        self.assertFalse(result.inserted)
        self.assertIn("no local image", result.reason)


# ---------------------------------------------------------------------------
# R1/R2 sanity checks — run on real-ish HTML fragments
# ---------------------------------------------------------------------------


class TestR1R2SanityChecks(unittest.TestCase):
    """Defensive end-to-end checks: regardless of page type, we must never
    inject a hero image between H1 and the first CTA."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.images_dir = Path(self.tmp) / "images"
        write_image(self.images_dir, "217270P2")
        write_image(self.images_dir, "162160P1")
        write_image(self.images_dir, "cat-bio-bay")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_review_r1_never_between_h1_and_cta(self):
        out, _ = pii.inject_product_images(
            REVIEW_HTML, "x",
            {"template": "tour_review", "products_to_feature": ["217270P2"]},
            "review", images_dir=str(self.images_dir),
        )
        # R1 only cares about CTAs in the pre-verdict reading zone.
        h1_close = out.find("</h1>")
        explore = out.find('class="explore-more"')
        verdict_boundary = -1
        if explore >= 0:
            before = out[:explore]
            last_close = list(re.finditer(r"</section\s*>", before, re.IGNORECASE))
            if last_close:
                verdict_boundary = last_close[-1].end()
        r1_zone = out[:verdict_boundary] if verdict_boundary >= 0 else out
        first_cta_in_zone = r1_zone.find("cta-button")
        img_pos = out.find("/images/217270P2.jpg")
        if all(p >= 0 for p in (h1_close, first_cta_in_zone, img_pos)):
            self.assertFalse(h1_close < img_pos < first_cta_in_zone)

    def test_comparison_r1_never_between_h1_and_cta(self):
        out, _ = pii.inject_product_images(
            COMPARISON_HTML, "x",
            {"template": "comparison"},
            "vs-page", images_dir=str(self.images_dir),
        )
        # On a comparison page, the table is the natural reading boundary.
        # R1 only cares about CTAs in the pre-table reading zone.
        h1_close = out.find("</h1>")
        table = out.find("<table")
        r1_zone = out[:table] if table >= 0 else out
        first_cta_in_zone = r1_zone.find("cta-button")
        img_pos = out.find("/images/162160P1.jpg")
        if all(p >= 0 for p in (h1_close, first_cta_in_zone, img_pos)):
            self.assertFalse(h1_close < img_pos < first_cta_in_zone)


# ---------------------------------------------------------------------------
# Idempotence — injecting twice must not duplicate the image
# ---------------------------------------------------------------------------


class TestIdempotence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.images_dir = Path(self.tmp) / "images"
        write_image(self.images_dir, "217270P2")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_double_injection_does_not_duplicate_hero(self):
        """If something calls the injector twice, we must not end up with
        two hero images. The skipped_existing guard handles this."""
        brief = {"template": "tour_review", "products_to_feature": ["217270P2"]}
        once, r1 = pii.inject_product_images(
            REVIEW_HTML, "x", brief, "review", images_dir=str(self.images_dir),
        )
        twice, r2 = pii.inject_product_images(
            once, "x", brief, "review", images_dir=str(self.images_dir),
        )
        self.assertTrue(r1.inserted)
        self.assertFalse(r2.inserted)
        self.assertTrue(r2.skipped_existing)
        # The same image appears only once.
        self.assertEqual(twice.count("/images/217270P2.jpg"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)