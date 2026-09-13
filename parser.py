"""
hwpx 표준 양식 파서 — 표준 라이브러리만 (zipfile + xml.etree)

표준 양식이면 계약 1 의 target/blocks 를 돌려주고, 아니면 None 을 돌려준다.
안 맞는 문서를 억지로 파싱하지 않는다. 실패는 사람에게 넘기는 정상 경로다.

사용:
    python parser.py 신청서.hwpx        # JSON 출력
    python parser.py --selftest         # file/work/*.hwpx 로 자체검증
"""
import json
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

HEADERS = ["사이트명", "메뉴명", "위치", "내용", "담당자명", "행정번호"]
STANDARD_MIN = 5          # 6개 중 5개 이상 맞으면 표준 양식
PARSER_VERSION = "hwpx-standard-v1"

norm = lambda s: re.sub(r"\s+", "", s or "")

MAX_XML_BYTES = 32 * 1024 * 1024  # 표준 신청서 section 보다 훨씬 큰 상한
_DTD_MARKER = re.compile(rb"<!(?:DOCTYPE|ENTITY)", re.IGNORECASE)


class UnsafeXMLError(ValueError):
    """첨부 XML 이 안전 한도를 벗어나 파싱을 거부했음을 나타낸다."""


# ── XML 헬퍼 ────────────────────────────────────────────────
def _safe_xml(z, name):
    """zip 멤버 하나를 XML 로 읽되, 엔티티 폭탄과 과대 입력을 막는다.

    첨부 hwpx 는 신뢰할 수 없는 입력이다. 표준 라이브러리만 쓰는 파서라
    defusedxml 대신 직접 막는다: 압축 해제 크기를 먼저 확인해 zip 폭탄을
    거르고, DTD/ENTITY 선언을 거부해 billion laughs·quadratic blowup 을
    막는다. 외부 엔티티는 expat 기본값에서 이미 무력(파일 읽기·SSRF 불가)하다.
    거부는 UnsafeXMLError 로 알려 호출부가 크래시 대신 검토로 넘기게 한다.
    """
    info = z.getinfo(name)  # 없는 멤버는 KeyError — 기존 호출부가 그대로 처리
    if info.file_size > MAX_XML_BYTES:
        raise UnsafeXMLError(f"XML member too large: {name} ({info.file_size} bytes)")
    raw = z.read(name)
    if _DTD_MARKER.search(raw):
        raise UnsafeXMLError(f"XML with a DTD/entity declaration is refused: {name}")
    return ET.fromstring(raw)


def ln(el):
    """네임스페이스 떼고 태그 이름만. prefix 가 파일마다 달라서 필요하다."""
    return el.tag.split("}")[-1] if isinstance(el.tag, str) else ""


def own(el, tag):
    """el 아래에서 tag 를 찾되 중첩 tbl 안으로는 들어가지 않는다.

    ET.iter() 를 쓰면 중첩 표의 행/셀/글자가 바깥 표로 딸려 올라온다.
    무더위쉼터 문서에서 바깥 셀 하나가 497칸짜리 괴물이 됐던 원인.
    """
    out = []

    def walk(e):
        for c in e:
            if ln(c) == tag:
                out.append(c)
            elif ln(c) == "tbl":
                continue
            else:
                walk(c)

    walk(el)
    return out


def para_text(p):
    return "".join(t.text or "" for t in own(p, "t"))


def cell(tc):
    """셀 텍스트. p 단위로 끊어 \\n 으로 잇는다 (안 하면 문단이 다 붙는다)."""
    return "\n".join(para_text(p) for p in own(tc, "p")).strip()


def cell_runs(tc, colors):
    """셀 안의 유색 런. 색을 고르지 않는다 — 검정이 아니면 전부 후보다.

    파랑만 잡으면 안 된다: 표본 10건 중 빨강만 쓴 문서, 색을 아예 안 쓴
    문서가 각각 있었다. 어떤 색이 무슨 뜻인지는 모듈 2 가 판정한다.
    """
    out = []
    for p in own(tc, "p"):
        for run in own(p, "run"):
            color = colors.get(run.get("charPrIDRef"))
            text = "".join(t.text or "" for t in own(run, "t")).strip()
            if text and color and color.upper() not in ("#000000", "NONE"):
                out.append({"color": color.upper(), "text": text})
    return out


# ── 문서 읽기 ───────────────────────────────────────────────
def read_colors(z):
    """charPr id -> textColor. section 의 charPrIDRef 가 이걸 참조한다."""
    try:
        root = _safe_xml(z, "Contents/header.xml")
    except KeyError:
        return {}
    return {el.get("id"): el.get("textColor")
            for el in root.iter() if ln(el) == "charPr"}


def read_tables(z, colors):
    """[(rows, runs)] — rows 는 셀 텍스트 2차원, runs 는 표 전체의 유색 런."""
    tables = []
    names = sorted(n for n in z.namelist()
                   if re.match(r"Contents/section\d+\.xml", n))
    for name in names:
        for tbl in _safe_xml(z, name).iter():
            if ln(tbl) != "tbl":
                continue
            rows, runs = [], []
            for tr in own(tbl, "tr"):
                cells = []
                for tc in own(tr, "tc"):
                    cells.append(cell(tc))
                    runs.extend(cell_runs(tc, colors))
                if cells:
                    rows.append(cells)
            if rows:
                tables.append((rows, runs))
    return tables


# ── 판정 · 추출 ─────────────────────────────────────────────
def found_headers(tables):
    blob = norm("".join(c for rows, _ in tables for r in rows for c in r))
    return [h for h in HEADERS if norm(h) in blob]


def extract_fields(rows):
    """라벨 셀 바로 다음 칸이 값. '메 뉴 명' 처럼 자간 띄운 라벨도 norm 으로 잡힌다."""
    out = {}
    for row in rows:
        for i, c in enumerate(row[:-1]):
            for h in HEADERS:
                if norm(c) == norm(h) and h not in out:
                    out[h] = row[i + 1].strip()
    return out


def parse(path):
    """표준 양식이면 dict, 아니면 None."""
    z = zipfile.ZipFile(path)
    colors = read_colors(z)
    tables = read_tables(z, colors)
    if len(found_headers(tables)) < STANDARD_MIN:
        return None

    # 헤더를 가장 많이 품은 표가 필드 표. 나머지는 데이터 표다.
    def score(rows):
        blob = norm("".join(c for r in rows for c in r))
        return sum(1 for h in HEADERS if norm(h) in blob)

    field_idx = max(range(len(tables)), key=lambda i: score(tables[i][0]))
    field_rows, field_runs = tables[field_idx]
    fields = extract_fields(field_rows)
    if len(fields) < STANDARD_MIN:
        return None

    # kind 는 채우지 않는다. 종류 판정은 모듈 2 몫이다.
    # "표 = 데이터축" 이라고 단정했다가 틀렸다: 건축과 문서의 14x12 표는
    # 데이터가 아니라 빈 칸으로 그린 순서도였다. 파서는 구조만 말한다.
    blocks = []

    # 본문: 필드 표 안의 단독 셀. 필드 행은 전부 2칸/4칸 짝이라
    # 1칸짜리 행은 정의상 본문이다. 길이 임계값이 필요 없다.
    for row in field_rows:
        if len(row) == 1 and row[0]:
            blocks.append({
                "kind": None,
                "body": row[0],
                "table": None,
                "changes": [c for c in field_runs if c["text"] in row[0]],
            })

    # 표: 필드 표를 뺀 나머지 전부
    for i, (rows, runs) in enumerate(tables):
        if i == field_idx:
            continue
        blocks.append({
            "kind": None,
            "body": None,
            "table": rows,
            "changes": runs,
        })

    return {
        "form": "standard",
        "fields": fields,
        "target": {
            "site": fields.get("사이트명"),
            "menu_path": fields.get("위치"),
            "url": None,                    # 크롤러가 채운다
        },
        "blocks": blocks,
    }


# ── 자체검증 ────────────────────────────────────────────────
def selftest():
    work = Path(__file__).parent / "file" / "work"
    files = sorted(work.glob("*.hwpx"))
    assert len(files) == 9, f"표본 9건이어야 하는데 {len(files)}건"

    got = {f.name: parse(f) for f in files}
    std = {n: r for n, r in got.items() if r}
    assert len(std) == 7, f"표준 7건이어야 하는데 {len(std)}건: {sorted(std)}"

    # 요청서가 아닌 두 건은 반드시 실패해야 한다 (오탐 0)
    for key in ("문화관광", "추천서식"):
        hit = [n for n in got if key in n]
        assert hit and got[hit[0]] is None, f"{key} 는 비표준이어야 한다"

    # 필드 정확도. 담당자명은 마스킹 대상이라 값을 코드에 박지 않는다
    napse = next(r for n, r in std.items() if "납세자보호관" in n)
    assert re.fullmatch(r"0\d{1,2}-\d{3,4}-\d{4}", napse["fields"]["행정번호"]), napse["fields"]
    assert napse["target"]["site"].endswith("홈페이지"), napse["target"]
    for r in std.values():
        assert r["fields"].get("담당자명", "").strip(), r["fields"]

    # 중첩 표 회귀: 바깥 셀이 안쪽 표를 삼키면 안 된다
    heat = next(r for n, r in std.items() if "무더위쉼터" in n)
    data = [b for b in heat["blocks"] if b["table"]]
    assert len(data) == 1, f"표 1개여야 하는데 {len(data)}개"
    assert len(data[0]["table"]) == 62, len(data[0]["table"])
    assert max(len(r) for r in data[0]["table"]) == 8
    body = [b["body"] for b in heat["blocks"] if b["body"]]
    assert len(body) == 1 and len(body[0]) < 1000, "본문 셀이 중첩 표를 삼켰다"
    assert "무더위쉼터 현황" in body[0], body      # 표 제목·기준일을 잃지 않는다

    # kind 는 파서가 채우지 않는다 (모듈 2 몫)
    assert all(b["kind"] is None for r in std.values() for b in r["blocks"])

    # 색: 파랑 하드코딩이면 놓쳤을 문서
    menu = next(r for n, r in std.items() if "메뉴+추가" in n)
    colors = {c["color"] for b in menu["blocks"] for c in b["changes"]}
    assert colors and "#0000FF" not in colors, f"빨강 전용 문서인데 {colors}"

    print(f"OK  표준 {len(std)}/9, 비표준 {9 - len(std)} (오탐 0)")
    for n, r in sorted(std.items()):
        nb = len([b for b in r["blocks"] if b["body"]])
        nd = len([b for b in r["blocks"] if b["table"]])
        nc = sum(len(b["changes"]) for b in r["blocks"])
        print(f"    본문{nb} 표{nd} 변경{nc:3}  {n[:52]}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) < 2:
        print(__doc__)
    elif sys.argv[1] == "--selftest":
        selftest()
    else:
        r = parse(sys.argv[1])
        if r is None:
            print("비표준 양식 — 사람에게 넘긴다", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(r, ensure_ascii=False, indent=2))
