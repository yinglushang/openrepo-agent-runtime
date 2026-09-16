from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, cast

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings
from app.domain import ApprovalChoice, EventRecord, RunResult, SessionRecord
from app.service import AgentRuntime

STATIC_DIR = Path(__file__).parent / "static"


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=20_000)


class AuditVerification(BaseModel):
    valid: bool
    records: int


def get_runtime(request: Request) -> AgentRuntime:
    return cast(AgentRuntime, request.app.state.runtime)


RuntimeDependency = Annotated[AgentRuntime, Depends(get_runtime)]


def create_app(
    runtime: AgentRuntime | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        active_runtime = runtime or await AgentRuntime.create(settings or Settings())
        app.state.runtime = active_runtime
        try:
            yield
        finally:
            if runtime is None:
                await active_runtime.close()

    app = FastAPI(
        title="OpenRepo Agent Runtime",
        version="0.1.0",
        description="LangGraph Agent runtime with durable approvals and tamper-evident audit logs.",
        lifespan=lifespan,
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "index.html",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/health")
    async def health(active: RuntimeDependency) -> dict[str, str]:
        return {
            "status": "ok",
            "provider": active.model.provider,
            "model": active.model.model_name,
        }

    @app.post("/v1/sessions", response_model=SessionRecord, status_code=status.HTTP_201_CREATED)
    async def create_session(active: RuntimeDependency) -> SessionRecord:
        return await active.create_session()

    @app.get("/v1/sessions/{session_id}", response_model=SessionRecord)
    async def get_session(session_id: str, active: RuntimeDependency) -> SessionRecord:
        session = await active.store.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return session

    @app.post("/v1/sessions/{session_id}/runs", response_model=RunResult)
    async def run_agent(
        session_id: str,
        body: RunRequest,
        active: RuntimeDependency,
    ) -> RunResult:
        try:
            result = await active.run(session_id, body.prompt)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        if result.status == "failed":
            raise HTTPException(status_code=500, detail=result.error or "Agent run failed")
        return result

    @app.post("/v1/sessions/{session_id}/approvals", response_model=RunResult)
    async def resolve_approval(
        session_id: str,
        choice: ApprovalChoice,
        active: RuntimeDependency,
    ) -> RunResult:
        try:
            result = await active.resume(session_id, choice)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        if result.status == "failed":
            raise HTTPException(status_code=500, detail=result.error or "Agent run failed")
        return result

    @app.get("/v1/sessions/{session_id}/events", response_model=list[EventRecord])
    async def list_events(
        session_id: str,
        active: RuntimeDependency,
        after_id: Annotated[int, Query(ge=0)] = 0,
    ) -> list[EventRecord]:
        if await active.store.get_session(session_id) is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return await active.store.list_events(session_id, after_id)

    @app.get("/v1/sessions/{session_id}/events/stream")
    async def stream_events(
        session_id: str,
        request: Request,
        active: RuntimeDependency,
        after_id: Annotated[int, Query(ge=0)] = 0,
    ) -> StreamingResponse:
        if await active.store.get_session(session_id) is None:
            raise HTTPException(status_code=404, detail="Session not found")

        async def generate() -> AsyncIterator[str]:
            cursor = after_id
            async with active.broker.channel(session_id) as event_queue:
                for event in await active.store.list_events(session_id, cursor):
                    cursor = event.id
                    yield _format_sse(event)
                while not await request.is_disconnected():
                    try:
                        streamed_event = await asyncio.wait_for(event_queue.get(), timeout=15)
                    except TimeoutError:
                        yield ": heartbeat\n\n"
                        continue
                    if streamed_event.id > cursor:
                        cursor = streamed_event.id
                        yield _format_sse(streamed_event)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/v1/audit/verify", response_model=AuditVerification)
    async def verify_audit(active: RuntimeDependency) -> AuditVerification:
        try:
            count = await active.verify_audit()
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return AuditVerification(valid=True, records=count)

    return app


def _format_sse(event: EventRecord) -> str:
    payload: dict[str, Any] = event.model_dump(mode="json")
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"id: {event.id}\nevent: {event.kind}\ndata: {data}\n\n"


app = create_app()
