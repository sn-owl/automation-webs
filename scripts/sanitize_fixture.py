import re
from pathlib import Path


FORBIDDEN_PATTERNS = (
    ("person_name", re.compile(r"(?:담당자|작성자|요청자|민원인)\s*[:：]\s*[가-힣]{2,4}")),
    ("phone_number", re.compile(r"\b(?:0\d{1,2}-\d{3,4}-\d{4}|01\d-\d{3,4}-\d{4})\b")),
    (
        "internal_url",
        re.compile(
            r"https?://(?:cug\.thewebs\.kr(?::443)?|10(?:\.\d{1,3}){3}|172\.(?:1[6-9]|2\d|3[0-1])(?:\.\d{1,3}){2}|192\.168(?:\.\d{1,3}){2}|[^/\s\"']+\.local)(?:[^\s\"'<)]*)?"
        ),
    ),
    ("source_identifier", re.compile(r"(?:wr_id|dataSid|fileSid)=\d+")),
    (
        "session_token",
        re.compile(
            r"\b(?:PHPSESSID|JSESSIONID|SESSIONID|session|csrf|token)\b\s*[=:]\s*[A-Za-z0-9._~+/=-]{6,}",
            re.IGNORECASE,
        ),
    ),
)


def scan_fixture_text(text: str) -> list[str]:
    return [name for name, pattern in FORBIDDEN_PATTERNS if pattern.search(text)]


def sanitize_html(source: Path, output: Path, replacements: dict[str, str]) -> dict[str, int]:
    text = source.read_text(encoding="utf-8")
    replacement_count = 0
    for old in sorted(replacements, key=len, reverse=True):
        new = replacements[old]
        if old in text:
            replacement_count += text.count(old)
            text = text.replace(old, new)
    findings = scan_fixture_text(text)
    if findings:
        output.unlink(missing_ok=True)
        raise ValueError(", ".join(findings))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    return {"replacements": replacement_count}
