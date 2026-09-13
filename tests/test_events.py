import tempfile
import unittest
from pathlib import Path

from automation.events import (
    CorruptEventError,
    DuplicateEventError,
    EventLog,
    SecretFieldError,
)


class EventLogTest(unittest.TestCase):
    def test_reader_preserves_append_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            log = EventLog(path)
            first = {"event_id": "evt-1", "type": "discovered", "task_id": "task-1"}
            second = {"event_id": "evt-2", "type": "stored", "task_id": "task-1"}

            log.append(first)
            log.append(second)

            self.assertEqual(log.read(), [first, second])

    def test_duplicate_event_id_is_rejected_without_changing_log(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            log = EventLog(path)
            log.append({"event_id": "evt-1", "type": "discovered"})
            original = path.read_bytes()

            with self.assertRaises(DuplicateEventError):
                log.append({"event_id": "evt-1", "type": "stored"})

            self.assertEqual(path.read_bytes(), original)

    def test_reader_rejects_corrupt_json_line(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_bytes(b'{"event_id":"evt-1"}\nnot-json\n')

            with self.assertRaises(CorruptEventError):
                EventLog(path).read()

    def test_secret_fields_are_rejected_before_any_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            log = EventLog(path)

            with self.assertRaises(SecretFieldError):
                log.append(
                    {
                        "event_id": "evt-1",
                        "type": "stored",
                        "details": {"session_cookie": "raw-cookie"},
                    }
                )

            self.assertFalse(path.exists())
    def test_raw_secret_value_in_innocuous_field_is_rejected_without_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            log = EventLog(path)
            log.append({"event_id": "evt-1", "type": "discovered"})
            original = path.read_bytes()

            with self.assertRaises(SecretFieldError):
                log.append(
                    {
                        "event_id": "evt-2",
                        "type": "stored",
                        "message": "Authorization: Bearer raw-token-value",
                    }
                )

            self.assertEqual(path.read_bytes(), original)
    def test_ordinary_basic_text_is_not_treated_as_a_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"

            EventLog(path).append(
                {
                    "event_id": "evt-1",
                    "type": "note",
                    "message": "basic training",
                }
            )

            self.assertEqual(EventLog(path).read()[0]["message"], "basic training")



    def test_existing_bytes_are_preserved_when_appending(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            prefix = b'{"z":1,"event_id":"evt-1"}\r\n'
            path.write_bytes(prefix)

            EventLog(path).append({"event_id": "evt-2", "type": "stored"})

            self.assertEqual(path.read_bytes()[: len(prefix)], prefix)
            self.assertEqual(EventLog(path).read()[0], {"z": 1, "event_id": "evt-1"})


if __name__ == "__main__":
    unittest.main()
