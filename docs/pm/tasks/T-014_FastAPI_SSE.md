# T-014 FastAPI REST + SSE (F2013 서버)

- 상태: 완료 (2026-10-22)
- 관련 요구사항: F2013(화면·서버 분리, 요청·응답 형식 정의), F2009, F2011, F2012, NF2003, NF2004, NF2005
- 선행 작업: T-013(공통 서비스), ADR-015(⑤ REST + SSE, ⑥ 익명 UUID)

## 목표
T-013의 공통 서비스를 HTTP로 공개한다. 웹 화면(T-015)이 세트 목록·상태·대화 기록·컬렉션은 REST로, 질문은 SSE로 받아 진행 단계와 최종 응답을 표시할 수 있게 한다. 요청·응답 형식은 Pydantic 모델과 OpenAPI 문서로 정의해 F2013의 "형식 정의" 증빙으로 쓴다.

## 실측 (PM, 2026-10-22)
| 항목 | 현재 상태 | 의미 |
|---|---|---|
| 의존성 | fastapi 0.141.1 · uvicorn 0.52.4 · pydantic 2.13.5 고정, `.venv`에 설치됨. httpx 0.28.1(TestClient용) 있음 | **새 의존성 없음** |
| SSE 지원 | FastAPI 0.141.1에 `fastapi.sse.EventSourceResponse`·`ServerSentEvent`(event·id·retry·comment, keep-alive 주석) 내장 | `sse-starlette` 같은 추가 패키지 불필요 |
| 공통 서비스 | `chat_turn`(동기, 진행 콜백 `on_stage(label, detail)`), `ConversationStore.turns/threads/clear`, `catalog_options`, `check_status`, `load_collection/save_collection`, `response_to_dict`, `cited_cards` | 서버는 이 함수만 호출 |
| 처리 시간 | 템플릿 경로 수십 ms ~ RAG·수집 경로 20~40초 | 동기 함수를 스레드에서 실행하고, 진행 이벤트를 큐로 SSE에 흘려보내야 한다 |
| 브라우저 제약 | `EventSource`는 GET만 지원한다. ADR-015는 `POST /api/chat` | 웹은 `fetch` + `ReadableStream`으로 SSE를 읽는다(T-015). 서버 쪽 형식은 표준 SSE 그대로 |
| `server/`, `web/` | 비어 있음 | 새로 만든다 |

## 범위
- 포함
  1. `server/main.py`: 앱 생성, 라우터, 오류 처리기, 정적 파일(`web/`) 제공
  2. `server/schemas.py`: 요청·응답 Pydantic 모델(= API 계약)
  3. `server/deps.py`: uid 검증 의존성 (`X-User-Id` 헤더), 대화별 처리 중 잠금
  4. `config/settings.yaml`에 `server` 섹션 추가: `max_question_chars`, `keepalive_sec`, `cors_origins: []`, `static_dir: "web"`
  5. API 계약 문서는 **PM이 작성**한다 (Codex는 OpenAPI 생성 결과를 결과 보고에 붙인다)
  6. 테스트 `tests/test_server_*.py` (TestClient, 오케스트레이터 대역)
- 제외
  - 화면(T-015). `web/`에는 동작 확인용 `index.html` 한 장(“T-015에서 구현”)만 둔다
  - 인증·로그인, 토큰 단위 답변 스트리밍, 질문 취소
  - 서비스·RAG 로직 변경. 서버 안에 검색·프롬프트·SQL을 두지 않는다 (NF2005)

## 엔드포인트
| 메서드·경로 | 용도 | 서비스 함수 |
|---|---|---|
| `GET /api/health` | 서버 생존 확인 | — |
| `GET /api/status` | 준비 상태·안내 문구·`can_chat` (F2012) | `check_status` |
| `POST /api/users` | 익명 uid 발급 | `new_user_id` |
| `GET /api/sets` | 세트 목록 378건 (번호·이름·연도·부품 수·범위·별칭·이미지·라벨) | `catalog_options` |
| `GET /api/threads` | 내 대화 목록 (키·턴 수·최근 시각) | `ConversationStore.threads` |
| `GET /api/threads/{thread}/turns` | 대화 기록 (F2009) | `ConversationStore.turns` |
| `DELETE /api/threads/{thread}` | 대화 지우기 | `ConversationStore.clear` |
| `GET /api/collection` | 내 컬렉션 | `load_collection` |
| `PUT /api/collection` | 컬렉션 전체 교체 | `save_collection` |
| `POST /api/chat` | 질문 → SSE (`stage`* → `result` 또는 `error`) | `chat_turn` |
| `GET /` | `web/` 정적 파일 | — |

## 인터페이스 계약
```python
# server/schemas.py  (응답 필드 이름은 OrchestratorResponse·Card와 같게 유지)
class CardOut(BaseModel):
    n: int; source_type: Literal["set_meta", "db", "wikipedia", "brickipedia"]
    title: str; url: str; license: str; text: str
    item_id: str = ""; doc_id: str = ""; lang: str = ""; section_path: str = ""
class TableOut(BaseModel):
    title: str; columns: list[str]; rows: list[list]; formats: dict[str, str] = {}
class AnswerOut(BaseModel):
    route: str; answer: str; cards: list[CardOut]      # 인용된 카드만 (cited_cards)
    tables: list[TableOut]; set_nums: list[str]; needs_input: str | None
    warnings: list[str]; timings: dict[str, int]
    # usage·verification·route_source는 내보내지 않는다 ← 검토 포인트 ④
class TurnOut(BaseModel):
    id: int; thread: str; question: str; answer: AnswerOut; created_at: str; saved: bool
class ChatIn(BaseModel):
    thread: str                      # 세트 번호 | "explore" | "collection"
    question: str = Field(min_length=1, max_length=...)   # 상한은 settings.server.max_question_chars
    selected_set: str | None = None
class StageEvent(BaseModel):         # SSE event: stage
    stage: str; detail: str | None; index: int           # index = STAGES 안 위치 (진행 막대용)
class ErrorOut(BaseModel):           # 모든 오류 응답·SSE error 이벤트 공통
    code: str; message: str          # message는 사용자용 원인·조치 한 줄 (NF2004)
```

### SSE 형식 (`POST /api/chat`, `text/event-stream`)
```
event: stage
data: {"stage": "분류", "detail": null, "index": 0}

event: stage
data: {"stage": "문서 수집", "detail": "Millennium Falcon", "index": 2}

: keep-alive                      ← 처리 중 주기적 주석 (프록시 타임아웃 방지)

event: result
data: {TurnOut}
```
- 마지막 이벤트는 반드시 `result` 또는 `error` 중 하나 1개.
- `needs_input`(되묻기·키 없음 안내)은 오류가 아니라 `result`로 보낸다 (화면에서 대화로 남기기 위해).
- 처리 전 검증 실패(uid·thread·질문 형식)는 SSE를 열지 않고 4xx JSON `ErrorOut`.

## 완료 기준 (DoD)
- [ ] 위 엔드포인트가 TestClient로 동작하고, `/docs`(OpenAPI)에 모든 요청·응답 모델이 나타난다 (F2013)
- [ ] `POST /api/chat` 스트림이 `stage`를 순서대로 보내고 `result` 1개로 끝난다. 결과는 user.sqlite에 저장되어 `GET /api/threads/{thread}/turns`로 다시 읽힌다 (F2009)
- [ ] 키 없음·DB 없음·저장 실패·예상하지 못한 예외에서 스택트레이스·키·프롬프트가 HTTP 응답과 SSE에 나오지 않는다 (NF2004, 테스트로 응답 본문 검사)
- [ ] `X-User-Id`가 없거나 형식이 틀리면 400, 질문·thread·selected_set 형식 오류와 불일치는 422, 같은 uid·thread 처리 중 재요청은 409 (모두 `ErrorOut`)
- [ ] SSE 연결이 중간에 끊겨도 처리가 끝까지 진행되어 저장되고, 잠금이 풀린다 (테스트: 스트림을 첫 `stage`에서 닫은 뒤 turns 조회·재질문)
- [ ] 서버 코드가 `rag_service.services`의 공개 함수만 import한다 (테스트로 검사, NF2005)
- [ ] Streamlit과 서버가 같은 uid로 같은 대화를 본다 (같은 user.sqlite를 쓰는 테스트)
- [ ] `uvicorn server.main:app` 실행 → `GET /api/status`·`/docs` 응답 (PM이 실제 키로 `curl -N` SSE 확인)
- [ ] `.venv\Scripts\python -m pytest -q` 전체 통과 (현재 761)

## 검증 명령
```powershell
.venv\Scripts\python -m pytest -q tests/test_server_api.py tests/test_server_chat.py
.venv\Scripts\python -m pytest -q
.venv\Scripts\uvicorn server.main:app --port 8000
curl.exe -N -X POST http://localhost:8000/api/chat -H "X-User-Id: <uid>" -H "Content-Type: application/json" -d "{...}"
```

## 검토 포인트 결정 (사용자, 2026-10-22 — 6건 모두 권장안)
1. **uid 전달**: 요청 헤더 `X-User-Id`. 경로·쿼리에 넣지 않는다(접근 로그에 남지 않게). `POST /api/users`, `GET /api/health`, `GET /api/status`, `GET /api/sets`, 정적 파일은 헤더가 필요 없다.
2. **연결 끊김**: 처리는 백그라운드 스레드에서 끝까지 하고 저장한다. 스트림 쪽은 끊김을 감지하면 이벤트 전송만 멈춘다. 서버 종료 외에는 처리 중 작업을 버리지 않는다.
3. **동시 질문**: 같은 `(uid, thread)`에 처리 중인 질문이 있으면 409 `{"code": "busy", "message": "이전 질문을 처리 중입니다. 답변이 끝난 뒤 다시 질문하세요."}`. 다른 thread는 동시에 처리한다. 잠금은 프로세스 메모리로 충분하다(uvicorn 단일 워커 전제, 결과 보고에 명시).
4. **응답 필드**: 화면용만 내보낸다(`AnswerOut`). `usage`·`verification`·`route_source`는 응답에 넣지 않는다. 저장된 전체 JSON은 그대로 두고 응답 변환만 거른다. 인용 카드는 `cited_cards`로 다시 거른다.
5. **화면 제공**: FastAPI가 `web/`을 `/`에 마운트해 같은 주소에서 제공한다. API는 `/api` 아래에만 둔다. CORS는 `server.cors_origins`가 비어 있으면 켜지 않는다.
6. **세트 목록**: `GET /api/sets`가 378건 전체(별칭 전부 포함)를 한 번에 돌려준다. 서버 검색 API는 만들지 않는다. 목록은 프로세스 안에 캐시하고, 응답에 `Cache-Control: max-age`(값은 settings)를 붙인다.

---

## 결과 보고 (Codex 작성)
- 변경 파일:
  - `server/main.py`: REST 9개와 POST SSE 1개 operation, 공통 서비스 연결, 안전한 오류 응답, 세트 목록 캐시, 선택적 CORS, `/` 정적 파일 제공.
  - `server/schemas.py`: 요청·응답 모델 15개, 질문 길이·공백·thread/selected_set 일치·컬렉션 정수 수량 검증, 화면용 응답 계약.
  - `server/deps.py`: `X-User-Id` 검증, `(uid, thread)` 처리 중 잠금, YAML의 서버 설정 로드.
  - `config/settings.yaml`: `server` 설정 추가 (`max_question_chars: 4000`, `keepalive_sec: 15`, `catalog_max_age_sec: 300`, `cors_origins: []`, `static_dir: web`).
  - `web/index.html`: “T-015에서 구현” 확인용 화면.
  - `tests/test_server_api.py`, `tests/test_server_chat.py`, `tests/test_server_support.py`: 서버 검증 57개와 격리된 공통 fixture.
  - 문서 수정은 이 작업지시서의 결과 보고 섹션에 한정했다. 서비스·RAG·Streamlit 코드는 수정하지 않았고 새 의존성 및 git commit·push·branch 작업은 없다.
- 실행 명령과 결과:
  - `.venv/Scripts/python -V`: 기반 인터프리터 프로세스 생성 실패. AGENTS.md §8의 알려진 환경 제약에 따라 대체 Python 3.12와 기존 `.venv/Lib/site-packages`로 검증했다.
  - 대체 실행 환경과 서버 전용 검증 명령:
    ```powershell
    $env:PYTHONPATH='src;.venv/Lib/site-packages'
    $env:PYTHONIOENCODING='utf-8'
    $env:PYTHON_DOTENV_DISABLED='1'
    $env:ANONYMIZED_TELEMETRY='False'
    $env:LANGSMITH_TRACING='false'
    Remove-Item Env:GMS_KEY -ErrorAction SilentlyContinue
    & 'C:/Users/SSAFY/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -m pytest -q tests/test_server_api.py tests/test_server_chat.py --basetemp=.pytest_tmp_T014_unit2
    ```
    **57 passed, 5.60초**. 외부 모델 호출이나 `.env` 직접 열람·출력은 하지 않았다.
  - 전체 회귀 테스트: 같은 환경에서 `-m pytest -q --basetemp=.pytest_tmp_T014_full` → **818 passed, 1 warning, 186.41초**. 기존 761개와 서버 검증 57개가 모두 통과했다. 경고는 기존 오프라인 수집 CLI 테스트의 `WIKI_CONTACT` 미설정 안내 1개다.
  - 검증 항목: 모든 API 및 `/docs`·정적 화면, 사용자/대화 분리, 컬렉션 전체 교체·삭제, 400/409/422 JSON, 질문 길이·선택 세트 일치, 진행 이벤트 순서와 최종 이벤트 1개, 인용 카드만 노출, 저장 JSON의 내부 필드 보존, 응답에서 내부 필드 제외, 저장 실패 시 `saved=false`, 예상 밖 오류 비노출, keep-alive, 캐시·CORS·서비스 import 경계.
  - 첫 `stage` 수신 직후 ASGI `http.disconnect`를 보내 응답 전송이 종료되는 것을 확인했다. 서비스는 계속 처리하고 SQLite에 저장했으며, 작업 중 재요청은 409이고 완료 후 조회·재질문은 성공했다. TestClient가 스트림을 버퍼링하기 때문에 연결 중단 검증만 ASGI 수신/송신으로 수행했다.
  - Streamlit과 동일한 `conversation_store()`가 같은 임시 `user.sqlite`를 사용하도록 구성하여 API 저장→공통 서비스 조회 및 공통 서비스 저장→API 조회를 확인했다.
  - 별도 통합 확인: 오케스트레이터 대역 없이 실제 공통 서비스를 호출했다. 키 없음은 `stage → result`, DB 없음은 `result`로 종료했고 각각 안전한 `needs_input`과 `saved=true`를 반환했다. 두 턴 모두 같은 대화에서 다시 조회했다. 임시 사용자 DB·문서 캐시를 사용했다.
  - 같은 대체 환경에서 `-m uvicorn server.main:app --host 127.0.0.1 --port 8000 --no-access-log`를 실행했다. `/api/health`, `/api/status`, `/docs`, `/` 모두 **200**. `/api/sets` **378건(core 332 / compare_only 46)**, `Cache-Control: max-age=300`. 상태는 DB 정상, 메타 378·외부 문서 3025 청크, 키 미설정, `can_chat=false`로 확인했다. 확인용 서버는 종료했다.
  - `git diff --check`: 통과 (Git의 LF→CRLF 안내만 있음).
- 설계 판단과 이유:
  - HTTP 계층은 `rag_service.services.*`의 공개 함수·타입·단계 상수만 가져온다. 서버 시작 시에는 같은 `config/settings.yaml`의 `server` 섹션만 읽어 비서비스 모듈 import와 범위 밖 서비스 변경을 피했다.
  - 작업마다 별도 스레드에서 `chat_turn`을 끝까지 실행한다. 진행 이벤트는 이벤트 루프의 큐로 전달하고, 스트림이 닫히면 전달만 중단한다. 잠금은 스트림 종료가 아니라 서비스 작업의 `finally`에서 해제한다. 같은 대화 삭제도 동일한 잠금으로 보호한다.
  - FastAPI 0.141.1은 라우트의 `response_class=EventSourceResponse`를 generator 호출로 처리한다. 일반 async 라우트에서 사용하면 coroutine을 iterable로 취급하는 문제가 첫 테스트에서 확인됐다. 따라서 `StreamingResponse`의 미디어 타입을 지정하고 FastAPI 내장 `format_sse_event`로 인코딩한다. 검증·잠금·스레드 시작 실패를 헤더 전 JSON 오류로 처리하고, keep-alive 주기도 설정값으로 제어한다. 추가 SSE 패키지는 없다.
  - `needs_input`과 저장 실패는 정상 `result`로 전달한다. 저장 실패는 기존 서비스의 `id=0`을 `saved=false`로 표시한다. 예외를 직접 HTTP/SSE에 넣지 않으며, 검증 오류의 입력 원문·ctx도 반환하지 않는다.
  - 전체 응답은 기존 서비스대로 저장하고 `AnswerOut`으로 공개 필드만 추린다. `cited_cards`를 실시간 결과와 재조회 모두에 적용한다. `usage`·`verification`·`route_source`는 저장소에만 남는다.
  - 목록은 앱 인스턴스의 메모리에 최초 성공 시 캐시한다. 실패는 캐시하지 않는다. 데이터 변경 후에는 서버를 재시작하면 새 목록을 읽는다.

### OpenAPI 생성 결과

실제 실행한 서버의 `GET /openapi.json` 결과: **OpenAPI 3.1.0, API 경로 9개 / HTTP operation 10개, 모델 15개**. `/`는 정적 파일 마운트이므로 API 스키마에 포함되지 않는다. `/docs`에서 요청 모델·헤더·성공 응답·오류 모델을 확인할 수 있다.

| Operation | 요청 | 성공 응답 |
|---|---|---|
| GET `/api/health` | 없음 | `HealthOut` |
| GET `/api/status` | 없음 | `StatusOut` |
| POST `/api/users` | 없음 | `UserOut` |
| GET `/api/sets` | 없음 | `SetOut[]` |
| GET `/api/threads` | `X-User-Id` | `ThreadOut[]` |
| GET `/api/threads/{thread}/turns` | `X-User-Id`, thread | `TurnOut[]` |
| DELETE `/api/threads/{thread}` | `X-User-Id`, thread | `ClearOut` |
| GET `/api/collection` | `X-User-Id` | `CollectionOut` |
| PUT `/api/collection` | `X-User-Id`, `CollectionIn` | `CollectionOut` |
| POST `/api/chat` | `X-User-Id`, `ChatIn` | SSE의 `StageEvent` / `TurnOut` / `ErrorOut` |

`POST /api/chat`의 생성 결과 발췌(200은 각 SSE `data:`의 JSON 스키마, 처리 전 오류는 JSON):

```json
{
  "200": {
    "description": "SSE: event: stage (StageEvent)* → result (TurnOut) 또는 error (ErrorOut) 1개. 중간 keep-alive 주석. needs_input은 result로 반환.",
    "content": {
      "text/event-stream": {
        "schema": {
          "type": "object",
          "anyOf": [
            {"$ref": "#/components/schemas/StageEvent"},
            {"$ref": "#/components/schemas/TurnOut"},
            {"$ref": "#/components/schemas/ErrorOut"}
          ],
          "title": "Response 200 Chat Api Chat Post"
        }
      }
    }
  },
  "422": {
    "content": {
      "application/json": {"schema": {"$ref": "#/components/schemas/ErrorOut"}}
    },
    "description": "Unprocessable Entity"
  }
}
```

400·404·405·409·500도 같은 `application/json`의 `ErrorOut`을 사용한다. `ChatIn.question.maxLength=4000`이며, `AnswerOut` 속성은 `route`, `answer`, `cards`, `tables`, `set_nums`, `needs_input`, `warnings`, `timings`다. 나머지 중첩 모델은 `CardOut`, `TableOut`이며 전체 15개 모델의 등록과 참조를 테스트로 확인했다.

- 남은 이슈 / PM 확인 필요:
  - 잠금과 목록 캐시는 프로세스 메모리이므로 **uvicorn 단일 워커** 전제다. 다중 워커·인증·질문 취소·토큰 스트리밍은 이번 범위에 포함하지 않았다.
  - PM 환경의 `.venv`로 전체 테스트 재확인, 실제 키를 이용한 `curl.exe -N` SSE 확인 및 API 계약 산출물 문서 작성이 남아 있다. 실제 외부 모델 호출은 수행하지 않았다.
  - 범위 밖 코드 수정이 필요한 이슈는 발견하지 못했다. 화면 구현은 T-015에서 이어간다.

## PM 리뷰
- 판정: **승인** (피드백 없음)
- `.venv` 재검증: `pytest -q` **818 passed** (134초)
- 코드 리뷰
  - 서버는 `rag_service.services.*`만 import한다.
  - 질문 처리는 별도 스레드에서 실행하고, 잠금은 작업의 `finally`에서 해제한다.
  - 연결이 끊기면 이벤트 전송만 중단한다.
  - `EventSourceResponse` 대신 `StreamingResponse` + 내장 `format_sse_event`를 썼다. 4xx 검증을 스트림 시작 전에 끝내기 위해서이며, 근거는 결과 보고에 있다. 타당하다.
- 실제 키로 실측 (uvicorn :8000, curl)
  - `/api/status`: `can_chat=true`, 세트 378 · 메타 378 · 문서 3,025. `/docs`·`/openapi.json`·`/` 200. `/api/sets`에 `Cache-Control: max-age=300`.
  - 오류: 헤더 없음과 잘못된 uid는 400 `invalid_user_id`, thread와 selected_set 불일치는 422 `invalid_request`. 본문에 입력 원문·트레이스가 없다.
  - SSE (75295-1 서술 질문): 분류 → 검색어 추출 → 문서 수집("이미 수집됨") → 검색 → 생성 → 검증 → `result`. 5.1초. 응답 키는 화면용 8개만 있고, 인용 카드만 포함된다.
  - 연결 끊김 (75313-1 신규 질문): 3초 만에 클라이언트를 끊었다. 같은 대화로 바로 다시 질문하면 409 `busy`가 나온다. 약 9초 뒤 turns에 저장된 것을 확인했다(total 8,022ms). 인용 카드는 set_meta 1개 + wikipedia 2개다.
  - 공유: API로 저장한 턴을 Streamlit의 `conversation_store()`로 같은 uid에서 조회할 수 있다(1건).
- 남은 이슈
  1. 서버는 `routing`·`resolving`을 `분류` 두 번으로 보낸다. Streamlit은 연속 중복을 화면에서 지우므로, T-015 웹도 같은 규칙(연속 중복 무시)으로 처리한다. 서버 쪽 중복 제거는 M6에서 검토한다.
  2. `server/deps.py`가 `config.load_settings` 대신 YAML을 직접 읽는다(Codex 판단: 서비스 외 import 회피). 설정의 단일 출처 원칙과 어긋나므로, M6에서 `services`에 설정 조회 함수를 공개해 정리한다.
  3. 잠금은 프로세스 메모리 기반이라 uvicorn 단일 워커를 전제로 한다(README 실행법에 명시 예정).
