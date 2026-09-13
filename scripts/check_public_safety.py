#!/usr/bin/env python3
"""Gate the repository before it is published or exported.

The fixture policy in ``fixtures/sanitized/POLICY.md`` already names the
classes of content that must never leave the machine. This scanner applies the
same policy to the whole tree, reusing ``sanitize_fixture``'s patterns as
policy input rather than restating them, so the two cannot drift apart.

A finding reports the file and line number and nothing else. Printing the
matched text would copy the secret into a terminal, a CI log and possibly an
issue, which is precisely what the gate exists to prevent.

Exit codes:
  0  no findings
  1  at least one finding
  2  the invocation was wrong
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from sanitize_fixture import FORBIDDEN_PATTERNS  # noqa: E402

# Directories the repository already treats as local-only (.gitignore) plus
# version-control and build noise. Scanning them would report content that is
# never published in the first place.
IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".gstack",
        ".session",
        ".superpowers",
        ".worktrees",
        "worktrees",
        "__pycache__",
        "file",
        "masks",
        "node_modules",
        "raw",
    }
)

# Binary payloads that plausibly carry unsanitized business documents. The
# repository's own committed binaries (icons, the XLS template) are allowed by
# extension below.
PRIVATE_ARCHIVE_SUFFIXES = frozenset({".zip", ".7z", ".rar", ".hwp", ".hwpx", ".docx", ".pptx"})
ALLOWED_BINARY_SUFFIXES = frozenset({".png", ".ico", ".jpg", ".jpeg", ".gif", ".xls", ".woff", ".woff2"})

# Additional classes the fixture policy names but the fixture scanner does not
# need: generic credential assignments and bearer tokens.
EXTRA_PATTERNS = (
    (
        "credential_assignment",
        re.compile(
            r"(?i)\b(?:password|passwd|secret|api[-_ ]?key|access[-_ ]?token|bot[-_ ]?token|token)\b"
            r"\s*[=:]\s*[\"']?[A-Za-z0-9._~+/=-]{8,}"
        ),
    ),
    ("bearer_token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._-]{12,}")),
)


# Hosts the repository uses on purpose to stand in for a real one.  A
# sanitized fixture is supposed to contain these, so flagging them would train
# a reader to ignore the gate -- the failure mode a security check can least
# afford.  The list is explicit rather than a ".local wildcard": a new internal
# hostname must still trip the gate even though it ends in .local.
PLACEHOLDER_HOSTS = (
    "fixture.local",
    "invalid.local",
    "upmuzadong.local",
    "example.invalid",
    "example.com",
    "example.org",
)
PLACEHOLDER_URL = re.compile(
    r"https?://(?:" + "|".join(re.escape(host) for host in PLACEHOLDER_HOSTS) + r")[^\s\"'<>)\]]*"
)

# Files whose purpose is to contain unsafe-looking values: a test that asserts
# a token is rejected has to hold a token.  Excluding them is a real limitation
# of this gate, not a clean result -- they need human review, and the allowlist
# is asserted to cover test paths only so it cannot quietly grow.
TEST_PATH = re.compile(r"(?:\A|/)(?:tests/|test_[^/]+\.py\Z|[^/]+\.test\.mjs\Z)")

# A value with no digit that reads as an identifier is a code reference such as
# `token: telegramToken`, not a literal secret.
_IDENTIFIER_VALUE = re.compile(r"\A[A-Za-z_][A-Za-z_]*\Z")
_VALUE_AFTER_ASSIGNMENT = re.compile(r"[=:]\s*[\"']?([A-Za-z0-9._~+/=-]+)")


def _patterns() -> tuple[tuple[str, re.Pattern[str]], ...]:
    return tuple(FORBIDDEN_PATTERNS) + EXTRA_PATTERNS


def _is_code_reference(line: str) -> bool:
    """True when a credential-shaped match assigns a bare identifier."""
    match = _VALUE_AFTER_ASSIGNMENT.search(line)
    return bool(match and _IDENTIFIER_VALUE.fullmatch(match.group(1)))


def _is_ignored(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    return any(part in IGNORED_DIRECTORIES for part in relative.parts)


def _candidate_files(root: Path) -> list[Path]:
    """List the files an export would contain.

    Inside a git work tree only tracked files ship, so only those are scanned;
    an untracked scratch file is not part of the export. Outside one (a
    temporary export directory, a test fixture) every file is scanned.
    """
    try:
        import subprocess

        completed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return sorted(root.rglob("*"))
    names = [name for name in completed.stdout.decode("utf-8", "replace").split("\0") if name]
    if not names:
        return sorted(root.rglob("*"))
    return sorted(root / name for name in names)


def scan_text(text: str) -> list[tuple[int, str]]:
    """Return (line number, finding name) pairs, never the matched value.

    Documented placeholder URLs are removed before matching rather than
    allow-listed afterwards, so a real internal host on the same line as a
    placeholder is still reported.
    """
    findings: list[tuple[int, str]] = []
    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = PLACEHOLDER_URL.sub("", raw_line)
        matched = {name for name, pattern in _patterns() if pattern.search(line)}
        for name in sorted(matched):
            if name in {"session_token", "credential_assignment"} and _is_code_reference(line):
                continue
            # A bare post id discloses nothing on its own; it only matters
            # alongside a host that identifies where the post lives.
            if name == "source_identifier" and "internal_url" not in matched:
                continue
            findings.append((number, name))
    return findings


def scan_tree(root: Path | str) -> list[str]:
    """Scan ``root`` and return sorted, value-free finding descriptions."""
    root = Path(root)
    findings: list[str] = []
    for path in _candidate_files(root):
        if not path.is_file() or _is_ignored(path, root):
            continue
        relative = path.relative_to(root).as_posix()
        if TEST_PATH.search(relative):
            continue
        suffix = path.suffix.lower()

        if suffix in PRIVATE_ARCHIVE_SUFFIXES:
            findings.append(f"{relative}: private_document_binary")
            continue
        if suffix in ALLOWED_BINARY_SUFFIXES:
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            # Undecodable bytes are not scannable text; a binary that matters
            # is caught by suffix above.
            continue

        for number, name in scan_text(text):
            findings.append(f"{relative}:{number}: {name}")
    return sorted(findings)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail when tracked content violates the public-safety policy."
    )
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0

    if not args.root.is_dir():
        print("check_public_safety: root is not a directory", file=sys.stderr)
        return 2

    findings = scan_tree(args.root)
    for finding in findings:
        print(finding)
    print(f"{len(findings)} findings")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
