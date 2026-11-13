#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""nww_prose_paths.py — 正文件发现与章节解析的**唯一权威实现**（engine/scripts 侧）

## 为什么要有这个模块

`正文/` 目录下存在 `_review_snapshots/`（审稿 round 快照）。它的文件名与正式正文
**同形**，例如：

    正式稿：正文/第27章_同制不同印.md          （两位零填充，3664 字）
    快照 ：正文/_review_snapshots/第027章_round1.md（三位零填充，3427 字）

2026-10-12 实测确证：engine/scripts 下 4 个脚本各自手写了过滤逻辑，规则**互不等价**
且普遍残缺，导致因果审计、伏笔账本、写前上下文实际读到被退回的 round1 稿。

此前各处的手写规则（互不相同的四套）：
  - `nww_causal_audit.py:93`      只排除 `.` 开头，且**三位 pattern 优先于两位** → 首轮即命中快照
  - `nww_foreshadow_ledger.py:92` 只排除 `.` 开头与 `_原稿_` → 快照按路径字典序**先占位**
  - `nww_book_audit.py:60`        只查 `"_review" in str(p)`
  - `nww_book_audit.py:250`       只查 `_review_snapshots` / `_archive` 两个目录部件
  （`.bak` / `_损坏_` / `_复读_` 在上述实现中全部漏网）

## 统一规则

与下列既有实现保持**语义一致**（已有单测覆盖，勿再各写一套）：
  - `web/backend/app/books.py::_is_non_prose`
  - `skills/nww-novel-skill/scripts/nww_track_commit.py::_is_non_prose`

排除项：
  目录部件：  `_review_snapshots` / `_archive`
  文件名特征：`.` 开头 / `_原稿_` / `.bak` / `_损坏_` / `_复读_`

## 设计原则

1. **章号锚定解析**：用 `^第0*(\d+)章` 匹配文件名开头，一次性处理 1/2/3 位零填充。
   不使用多个 pattern 的 `re.search` 逐个试（非锚定 + 顺序敏感）。
2. **不靠字典序碰运气**：文件选择规则是显式的（见 `resolve_chapter_file`），
   不依赖 `_`(U+005F) 与 `第`(U+7B2C) 的码位大小关系。
3. **零 LLM、只读、确定性**：任一步失败返回空值，绝不抛。

用法：
    from nww_prose_paths import iter_prose_files, resolve_chapter_file, load_chapter_texts
"""
from __future__ import annotations

import re
from pathlib import Path

# 非正文目录（按路径部件匹配）—— 三处实现一致
NON_PROSE_DIRS = ("_review_snapshots", "_archive")

# 非正文文件名特征 —— ★ 取两处既有实现的**并集**
#
# 2026-10-12 实测发现：两处权威实现的关键词表**已经漂移**，互不包含：
#   web/backend/app/books.py            : _原稿_ / .bak / _损坏_ / _复读_
#   skills/.../nww_track_commit.py      : _原稿_ / _设定 / _笔记 / _大纲 / _细纲
#                                         / _草稿 / _废弃 / _储备
# 此处取并集（宁可漏读、不可读错）。已扫描 projects/admin/*/正文 全部 md，
# 这些关键词**零命中**正式正文文件名，因此不存在误杀风险。
NON_PROSE_NAME_MARKS = (
    "_原稿_", ".bak", "_损坏_", "_复读_",
    "_设定", "_笔记", "_大纲", "_细纲", "_草稿", "_废弃", "_储备",
)

# 章号：锚定文件名开头，兼容 第1章 / 第01章 / 第001章 / ch0001
CHAPTER_RE = re.compile(r"^(?:第0*(\d+)章|ch0*(\d+))")

__all__ = [
    "NON_PROSE_DIRS",
    "NON_PROSE_NAME_MARKS",
    "is_hidden",
    "is_non_prose",
    "chapter_no",
    "iter_prose_files",
    "resolve_chapter_file",
    "load_chapter_texts",
]


def is_hidden(p: Path) -> bool:
    """隐藏文件（`.` 开头）。

    ★ 单独成函数的原因：两处既有的 `_is_non_prose` **都不处理**隐藏文件，
      由调用方另行过滤（如 `books.build_tree` 的「`.` 开头 + `_is_non_prose`」双重过滤）。
      为保持 `is_non_prose` 与既有实现**语义完全一致**（便于一致性测试），
      隐藏文件判断放在枚举层 `iter_prose_files` / `resolve_chapter_file` 中。
    """
    return p.name.startswith(".")


def is_non_prose(p: Path) -> bool:
    """路径是否属于「非正文」（快照 / 归档 / 原稿 / 备份 / 损坏 / 复读 / 设定笔记等）。

    与 `web/backend/app/books.py::_is_non_prose` 及
    `skills/nww-novel-skill/scripts/nww_track_commit.py::_is_non_prose`
    **语义对齐**（关键词取并集，见 NON_PROSE_NAME_MARKS 注释）。
    """
    name = p.name
    if any(d in p.parts for d in NON_PROSE_DIRS):
        return True
    return any(m in name for m in NON_PROSE_NAME_MARKS)


def chapter_no(p: Path | str) -> int | None:
    """从文件名解析章号：`第1章` / `第01章` / `第001章` / `ch0001` 均返回 1；无法解析返回 None。

    ★ 锚定在文件名开头，因此 `第1章_甲.md` 不会被 `第10章_癸.md` 之类的子串误伤，
      也不依赖「数字后面紧跟『章』」这种巧合。
    """
    name = p.name if isinstance(p, Path) else p
    m = CHAPTER_RE.match(name)
    if not m:
        return None
    return int(m.group(1) or m.group(2))


def iter_prose_files(body: Path, pattern: str = "*.md"):
    """枚举正文目录下的正式章节文件（已排除非正文），按路径升序 yield。

    body 不存在或不是目录 → 不产出任何东西（不抛）。
    """
    if not body.is_dir():
        return
    for p in sorted(body.rglob(pattern)):
        if is_hidden(p) or is_non_prose(p):
            continue
        yield p


def resolve_chapter_file(body: Path, chapter: int, pattern: str = "*.md") -> Path | None:
    r"""章号 → 唯一的正式正文文件。

    与旧实现的关键差异：
      - 章号用**锚定** `^第0*(\d+)章` 解析，不做 `re.search("第1章", name)` 子串试错；
      - 先按 `is_non_prose` 过滤，再选文件，不依赖路径字典序；
      - 多候选时按 **mtime 取最新**（与 nww_track_commit.resolve_chapter_file 同口径），
        而非字典序取最后一个。

    找不到 → 返回 None（调用方自行决定是否降级）。
    """
    if not body.is_dir():
        return None
    cands = [p for p in body.rglob(pattern)
             if not is_hidden(p) and not is_non_prose(p) and chapter_no(p) == chapter]
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    try:
        cands.sort(key=lambda p: p.stat().st_mtime_ns)
    except OSError:
        cands.sort(key=lambda p: p.as_posix())
    return cands[-1]


def load_chapter_texts(body: Path, drop_headings: bool = True) -> dict[int, str]:
    """章号 → 正文文本。用于伏笔/关系等需要全文扫描的场景。

    ★ 修复要点（对应旧 `nww_foreshadow_ledger.chapter_texts` 的缺陷）：
      旧实现用 `if n not in out` 让**先遇到者占位**。由于 `_`(U+005F) < `第`(U+7B2C)，
      快照路径排序在前，于是快照覆盖正式稿。此处改为：
        1. 先按章号分组收集**全部**候选；
        2. 同章多候选时按 `resolve_chapter_file` 的同一规则（mtime 最新）挑选；
      —— 显式挑选，不靠排序碰运气。

    drop_headings：是否剔除 `#` 开头行（保留旧行为，默认 True）。
      ⚠️ 外脑 2026-10-12 提示：无差别剔除标题行可能丢失「标题中含事件信息」的证据。
      目前**没有实测证据**证明已造成漏判，故默认保持旧行为以避免结果漂移；
      置 False 可保留完整原文，供后续对照实验使用。
    """
    if not body.is_dir():
        return {}
    grouped: dict[int, list[Path]] = {}
    for p in iter_prose_files(body, "*.md"):
        n = chapter_no(p)
        if n is None:
            continue
        grouped.setdefault(n, []).append(p)

    out: dict[int, str] = {}
    for n, paths in grouped.items():
        chosen = paths[0]
        if len(paths) > 1:
            try:
                chosen = sorted(paths, key=lambda q: q.stat().st_mtime_ns)[-1]
            except OSError:
                chosen = sorted(paths, key=lambda q: q.as_posix())[-1]
        try:
            raw = chosen.read_text(encoding="utf-8", errors="ignore")
        except (OSError, UnicodeError):
            continue
        if drop_headings:
            raw = "\n".join(l for l in raw.splitlines() if not l.strip().startswith("#"))
        out[n] = raw.strip()
    return dict(sorted(out.items()))
