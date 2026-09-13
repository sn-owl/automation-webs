/**
 * popup.js
 * 세 탭: 현황 / 게시판 관리 / 설정
 */

import { externalId } from "./archiver.js";

// ── 유틸 ──────────────────────────────────────────
const $ = (id) => document.getElementById(id);
function truncate(s, n) { return s.length > n ? s.slice(0, n) + "…" : s; }
function formatTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts);
  return `${d.getMonth()+1}/${d.getDate()} ${String(d.getHours()).padStart(2,"0")}:${String(d.getMinutes()).padStart(2,"0")} 스캔`;
}
function uid() { return Date.now().toString(36) + Math.random().toString(36).slice(2, 6); }

// ── 탭 전환 ──────────────────────────────────────
document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
    document.querySelectorAll(".panel").forEach(p => p.classList.remove("active"));
    tab.classList.add("active");
    $(`panel-${tab.dataset.tab}`).classList.add("active");
    if (tab.dataset.tab === "boards") renderBoardList();
    if (tab.dataset.tab === "status") loadStatus();
    if (tab.dataset.tab === "logs") renderLogs();
  });
});

// ════════════════════════════════════════════════
// 로그(Dashboard) 탭
// ════════════════════════════════════════════════
async function renderLogs() {
  const { notificationLogs = [] } = await chrome.storage.local.get("notificationLogs");
  const list = $("logsList");
  
  if (notificationLogs.length === 0) {
    list.innerHTML = '<div class="empty-state">전송 이력 없음</div>';
    return;
  }

  list.innerHTML = notificationLogs.map(log => {
    return `
      <div class="log-item ${log.status}" data-id="${log.id}">
        <div class="log-meta">
          <span>[${log.boardName}]</span>
          <span>${log.timestamp}</span>
        </div>
        <div class="log-status">${log.status}</div>
        <div class="log-body">${log.message.replace(/\n/g, "<br>")}</div>
        ${log.error ? `<div class="log-error">⚠️ ${log.error}</div>` : ""}
        ${log.status === "fail" ? `
          <div class="log-actions">
            <button class="btn-resend" data-id="${log.id}">재전송</button>
          </div>
        ` : ""}
      </div>
    `;
  }).join("");

  // 재전송 버튼 이벤트
  list.querySelectorAll(".btn-resend").forEach(btn => {
    btn.addEventListener("click", async (e) => {
      const id = btn.dataset.id;
      const log = notificationLogs.find(l => l.id === id);
      if (!log) return;
      
      btn.textContent = "전송 중...";
      btn.disabled = true;

      // 설정값 가져오기
      const { telegramToken, telegramChatId } = await chrome.storage.local.get(["telegramToken", "telegramChatId"]);
      
      // background에 전송 요청 (이미 선언된 sendTelegram 호출)
      // 주의: popup에서 직접 fetch하지 않고 background의 재시도 로직을 활용하기 위해 메시지 전송
      chrome.runtime.sendMessage({
        type: "RESEND_TELEGRAM",
        token: telegramToken,
        chatId: telegramChatId,
        text: log.message,
        boardName: log.boardName
      }, (resp) => {
        setTimeout(renderLogs, 1000); // 전송 결과 반영을 위해 1초 뒤 갱신
      });
    });
  });
}

// 로그 전체 삭제
$("btnClearLogs").addEventListener("click", async () => {
  if (!confirm("모든 전송 이력을 삭제하시겠습니까?")) return;
  await chrome.storage.local.set({ notificationLogs: [] });
  renderLogs();
});

// ════════════════════════════════════════════════
// 현황 탭
// ════════════════════════════════════════════════
async function loadStatus() {
  const data = await chrome.storage.local.get(null);
  const ignoredLinks = new Set(data.ignoredLinks ?? []);
  const blockedIds = new Set(data.blockedIds ?? []);

  if (data.lastScanTime) {
    const d = new Date(data.lastScanTime);
    $("scanTime").textContent =
      `${String(d.getHours()).padStart(2,"0")}:${String(d.getMinutes()).padStart(2,"0")}:${String(d.getSeconds()).padStart(2,"0")}`;
  }

  const entries = Object.entries(data)
    .filter(([k]) => k.startsWith("board_state_"))
    .map(([k, v]) => ({ ...v, boardId: v.boardId ?? k.slice("board_state_".length) }));

  const list = $("statusBoardList");
  if (entries.length === 0) {
    list.innerHTML = '<div class="empty-state">스캔 데이터 없음<br>아래 버튼으로 첫 스캔을 시작하세요</div>';
    return;
  }

  list.innerHTML = entries.map((entry) => {
    // 이 게시판이 다운로드(수집)한 글 ID → HTML 해시
    const ledger = data[`collector_delivery_v1_${entry.boardId}`] ?? {};
    const posts = (entry.unhandledPosts ?? []).map(p => {
      const ext = externalId(p.link);
      return {
        ...p,
        ext,
        muted: ignoredLinks.has(p.link),
        blocked: ext != null && blockedIds.has(`${entry.boardId}:${ext}`),
        collected: ext != null && ledger[ext] != null,
      };
    });

    // 실제 미처리 건수 (보류·영구무시 제외)
    const count = posts.filter(p => !p.muted && !p.blocked).length;
    const badgeCls = count > 0 ? "badge-red" : "badge-green";
    const badgeText = count > 0 ? `미처리 ${count}건` : "✓ 완료";

    const postItems = posts.slice(0, 7).map(p => {
      const state = p.blocked ? "blocked" : (p.muted ? "muted" : "");
      const blockBtn = p.ext != null
        ? `<button class="btn-block" data-key="${entry.boardId}:${p.ext}" title="${p.blocked ? '영구 무시 해제' : '영구 무시 — 완료돼도·URL이 바뀌어도 유지'}">${p.blocked ? '♻️' : '🚫'}</button>`
        : "";
      return `<div class="post-row ${state}">
        <div class="post-dot"></div>
        <div class="post-title" data-link="${p.link}" title="${p.title}">${truncate(p.title, 28)}</div>
        ${p.collected ? '<span class="post-collected" title="수집 완료 — 상세·첨부 다운로드됨">📥</span>' : ''}
        <button class="btn-mute" data-link="${p.link}" title="${p.muted ? '알림 보류 해제' : '알림 보류 — 미처리 카운트 제외'}">${p.muted ? '🔔' : '🔕'}</button>
        ${blockBtn}
      </div>`;
    }).join("");

    const totalCount = posts.length;
    const more = totalCount > 7
      ? `<div class="post-row"><div class="post-title" style="color:var(--text-dim);text-align:center">...외 ${totalCount-7}건은 원본 사이트에서 확인</div></div>`
      : "";

    return `<div class="board-card">
      <div class="bc-hdr">
        <div class="bc-name">${entry.boardName ?? "게시판"}</div>
        <span class="badge ${badgeCls}">${badgeText}</span>
      </div>
      <div class="bc-meta">${formatTime(entry.updatedAt)}</div>
      ${totalCount > 0 ? `<div class="post-list">${postItems}${more}</div>` : ""}
    </div>`;
  }).join("");

  // 제목 클릭 -> 이동
  list.querySelectorAll(".post-title[data-link]").forEach(el => {
    el.addEventListener("click", () => chrome.tabs.create({ url: el.dataset.link }));
  });

  // 보류 버튼 클릭 -> 토글
  list.querySelectorAll(".btn-mute").forEach(btn => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const link = btn.dataset.link;
      const { ignoredLinks: current = [] } = await chrome.storage.local.get("ignoredLinks");
      let next;
      if (current.includes(link)) {
        next = current.filter(l => l !== link);
      } else {
        next = [...current, link];
      }
      await chrome.storage.local.set({ ignoredLinks: next });
      await loadStatus(); // 즉시 리렌더링
    });
  });

  // 영구 무시 버튼 클릭 -> 토글 ("boardId:글ID" 기준, 완료돼도 유지)
  list.querySelectorAll(".btn-block").forEach(btn => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const key = btn.dataset.key;
      const { blockedIds: current = [] } = await chrome.storage.local.get("blockedIds");
      const next = current.includes(key)
        ? current.filter(k => k !== key)
        : [...current, key];
      await chrome.storage.local.set({ blockedIds: next });
      await loadStatus();
    });
  });
}

$("btnScan").addEventListener("click", async () => {
  const btn = $("btnScan");
  btn.disabled = true; btn.textContent = "스캔 중...";
  await chrome.runtime.sendMessage({ type: "MANUAL_SCAN" });
  setTimeout(async () => {
    await loadStatus();
    btn.disabled = false; btn.textContent = "🔍 지금 스캔";
  }, 2000);
});

// ════════════════════════════════════════════════
// 게시판 관리 탭
// ════════════════════════════════════════════════
let editingId = null; // null=추가모드, string=수정모드

async function getBoards() {
  const { boards } = await chrome.storage.local.get("boards");
  return boards ?? [];
}

async function saveBoards(boards) {
  await chrome.storage.local.set({ boards });
}

async function renderBoardList() {
  const boards = await getBoards();
  const area = $("boardListArea");

  if (boards.length === 0) {
    area.innerHTML = '<div class="empty-state">등록된 게시판 없음<br>+ 추가 버튼으로 시작하세요</div>';
    return;
  }

  area.innerHTML = boards.map(function(b) {
    var kwTags = (b.completePatterns ?? []).map(function(p) {
      return '<span class="bi-tag kw" title="완료 키워드">' + p + "</span>";
    }).join("");
    var selTag = b.rowSelector
      ? '<span class="bi-tag" title="행 Selector">' + truncate(b.rowSelector, 24) + "</span>"
      : "";
    return '<div class="bi" data-id="' + b.id + '">' +
      '<label class="toggle" title="' + (b.enabled ? "활성" : "비활성") + '">' +
        '<input type="checkbox" class="toggle-enabled" data-id="' + b.id + '" ' + (b.enabled ? "checked" : "") + ">" +
        '<div class="t-track"></div>' +
        '<div class="t-thumb"></div>' +
      "</label>" +
      '<div class="bi-info">' +
        '<div class="bi-name">' + b.name + (b.allowHttpError ? ' <span style="color:#d69e2e;font-size:9px">[500무시]</span>' : "") + "</div>" +
        '<div class="bi-url">' + b.listUrl + "</div>" +
        '<div class="bi-detail">' + selTag + kwTags + "</div>" +
      "</div>" +
      '<div class="bi-actions">' +
        '<button class="icon-btn btn-edit" data-id="' + b.id + '" title="수정">✎</button>' +
        '<button class="icon-btn danger btn-delete" data-id="' + b.id + '" title="삭제">✕</button>' +
      "</div>" +
    "</div>";
  }).join("");

  // 토글 이벤트
  area.querySelectorAll(".toggle-enabled").forEach(chk => {
    chk.addEventListener("change", async () => {
      const boards = await getBoards();
      const board = boards.find(b => b.id === chk.dataset.id);
      if (board) { board.enabled = chk.checked; await saveBoards(boards); }
    });
  });

  // 수정 버튼
  area.querySelectorAll(".btn-edit").forEach(btn => {
    btn.addEventListener("click", async () => {
      const boards = await getBoards();
      const board = boards.find(b => b.id === btn.dataset.id);
      if (board) openForm(board);
    });
  });

  // 삭제 버튼
  area.querySelectorAll(".btn-delete").forEach(btn => {
    btn.addEventListener("click", async () => {
      if (!confirm("이 게시판을 삭제할까요?")) return;
      const boards = await getBoards();
      const filtered = boards.filter(b => b.id !== btn.dataset.id);
      await saveBoards(filtered);
      // 해당 게시판 스캔 상태도 삭제
      await chrome.storage.local.remove(`board_state_${btn.dataset.id}`);
      await renderBoardList();
    });
  });
}

function openForm(board = null) {
  editingId = board ? board.id : null;
  $("formTitle").textContent = board ? "게시판 수정" : "게시판 추가";
  $("fName").value = board?.name ?? "";
  $("fUrl").value = board?.listUrl ?? "";
  $("fRowSel").value = board?.rowSelector ?? "table.board-list tbody tr";
  $("fTitleSel").value = board?.titleSelector ?? "td.title a";
  $("fStatusSel").value = board?.statusSelector ?? "";
  $("fPatterns").value = (board?.completePatterns ?? ["완료"]).join("\n");
  $("fAllowHttpError").checked = board?.allowHttpError ?? false;
  $("boardForm").style.display = "block";
  $("fName").focus();
}

function closeForm() {
  $("boardForm").style.display = "none";
  editingId = null;
}

$("btnNew").addEventListener("click", () => openForm(null));
$("btnFormClose").addEventListener("click", closeForm);
$("btnFormCancel").addEventListener("click", closeForm);

$("btnFormSave").addEventListener("click", async () => {
  const name = $("fName").value.trim();
  const url = $("fUrl").value.trim();
  const rowSel = $("fRowSel").value.trim();
  const titleSel = $("fTitleSel").value.trim();
  const statusSel = $("fStatusSel").value.trim();
  const patterns = $("fPatterns").value.split("\n").map(s => s.trim()).filter(Boolean);
  const allowHttpError = $("fAllowHttpError").checked;

  if (!name || !url || !rowSel || !titleSel) {
    alert("이름, URL, Selector는 필수입니다.");
    return;
  }
  if (patterns.length === 0) {
    alert("완료 패턴을 최소 1개 입력해주세요.");
    return;
  }

  const boards = await getBoards();

  const boardData = {
    name,
    listUrl: url,
    rowSelector: rowSel,
    titleSelector: titleSel,
    statusSelector: statusSel || null,
    linkSelector: titleSel,
    completePatterns: patterns,
    allowHttpError
  };

  if (editingId) {
    const idx = boards.findIndex(b => b.id === editingId);
    if (idx !== -1) {
      boards[idx] = { ...boards[idx], ...boardData };
    }
  } else {
    boards.push({
      id: uid(),
      ...boardData,
      enabled: true,
    });
  }

  await saveBoards(boards);
  closeForm();
  await renderBoardList();
});

// ── selector 자동감지 ───────────────────────────────────
$("btnAutoDetect").addEventListener("click", async () => {
  const url = $("fUrl").value.trim();
  if (!url) { alert("URL을 먼저 입력해주세요."); return; }

  const btn = $("btnAutoDetect");
  btn.disabled = true;
  btn.textContent = "⏳ 분석 중...";
  $("detectResult").style.display = "none";

  try {
    const resp = await chrome.runtime.sendMessage({ type: "DETECT_SELECTORS", url, allowHttpError: $("fAllowHttpError").checked });

    if (!resp?.ok) {
      showDetectError(resp?.error ?? "알 수 없는 오류");
      return;
    }

    renderCandidates(resp.candidates ?? [], resp.completionPatterns ?? []);
  } catch (e) {
    showDetectError(e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "🔍 자동 감지";
  }
});

function showDetectError(msg) {
  const area = $("detectResult");
  area.style.display = "block";
  area.innerHTML = `<div class="detect-error">⚠ ${msg}<br><span style="color:var(--text-dim);font-size:10px">해당 사이트에 로그인되어 있는지 확인하세요.</span></div>`;
}

function candidateCardHTML(c, i) {
  var preview = c.preview.slice(0, 3).map(function(t) {
    return '<span class="preview-tag">' + truncate(t, 28) + "</span>";
  }).join("");
  return '<div class="detect-card" data-idx="' + i + '">' +
    '<div class="dc-header">' +
      '<span class="dc-rank">#' + (i + 1) + "</span>" +
      '<span class="dc-score">신뢰도 ' + Math.min(100, c.score) + "%</span>" +
      '<span class="dc-rows">' + c.rowCount + "행 감지</span>" +
    "</div>" +
    '<div class="dc-sel">행: <code>' + truncate(c.rowSelector, 45) + "</code></div>" +
    '<div class="dc-sel">제목: <code>' + truncate(c.titleSelector, 45) + "</code></div>" +
    '<div class="dc-preview">' + preview + "</div>" +
  "</div>";
}

function renderCandidates(candidates, patterns) {
  const area = $("detectResult");
  area.style.display = "block";

  if (candidates.length === 0) {
    area.innerHTML = '<div class="detect-error">게시판 구조를 감지하지 못했습니다.<br><span style="color:var(--text-dim);font-size:10px">Selector를 수동으로 입력해주세요.</span></div>';
    return;
  }

  var cards = candidates.map(function(c, i) { return candidateCardHTML(c, i); }).join("");
  var chips = "";
  if (patterns.length > 0) {
    chips = '<div class="detect-label" style="margin-top:8px">감지된 완료 패턴</div>' +
      '<div class="pattern-chips">' +
      patterns.map(function(p) {
        return '<span class="pattern-chip" data-pattern="' + p + '">' + p + "</span>";
      }).join("") +
      "</div>";
  }
  area.innerHTML = '<div class="detect-label">감지된 selector 후보 — 클릭해서 적용</div>' + cards + chips;

  // 후보 카드 클릭 → selector 필드에 적용
  area.querySelectorAll(".detect-card").forEach((card) => {
    card.addEventListener("click", () => {
      const c = candidates[+card.dataset.idx];
      $("fRowSel").value = c.rowSelector;
      $("fTitleSel").value = c.titleSelector;
      // 선택 표시
      area.querySelectorAll(".detect-card").forEach(el => el.classList.remove("selected"));
      card.classList.add("selected");
    });
  });

  // 패턴 칩 클릭 → 패턴 필드에 추가
  area.querySelectorAll(".pattern-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      const existing = $("fPatterns").value.trim();
      const pat = chip.dataset.pattern;
      if (!existing.includes(pat)) {
        $("fPatterns").value = existing ? existing + "\n" + pat : pat;
      }
      chip.classList.toggle("chip-selected");
    });
  });
}

// ════════════════════════════════════════════════
// 설정 탭
// ════════════════════════════════════════════════
async function loadSettings() {
  const data = await chrome.storage.local.get(["telegramToken","telegramChatId","scanInterval","telegramEnabled"]);
  if (data.telegramToken) $("telegramToken").value = data.telegramToken;
  if (data.telegramChatId) $("telegramChatId").value = data.telegramChatId;
  if (data.scanInterval) $("scanInterval").value = data.scanInterval;
  $("telegramEnabled").checked = data.telegramEnabled !== false; // 기본값 true
}

$("btnSave").addEventListener("click", async () => {
  await chrome.storage.local.set({
    telegramToken: $("telegramToken").value.trim(),
    telegramChatId: $("telegramChatId").value.trim(),
    scanInterval: parseInt($("scanInterval").value) || 1,
    telegramEnabled: $("telegramEnabled").checked,
  });
  await chrome.runtime.sendMessage({ type: "UPDATE_INTERVAL" });
  const t = $("savedToast");
  t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 2000);
});

// 텔레그램 테스트 전송
$("btnTestTelegram").addEventListener("click", async () => {
  const token = $("telegramToken").value.trim();
  const chatId = $("telegramChatId").value.trim();

  if (!token || !chatId) {
    alert("봇 토큰과 채팅 ID를 먼저 입력해주세요.");
    return;
  }

  const btn = $("btnTestTelegram");
  const originalText = btn.textContent;
  btn.textContent = "⏳ 전송 중...";
  btn.disabled = true;

  chrome.runtime.sendMessage({
    type: "RESEND_TELEGRAM",
    token,
    chatId,
    text: "🔔 *[테스트]* 텔레그램 알림 설정이 정상적으로 완료되었습니다\\!",
    boardName: "시스템 테스트"
  }, (resp) => {
    btn.textContent = originalText;
    btn.disabled = false;
    alert("테스트 메시지 전송 요청을 보냈습니다.\n'로그' 탭에서 결과를 확인하세요.");
  });
});

// ── 초기 로드 ─────────────────────────────────────
loadStatus();
loadSettings();
