#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nww_track_migrate.py — 追踪状态 schema 版本迁移（2026-11-01）。

背景（真机冒烟实测）：
    老项目《用泡面征服修仙界》的 追踪/_tracking-state.json 仍是 schema_version=4，
    写章链跑到 `nww_track_commit.normalize_state` 直接失败：
    `tracking state schema is unsupported` → delta_draft 阶段 BLOCK，整条写链断在起点。
    schema 5 在 4 的基础上新增 6 个结构键（dormant_characters / story_events / facts /
    cost / global / chapter_types）；`normalize_state` 对缺键有 `root.get(..., {})` 兜底，
    但**版本号本身是硬校验**，所以老数据必须显式升版。

设计原则（保守、可回滚、零猜写）：
    1. 只做**结构补齐**：缺的键补空结构（{}），已有键一律原样保留，不重排、不改值；
    2. 只改 `schema_version`：4 → 5；其他未知版本一律拒做（不猜语义）；
    3. 写前必须通过官方 `nww_track_commit.normalize_state()` 全量校验 —— 校验不过就
       不写盘（宁可报错，不落半成品）；
    4. 写入前先备份原文 `追踪/_backup_tracking-state.v<old>.<时间戳>.json`（字节级复制）；
    5. 原子替换（复用 tc.atomic_write_text：临时文件 + fsync + os.replace）；
    6. 幂等：schema 已是 5 → no-op（exit 0），重复执行不产生任何变化；
    7. `--dry-run` 只校验 + 打印将新增的键，绝不写盘（含不写备份）。

用法：
    python3 nww_track_migrate.py --project <book_root> [--dry-run] [--json]

退出码：
    0 = 已是最新 / 迁移成功（含 dry-run 校验通过）
    1 = 项目缺失、状态文件缺失、版本不支持、或迁移结果过不了 normalize_state
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nww_track_commit as tc  # noqa: E402 — 复用官方校验/原子写，零复制

TARGET_VERSION = tc.TRACKING_SCHEMA_VERSION
# schema 5 新增的结构键。空结构形状取自 normalize_state 的默认值（root.get(key, {})），
# 不发明任何业务内容。
NEW_STRUCTURE_KEYS = (
    "dormant_characters", "story_events", "facts", "cost", "global", "chapter_types",
)


def _emit(text: str, *, error: bool = False) -> None:
    stream = sys.stderr if error else sys.stdout
    stream.flush()
    stream.buffer.write((text + "\n").encode("utf-8"))
    stream.buffer.flush()


def migrate_document(document: object) -> tuple[dict, list[str], str]:
    """纯函数：返回 (迁移后的文档, 新增键列表, 说明)。不写盘。

    版本不支持 / 结构非法 → 抛 tc.TrackingError（由 CLI 转 exit 1）。
    """
    if not isinstance(document, dict):
        raise tc.TrackingError("tracking state must be a JSON object")
    version = document.get("schema_version")
    if version == TARGET_VERSION:
        return dict(document), [], f"already v{TARGET_VERSION}（无需迁移）"
    if version != TARGET_VERSION - 1:
        raise tc.TrackingError(
            f"unsupported schema_version={version!r}（本脚本只做 v{TARGET_VERSION - 1} → "
            f"v{TARGET_VERSION}，未知版本拒绝迁移，请先人工确认结构差异）")
    migrated = dict(document)
    added: list[str] = []
    for key in NEW_STRUCTURE_KEYS:
        if key not in migrated:
            migrated[key] = {}
            added.append(key)
    migrated["schema_version"] = TARGET_VERSION
    # 官方全量校验：不过就不允许落盘（含 active 角色无快照 / recent_chapters 顺序等不变式）
    normalized = tc.normalize_state(migrated)
    return normalized, added, f"v{version} → v{TARGET_VERSION}"


def _backup_path(tracking: Path, old_version: object) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    return tracking / f"_backup_tracking-state.v{old_version}.{stamp}.json"


def run(project: Path, *, dry_run: bool = False, as_json: bool = False) -> int:
    state_file = project / "追踪" / "_tracking-state.json"
    if not project.is_dir():
        _emit(f"ERROR: 项目目录不存在：{project}", error=True)
        return 1
    if not state_file.is_file():
        _emit(f"ERROR: 追踪状态缺失：{state_file}（新项目请先 init，无需迁移）", error=True)
        return 1
    try:
        raw_text = state_file.read_text(encoding="utf-8")
        raw = json.loads(raw_text)
    except (OSError, json.JSONDecodeError) as exc:
        _emit(f"ERROR: 状态文件读取/解析失败：{exc}", error=True)
        return 1
    old_version = raw.get("schema_version") if isinstance(raw, dict) else None
    try:
        migrated, added, note = migrate_document(raw)
    except tc.TrackingError as exc:
        _emit(f"ERROR: 迁移被拒：{exc}", error=True)
        return 1

    result = {
        "project": str(project), "file": str(state_file),
        "from_version": old_version, "to_version": TARGET_VERSION,
        "added_keys": added, "note": note, "dry_run": dry_run, "written": False,
    }
    if not added:
        if as_json:
            _emit(json.dumps(result, ensure_ascii=False))
        else:
            _emit(f"OK: {note}；未写入任何文件（{state_file}）")
        return 0
    if dry_run:
        if as_json:
            _emit(json.dumps(result, ensure_ascii=False))
        else:
            _emit(f"DRY-RUN: {note}；将新增键：{', '.join(added)}；"
                  f"normalize_state 校验通过；未写盘（{state_file}）")
        return 0
    backup = _backup_path(state_file.parent, old_version)
    try:
        shutil.copyfile(state_file, backup)
        tc.atomic_write_text(state_file, tc.json_payload(migrated))
    except (OSError, tc.TrackingError) as exc:
        _emit(f"ERROR: 写入失败（原文件未替换）：{exc}", error=True)
        return 1
    result["written"] = True
    result["backup"] = str(backup)
    if as_json:
        _emit(json.dumps(result, ensure_ascii=False))
    else:
        _emit(f"OK: {note}；新增键：{', '.join(added)}；已备份 {backup.name}；已原子替换 {state_file}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="追踪状态 schema 迁移（v4 → v5，幂等）")
    parser.add_argument("--project", required=True, help="书项目根目录")
    parser.add_argument("--dry-run", action="store_true", help="只校验并打印将新增的键，不写盘")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = parser.parse_args(argv)
    return run(Path(args.project).expanduser().resolve(), dry_run=args.dry_run, as_json=args.json)


if __name__ == "__main__":
    raise SystemExit(main())
