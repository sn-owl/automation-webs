/**
 * escape.js — 텔레그램 MarkdownV2 이스케이프
 *
 * background.js 에서 분리했다. 여기서 한 번 틀려서 알림이 통째로 실패했고,
 * 서비스워커 안에 있으면 테스트가 안 된다. (escape.test.mjs)
 *
 * 규칙이 두 개다. 섞으면 안 된다.
 */

/** 본문 텍스트용. MarkdownV2 예약문자 전부 이스케이프. */
export function escMd(text) {
  if (!text) return "";
  return String(text).replace(/[_*[\]()~`>#+=|{}.!\\-]/g, "\\$&");
}

/**
 * 인라인 링크 `[문구](URL)` 의 URL 부분용.
 *
 * MarkdownV2 는 `(...)` 안에서 `)` 와 `\` 만 이스케이프를 허용한다.
 * 여기에 escMd 를 쓰면 https://cug\.thewebs\.kr/... 로 망가져서
 * 텔레그램이 "can't parse entities" 로 거절한다. 실제로 그랬다.
 */
export function escUrl(url) {
  return String(url ?? "").replace(/[)\\]/g, "\\$&");
}
