#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nww_text.py — 正文段落协议的单一权威实现（V12.5，2026-09-13）。

## 为什么需要这个模块

本项目历史上「段落怎么切」有多套口径，散在各个脚本里各写一遍，于是出现
「同一份正文，不同脚本给出不同段数」的分裂：

* **长篇协议（唯一合法）**：段落之间空行（``\\n\\n``），一段占一行。
  ``web/backend/app/agents_runtime/writer_prompt.py`` 与
  ``agents/narrative-writer.md`` 都明确要求这一种。
* **单换行协议（历史遗留 / 主笔偶发不遵守）**：段落之间只有一个 ``\\n``。
  实测 2026-09-12 之后 Web 链路产出的第 025-028 章整章都是这一种。

按空行硬切（``text.split("\\n\\n")``）在单换行协议下会把**整章并成 1-2 个巨块**，
制造四类事故：

1. 块长上千字 → 误报「单段超长」→ **假 BLOCK**（实测 2584 / 3474 字）；
2. 块数 <3 → 落进「段落<3，不判」→ 门禁**静默空转**，等于没跑；
3. ``nww_enforce`` 结尾泄漏清理取 ``paras[-1]`` = 整章，命中即**整章被删**；
4. 必种硬锚注入把句子插到「整章」的头部/尾部，位置全错。

本模块把切分口径收敛成一处，供所有需要「段落」语义的脚本共用。

## 对外接口

- ``split_paragraphs(text) -> (paras, mode)`` —— 容错切分，自动识别两种协议；
- ``normalize_paragraphs(text) -> str``       —— 归一为长篇协议（纯空白变换）；
- ``detect_mode(text) -> str``                —— 只取模式。

## 铁律

**只动空白，绝不增删或改写任何非空白字符。** 因此
``re.sub(r"\\s", "", text)`` 在归一前后必须逐字节一致 —— 这是可以安全地对
存量正文做「格式修复」而不触碰内容的前提。
"""

from __future__ import annotations

import re

__all__ = ["split_paragraphs", "normalize_paragraphs", "detect_mode"]

# 空行分段：一个或多个空行（允许行内含空白）即段界
_BLANK_SPLIT_RE = re.compile(r"\n\s*\n")

# 连续 3 个及以上换行 → 压缩为 1 个空行
_MULTI_BLANK_RE = re.compile(r"\n{3,}")

# 判据阈值：逐行段数 / 空行块数 超过该比值 → 判定为「单换行分段」。
#
# 取值依据（2026-09-13 全仓 263 个正文文件实测）：
#   真实空行协议文件的 lines/blocks **最大仅 1.10** —— NWW 正文不做硬折行，
#   一行即一段，所以两种数法几乎相等；
#   单换行协议文件普遍 ≥ 4（因为空行块数被压到 1-3，而逐行数就是段数）。
# 取 4 时对空行协议仍有 3.6 倍余量，不会误触发。
_SINGLE_NEWLINE_RATIO = 4

# 少于 3 行不判（与 detect_internal.py 的「段落<3，不判」口径一致）
_MIN_LINES = 3


def _nonempty_lines(text: str) -> list[str]:
    """逐行切分：去空行，每行 strip。"""
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def split_paragraphs(text: str) -> tuple[list[str], str]:
    """容错段落切分 → ``(段列表, 模式)``。

    模式取值：

    * ``"blank"`` —— 正文已是长篇协议（段间空行），按空行切分；
    * ``"line"``  —— 正文是单换行分段，按行切分（调用方应提示正文格式待归一）。

    两种模式对**同一份正文**必须给出逐段等价的结果：把单换行正文交给
    :func:`normalize_paragraphs` 归一后再切分，得到的段列表必须与 ``"line"``
    模式的返回值完全相同。（已用全仓 16 个单换行文件断言，0 失败。）

    因此这里**不排除标题行** —— 空行模式本来就把 ``# 第N章…`` 算作第 1 段
    （与 ``quality/detect_internal.py`` 的 ``paras`` 口径一致），逐行模式也必须
    算进去，否则「文件格式」会改变判定结果。标题行很短（<20 字），不会被
    单段超长 / 信息层堆叠命中；在长段连片判定里只占据一个「短段」位置，
    与既有的 247 个空行协议章节行为一致。
    """
    blocks = [b.strip() for b in _BLANK_SPLIT_RE.split(text) if b.strip()]
    lines = _nonempty_lines(text)
    if len(lines) >= _MIN_LINES and (
        len(blocks) < _MIN_LINES
        or len(lines) > _SINGLE_NEWLINE_RATIO * len(blocks)
    ):
        return lines, "line"
    return blocks, "blank"


def detect_mode(text: str) -> str:
    """只返回分段模式（``"blank"`` / ``"line"``）。"""
    return split_paragraphs(text)[1]


def normalize_paragraphs(text: str) -> str:
    """归一为长篇协议：段间空行，段内单行。**纯空白变换**。

    步骤：去空行 → 逐行 strip → 用 ``\\n\\n`` 重拼 → 压缩 3 个以上连续换行
    → 去掉首尾空白。归一后 ``re.sub(r"\\s", "", ...)`` 与输入逐字节一致。

    注意：本函数假定「一行 = 一段」。NWW 正文不做硬折行（已实测确认），
    故该假定成立；若未来引入软折行，必须先改这里。
    """
    lines = _nonempty_lines(text)
    return _MULTI_BLANK_RE.sub("\n\n", "\n\n".join(lines)).strip()
