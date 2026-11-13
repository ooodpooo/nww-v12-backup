#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nww_state_checks.py — 全局状态空判与跨章逐字复读的唯一实现。

* ``is_empty_global``（C-STATE-EMPTY@1）：内容门 ``nww_content_gates`` 与
  ``web/backend/app/guard.py`` 共用；分级阈值（章>3 warning / 章>10 block）
  留在内容门，不进本函数。
* ``cross_chapter_repeats``（C-REPEAT@1）：``guard.py`` 与
  ``nww_track_commit.verify_cross_chapter_repeats`` 共用；切句规则与阈值只在此维护。
"""
from __future__ import annotations

import re
from typing import Any

# 跨章逐字复读的最短句长（与 guard 原内联判定一致）
REPEAT_MIN_LEN = 20
_SENTENCE_SPLIT = re.compile(r"[。！？!?\n]+")


def is_empty_global(state: Any) -> bool:
    """非 dict 或空映射即视为全局状态为空。"""
    return not isinstance(state, dict) or not state


def cross_chapter_repeats(
    chapters: list[tuple[str, str]], min_len: int = REPEAT_MIN_LEN,
) -> dict[str, list[int]]:
    """返回出现在 ≥2 章的整句 → 升序章序号列表。

    ``chapters`` 为 ``[(章标识, 正文)]``，序号取列表下标。切句按
    ``[。！？!?\n]``，每句 strip 后长度 ≥ ``min_len`` 才计入。
    """
    sent_map: dict[str, set[int]] = {}
    for idx, item in enumerate(chapters or []):
        try:
            _name, text = item
        except (TypeError, ValueError):
            continue
        for sentence in _SENTENCE_SPLIT.split(str(text or "")):
            sentence = sentence.strip()
            if len(sentence) >= min_len:
                sent_map.setdefault(sentence, set()).add(idx)
    return {sentence: sorted(idxs) for sentence, idxs in sent_map.items()
            if len(idxs) >= 2}