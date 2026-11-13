#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nww_chapter_tail_gate.py — 章尾动力类型轮换闸（2026-10-11 新建）

## 为什么要有这个

`skills/nww-novel-skill/skills/rhythm/SKILL.md` 早就规定了章尾动力类型轮换：
    「给本章定章尾类型 A 代价结算 / B 认知变化 / C 外部压力 / D 关系变化；
      约束 last_3 禁 AA、AAA（允许 ABA、ACB、DAC）」

但 2026-10-11 实测发现：**这条规则没有任何代码实现**——
自动写章链不执行它，唯一相关的 `nww_sequence_audit.py` 也只统计、不判定。
结果：《钦天监里的统计师》**27/27 = 100%** 章末都是 A 类型（「【天机反噬：N】」）。

本脚本把这条规则做成**确定性、零 LLM** 的可执行闸。

## 用法

    python3 nww_chapter_tail_gate.py <book_root>                 # 人类可读报告
    python3 nww_chapter_tail_gate.py <book_root> --json          # 机器可读

## 设计约束（项目硬要求）

- **纯扩展**：只读文件系统，不改任何正文/tracking/bible。
- **零 LLM**：全正则确定性判定，可复现。
- **不阻断生产**：本脚本是审计工具，不自动挂进写章硬闸。
  如需接入，由调用方（pipeline / CI）依据退出码决定。
- 退出码：0 = 无违规；1 = 存在违规；2 = 用法/输入错误。

## 判定口径（可调整，改这里要同步改 tests）

章尾类型取自每章**末尾 TAIL_CHARS 字**（默认 400），按优先级判定：
  A 代价结算 —— 出现金手指/代价/进度标记（【天机反噬：N】、【烟火值：N】、代价、反噬…）
  D 关系变化 —— 出现关系动词（结盟/决裂/认/认作/托付/许诺/断交…）
  C 外部压力 —— 出现压力/威胁/命令（通牒/限期/勒令/否则/来人/传唤/围…）
  B 认知变化 —— 出现认知动词（明白/意识到/才知道/原来/忽然懂/醒悟…）
  U 未分类   —— 以上均未命中
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

TAIL_CHARS = int(os.environ.get("NWW_TAIL_GATE_CHARS", "400") or 400)

# —— 章尾类型正则（按优先级从上到下匹配）——
# A：代价结算 / 金手指结算（本项目实测最常见的"套路尾"）
_A_RE = re.compile(
    r"【\s*(?:天机反噬|烟火值|代价|反噬|进度|结算)[^】]{0,20}】"
    r"|(?:天机反噬|烟火值)\s*[:：]\s*\d"
    r"|代价|反噬|损耗|折损|燃烧寿元|扣除"
)
# D：关系变化
_D_RE = re.compile(r"结盟|结拜|决裂|断交|认作|认了|拜师|收徒|托付|许诺|许下|和好|翻脸|誓约|约为")
# C：外部压力
_C_RE = re.compile(r"通牒|限期|勒令|否则|来人|传唤|围住|包围|追杀|通缉|问罪|押送|最后期限|不得再|封了|查抄")
# B：认知变化（「明白」单独成词即可，不必带"了/过来"）
_B_RE = re.compile(r"明白|意识到|才知道|才明白|原来是|忽然懂|醒悟|想通|终于懂|看懂了|看穿|认清")

TYPE_ORDER = [("A", _A_RE), ("D", _D_RE), ("C", _C_RE), ("B", _B_RE)]
TYPE_DESC = {"A": "代价结算", "B": "认知变化", "C": "外部压力", "D": "关系变化", "U": "未分类"}

_CH_RE = re.compile(r"^第(\d+)章")


# ★★ 2026-10-12 重大修正：系统方括号计数器必须先剥离再分类。
# 来源：bible.json 的 cost_system（device="天机反噬" / counter_format="天机反噬：{n}"）。
# 该计数器由系统**必然**写在章尾最后一行，与作者的章尾动力选择无关。
# 不剥离 → 【天机反噬：N】必然命中 _A_RE → 全书 100% 判 A。
# 2026-10-11 报出的「钦天监 27/27 = 100% A 类型」就是这个测量错误，已作废。
# 剥离后实测分布：U 15 / A 8 / B 3 / C 1（序列 UUAUUBBABUUUUAAAAAUUAUCUUUU）。
_COUNTER_RE = re.compile(r"【[^】]{0,20}[：:][^】]{0,12}】")


def strip_system_counters(text: str) -> str:
    """剥离系统写入的方括号计数器（【XX：N】），只保留叙事文本。"""
    return _COUNTER_RE.sub("", text or "").strip()


# —— 章尾钩子具体形态分类（2026-10-26 新增：解决 D8/F4 连续倒计时等钩子形态同质化盲区）——
# 动力类型（A/B/C/D）管"什么类型的账"，钩子形态（Morphology）管"用什么悬念手法断章"。
_MORPH_PATTERNS = [
    ("countdown", "倒计时/时限悬念", re.compile(
        r"(?:[一二两三四五六七八九十百\d]+\s*(?:日|天|时辰|刻钟|月|年)(?:后|内|之内|之后))|倒计时|期限|子时|明日|天亮之前|第[一二三四五六七八九十\d]+格"
    )),
    ("revelation_twist", "真相揭露/认知反转", re.compile(
        r"原来|根本不是|绝非|竟然是|才是|真相|看清|认出|名字|字迹|刻字|小字|浮出|浮现|血指画下|账本上|多了一行|少了一页|死人|活人"
    )),
    ("decision_bet", "决断押注/亮底牌", re.compile(
        r"赌|立字据|画押|掀桌|开价|成交|接下|应下|走一趟|亲自去|开炉|挂牌|选死还是选活|自己定"
    )),
    ("relation_shift", "关系质变/对峙留白", re.compile(
        r"结盟|决裂|拜师|收徒|托付|许诺|看着他|看着她|笑了|转身|留下一句|问道|反问|谁准你们"
    )),
    ("crisis_arrival", "危机/强敌临头", re.compile(
        r"破门|包围|围住|追杀|通缉|传唤|杀机|拔刀|亮剑|血泊|中毒|死局|封锁|查抄|撞开城门|格杀勿论|就地正法"
    )),
    ("anomaly_hook", "反常异变/未知悬念", re.compile(
        r"异响|裂纹|震动|亮起|熄灭|消失|凭空|古怪|不对劲|已经运到|化铜大炉|[？?]"
    )),
]


def classify_hook_morphology(tail: str, strip_counters: bool = True) -> str:
    """判定章尾最后 150 字的具体钩子形态（优先匹配收尾最后一句，再回退最后 150 字窗口）。"""
    t = strip_system_counters(tail) if strip_counters else (tail or "")
    focus = t[-150:] if len(t) > 150 else t
    sents = [s.strip() for s in re.split(r"[\n]+", focus) if s.strip()]
    last_para = sents[-1] if sents else focus
    for mcode, _mdesc, mrx in _MORPH_PATTERNS:
        if mrx.search(last_para):
            return mcode
    for mcode, _mdesc, mrx in _MORPH_PATTERNS:
        if mrx.search(focus):
            return mcode
    return "plain_close"


def classify_tail(tail: str, strip_counters: bool = True) -> str:
    """判定章尾动力类型。返回 A/B/C/D/U。

    strip_counters=True（默认）时先剥离系统计数器，避免把必然出现的
    数值标记误判为"A 代价结算"。
    """
    t = strip_system_counters(tail) if strip_counters else (tail or "")
    for code, rx in TYPE_ORDER:
        if rx.search(t):
            return code
    return "U"


def _chapter_no(path: Path) -> int | None:
    m = _CH_RE.match(path.name)
    return int(m.group(1)) if m else None


def scan_book(book: Path, tail_chars: int = TAIL_CHARS,
              upto: int | None = None) -> dict:
    """扫描一本书的正文，逐章判定章尾类型并找出轮换违规。

    upto：只看 <= upto 的章（写章时只需判定 last_3，不必全本重扫）。
    """
    body = book / "正文"
    if not body.is_dir():
        raise SystemExit(2)
    chapters: dict[int, Path] = {}
    for p in body.rglob("*.md"):
        if p.name.startswith("_") or "_review" in str(p):
            continue
        n = _chapter_no(p)
        if n is not None and (upto is None or n <= upto):
            chapters[n] = p  # 同章号取最后一个（版本优先）

    rows = []
    for n in sorted(chapters):
        text = chapters[n].read_text(encoding="utf-8")
        text = re.sub(r"^#.*?$", "", text, flags=re.M).strip()
        tail = text[-tail_chars:] if len(text) > tail_chars else text
        narrative = strip_system_counters(tail)
        rows.append({"chapter": n, "file": str(chapters[n].relative_to(book)),
                     "type": classify_tail(tail),
                     "hook_morphology": classify_hook_morphology(tail),
                     "tail": (narrative or tail)[-80:],
                     "counter_stripped": narrative != tail})

    # last_3 禁 AA / AAA —— 按「连续同类型段（run）」计，一段只报一次。
    # （2026-10-11 修正：原滑动窗口会把一段连续 27 章全 A 重复报 25 次，噪声淹没信号。）
    violations = []
    i = 0
    while i < len(rows):
        t = rows[i]["type"]
        j = i
        while j + 1 < len(rows) and rows[j + 1]["type"] == t:
            j += 1
        run_len = j - i + 1
        if t != "U" and run_len >= 2:
            violations.append({
                "kind": "AAA" if run_len >= 3 else "AA",
                "chapters": [r["chapter"] for r in rows[i:j + 1]],
                "type": t,
                "run_len": run_len,
            })
        i = j + 1

    dist: dict[str, int] = {}
    for r in rows:
        dist[r["type"]] = dist.get(r["type"], 0) + 1

    # 2026-10-26 新增：章尾钩子形态（hook_morphology）全书分布与连续同形态审计（D8/F4）
    morph_dist: dict[str, int] = {}
    for r in rows:
        m = r.get("hook_morphology", "plain_close")
        morph_dist[m] = morph_dist.get(m, 0) + 1
    morph_violations = []
    mi = 0
    while mi < len(rows):
        mt = rows[mi].get("hook_morphology", "plain_close")
        mj = mi
        while mj + 1 < len(rows) and rows[mj + 1].get("hook_morphology", "plain_close") == mt:
            mj += 1
        mrun = mj - mi + 1
        if mt != "plain_close" and mrun >= 2:
            morph_violations.append({
                "kind": "MORPH_REPEAT",
                "morphology": mt,
                "chapters": [r["chapter"] for r in rows[mi:mj + 1]],
                "run_len": mrun,
            })
        mi = mj + 1
    if len(rows) >= 5:
        for mcode, mcnt in morph_dist.items():
            if mcode != "plain_close" and (mcnt / len(rows)) > 0.40:
                morph_violations.append({
                    "kind": "MORPH_DOMINANCE_OVER_40PCT",
                    "morphology": mcode,
                    "count": mcnt,
                    "ratio": round(mcnt / len(rows), 2),
                })

    unclassified = dist.get("U", 0)
    return {
        "book": book.name,
        "chapter_count": len(rows),
        "type_distribution": dist,
        "morphology_distribution": morph_dist,
        "morphology_violations": morph_violations,
        "aaa_count": sum(1 for v in violations if v["kind"] == "AAA"),
        "aa_count": sum(1 for v in violations if v["kind"] == "AA"),
        "violations": violations,
        "rows": rows,
        # 分类器自身的可信度：U 占比高说明正则覆盖不足，结论应打折看待
        "unclassified_rate": (unclassified / len(rows)) if rows else 0.0,
        "counters_stripped": sum(1 for r in rows if r.get("counter_stripped")),
        "note": ("章尾类型已剥离系统方括号计数器（bible cost_system）后再判定；"
                 "同时输出 hook_morphology 钩子形态分布与连续同型审计。"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="章尾动力类型轮换闸（确定性、零 LLM、只读）")
    ap.add_argument("book_root")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--tail-chars", type=int, default=TAIL_CHARS)
    ap.add_argument("--upto", type=int, default=None,
                    help="只看 <= upto 的章（写章时判定 last_3 用）")
    args = ap.parse_args()

    book = Path(args.book_root)
    if not book.is_dir():
        print(f"[ERR] 书目录不存在: {book}", file=sys.stderr)
        return 2
    try:
        res = scan_book(book, args.tail_chars, upto=args.upto)
    except SystemExit:
        print(f"[ERR] 未找到 正文/ 目录: {book}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 1 if res["violations"] else 0

    total = res["chapter_count"]
    print(f"书：{res['book']}    章节数：{total}")
    print("章尾类型分布：" + "  ".join(
        f"{k}({TYPE_DESC.get(k, k)})={v} ({v / total:.0%})" for k, v in sorted(res["type_distribution"].items())
    ))
    print(f"违规：AAA（连续3章同类型）={res['aaa_count']}    AA（连续2章同类型）={res['aa_count']}")
    if res["violations"]:
        print("\n违规明细（前 20 条）：")
        for v in res["violations"][:20]:
            print(f"  [{v['kind']}] 章{v['chapters']} 全为 {v['type']}({TYPE_DESC.get(v['type'], '')})")
    else:
        print("\n无轮换违规。")
    return 1 if res["violations"] else 0


if __name__ == "__main__":
    sys.exit(main())
