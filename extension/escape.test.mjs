/**
 * 텔레그램 MarkdownV2 이스케이프 회귀 테스트.
 *
 *   node extension/escape.test.mjs
 *
 * 실제로 터진 버그: 인라인 링크의 URL 에 escMd 를 먹여서
 * "can't parse entities" 로 알림이 통째로 실패했다.
 */
import assert from "node:assert/strict";
import { escMd, escUrl } from "./escape.js";

const URL_ = "https://cug.thewebs.kr/bbs/board.php?bo_table=yeonje&wr_id=13423";

// ── escUrl: URL 을 망가뜨리지 않는다 ─────────────────────
assert.equal(escUrl(URL_), URL_, "URL 이 변형됐다 — 텔레그램이 거절한다");
assert.ok(!escUrl(URL_).includes("\\"), escUrl(URL_));

// 그러나 링크를 깨는 두 문자는 반드시 막는다
assert.equal(escUrl("http://x/a)b"), "http://x/a\\)b");
assert.equal(escUrl("http://x/a\\b"), "http://x/a\\\\b");
assert.equal(escUrl(null), "");
assert.equal(escUrl(undefined), "");

// ── 이게 버그였다: escMd 를 URL 에 쓰면 안 된다 ──────────
assert.notEqual(escMd(URL_), URL_, "전제가 깨졌다 — escMd 는 URL 을 변형해야 정상");
assert.ok(escMd(URL_).includes("cug\\.thewebs"), "escMd 가 점을 이스케이프한다");

// ── escMd: 본문 텍스트에는 여전히 필요하다 ───────────────
assert.equal(escMd("무더위쉼터 현황(2026.5.기준)"), "무더위쉼터 현황\\(2026\\.5\\.기준\\)");
assert.equal(escMd("2026년 3분기 학부모 정신건강증진교육 안내문.pdf"),
             "2026년 3분기 학부모 정신건강증진교육 안내문\\.pdf");
assert.equal(escMd("(완료)Re: 도시공원"), "\\(완료\\)Re: 도시공원");
assert.equal(escMd(""), "");
assert.equal(escMd(null), "");

// 실물 제목들이 예약문자를 실제로 물고 있다 (그래서 이스케이프가 필요했다)
for (const t of ["홈페이지 메뉴 추가(수정)신청서", "[완료] 처리됨", "무더위쉼터 현황(2026.5.기준)"]) {
  assert.notEqual(escMd(t), t, `예약문자가 있는데 그대로다: ${t}`);
}

// 반대로 예약문자가 없으면 건드리지 않는다 (⇒ 는 MarkdownV2 예약문자가 아니다)
assert.equal(escMd("⇒ 문구 삭제"), "⇒ 문구 삭제");
assert.equal(escMd("무더위쉼터 현황"), "무더위쉼터 현황");

console.log("OK  URL 원형 보존 · 링크깨짐 문자 차단 · 본문 이스케이프");
