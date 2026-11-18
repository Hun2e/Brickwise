# 평가 데이터와 실행

T-016은 평가 도구와 초안 생성기다. 최종 문항은 PM이 검수한다. 기존 `route_questions.jsonl`, `p3_questions.jsonl`, `spike_questions.jsonl`은 보존한다.

## 문항 형식

UTF-8 JSONL, 한 줄에 객체 하나. `id`는 파일 안에서 고유하며 `question`은 빈 문자열일 수 없다. 알 수 없는 추가 필드는 검수 메모·기존 평가와의 호환을 위해 보존한다.

| 파일 | 필수 필드 | 제약 |
| --- | --- | --- |
| retrieval.jsonl | id, question, set_num, lang, relevant, kind, source | lang=ko/en; kind=entity/series/set; source=draft-llm/pm; relevant는 1개 이상 |
| routing.jsonl | id, question, selected_set, history, has_collection, expected_route | selected_set은 문자열 또는 null; history는 객체 배열; has_collection은 bool |
| rag_compare.jsonl | id, question, set_num, type, facts | type=fact/compare/narrative/unanswerable; facts는 field/value 객체 배열 |

`relevant` 원소는 `{"doc_id":"실제 색인 ID","section_path":"실제 섹션 경로"}`다. 정답 섹션 경로가 청크 경로의 접두어이면 적중한다. 빈 경로는 문서 전체를 뜻한다. 문서 ID에 언어가 포함되면 다른 언어 문서와 별도 정답이다. 예시 이름을 추측하지 말고 스냅샷에서 복사한다.

라우팅 경로는 `fact`, `filter`, `set_ops`, `narrative`, `compare`, `build`, `build_archive`다. 선택 필드 `tags`는 문자열 배열(기본 `[]`), `note`는 검수 메모다. 기존 `route_questions.jsonl`의 `collection_sets`와 `expected_rule`도 보존한다. 지연 측정은 보유 세트 문항의 `collection_sets: [["세트 번호", 수량]]`을 사용하고, build·build_archive 또는 `has_collection=true` 문항에 값이 없으면 `evaluation.latency_collection`으로 채운다. 컬렉션은 임시 user DB에 문항별로 저장하고 종료 시 삭제한다. 운영 `user.sqlite`는 열거나 수정하지 않는다. `facts`는 PM이 DB 값으로 채운다. 형식 검증은 사실의 정확성을 보증하지 않는다.

검색 40문항은 생성된 60개 초안에서 PM이 골라 `retrieval.jsonl`에 저장한다. `routing.jsonl` 40문항 및 `rag_compare.jsonl` 8문항은 PM이 작성한다. 도구가 이 파일들을 임의로 생성하거나 덮어쓰지 않는다. Python의 `rag_service.eval.schema.read_cases(path, kind)`로 세 형식을 검증할 수 있다.

## 스냅샷과 재현성

앱과 색인 작업을 정지한 뒤 실행한다. 원본 디렉터리를 복사하고 복사 중 변경이 감지되면 실패한다. 동일한 스냅샷 이름을 덮어쓰지 않는다. 실패한 복사본에는 유효한 매니페스트가 없으므로 평가할 수 없다.

```powershell
.venv\Scripts\python scripts\eval_snapshot.py --name m6-base
.venv\Scripts\python scripts\eval_draft_retrieval.py --snapshot m6-base --n 60
# PM이 초안을 검수하여 eval/retrieval.jsonl 40문항을 저장한 다음 실행
.venv\Scripts\python scripts\eval_retrieval.py --snapshot m6-base --name baseline
.venv\Scripts\python scripts\eval_retrieval.py --snapshot m6-base --name repeat
.venv\Scripts\python scripts\eval_retrieval.py --snapshot m6-base --name top8 --override retrieval.top_k=8 --baseline eval/results/YYYY-MM-DD_baseline.json
.venv\Scripts\python scripts\eval_routing.py --file eval/route_questions.jsonl --validate-only
.venv\Scripts\python scripts\eval_routing.py --file eval/route_questions.jsonl --name route-v2-legacy
# 지연 측정 기본값: 운영 색인·질문 시점 수집을 포함하는 live
.venv\Scripts\python scripts\eval_latency.py --reps 3 --name latency-base
.venv\Scripts\python scripts\eval_latency.py --file eval/route_questions.jsonl --condition cold --override llm.cache_enabled=false --name latency-cold
# 수집 없는 고정 색인 지연은 별도 모드로 명시
.venv\Scripts\python scripts\eval_latency.py --snapshot m6-base --reps 3 --name latency-snapshot
```

스냅샷은 `data/eval_snapshot/<이름>/chroma/`와 `manifest.json`이다(git 제외). 매니페스트에 컬렉션별 청크 수·문서 수·`chunk_config_hash` 목록, 본문·메타·임베딩 내용 해시, 설정 및 설정 해시, UTC 생성 시각을 남긴다. 평가 전에 내용 해시를 검증한다. 검색 품질 평가와 `--snapshot` 지연 측정에서는 스냅샷 컬렉션만 열고 `collect_fn=None`으로 수집을 연결하지 않는다. 기본 live 지연 측정은 운영 색인에 수집·색인을 수행할 수 있다. DB가 필요한 라우팅·지연 측정은 세트 DB를 읽기 전용으로 연다.

초안은 한국어 45개·영어 15개를 해당 언어 문서에서 생성하며 문서 종류를 균등 순환 추출한다. 한 종류의 후보가 부족하면 남은 종류에서 채운다. 스냅샷에 없는 종류(현재 색인의 세트 전용 문서 등)는 보고서 `unavailable_kinds`에 남긴다. 없는 근거를 만들어 세 종류를 맞추지 않는다. 같은 문서+섹션을 반복 추출하지 않으며 seed를 고정한다. 원문 청크를 순서대로 합친 `section_text`, 출처·라이선스, 청크 ID, `longest_common_substring_ratio`를 보존한다. 비율은 공백·대소문자 정규화 후 최장 공통 부분 문자열 길이/질문 길이다. 높은 비율은 PM 검수 참고값이며 자동 탈락 기준이 아니다.

P1 확장어는 `eval/cache/en_terms.jsonl`에 문항 ID+질문 해시+P1 버전으로 한 번 저장한다. 세트·제목·모델 문맥이 달라진 캐시는 재사용을 거절한다. 캐시 생성 실패는 기록하지 않는다. 설정 실험은 동일한 캐시를 재사용하며 캐시와 평가 결과는 git 관리 대상이다. 다른 P1 실험은 `--override evaluation.en_terms_cache=...`로 분리한다. 실제 키 호출 검증은 PM이 수행한다.

## 채점 및 결과 해석

결과는 `eval/results/<UTC 날짜>_<실험명>.json`과 `.md`다. 같은 이름은 덮어쓰지 않는다. JSON에는 원문항·검색 순위와 거리·문항별 점수·분할 요약·설정·스냅샷을 보존한다. 생성 시각과 실험명은 달라도 고정 스냅샷/설정/질문/P1/임베딩 캐시의 검색 `rows`와 `summary`는 같아야 한다. Chroma 동점 순서는 기존 서비스 정책을 유지한다.

- 주 지표: 섹션 Recall@5. 문항마다 회수한 고유 정답 라벨 수/전체 고유 정답 라벨 수를 구한 뒤 매크로 평균한다. 중복 청크나 중복 정답은 점수를 부풀리지 않는다.
- 보조 지표: 섹션 Recall@1·3, MRR(최종 선택된 순위의 첫 적중 역수), 문서 Recall@5. 문서 지표의 분모는 고유 doc_id 수다. top_k가 5 미만이면 반환된 청크까지만 채점한다.
- 언어(`lang`) 및 문서 종류(`kind`)별 평균·문항 수를 기록한다. 하나 이상 적중한 문항 수와 모든 정답을 회수한 문항 수도 기록한다.
- `--baseline`은 문항·정답·스냅샷·확장어가 같을 때만 비교하며, 주 지표 %p 차이·적중 문항 수 차이·개선/악화 문항 수를 기록한다. 40개 단일 정답 문항에서는 1문항=2.5%p다. 여러 정답 라벨이 있는 문항에서는 점수 변화와 적중 문항 수 변화가 다를 수 있다.
- 라우팅은 실제 Router 프롬프트와 후처리 규칙까지 채점한다. 답변 생성은 실행하지 않는다. 구조화 호출 실패는 `error` 열의 오답으로 기록한다. 혼동 행렬 행은 정답, 열은 예측이다.
- 지연은 `--file`(기본 `evaluation.routing_file`)에서 경로별 첫 문항을 골라 warm/cold 각각 3회 측정한다. warm 준비 호출 1회는 통계에서 제외한다. p90은 선형 보간이다. 실패 또는 다른 경로 응답은 원자료에 남기고 성공 지연 요약에서 제외한다. 준비 호출을 포함해 warm 이후 cold를 실행하므로 두 조건은 이전 호출이 남긴 문서·수집 캐시를 공유한다.
- 지연 결과 JSON/Markdown에 `live` 또는 `snapshot` 모드를 기록한다. live는 `orchestrator.answer()`와 같은 운영 저장소·수집 팩토리를 사용하며 질문 시점 P1·위키 검색·P2가 실행되면 그 시간을 포함한다. `--snapshot`은 수집을 연결하지 않아 P1/P2·외부 위키 수집 시간이 제외된다.
- cold는 `llm.cache_enabled=false`로 라우터·선택·탐색·P1의 LLM 캐시 읽기·쓰기를 우회한다. 수집 매니페스트·문서·임베딩 캐시는 유지하여 완료된 같은 검색어를 재요청하지 않는다. P1이 새 검색어를 만들거나 이전 수집이 실패했다면 위키 요청이 발생할 수 있다. warm은 기존 캐시를 사용한다.
- 각 표본의 `timings`에 routing·retrieve·generate 등의 시간을 보존하고 단계별 중앙값·p90·표본 수를 낸다. 없는 단계는 0으로 채우지 않는다. `collection`에는 수집 호출 수(`calls`), 완료된 신규 검색 수(`searched`), 건너뛴 검색어 수(`skipped_queries`), 수집 예외 수(`failures`), 부분 완료 수(`partial`)를 기록한다. 수집 합계는 실패 표본과 재시도를 포함하되 준비 호출은 제외한다. 수집 함수가 예외로 끝나 결과가 없으면 검색 수를 추측하지 않고 실패 수를 기록한다.

모든 튜닝값·기본 경로는 `config/settings.yaml`의 `evaluation` 등에 둔다. `--settings`와 반복 가능한 `--override key=value`를 지원하며 미등록 키·잘못된 타입을 거절한다. MMR 구현은 T-017 범위이므로 현재 `search_type=mmr`는 오류로 처리한다. 키가 필요한 명령은 키가 없으면 한 줄 안내하고 종료한다.
