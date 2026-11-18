"""성공·실패를 구분한 warm/cold 경로별 지연 요약."""

import math
from collections import defaultdict
from statistics import median
from time import perf_counter
from typing import Callable

from rag_service.eval.common import table


def percentile(values: list[float], quantile: float) -> float:
    """선형 보간 백분위수. 소표본 p90의 계산법을 고정한다."""
    if not values or not 0 <= quantile <= 1:
        raise ValueError("백분위수 입력을 확인하세요.")
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower, upper = math.floor(position), math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def representatives(cases: list[dict], collection_sets: list | None = None) -> list[dict]:
    """경로별 첫 문항을 고르고 누락된 보유 세트는 평가 설정으로 채운다."""
    selected = {}
    for case in cases:
        if case["expected_route"] in selected:
            continue
        case = dict(case)
        if case["has_collection"] or case["expected_route"] in {"build", "build_archive"}:
            case["collection_sets"] = case.get("collection_sets") or collection_sets
            if not case["collection_sets"]:
                raise ValueError("대표 문항에 collection_sets 또는 evaluation.latency_collection을 지정하세요.")
        selected.setdefault(case["expected_route"], case)
    return [selected[key] for key in sorted(selected)]


def measure(cases: list[dict], run: Callable[[dict], object], *, reps: int, condition: str,
            cache_enabled: bool) -> list[dict]:
    if reps <= 0 or condition not in {"warm", "cold"}:
        raise ValueError("반복 수·측정 조건을 확인하세요.")
    rows = []
    for case in cases:
        # warm은 준비 호출을 표본에 섞지 않는다.
        if condition == "warm":
            run(case)
        for repetition in range(1, reps + 1):
            started = perf_counter()
            response = run(case)
            elapsed = (perf_counter() - started) * 1000
            rows.append({"id": case["id"], "question": case["question"], "condition": condition,
                         "cache_enabled": cache_enabled, "repetition": repetition,
                         "expected_route": case["expected_route"], "route": response.route,
                         "success": not response.needs_input and bool(response.answer)
                                    and response.route == case["expected_route"],
                         "needs_input": response.needs_input, "warnings": response.warnings,
                         "elapsed_ms": elapsed, "timings": response.timings, "usage": response.usage,
                         "collection": dict(response.usage.get("collection", {}))})
    return rows


def summarize_latency(rows: list[dict]) -> dict:
    groups = defaultdict(list)
    for row in rows:
        groups[(row["condition"], row["expected_route"])].append(row)
    result = {}
    for (condition, route), group in sorted(groups.items()):
        good = [row for row in group if row["success"]]
        stages = sorted({stage for row in good for stage in row["timings"]})
        def stats(values: list[float]) -> dict:
            return {"count": len(values), "median_ms": median(values) if values else None,
                    "p90_ms": percentile(values, .9) if values else None}
        result[f"{condition}:{route}"] = {"samples": len(group), "failures": len(group) - len(good),
            "collection": {key: sum(row.get("collection", {}).get(key, 0) for row in group)
                           for key in ("calls", "searched", "skipped_queries", "failures", "partial")},
            **stats([row["elapsed_ms"] for row in good]),
            "timings": {stage: stats([row["timings"][stage] for row in good if stage in row["timings"]])
                        for stage in stages}}
    return result


def markdown(summary: dict, mode: str = "live") -> str:
    def ms(value: float | None) -> int | None:
        return round(value) if value is not None else None

    rows, stages, collected = [], [], []
    for key, group in summary.items():
        rows.append([key, group["count"], group["failures"], ms(group["median_ms"]), ms(group["p90_ms"])])
        stages.extend([key, stage, values["count"], ms(values["median_ms"]), ms(values["p90_ms"])]
                      for stage, values in group["timings"].items())
        collected.append([key, *(group["collection"][field] for field in
                                 ("calls", "searched", "skipped_queries", "failures", "partial"))])
    return (f"측정 모드: {mode}\n\n" + table(["조건:경로", "성공 수", "실패 수", "중앙값 ms", "p90 ms"], rows)
            + "\n\n" + table(["조건:경로", "단계", "표본 수", "중앙값 ms", "p90 ms"], stages)
            + "\n\n" + table(["조건:경로", "수집 호출", "신규 검색", "건너뛴 검색어", "수집 실패", "부분 완료"], collected)
            + "\n\np90은 선형 보간입니다. warm은 준비 호출 1회를 제외합니다. "
            "실패·다른 경로 응답은 성공 지연 통계에서 제외하며 원자료에 보존합니다. "
            + ("운영 색인과 질문 시점 수집을 사용하며 실행 중 색인이 갱신될 수 있습니다. " if mode == "live" else
               "고정 스냅샷에 collect_fn=None으로 실행하므로 외부 수집과 P1/P2 지연은 포함하지 않습니다. ")
            + "cold는 LLM 캐시만 우회하며 수집 매니페스트·문서·임베딩 캐시는 유지합니다. "
            "이미 완료된 검색어는 건너뜁니다. cold에서 P1이 새 검색어를 만들면 신규 위키 요청이 발생할 수 있습니다. "
            "수집 집계는 실패 표본과 재시도도 포함하며 준비 호출은 제외합니다. 검색 수는 수집기가 반환한 완료 검색 수입니다.")
