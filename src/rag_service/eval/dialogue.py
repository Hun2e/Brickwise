"""비공개 문항과 독립적인 다턴 시나리오 계약·판정·격리 실행."""

import json
import re
import shutil
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Callable, Iterator
from uuid import uuid4

from rag_service.config import resolve_path, settings_context
from rag_service.eval.common import table
from rag_service.eval.snapshot import close_temporary_clients
from rag_service.services import chat, users
from rag_service.services.conversations import ConversationRepo
from rag_service.services.dialogue_state import DialogueState, validate_set_num
from rag_service.planner.collections import CollectionStore

EXPECT_KEYS = {
    "route", "subject", "subject_is_list_rank", "list_kind", "list_contains", "list_excludes",
    "collection", "mode", "compare_includes", "must_include", "must_not_include", "clarify",
}


def _collection(value: object) -> None:
    if not isinstance(value, dict):
        raise ValueError("컬렉션 기대값은 객체여야 합니다.")
    for number, qty in value.items():
        validate_set_num(number)
        if type(qty) is not int or qty < 1:
            raise ValueError("컬렉션 수량은 양의 정수여야 합니다.")


def validate_expect(expect: dict) -> None:
    """오타·타입 오류로 검사를 조용히 건너뛰지 않는다."""
    if not isinstance(expect, dict) or expect.keys() - EXPECT_KEYS:
        raise ValueError("지원하지 않는 기대값 키입니다.")
    enums = {"route": {"filter", "fact", "narrative", "compare", "build", "build_archive", "set_ops", "collection"},
             "mode": {"exact", "ignore_color"}, "list_kind": {"search", "recommend", "candidates"}}
    for key, choices in enums.items():
        if key in expect and (not isinstance(expect[key], str) or expect[key] not in choices):
            raise ValueError("기대값 종류를 확인하세요.")
    if expect.get("subject") is not None:
        validate_set_num(expect["subject"])
    if "subject_is_list_rank" in expect:
        rank = expect["subject_is_list_rank"]
        if type(rank) is not int or rank < 1:
            raise ValueError("목록 순번은 1부터 시작하는 정수여야 합니다.")
    if "clarify" in expect and type(expect["clarify"]) is not bool:
        raise ValueError("clarify는 불리언이어야 합니다.")
    if "collection" in expect:
        _collection(expect["collection"])
    for key in ("list_contains", "list_excludes", "compare_includes", "must_include", "must_not_include"):
        if key not in expect:
            continue
        values = expect[key]
        if not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values):
            raise ValueError("기대값 목록은 비어 있지 않은 문자열로 구성하세요.")
        if key in ("list_contains", "list_excludes", "compare_includes"):
            for value in values:
                validate_set_num(value)


def read_scenarios(path: Path) -> list[dict]:
    """호출자가 지정한 JSONL만 읽고 형식만 검증한다."""
    rows = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return validate_scenarios(rows)


def validate_scenarios(rows: list[dict]) -> list[dict]:
    ids = set()
    if not rows:
        raise ValueError("시나리오가 없습니다.")
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip():
            raise ValueError("시나리오 ID를 확인하세요.")
        if row["id"] in ids or not isinstance(row.get("title"), str):
            raise ValueError("시나리오 ID 중복 또는 제목 형식 오류입니다.")
        ids.add(row["id"])
        setup = row.get("setup", {})
        if not isinstance(setup, dict) or setup.keys() - {"collection"}:
            raise ValueError("시작 설정을 확인하세요.")
        collection = setup.get("collection", [])
        if not isinstance(collection, list):
            raise ValueError("시작 컬렉션은 목록이어야 합니다.")
        for pair in collection:
            if not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[0], str):
                raise ValueError("시작 컬렉션은 [세트 번호, 수량] 쌍이어야 합니다.")
            _collection({pair[0]: pair[1]})
        turns = row.get("turns")
        if not isinstance(turns, list) or not turns:
            raise ValueError("시나리오에 질문이 없습니다.")
        for turn in turns:
            if (not isinstance(turn, dict) or not isinstance(turn.get("q"), str)
                    or not turn["q"].strip() or turn.keys() - {"q", "expect"}):
                raise ValueError("턴의 질문·형식을 확인하세요.")
            validate_expect(turn.get("expect", {}))
    return rows


def normalize_numbers(text: str) -> str:
    """천 단위 구분 쉼표만 제거하고 일반 문장 쉼표는 보존한다."""
    return re.sub(r"(?<!\d)\d{1,3}(?:,\d{3})+(?!\d)", lambda m: m[0].replace(",", ""), text)


def judge(expect: dict, response: dict, before: DialogueState, after: DialogueState,
          collection: dict[str, int]) -> list[dict]:
    """지정된 항목만 검사한다. 순번 참조는 실행 전 목록을 기준으로 한다."""
    validate_expect(expect)
    actual = {"route": response.get("route"), "subject": after.subject.set_num if after.subject else None,
              "list_kind": after.last_list.kind if after.last_list else None,
              "collection": collection, "mode": after.mode, "clarify": after.pending is not None}
    items = list(after.last_list.items) if after.last_list else []
    answer = response.get("answer", "") or ""
    checks = []
    for key, wanted in expect.items():
        observed = actual.get(key)
        if key == "subject_is_list_rank":
            previous = list(before.last_list.items) if before.last_list else []
            target = previous[wanted - 1] if wanted <= len(previous) else None
            observed = {"subject": actual["subject"], "previous_list": previous, "rank_target": target}
            passed = target is not None and actual["subject"] == target
        elif key in ("list_contains", "list_excludes", "compare_includes"):
            observed = list(after.compare) if key == "compare_includes" else items
            passed = all((value not in observed if key == "list_excludes" else value in observed) for value in wanted)
        elif key in ("must_include", "must_not_include"):
            observed = answer
            normalized = normalize_numbers(answer)
            passed = all((normalize_numbers(value) not in normalized if key == "must_not_include"
                          else normalize_numbers(value) in normalized) for value in wanted)
        else:
            passed = observed == wanted
        checks.append({"key": key, "expected": wanted, "actual": observed, "passed": passed})
    return checks


@contextmanager
def working_data(settings: dict) -> Iterator[dict]:
    """운영 색인·캐시는 복사해 사용하고 참조 SQLite는 서비스의 읽기 전용 연결로 연다."""
    with TemporaryDirectory(prefix="rag-dialogue-data-") as directory:
        root = Path(directory)
        config = deepcopy(settings)
        # 각 경로를 따로 복사해 사용자 override로 분리된 저장소도 보호한다.
        for key in ("chroma_dir", "docs_dir", "collect_sqlite", "embed_cache"):
            source = resolve_path(settings["paths"][key])
            target = root / key
            if source.is_dir():
                shutil.copytree(source, target)
            elif source.is_file():
                shutil.copy2(source, target)
            config["paths"][key] = str(target)
        config["paths"]["collected_dir"] = str(root)
        try:
            yield config
        finally:
            close_temporary_clients(root)


def evaluate(scenarios: list[dict], settings: dict, *, reps: int,
             turn_fn: Callable | None = None) -> tuple[list[dict], dict]:
    """시나리오·반복마다 새 DB를 만들어 실제 공용 채팅 진입점을 호출한다."""
    validate_scenarios(scenarios)
    if type(reps) is not int or reps < 1:
        raise ValueError("반복 수는 양의 정수여야 합니다.")
    turn_fn = turn_fn or chat.chat_turn_v2
    rows = []
    for scenario in scenarios:
        for repetition in range(1, reps + 1):
            with TemporaryDirectory(prefix="rag-dialogue-user-") as directory:
                config = deepcopy(settings)
                config["paths"]["user_sqlite"] = str(Path(directory) / "user.sqlite")
                with settings_context(config):
                    uid = users.new_user_id()
                    repo = ConversationRepo(Path(config["paths"]["user_sqlite"]))
                    cid = repo.create(uid)
                    store = CollectionStore(repo.path)
                    store.set_sets(uid, scenario.get("setup", {}).get("collection", []), ensure=True)
                    turns = []
                    for index, step in enumerate(scenario["turns"], 1):
                        before, _ = repo.load_state(uid, cid)
                        started = perf_counter()
                        error = None
                        response = {}
                        try:
                            turn = turn_fn(uid, cid, step["q"], request_id=uuid4().hex)
                            response = turn.response
                            if not turn.id:
                                error = "턴이 저장되지 않았습니다."
                        except Exception as exc:
                            # 예외 원문에는 키나 프롬프트가 있을 수 있어 결과에 넣지 않는다.
                            chat.orchestrator.log_exception(exc, config)
                            error = "턴 실행 예외: 입력·연결·DB 상태와 평가 로그를 확인하세요."
                        elapsed = (perf_counter() - started) * 1000
                        after, _ = repo.load_state(uid, cid)
                        owned = dict(store.get(uid)["sets"])
                        if response.get("needs_input") and after.pending is None:
                            error = error or "되묻기 상태 없이 요청 처리가 중단됐습니다."
                        checks = judge(step.get("expect", {}), response, before, after, owned)
                        failures = [check for check in checks if not check["passed"]]
                        turns.append({"turn": index, "question": step["q"], "passed": not failures and error is None,
                                      "checks": checks, "failures": failures, "error": error, "elapsed_ms": elapsed,
                                      "response": response, "state_before": json.loads(before.to_json()),
                                      "state_after": json.loads(after.to_json()), "collection": owned})
                    rows.append({"id": scenario["id"], "title": scenario["title"], "repetition": repetition,
                                 "passed": all(t["passed"] for t in turns), "turns": turns,
                                 "clarifications": sum(t["state_after"]["pending"] is not None for t in turns)})
    return rows, summarize(rows)


def summarize(rows: list[dict]) -> dict:
    turns = [turn for row in rows for turn in row["turns"]]
    passed = sum(row["passed"] for row in rows)
    return {"scenario_runs": len(rows), "passed": passed, "failed": len(rows) - passed,
            "pass_rate": passed / len(rows) if rows else 0,
            "turns": len(turns), "turns_passed": sum(t["passed"] for t in turns),
            "clarifications": sum(row["clarifications"] for row in rows),
            "by_scenario": {ident: {"runs": len(group), "passed": sum(r["passed"] for r in group)}
                            for ident in dict.fromkeys(r["id"] for r in rows)
                            for group in [[r for r in rows if r["id"] == ident]]}}


def markdown(rows: list[dict], summary: dict) -> str:
    text = (f"시나리오 {summary['passed']}/{summary['scenario_runs']} 통과 · "
            f"턴 {summary['turns_passed']}/{summary['turns']} 통과 · 되묻기 {summary['clarifications']}회\n\n")
    text += table(["시나리오", "반복", "통과", "되묻기"],
                  [[r["id"], r["repetition"], r["passed"], r["clarifications"]] for r in rows])
    for row in rows:
        text += f"\n\n## {row['id']} · {row['title']} · 반복 {row['repetition']}\n\n"
        text += table(["턴", "질문", "통과", "실패 이유 (기대 / 실제)"], [
            [t["turn"], t["question"], t["passed"], t["error"] or "; ".join(
                f"{f['key']}: {json.dumps(f['expected'], ensure_ascii=False)} / "
                f"{json.dumps(f['actual'], ensure_ascii=False)}" for f in t["failures"])] for t in row["turns"]])
    return text + "\n\n운영 색인·수집 캐시의 임시 복사본과 반복별 새 user DB를 사용합니다. 실패 원인 분류는 PM이 원자료로 확인합니다."
