#!/usr/bin/env python3
"""Collect fix logs from flywheel cron outputs. Idempotent, dedup via md5 hashing."""
import json, re, hashlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SGT = timezone(timedelta(hours=8))
CRON_OUTPUT_DIR = Path.home() / ".hermes/cron/output"
FIX_LOGS_DIR = Path.home() / ".hermes/affiliate-crons/data/fix_logs"
RETENTION_DAYS = 90
FLEET_SITES_SORTED = sorted([
    "madeira-trail-guide", "porto-sommelier", "tenerife-outdoor-guide",
    "lapland-adventure-guide", "san-juan-excursions", "yogyakarta-temple-tours"
])

# Each pattern: (bug_class, regex, cron_filter)
BUG_PATTERNS = [
    ("WRONG_DOMAIN",        r"\[FIXED\]\s+(\S+)",          "sitemap-check-daily"),
    ("CROSS_DOMAIN_SITEMAP", r"sitemap.*<loc>.*->",        "sitemap-check-daily"),
    ("ANTI_WORD",           r"replaced\s+\S+\s*->",        "anti-word-flywheel"),
    ("BROKEN_LINK",         r"BROKEN:.*?->.*?FIXED",         "link-health-flywheel"),
    ("WRONG_PRODUCT_CODE",  r"NOT_FOUND.*?->",              "product-code-flywheel"),
    ("UNCLOSED_TAG",        r"unclosed tag|missing closing", "structure-integrity-weekly"),
    ("FAQ_TRUNCATION",      r"FAQ.*truncat|incomplete.*JSON-LD", "structure-integrity-weekly"),
    ("MISSING_CANONICAL",   r"missing.*canonical|no canonical",  "structure-integrity-weekly"),
    ("PRODUCT_COUNT_BADGE", r"product count.*mismatch|badge.*\d+.*actual.*\d+", "hanuman_checks"),
]


def cron_matches(cron_dir_name, cron_filter):
    """Check if a cron directory name matches the filter string."""
    if "|" in cron_filter:
        return cron_dir_name in cron_filter.split("|")
    return cron_dir_name == cron_filter


def extract_site(match_text, known_sites_sorted):
    """Try to find a known site slug in the matched text. Returns first match in sorted order (deterministic)."""
    for site in known_sites_sorted:
        if site in match_text.lower():
            return site
    return "unknown"


def dedup_key(fix):
    """Deterministic dedup key: md5 of cron+bug_class+file+match_start_offset.
    Includes file path and match position so distinct fixes on different pages
    or at different offsets never collide. Survives cron retries."""
    raw = f"{fix['cron']}|{fix['bug_class']}|{fix['file']}|{fix['offset']}"
    return hashlib.md5(raw.encode()).hexdigest()


def parse_cron_outputs(target_date):
    """Parse today's cron output files for fix patterns."""
    fixes = []
    date_str = target_date.isoformat()

    for job_dir in sorted(CRON_OUTPUT_DIR.iterdir()):
        if not job_dir.is_dir():
            continue
        for md_file in sorted(job_dir.glob(f"{date_str}*.md")):
            try:
                content = md_file.read_text()
            except (UnicodeDecodeError, OSError):
                continue
            file_ts = datetime.fromtimestamp(md_file.stat().st_mtime, tz=SGT).isoformat()

            for bug_class, pattern, cron_filter in BUG_PATTERNS:
                if not cron_matches(job_dir.name, cron_filter):
                    continue
                for match in re.finditer(pattern, content, re.IGNORECASE):
                    match_text = match.group(0)[:300]
                    site = extract_site(match_text, FLEET_SITES_SORTED)

                    fixes.append({
                        "timestamp": file_ts,
                        "cron": job_dir.name,
                        "bug_class": bug_class,
                        "site": site,
                        "match": match_text,
                        "file": str(md_file),
                        "offset": match.start(),
                    })
    return fixes


def main():
    now = datetime.now(SGT)
    today = now.date()
    FIX_LOGS_DIR.mkdir(parents=True, exist_ok=True)

    fixes = parse_cron_outputs(today)

    # Always run cleanup regardless of whether fixes were found
    cutoff = today - timedelta(days=RETENTION_DAYS)
    for old_log in FIX_LOGS_DIR.glob("*.jsonl"):
        try:
            log_date = date.fromisoformat(old_log.stem)
            if log_date < cutoff:
                old_log.unlink()
        except (ValueError, OSError):
            pass

    if not fixes:
        return  # Silent — nothing new to collect

    log_file = FIX_LOGS_DIR / f"{today.isoformat()}.jsonl"

    # Load existing dedup keys from today's file (cross-run dedup via md5)
    seen_keys = set()
    if log_file.exists():
        for line in log_file.read_text().strip().split("\n"):
            if not line:
                continue
            try:
                existing = json.loads(line)
                seen_keys.add(dedup_key(existing))
            except (json.JSONDecodeError, KeyError):
                pass

    new_count = 0
    with open(log_file, "a") as f:
        for fix in fixes:
            key = dedup_key(fix)
            if key not in seen_keys:
                f.write(json.dumps(fix) + "\n")
                seen_keys.add(key)
                new_count += 1

    if new_count:
        print(f"Collected {new_count} new fixes ({len(fixes)} total, {len(fixes) - new_count} duplicates skipped)")


if __name__ == "__main__":
    main()
