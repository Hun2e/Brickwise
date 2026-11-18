"""메타 색인. 순서: load_rebrickable.py → build_entities.py → index_items.py. GMS_KEY는 환경변수로 설정."""

import argparse
import json
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rag_service.config import load_settings, resolve_path
from rag_service.llm import MissingApiKey, get_embeddings


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    started = time.monotonic()
    try:
        settings = load_settings()
        inner = get_embeddings(
            settings,
            chunk_size=settings["index"]["embed_batch_size"],
            check_embedding_ctx_length=False,
        )

        from rag_service.data_prep.item_text import iter_item_docs
        from rag_service.vectorstore.embed_cache import CachedEmbeddings
        from rag_service.vectorstore.store import get_collection, sync_set_meta

        path = resolve_path(settings["paths"]["sqlite"])
        if not path.is_file():
            print("세트 DB가 없습니다. load_rebrickable.py와 build_entities.py를 먼저 실행하세요.")
            return 2
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
            docs = list(iter_item_docs(conn, settings["index"]["meta_scopes"]))
        llm = settings["llm"]
        embeddings = CachedEmbeddings(inner, resolve_path(settings["paths"]["embed_cache"]),
                                      llm["embedding_model"], settings["index"]["embed_batch_size"])
        store = get_collection(settings["collections"]["items"], embeddings, settings)
        report = sync_set_meta(store, docs)
        report.update(embeddings.stats)
        report["elapsed_sec"] = round(time.monotonic() - started, 2)
        print(json.dumps(report, ensure_ascii=False))
        return 0
    except MissingApiKey:
        print("GMS_KEY가 설정되지 않았습니다. 환경변수에 키를 설정한 뒤 다시 실행하세요.")
        return 2
    except Exception:
        # SDK 예외에 요청 내용이나 인증 정보가 섞일 수 있어 원문을 출력하지 않는다.
        print("색인 실패: DB 적재·기체 매핑·설정과 GMS 연결 상태를 확인하세요.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
