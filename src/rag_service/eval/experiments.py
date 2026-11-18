"""고정 검색 실험의 측정, 단계별 조합과 승자 판정."""

import hashlib
import json
import sqlite3
from collections import Counter
from copy import deepcopy
from itertools import product
from statistics import median
from time import perf_counter
from typing import Any

import yaml
from langchain_core.embeddings import Embeddings

from rag_service.config import resolve_path
from rag_service.eval.common import overrides, safe_name, table
from rag_service.eval.retrieval import evaluate, frozen_terms
from rag_service.rag.retrieve import _meta_card, rank_docs


class MissingQueryEmbedding(ValueError):
    """캐시만 사용하는 실험에서 누락된 질의 벡터."""


class QueryEmbeddings(Embeddings):
    """저장된 질문 벡터만 읽는다. 선택적으로 누락 질문만 API로 캐시한다."""

    def __init__(self, settings: dict, embed_missing: bool = False):
        self.settings = settings
        self.embed_missing = embed_missing
        self.memory: dict[str, list[float]] = {}
        self.api = None

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise ValueError("검색 실험에서는 문서를 임베딩하지 않습니다.")

    def embed_query(self, text: str) -> list[float]:
        if text in self.memory:
            return self.memory[text]
        model = self.settings["llm"]["embedding_model"]
        key = hashlib.sha256((model + "\n" + text).encode("utf-8")).hexdigest()
        path = resolve_path(self.settings["paths"]["embed_cache"])
        row = None
        if path.exists():
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
                row = conn.execute("SELECT vector FROM embeddings WHERE cache_key=?", (key,)).fetchone()
        if row:
            vector = json.loads(row[0])
        elif self.embed_missing:
            from rag_service.llm import get_embeddings
            from rag_service.vectorstore.embed_cache import CachedEmbeddings

            if self.api is None:
                self.api = CachedEmbeddings(
                    get_embeddings(self.settings, check_embedding_ctx_length=False), path, model,
                    self.settings["index"]["embed_batch_size"])
            vector = self.api.embed_query(text)
        else:
            raise MissingQueryEmbedding("질의 벡터 캐시가 없습니다. 키가 있는 환경에서 --embed-missing으로 준비하세요.")
        self.memory[text] = vector
        return vector


def retrieval_stores(client: Any, settings: dict, embed_missing: bool = False) -> tuple:
    from langchain_chroma import Chroma

    embeddings = QueryEmbeddings(settings, embed_missing)
    return tuple(Chroma(client=client, collection_name=settings["collections"][key],
                        embedding_function=embeddings, create_collection_if_not_exists=False)
                 for key in ("items", "docs"))


def contexts(cases: list[dict], meta_store: Any, settings: dict) -> tuple[dict, list[dict]]:
    prepared, used_terms = {}, []
    for case in cases:
        meta = _meta_card(meta_store, case["set_num"])
        if meta is None:
            raise ValueError("문항의 세트 메타 색인이 스냅샷에 없습니다.")
        terms = frozen_terms(case, meta.title, path=resolve_path(settings["evaluation"]["en_terms_cache"]),
                             settings=settings)
        prepared[case["id"]] = (meta, terms)
        used_terms.append({"id": case["id"], "en_terms": terms})
    return prepared, used_terms


def measure(cases: list[dict], docs_store: Any, settings: dict, prepared: dict) -> tuple[list[dict], dict]:
    """워밍업·질의 임베딩은 제외하고 공통 rank_docs의 반복 중앙값을 기록한다."""
    measurements = {}
    repetitions = settings["evaluation"]["repetitions"]
    if repetitions <= 0:
        raise ValueError("측정 반복 수는 양수여야 합니다.")

    def rank(case: dict) -> list:
        meta, terms = prepared[case["id"]]

        def once() -> list:
            return rank_docs(case["question"], case["set_num"], docs_store=docs_store,
                             settings=settings, en_terms=terms, meta=meta, include_content=True)

        once()
        durations = []
        for _ in range(repetitions):
            started = perf_counter()
            selected = once()
            durations.append((perf_counter() - started) * 1000)
        chars = sum(len(row["page_content"]) for row, _ in selected)
        measurements[case["id"]] = {"retrieval_ms": median(durations), "samples_ms": durations,
                                     "returned_count": len(selected), "input_chars": chars}
        return [({k: v for k, v in row.items() if k != "page_content"}, score) for row, score in selected]

    evaluated = deepcopy(settings)
    evaluated["evaluation"]["recall_k"] = sorted(set(
        [*settings["evaluation"]["recall_k"], settings["retrieval"]["top_k"]]))
    rows, summary = evaluate(cases, rank, evaluated)
    for row in rows:
        row.update(measurements[row["id"]])
    summary.update(returned_counts=dict(sorted(Counter(str(row["returned_count"]) for row in rows).items())),
                   median_retrieval_ms=median(row["retrieval_ms"] for row in rows),
                   median_input_chars=median(row["input_chars"] for row in rows),
                   latency_protocol={"warmups_per_case": 1, "repetitions": repetitions,
                                     "embedding": "prewarmed query cache", "scope": "rank_docs"})
    return rows, summary


def decision(result: dict, reference: dict, policy: dict) -> dict:
    """품질·지연·양 언어 회귀 금지 조건을 모두 만족해야 통과한다."""
    current, base = result["summary"], reference["summary"]
    hit_delta = current["hit_questions"] - base["hit_questions"]
    latency = current["median_retrieval_ms"] / base["median_retrieval_ms"] - 1
    primary = base["primary_metric"]
    reasons = []
    if hit_delta < policy["min_hit_gain"]:
        reasons.append(f"적중 증가 {hit_delta:+d} < +{policy['min_hit_gain']}")
    if latency > policy["max_latency_increase"]:
        reasons.append(f"지연 +{latency:.1%}")
    for lang in policy["languages"]:
        if lang not in current["lang"] or lang not in base["lang"]:
            reasons.append(f"{lang} 평가 없음")
        elif current["lang"][lang]["metrics"][primary] < base["lang"][lang]["metrics"][primary]:
            reasons.append(f"{lang} Recall 회귀")
    return {"passed": not reasons, "reasons": reasons, "hit_delta": hit_delta,
            "latency_delta_pct": latency * 100}


def winner(results: list[dict], reference: dict) -> dict:
    eligible = [result for result in results if result["decision"]["passed"]]
    if not eligible:
        return reference

    def key(result: dict) -> tuple:
        cfg = result["settings"]["retrieval"]
        return (-result["summary"]["overall"]["metrics"]["recall@5"],
                cfg["search_type"] != "similarity", not cfg["mmr_apply_caps"],
                cfg["query_mode"] == "dual", cfg["query_mode"] != "question",
                cfg["similarity_fetch_k"] if cfg["search_type"] == "similarity" else cfg["mmr_fetch_k"],
                result["name"])
    return min(eligible, key=key)


def variants(settings: dict, axes: dict) -> list[dict]:
    """YAML의 retrieval 필드 직교 조합을 기존 타입 검증으로 적용한다."""
    if not axes or any(not isinstance(values, list) or not values for values in axes.values()):
        raise ValueError("실험 축은 비어 있지 않은 목록이어야 합니다.")
    return [overrides(settings, [f"retrieval.{key}={json.dumps(value)}" for key, value in zip(axes, values)])
            for values in product(*axes.values())]


def run_grid(settings: dict, grid: dict, run: Any) -> tuple[list[dict], dict]:
    """E1/E2 → 조건 통과 승자(없으면 기준선) → E3 → E4."""
    reference = run("reference", settings)
    reference["decision"] = decision(reference, reference, grid["policy"])
    results = [reference]
    chosen = reference
    selections = {}
    for stages in (("E1", "E2"), ("E3",), ("E4",)):
        stage_results = []
        for stage in stages:
            for index, config in enumerate(variants(chosen["settings"], grid[stage])):
                name = f"{stage.lower()}-{index + 1:02d}"
                try:
                    result = run(name, config)
                    result["decision"] = decision(result, reference, grid["policy"])
                except MissingQueryEmbedding as error:
                    result = dict(name=name, settings=config, rows=[], summary={}, blocked_reason=str(error),
                                  decision={"passed": False, "reasons": [str(error)]})
                result["parent"] = chosen["name"]
                stage_results.append(result)
                results.append(result)
        chosen = winner(stage_results, reference)
        selections["+".join(stages)] = chosen["name"]
    return results, selections


def grid_markdown(results: list[dict], selections: dict) -> str:
    rows = []
    for result in results:
        cfg, summary = result["settings"]["retrieval"], result["summary"]
        if result.get("blocked_reason"):
            rows.append([result["name"], cfg["search_type"],
                         cfg["similarity_fetch_k"] if cfg["search_type"] == "similarity" else cfg["mmr_fetch_k"],
                         cfg["mmr_lambda"] if cfg["search_type"] == "mmr" else "—",
                         cfg["mmr_apply_caps"] if cfg["search_type"] == "mmr" else True,
                         cfg["query_mode"], cfg["top_k"], *["—"] * 18,
                         "미측정: " + result["blocked_reason"]])
            continue
        metrics = summary["overall"]["metrics"]
        compared = result["comparison"]
        judged = result["decision"]
        rows.append([result["name"], cfg["search_type"],
                     cfg["similarity_fetch_k"] if cfg["search_type"] == "similarity" else cfg["mmr_fetch_k"],
                     cfg["mmr_lambda"] if cfg["search_type"] == "mmr" else "—",
                     cfg["mmr_apply_caps"] if cfg["search_type"] == "mmr" else True,
                     cfg["query_mode"], cfg["top_k"],
                     *[f"{metrics[f'recall@{k}']:.4f}" for k in (1, 3, 5)],
                     f"{metrics[f'recall@{cfg['top_k']}']:.4f}", f"{metrics['mrr']:.4f}",
                     f"{metrics['doc_recall@5']:.4f}",
                     *[f"{summary['lang'].get(lang, {}).get('metrics', {}).get('recall@5', 0):.4f}"
                       for lang in ("ko", "en")],
                     *[f"{summary['lang'].get(lang, {}).get('metrics', {}).get('doc_recall@5', 0):.4f}"
                       for lang in ("ko", "en")],
                     json.dumps(summary["returned_counts"], ensure_ascii=False),
                     f"{summary['median_retrieval_ms']:.3f}", summary["median_input_chars"],
                     f"{compared['primary_delta_pp']:+.2f}", compared["hit_question_delta"],
                     compared["improved_questions"], compared["regressed_questions"],
                     f"{judged['latency_delta_pct']:+.1f}%",
                     "통과" if judged["passed"] else "실패: " + "; ".join(judged["reasons"])])
    return table(["조합", "검색", "fetch_k", "λ", "상한", "질의", "k", "R@1", "R@3", "R@5",
                  "R@k", "MRR", "문서 R@5", "한국어 섹션 R@5", "영어 섹션 R@5",
                  "한국어 문서 R@5", "영어 문서 R@5", "반환 개수:문항 수",
                  "검색 중앙값 ms", "외부 본문 문자 중앙값", "Δ %p", "Δ 적중", "개선", "퇴보",
                  "Δ 지연", "판정"], rows) + (
        "\n\n단계별 선택: " + json.dumps(selections, ensure_ascii=False) +
        "\n\n기준선을 포함해 같은 실행에서 워밍업 후 측정한 검색 지연을 비교합니다. "
        "과거 기준선에 지연이 없으면 재측정 기준선을 사용합니다. P1/질의 임베딩 시간은 제외합니다. "
        "통과 조합이 없는 단계는 기준선을 사용하며 기본 settings는 수정하지 않습니다. "
        "미측정 조합이 있으면 단계별 선택은 잠정 결과이며 후속 단계의 부모 조합은 JSON에 기록합니다. "
        "언어는 질문 언어가 아닌 정답 문서 언어입니다. 입력 길이는 선택된 외부 본문 문자 수입니다. "
        "dual MMR은 합집합 후보에 원래 질문 벡터로 MMR을 적용하고 최소 거리를 유지합니다.")


def read_grid(path: str) -> dict:
    grid = yaml.safe_load(resolve_path(path).read_text(encoding="utf-8"))
    safe_name(grid["name"])
    return grid
