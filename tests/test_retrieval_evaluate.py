from scripts.retrieval_evaluate import RetrievalCase, score_case


def test_retrieval_metrics_use_ranked_unique_paths() -> None:
    case = RetrievalCase(id="example", query="query", relevant_paths=["src/target.py"])

    result = score_case(case, ["src/other.py", "src/target.py", "README.md"], top_k=3)

    assert result.precision == 1 / 3
    assert result.recall == 1.0
    assert result.reciprocal_rank == 0.5
    assert 0 < result.ndcg < 1
