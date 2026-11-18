import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

/** 화면 연결 테스트용 DOM 대역. 레이아웃/실제 브라우저 검증은 PM이 수행한다. */
class Element {
  constructor(tag, text = "") {
    this.tag = tag; this.children = []; this.text = text; this.attributes = {};
    this.dataset = {}; this.listeners = {}; this.value = ""; this.disabled = false;
    this.className = ""; this.hidden = false; this.open = false;
    const classes = () => new Set(this.className.split(/\s+/).filter(Boolean));
    this.classList = {
      add: name => { const list = classes(); list.add(name); this.className = [...list].join(" "); },
      remove: name => { const list = classes(); list.delete(name); this.className = [...list].join(" "); },
      contains: name => classes().has(name),
      toggle: name => { const exists = classes().has(name); this.classList[exists ? "remove" : "add"](name); return !exists; },
    };
  }
  append(...children) {
    for (const child of children) {
      if (child.tag === "#fragment") this.append(...child.children);
      else { child.parent = this; this.children.push(child); }
    }
  }
  replaceChildren(...children) { this.children = []; this.text = ""; this.append(...children); }
  set textContent(text) { this.text = String(text); this.children = []; }
  get textContent() { return this.text + this.children.map(child => child.textContent).join(""); }
  get rows() { return this.children; }
  setAttribute(key, value) {
    this.attributes[key] = value;
    if (key.startsWith("data-")) this.dataset[key.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())] = value;
    if (key === "class") this.className = value;
    if (["hidden", "disabled", "open"].includes(key)) this[key] = true;
  }
  matches(selector) {
    if (selector.includes(",")) return selector.split(/,\s*/).some(part => this.matches(part));
    if (selector === ":disabled") return this.disabled || Boolean(this.closest("fieldset")?.disabled);
    if (selector.startsWith(".")) return this.classList.contains(selector.slice(1));
    const attribute = /^\[([^=\]]+)(?:=([^\]]+))?\]$/.exec(selector);
    if (attribute) {
      const value = attribute[1].startsWith("data-") ? this.dataset[attribute[1].slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())] : this.attributes[attribute[1]];
      return attribute[2] === undefined ? value !== undefined : value === attribute[2];
    }
    return this.tag === selector;
  }
  querySelectorAll(selector) { return this.children.flatMap(child => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) { return selector.split(/,\s*/).some(part => this.matches(part)) ? this : this.parent?.closest(selector); }
  addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler); }
  async emit(name, fields = {}) { for (const handler of this.listeners[name] || []) await handler({ target: this, preventDefault() {}, stopPropagation() {}, ...fields }); }
  remove() { this.parent.children = this.parent.children.filter(child => child !== this); }
  focus() { this.focused = true; document.activeElement = this; }
  getClientRects() { return this.hidden || this.parent?.getClientRects().length === 0 ? [] : [{}]; }
  scrollIntoView() { this.scrolled = true; }
}

function documentFixture() {
  const root = new Element("document"), stack = [root], ids = new Map();
  const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");
  for (const token of html.matchAll(/<\/?([a-z][a-z0-9-]*)([^>]*)>/gi)) {
    const tag = token[1];
    if (token[0].startsWith("</")) { stack.pop(); continue; }
    const node = new Element(tag);
    for (const attribute of token[2].matchAll(/([\w-]+)(?:="([^"]*)")?/g)) node.setAttribute(attribute[1], attribute[2] ?? "");
    stack.at(-1).append(node);
    if (node.attributes.id) ids.set(node.attributes.id, node);
    if (!["meta", "link", "input"].includes(tag)) stack.push(node);
  }
  return Object.assign(root, {
    documentElement: root.querySelector("html"), getElementById: id => ids.get(id),
    createElement: tag => new Element(tag), createTextNode: text => new Element("#text", text),
    createDocumentFragment: () => new Element("#fragment"),
  });
}

let scenario = 0;
const item = (n, rank = 1) => ({ rank, set_num: n, name: n === "A-1" ? "Falcon" : "X-wing", year: 2021, num_parts: 100 });
const summary = c => ({ subject: c.state.subject?.set_num || null, subject_name: c.state.subject ? item(c.state.subject.set_num).name : null,
  version: c.version, pending: c.state.pending ? "대상을 골라 주세요" : null, mode: c.state.mode });
const makeTurn = (id, q, items = [], extra = {}) => ({ id, question: q, saved: true, thread: "general", created_at: "2026-10-23", items,
  answer: { route: "fact", answer: `${q} 답변 [근거 1] [근거 99]`, cards: [{ n: 1, title: "DB 근거", source_type: "db", url: "https://example.com", license: "CC", text: "발췌" }],
    tables: [], warnings: [], timings: { total_ms: 1 }, set_nums: items.map(i => i.set_num), needs_input: null }, ...extra });
async function setup(t, ready = true, legacy = false) {
  const document = documentFixture(); globalThis.document = document;
  const storage = new Map();
  globalThis.localStorage = { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value) };
  globalThis.window = { confirm: () => true };
  globalThis.getComputedStyle = () => ({ getPropertyValue: name => name.endsWith("medium") ? "768px" : "1100px" });
  globalThis.matchMedia = query => ({ matches: query.includes("1100"), addEventListener() {} });
  const catalog = ["A-1", "B-1"].map(n => ({ ...item(n), label: `${n} ${item(n).name}`, scope: "core", aliases: [] }));
  const db = new Map(), replay = new Map(), calls = [];
  let collection = { sets: [], adjustments: [], version: 0 }, failOnce = false;
  let pauseStream = false, finishStream;
  const add = () => {
    const id = `conv-${db.size + 1}`;
    db.set(id, { id, title: "새 대화", turns: [], state: { subject: null, last_list: null, pending: null, mode: "exact" }, version: 0 }); return id;
  };
  if (legacy) {
    add(); add();
    storage.set("lego-archive-state", JSON.stringify({ uid: "f".repeat(32), selectedSet: "B-1", activeTab: "archive" }));
  }
  t.mock.method(globalThis, "fetch", async (path, options) => {
    const body = options.body ? JSON.parse(options.body) : null; calls.push({ path, options, body });
    if (path === "/api/users") return Response.json({ uid: "f".repeat(32) });
    if (path === "/api/status") return Response.json({ can_chat: ready, db_ok: true, user_db_ok: true, messages: ready ? [] : ["API 키가 설정되지 않았습니다. GMS_KEY를 설정하세요."] });
    if (path === "/api/sets") return Response.json(catalog);
    if (path === "/api/conversations") {
      if (options.method === "POST") return Response.json({ id: add() });
      return Response.json([...db.values()].reverse().map(c => ({ id: c.id, title: c.title, updated_at: "2026-10-23" })));
    }
    if (path === "/api/threads/B-1/turns") return Response.json([{ conversation_id: "conv-1" }]);
    if (path === "/api/collection") {
      if (options.method === "PUT") {
        if (body.version !== collection.version) return Response.json({ code: "collection_conflict", message: "보유 목록이 변경되었습니다. 다시 불러오세요." }, { status: 409 });
        collection = { ...collection, sets: body.sets, version: collection.version + 1 };
      }
      return Response.json(collection);
    }
    if (path.startsWith("/api/sets/")) {
      const n = path.split("/")[3];
      return Response.json([makeTurn(80, `${n} 전용 기록`), makeTurn(81, "비교 질문", [], { comparison_label: "비교: A-1 · B-1" })]);
    }
    if (path.startsWith("/api/conversations/")) {
      const id = path.split("/")[3], c = db.get(id);
      if (options.method === "DELETE") { db.delete(id); return Response.json({ deleted: 1 }); }
      if (options.method === "PATCH") { c.state.subject = body.set_num ? { set_num: body.set_num } : null; c.version++; }
      return Response.json({ ...c, state_summary: summary(c) });
    }
    if (path === "/api/chat") {
      const c = db.get(body.conversation_id), q = body.question;
      if (q === "network" && !failOnce) { failOnce = true; throw Error("Traceback private-key"); }
      if (q === "busy") return Response.json({ code: "busy" }, { status: 409 });
      let result = replay.get(body.request_id);
      if (!result) {
        let items = [], action = null;
        if (q.includes("찾아")) { items = [item("A-1"), item("B-1", 2)]; c.state.last_list = { kind: "search", items: ["A-1", "B-1"] }; }
        if (q.includes("그중")) c.state.subject = { set_num: "A-1" };
        if (q.includes("등록")) { collection = { ...collection, sets: [["A-1", 1]], version: collection.version + 1 }; action = { set_num: "A-1", qty_before: 0, qty_after: 1, applied: true }; }
        if (q.includes("다른 건")) { items = [{ ...item("B-1"), missing: 12, lots_missing: 3 }]; c.state.last_list = { kind: "recommend", items: ["B-1"] }; }
        if (q.includes("첫 번째")) c.state.subject = { set_num: c.state.last_list.items[0] };
        if (q.includes("색상")) c.state.mode = "ignore_color";
        if (q === "되묻기") c.state.pending = { kind: "target", options: ["B-1"] };
        if (q === "B-1") { c.state.pending = null; c.state.subject = { set_num: q }; }
        c.version++;
        const turn = makeTurn(c.turns.length + 1, q, items, { state_summary: summary(c), action, request_id: body.request_id });
        if (q === "인용 없음") turn.answer.answer = "확인되지 않음";
        c.turns.push(turn); result = { turn, state: structuredClone(c.state), action }; replay.set(body.request_id, result);
      }
      if (q === "disconnect" && !failOnce) { failOnce = true; throw Error("network lost after commit"); }
      if (pauseStream) {
        pauseStream = false;
        return new Response(new ReadableStream({ start(controller) {
          const encoder = new TextEncoder();
          controller.enqueue(encoder.encode('event: stage\ndata: {"stage":"문서 수집","detail":"Return of the Jedi"}\n\n'));
          finishStream = () => {
            controller.enqueue(encoder.encode('event: result\ndata: ' + JSON.stringify(result) + '\n\n'));
            controller.close();
          };
        } }), { headers: { "Content-Type": "text/event-stream" } });
      }
      return new Response('event: stage\ndata: {"stage":"분류"}\n\nevent: stage\ndata: {"stage":"분류"}\n\nevent: stage\ndata: {"stage":"수집","detail":"文書"}\n\nevent: result\ndata: ' + JSON.stringify(result) + "\n\n", { headers: { "Content-Type": "text/event-stream" } });
    }
    throw Error(`unexpected ${path}`);
  });
  const reload = async () => {
    globalThis.document = documentFixture();
    await import(`../js/app.js?scenario=${scenario++}`);
  };
  await import(`../js/app.js?scenario=${scenario++}`);
  return { $: id => globalThis.document.getElementById(id), db, calls, storage, reload,
    pauseNext: () => { pauseStream = true; }, finish: () => finishStream(),
    collection: () => collection, externalChange: () => { collection.version++; } };
}
async function send($, q) { $("question").value = q; await $("chat-form").emit("submit"); }

test("welcome has four direct-send examples, healthy status and drawer are hidden", async t => {
  const { $, calls } = await setup(t);
  assert.match($("turns").textContent, /어떤 레고를 찾고 있나요/);
  assert.match($("turns").textContent, /가진 레고로 만들 수 있는 걸 추천받아요/);
  assert.equal($("status").hidden, true);
  assert.equal($("sidebar").hidden, true);
  assert.equal($("drawer-backdrop").hidden, true);
  const examples = $("turns").querySelector(".examples").querySelectorAll("button");
  assert.equal(examples.length, 4);
  for (let index = 0; index < 4; index++) {
    const button = $("turns").querySelector(".examples").querySelectorAll("button")[index];
    const question = button.textContent;
    await button.emit("click");
    assert.equal(calls.filter(call => call.path === "/api/chat").at(-1).body.question, question);
    assert.equal($("turns").querySelector(".empty-state"), null);
    await $("new-conversation").emit("click");
  }
});

test("drawer closes outside or with Escape, traps focus, and opens a conversation", async t => {
  const { $ } = await setup(t);
  await send($, "이전 질문");
  await $("new-conversation").emit("click");
  await $("sidebar-toggle").emit("click");
  assert.equal($("sidebar").hidden, false);
  assert.equal($("workspace").inert, true);
  assert.equal($("sidebar-toggle").attributes["aria-expanded"], "true");
  assert.equal(document.activeElement, $("sidebar-close"));
  await document.emit("keydown", { key: "Tab", shiftKey: true });
  assert.notEqual(document.activeElement, $("sidebar-close"));
  await document.emit("keydown", { key: "Tab" });
  assert.equal(document.activeElement, $("sidebar-close"));
  await $("drawer-backdrop").emit("click");
  assert.equal($("sidebar").hidden, true);
  assert.equal($("workspace").inert, false);
  assert.equal(document.activeElement, $("sidebar-toggle"));
  await $("sidebar-toggle").emit("click");
  await document.emit("keydown", { key: "Escape" });
  assert.equal($("drawer-backdrop").hidden, true);
  await $("sidebar-toggle").emit("click");
  await $("conversations").querySelector("button").emit("click");
  assert.equal($("sidebar").hidden, true);
  assert.match($("turns").textContent, /이전 질문/);
});

test("Enter sends once; Shift+Enter and Korean composition do not; progress collapses after completion", async t => {
  const { $, calls, pauseNext, finish } = await setup(t);
  $("question").value = "첫 질문";
  for (const fields of [{ shiftKey: true }, { isComposing: true }, { keyCode: 229 }])
    await $("question").emit("keydown", { key: "Enter", ...fields });
  assert.equal(calls.filter(call => call.path === "/api/chat").length, 0);
  pauseNext();
  const pending = $("question").emit("keydown", { key: "Enter" });
  await new Promise(setImmediate);
  assert.equal($("send").disabled, true);
  assert.equal($("question").disabled, true);
  const progress = $("turns").querySelector(".progress");
  assert.equal(progress.textContent, "찾는 중… (문서 수집: Return of the Jedi)");
  assert.equal(progress.querySelector("ol"), null);
  await $("question").emit("keydown", { key: "Enter" });
  await $("chat-form").emit("submit");
  assert.equal(calls.filter(call => call.path === "/api/chat").length, 1);
  finish(); await pending;
  assert.equal($("turns").querySelector(".progress"), null);
  const processing = $("turns").querySelector(".processing");
  assert.equal(processing.open, false);
  assert.equal(processing.querySelector("summary").textContent, "처리 과정");
  assert.match(processing.textContent, /문서 수집: Return of the Jedi/);
  assert.match(processing.textContent, /경로: fact · 소요 시간: 1 ms/);
  assert.equal($("conversation-end").scrolled, true);
  assert.equal($("send").disabled, false);
});

test("zero citations keep the required empty notice inside collapsed sources", async t => {
  const { $ } = await setup(t);
  await send($, "인용 없음");
  const sources = $("turns").querySelector(".sources");
  assert.equal(sources.open, false);
  assert.equal(sources.querySelector("summary").textContent, "출처 보기 (0)");
  assert.match(sources.textContent, /답변에 인용된 근거가 없습니다/);
  assert.equal(sources.querySelector(".evidence-card"), null);
});

test("owned set search edits quantities, avoids duplicate rows and removes sets", async t => {
  const { $, collection } = await setup(t);
  await $("add-set").emit("click");
  $("set-search").value = "falcon"; await $("set-search").emit("input");
  assert.equal($("set-results").querySelectorAll("button").length, 1);
  await $("set-results").querySelector("button").emit("click");
  await $("set-results").querySelector("button").emit("click");
  assert.equal($("collection-rows").rows.length, 1);
  assert.match($("collection-rows").textContent, /Falcon/);
  $("collection-rows").querySelector("input").value = "3";
  await $("collection-form").emit("submit");
  assert.deepEqual(collection().sets, [["A-1", 3]]);
  await $("collection-rows").querySelector("button").emit("click");
  await $("collection-form").emit("submit");
  assert.deepEqual(collection().sets, []);
  $("set-search").value = "없는 세트"; await $("set-search").emit("input");
  assert.match($("set-results").textContent, /검색 결과가 없습니다/);
});

test("seven steps without tabs or number reentry; subject, list and pending survive reload", async t => {
  const { $, db, calls, reload } = await setup(t);
  assert.equal($("question").disabled, false);
  assert.equal($("subject-bar").hidden, true);
  const questions = ["루크가 들어 있는 엑스윙 찾아줘", "그중 2021년에 나온 건?", "부품이 몇 개야?", "그거 하나 보유 목록에 등록해줘", "내 부품으로 다른 건 뭐 만들 수 있어?", "첫 번째 건 어떤 기체야?", "색상은 무시하면 얼마나 부족해?"];
  for (const q of questions.slice(0, 5)) await send($, q);
  assert.match($("turns").textContent, /0개 → 1개/);
  assert.match($("turns").textContent, /부품 12개 더 필요/);
  await reload();
  assert.equal($("subject-label").textContent, "지금 이야기 중: Falcon A");
  await send($, questions[5]); await send($, questions[6]);
  assert.equal(db.get("conv-1").state.mode, "ignore_color");
  assert.equal(db.get("conv-1").turns.length, 7);
  assert.equal($("subject-label").textContent, "지금 이야기 중: X-wing B");
  assert.ok(calls.filter(c => c.path === "/api/chat").every(c => c.body.conversation_id === "conv-1" && c.body.request_id && !c.body.thread));
  const first = $("turns").querySelector(".answer");
  const sources = first.querySelector(".sources");
  assert.equal(sources.open, false);
  assert.equal(sources.querySelector("summary").textContent, "출처 보기 (1)");
  await first.querySelector(".cite").emit("click");
  assert.equal(sources.open, true);
  assert.equal(sources.querySelector(".evidence-card").open, true);
  assert.equal(sources.querySelector(".evidence-card").classList.contains("highlight"), true);
  assert.equal(sources.querySelector(".evidence-card").scrolled, true);
  assert.equal($("turns").querySelectorAll(".sources")[1].open, false);
  await first.querySelectorAll(".cite")[1].emit("click");
  assert.equal(sources.querySelectorAll(".evidence-card").length, 1);
  await send($, "되묻기"); await reload();
  await $("pending-choices").querySelector("button").emit("click");
  assert.equal(db.get("conv-1").state.pending, null);
});

test("cards change only subject; history isolates sets; registration twice applies once", async t => {
  const { $, db, collection, reload, calls } = await setup(t);
  await send($, "찾아줘");
  const cards = $("turns").querySelectorAll(".result-card");
  await cards[0].querySelector("button").emit("click");
  assert.equal($("question").focused, true);
  assert.match($("chat-hint").textContent, /Falcon에 대해 물어보세요/);
  assert.match($("history-content").textContent, /A-1 전용/);
  await cards[1].querySelector("button").emit("click");
  assert.match($("history-content").textContent, /B-1 전용/);
  assert.doesNotMatch($("history-content").textContent, /A-1 전용/);
  assert.match($("history-content").textContent, /비교: A-1 · B-1/);
  assert.equal(db.get("conv-1").turns.length, 1);
  const own = cards[0].querySelectorAll("button")[1];
  await own.emit("click"); await own.emit("click");
  assert.equal(collection().version, 1);
  assert.match($("notice").textContent, /이미 가지고/);
  assert.equal(own.textContent, "✓ 가지고 있어요");
  assert.equal(own.disabled, true);
  await $("clear-subject").emit("click");
  assert.equal($("subject-bar").hidden, true);
  await reload();
  const restored = $("turns").querySelector(".result-card").querySelectorAll("button")[1];
  assert.equal(restored.textContent, "✓ 가지고 있어요");
  const before = calls.filter(call => call.path === "/api/chat").length;
  await restored.emit("click");
  assert.equal(calls.filter(call => call.path === "/api/chat").length, before);
});

test("network retry reuses ID; version conflict is not a busy error; new/delete conversation", async t => {
  const { $, calls, externalChange } = await setup(t);
  await send($, "network"); assert.equal($("retry").hidden, false);
  assert.doesNotMatch($("turns").textContent, /Traceback|private-key/);
  await $("retry").emit("click");
  const chats = calls.filter(c => c.path === "/api/chat");
  assert.equal(chats[0].body.request_id, chats[1].body.request_id);
  await $("add-set").emit("click");
  await $("set-results").querySelector("button").emit("click");
  const row = $("collection-rows").rows[0]; assert.equal(row.dataset.setNum, "A-1"); row.querySelector("input").value = "2";
  externalChange(); await $("collection-form").emit("submit");
  assert.match($("collection-message").textContent, /다시 불러오세요/);
  await $("reload-collection").emit("click");
  await $("collection-form").emit("submit");
  assert.match($("collection-message").textContent, /저장했습니다/);
  await $("new-conversation").emit("click");
  assert.match($("turns").textContent, /어떤 레고를 찾고 있나요/);
  assert.equal($("clear-conversation").disabled, true);
  await send($, "새 대화의 첫 질문");
  await $("clear-conversation").emit("click");
  assert.ok(calls.some(c => c.options.method === "DELETE"));
});

test("opening and resetting drafts creates no empty conversation; first submission creates one", async t => {
  const { $, calls, db, reload } = await setup(t);
  const creations = () => calls.filter(c => c.path === "/api/conversations" && c.options.method === "POST");
  assert.equal(creations().length, 0);
  await reload();
  await $("new-conversation").emit("click");
  await $("new-conversation").emit("click");
  assert.equal(creations().length, 0);
  await send($, "B-1");
  assert.equal(creations().length, 1);
  assert.equal(db.get("conv-1").state.subject.set_num, "B-1");
  await send($, "다음 질문");
  assert.equal(creations().length, 1);
  await $("new-conversation").emit("click"); await reload();
  assert.equal(creations().length, 1);
  assert.match($("turns").textContent, /어떤 레고를 찾고 있나요/);
});

test("missing key shows problem banner and disables chat and examples, leaves collection available", async t => {
  const { $ } = await setup(t, false);
  assert.equal($("question").disabled, true);
  assert.match($("chat-hint").textContent, /GMS_KEY/);
  assert.equal($("collection-fields").disabled, false);
  assert.equal($("status").hidden, false);
  assert.ok($("turns").querySelectorAll("button").every(button => button.disabled));
  await $("add-set").emit("click");
  await $("set-results").querySelector("button").emit("click");
  assert.equal($("collection-rows").rows.length, 1);
});

test("legacy selection opens the migrated conversation and discards tab/selection", async t => {
  const { calls, storage } = await setup(t, true, true);
  assert.ok(calls.some(c => c.path === "/api/threads/B-1/turns"));
  assert.deepEqual(JSON.parse(storage.get("lego-archive-state")), { uid: "f".repeat(32), conversationId: "conv-1" });
});

test("replaying a completed request cannot roll back a newer subject", async t => {
  const { $, db, calls } = await setup(t);
  await send($, "찾아줘");
  await send($, "disconnect");
  await $("turns").querySelectorAll(".result-card")[1].querySelector("button").emit("click");
  assert.equal($("subject-label").textContent, "지금 이야기 중: X-wing B");
  await $("retry").emit("click");
  assert.equal($("subject-label").textContent, "지금 이야기 중: X-wing B");
  assert.equal(db.get("conv-1").turns.length, 2);
  const requests = calls.filter(c => c.path === "/api/chat");
  assert.equal(requests[1].body.request_id, requests[2].body.request_id);
});
