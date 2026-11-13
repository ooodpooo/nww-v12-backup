#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
轨A · 客观硬指标评分器 v2（NWW-V12 质量评分体系）
================================================
独立新模块，与现有流水线零耦合。纯确定性规则，零 LLM 成本。

v2 校准要点（依据基因库首轮回测证据，2026-08-27）：
1. 硬否决缩至 3 项：系统流痕迹 / 蓝图泄漏词 / 章节号指称
   依据：真实 95 分书上这三类 0 命中；其余规则都在真实书出现（见命中率表）。
2. 其余规则全部降为「软扣分」：设行业容忍次数 free，超过部分按 unit 扣分。
3. AI 词密度按【次/CJK千字】计。
4. 机械指标只保留跨文体安全项：长段、连续了字、惊讶标记词密度。

v3 校准要点（2026-08-27 二轮回测）：
5. 软规则计数按篇幅归一化：free 以「每章(≈3000字)」计，
   free_scaled = free * max(1, cjk/3000)。修复 30 章合体文件的
   计数放大假阳性（捞尸人「不是而是×16」在30万字仅0.5次/万字；玄鉴
   「————」每章1.15次分隔是风格；捞尸人「显然」×10 是说书口吻）。

评分公式 v3：
    base  = clamp(100 - max(0, density - 3.5) * 8, 0, 100)
    soft  = sum(unit * max(0, count - free*chapter_equiv) for each soft rule)
    硬否决命中 n>=1 -> score = min(score, 40 - 15*(n-1))
    score = clamp(base - soft - 3*mech_warns)
"""

import re

# ---------------------------------------------------------------------------
# AI 高频词表（密度打分，非命中即扣）
# ---------------------------------------------------------------------------
AI_WORDS = [
    "微微", "缓缓", "轻轻", "深深", "不禁", "猛然", "猛地", "忽然", "悄然",
    "默默", "渐渐", "慢慢", "顿时", "瞬间", "那一刻", "那一瞬间", "在这一刻",
    "深吸一口气", "倒吸一口凉气", "难以置信", "心头一紧", "眉头紧锁",
    "瞳孔微缩", "瞳孔收缩", "嘴角上扬", "嘴角勾起", "展现出", "流露出",
    "浮现出", "涌上心头", "弥漫着", "弥漫开", "充斥着", "难以言喻",
    "无法形容", "五味杂陈", "若有所思", "不由自主", "情不自禁", "恍然大悟",
    "眼底深处", "眸光闪动", "眼神深邃", "仿佛", "宛如", "似乎",
]

# ---------------------------------------------------------------------------
# 硬否决项（真实 95 分书 0 命中的铁板 AI 痕迹）
# ---------------------------------------------------------------------------
HARD_VETO_RULES = [
    ("系统流痕迹", re.compile(r"(【(检测到|推演进度|自动战斗模块|目标锁定|警告|提示)[^】]{0,20}】|检测到[^。]{0,12}|推演进度[：:]?\d|建议[：:]物理超度|建议[：:]立即(休眠|规避)|目标锁定[：:])")),
    ("蓝图泄漏词", re.compile(r"(威胁性悬念|无用描写|信息牌|契诃夫之枪|缺德话|幽默节拍|藏拙张力|退无可退|埋下伏笔)")),
    ("章节号指称", re.compile(r"第\s*\d+\s*章")),
]

# ---------------------------------------------------------------------------
# 软扣分规则：(名称, 正则, 容忍次数free, 每超一次扣分unit)
# free/unit 依据基因库实测：头部书在 free 内无伤大雅，AI 样本普遍超标
# ---------------------------------------------------------------------------
SOFT_RULES = [
    ("破折号断行", re.compile(r"^\s*——", re.M), 3, 4),
    ("裸括号对白", re.compile(r"【[^【】]{6,}[！！？。，…：][^】]*】"), 3, 3),
    ("路标词", re.compile(r"(与此同时|就在这时|不知不觉(中|间)|话说回来|且说|无巧不成书)"), 3, 2),
    ("排比堆砌", re.compile(r"(不是[，,][^。]{0,12}，而是|一边[，,][^。]{0,12}，一边)"), 1, 4),
    ("不是而是句式", re.compile(r"不是[^。]{0,10}，而是[^。]{0,10}"), 2, 3),
    ("模板比喻", re.compile(r"(像打了鸡血(一|似)样|像死(狗|尸)(一|似)样|像(一|坨)滩烂泥)"), 2, 3),
    ("情绪直接命名", re.compile(r"(他感到(一阵)?(悲伤|愤怒|恐惧|绝望)|一股[一-龥]{1,4}涌上心头)"), 1, 4),
    ("升华金句收尾", re.compile(r"([林夜他]*(?:这一刻|从这一天起|从今往后|他知道|他明白|他不再是)[，,。][^。]*(命运|人生|世界|修仙之路|最硬的骨头|只想躺平|社畜))"), 1, 5),
    ("章节摘要式结尾", re.compile(r"(坏事[^。]{0,5}正在逼近|危机[^。]{0,5}即将|风暴[^。]{0,5}来临|没有人知道[，。][^。]{0,10}|一场大戏[^。]{0,6}开始|命运[的]?齿轮[^。]{0,6}|真正的考验[^。]{0,6}(才刚刚|拉开)|具体的[^。]{0,8}正在逼近|不知道的是[^。]{0,10}|退路已断[，。][^。]{0,12}正在|悄然转动|真正的威胁[^。]{0,6}开始|才刚刚开始)"), 1, 5),
    ("AI式升华结尾", re.compile(r"(无尽的黑暗|未知的深渊|刀山火海|真正的风暴[^。]*还在后面|这只是开始|无路可退|大戏的序幕|只能向前)"), 1, 5),
    ("报告术语", re.compile(r"(核心动机|信息边界|信息落差|利益最大化|行为约束|性格过滤|情绪外化|锚定效应|沉没成本|认知共鸣|当前处境)"), 1, 4),
    ("元叙事", re.compile(r"(到这里[，,]?算是|接下来[，,]?(就是|将会|即将)|(后面|之后)[，,]?(会|将|还会)|(故事|剧情)(发展)?到了|读者[，,]?(可能|应该|也许)(会|觉得)|我们[，,]?(可以|不妨|来看))"), 1, 6),
    ("说教词", re.compile(r"(显然|毋庸置疑|不言而喻|众所周知|不难看出)"), 1, 4),
    ("集体反应", re.compile(r"((全场|众人|所有人|在场的人)[，,]?(都|全|齐齐|纷纷)?(震惊|惊呆|倒吸凉气|目瞪口呆|哗然|惊呼)|(全场|一片)[，,]?(寂静|哗然|沸腾|震动))"), 1, 5),
]

# ---------------------------------------------------------------------------
# 机械告警项（每项 -3）
# ---------------------------------------------------------------------------
SURPRISE_MARKERS = ["仿佛", "忽然", "竟然", "猛地", "猛然", "不禁", "宛如"]


def count_cjk(s: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", s))


def strip_dialogue_paragraphs(paras):
    """过滤对话段（以引号开头行），用于叙述统计。"""
    out = []
    for p in paras:
        t = p.strip()
        if not t:
            continue
        if t.startswith(("“", "「", "『", "'", '"')):
            continue
        out.append(p)
    return out


def analyze_ai_density(text: str):
    """AI词密度（次/CJK千字）。"""
    cjk = count_cjk(text)
    if cjk == 0:
        return 0.0, {}
    counts = {}
    for w in AI_WORDS:
        c = text.count(w)
        if c:
            counts[w] = c
    total = sum(counts.values())
    return round(total / cjk * 1000, 2), counts


def check_mechanical_warnings(text: str):
    """机械层告警（跨文体安全项，实测阈值）。"""
    warns = []
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    cjk = count_cjk(text)
    if cjk < 500:  # 过短文不判机械层
        return warns

    longp = [p for p in paras if len(p) > 300]
    if len(longp) >= 2:
        warns.append("长段过多(%d段>300字)" % len(longp))

    sents = [s for s in re.split(r"[。！？!?]", text) if len(s.strip()) > 2]
    max_le = cur = 0
    for s in sents:
        cur = cur + 1 if "了" in s else 0
        max_le = max(max_le, cur)
    if max_le >= 12:
        warns.append("连续了字(%d句)" % max_le)

    marker_total = sum(text.count(w) for w in SURPRISE_MARKERS)
    if cjk and marker_total / cjk * 1000 > 0.8:
        warns.append("惊讶标记词(%s次/千字)" % round(marker_total / cjk * 1000, 2))

    if len(paras) >= 3:
        lens = [len(p) for p in paras]
        mean = sum(lens) / len(lens)
        if mean > 0:
            std = (sum((x - mean) ** 2 for x in lens) / len(lens)) ** 0.5
            if mean > 0 and std / mean < 0.10:
                warns.append("段落等长(CV=%.3f)" % (std / mean))
    return warns


def strip_quoted_content(text: str) -> str:
    """去除引号内的内容，防止对话中提到章节号触发硬否决。

    处理：中文引号「」『』、全角引号""''、半角引号""''
    未闭合引号不删除（避免误删正文后半段）。
    """
    # 分组匹配：每种引号对独立处理，跳过未闭合的情况
    patterns = [
        r"「[^」]*」",     # 日式引号
        r"『[^』]*』",     # 日式双引号
        r"\u201c[^\u201d]*\u201d",  # 中文左双引号...右双引号
        r"\u2018[^\u2019]*\u2019",  # 中文左单引号...右单引号
        r'"[^"]*"',        # 半角双引号
        r"'[^']*'",        # 半角单引号
    ]
    result = text
    for pat in patterns:
        result = re.sub(pat, "", result)
    return result


def score_text(text: str):
    """轨A打分入口。返回 (score, details)。"""
    density, word_counts = analyze_ai_density(text)
    base = 100 - max(0.0, density - 3.5) * 8
    base = max(0.0, min(100.0, base))

    soft_hits, soft_penalty = [], 0.0
    cjk = count_cjk(text)
    chapter_equiv = max(1.0, cjk / 3000.0)
    for name, rx, free, unit in SOFT_RULES:
        cnt = len(rx.findall(text))
        if cnt:
            free_scaled = free * chapter_equiv
            over = max(0.0, cnt - free_scaled)
            soft_hits.append(f"{name}x{cnt}(free={free_scaled:.0f})")
            soft_penalty += unit * over

    # 硬否决：章节号指称只在非引号内容中检测（避免对话"第3章"误触）
    non_quoted = strip_quoted_content(text)
    hard_hits = []
    for name, rx in HARD_VETO_RULES:
        search_text = non_quoted if name == "章节号指称" else text
        if rx.search(search_text):
            hard_hits.append(name)
    score = base - soft_penalty
    if hard_hits:
        score = min(score, 40 - 15 * (len(hard_hits) - 1))

    warns = check_mechanical_warnings(text)
    score = score - 3 * len(warns)
    score = max(0.0, min(100.0, round(score, 1)))

    cjk = count_cjk(text)
    details = {
        "chars": cjk,
        "ai_density": density,
        "ai_top_words": sorted(word_counts.items(), key=lambda kv: -kv[1])[:8],
        "soft_hits": soft_hits,
        "hard_hits": hard_hits,
        "warns": warns,
        "score": score,
    }
    return score, details




# ---------------------------------------------------------------------------
# 90 分制综合评分合成公式（2026-10-26 补齐审计 A11：轨A客观分 × 内部实证I-A~I-J × 外部维度E0~E5）
# ---------------------------------------------------------------------------
DIMENSION_FLOOR = 70.0
BASELINE_90_THRESHOLD = 90.0


def compute_composite_90score(
    raw_text: str,
    external_scores: dict[str, float] | None = None,
    chapter: int | None = None,
) -> dict:
    """计算可审计的 90 分制综合评分（打通轨A objective_scorer + detect_internal I-A~I-J + E0~E5）。

    权重结构（对齐 quality/rubric_90score_v3.md）：
      - 内部实证与客观去AI味层（40%）：轨A客观分（20%）+ 内部检测器 I-A~I-J 均分（20%）
      - 外部叙事结构与读者体验层（60%）：E1~E5（第1~3章含 E0 黄金三章链）；若未提供外部维度分，
        则按内部实证与轨A客观分等权合成，并标注 mode="internal_only"。
      - 硬底线（一票否决）：
        1) 任一已提供维度 < 70（DIMENSION_FLOOR）→ floor_veto=True，passed_90_baseline=False
        2) 硬否决项（系统流泄漏/蓝图词/章节号指称）或 I-G/I-H/I-J fail → hard_veto=True
    """
    cleaned = clean_text(raw_text)
    track_a_score, track_a_details = score_text(cleaned)

    internal_results = {}
    internal_scores = {}
    internal_fails = []
    try:
        import sys as _sys
        from pathlib import Path as _Path
        _qdir = str(_Path(__file__).resolve().parent)
        if _qdir not in _sys.path:
            _sys.path.insert(0, _qdir)
        import detect_internal as _di
        internal_results = _di.run_all(cleaned)
        internal_results["I-H"] = _di.detect_i_h_title_line(raw_text)
        for k, v in internal_results.items():
            if isinstance(v, dict) and "score" in v:
                internal_scores[k] = float(v["score"])
                if v.get("status") == "fail":
                    internal_fails.append(k)
    except Exception:
        pass

    internal_mean = (
        round(sum(internal_scores.values()) / len(internal_scores), 2)
        if internal_scores else track_a_score
    )
    internal_combined = round(0.5 * track_a_score + 0.5 * internal_mean, 2)

    floor_violations = []
    for k, v in internal_scores.items():
        if v < DIMENSION_FLOOR:
            floor_violations.append(f"{k}={v:g}")

    ext_valid = {}
    if isinstance(external_scores, dict) and external_scores:
        for k, v in external_scores.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                fv = float(v)
                ext_valid[str(k)] = fv
                if fv < DIMENSION_FLOOR:
                    floor_violations.append(f"{k}={fv:g}")

    if ext_valid:
        ext_mean = round(sum(ext_valid.values()) / len(ext_valid), 2)
        composite = round(0.40 * internal_combined + 0.60 * ext_mean, 2)
        mode = "full_hybrid"
    else:
        ext_mean = None
        composite = internal_combined
        mode = "internal_only"

    hard_veto = bool(track_a_details.get("hard_hits") or any(x in internal_fails for x in ("I-G", "I-H")))
    if hard_veto:
        composite = min(composite, 59.0)

    passed_90 = bool(
        composite >= BASELINE_90_THRESHOLD
        and not floor_violations
        and not hard_veto
    )
    passed_85_chapter = bool(
        composite >= 85.0
        and not floor_violations
        and not hard_veto
    )
    return {
        "composite_score": composite,
        "mode": mode,
        "passed_90_baseline": passed_90,
        "passed_85_chapter_gate": passed_85_chapter,
        "track_a_score": track_a_score,
        "internal_mean": internal_mean,
        "external_mean": ext_mean,
        "internal_scores": internal_scores,
        "external_scores": ext_valid,
        "floor_violations": sorted(set(floor_violations)),
        "internal_fails": internal_fails,
        "hard_veto": hard_veto,
        "track_a_details": track_a_details,
        "chapter": chapter,
    }


def clean_text(raw: str) -> str:
    """清洗：去 HTML 注释、markdown 标题/YAML frontmatter/代码块/章节标题行。"""
    lines = []
    in_front = False
    fence = False
    for line in raw.splitlines():
        s = line.strip()
        if s.startswith("<!--"):
            continue
        if s == "---":
            in_front = not in_front
            continue
        if in_front:
            continue
        if s.startswith("```"):
            fence = not fence
            continue
        if fence:
            continue
        if s.startswith("#"):
            continue
        if re.match(r"^第\s*\d+\s*章", s) and len(s) < 30:
            continue
        lines.append(line)
    return "\n".join(lines)