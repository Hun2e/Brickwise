# T-017 MMR 구현·검색 실험 (F2014)

- 상태: 완료 (2026-10-22, 기본값 교체 ADR-017)
- 관련 요구사항: **F2014**(MMR, fetch_k 넉넉히 → 다양성 선별, 유사도 검색과 차이 확인), F2006(Top-k 설정), 설계문서 "선택 이유·문제·해결"
- 선행 작업: T-016(평가셋·`rank_docs`·스냅샷 m6-base), ADR-016(③ 비교 범위, ④ 교체 기준 +3%p·지연 20%)

## 목표
MMR을 설정으로 고를 수 있게 구현하고, 같은 평가셋·스냅샷에서 검색 방식을 비교해 기본 설정을 근거와 함께 확정한다.

## 실측 (PM, 2026-10-22 — 기준선 실패 분석)
| 항목 | 사실 | 의미 |
|---|---|---|
| 기준선 | 섹션 Recall@5 0.65 (26/40). 한국어 문서 0.54 · 영어 0.86 / entity 0.72 · series 0.45 | 개선 대상이 분명하다 |
| **결과 개수 부족** | 40문항 중 **14문항이 5개가 아니라 2개만** 받는다(분포: 2개 14 · 3개 3 · 4개 12 · 5개 11). 실패 14문항 중 6문항이 2개짜리 | 후보 20개(`top_k 5 × fetch_multiplier 4`)가 한 문서에 몰리면 "문서당 ≤2" 상한 뒤에 빈자리가 남는다. **상한 규칙이 후보 수에 비해 너무 빡빡하다** |
| **필터 문제 아님** | 실패 14문항 모두 정답 문서가 세트 필터(`sets $contains` 또는 series) 안에 있다 | 후보 선택 단계의 문제다 |
| 한국어 문서 탈락 | 예: RV-18 "보바 펫은 어떤 캐릭터야?" → 영어 문서(wp-en:50786) 청크 2개만 나온다. 정답인 한국어 보바 펫 문서는 후보 20개에도 없다 | **질의 뒤에 붙는 P1 영어 확장어가 영어 문서 쪽으로 끌어당긴다는 가설.** 검증이 필요하다(검토 포인트 ②) |
| MMR 코드 | `retrieval.search_type`·`mmr_fetch_k`·`mmr_lambda`는 settings에만 있고 읽는 코드가 없다 | 이번 작업에서 구현한다 |
| 지원 API | langchain-chroma 1.1.0 `max_marginal_relevance_search(query, k, fetch_k, lambda_mult, filter)`, `..._by_vector` 있음. 점수는 돌려주지 않는다 | 거리 점수가 필요하면 별도로 계산한다 |

## 범위
- 포함
  1. `rank_docs()`에 `search_type` 분기를 둔다.
     - `similarity`: 현재 방식(후보 → 상한)
     - `mmr`: Chroma MMR(`fetch_k`, `lambda_mult`, 같은 필터) → 같은 상한 규칙 적용 여부는 설정 `mmr_apply_caps`
     - 모든 값은 settings에서 읽는다.
  2. 후보 풀 크기를 설정으로 분리한다. `fetch_multiplier`를 대신하는 `similarity_fetch_k`를 두되, 기본값은 지금과 같게(20) 둔다.
  3. 질의 구성 방식을 설정으로 둔다(검토 포인트 ②에 따라).
  4. `scripts/eval_retrieval.py`에 **실험 행렬 실행**을 추가한다. `--grid experiments/t017.yaml` → 조합마다 결과 파일을 만들고, 기준선 대비 %p·문항 수 차이와 검색 지연(ms)을 적은 요약 표 1장을 낸다.
  5. **F2014 차이 확인 자료** `scripts/compare_mmr.py`: 대표 5문항에서 similarity와 MMR의 상위 5개(문서·섹션·거리)를 나란히 보여 주는 Markdown을 만든다(설계문서·화면 캡처용).
  6. 테스트: MMR 분기, 상한 적용 여부, 빈자리 처리, 설정 덮어쓰기, 행렬 실행(대역 저장소).
- 제외
  - 기본 설정 변경(실험 결과를 사용자와 함께 보고 결정한다, 결정 ⑤), BM25·하이브리드·재순위(로드맵 후속)
  - 답변 생성 품질 평가(T-018), 이월 결함 수정(T-019)

## 실험 행렬
| 묶음 | 변수 | 값 |
|---|---|---|
| E1 후보 풀 | similarity `fetch_k` (상한 유지) | 20(기준) · 40 · 80 |
| E2 MMR | `fetch_k` × `λ` (상한 적용/미적용) | 20·40 × 0.3·0.5·0.7 |
| E3 질의 구성 | 질문+영어 확장(기준) · 질문만 · 이중 질의(질문 / 질문+확장 각각 검색 후 합침) | E1·E2 승자 위에서 |
| E4 Top-k | `top_k` | 3 · 5 · 8 (Recall@k와 답변 입력 길이) |
| (E5 청크 크기) | — | **제외** (로드맵 후속, 결정 ④) |

## 인터페이스 계약
```python
# src/rag_service/rag/retrieve.py
def rank_docs(question, set_num, *, docs_store, settings, en_terms=None, meta=None,
              injected_cards=None, include_content=False) -> list[tuple[dict, float]]
# settings.retrieval:
#   search_type: similarity | mmr
#   similarity_fetch_k: 20          # 기존 top_k*fetch_multiplier와 같은 값으로 시작
#   mmr_fetch_k: 30, mmr_lambda: 0.5, mmr_apply_caps: true
#   query_mode: expanded | question | dual     # 검토 포인트 ②
# MMR 결과의 거리 점수는 질의 벡터와의 거리를 직접 계산해 채운다(근거 카드 score 일관성).
```

## 완료 기준 (DoD)
- [ ] `search_type: similarity`(기본)에서 기존 서비스 출력과 평가 결과가 기준선과 **완전히 같다**(회귀 없음, baseline 재실행으로 확인)
- [ ] `search_type: mmr`이 settings만으로 켜지고, 서비스(Streamlit·웹)에서도 같은 경로로 동작한다
- [ ] `eval_retrieval.py --grid`로 E1~E4를 한 번에 실행해 요약 표 1장(조합별 Recall@1·3·5, MRR, 문서 Recall, 반환 개수 분포, 검색 지연 중앙값, 기준선 대비 %p·문항 수)을 만든다
- [ ] `compare_mmr.py`로 대표 5문항의 similarity vs MMR 비교 Markdown을 만든다 (F2014 "차이 확인")
- [ ] P1 확장어는 T-016 고정 캐시(`eval/cache/en_terms.jsonl`)만 쓴다. 실험 중 LLM 호출은 0회, 임베딩은 질문 벡터만 쓴다(캐시)
- [ ] `.venv\Scripts\python -m pytest -q` 전체 통과 (현재 873)

## 검증 명령
```powershell
.venv\Scripts\python scripts\eval_retrieval.py --snapshot m6-base --name baseline-t017 --baseline eval\results\2026-10-22_baseline.json
.venv\Scripts\python scripts\eval_retrieval.py --snapshot m6-base --grid experiments\t017.yaml --baseline eval\results\2026-10-22_baseline.json
.venv\Scripts\python scripts\compare_mmr.py --snapshot m6-base --ids RV-18,RV-01,RV-05,RV-21,RV-34
.venv\Scripts\python -m pytest -q
```

## 검토 포인트 결정 (사용자, 2026-10-22)
1. **빈자리**: 별도 규칙을 만들지 않고 실험 변수로 다룬다. 해결 여부는 E1(후보 풀 확대)과 E2(MMR)로 확인한다. 요약 표에 **반환 개수 분포**(2·3·4·5개 문항 수)를 반드시 넣는다.
2. **질의 구성**: E3에 포함한다. 비교 조건은 세 가지다.
   - `expanded`: 현재 방식
   - `question`: 질문만
   - `dual`: 질문 검색과 질문+확장 검색을 각각 `fetch_k`만큼 한 뒤 청크 ID로 합친다. 거리는 더 작은 값을 쓰고, 그다음 상한·MMR을 적용한다.
   - 한국어/영어 문서 Recall을 따로 보고한다. LLM 호출은 없다.
3. **MMR과 상한**: 상한 적용·미적용 두 경우를 모두 실험한다.
4. **청크 크기 실험**: 이번에는 제외하고 로드맵 '후속'으로 둔다. 설계문서에는 현재 값(1000/200)의 근거와 "후보 풀·질의 구성이 더 큰 변수였다"는 실측을 적는다.
5. **기본값 반영**: 기준을 넘는 조합이 나와도 바로 바꾸지 않는다. **PM이 실험 표를 사용자에게 보여 주고 함께 결정한다**(권장안과 다른 사용자 선택). Codex는 settings 기본값을 바꾸지 않는다.
6. **승자 선정 규칙**: ADR-016 기준에 언어별 회귀 금지를 더한다.
   - 전체 섹션 Recall@5가 기준선보다 **+2문항 이상**이어야 한다.
   - 지연 증가는 20% 이내여야 한다.
   - 한국어 문서와 영어 문서 Recall@5가 **어느 쪽도 기준선보다 낮아지지 않아야** 한다.
   - 동률이면 더 단순한 설정을 고른다(similarity > MMR, 상한 적용 여부는 그다음).
   - 요약 표에 판정 열(통과/실패와 사유)을 자동으로 계산해 넣는다.

---

## 결과 보고 (Codex 작성)
- 변경 파일:
  - `src/rag_service/rag/retrieve.py`: similarity/MMR 분기, 후보 수 설정, expanded/question/dual 질의, 청크 ID 합집합·최소 거리, MMR 거리 계산, 서비스에서도 동일하게 적용되는 상한 선택.
  - `config/settings.yaml`: `similarity_fetch_k: 20`, `query_mode: expanded`, `mmr_apply_caps: true`만 추가. **기존 검색 설정 값은 모두 유지**했다. 기존 `fetch_multiplier` 값도 보존하되 검색 후보 수는 새 필드를 사용한다.
  - `src/rag_service/eval/common.py`, `retrieval.py`, 신규 `experiments.py`: 설정 검증, 읽기 전용 P1 캐시 사용, 질문 벡터 캐시, 반복 검색 측정, 단계별 행렬·승자 판정·요약 표.
  - `scripts/eval_retrieval.py`, 신규 `scripts/compare_mmr.py`, `experiments/t017.yaml`: E1~E4 실행 및 대표 문항 비교 CLI.
  - `tests/test_mmr.py`, `tests/test_eval.py`, `tests/test_retrieve_offline.py`: 실제 로컬 Chroma와 대역 저장소로 MMR·dual·상한·거리·캐시·설정·행렬·부분 결과·기존 서비스 회귀 검증.
  - `requirements.txt`: 이미 설치된 Chroma 전이 의존성 NumPy를 `numpy==2.5.4`로 직접 고정. float32 거리 계산과 Chroma MMR 함수 입력에 직접 사용하기 때문이다. 패키지 설치는 수행하지 않았다.
  - `eval/results/2026-10-22_baseline-t017.{json,md}`, `2026-10-22_t017-*.{json,md}`: 기준선, 조합별 결과, 요약, 대표 5문항 비교 등 총 50개 파일. 미측정 조합도 별도 파일에 사유를 남겼다.
  - 이 작업지시서의 결과 보고 섹션. 다른 문서·UI·AGENTS.md는 수정하지 않았고 git commit/push/branch 작업도 하지 않았다.
- 실행 명령과 결과:
  - `.venv\Scripts\python -m pytest -q`: 기반 인터프리터 프로세스 생성 불가로 실패. 대체 실행기 `C:\Users\SSAFY\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe` (Python 3.12.14)에 `PYTHONPATH=.venv/Lib/site-packages`, `PYTHON_DOTENV_DISABLED=1`, 빈 `GMS_KEY`를 설정해 아래 명령들을 실행했다. `.env` 열람·출력, LLM 호출, 임베딩 API 호출은 모두 없었다.
  - 대체 Python `scripts/eval_retrieval.py --snapshot m6-base --name baseline-t017 --baseline eval/results/2026-10-22_baseline.json`: 성공(40문항). `baseline_ranked_equal=true`로 **청크 순서·메타데이터·거리까지 완전히 일치**. Recall@5 **0.65(26/40)**, 문서 Recall@5 0.75, MRR 0.5583, 반환 개수 분포 **2개:14 / 3개:3 / 4개:12 / 5개:11**, 기준선 대비 모든 차이 0.
  - 대체 Python `scripts/eval_retrieval.py --snapshot m6-base --grid experiments/t017.yaml --baseline eval/results/2026-10-22_baseline.json`: **22개 조합 중 20개 측정, 2개 미측정**, 부분 결과 저장 후 종료 코드 1. `eval/results/2026-10-22_t017-grid.md`에 R@1·3·5·k, MRR, 문서 Recall, 언어별 Recall, 반환 개수, 지연, 입력 길이, 차이와 자동 판정을 기록했다. E3 question/dual은 질문 원문 벡터가 40개 모두 캐시에 없어서 미측정이다. 초기 실행에서 캐시 누락을 확인한 뒤, 완료 조합과 미측정 사유를 함께 저장하도록 보완하고 재실행했다.
  - 측정 예: E1 fetch=80은 **R@5 0.725(29/40), +7.5%p**, 반환 5개 문항 29개, 지연 중앙값 6.904ms(동일 실행 기준선 3.869ms 대비 +78.5%). E2 fetch=40/λ=0.7/상한 미적용은 **R@5 0.75(30/40), +10%p**, 40문항 모두 5개 반환, 한국어 0.6923·영어 0.8571, 지연 8.489ms(+119.4%). 측정된 개선 조합은 지연 증가 20% 조건을 통과하지 못했다. E3 미측정이 있으므로 최종 승자는 확정하지 않는다.
  - 대체 Python `scripts/compare_mmr.py --snapshot m6-base --ids RV-18,RV-01,RV-05,RV-21,RV-34`: 성공. `eval/results/2026-10-22_t017-compare-mmr.md` 생성. **5/5문항에서 선택 청크/순위가 달라짐**을 제목·문서 ID·섹션·청크 ID·거리로 확인했다. top_k=5라도 상한 뒤 실제 개수가 적으면 그대로 표시한다.
  - 대체 Python `-m pytest tests/test_mmr.py tests/test_eval.py tests/test_retrieve_offline.py -q --basetemp=.pytest-t017-focused2`: 당시 **89 passed**. 이후 캐시 누락 부분 결과와 명시적 임베딩 시 키 누락 테스트를 추가했다.
  - 대체 Python `-m pytest -q --basetemp=.pytest-t017-full`: 최종 **900 passed, 1 warning, 124.34초**. 경고는 기존 수집 CLI 테스트의 WIKI_CONTACT 누락 안내다.
  - `git diff --check`: 통과.
- 설계 판단과 이유:
  - 기본 similarity 경로는 기존 저장소 API·거리 동점 순서·중복/상한 순서를 보존했다. 기본 후보 20개를 독립 설정으로 옮겼으므로 E4에서 top_k를 바꿔도 후보 수가 함께 변하지 않는다.
  - 단일 질의 MMR은 Chroma `max_marginal_relevance_search_by_vector(k=top_k, fetch_k, lambda_mult, filter)`를 호출한다. 선택된 벡터를 ID로 읽어 컬렉션 거리 척도(L2 제곱 거리/cosine/IP)와 같은 단위의 score를 계산하며 문서는 재임베딩하지 않는다. 이후 상한·중복 제거로 생긴 빈자리는 채우지 않는다.
  - dual은 각 질의에서 fetch_k개를 읽어 ID별 합집합·최소 거리로 정렬한 뒤 선택한다. dual MMR의 관련도 기준은 원래 질문 벡터이며, 합집합에 Chroma와 같은 MMR 함수를 적용한다. 반환 순서는 Chroma API처럼 선택 집합의 후보 거리 순서를 유지한다. 이 선택을 요약 표에도 명시했다.
  - E1/E2의 조건 통과 승자 위에서 E3, 그 승자 위에서 E4를 실행한다. 통과 조합이 없으면 기준선을 유지한다. 동률은 similarity, 상한 적용, 단순 질의/작은 후보 수 순으로 정한다. 미측정 조합은 실패한 품질 점수로 만들지 않고 미측정으로 표시하며 후속 단계 부모 조합도 JSON에 남긴다.
  - 기존 기준선에는 검색 지연이 없어 같은 grid 실행에서 기준선을 재측정했다. 문항별 워밍업 1회 후 설정의 repetitions=3회 중앙값, 전체 문항 중앙값을 사용한다. P1과 질문 임베딩 시간을 제외한 `rank_docs` 지연이다. 입력 길이는 외부 근거 본문 문자 수이며 전체 생성 프롬프트 토큰 수가 아니다.
  - 평가/비교 CLI는 기본적으로 질문 벡터 캐시만 읽으므로 키 없이 재현 가능하다. P1 캐시가 없으면 생성하지 않고 실패한다. PM이 `--embed-missing`을 명시한 경우에만 누락 질문 벡터를 임베딩하고 기존 SQLite 캐시에 저장한다(LLM 생성 호출은 여전히 0회). 대역 행렬에서는 E1~E4 전체 경로 및 캐시 누락 경로를 모두 검증했다.
- 남은 이슈 / PM 확인 필요:
  - **E3 실측 2개 조합과 최종 승자 확정은 남아 있다.** AGENTS.md의 실제 API 결과 PM 확인 규칙에 따라 Codex는 누락 질문 벡터 40개를 API로 생성하지 않았다. 키가 있는 환경에서 `.venv\Scripts\python scripts\eval_retrieval.py --snapshot m6-base --grid experiments\t017.yaml --name t017-pm --baseline eval\results\2026-10-22_baseline.json --embed-missing`으로 전체 행렬을 다시 실행하면 된다. 새 접두사는 기존 결과 덮어쓰기를 피한다. 질문 벡터만 캐시에 추가되며 스냅샷·P1 캐시는 변경하지 않는다.
  - PM 환경의 `.venv`로 전체 테스트와 검색 지연을 재검증해야 한다. 현재 측정은 대체 Python/로컬 검색/워밍업 캐시 조건이며 종단 간 응답 시간과 다르다. E3 완료 전 질의 구성 가설이나 최종 기본값을 확정할 수 없다.
  - 기존 설정 값은 변경하지 않았다. 전체 결과 확인 후 기본값 반영 여부는 사용자와 PM이 결정한다.

### 피드백 1차 결과
- 변경 파일:
  - `tests/retrieval_helpers.py`: 이전 동작 검증용 공통 헬퍼에 `search_type=similarity`, `query_mode=expanded`, `mmr_apply_caps=true`, `similarity_fetch_k=20`을 명시했다.
  - `tests/test_retrieve_offline.py`, `tests/test_eval.py`: 기존 fixture에서 공통 헬퍼를 사용한다. 이를 가져오는 `test_graph_offline`·`test_mmr`에도 동일한 조건이 적용되며 기존 기대값은 변경하지 않았다.
  - `tests/test_orchestrator_offline.py`: similarity 대역으로 502 재시도를 검증하는 테스트에만 이전 설정을 명시했다.
  - `tests/test_retrieval_defaults.py`: `load_settings()`의 검색 기본값을 그대로 사용하는 테스트 4개를 추가했다. `rank_docs`, `retrieve`, 실제 그래프 `ask`, 오케스트레이터 narrative 경로에서 질문 원문 임베딩과 MMR 호출(`k=5`, `fetch_k=40`, `lambda_mult=0.7`, 세트/series 필터), 거리 계산, 상한 미적용, 최종 인용을 검증한다.
  - 위 테스트 파일들과 이 결과 보고만 수정했다. 기존 PM 변경 사항은 보존했고 `src/`, `config/`, 의존성 및 기존 테스트 기대값은 변경하지 않았다. git commit/push/branch 작업은 하지 않았다.
- 실행 명령과 결과:
  - `.venv/Scripts/python -m pytest -q --basetemp=tests/.tmp-t017-feedback-venv -p no:cacheprovider`: 기반 인터프리터 프로세스 생성 불가로 실행 실패(기존 샌드박스 제약).
  - 대체 실행기 `C:/Users/SSAFY/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe`에 `PYTHONPATH=.venv/Lib/site-packages;src`, `PYTHON_DOTENV_DISABLED=1`, 빈 `GMS_KEY`, `PYTHONDONTWRITEBYTECODE=1`을 설정하여 검증했다. `.env` 열람·출력이나 실제 LLM/임베딩 API 호출은 하지 않았다.
  - 대체 Python `-m pytest tests/test_retrieve_offline.py tests/test_graph_offline.py tests/test_mmr.py tests/test_orchestrator_offline.py tests/test_eval.py tests/test_retrieval_defaults.py -q --basetemp=tests/.tmp-t017-feedback-focused -p no:cacheprovider`: **215 passed, 37.00초**.
  - 대체 Python `-m pytest -q --basetemp=tests/.tmp-t017-feedback-full -p no:cacheprovider`: **904 passed, 1 warning, 197.22초**. 기존 900개와 신규 4개가 모두 통과했다. 경고는 기존 수집 CLI 테스트의 WIKI_CONTACT 누락 안내다.
  - `git diff --check`: 통과.
- 설계 판단과 이유:
  - 이전 동작의 기대값을 유지하면서 테스트 입력 설정을 고정해 운영 기본값과 분리했다. `test_mmr`·`test_eval` 실패도 설정 기본값 의존으로 확인되어 서비스 코드 수정은 필요하지 않았다.
  - 새 기본값 테스트는 이전 설정 헬퍼를 사용하지 않는다. MMR API와 질문 임베딩만 허용하는 대역 저장소로 잘못된 similarity 경로를 감지하고, 같은 문서이자 series인 서로 다른 청크 5개로 문서별·유형별 상한이 적용되지 않는지 확인한다. 저장 벡터는 역순으로 반환하여 ID 기준 거리 대응도 확인한다.
  - 오케스트레이터 테스트는 `ask_fn`을 대체하지 않고 실제 retrieve→generate 그래프를 통과하며 모델과 저장소만 대체한다.
- 남은 이슈 / PM 확인 필요:
  - 발견된 서비스 결함은 없다. PM 환경의 `.venv`로 전체 테스트를 재검증하면 된다.
  - 이번 범위는 테스트 보완이므로 실측 평가 파일과 운영 기본값은 변경하지 않았고 검색 실험·외부 API 실측은 재실행하지 않았다.

## PM 리뷰
- 판정:
- 메모:
- 판정: **승인** (구현·실험 행렬). 기본값은 사용자와 결정한다(결정 ⑤).
- `.venv` 재검증
  - `pytest -q` **900 passed**
  - 기본 경로 `baseline_ranked_equal=true`(회귀 없음)
  - `numpy==2.5.4` 고정은 설치된 버전과 같다
- PM 실측
  - E3 미측정 2조합은 `--embed-missing`으로 질문 벡터 40개를 채워 전체 행렬(`t017-pm-*`)을 다시 돌렸다.
  - Codex 단계별 탐색에서 빠진 조합 4개를 추가로 측정했다(`x-*`).
- 핵심 결과 (섹션 R@5, 기준선 0.65 = 26/40)

| 조합 | R@5 (적중) | 한국어 문서 | 영어 문서 | MRR | 비고 |
|---|---|---|---|---|---|
| 기준선 similarity·20·상한·expanded | 0.650 (26) | 0.538 | 0.857 | 0.558 | 14문항이 2개만 받음 |
| e1-03 similarity·fetch 80 | 0.725 (29) | 0.654 | 0.857 | 0.578 | 회귀 없음 |
| e3-02 similarity·question | 0.775 (31) | 0.846 | 0.643 | 0.717 | 영어 −3문항 |
| e2-12 MMR 40·λ0.7·상한 없음·expanded | 0.750 (30) | 0.692 | 0.857 | 0.598 | 언어 회귀 없음 |
| **x MMR 40·λ0.7·상한 없음·question** | **0.875 (35)** | **0.923** | 0.786 | **0.743** | 영어 −1문항(RV-35) |
| x MMR 40·λ0.7·상한 없음·dual | 0.875 (35) | 0.923 | 0.786 | 0.690 | 질의 임베딩 2회 |
| x MMR 80·λ0.7·상한 없음·expanded | 0.775 (31) | 0.731 | 0.857 | 0.603 | |
| x similarity 80·dual | 0.700 (28) | 0.654 | 0.786 | 0.583 | |

- 해석
  1. **가설 확인**: 영어 확장어가 한국어 문서를 밀어낸다. 질문만으로 검색하면 한국어 문서가 0.54 → 0.85~0.92로 오른다. 반대로 한국어 질문 + 영어 정답 문서(RV-35 "레아도 제다이 훈련을 받았어?")는 확장어가 있어야 찾는다.
  2. **빈자리 해소**: MMR(상한 없음)은 40문항 모두 5개를 채운다. 기존 "문서당 ≤2" 상한이 MMR에서는 오히려 손해다.
  3. **지연 기준 해석 문제**: 판정의 "지연 +20%"는 `rank_docs` 단독 ms 기준이다(3.9 → 8.5ms, +4.6ms). 종단 응답(warm 5~15초, cold 15~46초)에 비하면 0.1% 미만이다. ADR-016 ④의 원래 의도(사용 체감 지연)로 해석할지는 사용자 결정 사항이다.
  4. **과적합 주의**: 약 26개 설정을 40문항으로 고른 결과다. 단, +9문항(22.5%p)은 1문항 단위의 잡음보다 훨씬 크다.

## PM 피드백 1차 (2026-10-22, 기본값 교체 후속)
- 배경: 사용자 결정(ADR-017)에 따라 PM이 `config/settings.yaml` 기본값을 `search_type: mmr`, `mmr_fetch_k: 40`, `mmr_lambda: 0.7`, `mmr_apply_caps: false`, `query_mode: question`으로 바꿨다. 평가 재현(35/40)과 실제 답변 3건은 확인했다.
- 문제: `.venv` 전체 테스트에서 **29 failed / 871 passed**.
  - 실패 위치: test_retrieve_offline 11 · test_graph_offline 11 · test_mmr 4 · test_orchestrator_offline 2 · test_eval 1
  - 원인: 테스트 대역 저장소가 `similarity_search_with_score`만 구현했거나, 기대값이 similarity·expanded 기본값을 전제로 한다(`TypeError: 'Mock' object is not iterable`, retrieve.py:110). T-012와 같은 "settings 기본값 의존" 문제다.
- 수정 지시
  1. 기존 동작(similarity·expanded·상한)을 검증하는 테스트는 **검색 설정을 명시한다**. 공통 fixture나 헬퍼로 `search_type=similarity`, `query_mode=expanded`, `mmr_apply_caps=true`, `similarity_fetch_k=20`을 넣는다. 테스트의 기대값 자체는 바꾸지 않는다.
  2. 새 기본값을 검증하는 테스트를 추가한다.
     - `load_settings()` 기본값으로 `rank_docs`/`retrieve`가 MMR·질문 원문 경로를 타는지 확인한다(MMR API를 구현한 대역 저장소).
     - 그래프·오케스트레이터 1개 경로가 새 기본값으로 동작하는지 확인한다.
  3. test_mmr·test_eval의 실패는 원인을 확인해 같은 방식(설정 명시)으로 고친다. 기본값 의존이 아닌 실제 결함이면 고치지 말고 결과 보고에 적는다.
- 범위: `tests/` 아래 파일만 수정한다. `src/`, `config/`는 수정하지 않는다(기본값은 PM이 확정했다).

## PM 리뷰 (최종)
- 판정: **승인**. 피드백 1차 반영 후 `.venv` `pytest -q` **904 passed**.
- 기본값 교체(ADR-017): MMR 40·λ0.7·상한 없음·질문 원문 질의
  - `default-adr017` 평가: **35/40 재현**
  - 실제 답변 3건(보바 펫·레드 파이브·팰퍼틴): 한국어 문서 근거로 정답
