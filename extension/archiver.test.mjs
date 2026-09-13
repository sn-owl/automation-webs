/**
 * archiver 순수 함수 회귀 테스트.
 *
 *   node extension/archiver.test.mjs
 *
 * 크롬 API 를 안 쓰는 함수만 본다. 프레임워크 없음.
 */
import assert from "node:assert/strict";
import {
  buildManifest,
  buildSourceObservation,
  bundleDir,
  captureId,
  externalId,
  extractAttachments,
  safeName,
  sha256Hex,
  sourceId,
} from "./archiver.js";

// ── 글 ID (그누보드 wr_id) ───────────────────────────────
assert.equal(externalId("https://board.example.kr/bbs/board.php?bo_table=alpha&wr_id=13423"), "13423");
assert.equal(externalId("board.php?wr_id=13411&page=1"), "13411");
assert.equal(externalId("board.php?bo_table=alpha"), null);

// ── safeName ─────────────────────────────────────────────
assert.equal(safeName("../../etc/passwd"), "passwd");            // 경로 탈출 차단
assert.equal(safeName("C:\\Windows\\system32\\x.dll"), "x.dll"); // 윈도 절대경로도 basename
assert.equal(safeName("보고서: 최종본?.hwpx"), "보고서_ 최종본_.hwpx"); // 확장자 보존
assert.equal(safeName(""), "unnamed");
assert.ok(safeName("가".repeat(500)).length <= 120);

// ── Bundle 식별자 ────────────────────────────────────────
assert.equal(
  sourceId("https://x/bbs/board.php?bo_table=alpha&wr_id=13452"),
  "alpha"
);
assert.equal(sourceId("https://x/bbs/board.php?wr_id=13452"), null);

const digest = await sha256Hex("<html>same</html>");
assert.equal(digest.length, 64);
assert.equal(await sha256Hex("<html>same</html>"), digest);
assert.notEqual(await sha256Hex("<html>changed</html>"), digest);
assert.equal(captureId("alpha", "13452", digest), `alpha-13452-${digest.slice(0, 8)}`);
assert.equal(
  bundleDir("alpha-13452-a1b2c3d4", "무더위쉼터 현황: 엑셀/갱신"),
  "alpha-13452-a1b2c3d4--무더위쉼터-현황_-엑셀_갱신"
);

const manifest = buildManifest({
  captureId: `alpha-13452-${digest.slice(0, 8)}`,
  sourceId: "alpha",
  externalId: "13452",
  sourceUrl: "https://x/bbs/board.php?bo_table=alpha&wr_id=13452",
  pageHash: digest,
  capturedAt: "2026-09-01T01:30:00.000Z",
  attachments: [{ name: "request.hwpx", url: "https://x/download.php?no=0" }],
});
assert.equal(manifest.schema_version, 1);
assert.equal(manifest.source.adapter, "gnuboard");
assert.equal(manifest.page.path, "page.html");
assert.equal(manifest.attachments[0].path, "attachments/request.hwpx");
assert.equal(JSON.stringify(manifest).includes("cookie"), false);

// ── extractAttachments: 비식별 상세 HTML ─────────────────
const html = `
<section id="bo_v_file"><ul>
<li><a href="https://example.invalid/bbs/download.php?bo_table=alpha&amp;wr_id=13423&amp;no=0" class="view_file_download">
<img><strong>교육 안내문.pdf</strong></a></li>
</ul></section>`;
const atts = extractAttachments(html);

assert.equal(atts.length, 1, `첨부 1건이어야 하는데 ${atts.length}건`);
assert.equal(atts[0].name, "교육 안내문.pdf", atts[0].name);
assert.ok(atts[0].url.startsWith("https://example.invalid/bbs/download.php"), atts[0].url);
assert.ok(!atts[0].url.includes("&amp;"), "&amp; 가 안 풀렸다 — 그대로 요청하면 404");
assert.ok(atts[0].url.includes("wr_id=13423") && atts[0].url.includes("no=0"), atts[0].url);

// ── extractAttachments: 첨부 없는 문서 ───────────────────
assert.deepEqual(extractAttachments("<article>첨부 없음</article>"), []);

// ── <strong> 이 없는 첨부는 no= 값으로 대체 ──────────────
const noName = extractAttachments('<a href="/bbs/download.php?wr_id=1&amp;no=2" class="view_file_download">파일</a>');
assert.equal(noName.length, 1);
assert.equal(noName[0].name, "attach-2", noName[0].name);

// ── eGov 어댑터 (*.egov) ──────────────────────────────
const dnUrl = "https://www.egov.go.kr/board/view.egov?boardId=BBS_0000275&startPage=1&dataSid=898320";
assert.equal(externalId(dnUrl), "898320");
assert.equal(sourceId(dnUrl), "BBS_0000275");
assert.equal(externalId("https://www.egov.go.kr/board/view.egov?boardId=BBS_0000275"), null);

// 같은 fileSid 가 파일명 앵커 + "다운받기" 앵커로 두 번, href 는 상대경로
const dnHtml = `<ul class="attach clearfix"><li>
<a href="/board/download.egov?boardId=BBS_0000275&menuCd=null&dataSid=898320&fileSid=379374" title="웹사이트콘텐츠수정신청서(바가지요금통합신고창구).hwpx 파일 다운로드">웹사이트콘텐츠수정신청서(바가지요금통합신고창구).hwpx</a>
<span class="button icon_down"><a href="/board/download.egov?boardId=BBS_0000275&menuCd=null&dataSid=898320&fileSid=379374" title="웹사이트콘텐츠수정신청서(바가지요금통합신고창구).hwpx 파일 다운로드">다운받기</a></span>
</li></ul>`;
const dnAtts = extractAttachments(dnHtml, dnUrl);
assert.equal(dnAtts.length, 1, `fileSid 중복제거 실패 — ${dnAtts.length}건`);
assert.equal(dnAtts[0].name, "웹사이트콘텐츠수정신청서(바가지요금통합신고창구).hwpx", dnAtts[0].name);
assert.equal(
  dnAtts[0].url,
  "https://www.egov.go.kr/board/download.egov?boardId=BBS_0000275&menuCd=null&dataSid=898320&fileSid=379374",
  dnAtts[0].url
);

const dnManifest = buildManifest({
  captureId: captureId("BBS_0000275", "898320", digest),
  sourceId: "BBS_0000275",
  externalId: "898320",
  sourceUrl: dnUrl,
  pageHash: digest,
  capturedAt: "2026-09-01T01:30:00.000Z",
  attachments: [{ name: "x.hwpx", url: "https://www.egov.go.kr/board/download.egov?fileSid=1" }],
});
assert.equal(dnManifest.source.adapter, "egov");
assert.equal(dnManifest.source.id, "bbs_0000275");
assert.equal(dnManifest.capture_id, `bbs_0000275-898320-${digest.slice(0, 8)}`);

const observation = buildSourceObservation({
  observationId: "alpha-source_error-20260912t120000000z",
  kind: "source_error",
  observedAt: "2026-09-12T12:00:00.000Z",
  sourceId: "alpha",
  sourceUrl: "https://example.invalid/bbs/board.php?bo_table=alpha",
  code: "http_error",
  stage: "fetch",
  retryable: true,
  nextAction: "retry next poll",
});
assert.deepEqual(Object.keys(observation).sort(), ["code", "kind", "next_action", "observation_id", "observed_at", "retryable", "schema_version", "source", "stage"].sort());
assert.deepEqual(Object.keys(observation.source).sort(), ["adapter", "id", "type"]);
assert.equal(JSON.stringify(observation).includes("example.invalid"), false);
assert.equal(JSON.stringify(observation).includes("secret"), false);
assert.throws(
  () => buildSourceObservation({
    observationId: "alpha-source_error-invalid-retryable",
    kind: "source_error",
    observedAt: "2026-09-12T12:00:00.000Z",
    sourceId: "alpha",
    sourceUrl: "https://example.invalid/bbs/board.php?bo_table=alpha",
    code: "http_error",
    stage: "fetch",
    retryable: "true",
    nextAction: "retry next poll",
  }),
  /invalid error details/,
);
console.log(`OK  첨부 추출 ${atts.length}건 · 관찰 번들 allow-list`);
