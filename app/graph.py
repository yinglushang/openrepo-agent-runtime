from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, ToolMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import START, StateGraph, add_messages
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from app.audit import AuditLog
from app.config import PolicyConfig, Settings
from app.domain import (
    ApprovalChoice,
    PendingApproval,
    PolicyAction,
    PolicyDecision,
    ToolRequest,
)
from app.model import ModelClient
from app.model import invoke_role as invoke_model_role
from app.policy import ToolPolicy, apply_run_limits
from app.tools import ToolRegistry, serialize_tool_result

EventCallback = Callable[[str, str, str, dict[str, Any]], Awaitable[None]]


class AgentState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    session_id: str
    run_id: str
    tool_calls: int
    failures: int
    signature_counts: dict[str, int]
    approved_signatures: list[str]
    plan: str
    review: str
    review_approved: bool
    review_attempts: int


class GraphDependencies:
    def __init__(
        self,
        *,
        settings: Settings,
        policy_config: PolicyConfig,
        policy: ToolPolicy,
        tools: ToolRegistry,
        model: ModelClient,
        audit: AuditLog,
        emit: EventCallback,
    ) -> None:
        self.settings = settings
        self.policy_config = policy_config
        self.policy = policy
        self.tools = tools
        self.model = model
        self.audit = audit
        self.emit = emit


def _last_ai_message(state: AgentState) -> AIMessage:
    message = state["messages"][-1]
    if not isinstance(message, AIMessage):
        raise TypeError("Expected the last graph message to be an AIMessage")
    return message


def _message_content(message: AIMessage) -> str:
    return message.content if isinstance(message.content, str) else str(message.content)


async def _audit_decision(
    deps: GraphDependencies,
    state: AgentState,
    request: ToolRequest,
    decision: PolicyDecision,
    outcome: str,
) -> None:
    await deps.audit.append(
        session_id=state["session_id"],
        run_id=state["run_id"],
        tool_call_id=request.call_id,
        tool_name=request.name,
        kind="decision",
        outcome=outcome,
        risk=decision.risk,
        rule_id=decision.rule_id,
        reason=decision.reason,
        arguments=request.arguments,
    )


def build_graph(
    deps: GraphDependencies,
    checkpointer: BaseCheckpointSaver[Any],
) -> CompiledStateGraph[AgentState, None, AgentState, AgentState]:
    async def planner_node(state: AgentState) -> dict[str, str]:
        await deps.emit(
            state["session_id"],
            state["run_id"],
            "role_started",
            {"role": "planner"},
        )
        response = await invoke_model_role(deps.model, "planner", state["messages"], [])
        plan = _message_content(response)
        await deps.emit(
            state["session_id"],
            state["run_id"],
            "plan_created",
            {"plan": plan},
        )
        return {"plan": plan}

    async def model_node(state: AgentState) -> dict[str, list[AIMessage]]:
        await deps.emit(
            state["session_id"],
            state["run_id"],
            "model_started",
            {"model": deps.model.model_name},
        )
        if deps.settings.workflow_mode == "multi_role":
            await deps.emit(
                state["session_id"],
                state["run_id"],
                "role_started",
                {"role": "executor"},
            )
            context_parts = [f"Execution plan:\n{state.get('plan', '')}"]
            if state.get("review") and not state.get("review_approved", True):
                context_parts.append(f"Reviewer feedback:\n{state['review']}")
            response = await invoke_model_role(
                deps.model,
                "executor",
                state["messages"],
                deps.tools.definitions(),
                "\n\n".join(context_parts),
            )
        else:
            response = await deps.model.invoke(state["messages"], deps.tools.definitions())
        if response.content:
            await deps.emit(
                state["session_id"],
                state["run_id"],
                "model_output",
                {"content": response.content},
            )
        return {"messages": [response]}

    async def reviewer_node(state: AgentState) -> dict[str, Any]:
        await deps.emit(
            state["session_id"],
            state["run_id"],
            "role_started",
            {"role": "reviewer"},
        )
        candidate = _message_content(_last_ai_message(state))
        context = f"Execution plan:\n{state.get('plan', '')}\n\nCandidate answer:\n{candidate}"
        response = await invoke_model_role(
            deps.model,
            "reviewer",
            state["messages"],
            [],
            context,
        )
        review = _message_content(response)
        approved = not review.lstrip().upper().startswith("REVISE:")
        attempts = state.get("review_attempts", 0) + 1
        await deps.emit(
            state["session_id"],
            state["run_id"],
            "review_completed",
            {"approved": approved, "attempt": attempts, "review": review},
        )
        return {
            "review": review,
            "review_approved": approved,
            "review_attempts": attempts,
        }

    def route_after_model(state: AgentState) -> Literal["tools", "reviewer", "__end__"]:
        if _last_ai_message(state).tool_calls:
            return "tools"
        return "reviewer" if deps.settings.workflow_mode == "multi_role" else "__end__"

    def route_after_review(state: AgentState) -> Literal["model", "__end__"]:
        if state.get("review_approved", True) or state.get("review_attempts", 0) >= 2:
            return "__end__"
        return "model"

    async def tools_node(state: AgentState) -> dict[str, Any]:
        ai_message = _last_ai_message(state)
        tool_calls = state.get("tool_calls", 0)
        failures = state.get("failures", 0)
        signature_counts = dict(state.get("signature_counts", {}))
        approved_signatures = set(state.get("approved_signatures", []))
        planned: list[tuple[ToolRequest, PolicyDecision, str]] = []

        for raw_call in ai_message.tool_calls:
            request = ToolRequest(
                name=raw_call["name"],
                arguments=dict(raw_call["args"]),
                call_id=raw_call["id"],
            )
            tool_calls += 1
            decision = deps.policy.evaluate(request)
            repeated_calls = signature_counts.get(decision.signature, 0) + 1
            signature_counts[decision.signature] = repeated_calls
            decision = apply_run_limits(
                decision,
                tool_calls=tool_calls,
                failures=failures,
                repeated_calls=repeated_calls,
                config=deps.policy_config,
            )
            outcome = "allowed_policy"
            if decision.action == PolicyAction.DENY:
                outcome = "denied_policy"
            elif decision.action == PolicyAction.CONFIRM and decision.signature not in approved_signatures:
                expires_at = datetime.now(UTC) + timedelta(
                    seconds=deps.policy_config.approval_timeout_seconds
                )
                pending = PendingApproval(
                    session_id=state["session_id"],
                    run_id=state["run_id"],
                    tool_call_id=request.call_id,
                    tool_name=request.name,
                    arguments=request.arguments,
                    risk=decision.risk,
                    rule_id=decision.rule_id,
                    reason=decision.reason,
                    expires_at=expires_at.isoformat(),
                )
                choice = ApprovalChoice.model_validate(interrupt(pending.model_dump(mode="json")))
                if not choice.approved:
                    decision = decision.model_copy(update={"action": PolicyAction.DENY})
                    outcome = "denied_user"
                elif choice.remember_for_run:
                    approved_signatures.add(decision.signature)
                    outcome = "approved_for_run"
                else:
                    outcome = "approved_once"
            elif decision.action == PolicyAction.CONFIRM:
                outcome = "approved_from_run_cache"
            planned.append((request, decision, outcome))

        messages: list[ToolMessage] = []
        for request, decision, outcome in planned:
            await _audit_decision(deps, state, request, decision, outcome)
            if decision.action == PolicyAction.DENY:
                await deps.emit(
                    state["session_id"],
                    state["run_id"],
                    "tool_blocked",
                    {"tool": request.name, "call_id": request.call_id, "rule_id": decision.rule_id},
                )
                messages.append(
                    ToolMessage(
                        content=f"Blocked by runtime policy: {decision.reason}",
                        tool_call_id=request.call_id,
                        name=request.name,
                        status="error",
                    )
                )
                continue

            await deps.emit(
                state["session_id"],
                state["run_id"],
                "tool_started",
                {"tool": request.name, "call_id": request.call_id, "risk": decision.risk},
            )
            result_text = ""
            error: Exception | None = None
            for attempt in range(deps.settings.tool_max_retries + 1):
                try:
                    value = await asyncio.wait_for(
                        deps.tools.invoke(request.name, request.arguments),
                        timeout=deps.settings.tool_timeout_seconds,
                    )
                    result_text = serialize_tool_result(value)
                    error = None
                    break
                except Exception as caught:
                    error = caught
                    if attempt < deps.settings.tool_max_retries:
                        await deps.emit(
                            state["session_id"],
                            state["run_id"],
                            "tool_retry",
                            {"tool": request.name, "call_id": request.call_id, "attempt": attempt + 2},
                        )
                        await asyncio.sleep(min(0.25 * (2**attempt), 2.0))

            if error is not None:
                failures += 1
                result_text = f"{type(error).__name__}: {error}"
                status: Literal["success", "error"] = "error"
                result_outcome = "result_error"
            else:
                status = "success"
                result_outcome = "result_ok"
            await deps.audit.append(
                session_id=state["session_id"],
                run_id=state["run_id"],
                tool_call_id=request.call_id,
                tool_name=request.name,
                kind="result",
                outcome=result_outcome,
                risk=decision.risk,
                rule_id=decision.rule_id,
                reason=(
                    "Tool completed successfully"
                    if error is None
                    else f"Tool failed with {type(error).__name__}"
                ),
                arguments=request.arguments,
            )
            await deps.emit(
                state["session_id"],
                state["run_id"],
                "tool_finished",
                {
                    "tool": request.name,
                    "call_id": request.call_id,
                    "status": status,
                    "result_chars": len(result_text),
                },
            )
            messages.append(
                ToolMessage(
                    content=result_text,
                    tool_call_id=request.call_id,
                    name=request.name,
                    status=status,
                )
            )

        return {
            "messages": messages,
            "tool_calls": tool_calls,
            "failures": failures,
            "signature_counts": signature_counts,
            "approved_signatures": sorted(approved_signatures),
        }

    builder = StateGraph(AgentState)
    builder.add_node("planner", planner_node)
    builder.add_node("model", model_node)
    builder.add_node("tools", tools_node)
    builder.add_node("reviewer", reviewer_node)
    builder.add_edge(START, "planner" if deps.settings.workflow_mode == "multi_role" else "model")
    builder.add_edge("planner", "model")
    builder.add_conditional_edges("model", route_after_model)
    builder.add_edge("tools", "model")
    builder.add_conditional_edges("reviewer", route_after_review)
    return builder.compile(checkpointer=checkpointer, name="openrepo-agent-runtime")
