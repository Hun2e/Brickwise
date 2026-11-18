# T-018 RAG 사용 전후 비교 (F2015)

- 상태: 완료 (2026-10-22)
- 관련 요구사항: **F2015**(RAG 사용 전후 답변 비교 문서, 대표 질문 5개 이상), F2007(근거 없으면 "확인되지 않음"), F2008(근거 표시)
- 선행 작업: T-017(검색 기본값 ADR-017), ADR-016 ⑤(같은 모델 단독 vs 서비스, 8문항, 3기준, 숫자는 DB 채점)

## 목표
같은 질문을 (A) LLM 단독과 (B) 서비스(RAG + DB 카드 + 수치 검증)에 각각 물어, 사실 정확성·근거 제시·모름 처리를 비교한 문서 `docs/04_RAG전후비교.md`를 만든다. Codex는 실행·자동 채점 스크립트를 만들고, 문항 작성·사람 채점·문서 작성은 PM이 한다.

## 실측 (PM, 2026-10-22)
| 항목 | 현재 상태 | 의미 |
|---|---|---|
| 산출물 템플릿 | `docs/04_RAG전후비교.md`에 3기준 표 골격이 있다 | 이 형식을 채운다 |
| 평가 파일 스키마 | `eval/rag_compare.jsonl` 스키마(T-016): `type: fact\|compare\|narrative\|unanswerable`, `facts: [{field, value}]` | 문항은 PM이 작성한다 |
| 재사용 코드 | `scripts/compare_p3.py`(같은 카드로 두 프롬프트 비교), `orchestrator.answer()`, `verify.py`(숫자 추출·정규화) | B는 서비스 그대로 실행하고, 숫자 채점은 verify의 정규화를 재사용한다 |
| 모델 | gpt-5.4-nano, temperature 0 전송, LLM 캐시 있음 | A·B 같은 모델. 비교 실행에서는 **LLM 캐시를 끈다**(`llm.cache_enabled=false`). 질문 시점 수집 캐시는 유지한다 |
| 화면 | Streamlit·웹이 근거 패널을 보여 준다 | 대표 2문항은 웹 화면 캡처를 문서에 넣는다 |

## 범위
- 포함 (Codex)
  1. `scripts/compare_rag.py`: `eval/rag_compare.jsonl`의 각 문항을 실행한다.
     - **A**: 단독 모델(검토 포인트 ①의 프롬프트, 근거 없음)
     - **B**: `orchestrator.answer(question, selected_set=set_num)`. 운영 색인을 쓰고 질문 시점 수집을 포함한다.
     - 두 답변·지연·토큰·B의 인용 카드(번호·출처 유형·제목·URL)·경고를 JSON으로 저장하고 Markdown 비교표를 만든다.
  2. **자동 채점** (`src/rag_service/eval/rag_compare.py`)
     - `facts`의 숫자 값(부품 수·연도·미니피그 수 등)이 답변에 **정확히** 나오는지(콤마·단위 정규화), DB와 **다른 숫자**를 말했는지 판정한다(검토 포인트 ③).
     - "확인되지 않음/모르겠다" 류 표현을 감지한다(모름 처리 판정 보조).
     - B 답변의 `[근거 N]` 수와 인용 카드 수를 센다.
  3. 사람 채점용 시트 `eval/results/<날짜>_rag-compare-scoring.md`: 문항마다 A/B 답변 전문과 빈 채점 칸(3기준 × 0/1/2점, 메모)을 둔다.
  4. 테스트: 숫자 판정(정확·불일치·누락·콤마·"7,541개"), 모름 표현 감지, 대역 모델로 A/B 실행 흐름.
- 포함 (PM)
  - `eval/rag_compare.jsonl` 8문항 작성(DB에서 `facts` 값 채움)
  - 사람 채점, `docs/04_RAG전후비교.md` 작성, 웹 화면 캡처 2장
- 제외
  - 서비스 동작 변경, "검색만 RAG" 중간 단계(ADR-016 ⑤에서 제외), LLM 판정기

## 문항 구성 (ADR-016 ⑤, PM 작성 예정)
| 유형 | 수 | 의도 | 예시 (초안) |
|---|---|---|---|
| fact | 3 | 단독 모델이 숫자를 지어내기 쉬움 | "75313 AT-AT UCS는 부품이 몇 개야?" / "75257 밀레니엄 팔콘에는 미니피그가 몇 개 들어 있어?" / 최신 2025~2026 세트 출시 연도 |
| compare | 2 | 판본 혼동 | "75192와 75257 밀레니엄 팔콘 차이" / AT-AT 판본 비교 |
| narrative | 2 | 세계관 서술 + 근거 표시 | "보바 펫은 어떤 캐릭터야?"(75312-1) / 레드 파이브 활약 |
| unanswerable | 1 | 데이터에 없는 정보 | "75192 한국 정가는 얼마야?" (LEGO.com 미수집 → 확인되지 않음이 정답) |

## 인터페이스 계약
```python
# src/rag_service/eval/rag_compare.py
def check_facts(answer: str, facts: list[dict]) -> list[dict]
#   → [{"field", "expected", "status": "correct" | "wrong" | "missing", "found": [...]}]
#   wrong = 같은 종류의 다른 숫자를 말함(예: 부품 수 문맥에서 7,541 대신 7,500)
def says_unknown(answer: str) -> bool
def run_case(case: dict, *, solo_model, answer_fn, settings) -> dict   # A·B 실행과 자동 채점 결과
```

## 완료 기준 (DoD)
- [ ] `compare_rag.py --file eval/rag_compare.jsonl --name rag-compare`로 8문항 A/B 결과 JSON·비교 Markdown·사람 채점 시트를 만든다
- [ ] A 실행에는 검색·DB·수집이 전혀 연결되지 않는다(테스트로 확인). B는 서비스 기본 설정(ADR-017)을 그대로 쓴다
- [ ] 비교 실행은 LLM 캐시를 우회하고, 결과에 모델명·설정 해시·실행 시각을 기록한다
- [ ] 자동 숫자 판정이 정확·불일치·누락을 구분한다(단위 테스트)
- [ ] 키가 없으면 한 줄 안내 후 종료. 예외는 파일 로그에 남긴다(T-016 남은 이슈 4를 이 스크립트에서는 처음부터 지킨다)
- [ ] `.venv\Scripts\python -m pytest -q` 전체 통과 (현재 904)

## 검증 명령
```powershell
.venv\Scripts\python scripts\compare_rag.py --file eval\rag_compare.jsonl --name rag-compare
.venv\Scripts\python -m pytest -q
```

## 검토 포인트 결정 (사용자, 2026-10-22)
1. **A 프롬프트**: 짧은 역할만 준다. 시스템 메시지는 "당신은 레고 스타워즈 전문가다. 한국어로 답하라. 확실하지 않거나 모르는 것은 모른다고 말하라." 사용자 메시지는 질문(세트 번호·이름 포함)이다. 문구는 `rag/prompts.py`에 `SOLO_BASELINE`(버전 `solo-v1`)으로 둔다.
2. **실행 횟수**: A·B 각 1회. temperature 0, LLM 캐시를 우회한다.
3. **숫자 기준**: DB 값과 일치해야 정답이다. 문서에는 "공식 부품 수와 Rebrickable 집계가 다를 수 있다"(예: 75257 공식 1,351 / DB 1,328)는 주석을 단다.
   - `facts.field`는 `num_parts`·`year`·`minifigs` 또는 `<field>:<set_num>`(비교 문항) 형식이다.
   - 판정은 숫자 문맥(부품·피스·개 / 년 / 미니피그·피규어) 기준으로 한다.
4. **문항·채점**: PM이 초안을 만들고 사용자가 확인한다. 채점은 PM이 3기준 × 0/1/2점으로 하고 사용자가 검토한다.
5. **문서 형식**(기본안 적용): 본문 표에는 핵심 문장과 점수를 넣고, 답변 전문은 부록에 둔다.
6. **문항 확정**(사용자 확인): `eval/rag_compare.jsonl` 8문항(B-01~B-08)이다. Codex는 형식 검증·실행에만 쓴다.

---

## 결과 보고 (Codex 작성)
- 변경 파일:
  - `scripts/compare_rag.py`: 확정 JSONL 검증, 동일 모델 A/B 각 1회 실행, LLM 캐시 해제, 운영 서비스 연결, 결과 3종 저장, 키 누락 안내·파일 오류 로그.
  - `src/rag_service/eval/rag_compare.py`: 숫자 속성·세트별 자동 판정, 모름 표현 감지, 실제 인용 카드 필터링, 실행 메타데이터와 비교표·사람 채점 시트 생성.
  - `src/rag_service/rag/prompts.py`: 승인 문구 그대로 `SOLO_BASELINE`, `SOLO_BASELINE_VERSION = "solo-v1"` 추가. 기존 서비스 프롬프트는 변경하지 않았다.
  - `tests/test_rag_compare.py`: 신규 오프라인 테스트 44개.
  - 이 작업지시서의 결과 보고 섹션. 의존성 추가 없음. `eval/rag_compare.jsonl`, `docs/04_RAG전후비교.md`, 서비스 동작·운영 설정은 수정하지 않았다.
- 실행 명령과 결과:
  - `.venv\Scripts\python scripts\compare_rag.py --file eval\rag_compare.jsonl --name rag-compare` 및 `.venv\Scripts\python -m pytest -q`: 기존 샌드박스 제약으로 기반 Python 프로세스를 생성하지 못했다.
  - 대체 실행기 `C:\Users\SSAFY\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe`와 `PYTHONPATH=.venv/Lib/site-packages;src`, `PYTHON_DOTENV_DISABLED=1`, 빈 `GMS_KEY`, `PYTHONDONTWRITEBYTECODE=1`로 아래 검증을 수행했다. `.env` 열람·출력 및 실제 LLM·임베딩·위키 API 호출은 하지 않았다.
  - 대체 Python `-m pytest tests/test_rag_compare.py -q --basetemp=tests/.tmp-t018-focused2 -p no:cacheprovider`: **44 passed, 1.41초**. 첫 실행에서 발견한 괄호 구문 오류를 수정한 뒤 통과했다.
  - 대체 Python `scripts\compare_rag.py --file eval\rag_compare.jsonl --name rag-compare`: 키 없는 조건에서 **종료 코드 1**, `API 키가 설정되지 않았습니다. 실행 환경에 GMS_KEY를 설정하세요.` 한 줄 출력. DB·수집 초기화나 결과 파일 생성 없이 종료했다.
  - 대체 Python `-m pytest -q --basetemp=tests/.tmp-t018-full -p no:cacheprovider`: 최종 **948 passed, 1 warning, 135.42초**(기존 904 + 신규 44). 경고는 기존 수집 CLI 테스트의 `WIKI_CONTACT` 누락 안내다.
  - 확정 8문항으로 CLI를 대역 실행하여 A/B 각각 8회, JSON·비교 Markdown·채점 시트 3개 생성, A/B 16개 답변별 3기준 빈 채점 칸을 검증했다. 산출물은 pytest 임시 폴더에만 생성했으며 실제 실험 결과로 제출하지 않는다. 입력 파일 바이트 불변, 운영 검색·수집·경로 설정 유지, 예외 상세의 파일 기록, 중복 결과·기존 채점 시트 보호도 확인했다.
  - `git diff --check`: 통과. 수정 금지 두 파일의 diff 없음. commit·push·branch 작업 없음.
- 설계 판단과 이유:
  - A에는 승인된 시스템 문구와 원래 질문만 전달한다. 첫 A 호출 전에는 B의 DB·색인·수집 자원을 열지 않으며, A 모델에는 검색·DB·수집 함수나 `facts`를 전달하지 않는다.
  - B는 기존 `eval.runtime.dependencies(settings, live=True)`를 통해 `orchestrator.answer(question, selected_set=...)`를 호출한다. 이 팩토리는 운영 색인·질문 시점 수집과 수집 캐시를 유지하면서 RAG 하위 경로까지 같은 설정을 전달하므로 별도 서비스 구현이나 설정 재로딩을 피할 수 있다.
  - 설정 복사본의 `llm.cache_enabled`만 false로 바꾸고 A/B 모델의 LangChain 캐시도 false로 지정한다. 모델명·temperature 등은 운영 설정을 읽으며 temperature 0 전송 조건이 아니면 실행 전에 거절한다. 서비스 내부 재시도 정책은 유지한다.
  - `verify.py`의 숫자 토큰·정규화·문장 분리를 재사용한다. 부품·피스·개, 연도·년, 미니피그·피규어 문맥을 구분하고, 비교 문항에서는 세트 번호·제목·Markdown 표 헤더와 행을 연결한다. 세트 번호와 인용 번호를 정답 숫자로 오인하지 않으며, 정답과 다른 수치가 함께 나오면 보수적으로 wrong 처리한다.
  - `citation_count`는 고유 인용 번호 수, `citation_mentions`는 인용 표기 출현 수, `cited_card_count`는 실제 유효 카드 수다. 번호가 없는 카드와 인용되지 않은 카드는 제외하며 잘못된 인용은 경고로 보존한다.
  - JSON에는 A/B 답변 전문·지연·반환된 usage, B의 카드·경고·추가 입력 안내·표·수치 검증 결과를 기록한다. 모델명, 설정 SHA-256, 문항 해시, UTC 실행 시각, 프롬프트 버전도 남긴다. 미보고 토큰을 0으로 추정하지 않는다. 비교표에는 첫 문장과 자동 판정을, 부록과 채점 시트에는 답변 전문을 둔다. 사람 점수는 모두 비워 둔다.
- 남은 이슈 / PM 확인 필요:
  - **실제 API를 이용한 8문항 비교 결과는 아직 생성하지 않았다.** AGENTS.md의 개발 규칙인 “키가 필요한 검증은 키가 없을 때의 동작(안내 메시지)까지 확인하고, 실제 호출 결과는 PM이 확인한다”에 따라 PM이 키가 있는 환경에서 위 검증 명령으로 실행해야 한다. 정상 실행 시 `eval/results/<UTC 날짜>_rag-compare.json`, `.md`, `-scoring.md`가 생성된다. 기존 이름이 있으면 새 `--name`을 사용한다.
  - PM 환경의 `.venv`로 전체 테스트를 다시 확인하고, 실제 답변·인용 근거를 보고 3기준 × 0/1/2점 채점, 대표 2문항 화면 캡처, `docs/04_RAG전후비교.md` 작성을 진행해야 한다. 실제 API 실행에 의존하는 DoD는 이 보고에서 완료로 간주하지 않는다.
  - 자동 판정은 **답변 본문**의 숫자만 대상으로 한다. 별도 서비스 표·카드의 값은 본문 누락을 대신하지 않는다. 세트 대응이 모호한 “A와 B는 각각 …”, 숫자 없이 “미니피그가 없다”라고만 답한 경우는 missing으로 남겨 사람이 검토한다. 모름 표현은 존재 여부만 감지하므로 부정·인용·적절성 판단은 사람이 해야 한다.
  - 각 조건 1회 측정이며 첫 B의 지연에는 지연 초기화한 의존성 생성도 포함된다. 토큰은 서비스가 반환한 단계별 usage 그대로이므로 외부 API 총 과금량으로 해석하지 않는다. 기존 서비스의 추가 입력/실패 응답도 결과에 보존하고 CLI 종료 코드 1로 알린다.

## PM 리뷰
- 판정: **승인** (피드백 없음). `.venv` `pytest -q` **948 passed**
- 실측: 8문항 A/B 실행(`2026-10-22_rag-compare.json`). B-05의 B는 GMS 502(retrieve 단계)로 1회 실패해 재실행했다(`-b05-retry`).
- PM 채점 → `docs/04_RAG전후비교.md`
  - 합계: **A 17 / B 39** (48점 만점)
  - 숫자 정확: **A 1/11(지어낸 숫자 4) / B 11/11**
  - 근거: A 0 / B 14
- 자동 판정의 한계: 비교 문항에서 세트 번호와 연도가 다른 줄에 있으면 "누락"으로 판정한다(B-05 재실행). 사람이 확인해 정정했다.
- 발견한 결함 → **T-019**
  1. **라우팅**: 세트 번호가 들어간 캐릭터 질문(B-06 "75312의 보바 펫은 어떤 캐릭터야?")과 가격 질문(B-08)이 fact로 분류된다.
  2. **답할 수 없는 속성**: 가격처럼 수집하지 않는 속성을 물어도 fact 템플릿이 "확인되지 않음"을 말하지 않고 다른 속성만 나열한다.
  3. (참고) 75257 "공식 부품 수" 표기: Rebrickable `num_parts`(1,328)를 "공식"이라고 부른다. LEGO 공식 표기(1,351)와 다를 수 있으므로 용어 정리를 검토한다.
- 남은 일: 웹 화면 캡처 2장(B-04·B-07)은 M7 스크린샷 작업에서 함께 찍는다.
