from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from typing import Any


def _digest_payload(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _optional_text(data: dict[str, Any], key: str, default: str) -> str:
    value = data.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _optional_mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return dict(value)


@dataclass(frozen=True)
class SourceRef:
    type: str
    id: str
    external_id: str
    url: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SourceRef":
        if not isinstance(data, dict):
            raise ValueError("source must be an object")
        allowed = {"type", "id", "external_id", "url"}
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"unknown source field: {sorted(unknown)[0]}")
        return cls(
            type=_required_text(data, "type"),
            id=_required_text(data, "id"),
            external_id=_required_text(data, "external_id"),
            url=(
                _optional_text(data, "url", "")
                if "url" in data
                else None
            ),
        )

    def to_dict(self) -> dict[str, str]:
        payload = {
            "type": self.type,
            "id": self.id,
            "external_id": self.external_id,
        }
        if self.url is not None:
            payload["url"] = self.url
        return payload


@dataclass(frozen=True)
class AttachmentRef:
    name: str
    type: str
    raw_ref: str
    extracted_ref: str
    sha256: str | None = None
    status: str = "complete"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AttachmentRef":
        if not isinstance(data, dict):
            raise ValueError("attachment must be an object")
        allowed = {"name", "type", "raw_ref", "extracted_ref", "sha256", "status"}
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"unknown attachment field: {sorted(unknown)[0]}")
        sha256 = data.get("sha256")
        if sha256 is not None and (
            not isinstance(sha256, str)
            or len(sha256) != 64
            or any(char not in "0123456789abcdef" for char in sha256)
        ):
            raise ValueError("sha256 must be a sha256 digest")
        status = data.get("status", "complete")
        if status not in {"complete", "incomplete", "failed"}:
            raise ValueError("status must be complete, incomplete, or failed")
        return cls(
            name=_required_text(data, "name"),
            type=_required_text(data, "type"),
            raw_ref=_required_text(data, "raw_ref"),
            extracted_ref=_required_text(data, "extracted_ref"),
            sha256=sha256,
            status=status,
        )

    def to_dict(self) -> dict[str, str]:
        payload = {
            "name": self.name,
            "type": self.type,
            "raw_ref": self.raw_ref,
            "extracted_ref": self.extracted_ref,
        }
        if self.sha256 is not None:
            payload["sha256"] = self.sha256
        if self.status != "complete":
            payload["status"] = self.status
        return payload


@dataclass(frozen=True)
class WorkItem:
    task_id: str
    source: SourceRef
    received_at: str
    title: str
    body: str
    author: str | None
    attachments: tuple[AttachmentRef, ...]
    mask_table_ref: str
    contract_version: int = 1
    scope: str = "legacy"
    connector_id: str = "legacy"
    capture_id: str | None = None
    revision: int = 1
    revision_digest: str | None = None
    content_digest: str | None = None
    completeness: str = "complete"
    provenance: dict[str, Any] | None = None
    source_completion_observed: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkItem":
        if not isinstance(data, dict):
            raise ValueError("work item must be an object")
        allowed = {
            "task_id", "source", "received_at", "title", "body", "author",
            "attachments", "mask_table_ref", "contract_version", "scope",
            "connector_id", "capture_id", "revision", "revision_digest",
            "content_digest", "completeness", "provenance",
            "source_completion_observed",
        }
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"unknown work item field: {sorted(unknown)[0]}")
        received_at = _required_text(data, "received_at")
        _validate_iso_datetime(received_at)
        attachments = data.get("attachments", [])
        if not isinstance(attachments, list):
            raise ValueError("attachments must be a list")
        extended = any(
            key in data
            for key in (
                "contract_version",
                "scope",
                "connector_id",
                "capture_id",
                "revision",
                "revision_digest",
                "content_digest",
                "completeness",
                "provenance",
                "source_completion_observed",
            )
        )
        contract_version = data.get("contract_version", 2 if extended else 1)
        if type(contract_version) is not int or contract_version < 1:
            raise ValueError("contract_version must be a positive integer")
        if contract_version >= 2:
            required_v2 = {
                "scope",
                "connector_id",
                "capture_id",
                "revision",
                "completeness",
                "provenance",
                "source_completion_observed",
            }
            missing = sorted(required_v2 - set(data))
            if missing:
                raise ValueError(f"v2 fields are required: {', '.join(missing)}")
        revision = data.get("revision", 1)
        if type(revision) is not int or revision < 1:
            raise ValueError("revision must be a positive integer")
        completeness = data.get("completeness", "complete")
        if completeness not in {"complete", "incomplete", "failed"}:
            raise ValueError("completeness must be complete, incomplete, or failed")
        source_completion_observed = data.get("source_completion_observed", False)
        if type(source_completion_observed) is not bool:
            raise ValueError("source_completion_observed must be boolean")
        capture_id = data.get("capture_id")
        if contract_version >= 2 and (not isinstance(capture_id, str) or not capture_id.strip()):
            raise ValueError("capture_id must be a non-empty string")
        revision_digest = data.get("revision_digest")
        content_digest = data.get("content_digest")
        for field, digest in (("revision_digest", revision_digest), ("content_digest", content_digest)):
            if digest is not None and (not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)):
                raise ValueError(f"{field} must be a sha256 digest")
        return cls(
            task_id=_required_text(data, "task_id"),
            source=SourceRef.from_dict(_required_mapping(data, "source")),
            received_at=received_at,
            title=_required_text(data, "title"),
            body=_required_text(data, "body"),
            author=(
                _optional_text(data, "author", "")
                if "author" in data
                else None
            ),
            attachments=tuple(AttachmentRef.from_dict(item) for item in attachments),
            mask_table_ref=_required_text(data, "mask_table_ref"),
            contract_version=contract_version,
            scope=_optional_text(data, "scope", "legacy"),
            connector_id=_optional_text(data, "connector_id", "legacy"),
            capture_id=capture_id,
            revision=revision,
            revision_digest=revision_digest,
            content_digest=content_digest,
            completeness=completeness,
            provenance=_optional_mapping(data, "provenance"),
            source_completion_observed=source_completion_observed,
        )

    def meaningful_payload(self) -> dict[str, Any]:
        return {
            "source": self.source.to_dict(),
            "title": self.title,
            "body": self.body,
            "author": self.author,
            "attachments": [
                {"name": item.name, "type": item.type, "sha256": item.sha256, "status": item.status}
                for item in self.attachments
            ],
        }

    def computed_content_digest(self) -> str:
        return _digest_payload(self.meaningful_payload())

    def computed_revision_digest(self) -> str:
        return self.computed_content_digest()

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "task_id": self.task_id,
            "source": self.source.to_dict(),
            "received_at": self.received_at,
            "title": self.title,
            "body": self.body,
            "attachments": [attachment.to_dict() for attachment in self.attachments],
            "mask_table_ref": self.mask_table_ref,
        }
        if self.author is not None:
            payload["author"] = self.author
        if self.contract_version >= 2:
            payload.update(
                {
                    "contract_version": self.contract_version,
                    "scope": self.scope,
                    "connector_id": self.connector_id,
                    "capture_id": self.capture_id or self.task_id,
                    "revision": self.revision,
                    "revision_digest": self.revision_digest or self.computed_revision_digest(),
                    "content_digest": self.content_digest or self.computed_content_digest(),
                    "completeness": self.completeness,
                    "provenance": dict(self.provenance or {}),
                    "source_completion_observed": self.source_completion_observed,
                }
            )
        return payload


def _required(data: dict[str, Any], key: str) -> Any:
    try:
        return data[key]
    except KeyError as exc:
        raise ValueError(f"{key} is required") from exc


def _required_text(data: dict[str, Any], key: str) -> str:
    value = _required(data, key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required")
    return value


def _required_mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = _required(data, key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value


def _validate_iso_datetime(value: str) -> None:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("received_at must be ISO 8601") from exc
