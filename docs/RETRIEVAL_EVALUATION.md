# Repository Retrieval Evaluation

- Repository: `examples/qwen-fixture`（原本机基准路径已省略）
- Embedding: `local-hash-embedding` (deterministic)
- Retrieval: BM25-style keyword + vector cosine + weighted RRF + reranking
- Top K: `3`

| Metric | Result |
| --- | ---: |
| Cases | 6 |
| Hit@3 | 100.0% |
| Precision@3 | 33.3% |
| Recall@3 | 100.0% |
| MRR | 0.889 |
| nDCG@3 | 0.917 |

## Per-case results

| Case | Recall | Reciprocal rank | nDCG | Retrieved paths |
| --- | ---: | ---: | ---: | --- |
| `calculator-discount` | 100.0% | 0.333 | 0.500 | `tests/test_calculator.py`, `README.md`, `src/calculator.py` |
| `sql-injection` | 100.0% | 1.000 | 1.000 | `src/order_service.py`, `tests/test_calculator.py`, `src/__init__.py` |
| `stock-race` | 100.0% | 1.000 | 1.000 | `src/order_service.py`, `tests/test_calculator.py`, `README.md` |
| `invalid-discount-test` | 100.0% | 1.000 | 1.000 | `tests/test_calculator.py`, `src/calculator.py`, `README.md` |
| `verification-command` | 100.0% | 1.000 | 1.000 | `README.md`, `tests/test_calculator.py`, `build/keep.txt` |
| `repository-purpose` | 100.0% | 1.000 | 1.000 | `README.md`, `tests/__init__.py`, `src/__init__.py` |

The evaluation set contains one relevant path per query, so the maximum possible Precision@3
is 33.3%. This is a deterministic regression benchmark, not a universal repository-search claim.
