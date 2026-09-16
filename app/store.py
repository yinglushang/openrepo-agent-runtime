from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Literal

import aiosqlite

from app.domain import EventRecord, PendingApproval, SessionRecord, utc_now


class SessionStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._connection: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def connect(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(self.database_path)
        self._connection.row_factory = aiosqlite.Row
        await self._connection.execute("PRAGMA journal_mode=WAL")
        await self._connection.execute("PRAGMA foreign_keys=ON")
        await self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_response TEXT,
                pending_approval_json TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                run_id TEXT,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS events_session_id_id ON events(session_id, id);
            """
        )
        await self._connection.commit()

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("SessionStore is not connected")
        return self._connection

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    async def create_session(self, session_id: str, provider: str, model: str) -> SessionRecord:
        timestamp = utc_now()
        async with self._write_lock:
            await self.connection.execute(
                """
                INSERT INTO sessions(id, status, provider, model, created_at, updated_at)
                VALUES (?, 'idle', ?, ?, ?, ?)
                """,
                (session_id, provider, model, timestamp, timestamp),
            )
            await self.connection.commit()
        record = await self.get_session(session_id)
        if record is None:
            raise RuntimeError("Created session could not be loaded")
        return record

    async def get_session(self, session_id: str) -> SessionRecord | None:
        cursor = await self.connection.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        pending_source = row["pending_approval_json"]
        return SessionRecord(
            id=row["id"],
            status=row["status"],
            provider=row["provider"],
            model=row["model"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_response=row["last_response"],
            pending_approval=PendingApproval.model_validate_json(pending_source) if pending_source else None,
        )

    async def update_session(
        self,
        session_id: str,
        *,
        status: Literal["idle", "running", "awaiting_approval", "completed", "failed"],
        last_response: str | None = None,
        pending_approval: PendingApproval | None = None,
    ) -> None:
        pending_json = pending_approval.model_dump_json() if pending_approval else None
        async with self._write_lock:
            await self.connection.execute(
                """
                UPDATE sessions
                SET status = ?, updated_at = ?, last_response = ?, pending_approval_json = ?
                WHERE id = ?
                """,
                (status, utc_now(), last_response, pending_json, session_id),
            )
            await self.connection.commit()

    async def append_event(
        self,
        session_id: str,
        run_id: str | None,
        kind: str,
        payload: dict[str, Any],
    ) -> EventRecord:
        timestamp = utc_now()
        async with self._write_lock:
            cursor = await self.connection.execute(
                """
                INSERT INTO events(session_id, run_id, kind, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    run_id,
                    kind,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    timestamp,
                ),
            )
            await self.connection.commit()
            event_id = cursor.lastrowid
            await cursor.close()
        if event_id is None:
            raise RuntimeError("SQLite did not return an event ID")
        return EventRecord(
            id=event_id,
            session_id=session_id,
            run_id=run_id,
            kind=kind,
            payload=payload,
            created_at=timestamp,
        )

    async def list_events(self, session_id: str, after_id: int = 0) -> list[EventRecord]:
        cursor = await self.connection.execute(
            "SELECT * FROM events WHERE session_id = ? AND id > ? ORDER BY id",
            (session_id, after_id),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            EventRecord(
                id=row["id"],
                session_id=row["session_id"],
                run_id=row["run_id"],
                kind=row["kind"],
                payload=json.loads(row["payload_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]
