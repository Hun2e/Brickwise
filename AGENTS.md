# AGENTS.md — RAG 기반 도메인 지식 서비스 (AI 코어 프로젝트 2회차)

> 이 파일이 Claude Code와 Codex가 함께 따르는 **유일한 에이전트 지침**이다.
> `CLAUDE.md`는 `@AGENTS.md` 한 줄만 둔다. 지침을 바꿀 때는 이 파일만 고친다 (수정 권한: PM).

---

## 1. 프로젝트 개요

- 사용자가 고른 도메인의 **항목 메타데이터(RAG 1)** 와 **외부 문서(RAG 2)** 에서 근거를 찾아 답하고, 답변에 쓴 근거의 제목과 원문 출처를 함께 보여 주는 서비스를 만든다.
- 도메인: **LEGO — Series Archive AI(시리즈·제품 지식 RAG) + Custom Build Planner(보유 세트 기반 부품 분석·추천)**. 1차 기획 검토는 `docs/planning/00_LEGO_1차기획검토.md`에 있다.
  - 항목(RAG 1) = 레고 세트 (권장 범위: Star Wars 2015년 이후·100~1,500부품 약 332세트). 구조화 데이터 = Rebrickable CSV(SQLite). 외부 문서(RAG 2) = Brickipedia(CC-BY-SA, 사용 승인 대기) + 위키백과.
  - **LEGO.com·LEGO 코리아 콘텐츠와 공식 조립 설명서는 수집·색인하지 않는다** (LEGO 약관의 AI 목적 이용 금지 조항). 링크만 제공한다.
  - **숫자·세트 번호·부품 번호는 DB에서만 가져온다.** 부품 매칭은 LLM 추론이 아니라 `(part_num, color_id)` 기준 결정적 알고리즘으로 계산한다.
  - Rebrickable CSV 원본은 `data/raw/rebrickable/`(git 제외, `scripts/load_rebrickable.py`로 내려받음)에 두고 자동 다운로드는 하루 1회 이하로 한다. 출처를 표기한다.
- **현재 단계: 기획 완료 (PRD·데이터 수집 설계·아키텍처·WBS).** 사용자가 구현을 승인하기 전까지 Codex를 호출하지 않는다. 기준 문서는 `docs/planning/01_PRD.md`, `02_데이터수집설계서.md`, `03_아키텍처설계서.md`, `04_개발로드맵_WBS.md`이다. 첫 작업 묶음은 WBS §5의 T-001~T-004이다.
  - 그래프 원칙: 과제 필수 RAG 그래프는 `retrieve → generate` 두 노드다. DB·planner 결과는 별도 노드가 아니라 state의 `injected_cards`로 주입한다. 라우팅은 그래프 바깥 `services/orchestrator.py`가 맡는다. Brickipedia 사용(L2)은 사용자의 Fandom 약관 확인 전까지 보류한다.
  - 부품 집계 정의: 최신 인벤토리, 스페어 제외, 미니피그 구성 부품 전개, `(part_num, color_id)` 합산. 이 정의로 `sets.num_parts`가 재현되어야 한다 (범위 332세트 중 331세트 일치 확인).
- 선택한 심화 과제: **F2013 프론트엔드 직접 구현** (Vanilla HTML/CSS/JS + FastAPI). Streamlit 필수 UI도 함께 유지해 결과를 비교한다. 다른 심화 과제는 `docs/05_심화고도화로드맵.md`에서 정한다.

### 요구사항 ID

| ID | 내용 | 구분 |
|---|---|---|
| F2001 | 항목 메타데이터 로드, 항목 1건을 색인 텍스트 1개로 구성 (약 100건 이상) | 필수 |
| F2002 | 항목 텍스트 임베딩 색인 (RAG 1). 청크 수 확인 가능, 다시 색인해도 중복 없음 | 필수 |
| F2003 | 프롬프트1: 도메인 전문가 역할로 검색어 추출. 파싱 가능한 출력. 항목 자체 문서는 항상 포함 | 필수 |
| F2004 | 외부 출처 검색, 프롬프트2로 후보 선별, 본문과 원문 출처 수집. 항목 자체 문서는 항상 색인. 이미 수집한 검색어는 다시 수집하지 않음 | 필수 |
| F2005 | 청킹해서 별도 컬렉션에 색인 (RAG 2). 청크 크기·중복 길이는 설정으로. 청크 ID가 서로 덮어쓰지 않음 | 필수 |
| F2006 | 선택 항목의 메타 청크는 항상 포함. 두 컬렉션 결과를 병합하고 중복 제거. Top-k는 설정으로 | 필수 |
| F2007 | LangGraph retrieve→generate 두 노드. 검색된 문서만 사용, 근거가 없으면 "확인되지 않음". 출처 유형 구분 | 필수 |
| F2008 | 주요 주장에 `[근거 N]` 표시. 실제로 인용한 근거만 제목과 원문 출처로 표시. 인용이 없으면 안내 | 필수 |
| F2009 | 목록에서 항목 선택, 항목별로 대화 기록을 분리 | 필수 |
| F2010 | 도메인 특화 기능 1개 이상, 근거와 함께 제공 | 필수 |
| F2011 | 자연어 조건으로 항목 탐색 → 조건에 맞는 항목만 제시 → 선택하면 그 항목 대화로 이동 | 필수 |
| F2012 | 수집·생성 진행 상태, 색인 없음·키 누락·결과 없음 안내, 위험 도메인 면책 문구 | 필수 |
| F2013 | HTML/CSS/JS 화면, 화면과 서버 분리, 요청·응답 형식 정의, 질문·답변·근거 표시 | 심화(선택함) |
| F2014 | MMR 검색 (fetch_k를 넉넉히 → 다양성으로 선별), 유사도 검색과 차이 확인 | 심화 |
| F2015 | RAG 사용 전후 답변 비교 문서 (대표 질문 5개 이상) | 심화 |
| F2016 | 종류가 다른 문서 출처 결합, 근거에 출처 종류 표시 | 심화 |
| NF2001 | 대표 질문 응답 시간이 사용에 지장 없을 것 | 비기능 |
| NF2002 | 수집 캐시 (이미 수집한 검색어는 재수집 안 함) | 비기능 |
| NF2003 | API 키는 환경변수로만, 소스·제출물에 포함 금지 | 비기능 |
| NF2004 | 화면에 키·전체 프롬프트·스택트레이스 노출 금지 | 비기능 |
| NF2005 | 수집·색인·검색·생성·UI 모듈 분리, 도메인 의존 코드는 데이터 준비 영역에 | 비기능 |
| NF2006 | 외부 API 예절: 재시도·호출 간격, User-Agent에 연락처 | 비기능 |
| NF2007 | 데이터·문서의 출처와 이용 조건 기록 | 비기능 |

---

## 2. 역할

| 역할 | 담당 | 책임 |
|---|---|---|
| **PM** | Claude Code | 이 파일 관리, 작업지시서(`docs/pm/tasks/`) 작성, 산출물 문서 작성, 작업 에이전트용 프롬프트 제공, 결과 리뷰(diff·DoD 실행·요구사항 대조), 커밋, `docs/pm/progress.md`·`decisions.md` 관리 |
| **개발** | 작업 에이전트 (Codex 등, 사용자가 직접 실행) | 작업지시서 범위 안에서 코드 구현과 테스트, 작업지시서의 **결과 보고** 섹션 작성 |

### Codex(개발) 규칙
- 작업지시서에 적힌 파일과 범위만 수정한다. 범위를 넘어야 하면 고치지 말고 결과 보고의 "남은 이슈"에 적는다.
- `docs/` 아래에서는 **자기 작업지시서의 결과 보고 섹션만** 수정한다. `AGENTS.md`, `CLAUDE.md`, `README.md`, 산출물 문서는 수정하지 않는다.
- git commit·push·branch 작업은 하지 않는다 (PM이 리뷰 후 커밋).
- `.env`를 작업 중에 열람하거나 출력하지 않는다. **단, 실행 시점에 코드가 `.env`를 로드하는 것(`rag_service.env.load_env`, `llm.py`)은 정상 동작이므로 막지 않는다** (T-005 교훈). 키가 필요한 검증은 키가 없을 때의 동작(안내 메시지)까지 확인하고, 실제 호출 결과는 PM이 확인한다.
- 새 의존성이 필요하면 `requirements.txt`에 고정 버전으로 추가하고 결과 보고에 이유를 적는다.

### PM(Claude) 규칙
- 작업지시서 한 장에는 독립적으로 검증할 수 있는 단위 하나만 담는다.
- **Codex를 직접 실행하지 않는다** (2026-10-23 사용자 결정). 작업지시서를 쓴 뒤 사용자가 작업 에이전트에 붙여 넣을 프롬프트를 코드 블록으로 준다. 사용자가 완료를 알리면 PM이 리뷰·검증·커밋을 이어서 한다.
- 프롬프트 기본형 (금지 범위·검증 명령·미완성 변경 파일이 있으면 덧붙인다):
  ```
  AGENTS.md를 따르고 docs/pm/tasks/T-0NN_<이름>.md를 구현하라.
  운영 data/와 eval/*.jsonl은 수정하지 마라. git commit·push는 하지 마라.
  끝나면 node --test web/tests/*.test.mjs와 pytest 전체를 실행하고 작업지시서의 결과 보고 섹션을 채워라.
  ```
- 리뷰를 통과한 작업만 커밋한다. 커밋 메시지는 `[T-0NN] 요약 (F20xx)` 형식으로 쓴다.
- 설계 결정과 겪은 문제·해결은 `docs/pm/decisions.md`에 남긴다 (설계문서의 "선택 이유·문제·해결" 재료).

---

## 3. 리포 지도와 모듈 경계 (NF2005)

```
CLAUDE.md / AGENTS.md / README.md
requirements.txt  .env.example  config/settings.yaml
docs/                      산출물 문서 (PM 작성)
  01_요구사항정의서.md  02_RAG파이프라인설계.md  03_검증결과.md
  04_RAG전후비교.md     05_심화고도화로드맵.md   screenshots/
  pm/ progress.md  decisions.md  tasks/
  planning/                서비스 기획서·데이터 출처 조사 (PM 작성)
data/sources.yaml          외부 출처 레지스트리 (출처·라이선스·URL·수집일)
data/lego.sqlite           Rebrickable CSV 적재 RDB (274MB라 git 제외 — raw CSV로 `scripts/load_rebrickable.py` 재생성)
data/raw/                  항목 메타데이터 원본 (출처 기록 필수)
data/collected/            수집 문서와 수집 매니페스트 (git 추적. cache/·embed_cache.sqlite(134MB)는 제외)
data/chroma/               Chroma 영속 저장소 (git 추적. eval_snapshot/도 추적)
src/rag_service/
  config.py                settings.yaml + 환경변수 로드. 모든 튜닝 값의 단일 출처
  data_prep/               ★ 도메인에 묶인 코드는 전부 여기
    rebrickable_loader.py  CSV → SQLite 적재·파생 테이블
    scope.py               범위 세트 선정 (core / compare_only)
    item_text.py           세트 1건 → 색인 텍스트
    aliases.py             한국어 별칭 사전
    collectors/            http.py(UA·간격·재시도), brickipedia.py, wikipedia.py
  db/                      schema.sql, connection.py, queries.py(조회·필터·비교·집합 연산)
  planner/                 inventory.py(집계), matcher.py(부족 계산·모드), recommend.py(순위) — LLM 없음
  vectorstore/             store.py(컬렉션), chunking.py, indexer.py, bm25.py
  rag/                     prompts.py, router.py, filter_parser.py, query_extract.py(P1),
                           select_docs.py(P2), retrieve.py, graph.py(retrieve→generate 2노드),
                           evidence.py(근거 카드), citations.py, verify.py(수치 검증)
  services/                archive.py, build.py, search.py, orchestrator.py(질의 라우팅)
  observability.py         토큰·지연 로깅 (logs/usage.jsonl, git 제외)
scripts/                   load_rebrickable.py, index_items.py, prefetch_docs.py, eval_*.py, build_submission.ps1
eval/                      평가 데이터셋 (*.jsonl)
app_streamlit.py           필수 UI (Streamlit)
server/                    FastAPI (main.py, schemas.py) — F2013 API
web/                       Vanilla HTML/CSS/JS — F2013 화면
tests/                     pytest
```

- UI 계층(`app_streamlit.py`, `server/`, `web/`)은 `src/rag_service`의 공개 함수만 호출한다. UI 안에 검색·프롬프트 로직을 넣지 않는다.
- Streamlit과 FastAPI는 **같은 서비스 함수**를 공유한다. 기능을 한쪽에만 구현하지 않는다.

---

## 4. 기술 스택 (명세 고정 버전)

Python 3.12 (`.venv`). 주요 라이브러리: langchain 1.3.14 · langgraph 1.2.10 · langsmith 0.10.17 · langchain-core 1.6.0 · langchain-openai 1.4.3 · langchain-community 0.4.2 · langchain-chroma 1.1.0 · langchain-text-splitters 1.1.2 · chromadb 1.5.9 · openai 2.54.0 · streamlit 1.62.0 · requests 2.34.2 · python-dotenv 1.2.3 · typing-extensions 4.16.0. 심화용: fastapi 0.141.1 · uvicorn[standard] 0.52.4 · pydantic 2.13.5. 실제 고정 목록은 `requirements.txt`가 기준이다.

- LLM·임베딩은 SSAFY GMS(OpenAI 호환)를 쓴다. `base_url`과 모델명은 `config/settings.yaml`에서 읽는다.
- 키는 환경변수 `GMS_KEY`, 위키 연락처는 `WIKI_CONTACT`로 읽는다.

---

## 5. 구현 규칙

1. **설정 분리**: 청크 크기·중복 길이·Top-k·검색 방식(similarity/mmr)·fetch_k·모델명·경로는 `config/settings.yaml`에만 둔다. 코드에 숫자를 직접 쓰지 않는다.
2. **컬렉션**: 항목 메타데이터와 외부 문서는 서로 다른 컬렉션에 둔다 (이름은 settings에 둔다).
3. **ID 규칙**
   - 메타 청크 ID = `item_id`. 다시 색인하면 덮어쓰기(upsert)만 일어나야 한다.
   - 외부 문서 청크 ID = `{scope}:{doc_id}:{seq}` (scope: `entity` 기체·캐릭터 문서 / `series` 시리즈 문서 / 세트 전용 문서는 `{set_num}`). 기체 문서는 1벌만 저장하고 청크 메타 `sets` 배열에 연결 세트를 둔다. **새 세트를 연결할 때는 `sets`를 덮어쓰지 말고 합집합으로 갱신한다** (ADR-008).
4. **청크 메타데이터**: `source_type`(set_meta | db | brickipedia | wikipedia), `title`, `url`, `license`, `doc_id`(위키면 page_id), `item_id`(= set_num, 예: 75192-1), `item_title`, `theme`, `year`, `lang`, `search_query`, `collected_at`.
   - `db` 근거는 SQL·부품 매칭 결과를 근거 카드로 만든 것이다. 출처는 Rebrickable이다. 답변의 숫자는 이 근거에 있어야 한다 (사후 검증).
5. **인용**: 프롬프트3은 `[근거 N]` 형식을 쓰게 한다. 화면에는 실제로 인용된 근거만 보여 준다. 인용이 없으면 "답변에 인용된 근거가 없습니다"라고 안내한다.
6. **프롬프트**: 세 프롬프트(검색어 추출·후보 선별·답변 생성)는 `rag/prompts.py`에 모은다. 프롬프트1·2의 출력은 JSON이고, 파싱에 실패하면 안전한 기본값으로 돌아간다 (항목 자체 문서는 반드시 포함).
7. **오류 처리**: 사용자에게는 원인과 조치만 짧게 보여 준다. 스택트레이스·프롬프트 전문·키는 로그에만 남기고 화면에 노출하지 않는다 (NF2004).
8. **외부 호출 예절** (NF2006): User-Agent는 `AppName/버전 (WIKI_CONTACT) python-requests` 형식이고 영문만 쓴다. 429/5xx가 오면 `Retry-After`를 따르는 재시도를 최대 N회 하고, 요청 사이 간격을 둔다 (값은 settings에서).
9. **캐시** (NF2002): 수집 매니페스트에 항목별로 검색한 키워드와 수집한 doc_id를 기록하고, 이미 있는 키워드는 다시 수집하지 않는다.
10. **코드 스타일**: 타입 힌트, 짧은 docstring(한국어 가능), 함수는 작게. 주석은 "왜"를 설명할 때만 단다.

---

## 6. 보안과 법·윤리

- `.env`, 키, 토큰은 절대 커밋하거나 출력하지 않는다. 예시는 `.env.example`에만 둔다.
- 수집한 데이터·문서의 출처와 이용 조건(예: 위키백과 CC BY-SA 4.0)을 `README.md`와 `docs/01_요구사항정의서.md`에 기록한다 (NF2007).
- 위험 도메인(건강·금융 등)을 고르면 모든 답변 화면에 면책 문구를 표시한다.

---

## 7. 작업 프로토콜 (작업지시서)

파일: `docs/pm/tasks/T-0NN_<이름>.md`. 형식은 `docs/pm/tasks/_TEMPLATE.md`를 따른다.

1. Codex는 작업지시서의 목표·범위·인터페이스·DoD를 읽는다.
2. 구현하고 DoD 명령을 직접 실행한다.
3. **결과 보고** 섹션에 다음을 적는다:
   - 변경 파일 목록
   - 실행한 명령과 결과 요약 (통과/실패, 숫자)
   - 설계상 판단과 그 이유
   - 남은 이슈와 PM 확인이 필요한 점
4. PM이 리뷰한다. 반려하면 작업지시서 아래에 "PM 피드백 N차"를 덧붙이고 다시 호출한다.

---

## 8. 실행·테스트 명령 (Windows, 리포 루트 기준)

```powershell
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pytest -q
.venv\Scripts\python scripts\index_items.py           # RAG 1 색인
.venv\Scripts\streamlit run app_streamlit.py          # 필수 UI
.venv\Scripts\uvicorn server.main:app --reload        # F2013 API + web/
```

- `src/`는 패키지 경로로 import한다 (`pytest`는 `pyproject`/`conftest.py`에서 `src`를 경로에 추가).
- 키가 필요한 테스트는 `GMS_KEY`가 없으면 skip한다.
- **환경 메모 (T-001에서 확인)**: Codex 샌드박스에서는 `.venv\Scripts\python`을 실행할 수 없다 (기반 인터프리터 접근 거부). Codex는 대체 Python + `PYTHONPATH=.venv/Lib/site-packages`로 검증하고 결과 보고에 명시한다. PM이 `.venv`로 다시 검증한다. pytest 기본 임시 폴더 권한 오류가 나면 `--basetemp=<쓰기 가능한 경로>`를 지정한다.

---

## 9. 제출 규칙

- 산출물: 요구사항 정의서, RAG 파이프라인 설계문서, 검증 결과(4장 확인 화면·요구사항 대조표·테스트 결과표·셀프/상호 체크리스트·제출 전 점검), 소스코드(수집·색인 스크립트, 심화 코드 포함), README.md(개요·실행법·필수/심화 기능·색인 절차·출처/이용 조건·심화 요약).
- 파일 이름: `AI코어프로젝트2_[주제명]_지역_반_성명.zip`. `scripts/build_submission.ps1`로 만든다 (키 포함 여부 검사 포함).
- 제출물에 `.env`, `.venv`, `data/chroma`, `00. Docs`, `01.Prepare`를 넣지 않는다.
