"""컬렉션 생성과 항목 ID 기준 메타 색인 동기화."""

from chromadb.config import Settings
from langchain_chroma import Chroma
from langchain_core.embeddings import Embeddings

from rag_service.config import load_settings, resolve_path
from rag_service.data_prep.item_text import ItemDoc
from rag_service.vectorstore.paths import persistence_path


def get_collection(name: str, embeddings: Embeddings, settings: dict | None = None) -> Chroma:
    """텔레메트리를 끈 로컬 영속 컬렉션을 연다."""
    settings = settings if settings is not None else load_settings()
    return Chroma(collection_name=name, embedding_function=embeddings,
                  persist_directory=persistence_path(resolve_path(settings["paths"]["chroma_dir"])),
                  client_settings=Settings(anonymized_telemetry=False, _env_file=None))


def sync_set_meta(store: Chroma, docs: list[ItemDoc]) -> dict:
    """upsert 성공 후 범위를 벗어난 ID를 삭제한다."""
    wanted = {doc.set_num for doc in docs}
    if len(wanted) != len(docs):
        raise ValueError("항목 ID가 중복되었습니다.")
    previous = store.get(include=["metadatas"])
    existing = set(previous["ids"])
    old_metadata = dict(zip(previous["ids"], previous["metadatas"]))
    # Chroma 서버가 허용하는 배치 크기와 임베딩 API 배치 크기는 별개다.
    batch_size = store._client.get_max_batch_size()
    for start in range(0, len(docs), batch_size):
        batch = docs[start:start + batch_size]
        # Chroma는 메타데이터를 병합하므로 사라진 필드는 명시적으로 제거한다.
        metadata = [{**{key: None for key in (old_metadata.get(doc.set_num) or {})
                        if key not in doc.metadata}, **doc.metadata} for doc in batch]
        store.add_texts(texts=[doc.text for doc in batch],
                        metadatas=metadata,
                        ids=[doc.set_num for doc in batch])
    removed = sorted(existing - wanted)
    for start in range(0, len(removed), batch_size):
        store.delete(ids=removed[start:start + batch_size])
    return {"total": len(store.get(include=[])["ids"]), "upserted": len(docs),
            "new": len(wanted - existing), "updated": len(wanted & existing), "deleted": len(removed)}


def get_docs_collection(embeddings: Embeddings, settings: dict | None = None) -> Chroma:
    """외부 문서 전용 컬렉션을 연다."""
    settings = settings if settings is not None else load_settings()
    return get_collection(settings["collections"]["docs"], embeddings, settings)
