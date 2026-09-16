from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import socket
import time
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import httpx
import uvicorn
from langchain_core.messages import AIMessage, AnyMessage, ToolMessage
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict

from app.api import create_app
from app.audit import AuditLog
from app.config import Settings
from app.domain import ApprovalChoice
from app.service import AgentRuntime

ROOT = Path(__file__).resolve().parents[1]


class SampleDistribution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    samples: int
    minimum_ms: float
    mean_ms: float
    p50_ms: float
    p95_ms: float
    maximum_ms: float


class RateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    successful: int
    total: int
    rate: float


class BenchmarkReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generated_at: str
    run_directory: str
    environment: dict[str, str | int | None]
    configuration: dict[str, int]
    task_completion: RateResult
    task_latency: SampleDistribution
    tool_call_success: RateResult
    approval_recovery: RateResult
    approval_recovery_latency: SampleDistribution
    sse_delivery: RateResult
    sse_delivery_latency: SampleDistribution
    audit_records: int
    audit_append_throughput_records_per_second: float
    audit_verification_ms: float
    restart_recovery: RateResult
    restart_recovery_latency: SampleDistribution
    passed: bool


class ReadOnceModel:
    provider = "benchmark"
    model_name = "scripted-read-once"

    def __init__(self) -> None:
        self._calls = 0

    async def invoke(
        self,
        messages: Sequence[AnyMessage],
        tools: Sequence[BaseTool],
    ) -> AIMessage:
        del tools
        if isinstance(messages[-1], ToolMessage):
            return AIMessage(content="benchmark task completed")
        self._calls += 1
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "read_file",
                    "args": {"path": "example.txt"},
                    "id": f"benchmark-read-{self._calls}",
                }
            ],
        )


class ApprovalModel:
    provider = "benchmark"
    model_name = "scripted-approval"

    def __init__(self) -> None:
        self._calls = 0

    async def invoke(
        self,
        messages: Sequence[AnyMessage],
        tools: Sequence[BaseTool],
    ) -> AIMessage:
        del tools
        if isinstance(messages[-1], ToolMessage):
            return AIMessage(content="approval decision handled")
        self._calls += 1
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "run_command",
                    "args": {"command": "rm -rf build"},
                    "id": f"benchmark-risk-{self._calls}",
                }
            ],
        )


def rate(successful: int, total: int) -> RateResult:
    return RateResult(successful=successful, total=total, rate=successful / total if total else 0.0)


def distribution(samples: list[float]) -> SampleDistribution:
    if not samples:
        raise ValueError("At least one latency sample is required")
    ordered = sorted(samples)

    def nearest_rank(percentile: float) -> float:
        index = max(0, math.ceil(percentile * len(ordered)) - 1)
        return ordered[index]

    return SampleDistribution(
        samples=len(ordered),
        minimum_ms=round(ordered[0], 3),
        mean_ms=round(sum(ordered) / len(ordered), 3),
        p50_ms=round(nearest_rank(0.50), 3),
        p95_ms=round(nearest_rank(0.95), 3),
        maximum_ms=round(ordered[-1], 3),
    )


def benchmark_settings(root: Path) -> Settings:
    return Settings(
        provider="demo",
        workspace=root / "workspace",
        data_dir=root / "data",
        policy_file=None,
        tool_max_retries=0,
    )


async def benchmark_tasks(root: Path, iterations: int) -> tuple[RateResult, SampleDistribution, RateResult]:
    runtime = await AgentRuntime.create(benchmark_settings(root), ReadOnceModel())
    completed = 0
    tool_successes = 0
    tool_total = 0
    latencies: list[float] = []
    try:
        for _ in range(iterations):
            session = await runtime.create_session()
            started = time.perf_counter()
            result = await runtime.run(session.id, "read the benchmark fixture")
            latencies.append((time.perf_counter() - started) * 1_000)
            events = await runtime.store.list_events(session.id)
            tool_events = [event for event in events if event.kind == "tool_finished"]
            tool_total += len(tool_events)
            tool_successes += sum(event.payload.get("status") == "success" for event in tool_events)
            completed += result.status == "completed" and len(tool_events) == 1
    finally:
        await runtime.close()
    return rate(completed, iterations), distribution(latencies), rate(tool_successes, tool_total)


async def benchmark_approvals(
    root: Path,
    iterations: int,
) -> tuple[RateResult, SampleDistribution]:
    runtime = await AgentRuntime.create(benchmark_settings(root), ApprovalModel())
    recovered = 0
    latencies: list[float] = []
    try:
        for _ in range(iterations):
            session = await runtime.create_session()
            pending = await runtime.run(session.id, "request a destructive operation")
            started = time.perf_counter()
            result = await runtime.resume(session.id, ApprovalChoice(approved=False))
            latencies.append((time.perf_counter() - started) * 1_000)
            recovered += pending.status == "awaiting_approval" and result.status == "completed"
    finally:
        await runtime.close()
    return rate(recovered, iterations), distribution(latencies)


async def benchmark_sse(
    root: Path,
    event_count: int,
) -> tuple[RateResult, SampleDistribution]:
    runtime = await AgentRuntime.create(benchmark_settings(root), ReadOnceModel())
    app = create_app(runtime=runtime)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = int(listener.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False)
    )
    server_task = asyncio.create_task(server.serve(sockets=[listener]))
    for _ in range(500):
        if server.started:
            break
        if server_task.done():
            await server_task
        await asyncio.sleep(0.01)
    else:
        raise TimeoutError("Benchmark HTTP server did not start")

    sent_at: dict[int, int] = {}
    latencies: list[float] = []
    stream_ready = asyncio.Event()
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            timeout=httpx.Timeout(10.0),
        ) as client:
            response = await client.post("/v1/sessions", json={})
            response.raise_for_status()
            session_id = str(response.json()["id"])

            async def consume() -> None:
                async with client.stream(
                    "GET",
                    f"/v1/sessions/{session_id}/events/stream",
                ) as stream:
                    stream.raise_for_status()
                    async for line in stream.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        event = json.loads(line.removeprefix("data: "))
                        if event["kind"] == "session_created":
                            stream_ready.set()
                            continue
                        sequence = event["payload"].get("benchmark_sequence")
                        if isinstance(sequence, int) and sequence in sent_at:
                            latency = (time.perf_counter_ns() - sent_at[sequence]) / 1_000_000
                            latencies.append(latency)
                            if len(latencies) == event_count:
                                return

            consumer = asyncio.create_task(consume())
            await asyncio.wait_for(stream_ready.wait(), timeout=5)
            for sequence in range(event_count):
                sent_at[sequence] = time.perf_counter_ns()
                await runtime.emit(
                    session_id,
                    "benchmark-sse",
                    "benchmark_event",
                    {"benchmark_sequence": sequence},
                )
            await asyncio.wait_for(consumer, timeout=10)
    finally:
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=10)
        listener.close()
        await runtime.close()
    return rate(len(latencies), event_count), distribution(latencies)


async def benchmark_audit(root: Path, record_count: int) -> tuple[float, float, int]:
    audit = AuditLog(root / "audit.jsonl")
    await audit.initialize()
    started = time.perf_counter()
    for index in range(record_count):
        await audit.append(
            session_id="benchmark-session",
            run_id="benchmark-run",
            tool_call_id=f"audit-{index}",
            tool_name="read_file",
            kind="decision",
            outcome="allowed_policy",
            risk="low",
            rule_id="file.read.workspace",
            reason="Benchmark audit append",
            arguments={"path": f"fixture-{index % 16}.txt"},
        )
    elapsed = time.perf_counter() - started
    verify_started = time.perf_counter()
    verified = await audit.verify_file()
    verify_ms = (time.perf_counter() - verify_started) * 1_000
    return round(record_count / elapsed, 2), round(verify_ms, 3), verified


async def benchmark_restart_recovery(
    root: Path,
    iterations: int,
) -> tuple[RateResult, SampleDistribution]:
    recovered = 0
    latencies: list[float] = []
    for index in range(iterations):
        case_root = root / f"case-{index:03d}"
        settings = benchmark_settings(case_root)
        runtime = await AgentRuntime.create(settings, ApprovalModel())
        session = await runtime.create_session()
        pending = await runtime.run(session.id, "persist this approval")
        await runtime.close()

        started = time.perf_counter()
        restored = await AgentRuntime.create(settings, ApprovalModel())
        try:
            persisted = await restored.store.get_session(session.id)
            result = await restored.resume(session.id, ApprovalChoice(approved=False))
            latencies.append((time.perf_counter() - started) * 1_000)
            recovered += (
                pending.status == "awaiting_approval"
                and persisted is not None
                and persisted.status == "awaiting_approval"
                and result.status == "completed"
            )
        finally:
            await restored.close()
    return rate(recovered, iterations), distribution(latencies)


def markdown_report(report: BenchmarkReport) -> str:
    task_detail = (
        f"{report.task_completion.successful}/{report.task_completion.total}; "
        f"p50 {report.task_latency.p50_ms:.3f} ms, p95 {report.task_latency.p95_ms:.3f} ms"
    )
    tool_detail = f"{report.tool_call_success.successful}/{report.tool_call_success.total}"
    approval_detail = (
        f"{report.approval_recovery.successful}/{report.approval_recovery.total}; "
        f"p50 {report.approval_recovery_latency.p50_ms:.3f} ms, "
        f"p95 {report.approval_recovery_latency.p95_ms:.3f} ms"
    )
    sse_detail = (
        f"{report.sse_delivery.successful}/{report.sse_delivery.total}; "
        f"p50 {report.sse_delivery_latency.p50_ms:.3f} ms, "
        f"p95 {report.sse_delivery_latency.p95_ms:.3f} ms"
    )
    audit_detail = f"{report.audit_records} records; verification {report.audit_verification_ms:.3f} ms"
    restart_detail = (
        f"{report.restart_recovery.successful}/{report.restart_recovery.total}; "
        f"p50 {report.restart_recovery_latency.p50_ms:.3f} ms, "
        f"p95 {report.restart_recovery_latency.p95_ms:.3f} ms"
    )
    lines = [
        "# Runtime Performance Benchmark",
        "",
        f"- Generated: `{report.generated_at}`",
        f"- Overall: `{'PASS' if report.passed else 'FAIL'}`",
        f"- Python: `{report.environment['python']}`",
        f"- Platform: `{report.environment['platform']}`",
        f"- Logical CPUs: `{report.environment['logical_cpus']}`",
        "",
        "| Metric | Result | Sample / percentile |",
        "| --- | ---: | --- |",
        f"| Task completion rate | {report.task_completion.rate:.1%} | {task_detail} |",
        f"| Tool call success rate | {report.tool_call_success.rate:.1%} | {tool_detail} |",
        f"| Approval recovery rate | {report.approval_recovery.rate:.1%} | {approval_detail} |",
        f"| SSE delivery rate | {report.sse_delivery.rate:.1%} | {sse_detail} |",
        (
            "| Audit append throughput | "
            f"{report.audit_append_throughput_records_per_second:.2f} records/s | "
            f"{audit_detail} |"
        ),
        f"| Restart recovery rate | {report.restart_recovery.rate:.1%} | {restart_detail} |",
        "",
        "## Measurement scope",
        "",
        (
            "- Runtime tasks use a deterministic scripted model and one successful `read_file` "
            "call per task. This isolates runtime overhead from external model and network latency."
        ),
        (
            "- SSE latency is measured end to end from `AgentRuntime.emit` start, including SQLite "
            "persistence, broker dispatch, HTTP SSE serialization, and receipt by one loopback client."
        ),
        (
            "- Approval latency measures rejection-to-completion recovery through the persisted "
            "LangGraph interrupt path."
        ),
        (
            "- Restart recovery closes and reconstructs `AgentRuntime` against the same SQLite/"
            "checkpoint files; it simulates a service-runtime restart, not an operating-system reboot."
        ),
        (
            "- Audit throughput uses one writer because the hash chain is intentionally serialized. "
            "Results are a local baseline, not a production SLA."
        ),
        (
            "- Real-model behavior is reported separately in `docs/QWEN_ACCEPTANCE_RESULT.md`. "
            "The 44/44 policy evaluation is regression-set conformance, not universal "
            "dangerous-command accuracy."
        ),
        "",
    ]
    return "\n".join(lines)


def prepare_run_directory(output_root: Path, run_id: str) -> Path:
    run_directory = output_root.resolve() / run_id
    run_directory.mkdir(parents=True, exist_ok=False)
    return run_directory


def write_report(report_path: Path, report: BenchmarkReport) -> None:
    report_path.write_text(markdown_report(report), encoding="utf-8")
    report_path.with_name("report.json").write_text(
        report.model_dump_json(indent=2),
        encoding="utf-8",
    )


def display_run_directory(run_directory: Path) -> str:
    try:
        return run_directory.relative_to(ROOT).as_posix()
    except ValueError:
        return run_directory.as_posix()


async def run_benchmark(
    output_root: Path,
    *,
    task_iterations: int,
    approval_iterations: int,
    sse_events: int,
    audit_records: int,
    restart_iterations: int,
) -> Path:
    run_id = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    run_directory = await asyncio.to_thread(prepare_run_directory, output_root, run_id)

    task_result, task_latency, tool_result = await benchmark_tasks(
        run_directory / "task",
        task_iterations,
    )
    approval_result, approval_latency = await benchmark_approvals(
        run_directory / "approval",
        approval_iterations,
    )
    sse_result, sse_latency = await benchmark_sse(run_directory / "sse", sse_events)
    audit_throughput, audit_verification_ms, verified_records = await benchmark_audit(
        run_directory / "audit",
        audit_records,
    )
    restart_result, restart_latency = await benchmark_restart_recovery(
        run_directory / "restart",
        restart_iterations,
    )
    passed = (
        all(
            result.rate == 1.0
            for result in (
                task_result,
                tool_result,
                approval_result,
                sse_result,
                restart_result,
            )
        )
        and verified_records == audit_records
    )
    report = BenchmarkReport(
        generated_at=datetime.now(UTC).isoformat(),
        run_directory=display_run_directory(run_directory),
        environment={
            "python": platform.python_version(),
            "platform": platform.platform(),
            "logical_cpus": os.cpu_count(),
        },
        configuration={
            "task_iterations": task_iterations,
            "approval_iterations": approval_iterations,
            "sse_events": sse_events,
            "audit_records": audit_records,
            "restart_iterations": restart_iterations,
        },
        task_completion=task_result,
        task_latency=task_latency,
        tool_call_success=tool_result,
        approval_recovery=approval_result,
        approval_recovery_latency=approval_latency,
        sse_delivery=sse_result,
        sse_delivery_latency=sse_latency,
        audit_records=verified_records,
        audit_append_throughput_records_per_second=audit_throughput,
        audit_verification_ms=audit_verification_ms,
        restart_recovery=restart_result,
        restart_recovery_latency=restart_latency,
        passed=passed,
    )
    report_path = run_directory / "REPORT.md"
    await asyncio.to_thread(write_report, report_path, report)
    return report_path


def positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark Agent Runtime performance and recovery")
    parser.add_argument("--output-root", type=Path, default=ROOT / "artifacts" / "benchmarks")
    parser.add_argument("--tasks", type=positive_integer, default=30)
    parser.add_argument("--approvals", type=positive_integer, default=20)
    parser.add_argument("--sse-events", type=positive_integer, default=100)
    parser.add_argument("--audit-records", type=positive_integer, default=1_000)
    parser.add_argument("--restarts", type=positive_integer, default=10)
    arguments = parser.parse_args()
    report_path = asyncio.run(
        run_benchmark(
            arguments.output_root,
            task_iterations=arguments.tasks,
            approval_iterations=arguments.approvals,
            sse_events=arguments.sse_events,
            audit_records=arguments.audit_records,
            restart_iterations=arguments.restarts,
        )
    )
    report = BenchmarkReport.model_validate_json(
        report_path.with_name("report.json").read_text(encoding="utf-8")
    )
    print(f"Benchmark report: {report_path}")
    if not report.passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
