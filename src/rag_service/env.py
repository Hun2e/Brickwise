"""명시적으로 요청할 때만 프로젝트 환경을 한 번 로드한다."""

from functools import lru_cache

from dotenv import load_dotenv

from rag_service.config import project_root


@lru_cache(maxsize=1)
def load_env() -> None:
    """기존 환경변수를 우선하며 import 시에는 파일을 열지 않는다."""
    load_dotenv(project_root() / ".env", override=False)
