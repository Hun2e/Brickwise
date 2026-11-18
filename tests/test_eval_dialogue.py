"""공개 형식만 사용한 합성 문항으로 판정·실행·저장소 격리를 검증한다."""

import importlib.util
import json
import sqlite3
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from rag_service.config import load_settings, settings_context
from rag_service.eval import dialogue, snapshot
from rag_service.eval.common import write_result
from rag_service.services import chat, users
from rag_service.services.conversations import ConversationRepo
from rag_service.services.dialogue_state import DialogueState, Pending, ShownList, Subject
from rag_service.planner.collections import CollectionStore
from test_db_queries import conn
from test_index_items_offline import offline


def scenario(**changes):
    return {"id": "SYNTHETIC", "title": "형식 검증용", "turns": [{"q": "합성 질문", "expect": {}}], **changes}


def script(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_judgement_and_previous_order():
    before = DialogueState(last_list=ShownList("search", None, ("B-1", "A-1")))
    after = DialogueState(subject=Subject("B-1", "list", None),
        last_list=ShownList("recommend", None, ("A-1", "B-1")), compare=("A-1", "B-1"),
        mode="ignore_color", pending=Pending("confirm", ("B-1",), {"intent": "own", "question": "등록"}))
    expect = {"route": "build", "subject": "B-1", "subject_is_list_rank": 1, "list_kind": "recommend",
              "list_contains": ["B-1", "A-1"], "list_excludes": ["C-1"], "collection": {"A-1": 2},
              "mode": "ignore_color", "compare_includes": ["B-1"], "must_include": ["1234", "5,678"],
              "must_not_include": ["9999"], "clarify": True}
    response = {"route": "build", "answer": "부품 1,234개, 5678개"}
    checks = dialogue.judge(expect, response, before, after, {"A-1": 2})
    assert {c["key"] for c in checks} == dialogue.EXPECT_KEYS
    assert all(c["passed"] for c in checks)
    failed = dialogue.judge(expect, {"route": "fact", "answer": "9999"}, after, DialogueState(), {})
    assert {c["key"] for c in failed if not c["passed"]} == dialogue.EXPECT_KEYS - {"list_excludes"}
    assert all("expected" in c and "actual" in c for c in failed)
    assert dialogue.judge({}, {}, before, after, {}) == []


@pytest.mark.parametrize("rank", [1, 3])
def test_missing_rank_never_matches_null_subject(rank):
    assert not dialogue.judge({"subject_is_list_rank": rank}, {}, DialogueState(), DialogueState(), {})[0]["passed"]


def test_empty_collection_null_subject_false_pending_and_exact_quantities():
    state = DialogueState()
    expect = {"collection": {}, "subject": None, "clarify": False}
    assert all(c["passed"] for c in dialogue.judge(expect, {}, state, state, {}))
    for owned in ({"A-1": 2}, {"A-1": 1, "B-1": 1}, {}):
        assert not dialogue.judge({"collection": {"A-1": 1}}, {}, state, state, owned)[0]["passed"]
    assert dialogue.normalize_numbers("1,234,567과 1,234 / A,B") == "1234567과 1234 / A,B"


@pytest.mark.parametrize("expect", [{"typo": 1}, {"clarify": "false"}, {"subject_is_list_rank": True},
    {"subject_is_list_rank": 0}, {"collection": {"A-1": 0}}, {"collection": {"A-1": True}},
    {"list_contains": "A-1"}, {"subject": "A"}, {"route": []}, {"mode": "typo"}, {"must_include": [""]}])
def test_contract_rejects_malformed_expectations(expect):
    with pytest.raises(ValueError):
        dialogue.validate_scenarios([scenario(turns=[{"q": "Q", "expect": expect}])])


def test_jsonl_validation_no_private_inputs(tmp_path):
    path = tmp_path / "synthetic.jsonl"
    path.write_text(json.dumps(scenario()) + "\n\n", encoding="utf-8-sig")
    assert dialogue.read_scenarios(path) == [scenario()]
    for rows in ([], [scenario(), scenario()], [scenario(turns=[])], [scenario(setup={"collection": [["A-1", -1]]})]):
        with pytest.raises(ValueError):
            dialogue.validate_scenarios(rows)


def test_repetitions_isolate_user_conversation_database_and_preserve_sequence(tmp_path):
    settings = load_settings()
    settings["paths"]["user_sqlite"] = str(tmp_path / "DO-NOT-OPEN.sqlite")
    original = deepcopy(settings)
    seen = []
    cases = [scenario(setup={"collection": [["A-1", 2]]}, turns=[
        {"q": "목록", "expect": {"list_contains": ["B-1"], "collection": {"A-1": 2}}},
        {"q": "선택", "expect": {"subject_is_list_rank": 1, "subject": "B-1"}},
    ])]

    def fake(uid, cid, question, *, request_id):
        path = users.user_db_path()
        seen.append((uid, cid, path, question, request_id))
        assert path != Path(settings["paths"]["user_sqlite"])
        repo = chat.conversation_repo()
        state, version = repo.load_state(uid, cid)
        if question == "목록":
            assert version == 0 and state == DialogueState()
            state = replace(state, last_list=ShownList("search", None, ("B-1", "A-1")))
        else:
            assert version == 1
            state = replace(state, subject=Subject(state.last_list.items[0], "list", None),
                            last_list=ShownList("search", None, ("A-1",)))
        return repo.save_turn_with_state(uid, cid, question=question, response={"answer": "ok"},
            kind="answer", subjects=[], state=state, expected_version=version, request_id=request_id)

    rows, summary = dialogue.evaluate(cases, settings, reps=3, turn_fn=fake)
    assert summary["passed"] == 3 and summary["turns_passed"] == 6
    assert len({s[0] for s in seen}) == len({s[1] for s in seen}) == len({s[2] for s in seen}) == 3
    assert len({s[4] for s in seen}) == 6
    assert [s[3] for s in seen] == ["목록", "선택"] * 3
    assert all(not s[2].exists() for s in seen)
    assert not Path(settings["paths"]["user_sqlite"]).exists() and settings == original
    assert load_settings()["paths"]["user_sqlite"] != settings["paths"]["user_sqlite"]
    settings["evaluation"]["results_dir"] = str(tmp_path / "results")
    path = write_result("synthetic", "dialogue", settings, rows, summary, dialogue.markdown(rows, summary))
    assert json.loads(path.read_text(encoding="utf-8"))["summary"]["passed"] == 3
    assert "선택" in path.with_suffix(".md").read_text(encoding="utf-8")


def test_real_chat_turn_entrypoint_collection_and_title(conn, tmp_path, monkeypatch):
    settings = load_settings()
    ref = tmp_path / "reference.sqlite"
    conn.commit()
    with sqlite3.connect(ref) as target:
        conn.backup(target)
    settings["paths"].update(sqlite=str(ref), user_sqlite=str(tmp_path / "not-opened.sqlite"))
    def unexpected(*args, **kwargs):
        raise AssertionError("컬렉션 명령은 LLM을 부르지 않아야 합니다")
    monkeypatch.setattr(chat.orchestrator, "answer", unexpected)
    cases = [scenario(turns=[
        {"q": "A-1 하나 보유 목록에 등록해줘", "expect": {"route": "collection", "collection": {"A-1": 1}, "clarify": False}},
        {"q": "A-1 하나 더 추가해줘", "expect": {"collection": {"A-1": 2}}},
    ])]
    actual = chat.chat_turn_v2
    def observed(*args, **kwargs):
        turn = actual(*args, **kwargs)
        assert chat.get_conversation(args[0], args[1])["title"] == cases[0]["turns"][0]["q"][:20]
        return turn
    monkeypatch.setattr(chat, "chat_turn_v2", observed)
    rows, summary = dialogue.evaluate(cases, settings, reps=2)
    assert summary["passed"] == 2, rows
    assert not Path(settings["paths"]["user_sqlite"]).exists()


def test_exceptions_fail_even_without_expectations_and_later_turns_continue(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(chat.orchestrator, "log_exception", lambda *args: None)
    def broken(*args, **kwargs):
        calls.append(args[2])
        raise RuntimeError("private exception content")
    cases = [scenario(turns=[{"q": "one"}, {"q": "two"}])]
    rows, summary = dialogue.evaluate(cases, load_settings(), reps=1, turn_fn=broken)
    assert calls == ["one", "two"] and summary["failed"] == 1
    assert summary["turns_passed"] == 0 and "private exception content" not in json.dumps(rows)
    assert "턴 실행 예외" in dialogue.markdown(rows, summary)


def test_settings_context_restores_and_loads_other_config():
    original = load_settings()
    changed = deepcopy(original)
    changed["paths"]["user_sqlite"] = "isolated.sqlite"
    with pytest.raises(RuntimeError), settings_context(changed):
        assert load_settings()["paths"]["user_sqlite"] == "isolated.sqlite"
        assert "references" in load_settings(changed["paths"]["dialogue_cues"])
        raise RuntimeError()
    assert load_settings() == original


def test_working_data_only_opens_copies(tmp_path):
    settings = load_settings()
    for key in ("chroma_dir", "docs_dir", "collect_sqlite", "embed_cache"):
        source = tmp_path / "source" / key
        source.parent.mkdir(exist_ok=True)
        if key.endswith("dir"):
            source.mkdir()
            (source / "marker").write_text("original")
        else:
            source.write_text("original")
        settings["paths"][key] = str(source)
    with dialogue.working_data(settings) as copied:
        for key in ("chroma_dir", "docs_dir", "collect_sqlite", "embed_cache"):
            path = Path(copied["paths"][key])
            assert path != Path(settings["paths"][key])
            (path / "marker" if path.is_dir() else path).write_text("modified")
    assert not path.exists()
    for key in ("chroma_dir", "docs_dir", "collect_sqlite", "embed_cache"):
        source = Path(settings["paths"][key])
        assert (source / "marker" if source.is_dir() else source).read_text() == "original"


def test_cli_results_and_failure_exit_code(tmp_path, monkeypatch):
    cli = script("eval_dialogue")
    settings = load_settings()
    settings["evaluation"]["results_dir"] = str(tmp_path / "results")
    monkeypatch.setattr(cli, "configured", lambda _: settings)
    monkeypatch.setattr(cli, "get_chat_model", lambda _: object())
    monkeypatch.setattr(cli, "working_data", lambda config: nullcontext(config))
    path = tmp_path / "synthetic.jsonl"
    path.write_text(json.dumps(scenario(turns=[{"q": "Q", "expect": {"route": "fact"}}])), encoding="utf-8")
    monkeypatch.setattr(chat, "chat_turn_v2", lambda *a, **k: type("Turn", (), {"id": 1, "response": {"route": "filter"}})())
    assert cli.main(["--file", str(path), "--reps", "2", "--name", "failed"]) == 1
    report = json.loads(next((tmp_path / "results").glob("*.json")).read_text(encoding="utf-8"))
    assert report["experiment"] == "dialogue" and report["summary"]["failed"] == 2
    assert report["rows"][0]["turns"][0]["failures"][0]["actual"] == "filter"
    assert cli.main(["--file", str(path), "--validate-only"]) == 0
    assert cli.main(["--file", str(path), "--reps", "0"]) == 1


def test_cli_missing_key_before_data_copy(tmp_path, monkeypatch, capsys, offline):
    cli = script("eval_dialogue")
    path = tmp_path / "synthetic.jsonl"
    path.write_text(json.dumps(scenario()), encoding="utf-8")
    monkeypatch.setattr(cli, "working_data", lambda _: pytest.fail("키 없을 때 데이터 복사 금지"))
    assert cli.main(["--file", str(path)]) == 1
    assert "GMS_KEY" in capsys.readouterr().err


@pytest.mark.parametrize("fail", [False, True])
def test_snapshot_copy_checks_and_cleanup_without_mutating_source(tmp_path, monkeypatch, fail):
    settings = load_settings()
    settings["evaluation"]["snapshot_dir"] = str(tmp_path / "snapshots")
    source = snapshot.snapshot_path(settings, "fixed")
    (source / "chroma").mkdir(parents=True)
    (source / "chroma" / "marker").write_bytes(b"original")
    seen = []
    def load(config, name):
        path = snapshot.snapshot_path(config, name)
        assert path != source
        seen.append(path)
        (path / "chroma" / "marker").write_bytes(b"changed")
        if fail:
            raise ValueError("bad snapshot")
        return object(), {"name": name}
    monkeypatch.setattr(snapshot, "load_snapshot", load)
    with pytest.raises(ValueError) if fail else nullcontext():
        with snapshot.temporary_snapshot(settings, "fixed") as (_, manifest):
            assert manifest["name"] == "fixed"
    assert (source / "chroma" / "marker").read_bytes() == b"original"
    assert not seen[0].exists()


def test_both_evaluation_clis_leave_real_chroma_snapshot_bytes_unchanged(tmp_path, monkeypatch):
    from rag_service.vectorstore.store import get_collection
    from test_index_items_offline import cached

    settings = load_settings()
    settings["paths"]["chroma_dir"] = str(tmp_path / "source")
    settings["evaluation"].update(snapshot_dir=str(tmp_path / "snapshots"), results_dir=str(tmp_path / "results"))
    embeddings = cached(tmp_path)
    for name in settings["collections"].values():
        store = get_collection(name, embeddings, settings)
        store.add_texts(["synthetic"], ids=["A-1"], metadatas=[{"doc_id": "synthetic"}])
    snapshot.create_snapshot(settings, "fixed")
    snapshot.close_temporary_clients(tmp_path)
    source = snapshot.snapshot_path(settings, "fixed")
    before = {str(p.relative_to(source)): (p.read_bytes(), p.stat().st_mtime_ns)
              for p in source.rglob("*") if p.is_file()}
    opened = []

    def stores(client, *args):
        root = Path(client.get_settings().persist_directory)
        assert not root.is_relative_to(source)
        opened.append(root)
        docs = client.get_collection(settings["collections"]["docs"])
        docs.update(ids=["A-1"], metadatas=[{"doc_id": "write-to-evaluation-copy"}])
        return object(), object()

    for name in ("eval_retrieval", "compare_mmr"):
        cli = script(name)
        monkeypatch.setattr(cli, "guarded", lambda run: run())
        monkeypatch.setattr(cli, "configured", lambda _: settings)
        monkeypatch.setattr(cli, "read_cases", lambda *args: [{"id": "SYNTHETIC", "question": "Q", "set_num": "A-1"}])
        monkeypatch.setattr(cli, "retrieval_stores", stores)
        monkeypatch.setattr(cli, "contexts", lambda *args: ({"SYNTHETIC": ({}, [])}, []))
        if name == "eval_retrieval":
            monkeypatch.setattr(cli, "measure", lambda *args: ([], {}))
            monkeypatch.setattr(cli, "markdown", lambda *args: "synthetic result")
            argv = []
        else:
            monkeypatch.setattr(cli, "rank_docs", lambda *args, **kwargs: [])
            argv = ["--ids", "SYNTHETIC"]
        assert cli.main(["--snapshot", "fixed", "--name", name, *argv]) == 0
    after = {str(p.relative_to(source)): (p.read_bytes(), p.stat().st_mtime_ns)
             for p in source.rglob("*") if p.is_file()}
    assert after == before and all(not path.exists() for path in opened)
