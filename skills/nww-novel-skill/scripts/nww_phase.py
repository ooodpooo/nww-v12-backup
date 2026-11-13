#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nww_phase.py — NWW-V12 写章流程阶段机（只读诊断，零耦合）。

回答「当前写到哪一步、缺什么、下一步做什么」，供断点续跑 / 会话启动定位。

设计原则：
- 只读：不写任何文件，不 import server/，不依赖网络。
- 零耦合：不修改现有 skill / agent / hook / 脚本，独立新增文件。
- 权威状态：以 `追踪/_tracking-state.json` 的 last_committed_chapter 为基准，
  细纲/正文/逐章记录的存在性为证据，派生视图一致性复用 `nww_track_commit.py check`。

阶段枚举（贴合 V12 写章流程）：
- no_project       : _tracking-state.json 缺失 → 先 /nww-setup init
- plan_needed      : 目标章缺细纲 → 先规划
- ready_to_write   : 有细纲无正文 → 可写正文
- draft_in_progress: 有正文未提交 → 走 enforce→deslop→consistency→commit
- inconsistent     : target 章已有 delta 但 last_committed_chapter 未同步 → 数据异常
- views_dirty      : 派生视图与 state 不一致（check 失败）→ 先修复再写

用法：
    python3 nww_phase.py --project {BOOK}
    python3 nww_phase.py --project {BOOK} --json
输出：
    --json 输出单行 JSON；否则输出人类可读文本。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

SCHEMA = "story-phase/v1"
DEFAULT_BACKUP_DIR_NAME = ".backup"


class PhaseError(ValueError):
    """Expected validation or state error."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise PhaseError(message)


def _json_line(payload: dict[str, Any]) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    sys.stdout.buffer.write(rendered.encode("utf-8"))
    sys.stdout.buffer.flush()


def _find_body_file(prose_dir: Path, chapter: int) -> Path | None:
    """正文文件名形如 第N章_*.md；支持卷目录（正文/第一卷/第N章*.md），
    同章的原稿备份（_原稿_ 或 .backup）不算正文。"""
    for path in sorted(prose_dir.rglob("第*章*.md")):
        if "_review_snapshots" in path.parts:
            continue        # 快照不是正文（与正文同形，只能按目录排除）
        match = re.fullmatch(r"第0*(\d+)章.*\.md", path.name)
        if not match or int(match.group(1)) != chapter:
            continue
        if "_原稿_" in path.name:
            continue
        return path
    return None


def _find_outline_file(outline_dir: Path, chapter: int) -> Path | None:
    """细纲文件名形如 细纲_第N章.md。"""
    for path in sorted(outline_dir.glob("细纲_第*章*.md")):
        match = re.fullmatch(r"细纲_第0*(\d+)章.*\.md", path.name)
        if match and int(match.group(1)) == chapter:
            return path
    return None


def _read_state(state_file: Path) -> dict[str, Any]:
    try:
        payload = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PhaseError(f"unable to read tracking state: {exc}") from exc
    require(isinstance(payload, dict), "tracking state must be an object")
    return payload


def _tracking_check(project: Path, track_commit: Path) -> tuple[bool, str]:
    """调用 nww_track_commit.py check 做派生视图一致性校验。

    成功 = 派生视图与 _tracking-state.json 完全一致；失败 = 不一致（需先修复）。
    只读调用，不产生副作用。
    """
    if not track_commit.is_file():
        return True, "nww_track_commit.py not found; skip consistency check"
    try:
        completed = subprocess.run(
            [sys.executable, str(track_commit), "check", "--project", str(project)],
            text=True, encoding="utf-8", capture_output=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return True, f"tracking check unavailable: {exc}"
    if completed.returncode == 0:
        return True, ""
    # 取 stderr 里第一个有意义的错误行：跳过 Traceback / File "..." / 内部调用栈行，
    # 只留 ERROR: 摘要；仍找不到则给通用提示，绝不把 traceback 泄给调用方。
    for line in (completed.stderr or completed.stdout or "").splitlines():
        stripped = line.strip()
        if not stripped or "Traceback" in stripped or stripped.startswith(("File ", "  ", "raise ", "ValueError", "TypeError", "KeyError")):
            continue
        if stripped.startswith("ERROR:"):
            return False, stripped[len("ERROR:"):].strip()
        return False, stripped
    return False, "derived views differ from tracking state"


def resolve_phase(project: Path, *, skip_consistency: bool = False) -> dict[str, Any]:
    project = project.resolve()
    tracking = project / "追踪"
    state_file = tracking / "_tracking-state.json"
    track_commit = Path(__file__).resolve().parent / "nww_track_commit.py"

    blocking: list[str] = []
    warnings: list[str] = []

    if not state_file.is_file():
        return {
            "schema": SCHEMA,
            "project": str(project),
            "phase": "no_project",
            "target_chapter": 1,
            "last_committed_chapter": 0,
            "state_revision": None,
            "evidence": {"state_exists": False},
            "blocking": ["tracking state missing; run /nww-setup init first"],
            "warnings": [],
            "next_action": "run /nww-setup to init the book project, then write 细纲_第1章.md",
        }

    state = _read_state(state_file)
    last = int(state.get("last_committed_chapter") or 0)
    revision = state.get("state_revision")
    target = last + 1

    outline_dir = project / "大纲"
    prose_dir = project / "正文"

    outline = _find_outline_file(outline_dir, target)
    body = _find_body_file(prose_dir, target)
    delta = tracking / "逐章记录" / f"第{target:03d}章.md"

    evidence = {
        "state_exists": True,
        "outline_exists": outline is not None,
        "body_exists": body is not None,
        "delta_exists": delta.is_file(),
    }

    # 派生视图一致性：views_dirty 优先于其余阶段（一致性破坏时禁止续写）
    if not skip_consistency:
        consistent, reason = _tracking_check(project, track_commit)
        if not consistent:
            return {
                "schema": SCHEMA,
                "project": str(project),
                "phase": "views_dirty",
                "target_chapter": target,
                "last_committed_chapter": last,
                "state_revision": revision,
                "evidence": evidence,
                "blocking": ["derived views differ from _tracking-state.json"],
                "warnings": [reason],
                "next_action": "run nww_track_commit.py check to see the mismatch, then re-render derived views before writing",
            }

    if outline is None:
        phase = "plan_needed"
        next_action = f"write 大纲/细纲_第{target}章.md first (no writing without an outline)"
        blocking.append(f"outline missing: 大纲/细纲_第{target}章.md")
    elif body is None:
        phase = "ready_to_write"
        next_action = f"run /nww-long-write for chapter {target} (outline ready, prose not written)"
    elif not delta.is_file():
        phase = "draft_in_progress"
        next_action = f"chapter {target} has prose but is not committed: run nww_enforce.py → /nww-deslop → /nww-consistency → commit"
        warnings.append("prose exists but no chapter delta; commit is pending")
    else:
        # 通过 check 的项目 target = last+1 章不可能有 delta（check_project 强制 delta ≤ last_committed），
        # 此处出现 delta 说明 last_committed_chapter 未同步，属数据异常而非正常「已提交」态。
        phase = "inconsistent"
        next_action = f"chapter {target} has a delta but last_committed_chapter is {last}; check nww_track_commit.py check and sync state"
        blocking.append(f"delta exists for chapter {target} but last_committed_chapter is {last} (unsynced)")

    return {
        "schema": SCHEMA,
        "project": str(project),
        "phase": phase,
        "target_chapter": target,
        "last_committed_chapter": last,
        "state_revision": revision,
        "evidence": evidence,
        "blocking": blocking,
        "warnings": warnings,
        "next_action": next_action,
    }


def _render_text(result: dict[str, Any]) -> str:
    lines = [
        f"阶段: {result['phase']}",
        f"目标章: {result['target_chapter']}（已提交至 {result['last_committed_chapter']}，revision {result['state_revision']}）",
        f"证据: 细纲={'有' if result['evidence'].get('outline_exists') else '无'} | "
        f"正文={'有' if result['evidence'].get('body_exists') else '无'} | "
        f"逐章记录={'有' if result['evidence'].get('delta_exists') else '无'}",
    ]
    for item in result.get("blocking", []):
        lines.append(f"阻断: {item}")
    for item in result.get("warnings", []):
        lines.append(f"警告: {item}")
    lines.append(f"下一步: {result['next_action']}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True, help="book project root containing 追踪/")
    parser.add_argument("--json", action="store_true", help="output single-line JSON")
    parser.add_argument("--skip-consistency", action="store_true", help="skip derived-view consistency check")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = resolve_phase(args.project, skip_consistency=args.skip_consistency)
    except PhaseError as exc:
        if args.json:
            _json_line(
                {
                    "schema": SCHEMA,
                    "project": str(args.project.resolve()),
                    "phase": "error",
                    "target_chapter": None,
                    "last_committed_chapter": None,
                    "state_revision": None,
                    "evidence": {},
                    "blocking": [str(exc)],
                    "warnings": [],
                    "next_action": "fix the reported error and re-run nww_phase.py",
                }
            )
        else:
            print(f"ERROR: {exc}")
        return 2
    if args.json:
        _json_line(result)
    else:
        print(_render_text(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
