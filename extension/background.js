/**
 * background.js - Service Worker
 * 
 * 역할:
 *   1. chrome.alarms로 주기적 스캔 트리거
 *   2. 각 게시판 fetch (브라우저 세션 그대로 사용)
 *   3. HTML 파싱 → analyzer로 미처리 판단
 *   4. chrome.storage로 이전 상태 비교 → 신규 미처리만 필터
 *   5. 텔레그램 알림 전송
 *   6. 상세·첨부를 Downloads Inbox Bundle로 발행
 */

import { analyze } from "./analyzer.js";
import { archivePosts, externalId, sourceId, publishSourceObservation } from "./archiver.js";
import { escMd, escUrl } from "./escape.js";
import { singleFlight } from "./singleflight.js";

/** chrome.storage에서 게시판 목록 조회 */
async function getBoards() {
  const { boards } = await chrome.storage.local.get("boards");
  return (boards ?? []).filter((b) => b.enabled);
}

// ────────────────────────────────────────────
// 설정값 (popup에서 수정 가능)
// ────────────────────────────────────────────
const ALARM_NAME = "board-scan";
const DEFAULT_INTERVAL_MINUTES = 1; // 스캔 주기 (분)

// 텔레그램 설정 - popup 설정 화면에서 입력
// storage key: "telegramToken", "telegramChatId"

// ────────────────────────────────────────────
// 초기화
// ────────────────────────────────────────────
chrome.runtime.onInstalled.addListener(async () => {
  console.log("[Monitor] 확장프로그램 설치됨, 스캔 알람 등록");
  await setupAlarm();
});

chrome.runtime.onStartup.addListener(async () => {
  await setupAlarm();
});

async function setupAlarm() {
  const { scanInterval } = await chrome.storage.local.get("scanInterval");
  const intervalMinutes = scanInterval || DEFAULT_INTERVAL_MINUTES;

  await chrome.alarms.clearAll();
  chrome.alarms.create(ALARM_NAME, {
    delayInMinutes: 0.1, // 등록 후 6초 뒤 첫 실행
    periodInMinutes: intervalMinutes,
  });
  console.log(`[Monitor] 알람 등록: ${intervalMinutes}분 주기`);
}

// ────────────────────────────────────────────
// 스캔 트리거
// ────────────────────────────────────────────
chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name !== ALARM_NAME) return;
  console.log("[Monitor] 스캔 시작:", new Date().toLocaleTimeString());
  await scanAllBoards();
});

// popup/offscreen 메시지 라우팅
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "MANUAL_SCAN") {
    scanAllBoards().then(() => sendResponse({ ok: true }));
    return true;
  }
  if (msg.type === "UPDATE_INTERVAL") {
    setupAlarm().then(() => sendResponse({ ok: true }));
    return true;
  }
  if (msg.type === "RESEND_TELEGRAM") {
    sendTelegram(msg.token, msg.chatId, msg.text, msg.boardName).then((ok) => sendResponse({ ok }));
    return true;
  }
  // popup → background → offscreen (DOM 파싱 필요한 작업)
  if (msg.type === "DETECT_SELECTORS") {
    handleDetectSelectors(msg).then(sendResponse);
    return true;
  }
});

// ────────────────────────────────────────────
// Offscreen document 관리
// ────────────────────────────────────────────
const OFFSCREEN_URL = chrome.runtime.getURL("offscreen.html");

async function ensureOffscreen() {
  const existing = await chrome.offscreen.hasDocument?.();
  if (existing) return;
  // hasDocument가 없는 구버전 대비
  try {
    await chrome.offscreen.createDocument({
      url: OFFSCREEN_URL,
      reasons: ["DOM_PARSER"],
      justification: "HTML 파싱 및 selector 자동감지",
    });
  } catch (e) {
    // 이미 존재하면 무시
    if (!e.message?.includes("already")) throw e;
  }
}

async function sendToOffscreen(msg) {
  await ensureOffscreen();

  const result = await new Promise((resolve) => {
    chrome.runtime.sendMessage(msg, (resp) => {
      void chrome.runtime.lastError; // 소비하지 않으면 Chrome이 경고를 던짐
      resolve(resp ?? null);
    });
  });

  if (result !== null) return result;

  // null = offscreen이 응답하지 않음 → 문서 재생성 후 1회 재시도
  console.warn("[Monitor] offscreen 무응답, 재생성 후 재시도");
  try { await chrome.offscreen.closeDocument?.(); } catch (_) { /* ignore */ }
  await new Promise((r) => setTimeout(r, 200));
  await ensureOffscreen();

  return new Promise((resolve) => {
    chrome.runtime.sendMessage(msg, (resp) => {
      void chrome.runtime.lastError;
      resolve(resp ?? null);
    });
  });
}

async function handleDetectSelectors(msg) {
  try {
    const html = await fetchBoardHtml(msg.url, msg.allowHttpError ?? false);
    const result = await sendToOffscreen({
      type: "DETECT_SELECTORS",
      html,
      url: msg.url,
    });
    return { ok: true, ...result };
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

// ────────────────────────────────────────────
// source별 실패는 다음 polling과 독립적으로 기록·알림한다.
function observationSource(board) {
  const canonical = sourceId(board.listUrl);
  const fallback = String(board.id ?? "").trim().toLowerCase();
  if (canonical) return canonical;
  if (/^[a-z0-9][a-z0-9._-]*$/.test(fallback)) return fallback;
  throw new Error("invalid source id");
}

function observationId(source, kind, suffix = "") {
  const stamp = new Date().toISOString().replace(/[^0-9]/g, "");
  return `${source}-${kind}-${suffix || stamp}`.toLowerCase();
}

async function publishObservation(board, observation) {
  try {
    await publishSourceObservation({ ...observation, sourceId: observationSource(board), sourceUrl: board.listUrl });
    return true;
  } catch (error) {
    console.warn(`[Monitor] ${board.name} observation publication failed`, error?.message);
    return false;
  }
}

 // ────────────────────────────────────────────
 // source별 실패는 다음 polling과 독립적으로 기록·알림한다.
function sourceErrorDetails(board, error, previous = {}) {
  const raw = String(error?.message ?? error);
  const normalized = raw.toLowerCase();
  const authentication = /로그인|login|sign/.test(normalized);
  const http = /\bhttp\s+\d{3}\b/.test(normalized);
  const selector = /selector|파싱|offscreen/.test(normalized);
  const stage = authentication ? "authentication" : http ? "fetch" : selector ? "parse" : "poll";
  const code = authentication ? "authentication_required" : http ? "http_error" : selector ? "parse_error" : "poll_error";
  const nextAction = authentication
    ? "해당 source 브라우저 세션에 다시 로그인한 뒤 다음 polling에서 재시도합니다."
    : selector
      ? "해당 source의 selector 설정을 확인한 뒤 다음 polling에서 재시도합니다."
      : "다음 polling에서 자동 재시도합니다.";
  return {
    boardId: board.id,
    boardName: board.name,
    code,
    stage,
    message: authentication
      ? "로그인 세션이 만료되었거나 인증 페이지가 반환되었습니다."
      : http
        ? "source 응답을 가져오지 못했습니다."
        : selector
          ? "source 응답을 해석하지 못했습니다."
          : "source polling에 실패했습니다.",
    occurredAt: new Date().toISOString(),
    lastSuccessAt: previous.lastSuccessAt ?? null,
    retryable: true,
    nextAction,
  };
}

function notifySourceError(details) {
  if (!chrome.notifications?.create) return;
  chrome.notifications.create({
    type: "basic",
    iconUrl: "icons/icon48.png",
    title: `${details.boardName} 수신 오류`,
    message: `${details.message}\n다음 행동: ${details.nextAction}`,
  });
}

// ────────────────────────────────────────────
// 핵심: 전체 게시판 스캔
// ────────────────────────────────────────────
const scanAllBoards = singleFlight(async () => {
  const enabledBoards = await getBoards();
  const { sourceHealth: storedHealth = {} } = await chrome.storage.local.get("sourceHealth");
  const sourceHealth = { ...storedHealth };
  const results = [];

  for (const board of enabledBoards) {
    try {
      const result = await scanBoard(board);
      const attemptedAt = new Date().toISOString();
      const previousError = sourceHealth[board.id]?.lastError;
      let handoffFailed = false;
      for (const post of result.nowResolved) {
        const external = externalId(post.link);
        if (external && !await publishObservation(board, {
          observationId: observationId(observationSource(board), "completion", external),
          kind: "source_completion_observed", observedAt: attemptedAt, externalId: external,
        })) handoffFailed = true;
      }
      if (previousError && !await publishObservation(board, {
        observationId: observationId(observationSource(board), "recovered"),
        kind: "source_recovered", observedAt: attemptedAt, priorErrorCode: previousError.code,
      })) handoffFailed = true;
      if (handoffFailed) {
        if (result.previousState) await chrome.storage.local.set({ [result.storageKey]: result.previousState });
        else if (chrome.storage.local.remove) await chrome.storage.local.remove(result.storageKey);
        const failedAt = new Date().toISOString();
        const handoffError = { code: "observation_delivery_failed", stage: "handoff", retryable: true, occurredAt: failedAt, lastSuccessAt: sourceHealth[board.id]?.lastSuccessAt ?? null, nextAction: "다음 polling에서 observation handoff를 재시도합니다." };
        sourceHealth[board.id] = { ...(sourceHealth[board.id] ?? {}), lastAttemptAt: failedAt, lastError: handoffError, retryable: true, nextAction: handoffError.nextAction };
        results.push({ board, error: "source observation handoff failed", errorDetails: handoffError });
        continue;
      }
      sourceHealth[board.id] = { ...(sourceHealth[board.id] ?? {}), lastAttemptAt: attemptedAt, lastSuccessAt: attemptedAt, lastError: null, retryable: false, nextAction: null };
      results.push(result);
    } catch (err) {
      console.error(`[Monitor] ${board.name} 스캔 실패:`, err.message);
      const details = sourceErrorDetails(board, err, sourceHealth[board.id]);
      sourceHealth[board.id] = {
        ...(sourceHealth[board.id] ?? {}),
        lastAttemptAt: details.occurredAt,
        lastSuccessAt: details.lastSuccessAt,
        lastError: details,
        retryable: details.retryable,
        nextAction: details.nextAction,
      };
      notifySourceError(details);
      await publishObservation(board, {
        observationId: observationId(observationSource(board), "error"),
        kind: "source_error", observedAt: details.occurredAt, code: details.code,
        stage: details.stage, retryable: details.retryable,
        lastSuccessAt: details.lastSuccessAt, nextAction: details.nextAction,
      });
      results.push({ board, error: details.message, errorDetails: details });
    }
  }

  // 마지막 스캔 시각과 source별 성공/실패 상태 저장
  await chrome.storage.local.set({
    lastScanTime: Date.now(),
    sourceHealth,
    lastScanResults: results.map((r) => ({
      boardId: r.board?.id,
      boardName: r.board?.name,
      unhandledCount: r.newUnhandled?.length ?? 0,
      error: r.error ?? null,
      errorDetails: r.errorDetails ?? null,
    })),
  });
  console.log("[Monitor] 스캔 완료");
});

// ────────────────────────────────────────────
// 단일 게시판 스캔
// ────────────────────────────────────────────
async function scanBoard(board) {
  // 1. HTML 가져오기 (브라우저 세션 자동 포함)
  const html = await fetchBoardHtml(board.listUrl, board.allowHttpError ?? false);

  // 2. HTML → 게시글 목록 파싱 (offscreen에 위임)
  let posts;
  try {
    await ensureOffscreen();
    const result = await sendToOffscreen({ type: "PARSE_POSTS", html, board });
    posts = result?.posts ?? [];
  } catch (e) {
    // offscreen 실패 시 정규식 폴백
    console.warn("[Monitor] offscreen 파싱 실패, 정규식 폴백:", e.message);
    posts = parsePostsRegex(html, board);
  }

  if (posts.length === 0) {
    console.warn(`[Monitor] ${board.name}: 파싱된 게시글 없음 (selector 확인 필요)`);
  }

  // 3. 완료/미처리 분석
  const { unhandled, completedPosts = [], totalOriginals } = analyze(posts, board.completePatterns, board);

  // 4. 이전 상태와 비교 → 신규 미처리만 추출
  const storageKey = `board_state_${board.id}`;
  const stored = await chrome.storage.local.get(storageKey);
  const previousState = stored[storageKey];
  const prevUnhandledLinks = new Set(previousState?.unhandledLinks ?? []);

  const newUnhandled = unhandled.filter(
    (p) => !prevUnhandledLinks.has(p.link)
  );
  const nowResolved = completedPosts.filter((post) => prevUnhandledLinks.has(post.link));

  // 5. 현재 상태 저장
  await chrome.storage.local.set({
    [storageKey]: {
      unhandledLinks: unhandled.map((p) => p.link),
      unhandledPosts: unhandled,
      updatedAt: Date.now(),
      boardName: board.name,
      stats: {
        totalPosts: posts.length,
        totalOriginals,
        completedCount: totalOriginals - unhandled.length,
        unhandledCount: unhandled.length,
      },
    },
  });

  // 6. 신규 미처리 알림 (보류 링크·영구무시 ID 제외)
  //   보류(ignoredLinks): 링크 기준, 완료되면 자동 해제 (아래 7번)
  //   영구무시(blockedIds): "boardId:글ID" 기준, 완료돼도·URL 파라미터가 바뀌어도 유지
  const { ignoredLinks = [], blockedIds = [] } = await chrome.storage.local.get(["ignoredLinks", "blockedIds"]);
  const blocked = new Set(blockedIds);
  const isHidden = (p) => ignoredLinks.includes(p.link) || blocked.has(`${board.id}:${externalId(p.link)}`);
  const filteredNewUnhandled = newUnhandled.filter(p => !isHidden(p));

  if (filteredNewUnhandled.length > 0) {
    await notify(board, filteredNewUnhandled, "new");
  }

  // 6-1. 상세 HTML·첨부를 완전한 Inbox Bundle로 발행한다.
  //
  //   현재 검증 단계에서는 기존 미처리 선별을 유지한다. 같은 HTML의 재다운로드
  //   방지와 `_READY` 발행은 archiver의 delivery ledger가 맡는다.
  //
  //   알림 뒤에 둔다 — Bundle 실패가 기존 알림을 막지 않는다.
  //   인증 fetch는 검증된 3단 폴백을 그대로 재사용한다.
  const toArchive = unhandled.filter((p) => !isHidden(p));
  if (board.archive !== false && toArchive.length > 0) {
    try {
      const r = await archivePosts(board, toArchive, fetchBoardHtml);
      console.log(
        `[Inbox] ${board.name}: 대상 ${toArchive.length} → 완료 Bundle ${r.saved.length}, 동일 HTML ${r.skipped.length}, 실패 ${r.failed.length}`
      );
    } catch (e) {
      console.warn("[Inbox] Bundle 발행 실패:", e.message, e.stack);
    }
  } else {
    console.log(`[Inbox] ${board.name}: 대상 0건 (미처리 ${unhandled.length}, 숨김 ${unhandled.filter(isHidden).length})`);
  }

  // 7. 완료된 글은 ignoredLinks에서 자동 제거 (최적화)
  if (nowResolved.length > 0) {
    const resolvedLinks = new Set(nowResolved.map((post) => post.link));
    const nextIgnored = ignoredLinks.filter(link => !resolvedLinks.has(link));
    if (nextIgnored.length !== ignoredLinks.length) {
      await chrome.storage.local.set({ ignoredLinks: nextIgnored });
    }
  }

  console.log(
    `[Monitor] ${board.name}: 전체 원글 ${posts.length}건, 미처리 ${unhandled.length}건(노출 ${unhandled.filter(p => !isHidden(p)).length}건), 신규 ${newUnhandled.length}건`
  );
  return { board, unhandled, newUnhandled, nowResolved, storageKey, previousState };
}

// ────────────────────────────────────────────
// HTML fetch
// ────────────────────────────────────────────
async function fetchBoardHtml(url, allowHttpError = false) {
  const headers = { Accept: "text/html,application/xhtml+xml" };

  // ── 1단계: 서비스워커 직접 fetch (credentials:include) ──
  try {
    const resp = await fetch(url, { credentials: "include", headers });
    const finalUrl = resp.url;
    if (finalUrl.includes("login") || finalUrl.includes("sign")) {
      throw new Error("로그인 필요 (리다이렉트: " + finalUrl + ")");
    }
    if (!resp.ok && !allowHttpError) throw new Error("HTTP " + resp.status);
    return await resp.text();
  } catch (e1) {
    if (e1.message.startsWith("로그인") || e1.message.startsWith("HTTP")) throw e1;
    console.warn("[Monitor] SW fetch(with cred) 실패, omit 재시도:", e1.message);
  }

  // ── 2단계: credentials:omit 재시도 ──
  try {
    const resp = await fetch(url, { credentials: "omit", headers });
    if (!resp.ok && !allowHttpError) throw new Error("HTTP " + resp.status);
    return await resp.text();
  } catch (e2) {
    if (e2.message.startsWith("HTTP")) throw e2;
    console.warn("[Monitor] SW fetch(omit) 실패, 탭 방식 시도:", e2.message);
  }

  // ── 3단계: 브라우저에 열린 탭에서 fetch 실행 ──
  return await fetchViaTab(url, allowHttpError);
}

/**
 * 게시판 오리진의 탭을 확보한다. 없으면 고정 백그라운드 탭을 직접 연다.
 *
 * 3단계는 예외 경로가 아니라 사실상 정상 경로다 — 세션 쿠키가 SameSite=Lax 면
 * 서비스워커의 cross-site fetch 에는 안 실려서 1·2단계가 늘 실패한다.
 * 그런데 "사이트를 열어두세요" 로 사람에게 떠넘기면 탭을 닫는 순간 다시 멈춘다.
 * 무인 실행이 목적이므로 탭 확보까지 확장이 한다.
 */
async function ensureBoardTab(origin) {
  const existing = await chrome.tabs.query({ url: origin + "/*" });
  if (existing.length > 0) return existing[0].id;

  console.log("[Monitor] 탭 없음 → 고정 탭 생성:", origin);
  const tab = await chrome.tabs.create({ url: origin + "/", pinned: true, active: false });

  // 로드 완료까지 최대 15초 폴링
  for (let i = 0; i < 30; i++) {
    await new Promise((r) => setTimeout(r, 500));
    const t = await chrome.tabs.get(tab.id).catch(() => null);
    if (!t) throw new Error("생성한 탭이 사라졌다");
    if (t.status === "complete") return tab.id;
  }
  throw new Error("탭 로드 15초 초과: " + origin);
}

async function fetchViaTab(url, allowHttpError) {
  const origin = new URL(url).origin;
  const tabId = await ensureBoardTab(origin);

  const results = await chrome.scripting.executeScript({
    target: { tabId },
    func: async (fetchUrl, allowErr) => {
      try {
        const resp = await fetch(fetchUrl, {
          credentials: "include",
          headers: { Accept: "text/html,application/xhtml+xml" },
        });
        if (!resp.ok && !allowErr) return { error: "HTTP " + resp.status };
        return { html: await resp.text(), finalUrl: resp.url };
      } catch (e) {
        return { error: e.message };
      }
    },
    args: [url, allowHttpError],
  });

  const result = results?.[0]?.result;
  if (!result) throw new Error("탭 스크립트 실행 실패");
  if (result.error) throw new Error(result.error);

  // 로그인 안 된 상태면 로그인 페이지가 돌아온다.
  // 그냥 두면 "파싱된 게시글 없음 (selector 확인 필요)" 로 보여서 엉뚱한 데를 파게 된다.
  if (/\/(login|sign)/i.test(result.finalUrl ?? "")) {
    throw new Error(`로그인 필요 — 브라우저에서 ${origin} 에 로그인해라 (리다이렉트: ${result.finalUrl})`);
  }
  return result.html;
}

/**
 * DOMParser 불가 환경용 정규식 폴백
 * a 태그 텍스트와 href를 추출 (정밀도 낮음, 임시용)
 */
function parsePostsRegex(html, board) {
  const posts = [];
  // href와 텍스트 추출
  const regex = /<a[^>]+href=["']([^"']+)["'][^>]*>([\s\S]*?)<\/a>/gi;
  let match;
  while ((match = regex.exec(html)) !== null) {
    const href = match[1];
    const text = match[2].replace(/<[^>]+>/g, "").trim();
    // 제목으로 보이는 것만 (공백 제거 후 길이 2 이상)
    if (text.length >= 2 && !href.includes("javascript")) {
      const link = href.startsWith("http")
        ? href
        : new URL(href, board.listUrl).href;
      posts.push({ title: text, link });
    }
  }
  return posts;
}

// ────────────────────────────────────────────
// 텔레그램 알림
// ────────────────────────────────────────────
async function notify(board, posts, type) {
  const { telegramToken, telegramChatId, telegramEnabled } = await chrome.storage.local.get([
    "telegramToken",
    "telegramChatId",
    "telegramEnabled",
  ]);

  // 알림이 비활성화되어 있으면 크롬 알림만 보내고 종료
  if (telegramEnabled === false) {
    chrome.notifications.create({
      type: "basic",
      iconUrl: "icons/icon48.png",
      title: `🔴 ${board.name} 미처리 ${posts.length}건`,
      message: posts.map((p) => p.title).join("\n"),
    });
    return;
  }

  if (!telegramToken || !telegramChatId) {
    console.warn("[Monitor] 텔레그램 설정 없음 — popup에서 설정해주세요");
    return;
  }

  // 한 번에 5건 이상이면 요약, 이하면 개별
  if (posts.length >= 5) {
    await sendTelegram(
      telegramToken,
      telegramChatId,
      `🔴 *[${escMd(board.name)}]* 미처리 신규 ${posts.length}건\n\n` +
        posts.map((p, i) => `${i + 1}\\. ${escMd(p.title)}`).join("\n"),
      board.name
    );
  } else {
    for (const post of posts) {
      const msg =
        `🔴 *[${escMd(board.name)}]* 미처리 신규\n\n` +
        `*제목:* ${escMd(post.title)}\n` +
        `[게시글 바로가기](${escUrl(post.link)})`;
      await sendTelegram(telegramToken, telegramChatId, msg, board.name);
    }
  }
}

/** 텔레그램 로그 저장 */
async function addLog(entry) {
  const { notificationLogs = [] } = await chrome.storage.local.get("notificationLogs");
  const newLog = {
    id: Date.now() + Math.random().toString(36).substr(2, 5),
    timestamp: new Date().toLocaleString("ko-KR"),
    status: "pending",
    ...entry
  };
  
  notificationLogs.unshift(newLog);
  // 최대 50개 유지
  await chrome.storage.local.set({ notificationLogs: notificationLogs.slice(0, 50) });
  return newLog.id;
}

