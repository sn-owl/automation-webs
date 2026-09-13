import unittest
from pathlib import Path


FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "sanitized"


class GnuBoardParserTest(unittest.TestCase):
    def parser(self):
        try:
            from automation.adapters.gnuboard import GnuBoardParseError, parse_gnuboard_html
        except ModuleNotFoundError as exc:
            self.fail(f"gnuboard parser should exist: {exc}")

        return GnuBoardParseError, parse_gnuboard_html

    def test_yeonje_fixture_normalizes_title_author_date_body_and_attachment(self):
        _, parse_gnuboard_html = self.parser()
        html = (FIXTURE_DIR / "yeonje-ready.html").read_text(encoding="utf-8")

        item = parse_gnuboard_html(
            html,
            board_id="yeonje",
            source_url="https://fixture.local/bbs/board.php?bo_table=yeonje&wr_id=13452",
        )

        self.assertEqual(item.task_id, "yeonje-13452")
        self.assertEqual(item.source.to_dict(), {
            "type": "board",
            "id": "yeonje",
            "external_id": "13452",
            "url": "https://fixture.local/bbs/board.php?bo_table=yeonje&wr_id=13452",
        })
        self.assertEqual(item.received_at, "2026-08-27T10:20:00+09:00")
        self.assertEqual(item.title, "무더위쉼터 현황 현행화")
        self.assertEqual(item.author, "DEPARTMENT_001")
        self.assertIn("위치: SANITIZED_TARGET", item.body)
        self.assertIn("※ 첨부물 확인 바랍니다", item.body)
        self.assertEqual(len(item.attachments), 1)
        self.assertEqual(item.attachments[0].name, "SANITIZED_RESTAREA_REQUEST.hwpx")
        self.assertEqual(item.attachments[0].type, "hwpx")
        self.assertEqual(item.attachments[0].raw_ref, "https://example.invalid/url-004")

    def test_attachment_extension_outside_schema_enum_is_rejected(self):
        GnuBoardParseError, parse_gnuboard_html = self.parser()
        html = (FIXTURE_DIR / "yeonje-ready.html").read_text(encoding="utf-8")
        html = html.replace("SANITIZED_RESTAREA_REQUEST.hwpx", "SANITIZED_RESTAREA_REQUEST.exe")

        with self.assertRaisesRegex(GnuBoardParseError, "unsupported attachment extension"):
            parse_gnuboard_html(
                html,
                board_id="yeonje",
                source_url="https://fixture.local/bbs/board.php?bo_table=yeonje&wr_id=13452",
            )

    def test_bukgu_fixture_normalizes_board_specific_values(self):
        _, parse_gnuboard_html = self.parser()
        html = (FIXTURE_DIR / "bsbukgu-manual.html").read_text(encoding="utf-8")

        item = parse_gnuboard_html(
            html,
            board_id="bsbukgu",
            source_url="https://fixture.local/bbs/board.php?bo_table=bsbukgu&wr_id=2048",
        )

        self.assertEqual(item.task_id, "bsbukgu-2048")
        self.assertEqual(item.received_at, "2026-08-27T13:07:00+09:00")
        self.assertEqual(item.title, "팝업 이미지 게재 요청")
        self.assertEqual(item.author, "SOURCE_002")
        self.assertEqual(item.body, "첨부 파일 기준으로 팝업 이미지를 게재해 주세요.")
        self.assertEqual(item.attachments[0].name, "SANITIZED_POPUP_REQUEST.hwpx")
        self.assertEqual(item.attachments[0].raw_ref, "https://example.invalid/url-004")

    def test_explicit_source_completion_marker_is_observed(self):
        _, parse_gnuboard_html = self.parser()
        html = (FIXTURE_DIR / "yeonje-ready.html").read_text(encoding="utf-8")
        html = html.replace(
            "<head>",
            '<head><meta content="completed" name="source-status">',
            1,
        )

        item = parse_gnuboard_html(
            html,
            board_id="yeonje",
            source_url="https://fixture.local/bbs/board.php?bo_table=yeonje&wr_id=13452",
            scope="team-a",
            connector_id="connector-gnu",
        )

        self.assertTrue(item.source_completion_observed)

    def test_login_or_permission_html_is_rejected(self):
        GnuBoardParseError, parse_gnuboard_html = self.parser()

        with self.assertRaisesRegex(GnuBoardParseError, "login or permission"):
            parse_gnuboard_html(
                '<html><body><form id="flogin">로그인</form></body></html>',
                board_id="yeonje",
                source_url="https://fixture.local/bbs/board.php?bo_table=yeonje&wr_id=13452",
            )


if __name__ == "__main__":
    unittest.main()
