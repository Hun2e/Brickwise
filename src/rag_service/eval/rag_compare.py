"""단독 모델과 서비스의 답변 비교. 자동 판정은 사람 채점의 보조 자료다."""

import re
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import InvalidOperation
from pathlib import Path
from time import perf_counter
from typing import Any, Callable

from langchain_core.messages import HumanMessage, SystemMessage

from rag_service.config import resolve_path
from rag_service.eval.common import digest, safe_name, table, write_json
from rag_service.rag.citations import filter_cited, parse_citations
from rag_service.rag.prompts import SOLO_BASELINE, SOLO_BASELINE_VERSION
from rag_service.rag.verify import _NUMBER, _SETNUM, _normalize, split_sentences

_LABEL = re.compile(
    r"(?P<minifigs>미니\s*피규어|미니\s*피그|피규어|피겨|미니피겨|mini\s*fig(?:ure)?s?)"
    r"|(?P<num_parts>부품|피스|조각|pieces?\b|parts?\b)"
    r"|(?P<year>출시\s*연도|출시\s*년도|연도|년도|출시|발매|year\b|released?\b)", re.I)
_CITATION = re.compile(r"\[근거\s*[\d,\s]+\]")
_SET = re.compile(r"(?<![\d,])\d{5}(?:-\d{1,2})?(?![\d,]|\s*(?:개|년|명|종|원|피스))")
_UNKNOWN = re.compile(
    r"확인(?:되지\s*(?:않|못)|할\s*수\s*없|이?\s*(?:불가|어렵))"
    r"|알\s*(?:수\s*없|지\s*못)|모르(?:겠|는|다|며|고)|모릅니다|모른다|모름"
    r"|(?:정보|자료|근거)(?:가|는)?\s*(?:없|부족)"
    r"|불확실|확실하지\s*않|\b(?:unknown|unconfirmed)\b|\bi\s+(?:do\s+not|don't)\s+know\b", re.I)


def says_unknown(answer: str) -> bool:
    """모름 표현의 존재만 센다. 적절한 거절인지는 사람이 판정한다."""
    return bool(_UNKNOWN.search(answer))


def _clean(text: str) -> str:
    return re.sub(r"[*_`]", "", _CITATION.sub("", text))


def _set_id(value: str) -> str:
    return value if "-" in value else value + "-1"


def _prose_values(text: str, context: str | None) -> list[tuple[str, str | None, str]]:
    """같은 절의 단위·속성명으로 분류하며 세트 번호와 인용 번호는 제외한다."""
    text = _clean(text)
    sets = list(_SET.finditer(text))
    labels = list(_LABEL.finditer(text))
    numbers = list(_NUMBER.finditer(text))
    result = []
    for index, match in enumerate(numbers):
        if _SETNUM.fullmatch(match.group()) or any(s.start() == match.start() for s in sets):
            continue
        previous = numbers[index - 1].end() if index else 0
        following = numbers[index + 1].start() if index + 1 < len(numbers) else len(text)
        before = [label for label in labels if previous <= label.start() and label.end() <= match.start()]
        after = [label for label in labels if match.end() <= label.start() < following]
        suffix = text[match.end():following]
        if re.match(r"\s*년", suffix):
            kind = "year"
        elif re.match(r"\s*(?:피스|조각|pieces?\b|parts?\b)", suffix, re.I):
            kind = "num_parts"
        elif before:
            kind = before[-1].lastgroup
        elif after and not re.search(r"[,;。.!?]|이고|이며", text[match.end():after[0].start()]):
            kind = after[0].lastgroup
        elif re.match(r"\s*개", suffix):
            kind = "num_parts"
        else:
            continue
        preceding_sets = [s for s in sets if s.end() <= match.start()]
        number_set = _set_id(preceding_sets[-1].group()) if preceding_sets else context
        # 'A와 B는 각각 ...'은 가까운 번호로 추측하지 않는다.
        if len(preceding_sets) > 1 and not _NUMBER.search(
                text[preceding_sets[-2].end():preceding_sets[-1].start()]):
            number_set = None
        result.append((kind, number_set, _normalize(match.group())))
    return result


def _observations(answer: str) -> list[tuple[str, str | None, str]]:
    result, headers = [], []
    context = None
    for line in answer.splitlines():
        clean = _clean(line)
        if "|" in clean:
            cells = [cell.strip() for cell in clean.strip().strip("|").split("|")]
            if all(re.fullmatch(r":?-+:?", cell) for cell in cells):
                continue
            if not headers:
                headers = cells
                continue
            row_sets = [_set_id(m.group()) for cell in cells for m in _SET.finditer(cell)]
            row_context = row_sets[0] if len(row_sets) == 1 else context
            for header, cell in zip(headers, cells):
                header_sets = list(_SET.finditer(header))
                if header_sets:
                    result.extend(_prose_values(cells[0] + " " + cell, _set_id(header_sets[0].group())))
                else:
                    result.extend(_prose_values(header + " " + cell, row_context))
            continue
        headers = []
        for sentence in split_sentences(clean):
            result.extend(_prose_values(sentence, context))
            sets = list(_SET.finditer(sentence))
            if sets:
                context = _set_id(sets[0].group()) if len(sets) == 1 else None
    return result


def check_facts(answer: str, facts: list[dict]) -> list[dict]:
    """DB 기준값과 속성·세트별 수치를 비교한다. 모호한 대응은 missing이다."""
    observations = _observations(answer)
    result = []
    for fact in facts:
        kind, _, number = fact["field"].partition(":")
        if kind not in {"num_parts", "year", "minifigs"} or (number and not _SETNUM.fullmatch(number)):
            raise ValueError("숫자 기준의 field 형식을 확인하세요.")
        try:
            expected = _normalize(str(fact["value"]))
            if not re.fullmatch(r"\d+", expected):
                raise ValueError
        except (InvalidOperation, ValueError):
            raise ValueError("숫자 기준값은 음이 아닌 정수여야 합니다.") from None
        found = list(dict.fromkeys(value for field, set_num, value in observations
                                   if field == kind and (not number or set_num == number)))
        # 정답과 오답을 함께 말한 경우도 검토에서 놓치지 않는다.
        status = "missing" if not found else "correct" if found == [expected] else "wrong"
        result.append({"field": fact["field"], "expected": fact["value"], "status": status, "found": found})
    return result


def _usage(raw: Any) -> dict:
    tokens = getattr(raw, "usage_metadata", None) or (
        getattr(raw, "response_metadata", {}) or {}).get("token_usage", {})
    result = {"calls": 1}
    for key, legacy in (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens")):
        value = tokens.get(key, tokens.get(legacy))
        if type(value) is int and value >= 0:
            result[key] = value
    return result


def _assessment(answer: str, facts: list[dict]) -> dict:
    return {"facts": check_facts(answer, facts), "says_unknown": says_unknown(answer),
            "citation_count": len(parse_citations(answer)),
            "citation_mentions": len(_CITATION.findall(answer))}


def run_case(case: dict, *, solo_model: Any, answer_fn: Callable, settings: dict) -> dict:
    """A에는 역할과 질문만 보내고 B에는 선택 세트와 같은 질문을 전달한다."""
    if settings["llm"]["cache_enabled"] or not settings["llm"]["send_temperature"] or settings["llm"]["temperature"] != 0:
        raise ValueError("비교에는 LLM 캐시 해제와 temperature 0 전송이 필요합니다.")
    check_facts("", case["facts"])
    at = datetime.now(timezone.utc).isoformat()
    started = perf_counter()
    raw = solo_model.invoke([SystemMessage(content=SOLO_BASELINE), HumanMessage(content=case["question"])])
    solo_ms = (perf_counter() - started) * 1000
    content = raw if isinstance(raw, str) else raw.content
    if isinstance(content, list):
        content = "\n".join(block if isinstance(block, str) else block.get("text", "")
                            for block in content if isinstance(block, (str, dict)))
    if not isinstance(content, str) or not content.strip():
        raise ValueError("단독 모델이 빈 답변을 반환했습니다.")
    solo = {"answer": content, "elapsed_ms": solo_ms, "usage": _usage(raw), "warnings": [],
            **_assessment(content, case["facts"])}
    started = perf_counter()
    response = answer_fn(case["question"], selected_set=case["set_num"])
    elapsed = (perf_counter() - started) * 1000
    used, _, warnings = filter_cited(response.answer, response.cards)
    service = {**asdict(response), "elapsed_ms": elapsed,
               "cards": [asdict(card) for card in used], "cited_card_count": len(used),
               "warnings": list(dict.fromkeys(response.warnings + warnings)),
               **_assessment(response.answer, case["facts"])}
    return {**case, "started_at": at, "model": settings["llm"]["chat_model"],
            "settings_hash": digest(settings), "solo_prompt_version": SOLO_BASELINE_VERSION,
            "A": solo, "B": service}


def report_paths(settings: dict, name: str, at: datetime) -> tuple[Path, Path, Path]:
    base = resolve_path(settings["evaluation"]["results_dir"]) / f"{at:%Y-%m-%d}_{safe_name(name)}"
    paths = tuple(base.with_name(base.name + suffix) for suffix in (".json", ".md", "-scoring.md"))
    if any(path.exists() for path in paths):
        raise ValueError("같은 날짜·실험명 결과가 있습니다. 새 --name을 지정하세요.")
    return paths


def _quoted(answer: str) -> str:
    return "\n".join("> " + line for line in answer.splitlines())


def write_reports(rows: list[dict], *, settings: dict, name: str, started_at: datetime,
                  dataset_hash: str) -> tuple[Path, Path, Path]:
    """자동 판정과 빈 사람 채점 칸을 분리하고 답변 전문을 보존한다."""
    paths = report_paths(settings, name, started_at)
    metadata = {"schema_version": 1, "experiment": "rag-compare", "started_at": started_at.isoformat(),
                "finished_at": datetime.now(timezone.utc).isoformat(), "model": settings["llm"]["chat_model"],
                "settings": settings, "settings_hash": digest(settings), "dataset_hash": dataset_hash,
                "solo_prompt_version": SOLO_BASELINE_VERSION, "repetitions": 1,
                "mode": "live", "collection_cache_preserved": True}
    intro = (f"모델: {metadata['model']} · 프롬프트: {SOLO_BASELINE_VERSION} · 시작(UTC): {started_at.isoformat()}\n\n"
             f"설정 SHA-256: `{metadata['settings_hash']}`\n\n"
             "A/B 각 1회, temperature 0, LLM 캐시 해제. B는 운영 색인과 질문 시점 수집을 사용하며 "
             "수집·문서·임베딩 캐시는 유지합니다. 서비스 내부 재시도는 기본 설정을 따릅니다.\n\n"
             "자동 판정은 숫자 문맥과 모름 표현의 존재만 확인합니다. 정확성·근거의 적절성·모름 처리는 사람이 "
             "각각 0/1/2점으로 채점합니다. 모호한 세트 대응·숫자 없는 표현은 missing으로 남깁니다. "
             "correct/wrong/missing은 답변 본문만 대상으로 하며 카드·별도 표의 숫자를 대신 채점하지 않습니다.\n\n"
             "공식 부품 수와 Rebrickable 집계가 다를 수 있습니다(75257 공식 1,351 / DB 기준 1,328). "
             "수치는 PM 확정 facts를 기준으로 판정합니다. 토큰은 반환된 usage만 기록하며 미보고 값을 0으로 추정하지 않습니다.\n\n"
             "citation_count는 고유 인용 번호 수, citation_mentions는 인용 표기 출현 수, cited_card_count는 실제 인용 카드 수입니다.")
    comparison, details, scoring = [], [], []
    for row in rows:
        scoring.append(f"## {row['id']} · {row['type']}\n\n{row['question']}\n\n메모: {row.get('note', '')}")
        details.append(f"## {row['id']}\n\n{row['question']}")
        for side in ("A", "B"):
            data = row[side]
            counts = Counter(fact["status"] for fact in data["facts"])
            sentences = split_sentences(data["answer"])
            excerpt = sentences[0].strip() if sentences else ""
            comparison.append([row["id"], side, excerpt, counts["correct"], counts["wrong"],
                               counts["missing"], data["says_unknown"], data["citation_count"],
                               data.get("cited_card_count", "—"), round(data["elapsed_ms"], 2)])
            full = f"### {side}\n\n{_quoted(data['answer'])}\n\n"
            full += table(["필드", "기준", "판정", "발견"], [
                [f["field"], f["expected"], f["status"], ", ".join(f["found"])] for f in data["facts"]])
            full += "\n\n경고: " + (" / ".join(data["warnings"]) or "없음")
            if side == "B":
                full += f"\n\n추가 입력/실패 안내: {data['needs_input'] or '없음'}\n\n" + table(
                    ["근거", "출처 유형", "제목", "URL"],
                    [[c["n"], c["source_type"], c["title"], c["url"]] for c in data["cards"]])
            details.append(full)
            scoring.append(full + "\n\n" + table(["기준 (0/1/2점)", "점수", "메모"],
                                                   [[criterion, "", ""] for criterion in
                                                    ("사실 정확성", "근거 제시", "모름 처리")]))
    write_json(paths[0], {**metadata, "rows": rows})
    paths[1].write_text(f"# {name}\n\n{intro}\n\n" + table(
        ["문항", "구분", "답변 첫 문장", "정확", "불일치", "누락", "모름 표현", "고유 인용", "인용 카드", "ms"], comparison)
        + "\n\n# 답변 전문과 자동 판정\n\n" + "\n\n".join(details) + "\n", encoding="utf-8")
    paths[2].write_text(f"# {name} 사람 채점 시트\n\n{intro}\n\n" + "\n\n".join(scoring) + "\n", encoding="utf-8")
    return paths
