#!/usr/bin/env python3
"""Convert a complete Bandit JSON report to SARIF, refusing incomplete scans."""

from __future__ import annotations

import argparse
import json
from importlib.metadata import version
from pathlib import Path, PurePosixPath
from urllib.parse import quote


def convert(report: dict, tool_version: str) -> dict:
    """Preserve every finding; never turn a scanner error into a clean report."""
    if not isinstance(report, dict):
        raise TypeError("Bandit report must be an object")
    if not isinstance(report.get("errors"), list) or report["errors"]:
        raise ValueError("Bandit scan is incomplete: errors must be an empty list")
    if not isinstance(report.get("results"), list):
        raise TypeError("Bandit report is missing its results list")
    if not isinstance(report.get("metrics", {}).get("_totals"), dict):
        raise TypeError("Bandit report is missing scan metrics")

    rules: dict[str, dict] = {}
    results = []
    for finding in report["results"]:
        rule_id = finding["test_id"]
        path = PurePosixPath(finding["filename"])
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError("Bandit finding must reference a repository-relative path")
        line = finding["line_number"]
        if not isinstance(line, int) or isinstance(line, bool) or line < 1:
            raise ValueError("Bandit finding must have a positive line number")
        level = {"LOW": "note", "MEDIUM": "warning", "HIGH": "error"}[finding["issue_severity"]]
        if rule_id not in rules:
            rules[rule_id] = {
                "id": rule_id,
                "name": finding["test_name"],
                "shortDescription": {"text": finding["test_name"]},
                "helpUri": finding["more_info"],
            }
        results.append(
            {
                "ruleId": rule_id,
                "message": {"text": finding["issue_text"]},
                "level": level,
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": quote(path.as_posix(), safe="/")},
                            "region": {"startLine": line},
                        }
                    }
                ],
            }
        )
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "Bandit",
                        "version": tool_version,
                        "informationUri": "https://bandit.readthedocs.io/",
                        "rules": list(rules.values()),
                    }
                },
                "results": results,
            }
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    report = root / "bandit-results.json"
    output = root / "bandit-results.sarif"
    # A failed rerun must not leave a previous successful scan ready for upload.
    output.unlink(missing_ok=True)
    sarif = convert(json.loads(report.read_text()), version("bandit"))
    output.write_text(json.dumps(sarif, indent=2) + "\n")


if __name__ == "__main__":
    main()
