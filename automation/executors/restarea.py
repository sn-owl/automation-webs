from __future__ import annotations

import hashlib
import importlib.util
import re
import shutil
from collections.abc import Collection
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType

from automation import identity
from automation.models import WorkItem
from automation.recipes import get_recipe


RECIPE_ID = "restarea-hwpx-to-xls"
_REPO_ROOT = Path(__file__).parents[2].resolve()
_CONVERTER_PATH = _REPO_ROOT / "restarea-converter" / "converter.py"


def _load_converter() -> ModuleType:
    spec = importlib.util.spec_from_file_location("restarea_converter", _CONVERTER_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load restarea converter: {_CONVERTER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_converter = _load_converter()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _contained(path: Path, root: Path) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"artifact output escapes artifact directory: {path}") from exc


_URL_SCHEME = re.compile(r"\A[A-Za-z][A-Za-z0-9+.\-]*://")


def _reject_remote_ref(raw_ref: str) -> None:
    """Refuse an attachment reference that would read across a trust boundary.

    ``raw_ref`` on a ``run_pipeline``-ingested WorkItem is the attachment href
    copied verbatim from scraped board HTML (``adapters/gnuboard.py`` and
    ``adapters/dongnae.py`` set ``raw_ref=href`` with no scheme/host check). A
    UNC path (``\\\\host\\share``) or a URL scheme (``file://``, ``smb://``)
    would make the executor open a network location -- an outbound SMB/HTTP
    fetch and, on Windows, an NTLM credential leak -- so those forms are
    refused before the path is ever touched. Materialized local attachments
    (the inbox's ``attachments/<name>`` and absolute scratch paths) are
    unaffected.
    """
    text = str(raw_ref)
    if _URL_SCHEME.match(text):
        raise ValueError("attachment reference is a URL, not a local file")
    if text[:2] in ("\\\\", "//"):
        raise ValueError("attachment reference is a UNC network path")


def _source_attachment(item: WorkItem) -> Path:
    attachments = [attachment for attachment in item.attachments if attachment.type == "hwpx"]
    if len(attachments) != 1:
        raise ValueError(f"expected exactly one HWPX attachment, found {len(attachments)}")
    _reject_remote_ref(attachments[0].raw_ref)
    source = Path(attachments[0].raw_ref)
    if not source.is_file():
        raise FileNotFoundError(source)
    return source


def source_digest(item: WorkItem, recipe_definition=None) -> str:
    """Return the sha256 of the single HWPX attachment this recipe consumes.

    The execution flow uses this to bind the reserved execution key to the real
    input bytes instead of an operator-supplied token, so a completed execution
    cannot be replayed by changing a CLI argument. It applies the same remote-
    reference guard as ``execute_restarea`` before reading the file.
    """
    if not isinstance(item, WorkItem):
        raise TypeError("item must be a WorkItem")
    return _sha256(_source_attachment(item))

def validate_restarea_input(item: WorkItem, recipe_definition=None) -> dict[str, object]:
    """Return the shared preflight result for the HWPX→XLS recipe.

    ``recipe_definition`` is accepted so every capability validator shares the
    same Core call contract; the restarea implementation intentionally keeps
    its historical fixed parser as a regression path.
    """
    attachments = [attachment for attachment in item.attachments if attachment.type == "hwpx"]
    if not attachments:
        return {
            "state": "missing",
            "recipe_id": RECIPE_ID,
            "reasons": ["HWPX 첨부가 없습니다."],
            "evidence": [],
        }
    if len(attachments) > 1:
        return {
            "state": "ambiguous",
            "recipe_id": RECIPE_ID,
            "reasons": ["HWPX 첨부가 2개 이상이라 자동 선택할 수 없습니다."],
            "evidence": [f"hwpx_attachments={len(attachments)}"],
        }
    attachment = attachments[0]
    try:
        _reject_remote_ref(attachment.raw_ref)
        source = Path(attachment.raw_ref)
        if not source.is_file():
            return {
                "state": "missing",
                "recipe_id": RECIPE_ID,
                "reasons": ["HWPX 원본 파일을 로컬에서 찾을 수 없습니다."],
                "evidence": ["local_file=false"],
            }
        rows = _converter.parse_hwpx(source)
    except Exception:
        return {
            "state": "invalid",
            "recipe_id": RECIPE_ID,
            "reasons": ["필수 헤더를 가진 단일 표를 읽을 수 없습니다."],
            "evidence": ["hwpx_table_valid=false"],
        }
    if not rows:
        return {
            "state": "invalid",
            "recipe_id": RECIPE_ID,
            "reasons": ["변환할 데이터 행이 없습니다."],
            "evidence": ["rows=0"],
        }
    return {
        "state": "satisfied",
        "recipe_id": RECIPE_ID,
        "reasons": [],
        "evidence": [
            "hwpx_attachments=1",
            "local_file=true",
            "required_headers=true",
            f"rows={len(rows)}",
        ],
    }


def validate_restarea_plan(recipe_definition) -> None:
    """Validate declarations against the installed fixed converter capability."""
    if recipe_definition.input_type != "hwpx" or recipe_definition.output_type != "xls":
        raise ValueError("restarea requires HWPX to XLS")
    if recipe_definition.template != "restarea-converter/restarea-template.xls":
        raise ValueError("restarea capability requires its reviewed XLS template")
    expected_operations = [
        "select_attachment",
        "parse_table",
        "map_columns",
        "write_template",
        "verify_output",
    ]
    if [step["operation"] for step in recipe_definition.steps] != expected_operations:
        raise ValueError("restarea declaration must follow the installed capability phases")
    select, parse, mapping, writing, verification = [
        step["parameters"] for step in recipe_definition.steps
    ]
    if select != {"type": "hwpx", "count": 1}:
        raise ValueError("restarea attachment declaration is incompatible")
    if parse != {"required_headers": list(_converter.REQUIRED_HEADERS)}:
        raise ValueError("restarea header declaration is incompatible")
    if mapping != {"output_columns": list(_converter.OUTPUT_MAPPING)}:
        raise ValueError("restarea mapping declaration is incompatible")
    if writing != {"output_type": "xls"}:
        raise ValueError("restarea output declaration is incompatible")
    installed_checks = {"header_mapping", "row_count", "cell_values"}
    checks = verification.get("checks")
    if (
        not isinstance(checks, list)
        or not checks
        or any(check not in installed_checks for check in checks)
    ):
        raise ValueError("restarea verification declaration is incompatible")
    if any(check not in installed_checks for check in recipe_definition.success_checks):
        raise ValueError("restarea success check is unsupported")
    declared = (recipe_definition.verification or {}).get("checks", [])
    if not isinstance(declared, list) or any(check not in installed_checks for check in declared):
        raise ValueError("restarea verification policy is unsupported")


def execute_restarea(
    item: WorkItem,
    artifact_dir: Path | str,
    *,
    seen_execution_keys: Collection[str] = (),
    recipe_id: str = RECIPE_ID,
    recipe_definition=None,
    execution_key=None,
) -> dict[str, object]:
    """Convert one WorkItem's HWPX attachment into a contained XLS artifact."""
    if not isinstance(item, WorkItem):
        raise TypeError("item must be a WorkItem")

    source = _source_attachment(item)
    input_sha256 = _sha256(source)
    execution_key = execution_key or identity.execution_key(item.task_id, recipe_id, input_sha256)
    duplicate = execution_key in seen_execution_keys
    if duplicate:
        return {
            "task_id": item.task_id,
            "recipe_id": recipe_id,
            "execution_key": execution_key,
            "input_sha256": input_sha256,
            "status": "duplicate",
            "duplicate": True,
            "artifacts": [],
        }

    recipe = recipe_definition or get_recipe(recipe_id)
    if recipe.template is None:
        raise ValueError(f"recipe {recipe_id} has no XLS template")
    template = (_REPO_ROOT / recipe.template).resolve()
    _contained(template, _REPO_ROOT)
    if not template.is_file():
        raise FileNotFoundError(template)

    root = Path(artifact_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    output = root / f"{execution_key}.xls"
    _contained(output, root)
    if output.exists():
        raise FileExistsError("refusing to overwrite existing output")

    with TemporaryDirectory(prefix="restarea-") as scratch_dir:
        scratch_source = Path(scratch_dir) / source.name
        shutil.copy2(source, scratch_source)
        _converter.convert(scratch_source, output, template)

    _contained(output, root)
    if not output.is_file():
        raise FileNotFoundError(f"converter did not create XLS artifact: {output}")

    return {
        "task_id": item.task_id,
        "recipe_id": recipe_id,
        "execution_key": execution_key,
        "input_sha256": input_sha256,
        "duplicate": duplicate,
        "status": "duplicate" if duplicate else "succeeded",
        "artifacts": [{"path": str(output), "sha256": _sha256(output)}],
    }
