"""위키백과 PoC 시드를 수집하고 정제·캐시 통계를 출력한다."""

import argparse
import os
import sqlite3
import sys
import time
from collections import deque
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rag_service.config import load_settings, resolve_path
from rag_service.data_prep.collectors.base import Doc
from rag_service.data_prep.collectors.http import HttpClient, UpstreamError, build_user_agent
from rag_service.data_prep.collectors.wikipedia import WikipediaCollector
from rag_service.data_prep.doc_store import DocStore, ensure_doc
from rag_service.data_prep.prefetch import PrefetchTarget, mapped_targets, target_query
from rag_service.env import load_env


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=["wikipedia"], default="wikipedia")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--seeds", action="store_true", help="설정의 시드와 한국어 언어 링크 수집")
    mode.add_argument("--mapped", action="store_true", help="매핑 문서·시리즈·한국어 언어 링크 수집")
    parser.add_argument("--limit", type=int, help="최대 수집 문서 수 (생략하면 전체 시드)")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit은 양수여야 합니다.")
    return args


def _print_doc(doc: Doc | None, status: str, lang: str, title: str) -> None:
    if doc is None:
        print(f"doc_id=- | {lang}:{title} | {status} | revid=-")
        return
    stats = doc.cleaning_stats
    print(f"{doc.doc_id} | {doc.title} | {status} | revid={doc.revid}")
    print(f"  글자 수 {stats.get('before_chars', '-')} → {stats.get('after_chars', '-')} | "
          f"남은 섹션 {stats.get('kept_sections', len(doc.sections))} | "
          f"버린 섹션 {stats.get('dropped_sections', '-')}")


def collect_seeds(store: DocStore, collector: WikipediaCollector, settings: dict,
                  limit: int | None = None) -> int:
    """시드 및 한국어 링크를 중복 없이 수집한다. 실패해도 나머지를 진행한다."""
    pending = deque((lang, title) for lang, title in settings["wikipedia"]["seeds"])
    visited: set[tuple[str, str]] = set()
    doc_ids: set[str] = set()
    failures = 0
    while pending and (limit is None or len(doc_ids) < limit):
        lang, title = pending.popleft()
        if (lang, title) in visited:
            continue
        visited.add((lang, title))
        try:
            doc, status = ensure_doc(store, collector, lang, title)
            if doc is None:
                _print_doc(doc, status, lang, title)
                continue
            visited.add((lang, doc.title))
            if doc.doc_id in doc_ids:
                continue
            doc_ids.add(doc.doc_id)
            _print_doc(doc, status, lang, title)
            if lang == "en" and "ko" in settings["wikipedia"]["langs"]:
                found, korean = store.get_language_link(doc, "ko")
                if not found:
                    korean = collector.langlinks([doc.title], "en", "ko")[doc.title]
                    store.save_language_link(doc, "ko", korean)
                if korean:
                    pending.append(("ko", korean))
        except (UpstreamError, OSError, sqlite3.Error, ValueError, KeyError, TypeError):
            failures += 1
            print(f"{lang}:{title} | error | 수집하지 못했습니다. 설정과 연결 상태를 확인하고 다시 실행하세요.")
    return failures


def collect_mapped(conn: sqlite3.Connection, store: DocStore, collector: WikipediaCollector,
                   settings: dict, limit: int | None = None) -> int:
    """영어 본문을 먼저 확보하고 판번호별 언어 링크 캐시를 재사용한다."""
    seen: dict[tuple[str, str], Doc | None] = {}
    doc_ids: set[str] = set()
    failures = 0

    def collect(target: PrefetchTarget) -> None:
        nonlocal failures
        key = (target.lang, target.title)
        if key in seen:
            return
        seen[key] = None
        try:
            doc, status = ensure_doc(store, collector, *key, search_query=target_query(*key))
            seen[key] = doc
            _print_doc(doc, status, *key)
            if doc is not None:
                doc_ids.add(doc.doc_id)
            else:
                failures += 1
        except (UpstreamError, OSError, sqlite3.Error, ValueError, KeyError, TypeError):
            failures += 1
            print(f"{target.lang}:{target.title} | error | 문서를 수집하지 못했습니다. 다시 실행하세요.")

    class CachedLinks:
        def langlinks(self, titles: list[str], from_lang: str, to_lang: str) -> dict[str, str | None]:
            result = {}
            for title in titles:
                doc = seen.get((from_lang, title))
                if doc is None:
                    result[title] = None
                    continue
                found, link = store.get_language_link(doc, to_lang)
                if not found:
                    link = collector.langlinks([doc.title], from_lang, to_lang)[doc.title]
                    store.save_language_link(doc, to_lang, link)
                result[title] = link
            return result

    base_settings = {**settings, "index": {**settings["index"], "docs_include_korean": False}}
    for target in mapped_targets(conn, base_settings, collector):
        if limit is not None and len(doc_ids) >= limit:
            return failures
        collect(target)
    try:
        targets = mapped_targets(conn, settings, CachedLinks())
    except (UpstreamError, OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        print("언어 링크 수집 실패: 연결 상태를 확인하고 다시 실행하세요.")
        return failures + 1
    for target in targets:
        if limit is not None and len(doc_ids) >= limit:
            break
        collect(target)
    return failures


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    started = time.monotonic()
    client: HttpClient | None = None
    try:
        settings = load_settings()
        load_env()
        contact = os.environ.get("WIKI_CONTACT", "").strip() or None
        api, app = settings["external_api"], settings["app"]
        agent = build_user_agent(app["name"], app["version"], contact)
        interval = api["request_interval_sec"] if contact else api["interval_without_contact_sec"]
        client = HttpClient(agent, interval, api["max_retries"], api["timeout_sec"], api["backoff_base_sec"])
        store = DocStore(resolve_path(settings["paths"]["docs_dir"]), resolve_path(settings["paths"]["collect_sqlite"]))
        collector = WikipediaCollector(client, settings)
        if args.mapped:
            path = resolve_path(settings["paths"]["sqlite"])
            if not path.is_file():
                print("세트 DB가 없습니다. load_rebrickable.py와 build_entities.py를 먼저 실행하세요.")
                return 2
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
                failures = collect_mapped(conn, store, collector, settings, args.limit)
        else:
            failures = collect_seeds(store, collector, settings, args.limit)
        return 1 if failures else 0
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        print("수집을 시작하지 못했습니다. 설정 파일과 저장 경로를 확인하세요.")
        return 2
    finally:
        if client is not None:
            print(f"전체 소요 시간: {time.monotonic() - started:.2f}초 | HTTP 요청 수: {client.request_count}")
            client.session.close()


if __name__ == "__main__":
    raise SystemExit(main())
