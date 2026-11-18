"""익명 사용자 식별과 사용자별 컬렉션의 공통 서비스."""

import re
from pathlib import Path
from uuid import uuid4

from rag_service.config import load_settings, resolve_path
from rag_service.planner.collections import CollectionConflict, CollectionStore


def new_user_id() -> str:
    return uuid4().hex


def valid_user_id(uid: str) -> bool:
    return isinstance(uid, str) and re.fullmatch(r"[0-9a-fA-F]{32}", uid) is not None


def collection_id_for(uid: str) -> str:
    """사용자 ID와 컬렉션 ID는 동일하며 경로로 사용하지 않는다."""
    if not valid_user_id(uid):
        raise ValueError("사용자 ID 형식을 확인하세요.")
    return uid


def user_db_path() -> Path:
    return resolve_path(load_settings()["paths"]["user_sqlite"])


def load_collection(uid: str) -> dict:
    """읽기만 할 때는 빈 컬렉션 행을 만들지 않는다."""
    cid = collection_id_for(uid)
    store = CollectionStore(user_db_path())
    try:
        return store.get(cid)
    except KeyError:
        return {"id": cid, "sets": [], "adjustments": [], "version": 0}


def save_collection(uid: str, sets: list[tuple[str, int]], expected_version: int | None = None) -> None:
    """DB에 있는 세트만 등록하고 기존 부품 보정은 보존한다."""
    from rag_service.services.catalog import catalog_options

    cid = collection_id_for(uid)
    known = {option.set_num for option in catalog_options()}
    if any(number not in known or type(qty) is not int or qty < 1 for number, qty in sets):
        raise ValueError("목록의 세트와 1 이상의 정수 수량을 입력하세요.")
    store = CollectionStore(user_db_path())
    store.set_sets(cid, sets, expected_version, ensure=True)
