from __future__ import annotations

import asyncio
import hashlib
import math
import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from langchain_openai import OpenAIEmbeddings
from pydantic import BaseModel, ConfigDict

from app.config import PROVIDER_BASE_URLS, Settings

TOKEN_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+|[\u4e00-\u9fff]")
CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
SYMBOL_PATTERN = re.compile(
    r"^\s*(?:async\s+def|def|class|function|interface|type|enum|struct|func)\s+([A-Za-z_$][\w$]*)"
)
SUPPORTED_SUFFIXES = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".cs",
        ".go",
        ".h",
        ".hpp",
        ".java",
        ".js",
        ".jsx",
        ".kt",
        ".md",
        ".php",
        ".py",
        ".rb",
        ".rs",
        ".scala",
        ".sql",
        ".swift",
        ".toml",
        ".txt",
        ".ts",
        ".tsx",
        ".yaml",
        ".yml",
    }
)
IGNORED_PARTS = frozenset(
    {
        ".aws",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".ssh",
        ".venv",
        "data",
        "node_modules",
    }
)


class CodeChunk(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_id: str
    path: str
    language: str
    start_line: int
    end_line: int
    symbol: str | None = None
    content: str

    @property
    def embedding_text(self) -> str:
        symbol = f" symbol {self.symbol}" if self.symbol else ""
        return f"path {self.path}{symbol}\n{self.content}"


class RetrievalHit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    start_line: int
    end_line: int
    symbol: str | None
    content: str
    score: float
    keyword_score: float
    vector_score: float
    rrf_score: float


class EmbeddingBackend(Protocol):
    name: str

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for raw in TOKEN_PATTERN.findall(text):
        normalized = raw.replace("_", " ")
        for part in normalized.split():
            tokens.extend(piece.lower() for piece in CAMEL_BOUNDARY.split(part) if piece)
    return tokens


class LocalHashEmbedding:
    name = "local-hash-embedding"

    def __init__(self, dimensions: int = 384) -> None:
        self.dimensions = dimensions

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        counts = Counter(tokenize(text))
        for token, count in counts.items():
            digest = hashlib.sha256(token.encode()).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign * (1.0 + math.log(count))
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector] if norm else vector

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._embed(text)


class OpenAICompatibleEmbedding:
    def __init__(self, settings: Settings) -> None:
        if settings.api_key is None:
            raise ValueError("AGENT_API_KEY is required for remote embeddings")
        base_url = settings.embedding_base_url or settings.base_url or PROVIDER_BASE_URLS[settings.provider]
        self.name = settings.embedding_model
        self._client = OpenAIEmbeddings(  # type: ignore[call-arg]
            api_key=settings.api_key,
            base_url=base_url,
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
            chunk_size=settings.embedding_batch_size,
            max_retries=0,
            timeout=settings.model_timeout_seconds,
            check_embedding_ctx_length=False,
        )

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return await self._client.aembed_documents(list(texts))

    async def embed_query(self, text: str) -> list[float]:
        return await self._client.aembed_query(text)


def create_embedding_backend(settings: Settings) -> EmbeddingBackend:
    if settings.embedding_provider == "remote":
        return OpenAICompatibleEmbedding(settings)
    return LocalHashEmbedding(settings.embedding_dimensions)


def _chunk_id(path: str, start_line: int, end_line: int, content: str) -> str:
    digest = hashlib.sha256(f"{path}:{start_line}:{end_line}:{content}".encode()).hexdigest()
    return digest[:20]


def _language(path: Path) -> str:
    return path.suffix.lower().removeprefix(".") or "text"


def _split_region(
    *,
    path: str,
    language: str,
    lines: list[str],
    start: int,
    end: int,
    symbol: str | None,
    chunk_lines: int,
    overlap_lines: int,
) -> list[CodeChunk]:
    chunks: list[CodeChunk] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + chunk_lines - 1)
        content = "\n".join(lines[cursor - 1 : chunk_end]).strip()
        if content:
            chunks.append(
                CodeChunk(
                    chunk_id=_chunk_id(path, cursor, chunk_end, content),
                    path=path,
                    language=language,
                    start_line=cursor,
                    end_line=chunk_end,
                    symbol=symbol,
                    content=content,
                )
            )
        if chunk_end == end:
            break
        cursor = chunk_end - overlap_lines + 1
    return chunks


def chunk_file(
    root: Path,
    target: Path,
    *,
    chunk_lines: int = 80,
    overlap_lines: int = 12,
) -> list[CodeChunk]:
    try:
        source = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    lines = source.splitlines()
    if not lines:
        return []
    relative = target.relative_to(root).as_posix()
    language = _language(target)
    symbols = [
        (line_number, match.group(1))
        for line_number, line in enumerate(lines, start=1)
        if (match := SYMBOL_PATTERN.match(line)) is not None
    ]
    regions: list[tuple[int, int, str | None]] = []
    if symbols:
        first_symbol_line = symbols[0][0]
        if first_symbol_line > 1 and any(line.strip() for line in lines[: first_symbol_line - 1]):
            regions.append((1, first_symbol_line - 1, None))
        for index, (start, symbol) in enumerate(symbols):
            end = symbols[index + 1][0] - 1 if index + 1 < len(symbols) else len(lines)
            regions.append((start, end, symbol))
    else:
        regions.append((1, len(lines), None))

    chunks: list[CodeChunk] = []
    for start, end, symbol in regions:
        chunks.extend(
            _split_region(
                path=relative,
                language=language,
                lines=lines,
                start=start,
                end=end,
                symbol=symbol,
                chunk_lines=chunk_lines,
                overlap_lines=overlap_lines,
            )
        )
    return chunks


def scan_repository(
    root: Path,
    *,
    chunk_lines: int,
    overlap_lines: int,
    max_file_bytes: int,
) -> tuple[str, list[CodeChunk]]:
    files: list[Path] = []
    fingerprint_parts: list[str] = []
    for target in root.rglob("*"):
        if not target.is_file() or target.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        relative = target.relative_to(root)
        if IGNORED_PARTS.intersection(relative.parts):
            continue
        try:
            stat = target.stat()
        except OSError:
            continue
        if stat.st_size > max_file_bytes:
            continue
        files.append(target)
        fingerprint_parts.append(f"{relative.as_posix()}:{stat.st_mtime_ns}:{stat.st_size}")
    chunks = [
        chunk
        for target in sorted(files)
        for chunk in chunk_file(
            root,
            target,
            chunk_lines=chunk_lines,
            overlap_lines=overlap_lines,
        )
    ]
    fingerprint = hashlib.sha256("\n".join(fingerprint_parts).encode()).hexdigest()
    return fingerprint, chunks


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise ValueError("Embedding dimensions do not match")
    return sum(a * b for a, b in zip(left, right, strict=True))


class RepositoryIndex:
    def __init__(self, workspace: Path, settings: Settings, embedder: EmbeddingBackend) -> None:
        self.workspace = workspace.resolve()
        self.settings = settings
        self.embedder = embedder
        self._fingerprint = ""
        self._chunks: list[CodeChunk] = []
        self._vectors: list[list[float]] = []
        self._tokens: list[list[str]] = []
        self._document_frequency: Counter[str] = Counter()
        self._lock = asyncio.Lock()

    @property
    def chunk_count(self) -> int:
        return len(self._chunks)

    async def refresh(self) -> int:
        async with self._lock:
            fingerprint, chunks = await asyncio.to_thread(
                scan_repository,
                self.workspace,
                chunk_lines=self.settings.retrieval_chunk_lines,
                overlap_lines=self.settings.retrieval_overlap_lines,
                max_file_bytes=self.settings.retrieval_max_file_bytes,
            )
            if fingerprint == self._fingerprint:
                return len(self._chunks)
            vectors = (
                await self.embedder.embed_documents([chunk.embedding_text for chunk in chunks])
                if chunks
                else []
            )
            token_lists = [tokenize(chunk.embedding_text) for chunk in chunks]
            document_frequency: Counter[str] = Counter()
            for tokens in token_lists:
                document_frequency.update(set(tokens))
            self._fingerprint = fingerprint
            self._chunks = chunks
            self._vectors = vectors
            self._tokens = token_lists
            self._document_frequency = document_frequency
            return len(chunks)

    def _keyword_scores(self, query_tokens: list[str]) -> list[float]:
        document_count = len(self._chunks)
        average_length = (
            sum(len(tokens) for tokens in self._tokens) / document_count if document_count else 1.0
        )
        scores: list[float] = []
        for tokens in self._tokens:
            counts = Counter(tokens)
            score = 0.0
            for term in query_tokens:
                frequency = counts[term]
                if not frequency:
                    continue
                document_frequency = self._document_frequency[term]
                inverse_frequency = math.log(
                    1 + (document_count - document_frequency + 0.5) / (document_frequency + 0.5)
                )
                denominator = frequency + 1.5 * (1 - 0.75 + 0.75 * len(tokens) / average_length)
                score += inverse_frequency * (frequency * 2.5) / denominator
            scores.append(score)
        return scores

    async def search(self, query: str, top_k: int = 8) -> list[RetrievalHit]:
        if not query.strip():
            raise ValueError("Retrieval query must not be empty")
        await self.refresh()
        if not self._chunks:
            return []
        query_tokens = tokenize(query)
        query_vector = await self.embedder.embed_query(query)
        keyword_scores = self._keyword_scores(query_tokens)
        vector_scores = [cosine_similarity(query_vector, vector) for vector in self._vectors]
        candidate_limit = min(len(self._chunks), max(top_k * 4, top_k))
        keyword_rank = sorted(
            range(len(self._chunks)), key=lambda index: keyword_scores[index], reverse=True
        )[:candidate_limit]
        vector_rank = sorted(range(len(self._chunks)), key=lambda index: vector_scores[index], reverse=True)[
            :candidate_limit
        ]
        rrf: dict[int, float] = defaultdict(float)
        for rank, index in enumerate(keyword_rank, start=1):
            rrf[index] += 0.55 / (60 + rank)
        for rank, index in enumerate(vector_rank, start=1):
            rrf[index] += 0.45 / (60 + rank)

        query_token_set = set(query_tokens)
        hits: list[RetrievalHit] = []
        for index in set(keyword_rank) | set(vector_rank):
            chunk = self._chunks[index]
            chunk_tokens = set(self._tokens[index])
            coverage = len(query_token_set & chunk_tokens) / max(1, len(query_token_set))
            exact_bonus = 0.4 if query.lower() in chunk.embedding_text.lower() else 0.0
            path_tokens = set(tokenize(chunk.path))
            path_bonus = 0.3 * len(query_token_set & path_tokens) / max(1, len(query_token_set))
            symbol_tokens = set(tokenize(chunk.symbol or ""))
            symbol_bonus = 0.4 * len(query_token_set & symbol_tokens) / max(1, len(query_token_set))
            score = rrf[index] * 100 + coverage + exact_bonus + path_bonus + symbol_bonus
            hits.append(
                RetrievalHit(
                    path=chunk.path,
                    start_line=chunk.start_line,
                    end_line=chunk.end_line,
                    symbol=chunk.symbol,
                    content=chunk.content[:4_000],
                    score=round(score, 6),
                    keyword_score=round(keyword_scores[index], 6),
                    vector_score=round(vector_scores[index], 6),
                    rrf_score=round(rrf[index], 6),
                )
            )
        return sorted(hits, key=lambda hit: hit.score, reverse=True)[:top_k]
