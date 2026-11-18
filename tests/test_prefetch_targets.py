"""가짜 수집기와 임시 저장소로 매핑 사전 수집을 검증한다."""

import sqlite3
from dataclasses import replace
from unittest.mock import Mock

import pytest

from rag_service.config import load_settings
from rag_service.data_prep.doc_store import DocStore
from rag_service.data_prep.prefetch import LocalDocuments, mapped_targets, target_query
from test_collectors_offline import make_collector, offline
from test_index_items_offline import load_script


@pytest.fixture
def mapping():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE set_entities(set_num TEXT, wiki_title TEXT, kind TEXT, rule_index INTEGER)")
    conn.executemany("INSERT INTO set_entities VALUES (?, ?, ?, ?)", [
        ("A", "Millennium Falcon", "vehicle", 0), ("B", "Millennium Falcon", "vehicle", 0),
        ("C", "Jedi", "group", 1)])
    yield conn
    conn.close()


@pytest.mark.parametrize("policy", ["include", "tag", "exclude"])
def test_targets_policies_series_korean_and_dedup(mapping, policy):
    settings = load_settings()
    settings["index"]["docs_group_policy"] = policy
    collector = Mock()
    collector.langlinks.return_value = {"Millennium Falcon": "밀레니엄 팰컨", "Jedi": None}
    targets = mapped_targets(mapping, settings, collector)
    assert len(targets) == (4 if policy == "exclude" else 5)
    assert {(target.lang, target.title) for target in targets if target.scope == "series"} == {
        ("en", "Lego Star Wars"), ("ko", "레고 스타워즈")}
    korean = next(target for target in targets if target.title == "밀레니엄 팰컨")
    assert korean.entity == "Millennium Falcon" and korean.entity_kind == "vehicle"
    assert sum(target.title == "Millennium Falcon" for target in targets) == 1
    assert any(target.entity_kind == "group" for target in targets) == (policy != "exclude")


def test_korean_disabled_and_invalid_policy(mapping):
    settings, collector = load_settings(), Mock()
    settings["index"]["docs_include_korean"] = False
    assert len(mapped_targets(mapping, settings, collector)) == 4
    collector.langlinks.assert_not_called()
    settings["index"]["docs_group_policy"] = "unknown"
    with pytest.raises(ValueError):
        mapped_targets(mapping, settings, collector)


def mapped_fixture(tmp_path):
    source, _, _ = make_collector(["extract_en_millennium_falcon"])
    doc = source.fetch("en", "Millennium Falcon")
    korean = replace(doc, doc_id="wp-ko:99", page_id=99, lang="ko", title="밀레니엄 팰컨")
    collector = Mock(source="wikipedia")
    by_page = {("en", doc.page_id): doc, ("ko", 99): korean}
    by_title = {("en", "Millennium Falcon"): doc, ("ko", korean.title): korean}
    collector.latest_revid.side_effect = lambda lang, title: (by_title[(lang, title)].page_id,
        by_title[(lang, title)].revid, by_title[(lang, title)].title)
    collector.fetch.side_effect = lambda lang, page, search_query=None: replace(
        by_page[(lang, page)], search_query=search_query)
    collector.langlinks.return_value = {doc.title: korean.title}
    store = DocStore(tmp_path / "docs", tmp_path / "collect.sqlite")
    settings = load_settings()
    settings["wikipedia"]["seeds"] = []
    settings["index"]["docs_group_policy"] = "exclude"
    return collector, store, settings


def test_collect_twice_cached_and_local_resolution(mapping, tmp_path, capsys):
    module = load_script("prefetch_docs")
    collector, store, settings = mapped_fixture(tmp_path)
    assert module.collect_mapped(mapping, store, collector, settings) == 0
    assert collector.fetch.call_count == 2 and collector.langlinks.call_count == 1
    assert module.collect_mapped(mapping, store, collector, settings) == 0
    assert collector.fetch.call_count == 2 and collector.langlinks.call_count == 1
    local = LocalDocuments(store)
    targets = mapped_targets(mapping, settings, local)
    assert len(targets) == 2
    assert all(local.get(target.lang, target.title) for target in targets)
    assert capsys.readouterr().out.count("| cached |") == 2


def test_redirect_and_missing_cache_fail_closed(mapping, tmp_path):
    collector, store, settings = mapped_fixture(tmp_path)
    doc = collector.fetch("en", collector.latest_revid("en", "Millennium Falcon")[0])
    store.save(replace(doc, title="Canonical redirect target"))
    store.log_fetch("wikipedia", target_query("en", "Millennium Falcon"), doc.doc_id, doc.revid, "fetched")
    local = LocalDocuments(store)
    assert local.get("en", "Millennium Falcon").title == "Canonical redirect target"
    with pytest.raises(ValueError, match="언어 링크"):
        mapped_targets(mapping, settings, local)
    store.save_language_link(doc, "ko", None)
    assert len(mapped_targets(mapping, settings, local)) == 1
    store.log_fetch("wikipedia", target_query("en", "Millennium Falcon"), None, None, "missing")
    with pytest.raises(ValueError, match="대상 문서"):
        local.get("en", "Millennium Falcon")


def test_mapped_limit_and_cli_mode(mapping, tmp_path):
    module = load_script("prefetch_docs")
    collector, store, settings = mapped_fixture(tmp_path)
    assert module.collect_mapped(mapping, store, collector, settings, limit=1) == 0
    assert collector.fetch.call_count == 1
    assert module.parse_args(["--mapped"]).mapped
    assert module.parse_args(["--seeds"]).seeds
    with pytest.raises(SystemExit):
        module.parse_args(["--mapped", "--seeds"])


def test_missing_mapped_page_reports_failure(mapping, tmp_path):
    module = load_script("prefetch_docs")
    collector, store, settings = mapped_fixture(tmp_path)
    collector.latest_revid.side_effect = None
    collector.latest_revid.return_value = None
    assert module.collect_mapped(mapping, store, collector, settings) == 1
    collector.fetch.assert_not_called()
    collector.langlinks.assert_not_called()
