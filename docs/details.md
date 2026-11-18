# LEGO Archive & Build Planner — RAG 기반 레고 스타워즈 지식·제작 도우미

> SSAFY AI 코어 프로젝트 · 개인 프로젝트 (2026.10)
> LEGO Group과 무관한 비공식 팬 프로젝트입니다.

## 개요
레고 스타워즈 세트에 대해 **한 대화 안에서** 검색하고, 질문하고, 내 보유 세트를 등록하고, 가진 부품으로 만들 수 있는 세트를 추천받는 챗봇입니다.

- **RAG 1 (항목 메타데이터)**: Rebrickable 세트 정보 378건(핵심 332 + 판본 비교용 46)을 세트 1건 = 1청크로 색인합니다.
- **RAG 2 (외부 문서)**: 위키백과(한·영) 문서를 질문할 때 검색어를 추출(프롬프트1)하고 후보를 선별(프롬프트2)해 수집·청킹·색인합니다. 현재 135문서 · 3,529청크입니다.
- **답변**: LangGraph `retrieve → generate` 두 노드가 검색된 근거만으로 답하고 `[근거 N]`을 표시합니다. 숫자(부품 수·연도·부족 수량)는 LLM이 아니라 **DB와 결정적 계산**에서만 가져오고, 답변 뒤 수치 사후 검증으로 근거 없는 숫자를 지웁니다.
- **도메인 특화 기능**: 보유 세트의 부품으로 `(부품 번호, 색상)` 기준 부족 수량을 계산하고, 부족이 적은 세트를 추천합니다(색상 무시 모드 지원).

대표 대화 예:
```
루크가 들어 있는 엑스윙 찾아줘 → 그중 2021년에 나온 건? → 부품이 몇 개야?
→ 그거 하나 보유 목록에 등록해줘 → 내 부품으로 다른 건 뭐 만들 수 있어?
→ 첫 번째 건 어떤 기체야? → 색상은 무시하면 얼마나 부족해?
```

## 역할과 작업 방식
개인 프로젝트이며, **AI 코딩 도구를 팀원처럼 쓰고 저는 PM 역할을 맡는 방식**으로 진행했습니다.

- **제가 한 일**: 요구사항 해석, 설계 결정(검색 구조·수치 검증 방식·평가 기준), 작업지시서 작성과 결과 검토, 평가셋 채점과 개선 방향 결정
- **AI 도구가 한 일**: 작업지시서에 따른 코드 구현(Claude Code·Codex)
- 작업지시서와 검토 기록은 `docs/pm/`에 남아 있습니다.

## 실행 방법 (Windows, Python 3.12)
> 이 공개본에는 수집 데이터와 벡터 색인을 넣지 않았습니다. 아래 순서대로 실행하면 직접 만들어집니다.
> LLM은 OpenAI 호환 API를 씁니다. 엔드포인트는 `config/settings.yaml`의 `llm.base_url`에서 바꿀 수 있습니다.

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env                      # GMS_KEY(OpenAI 호환 API 키), WIKI_CONTACT 입력
.venv\Scripts\python scripts\load_rebrickable.py      # Rebrickable CSV → data/lego.sqlite (원본이 있으면 --skip-download)
.venv\Scripts\python scripts\index_items.py           # RAG 1 색인 (set_meta)
.venv\Scripts\python scripts\index_docs.py            # RAG 2 사전 수집 문서 색인 (set_docs)
.venv\Scripts\streamlit run app_streamlit.py          # 필수 UI (Streamlit)
.venv\Scripts\uvicorn server.main:app --port 8000     # 심화 F2013 UI → http://127.0.0.1:8000
.venv\Scripts\python -m pytest -q                     # 테스트
```
- 키가 없으면 화면에 "API 키가 설정되지 않았습니다" 안내가 나오고 질문 입력이 막힙니다. 색인이 0건이면 색인 스크립트 실행을 안내합니다(F2012).
- 서버는 단일 워커로 실행합니다(처리 중 잠금이 프로세스 메모리 기준).

## 기능
### 필수 (F2001~F2012)
| ID | 구현 |
|---|---|
| F2001 | Rebrickable CSV 12종 → SQLite. 범위 세트 378건을 세트 1건 = 색인 텍스트 1개(이름·연도·테마·부품 수·미니피그·한국어 별칭)로 구성 |
| F2002 | `set_meta` 컬렉션, ID = 세트 번호(다시 색인해도 upsert, 중복 없음) |
| F2003 | 프롬프트1(P1 v2.1): 레고 전문가 역할로 위키 검색어 JSON 추출, 실패 시 세트 이름 기본값 |
| F2004 | 위키백과 검색 → 프롬프트2 후보 선별 → 본문·출처 수집, 이미 수집한 검색어는 재수집 안 함, 도메인 보호 장치 |
| F2005 | 청크 1000/200(설정), `set_docs` 컬렉션, ID = `{scope}:{doc_id}:{seq}` |
| F2006 | 선택 세트 메타 청크 항상 포함 + 외부 문서 MMR 검색, 병합·중복 제거, Top-k 5(설정) |
| F2007 | LangGraph retrieve→generate 2노드, 근거 없으면 "확인되지 않음", 출처 유형(set_meta·db·wikipedia) 구분 |
| F2008 | `[근거 N]` 인용, 실제 인용 근거만 답변 아래 "출처 보기"에 제목·원문 링크·라이선스 표시, 인용 없으면 안내 |
| F2009 | 한 대화 안에서 현재 대상 세트 지정·변경, **세트별 기록**(보조 패널·API)으로 항목별 대화 분리 |
| F2010 | 보유 세트 기반 부족 부품 분석·제작 추천(결정적 알고리즘) + 판본 비교, DB 근거 카드와 함께 제공 |
| F2011 | 자연어 조건 → 구조화 필터 → SQL → 의미 정렬 → LLM 검수 → 카드, 카드에서 바로 그 세트 질문 |
| F2012 | 단계별 진행 상태(분류·검색어 추출·문서 수집(제목)·검색·생성·검증), 색인 없음·키 누락·결과 없음 안내 |

### 심화
| ID | 내용 |
|---|---|
| **F2013** (선택) | FastAPI(REST + SSE) + Vanilla HTML/CSS/JS 화면. 요청·응답 형식은 Pydantic 모델과 `/docs`(OpenAPI)로 정의 |
| F2014 | MMR 검색(fetch_k 40, λ 0.7) — 유사도 검색 대비 Recall@5 0.65 → 0.875 (`docs/02` §6) |
| F2015 | RAG 사용 전후 비교 8문항 — 단독 모델 17점 vs 서비스 39점(48점 만점) (`docs/04_RAG전후비교.md`) |
| F2016 | Rebrickable DB 근거 + 위키백과(한·영) 결합, 근거마다 출처 유형 배지 |

### 대화 중심 UX (고도화)
- 세트를 고르지 않고 바로 질문, "그중·첫 번째·그거·색상은 무시하면" 같은 후속 표현을 이전 결과 기준으로 해석
- 대화로 보유 세트 등록·수정·삭제(같은 요청을 다시 보내도 한 번만 반영)
- 새로고침해도 대화·현재 대상·목록 문맥 복원

## 데이터 준비와 색인 절차
1. `scripts/load_rebrickable.py` — Rebrickable CSV 다운로드(하루 1회 이하) → `data/lego.sqlite`, 파생 테이블(최신 인벤토리·스페어 제외·미니피그 전개 부품 집계), 범위 세트 선정
2. `scripts/build_entities.py` — 세트명 → 위키 엔티티 매핑(`config/entity_rules.yaml`), 한국어 별칭
3. `scripts/index_items.py` — RAG 1 색인
4. `scripts/prefetch_docs.py` → `scripts/index_docs.py` — 엔티티·시리즈 문서 사전 수집과 RAG 2 색인
5. 질문할 때 부족한 문서는 질문 시점 수집(P1 → 위키 검색 → P2 → 수집·색인)으로 보강

## 데이터 · 문서 출처와 이용 조건 (NF2007)
| 출처 | 용도 | 이용 조건 |
|---|---|---|
| [Rebrickable](https://rebrickable.com/downloads/) CSV | 세트·부품·인벤토리 구조화 데이터 | Rebrickable 데이터 다운로드 페이지의 이용 안내에 따름, 출처 표기 |
| [위키백과](https://www.wikipedia.org/) (한국어·영어) | 기체·캐릭터·시리즈 서술 근거 | CC BY-SA 4.0, 문서별 원문 링크·라이선스를 근거 카드에 표시 |
| LEGO.com · LEGO 코리아 | 사용하지 않음 | 약관의 AI 목적 이용 금지 → 수집·색인하지 않고 링크만 안내 |
| Brickipedia (Fandom) | 사용하지 않음 | 약관 확인 전 보류 |

## 산출물
- [RAG 파이프라인 설계문서](docs/02_RAG파이프라인설계.md)
- [검증 결과](docs/03_검증결과.md)
- [RAG 전후 비교](docs/04_RAG전후비교.md)
- [심화 · 고도화 로드맵](docs/05_심화고도화로드맵.md)
- 개발 기록: `docs/pm/` (작업지시서 T-001~T-026, 설계 결정 ADR-001~018)

