"""Tests for detectors.disclosure — disclosure-before-link detection."""

from __future__ import annotations

from inline_link_auditor.detectors.disclosure import detect


VIATOR = "https://www.viator.com/tours/Porto/d26879-31913P1"


def _violations(html: str):
    return detect(html, filepath="/tmp/fake.html", url="https://example.com/post")


def test_visible_disclosure_before_first_viator_link_is_clean():
    html = (
        "<p>This post contains affiliate links and we may earn a commission.</p>"
        f'<p>Book the <a href="{VIATOR}">Porto wine tour</a>.</p>'
    )

    assert _violations(html) == []


def test_missing_disclosure_is_flagged():
    html = f'<p>Book the <a href="{VIATOR}">Porto wine tour</a>.</p>'

    violations = _violations(html)

    assert len(violations) == 1
    violation = violations[0]
    assert violation.detector == "disclosure"
    assert violation.rule == "missing-disclosure"
    assert violation.severity == "critical"
    assert violation.line == 1
    assert violation.extra == {
        "first_viator_line": 1,
        "disclosure_found": False,
        "disclosure_is_hyperlink": False,
    }


def test_disclosure_only_in_hyperlink_is_flagged():
    html = (
        f'<p><a href="/disclosure">Affiliate disclosure</a></p>\n'
        f'<p>Book the <a href="{VIATOR}">Porto wine tour</a>.</p>'
    )

    violations = _violations(html)

    assert len(violations) == 1
    violation = violations[0]
    assert violation.rule == "hyperlink-disclosure"
    assert violation.line == 2
    assert violation.extra["disclosure_found"] is True
    assert violation.extra["disclosure_is_hyperlink"] is True


def test_non_hyperlink_disclosure_makes_hyperlink_disclosure_sufficient():
    html = (
        '<p>Affiliate disclosure: we may earn a commission from bookings.</p>\n'
        f'<p><a href="{VIATOR}">Porto wine tour</a>.</p>'
    )

    assert _violations(html) == []


def test_disclosure_after_first_viator_link_does_not_count():
    html = (
        f'<p><a href="{VIATOR}">Porto wine tour</a>.</p>\n'
        '<p>Affiliate disclosure: we may earn a commission.</p>'
    )

    violations = _violations(html)

    assert len(violations) == 1
    assert violations[0].rule == "missing-disclosure"
    assert violations[0].line == 1


def test_only_non_viator_links_do_not_require_disclosure():
    html = '<p><a href="https://example.com/tour">Porto wine tour</a>.</p>'

    assert _violations(html) == []


def test_disclosure_matching_is_case_insensitive():
    html = (
        "<p>SPONSORED content may appear here.</p>\n"
        f'<p><a href="{VIATOR}">Porto wine tour</a>.</p>'
    )

    assert _violations(html) == []


def test_disclosure_keywords_in_scripts_do_not_count_as_visible_disclosure():
    html = (
        '<script>const label = "affiliate disclosure";</script>\n'
        f'<p><a href="{VIATOR}">Porto wine tour</a>.</p>'
    )

    violations = _violations(html)

    assert len(violations) == 1
    assert violations[0].rule == "missing-disclosure"
