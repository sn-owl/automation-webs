import dataclasses
import json
from pathlib import Path
import unittest


SAMPLE_WORK_ITEM = {
    "task_id": "yeonje-13452",
    "source": {
        "type": "board",
        "id": "yeonje",
        "external_id": "13452",
        "url": "https://fixture.local/bbs/board.php?wr_id=13452",
    },
    "received_at": "2026-08-28T09:44:00+09:00",
    "title": "무더위쉼터 현황 현행화",
    "body": "첨부물 확인 바랍니다.",
    "author": "요청부서A",
    "attachments": [
        {
            "name": "무더위쉼터 현황.hwpx",
            "type": "hwpx",
            "raw_ref": "raw/yeonje/13452/attachments/restarea.hwpx",
            "extracted_ref": "normalized/yeonje/13452/restarea.json",
        }
    ],
    "mask_table_ref": "local://masks/yeonje-13452.json",
}


SCHEMA_PATH = Path(__file__).parents[1] / "schemas" / "work-item.schema.json"


class WorkItemModelTest(unittest.TestCase):
    def models(self):
        try:
            from automation.models import AttachmentRef, SourceRef, WorkItem
        except ModuleNotFoundError as exc:
            self.fail(f"automation.models should exist: {exc}")

        return SourceRef, AttachmentRef, WorkItem

    def schema(self):
        try:
            return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            self.fail(f"work item schema should exist: {exc}")

    def test_work_item_round_trips_to_canonical_dict(self):
        _, _, WorkItem = self.models()

        item = WorkItem.from_dict(SAMPLE_WORK_ITEM)

        self.assertEqual(item.to_dict(), SAMPLE_WORK_ITEM)
        self.assertEqual(json.loads(json.dumps(item.to_dict(), ensure_ascii=False)), SAMPLE_WORK_ITEM)
    def test_connector_minimal_fields_are_optional_and_round_trip_safely(self):
        _, _, WorkItem = self.models()
        payload = dict(SAMPLE_WORK_ITEM)
        payload["source"] = {
            key: value for key, value in payload["source"].items() if key != "url"
        }
        payload.pop("author")
        payload.pop("attachments")
        payload["source"]["type"] = "future-connector"

        item = WorkItem.from_dict(payload)

        self.assertIsNone(item.author)
        self.assertIsNone(item.source.url)
        self.assertEqual(item.attachments, ())
        canonical = item.to_dict()
        self.assertNotIn("author", canonical)
        self.assertNotIn("url", canonical["source"])
        self.assertEqual(canonical["attachments"], [])


    def test_missing_required_field_is_rejected(self):
        _, _, WorkItem = self.models()
        payload = dict(SAMPLE_WORK_ITEM)
        del payload["source"]

        with self.assertRaisesRegex(ValueError, "source is required"):
            WorkItem.from_dict(payload)
    def test_unknown_fields_are_rejected_like_the_closed_schema(self):
        _, _, WorkItem = self.models()
        with self.assertRaisesRegex(ValueError, "unknown"):
            WorkItem.from_dict({**SAMPLE_WORK_ITEM, "instructions": "not a WorkItem field"})


    def test_empty_task_id_is_rejected(self):
        _, _, WorkItem = self.models()
        payload = dict(SAMPLE_WORK_ITEM, task_id="   ")

        with self.assertRaisesRegex(ValueError, "task_id is required"):
            WorkItem.from_dict(payload)

    def test_invalid_received_at_is_rejected(self):
        _, _, WorkItem = self.models()
        payload = dict(SAMPLE_WORK_ITEM, received_at="2026/08/28 09:44")

        with self.assertRaisesRegex(ValueError, "received_at must be ISO 8601"):
            WorkItem.from_dict(payload)

    def test_models_are_frozen(self):
        SourceRef, AttachmentRef, WorkItem = self.models()
        item = WorkItem.from_dict(SAMPLE_WORK_ITEM)

        with self.assertRaises(dataclasses.FrozenInstanceError):
            item.title = "다른 제목"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            item.source.id = "other"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            item.attachments[0].name = "other.hwpx"

        self.assertIsInstance(item.source, SourceRef)
        self.assertIsInstance(item.attachments[0], AttachmentRef)

    def test_schema_required_fields_match_work_item_dict(self):
        schema = self.schema()

        self.assertEqual(
            set(schema["required"]),
            {"task_id", "source", "received_at", "title", "body", "mask_table_ref"},
        )
        self.assertEqual(set(schema["properties"]), set(SAMPLE_WORK_ITEM))
        self.assertFalse(schema["additionalProperties"])

    def test_schema_defines_source_and_attachment_contracts(self):
        schema = self.schema()
        source = schema["properties"]["source"]
        attachment = schema["properties"]["attachments"]["items"]

        self.assertEqual(set(source["required"]), {"type", "id", "external_id"})
        self.assertEqual(set(source["properties"]), set(SAMPLE_WORK_ITEM["source"]))
        self.assertEqual(source["properties"]["type"]["type"], "string")
        self.assertNotIn("enum", source["properties"]["type"])
        self.assertFalse(source["additionalProperties"])

        self.assertEqual(set(attachment["required"]), set(SAMPLE_WORK_ITEM["attachments"][0]))
        self.assertEqual(set(attachment["properties"]), set(SAMPLE_WORK_ITEM["attachments"][0]))
        self.assertIn(SAMPLE_WORK_ITEM["attachments"][0]["type"], attachment["properties"]["type"]["enum"])
        self.assertIn("jpg", attachment["properties"]["type"]["enum"])
        self.assertFalse(attachment["additionalProperties"])

    def test_schema_declares_iso_dates_and_null_policy(self):
        schema = self.schema()

        self.assertEqual(schema["properties"]["received_at"]["format"], "date-time")
        self.assertNotIn("null", _schema_types(schema))


def _schema_types(node):
    found = []
    if isinstance(node, dict):
        value = node.get("type")
        if isinstance(value, list):
            found.extend(value)
        elif isinstance(value, str):
            found.append(value)
        for child in node.values():
            found.extend(_schema_types(child))
    elif isinstance(node, list):
        for child in node:
            found.extend(_schema_types(child))
    return found


if __name__ == "__main__":
    unittest.main()
