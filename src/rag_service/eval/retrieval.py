"""고정 P1 확장어와 고정 색인에 대한 검색 평가."""

import json
from pathlib import Path
from typing import Any, Callable

from rag_service.eval.common import digest, table, write_jsonl
from rag_service.eval.metrics import label_hit, mrr, recall_at_k, summarize
from rag_service.rag.prompts import p1_messages
from rag_service.rag.schemas import p1_schema
from rag_service.rag.structured import invoke_structured


def frozen_terms(case: dict, title: str, *, path: Path, settings: dict, model: Any = None) -> list[str]:
    """캐시 히트는 LLM 호출을 하지 않고, 실패한 호출은 캐시에 넣지 않는다."""
    version = settings["collection"]["p1_prompt_version"]
    key = {"id": case["id"], "question_hash": digest(case["question"]), "p1_version": version}
    context_hash = digest({"set_num": case["set_num"], "title": title, "llm": {
        k: settings["llm"][k] for k in ("chat_model", "base_url", "temperature", "send_temperature")}})
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.exists() else []
    matches = [row for row in rows if all(row.get(k) == v for k, v in key.items())]
    if len(matches) > 1:
        raise ValueError("P1 캐시에 중복 키가 있습니다.")
    if matches:
        saved = matches[0]
        if saved.get("context_hash") != context_hash or not isinstance(saved.get("en_terms"), list) or not all(
                isinstance(term, str) for term in saved["en_terms"]):
            raise ValueError("P1 캐시 문맥이 다릅니다. 별도 캐시 경로를 지정하세요.")
        return saved["en_terms"]
    if model is None:
        raise ValueError("고정 P1 캐시에 문항이 없습니다. T-016 캐시를 확인하세요.")
    cfg = settings["llm"]
    result = invoke_structured(model, p1_messages(title, case["set_num"], case["question"], [], version),
                               p1_schema(version), cfg["structured_method"], cfg["max_retries"])
    if not result.ok:
        raise ValueError("P1 확장어 생성에 실패했습니다. 캐시하지 않았습니다.")
    # 실제 수집기의 P1 안전 기본값과 동일하게 처리한다.
    terms = result.value.en_terms if result.value.queries and result.value.queries[0].q.strip() else []
    rows.append({**key, "context_hash": context_hash, "set_num": case["set_num"], "en_terms": terms})
    write_jsonl(path, rows)
    return terms


def evaluate(cases: list[dict], rank: Callable[[dict], list[tuple[dict, float]]], settings: dict) -> tuple[list[dict], dict]:
    cfg = settings["evaluation"]
    primary = cfg["primary_k"]
    rows = []
    for case in cases:
        selected = rank(case)
        ranked = [meta for meta, _ in selected]
        metrics = {f"recall@{k}": recall_at_k(ranked, case["relevant"], k) for k in cfg["recall_k"]}
        metrics.update(mrr=mrr(ranked, case["relevant"]),
                       **{f"doc_recall@{primary}": recall_at_k(ranked, case["relevant"], primary, "doc")})
        rows.append({**case, "ranked": [{"metadata": meta, "distance": float(score)} for meta, score in selected],
                     "series_chunks": sum(meta.get("scope") == "series" for meta in ranked),
                     "metrics": metrics,
                     "primary_hit": any(label_hit(meta, case["relevant"]) for meta in ranked[:primary]),
                     "primary_complete": recall_at_k(ranked, case["relevant"], primary) == 1})
    summary = summarize(rows, ["lang", "kind"])
    summary.update(primary_metric=f"recall@{primary}",
                   hit_questions=sum(row["primary_hit"] for row in rows),
                   complete_questions=sum(row["primary_complete"] for row in rows),
                   total_questions=len(rows))
    summary.update(series_chunks=sum(row["series_chunks"] for row in rows),
                   series_heavy_questions=sum(row["series_chunks"] >= 3 for row in rows))
    return rows, summary


def compare_results(rows: list[dict], baseline: dict, manifest: dict, settings: dict) -> dict:
    """동일 문항·정답·스냅샷인 경우에만 주 지표와 문항 수 차이를 낸다."""
    old = baseline["rows"]
    identity = lambda row: {key: row[key] for key in ("id", "question", "set_num", "relevant", "lang", "kind")}
    if ([identity(row) for row in rows] != [identity(row) for row in old]
            or baseline.get("snapshot") != manifest):
        raise ValueError("기준선의 문항·정답·스냅샷이 다릅니다.")
    metric = f"recall@{settings['evaluation']['primary_k']}"
    if baseline["summary"]["primary_metric"] != metric:
        raise ValueError("기준선의 주 지표가 다릅니다.")
    return {"primary_delta_pp": 100 * sum(row["metrics"][metric] - prev["metrics"][metric]
                                          for row, prev in zip(rows, old)) / len(rows),
            "hit_question_delta": sum(row["primary_hit"] for row in rows) - sum(row["primary_hit"] for row in old),
            "improved_questions": sum(row["metrics"][metric] > prev["metrics"][metric] for row, prev in zip(rows, old)),
            "regressed_questions": sum(row["metrics"][metric] < prev["metrics"][metric] for row, prev in zip(rows, old))}


def markdown(summary: dict, comparison: dict | None = None) -> str:
    names = list(summary["overall"]["metrics"])
    values = []
    for dimension in ("overall", "lang", "kind"):
        groups = {"all": summary[dimension]} if dimension == "overall" else summary[dimension]
        for key, group in groups.items():
            values.append([f"{dimension}:{key}", group["count"], *[f"{group['metrics'][name]:.4f}" for name in names]])
    text = table(["구분", "문항 수", *names], values)
    text += (f"\n\n주 지표: {summary['primary_metric']}. 하나 이상 적중: {summary['hit_questions']}/"
             f"{summary['total_questions']}, 모든 정답 적중: {summary['complete_questions']}. "
             "Recall은 정답 라벨 회수율의 문항별 평균이며, 적중 문항 수와 별도로 해석합니다.")
    if comparison:
        text += "\n\n" + table(["기준선 대비", "차이"], [[k, v] for k, v in comparison.items()])
    else:
        text += "\n\n기준선 미지정: 문항 수 차이는 계산하지 않았습니다. --baseline으로 비교하세요."
    text += (f"\n\n시리즈 청크: {summary.get('series_chunks', 0)}개. "
             f"시리즈 청크가 3개 이상인 문항: {summary.get('series_heavy_questions', 0)}개.")
    return text
