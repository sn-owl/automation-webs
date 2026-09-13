import argparse
import os
import re
import shutil
import uuid
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile


# 첨부 hwpx 는 신뢰할 수 없는 입력이다. 아래 상한과 DTD 거부로 zip 폭탄과
# 엔티티 확장(billion laughs / quadratic blowup), 과대 할당을 막는다.
MAX_XML_BYTES = 32 * 1024 * 1024   # section0.xml 압축 해제 크기 상한
MAX_TABLE_COLUMNS = 512            # 무더위쉼터 표는 8열; 상한은 넉넉히
_DTD_MARKER = re.compile(rb"<!(?:DOCTYPE|ENTITY)", re.IGNORECASE)


# Excel 의 Workbooks.Open 은 전체 경로가 약 218자를 넘으면 거부하며, 그때
# 돌아오는 것은 원인을 알 수 없는 com_error 뿐이다. 임시 파일은 os.replace 로
# 원자적 교체를 하려고 산출물과 같은 디렉터리에 두어야 하므로, 경로 예산을
# 지키는 방법은 파일 이름을 짧게 유지하는 것이다.
EXCEL_MAX_PATH = 218


class ExcelPathTooLongError(OSError):
    """Excel 이 열 수 없는 길이의 경로."""


REQUIRED_HEADERS = ("연번", "해당동", "시설명", "주소", "운영요일", "운영시간", "대표전화")
OUTPUT_MAPPING = ("연번", "시설명", "주소", "대표전화", "해당동", "운영시간", "운영요일", "해당동")


def _local_name(tag):
    return tag.rsplit("}", 1)[-1]


def _cell_text(cell):
    sublist = next((node for node in cell if _local_name(node.tag) == "subList"), None)
    if sublist is None:
        return ""

    paragraphs = []
    for paragraph in (node for node in sublist if _local_name(node.tag) == "p"):
        text = "".join((node.text or "") for node in paragraph.iter() if _local_name(node.tag) == "t")
        if text.strip():
            paragraphs.append(text)
    return "\n".join(paragraphs)


def _table_rows(table):
    width = int(table.attrib["colCnt"])
    if not 0 <= width <= MAX_TABLE_COLUMNS:
        raise ValueError(f"표 열 개수가 허용 범위를 벗어났습니다: {width}")
    rows = []
    for table_row in (node for node in table if _local_name(node.tag) == "tr"):
        row = [""] * width
        for cell in (node for node in table_row if _local_name(node.tag) == "tc"):
            address = next(node.attrib for node in cell.iter() if _local_name(node.tag) == "cellAddr")
            column = int(address["colAddr"])
            if not 0 <= column < width:
                raise ValueError(f"셀 열 주소가 표 범위를 벗어났습니다: {column}")
            row[column] = _cell_text(cell)
        rows.append(row)
    return rows


def parse_hwpx(path):
    source = Path(path)
    if source.suffix.lower() != ".hwpx":
        raise ValueError("입력 파일을 한글에서 .hwpx 형식으로 저장한 뒤 실행하세요.")

    with ZipFile(source) as package:
        info = package.getinfo("Contents/section0.xml")
        if info.file_size > MAX_XML_BYTES:
            raise ValueError("section0.xml 이 허용 크기를 초과합니다.")
        raw = package.read("Contents/section0.xml")
        if _DTD_MARKER.search(raw):
            raise ValueError("DTD/엔티티 선언이 있는 XML 은 처리하지 않습니다.")
        section = ElementTree.fromstring(raw)

    candidates = []
    for table in (node for node in section.iter() if _local_name(node.tag) == "tbl"):
        rows = _table_rows(table)
        if rows and set(REQUIRED_HEADERS).issubset(rows[0]):
            candidates.append(rows)

    if len(candidates) != 1:
        raise ValueError(f"무더위쉼터 표를 정확히 하나 찾아야 하지만 {len(candidates)}개를 찾았습니다.")

    rows = candidates[0]
    column = {name: index for index, name in enumerate(rows[0])}
    output = []
    for source_row in rows[1:]:
        if not any(source_row):
            continue
        sequence = source_row[column["연번"]]
        if not sequence.isdigit():
            raise ValueError(f"연번이 숫자가 아닙니다: {sequence!r}")
        output.append((int(sequence), *(source_row[column[name]] for name in OUTPUT_MAPPING[1:])))
    return tuple(output)


def convert(source, output, template=None):
    source = Path(source)
    output = Path(output)
    template = Path(template) if template else Path(__file__).with_name("restarea-template.xls")

    if output.suffix.lower() != ".xls":
        raise ValueError("출력 파일 확장자는 .xls여야 합니다.")
    if not template.is_file():
        raise FileNotFoundError(f"XLS 템플릿을 찾을 수 없습니다: {template}")
    if output.resolve() in (source.resolve(), template.resolve()):
        raise ValueError("출력 파일은 입력 파일 및 템플릿과 다른 경로여야 합니다.")

    rows = parse_hwpx(source)
    if not rows:
        raise ValueError("변환할 데이터 행이 없습니다.")

    output.parent.mkdir(parents=True, exist_ok=True)
    # 이전 이름은 `.{64자 실행 키}.{32자 uuid}.tmp.xls` 로 파일 이름만 106자였고,
    # 세션 디렉터리가 깊으면 그것만으로 Excel 의 경로 한계를 넘겼다. 같은
    # 디렉터리 안에서의 유일성만 있으면 되므로 uuid 앞 12자로 충분하다.
    temporary = output.with_name(f".{uuid.uuid4().hex[:12]}.tmp.xls")
    resolved_length = len(str(temporary.resolve()))
    if resolved_length > EXCEL_MAX_PATH:
        raise ExcelPathTooLongError(
            f"Excel 이 열 수 있는 경로 길이({EXCEL_MAX_PATH}자)를 넘었습니다: {resolved_length}자. "
            "산출물 디렉터리를 더 짧은 경로로 지정하세요."
        )
    shutil.copy2(template, temporary)
    excel = None
    workbook = None
    pythoncom = None
    com_initialized = False
    try:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        com_initialized = True
        excel = win32com.client.DispatchEx("Excel.Application")
        excel.Visible = False
        excel.DisplayAlerts = False
        workbook = excel.Workbooks.Open(str(temporary), ReadOnly=False)
        sheet = workbook.Worksheets(1)
        existing_last_row = sheet.UsedRange.Row + sheet.UsedRange.Rows.Count - 1
        output_last_row = 2 + len(rows)
        clear_last_row = max(existing_last_row, output_last_row)
        sheet.Range(f"A3:H{clear_last_row}").ClearContents()

        if output_last_row > existing_last_row:
            sheet.Range("A3:H3").Copy()
            sheet.Range(f"A{existing_last_row + 1}:H{output_last_row}").PasteSpecial(Paste=-4122)
            excel.CutCopyMode = False

        sheet.Range(f"A3:H{output_last_row}").Value = rows
        workbook.Save()
        workbook.Close(False)
        workbook = None
        excel.Quit()
        excel = None
        os.replace(temporary, output)
    finally:
        if workbook is not None:
            workbook.Close(False)
        if excel is not None:
            excel.Quit()
        if com_initialized:
            pythoncom.CoUninitialize()
        temporary.unlink(missing_ok=True)

    return output


def main():
    parser = argparse.ArgumentParser(description="무더위쉼터 HWPX 표를 고정 XLS 양식으로 변환합니다.")
    parser.add_argument("source", type=Path, help="한글에서 HWPX로 저장한 신청서")
    parser.add_argument("-o", "--output", type=Path, help="생성할 XLS 파일")
    parser.add_argument("--template", type=Path, help="사용할 XLS 템플릿")
    args = parser.parse_args()

    output = args.output or args.source.with_name(f"{args.source.stem}-restarea.xls")
    try:
        result = convert(args.source, output, args.template)
    except Exception as error:
        parser.exit(1, f"오류: {error}\n")
    print(result)


if __name__ == "__main__":
    main()
