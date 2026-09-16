from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any, cast

import aiosqlite
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from app.audit import AuditLog
from app.config import Settings, load_policy
from app.domain import ApprovalChoice, PendingApproval, RunResult, SessionRecord
from app.events import EventBroker
from app.graph import AgentState, GraphDependencies, build_graph
from app.model import ModelClient, create_model_client
from app.policy import ToolPolicy
from app.retrieval import RepositoryIndex, create_embedding_backend
from app.store import SessionStore
from app.tools import ToolRegistry


class AgentRuntime:
    def __init__(self, settings: Settings, model: ModelClient | None = None) -> None:
        self.settings = settings
        self.policy_config = load_policy(settings)
        self.model = model or create_model_client(settings)
        self.store = SessionStore(settings.database_path)
        self.audit = AuditLog(settings.audit_path)
        self.broker = EventBroker()
        self.repository_index = RepositoryIndex(
            settings.workspace,
            settings,
            create_embedding_backend(settings),
        )
        self.tools = ToolRegistry(settings.workspace, self.repository_index)
        self.policy = ToolPolicy(self.policy_config, settings.workspace)
        self._checkpoint_connection: aiosqlite.Connection | None = None
        self._graph: CompiledStateGraph[AgentState, None, AgentState, AgentState] | None = None
        self._session_locks: dict[str, asyncio.Lock] = {}

    @classmethod
    async def create(cls, settings: Settings, model: ModelClient | None = None) -> AgentRuntime:
        runtime = cls(settings, model)
        settings.workspace.mkdir(parents=True, exist_ok=True)
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        if settings.provider == "demo":
            sample = settings.workspace / "example.txt"
            if not sample.exists():
                sample.write_text("This file is available to the offline Demo Agent.\n", encoding="utf-8")
        await runtime.store.connect()
        await runtime.audit.initialize()
        runtime._checkpoint_connection = await aiosqlite.connect(settings.database_path)
        checkpointer = AsyncSqliteSaver(runtime._checkpoint_connection)
        await checkpointer.setup()
        dependencies = GraphDependencies(
            settings=settings,
            policy_config=runtime.policy_config,
            policy=runtime.policy,
            tools=runtime.tools,
            model=runtime.model,
            audit=runtime.audit,
            emit=runtime.emit,
        )
        runtime._graph = build_graph(dependencies, checkpointer)
        return runtime

    @property
    def graph(self) -> CompiledStateGraph[AgentState, None, AgentState, AgentState]:
        if self._graph is None:
            raise RuntimeError("AgentRuntime is not initialized")
        return self._graph

    async def close(self) -> None:
        if self._checkpoint_connection is not None:
            await self._checkpoint_connection.close()
            self._checkpoint_connection = None
        await self.store.close()

    async def emit(
        self,
        session_id: str,
        run_id: str,
        kind: str,
        payload: dict[str, Any],
    ) -> None:
        event = await self.store.append_event(session_id, run_id, kind, payload)
        await self.broker.publish(event)

    async def create_session(self) -> SessionRecord:
        session_id = str(uuid.uuid4())
        session = await self.store.create_session(session_id, self.model.provider, self.model.model_name)
        await self.emit(
            session_id,
            "",
            "session_created",
            {"provider": self.model.provider, "model": self.model.model_name},
        )
        return session

    async def run(self, session_id: str, prompt: str) -> RunResult:
        if not prompt.strip():
            raise ValueError("Prompt must not be empty")
        async with self._session_locks.setdefault(session_id, asyncio.Lock()):
            session = await self._require_session(session_id)
            if session.status in {"running", "awaiting_approval"}:
                raise RuntimeError(f"Session is currently {session.status}")
            run_id = str(uuid.uuid4())
            await self.store.update_session(session_id, status="running")
            await self.emit(session_id, run_id, "run_started", {"prompt_chars": len(prompt)})
            initial: AgentState = {
                "messages": [HumanMessage(content=prompt)],
                "session_id": session_id,
                "run_id": run_id,
                "tool_calls": 0,
                "failures": 0,
                "signature_counts": {},
                "approved_signatures": [],
            }
            return await self._invoke(session_id, run_id, initial)

    async def resume(self, session_id: str, choice: ApprovalChoice) -> RunResult:
        async with self._session_locks.setdefault(session_id, asyncio.Lock()):
            session = await self._require_session(session_id)
            pending = session.pending_approval
            if session.status != "awaiting_approval" or pending is None:
                raise RuntimeError("Session is not awaiting approval")
            if datetime.fromisoformat(pending.expires_at) < datetime.now(UTC):
                message = "Approval request has expired"
                await self.store.update_session(session_id, status="failed", last_response=message)
                await self.emit(session_id, pending.run_id, "approval_expired", {})
                raise RuntimeError("Approval request has expired")
            await self.store.update_session(session_id, status="running")
            await self.emit(
                session_id,
                pending.run_id,
                "approval_resolved",
                {"approved": choice.approved, "remember_for_run": choice.remember_for_run},
            )
            return await self._invoke(session_id, pending.run_id, Command(resume=choice.model_dump()))

    async def _invoke(
        self,
        session_id: str,
        run_id: str,
        graph_input: AgentState | Command[Any],
    ) -> RunResult:
        config: RunnableConfig = {
            "configurable": {"thread_id": session_id},
            # A run may use the full tool budget plus one Reviewer revision. Keep the graph
            # recursion ceiling above that policy limit so policy, rather than LangGraph's
            # default traversal guard, remains the controlling bound.
            "recursion_limit": max(40, self.policy_config.max_tool_calls_per_run * 3 + 10),
        }
        try:
            raw_result = await self.graph.ainvoke(graph_input, config=config)
            result = cast(dict[str, Any], raw_result)
            interrupts = result.get("__interrupt__", ())
            if interrupts:
                pending = PendingApproval.model_validate(interrupts[0].value)
                await self.store.update_session(
                    session_id,
                    status="awaiting_approval",
                    pending_approval=pending,
                )
                await self.emit(session_id, run_id, "approval_required", pending.model_dump(mode="json"))
                return RunResult(
                    session_id=session_id,
                    run_id=run_id,
                    status="awaiting_approval",
                    pending_approval=pending,
                )
            response = self._last_response(result.get("messages", []))
            await self.store.update_session(session_id, status="completed", last_response=response)
            await self.emit(session_id, run_id, "run_completed", {"response": response})
            return RunResult(session_id=session_id, run_id=run_id, status="completed", response=response)
        except Exception as error:
            await self.store.update_session(session_id, status="failed", last_response=str(error))
            await self.emit(session_id, run_id, "run_failed", {"error": str(error)})
            return RunResult(
                session_id=session_id,
                run_id=run_id,
                status="failed",
                error=f"{type(error).__name__}: {error}",
            )

    async def _require_session(self, session_id: str) -> SessionRecord:
        session = await self.store.get_session(session_id)
        if session is None:
            raise KeyError(f"Session not found: {session_id}")
        return session

    @staticmethod
    def _last_response(messages: list[Any]) -> str:
        for message in reversed(messages):
            if isinstance(message, AIMessage):
                if isinstance(message.content, str):
                    return message.content
                return str(message.content)
        return ""

    async def verify_audit(self) -> int:
        return await self.audit.verify_file()
