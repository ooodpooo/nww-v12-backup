#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zhuque_preflight.py — NWW-V12 去AI味预检（deai-promax 校准版）

蒸馏自 ~/.workbuddy/skills/deai-promax（朱雀五维启发式），针对 NWW-V12 做四项校准：
  C1. 引号口径统一：ASCII/弯引号一律计痕，目标态=直角引号「」『』；--fix 可自动规整
  C2. 母题白名单：≥8字重复串出现在 ≥2 个不同章节 → 判定为作者声纹（refrain），
      从困惑度维扣除；仅单章内复现 → 仍计痕。另可手动补 <正文>/../zhuque_whitelist.txt
  C3. 词边界修正：逻辑词仅在后接标点（，。！？；）时计数，消灭"不过是/最后一格"类误伤
  C4. 词表去重：AI_PREF 与 LOGIC 两表 10 个重叠词只计逻辑维；"如同"按 B 类不改动清单放行

维度（0-100，越高越像 AI）：突发性 / 困惑度 / 词汇分布 / 标点痕迹 / 逻辑明线
分级：composite ≤15 安全区 | 16-40 轻处理（引号/套词/个别词）| >40 深度改写
退出码（流水线契约，nww-local 可直接以 shell 调用承接）：
    0 = 全部章节处于安全区
    1 = 存在轻处理章节（无深度项）
    2 = 存在深度改写章节（需人工/deeprewrite）

用法：
    python zhuque_preflight.py <章节.md>            # 单章评分
    python zhuque_preflight.py <正文目录>/          # 整书扫描（先学母题再逐章评）
    python zhuque_preflight.py <路径> --fix         # 额外输出 *_去AI版.md（不覆盖原稿）
    python zhuque_preflight.py <路径> --json
"""

import sys
import json
import re
from collections import Counter
from pathlib import Path

# ────────────── 词典（蒸馏自 deai-promax banned-words / patterns D 类） ──────────────
AI_PREF_WORDS = [
    "此外", "然而", "因此", "总而言之", "事实上", "值得注意",
    "更关键", "更要命", "换句话说", "首先", "其次", "最后", "在这个过程中",
    "在此基础上", "至关重要", "关键性的", "不可或缺的", "深远影响", "标志着",
    "见证了", "彰显了", "充分体现了", "举足轻重", "里程碑", "深远意义",
    "行业报告显示", "观察者指出", "专家认为", "研究表明", "业内人士认为",
    "未来可期", "前景广阔", "充满希望", "毫无疑问", "综上所述", "本质上",
    "归根结底", "简单来说", "赋能", "闭环", "抓手", "落地", "重塑", "深耕",
    "洞察", "剖析", "解构", "聚焦", "驱动", "构建", "打造", "助力", "践行",
    "不由得", "霎时间", "殊不知", "良久", "恍然大悟", "稳稳", "猛地", "死死",
    "狠狠", "仿佛", "不仅", "而且",
]
# C4: 与 LOGIC 重叠的词不再计入 PREF
LOGIC_WORDS = [
    "因此", "然而", "此外", "与此同时", "首先", "其次", "最后", "所以",
    "于是", "并且", "而且", "但是", "不过", "由此可见", "总而言之", "总之",
    "换言之", "换句话说",
]
PREF_SET = set(w for w in AI_PREF_WORDS if w not in LOGIC_WORDS)

# 网文禁用套词（patterns.md D 类 + banned-words 四类；报告用，不进评分）
NOVEL_CLICHES = [
    "不由得", "霎时间", "殊不知", "良久", "此刻心中暗想", "心中暗道", "就在这时",
    "顿时", "瞬间", "恍然大悟", "微微一笑", "轻轻叹了口气", "嘴角微微上扬",
    "眼底闪过一丝", "眼底划过一丝", "喉结滚动", "周身气场", "心中五味杂陈",
    "气氛瞬间凝滞", "声音很平", "如沐春风", "梨花带雨",
]

PROMPT_COLON_HINTS = [
    "核心是", "关键在于", "原因在于", "原因如下", "结论是", "本质上",
    "换句话说", "一句话总结", "答案是", "问题是",
]

SENT_SPLIT = re.compile(r"[。！？；;\n]+")
NON_CN = re.compile(r"[^\u4e00-\u9fff]+")
# C3: 逻辑词仅在后接标点时计数（句首/顿号后起连接作用）
LOGIC_PAT = re.compile("|".join(re.escape(w) + r"(?=[，。！？；])" for w in LOGIC_WORDS))

SKIP_MARKERS = ("去AI版", "backup", "_bak", "重写90分")


def read_text(path):
    return Path(path).read_text(encoding="utf-8")


def body_text(raw):
    """剔除标题行与章尾系统标记行，只给正文参与评分。"""
    lines = []
    for ln in raw.split("\n"):
        t = ln.strip()
        if not t or t.startswith("#") or t.startswith("【"):
            continue
        lines.append(ln)
    return "\n".join(lines)


def cn_len(s):
    return len(NON_CN.sub("", s))


def variance(xs):
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    return sum((x - m) ** 2 for x in xs) / n


def ngrams_of(text, n):
    t = NON_CN.sub("", text)
    return Counter(t[i:i + n] for i in range(len(t) - n + 1))


def max_repeats(text, n=8):
    """找出所有最大重复子串（≥n字，出现≥2次）。
    相邻窗口归并：一个重复12字的长串只算 1 条，不再切出 5 个错位窗口。"""
    t = NON_CN.sub("", text)
    if len(t) < n * 2:
        return []
    cnt = Counter(t[i:i + n] for i in range(len(t) - n + 1))
    repeated = {g for g, c in cnt.items() if c >= 2}
    if not repeated:
        return []
    # 每个重复 8 字串的位置
    pos = {}
    for i in range(len(t) - n + 1):
        g = t[i:i + n]
        if g in repeated:
            pos.setdefault(g, []).append(i)
    maximals = set()
    for g, ps in pos.items():
        for a, b in zip(ps, ps[1:]):
            i, j, L = a, b, n
            while i > 0 and j > 0 and t[i - 1] == t[j - 1]:
                i -= 1; j -= 1; L += 1
            while i + L < len(t) and j + L < len(t) and t[i + L] == t[j + L]:
                L += 1
            maximals.add(t[i:i + L])
    return sorted(maximals, key=len, reverse=True)


def exempt_repeat(r, whitelist):
    """重复串与白名单任一条互含或重叠≥6字 → 豁免（作者声纹）。"""
    for w in whitelist:
        if w in r or r in w:
            return True
        if len(w) >= 6:
            for k in range(len(r) - 5):
                if r[k:k + 6] in w:
                    return True
    return False


def learn_motifs(texts, n=8, min_chapters=2):
    """C2: 作者声纹白名单。两条来源：
    a) 跨章复现：n-gram 出现在 ≥min_chapters 个不同章节（refrain 母题）
    b) 章内复现：单章出现 ≥3 次（B类清单：反复写全称/职称，人类比AI更常做）
    """
    per_ch = []
    for t in texts:
        grams = set(g for g, c in ngrams_of(t, n).items() if c >= 1)
        per_ch.append(grams)
    joint = Counter()
    for grams in per_ch:
        joint.update(grams)
    whitelist = set(g for g, c in joint.items() if c >= min_chapters)
    for t in texts:
        for g, c in ngrams_of(t, n).items():
            if c >= 3:
                whitelist.add(g)
    return whitelist


def load_whitelists(book_dir):
    """加载手动白名单 zhuque_whitelist.txt + 自动学习结果 zhuque_whitelist_auto.txt"""
    grams = set()
    for name in ("zhuque_whitelist.txt", "zhuque_whitelist_auto.txt"):
        p = Path(book_dir) / name
        if not p.exists():
            continue
        for ln in p.read_text(encoding="utf-8").split("\n"):
            ln = ln.strip()
            if ln and not ln.startswith("#") and len(ln) >= 4:
                grams.add(NON_CN.sub("", ln))
    return grams


def save_whitelist(book_dir, grams):
    p = Path(book_dir) / "zhuque_whitelist_auto.txt"
    p.write_text(
        "# 自动生成：跨章复现≥2章 或 单章复现≥3次 的≥8字串（作者声纹，评分时豁免）\n"
        + "\n".join(sorted(grams)) + "\n",
        encoding="utf-8")
    return p


def normalize_quotes(text):
    """C1: ASCII/弯引号 → 直角引号（逐行开合交替）。返回 (新文本, 替换数)。"""
    n_before = sum(text.count(c) for c in "\"'\u201c\u201d\u2018\u2019")
    out = []
    for ln in text.split("\n"):
        buf, dq, sq = [], False, False
        for ch in ln:
            if ch in "\"\u201c\u201d":
                buf.append("「" if not dq else "」"); dq = not dq
            elif ch in "'\u2018\u2019":
                buf.append("『" if not sq else "』"); sq = not sq
            else:
                buf.append(ch)
        out.append("".join(buf))
    return "\n".join(out), n_before


def detect(text, whitelist=frozenset()):
    raw = text
    text = text.replace("\r", "")
    sents = [p.strip() for p in SENT_SPLIT.split(text) if p.strip()]
    paras = [p for p in text.split("\n") if p.strip()]
    total_cn = cn_len(text)

    # 1) 突发性
    lens = [cn_len(s) for s in sents]
    var = variance(lens)
    burst = 5 if var >= 25 else (95 if var <= 5 else max(0, min(100, int((25 - var) / 25 * 100))))

    # 2) 困惑度（最大重复串归并后，扣除白名单母题）
    reps = [r for r in max_repeats(text) if not exempt_repeat(r, whitelist)]
    r12 = sum(1 for r in reps if len(r) >= 12)
    r8 = len(reps)
    ppl = min(100, r12 * 25 + max(0, r8 - r12) * 6)

    # 3) 词汇分布（已去重）
    hits = [(w, raw.count(w)) for w in sorted(PREF_SET) if raw.count(w)]
    density = sum(c for _, c in hits) / total_cn * 1000 if total_cn else 0
    vocab = min(100, int(density * 10))

    # 4) 标点（输入应已规整引号；此处 ASCII/弯引号仍计痕作为回归检查）
    emdash = raw.count("——")
    emd = emdash / total_cn * 1000 if total_cn else 0
    pc = 0
    for h in PROMPT_COLON_HINTS:
        idx = 0
        while True:
            i = raw.find(h, idx)
            if i == -1:
                break
            if "：" in raw[i + len(h): i + len(h) + 5] or ":" in raw[i + len(h): i + len(h) + 5]:
                pc += 1
            idx = i + len(h)
    eq = sum(raw.count(c) for c in "\"'\u201c\u201d\u2018\u2019")
    punct = min(100, int(emd * 25 + pc * 12 + eq / max(1, total_cn / 1000) * 4))

    # 5) 逻辑明线（C3 词边界）+ 段落同构
    logic_hits = len(LOGIC_PAT.findall(raw))
    ld = logic_hits / total_cn * 1000 if total_cn else 0
    iso = 0
    for p in paras:
        ps = [q for q in SENT_SPLIT.split(p) if q.strip()]
        for a, b in zip(ps, ps[1:]):
            ca, cb = a.count("，") + a.count(","), b.count("，") + b.count(",")
            if ca == cb and ca >= 2 and abs(cn_len(a) - cn_len(b)) <= 4:
                iso += 1
    logic = min(100, int(ld * 12 + iso * 8))

    composite = int(burst * .28 + ppl * .24 + vocab * .22 + punct * .13 + logic * .13)
    return {
        "total_cn": total_cn, "sent_var": round(var, 2),
        "repeat12": r12, "repeat8": r8,
        "top_repeats": reps[:8],
        "ai_word_hits": hits, "ai_word_density": round(density, 2),
        "emdash": emdash, "prompt_colon": pc, "bad_quotes": eq,
        "logic_hits": logic_hits, "para_iso": iso,
        "scores": {"burstiness": burst, "perplexity": ppl, "vocab": vocab,
                   "punct": punct, "logic": logic},
        "composite": composite,
    }


def cliches_of(raw):
    return [(w, raw.count(w)) for w in NOVEL_CLICHES if raw.count(w)]


def grade(c):
    return "✅ 安全区" if c <= 15 else ("🟡 轻处理" if c <= 40 else "🔴 深度改写")


def report_ch(name, d, cl, extra=""):
    s = d["scores"]
    print(f"\n── {name} {extra}")
    print(f"  五维: 突发{s['burstiness']:>3} 困惑{s['perplexity']:>3} 词汇{s['vocab']:>3} "
          f"标点{s['punct']:>3} 逻辑{s['logic']:>3}  │  综合 {d['composite']}/100 {grade(d['composite'])}")
    notes = []
    if d["bad_quotes"]:
        notes.append(f"非直角引号×{d['bad_quotes']}（--fix 可规整）")
    if d["emdash"] > 2:
        notes.append(f"破折号×{d['emdash']}")
    if d["ai_word_hits"]:
        notes.append("偏好词: " + "、".join(f"{w}×{c}" for w, c in d["ai_word_hits"][:6]))
    if d["repeat8"] and d["scores"]["perplexity"] >= 40:
        tops = "；".join(d.get("top_repeats", [])[:4])
        notes.append(f"单章重复串 {d['repeat8']} 条（已扣母题白名单）：{tops}")
    if cl:
        notes.append("套词: " + "、".join(f"{w}×{c}" for w, c in cl))
    for n in notes:
        print(f"  • {n}")


def main():
    args = sys.argv[1:]
    as_json = "--json" in args
    do_fix = "--fix" in args
    paths = [a for a in args if not a.startswith("--")]
    if not paths:
        print(__doc__)
        sys.exit(1)
    target = Path(paths[0])

    if target.is_dir():
        files = sorted(f for f in target.glob("*.md")
                       if not any(m in f.name for m in SKIP_MARKERS))
        if not files:
            print("目录内无可评章节"); sys.exit(1)
        texts = [read_text(f) for f in files]
        whitelist = learn_motifs(texts) | load_whitelists(target.parent)
        save_whitelist(target.parent, whitelist)
        if not as_json:
            print(f"整书预检：{len(files)} 章，母题白名单 {len(whitelist)} 条"
                  f"（跨章复现≥2章 或 单章复现≥3次的≥8字串，已持久化到 zhuque_whitelist_auto.txt）")
        rows = []
        for f, raw in zip(files, texts):
            fixed, nq = normalize_quotes(raw)
            if do_fix and nq:
                out = f.with_name(f.stem + "_去AI版.md")
                out.write_text(fixed, encoding="utf-8")
            d = detect(body_text(fixed), whitelist)
            cl = cliches_of(fixed)
            rows.append((f.name, d, cl, nq))
        if as_json:
            print(json.dumps([{"file": n, **d, "cliches": cl, "quotes_fixed": q}
                              for n, d, cl, q in rows], ensure_ascii=False, indent=2))
            return
        for name, d, cl, nq in rows:
            report_ch(name, d, cl, f"(引号{nq}处待规整)" if nq and not do_fix else "")
        n_safe = sum(1 for _, d, _, _ in rows if d["composite"] <= 15)
        n_mid = sum(1 for _, d, _, _ in rows if 15 < d["composite"] <= 40)
        n_deep = sum(1 for _, d, _, _ in rows if d["composite"] > 40)
        print(f"\n════ 总览：{len(rows)} 章 ｜ ✅安全 {n_safe} ｜ 🟡轻处理 {n_mid} ｜ 🔴深度 {n_deep}")
        deep = [n for n, d, _, _ in rows if d["composite"] > 40]
        if deep:
            print("建议深度改写：" + "、".join(deep))
        # 退出码契约：2=有深度项 1=有轻处理项 0=全安全
        sys.exit(2 if deep else (1 if n_mid else 0))
    else:
        raw = read_text(target)
        # 白名单可能在正文目录或书根目录（书根 = 正文.parent）
        whitelist = set()
        if target.parent.name == "正文":
            whitelist = load_whitelists(target.parent) | load_whitelists(target.parent.parent)
        fixed, nq = normalize_quotes(raw)
        if do_fix and nq:
            out = target.with_name(target.stem + "_去AI版.md")
            out.write_text(fixed, encoding="utf-8")
            print(f"已输出规整版: {out.name}（原稿未动）")
        d = detect(body_text(fixed), whitelist)
        cl = cliches_of(fixed)
        if as_json:
            print(json.dumps({"file": target.name, **d, "cliches": cl},
                             ensure_ascii=False, indent=2))
            return
        report_ch(target.name, d, cl, f"(引号{nq}处待规整)" if nq else "")
        sys.exit(0 if d["composite"] <= 15 else (1 if d["composite"] <= 40 else 2))


if __name__ == "__main__":
    main()
