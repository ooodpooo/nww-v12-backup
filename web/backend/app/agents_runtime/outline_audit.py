# -*- coding: utf-8 -*-
"""一致性审计链（consistency audit，2026-09-19 建；2026-09-20 只读化裁决）。

职责边界（用户裁决 2026-09-20）：
  审计 = 一致性检测层。只做 Observe → Analyze → Report，绝不 Modify。
  auto_fix 已从审计职责中彻底移除（所有路径：Web 按钮 / Chat Agent execute / API
  统一 readonly）；「修复」由报告中的 suggestion 驱动用户决策，不在审计内执行。

  证据装配（确定性）→ LLM 结构化抽取 → 确定性复核（防幻觉：
    invented 实体必须在细纲+bible 字符串缺席才成立；evidence 摘录必须在源文本中
    真实存在，否则标记未证实）→ 分级报告落盘 追踪/脱纲审计/。

  检查四类（2026-09-20 扩展）：
    A 细纲一致性：missing_beat（漏=P1）/ deviation（越=P1，偏=P2）
    B 世界事实一致性：invented_character/faction/setting（P0）/ invented_item（P1）
    C 角色一致性：character_drift（性格/能力/关系漂移，对照 追踪/角色状态/）
    D 伏笔/状态漂移：foreshadow_drift（未回收/逾期，对照 追踪/伏笔.md）

  每条 finding 携带 issue_id + status="open"（接受偏移治理留待后续体系，本轮仅记录）。
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from pathlib import Path

from . import runtime
from .pipeline import (
    _find_outline_file,
    _read_prev_chapter_full,
    _resolve_existing_body,
    _volume_outline_section,
    _writer_stream,
    _TRACKING_DIR,
    _DELTA_PREFIX,
)

_AUDIT_DIR = "脱纲审计"

_AUDIT_SYSTEM = (
    "你是一致性审计师，审计小说章节正文是否偏离原计划与世界状态。\n"
    "【输入】本章正文 <prose>、本章细纲 <outline>、全书设定 <bible>、角色状态 <character_state>、"
    "伏笔台账 <foreshadow>、上一章已认证事实 <prev_delta>。\n"
    "【任务】只输出一个 JSON 对象（前后不写任何说明/围栏）：\n"
    "{\n"
    '  "must_beats": [{"beat": "细纲要求必须落地的点(从细纲摘录)", "landed": true, "evidence": "正文对应句(逐字摘录；未落地则空串)"}],\n'
    '  "entities": {"characters": ["正文出场人物"], "items": ["关键物品道具"], "factions": ["势力组织"], "settings": ["正文新声明的设定"]},\n'
    '  "findings": [{"type": "missing_beat|invented_character|invented_item|invented_faction|invented_setting|deviation|character_drift|foreshadow_drift",\n'
    '    "entity": "漂移对象名(仅 invented_*/character_drift/foreshadow_drift 填)", "severity": "P0|P1|P2", "detail": "问题描述",\n'
    '    "evidence": "正文/证据逐字摘录", "fix_hint": "给修复动作的执行要点(60字内)"}]\n'
    "}\n"
    "【判定规则】\n"
    "1. missing_beat(P1)：细纲明确要求的事件/落点/台词/钩子在正文找不到对应落地。\n"
    "2. invented_character/faction/setting(P0)、invented_item(P1)：正文出现的角色/势力/设定/关键物品，"
    "在细纲与 bible 中都不存在（无名路人、泛用日常物不算）。\n"
    "3. deviation(P1)：正文事件走向与细纲明确冲突（输赢/生死/立场/因果反转）；细节偏移(P2)。\n"
    "4. character_drift(P1，对照 <character_state> 与 <bible>)：已登记角色的性格/能力/关系/状态"
    "与正文表现冲突（如谨慎经营的角色突然无脑冒险、无灵根却用灵力）；能力与事实冲突升 P0。\n"
    "5. foreshadow_drift(P2，对照 <foreshadow> 台账)：已埋伏笔逾期未回收、或正文与伏笔台账状态冲突；"
    "伏笔结算与台账严重矛盾升 P1。\n"
    "6. 拿不准的不报（宁缺毋滥）；无问题 → findings 空数组。\n"
    "7. 以下两类由系统**确定性正则**检测，你不要重复报：① 方括号数值「【烟火值：N】」；"
    "② 面板凭空浮现的描写（浮在眼前/浮起一行/虚空里…）。\n"
    "【自检】每条 evidence 真的是从正文/证据源逐字抄的吗？每条 invented/character_drift/foreshadow_drift"
    "的 entity 你确认在对应证据源里能对上吗？"
)

_SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}

# 建议动作确定性路由（不依赖 LLM）：由 finding 的 type/severity 唯一决定
def _suggest(type_: str, severity: str) -> str:
    """审计发现 → 建议动作（V1 确定性映射，人工可在报告上覆写）。

    rewrite_L1/L2/L3 = 重写等级（见重写链）；patch = 补丁修改（revise 只增不删）；
    accept = 接受偏移（人工背书，治理体系后续版本落地）。
    """
    if type_ == "canon_violation":
        # 确定性门禁产出：文字级修正（去方括号/改浮空描写），不需结构级重写
        return "rewrite_L1"
    if type_.startswith("invented_character") or type_.startswith("invented_faction") or type_.startswith("invented_setting"):
        return "rewrite_L3"
    if type_ == "character_drift":
        return "rewrite_L2" if severity == "P1" else "rewrite_L3"
    if type_ == "missing_beat":
        return "rewrite_L2"
    if type_ == "deviation":
        return "rewrite_L3" if severity == "P1" else "accept"
    if type_.startswith("invented_item"):
        return "patch"
    if type_ == "fabricated_number":
        # 确定性门禁产出：文字级修正（改回细纲数字/模糊化），不需结构级重写
        return "patch"
    if type_ == "foreshadow_drift":
        return "accept" if severity == "P2" else "patch"
    return "accept"


def _assign_issue_meta(chapter_id: int, findings: list[dict]) -> list[dict]:
    """给每条 finding 装配 issue_id + status（接受偏移治理的前置记录，V1 仅写 open）。"""
    out: list[dict] = []
    for idx, f in enumerate(findings, start=1):
        f = dict(f)
        f["issue_id"] = f"audit-{chapter_id:03d}-{idx:03d}"
        f.setdefault("status", "open")
        f["suggestion"] = _suggest(str(f.get("type", "")), str(f.get("severity", "P2")))
        out.append(f)
    return out


def _read_outline_text(book: Path, chapter_id: int) -> tuple[str, str]:
    """读本章细纲文本：单章细纲优先，卷级主细纲段落兜底。返回 (文本, 来源描述)。"""
    p = _find_outline_file(book, chapter_id)
    if p is not None and p.is_file():
        try:
            return p.read_text(encoding="utf-8"), p.name
        except OSError:
            pass
    vol = _volume_outline_section(book, chapter_id)
    if vol is not None:
        text, _title, vp = vol
        return text, f"{vp.name}(卷级段落)"
    return "", "缺失"


def _read_bible_text(book: Path) -> str:
    p = book / "bible.json"
    if not p.is_file():
        return ""
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


def _read_character_state(book: Path) -> str:
    """C 类证据源：追踪/角色状态/*.md（已登记角色的性格/能力/关系/状态）。"""
    d = book / _TRACKING_DIR / "角色状态"
    if not d.is_dir():
        return ""
    parts: list[str] = []
    try:
        for p in sorted(d.glob("*.md")):
            try:
                parts.append(f"## {p.stem}\n{p.read_text(encoding='utf-8')[:2500]}")
            except OSError:
                continue
    except OSError:
        return ""
    return "\n\n".join(parts)[:12000]


def _read_foreshadow(book: Path) -> str:
    """D 类证据源：追踪/伏笔.md（伏笔台账：未回收/逾期/结算状态）。"""
    p = book / _TRACKING_DIR / "伏笔.md"
    if not p.is_file():
        return ""
    try:
        return p.read_text(encoding="utf-8")[:6000]
    except OSError:
        return ""


def _read_prev_delta(book: Path, chapter_id: int) -> str:
    if chapter_id <= 1:
        return ""
    p = book / _TRACKING_DIR / f"{_DELTA_PREFIX}{chapter_id - 1:03d}章.json"
    if not p.is_file():
        return ""
    try:
        return p.read_text(encoding="utf-8")[:4000]
    except OSError:
        return ""


def _repair_json_text(s: str) -> str:
    """修复模型输出 JSON 的两类常见瑕疵（状态机，只在字符串内部动手）。

    2026-09-27：审计要求模型把**正文原句**抄进 evidence 字段，而这些句子里大量
    含中文对话引号、裸换行 → 模型经常不做转义 → `json.loads` 直接
    `Expecting ',' delimiter` → **整章审计失败**（1-28 章实测 ch2/ch3 连续挂）。
    本函数只修「字符串内部的裸引号/裸换行」，不动结构字符。

    引号消歧规则：字符串内的 `"` 若其后首个非空白字符是 `, } ] :` 之一（或到末尾），
    判定为**字符串结束**；否则判定为**内容**并转义为 `\\"`。
    """
    out: list[str] = []
    in_str = False
    esc = False
    n = len(s)
    for i, ch in enumerate(s):
        if esc:
            out.append(ch)
            esc = False
            continue
        if ch == "\\":
            out.append(ch)
            esc = True
            continue
        if ch == '"':
            if not in_str:
                in_str = True
                out.append(ch)
                continue
            k = i + 1
            while k < n and s[k] in " \t\r\n":
                k += 1
            nxt = s[k] if k < n else ""
            if nxt in ("", ",", "}", "]", ":"):
                in_str = False
                out.append(ch)
            else:
                out.append('\\"')
            continue
        if in_str and ch == "\n":
            out.append("\\n")
            continue
        if in_str and ch == "\r":
            out.append("\\r")
            continue
        if in_str and ch == "\t":
            out.append("\\t")
            continue
        out.append(ch)
    return "".join(out)


def _parse_audit_json(raw: str) -> dict:
    """容错解析审计 JSON：剥围栏 → 取首个 { 到末个 } → 原样解析，失败则宽容修复后再试。"""
    s = (raw or "").strip()
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        raise ValueError("审计模型未输出 JSON 对象")
    body = s[i:j + 1]
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        # 宽容修复后重试一次；仍失败才把原始错误抛出去（保留可诊断信息）
        try:
            data = json.loads(_repair_json_text(body))
        except json.JSONDecodeError:
            raise ValueError(f"审计 JSON 解析失败：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("审计 JSON 不是对象")
    data.setdefault("must_beats", [])
    data.setdefault("entities", {})
    data.setdefault("findings", [])
    return data


def _recheck_findings(findings: list[dict], prose: str, outline: str, bible: str) -> list[dict]:
    """确定性复核（防幻觉闸）：

    - invented_*：entity 必须在「细纲+bible」中字符串缺席才成立（在场 → 幻觉，丢弃并记录）；
      P2-A 短词边界保护：entity < 4 字（如"辣椒""凡气"）在长文 bible 中极易作为子串被误判「在场」，
      短 entity 不做字符串否决（改标 entity_short，交 LLM/人工兜底），避免审计假阴性；
    - evidence 非空且 ≥6 字时必须在源文本中真实存在，否则标 evidence_unverified
      （P2-B：阈值由 12 降到 6，与锚点下限对齐）。
      ★2026-09-27 修正（双底本）：missing_beat 的 evidence 有两种**合法形态**——
        (a) 摘录细纲中未被落实的 beat        → 该拿 outline 核；
        (b) 摘录正文中「替代/缺失」的现象    → 该拿 prose 核。
      旧实现固定 `src = outline`，导致 (b) 形态被整批误标（实测 27 条，占
      missing_beat 的 37.5%）⇒ P1 证据可信度被人为压到 21%。现改为「任一底本命中即通过」。
    """
    haystack = outline + "\n" + bible
    out: list[dict] = []
    for f in findings:
        if not isinstance(f, dict):
            continue
        t = str(f.get("type") or "")
        if t.startswith("invented_"):
            ent = str(f.get("entity") or "").strip()
            if not ent:
                f["dropped"] = "invented 缺 entity 字段"
                continue
            if len(ent) < 4:
                f["entity_short"] = True  # 短词不做字符串否决（防"辣椒"⊂"辣椒面"误杀）
            elif ent in haystack:
                f["dropped"] = f"entity「{ent}」在细纲/bible 中存在（模型幻觉，复核否决）"
                continue
        ev = str(f.get("evidence") or "").strip()
        if ev:
            # P2-B：阈值降到 6；同时按省略号/省略连接符切片——模型摘录「A……B」摘要时
            # 中间用省略号拼接，原文并不存在「……」字符，需按段分别核验存在性
            segs = [s.strip() for s in re.split(r"…+|…", ev)]
            segs = [s for s in segs if len(s) >= 6]
            # ★双底本：missing_beat 先看细纲、再看正文；其余类型只看正文。任一底本
            #   让所有片段全部命中即判「已证实」。
            srcs = (outline, prose) if t == "missing_beat" else (prose,)
            if not any(all(seg in src for seg in segs) for src in srcs):
                f["evidence_unverified"] = True
        f.setdefault("severity", "P2")
        out.append(f)
    return out


# ★单章审计 prompt 预算（2026-09-27）
# 原实现各段独立截断：outline 9000 + bible 6000 + character_state 10000 = **25k 固定开销**，
# 而 foreshadow / prev_delta / prose **完全没截断** → 单章审计 prompt 实测 **26033 字符**
# → 触发 Gemini 侧「错误占位」→ 冷却重发 ×3 → 502「执行失败」。
# 实证边界：26k 失败、23k 成功 ⇒ 预算取 20k 留足余量。
_AUDIT_PROMPT_BUDGET = 20000
# 审计对象是**正文** ⇒ 正文优先保证（仅防异常超长章），压缩的是每章重复的静态上下文。
_AUDIT_PROSE_MAX = 9000
# ★★确定性正文硬门禁（2026-09-27 新增）
# 起因：ch1–28 审计「P0 全清」之后，全量正则扫描仍发现 **26 处 P0 性质残留被模型漏报**：
#   - 浮空文字 1 处（第006章:129「摸黑看了看那行浮在眼前的字」）
#   - 方括号系统数值 25 处 / 23 章（「【烟火值：1】」…「【烟火值：63】」）
# 这些都是 bible cost_system 的**硬约束**（禁浮空文字 / counter_format=烟火值：{n} 禁【】），
# 但靠 LLM 采样属「偶然报」（ch1 报了、ch6 漏了）⇒ 改为确定性正则，必报。
# 原则：**模式固定的硬约束用代码判，语义判断才交给模型。**
# 通用系统字段方括号：要求内含「：」——「【烟火值：1】」「【解锁：泡面】」都命中，
# 而普通的行内注释「【待补】」不含冒号，不会误报。
_CANON_BRACKET_RE = re.compile(r"【[^】\n]{0,20}[：:][^】\n]{0,20}】")
# 浮空呈现关键词；需上下文窗口内出现 UI/数值相关词，避免「笑容浮在眼前」这类比喻误杀。
# ★2026-09-27 归因补充：原词表漏了「那行字不知什么时候浮了起来」（ch7/8/13）与
#   「眼前忽然浮出一行字」（ch3），共 4 处 P0 漏报。补词后全部命中。
#   刻意**不加**宽泛的「浮现/浮出」——那会误伤「纸背浮出半个『宋』字」（物理显字，合法）
#   与「脑海中浮现出汪万金的名字」（心智描写，合法）。窄词表 + UI 上下文 = 高精度。
_CANON_FLOAT_WORDS = (
    "浮在眼前", "浮起一行", "缓缓浮", "浮出一列", "眼前炸开",
    "面板立起", "虚空里", "半空中浮", "凭空浮",
    "浮了起来", "浮了出来", "浮出一行", "眼前浮出", "浮现一行",
)
_CANON_FLOAT_CTX = re.compile(r"字|字迹|面板|烟火值|界面|提示")
_CANON_FLOAT_WINDOW = 40


def _deterministic_prose_gate(prose: str) -> list[dict]:
    """正文硬门禁：扫描 bible 明令禁止的「系统 UI 感」残留，命中即报 P0。

    确定性（正则），不经过 LLM；只读——只产出 finding，不修改任何内容。
    """
    if not prose:
        return []
    out: list[dict] = []
    seen_line: set[int] = set()

    def _line_of(pos: int) -> int:
        return prose.count("\n", 0, pos) + 1

    for m in _CANON_BRACKET_RE.finditer(prose):
        ln = _line_of(m.start())
        if ln in seen_line:
            continue
        seen_line.add(ln)
        hit = m.group(0)
        is_meter = "烟火值" in hit
        out.append({
            "type": "canon_violation",
            "entity": "烟火值" if is_meter else "系统字段",
            "severity": "P0",
            "detail": f"第{ln}行：「{hit}」写成方括号系统字段，违反 bible "
                      + ("cost_system.counter_format（应为「烟火值：N」）与 perception 硬约束（禁系统 UI 感）"
                         if is_meter else "perception 硬约束（正文不得出现任何系统 UI 字段）"),
            "evidence": hit,
            "fix_hint": "去掉方括号，改成叙事里自然带出（如「她摸出面板看了一眼：烟火值：1」）",
        })

    for kw in _CANON_FLOAT_WORDS:
        for m in re.finditer(re.escape(kw), prose):
            s = max(0, m.start() - _CANON_FLOAT_WINDOW)
            e = min(len(prose), m.end() + _CANON_FLOAT_WINDOW)
            if not _CANON_FLOAT_CTX.search(prose[s:e]):
                continue  # 非 UI 语境（比喻/动作描写），不报
            ln = _line_of(m.start())
            if ln in seen_line:
                continue
            seen_line.add(ln)
            # evidence 取**整行**（不跨行），保证它是 prose 的逐字子串——
            # 复核闸会拿它回源核验，跨行切片若把 \n 抹成空格就会误判「证据未证实」。
            ls = prose.rfind("\n", 0, m.start()) + 1
            le = prose.find("\n", m.end())
            le = len(prose) if le < 0 else le
            out.append({
                "type": "canon_violation",
                "entity": "烟火值面板",
                "severity": "P0",
                "detail": f"第{ln}行：出现浮空文字描写「{kw}」，违反 bible cost_system.perception "
                          f"硬约束（面板是实体，不得凭空浮现）",
                "evidence": prose[ls:le].strip()[:80],
                "fix_hint": "改为角色主动查看实体面板的动作（摸出/掀开/凑到灯前看）",
            })
    return out


# 静态上下文各段权重（按对一致性判定的信息价值排序）
_AUDIT_CTX_WEIGHTS = {
    "outline": 5,            # 细纲：判定「是否脱纲」的基准，最高优先级
    "character_state": 3,    # 角色状态：查数值/状态矛盾
    "bible": 2,              # 世界观设定
    "foreshadow": 1,         # 伏笔台账
    "prev_delta": 1,         # 前章增量
}


def _fit_audit_prompt(prose: str, outline: str, bible: str, character_state: str,
                      foreshadow: str, prev_delta: str) -> dict[str, str]:
    """按预算裁剪审计 prompt 各段：**正文优先**，静态上下文按权重分配剩余额度。

    为什么不能各段独立截断：那样总额不可控（25k 固定 + 未截断段），超预算时
    只知道「总量爆了」却压不掉任何一段。这里先扣掉正文，再把余量按权重分给
    静态段，总额恒定 ≤ _AUDIT_PROMPT_BUDGET。
    """
    prose = (prose or "")[:_AUDIT_PROSE_MAX]
    ctx = {
        "outline": outline or "",
        "bible": bible or "",
        "character_state": character_state or "",
        "foreshadow": foreshadow or "",
        "prev_delta": prev_delta or "",
    }
    # 各段自身未超份额 → 不截断，留出的余量不强行填满（短文本不该被填充）
    budget_ctx = max(0, _AUDIT_PROMPT_BUDGET - len(prose))
    total_w = sum(_AUDIT_CTX_WEIGHTS.values())
    for k, w in _AUDIT_CTX_WEIGHTS.items():
        share = int(budget_ctx * w / total_w)
        if len(ctx[k]) > share:
            ctx[k] = ctx[k][:share] + "…（已按预算截断）"
    ctx["prose"] = prose
    return ctx


# —— P3-12 自创数字检测（2026-10-25）：正文「数字+剧情量词」不在细纲 → fabricated_number ——
# 根因实证：第001章正文凭空出现「五百份遮瑕膏，单价十块灵石，供货周期二十天」，
# 而细纲承诺是「两块下品灵石」；现有 missing_beat 只查「少写」不查「多编」，
# 数字漂移既没人拦，还被 promise_audit 用正文反推洗白。
_FAB_NUM_RE = re.compile(
    r"([零一二两三四五六七八九十百千]+|\d+(?:\.\d+)?)\s*(?:块|枚|颗)\s*(?:下品|中品|上品|极品)?灵石"
    r"|([零一二两三四五六七八九十百千]+|\d+(?:\.\d+)?)\s*(?:份|瓶|支|包)(?:[一-龥]{0,4})?")
_FAB_NUM_STOP = ("一份", "一瓶", "一支", "一包", "一块灵石也没", "几分", "几瓶", "几支", "几包")


def _fabricated_numbers_gate(prose: str, outline: str) -> list[dict]:
    """确定性自创数字检测：正文「数字+剧情量词」短语在细纲中无等值 → P2 finding。

    只报「细纲完全不存在该数字」的凭空数字；数值冲突（两块→十块）由写章链
    数字锚审计（pipeline._numeric_anchor_violations）按 P1 级阻断，此处互补盲区。"""
    if not prose or not outline:
        return []
    out: list[dict] = []
    seen: set[str] = set()

    def _val(s: str):
        s = s.strip()
        if s.isdigit():
            return s
        m = {"一": "1", "二": "2", "两": "2", "三": "3", "四": "4", "五": "5",
             "六": "6", "七": "7", "八": "8", "九": "9", "十": "10"}
        if s in m:
            return m[s]
        if "百" in s:
            parts = s.split("百")
            h = m.get(parts[0], "1") if parts[0] else "1"
            rest = parts[1] if len(parts) > 1 and parts[1] else None
            return str(int(h) * 100 + (int(_val(rest)) if rest else 0))
        if "十" in s:
            parts = s.split("十")
            tens = m.get(parts[0], "1") if parts[0] else "1"
            ones = m.get(parts[1], "0") if len(parts) > 1 and parts[1] else "0"
            return str(int(tens) * 10 + int(ones))
        return None

    for m in _FAB_NUM_RE.finditer(prose):
        phrase = m.group(0).strip()
        if phrase in seen or any(st in phrase for st in _FAB_NUM_STOP):
            continue
        # 虚指（一支/一瓶）与细纲已有数字跳过
        if phrase.startswith(("一支", "一瓶", "一名", "一人")):
            continue
        num = _val(m.group(1) or m.group(2) or "")
        if num is None:
            continue
        seen.add(phrase)
        if phrase in outline:
            continue
        ln = prose.count("\n", 0, m.start()) + 1
        out.append({
            "type": "fabricated_number",
            "entity": phrase,
            "severity": "P2",
            "detail": f"第{ln}行：「{phrase}」为正文自创数字（细纲无此数字或此数值）——"
                      "数字类设定只能来自细纲/bible，凭空数字会让后续章被迫继承错误锚点",
            "evidence": phrase,
            "fix_hint": "改为细纲已有数字，或删去具体数值改为模糊表达（「一批」「数十块」）",
        })
    return out[:10]


def audit_chapter(book: Path, chapter_id: int, on_token=None) -> dict:
    """单章一致性审计：证据装配（正文+细纲+bible+角色状态+伏笔+前章 delta）
    → LLM 抽取 → 确定性复核 → issue 元数据装配 → 报告 dict（不落盘）。

    只读：本函数不写入任何正文/追踪文件。
    """
    target = _resolve_existing_body(book, chapter_id)
    if target is None:
        return {"chapter": chapter_id, "ok": False, "error": "无已提交正文", "findings": []}
    prose = target.read_text(encoding="utf-8")
    outline, outline_src = _read_outline_text(book, chapter_id)
    bible = _read_bible_text(book)
    character_state = _read_character_state(book)
    foreshadow = _read_foreshadow(book)
    prev_delta = _read_prev_delta(book, chapter_id)
    if not outline.strip():
        return {"chapter": chapter_id, "ok": False, "error": f"细纲缺失（来源: {outline_src}）", "findings": []}

    parts = _fit_audit_prompt(prose, outline, bible, character_state, foreshadow, prev_delta)
    prompt = (
        f"# 第{chapter_id}章 一致性审计\n"
        f"<outline>\n{parts['outline']}\n</outline>\n\n"
        f"<bible>\n{parts['bible']}\n</bible>\n\n"
        f"<character_state>\n{parts['character_state']}\n</character_state>\n\n"
        f"<foreshadow>\n{parts['foreshadow']}\n</foreshadow>\n\n"
        f"<prev_delta>\n{parts['prev_delta']}\n</prev_delta>\n\n"
        f"<prose>\n{parts['prose']}\n</prose>"
    )
    last_err = ""
    runs: list[dict] = []
    # P0 稳定性双采样（temperature=0.1 仍偶发漏报 P0，单次 LLM 输出不确定；
    # 对 P0 级做两次独立采样，发现取并集，避免「第一次报了 invented_setting、第二次漏了」
    # 导致最高严重度发现随机丢失）
    for sample in range(2):
        attempt = 1
        while attempt <= 2:  # 每个 sample 内部仍允许 2 次解析重试
            p = prompt if (sample == 0 and attempt == 1) else \
                prompt + (f"\n\n【上次输出未通过解析：{last_err}】请只输出合法 JSON 对象。" if attempt > 1 else "\n\n【第2次独立采样】请严格按协议重新输出。")
            try:
                # ★2026-09-27 根治：_writer_stream 返回 **(text, finish_reason)** 元组，
                #   原实现 `raw = _writer_stream(...)` 未解包 → raw 是 tuple →
                #   _parse_audit_json 内部 .strip() → AttributeError:
                #   "'tuple' object has no attribute 'strip'" → **每章审计必失败**
                #   （报告里全部记成「审计输出解析失败」）。
                #   这是确定性 bug，与 prompt 体量/模型无关 —— 审计功能此前实际完全不可用。
                raw, _finish_reason = _writer_stream(
                    _AUDIT_SYSTEM, p, on_token=on_token, temperature=0.1)
                data = _parse_audit_json(raw)
                runs.append(data)
                break
            except runtime.AgentCancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
                attempt += 1
        if sample == 0:
            last_err = ""  # 采样间重置（第 2 次采样不需要带第 1 次的解析失败信息）
    if not runs:
        return {"chapter": chapter_id, "ok": False, "error": f"审计输出解析失败：{last_err}", "findings": []}

    all_findings: list[dict] = []
    seen = set()
    for data in runs:
        for f in data.get("findings", []):
            if not isinstance(f, dict):
                continue
            key = f"{f.get('type')}|{f.get('entity')}|{str(f.get('detail'))[:30]}"
            if key in seen:
                continue
            seen.add(key)
            all_findings.append(f)

    # must_beats / entities 取两次采样中最长的一份（更完整）
    best = max(runs, key=lambda r: len(str(r.get("must_beats", []))) + len(str(r.get("entities", {}))))

    # ★确定性硬门禁（2026-09-27）：bible 硬约束残留（方括号数值 / 浮空面板）由正则必报，
    #   不再依赖 LLM 采样偶然命中。并入 findings 后同样过一遍 _recheck 做 evidence 核实。
    all_findings.extend(_deterministic_prose_gate(prose))
    # P3-12（2026-10-25）：自创数字确定性检测并入（missing_beat 只查少写，此处查多编）
    all_findings.extend(_fabricated_numbers_gate(prose, outline))

    findings = _recheck_findings(all_findings, prose, outline, bible)
    dropped = [f for f in all_findings if isinstance(f, dict) and f.get("dropped")]
    findings.sort(key=lambda f: _SEVERITY_ORDER.get(str(f.get("severity")), 9))
    return {
        "chapter": chapter_id,
        "ok": True,
        # 正文 mtime：断点续跑的失效判据（正文被改写过 → 上次审计结果作废）
        "prose_mtime": target.stat().st_mtime,
        "schema_version": _AUDIT_SCHEMA_VERSION,
        "outline_source": outline_src,
        "must_beats": best.get("must_beats", []),
        "entities": best.get("entities", {}),
        "findings": _assign_issue_meta(chapter_id, findings),
        "dropped_by_recheck": dropped,
        "prose_path": target.relative_to(book).as_posix(),
    }


_AUDIT_PROGRESS_FILE = "_audit_progress.json"

# 审计结论的「配方版本」。改动判定逻辑（prompt / _recheck / 硬门禁）时必须 +1，
# 否则断点续跑会静默复用按旧配方得出的结论。
# 初值取 1 且把「字段缺失」也视为 1：本次改造未动判定逻辑，已有的 28 章结果
# 仍然有效，不该因为加了持久化就被迫全部重跑。
_AUDIT_SCHEMA_VERSION = 1


def _audit_report_path(out_dir: Path, chapter_id: int) -> Path:
    return out_dir / f"第{chapter_id:03d}章.json"


def _read_done_report(out_dir: Path, chapter_id: int) -> dict | None:
    """读取上次审计留存的单章报告；不存在/损坏/非 dict/配方版本过期 → None。"""
    p = _audit_report_path(out_dir, chapter_id)
    if not p.exists():
        return None
    try:
        rep = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — 损坏的留存不该阻断审计，当作无留存重跑
        return None
    if not isinstance(rep, dict):
        return None
    if int(rep.get("schema_version", _AUDIT_SCHEMA_VERSION)) != _AUDIT_SCHEMA_VERSION:
        return None  # 旧配方产物：判定逻辑已变，不能复用
    return rep


def _prose_unchanged(book: Path, chapter_id: int, rep: dict) -> bool:
    """断点续跑复用判据：正文自上次审计后未被改动。

    只认 mtime 精确相等（容错 1e-6 浮点噪声）。正文被改写 → 审计结论即失效，必须重跑。
    """
    target = _resolve_existing_body(book, chapter_id)
    if target is None:
        return False
    try:
        return abs(float(rep.get("prose_mtime") or -1.0) - target.stat().st_mtime) < 1e-6
    except OSError:
        return False


def _write_json_atomic(path: Path, payload: dict) -> None:
    """原子写 JSON：先写 .tmp 再 replace，避免读到半截文件（中断/超时时尤重要）。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _write_progress(out_dir: Path, start: int, end: int, done: list[int],
                    failed: list[int], finished: bool) -> None:
    _write_json_atomic(out_dir / _AUDIT_PROGRESS_FILE, {
        "range": [start, end],
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "done": sorted(done),
        "failed": sorted(failed),
        "total_chapters": end - start + 1,
        "finished": finished,
    })


def run_outline_audit(book: Path, start: int, end: int, progress=None,
                      on_writer_token=None, resume: bool = True) -> dict:
    """区间批量一致性审计（只读，无任何正文写入）。

    progress: 可选回调 (name, ok, detail)；name 形如 audit_ch36 / report。
    resume: 断点续跑开关（默认开）。已有 ok 报告且正文未改动的章直接复用、不消耗 LLM。

    返回 {ok, range, chapters: [...], report_md}；报告落盘 追踪/脱纲审计/。
    """
    book = book.resolve()
    out_dir = book / _TRACKING_DIR / _AUDIT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    chapters: list[dict] = []
    done: list[int] = []
    failed: list[int] = []
    reused: list[int] = []

    def _emit(name: str, ok: bool, detail: str) -> None:
        if progress:
            try:
                progress(name, ok, detail[:300])
            except runtime.AgentCancelled:
                raise
            except Exception:  # noqa: BLE001
                pass

    for ch in range(start, end + 1):
        # ★断点续跑：上次已产出 ok 报告且正文未变动 → 直接复用（不消耗 LLM）
        if resume:
            prev = _read_done_report(out_dir, ch)
            if prev and prev.get("ok") and _prose_unchanged(book, ch, prev):
                chapters.append(prev)
                done.append(ch)
                reused.append(ch)
                _emit(f"audit_ch{ch}", True,
                      f"复用上次结果（正文未改动）：发现 {len(prev.get('findings', []))} 项")
                continue
        try:
            rep = audit_chapter(book, ch, on_token=on_writer_token)
        except runtime.AgentCancelled:
            # 被终止/预算耗尽：已完成的章已逐章落盘，进度先记一笔再抛出
            _write_progress(out_dir, start, end, done, failed, finished=False)
            raise
        except Exception as exc:  # noqa: BLE001 — 单章审计失败不中断区间
            rep = {"chapter": ch, "ok": False, "error": f"审计异常: {exc}", "findings": []}
        # ★逐章即时落盘：旧实现是「全部章节跑完后」才统一写，中途任何中断 = 零产出。
        #   28 章实测 70~110 分钟，这个窗口太大，必须每章一写。
        _write_json_atomic(_audit_report_path(out_dir, rep["chapter"]), rep)
        if not rep.get("ok"):
            failed.append(ch)
            _emit(f"audit_ch{ch}", False, str(rep.get("error"))[:200])
            _write_progress(out_dir, start, end, done, failed, finished=False)
            chapters.append(rep)
            continue
        done.append(ch)
        n = len(rep["findings"])
        sev = {}
        for f in rep["findings"]:
            sev[f["severity"]] = sev.get(f["severity"], 0) + 1
        _emit(f"audit_ch{ch}", True,
              f"发现 {n} 项（{'/'.join(f'{k}×{v}' for k, v in sorted(sev.items())) or '无'}）；细纲来源 {rep.get('outline_source')}")
        _write_progress(out_dir, start, end, done, failed, finished=False)
        chapters.append(rep)

    # 汇总 MD（只读审计产物，不影响正文/追踪状态）
    md_path = out_dir / f"汇总_第{start:03d}-{end:03d}章_{datetime.now():%Y%m%d_%H%M%S}.md"
    md_path.write_text(_render_summary_md(book, start, end, chapters), encoding="utf-8")
    _write_progress(out_dir, start, end, done, failed, finished=True)
    _emit("report", True, f"报告: {md_path.relative_to(book).as_posix()}")

    total = sum(len(c.get("findings", [])) for c in chapters)
    return {
        "ok": True,
        "mode": "audit",
        "range": [start, end],
        "chapters": chapters,
        "total_findings": total,
        "report_md": md_path.relative_to(book).as_posix(),
        # 断点续跑增量字段（纯附加，不改既有字段语义）
        "reused_chapters": sorted(reused),
        "failed_chapters": sorted(failed),
    }


def _render_summary_md(book: Path, start: int, end: int, chapters: list[dict]) -> str:
    lines = [
        f"# 一致性审计汇总（{book.name} 第{start}-{end}章）",
        f"- 时间：{datetime.now():%Y-%m-%d %H:%M}",
        f"- 章数：{len(chapters)}；发现总数：{sum(len(c.get('findings', [])) for c in chapters)}",
        f"- 模式：只读诊断（不修改正文）；修复由报告「建议动作」驱动人工决策",
        "",
        "| 章 | 发现 | P0 | P1 | P2 |",
        "|---|---|---|---|---|",
    ]
    for c in chapters:
        fs = c.get("findings", [])
        sev = {s: sum(1 for f in fs if f.get("severity") == s) for s in ("P0", "P1", "P2")}
        if not c.get("ok"):
            lines.append(f"| {c['chapter']} | 审计失败：{c.get('error', '')[:40]} | | | |")
            continue
        lines.append(f"| {c['chapter']} | {len(fs)} | {sev['P0'] or ''} | {sev['P1'] or ''} | {sev['P2'] or ''} |")
    lines.append("")
    for c in chapters:
        if not c.get("ok") or not c.get("findings"):
            continue
        lines.append(f"## 第{c['chapter']}章")
        for f in c["findings"]:
            lines.append(f"- **[{f.get('severity')}/{f.get('type')}]** {f.get('detail')}"
                         + (f"（entity: {f.get('entity')}）" if f.get("entity") else "")
                         + (" ⚠️证据未证实" if f.get("evidence_unverified") else "")
                         + f" → 建议：{f.get('suggestion', 'accept')}")
            if f.get("evidence"):
                lines.append(f"  - 证据：{f['evidence'][:120]}")
        if c.get("dropped_by_recheck"):
            lines.append(f"- （复核否决 {len(c['dropped_by_recheck'])} 条幻觉发现）")
        lines.append("")
    return "\n".join(lines)
