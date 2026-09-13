import tempfile
import unittest
import zipfile
from pathlib import Path

import win32com.client

from converter import convert, parse_hwpx


HEADERS = ("연번", "해당동", "시설명", "주소", "운영요일", "운영시간", "대표전화", "지도")


def cell(column, *paragraphs):
    content = "".join(f"<p><run><t>{text}</t></run></p>" for text in paragraphs)
    return f'<tc><cellAddr colAddr="{column}"/><subList>{content}</subList></tc>'


def build_hwpx(path):
    header = "".join(cell(index, value) for index, value in enumerate(HEADERS))
    data = "".join(
        [
            cell(0, "1"),
            cell(1, "거제1동"),
            cell(2, "테스트 쉼터"),
            cell(3, "주소 끝 공백 "),
            cell(4, "월~금"),
            cell(5, "09:00~18:00", "(토 10:00~14:00)"),
            cell(6, "051-000-0000"),
            cell(7, ""),
        ]
    )
    section = (
        '<sec xmlns="http://www.hancom.co.kr/hwpml/2011/section">'
        f'<tbl colCnt="8"><tr>{header}</tr><tr>{data}</tr></tbl>'
        "</sec>"
    )
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("Contents/section0.xml", section)


class ParseHwpxTest(unittest.TestCase):
    def test_projects_named_headers_and_preserves_whitespace(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "sample.hwpx"
            build_hwpx(source)

            rows = parse_hwpx(source)

        self.assertEqual(
            rows,
            (
                (
                    1,
                    "테스트 쉼터",
                    "주소 끝 공백 ",
                    "051-000-0000",
                    "거제1동",
                    "09:00~18:00\n(토 10:00~14:00)",
                    "월~금",
                    "거제1동",
                ),
            ),
        )


class ConvertTest(unittest.TestCase):
    def test_writes_parsed_rows_into_fixed_xls_template(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            source = directory / "sample.hwpx"
            output = directory / "result.xls"
            template = Path(__file__).with_name("restarea-template.xls")
            build_hwpx(source)

            result = convert(source, output, template)

            self.assertEqual(result, output)
            self.assertTrue(output.exists())

            excel = win32com.client.DispatchEx("Excel.Application")
            excel.Visible = False
            excel.DisplayAlerts = False
            workbook = None
            try:
                workbook = excel.Workbooks.Open(str(output), ReadOnly=True)
                values = workbook.Worksheets(1).Range("A1:H3").Value
                workbook.Close(False)
                workbook = None
            finally:
                if workbook is not None:
                    workbook.Close(False)
                excel.Quit()

        self.assertEqual(values[0][0], "무더위 쉼터 현황")
        self.assertEqual(
            values[1],
            ("번호", "쉼터명", "주소", "연락처", "해당동", "운영시간", "운영요일", "해당동"),
        )
        self.assertEqual(
            values[2],
            (
                1.0,
                "테스트 쉼터",
                "주소 끝 공백 ",
                "051-000-0000",
                "거제1동",
                "09:00~18:00\n(토 10:00~14:00)",
                "월~금",
                "거제1동",
            ),
        )


if __name__ == "__main__":
    unittest.main()
