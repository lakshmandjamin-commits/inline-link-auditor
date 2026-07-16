#!/usr/bin/env python3
"""Inline Link Auditor — main CLI.

Scans fleet HTML for 6 inline-linking rule violations from the
Travel Affiliate Inline Linking Framework 2026.

Usage:
    python3 inline_link_auditor.py all                        # all 6 saraswati sites
    python3 inline_link_auditor.py <site>                     # single site
    python3 inline_link_auditor.py <site> --page <slug>        # single page (slug match)
    python3 inline_link_auditor.py all --html                 # include HTML location in output
    python3 inline_link_auditor.py all --json --output /tmp/audit.json

Reads site list from ``../config/sites.yaml`` (relative to this script).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable

try:
    import yaml  # PyYAML
except ImportError:  # pragma: no cover
    yaml = None

# Make sure the inline_link_auditor package is importable when run as a script.
SCRIPT_DIR = Path(__file__).resolve().parent
PKG_DIR = SCRIPT_DIR / "inline_link_auditor"
sys.path.insert(0, str(SCRIPT_DIR))

from inline_link_auditor.models import AuditReport, Violation, FRAMEWORK_VERSION  # noqa: E402
from inline_link_auditor.parser import load_pages  # noqa: E402

# Detectors — imported here so each registers its ``detect()`` function.
from inline_link_auditor.detectors import link_chain, price_adjacency  # noqa: E402
from inline_link_auditor.detectors import specificity, first_mention, trust_gate, disclosure  # noqa: E402

# Order matters for the report's violation-count keys.
DETECTOR_ORDER = [
    "specificity",
    "first_mention",
    "trust_gate",
    "disclosure",
    "link_chain",
    "price_adjacency",
]


def _load_sites(config_path: Path) -> dict[str, dict]:
    """Load sites from sites.yaml. Falls back to a minimal parser if PyYAML is missing."""
    if yaml is not None:
        with config_path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return data.get("sites", {})

    # Minimal fallback parser. The YAML we consume is restricted to:
    #   sites:
    #     <slug>:
    #       path: <value>
    #       domain: <value>
    #       paused: true|false
    sites: dict[str, dict] = {}
    section: str | None = None          # "fleet_defaults" | "sites" | None
    current_slug: str | None = None

    for raw in config_path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            # Top-level key.
            key = line.rstrip(":").strip()
            if key in {"fleet_defaults", "sites"}:
                section = key
            else:
                section = None
            current_slug = None
            continue
        if section != "sites":
            continue
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))
        if indent == 2 and stripped.endswith(":"):
            # A new site slug (e.g. "  foo-bar:").
            current_slug = stripped.rstrip(":").strip()
            sites.setdefault(current_slug, {})
            continue
        if current_slug is None:
            continue
        if ":" in stripped:
            key, _, value = stripped.partition(":")
            key = key.strip()
            value = value.strip()
            if value and not value.startswith("["):
                sites[current_slug][key] = value
    return sites


def _resolve_sites(sites: dict[str, dict], target: str) -> list[tuple[str, dict]]:
    """Map the CLI target ('all', a slug, or a domain) to ``[(slug, config), ...]``."""
    if target == "all":
        return [(slug, cfg) for slug, cfg in sites.items() if not cfg.get("paused")]
    if target in sites:
        return [(target, sites[target])]
    # Domain lookup.
    for slug, cfg in sites.items():
        if cfg.get("domain") == target:
            return [(slug, cfg)]
    raise SystemExit(f"Unknown site: {target!r} (not a slug or domain in sites.yaml)")


def _page_url(slug: str, cfg: dict, filepath: str) -> str:
    """Best-effort page URL for the violation report."""
    domain = cfg.get("domain", f"{slug}.local")
    try:
        rel = Path(filepath).relative_to(cfg["path"])
    except (KeyError, ValueError):
        rel = Path(filepath).name
    return f"https://{domain}/{rel}"


def _audit_site(slug: str, cfg: dict, page_filter: str | None) -> AuditReport:
    """Run all 6 detectors across every page of one site and return the report."""
    site_path = cfg.get("path")
    if not site_path or not Path(site_path).exists():
        print(f"  ⚠️  Site {slug!r} path missing: {site_path}", file=sys.stderr)
        return AuditReport(
            audit_date=datetime.utcnow().isoformat(timespec="seconds") + "Z",
            framework_version=FRAMEWORK_VERSION,
            site=slug,
            pages_scanned=0,
            pages_clean=0,
            violations={k: 0 for k in DETECTOR_ORDER},
        )

    pages = load_pages(site_path)
    if page_filter:
        pages = [(p, h) for p, h in pages if page_filter in p]
    print(f"  {slug}: {len(pages)} page(s)", file=sys.stderr)

    all_violations: list[Violation] = []
    pages_clean = 0
    detectors = {
        "specificity": specificity.detect,
        "first_mention": first_mention.detect,
        "trust_gate": trust_gate.detect,
        "disclosure": disclosure.detect,
        "link_chain": link_chain.detect,
        "price_adjacency": price_adjacency.detect,
    }

    for filepath, html in pages:
        page_url = _page_url(slug, cfg, filepath)
        page_violations: list[Violation] = []
        for name, fn in detectors.items():
            page_violations.extend(fn(html=html, filepath=filepath, url=page_url))
        if not page_violations:
            pages_clean += 1
        all_violations.extend(page_violations)

    counts = {k: 0 for k in DETECTOR_ORDER}
    for v in all_violations:
        counts[v.detector] = counts.get(v.detector, 0) + 1

    return AuditReport(
        audit_date=datetime.utcnow().isoformat(timespec="seconds") + "Z",
        framework_version=FRAMEWORK_VERSION,
        site=slug,
        pages_scanned=len(pages),
        pages_clean=pages_clean,
        violations=counts,
        details=all_violations,
    )


def _print_summary(reports: list[AuditReport]) -> None:
    print()
    print("=" * 70)
    print("Inline Link Auditor — Summary")
    print("=" * 70)
    grand_total = 0
    for r in reports:
        total = sum(r.violations.values())
        grand_total += total
        print(
            f"  {r.site:<28} pages={r.pages_scanned:<5} clean={r.pages_clean:<5} "
            f"violations={total}"
        )
        for k in DETECTOR_ORDER:
            if r.violations.get(k):
                print(f"      {k:<18} {r.violations[k]}")
    print(f"\n  TOTAL violations across {len(reports)} site(s): {grand_total}")


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Inline link auditor for the fleet.")
    ap.add_argument("target", help="'all' or a site slug/domain from sites.yaml")
    ap.add_argument("--page", help="Substring filter: only scan pages whose path contains this")
    ap.add_argument("--html", action="store_true", help="Include HTML location in output (default: JSON only)")
    ap.add_argument("--json", action="store_true", help="Emit JSON to stdout (or --output)")
    ap.add_argument("--output", help="Write JSON report to this path (implies --json)")
    ap.add_argument("--config", help="Override path to sites.yaml")
    args = ap.parse_args(list(argv) if argv is not None else None)

    config_path = Path(args.config) if args.config else SCRIPT_DIR.parent / "config" / "sites.yaml"
    if not config_path.exists():
        print(f"ERROR: sites config not found at {config_path}", file=sys.stderr)
        return 2

    sites = _load_sites(config_path)
    targets = _resolve_sites(sites, args.target)

    reports = [_audit_site(slug, cfg, args.page) for slug, cfg in targets]

    if args.json or args.output:
        payload = {
            "framework_version": FRAMEWORK_VERSION,
            "audit_date": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "sites": [r.to_dict() for r in reports],
        }
        text = json.dumps(payload, indent=2, ensure_ascii=False)
        if args.output:
            Path(args.output).write_text(text, encoding="utf-8")
            print(f"Wrote {args.output}", file=sys.stderr)
        else:
            print(text)
    else:
        _print_summary(reports)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())