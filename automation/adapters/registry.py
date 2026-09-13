from __future__ import annotations

from collections.abc import Mapping
from typing import Callable

from automation.adapters.egov import parse_egov_html
from automation.adapters.gnuboard import parse_gnuboard_html
from automation.models import WorkItem


class UnsupportedBoardType(ValueError):
    pass


Adapter = Callable[[str, Mapping[str, str]], WorkItem]


def _gnuboard(html: str, source: Mapping[str, str]) -> WorkItem:
    kwargs = {
        "board_id": source["board_id"],
        "source_url": source["url"],
    }
    if "scope" in source or "connector_id" in source:
        kwargs.update(
            scope=source.get("scope"),
            connector_id=source.get("connector_id"),
        )
    return parse_gnuboard_html(html, **kwargs)


def _egov(html: str, source: Mapping[str, str]) -> WorkItem:
    kwargs = {"source_url": source["url"]}
    if "scope" in source or "connector_id" in source:
        kwargs.update(
            scope=source.get("scope"),
            connector_id=source.get("connector_id"),
        )
    return parse_egov_html(html, **kwargs)

ADAPTERS: dict[str, Adapter] = {
    "gnuboard": _gnuboard,
    "egov": _egov,
}


def normalize_html(board_type: str, html: str, source: Mapping[str, str]) -> WorkItem:
    try:
        adapter = ADAPTERS[board_type]
    except KeyError as exc:
        raise UnsupportedBoardType(f"unsupported board type: {board_type}") from exc
    return adapter(html, source)
