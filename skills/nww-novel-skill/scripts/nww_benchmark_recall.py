#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nww_benchmark_recall.py — 对标锚召回（Benchmark Anchor Recall，可选旁路，默认关闭）

职责：
  仅当 {BOOK}/.benchmark.enabled 存在时，从 拆文库/{对标名}/ 召回题材卡 + 情绪模块 + 节奏 + 文风，
  输出一段 advisory 级「【对标锚】」文本供 nww-long-write Phase 1 并入写作 prompt。
  无标记文件 / 数据缺失 → 输出空（不注入、不报错、不影响现有流程）。

原则：
  - 零耦合：只读拆文库与题材卡，绝不写书项目任何文件。
  - advisory：对标锚低于硬事实锚/voice 锚/代价刻度；写作侧冲突时硬锚赢。
  - fail-open：任何数据源缺失只跳过该段，不阻断。

用法：
  python3 nww_benchmark_recall.py --book {BOOK} [--name 对标名] [--genre 题材] [--chapter N] --out txt|json

--name 缺省取 {BOOK} 目录名；--genre 缺省读 {BOOK}/bible.json 的 genre 字段（斜杠拆标签做题材卡匹配）。
"""
import argparse
import json
import os
import re
import sys

# 题材标签 → 题材卡关键词 同义映射（best-effort，仅作匹配辅助；匹配不到就跳过题材卡段）
GENRE_SYNONYMS = {
    "修仙": ["东方仙侠", "都市修真"],
    "仙侠": ["东方仙侠", "都市修真"],
    "都市": ["都市脑洞", "都市日常", "都市种田", "都市高武", "都市修真"],
    "种田": ["都市种田", "女频种田"],
    "美食": ["都市日常", "都市种田"],
    "悬疑": ["悬疑灵异", "悬疑脑洞"],
    "灵异": ["悬疑灵异", "悬疑脑洞"],
    "玄幻": ["传统玄幻", "玄幻脑洞", "玄幻言情"],
    "科幻": ["科幻末世"],
    "末世": ["科幻末世"],
    "历史": ["历史古代", "历史脑洞"],
    "年代": ["年代"],
    "脑洞": ["都市脑洞", "玄幻脑洞", "悬疑脑洞", "历史脑洞"],
    "娱乐": ["星光璀璨"],
    "权谋": ["宫斗宅斗"],
    "宫斗": ["宫斗宅斗"],
    "宅斗": ["宫斗宅斗"],
    "战": ["战神赘婿"],
    "赘婿": ["战神赘婿"],
    "甜宠": ["青春甜宠"],
    "电竞": ["游戏体育"],
    "体育": ["游戏体育"],
    "破解": ["都市脑洞", "悬疑脑洞"],
    "系统": ["都市脑洞", "玄幻脑洞"],
}
DEFAULT_CARD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "references", "agent-references", "genre-prose-cards")

SEC_CHARS = 2500          # 情绪/节奏/文风各段字符上限
CARD_CHARS = 3000         # 题材卡字符上限


def find_cards_dir():
    """题材卡目录：脚本相对路径优先，其次环境变量覆盖。"""
    env = os.environ.get("NWW_GENRE_CARDS_DIR")
    if env and os.path.isdir(env):
        return env
    cand = os.path.realpath(DEFAULT_CARD_DIR)
    return cand if os.path.isdir(cand) else None


def read_bible_genre(book):
    try:
        with open(os.path.join(book, "bible.json"), encoding="utf-8") as f:
            genre = json.load(f).get("genre", "")
        return [g.strip() for g in re.split(r"[/、,，|]", genre) if g.strip()]
    except Exception:
        return []


def match_card(genre_tags, cards_dir):
    if not cards_dir or not genre_tags:
        return None
    names = [f[:-3] for f in os.listdir(cards_dir) if f.endswith(".md")]
    for tag in genre_tags:
        if tag in names:
            return tag
    scored = {}
    all_syn = []
    for tag in genre_tags:
        all_syn.append(tag)
        all_syn.extend(GENRE_SYNONYMS.get(tag, []))
    for tag in all_syn:
        for n in names:
            if tag and (tag in n or n in tag):
                scored[n] = scored.get(n, 0) + 1
    if not scored:
        return None
    return max(scored, key=scored.get)


def read_limited(path, limit):
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            data = f.read()
    except Exception:
        return ""
    if len(data) <= limit:
        return data
    return data[:limit] + "\n…（锚段截断，完整内容见拆文库源文件）"


def build_anchor(book, name, genre_tags, chapter=None):
    lib = _find_library(book, name)
    sections = {}

    # 1. 题材卡（best-effort）
    cards_dir = find_cards_dir()
    card = match_card(genre_tags, cards_dir)
    if card and cards_dir:
        content = read_limited(os.path.join(cards_dir, card + ".md"), CARD_CHARS)
        if content:
            sections["card"] = {"genre_card": card, "content": content}

    # 2-4. 拆文库产物（逐项可选）
    paths = {
        "emotion": os.path.join(lib, "剧情", "情绪模块.md"),
        "rhythm": os.path.join(lib, "剧情", "节奏.md"),
        "style": os.path.join(lib, "文风.md"),
    }
    for key in ("emotion", "rhythm", "style"):
        content = read_limited(paths[key], SEC_CHARS)
        if content:
            sections[key] = content

    return sections, lib


def _find_library(book, name):
    """拆文库定位：自 book 向上最多 3 层，取首个含 `拆文库/` 的目录（NWW-V12 仓库根）。"""
    base = os.path.dirname(os.path.abspath(book))
    for _ in range(3):
        lib = os.path.join(base, "拆文库", name)
        if os.path.isdir(lib):
            return lib
        parent = os.path.dirname(base)
        if parent == base:
            break
        base = parent
    return os.path.join(os.path.dirname(os.path.abspath(book)), "拆文库", name)


def anchor_text(sections):
    if not sections:
        return ""
    lines = ["【对标锚】（advisory 级，冲突时硬事实锚/voice 锚/代价刻度优先）"]
    card = sections.get("card")
    if card:
        lines.append("\n—— 题材卡锚 ——")
        lines.append(f"题材卡：{card['genre_card']}")
        lines.append(card["content"])
    emo = sections.get("emotion")
    if emo:
        lines.append("\n—— 情绪模块锚（读者需求/情绪引擎/可复现模块，权威在拆文库）——")
        lines.append(emo)
    rhy = sections.get("rhythm")
    if rhy:
        lines.append("\n—— 节奏锚（关键信息推进/爽点循环/情绪触动点）——")
        lines.append(rhy)
    sty = sections.get("style")
    if sty:
        lines.append("\n—— 文风锚（表达层参考，不复制专名/台词/独特桥段）——")
        lines.append(sty)
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="对标锚召回（可选旁路）")
    ap.add_argument("--book", required=True, help="书项目根（含 bible.json / 追踪）")
    ap.add_argument("--name", default=None, help="对标书名；缺省取 book 目录名")
    ap.add_argument("--genre", default=None, help="题材标签，斜杠分隔；缺省读 bible.json genre")
    ap.add_argument("--chapter", type=int, default=None, help="章号（预留，当前仅随 json 输出）")
    ap.add_argument("--out", choices=["txt", "json"], default="txt")
    args = ap.parse_args()

    book = os.path.abspath(args.book)
    enabled = os.path.isfile(os.path.join(book, ".benchmark.enabled"))
    if not enabled:
        out = {"enabled": False, "reason": ".benchmark.enabled 不存在", "anchor_text": ""}
        print(json.dumps(out, ensure_ascii=False) if args.out == "json"
              else "## 对标锚：未启用（缺少 .benchmark.enabled 标记文件，跳过注入）")
        return 0

    name = args.name or os.path.basename(book.rstrip("/")) or "book"
    genre_tags = []
    if args.genre:
        genre_tags = [g.strip() for g in re.split(r"[/、,，|]", args.genre) if g.strip()]
    if not genre_tags:
        genre_tags = read_bible_genre(book)

    sections, lib = build_anchor(book, name, genre_tags, args.chapter)
    txt = anchor_text(sections)
    out = {
        "enabled": True,
        "name": name,
        "library_dir": lib,
        "chapter": args.chapter,
        "sections": sorted(sections.keys()),
        "benchmark_count": len(sections),
        "anchor_text": txt,
    }
    if args.out == "json":
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(txt if txt else f"## 对标锚：{lib} 无可用拆文产物，跳过注入")


if __name__ == "__main__":
    sys.exit(main())