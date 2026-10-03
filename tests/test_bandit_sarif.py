"""A failed security scan must never be published as an empty successful scan."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "bandit_sarif", Path(__file__).parents[1] / "scripts" / "bandit_sarif.py"
)
converter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(converter)


def complete_report():
    return {
        "errors": [],
        "metrics": {"_totals": {"loc": 12}},
        "results": [
            {
                "test_id": "B101",
                "test_name": "assert_used",
                "more_info": "https://bandit.readthedocs.io/en/latest/plugins/b101_assert_used.html",
                "issue_text": "Use of assert detected.",
                "issue_severity": "LOW",
                "filename": "./src/example.py",
                "line_number": 3,
            }
        ],
    }


def test_preserves_every_finding_and_severity():
    report = complete_report()
    for severity in ("MEDIUM", "HIGH"):
        finding = copy.deepcopy(report["results"][0])
        finding["issue_severity"] = severity
        report["results"].append(finding)
    run = converter.convert(report, "1.9.4")["runs"][0]
    assert run["tool"]["driver"]["version"] == "1.9.4"
    assert len(run["tool"]["driver"]["rules"]) == 1
    assert [r["level"] for r in run["results"]] == ["note", "warning", "error"]
    location = run["results"][0]["locations"][0]["physicalLocation"]
    assert location["artifactLocation"]["uri"] == "src/example.py"
    assert location["region"]["startLine"] == 3


@pytest.mark.parametrize("missing", ["errors", "results", "metrics"])
def test_rejects_incomplete_report(missing):
    report = complete_report()
    del report[missing]
    with pytest.raises((TypeError, ValueError)):
        converter.convert(report, "1.9.4")


def test_rejects_scan_errors_even_with_findings():
    report = complete_report()
    report["errors"] = [{"filename": "src/broken.py", "reason": "syntax error"}]
    with pytest.raises(ValueError, match="incomplete"):
        converter.convert(report, "1.9.4")


@pytest.mark.parametrize("path", ["/tmp/example.py", "../example.py", "src/../example.py"])
def test_rejects_paths_outside_repository(path):
    report = complete_report()
    report["results"][0]["filename"] = path
    with pytest.raises((TypeError, ValueError)):
        converter.convert(report, "1.9.4")


def test_accepts_complete_clean_scan():
    report = complete_report()
    report["results"] = []
    assert converter.convert(report, "1.9.4")["runs"][0]["results"] == []


def test_failed_rerun_removes_stale_report(monkeypatch, tmp_path):
    monkeypatch.setattr(converter, "__file__", str(tmp_path / "scripts" / "bandit_sarif.py"))
    monkeypatch.setattr("sys.argv", ["bandit_sarif.py"])
    monkeypatch.setattr(converter, "version", lambda name: "1.9.4")
    (tmp_path / "bandit-results.json").write_text(json.dumps({"errors": ["scan failed"]}))
    output = tmp_path / "bandit-results.sarif"
    output.write_text("stale scan")
    with pytest.raises(ValueError, match="incomplete"):
        converter.main()
    assert not output.exists()


def test_cli_rejects_custom_paths_without_touching_files(monkeypatch, tmp_path):
    output = tmp_path / "untouched.txt"
    output.write_text("keep")
    monkeypatch.setattr("sys.argv", ["bandit_sarif.py", "report.json", str(output)])
    with pytest.raises(SystemExit):
        converter.main()
    assert output.read_text() == "keep"
