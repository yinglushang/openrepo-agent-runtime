from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import PolicyConfig
from app.domain import PolicyAction, PolicyDecision, RiskLevel, ToolRequest


@dataclass(frozen=True)
class ShellRule:
    rule_id: str
    action: PolicyAction
    risk: RiskLevel
    reason: str
    patterns: tuple[re.Pattern[str], ...]


def _compile(*patterns: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns)


SHELL_RULES = (
    ShellRule(
        "shell.system_destruction",
        PolicyAction.DENY,
        RiskLevel.CRITICAL,
        "Command can destroy a filesystem, disk, or host session",
        _compile(
            r"\brm\s+(?:-[a-z]*r[a-z]*f[a-z]*|--recursive\s+--force|--force\s+--recursive)\s+(?:[/~]|\$HOME)(?:\s|$)",
            r"\b(?:mkfs(?:\.[a-z0-9]+)?|format-volume|clear-disk|initialize-disk)\b",
            r"\bdd\b[^\r\n;|]*\bof=/dev/",
            r"\b(?:shutdown|reboot|halt|poweroff|stop-computer|restart-computer)\b",
            r":\(\)\s*\{\s*:\|:\s*&\s*\}\s*;\s*:",
        ),
    ),
    ShellRule(
        "shell.destructive_files",
        PolicyAction.CONFIRM,
        RiskLevel.HIGH,
        "Command recursively or forcibly deletes files",
        _compile(
            r"\brm\s+(?:-[a-z]*r[a-z]*|--recursive|-f|--force)\b",
            r"\bremove-item\b[^\r\n;|]*(?:-recurse|-force|-r\b|-fo\b)",
            r"\b(?:del|rmdir|rd)\b[^\r\n;|]*/s\b",
            r"\bpython(?:3|\.exe)?\b[^\r\n]*-c\b[^\r\n]*(?:shutil\.rmtree|os\.(?:remove|unlink))\b",
            r"\bnode(?:\.exe)?\b[^\r\n]*-e\b[^\r\n]*(?:rmSync|rmdirSync)\b",
        ),
    ),
    ShellRule(
        "shell.git_history",
        PolicyAction.CONFIRM,
        RiskLevel.HIGH,
        "Command can discard Git state or rewrite remote history",
        _compile(
            r"\bgit\s+reset\b[^\r\n;|]*--hard\b",
            r"\bgit\s+clean\b[^\r\n;|]*-[a-z]*f",
            r"\bgit\s+push\b[^\r\n;|]*(?:--force(?:-with-lease)?|-f\b)",
        ),
    ),
    ShellRule(
        "shell.privilege",
        PolicyAction.CONFIRM,
        RiskLevel.HIGH,
        "Command changes privileges or broad filesystem permissions",
        _compile(r"\b(?:sudo|runas)\b", r"\b(?:chmod|chown)\b[^\r\n;|]*(?:777|-R\b)"),
    ),
    ShellRule(
        "shell.external_mutation",
        PolicyAction.CONFIRM,
        RiskLevel.HIGH,
        "Command can publish, deploy, or mutate external infrastructure",
        _compile(
            r"\b(?:npm|pnpm|yarn)\s+(?:publish|deploy)\b",
            r"\b(?:terraform|tofu)\s+(?:apply|destroy)\b",
            r"\bkubectl\s+(?:apply|delete|replace|patch)\b",
            r"\bhelm\s+(?:install|upgrade|uninstall)\b",
            r"\bdocker\s+system\s+prune\b",
            r"\b(?:curl|wget|invoke-webrequest)\b[^\r\n]*(?:-X|--request|\bmethod\b)[^\r\n]*(?:POST|PUT|PATCH|DELETE)\b",
        ),
    ),
    ShellRule(
        "shell.dependency_change",
        PolicyAction.CONFIRM,
        RiskLevel.MEDIUM,
        "Command changes installed dependencies or system packages",
        _compile(
            r"\b(?:npm|pnpm|yarn)\s+(?:install|add|remove|uninstall|update|upgrade)\b",
            r"\b(?:pip|pip3|uv)\s+(?:install|uninstall|sync)\b",
            r"\b(?:apt|apt-get|brew|winget|choco)\s+(?:install|remove|uninstall|upgrade)\b",
        ),
    ),
)


def stable_signature(name: str, arguments: dict[str, Any]) -> str:
    canonical = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(f"{name}:{canonical}".encode()).hexdigest()


class ToolPolicy:
    FILE_TOOLS = frozenset({"read_file", "write_file"})

    def __init__(self, config: PolicyConfig, workspace: Path) -> None:
        self.config = config
        self.workspace = workspace.resolve()

    def evaluate(self, request: ToolRequest) -> PolicyDecision:
        signature = stable_signature(request.name, request.arguments)
        if request.name in self.config.denied_tools:
            return self._decision(
                PolicyAction.DENY,
                RiskLevel.HIGH,
                "tool.explicitly_denied",
                f"Tool {request.name!r} is explicitly denied",
                signature,
            )
        if request.name not in self.config.allowed_tools:
            return self._decision(
                PolicyAction.DENY,
                RiskLevel.HIGH,
                "tool.not_allowed",
                f"Tool {request.name!r} is not in the allowlist",
                signature,
            )
        if request.name in self.FILE_TOOLS:
            path_decision = self._evaluate_path(request, signature)
            if path_decision is not None:
                return path_decision
        if request.name == "run_command":
            shell_decision = self._evaluate_command(request, signature)
            if shell_decision is not None:
                return shell_decision
        return self._decision(
            PolicyAction.ALLOW,
            RiskLevel.LOW,
            "default.allow",
            "Tool call passed runtime policy",
            signature,
        )

    def _evaluate_path(self, request: ToolRequest, signature: str) -> PolicyDecision | None:
        raw_path = request.arguments.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return self._decision(
                PolicyAction.DENY,
                RiskLevel.HIGH,
                "path.invalid",
                "File tool call has no valid path",
                signature,
            )
        target = (self.workspace / raw_path).resolve()
        if not target.is_relative_to(self.workspace):
            return self._decision(
                PolicyAction.DENY,
                RiskLevel.CRITICAL,
                "path.outside_workspace",
                f"{request.name} targets a path outside the workspace",
                signature,
            )
        if request.name == "write_file" and self._matches_any_path(target, self.config.protected_paths):
            return self._decision(
                PolicyAction.DENY,
                RiskLevel.HIGH,
                "path.protected_write",
                "Write targets a protected project path",
                signature,
            )
        if request.name == "read_file" and self._matches_any_path(target, self.config.sensitive_read_paths):
            return self._decision(
                PolicyAction.CONFIRM,
                RiskLevel.HIGH,
                "path.sensitive_read",
                "Read targets a potentially sensitive file",
                signature,
            )
        return None

    def _matches_any_path(self, target: Path, patterns: list[str]) -> bool:
        relative = target.relative_to(self.workspace)
        parts = tuple(part.lower() for part in relative.parts)
        normalized = relative.as_posix().lower()
        for raw_pattern in patterns:
            pattern = raw_pattern.replace("\\", "/").removeprefix("./").lower()
            if pattern.startswith("*.") and normalized.endswith(pattern[1:]):
                return True
            if pattern == ".env" and any(part == ".env" or part.startswith(".env.") for part in parts):
                return True
            if normalized == pattern or normalized.startswith(f"{pattern}/") or pattern in parts:
                return True
        return False

    @staticmethod
    def _evaluate_command(request: ToolRequest, signature: str) -> PolicyDecision | None:
        command = request.arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            return ToolPolicy._decision(
                PolicyAction.DENY,
                RiskLevel.HIGH,
                "shell.invalid",
                "Shell tool call has no valid command",
                signature,
            )
        for rule in SHELL_RULES:
            if any(pattern.search(command) for pattern in rule.patterns):
                return ToolPolicy._decision(rule.action, rule.risk, rule.rule_id, rule.reason, signature)
        return None

    @staticmethod
    def _decision(
        action: PolicyAction,
        risk: RiskLevel,
        rule_id: str,
        reason: str,
        signature: str,
    ) -> PolicyDecision:
        return PolicyDecision(action=action, risk=risk, rule_id=rule_id, reason=reason, signature=signature)


def apply_run_limits(
    decision: PolicyDecision,
    *,
    tool_calls: int,
    failures: int,
    repeated_calls: int,
    config: PolicyConfig,
) -> PolicyDecision:
    if tool_calls > config.max_tool_calls_per_run:
        return ToolPolicy._decision(
            PolicyAction.DENY,
            RiskLevel.HIGH,
            "budget.tool_calls",
            f"Run exceeded {config.max_tool_calls_per_run} tool calls",
            decision.signature,
        )
    if repeated_calls > config.max_repeated_calls:
        return ToolPolicy._decision(
            PolicyAction.DENY,
            RiskLevel.HIGH,
            "budget.repeated_call",
            f"Identical call repeated more than {config.max_repeated_calls} times",
            decision.signature,
        )
    if failures >= config.max_failures_per_run:
        return ToolPolicy._decision(
            PolicyAction.DENY,
            RiskLevel.HIGH,
            "budget.failures",
            f"Run reached {config.max_failures_per_run} failed calls",
            decision.signature,
        )
    return decision
