"""Tests for no_viator_pages.py — regression guard for index.html skip bug."""

import tempfile
from pathlib import Path
import subprocess
import sys

SCRIPT = Path(__file__).parent.parent / "no_viator_pages.py"


def _make_site(tmpdir: Path) -> Path:
    """Create a fake directory-based site with index.html pages and utility pages."""
    site = tmpdir / "test-site"
    site.mkdir(parents=True)

    # Root index.html (homepage) — should be SKIPPED (not editorial)
    (site / "index.html").write_text("<html><body>Home</body></html>")

    # Directory-based editorial page — has Viator links
    (site / "things-to-do").mkdir()
    (site / "things-to-do" / "index.html").write_text(
        '<html><body><a href="https://www.viator.com/tours/Madeira/d123-abc">Book now</a></body></html>'
    )

    # Directory-based editorial page — NO Viator links (should be flagged)
    (site / "levada-walks").mkdir()
    (site / "levada-walks" / "index.html").write_text(
        "<html><body>Beautiful walks</body></html>"
    )

    # DE directory-based editorial pages
    (site / "de").mkdir()
    (site / "de" / "index.html").write_text("<html><body>Startseite</body></html>")
    (site / "de" / "aktivitaten").mkdir()
    (site / "de" / "aktivitaten" / "index.html").write_text(
        '<html><body><a href="https://www.viator.com/de/tours">Buchen</a></body></html>'
    )

    # Directory-based UTILITY pages — should be SKIPPED (parent dir name matches SKIP_STEMS)
    (site / "privacidad").mkdir()
    (site / "privacidad" / "index.html").write_text("<html><body>Privacidad</body></html>")
    (site / "datenschutz").mkdir()
    (site / "datenschutz" / "index.html").write_text("<html><body>Datenschutz</body></html>")
    (site / "acerca-de").mkdir()
    (site / "acerca-de" / "index.html").write_text("<html><body>Acerca de</body></html>")

    # Standalone utility page — should still be skipped via stem match
    (site / "about.html").write_text("<html><body>About us</body></html>")

    # Standalone editorial page (non-index) — should still be checked
    (site / "attractions.html").write_text(
        '<html><body><a href="https://www.viator.com/attractions">See all</a></body></html>'
    )

    return site


def test_index_html_in_subdirectories_is_checked():
    """Editorial dir/index.html pages are included; utility dir/index.html skipped."""
    with tempfile.TemporaryDirectory() as td:
        site = _make_site(Path(td))
        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(site)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"Script failed: {result.stderr}"

        # Live editorial pages should be:
        #   things-to-do/index.html     (editorial, has Viator)
        #   levada-walks/index.html     (editorial, no Viator)
        #   de/index.html               (editorial, no Viator)
        #   de/aktivitaten/index.html   (editorial, has Viator)
        #   attractions.html            (editorial standalone, has Viator)
        # Total: 5
        #
        # EXCLUDED:
        #   index.html (root homepage)
        #   privacidad/index.html (utility dir)
        #   datenschutz/index.html (utility dir)
        #   acerca-de/index.html (utility dir)
        #   about.html (utility standalone)

        assert "Live editorial pages: 5" in result.stdout, (
            f"Expected 5 live editorial pages, got:\n{result.stdout}"
        )

        # Pages WITHOUT Viator links:
        #   levada-walks/index.html
        #   de/index.html
        # Total: 2
        assert "Pages without Viator links: 2" in result.stdout, (
            f"Expected 2 pages without Viator links, got:\n{result.stdout}"
        )

        # Specific assertions
        assert "levada-walks/index.html" in result.stdout
        assert "de/index.html" in result.stdout
        assert "📄 index.html" not in result.stdout, "Root homepage should not appear"
        assert "privacidad" not in result.stdout, "Utility dir should be skipped"
        assert "datenschutz" not in result.stdout, "DE utility dir should be skipped"
        assert "acerca-de" not in result.stdout, "ES utility dir should be skipped"


def test_standalone_utility_pages_still_skipped():
    """About, contact, privacy, 404 standalone pages should still be excluded."""
    with tempfile.TemporaryDirectory() as td:
        site = Path(td) / "test-site"
        site.mkdir()
        (site / "about.html").write_text("<html><body>About</body></html>")
        (site / "contact.html").write_text("<html><body>Contact</body></html>")

        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(site)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert "Live editorial pages: 0" in result.stdout, (
            f"Standalone utility pages should be skipped. Output:\n{result.stdout}"
        )


def test_directory_utility_pages_skipped():
    """Directory-based utility pages (privacidad/index.html etc.) are skipped."""
    with tempfile.TemporaryDirectory() as td:
        site = Path(td) / "test-site"
        site.mkdir()

        # ES utility dirs
        (site / "privacidad").mkdir()
        (site / "privacidad" / "index.html").write_text("<html></html>")
        (site / "contacto").mkdir()
        (site / "contacto" / "index.html").write_text("<html></html>")

        # DE utility dirs
        (site / "datenschutz").mkdir()
        (site / "datenschutz" / "index.html").write_text("<html></html>")
        (site / "impressum").mkdir()
        (site / "impressum" / "index.html").write_text("<html></html>")

        # One real editorial dir/index.html
        (site / "tours").mkdir()
        (site / "tours" / "index.html").write_text(
            '<html><a href="https://www.viator.com/tours">Book</a></html>'
        )

        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(site)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0

        # Only tours/index.html should be live
        assert "Live editorial pages: 1" in result.stdout, (
            f"Expected 1 live editorial page (tours/index.html). Output:\n{result.stdout}"
        )
        assert "Pages without Viator links: 0" in result.stdout, (
            f"Expected 0 without Viator. Output:\n{result.stdout}"
        )


def test_madeira_scenario():
    """Simulate Madeira: majority dir/index.html, few standalone .html files."""
    with tempfile.TemporaryDirectory() as td:
        site = Path(td) / "madeira"
        site.mkdir(parents=True)
        (site / "index.html").write_text("<html></html>")

        # 8 directory-based editorial pages — all no Viator
        dir_pages = [
            "levada-walks", "funchal-guide", "hiking-routes",
            "dolphin-watching", "botanical-gardens", "cabo-girao",
            "porto-moniz", "santana",
        ]
        for d in dir_pages:
            (site / d).mkdir()
            (site / d / "index.html").write_text("<html></html>")

        # 2 standalone pages — with Viator
        (site / "tours.html").write_text(
            '<html><a href="https://www.viator.com/tours">Tours</a></html>'
        )
        (site / "attractions.html").write_text(
            '<html><a href="https://www.viator.com/attractions">Attr</a></html>'
        )

        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(site)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0

        # Live editorial: 8 dir/index.html + 2 standalone = 10 (root homepage excluded)
        assert "Live editorial pages: 10" in result.stdout, (
            f"Expected 10 live editorial pages (8 dir/index.html + 2 standalone). "
            f"Output:\n{result.stdout}"
        )

        # All 8 dir/index.html pages have no Viator links
        assert "Pages without Viator links: 8" in result.stdout, (
            f"Expected 8 pages without Viator links. Output:\n{result.stdout}"
        )
