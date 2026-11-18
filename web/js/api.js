import { readState } from "./state.js";

export class ApiError extends Error {
  constructor(code, message) { super(message); this.code = code; }
}
const NETWORK = "서버에 연결하지 못했습니다. 네트워크와 서버 실행 상태를 확인한 뒤 다시 시도하세요.";
const PROTOCOL = "응답을 읽지 못했습니다. 대화 기록을 다시 불러온 뒤 재시도하세요.";

export function safeMessage(error) {
  return error instanceof ApiError ? error.message : NETWORK;
}

function errorOut(data, status) {
  if (data?.code === "busy") return new ApiError("busy", "이전 질문을 처리 중입니다");
  return data && typeof data.code === "string" && typeof data.message === "string"
    ? new ApiError(data.code, data.message) : new ApiError("protocol", PROTOCOL);
}

async function request(path, options = {}) {
  const headers = new Headers(options.headers);
  const uid = readState().uid;
  if (uid) headers.set("X-User-Id", uid);
  if (options.body !== undefined) headers.set("Content-Type", "application/json");
  let response;
  try { response = await fetch(path, { ...options, headers }); }
  catch { throw new ApiError("network", NETWORK); }
  if (!response.ok) {
    let data;
    try { data = await response.json(); } catch { /* 원시 오류 본문은 표시하지 않는다. */ }
    throw errorOut(data, response.status);
  }
  return response;
}

export async function api(path, options = {}) {
  const response = await request(path, options);
  try { return await response.json(); }
  catch { throw new ApiError("protocol", PROTOCOL); }
}

export const conversations = () => api("/api/conversations");
export const createConversation = () => api("/api/conversations", { method: "POST" });
export const conversation = id => api(`/api/conversations/${encodeURIComponent(id)}`);
export const changeSubject = (id, set_num, version) => api(`/api/conversations/${encodeURIComponent(id)}/subject`,
  { method: "PATCH", body: JSON.stringify({ set_num, version }) });
export const setHistory = number => api(`/api/sets/${encodeURIComponent(number)}/history`);
export const chatRequest = (conversation_id, question) => ({ conversation_id, question, request_id: crypto.randomUUID() });

/** 완성된 빈 줄까지 소비한다. CRLF의 CR이 청크 끝에 있어도 보존한다. */
export function parseSSE(chunkText, buffer = "") {
  let pending = buffer + chunkText;
  const events = [];
  const separator = /\r\n\r\n|\r\n\n|\n\r\n|\n\n/;
  let match;
  while ((match = separator.exec(pending))) {
    const block = pending.slice(0, match.index);
    pending = pending.slice(match.index + match[0].length);
    let event = "message";
    const data = [];
    for (const line of block.split(/\r\n|\n/)) {
      if (line.startsWith(":")) continue;
      const colon = line.indexOf(":");
      const field = colon < 0 ? line : line.slice(0, colon);
      const value = colon < 0 ? "" : line.slice(colon + 1).replace(/^ /, "");
      if (field === "event") event = value;
      if (field === "data") data.push(value);
    }
    if (data.length) events.push({ event, data: data.join("\n") });
  }
  return { events, buffer: pending };
}

/** result/error 중 하나만 전달한다. 중도 종료도 안전한 오류로 마무리한다. */
export async function streamChat(body, { onStage, onResult, onError, signal } = {}) {
  let terminal = false;
  let reader;
  const finish = (kind, data) => {
    if (terminal) return;
    terminal = true;
    if (kind === "result") onResult?.(data);
    else onError?.(data);
  };
  try {
    const response = await request("/api/chat", { method: "POST", body: JSON.stringify(body), signal });
    if (!response.headers.get("content-type")?.includes("text/event-stream") || !response.body) {
      throw new ApiError("protocol", PROTOCOL);
    }
    reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (!terminal) {
      const { value, done } = await reader.read();
      const parsed = parseSSE(done ? decoder.decode() : decoder.decode(value, { stream: true }), buffer);
      buffer = parsed.buffer;
      for (const event of parsed.events) {
        if (terminal) break;
        if (!["stage", "result", "error"].includes(event.event)) continue;
        const data = JSON.parse(event.data);
        if (event.event === "stage") onStage?.(data);
        if (event.event === "result") finish("result", data);
        if (event.event === "error") finish("error", errorOut(data));
      }
      if (done && !terminal) throw new ApiError("protocol", PROTOCOL);
    }
  } catch (error) {
    finish("error", error instanceof ApiError ? error : new ApiError("protocol", PROTOCOL));
  } finally {
    if (reader) { try { await reader.cancel(); } catch { /* 연결 종료 오류 무시 */ } reader.releaseLock(); }
  }
}
