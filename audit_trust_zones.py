#!/usr/bin/env python3
"""
Audit: Detect Viator affiliate links in trust-sensitive zones.
Scans for <a href="...viator.com..."> inside:
  - Hero/intro sections (<header class="hero">, hero-content divs)
  - "Why I Built" / "About" / methodology sections (by heading text)
  - Trust badges / author blocks
  - First <p> in <main> (intro paragraph)

Usage: python3 audit_trust_zones.py [--csv]
Exit 0 = clean, 1 = issues found.
"""
import os, re, sys, sqlite3, csv

DB_DIR = os.path.expanduser("~/.hermes/affiliate-crons/db")

TRUST_ZONE_HEADINGS = [
    r'why\s+i\s+built\s+this',
    r'why\s+i\s+started',
    r'how\s+i\s+test',
    r'my\s+(testing|review)\s+(process|methodology)',
    r'about\s+(me|mia|tiago|sofia|alejandro)',
    r'our\s+story',
    r'meet\s+the\s+author',
]


def get_sites():
    reg = sqlite3.connect(os.path.join(DB_DIR, "site_registry.db"))
    sites = reg.execute(
        "SELECT site_id, local_path FROM sites WHERE status='active'"
    ).fetchall()
    reg.close()
    return sites


def audit_site(site_id, site_path):
    findings = []
    for root, dirs, files in os.walk(site_path):
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

            issues = []

            # 1. Check hero sections
            hero_sections = re.findall(
                r'<(?:header|section|div)[^>]*class="[^"]*hero[^"]*"[^>]*>'
                r'(.*?)'
                r'</(?:header|section|div)>',
                html, re.DOTALL
            )
            for hero in hero_sections:
                if re.search(r'href="[^"]*viator\.com[^"]*"', hero):
                    issues.append('HERO_SECTION')

            # 2. Check author blocks
            author_blocks = re.findall(
                r'<div[^>]*class="[^"]*author(?:-block)?[^"]*"[^>]*>'
                r'(.*?)'
                r'</div>',
                html, re.DOTALL
            )
            for block in author_blocks:
                if re.search(r'href="[^"]*viator\.com[^"]*"', block):
                    issues.append('AUTHOR_BLOCK')

            # 3. Check trust badge areas
            badge_areas = re.findall(
                r'<div[^>]*class="[^"]*trust-badges?[^"]*"[^>]*>'
                r'(.*?)'
                r'</div>',
                html, re.DOTALL
            )
            for area in badge_areas:
                if re.search(r'href="[^"]*viator\.com[^"]*"', area):
                    issues.append('TRUST_BADGES')

            # 4. First <p> in <main> (intro paragraph)
            main_match = re.search(r'<main[^>]*>(.*?)</main>', html, re.DOTALL)
            if main_match:
                main_content = main_match.group(1)
                first_p = re.search(r'<p[^>]*>(.*?)</p>', main_content, re.DOTALL)
                if first_p and re.search(
                    r'href="[^"]*viator\.com[^"]*"', first_p.group(1)
                ):
                    issues.append('FIRST_PARAGRAPH')

            # 5. Trust-zone headings
            for heading_pattern in TRUST_ZONE_HEADINGS:
                h_match = re.search(
                    r'<(h[1-4])[^>]*>\s*' + heading_pattern,
                    html, re.IGNORECASE
                )
                if h_match:
                    # Get the section after this heading until next h2/h3
                    h_start = h_match.start()
                    next_h = re.search(
                        r'<(h[2-3])[^>]*>',
                        html[h_start + len(h_match.group(0)):]
                    )
                    section_end = (h_start + len(h_match.group(0)) +
                                   next_h.start()) if next_h else len(html)
                    section = html[h_start:section_end]
                    if re.search(r'href="[^"]*viator\.com[^"]*"', section):
                        zone_name = re.sub(r'\s+', ' ', h_match.group(0).strip())
                        issues.append(f'TRUST_HEADING({zone_name[:50]})')

            for issue in sorted(set(issues)):
                findings.append({
                    'site': site_id,
                    'file': rel,
                    'zone': issue,
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
        writer = csv.DictWriter(sys.stdout, fieldnames=['site', 'file', 'zone'])
        writer.writeheader()
        for f in all_findings:
            writer.writerow(f)
    else:
        if not all_findings:
            print("✅ All trust zones clean — no Viator links in sensitive areas")
        else:
            print(f"❌ {len(all_findings)} trust-zone violation(s):\n")
            for f in all_findings:
                print(f"  [{f['site']}] {f['file']} — {f['zone']}")

    return 1 if all_findings else 0


if __name__ == '__main__':
    sys.exit(main())
