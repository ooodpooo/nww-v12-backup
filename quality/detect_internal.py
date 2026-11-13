#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
内部门禁 · 确定性检测（I-A ~ I-J）
==================================

# S5 命名消歧：此处 I-A~I-J 是确定性检测器（文本分析），与 nww_review_cycle.py 的
# REVIEW_DIMENSIONS（LLM 打分）同名不同义。仅 I-G（解释性独白）和 I-H（标题核验）语义重合。
# 映射表见 docs/评分体系融合方案_20260913.md。
90分评分法「内部维度」的确定性检测层。纯程序、模型无关、可复现。
输出每项检测的归一化得分 0-100（越高越好）与 warning/info 明细。

检测项（对应 quality/90分评分法_设计方案.md 第三节）：
  I-A 段落等长CV        cv<0.15 过均匀 → warning
  I-B 套话密度          似乎/可能/或许等 >3次/千字 → warning
  I-C 公式化转折        同一非白名单转折词≥3次 → warning（"不过/然而"已白名单）
  I-D 列表式开头        ≥3句连以相同开头 → info
  I-E AI模板情绪词密度   模板词（瞳孔地震/难以置信/嘴角上扬等）>1.5次/千字 → warning；叙事性高频词白名单不计
  I-F 句长节奏          通篇超短句成串像提纲 / 机械长短交替
  I-G 解释性独白        Phase2-B 硬约束：为补人物因果的内心独白/解释复述 → FAIL
  I-H 标题行核验        正文首行必须为 `# 第N章 章名`（落盘确定性写入，禁漏写/格式不符）→ FAIL
  I-J 段落密度门禁      防"过密"：单段>80字 warning/>160字 FAIL；连续3段均>70字 FAIL；
                        单段>120字且动作/心理/规则/因果≥3层堆叠 FAIL（2026-09-09 新增）

评分口径（0-100，越高越好）：
  每项 100 起扣，命中 warning 扣 25，命中 info 扣 10（I-E 按密度超幅连续扣）。
  这是「检测得分」；内部扣分系数由 run_backtest 按扣分点清单合成。

I-G · 解释性独白（Phase2-B 新增，2026-08-28）
  背景：Character Spine 工程化的硬约束——"不得用额外内心独白/解释性文字补人物因果"。
  盲评暴露的副作用样本：稿4/稿2/稿6"作者解释最重/后半夜心理回溯过长/重复确认"。
  检测策略（模型无关）：
    G1) 纯内心解释段：以解释/复盘类引导词开头的段落，段内无引号对白且无动作动词，
         段长超过阈值 → FAIL（"解释性独白"）；
    G2) 同义反复因果簇：同一个"因果复盘句式"（含 持平/不能再犯/我要说/我决定/同一道题 等
        关键token）在正文重复 ≥N 处 → FAIL（"重复确认"）。
  判定级：FAIL（Phase2-B 硬约束，非 warning），扣分按 fail 规则（见 run_backtest 合成）。
"""
import json
import re
import sys
import os
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from objective_scorer import count_cjk, AI_WORDS  # noqa: E402

HEDGE_WORDS = ["似乎", "可能", "或许", "大概", "某种程度上", "一定程度上", "在某种意义上"]
# I-C 白名单（第一轮校准实证，2026-08-27）：
#   "不过/然而" 在头部作品为作者风格节奏（剑来"不过x26"为陈平安式 OS），
#   不计入公式化转折统计；保留其余转折词检测。
TRANSITION_WORDS = ["与此同时", "另一方面", "尽管如此", "话虽如此", "但值得注意的是"]

# ---------------------------------------------------------------------------
# I-E 词表拆分（第二轮校准实证，2026-08-27）
# 单纯看 AI 词密度总和分不开名作与 AI 水稿（道诡4.6/玄鉴4.1/AI负4.8 几乎相等）。
# 根因：名作高频的是叙事性常用词（忽然/顿时/缓缓…），AI 水稿高频的是模板化情绪词
#        （瞳孔地震/难以置信/嘴角上扬…）。故拆两类：
#   - I-E_NARRATIVE（叙事性高频，白名单）：名作风格，不计入 I-E 密度
#   - I-E_TEMPLATE（模板化情绪词）：AI 水稿主流，计入 I-E 密度
# ---------------------------------------------------------------------------
I_E_NARRATIVE = [
    "忽然", "顿时", "缓缓", "轻轻", "仿佛", "似乎", "猛然", "瞬间", "不禁",
    "渐渐", "微微", "隐约", "神色", "目光", "片刻", "沉默", "淡淡", "不动声色",
    "暗自", "心头", "心底", "不由", "不由得", "恍然", "恍若", "默然", "悄然", "慢慢",
]
I_E_TEMPLATE = [
    "瞳孔地震", "难以置信", "倒吸一口凉气", "倒抽一口凉气", "嘴角微微上扬",
    "嘴角勾起", "嘴角上扬", "嘴角抽搐", "五味杂陈", "心头一紧", "呼吸一窒",
    "浑身一震", "虎躯一震", "娇躯一震", "凤眸微眯", "勾唇一笑", "冷哼一声",
    "睥睨", "傲然", "玩味", "兴味", "扯了扯嘴角", "神色阴晴不定", "眸色一沉",
    "眉头紧锁", "若有所思", "心头一凛", "深吸一口气", "眸光闪动", "眼神深邃",
    "涌上心头", "难以言喻", "无法形容", "眼底深处", "弥漫着", "充斥着",
]

# ---------------------------------------------------------------------------
# I-G 解释性独白（Phase2-B 硬约束，2026-08-28）
# 盲评副作用样本校准：稿4/稿2/稿6 被点"作者解释最重/心理回溯过长/重复确认"；
# baseline 稿1/3/5 的内心独白短促带决断、附着动作，应不误伤。
# G1 引导词：解释/复盘类"他X"开头（段首检索）；G2 因果复盘 token：同义反复句式簇。
# 引导词与 token 来自对 C4/C5/C6（injected）与 C1/C2/C3（baseline）的差异提取。
# ---------------------------------------------------------------------------
I_G_THINK_LEADS = [
    "他想起", "他明白", "他忽然觉得", "他忽然明白", "他告诉自己", "他清楚",
    "他怕", "他决定", "他合上眼", "他闭上眼", "他睁开眼", "他想了想",
    "他仍然觉得", "他开始明白", "他终于明白", "他心里明白", "他隐隐觉得",
    "他意识到", "他忽然意识到", "他忽然想通", "他慢慢明白", "他分明知道",
]
# 动作动词（段内若含动作/对话 → 非"纯内心解释段"；只看确定性动作，排除心理性动词）
I_G_ACTION_WORDS = [
    "走", "站", "坐", "推", "拉", "拿", "放", "搁", "抬", "举", "摊",
    "握", "捏", "捡", "擦", "按", "写", "抄", "递", "接", "倒", "喝", "笑",
    "喊", "叹", "摇头", "点头", "起身", "迈步", "转身",
]
# G2 因果复盘 token：核心病灶词（注入纲领词，重复 ≥6 次 = 过量复盘/重复确认）。
# 校准（2026-08-28）：baseline C3 仅 '当年'x1；injected C4(持平14/上辈7)、C5(持平12/上辈9)、
#   C6(持平17/上辈12) —— 单词 ≥6 可稳定分离，且保留"前史呈现 1-2 处"的合法空间。
I_G_RECAP_TOKENS = [
    "持平", "上辈子", "同一道题", "不能再犯", "不能再看", "我要说", "我决定",
    "再来一次", "重蹈", "旧路", "又一次",
]


def detect_i_a_paragraph_cv(text: str) -> dict:
    """I-A 段落等长CV。cv<0.15 → warning。"""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(paras) < 3:
        return {"score": 100, "status": "pass", "detail": "段落<3，不判"}
    lens = [len(p) for p in paras]
    mean = sum(lens) / len(lens)
    if mean == 0:
        return {"score": 100, "status": "pass", "detail": "空文本"}
    std = (sum((x - mean) ** 2 for x in lens) / len(lens)) ** 0.5
    cv = std / mean
    if cv < 0.15:
        return {"score": 75, "status": "warning", "detail": f"CV={cv:.3f}(<0.15) 段落过均匀"}
    return {"score": 100, "status": "pass", "detail": f"CV={cv:.3f}"}


def detect_i_b_hedge_density(text: str) -> dict:
    """I-B 套话密度。>3次/千字 → warning。"""
    cjk = count_cjk(text)
    if cjk == 0:
        return {"score": 100, "status": "pass", "detail": "空文本"}
    n = sum(text.count(w) for w in HEDGE_WORDS)
    density = n / cjk * 1000
    if density > 3:
        return {"score": 75, "status": "warning", "detail": f"{density:.1f}次/千字(>3)"}
    return {"score": 100, "status": "pass", "detail": f"{density:.1f}次/千字"}


def detect_i_c_formulaic_transition(text: str) -> dict:
    """I-C 公式化转折。同一转折词≥3次 → warning。"""
    hits = []
    for w in TRANSITION_WORDS:
        c = text.count(w)
        if c >= 3:
            hits.append(f"{w}x{c}")
    if hits:
        return {"score": 75, "status": "warning", "detail": "重复转折:" + ",".join(hits)}
    return {"score": 100, "status": "pass", "detail": "无重复转折"}


def detect_i_d_list_like(text: str) -> dict:
    """I-D 列表式开头。≥3句连以相同开头 → info。"""
    sents = [s.strip() for s in re.split(r"[。！？!?\n]", text) if len(s.strip()) > 2]
    max_consec = cur = 1
    prev = sents[0][:2] if sents else ""
    for s in sents[1:]:
        cur_prefix = s[:2]
        if cur_prefix == prev:
            cur += 1
            max_consec = max(max_consec, cur)
        else:
            cur = 1
        prev = cur_prefix
    if max_consec >= 3:
        return {"score": 90, "status": "info", "detail": f"{max_consec}句连续相同开头"}
    return {"score": 100, "status": "pass", "detail": f"max连续相同开头={max_consec}"}


def detect_i_e_ai_density(text: str) -> dict:
    """I-E AI模板情绪词密度。只统计模板化情绪词（I_E_TEMPLATE），叙事性高频词不参与。
    阈值 >1.5次/千字 起扣（模板词天然低频，名作 0-1 次，AI 水稿堆砌）。"""
    cjk = count_cjk(text)
    if cjk == 0:
        return {"score": 100, "status": "pass", "detail": "空文本"}
    counts = {}
    for w in I_E_TEMPLATE:
        c = text.count(w)
        if c:
            counts[w] = c
    n = sum(counts.values())
    density = n / cjk * 1000
    narr = sum(text.count(w) for w in I_E_NARRATIVE) / cjk * 1000
    ext = f" | 叙事白名单{narr:.1f}/千字(不计)" if narr else ""
    if density > 1.5:
        score = max(0, 100 - (density - 1.5) * 15)
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:5]
        return {"score": round(score, 1), "status": "warning", "detail": f"模板词{density:.1f}次/千字(>1.5){ext} top={top}"}
    if counts:
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:5]
        return {"score": 100, "status": "pass", "detail": f"模板词{density:.1f}次/千字(≤1.5){ext} top={top}"}
    return {"score": 100, "status": "pass", "detail": f"模板词 0 次/千字{ext}"}


def detect_i_f_sentence_rhythm(text: str) -> dict:
    """I-F 句长节奏。通篇超短句成串像提纲 → warning。"""
    sents = [s.strip() for s in re.split(r"[。！？!?]", text) if len(s.strip()) > 1]
    if len(sents) < 20:
        return {"score": 100, "status": "pass", "detail": "句数<20不判"}
    short = [s for s in sents if len(s) <= 5]
    short_ratio = len(short) / len(sents)
    if short_ratio > 0.5:
        return {"score": 75, "status": "warning", "detail": f"超短句占比{short_ratio:.0%}(>50%) 像提纲"}
    return {"score": 100, "status": "pass", "detail": f"超短句占比{short_ratio:.0%}"}


def detect_i_g_explanatory_monologue(text: str) -> dict:
    """I-G 解释性独白（Phase2-B 硬约束，命中即 FAIL）。

    G1 纯内心解释段：段首命中解释/复盘类引导词（他想起/他明白/他告诉自己…），
       段内无引号对白、无动作动词，且段长超过阈值（纯心理复盘长篇） → FAIL。
    G2 重复确认：因果复盘 token（持平/不能再犯/我要说/我决定/同一道题…）
       在全文总命中数超阈值 → FAIL（同义反复"作者怕读者没懂"）。

    阈值取自上轮盲评副作用样本（C4/C5/C6）与 baseline（C1/C2/C3）的分离点校准，
    校准记录见 detect_internal.py 首部注释与项目 phase2B 验证记录。
    """
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    # G1：纯内心解释段
    pure_think = []
    for p in paras:
        if not any(p.startswith(lead) for lead in I_G_THINK_LEADS):
            continue
        # 对话判定：只认"引号内含句末标点/语气词"的真对话（排除"持平"这类短引号强调词）
        has_dialog = re.search(r"[""「『]([^""「」『』]{1,10}[。！？!?？…~])[」』""]|他说|她问|问道|答道|说：|说:", p) is not None
        if has_dialog:
            continue
        if any(a in p for a in I_G_ACTION_WORDS):  # 含确定性动作 → 非纯解释
            continue
        if len(p) > 60:  # 纯心理复盘长段（校准：baseline 短促带决断均 <60）
            pure_think.append(f"{len(p)}字：「{p[:24]}…」")
    if pure_think:
        return {"score": 0, "status": "fail", "detail": "解释性内心段x" + str(len(pure_think)) + ": " + "; ".join(pure_think[:3])}
    # G2：重复确认（核心病灶词单词 ≥6 = 过量复盘）
    recap_hits = {}
    for w in I_G_RECAP_TOKENS:
        c = text.count(w)
        if c:
            recap_hits[w] = c
    worst = max(recap_hits.values(), default=0)
    if worst >= 6:
        top = sorted(recap_hits.items(), key=lambda kv: -kv[1])[:5]
        return {"score": 0, "status": "fail", "detail": f"复盘词最高x{worst}(≥6) 重复确认 top={top}"}
    return {"score": 100, "status": "pass", "detail": f"解释段0 | 复盘词最高x{worst}"}


# ---------- I-H 标题行核验（2026-08-28，作用于清洗前原文） ----------
# 标题行规则：正文首行必须为 `# 第N章 章名`（由落盘步骤确定性写入，禁 LLM 自带/漏写/格式不符）。
# 注意：clean_text 会把 `#` 行和短「第N章」行整行剥离，故 I-H 不能并入 run_all(clean 文本)，
# 必须在 main() 里对 raw 原文单独检测。
_I_H_HEADING_RE = re.compile(r"^#{1,6}\s*第\s*([0-9]+|[一二三四五六七八九十两]+)\s*章[\s:：·]+\S+")
_I_H_GUESS_RE = re.compile(r"^(?:#{0,6}\s*)?第\s*([0-9]+|[一二三四五六七八九十两]+)\s*章[\s:：·]?\S*")
_I_H_CN_DIGIT = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _i_h_chapter_num(tok: str):
    """章号 token（阿拉伯或中文，1-99）→ int；解析不了返回 None。"""
    t = re.sub(r"\s+", "", tok)
    if not t:
        return None
    if t.isdigit():
        return int(t)
    if t == "十":
        return 10
    if t.startswith("十") and len(t) == 2:
        return 10 + _I_H_CN_DIGIT.get(t[1], 0)
    if t.endswith("十") and len(t) == 2:
        return _I_H_CN_DIGIT.get(t[0], 1) * 10
    return _I_H_CN_DIGIT.get(t)


def detect_i_h_title_line(raw: str) -> dict:
    """I-H 标题行核验：首行必须为 `# 第N章 章名`。漏写/格式不符/疑似标题但缺 # → FAIL。"""
    for line in raw.splitlines():
        if not line.strip():
            continue
        s = line.strip()
        m = _I_H_HEADING_RE.match(s)
        if m:
            return {"score": 100, "status": "pass", "detail": f"标题行OK：{s[:24]}"}
        if _I_H_GUESS_RE.match(s):
            return {"score": 0, "status": "fail", "detail": f"首行疑似标题但格式不符（须为 # 第N章 章名 带 # 前缀）：{s[:24]}"}
        return {"score": 0, "status": "fail", "detail": f"首行非标题行（缺 # 第N章 章名）：{s[:24]}"}
    return {"score": 0, "status": "fail", "detail": "空文本，缺标题行"}


# ---------------------------------------------------------------------------
# I-J 段落密度门禁（Paragraph Density Gate，2026-09-09 新增）
# 修复"单向残疾"：原有 I-A~I-H 只防短（过均匀/超短句），不防长（过密）。
#  - J1 单段超长：普通叙事段 >80 字 → warning；>160 字 → fail
#    （80 字≈手机 3-4 行；160 对齐 check-ai-patterns strong-warning 上限。
#      完整推理链/氛围/情绪呼吸段允许 100-150 字，J1 以 warning 提示不硬断）
#  - J2 长段连片：任意连续 3 段每段 >70 字 且 窗口内最长段 >120 字 → fail
#    （2026-09-09 校准：原"连续3段平均>70"会误伤正常氛围密集区（70-80字连片），
#      导致 writer 为躲 warning 过度切段。改为"每段>70 且出现 120+ 墙"，
#      只拦"长段连片+墙"的真病，正常短-长-短呼吸不受影响）
#  - J3 信息层堆叠：段长 >120 字且同段命中 动作/心理/规则/因果 中 ≥3 类 → fail
#    （对应 format-and-structure.md「拆信息层级」：禁止一段同时承担多层信息）
# ---------------------------------------------------------------------------
I_J_MIND_PATTERN = re.compile(r"心里|觉得|他想|他明白|判断|念头|认为|打算|意识到")
I_J_RULE_PATTERN = re.compile(r"规则|必须|不许|不能|只能|对应|等于|就是|规矩|走法|机制")
I_J_CAUSE_PATTERN = re.compile(r"因为|所以|于是|也就是说|换句话说|这意味着|之所以")


def detect_i_j_paragraph_density(text: str) -> dict:
    """I-J 段落密度门禁：防"过密"（长段墙/呼吸感缺失/信息层堆叠）。
    Round 3 R3-06：与 nww_paragraph_gate.py 校准后的生产阈值对齐（J1_WARN=160, J1_FAIL=220，J2 连续3段>95且最长>150），
    消除正常 85~145 字叙事段落被一律误判为 75 分 warning 的假阳性。
    """
    paras = [
        p.strip() for p in re.split(r"\n\s*\n", text)
        if p.strip() and not p.strip().startswith("#")
    ]
    if len(paras) < 3:
        return {"score": 100, "status": "pass", "detail": "段落<3，不判"}
    lens = [len(p) for p in paras]
    mean = sum(lens) / len(lens)
    j1_warn = int(os.getenv("NWW_IJ_WARN_CHARS", "160"))
    j1_fail = int(os.getenv("NWW_IJ_FAIL_CHARS", "220"))
    over_warn = [i + 1 for i, x in enumerate(lens) if x > j1_warn]
    over_fail = [i + 1 for i, x in enumerate(lens) if x > j1_fail]
    # J2 长段连片：连续 3 段每段 >95 且窗口内最长段 >150
    dense_windows = []
    for i in range(len(lens) - 2):
        w = lens[i:i + 3]
        if all(x > 95 for x in w) and max(w) > 150:
            dense_windows.append(f"[{i+1}-{i+3}]({w[0]}/{w[1]}/{w[2]}字,最长{max(w)})")
    # J3 信息层堆叠（段长>150 且 ≥3 类命中）
    stacked = []
    for i, p in enumerate(paras):
        if lens[i] <= 150:
            continue
        layers = 0
        if any(a in p for a in I_G_ACTION_WORDS):
            layers += 1
        if I_J_MIND_PATTERN.search(p):
            layers += 1
        if I_J_RULE_PATTERN.search(p):
            layers += 1
        if I_J_CAUSE_PATTERN.search(p):
            layers += 1
        if layers >= 3:
            stacked.append(f"第{i+1}段{lens[i]}字/命中{layers}层")
    if dense_windows:
        detail = f"长段连片(连续3段每段>95且含150+墙): {', '.join(dense_windows[:4])}"
        if stacked:
            detail += f" | 信息层堆叠: {', '.join(stacked[:3])}"
        return {"score": 0, "status": "fail", "detail": detail}
    if stacked:
        return {"score": 0, "status": "fail", "detail": f"信息层堆叠(动作+心理+规则/因果同段): {', '.join(stacked[:3])}"}
    if over_fail:
        return {"score": 0, "status": "fail", "detail": f">{j1_fail}字段x{len(over_fail)}: 第{','.join(str(i) for i in over_fail[:5])}段"}
    if over_warn:
        return {"score": 75, "status": "warning", "detail": f">{j1_warn}字段x{len(over_warn)}: 第{','.join(str(i) for i in over_warn[:5])}段（若为完整推理/氛围呼吸段可豁免）"}
    return {"score": 100, "status": "pass", "detail": f"无超长段 | 段均{mean:.0f}字 | 最长{max(lens)}字"}


_SENSORY_CHANNELS = {
    "视觉光影": ("光", "影", "烛", "灯", "暗", "青", "黑", "白", "红", "紫", "金", "铜", "纹", "色", "照", "映", "霜", "雾", "烟"),
    "听觉声响": ("声", "响", "音", "鸣", "叩", "敲", "裂", "碎", "风", "雨", "滴", "漏", "哑", "喝", "笑", "叹", "息"),
    "触觉温感": ("冷", "寒", "凉", "热", "烫", "温", "湿", "潮", "滑", "糙", "硬", "沉", "重", "痛", "麻", "痒", "冰", "汗"),
    "嗅味质感": ("香", "臭", "腥", "锈", "霉", "苦", "涩", "酸", "甜", "辣", "咸", "茶", "酒", "墨", "松", "焦", "味", "息"),
}


def detect_i_i_sensory_grounding(text: str) -> dict:
    """I-I 五感与器物具象度（Round 3 R3-06）：防止纯概念空转与抽象总结，检验多感官通道覆盖与具象密度。"""
    cjk = count_cjk(text)
    if cjk < 300:
        return {"score": 100, "status": "pass", "detail": "篇幅<300字，不判"}
    active_channels = []
    total_hits = 0
    for ch_name, words in _SENSORY_CHANNELS.items():
        cnt = sum(text.count(w) for w in words)
        if cnt >= 2:
            active_channels.append(f"{ch_name}({cnt})")
        total_hits += cnt
    density = total_hits / max(1, cjk) * 1000.0
    if len(active_channels) >= 3 and density >= 12.0:
        return {"score": 100, "status": "pass", "detail": f"感官通道 {len(active_channels)}/4 ({', '.join(active_channels)}) | 具象密度 {density:.1f}/千字"}
    if len(active_channels) >= 2 and density >= 6.0:
        return {"score": 85, "status": "pass", "detail": f"感官通道 {len(active_channels)}/4 | 具象密度 {density:.1f}/千字"}
    return {"score": 65, "status": "warning", "detail": f"感官通道偏单薄({len(active_channels)}/4, 密度{density:.1f}/千字)"}


_DIALOGUE_SPAN_RE = re.compile(r"\u201c([^\u201d]{1,200})\u201d|「([^」]{1,200})」|『([^』]{1,200})』")


def detect_i_k_character_depth(text: str) -> dict:
    """I-K 人物厚度、声纹区分与潜台词留白（Round 3 R3-06）：检测对白句长方差（防千人一面）与动作/微表情锚定。"""
    cjk = count_cjk(text)
    if cjk < 300:
        return {"score": 100, "status": "pass", "detail": "篇幅<300字，不判"}
    spans = [next(g for g in m.groups() if g) for m in _DIALOGUE_SPAN_RE.finditer(text)]
    if len(spans) < 3:
        return {"score": 90, "status": "pass", "detail": f"对白数={len(spans)}（偏少，按叙事章豁免）"}
    lens = [len(s) for s in spans]
    mean_l = sum(lens) / len(lens)
    std_l = (sum((x - mean_l) ** 2 for x in lens) / len(lens)) ** 0.5
    cv = std_l / max(1.0, mean_l)
    micro_anchors = ("指尖", "目光", "袖", "眉", "唇", "掌心", "手背", "算筹", "铜尺", "步", "转身", "抬眼", "垂眸", "按住", "捻", "叩")
    anchor_hits = sum(text.count(w) for w in micro_anchors)
    if cv >= 0.30 and anchor_hits >= 4:
        return {"score": 100, "status": "pass", "detail": f"对白句长CV={cv:.2f}（声纹错落） | 动作微锚点={anchor_hits}处"}
    if cv >= 0.20 and anchor_hits >= 2:
        return {"score": 88, "status": "pass", "detail": f"对白句长CV={cv:.2f} | 动作微锚点={anchor_hits}处"}
    return {"score": 70, "status": "warning", "detail": f"对白声纹区分度或动作锚点偏弱(CV={cv:.2f}, 锚点={anchor_hits})"}


# I-L 句法残损「无条件」规则：机械删词导致的悬空副词/破碎短语与异体字。
# ★ 2026-11-07 有意收窄（契约 C-IL-RULE@2）：「不是|并非|绝非」已从本无条件分支移除，
#   它们仅在「独立成句」时才判损（见 _BROKEN_NEG_STANDALONE_RE）。行为差异：
#     - 句中「规矩不是。」（否定词前接实词）与「他说：不是。」「他想了想，不是。」（前接 ：，）
#       不再判损 —— 均为合法的口语省略结句/短答；
#     - 位于认定位点之后的「不是。」「并非，」「绝非；」仍判损（位点集合见下方 @2 规则）。
#   ★ 本正则全文不得出现捕获组（保持 (?:...)），以便 findall 直接返回命中片段。
_BROKEN_PROSE_RE = re.compile(
    r"(?:根本|忍不住|不由得|不禁|竟然|居然|却是|而是|正在|已经|将要|试图|准备|法支取|绝山)[，。！？；、]"
    r"|(?:根本)[、，。！？；]"
    r"|\u6238"
)
# 否定词「独立成句」专用规则（契约 C-IL-RULE@2）：恰一个捕获组，findall 返回否定词本身（不是/并非/绝非）。
# 认定位点（否定词前一个字符，或行首）= 行首 / 换行 / 句末标点 。！？； / 省略号 … / 右引号 ”』」。
#   ★ @1 曾把「，、：」也算位点，会误伤「他说：不是。」「他想了想，不是。」这类合法短答；
#     @2 收窄为上面的位点集合，故「：」「，」前不判损。
#   ★ 必须零宽且不消费前置字符：若写成 `(?:^|[标点])(否定词)[标点]`，连续独立否定句
#     「不是。并非。绝非；」中的前一处会把「。」吃掉，导致后一处失配，findall 只返回 2 个
#     （违反契约 R1-AC2）；`^` 与后视两个分支均为零宽，扫描不前移，三处可全部命中。
# 「规矩不是。」的否定词前是实词「矩」，不属任何位点，故不命中（合法口语省略结句）。
_BROKEN_NEG_STANDALONE_RE = re.compile(
    r"(?:^|(?<=[\n。！？；…\u201d\u300d\u300f]))(不是|并非|绝非)[，。！？；]",
    re.MULTILINE,
)
# 回滚路径专用：否定词无条件判损（等价于把「不是|并非|绝非」并回旧 _BROKEN_PROSE_RE）。
# 仅在 NWW_IL_STRICT_BROKEN 打开时使用；同样恰一个捕获组，保证 detail 与默认路径同形。
_BROKEN_NEG_ANY_RE = re.compile(r"(不是|并非|绝非)[，。！？；]")
# I-L 残句判定回滚开关（默认关闭）：设置 NWW_IL_STRICT_BROKEN=1|true|on|yes（忽略大小写与首尾空白）时，
# 「不是|并非|绝非」恢复无条件判损；未设置/空/其它值 → 走收窄后的默认判定。
_FILTER_IL_STRICT_ENV = "NWW_IL_STRICT_BROKEN"


def _il_strict_broken_enabled() -> bool:
    """读取 I-L 严格残句回滚开关。未设置/空/其它值 → False；1|true|on|yes → True。"""
    raw = os.environ.get(_FILTER_IL_STRICT_ENV, "")
    return raw.strip().lower() in ("1", "true", "on", "yes")


_DUP_SUBJECT_RE = re.compile(r"([\u4e00-\u9fa5]{2,3})(?:面色|神色|眼神|心中|脸色|目光)[^，。！？\n]{1,12}，\1")


def detect_i_l_literary_texture(text: str) -> dict:
    """I-L 文笔质感、词汇丰富度与句法完整性（Round 3 R3-06）：
    1) 硬检：机械删词导致的半截残句/悬空副词/异体字（如「忍不住。」「根本、」「法支取」「戸」）与主语重复前缀；
       否定词「不是|并非|绝非」仅在独立成句时判损——句中「规矩不是。」属合法口语省略结句，不判损
       （有意收窄；回滚开关 NWW_IL_STRICT_BROKEN=1 可恢复无条件判损）；
    2) 软检：滑动窗口双字词丰富度（MATTR-2gram）衡量词汇多样性与文笔密度。
    """
    cjk_chars = re.findall(r"[\u4e00-\u9fff]", text)
    if len(cjk_chars) < 300:
        return {"score": 100, "status": "pass", "detail": "篇幅<300字，不判"}
    broken_hits = _BROKEN_PROSE_RE.findall(text)
    if _il_strict_broken_enabled():
        # 回滚：否定词恢复无条件判损（与旧 _BROKEN_PROSE_RE 行为等价）
        broken_hits += _BROKEN_NEG_ANY_RE.findall(text)
    else:
        # 默认：仅独立成句的「不是。」「并非，」「绝非；」判损
        broken_hits += _BROKEN_NEG_STANDALONE_RE.findall(text)
    dup_hits = [m.group(0) for m in _DUP_SUBJECT_RE.finditer(text)]
    if broken_hits:
        return {
            "score": 0,
            "status": "fail",
            "detail": f"发现句法残损/截断或异体字 x{len(broken_hits)}: {broken_hits[:4]}",
        }
    if dup_hits:
        return {
            "score": 72,
            "status": "warning",
            "detail": f"发现主语前缀重复 x{len(dup_hits)}: {dup_hits[:3]}",
        }
    bigrams = [cjk_chars[i] + cjk_chars[i + 1] for i in range(len(cjk_chars) - 1)]
    win = 200
    if len(bigrams) >= win:
        ratios = [
            len(set(bigrams[i:i + win])) / float(win)
            for i in range(0, len(bigrams) - win + 1, 50)
        ]
        mattr = sum(ratios) / len(ratios)
    else:
        mattr = len(set(bigrams)) / max(1.0, float(len(bigrams)))
    if mattr >= 0.82:
        return {"score": 100, "status": "pass", "detail": f"句法完整无截断 | 词汇丰富度 MATTR={mattr:.3f}"}
    if mattr >= 0.74:
        return {"score": 88, "status": "pass", "detail": f"句法完整 | 词汇丰富度 MATTR={mattr:.3f}"}
    return {"score": 72, "status": "warning", "detail": f"词汇重复率偏高(MATTR={mattr:.3f})"}


DETECTORS = {
    "I-A": detect_i_a_paragraph_cv,
    "I-B": detect_i_b_hedge_density,
    "I-C": detect_i_c_formulaic_transition,
    "I-D": detect_i_d_list_like,
    "I-E": detect_i_e_ai_density,
    "I-F": detect_i_f_sentence_rhythm,
    "I-G": detect_i_g_explanatory_monologue,
    "I-I": detect_i_i_sensory_grounding,
    "I-J": detect_i_j_paragraph_density,
    "I-K": detect_i_k_character_depth,
    "I-L": detect_i_l_literary_texture,
}


def run_all(text: str) -> dict:
    return {k: fn(text) for k, fn in DETECTORS.items()}


# ---------- 结构化结果语义（2026-10-12 根治版） ----------
# 背景（外脑 D5/D6）：旧契约只有 0/1/2，"脚本崩溃 / 读不了文件 / 超时" 与
# "正文质量不合格" 共用退出码 1，调用方无法区分，于是把执行失败当成了质量失败。
# 根治办法：让门禁**显式说出自己是哪种结果**，而不是让调用方去猜退出码。
_A2_RESULT_SCHEMA = "a2_result_v1"

RC_PASS = 0   # 检测完成，且无阻断 fail
RC_FAIL = 1   # 检测完成，存在阻断 fail（正文质量问题）
RC_WARN = 2   # 检测完成，仅 warning
RC_USAGE = 3  # 用法/参数错误（门禁没跑起来，属配置问题）
RC_EXEC = 4   # 执行错误：文件读不了/编码错/检测异常（门禁没跑完，不等于通过）

# status ↔ returncode 的**唯一**权威映射：两边（本文件产出、调用方校验）共用同一语义。
# 任何 status 与 returncode 不自洽的组合都意味着结果不可信（外脑 P0：伪造/畸形必须 fail-closed）。
STATUS_RC = {
    "pass": RC_PASS,
    "warn": RC_WARN,
    "fail": RC_FAIL,
    "usage_error": RC_USAGE,
    "execution_error": RC_EXEC,
}


def _a2_result(status, rc, fail_items, excluded, errors, scanned, failed,
               warn_items=None, empty=0):
    """结构化结果（协议 `a2_result_v1`）。

    协议约定（供调用方严格校验，见 nww_track_commit.py::_a2_verify_result）：
      - `schema` 恒为 "a2_result_v1"；v1 允许后续**追加**可选字段，但已有字段的
        名称与类型不得变更（调用方按此做不变量校验）。
      - `returncode` **必须**与本进程最终退出码一致，`status` 与 `returncode`
        必须一一对应（见 _A2_STATUS_RC）—— 出现内部矛盾即视为结果不可信。
    """
    return {
        "schema": _A2_RESULT_SCHEMA,
        "status": status,
        "returncode": rc,
        "fail_items": sorted(fail_items),
        "warn_items": sorted(warn_items or []),
        "excluded": sorted(excluded),
        "errors": errors,
        "files_scanned": scanned,
        "files_failed": failed,
        "files_empty": empty,
    }


def main() -> int:
    """返回码契约。

    2026-08-28 旧契约（**对正常输入仍完全保持**）：
      0 = 全 pass；1 = 存在 fail（I-G/I-H）→ BLOCKING；2 = 仅 warning（记录不阻断）
    2026-10-12 根治版（新增，语义无歧义）：
      0 = pass｜1 = fail｜2 = warn｜3 = usage_error｜4 = execution_error
      —— execution_error 独立成码，杜绝"崩溃被当成质量 fail"。
      传 `--json` 时额外输出一行 `[A2-JSON] {...}`，字段见 `_a2_result()`。

    2026-10-12 新增 `--exclude`（只增不改，不传时行为逐字节同 2026-08-28 契约）：
      --exclude I-J[,I-X...]
        被排除的检测项**照常跑、照常打印**（可观测性不丢），但**不计入 any_fail / any_warn**，
        因此也不会抬升返回码。
        用途：把「已有更准的独立实现在生产闸负责」的项从本门禁的阻断面摘出去，避免同一判据
        两套阈值互相打架。典型场景我写在和此 diff 配套的 plan 里：
        I-J 已由 nww_paragraph_gate.py 以校准后的阈值（J1 BLOCK=220）独立执行，
        而本文件的 J1 fail=160 已被判定为「门禁失据」（见 nww_paragraph_gate.py 模块 docstring）。
        不传本开关 → 行为与 2026-08-28 契约**完全一致**。
    """
    argv = sys.argv[1:]
    exclude_items: set[str] = set()
    json_out = False
    usage = ("用法: python3 detect_internal.py <file...> "
             "[--exclude I-J[,I-X...]] [--json]")
    while "--json" in argv:
        argv.remove("--json")
        json_out = True
    # while 而非 if：允许多次出现（每次合并），避免第二个 --exclude 及其取值
    # 被当作「待检测文件名」静默跳过（首版实测缺陷，见 plan V9）
    while "--exclude" in argv:
        idx = argv.index("--exclude")
        if idx + 1 >= len(argv):
            print(usage)
            return RC_USAGE
        raw_exclude = argv[idx + 1]
        exclude_items |= {x.strip().upper() for x in raw_exclude.split(",") if x.strip()}
        argv = argv[:idx] + argv[idx + 2 :]
    files = argv
    if not files:
        print(usage)
        return RC_USAGE
    excluded_seen: set[str] = set()
    fail_items: set[str] = set()
    warn_items: set[str] = set()
    any_fail = False
    any_warn = False
    exec_errors: list[dict] = []
    files_scanned = 0
    files_failed = 0
    files_empty = 0
    for path in files:
        # ★ 根治点 1：任何"读不到/读不懂"都必须被记录为执行错误，而不是让进程崩溃
        #   （旧版未捕获 UnicodeDecodeError → 退出码 1 → 被 A-2 误判为质量 fail）
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = fh.read()
        except FileNotFoundError:
            print(f"[skip] 不存在: {path}")
            exec_errors.append({"kind": "file_not_found", "path": path, "detail": "文件不存在"})
            files_failed += 1
            continue
        except UnicodeDecodeError as exc:
            print(f"[ERROR] 非 UTF-8，无法解码: {path} ({exc})")
            exec_errors.append({"kind": "unicode_decode", "path": path, "detail": str(exc)})
            files_failed += 1
            continue
        except OSError as exc:
            print(f"[ERROR] 读取失败: {path} ({type(exc).__name__}: {exc})")
            exec_errors.append({"kind": "io_error", "path": path, "detail": f"{type(exc).__name__}: {exc}"})
            files_failed += 1
            continue
        # ★ 根治点 2：单个文件的检测异常不得污染整轮，也不得伪装成质量 fail
        try:
            from objective_scorer import clean_text
            text = clean_text(raw)
            if not text.strip():
                print(f"[skip] 空内容: {path}")
                files_empty += 1
                continue
            print(f"\n===== {os.path.basename(path)} (CJK={count_cjk(text)}) =====")
            results = run_all(text)
            for k in sorted(results):
                r = results[k]
                mark = {"pass": "✅", "warning": "⚠️", "info": "ℹ️", "fail": "❌"}.get(r["status"], "?")
                _excluded = k in exclude_items
                print(f"  {k} [{r['status']}] score={r['score']}  {r['detail']}"
                      f"{'  [已排除，不计入返回码]' if _excluded and r['status'] in ('fail', 'warning') else ''}")
                if _excluded and r["status"] in ("fail", "warning"):
                    excluded_seen.add(k)
                    continue
                if r["status"] == "fail":
                    any_fail = True
                    fail_items.add(k)
                elif r["status"] == "warning":
                    any_warn = True
            # I-H 对清洗前原文单独检测（clean_text 会剥标题行）
            ih = detect_i_h_title_line(raw)
            mark = {"pass": "✅", "warning": "⚠️", "info": "ℹ️", "fail": "❌"}.get(ih["status"], "?")
            print(f"  I-H [{ih['status']}] score={ih['score']}  {ih['detail']}"
                  f"{'  [已排除，不计入返回码]' if 'I-H' in exclude_items and ih['status'] == 'fail' else ''}")
            if ih["status"] == "fail":
                if "I-H" in exclude_items:
                    excluded_seen.add("I-H")
                else:
                    any_fail = True
                    fail_items.add("I-H")
            # I-J 同样对清洗前原文单独检测（clean_text 会剥正文中的 --- 分隔符，
            # 把原本独立的段合并，导致 J1/J2 漏检。paragraph_gate 用 raw，这里也对齐）
            ij = detect_i_j_paragraph_density(raw)
            results["I-J"] = ij  # 覆盖 run_all 里 clean_text 版本的结果
            mark = {"pass": "✅", "warning": "⚠️", "info": "ℹ️", "fail": "❌"}.get(ij["status"], "?")
            print(f"  I-J [{ij['status']}] score={ij['score']}  {ij['detail']}"
                  f"{'  [已排除，不计入返回码]' if 'I-J' in exclude_items and ij['status'] == 'fail' else ''}")
            if ij["status"] == "fail":
                if "I-J" in exclude_items:
                    excluded_seen.add("I-J")
                else:
                    any_fail = True
                    fail_items.add("I-J")
            # 章号交叉核验：标题行 vs 文件名（阿拉伯/中文章号可比才比）
            base = os.path.basename(path)
            bm = re.search(r"第\s*([0-9]+|[一二三四五六七八九十两]+)\s*章", base)
            first_content = next((ln.strip() for ln in raw.splitlines() if ln.strip()), "")
            fm = _I_H_HEADING_RE.match(first_content)
            inum = _i_h_chapter_num(bm.group(1)) if bm else None
            fnum = _i_h_chapter_num(fm.group(1)) if fm else None
            if inum is not None and fnum is not None and inum != fnum:
                print(f"  I-H [fail] score=0  标题行章号 {fnum} 与文件名章号 {inum} 不一致（{base}）"
                      f"{'  [已排除，不计入返回码]' if 'I-H' in exclude_items else ''}")
                if "I-H" in exclude_items:
                    excluded_seen.add("I-H")
                else:
                    any_fail = True
                    fail_items.add("I-H")
        except Exception as exc:  # noqa: BLE001 —— 门禁必须说出"我没跑完"，而不是崩成质量 fail
            print(f"[ERROR] 检测过程异常: {path} ({type(exc).__name__}: {exc})")
            # 外脑 P0：宽泛捕获不得吞掉堆栈 —— 完整 traceback 走 stderr（不进 stdout，
            # 以免污染 [A2-JSON] 解析），结构化结果里只留类型+摘要。
            traceback.print_exc(file=sys.stderr)
            exec_errors.append({
                "kind": "detect_error", "path": path,
                "detail": f"{type(exc).__name__}: {exc}",
            })
            files_failed += 1
            continue
        files_scanned += 1

    # ★ 根治点 4：「一个文件都没检测成」不等于通过。
    #   外脑 Q5-6：空内容文件既不 scanned 也不 failed，若输入全是空文件，旧语义会输出
    #   pass —— 那是"没检测"伪装成"检测通过"，属少杀红线。
    if files and files_scanned == 0:
        print("[ERROR] 无任一文件完成检测（空内容/读取失败），不得判定为通过")
        exec_errors.append({
            "kind": "nothing_scanned", "path": None,
            "detail": f"空内容 {files_empty} 个，读取失败 {files_failed} 个，完成检测 0 个",
        })
    if exclude_items:
        # 只在使用 --exclude 时才打印，避免污染既有消费者的 stdout 语义
        print(f"\n[A2-EXCLUDED] {','.join(sorted(excluded_seen)) or 'none'}"
              f"（策略排除：{','.join(sorted(exclude_items))}；已检测但不抬升返回码）")
        print(f"[A2-FAIL-ITEMS] {','.join(sorted(fail_items)) or 'none'}（真正抬升返回码的项）")

    # ★ 根治点 3：状态判定优先级 —— 门没跑完 > 质量 fail > warning > pass
    #   执行错误优先，是因为"没检测完"绝不能输出"通过"或"质量不合格"中的任何一个。
    if exec_errors:
        status, rc = "execution_error", RC_EXEC
    elif any_fail:
        status, rc = "fail", RC_FAIL
    elif any_warn:
        status, rc = "warn", RC_WARN
    else:
        status, rc = "pass", RC_PASS

    if json_out:
        print("[A2-JSON] " + json.dumps(
            _a2_result(status, rc, fail_items, excluded_seen, exec_errors,
                       files_scanned, files_failed, warn_items, files_empty),
            ensure_ascii=False, sort_keys=False))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())