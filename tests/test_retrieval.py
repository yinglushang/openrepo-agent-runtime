from pathlib import Path

import pytest

from app.config import Settings
from app.retrieval import (
    LocalHashEmbedding,
    OpenAICompatibleEmbedding,
    RepositoryIndex,
    chunk_file,
)
from app.service import AgentRuntime


def retrieval_settings(tmp_path: Path) -> Settings:
    return Settings(
        provider="demo",
        workspace=tmp_path / "workspace",
        data_dir=tmp_path / "data",
        policy_file=None,
        embedding_provider="local",
        embedding_dimensions=128,
        retrieval_chunk_lines=20,
        retrieval_overlap_lines=4,
    )


def test_code_chunking_preserves_symbol_and_line_metadata(tmp_path: Path) -> None:
    source = tmp_path / "service.py"
    source.write_text(
        "from __future__ import annotations\n\n"
        "def find_order(order_id: str) -> str:\n"
        "    return f\"SELECT * FROM orders WHERE id = '{order_id}'\"\n\n"
        "def reserve_stock(quantity: int) -> bool:\n"
        "    return quantity > 0\n",
        encoding="utf-8",
    )

    chunks = chunk_file(tmp_path, source, chunk_lines=20, overlap_lines=4)

    assert [chunk.symbol for chunk in chunks] == [None, "find_order", "reserve_stock"]
    assert chunks[1].path == "service.py"
    assert chunks[1].start_line == 3


@pytest.mark.asyncio
async def test_hybrid_retrieval_ranks_relevant_symbol_first(tmp_path: Path) -> None:
    settings = retrieval_settings(tmp_path)
    settings.workspace.mkdir(parents=True)
    (settings.workspace / "order_service.py").write_text(
        "def find_order(connection, order_id):\n"
        "    query = f\"SELECT * FROM orders WHERE id = '{order_id}'\"\n"
        "    return connection.execute(query).fetchone()\n",
        encoding="utf-8",
    )
    (settings.workspace / "email_service.py").write_text(
        "def send_receipt(address):\n    return address\n",
        encoding="utf-8",
    )
    index = RepositoryIndex(
        settings.workspace,
        settings,
        LocalHashEmbedding(settings.embedding_dimensions),
    )

    hits = await index.search("find_order SQL injection query", top_k=2)

    assert hits[0].path == "order_service.py"
    assert hits[0].symbol == "find_order"
    assert hits[0].keyword_score > 0
    assert hits[0].rrf_score > 0


@pytest.mark.asyncio
async def test_retrieve_code_is_registered_as_runtime_tool(tmp_path: Path) -> None:
    settings = retrieval_settings(tmp_path)
    runtime = await AgentRuntime.create(settings)
    try:
        result = await runtime.tools.invoke(
            "retrieve_code",
            {"query": "offline Demo Agent example file", "top_k": 3},
        )
        definitions = {tool.name for tool in runtime.tools.definitions()}

        assert "retrieve_code" in definitions
        assert result
        assert result[0]["path"] == "example.txt"
    finally:
        await runtime.close()


def test_remote_embedding_keeps_source_text_for_openai_compatible_api(tmp_path: Path) -> None:
    settings = Settings(
        provider="qwen",
        model="qwen-plus",
        api_key="test-key",
        workspace=tmp_path / "workspace",
        data_dir=tmp_path / "data",
        policy_file=None,
        embedding_provider="remote",
        embedding_dimensions=1_024,
    )

    backend = OpenAICompatibleEmbedding(settings)

    assert backend._client.check_embedding_ctx_length is False
