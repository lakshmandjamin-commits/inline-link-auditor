#!/usr/bin/env python3.11
"""
audit-action-loop.py — Component 3 of the Viator Image Pipeline.

Post-generation action loop:
  1. Take a list of newly generated page URLs.
  2. Run the image placement audit (audit_image_placement/.venv).
  3. For every page with an R12 violation (zero images on a non-blog page):
       - Look for product images locally in the site's /images/ dir.
       - If found, auto-inject an <img> tag (atomic, idempotent).
       - If not, queue the product code(s) for the Viator API fetcher.
  4. Log: injected count, queued count, pages that need manual curation.

This script is the thin orchestrator. The actual work is split across
sibling modules:

    audit_action.url_utils        URL <-> site mapping, URL file loading
    audit_action.site_registry    SiteConfig + sites.yaml loader
    audit_action.audit_runner     Wraps the image-placement audit CLI
    audit_action.image_injection  Viator code extraction + <img> injection
    audit_action.queue_io         Append-only queue files

We re-export the public names here so existing imports (and the test suite,
which loads this file via importlib) keep working unchanged.

Usage:
  python3 audit-action-loop.py --urls /tmp/newly-generated.txt
  python3 audit-action-loop.py --urls /tmp/newly-generated.txt --site porto-sommelier
  python3 audit-action-loop.py --urls /tmp/newly-generated.txt --fleet saraswati
  python3 audit-action-loop.py --urls /tmp/newly-generated.txt --dry-run
  python3 audit-action-loop.py --urls /tmp/newly-generated.txt --audit-output /tmp/audit.json
  python3 audit-action-loop.py --urls /tmp/newly-generated.txt --no-audit   # use cached audit JSON

URL file format: one URL per line, '#' starts a comment, blank lines ignored.

Pid: P00303273
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

# ── Re-exports: keep the old single-file public API stable ─────────────────
from audit_action.audit_runner import (
    R12_RULE_ID,
    has_r12_violation,
    run_audit,
)
from audit_action.image_injection import (
    DEFAULT_INJECT_IMG_TEMPLATE,
    VIATOR_CODE_RE,
    VIATOR_LINK_RE,
    build_img_tag,
    extract_product_codes,
    extract_product_codes_for_queue,
    find_local_product_image,
    inject_image_into_file,
    inject_image_into_html,
)
from audit_action.queue_io import append_to_queue
from audit_action.site_registry import (
    SiteConfig,
    load_site_registry,
    load_sites_yaml,
)
from audit_action.url_utils import (
    load_urls,
    strip_www,
    url_to_local_file,
    url_to_site,
)

# ── Constants ───────────────────────────────────────────────────────────────

DEFAULT_SITES_YAML = Path.home() / ".hermes/affiliate-crons/config/sites.yaml"
DEFAULT_AUDIT_DIR = (
    Path.home() / ".hermes/profiles/lakshman/home/audit_image_placement"
)
DEFAULT_LOG_DIR = (
    Path.home() / ".hermes/affiliate-crons/logs/audit-action"
)
DEFAULT_QUEUE_DIR = (
    Path.home() / ".hermes/affiliate-crons/queues/image-fetch"
)

# Kept for backward-compat with any external caller that imported the
# constants from this module directly.
__all__ = [
    "SiteConfig",
    "PageAction",
    "LoopReport",
    "R12_RULE_ID",
    "VIATOR_CODE_RE",
    "VIATOR_LINK_RE",
    "DEFAULT_INJECT_IMG_TEMPLATE",
    "DEFAULT_SITES_YAML",
    "DEFAULT_AUDIT_DIR",
    "DEFAULT_LOG_DIR",
    "DEFAULT_QUEUE_DIR",
    # functions
    "load_sites_yaml",
    "load_site_registry",
    "url_to_site",
    "url_to_local_file",
    "load_urls",
    "strip_www",
    "run_audit",
    "has_r12_violation",
    "extract_product_codes",
    "extract_product_codes_for_queue",
    "find_local_product_image",
    "build_img_tag",
    "inject_image_into_html",
    "inject_image_into_file",
    "append_to_queue",
    "process_page",
    "run",
    "main",
]


# ── Result aggregation ──────────────────────────────────────────────────────


@dataclass
class PageAction:
    url: str
    site: str | None
    status: str                  # "ok" | "audit_error" | "injected" | "queued" | "manual_curation"
    page_type: str
    r12_violation: bool
    injected_filename: str | None = None
    queued_codes: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class LoopReport:
    started_at: str
    finished_at: str
    sites_yaml: str
    url_count: int
    pages_with_r12: int
    pages_injected: int
    pages_queued: int
    pages_manual_curation: list[dict]
    queue_files_written: list[str]
    pages: list[PageAction] = field(default_factory=list)


# ── Per-page decisioning ────────────────────────────────────────────────────


def process_page(
    url: str,
    audit_result: dict,
    sites: dict[str, SiteConfig],
    queue_dir: Path,
    dry_run: bool,
) -> tuple[PageAction, list[Path]]:
    """
    Decide what to do with one page given its audit result. Returns
    (page_action, queue_files_written). Side effects: writes to disk only
    when not dry_run.
    """
    site_slug = url_to_site(url, sites)
    page_type = audit_result.get("page_type", "unknown")
    audit_status = audit_result.get("status", "ok")
    r12 = has_r12_violation(audit_result)

    base = PageAction(
        url=url,
        site=site_slug,
        status="ok",
        page_type=page_type,
        r12_violation=r12,
    )

    # Audit failed entirely — leave it for human investigation
    if audit_status != "ok":
        base.status = "audit_error"
        bits = [f"audit status={audit_status}"]
        if audit_result.get("error"):
            bits.append(str(audit_result["error"]))
        base.reason = " | ".join(bits)
        return base, []

    # Not R12 — nothing to do
    if not r12:
        return base, []

    # R12: find the page file & try to fix
    if not site_slug:
        base.status = "manual_curation"
        base.reason = "R12 violation but URL did not match any site in sites.yaml"
        return base, []

    cfg = sites[site_slug]
    page_file = url_to_local_file(url, cfg)
    if not page_file or not page_file.exists():
        # Page not on disk yet — could be a preview URL
        base.status = "manual_curation"
        base.reason = f"R12 violation but page file not found locally ({page_file})"
        return base, []

    html = page_file.read_text()
    local_image = find_local_product_image(html, cfg.images_dir)

    queue_files: list[Path] = []
    if local_image:
        # We have a local image — inject it
        if not dry_run:
            inject_image_into_file(page_file, local_image, dry_run=False)
        else:
            inject_image_into_html(html, local_image)  # exercise path for dry-run
        base.status = "injected"
        base.injected_filename = local_image
        # Anything else to queue? Only if other codes on the page lack images.
        codes_to_queue = extract_product_codes_for_queue(html, local_image)
        if codes_to_queue and cfg.images_dir:
            codes_with_images = [
                c for c in codes_to_queue
                if any((cfg.images_dir / f"{c}{ext}").exists()
                       for ext in (".jpg", ".jpeg", ".png", ".webp"))
            ]
            codes_to_queue = [c for c in codes_to_queue if c not in codes_with_images]
        if codes_to_queue:
            qfile = queue_dir / f"{site_slug}-{datetime.now():%Y%m%d}.queue"
            if not dry_run:
                append_to_queue(qfile, codes_to_queue)
            queue_files.append(qfile)
            base.queued_codes = codes_to_queue
        return base, queue_files

    # No local image — queue all product codes
    codes = extract_product_codes(html)
    if not codes:
        base.status = "manual_curation"
        base.reason = "R12 violation, no product codes found in HTML (no Viator link to anchor image)"
        return base, []

    qfile = queue_dir / f"{site_slug}-{datetime.now():%Y%m%d}.queue"
    if not dry_run:
        append_to_queue(qfile, codes)
    queue_files.append(qfile)
    base.status = "queued"
    base.queued_codes = codes
    return base, queue_files


# ── Main pipeline ───────────────────────────────────────────────────────────


def run(
    urls: list[str],
    sites_yaml: Path,
    audit_dir: Path,
    queue_dir: Path,
    log_dir: Path,
    audit_output: Path | None = None,
    no_audit: bool = False,
    cached_audit: Path | None = None,
    dry_run: bool = False,
    viewport: str = "1280x720",
    concurrency: int = 3,
    site_filter: str | None = None,
    fleet_filter: str | None = None,
) -> LoopReport:
    sites = load_site_registry(sites_yaml, site_filter, fleet_filter)
    queue_dir = Path(queue_dir)
    log_dir = Path(log_dir)

    started = datetime.now().isoformat(timespec="seconds")

    # Step 1 — load or run audit
    if no_audit and cached_audit:
        audit_results_raw = json.loads(Path(cached_audit).read_text())
        if isinstance(audit_results_raw, dict):
            audit_results_raw = [audit_results_raw]
        audit_results = audit_results_raw
    else:
        audit_results = run_audit(
            urls,
            audit_dir=audit_dir,
            output_json=audit_output,
            viewport=viewport,
            concurrency=concurrency,
        )

    # Step 2 — align URLs to results (CLI may return single dict for len==1)
    if isinstance(audit_results, dict):
        audit_results = [audit_results]
    by_url = {r.get("url"): r for r in audit_results if r.get("url")}

    pages: list[PageAction] = []
    queue_files: set[Path] = set()

    for url in urls:
        result = by_url.get(url) or {"url": url, "status": "missing", "violations": [], "page_type": "unknown"}
        action, qfiles = process_page(url, result, sites, queue_dir, dry_run)
        pages.append(action)
        for q in qfiles:
            queue_files.add(q)

    finished = datetime.now().isoformat(timespec="seconds")

    # Step 3 — aggregate
    pages_with_r12 = sum(1 for p in pages if p.r12_violation)
    pages_injected = sum(1 for p in pages if p.status == "injected")
    pages_queued = sum(1 for p in pages if p.status == "queued")
    manual = [
        {"url": p.url, "site": p.site, "reason": p.reason, "page_type": p.page_type}
        for p in pages
        if p.status in ("manual_curation", "audit_error")
    ]

    report = LoopReport(
        started_at=started,
        finished_at=finished,
        sites_yaml=str(sites_yaml),
        url_count=len(urls),
        pages_with_r12=pages_with_r12,
        pages_injected=pages_injected,
        pages_queued=pages_queued,
        pages_manual_curation=manual,
        queue_files_written=[str(q) for q in sorted(queue_files)],
        pages=pages,
    )

    # Step 4 — log to disk
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"audit-action-{datetime.now():%Y%m%d-%H%M%S}.json"
    log_file.write_text(json.dumps(asdict(report), indent=2, default=str) + "\n")

    return report


# ── CLI ─────────────────────────────────────────────────────────────────────


def _print_summary(report: LoopReport) -> None:
    print(f"\nAudit-Action Loop — {report.started_at} → {report.finished_at}")
    print(f"  URLs processed       : {report.url_count}")
    print(f"  Pages with R12       : {report.pages_with_r12}")
    print(f"  Pages auto-injected  : {report.pages_injected}")
    print(f"  Pages queued         : {report.pages_queued}")
    print(f"  Manual curation      : {len(report.pages_manual_curation)}")
    if report.queue_files_written:
        print("  Queue files written  :")
        for q in report.queue_files_written:
            print(f"    - {q}")
    if report.pages_manual_curation:
        print("  Pages needing manual curation:")
        for m in report.pages_manual_curation:
            print(f"    - [{m['site'] or '?'}] {m['url']}  ({m['reason']})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit-Action Loop: detect R12 violations, inject local images, queue the rest.",
    )
    parser.add_argument("--urls", required=True, type=Path,
                        help="File of newly generated page URLs (one per line)")
    parser.add_argument("--site", help="Filter to a single site slug (e.g. porto-sommelier)")
    parser.add_argument("--fleet", help="Filter to a single fleet (saraswati | hanumanhermes)")
    parser.add_argument("--sites-yaml", type=Path, default=DEFAULT_SITES_YAML,
                        help="Path to sites.yaml (default: ~/.hermes/affiliate-crons/config/sites.yaml)")
    parser.add_argument("--audit-dir", type=Path, default=DEFAULT_AUDIT_DIR,
                        help="Path to audit_image_placement/ (default: ~/.hermes/profiles/lakshman/home/audit_image_placement)")
    parser.add_argument("--queue-dir", type=Path, default=DEFAULT_QUEUE_DIR,
                        help="Directory for fetch queues (default: ~/.hermes/affiliate-crons/queues/image-fetch)")
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR,
                        help="Directory for run logs (default: ~/.hermes/affiliate-crons/logs/audit-action)")
    parser.add_argument("--audit-output", type=Path,
                        help="If set, write the raw audit JSON here")
    parser.add_argument("--no-audit", action="store_true",
                        help="Skip running the audit; require --cached-audit")
    parser.add_argument("--cached-audit", type=Path,
                        help="Path to a saved audit JSON (use with --no-audit)")
    parser.add_argument("--viewport", default="1280x720")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview only — do not write to disk or queues")
    parser.add_argument("--report", type=Path,
                        help="Write the final aggregated report JSON to this path")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress the human-readable summary")
    args = parser.parse_args(argv)

    if not args.urls.exists():
        print(f"ERROR: URL file not found: {args.urls}", file=sys.stderr)
        return 2

    if args.no_audit and not args.cached_audit:
        print("ERROR: --no-audit requires --cached-audit", file=sys.stderr)
        return 2

    urls = load_urls(args.urls)
    if not urls:
        print("No URLs to process.", file=sys.stderr)
        return 0

    try:
        report = run(
            urls=urls,
            sites_yaml=args.sites_yaml,
            audit_dir=args.audit_dir,
            queue_dir=args.queue_dir,
            log_dir=args.log_dir,
            audit_output=args.audit_output,
            no_audit=args.no_audit,
            cached_audit=args.cached_audit,
            dry_run=args.dry_run,
            viewport=args.viewport,
            concurrency=args.concurrency,
            site_filter=args.site,
            fleet_filter=args.fleet,
        )
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    if not args.quiet:
        _print_summary(report)

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(asdict(report), indent=2, default=str) + "\n")
        if not args.quiet:
            print(f"\nReport: {args.report}")

    # Always print a log line so cron captures something useful
    print(
        f"[audit-action-loop] urls={report.url_count} "
        f"r12={report.pages_with_r12} injected={report.pages_injected} "
        f"queued={report.pages_queued} manual={len(report.pages_manual_curation)} "
        f"dry_run={args.dry_run}"
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
