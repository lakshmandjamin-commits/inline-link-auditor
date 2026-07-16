"""
audit_runner — thin wrapper around the image-placement audit CLI.

Responsibilities:
    - Validate that the audit tool is installed (path check).
    - Mutate sys.path exactly once to make ``src.cli`` importable.
    - Invoke ``audit_batch`` asynchronously and (optionally) persist the JSON.
    - Provide ``has_r12_violation`` so callers don't need to know about the
      violation-list shape.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Rule ID we treat as "the page is broken without images". Anything that
# doesn't match the audit tool's contract would need updating here.
R12_RULE_ID = "R12"


def has_r12_violation(audit_result: dict) -> bool:
    """True if the page result has at least one R12 violation."""
    for v in audit_result.get("violations", []) or []:
        if v.get("rule_id") == R12_RULE_ID:
            return True
    return False


def _ensure_audit_tool_importable(audit_dir: Path) -> None:
    """
    Make the audit tool's ``src/`` package importable.

    The audit tool lives at ``<audit_dir>/src/`` and uses relative imports,
    so we must add ``<audit_dir>`` (not ``<audit_dir>/src``) to ``sys.path``
    and import via ``from src.cli import audit_batch``.

    This is the only place we mutate ``sys.path`` so the side effect is
    easy to audit.
    """
    audit_src = audit_dir / "src"
    if not audit_src.exists():
        raise FileNotFoundError(
            f"Audit tool not found at {audit_dir}. "
            "Pass --audit-dir to override."
        )
    audit_parent = str(audit_dir)
    if audit_parent not in sys.path:
        sys.path.insert(0, audit_parent)


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
    _ensure_audit_tool_importable(audit_dir)
    from src.cli import audit_batch  # type: ignore[import-not-found]

    results = asyncio.run(
        audit_batch(urls, viewport=viewport, concurrency=concurrency)
    )
    if output_json:
        output_json.parent.mkdir(parents=True, exist_ok=True)
        payload = results[0] if len(results) == 1 else results
        output_json.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    return results
