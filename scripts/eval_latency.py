"""실사용 또는 선택한 스냅샷에서 warm/cold 경로 지연을 측정한다."""

import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rag_service.config import resolve_path
from rag_service.eval.common import configured, digest, guarded, parser, write_result
from rag_service.eval.latency import markdown, measure, representatives, summarize_latency
from rag_service.eval.runtime import dependencies, temporary_collections
from rag_service.eval.schema import read_cases
from rag_service.eval.snapshot import load_snapshot
from rag_service.llm import get_chat_model
from rag_service.services.orchestrator import answer


def main(argv: list[str] | None = None) -> int:
    cli = parser(__doc__)
    cli.add_argument("--snapshot")
    cli.add_argument("--file")
    cli.add_argument("--reps", type=int)
    cli.add_argument("--condition", choices=["both", "warm", "cold"], default="both")
    cli.add_argument("--name", default="latency-base")
    args = cli.parse_args(argv)

    def run() -> int:
        settings = configured(args)
        get_chat_model(settings)
        cfg = settings["evaluation"]
        reps = args.reps if args.reps is not None else cfg["repetitions"]
        if reps <= 0:
            raise ValueError("반복 수는 양수여야 합니다.")
        cases = representatives(read_cases(resolve_path(args.file or cfg["routing_file"]), "routing"),
                                cfg["latency_collection"])
        mode = "snapshot" if args.snapshot is not None else "live"
        client, manifest = load_snapshot(settings, args.snapshot) if mode == "snapshot" else (None, None)
        conditions = ["warm", "cold"] if args.condition == "both" else [args.condition]
        rows = []
        with temporary_collections(cases) as (collection_store, collection_ids):
            for condition in conditions:
                current = deepcopy(settings)
                current["llm"]["cache_enabled"] = condition == "warm"
                current["paths"]["user_sqlite"] = str(collection_store.path)
                # --override로 warm을 cold처럼 표시하지 않도록 조건과 설정을 함께 검사한다.
                if condition == "warm" and not settings["llm"]["cache_enabled"]:
                    raise ValueError("cache_enabled=false는 --condition cold와 함께 사용하세요.")
                collection = {}

                def observed(result: object | None) -> None:
                    collection["calls"] += 1
                    if result is None:
                        collection["failures"] += 1
                    else:
                        collection["searched"] += len(result.searched)
                        collection["skipped_queries"] += len(result.skipped_queries)
                        collection["partial"] += int(result.partial)

                with dependencies(current, client=client, live=mode == "live", on_collect=observed) as deps:
                    deps.collection_store = collection_store

                    def execute(case: dict) -> object:
                        collection.update(dict.fromkeys(("calls", "searched", "skipped_queries", "failures", "partial"), 0))
                        response = answer(case["question"], selected_set=case["selected_set"], history=case["history"],
                                          collection_id=collection_ids.get(case["id"]), deps=deps)
                        response.usage["collection"] = dict(collection)
                        return response
                    rows.extend(measure(cases, execute, reps=reps, condition=condition,
                                        cache_enabled=current["llm"]["cache_enabled"]))
        summary = summarize_latency(rows)
        path = write_result(args.name, "latency", settings, rows, summary, markdown(summary, mode),
                            snapshot=manifest, cases=cases, dataset_hash=digest(cases), mode=mode,
                            collect_fn="collect_for_question" if mode == "live" else None,
                            user_db="temporary",
                            repetitions=reps, conditions=conditions)
        print(f"지연 측정 {len(rows)}회: {path}")
        return 0

    return guarded(run)


if __name__ == "__main__":
    raise SystemExit(main())
