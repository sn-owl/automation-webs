"""Local recognition artifacts and safe external-model projection.

Recognition keeps raw files, local previews, and masked summaries separate. The
projection used by Hermes intentionally contains no attachment metadata.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from automation.document_preview import preview_attachment
from automation.hermes_sanitize import mask_for_hermes
from automation.models import WorkItem


def _sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def build_recognition_artifacts(item: WorkItem) -> dict[str, Any]:
    """Create bounded local artifacts without changing the source WorkItem."""
    attachments: list[dict[str, Any]] = []
    for attachment in item.attachments:
        path = Path(attachment.raw_ref)
        preview = preview_attachment(path, attachment.type)
        attachments.append(
            {
                "name": attachment.name,
                "type": attachment.type,
                "raw_ref": attachment.raw_ref,
                "raw_sha256": attachment.sha256 or _sha256(path),
                "preview": preview,
                "extraction_status": preview.get("status", "unknown"),
            }
        )
    return {
        "artifact_version": "recognition-v1",
        "task_id": item.task_id,
        "source": {"type": item.source.type, "id": item.source.id},
        "content_digest": item.content_digest or item.computed_content_digest(),
        "body_summary": mask_for_hermes(item.body),
        "title_summary": mask_for_hermes(item.title),
        "attachments": attachments,
    }


def build_recognition_event(item: WorkItem) -> dict[str, Any]:
    """Event-safe recognition projection (C7).

    The recognized event is durable and reaches EventLog validation and
    operators, so it must not carry extracted preview text or local raw_ref
    paths. Only masked summaries, digests, and extraction status cross into the
    event; consumers that need the full preview recompute it from the task
    store (see dashboard.build_task_content_preview).
    """
    artifacts = build_recognition_artifacts(item)
    artifacts["attachments"] = [
        {
            "name": attachment["name"],
            "type": attachment["type"],
            "raw_sha256": attachment["raw_sha256"],
            "extraction_status": attachment["extraction_status"],
        }
        for attachment in artifacts["attachments"]
    ]
    return artifacts


def build_model_projection(item: WorkItem, policy_context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Project only a local masked summary and non-identifying role criteria.

    Neither capture identity nor attachment existence is model input. The
    opaque request identifier correlates proposals without encoding an owner.
    """
    context = policy_context or {}
    allowed = {"roles", "responsibilities", "collaboration", "constraints"}
    if not isinstance(context, dict) or set(context) - allowed:
        raise ValueError("model policy context contains unsupported fields")
    safe_context = {}
    for key, values in context.items():
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise ValueError("model policy criteria must be text lists")
        safe_context[key] = [mask_for_hermes(value)[:500] for value in values[:30]]
    # Deterministic local extractive summary, not an AI preprocessing call.
    text = mask_for_hermes(f"{item.title}\n{item.body}")
    summary = "\n".join(line.strip() for line in text.splitlines() if line.strip())[:2000]
    return {
        "request_id": hashlib.sha256(item.task_id.encode("utf-8")).hexdigest(),
        "summary": summary or "[요약 정보 부족]",
        "policy_context": safe_context,
    }
