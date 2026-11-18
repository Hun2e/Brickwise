import pytest

from rag_service.services.progress import STAGES, StageTracker, to_stage


@pytest.mark.parametrize("name,data,expected", [
    ("routing", {}, ("분류", None)), ("resolving", {}, ("분류", None)),
    ("extracting_queries", {}, ("검색어 추출", None)), ("extracting", {}, ("검색어 추출", None)),
    ("searching", {"q": "Falcon"}, ("문서 수집", None)),
    ("searching", {"q": ""}, ("문서 수집", None)), ("searching", {}, ("검색", None)),
    ("selecting", {}, ("문서 수집", None)), ("indexing", {}, ("문서 수집", None)),
    ("selected", {"selected": ["제목"], "rejected_count": 4}, ("문서 수집", "선택 1 · 제외 4")),
    ("selected", {"selected": [], "rejected_count": 0}, ("문서 수집", "선택 0 · 제외 0")),
    ("fetching", {"title": "Millennium Falcon", "lang": "en"}, ("문서 수집", "Millennium Falcon")),
    ("skipped", {"reason": "cached"}, ("문서 수집", "이미 수집됨")),
    ("skipped", {"reason": "budget"}, ("문서 수집", None)),
    ("querying", {}, ("검색", None)), ("retrieving", {}, ("검색", None)),
    ("ranking", {}, ("검색", None)), ("reviewing", {}, ("검색", None)),
    ("generating", {}, ("생성", None)), ("verifying", {}, ("검증", None)),
    ("done", {}, None), ("unknown", {}, None),
])
def test_mapping(name, data, expected):
    assert to_stage(name, data) == expected
    if expected:
        assert expected[0] in STAGES


def test_tracker_collapses_untitled_stages_and_preserves_titles():
    tracker = StageTracker()
    events = [("routing", {}), ("resolving", {}), ("extracting_queries", {}),
              ("searching", {"q": "q"}), ("selecting", {}),
              ("fetching", {"title": "제목A"}), ("indexing", {}), ("retrieving", {})]
    assert [stage for name, data in events if (stage := tracker.update(name, data))] == [
        ("분류", None), ("검색어 추출", None), ("문서 수집", None),
        ("문서 수집", "제목A"), ("검색", None)]
    assert tracker.update("fetching", {"title": "제목A"}) == ("문서 수집", "제목A")
    assert tracker.update("fetching", {"title": "제목B"}) == ("문서 수집", "제목B")
    assert tracker.update("skipped", {"reason": "cached"}) == ("문서 수집", "이미 수집됨")
    assert tracker.update("indexing", {}) is None
    assert tracker.update("selected", {"selected": ["제목"], "rejected_count": 4}) == ("문서 수집", "선택 1 · 제외 4")
    assert StageTracker().update("routing", {}) == ("분류", None)
