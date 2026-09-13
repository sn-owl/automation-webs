"""Mask untrusted WorkItem text before the Hermes advisory boundary."""

from __future__ import annotations

import re
from typing import Any

MAX_HERMES_BODY_CHARS = 6000

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(?:authorization|proxy-authorization|password|passwd|pwd|"
    r"api[-_ ]?(?:key|token)|access[-_]?token|refresh[-_]?token|"
    r"session[-_]?(?:id|token|cookie)?|cookie|cookies|credential|credentials|"
    r"secret|private[-_ ]+key)\s*[:=]\s*[^\s,;]+"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_URL = re.compile(r"https?://[^\s<>\"']+")
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PHONE = re.compile(r"(?<!\d)(?:0\d{1,2})[- .]\d{3,4}[- .]\d{4}(?!\d)")
_SOURCE_ID = re.compile(r"(?i)\b(?:wr_id|dataSid|fileSid)\s*=\s*\d+\b")
_PERSONAL_FIELD = re.compile(
    r"(?im)((?:담당자|작성자|요청자|민원인|성명|부서명|전화번호|휴대전화|휴대폰|"
    r"이메일|e-mail)\s*[:：]\s*)[^\n]+"
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def mask_for_hermes(value: Any) -> str:
    """Keep operational meaning while removing common identity/secrets."""
    if not isinstance(value, str):
        return ""
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    text = _PERSONAL_FIELD.sub(r"\1[개인정보 생략]", text)
    text = _SECRET_ASSIGNMENT.sub("[민감정보 생략]", text)
    text = _BEARER.sub("Bearer [민감정보 생략]", text)
    text = _URL.sub("[URL 생략]", text)
    text = _EMAIL.sub("[이메일 생략]", text)
    text = _PHONE.sub("[전화번호 생략]", text)
    text = _SOURCE_ID.sub("[게시글 식별자 생략]", text)
    text = _CONTROL.sub("", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    while text.startswith("\n"):
        text = text[1:]
    while text.endswith("\n"):
        text = text[:-1]
    if len(text) > MAX_HERMES_BODY_CHARS:
        text = text[:MAX_HERMES_BODY_CHARS].rstrip() + "\n[본문 일부 생략]"
    return text
