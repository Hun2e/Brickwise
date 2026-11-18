"""같은 질문·검색 스냅샷으로 P3 v1/v2를 비교한다. 실제 실행은 GMS를 호출한다."""

import argparse
import json
import sqlite3
import sys
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

if TYPE_CHECKING:
    from rag_service.rag.evidence import Card
    from rag_service.rag.graph import RagDeps
    from rag_service.services.orchestrator import OrchestratorDeps


def compare_case(case: dict, cards: list["Card"], *, model: Any, conn: sqlite3.Connection, settings: dict,
                 selected_set: dict | None = None, tables: list[dict] | None = None) -> dict:
    """검색은 호출하지 않는다. 두 버전에 동일한 카드·이력·표 문맥을 전달한다."""
    from rag_service.rag.citations import NO_CITATION_MESSAGE, filter_cited, parse_citations
    from rag_service.rag.generate import generate
    from rag_service.rag.graph import RagDeps
    from rag_service.rag.prompts import p3_context
    from rag_service.rag.verify import split_sentences, verify_answer

    tables = tables or []
    output = {"id": case["id"], "question": case["question"], "history": case.get("history", []),
              "selected_set": selected_set or {}, "tables": tables,
              "cards": [asdict(card) for card in cards], "versions": {}}
    for version in ("p3-v1", "p3-v2"):
        config = deepcopy(settings)
        config["retrieval"]["p3_prompt_version"] = version
        deps = RagDeps(settings=config, model=model)
        state = {"question": case["question"], "cards": list(cards), "history": case.get("history", [])}
        started = perf_counter()
        with p3_context(selected_set=output["selected_set"],
                        table_titles=[table["title"] for table in tables], settings=config):
            generated = generate(state, deps)
        elapsed_ms = round((perf_counter() - started) * 1000)
        context_numbers = [number for number in (output["selected_set"].get("set_num"), case.get("selected_set"))
                           if number]
        verified = verify_answer(generated["answer"], cards, case["question"], conn,
                                 context_set_nums=context_numbers, settings=config)
        used, no_citation, warnings = filter_cited(verified.answer, cards)
        output["versions"][version] = {
            "answer": generated["answer"], "verified_answer": verified.answer,
            "sentence_count": len(split_sentences(generated["answer"])),
            "citation_count": len(parse_citations(generated["answer"])),
            "removed_count": len(verified.removed), "checked": verified.checked,
            "violations": verified.removed, "items": verified.items,
            "elapsed_ms": elapsed_ms, "usage": generated["usage"],
            "used_cards": [asdict(card) for card in used], "no_citation": no_citation,
            "warnings": generated["warnings"] + verified.warnings + warnings
                        + ([NO_CITATION_MESSAGE] if no_citation else []),
        }
    return output


def prepare_case(case: dict, *, rag_deps: "RagDeps",
                 orchestrator_deps: "OrchestratorDeps") -> tuple[list["Card"], dict, list[dict]]:
    """Q 문항은 RAG, S 문항은 실제 경로를 거쳐 검색을 딱 한 번 수행한다."""
    from rag_service.rag.retrieve import retrieve
    from rag_service.services.orchestrator import answer

    captured = {}

    def capture(number: str, question: str, *, history=None, injected_cards=None, on_event=None, deps=None) -> dict:
        state = {"set_num": number, "question": question, "history": history or [],
                 "injected_cards": injected_cards or []}
        result = retrieve(state, rag_deps, on_event)
        row = orchestrator_deps.conn.execute("SELECT name FROM sets WHERE set_num=?", (number,)).fetchone()
        captured.update(cards=result["cards"], selected_set={"set_num": number, "name": row[0] if row else ""})
        return {"answer": "", "cards": result["cards"]}

    if case.get("route", "narrative") == "narrative":
        capture(case["selected_set"], case["question"], history=case.get("history"))
        tables = []
    else:
        from dataclasses import replace

        deps = replace(orchestrator_deps, ask_fn=capture, rag_deps=rag_deps,
                       collection={"sets": case.get("collection_sets", []), "adjustments": []})
        result = answer(case["question"], selected_set=case.get("selected_set"),
                        history=case.get("history"), deps=deps)
        if result.needs_input or not captured:
            raise ValueError("비교용 근거 준비에 실패했습니다.")
        tables = result.tables
    return captured["cards"], captured["selected_set"], tables


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", type=Path, default=Path(__file__).resolve().parents[1] / "eval/p3_questions.jsonl",
                        help="질문 JSONL (기본: 대표 13문항)")
    parser.add_argument("--prepared", type=Path, help="이전 출력 JSON의 근거 스냅샷 재사용 (검색·수집 생략)")
    parser.add_argument("--output", type=Path, help="결과 JSON 저장 경로 (생략하면 stdout)")
    args = parser.parse_args(argv)
    # --help는 설정·DB·모델을 초기화하기 전에 종료한다.
    from rag_service.config import load_settings
    from rag_service.llm import MissingApiKey, get_chat_model
    from rag_service.rag.evidence import Card
    from rag_service.rag.graph import default_deps as rag_default_deps
    from rag_service.services.orchestrator import default_deps, log_exception

    try:
        settings = load_settings()
        cases = [json.loads(line) for line in args.questions.read_text(encoding="utf-8").splitlines() if line.strip()]
        results = []
        with default_deps() as deps:
            if args.prepared:
                snapshots = {row["id"]: row for row in json.loads(args.prepared.read_text(encoding="utf-8"))["results"]}
                model = get_chat_model(settings)
                for case in cases:
                    snapshot = snapshots[case["id"]]
                    if snapshot["question"] != case["question"] or snapshot["history"] != case.get("history", []):
                        raise ValueError("스냅샷 질문·이력이 다릅니다.")
                    results.append(compare_case(case, [Card(**row) for row in snapshot["cards"]], model=model,
                                                conn=deps.conn, settings=settings,
                                                selected_set=snapshot["selected_set"], tables=snapshot["tables"]))
            else:
                with rag_default_deps() as rag_deps:
                    for case in cases:
                        cards, selected, tables = prepare_case(case, rag_deps=rag_deps, orchestrator_deps=deps)
                        results.append(compare_case(case, cards, model=rag_deps.model, conn=deps.conn,
                                                    settings=settings, selected_set=selected, tables=tables))
        output = json.dumps({"results": results}, ensure_ascii=False, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(output + "\n", encoding="utf-8")
        else:
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8")
            print(output)
        return 0
    except MissingApiKey:
        print("API 키가 설정되지 않았습니다. 실행 환경에 키를 설정하세요.", file=sys.stderr)
    except Exception as exc:
        log_exception(exc)
        print("P3 비교에 실패했습니다. 입력 파일·DB·색인·설정과 연결 상태를 확인하세요.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
