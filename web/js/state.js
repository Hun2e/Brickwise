const KEY = "lego-archive-state";
let memory = { uid: "", conversationId: null };
let legacy = null;

export function readState() {
  try {
    const saved = JSON.parse(globalThis.localStorage.getItem(KEY) || "{}");
    legacy = saved.activeTab === "archive" ? saved.selectedSet : saved.activeTab;
    memory = {
      uid: /^[a-f0-9]{32}$/i.test(saved.uid) ? saved.uid : "",
      conversationId: typeof saved.conversationId === "string" && saved.conversationId ? saved.conversationId : null,
    };
  } catch { /* 저장소를 사용할 수 없어도 현재 세션을 유지한다. */ }
  return { ...memory };
}
export function legacyThread() { return typeof legacy === "string" ? legacy : null; }
export function saveState(changes) {
  memory = { uid: changes.uid ?? memory.uid,
    conversationId: Object.hasOwn(changes, "conversationId") ? changes.conversationId : memory.conversationId };
  try { globalThis.localStorage.setItem(KEY, JSON.stringify(memory)); } catch { /* 세션 내 상태 유지 */ }
  return { ...memory };
}
