"""T-003 실응답 픽스처 및 경계 조건으로 청킹을 검증한다."""

from dataclasses import replace

import pytest

from rag_service.config import load_settings
from rag_service.data_prep.collectors.base import Section
from rag_service.vectorstore.chunking import chunk_doc
from test_collectors_offline import make_collector, offline


@pytest.fixture
def wiki_doc():
    collector, _, _ = make_collector(["extract_en_lego_star_wars"])
    return collector.fetch("en", "Lego Star Wars")


def test_fixture_headers_limits_determinism_and_settings(wiki_doc):
    cfg = load_settings()["chunking"]
    args = (cfg["chunk_size"], cfg["chunk_overlap"], cfg["min_chunk_chars"], cfg["header"])
    chunks = chunk_doc(wiki_doc, *args)
    assert chunks and chunks == chunk_doc(wiki_doc, *args)
    assert [chunk.seq for chunk in chunks] == list(range(len(chunks)))
    assert all(0 < chunk.char_len <= cfg["chunk_size"] for chunk in chunks)
    assert all(chunk.text.startswith("[Lego Star Wars") for chunk in chunks)
    assert all(len(chunk.text.split("\n", 1)[1]) == chunk.char_len for chunk in chunks)
    assert any(" › " in chunk.section_path for chunk in chunks)
    assert len(chunk_doc(wiki_doc, 300, 60, 60, True)) != len(chunks)
    without_headers = chunk_doc(wiki_doc, *args[:-1], False)
    assert [chunk.text for chunk in without_headers] == [chunk.text.split("\n", 1)[1] for chunk in chunks]


def test_exact_overlap_for_long_unbroken_text(wiki_doc):
    text = "".join(chr(0x4e00 + index) for index in range(2300))
    doc = replace(wiki_doc, sections=[Section("Long", 2, text)])
    chunks = chunk_doc(doc, 1000, 200, 200, False)
    assert [chunk.char_len for chunk in chunks] == [1000, 1000, 700]
    assert chunks[0].text[-200:] == chunks[1].text[:200]
    assert chunks[1].text[-200:] == chunks[2].text[:200]
    assert chunks[0].text + chunks[1].text[200:] + chunks[2].text[200:] == text


def test_short_siblings_merge_without_crossing_parent_or_cap(wiki_doc):
    doc = replace(wiki_doc, sections=[Section("Parent", 2, ""),
        Section("First", 3, "a" * 100), Section("Second", 3, "b" * 300),
        Section("Third", 3, "c" * 900), Section("Fourth", 3, "d" * 150),
        Section("Other", 2, ""), Section("Fifth", 3, "e" * 100),
        Section("Sixth", 3, "f" * 200)])
    chunks = chunk_doc(doc, 1000, 200, 200, True)
    assert [chunk.char_len for chunk in chunks] == [402, 900, 150, 302]
    assert chunks[0].section_path == "Parent › First | Parent › Second"
    assert chunks[-1].section_path == "Other › Fifth | Other › Sixth"
    assert all(not ("Parent" in chunk.section_path and "Other" in chunk.section_path) for chunk in chunks)


def test_empty_sections_keep_hierarchy_and_empty_document(wiki_doc):
    doc = replace(wiki_doc, sections=[Section("Top", 2, ""), Section("Child", 3, "body")])
    assert chunk_doc(doc, 100, 20, 20, True)[0].text == "[Lego Star Wars › Top › Child]\nbody"
    assert chunk_doc(replace(doc, sections=[]), 100, 20, 20, True) == []


@pytest.mark.parametrize("size,overlap,minimum", [(0, 0, 0), (10, 10, 0), (10, -1, 0), (10, 0, 11), (10, 0, -1)])
def test_invalid_settings(wiki_doc, size, overlap, minimum):
    with pytest.raises(ValueError):
        chunk_doc(wiki_doc, size, overlap, minimum, True)
