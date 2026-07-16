"""Tests for the link-chain detector (detector 5).

Covers:
  * Empty separator between two <a> tags → 1 violation
  * Whitespace separator → 1 violation
  * Punctuation-only separator → 1 violation
  * Real prose separator → 0 violations
  * Three-link chain → 1 violation listing all three anchors
  * Chain split by prose → 2 violations (one per sub-chain)
  * Non-Viator chains are still flagged (rule applies to any <a>)
  * line number is captured when available
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the package importable when running pytest from the repo root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inline_link_auditor.detectors.link_chain import detect  # noqa: E402


def _violation_count(html: str) -> int:
    return len(detect(html=html, filepath="", url=""))


def test_empty_separator_flags_chain():
    html = '<p>See <a href="https://viator.com/tour-a">Tour A</a><a href="https://viator.com/tour-b">Tour B</a> for details.</p>'
    violations = detect(html=html)
    assert len(violations) == 1
    assert violations[0].detector == "link_chain"
    assert violations[0].rule == "link-chain"
    assert violations[0].extra["chain_length"] == 2
    assert violations[0].extra["adjacent_links"] == ["Tour A", "Tour B"]


def test_whitespace_separator_flags_chain():
    html = '<p>Links: <a href="https://example.com/a">A</a>   <a href="https://example.com/b">B</a></p>'
    violations = detect(html=html)
    assert len(violations) == 1
    assert violations[0].extra["adjacent_links"] == ["A", "B"]


def test_punctuation_only_separator_flags_chain():
    html = '<p><a href="https://example.com/a">A</a>|<a href="https://example.com/b">B</a></p>'
    violations = detect(html=html)
    assert len(violations) == 1
    assert violations[0].extra["chain_length"] == 2


def test_punctuation_with_dash_separator_flags_chain():
    html = '<p><a href="https://example.com/a">A</a> - <a href="https://example.com/b">B</a></p>'
    # dash + spaces is still whitespace/punctuation only → flag
    violations = detect(html=html)
    assert len(violations) == 1


def test_prose_separator_no_violation():
    html = '<p>Read <a href="https://example.com/a">tour A</a> then compare it with <a href="https://example.com/b">tour B</a> afterwards.</p>'
    assert _violation_count(html) == 0


def test_three_link_chain_collapses_to_single_violation():
    html = '<p><a href="https://viator.com/x1">Alpha</a><a href="https://viator.com/x2">Beta</a><a href="https://viator.com/x3">Gamma</a></p>'
    violations = detect(html=html)
    assert len(violations) == 1
    assert violations[0].extra["chain_length"] == 3
    assert violations[0].extra["adjacent_links"] == ["Alpha", "Beta", "Gamma"]


def test_chain_split_by_prose_yields_two_violations():
    html = (
        '<p>'
        '<a href="https://viator.com/x1">A</a><a href="https://viator.com/x2">B</a>'
        ' middle prose '
        '<a href="https://viator.com/x3">C</a><a href="https://viator.com/x4">D</a>'
        '</p>'
    )
    violations = detect(html=html)
    assert len(violations) == 2
    chains = [v.extra["adjacent_links"] for v in violations]
    assert ["A", "B"] in chains
    assert ["C", "D"] in chains


def test_non_viator_chains_are_still_flagged():
    html = '<p>See <a href="https://example.com/a">A</a><a href="https://example.com/b">B</a></p>'
    # The rule applies to any <a>, not just Viator — but for non-Viator
    # links, the detector still returns Violations (severity: major).
    violations = detect(html=html)
    assert len(violations) == 1
    assert violations[0].severity == "major"


def test_no_links_no_violations():
    html = '<p>No links here at all.</p>'
    assert _violation_count(html) == 0


def test_single_link_no_violation():
    html = '<p>Only <a href="https://example.com/a">A</a> present.</p>'
    assert _violation_count(html) == 0


def test_chains_in_separate_parents_are_independent():
    # Two paragraphs each with their own chain → 2 violations, not 1.
    html = (
        '<div>'
        '<p><a href="https://example.com/a">A</a><a href="https://example.com/b">B</a></p>'
        '<p><a href="https://example.com/c">C</a><a href="https://example.com/d">D</a></p>'
        '</div>'
    )
    violations = detect(html=html)
    assert len(violations) == 2


def test_violation_filepath_and_url_propagate():
    html = '<p><a href="https://example.com/a">A</a><a href="https://example.com/b">B</a></p>'
    violations = detect(html=html, filepath="/tmp/page.html", url="https://example.com/page")
    assert violations[0].file == "/tmp/page.html"
    assert violations[0].url == "https://example.com/page"


def test_line_number_present_for_multiline_html():
    html = """<html>
<body>
<p>
<a href="https://example.com/a">A</a><a href="https://example.com/b">B</a>
</p>
</body>
</html>"""
    violations = detect(html=html)
    assert len(violations) == 1
    # BeautifulSoup may or may not populate sourcepos with html.parser;
    # we accept any non-zero or zero value but require a valid int.
    assert isinstance(violations[0].line, int)


def test_violation_to_dict_contains_adjacent_links():
    html = '<p><a href="https://example.com/a">A</a><a href="https://example.com/b">B</a></p>'
    v = detect(html=html)[0]
    d = v.to_dict()
    assert d["detector"] == "link_chain"
    assert d["rule"] == "link-chain"
    assert d["extra"]["adjacent_links"] == ["A", "B"]
    assert d["extra"]["chain_length"] == 2


def test_pure_whitespace_chain_inside_nested_span():
    # Nested inline tags between anchors should not break the chain if
    # their combined text is empty/whitespace.
    html = '<p><a href="https://example.com/a">A</a> <span> </span> <a href="https://example.com/b">B</a></p>'
    violations = detect(html=html)
    assert len(violations) == 1
    assert violations[0].extra["adjacent_links"] == ["A", "B"]


def test_word_in_separator_breaks_chain():
    html = '<p><a href="https://example.com/a">A</a> plus <a href="https://example.com/b">B</a></p>'
    assert _violation_count(html) == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))