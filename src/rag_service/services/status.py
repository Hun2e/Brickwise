"""외부 API 호출 없이 준비 상태와 사용자용 조치만 반환한다."""

import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field

from rag_service.config import load_settings, resolve_path
from rag_service.env import load_env
from rag_service.services.conversations import ConversationRepo
from rag_service.services.users import new_user_id
from rag_service.services.migrations import MigrationError, migration_needed
from rag_service.vectorstore.paths import persistence_path


@dataclass
class Status:
    db_ok: bool = False
    set_count: int = 0
    meta_chunks: int = 0
    doc_chunks: int = 0
    api_key_set: bool = False
    user_db_ok: bool = False
    messages: list[str] = field(default_factory=list)
    can_chat: bool = False
    migration_required: bool = False


def _index_counts(settings: dict) -> tuple[int, int]:
    path = resolve_path(settings["paths"]["chroma_dir"])
    if not (path / "chroma.sqlite3").is_file():
        return 0, 0
    import chromadb
    from chromadb.config import Settings
    from chromadb.errors import NotFoundError

    client = chromadb.PersistentClient(persistence_path(path), settings=Settings(anonymized_telemetry=False, _env_file=None))
    counts = []
    for kind in ("items", "docs"):
        try:
            counts.append(client.get_collection(settings["collections"][kind], embedding_function=None).count())
        except NotFoundError:
            counts.append(0)
    return tuple(counts)


def _user_db_writable(settings: dict) -> bool:
    path = resolve_path(settings["paths"]["user_sqlite"])
    if not path.exists():
        ConversationRepo(path)
    with closing(sqlite3.connect(path)) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO collections(id) VALUES (?)", (new_user_id(),))
        finally:
            conn.rollback()
    return True


def check_status(settings: dict | None = None) -> Status:
    settings = settings if settings is not None else load_settings()
    status = Status()
    try:
        path = resolve_path(settings["paths"]["sqlite"])
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
            status.set_count = conn.execute(
                "SELECT COUNT(*) FROM sets s JOIN set_scope sc ON sc.set_num=s.set_num"
            ).fetchone()[0]
        status.db_ok = True
        if not status.set_count:
            status.messages.append("세트 DB가 비어 있습니다. 데이터를 적재하세요.")
    except Exception:
        status.messages.append("세트 DB를 읽을 수 없습니다. 데이터 적재와 DB 경로를 확인하세요.")
    try:
        status.meta_chunks, status.doc_chunks = _index_counts(settings)
        if not status.meta_chunks:
            status.messages.append("항목 색인이 0건입니다. 항목 색인 스크립트를 실행하세요.")
        if not status.doc_chunks:
            status.messages.append("외부 문서 색인이 0건입니다. 질문 시 수집하거나 문서를 미리 수집·색인하세요.")
    except Exception:
        status.messages.append("색인을 읽을 수 없습니다. 색인 경로와 저장소 상태를 확인하세요.")
    try:
        load_env()
        status.api_key_set = bool(os.environ.get("GMS_KEY", "").strip())
    except Exception:
        pass
    if not status.api_key_set:
        status.messages.append("API 키가 설정되지 않았습니다. 실행 환경에 GMS_KEY를 설정하세요.")
    try:
        user_path = resolve_path(settings["paths"]["user_sqlite"])
        # 구 UI는 계속 사용 가능하며 상태 점검이 자동 이관을 실행하지 않는다.
        if user_path.exists():
            status.migration_required = migration_needed(user_path)
            if status.migration_required:
                status.messages.append("대화 저장소 이관이 필요합니다. 백업 후 migrate(path)를 실행하세요.")
        status.user_db_ok = _user_db_writable(settings)
    except MigrationError:
        status.messages.append("대화 저장소 이관 상태 확인에 실패했습니다. DB와 백업을 확인하세요.")
    except Exception:
        status.messages.append("대화 저장소에 쓸 수 없습니다. 사용자 DB 경로와 쓰기 권한을 확인하세요.")
    status.can_chat = bool(status.db_ok and status.set_count and status.meta_chunks
                           and status.api_key_set and status.user_db_ok)
    return status


def ui_settings() -> dict:
    """화면은 서비스가 제공하는 표시 설정만 사용한다."""
    settings = load_settings()
    return {**settings["ui"], "disclaimer": settings["app"]["disclaimer"],
            "risky_domain": settings["app"]["risky_domain"]}
