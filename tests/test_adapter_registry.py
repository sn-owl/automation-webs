import unittest
from pathlib import Path

from automation.models import WorkItem


FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "sanitized"
SOURCES = {
    "alpha-ready.html": {
        "board_type": "gnuboard",
        "source": {
            "board_id": "alpha",
            "url": "https://fixture.local/bbs/board.php?bo_table=alpha&wr_id=13452",
        },
        "task_id": "alpha-13452",
    },
    "beta-manual.html": {
        "board_type": "gnuboard",
        "source": {
            "board_id": "beta",
            "url": "https://fixture.local/bbs/board.php?bo_table=beta&wr_id=2048",
        },
        "task_id": "beta-2048",
    },
    "egov-developable.html": {
        "board_type": "egov",
        "source": {
            "url": "https://fixture.local/egov/board/view.sko?nttId=9001",
        },
        "task_id": "egov-9001",
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
        html = (FIXTURE_DIR / "alpha-ready.html").read_text(encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "scope and connector_id"):
            normalize_html(
                "gnuboard",
                html,
                {
                    "board_id": "alpha",
                    "url": "https://fixture.local/bbs/board.php?bo_table=alpha&wr_id=13452",
                    "scope": "owner-a",
                },
            )


if __name__ == "__main__":
    unittest.main()
