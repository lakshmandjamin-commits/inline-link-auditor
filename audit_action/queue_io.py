"""
queue_io — append-only queue files for product-code fetch jobs.

The queue format is one product code per line. Existing lines are de-duped
across re-runs so we never re-queue a code we've already asked the fetcher
about.
"""
from __future__ import annotations

from pathlib import Path


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
