from pathlib import Path

import pytest

from scripts.qwen_acceptance import AcceptanceReport, report_exit_code


@pytest.mark.parametrize(("passed", "expected"), [(True, 0), (False, 1)])
def test_report_exit_code(tmp_path: Path, passed: bool, expected: int) -> None:
    report = AcceptanceReport(
        generated_at="2026-09-16T00:00:00+00:00",
        provider="qwen",
        model="qwen-plus",
        run_directory="artifacts/qwen-runs/example",
        passed=passed,
        scenarios=[],
    )
    report_path = tmp_path / "REPORT.md"
    report_path.write_text("# Acceptance report\n", encoding="utf-8")
    report_path.with_name("report.json").write_text(
        report.model_dump_json(), encoding="utf-8"
    )

    assert report_exit_code(report_path) == expected
