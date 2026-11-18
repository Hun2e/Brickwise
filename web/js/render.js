const CITATION = /\[근거\s+(\d+(?:\s*,\s*\d+)*)\]/g;

export function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

export function citedNumbers(text) {
  return [...new Set([...String(text).matchAll(CITATION)].flatMap(match =>
    match[1].split(",").map(Number)))];
}

export function dedupeStages(stages) {
  return stages.filter((stage, index) => !index || stage.stage !== stages[index - 1].stage ||
    (stage.detail || "") !== (stages[index - 1].detail || ""));
}

function inline(parent, text, onCite) {
  const tokens = /(\*\*[^*\n]+\*\*|\[근거\s+\d+(?:\s*,\s*\d+)*\])/g;
  let start = 0;
  for (const match of text.matchAll(tokens)) {
    parent.append(document.createTextNode(text.slice(start, match.index)));
    if (match[0].startsWith("**")) {
      const strong = el("strong");
      inline(strong, match[0].slice(2, -2), onCite);
      parent.append(strong);
    } else {
      for (const n of citedNumbers(match[0])) {
        const button = el("button", `[근거 ${n}]`, "cite");
        button.type = "button";
        button.addEventListener("click", event => { event.stopPropagation(); onCite?.(n); });
        parent.append(button);
      }
    }
    start = match.index + match[0].length;
  }
  parent.append(document.createTextNode(text.slice(start)));
}

export function renderAnswer(text, { onCite } = {}) {
  const fragment = document.createDocumentFragment();
  let block = null;
  let kind = "";
  for (const line of String(text).replace(/\r\n/g, "\n").split("\n")) {
    if (!line.trim()) { block = null; kind = ""; continue; }
    const list = /^\s*(?:([-*]) |(\d+)\. )(.*)$/.exec(line);
    const nextKind = list ? (list[1] ? "ul" : "ol") : "p";
    if (!block || kind !== nextKind) {
      kind = nextKind;
      block = el(kind);
      if (kind === "ol") block.start = Number(list[2]);
      fragment.append(block);
    } else if (kind === "p") block.append(el("br"));
    const target = list ? el("li") : block;
    if (list) block.append(target);
    inline(target, list ? list[3] : line, onCite);
  }
  return fragment;
}

export function renderEvidence(cards, text, onCard) {
  const fragment = document.createDocumentFragment();
  const numbers = citedNumbers(text);
  const cited = cards.filter(card => numbers.includes(card.n));
  if (!cited.length) fragment.append(el("p", "답변에 인용된 근거가 없습니다", "muted"));
  for (const card of cited) {
    const details = el("details", undefined, "evidence-card");
    details.open = true;
    const summary = el("summary");
    const badge = el("span", ({ set_meta: "레고 데이터", db: "레고 데이터", wikipedia: "위키백과", brickipedia: "브릭키피디아" })[card.source_type] || card.source_type, "badge");
    badge.title = card.source_type;
    summary.append(el("span", `[근거 ${card.n}] `), badge, el("span", ` ${card.title}`));
    details.append(summary);
    try {
      const url = new URL(card.url);
      if (["https:", "http:"].includes(url.protocol)) {
        const link = el("a", "원문 보기 ↗");
        link.href = url.href; link.target = "_blank"; link.rel = "noopener noreferrer";
        details.append(link);
      }
    } catch { /* 올바르지 않은 출처 URL은 링크로 만들지 않는다. */ }
    details.append(el("p", `라이선스: ${card.license}`, "muted"), el("p", card.text, "excerpt"));
    onCard?.(card.n, details);
    fragment.append(details);
  }
  return fragment;
}

export function renderTables(tables) {
  const fragment = document.createDocumentFragment();
  for (const data of tables) {
    const wrap = el("div", undefined, "table-scroll");
    wrap.tabIndex = 0;
    wrap.setAttribute("role", "region"); wrap.setAttribute("aria-label", data.title);
    const table = el("table"); table.append(el("caption", data.title));
    const head = el("thead"), row = el("tr");
    data.columns.forEach(column => { const cell = el("th", column); cell.scope = "col"; row.append(cell); });
    head.append(row); table.append(head);
    const body = el("tbody");
    data.rows.forEach(values => {
      const tr = el("tr");
      values.forEach(value => tr.append(el("td", value === null ? "—" :
        typeof value === "object" ? "—" : String(value))));
      body.append(tr);
    });
    table.append(body); wrap.append(table); fragment.append(wrap);
  }
  return fragment;
}

export function renderInfo(answer, stages = []) {
  const fragment = document.createDocumentFragment();
  const details = el("details", undefined, "processing");
  details.append(el("summary", "처리 과정"));
  const steps = el("ol");
  for (const stage of dedupeStages(stages)) steps.append(el("li", stage.stage + (stage.detail ? `: ${stage.detail}` : "")));
  if (stages.length) details.append(steps);
  else details.append(el("p", "이전 답변의 단계 기록은 제공되지 않습니다."));
  details.append(el("p", `경로: ${answer.route} · 소요 시간: ${answer.timings.total_ms ?? 0} ms`));
  fragment.append(details);
  return fragment;
}

export function renderItems(items, { onSubject, onRegister, isRegistered = () => false } = {}) {
  const fragment = document.createDocumentFragment();
  for (const item of items) {
    const card = el("section", undefined, "result-card");
    card.append(el("span", item.set_num.replace(/-1$/, ""), "badge"), el("h3", item.name),
      el("p", `${item.year}년 · ${item.num_parts}개 부품`, "muted"));
    if (item.missing != null) card.append(el("p", `부품 ${item.missing}개 더 필요`, "badge"));
    const actions = el("div", undefined, "actions");
    for (const [label, handler] of [["자세히 물어보기", onSubject], ["갖고 있어요", onRegister]]) {
      const button = el("button", label); button.type = "button"; button.dataset.navigation = "true";
      if (label === "갖고 있어요") {
        button.dataset.chatAction = "true";
        button.dataset.registerSet = item.set_num;
        if (isRegistered(item)) { button.textContent = "✓ 가지고 있어요"; button.dataset.registered = "true"; button.disabled = true; }
      }
      button.addEventListener("click", () => handler?.(item)); actions.append(button);
    }
    card.append(actions); fragment.append(card);
  }
  return fragment;
}

export function renderSubject(summary) {
  if (!summary?.subject) return "";
  const chars = [...(summary.subject_name || "")];
  const name = chars.length > 30 ? `${chars.slice(0, 29).join("")}…` : chars.join("");
  return `지금 이야기 중: ${name} ${summary.subject.replace(/-1$/, "")}`;
}

export function renderHistory(turns) {
  const fragment = document.createDocumentFragment();
  if (!turns.length) fragment.append(el("p", "이 세트의 대화 기록이 없습니다.", "muted"));
  for (const turn of turns) {
    const entry = el("details"); entry.append(el("summary", turn.question));
    if (turn.comparison_label) entry.append(el("span", turn.comparison_label, "badge"));
    entry.append(el("p", turn.answer.answer), el("p", localTimestamp(turn.created_at), "muted")); fragment.append(entry);
  }
  return fragment;
}

export function renderPending(pending, onReply) {
  const fragment = document.createDocumentFragment();
  if (pending?.kind === "quantity") {
    const quantity = el("input"); quantity.type = "number"; quantity.min = "1"; quantity.step = "1";
    quantity.value = "1"; quantity.setAttribute("aria-label", "보유 수량");
    const button = el("button", "수량 보내기"); button.type = "button"; button.dataset.navigation = "true";
    button.dataset.chatAction = "true";
    button.addEventListener("click", () => {
      const value = Number(quantity.value);
      if (Number.isSafeInteger(value) && value >= 1) return onReply(`${value}개`);
    });
    fragment.append(quantity, button);
  }
  const choices = pending?.kind === "confirm" ? [["네", "응"], ["아니요", "아니"]] :
    pending?.kind === "target" ? pending.options.map(n => [n, n]) : [];
  for (const [label, value] of choices) {
    const button = el("button", label); button.type = "button"; button.dataset.navigation = "true";
    button.dataset.chatAction = "true";
    button.addEventListener("click", () => onReply(value)); fragment.append(button);
  }
  return fragment;
}
export function localTimestamp(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const pad = number => String(number).padStart(2, "0");
  return `${pad(date.getMonth() + 1)}/${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}
