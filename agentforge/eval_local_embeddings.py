"""使用明确指定的本地 Embedding 服务运行固定合成检索基准。"""

import argparse
import json
from math import sqrt
from pathlib import Path
import sys
from time import perf_counter

from .embeddings import LlamaServerEmbeddingProvider, VectorCandidateRetriever, cosine_similarity, embed_text
from .embeddings.llama_server import DEFAULT_QUERY_INSTRUCTION
from .evaluation import evaluate_benchmark
from .indexer import index_repository
from .store import IndexStore


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATASET_PATH = PROJECT_ROOT / "tests" / "fixtures" / "retrieval_eval.json"
CANDIDATE_DATASET_PATH = (
    PROJECT_ROOT / "tests" / "fixtures" / "retrieval_eval_expansion_candidates.json"
)
REVIEW_POOL_DATASET_PATH = (
    PROJECT_ROOT / "tests" / "fixtures" / "retrieval_eval_review_pool_v2.json"
)
CORPUS_PATH = PROJECT_ROOT / "tests" / "fixtures" / "retrieval_eval_repo"


def run_local_benchmark(
    *, base_url: str, model_id: str, api_model: str, dimension: int = 1024,
    dataset_path: Path = DATASET_PATH,
) -> dict:
    with dataset_path.open("r", encoding="utf-8") as stream:
        dataset = json.load(stream)
    provider = LlamaServerEmbeddingProvider(
        base_url, model_id=model_id, api_model=api_model, dimension=dimension,
    )
    with IndexStore(":memory:") as store:
        index_repository(CORPUS_PATH, store)
        first_query = dataset["queries"][0]["query"]
        other_query = dataset["queries"][1]["query"]
        first_chunk = next(store.iter_chunks())
        document_text = first_chunk["text"]

        query_started = perf_counter()
        query_a = embed_text(provider, first_query, role="query")
        query_b = embed_text(provider, first_query, role="query")
        repeated_query_seconds = perf_counter() - query_started
        document_started = perf_counter()
        document_a = embed_text(provider, document_text, role="document")
        document_b = embed_text(provider, document_text, role="document")
        repeated_document_seconds = perf_counter() - document_started
        other_vector = embed_text(provider, other_query, role="query")

        index_started = perf_counter()
        retriever = VectorCandidateRetriever(store, provider)
        index_result = retriever.index_current_index(include_symbols=False)
        index_seconds = perf_counter() - index_started

        query_times = []

        def timed_search(query, *, top_k):
            started = perf_counter()
            results = retriever.search(query, top_k=top_k)
            query_times.append(perf_counter() - started)
            return results

        benchmark = evaluate_benchmark(store, dataset, vector_search=timed_search)
        vector_bytes = store.connection.execute(
            "SELECT COALESCE(SUM(length(vector)), 0) FROM vector_embeddings"
        ).fetchone()[0]
        return {
            "dataset_version": dataset["dataset_version"],
            "query_total": len(dataset["queries"]),
            "model": {
                "model_id": model_id,
                "api_model": api_model,
                "dimension": dimension,
                "query_instruction": DEFAULT_QUERY_INSTRUCTION,
                "document_preprocessing": "raw chunk text; no added instruction",
                "server_endpoint": base_url + "/v1/embeddings",
            },
            "repeatability": {
                "query_cosine": cosine_similarity(query_a, query_b),
                "query_exact_equal": query_a == query_b,
                "document_cosine": cosine_similarity(document_a, document_b),
                "document_exact_equal": document_a == document_b,
                "different_queries_cosine": cosine_similarity(query_a, other_vector),
                "query_norm": sqrt(sum(value * value for value in query_a)),
                "document_norm": sqrt(sum(value * value for value in document_a)),
                "repeated_query_seconds": round(repeated_query_seconds, 6),
                "repeated_document_seconds": round(repeated_document_seconds, 6),
            },
            "vector_index": {
                **index_result,
                "embedding_seconds": round(index_seconds, 6),
                "blob_bytes": vector_bytes,
                "database": "SQLite :memory:",
            },
            "vector_search": {
                "query_count": len(query_times),
                "total_seconds": round(sum(query_times), 6),
                "mean_seconds": round(sum(query_times) / len(query_times), 6) if query_times else None,
            },
            "benchmark": benchmark,
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate an explicitly configured, already-running loopback llama-server embedding API.",
    )
    parser.add_argument("--base-url", required=True, help="Explicit http://loopback-IP:port origin; no service is started.")
    parser.add_argument("--model-id", required=True, help="Stable local model/revision/quantization identity for the vector records.")
    parser.add_argument("--api-model", required=True, help="The local llama-server alias sent in the embeddings request.")
    parser.add_argument("--dimension", type=int, default=1024, help="Expected vector dimension (default: 1024).")
    parser.add_argument(
        "--dataset", choices=("baseline", "expansion", "review-pool"), default="baseline",
        help="Evaluate the frozen baseline, AF-009D expansion, or AF-009E review pool.",
    )
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = run_local_benchmark(
            base_url=args.base_url, model_id=args.model_id,
            api_model=args.api_model, dimension=args.dimension,
            dataset_path={
                "baseline": DATASET_PATH,
                "expansion": CANDIDATE_DATASET_PATH,
                "review-pool": REVIEW_POOL_DATASET_PATH,
            }[args.dataset],
        )
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError) as exc:
        # Provider 错误会有意脱敏（保留状态/类型，不保留响应
        # 正文或凭证），因此应在本地诊断中保留有助于处理问题的细节，
        # 不要把所有错误都简化成异常类型名称。
        print(f"Local embedding evaluation failed ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
