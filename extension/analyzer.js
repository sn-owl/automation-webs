/**
 * analyzer.js
 *
 * 완료 판단 규칙:
 *   - "답변글"로 시작하는 글 → 답변글
 *   - 답변글 제목에 completePatterns 중 하나가 포함되어 있으면 → 완료 답변
 *   - 완료 답변에서 원글 제목을 추출해, 해당 원글을 완료로 표시
 *   - 완료 답변이 없는 원글 → 미처리
 *
 * 답변글 제목 형식 (eGovFramework 일반):
 *   "답변글 (완료)Re: 원글제목"
 *   "답변글 Re: (완료) 원글제목"
 *   "답변글 Re: (확인중)원글제목"  ← 완료 아님
 */

/**
 * 답변글 여부 판별
 * 1. "Re:"로 시작하거나
 * 2. 사용자가 설정한 완료 패턴으로 시작하거나
 * 3. (하위호환) "답변글"로 시작하는 경우
 */
function isReply(title, completePatterns = []) {
  const t = title.trim();
  if (/^Re:/i.test(t)) return true;
  if (/^답변글/i.test(t)) return true;
  return completePatterns.some((p) => t.toLowerCase().startsWith(p.toLowerCase()));
}

/**
 * 완료 답변 여부 판별
 */
function isCompleteReply(title, completePatterns) {
  const t = title.toLowerCase();
  return completePatterns.some((p) => t.includes(p.toLowerCase()));
}

/**
 * 답변글 제목에서 원글 제목을 추출한다.
 *
 * 처리 순서:
 *   1. "답변글" prefix 제거
 *   2. 사용자가 설정한 completePatterns 제거 (긴 것부터)
 *   3. "Re:" prefix 제거
 *   4. 선두 괄호 표기 제거: (완료), [완료] 등
 */
function extractOriginalFromReply(title, completePatterns = []) {
  let t = title.trim();

  // 1. "답변글" 제거 (기존 게시판 대응)
  t = t.replace(/^답변글\s*/i, "");

  // 2. 설정된 완료 패턴 제거 (패턴 중첩 방지를 위해 긴 것부터)
  const sortedPatterns = [...completePatterns].sort((a, b) => b.length - a.length);
  for (const p of sortedPatterns) {
    if (t.toLowerCase().startsWith(p.toLowerCase())) {
      t = t.slice(p.length).trim();
      break; 
    }
  }

  // Bracket status may precede Re: (e.g. "(완료)Re:"); remove it
  // before the Re: pass as well as after it, without deleting title text.
  t = t.replace(/^((\([^)]*\)|\[[^\]]*\])\s*)+/, "");
  const reMatch = t.match(/^Re:\s*/i);
  if (reMatch) {
    t = t.slice(reMatch[0].length).trim();
  }
  t = t.replace(/^((\([^)]*\)|\[[^\]]*\])\s*)+/, "");
  return t.trim();
}

/**
 * 제목 정규화 (비교 시 공백/특수문자 차이 흡수)
 */
function normalizeTitle(title) {
  return title
    .trim()
    .replace(/\s+/g, " ")
    .replace(/[""'']/g, "")
    .toLowerCase();
}

/**
 * 게시글 목록을 분석해서 미처리 원글 목록을 반환한다.
 */
export function analyze(posts, completePatterns, board = {}) {
  // ── 방식 1: 상태값 기반 (동래구 등) ──
  // 게시판 설정에 statusSelector가 있는 경우
  if (board.statusSelector) {
    const completedPosts = posts.filter((post) => {
      const status = String(post.status ?? "");
      return status && completePatterns.some((p) => status.toLowerCase().includes(String(p).toLowerCase()));
    });
    const unhandled = posts.filter((post) => !completedPosts.includes(post));
    return {
      unhandled,
      completedPosts,
      totalOriginals: posts.length,
      mode: "status",
    };
  }

  // ── 방식 2: 답글 기반 (연제구 등) ──
  const replies = [];
  const originals = [];

  for (let i = 0; i < posts.length; i++) {
    const post = posts[i];
    const isRep = isReply(post.title, completePatterns);
    if (i === posts.length - 1 && !isRep && posts.length > 1) continue;
    if (isRep) replies.push(post);
    else originals.push(post);
  }

  const completedOriginalTitles = new Set();
  for (const reply of replies) {
    if (isCompleteReply(reply.title, completePatterns)) {
      const orig = extractOriginalFromReply(reply.title, completePatterns);
      if (orig) completedOriginalTitles.add(normalizeTitle(orig));
    }
  }

  const completedPosts = originals.filter((post) => {
    const norm = normalizeTitle(post.title);
    if (completedOriginalTitles.has(norm)) return true;
    for (const completed of completedOriginalTitles) {
      if (norm.startsWith(completed + " ") || norm.startsWith(completed)) return true;
    }
    return false;
  });
  const unhandled = originals.filter((post) => !completedPosts.includes(post));

  return {
    unhandled,
    completedPosts,
    completedOriginalTitles,
    totalOriginals: originals.length,
    mode: "reply",
  };
}
