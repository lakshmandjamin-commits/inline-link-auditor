#!/usr/bin/env python3
"""
Run validate.py across all sites. Wrapper for cron-friendly output.
Usage: python3 validate_runner.py [all|site_id]
"""
import sqlite3, os, sys, subprocess
from pathlib import Path
from datetime import datetime

DB_DIR = os.path.expanduser("~/.hermes/affiliate-crons/db")
VALIDATE_SCRIPT = os.path.expanduser(
    "~/.hermes/skills/productivity/website-build-learnings/templates/validate.py")

def get_sites(target="all"):
    reg = sqlite3.connect(os.path.join(DB_DIR, "site_registry.db"))
    if target == "all":
        sites = reg.execute("SELECT site_id, local_path FROM sites WHERE status='active'").fetchall()
    else:
        sites = reg.execute("SELECT site_id, local_path FROM sites WHERE site_id=? AND status='active'", (target,)).fetchall()
    reg.close()
    return sites

def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "all"
    print(f"Pre-Deploy Validator — {target} — {datetime.now().isoformat()[:19]}\n")

    sites = get_sites(target)
    results = {"passed": 0, "failed": 0, "errors": 0, "warnings": 0, "details": []}

    for site_id, local_path in sites:
        print(f"--- {site_id} ---")
        if not Path(VALIDATE_SCRIPT).exists():
            print(f"  SKIP: validate.py not found at {VALIDATE_SCRIPT}")
            continue

        r = subprocess.run(["python3", VALIDATE_SCRIPT],
                          cwd=local_path, capture_output=True, text=True, timeout=60)

        # Parse output for error/warning counts
        for line in r.stdout.split("\n"):
            if "errors" in line.lower() and "❌" in line:
                try:
                    count = int(line.split("❌")[1].split()[0])
                    results["errors"] += count
                except Exception as e:
                    print(f"  [WARN] Parse error: {e}", file=sys.stderr)
            if "warnings" in line.lower() and "⚠️" in line:
                try:
                    count = int(line.split("⚠️")[1].split()[0])
                    results["warnings"] += count
                except Exception as e:
                    print(f"  [WARN] Parse error: {e}", file=sys.stderr)

        if r.returncode == 0:
            results["passed"] += 1
            print(f"  PASS")
        else:
            results["failed"] += 1
            # Show first 5 issues
            issues = [l for l in r.stdout.split("\n") if "❌" in l][:5]
            for i in issues:
                stripped = i.strip()
                if stripped:
                    print(f"  {stripped}")
            results["details"].append(f"{site_id}: {len([l for l in r.stdout.split(chr(10)) if '❌' in l])} errors")

        # Update DB
        reg = sqlite3.connect(os.path.join(DB_DIR, "site_registry.db"))
        reg.execute("UPDATE sites SET last_validated_at=CURRENT_TIMESTAMP WHERE site_id=?", (site_id,))
        reg.execute("""INSERT INTO audit_log (site_id, check_type, status, issues_found)
            VALUES (?, 'validate', ?, ?)""",
            (site_id, 'pass' if r.returncode == 0 else 'fail',
             len([l for l in r.stdout.split("\n") if "❌" in l])))
        reg.commit()
        reg.close()

    print(f"\n{'='*50}")
    print(f"Validator complete: {results['passed']} passed, {results['failed']} failed")
    print(f"  Total errors: {results['errors']}, warnings: {results['warnings']}")
    sys.exit(1 if results["failed"] > 0 else 0)

if __name__ == "__main__":
    main()
