"""실제 응답 픽스처와 가짜 세션만 사용하는 수집기 회귀 검사."""

import builtins
import importlib.util
import io
import json
import socket
import sqlite3
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import unquote

import pytest
import requests

from rag_service import env
from rag_service.config import load_settings, project_root
from rag_service.data_prep import doc_store
from rag_service.data_prep.collectors.base import Candidate, Section
from rag_service.data_prep.collectors.cleaning import clean_sections, split_sections
from rag_service.data_prep.collectors.http import HttpClient, MissingContact, UpstreamError, build_user_agent
from rag_service.data_prep.collectors.wikipedia import WikipediaCollector
from rag_service.data_prep.doc_store import DocStore, ensure_doc


FIXTURES = Path(__file__).parent / "fixtures" / "wikipedia"
AGENT = "BrickArchivePlanner/0.1 (offline@example.invalid) python-requests"


@pytest.mark.parametrize("continued", [False, True])
def test_langlinks_batches_120_and_continuation(continued):
    client = Mock()
    calls = []

    def query(url, params):
        batch = params["titles"].split("|")
        calls.append(params.copy())
        pages = [{"title": title.upper(), "langlinks": [{"lang": "ko", "title": "한국어 " + title}]}
                 for title in batch]
        result = {"query": {"normalized": [{"from": title, "to": title.upper()} for title in batch],
                            "pages": pages}}
        if continued and "llcontinue" not in params:
            result["query"]["pages"] = [{"title": title.upper()} for title in batch]
            result["continue"] = {"llcontinue": "next-" + batch[0], "continue": "||"}
        return result

    client.get_json.side_effect = query
    collector = WikipediaCollector(client, load_settings())
    titles = [f"page-{i}" for i in range(120)]
    assert collector.langlinks(titles, "en", "ko") == {title: "한국어 " + title for title in titles}
    assert [len(call["titles"].split("|")) for call in calls] == (
        [50, 50, 50, 50, 20, 20] if continued else [50, 50, 20])
    if continued:
        for first, second in zip(calls[::2], calls[1::2]):
            assert "llcontinue" not in first
            assert second["llcontinue"] == "next-" + first["titles"].split("|")[0]
    client.reset_mock()
    assert collector.langlinks([], "en", "ko") == {}
    client.get_json.assert_not_called()


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Network access is forbidden")

    def guard(original):
        def checked(file, *args, **kwargs):
            if isinstance(file, (str, Path)):
                assert Path(file).name != ".env", ".env access is forbidden"
            return original(file, *args, **kwargs)
        return checked

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(requests.Session, "request", forbidden)
    monkeypatch.setattr(builtins, "open", guard(builtins.open))
    monkeypatch.setattr(io, "open", guard(io.open))
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    env.load_env.cache_clear()
    yield
    env.load_env.cache_clear()


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["response"]


class FakeTime:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeResponse:
    def __init__(self, data=None, status=200, headers=None):
        self.data = {} if data is None else data
        self.status_code = status
        self.headers = headers or {}
        self.closed = False

    def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, FakeResponse) else FakeResponse(value)

    def close(self):
        self.closed = True


def make_client(responses, interval=0.5, retries=3):
    session, timer = FakeSession(responses), FakeTime()
    client = HttpClient(AGENT, interval, retries, 20, 2, session=session,
                        sleep=timer.sleep, clock=timer.clock)
    return client, session, timer


def make_collector(names):
    client, session, timer = make_client([fixture(name) for name in names])
    return WikipediaCollector(client, load_settings()), session, timer


@pytest.fixture
def store(tmp_path):
    return DocStore(tmp_path / "docs", tmp_path / "collect.sqlite")


@pytest.fixture
def prefetch():
    spec = importlib.util.spec_from_file_location("prefetch_docs_offline", project_root() / "scripts/prefetch_docs.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_user_agent():
    assert build_user_agent("BrickArchivePlanner", "0.1", "offline@example.invalid") == AGENT
    with pytest.warns(MissingContact, match="WIKI_CONTACT"):
        assert build_user_agent("App", "1", None) == "App/1 python-requests"
    with pytest.warns(MissingContact):
        assert build_user_agent("App", "1", "  ") == "App/1 python-requests"
    for app, version, contact in [("앱", "1", "x"), ("App", "버전", "x"),
                                  ("App", "1", "연락처"), ("App", "1", "x\r\nHeader: value")]:
        with pytest.raises(ValueError, match="ASCII"):
            build_user_agent(app, version, contact)


def test_http_spacing_headers_and_timeout():
    responses = [FakeResponse({"ok": True}), FakeResponse()]
    client, session, timer = make_client(responses)
    assert client.get_json("https://example.invalid", {}) == {"ok": True}
    timer.now += 0.2
    client.get_json("https://example.invalid", {})
    assert timer.sleeps == pytest.approx([0.3])
    assert client.request_count == 2 and all(response.closed for response in responses)
    assert session.calls[0][1] == {"params": {}, "headers": {"User-Agent": AGENT}, "timeout": 20}


def test_retry_after_and_backoff():
    client, session, timer = make_client([
        FakeResponse(status=429, headers={"Retry-After": "3"}),
        FakeResponse(status=503), FakeResponse({"ok": True}),
    ])
    assert client.get_json("https://example.invalid", {}) == {"ok": True}
    assert timer.sleeps == [3, 4] and len(session.calls) == 3


@pytest.mark.parametrize("header", ["bad", "-1", "NaN", "inf"])
def test_invalid_retry_after_falls_back(header):
    client, _, timer = make_client([FakeResponse(status=500, headers={"Retry-After": header}), {}])
    client.get_json("https://example.invalid", {})
    assert timer.sleeps == [2]


def test_retry_after_still_obeys_minimum_interval():
    client, _, timer = make_client([FakeResponse(status=429, headers={"Retry-After": "1"}), {}], interval=6.5)
    client.get_json("https://example.invalid", {})
    assert timer.sleeps == [1, 5.5]


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_retry_exhaustion_sanitizes_errors(status):
    client, session, timer = make_client([FakeResponse(status=status)] * 4)
    with pytest.raises(UpstreamError) as error:
        client.get_json("https://example.invalid", {})
    assert len(session.calls) == 4 and timer.sleeps == [2, 4, 8]
    assert AGENT not in str(error.value) and "offline@example.invalid" not in str(error.value)


@pytest.mark.parametrize("response", [FakeResponse(status=404), FakeResponse(data=ValueError(AGENT)),
                                     FakeResponse(data={"error": {"info": AGENT}}),
                                     FakeResponse(data=[]), requests.Timeout(AGENT)])
def test_non_retryable_failures(response):
    client, session, _ = make_client([response])
    with pytest.raises(UpstreamError) as error:
        client.get_json("https://example.invalid", {})
    assert len(session.calls) == 1 and AGENT not in str(error.value)
    assert error.value.__suppress_context__ or error.value.__context__ is None


@pytest.mark.parametrize("lang", ["en", "ko"])
def test_search_real_fixture(lang):
    name = f"search_{lang}_lego_star_wars"
    collector, session, _ = make_collector([name])
    candidates = collector.search("Lego Star Wars", lang, 5)
    rows = fixture(name)["query"]["search"]
    assert len(candidates) == len(rows) == 5
    for candidate, row in zip(candidates, rows):
        assert isinstance(candidate, Candidate) and candidate.page_id == row["pageid"]
        assert "<" not in candidate.snippet and "&#" not in candidate.snippet
        assert candidate.source == "wikipedia" and candidate.lang == lang
        assert unquote(candidate.url).endswith(row["title"].replace(" ", "_"))
    params = session.calls[0][1]["params"]
    assert params["formatversion"] == 2 and params["redirects"] == 1
    assert params["srlimit"] == 5


@pytest.mark.parametrize("lang", ["en", "ko"])
def test_fetch_real_fixture(lang):
    collector, session, _ = make_collector([f"extract_{lang}_lego_star_wars"])
    doc = collector.fetch(lang, "Lego Star Wars", search_query="seed query")
    headings = [section.heading for section in doc.sections]
    assert doc.doc_id == f"wp-{lang}:{doc.page_id}" and doc.license == "CC BY-SA 4.0"
    assert doc.attribution == f"Wikipedia contributors, {doc.title}, {doc.url}"
    assert doc.search_query == "seed query" and doc.fetched_at.endswith("+00:00")
    assert not set(headings) & set(load_settings()["wikipedia"]["drop_sections"][lang])
    assert doc.sections[0].heading == "" and doc.sections[0].level == 1
    assert doc.cleaning_stats["before_chars"] > doc.cleaning_stats["after_chars"] > 0
    assert doc.cleaning_stats["kept_sections"] == len(doc.sections)
    assert doc.cleaning_stats["dropped_sections"] > 0
    assert doc.text().startswith(doc.sections[0].text)
    if lang == "en":
        assert {"Overview", "Development"} <= set(headings)
        assert "## Overview\n" in doc.text()
    params = session.calls[0][1]["params"]
    assert params["prop"] == "extracts|info|revisions"
    assert params["explaintext"] == 1 and params["exsectionformat"] == "wiki" and params["rvprop"] == "ids"


def test_irregular_headings_in_real_extract():
    extract = fixture("extract_en_lego_star_wars")["query"]["pages"][0]["extract"]
    standard = "===== List of Ultimate Collectors Series sets ====="
    irregular = "==== = List of Ultimate Collectors Series sets = ===="
    assert standard in extract
    section = next(s for s in split_sections(extract) if s.heading == "List of Ultimate Collectors Series sets")
    assert section.level == 5
    section = next(s for s in split_sections(extract.replace(standard, irregular))
                   if s.heading == "List of Ultimate Collectors Series sets")
    assert section.level == 4


def test_cleaning_subtrees_whitespace_and_short_sections():
    sections = split_sections("Lead long enough\n\n\n== Keep ==\n a   b \n\n\n c \n"
                              "== REFERENCES ==\nremove\n=== Nested ===\nremove too\n"
                              "== Empty ==\n== Short ==\nx\n== Kept ==\nlast content")
    result = clean_sections(sections, [" references "], 3)
    assert [s.heading for s in result] == ["", "Keep", "Kept"]
    assert result[1].text == "a b\n\nc"
    assert clean_sections(split_sections(""), [], 0) == []


def test_langlinks_redirect_normalization_and_continuation():
    data = fixture("langlinks_en_ko")
    data["query"]["normalized"] = [{"from": "millennium_Falcon", "to": "Millennium Falcon"}]
    data["continue"] = {"continue": "||", "llcontinue": "next"}
    client, session, _ = make_client([data, {"query": {"pages": []}}])
    collector = WikipediaCollector(client, load_settings())
    result = collector.langlinks(["Millennium Falcon", "AT-AT", "millennium_Falcon"], "en", "ko")
    assert result == {"Millennium Falcon": "밀레니엄 팰컨", "AT-AT": None, "millennium_Falcon": "밀레니엄 팰컨"}
    assert session.calls[1][1]["params"]["llcontinue"] == "next"
    assert collector.langlinks([], "en", "ko") == {}


@pytest.mark.parametrize("continued", [False, True])
def test_langlinks_batches_120_titles(continued):
    titles = [f"Title {i}" for i in range(120)]
    responses = []
    for start in range(0, len(titles), 50):
        pages = [{"title": title, "langlinks": [{"lang": "ko", "title": f"한글 {title}"}]}
                 for title in titles[start:start + 50]]
        query = {"pages": pages}
        if continued:
            # 각 배치에서 정규화·리다이렉트·후속 응답을 함께 처리해야 한다.
            original = pages[0]["title"]
            query["normalized"] = [{"from": original, "to": f"Normalized {start}"}]
            query["redirects"] = [{"from": f"Normalized {start}", "to": f"Canonical {start}"}]
            pages[0] = {"title": f"Canonical {start}"}
            responses.extend([
                {"query": query, "continue": {"continue": "||", "llcontinue": f"next-{start}"}},
                {"query": {"pages": [{"title": f"Canonical {start}",
                    "langlinks": [{"lang": "ko", "title": f"한글 {original}"}]}]}},
            ])
        else:
            responses.append({"query": query})
    client, session, _ = make_client(responses)
    result = WikipediaCollector(client, load_settings()).langlinks(titles, "en", "ko")
    assert result == {title: f"한글 {title}" for title in titles}
    params = [call[1]["params"] for call in session.calls]
    assert [len(p["titles"].split("|")) for p in params] == (
        [50, 50, 50, 50, 20, 20] if continued else [50, 50, 20])
    if continued:
        assert all("llcontinue" not in p for p in params[::2])
        assert [p["llcontinue"] for p in params[1::2]] == ["next-0", "next-50", "next-100"]


def test_missing_and_latest_revision():
    collector, session, _ = make_collector(["missing_page", "missing_page", "info_en_lego_star_wars"])
    assert collector.fetch("en", "missing") is None
    assert collector.latest_revid("en", "missing") is None
    page = fixture("info_en_lego_star_wars")["query"]["pages"][0]
    assert collector.latest_revid("en", page["pageid"]) == (page["pageid"], page["lastrevid"], page["title"])
    assert session.calls[-1][1]["params"]["pageids"] == page["pageid"]
    with pytest.raises(ValueError):
        collector.fetch("en/../../bad", "x")


def test_store_revision_cycle_and_persistence(store):
    info = fixture("info_en_lego_star_wars")
    extract = fixture("extract_en_lego_star_wars")
    updated_info = fixture("info_en_lego_star_wars")
    updated_extract = fixture("extract_en_lego_star_wars")
    for data in (updated_info, updated_extract):
        data["query"]["pages"][0]["revisions"][0]["revid"] += 1
    client, session, _ = make_client([info, extract, info, updated_info, updated_extract])
    collector = WikipediaCollector(client, load_settings())
    first, status = ensure_doc(store, collector, "en", "Lego Star Wars", "query")
    assert status == "fetched" and len(session.calls) == 2
    reopened = DocStore(store.docs_dir, store.collect_sqlite)
    assert reopened.get(first.doc_id) == first and reopened.stored_revid(first.doc_id) == first.revid
    cached, status = ensure_doc(reopened, collector, "en", "Lego Star Wars", "other query")
    assert status == "cached" and cached == first and len(session.calls) == 3
    changed, status = ensure_doc(reopened, collector, "en", "Lego Star Wars", "query")
    assert status == "fetched" and changed.revid == first.revid + 1 and len(session.calls) == 5
    assert reopened.queried("wikipedia", "query") and reopened.queried("wikipedia", "other query")
    assert not reopened.queried("other", "query") and not reopened.queried("wikipedia", "new")
    assert len(list(store.docs_dir.rglob("*.json"))) == 1


def test_store_atomic_failure_preserves_previous_file(store, monkeypatch):
    collector, _, _ = make_collector(["extract_en_lego_star_wars"])
    doc = collector.fetch("en", "Lego Star Wars")
    path = store.save(doc)
    before = path.read_bytes()
    observed = []

    def fail_replace(source, destination):
        observed.append((source, destination))
        assert source.parent == destination.parent
        assert json.loads(source.read_text(encoding="utf-8"))["revid"] == doc.revid + 1
        raise OSError("simulated failure")

    monkeypatch.setattr(doc_store.os, "replace", fail_replace)
    with pytest.raises(OSError):
        store.save(replace(doc, revid=doc.revid + 1))
    assert observed and path.read_bytes() == before and not list(path.parent.glob("*.tmp"))
    assert store.stored_revid(doc.doc_id) == doc.revid
    assert store.get("unknown") is None and store.stored_revid("unknown") is None
    with pytest.raises(ValueError):
        store.save(replace(doc, source="../outside"))


def test_missing_and_error_log_and_retry(store):
    collector, _, _ = make_collector(["missing_page"])
    assert ensure_doc(store, collector, "en", "missing", "missing query") == (None, "missing")
    assert store.queried("wikipedia", "missing query")
    client, _, _ = make_client([FakeResponse(status=404)])
    with pytest.raises(UpstreamError):
        ensure_doc(store, WikipediaCollector(client, load_settings()), "en", "bad", "failed query")
    assert not store.queried("wikipedia", "failed query")
    with sqlite3.connect(store.collect_sqlite) as conn:
        assert conn.execute("SELECT status FROM fetch_log ORDER BY id").fetchall() == [("missing",), ("error",)]


def test_deleted_cached_file_is_refetched(store):
    collector, session, _ = make_collector(["info_en_lego_star_wars", "extract_en_lego_star_wars"] * 2)
    doc, _ = ensure_doc(store, collector, "en", "Lego Star Wars")
    next(store.docs_dir.rglob("*.json")).unlink()
    assert store.get(doc.doc_id) is None
    assert ensure_doc(store, collector, "en", "Lego Star Wars")[1] == "fetched"
    assert len(session.calls) == 4


def test_page_disappears_between_info_and_fetch(store):
    collector, session, _ = make_collector(["info_en_lego_star_wars", "missing_page"])
    assert ensure_doc(store, collector, "en", "Lego Star Wars", "query") == (None, "missing")
    assert len(session.calls) == 2 and not list(store.docs_dir.rglob("*.json"))


def test_language_link_absence_is_cached_by_revision(store):
    collector, _, _ = make_collector(["extract_en_millennium_falcon"])
    doc = collector.fetch("en", "Millennium Falcon")
    assert store.get_language_link(doc, "ko") == (False, None)
    store.save_language_link(doc, "ko", None)
    assert store.get_language_link(doc, "ko") == (True, None)
    assert store.get_language_link(replace(doc, revid=doc.revid + 1), "ko") == (False, None)


def test_env_helper_loads_once_with_mock(monkeypatch):
    loader = Mock()
    monkeypatch.setattr(env, "load_dotenv", loader)
    env.load_env()
    env.load_env()
    loader.assert_called_once_with(project_root() / ".env", override=False)


def test_cli_two_runs_use_revision_only_on_second(prefetch, store, capsys):
    settings = load_settings()
    settings["wikipedia"]["seeds"] = [["en", "Lego Star Wars"], ["ko", "레고 스타워즈"]]
    names = ["info_en_lego_star_wars", "extract_en_lego_star_wars", "langlinks_en_ko",
             "info_ko_lego_star_wars", "extract_ko_lego_star_wars"]
    collector, session, _ = make_collector(names)
    assert prefetch.collect_seeds(store, collector, settings) == 0
    output = capsys.readouterr().out
    assert output.count("| fetched |") == 2 and "버린 섹션" in output
    assert len(session.calls) == 5
    collector, session, _ = make_collector(["info_en_lego_star_wars", "info_ko_lego_star_wars"])
    reopened = DocStore(store.docs_dir, store.collect_sqlite)
    assert prefetch.collect_seeds(reopened, collector, settings) == 0
    assert capsys.readouterr().out.count("| cached |") == 2
    assert len(session.calls) == 2
    assert all(call[1]["params"]["prop"] == "info|revisions" for call in session.calls)


def test_cli_discovers_korean_and_honors_limit(prefetch, store, capsys):
    settings = load_settings()
    settings["wikipedia"]["seeds"] = [["en", "Lego Star Wars"]]
    collector, session, _ = make_collector(["info_en_lego_star_wars", "extract_en_lego_star_wars", "langlinks_en_ko"])
    assert prefetch.collect_seeds(store, collector, settings, limit=1) == 0
    assert len(session.calls) == 3 and len(list(store.docs_dir.rglob("*.json"))) == 1
    collector, session, _ = make_collector(["info_en_lego_star_wars", "info_ko_lego_star_wars", "extract_ko_lego_star_wars"])
    assert prefetch.collect_seeds(store, collector, settings) == 0
    assert len(session.calls) == 3 and len(list(store.docs_dir.rglob("*.json"))) == 2


def test_cli_main_contact_fallback_report_and_errors(prefetch, tmp_path, monkeypatch, capsys):
    settings = load_settings()
    settings["paths"].update(docs_dir=str(tmp_path / "docs"), collect_sqlite=str(tmp_path / "collect.sqlite"))
    settings["wikipedia"]["seeds"] = [["en", "Lego Star Wars"]]
    monkeypatch.setattr(prefetch, "load_settings", lambda: settings)
    loader = Mock()
    monkeypatch.setattr(prefetch, "load_env", loader)
    monkeypatch.delenv("WIKI_CONTACT", raising=False)
    client, session, _ = make_client([FakeResponse(status=404)])
    factory = Mock(return_value=client)
    monkeypatch.setattr(prefetch, "HttpClient", factory)
    with pytest.warns(MissingContact):
        assert prefetch.main(["--seeds", "--limit", "1"]) == 1
    assert factory.call_args.args[1] == settings["external_api"]["interval_without_contact_sec"]
    assert "HTTP 요청 수: 1" in capsys.readouterr().out and session.closed
    loader.assert_called_once()


@pytest.mark.parametrize("args", [["--help"], ["--seeds", "--limit", "0"],
                                 ["--seeds", "--limit", "-1"], ["--source", "brickipedia", "--seeds"], []])
def test_cli_parses_before_loading_env(prefetch, monkeypatch, args):
    loader = Mock(side_effect=AssertionError("must not load environment"))
    monkeypatch.setattr(prefetch, "load_env", loader)
    with pytest.raises(SystemExit) as error:
        prefetch.main(args)
    assert error.value.code == (0 if args == ["--help"] else 2)
    loader.assert_not_called()
