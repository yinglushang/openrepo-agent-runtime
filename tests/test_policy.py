from __future__ import annotations

from pathlib import Path

import pytest

from app.config import PolicyConfig
from app.domain import PolicyAction, RiskLevel, ToolRequest
from app.policy import ToolPolicy, apply_run_limits


@pytest.fixture
def policy(tmp_path: Path) -> ToolPolicy:
    return ToolPolicy(PolicyConfig(), tmp_path)


@pytest.mark.parametrize(
    ("command", "action", "rule_id"),
    [
        ("python -m pytest", PolicyAction.ALLOW, "default.allow"),
        ("rm -rf build", PolicyAction.CONFIRM, "shell.destructive_files"),
        (
            'python -c "import shutil; shutil.rmtree(\'build\')"',
            PolicyAction.CONFIRM,
            "shell.destructive_files",
        ),
        (
            'node -e "require(\'fs\').rmSync(\'build\', {recursive:true})"',
            PolicyAction.CONFIRM,
            "shell.destructive_files",
        ),
        ("rm -rf /", PolicyAction.DENY, "shell.system_destruction"),
        ("git reset --hard HEAD~1", PolicyAction.CONFIRM, "shell.git_history"),
        ("git push --force origin main", PolicyAction.CONFIRM, "shell.git_history"),
        ("pip install httpx", PolicyAction.CONFIRM, "shell.dependency_change"),
        ("terraform destroy", PolicyAction.CONFIRM, "shell.external_mutation"),
        ("shutdown /s", PolicyAction.DENY, "shell.system_destruction"),
    ],
)
def test_shell_classification(
    policy: ToolPolicy,
    command: str,
    action: PolicyAction,
    rule_id: str,
) -> None:
    decision = policy.evaluate(
        ToolRequest(name="run_command", arguments={"command": command}, call_id="call-1")
    )
    assert decision.action == action
    assert decision.rule_id == rule_id


def test_sensitive_file_read_requires_approval(tmp_path: Path) -> None:
    policy = ToolPolicy(PolicyConfig(), tmp_path)
    decision = policy.evaluate(
        ToolRequest(name="read_file", arguments={"path": ".env.local"}, call_id="call-1")
    )
    assert decision.action == PolicyAction.CONFIRM
    assert decision.risk == RiskLevel.HIGH
    assert decision.rule_id == "path.sensitive_read"


def test_write_outside_workspace_is_denied(tmp_path: Path) -> None:
    policy = ToolPolicy(PolicyConfig(), tmp_path)
    decision = policy.evaluate(
        ToolRequest(name="write_file", arguments={"path": "../escape.txt"}, call_id="call-1")
    )
    assert decision.action == PolicyAction.DENY
    assert decision.risk == RiskLevel.CRITICAL


def test_read_outside_workspace_is_denied(tmp_path: Path) -> None:
    policy = ToolPolicy(PolicyConfig(), tmp_path)
    decision = policy.evaluate(
        ToolRequest(name="read_file", arguments={"path": "../outside.txt"}, call_id="call-1")
    )
    assert decision.action == PolicyAction.DENY
    assert decision.risk == RiskLevel.CRITICAL


def test_protected_path_write_is_denied(tmp_path: Path) -> None:
    policy = ToolPolicy(PolicyConfig(), tmp_path)
    decision = policy.evaluate(
        ToolRequest(name="write_file", arguments={"path": ".git/config"}, call_id="call-1")
    )
    assert decision.action == PolicyAction.DENY
    assert decision.rule_id == "path.protected_write"


def test_unknown_tool_is_denied(tmp_path: Path) -> None:
    policy = ToolPolicy(PolicyConfig(), tmp_path)
    decision = policy.evaluate(ToolRequest(name="email_everyone", call_id="call-1"))
    assert decision.action == PolicyAction.DENY
    assert decision.rule_id == "tool.not_allowed"


@pytest.mark.parametrize(
    ("tool_calls", "failures", "repeated", "rule_id"),
    [
        (21, 0, 1, "budget.tool_calls"),
        (1, 0, 4, "budget.repeated_call"),
        (1, 4, 1, "budget.failures"),
    ],
)
def test_run_circuit_breakers(
    policy: ToolPolicy,
    tool_calls: int,
    failures: int,
    repeated: int,
    rule_id: str,
) -> None:
    original = policy.evaluate(
        ToolRequest(name="list_files", arguments={"pattern": "**/*"}, call_id="call-1")
    )
    decision = apply_run_limits(
        original,
        tool_calls=tool_calls,
        failures=failures,
        repeated_calls=repeated,
        config=PolicyConfig(),
    )
    assert decision.action == PolicyAction.DENY
    assert decision.rule_id == rule_id
