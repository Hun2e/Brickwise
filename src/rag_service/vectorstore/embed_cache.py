"""모델명과 원문 해시를 키로 사용하는 SQLite 임베딩 캐시."""

import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from langchain_core.embeddings import Embeddings
from openai import APIConnectionError, APIStatusError

from rag_service.config import load_settings


class CachedEmbeddings(Embeddings):
    """배치 중 중복 텍스트도 한 번만 임베딩한다."""

    def __init__(self, inner: Embeddings, cache_path: Path, model_name: str, batch_size: int,
                 *, settings: dict | None = None):
        if batch_size <= 0:
            raise ValueError("임베딩 배치 크기는 양수여야 합니다.")
        self.inner = inner
        self.cache_path = Path(cache_path)
        self.model_name = model_name
        self.batch_size = batch_size
        cfg = (settings if settings is not None else load_settings())["index"]
        self.max_retries = cfg["embed_max_retries"]
        self.retry_backoff_sec = cfg["embed_retry_backoff_sec"]
        if self.max_retries < 0 or self.retry_backoff_sec < 0:
            raise ValueError("임베딩 재시도 횟수와 대기 시간은 음수일 수 없습니다.")
        self.stats = {"api_calls": 0, "cache_hits": 0, "embedded": 0}
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.cache_path)
        try:
            with conn:
                conn.execute("CREATE TABLE IF NOT EXISTS embeddings (cache_key TEXT PRIMARY KEY, vector TEXT NOT NULL)")
        finally:
            conn.close()

    def _key(self, text: str) -> str:
        return hashlib.sha256((self.model_name + "\n" + text).encode("utf-8")).hexdigest()

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        """SDK 재시도 없이 캐시 미스 배치의 일시 오류만 재시도한다."""
        for attempt in range(self.max_retries + 1):
            self.stats["api_calls"] += 1
            try:
                return self.inner.embed_documents(texts)
            except (APIConnectionError, APIStatusError, ConnectionError, TimeoutError) as exc:
                if isinstance(exc, APIStatusError) and not (
                    exc.status_code == 429 or 500 <= exc.status_code < 600
                ):
                    raise
                if attempt == self.max_retries:
                    raise
                delay = self.retry_backoff_sec * 2 ** attempt
                if isinstance(exc, APIStatusError):
                    retry_after = exc.response.headers.get("Retry-After")
                    if retry_after:
                        try:
                            delay = max(delay, float(retry_after))
                        except ValueError:
                            try:
                                deadline = parsedate_to_datetime(retry_after)
                                delay = max(delay, (deadline - datetime.now(timezone.utc)).total_seconds())
                            except (TypeError, ValueError, OverflowError):
                                pass
                time.sleep(delay)
        raise AssertionError("도달할 수 없는 재시도 상태")

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """캐시 누락분만 배치 호출하고 입력 순서대로 벡터를 반환한다."""
        keys = [self._key(text) for text in texts]
        values: dict[str, list[float]] = {}
        missing: dict[str, str] = {}
        conn = sqlite3.connect(self.cache_path)
        try:
            for key, text in zip(keys, texts):
                if key in values:
                    self.stats["cache_hits"] += 1
                    continue
                if key in missing:
                    continue
                row = conn.execute("SELECT vector FROM embeddings WHERE cache_key = ?", (key,)).fetchone()
                if row:
                    values[key] = json.loads(row[0])
                    self.stats["cache_hits"] += 1
                else:
                    missing[key] = text
            pending = list(missing)
            for start in range(0, len(pending), self.batch_size):
                batch = pending[start:start + self.batch_size]
                vectors = self._embed_batch([missing[key] for key in batch])
                if len(vectors) != len(batch):
                    raise ValueError("임베딩 응답 개수가 입력과 다릅니다.")
                with conn:
                    for key, vector in zip(batch, vectors):
                        if not vector:
                            raise ValueError("임베딩 벡터가 비어 있습니다.")
                        values[key] = [float(value) for value in vector]
                        conn.execute("INSERT OR REPLACE INTO embeddings VALUES (?, ?)",
                                     (key, json.dumps(values[key], allow_nan=False)))
                self.stats["embedded"] += len(batch)
            return [values[key] for key in keys]
        finally:
            conn.close()

    def embed_query(self, text: str) -> list[float]:
        """OpenAI 계열의 문서·질문 공통 임베딩 공간을 사용한다."""
        return self.embed_documents([text])[0]
