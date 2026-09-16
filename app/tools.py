from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from app.retrieval import RepositoryIndex


class PathInput(BaseModel):
    path: str = Field(description="Path relative to the configured workspace")


class ListFilesInput(BaseModel):
    pattern: str = Field(default="**/*", description="Glob pattern relative to the workspace")
    limit: int = Field(default=100, ge=1, le=500)


class SearchTextInput(BaseModel):
    query: str = Field(min_length=1, description="Regular expression to search for")
    pattern: str = Field(default="**/*", description="File glob to search")
    limit: int = Field(default=50, ge=1, le=200)


class WriteFileInput(PathInput):
    content: str
    overwrite: bool = False


class RunCommandInput(BaseModel):
    command: str = Field(min_length=1, description="Shell command executed inside the workspace")


class RetrieveCodeInput(BaseModel):
    query: str = Field(min_length=1, description="Natural-language or code search query")
    top_k: int = Field(default=8, ge=1, le=30)


class WorkspaceTools:
    IGNORED_PARTS = frozenset({".git", ".venv", "__pycache__", "data", "node_modules"})

    def __init__(self, workspace: Path, repository_index: RepositoryIndex) -> None:
        self.workspace = workspace.resolve()
        self.repository_index = repository_index
        self.workspace.mkdir(parents=True, exist_ok=True)

    def resolve_path(self, raw_path: str) -> Path:
        target = (self.workspace / raw_path).resolve()
        if not target.is_relative_to(self.workspace):
            raise PermissionError(f"Path escapes workspace: {raw_path}")
        return target

    async def read_file(self, path: str) -> str:
        """Read a UTF-8 text file from the workspace."""
        target = self.resolve_path(path)
        if not target.is_file():
            raise FileNotFoundError(path)
        source = await asyncio.to_thread(target.read_text, encoding="utf-8")
        return source if len(source) <= 50_000 else f"{source[:50_000]}\n...[truncated]"

    async def write_file(self, path: str, content: str, overwrite: bool = False) -> dict[str, Any]:
        """Create a UTF-8 file in the workspace, optionally replacing an existing file."""
        target = self.resolve_path(path)
        if target.exists() and not overwrite:
            raise FileExistsError(f"File already exists: {path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_text, content, encoding="utf-8")
        return {"path": target.relative_to(self.workspace).as_posix(), "bytes": len(content.encode())}

    async def list_files(self, pattern: str = "**/*", limit: int = 100) -> list[str]:
        """List files in the workspace that match a glob pattern."""

        def collect() -> list[str]:
            matches: list[str] = []
            for target in self.workspace.glob(pattern):
                relative = target.relative_to(self.workspace)
                if target.is_file() and not self.IGNORED_PARTS.intersection(relative.parts):
                    matches.append(relative.as_posix())
                    if len(matches) >= limit:
                        break
            return sorted(matches)

        return await asyncio.to_thread(collect)

    async def search_text(self, query: str, pattern: str = "**/*", limit: int = 50) -> list[dict[str, Any]]:
        """Search workspace text files with a regular expression."""
        expression = re.compile(query)

        def collect() -> list[dict[str, Any]]:
            matches: list[dict[str, Any]] = []
            for target in self.workspace.glob(pattern):
                relative = target.relative_to(self.workspace)
                if not target.is_file() or self.IGNORED_PARTS.intersection(relative.parts):
                    continue
                try:
                    lines = target.read_text(encoding="utf-8").splitlines()
                except (OSError, UnicodeDecodeError):
                    continue
                for line_number, line in enumerate(lines, start=1):
                    if expression.search(line):
                        matches.append({"path": relative.as_posix(), "line": line_number, "text": line[:500]})
                        if len(matches) >= limit:
                            return matches
            return matches

        return await asyncio.to_thread(collect)

    async def run_command(self, command: str) -> dict[str, Any]:
        """Run a shell command inside the workspace and return its combined output."""
        environment = os.environ.copy()
        python_bin = str(Path(sys.executable).parent)
        environment["PATH"] = os.pathsep.join((python_bin, environment.get("PATH", "")))
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=self.workspace,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await process.communicate()
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            raise
        output = stdout.decode(errors="replace")
        if len(output) > 30_000:
            output = f"{output[:30_000]}\n...[truncated]"
        return {"exit_code": process.returncode, "output": output}

    async def retrieve_code(self, query: str, top_k: int = 8) -> list[dict[str, Any]]:
        """Retrieve relevant repository chunks with hybrid keyword/vector search and reranking."""
        hits = await self.repository_index.search(query, top_k=top_k)
        return [hit.model_dump(mode="json") for hit in hits]


class ToolRegistry:
    def __init__(self, workspace: Path, repository_index: RepositoryIndex) -> None:
        implementations = WorkspaceTools(workspace, repository_index)
        self._tools: dict[str, BaseTool] = {
            "read_file": StructuredTool.from_function(
                coroutine=implementations.read_file,
                name="read_file",
                description="Read a UTF-8 text file inside the workspace.",
                args_schema=PathInput,
            ),
            "write_file": StructuredTool.from_function(
                coroutine=implementations.write_file,
                name="write_file",
                description="Create or replace a UTF-8 text file inside the workspace.",
                args_schema=WriteFileInput,
            ),
            "list_files": StructuredTool.from_function(
                coroutine=implementations.list_files,
                name="list_files",
                description="List workspace files with a glob pattern.",
                args_schema=ListFilesInput,
            ),
            "search_text": StructuredTool.from_function(
                coroutine=implementations.search_text,
                name="search_text",
                description="Search text files in the workspace with a regular expression.",
                args_schema=SearchTextInput,
            ),
            "run_command": StructuredTool.from_function(
                coroutine=implementations.run_command,
                name="run_command",
                description="Run a shell command inside the workspace.",
                args_schema=RunCommandInput,
            ),
            "retrieve_code": StructuredTool.from_function(
                coroutine=implementations.retrieve_code,
                name="retrieve_code",
                description=(
                    "Retrieve relevant code chunks using keyword search, embeddings, RRF fusion, "
                    "and reranking. Use this before broad file reads when locating implementation code."
                ),
                args_schema=RetrieveCodeInput,
            ),
        }

    def definitions(self) -> list[BaseTool]:
        return list(self._tools.values())

    async def invoke(self, name: str, arguments: dict[str, Any]) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"Unknown tool: {name}")
        return await tool.ainvoke(arguments)


def serialize_tool_result(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
