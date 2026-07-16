"""Tests for detectors.trust_gate — safety-sensitive context detection."""

from __future__ import annotations

from inline_link_auditor.detectors.trust_gate import detect


VIATOR = "https://www.viator.com/tours/Porto/d26879-31913P1"


def _violations(html: str):
    return detect(html, filepath="/tmp/fake.html", url="https://example.com/post")


def test_safety_keyword_in_surrounding_context_is_flagged():
    html = (
        "<p>Read this safety warning before booking the "
        f'<a href="{VIATOR}">Porto canyoning tour</a>.</p>'
    )

    violations = _violations(html)

    assert len(violations) == 1
    violation = violations[0]
    assert violation.detector == "trust_gate"
    assert violation.rule == "trust-keyword"
    assert violation.matched_keyword == "safety"
    assert "safety" in violation.context_snippet.lower()
    assert "Porto canyoning tour" in violation.context_snippet


def test_keywords_are_case_insensitive():
    html = f'<p>For an EMERGENCY, use <a href="{VIATOR}">this tour</a>.</p>'

    violations = _violations(html)

    assert len(violations) == 1
    assert violations[0].matched_keyword == "emergency"


def test_multi_word_keyword_is_reported():
    html = f'<p>Review the <a href="{VIATOR}">refund policy</a> first.</p>'

    violations = _violations(html)

    assert len(violations) == 1
    assert violations[0].matched_keyword == "refund policy"


def test_each_viator_link_is_checked_independently():
    second = "https://www.viator.com/tours/Porto/d26879-3914P2"
    html = (
        f'<a href="{VIATOR}">Porto wine tour</a> is safe. '
        + ("x" * 60)
        + f' <a href="{second}">Porto hiking tour</a> requires insurance.'
    )

    violations = _violations(html)

    assert len(violations) == 1
    assert violations[0].anchor_text == "Porto hiking tour"
    assert violations[0].matched_keyword == "insurance"


def test_non_viator_links_are_ignored():
    html = '<a href="https://example.com/safety">safety information</a>'

    assert _violations(html) == []


def test_clean_context_is_not_flagged():
    html = f'<p>Explore <a href="{VIATOR}">Porto wine tour</a> today.</p>'

    assert _violations(html) == []


def test_violation_fields_are_populated():
    html = f'<p>Check your visa before using <a href="{VIATOR}">this tour</a>.</p>'

    violation = _violations(html)[0]

    assert violation.url == "https://example.com/post"
    assert violation.file == "/tmp/fake.html"
    assert violation.severity == "critical"
    assert violation.line >= 1
    assert violation.anchor_text == "this tour"
    assert violation.context_snippet
