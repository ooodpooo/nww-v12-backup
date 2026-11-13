#!/usr/bin/env python3
"""千章守护中心 · 「全局状态未填充」受控修复（2026-11-07，界面内一键）。

事实链（改动前逐条核对过源码）：
  - 追踪/全局状态.md 由 nww_track_commit.render_global_view(state) 渲染；
    state["global"] 为空时输出「（尚未写入全局状态；首次 commit 时通过
    global_change 增量填充。）」（nww_track_commit.py:1145-1147）。
  - state["global"] 只由 commit 事务的 global_change 经 deep_merge 写入
    （nww_track_commit.py:2114-2117）；nww_delta_draft.py 机械层默认
    global_change=None，不替模型猜全局状态。
  - web 生产链 2026-10-12 已修好 global_change 回流（pipeline.py:6515
    _global_change_enabled 默认开），但**存量书**在此之前的 commit 没有任何增量，
    global 会永久停在空态；章>10 还会被 nww_content_gates 硬闸 BLOCK。

本脚本只做两件真实、可验证的事，**绝不臆造剧情、不调用模型**：
  ① 补建基线：把**已提交的权威状态**（context.position 的卷/场景/故事时间 +
     last_committed_chapter）投影进 state["global"]，再用生产渲染器重建全部派生视图；
  ② 视图重建：若 state["global"] 已有内容而 全局状态.md 仍是空态/过期，
     只重建派生视图，不改权威状态。
语义字段（发动机类型 / 势力立场 / 下一切换点）属于创作决策，由后续章节 commit
产出的 global_change 增量补齐（deep_merge），本脚本不代写、不留占位假值。

铁律：
  - 全部状态读写/渲染调用 skills/scripts/nww_track_commit.py 的**生产函数本身**，
    不自造状态 schema、不自造视图模板、不自造正则解析资产。
  - --book 由服务端按登录 user_id 解析后传入；stdout 只输出一行 JSON，
    绝不回传绝对路径、用户目录名或部署结构。
  - 默认 --check-only（只读诊断），必须显式 --apply 才写盘；写前唯一化备份，
    写后用生产 check_project 校验，目标校验不过即自动回滚。
  - 全程 project_write_lock，与正式 commit 同一把锁，杜绝并发丢更新。
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
import nww_track_commit as tc  # noqa: E402 — 权威副本（toolbox.resolve_script 首选）

_EMPTY_MARK = "尚未写入"
_CTX_EMPTY_MARK = "（尚未写入全局状态）"


def _emit(payload: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False))
    return code


def _safe_text(text: object, limit: int = 300) -> str:
    """异常文本可能带绝对路径；统一抹掉，绝不回传文件系统结构。"""
    s = str(text or "")
    s = re.sub(r"(?:/Users/|/home/|C:\\|/var/|/private/|projects/[A-Za-z0-9_.-]+/)[^\s\"']*", "（路径略）", s)
    return s[:limit]


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _silent(fn: Callable[..., Any], *args: Any) -> Any:
    """check_project 内部 emit() 直接写 fd 1，会污染 stdout 的 JSON；临时重定向。"""
    sys.stdout.flush()
    saved = os.dup(1)
    null_fd = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null_fd, 1)
        return fn(*args)
    finally:
        sys.stdout.flush()
        os.dup2(saved, 1)
        os.close(saved)
        os.close(null_fd)


def _probe(book: Path) -> tuple[bool, str]:
    try:
        _silent(tc.check_project, book)
        return True, "通过"
    except Exception as exc:  # noqa: BLE001 — 记录原因，不作为修复闸门
        return False, _safe_text(exc, 160)


def _derive_baseline(state: dict[str, Any]) -> dict[str, Any]:
    """只投影已提交权威状态，不读大纲/正文、不推断剧情。"""
    pos = (state.get("context") or {}).get("position") or {}
    chapter = state["last_committed_chapter"]
    volume = pos.get("volume") or ""
    fields: dict[str, Any] = {
        "position": {
            "as_of_chapter": chapter,
            "volume": volume,
            "volume_start_chapter": pos.get("volume_start_chapter"),
            "scene": pos.get("scene") or "",
            "story_time": pos.get("story_time") or "",
            # phase 是已提交的当前卷名，不是编造的「发动机类型」；
            # 内容门只要求 position.engine 非空（nww_content_gates.py:94-100）。
            "engine": {"phase": volume} if volume else {},
        },
        "world": {"phase": volume, "changes": []} if volume else {"changes": []},
        "补建口径": {
            "来源": "追踪/_tracking-state.json 的 context.position / last_committed_chapter（已提交权威状态）",
            "说明": "本次只投影已提交事实，未新增剧情；发动机类型/势力/下一切换点由后续章节 commit 的 global_change 增量补齐",
            "截至章": chapter,
        },
    }
    return tc.normalize_global_state(fields, "guard.global_baseline")


def _unique_backup_dir(tracking: Path) -> Path:
    """唯一化备份目录：秒级时间戳 + 纳秒 + 计数，杜绝两轮快速修复互覆原备份。"""
    base = tracking / "_backup_global_state"
    base.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    for i in range(1000):
        candidate = base / f"{stamp}_{time.time_ns() % 1000000:06d}_{i}"
        if not candidate.exists():
            candidate.mkdir()
            return candidate
    raise RuntimeError("无法创建唯一备份目录")


def _snapshot(book: Path, tracking: Path, views: dict[str, str]) -> tuple[Path, dict[str, Path]]:
    target = _unique_backup_dir(tracking)
    saved: dict[str, Path] = {}
    files: dict[str, Path] = {"_tracking-state.json": tc.state_path(book)}
    for rel in views:
        files[rel] = tracking / rel
    for p in sorted((tracking / "角色状态").glob("*.md")):
        files[f"角色状态/{p.name}"] = p
    for rel, src in files.items():
        if not src.is_file():
            continue
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        saved[rel] = dst
    return target, saved


def _rollback(book: Path, tracking: Path, saved: dict[str, Path], views: dict[str, str]) -> None:
    for rel, src in saved.items():
        dst = tracking / rel if rel != "_tracking-state.json" else tc.state_path(book)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    # 清掉本次新建、但备份里没有的视图文件
    for rel in views:
        p = tracking / rel
        if rel not in saved and p.is_file():
            p.unlink()


def main() -> int:
    ap = argparse.ArgumentParser(description="补建/重建全局状态（受控、可回滚、不臆造剧情）")
    ap.add_argument("--book", type=Path, required=True, help="书稿根（由服务端按登录 user_id 解析）")
    ap.add_argument("--apply", action="store_true", help="真正写盘；缺省为只读诊断")
    ap.add_argument("--check-only", action="store_true", help="显式只读诊断（与缺省行为一致）")
    args = ap.parse_args()

    book = args.book.resolve()
    tracking = tc.tracking_root(book)
    state_path = tc.state_path(book)
    if not book.is_dir() or not state_path.is_file():
        return _emit({"ok": False, "action": "missing_state",
                      "note": "该书尚未建立权威追踪状态，请先完成建书 bootstrap。"}, 2)
    try:
        state0 = tc.load_state(book)
    except Exception as exc:  # noqa: BLE001
        return _emit({"ok": False, "action": "invalid_state",
                      "note": f"权威追踪状态无法读取，未做任何改动：{_safe_text(exc)}"}, 2)

    try:
        views0 = tc.render_views(state0)
    except Exception as exc:  # noqa: BLE001
        return _emit({"ok": False, "action": "render_failed",
                      "note": f"生产渲染器无法渲染该书视图，未做任何改动：{_safe_text(exc)}"}, 2)

    global0 = state0.get("global") or {}
    view_now = _read_text(tracking / "全局状态.md")
    view_drift = any(_read_text(tracking / rel) != payload for rel, payload in views0.items())
    placeholder = _EMPTY_MARK in view_now or not (tracking / "全局状态.md").is_file()

    if global0 and not view_drift and not placeholder:
        mode = "already_ok"
    elif global0:
        mode = "views_rebuilt"
    else:
        mode = "baseline_created"

    if not args.apply:
        return _emit({
            "ok": True, "action": "check_only", "would_be": mode,
            "chapter": state0["last_committed_chapter"],
            "global_empty": not bool(global0), "view_placeholder": placeholder,
            "view_drift": view_drift, "revision": state0["state_revision"],
            "note": "只读诊断，未改动任何文件。",
        })

    if mode == "already_ok":
        return _emit({
            "ok": True, "action": mode, "verified": True, "views_written": 0,
            "chapter": state0["last_committed_chapter"],
            "revision_before": state0["state_revision"], "revision_after": state0["state_revision"],
            "fields": sorted(global0.keys()), "position": {}, "backup_name": "",
            "check_ok": _probe(book)[0],
            "note": "全局状态已填充且派生视图与权威状态一致，无需改动。",
        })

    state1 = copy.deepcopy(state0)
    if mode == "baseline_created":
        state1["global"] = _derive_baseline(state0)
        state1["state_revision"] = state0["state_revision"] + 1
    try:
        views1 = tc.render_views(state1)
    except Exception as exc:  # noqa: BLE001
        return _emit({"ok": False, "action": "render_failed",
                      "note": f"补建后的视图渲染失败，未做任何改动：{_safe_text(exc)}"}, 2)

    changed = [rel for rel, payload in views1.items() if _read_text(tracking / rel) != payload]
    precheck_ok, _ = _probe(book)
    backup_dir: Path | None = None
    saved: dict[str, Path] = {}
    try:
        with tc.project_write_lock(book, timeout_seconds=15):
            backup_dir, saved = _snapshot(book, tracking, views1)
            # 视图先写、权威状态最后写（与正式 commit 同一落盘顺序）
            tc.write_views(tracking, views1)
            if mode == "baseline_created":
                tc.atomic_write_text(state_path, tc.json_payload(state1))

        # 目标校验：只看本次修复目标，避免被书里其他既有问题误判成失败
        state2 = tc.load_state(book)
        verified = (
            bool(state2.get("global"))
            and _read_text(tracking / "全局状态.md") == tc.render_views(state2)["全局状态.md"]
            and _CTX_EMPTY_MARK not in _read_text(tracking / "上下文.md")
            and state2["state_revision"] == state1["state_revision"]
        )
        if not verified:
            _rollback(book, tracking, saved, views1)
            return _emit({"ok": False, "action": "verification_failed", "verified": False,
                          "note": "写入后目标校验未通过，已自动回滚到写前状态；未留下半完成态。"}, 2)
    except Exception as exc:  # noqa: BLE001
        if saved:
            try:
                _rollback(book, tracking, saved, views1)
            except Exception:
                pass
        return _emit({"ok": False, "action": "apply_failed", "verified": False,
                      "note": f"补建中断，已回滚到写前状态：{_safe_text(exc)}"}, 2)

    postcheck_ok, _ = _probe(book)
    gl = state1.get("global") or {}
    pos = (state0.get("context") or {}).get("position") or {}
    note = (
        "已把已提交的当前卷/故事时间/场景等事实投影为全局状态基线，并用生产渲染器重建派生视图；"
        "未新增任何剧情，未改动正文、大纲、细纲与执行回执。"
        "发动机类型/势力/下一切换点属于创作决策，由后续章节 commit 的 global_change 增量补齐。"
        if mode == "baseline_created" else
        "权威状态的 global 段已有内容，只是派生视图仍是空态/过期；已重建派生视图，未改动权威状态。"
    )
    return _emit({
        "ok": True, "action": mode, "verified": True,
        "chapter": state0["last_committed_chapter"],
        "fields": sorted(gl.keys()),
        "position": {k: pos.get(k) for k in ("volume", "scene", "story_time") if pos.get(k)},
        "revision_before": state0["state_revision"], "revision_after": state1["state_revision"],
        "views_written": len(changed),
        "backup_name": backup_dir.name if backup_dir else "",
        "precheck_ok": precheck_ok, "check_ok": postcheck_ok,
        "note": note,
    })


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — 顶层兜底，绝不把 traceback/路径写进 stdout
        sys.exit(_emit({"ok": False, "action": "unexpected",
                        "note": f"补建未执行：{_safe_text(exc)}"}, 2))
