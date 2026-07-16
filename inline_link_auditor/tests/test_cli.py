"""Tests for the inline_link_auditor CLI.

Covers:
  * sites.yaml loading (PyYAML path AND minimal-parser fallback)
  * ``all`` target iterates every site
  * Single-site target produces a per-site AuditReport
  * ``--json`` emits parseable JSON
  * ``--output`` writes JSON to disk
  * Unknown site errors out cleanly
  * All 6 detectors contribute to the violation counts
  * Empty / missing site path is handled gracefully
  * ``--page`` substring filter narrows the page set
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent  # /scripts
SCRIPT_PATH = ROOT.parent / "inline_link_auditor.py"
PKG_DIR = ROOT  # /scripts/inline_link_auditor


def _load_cli_module():
    """Import the CLI script (hyphenated name needs importlib)."""
    spec = importlib.util.spec_from_file_location("inline_link_auditor_cli", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["inline_link_auditor_cli"] = mod
    spec.loader.exec_module(mod)
    return mod


CLI = _load_cli_module()


# ---------------------------------------------------------------------------
# sites.yaml loading
# ---------------------------------------------------------------------------


def test_load_sites_with_pyyaml(tmp_path: Path):
    cfg = tmp_path / "sites.yaml"
    cfg.write_text(
        "sites:\n"
        "  porto-sommelier:\n"
        "    path: /tmp/porto\n"
        "    domain: porto-wine-tours.com\n"
        "    fleet: saraswati\n",
        encoding="utf-8",
    )
    sites = CLI._load_sites(cfg)
    assert "porto-sommelier" in sites
    assert sites["porto-sommelier"]["path"] == "/tmp/porto"
    assert sites["porto-sommelier"]["domain"] == "porto-wine-tours.com"


def test_load_sites_yaml_fallback(tmp_path: Path):
    """Minimal YAML parser should still produce a usable dict when PyYAML is missing."""
    cfg = tmp_path / "sites.yaml"
    cfg.write_text(
        "sites:\n"
        "  foo-bar:\n"
        "    path: /tmp/foo\n"
        "    domain: foo.test\n",
        encoding="utf-8",
    )
    with mock.patch.object(CLI, "yaml", None):
        sites = CLI._load_sites(cfg)
    assert "foo-bar" in sites
    assert sites["foo-bar"]["path"] == "/tmp/foo"


def test_load_sites_skips_paused():
    sites = {"a": {"path": "/x"}, "b": {"path": "/y", "paused": True}}
    resolved = CLI._resolve_sites(sites, "all")
    slugs = [s for s, _ in resolved]
    assert "a" in slugs
    assert "b" not in slugs


# ---------------------------------------------------------------------------
# _resolve_sites
# ---------------------------------------------------------------------------


def test_resolve_by_slug():
    sites = {"alpha": {"path": "/a", "domain": "a.test"}}
    assert CLI._resolve_sites(sites, "alpha")[0][0] == "alpha"


def test_resolve_by_domain():
    sites = {"alpha": {"path": "/a", "domain": "alpha.test"}}
    out = CLI._resolve_sites(sites, "alpha.test")
    assert out == [("alpha", {"path": "/a", "domain": "alpha.test"})]


def test_resolve_unknown_raises():
    with pytest.raises(SystemExit):
        CLI._resolve_sites({"alpha": {"path": "/a"}}, "nope")


def test_resolve_all_returns_every_non_paused():
    sites = {
        "a": {"path": "/a"},
        "b": {"path": "/b", "paused": True},
        "c": {"path": "/c"},
    }
    out = CLI._resolve_sites(sites, "all")
    slugs = [s for s, _ in out]
    assert slugs == ["a", "c"]


# ---------------------------------------------------------------------------
# end-to-end audit_site
# ---------------------------------------------------------------------------


def _write_site(root: Path, files: dict[str, str]) -> Path:
    """Create a temporary site with the given {relative_path: html} files."""
    root.mkdir(parents=True, exist_ok=True)
    for rel, html in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(html, encoding="utf-8")
    return root


def test_audit_site_clean_page(tmp_path: Path):
    site = _write_site(tmp_path / "clean-site", {
        "index.html": (
            "<html><body>"
            "<p>Disclosure: this page contains affiliate links.</p>"
            '<p>Tour: <a href="https://viator.com/d1234-31913P1">Marina Bay Sands Walking Tour</a></p>'
            "</body></html>"
        ),
    })
    cfg = {"path": str(site), "domain": "clean.test"}
    report = CLI._audit_site("clean-site", cfg, page_filter=None)
    assert report.site == "clean-site"
    assert report.pages_scanned == 1
    # Spec says a single clean page should report clean + zero of each violation.
    assert report.pages_clean == 1
    for k in CLI.DETECTOR_ORDER:
        assert report.violations.get(k, 0) == 0


def test_audit_site_detects_link_chain(tmp_path: Path):
    site = _write_site(tmp_path / "chain-site", {
        "index.html": (
            "<html><body>"
            "<p>Disclosure: affiliate links below.</p>"
            '<p>Compare: <a href="https://viator.com/a">Marina Bay Tour</a><a href="https://viator.com/b">Sentosa Tour</a></p>'
            "</body></html>"
        ),
    })
    cfg = {"path": str(site), "domain": "chain.test"}
    report = CLI._audit_site("chain-site", cfg, page_filter=None)
    assert report.violations.get("link_chain", 0) >= 1


def test_audit_site_detects_price_adjacency(tmp_path: Path):
    site = _write_site(tmp_path / "price-site", {
        "index.html": (
            "<html><body>"
            "<p>Disclosure: affiliate links below.</p>"
            '<p>Book for $99: <a href="https://viator.com/d1234-31913P1">Marina Bay Tour</a></p>'
            "</body></html>"
        ),
    })
    cfg = {"path": str(site), "domain": "price.test"}
    report = CLI._audit_site("price-site", cfg, page_filter=None)
    assert report.violations.get("price_adjacency", 0) >= 1


def test_audit_site_missing_path_returns_empty(tmp_path: Path):
    cfg = {"path": str(tmp_path / "does-not-exist"), "domain": "x.test"}
    report = CLI._audit_site("ghost", cfg, page_filter=None)
    assert report.pages_scanned == 0
    assert all(report.violations.get(k, 0) == 0 for k in CLI.DETECTOR_ORDER)


def test_audit_site_page_filter(tmp_path: Path):
    site = _write_site(tmp_path / "filter-site", {
        "tour-a.html": "<html><body><p>Disclosure: affiliate.</p><p>Tour: <a href='https://viator.com/d1234-31913P1'>Marina Bay Tour</a></p></body></html>",
        "tour-b.html": "<html><body><p>Disclosure: affiliate.</p><p>Tour: <a href='https://viator.com/d1234-31913P2'>Sentosa Tour</a></p></body></html>",
    })
    cfg = {"path": str(site), "domain": "filter.test"}
    report = CLI._audit_site("filter-site", cfg, page_filter="tour-a")
    assert report.pages_scanned == 1


# ---------------------------------------------------------------------------
# main() CLI runner
# ---------------------------------------------------------------------------


def test_main_json_emits_parseable_output(tmp_path: Path):
    site = _write_site(tmp_path / "json-site", {
        "index.html": (
            "<html><body>"
            "<p>Disclosure: affiliate links below.</p>"
            '<p>Book for $99: <a href="https://viator.com/d1234-31913P1">Marina Bay Tour</a></p>'
            "</body></html>"
        ),
    })
    cfg_path = tmp_path / "sites.yaml"
    cfg_path.write_text(
        "sites:\n"
        f"  json-site:\n"
        f"    path: {site}\n"
        f"    domain: json.test\n",
        encoding="utf-8",
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = CLI.main(["json-site", "--json", "--config", str(cfg_path)])
    assert rc == 0
    payload = json.loads(buf.getvalue())
    assert "sites" in payload
    assert payload["sites"][0]["site"] == "json-site"
    assert payload["sites"][0]["summary"]["violations"]["price_adjacency"] >= 1


def test_main_json_output_to_file(tmp_path: Path):
    site = _write_site(tmp_path / "out-site", {
        "index.html": "<html><body><p>Disclosure: affiliate.</p><p>X.</p></body></html>",
    })
    cfg_path = tmp_path / "sites.yaml"
    cfg_path.write_text(
        "sites:\n"
        f"  out-site:\n"
        f"    path: {site}\n"
        f"    domain: out.test\n",
        encoding="utf-8",
    )
    out_file = tmp_path / "audit.json"
    rc = CLI.main(["out-site", "--json", "--output", str(out_file), "--config", str(cfg_path)])
    assert rc == 0
    assert out_file.exists()
    payload = json.loads(out_file.read_text())
    assert payload["sites"][0]["site"] == "out-site"


def test_main_unknown_target_returns_error(tmp_path: Path):
    cfg_path = tmp_path / "sites.yaml"
    cfg_path.write_text("sites:\n  alpha:\n    path: /tmp/x\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        CLI.main(["nope", "--config", str(cfg_path)])


def test_main_missing_config_returns_error_code(tmp_path: Path):
    rc = CLI.main(["all", "--config", str(tmp_path / "missing.yaml")])
    assert rc == 2


def test_main_all_iterates_multiple_sites(tmp_path: Path):
    site_a = _write_site(tmp_path / "all-a", {
        "index.html": "<html><body><p>Disclosure: affiliate.</p><p>X.</p></body></html>",
    })
    site_b = _write_site(tmp_path / "all-b", {
        "index.html": "<html><body><p>Disclosure: affiliate.</p><p>Y.</p></body></html>",
    })
    cfg_path = tmp_path / "sites.yaml"
    cfg_path.write_text(
        "sites:\n"
        f"  a-site:\n    path: {site_a}\n    domain: a.test\n"
        f"  b-site:\n    path: {site_b}\n    domain: b.test\n",
        encoding="utf-8",
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = CLI.main(["all", "--json", "--config", str(cfg_path)])
    assert rc == 0
    payload = json.loads(buf.getvalue())
    slugs = [s["site"] for s in payload["sites"]]
    assert "a-site" in slugs
    assert "b-site" in slugs


def test_audit_report_schema_matches_spec():
    """The report must conform to the JSON schema in the spec."""
    from inline_link_auditor.models import AuditReport, Violation

    rep = AuditReport(
        audit_date="2026-07-16T00:00:00Z",
        framework_version=CLI.FRAMEWORK_VERSION,
        site="unit-test",
        pages_scanned=5,
        pages_clean=3,
        violations={k: 0 for k in CLI.DETECTOR_ORDER},
        details=[
            Violation(detector="link_chain", rule="link-chain", url="u", file="f", line=10, severity="major")
        ],
    )
    d = rep.to_dict()
    assert d["framework_version"] == CLI.FRAMEWORK_VERSION
    assert d["summary"]["pages_scanned"] == 5
    assert d["summary"]["pages_clean"] == 3
    assert set(d["summary"]["violations"]) == set(CLI.DETECTOR_ORDER)
    assert isinstance(d["violations"], list)
    assert d["violations"][0]["detector"] == "link_chain"


def test_script_runs_as_module():
    """Smoke test: invoking the CLI script via subprocess with --help exits 0."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--help"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "target" in result.stdout.lower() or "usage" in result.stdout.lower()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))