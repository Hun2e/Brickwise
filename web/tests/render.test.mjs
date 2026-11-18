import test from "node:test";
import assert from "node:assert/strict";
import { renderAnswer, citedNumbers, dedupeStages, renderEvidence, renderTables, renderInfo, localTimestamp, renderSubject } from "../js/render.js";

test("conversation timestamp uses local date and zero-padded MM/DD HH:mm", () => {
  const local = new Date(2026, 0, 2, 3, 4);
  assert.equal(localTimestamp(local.toISOString()), "01/02 03:04");
  assert.equal(localTimestamp("invalid"), "");
});

// HTML 파싱 API를 제공하지 않는 작은 DOM 대역. 렌더러가 만든 요소를 직접 검사한다.
class Node {
  constructor(tag, text = "") { this.tag = tag; this.children = []; this.text = text; this.listeners = {}; this.attrs = {}; }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(child => child.textContent).join(""); }
  append(...nodes) { for (const node of nodes) this.children.push(...(node.tag === "#fragment" ? node.children : [node])); }
  addEventListener(name, listener) { this.listeners[name] = listener; }
  setAttribute(name, value) { this.attrs[name] = value; }
}
globalThis.document = {
  createElement: tag => new Node(tag), createTextNode: text => new Node("#text", text),
  createDocumentFragment: () => new Node("#fragment"),
};
const walk = node => [node, ...node.children.flatMap(walk)];
const tags = node => walk(node).map(item => item.tag);
const attack = '<script>globalThis.pwned = true</script><img src=x onerror="globalThis.pwned = true">';

test("citation numbers: adjacent, grouped, duplicate, missing and malformed", () => {
  assert.deepEqual(citedNumbers("[근거 1][근거 2] [근거 1, 3] [근거 7]"), [1, 2, 3, 7]);
  assert.deepEqual(citedNumbers("[근거 x] [근거 1,] 일반 문장"), []);
});

test("minimal markdown supports paragraphs, line breaks, bold and three lists", () => {
  const result = renderAnswer("첫 줄\n둘째 **굵게**\n\n- 하나\n* 둘\n\n3. 셋\n4. 넷\n\n# 제목은 글자 그대로");
  assert.deepEqual(result.children.map(node => node.tag), ["p", "ul", "ol", "p"]);
  assert.ok(tags(result).includes("strong")); assert.ok(tags(result).includes("br"));
  assert.equal(result.children[2].start, 3);
  assert.equal(result.children[3].textContent, "# 제목은 글자 그대로");
});

test("answer XSS remains text even inside bold and lists", () => {
  const result = renderAnswer(`${attack}\n\n**${attack}**\n- ${attack}`);
  assert.ok(result.textContent.includes(attack));
  assert.ok(!tags(result).some(tag => ["script", "img"].includes(tag)));
  assert.equal(globalThis.pwned, undefined);
});

test("citation buttons call the requested number including grouped bold citations", () => {
  const calls = [];
  const result = renderAnswer("**내용 [근거 1, 3]** [근거 2]", { onCite: n => calls.push(n) });
  const buttons = walk(result).filter(node => node.tag === "button");
  buttons.forEach(button => button.listeners.click({ stopPropagation() {} }));
  assert.deepEqual(calls, [1, 3, 2]);
  assert.ok(buttons.every(button => button.type === "button"));
});

test("only consecutive equal stages are deduplicated; document titles remain", () => {
  const stages = [{ stage: "분류", detail: null, index: 0 }, { stage: "분류", detail: null, index: 0 },
    { stage: "수집", detail: "A" }, { stage: "수집", detail: "B" }, { stage: "분류", detail: null }];
  assert.deepEqual(dedupeStages(stages), [stages[0], ...stages.slice(2)]);
  assert.equal(stages.length, 5);
});

function card(n, url = "https://example.com/source") {
  return { n, source_type: "db", title: attack, url, license: "CC BY-SA", text: attack };
}

test("evidence filters unused cards, treats content as text and secures external links", () => {
  const result = renderEvidence([card(1), card(2)], "답변 [근거 1]");
  assert.equal(result.children.length, 1);
  const link = walk(result).find(node => node.tag === "a");
  assert.equal(link.target, "_blank"); assert.equal(link.rel, "noopener noreferrer");
  assert.ok(result.textContent.includes(attack));
  assert.ok(!tags(result).includes("script")); assert.ok(!tags(result).includes("img"));
});

for (const url of ["javascript:alert(1)", "data:text/html,<script>alert(1)</script>", "/relative", "invalid"]) {
  test(`evidence rejects unsafe or invalid URL: ${url}`, () => {
    assert.ok(!tags(renderEvidence([card(1, url)], "[근거 1]")).includes("a"));
  });
}

test("no citation / nonexistent card has specified empty message", () => {
  for (const text of ["없음", "[근거 7]"]) assert.equal(renderEvidence([card(1)], text).textContent, "답변에 인용된 근거가 없습니다");
});

test("source badges use friendly labels while retaining the original type as title", () => {
  for (const [type, label] of [["db", "레고 데이터"], ["set_meta", "레고 데이터"], ["wikipedia", "위키백과"]]) {
    const result = renderEvidence([{ ...card(1), source_type: type }], "[근거 1]");
    const badge = walk(result).find(node => node.className === "badge");
    assert.equal(badge.textContent, label);
    assert.equal(badge.title, type);
  }
});

test("subject chip shortens names to 30 characters and only hides the -1 suffix", () => {
  assert.equal(renderSubject({ subject: "75301-1", subject_name: "X-wing" }), "지금 이야기 중: X-wing 75301");
  assert.equal(renderSubject({ subject: "123-2", subject_name: "가".repeat(40) }), `지금 이야기 중: ${"가".repeat(29)}… 123-2`);
  assert.equal(renderSubject({ subject: null }), "");
});

test("tables, warnings and processing info use safe DOM text", () => {
  const table = renderTables([{ title: attack, columns: [attack], rows: [[attack], [null], [{ private: "do not expose" }]] }]);
  assert.ok(table.textContent.includes(attack)); assert.ok(!tags(table).includes("script"));
  assert.ok(!table.textContent.includes("do not expose"));
  const info = renderInfo({ warnings: [attack, "경고 2"], route: "fact", timings: { total_ms: 123 } });
  assert.ok(info.textContent.includes("123 ms")); assert.ok(!tags(info).includes("script"));
  assert.equal(info.children.at(-1).tag, "details");
});
