# Inline Link Auditor — Agent Handoff

> **Date:** 2026-07-16  
> **Author:** Lakshman (Hermes profile)  
> **Location:** `~/.hermes/affiliate-crons/scripts/inline_link_auditor/`  
> **CLI entry:** `~/.hermes/affiliate-crons/scripts/inline_link_auditor.py`  
> **Tests:** 97 passing  
> **Framework:** Travel Affiliate Inline Linking Framework 2026  
> **Evidence:** W3C WCAG 2.2, Google Search Central, FTC Endorsement Guides, ASA/CAP, GOV.UK

## What This Tool Does

Scans fleet HTML pages for 6 inline-linking rule violations. Pure Python — no browser, no network, works on local files only. Detection only — Gate C handles fixes.

Fleet-agnostic: reads site list from `../config/sites.yaml`. Works for both Saraswati's 5 sites and Hanumanhermes's 8 sites.

## Quick Start

```bash
cd ~/.hermes/affiliate-crons/scripts

# All fleet sites
python3 inline_link_auditor.py all

# Single site (by slug or domain)
python3 inline_link_auditor.py porto-sommelier
python3 inline_link_auditor.py porto-wine-tours.com

# Single page (substring match on file path)
python3 inline_link_auditor.py porto-sommelier --page madeira-levada-walk

# JSON output
python3 inline_link_auditor.py all --json

# Save to file
python3 inline_link_auditor.py all --json --output /tmp/audit.json
```

## Six Detectors

| # | Detector | Rule | What It Catches | Severity | Fleet Impact |
|---|---|---|---|---|---|
| 1 | `specificity` | `vague-anchor` | Empty, <8 chars, generic phrases, no proper noun | major | Likely widespread in prose |
| 2 | `first_mention` | `repeated-link` | Same product linked 2+ times in prose (cards exempt) | major | Rampant on long-form pages |
| 3 | `trust_gate` | `trust-keyword` | Safety/visa/medical/emergency keywords near Viator links | critical | Unknown, needs audit |
| 4 | `disclosure` | `missing-disclosure` / `hyperlink-disclosure` | No disclosure before first affiliate link, or disclosure exists only as hyperlink | critical | Likely violated (footer-only) |
| 5 | `link_chain` | `link-chain` | Adjacent `<a>` tags without separating text | minor | Edge case, low count |
| 6 | `price_adjacency` | `price-nearby` | Currency + digits in/near affiliate link anchor | major | Fleet already avoids this |

## Architecture

```
inline_link_auditor/
├── models.py          (57 lines)  Violation + AuditReport dataclasses
├── parser.py          (74 lines)  BeautifulSoup link extraction + context
├── HANDOFF.md                     This file
├── detectors/
│   ├── __init__.py                Package exports
│   ├── specificity.py             Detector 1: vague anchors
│   ├── first_mention.py           Detector 2: repeated product links
│   ├── trust_gate.py              Detector 3: safety/visa keywords
│   ├── disclosure.py              Detector 4: disclosure position
│   ├── link_chain.py              Detector 5: adjacent link chains
│   └── price_adjacency.py         Detector 6: price near affiliate links
└── tests/
    ├── test_specificity.py        13 tests
    ├── test_first_mention.py      13 tests
    ├── test_trust_gate.py
    ├── test_disclosure.py         8 tests
    ├── test_link_chain.py
    ├── test_price_adjacency.py
    └── test_cli.py               End-to-end CLI tests
```

## Detector Contract

Every detector exports the same function signature:

```python
def detect(html: str, filepath: str, url: str) -> list[Violation]:
    ...
```

The CLI iterates pages, calls each detector, and aggregates results into an `AuditReport`.

## Output Schema

```json
{
  "framework_version": "travel-affiliate-inline-linking-framework-2026",
  "audit_date": "2026-07-16T...",
  "sites": [
    {
      "audit_date": "...",
      "site": "porto-sommelier",
      "summary": {
        "pages_scanned": 69,
        "pages_clean": 12,
        "violations": {
          "specificity": 47,
          "first_mention": 89,
          "trust_gate": 3,
          "disclosure": 5,
          "link_chain": 12,
          "price_adjacency": 2
        }
      },
      "violations": [
        {
          "detector": "specificity",
          "rule": "vague-anchor",
          "url": "https://porto-wine-tours.com/...",
          "file": "/Users/saraswati/sites/porto-sommelier/...",
          "line": 42,
          "severity": "major",
          "anchor_text": "this tour"
        }
      ]
    }
  ]
}
```

## Adding Hanumanhermes Sites

1. Add site entries to `../config/sites.yaml` under the `sites:` key:
   ```yaml
   sites:
     onsen-experiences:
       path: /Users/hanumanhermes/sites/onsen-experiences
       domain: onsenexperiences.com
       fleet: hanumanhermes
   ```
2. Run `python3 inline_link_auditor.py all` — new sites auto-detected.

## Adding a New Detector

1. Create `detectors/my_detector.py` with `detect(html, filepath, url) -> list[Violation]`
2. Import it in `inline_link_auditor.py` line 41
3. Add it to `DETECTOR_ORDER` list at line 44
4. Add it to the `detectors` dict at line 148
5. Write tests in `tests/test_my_detector.py`

## Known Limitations

- Context bleed: trust_gate scans 50 chars before each link, which can pick up keywords from preceding paragraphs
- Price window: 12 chars (intentionally wider than spec's 5 chars)
- No auto-fix: detection only — Gate C handles remediation
- YAML fallback parser skips list values (product lists in sites.yaml)
- `datetime.utcnow()` deprecated in Python 3.12+

## Changelog

- **2026-07-16:** Initial implementation. 6 detectors, 97 tests, ~1,200 source lines. Codex-reviewed (5/6 findings real, 2 fixed). Live test: 11 violations from one dirty page.
