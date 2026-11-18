"""질문 시점 수집을 실행하고 진행 이벤트와 결과 JSON을 출력한다."""

import argparse
import json
import os
import sqlite3
import sys
from itertools import zip_longest
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rag_service.config import load_settings, resolve_path
from rag_service.llm import MissingApiKey, get_chat_model, get_embeddings

if TYPE_CHECKING:
    from rag_service.rag.collect import CollectResult


def format_table(result: "CollectResult") -> str:
    """검색어와 선별 결과를 제목·사유가 있는 세 열로 출력한다."""
    def cell(value: str) -> str:
        return " ".join(value.split()).replace("|", "\\|")

    queries = [query.q for query in result.queries]
    selected = [f"{result.selected_titles.get(ident, ident)} ({ident}) — {reason}"
                for ident, reason in result.selected]
    rejected = [f"{title} ({ident}) — {reason}" for ident, title, reason in result.rejected]
    rows = ["| 검색어 | 선택(제목·사유) | 제외(제목·사유) |", "| --- | --- | --- |"]
    for row in zip_longest(queries or ["없음"], selected or ["없음"], rejected or ["없음"], fillvalue=""):
        rows.append("| " + " | ".join(cell(value) for value in row) + " |")
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set-num", required=True, help="선택 세트 번호")
    parser.add_argument("--question", required=True, help="질문")
    parser.add_argument("--table", action="store_true", help="JSON 대신 검색어·선택·제외 문서 표 출력")
    parser.add_argument("--p1-version", choices=["p1-v1", "p1-v2", "p1-v2.1"], help="P1 비교용 버전")
    args = parser.parse_args(argv)
    client = None
    try:
        settings = load_settings()
        if args.p1_version:
            settings["collection"]["p1_prompt_version"] = args.p1_version
        model = get_chat_model(settings)
        inner = get_embeddings(settings, chunk_size=settings["index"]["embed_batch_size"],
                               check_embedding_ctx_length=False)
        from rag_service.data_prep.collectors.http import HttpClient, build_user_agent
        from rag_service.data_prep.collectors.wikipedia import WikipediaCollector
        from rag_service.data_prep.doc_store import DocStore
        from rag_service.rag.collect import collect_for_question
        from rag_service.vectorstore.embed_cache import CachedEmbeddings
        from rag_service.vectorstore.store import get_docs_collection

        path = resolve_path(settings["paths"]["sqlite"])
        if not path.is_file():
            print("세트 DB가 없습니다. load_rebrickable.py와 build_entities.py를 먼저 실행하세요.")
            return 2
        contact = os.environ.get("WIKI_CONTACT", "").strip() or None
        api, app = settings["external_api"], settings["app"]
        interval = api["request_interval_sec"] if contact else api["interval_without_contact_sec"]
        client = HttpClient(build_user_agent(app["name"], app["version"], contact), interval,
                            api["max_retries"], api["timeout_sec"], api["backoff_base_sec"])
        doc_store = DocStore(resolve_path(settings["paths"]["docs_dir"]),
                             resolve_path(settings["paths"]["collect_sqlite"]))
        embeddings = CachedEmbeddings(inner, resolve_path(settings["paths"]["embed_cache"]),
                                      settings["llm"]["embedding_model"], settings["index"]["embed_batch_size"],
                                      settings=settings)
        docs_store = get_docs_collection(embeddings, settings)

        def event(name: str, data: dict) -> None:
            print(json.dumps({"event": name, **data}, ensure_ascii=False), file=sys.stderr)

        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
            result = collect_for_question(args.set_num, args.question, settings=settings, conn=conn,
                                          model=model, collector=WikipediaCollector(client, settings),
                                          doc_store=doc_store, docs_store=docs_store,
                                          embeddings=embeddings, on_event=event)
        data = asdict(result)
        data["queries"] = [query.model_dump() for query in result.queries]
        print(format_table(result) if args.table else json.dumps(data, ensure_ascii=False))
        return 1 if result.partial else 0
    except MissingApiKey:
        print("GMS_KEY가 설정되지 않았습니다. 환경변수에 키를 설정한 뒤 다시 실행하세요.")
        return 2
    except Exception:
        print("질문 문서 수집 실패: DB·수집 저장소·설정과 연결 상태를 확인하세요.", file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.session.close()


if __name__ == "__main__":
    raise SystemExit(main())
