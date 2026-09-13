/**
 * archiver.js
 *
 * 신규 미처리 글의 상세 페이지와 첨부를 완전한 Inbox Bundle로 저장한다.
 * Python Core는 `_READY`가 있는 Bundle만 읽는다.
 *
 * 쿠키를 Python에 넘기지 않는다. 인증된 HTML과 첨부 다운로드는 Chrome이 맡고,
 * 업무 파싱과 상태 관리는 Core가 맡는다.
 *
 * 저장 위치 (Downloads 상대경로)
 *   upmuzadong-inbox/{captureId}/page.html
 *   upmuzadong-inbox/{captureId}/attachments/{원본파일명}
 *   upmuzadong-inbox/{captureId}/manifest.json
 *   upmuzadong-inbox/{captureId}/_READY
 */

const ROOT = "upmuzadong-inbox";
export const READY_MARKER = "_READY";
const KEEP = 300; // 아카이브 기록 보관 개수

// ── 순수 함수 (테스트 대상) ──────────────────────────────

/** 상세 URL 하나로 게시판 유형을 가른다.
 *  그누보드(board.php?bo_table&wr_id) vs eGov(*.egov?boardId&dataSid).
 *  eGov 판별은 호스트 접미사로 한다. 실제 사이트에 붙일 때는 아래 패턴을 그
 *  사이트의 도메인으로 바꾼다 — 여기 값은 특정 기관에 묶이지 않은 예시다. */
function adapterFor(url) {
  return /\.egov\b/.test(String(url)) ? "egov" : "gnuboard";
}

/** 재다운로드 판단(해시 비교)에서 휘발성 요소를 지운다. page.html 원본은 그대로 저장하고
 *  이 정규화본은 delivery ledger 비교에만 쓴다.
 *
 *  eGov 상세는 조회수가 HTML 안에 있어서 fetch 할 때마다 +1 된다 —
 *  그대로 해시하면 스캔마다 새 Bundle 이 만들어진다.
 *  note: 조회수 셀만 정규식으로 제거. 마크업이 바뀌면 불필요한 재수집이 늘 뿐 오작동은 아님. */
export function deliveryContent(html, url) {
  if (adapterFor(url) !== "egov") return html;
  return String(html).replace(
    /<th[^>]*>\s*<span>\s*조(?:&nbsp;|&#160;|\s)*회\s*<\/span>\s*<\/th>\s*<td>\s*\d+\s*<\/td>/gi,
    ""
  );
}

/** 상세 URL 에서 글 ID. 목록의 표시번호가 아니라 진짜 ID.
 *  그누보드 wr_id / eGov dataSid. */
export function externalId(url) {
  const u = String(url);
  const m = adapterFor(u) === "egov"
    ? u.match(/[?&]dataSid=(\d+)/)
    : u.match(/[?&]wr_id=(\d+)/);
  return (m || [])[1] ?? null;
}

/** 상세 URL 에서 Core source id. 그누보드 bo_table / eGov boardId. */
export function sourceId(url) {
  try {
    const params = new URL(String(url), "https://invalid.local").searchParams;
    return params.get(adapterFor(url) === "egov" ? "boardId" : "bo_table");
  } catch {
    return null;
  }
}

/** 상세 HTML 에서 첨부 [{url, name}] 를 뽑는다. baseUrl 로 어댑터를 고른다.
 *  DOMParser 를 안 쓴다 — 서비스워커에 없고, 두 마크업 다 실물로 고정 확인됐다. */
export function extractAttachments(html, baseUrl) {
  return adapterFor(baseUrl) === "egov"
    ? egovAttachments(html, baseUrl)
    : gnuboardAttachments(html);
}

/**   <a href="...download.php?...&amp;no=0" class="view_file_download">
 *      <img ...><strong>원본파일명.pdf</strong>                          */
function gnuboardAttachments(html) {
  const out = [];
  const re = /<a\s[^>]*href="([^"]*download\.php[^"]*)"[^>]*>/gi;
  let m;
  while ((m = re.exec(html)) !== null) {
    // 링크 직후 500자 안의 첫 <strong> 이 원본 파일명. 없으면 no= 값으로 대체
    const tail = html.slice(m.index + m[0].length, m.index + m[0].length + 500);
    const strong = tail.match(/<strong>([\s\S]*?)<\/strong>/i);
    const url = unescapeHtml(m[1]);
    const no = (url.match(/[?&]no=(\d+)/) || [])[1] ?? out.length;
    out.push({
      url,
      name: safeName(strong ? stripTags(strong[1]) : `attach-${no}`),
    });
  }
  return out;
}

/** eGov: <ul class="attach"> 안 download.egov 링크. 같은 fileSid 가
 *  파일명 앵커와 "다운받기" 앵커로 두 번 나와서 fileSid 로 중복 제거한다.
 *  href 가 상대경로라 baseUrl 로 절대화한다. 파일명은 title 속성에서 뽑는다:
 *  title="원본파일명.hwpx 파일 다운로드" */
function egovAttachments(html, baseUrl) {
  const out = [];
  const seen = new Set();
  const re = /<a\s[^>]*href="([^"]*download\.egov[^"]*)"[^>]*>/gi;
  let m;
  while ((m = re.exec(html)) !== null) {
    let url = unescapeHtml(m[1]);
    try { url = new URL(url, baseUrl).href; } catch { /* baseUrl 없으면 상대경로 그대로 */ }
    const fileSid = (url.match(/[?&]fileSid=(\d+)/) || [])[1] ?? url;
    if (seen.has(fileSid)) continue;
    seen.add(fileSid);
    const title = (m[0].match(/title="([^"]*)"/i) || [])[1] ?? "";
    const name = title.replace(/\s*파일\s*다운로드\s*$/, "").trim();
    out.push({ url, name: safeName(name || `attach-${fileSid}`) });
  }
  return out;
}

export async function sha256Hex(text) {
  const bytes = new TextEncoder().encode(text);
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
  let hex = "";
  for (const byte of digest) hex += byte.toString(16).padStart(2, "0");
  return hex;
}

function safeIdentifier(value, name) {
  const normalized = String(value).trim().toLowerCase();
  if (!/^[a-z0-9][a-z0-9._-]*$/.test(normalized)) {
    throw new Error(`unsafe ${name}: ${value}`);
  }
  return normalized;
}

export function captureId(source, external, htmlHash) {
  const sourceKey = safeIdentifier(source, "source id");
  const externalKey = safeIdentifier(external, "external id");
  if (!/^[0-9a-f]{64}$/.test(htmlHash)) throw new Error("invalid page hash");
  return `${sourceKey}-${externalKey}-${htmlHash.slice(0, 8)}`;
}
export function bundleDir(id, title) {
  const label = safeName(String(title || "untitled").trim().replace(/[\\/]/g, "_").replace(/\s+/g, "-"))
    .replace(/-+/g, "-")
    .slice(0, 80) || "untitled";
  return `${id}--${label}`;
}

export function buildManifest({
  captureId: id,
  sourceId: source,
  externalId,
  sourceUrl,
  pageHash,
  capturedAt,
  attachments,
}) {
  const sourceKey = safeIdentifier(source, "source id");
  const externalKey = safeIdentifier(externalId, "external id");
  if (id !== captureId(sourceKey, externalKey, pageHash)) {
    throw new Error("capture id does not match source and page hash");
  }

  return {
    schema_version: 1,
    capture_id: id,
    source: {
      type: "board",
      id: sourceKey,
      adapter: adapterFor(sourceUrl),
      external_id: externalKey,
      url: sourceUrl,
    },
    captured_at: capturedAt,
    page: { path: "page.html", sha256: pageHash },
    attachments: attachments.map(({ name, url }) => {
      const filename = safeName(name);
      if (filename !== name) throw new Error(`unsafe attachment name: ${name}`);
      return {
        name: filename,
        path: `attachments/${filename}`,
        source_url: url,
      };
    }),
  };
}

function unescapeHtml(s) {
  return s
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#0?39;/g, "'");
}

function stripTags(s) {
  return unescapeHtml(s.replace(/<[^>]*>/g, "")).trim();
}

/** chrome.downloads 가 거부하는 문자·경로 탈출 제거. 확장자는 살린다.
 *
 * 경로 구분자로 먼저 잘라 basename 만 남긴다 — 문자 치환만 하면
 * `../../etc/passwd` 가 `_._etc_passwd` 같은 쓰레기로 남는다. 안전하지만 더럽다.
 */
export function safeName(name) {
  const base = String(name).split(/[\\/]/).pop();
  const cleaned = base
    .replace(/[:*?"<>|\r\n\t]/g, "_")
    .replace(/\.{2,}/g, ".")
    .replace(/^[.\s]+|[.\s]+$/g, "")
    .slice(0, 120);
  return cleaned || "unnamed";
}

const OBSERVATION_ROOT = ROOT;

function isoTimestamp(value, name) {
  const text = value instanceof Date ? value.toISOString() : String(value);
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$/.test(text) || Number.isNaN(Date.parse(text))) {
    throw new Error(`invalid ${name}`);
  }
  return text;
}

export function buildSourceObservation({
  observationId, kind, observedAt, sourceId: source, sourceUrl,
  externalId, code, stage, retryable, lastSuccessAt, nextAction, priorErrorCode,
}) {
  const id = safeIdentifier(observationId, "observation id");
  const sourceKey = safeIdentifier(source, "source id");
  if (!["source_completion_observed", "source_error", "source_recovered"].includes(kind)) {
    throw new Error("invalid observation kind");
  }
  const result = {
    schema_version: 1,
    observation_id: id,
    kind,
    observed_at: isoTimestamp(observedAt, "observed_at"),
    source: { type: "board", id: sourceKey, adapter: adapterFor(sourceUrl) },
  };
  if (kind === "source_completion_observed") {
    result.external_id = safeIdentifier(externalId, "external id");
  } else if (kind === "source_error") {
    if (!/^[a-z][a-z0-9_]*$/.test(String(code)) || !/^[a-z][a-z0-9_]*$/.test(String(stage)) || typeof retryable !== "boolean") throw new Error("invalid error details");
    result.code = String(code);
    result.stage = String(stage);
    result.retryable = retryable;
    if (lastSuccessAt != null) result.last_success_at = isoTimestamp(lastSuccessAt, "last_success_at");
    result.next_action = safeName(String(nextAction || "retry next poll")).slice(0, 160);
  } else {
    if (!/^[a-z][a-z0-9_]*$/.test(String(priorErrorCode))) throw new Error("invalid prior error code");
    result.prior_error_code = String(priorErrorCode);
  }
  return result;
}

export async function publishSourceObservation(observation) {
  const payload = buildSourceObservation(observation);
  const dir = `${OBSERVATION_ROOT}/${payload.observation_id}--source-observation`;
  await download(
    "data:application/json;charset=utf-8," + encodeURIComponent(JSON.stringify(payload) + "\n"),
    `${dir}/source_observation.json`
  );
  await download(
    "data:text/plain;charset=utf-8," + encodeURIComponent(payload.observation_id + "\n"),
    `${dir}/${READY_MARKER}`
  );
  return payload.observation_id;
}

// ── 부수효과 (크롬 API) ──────────────────────────────────

/** 다운로드 ID가 아니라 실제 파일 완료까지 기다린다. */
export function downloadAndWait(url, filename, { timeoutMs = 120_000 } = {}) {
  const api = chrome.downloads;
  if (!api) {
    throw new Error("chrome.downloads 없음 — chrome://extensions 에서 확장 새로고침 필요");
  }

  return new Promise((resolve, reject) => {
    let downloadId;
    let timer;
    let settled = false;

    const finish = (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      api.onChanged.removeListener(onChanged);
      if (error) reject(error);
      else resolve(downloadId);
    };

    const onChanged = (delta) => {
      if (delta.id !== downloadId) return;
      if (delta.state?.current === "complete") finish();
      if (delta.state?.current === "interrupted") {
        finish(new Error(`${filename}: ${delta.error?.current ?? "다운로드 중단"}`));
      }
    };

    api.onChanged.addListener(onChanged);
    timer = setTimeout(
      () => finish(new Error(`${filename}: 다운로드 ${timeoutMs}ms 초과`)),
      timeoutMs
    );

    api.download(
      { url, filename, conflictAction: "overwrite", saveAs: false },
      (id) => {
        const err = chrome.runtime.lastError;
        if (err || id === undefined) {
          finish(new Error(`${filename}: ${err?.message ?? "id 없음"}`));
          return;
        }

        downloadId = id;
        api.search({ id }, (items) => {
          const searchError = chrome.runtime.lastError;
          if (searchError) {
            finish(new Error(`${filename}: ${searchError.message}`));
            return;
          }
          const item = items[0];
          if (item?.state === "complete") finish();
          if (item?.state === "interrupted") {
            finish(new Error(`${filename}: ${item.error ?? "다운로드 중단"}`));
          }
        });
      }
    );
  });
}

function download(url, filename) {
  return downloadAndWait(url, filename);
}

/**
 * Publish the completion marker only after every payload (including the
 * manifest) has reached Chrome's `complete` state.  `downloadAndWait` does
 * not resolve on ID allocation, so this remains the sole publication edge.
 */
async function publishReadyMarker(captureId, dir) {
  await download(
    "data:text/plain;charset=utf-8," + encodeURIComponent(captureId + "\n"),
    `${dir}/${READY_MARKER}`
  );
}

/**
 * 신규 미처리 글들을 Inbox Bundle로 발행한다.
 *
 * @param board       게시판 설정
 * @param posts       현재 호출부가 선택한 신규 미처리 글
 * @param fetchHtml   background 의 인증 fetch
 * @returns {saved: string[], skipped: string[], failed: string[]}
 */
export async function archivePosts(board, posts, fetchHtml) {
  const key = `collector_delivery_v1_${board.id}`;
  const stored = await chrome.storage.local.get(key);
  let delivered = stored[key] && !Array.isArray(stored[key]) ? { ...stored[key] } : {};
  const saved = [], skipped = [], failed = [];

  for (const post of posts) {
    const external = externalId(post.link);
    const source = sourceId(post.link);
    if (!external || !source) {
      failed.push(post.link);
      continue;
    }

    try {
      const html = await fetchHtml(post.link, board.allowHttpError ?? false);
      const pageHash = await sha256Hex(html);
      // 재다운로드 판단은 휘발성(조회수 등) 제거본으로 — pageHash 는 manifest·captureId 용
      const deliveryKey = await sha256Hex(deliveryContent(html, post.link));
      if (delivered[external] === deliveryKey) {
        skipped.push(external);
        continue;
      }

      const id = captureId(source, external, pageHash);
      const dir = `${ROOT}/${bundleDir(id, post.title)}`;
      const attachments = extractAttachments(html, post.link);
      const manifest = buildManifest({
        captureId: id,
        sourceId: source,
        externalId: external,
        sourceUrl: post.link,
        pageHash,
        capturedAt: new Date().toISOString(),
        attachments,
      });

      await download(
        "data:text/html;charset=utf-8," + encodeURIComponent(html),
        `${dir}/page.html`
      );
      for (const attachment of attachments) {
        await download(
          attachment.url,
          `${dir}/attachments/${attachment.name}`
        );
      }
      await download(
        "data:application/json;charset=utf-8," +
          encodeURIComponent(JSON.stringify(manifest, null, 2) + "\n"),
        `${dir}/manifest.json`
      );
      await publishReadyMarker(id, dir);

      delete delivered[external];
      delivered[external] = deliveryKey;
      saved.push(id);
    } catch (e) {
      console.warn(`[Archive] ${post.link} 실패:`, e.message);
      failed.push(post.link);
    }
  }

  if (saved.length) {
    delivered = Object.fromEntries(Object.entries(delivered).slice(-KEEP));
    await chrome.storage.local.set({ [key]: delivered });
  }
  return { saved, skipped, failed };
}
