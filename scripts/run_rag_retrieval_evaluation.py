"""Deterministic offline retrieval evaluation with opt-in DashScope calibration."""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate shared-guide retrieval fixtures")
    parser.add_argument(
        "--fixture-dir",
        type=Path,
        default=PROJECT_ROOT / "tests" / "fixtures" / "rag" / "v1",
    )
    parser.add_argument("--summary-only", action="store_true")
    parser.add_argument(
        "--live-dashscope",
        action="store_true",
        help="manual calibration only; fixtures are never modified",
    )
    return parser.parse_args(argv)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid fixture file: {path.name}") from error
    if not isinstance(value, dict):
        raise ValueError(f"fixture root must be an object: {path.name}")
    return value


def _load_fixture(fixture_dir: Path):
    manifest = _load_json(fixture_dir / "manifest.json")
    corpus = _load_json(fixture_dir / "corpus.json")
    queries = _load_json(fixture_dir / "queries.json")
    documents = corpus.get("documents")
    query_items = queries.get("queries")
    if not isinstance(documents, list) or not isinstance(query_items, list):
        raise ValueError("fixture documents and queries must be arrays")
    if manifest.get("corpus_count") != len(documents):
        raise ValueError("manifest corpus_count does not match corpus")
    if manifest.get("query_count") != len(query_items):
        raise ValueError("manifest query_count does not match queries")
    if manifest.get("fixture_version") != "rag-retrieval-v1":
        raise ValueError("unsupported RAG fixture version")
    return manifest, documents, query_items


def _dcg(retrieved: Sequence[str], relevant: set[str], *, k: int) -> float:
    return sum(
        1.0 / math.log2(rank + 2)
        for rank, share_id in enumerate(retrieved[:k])
        if share_id in relevant
    )


def _ndcg(retrieved: Sequence[str], relevant: set[str], *, k: int) -> float:
    ideal = sum(1.0 / math.log2(rank + 2) for rank in range(min(k, len(relevant))))
    return _dcg(retrieved, relevant, k=k) / ideal if ideal else 1.0


def _latency_summary(values: Sequence[int | float]) -> dict[str, int | float]:
    normalized = sorted(float(value) for value in values)
    if not normalized:
        return {"min_ms": 0, "max_ms": 0, "mean_ms": 0.0, "p95_ms": 0}
    p95_index = max(0, math.ceil(0.95 * len(normalized)) - 1)

    def compact(value: float) -> int | float:
        return int(value) if value.is_integer() else round(value, 3)

    return {
        "min_ms": compact(normalized[0]),
        "max_ms": compact(normalized[-1]),
        "mean_ms": round(statistics.fmean(normalized), 3),
        "p95_ms": compact(normalized[p95_index]),
    }


def _evaluate_data(
    manifest: Mapping[str, Any],
    documents: Sequence[Mapping[str, Any]],
    queries: Sequence[Mapping[str, Any]],
    *,
    min_score: float,
) -> dict[str, Any]:
    if not math.isfinite(min_score) or not -1.0 <= min_score <= 1.0:
        raise ValueError("RAG_MIN_SCORE must be between -1 and 1")
    top_k = int(manifest["top_k"])
    corpus = {str(item["share_id"]): item for item in documents}
    recall_values: list[float] = []
    ndcg_values: list[float] = []
    same_city_checks: list[bool] = []
    cancelled_hits = 0
    cancelled_total = 0
    latencies: list[int | float] = []
    per_query: list[dict[str, Any]] = []

    for query in queries:
        scores = query.get("scores")
        relevant = {str(value) for value in query.get("expected_relevant_share_ids", [])}
        cancelled = {str(value) for value in query.get("cancelled_share_ids", [])}
        if not isinstance(scores, Mapping) or not relevant:
            raise ValueError("each query requires scores and relevant share IDs")
        ranked: list[tuple[str, float]] = []
        for share_id, raw_score in scores.items():
            document = corpus.get(str(share_id))
            if document is None:
                continue
            score = float(raw_score)
            if not math.isfinite(score) or score < min_score:
                continue
            if document.get("publication_status") != "PUBLIC":
                continue
            if document.get("index_status") != "READY":
                continue
            if document.get("city") != query.get("city"):
                continue
            ranked.append((str(share_id), score))
        ranked.sort(key=lambda item: item[0])
        ranked.sort(key=lambda item: item[1], reverse=True)
        retrieved = [share_id for share_id, _ in ranked[:top_k]]
        recall = len(relevant.intersection(retrieved)) / len(relevant)
        ndcg = _ndcg(retrieved, relevant, k=top_k)
        recall_values.append(recall)
        ndcg_values.append(ndcg)
        cancelled_hits += len(cancelled.intersection(retrieved))
        cancelled_total += len(cancelled)
        for share_id in retrieved:
            document = corpus[share_id]
            same_city_checks.append(
                document.get("city") == query.get("city")
                and document.get("publication_status") == "PUBLIC"
                and document.get("index_status") == "READY"
            )
        latencies.append(query.get("latency_ms", 0))
        per_query.append(
            {
                "query_id": query["query_id"],
                "retrieved_share_ids": retrieved,
                "recall_at_3": round(recall, 6),
                "ndcg_at_3": round(ndcg, 6),
            }
        )

    recall_at_3 = statistics.fmean(recall_values) if recall_values else 0.0
    ndcg_at_3 = statistics.fmean(ndcg_values) if ndcg_values else 0.0
    same_city = (
        sum(1 for value in same_city_checks if value) / len(same_city_checks)
        if same_city_checks
        else 1.0
    )
    cancelled_recall = cancelled_hits / cancelled_total if cancelled_total else 0.0
    thresholds = {
        "min_recall_at_3": float(manifest["min_recall_at_3"]),
        "min_ndcg_at_3": float(manifest["min_ndcg_at_3"]),
    }
    metrics = {
        "recall_at_3": round(recall_at_3, 6),
        "ndcg_at_3": round(ndcg_at_3, 6),
        "same_city_public_correctness": round(same_city, 6),
        "cancelled_recall": round(cancelled_recall, 6),
    }
    passed = (
        metrics["same_city_public_correctness"] == 1.0
        and metrics["cancelled_recall"] == 0.0
        and metrics["recall_at_3"] >= thresholds["min_recall_at_3"]
        and metrics["ndcg_at_3"] >= thresholds["min_ndcg_at_3"]
    )
    return {
        "fixture_version": manifest["fixture_version"],
        "source": manifest["source"],
        "query_count": len(queries),
        "corpus_count": len(documents),
        "rag_min_score": min_score,
        "thresholds": thresholds,
        "metrics": metrics,
        "latency_ms": _latency_summary(latencies),
        "passed": passed,
        "per_query": per_query,
    }


def evaluate_fixture(fixture_dir: Path | str, *, min_score: float) -> dict[str, Any]:
    fixture_path = Path(fixture_dir)
    manifest, documents, queries = _load_fixture(fixture_path)
    return _evaluate_data(
        manifest,
        documents,
        queries,
        min_score=float(min_score),
    )


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return numerator / (left_norm * right_norm) if left_norm and right_norm else 0.0


def _live_scores(
    documents: Sequence[Mapping[str, Any]],
    queries: Sequence[Mapping[str, Any]],
    *,
    api_key: str,
    base_url: str,
) -> tuple[list[dict[str, Any]], float]:
    from app.rag.embedding import DashScopeEmbeddingClient

    model = "qwen3.7-text-embedding"
    dimension = int(os.environ.get("EMBEDDING_DIMENSION", "768"))
    timeout = float(os.environ.get("EMBEDDING_TIMEOUT_SECONDS", "10"))
    attempts = int(os.environ.get("EMBEDDING_MAX_ATTEMPTS", "3"))
    client = DashScopeEmbeddingClient(
        api_key=api_key,
        base_url=base_url,
        model=model,
        dimension=dimension,
        timeout_seconds=timeout,
        max_attempts=attempts,
    )
    document_vectors: dict[str, list[float]] = {}
    for document in documents:
        text = (
            "公开旅行攻略\n"
            f"目的地：{document['city']}\n"
            f"旅行天数：{document['travel_days']}天\n"
            f"主要交通：{document['transportation']}"
        )
        document_vectors[str(document["share_id"])] = client.embed(text)

    calibrated = deepcopy(list(queries))
    relevant_scores: list[float] = []
    nonrelevant_scores: list[float] = []
    for query in calibrated:
        query_text = (
            "旅行攻略检索请求\n"
            f"目的地：{query['city']}\n"
            f"旅行天数：{query['travel_days']}天\n"
            f"主要交通：{query['transportation']}"
        )
        query_vector = client.embed(query_text)
        scores = {
            share_id: _cosine(query_vector, vector)
            for share_id, vector in document_vectors.items()
        }
        query["scores"] = scores
        relevant = set(query["expected_relevant_share_ids"])
        for document in documents:
            if document.get("city") != query.get("city"):
                continue
            score = scores[str(document["share_id"])]
            if document["share_id"] in relevant:
                relevant_scores.append(score)
            elif document.get("publication_status") == "PUBLIC" and document.get("index_status") == "READY":
                nonrelevant_scores.append(score)
    lower = max(nonrelevant_scores, default=-1.0)
    upper = min(relevant_scores, default=1.0)
    recommended = round((lower + upper) / 2.0, 4)
    return calibrated, recommended


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.live_dashscope:
        api_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
        base_url = os.environ.get("DASHSCOPE_BASE_URL", "").strip()
        missing = [
            name
            for name, value in (
                ("DASHSCOPE_API_KEY", api_key),
                ("DASHSCOPE_BASE_URL", base_url),
            )
            if not value
        ]
        if missing:
            print(
                json.dumps(
                    {
                        "passed": False,
                        "error": (
                            f"{', '.join(missing)} is required for "
                            "--live-dashscope"
                        ),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 2
    try:
        from app.core.config import settings

        min_score = float(settings.RAG_MIN_SCORE if settings.RAG_MIN_SCORE is not None else 0.55)
        manifest, documents, queries = _load_fixture(args.fixture_dir)
        live_recommended = None
        if args.live_dashscope:
            queries, live_recommended = _live_scores(
                documents,
                queries,
                api_key=api_key,
                base_url=base_url,
            )
        report = _evaluate_data(
            manifest,
            documents,
            queries,
            min_score=min_score,
        )
        if live_recommended is not None:
            report["live_recommended_threshold"] = live_recommended
            report["live_note"] = "manual recommendation only; fixture files were not modified"
        if args.summary_only:
            report.pop("per_query", None)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0 if report["passed"] else 1
    except Exception as error:
        print(
            json.dumps(
                {"passed": False, "error_class": type(error).__name__},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
