"""수집된 매핑·시리즈 문서를 색인한다. 사전 수집 필수, GMS_KEY는 환경변수로 설정."""

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rag_service.config import load_settings, resolve_path
from rag_service.llm import MissingApiKey, get_embeddings


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    try:
        settings = load_settings()
        inner = get_embeddings(settings, chunk_size=settings["index"]["embed_batch_size"],
                               check_embedding_ctx_length=False)
        from rag_service.data_prep.doc_store import DocStore
        from rag_service.data_prep.prefetch import LocalDocuments, mapped_targets
        from rag_service.vectorstore.doc_index import DocWithScope, doc_sets, question_documents, sync_set_docs
        from rag_service.vectorstore.embed_cache import CachedEmbeddings
        from rag_service.vectorstore.store import get_docs_collection

        path = resolve_path(settings["paths"]["sqlite"])
        if not path.is_file():
            print("세트 DB가 없습니다. load_rebrickable.py와 build_entities.py를 먼저 실행하세요.")
            return 2
        doc_store = DocStore(resolve_path(settings["paths"]["docs_dir"]),
                             resolve_path(settings["paths"]["collect_sqlite"]))
        local = LocalDocuments(doc_store)
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
            targets = mapped_targets(conn, settings, local)
            docs = [DocWithScope(local.get(target.lang, target.title), target.scope,
                                 target.entity, target.entity_kind) for target in targets]
            docs = question_documents(doc_store, docs)
            sets = doc_sets(conn, doc_store)
        llm = settings["llm"]
        embeddings = CachedEmbeddings(inner, resolve_path(settings["paths"]["embed_cache"]),
                                      llm["embedding_model"], settings["index"]["embed_batch_size"])
        store = get_docs_collection(embeddings, settings)
        print(json.dumps(sync_set_docs(store, docs, sets, settings["chunking"]), ensure_ascii=False))
        return 0
    except MissingApiKey:
        print("GMS_KEY가 설정되지 않았습니다. 환경변수에 키를 설정한 뒤 다시 실행하세요.")
        return 2
    except Exception:
        print("문서 색인 실패: DB·기체 매핑·prefetch_docs.py --mapped 완료 여부와 설정을 확인하세요.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
