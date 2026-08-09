#!/usr/bin/env python3
"""Enforce per-module coverage gates from coverage.json (QA review M1 gate).

Gates: config_flow 100% statements, api/ >= 95%, every other module >= 90%.
Run after: pytest --cov-report=json
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

GATES = (
    ("custom_components/bonnvoyage/config_flow.py", 100.0),
    ("custom_components/bonnvoyage/api/", 95.0),
    ("custom_components/bonnvoyage/", 90.0),
)


def gate_for(filename: str) -> float:
    for prefix, minimum in GATES:
        if filename == prefix or (prefix.endswith("/") and filename.startswith(prefix)):
            return minimum
    return 0.0


def main() -> int:
    report = json.loads(Path("coverage.json").read_text())
    failed = False
    for filename, data in sorted(report["files"].items()):
        summary = data["summary"]
        covered = 100.0 * (
            1 - summary["missing_lines"] / max(summary["num_statements"], 1)
        )
        minimum = gate_for(filename)
        status = "OK " if covered >= minimum else "FAIL"
        if covered < minimum:
            failed = True
        print(f"{status} {filename}: {covered:.1f}% (gate {minimum:.0f}%)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
