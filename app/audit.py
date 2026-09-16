from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.domain import utc_now


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def hash_arguments(arguments: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(arguments).encode()).hexdigest()


class AuditRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    sequence: int
    timestamp: str
    previous_hash: str
    session_id: str
    run_id: str
    tool_call_id: str
    tool_name: str
    kind: str
    outcome: str
    risk: str
    rule_id: str
    reason: str
    arguments_hash: str
    hash: str


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._sequence = 0
        self._previous_hash = "GENESIS"
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        records = await asyncio.to_thread(self._read_records)
        self.verify(records)
        if records:
            self._sequence = records[-1].sequence
            self._previous_hash = records[-1].hash

    def _read_records(self) -> list[AuditRecord]:
        if not self.path.exists():
            return []
        records: list[AuditRecord] = []
        for line_number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                records.append(AuditRecord.model_validate_json(line))
            except ValueError as error:
                raise ValueError(f"Invalid audit record at line {line_number}: {error}") from error
        return records

    @staticmethod
    def verify(records: list[AuditRecord]) -> None:
        previous_hash = "GENESIS"
        for expected_sequence, record in enumerate(records, start=1):
            if record.version != 1:
                raise ValueError(f"Unsupported audit version at sequence {expected_sequence}")
            if record.sequence != expected_sequence:
                raise ValueError(f"Invalid audit sequence at {expected_sequence}")
            if record.previous_hash != previous_hash:
                raise ValueError(f"Broken audit chain at sequence {expected_sequence}")
            unsigned = record.model_dump(exclude={"hash"})
            expected_hash = hashlib.sha256(canonical_json(unsigned).encode()).hexdigest()
            if record.hash != expected_hash:
                raise ValueError(f"Audit hash mismatch at sequence {expected_sequence}")
            previous_hash = record.hash

    async def append(
        self,
        *,
        session_id: str,
        run_id: str,
        tool_call_id: str,
        tool_name: str,
        kind: str,
        outcome: str,
        risk: str,
        rule_id: str,
        reason: str,
        arguments: dict[str, Any],
    ) -> AuditRecord:
        async with self._lock:
            unsigned: dict[str, Any] = {
                "version": 1,
                "sequence": self._sequence + 1,
                "timestamp": utc_now(),
                "previous_hash": self._previous_hash,
                "session_id": session_id,
                "run_id": run_id,
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "kind": kind,
                "outcome": outcome,
                "risk": risk,
                "rule_id": rule_id,
                "reason": reason,
                "arguments_hash": hash_arguments(arguments),
            }
            record_hash = hashlib.sha256(canonical_json(unsigned).encode()).hexdigest()
            record = AuditRecord(**unsigned, hash=record_hash)
            line = f"{record.model_dump_json()}\n"
            await asyncio.to_thread(self._append_line, line)
            self._sequence = record.sequence
            self._previous_hash = record.hash
            return record

    def _append_line(self, line: str) -> None:
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()

    async def verify_file(self) -> int:
        async with self._lock:
            records = await asyncio.to_thread(self._read_records)
            self.verify(records)
            return len(records)
