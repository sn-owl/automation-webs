import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StoredObject:
    task_id: str
    name: str
    path: str
    sha256: str
    size: int


class RawStoreConflict(RuntimeError):
    """Raised when a raw object already exists with different bytes."""


def store_raw(task_id: str, name: str, data: bytes, *, root: Path | str = ".") -> StoredObject:
    target = Path(root) / "raw" / task_id / name
    digest = hashlib.sha256(data).hexdigest()

    if target.exists():
        if target.read_bytes() != data:
            raise RawStoreConflict(f"raw object already exists with different bytes: {target}")
        return StoredObject(task_id, name, str(target), digest, len(data))

    target.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temp:
            temp.write(data)
            temp.flush()
            os.fsync(temp.fileno())
            temp_name = temp.name
        os.replace(temp_name, target)
    finally:
        if temp_name is not None and os.path.exists(temp_name):
            os.unlink(temp_name)

    return StoredObject(task_id, name, str(target), digest, len(data))
