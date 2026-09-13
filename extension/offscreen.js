/**
 * offscreen.js
 *
 * Service Worker는 DOM API가 없으므로,
 * HTML 파싱이 필요한 모든 작업을 이 offscreen document에서 처리한다.
 *
 * 메시지 타입:
 *   PARSE_POSTS      → { html, board }           → { posts }
 *   DETECT_SELECTORS → { html, url }              → { candidates, completionPatterns }
 */

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (sender.url === chrome.runtime.getURL("offscreen.html")) return;

  if (msg.type === "PARSE_POSTS") {
    const posts = parsePosts(msg.html, msg.board);
    sendResponse({ posts });
    return true;
  }

  if (msg.type === "DETECT_SELECTORS") {
    const result = detectSelectors(msg.html, msg.url);
    sendResponse(result);
    return true;
  }
});

// ════════════════════════════════════════════════════════
// PARSE_POSTS: 실제 게시글 목록 파싱
// ════════════════════════════════════════════════════════
function parsePosts(html, board) {
  const doc = new DOMParser().parseFromString(html, "text/html");
  const rows = doc.querySelectorAll(board.rowSelector);
  const posts = [];

  rows.forEach((row) => {
    const titleEl = row.querySelector(board.titleSelector);
    const linkEl  = row.querySelector(board.linkSelector ?? board.titleSelector);
    const statusEl = board.statusSelector ? row.querySelector(board.statusSelector) : null;
    
    if (!titleEl) return;

    const title = titleEl.textContent.trim();
    const href  = linkEl?.getAttribute("href") ?? "";
    const status = statusEl ? statusEl.textContent.trim() : null;

    if (!title || !href || href.startsWith("javascript")) return;

    const link = href.startsWith("http") ? href : new URL(href, board.listUrl).href;
    posts.push({ title, link, status });
  });

  return posts;
}

// ════════════════════════════════════════════════════════
// DETECT_SELECTORS: selector 자동감지 휴리스틱
// ════════════════════════════════════════════════════════

/**
 * HTML에서 게시판 selector 후보를 추론한다.
 *
 * 반환:
 *   candidates: [
 *     {
 *       rowSelector,    // 행 selector
 *       titleSelector,  // 제목 selector
 *       score,          // 신뢰도 점수 (높을수록 좋음)
 *       preview,        // 추출된 제목 샘플 배열 (최대 5개)
 *       rowCount,       // 감지된 행 수
 *     }, ...
 *   ]
 *   completionPatterns: string[]  // 발견된 완료 prefix 패턴
 */
function detectSelectors(html, pageUrl) {
  const doc = new DOMParser().parseFromString(html, "text/html");
  const candidates = [];

  // ── 전략 1: table > tbody > tr ──────────────────────
  doc.querySelectorAll("table").forEach((table) => {
    const tbody = table.querySelector("tbody");
    if (!tbody) return;

    const rows = tbody.querySelectorAll("tr");
    if (rows.length < 3) return; // 너무 적으면 스킵

    // 제목 셀 후보 찾기
    const titleCandidates = findTitleCellSelector(rows, pageUrl);

    for (const tc of titleCandidates) {
      const rowSel = buildSelector(table) + " tbody tr";
      const preview = extractPreview(rows, tc.selector);
      if (preview.length === 0) continue;

      candidates.push({
        rowSelector: rowSel,
        titleSelector: tc.selector,
        score: scoreCandidate(rows.length, preview, tc.confidence),
        preview,
        rowCount: rows.length,
        source: "table",
      });
    }
  });

  // ── 전략 2: ul/ol > li ──────────────────────────────
  doc.querySelectorAll("ul, ol").forEach((list) => {
    const items = list.querySelectorAll(":scope > li");
    if (items.length < 3) return;

    const titleCandidates = findTitleCellSelector(items, pageUrl);

    for (const tc of titleCandidates) {
      const listSel = buildSelector(list) + " > li";
      const preview = extractPreview(items, tc.selector);
      if (preview.length === 0) continue;

      candidates.push({
        rowSelector: listSel,
        titleSelector: tc.selector,
        score: scoreCandidate(items.length, preview, tc.confidence),
        preview,
        rowCount: items.length,
        source: "list",
      });
    }
  });

  // ── 전략 3: div 반복 패턴 (class 기반) ──────────────
  const divGroups = findRepeatingDivs(doc);
  for (const g of divGroups) {
    const titleCandidates = findTitleCellSelector(g.items, pageUrl);
    for (const tc of titleCandidates) {
      const preview = extractPreview(g.items, tc.selector);
      if (preview.length === 0) continue;

      candidates.push({
        rowSelector: g.selector,
        titleSelector: tc.selector,
        score: scoreCandidate(g.items.length, preview, tc.confidence) - 5, // div는 낮은 신뢰도
        preview,
        rowCount: g.items.length,
        source: "div",
      });
    }
  }

  // ── 중복 제거 + 점수 내림차순 정렬 ─────────────────
  const unique = dedup(candidates);
  unique.sort((a, b) => b.score - a.score);

  // ── 완료 패턴 감지 ──────────────────────────────────
  const allTitles = collectAllTitles(doc);
  const completionPatterns = detectCompletionPatterns(allTitles);

  return {
    candidates: unique.slice(0, 4), // 상위 4개만
    completionPatterns,
    allTitleCount: allTitles.length,
  };
}

// ── 제목 셀 selector 후보 찾기 ─────────────────────────
function findTitleCellSelector(rows, pageUrl) {
  const candidates = [];
  if (rows.length === 0) return candidates;

  const firstRow = rows[0];

  // 방법 A: class에 title/subject/tit/게시 포함한 요소
  const titleKeywords = ["title", "subject", "tit", "제목", "content", "cont", "bbs", "nttSj", "bbsList", "artclTit", "boardList"];
  for (const kw of titleKeywords) {
    const el = firstRow.querySelector(`[class*="${kw}"] a, [class*="${kw}"]`);
    if (el && el.textContent.trim().length > 1) {
      candidates.push({ selector: buildRelativeSelector(firstRow, el), confidence: 90 });
    }
  }

  // 방법 B: a 태그 href가 상세 페이지 패턴인 것
  const links = firstRow.querySelectorAll("a[href]");
  links.forEach((a) => {
    const href = a.getAttribute("href") ?? "";
    const text = a.textContent.trim();
    if (text.length < 2) return;
    if (href.includes("javascript")) return;
    // 상세 URL 패턴: view, read, detail, seq, bbsId, nttId 포함
    const isDetailLink = /view|read|detail|seq=|nttId|bbsId|artcl|artclNum/i.test(href);
    if (isDetailLink) {
      candidates.push({ selector: buildRelativeSelector(firstRow, a), confidence: 85 });
    }
  });

  // 방법 C: 텍스트가 가장 긴 a 태그 (제목일 가능성)
  let maxLen = 0, maxEl = null;
  firstRow.querySelectorAll("a").forEach((a) => {
    const t = a.textContent.trim();
    if (t.length > maxLen && !a.getAttribute("href")?.includes("javascript")) {
      maxLen = t.length; maxEl = a;
    }
  });
  if (maxEl && maxLen > 3) {
    candidates.push({ selector: buildRelativeSelector(firstRow, maxEl), confidence: 60 });
  }

  return candidates;
}

// ── 행에서 selector로 텍스트 추출 (preview) ─────────────
function extractPreview(rows, selector) {
  const results = [];
  for (const row of rows) {
    const el = row.querySelector(selector);
    const text = el?.textContent.trim();
    if (text && text.length > 1) results.push(text);
    if (results.length >= 5) break;
  }
  return results;
}

// ── div 반복 패턴 탐지 ──────────────────────────────────
function findRepeatingDivs(doc) {
  const groups = [];
  const seen = new Set();

  doc.querySelectorAll("div[class]").forEach((div) => {
    const cls = div.className.trim().split(/\s+/)[0];
    if (!cls || seen.has(cls)) return;

    // 같은 클래스가 3개 이상 연속으로 있는지 확인
    const siblings = div.parentElement?.querySelectorAll(`:scope > div.${CSS.escape(cls)}`);
    if (siblings && siblings.length >= 3) {
      seen.add(cls);
      const parentSel = buildSelector(div.parentElement);
      groups.push({
        selector: `${parentSel} > div.${cls}`,
        items: Array.from(siblings),
      });
    }
  });

  return groups;
}

// ── 완료 패턴 감지 ──────────────────────────────────────
function collectAllTitles(doc) {
  const titles = [];
  // a 태그 텍스트 전체 수집
  doc.querySelectorAll("a").forEach((a) => {
    const t = a.textContent.trim();
    if (t.length > 2 && !a.getAttribute("href")?.includes("javascript")) {
      titles.push(t);
    }
  });
  return titles;
}

function detectCompletionPatterns(titles) {
  const patternCounts = new Map();

  // prefix 패턴 추출 규칙
  const RULES = [
    /^(Re:\(완료\))/i,
    /^(\(완료\)Re:)/i,
    /^(Re:\s*\(완료\))/i,
    /^(\(완료\)\s*Re:)/i,
    /^(\[완료\]\s*Re:)/i,
    /^(Re:\s*\[완료\])/i,
    /^(\[완료\]Re:)/i, // 붙어있는 케이스 추가
    /^(답변:\s*\(완료\))/i,
    /^(\(처리완료\))/i,
    /^(\[처리완료\])/i,
    /^(완료:\s*Re:)/i,
    /^(\[완료\])/i,   // Re:가 없는 경우도 고려
  ];

  for (const title of titles) {
    for (const rule of RULES) {
      const m = title.match(rule);
      if (m) {
        const pat = m[1].trim();
        patternCounts.set(pat, (patternCounts.get(pat) ?? 0) + 1);
      }
    }
  }

  // 1회 이상 등장한 패턴만 반환, 빈도 내림차순
  return [...patternCounts.entries()]
    .filter(([, c]) => c >= 1)
    .sort((a, b) => b[1] - a[1])
    .map(([pat]) => pat);
}

// ── selector 신뢰도 점수 계산 ─────────────────────────────
function scoreCandidate(rowCount, preview, confidence) {
  let score = confidence;
  // 행 수 보너스 (게시판다운 개수)
  if (rowCount >= 10) score += 10;
  else if (rowCount >= 5) score += 5;
  // preview 품질: 제목 평균 길이
  const avgLen = preview.reduce((s, t) => s + t.length, 0) / preview.length;
  if (avgLen > 10) score += 10;
  else if (avgLen > 5) score += 5;
  // 숫자만 있는 preview는 감점 (번호 컬럼일 가능성)
  if (preview.every((t) => /^\d+$/.test(t))) score -= 50;
  return score;
}

// ── CSS selector 빌더 ────────────────────────────────────
function buildSelector(el) {
  if (!el || el === document.body) return "body";
  const parts = [];
  let cur = el;
  while (cur && cur !== document.body && cur.nodeType === 1) {
    let part = cur.tagName.toLowerCase();
    if (cur.id) { part = `#${CSS.escape(cur.id)}`; parts.unshift(part); break; }
    const cls = [...cur.classList].filter(c => !/^js-|active|selected|hover/.test(c)).slice(0, 2);
    if (cls.length) part += "." + cls.map(c => CSS.escape(c)).join(".");
    parts.unshift(part);
    cur = cur.parentElement;
    if (parts.length >= 4) break; // 너무 깊으면 중단
  }
  return parts.join(" > ");
}

function buildRelativeSelector(root, el) {
  // root 기준으로 el까지의 상대 selector 생성
  if (root === el) return el.tagName.toLowerCase();
  const parts = [];
  let cur = el;
  while (cur && cur !== root && cur !== document.body) {
    let part = cur.tagName.toLowerCase();
    const cls = [...cur.classList].filter(c => !/active|selected|hover/.test(c)).slice(0, 2);
    if (cls.length) part += "." + cls.map(c => CSS.escape(c)).join(".");
    parts.unshift(part);
    cur = cur.parentElement;
  }
  return parts.join(" ");
}

function dedup(candidates) {
  const seen = new Set();
  return candidates.filter((c) => {
    const key = c.rowSelector + "|" + c.titleSelector;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}
