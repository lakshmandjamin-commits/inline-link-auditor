"""Detector package — all 6 inline-link detectors.

Each module exposes a ``detect(...)`` function that takes (html, filepath, page_url)
and returns a list of ``Violation`` records. The main CLI iterates pages, runs
every detector, and aggregates results into an ``AuditReport``.
"""

from __future__ import annotations

from typing import Callable, List, Tuple

# Populated when detectors are imported (see inline_link_auditor.py main).
DETECTORS: List[Tuple[str, Callable]] = []


def register(name: str, fn: Callable) -> None:
    """Add a detector (idempotent — same name won't double-register)."""
    for existing_name, _ in DETECTORS:
        if existing_name == name:
            return
    DETECTORS.append((name, fn))