import unittest
from pathlib import Path

from automation.models import WorkItem


FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "sanitized"
SOURCES = {
    "yeonje-ready.html": {
        "board_type": "gnuboard",
        "source": {
            "board_id": "yeonje",
            "url": "https://fixture.local/bbs/board.php?bo_table=yeonje&wr_id=13452",
        },
        "task_id": "yeonje-13452",
    },
    "bsbukgu-manual.html": {
        "board_type": "gnuboard",
        "source": {
            "board_id": "bsbukgu",
            "url": "https://fixture.local/bbs/board.php?bo_table=bsbukgu&wr_id=2048",
        },
        "task_id": "bsbukgu-2048",
    },
    "dongnae-developable.html": {
        "board_type": "dongnae",
        "source": {
            "url": "https://fixture.local/dongnae/board/view.sko?nttId=9001",
        },
        "task_id": "dongnae-9001",
    },
}


class AdapterRegistryTest(unittest.TestCase):
    def registry(self):
        try:
            from automation.adapters.registry import UnsupportedBoardType, normalize_html
        except ModuleNotFoundError as exc:
            self.fail(f"adapter registry should exist: {exc}")

        return UnsupportedBoardType, normalize_html

    def test_known_board_types_normalize_all_sanitized_fixtures(self):
        _, normalize_html = self.registry()

        for filename, case in SOURCES.items():
            with self.subTest(filename=filename):
                item = normalize_html(
                    case["board_type"],
                    (FIXTURE_DIR / filename).read_text(encoding="utf-8"),
                    case["source"],
                )

                self.assertIsInstance(item, WorkItem)
                self.assertEqual(item.task_id, case["task_id"])

    def test_unsupported_board_type_is_rejected(self):
        UnsupportedBoardType, normalize_html = self.registry()

        with self.assertRaisesRegex(UnsupportedBoardType, "unsupported board type"):
            normalize_html(
                "unknown-board",
                "<html></html>",
                {"url": "https://fixture.local/unknown"},
            )
    def test_rejects_partial_scoped_connection(self):
        _, normalize_html = self.registry()
        html = (FIXTURE_DIR / "yeonje-ready.html").read_text(encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "scope and connector_id"):
            normalize_html(
                "gnuboard",
                html,
                {
                    "board_id": "yeonje",
                    "url": "https://fixture.local/bbs/board.php?bo_table=yeonje&wr_id=13452",
                    "scope": "owner-a",
                },
            )


if __name__ == "__main__":
    unittest.main()
