#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nww_skill_preflight.py — Skill long-write 写前契约预检（Task 7 / C-P1-2）。

Skill Phase1 与 CLI/Web 共用同一 builder_v2 + repair + 内容硬闸，禁止方法论拼装绕过：
  1. build_packet_v2（skills 副本优先）
  2. repair_packet_contract（character/facts 兜底闸，strict 下可 BLOCK）
  3. assert_packet_content_anchors + state.global / 主角境界闸（与 builder 内部同源）

成功：写 大纲/细纲_第{N:03d}章.contract.json（builder sidecar 已写则校验存在）
      + 追踪/_preflight_ok_ch{N} 标记（guard-outline-before-prose 可消费）；
      stdout 打 JSON {ok:true, chapter_id, packet_meta, problems:[]}
失败：exit 2，stdout JSON {ok:false, problems:[...]}；exit 0 前绝不半截放行。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import nww_gates_shared as gates  # noqa: E402


def skill_preflight(book: Path, chapter_id: int) -> dict:
    """跑 packet_v2 + repair + content anchors；失败 raise RuntimeError（消息含 problems）。"""
    book = Path(book)
    if not (book / "bible.json").is_file():
        raise RuntimeError(f"bible.json 缺失: {book}")
    packet = gates.build_packet_v2(book, chapter_id)
    packet = gates.repair_packet_contract(book, chapter_id, packet)
    from nww_content_gates import assert_packet_content_anchors
    assert_packet_content_anchors(packet, chapter_id)
    return packet


def _write_ok_marker(book: Path, chapter_id: int, packet: dict) -> Path:
    marker = book / "追踪" / f"_preflight_ok_ch{chapter_id}"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps({
            "chapter_id": chapter_id,
            "schema_version": (packet.get("meta") or {}).get("schema_version", ""),
            "outline_hash": (packet.get("meta") or {}).get("outline_hash", ""),
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    return marker


def main() -> int:
    ap = argparse.ArgumentParser(description="Skill long-write 写前契约预检")
    ap.add_argument("--book", required=True, help="书根目录")
    ap.add_argument("--chapter", required=True, type=int, help="章号")
    ap.add_argument("--no-marker", action="store_true", help="不写 _preflight_ok_chN 标记（测试用）")
    args = ap.parse_args()

    book = Path(args.book).resolve()
    problems: list[str] = []
    try:
        packet = skill_preflight(book, args.chapter)
    except Exception as exc:  # noqa: BLE001
        problems.append(str(exc))

    if problems:
        print(json.dumps({"ok": False, "chapter_id": args.chapter, "problems": problems},
                         ensure_ascii=False))
        return 2

    if not args.no_marker:
        try:
            _write_ok_marker(book, args.chapter, packet)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"preflight 标记写入失败（不阻断）: {exc}")

    print(json.dumps({
        "ok": True,
        "chapter_id": args.chapter,
        "parse_status": (packet.get("meta") or {}).get("parse_status", ""),
        "characters": [c.get("name") for c in (packet.get("character_contract") or [])],
        "pending_foreshadows": [r.get("id") for r in (packet.get("pending_foreshadows") or [])],
        "problems": problems,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
