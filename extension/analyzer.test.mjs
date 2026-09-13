import assert from "node:assert/strict";
import { analyze } from "./analyzer.js";

const patterns = ["완료"];
const original = (id, title) => ({ link: `https://example.invalid/bbs/board.php?bo_table=alpha&wr_id=${id}`, title });

let result = analyze([original(1, "처리 요청")], patterns, {});
assert.equal(result.completedPosts.length, 0, "a disappeared reply must not imply completion");
assert.equal(result.unhandled.length, 1, "a current original remains unhandled without an explicit completion match");

result = analyze([
  original(1, "처리 요청"),
  original(2, "답변글 (완료)Re: 처리 요청"),
], patterns, {});
assert.deepEqual(result.completedPosts.map((post) => post.link), [original(1, "처리 요청").link]);
assert.equal(result.unhandled.length, 0);

result = analyze([
  { ...original(1, "처리 요청"), status: "처리중" },
  { ...original(2, "완료 요청"), status: "완료" },
], patterns, { statusSelector: ".status" });
assert.deepEqual(result.completedPosts.map((post) => post.link), [original(2, "완료 요청").link]);
assert.deepEqual(result.unhandled.map((post) => post.link), [original(1, "처리 요청").link]);

console.log("OK analyzer explicit completion matches only");
