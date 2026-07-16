#!/usr/bin/env python3
"""
Audit: Detect Viator affiliate links inside hero sections across fleet sites.
Flags any <a href="...viator.com..."> inside <header class="hero"> or <section class="hero">.

Usage: python3 audit_hero_links.py [--csv]
Output: site, file, line_number, product_code, anchor_text
Exit 0 = clean, 1 = issues found.
"""
import os, re, sys, sqlite3, csv, io

DB_DIR = os.path.expanduser("~/.hermes/affiliate-crons/db")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def get_sites():
    """Get active site paths from registry."""
    reg = sqlite3.connect(os.path.join(DB_DIR, "site_registry.db"))
    sites = reg.execute(
        "SELECT site_id, local_path FROM sites WHERE status='active'"
    ).fetchall()
    reg.close()
    return sites


def audit_site(site_id, site_path):
    """Scan all HTML files in a site for hero Viator links."""
    findings = []
    for root, dirs, files in os.walk(site_path):
        # Skip backup, css, images dirs
        SKIP_DIRS = {'backup', 'backup_pre_fixes', 'backup_fresh',
                     'css', 'images', '.git', 'node_modules'}
        dirs[:] = [d for d in dirs
                   if d not in SKIP_DIRS
                   and not d.startswith('.')
                   and not d.startswith('backup_nav_')]
        for fn in files:
            if not fn.endswith('.html'):
                continue
            fpath = os.path.join(root, fn)
            rel = os.path.relpath(fpath, site_path)
            try:
                with open(fpath) as f:
                    html = f.read()
            except Exception:
                continue

            # Find hero sections: <header/section/div class="hero">
            for hero_match in re.finditer(
                r'<(header|section|div)[^>]*class="[^"]*hero[^"]*"[^>]*>'
                r'(.*?)'
                r'</(header|section|div)>',
                html, re.DOTALL
            ):
                hero_content = hero_match.group(2)
                hero_start = hero_match.start()

                # Find Viator links inside hero
                for link_match in re.finditer(
                    r'<a[^>]*href="(https?://(?:www\.)?viator\.com/[^"]*)"[^>]*>'
                    r'(.*?)'
                    r'</a>',
                    hero_content, re.DOTALL
                ):
                    url = link_match.group(1)
                    anchor = re.sub(r'<[^>]+>', '', link_match.group(2)).strip()

                    # Extract product code from URL
                    code_match = re.search(r'/d\d+-(\w+)', url)
                    product_code = code_match.group(1) if code_match else 'unknown'

                    # Calculate line number
                    line_num = html[:hero_start + link_match.start()].count('\n') + 1

                    findings.append({
                        'site': site_id,
                        'file': rel,
                        'line': line_num,
                        'product_code': product_code,
                        'anchor_text': anchor[:80],
                        'url': url[:200],
                    })

    return findings


def main():
    csv_mode = '--csv' in sys.argv

    sites = get_sites()
    all_findings = []

    for site_id, site_path in sites:
        findings = audit_site(site_id, site_path)
        all_findings.extend(findings)

    if csv_mode:
        writer = csv.DictWriter(
            sys.stdout,
            fieldnames=['site', 'file', 'line', 'product_code', 'anchor_text', 'url']
        )
        writer.writeheader()
        for f in all_findings:
            writer.writerow(f)
    else:
        if not all_findings:
            print("✅ All sites clean — no Viator links in hero sections")
        else:
            print(f"❌ {len(all_findings)} hero inline link(s) found:\n")
            for f in all_findings:
                print(f"  [{f['site']}] {f['file']}:{f['line']}")
                print(f"       product={f['product_code']}  anchor=\"{f['anchor_text']}\"")
                print(f"       url={f['url']}")
                print()

    return 1 if all_findings else 0


if __name__ == '__main__':
    sys.exit(main())
