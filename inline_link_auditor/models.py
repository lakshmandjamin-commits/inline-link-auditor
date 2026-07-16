"""Shared data models for the inline link auditor."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any

FRAMEWORK_VERSION = "travel-affiliate-inline-linking-framework-2026"


@dataclass
class Violation:
    """A single inline-link rule violation."""

    detector: str          # specificity | first_mention | trust_gate | disclosure | link_chain | price_adjacency
    rule: str              # vague-anchor | repeated-link | trust-keyword | missing-disclosure | link-chain | price-nearby
    url: str               # page URL
    file: str              # filesystem path
    line: int              # line number (or 0 if unparseable)
    severity: str          # critical | major | minor
    anchor_text: str = ""
    product_code: str = ""
    matched_keyword: str = ""
    context_snippet: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Drop empty fields for cleaner JSON
        return {k: v for k, v in d.items() if v or k in ("line", "severity")}


@dataclass
class AuditReport:
    """Top-level report for one site."""

    audit_date: str
    framework_version: str
    site: str
    pages_scanned: int
    pages_clean: int
    violations: dict[str, int]   # detector → count
    details: list[Violation] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "audit_date": self.audit_date,
            "framework_version": self.framework_version,
            "site": self.site,
            "summary": {
                "pages_scanned": self.pages_scanned,
                "pages_clean": self.pages_clean,
                "violations": self.violations,
            },
            "violations": [v.to_dict() for v in self.details],
        }
