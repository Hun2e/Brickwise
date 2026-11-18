"""원본 Chroma를 열지 않고 복제한 뒤 내용 지문을 기록한다."""

import json
import shutil
from contextlib import contextmanager
from copy import deepcopy
from tempfile import TemporaryDirectory
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from rag_service.config import resolve_path
from rag_service.eval.common import digest, safe_name, write_json
from rag_service.vectorstore.paths import persistence_path


def collection_records(collection: Any, *, embeddings: bool = False) -> list[dict]:
    include = ["documents", "metadatas"] + (["embeddings"] if embeddings else [])
    data = collection.get(include=include)
    rows = []
    for index, ident in enumerate(data["ids"]):
        row = {"id": ident, "text": data["documents"][index], "metadata": data["metadatas"][index] or {}}
        if embeddings:
            vector = data["embeddings"][index]
            row["embedding"] = vector.tolist() if hasattr(vector, "tolist") else list(vector)
        rows.append(row)
    return sorted(rows, key=lambda row: row["id"])


def manifest_for(records: dict[str, list[dict]], settings: dict, name: str) -> dict:
    """대역 레코드로도 검증 가능한 매니페스트 생성 함수."""
    collections = {}
    for collection, rows in sorted(records.items()):
        collections[collection] = {
            "chunk_count": len(rows),
            "document_count": len({row["metadata"].get("doc_id", row["id"]) for row in rows}),
            "chunk_config_hash": sorted({row["metadata"]["chunk_config_hash"] for row in rows
                                          if row["metadata"].get("chunk_config_hash")}),
            "content_hash": digest(sorted(rows, key=lambda row: row["id"])),
        }
    return {"schema_version": 1, "name": name, "created_at": datetime.now(timezone.utc).isoformat(),
            "settings_hash": digest(settings), "settings": settings, "collections": collections}


def open_client(path: Path) -> Any:
    import chromadb
    from chromadb.config import Settings

    if not (path / "chroma.sqlite3").is_file():
        raise ValueError("Chroma 스냅샷이 없습니다.")
    return chromadb.PersistentClient(path=persistence_path(path), settings=Settings(anonymized_telemetry=False, _env_file=None))


def snapshot_path(settings: dict, name: str) -> Path:
    return resolve_path(settings["evaluation"]["snapshot_dir"]) / safe_name(name)


def _file_state(path: Path) -> dict:
    return {str(file.relative_to(path)): (file.stat().st_size, file.stat().st_mtime_ns)
            for file in path.rglob("*") if file.is_file()}


def create_snapshot(settings: dict, name: str) -> dict:
    """앱·색인 작업을 정지한 상태에서 호출한다. 복사 중 변경은 거절한다."""
    source = resolve_path(settings["paths"]["chroma_dir"])
    target = snapshot_path(settings, name)
    if target.exists() or not (source / "chroma.sqlite3").is_file() or source in target.parents:
        raise ValueError("원본 또는 새 스냅샷 경로를 확인하세요.")
    before = _file_state(source)
    shutil.copytree(source, target / "chroma")
    if before != _file_state(source):
        raise ValueError("복사 중 색인이 변경되었습니다. 앱을 정지하고 새 이름으로 재시도하세요.")
    client = open_client(target / "chroma")
    records = {name: collection_records(client.get_collection(name), embeddings=True)
               for name in settings["collections"].values()}
    manifest = manifest_for(records, settings, name)
    write_json(target / "manifest.json", manifest)
    return manifest


def load_snapshot(settings: dict, name: str) -> tuple[Any, dict]:
    """평가 전에 레코드·벡터 지문을 대조하고 기존 컬렉션만 연다."""
    path = snapshot_path(settings, name)
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    if manifest["settings"]["llm"]["embedding_model"] != settings["llm"]["embedding_model"]:
        raise ValueError("스냅샷과 질의의 임베딩 모델이 다릅니다.")
    client = open_client(path / "chroma")
    for collection in settings["collections"].values():
        records = collection_records(client.get_collection(collection), embeddings=True)
        if digest(records) != manifest["collections"][collection]["content_hash"]:
            raise ValueError("스냅샷 내용이 생성 시점과 다릅니다.")
    return client, manifest


def close_temporary_clients(root: Path) -> None:
    """임시 경로의 Chroma 시스템만 닫아 Windows 파일 잠금을 해제한다."""
    from chromadb.api.shared_system_client import SharedSystemClient

    systems = SharedSystemClient._identifier_to_system
    for identifier, system in list(systems.items()):
        path = system.settings.persist_directory
        if path and Path(path).resolve().is_relative_to(root.resolve()):
            system.stop()
            systems.pop(identifier, None)


@contextmanager
def temporary_snapshot(settings: dict, name: str) -> Iterator[tuple[Any, dict]]:
    """평가는 복사본에서 검증·검색하고 보관 스냅샷은 열지 않는다."""
    source = snapshot_path(settings, name)
    with TemporaryDirectory(prefix="rag-snapshot-") as directory:
        root = Path(directory)
        config = deepcopy(settings)
        config["evaluation"]["snapshot_dir"] = str(root)
        before = _file_state(source)
        shutil.copytree(source, root / safe_name(name))
        if before != _file_state(source):
            raise ValueError("복사 중 스냅샷이 변경되었습니다. 작업을 정지하고 재시도하세요.")
        try:
            yield load_snapshot(config, name)
        finally:
            close_temporary_clients(root)
