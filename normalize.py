from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from automation.adapters.registry import normalize_html


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Normalize sanitized board HTML to WorkItem JSON.")
    parser.add_argument("board_type")
    parser.add_argument("html_path", type=Path)
    parser.add_argument("output_path", type=Path)
    parser.add_argument("--board-id", help="GnuBoard board id, e.g. alpha or beta")
    parser.add_argument("--source-url", required=True)
    args = parser.parse_args(argv)

    source = {"url": args.source_url}
    if args.board_id:
        source["board_id"] = args.board_id

    try:
        html = args.html_path.read_text(encoding="utf-8")
        item = normalize_html(args.board_type, html, source)
        args.output_path.write_text(
            json.dumps(item.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"normalize failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
