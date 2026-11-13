#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nww_enforce.py — NWW-V12 纯 skill 模式的「确定性机械执行层」

移植自 _archive/server-legacy-20260829/pipelines/scene_writer.py 的 enforce /
_patch_plants_fallback / _realm_guard / crack-meter 纠偏（+ rule_checker 的
plant/realm/cost 判定哲学），使其不依赖原 Python 管线即可运行（server/ 已于
2026-08-29 归档至 _archive/server-legacy-20260829/）。

职责（模型无关、确定性、fail-closed 兜底）：
  1. 机械删除可删负向类（系统流痕迹 / 蓝图泄漏词 / 路标词 / 模板比喻 /
     排比堆砌 / 不是而是句式 / 情绪直接命名 / 对话标签循环 / AI式升华结尾 /
     章节摘要式结尾 / 升华金句收尾）
  2. markdown 残留清洗（*斜体*、**粗体**、`代码` → 取内容，仅对无中文内容生效，保护合法中文强调）
  3. 括号对白 → 引号（≤5字功法名强调保留）
  4. 代价不可逆机械修复（裂纹/代价附近删愈合/修复/消退类词）
  5. 计数行（crack meter）机械纠偏：正文计数低于历史值时抬回
  6. 必种硬锚缺失 → 逐类注入一句合规补种句（动机锚点 / 专属比喻 / 反派败相）；
     代价数值由 crack_meter_correct 纠偏，文学呈现归属 Writer，enforce 不替 Writer 生成独立数值行
  7. 主角境界守卫（只补不压：state 缺主角境界先由 bible.json 主角 identity 兜底取，
     仍缺用正文主角语境峰值补入；已有则只做矛盾检查，绝不强改已有状态）——对应 V12 A/F 修复

用法：
  python nww_enforce.py --book <书项目根> --file <正文.md> [--skill <skill根>]
  读 --file 正文，原地写回清洗后文本；findings 以 JSON 打到 stdout。

书项目根约定（与 nww-novel-skill 内部布局兼容，纯 skill 新书的布局）：
  <book>/bible.json         世界观/角色/代价系统/签名比喻/反派败相（主角境界兜底源）
  <book>/state.json        当前状态快照（含 crack_count / protagonist.cultivation；缺境界时从 bible.json 兜底）
  <book>/chapters/NN/final.md  （可选；本脚本只吃 --file 路径）
"""
import argparse
import json
import os
import re
import sys

# 让同目录脚本可被 import（nww_backup 提供写前原稿备份；
# nww_text 提供段落切分的单一权威实现）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nww_backup
import nww_text

# ---------- 中文数字 ----------
CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
          "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_num(s):
    if not s:
        return 0
    if s in CN_NUM:
        return CN_NUM[s]
    if s == "十":
        return 10
    if s.startswith("十"):
        return 10 + CN_NUM.get(s[1:], 0)
    if s.endswith("十"):
        return CN_NUM.get(s[0], 1) * 10
    if "十" in s:
        a, b = s.split("十", 1)
        return CN_NUM.get(a, 1) * 10 + CN_NUM.get(b, 0)
    return 0


def _cn_or_int(tok):
    try:
        return int(tok)
    except (ValueError, TypeError):
        return _cn_num(tok)


def _chapter_num(path):
    """从 md 路径解析章号（如「第5章_过府.md」→ 5）；无匹配返回 None。"""
    m = re.search(r"第(\d+)章", os.path.basename(path))
    return int(m.group(1)) if m else None


def _protagonist_name(bible, state=None):
    if state and (state.get("protagonist") or {}).get("name"):
        return state["protagonist"]["name"]
    for c in (bible or {}).get("characters", []):
        if c.get("role") == "主角":
            return c["name"]
    chars = (bible or {}).get("characters", [])
    return chars[0]["name"] if chars else ""


def _bible_realm(bible, protagonist_name=""):
    """从 bible.json 主角 identity 兜底抽取境界（如「炼气三层」）；无匹配返回空串。

    新书只建 bible.json + _tracking-state.json、尚无 state.json 时，境界锚
    （protagonist.cultivation / floor_realm）从这里兜底取，避免首章锚点落空。
    """
    for c in (bible or {}).get("characters", []):
        if protagonist_name and c.get("name") != protagonist_name:
            continue
        if c.get("role") == "主角" or c.get("name") == protagonist_name:
            m = re.search(r"炼气([一二三四五六七八九十]+)层", c.get("identity", "") or "")
            if m:
                return f"炼气{m.group(1)}层"
    return ""


# ---------- 负向词表加载 ----------
DELETE_NAMES = ["系统流痕迹", "蓝图泄漏词", "路标词", "模板比喻", "排比堆砌",
                "不是而是句式", "情绪直接命名", "对话标签循环", "AI式升华结尾",
                "章节摘要式结尾", "升华金句收尾"]
ENDING_NAMES = ["章节摘要式结尾", "升华金句收尾"]


def load_negative_patterns(skill_root):
    p = os.path.join(skill_root, "references", "negative_patterns.yaml")
    try:
        with open(p, "r", encoding="utf-8") as f:
            txt = f.read()
    except Exception:
        return []
    # 极简 yaml 解析（仅覆盖 patterns: - name:/regex: 结构，避免引入 PyYAML 依赖）
    def _strip_quotes(s):
        s = s.strip()
        if len(s) >= 2 and s[0] in "'\"" and s[-1] == s[0]:
            s = s[1:-1]
        return s
    pats = []
    cur = None
    for line in txt.splitlines():
        m = re.match(r"\s*-\s*name:\s*(.+)", line)
        if m:
            cur = {"name": _strip_quotes(m.group(1))}
            pats.append(cur)
            continue
        m = re.match(r"\s*regex:\s*(.+)", line)
        if m and cur is not None:
            cur["regex"] = _strip_quotes(m.group(1))
    return pats


# ---------- 删除孤儿标点护栏（2026-10-12 修复）----------
# 事故：DELETE_NAMES 的正则是**句中片段匹配**（如「不是而是句式」=
# `不是[^。]{0,10}，而是[^。]{0,10}`），删完留下半截句子。实测：
#   第028章:77 `那种稳，不是灵气浓郁度提升，而是一种扎根感。`
#     → `不是…{0,10}，而是…{0,10}` 只吃到「不是灵气浓郁度提升，而是一种扎根感」的
#       前半（{0,10} 上限使正则提前停手）→ 剩 `那种稳，。`
#   《用泡面征服修仙界》第036章 现有 3 行纯 `。`，同源。
# 修法（根因层，不是事后补丁）：删除**与残句检测绑定**——
#   删完先清孤儿标点，若仍产生「比删除前更多的过短残句/纯标点段」，则**整类不删**。
#   fail-closed：宁可不删，也不许把正文删成碎片。
# ★ 判定必须**窄**：只丢「作者不可能写出」的行 —— 纯句末标点行（。／，／、／；／：及其组合）。
#   绝不含 … ！ ？ — ·：
#     · 独立 `……` 行是有意义的省略，MEMORY REVIEW-FINALIZE §6 把「独立 …… 行删除」
#       明确列为 finalize 后写盘者造成的**损伤**，不是垃圾（第一版误删，已收窄）。
#     · `！`／`？` 单独成行可作语气停顿；`—`／`·` 可作分隔。
#   宁可多留一行可疑标点，也不许删掉作者有意写出的省略号。
_PURE_PUNCT_RE = re.compile(r"^[，。、；：\s]+$")
_ORPHAN_PAIRS = (("，。", "。"), ("、。", "。"), ("：。", "。"), ("；。", "。"),
                 ("。。", "。"), ("，，", "，"), ("；；", "；"), ("：：", "："))
_MIN_SENT_CHARS = 6


def _clean_orphan_punct(text: str) -> str:
    """清理删除留下的孤儿标点：合并重复标点 + 丢弃纯标点行。"""
    prev = None
    while prev != text:
        prev = text
        for a, b in _ORPHAN_PAIRS:
            text = text.replace(a, b)
    # 纯标点行整行丢弃（这类行只可能由删除产生）
    kept = [ln for ln in text.split("\n") if not (ln.strip() and _PURE_PUNCT_RE.match(ln))]
    text = "\n".join(kept)
    return re.sub(r"\n{3,}", "\n\n", text)


def _short_sentences(text: str) -> int:
    """统计「过短残句」数量（去空白后 < _MIN_SENT_CHARS 字的句子）。"""
    n = 0
    for seg in re.split(r"[。！？…\n]", text):
        s = re.sub(r"\s", "", seg)
        if 0 < len(s) < _MIN_SENT_CHARS:
            n += 1
    return n


# ---------- 硬性损伤检查（2026-10-12 元审查补的第二层）----------
# 外脑裁决：「去空白后 <6 字的句子数增加」只能作**辅助告警**，不能作核心判据 ——
#   误拦：删对了但句子边界重划，短句数上升；
#   漏拦：删坏了但残句 ≥6 字，指标不变。
# 故补一层**不依赖句长**的硬性损伤检查，命中即拒绝（与短句告警是 OR 关系）：
#   ① 悬空标点（，。／、。／：。／；。）——事故样本 `那种稳，。`
#   ② 空段落/空行块增加
#   ③ 引号不配对（"「」" 数量奇偶性被破坏）
#   ④ 方括号【】不配对
# 这层是**结构性**判据，不受"作者本来就爱写短句"影响。
_DANGLING_PUNCT_RE = re.compile(r"[，、；：]\s*。")


def _count_dangling(text: str) -> int:
    return len(_DANGLING_PUNCT_RE.findall(text))


def _quotes_balanced(text: str) -> bool:
    return (text.count('"') % 2 == 0
            and text.count("“") == text.count("”")
            and text.count("‘") == text.count("’")
            and text.count("「") == text.count("」")
            and text.count("『") == text.count("』")
            and text.count("【") == text.count("】"))


def _empty_paras(text: str) -> int:
    return sum(1 for p in re.split(r"\n\s*\n", text) if p.strip() and _PURE_PUNCT_RE.match(p))


_BROKEN_SYNTAX_RE = re.compile(
    r"(?:根本|忍不住|不由得|不禁|竟然|居然|却是|而是|不是|并非|绝非|正在|已经|将要|试图|准备|无法|法支取|绝山)[，。！？；、]"
    r"|(?:根本)[、，。！？；]"
    r"|[，。！？；、]{2,}"
)


def _count_broken_syntax(text: str) -> int:
    return len(_BROKEN_SYNTAX_RE.findall(text))


def _has_hard_damage(before: str, after: str) -> str:
    """返回损伤原因；无损伤返回 ""。结构性判据 + 句法完整性护栏。"""
    if _count_dangling(after) > _count_dangling(before):
        return "产生悬空标点"
    if _empty_paras(after) > _empty_paras(before):
        return "产生空的标点段落"
    if _quotes_balanced(before) and not _quotes_balanced(after):
        return "引号/括号不再配对"
    if _count_broken_syntax(after) > _count_broken_syntax(before):
        return "产生半截残句或副词悬空"
    return ""


def _safe_clause_sub(rx: str, prose: str) -> str:
    """仅当匹配项处于独立分句边界（首尾紧邻标点或行首行尾）时才允许删除，禁止句中挖词导致截断。"""
    bound_left = set("，。！？；：、“「『（\n\r \t")
    bound_right = set("，。！？；：、”」』）\n\r \t")

    def _repl(m: re.Match) -> str:
        s, e = m.start(), m.end()
        left_ok = (s == 0) or (prose[s - 1] in bound_left)
        right_ok = (e == len(prose)) or (prose[e] in bound_right)
        if left_ok and right_ok:
            return ""
        return m.group(0)

    return re.sub(rx, _repl, prose)


def _guarded_delete(prose: str, rx: str, rule_name: str = "") -> tuple[str, str]:
    """删 rx；若位于句中非分句边界、或清理后产生句法损伤/过短残句则**放弃删除**（返回原文）。

    特别处理：对于「不是而是句式」，若命中形如「不是A，而是B」，优先无损改写为「并非A，实为B」，
    绝不直接抹除导致句中宾语/谓语断裂（Round 3 R3-01 修复）。
    """
    try:
        if rule_name == "不是而是句式":
            out = re.sub(
                r"不是([^，。！？；\n]{1,10})，而是([^，。！？；\n]{1,10})",
                r"并非\1，实为\2",
                prose,
            )
            if out != prose and not _has_hard_damage(prose, out):
                return out, "deleted"
            return prose, "no_match" if out == prose else "refused"
        out = _safe_clause_sub(rx, prose)
    except re.error:
        return prose, "bad_regex"
    if out == prose:
        try:
            if re.search(rx, prose):
                return prose, "refused"
        except re.error:
            return prose, "bad_regex"
        return prose, "no_match"
    out = _clean_orphan_punct(out)
    # 第一层（硬）：结构性损伤 —— 悬空标点 / 空标点段 / 引号括号失配 / 副词悬空 → 直接拒绝
    hard = _has_hard_damage(prose, out)
    if hard:
        return prose, "refused"
    # 第二层（软）：过短残句增量 —— 仅作辅助告警，命中即拒绝（宁可不删）
    if _short_sentences(out) > _short_sentences(prose):
        return prose, "refused"      # 会产生残句 → 不删
    return out, "deleted"


def _apply_delete_stage(prose: str, pats: dict) -> tuple[str, list]:
    """机械删除可删类（2026-10-26 Round 3：句中边界护栏 + 孤儿标点护栏，删除失败即放弃）。

    返回 (新正文, findings)。单独成函数以便回归测试直接调用（与 enforce 同一条路径）。
    """
    findings: list = []
    for nm in DELETE_NAMES:
        if nm in pats:
            prose2, st = _guarded_delete(prose, pats[nm]["regex"], rule_name=nm)
            if st == "deleted":
                prose = prose2
                findings.append({"type": "delete", "name": nm})
            elif st == "refused":
                findings.append({"type": "delete_skipped", "name": nm,
                                 "reason": "删除将产生残句/孤儿标点，已放弃"})
            else:
                findings.append({"type": "delete_none", "name": nm, "reason": st})
    # 终态孤儿标点清理（幂等，只清垃圾）：既收掉本次删除的残留，也顺带修掉
    # **存量**被删坏的正文（实测《泡面》第036章 3 行纯 `。`，2026-09-14 事故遗留）。
    # 纯标点行不可能由作者写出，只可能由机械删除产生 → 丢弃安全且幂等。
    return _clean_orphan_punct(prose), findings


# ---------- 机械执行层 ----------
def enforce(prose, book, bible, state, skill_root):
    findings = []
    pats = {p["name"]: p for p in load_negative_patterns(skill_root) if "regex" in p}
    prose, del_findings = _apply_delete_stage(prose, pats)
    findings.extend(del_findings)
    # 2) markdown 残留机械清除（仅对无中文内容生效，保护合法中文强调）
    def _strip_md(m):
        inner = m.group(1)
        if re.search(r"[一-鿿]", inner):
            return m.group(0)
        return inner
    prose = re.sub(r'(?<!["“「])\*{1,2}([^*\n]{1,80})\*{1,2}(?!["”」])', _strip_md, prose)
    prose = re.sub(r'`([^`\n]{1,40})`',
                   lambda m: m.group(1) if not re.search(r"[一-鿿]", m.group(1)) else m.group(0), prose)
    # 3) 清残留多逗号/空格
    prose = re.sub(r"([，,])\s*([，,])", "，", prose)
    prose = re.sub(r"^\s*[，,]\s*", "", prose, flags=re.M)
    # 4) 括号对白 → 引号（≤5字功法名强调保留）
    #    【】默认视为系统面板/信息条目（系统提示、面板记录、功法名），保留【】不转引号；
    #    仅命中对白特征（言语动词说/道/问/喊/叫/答、语气词结尾、感叹/疑问句）才转引号。
    #    修复：旧逻辑"≥6字即转"会误伤面板长条目（如【未注册棋子·行为画像建档】）。
    def _br(m):
        inner = m.group(1)
        if re.search(r"(说道|问道|喊道|叫道|答道|喝道|嘟囔|招呼|喊话)", inner) or \
           re.search(r"[他她我你][说问道喊叫答][：:]", inner) or \
           re.search(r"[啊吧吗呢呀嘛]$", inner) or \
           inner.rstrip().endswith(("！", "？")):
            return f"“{inner}”"
        return m.group(0)
    prose = re.sub(r"【([^【】]+)】", _br, prose)
    prose = re.sub(r"【】", "", prose)  # 清理负向词删空后残留的空括号
    prose = re.sub(r"\n{3,}", "\n\n", prose)
    # 5) 代价不可逆机械修复
    cs = (bible or {}).get("cost_system")
    device = (cs or {}).get("device") or "裂纹"
    ci_kw = ["愈合", "修复", "消退", "恢复", "好转", "减轻", "消失"]
    for kw in ci_kw:
        n = len(prose)
        prose = re.sub(rf'({re.escape(device)}[^。！？]{{0,15}}){kw}', r'\1', prose)
        if len(prose) != n:
            findings.append({"type": "cost_irreversible_fixed", "device": device, "kw": kw})
    # 6) 结尾泄漏类：机械丢弃泄漏末段（不依赖 LLM）
    #    段切分走 nww_text 容错口径。原实现是 prose.rstrip().split("\n\n")：
    #    单换行正文下 paras == [整章]，paras[-1] 就是整章，命中负向正则会
    #    把**整章删光**（body = "\n\n".join([]) = ""）。除改用容错切分外，
    #    再加一条 len(paras) >= 2 的硬护栏：只有 1 个真实段落时绝不执行「丢末段」。
    paras = nww_text.split_paragraphs(prose.rstrip())[0]
    last = paras[-1]
    if len(paras) >= 2 and any((nm in pats and re.search(pats[nm]["regex"], last)) for nm in ENDING_NAMES):
        body = "\n\n".join(paras[:-1])
        findings.append({"type": "ending_leak_dropped"})
        prose = body.rstrip()
    # 7) 引号风格修复：番茄平台 "「" → ""，自动替换
    platform = (bible or {}).get("platform", "")
    if platform in ("番茄", ""):
        if '「' in prose or '」' in prose:
            prose = prose.replace('「', '"').replace('」', '"')
            findings.append({"type": "quote_style_fixed", "name": "quote_style", "detail": "「」已替换为\"\""})
    return prose, findings


# ---------- 计数行（crack meter）纠偏 ----------
def _meter_re(cs):
    if not cs:
        return re.compile(r"当前裂纹[：:]\s*(\d+)")
    mr = cs.get("meter_regex")
    if mr:
        try:
            return re.compile(mr)
        except re.error:
            pass
    return re.compile(r"当前裂纹[：:]\s*(\d+)")


def load_tracking(book):
    """从 _tracking-state.json 读取完整追踪状态（千章机制唯一权威）；无则返回 {}。"""
    try:
        with open(os.path.join(book, "追踪", "_tracking-state.json"), "r", encoding="utf-8") as f:
            st = json.load(f)
        return st or {}
    except Exception:
        return {}


def load_tracking_cost(book):
    """从 _tracking-state.json 读取权威代价计数（千章机制唯一权威）；无则返回 {}。"""
    return load_tracking(book).get("cost") or {}


def crack_meter_correct(prose, bible, state, tracking_cost=None, allow_lift=True):
    cs = (bible or {}).get("cost_system")
    if not cs:
        return prose, None
    device = cs.get("device", "裂纹")
    # 抬升目标唯一权威 = _tracking-state.json 的 cost 当前值（千章机制）。
    # 不再把 bible.cost_system.current（初始值/下限）与 state.crack_count（V12 legacy，已停写，
    # 见 long-write/SKILL.md）混进 max 当抬升目标：前者一旦被某书误当“规划总值”会让全章计数
    # 被拉平到该值，后者恒为 0 属冗余。二者即使用也只应是“下限”，不参与把正文抬到峰值的对齐。
    # 章位感知护栏（fail-safe）：仅当本文件章号不低于已提交的最新章时才允许按权威值抬升，
    # 避免对 ch1-5 之类历史章做批量回测/回溯时，把 1→2→2→3 的进展弧线拉平成全局峰值。
    if not allow_lift:
        return prose, None
    prev_cc = (tracking_cost or {}).get(device, 0)
    tpl = cs.get("meter_template") or (f"{device}：{{n}}" if device else "当前裂纹：{n}/7")
    mrx = _meter_re(cs)
    changed = False
    def fix(m):
        nonlocal changed
        v = _cn_or_int(m.group(1))
        if prev_cc and v < prev_cc:
            changed = True
            return tpl.format(n=prev_cc)
        return m.group(0)
    prose = mrx.sub(fix, prose)
    return prose, ("crack_meter_lifted" if changed else None)


# ---------- 必种硬锚兜底注入 ----------
def plant_fallback(prose, bible, tracking_cost=None):
    added = []
    # 容错切分：单换行正文下按空行硬切会把整章当成 1 段，硬锚会插到整章的头/尾；
    # 用 nww_text 后段号正确。末尾 "\n\n".join(paras) 同时把正文归一为长篇协议。
    paras = nww_text.split_paragraphs(prose)[0]
    if not paras:
        return prose, added
    prot_name = _protagonist_name(bible)

    def ins_at(idx, sent):
        paras.insert(min(idx, len(paras)), sent)

    # 硬锚注入总开关（fail-closed：默认关，跨题材安全）。
    # bible.plant_fallback.enabled=True 的某本书显式开启时，才恢复强制补种；
    # 否则仅上报缺漏给 LLM/rule_checker 层，不向正文注入模板腔句子，避免任何题材被机械腔污染。
    pf_cfg = (bible or {}).get("plant_fallback") or {}
    plant_on = bool(pf_cfg.get("enabled", False))

    # 动机锚点
    anchor = next((c.get("anchor", "") for c in bible.get("characters", []) if c.get("anchor")), "")
    if anchor and not any(anchor in p for p in paras):
        if plant_on:
            idx = 1
            for i, p in enumerate(paras):
                if prot_name in p:
                    idx = i + 1
                    break
            ins_at(idx, f"{prot_name}心中默念：「{anchor}」")
            added.append("motivation_anchor")
        else:
            added.append("motivation_anchor_missing")
    # 专属比喻种子（题材化：注入文案由 bible.signature_metaphor.metaphor_template 提供，
    # 可含 {seed} 占位；未提供模板时不注入，避免修仙措辞错配到非修仙题材）。
    # 与动机锚点相同，纳入 plant_on 总开关：关闭时不向正文注入固定比喻句，
    # 仅标记缺漏交由 writer/revision 自然融入，避免结构性制造「像」式模板腔。
    sm = (bible or {}).get("signature_metaphor") or {}
    seed = sm.get("seed")
    tpl = sm.get("metaphor_template")
    if seed and tpl and seed not in prose:
        if plant_on:
            ins_at(max(0, len(paras) - 1),
                   f"{prot_name}忽然觉得，{tpl.format(seed=seed)}")
            added.append("signature_metaphor")
        else:
            added.append("signature_metaphor_missing")
    # 反派败相（兜底一句）
    # bible.villain_down_beats 是全局败相伏笔句（非 V12 单章战败关键词），故不沿用
    # 「躺在地上…倒在这一拳之下」动作模板（会与剧情矛盾）；改为把作者败相短语
    # 以悬念旁白收束，且不用破折号（避免触发 AI 味 em-dash 检查）。
    vdb = bible.get("villain_down_beats") or []
    if vdb and not any(k in prose for k in vdb):
        if plant_on:
            beat = next((k for k in vdb if k not in prose), vdb[0])
            # 补种句去掉「此事记下，日后自有应验」追踪腔，避免跨题材模板污染；
            # 仅织入原作者定制的败相短语本身，交给开启硬锚的书自行取舍
            ins_at(len(paras), beat)
            added.append("villain_down_beat")
        else:
            added.append("villain_down_beat_missing")
    return "\n\n".join(paras), added


# ---------- 主角境界守卫（只补不压）----------
def realm_guard(prose, new_state, bible, protagonist_name="", floor_realm=None):
    cn = {v: k for k, v in CN_NUM.items()}
    def disp(x):
        return cn.get(x, str(x))
    pnums = []
    if protagonist_name:
        for m in re.finditer(rf"{re.escape(protagonist_name)}[^。！？\n]{{0,14}}炼气([一二三四五六七八九十]+)层", prose):
            pnums.append(_cn_num(m.group(1)))
        if not pnums:
            for m in re.finditer(rf"炼气([一二三四五六七八九十]+)层[^。！？\n]{{0,14}}{re.escape(protagonist_name)}", prose):
                pnums.append(_cn_num(m.group(1)))
    prose_peak = max(pnums) if pnums else None
    cult = (new_state.get("protagonist") or {}).get("cultivation", "")
    if not cult:
        # G3: state 缺主角境界时，从 bible.json 主角 identity 兜底取（新书尚无 state.json / 双轨断裂）
        cult = _bible_realm(bible, protagonist_name)
    m = re.search(r"炼气([一二三四五六七八九十]+)层", cult)
    if not m:
        if prose_peak is not None:
            new_state.setdefault("protagonist", {})["cultivation"] = f"炼气{disp(prose_peak)}层"
            return new_state, f"状态补入主角境界(正文主角语境峰值): 炼气{disp(prose_peak)}层"
        return new_state, None
    state_num = _cn_num(m.group(1))
    notes = []
    if prose_peak is not None and state_num > prose_peak:
        notes.append(f"状态境界(炼气{disp(state_num)}层)高于正文主角峰值(炼气{disp(prose_peak)}层)")
    if floor_realm:
        fm = re.search(r"炼气([一二三四五六七八九十]+)层", floor_realm)
        if fm and state_num < _cn_num(fm.group(1)):
            notes.append(f"状态境界低于权威下限{floor_realm}")
    if notes:
        return new_state, "境界矛盾待人工核对: " + "; ".join(notes)
    return new_state, None


# ---------- 入口 ----------
def quality_advisory(book: str) -> dict:
    """V12.3 新增：轻量创作质量告警（advisory，不阻断提交）。

    三项检查：
    1. 主角主动性：最近5章 declared.protagonist_initiative 连续 false → 告警
    2. 声明与观察不一致：declared=true 但 observed.initiative_keywords=0 → 告警
    3. 规则获取重复：archive 中最近3次规则事件 actual.method 完全相同 → 告警
    """
    import os as _os
    import json as _json
    tracking_dir = _os.path.join(book, "追踪")
    state_path = _os.path.join(tracking_dir, "_tracking-state.json")
    warnings = []

    # 读取 bible.creative.forbidden_patterns
    try:
        with open(_os.path.join(book, "bible.json"), "r", encoding="utf-8") as f:
            bible = _json.load(f)
        forbidden = (bible.get("creative") or {}).get("forbidden_patterns") or []
    except Exception:
        forbidden = []

    # 1 & 2: 读取最近5章逐章记录的 quality_meta
    delta_dir = _os.path.join(tracking_dir, "逐章记录")
    recent_chapters = []
    if _os.path.isdir(delta_dir):
        for fname in sorted(_os.listdir(delta_dir)):
            m = re.match(r"第(\d+)章\.md", fname)
            if m:
                recent_chapters.append((int(m.group(1)), _os.path.join(delta_dir, fname)))
    recent_chapters.sort(key=lambda x: x[0])
    last5 = recent_chapters[-5:]

    initiative_false_count = 0
    mismatch_count = 0
    for ch_num, ch_path in last5:
        try:
            with open(ch_path, "r", encoding="utf-8") as f:
                content = f.read()
            # 从逐章记录的「## 质量元数据」段提取
            qm_section = ""
            in_qm = False
            for line in content.split("\n"):
                if line.startswith("## 质量元数据"):
                    in_qm = True
                    continue
                if in_qm and line.startswith("## "):
                    break
                if in_qm:
                    qm_section += line + "\n"
            declared_match = re.search(r"主角主动性（声明）：(是|否)", qm_section)
            observed_match = re.search(r"主动行为关键词命中：(\d+) 处", qm_section)
            if declared_match:
                declared_val = declared_match.group(1) == "是"
                if not declared_val:
                    initiative_false_count += 1
                observed_val = int(observed_match.group(1)) if observed_match else 0
                if declared_val and observed_val == 0:
                    mismatch_count += 1
        except Exception:
            continue

    if len(last5) >= 3 and initiative_false_count >= 3:
        consecutive = 0
        for ch_num, ch_path in reversed(last5):
            try:
                with open(ch_path, "r", encoding="utf-8") as f:
                    content = f.read()
                qm_section = ""
                in_qm = False
                for line in content.split("\n"):
                    if line.startswith("## 质量元数据"):
                        in_qm = True
                        continue
                    if in_qm and line.startswith("## "):
                        break
                    if in_qm:
                        qm_section += line + "\n"
                declared_match = re.search(r"主角主动性（声明）：(是|否)", qm_section)
                if declared_match and declared_match.group(1) == "否":
                    consecutive += 1
                else:
                    break
            except Exception:
                break
        if consecutive >= 3:
            warnings.append({
                "type": "QUALITY_WARNING",
                "check": "protagonist_initiative",
                "severity": "MEDIUM",
                "message": f"最近{consecutive}章主角主动性（声明）连续为否，建议增加主动目标或选择",
            })
    if mismatch_count > 0:
        warnings.append({
            "type": "QUALITY_WARNING",
            "check": "initiative_declared_vs_observed",
            "severity": "LOW",
            "message": f"{mismatch_count}章声明主角主动但未检测到主动行为关键词，建议核实",
        })

    # 3: 规则获取重复检查（从 archive/rule_history.json 读取）
    archive_path = _os.path.join(tracking_dir, "档案", "rule_history.json")
    if _os.path.exists(archive_path):
        try:
            with open(archive_path, "r", encoding="utf-8") as f:
                archive = _json.load(f)
            # 按 lost_chapter 排序，取最近3次
            events = sorted(archive.values(), key=lambda e: e.get("lost_chapter", 0))
            last3 = events[-3:]
            if len(last3) >= 3:
                methods = [e.get("actual", {}).get("method", "") for e in last3]
                if all(m == methods[0] and m for m in methods):
                    msg = f"最近3次规则事件获取方式均为「{methods[0]}」，建议变化获取方式"
                    if any(p in methods[0] for p in forbidden):
                        msg += "（命中 forbidden_patterns）"
                    warnings.append({
                        "type": "QUALITY_WARNING",
                        "check": "rule_acquisition_repetition",
                        "severity": "HIGH",
                        "message": msg,
                    })
        except Exception:
            pass

    return {
        "mode": "quality_advisory",
        "book": book,
        "warnings": warnings,
        "summary": f"{len(warnings)} 项质量提醒（advisory，不阻断提交）",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", required=True, help="书项目根（含 bible.json/state.json）")
    ap.add_argument("--file", required=False, help="待清洗正文 md 路径（quality-advisory 模式下不需要）")
    ap.add_argument("--skill", default=None, help="skill 根（含 references/negative_patterns.yaml）；缺省取脚本父目录")
    ap.add_argument("--quality-advisory", action="store_true", help="V12.3：轻量创作质量告警（advisory，不阻断提交；不需要 --file）")
    args = ap.parse_args()

    if args.quality_advisory:
        result = quality_advisory(args.book)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if not args.file:
        print(json.dumps({"error": "--file is required unless --quality-advisory is used"}, ensure_ascii=False))
        sys.exit(2)

    skill_root = args.skill or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(args.file, "r", encoding="utf-8") as f:
        prose = f.read()
    original = prose

    def load_json(name, default):
        try:
            with open(os.path.join(args.book, name), "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default

    bible = load_json("bible.json", {})
    state = load_json("state.json", {})

    all_findings = []
    prose, f1 = enforce(prose, args.book, bible, state, skill_root)
    all_findings += f1
    tracking = load_tracking(args.book)
    # 章位感知：解析本文件章号；不低于已提交最新章才允许 crack meter 按权威值抬升
    cur_ch = _chapter_num(args.file)
    last_committed = tracking.get("last_committed_chapter")
    allow_lift = True
    if cur_ch is not None and isinstance(last_committed, int) and cur_ch < last_committed:
        allow_lift = False
    prose, f2 = crack_meter_correct(prose, bible, state,
                                    tracking_cost=tracking.get("cost"), allow_lift=allow_lift)
    if f2:
        all_findings.append({"type": f2})
    prose, planted = plant_fallback(prose, bible, tracking_cost=tracking.get("cost"))
    # 清掉注入产生的纯标点/空段落残片，并用 "\n\n" 重拼 → 正文归一为长篇协议。
    # 切分必须走容错口径：用 split("\n\n") 时单换行正文的 _paras == [整章]，
    # 重拼等于原样保留单换行格式，enforce 的归一就白跑了。
    _paras = [p for p in nww_text.split_paragraphs(prose)[0]
              if p.strip() and not re.fullmatch(r"[\s。，、！？…：]+", p.strip())]
    prose = "\n\n".join(_paras)
    for p in planted:
        all_findings.append({"type": "planted_fallback", "name": p})

    # 境界守卫：读回刚刚可能补种的 state 之前，先用纯正文做只补不压（state 由 long-write 主流程写）
    prot = _protagonist_name(bible, state)
    # P0-3: 境界守卫只对修炼/实力等级题材生效（regex 识别「炼气X层」），非此类题材跳过，
    # 避免历史/都市等题材被死代码耦合或误判
    genre = (bible or {}).get("genre", "") or ""
    if any(k in genre for k in ("修仙", "修真", "玄幻", "仙侠", "炼气", "练气")):
        floor_realm = (state.get("protagonist") or {}).get("cultivation", "")
        if not floor_realm:
            # G3: 境界锚从 bible.json 兜底取，作为权威下限
            floor_realm = _bible_realm(bible, prot)
        _, rg = realm_guard(prose, dict(state), bible, protagonist_name=prot, floor_realm=floor_realm)
        if rg:
            all_findings.append({"type": "realm_guard", "note": rg})

    # 写前原稿备份（借鉴 sumeru：修改前自动备份，首份优先、幂等）
    if prose != original:
        try:
            st = nww_backup.backup(args.book, args.file, orig_content=original)
            all_findings.append({"type": "backup", "status": st["status"], "path": st["path"]})
        except Exception as _e:
            all_findings.append({"type": "backup_skipped", "reason": str(_e)})

    with open(args.file, "w", encoding="utf-8") as f:
        f.write(prose)

    # 2026-10-26 同步留痕：若对应章节的 delta 草稿已存在，顺手写入 quality_meta.enforced=True
    if cur_ch is not None:
        import time as _time
        _delta_cand = os.path.join(args.book, "追踪", f"delta_第{cur_ch:03d}章.json")
        if os.path.isfile(_delta_cand):
            try:
                with open(_delta_cand, "r", encoding="utf-8") as _df:
                    _ddata = json.load(_df)
                _tx = _ddata.get("transaction", _ddata) if isinstance(_ddata, dict) else _ddata
                if isinstance(_tx, dict):
                    _qm = _tx.setdefault("delta", {}).setdefault("quality_meta", {})
                    if isinstance(_qm, dict):
                        _qm["enforced"] = True
                        _qm["enforced_at"] = int(_time.time())
                        with open(_delta_cand, "w", encoding="utf-8") as _df:
                            json.dump(_ddata, _df, ensure_ascii=False, indent=2)
            except Exception:
                pass

    print(json.dumps({"file": args.file, "findings": all_findings,
                      "prose_chars": len(prose)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
