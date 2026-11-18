"""Rebrickable CSV의 UTC 일일 가드와 제한된 재시도 다운로드."""

import gzip
import shutil
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from rag_service.config import load_settings, resolve_path

TABLES = (
    "themes", "colors", "part_categories", "parts", "part_relationships", "elements",
    "sets", "minifigs", "inventories", "inventory_parts", "inventory_sets",
    "inventory_minifigs",
)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("다운로드 시각에는 시간대가 필요합니다.")
    return value.astimezone(timezone.utc)


def should_download(csv_dir: Path, now: datetime) -> bool:
    """같은 UTC 날짜 및 미래 기록은 차단한다. 잘못된 기록은 명시적으로 실패한다."""
    today = _utc(now).date()
    csv_dir = resolve_path(csv_dir)
    for name in ("DOWNLOADED_AT.txt", "DOWNLOAD_ATTEMPTED_AT.txt"):
        marker = csv_dir / name
        if marker.exists():
            recorded = datetime.fromisoformat(marker.read_text(encoding="utf-8").strip())
            if _utc(recorded).date() >= today:
                return False
    return True


def _retry_delay(value: str | None, interval: float) -> float:
    if value is None:
        return interval
    try:
        delay = float(value)
    except ValueError:
        try:
            delay = (_utc(parsedate_to_datetime(value)) - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return interval
    return max(interval, delay)


def _fetch(request: Request, target: Path, api_cfg: dict, interval: float) -> None:
    for attempt in range(api_cfg["max_retries"] + 1):
        try:
            with urlopen(request, timeout=api_cfg["timeout_sec"]) as response:
                with target.open("wb") as output:
                    shutil.copyfileobj(response, output)
            # HTML 오류 응답과 손상된 gzip을 원본 파일로 확정하지 않는다.
            with gzip.open(target, "rb") as stream:
                while stream.read(64 * 1024):
                    pass
            return
        except HTTPError as error:
            retryable = error.code == 429 or 500 <= error.code < 600
            delay = _retry_delay(error.headers.get("Retry-After"), interval)
            error.close()
            if not retryable or attempt == api_cfg["max_retries"]:
                raise
            time.sleep(delay)


def download_all(
    csv_dir: Path, base_url: str, user_agent: str, interval_sec: float,
) -> dict[str, int]:
    """테이블별 바이트 수 반환. 당일 재호출은 빈 dict, 실패한 시도도 당일 차단."""
    if not user_agent or not user_agent.isascii() or "\n" in user_agent or "\r" in user_agent:
        raise ValueError("User-Agent는 줄바꿈 없는 영문 문자열이어야 합니다.")
    if interval_sec < 0:
        raise ValueError("요청 간격은 0 이상이어야 합니다.")
    csv_dir = resolve_path(csv_dir)
    csv_dir.mkdir(parents=True, exist_ok=True)
    api_cfg = load_settings()["external_api"]
    lock = csv_dir / ".download.lock"
    # 여러 프로세스가 동시에 날짜 검사를 통과하는 것을 막는다.
    lock.touch(exist_ok=False)
    try:
        now = datetime.now(timezone.utc)
        if not should_download(csv_dir, now):
            return {}
        # 실패 후 반복 실행해도 같은 날 원본 전체를 다시 요청하지 않는다.
        (csv_dir / "DOWNLOAD_ATTEMPTED_AT.txt").write_text(now.isoformat(), encoding="utf-8")
        sizes = {}
        with TemporaryDirectory(prefix=".download-", dir=csv_dir) as directory:
            staging = Path(directory)
            for index, table in enumerate(TABLES):
                if index:
                    time.sleep(interval_sec)
                filename = f"{table}.csv.gz"
                request = Request(
                    base_url.rstrip("/") + "/" + filename,
                    headers={"User-Agent": user_agent},
                )
                _fetch(request, staging / filename, api_cfg, interval_sec)
                sizes[table] = (staging / filename).stat().st_size
            for table in TABLES:
                (staging / f"{table}.csv.gz").replace(csv_dir / f"{table}.csv.gz")
        (csv_dir / "DOWNLOADED_AT.txt").write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
        return sizes
    finally:
        lock.unlink()
