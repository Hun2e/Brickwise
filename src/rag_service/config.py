"""리포 루트 기준 설정 로드. 비밀 파일은 자동으로 읽지 않는다."""

from pathlib import Path
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

import yaml

_settings_override: ContextVar[dict | None] = ContextVar("settings_override", default=None)


@contextmanager
def settings_context(settings: dict) -> Iterator[None]:
    """평가 호출 안의 기본 설정만 교체하고 별도 사전 파일 로드는 유지한다."""
    token = _settings_override.set(settings)
    try:
        yield
    finally:
        _settings_override.reset(token)


def project_root() -> Path:
    """실행 디렉터리에 무관한 리포 루트를 반환한다."""
    return Path(__file__).resolve().parents[2]


def resolve_path(path: str | Path) -> Path:
    """상대 경로를 리포 루트 기준으로 해석한다."""
    return (project_root() / path).resolve()


def load_settings(path: str | Path = "config/settings.yaml") -> dict:
    """YAML 설정을 읽는다. 환경변수는 소비하는 모듈에서 직접 읽는다."""
    overridden = _settings_override.get()
    if overridden is not None and resolve_path(path) == resolve_path("config/settings.yaml"):
        return overridden
    with resolve_path(path).open(encoding="utf-8") as stream:
        try:
            settings = yaml.safe_load(stream)
        except yaml.YAMLError:
            raise ValueError("설정 파일의 YAML 형식을 확인하세요.") from None
    if not isinstance(settings, dict):
        raise ValueError("설정 파일은 YAML 매핑이어야 합니다.")
    return settings
