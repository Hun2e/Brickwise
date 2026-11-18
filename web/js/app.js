import { api, streamChat, safeMessage, conversations, createConversation, conversation, changeSubject, setHistory, chatRequest } from "./api.js";
import { readState, saveState, legacyThread } from "./state.js";
import { searchSets, setDescription } from "./sets.js";
import { el, renderAnswer, renderEvidence, renderTables, renderInfo, renderItems, renderSubject, renderHistory, renderPending, localTimestamp } from "./render.js";
const $ = id => document.getElementById(id);
let state = readState();
const legacy = legacyThread();
export const EXAMPLE_QUESTIONS = [
  "루크가 들어 있는 엑스윙 찾아줘", "75192랑 75375 차이 뭐야?",
  "보바 펫은 어떤 캐릭터야?", "내 부품으로 뭐 만들 수 있어?",
];
let sets = [], status = null, current = null, busy = false;
let ownedSets = new Set();
const stageHistory = new Map();
let collectionLoaded = false, collectionVersion = 0, collectionDirty = false;
const requests = new Map(), completed = new Set(), unsaved = new Map();
let retry = null;
function persist(changes) { state = saveState(changes); }
function notice(text = "") { $("notice").textContent = text; }
function navigationButton(text, handler) {
  const button = el("button", text); button.type = "button";
  button.dataset.navigation = "true"; button.disabled = busy;
  button.addEventListener("click", handler); return button;
}
function updateControls() {
  const enabled = Boolean(current && status?.can_chat && !busy);
  $("question").disabled = !enabled; $("send").disabled = !enabled;
  for (const button of document.querySelectorAll("[data-navigation]"))
    button.disabled = busy || !current || button.dataset.registered === "true" || (button.dataset.chatAction === "true" && !status?.can_chat);
  $("new-conversation").disabled = busy || !status?.user_db_ok;
  $("clear-conversation").disabled = busy || !state.conversationId;
  $("collection-fields").disabled = busy || !collectionLoaded || !status?.db_ok || !status?.user_db_ok;
  $("retry").hidden = !retry; $("retry").disabled = !enabled;
  $("chat-hint").textContent = busy ? "질문을 처리하고 있습니다. 완료될 때까지 기다려 주세요." :
    !status?.can_chat ? (status?.messages?.join(" ") || "서버 상태를 확인하세요.") :
    current?.state_summary.subject ? `${current.state_summary.subject_name || current.state_summary.subject}에 대해 물어보세요` : "Enter로 보내기 · Shift+Enter로 줄바꿈";
}
function renderSetList() {
  const found = searchSets(sets, $("set-search").value);
  $("set-count").textContent = `${found.length}개 세트`; $("set-results").replaceChildren();
  if (!found.length) $("set-results").append(el("li", "검색 결과가 없습니다.", "muted"));
  for (const set of found) {
    const row = el("li"); row.append(navigationButton(setDescription(set), () => {
      if (busy || !collectionLoaded) return;
      const existing = [...$("collection-rows").rows].find(row => row.dataset.setNum === set.set_num);
      if (existing) { existing.querySelector("input").focus(); $("collection-message").textContent = "이미 목록에 있어요. 수량을 고쳐 주세요."; }
      else { addCollectionRow(set.set_num); collectionDirty = true; $("collection-message").textContent = "세트를 추가했어요. 목록 저장을 눌러 주세요."; }
    }));
    $("set-results").append(row);
  }
}
async function loadConversations() {
  const rows = await conversations(); $("conversations").replaceChildren();
  for (const row of rows) {
    const node = el("li"); node.append(navigationButton(`${row.title} · ${localTimestamp(row.updated_at)}`, () => openConversation(row.id)));
    $("conversations").append(node);
  }
  return rows;
}
async function refreshContext() {
  $("subject-label").textContent = renderSubject(current?.state_summary);
  $("subject-bar").hidden = !current?.state_summary.subject;
  $("pending-message").textContent = current?.state_summary.pending || "";
  $("pending-choices").replaceChildren(renderPending(current?.state.pending, question => submit(question)));
  $("history-content").replaceChildren(el("p", "세트를 정하면 그 세트와 나눈 이야기만 모아 보여 줘요", "muted"));
  const number = current?.state_summary.subject;
  if (number) {
    try { $("history-content").replaceChildren(renderHistory(await setHistory(number))); }
    catch (error) { $("history-content").replaceChildren(el("p", safeMessage(error), "warning")); }
  }
}
async function selectSubject(number) {
  if (busy || !current) return;
  busy = true; updateControls(); notice();
  try {
    if (state.conversationId) current = await changeSubject(state.conversationId, number, current.version);
    else {
      current.state.subject = number ? { set_num: number } : null;
      current.state_summary.subject = number;
      current.state_summary.subject_name = sets.find(set => set.set_num === number)?.name || null;
    }
    await refreshContext(); await loadConversations();
  } catch (error) {
    notice(safeMessage(error));
    if (error.code === "state_conflict") { current = await conversation(state.conversationId); await refreshContext(); }
  } finally { busy = false; updateControls(); $("question").focus(); }
}
function selectEvidence(view, number) {
  const card = view.inlineCards.get(number);
  if (!card) return;
  view.sources.open = true;
  for (const other of view.inlineCards.values()) other.classList.remove("highlight");
  card.open = true; card.classList.add("highlight"); card.tabIndex = -1;
  card.focus({ preventScroll: true });
  card.scrollIntoView({ behavior: "smooth", block: "nearest" });
}
function scrollToEnd() { $("conversation-end").scrollIntoView({ block: "end" }); }
function setDrawer(open) {
  $("sidebar").hidden = !open; $("drawer-backdrop").hidden = !open;
  $("sidebar-toggle").setAttribute("aria-expanded", String(open));
  for (const node of [document.querySelector(".header"), $("workspace"), $("composer")]) node.inert = open;
  (open ? $("sidebar-close") : $("sidebar-toggle")).focus();
}
function welcome() {
  const node = el("section", undefined, "empty-state");
  node.append(el("h2", "어떤 레고를 찾고 있나요?"), el("p", "세트를 찾고, 궁금한 걸 묻고, 가진 레고로 만들 수 있는 걸 추천받아요", "muted"));
  const examples = el("div", undefined, "examples");
  for (const question of EXAMPLE_QUESTIONS) {
    const button = navigationButton(question, () => submit(question));
    button.dataset.chatAction = "true"; examples.append(button);
  }
  node.append(examples); return node;
}
function updateRegistrationButtons() {
  for (const button of document.querySelectorAll("[data-register-set]")) {
    const registered = ownedSets.has(button.dataset.registerSet) || completed.has(button.dataset.cardKey);
    button.dataset.registered = String(registered);
    button.textContent = registered ? "✓ 가지고 있어요" : "갖고 있어요";
  }
}

function displayTurn(turn, mount = $("turns"), showQuestion = true) {
  if (showQuestion) mount.append(el("p", turn.question, "question-bubble"));
  const answer = turn.answer;
  const node = el("article", undefined, "answer");
  const view = { inlineCards: new Map(), sources: el("details", undefined, "sources") };
  node.append(renderAnswer(answer.answer || answer.needs_input || "확인되지 않음", { onCite: n => selectEvidence(view, n) }));
  if (answer.warnings?.length) node.append(el("p", answer.warnings.join(" · "), "warning"));
  if (turn.action) node.append(el("p", `${turn.action.set_num}: 보유 ${turn.action.qty_before}개 → ${turn.action.qty_after}개`, "action-result"));
  node.append(renderItems(turn.items || answer.items || [], {
    onSubject: item => selectSubject(item.set_num),
    isRegistered: item => ownedSets.has(item.set_num) || completed.has(`${state.conversationId}:${turn.id}:${item.set_num}`),
    onRegister: item => {
      const key = `${state.conversationId}:${turn.id}:${item.set_num}`;
      if (completed.has(key) || ownedSets.has(item.set_num)) { notice("이미 가지고 있는 세트예요. 메뉴에서 수량을 고칠 수 있어요."); return; }
      if (!requests.has(key)) requests.set(key, chatRequest(state.conversationId, `${item.set_num} 하나 보유 목록에 등록해줘`));
      return submit(requests.get(key).question, requests.get(key), key);
    },
  }));
  if (answer.route === "filter" && !(turn.items || answer.items || []).length && !answer.needs_input)
    node.append(el("p", "조건에 맞는 세트가 없습니다. 조건을 바꾸어 다시 탐색하세요.", "muted"));
  if (answer.tables.length) {
    const tables = el("details"); tables.append(el("summary", "상세 표"), renderTables(answer.tables)); node.append(tables);
  }
  if (!turn.saved) node.append(el("p", "이 답변은 저장되지 않았습니다. 새로고침 전에 내용을 보관하세요.", "warning"));
  const evidence = renderEvidence(answer.cards, answer.answer, (n, card) => view.inlineCards.set(n, card));
  view.sources.append(el("summary", `출처 보기 (${view.inlineCards.size})`), evidence);
  node.append(view.sources, renderInfo(answer, stageHistory.get(`${state.conversationId}:${turn.id}`) || []));
  for (const button of node.querySelectorAll("[data-register-set]"))
    button.dataset.cardKey = `${state.conversationId}:${turn.id}:${button.dataset.registerSet}`;
  mount.append(node);
}
function drawConversation() {
  $("turns").replaceChildren();
  const turns = [...current.turns, ...(unsaved.get(state.conversationId) || [])];
  if (!turns.length) $("turns").append(welcome());
  turns.forEach(turn => displayTurn(turn));
  if (turns.length) scrollToEnd();
  else $("workspace").scrollTop = 0;
}
async function openConversation(id) {
  if (busy) return;
  busy = true; updateControls();
  try {
    const loaded = await conversation(id);
    persist({ conversationId: id }); current = loaded; retry = null;
    drawConversation(); await refreshContext();
    setDrawer(false);
  } catch (error) { notice(safeMessage(error)); }
  finally { busy = false; updateControls(); }
}
async function newDraft() {
  persist({ conversationId: null }); retry = null;
  current = { turns: [], version: 0, state: { subject: null, pending: null },
    state_summary: { subject: null, subject_name: null, pending: null, mode: "exact", version: 0 } };
  drawConversation(); await refreshContext(); updateControls();
}
async function submit(question, request = null, cardKey = null) {
  if (busy || !current || !status?.can_chat || !question.trim()) return;
  busy = true; notice(); updateControls();
  try {
    if (!state.conversationId) {
      const subject = current.state_summary.subject;
      const created = await createConversation();
      persist({ conversationId: created.id });
      current = subject ? await changeSubject(created.id, subject, 0) : await conversation(created.id);
    }
    request ||= retry?.request.question === question ? retry.request : chatRequest(state.conversationId, question);
  } catch (error) {
    notice(safeMessage(error)); busy = false; updateControls(); return;
  }
  $("turns").querySelector(".empty-state")?.remove();
  const mount = el("section", undefined, "turn");
  const progress = el("div", undefined, "progress"); progress.setAttribute("role", "status"); progress.setAttribute("aria-live", "polite");
  const label = el("p", "찾는 중…"); progress.append(label);
  mount.append(el("p", question, "question-bubble"), progress); $("turns").append(mount); scrollToEnd();
  const stages = [];
  await streamChat(request, {
    onStage: stage => { stages.push(stage); label.textContent = `찾는 중… (${stage.stage}${stage.detail ? `: ${stage.detail}` : ""})`; scrollToEnd(); },
    onResult: result => {
      const turn = result.turn; progress.remove(); retry = null;
      stageHistory.set(`${state.conversationId}:${turn.id}`, stages);
      // 이미 끝난 요청의 재전송이 더 최신인 대상·pending을 되돌리지 않게 한다.
      if (turn.state_summary.version >= current.version) {
        current.state = result.state; current.state_summary = turn.state_summary; current.version = turn.state_summary.version;
      }
      if (cardKey && result.action?.applied) completed.add(cardKey);
      if (result.action?.applied && result.action.qty_after > 0) ownedSets.add(result.action.set_num);
      if (turn.saved) {
        if (!current.turns.some(t => t.id === turn.id)) current.turns.push(turn);
        unsaved.set(state.conversationId, (unsaved.get(state.conversationId) || []).filter(t => t.request_id !== turn.request_id));
      } else {
        unsaved.set(state.conversationId, [...(unsaved.get(state.conversationId) || []), turn]); retry = { request, cardKey };
      }
      displayTurn(turn, mount, false); $("question").value = "";
      updateRegistrationButtons(); scrollToEnd();
    },
    onError: error => { retry = { request, cardKey }; label.textContent = "처리 실패"; mount.append(el("p", safeMessage(error), "warning"), renderInfo({ route: "미완료", timings: {} }, stages)); scrollToEnd(); },
  });
  try { await refreshContext(); await loadConversations(); if (!collectionDirty) await loadCollection(); }
  catch (error) { notice(safeMessage(error)); }
  finally { busy = false; updateControls(); scrollToEnd(); }
}
function addCollectionRow(number = "", quantity = 1) {
  const row = el("tr");
  row.dataset.setNum = number;
  const set = sets.find(set => set.set_num === number);
  // 범위 밖 세트도 원래 번호를 보존해 목록 저장으로 유실되지 않게 한다.
  const choice = el("span", set ? `${set.name} · ${number.replace(/-1$/, "")}` : `${number} (현재 목록에 없음)`);
  const count = el("input"); count.type = "number"; count.min = "1"; count.step = "1"; count.required = true;
  count.value = quantity; count.setAttribute("aria-label", "보유 수량");
  const remove = el("button", "빼기"); remove.type = "button";
  remove.addEventListener("click", () => { row.remove(); collectionDirty = true; });
  for (const child of [choice, count, remove]) { const cell = el("td"); cell.append(child); row.append(cell); }
  $("collection-rows").append(row);
}


async function loadCollection() {
  try {
    const collection = await api("/api/collection"); collectionVersion = collection.version; collectionDirty = false;
    ownedSets = new Set(collection.sets.filter(([, qty]) => qty > 0).map(([number]) => number));
    updateRegistrationButtons();
    $("collection-rows").replaceChildren(); collection.sets.forEach(([n, q]) => addCollectionRow(n, q)); collectionLoaded = true;
  } catch (error) { $("collection-message").textContent = safeMessage(error); }
  updateControls();
}
$("collection-form").addEventListener("input", () => { collectionDirty = true; });
$("collection-form").addEventListener("submit", async event => {
  event.preventDefault(); if (busy || !collectionLoaded) return;
  const owned = [...$("collection-rows").rows].map(row => [row.dataset.setNum, Number(row.querySelector("input").value)]);
  if (owned.some(([n, q]) => !n || !Number.isSafeInteger(q) || q < 1)) { $("collection-message").textContent = "세트와 1 이상의 정수 수량을 입력하세요."; return; }
  busy = true; updateControls();
  try {
    await api("/api/collection", { method: "PUT", body: JSON.stringify({ sets: owned, version: collectionVersion }) });
    await loadCollection(); $("collection-message").textContent = "내가 가진 레고를 저장했습니다.";
  } catch (error) { $("collection-message").textContent = safeMessage(error); }
  finally { busy = false; updateControls(); }
});
$("reload-collection").addEventListener("click", () => { if (!busy) return loadCollection(); });
$("chat-form").addEventListener("submit", event => { event.preventDefault(); return submit($("question").value.trim()); });
$("retry").addEventListener("click", () => retry && submit(retry.request.question, retry.request, retry.cardKey));
$("new-conversation").addEventListener("click", async () => {
  if (busy) return; busy = true; updateControls();
  try { await newDraft(); await loadConversations(); }
  catch (error) { notice(safeMessage(error)); }
  finally { busy = false; updateControls(); }
});
$("clear-conversation").addEventListener("click", async () => {
  if (busy || !current || !window.confirm("이 대화를 지울까요?")) return;
  busy = true; updateControls();
  try {
    await api(`/api/conversations/${encodeURIComponent(state.conversationId)}`, { method: "DELETE" });
    unsaved.delete(state.conversationId);
    await newDraft(); await loadConversations();
  } catch (error) { notice(safeMessage(error)); }
  finally { busy = false; updateControls(); }
});
$("clear-subject").addEventListener("click", () => selectSubject(null));
$("set-search").addEventListener("input", renderSetList);
$("add-set").addEventListener("click", () => {
  $("set-picker").hidden = !$("set-picker").hidden;
  $("add-set").setAttribute("aria-expanded", String(!$("set-picker").hidden));
  renderSetList(); if (!$("set-picker").hidden) $("set-search").focus();
});
$("sidebar-toggle").addEventListener("click", () => setDrawer($("sidebar").hidden));
$("sidebar-close").addEventListener("click", () => setDrawer(false));
$("drawer-backdrop").addEventListener("click", () => setDrawer(false));
document.addEventListener("keydown", event => {
  if (!$("sidebar").hidden && event.key === "Escape") { event.preventDefault(); setDrawer(false); }
  if (!$("sidebar").hidden && event.key === "Tab") {
    const focusable = [...$("sidebar").querySelectorAll("button, input, select, summary")]
      .filter(node => !node.matches(":disabled") && node.getClientRects().length);
    const first = focusable[0], last = focusable.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }
});
$("question").addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault(); return submit($("question").value.trim());
  }
});

async function start() {
  try {
    if (!state.uid) persist({ uid: (await api("/api/users", { method: "POST" })).uid });
    const results = await Promise.allSettled([api("/api/status"), api("/api/sets")]);
    if (results[0].status === "fulfilled") status = results[0].value; else notice(safeMessage(results[0].reason));
    if (results[1].status === "fulfilled") sets = results[1].value; else notice(safeMessage(results[1].reason));
    $("status-badge").textContent = status?.can_chat ? "질문 가능" : "준비 필요";
    $("status-messages").replaceChildren(...(status?.messages || ["서버 상태를 확인하세요."]).map(s => el("p", s)));
    $("status").hidden = Boolean(status?.can_chat && !status?.messages?.length);
    const rows = await loadConversations();
    let id = rows.find(row => row.id === state.conversationId)?.id;
    if (!id && legacy) {
      const turns = await api(`/api/threads/${encodeURIComponent(legacy)}/turns`);
      id = turns.find(turn => rows.some(row => row.id === turn.conversation_id))?.conversation_id;
    }
    id ||= state.conversationId ? rows[0]?.id : null;
    if (id) await openConversation(id); else await newDraft();
    await loadCollection(); renderSetList();
  } catch (error) { notice(safeMessage(error)); }
  updateControls();
}
await start();
