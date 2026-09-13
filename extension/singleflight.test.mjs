import assert from "node:assert/strict";
import { singleFlight } from "./singleflight.js";

let calls = 0;
let release;
const gate = new Promise((resolve) => { release = resolve; });
const run = singleFlight(async () => {
  calls += 1;
  await gate;
  return calls;
});

const first = run();
const second = run();
assert.equal(first, second, "overlapping calls must share one promise");
await Promise.resolve();
assert.equal(calls, 1);
release();
assert.deepEqual(await Promise.all([first, second]), [1, 1]);

assert.equal(await run(), 2, "a completed flight must allow the next scan");
console.log("OK  중복 스캔 단일 실행 · 완료 후 재실행");
