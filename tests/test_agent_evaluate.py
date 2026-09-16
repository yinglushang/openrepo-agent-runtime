from pathlib import Path

from app.domain import EventRecord
from scripts.agent_evaluate import (
    AgentEvalCase,
    ToolObservation,
    load_cases,
    task_passes,
    validate_case,
)


def test_agent_eval_cases_have_unique_ids_and_cover_runtime_tools(tmp_path: Path) -> None:
    source = tmp_path / "cases.jsonl"
    source.write_text(
        '{"id":"one","category":"selection","prompt":"list","expected_tool":"list_files",'
        '"validator":"tool_success"}\n'
        '{"id":"two","category":"selection","prompt":"read","expected_tool":"read_file",'
        '"validator":"tool_success"}\n',
        encoding="utf-8",
    )

    cases = load_cases(source)

    assert [case.id for case in cases] == ["one", "two"]
    assert all(isinstance(case, AgentEvalCase) for case in cases)


def test_task_completion_is_independent_from_first_tool_accuracy() -> None:
    assert task_passes(
        status="completed",
        validator_passed=True,
        planner_observed=True,
        reviewer_observed=True,
        reviewer_approved=True,
    )


async def test_no_match_validator_uses_actual_tool_message(tmp_path: Path) -> None:
    case = AgentEvalCase(
        id="missing-symbol",
        category="negative-retrieval",
        prompt="search",
        expected_tool="search_text",
        validator="no_matches",
    )

    result = await validate_case(
        case,
        workspace=tmp_path,
        events=[],
        observations=[ToolObservation(name="search_text", status="success", content="[]")],
        observed_tools=["search_text"],
        approval_observed=False,
    )

    assert result.passed


async def test_policy_validator_checks_rule_and_preserved_file(tmp_path: Path) -> None:
    protected = tmp_path / "build" / "keep.txt"
    protected.parent.mkdir()
    protected.write_text("keep", encoding="utf-8")
    case = AgentEvalCase(
        id="dangerous-delete",
        category="dangerous-operation",
        prompt="delete",
        expected_tool="run_command",
        validator="dangerous_contained",
        validator_args={
            "rule_id": "shell.destructive_files",
            "preserve_paths": ["build/keep.txt"],
        },
    )
    event = EventRecord(
        id=1,
        session_id="session",
        run_id="run",
        kind="tool_blocked",
        payload={"tool": "run_command", "rule_id": "shell.destructive_files"},
        created_at="2026-09-16T00:00:00+00:00",
    )

    result = await validate_case(
        case,
        workspace=tmp_path,
        events=[event],
        observations=[],
        observed_tools=["run_command"],
        approval_observed=True,
    )

    assert result.passed


async def test_missing_path_validator_accepts_evidence_based_report(tmp_path: Path) -> None:
    case = AgentEvalCase(
        id="missing-file",
        category="failure-handling",
        prompt="check missing file",
        expected_tool="read_file",
        validator="missing_path_reported",
        validator_args={"path": "docs/missing.md"},
    )

    result = await validate_case(
        case,
        workspace=tmp_path,
        events=[],
        observations=[],
        observed_tools=["list_files"],
        approval_observed=False,
        response="文件不存在。",
    )

    assert result.passed
