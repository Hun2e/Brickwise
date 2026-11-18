"""문서 1벌 색인 및 전체 매핑을 기준으로 한 연결 세트 동기화."""

import hashlib
import json
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass

from langchain_chroma import Chroma

from rag_service.data_prep.collectors.base import Doc
from rag_service.data_prep.doc_store import DocStore
from .chunking import chunk_doc


@dataclass(frozen=True)
class DocWithScope:
    doc: Doc
    scope: str
    entity: str | None
    entity_kind: str


def doc_sets(conn: sqlite3.Connection, doc_store: DocStore | None = None) -> dict[str, list[str]]:
    """부분 갱신으로 공유 문서의 연결이 유실되지 않게 전체 매핑을 읽는다."""
    result: dict[str, list[str]] = defaultdict(list)
    for title, number in conn.execute("SELECT DISTINCT wiki_title, set_num FROM set_entities ORDER BY wiki_title, set_num"):
        result[title].append(number)
    if doc_store is not None:
        for number, _, title in doc_store.question_links():
            result[title].append(number)
    return {title: sorted(set(numbers)) for title, numbers in result.items()}


def question_documents(doc_store: DocStore,
                       existing: list[DocWithScope] | None = None) -> list[DocWithScope]:
    """매핑 대상에 질문 문서와 번역을 합치며 기존 scope·태그를 보존한다."""
    result = list(existing or [])
    by_id = {item.doc.doc_id: item for item in result}
    seen = {(item.doc.doc_id, item.entity) for item in result}
    for _, doc_id, title in doc_store.question_links():
        if (doc_id, title) in seen:
            continue
        doc = doc_store.get(doc_id)
        if doc is None:
            raise ValueError("질문 연결 문서가 없습니다. 수집 저장소를 확인하세요.")
        original = by_id.get(doc_id)
        kind = "list" if title.startswith("List of") or doc.title.startswith("List of") else "question"
        result.append(DocWithScope(doc, original.scope if original else "entity", title,
                                   original.entity_kind if original else kind))
        seen.add((doc_id, title))
    return result


def _records(docs: list[DocWithScope], sets_by_entity: dict[str, list[str]],
             cfg: dict) -> dict[tuple[str, str], list[tuple[str, str, dict]]]:
    config = {key: cfg[key] for key in ("chunk_size", "chunk_overlap", "min_chunk_chars", "header")}
    digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    label = f"{config['chunk_size']}/{config['chunk_overlap']}/{config['min_chunk_chars']}"
    groups: dict[tuple[str, str], list[DocWithScope]] = defaultdict(list)
    for item in docs:
        if item.scope not in {"entity", "series"} or item.doc.source != "wikipedia":
            raise ValueError("문서 출처와 scope를 확인하세요.")
        if item.scope == "entity" and not item.entity:
            raise ValueError("기체 문서의 영어 엔티티가 필요합니다.")
        groups[(item.scope, item.doc.doc_id)].append(item)
    result = {}
    for key, items in sorted(groups.items()):
        item, doc = items[0], items[0].doc
        if any(other.doc != doc for other in items):
            raise ValueError("같은 문서 ID에 서로 다른 본문이 있습니다.")
        entities = sorted({other.entity for other in items if other.entity})
        linked = sorted({number for entity in entities for number in sets_by_entity.get(entity, [])})
        base = dict(source_type=doc.source, scope=item.scope, doc_id=doc.doc_id, title=doc.title,
                    url=doc.url, license=doc.license, attribution=doc.attribution, lang=doc.lang,
                    revid=doc.revid, entity=entities[0] if entities else "", entity_kind=item.entity_kind,
                    collected_at=doc.fetched_at, chunk_config=label, chunk_config_hash=digest)
        # Chroma 1.5.9는 빈 배열을 거부한다. 빈 연결은 필드 부재로 표현한다.
        if linked:
            base["sets"] = linked
        result[key] = [(f"{item.scope}:{doc.doc_id}:{chunk.seq:04d}", chunk.text,
                        {**base, "section_path": chunk.section_path, "seq": chunk.seq})
                       for chunk in chunk_doc(doc, config["chunk_size"], config["chunk_overlap"],
                                              config["min_chunk_chars"], config["header"])]
    return result


def sync_set_docs(store: Chroma, docs: list[DocWithScope],
                  sets_by_entity: dict[str, list[str]], chunk_cfg: dict, *,
                  prune: bool = True, embeddings=None) -> dict:
    """전체 벡터 준비가 성공한 뒤에만 본문·메타 변경과 삭제를 적용한다."""
    started = time.monotonic()
    embedding = embeddings if embeddings is not None else store._embedding_function
    before = dict(getattr(embedding, "stats", {}))
    wanted = _records(docs, sets_by_entity, chunk_cfg)
    previous = store.get(include=["metadatas", "documents"])
    old: dict[tuple[str, str], dict[str, tuple[str, dict]]] = defaultdict(dict)
    for ident, text, meta in zip(previous["ids"], previous["documents"], previous["metadatas"]):
        old[(meta["scope"], meta["doc_id"])][ident] = (text, meta)
    report = dict(documents=len(wanted), chunks=0, new=0, updated=0, metadata_updated=0,
                  deleted=0, deleted_chunks=0, skipped=0)
    batch_size = store._client.get_max_batch_size()
    metadata_changes = []
    replacements = []

    def delete(ids: list[str]) -> None:
        for start in range(0, len(ids), batch_size):
            store.delete(ids=ids[start:start + batch_size])

    for key, records in wanted.items():
        existing = old.get(key, {})
        unchanged = set(existing) == {record[0] for record in records} and all(
            existing[ident][0] == text and all(existing[ident][1].get(field) == meta[field]
                                             for field in ("revid", "chunk_config_hash"))
            for ident, text, meta in records)
        if unchanged:
            changes = [(ident, {**{field: None for field in existing[ident][1] if field not in meta}, **meta})
                       for ident, _, meta in records if existing[ident][1] != meta]
            metadata_changes.extend(changes)
            report["metadata_updated" if changes else "skipped"] += 1
            continue
        # 뒤 문서에서 실패해도 앞 문서의 본문·메타가 바뀌지 않게 모두 준비한다.
        vectors = embedding.embed_documents([text for _, text, _ in records]) if records else []
        if len(vectors) != len(records):
            raise ValueError("임베딩 응답 개수가 입력과 다릅니다.")
        replacements.append((list(existing), records, vectors))
        report["updated" if existing else "new"] += 1

    for start in range(0, len(metadata_changes), batch_size):
        batch = metadata_changes[start:start + batch_size]
        store._collection.update(ids=[ident for ident, _ in batch],
                                 metadatas=[meta for _, meta in batch])
    for existing_ids, records, vectors in replacements:
        delete(existing_ids)
        for start in range(0, len(records), batch_size):
            batch = records[start:start + batch_size]
            store._collection.upsert(ids=[ident for ident, _, _ in batch],
                                     documents=[text for _, text, _ in batch],
                                     metadatas=[meta for _, _, meta in batch],
                                     embeddings=vectors[start:start + batch_size])
    for key in (old.keys() - wanted.keys()) if prune else ():
        delete(list(old[key]))
        report["deleted"] += 1
        report["deleted_chunks"] += len(old[key])
    report["chunks"] = len(store.get(include=[])["ids"])
    stats = getattr(embedding, "stats", {})
    report.update({key: stats.get(key, 0) - before.get(key, 0) for key in ("api_calls", "cache_hits", "embedded")})
    report["elapsed_sec"] = round(time.monotonic() - started, 3)
    return report
