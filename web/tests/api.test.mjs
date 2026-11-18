import test from "node:test";
import assert from "node:assert/strict";
import { parseSSE, streamChat, api, safeMessage } from "../js/api.js";
import { saveState } from "../js/state.js";

test("SSE: all character boundaries, CRLF, comments and multiple events", () => {
  const input = ':keep-alive\r\n\r\nevent: stage\r\ndata: {"stage":"분류"}\r\n\r\nevent: result\ndata: {"id":1}\n\n';
  const expected = [{ event: "stage", data: '{"stage":"분류"}' }, { event: "result", data: '{"id":1}' }];
  for (let cut = 0; cut <= input.length; cut++) {
    const first = parseSSE(input.slice(0, cut), "");
    const second = parseSSE(input.slice(cut), first.buffer);
    assert.deepEqual([...first.events, ...second.events], expected);
    assert.equal(second.buffer, "");
  }
  let buffer = "", events = [];
  for (const char of input) { const parsed = parseSSE(char, buffer); buffer = parsed.buffer; events.push(...parsed.events); }
  assert.deepEqual(events, expected);
});

test("SSE: multiline data, unknown fields, default event and unfinished tail", () => {
  assert.deepEqual(parseSSE("id: 1\ndata:first\ndata: second\n\nevent: res"), {
    events: [{ event: "message", data: "first\nsecond" }], buffer: "event: res",
  });
  assert.deepEqual(parseSSE(":keep-alive\n\n"), { events: [], buffer: "" });
});

function streamed(text) {
  const bytes = new TextEncoder().encode(text);
  return new Response(new ReadableStream({ start(controller) {
    // 한국어 UTF-8 바이트 경계도 실제 스트림 디코더를 거친다.
    for (const byte of bytes) controller.enqueue(Uint8Array.of(byte));
    controller.close();
  } }), { headers: { "Content-Type": "text/event-stream; charset=utf-8" } });
}

async function runStream(t, response) {
  t.mock.method(globalThis, "fetch", async () => response);
  const events = [];
  await streamChat({ thread: "explore", question: "질문" }, {
    onStage: value => events.push(["stage", value]),
    onResult: value => events.push(["result", value]),
    onError: value => events.push(["error", value]),
  });
  return events;
}

test("stream: UTF-8 stage and result, exactly one terminal callback", async t => {
  const events = await runStream(t, streamed('event: stage\ndata: {"stage":"수집","detail":"문서"}\n\nevent: result\ndata: {"id":1}\n\nevent: error\ndata: {"code":"late","message":"late"}\n\n'));
  assert.deepEqual(events, [["stage", { stage: "수집", detail: "문서" }], ["result", { id: 1 }]]);
});

test("stream: error terminal is preserved and later result ignored", async t => {
  const events = await runStream(t, streamed('event: error\ndata: {"code":"chat_failed","message":"다시 시도하세요"}\n\nevent: result\ndata: {}\n\n'));
  assert.equal(events.length, 1); assert.equal(events[0][0], "error"); assert.equal(events[0][1].message, "다시 시도하세요");
});

for (const [name, body] of [["empty stream", ""], ["truncated result", "event: result\ndata: {}"], ["invalid JSON", "event: result\ndata: Traceback secret-key\n\n"]]) {
  test(`stream: ${name} ends with one safe error`, async t => {
    const events = await runStream(t, streamed(body));
    assert.equal(events.length, 1); assert.equal(events[0][0], "error");
    assert.doesNotMatch(events[0][1].message, /Traceback|secret-key|SyntaxError/);
  });
}

test("stream: wrong content type does not expose raw response", async t => {
  const events = await runStream(t, new Response("Traceback secret-key"));
  assert.equal(events[0][0], "error"); assert.doesNotMatch(events[0][1].message, /Traceback|secret-key/);
});

test("stream: 409 maps to the specified busy message", async t => {
  const events = await runStream(t, new Response('{"code":"busy","message":"busy"}', { status: 409 }));
  assert.equal(events[0][1].message, "이전 질문을 처리 중입니다");
});

test("API: user header and JSON request contract", async t => {
  saveState({ uid: "a".repeat(32) });
  t.mock.method(globalThis, "fetch", async (path, options) => {
    assert.equal(path, "/api/collection");
    assert.equal(options.headers.get("X-User-Id"), "a".repeat(32));
    assert.equal(options.headers.get("Content-Type"), "application/json");
    assert.deepEqual(JSON.parse(options.body), { sets: [["A-1", 2]] });
    return Response.json({ sets: [["A-1", 2]] });
  });
  assert.deepEqual(await api("/api/collection", { method: "PUT", body: JSON.stringify({ sets: [["A-1", 2]] }) }), { sets: [["A-1", 2]] });
});

test("API: ErrorOut messages are retained, unknown HTML/JSON errors hidden", async t => {
  const fetch = t.mock.method(globalThis, "fetch", async () => Response.json({ code: "invalid_request", message: "입력을 확인하세요" }, { status: 422 }));
  await assert.rejects(api("/api/chat"), { message: "입력을 확인하세요" });
  fetch.mock.mockImplementation(async () => new Response("Traceback secret-key", { status: 500 }));
  await assert.rejects(api("/api/chat"), error => !/Traceback|secret-key/.test(safeMessage(error)));
  fetch.mock.mockImplementation(async () => Response.json({ detail: "Traceback secret-key" }, { status: 500 }));
  await assert.rejects(api("/api/chat"), error => !/Traceback|secret-key/.test(safeMessage(error)));
});

test("API: network rejection and abort terminate safely", async t => {
  t.mock.method(globalThis, "fetch", async () => { throw new Error("Traceback secret-key"); });
  await assert.rejects(api("/api/status"), error => safeMessage(error).includes("네트워크"));
  const errors = [];
  await streamChat({}, { signal: AbortSignal.abort(), onError: error => errors.push(error) });
  assert.equal(errors.length, 1); assert.doesNotMatch(errors[0].message, /Traceback|secret-key/);
});

test("stream: midstream network failure ends once", async t => {
  const response = new Response(new ReadableStream({ pull(controller) { controller.error(new Error("secret-key")); } }), { headers: { "content-type": "text/event-stream" } });
  const events = await runStream(t, response);
  assert.equal(events.length, 1); assert.equal(events[0][0], "error");
  assert.doesNotMatch(events[0][1].message, /secret-key/);
});
