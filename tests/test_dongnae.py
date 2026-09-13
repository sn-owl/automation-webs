import unittest
from pathlib import Path


FIXTURE = Path(__file__).parents[1] / "fixtures" / "sanitized" / "dongnae-developable.html"
SOURCE_URL = "https://fixture.local/dongnae/board/view.sko?nttId=9001"


class DongnaeParserTest(unittest.TestCase):
    def parser(self):
        try:
            from automation.adapters.dongnae import DongnaeParseError, parse_dongnae_html
        except ModuleNotFoundError as exc:
            self.fail(f"dongnae parser should exist: {exc}")

        return DongnaeParseError, parse_dongnae_html

    def test_fixture_normalizes_title_author_status_body_and_attachments(self):
        _, parse_dongnae_html = self.parser()
        html = FIXTURE.read_text(encoding="utf-8")

        item = parse_dongnae_html(html, source_url=SOURCE_URL)

        self.assertEqual(item.task_id, "dongnae-9001")
        self.assertEqual(item.source.to_dict(), {
            "type": "board",
            "id": "dongnae",
            "external_id": "9001",
            "url": SOURCE_URL,
        })
        self.assertEqual(item.received_at, "2026-08-28T10:46:00+09:00")
        self.assertEqual(item.title, "문화시설 페이지 구조 변경 요청")
        self.assertEqual(item.author, "PERSON_001")
        self.assertIn("작업구분: 수정", item.body)
        self.assertIn("진행구분: 접수", item.body)
        self.assertIn("버튼 3개로 변경 후 첨부파일을 등록해 주세요.", item.body)
        self.assertIn("시설 사진과 면적 정보를 변경해 주세요.", item.body)
        self.assertEqual(
            [attachment.name for attachment in item.attachments],
            ["ATTACHMENT_001.zip", "ATTACHMENT_002.zip", "ATTACHMENT_003.jpg", "ATTACHMENT_004.hwpx"],
        )
        self.assertEqual(
            [attachment.type for attachment in item.attachments],
            ["zip", "zip", "jpg", "hwpx"],
        )
        self.assertEqual(item.attachments[2].raw_ref, "/fixture/path-004")

    def test_missing_progress_status_is_rejected(self):
        DongnaeParseError, parse_dongnae_html = self.parser()
        html = FIXTURE.read_text(encoding="utf-8").replace(
            "진행구분</span></th><td>접수</td>",
            "진행구분</span></th><td></td>",
        )

        with self.assertRaisesRegex(DongnaeParseError, "missing 진행구분"):
            parse_dongnae_html(html, source_url=SOURCE_URL)

    def test_login_html_is_rejected(self):
        DongnaeParseError, parse_dongnae_html = self.parser()

        with self.assertRaisesRegex(DongnaeParseError, "login"):
            parse_dongnae_html(
                '<html><body><form id="loginForm">로그인</form></body></html>',
                source_url=SOURCE_URL,
            )


if __name__ == "__main__":
    unittest.main()
