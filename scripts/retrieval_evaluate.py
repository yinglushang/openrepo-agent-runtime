from __future__ import annotations

import argparse
import asyncio
import math
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings
from app.retrieval import LocalHashEmbedding, RepositoryIndex

ROOT = Path(__file__).resolve().parents[1]


class RetrievalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    query: str
    relevant_paths: list[str] = Field(min_length=1)


class CaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    query: str
    relevant_paths: list[str]
    retrieved_paths: list[str]
    precision: float
    recall: float
    reciprocal_rank: float
    ndcg: float


def load_cases(path: Path) -> list[RetrievalCase]:
    cases = [
        RetrievalCase.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not cases:
        raise ValueError("Retrieval evaluation set is empty")
    return cases


def score_case(case: RetrievalCase, retrieved_paths: list[str], top_k: int) -> CaseResult:
    relevant = set(case.relevant_paths)
    retrieved = retrieved_paths[:top_k]
    relevance = [1 if path in relevant else 0 for path in retrieved]
    hits = sum(relevance)
    first_rank = next((index for index, value in enumerate(relevance, start=1) if value), None)
    dcg = sum(value / math.log2(index + 1) for index, value in enumerate(relevance, start=1))
    ideal_hits = min(len(relevant), top_k)
    ideal_dcg = sum(1 / math.log2(index + 1) for index in range(1, ideal_hits + 1))
    return CaseResult(
        id=case.id,
        query=case.query,
        relevant_paths=case.relevant_paths,
        retrieved_paths=retrieved,
        precision=hits / top_k,
        recall=hits / len(relevant),
        reciprocal_rank=1 / first_rank if first_rank is not None else 0.0,
        ndcg=dcg / ideal_dcg if ideal_dcg else 0.0,
    )


async def evaluate(
    repository: Path,
    cases: list[RetrievalCase],
    top_k: int,
) -> tuple[str, bool]:
    settings = Settings(
        provider="demo",
        workspace=repository,
        data_dir=repository / ".retrieval-eval-data",
        policy_file=None,
        embedding_provider="local",
        embedding_dimensions=1_024,
    )
    index = RepositoryIndex(
        repository,
        settings,
        LocalHashEmbedding(settings.embedding_dimensions),
    )
    results: list[CaseResult] = []
    for case in cases:
        hits = await index.search(case.query, top_k=max(top_k * 3, top_k))
        unique_paths = list(dict.fromkeys(hit.path for hit in hits))
        results.append(score_case(case, unique_paths, top_k))

    count = len(results)
    precision = sum(result.precision for result in results) / count
    recall = sum(result.recall for result in results) / count
    mrr = sum(result.reciprocal_rank for result in results) / count
    ndcg = sum(result.ndcg for result in results) / count
    hit_rate = sum(result.recall > 0 for result in results) / count
    rows = "\n".join(
        f"| `{result.id}` | {result.recall:.1%} | {result.reciprocal_rank:.3f} | "
        f"{result.ndcg:.3f} | {', '.join(f'`{path}`' for path in result.retrieved_paths)} |"
        for result in results
    )
    report = f"""# Repository Retrieval Evaluation

- Repository: `{repository.as_posix()}`
- Embedding: `local-hash-embedding` (deterministic)
- Retrieval: BM25-style keyword + vector cosine + weighted RRF + reranking
- Top K: `{top_k}`

| Metric | Result |
| --- | ---: |
| Cases | {count} |
| Hit@{top_k} | {hit_rate:.1%} |
| Precision@{top_k} | {precision:.1%} |
| Recall@{top_k} | {recall:.1%} |
| MRR | {mrr:.3f} |
| nDCG@{top_k} | {ndcg:.3f} |

## Per-case results

| Case | Recall | Reciprocal rank | nDCG | Retrieved paths |
| --- | ---: | ---: | ---: | --- |
{rows}

The evaluation set contains one relevant path per query, so the maximum possible Precision@{top_k}
is {1 / top_k:.1%}. This is a deterministic regression benchmark, not a universal repository-search claim.
"""
    passed = all(result.recall == 1.0 for result in results)
    return report, passed


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate hybrid repository retrieval")
    parser.add_argument(
        "--repository",
        type=Path,
        default=ROOT / "examples" / "qwen-fixture",
    )
    parser.add_argument(
        "--cases",
        type=Path,
        default=ROOT / "evals" / "retrieval_cases.jsonl",
    )
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--output", type=Path, default=None)
    arguments = parser.parse_args()
    report, passed = asyncio.run(
        evaluate(
            arguments.repository.resolve(),
            load_cases(arguments.cases),
            arguments.top_k,
        )
    )
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(report, encoding="utf-8")
    print(report)
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
