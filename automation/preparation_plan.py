"""Structured, read-only preparation plans for registered projects."""

from __future__ import annotations

import ast
from dataclasses import asdict, dataclass
import codecs
import hashlib
import json
import re
from pathlib import Path
import sqlite3
from typing import Any

from automation.project_profile import (
    MAX_ANALYSIS_FILE_BYTES,
    ProjectProfile,
    discover_files,
)


_JS_SYMBOL = re.compile(
    r"(?:function\s+|class\s+|(?:const|let|var)\s+)([A-Za-z_$][A-Za-z0-9_$]*)"
)
_PROFILE_ID = re.compile(r"\A[a-z0-9][a-z0-9._-]{0,63}\Z")
_SAFE_SEARCH_CHUNK_BYTES = 64 * 1024
_DATABASE_TERMS = {"db", "database", "schema", "sql", "데이터베이스", "스키마"}


@dataclass(frozen=True)
class _SearchFile:
    path: str
    size: int
    path_score: int
    content_score: int
    match_evidence: tuple[dict[str, str], ...]

    @property
    def score(self) -> int:
        return self.path_score + self.content_score


@dataclass(frozen=True)
class _IndexedFile:
    path: str
    sha256: str
    symbols: tuple[str, ...]
    size: int
    path_score: int
    content_score: int
    match_evidence: tuple[dict[str, str], ...]

    @property
    def score(self) -> int:
        return self.path_score + self.content_score


def _symbols(path: Path, content: str) -> tuple[str, ...]:
    if path.suffix.casefold() == ".py":
        try:
            tree = ast.parse(content, filename=path.name)
        except SyntaxError:
            return ()
        return tuple(
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        )[:100]
    if path.suffix.casefold() in {".js", ".jsx", ".ts", ".tsx"}:
        return tuple(dict.fromkeys(_JS_SYMBOL.findall(content)))[:100]
    return ()


def _search_evidence(
    relative: str,
    content: str,
    terms: tuple[str, ...],
) -> tuple[dict[str, str], ...]:
    lowered_path = relative.casefold()
    lowered_name = Path(relative).name.casefold()
    lowered_content = content.casefold()
    evidence: list[dict[str, str]] = []
    for term in terms:
        if term in lowered_name:
            evidence.append({"kind": "filename", "term": term})
        elif term in lowered_path:
            evidence.append({"kind": "path", "term": term})
        if term in lowered_content:
            evidence.append({"kind": "text", "term": term})
    return tuple(evidence)


def _database_schemas(profile: ProjectProfile) -> tuple[list[dict[str, Any]], list[str]]:
    root = Path(profile.root).resolve()
    results: list[dict[str, Any]] = []
    missing: list[str] = []
    for relative in profile.database_paths:
        path = (root / relative).resolve()
        if not path.is_file():
            missing.append("schema_missing")
            results.append(
                {"path": relative, "state": "missing", "reason": "schema_missing", "objects": []}
            )
            continue
        try:
            # Explicit database inputs are observed in SQLite read-only mode.
            # query_only prevents accidental writes even if this code changes.
            connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
            try:
                connection.execute("PRAGMA query_only = ON")
                objects = [
                    {"type": row[0], "name": row[1], "table": row[2], "sql": row[3]}
                    for row in connection.execute(
                        "SELECT type, name, tbl_name, sql FROM sqlite_master "
                        "WHERE type IN ('table', 'index', 'view') ORDER BY type, name"
                    )
                    if not str(row[1]).startswith("sqlite_")
                ]
            finally:
                connection.close()
            results.append({"path": relative, "state": "read", "objects": objects})
        except (OSError, sqlite3.Error):
            missing.append("schema_unreadable")
            results.append(
                {
                    "path": relative,
                    "state": "unreadable",
                    "reason": "schema_unreadable",
                    "objects": [],
                }
            )
    return results, sorted(set(missing))


def _safe_search(
    path: Path,
    root: Path,
    terms: tuple[str, ...],
    skipped_files: list[dict[str, str]],
) -> _SearchFile | None:
    relative = path.relative_to(root).as_posix()
    try:
        size = path.stat().st_size
    except OSError:
        skipped_files.append({"path": relative, "reason": "metadata_unreadable"})
        return None
    if size > MAX_ANALYSIS_FILE_BYTES:
        skipped_files.append({"path": relative, "reason": "file_too_large"})
        return None
    path_evidence = _search_evidence(relative, "", terms)
    decoder = codecs.getincrementaldecoder("utf-8")()
    content_score = 0
    text_evidence: list[dict[str, str]] = []
    tail = ""
    overlap = max((len(term) for term in terms), default=1) - 1
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(_SAFE_SEARCH_CHUNK_BYTES):
                decoded = decoder.decode(chunk).casefold()
                window = tail + decoded
                content_score += sum(
                    window.count(term) - tail.count(term)
                    for term in terms
                )
                text_evidence.extend(
                    evidence
                    for evidence in _search_evidence(relative, window, terms)
                    if evidence["kind"] == "text"
                )
                tail = window[-overlap:] if overlap else ""
            decoded = decoder.decode(b"", final=True).casefold()
            if decoded:
                window = tail + decoded
                content_score += sum(
                    window.count(term) - tail.count(term)
                    for term in terms
                )
                text_evidence.extend(
                    evidence
                    for evidence in _search_evidence(relative, window, terms)
                    if evidence["kind"] == "text"
                )
    except OSError:
        skipped_files.append({"path": relative, "reason": "content_unreadable"})
        return None
    except UnicodeError:
        skipped_files.append({"path": relative, "reason": "not_utf8_text"})
        return None
    evidence = list(path_evidence)
    for item in text_evidence:
        if item not in evidence:
            evidence.append(item)
    # The search is bounded by the registered file-size limit and a fixed
    # chunk budget; it never materializes the complete file in memory.
    return _SearchFile(
        relative,
        size,
        5 * sum(relative.casefold().count(term) for term in terms),
        content_score,
        tuple(evidence),
    )


def _detail_read(
    path: Path,
    root: Path,
    search: _SearchFile,
    terms: tuple[str, ...],
    skipped_files: list[dict[str, str]],
    detail_read_files: list[str],
    symbol_analysis_files: list[str],
) -> _IndexedFile | None:
    relative = search.path
    try:
        raw = path.read_bytes()
    except OSError:
        skipped_files.append({"path": relative, "reason": "content_unreadable"})
        return None
    try:
        content = raw.decode("utf-8")
    except UnicodeError:
        skipped_files.append({"path": relative, "reason": "not_utf8_text"})
        return None
    detail_read_files.append(relative)
    symbols = _symbols(path, content)
    symbol_analysis_files.append(relative)
    evidence = list(_search_evidence(relative, content, terms))
    lowered_symbols = tuple(symbol.casefold() for symbol in symbols)
    for term in terms:
        if any(term in symbol for symbol in lowered_symbols):
            marker = {"kind": "symbol", "term": term}
            if marker not in evidence:
                evidence.append(marker)
    return _IndexedFile(
        path=relative,
        sha256=hashlib.sha256(raw).hexdigest(),
        symbols=symbols,
        size=len(raw),
        path_score=search.path_score,
        content_score=sum(content.casefold().count(term) for term in terms),
        match_evidence=tuple(evidence),
    )


def build_preparation_plan(profile: ProjectProfile, *, query: str) -> dict[str, Any]:
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query is required")
    root = Path(profile.root).resolve()
    terms = tuple(dict.fromkeys(part.casefold() for part in query.split() if part.strip()))
    search_attempted_files: list[str] = []
    searched_files: list[str] = []
    detail_read_files: list[str] = []
    symbol_analysis_files: list[str] = []
    skipped_files: list[dict[str, str]] = []
    search_hits: list[_SearchFile] = []
    for path in discover_files(profile):
        relative = path.relative_to(root).as_posix()
        search_attempted_files.append(relative)
        search = _safe_search(path, root, terms, skipped_files)
        if search is not None:
            searched_files.append(relative)
            if search.score > 0:
                search_hits.append(search)

    # Search ranking is deterministic and bounded. Only these files receive a
    # full decode, digest, and AST/JS symbol pass.
    search_hits = sorted(search_hits, key=lambda entry: (-entry.score, entry.path))[:50]
    indexed: list[_IndexedFile] = []
    for search in search_hits:
        detail = _detail_read(
            root / search.path,
            root,
            search,
            terms,
            skipped_files,
            detail_read_files,
            symbol_analysis_files,
        )
        if detail is not None:
            indexed.append(detail)
    candidates = sorted(indexed, key=lambda entry: (-entry.score, entry.path))

    missing_information: list[str] = []
    if not candidates:
        missing_information.append("location_unknown")
    elif len(candidates) > 1:
        # A ranked list is evidence, not permission to choose a target.  A
        # caller must explicitly resolve every multi-match before any later
        # work can rely on this plan.
        missing_information.append("multiple_candidates")

    database_schemas, database_missing = _database_schemas(profile)
    missing_information.extend(database_missing)
    if any(term in _DATABASE_TERMS for term in terms) and not profile.database_paths:
        missing_information.append("schema_missing")
    missing_information = sorted(set(missing_information))

    candidate_state = (
        "no_candidate"
        if not candidates
        else "multiple_candidates"
        if "multiple_candidates" in missing_information
        else "single_candidate"
    )
    primary = candidates[0] if candidate_state == "single_candidate" else None
    if primary is None:
        confidence = 0.0 if not candidates else 0.5
    else:
        runner_up = candidates[1].score if len(candidates) > 1 else 0
        confidence = round(
            min(0.99, 0.6 + (primary.score - runner_up) / max(primary.score, 1) * 0.39),
            2,
        )

    candidate_values = [
        {
            **asdict(candidate),
            "symbols": list(candidate.symbols),
            "match_evidence": list(candidate.match_evidence),
            "score": candidate.score,
        }
        for candidate in candidates
    ]
    primary_value = candidate_values[0] if primary is not None else None
    evidence_payload = {
        "scope": profile.scope,
        "project_id": profile.project_id,
        "query": query.strip(),
        "search_attempted_files": search_attempted_files,
        "detail_read_files": detail_read_files,
        "symbol_analysis_files": symbol_analysis_files,
        "skipped_files": skipped_files,
        "candidates": candidate_values,
        "database_schemas": database_schemas,
    }
    evidence_digest = hashlib.sha256(
        json.dumps(
            evidence_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    read_evidence = {
        # indexed_paths/read_files retain the existing CLI and dashboard shape:
        # the former is bounded safe-search, the latter is detail-only.
        "indexed_paths": searched_files,
        "search_attempted_files": search_attempted_files,
        "safe_search_files": searched_files,
        "read_files": detail_read_files,
        "detail_read_files": detail_read_files,
        "symbol_analysis_files": symbol_analysis_files,
        "skipped_files": skipped_files,
        "hard_exclusions_applied": True,
        "evidence_digest": evidence_digest,
    }
    return {
        "plan_version": "preparation-v2",
        "scope": profile.scope,
        "project_id": profile.project_id,
        "project_name": profile.name,
        "query": query.strip(),
        "state": "information_needed" if missing_information else "ready",
        "candidate_state": candidate_state,
        "missing_information": missing_information,
        "confidence": confidence,
        "target": {
            "primary": primary_value,
            "alternatives": (
                candidate_values[1:10] if primary_value is not None else candidate_values[:10]
            ),
        },
        "related_units": [
            {"path": candidate["path"], "symbols": candidate["symbols"]}
            for candidate in candidate_values[:10]
        ],
        "impact": {
            "files_considered": len(search_attempted_files),
            "candidate_files": len(candidate_values),
            "database_objects": sum(len(entry["objects"]) for entry in database_schemas),
        },
        "ordered_steps": (
            [
                "사용자가 선택된 프로젝트와 관련 위치를 확인합니다.",
                "표시된 함수·클래스와 요청 영향 범위를 검토합니다.",
                "수정 권한은 부여하지 않고 별도 업무 수행으로 인계합니다.",
            ]
            if not missing_information
            else ["부족 정보 또는 복수 후보를 사용자가 보완·선택합니다."]
        ),
        "risk_flags": [
            "read_only",
            "no_execution",
            "no_tests",
            "no_git_history",
            "no_external_endpoint",
        ],
        "read_evidence": read_evidence,
        "database_schemas": database_schemas,
        "external_ai": {
            "permitted": profile.allow_external_ai_code,
            "used": False,
            "payload": None,
        },
        "next_action": (
            "관련 위치와 분석 순서를 검토하세요."
            if not missing_information
            else "부족 정보와 후보를 확인하세요."
        ),
    }


def export_preparation_report(
    plan: dict[str, Any],
    output_root: str | Path,
    *,
    report_format: str,
) -> Path:
    if report_format not in {"txt", "md"}:
        raise ValueError("report format must be txt or md")
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    project_id = str(plan.get("project_id", "project"))
    if _PROFILE_ID.fullmatch(project_id) is None:
        raise ValueError("plan project_id is unsafe")
    for version in range(1, 10_000):
        destination = root / f"{project_id}_analysis_v{version}.{report_format}"
        try:
            with destination.open("x", encoding="utf-8") as stream:
                if report_format == "txt":
                    stream.write(json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
                else:
                    stream.write(f"# {plan['project_name']} 정적 분석\n\n")
                    stream.write(f"- 상태: `{plan['state']}`\n")
                    stream.write(f"- 신뢰도: `{plan['confidence']}`\n")
                    stream.write(f"- 요청: {plan['query']}\n")
                    stream.write(f"- evidence digest: `{plan['read_evidence']['evidence_digest']}`\n\n")
                    stream.write("## 관련 위치\n\n")
                    for unit in plan["related_units"]:
                        symbols = ", ".join(unit["symbols"]) or "symbol 없음"
                        stream.write(f"- `{unit['path']}` — {symbols}\n")
                    stream.write("\n## 다음 단계\n\n")
                    for step in plan["ordered_steps"]:
                        stream.write(f"- {step}\n")
            return destination
        except FileExistsError:
            continue
    raise ValueError("report version limit exceeded")
