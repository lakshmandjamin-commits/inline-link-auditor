"""Shared pytest config + path bootstrap for the inline_link_auditor package.

Running ``pytest`` from /scripts picks up this conftest automatically.
The conftest inserts /scripts onto sys.path so the package
``inline_link_auditor`` and its ``detectors.*`` submodules resolve.
"""

from __future__ import annotations

import sys
from pathlib import Path

# /scripts
SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))