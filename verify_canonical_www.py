#!/usr/bin/env python3
"""
verify_canonical_www.py — Check and fix non-www canonical URLs site-wide.

Usage:
    python3 verify_canonical_www.py                # Check current dir
    python3 verify_canonical_www.py --dir /path     # Check specific site
    python3 verify_canonical_www.py --fix           # Auto-fix non-www canonicals
    python3 verify_canonical_www.py --dir /path --fix

Exit code:
    0 = All canonical URLs use www. prefix (or no HTML files found)
    1 = Non-www canonical URLs found (and not fixed)
    2 = Files were fixed

This is designed for pre-commit hooks, cron QA loops, and manual verification.
"""
import os, re, sys

def find_html_files(directory):
    """Find all HTML files excluding backup/node_modules/.git dirs."""
    html_files = []
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs
                   if not d.startswith('.')
                   and d != 'node_modules'
                   and 'backup' not in d
                   and d != 'backups']
        for f in files:
            if f.endswith('.html') or f.endswith('.htm'):
                html_files.append(os.path.join(root, f))
    return sorted(html_files)


def check_file(filepath, do_fix=False):
    """Check a single file for non-www canonical URLs and JSON-LD url fields.

    Returns (problems_found, problems_fixed, file_was_modified).
    """
    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        content = f.read()

    original = content
    problems = []

    # Check canonical link tags
    # Pattern matches <link ... rel="canonical" ... href="https://domain.com/...">
    # where domain does NOT start with www.

    # Pattern 1: rel="canonical" before href
    canon_matches = list(re.finditer(
        r'<link[^>]*rel="canonical"[^>]*href="https://(?!www\.)([^"]+)"',
        content
    ))
    # Pattern 2: href before rel="canonical"
    canon_matches += list(re.finditer(
        r'<link[^>]*href="https://(?!www\.)([^"]+)"[^>]*rel="canonical"',
        content
    ))

    for m in canon_matches:
        bare_domain = m.group(1).split('/')[0]
        problems.append(f"  canonical: https://{m.group(1)} -> https://www.{m.group(1)}")

    # Check JSON-LD url fields with non-www
    jsonld_matches = list(re.finditer(
        r'"url"\s*:\s*"https://(?!www\.)([^"]+)"',
        content
    ))
    for m in jsonld_matches:
        problems.append(f"  JSON-LD url: https://{m.group(1)}")

    # Check for trailing slashes in canonical paths (exclude root /)
    trailing_matches = list(re.finditer(
        r'<link[^>]*rel="canonical"[^>]*href="(https?://[^"]+/)"',
        content
    ))
    for m in trailing_matches:
        href = m.group(1)
        # Exclude bare domain root (https://domain.com/ has exactly 3 slashes)
        if href.count('/') > 3:
            problems.append(f"  trailing slash: {href} → {href[:-1]}")

    if do_fix and problems:
        # Fix canonical links — add www. prefix
        content = re.sub(
            r'(<link[^>]*rel="canonical"[^>]*href="https://)(?!www\.)([^"]+")',
            r'\1www.\2',
            content
        )
        content = re.sub(
            r'(<link[^>]*href="https://)(?!www\.)([^"]+"[^>]*rel="canonical")',
            r'\1www.\2',
            content
        )
        # Fix JSON-LD url fields
        content = re.sub(
            r'("url"\s*:\s*"https://)(?!www\.)([^"]+")',
            r'\1www.\2',
            content
        )
        # Fix trailing slashes in canonicals (preserve root /)
        content = re.sub(
            r'(<link[^>]*rel="canonical"[^>]*href="https://[^"]+)/(?=")',
            r'\1',
            content
        )

    modified = (content != original)
    if modified:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)

    return len(problems), len(problems) if modified else 0, modified


def main():
    directory = '.'
    do_fix = False

    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == '--dir' and i + 1 < len(args):
            directory = args[i + 1]
        elif arg == '--fix':
            do_fix = True

    directory = os.path.abspath(directory)
    if not os.path.isdir(directory):
        print(f"ERROR: Directory not found: {directory}", file=sys.stderr)
        sys.exit(1)

    html_files = find_html_files(directory)
    if not html_files:
        print(f"No HTML files found in {directory}")
        sys.exit(0)

    total_problems = 0
    total_fixed = 0
    total_modified = 0
    any_fixed = False

    for fp in html_files:
        rel_path = os.path.relpath(fp, directory)
        problems, fixed, modified = check_file(fp, do_fix)
        if problems > 0:
            print(f"\n{rel_path} ({problems} issue(s)):")
            # Re-run check to show details
            check_file(fp, do_fix=False)  # just for display
            with open(fp) as f:
                for m in re.finditer(r'(<link[^>]*rel="canonical"[^>]*href="https://(?!www\.)[^"]+")', f.read()):
                    print(f"  {m.group(1)}")
            total_problems += problems
            total_fixed += fixed
            if modified:
                total_modified += 1
                any_fixed = True

    print(f"\n--- Summary ---")
    print(f"Files scanned: {len(html_files)}")
    print(f"Files with issues: {total_modified if do_fix else sum(1 for fp in html_files if check_file(fp)[0] > 0)}")
    print(f"Total issues found: {total_problems}")
    if do_fix:
        print(f"Total issues fixed: {total_fixed}")

    if total_problems > 0 and not do_fix:
        print(f"\nRun with --fix to auto-correct, or fix manually.")
        sys.exit(1)
    elif any_fixed:
        sys.exit(2)
    else:
        sys.exit(0)


if __name__ == '__main__':
    main()
