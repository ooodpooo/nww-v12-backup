#!/usr/bin/env python3
"""
nww_packet_builder_v2.py — Writing Packet v2 归一化 parser（Phase 1 交付物）

契约依据：docs/Writing_Packet_v2_Contract_定义_20260923.md（v2.1 冻结稿，含 R8）
定位：细纲 Markdown →（确定性解析）→ Writing Packet v2。
设计原则：
  - 只做「归一化已有信息」，不做「剧情解释/推理」（intent.goal 派生只取原文片断，source=derived）
  - 不静默丢字段（R8）：每个 SOURCE 字段要么进 PACKET，要么显式 discard_reason
  - 双发射（R6）：v2 键与 v1 别名并存，未迁移前不改变现有生产行为
  - 只读：不修改细纲、不修改 tracking state

实测格式（2026-09-23，ch14/25/39/40）：
  ## 第一幕 · 批量订单（场景：杂货铺/上午）。D2 事件
  - **A 行动**：…
  - **B 阻碍**：…
  - **D 过程（谈判→算产能→立规矩）**：…
  - **F 结果**：…
  - 对话双职能（E5）：…  /  认知落差（E3）：…  /  设定绑定（E2）：…
  ## 落点（章尾类型 / 主动力 drive / 烟火值 / 伏笔埋设 F0XX / 章末钩子）
  ## 执行层备注（写时须守）（含「禁止…」行）
"""

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

OUTLINE_DIR = "大纲"
TRACKING_DIR = "追踪"
BODY_DIR = "正文"
SCHEMA_VERSION = "2.0"

# Task 3 / C-P0-1：内容存在性硬闸（写前）
def _nww_content_gates():
    # 位置鲁棒：engine/scripts 副本与 skills 副本（同目录即闸脚本）均可解析
    here = Path(__file__).resolve().parent
    candidates = [
        here,
        Path(__file__).resolve().parents[2] / "skills" / "nww-novel-skill" / "scripts",
    ]
    for c in candidates:
        if (c / "nww_content_gates.py").is_file():
            if str(c) not in sys.path:
                sys.path.insert(0, str(c))
            import nww_content_gates as _cg  # noqa: WPS433
            return _cg
    raise ImportError(f"nww_content_gates.py 未找到（候选: {[str(c) for c in candidates]}）")


# Task 6 / C-P1-1：活跃伏笔 = status ∈ {已埋, 需处理}；已收/回收/作废/闭合/关闭 排除。
_ACTIVE_FSH_STATUS = ("已埋", "需处理")
_FSH_IMPORTANCE_ORDER = {"高": 0, "中": 1, "低": 2}
# pending_foreshadows 注入窗口：按 importance+planted 排序后截断，防长书 packet 膨胀
_PENDING_FSH_LIMIT = 12


def iter_active_foreshadows(
    state: dict,
    ledger_rows: list[dict],
    *,
    limit: int | None = None,
) -> list[dict]:
    """合并 state.foreshadow 与 伏笔台账 rows，返回活跃（未收）伏笔。

    status ∈ {已埋, 需处理} 视为活跃；已收/已回收/回收/作废/闭合/关闭 排除。
    按 importance（高>中>低）+ planted_chapter（近埋优先）排序；limit 截断窗口。
    """
    merged: dict[str, dict] = {}
    for fid, row in (state.get("foreshadow") or {}).items():
        if not isinstance(row, dict):
            continue
        merged[str(fid)] = {
            "id": str(fid),
            "summary": str(row.get("summary") or ""),
            "importance": str(row.get("importance") or "中"),
            "planted_chapter": int(row.get("planted_chapter") or 0),
            "status": str(row.get("status") or "已埋"),
        }
    for row in ledger_rows or []:
        if not isinstance(row, dict):
            continue
        fid = str(row.get("id") or "")
        if not fid:
            continue
        merged.setdefault(fid, {
            "id": fid,
            "summary": str(row.get("summary") or row.get("content") or ""),
            "importance": str(row.get("importance") or "中"),
            "planted_chapter": int(row.get("planted_chapter") or row.get("planted") or 0),
            "status": str(row.get("status") or "已埋"),
        })
    active = [r for r in merged.values() if r["status"] in _ACTIVE_FSH_STATUS]
    active.sort(key=lambda r: (
        _FSH_IMPORTANCE_ORDER.get(r["importance"], 1),
        -r["planted_chapter"],
        r["id"],
    ))
    if limit is not None and limit >= 0:
        active = active[:limit]
    return active


# Task 6 / C-P1-4：cost 归一化 {count} / {烟火值:85} / {device,count} → {device: int}
_META_COST_KEYS = ("count", "rule", "note", "备注", "规则")


def normalize_cost_view(cost: dict) -> dict[str, int]:
    """兼容三形：{count} 旧形保留 count；{device: int} 直传；{device, count} → {device: count}。

    非数值 device 值（如 rule 文本）不入视图；主角境界缺失由调用方 BLOCK。
    """
    if not isinstance(cost, dict) or not cost:
        return {}
    out: dict[str, int] = {}
    if "device" in cost and "count" in cost:
        dev = str(cost["device"])
        try:
            out[dev] = int(cost["count"])
        except (TypeError, ValueError):
            pass
        return out
    for k, v in cost.items():
        if k in _META_COST_KEYS:
            if k == "count":
                try:
                    out["count"] = int(v)
                except (TypeError, ValueError):
                    pass
            continue
        if isinstance(v, bool):
            continue
        if isinstance(v, int):
            out[str(k)] = v
        elif isinstance(v, str) and v.strip().isdigit():
            out[str(k)] = int(v.strip())
        elif isinstance(v, float) and v == int(v):
            out[str(k)] = int(v)
    return out


CN_NUMS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5}


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def norm(text: str) -> str:
    return text.strip().replace("\r\n", "\n").replace("\r", "\n")


def _sec(text: str, header_re: str) -> str:
    """提取小节内容（到下一个同级/更高级标题或文件尾）。

    §7-1 修复①：标题层级识别兼容 ## 与 ###（原仅认 ##，导致 ### 段落被漏取；
    仅放宽起始/边界匹配为 #{2,4}，H1 标题不误伤，不改既有语义）。
    """
    # 非捕获组包裹 header_re：防止 header 含「|」时正则优先级拆分支（P2-5 修复）
    m = re.search(rf"^#{{2,4}}\s*(?:{header_re})\s*$.*?(?=^#{{2,4}}\s*|\Z)", text, re.M | re.S)
    return m.group(0).split("\n", 1)[1].strip() if m else ""


def _sec_loose(text: str, header_re: str) -> str:
    """宽松版：标题行允许带后缀（如「出场角色voice锚点（写章硬约束，禁止偏离）」）。

    §7-1 修复①：同 _sec，起始/边界兼容 ## 与 ###。
    """
    m = re.search(rf"^#{{2,4}}\s*(?:{header_re}).*?$.*?(?=^#{{2,4}}\s*|\Z)", text, re.M | re.S)
    return m.group(0).split("\n", 1)[1].strip() if m else ""


# —— C-VOICE-PARSE@1（T1，2026-11-06）：voice 锚点双形态解析 + 章节切片 ——
# 权威形态：`##/###/#### 出场角色voice锚点` 标题小节（归一后由 nww_outline_format_fix.py 产出）。
# 兼容形态：`- **出场角色voice锚点**：` 字段行（历史细纲实存写法，只读兼容，不在此处改写）。
_RE_VOICE_HEADING = re.compile(r"^#{2,4}\s*出场角色[^\n]*voice\s*锚点.*$", re.M)
# 允许字段行带缩进（实测第 2/3 章把该字段行嵌在「人物关系和出场顺序」子项里）。
_RE_VOICE_FIELD = re.compile(
    r"^[ \t]*[-*][ \t]*\*{0,2}出场角色[ \t]*voice[ \t]*锚点\*{0,2}[ \t]*[：:][ \t]*(.*)$", re.M)
_RE_CHAPTER_BLOCK = re.compile(r"^#{1,3}\s*第\s*(\d+)\s*章\b", re.M)   # 章块起点
_RE_VOICE_NEXT_HEADING = re.compile(r"^#{2,4}\s*", re.M)


def _indent_width(line: str) -> int:
    """行首缩进宽度（tab 按 4 空格折算，仅用于比较深浅）。"""
    m = re.match(r"[ \t]*", line)
    return len((m.group(0) if m else "").replace("\t", "    "))


def _voice_anchor_seg(text: str) -> str:
    """返回 voice 锚点正文段（不含标题行）；两种形态都支持；找不到返回 ""。

    权威形态取标题行之后到下一个 `#{2,4}` 标题（与 _sec_loose 同边界）。
    兼容形态取「冒号后同行值 + 更深的子项行」；冒号后为空且无子项时才回落到
    「到下一个标题/文件尾」，以免把后续兄弟小节（如「情节细化」）误当角色条目。
    """
    if not text:
        return ""
    if _RE_VOICE_HEADING.search(text):
        return _sec_loose(text, r"出场角色[^\n]*voice\s*锚点")

    fm = _RE_VOICE_FIELD.search(text)
    if not fm:
        return ""
    line_start = text.rfind("\n", 0, fm.start()) + 1
    field_indent = _indent_width(text[line_start:fm.start() + 1])
    inline = (fm.group(1) or "").strip()
    tail_text = text[fm.end():]
    if tail_text.startswith("\n"):
        tail_text = tail_text[1:]  # 去掉字段行自身的行尾换行，真正的空行仍保留
    rest = tail_text.splitlines()

    if inline:
        # 同行有内容：内容即首条，随后只收缩进更深的子项；首个不更深/非子项行结束。
        body = ["- " + inline]
        for ln in rest:
            if not ln.strip():
                break
            if re.match(r"^[ \t]+-\s+", ln) and _indent_width(ln) > field_indent:
                body.append(ln.strip())
                continue
            break
        return "\n".join(body).strip()

    # 冒号后为空：优先收更深缩进的子项（细纲实存写法）。
    body: list[str] = []
    for ln in rest:
        if not ln.strip():
            if body:
                body.append("")
            continue
        if re.match(r"^[ \t]+-\s+", ln) and _indent_width(ln) > field_indent:
            body.append(ln.strip())
            continue
        break
    if body:
        return "\n".join(body).strip()
    # 无子项的兜底：按契约取到下一个标题行/文件尾（兼容纯段落写法）。
    tail = "\n".join(rest)
    nm = _RE_VOICE_NEXT_HEADING.search(tail)
    return (tail[:nm.start()] if nm else tail).strip()


def _slice_chapter_text(text: str, chapter_id: int) -> tuple[str, str]:
    """章节切片。返回 (region_text, status)；status ∈ {"found","single","missing"}。

    - 文件中不存在 `#{1,3} 第N章` 形态的章块 → ("<text 原文>", "single")   # 单章形态，零回归
    - 存在章块且找到第 chapter_id 块 → (块正文, "found")                   # 块边界=下一个章块或文件尾
    - 存在章块但无第 chapter_id 块   → ("", "missing")                     # 上层报错，禁止回落全文
    """
    blocks = list(_RE_CHAPTER_BLOCK.finditer(text or ""))
    if not blocks:
        return text, "single"
    try:
        want = int(chapter_id)
    except (TypeError, ValueError):
        return "", "missing"
    for idx, m in enumerate(blocks):
        if int(m.group(1)) == want:
            start = m.start()
            end = blocks[idx + 1].start() if idx + 1 < len(blocks) else len(text)
            return text[start:end], "found"
    return "", "missing"


_LETTER_KEY = {"A": "driver_goal", "B": "conflict", "C": "conflict", "D": "decision", "F": "final_state"}
_FIELD_MAP = {
    "driver_goal": {"actor": ["驱动方", "actor"], "goal": ["目标", "goal"],
                    "constraint": ["限制", "constraint"], "failure": ["失败", "failure"]},
    "conflict": {"source": ["来源", "source"], "pressure": ["压力", "pressure"],
                 "forces": ["迫使", "forces"]},
    "decision": {"choice": ["选择", "choice"], "cost": ["代价", "cost"],
                 "benefit": ["收益", "benefit"]},
    "final_state": {"from": ["旧状态", "from"], "to": ["新状态", "to"],
                    "new_issue": ["新增问题", "new_issue"]},
}
_DEFAULT_FIELD = {"driver_goal": "goal", "conflict": "pressure",
                  "decision": "choice", "final_state": "to"}
_SCALE_CN = ("小戏", "中戏", "大戏", "重戏")


def _split_structured(content: str, field_map: dict) -> dict | None:
    """尝试按「键：值」拆结构化子字段；命中 ≥2 个子字段才返回 dict，否则 None。

    CCv2 语义：只认显式子键（驱动方/目标/限制/失败 …）。存量旧句式（无子键的整句）
    返回 None，由调用方落入默认子字段（goal/pressure/choice/to），结构不丢（R8）。
    """
    out = {k: "" for k in field_map}
    hits = 0
    for part in re.split(r"[；;\n]+", content):
        part = part.strip().strip("。，,.")
        if not part:
            continue
        for key, labels in field_map.items():
            m = re.match(rf"^(?:{'|'.join(labels)})\s*[：:=]\s*(.+)$", part)
            if m and not out[key]:
                out[key] = m.group(1).strip()
                hits += 1
                break
    return out if hits >= 2 else None


def _parse_acts(text: str) -> tuple[list[dict], list[str]]:
    """解析 第一~三幕（A/B/D/F + craft notes），返回 (scene_chain, missing_flags)。

    CCv2 语义（v2.2）：A→driver_goal（驱动方/目标/限制/失败）、B→conflict（来源/压力/迫使）、
    D→decision（选择/代价/收益）、F→final_state（旧状态→新状态→新增问题）。
    键名保留（driver_goal/conflict/decision/final_state），仅内容语义升级；幕档位（D2/D3/
    小戏/中戏/大戏/重戏）提取入 scene.scale，由 build_packet_v2 归入 meta（E1/R8，不静默丢）。
    """
    acts: list[dict] = []
    missing: list[str] = []
    # 找所有幕标题：## 第X幕 ·/: 名字（场景：…）。DX 事件
    headers = list(re.finditer(r"^#{2,4}\s*第([一二三四五])幕\s*[·.、：:]\s*([^（\n]+)(?:（场景：([^）]+)）)?.*$", text, re.M))
    for m in headers:
        num, name, setting = m.group(1), m.group(2).strip(), (m.group(3) or "").strip()
        scale = ""
        sm = re.search(r"[。．.]\s*(D[0-9]|小戏|中戏|大戏|重戏)", m.group(0))
        if sm:
            scale = sm.group(1)
        nxt = headers[headers.index(m) + 1] if headers.index(m) + 1 < len(headers) else None
        end_pos = nxt.start() if nxt else len(text)
        h2_after = re.search(r"^##\s+[^#]", text[m.end():end_pos], re.M)
        if h2_after:
            end_pos = m.end() + h2_after.start()
        seg = text[m.end(): end_pos]
        scene = {
            "scene_id": f"S{CN_NUMS.get(num, len(acts)+1)}",
            "act": f"第{num}幕 · {name}",
            "setting": setting or "",
            "scale": scale,
            "driver_goal": {"actor": "", "goal": "", "constraint": "", "failure": ""},
            "conflict": {"source": "", "pressure": "", "forces": ""},
            "decision": {"choice": "", "cost": "", "benefit": ""},
            "final_state": {"from": "", "to": "", "new_issue": ""},
            "craft_notes": [],
        }
        for lm in re.finditer(r"^-\s*\*\*([A-F])\s*([^*]*?)\*\*[：:]\s*(.+)$", seg, re.M):
            letter, content = lm.group(1), lm.group(3).strip()
            content = re.sub(r"^（(.+?)）\s*", "", content)  # 去掉「（谈判→…）」前缀保留内容
            if letter not in _LETTER_KEY:
                continue
            key = _LETTER_KEY[letter]
            structured = _split_structured(content, _FIELD_MAP[key])
            if structured is not None:
                for k, v in structured.items():
                    if v and not scene[key][k]:
                        scene[key][k] = v
            else:
                default = _DEFAULT_FIELD[key]
                scene[key][default] = (scene[key][default] + " / " + content).strip(" /") \
                    if scene[key][default] else content
        # Round 3 R3-02a：兼容中文字段名幕结构（驱动目标 / 情节点 / 冲突 / 决策 / 功能 / 落点）
        for cm_cn in re.finditer(r"^[-*]\s*\*{0,2}(驱动目标|冲突(?:与阻力)?|情节点|主角决策|决策|功能|幕末状态|落点状态)\*{0,2}[：:]\s*(.+)$", seg, re.M):
            cn_k, cn_v = cm_cn.group(1).strip(), cm_cn.group(2).strip()
            if cn_k == "驱动目标" and not scene["driver_goal"]["goal"]:
                scene["driver_goal"]["goal"] = cn_v
            elif cn_k in ("冲突", "冲突与阻力") and not scene["conflict"]["pressure"]:
                scene["conflict"]["pressure"] = cn_v
            elif cn_k == "情节点":
                if not scene["conflict"]["pressure"]:
                    scene["conflict"]["pressure"] = cn_v
                if not scene["decision"]["choice"]:
                    scene["decision"]["choice"] = cn_v
            elif cn_k in ("主角决策", "决策") and not scene["decision"]["choice"]:
                scene["decision"]["choice"] = cn_v
            elif cn_k in ("功能", "幕末状态", "落点状态") and not scene["final_state"]["to"]:
                scene["final_state"]["to"] = cn_v
        for cm in re.finditer(r"^-\s*(对话双职能（E\d+）[：:]?\s*.+?)$|^-\s*(认知落差（E\d+）[：:]?\s*.+?)$|^-\s*(设定绑定（E\d+）[：:]?\s*.+?)$", seg, re.M):
            scene["craft_notes"].append(next(g for g in cm.groups() if g))
        for k in ("driver_goal", "conflict", "decision", "final_state"):
            if not any(scene[k].values()):
                missing.append(f"{scene['act']}缺{k}")
        acts.append(scene)
    return acts[:5], missing


# —— 2026-10-12：细纲 beats 表回退解析（只在「幕格式」解析为空时启用）——
# 背景（实测确诊）：`_parse_acts()` 只认「## 第X幕 · 名字」格式，而实际生产细纲
#   用的是「## 情节细化（beats）」markdown 表格 → 格式不匹配 → scene_chain 恒为 []。
#   实测钦天监 ch12/18/24、诡异时代 ch001-004 全部 scene_chain=0。
# 后果：writer_prompt.py:41 要求模型「必须消费 scene_chain…每项独立落地、不许漏」，
#   而 pipeline.py:909 用它拼 outline → 空则 outline 只剩 intent.goal，
#   细纲里「每个情节点 + 功能 + 执行边界」这一整层根本到不了模型。
# ★ 只增不改：有幕格式的细纲行为完全不变；仅 acts 为空时才回退，且带 env 回滚开关。
_BEATS_FALLBACK_SWITCH = "NWW_BEATS_FALLBACK"
# 表格里"情节点"列的列名变体（宽松匹配，取最长的那列作为正文）
_BEAT_EVENT_KEYS = ("情节点", "情节点（谁做了什么）", "情节", "beat", "事件")
_BEAT_FUNC_KEYS = ("功能", "功能标签")
_BEAT_BOUND_KEYS = ("执行边界", "边界", "约束")


def _beats_fallback_enabled() -> bool:
    """beats 回退解析开关。默认开（1）；置 0 回到修复前的行为。"""
    return os.getenv("NWW_BEATS_FALLBACK", "1") != "0"


# 2026-10-14（外脑 Q2 最小 diff · 只增不改）。
# 把细纲里**已经写好**的「目标情绪 / 爽点」传进 intent 的独立字段，
# 解决 ch2–ch15 用项目符号写法、生产解析器只认标题行导致 14/14 章丢失的问题。
# 开关：默认开；置 0 回到修复前行为（两新字段恒为 ""）。
_INTENT_EXTRA = os.getenv("NWW_INTENT_EXTRA", "1") != "0"


def _field_loose(text: str, key_re: str, cap: int = 300) -> str:
    """取字段值，兼容两种写法，并严格限定在本字段内（不跨字段、不跨章）。

    ① 标题式：`## 目标情绪（…）` 之后、到下一个标题（任意 #1~#6）前的非空内容。
       逐行扫描，遇到任何标题行立即终止；绝不跨入下一字段或下一章（每章一个文件）。
    ② 项目符号式：`- 目标情绪：值` / `- **目标情绪**：值`（半角/全角冒号、有无加粗皆可）。

    缺失或为空 → ""，绝不猜测补齐。重复出现 → **首次命中优先**，不静默拼接
    （若真实资产存在重复项，先审计分布再决定兼容规则；当前采取保守的首命中）。
    实现约束：标题式用逐行扫描而非 `.*?` 跨行通配，避免吞入后续字段或文件尾部。
    """
    lines = text.splitlines()
    # —— 标题式：找到标题行，向下收集直到下一个标题或文件结束 ——
    for i, ln in enumerate(lines):
        if re.match(rf"^#{{2,4}}\s*(?:{key_re})", ln):
            body_lines = []
            for j in range(i + 1, len(lines)):
                nxt = lines[j]
                if re.match(r"^#{1,6}\s", nxt):   # 任意级标题都终止收集
                    break
                body_lines.append(nxt)
            body = "\n".join(body_lines).strip()
            return re.sub(r"\s+", " ", body)[:cap] if body else ""
    # —— 项目符号式 ——
    for ln in lines:
        m = re.match(rf"^[-*]\s*(?:\*\*)?(?:{key_re})(?:\*\*)?\s*[：:]\s*(.+)$", ln)
        if m:
            return m.group(1).strip()[:cap]
    return ""


def _parse_emotion_target(text: str) -> str:
    """细纲「目标情绪」原文（兼容标题式 / 项目符号式）。缺失 → ""。"""
    return _field_loose(text, r"目标情绪")


def _parse_selling_points(text: str) -> str:
    """细纲「爽点」原文（兼容标题式 / 项目符号式）。缺失 → ""。"""
    return _field_loose(text, r"爽点")


# 2026-11-04（Step 2 · 只增不改 + env 回滚，默认关闭）。
# 细纲上下章义务字段（inheritedHookResponsibilities / nextChapterEntryState / readerExperience）
# 解析与写前门禁。默认关（"0"）；置 "1" 开启：planner 产出结构化义务段、packet 解析、
# 写前门禁启用、writer 硬约束注入。关闭时 _parse_outline_contract 返回 None（不污染 packet）。
_OUTLINE_CONTRACT_ENABLED = os.getenv("NWW_OUTLINE_CONTRACT", "0") != "0"

# 读者体验合同九字段（label 用于细纲 bullet 解析，key 用于结构化输出）。
_READER_EXP_FIELDS = (
    ("reader_question", "读者问题"),
    ("promised_reward", "可见回报"),
    ("reward_level", "回报层级"),
    ("protagonist_want", "主角欲望"),
    ("primary_resistance", "主要阻力"),
    ("key_turn", "关键转折"),
    ("emotional_shift", "情绪位移"),
    ("information_reveal", "信息揭示"),
    ("net_change", "净变化"),
)


def _parse_reader_experience(text: str) -> dict:
    """细纲读者体验合同 → 9 字段结构化（宽松解析，缺失 → ""，不猜测补齐）。

    直接在全文中按 bullet 标签定位（避免 _field_loose 的空白折叠破坏逐行匹配）。
    """
    out: dict[str, str] = {}
    for key, label in _READER_EXP_FIELDS:
        m = re.search(rf"^[-*]\s*(?:\*\*)?{label}(?:\*\*)?\s*[：:]\s*(.+)$", text, re.M)
        out[key] = m.group(1).strip() if m else ""
    return out


def _parse_outline_contract(text: str) -> dict | None:
    """细纲 → 上下章义务 + 读者体验合同（结构化）。

    关闭开关或三段全缺 → 返回 None（调用方不注入该键，packet 逐字节不变）。
    """
    if not _OUTLINE_CONTRACT_ENABLED:
        return None
    inherited = _field_loose(text, r"上章承接")
    next_state = _field_loose(text, r"下章铺垫")
    reader_exp = _parse_reader_experience(text)
    if not (inherited.strip() or next_state.strip() or any(reader_exp.values())):
        return None
    return {
        "inherited_hook_responsibilities": inherited,
        "next_chapter_entry_state": next_state,
        "reader_experience": reader_exp,
    }


def _validate_outline_contract(text: str) -> tuple[bool, str]:
    """写前细纲门禁（Step 2）：NWW_OUTLINE_CONTRACT=1 时校验上下章义务字段齐备。

    返回 (is_valid, reason)。关闭开关 → (True, "SKIPPED")（不拦）。
    """
    if not _OUTLINE_CONTRACT_ENABLED:
        return True, "SKIPPED"
    contract = _parse_outline_contract(text)
    if contract is None:
        return False, "NO_OUTLINE_CONTRACT（细纲缺 上章承接/下章铺垫/读者体验合同 任一段且有内容）"
    if not contract["inherited_hook_responsibilities"].strip():
        return False, "MISSING_INHERITED_HOOK（上章承接为空）"
    if not contract["next_chapter_entry_state"].strip():
        return False, "MISSING_NEXT_ENTRY_STATE（下章铺垫为空）"
    required = ("reader_question", "promised_reward", "protagonist_want",
                "primary_resistance", "key_turn")
    missing = [k for k in required if not contract["reader_experience"].get(k, "").strip()]
    if missing:
        return False, "MISSING_READER_EXP_FIELDS（读者体验合同缺：" + ",".join(missing) + "）"
    return True, "VALID"


def _parse_beats(text: str) -> tuple[list[dict], list[str]]:
    """解析「#### 情节细化（beats）」markdown 表格 → scene_chain 形态。

    兼容两种列名变体（表头感知，优先按列名定位；认不出列名才回退「最长列」启发式）：
      | # | 情节点 | 功能 | 执行边界 |
      | # | 情节点（谁做了什么） | 功能标签 | 执行边界 |
    映射（外脑 2026-10-12 决策 2）：
      - 情节点 → driver_goal.goal
      - 功能 / 功能标签 → craft_notes（剧情功能，属 craft 层）
      - 执行边界 → driver_goal.constraint（下游有正式 constraint 字段，优先落这里；
        不无条件塞进 craft_notes，避免与「功能」语义混淆）
      - 原始三列文本 → beats_raw（可审计，绝不静默丢弃）
    只增不改：不触碰 `_parse_acts()`，也不改动 scene_chain 的既有键名结构。
    """
    out: list[dict] = []
    missing: list[str] = []
    if not text:
        return out, ["BEATS_EMPTY_TEXT"]
    # 定位 beats 小节（宽松：允许「情节细化」「情节细化（beats）」「beats」等写法）
    m = re.search(r"^#{2,4}\s*[^\n]*情节细化[^\n]*$|^#{2,4}\s*[^\n]*\bbeats\b[^\n]*$",
                  text, re.M | re.I)
    seg = text[m.end():] if m else ""
    if not seg:
        return out, ["BEATS_SECTION_NOT_FOUND"]
    # 收集所有表格行（含表头/分隔行，后面再判定）
    raw_rows = []
    for line in seg.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 2:
            continue
        raw_rows.append(cells)

    def _is_sep(cells: list[str]) -> bool:
        return all(re.fullmatch(r"[-:\s]*", c or "") for c in cells)

    if not any(not _is_sep(c) for c in raw_rows):
        return out, ["BEATS_NO_ROWS"]

    def _flat(c: str) -> str:
        return c.replace("（", "(").replace("）", ")").replace(" ", "").lower()

    def _has_col(cells: list[str]) -> bool:
        joined = "".join(cells)
        return any(k in joined for k in
                   (_BEAT_EVENT_KEYS + _BEAT_FUNC_KEYS + _BEAT_BOUND_KEYS))

    # —— 表头感知：在「原始行」里找「含列名 + 下一行是分隔行」的那一行 ——
    header_idx = None
    for i, cells in enumerate(raw_rows):
        if _has_col(cells) and i + 1 < len(raw_rows) and _is_sep(raw_rows[i + 1]):
            header_idx = i
            break
    if header_idx is None:
        # 宽松兜底：无分隔行时，第一行含列名也算表头
        for i, cells in enumerate(raw_rows[:1]):
            if _has_col(cells):
                header_idx = i
                break
    header = raw_rows[header_idx] if header_idx is not None else []

    def _col(keys: tuple[str, ...]) -> int | None:
        for j, c in enumerate(header):
            cj = _flat(c)
            for k in keys:
                if _flat(k) in cj:
                    return j
        return None

    ev_i = _col(_BEAT_EVENT_KEYS)
    fn_i = _col(_BEAT_FUNC_KEYS)
    bd_i = _col(_BEAT_BOUND_KEYS)
    # 数据行 = 表头之后的所有非分隔行（表头不存在时取全部非分隔行）
    body = raw_rows[header_idx + 1:] if header_idx is not None else raw_rows
    data_rows = [c for c in body if not _is_sep(c)]

    for i, cells in enumerate(data_rows, 1):
        # 列定位可用 → 精确取值；不可用 → 回退「最长列」启发式（兼容列名变体与列序差异）
        if ev_i is not None and ev_i < len(cells):
            event = cells[ev_i].strip()
        else:
            rest = cells[1:] if len(cells) >= 3 else cells
            event = max(rest, key=len).strip() if rest else ""
        if not event or event in ("—", "-", "无"):
            continue
        func = cells[fn_i].strip() if (fn_i is not None and fn_i < len(cells)) else ""
        bound = cells[bd_i].strip() if (bd_i is not None and bd_i < len(cells)) else ""
        if not bound and ev_i is not None:
            # 没认出边界列 → 取既非情节点也非功能的最长剩余列（保守，不静默丢弃）
            others = [c.strip() for j, c in enumerate(cells)
                      if j not in (ev_i, fn_i) and c.strip() and c.strip() != event]
            bound = max(others, key=len) if others else ""
        out.append({
            "scene_id": f"B{i}",
            "act": f"beat{i}",
            "setting": "",
            "scale": "",
            "driver_goal": {"actor": "", "goal": event, "constraint": bound, "failure": ""},
            "conflict": {"source": "", "pressure": "", "forces": ""},
            "decision": {"choice": "", "cost": "", "benefit": ""},
            "final_state": {"from": "", "to": "", "new_issue": ""},
            "craft_notes": [func] if func else [],
            "beats_raw": {"情节点": event, "功能": func, "执行边界": bound},
            "source": "beats_fallback",
        })
    if not out:
        missing.append("BEATS_PARSED_EMPTY")
    return out[:8], missing


def _parse_characters(text: str, chapter_id: int | None = None) -> tuple[list[dict], list[str]]:
    """解析 出场角色voice锚点（权威标题小节 / 兼容字段行双形态）。

    chapter_id 非空时先按章块切片（多章合一细纲只取目标章，禁止跨章回落）；
    块不存在 → ([], ["章块不存在:第N章"])，由上层决定报错。
    """
    chars: list[dict] = []
    missing: list[str] = []
    if chapter_id is not None:
        region, status = _slice_chapter_text(text, chapter_id)
        if status == "missing":
            return [], [f"章块不存在:第{int(chapter_id)}章"]
        text = region
    # 双形态统一入口：权威标题 / 兼容字段行（见 _voice_anchor_seg）。
    seg = _voice_anchor_seg(text)
    # §7-1 修复②b：条目名格式兼容「- **名**：」「- 名：」与无前缀裸行「姜禾：①…」
    #（2026-10-25 实测：prealign 回补块条目为裸行，原 `^-\s*` 强制短横线导致全丢）。
    for lm in re.finditer(r"^[-*\s]*\*{0,2}(.+?)\*{0,2}[：:]\s*(.+)$", seg, re.M):
        name, body = lm.group(1).strip(), lm.group(2).strip()
        # 2026-10-26 Round 3 R3-02b：子字段行（「①口癖/句式：…」「②动作习惯：…」）不是角色条目；
        # 同时兼容带 ①②③ 的条目与标准分号/句号分隔的声纹条目（如 `- **沈砚**：冷峻克制…；口头禅…；禁…`）。
        if re.search(r"[①-⑩]", name):
            continue
        role = "新角色" if "新角色" in name else ""
        name_clean = re.sub(r"（.*?）|\(.*?\)", "", name).strip()
        if not name_clean or len(name_clean) > 12 or any(w in name_clean for w in ("口癖", "动作习惯", "禁用", "注意", "要求")):
            continue
        if "①" in body:
            parts = [p.strip() for p in re.split(r"[①-⑩]", body) if p.strip()]
        else:
            parts = [p.strip() for p in re.split(r"[；;。]", body) if p.strip()]
        if not parts:
            continue
        behavior = [p for p in parts if ("禁用" in p or p.startswith("禁") or "不得" in p)]
        voice = [p for p in parts if p not in behavior]
        chars.append({
            "name": name_clean,
            "role": role,
            "voice_constraints": voice,
            "behavior_constraints": behavior,
            "state": "",
        })
    if not chars:
        missing.append("出场角色voice锚点")
    return chars, missing


# —— §1 intent.ending 三级优先级（2026-10-11 冻结契约，docs/Writing_Packet_v2_§7-1字段契约冻结稿）——
# T1 严格「落点」（保持现状，泡面存量）→ T2「结尾设定和钩子」+ 一个明确配对的中文括号后缀
# （钦天监 ch2–ch27，含 ch16 `（三层递进）`）→ T3 行内兜底（仅当全文无章节级 T1/T2 标题时启用）。
# 有界后缀：核心标题精确等于「结尾设定和钩子」，仅允许一个 `（…）`；`…扩展` / 无括号后缀不命中；
# `决策脊柱落点（…）` 天然不命中（禁止改用宽松匹配）。
_RE_LANDING_T1 = re.compile(r"^#{2,4}[ \t]*落点[ \t]*$", re.M)
_RE_LANDING_T2 = re.compile(r"^#{2,4}[ \t]*结尾设定和钩子(?:[ \t]*（[^（）]*）)?[ \t]*$", re.M)
_RE_LANDING_T3 = re.compile(r"^-[ \t]*结尾设定[：:][ \t]*(.+)$", re.M)
_RE_LANDING_NEXT = re.compile(r"^#{2,4}\s*", re.M)  # 边界语义与 _sec 一致


def _landing_block_after(text: str, end: int) -> str:
    """取标题行（结束于 end）之后、到下一个 #{2,4} 标题或文件尾之间的内容（strip）。"""
    m = _RE_LANDING_NEXT.search(text, end)
    seg = text[end: m.start()] if m else text[end:]
    return seg.strip()


def _resolve_landing_section(text: str) -> dict:
    """按冻结优先级 T1>T2>T3 解析章尾落点正文，返回 {body, tier, heading, line, conflicts}。

    冻结规则（契约 §1.3）：T3 仅当全文无任何章节级 T1/T2 标题时启用；命中 T1/T2 但区块内容
    为空则**禁止回退** T3（body=""，由调用方判 missing）；T1&T2 并存取 T1；同优先级出现多候选
    取文档顺序首个、**不合并**，冲突进构建期审计（stderr），不写主 Packet（Q2）。
    """
    hits_t1 = list(_RE_LANDING_T1.finditer(text))
    hits_t2 = list(_RE_LANDING_T2.finditer(text))
    conflicts: list[str] = []
    if hits_t1 and hits_t2:
        conflicts.append("T1&T2并存->取T1")
    if len(hits_t1) > 1:
        conflicts.append(f"T1多候选({len(hits_t1)})->取首个")
    if len(hits_t2) > 1:
        conflicts.append(f"T2多候选({len(hits_t2)})->取首个")
    if hits_t1:
        m, tier = hits_t1[0], "T1"
    elif hits_t2:
        m, tier = hits_t2[0], "T2"
    else:
        m3 = _RE_LANDING_T3.search(text)
        if m3:
            return {"body": m3.group(1).strip(), "tier": "T3",
                    "heading": m3.group(0).strip(),
                    "line": text[:m3.start()].count("\n") + 1, "conflicts": conflicts}
        # T4 兜底（2026-10-25 P0-1 契约断裂修复，不改变 T1>T2>T3 冻结优先级）：
        # 细纲主文「- 章尾钩子：…」bullet 行 → 无 # 前缀 FAIL 补充块的「章尾钩子」块。
        # 实测：仙门书细纲主文只有 bullet 章尾钩子，T1/T2/T3 全 MISS → missing 误报「落点」。
        m4 = re.search(r"^[-*]\s*\*{0,2}(?:E\d+\s*)?(?:章尾钩子|Ending\s*State|结尾设定)\*{0,2}[：:]\s*(.+)$", text, re.M | re.I)
        if m4:
            return {"body": m4.group(1).strip()[:600], "tier": "T4-bullet",
                    "heading": m4.group(0).strip()[:60],
                    "line": text[:m4.start()].count("\n") + 1, "conflicts": conflicts}
        for _t, _b in _fail_blocks(text):
            if "章尾钩子" in _t and _b:
                return {"body": _b[:600], "tier": "T4-fail",
                        "heading": _t[:60],
                        "line": 0, "conflicts": conflicts}
        return {"body": "", "tier": "MISS", "heading": "", "line": 0, "conflicts": conflicts}
    return {"body": _landing_block_after(text, m.end()), "tier": tier,
            "heading": m.group(0).strip(),
            "line": text[:m.start()].count("\n") + 1, "conflicts": conflicts}


def _parse_landing(text: str) -> tuple[dict, list[dict], list[str]]:
    """解析章尾落点：返回 (landing 各字段, foreshadow_refs, missing)。

    §1 兼容修复（2026-10-11 冻结契约）：`intent.ending` 经 `_resolve_landing_section`
    按 T1>T2>T3 三级优先级取值——原仅 T1 严格「落点」，致钦天监 27 章 `ending` 全空、
    `meta.missing` 误报「落点」。`ending` 与 missing 由**同一解析结果**决定（契约 §1.3）。
    **`drive` 与 `foreshadow_refs` 语义不变**，仍取 T1「落点」小节：本任务只解决
    intent.ending，不改变其他字段行为（§1 实施边界，禁止借机重构）。

    foreshadow_refs 只认「伏笔埋设：/伏笔：」声明行（裁决：只接受细纲显式声明 F-code），
    排除「活跃伏笔（tracking 更新枚举）」这类登记行——那是 tracking 状态行，不是本章声明。
    """
    seg = _sec(text, r"落点")  # T1 严格「落点」小节：drive / foreshadow_refs 的唯一来源（语义不变）
    res = _resolve_landing_section(text)
    if res["conflicts"]:
        # 冲突诊断只进构建期审计（stderr，同 [S71-*] 约定），不写主 Packet（§1.3 provenance）
        sys.stderr.write(f"[S1-LANDING] tier={res['tier']} line={res['line']} "
                         f"heading='{res['heading']}' conflicts={res['conflicts']}\n")
    missing: list[str] = []
    ending = res["body"][:600]
    drive = ""
    m = re.search(r"主动力\s*drive[：:]\s*(.+)$", seg, re.M)
    if m:
        drive = m.group(1).strip()[:300]
    refs: list[dict] = []
    for ln in seg.splitlines():
        stripped = re.sub(r"^[-*\s]+", "", ln)
        # 只处理伏笔声明行；活跃伏笔/烟火值/章尾类型/容量等行跳过
        if not stripped.startswith(("伏笔埋设", "伏笔：")):
            continue
        for fm in re.finditer(r"F\d{3,4}", stripped):
            fid = fm.group(0)
            rest = stripped[fm.end():]
            desc = re.split(r"[；;]\s*(?:F\d{3,4}|$)|$", rest, maxsplit=1)[0].strip()[:120]
            refs.append({"id": fid, "source": "outline", "declared_text": desc})
    # 去重（同一 id 出现多次保留首次）
    seen: dict[str, dict] = {}
    for r in refs:
        seen.setdefault(r["id"], r)
    return {"ending": ending, "drive": drive}, list(seen.values()), missing


def _parse_wordcount(text: str) -> tuple[dict, str]:
    raw = ""
    m = re.search(r"字数目标\*{0,2}[：:]\s*([^\n]+)", text)
    if m:
        raw = m.group(1).strip()
    metric_m = re.search(r"字数口径\*{0,2}[：:]\s*([^\n]+)", text)
    metric = metric_m.group(1).strip() if metric_m else "visible_chars_v1"
    nums = re.findall(r"\d+", raw)
    if len(nums) >= 2:
        target, upper = int(nums[0]), int(nums[1])
    elif len(nums) == 1:
        target = upper = int(nums[0])
    else:
        target = upper = 0
    return {"declared": raw, "target": target, "min": target, "max": upper, "metric": metric}, raw


def _parse_exec_notes(text: str) -> tuple[list[str], list[str]]:
    keep, avoid = [], []
    seg = _sec_loose(text, r"执行层备注")
    if seg:
        for ln in seg.splitlines():
            ln = re.sub(r"^[-*\s]+", "", ln).strip()
            if not ln:
                continue
            if ln.startswith("禁止"):
                avoid.append(ln[:300])
            else:
                keep.append(ln[:300])
    return keep, avoid


def _parse_character_function(text: str) -> list[dict]:
    """解析 角色功能 小节（- **名**：为什么在本章出现/推动什么）。CCv2 新增字段。

    边界：只取「本章功能」，不做人物小传（性格/背景/外貌/语言风格归角色卡）。
    """
    seg = _sec_loose(text, r"角色功能|Character Function")
    out: list[dict] = []
    for lm in re.finditer(r"^-\s*\*\*(.+?)\*\*[：:]\s*(.+)$", seg, re.M):
        name = re.sub(r"（.*?）", "", lm.group(1)).strip()
        fn = lm.group(2).strip()[:200]
        if name and fn:
            out.append({"name": name, "function": fn})
    return out


def _merge_character_function(chars: list[dict], funcs: list[dict]) -> list[dict]:
    """把 角色功能 并入 character_contract（按名匹配；新角色追加条目）。"""
    by_name = {c["name"]: c for c in chars}
    for f in funcs:
        c = by_name.get(f["name"])
        if c is not None:
            c["function"] = f["function"]
        else:
            chars.append({"name": f["name"], "role": "", "voice_constraints": [],
                          "behavior_constraints": [], "state": "", "function": f["function"]})
    for c in chars:
        c.setdefault("function", "")
    return chars


# —— Role Lifecycle Contract v1（2026-09-23 外脑裁决冻结）——
# 双维度分离：entity_type（实体性质）与 lifecycle.classification（生命周期）不绑死，
# 组织身份（faction）从生命周期主类型拆出，避免未来组织角色成长路径耦合。
# 职称词 → faction_member 信号（确定性，不用 LLM）
RANK_WORDS = ["使者", "执事", "长老", "阁主", "宗主", "掌门", "护法", "司命", "司库"]
# 聚合占位：群体性、无具名，不进入角色体系
AGGREGATE_NAMES = {"坊市群像", "坊市众人", "各方势力", "围观修士", "群像", "众人"}
# 泛称（黑风谷守门人反例）：未具名个人/群体（境界+泛称/职业+泛称），有 voice 约束但非长期角色
GENERIC_SUFFIX = ("修士", "丹修", "小修", "商贩", "路人", "围观", "顾客", "客人", "弟子", "商贾", "学徒")
# recurring 证据（确定性规则，非 LLM）
_RECURRING_HINTS = [
    "新角色", "主角团队", "徒弟", "学徒", "收徒", "拜师", "伙伴", "同伴",
    "长期", "后续", "未来", "再次出现", "贯穿", "支线",
]


def _entity_type_for(name: str, role: str, func: str, own_text: str) -> str:
    """实体性质：person / faction_member / aggregate（organization 类由 faction_member 承担，聚合占位排除）。

    只依据该角色自己的文本（voice/behavior 约束 + 功能 + 名字），不做跨章猜测——
    细纲未给身份信号时诚实判 person（跨章身份归 R2 人工确认修正）。
    """
    if name in AGGREGATE_NAMES or any(a in name for a in ("群像", "众人", "修士们", "势力")):
        return "aggregate"
    haystack = f"{name} {role} {func} {own_text}"
    if any(r in name or r in haystack for r in RANK_WORDS):
        return "faction_member"
    if re.search(r"使者|执事|长老|阁主|宗主|掌门|护法|司命|司库|宗门|账处", haystack):
        return "faction_member"
    return "person"


def _is_generic(name: str) -> bool:
    """泛称检测：未具名个人/群体（境界/职业+泛称，如「青衫丹修」「历练队修士」「凡人商贩」）。

    与 AGGREGATE 的区别：泛称有 voice 约束（写作时需要），归 temporary+scene（保留约束不注册）；
    AGGREGATE 是纯群像占位（坊市众人），归 aggregate（不进角色体系）。
    """
    return any(s in name for s in GENERIC_SUFFIX)


def _is_recurring(name: str, role: str, func: str, own_text: str) -> bool:
    """recurring 判定（本章证据）：至少满足一个信号（新角色标记/主角团队/个人目标/持续功能）。

    收紧（外脑裁决）：仅 function 存在不算 recurring（黑风谷守门人反例）；
    证据只取该角色自己的文本（voice/behavior 约束 + role + func），不取细纲全文——
    避免全文「后续/长期/未来」等字样误伤同章所有角色。
    """
    hay = f"{name} {role} {func} {own_text}"
    if any(h in hay for h in _RECURRING_HINTS):
        return True
    return False


def _cross_chapter_refs(book: Path, chapter_id: int, name: str) -> tuple[int, bool]:
    """跨章证据（确定性）：历史出现次数（近 3 章细纲）+ 是否未来引用（下一章细纲）。

    外脑裁决：分类规则需增加「未来引用」检测——仅本章证据不足（灰袍人反例：
    ch39/40/43 出现 + ch44 关联，但 ch43 本章 voice 锚点无 recurring 信号）。
    """
    hist, future = 0, False
    for delta in (-3, -2, -1, 1):
        ch = chapter_id + delta
        if ch < 1:
            continue
        for nm in (f"细纲_第{ch}章.md", f"细纲_第{ch:03d}章.md"):
            p = book / OUTLINE_DIR / nm
            if p.exists():
                if name in p.read_text(encoding="utf-8"):
                    if delta == 1:
                        future = True
                    else:
                        hist += 1
                break
    return hist, future


def resolve_role_lifecycle(chars: list[dict], funcs: list[dict], bible_names: set[str],
                           chapter_id: int, book: Path) -> tuple[list[dict], list[dict]]:
    """R1：为 character_contract 附 role lifecycle metadata。

    输入：_parse_characters 输出（含 voice/behavior 约束）+ 角色功能 + bible.characters 名字集合 + 章节号 + 书根。
    输出：(chars 每项附 entity_type/lifecycle/card_required, role_lifecycle_report)
    只读 bible/细纲、只输出 metadata；不建卡、不注册 tracking、不改正文。
    """
    report: list[dict] = []
    for c in chars:
        name = c["name"]
        role = c.get("role", "")
        func = c.get("function", "")
        own_text = " ".join(c.get("voice_constraints", []) + c.get("behavior_constraints", []))
        bible_match = name in bible_names
        entity_type = _entity_type_for(name, role, func, own_text)
        hist, future = _cross_chapter_refs(book, chapter_id, name)

        if bible_match:
            classification = "existing"
        elif entity_type == "aggregate":
            classification = "aggregate"
        elif _is_generic(name):
            # 泛称：有 voice 约束但非长期角色——不因跨章/未来引用/职称升级（黑风谷守门人反例）
            classification = "temporary"
        elif entity_type == "faction_member":
            # 组织角色（某宗执事/使者/长老）：固定 temporary + partial 卡，跨章不升级为 recurring
            # （组织身份/行为规则/立场/语言风格即够；升级完整卡走 R2 人工确认）
            classification = "temporary"
        else:
            # 个人角色：recurring = 本章证据（新角色/团队/目标） OR 未来引用 OR 历史多章出现
            recurring = (
                _is_recurring(name, role, func, own_text)
                or future
                or hist >= 1
            )
            classification = "recurring" if recurring else "temporary"

        # card_required：classification 基准 + entity_type 修正（faction_member → partial）
        card_map = {"existing": False, "recurring": "full", "temporary": "scene", "aggregate": "none"}
        card = card_map[classification]
        if entity_type == "faction_member" and classification == "temporary":
            card = "partial"

        c["entity_type"] = entity_type
        c["lifecycle"] = {
            "status": "existing" if bible_match else "unregistered",
            "classification": classification,
            "first_appear": chapter_id,
            "bible_match": bible_match,
        }
        c["card_required"] = card

        # new_entities 报告（recurring / temporary 均列入；existing/aggregate 不列入）
        if classification in ("recurring", "temporary"):
            reasons = []
            if role == "新角色":
                reasons.append("细纲标注新角色")
            if func:
                reasons.append("有角色功能")
            if future:
                reasons.append("下一章引用")
            if hist >= 1:
                reasons.append(f"近3章历史出现×{hist}")
            if classification == "recurring":
                if any(h in f"{role} {func} {own_text}" for h in ("主角团队", "徒弟", "学徒", "伙伴")):
                    reasons.append("主角团队/师徒关系")
                if any(h in f"{role} {func} {own_text}" for h in ("战斗", "冲突", "信息传递")):
                    reasons.append("剧情功能")
            report.append({
                "name": name,
                "entity_type": entity_type,
                "classification": classification,
                "card_required": card,
                "bible_match": bible_match,
                "first_appear": chapter_id,
                "reasons": reasons or [classification],
            })
    return chars, report


def _parse_emotion_curve(text: str) -> dict:
    """解析 情绪曲线（章级四格：开始/中段/高潮/结束 或 opening/middle/climax/ending）。

    轻量四格，不引入评分/数值（禁止情绪强度 8/10 之类）。
    """
    seg = _sec_loose(text, r"情绪曲线|情绪弧线|Emotion Curve")
    if not seg:
        # 2026-10-25：细纲情绪弧线常写在 FAIL 补充块（无 # 前缀的裸标题，如
        # 「FAIL 7. Emotion Curve（目标情绪与情绪弧线）补充：」），_sec_loose 只认
        # #{2,4} 标题 → 实测仙门书 ch1–ch3 全部解析为空。FAIL 块兜底（同章首钩子范式）。
        for _t, _b in _fail_blocks(text):
            if re.search(r"情绪|Emotion", _t) and _b:
                seg = _b
                break
    if not seg:
        seg = _sec_loose(text, r"目标情绪")
    out = {"opening": "", "middle": "", "climax": "", "ending": ""}
    labels = {"opening": ["开始", "开篇", "opening"], "middle": ["中段", "middle"],
              "climax": ["高潮", "爆发", "climax"], "ending": ["结束", "章末", "结尾", "ending"]}
    for ln in seg.splitlines():
        ln = ln.strip().lstrip("-* ").strip()
        for key, labs in labels.items():
            m = re.match(rf"^(?:\*\*)?(?:{'|'.join(labs)})(?:[^：:]*)?[：:]\s*(.+)$", ln)
            if m and not out[key]:
                # 2026-10-25：[:120]→[:400]。实测细纲情绪弧线（W形逐段标注）被截到 120 字后
                # writer 只能看到峰值标签、看不到过渡拍，情绪过渡失真（濒死后秒开笑）的上游之一。
                out[key] = m.group(1).strip()[:400]
                break
    if not any(out.values()):
        # 2026-10-25 兜底：细纲无四格标签时（FAIL 块散写 or 「## 目标情绪」标题 + 整段
        # 散文式弧线），取「目标情绪」行/首行放 middle，writer 至少能看到本章情绪走向。
        _cand = seg or _sec_loose(text, r"目标情绪")
        if not _cand:
            for _t, _b in _fail_blocks(text):
                if re.search(r"目标情绪|情绪", _t) and _b:
                    _cand = _b
                    break
        if _cand:
            _m2 = re.search(r"^目标情绪[：:]\s*(.+)$", _cand, re.M)
            if _m2:
                out["middle"] = _m2.group(1).strip()[:400]
            else:
                for _ln in _cand.splitlines():
                    _ln = _ln.strip().lstrip("-* ").strip()
                    if _ln and not re.match(r"^#{2,4}", _ln) and "补充" not in _ln[:4]:
                        out["middle"] = _ln[:400]
                        break
    return out


def _parse_info_boundary(text: str) -> tuple[list[str], list[str]]:
    """解析 信息边界（允许释放/禁止释放）→ (must_release, must_not_invent)。

    CCv2 新增：Writer 信息权限控制（不是 canon 约束语义，是「本章能/不能释放什么信息」）。
    """
    seg = _sec_loose(text, r"信息边界|Information Boundary")
    release: list[str] = []
    invent: list[str] = []
    for ln in seg.splitlines():
        ln = ln.strip().lstrip("-* ").strip()
        m = re.match(r"^(?:\*\*)?(允许释放|禁止释放|must_release|must_not_invent)(?:\*\*)?\s*[：:]\s*(.+)$", ln)
        if not m:
            continue
        items = [p.strip() for p in re.split(r"[；;]", m.group(2)) if p.strip()]
        (release if m.group(1) in ("允许释放", "must_release") else invent).extend(items[:12])
    return release, invent


def _parse_anchor(text: str) -> list[str]:
    return [m.group(1).strip()[:200] for m in re.finditer(r"^复沓锚句[：:]\s*(.+)$", text, re.M)]


# —— 写前上下文容量（2026-10-11 修复：原 300 字 ≈ 全章 7.5%，是跨章断层/数值硬伤的共同上游）——
# 扩展接入：只改默认容量 + 新增结构化状态卡，不改变 preceding 的既有语义与消费者。
# 可用 env NWW_PRECEDING_CHARS 覆盖（回滚只需设回 300，不动代码）。
_PRECEDING_DEFAULT_CHARS = int(os.environ.get("NWW_PRECEDING_CHARS", "1500") or 1500)
_PRECEDING_STATE_CAP = int(os.environ.get("NWW_PRECEDING_STATE_CAP", "6") or 6)
_PRECEDING_THREAD_CAP = int(os.environ.get("NWW_PRECEDING_THREAD_CAP", "8") or 8)


def _find_prev_chapter_file(body: Path, chapter_id: int) -> Path | None:
    """正文目录递归找上一章文件（第NNN章* / 第NN章* / chNNNN* / 第N章*）。

    ★ 2026-10-12 修复①：补齐「两位零填充」形态（第01章）。此前只有 3 位/1 位两种模式，
    实测《钦天监里的统计师》正文前 9 章命名为 `第01章…第09章`（两位），
    导致 ch2–ch10 共 9 章的 `_preceding_v2` 全部返回「【前文衔接缺失】」——
    即 10-11 把容量从 300 提到 1500 的修复，在这 9 章里一点都没生效。

    ★ 2026-10-12 修复②（外脑裁决）：上述补法仍有两个未解决的问题——
      (a) `re.search(pat, p.name)` **非锚定**，且四个 pattern 逐个试、先命中者入选；
      (b) `rglob` 无路径过滤，`_review_snapshots/` 下的同名快照会进入候选，
          最终靠 `sorted()[-1]` 的字典序碰运气选中正式稿 —— 这不是正确性保证。
    现统一走 `nww_prose_paths.resolve_chapter_file`：先排除非正文，再**锚定**解析章号，
    多候选按 mtime 取最新。
    """
    try:
        from nww_prose_paths import resolve_chapter_file
        return resolve_chapter_file(body, chapter_id - 1)
    except Exception:  # noqa: BLE001
        pass
    patterns = [
        rf"第{chapter_id - 1:03d}章",
        rf"第{chapter_id - 1:02d}章",
        rf"ch{chapter_id - 1:04d}",
        rf"第{chapter_id - 1}章",
    ]
    candidates = []
    for p in body.rglob("*.md"):
        if "_review" in str(p) or ".bak" in p.name:
            continue
        for pat in patterns:
            if re.search(pat, p.name):
                candidates.append(p)
                break
    return sorted(candidates)[-1] if candidates else None


def _preceding_state_card(book: Path, chapter_id: int) -> str:
    """上章末结构化状态卡（2026-10-11 新增）：把「上一章结束时世界是什么样」确定性喂给 writer。

    来源：追踪/delta_第NNN章.json 的 character_snapshots（location/state/open_threads）+ global_change。
    零 LLM、只读。delta 缺失 / 结构不符 / 解析失败 → 返回空串，**绝不破坏既有路径**。

    作用：补 preceding 纯正文片段的结构性缺口——正文末尾只交代语气与情势，
    不交代「人在哪、状态如何、哪些线索未结」，而这正是跨章数值/因果硬伤的源头。
    """
    if chapter_id <= 1:
        return ""
    tdir = book / TRACKING_DIR
    if not tdir.is_dir():
        return ""
    try:
        cands = sorted(tdir.glob(f"delta_第{chapter_id - 1:03d}章.json")) or sorted(
            tdir.glob(f"delta_第{chapter_id - 1}章.json")
        )
        if not cands:
            return ""
        d = json.loads(cands[-1].read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(d, dict):
        return ""

    lines: list[str] = []
    snaps = d.get("character_snapshots") or {}
    if isinstance(snaps, dict):
        for name, row in list(snaps.items())[:_PRECEDING_STATE_CAP]:
            if not isinstance(row, dict):
                continue
            loc = str(row.get("location") or "").strip()
            st = str(row.get("state") or "").strip()
            if not (loc or st):
                continue
            seg = f"- {name}"
            if loc:
                seg += f"｜位置：{loc}"
            if st:
                seg += f"｜状态：{st}"
            lines.append(seg)

    threads: list[str] = []
    if isinstance(snaps, dict):
        for _n, row in snaps.items():
            if not isinstance(row, dict):
                continue
            for t in (row.get("open_threads") or [])[:3]:
                if isinstance(t, str) and t.strip() and t.strip() not in threads:
                    threads.append(t.strip())
    if threads:
        lines.append("未结线索：" + " / ".join(threads[:_PRECEDING_THREAD_CAP]))

    gc = d.get("global_change")
    if isinstance(gc, dict) and gc:
        bits = [f"{k}={v}" for k, v in gc.items() if v not in (None, "", [], {})]
        if bits:
            lines.append("全局变化：" + "；".join(str(b) for b in bits[:4]))

    if not lines:
        return ""
    return ("【上一章结束时状态卡】（来自追踪，确定性事实；与正文冲突时以正文为准，"
            "不得据此新增未认证设定）\n" + "\n".join(lines))


# ============================================================================
# §W1 世界观/宏观素材按需召回（2026-10-12 新增，外脑 ChatGPT 裁决 P0-1）
#
# 根因：packet 构建链路只读 `设定/角色`（ROLE_CARD_DIR），`设定/世界观/*.md`
#       从未进入 writer 写前上下文 —— 实测钦天监 9644 字节的宏观素材（10 层空间版图
#       + 5 阶段 40 年时间线 + 十大政治势力）在写第 27 章时 writer 完全看不到。
#
# 设计原则（与 _preceding_state_card 同一范式）：
#   - 零 LLM、只读、确定性；任一步失败 → 返回 ("", [])，绝不破坏既有路径
#   - 只增顶层键 world_context，不动既有字段/既有解析/既有执行路径
#   - 按需召回（细纲原文 + 本章 subjects 命中标题关键词才召回），不做全量注入
#   - env NWW_WORLD_CONTEXT_CHARS=0 可整体关闭
# ============================================================================

WORLD_SUBDIRS = ("设定/世界观", "设定/势力")

# ★ 2026-10-12 修复：WORLD_SUBDIRS 只硬编码两个目录，实测漏读真实存在的设定资产——
#   钦天监 `设定/文风.md`（账房式冷幽默风格契约，MEMORY §5 明确定版）从未进 packet；
#   诡异时代 `设定/力量体系/`、`设定/职业库/` 同样从未进 packet。
#   这是「物件的交代/规则的铺垫不厚」的一类根因：作者写了素材，系统没读。
#   修法：**动态发现** 设定/ 下的全部子目录与顶层 .md，而不是猜目录名。
#   排除 设定/角色（已由 §7-1 的 character_resources / character_forbids 承载，避免重复）。
#   只增不改：WORLD_SUBDIRS 常量保留（既有消费者不变），新增 _world_dirs() 供新路径使用。
_WORLD_EXCLUDE_DIRS = ("角色",)
_WORLD_EXCLUDE_FILES = ("文风.md",)  # 不排除！文风正是被漏掉的那个（保留此行说明其特殊性）
_WORLD_EXCLUDE_FILES = ()


def _world_dirs(book: Path) -> list[str]:
    """动态发现 设定/ 下所有素材目录（含顶层 .md 视为独立素材）。

    返回相对 book 的路径列表；目录 → "设定/xxx"，顶层 md → "设定/xxx.md"。
    失败/不存在 → 回退到 WORLD_SUBDIRS（与修复前行为一致）。
    """
    root = book / "设定"
    if not root.is_dir():
        return list(WORLD_SUBDIRS)
    found: list[str] = []
    try:
        for p in sorted(root.iterdir()):
            if p.name.startswith(".") or p.name.startswith("_"):
                continue
            if p.is_dir():
                if p.name in _WORLD_EXCLUDE_DIRS:
                    continue
                if not any(p.glob("*.md")):
                    continue
                found.append(f"设定/{p.name}")
            elif p.is_file() and p.suffix == ".md":
                if p.name in _WORLD_EXCLUDE_FILES:
                    continue
                found.append(f"设定/{p.name}")
    except Exception:
        return list(WORLD_SUBDIRS)
    # 保证原有两项优先且一定在（既有行为不丢）
    for legacy in WORLD_SUBDIRS:
        if legacy not in found and (book / legacy).is_dir():
            found.append(legacy)
    return found or list(WORLD_SUBDIRS)

_WORLD_DEFAULT_CHARS = int(os.getenv("NWW_WORLD_CONTEXT_CHARS", "1200") or 1200)
_WORLD_SEG_CAP = int(os.getenv("NWW_WORLD_CONTEXT_SEGS", "8") or 8)
_WORLD_SEG_CHARS = int(os.getenv("NWW_WORLD_CONTEXT_SEG_CHARS", "260") or 260)
# 宏观骨架（表格行采样）：默认 700 字；设 0 可只保留强命中召回
_WORLD_SKELETON_CHARS = int(os.getenv("NWW_WORLD_SKELETON_CHARS", "700") or 700)
_WORLD_SKELETON_GROUP_ROWS = int(os.getenv("NWW_WORLD_SKELETON_ROWS", "3") or 3)
_WORLD_SKELETON_ROW_CHARS = int(os.getenv("NWW_WORLD_SKELETON_ROWCHARS", "58") or 58)

# 通用标题（不承载具体可召回实体，命中它们没有信息量）
_WORLD_GENERIC_HEAD = re.compile(
    r"^(一|二|三|四|五|六|七|八|九|十|概述|总览|前言|说明|附录|背景设定|世界观扩展|扩展设定)$"
)


# ★★ 风格锚（style_anchor）—— 2026-10-12 实测确诊的第二个「写了没人读」资产。
# `设定/文风.md` 文件开头自己写着：「『基调与冷幽默』节为写作 prompt 的风格锚
# （2026-09-10 拍板）」。但全仓 grep 证明：**没有任何代码读取它**。
# 钦天监 3498B 的风格契约（账房式冷幽默四种配方 + 三条硬边界）从未进入 writer 输入
# → 这直接对应「文风不稳 / 冷幽默时有时无 / 该压抑的场口却在抖机灵」。
# 修法：风格契约**恒常注入**（不是按需召回——风格每章都要守），读「基调与冷幽默」节；
# 「句式与排版节奏」节由 nww-long-write 的排版锚消费，不重复注入。
_STYLE_FILE = "设定/文风.md"
_STYLE_SECTION = "基调与冷幽默"
# 叙事人称权威来源（2026-11-04 用户裁决）：bible.pov 是全书第几人称的唯一事实源。
# 缺失时默认第三人称（确定性兜底，不让 writer 自己猜）。文风.md 的「叙事视角」从 bible.pov 继承，
# 不再由建书 LLM 自由发挥；写章链从这里读，与文风、正文保持一致。
_DEFAULT_POV = "第三人称"


def _narrative_pov_card(book: Path) -> dict:
    """读 bible.json 的 pov 字段 → 叙事人称卡。缺失/空 → 默认第三人称。

    返回 {"text", "source"}；source ∈ {"bible", "default"}。零 LLM、只读、永不抛。
    """
    try:
        bible = load_json(book / "bible.json")
    except Exception:
        bible = {}
    pov = ""
    if isinstance(bible, dict):
        v = bible.get("pov")
        if isinstance(v, str) and v.strip():
            pov = v.strip()
    if pov:
        return {"text": pov, "source": "bible"}
    return {"text": _DEFAULT_POV, "source": "default"}
# ★ 2026-10-12 元审查自查抓到的自身缺陷（勿再传播"已修好"的旧说法）：
#   首版把节名**硬编码成钦天监那一本的节名** `基调与冷幽默`。
#   实测 5 本：钦天监命中（1020 字），**全民棋局有 设定/文风.md 却命中为空**
#   （它的节名是 `## 1. 语言风格`）→ 即"修了一个样本、其余全落空"，
#   属于我自己反复批评过的「按一本书的样本写死规则」。
#   修法：三级回落，且把命中方式写进产物，便于事后审计而不是"空了也不知道为什么空"。
#     Tier1 精确节名；Tier2 标题含风格类关键词；Tier3 全文无 ## 小节则取整篇。
#   三级都不中 → 返回 {}（宁可空，不猜）。
_STYLE_SECTION_KEYS = ("基调", "调性", "语气", "口吻", "语言风格", "文风", "风格", "腔调", "笔法", "叙事", "行文")
_STYLE_MAX_CHARS = int(os.getenv("NWW_STYLE_ANCHOR_CHARS", "1800") or 1800)


def _style_sections(text: str) -> list[tuple[str, str]]:
    """把 markdown 文本按 `## ` 切成 [(节名, 正文)]。无 `## ` 时返回 []。"""
    out: list[tuple[str, str]] = []
    cur: str | None = None
    buf: list[str] = []
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("## "):
            if cur is not None:
                out.append((cur, "\n".join(buf).strip()))
            cur = s[3:].strip()
            buf = []
            continue
        if cur is not None:
            buf.append(ln.rstrip())
    if cur is not None:
        out.append((cur, "\n".join(buf).strip()))
    return out


def _style_anchor_card(book: Path) -> dict:
    """读 设定/文风.md 的风格锚节。零 LLM、只读、失败即空。

    返回 {"text","source","section","match"}；match ∈ {"exact","keyword","whole","none"}，
    `none` 时不返回（保持 {}）。
    """
    f = book / _STYLE_FILE
    if not f.is_file():
        return {}
    try:
        text = f.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return {}
    secs = _style_sections(text)
    if not secs:
        # 没有 ## 小节 → 整篇即风格契约（去掉一级标题行）
        body = [ln for ln in text.splitlines() if not ln.strip().startswith("# ")]
        txt = "\n".join(body).strip()
        sec_name, match = "", "whole"
    else:
        sec_name, txt, match = "", "", ""
        for name, body in secs:
            if name == _STYLE_SECTION:
                sec_name, txt, match = name, body, "exact"
                break
        if not txt:
            for kw in _STYLE_SECTION_KEYS:
                for name, body in secs:
                    if kw in name and body:
                        sec_name, txt, match = name, body, f"keyword:{kw}"
                        break
                if txt:
                    break
    if not txt or not txt.strip():
        return {}
    if len(txt) > _STYLE_MAX_CHARS:
        txt = txt[:_STYLE_MAX_CHARS].rstrip() + "…"
    return {"text": txt, "source": _STYLE_FILE, "section": sec_name, "match": match}


# —— §W6 创作层正向镜头卡（2026-11-06 黄金三章新案）——
# 背景：writer 输入长期只有「不许写什么」（must_keep/数字锚/规则锚），
# 细纲里可拍的感官与微动作无人读取 → 正文被压成合规清单腔。
# 本组函数确定性解析细纲 `## 镜头模具` 与 `设定/题材正文提示卡.md`，
# 零 LLM、失败即空并留 *_status（禁止静默空转，沿用 Q10 口径）。
_SENSORY_SECTION = "镜头模具"
_SENSORY_KEYS = ("五感锚点表", "生理微动作表", "烟火破绽", "人话台词样本", "本章一个物理母题")
_SENSORY_MAX_CHARS = int(os.getenv("NWW_SENSORY_CARD_CHARS", "1600") or 1600)
_GENRE_STYLE_FILE = "设定/题材正文提示卡.md"
_GENRE_STYLE_SECTIONS = ("正文落点", "场景颗粒", "核心爽点")
_GENRE_STYLE_MAX_CHARS = int(os.getenv("NWW_GENRE_STYLE_CHARS", "1400") or 1400)


def _bracket_sections(text: str, head: str) -> dict[str, str]:
    """解析 `- 【键】内容` 形式的小节；内容延续到下一个 【】 或空行后的新 `-`。"""
    out: dict[str, list[str]] = {}
    in_head = False
    cur: str | None = None
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("#"):
            in_head = head in s
            cur = None
            continue
        if not in_head:
            continue
        m = re.match(r"^[-*]\s*【([^】]+)】\s*(.*)$", s)
        if m:
            cur = m.group(1).strip()
            out[cur] = [m.group(2).strip()] if m.group(2).strip() else []
            continue
        if cur is None:
            continue
        if not s:
            cur = None
            continue
        if s.startswith("-") or s.startswith("|") or s.startswith(">"):
            cur = None
            continue
        out[cur].append(s)
    return {k: "\n".join(v).strip() for k, v in out.items() if v}


def _sensory_card(text: str) -> dict:
    """从细纲 `## 镜头模具` 提取正向镜头清单；无任何槽位时返回 {}。"""
    secs = _bracket_sections(text, _SENSORY_SECTION)
    card: dict[str, str] = {}
    for key in _SENSORY_KEYS:
        hit = next((v for k, v in secs.items() if key in k), "")
        if hit:
            card[key] = hit[:_SENSORY_MAX_CHARS]
    if not card:
        return {}
    card["scene_anchor_rule"] = (
        "锚点是触发式调用，不是静态清单：写「牛油凝块黏在拇指上、她拿指腹蹭桶沿」，"
        "不写「凡食分子浓度累积」。五感随人物的动作、受阻与反应出现，禁止开场连堆三个感官；"
        "微动作须是无意义的下意识动作，不承载情绪定性；烟火破绽至少一处。"
    )
    return card


def _genre_style_card(book: Path) -> dict:
    """读 设定/题材正文提示卡.md 的正文落点/场景颗粒/核心爽点三节。"""
    f = book / _GENRE_STYLE_FILE
    if not f.is_file():
        return {}
    try:
        raw = f.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return {}
    sections = dict(_style_sections(raw))
    card: dict[str, str] = {}
    for key in _GENRE_STYLE_SECTIONS:
        hit = next((v for k, v in sections.items() if key in k), "")
        if hit:
            card[key] = hit[:_GENRE_STYLE_MAX_CHARS]
    if not card:
        return {}
    card["source"] = _GENRE_STYLE_FILE
    return card


def _style_anchor_enabled() -> bool:
    """风格锚注入总开关。**默认关闭（0）**，显式置 1 才启用。

    依据（2026-10-12 正式 A/B，新书 ch2，n=30/组，生产 max_tokens=8192）：
      - 只开 style_anchor：ΔT3 = +0.49，95% CI [-0.20, +1.19] **含 0** → 未达标；
      - G3（全开）− G1（只开 macro）= −0.47，95% CI [-1.27, +0.32] 含 0；
        且在「未被 CAP 截断的完整稿」子集里缩到 **−0.11** → 该差值主要是截断伪影，
        无证据表明 style_anchor 在 macro 之上有增量收益。
    外脑 R1-c 裁决：「默认关闭 style_anchor，采用资产校验后的显式启用机制。」
    → 结论：默认不注入；只有在**明确要验证/启用**时才 `NWW_STYLE_ANCHOR=1`。
    """
    return os.getenv("NWW_STYLE_ANCHOR", "0") == "1"


def _book_style_anchor_opt_in(book: Path) -> bool:
    """按书显式开启：bible.json 里 `policy.style_anchor: "on"` 才注入。

    全局 A/B「默认关闭」不推翻；只有已拍板文风契约的书（如用泡面征服修仙界）
    自建书层 opt-in。资产缺失时由 _style_anchor_status 显式报 missing_file。
    """
    try:
        data = json.loads((book / "bible.json").read_text(encoding="utf-8", errors="ignore"))
        return str((data.get("policy") or {}).get("style_anchor", "off")).lower() == "on"
    except Exception:
        return False


def _style_anchor_status(book: Path, card: dict | None = None) -> dict:
    """风格锚状态（外脑 Q10：禁止静默伪装为成功注入）。

    status: ok / missing_file / no_match / unknown
    —— `style_anchor` 为空时，必须能区分「根本没有文件」和「有文件但没命中风格节」。
    """
    f = book / _STYLE_FILE
    if not f.is_file():
        return {"status": "missing_file", "file": _STYLE_FILE,
                "anchored": False,
                "reason": f"本书无 {_STYLE_FILE}，风格锚恒空，风格契约须由人拍板"}
    if card:
        return {"status": "ok", "file": _STYLE_FILE, "anchored": True,
                "match": card.get("match"), "section": card.get("section"),
                "chars": len(card.get("text", ""))}
    return {"status": "no_match", "file": _STYLE_FILE, "anchored": False,
            "reason": "文件存在但未命中任何风格节（见 _STYLE_SECTION_KEYS），风格锚为空"}


# —— §W5c 作者改稿口味（2026-10-24 P2）——
# 来源：web/backend/app/style_feedback.py（P1）从「编辑器手改 diff」提炼出的规律库。
# 与 style_anchor 的边界：style_anchor 是**人拍板的定版文风契约**，本卡片是
# **机器从作者改稿中学到的经验偏好**——两者独立开关、独立审计，互不写入。
_FEEDBACK_FILE = "设定/笔记反馈/rules.json"
_FEEDBACK_MAX_CHARS = 1200
_FEEDBACK_MAX_RULES = 15


def _style_feedback_enabled() -> bool:
    """改稿口味注入总开关。**默认关闭（0）**，A/B 通过前不进入生产提示词。"""
    return os.getenv("NWW_STYLE_FEEDBACK", "0") == "1"


def _style_feedback_card(book: Path) -> dict:
    """读 设定/笔记反馈/rules.json。零 LLM、只读、失败即空。

    只注入规则正文与溯源计数，**不注入 evidence**（对照句一旦进提示词，
    模型容易把它当素材搬进正文）。
    """
    f = book / _FEEDBACK_FILE
    if not f.is_file():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}
    rules = [r for r in (data.get("rules") or [])
             if isinstance(r, dict) and str(r.get("rule") or "").strip()]
    if not rules:
        return {}
    lines = []
    for i, r in enumerate(rules[:_FEEDBACK_MAX_RULES], 1):
        ch = str(r.get("last_chapter") or "").strip()
        cnt = int(r.get("count") or 1)
        meta = f"（已吸收自{ch}改稿，共 {cnt} 次）" if ch else f"（共 {cnt} 次）"
        lines.append(f"{i}. {str(r['rule']).strip()}{meta}")
    text = "\n".join(lines)
    if len(text) > _FEEDBACK_MAX_CHARS:
        text = text[:_FEEDBACK_MAX_CHARS].rstrip() + "…"
    return {"text": text, "rule_count": min(len(rules), _FEEDBACK_MAX_RULES),
            "source": _FEEDBACK_FILE, "updated_at": str(data.get("updated_at") or "")}


def _style_feedback_status(book: Path, card: dict | None = None) -> dict:
    """口味库状态：ok / missing_file / empty / disabled（禁止静默伪装为成功注入）。"""
    if not _style_feedback_enabled():
        return {"status": "disabled", "injected": False,
                "reason": "NWW_STYLE_FEEDBACK=0，改稿口味注入已关闭（待 A/B 验收）"}
    f = book / _FEEDBACK_FILE
    if not f.is_file():
        return {"status": "missing_file", "file": _FEEDBACK_FILE, "injected": False,
                "reason": "本书尚无改稿口味库（在编辑器手改正文并保存后由后台提炼生成）"}
    if not card:
        return {"status": "empty", "file": _FEEDBACK_FILE, "injected": False,
                "reason": "口味库存在但暂无有效条目"}
    return {"status": "ok", "file": _FEEDBACK_FILE, "injected": True,
            "rule_count": card.get("rule_count"), "chars": len(card.get("text", ""))}


def _world_setting_files(book: Path) -> list[tuple[str, list]]:
    """按 _world_dirs() 展开成 [(相对目录/文件名, [md 文件])]。

    目录 → 其下所有 *.md；顶层 .md → 单元素列表。统一上游循环形态，
    使 设定/ 下的**任意**素材目录都能被召回（不再依赖硬编码目录名）。
    """
    out: list[tuple[str, list]] = []
    for rel in _world_dirs(book):
        p = book / rel
        try:
            if p.is_dir():
                files = sorted(p.glob("*.md"))
            elif p.is_file():
                files = [p]
            else:
                continue
        except Exception:
            continue
        if files:
            out.append((rel, files))
    return out


def _world_segments(book: Path) -> list[dict]:
    """设定/世界观/*.md（+ 设定/势力/，存在时）按 markdown 标题切成片段。

    返回 [{"source": 相对路径, "heading": 标题, "body": 正文}]。零 LLM、只读。
    """
    out: list[dict] = []
    for d, files in _world_setting_files(book):
        for f in files:
            try:
                txt = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            try:
                rel = str(f.relative_to(book))
            except Exception:
                rel = f.name
            # 按 markdown 标题切分；保留标题作为可召回实体名
            parts = re.split(r"^(#{1,6})[ \t]*(.+)$", txt, flags=re.M)
            # parts = [前置文本, '#', '标题', 正文, '##', '标题', 正文, ...]
            i = 1
            cur_head, cur_body = "", parts[0] if parts else ""
            while i < len(parts):
                level, head = parts[i], parts[i + 1]
                body = parts[i + 2] if i + 2 < len(parts) else ""
                if cur_head:
                    out.append({"source": rel, "heading": cur_head.strip(), "body": cur_body})
                cur_head, cur_body = head.strip(), body
                i += 3
            if cur_head:
                out.append({"source": rel, "heading": cur_head.strip(), "body": cur_body})
    return out


def _world_heading_keys(heading: str) -> list[str]:
    """从标题抽出可匹配实体名（>=2 字，去层级序号/括号/标点）。"""
    h = re.sub(r"[（(].*?[）)]", " ", heading)
    h = re.sub(r"[0-9]+[.、:：]|^\s*[一二三四五六七八九十]+\s*[、.]\s*", " ", h)
    keys = []
    for seg in re.split(r"[／/、,，。;；|｜\s]+", h):
        s = seg.strip()
        if len(s) >= 2 and not _WORLD_GENERIC_HEAD.match(s):
            keys.append(s)
    if not keys:
        return keys
    # —— 新增：实体名再切分（长标题拆成可被细纲命中的实体词）——
    # ★ 2026-10-30 修复（建书产物召回缺口，回灌本 runtime 实际副本）：
    #   原实现只剥「括号内容」与「层级序号」，不过分隔符——于是
    #   `「泡面·红尘满足值」经营规则` 抽出的 key 是整串，永远不可能作为子串命中细纲正文。
    #   把真分隔符纳入切分，让长标题能抽出真正的实体名（泡面 / 红尘满足值 / 青牛坊市）。
    extra: list[str] = []
    for s in list(keys):
        for seg in re.split(
            r"[：:·・、,，。;；|｜/／\s「」『』《》〈〉()（）【】\[\]“”\"'‘’!！?？]+",
            s,
        ):
            t = seg.strip()
            if len(t) >= 2 and not _WORLD_GENERIC_HEAD.match(t) and t not in keys:
                extra.append(t)
    return keys + extra


def _world_body_entity_keys(body: str) -> list[str]:
    """从片段正文抽实体名（粗体标注优先），用于标题命中不了时的兜底。

    ★ 2026-10-30 新增（建书产物召回缺口，回灌本 runtime 实际副本）：
      bootstrap 产出的世界观是结构化章节（标题如「4.3 对立势力（压力来源）」），
      真正的实体名（辟谷宗 / 黑蛇帮 / 清冷剑尊）只出现在正文中，标题路径永远召不回。
      正文里实体名通常被 `**粗体**` 标注（生成约定），故据此抽取。
      零 LLM、只读；无粗体实体 → 返回 []（不猜、不泛化）。
    """
    if not body:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in re.findall(r"\*\*([^*\n]{2,40})\*\*", body):
        for seg in re.split(r"[/／、,，。;；|｜\s（(）)]+", raw):
            s = seg.strip().strip("《》「」【】\"'“”‘’")
            if len(s) >= 2 and s not in seen:
                seen.add(s)
                out.append(s)
    return out


def _current_volume_no(book: Path, chapter_id: int) -> int | None:
    """当前章节所属卷号（读 大纲/卷纲_第N卷.md 的「章节范围」）。零 LLM、只读；失败 → None。"""
    od = book / "大纲"
    if not od.is_dir():
        return None
    for f in sorted(od.glob("卷纲_第*卷.md")):
        m = re.search(r"卷纲_第(\d+)卷", f.name)
        if not m:
            continue
        try:
            t = f.read_text(encoding="utf-8", errors="ignore")
            # 兼容「- **章节范围**：第 1–100 章」这类加粗包裹与全/半角分隔符
            r = re.search(
                r"章节范围[^0-9]{0,16}第?[ \t]*(\d+)[ \t]*[–\-~～—至][ \t]*(\d+)", t)
            if r:
                a, b = int(r.group(1)), int(r.group(2))
                if a <= chapter_id <= b:
                    return int(m.group(1))
        except Exception:
            continue
    return None


def _row_in_volume(row: str, vol: int | None) -> bool:
    """表格行是否落在当前卷。兼容「第1卷」「第1-6卷」「第1–2卷」等范围写法。"""
    if vol is None:
        return False
    for m in re.finditer(r"第[ \t]*(\d+)[ \t]*(?:[–\-~～—至][ \t]*(\d+))?[ \t]*卷", row):
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        if a <= vol <= b:
            return True
    return False


def _world_skeleton(book: Path, chapter_id: int, budget: int) -> tuple[str, list[str]]:
    """宏观骨架：从 设定/世界观/*.md 抽取表格行（空间版图 / 时间线 / 势力清单等）。

    单章细纲不会提到「江南」「三皇子党」这类宏观实体，纯标题命中召回率极低；
    骨架层按「表格分组采样 + 当前卷优先」恒常提供世界规模，让 writer 知道世界有多大。

    零 LLM、只读；失败/无表格 → ("", [])。
    """
    if budget <= 0:
        return "", []
    vol = _current_volume_no(book, chapter_id)
    rows_by_file: list[tuple[str, list[list[str]]]] = []
    for d, files in _world_setting_files(book):
        for f in files:
            try:
                txt = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            try:
                rel = str(f.relative_to(book))
            except Exception:
                rel = f.name
            groups: list[list[str]] = []
            cur: list[str] = []
            for line in txt.split("\n"):
                s = line.strip()
                if s.startswith("|"):
                    if set(s) <= set("|-: "):
                        continue
                    cur.append(s)
                else:
                    if cur:
                        groups.append(cur)
                        cur = []
            if cur:
                groups.append(cur)
            if groups:
                rows_by_file.append((rel, groups))

    if not rows_by_file:
        return "", []

    def _compact(row: str) -> str:
        cells = [c.strip() for c in row.strip("|").split("|")]
        cells = [c for c in cells if c]
        line = "· " + " / ".join(cells)
        return line[: _WORLD_SKELETON_ROW_CHARS].rstrip()

    lines: list[str] = []
    refs: list[str] = []
    used = 0
    for rel, groups in rows_by_file:
        for g in groups:
            if len(g) < 2:
                continue
            head = _compact(g[0])
            body_rows = g[1:]
            vol_hit = [r for r in body_rows if _row_in_volume(r, vol)]
            pick = (vol_hit or body_rows)[: _WORLD_SKELETON_GROUP_ROWS]
            block = [head] + [_compact(r) for r in pick]
            block_text = "\n".join(block)
            if used + len(block_text) > budget:
                continue
            lines.extend(block)
            used += len(block_text)
            if rel not in refs:
                refs.append(rel)
        if used >= budget:
            break

    if not lines:
        return "", []
    return "\n".join(lines), refs


def _world_context_card(book: Path, query_text: str) -> tuple[str, list[str]]:
    """按需召回与本章相关的世界观/势力片段。

    query_text：细纲原文 + 本章 subjects（由调用方拼好）。
    命中判据：片段标题中的实体名（>=2 字）出现在 query_text 中。
    返回 (召回正文, source_refs)。预算内截断；无命中 / 失败 → ("", [])。
    """
    if _WORLD_DEFAULT_CHARS <= 0:
        return "", []
    if not query_text or not query_text.strip():
        return "", []
    try:
        segs = _world_segments(book)
    except Exception:
        return "", []
    if not segs:
        return "", []

    hits: list[tuple[int, dict, str]] = []
    for sg in segs:
        keys = _world_heading_keys(sg.get("heading", ""))
        matched = False
        for k in keys:
            if k and k in query_text:
                hits.append((len(k), sg, k))
                matched = True
                break
        if not matched:
            # —— 正文兜底（2026-10-30 新增）——
            # 标题抽不出实体名时（如「4.3 对立势力（压力来源）」这种结构化章节名，
            # 实体名只写在正文里），退而从正文的粗体标注抽实体名。
            for k in _world_body_entity_keys(sg.get("body", "")):
                if k and k in query_text:
                    hits.append((len(k), sg, k))
                    break
    if not hits:
        return "", []

    # 长实体名优先（更具体），同级按文件内顺序
    hits.sort(key=lambda x: -x[0])

    lines: list[str] = []
    refs: list[str] = []
    used = 0
    seen: set[tuple[str, str]] = set()
    for _ln, sg, key in hits:
        if len(lines) >= _WORLD_SEG_CAP:
            break
        ident = (sg["source"], sg["heading"])
        if ident in seen:
            continue
        seen.add(ident)
        body = re.sub(r"[ \t]+", " ", sg.get("body", "")).strip()
        body = re.sub(r"\n{2,}", "\n", body)
        if len(body) > _WORLD_SEG_CHARS:
            body = body[:_WORLD_SEG_CHARS].rstrip() + "…"
        if not body:
            continue
        seg_text = f"· {sg['heading']}：{body}"
        if used + len(seg_text) > _WORLD_DEFAULT_CHARS:
            remain = _WORLD_DEFAULT_CHARS - used
            if remain < 40:
                break
            seg_text = seg_text[:remain].rstrip() + "…"
        lines.append(seg_text)
        used += len(seg_text)
        r = f"{sg['source']}#{sg['heading']}"
        if r not in refs:
            refs.append(r)

    if not lines:
        return "", []
    head = ("【本章相关世界观/宏观事实】（来自设定文件，确定性只读；"
            "只可用于让本章行动受其约束，不得据此新增细纲未批准的事件）")
    return head + "\n" + "\n".join(lines), refs


# ============================================================================
# §W2 跨章状态卡（2026-10-12 新增，外脑 ChatGPT 裁决：闭环缺口 P0-1b）
#
# 根因（实测，非推测）：_preceding_state_card 的唯一数据源是 追踪/delta_第{N-1}章.json，
#   但 delta 在 commit 成功后即被消费，钦天监 29 章仅残留 5 个（全是未提交/失败的）。
#   实测 27 章里 preceding_state_card 只有 ch27 产出了 174 字，其余 26 章 **恒为空串**
#   —— 即 2026-10-11 加的「结构化状态卡」在生产中 96% 是空转。
#   而真正持久化的跨章资产（commit 后仍在）从未被 writer 读过：
#     追踪/逐章记录/第NNN章.md —— 钦天监 27/27 全部含「结果」「下一章承诺」「角色变化」
#     追踪/角色状态/*.md       —— 7 个角色的 位置/当前目标/身心状态/关键关系
#     追踪/伏笔.md             —— F001/F002/F003/F006 全部「已埋」、0 回收
#
# 设计原则（与 §W1 同一范式）：零 LLM、只读、确定性；只增顶层键 cross_chapter_state，
#   不动 preceding_state / preceding 的既有语义与消费者；任一步失败 → 空，绝不抛。
# ============================================================================

_XCHAPTER_CHARS = int(os.getenv("NWW_XCHAPTER_CHARS", "900") or 900)
_XCHAPTER_COMMIT_CAP = int(os.getenv("NWW_XCHAPTER_COMMITS", "6") or 6)
_XCHAPTER_ROLE_CAP = int(os.getenv("NWW_XCHAPTER_ROLES", "4") or 4)
_XCHAPTER_ROLE_CHARS = int(os.getenv("NWW_XCHAPTER_ROLECHARS", "110") or 110)
_XCHAPTER_FSH_CAP = int(os.getenv("NWW_XCHAPTER_FSH", "6") or 6)
_XCHAPTER_FSH_CHARS = int(os.getenv("NWW_XCHAPTER_FSHCHARS", "70") or 70)
_XCHAPTER_REL_CAP = int(os.getenv("NWW_XCHAPTER_RELS", "8") or 8)

# 关系账本（P1-1）：同目录可选依赖，缺失即降级为空 —— 绝不破坏 packet 构建
try:
    import nww_relation_ledger as _rel  # type: ignore
except Exception:  # noqa: BLE001
    _rel = None

# 全局上下文派生（P1-3）：同上，可选依赖
try:
    import nww_world_context_derive as _dwc  # type: ignore
except Exception:  # noqa: BLE001
    _dwc = None


def _ledger_path(book: Path, chapter_id: int) -> Path | None:
    """定位 追踪/逐章记录/第{N}章.md（兼容 027/27 两种编号）。"""
    d = book / TRACKING_DIR / "逐章记录"
    if not d.is_dir():
        return None
    for pat in (f"第{chapter_id:03d}章*.md", f"第{chapter_id}章*.md"):
        c = sorted(d.glob(pat))
        c = [p for p in c if not p.name.startswith(".")]
        if c:
            return c[-1]
    return None


def _ledger_fields(book: Path, chapter_id: int) -> dict:
    """解析上一章逐章记录的「结果 / 下一章承诺 / 角色变化」。零 LLM、只读。"""
    if chapter_id <= 1:
        return {}
    p = _ledger_path(book, chapter_id - 1)
    if p is None:
        return {}
    try:
        txt = p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return {}

    out: dict = {}
    m = re.search(r"^\s*[-*]\s*结果[：:]\s*(.+)$", txt, re.M)
    if m:
        out["result"] = m.group(1).strip()

    m = re.search(r"^\s*[-*]\s*下一章承诺[：:]\s*(.+)$", txt, re.M)
    if m:
        raw = m.group(1).strip()
        # 中文长承诺串以「；」「;」「，」并列，逐条拆开（保留顺序，去重）
        parts: list[str] = []
        for seg in re.split(r"[；;]", raw):
            s = seg.strip()
            if s and s not in parts:
                parts.append(s)
        out["commitments"] = parts

    # 「## 角色变化」小节： - 沈砚｜核心｜……
    m = re.search(r"^##\s*角色变化\s*$(.*?)(?=^##\s|\Z)", txt, re.M | re.S)
    if m:
        rows = []
        for line in m.group(1).splitlines():
            mm = re.match(r"^\s*[-*]\s*([^｜|]+)｜([^｜|]*)｜(.+)$", line)
            if mm:
                rows.append({"character": mm.group(1).strip(),
                             "level": mm.group(2).strip(),
                             "change": mm.group(3).strip()})
        if rows:
            out["character_changes"] = rows
    return out


def _load_snapshot_for_as_of(book: Path, as_of: int | None) -> dict | None:
    """历史重写时点快照读取：若 as_of < 当前已提交最大章且存在 追踪/快照/第{as_of:03d}章.json，返回该快照。"""
    if as_of is None or as_of <= 0:
        return None
    snap_dir = book / TRACKING_DIR / "快照"
    if not snap_dir.is_dir():
        return None
    for name in (f"第{as_of:03d}章.json", f"第{as_of:02d}章.json", f"第{as_of}章.json"):
        p = snap_dir / name
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
    return None


def _role_state(book: Path, name: str, as_of: int | None = None) -> dict:
    """读 追踪/角色状态/{name}.md 的 位置 / 当前目标 / 身心状态（支持 as_of 历史快照回溯）。零 LLM、只读。"""
    if as_of is not None:
        snap = _load_snapshot_for_as_of(book, as_of)
        if isinstance(snap, dict) and isinstance(snap.get("characters"), dict):
            sc = snap["characters"].get(name)
            if not isinstance(sc, dict):
                return {}
            out_snap: dict = {}
            for fields, key in ((("location",), "location"), (("goal", "current_goal"), "goal"), (("state", "physical_mental_state"), "state")):
                v = ""
                for f_name in fields:
                    v = str(sc.get(f_name) or "").strip()
                    if v and v != "—":
                        break
                if v and v != "—":
                    out_snap[key] = v[:_XCHAPTER_ROLE_CHARS]
            return out_snap
    d = book / TRACKING_DIR / "角色状态"
    if not d.is_dir():
        return {}
    c = sorted(d.glob(f"{name}.md"))
    if not c:
        return {}
    try:
        txt = c[-1].read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return {}
    if as_of is not None:
        fm = re.search(r"首次登场(?:章)?[：:]\s*第?(\d+)", txt)
        if fm and int(fm.group(1)) > as_of:
            return {}
    out: dict = {}
    for label, key in (("位置", "location"), ("当前目标", "goal"), ("身心状态", "state")):
        m = re.search(rf"^\s*[-*]\s*{label}[：:]\s*(.+)$", txt, re.M)
        if m:
            v = m.group(1).strip()
            if v and v != "—":
                out[key] = v[:_XCHAPTER_ROLE_CHARS]
    return out


def _open_foreshadows(book: Path, as_of: int | None = None) -> list[dict]:
    """读 追踪/伏笔.md 表格，取未回收（状态不含 回收/已收/闭合/作废）的条目；支持 as_of 截止过滤。"""
    if as_of is not None:
        snap = _load_snapshot_for_as_of(book, as_of)
        if isinstance(snap, dict) and isinstance(snap.get("foreshadow"), dict):
            out_snap: list[dict] = []
            for fid in sorted(snap["foreshadow"]):
                row = snap["foreshadow"][fid]
                if not isinstance(row, dict):
                    continue
                pc = int(row.get("planted_chapter") or 0)
                if pc > as_of:
                    continue
                st = str(row.get("status") or "已埋")
                resolved = any(w in st for w in ("回收", "已收", "闭合", "作废", "关闭"))
                out_snap.append({
                    "id": fid,
                    "content": str(row.get("summary") or "")[:_XCHAPTER_FSH_CHARS],
                    "planted": f"第{pc}章" if pc else "",
                    "status": st,
                    "resolved": resolved,
                })
            return out_snap
    f = book / TRACKING_DIR / "伏笔.md"
    if not f.is_file():
        return []
    try:
        txt = f.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    rows = [l.strip() for l in txt.splitlines() if l.strip().startswith("|")]
    if len(rows) < 3:
        return []
    header = [c.strip() for c in rows[0].strip("|").split("|")]
    try:
        i_id = header.index("ID"); i_txt = header.index("内容")
        i_ch = header.index("埋设章") if "埋设章" in header else -1
        i_st = header.index("状态") if "状态" in header else -1
        i_last = header.index("最近变更章") if "最近变更章" in header else -1
    except ValueError:
        return []
    out: list[dict] = []
    for r in rows[2:]:
        cells = [c.strip() for c in r.strip("|").split("|")]
        if len(cells) <= max(i_id, i_txt):
            continue
        fid = cells[i_id]
        if not re.match(r"^[A-Za-z]*\d+$", fid):
            continue
        planted_str = cells[i_ch] if 0 <= i_ch < len(cells) else ""
        if as_of is not None and planted_str:
            pm = re.search(r"(\d+)", planted_str)
            if pm and int(pm.group(1)) > as_of:
                continue
        st = cells[i_st] if 0 <= i_st < len(cells) else ""
        last_str = cells[i_last] if 0 <= i_last < len(cells) else ""
        last_num = None
        if last_str:
            lm = re.search(r"(\d+)", last_str)
            if lm:
                last_num = int(lm.group(1))
        is_resolved = any(w in st for w in ("回收", "已收", "闭合", "作废", "关闭"))
        if is_resolved and as_of is not None and last_num is not None and last_num > as_of:
            # 在 as_of 之后才被回收 → 在 as_of 时点仍处于已埋状态
            is_resolved = False
            st = "已埋"
        if is_resolved:
            out.append({"id": fid, "content": cells[i_txt][:_XCHAPTER_FSH_CHARS],
                        "planted": planted_str,
                        "status": st, "resolved": True})
            continue
        out.append({"id": fid, "content": cells[i_txt][:_XCHAPTER_FSH_CHARS],
                    "planted": planted_str,
                    "status": st or "已埋", "resolved": False})
    return out


def _cross_chapter_state_card(book: Path, chapter_id: int,
                               focus_names: list[str] | None = None) -> dict:
    """跨章状态卡：上一章结果 + 下一章承诺 + 角色现状 + 未回收伏笔。

    全部来自 commit 后仍持久化的资产（逐章记录 / 角色状态 / 伏笔），
    不依赖会被消费掉的 delta。零 LLM、只读；任一步失败 → 返回空 dict。
    """
    if _XCHAPTER_CHARS <= 0:
        return {}
    led = _ledger_fields(book, chapter_id)
    if not led:
        return {}

    refs: list[str] = []
    lp = _ledger_path(book, chapter_id - 1)
    if lp is not None:
        try:
            refs.append(str(lp.relative_to(book)))
        except Exception:
            refs.append(lp.name)

    # 承诺条数受限
    commits = list(led.get("commitments") or [])[:_XCHAPTER_COMMIT_CAP]

    # 角色现状：本章出场角色优先，再补其他有变化记录的角色
    names: list[str] = []
    for n in (focus_names or []):
        if n and n not in names:
            names.append(n)
    for row in (led.get("character_changes") or []):
        n = row.get("character", "")
        if n and n not in names:
            names.append(n)
    roles: list[dict] = []
    used = 0
    _as_of_prev = max(0, chapter_id - 1)
    for n in names[:_XCHAPTER_ROLE_CAP]:
        st = _role_state(book, n, as_of=_as_of_prev)
        if not st:
            continue
        item = {"character": n}
        item.update(st)
        seg = f"{n}｜" + "｜".join(f"{k}={v}" for k, v in st.items())
        if used + len(seg) > _XCHAPTER_CHARS // 2:
            continue
        roles.append(item)
        used += len(seg)
        r = f"{TRACKING_DIR}/角色状态/{n}.md"
        if r not in refs:
            refs.append(r)

    # 未回收伏笔（最缺的一环：实测钦天监 4 条全部「已埋」、0 回收；按 as_of=chapter_id-1 截断）
    fsh_all = _open_foreshadows(book, as_of=_as_of_prev)
    fsh_open = [f for f in fsh_all if not f.get("resolved")][:_XCHAPTER_FSH_CAP]
    if fsh_open:
        r = f"{TRACKING_DIR}/伏笔.md"
        if r not in refs:
            refs.append(r)

    # 关系声明（P1-1）：只给「已声明」的关系，另一方向无声明 = UNOBSERVED，
    # 绝不伪造双向关系，也不推导关系的客观当前状态。
    rels: list[dict] = []
    if _rel is not None and _XCHAPTER_REL_CAP > 0:
        # 细纲未列 subjects 时，退回「上一章有角色变化的角色」——保证关系网不因
        # 细纲格式差异而整体缺失（实测 ch12 的 subjects 为空 → 关系 0 条）。
        _focus = list(focus_names or []) or [r.get("character", "")
                                             for r in (led.get("character_changes") or [])]
        try:
            rels = _rel.relations_for(book, _focus, _XCHAPTER_REL_CAP, as_of=chapter_id)
        except Exception:
            rels = []
    if rels:
        # 引用真实存在的证据文件（账本可能尚未落盘，故用声明的原始出处）
        for item in rels:
            ev = (item.get("evidence") or "").split("#")[0].strip()
            if ev and ev not in refs:
                refs.append(ev)

    out = {
        "prev_chapter": chapter_id - 1,
        "prev_result": led.get("result", ""),
        "open_commitments": commits,
        "character_states": roles,
        "relations": rels,
        "open_foreshadows": fsh_open,
        "resolved_foreshadows": len([f for f in fsh_all if f.get("resolved")]),
        "source_refs": refs,
    }
    return out


def _preceding_v2(book: Path, chapter_id: int, max_chars: int | None = None) -> str:
    """前情：正文目录递归找上一章文件，取末尾 N 字（默认 1500，env 可调；旧默认 300）。"""
    if max_chars is None:
        max_chars = _PRECEDING_DEFAULT_CHARS
    if chapter_id <= 1:
        return "【序幕开篇】"
    body = book / BODY_DIR
    if not body.is_dir():
        return "【前文衔接缺失】"
    cand = _find_prev_chapter_file(body, chapter_id)
    if cand is None:
        return "【前文衔接缺失】"
    text = norm(cand.read_text(encoding="utf-8"))
    lines = [l for l in text.splitlines() if not l.startswith("#")]
    clean = "\n".join(lines).strip()
    return clean[-max_chars:] if len(clean) > max_chars else clean



# —— C1-2 Canon Projection（2026-09-23，CCv2 事实输入质量升级）——
# 三层确定性过滤：tier gate（仅 confirmed）→ relevance gate（subject > relationship > rule）→ 容量 30。
# 不做 LLM 判断（Canon 层最怕用模型决定什么事实重要）；pending/deleted 绝不进投影。
_CANON_CAP = 30
_RULE_TYPES = {"world_rule", "world_state", "relationship"}


def _canon_projection(state: dict, subjects: list[str], as_of_chapter: int | None = None) -> dict:
    """把 tracking facts.confirmed 投影为本章相关事实（确定性过滤，支持 as_of_chapter 截止隔离）。

    subjects: 本章锚（character_contract 角色名 + scene_chain driver_goal.actor）。
    返回 {"confirmed": [...], "meta": {...}}；confirmed 内不含 pending/deleted（A1），
    且当 as_of_chapter 指定时严格过滤 evidence.chapter / introduced_chapter <= as_of_chapter（A4）。
    """
    facts = ((state.get("facts") or {}).get("confirmed") or [])
    # tier gate + as_of gate：只取 tier == "confirmed"，且不晚于 as_of_chapter
    confirmed = []
    for f in facts:
        if (f.get("tier") or "confirmed") != "confirmed":
            continue
        if as_of_chapter is not None:
            ev = f.get("evidence") or {}
            src = f.get("source") or {}
            ev_ch = ev.get("chapter") if isinstance(ev, dict) and ev.get("chapter") is not None else (
                src.get("chapter") if isinstance(src, dict) else None
            )
            intro_ch = f.get("introduced_chapter") or f.get("recorded_chapter")
            ch_val = ev_ch if isinstance(ev_ch, int) else (intro_ch if isinstance(intro_ch, int) else None)
            if ch_val is not None and ch_val > as_of_chapter:
                continue
        confirmed.append(f)
    dropped = len(facts) - len(confirmed)

    subs = set(s for s in (subjects or []) if s)
    if not subs:
        # 无 subjects 锚：退化——仍只投影 confirmed，按规则类优先取前 30（不引入污染）
        ranked = sorted(confirmed, key=lambda f: (f.get("fact_type") in _RULE_TYPES, 0), reverse=True)
        picked, meta_reason = ranked[:_CANON_CAP], "no_subjects_fallback"
    else:
        scored: list[tuple[int, int, dict]] = []
        ftexts = [(f.get("fact") or "") for f in confirmed]
        for i, f in enumerate(confirmed):
            fsub = f.get("subject") or ""
            ftext = ftexts[i]
            if fsub in subs:
                score = 3  # A. subject 直接命中
            elif any(s in ftext for s in subs):
                score = 2  # B. relationship 链（subject 缺失时事实文本提及本章主体，旧数据兼容）
            elif f.get("fact_type") in _RULE_TYPES:
                score = 1  # C. 规则类约束（主体未出场也可能约束本章）
            else:
                score = 0
            if score:
                scored.append((score, -i, f))
        scored.sort(reverse=True)
        picked = [f for _, _, f in scored[:_CANON_CAP]]
        meta_reason = f"scored:{len(scored)}"

    # 输出（A1/A4 保证：仅 confirmed 且 introduced ≤ 本章；A2 报告缺 quote 不阻塞）
    out = []
    missing_q = 0
    for f in picked:
        ev = f.get("evidence") or {}
        src = f.get("source") or {}
        quote = (ev.get("quote") if isinstance(ev, dict) else "") or (
            src.get("span") if isinstance(src, dict) else ""
        ) or ""
        ev_chapter = (
            ev.get("chapter") if isinstance(ev, dict) and ev.get("chapter") is not None else (
                src.get("chapter") if isinstance(src, dict) and src.get("chapter") is not None else f.get("recorded_chapter")
            )
        )
        if not quote:
            missing_q += 1
        subj = f.get("subject", "")
        if not subj and subs:
            ft = f.get("fact", "")
            for s_cand in sorted(subs, key=len, reverse=True):
                if s_cand and s_cand in ft:
                    subj = s_cand
                    break
        out.append({
            "id": f.get("id", ""),
            "subject": subj,
            "predicate": f.get("predicate", ""),
            "value": f.get("value", ""),
            "fact": f.get("fact", ""),
            "fact_type": f.get("fact_type", "") or (src.get("type", "") if isinstance(src, dict) else ""),
            "evidence": {
                "chapter": ev_chapter,
                "quote": quote,
            },
        })
    meta = {
        "relevance_hits": len(scored) if 'scored' in dir() else 0,
        "filtered": {"pending_deleted": dropped, "missing_quote": missing_q},
        "subjects_used": sorted(subs),
        "reason": meta_reason,
    }
    return {"confirmed": out, "meta": meta}


def _chapter_subjects(acts: list[dict], chars: list[dict]) -> list[str]:
    """本章 subjects 锚（确定性提取）：character_contract 角色名 + scene_chain driver_goal.actor + decision 主体。

    规范化：剥离括号注（如「温故（在林小凡的旁观下）」→「温故」），去空白。
    """
    def _norm(nm: str) -> str:
        nm = re.sub(r"[（(].*?[）)]", "", nm).strip()
        return nm

    subs: list[str] = []
    for c in chars:
        nm = _norm(c.get("name") or "")
        if nm and nm not in subs:
            subs.append(nm)
    for a in acts:
        dg = a.get("driver_goal") or {}
        act = _norm(dg.get("actor") if isinstance(dg, dict) else "")
        if act and act not in subs:
            subs.append(act)
    return subs


def _canon_delta_outline_explicit(release_items: list[str], invent_items: list[str]) -> dict:
    """canon_delta（C1-2，outline_explicit only）。

    用户裁决：第一阶段 may_add 只允许细纲显式声明（must_release 中跨章性条目），
    不做 scene_derived 自动推导（scene_chain 是剧情计划，不是事实授权）。
    may_not_contradict 由投影相关事实 ID 提供（builder 上层填充，此处先留空由 projection 补）。
    """
    # 保守：must_release 中带「掌握/成为/确立/解锁/进入/规则/机制」等跨章性动词的条目
    _cross_chapter_kw = ("掌握", "成为", "确立", "解锁", "进入", "规则", "机制", "融合", "突破")
    may = [it for it in (release_items or []) if any(k in it for k in _cross_chapter_kw)]
    return {"may_add": [{"item": it, "source": "outline_explicit"} for it in may], "may_not_contradict": []}


# ============================================================================
# §7-1 Writing Packet v2.3 三新键（2026-10-11 独立实施 GO）
# 规则冻结：docs/Writing_Packet_v2_§7-1字段契约冻结稿_20261011.md（§2 scene_time /
# §3 character_resources / §4 character_forbids + §11.2 R1–R12）
# 定位：扩展接入——只新增顶层键与独立解析函数；不改动既有字段、既有解析与既有执行路径。
# 原样取自冻结原型 /tmp/nww_proto_v2_forbids_v2.py（SHA 1617405f…，v2.3 / R1–R12）。
# ============================================================================

ROLE_CARD_DIR = "设定/角色"

# —— R1–R12 冻结正则（触发/作用域/排除）——
_F_GATE_HEAD = re.compile(r"^#{2,4}\s*.*(?:禁用表达|禁用行为|禁止行为|角色禁区).*$", re.M)
_F_NEXT_HEAD = re.compile(r"^#{2,4}\s*.*$", re.M)
_F_SUB_LABEL = re.compile(r"^[ \t]*[-*]?[ \t]*\*{0,2}(?:绝对)?禁止(?:行为|表达)\*{0,2}[ \t]*[：:]?[ \t]*$", re.M)
_F_ITEM = re.compile(r"^[ \t]*-[ \t]+\S", re.M)
_F_QUOTE = re.compile(r"[「『“\"]([^」』”\"]{1,40})[」』”\"]")
_F_PREFIX = re.compile(r"(?:绝对)?(?:禁止|禁用|不自称|不写|不得|不与他人)")
_F_STORY = re.compile(r"(ch\s?\d|第\d+章|落地后|登台|亮相|对证人|剧情安排|剧情说明)")
_F_ALLOW_T = re.compile(r"(仍可|仍能|仍要|可以|允许|少量|示例|余地|可以不)")
_F_FORBID_T = re.compile(r"(不行|不可|禁用|禁止|不得|自称)")
_F_ALLOW_HEAD = re.compile(r"(?:必称|改用|换用|代替|藏在|的壳|的是|的不是|写作|记为|视为|示意|余地|允许说|允许|仍可)[「『“\"]?$")
_F_BAN_EXPL = re.compile(r"(绝对禁止|禁止|禁用|严禁|绝不|不许|不准|不自称|不与他人|不写成|不符合)")
_F_LABEL_LINE = re.compile(r"^[ \t]*[-*]?[ \t]*\*{0,2}(?:绝对)?(?:禁止|禁用)(?:行为|表达|事项)\*{0,2}[ \t]*[：:]?[ \t]*$")
_F_NEG_QUOTE = re.compile(r"(?:不靠明说|不靠说|不要直接说|不能直接说|不要直说|不能用|不要用|不靠)[「『“\"]?$")
_F_POSITION = re.compile(r"[（(]([^（()）]*)[)）]")


def _f_regions(text: str) -> list:
    """R1 区块门 + R2 行级子标签作用域（返回 (start, title, body, body_offset)）。"""
    out = []
    for m in _F_GATE_HEAD.finditer(text):
        nxt = _F_NEXT_HEAD.search(text, m.end())
        out.append((m.start(), m.group(0).strip(),
                    text[m.end(): nxt.start() if nxt else len(text)], m.end()))
    for m in _F_SUB_LABEL.finditer(text):
        nh = _F_NEXT_HEAD.search(text, m.end())
        nt = re.search(r"^-[ \t]+\S", text[m.end():], re.M)
        end = min([e for e in [len(text), nh.start() if nh else None,
                               (m.end() + nt.start()) if nt else None] if e is not None])
        out.append((m.start(), m.group(0).strip(), text[m.end(): end], m.end()))
    return out


def _f_tail_allow(tail: str) -> bool:
    """R5：尾部允许词归属限定（其后 4 字内无引号开启符，否则归属下一条引号）。"""
    for m in re.finditer(r"(仍可|仍能|仍要|可以|允许|少量|余地|可以不)", tail):
        if not re.search(r"[「『“\"]", tail[m.end(): m.end() + 4]):
            return True
    return False


def _f_analyze(s: str) -> tuple:
    """R3–R12 固定执行顺序判定，返回 (status, expressions, reason)；status ∈ V/U/X。"""
    if _F_STORY.search(s):
        return "X", set(), "剧情安排/引用归属"
    qm = list(_F_QUOTE.finditer(s))
    allowed = set()
    for i, q in enumerate(qm):
        val, pos = q.group(1), q.start(1)
        head = s[max(0, pos - 16):pos]
        nxt = qm[i + 1].start() if i + 1 < len(qm) else None
        tail = s[pos + len(val): (nxt + 1 if nxt is not None else len(s))]
        if _F_ALLOW_HEAD.search(head):
            allowed.add(val)
        elif _F_NEG_QUOTE.search(head):
            allowed.add(val)
        elif _f_tail_allow(tail) and not _F_FORBID_T.search(tail[:6]):
            allowed.add(val)
    quotes = [(q.group(1), q.start(1)) for q in qm]
    pair = re.search(r"[「『]([^」』]+)[」』][^「『]{0,6}可以[^「『]*[「『]([^」』]+)[」』][^「『]{0,6}不行", s)
    if pair:
        return "V", {pair.group(2)}, "成对(X可以Y不行)"
    rpair = re.search(r"[「『]([^」』]+)[」』][^「『]{0,6}不行[^「『]*[「『]([^」』]+)[」』][^「『]{0,6}可以", s)
    if rpair:
        return "V", {rpair.group(1)}, "成对(X不行Y可以)"
    m = re.search(r"禁用把[^，。]*?写成([^——，。（]+)", s)
    if m:
        return "V", {m.group(1).strip()}, "禁用+非引号对象"
    for q, pos in quotes:
        tail = s[pos + len(q): pos + len(q) + 24]
        if _F_ALLOW_T.search(tail) and re.search(r"(不行|不可|禁用|禁止|不得)", tail[6:]):
            return "U", set(), "允许/禁止范围无法分离→UNDET"
    forbid = {q for q, _ in quotes if q not in allowed}
    has_pref = bool(_F_PREFIX.search(s))
    if has_pref and forbid:
        return "V", forbid, "前缀+引号对象"
    if has_pref and not forbid:
        return "V", set(), "前缀+名词(无引号)"
    if not has_pref:
        if forbid:
            return "V", forbid, "B2区块继承+引号对象"
        if _F_BAN_EXPL.search(s):
            tailtxt = s.split("：", 1)[1] if "：" in s else re.sub(r"^[-*][ \t]+", "", s)
            cand = re.split(r"[（(]", tailtxt)[0].strip().rstrip("。；;")
            if cand:
                objs = {o.strip() for o in re.split(r"[、，,]", cand) if o.strip()}
                return "V", objs, "B2区块继承+显式标记"
        return "U", set(), "无显式禁令标记/对象不明"
    return "U", set(), "混合句范围不明"


def build_character_forbids(book: Path) -> list:
    """§4：设定/角色/*.md → 有效禁令（仅 V）。U 只进 stderr 审计，不入本字段。"""
    out: list = []
    card_dir = book / ROLE_CARD_DIR
    if not card_dir.exists():
        return out
    undet: list = []
    for card_path in sorted(card_dir.glob("*.md")):
        card = card_path.stem
        text = card_path.read_text(encoding="utf-8")
        seen = set()
        for soff, title, body, boff in sorted(_f_regions(text)):
            if (soff, title) in seen:
                continue
            seen.add((soff, title))
            for im in _F_ITEM.finditer(body):
                eol = body.find("\n", im.start())
                rule = body[im.start(): eol if eol >= 0 else len(body)].strip()
                if _F_LABEL_LINE.match(rule):     # R11 纯标签行只声明作用域
                    continue
                line = text[:boff + im.start()].count("\n") + 1
                st, exp, why = _f_analyze(rule)
                if st == "U":
                    undet.append({"character": card, "line": line, "rule_text": rule, "reason": why})
                    continue
                if st != "V":
                    continue
                src = {"file": f"{ROLE_CARD_DIR}/{card}.md", "section": title,
                       "rule_text": rule, "line": line}
                objs = sorted(exp) if exp else [rule]
                for o in objs:
                    out.append({"character": card, "expression": o, "source": dict(src)})
    if undet:
        sys.stderr.write("[S71-UNDET] 未判定 %d 条（不入 character_forbids）：%s\n"
                         % (len(undet), json.dumps(undet, ensure_ascii=False)))
    return out


# —— 角色底蕴层（character_depth，2026-10-25 新增）——
# 根因：写前链路对 设定/角色/*.md 只取「禁令」（character_forbids）与兜底时的口头禅条目，
#   角色卡里的 情绪反应模式/语言指纹/恐惧欲望/前史/行为标准 全部到不了 writer 手里，
#   角色只能按细纲功能演出（审计结论「所有角色都在执行功能」的上游之一）。
# 修法：把本章出场角色的**整张角色卡原文**（截断）注入 character_depth。
#   不做小节级解析——卡的结构由建书模板约定，注入原文最稳、不依赖字段拼写。
# 开关：NWW_CHAR_DEPTH=0 可整体关闭（回滚点）。默认开：没有卡的角色键不出现，零副作用。
_CHAR_DEPTH_SWITCH = "NWW_CHAR_DEPTH"
_CHAR_DEPTH_CARD_CHARS = int(os.environ.get("NWW_CHAR_DEPTH_CARD_CHARS", "1600") or 1600)
_CHAR_DEPTH_TOTAL_CHARS = int(os.environ.get("NWW_CHAR_DEPTH_TOTAL_CHARS", "4800") or 4800)


def _char_depth_enabled() -> bool:
    return os.environ.get(_CHAR_DEPTH_SWITCH, "1") == "1"


def build_character_depth(book: Path, names: list[str]) -> dict:
    """设定/角色/{name}.md 整卡注入（截断），仅本章出场角色。失败即空，绝不破坏既有路径。"""
    if not _char_depth_enabled():
        return {}
    card_dir = book / ROLE_CARD_DIR
    if not card_dir.exists():
        return {}
    out: dict = {}
    used = 0
    for name in dict.fromkeys(n.strip() for n in names if n and n.strip()):
        if used >= _CHAR_DEPTH_TOTAL_CHARS:
            break
        p = card_dir / f"{name}.md"
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8").strip()
        except Exception:
            continue
        if not text:
            continue
        seg = text[:min(_CHAR_DEPTH_CARD_CHARS,
                        _CHAR_DEPTH_TOTAL_CHARS - used)]
        out[name] = seg
        used += len(seg)
    return out


# scene_time 填充开关 —— **默认关**（2026-10-12 元审查后更正）。
#
# ★★ 更正说明（重要，勿再按旧说法改回默认开）：
#   我曾判定 scene_time 是「契约定义了但从未实现的空壳」，据此默认开启填值。
#   这是**错的**：`docs/Writing_Packet_v2_§7-1字段契约冻结稿_20261011.md` §2.1 明文写着
#     「真实路径：追踪/_tracking-state.json → context.position（:199 scene / :200 story_time）
#       —— **仅作背景，本轮不注入其值**」
#     并给出 `scene_time = {"story_time": null, "scene": null}`。
#   即冻结契约**明确指定本轮该字段为 null**，不是"漏实现"。
#   另有 `projects/admin/P4B_第二刀_LossPoint1_最小实施稿.md:85`：
#     「不新增 Packet 顶层字段（不加 scene_time …）」——scene_time 的注入被划归后续刀次。
#   → 冻结契约不可擅改。填充能力保留为可选，但**默认必须关闭**；
#     若要默认开启，须先修订冻结契约 + 评估下游消费者，不能由我单方面判定。
_SCENE_TIME_SWITCH = "NWW_SCENE_TIME"


def _scene_time_enabled() -> bool:
    return os.environ.get(_SCENE_TIME_SWITCH, "0") == "1"


# —— §W4 宏观连续性上下文（macro_context）——
# ★★ 这是「厚度」缺口里最大的一个洞（2026-10-12 实测确诊）。
#
# 现象：`追踪/上下文.md` 由 nww_track_commit.py:2284 每次 commit 渲染，
#   钦天监 5336B，里面是系统**自己已经整理好**的宏观资产：
#     · 长期约束 7 条（约束与秩序维度）
#     · 近三章速记（跨章累积维度）
#     · 连贯性风险 5 条（宏观发展关系维度）
#     · 当前位置 / 活跃伏笔 / 下一章承诺
#   但 **Web 链路（本文件）确定性不读它** —— packet 的 context 只有 `preceding`
#   （上一章正文最后 1500 字的原始片段，只有语气与情势）。
#
# ★ 表述更正（2026-10-12 元审查自查，勿再传播旧说法）：
#   我一度写成「全仓没有任何读取方」，**不准确**。真实分工是：
#     · 本地 skill 链路：`agents/nww-explorer.md:172` 明确要求 `Read 追踪/上下文.md`
#       （且规定它必须恰好 7 栏）；consistency SKILL.md:24、hooks/session-start.sh:132、
#       hooks/pre-compact.sh 也读它。
#     · 但那份 2026-09-14 的审计报告已查证：这些都是**靠 LLM 自觉转述，无任何脚本校验**
#       （`NWW系统审核报告_第24章起崩坏根因_20260914.md:29`），而 writer 被设计为被动接收
#       父流程拼装的 prompt、自己不读任何状态文件 → 转述是否真的发生，没人知道。
#     · **Web 链路（本文件）则是确定性不读** —— 这是可以确定性修的那一半。
#   → 所以本次修的是「Web 链路确定性注入」，把"靠 LLM 自觉"换成"脚本保证"。
#     同时应记录：该问题 2026-09-14 已被查证、挂了约一个月未修，本次是独立重推后落地。
#
# 修法：确定性解析 上下文.md 的这几个段落 → 进 packet → writer 合同化处置。
#   只读、零 LLM、失败即空。不写任何追踪文件。
_MACRO_FILE = "上下文.md"
# 段落名 → 输出键
_MACRO_SECTIONS = (
    ("长期约束", "long_term_constraints"),
    ("近三章速记", "recent_chapters"),
    ("连贯性风险", "continuity_risks"),
    ("全局状态", "global_state"),
    ("当前位置", "current_position"),
)
_MACRO_ITEM_CAP = 8          # 每段最多条目
_MACRO_TOTAL_CHARS = 2400    # 全段总字数硬帽（约占 4000 字窗口的 6%）


def _macro_context_card(book: Path, chapter_id: int | None = None) -> dict:
    """读 追踪/上下文.md，抽出 writer 真正需要的宏观段落。

    只提取「不可从上一章正文末尾推断」的部分：长期约束 / 近三章速记 /
    连贯性风险 / 全局状态 / 当前位置。活跃伏笔与下一章承诺已由
    cross_chapter_state 承载，此处**不重复**注入（避免同一信息两处、口径分裂）。
    """
    out: dict = {}
    f = book / TRACKING_DIR / _MACRO_FILE
    if not f.is_file():
        return out
    try:
        text = f.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return out
    # 按 `## ` 切段
    cur = None
    buckets: dict[str, list[str]] = {}
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("## "):
            cur = s[3:].strip()
            buckets.setdefault(cur, [])
            continue
        if cur and s.startswith("- "):
            item = s[2:].strip()
            if item and "尚未写入全局状态" not in item:
                buckets[cur].append(item)
    # 2026-10-26 历史重写时点隔离：若指定了 chapter_id，过滤掉 recent_chapters 等段落中 >= chapter_id 的未来条目
    for sec_name, key in _MACRO_SECTIONS:
        raw_items = [x for x in buckets.get(sec_name, []) if x]
        if chapter_id is not None and key == "recent_chapters":
            filtered_rc = []
            for x in raw_items:
                m_ch = re.match(r"^第0*(\d+)章", x.strip())
                if m_ch and int(m_ch.group(1)) >= chapter_id:
                    continue
                filtered_rc.append(x)
            # 若上下文.md 仅存最新三章导致过滤后为空，从 <= chapter_id - 1 的逐章记录确定性回填
            if not filtered_rc and chapter_id > 1:
                for prev_ch in range(chapter_id - 1, max(0, chapter_id - 4), -1):
                    lp = _ledger_path(book, prev_ch)
                    if lp is not None:
                        ltxt = lp.read_text(encoding="utf-8", errors="ignore")
                        rm = re.search(r"^\s*[-*]\s*结果[：:]\s*(.+)$", ltxt, re.M)
                        if rm:
                            filtered_rc.append(f"第{prev_ch:03d}章：{rm.group(1).strip()}")
            raw_items = filtered_rc
        items = raw_items[:_MACRO_ITEM_CAP]
        if items:
            out[key] = items
    # 总字数硬帽 —— ★ 元审查更正（2026-10-12）：
    #   旧实现是「从最长的段开始裁」，等于按文档顺序隐式截断，
    #   可能把最硬的约束裁掉、却留下低优先级的背景。改为**按优先级裁剪**：
    #   高优先级段落最后才裁，且每段至少保 1 条。
    _PRIORITY = ("long_term_constraints", "continuity_risks", "recent_chapters",
                 "global_state", "current_position")

    def _size(d: dict) -> int:
        return sum(len(x) for v in d.values() if isinstance(v, list) for x in v)

    for key in reversed(_PRIORITY):          # 从最低优先级开始裁
        while _size(out) > _MACRO_TOTAL_CHARS and key in out:
            if len(out[key]) <= 1:           # 每段至少保 1 条
                break
            out[key].pop()
        if _size(out) <= _MACRO_TOTAL_CHARS:
            break
    out["source"] = f"{TRACKING_DIR}/{_MACRO_FILE}"
    out["trimmed"] = _size(out) >= _MACRO_TOTAL_CHARS
    return out


def _scene_time_card(state: dict, chapter_id: int | None = None) -> dict:
    """§7-1 §2 scene_time：本章开场的「故事时间 / 场景」锚（2026-10-12 修复死桩）。

    ★ 修复前这里是硬编码 `{"story_time": None, "scene": None}` —— 契约冻结稿
    （docs/Writing_Packet_v2_§7-1字段契约冻结稿_20261011.md §2）定义了这个键，
    但生产链从没填过值，实测 4 本书全部为 null。

    为什么值得修：MEMORY §0 实测「章首 150 字无任何时间/过渡标记」——
    全民棋局 95%、诡异 80%、五行归源 74%、钦天监 70%、泡面 67%。
    writer 拿不到「此刻是何时、人在何处」，只能凭 preceding 末尾的语气猜，
    于是章首普遍无过渡、跨章时间线漂移（如泡面 ch1 夜 → ch2 晨无任何交代）。

    数据源：`state["context"]["position"]`（commit 侧 normalize_context 已强校验：
    story_time/scene 均为必填字符串，见 nww_track_commit.py:358-364）。
    零 LLM、只读。缺 state / 缺 position / 类型不符 → 回退到全 null（与修复前一致）。

    只增不改：只填这个本来就存在但恒空的键，不动其他任何字段。
    """
    out = {"story_time": None, "scene": None}
    if not _scene_time_enabled():
        return out
    try:
        pos = ((state or {}).get("context") or {}).get("position") or {}
        if not isinstance(pos, dict):
            return out
        for k in ("story_time", "scene"):
            v = pos.get(k)
            if isinstance(v, str) and v.strip():
                out[k] = v.strip()
        vol = pos.get("volume")
        if isinstance(vol, str) and vol.strip():
            out["volume"] = vol.strip()
        vsc = pos.get("volume_start_chapter")
        if isinstance(vsc, int) and not isinstance(vsc, bool):
            out["volume_start_chapter"] = vsc
    except Exception:
        return {"story_time": None, "scene": None}
    return out


def build_character_resources(state: dict) -> dict:
    """§3：characters[*].abilities_resources → {角色:[{text,position}]}；仅有效字符串条目。"""
    out: dict = {}
    chars = state.get("characters") if isinstance(state, dict) else None
    if not isinstance(chars, dict):
        return out
    skipped: list = []
    for name, snap in chars.items():
        if not isinstance(snap, dict):
            continue
        items = snap.get("abilities_resources")
        if not isinstance(items, list):
            continue
        rows: list = []
        for it in items:
            if not isinstance(it, str):
                skipped.append({"character": name, "type": type(it).__name__})
                continue
            m = _F_POSITION.search(it)
            rows.append({"text": it, "position": m.group(1) if m else ""})
        if rows:                                  # 空列表角色不生成键
            out[name] = rows
    if skipped:
        sys.stderr.write("[S71-RESOURCE-SKIP] 跳过非字符串条目 %d 条：%s\n"
                         % (len(skipped), json.dumps(skipped, ensure_ascii=False)))
    return out


# —— P0-1 契约断裂修复（2026-10-25）：写前对齐补充块的结构化兜底解析 ——
# 实测（仙门书 ch1，10 次生成回执）：prealign 回补产出无 `#` 前缀的 FAIL 块
# （`FAIL 5. 出场角色 character_contract（角色Voice锚点）补充：`），_sec_loose 全失明
# → character_contract/hook/落点 10 次全部 missing、PARTIAL 不阻断 → voice 锚点从未进 writer。
_RE_FAIL_HEADER = re.compile(r"^(?:#{1,6}\s*)?FAIL\s*\d+\.\s*(.*)$", re.M)


def _fail_blocks(text: str) -> list[tuple[str, str]]:
    """拆 FAIL 补充块为 (标题, 正文)。兼容有无 `#` 前缀两种形态；
    块内遇下一级 markdown 标题即截断，防止吞掉后续正常小节。"""
    blocks: list[tuple[str, str]] = []
    matches = list(_RE_FAIL_HEADER.finditer(text))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        nxt = re.search(r"^#{2,4}\s*\S", text[start:end], re.M)
        if nxt:
            end = start + nxt.start()
        blocks.append((m.group(1).strip(), text[start:end].strip()))
    return blocks


def _parse_characters_repair(text: str) -> list[dict]:
    """FAIL 块兜底解析出场角色 voice 锚点（无 `#` 前缀的写前对齐补充）。

    条目形态（2026-10-25 实测）：裸行 `姜禾：①口癖/句式：…；②动作习惯：…；③禁用表达：…`。
    仅接受含 ① 标记的条目行，防误捕子字段行与决策脊柱行。"""
    chars: list[dict] = []
    for title, body in _fail_blocks(text):
        if not (re.search(r"出场角色", title) and re.search(r"voice|Voice|锚点|character_contract", title)):
            continue
        for lm in re.finditer(r"^[-*\s]*\*{0,2}(.+?)\*{0,2}[：:]\s*(.+)$", body, re.M):
            name, entry = lm.group(1).strip(), lm.group(2).strip()
            if re.search(r"[①-⑩]", name) or "①" not in entry:
                continue
            name_clean = re.sub(r"（.*?）", "", name).strip()
            if not name_clean or len(name_clean) > 16:
                continue
            parts = [p.strip() for p in re.split(r"[①-⑩]", entry) if p.strip()]
            behavior = [p for p in parts if "禁用" in p]
            voice = [p for p in parts if p not in behavior]
            chars.append({
                "name": name_clean,
                "role": "",
                "voice_constraints": voice,
                "behavior_constraints": behavior,
                "state": "",
            })
        if chars:
            break
    return chars


# 数字锚（P0-4）：数字+量词承诺短语 → must_keep，写后确定性比对基线。
# lookbehind 防止从复杂数词中段起匹配（如「唯一一瓶」抽出「一一瓶」）。
_NUM_CN = r"(?<![零一二两三四五六七八九十百千\d第])(?:[零一二两三四五六七八九十百千]+|\d+(?:\.\d+)?)"
_NUM_ANCHOR_PATS = (
    re.compile(rf"({_NUM_CN})\s*(?:块|枚|颗)\s*(?:下品|中品|上品|极品)?灵石"),
    re.compile(rf"({_NUM_CN})\s*(?:份|瓶|支|包|名|人|层|间|张|日|天|年)"),
)
# 量词虚指（「一支遮瑕膏」「唯一一瓶」）：数词为一/一一且单位是件数量词时不是剧情承诺
_NUM_ANCHOR_ARTICLE_UNITS = ("支", "瓶", "名", "人", "张", "间", "份", "包")

# —— 规则锚（2026-10-25 P0-4b：定性规则承诺）——
# 数字锚只查数字，实测漏掉「初犯警告/再犯夺五感三年」这类**定性处罚力度**承诺，
# 导致同一规则两章力度漂移（摔一跤 vs 夺五感三年）无任何机械比对。
# 修法：细纲中含 处罚/惩罚/初犯/再犯/处断/拉黑 等规则性关键词的行 → must_keep（writer 硬约束）。
# 只增不改：独立函数与常量，不改 _extract_numeric_anchors 行为。
_RULE_ANCHOR_PAT = re.compile(
    r"(?:处罚|惩罚|处断|初犯|再犯|三犯|禁武|拉黑|夺.{0,6}(?:三年|五年|十年)|(?:警告|重罚|废掉|轰飞))"
)
_RULE_ANCHOR_LINE_CAP = 120
_RULE_ANCHOR_MAX = 8


def _extract_rule_anchors(text: str, chapter_id: int = 0) -> list[str]:
    """从细纲提取「定性规则承诺」行（处罚力度/规则后果），进 must_keep。

    排除跨章规划行（同 _extract_numeric_anchors 口径）。"""
    out: list[str] = []
    seen: set[str] = set()
    for ln in text.splitlines():
        s = ln.strip()
        if not s or len(s) < 8:
            continue
        if not _RULE_ANCHOR_PAT.search(s):
            continue
        if re.search(rf"第(?:{chapter_id + 1}|{chapter_id + 2}|{chapter_id + 3}|[5-9]\d*)\s*章", s):
            continue
        key = s[:60]
        if key in seen:
            continue
        seen.add(key)
        out.append(s[:_RULE_ANCHOR_LINE_CAP])
        if len(out) >= _RULE_ANCHOR_MAX:
            break
    return out


def _extract_numeric_anchors(text: str, chapter_id: int = 0) -> list[str]:
    """从细纲提取「数字+量词」承诺短语（首单价、库存数、限购数、天数等）。

    排除：字数目标行（2500-2750 是字数口径不是剧情数字）；跨章规划行（含
    第N章(>本章) 引用的章间升级/承接设计，其数字属于后续章，不得进本章 must_keep）；
    量词虚指（「一支」）。FAIL 补充块内的本章机制数字（每日限购3支、2块下品灵石）
    是正式承诺，必须纳入。输出带上下文便于人工复核。"""
    out: list[str] = []
    seen: set[str] = set()
    for ln in text.splitlines():
        if re.search(r"字数目标|字数口径|硬下限|目标情绪|情绪弧线|W形|G[0-9]\s|E[1-5]\s|12层角色卡|五张12层|去AI味", ln):
            continue
        # 跨章规划行：本章锚点不得携带后续章的数字承诺
        if re.search(rf"第(?:{chapter_id + 1}|{chapter_id + 2}|{chapter_id + 3}|[5-9]\d*)\s*章", ln):
            continue
        for pat in _NUM_ANCHOR_PATS:
            for m in pat.finditer(ln):
                num, unit = m.group(1), (m.group(0)[-1] if m.group(0)[-1] not in "块枚颗" else "")
                s = m.group(0).strip()
                if s in seen or len(s) > 20:
                    continue
                if num in ("一", "一一") and unit in _NUM_ANCHOR_ARTICLE_UNITS:
                    continue
                seen.add(s)
                out.append(f"数字锚点（数值必须与细纲一致，不得改动/夸大/另创）：{s}（细纲原句：{ln.strip()[:80]}）")
    return out[:12]


def _extract_banned_terms(text: str) -> list[str]:
    """W6：细纲 `【旁白禁词表】` → 提交期 must_avoid 的确定性条目。

    只收本行以「／」或「/」分隔的词，不扫正文其余部分（避免把"本章禁词说明"
    里的引用误当承诺）。零 LLM；无该行返回 []。
    """
    for ln in text.splitlines():
        if "旁白禁词表" not in ln:
            continue
        payload = ln.split("：", 1)[-1].split(":", 1)[-1]
        payload = re.sub(r"^（[^）]*）", "", payload).strip()
        terms = [t.strip() for t in re.split(r"[／/、,，]", payload) if t.strip()]
        return [f"旁白禁词：{t}" for t in terms if len(t) <= 12][:24]
    return []


def build_packet_v2(chapter_id: int, book_root_str: str) -> dict:
    book = Path(book_root_str)
    if not book.exists():
        sys.stderr.write(f"[ERROR] 工作区不存在: {book_root_str}\n")
        sys.exit(1)

    # 定位细纲（支持前导零与标题后缀）
    outline_path = None
    for name in (f"细纲_第{chapter_id}章.md", f"细纲_第{chapter_id:02d}章.md", f"细纲_第{chapter_id:03d}章.md",
                 f"细纲_第{chapter_id}章.json", f"outline_{chapter_id}.md"):
        p = book / OUTLINE_DIR / name
        if p.exists():
            outline_path = p
            break
    if outline_path is None and (book / OUTLINE_DIR).is_dir():
        for pat in (f"细纲_第{chapter_id}章*.md", f"细纲_第{chapter_id:02d}章*.md", f"细纲_第{chapter_id:03d}章*.md"):
            cands = [f for f in sorted((book / OUTLINE_DIR).glob(pat)) if not f.name.startswith(".")]
            if cands:
                outline_path = cands[0]
                break
    if outline_path is None:
        sys.stderr.write(f"[ERROR] 找不到第 {chapter_id} 章细纲\n")
        sys.exit(1)

    raw = norm(outline_path.read_text(encoding="utf-8"))
    text = re.sub(r"<!--.*?-->", "", raw, flags=re.S)  # 剥 source_hash 注释

    # —— C-VOICE-PARSE@1 章节切片（多章合一细纲只解析目标章块）——
    # found=按 `#{1,3} 第N章` 切出目标块；single=单章形态（原文，零回归）；
    # missing=文件含章块但无目标章 → 明确报错，禁止静默回落全文/别章。
    _region, _chapter_status = _slice_chapter_text(text, chapter_id)
    if _chapter_status == "missing":
        sys.stderr.write(f"[ERROR] 细纲文件存在但无第{chapter_id}章块: {outline_path}\n")
        sys.exit(3)
    parse_text = _region if _chapter_status == "found" else text

    state = load_json(book / TRACKING_DIR / "_tracking-state.json")

    # Task 3：state.global 空且章>10 → 禁止空则省略（stderr + exit 2）
    try:
        _nww_content_gates().enforce_state_global_for_packet(state, chapter_id)
    except Exception as _cg_exc:  # ContentGateError
        if _cg_exc.__class__.__name__ != "ContentGateError" and "ContentGate" not in str(_cg_exc):
            raise
        sys.stderr.write(f"{_cg_exc}\n")
        sys.exit(2)

    # —— 各字段解析（found 形态一律用章块 region；single 用原文）——
    acts, act_missing = _parse_acts(parse_text)
    # —— 2026-10-12 beats 回退：仅在「幕格式」解析为空时才启用（修复 scene_chain 恒空）——
    # 只增不改：幕格式细纲（如泡面 ch14/25/39/40）走原路径，行为完全不变；
    # 只有 beats 表细纲（钦天监/诡异时代等）才走回退，且 meta.beats_source 显式标记来源。
    beats_source = "acts" if acts else "empty"
    if not acts and _beats_fallback_enabled():
        _beats, _beats_missing = _parse_beats(parse_text)
        if _beats:
            acts = _beats
            beats_source = "beats_fallback"
            # 不静默：把回退事实写进 missing_flags，保持「missing 确定性派生 parse_status」的契约
            act_missing = act_missing + [f"BEATS_FALLBACK_APPLIED:{m}" for m in _beats_missing]
        else:
            beats_source = (f"beats_fallback_failed:{','.join(_beats_missing)}"
                            if _beats_missing else "empty")
    chars, char_missing = _parse_characters(parse_text)
    # C-VOICE-PARSE@1 第 5 条：voice 锚点三键（只增字段，供 C-GATE 区分「格式读不到/内容真缺失」）
    if _RE_VOICE_HEADING.search(parse_text):
        _voice_form = "heading"
    elif _RE_VOICE_FIELD.search(parse_text):
        _voice_form = "field"
    else:
        _voice_form = "none"
    _voice_char_count = len(chars)
    # P0-1：FAIL 块兜底合并（无 # 前缀写前对齐补充的角色锚点），不覆盖已解析条目
    _repair_chars = _parse_characters_repair(parse_text)
    if _repair_chars:
        _have = {c["name"] for c in chars}
        chars = chars + [c for c in _repair_chars if c["name"] not in _have]
        if chars and "出场角色voice锚点" in char_missing:
            char_missing = [m for m in char_missing if m != "出场角色voice锚点"]
            sys.stderr.write("[P01-REPAIR] 出场角色经 FAIL 块兜底解析补齐 "
                             f"({len(_repair_chars)} 条：{[c['name'] for c in _repair_chars]})\n")
    funcs = _parse_character_function(parse_text)
    landing, frefs, _lm = _parse_landing(parse_text)
    wc, wc_raw = _parse_wordcount(parse_text)
    keep_notes, avoid_notes = _parse_exec_notes(parse_text)
    anchors = _parse_anchor(parse_text)
    hook = _sec_loose(parse_text, r"章首钩子")[:600]
    # P0-1 & Round 3 R3-02c：章首钩子兜底——细纲主文 bullet 行（兼容加粗与 E1 前缀）→ FAIL 补充块
    if not hook:
        _hm = re.search(r"^[-*]\s*\*{0,2}(?:E1\s*)?章首钩子\*{0,2}[：:]\s*(.+)$", parse_text, re.M)
        if _hm:
            hook = _hm.group(1).strip()[:600]
    if not hook:
        for _t, _b in _fail_blocks(parse_text):
            if "章首钩子" in _t and _b:
                hook = _b[:600]
                break
    core = (
        _sec_loose(parse_text, r"Chapter Core|章节核心")
        or _field_loose(parse_text, r"Chapter Core|章节核心", cap=600)
        or _sec(parse_text, r"本章收益兑现")
    )
    memory = core[:600]
    explicit_goal = (
        _field_loose(parse_text, r"Chapter Goal|章节目标", cap=300)
        or _sec_loose(parse_text, r"Chapter Goal|章节目标|本章目标")[:300]
    )
    emotion = _parse_emotion_curve(parse_text)
    # 2026-10-14（外脑 Q2）：把细纲已写好的「目标情绪 / 爽点」传进独立字段。
    # 开关关闭 → 两字段恒为 ""（字段内容回滚，键仍保留，符合「只增不改」）。
    et = _parse_emotion_target(parse_text) if _INTENT_EXTRA else ""
    sp = _parse_selling_points(parse_text) if _INTENT_EXTRA else ""
    # 2026-11-04（Step 2）：上下章义务合同解析；关闭或三段全缺 → None，不污染 packet。
    outline_contract = _parse_outline_contract(parse_text)
    release_items, invent_items = _parse_info_boundary(parse_text)

    # intent.goal 来源优先级（CCv2 裁决：explicit Chapter Goal → derived 第一幕 driver_goal.goal → 空；
    # 落点 drive 是「章节主动力/戏剧功能」，不再作为主角章节目标）
    if explicit_goal:
        goal, goal_src = explicit_goal, "explicit"
    elif acts and beats_source == "acts" and acts[0].get("driver_goal", {}).get("goal"):
        # 外脑 2026-10-12 判定 1(b)：derived 只允许来自「真实幕格式」的第一幕驱动目标。
        # beats 回退项的第一条是「开场事件/对方动作」（实测钦天监 ch12 B1 =
        # 「裴吏拦查文引」），把它当主角本章目标会让 writer 误判方向 → 不派生。
        goal, goal_src = acts[0]["driver_goal"]["goal"][:200], "derived"
    else:
        goal, goal_src = "", ""

    # —— 幕档位（E1/R8）：中文档位 → meta.scene_scale；D0-D3 旧判据 → 显式 discard_reason ——
    scales = [a.get("scale", "") for a in acts if a.get("scale")]
    cn_scales = [s for s in scales if s in _SCALE_CN]
    d_scales = [s for s in scales if re.fullmatch(r"D[0-9]", s)]
    discard = [
        "本幕容量（1C 判据）/LC 字段：prealign 已归档判据（2026-08-29），按裁决不进 v2",
        "E2/E3/E5 标签：并入 scene_chain.craft_notes 与 intent 字段",
    ]
    if d_scales:
        discard.append(f"幕档位 {d_scales}：prealign 已归档判据（2026-08-29），按裁决不进 v2（E1/R8 显式声明）")

    # —— 组装 v2 ——
    missing_flags = act_missing + char_missing + (["落点"] if not landing["ending"] else [])
    if not hook:
        missing_flags.append("章首钩子")
    parse_status = "FULL" if not missing_flags else "PARTIAL"

    meta = {
        "source": str(outline_path.relative_to(book)),
        "outline_hash": hashlib.md5(norm(raw).encode("utf-8")).hexdigest(),
        "parse_status": parse_status,
        "missing": missing_flags,
        "schema_semantics": "chapter_contract_v2",
        "discard_reasons": discard,
        # 2026-10-12 新增（只增）：scene_chain 的来源，可审计。
        # acts=幕格式解析成功｜beats_fallback=幕格式为空、回退到「情节细化」表格
        # empty=两者皆无｜beats_fallback_failed:*=回退尝试但未解析出内容
        "beats_source": beats_source,
        # 2026-11-06（C-VOICE-PARSE@1 第 5 条，只增）：voice 锚点可解析性三键。
        # detected=region 内命中权威标题或兼容字段行；form=heading/field/none；
        # char_count=从 voice 锚点解析出的角色条目数（供 C-GATE 区分「格式读不到/内容真缺失」）。
        "voice_anchor_detected": _voice_form != "none",
        "voice_anchor_form": _voice_form,
        "voice_anchor_char_count": _voice_char_count,
    }
    if cn_scales:
        meta["scene_scale"] = cn_scales
    if landing.get("drive"):
        meta["chapter_drive_type"] = landing["drive"]

    # P2-9 内容超载检查（advisory）：必备情节点数（幕 + FAIL 补充正式情节点）vs 字数目标。
    # 实测教训：2500 字目标装 3 幕 + 8 项机制/决策补充，writer 必然静默丢铺垫。
    _formal_points = len(re.findall(r"【[^】]{2,40}】", "\n".join(b for _, b in _fail_blocks(parse_text))))
    _wc_int = 0
    try:
        _m = re.search(r"\d{3,4}", str(wc.get("target", "")))
        if _m:
            _wc_int = int(_m.group(0))
    except Exception:
        pass
    _est = (len(acts) + _formal_points) * 400
    meta["content_load"] = {
        "beats": len(acts), "formal_points": _formal_points,
        "wc_target": _wc_int, "estimated_min_chars": _est,
        "overload": bool(_wc_int > 0 and _est > _wc_int * 1.1),
    }
    if meta["content_load"]["overload"]:
        sys.stderr.write(f"[P29-OVERLOAD] 内容超载：{len(acts)}幕+{_formal_points}个正式情节点 "
                         f"≈{_est}字 > 目标{_wc_int}×1.1 —— 建议拆章或上调字数\n")

    _proj = _canon_projection(state, _chapter_subjects(acts, chars))
    _canon_delta = _canon_delta_outline_explicit(release_items, invent_items)
    _canon_delta["may_not_contradict"] = [f["id"] for f in _proj["confirmed"] if f.get("id")]
    # P0-4：细纲数字锚 → must_keep（writer 硬约束 + 写后确定性比对基线）
    _num_anchors = _extract_numeric_anchors(parse_text, chapter_id)
    # P0-4b（2026-10-25）：定性规则承诺（处罚力度/规则后果）→ must_keep，防跨章力度漂移
    _rule_anchors = _extract_rule_anchors(parse_text, chapter_id)
    # W6（2026-11-06）：细纲【旁白禁词表】→ 提交期 must_avoid，创作期不进镜头内容。
    _banned_terms = _extract_banned_terms(parse_text)

    # —— Role Lifecycle Contract v1（R1：只分类 + report，不注册）——
    # 读 bible.characters 名字集合做存在性比对；无 bible 文件时退化为空集（全部 unregistered）。
    bible = load_json(book / "bible.json")
    bible_names = {ch.get("name", "") for ch in (bible.get("characters") or []) if ch.get("name")}
    contract = _merge_character_function(chars, funcs)
    contract, role_report = resolve_role_lifecycle(contract, funcs, bible_names, chapter_id, book)

    # voice_print 前置挂载（2026-11-13）：Context 组装阶段按 NWW_VOICE_PRINT 改写合同。
    # off 过滤旧方言提示 / lite 追加防滥用压制 / smart 注入四维与状态触发约束。
    # 失败即保持原合同，绝不改变 packet 结构与既有闸门。
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import nww_voice_print as _voice_print
        contract = _voice_print.build_character_contract(book, contract)
    except Exception as _vp_exc:  # noqa: BLE001 — 零阻断：指纹改写失败不改变 packet
        sys.stderr.write(f"[VOICE_PRINT] contract rewrite skipped: {_vp_exc}\n")

    v2 = {
        "schema_version": SCHEMA_VERSION,
        "chapter_id": chapter_id,
        "meta": meta,
        "intent": {
            "hook": hook,
            "goal": {"value": goal, "source": goal_src},
            "ending": landing["ending"],
            "memory_point": memory,
            "emotion_curve": emotion,
            "emotion_target": et,
            "selling_points": sp,
        },
        # 2026-11-04（Step 2 · env 回滚）：上下章义务合同；
        # NWW_OUTLINE_CONTRACT=1 且细纲含义务段才注入，关闭/无段时 v2 无此键（逐字节不变）。
        **({"outline_contract": outline_contract} if outline_contract is not None else {}),
        "scene_chain": acts,
        "character_contract": contract,
        # 2026-10-25：角色底蕴层——出场角色整卡注入（情绪模式/语言指纹/恐惧欲望/前史）。
        # 失败即空 dict；NWW_CHAR_DEPTH=0 可整体关闭。
        "character_depth": build_character_depth(book, [c.get("name", "") for c in contract]),
        "role_lifecycle_report": role_report,
        "canon_constraints": {
            "must_keep": anchors + keep_notes + _num_anchors + _rule_anchors,
            "must_not_invent": invent_items,
            "must_release": release_items,
            "must_avoid": avoid_notes + _banned_terms,
        },
        "facts": _proj,
        "canon_delta": _canon_delta,
        "foreshadow_refs": [
            {**r, "resolve": ("RESOLVED" if r["id"] in (state.get("foreshadow") or {}) else "UNRESOLVED")}
            for r in frefs
        ],
        # Task 6 / C-P1-1：pending_foreshadows = 活跃伏笔（已埋+需处理），按 importance+planted 排序
        "pending_foreshadows": iter_active_foreshadows(state, [], limit=_PENDING_FSH_LIMIT),
        # 2026-10-11：preceding 容量 300→1500（env 可调）+ 新增结构化状态卡（扩展键，不动既有语义）
        "context": {
            "preceding": _preceding_v2(book, chapter_id),
            "preceding_state": _preceding_state_card(book, chapter_id),
            "note": "",
        },
        "wordcount": wc,
        # —— §7-1 三新键（v2.3 冻结契约）：方案 C 固定 null / 角色资源 / 有效禁令 ——
        "scene_time": _scene_time_card(state, chapter_id),
        "character_resources": build_character_resources(state),
        "character_forbids": build_character_forbids(book),
    }

    # —— §W1 世界观/宏观按需召回（2026-10-12 新增扩展键；只增不改，失败即空）——
    # query = 细纲原文 + 本章 subjects；命中设定文件标题实体才召回，不做全量注入。
    try:
        _wq = "\n".join([str(parse_text or ""), " ".join(_chapter_subjects(acts, chars))])
        _wc_text, _wc_refs = _world_context_card(book, _wq)
        _sk_text, _sk_refs = _world_skeleton(book, chapter_id, _WORLD_SKELETON_CHARS)
        v2["world_context"] = {
            "text": _wc_text,
            "skeleton": _sk_text,
            "source_refs": _wc_refs + [r for r in _sk_refs if r not in _wc_refs],
        }
    except Exception:
        v2["world_context"] = {"text": "", "skeleton": "", "source_refs": []}

    # —— §W2 跨章状态卡（2026-10-12 新增扩展键；只增不改，失败即空）——
    # 数据源是 commit 后仍持久化的逐章记录/角色状态/伏笔，不依赖会被消费掉的 delta。
    try:
        v2["cross_chapter_state"] = _cross_chapter_state_card(
            book, chapter_id, _chapter_subjects(acts, chars))
    except Exception:
        v2["cross_chapter_state"] = {}

    # —— §W3 全局上下文派生（2026-10-12 新增扩展键；只增不改，失败即空）——
    # 外脑 P1-3：kind 区分 ESTABLISHED_FACT / CURRENT_STATE / PLAN / COMMITMENT /
    # UNRESOLVED；PLAN 与 COMMITMENT 绝不进入已发生事实集合；不写 state["global"]。
    try:
        v2["derived_world_context"] = (
            _dwc.derive(book, chapter_id) if _dwc is not None else {})
    except Exception:
        v2["derived_world_context"] = {}

    # —— §W4 宏观连续性上下文（2026-10-12 新增扩展键；只增不改，失败即空）——
    try:
        v2["macro_context"] = _macro_context_card(book, chapter_id)
    except Exception:
        v2["macro_context"] = {}

    # —— §W5 风格锚（2026-10-12 新增扩展键）——
    # ★ 2026-10-12 正式 A/B 结论 + 外脑 R2/R1-c 裁决：style_anchor **默认关闭**。
    #   实测（新书 ch2，n=30/组，生产 8192）：只开 style_anchor ΔT3=+0.49，
    #   95%CI [-0.20,+1.19] **含 0** 未达预注册门槛（该结论在 5 种口径下均不显著）；
    #   而只开 macro_context ΔT3=+1.88 CI[+1.18,+2.58] 显著（5 种口径全部显著）。
    #   → style_anchor **不是必需生产路径**。默认 `NWW_STYLE_ANCHOR=0`（不注入）；
    #     显式置 1 才启用（用于后续验证/复验）。
    # 2026-11-06：本书可由 bible.policy.style_anchor="on" 按书 opt-in（不推翻全局默认关闭）。
    _sa_on = _style_anchor_enabled() or _book_style_anchor_opt_in(book)
    try:
        v2["style_anchor"] = _style_anchor_card(book) if _sa_on else {}
    except Exception:
        v2["style_anchor"] = {}

    # —— §W6 创作层正向镜头卡（2026-11-06 黄金三章新案；恒常注入，失败显式留痕）——
    # 与 style_anchor 边界：style_anchor=人拍板的文风契约；sensory_card=本章可拍镜头；
    # genre_style_card=题材层的稳定正文落点。三者独立开关、独立审计。
    try:
        v2["sensory_card"] = _sensory_card(parse_text)
    except Exception as _sc_exc:
        v2["sensory_card"] = {}
        sys.stderr.write(f"[W6-SENSORY] 提取失败（不阻断）: {_sc_exc}\n")
    v2["sensory_card_status"] = (
        {"status": "ok", "slots": len(v2["sensory_card"]) - 1,
         "keys": [k for k in v2["sensory_card"] if k != "scene_anchor_rule"]}
        if any(k != "scene_anchor_rule" for k in v2["sensory_card"])
        else {"status": "no_sensory_section", "reason": "细纲无 ## 镜头模具 或槽位为空"})
    try:
        v2["genre_style_card"] = _genre_style_card(book)
    except Exception as _gs_exc:
        v2["genre_style_card"] = {}
        sys.stderr.write(f"[W6-GENRE] 提取失败（不阻断）: {_gs_exc}\n")
    v2["genre_style_card_status"] = (
        {"status": "ok", "sections": [k for k in v2["genre_style_card"] if k != "source"]}
        if v2["genre_style_card"]
        else {"status": "missing_or_no_match", "file": _GENRE_STYLE_FILE})

    # —— §W5b 风格锚状态（2026-10-12 外脑 Q10 裁决；只增不改的独立顶层键）——
    # 裁决原话：「禁止静默伪装为成功注入」。旧行为是缺文件时 `style_anchor` 返回 {}，
    # 与"注入成功但内容为空"在产物上完全无法区分 —— 这正是我自己批评过的"静默空转"。
    # 新增**独立**键显式声明状态，不改动 `style_anchor` 本身的字段语义。
    try:
        v2["style_anchor_status"] = (
            {"status": "disabled", "anchored": False,
             "reason": "全局 NWW_STYLE_ANCHOR=0 且本书 bible.policy.style_anchor 未置 on"}
            if not _sa_on
            else _style_anchor_status(book, v2.get("style_anchor")))
    except Exception:
        v2["style_anchor_status"] = {"status": "unknown", "reason": "状态判定异常"}

    # —— §W5d 叙事人称（2026-11-04 用户裁决：故事设定补「第几人称视角」维度）——
    # bible.pov 是全书人称唯一事实源；缺失默认第三人称。独立顶层键，只增不改。
    # 与 style_anchor 无关：人称是恒定基础维度，不随风格锚开关关闭。
    try:
        v2["narrative_pov"] = _narrative_pov_card(book)
    except Exception:
        v2["narrative_pov"] = {"text": _DEFAULT_POV, "source": "default"}

    # —— §W5c 作者改稿口味（2026-10-24 P2；默认关闭）——
    # P1 从「编辑器手改 diff」提炼的规律库（设定/笔记反馈/rules.json）。
    # ★ 关闭时不新增任何键 ⇒ packet 与接入前逐字节一致，现有链路零影响。
    #   显式 NWW_STYLE_FEEDBACK=1 才启用（A/B 验收前保持关闭）。
    if _style_feedback_enabled():
        try:
            v2["style_feedback"] = _style_feedback_card(book)
        except Exception:
            v2["style_feedback"] = {}
        try:
            v2["style_feedback_status"] = _style_feedback_status(book, v2.get("style_feedback"))
        except Exception:
            v2["style_feedback_status"] = {"status": "unknown", "injected": False}

        # —— §W6 千章不崩中长期护栏（2026-10-26 新增扩展键；只增不改，失败即空）——
    try:
        _as_of_ch = min(int(state.get("last_committed_chapter") or 0), max(0, chapter_id - 1))
        _vol_guard = _extract_volume_arc_band(book, chapter_id)
        _res_guard = _extract_resource_ledger_card(bible, state)
        _emo_guard = _extract_emotional_arc_card(book, bible, chapter_id, parse_text)
        v2["thousand_chapter_guard"] = {
            "as_of_chapter": _as_of_ch,
            **_vol_guard,
            "resource_ledger": _res_guard,
            "emotional_arc": _emo_guard,
        }
    except Exception:
        v2["thousand_chapter_guard"] = {}

    # —— Task 6 / C-P1-4：cost_panel 注入（{烟火值:85} → writer 可读的设备视图）——
    # 旧形 {count: n} 保留 count；cost_system.device + state.cost.current → device: current。
    try:
        _cost_panel = normalize_cost_view(state.get("cost") if isinstance(state, dict) else {})
        if not _cost_panel and isinstance(bible, dict):
            _cs = bible.get("cost_system") or {}
            _cur = _cs.get("current")
            if _cur is None and isinstance(state, dict):
                _cur = (state.get("cost") or {}).get("current") if isinstance(state.get("cost"), dict) else None
            if _cs.get("device") and _cur is not None:
                _cost_panel = normalize_cost_view({"device": _cs.get("device"), "count": _cur})
        if _cost_panel:
            v2["cost_panel"] = _cost_panel
    except Exception:
        pass

    # Task 6：主角境界缺失且章>5 → exit 2（写前阻断，与 commit 侧 nww_content_gates 同判定）
    try:
        _cg_mod2 = _nww_content_gates()
        _cult = _cg_mod2._protagonist_cultivation(
            state if isinstance(state, dict) else {},
            bible if isinstance(bible, dict) else {},
        )
        if _cult is not None and not _cult and chapter_id > 5:
            sys.stderr.write(
                "[ContentGate BLOCK] 主角境界缺失（cultivation/境界）章>5 → 禁止写章；"
                "先补 characters[主角].cultivation 再写\n"
            )
            sys.exit(2)
    except SystemExit:
        raise
    except Exception:
        pass

    # Task 3：把 state.global 锚注入 packet，写前 assert（缺则 raise→exit 2）
    _g = state.get("global") if isinstance(state, dict) else None
    if isinstance(_g, dict) and _g:
        v2["global"] = _g
        _pos = _g.get("position")
        if isinstance(_pos, dict) and _pos:
            v2["position"] = _pos
        # 摘要：供 assert_packet_content_anchors / 下游消费
        _bits = []
        if isinstance(_pos, dict):
            for _k in ("era", "stage", "core_conflict"):
                if _pos.get(_k):
                    _bits.append(f"{_k}={_pos.get(_k)}")
        v2["global_summary"] = "；".join(_bits) if _bits else "global-present"
    # Task 3 fix：scale 关键键闸（bible.scale → position.engine / world）；block → exit 2
    try:
        _cg_mod = _nww_content_gates()
        _cgv = _cg_mod.check_global_presence(
            state if isinstance(state, dict) else {},
            chapter_id,
            bible=bible if isinstance(bible, dict) else {},
        )
        if _cgv.level == "block":
            sys.stderr.write(
                "[ContentGate BLOCK] " + "; ".join(_cgv.reasons or ["content presence failed"]) + "\n"
            )
            sys.exit(2)
        if _cgv.level == "warning":
            sys.stderr.write(
                "[ContentGate WARNING] " + "; ".join(_cgv.reasons or ["content presence warning"]) + "\n"
            )
        _cg_mod.assert_packet_content_anchors(v2, chapter_id)
    except Exception as _cg_exc2:
        if _cg_exc2.__class__.__name__ != "ContentGateError" and "ContentGate" not in str(_cg_exc2):
            raise
        sys.stderr.write(f"{_cg_exc2}\n")
        sys.exit(2)

    # —— P0-1 sidecar：本章写作合同的结构化落盘（审计 + 写后数字锚比对的权威基线）——
    # NWW_PACKET_SIDECAR=0 关闭（dry_run 沙箱预览须保持正式目录零变化，由调用方设置）。
    if os.environ.get("NWW_PACKET_SIDECAR", "1") != "0":
        try:
            _sc = {
                "chapter_id": chapter_id,
                "outline_hash": meta["outline_hash"],
                "parse_status": meta["parse_status"],
                "missing": meta["missing"],
                "hook": v2["intent"]["hook"],
                "ending": v2["intent"]["ending"],
                "characters": v2["character_contract"],
                "numeric_anchors": _num_anchors,
                "rule_anchors": _rule_anchors,
                "character_depth_chars": {k: len(v) for k, v in v2.get("character_depth", {}).items()},
                "content_load": meta.get("content_load"),
                "generated_at": int(time.time()),
            }
            (book / OUTLINE_DIR / f"细纲_第{chapter_id:03d}章.contract.json").write_text(
                json.dumps(_sc, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as _sc_exc:
            sys.stderr.write(f"[P01-SIDECAR] 写入失败（不阻断）: {_sc_exc}\n")

    # —— _v1_aliases 已删除（2026-09-23 CCv2 migration complete，见 docs/migrations/CCv2_v1_deprecation.md）——
    return v2



# ============================================================================
# §W6 千章不崩中长期护栏（2026-10-26 新增：填补双标尺复评 D1/D3/D4/D5/F3）
#
# 涵盖五项确定性结构化派生（只增顶层键 thousand_chapter_guard，不改既有字段）：
#   1. narrative_arc_band：30-50章中程叙事重心带（从卷纲 ### 事件弧 提取当前章所属弧段与重心类型）
#   2. macro_stage：全书四阶段卡点（立柱期/破局期/扩张期/收束期 + 阶段硬纪律如收束期禁开无关新支线）
#   3. resource_ledger：数值/资源天花板与物价基准账本（从 bible.json resource_ledger + cost_system 派生）
#   4. map_foreshadow：套娃式世界地图递进与换图前 30-50 章铺垫窗口检查
#   5. emotional_arc：情感线推进节拍追踪（检查距上次情感线推进是否已达节拍周期，默认 2-3 章）
# ============================================================================

_ARC_BAND_TYPES = {
    "经营": ("经营", "开店", "生意", "选品", "账", "理赔", "立规", "客源"),
    "解密": ("解密", "调查", "查账", "真相", "线索", "推理", "暗查", "勘验"),
    "闯关": ("闯关", "考核", "试炼", "比试", "破局", "晋升", "问对"),
    "大战": ("大战", "决战", "围剿", "反扑", "死局", "交锋", "雷劫"),
    "关系": ("关系", "群像", "结盟", "常客", "羁绊", "收徒", "同门"),
}


def _infer_band_type(text: str) -> str:
    scores = {btype: sum(1 for kw in kws if kw in text) for btype, kws in _ARC_BAND_TYPES.items()}
    best_type, best_score = max(scores.items(), key=lambda kv: kv[1])
    return best_type if best_score > 0 else "综合推进"


def _extract_volume_arc_band(book: Path, chapter_id: int) -> dict:
    """从 大纲/卷纲_第*卷.md 提取当前卷、四阶段卡点、30-50章叙事重心带、地图层级与名场面配额。"""
    outline_dir = book / OUTLINE_DIR
    if not outline_dir.is_dir():
        return {}
    vol_files = sorted(outline_dir.glob("卷纲_第*卷.md"))
    total_vols = len(vol_files)
    target_vol = None
    target_vol_no = 0
    vol_start, vol_end = 0, 0
    next_vol_space = ""

    parsed_vols = []
    for vf in vol_files:
        m = re.search(r"卷纲_第(\d+)卷", vf.name)
        if not m:
            continue
        vno = int(m.group(1))
        t = vf.read_text(encoding="utf-8", errors="ignore")
        rm = re.search(r"章节范围[^0-9]{0,16}第?[ \t]*(\d+)[ \t]*[–\-~～—至][ \t]*(\d+)", t)
        s_ch, e_ch = (int(rm.group(1)), int(rm.group(2))) if rm else (0, 0)
        sp_m = re.search(r"^\s*[-*]\s*\**(?:空间层级|地图层级|本卷地图)\**[：:]\s*(.+)$", t, re.M)
        space = sp_m.group(1).strip() if sp_m else ""
        parsed_vols.append((vno, s_ch, e_ch, space, vf, t))

    parsed_vols.sort(key=lambda x: x[0])
    for idx, (vno, s_ch, e_ch, space, vf, t) in enumerate(parsed_vols):
        if s_ch and e_ch and s_ch <= chapter_id <= e_ch:
            target_vol = t
            target_vol_no = vno
            vol_start, vol_end = s_ch, e_ch
            if idx + 1 < len(parsed_vols):
                next_vol_space = parsed_vols[idx + 1][3]
            break

    if target_vol is None:
        return {}

    # 1. 四阶段卡点判定（立柱 / 破局 / 扩张 / 收束）
    if target_vol_no == 1:
        stage_name = "立柱期"
        stage_rule = "立柱期纪律：锁死核心高概念、金手指代价边界与物价基准，禁止过早抛出跨界战力导致后期数值崩盘。"
    elif total_vols >= 8 and target_vol_no >= total_vols - 2:
        stage_name = "收束期"
        stage_rule = "收束期硬门禁：禁止开启与终极主线无关的新支线或跨卷新长线伏笔；每章须优先回收存量中长线伏笔。"
    elif target_vol_no <= max(2, total_vols // 2 - 1):
        stage_name = "破局期"
        stage_rule = "破局期纪律：验证核心玩法在不同势力/场景下的复用与变奏，逐步铺垫中层地图线索。"
    else:
        stage_name = "扩张期"
        stage_rule = "扩张期纪律：回收前中期伏笔并实现能力维度跃迁（换维度而非纯堆数值），提前30-50章铺垫下一空间层级。"

    # 2. 30-50章叙事重心带（解析 ### 事件弧A/B/C）
    active_arc = None
    all_arcs = []
    for m in re.finditer(r"###\s*(事件弧[^\n]*)\n(.*?)(?=\n###|\n##|\Z)", target_vol, re.S):
        heading = m.group(1).strip()
        body = m.group(2).strip()
        cm = re.search(r"第?\s*(\d+)\s*[–\-~～—至]\s*(\d+)\s*章", heading + " " + body[:120])
        a_start, a_end = (int(cm.group(1)), int(cm.group(2))) if cm else (None, None)
        btype = _infer_band_type(heading + " " + body[:300])
        arc_info = {
            "heading": heading,
            "start_chapter": a_start,
            "end_chapter": a_end,
            "band_type": btype,
            "summary": re.sub(r"\s+", " ", body)[:160],
        }
        all_arcs.append(arc_info)
        if a_start and a_end and a_start <= chapter_id <= a_end:
            active_arc = arc_info
    if active_arc is None and all_arcs and vol_start and vol_end:
        # 按卷内位置等分回退匹配事件弧
        rel_pos = (chapter_id - vol_start) / max(1, (vol_end - vol_start + 1))
        idx = min(len(all_arcs) - 1, int(rel_pos * len(all_arcs)))
        active_arc = all_arcs[idx]

    # 3. 地图铺垫窗口检查（换图前 30-50 章预警）
    sp_m = re.search(r"^\s*[-*]\s*\**(?:空间层级|地图层级|本卷地图)\**[：:]\s*(.+)$", target_vol, re.M)
    cur_space = sp_m.group(1).strip() if sp_m else ""
    chapters_to_vol_end = (vol_end - chapter_id) if vol_end else None
    map_foreshadow = {
        "current_space": cur_space,
        "next_volume_space": next_vol_space,
        "chapters_to_volume_end": chapters_to_vol_end,
        "needs_pre_foreshadow": bool(
            next_vol_space and next_vol_space != cur_space
            and chapters_to_vol_end is not None and chapters_to_vol_end <= 40
        ),
    }

    # 4. 名场面配额提取
    fs_m = re.search(r"^\s*[-*]\s*\**(?:名场面|本卷名场面|高光场面)\**[：:]\s*(.+)$", target_vol, re.M)
    famous_scene = fs_m.group(1).strip() if fs_m else ""

    return {
        "volume_no": target_vol_no,
        "volume_range": [vol_start, vol_end] if vol_start and vol_end else [],
        "is_volume_end": bool(vol_end and chapter_id == vol_end),
        "macro_stage": {"stage": stage_name, "rule": stage_rule},
        "narrative_arc_band": active_arc or {},
        "map_foreshadow": map_foreshadow,
        "famous_scene_quota": {"target_per_volume": 2, "declared": famous_scene},
    }


def _extract_resource_ledger_card(bible: dict, state: dict) -> dict:
    """数值与资源天花板 + 物价基准账本（D1 能力补齐）。"""
    rl = bible.get("resource_ledger") if isinstance(bible, dict) else None
    cost_sys = bible.get("cost_system") if isinstance(bible, dict) else None
    state_cost = state.get("cost") if isinstance(state, dict) else None
    out: dict = {
        "price_anchors": [],
        "power_ceiling": None,
        "resource_ceilings": {},
        "cost_meter": {},
    }
    if isinstance(rl, dict):
        out["price_anchors"] = list(rl.get("price_anchors") or [])[:8]
        out["power_ceiling"] = rl.get("power_ceiling")
        out["resource_ceilings"] = dict(rl.get("resource_ceilings") or {})
    if isinstance(cost_sys, dict) and cost_sys.get("device"):
        out["cost_meter"] = {
            "device": cost_sys.get("device"),
            "rule": cost_sys.get("rule", "只进不退"),
            "current": (state_cost.get("current") if isinstance(state_cost, dict) else cost_sys.get("current")),
            "max_ceiling": cost_sys.get("max_ceiling"),
        }
    return out


def _extract_emotional_arc_card(book: Path, bible: dict, chapter_id: int, outline_text: str) -> dict:
    """情感线节拍追踪器（F3 能力补齐：约每 2-3 章一次情感/群像互动推进）。"""
    ea = (bible.get("emotional_arc") if isinstance(bible, dict) else None) or {}
    cadence = int(ea.get("cadence_chapters") or 3)
    has_outline_beat = bool(re.search(r"情感线|关系线|群像互动|常客互动|羁绊", outline_text))
    last_beat_ch = None
    for prev_ch in range(chapter_id - 1, max(0, chapter_id - 8), -1):
        for name in (f"细纲_第{prev_ch}章.md", f"细纲_第{prev_ch:03d}章.md"):
            p = book / OUTLINE_DIR / name
            if p.is_file():
                try:
                    ptxt = p.read_text(encoding="utf-8", errors="ignore")
                    if re.search(r"情感线|关系线|群像互动|常客互动", ptxt):
                        last_beat_ch = prev_ch
                        break
                except Exception:
                    pass
        if last_beat_ch is not None:
            break
    gap = (chapter_id - last_beat_ch) if last_beat_ch is not None else chapter_id
    return {
        "cadence_chapters": cadence,
        "current_outline_has_beat": has_outline_beat,
        "last_beat_chapter": last_beat_ch,
        "chapters_since_last_beat": gap,
        "due_for_beat": bool(gap >= cadence and not has_outline_beat),
    }


# —— R2 人工确认层（2026-09-23 外脑裁决）——
# R1 发现 → R2 人工裁决 → R3 注册。R2 只产出 candidates + 接收人工确认结果：
#   detected_*（R1 自动判断）与 confirmed_*（人工裁决）双保留，不覆盖，可审计。
#   暂不写 tracking（state=human_confirmed → WAITING_FOR_R3）。


def _evidence_from_packet(pkt: dict, name: str) -> dict:
    """从 packet 取该角色 evidence（voice 锚点/功能，展示用，不重新解析细纲）。"""
    voice, func = "", ""
    for c in pkt.get("character_contract", []):
        if c.get("name") == name:
            voice = " ".join(c.get("voice_constraints", []) + c.get("behavior_constraints", []))[:160]
            func = c.get("function", "")[:120]
            break
    return {"voice_anchor": voice, "function": func}


def build_candidates(book: Path, start: int, end: int) -> list[dict]:
    """R2.1：聚合多章 role_lifecycle_report → characters_candidates（人工确认输入）。

    复用 build_packet_v2（不重写细纲解析）；按角色名跨章聚合：
      chapters：出现章节（去重排序）；detected_* 取最严重档（recurring > temporary）；
      bible_match：任一章节命中即 True；recurring_reasons：触发原因合并（人工复核依据）。
    排序：recurring（需人工动作）优先。
    """
    merged: dict[str, dict] = {}
    # scan 幂等合并：保留旧 candidates 中已确认/已注册条目的 confirmed_* 与 state
    # （防止重跑 scan 覆盖 R2 人工确认结果，守 detected_*/confirmed_* 双保留可审计）
    old_confirmed: dict[str, dict] = {}
    old_path = book / TRACKING_DIR / "role_lifecycle_candidates.json"
    if old_path.exists():
        try:
            for oc in json.loads(old_path.read_text(encoding="utf-8")):
                if oc.get("state") in ("human_confirmed", "registered"):
                    old_confirmed[oc["name"]] = oc
        except json.JSONDecodeError:
            old_confirmed = {}
    for ch in range(start, end + 1):
        try:
            pkt = build_packet_v2(ch, str(book))
        except SystemExit:
            continue  # 该章无细纲/构建失败，跳过
        for ent in pkt.get("role_lifecycle_report", []):
            name = ent["name"]
            # R2 候选筛选：跳过非角色实体（voice 锚点里的元信息隔离约束行，如「通用（元信息隔离）」）
            if name in ("通用", "工程标注") or "元信息" in name:
                continue
            if name in merged:
                m = merged[name]
                m["chapters"] = sorted(set(m["chapters"] + [ch]))
                if ent["classification"] == "recurring" and m["detected_classification"] != "recurring":
                    m["detected_entity_type"] = ent["entity_type"]
                    m["detected_classification"] = ent["classification"]
                    m["detected_card_required"] = ent["card_required"]
                m["bible_match"] = m["bible_match"] or ent["bible_match"]
                m["first_seen_in_scan"] = min(m["first_seen_in_scan"], ent["first_appear"])
                for r in ent.get("reasons", []):
                    if r not in m["recurring_reasons"]:
                        m["recurring_reasons"].append(r)
            else:
                ev = _evidence_from_packet(pkt, name)
                merged[name] = {
                    "name": name,
                    "chapters": [ch],
                    "detected_entity_type": ent["entity_type"],
                    "detected_classification": ent["classification"],
                    "detected_card_required": ent["card_required"],
                    "bible_match": ent["bible_match"],
                    "first_seen_in_scan": ent["first_appear"],
                    "recurring_reasons": list(ent.get("reasons", [])),
                    "evidence": {
                        "voice_anchor": ev.get("voice_anchor", ""),
                        "function": ev.get("function", ""),
                        "future_reference": any("下一章引用" in r for r in ent.get("reasons", [])),
                    },
                    "recommended_action": "REVIEW",
                    "confirmed_entity_type": None,
                    "confirmed_classification": None,
                    "confirmed_card_required": None,
                    "confirmed_by": None,
                    "confirmed_at": None,
                    "state": "unconfirmed",
                }
    # 合并保留已确认字段（不覆盖新聚合的 detected/chapters，只回填人工裁决）
    for c in merged.values():
        oc = old_confirmed.get(c["name"])
        if oc:
            c["confirmed_entity_type"] = oc.get("confirmed_entity_type")
            c["confirmed_classification"] = oc.get("confirmed_classification")
            c["confirmed_card_required"] = oc.get("confirmed_card_required")
            c["confirmed_by"] = oc.get("confirmed_by")
            c["confirmed_at"] = oc.get("confirmed_at")
            c["state"] = oc.get("state")
    order = {"recurring": 0, "temporary": 1}
    return sorted(merged.values(), key=lambda c: (order.get(c["detected_classification"], 9), c["name"]))


def confirm_candidate(book: Path, name: str, entity_type: str | None = None,
                      classification: str | None = None, card: str | None = None) -> dict:
    """R2.3：人工确认 → 填 confirmed_*（不覆盖 detected_*）+ state=human_confirmed。

    裁决来源：作者人工；不自动写 bible/tracking（WAITING_FOR_R3）。
    """
    import datetime
    path = book / TRACKING_DIR / "role_lifecycle_candidates.json"
    if not path.exists():
        sys.stderr.write(f"[ERROR] candidates 文件不存在: {path}\n")
        sys.exit(1)
    cands = json.loads(path.read_text(encoding="utf-8"))
    for c in cands:
        if c["name"] == name:
            if entity_type:
                c["confirmed_entity_type"] = entity_type
            if classification:
                c["confirmed_classification"] = classification
            if card:
                c["confirmed_card_required"] = card
            c["confirmed_by"] = "author"
            c["confirmed_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            c["state"] = "human_confirmed"
            path.write_text(json.dumps(cands, ensure_ascii=False, indent=2), encoding="utf-8")
            return c
    sys.stderr.write(f"[ERROR] 未找到 candidate: {name}\n")
    sys.exit(1)


def register_confirmed(book: Path) -> dict:
    """R3：把已 human_confirmed 的角色生命周期结果接入 tracking.characters（最小注册）。

    规则（外脑裁决）：
      - 前置：state == human_confirmed（R2 人工确认是唯一注册授权，detected_* 永不触发注册）
      - 使用 confirmed_*（不重新跑分类规则，避免三套判断漂移）
      - 只注册 card_required in {partial, full}；scene（仅场景简档）与 aggregate 不注册（留 candidates 审计）
      - 幂等：已注册（candidates.state == registered 或 tracking 已存在该角色）→ SKIP，不覆盖既有资产
      - 失败不半注册：单角色写入异常 → 记 failed，不影响其他角色
    """
    cand_path = book / TRACKING_DIR / "role_lifecycle_candidates.json"
    if not cand_path.exists():
        sys.stderr.write(f"[ERROR] candidates 文件不存在: {cand_path}\n")
        sys.exit(1)
    state_path = book / TRACKING_DIR / "_tracking-state.json"
    if not state_path.exists():
        sys.stderr.write(f"[ERROR] tracking 文件不存在: {state_path}\n")
        sys.exit(1)
    cands = json.loads(cand_path.read_text(encoding="utf-8"))
    st = json.loads(state_path.read_text(encoding="utf-8"))
    chars = st.setdefault("characters", {})
    result = {"registered": [], "skipped": [], "scene_not_registered": [], "failed": []}

    for c in cands:
        name = c["name"]
        if c.get("state") == "registered":
            result["skipped"].append(name)  # 幂等：已注册，不重复创建
            continue
        if c.get("state") != "human_confirmed":
            continue
        card = c.get("confirmed_card_required") or c.get("detected_card_required")
        if card not in ("partial", "full"):
            result["scene_not_registered"].append(name)  # scene 简档/aggregate 不注册
            continue
        if name in chars or c.get("state") == "registered":
            result["skipped"].append(name)  # 幂等：不覆盖既有 tracking
            continue
        try:
            chars[name] = {
                "identity": f"角色生命周期注册（R2 人工确认：{c.get('confirmed_entity_type') or c.get('detected_entity_type')}"
                            f"/{c.get('confirmed_classification') or c.get('detected_classification')}"
                            f"/{card} 卡）",
                "state": c.get("confirmed_classification") or c.get("detected_classification"),
                "goal": "",
                "_lifecycle": {
                    "registered_from": "role_lifecycle_candidates",
                    "confirmed_by": c.get("confirmed_by", "author"),
                    "confirmed_at": c.get("confirmed_at", ""),
                    "entity_type": c.get("confirmed_entity_type") or c.get("detected_entity_type"),
                    "classification": c.get("confirmed_classification") or c.get("detected_classification"),
                    "card_required": card,
                },
            }
            c["state"] = "registered"
            result["registered"].append(name)
        except Exception as e:  # 失败不半注册
            result["failed"].append({"name": name, "error": str(e)})

    state_path.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    cand_path.write_text(json.dumps(cands, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage:")
        print("  python3 nww_packet_builder_v2.py <book_root> <chapter_id>")
        print("  python3 nww_packet_builder_v2.py <book_root> scan [end_chapter]   # R2.1 生成 candidates")
        print("  python3 nww_packet_builder_v2.py <book_root> confirm <name> [--entity-type X] [--classification Y] [--card Z]")
        print("  python3 nww_packet_builder_v2.py <book_root> register            # R3: human_confirmed → tracking.characters")
        sys.exit(1)
    book_root = sys.argv[1]
    mode = sys.argv[2]
    # --no-sidecar（2026-10-25 并发安全）：dry_run 沙箱预览须保持正式目录零变化。
    # 用 CLI 参数而非环境变量（NWW_PACKET_SIDECAR）：os.environ 是进程级全局，
    # 两书并发（一书 dry_run 一书正式）会互相覆盖——pipeline 自身注释明令禁止此模式。
    # 环境变量仍向后兼容（旧调用方），但新调用方一律走 CLI 参数。
    _argv_rest = sys.argv[3:]
    if "--no-sidecar" in _argv_rest:
        os.environ["NWW_PACKET_SIDECAR"] = "0"
        _argv_rest = [a for a in _argv_rest if a != "--no-sidecar"]
    if mode == "scan":
        book = Path(book_root)
        end = int(_argv_rest[0]) if _argv_rest else 200
        cands = build_candidates(book, 1, end)
        out = book / TRACKING_DIR / "role_lifecycle_candidates.json"
        out.write_text(json.dumps(cands, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"candidates={len(cands)} -> {out}")
        sys.exit(0)
    if mode == "register":
        book = Path(book_root)
        res = register_confirmed(book)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        sys.exit(0)
    if mode == "confirm":
        book = Path(book_root)
        name = _argv_rest[0] if _argv_rest else ""
        et = cl = cd = None
        i = 1
        while i < len(_argv_rest):
            if sys.argv[i] == "--entity-type" and i + 1 < len(sys.argv):
                et = sys.argv[i + 1]; i += 2
            elif sys.argv[i] == "--classification" and i + 1 < len(sys.argv):
                cl = sys.argv[i + 1]; i += 2
            elif sys.argv[i] == "--card" and i + 1 < len(sys.argv):
                cd = sys.argv[i + 1]; i += 2
            else:
                i += 1
        c = confirm_candidate(book, name, et, cl, cd)
        print(json.dumps(c, ensure_ascii=False, indent=2))
        sys.exit(0)
    print(json.dumps(build_packet_v2(int(mode), book_root), ensure_ascii=False, indent=2))
