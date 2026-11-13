#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nww_write_chapter.py — 单一写章编排库（Task 1 / C-P0-2 + Task 5 quality_loop）。

统一 CLI / Web / Skill 的确定性后半链：
  packet_v2 + repair → generate → enforce → deslop(quality_loop) → post_write → commit

禁止跳步（阶段必记录）；首个 hard BLOCK → blocked_reason 非空，其余阶段 skip，
不再调用 run_post_write / commit（fail-closed，禁止失败后继续突变）。
deslop 经 run_quality_loop：passed=False 时 stages.deslop=block，不调用 post_write/commit。
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import nww_gates_shared as gates  # noqa: E402
import nww_quality_loop as quality_loop  # noqa: E402

_STAGE_ORDER = ("packet", "generate", "enforce", "deslop", "post_write", "commit")


@dataclass
class WriteChapterResult:
    chapter_id: int
    prose_path: Path
    packet: dict
    stages: dict[str, str] = field(default_factory=dict)
    # packet|generate|enforce|deslop|post_write|commit → ok|block|skip|warn
    blocked_reason: str | None = None


def _resolve_prose(book: Path, chapter_id: int, prose_path: Path | None) -> Path | None:
    if prose_path is not None:
        return Path(prose_path)
    try:
        from nww_post_write import resolve_prose_file
        return resolve_prose_file(book, chapter_id)
    except Exception:  # noqa: BLE001
        return None


def _skip_remaining(stages: dict[str, str], after: str) -> None:
    """Mark every stage after `after` as skip (inclusive of later hard steps)."""
    seen = False
    for name in _STAGE_ORDER:
        if name == after:
            seen = True
            continue
        if seen and name not in stages:
            stages[name] = "skip"


def run_write_chapter(
    book: Path,
    chapter_id: int,
    *,
    prose_path: Path | None = None,
    skip_generate: bool = False,
    policy: Any = None,  # PolicyBundle | None（Task 2）；未传则调用方自行 load_policy
    packet: dict | None = None,  # 调用方已构建则复用，避免双次构建（Task 1 Minor）
) -> WriteChapterResult:
    """共享写章编排。首个 hard BLOCK 即停：不调用 post_write / commit。

    enforce 与 post_write Step1 对齐：有正文时失败记 warn（非 hard block），继续。
    policy：可选 PolicyBundle；本编排暂不消费闸配置（闸在 track_commit 内 load_policy），
    保留参数供调用方注入/测试透传，避免 Task 1 API 破裂。
    """
    _ = policy
    book = Path(book)
    stages: dict[str, str] = {}
    blocked_reason: str | None = None
    prose = _resolve_prose(book, chapter_id, prose_path)

    def _blocked(stage: str, reason: str) -> WriteChapterResult:
        nonlocal blocked_reason
        stages[stage] = "block"
        blocked_reason = blocked_reason or reason
        _skip_remaining(stages, stage)
        return WriteChapterResult(
            chapter_id=chapter_id,
            prose_path=Path(prose) if prose else book / "正文" / f"第{chapter_id}章.md",
            packet=packet,
            stages=stages,
            blocked_reason=blocked_reason,
        )

    # 1. packet_v2 + repair（调用方已传 packet 则复用，避免双次构建）
    if packet is None:
        try:
            packet = gates.build_packet_v2(book, chapter_id)
            packet = gates.repair_packet_contract(book, chapter_id, packet)
            stages["packet"] = "ok"
        except Exception as exc:  # noqa: BLE001 — 合同/缺细纲均记 hard block
            packet = packet or {}
            return _blocked("packet", f"packet: {exc}")
    else:
        stages["packet"] = "ok"

    # 2. generate
    if skip_generate:
        stages["generate"] = "skip"
        if prose is None:
            prose = _resolve_prose(book, chapter_id, None)
    else:
        if prose is None or not Path(prose).is_file():
            return _blocked("generate", "generate: 正文未落盘且未 skip_generate")
        stages["generate"] = "ok"

    # 3. enforce — warn-only when prose exists (mirrors post_write Step1 WARNING-continue);
    #    missing prose is a hard block.
    if prose is not None and Path(prose).is_file():
        enf = gates.run_enforce(book, Path(prose))
        if enf.get("ok"):
            stages["enforce"] = "ok"
        else:
            stages["enforce"] = "warn"
            # Not hard-block: do not set blocked_reason; continue to deslop/post_write.
    else:
        return _blocked("enforce", "enforce: 正文缺失")

    # 4. deslop / quality_loop（闭环：blocking → rewriter → re-scan；此处 rewriter=None=CI 判定）
    ql = quality_loop.run_quality_loop(Path(prose), max_rounds=3, rewriter=None)
    if not ql.passed:
        return _blocked(
            "deslop",
            ql.blocked_reason or "quality_loop: blocking_findings",
        )
    stages["deslop"] = "ok"

    # 5. post_write（paragraph_gate + commit）— only if no prior hard block
    skip_enf = stages.get("enforce") in ("ok", "warn")
    post = gates.run_post_write(
        book, chapter_id, Path(prose) if prose else None, skip_enforce=skip_enf
    )
    if post.get("ok"):
        stages["post_write"] = "ok"
        stages["commit"] = "ok"
    else:
        stages["post_write"] = "block"
        stages["commit"] = "block"
        blocked_reason = blocked_reason or (
            f"post_write: {post.get('stderr') or post.get('stdout') or 'BLOCK'}"
        )

    return WriteChapterResult(
        chapter_id=chapter_id,
        prose_path=Path(prose) if prose else book / "正文" / f"第{chapter_id}章.md",
        packet=packet,
        stages=stages,
        blocked_reason=blocked_reason,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="NWW write_chapter 共享编排")
    ap.add_argument("book", type=Path)
    ap.add_argument("chapter_id", type=int)
    ap.add_argument("--prose", type=Path, default=None)
    ap.add_argument("--skip-generate", action="store_true")
    args = ap.parse_args(argv)
    result = run_write_chapter(
        args.book,
        args.chapter_id,
        prose_path=args.prose,
        skip_generate=args.skip_generate,
    )
    print(f"stages={result.stages}")
    if result.blocked_reason:
        print(f"BLOCKED: {result.blocked_reason}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
