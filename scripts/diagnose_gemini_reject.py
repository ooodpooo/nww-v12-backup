#!/usr/bin/env python3
"""定位 Gemini 拒答「我只是一个语言模型」的触发要素（脱纲审计链路）。

背景（2026-09-26 23:12 用户截图）：脱纲审计 prompt（~19.4k 字符）被 Gemini 消费端
安全过滤拒答。同通道其它链路（writer 22.7k / agent chat 23.4k）都正常。

被审 prompt 两个独有特征（其它链路都没有）：
  A. 「你是一致性审计师，…」角色扮演开头
  B. <prose>/<outline>/<bible>… XML 式标签包裹大段内容
对照实验逐步消去，定位触发要素。只读正文，不写任何文件。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "web" / "backend"))
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ.setdefault("NWW_DEV_WEAK_SECRET", "1")

BOOK = ROOT / "projects" / "admin" / "用泡面征服修仙界"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=150.0)
    args = ap.parse_args()

    from app.agents_runtime.outline_audit import _AUDIT_SYSTEM
    import app.chrome_bridge as cb

    prose = (BOOK / "正文/第一卷/第025章_历练队的订单.md").read_text(encoding="utf-8")
    ol = BOOK / "大纲/细纲_第025章.md"
    outline = ol.read_text(encoding="utf-8") if ol.exists() else "（无细纲）"

    # 短版素材（控制体量，隔离"结构"变量）
    prose_s, outline_s = prose[:1200], outline[:600]

    REJECT = ("一个语言模型", "没办法提供", "无法提供这方面的帮助", "超出了我的设计用途",
              "违反内容政策", "无法协助", "不能协助", "I can't help")

    cases = [
        # (标签, 说明, system, user)
        ("T1 复刻结构·小体量", "A+B 都在，素材仅 ~2k → 隔离体量变量",
         _AUDIT_SYSTEM,
         f"<prose>\n{prose_s}\n</prose>\n\n<outline>\n{outline_s}\n</outline>\n\n"
         "请审计。"),
        ("T2 去角色扮演开头", "消去 A：任务指令式开头，保留 XML 标签",
         _AUDIT_SYSTEM.replace(
             "你是一致性审计师，审计小说章节正文是否偏离原计划与世界状态。\n",
             "请审计以下小说章节正文是否偏离原计划与世界状态。\n"),
         f"<prose>\n{prose_s}\n</prose>\n\n<outline>\n{outline_s}\n</outline>\n\n"
         "请审计。"),
        ("T3 去XML标签", "消去 B：保留角色扮演开头，标签换中文方括号",
         _AUDIT_SYSTEM.replace("<prose>", "【本章正文】").replace("<outline>", "【本章细纲】")
                       .replace("<bible>", "【全书设定】").replace("<character_state>", "【角色状态】")
                       .replace("<foreshadow>", "【伏笔台账】").replace("<prev_delta>", "【上一章事实】"),
         f"【本章正文】\n{prose_s}\n\n【本章细纲】\n{outline_s}\n\n请审计。"),
    ]

    results = []
    for label, note, sys_p, usr_p in cases:
        n = len(sys_p) + len(usr_p)
        print("=" * 74)
        print(f"{label}   （{note}）投喂 ≈ {n:,} 字符")
        print("=" * 74)
        try:
            raw = cb.chat(sys_p, usr_p, "gemini-web", timeout=args.timeout,
                          session_id=f"audit_{label.split()[0].lower()}")
        except Exception as exc:  # noqa: BLE001
            print(f"❌ 桥接失败: {type(exc).__name__}: {str(exc)[:140]}")
            results.append((label, n, "ERROR", False))
            continue
        rejected = any(w in raw for w in REJECT)
        head = raw[:110].replace("\n", " ")
        print(f"回复 {len(raw)} 字符: {head}…")
        print(f"  → {'❌ 安全过滤拒答' if rejected else '✅ 正常响应'}")
        results.append((label, n, "REJECT" if rejected else "OK", not rejected))
        print()

    print("=" * 74)
    print("结论矩阵")
    print("=" * 74)
    for label, n, st, ok in results:
        print(f"  {label:20s} 投喂{n:>7,}  {st}")
    ok_map = {r[0].split()[0]: r[3] for r in results}
    if not ok_map.get("T1"):
        print("\n  ⇒ 小体量即拒 → 触发要素在结构（A 角色扮演 / B XML 标签）里，"
              "与体量无关；看 T2/T3 谁通过即知是哪个。")
    elif not ok_map.get("T2") and ok_map.get("T3"):
        print("\n  ⇒ 触发要素 = A 角色扮演开头（去 A 后通过）。")
    elif not ok_map.get("T3") and ok_map.get("T2"):
        print("\n  ⇒ 触发要素 = B XML 标签（去 B 后通过）。")
    elif ok_map.get("T1"):
        print("\n  ⇒ 三种变体小体量都通过 → 触发需要体量×结构组合，"
              "下一步要跑全量复刻（--full）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
