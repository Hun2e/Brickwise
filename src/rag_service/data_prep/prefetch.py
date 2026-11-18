"""매핑·시리즈·언어 링크에 따른 사전 수집 대상과 로컬 조회."""

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from typing import Protocol

from .collectors.base import Doc
from .doc_store import DocStore


@dataclass(frozen=True)
class PrefetchTarget:
    lang: str
    title: str
    scope: str
    entity: str | None
    entity_kind: str


class LanguageLinks(Protocol):
    def langlinks(self, titles: list[str], from_lang: str,
                  to_lang: str) -> dict[str, str | None]: ...


def mapped_targets(conn: sqlite3.Connection, settings: dict,
                   collector: LanguageLinks) -> list[PrefetchTarget]:
    """전체 매핑에서 대상을 만든다. 언어 링크 조회 방식은 호출자가 주입한다."""
    policy = settings["index"]["docs_group_policy"]
    if policy not in {"include", "tag", "exclude"}:
        raise ValueError("문서 group 정책을 확인하세요.")
    targets = []
    for title, kind in conn.execute("SELECT DISTINCT wiki_title, kind FROM set_entities ORDER BY wiki_title, kind"):
        if kind != "group" or policy != "exclude":
            targets.append(PrefetchTarget("en", title, "entity", title, kind))
    # 시리즈 시드 판별은 도메인 데이터 준비 계층에만 둔다.
    series = {("en", "Lego Star Wars"), ("ko", "레고 스타워즈")}
    targets.extend(PrefetchTarget(lang, title, "series", None, "series")
                   for lang, title in settings["wikipedia"]["seeds"] if (lang, title) in series)
    if settings["index"]["docs_include_korean"]:
        english = [target for target in targets if target.lang == "en" and target.scope == "entity"]
        links = collector.langlinks(sorted({target.title for target in english}), "en", "ko") if english else {}
        targets.extend(PrefetchTarget("ko", links[target.title], "entity", target.entity, target.entity_kind)
                       for target in english if links[target.title])
    return sorted(set(targets), key=lambda target: (target.lang, target.title, target.scope, target.entity or ""))


def target_query(lang: str, title: str) -> str:
    """리다이렉트 전 요청 제목도 기존 수집 이력에 보존한다."""
    return f"mapped:{lang}:{title}"


class LocalDocuments:
    """네트워크 없이 저장된 본문과 revision별 언어 링크만 사용한다."""

    def __init__(self, store: DocStore):
        self.store = store
        with closing(sqlite3.connect(store.collect_sqlite)) as conn:
            ids = [row[0] for row in conn.execute("SELECT doc_id FROM documents ORDER BY doc_id")]
        self.by_title = {}
        for doc_id in ids:
            doc = store.get(doc_id)
            if doc is not None and doc.source == "wikipedia":
                self.by_title[(doc.lang, doc.title)] = doc

    def get(self, lang: str, title: str) -> Doc:
        with closing(sqlite3.connect(self.store.collect_sqlite)) as conn:
            row = conn.execute("SELECT doc_id, status FROM fetch_log WHERE source='wikipedia' AND query=? "
                               "ORDER BY id DESC LIMIT 1", (target_query(lang, title),)).fetchone()
        if row is not None:
            doc = self.store.get(row[0]) if row[1] in {"fetched", "cached"} else None
        else:
            doc = self.by_title.get((lang, title))
        if doc is None:
            raise ValueError("대상 문서가 없습니다. prefetch_docs.py --mapped를 먼저 완료하세요.")
        return doc

    def langlinks(self, titles: list[str], from_lang: str, to_lang: str) -> dict[str, str | None]:
        result = {}
        for title in titles:
            found, link = self.store.get_language_link(self.get(from_lang, title), to_lang)
            if not found:
                raise ValueError("언어 링크 캐시가 없습니다. prefetch_docs.py --mapped를 먼저 완료하세요.")
            result[title] = link
        return result
