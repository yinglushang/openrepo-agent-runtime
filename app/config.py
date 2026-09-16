from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

ProviderName = Literal["demo", "openai", "qwen", "deepseek", "custom"]
EmbeddingProviderName = Literal["local", "remote"]
WorkflowMode = Literal["react", "multi_role"]


class PolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    allowed_tools: list[str] = Field(
        default_factory=lambda: [
            "list_files",
            "read_file",
            "retrieve_code",
            "run_command",
            "search_text",
            "write_file",
        ]
    )
    denied_tools: list[str] = Field(default_factory=list)
    max_tool_calls_per_run: int = Field(default=20, ge=1, le=1_000)
    max_repeated_calls: int = Field(default=3, ge=1, le=100)
    max_failures_per_run: int = Field(default=4, ge=1, le=100)
    approval_timeout_seconds: int = Field(default=60, ge=5, le=600)
    protected_paths: list[str] = Field(default_factory=lambda: [".git", ".venv", "data", "node_modules"])
    sensitive_read_paths: list[str] = Field(
        default_factory=lambda: [".env", ".npmrc", ".ssh", ".aws", "*.key", "*.pem"]
    )

    @field_validator("allowed_tools", "denied_tools", "protected_paths", "sensitive_read_paths")
    @classmethod
    def validate_string_lists(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("list entries must be non-empty strings")
        return list(dict.fromkeys(normalized))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="AGENT_",
        extra="ignore",
        case_sensitive=False,
    )

    provider: ProviderName = "demo"
    model: str = "demo-safe"
    api_key: SecretStr | None = None
    base_url: str | None = None
    workspace: Path = Path("workspace")
    data_dir: Path = Path("data")
    policy_file: Path | None = Path("config/runtime.example.json")
    tool_timeout_seconds: float = Field(default=20.0, ge=0.1, le=600)
    tool_max_retries: int = Field(default=1, ge=0, le=5)
    model_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    model_timeout_seconds: float = Field(default=60.0, ge=1.0, le=600.0)
    workflow_mode: WorkflowMode = "react"
    embedding_provider: EmbeddingProviderName = "local"
    embedding_model: str = "text-embedding-v4"
    embedding_base_url: str | None = None
    embedding_dimensions: int = Field(default=1_024, ge=64, le=3_072)
    embedding_batch_size: int = Field(default=10, ge=1, le=100)
    retrieval_top_k: int = Field(default=8, ge=1, le=30)
    retrieval_chunk_lines: int = Field(default=80, ge=20, le=300)
    retrieval_overlap_lines: int = Field(default=12, ge=0, le=100)
    retrieval_max_file_bytes: int = Field(default=500_000, ge=1_000, le=10_000_000)

    @model_validator(mode="after")
    def validate_provider(self) -> Settings:
        api_key = self.api_key.get_secret_value().strip() if self.api_key else ""
        if self.provider != "demo" and not api_key:
            raise ValueError(f"AGENT_API_KEY is required for provider {self.provider}")
        if self.provider == "custom" and not self.base_url:
            raise ValueError("AGENT_BASE_URL is required for the custom provider")
        if self.embedding_provider == "remote" and not api_key:
            raise ValueError("AGENT_API_KEY is required for remote embeddings")
        if self.retrieval_overlap_lines >= self.retrieval_chunk_lines:
            raise ValueError("AGENT_RETRIEVAL_OVERLAP_LINES must be smaller than chunk lines")
        return self

    @property
    def database_path(self) -> Path:
        return self.data_dir / "runtime.db"

    @property
    def audit_path(self) -> Path:
        return self.data_dir / "audit.jsonl"


PROVIDER_BASE_URLS: dict[ProviderName, str | None] = {
    "demo": None,
    "openai": None,
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "deepseek": "https://api.deepseek.com",
    "custom": None,
}


def load_policy(settings: Settings) -> PolicyConfig:
    if settings.policy_file is None:
        return PolicyConfig()
    try:
        source = settings.policy_file.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise ValueError(f"Policy file not found: {settings.policy_file}") from error
    try:
        return PolicyConfig.model_validate(json.loads(source))
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"Invalid policy file {settings.policy_file}: {error}") from error
