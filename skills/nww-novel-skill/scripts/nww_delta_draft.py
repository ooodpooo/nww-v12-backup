#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nww_delta_draft.py — 逐章事务 delta 草稿预填（确定性提取 → LLM 补语义 → A-4 终审）。

设计（2026-09-05，资产质量批次 Phase C）：
- delta 手写是漏写的主要来源。本脚本从正文确定性提取「高置信」部分预填草稿，
  把作者/LLM 的工作从「凭记忆写全」降为「核对 + 补语义」，再由 A-4 闸在 commit
  时终审——草稿不是免检通行证，只是降低漏写概率的脚手架。
- 与 A-4 共用同一套提取器（nww_delta_audit 的正则），保证草稿预填和闸门判定
  看到同一个正文事实，不会出现「草稿说没变化、闸门说有」的分裂。
- 纯确定性、零 LLM、只读正文与状态；缺省输出 stdout，--out 可写文件（不写入
  追踪/，避免污染派生视图一致性校验与快照差集清理）。

预填范围（只预填机械可判定的部分，语义字段全部留白标记【草稿】）：
- 高置信：死亡 → retired_characters + retirement_reasons 占位；
  境界/能力变化 → character_changes（change 为句子摘录）+ 当前快照副本；
- 提示级：受伤、活跃伏笔提及、在场角色清单 → _draft_hints（不冒充语义判断）；
- 留白：result / chapter_type / cost_change / next_chapter_commitments /
  timeline_events / global_change——这些必须由 LLM 补，机械层不猜。

调用：
    python nww_delta_draft.py --project BOOK --chapter N [--out 路径.json]
产出可直接编辑后送 nww_track_commit.py commit（A-4 在 commit 侧终审）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import nww_delta_audit as audit  # noqa: E402  与 A-4 共用提取器
from nww_track_commit import load_state, resolve_chapter_file  # noqa: E402

_DRAFT_TAG = "【草稿】"
# 容错：行首允许 markdown 装饰（` * _ >）——writer 偶发把标题写成
# `` `# 第28章 x` ``（反引号包裹，实测 2026-09-13 第028章），严格 `^#` 会失配，
# 逐章记录标题就回填成「【草稿】请填写章节标题」。
_TITLE_RE = re.compile(r"^[`*_>\s]*#+\s*第\s*\d+\s*章\s*(\S.*)?$")
_TITLE_HEAD_DECOR_RE = re.compile(r"^[`*_>\s]+")
_TITLE_TAIL_DECOR_RE = re.compile(r"[`*_\s]+$")


def _chapter_title(prose: str) -> str:
    """从正文 H1 提取章节标题；提不出 → 留白标记。"""
    for line in prose.splitlines():
        s = line.strip()
        if _TITLE_RE.match(s):
            title = _TITLE_HEAD_DECOR_RE.sub("", s)   # 剥行首装饰（含反引号）
            title = title.lstrip("#").strip()
            title = re.sub(r"^第\s*\d+\s*章\s*", "", title).strip()
            title = _TITLE_TAIL_DECOR_RE.sub("", title)  # 剥行尾装饰
            return title or f"{_DRAFT_TAG}请填写章节标题"
    return f"{_DRAFT_TAG}请填写章节标题"


def _current_global_view(project: Path, state: dict) -> dict:
    """供 LLM 参照的「当前全局状态」（只增不改：state["global"] 为空时回退只读视图）。

    2026-10-12 P0 修复：实测 4 本书 26 份 delta 的 global_change 非空率 = 0/26。
    原因之一是 `state.get("global", {})` 在 global 尚未初始化时恒空 —— 模型看到
    「当前全局状态 = {}」，再叠加旧 hint「旧 hint 明示「无变化就置空」」，
    形成**自我实现的空转**：越空越不填，越不填越空。（旧 hint 还明示「无变化就别填」，已改。）

    回退数据源 `追踪/全局状态.md`（由 nww_track_commit.py:2287 render_global_view 落盘）
    —— 它是 state["global"] 的只读渲染，内容等价但**在 global 为空时仍有骨架段落**，
    至少能让模型看到该写哪一类键。读不到 → 返回空 dict，行为与修复前一致。
    """
    g = state.get("global") or {}
    if g:
        return g
    try:
        f = Path(project) / "追踪" / "全局状态.md"
        if not f.is_file():
            return {}
        text = f.read_text(encoding="utf-8", errors="ignore")
        # 只回传「## 段 → - **键**：值」的骨架，不回传说明性文字（避免污染 prompt）
        out: dict[str, Any] = {}
        section = ""
        for ln in text.splitlines():
            s = ln.strip()
            if s.startswith("## "):
                section = s[3:].strip()
                out.setdefault(section, {})
            elif s.startswith("- ") and section:
                m = re.match(r"-\s*\*\*(.+?)\*\*\s*[：:]\s*(.+)$", s)
                if m:
                    out[section][m.group(1).strip()] = m.group(2).strip()
                elif s[2:].strip():
                    out[section].setdefault("_lines", []).append(s[2:].strip())
        return {k: v for k, v in out.items() if v}
    except Exception:
        return {}


def build_draft(project: Path, chapter: int, body_path: Path | None = None) -> dict[str, Any]:
    state = load_state(project)
    last = state["last_committed_chapter"]
    is_append = chapter == last + 1
    if body_path is not None:
        prose = body_path.read_text(encoding="utf-8")
    else:
        prose = audit._load_prose(project, chapter)
    if not prose:
        raise SystemExit(
            f"ERROR: 无法定位第 {chapter} 章正文（resolve_chapter_file 为空），无法预填草稿"
        )

    tracked = audit._tracked_names(project, state)
    hot = set(state.get("characters", {}).keys())

    # 逐句扫描（与 A-4 同一提取器）：分类每个「角色 × 句子」命中
    power_hits: dict[str, list[str]] = {}
    death_hits: dict[str, list[str]] = {}
    injury_names: set[str] = set()
    present: set[str] = set()
    for sent in audit._sentences(prose):
        for name in tracked:
            if name not in sent:
                continue
            present.add(name)
            if audit._DEATH_RE.search(sent):
                death_hits.setdefault(name, []).append(sent.strip()[:80])
            elif audit._POWER_GAIN_RE.search(sent):
                power_hits.setdefault(name, []).append(sent.strip()[:80])
            elif audit._INJURY_RE.search(sent) and name in hot:
                injury_names.add(name)

    # P2 fix: 扩展预填范围到所有出场核心角色（不只是 power_hits）
    # 目的：让 LLM 有机会更新 location/state/open_threads 等字段，解决快照滞后问题
    _present_hot = [name for name in sorted(present) if name in hot and name not in death_hits]

    # 预填 character_changes：所有出场核心角色
    character_changes = []
    for name in _present_hot:
        if name in power_hits:
            # 境界/能力变化：保留原有强提示
            character_changes.append({
                "name": name,
                "change": f"{_DRAFT_TAG}【境界/能力变化】正文表述：{power_hits[name][0]}（请核实并改写为准确变化）"
            })
        else:
            # 普通出场：提示 LLM 检查 location/state 是否需要更新
            _cur = state["characters"].get(name, {})
            _cur_loc = _cur.get("location", "未知")
            _cur_state = str(_cur.get("state", "未知"))[:50]
            character_changes.append({
                "name": name,
                "change": (
                    f"{_DRAFT_TAG}【状态检查】该角色本章出场。"
                    f"当前 location=\"{_cur_loc}\"，state=\"{_cur_state}\"。"
                    "请根据正文判断：location/state 是否有变化？"
                    "如有变化，在 character_snapshots 里更新对应字段；"
                    "如无变化，保持原值不动。"
                )
            })

    retired = sorted(death_hits)
    retirement_reasons = {
        name: f"{_DRAFT_TAG}正文死亡表述：{death_hits[name][0]}（请补充退役原因）"
        for name in retired
    }
    # 所有出场核心角色的当前快照副本（LLM 在副本上改字段）
    snapshots = {
        name: json.loads(json.dumps(state["characters"].get(name, {}), ensure_ascii=False))
        for name in _present_hot
    }

    # timeline id 预填（A-7 闸配合：让 LLM 有正确起点，避免从 E001 起编覆盖历史事件）
    _existing_tl_ids = set((state.get("timeline") or {}).keys())
    _max_tl_num = 0
    for _tid in _existing_tl_ids:
        if _tid.startswith("E") and _tid[1:].isdigit():
            _max_tl_num = max(_max_tl_num, int(_tid[1:]))
    _next_tl_id = f"E{_max_tl_num + 1:03d}" if is_append else None
    # 提示级：活跃伏笔提及（不冒充 foreshadow_changes，语义归属 LLM）
    mentioned = set(audit._FORESHADOW_ID_RE.findall(prose))
    active_ids = {
        fid for fid, row in (state.get("foreshadow") or {}).items()
        if isinstance(row, dict) and row.get("status") in ("已埋", "需处理")
    }
    foreshadow_hints = sorted(mentioned & active_ids)

    # 上下文预填：从当前 state 复制（LLM 负责更新 position/约束增删）
    context = {
        "position": json.loads(json.dumps(state["context"]["position"], ensure_ascii=False)),
        "long_term_constraints": list(state["context"]["long_term_constraints"]),
        "active_character_names": [
            name for name in sorted(present) if name in hot and name not in retired
        ][:6],
        "continuity_risks": list(state["context"]["continuity_risks"]),
    }

    # 2026-10-26 深化修复（R2-3）：确定性预填 cost_change 与 wordcount，避免模型漏填触发 A-1 BLOCK
    _auto_cost_change = None
    try:
        _bible_p = project / "bible.json"
        if _bible_p.is_file():
            _bible = json.loads(_bible_p.read_text(encoding="utf-8"))
            _dev = ((_bible.get("cost_system") or {}).get("device") or "").strip()
            if _dev:
                _m_hits = re.findall(re.escape(_dev) + r"[：:]?\s*(\d+)", prose)
                if _m_hits:
                    _auto_cost_change = {"device": _dev, "count": int(_m_hits[-1])}
    except Exception:
        _auto_cost_change = None

    _auto_wordcount = None
    try:
        import nww_wordcount as _wc
        _auto_wordcount = _wc.build_current_wordcount_record(project, chapter)
    except Exception:
        _auto_wordcount = None

    return {
        "schema_version": 1,
        "mode": "append" if is_append else "revision",
        "chapter": chapter,
        "chapter_title": _chapter_title(prose),
        "expected_state_revision": state["state_revision"],
        "character_snapshots": snapshots,
        "context": context,
        "delta": {
            "result": f"{_DRAFT_TAG}请用一两句话概述本章结果（照抄正文明确事实，禁推测）",
            "character_changes": character_changes,
            "foreshadow_changes": [],
            "timeline_events": [],
            "constraints": [],
            "next_chapter_commitments": [],
            "retired_context_items": [],
            "retired_characters": retired if is_append else [],
            "retirement_reasons": retirement_reasons if is_append else {},
            "cost_change": _auto_cost_change,
            "chapter_type": None,
        },
        "wordcount": _auto_wordcount,
        "global_change": None,
        "_draft_hints": {
            "note": (
                "本文件是确定性预填草稿，字段值可能不完整；编辑后送 nww_track_commit.py commit，A-4 闸终审。"
                "retired_characters/retirement_reasons 仅 append 模式有效。"
                "character_changes 包含两类：【境界/能力变化】（必须更新快照）和【状态检查】（请根据正文判断 location/state/open_threads 是否需要更新）。"
            ),
            "global_change_hint": (
                "若本章涉及以下全局状态变化，请在 global_change 中填写对应字段（递归合并，未提及字段不变）："
                "纪元/时代、卷/阶段、核心冲突、发动机/动力源、世界变化、势力格局、下一切换点。"
                "★ 判定标准：该变化是否会让**后续若干章的世界运行方式**不同？"
                "是 → 必填；只是本章内部的胜负/情绪/单次事件 → 不填（写进 result 即可）。"
                "未变化的字段不要重复输出。"
            ),
            "current_global": _current_global_view(project, state),
            "characters_present": sorted(present),
            "injury_names": sorted(injury_names),
            "active_foreshadow_mentioned": foreshadow_hints,
            "next_timeline_id": _next_tl_id,
            "revision_note": (
                f"第 {chapter} 章已是已提交章节，本草稿按 revision 模式预填（禁止 retired_characters）；"
                if not is_append else None
            ),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="逐章事务 delta 草稿预填（确定性提取，A-4 终审前置脚手架）")
    parser.add_argument("--project", type=Path, required=True, help="book project root containing 追踪/")
    parser.add_argument("--chapter", type=int, required=True)
    parser.add_argument("--out", type=Path, default=None, help="草稿输出路径（缺省打印 stdout；勿放 追踪/ 内）")
    parser.add_argument("--body", type=Path, default=None, help="dry_run 预览正文路径（缺省从 resolve_chapter_file 加载）")
    args = parser.parse_args()

    draft = build_draft(args.project, args.chapter, body_path=args.body)
    payload = json.dumps(draft, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload, encoding="utf-8")
        print(f"draft written: {args.out}")
    else:
        sys.stdout.buffer.write(payload.encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
