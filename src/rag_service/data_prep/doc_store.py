"""문서 JSON과 독립 SQLite 수집 이력 저장소."""

import json
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .collectors.base import Collector, Doc, Section


class DocStore:
    def __init__(self, docs_dir: Path, collect_sqlite: Path):
        self.docs_dir = Path(docs_dir)
        self.collect_sqlite = Path(collect_sqlite)
        self.docs_dir.mkdir(parents=True, exist_ok=True)
        self.collect_sqlite.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.collect_sqlite)) as conn, conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS documents (
                    doc_id TEXT PRIMARY KEY, source TEXT NOT NULL, lang TEXT NOT NULL,
                    page_id INTEGER NOT NULL, UNIQUE(source, lang, page_id)
                );
                CREATE TABLE IF NOT EXISTS fetch_log (
                    id INTEGER PRIMARY KEY, source TEXT NOT NULL, query TEXT,
                    doc_id TEXT, revid INTEGER, status TEXT NOT NULL
                    CHECK(status IN ('fetched','cached','missing','error')), fetched_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS fetch_query ON fetch_log(source, query, status);
                CREATE TABLE IF NOT EXISTS language_links (
                    doc_id TEXT NOT NULL, revid INTEGER NOT NULL, target_lang TEXT NOT NULL,
                    title TEXT, PRIMARY KEY(doc_id, revid, target_lang)
                );
                CREATE TABLE IF NOT EXISTS question_links (
                    set_num TEXT NOT NULL, doc_id TEXT NOT NULL, wiki_title TEXT NOT NULL,
                    lang TEXT NOT NULL, query TEXT NOT NULL, selected_reason TEXT NOT NULL,
                    created_at TEXT NOT NULL, PRIMARY KEY(set_num, doc_id)
                );
                CREATE TABLE IF NOT EXISTS llm_cache (
                    key TEXT PRIMARY KEY, prompt_version TEXT NOT NULL,
                    response_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
            """)

    def link_question_doc(self, set_num: str, doc_id: str, wiki_title: str,
                          lang: str, query: str, reason: str) -> None:
        """한국어 번역도 원문과 같은 wiki_title로 연결한다."""
        with closing(sqlite3.connect(self.collect_sqlite)) as conn, conn:
            conn.execute("INSERT INTO question_links VALUES (?, ?, ?, ?, ?, ?, ?) "
                         "ON CONFLICT(set_num, doc_id) DO UPDATE SET "
                         "wiki_title=excluded.wiki_title, lang=excluded.lang, "
                         "query=excluded.query, selected_reason=excluded.selected_reason",
                         (set_num, doc_id, wiki_title, lang, query, reason,
                          datetime.now(timezone.utc).isoformat()))

    def question_links(self) -> list[tuple[str, str, str]]:
        """전체 연결을 읽어 색인 동기화 때 다른 세트 연결도 보존한다."""
        with closing(sqlite3.connect(self.collect_sqlite)) as conn:
            return conn.execute("SELECT set_num, doc_id, wiki_title FROM question_links "
                                "ORDER BY set_num, doc_id").fetchall()

    def get_llm_cache(self, key: str, prompt_version: str) -> str | None:
        """프롬프트 버전과 입력이 같은 응답만 재사용한다."""
        with closing(sqlite3.connect(self.collect_sqlite)) as conn:
            row = conn.execute("SELECT response_json FROM llm_cache WHERE key=? AND prompt_version=?",
                               (key, prompt_version)).fetchone()
        return row[0] if row else None

    def save_llm_cache(self, key: str, prompt_version: str, response_json: str) -> None:
        with closing(sqlite3.connect(self.collect_sqlite)) as conn, conn:
            conn.execute("INSERT OR REPLACE INTO llm_cache VALUES (?, ?, ?, ?)",
                         (key, prompt_version, response_json, datetime.now(timezone.utc).isoformat()))

    def _path(self, source: str, lang: str, page_id: int) -> Path:
        if (not all(re.fullmatch(r"[A-Za-z0-9_-]+", value) for value in (source, lang))
                or not isinstance(page_id, int) or page_id < 0):
            raise ValueError("문서 출처·언어·ID 형식을 확인하세요.")
        return self.docs_dir / source / lang / f"{page_id}.json"

    def get(self, doc_id: str) -> Doc | None:
        """등록된 문서를 역직렬화한다. 파일이 없으면 캐시 미스로 취급한다."""
        with closing(sqlite3.connect(self.collect_sqlite)) as conn:
            row = conn.execute("SELECT source, lang, page_id FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
        if row is None:
            return None
        try:
            data = json.loads(self._path(*row).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        data["sections"] = [Section(**section) for section in data["sections"]]
        return Doc(**data)

    def find(self, source: str, lang: str, page_id: int) -> Doc | None:
        """수집기별 doc_id 표현에 의존하지 않고 저장본을 찾는다."""
        with closing(sqlite3.connect(self.collect_sqlite)) as conn:
            row = conn.execute("SELECT doc_id FROM documents WHERE source=? AND lang=? AND page_id=?",
                               (source, lang, page_id)).fetchone()
        return self.get(row[0]) if row else None

    def save(self, doc: Doc) -> Path:
        """같은 디렉터리의 임시 파일을 원자적으로 교체한 후 목록을 갱신한다."""
        path = self._path(doc.source, doc.lang, doc.page_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             suffix=".tmp", delete=False) as stream:
                temp_path = Path(stream.name)
                json.dump(asdict(doc), stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, path)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        with closing(sqlite3.connect(self.collect_sqlite)) as conn, conn:
            conn.execute("INSERT INTO documents VALUES (?, ?, ?, ?) ON CONFLICT(doc_id) DO UPDATE SET "
                         "source=excluded.source, lang=excluded.lang, page_id=excluded.page_id",
                         (doc.doc_id, doc.source, doc.lang, doc.page_id))
        return path

    def stored_revid(self, doc_id: str) -> int | None:
        doc = self.get(doc_id)
        return doc.revid if doc else None

    def log_fetch(self, source: str, query: str | None, doc_id: str | None,
                  revid: int | None, status: str) -> None:
        with closing(sqlite3.connect(self.collect_sqlite)) as conn, conn:
            conn.execute("INSERT INTO fetch_log(source, query, doc_id, revid, status, fetched_at) "
                         "VALUES (?, ?, ?, ?, ?, ?)",
                         (source, query, doc_id, revid, status, datetime.now(timezone.utc).isoformat()))

    def queried(self, source: str, query: str) -> bool:
        """오류만 발생한 검색어는 다시 시도할 수 있게 한다."""
        with closing(sqlite3.connect(self.collect_sqlite)) as conn:
            return conn.execute("SELECT 1 FROM fetch_log WHERE source=? AND query=? "
                                "AND status IN ('fetched','cached','missing') LIMIT 1", (source, query)).fetchone() is not None

    def get_language_link(self, doc: Doc, target_lang: str) -> tuple[bool, str | None]:
        """링크 없음도 캐시하며 문서 revision이 바뀌면 다시 조회한다."""
        with closing(sqlite3.connect(self.collect_sqlite)) as conn:
            row = conn.execute("SELECT title FROM language_links WHERE doc_id=? AND revid=? AND target_lang=?",
                               (doc.doc_id, doc.revid, target_lang)).fetchone()
        return (True, row[0]) if row else (False, None)

    def save_language_link(self, doc: Doc, target_lang: str, title: str | None) -> None:
        with closing(sqlite3.connect(self.collect_sqlite)) as conn, conn:
            conn.execute("INSERT OR REPLACE INTO language_links VALUES (?, ?, ?, ?)",
                         (doc.doc_id, doc.revid, target_lang, title))


def ensure_doc(store: DocStore, collector: Collector, lang: str, title: str,
               search_query: str | None = None) -> tuple[Doc | None, str]:
    """최신 revision만 확인하고 변경된 본문만 저장한다."""
    doc_id, revid = None, None
    try:
        latest = collector.latest_revid(lang, title)
        if latest is None:
            store.log_fetch(collector.source, search_query, None, None, "missing")
            return None, "missing"
        page_id, revid, _ = latest
        stored = store.find(collector.source, lang, page_id)
        if stored is not None:
            doc_id = stored.doc_id
            if stored.revid == revid:
                store.log_fetch(collector.source, search_query, doc_id, revid, "cached")
                return stored, "cached"
        doc = collector.fetch(lang, page_id, search_query=search_query)
        if doc is None:
            store.log_fetch(collector.source, search_query, doc_id, revid, "missing")
            return None, "missing"
        store.save(doc)
        store.log_fetch(collector.source, search_query, doc.doc_id, doc.revid, "fetched")
        return doc, "fetched"
    except Exception:
        store.log_fetch(collector.source, search_query, doc_id, revid, "error")
        raise
