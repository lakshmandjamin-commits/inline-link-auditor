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

Fleet-agnostic: reads the site list from ~/.hermes/affiliate-crons/config/sites.yaml,
which is shared with the image fetcher. Both Saraswati and Hanumanhermes fleets
are supported — the script works against any subset selected with --site or
--fleet.

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
import os
import re
import shutil
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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

R12_RULE_ID = "R12"

# Matches Viator product links like
#   https://www.viator.com/.../d1234-ABC123?...   →  ABC123
#   https://www.viator.com/.../ABC123?...          →  ABC123
VIATOR_CODE_RE = re.compile(
    r'viator\.com/[^\s"\']*/(?:d\d+-)?([A-Z][A-Z0-9]+)\?',
    re.IGNORECASE,
)
VIATOR_LINK_RE = re.compile(
    r'(<a\s[^>]*href="https?://[^\"]*viator\.com[^\"]*"[^>]*>)',
    re.IGNORECASE,
)


# ── Tiny YAML loader (handles the limited syntax our sites.yaml uses) ───────
# We don't want to require PyYAML at runtime. The sites.yaml we generate is
# pure YAML 1.1 — keys, scalars, lists, nested maps. This parser handles that.

def _parse_yaml_minimal(text: str) -> dict:
    """
    Minimal YAML loader for sites.yaml. Supports:
      - key: value
      - key:\n  nested-key: value
      - key:\n    - item\n    - item
      - comments starting with '#'
    Raises ValueError on unsupported constructs so the caller can fall back
    to PyYAML if installed.
    """
    lines = []
    for raw in text.splitlines():
        # strip trailing comments but not '#' inside quoted strings
        # (our file has no quoted scalars so a simple split is safe)
        hash_pos = raw.find("#")
        if hash_pos >= 0 and not raw[:hash_pos].rstrip().endswith('"'):
            raw = raw[:hash_pos]
        stripped = raw.rstrip()
        if not stripped.strip():
            continue
        lines.append(stripped)

    root: dict = {}
    # stack of (indent, container)
    stack: list[tuple[int, Any]] = [(-1, root)]

    def peek_next_is_list(idx: int) -> bool:
        """True if the next non-empty line starts with '- ' at greater indent."""
        for nxt in lines[idx + 1:]:
            nxt_stripped = nxt.strip()
            if not nxt_stripped:
                continue
            nxt_indent = len(nxt) - len(nxt.lstrip(" "))
            return nxt_stripped.startswith("- ") and nxt_indent > indent
        return False

    for idx, line in enumerate(lines):
        indent = len(line) - len(line.lstrip(" "))
        content = line.strip()
        # pop containers that don't contain this line
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent_indent, parent = stack[-1]

        if content.startswith("- "):
            # list item
            item_val = content[2:].strip()
            if not isinstance(parent, list):
                raise ValueError(f"unexpected list item: {line!r}")
            if ":" in item_val and not item_val.startswith('"'):
                # inline mapping start: "- key: value"
                key, _, val = item_val.partition(":")
                val = val.strip()
                new_map: dict = {}
                new_map[key.strip()] = _coerce(val)
                parent.append(new_map)
                # push map at indent+2 so nested keys hang off it
                stack.append((indent + 2, new_map))
            else:
                parent.append(_coerce(item_val))
        elif ":" in content:
            key, _, val = content.partition(":")
            key = key.strip()
            val = val.strip()
            if val == "":
                if not isinstance(parent, dict):
                    raise ValueError(f"unexpected map under non-dict: {line!r}")
                # Decide map vs list by peeking ahead
                if peek_next_is_list(idx):
                    new_container: list = []
                    parent[key] = new_container
                    stack.append((indent, new_container))
                else:
                    new_container = {}
                    parent[key] = new_container
                    stack.append((indent, new_container))
            else:
                if not isinstance(parent, dict):
                    raise ValueError(f"unexpected map key: {line!r}")
                parent[key] = _coerce(val)

    return root


def _coerce(val: str) -> Any:
    """Coerce a scalar string to int/float/bool/str."""
    if val == "" or val is None:
        return val
    if val.lower() in ("true", "yes"):
        return True
    if val.lower() in ("false", "no"):
        return False
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        if not inner:
            return []
        return [_coerce(v.strip()) for v in inner.split(",")]
    try:
        return int(val)
    except ValueError:
        pass
    try:
        return float(val)
    except ValueError:
        pass
    return val


def load_sites_yaml(path: Path) -> dict:
    """Load sites.yaml. Tries PyYAML first, falls back to the minimal parser."""
    text = path.read_text()
    try:
        import yaml  # type: ignore[import-not-found]
        return yaml.safe_load(text) or {}
    except ImportError:
        return _parse_yaml_minimal(text)


# ── Site registry ───────────────────────────────────────────────────────────

@dataclass
class SiteConfig:
    slug: str
    path: Path
    domain: str = ""
    fleet: str = ""
    images_dir: Path | None = None
    languages: list[str] = field(default_factory=list)
    paused: bool = False


def load_site_registry(
    yaml_path: Path = DEFAULT_SITES_YAML,
    site_filter: str | None = None,
    fleet_filter: str | None = None,
) -> dict[str, SiteConfig]:
    """
    Load sites.yaml and return a {slug: SiteConfig} dict. Optionally filter
    by site slug or fleet name. Filter is an exact match (no glob).
    """
    if not yaml_path.exists():
        raise FileNotFoundError(
            f"sites.yaml not found at {yaml_path}. "
            "Create it (see ~/.hermes/affiliate-crons/config/sites.yaml) "
            "or pass --sites-yaml to point elsewhere."
        )
    raw = load_sites_yaml(yaml_path)
    sites_raw = raw.get("sites", {}) or {}
    out: dict[str, SiteConfig] = {}
    for slug, cfg in sites_raw.items():
        if site_filter and slug != site_filter:
            continue
        if fleet_filter and cfg.get("fleet") != fleet_filter:
            continue
        if cfg.get("paused"):
            continue
        path = Path(os.path.expanduser(cfg["path"]))
        images_dir = cfg.get("images_dir")
        out[slug] = SiteConfig(
            slug=slug,
            path=path,
            domain=cfg.get("domain", ""),
            fleet=cfg.get("fleet", ""),
            images_dir=Path(os.path.expanduser(images_dir)) if images_dir else (path / "images"),
            languages=list(cfg.get("languages", []) or []),
            paused=bool(cfg.get("paused", False)),
        )
    return out


# ── URL → site mapping ──────────────────────────────────────────────────────

def url_to_site(url: str, sites: dict[str, SiteConfig]) -> str | None:
    """Match a URL to a site slug by hostname or by site path prefix."""
    parsed = urlparse(url)
    host = parsed.netloc.lower().lstrip("www.")

    # 1) Match by domain
    for slug, cfg in sites.items():
        if cfg.domain and cfg.domain.lower().lstrip("www.") == host:
            return slug

    # 2) Fallback: path-based heuristic. Local files have file:// or absolute paths.
    #    "https://porto-wine-tours.com/foo" → slug whose domain matches, else None.
    #    For file:// URLs the path may be the actual site root.
    for slug, cfg in sites.items():
        if cfg.path and url.startswith(str(cfg.path)):
            return slug
        if cfg.path and cfg.path.name in url:
            return slug

    # 3) Hostname fuzzy match (e.g. "porto-wine-tours.com" → "porto-sommelier")
    for slug, cfg in sites.items():
        if cfg.domain:
            d = cfg.domain.lower().lstrip("www.")
            if d.split(".")[0] in host:
                return slug

    return None


# ── URL file loader ─────────────────────────────────────────────────────────

def load_urls(path: Path) -> list[str]:
    """Read URLs from a file (one per line, '#' = comment)."""
    urls: list[str] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        urls.append(line)
    return urls


# ── Audit runner ────────────────────────────────────────────────────────────

def run_audit(
    urls: list[str],
    audit_dir: Path,
    output_json: Path | None = None,
    viewport: str = "1280x720",
    concurrency: int = 3,
) -> list[dict]:
    """
    Run the image placement audit CLI against a list of URLs.
    Returns the parsed JSON report (a list of page-result dicts, or a single
    dict if only one URL).
    """
    # Import the audit tool as a package so its relative imports work.
    # The audit tool's `src/` directory is the package (it has __init__.py).
    audit_src = audit_dir / "src"
    if not audit_src.exists():
        raise FileNotFoundError(
            f"Audit tool not found at {audit_dir}. "
            "Pass --audit-dir to override."
        )
    audit_parent = str(audit_dir)
    if audit_parent not in sys.path:
        sys.path.insert(0, audit_parent)
    from src.cli import audit_batch  # type: ignore[import-not-found]

    results = audit_batch(urls, viewport=viewport, concurrency=concurrency)
    if output_json:
        output_json.parent.mkdir(parents=True, exist_ok=True)
        payload = results[0] if len(results) == 1 else results
        output_json.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    return results


def has_r12_violation(audit_result: dict) -> bool:
    """True if the page result has at least one R12 violation."""
    for v in audit_result.get("violations", []) or []:
        if v.get("rule_id") == R12_RULE_ID:
            return True
    return False


# ── Local image lookup ──────────────────────────────────────────────────────

def find_local_product_image(
    html: str,
    images_dir: Path,
) -> str | None:
    """
    For an R12-violating page, find the primary product code mentioned in
    the HTML and check whether its image exists locally. Returns the
    image's filename (e.g. "ABC123.jpg") if found, else None.
    """
    codes = extract_product_codes(html)
    if not codes:
        return None
    images_dir = Path(images_dir)
    if not images_dir.is_dir():
        return None
    for code in codes:
        for ext in (".jpg", ".jpeg", ".png", ".webp"):
            candidate = images_dir / f"{code}{ext}"
            if candidate.exists():
                return candidate.name
    return None


def extract_product_codes(html: str) -> list[str]:
    """Extract Viator product codes from a page's HTML (deduped, order preserved)."""
    seen: set[str] = set()
    out: list[str] = []
    for m in VIATOR_CODE_RE.finditer(html):
        code = m.group(1).upper()
        if code not in seen:
            seen.add(code)
            out.append(code)
    return out


def extract_product_codes_for_queue(html: str, primary_image_filename: str | None) -> list[str]:
    """
    Return the product codes that need to be queued for Viator fetch.
    If primary_image_filename is set, that means injection succeeded —
    skip those codes (they're already covered). Return only the rest.
    """
    all_codes = extract_product_codes(html)
    if primary_image_filename:
        # crude: strip extension from filename and compare uppercase
        covered = primary_image_filename.rsplit(".", 1)[0].upper()
        return [c for c in all_codes if c != covered]
    return all_codes


# ── Image injection ─────────────────────────────────────────────────────────

DEFAULT_INJECT_IMG_TEMPLATE = (
    '<img src="/images/{filename}" alt="{alt}" '
    'width="800" height="533" loading="lazy" '
    'class="injected-product-image">'
)


def build_img_tag(filename: str, alt: str = "Product image") -> str:
    """Build a self-contained <img> tag. Public so other tools can import it."""
    safe_alt = (
        alt.replace("&", "&amp;")
           .replace('"', "&quot;")
           .replace("<", "&lt;")
           .replace(">", "&gt;")
    )
    safe_alt = safe_alt[:125]  # accessibility cap
    return DEFAULT_INJECT_IMG_TEMPLATE.format(filename=filename, alt=safe_alt)


def inject_image_into_html(html: str, image_filename: str, alt: str = "Product image") -> str:
    """
    Insert an <img> tag before the first Viator <a> link on the page. If no
    Viator link exists, insert after the first <h1> as a last resort.

    Idempotent: if an injected-product-image already exists, skip.
    Atomic at the caller level (we only return modified text).
    """
    if 'class="injected-product-image"' in html or "injected-product-image" in html:
        return html

    img_tag = build_img_tag(image_filename, alt)

    # Prefer: right before first Viator link
    match = VIATOR_LINK_RE.search(html)
    if match:
        return html[: match.start()] + img_tag + match.group(0) + html[match.end():]

    # Fallback: right after first <h1> closing tag
    h1_close = re.search(r"</h1\s*>", html, re.IGNORECASE)
    if h1_close:
        idx = h1_close.end()
        return html[:idx] + "\n" + img_tag + html[idx:]

    # Last resort: after <body>
    body_match = re.search(r"<body[^>]*>", html, re.IGNORECASE)
    if body_match:
        idx = body_match.end()
        return html[:idx] + "\n" + img_tag + html[idx:]

    # No anchors to inject after — prepend
    return img_tag + "\n" + html


def inject_image_into_file(filepath: Path, image_filename: str, dry_run: bool = False) -> bool:
    """Read a file, inject an <img>, write atomically. Returns True if changed."""
    original = filepath.read_text()
    modified = inject_image_into_html(original, image_filename)
    if modified == original:
        return False
    if dry_run:
        return True
    tmp = filepath.with_suffix(filepath.suffix + ".tmp")
    tmp.write_text(modified)
    os.replace(tmp, filepath)
    return True


# ── Queue I/O ───────────────────────────────────────────────────────────────

def append_to_queue(queue_file: Path, codes: list[str]) -> int:
    """
    Append product codes to a queue file (one per line). De-dupes against
    existing lines so the queue stays clean across re-runs. Returns the
    number of NEW codes appended.
    """
    queue_file.parent.mkdir(parents=True, exist_ok=True)
    existing: set[str] = set()
    if queue_file.exists():
        existing = {
            ln.strip().upper()
            for ln in queue_file.read_text().splitlines()
            if ln.strip() and not ln.startswith("#")
        }
    new = []
    for c in codes:
        cu = c.upper()
        if cu not in existing:
            existing.add(cu)
            new.append(cu)
    if not new:
        return 0
    with queue_file.open("a") as f:
        for c in new:
            f.write(c + "\n")
    return len(new)


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


def url_to_local_file(url: str, cfg: SiteConfig) -> Path | None:
    """
    Translate a public URL into a local file path under cfg.path. Handles:
      - file:///abs/path
      - file://localhost/abs/path
      - https://domain.tld/...      → <cfg.path>/...
      - already absolute path       → as-is
    """
    parsed = urlparse(url)
    if parsed.scheme == "file":
        p = parsed.path
        return Path(p)
    if parsed.scheme in ("", "file"):
        return Path(url)
    if parsed.scheme in ("http", "https"):
        if not cfg.path:
            return None
        # strip leading slash so we don't end up at site root + abs-path
        rel = parsed.path.lstrip("/")
        return cfg.path / rel
    return None


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