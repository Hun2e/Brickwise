"""HTTP 입력 검증과 단일 워커의 대화별 잠금."""

from pathlib import Path
from threading import Lock
from typing import Annotated

from fastapi import Header

from rag_service.services import server_settings
from rag_service.services.users import valid_user_id

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVER_SETTINGS = server_settings()


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        self.status_code = status_code
        self.code = code
        self.message = message


def user_id(x_user_id: Annotated[str | None, Header()] = None) -> str:
    """공통 서비스와 동일한 UUID hex 형식과 대소문자를 보존한다."""
    if not valid_user_id(x_user_id):
        raise ApiError(400, "invalid_user_id", "사용자 ID가 없거나 올바르지 않습니다. 사용자 ID를 발급받아 다시 요청하세요.")
    return x_user_id


class TurnLocks:
    """스트림 수명이 아니라 서비스 작업 수명 동안 잠근다."""

    def __init__(self) -> None:
        self._guard = Lock()
        self._busy: set[tuple[str, str]] = set()

    def acquire(self, uid: str, thread: str) -> None:
        with self._guard:
            key = (uid, thread)
            if key in self._busy:
                raise ApiError(409, "busy", "이전 질문을 처리 중입니다. 답변이 끝난 뒤 다시 질문하세요.")
            self._busy.add(key)

    def release(self, uid: str, thread: str) -> None:
        with self._guard:
            self._busy.discard((uid, thread))
