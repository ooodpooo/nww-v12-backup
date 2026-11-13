#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nww_volume_review.py — 卷末复盘门禁（P0-3，独立模块，零耦合接入）。

在卷末章（或期中任意时点）生成 `追踪/卷末复盘/卷N_复盘.md`，把「卷纲承诺」与
「追踪权威状态 + 逐章记录」摆到同一张桌上。确定性层（脚本算）与研判层（模板，
人工/LLM 填）严格分离——脚本不下任何叙事判断。

确定性层（三大指标）：
  1. 伏笔账：本卷新埋 / 本卷有动作 / 本卷回收 / 活跃但本卷零动作（休眠风险），
     数据源 = state.foreshadow（权威现值）× 逐章记录「伏笔变化」节（历史轨迹）。
  2. 逾期与缺字段：K5 stale_core_hook_lines（官方实现复用）+ policy 缺字段清单
     （nww_foreshadow_policy.collect_findings 复用），以卷末截止章计算。
  3. 跨卷继承清单：活跃伏笔 / 连续性风险 / 活跃角色——下卷开篇必须接住的东西。

研判层（模板，留白给人/LLM）：
  - 卷纲核心目标逐条 [ ] 达成核对
  - 三事件弧 [ ] 已落地 / 部分 / 未落地（原因）
  - 下卷（卷纲N+1）核心冲突/目标/伏笔计划摘录 + 衔接注意点

用法：
  python3 nww_volume_review.py --project <book_root> --volume 1
只读 state/大纲/逐章记录，唯一写盘 = 复盘 md。exit 0 成功；环境错误 exit 1。
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nww_track_commit as tc
import nww_foreshadow_policy as fpolicy

RESOLVED_STATUSES = ("已回收",)
ACTIVE_STATUSES = ("已埋", "需处理")


# ---------------- 解析 ----------------
def parse_volume_outline(text: str) -> dict:
    out = {"raw": text, "start": None, "end": None}
    m = re.search(r"章节范围.*?第\s*(\d+)\s*[–—\-~～]\s*(\d+)\s*章", text)
    if m:
        out["start"], out["end"] = int(m.group(1)), int(m.group(2))
    for key, label in (("核心冲突", "core_conflict"), ("核心目标（卷末状态）", "core_goal"),
                       ("本卷主题", "theme")):
        m = re.search(r"-\s*\*\*%s\*\*：(.*)" % re.escape(key), text)
        out[label] = m.group(1).strip() if m else None
    # 事件弧：### 事件弧X（…） 至下一个 ###/##
    arcs = []
    for m in re.finditer(r"###\s*(事件弧[^\n]*)\n(.*?)(?=\n###|\n##|\Z)", text, re.S):
        body = re.sub(r"\n\*{3,}\s*$", "", m.group(2).strip()).rstrip()  # 去掉卷纲节尾分隔线
        arcs.append({"heading": m.group(1).strip(), "body": body})
    out["arcs"] = arcs
    # 伏笔与回收段
    m = re.search(r"##\s*[^\n]*伏笔与回收[^\n]*\n(.*?)(?=\n##|\Z)", text, re.S)
    out["foreshadow_plan"] = m.group(1).strip() if m else None
    return out


def parse_chapter_records(tracking: Path, start: int, end: int) -> dict:
    """逐章记录「伏笔变化」节 → {fid: [(chapter, status, tail), ...]} + 各章结果行。"""
    record_dir = tracking / "逐章记录"
    hook_re = re.compile(r"^-\s*(F\d+)｜([^｜]*)｜(.*?)｜回收(.*)$")
    hooks: dict[str, list] = {}
    results: list[tuple[int, str]] = []
    for path in sorted(record_dir.glob("第*章.md")):
        m = re.fullmatch(r"第(\d+)章\.md", path.name)
        if not m:
            continue
        chapter = int(m.group(1))
        if not (start <= chapter <= end):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        section = None
        for line in text.splitlines():
            if line.startswith("## "):
                section = line
                continue
            if section == "## 伏笔变化":
                hm = hook_re.match(line.strip())
                if hm:
                    hooks.setdefault(hm.group(1), []).append((chapter, hm.group(2), hm.group(4)))
            elif section is None and line.startswith("- 结果："):
                results.append((chapter, line[len("- 结果："):].strip()))
    return {"hooks": hooks, "results": results}


# ---------------- 生成 ----------------
def build_review(project: Path, volume: int) -> tuple[str, dict]:
    tracking = project / "追踪"
    state = json.loads((tracking / "_tracking-state.json").read_text(encoding="utf-8"))
    last = int(state["last_committed_chapter"])
    foreshadow = state.get("foreshadow") or {}

    outline_dir = project / "大纲"
    vol_path = outline_dir / f"卷纲_第{volume}卷.md"
    if not vol_path.is_file():
        raise FileNotFoundError(f"卷纲不存在：{vol_path}")
    vol = parse_volume_outline(vol_path.read_text(encoding="utf-8"))
    if vol["start"] is None:
        raise ValueError(f"卷纲章节范围解析失败：{vol_path}")
    next_path = outline_dir / f"卷纲_第{volume + 1}卷.md"
    next_vol = parse_volume_outline(next_path.read_text(encoding="utf-8")) if next_path.is_file() else None

    v_start, v_end = vol["start"], vol["end"]
    review_end = min(v_end, last)
    interim = last < v_end
    records = parse_chapter_records(tracking, v_start, review_end)

    # ---- 指标一：伏笔账 ----
    planted_in_vol = {fid: row for fid, row in foreshadow.items()
                      if v_start <= int(row.get("planted_chapter") or 0) <= review_end}
    touched = records["hooks"]
    unregistered = sorted(fid for fid in touched if fid not in foreshadow)
    resolved_in_vol: dict[str, list] = {}
    for fid, entries in touched.items():
        for chapter, status, _tail in entries:
            if status in RESOLVED_STATUSES:
                resolved_in_vol.setdefault(fid, []).append(chapter)
    active_all = {fid: row for fid, row in foreshadow.items() if row.get("status") in ACTIVE_STATUSES}
    dormant_risk = {fid: row for fid, row in active_all.items() if fid not in touched}

    # ---- 指标二：逾期 + 缺字段（截止卷末章）----
    stale = tc.stale_core_hook_lines(foreshadow, review_end)
    findings = fpolicy.collect_findings({**state, "last_committed_chapter": review_end})

    L: list[str] = []
    L.append(f"# 卷{volume} 复盘（{'期中' if interim else '卷末'}）")
    L.append("")
    L.append(
        f"> 生成：nww_volume_review.py（确定性层）｜状态修订：{state['state_revision']}｜"
        f"卷范围：第{v_start}–{v_end}章｜实际覆盖：第{v_start}–{review_end}章"
        + ("（**期中复盘**：本卷尚未写完，以下为截至当前的盘点）" if interim else "")
    )
    L.append("")

    L.append("## 一、伏笔账（确定性统计）")
    L.append("")
    L.append(f"本卷新埋 {len(planted_in_vol)} 条｜本卷有动作 {len(touched)} 条｜"
             f"本卷回收 {len(resolved_in_vol)} 条｜活跃但本卷零动作 {len(dormant_risk)} 条（休眠风险）")
    L.append("")
    if planted_in_vol:
        L.append("### 本卷新埋")
        for fid in sorted(planted_in_vol):
            row = planted_in_vol[fid]
            L.append(f"- {fid}（第{row['planted_chapter']}章）：{row['summary']}")
        L.append("")
    if touched:
        L.append("### 本卷有动作（逐章记录轨迹）")
        L.append("| ID | 埋设章 | 本卷出现章 | 卷末状态 | 计划回收章 | 核心/半衰期 |")
        L.append("|---|---:|---|---|---:|---|")
        for fid in sorted(touched):
            row = foreshadow.get(fid)
            chapters = "、".join(str(c) for c, _s, _t in touched[fid])
            if row is None:
                L.append(f"| {fid} | ⚠未登记 | {chapters} | ⚠未登记 | — | — |")
                continue
            planned = row.get("planned_resolution_chapter")
            core = f"核心/{row.get('half_life_chapters')}" if row.get("core_hook") else "—"
            L.append(f"| {fid} | {row.get('planted_chapter', '?')} | {chapters} | "
                     f"{row.get('status', '?')} | {planned if planned else '未定'} | {core} |")
        L.append("")
        if unregistered:
            L.append("**⚠ 账务不一致告警**：以下伏笔 ID 出现于逐章记录但未在追踪状态登记，"
                     "需人工确认是否已并入其他伏笔或需补登记：")
            for fid in unregistered:
                chapters = "、".join(str(c) for c, _s, _t in touched[fid])
                L.append(f"- {fid}（出现于第{chapters}章）")
            L.append("")
    if resolved_in_vol:
        L.append("### 本卷回收")
        for fid in sorted(resolved_in_vol):
            L.append(f"- {fid}：第{'、'.join(str(c) for c in resolved_in_vol[fid])}章回收")
        L.append("")
    if dormant_risk:
        L.append("### 活跃但本卷零动作（休眠风险——读者记忆衰减区）")
        for fid in sorted(dormant_risk):
            row = dormant_risk[fid]
            L.append(f"- {fid}（埋第{row['planted_chapter']}章，最近变更第{row.get('updated_chapter')}章）："
                     f"{row['summary']}")
        L.append("")

    L.append("## 二、逾期与缺字段（截至第%d章）" % review_end)
    L.append("")
    if stale:
        L.append("**K5 核心伏笔超半衰期：**")
        L.extend(f"- {s}" for s in stale)
    else:
        L.append("- K5 核心伏笔无超半衰期。")
    if findings["missing"]:
        L.append("**debt 元数据缺字段：**")
        L.extend(f"- {m['id']} 缺 {', '.join(m['gaps'])}（埋第{m['planted_chapter']}章，已 {m['age_chapters']} 章）"
                 for m in findings["missing"])
    else:
        L.append("- 已埋伏笔 debt 元数据完整。")
    L.append("")

    L.append("## 三、事件弧对照（研判模板——人工/LLM 填写，脚本不代判）")
    L.append("")
    L.append("### 卷纲核心目标（卷末状态）")
    L.append(f"> {vol['core_goal'] or '（卷纲未提供）'}")
    L.append("")
    L.append("逐条核对：")
    goal = (vol["core_goal"] or "")
    for part in [p.strip() for p in re.split(r"[；;]", goal) if p.strip()]:
        L.append(f"- [ ] {part} —— 达成证据（章节/事件）：")
    L.append("")
    for arc in vol["arcs"]:
        L.append(f"#### {arc['heading']}")
        L.append("")
        L.append(arc["body"])
        L.append("")
        L.append("- [ ] 已落地　- [ ] 部分落地　- [ ] 未落地（原因：＿＿＿＿）")
        L.append("")

    L.append("## 四、下卷衔接（卷%d → 卷%d）" % (volume, volume + 1))
    L.append("")
    if next_vol:
        L.append(f"**下卷核心冲突**：{next_vol['core_conflict'] or '（未提供）'}")
        L.append("")
        L.append(f"**下卷核心目标**：{next_vol['core_goal'] or '（未提供）'}")
        L.append("")
        if next_vol["foreshadow_plan"]:
            L.append("**下卷伏笔计划（卷纲原文）**：")
            L.append("")
            L.append(next_vol["foreshadow_plan"])
            L.append("")
    else:
        L.append("（无下一卷卷纲）")
        L.append("")
    L.append("### 跨卷继承清单（下卷开篇必须接住）")
    L.append("")
    L.append("**活跃伏笔**（%d 条，核心 %d）：" % (
        len(active_all), sum(1 for r in active_all.values() if r.get("core_hook"))))
    for fid in sorted(active_all):
        row = active_all[fid]
        planned = f"计划第{row['planned_resolution_chapter']}章回收" if row.get("planned_resolution_chapter") else "回收未定"
        mark = "★" if row.get("core_hook") else ""
        L.append(f"- {fid}{mark}（{planned}）：{row['summary']}")
    L.append("")
    risks = state.get("context", {}).get("continuity_risks") or []
    if risks:
        L.append("**连续性风险**：")
        L.extend(f"- {r}" for r in risks)
        L.append("")
    names = state.get("context", {}).get("active_character_names") or []
    if names:
        L.append("**活跃角色**：" + "、".join(names))
        L.append("")

    L.append("## 附录：本卷章节结果速览（逐章记录「结果」行，供事件弧核对）")
    L.append("")
    for chapter, result in records["results"]:
        L.append(f"- 第{chapter}章：{result}")
    L.append("")

    stats = {
        "volume": volume, "interim": interim, "review_end": review_end,
        "planted": len(planted_in_vol), "touched": len(touched),
        "resolved": len(resolved_in_vol), "dormant": len(dormant_risk),
        "stale": len(stale), "missing": len(findings["missing"]),
        "unregistered": len(unregistered),
    }
    return "\n".join(L) + "\n", stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True, help="book project root（含 追踪/ 与 大纲/）")
    parser.add_argument("--volume", type=int, required=True, help="复盘卷号 N（对应 大纲/卷纲_第N卷.md）")
    args = parser.parse_args()
    try:
        review, stats = build_review(args.project, args.volume)
    except (FileNotFoundError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    out_dir = args.project / "追踪" / "卷末复盘"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"卷{args.volume}_复盘.md"
    out_path.write_text(review, encoding="utf-8")
    print(json.dumps({"output": str(out_path), "stats": stats}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
