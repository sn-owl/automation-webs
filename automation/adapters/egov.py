from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from automation.adapters.gnuboard import (
    _ALLOWED_ATTACHMENT_TYPES,
    _Node,
    _clean_body,
    _clean_inline,
    _find_all,
    _find_by_id,
    _parse,
)
from automation.models import AttachmentRef, SourceRef, WorkItem


class EgovParseError(ValueError):
    pass


def parse_egov_html(
    html: str,
    *,
    source_url: str,
    scope: str | None = None,
    connector_id: str | None = None,
) -> WorkItem:
    if (scope is None) != (connector_id is None):
        raise EgovParseError("scope and connector_id must be provided together")
    tree = _parse(html)
    if _find_by_id(tree, "loginForm") is not None or _find_by_id(tree, "view") is None:
        raise EgovParseError("login page")

    fields = _table_fields(tree)
    progress = _required_field(fields, "진행구분")
    work_type = _required_field(fields, "작업구분")
    external_id = _external_id(source_url)
    body = _body(tree)
    from automation.identity import task_id as make_task_id

    task_id = make_task_id(
        "egov",
        external_id,
        scope=scope,
        connector_id=connector_id,
    )
    extended = scope is not None or connector_id is not None

    return WorkItem(
        task_id=task_id,
        source=SourceRef(type="board", id="egov", external_id=external_id, url=source_url),
        received_at=_posted_at(_required_field(fields, "등록일")),
        title=_title(tree),
        body=f"작업구분: {work_type}\n진행구분: {progress}\n\n{body}",
        author=_required_field(fields, "작성자"),
        attachments=tuple(_attachments(tree, external_id)),
        mask_table_ref=f"local://masks/{task_id}.json",
        contract_version=2 if extended else 1,
        scope=scope or "legacy",
        connector_id=connector_id or "legacy",
        capture_id=task_id,
        provenance={"adapter": "egov", "source_url": source_url} if extended else None,
        # C1: scope/connector presence is a capture channel, not a source
        # completion signal. Do not claim observation without a parsed marker.
        # ponytail: egov's 진행구분 could feed a real marker once its
        # completion vocabulary is confirmed (contract decision).
        source_completion_observed=False,
    )


def _title(tree: _Node) -> str:
    for cell in _find_all(tree, tag="td", class_name="subject"):
        title = _clean_inline(cell.text_content())
        if title:
            return title
    raise EgovParseError("missing title")


def _table_fields(tree: _Node) -> dict[str, str]:
    fields: dict[str, str] = {}
    for row in _find_all(tree, tag="tr"):
        label = ""
        for cell in [child for child in row.children if child.tag in {"th", "td"}]:
            text = _clean_inline(cell.text_content())
            if cell.tag == "th":
                label = _label(text)
            elif label:
                fields[label] = text
                label = ""
    return fields


def _label(text: str) -> str:
    return text.replace(" ", "").replace("\xa0", "")


def _required_field(fields: dict[str, str], name: str) -> str:
    value = fields.get(name, "")
    if not value.strip():
        raise EgovParseError(f"missing {name}")
    return value


def _body(tree: _Node) -> str:
    for row in _find_all(tree, tag="tr"):
        cells = [child for child in row.children if child.tag == "td"]
        if len(cells) == 1 and cells[0].attrs.get("colspan") == "6" and cells[0].attrs.get("class") != "subject":
            body = _clean_body(cells[0].text_content())
            if body:
                return body
    raise EgovParseError("missing body")


def _attachments(tree: _Node, external_id: str) -> list[AttachmentRef]:
    attach_lists = _find_all(tree, tag="ul", class_name="attach")
    if not attach_lists:
        return []

    attachments = []
    seen: set[tuple[str, str]] = set()
    for link in _find_all(attach_lists[0], tag="a"):
        name = _clean_inline(link.text_content())
        href = link.attrs.get("href", "")
        if "." not in name or not href or name == "다운받기":
            continue
        key = (name, href)
        if key in seen:
            continue
        seen.add(key)
        extension = name.rsplit(".", 1)[-1].lower()
        if extension not in _ALLOWED_ATTACHMENT_TYPES:
            raise EgovParseError("unsupported attachment extension")
        attachments.append(
            AttachmentRef(
                name=name,
                type=extension,
                raw_ref=href,
                extracted_ref=f"normalized/egov/{external_id}/attachments/{name}.json",
            )
        )
    return attachments


def _posted_at(value: str) -> str:
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})", value.strip())
    if not match:
        raise EgovParseError("missing 등록일")
    year, month, day, hour, minute = match.groups()
    return f"{year}-{month}-{day}T{hour}:{minute}:00+09:00"


def _external_id(source_url: str) -> str:
    query = parse_qs(urlparse(source_url).query)
    for key in ("nttId", "wr_id", "id"):
        values = query.get(key, [])
        if values and values[0].strip():
            return values[0]
    raise EgovParseError("missing external id")
