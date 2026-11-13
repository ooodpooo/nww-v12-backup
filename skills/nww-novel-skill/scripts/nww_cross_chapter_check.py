#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nww_cross_chapter_check.py — 跨章质量检测（advisory，只报告不修改）。

对照 inkos 实证方案移植（post-write-validator / chapter-cadence，2026-09-05）：

1. 跨章重复：当前章 6 字中文 ngram（出现 ≥2 次）在近 N 章正文也出现，
   命中 ≥3 个不同 ngram → WARNING。跨章机械复读是连载长跑最先出现的退化指纹。
2. 段落密度漂移：当前章平均段长较近 N 章显著缩短（<55%）且短段占比抬升
   （Δ≥0.25）→ WARNING。针对「每拍一切」电报体回潮。
3. 标题疲劳：近 N 章标题反复撞同一 token（≥3 章）→ WARNING。命名公式化是
   章节类型单调的可见信号（inkos long-span-fatigue 同思路的最小实现）。

用法：
    python3 nww_cross_chapter_check.py --book {BOOK} --chapter {N} [--window 5] [--json]

定位：advisory 层，只报告、永不改稿、不阻断提交。正文定位复用
nww_track_commit.resolve_chapter_file 的契约（排除 _原稿_ 备份）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

_WINDOW_DEFAULT = 5
_NGRAM_SIZE = 6
_NGRAM_MIN_COUNT = 2
_NGRAM_ALERT_MIN = 3
_SHRINK_RATIO_FLOOR = 0.55
_SHORT_RATIO_DELTA = 0.25
_MIN_PARAGRAPHS = 4
_TOKEN_MIN_LEN = 2
_TOKEN_ALERT_CHAPTERS = 3

_CJK_RE = re.compile(r"^[\u4e00-\u9fff]+$")
_TITLE_TOKEN_SPLIT_RE = re.compile(r"[\s_·：:，,。！!？?（）()\[\]【】\-—/]+")

sys.path.insert(0, str(Path(__file__).resolve().parent))


def chapter_files(book: Path) -> list[tuple[int, Path]]:
    """全部正文文件按章号排序（排除 _原稿_ 备份与非正文件，契约同 A-1）。"""
    body = book / "正文"
    if not body.is_dir():
        return []
    non_prose = ("_设定", "_笔记", "_大纲", "_细纲", "_草稿", "_废弃", "_储备")
    found: list[tuple[int, Path]] = []
    for p in body.rglob("第*章*.md"):
        # 快照（_review_snapshots/第027章_round1.md）与正文同形，会被当成一章
        # 参与跨章重复比对 → 与正文自身高度重复，产生误报。
        if "_review_snapshots" in p.parts:
            continue
        if "_原稿_" in p.name or any(kw in p.name for kw in non_prose):
            continue
        m = re.match(r"第0*(\d+)章", p.name)
        if m:
            found.append((int(m.group(1)), p))
    found.sort(key=lambda item: item[0])
    return found


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""


def cross_chapter_repeats(current: str, recent: str) -> list[str]:
    """当前章重复 ≥2 次的 6 字 ngram 中，也出现在近章正文的条目。"""
    chars = re.sub(r"\s", "", current)
    counts: dict[str, int] = {}
    for i in range(len(chars) - _NGRAM_SIZE + 1):
        gram = chars[i:i + _NGRAM_SIZE]
        if _CJK_RE.match(gram):
            counts[gram] = counts.get(gram, 0) + 1
    recent_clean = re.sub(r"\s", "", recent)
    hits = [g for g, c in counts.items() if c >= _NGRAM_MIN_COUNT and g in recent_clean]
    # 错位重叠的 ngram（井绳磨得手心/绳磨得手心发/磨得手心发红）是同一短语，
    # 按 3 字子串重叠归并成短语族，避免 1 个复读报成 3 个。
    families: list[str] = []

    def _overlaps(a: str, b: str) -> bool:
        return any(a[i:i + 3] in b for i in range(len(a) - 2))

    for gram in hits:
        if not any(_overlaps(gram, fam) for fam in families):
            families.append(gram)
    return families


def paragraph_shape(text: str) -> tuple[list[int], float]:
    """段落长度列表 + 短段（<40 可见字符）占比。"""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    lengths = [len(re.sub(r"\s", "", p)) for p in paragraphs]
    short_ratio = (sum(1 for l in lengths if l < 40) / len(lengths)) if lengths else 0.0
    return lengths, short_ratio


def paragraph_drift(current: str, recent: str) -> dict[str, float] | None:
    cur_lengths, cur_short = paragraph_shape(current)
    rec_lengths, rec_short = paragraph_shape(recent)
    if len(cur_lengths) < _MIN_PARAGRAPHS or len(rec_lengths) < _MIN_PARAGRAPHS:
        return None
    cur_avg = sum(cur_lengths) / len(cur_lengths)
    rec_avg = sum(rec_lengths) / len(rec_lengths)
    if rec_avg <= 0 or cur_avg <= 0:
        return None
    ratio = cur_avg / rec_avg
    if ratio >= _SHRINK_RATIO_FLOOR:
        return None
    if cur_short - rec_short < _SHORT_RATIO_DELTA:
        return None
    return {
        "recent_avg": round(rec_avg, 1),
        "current_avg": round(cur_avg, 1),
        "shrink_ratio": round(ratio, 2),
        "short_ratio_delta": round(cur_short - rec_short, 2),
    }


def title_fatigue(titles: list[str]) -> list[str]:
    """近章标题中出现在 ≥3 章的重复 token。"""
    token_map: dict[str, list[int]] = {}
    for idx, title in enumerate(titles):
        tokens = {
            t for t in _TITLE_TOKEN_SPLIT_RE.split(title)
            if len(t) >= _TOKEN_MIN_LEN and not t.isdigit()
        }
        for token in tokens:
            token_map.setdefault(token, []).append(idx)
    return [
        token for token, idxs in sorted(token_map.items())
        if len(set(idxs)) >= _TOKEN_ALERT_CHAPTERS
    ]


def run_check(book: Path, chapter: int, window: int = _WINDOW_DEFAULT) -> dict[str, Any]:
    files = chapter_files(book)
    target = next((p for num, p in files if num == chapter), None)
    findings: list[dict[str, str]] = []
    if target is None:
        return {"findings": [], "note": f"chapter {chapter} prose not found; skipped"}
    current = read_text(target)
    recent_files = [p for num, p in files if num < chapter][-window:]
    recent_texts = [read_text(p) for p in recent_files]
    recent_body = "\n\n".join(recent_texts)
    recent_names = [p.stem for p in recent_files]

    repeats = cross_chapter_repeats(current, recent_body) if recent_body.strip() else []
    if len(repeats) >= _NGRAM_ALERT_MIN:
        findings.append({
            "severity": "warning",
            "rule": "跨章重复",
            "description": f"{len(repeats)} 个 6 字短语在近 {len(recent_files)} 章中也出现过"
                           f"（示例：{'、'.join(repeats[:5])}）。变换动作与场景用语，避免跨章机械复读。",
        })

    drift = paragraph_drift(current, recent_body) if recent_body.strip() else None
    if drift:
        drop = round((1 - drift["shrink_ratio"]) * 100)
        findings.append({
            "severity": "warning",
            "rule": "段落密度漂移",
            "description": f"平均段长从近章 {drift['recent_avg']} 字降到 {drift['current_avg']} 字"
                           f"（-{drop}%），短段占比抬升 {drift['short_ratio_delta']}。"
                           "不要把每个动作切成单句电报体；把动作、观察、反应并回段落层次。",
        })

    fatigue = title_fatigue(recent_names)
    if fatigue:
        findings.append({
            "severity": "warning",
            "rule": "标题疲劳",
            "description": f"近 {len(recent_files)} 章标题反复撞词：{'、'.join(fatigue[:5])}。"
                           "章节类型可能开始固化，考虑换叙事单元类型（日常/冲突/揭示轮换）。",
        })
    return {"findings": findings}


def main() -> int:
    ap = argparse.ArgumentParser(description="跨章质量检测（advisory，只报告）")
    ap.add_argument("--book", required=True, help="书项目根（含 正文/）")
    ap.add_argument("--chapter", type=int, required=True, help="当前章号")
    ap.add_argument("--window", type=int, default=_WINDOW_DEFAULT, help="回看章数（默认 5）")
    ap.add_argument("--json", action="store_true", help="输出单行 JSON")
    args = ap.parse_args()
    result = run_check(Path(args.book), args.chapter, args.window)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        if not result["findings"]:
            print("cross-chapter check: clean")
        for f in result["findings"]:
            print(f"[{f['severity'].upper()}] {f['rule']}：{f['description']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
