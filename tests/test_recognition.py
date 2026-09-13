import tempfile
import unittest
from pathlib import Path

from automation.models import WorkItem
from automation.recognition import build_recognition_artifacts, build_recognition_event


def _item(raw_ref: str) -> WorkItem:
    return WorkItem.from_dict(
        {
            "task_id": "rec-1",
            "source": {"type": "board", "id": "b", "external_id": "1", "url": "https://x.invalid/1"},
            "received_at": "2026-01-01T00:00:00Z",
            "title": "제목",
            "body": "본문",
            "mask_table_ref": "local://m.json",
            "attachments": [
                {"name": "doc.pdf", "type": "pdf", "raw_ref": raw_ref, "extracted_ref": raw_ref}
            ],
        }
    )


class RecognitionEventTest(unittest.TestCase):
    def test_event_projection_drops_preview_text_and_local_paths(self):
        # C7: the durable recognized event must not carry extracted preview
        # text or a local raw_ref path.
        with tempfile.TemporaryDirectory() as directory:
            attachment = Path(directory) / "doc.pdf"
            attachment.write_bytes(b"%PDF-1.4\nstream\nBT (secret text) Tj ET\nendstream\n%%EOF\n")
            item = _item(str(attachment))

            full = build_recognition_artifacts(item)
            event = build_recognition_event(item)

        # The full artifact still carries preview + raw_ref for local use.
        self.assertIn("raw_ref", full["attachments"][0])
        self.assertIn("preview", full["attachments"][0])

        # The event projection carries neither.
        event_attachment = event["attachments"][0]
        self.assertNotIn("raw_ref", event_attachment)
        self.assertNotIn("preview", event_attachment)
        self.assertEqual(
            set(event_attachment),
            {"name", "type", "raw_sha256", "extraction_status"},
        )


if __name__ == "__main__":
    unittest.main()
