#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nww_memory_pack.py — 分层记忆选材包生成（只读旁路，零耦合）。

借鉴 webnovel-writer memory/orchestrator 的分层选材思想，适配 V12 数据模型：
读 `_tracking-state.json` + 本章细纲，按「优先级 + 相关性 + 预算」组装三层记忆包：
- working   : 当前章工作区（位置 / 下一章承诺 / 细纲核心事件）
- episodic  : 逐章事件记忆（近 N 章摘要 + 时间线事件）
- semantic  : 跨章语义事实（长期约束 / 角色状态 / 活跃伏笔 / 连贯性风险）

设计原则：
- 只读：不写任何文件，不 import server/，不依赖网络。
- 零耦合：独立新增脚本，不改 `_tracking-state.json`、不改 context_load 默认路径、
  不作任何现有 skill 的依赖。默认仅作增强诊断输出。
- 相关性：从细纲提取角色名/关键词，对 semantic 项匹配打分；无细纲则按优先级+更新时间排序。
- 预算：按 task_type 分配各层上限，防止上下文膨胀。

用法：
    python3 nww_memory_pack.py --project {BOOK} --chapter {N} [--task write] [--max-items M] [--json]
输出：
    --json 输出单行 JSON 记忆包；否则输出人类可读摘要。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

SCHEMA = "memory-pack/v1"

# 优先级：数值越小越优先注入（借鉴 orchestrator.PRIORITY，适配 V12 数据模型）
PRIORITY = {
    "long_term_constraints": 0,   # 写作铁律，最高
    "continuity_risks": 1,        # 连贯性风险，防跑偏
    "next_chapter_commitments": 2,  # 上一章留下的承诺，必须兑现
    "character_state": 3,         # 核心角色当前状态
    "foreshadow_active": 4,       # 活跃伏笔
    "timeline": 5,                # 时间线事件
    "recent_chapters": 6,         # 近章摘要
}


def load_state(project: Path) -> dict[str, Any]:
    state_file = project / "追踪" / "_tracking-state.json"
    if not state_file.is_file():
        raise FileNotFoundError(f"tracking state missing: {state_file}")
    return json.loads(state_file.read_text(encoding="utf-8"))


def extract_keywords(outline_text: str) -> list[str]:
    """从细纲提取关键词：角色名（《》内/顿号分隔的专名）+ 高频词。

    最小版：抓中文专名模式（《xx》《xx》书名号）+ 已知角色名不做先验假设，
    用书名号、引号内片段作为关键词锚；其余靠字符 n-gram 高频词兜底。
    """
    keywords: list[str] = []
    # 书名号/引号内片段作为专名锚
    keywords += re.findall(r'[《「“"]([^》」”"]{2,12})', outline_text)
    # 顿号分隔的连续专名片段
    keywords += re.findall(r"([\u4e00-\u9fff]{2,8})(?:[、，,]|$)", outline_text)
    # 去重、去单字、截断
    seen: set[str] = set()
    result: list[str] = []
    for kw in keywords:
        kw = kw.strip()
        if len(kw) < 2 or kw in seen:
            continue
        seen.add(kw)
        result.append(kw)
    return result[:50]


def build_working(state: dict[str, Any], outline_core: str) -> list[dict[str, Any]]:
    ctx = state.get("context", {})
    items: list[dict[str, Any]] = []
    pos = ctx.get("position", {})
    items.append(
        {
            "layer": "working",
            "source": "position",
            "priority": PRIORITY["next_chapter_commitments"],
            "content": f"位置：卷[{pos.get('volume', '?')}] 第{pos.get('volume_start_chapter', '?')}章起 | 场景[{pos.get('scene', '?')}] | 时间[{pos.get('story_time', '?')}]",
        }
    )
    for i, commitment in enumerate(ctx.get("next_chapter_commitments", [])[:2]):
        items.append(
            {
                "layer": "working",
                "source": "next_chapter_commitments",
                "priority": PRIORITY["next_chapter_commitments"],
                "content": f"上一章承诺[{i+1}]：{commitment}",
            }
        )
    if outline_core:
        items.append(
            {
                "layer": "working",
                "source": "outline_core",
                "priority": PRIORITY["next_chapter_commitments"],
                "content": f"本章核心事件：{outline_core}",
            }
        )
    return items


def build_episodic(state: dict[str, Any], max_items: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    ctx = state.get("context", {})
    for recent in ctx.get("recent_chapters", [])[-3:]:
        items.append(
            {
                "layer": "episodic",
                "source": "recent_chapters",
                "chapter": recent.get("chapter"),
                "priority": PRIORITY["recent_chapters"],
                "content": f"第{recent.get('chapter')}章：{recent.get('summary', '')}",
            }
        )
    timeline = state.get("timeline", {})
    for event in list(timeline.values())[-3:]:
        items.append(
            {
                "layer": "episodic",
                "source": "timeline",
                "chapter": event.get("reveal_chapter") or event.get("updated_chapter"),
                "priority": PRIORITY["timeline"],
                "content": f"事件[{event.get('id')}]：{event.get('objective_fact', '')}",
            }
        )
    items.sort(key=lambda x: (-(x.get("chapter") or 0), x["priority"]))
    return items[:max_items]


def build_semantic(state: dict[str, Any], keywords: list[str], max_items: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []

    # 长期约束（写作铁律，无条件注入）
    for constraint in state.get("context", {}).get("long_term_constraints", []):
        items.append(
            {
                "layer": "semantic",
                "source": "long_term_constraints",
                "priority": PRIORITY["long_term_constraints"],
                "content": f"铁律：{constraint}",
            }
        )

    # 连贯性风险
    for risk in state.get("context", {}).get("continuity_risks", []):
        items.append(
            {
                "layer": "semantic",
                "source": "continuity_risks",
                "priority": PRIORITY["continuity_risks"],
                "content": f"风险：{risk}",
            }
        )

    # 角色状态（相关性：细纲关键词命中角色的 identity/location/open_threads 则提升）
    for name, profile in state.get("characters", {}).items():
        content_parts = [
            f"{name}｜{profile.get('identity', '')}",
            f"位置：{profile.get('location', '')}",
            f"状态：{profile.get('state', '')}",
        ]
        if profile.get("open_threads"):
            content_parts.append(f"未结：{'；'.join(profile['open_threads'][:2])}")
        score = sum(1 for kw in keywords if kw in name or any(kw in p for p in content_parts))
        items.append(
            {
                "layer": "semantic",
                "source": "character_state",
                "priority": PRIORITY["character_state"],
                "relevance_score": score,
                "content": "｜".join(content_parts),
            }
        )

    # 活跃伏笔
    for fid, f in state.get("foreshadow", {}).items():
        if f.get("status") in ("已埋", "需处理"):
            score = sum(1 for kw in keywords if kw in f.get("summary", ""))
            items.append(
                {
                    "layer": "semantic",
                    "source": "foreshadow_active",
                    "priority": PRIORITY["foreshadow_active"],
                    "relevance_score": score,
                    "content": f"伏笔[{fid}]({f.get('status')})：{f.get('summary', '')}",
                }
            )

    # 排序：铁律/风险无条件优先（写作红线，不受相关性挤占）；其余按相关性降序、同级按优先级。
    def sort_key(item: dict[str, Any]) -> tuple[int, int, int]:
        if item.get("source") in ("long_term_constraints", "continuity_risks"):
            return (0, item["priority"], 0)
        score = item.get("relevance_score", 0)
        return (1, -score, item["priority"])

    items.sort(key=sort_key)
    return items[:max_items]


def resolve_outline_core(outline_file: Path | None) -> str:
    if outline_file is None or not outline_file.is_file():
        return ""
    try:
        text = outline_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""
    # 抓「核心事件：...」行；无则抓第一个 `- ` 行
    match = re.search(r"核心事件[:：]\s*(.+)", text)
    if match:
        return match.group(1).strip()[:200]
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("- ") or line.startswith("#"):
            return line[:200]
    return text[:200]


def build_memory_pack(project: Path, chapter: int, *, task: str = "write", max_items: int = 24) -> dict[str, Any]:
    state = load_state(project)
    outline_file = None
    # 细纲命名：细纲_第N章.md（支持任意宽度数字）
    outline_dir = project / "大纲"
    for path in sorted(outline_dir.glob("细纲_第*章*.md")):
        match = re.fullmatch(r"细纲_第0*(\d+)章.*\.md", path.name)
        if match and int(match.group(1)) == chapter:
            outline_file = path
            break

    outline_text = outline_file.read_text(encoding="utf-8") if outline_file and outline_file.is_file() else ""
    outline_core = resolve_outline_core(outline_file)
    keywords = extract_keywords(outline_text) if outline_text else []

    # 预算：按任务类型分配各层上限（总 = max_items）
    if task == "review":
        working_limit, episodic_limit = max(2, max_items // 6), max(4, max_items // 4)
    elif task == "deslop":
        working_limit, episodic_limit = max(2, max_items // 6), max(3, max_items // 5)
    else:  # write 默认
        working_limit, episodic_limit = max(2, max_items // 8), max(4, max_items // 5)
    semantic_limit = max(1, max_items - working_limit - episodic_limit)

    working = build_working(state, outline_core)[:working_limit]
    episodic = build_episodic(state, episodic_limit)
    semantic = build_semantic(state, keywords, semantic_limit)

    return {
        "schema": SCHEMA,
        "project": str(project),
        "chapter": chapter,
        "task": task,
        "outline_used": outline_file is not None,
        "keywords": keywords[:20],
        "layers": {
            "working": working,
            "episodic": episodic,
            "semantic": semantic,
        },
        "stats": {
            "working_injected": len(working),
            "episodic_injected": len(episodic),
            "semantic_injected": len(semantic),
            "semantic_total": (
                len(state.get("context", {}).get("long_term_constraints", []))
                + len(state.get("context", {}).get("continuity_risks", []))
                + len(state.get("characters", {}))
                + len(state.get("foreshadow", {}))
            ),
            "budget_total": max_items,
            "keyword_count": len(keywords),
        },
    }


def _render_text(pack: dict[str, Any]) -> str:
    lines = [
        f"记忆包: 第{pack['chapter']}章 (task={pack['task']}) 细纲={'有' if pack['outline_used'] else '无'}",
        f"关键词: {'、'.join(pack['keywords']) or '（无）'}",
    ]
    for layer in ("working", "episodic", "semantic"):
        lines.append(f"\n[{layer}] ({len(pack['layers'][layer])} 条)")
        for item in pack["layers"][layer]:
            lines.append(f"  · {item['content'][:80]}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True, help="book project root containing 追踪/")
    parser.add_argument("--chapter", type=int, required=True, help="target chapter number")
    parser.add_argument("--task", choices=("write", "review", "deslop"), default="write")
    parser.add_argument("--max-items", type=int, default=24)
    parser.add_argument("--json", action="store_true", help="output single-line JSON")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        pack = build_memory_pack(args.project, args.chapter, task=args.task, max_items=args.max_items)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except (json.JSONDecodeError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if args.json:
        sys.stdout.buffer.write(
            (json.dumps(pack, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        )
    else:
        print(_render_text(pack))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
