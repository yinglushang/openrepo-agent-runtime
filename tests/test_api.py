from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings


def test_demo_api_end_to_end(tmp_path: Path) -> None:
    settings = Settings(
        provider="demo",
        workspace=tmp_path / "workspace",
        data_dir=tmp_path / "data",
        policy_file=None,
        tool_max_retries=0,
    )
    with TestClient(create_app(settings=settings)) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["provider"] == "demo"

        session_response = client.post("/v1/sessions", json={})
        assert session_response.status_code == 201
        session_id = session_response.json()["id"]

        result = client.post(
            f"/v1/sessions/{session_id}/runs",
            json={"prompt": "列出工作区中的文件"},
        )
        assert result.status_code == 200
        assert result.json()["status"] == "completed"

        risky = client.post(
            f"/v1/sessions/{session_id}/runs",
            json={"prompt": "演示删除目录的危险操作"},
        )
        assert risky.status_code == 200
        assert risky.json()["status"] == "awaiting_approval"

        rejected = client.post(
            f"/v1/sessions/{session_id}/approvals",
            json={"approved": False, "remember_for_run": False},
        )
        assert rejected.status_code == 200
        assert rejected.json()["status"] == "completed"

        events = client.get(f"/v1/sessions/{session_id}/events")
        assert events.status_code == 200
        assert any(event["kind"] == "approval_required" for event in events.json())

        verified = client.get("/v1/audit/verify")
        assert verified.status_code == 200
        assert verified.json()["valid"] is True
        assert verified.json()["records"] >= 3


def test_static_console_and_openapi_are_available(tmp_path: Path) -> None:
    settings = Settings(
        provider="demo",
        workspace=tmp_path / "workspace",
        data_dir=tmp_path / "data",
        policy_file=None,
    )
    with TestClient(create_app(settings=settings)) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "OpenRepo Agent Runtime" in page.text
        assert client.get("/openapi.json").status_code == 200
