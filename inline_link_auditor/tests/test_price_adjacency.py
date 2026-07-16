"""Tests for the price-adjacency detector (detector 6).

Covers:
  * Currency symbol + digits adjacent to anchor → violation
  * Digits + ISO code adjacent → violation
  * Rp + digits / digits + Rp → violation
  * Price within 5 chars (boundary)
  * Price at 6+ chars → no violation
  * No Viator links → no violations
  * Anchor text contains price → violation
  * Pre-computed ``links`` argument is honoured
  * Non-Viator links are NOT scanned
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inline_link_auditor.detectors import price_adjacency as pa  # noqa: E402
from inline_link_auditor.detectors.price_adjacency import detect  # noqa: E402
from inline_link_auditor.parser import extract_viator_links  # noqa: E402


def _count(html: str) -> int:
    return len(detect(html=html, filepath="", page_url=""))


def test_dollar_symbol_before_anchor_flags_violation():
    html = '<p>Book for $99 today: <a href="https://viator.com/d1234-31913P1">Marina Bay Tour</a></p>'
    violations = detect(html=html)
    assert len(violations) == 1
    assert violations[0].detector == "price_adjacency"
    assert violations[0].rule == "price-nearby"
    assert violations[0].severity == "minor"
    assert "$99" in violations[0].extra["adjacent_price"]


def test_dollar_symbol_after_anchor_flags_violation():
    html = '<p>Tour: <a href="https://viator.com/d1234-31913P1">Marina Bay Tour</a> from $99</p>'
    violations = detect(html=html)
    assert len(violations) == 1


def test_euro_symbol_flags_violation():
    html = '<p>From €120: <a href="https://viator.com/x">A</a></p>'
    violations = detect(html=html)
    assert len(violations) == 1
    assert "€120" in violations[0].extra["adjacent_price"]


def test_pound_symbol_flags_violation():
    html = '<p>£75 <a href="https://viator.com/x">A</a></p>'
    violations = detect(html=html)
    assert len(violations) == 1
    assert "£75" in violations[0].extra["adjacent_price"]


def test_yen_symbol_flags_violation():
    html = '<p>¥1500 <a href="https://viator.com/x">A</a></p>'
    violations = detect(html=html)
    assert len(violations) == 1
    assert "¥1500" in violations[0].extra["adjacent_price"]


def test_rupee_symbol_flags_violation():
    html = '<p>₹500 <a href="https://viator.com/x">A</a></p>'
    violations = detect(html=html)
    assert len(violations) == 1
    assert "₹500" in violations[0].extra["adjacent_price"]


def test_digit_iso_code_after_flags_violation():
    html = '<p>Tour: <a href="https://viator.com/x">A</a> for 99 USD</p>'
    violations = detect(html=html)
    assert len(violations) == 1
    assert "99 USD" in violations[0].extra["adjacent_price"]


def test_digit_iso_code_before_flags_violation():
    html = '<p>From 99 EUR: <a href="https://viator.com/x">A</a></p>'
    violations = detect(html=html)
    assert len(violations) == 1
    assert "99 EUR" in violations[0].extra["adjacent_price"]


def test_iso_code_lowercase_is_not_detected():
    # We deliberately only match uppercase ISO codes — lowercase is too noisy
    # (could be a noun like "us daily").
    html = '<p><a href="https://viator.com/x">A</a> 99 usd</p>'
    # 6 chars between "A" anchor text and "99" → outside window
    # Even if within window, lowercase "usd" should NOT match.
    violations = detect(html=html)
    assert all("usd" not in v.extra["adjacent_price"].lower() for v in violations)


def test_rupiah_with_digits_flags_violation():
    html = '<p><a href="https://viator.com/x">A</a> Rp 250000</p>'
    violations = detect(html=html)
    assert len(violations) == 1
    assert "Rp" in violations[0].extra["adjacent_price"]


def test_digit_then_rupiah_flags_violation():
    html = '<p>From 250000 Rp: <a href="https://viator.com/x">A</a></p>'
    violations = detect(html=html)
    assert len(violations) == 1


def test_price_at_five_char_boundary_flags():
    # "USD99 " → 4 chars between anchor and "USD" — must flag.
    html = '<p>X <a href="https://viator.com/x">A</a> USD99 tour</p>'
    violations = detect(html=html)
    assert len(violations) == 1


def test_price_outside_window_does_not_flag():
    # 10 chars between anchor and the price → outside the 5-char window.
    html = '<p>X <a href="https://viator.com/x">A</a> 0123456789 $99</p>'
    assert _count(html) == 0


def test_no_viator_links_no_violations():
    html = '<p>$99 <a href="https://example.com/x">A</a></p>'
    # The detector only scans Viator links.
    assert _count(html) == 0


def test_non_viator_link_ignored_even_with_price():
    html = '<p>$99 <a href="https://example.com/article">A</a></p>'
    assert _count(html) == 0


def test_price_in_anchor_text_flags_violation():
    html = '<p>See <a href="https://viator.com/x">Tour from $99</a> today</p>'
    violations = detect(html=html)
    assert len(violations) == 1


def test_uses_precomputed_links_when_passed():
    html = '<p>$99 <a href="https://viator.com/x">Tour</a></p>'
    links = extract_viator_links(html)
    violations = detect(html=html, links=links)
    assert len(violations) == 1


def test_multiple_viator_links_each_scanned_independently():
    html = (
        '<p>Book for $50: <a href="https://viator.com/a">Tour A</a></p>'
        '<p>Also <a href="https://viator.com/b">Tour B</a> from €75</p>'
    )
    violations = detect(html=html)
    assert len(violations) == 2
    prices = [v.extra["adjacent_price"] for v in violations]
    assert any("$50" in p for p in prices)
    assert any("€75" in p for p in prices)


def test_to_dict_contains_required_fields():
    html = '<p>$99 <a href="https://viator.com/x">A</a></p>'
    v = detect(html=html)[0]
    d = v.to_dict()
    assert d["detector"] == "price_adjacency"
    assert d["rule"] == "price-nearby"
    assert d["severity"] == "minor"
    assert "adjacent_price" in d["extra"]
    assert "window" in d["extra"]


def test_currency_symbol_without_digits_does_not_flag():
    # "$" alone without digits → not a price.
    html = '<p>$ <a href="https://viator.com/x">A</a></p>'
    assert _count(html) == 0


def test_filepath_and_url_propagate():
    html = '<p>$99 <a href="https://viator.com/x">A</a></p>'
    v = detect(html=html, filepath="/tmp/p.html", page_url="https://x.test/p")[0]
    assert v.file == "/tmp/p.html"
    assert v.url == "https://x.test/p"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))