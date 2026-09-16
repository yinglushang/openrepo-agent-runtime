from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class PolicyAction(StrEnum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


class PolicyDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: PolicyAction
    risk: RiskLevel
    rule_id: str
    reason: str
    signature: str


class ToolRequest(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    call_id: str


class ApprovalChoice(BaseModel):
    approved: bool
    remember_for_run: bool = False


class PendingApproval(BaseModel):
    session_id: str
    run_id: str
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    risk: RiskLevel
    rule_id: str
    reason: str
    expires_at: str


class SessionRecord(BaseModel):
    id: str
    status: Literal["idle", "running", "awaiting_approval", "completed", "failed"]
    provider: str
    model: str
    created_at: str
    updated_at: str
    last_response: str | None = None
    pending_approval: PendingApproval | None = None


class EventRecord(BaseModel):
    id: int
    session_id: str
    run_id: str | None
    kind: str
    payload: dict[str, Any]
    created_at: str


class RunResult(BaseModel):
    session_id: str
    run_id: str
    status: Literal["completed", "awaiting_approval", "failed"]
    response: str | None = None
    pending_approval: PendingApproval | None = None
    error: str | None = None

