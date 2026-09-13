/**
 * archivePosts 동작 테스트 — chrome API 를 스텁해서 노드에서 돌린다.
 *
 *   node extension/archivepost.test.mjs
 *
 * 잡으려는 것: "이미 받은 건 건너뛰고 안 받은 건 받는다".
 * 이게 성립해야 호출부가 미처리 전체를 넘겨도 안전하다.
 * (호출부를 newUnhandled 로 묶었던 게 "스캔했는데 안 떨어지네" 의 원인이었다)
 */
import assert from "node:assert/strict";

// ── chrome 스텁 ──────────────────────────────────────────
let store = {};
let downloads = [];
let downloadListeners = new Set();
let downloadsAvailable = true;
let autoComplete = true;
let interruptFilenameSuffix = null;

function emitDownloadState(id, state, error = null) {
  const item = downloads.find((entry) => entry.id === id);
  item.state = state;
  item.error = error;
  const delta = { id, state: { current: state } };
  if (error) delta.error = { current: error };
  for (const listener of downloadListeners) listener(delta);
}

function downloadApi() {
  return {
    download(opts, cb) {
      const id = downloads.length + 1;
      downloads.push({ id, opts, state: "in_progress", error: null });
      cb(id);
      if (autoComplete) queueMicrotask(() => {
        if (interruptFilenameSuffix && opts.filename.endsWith(interruptFilenameSuffix)) {
          emitDownloadState(id, "interrupted", "NETWORK_FAILED");
        } else {
          emitDownloadState(id, "complete");
        }
      });
    },
    search({ id }, cb) {
      cb(downloads.filter((entry) => entry.id === id));
    },
    onChanged: {
      addListener(listener) { downloadListeners.add(listener); },
      removeListener(listener) { downloadListeners.delete(listener); },
    },
  };
}

globalThis.chrome = {
  runtime: { lastError: undefined },
  storage: {
    local: {
      async get(key) { return key in store ? { [key]: store[key] } : {}; },
      async set(obj) { Object.assign(store, obj); },
    },
  },
  get downloads() {
    return downloadsAvailable ? downloadApi() : undefined;
  },
};

const { archivePosts, downloadAndWait, publishSourceObservation } = await import("./archiver.js");

const DETAIL = `
<section id="bo_v_file"><ul>
<li><a href="https://x/bbs/download.php?bo_table=yeonje&amp;wr_id=13411&amp;no=0" class="view_file_download">
<img><strong>신청서.hwpx</strong></a></li>
</ul></section>`;

const board = { id: "b1", name: "게시판 A" };
const post = (id) => ({ link: `https://x/bbs/board.php?bo_table=yeonje&wr_id=${id}`, title: `글 ${id}` });
const fetchHtml = async () => DETAIL;

function reset() {
  store = {};
  downloads = [];
  downloadListeners = new Set();
  downloadsAvailable = true;
  autoComplete = true;
  interruptFilenameSuffix = null;
}

function filenames() {
  return downloads.map((entry) => entry.opts.filename);
}

// ── 다운로드 ID 할당과 실제 완료는 다르다 ────────────────
reset();
autoComplete = false;
const pending = downloadAndWait("data:text/plain,ok", "board/test.txt", { timeoutMs: 1000 });
await new Promise((resolve) => setTimeout(resolve, 0));
let settled = false;
pending.finally(() => { settled = true; });
await Promise.resolve();
assert.equal(settled, false, "download ID allocation must not mean completion");
emitDownloadState(1, "complete");
assert.equal(await pending, 1);

reset();
autoComplete = false;
const interrupted = downloadAndWait("data:text/plain,bad", "board/bad.txt", { timeoutMs: 1000 });
await new Promise((resolve) => setTimeout(resolve, 0));
emitDownloadState(1, "interrupted", "NETWORK_FAILED");
await assert.rejects(interrupted, /NETWORK_FAILED/);

// ── 1. 처음이면 완전한 Inbox Bundle을 발행한다 ────────────
reset();
let r = await archivePosts(board, [post(1)], fetchHtml);
assert.equal(r.saved.length, 1, JSON.stringify(r));
const capture = r.saved[0];
assert.match(capture, /^yeonje-1-[0-9a-f]{8}$/);
const bundle = `upmuzadong-inbox/${capture}--글-1`;
assert.deepEqual(filenames(), [
  `${bundle}/page.html`,
  `${bundle}/attachments/신청서.hwpx`,
  `${bundle}/manifest.json`,
  `${bundle}/_READY`,
]);
assert.ok(downloads[1].opts.url.startsWith("https://x/bbs/download.php"));
assert.ok(downloads[2].opts.url.startsWith("data:application/json;charset=utf-8,"));
assert.ok(downloads[3].opts.url.startsWith("data:text/plain;charset=utf-8,"));

const manifest = JSON.parse(decodeURIComponent(downloads[2].opts.url.split(",", 2)[1]));
assert.equal(manifest.capture_id, capture);
assert.equal(manifest.source.id, "yeonje");
assert.equal(manifest.source.external_id, "1");
assert.equal(manifest.attachments[0].path, "attachments/신청서.hwpx");

const ledgerKey = "collector_delivery_v1_b1";
assert.equal(typeof store[ledgerKey]["1"], "string");
assert.equal(store[ledgerKey]["1"].length, 64);

// ── 2. 같은 HTML은 다시 받지 않는다 ───────────────────────
downloads = [];
r = await archivePosts(board, [post(1)], fetchHtml);
assert.deepEqual(r.saved, [], "같은 HTML을 또 받았다");
assert.deepEqual(r.skipped, ["1"]);
assert.equal(downloads.length, 0);

// ── 3. 같은 글의 HTML이 바뀌면 새 Bundle을 발행한다 ───────
const changedHtml = async () => DETAIL + "<p>changed</p>";
r = await archivePosts(board, [post(1)], changedHtml);
assert.equal(r.saved.length, 1);
assert.notEqual(r.saved[0], capture);
assert.equal(downloads.length, 4);

// ── 4. 첨부 다운로드가 끊기면 READY와 ledger가 없다 ───────
reset();
interruptFilenameSuffix = "/attachments/신청서.hwpx";
r = await archivePosts(board, [post(7)], fetchHtml);
assert.deepEqual(r.failed, [post(7).link]);
assert.equal(filenames().some((name) => name.endsWith("/_READY")), false);
assert.equal(store[ledgerKey]?.["7"], undefined);

// ── 5. wr_id 없는 링크는 실패, 나머지는 계속 ──────────────
reset();
r = await archivePosts(board, [{ link: "https://x/bbs/board.php?bo_table=yeonje" }, post(7)], fetchHtml);
assert.equal(r.failed.length, 1);
assert.equal(r.saved.length, 1, "한 건 실패했다고 나머지를 안 받으면 안 된다");

// ── 6. downloads 권한 없으면 저장됐다고 기록하지 않는다 ──
reset();
downloadsAvailable = false;
r = await archivePosts(board, [post(1)], fetchHtml);
assert.deepEqual(r.saved, []);
assert.equal(r.failed.length, 1);
assert.equal(store[ledgerKey]?.["1"], undefined);

// ── 7. 상세 fetch 실패는 그 글만 실패 ─────────────────────
reset();
const boom = async (url) => { if (url.includes("wr_id=5")) throw new Error("HTTP 500"); return DETAIL; };
r = await archivePosts(board, [post(5), post(6)], boom);
assert.equal(r.saved.length, 1);
assert.equal(r.failed.length, 1);

// ── 8. 게시판의 HTTP 오류 허용값을 상세 fetch에도 전달한다 ─
reset();
let detailAllowHttpError;
await archivePosts(
  { ...board, allowHttpError: true },
  [post(8)],
  async (_url, allowHttpError) => {
    detailAllowHttpError = allowHttpError;
    return DETAIL;
  }
);
assert.equal(detailAllowHttpError, true);

// ── 9. eGov 게시판 글도 Bundle 로 발행한다 ───────────────
reset();
const dnPage = (hit) => `<table class="tb_t2"><tbody class="tb read">
<tr><td class="subject" colspan="6">[수정] 민원</td></tr>
<tr><th scope="row"><span>작 성 자</span></th><td>홍길동</td>
<th scope="row"><span>조&nbsp;&nbsp;&nbsp;회</span></th><td>${hit}</td></tr>
<tr><th scope="row"><span>첨부파일</span></th><td colspan="5"><ul class="attach clearfix"><li>
<a href="/board/download.dongnae?boardId=BBS_0000275&dataSid=898320&fileSid=379374" title="신청서.hwpx 파일 다운로드">신청서.hwpx</a>
<span class="button"><a href="/board/download.dongnae?boardId=BBS_0000275&dataSid=898320&fileSid=379374" title="신청서.hwpx 파일 다운로드">다운받기</a></span>
</li></ul></td></tr></tbody></table>`;
const dnBoard = { id: "dn1", name: "eGov 게시판" };
const dnPost = { link: "https://www.dongnae.go.kr/board/view.dongnae?boardId=BBS_0000275&startPage=1&dataSid=898320" };
r = await archivePosts(dnBoard, [dnPost], async () => dnPage(3));
assert.equal(r.saved.length, 1, JSON.stringify(r));
assert.match(r.saved[0], /^bbs_0000275-898320-[0-9a-f]{8}$/, r.saved[0]);
const dnFiles = filenames();
assert.equal(dnFiles[1], `upmuzadong-inbox/${r.saved[0]}--untitled/attachments/신청서.hwpx`, dnFiles[1]);
// 첨부 URL 은 절대경로로 변환돼야 chrome.downloads 가 받는다
assert.equal(
  downloads[1].opts.url,
  "https://www.dongnae.go.kr/board/download.dongnae?boardId=BBS_0000275&dataSid=898320&fileSid=379374",
  downloads[1].opts.url
);
const dnManifest = JSON.parse(decodeURIComponent(downloads[2].opts.url.split(",", 2)[1]));
assert.equal(dnManifest.source.adapter, "dongnae");
assert.equal(dnManifest.source.id, "bbs_0000275");
assert.equal(dnManifest.source.external_id, "898320");

// ── 10. 조회수만 바뀐 eGov 페이지는 다시 안 받는다 ──────
downloads = [];
r = await archivePosts(dnBoard, [dnPost], async () => dnPage(9)); // 조회 3 → 9
assert.deepEqual(r.saved, [], "조회수만 늘었는데 새 Bundle 을 받았다");
assert.deepEqual(r.skipped, ["898320"]);
assert.equal(downloads.length, 0);

// 첨부(fileSid)가 바뀌면 다시 받는다
r = await archivePosts(dnBoard, [dnPost], async () =>
  dnPage(9).replace(/fileSid=379374/g, "fileSid=999999"));
assert.equal(r.saved.length, 1, "첨부 교체를 감지 못했다");

reset();
await publishSourceObservation({
  observationId: "yeonje-source_recovered-20260912t120000000z",
  kind: "source_recovered",
  observedAt: "2026-09-12T12:00:00.000Z",
  sourceId: "yeonje",
  sourceUrl: board.listUrl ?? post(1).link,
  priorErrorCode: "http_error",
});
assert.deepEqual(filenames(), [
  "upmuzadong-inbox/yeonje-source_recovered-20260912t120000000z--source-observation/source_observation.json",
  "upmuzadong-inbox/yeonje-source_recovered-20260912t120000000z--source-observation/_READY",
]);
const observationJson = JSON.parse(decodeURIComponent(downloads[0].opts.url.split(",", 2)[1]));
assert.equal(observationJson.kind, "source_recovered");
assert.equal(decodeURIComponent(downloads[1].opts.url.split(",", 2)[1]), "yeonje-source_recovered-20260912t120000000z\n");
assert.equal(downloads[0].opts.filename.split("/").length, 3, "observation bundle must be an inbox-root child");

console.log("OK  완료대기 · Ready-last Bundle · hash 멱등 · 부분실패 격리 · eGov 어댑터 · 조회수 무시");
