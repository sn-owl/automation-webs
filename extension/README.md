# 유지보수 게시판 모니터 — Chrome Extension

> **범위 안내.** 이 확장 프로그램은 [아키텍처 문서](../docs/ARCHITECTURE.md)의 **S1 개인화 수집** 계층에 해당하는 입력 연결기 하나다.
> 여기의 selector와 수집 방식은 특정 게시판에 맞춘 것이며, Core의 분류·평가·실행 계약을 이 게시판 업무로 제한하지 않는다.
> 자격증명과 사용자별 설정은 브라우저 로컬 저장소에만 두고 저장소에 커밋하지 않는다.

미처리 게시글 자동 감지 → 텔레그램 알림

---

## 설치

1. `chrome://extensions` 접속
2. 우상단 **개발자 모드** ON
3. **압축해제된 확장 프로그램 로드** → 이 폴더 선택

---

## 텔레그램 봇 설정

1. Telegram에서 `@BotFather` 검색 → `/newbot` → 토큰 발급
2. 봇과 대화 후 `https://api.telegram.org/bot{토큰}/getUpdates` 에서 `chat.id` 확인
3. 확장프로그램 팝업 → **설정 탭** → 토큰/Chat ID 입력 → 저장

---

## 게시판 selector 설정 (핵심)

`boards.js` 파일 수정:

```js
{
  id: "yeonje",
  name: "유지보수 게시판",
  listUrl: "https://실제URL/board/list.do?boardId=...",

  // 크롬 개발자도구 → 게시판 목록 → Inspector에서 확인
  rowSelector: "table.board-list tbody tr",  // 각 행
  titleSelector: "td.title a",               // 제목 텍스트
  linkSelector: "td.title a",                // 링크

  completePatterns: ["Re:(완료)", "(완료)Re:", "Re: (완료)"],
  enabled: true,
}
```

### selector 확인 방법

1. 게시판 목록 페이지에서 `F12`
2. Inspector에서 게시글 행(tr 또는 li) 우클릭 → Copy → Copy selector
3. 제목 a태그도 동일하게 복사
4. `boards.js`에 붙여넣기

---

## 완료 패턴 커스터마이징

게시판마다 "완료" 답글 형식이 다를 수 있습니다:

```js
completePatterns: [
  "Re:(완료)",      // Re:(완료) 원글제목
  "(완료)Re:",      // (완료)Re: 원글제목
  "Re: (완료)",     // 공백 포함 변형
  "[완료]Re:",      // 대괄호 형식
]
```

→ 실제 답글 제목 prefix를 그대로 추가하면 됩니다.

---

## 동작 원리

```
1분마다 (설정 가능)
  ↓
각 게시판 URL fetch (브라우저 세션/쿠키 자동 포함)
  ↓
전체 제목 수집 → 완료 답글 분리 → 미처리 원글 추출
  ↓
이전 스캔 결과와 비교 → 신규 미처리만 필터
  ↓
텔레그램 알림
```

**로그인 별도 불필요**: 해당 사이트에 브라우저로 로그인된 상태라면 세션 쿠키가 자동으로 fetch에 포함됩니다.

---

## 트러블슈팅

| 증상 | 원인 | 해결 |
|---|---|---|
| 게시글 파싱 0건 | selector 불일치 | boards.js selector 재확인 |
| 스캔 시 로그인 페이지로 이동 | 세션 만료 | 해당 사이트 브라우저에서 재로그인 |
| 텔레그램 알림 미수신 | 토큰/Chat ID 오류 | 팝업 설정 탭 재확인 |
| 완료 글이 미처리로 표시 | 패턴 불일치 | 실제 답글 제목 prefix를 completePatterns에 추가 |

---

## 파일 구조

```
board-monitor/
├── manifest.json    — 확장프로그램 선언
├── background.js    — Service Worker (스케줄 + 스캔 + 알림)
├── analyzer.js      — 완료/미처리 판단 로직
├── boards.js        — 게시판별 URL, selector 설정
├── popup.html/js    — 팝업 UI
└── icons/           — 확장프로그램 아이콘
```
