"""Completion-aware handoff from the Downloads archive to Core's inbox."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from automation.adapters.registry import normalize_html
from automation.identity import task_id
from automation.locking import file_lock


_READY = "_READY"
_MANIFEST = "manifest.json"
_WORK_ITEM = "work_item.json"
_CAPTURE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ADAPTERS = frozenset(("gnuboard", "egov"))


_OBSERVATION = "source_observation.json"
_OBSERVATION_ROOT = "source-observations"
_OBSERVATION_KINDS = frozenset(("source_completion_observed", "source_error", "source_recovered"))
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]*$")
_OBSERVATION_ALLOWED = frozenset(("schema_version", "observation_id", "kind", "observed_at", "source", "external_id", "code", "stage", "retryable", "last_success_at", "next_action", "prior_error_code"))

def _safe_observation_id(value: object) -> bool:
    return isinstance(value, str) and bool(_CAPTURE_ID.fullmatch(value))

def _validate_observation_bytes(bundle: Path, *, expected_observation_id: str | None = None) -> tuple[str, bytes] | None:
    try:
        if not _is_directory(bundle):
            return None
        observation_id_from_name = bundle.name.removesuffix("--source-observation")
        if expected_observation_id is None and not _safe_observation_id(observation_id_from_name):
            return None
        if expected_observation_id is not None and not _safe_observation_id(expected_observation_id):
            return None
        if {item.name for item in bundle.iterdir()} != {_OBSERVATION, _READY}:
            return None
        if not _safe_regular_file(bundle, _OBSERVATION) or not _safe_regular_file(bundle, _READY):
            return None
        observation_bytes = (bundle / _OBSERVATION).read_bytes()
        marker = (bundle / _READY).read_text(encoding="utf-8")
        payload = json.loads(observation_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    observation_id = payload.get("observation_id")
    expected_id = expected_observation_id or observation_id_from_name
    if not _safe_observation_id(observation_id) or observation_id != expected_id:
        return None
    if marker not in (observation_id, observation_id + "\n"):
        return None
    if bundle.name != f"{observation_id}--source-observation" and expected_observation_id is None:
        return None
    if set(payload) - _OBSERVATION_ALLOWED or type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
        return None
    kind = payload.get("kind")
    if not isinstance(kind, str) or kind not in _OBSERVATION_KINDS:
        return None
    if not isinstance(payload.get("observed_at"), str) or not _valid_datetime(payload["observed_at"]):
        return None
    source = payload.get("source")
    if not isinstance(source, dict) or set(source) != {"type", "id", "adapter"}:
        return None
    if source.get("type") != "board" or not _safe_observation_id(source.get("id")) or source.get("adapter") not in _ADAPTERS:
        return None
    required = {"schema_version", "observation_id", "kind", "observed_at", "source"}
    if kind == "source_completion_observed":
        required.add("external_id")
        if not _safe_observation_id(payload.get("external_id")):
            return None
    elif kind == "source_error":
        required.update({"code", "stage", "retryable", "next_action"})
        if not isinstance(payload.get("code"), str) or not _SAFE_CODE.fullmatch(payload["code"]):
            return None
        if not isinstance(payload.get("stage"), str) or not _SAFE_CODE.fullmatch(payload["stage"]):
            return None
        next_action = payload.get("next_action")
        if type(payload.get("retryable")) is not bool or not isinstance(next_action, str) or not 1 <= len(next_action) <= 200 or any(ord(char) < 0x20 for char in next_action):
            return None
        if "last_success_at" in payload:
            if not isinstance(payload["last_success_at"], str) or not _valid_datetime(payload["last_success_at"]):
                return None
            required.add("last_success_at")
    else:
        required.add("prior_error_code")
        if not isinstance(payload.get("prior_error_code"), str) or not _SAFE_CODE.fullmatch(payload["prior_error_code"]):
            return None
    if set(payload) != required:
        return None
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    lowered = encoded.casefold()
    if any(term in lowered for term in ("http://", "https://", "password", "secret", "token", "cookie", "authorization")):
        return None
    return observation_id, observation_bytes

def accept_source_observation(download_dir: Path, inbox_root: Path) -> Path | None:
    source = Path(download_dir)
    validated = _validate_observation_bytes(source)
    if validated is None:
        return None
    observation_id, observation_bytes = validated
    root = Path(inbox_root)
    try:
        root.mkdir(parents=True, exist_ok=True)
        if not _is_directory(root) or _has_symlink_component(root.absolute(), Path(root.absolute().anchor)):
            return None
        destination_parent = root / _OBSERVATION_ROOT
        destination_parent.mkdir(parents=True, exist_ok=True)
        if _has_symlink_component(destination_parent.absolute(), Path(root.absolute().anchor)):
            return None
    except (OSError, RuntimeError):
        return None
    destination = destination_parent / source.name
    claim = root / f".{observation_id}.claim"
    claim_descriptor = _acquire_claim(claim)
    if claim_descriptor is None:
        return None
    temporary: Path | None = None
    try:
        if destination.is_symlink():
            return None
        if destination.exists():
            existing = destination / _OBSERVATION
            declared = (_OBSERVATION, _READY)
            if _safe_regular_file(destination, _OBSERVATION) and _safe_regular_file(destination, _READY) and _same_declared_content(source, destination, declared):
                _remove_tree(source)
                return existing
            return None
        declared = (_OBSERVATION, _READY)
        temporary = Path(tempfile.mkdtemp(prefix=f".{observation_id}.", dir=destination_parent))
        _copy_declared_files(source, temporary, declared)
        _fsync_files(temporary, declared)
        copied = _validate_observation_bytes(temporary, expected_observation_id=observation_id)
        if copied is None or copied[1] != observation_bytes or not _same_declared_content(source, temporary, declared):
            return None
        os.replace(temporary, destination)
        temporary = None
        _fsync_directory(destination_parent)
        _remove_tree(source)
        return destination / _OBSERVATION
    except (OSError, shutil.Error, ValueError):
        return None
    finally:
        if temporary is not None:
            _remove_tree(temporary)
        _release_claim(claim, claim_descriptor)

def ingest_source_observation(accepted_path: Path, *, root: Path, scope: str, connector_id: str) -> dict[str, object] | None:
    validated = _validate_observation_bytes(Path(accepted_path).parent)
    if validated is None or not _safe_observation_id(scope) or not _safe_observation_id(connector_id):
        return None
    try:
        payload = json.loads(validated[1].decode("utf-8"))
        from automation.events import EventLog
        from automation.task_store import get_task
        source = payload["source"]
        task = None
        if payload["kind"] == "source_completion_observed":
            source_key = source["id"] if source["adapter"] == "gnuboard" else "egov"
            task = get_task(task_id(source_key, payload["external_id"], scope=scope, connector_id=connector_id), root=root)
            if (task.scope, task.connector_id, task.source.type, task.source.id, task.source.external_id) != (scope, connector_id, source["type"], source_key, payload["external_id"]):
                return None
        identity = {"scope": scope, "connector_id": connector_id, "observation_id": payload["observation_id"]}
        event_id = "source-observation-" + hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        event = {"event_id": event_id, "type": "source_observation", "stage": "source", "scope": scope, "connector_id": connector_id, "source": source, "observed_at": payload["observed_at"], "observation_id": payload["observation_id"], "state": payload["kind"]}
        if payload["kind"] == "source_error":
            event["details"] = {"error": {key: payload[key] for key in ("code", "stage", "retryable", "next_action", "last_success_at") if key in payload}}
        elif payload["kind"] == "source_completion_observed":
            event["external_id"] = payload["external_id"]
            event.update({"task_id": task.task_id, "task_version": task.revision, "content_digest": task.computed_content_digest()})
        else:
            event["details"] = {"recovery": {"prior_error_code": payload["prior_error_code"]}}
        log_path = Path(root) / "state" / "events.jsonl"
        with file_lock(log_path.with_name("events.jsonl.lock")):
            existing = next((item for item in EventLog(log_path).read() if item.get("event_id") == event_id), None)
            if existing is not None:
                if existing != event:
                    return None
                return existing
            EventLog(log_path).append(event)
        return event
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError, RuntimeError):
        return None
def accept_completed_download(download_dir: Path, inbox_root: Path) -> Path | None:
    """Atomically accept one complete extension bundle."""

    source = Path(download_dir)
    if not _is_directory(source):
        return None
    validated = _validate_bundle(source)
    if validated is None:
        return None
    capture_id, declared, work_item_bytes = validated

    root = Path(inbox_root)
    try:
        source_real = source.resolve()
        root_real = root.resolve()
    except (OSError, RuntimeError):
        return None
    if root_real == source_real or source_real in root_real.parents:
        return None
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    if not _is_directory(root) or _has_symlink_component(root.absolute(), Path(root.absolute().anchor)):
        return None

    claim = root / f".{capture_id}.claim"
    claim_descriptor = _acquire_claim(claim)
    if claim_descriptor is None:
        return None
    try:
        destination = root / capture_id
        if destination.is_symlink():
            return None
        if destination.exists():
            destination_json = destination / _WORK_ITEM
            if (
                not destination.is_dir()
                or not _same_declared_content(source, destination, declared)
                or not _safe_regular_file(destination, _WORK_ITEM)
                or not _same_bytes_to_bytes(destination_json, work_item_bytes)
            ):
                return None
            if source.resolve() != destination.resolve():
                _remove_tree(source)
            return destination_json

        temporary: Path | None = None
        try:
            temporary = Path(tempfile.mkdtemp(prefix=f".{capture_id}.", dir=root))
            _copy_declared_files(source, temporary, declared)
            copied = _validate_bundle(temporary, capture_id=capture_id)
            if copied is None or copied[0] != capture_id or copied[2] != work_item_bytes:
                return None
            (temporary / _WORK_ITEM).write_bytes(work_item_bytes)
            _fsync_files(temporary, (*declared, _WORK_ITEM))
            if not _same_declared_content(source, temporary, declared):
                return None
            if not _same_bytes_to_bytes(temporary / _WORK_ITEM, work_item_bytes):
                return None
            os.replace(temporary, destination)
            temporary = None
            _fsync_directory(root)
        except (OSError, shutil.Error, ValueError):
            return None
        finally:
            if temporary is not None:
                _remove_tree(temporary)

        if source.resolve() != destination.resolve():
            _remove_tree(source)
        return destination / _WORK_ITEM
    finally:
        _release_claim(claim, claim_descriptor)


def _acquire_claim(path: Path) -> int | None:
    try:
        import fcntl
    except ImportError:
        try:
            import msvcrt
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            return descriptor
        except (OSError, ValueError):
            try:
                os.close(descriptor)
            except (OSError, UnboundLocalError):
                pass
            return None
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor
    except BlockingIOError:
        try:
            os.close(descriptor)
        except (OSError, UnboundLocalError):
            pass
        return None
    except OSError:
        try:
            os.close(descriptor)
        except (OSError, UnboundLocalError):
            pass
        return None


def _release_claim(path: Path, descriptor: int) -> None:
    try:
        import fcntl
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    except ImportError:
        try:
            import msvcrt
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        except (OSError, ValueError):
            pass
    except OSError:
        pass
    try:
        os.close(descriptor)
    except OSError:
        pass
    try:
        path.unlink()
    except OSError:
        pass
def _validate_bundle(bundle: Path, *, capture_id: str | None = None) -> tuple[str, tuple[str, ...], bytes] | None:
    if not _is_regular_file(bundle / _READY) or not _is_regular_file(bundle / _MANIFEST):
        return None


    try:
        manifest = json.loads((bundle / _MANIFEST).read_text(encoding="utf-8"))
        ready = (bundle / _READY).read_text(encoding="utf-8")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
        return None

    bundle_capture_id = manifest.get("capture_id")
    if not isinstance(bundle_capture_id, str) or not _CAPTURE_ID.fullmatch(bundle_capture_id):
        return None
    if capture_id is None:
        capture_id = bundle_capture_id
        if bundle.name != capture_id and not bundle.name.startswith(f"{capture_id}--"):
            return None
    if bundle_capture_id != capture_id or ready not in (capture_id, capture_id + "\n"):
        return None
    source = manifest.get("source")
    if not isinstance(source, dict) or source.get("type") != "board":
        return None
    for key in ("id", "adapter", "external_id", "url"):
        if not isinstance(source.get(key), str) or not source[key].strip():
            return None
    if (
        not _CAPTURE_ID.fullmatch(source["id"])
        or not _CAPTURE_ID.fullmatch(source["external_id"])
        or source["adapter"] not in _ADAPTERS
        or not _safe_source_url(source["url"])
    ):
        return None
    scope = source.get("scope")
    connector_id = source.get("connector_id")
    if (scope is None) != (connector_id is None):
        return None
    if scope is not None and (
        not isinstance(scope, str)
        or not _CAPTURE_ID.fullmatch(scope)
        or not isinstance(connector_id, str)
        or not _CAPTURE_ID.fullmatch(connector_id)
    ):
        return None
    if not isinstance(manifest.get("captured_at"), str) or not _valid_datetime(manifest["captured_at"]):
        return None
    if _url_identity(source["url"], source["adapter"]) != source["external_id"]:
        return None
    board_id = _url_board_id(source["url"], source["adapter"])
    if board_id is not None and board_id.lower() != source["id"].lower():
        return None

    page = manifest.get("page")
    if not isinstance(page, dict) or not isinstance(page.get("path"), str):
        return None
    page_path = _safe_relative_path(page["path"])
    page_hash = page.get("sha256")
    if page_path is None or not isinstance(page_hash, str) or not _SHA256.fullmatch(page_hash):
        return None
    if not _safe_regular_file(bundle, page_path):
        return None
    try:
        if _sha256(bundle / page_path) != page_hash:
            return None
    except (OSError, ValueError):
        return None
    expected_capture = f"{source['id'].lower()}-{source['external_id'].lower()}-{page_hash[:8]}"
    if capture_id != expected_capture:
        return None

    attachments = manifest.get("attachments")
    if not isinstance(attachments, list):
        return None
    declared: list[str] = [_MANIFEST, _READY, page_path]
    attachment_paths_by_name: dict[str, str] = {}
    attachment_names: set[str] = set()
    seen = set(declared)
    for attachment in attachments:
        if not isinstance(attachment, dict):
            return None
        name, path, source_url = attachment.get("name"), attachment.get("path"), attachment.get("source_url")
        if not isinstance(name, str) or not name.strip() or not isinstance(path, str):
            return None
        name_key = name.casefold()
        if name_key in attachment_names:
            return None
        attachment_names.add(name_key)
        if (
            "/" in name
            or "\\" in name
            or name in (".", "..")
            or path != f"attachments/{name}"
            or not isinstance(source_url, str)
            or not source_url.strip()
            or not _safe_source_url(source_url)
        ):
            return None
        attachment_path = _safe_relative_path(path)
        if attachment_path is None or attachment_path in seen or not _safe_regular_file(bundle, attachment_path):
            return None
        seen.add(attachment_path)
        declared.append(attachment_path)
        attachment_paths_by_name[name] = attachment_path

    try:
        html = (bundle / page_path).read_text(encoding="utf-8")
        parser_url = _normalizer_url(source["url"], source["adapter"], source["external_id"])
        source_input = {"url": parser_url}
        if scope is not None:
            source_input.update({"scope": scope, "connector_id": connector_id})
        if source["adapter"] == "gnuboard":
            source_input["board_id"] = source["id"]
        work_item = normalize_html(source["adapter"], html, source_input)
        if scope is None:
            expected_task_id = (
                f"{source['id']}-{source['external_id']}"
                if source["adapter"] == "gnuboard"
                else f"egov-{source['external_id']}"
            )
        else:
            from automation.identity import task_id as make_task_id

            expected_task_id = make_task_id(
                source["id"] if source["adapter"] == "gnuboard" else "egov",
                source["external_id"],
                scope=scope,
                connector_id=connector_id,
            )
        if work_item.task_id != expected_task_id or work_item.source.external_id != source["external_id"]:
            return None
        local_attachments = []
        for attachment in work_item.attachments:
            manifest_name = attachment.name if attachment.name in attachment_paths_by_name else _safe_name(attachment.name)
            local_path = attachment_paths_by_name.get(manifest_name)
            if local_path is None:
                return None
            local_attachments.append(
                replace(attachment, name=manifest_name, raw_ref=local_path, extracted_ref=local_path)
            )
        work_item = replace(
            work_item,
            source=replace(work_item.source, url=source["url"]),
            attachments=tuple(local_attachments),
            provenance={
                **(work_item.provenance or {}),
                "capture_id": capture_id,
                "source": source["id"],
            },
        )
        work_item_bytes = (json.dumps(work_item.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, RecursionError):
        return None
    return capture_id, tuple(declared), work_item_bytes


def _safe_source_url(value: str) -> bool:
    if any(ord(char) < 0x20 for char in value):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return not parsed.username and not parsed.password

def _url_query(url: str) -> dict[str, list[str]]:
    parsed = urlsplit(url)
    query: dict[str, list[str]] = {}
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        query.setdefault(key, []).append(value)
    return query


def _url_identity(url: str, adapter: str) -> str | None:
    query = _url_query(url)
    keys = ("wr_id",) if adapter == "gnuboard" else ("dataSid", "nttId", "wr_id", "id")
    for key in keys:
        if query.get(key):
            return query[key][0]
    return None


def _url_board_id(url: str, adapter: str) -> str | None:
    query = _url_query(url)
    key = "bo_table" if adapter == "gnuboard" else "boardId"
    return query.get(key, [None])[0]


def _normalizer_url(url: str, adapter: str, external_id: str) -> str:
    if adapter != "egov":
        return url
    query = _url_query(url)
    if any(query.get(key) for key in ("nttId", "wr_id", "id")):
        return url
    parsed = urlsplit(url)
    params = parse_qsl(parsed.query, keep_blank_values=True)
    params.append(("id", external_id))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(params), parsed.fragment))


def _valid_datetime(value: str) -> bool:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _safe_relative_path(value: str) -> str | None:
    if not value or value.startswith(("/", "\\")) or "\\" in value:
        return None
    try:
        path = Path(value)
    except (OSError, ValueError):
        return None
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        return None
    return value

def _safe_name(name: str) -> str:
    base = re.split(r"[\\/]", str(name))[-1]
    cleaned = re.sub(r"[:*?\"<>|\r\n\t]", "_", base)
    cleaned = re.sub(r"\.{2,}", ".", cleaned)
    cleaned = re.sub(r"^[.\s]+|[.\s]+$", "", cleaned)[:120]
    return cleaned or "unnamed"


def _has_symlink_component(path: Path, stop: Path) -> bool:
    current = path
    try:
        while current != stop:
            if current.is_symlink():
                return True
            current = current.parent
    except OSError:
        return True
    return False


def _safe_regular_file(bundle: Path, relative: str) -> bool:
    path = bundle / relative
    if not _is_regular_file(path):
        return False
    return not _has_symlink_component(path, bundle)


def _is_directory(path: Path) -> bool:
    try:
        return path.is_dir() and not path.is_symlink()
    except (OSError, ValueError):
        return False


def _is_regular_file(path: Path) -> bool:
    try:
        return path.is_file() and not path.is_symlink()
    except (OSError, ValueError):
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _same_declared_content(left: Path, right: Path, declared: tuple[str, ...]) -> bool:
    for relative in declared:
        left_path, right_path = left / relative, right / relative
        if not _safe_regular_file(left, relative) or not _safe_regular_file(right, relative):
            return False
        try:
            if left_path.stat().st_size != right_path.stat().st_size or not _same_bytes(left_path, right_path):
                return False
        except (OSError, ValueError):
            return False
    return True


def _same_bytes(left: Path, right: Path) -> bool:
    with left.open("rb") as left_stream, right.open("rb") as right_stream:
        while True:
            left_chunk, right_chunk = left_stream.read(1024 * 1024), right_stream.read(1024 * 1024)
            if left_chunk != right_chunk:
                return False
            if not left_chunk:
                return True


def _same_bytes_to_bytes(path: Path, expected: bytes) -> bool:
    try:
        return path.read_bytes() == expected
    except (OSError, ValueError):
        return False


def _copy_declared_files(source: Path, destination: Path, declared: tuple[str, ...]) -> None:
    for relative in declared:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, target)


def _fsync_files(root: Path, declared: tuple[str, ...]) -> None:
    directories: set[Path] = {root}
    for relative in declared:
        path = root / relative
        with path.open("rb") as stream:
            try:
                os.fsync(stream.fileno())
            except OSError:
                pass
        directories.add(path.parent)
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        _fsync_directory(directory)


def _fsync_directory(path: Path) -> None:
    flags = getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, os.O_RDONLY | flags)
    except (OSError, ValueError):
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _remove_tree(path: Path) -> None:
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
    except OSError:
        pass
