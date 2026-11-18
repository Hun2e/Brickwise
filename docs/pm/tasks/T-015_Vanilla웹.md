# T-015 Vanilla HTML/CSS/JS 웹 화면 (F2013 화면)

- 상태: 완료 (2026-10-22)
- 관련 요구사항: F2013(HTML/CSS/JS 화면, 질문·답변·근거 표시), F2008, F2009, F2011, F2012, NF2004
- 선행 작업: T-014(API 계약), ADR-015(화면 구성 ①~⑦)

## 목표
프레임워크 없이 HTML/CSS/JS만으로 T-014 API를 쓰는 웹 화면을 만든다. Streamlit과 같은 기능(탭 3개·세트 선택·진행 단계·답변·인용 근거·대화 기록·컬렉션 등록·탐색 카드 → 대화 이동)을 제공해 두 UI를 비교할 수 있게 한다.

## 실측 (PM, 2026-10-22)
| 항목 | 현재 상태 | 의미 |
|---|---|---|
| API | REST 9 + `POST /api/chat` SSE. 응답 모델 `TurnOut{answer: AnswerOut{answer, cards, tables, set_nums, needs_input, warnings, timings, route}}` | 화면은 이 계약만 쓴다 |
| SSE 이벤트 | `stage{stage, detail, index}`* → `result(TurnOut)` 또는 `error(ErrorOut)`. `분류`가 연속 2번 온다(T-014 남은 이슈 1) | 클라이언트가 연속 중복을 무시한다 |
| 오류 | 4xx는 JSON `ErrorOut{code, message}`. 409 `busy` | 메시지를 그대로 안내 문구로 쓴다 |
| 답변 본문 | `**굵게**`, `- 목록`, 줄바꿈, `[근거 N]`이 섞인 마크다운 형식 텍스트 | 최소 렌더러로 DOM 생성(결정 ①) |
| 세트 이미지 | `img_url` = `cdn.rebrickable.com` 이미지 | 표시하지 않는다(결정 ④) |
| 도구 | Node 24.21 설치됨, Playwright 없음 | 순수 JS 로직은 `node --test`로 의존성 없이 테스트할 수 있다 |
| 정적 제공 | FastAPI가 `web/`을 `/`에 마운트. CORS 꺼짐 | 상대 경로 `/api/...`로 호출, 빌드 도구 없음 |

## 범위
- 포함
  1. `web/index.html`: 화면 골격(헤더·사이드바·탭·대화 영역·근거 패널), 접근성 속성(`role="tab"`, `aria-selected`, `aria-live`)
  2. `web/css/app.css`: 레이아웃·라이트/다크 테마·좁은 화면 대응
  3. `web/js/` ES 모듈 (빌드 없음, `<script type="module">`)
     - `api.js`: fetch 래퍼(`X-User-Id` 헤더, `ErrorOut` 처리), SSE 스트림 읽기(`fetch` + `ReadableStream` + 이벤트 파서)
     - `state.js`: uid(localStorage)·선택 세트·활성 탭
     - `render.js`: 답변 렌더링, `[근거 N]` 링크, 근거 카드, 표, 경고, 처리 정보
     - `sets.js`: 세트 검색(번호·영문명·별칭 전체), 결과 목록
     - `app.js`: 화면 조립과 이벤트 연결
  4. `web/tests/*.test.mjs`: 순수 함수 테스트 (`node --test`, 의존성 없음)
  5. `tests/test_web_static.py`: pytest에서 정적 파일 제공·금지 패턴 검사, Node가 있으면 `node --test`를 실행하고 없으면 skip
- 제외
  - 서버·서비스 변경 (필요하면 남은 이슈에 적는다)
  - npm 패키지·번들러·프레임워크·CDN 라이브러리 (Vanilla 원칙)
  - 토큰 단위 스트리밍, 로그인

## 화면 구성 (ADR-015 기준)
- **헤더**: 서비스 이름, 비공식 팬 프로젝트 문구, 상태 배지(문제 있으면 안내 문구 펼침)
- **사이드바**: 세트 검색 입력 → 결과 목록(라벨·연도·부품 수, 비교용 표시) → 선택. 내 대화 목록(`/api/threads`)에서 다른 세트로 바로 이동. "이 대화 지우기"
- **탭**: 아카이브(선택 세트 대화) / 내 컬렉션(보유 세트 표 편집 + 질문) / 탐색(조건 질문 → 결과 카드 + "이 세트로 대화")
- **대화 영역**: 질문 → 진행 단계(실제로 지나간 단계만, 수집 문서 제목) → 답변 → 경고 한 줄씩 → 접힌 처리 정보(경로·소요 시간)
- **근거 패널**: 인용 카드만. 번호·출처 유형 배지·제목·원문 링크(새 탭, `rel="noopener noreferrer"`)·라이선스·발췌. 인용이 없으면 "답변에 인용된 근거가 없습니다"
- **상태별 안내**: `can_chat=false`면 입력 비활성 + 이유. 409이면 "이전 질문을 처리 중입니다". 네트워크 오류는 원인·조치 한 줄

## 인터페이스 계약 (JS 순수 함수 — 테스트 대상)
```js
// api.js
export function parseSSE(chunkText, buffer) -> { events: [{event, data}], buffer }  // 청크 경계·주석(:keep-alive)·CRLF 처리
export async function streamChat(body, { onStage, onResult, onError, signal })    // 마지막 이벤트 1개 보장
// render.js
export function renderAnswer(text, { onCite }) -> DocumentFragment   // 결정 ①②: DOM 생성만, innerHTML 금지
export function citedNumbers(text) -> number[]           // [근거 N], [근거 1][근거 2], [근거 1, 3] 형식
export function dedupeStages(stages) -> stages           // 연속 중복 제거
// sets.js
export function searchSets(sets, query, limit) -> SetOut[]   // 번호·영문명·별칭, 대소문자·공백 무시, core 먼저
```

## 완료 기준 (DoD)
- [ ] `uvicorn server.main:app` → 브라우저 `/`에서 세 탭이 동작한다. 질문 → 진행 단계 → 답변·인용 근거가 보인다 (F2013, F2012, F2008)
- [ ] 새로고침해도 uid·선택 세트·활성 탭이 유지되고, 대화 기록이 서버에서 다시 그려진다 (F2009)
- [ ] 탐색 결과 카드의 "이 세트로 대화"를 누르면 선택 세트가 바뀌고 아카이브 탭으로 이동한다 (F2011)
- [ ] 답변·근거 본문에 `<script>`, `<img onerror>` 같은 문자열이 있어도 실행되지 않는다 (테스트로 확인). JS 어디에도 서버 문자열을 `innerHTML`에 넣지 않는다 (정적 검사)
- [ ] 키 없음·409·네트워크 오류·0건 결과 안내가 보이고, 스택트레이스나 원시 JSON은 화면에 나오지 않는다 (NF2004)
- [ ] 360px 폭에서 가로 스크롤 없이 쓸 수 있다 (근거 패널은 답변 아래로 이동)
- [ ] `node --test web/tests` 통과, `.venv\Scripts\python -m pytest -q` 전체 통과 (현재 818)
- [ ] 외부 CDN·npm 의존 없음 (정적 검사로 `http` 스크립트·스타일 로드가 없음을 확인)

## 검증 명령
```powershell
node --test web/tests/*.test.mjs   # Node 24는 디렉터리 인수를 받지 않는다 (결과 보고 2)
.venv\Scripts\python -m pytest -q
.venv\Scripts\uvicorn server.main:app --port 8000   # PM이 브라우저로 대표 질문·스크린샷
```

## 검토 포인트 결정 (사용자, 2026-10-22 — 7건 모두 권장안)
1. **본문 렌더링**: 직접 만든 최소 렌더러를 쓴다. 지원 범위는 `**굵게**`, `- `·`* `·`1. ` 목록, 빈 줄 문단, 줄바꿈, `[근거 N]`(쉼표 묶음 `[근거 1, 3]` 포함)이다. 그 밖의 문법은 글자 그대로 둔다. `textContent`와 `createElement`만 쓰고 HTML 해석은 하지 않는다.
2. **[근거 N]**: 버튼 요소로 만든다. 누르면 근거 패널을 그 답변으로 바꾸고, 해당 카드를 펼쳐 스크롤·강조한다. 카드가 없는 번호(인용 필터로 빠진 번호)는 강조만 하고 동작하지 않는다.
3. **레이아웃**: 넓은 화면(≥1100px)은 사이드바 | 대화 | 근거 패널 3단이다. 중간 폭은 근거 패널을 대화 오른쪽 접이식 영역으로 둔다. 좁은 화면(<768px)은 1단으로, 사이드바는 버튼으로 열고 근거는 각 답변 아래에 둔다. 폭 기준값은 CSS 변수로 한곳에 둔다.
4. **세트 이미지**: 표시하지 않는다. 텍스트(번호·이름·연도·부품 수·별칭)만 쓴다.
5. **근거 패널 대상**: 최신 답변이 기본이다. 다른 답변이나 그 안의 `[근거 N]`을 누르면 그 답변으로 전환하고, 선택된 답변은 테두리로 표시한다. 패널 머리에 "N번째 질문의 근거"를 쓴다.
6. **테스트**: 순수 함수는 `node --test web/tests`로, 화면 흐름은 PM이 인앱 브라우저와 실제 키로 확인한다. 새 의존성은 없다. pytest의 `test_web_static.py`는 Node가 없으면 그 부분만 skip한다.
7. **디자인**: 깔끔한 기본형이다. 중립 배경에 포인트 색 1개(빨강 계열)를 쓰고, `prefers-color-scheme`로 라이트/다크를 자동 전환한다. 색은 CSS 변수 토큰으로 둔다. LEGO 로고·상표 모양·공식 폰트는 쓰지 않는다. 시스템 폰트를 쓴다.

---

## 결과 보고 (Codex 작성)
- 변경 파일:
  - `web/index.html`: 헤더·상태 안내·세트/대화 사이드바·세 탭·컬렉션 편집·질문 입력·근거 패널과 접근성 속성.
  - `web/css/app.css`: 중립 배경/빨강 포인트, 자동 라이트·다크 테마, 3단/접이식 근거/모바일 답변별 근거 배치.
  - `web/js/api.js`, `state.js`, `render.js`, `sets.js`, `app.js`: 상대 경로 API, uid 헤더/저장, SSE 파싱, 안전한 최소 마크다운, 세트 검색, 화면 연결.
  - `web/tests/api.test.mjs`, `render.test.mjs`, `state-sets.test.mjs`, `app.test.mjs`: 의존성 없는 Node 테스트 32개. 화면 연결은 작은 DOM/API 대역으로 검증.
  - `tests/test_web_static.py`: 정적 파일 HTTP 제공·접근성 골격·금지 패턴/외부 의존 검사·Node 테스트 호출, Node 미설치 시 해당 검사만 skip.
  - 이 작업지시서의 결과 보고 섹션. 서버·서비스·다른 문서는 수정하지 않았으며 의존성 추가 및 git commit/push/branch 작업 없음.
- 실행 명령과 결과:
  - `node --test web/tests`: **실패**. 설치된 Node v24.21.0이 디렉터리 인수를 모듈로 취급하여 `MODULE_NOT_FOUND` 발생.
  - 대체 명령 `$webTests = Get-ChildItem web/tests/*.test.mjs | Select-Object -ExpandProperty FullName` 이후 `node --test $webTests`: **32개 통과**. pytest에서도 같은 방식으로 파일을 열거해 실행한다.
  - `.venv\Scripts\python -m pytest -q`: **실행 불가**. 기존 환경 메모와 동일하게 기반 Python312 프로세스를 생성하지 못함.
  - 대체 Python `C:/Users/SSAFY/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe`, `PYTHONPATH="$PWD/src;$PWD/.venv/Lib/site-packages"`로 `-m pytest -q --basetemp=.pytest-tmp-t015`: **820 passed, 1 failed, 1 warning (129.68초)**. 유일한 실패는 아래 기존 임시 화면 문구 검사. 경고는 기존 오프라인 수집 테스트의 WIKI_CONTACT 안내.
  - 최종 수정 후 동일 대체 Python으로 `-m pytest -q tests/test_web_static.py --basetemp=.pytest-tmp-t015-web`: **3 passed (1.57초)**. 이 실행에 최종 Node 테스트 32개 전체가 포함됨.
  - `web/js/*.js` 각각 `node --check`: 모두 통과. `git diff --check`: 통과(LF→CRLF 작업 트리 안내만 있음).
  - `.env` 직접 열람/출력 및 실제 LLM 호출은 하지 않음.
- 설계 판단과 이유:
  - 서버 문자열은 텍스트 노드/`textContent`로만 표시한다. 굵게·목록·문단·줄바꿈·인용 버튼만 DOM으로 생성하며, 원문 링크는 HTTP(S)만 허용한다. 답변·근거·표·경고에 스크립트/이미지 이벤트 문자열을 넣어도 실행 요소가 생성되지 않음을 검사했다.
  - SSE 파서는 문자열 버퍼를 유지하고 `TextDecoder`를 스트리밍 모드로 사용한다. 모든 문자 분할 위치, CRLF 경계, 한국어 UTF-8 바이트 분할, keep-alive, 여러 이벤트, 중도 종료/잘못된 JSON, result/error 중 1회만 종료 콜백이 오는 경우를 검증했다. 진행 단계는 `(stage, detail)`의 연속 중복만 제거해 서로 다른 수집 문서 제목은 유지한다.
  - uid·선택 세트·탭만 localStorage에 저장하고 대화는 서버에서 다시 읽는다. 저장소 접근 제한 시 메모리 상태로 계속 동작한다. 빠른 탐색 중 이전 응답이 새 대화를 덮지 않도록 로드 순번을 확인하며, 질문 처리 중에는 중복 제출·세트/탭 이동·삭제를 잠근다. 미저장 답변은 세션 메모리에 보존하고 새로고침 시 사라짐을 안내한다.
  - 최신 답변을 기본 근거 대상으로 선택하고 이전 답변/인용을 누르면 테두리·패널 제목을 바꾼다. 없는 인용 번호는 패널을 전환하지 않는다. 모바일에는 답변별 근거를 둔다.
  - CSS 미디어 조건은 `var()`를 지원하지 않으므로 폭 기준은 CSS의 `--breakpoint-medium`/`--breakpoint-wide` 두 토큰에만 두고 JS `matchMedia`가 읽어 레이아웃 속성을 갱신한다. 색상은 CSS 변수와 `prefers-color-scheme`로 처리한다.
  - DOM/API 대역 흐름 테스트에서 세트별 기록 분리·이전 답변 근거 선택·없는 인용 번호·컬렉션 저장·실제 SSE 단계 중복 제거·탐색 카드→아카이브 이동·삭제·0건 결과·409·네트워크 오류·키 누락 시 입력 비활성을 확인했다. 키 누락 상태에서도 기록 조회와 컬렉션 편집은 유지된다.
- 남은 이슈 / PM 확인 필요:
  1. `tests/test_server_api.py:19`가 루트 응답에 기존 임시 문구 `T-015에서 구현`이 있는지 검사해 실패한다. 구현된 화면에서는 해당 문구를 제거했다. 이 파일은 T-015 수정 범위 밖이므로 고치지 않았다. PM이 실제 화면 골격/모듈 로드 검사로 갱신해야 전체 pytest가 통과한다. 테스트 통과만을 위해 임시 문구를 HTML에 남기지 않았다.
  2. 지시서의 Node 검증 명령은 현재 버전에서 디렉터리를 받지 않는다. 위 파일 목록 명시 명령으로 재검증할 수 있으며, pytest 래퍼는 이미 이 방법을 사용한다. npm/package.json/별도 실행 진입점은 추가하지 않았다.
  3. 작업지시서 결정 ⑥대로 실제 키·브라우저 대표 질문·스크린샷 검증은 PM이 수행해야 한다. 실제 브라우저에서 새로고침 복원, 360px 폭의 가로 넘침 여부, 중간 폭 접이식 근거, 다크 모드 및 키보드 조작을 확인할 것. DOM 대역 테스트는 실제 레이아웃 검증을 대신하지 않는다.
  4. PM 환경의 `.venv`로 전체 pytest 및 `uvicorn server.main:app --port 8000` 재확인 필요. 현재 작업에서 브라우저/실제 LLM을 통한 종단 검증 완료를 주장하지 않는다.

## PM 리뷰
- 판정: **승인** (PM 직접 수정 2건 포함, Codex 재호출 없음)
- PM 직접 수정
  1. `tests/test_server_api.py`: 화면 골격 검사를 갱신했다. 임시 문구 `T-015에서 구현` 대신 `<script type="module" src="/js/app.js">`를 확인한다(결과 보고 남은 이슈 1).
  2. **`server/main.py`: `.js`·`.css` MIME 타입을 명시 등록했다.** PM의 `.venv`에서 `test_web_static`이 실패해 원인을 찾았다. 이 PC는 Windows 레지스트리에 `.js`가 `text/plain`으로 등록되어 있어, StaticFiles가 ES 모듈을 `text/plain`으로 보내고 브라우저가 실행을 거부한다(= 웹 화면 전체 불능). Codex의 대체 Python 환경에서는 재현되지 않았다. Codex의 정적 검사 테스트가 이 결함을 잡아냈다.
- 재검증: `.venv` `pytest -q` **821 passed**. `node --test web/tests/*.test.mjs` **32 passed**. `/js/app.js` → `text/javascript`.
- 정적 검사: `web/js`에 `innerHTML`·`insertAdjacentHTML`·`eval`이 없다. 외부 URL은 근거 원문 링크의 프로토콜 검사(`http:`/`https:`)뿐이다.
- 실제 키·인앱 브라우저 실측 (uvicorn :8000)
  - 첫 화면: 콘솔 오류 0. 상태 "질문 가능".
  - 세트 검색 "엑스윙" → 20개, 별칭 전체 표시.
  - 75423-1 서술 질문 "레드 파이브는 어떤 전투에서 활약했어?"
    - **질문 시점 수집이 실제로 일어났고** 진행 단계에 문서 제목(Yavin 4, Universe of Star Wars, 야빈 4)이 보였다.
    - 이후 검색 → 생성 → 검증을 거쳐 굵게·목록이 렌더링된 답변이 나왔다(약 30초).
  - `[근거 2]` 클릭 → 근거 패널이 "1번째 질문의 근거"로 바뀌고, Yavin 4 › Battle of Yavin 카드가 펼쳐져 강조된다. 원문 링크·CC BY-SA 4.0 라이선스가 표시된다 (F2008).
  - 탐색 탭 "2021년에 나온 마이크로파이터" → 카드 2개. "이 세트로 대화"를 누르면 아카이브 탭(`aria-selected`)과 75295-1로 전환된다 (F2011).
  - 새로고침하면 uid·선택 세트·활성 탭(localStorage)과 서버의 대화 기록이 복원된다. "내 대화" 목록에 세트별·탐색 대화가 표시된다 (F2009). T-013 Streamlit의 선택 초기화 문제는 웹에는 없다.
  - 360px: 문서 폭 = 뷰포트 폭으로 가로 넘침이 없다. 1단 배치이고 "세트 · 내 대화" 버튼이 있다. 1280px: 3단(사이드바 | 대화 | 근거 패널).
- 남은 이슈 (M6)
  1. 진행 단계에 "문서 수집"이 제목 없이 여러 번 나온다(searching·selecting·indexing). 제목 있는 줄만 남기고, 제목 없는 같은 단계는 1번으로 합치는 방안을 검토한다.
  2. 헤더의 "SERIES ARCHIVE AI" 부제가 HTML에 고정되어 있다. 서비스 최종 명칭(L7)이 정해지면 settings에서 읽게 한다.
  3. 새로고침 후 사이드바 세트 목록이 선택 세트 위치로 스크롤되지 않는다(제목에는 반영됨).
