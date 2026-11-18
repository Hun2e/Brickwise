# T-024 공통 채팅 서비스·API·SSE 연결

- 상태: 완료 (2026-10-23, 피드백 1회)
- 관련 요구사항: F2009, F2011, F2012, F2013(요청·응답 형식), NF2004, ADR-018 ②⑤⑥⑦
- 선행 작업: T-021, T-022, T-023

## 목표
대화 단위의 `chat_turn`이 상태 로드 → 문맥 해석 → (컬렉션 변경) → 답변 → 턴·대상·상태 원자 저장까지 한 흐름으로 처리하게 한다. 이를 FastAPI(REST + SSE)로 공개한다. 중복 제출·재연결·재시도에서 변경이 두 번 일어나지 않게 하고, 새로고침 뒤 대화와 문맥을 복원한다. Streamlit과 웹은 이 함수 하나를 함께 쓴다.

## 처리 흐름 (`services/chat.py` `chat_turn_v2`)
1. `chat_requests`에 `(uid, request_id)` 등록
   - `done`이면 저장된 턴을 반환(재전송)한다. `running`이면 `Busy`를 낸다.
2. `ConversationRepo.get` → 상태·version 로드
3. `resolve_context(question, state)` → `ResolvedContext`
4. 분기
   - **clarify**: 되묻기 턴(kind=clarify)과 pending 상태를 저장하고 끝낸다.
   - **컬렉션 변경 의도**(T-022 `parse_intent`) + 대상 확정: `apply(..., request_id)` → 커밋 → 결과 문장. 이어서 같은 질문에 추천·설명 요청이 있으면 ②단계로 진행한다. ②가 실패하면 "등록 완료, 추천 실패"를 표시하고, 변경은 다시 실행하지 않는다.
   - **일반**: `orchestrator.answer(question, selected_set=ctx.subject, history=context_turns(같은 대상), collection_id=uid, context=ctx)`
5. `save_turn_with_state(...)`: 턴 + `turn_subjects`(primary = 대상, compared = 비교, listed = 목록 항목) + 새 상태(`state_delta` 적용)를 한 트랜잭션으로 저장한다.
   - version 충돌이면 한 번 다시 읽어 재적용한다. 그래도 충돌이면 답변은 보여 주고 "상태 저장 충돌" 경고를 단다.
6. `chat_requests.status=done`, `turn_id` 기록

## 범위
- 포함
  1. `src/rag_service/services/chat.py`: `chat_turn_v2(uid, conversation_id, question, *, request_id, on_stage)`
     - 기존 `chat_turn`은 구 경로 호환을 위해 유지한다(내부에서 레거시 대화로 변환해도 된다).
     - `TurnOut`에 `state_summary`(대상 이름·목록 종류·개수·모드·pending 문구), `items`, `action`(변경 전후)을 추가한다.
  2. `src/rag_service/services/__init__.py`: 공개 함수 내보내기(대화 생성·목록·조회·대상 변경·삭제·세트별 기록)
  3. `server/schemas.py`: `ChatIn{conversation_id, question, request_id}`, `ConversationOut`, `StateSummaryOut`, `ItemOut`, `ActionOut`, `SubjectIn{set_num|null, version}`, `CollectionIn.version`
  4. `server/main.py`: 05 §3.4 엔드포인트, 잠금 키 `(uid, conversation_id)`, `/api/threads*`는 읽기 전용 호환(쓰기는 410 + 안내)
  5. 테스트: `tests/test_chat_v2.py`, `tests/test_server_conversations.py`. 기존 server·chat 테스트는 **같은 의미를 새 계약으로** 바꾼다(F2009 분리 검증은 유지, 삭제 금지).
- 제외: 화면(T-025), 다턴 실측 평가(T-026)

## 완료 기준 (DoD)
- [ ] 대표 시나리오 7단계를 **대역 LLM + 실제 DB·컬렉션**으로 하나의 `conversation_id`에서 연속 실행해 통과한다. 4단계 등록 직후 5단계 추천이 최신 보유량(등록 세트 제외·부품 반영)을 쓴다.
- [ ] 멱등
  - 같은 `request_id`를 3번 보내면 턴 1개, 컬렉션 변경 1회다.
  - SSE를 첫 stage에서 끊고 같은 `request_id`로 재요청하면 → 진행 중이면 409, 완료 뒤에는 저장된 결과를 다시 보낸다.
- [ ] 단계 분리: 등록 뒤 RAG 설명에서 예외가 나도(대역) 등록은 1회만 반영되고, 답변에 부분 실패가 표시된다.
- [ ] 새로고침 복원: `GET /api/conversations/{id}`로 턴·현재 대상·목록·pending이 돌아온다. 다음 질문 "첫 번째 건"이 복원된 목록을 기준으로 해석된다.
- [ ] F2009 (b)(c)(d)
  - (b) 대상을 A → B로 바꾼 뒤 B 질문의 history에 A 턴이 없다.
  - (c) `GET /api/sets/{n}/history`가 세트별로 분리된다.
  - (d) 비교 턴이 두 세트 기록에 모두 나타난다.
- [ ] 오류·안내: 상태 충돌, 없는 세트, 컬렉션 버전 충돌, 키 없음 → 원인·조치 한 줄. 트레이스·키·프롬프트는 노출하지 않는다(NF2004).
- [ ] OpenAPI에 새 모델이 모두 나타난다(F2013 요청·응답 형식). 구 `/api/threads` GET은 이관된 레거시 대화를 돌려준다.
- [ ] `.venv\Scripts\python -m pytest -q` 전체 통과

## 검증 명령
```powershell
.venv\Scripts\python -m pytest -q tests\test_chat_v2.py tests\test_server_conversations.py tests\test_server_chat.py tests\test_server_api.py
.venv\Scripts\python -m pytest -q
.venv\Scripts\uvicorn server.main:app --port 8000   # PM: curl로 시나리오·멱등 실측
```

---

## 결과 보고 (Codex 작성)
- 변경 파일:
  - `src/rag_service/services/chat.py`: `chat_turn_v2`, 요청 예약·실행·재전송, 문맥 해석과 보유 변경 연결, pending 재개, 후속 답변 부분 실패, 상태 충돌 재시도, 대화 CRUD·대상 변경·세트별 기록 공개 함수.
  - `src/rag_service/services/__init__.py`: 두 UI가 사용할 공개 함수 지연 내보내기.
  - `server/schemas.py`, `server/main.py`: 대화 REST, `{conversation_id, question, request_id}` POST SSE, 상태·목록·변경 결과 모델, 컬렉션 version, 레거시 읽기 전용 및 410 안내.
  - `tests/test_chat_v2.py`, `tests/test_server_conversations.py`: 신규 서비스·API 검증.
  - `tests/test_server_chat.py`, `tests/test_server_api.py`, `tests/test_server_support.py`: 기존 서버 검증을 새 계약과 임시 저장소로 전환. 세트별 이력 분리, SSE 중단, 안전한 오류, 인용 필터 검증을 유지했다.
  - 이 작업지시서의 결과 보고 섹션만 수정했다. 새 의존성은 없다.
- 실행 명령과 결과:
  - 지시된 `.venv\Scripts\python -m pytest -q tests\test_chat_v2.py tests\test_server_conversations.py tests\test_server_chat.py tests\test_server_api.py`와 `.venv\Scripts\python -m pytest -q`: 모두 기반 인터프리터 프로세스 생성 실패. AGENTS.md §8의 기존 환경 문제와 같다.
  - 대체 실행기: `C:/Users/SSAFY/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe`. `PYTHONPATH=.venv/Lib/site-packages;src`, `PYTHON_DOTENV_DISABLED=1`, 빈 `GMS_KEY`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONIOENCODING=utf-8` 적용. `.env` 열람·출력과 실제 외부 LLM 호출은 하지 않았다.
  - 최종 집중 검증: 대체 Python `-m pytest -q tests/test_chat_v2.py tests/test_server_conversations.py tests/test_server_chat.py tests/test_server_api.py --basetemp=tests/.tmp-t024-contract-final -p no:cacheprovider` → **80 passed, 23.79초**.
  - 7단계를 한 대화에서 연속 실행했다. 실제 `lego.sqlite`를 read-only로 열어 임시 DB에 복제하고, 대역 LLM·실제 컬렉션 저장소·실제 planner로 등록 직후 최신 부품 반영과 보유 세트 제외를 검증했다.
  - 동일 요청 3회에서 턴·보유 변경 각 1회, 첫 stage에서 ASGI disconnect 후 409 및 완료 결과 재전송, 등록 후 예외/키 누락 형태의 후속 실패, 대상 A→B의 실제 `ask_fn` history 분리, 양쪽 비교 기록, listed 제외, pending·목록 순서 복원, 상태 충돌 1회/2회, 사용자별 요청 ID 분리, 레거시 사본 이관·읽기 및 410/503 안내를 검증했다.
  - 전체 회귀 1차: **1248 passed, 1 warning, 274.14초**. 이후 설명 요청의 보유 의도 오인 방지와 REST/SSE 상태 응답 일치를 보강하고 위 집중 검증을 완료했다.
  - 최종 전체 회귀: 대체 Python 표준 입력 실행기에서 `pytest.main(['-q', '--basetemp=tests/.tmp-t024-final-full', '-p', 'no:cacheprovider'])` → **1251 passed, 1 warning, 226.07초**. 경고 1건은 기존 수집 CLI 테스트의 `WIKI_CONTACT` 누락 안내다.
  - 두 전체 실행에는 `sys.addaudithook`으로 운영 `data/` 쓰기·변경, read-only가 아닌 운영 SQLite 연결, 평가 JSONL 변경을 차단했다. 각각 실행 전후 파일 크기·수정 시각 및 평가 JSONL SHA-256 비교 결과 **보호 대상 190개 파일 변경 없음**(`PROTECTED_FILES 190 UNCHANGED True`). 운영 `user.sqlite` 이관은 실행하지 않았다.
  - `git diff --check` 통과, `git diff --quiet -- data eval` 종료 코드 0. git commit·push·branch 작업 없음.
- 설계 판단과 이유:
  - T-021 `ConversationRepo`의 소유권 확인·상태 CAS·턴/대상/상태 원자 저장과 `context_turns`, T-022 `parse_intent`/`apply`/`describe`, T-023 `resolve_context`/`answer(context)`를 재사용했다. 해당 모듈과 운영 데이터는 수정하지 않았다.
  - T-021에 요청 장부와 턴 없는 대상 변경 공개 메서드가 없으므로, 허용 범위인 `chat.py`에 해당 SQL만 두었다. 저장소 비공개 메서드를 호출하지 않고 `repo.path`와 공개 읽기/저장 함수를 사용한다. 대상 변경은 `BEGIN IMMEDIATE` 및 version 검사로 보호한다.
  - HTTP 헤더 전에 요청을 예약해 durable `running`도 409로 반환한다. 실제 작업은 연결과 독립된 스레드에서 끝까지 수행한다. 하위 테이블의 전역 UNIQUE 요청 ID는 내부적으로 `uid:request_id`로 구분하고, HTTP에는 원래 요청 ID를 반환한다.
  - 턴 저장 후 장부 갱신 사이 중단은 이미 저장된 턴을 찾아 `done`으로 복구한다. 완료 재전송은 당시 응답·상태 스냅샷을 그대로 사용한다. 상태 충돌은 최신 상태에 해당 응답의 delta만 한 번 재적용하며, 두 번째 충돌은 저장되지 않은 답변과 안전한 경고를 반환한다.
  - 보유 변경을 먼저 커밋하고 추천·설명 절을 따로 실행한다. 후속 실패나 턴 저장 실패 후 재시도에도 같은 collection event ID를 사용하므로 보유량을 다시 늘리지 않는다. 일반 답변 실패 시 T-023이 비운 state_delta를 다시 덮어쓰지 않는다.
  - 기존 `chat_turn`은 유지했다. 새 UI의 진입점은 `chat_turn_v2`이며, 기존 HTTP thread 쓰기는 410으로 종료한다. 이관되지 않은 기존 사용자 DB는 자동 변경하지 않고 503 안내를 반환한다.
- 남은 이슈 / PM 확인 필요:
  - PM 환경의 `.venv`에서 전체 pytest 및 실제 키를 사용하는 curl/SSE 시나리오를 재확인해야 한다. 운영 사용자 DB 백업·이관은 사용자 지시에 따라 수행하지 않았으며 PM의 별도 작업이다.
  - 웹·Streamlit 화면의 새 API 전환은 제외 범위인 T-025다. 현재 웹의 구 POST payload는 새 계약에 맞춰 변경해야 한다.
  - 프로세스 자체가 강제 종료되어 턴 없이 `running`만 남는 경우의 자동 만료 정책은 현재 설계·설정에 없다. SSE 연결 중단은 처리하지만, 이 별도 상황의 관리자 복구/만료 정책은 후속 결정이 필요하다. 턴 저장까지 끝난 요청의 장부 복구는 구현·검증했다.

### 피드백 1차 결과

- 변경 파일:
  - `src/rag_service/services/orchestrator.py`: `_search_requested`와 `rule:search_cue`를 추가했다. 미지원 속성 검사 직후, fact 속성·비교 규칙 전에 탐색 질문을 `filter`로 보정한다.
  - `config/settings.yaml`: 지시된 9개 표현을 `router.search_cues`에 추가했다.
  - `tests/test_orchestrator_offline.py`: 가짜 라우터 출력으로 35개 회귀 사례를 추가했다. 세 대표 질문의 fact/narrative 오분류가 실제 검색 호출로 이어지는지, 명시 번호·지시어·set_refs·제작 경로는 보정하지 않는지, 포함 표현의 동반 단서·설정 변경·미지원 속성 우선순위를 검증한다.
  - `tests/test_chat_v2.py`: 기존 7단계 시나리오 첫 라우터 응답을 `filter`에서 `fact`로 바꿨다. 실제 DB의 임시 사본·실제 검색·컬렉션·planner에서 탐색으로 복구한 뒤 나머지 6단계까지 통과한다.
  - 이 작업지시서의 결과 보고 아래에 본 항목을 추가했다. 새 의존성은 없다.
- 설계 판단:
  - `들어 있는`·`들어있는`은 찾아·보여줘·목록·세트 표현이 함께 있을 때만 탐색 단서로 인정한다. 피규어 구성 질문은 기존 fact 경로를 유지한다.
  - 라우터의 `set_nums`·`set_refs`와 질문 원문의 번호를 함께 검사한다. 원문 번호 검사는 기존 문맥 해석과 같은 형식을 사용해 연도·수량을 제외하고, 라우터가 번호를 누락해도 탐색으로 바꾸지 않는다.
  - 지시어는 기존 `dialogue_cues.yaml`의 references를 재사용하고 공백을 정규화한다. 기존 규칙의 상대 순서와 검색 서비스 구현은 유지했다.
- 실행 명령과 결과:
  - 작업지시서의 `.venv\Scripts\python -m pytest -q tests\test_chat_v2.py tests\test_server_conversations.py tests\test_server_chat.py tests\test_server_api.py` 및 `.venv\Scripts\python -m pytest -q`는 모두 기반 인터프리터 프로세스 생성 실패로 실행되지 않았다.
  - 대체 Python은 `C:/Users/SSAFY/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe`를 사용했다. `PYTHONPATH=.venv/Lib/site-packages;src`, `PYTHON_DOTENV_DISABLED=1`, 빈 `GMS_KEY`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONIOENCODING=utf-8`을 적용했다. `.env` 열람·출력 및 실제 외부 LLM 호출은 하지 않았다.
  - 표준 입력 실행기에서 `pytest.main(['-q', 'tests/test_orchestrator_offline.py', 'tests/test_chat_v2.py', 'tests/test_server_conversations.py', 'tests/test_server_chat.py', 'tests/test_server_api.py', '--basetemp=tests/.tmp-t024-feedback1-focused', '-p', 'no:cacheprovider'])` → **218 passed, 54.93초**. 단, 실행 후 보호 파일 비교는 아래 데이터 관찰 사항 때문에 실패했다.
  - 전체 1차: `pytest.main(['-q', '--basetemp=tests/.tmp-t024-feedback1-full', '-p', 'no:cacheprovider'])` → **1285 passed, 1 failed, 1 warning, 277.95초**. 기존 `test_retrieval_cli_twice_on_real_fixture_snapshot`의 임시 Chroma 스냅샷 읽기에서 `Error creating hnsw segment reader: Nothing found on disk`가 발생했다.
  - 위 실패 테스트를 `--basetemp=tests/.tmp-t024-feedback1-snapshot-retry`로 단독 재실행 → **1 passed, 1.72초**. 해당 테스트나 스냅샷 구현은 수정하지 않았다.
  - 최종 전체: `pytest.main(['-q', '--basetemp=tests/.tmp-t024-feedback1-full-final', '-p', 'no:cacheprovider'])` → **1286 passed, 1 warning, 226.77초**. 경고는 기존 수집 CLI 테스트의 `WIKI_CONTACT` 누락 안내다.
  - `git diff --check` 통과, `git diff --quiet -- eval` 종료 코드 0. git commit·push·branch 작업은 하지 않았다.
- 데이터 보호 및 관찰 사항:
  - 작업 시작 시 이미 `data/chroma/chroma.sqlite3`, `data/collected/collect.sqlite`, `data/user.sqlite`가 git 변경 상태였다. 이를 편집하거나 복원하는 명령은 실행하지 않았다.
  - 집중 검증에 Python 감사 훅으로 운영 파일 쓰기·변경 및 read-only가 아닌 운영 SQLite 연결을 차단했지만, 전후 파일 크기·수정 시각 비교에서 `data/chroma/chroma.sqlite3`와 `data/user.sqlite`의 변화가 감지됐다. 이후 두 파일은 git 변경 목록에서 사라졌다. 변경 주체·원인은 확인하지 못했으며, 이 실행 전체에 대해 운영 데이터 변경 없음이라고 판정하지 않았다. 평가 JSONL 변경은 없었다.
  - 이후 전체 1차·단독 재검증·최종 전체 실행은 운영 `data/`와 평가 JSONL **192개 파일**에 OS 읽기 공유 잠금(`FileAccess.Read`, `FileShare.Read`)을 확보한 상태에서 동일 감사 훅을 적용했다. 각 실행 전후 크기·수정 시각과 평가 JSONL SHA-256 비교 결과 모두 **`PROTECTED_FILES 192 UNCHANGED True`**였다.
- 남은 이슈 / PM 확인:
  - PM 환경의 `.venv` 재검증과 실제 키를 사용하는 라우팅 40문항·holdout·legacy·검색 회귀는 PM 확인 사항이다.
  - 집중 검증 중 위 운영 DB 두 파일이 변경된 원인 및 동시 작업 여부는 PM 확인이 필요하다. 잠금 적용 후 검증에서는 변경이 없었다.

## PM 리뷰
- 판정:
- 메모:

## PM 피드백 1차 (2026-10-23)
- 판정: **부분 반려 (라우팅 규칙 1건)**. 서비스·API 구현은 이후 함께 판정한다.
- 실제 키 실측(임시 user DB, `chat_turn_v2` 대표 시나리오)
  - **1단계 "루크가 들어 있는 엑스윙 찾아줘"가 실패**했다.
    - LLM 라우터가 fact로 분류한다. 이어서 `rule:fact_without_attribute`가 narrative로 바꾸고, 대상이 없어 "어느 세트를 말씀하시는지"를 되묻는다.
    - "엑스윙 세트 찾아줘"는 filter로 맞게 간다. 반면 "○○가 들어 있는 ○○ 찾아줘"와 "루크 미니피그가 들어 있는 X-wing 세트 찾아줘"는 fact로 간다.
  - `search_sets`를 직접 호출하면 같은 질문에서 `minifig='Luke Skywalker'`, `name_keywords=('X-wing',)`로 12건을 정확히 찾는다 → **라우팅만의 문제**다.
  - 대역 LLM 시나리오 테스트는 라우터 출력을 고정하므로 이 결함을 잡지 못했다.
- 수정 지시 (결정적 규칙, `orchestrator._rules`)
  1. `rule:search_cue` 추가: 다음을 모두 만족하면 route를 `filter`로 바꾼다.
     - LLM route가 `fact` 또는 `narrative`다.
     - 질문에 탐색 단서(settings `router.search_cues`: "찾아", "검색", "보여줘", "알려줘 세트", "어떤 세트", "세트 목록", "있는 세트", "들어 있는", "들어있는")가 있다. 다만 "들어 있는"·"들어있는"은 "찾아·보여줘·목록·세트" 단서와 함께 있을 때만 인정한다("피규어 누구누구 들어있어?"는 fact로 유지).
     - 질문에 명시 세트 번호가 없다.
     - 지시어·현재 대상 참조(`set_refs`, "이/그 세트·그거" 등)가 없다.
     - 미지원 속성 규칙이 먼저 적용되지 않았다.
  2. 적용 순서: 미지원 속성 → **search_cue** → fact 속성 단서·두 세트 비교 → 나머지(기존 순서 유지).
  3. 테스트(가짜 라우터 출력)
     - 위 3개 질문 → filter
     - 대조군: "75301 찾아줘"(명시 번호 → 유지), "피규어 누구누구 들어있어?"(fact 유지), "그거 보여줘"(지시어 → 유지), "내 부품으로 뭐 만들 수 있어?"(build 유지)
  4. 범위: `services/orchestrator.py`, `config/settings.yaml`(router.search_cues), `tests/`. `eval/*.jsonl`과 운영 `data/`는 수정하지 않는다.
- 회귀(실제 키, 라우팅 40·holdout·legacy·검색)는 PM이 확인한다.

## PM 리뷰 (최종)
- 판정: **승인** (피드백 1차 후). `.venv` `pytest -q` **1286 passed**. 테스트 뒤 운영 `data/`는 무변경(LLM 캐시만 증가)이다.
- 라우팅 회귀(router-v4 + `rule:search_cue`, 실제 키): PM 40 **39/40**, legacy 24/24, holdout 12/12, holdout-2 8/8
- **대표 시나리오 7단계 실측** (실제 키, settings의 `user_sqlite`를 임시 파일로 바꿔 실행한 뒤 복원)
  1. 필터 12건, `last_list(search)`
  2. "그중 2021년" → 75301-1 1건 → 대상 자동 지정
  3. 부품 474개(DB)
  4. 등록 0 → 1개. **같은 request_id 재전송 → 같은 턴, 수량 1 유지**
  5. 추천 10건. "86개(51종)만 더 있으면 완성"(완성 표현 규칙 준수)
  6. "첫 번째" → 75535-1 Han Solo 대상(표시 순서 1위). ⚠️ 답변 품질 미흡(아래)
  7. 색상 무시 77개 / 정확 일치 86개 재계산
- 실측 중 PM 실수와 정정: 첫 시도에서 대화 저장소만 임시 DB로 바꿨다. 오케스트레이터가 설정 경로의 운영 `user.sqlite`를 열어 빈 새 테이블이 생겼다(데이터 무손상, `git checkout`으로 복원). Codex가 보고한 "원인 미확인 운영 DB 변경"이 이것이다.
- 후속 → **T-025 선행 정리**
  1. RAG 경로에 원문 질문("첫 번째 건 어떤 기체야?")이 그대로 들어가 답변이 빈약하다. 해석한 대상이 있으면 P3·P1에 "75535-1 Han Solo"를 명시한 질문으로 넘긴다(원문은 대화 기록에 그대로 둔다).
  2. 한국어 조사("75301-1 … Fighter을")를 받침에 따라 자동 선택한다.
  3. 프로세스가 강제 종료되어 `chat_requests.running`이 남는 경우의 만료 정책(settings 시간 초과 → failed) — Codex 남은 이슈.
- 웹 화면은 구 API 형식이라 T-025 완료 전까지 질문이 동작하지 않는다(Streamlit은 구 `chat_turn` 호환 유지).
