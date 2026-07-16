"""Tests for detectors.specificity — vague-anchor detection (Detector 1)."""

from __future__ import annotations

from inline_link_auditor.detectors.specificity import detect


VIATOR = "https://www.viator.com/tours/Porto/d26879-31913P1"


def _violations(html: str) -> list:
    return detect(html, filepath="/tmp/fake.html", url="https://example.com/post")


# --- clean anchors (no violations) ----------------------------------------

def test_named_entity_anchor_is_clean():
    html = f'<p>Book the <a href="{VIATOR}">Porto Wine Tasting Tour</a> today.</p>'
    assert _violations(html) == []


def test_long_proper_noun_anchor_is_clean():
    html = f'<a href="{VIATOR}">Douro Valley Day Trip from Porto</a>'
    assert _violations(html) == []


# --- vague phrases (rule violations) -------------------------------------

def test_this_tour_is_flagged():
    html = f'<a href="{VIATOR}">this tour</a>'
    v = _violations(html)
    assert len(v) == 1
    assert v[0].rule == "vague-anchor"
    assert v[0].detector == "specificity"
    assert v[0].anchor_text == "this tour"
    assert v[0].extra["reason"] == "vague-phrase"


def test_click_here_is_flagged():
    html = f'<a href="{VIATOR}">Click Here</a>'
    v = _violations(html)
    assert len(v) == 1
    assert v[0].extra["reason"] == "vague-phrase"


def test_book_now_is_flagged():
    html = f'<a href="{VIATOR}">Book Now</a>'
    v = _violations(html)
    assert len(v) == 1
    assert v[0].extra["reason"] == "vague-phrase"


def test_learn_more_is_flagged():
    html = f'<a href="{VIATOR}">learn more</a>'
    v = _violations(html)
    assert len(v) == 1


def test_read_more_is_flagged():
    html = f'<a href="{VIATOR}">read more</a>'
    v = _violations(html)
    assert len(v) == 1


def test_find_out_is_flagged():
    html = f'<a href="{VIATOR}">find out</a>'
    v = _violations(html)
    assert len(v) == 1


def test_check_it_is_flagged():
    html = f'<a href="{VIATOR}">check it</a>'
    v = _violations(html)
    assert len(v) == 1


# --- too-short anchors ----------------------------------------------------

def test_short_anchor_flagged_even_without_vague_phrase():
    # "Tours" is 5 chars (< 8) — must be flagged even though no vague phrase matches.
    html = f'<a href="{VIATOR}">Tours</a>'
    v = _violations(html)
    assert len(v) == 1
    assert v[0].extra["reason"].startswith("anchor-too-short")


# --- no-proper-noun anchors -----------------------------------------------

def test_long_anchor_without_capitalized_word_is_flagged():
    # 9 chars, no vague phrase, but no proper noun either (no capital).
    html = f'<a href="{VIATOR}">delicious wine</a>'
    v = _violations(html)
    assert len(v) == 1
    assert v[0].extra["reason"] == "no-proper-noun"


def test_acronym_2_chars_does_not_count_as_proper_noun():
    # Proper noun requires > 2 chars; "NY" is only 2 chars and would fail.
    # But "Porto" has 5 chars and is a proper noun → should be clean.
    html = f'<a href="{VIATOR}">NY</a>'
    v = _violations(html)
    # Short (< 8 chars) AND no proper noun > 2 chars → flagged.
    assert len(v) == 1


def test_word_starting_with_capital_mid_sentence_still_counts():
    # "Porto" is a proper noun (starts capitalized, > 2 chars).
    html = f'<a href="{VIATOR}">Porto tours</a>'
    v = _violations(html)
    assert v == []


# --- non-Viator links must be ignored ------------------------------------

def test_non_viator_vague_anchor_is_ignored():
    html = '<a href="https://example.com/x">this tour</a>'
    assert _violations(html) == []


# --- multiple violations on one page --------------------------------------

def test_multiple_violations_in_one_page():
    html = (
        f'<p>See <a href="{VIATOR}">this tour</a> and also '
        f'<a href="{VIATOR}">Click Here</a> for details.</p>'
    )
    v = _violations(html)
    assert len(v) == 2
    assert {x.anchor_text for x in v} == {"this tour", "Click Here"}


# --- field-shape sanity ---------------------------------------------------

def test_violation_fields_are_populated():
    html = f'<a href="{VIATOR}">learn more</a>'
    v = _violations(html)[0]
    assert v.url == "https://example.com/post"
    assert v.file == "/tmp/fake.html"
    assert v.severity == "major"
    assert v.line >= 1
    assert isinstance(v.extra, dict)