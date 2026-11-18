import test from "node:test";
import assert from "node:assert/strict";
import { searchSets } from "../js/sets.js";
import { readState, saveState, legacyThread } from "../js/state.js";

const sets = [
  { set_num: "B-1", name: "Big Falcon", aliases: ["팔콘", "비교 별칭"], scope: "compare_only" },
  { set_num: "A-1", name: "Millennium Falcon", aliases: ["팔콘", "밀레니엄 팔콘"], scope: "core" },
  { set_num: "C-1", name: "X-wing", aliases: ["엑스윙"], scope: "core" },
];

test("set search ignores case/whitespace, matches every alias and sorts core first", () => {
  assert.deepEqual(searchSets(sets, " F A L C O N ").map(set => set.set_num), ["A-1", "B-1"]);
  assert.equal(searchSets(sets, "밀 레 니 엄팔콘")[0].set_num, "A-1");
  assert.equal(searchSets(sets, "비교별칭")[0].set_num, "B-1");
  assert.equal(searchSets(sets, "a - 1")[0].set_num, "A-1");
  assert.deepEqual(sets.map(set => set.set_num), ["B-1", "A-1", "C-1"]);
});

test("set search empty, zero result, limit and negative limit", () => {
  assert.deepEqual(searchSets(sets, "", 2).map(set => set.set_num), ["A-1", "C-1"]);
  assert.deepEqual(searchSets(sets, "없는 세트"), []);
  assert.deepEqual(searchSets(sets, "", -1), []);
  assert.deepEqual(searchSets([], ""), []);
});

test("state stores only UID/conversation, discards old selection and tab", async () => {
  const storage = new Map();
  globalThis.localStorage = { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value) };
  storage.set("lego-archive-state", JSON.stringify({ uid: "c".repeat(32), selectedSet: "A-1", activeTab: "archive" }));
  assert.deepEqual(readState(), { uid: "c".repeat(32), conversationId: null });
  assert.equal(legacyThread(), "A-1");
  saveState({ conversationId: "conv-1" });
  const reloaded = await import("../js/state.js?reload");
  assert.deepEqual(reloaded.readState(), { uid: "c".repeat(32), conversationId: "conv-1" });
  assert.deepEqual(JSON.parse(storage.get("lego-archive-state")), { uid: "c".repeat(32), conversationId: "conv-1" });
});
test("invalid storage values are normalized", () => {
  globalThis.localStorage = { getItem: () => JSON.stringify({ uid: "bad", conversationId: {} }) };
  assert.deepEqual(readState(), { uid: "", conversationId: null });
});
test("storage unavailable: retain in-memory state", () => {
  globalThis.localStorage = { getItem() { throw Error(); }, setItem() { throw Error(); } };
  saveState({ uid: "d".repeat(32), conversationId: "conv-2" });
  assert.equal(readState().conversationId, "conv-2");
  globalThis.localStorage = { getItem: () => "{bad" };
  assert.equal(readState().uid, "d".repeat(32));
});
