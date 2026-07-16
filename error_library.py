#!/usr/bin/env python3
"""
error_library.py — Pipeline Error Library (Loop 2)

Loads known failure patterns from error_library.json.
Before generation: injects fix context into generation prompts.
After generation: detects and auto-fixes known patterns.
Runs BEFORE Loop 1 (quality gate) — deterministic fixes first.

Usage:
  from error_library import ErrorLibrary
  lib = ErrorLibrary(domain="porto-sommelier.com")
  context = lib.get_pre_generation_context()  # inject into LLM prompt
  lib.detect_and_fix(html, page_path)          # post-generation check
"""

import json
import os
import re
import subprocess
from pathlib import Path


DATA_DIR = Path(__file__).resolve().parent.parent / "data"
LIBRARY_PATH = DATA_DIR / "error_library.json"


class ErrorLibrary:
    def __init__(self, domain=None):
        self.domain = domain
        self.patterns = self._load()

    def _load(self):
        if not LIBRARY_PATH.exists():
            return []
        with open(LIBRARY_PATH) as f:
            data = json.load(f)
        return data.get("patterns", [])

    def _save(self):
        data = {"patterns": self.patterns, "version": 1}
        LIBRARY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LIBRARY_PATH, "w") as f:
            json.dump(data, f, indent=2, default=str)

    def get_pre_generation_context(self):
        """Return feedback string to inject into LLM prompt for known failure patterns."""
        contexts = []
        for p in self.patterns:
            if p.get("requires_regeneration"):
                contexts.append(f"[ERROR_LIBRARY] Known failure '{p['id']}': {p['root_cause']}. "
                               f"Preventive fix: {p['fix_detail']}")
        return "\n".join(contexts) if contexts else ""

    def detect_and_fix(self, html, filepath):
        """Run detection on generated HTML. Apply fixes for matches. Return (fixed_html, fixes_applied)."""
        fixed_html = html
        fixes = []

        for p in self.patterns:
            if not p.get("auto_fixable"):
                continue

            pid = p["id"]
            detection = p.get("detection", {})

            # Run detection
            if detection.get("command") == "grep":
                args = detection.get("args", [])
                threshold = detection.get("threshold", 0)
                operator = detection.get("operator", "gt")

                # Run grep against the HTML content as a string
                pattern = args[0] if args else ""
                matches = len(re.findall(pattern, fixed_html))

                hit = False
                if operator == "gt" and matches > threshold:
                    hit = True
                elif operator == "eq" and matches == threshold:
                    hit = True
                elif operator == "gte" and matches >= threshold:
                    hit = True

                if not hit:
                    continue

            elif detection.get("command") == "grep_domain_mismatch":
                # Check: does canonical or JSON-LD url contain a domain that's NOT self.domain?
                if not self.domain:
                    continue
                canonicals = re.findall(
                    r'(?:<link[^>]*rel="canonical"[^>]*href="|"url":\s*")(https?://[^"]+)',
                    fixed_html
                )
                mismatches = [u for u in canonicals if self.domain not in u]
                if not mismatches:
                    continue
            else:
                continue

            # Apply fix
            fix_type = p.get("fix")
            if fix_type == "sed_replace" and self.domain:
                scope = p.get("scope", "")
                # Scope to canonical and JSON-LD url only
                if "canonical_href" in scope:
                    fixed_html = re.sub(
                        r'(<link[^>]*rel="canonical"[^>]*href=")https?://[^"]*(")',
                        rf'\1https://www.{self.domain}\2',
                        fixed_html
                    )
                if "jsonld_url" in scope:
                    fixed_html = re.sub(
                        r'("url":\s*")https?://[^"]*(")',
                        rf'\1https://www.{self.domain}\2',
                        fixed_html
                    )
                fixes.append(pid)
                p["occurrences"] = p.get("occurrences", 0) + 1
                p["last_seen"] = "2026-07-03"

            elif fix_type == "sed_canonical_replace" and self.domain:
                fixed_html = re.sub(
                    r'(<link[^>]*rel="canonical"[^>]*href=")https?://[^"]*(")',
                    rf'\1https://www.{self.domain}\2',
                    fixed_html
                )
                fixed_html = re.sub(
                    r'("url":\s*")https?://[^"]*(")',
                    rf'\1https://www.{self.domain}\2',
                    fixed_html
                )
                fixes.append(pid)
                p["occurrences"] = p.get("occurrences", 0) + 1
                p["last_seen"] = "2026-07-03"

            elif fix_type == "regenerate_with_feedback":
                # Can't fix post-hoc — requires regeneration. Report to caller.
                fixes.append(f"{pid} (needs regeneration)")

        if fixes:
            self._save()

        return fixed_html, fixes


def main():
    """CLI: test error library against a file."""
    import sys
    if len(sys.argv) < 2:
        print("Usage: error_library.py <html_file> [--domain example.com]")
        sys.exit(1)

    filepath = sys.argv[1]
    domain = None
    if "--domain" in sys.argv:
        idx = sys.argv.index("--domain")
        domain = sys.argv[idx + 1]

    with open(filepath) as f:
        html = f.read()

    lib = ErrorLibrary(domain=domain)
    context = lib.get_pre_generation_context()
    if context:
        print(f"Pre-generation context:\n{context}\n")

    fixed_html, fixes = lib.detect_and_fix(html, filepath)
    if fixes:
        print(f"Fixes applied: {fixes}")
        # Write back if fixes applied
        with open(filepath, "w") as f:
            f.write(fixed_html)
        print(f"Written: {filepath}")
    else:
        print("No fixes needed.")


if __name__ == "__main__":
    main()
