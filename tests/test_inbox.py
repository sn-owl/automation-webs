from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from automation.models import WorkItem
from automation.inbox import accept_completed_download


class InboxContractTest(unittest.TestCase):
    def _bundle(self, root: Path, capture_id: str | None = None) -> Path:
        page = (
            '<article id="bo_v"><h2 id="bo_v_title">신청 안내</h2>'
            '<div id="bo_v_info"><span class="sv_member">홍길동</span> '
            '26-09-01 10:30</div><div id="bo_v_con">본문</div>'
            '<section id="bo_v_file"><a class="view_file_download" '
            'href="https://example.test/download.php?file=1"><strong>request.hwpx</strong>'
            '</a></section></article>'
        ).encode()
        page_hash = hashlib.sha256(page).hexdigest()
        capture_id = capture_id or f"alpha-13452-{page_hash[:8]}"
        bundle = root / capture_id
        (bundle / "attachments").mkdir(parents=True)
        (bundle / "page.html").write_bytes(page)
        (bundle / "attachments" / "request.hwpx").write_bytes(b"hwpx bytes")
        manifest = {
            "schema_version": 1,
            "capture_id": capture_id,
            "source": {
                "type": "board",
                "id": "alpha",
                "adapter": "gnuboard",
                "external_id": "13452",
                "url": "https://example.test/board.php?bo_table=alpha&wr_id=13452",
            },
            "captured_at": "2026-09-01T10:30:00+09:00",
            "page": {"path": "page.html", "sha256": page_hash},
            "attachments": [
                {
                    "name": "request.hwpx",
                    "path": "attachments/request.hwpx",
                    "source_url": "https://example.test/download.php?file=1",
                }
            ],
        }
        (bundle / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (bundle / "_READY").write_text(capture_id + "\n", encoding="utf-8")
        return bundle

    def test_accepts_complete_bundle_atomically_as_work_item_input(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            download = self._bundle(root / "downloads")
            accepted = accept_completed_download(download, root / "inbox")

            self.assertEqual(accepted, root / "inbox" / download.name / "work_item.json")
            self.assertFalse(download.exists())
            bundle = accepted.parent
            self.assertTrue(accepted.is_file())
            item = WorkItem.from_dict(json.loads(accepted.read_text(encoding="utf-8")))
            self.assertEqual(item.task_id, "alpha-13452")
            self.assertEqual(item.attachments[0].raw_ref, "attachments/request.hwpx")
            self.assertEqual(item.attachments[0].extracted_ref, "attachments/request.hwpx")
            self.assertTrue((bundle / "_READY").is_file())
            self.assertTrue((bundle / "page.html").is_file())
            self.assertTrue((bundle / "manifest.json").is_file())
            self.assertTrue((bundle / "attachments" / "request.hwpx").is_file())
            self.assertEqual(
                json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))["source"]["adapter"],
                "gnuboard",
            )

    def test_does_not_consume_partial_bundle_without_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            download = self._bundle(root / "downloads")
            (download / "_READY").unlink()

            self.assertIsNone(accept_completed_download(download, root / "inbox"))
            self.assertTrue(download.exists())
            self.assertFalse((root / "inbox").exists())

    def test_rejects_missing_or_invalid_manifest(self):
        for mutation in ("missing", "invalid"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                download = self._bundle(root / "downloads")
                if mutation == "missing":
                    (download / "manifest.json").unlink()
                else:
                    (download / "manifest.json").write_text("{}", encoding="utf-8")

                self.assertIsNone(accept_completed_download(download, root / "inbox"))
                self.assertTrue(download.exists())

    def test_duplicate_is_idempotent_but_conflicting_duplicate_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            first = self._bundle(root / "downloads")
            destination = accept_completed_download(first, root / "inbox")
            duplicate = self._bundle(root / "downloads-duplicate")

            self.assertEqual(accept_completed_download(duplicate, root / "inbox"), destination)
            destination_bundle = destination.parent
            original = (destination_bundle / "attachments" / "request.hwpx").read_bytes()

            conflict = self._bundle(root / "downloads-conflict")
            (conflict / "attachments" / "request.hwpx").write_bytes(b"different bytes")
            self.assertIsNone(accept_completed_download(conflict, root / "inbox"))
            self.assertTrue(conflict.exists())
            self.assertEqual((destination_bundle / "attachments" / "request.hwpx").read_bytes(), original)

    def test_rejects_path_traversal_and_page_hash_mismatch(self):
        for mutation in ("traversal", "hash"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                download = self._bundle(root / "downloads")
                manifest_path = download / "manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if mutation == "traversal":
                    manifest["page"]["path"] = "../page.html"
                else:
                    manifest["page"]["sha256"] = "0" * 64
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

                self.assertIsNone(accept_completed_download(download, root / "inbox"))
                self.assertTrue(download.exists())

    def test_rejects_symlinked_bundle_parent_and_destination(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            download = self._bundle(root / "downloads")
            outside = root / "outside"
            outside.mkdir()
            outside_page = outside / "page.html"
            outside_page.write_bytes(b"outside")
            (download / "page.html").unlink()
            try:
                (download / "nested").symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                if getattr(exc, "winerror", None) == 1314:
                    self.skipTest("symbolic-link privilege is unavailable")
                raise
            manifest_path = download / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["page"] = {
                "path": "nested/page.html",
                "sha256": hashlib.sha256(outside_page.read_bytes()).hexdigest(),
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertIsNone(accept_completed_download(download, root / "inbox"))

            valid = self._bundle(root / "downloads-valid")
            outside_bundle = self._bundle(root / "outside-bundle")
            destination_root = root / "inbox"
            destination_root.mkdir()
            try:
                (destination_root / valid.name).symlink_to(outside_bundle, target_is_directory=True)
            except OSError as exc:
                if getattr(exc, "winerror", None) == 1314:
                    self.skipTest("symbolic-link privilege is unavailable")
                raise
            self.assertIsNone(accept_completed_download(valid, destination_root))
            self.assertTrue(valid.exists())

    def test_requires_supported_adapter_and_attachment_source_url(self):
        for mutation in ("adapter", "source_url"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                download = self._bundle(root / "downloads")
                manifest_path = download / "manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if mutation == "adapter":
                    manifest["source"]["adapter"] = "unknown"
                else:
                    del manifest["attachments"][0]["source_url"]
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                self.assertIsNone(accept_completed_download(download, root / "inbox"))
    def test_rejects_duplicate_attachment_names(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            download = self._bundle(root / "downloads")
            duplicate = download / "attachments" / "request-copy.hwpx"
            duplicate.write_bytes(b"different bytes")
            manifest_path = download / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["attachments"].append(
                {
                    "name": "request.hwpx",
                    "path": "attachments/request-copy.hwpx",
                    "source_url": "https://example.test/download.php?file=2",
                }
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertIsNone(accept_completed_download(download, root / "inbox"))
            self.assertTrue(download.exists())


    def test_acceptance_survives_unavailable_directory_fsync(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            download = self._bundle(root / "downloads")
            with mock.patch("automation.inbox.os.fsync", side_effect=OSError("unsupported")):
                accepted = accept_completed_download(download, root / "inbox")
            self.assertIsNotNone(accepted)
            self.assertTrue(accepted.is_file())


    def test_rejects_schema_bool_external_id_mismatch_and_symlinked_inbox_ancestor(self):
        for mutation in ("schema", "external_id"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                download = self._bundle(root / "downloads")
                manifest_path = download / "manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if mutation == "schema":
                    manifest["schema_version"] = True
                else:
                    manifest["source"]["external_id"] = "999"
                    new_id = f"alpha-999-{manifest['page']['sha256'][:8]}"
                    manifest["capture_id"] = new_id
                    download = download.rename(download.parent / new_id)
                    (download / "_READY").write_text(new_id + "\n", encoding="utf-8")
                manifest_path = download / "manifest.json"
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                self.assertIsNone(accept_completed_download(download, root / "inbox"))

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            real_root = root / "real"
            real_root.mkdir()
            symlink_root = root / "link"
            try:
                symlink_root.symlink_to(real_root, target_is_directory=True)
            except OSError as exc:
                if getattr(exc, "winerror", None) == 1314:
                    self.skipTest("symbolic-link privilege is unavailable")
                raise
            self.assertIsNone(accept_completed_download(self._bundle(root / "downloads"), symlink_root / "inbox"))

    def test_normalizes_egov_data_sid_into_work_item(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            page = (Path(__file__).parents[1] / "fixtures" / "sanitized" / "egov-developable.html").read_bytes()
            page_hash = hashlib.sha256(page).hexdigest()
            capture_id = f"bbs_0000275-9001-{page_hash[:8]}"
            bundle = root / "downloads" / capture_id
            (bundle / "attachments").mkdir(parents=True)
            (bundle / "page.html").write_bytes(page)
            names = ("ATTACHMENT_001.zip", "ATTACHMENT_002.zip", "ATTACHMENT_003.jpg", "ATTACHMENT_004.hwpx")
            for name in names:
                (bundle / "attachments" / name).write_bytes(name.encode())
            manifest_attachments = [
                {"name": name, "path": f"attachments/{name}", "source_url": f"https://example.test/{name}"}
                for name in names
            ]
            manifest = {
                "schema_version": 1,
                "capture_id": capture_id,
                "source": {
                    "type": "board",
                    "id": "bbs_0000275",
                    "adapter": "egov",
                    "external_id": "9001",
                    "url": "https://www.egov.go.kr/board/view.egov?boardId=BBS_0000275&dataSid=9001",
                },
                "captured_at": "2026-09-01T10:30:00+09:00",
                "page": {"path": "page.html", "sha256": page_hash},
                "attachments": manifest_attachments,
            }
            (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            (bundle / "_READY").write_text(capture_id + "\n", encoding="utf-8")
            accepted = accept_completed_download(bundle, root / "inbox")
            item = WorkItem.from_dict(json.loads(accepted.read_text(encoding="utf-8")))
            self.assertEqual(item.task_id, "egov-9001")
            self.assertEqual(item.source.external_id, "9001")
            self.assertEqual(item.source.url, manifest["source"]["url"])
    def test_maps_archiver_sanitized_attachment_name_to_local_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            download = self._bundle(root / "downloads")
            page = (download / "page.html").read_text(encoding="utf-8").replace("request.hwpx", "request:v2.hwpx")
            (download / "page.html").write_text(page, encoding="utf-8")
            manifest_path = download / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            page_hash = hashlib.sha256(page.encode()).hexdigest()
            new_id = f"alpha-13452-{page_hash[:8]}"
            download = download.rename(download.parent / new_id)
            attachment = download / "attachments" / "request.hwpx"
            attachment.rename(download / "attachments" / "request_v2.hwpx")
            manifest["capture_id"] = new_id
            manifest["page"]["sha256"] = page_hash
            manifest["attachments"][0]["name"] = "request_v2.hwpx"
            manifest["attachments"][0]["path"] = "attachments/request_v2.hwpx"
            (download / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            (download / "_READY").write_text(new_id + "\n", encoding="utf-8")
            accepted = accept_completed_download(download, root / "inbox")
            item = WorkItem.from_dict(json.loads(accepted.read_text(encoding="utf-8")))
            self.assertEqual(item.attachments[0].raw_ref, "attachments/request_v2.hwpx")

    def test_stale_claim_file_does_not_block_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            download = self._bundle(root / "downloads")
            inbox = root / "inbox"
            inbox.mkdir()
            (inbox / f".{download.name}.claim").write_text("stale", encoding="utf-8")
            accepted = accept_completed_download(download, inbox)
            self.assertTrue(accepted.is_file())


if __name__ == "__main__":
    unittest.main()
