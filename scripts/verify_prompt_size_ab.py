#!/usr/bin/env python3
"""对照实验：prompt 体量 → EXECUTE_JSON 协议服从度。

背景（2026-09-26 步骤 4 撞到）：
  步骤 3 用 78 字裸提问 → Gemini **完美**输出协议块；
  步骤 4 走真实 /api/agent/chat（投喂 22,786 字符）→ 模型只回 40 字确认话术，
  **协议块完全没输出**，proposed_actions = None。

假设：投喂体量直接决定协议服从度（BRIDGE.md §9："削 prompt 是根治杠杆"；
39.7k 会 internal error）。本脚本用**同一句要求**在两档体量下对照：
  L2 = system only（~6,000 字符）
  L3 = system + 书稿素材（~22,800 字符，等同真实 /api/agent/chat 投喂量）

只读正文、不写任何文件、不跑 pipeline。
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
os.environ.setdefault("no_proxy", "127.0.0.1,localhost")
os.environ.setdefault("NWW_DEV_WEAK_SECRET", "1")

BOOK = ROOT / "projects" / "admin" / "用泡面征服修仙界"

ASK = ("请对第25章执行去AI味保守润色，并直接派单。"
       "回复最前面先一句话确认，紧接着输出 EXECUTE_JSON 标记行 + JSON。")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=200.0)
    ap.add_argument("--only", choices=("L2", "L3"), help="只跑一档")
    args = ap.parse_args()

    import app.agent as ag
    import app.chrome_bridge as cb
    from app.agent import _M_EXECUTE, _extract_marked_json

    ctx, _meta = ag._book_context("admin", "用泡面征服修仙界")
    material = ag._book_material("admin", "用泡面征服修仙界", "第25章 去AI味润色")
    system = f"{ctx}\n\n{ag._ENTRANCE_MAP}\n\n{ag._SYSTEM_PROMPT}"

    tiers = []
    if args.only != "L3":
        tiers.append(("L2  system only", system))
    if args.only != "L2":
        tiers.append(("L3  system + 17k 书稿素材",
                      f"{system}\n\n【书稿素材】（只读预注入）\n\n{material}"))

    results = []
    for label, sys_p in tiers:
        n = len(sys_p) + len(ASK)
        print("=" * 74)
        print(f"{label}   投喂 ≈ {n:,} 字符")
        print("=" * 74)
        try:
            raw = cb.chat(sys_p, ASK, "gemini-web", timeout=args.timeout,
                          session_id=f"ab_{label.split()[0]}")
        except Exception as exc:  # noqa: BLE001
            print(f"❌ 桥接失败: {type(exc).__name__}: {str(exc)[:160]}")
            results.append((label, n, None, False))
            continue
        print(f"取回 {len(raw)} 字符:")
        for line in raw.splitlines()[:25]:
            print(f"    {line}")
        payload, _clean = _extract_marked_json(raw, _M_EXECUTE)
        ok = bool(payload)
        print(f"\n  标记存活        : {'EXECUTE_JSON' in raw}")
        print(f"  抽出协议载荷    : {payload!r}" if payload else "  抽出协议载荷    : 无 ❌")
        if payload:
            try:
                print(f"  ✅ json.loads   : {json.loads(payload)}")
            except json.JSONDecodeError as e:
                print(f"  ❌ json.loads   : {e}")
                ok = False
        results.append((label, n, len(raw), ok))
        print()

    print("=" * 74)
    print("对照结论")
    print("=" * 74)
    for label, n, rlen, ok in results:
        print(f"  {label:32s} 投喂{n:>7,} → 回复{rlen if rlen is not None else 0:>6}  "
              f"{'✅ 协议服从' if ok else '❌ 未输出协议块'}")
    l2 = next((r for r in results if r[0].startswith("L2")), None)
    l3 = next((r for r in results if r[0].startswith("L3")), None)
    if l2 and l3 and l2[3] and not l3[3]:
        print("\n  ⇒ 定位：**17k 书稿素材预注入** 是协议服从度的分水岭"
              "（短投喂服从、全量投喂不服从）")
    elif l2 and l3 and not l2[3] and not l3[3]:
        print("\n  ⇒ 两档都不服从 → 元凶不是素材体量，需另查（prompt 措辞/模型波动）")
    elif l2 and l3 and l2[3] and l3[3]:
        print("\n  ⇒ 两档都服从 → 步骤 4 那次是模型随机波动，可重试确认")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
