"""Tests for detectors.first_mention — repeated-link detection (Detector 2)."""

from __future__ import annotations

from inline_link_auditor.detectors.first_mention import detect


PORTO_WINE = "https://www.viator.com/tours/Porto/d26879-3913P1"
DOURO_DAY = "https://www.viator.com/tours/Porto/d26879-3914P2"
LISBON_OLD = "https://www.viator.com/tours/Lisbon/d26880-3915P3"


def _violations(html: str):
    return detect(html, filepath="/tmp/fake.html", url="https://example.com/post")


# --- single link per product ----------------------------------------------

def test_single_prose_link_is_clean():
    html = f'<p>See the <a href="{PORTO_WINE}">Porto Wine Tasting Tour</a>.</p>'
    assert _violations(html) == []


# --- repeated prose links ------------------------------------------------

def test_two_prose_links_to_same_product_flags_second():
    html = (
        f'<p>Day one: <a href="{PORTO_WINE}">Porto Wine Tasting Tour</a>.</p>'
        f'<p>Day two: revisit the <a href="{PORTO_WINE}">same tour again</a>.</p>'
    )
    v = _violations(html)
    assert len(v) == 1
    assert v[0].detector == "first_mention"
    assert v[0].rule == "repeated-link"
    assert v[0].product_code == "3913P1"
    assert v[0].extra["occurrence"] == 2


def test_three_prose_links_flag_positions_2_and_3():
    html = (
        f'<a href="{PORTO_WINE}">First mention</a>'
        f'<a href="{PORTO_WINE}">Second mention</a>'
        f'<a href="{PORTO_WINE}">Third mention</a>'
    )
    v = _violations(html)
    assert len(v) == 2
    assert [x.extra["occurrence"] for x in v] == [2, 3]


def test_different_products_do_not_conflict():
    html = (
        f'<a href="{PORTO_WINE}">Porto Wine</a>'
        f'<a href="{DOURO_DAY}">Douro Day Trip</a>'
        f'<a href="{LISBON_OLD}">Lisbon Old Town</a>'
    )
    assert _violations(html) == []


# --- card / button links don't count against prose budget -----------------

def test_card_links_are_not_counted_as_repeats():
    html = f'''
    <p>See the <a href="{PORTO_WINE}">Porto Wine Tasting Tour</a>.</p>
    <div class="product-card">
        <a href="{PORTO_WINE}">Book Porto Wine</a>
    </div>
    <div class="card">
        <a href="{PORTO_WINE}">Reserve Porto Wine</a>
    </div>
    '''
    assert _violations(html) == []


def test_prose_followed_by_card_resets_and_clean_again():
    # First prose mention + multiple cards = clean (cards don't burn budget).
    html = f'''
    <p>Check out the <a href="{PORTO_WINE}">Porto Wine Tasting Tour</a>.</p>
    <div class="product-card">
        <a href="{PORTO_WINE}">Card 1</a>
    </div>
    <p>And later, another prose mention of the
       <a href="{PORTO_WINE}">Porto Wine Tasting Tour</a> would be a violation.</p>
    '''
    v = _violations(html)
    assert len(v) == 1
    assert v[0].extra["occurrence"] == 2


def test_only_card_links_no_prose_clean():
    html = f'''
    <div class="product-card"><a href="{PORTO_WINE}">Card 1</a></div>
    <div class="comp-card"><a href="{PORTO_WINE}">Card 2</a></div>
    '''
    assert _violations(html) == []


# --- URLs without product codes ------------------------------------------

def test_links_without_extractable_product_code_are_skipped():
    # No product code in URL — nothing to count, never flagged.
    html = '<a href="https://www.viator.com/Porto/">Generic landing</a>'
    assert _violations(html) == []


# --- field-shape sanity ---------------------------------------------------

def test_violation_fields_are_populated():
    html = (
        f'<a href="{PORTO_WINE}">First</a>'
        f'<a href="{PORTO_WINE}">Second</a>'
    )
    v = _violations(html)[0]
    assert v.url == "https://example.com/post"
    assert v.file == "/tmp/fake.html"
    assert v.severity == "major"
    assert v.line >= 1
    assert v.product_code == "3913P1"
    assert v.anchor_text == "Second"


def test_non_viator_links_ignored():
    html = (
        '<a href="https://example.com/a">one</a>'
        '<a href="https://example.com/a">two</a>'
    )
    assert _violations(html) == []