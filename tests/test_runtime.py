from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, AnyMessage, ToolMessage
from langchain_core.tools import BaseTool

from app.config import Settings
from app.domain import ApprovalChoice
from app.service import AgentRuntime
from tests.helpers import ScriptedModel


class MultiRoleModel:
    provider = "test"
    model_name = "multi-role-model"

    def __init__(self) -> None:
        self.roles: list[str] = []
        self.reviews = 0

    async def invoke(
        self,
        messages: Sequence[AnyMessage],
        tools: Sequence[BaseTool],
    ) -> AIMessage:
        del messages, tools
        raise AssertionError("multi-role workflow must use invoke_role")

    async def invoke_role(
        self,
        role: str,
        messages: Sequence[AnyMessage],
        tools: Sequence[BaseTool],
        context: str = "",
    ) -> AIMessage:
        del tools
        self.roles.append(role)
        if role == "planner":
            return AIMessage(content="Read the fixture and report evidence.")
        if role == "reviewer":
            self.reviews += 1
            if self.reviews == 1:
                return AIMessage(content="REVISE: mention that verification completed")
            return AIMessage(content="APPROVED: evidence is sufficient")
        if "Reviewer feedback" in context:
            return AIMessage(content="revised answer with verification")
        if isinstance(messages[-1], ToolMessage):
            return AIMessage(content="initial answer")
        return AIMessage(
            content="",
            tool_calls=[{"name": "read_file", "args": {"path": "example.txt"}, "id": "role-read"}],
        )


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        provider="demo",
        workspace=tmp_path / "workspace",
        data_dir=tmp_path / "data",
        policy_file=None,
        tool_max_retries=0,
    )


@pytest.mark.asyncio
async def test_safe_tool_call_completes_and_is_audited(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    model = ScriptedModel([{"name": "read_file", "args": {"path": "example.txt"}, "id": "read-1"}])
    runtime = await AgentRuntime.create(settings, model)
    try:
        session = await runtime.create_session()
        result = await runtime.run(session.id, "read the sample")
        assert result.status == "completed"
        assert result.response is not None and "finished:success" in result.response
        assert await runtime.verify_audit() == 2
        audit_source = settings.audit_path.read_text(encoding="utf-8")
        assert "This file is available" not in audit_source
        events = await runtime.store.list_events(session.id)
        assert {event.kind for event in events} >= {
            "run_started",
            "tool_started",
            "tool_finished",
            "run_completed",
        }
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_approval_survives_runtime_restart(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    calls: list[dict[str, object]] = [
        {"name": "run_command", "args": {"command": "rm -rf build"}, "id": "risk-1"}
    ]
    runtime = await AgentRuntime.create(settings, ScriptedModel(calls))
    session = await runtime.create_session()
    first = await runtime.run(session.id, "dangerous operation")
    assert first.status == "awaiting_approval"
    assert first.pending_approval is not None
    await runtime.close()

    restored = await AgentRuntime.create(settings, ScriptedModel(calls))
    try:
        persisted = await restored.store.get_session(session.id)
        assert persisted is not None and persisted.status == "awaiting_approval"
        completed = await restored.resume(session.id, ApprovalChoice(approved=False))
        assert completed.status == "completed"
        assert completed.response is not None and "error" in completed.response
        assert await restored.verify_audit() == 1
    finally:
        await restored.close()


@pytest.mark.asyncio
async def test_expired_approval_fails_closed(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    calls: list[dict[str, object]] = [
        {"name": "run_command", "args": {"command": "rm -rf build"}, "id": "risk-1"}
    ]
    runtime = await AgentRuntime.create(settings, ScriptedModel(calls))
    try:
        session = await runtime.create_session()
        first = await runtime.run(session.id, "dangerous operation")
        assert first.pending_approval is not None
        expired = first.pending_approval.model_copy(update={"expires_at": "2000-01-01T00:00:00+00:00"})
        await runtime.store.update_session(
            session.id,
            status="awaiting_approval",
            pending_approval=expired,
        )
        with pytest.raises(RuntimeError, match="Approval request has expired"):
            await runtime.resume(session.id, ApprovalChoice(approved=True))
        persisted = await runtime.store.get_session(session.id)
        assert persisted is not None and persisted.status == "failed"
        events = await runtime.store.list_events(session.id)
        assert events[-1].kind == "approval_expired"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_batch_is_preflighted_before_any_side_effect(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    calls: list[dict[str, object]] = [
        {
            "name": "write_file",
            "args": {"path": "should-wait.txt", "content": "safe", "overwrite": False},
            "id": "write-1",
        },
        {"name": "run_command", "args": {"command": "rm -rf build"}, "id": "risk-1"},
    ]
    runtime = await AgentRuntime.create(settings, ScriptedModel(calls))
    try:
        session = await runtime.create_session()
        first = await runtime.run(session.id, "batch")
        assert first.status == "awaiting_approval"
        output = settings.workspace / "should-wait.txt"
        assert not output.exists()

        completed = await runtime.resume(session.id, ApprovalChoice(approved=False))
        assert completed.status == "completed"
        assert output.read_text(encoding="utf-8") == "safe"
        records = await runtime.verify_audit()
        assert records == 3
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_multi_role_workflow_plans_executes_and_reviews(tmp_path: Path) -> None:
    settings = settings_for(tmp_path).model_copy(update={"workflow_mode": "multi_role"})
    model = MultiRoleModel()
    runtime = await AgentRuntime.create(settings, model)
    try:
        session = await runtime.create_session()
        result = await runtime.run(session.id, "inspect the fixture")
        events = await runtime.store.list_events(session.id)

        assert result.status == "completed"
        assert result.response == "revised answer with verification"
        assert model.roles == [
            "planner",
            "executor",
            "executor",
            "reviewer",
            "executor",
            "reviewer",
        ]
        assert {event.kind for event in events} >= {
            "plan_created",
            "review_completed",
            "tool_finished",
        }
        review_events = [event for event in events if event.kind == "review_completed"]
        assert [event.payload["approved"] for event in review_events] == [False, True]
    finally:
        await runtime.close()
