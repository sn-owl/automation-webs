import hashlib
import tempfile
import unittest
from pathlib import Path

from automation.store import RawStoreConflict, store_raw


TASK_ID = "yeonje-13452"
NAME = "page.html"
FIRST_BYTES = b"<html>first</html>"
OTHER_BYTES = b"<html>other</html>"


class RawStoreTest(unittest.TestCase):
    def test_first_store_creates_raw_file_and_returns_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            stored = store_raw(TASK_ID, NAME, FIRST_BYTES, root=directory)
            target = Path(directory) / "raw" / TASK_ID / NAME

            self.assertEqual(target.read_bytes(), FIRST_BYTES)
            self.assertEqual(stored.task_id, TASK_ID)
            self.assertEqual(stored.name, NAME)
            self.assertEqual(stored.path, str(target))
            self.assertEqual(stored.sha256, hashlib.sha256(FIRST_BYTES).hexdigest())
            self.assertEqual(stored.size, len(FIRST_BYTES))

    def test_same_bytes_store_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            first = store_raw(TASK_ID, NAME, FIRST_BYTES, root=directory)
            second = store_raw(TASK_ID, NAME, FIRST_BYTES, root=directory)
            target = Path(directory) / "raw" / TASK_ID / NAME

            self.assertEqual(target.read_bytes(), FIRST_BYTES)
            self.assertEqual(second, first)
            self.assertEqual(second.sha256, hashlib.sha256(FIRST_BYTES).hexdigest())

    def test_different_bytes_conflict_and_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            store_raw(TASK_ID, NAME, FIRST_BYTES, root=directory)
            target = Path(directory) / "raw" / TASK_ID / NAME

            with self.assertRaises(RawStoreConflict):
                store_raw(TASK_ID, NAME, OTHER_BYTES, root=directory)

            self.assertEqual(target.read_bytes(), FIRST_BYTES)


if __name__ == "__main__":
    unittest.main()
