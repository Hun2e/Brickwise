# T-013 공통 서비스 + Streamlit 필수 UI

- 상태: 완료 (2026-10-22)
- 관련 요구사항: F2009, F2010, F2011, F2012, NF2004, NF2005 (F2008 근거 표시는 화면 반영)
- 선행 작업: T-011(오케스트레이터), T-012(수치 검증·p3-v2), ADR-015(M5 화면 설계)

## 목표
Streamlit 화면에서 세트를 고르고 질문하면 단계별 진행 상태 → 답변 → 인용 근거 패널이 보이고, 대화가 사용자·세트별로 서버에 저장되어 새로고침 뒤에도 남는다. 이 작업에서 만드는 서비스 함수는 T-014(FastAPI)가 그대로 재사용한다.

## 실측 (PM, 2026-10-22)
| 항목 | 현재 상태 | 이 작업에서의 의미 |
|---|---|---|
| 진입점 | `services.orchestrator.answer(question, selected_set, history, collection_id, on_event)` → `OrchestratorResponse` | UI는 이 함수만 호출 |
| 이벤트 이름 | 오케스트레이터 `routing`·`resolving`·`searching`·`querying`·`done` / 수집 `extracting_queries`·`searching`·`selecting`·`fetching(title, lang)`·`indexing`·`skipped` / 그래프 `retrieving`·`generating` / 조건 탐색 `extracting`·`ranking`·`reviewing` | **`searching`이 두 곳에서 다른 뜻으로 쓰인다.** 검증 단계 이벤트는 없다 → 표시 단계 매핑 필요 |
| 사용자 DB | `user.sqlite`에 `collections`·`collection_sets`·`collection_adjustments`만 있음. 파일은 아직 없음 | 대화 테이블 추가, 컬렉션과 사용자 연결 방식 결정 필요 |
| 세트 목록 | `sets` 378건(`set_scope` core 332 / compare_only 46), 별칭은 `ko_aliases` | 검색형 선택 상자의 데이터 |
| 이력 설정 | `retrieval.history_turns: 4`, `router.history_answer_chars: 200` | 저장된 대화에서 직전 4턴을 `history`로 넘긴다 |
| Streamlit 1.62 | `st.tabs(..., default=, key=, on_change=)` 지원 | 탐색 카드 클릭 → 아카이브 탭 전환이 가능할 것으로 **추정**. Codex가 확인하고, 안 되면 대안을 쓴다(검토 포인트 ⑥) |
| `app_streamlit.py` | 없음 | 새로 만든다 |

## 범위
- 포함
  1. `services/conversations.py`: 대화 저장소 (user.sqlite, 사용자·대화 키별 턴 저장·조회·삭제)
  2. `services/users.py`: 익명 사용자 ID 발급·검증, 사용자 ↔ 컬렉션 연결
  3. `services/status.py`: 상태 점검 (DB·색인 건수·키 설정 여부·user.sqlite 쓰기). 키 값은 읽어서 보여 주지 않고 **있는지 여부만** 확인한다
  4. `services/catalog.py`: 세트 목록 (선택 상자 옵션, 번호·영문명·한국어 별칭)
  5. `services/progress.py`: 내부 이벤트 → 사용자 표시 단계 매핑 (UI 두 곳 공용)
  6. `services/chat.py`: `chat_turn(uid, thread_key, question, on_stage)` — 이력 조회 → `answer()` → 저장을 묶은 함수. Streamlit과 FastAPI가 같은 함수를 쓴다
  7. `orchestrator.py`: 검증 단계 이벤트(`verifying`) 발행 1줄 추가만 허용
  8. `app_streamlit.py`: 탭 3개, 사이드바 세트 선택, 진행 상태, 답변·경고·근거 패널, 상태 안내
  9. 테스트
- 제외
  - FastAPI·웹(T-014·T-015), 답변 토큰 스트리밍, 로그인·인증
  - 라우터·프롬프트·검색 로직 변경 (UI는 서비스 함수만 호출)
  - 수치 검증 상세·검수 제외 목록 화면 표시 (ADR-015 ⑦)

## 수정·생성할 파일
- 생성: `src/rag_service/services/{conversations,users,status,catalog,progress,chat}.py`, `app_streamlit.py`, `tests/test_conversations.py`, `tests/test_status.py`, `tests/test_catalog.py`, `tests/test_progress.py`, `tests/test_chat_service.py`, `tests/test_streamlit_app.py`
- 수정: `src/rag_service/db/user_schema.sql`(테이블 추가), `src/rag_service/planner/collections.py`(`ensure(cid)`만), `src/rag_service/services/orchestrator.py`(`verifying` 이벤트만), `config/settings.yaml`(`ui` 섹션)

## 인터페이스 계약
```python
# services/users.py
def new_user_id() -> str: ...                      # uuid4 hex
def valid_user_id(uid: str) -> bool: ...           # 32자리 hex만 허용 (URL 파라미터 위변조 방어)
def collection_id_for(uid: str) -> str: ...        # 검토 포인트 ③

# services/conversations.py
ThreadKey = str   # 세트 번호(예: "75192-1") | "explore" | "collection"   ← 검토 포인트 ①
@dataclass
class Turn:
    id: int; question: str; response: dict; created_at: str   # response = OrchestratorResponse 직렬화 ← ②
class ConversationStore:
    def __init__(self, user_db_path: Path): ...
    def append(self, uid: str, thread: ThreadKey, question: str, response: dict) -> Turn: ...
    def turns(self, uid: str, thread: ThreadKey, limit: int | None = None) -> list[Turn]: ...  # 오래된 순
    def threads(self, uid: str) -> list[dict]: ...        # [{thread, turns, updated_at}] 최근 순
    def clear(self, uid: str, thread: ThreadKey) -> int: ...
def history_for(turns: list[Turn], n: int) -> list[dict]: ...   # [{"question", "answer"}] 직전 n턴

# services/catalog.py
@dataclass(frozen=True)
class SetOption:
    set_num: str; name: str; year: int; num_parts: int; scope: str; aliases: list[str]; img_url: str
    def label(self) -> str: ...   # "75192-1 · Millennium Falcon (2017) · 밀레니엄 팔콘"  ← ⑤
def list_sets(conn) -> list[SetOption]: ...           # 정렬 ← ⑤

# services/progress.py
STAGES = ["분류", "검색어 추출", "문서 수집", "검색", "생성", "검증"]  # ADR-015 ③
def to_stage(name: str, data: dict) -> tuple[str, str | None] | None: ...  # (단계, 부가 표시) — 예: ("문서 수집", "Millennium Falcon")

# services/status.py
@dataclass
class Status:
    db_ok: bool; set_count: int; meta_chunks: int; doc_chunks: int
    api_key_set: bool; user_db_ok: bool
    messages: list[str]          # 사용자용 원인·조치 한 줄씩 (NF2004)
    can_chat: bool               # ← ④
def check_status(settings: dict | None = None) -> Status: ...

# services/chat.py
def chat_turn(uid: str, thread: ThreadKey, question: str, *, selected_set: str | None,
              on_stage: Callable[[str, str | None], None] | None = None,
              store: ConversationStore | None = None) -> Turn: ...
# - 직전 history_turns 턴을 history로 넘기고, 응답을 저장한 Turn을 돌려준다.
# - answer()가 needs_input을 돌려준 경우도 저장한다 (대화 흐름 보존).
# - 예외는 answer()가 이미 안전 안내로 바꾼다. 저장 실패는 응답은 보여 주고 warnings에 한 줄 추가.
def response_to_dict(r: OrchestratorResponse) -> dict: ...   # T-014 JSON 계약의 기초
```

### 화면 구성 (Streamlit)
- 사이드바: 상태 배지(문제 있을 때만 안내 문구), 세트 선택 상자, "이 세트 대화 지우기"
- 탭: **아카이브**(선택 세트 대화) / **내 컬렉션**(보유 세트 등록·수정 표 + 질문) / **탐색**(조건 질문 → 결과 카드, 카드의 "이 세트로 대화" 버튼)
- 답변 영역: 답변 본문 → `warnings` 한 줄씩(작은 글씨) → 접힌 "처리 정보"(경로·소요 시간)
- 근거 패널: 인용된 카드만 `[근거 N]` 번호·출처 유형 배지·제목·원문 링크·라이선스·발췌 / 없으면 "답변에 인용된 근거가 없습니다"
- 진행 상태: `st.status`에 STAGES 중 실제로 지나간 단계만 순서대로 표시, 문서 수집은 제목 표시
- 사용자 ID: `?uid=`가 없거나 형식이 틀리면 새로 발급해 URL에 넣는다

## 완료 기준 (DoD)
- [ ] 같은 uid로 다시 접속(새 세션)하면 세트별 대화가 그대로 보이고, 다른 세트의 대화와 섞이지 않는다 (F2009)
- [ ] 탐색 탭에서 결과 카드의 버튼을 누르면 선택 상자가 그 세트로 바뀌고 아카이브 탭으로 이동한다 (F2011)
- [ ] 질문 중 진행 단계가 순서대로 보이고, 수집이 일어나면 문서 제목이 보인다 (F2012)
- [ ] DB 없음·색인 0건·키 미설정·결과 0건 각각 원인·조치 한 줄 안내, 스택트레이스·키·프롬프트 노출 없음 (F2012, NF2004)
- [ ] 근거 패널에 인용 카드만 표시, 인용 없음 안내 (F2008)
- [ ] `app_streamlit.py`에 검색·프롬프트·SQL 로직 없음 — `rag_service.services`의 공개 함수만 import (NF2005, `tests/test_streamlit_app.py`에서 import 목록 검사)
- [ ] `streamlit.testing.v1.AppTest`로 오케스트레이터를 대역(fake)으로 바꾼 화면 테스트: 질문→답변·근거 표시, 탐색 카드→탭 이동, 키 없음 안내
- [ ] user.sqlite 스키마는 기존 컬렉션 데이터를 깨지 않고 추가만 한다 (`CREATE TABLE IF NOT EXISTS`)
- [ ] `.venv\Scripts\python -m pytest -q` 전체 통과 (현재 704)

## 검증 명령
```powershell
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m pytest -q tests/test_conversations.py tests/test_chat_service.py tests/test_streamlit_app.py
.venv\Scripts\streamlit run app_streamlit.py     # PM이 실제 키로 대표 질문 확인·스크린샷
```

## 검토 포인트 결정 (사용자, 2026-10-22 — 7건 모두 권장안)
1. **세트 없는 대화**: 탭별 고정 키. `ThreadKey` = 세트 번호 | `"explore"` | `"collection"`.
2. **저장 내용**: 응답 전체 JSON(`response_to_dict`). 다시 열어도 근거 패널·경고·처리 정보를 복원한다.
3. **사용자와 컬렉션**: 1:1. `collection_id_for(uid) == uid`. 처음 등록할 때 컬렉션 행을 만든다(`CollectionStore`에 `ensure(cid)` 추가 허용).
4. **키 없음**: `Status.can_chat = False`이면 질문 입력만 비활성하고 안내한다. 세트 목록·컬렉션 등록(DB만 사용)은 허용한다.
5. **선택 상자**: 라벨에 대표 별칭 1개(`verified` 우선, 없으면 생략). core 먼저 연도 최신순 → 같은 연도는 번호순, compare_only는 뒤에 두고 라벨 끝에 "(비교용)".
6. **탭 전환**: `st.tabs(key=..., on_change="rerun")`로 코드에서 탭을 바꿀 수 있는지 먼저 확인한다. 안 되면 `st.segmented_control`로 대체하고 결과 보고에 근거(시도한 방법·결과)를 적는다.
7. **진행 단계**: 실제로 지나간 단계만 순서대로 표시한다. 수집이 캐시로 건너뛰어지면(`skipped`, reason=cached) "문서 수집: 이미 수집됨" 한 줄을 남긴다(NF2002 확인용). 이벤트 매핑:

| 표시 단계 | 내부 이벤트 |
|---|---|
| 분류 | `routing`, `resolving` |
| 검색어 추출 | `extracting_queries`(수집), `extracting`(조건 탐색) |
| 문서 수집 | 수집기의 `searching`·`selecting`·`fetching`(제목 표시)·`indexing`·`skipped` |
| 검색 | 오케스트레이터 `searching`·`querying`, `retrieving`, `ranking`, `reviewing` |
| 생성 | `generating` |
| 검증 | `verifying` (이 작업에서 추가) |

   - `searching`은 출처에 따라 뜻이 다르므로, `to_stage`는 이벤트 데이터에 `q`(수집 검색어)가 있으면 문서 수집으로, 없으면 검색으로 분류한다. 이 규칙을 테스트로 고정한다.
   - 알 수 없는 이벤트는 `None`(표시 안 함). 하위 서비스의 `done`은 무시한다.

---

## 결과 보고 (Codex 작성)
- 변경 파일:
  - 생성: `src/rag_service/services/conversations.py`, `users.py`, `status.py`, `catalog.py`, `progress.py`, `chat.py`.
  - 생성: `app_streamlit.py`.
  - 생성: `tests/test_conversations.py`, `test_status.py`, `test_catalog.py`, `test_progress.py`, `test_chat_service.py`, `test_streamlit_app.py` (추가 테스트 57개).
  - 수정: `src/rag_service/db/user_schema.sql` (대화 테이블·인덱스 추가), `src/rag_service/planner/collections.py` (`ensure`만 추가), `src/rag_service/services/orchestrator.py` (실제 검증 조건에서 `verifying`를 발행하는 한 줄만 추가), `config/settings.yaml` (`ui` 섹션만 추가).
  - 문서 변경은 이 작업지시서의 결과 보고 섹션에 한정했다. 새 의존성 없음. 커밋·푸시·브랜치 작업 없음.
- 실행 명령과 결과:
  - `.venv/Scripts/python.exe --version`, `.venv/Scripts/python.exe -m pytest -q`, `.venv/Scripts/python.exe -m pytest -q tests/test_conversations.py tests/test_chat_service.py tests/test_streamlit_app.py`: 기반 인터프리터 프로세스 생성 실패. AGENTS.md §8의 알려진 샌드박스 제약으로 대체 Python 3.12.14를 사용했다.
  - 대체 실행 환경:
    ```powershell
    $env:PYTHONPATH='src;.venv/Lib/site-packages'
    $env:PYTHON_DOTENV_DISABLED='1'
    $env:PYTHONIOENCODING='utf-8'
    $env:ANONYMIZED_TELEMETRY='False'
    & 'C:/Users/SSAFY/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -m pytest -q --basetemp=.pytest_tmp_T013_full
    ```
    **761 passed, 1 warning, 124.21초**. 기존 704개와 추가 57개가 통과했다. 경고 1개는 기존 수집 CLI 오프라인 테스트의 `WIKI_CONTACT` 미설정 안내다.
  - 같은 환경에서 `-m pytest -q tests/test_conversations.py tests/test_status.py tests/test_catalog.py tests/test_progress.py tests/test_chat_service.py tests/test_streamlit_app.py --basetemp=.pytest_tmp_T013_final`: **57 passed, 7.80초**. 컬렉션 수량 수정·전체 삭제에 대한 추가 assertion까지 포함한 최종 검증이다.
  - AppTest: 질문→답변·인용 근거·진행 단계, 새 세션 재접속, 사용자·세트 분리, 대화 삭제, 탐색 카드→선택 상자·아카이브 탭 전환, 키 없음, 결과 없음, 인용 없음, 컬렉션 추가·수정·삭제, 잘못된 uid 재발급, 저장 실패 후 답변 유지 검증.
  - 서비스 테스트: 전체 응답 JSON 복원, 최근 4턴 전달, `needs_input` 저장, 동시 append 20건, 기존 컬렉션 보존, 사용자 ID·대화 키 검증, 실제 로컬 Chroma 건수 조회(임베딩 호출 없음), DB·색인·키·쓰기 오류의 안전한 안내, 수집/검색 이벤트 구분 검증.
  - 실제 DB 읽기 확인: `catalog_options()` **378건 (core 332 / compare_only 46)**. 세트 번호·영문명·대표 별칭과 비교용 표시 확인.
  - `git diff --check`: 통과 (Git의 LF→CRLF 안내만 있음).
- 설계 판단과 이유:
  - 대화는 `(uid, thread)`로 분리하고 전체 응답 JSON을 저장한다. 최근 턴은 ID 역순으로 제한한 뒤 오래된 순으로 돌려주며, 모델에 넘길 턴 수는 `retrieval.history_turns`에서 읽는다. 대화 테이블은 컬렉션 생성에 종속되지 않아 등록 전 아카이브·탐색 대화도 보존된다.
  - 사용자와 컬렉션은 ID를 공유한다. 읽기만 할 때는 컬렉션 행을 만들지 않고 최초 저장 시 `ensure`한다. 공통 `load_collection`/`save_collection` 서비스가 DB 목록·정수 수량을 확인하고 기존 부품 보정을 보존한다.
  - `chat_turn`이 이력·진행 이벤트 변환·오케스트레이터 호출·저장을 담당한다. 저장 실패 시 `id=0`인 Turn에 안전한 경고를 붙여 답변을 반환한다. 화면에서는 미저장 턴도 현재 세션 안에서 유지한다.
  - 상태 점검은 외부 API·임베딩을 호출하지 않는다. 없는 Chroma 컬렉션을 생성하지 않고, 사용자 DB는 실제 INSERT 후 rollback하여 쓰기 가능 여부를 확인한다. 외부 문서 0건은 첫 질문에서 수집할 수 있으므로 안내만 하며, 항목 DB·메타 색인·키·사용자 DB 쓰기 중 준비되지 않은 항목이 있으면 질문을 비활성화한다. 컬렉션 편집은 DB·쓰기 상태만으로 허용한다.
  - `st.tabs(key="active_tab", on_change="rerun")`와 버튼 callback에서의 session state 갱신이 정상 동작했다. `st.segmented_control` 대체는 필요하지 않았다. 설치된 Streamlit 1.62 구현을 확인했고, AppTest에서 실제 callback 실행 후 선택 상자 값과 아카이브 제목을 확인했다. AppTest가 tabs/data_editor 이벤트를 기본 직렬화하지 않는 부분만 테스트 helper에서 WidgetState로 보충했다.
  - 빈 컬렉션의 빈 리스트가 숫자 열로 추론되는 문제를 AppTest로 발견했다. 빈 입력 행을 제공해 세트 문자열 입력을 허용하고, 빈 행은 저장에서 제외하며 수량의 정수형 float만 int로 변환한다. 추가·재실행·수정·삭제까지 검증했다.
  - 진행 단계는 수신 순서대로 표시하고 연속 중복만 생략한다. `searching`의 `q` 키 유무를 구분하며 캐시 건너뛰기·수집 문서 제목도 표시한다. `done`과 미등록 이벤트는 무시한다.
  - UI의 프로젝트 import는 `rag_service.services` 공개 함수·타입으로 한정했다. 검색·프롬프트·SQL은 UI에 없다. 근거는 공통 서비스에서 실제 인용 번호로 필터링하고, 검증 상세·검수 제외 목록은 화면에 표시하지 않는다.
- 남은 이슈 / PM 확인 필요:
  - 구현 범위 밖 수정이 필요한 이슈는 발견하지 못했다.
  - PM 환경의 `.venv`로 전체 테스트 재확인, 실제 키를 사용한 `streamlit run app_streamlit.py` 대표 질문·문서 수집·새로고침 확인과 제출용 스크린샷이 남아 있다. Codex는 키 없이 대역 및 로컬 데이터로 검증했으며 `.env`를 직접 열람·출력하지 않았다.
  - 익명 uid는 결정된 설계대로 URL에 보존된다. 로그인·인증은 이번 범위에 포함하지 않았다. FastAPI는 T-014에서 동일한 `chat_turn`과 컬렉션 서비스를 재사용하면 된다.

## PM 리뷰
- 판정: **승인** (피드백 없음)
- `.venv` 재검증: `pytest -q` **761 passed** (193초). 처음에는 basetemp 권한 오류로 472 errors가 났고, scratchpad basetemp로 다시 실행해 통과했다(환경 문제).
- 코드 리뷰
  - UI import는 `rag_service.services`로만 한정됐다.
  - 오케스트레이터 변경은 `verifying` 1줄, 컬렉션 저장소 변경은 `ensure`뿐이다.
  - 상태 점검은 키 값을 읽지 않고, Chroma 컬렉션을 새로 만들지 않는다.
- 실제 키·브라우저 실측 (Streamlit, 인앱 브라우저)
  - 시작: `?uid=` 발급. 상태 "질문 가능". 세트 378 · 항목 색인 378 · 문서 색인 3,025.
  - 선택 상자: "밀레니엄"으로 검색하면 판본 8개가 나오고, 75192-1에는 "(비교용)"이 붙는다(7,541부품이라 core 범위 밖으로 정상).
  - 사실 경로(75192-1): 미니피그·부품 수 템플릿 답변 + [근거 1] db 카드. 13ms(라우터 캐시).
  - 탐색 탭 "2021년에 나온 마이크로파이터 세트": 진행 단계 분류 → 검색 → 검색어 추출 → 검색 → 검증. 결과 2건(75295-1, 75298-1) 카드, 약 20초.
  - **F2011**: 카드의 "이 세트로 대화"를 누르면 아카이브 탭이 활성화(`aria-selected`)되고 75295-1로 전환된다. `st.tabs(key=)` 방식으로 동작해 대체안은 필요 없었다.
  - 서술 질문 "밀레니엄 팰콘은 영화에서 어떤 역할을 한 배야?": 진행 단계 분류 → 검색어 추출 → 검색 → 생성 → 검증. 인용 근거 1·2·4만 표시(set_meta + 위키 2건), 약 30초.
  - **F2009**: 새로고침 후 같은 세트를 고르면 답변과 근거 패널이 그대로 복원된다.
- 남은 이슈 (M6 또는 해당 작업으로 이월)
  1. 새로고침하면 선택 세트가 첫 번째 옵션으로 돌아간다. 대화는 남아 있다. URL에 `set` 파라미터를 두는 방식을 검토한다(T-015 웹은 localStorage).
  2. 검색어가 모두 이미 수집된 경우 수집기가 `skipped` 대신 `done`을 발행해 "이미 수집됨"이 화면에 나오지 않는다. P1이 캐시이고 모든 검색어가 생략된 경우에만 `skipped`를 낸다(`collect.py` 범위). NF2002 증빙 화면을 위해 M6에서 조건을 완화한다.
  3. (T-010 동작) "2020년 이후 500부품 이하 AT-AT 세트" 질문에서 SQL 후보는 1건(75298-1 AT-AT vs. Tauntaun Microfighters)이었지만 LLM 검수가 제외해 "조건에 맞는 세트가 없습니다"로 답했다. 검수에서 모두 빠지면 0건 안내와 구분해야 한다 → M6 평가 문항에 추가한다.
  4. 처음 질문할 때 브라우저 패널이 닫히자 실행이 중단되어 저장되지 않았다(Streamlit 세션 종료 시 정상 동작). 재현되지 않았다.
