from pathlib import Path

import pytest

from scripts.benchmark import BenchmarkReport, distribution, run_benchmark


def test_distribution_uses_nearest_rank_percentiles() -> None:
    result = distribution([5.0, 1.0, 4.0, 2.0, 3.0])

    assert result.mean_ms == 3.0
    assert result.p50_ms == 3.0
    assert result.p95_ms == 5.0


@pytest.mark.asyncio
async def test_quick_benchmark_passes(tmp_path: Path) -> None:
    report_path = await run_benchmark(
        tmp_path,
        task_iterations=2,
        approval_iterations=1,
        sse_events=3,
        audit_records=4,
        restart_iterations=1,
    )
    report = BenchmarkReport.model_validate_json(
        report_path.with_name("report.json").read_text(encoding="utf-8")
    )

    assert report.passed is True
    assert report.task_completion.rate == 1.0
    assert report.tool_call_success.rate == 1.0
    assert report.approval_recovery.rate == 1.0
    assert report.sse_delivery.rate == 1.0
    assert report.audit_records == 4
    assert report.restart_recovery.rate == 1.0
