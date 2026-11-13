#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nww_paragraph_gate.py — 段落节奏阻断门（V12.5，2026-09-13）。

规则从 quality/detect_internal.py 的 I-J 段落密度门禁**移植并校准**，
把原本只参与评分（rubric 内部六项之一）的 I-J 升级为 post-write 的 BLOCK 门禁。

## 检测项（3 个 BLOCK + 2 个 WARNING）

  J1 单段超长：单段 >220 字 → BLOCK；>160 字 → WARNING
  J2 长段连片：连续 3 段每段 >70 字且窗口内最长段 >120 字 → BLOCK（重叠窗口合并为一个 run）
  J3 信息层堆叠：段长 >120 字且 动作/心理/规则/因果 ≥3 类同段 → BLOCK

## 与 detect_internal.py 的两处**有意差异**（必须记录，避免形成第三套口径）

  **J1 的 BLOCK 线从 160 上调到 220。** detect_internal 的 `>160 → fail` 是
  **评分口径**——它只是 rubric 内部六项检测之一，单项 fail 不否决整章
  （实测有章 I-J fail 仍综合 92 分）。把它原样搬成「不通过就不得 commit」的
  流水线硬门会过度拦截：全仓 124 个当前交付章里 **18 章（14.5%）** 只因存在
  一个 160-220 字段就被判 BLOCK，而 160-220 恰恰是 `narrative-writer.md`
  与 `check-ai-patterns.js` 两个**写作者面向**契约里明确写作「warning」的区间
  （「150-220 warning / >220 strong warning——原则上必须拆段」）。
  「写作者按自己的契约写 → 被门禁拒收」是门禁失据，故 BLOCK 线取 220
  （与两份面向写作者的契约一致），160-220 降为 WARNING。
  实测：>220 只命中 2/124 章（1.6%），>160 命中 18/124 章（14.5%）。

  **J2 合并重叠窗口。** 滑动窗口每移动一格就判定一次，一个长段会被相邻的
  3 个窗口各算一次「违规」。实测 `第02章_第七日` 的 146 字段在 78/93/78/100
  的正常「短-长-短」呼吸里生成了 3 条违规
  （[78,93,146]/[93,146,78]/[146,78,100]），与 2026-09-09 校准注释写明的
  意图「只拦长段连片+墙的真病，正常短-长-短呼吸不受影响」相矛盾。
  本版把连续命中的窗口合并成一个 dense run，一个 run 只报一次
  （报区间与各段字数）。**规则本身（>70 连片 + >120 墙）不变**，只修重复计数。

## 调用位置

nww_post_write.py Step 1.5（enforce 之后，commit 之前）；
宿主侧另由 hooks/check-prose-after-write.sh 独立复扫（advisory 呈现）。

## 退出码

  0 全过（可能带 WARNING）
  2 BLOCK（命中 J1 BLOCK / J2 / J3 任一）
  -1 正文文件缺失
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# 让同目录脚本可被 import（段落切分的单一权威实现 nww_text.py）。
# 显式插入脚本自身目录：hook / toolbox 用绝对路径调用，但被 importlib 以
# 其它 cwd 加载时 sys.path[0] 未必是本目录。
sys.path.insert(0, str(Path(__file__).resolve().parent))
import nww_text  # noqa: E402


# ===========================================================================
# 检测常量
#
# J3 的四个词表/正则与 detect_internal.py **逐字一致**（见下方对齐断言）。
# 只有 J1 的 BLOCK 线与 J2 的窗口合并是有意差异，理由见模块 docstring。
# ===========================================================================

# J1 阈值（字数含标点，与 detect_internal 的 len(p) 口径一致）
#   BLOCK  线 220 = narrative-writer.md / check-ai-patterns.js 的「原则上必须拆段」
#   WARNING 线 160 = detect_internal 的 fail 线，在本门禁里降为强提醒
J1_BLOCK_CHARS = 220
J1_WARN_CHARS = 160

# J2 阈值（与 detect_internal 一致）
J2_MIN_EACH = 70      # 窗口内每段都要超过它
J2_WALL_CHARS = 120   # 窗口内最长段要超过它

# J3 阈值（与 detect_internal 一致）
J3_MIN_CHARS = 120    # 段长要超过它才判堆叠
J3_MIN_LAYERS = 3     # 同段要命中这么多类信息层

# J3 信息层堆叠：动作词（同 I_G_ACTION_WORDS）
_ACTION_WORDS = [
    "走", "站", "坐", "推", "拉", "拿", "放", "搁", "抬", "举", "摊",
    "握", "捏", "捡", "擦", "按", "写", "抄", "递", "接", "倒", "喝", "笑",
    "喊", "叹", "摇头", "点头", "起身", "迈步", "转身",
]

# J3 心理层
_MIND_PATTERN = re.compile(r"心里|觉得|他想|他明白|判断|念头|认为|打算|意识到")

# J3 规则层
_RULE_PATTERN = re.compile(r"规则|必须|不许|不能|只能|对应|等于|就是|规矩|走法|机制")

# J3 因果层
_CAUSE_PATTERN = re.compile(r"因为|所以|于是|也就是说|换句话说|这意味着|之所以")


def _split_paragraphs(text: str) -> tuple[list[str], str]:
    """段落切分 → (段列表, 模式)。

    V12.5（2026-09-13）：口径收敛到单一权威实现 ``nww_text.split_paragraphs``，
    本函数只是薄委派，避免「同一份正文在不同脚本里段数不同」的历史分裂。
    详见 nww_text.py 的模块 docstring。
    """
    return nww_text.split_paragraphs(text)


def check(prose_path: Path) -> dict:
    """I-J 段落密度门禁（BLOCK 版）。

    移植 detect_internal.detect_i_j_paragraph_density 的逻辑并做两处校准
    （J1 的 BLOCK 线取 220 而非 160、J2 合并重叠窗口），详见模块 docstring。
    命中 BLOCK 级返回 returncode=2，命中 WARNING 级返回 0。
    """
    if not prose_path.is_file():
        return {"ok": False, "returncode": -1, "error": f"正文文件缺失: {prose_path}"}

    text = prose_path.read_text(encoding="utf-8")
    paras, split_mode = _split_paragraphs(text)
    if len(paras) < 3:
        # 早退分支同样必须回完整 stats 契约：main() 无条件读这三个键，
        # 只回 {"stats": {}} 会让 print 抛 KeyError → 退出码 1 → 被上游误判成 BLOCK。
        lite = [len(p) for p in paras]
        return {
            "ok": True,
            "returncode": 0,
            "message": "段落<3，不判",
            "stats": {
                "paragraphs": len(paras),
                "max_chars_per_paragraph": max(lite) if lite else 0,
                "avg_chars_per_paragraph": round(sum(lite) / len(lite), 1) if lite else 0,
                "split_mode": split_mode,
            },
            "violations": [],
            "warnings": [],
        }

    # detect_internal 用 len(p)（字符数，含标点），不是 CJK 数 — 保持一致
    lens = [len(p) for p in paras]

    violations = []  # BLOCK 级
    warnings = []    # WARNING 级

    # ---- J1: 单段超长（BLOCK >220 / WARNING >160）----
    over160 = [i + 1 for i, x in enumerate(lens) if x > J1_WARN_CHARS]
    over220 = [i + 1 for i, x in enumerate(lens) if x > J1_BLOCK_CHARS]

    for idx in over220:
        violations.append({
            "type": "J1_single_overlong",
            "message": f"段 {idx} 单段 {lens[idx - 1]} 字 > {J1_BLOCK_CHARS}",
            "length": lens[idx - 1],
        })

    # ---- J2: 长段连片（重叠窗口合并为一个 dense run）----
    # 规则不变（任意连续 3 段每段 >70 且窗口内最长段 >120），只是不再让**一个**
    # 长段因为窗口逐格滑动而被重复计数 2-3 次（见模块 docstring 的实测案例）。
    hit_windows = [
        i for i in range(len(lens) - 2)
        if all(x > J2_MIN_EACH for x in lens[i:i + 3]) and max(lens[i:i + 3]) > J2_WALL_CHARS
    ]
    runs = []  # 连续命中的窗口合并：窗口 i..j 覆盖段落 i+1 .. j+3
    for i in hit_windows:
        if runs and i == runs[-1][1] + 1:
            runs[-1][1] = i
        else:
            runs.append([i, i])
    dense_runs = []
    for start, end in runs:
        # 窗口 start 覆盖段 start+1..start+3；窗口 end 覆盖段 end+1..end+3
        seg = lens[start:end + 3]
        dense_runs.append(f"[{start+1}-{end+3}]({'/'.join(str(x) for x in seg)}字,最长{max(seg)})")
        violations.append({
            "type": "J2_long_chain",
            "message": (
                f"段 {start+1}-{end+3} 连续 {len(seg)} 段每段>{J2_MIN_EACH} 且含 {J2_WALL_CHARS}+ 墙"
                f"（{'/'.join(str(x) for x in seg)}字）"
            ),
            "lengths": seg,
        })

    # ---- J3: 信息层堆叠 ----
    # 规则与 detect_internal 一致：段长>120 且 动作/心理/规则/因果 ≥3 类同段
    for i, p in enumerate(paras):
        if lens[i] <= J3_MIN_CHARS:
            continue
        layers = 0
        if any(a in p for a in _ACTION_WORDS):
            layers += 1
        if _MIND_PATTERN.search(p):
            layers += 1
        if _RULE_PATTERN.search(p):
            layers += 1
        if _CAUSE_PATTERN.search(p):
            layers += 1
        if layers >= J3_MIN_LAYERS:
            violations.append({
                "type": "J3_info_stack",
                "message": f"段 {i+1} {lens[i]}字 信息层堆叠 命中{layers}类（动作/心理/规则/因果）",
                "length": lens[i],
                "layers": layers,
            })

    # ---- WARNING 级：>160 字段（160-220 是写作者契约里的 warning 区间）----
    if over160 and not violations:  # 只有无 BLOCK 时才单独报 WARNING
        warnings.append({
            "type": "J1_warning",
            "message": (
                f">{J1_WARN_CHARS}字段落 {len(over160)} 处（第 {','.join(str(i) for i in over160[:8])} 段；"
                f"{J1_WARN_CHARS}-{J1_BLOCK_CHARS} 为 warning 区间，>{J1_BLOCK_CHARS} 才 BLOCK）"
            ),
        })

    if split_mode == "line":
        warnings.append({
            "type": "P_fmt_single_newline",
            "message": "本章为单换行分段（短篇协议）；长篇协议要求段间空行（\\n\\n）。已按行切段判定，建议归一正文格式",
        })

    max_len = max(lens) if lens else 0
    avg_len = sum(lens) / len(lens) if lens else 0

    result = {
        "ok": len(violations) == 0,
        "returncode": 2 if violations else 0,
        "stats": {
            "paragraphs": len(paras),
            "max_chars_per_paragraph": max_len,
            "avg_chars_per_paragraph": round(avg_len, 1),
            "split_mode": split_mode,
        },
        "violations": violations,
        "warnings": warnings,
        "dense_runs": dense_runs,
    }
    return result


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "段落节奏阻断门（移植 detect_internal I-J 并校准："
            f"J1 >{J1_BLOCK_CHARS} BLOCK / >{J1_WARN_CHARS} WARNING，J2/J3 BLOCK）"
        )
    )
    ap.add_argument("--prose", type=Path, required=True, help="正文文件路径")
    ap.add_argument("--json", action="store_true", help="输出 JSON 格式")
    args = ap.parse_args()

    result = check(args.prose)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if result.get("returncode") == -1:
            print(f"ERROR: {result.get('error', 'unknown')}", file=sys.stderr)
            return -1

        s = result.get("stats") or {}
        mode_note = "（单换行分段）" if s.get("split_mode") == "line" else ""
        print(f"[paragraph_gate] 段落数 {s.get('paragraphs', 0)}  最长 {s.get('max_chars_per_paragraph', 0)}字  均 {s.get('avg_chars_per_paragraph', 0)}字{mode_note}")

        for w in result.get("warnings", []):
            print(f"  [WARN] {w['message']}")

        if result["ok"]:
            print("[paragraph_gate] PASS — 无 BLOCK 级 I-J 硬坏形态")
            return 0

        print(f"[paragraph_gate] BLOCK — 发现 {len(result['violations'])} 处 I-J fail:")
        for v in result["violations"]:
            print(f"  [{v['type']}] {v['message']}")

    return result["returncode"]


if __name__ == "__main__":
    raise SystemExit(main())
