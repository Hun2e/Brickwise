"""대표 문항의 similarity와 MMR 문서·섹션·거리를 나란히 기록한다."""

import json
import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rag_service.config import resolve_path
from rag_service.eval.common import configured, digest, guarded, parser, table, write_result
from rag_service.eval.experiments import MissingQueryEmbedding, contexts, retrieval_stores
from rag_service.eval.schema import read_cases
from rag_service.eval.snapshot import temporary_snapshot
from rag_service.rag.retrieve import rank_docs


def comparison_markdown(rows: list[dict], configs: dict) -> str:
    text = "설정: " + json.dumps({key: cfg["retrieval"] for key, cfg in configs.items()}, ensure_ascii=False)
    for row in rows:
        values = []
        left, right = row["similarity"], row["mmr"]
        for index in range(max(len(left), len(right))):
            cells = []
            for side in (left, right):
                if index < len(side):
                    meta, distance = side[index]
                    cells.extend([meta["title"], meta["doc_id"], meta["section_path"],
                                  meta["chunk_id"], f"{distance:.6f}"])
                else:
                    cells.extend(["—"] * 5)
            values.append([index + 1, *cells])
        text += f"\n\n## {row['id']} · {row['question']}\n\n"
        text += table(["순위", *[f"{mode} {field}" for mode in ("similarity", "MMR")
                                  for field in ("제목", "문서", "섹션", "청크", "거리")]], values)
        text += f"\n\n공통 청크 {row['common_chunks']}개, 순위 포함 동일: {row['same_ranking']}."
    return text + ("\n\n상한 뒤 빈자리를 추가 청크로 채우지 않습니다. 거리는 컬렉션의 거리 척도이며 "
                   "MMR 목적함수 점수가 아닙니다. MMR은 Chroma가 선택한 집합의 후보 순서를 유지합니다.")


def main(argv: list[str] | None = None) -> int:
    cli = parser(__doc__)
    cli.add_argument("--snapshot")
    cli.add_argument("--ids", required=True)
    cli.add_argument("--file")
    cli.add_argument("--name", default="t017-compare-mmr")
    cli.add_argument("--embed-missing", action="store_true")
    args = cli.parse_args(argv)

    def run() -> int:
        settings = configured(args)
        cfg = settings["evaluation"]
        all_cases = read_cases(resolve_path(args.file or cfg["retrieval_file"]), "retrieval")
        by_id = {case["id"]: case for case in all_cases}
        ids = [value.strip() for value in args.ids.split(",")]
        if len(set(ids)) != len(ids) or any(value not in by_id for value in ids):
            raise ValueError("비교 문항 ID를 확인하세요.")
        cases = [by_id[value] for value in ids]
        with temporary_snapshot(settings, args.snapshot or cfg["snapshot_name"]) as (client, manifest):
            meta_store, docs_store = retrieval_stores(client, settings, args.embed_missing)
            prepared, terms = contexts(cases, meta_store, settings)
            configs = {mode: deepcopy(settings) for mode in ("similarity", "mmr")}
            for mode, config in configs.items():
                config["retrieval"].update(search_type=mode, top_k=cfg["primary_k"])
            rows = []
            for case in cases:
                row = {"id": case["id"], "question": case["question"], "set_num": case["set_num"]}
                meta, en_terms = prepared[case["id"]]
                for mode, config in configs.items():
                    row[mode] = rank_docs(case["question"], case["set_num"], docs_store=docs_store,
                                          settings=config, meta=meta, en_terms=en_terms)
                left, right = ([hit[0]["chunk_id"] for hit in row[mode]] for mode in configs)
                row.update(common_chunks=len(set(left) & set(right)), same_ranking=left == right)
                rows.append(row)
            path = write_result(args.name, "mmr-comparison", settings, rows,
                                {"questions": len(rows)}, comparison_markdown(rows, configs),
                                snapshot=manifest, dataset_hash=digest(cases), en_terms=terms,
                                compared_settings=configs, llm_calls=0)
            print(f"MMR 비교 {len(rows)}문항: {path.with_suffix('.md')}")
            return 0

    def checked() -> int:
        try:
            return run()
        except MissingQueryEmbedding as error:
            print(str(error), file=sys.stderr)
            return 1

    return guarded(checked)


if __name__ == "__main__":
    raise SystemExit(main())
