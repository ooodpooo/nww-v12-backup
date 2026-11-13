"""era_inference.py — 语域档位感知-确认型引擎（2026-10-25 用户裁决）。

设计哲学（用户原话的工程化）：
  「正确方式应该是感知型和确认型……系统自动切换锁定本书风格，
    而不是让读者自己去选去操作……遇到跨空间题材，系统也可以主动和作者确认」

两个入口：
  ① 感知型（自动）：写前对齐阶段 ensure_book_register() 读建书素材（书名/简介/
     题材/主角/金手指），高置信 → 自动写 bible.era_constraints 并锁定，作者零操作；
  ② 确认型（提问）：低置信（跨空间/两界、现代元素无穿越者通道、风格倾向不明）
     → 按修真正典先行（不堵写作），把问题透出到聊天素材块，作者一句话即可改档。

档位（与 nww_era_check.REGISTER_MODES / 审稿 I-E prompt / writer 铁律三处同口径）：
  strict   修真正典：修仙界核心表述，现代词只经主角内心/对白（禁 gloss），土著禁
  balanced 修主现辅：修真语域为主，现代词点缀（低频、有效果目的）
  modern   现代语感：用现代语感描绘修仙界；审风格一致性与世界自洽

作者侧永远不需要知道这些名字：聊天里说「修仙界不能出现现代词」即可，agent 识别
意图后走 set_register 动作落 bible（见 agent.py 语域风格规则）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

__all__ = ["infer_era_constraints", "ensure_book_register", "register_block"]

# —— 信号词表（全部确定性子串匹配，零 LLM、可解释、可回放）——
_CULTIVATION_RE = re.compile(
    r"修仙|修真|宗门|灵石|筑基|炼气|金丹|元婴|修士|仙门|道宗|灵气|符箓|飞剑|法器|渡劫|心魔")
_CROSS_SPACE_RE = re.compile(
    r"两界|往返.{0,6}(现代|地球)|回到现代|现代世界|地球.{0,4}(往返|穿越)|穿越门|跨界")
_TRANSMIGRATOR_RE = re.compile(
    r"穿越|穿书|重生|前世|社畜|现代人|地球人|打工人|996|灵魂.{0,4}(现代|地球)")
# 现代语感信号词（书名/简介里出现→作者在前提层已用现代口吻，需确认叙述档位；
# 不自动锁 modern：语域是品味决策，本书实证——书名带「大佬」等现代味词仍选 strict）
_MODERN_FLAVOR_TERMS = (
    "打工人", "社畜", "KPI", "996", "内卷", "裁员", "大厂", "体制内", "职场",
    "搬砖", "躺平", "摆烂", "摸鱼", "乙方", "甲方", "加班", "绩效", "老板",
    "实习生", "简历", "面试", "办公", "工资", "双休", "调休", "工资条",
)
# 现代商品候选（用于主角随身白名单抽取；比 MODERN_ITEMS 宽，含化妆品/食品类）
_GOODS_CANDIDATES = (
    "遮瑕膏", "粉底膏", "粉底", "口红", "防晒霜", "可乐", "肥宅快乐水", "辣条",
    "卫龙", "大面筋", "泡面", "方便面", "火锅料", "自热米饭", "巧克力", "口香糖",
    "行李箱", "采购表", "供应链", "可乐糖浆", "咖啡", "奶茶", "薯片", "啤酒",
)

_MODE_LABEL = {"strict": "修真正典（修仙界核心表述，现代词只经主角内心/对白）",
               "balanced": "修主现辅（修真语域为主，现代词点缀）",
               "modern": "现代语感（用现代口吻描绘修仙界）"}

_POLICY_TMPL = {
    "strict": {
        "narration_register": "以修仙界世界为核心表述：旁白/公共描述禁用一切现代词汇（概念词、俚语、现代器物词），度量衡、比喻、称呼一律用修真界意象",
        "protagonist_channels": "现代词汇仅允许出现在主角（穿越者）的内心独白与对白中；主角提出现代词时禁止做修仙界式标注或解释（无 gloss，自然带过）",
        "native_channels": "修仙界土著（对白与内心）绝对禁止使用现代概念词/俚语；土著可指名购买铺内商品（商品名=店内交易名），但不得使用现代文化概念词",
        "premise_goods": "设定内商品为铺内实物：旁白（主角视角）可指名；主角对白可指名与命名；其他角色须已亲历主角传播（本章或前章在场听到/看到价牌）方可使用其名，未传播的新客/旁观者必须用替代词，禁止凭空说出商品名或诙谐现代词",
        "golden_finger_text": "金手指系统文本（面板读数/规章条文/采购表）= 主角侧设定内文本：自身用语可为现代词；土著可照读字面，不得引申使用现代概念词",
    },
    "balanced": {
        "narration_register": "修仙界语域为整体基调；现代词可点缀（低频、有对照/喜剧/张力效果处才用，密度红线约 1.5 处/千字，禁止常态化堆砌）",
        "protagonist_channels": "主角（穿越者）内心与对白可自由使用现代词",
        "native_channels": "土著台词原则上用修真语域；个别「跟主角学舌」桥段须有剧情支撑",
        "premise_goods": "设定内商品为铺内实物：旁白（主角视角）可指名；其他角色须已亲历主角传播（本章或前章在场听到/看到价牌）方可使用其名，未传播新客必须用替代词（见 naming_propagation），禁止凭空说出商品名或诙谐现代词",
    },
    "modern": {
        "narration_register": "用现代语感描绘铺垫修仙界（宗门当大厂、修士当打工人式行文）；现代概念词可进入旁白与对白，是风格不是穿帮",
        "style_consistency": "现代语感贯穿全书，禁止忽而现代忽而仿古的语感漂移",
        "world_coherence": "物理世界仍是修仙界（灵石/符箓/飞剑/宗门结构）；无来源的真实科技产物仍须有主角/金手指来源",
        "premise_goods": "设定内商品与金手指文本照旧合法",
    },
}


def _corpus(bible: dict) -> tuple[str, str, str]:
    """(全文语料, 主角相关语料, 书名+简介语料)——分层取信号。"""
    def s(*keys: str) -> str:
        out = []
        for k in keys:
            v = bible.get(k)
            if isinstance(v, str):
                out.append(v)
        return " ".join(out)

    protag = bible.get("protagonist")
    protag_text = ""
    if isinstance(protag, dict):
        protag_text = " ".join(str(v) for v in protag.values() if isinstance(v, str))
    mech = bible.get("mechanics") or []
    mech_text = " ".join((m.get("desc", "") if isinstance(m, dict) else str(m)) for m in mech)
    full = " ".join([s("book_title", "title", "genre", "synopsis", "world_summary",
                       "conflict", "differentiation", "scale"), protag_text, mech_text])
    head = " ".join([s("book_title", "title", "genre", "synopsis", "world_summary")])
    return full, protag_text, head


def infer_era_constraints(bible: dict) -> dict:
    """从建书素材推断语域档位。纯确定性，返回：
    {applicable, mode, confidence("high"|"low"), signals[], question|None,
     era_constraints{...}|None}
    """
    full, protag, head = _corpus(bible or {})
    signals: list[str] = []
    if not _CULTIVATION_RE.search(full):
        # 非修仙题材（现代都市/西幻等）：语域引擎不适用，零注入零检测
        return {"applicable": False, "mode": None, "confidence": "high",
                "signals": [], "question": None, "era_constraints": None}
    signals.append("修仙界题材")

    goods = [g for g in _GOODS_CANDIDATES if g in full]
    cross = bool(_CROSS_SPACE_RE.search(full))
    transmigrator = bool(_TRANSMIGRATOR_RE.search(protag) or _TRANSMIGRATOR_RE.search(head))
    flavor = sorted({t for t in _MODERN_FLAVOR_TERMS if t in head})

    if cross:
        signals.append("跨空间/两界信号")
    if goods:
        signals.append(f"现代元素（{'、'.join(goods[:5])}{'…' if len(goods) > 5 else ''}）")
    if transmigrator:
        signals.append("穿越者主角（现代词合法通道）")
    if len(flavor) >= 3:
        signals.append(f"书名/简介现代语感味（{'、'.join(flavor[:5])}）")

    question = None
    mode = "strict"
    confidence = "high"
    if cross:
        # 跨空间题材：现代世界真实在场，叙述跟谁走是作者的世界观决策，系统不越权
        confidence = "low"
        question = ("本书有跨空间/两界信号（现代世界真实在场）。叙述语域请拍板："
                    "A 修真正典——以修仙界表述为准，现代词只经主角内心与对白；"
                    "B 修主现辅——修真语域为主，现代词偶尔点缀；"
                    "C 现代语感——现代口吻描绘修仙界。回复 A/B/C 或一句话描述即可锁定。")
    elif len(flavor) >= 3:
        # 书名/简介本身是现代口吻写的：作者在前提层已做风格表达，但语域是品味决策
        # （本书实证：书名带「大佬」等现代味词，作者仍选修真正典）→ 必须问，不猜
        confidence = "low"
        question = ("书名/简介带明显现代语感词（" + "、".join(flavor[:5]) + "）。叙述语域请拍板："
                    "A 修真正典——修仙界表述为准，现代词只经主角内心与对白；"
                    "B 修主现辅——修真为主，现代词点缀；"
                    "C 现代语感——用现代口吻描绘修仙界（宗门当大厂、修士当打工人式行文）。"
                    "回复 A/B/C 或一句话描述即可锁定。")
    elif goods and not transmigrator:
        # 有现代元素但没有明确的穿越者通道：谁有资格说现代词？必须问
        confidence = "low"
        question = ("书里有现代元素但未见穿越者主角。现代词允许出现在哪里？"
                    "A 只经主角（或指定角色）内心与对白，修仙界叙述保持修真语域；"
                    "B 修真为主、现代词点缀；C 现代语感整体描绘。回复 A/B/C 或一句话描述。")
    elif not goods and not transmigrator:
        signals.append("无现代元素（档位基本不触发，锁定 strict 兜底）")

    ec = None
    if confidence == "high" or mode:
        protag_name = ""
        p = bible.get("protagonist")
        if isinstance(p, dict) and isinstance(p.get("name"), str) and p["name"].strip():
            protag_name = p["name"].strip()
        ec = {
            "register_mode": mode,
            "protagonist": protag_name or "主角",
            "protagonist_brought": goods[:12],
            "policy": dict(_POLICY_TMPL[mode]),
        }
        # 命名传播链脚手架（strict/balanced 档适用；modern 档整体现代语感不适用）：
        # 规则文本随档锁定带入；替代词表留空——由细纲生成层按 architect 规约逐品规划
        # （命名节拍/习得者/替代词三层），作者也可在聊天里补充。
        if mode != "modern":
            ec["naming_propagation"] = {
                "rule": "现代物品名与诙谐现代词的首次命名权归主角：主角亲口说出/标价牌/系统面板展示且对方看见，才算传入修仙界。亲历传播的角色此后可自然使用（可带学舌痕迹）；未经历传播的角色（新客/旁观者）绝不能凭空说出该词，须用替代词。传播前、传播中都应有替代词过渡。",
                "substitutes": {},
            }
    return {"applicable": True, "mode": mode, "confidence": confidence,
            "signals": signals, "question": question, "era_constraints": ec}


def ensure_book_register(book: Path) -> dict:
    """感知型入口：bible 缺 era_constraints 时自动推断并落盘。

    返回 {status: "kept"|"locked"|"pending", mode, note}
      kept    已有 era_constraints（作者或上次推断已锁定），不动
      locked  本次自动锁定（高置信）
      pending 低置信：按 strict 先行落盘（写作不被堵），标记 confirm_pending=true，
              question 透出给聊天素材块，作者一句话可改
    """
    bible_path = book / "bible.json"
    try:
        bible = json.loads(bible_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — bible 缺失/损坏：语域引擎整体退场
        return {"status": "kept", "mode": None, "note": ""}

    era = bible.get("era_constraints")
    if isinstance(era, dict) and era.get("register_mode"):
        if era.get("confirm_pending"):
            return {"status": "pending", "mode": era.get("register_mode"),
                    "note": str(era.get("confirm_question") or "语域档位待作者确认")}
        return {"status": "kept", "mode": era.get("register_mode"), "note": ""}

    r = infer_era_constraints(bible)
    if not r["applicable"]:
        return {"status": "kept", "mode": None, "note": ""}
    ec = r["era_constraints"] or {}
    ec["inferred"] = True
    ec["inferred_signals"] = r["signals"]
    if r["confidence"] == "low":
        ec["confirm_pending"] = True
        ec["confirm_question"] = r["question"]
    bible["era_constraints"] = ec
    try:
        bible_path.write_text(json.dumps(bible, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001 — 落盘失败按未锁定处理，不挂主链
        return {"status": "kept", "mode": None, "note": ""}

    label = _MODE_LABEL[r["mode"]]
    if r["confidence"] == "low":
        return {"status": "pending", "mode": r["mode"],
                "note": f"语域档位待确认（已按{label}先行）：{r['question']}"}
    basis = "、".join(r["signals"])
    return {"status": "locked", "mode": r["mode"],
            "note": f"语域档位已自动锁定：{label}（依据：{basis}）。"
                    "作者随时可在对话里一句话改档（如「改用现代语感」）"}


def register_block(book: Path) -> str:
    """聊天素材块：把语域档位状态用作者语言透出（确认型的提问面）。"""
    try:
        bible = json.loads((book / "bible.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return ""
    era = bible.get("era_constraints")
    if not (isinstance(era, dict) and era.get("register_mode")):
        return ""
    mode = str(era.get("register_mode"))
    label = _MODE_LABEL.get(mode, mode)
    lines = ["【语域档位】（后端实读 bible，直接采信）",
             f"当前档位：{label}"]
    if era.get("confirm_pending"):
        lines.append(f"待作者确认：{era.get('confirm_question') or ''}")
        lines.append("（确认前按上述档位先行；作者回复后在对话中派 set_register 动作锁定）")
    lines.append("作者表达语域意图（如「修仙界不能出现现代词」「偶尔来点现代词」「用现代语感写」）"
                 "→ 识别后派 EXECUTE_JSON 动作 set_register（register ∈ strict/balanced/modern）。")
    return "\n".join(lines)
