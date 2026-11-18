"""LLM 없이 문장·근거·표를 분리해서 반환한다."""

import sqlite3
import re
from dataclasses import dataclass, field, replace

from rag_service.config import load_settings
from rag_service.db.queries import FilterSpec, compare_versions, filter_sets, set_fact, set_ops
from rag_service.planner import (
    NoInventory, SetNotFound, analyze, collection_have, normalize_set_num, recommend, set_parts,
)
from rag_service.planner.recommend import Recommendation
from .db_cards import (
    analysis_card, compare_card, fact_card, filter_card, minifig_text, recommend_card, set_ops_card,
)
from .evidence import Card


@dataclass
class TemplateAnswer:
    answer: str
    cards: list[Card]
    tables: list[dict]
    warnings: list[str]
    items: list[dict] = field(default_factory=list)


def particle(word: str, pair: str) -> str:
    """한글·숫자 독음과 영문 어미의 한국어 발음에 맞게 조사를 고른다."""
    closed, opened = pair.split("/")
    word = word.rstrip().rstrip("'\"’”)]} ")
    last = word[-1:]
    if last and "가" <= last <= "힣":
        has_final = (ord(last) - ord("가")) % 28 != 0
    elif re.search(r"[A-Za-z]$", word):
        ending = re.search(r"[A-Za-z]+$", word)[0].lower()
        # Fighter/Destroyer의 r은 '어', Falcon/Wing의 n/ng는 받침으로 읽는다.
        has_final = ending.endswith(("l", "m", "n", "ng"))
    else:
        has_final = last in "013678" if last else False
    return closed if has_final else opened


def _table(title: str, columns: list[str], rows: list[list], warnings: list[str],
           formats: dict[str, str]) -> dict:
    """원시값과 열별 표시 형식을 분리한다. percent의 원시값은 0~1 비율이다."""
    maximum = load_settings()["compare"]["table_max_rows"]
    if len(rows) > maximum:
        warnings.append(f"{title}: 전체 {len(rows):,}행 중 {maximum:,}행을 표시합니다.")
    return {"title": title, "columns": columns, "rows": rows[:maximum], "formats": formats}


def _error(exc: SetNotFound | NoInventory) -> TemplateAnswer:
    if isinstance(exc, NoInventory):
        return TemplateAnswer("부품 목록이 없는 세트입니다. 다른 세트를 선택하세요.", [], [], [])
    tables = []
    if exc.candidates:
        tables.append({"title": "세트 후보", "columns": ["세트 번호", "이름"],
                       "rows": [[r.set_num, r.name] for r in exc.candidates], "formats": {}})
    return TemplateAnswer("데이터에 없는 세트 번호입니다. 번호 또는 후보를 확인하세요.", [], tables, [])


def _mismatch(number: str, official: int, aggregate: int) -> list[str]:
    return ([f"{number}: 부품 수(Rebrickable 기준) {official:,}개와 부품 목록 집계 {aggregate:,}개가 다릅니다 (원본 불일치)."]
            if official != aggregate else [])


def answer_fact(conn: sqlite3.Connection, set_num: str, question: str) -> TemplateAnswer:
    """질문한 속성만 답하고 개요 요청일 때 전체 속성을 제공한다."""
    from rag_service.data_prep.unsupported_attrs import unsupported_answer

    unsupported = unsupported_answer(question)
    if unsupported:
        return TemplateAnswer(unsupported, [], [], [])
    try:
        f = set_fact(conn, set_num)
    except SetNotFound as exc:
        return _error(exc)
    details = {"parts": f"부품 수(Rebrickable 기준)는 {f.num_parts:,}개",
               "year": f"출시 연도는 {f.year}년", "figs": minifig_text(f),
               "theme": f"서브테마는 {f.subtheme}"}
    question = question.casefold()
    order = [key for key, words in (
        ("figs", ("미니피그", "피규어", "누가 들어", "minifig")),
        ("year", ("연도", "출시", "언제", "몇 년", "몇년도", "year", "released")),
        ("parts", ("부품", "조각", "parts", "pieces")),
        ("theme", ("테마", "theme")),
    ) if any(word in question for word in words)]
    if not order and any(word in question for word in ("몇 개", "how many")):
        order = ["parts"]
    if not order:
        order = list(details)
    answer = f"{f.set_num} {f.name}: " + " · ".join(details[key] for key in order) + ". [근거 1]"
    return TemplateAnswer(answer, [fact_card(f, 1)], [], _mismatch(f.set_num, f.num_parts, f.agg_parts))


def answer_set_ops(conn: sqlite3.Connection, a: str, b: str, mode: str = "exact") -> TemplateAnswer:
    try:
        r = set_ops(conn, a, b, mode)
    except (SetNotFound, NoInventory) as exc:
        return _error(exc)
    warnings = [warning for f in r.facts for warning in _mismatch(f.set_num, f.num_parts, f.agg_parts)]
    answer = (f"{r.a}{particle(r.a, '과/와')} {r.b}의 공통 부품은 {r.common_lots:,}종, {r.common_qty:,}개입니다 ({r.mode}). "
              f"색상을 무시하면 {r.common_lots_ignore_color:,}종입니다. [근거 1]")
    table = _table("공통 부품", ["부품 번호", "색상 ID", "이름", "공통 수량"],
                   [list(row) for row in r.top_common], warnings, {"공통 수량": "count"})
    return TemplateAnswer(answer, [set_ops_card(r, 1)], [table], warnings)


def _collection(conn: sqlite3.Connection, collection: dict) -> tuple:
    """CollectionStore.get 결과를 받고 모든 보유 세트의 인벤토리를 확인한다."""
    sets = collection.get("sets", [])
    adjustments = collection.get("adjustments", [])
    have = collection_have(conn, sets, adjustments)
    owned = {normalize_set_num(conn, number) for number, _ in sets}
    for number in sorted(owned):
        if not +set_parts(conn, number):
            raise NoInventory(number)
    return have, owned


def _empty_collection() -> TemplateAnswer:
    return TemplateAnswer("컬렉션이 비어 있습니다. 보유 세트 또는 부품을 먼저 등록하세요.", [], [], [])


def answer_build_analysis(conn: sqlite3.Connection, collection: dict, target: str, mode: str) -> TemplateAnswer:
    try:
        have, _ = _collection(conn, collection)
        if not have:
            return _empty_collection()
        r = analyze(conn, have, target, mode)
    except (SetNotFound, NoInventory) as exc:
        return _error(exc)
    warnings = _mismatch(r.target.set_num, r.official_num_parts, r.need_total)
    label = "색상 무시" if mode == "ignore_color" else "정확 일치"
    answer = f"{r.target.set_num} {r.target.name}: {label} 기준으로 " + _build_sentence(r.total_missing, r.lots_missing)
    card = analysis_card(r, 1)
    if mode == "ignore_color":
        exact = analyze(conn, have, target, "exact")
        answer += f" 정확 일치 기준으로는 {exact.total_missing:,}개 부족해요."
        card = replace(card, text=card.text + f"\n정확 일치 부족 수량: {exact.total_missing:,}개")
    answer += " [근거 1]"
    rows = [[row.part_num, row.part_name,
             "색상 무시" if row.color_id is None else f"{row.color_name or '미상'} ({row.color_id:,})",
             row.need, row.have, row.missing] for row in r.rows]
    table = _table("부족 부품", ["부품 번호", "이름", "색상", "필요", "보유", "부족"], rows, warnings,
                   {"필요": "count", "보유": "count", "부족": "count"})
    return TemplateAnswer(answer, [card], [table], warnings)


def _build_sentence(missing: int, lots: int) -> str:
    if missing == 0:
        return "지금 가진 부품으로 만들 수 있어요."
    return f"{missing:,}개({lots:,}종)만 더 있으면 완성할 수 있어요."


def answer_build_recommend(conn: sqlite3.Connection, collection: dict, mode: str, filters: dict) -> TemplateAnswer:
    """filters는 planner.recommend의 scope/year_min/year_max/parts_min/parts_max/top_n."""
    try:
        have, owned = _collection(conn, collection)
        if not have:
            return _empty_collection()
        recs = recommend(conn, have, owned, mode=mode, **filters)
    except (SetNotFound, NoInventory) as exc:
        return _error(exc)
    return answer_recommendations(recs, mode)


def answer_recommendations(recs: list[Recommendation], mode: str) -> TemplateAnswer:
    """정제된 추천도 같은 근거·표·카드 원천으로 표시한다."""
    warnings = [warning for r in recs for warning in _mismatch(r.set_info.set_num, r.set_info.num_parts, r.need_total)]
    answer = (f"추천 목록의 첫 세트인 {recs[0].set_info.set_num} {recs[0].set_info.name}: "
              + _build_sentence(recs[0].total_missing, recs[0].lots_missing) + " [근거 1]"
              if recs else "조건에 맞는 추천 결과가 없습니다. [근거 1]")
    rows = [[r.set_info.set_num, r.set_info.name, r.set_info.year, r.total_missing,
             r.lots_missing, r.coverage] for r in recs]
    table = _table("제작 추천", ["세트 번호", "이름", "연도", "부족 개수", "부족 종류", "커버리지"], rows, warnings,
                   {"연도": "year", "부족 개수": "count", "부족 종류": "count", "커버리지": "percent"})
    visible = recs[:len(table["rows"])]
    items = [{"rank": i, "set_num": r.set_info.set_num, "name": r.set_info.name,
              "year": r.set_info.year, "num_parts": r.set_info.num_parts,
              "missing": r.total_missing, "lots_missing": r.lots_missing}
             for i, r in enumerate(visible, 1)]
    return TemplateAnswer(answer, [recommend_card(recs, mode, 1)], [table], warnings, items)


def answer_compare(conn: sqlite3.Connection, set_num: str) -> TemplateAnswer:
    try:
        r = compare_versions(conn, set_num, load_settings())
    except SetNotFound as exc:
        return _error(exc)
    warnings = list(r.warnings)
    for row in r.rows:
        for number, total in row["agg_parts"].items():
            warnings.extend(_mismatch(number, row["num_parts"], total))
    fields = ["set_num", "name", "year", "num_parts", "minifig_count", "subtheme", "scope"]
    table = _table("판본 비교", ["세트 번호", "이름", "연도", "부품 수(Rebrickable 기준)", "미니피그 수", "서브테마", "범위"],
                   [[row[key] for key in fields] for row in r.rows], warnings,
                   {"연도": "year", "부품 수(Rebrickable 기준)": "count", "미니피그 수": "count"})
    answer = (f"{r.entity} 판본 {len(r.rows):,}개를 비교했습니다. "
              f"기타 변형 {len(r.excluded):,}개: "
              + (", ".join(name for _, name in r.excluded) or "없음") + ". [근거 1]")
    return TemplateAnswer(answer, [compare_card(r, 1)], [table], warnings)


def answer_filter(conn: sqlite3.Connection, spec: FilterSpec) -> TemplateAnswer:
    results, total = filter_sets(conn, spec)
    warnings = [warning for f in results for warning in _mismatch(f.set_num, f.num_parts, f.agg_parts)]
    table = _table("조건 검색", ["세트 번호", "이름", "연도", "부품 수(Rebrickable 기준)", "서브테마"],
                   [[f.set_num, f.name, f.year, f.num_parts, f.subtheme] for f in results], warnings,
                   {"연도": "year", "부품 수(Rebrickable 기준)": "count"})
    label = "조건에 맞는 세트"
    answer = f"{label}{particle(label, '은/는')} {total:,}개이며 {len(results):,}개를 반환합니다. [근거 1]"
    return TemplateAnswer(answer, [filter_card(results, total, spec, 1)], [table], warnings)
