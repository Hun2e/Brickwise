# T-003 문서 수집기 PoC (위키백과 우선)

- 상태: 완료
- 관련 요구사항: F2004(외부 문서 수집·원문 출처), NF2002(수집 캐시), NF2006(외부 API 예절), NF2007(출처·이용 조건), PRD D-04·D-05
- WBS: 4.5(수집기), SP-2(정제 품질), 4.4(별칭 후보 생성 기반)
- 선행 작업: 없음 (T-001·T-002·T-004와 파일이 겹치지 않음)
- 참고 문서: `docs/planning/02_데이터수집설계서.md` §3.2·§5.2·§5.3·§7, `docs/planning/03_아키텍처설계서.md` §4.2
- **Brickipedia(L2)는 사용자 약관 확인 전이므로 구현하지 않는다.** 대신 수집기 인터페이스를 출처에 독립적으로 설계해 나중에 추가하기 쉽게 한다.

## 목표
위키백과(영·한) 문서를 검색·수집·정제해 로컬 문서 저장소에 보관하는 수집기를 만든다.
- 외부 API 예절(User-Agent 연락처, 호출 간격, 429·5xx 재시도)을 지킨다.
- `revid` 기반 캐시로 바뀌지 않은 문서는 다시 받지 않는다.
- 결과 문서는 다음 작업(청킹·`set_docs` 색인)이 그대로 쓸 수 있는 구조여야 한다.

## 역할 분담 (중요)
- **Codex**: 코드와 오프라인 테스트만 작성한다. **네트워크 호출을 실행하지 말고 `.env`를 열지 않는다.** 테스트는 `tests/fixtures/wikipedia/`의 **실제 API 응답 픽스처**(PM이 2026-10-21 수집, README 참고)와 가짜 HTTP 세션으로 한다.
- **PM**: 실제 수집 PoC(`scripts/prefetch_docs.py`)를 실행하고 정제 품질을 검수한다.

## 범위
- 포함
  1. 공통 HTTP 클라이언트: User-Agent, 최소 호출 간격, 재시도, 타임아웃
  2. 위키백과 수집기: 검색, 정보 조회(revid), 본문 조회(TextExtracts 평문 + 섹션), 언어 간 링크
  3. 정제: 섹션 분리, 불필요 섹션 제거, 공백 정리
  4. 문서 저장소: 문서 JSON 파일 + 수집 이력 SQLite(`fetch_log`)
  5. 사전 수집 스크립트(PoC 시드)와 리포트
- 제외: Brickipedia 수집, 청킹·임베딩·Chroma 색인, LLM 호출(P1·P2), UI, 기존 모듈(db·planner·rag) 수정

## 수정·생성할 파일
- `src/rag_service/env.py` — `.env` 1회 로드 헬퍼 (`load_env()`; 기존 `llm.py`의 `_load_environment`와 동작이 같게. **`llm.py`는 수정하지 않는다.** 통합은 이후 정리 작업에서 한다)
- `src/rag_service/data_prep/collectors/__init__.py`
- `src/rag_service/data_prep/collectors/base.py` — 공통 데이터 구조 (`Candidate`, `Doc`, `Section`)와 `Collector` 프로토콜
- `src/rag_service/data_prep/collectors/http.py`
- `src/rag_service/data_prep/collectors/wikipedia.py`
- `src/rag_service/data_prep/collectors/cleaning.py`
- `src/rag_service/data_prep/doc_store.py`
- `scripts/prefetch_docs.py`
- `config/settings.yaml` — `collection`·`external_api` 섹션 보강, `wikipedia` 섹션 추가 (기존 키 유지)
- `tests/test_collectors_offline.py`
- `.gitignore` — 필요 시 `data/collected/` 하위 경로 (이미 `data/collected/cache/` 제외 중. 문서 저장소 경로를 git 제외 대상으로 맞출 것)

## 설정 추가
```yaml
external_api:
  # 기존: request_interval_sec 0.5, max_retries 3, timeout_sec 20
  interval_without_contact_sec: 6.5   # WIKI_CONTACT 없을 때 (분당 10회 제한 대비)
  backoff_base_sec: 2.0
wikipedia:
  langs: ["en", "ko"]
  drop_sections:                       # 대소문자 무시, 앞뒤 공백 무시
    en: ["See also", "References", "Further reading", "External links", "Notes", "Bibliography", "Citations", "Sources"]
    ko: ["같이 보기", "각주", "외부 링크", "참고 문헌", "참고 자료", "출처", "내용주"]
  min_section_chars: 40                # 이보다 짧은 섹션은 제목만 있는 것으로 보고 다음 섹션에 병합하거나 버린다
  seeds:                               # PoC 시리즈·기체 시드 (언어, 제목)
    - ["en", "Lego Star Wars"]
    - ["ko", "레고 스타워즈"]
    - ["en", "Millennium Falcon"]
    - ["en", "X-wing fighter"]
    - ["en", "TIE fighter"]
    - ["en", "AT-AT"]
    - ["en", "Star Destroyer"]
paths:
  docs_dir: "data/collected/docs"      # {source}/{lang}/{page_id}.json
  collect_sqlite: "data/collected/collect.sqlite"   # fetch_log (lego.sqlite·user.sqlite와 분리)
```

## 인터페이스 계약
```python
# collectors/base.py
@dataclass(frozen=True)
class Candidate:                       # 검색 결과 (P2 선별 입력)
    source: str; lang: str; page_id: int; title: str; snippet: str; url: str
@dataclass(frozen=True)
class Section:
    heading: str; level: int; text: str  # 문서 첫머리(리드)는 heading="", level=1
@dataclass
class Doc:
    doc_id: str                        # "wp-en:{page_id}" / "wp-ko:{page_id}"
    source: str                        # "wikipedia"
    lang: str; page_id: int; title: str; url: str; revid: int
    license: str                       # "CC BY-SA 4.0"
    attribution: str                   # "Wikipedia contributors, {title}, {url}"
    sections: list[Section]
    fetched_at: str                    # ISO UTC
    search_query: str | None
    def text(self) -> str: ...         # 섹션을 "## heading\n본문"으로 이어 붙인 평문
class Collector(Protocol):
    source: str
    def search(self, q: str, lang: str, limit: int) -> list[Candidate]: ...
    def latest_revid(self, lang: str, title_or_id: str | int) -> tuple[int, int, str] | None: ...  # (page_id, revid, 최종 제목)
    def fetch(self, lang: str, title_or_id: str | int, search_query: str | None = None) -> Doc | None: ...

# collectors/http.py
class MissingContact(Warning): ...
def build_user_agent(app_name: str, version: str, contact: str | None) -> str
    # "{app}/{ver} ({contact}) python-requests". contact 없으면 "({contact})" 부분 생략 + MissingContact 경고
    # ASCII가 아닌 문자가 있으면 ValueError (HTTP 헤더는 영문만 — 수업 자료 step8 참고)
class HttpClient:
    def __init__(self, user_agent: str, min_interval_sec: float, max_retries: int,
                 timeout_sec: float, backoff_base_sec: float, session=None, sleep=time.sleep, clock=time.monotonic): ...
    def get_json(self, url: str, params: dict) -> dict
    # 요청 사이 최소 간격 보장 (clock·sleep 주입으로 테스트)
    # 429·500·502·503·504 → Retry-After(초) 우선, 없으면 backoff_base * 2**(attempt-1). max_retries 초과 시 UpstreamError
    # 그 밖의 4xx → 즉시 UpstreamError. 예외 메시지에 User-Agent(연락처) 미포함

# collectors/wikipedia.py
class WikipediaCollector:              # Collector 구현
    source = "wikipedia"
    def __init__(self, client: HttpClient, settings: dict): ...
    def langlinks(self, titles: list[str], from_lang: str, to_lang: str) -> dict[str, str | None]
        # 리다이렉트를 따라가고 결과 키는 요청한 원래 제목. 링크 없으면 None
    # API 파라미터: format=json, formatversion=2, redirects=1
    # 본문: prop=extracts|info|revisions, explaintext=1, exsectionformat=wiki, rvprop=ids
    # 없는 문서(missing) → fetch는 None, latest_revid도 None

# collectors/cleaning.py
def split_sections(extract: str) -> list[Section]
    # "== H ==", "=== H ===", ..., 비정상 표기 "==== = H = ====" 도 제목으로 인식하고 제목 텍스트에서 = 와 공백을 제거
def clean_sections(sections: list[Section], drop_headings: list[str], min_chars: int) -> list[Section]
    # drop 대상 섹션과 그 하위 섹션 전체 제거 (예: "References" 아래 하위 섹션도 제거)
    # 연속 빈 줄 1개로, 앞뒤 공백 제거, 빈 섹션 제거

# data_prep/doc_store.py
class DocStore:
    def __init__(self, docs_dir: Path, collect_sqlite: Path): ...    # fetch_log 스키마 자동 생성
    def get(self, doc_id: str) -> Doc | None
    def save(self, doc: Doc) -> Path                                  # 원자적 쓰기 (임시 파일 → 교체)
    def stored_revid(self, doc_id: str) -> int | None
    def log_fetch(self, source, query, doc_id, revid, status) -> None  # status: fetched | cached | missing | error
    def queried(self, source: str, query: str) -> bool                # NF2002: 이미 수집한 검색어
def ensure_doc(store: DocStore, collector: Collector, lang: str, title: str, search_query=None) -> tuple[Doc | None, str]
    # latest_revid → 저장본 revid와 같으면 ("cached") 저장본 반환, 다르거나 없으면 fetch 후 저장 ("fetched"), 없는 문서 ("missing")
```

## 사전 수집 스크립트 (`scripts/prefetch_docs.py`)
- `--source wikipedia --seeds` : settings `wikipedia.seeds`를 수집한다. 영어 시드의 한국어 언어 링크가 있으면 한국어 문서도 수집한다.
  - PM 확인: "레고 스타워즈"·"밀레니엄 팰컨"·"X-윙"·"타이 파이터"·"스타 디스트로이어"는 있다. AT-AT는 "Walker (Star Wars)"로 리다이렉트되고 한국어 링크가 없다.
- `--limit N` : 최대 수집 문서 수
- 리포트 (콘솔):
  - 문서별 doc_id, 제목, 상태(fetched/cached/missing), revid
  - 정제 전·후 글자 수, 남은 섹션 수와 버린 섹션 수
  - 전체 소요 시간, HTTP 요청 수
- 두 번째 실행에서는 모든 문서가 `cached`여야 한다 (본문 재요청 0회, revid 확인 요청만).
- `WIKI_CONTACT`가 없으면 경고를 출력하고 `interval_without_contact_sec` 간격으로 진행한다.

## 테스트 (`tests/test_collectors_offline.py`) — 네트워크 없이
- 가짜 세션이 픽스처 JSON을 돌려주게 해서 다음을 검증한다.
  - search: 픽스처 `search_en_lego_star_wars`의 5개 결과가 Candidate로 변환되고, snippet의 HTML 태그(`<span class="searchmatch">`)가 제거되며, URL이 올바름
  - fetch: `extract_en_lego_star_wars` → Doc. `See also`·`References`·`Further reading`·`External links`가 제거되고, `Overview`·`Development` 등은 남음. `==== = List of Ultimate Collectors Series sets = ====` 제목도 정상 인식
  - 한국어 `extract_ko_lego_star_wars` → `같이 보기`·`외부 링크` 제거
  - langlinks: Millennium Falcon → "밀레니엄 팰컨", AT-AT → None (리다이렉트 후 원래 제목 키 유지)
  - missing_page → fetch None
- HttpClient: 최소 간격 sleep 호출, 429 + Retry-After 후 성공, 503 백오프, 재시도 초과 → UpstreamError, 404 즉시 실패, 예외 메시지에 User-Agent 미포함
- build_user_agent: 연락처 포함·생략(경고)·비ASCII 거부
- DocStore·ensure_doc: 첫 호출 fetched → 같은 revid면 cached(본문 요청 없음) → revid 바뀌면 다시 fetched, 원자적 저장, `queried()` 동작
- Doc.text(), doc_id 형식, license·attribution 필드

## 완료 기준 (DoD)
- [ ] `pytest -q` 전체 통과 (기존 81개 회귀 없음)
- [ ] `scripts/prefetch_docs.py --help` 동작
- [ ] 네트워크 호출·`.env` 열람 없음, 새 의존성 없음 (`requests`는 이미 requirements에 있음. HTML 제거는 표준 라이브러리)
- [ ] 기존 모듈(`llm.py`, db, planner, rag) 미수정

## 검증 명령
```powershell
.venv\Scripts\python -m pytest -q --basetemp=<쓰기 가능한 경로>
.venv\Scripts\python scripts\prefetch_docs.py --help
```

---

## 결과 보고 (Codex 작성)
- 변경 파일:
  - `src/rag_service/env.py`: 명시적으로 호출할 때만 1회 환경 로드. `llm.py`는 그대로 유지.
  - `src/rag_service/data_prep/collectors/{__init__,base,http,cleaning,wikipedia}.py`: 공통 계약, 요청 간격·재시도·안전한 오류, 검색·revision·본문·언어 링크 수집과 섹션 정제.
  - `src/rag_service/data_prep/doc_store.py`: 원자적 JSON 저장, 독립 SQLite 수집 이력, revision 및 언어 링크 캐시.
  - `scripts/prefetch_docs.py`: 시드·한국어 링크 수집, 중복 제거·수량 제한, 문서별 정제 통계 및 요청 수 리포트.
  - `config/settings.yaml`, `.gitignore`: 위키백과·HTTP 설정과 저장 경로 추가, 생성물 제외. 기존 설정 키 유지, 기존 collection 값 재정의 없음.
  - `tests/test_collectors_offline.py`: 실제 API 응답 픽스처·가짜 세션 기반 41개 테스트.
  - 이 작업지시서의 결과 보고 섹션만 수정.
- 실행 명령과 결과:
  - AGENTS.md의 환경 메모에 따라 대체 Python `C:/Users/SSAFY/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe` (3.12.14)를 사용. `PYTHONPATH=.venv/Lib/site-packages`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONIOENCODING=utf-8`, `PYTHON_DOTENV_DISABLED=1`을 설정.
  - 전체 검증: 위 Python의 표준입력 실행(`python -`)에서 `sys.addaudithook`으로 `.env` 열기와 `socket.connect`·`socket.getaddrinfo`를 차단한 뒤 `pytest.main(['-q', '--basetemp=C:/ssafy/08.AI_core_PJT/.tmp-t003-pytest-final'])` 실행 → **122 passed in 34.81s** (기존 81개 + 신규 41개, skip 없음).
  - 집중 검증: 위 Python으로 `-m pytest tests/test_collectors_offline.py -q --basetemp=C:/ssafy/08.AI_core_PJT/.tmp-t003-focused` → **41 passed in 0.52s**. 테스트 자체에서도 네트워크·`.env` 접근을 차단하며 환경 로드는 mock 처리.
  - 첫 전체 실행(`--basetemp=C:/ssafy/08.AI_core_PJT/.tmp-t003-pytest`)은 119 passed / 1 failed. 작업지시서의 비정상 제목 예시가 원본 픽스처에 있다고 가정한 테스트가 실패했으며, 실제 정상 제목과 비정상 표기 변형을 각각 검증하도록 수정한 뒤 위 전체 검증 통과. 원본 픽스처는 수정하지 않음.
  - 도움말: 동일 감사 훅 아래 `sys.argv=['scripts/prefetch_docs.py', '--help']`와 `runpy.run_path('scripts/prefetch_docs.py', run_name='__main__')` 실행 → exit 0, 옵션 표시 정상. 환경 로드와 수집은 실행되지 않음.
  - 가짜 세션으로 CLI 수집 2회 실행: 첫 실행 영·한 2문서 fetched, HTTP 5회; 저장소 재생성 후 두 번째 실행 2문서 cached, HTTP 2회 모두 `info|revisions`, 본문·언어 링크 재요청 0회.
  - `git diff --check` 통과. `git diff --name-only -- src/rag_service/llm.py src/rag_service/db src/rag_service/planner src/rag_service/rag requirements.txt AGENTS.md CLAUDE.md README.md` 출력 없음. 네트워크 호출·`.env` 열람·새 의존성·commit/push/branch 작업 없음.
  - 임시 폴더 정리 시도: 워크스페이스 안의 일반 디렉터리임을 확인했으나, 자동 승인 검토가 `Remove-Item -LiteralPath ... -Recurse -Force`를 `blocked by policy`로 거부. 명시적 절대 경로로 좁힌 삭제도 거부되어 **임시 폴더 삭제는 미완료**.
- 설계 판단과 이유:
  - HTTP 최초 요청 이후 설정된 횟수만 재시도. Retry-After의 초 값을 우선하고 유효하지 않으면 지수 백오프 적용. 재시도에도 최소 간격 적용, 응답을 닫고 예외 메시지에 연락처·원문 오류를 포함하지 않음.
  - 정제는 제외 제목의 하위 트리까지 제거. 설정 길이보다 짧은 섹션은 계약에서 허용한 버리기 방식을 선택. 원본의 5단계 제목과 `==== = ... = ====` 변형을 모두 검증.
  - `Doc.cleaning_stats`를 기본값 있는 추가 필드로 저장하여 캐시에서도 정제 전후 글자 수·섹션 수를 보고. 기존 필수 필드와 `text()` 계약 유지.
  - 저장소의 문서 목록은 source/lang/page_id로 조회하므로 `ensure_doc`가 위키백과 doc_id 접두어에 의존하지 않음. JSON은 같은 디렉터리의 임시 파일을 flush/fsync 후 교체하며, 교체 실패 시 기존 파일 보존과 임시 파일 정리를 검증.
  - `fetch_log`는 실행 이력을 보존하도록 추가 기록 방식 사용. `queried()`는 fetched/cached/missing 이력만 인정하여 오류만 발생한 검색어는 재시도 가능.
  - 언어 링크 없음도 `(doc_id, revid, target_lang)`별로 SQLite에 캐시. 두 번째 실행의 revision 확인 요청만 허용하는 조건을 충족하고, revision이 달라지면 링크도 다시 조회.
- 남은 이슈 / PM 확인 필요:
  - **삭제 필요 경로**: `C:/ssafy/08.AI_core_PJT/.tmp-t003-pytest`, `C:/ssafy/08.AI_core_PJT/.tmp-t003-focused`, `C:/ssafy/08.AI_core_PJT/.tmp-t003-pytest-final`. 자동 승인 검토의 정책 차단 외에 상세 거부 사유는 제공되지 않음.
  - PM이 `.venv`에서 전체 테스트를 재검증하고 실제 `scripts/prefetch_docs.py --source wikipedia --seeds`를 두 번 실행하여 운영 환경 요청·정제 품질을 확인할 것. 실제 수집은 사용자 지시대로 실행하지 않음.
  - 언어 링크는 원문 revision 기준 캐시이므로 원문 revision 변경 없이 언어 링크만 바뀌는 경우 별도 갱신 정책은 후속 작업에서 검토 필요.
  - Brickipedia·LLM 선별·청킹·색인은 이번 범위에서 구현하지 않음. DoD의 오프라인 검증 항목은 통과했으며, 사용자 요청 중 임시 폴더 삭제만 실행 정책으로 미완료.

## 수집 PoC 실행 결과 (PM 작성)
- 실행 조건: 2026-10-21, `prefetch_docs.py --source wikipedia --seeds`를 2회 실행했다. WIKI_CONTACT가 설정되어 있어 요청 간격은 0.5초였다.
- 결과
  - 1회차: **11문서 fetched** (영 7, 한 4), HTTP 28회, 15.6초. AT-AT는 리다이렉트되어 `Walker (Star Wars)`를 받았고 한국어 링크는 없다.
  - 2회차: **11문서 모두 cached**, HTTP 11회(판번호 확인만), 5.7초 → NF2002 캐시 동작을 확인했다.
  - 정제 전·후 글자 수 예: Lego Star Wars(영) 27,658 → 23,557, 밀레니엄 팰컨(한) 545 → 494, Star Destroyer(영) 36,406 → 35,453.
- 정제 품질 검수 (PM이 문서 3건의 섹션 전체를 확인)
  - 섹션 계층(L1 리드 ~ L5)이 보존되고, 비정상 제목 `==== = List of … = ====`도 L5 "List of Ultimate Collectors Series sets"로 인식했다.
  - References·See also·External links·같이 보기·외부 링크는 하위 섹션까지 제거됐다. 본문 섹션 손실은 없다.
  - license·attribution(작성자·제목·URL) 필드가 채워져 있어 근거 표시에 그대로 쓸 수 있다.
- **발견 1: TextExtracts는 표(wikitable)를 내보내지 않는다.** "List of UCS sets" 섹션이 165자 설명만 남았다. 세트 목록·연도 같은 표 정보는 위키 문서로 얻을 수 없다. 이 정보는 설계대로 DB(Rebrickable)가 담당하므로 문제는 없지만, 설계문서에 근거로 기록한다.
- **발견 2: 한국어 위키 문서가 짧다.** 밀레니엄 팰컨(한)은 494자로 영어판 19,435자의 약 2.5%다. 한국어 질문에도 영어 원문 근거가 필요하다는 근거이며, 교차 언어 전략 ②(질의 확장)를 뒷받침한다.

## PM 리뷰
- 판정: **승인** (2026-10-21)
- PM 재검증 (`.venv`): pytest **122 passed**, `--help` 동작. 금지 모듈(llm.py·db·planner·rag) 변경 없음.
- 코드 리뷰
  - HTTP: 요청 간격 보장, 429·5xx는 Retry-After를 우선하고 없으면 지수 백오프, 그 밖의 4xx는 즉시 실패. 오류 메시지에 User-Agent·서버 원문이 들어가지 않는다.
  - 문서 저장은 원자적이며, 판번호 비교 캐시가 동작한다.
- 후속 메모
  1. Retry-After에 상한이 없어 서버가 긴 값을 주면 그만큼 기다린다 → 상한값(예: 60초) 설정 추가
  2. 타임아웃 등 연결 오류는 재시도하지 않는다 → RAG 코어 작업에서 재시도 정책과 함께 검토
  3. `env.py`와 `llm.py`의 `.env` 로드 중복 → 정리 작업에서 통합
- 환경: Codex 샌드박스가 저장소 루트에 만든 `.tmp-t003-*` 폴더 3개는 권한 때문에 삭제할 수 없다. `.gitignore`에 추가했다.
