# -*- coding: utf-8 -*-
"""NWW 门禁豁免审计工具（nww_override_audit）
负责只增不改地登记与列出 overrides.jsonl 审计日志。
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path
from typing import List, Optional


def add_override(book_dir: Path, gate: str, chapter: int, reason: str, expires: str, actor: str = "author") -> int:
    if not reason or not reason.strip() or not expires or not expires.strip():
        sys.stderr.write("[ERROR] reason 和 expires 必须非空\n")
        return 1

    tracking_dir = book_dir / "追踪"
    tracking_dir.mkdir(parents=True, exist_ok=True)
    jl = tracking_dir / "overrides.jsonl"

    record = {
        "gate": gate,
        "chapter": chapter,
        "reason": reason.strip(),
        "expires_at": expires.strip(),
        "actor": actor,
        "ts": datetime.datetime.now().astimezone().isoformat()
    }

    with jl.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return 0


def list_overrides(book_dir: Path) -> int:
    jl = book_dir / "追踪" / "overrides.jsonl"
    if not jl.is_file():
        return 0
    for line in jl.read_text(encoding="utf-8").splitlines():
        if line.strip():
            print(line.strip())
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(description="NWW 豁免审计")
    subparsers = parser.add_subparsers(dest="subcommand")

    add_parser = subparsers.add_parser("add")
    add_parser.add_argument("--book", required=True)
    add_parser.add_argument("--gate", required=True)
    add_parser.add_argument("--chapter", type=int, required=True)
    add_parser.add_argument("--reason", required=True)
    add_parser.add_argument("--expires", required=True)
    add_parser.add_argument("--actor", default="author")

    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--book", required=True)

    try:
        args = parser.parse_args(argv)
    except SystemExit as se:
        return se.code or 1

    if args.subcommand == "add":
        return add_override(Path(args.book), args.gate, args.chapter, args.reason, args.expires, args.actor)
    elif args.subcommand == "list":
        return list_overrides(Path(args.book))
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
