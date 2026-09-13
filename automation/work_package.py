"""Render deterministic, reviewable files for one automation work item."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Iterable, Mapping
from pathlib import Path
from string import Template
from typing import Any

from automation import identity
from automation.classification import Classification
from automation.models import WorkItem


TEMPLATE_DIR = Path(__file__).parents[1] / "templates"


def render_work_package(
    work_item: WorkItem,
    classification: Classification,
    output_root: Path | str,
    *,
    artifacts: Iterable[Mapping[str, Any]] | Mapping[str, Any] = (),
    final_status: str = "review_required",
) -> Path:
    """Render a work item into ``output_root/<task_id>`` and return that path.

    The task id is the only input used for the package directory name.  Titles
    are rendered into the review document and never interpreted as paths.
    Existing files are overwritten so this function is an idempotent view.
    Artifact metadata is copied into the package and re-hashed before it is
    recorded in ``WORKLOG.md``.
    """
    if not isinstance(work_item, WorkItem):
        raise TypeError("work_item must be a WorkItem")
    if not isinstance(classification, Classification):
        raise TypeError("classification must be a Classification")
    if not isinstance(final_status, str) or not final_status.strip():
        raise ValueError("final_status must be a non-empty string")

    # WorkItem currently only checks that task_id is non-blank.  Reuse the
    # identity contract here so a direct caller cannot turn it into a path.
    package_name = identity._normalize_identifier(work_item.task_id, "task_id")
    root = Path(output_root).resolve()
    package = (root / package_name).resolve()
    try:
        package.relative_to(root)
    except ValueError as exc:
        raise ValueError("work package path escapes output root") from exc

    package.mkdir(parents=True, exist_ok=True)
    artifact_dir = package / "artifacts"
    if artifact_dir.is_symlink():
        raise ValueError("artifact directory must not be a symlink")
    artifact_dir.mkdir(exist_ok=True)
    artifact_inputs = _normalize_artifacts(artifacts)
    _clear_artifacts(artifact_dir, _artifact_names(artifact_inputs))
    artifact_records = _copy_artifacts(artifact_inputs, artifact_dir)

    _write_json(package / "source.json", work_item.to_dict())
    _write_json(package / "classification.json", classification.to_dict())
    records = [
        {"path": "source.json", "sha256": _sha256(package / "source.json")},
        {
            "path": "classification.json",
            "sha256": _sha256(package / "classification.json"),
        },
    ]

    action_values = {
        "title": work_item.title,
        "body": work_item.body,
        "responsibility": classification.responsibility,
        "task_type": classification.task_type,
        "size": classification.size,
        "confidence": str(classification.confidence),
    }
    _write_template(package / "ACTION.md", "ACTION.md.txt", action_values)

    artifact_lines = "\n".join(
        f"- `{record['path']}` — `{record['sha256']}`" for record in artifact_records
    )
    if not artifact_lines:
        artifact_lines = "- None"
    record_lines = "\n".join(
        f"- `{record['path']}` — `{record['sha256']}`" for record in records
    )
    worklog_values = {
        "task_id": package_name,
        "final_status": final_status,
        "artifacts": artifact_lines,
        "records": record_lines,
    }
    _write_template(package / "WORKLOG.md", "WORKLOG.md.txt", worklog_values)
    return package


def _normalize_artifacts(
    artifacts: Iterable[Mapping[str, Any]] | Mapping[str, Any] | None,
) -> list[Mapping[str, Any]]:
    if artifacts is None:
        return []
    if isinstance(artifacts, Mapping):
        if "path" in artifacts:
            return [artifacts]
        if "artifacts" not in artifacts:
            raise ValueError("artifact mapping must contain path or artifacts")
        artifacts = artifacts["artifacts"]
        if artifacts is None:
            return []
    if isinstance(artifacts, (str, bytes)):
        raise ValueError("artifacts must be artifact records")
    return list(artifacts)


def _artifact_names(artifacts: Iterable[Mapping[str, Any]]) -> set[str]:
    names: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, Mapping):
            raise ValueError("artifact records must be objects")
        source_value = artifact.get("path")
        if not isinstance(source_value, str) or not source_value.strip():
            raise ValueError("artifact path must be a non-empty string")
        name = Path(source_value).name
        if not name or name in {".", ".."}:
            raise ValueError("artifact path must name a file")
        if name in names:
            raise ValueError(f"duplicate artifact filename: {name}")
        names.add(name)
    return names


def _clear_artifacts(artifact_dir: Path, keep: set[str]) -> None:
    for child in artifact_dir.iterdir():
        if child.is_symlink():
            raise ValueError(f"artifact destination must not be a symlink: {child.name}")
        if child.is_file():
            if child.name not in keep:
                child.unlink()
        else:
            raise ValueError(f"artifact directory contains non-file: {child.name}")


def _copy_artifacts(
    artifacts: Iterable[Mapping[str, Any]], artifact_dir: Path
) -> list[dict[str, str]]:

    records: list[dict[str, str]] = []
    destinations: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, Mapping):
            raise ValueError("artifact records must be objects")
        source_value = artifact.get("path")
        if not isinstance(source_value, str) or not source_value.strip():
            raise ValueError("artifact path must be a non-empty string")
        source = Path(source_value)
        if not source.is_file():
            raise FileNotFoundError(f"artifact does not exist: {source}")

        name = source.name
        if not name or name in {".", ".."}:
            raise ValueError("artifact path must name a file")
        if name in destinations:
            raise ValueError(f"duplicate artifact filename: {name}")
        destinations.add(name)

        digest = _sha256(source)
        expected = artifact.get("sha256")
        if expected is not None and expected != digest:
            raise ValueError(f"artifact sha256 mismatch: {source}")
        destination = artifact_dir / name
        if destination.is_symlink():
            raise ValueError(f"artifact destination must not be a symlink: {name}")
        if source.resolve() != destination.resolve():
            shutil.copyfile(source, destination)
        records.append({"path": f"artifacts/{name}", "sha256": digest})

    return sorted(records, key=lambda record: record["path"])


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: object) -> None:
    content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.write_bytes(content.encode("utf-8"))


def _write_template(path: Path, template_name: str, values: Mapping[str, str]) -> None:
    template = Template((TEMPLATE_DIR / template_name).read_text(encoding="utf-8"))
    path.write_bytes(template.substitute(values).encode("utf-8"))
