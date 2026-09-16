from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.audit import AuditLog


async def _append(log: AuditLog, index: int) -> None:
    await log.append(
        session_id="session",
        run_id="run",
        tool_call_id=f"call-{index}",
        tool_name="read_file",
        kind="result",
        outcome="result_ok",
        risk="low",
        rule_id="default.allow",
        reason="ok",
        arguments={"path": f"file-{index}.txt"},
    )


@pytest.mark.asyncio
async def test_audit_chain_handles_concurrent_writers(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    await log.initialize()
    await asyncio.gather(*(_append(log, index) for index in range(25)))
    assert await log.verify_file() == 25
    content = path.read_text(encoding="utf-8")
    assert '"path"' not in content
    assert "file-0.txt" not in content


@pytest.mark.asyncio
async def test_audit_chain_detects_tampering(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    await log.initialize()
    await _append(log, 1)
    source = path.read_text(encoding="utf-8").replace("result_ok", "result_bad")
    path.write_text(source, encoding="utf-8")
    with pytest.raises(ValueError, match="Audit hash mismatch"):
        await log.verify_file()
