from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
import re
from urllib.parse import parse_qs, urlparse

from automation.models import AttachmentRef, SourceRef, WorkItem

_ALLOWED_ATTACHMENT_TYPES = frozenset({"hwpx", "hwp", "pdf", "docx", "xls", "xlsx", "zip", "jpg", "jpeg", "png", "gif"})


class GnuBoardParseError(ValueError):
    pass


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    children: list["_Node"] = field(default_factory=list)
    parts: list[object] = field(default_factory=list)

    def text_content(self) -> str:
        return "".join(
            part.text_content() if isinstance(part, _Node) else str(part)
            for part in self.parts
        )


class _TreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", {})
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self.stack[-1].parts.append("\n")
            return
        node = _Node(tag, {name: value or "" for name, value in attrs})
        self.stack[-1].children.append(node)
        self.stack[-1].parts.append(node)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].parts.append(data)


def parse_gnuboard_html(
    html: str,
    *,
    board_id: str,
    source_url: str,
    scope: str | None = None,
    connector_id: str | None = None,
) -> WorkItem:
    if (scope is None) != (connector_id is None):
        raise GnuBoardParseError("scope and connector_id must be provided together")
    tree = _parse(html)
    article = _find_by_id(tree, "bo_v")
    if article is None or _find_by_id(tree, "flogin") is not None or "권한이 없습니다" in html:
        raise GnuBoardParseError("login or permission page")

    external_id = _external_id(source_url)
    from automation.identity import task_id as make_task_id

    task_id = make_task_id(
        board_id,
        external_id,
        scope=scope,
        connector_id=connector_id,
    )
    title = _clean_inline(_required_text(_find_by_id(tree, "bo_v_title"), "title"))
    info = _find_by_id(tree, "bo_v_info")
    body = _clean_body(_required_text(_find_by_id(tree, "bo_v_con"), "body"))
    extended = scope is not None or connector_id is not None

    return WorkItem(
        task_id=task_id,
        source=SourceRef(type="board", id=board_id, external_id=external_id, url=source_url),
        received_at=_posted_at(_required_text(info, "page info")),
        title=title,
        body=body,
        author=_author(info),
        attachments=tuple(_attachments(tree, board_id, external_id)),
        mask_table_ref=f"local://masks/{task_id}.json",
        contract_version=2 if extended else 1,
        scope=scope or "legacy",
        connector_id=connector_id or "legacy",
        capture_id=task_id,
        provenance={"adapter": "gnuboard", "source_url": source_url} if extended else None,
        # Connector presence alone is not a completion signal. Only the
        # explicit source-status marker is retained as an observation.
        source_completion_observed=_source_completion_observed(tree),
    )


def _source_completion_observed(tree: _Node) -> bool:
    """Read an explicit source completion marker without changing task meaning."""
    for meta in _find_all(tree, tag="meta"):
        if (
            meta.attrs.get("name", "").strip().casefold() == "source-status"
            and meta.attrs.get("content", "").strip().casefold() == "completed"
        ):
            return True
    return False

def _parse(html: str) -> _Node:
    parser = _TreeParser()
    parser.feed(html)
    return parser.root


def _find_by_id(node: _Node, target: str) -> _Node | None:
    if node.attrs.get("id") == target:
        return node
    for child in node.children:
        found = _find_by_id(child, target)
        if found is not None:
            return found
    return None


def _find_all(node: _Node, *, tag: str | None = None, class_name: str | None = None) -> list[_Node]:
    matched = []
    classes = node.attrs.get("class", "").split()
    if (tag is None or node.tag == tag) and (class_name is None or class_name in classes):
        matched.append(node)
    for child in node.children:
        matched.extend(_find_all(child, tag=tag, class_name=class_name))
    return matched


def _required_text(node: _Node | None, field_name: str) -> str:
    if node is None:
        raise GnuBoardParseError(f"missing {field_name}")
    text = node.text_content()
    if not text.strip():
        raise GnuBoardParseError(f"missing {field_name}")
    return text


def _clean_inline(text: str) -> str:
    return " ".join(text.split())


def _clean_body(text: str) -> str:
    lines = [" ".join(line.split()) for line in text.splitlines()]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _posted_at(info_text: str) -> str:
    match = re.search(r"(\d{2})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})", info_text)
    if not match:
        raise GnuBoardParseError("missing posted date")
    year, month, day, hour, minute = match.groups()
    return f"20{year}-{month}-{day}T{hour}:{minute}:00+09:00"


def _author(info: _Node | None) -> str:
    if info is None:
        raise GnuBoardParseError("missing page info")
    for node in _find_all(info, tag="span", class_name="sv_member"):
        text = _clean_inline(node.text_content())
        if text:
            return text
    raise GnuBoardParseError("missing author")


def _attachments(tree: _Node, board_id: str, external_id: str) -> list[AttachmentRef]:
    section = _find_by_id(tree, "bo_v_file")
    if section is None:
        return []

    attachments = []
    for link in _find_all(section, tag="a", class_name="view_file_download"):
        names = _find_all(link, tag="strong")
        name = _clean_inline(names[0].text_content()) if names else ""
        href = link.attrs.get("href", "")
        if not name or not href:
            raise GnuBoardParseError("missing attachment link")
        extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if extension not in _ALLOWED_ATTACHMENT_TYPES:
            raise GnuBoardParseError("unsupported attachment extension")
        attachments.append(
            AttachmentRef(
                name=name,
                type=extension,
                raw_ref=href,
                extracted_ref=f"normalized/{board_id}/{external_id}/attachments/{name}.json",
            )
        )
    return attachments


def _external_id(source_url: str) -> str:
    values = parse_qs(urlparse(source_url).query).get("wr_id", [])
    if not values or not values[0].strip():
        raise GnuBoardParseError("missing wr_id")
    return values[0]
