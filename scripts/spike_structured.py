"""PM용 GMS 구조화 출력 실험. --help는 키나 .env에 접근하지 않는다."""

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import TextIO

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pydantic import BaseModel, ConfigDict, StrictBool

from rag_service.config import load_settings, resolve_path
from rag_service.llm import MissingApiKey, get_chat_model
from rag_service.rag.prompts import (
    P1_PROMPT_VERSION, ROUTER_PROMPT_VERSION, p1_messages, router_messages,
)
from rag_service.rag.schemas import QueryExtraction, Route, RouterOutput
from rag_service.rag.structured import STRUCTURED_METHODS, invoke_structured


class SpikeQuestion(BaseModel):
    """JSONL 평가 문항 계약."""

    model_config = ConfigDict(extra="forbid", strict=True)
    id: str
    question: str
    expected_route: Route
    set_name: str
    set_num: str
    has_collection: StrictBool


def _positive(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("양의 정수를 입력하세요.")
    return number


def _choices(value: str, allowed: tuple[str, ...]) -> list[str]:
    choices = list(dict.fromkeys(part.strip() for part in value.split(",")))
    if not choices or any(part not in allowed for part in choices):
        raise argparse.ArgumentTypeError("허용 값: " + ",".join(allowed))
    return choices


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--methods", default=list(STRUCTURED_METHODS),
                        type=lambda value: _choices(value, STRUCTURED_METHODS))
    parser.add_argument("--repeat", type=_positive, default=2)
    parser.add_argument("--prompts", default=["router", "p1"],
                        type=lambda value: _choices(value, ("router", "p1")))
    parser.add_argument("--limit", type=_positive, help="평가 문항 수 제한 (재시도는 별도)")
    return parser.parse_args(argv)


def read_questions(path: Path) -> list[SpikeQuestion]:
    """입력 스키마와 중복 문항 ID를 검증한다."""
    rows = [SpikeQuestion.model_validate_json(line)
            for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows or len({row.id for row in rows}) != len(rows):
        raise ValueError("문항이 없거나 ID가 중복되었습니다.")
    return rows


def run_experiment(model: object, questions: list[SpikeQuestion], methods: list[str],
                   repeat: int, prompts: list[str], max_retries: int, stream: TextIO) -> list[dict]:
    """호출별 결과를 즉시 JSONL에 기록해 중단 시에도 측정값을 보존한다."""
    records = []
    for question in questions:
        for prompt in prompts:
            if prompt == "router":
                messages = router_messages(question.question, question.has_collection)
                schema = RouterOutput
                version = ROUTER_PROMPT_VERSION
            else:
                messages = p1_messages(question.set_name, question.set_num, question.question, [])
                schema = QueryExtraction
                version = P1_PROMPT_VERSION
            for method in methods:
                for iteration in range(1, repeat + 1):
                    result = invoke_structured(model, messages, schema, method, max_retries)
                    record = asdict(result)
                    record.update({
                        "id": question.id, "prompt": prompt, "prompt_version": version,
                        "method": method, "actual_method": result.method, "repeat": iteration,
                        "value": result.value.model_dump(mode="json") if result.value else None,
                        "expected_route": question.expected_route,
                    })
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                    stream.flush()
                    records.append(record)
    return records


def summarize(records: list[dict]) -> list[dict]:
    """성공률은 전체 호출 결과 기준, 토큰 평균은 관측된 결과만 사용한다."""
    groups = defaultdict(list)
    for row in records:
        groups[(row["prompt"], row["method"])].append(row)
    summaries = []
    for (prompt, method), rows in groups.items():
        count = len(rows)
        latencies = sorted(row["latency_ms"] for row in rows)
        summary = {
            "prompt": prompt, "method": method, "count": count,
            "success_rate": sum(row["ok"] for row in rows) / count,
            "retry_rate": sum(row["attempts"] > 1 for row in rows) / count,
            "text_extract_rate": sum(row["ok"] and row["actual_method"] == "text_extract"
                                     for row in rows) / count,
            "mean_latency_ms": mean(latencies),
            "p95_latency_ms": latencies[math.ceil(count * 0.95) - 1],
            "temperature_error": any(row["temperature_error"] for row in rows),
            "accuracy": None, "confusions": [],
        }
        for key in ("input_tokens", "output_tokens"):
            values = [row["usage"][key] for row in rows if key in row["usage"]]
            summary[f"mean_{key}"] = mean(values) if values else None
            summary[f"observed_{key}"] = len(values)
        if prompt == "router":
            predicted = [row["value"]["route"] if row["ok"] else "실패" for row in rows]
            summary["accuracy"] = sum(
                actual == row["expected_route"] for actual, row in zip(predicted, rows)
            ) / count
            summary["confusions"] = Counter(
                (row["expected_route"], actual) for actual, row in zip(predicted, rows)
                if actual != row["expected_route"]
            ).most_common(3)
        summaries.append(summary)
    return summaries


def print_summary(records: list[dict]) -> None:
    print("prompt | method | n | 성공률 | 재시도 | text_extract | 평균/p95 ms | 평균 입력/출력 토큰 | Router 정확도 | temperature 오류")
    for row in summarize(records):
        tokens = "/".join("N/A" if row[f"mean_{key}"] is None else f'{row[f"mean_{key}"]:.1f}'
                          for key in ("input_tokens", "output_tokens"))
        accuracy = "N/A" if row["accuracy"] is None else f'{row["accuracy"]:.1%}'
        print(f'{row["prompt"]} | {row["method"]} | {row["count"]} | '
              f'{row["success_rate"]:.1%} | {row["retry_rate"]:.1%} | '
              f'{row["text_extract_rate"]:.1%} | {row["mean_latency_ms"]:.1f}/'
              f'{row["p95_latency_ms"]} | {tokens} | {accuracy} | {row["temperature_error"]}')
        print(f'  토큰 관측 수(입력/출력): {row["observed_input_tokens"]}/{row["observed_output_tokens"]}')
        if row["prompt"] == "router":
            pairs = ", ".join(f"{a} → {b}: {n}" for (a, b), n in row["confusions"])
            print("  혼동 상위 3쌍: " + (pairs or "없음"))
    print("성공률은 JSON 복구 포함, 정확도는 실패 포함. 토큰은 재시도 합계의 관측값 평균입니다.")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        settings = load_settings()
        model = get_chat_model(settings)
        llm = settings["llm"]
        questions = read_questions(resolve_path(llm["spike_questions_path"]))
        if args.limit is not None:
            questions = questions[:args.limit]
        log_dir = resolve_path(llm["spike_logs_dir"])
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        log_path = log_dir / f"spike_structured_{stamp}.jsonl"
        with log_path.open("x", encoding="utf-8") as stream:
            records = run_experiment(model, questions, args.methods, args.repeat,
                                     args.prompts, llm["max_retries"], stream)
        print_summary(records)
        print(f"결과: {log_path}")
        return 0
    except MissingApiKey:
        print("GMS_KEY가 설정되지 않았습니다.")
        return 2
    except Exception as exc:
        print(f"실험을 완료하지 못했습니다 ({type(exc).__name__}). 입력과 설정을 확인하세요.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
