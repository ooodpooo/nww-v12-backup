#!/usr/bin/env python3
"""
nww_era_check.py — 时代器物词检测（世界观一致性护栏）

检测正文中出现的现代工业制品，判断是否有合理来源（主角随身/有来源解释）。
不自动删除，只告警——因为可能是剧情需要（如主角拿出打火机）。

用法：
  python3 nww_era_check.py --book <book_dir> --chapter <N>
  python3 nww_era_check.py --book <book_dir> --all
  python3 nww_era_check.py --file <prose_file> --bible <bible.json>

设计原则：
  1. 词库只收工业革命后才有的物品，不收古代已有物品（灶台/油灯/火折子/木床等）
  2. 主角随身白名单（从bible.json era_constraints读取）不告警
  3. 上下文有来源解释（"她带来的""从行李箱里翻出"）不告警
  4. 对话中出现可能是主角说漏嘴，降为MEDIUM
  5. 内心独白中出现是穿越者合理回忆，不告警

声道制语言宪法（2026-10-25 用户裁决，bible.era_constraints 为权威）：
  语域三档（era_constraints.register_mode，缺省 strict）：
  - strict   修真正典：以修仙界为核心表述，公共描述零现代词
  - balanced 修主现辅：修仙界语域为主基调，现代词可适当点缀
  - modern   现代语感：用现代语感描绘铺垫修仙界，现代词是风格不是穿帮
  strict 档声道规则：
  - 旁白/公共描述：禁用一切现代词汇（器物词 + 概念词/俚语）→ 命中即 HIGH
  - 主角（穿越者）内心独白：现代词合法 → 不告警
  - 主角对白：现代词合法（确定性层无法归属说话人，见下）
  - 修仙界土著（对白+内心）：现代概念词绝对禁止
  确定性层无法判定说话人（对白归属需语义分析），因此概念词按声道分流：
  - 概念词出现在对话内 → MEDIUM 告警交 LLM 审稿（I-E 维度）终判说话人
    （主角说的=合法，土著说的=违规）
  - 概念词出现在旁白 → HIGH（无归属歧义，直接违规）
  - 概念词出现在内心独白标记后 → 不告警（单视角书内心=主角内心）
  balanced/modern 档按 REGISTER_SEVERITY 矩阵降级（modern 档概念词不检测）。
"""

# 语域模式合法值（bible.era_constraints.register_mode；缺省 strict）
REGISTER_MODES = ("strict", "balanced", "modern")


def resolve_register_mode(bible: dict) -> str:
    """读 bible.era_constraints.register_mode；非法/缺失 → strict（最安全档）。"""
    era = bible.get("era_constraints", {}) or {}
    mode = str(era.get("register_mode") or "strict").strip().lower() if isinstance(era, dict) else "strict"
    return mode if mode in REGISTER_MODES else "strict"


# 严重度矩阵：{(kind, channel): {mode: severity}}
# modern 档概念词不进矩阵（全面合法，检测跳过）；无来源器物仍观察（世界自洽）。
# balanced 档旁白概念词=观察项（点缀允许，密度/效果由审稿判），器物仍 MEDIUM。
REGISTER_SEVERITY = {
    ("modern_item", "narration"): {"strict": "HIGH", "balanced": "MEDIUM", "modern": "LOW"},
    ("modern_item", "dialogue"): {"strict": "MEDIUM", "balanced": "LOW", "modern": "LOW"},
    ("concept_word", "narration"): {"strict": "HIGH", "balanced": "MEDIUM", "modern": None},
    ("concept_word", "dialogue"): {"strict": "MEDIUM", "balanced": "MEDIUM", "modern": None},
}

import argparse
import json
import os
import re
import sys
from pathlib import Path

# ──────────────────────────────────────────────────────────────
# 现代工业制品词库（工业革命后才有的，修仙界物理世界中不合理）
# 注意：灶台/油灯/火折子/木床/木板床/土炕/铁锅/瓷碗/陶碗/竹筷/蜡烛/火石等
#       古代已有，不进此词库
# ──────────────────────────────────────────────────────────────
MODERN_ITEMS = [
    # 家具家居
    "折叠床", "沙发", "钢丝床", "弹簧床", "席梦思",
    # 照明电器
    "火柴", "电灯", "灯泡", "手电筒", "台灯", "日光灯",
    # 电子设备
    "电话", "手机", "电脑", "电视", "冰箱", "空调", "相机", "收音机",
    # 交通工具
    "汽车", "自行车", "摩托车", "火车", "飞机", "轮船", "电车",
    # 建筑材料
    "玻璃", "塑料", "水泥", "钢筋", "铝合金", "不锈钢", "瓷砖", "油漆",
    # 门窗设施（P6-E Step3：补已确认时代冲突词条「卷帘门」，仅此一词，不扩张）
    "卷帘门",
    # 日用品
    "纸巾", "卫生纸", "卫生巾", "纸尿裤", "保鲜膜", "塑料袋", "易拉罐",
    "拉环", "拉杆", "轮子",
    "牙刷", "牙膏", "肥皂", "洗发水", "沐浴露", "洗面奶", "防晒霜",
    "拖鞋", "睡衣", "内衣", "内裤", "袜子", "手套", "围巾",
    "眼镜", "墨镜", "口罩", "雨伞", "雨衣", "胶鞋", "皮鞋", "皮带",
    "钱包", "背包", "手提包", "公文包", "文件夹", "笔记本",
    # 文具
    "钢笔", "铅笔", "橡皮", "尺子", "圆规", "订书机", "回形针", "胶水",
    # 工具五金
    "剪刀", "锤子", "钉子", "螺丝", "扳手", "钳子", "锯子", "螺丝刀",
    # 其他
    "手表", "钟表", "闹钟", "温度计", "血压计", "注射器", "听诊器",
    "拉链", "纽扣", "鞋带", "领带", "西装", "牛仔裤", "T恤",
    "巧克力", "口香糖", "方便面", "罐头", "味精", "鸡精",
    "打火机", "煤油灯", "汽油", "柴油", "煤炭",
]

# 来源豁免关键词（上下文30字内出现则不告警）
SOURCE_MARKERS = [
    "带来", "从包里", "从行李箱", "从箱子", "她的", "他的",
    "现代", "掏", "翻出", "拿出", "取出", "摸出", "掏出",
    "行李箱里", "箱子里", "包里", "口袋里", "怀里",
    "穿越", "魂穿", "前世", "上辈子", "原来的世界",
    "凡货", "凡物", "她卖的", "她铺子里的",
]

# 内心独白标记（在这些标记后的段落内出现现代物品不告警）
THOUGHT_MARKERS = [
    "心想", "暗想", "暗道", "心里想", "暗自", "默默",
    "她想", "他想", "心道", "念头", "思绪",
]

# ──────────────────────────────────────────────────────────────
# 现代概念词/俚语词库（2026-10-25 用户裁决：声道制检测）
# 与 MODERN_ITEMS（物理器物）不同类：这些词暴露的是现代文化/职场/科技语境，
# 物理上可以不存在但话语里会穿帮。例：主角可说"这不就是双标吗"，
# 修仙界土著绝不能说出"双标"二字；旁白公共描述也禁用。
# 收录原则：仅收现代独有的概念/俚语（古代汉语不存在该义），不收古今通用的词。
# ──────────────────────────────────────────────────────────────
MODERN_CONCEPT_WORDS = [
    # 职场/组织文化
    "KPI", "PPT", "996", "双标", "内卷", "社畜", "躺平", "摆烂", "PUA",
    "画大饼", "打工人", "上班族", "打卡", "上班", "下班", "加班", "调休",
    "工资", "薪水", "绩效", "述职", "面试", "简历", "辞职", "跳槽", "裁员",
    "老板娘", "员工", "同事", "客服", "前台", "秘书", "白领", "金领",
    # 商业营销
    "GDP", "CEO", "促销", "买一送一", "第二件半价", "满减", "性价比",
    "售后服务", "三包", "发票", "扫码", "二维码", "会员卡", "VIP", "积分兑换",
    "带货", "种草", "拔草", "剁手", "清仓", "秒杀", "预售", "快递", "物流",
    "外卖", "超市", "便利店", "商场", "专卖店", "旗舰店",
    # 网络文化
    "网友", "粉丝", "直播", "热搜", "流量", "网课", "上网", "冲浪",
    "人设", "翻车", "吐槽", "弹幕", "刷屏", "点赞", "转发",
    "自拍", "滤镜", "美颜", "照片", "视频", "电影", "电视剧", "综艺",
    "密码", "账号", "APP", "下载", "上传", "内存", "硬盘", "键盘", "鼠标",
    "充电", "电池", "WiFi", "蓝牙", "大数据", "人工智能", "算法推荐",
    # 现代概念杂项
    "心理学", "社会学", "经济学", "管理学", "二维码", "体检", "化验",
    "维生素", "抗生素", "卡路里", "胆固醇", "咖啡因", "添加剂", "防腐剂",
    "标准化", "流水线", "产业化", "供应链", "采购表",
]

# 概念词白名单（bible.era_constraints.modern_words_allowed，默认空）
# 用途：个别书有意让某些概念词全局解禁时显式声明，避免硬编码。


def load_bible(book_dir: str) -> dict:
    """加载bible.json，提取era_constraints。"""
    bible_path = os.path.join(book_dir, "bible.json")
    if not os.path.exists(bible_path):
        return {}
    try:
        with open(bible_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def get_whitelist(bible: dict) -> list:
    """从bible.json获取主角随身白名单。"""
    era = bible.get("era_constraints", {})
    if era:
        return era.get("protagonist_brought", [])
    # fallback：从mechanics和world_summary中提取
    wl = []
    for m in bible.get("mechanics", []):
        # 兼容 dict（带 desc 键）和 str（纯描述文本）两种形态
        # 2026-09-08 修复：mechanics 为 str 数组时 m.get() 会 AttributeError
        if isinstance(m, dict):
            desc = m.get("desc", "")
        elif isinstance(m, str):
            desc = m
        else:
            continue
        for item in ["辣条", "泡面", "可乐", "火锅料", "自热米饭", "行李箱"]:
            if item in desc:
                wl.append(item)
    return wl


def get_concept_allowlist(bible: dict, whitelist: list) -> list:
    """概念词解禁表 = era_constraints.modern_words_allowed（显式解禁）
    ∪ protagonist_brought（设定内商品名内嵌概念词时随商品名放行，如
    《快消品供应链采购表》里的「供应链/采购表」）。"""
    era = bible.get("era_constraints", {}) or {}
    allowed = era.get("modern_words_allowed", []) if isinstance(era, dict) else []
    return list(dict.fromkeys([str(w) for w in (allowed or [])] + [str(w) for w in (whitelist or [])]))


def _whitelist_spans(prose: str, terms: list) -> list:
    """terms 中长词在 prose 里的全部出现区间（用于豁免「概念词内嵌于白名单长词」）。"""
    spans = []
    for t in terms:
        if t and len(t) >= 2:
            for m in re.finditer(re.escape(t), prose):
                spans.append((m.start(), m.end()))
    return spans


def is_in_dialogue(text: str, pos: int) -> bool:
    """判断位置是否在对话引号内。

    支持两套引号：
    - 中文引号 “”／「」：向前开引号数 > 闭引号数 → 在对话内
    - 半角双引号 "：本项目正文对话默认半角引号（normalize-punctuation
      --quote-mode ascii，番茄口径），按「前面 " 出现次数为奇数」判定在对话内
    """
    before = text[:pos]
    # 中文引号配对
    open_count = before.count("“") + before.count("「")
    close_count = before.count("”") + before.count("」")
    if open_count > close_count:
        return True
    # 半角双引号奇偶配对（对话段 "xxx" 内部的前置 " 数为奇数）
    if before.count('"') % 2 == 1:
        return True
    return False


def is_in_thought(text: str, pos: int) -> bool:
    """判断位置是否在内心独白段落内（向前50字内有思想标记）。"""
    start = max(0, pos - 50)
    context = text[start:pos]
    return any(m in context for m in THOUGHT_MARKERS)


def has_source_explanation(text: str, pos: int, item: str) -> bool:
    """判断上下文是否有来源解释。"""
    start = max(0, pos - 30)
    end = min(len(text), pos + len(item) + 10)
    context = text[start:end]
    return any(m in context for m in SOURCE_MARKERS)


def check_prose(prose: str, whitelist: list, chapter: str = "",
                concept_allow: list | None = None,
                register_mode: str = "strict",
                extra_items: list | None = None) -> list:
    """检测正文中的时代器物词与现代概念词（声道制 × 语域三档 × 命名传播链）。

    extra_items（2026-10-25 P1-7）：本书设定层追加禁用器物词（来自 设定/世界观
    「禁用」条款，如 易拉罐/拉环/铝/橡胶），与 MODERN_ITEMS 同规则检测、同豁免
    逻辑（白名单/内心/来源解释/对话降级），供单书特化词表，不改静态词库。

    器物词（MODERN_ITEMS）：沿用原五条豁免逻辑（白名单/内心/来源解释/对话降级）。
    概念词（MODERN_CONCEPT_WORDS）：按声道分流——
      对话内   → 说话人归属交 LLM 审稿 I-E 终判（主角合法/土著违规）
      旁白     → 按档判罚（strict=HIGH 直接违规；balanced=MEDIUM 点缀观察）
      内心独白 → 不告警（单视角书内心=主角内心）
    命名传播链（2026-10-25 用户裁决，适用于 premise 商品名 whitelist 命中词）：
      现代物品名首次命名权归主角——旁白（主角视角）与内心独白合法；其他角色对白
      须已亲历主角传播（本章/前章在场听到/看到价牌），未传播新客凭空说出=违规、
      应用替代词。确定性层无法归属说话人与追踪传播 → 商品名进对话=MEDIUM
      「传播链待判」交审稿 LLM 终判（对照细纲命名节拍与替代词表）。
    register_mode（bible.era_constraints.register_mode，缺省 strict）按
    REGISTER_SEVERITY 矩阵定严重度；modern 档概念词全面合法（不检测）。
    concept_allow 内的概念词完全解禁（含系统术语长词内嵌）。
    标题行（首个 `#` 开头行）= 营销 meta 层，全面豁免。
    """
    mode = register_mode if register_mode in REGISTER_MODES else "strict"
    # 标题行豁免区间（章名属营销层，可用商品名/诙谐词）
    title_end = 0
    _first = prose.split("\n", 1)
    if _first and _first[0].lstrip().startswith("#"):
        title_end = len(_first[0])
    alerts = []
    _seen_items: set = set()
    for item in list(MODERN_ITEMS) + [str(x) for x in (extra_items or [])]:
        if not item or item in _seen_items or item in whitelist:
            continue
        _seen_items.add(item)
        for m in re.finditer(re.escape(item), prose):
            pos = m.start()
            if pos < title_end:
                continue
            # 内心独白不告警
            if is_in_thought(prose, pos):
                continue
            # 有来源解释不告警
            if has_source_explanation(prose, pos, item):
                continue
            in_dialogue = is_in_dialogue(prose, pos)
            channel = "dialogue" if in_dialogue else "narration"
            severity = REGISTER_SEVERITY[("modern_item", channel)][mode]
            ctx_start = max(0, pos - 15)
            ctx_end = min(len(prose), pos + len(item) + 15)
            context = prose[ctx_start:ctx_end].replace("\n", " ")
            alerts.append({
                "chapter": chapter,
                "item": item,
                "kind": "modern_item",
                "severity": severity,
                "context": context,
                "in_dialogue": in_dialogue,
                "channel": channel,
                "reason": ("对话中出现（可能是主角说漏嘴）" if in_dialogue
                           else "物理世界中出现无来源现代物品"),
            })
    # —— 命名传播链：premise 商品名（whitelist 命中词）进对话 → 传播链待判
    #    （modern 档整体现代语感，命名权规则不适用，跳过）——
    if mode != "modern":
        for good in whitelist:
            # 长词内嵌豁免区间：更长的白名单词（如「快消品供应链采购表」覆盖「采购表」）
            longer_spans = [
                (m.start(), m.end())
                for w in whitelist if len(w) > len(good)
                for m in re.finditer(re.escape(w), prose)
            ]
            for m in re.finditer(re.escape(good), prose):
                pos = m.start()
                if pos < title_end:
                    continue  # 标题行（营销层）豁免
                if any(s <= pos < e for s, e in longer_spans):
                    continue  # 内嵌于更长白名单词（系统术语/全名）→ 放行
                # 旁白（主角视角）与内心独白 → 合法；只有进对话才需传播链判定
                if is_in_thought(prose, pos):
                    continue
                if not is_in_dialogue(prose, pos):
                    continue
                ctx_start = max(0, pos - 15)
                ctx_end = min(len(prose), pos + len(good) + 15)
                context = prose[ctx_start:ctx_end].replace("\n", " ")
                alerts.append({
                    "chapter": chapter, "item": good, "kind": "premise_good",
                    "severity": "MEDIUM", "context": context, "in_dialogue": True,
                    "channel": "dialogue",
                    "reason": ("对话中出现商品名——传播链待判：主角/已亲历主角命名的角色合法，"
                               "未传播新客凭空说出=违规（应用替代词），交审稿 I-E 对照细纲命名节拍终判"),
                })
    # —— 概念词声道制检测（modern 档全面合法，跳过整段）——
    if mode != "modern":
        allow = list(concept_allow or [])
        wl_spans = _whitelist_spans(prose, allow)
        _narr_hits = 0  # balanced 档密度计量（旁白概念词命中数）
        for word in MODERN_CONCEPT_WORDS:
            if word in allow:
                continue
            for m in re.finditer(re.escape(word), prose):
                pos = m.start()
                if pos < title_end:
                    continue
                # 内嵌于白名单长词（如《快消品供应链采购表》里的「供应链」）→ 放行
                if any(s <= pos < e for s, e in wl_spans):
                    continue
                # 内心独白 = 主角内心 → 合法
                if is_in_thought(prose, pos):
                    continue
                in_dialogue = is_in_dialogue(prose, pos)
                channel = "dialogue" if in_dialogue else "narration"
                severity = REGISTER_SEVERITY[("concept_word", channel)][mode]
                if channel == "narration":
                    _narr_hits += 1
                ctx_start = max(0, pos - 15)
                ctx_end = min(len(prose), pos + len(word) + 15)
                context = prose[ctx_start:ctx_end].replace("\n", " ")
                if in_dialogue:
                    reason = ("对话中出现现代概念词——说话人待判：主角（穿越者）合法 / "
                              "修仙界土著违规，交审稿 I-E 终判")
                elif mode == "balanced":
                    reason = "旁白/公共描述使用现代概念词（修主现辅档=点缀观察项，密度与效果由审稿判罚）"
                else:
                    reason = "旁白/公共描述使用现代概念词（违反修仙界语域：叙述以修仙界为核心表述）"
                alerts.append({
                    "chapter": chapter, "item": word, "kind": "concept_word",
                    "severity": severity, "context": context, "in_dialogue": in_dialogue,
                    "channel": channel, "reason": reason,
                })
        # —— balanced 档「适当出现」量化红线：旁白概念词密度 > 1.5 处/千字 → 堆砌 ——
        # （口径对齐 detect_internal I-E 模板词密度的 1.5/千字先例；单点 MEDIUM 不罚，
        #  密度超标整体升 HIGH 交审稿判罚——「点缀」从此可测。）
        if mode == "balanced" and _narr_hits >= 2:
            _vis = len(re.sub(r"\s+", "", prose)) or 1
            _density = _narr_hits * 1000 / _vis
            # 样本护栏：全文 <1000 字时不做千字外推（短样本密度噪声大，单看条数）
            if _vis >= 1000 and _density > 1.5:
                alerts.append({
                    "chapter": chapter, "item": f"现代概念词密度 {_density:.1f} 处/千字",
                    "kind": "concept_density", "severity": "HIGH",
                    "context": f"旁白命中 {_narr_hits} 处 / 全文 {_vis} 字",
                    "in_dialogue": False, "channel": "narration",
                    "reason": "修主现辅档密度超标：点缀变堆砌（红线 1.5 处/千字），审稿应判罚",
                })
    return alerts


def find_chapter_file(book_dir: str, chapter: int) -> str:
    """查找章节文件。"""
    prose_dir = os.path.join(book_dir, "正文", "第一卷")
    if not os.path.exists(prose_dir):
        prose_dir = os.path.join(book_dir, "正文")
    pattern = f"第{chapter:03d}章"
    for f in os.listdir(prose_dir):
        if pattern in f and f.endswith(".md"):
            return os.path.join(prose_dir, f)
    return ""


def main():
    parser = argparse.ArgumentParser(description="时代器物词检测")
    parser.add_argument("--book", help="书项目目录")
    parser.add_argument("--chapter", type=int, help="检测指定章节")
    parser.add_argument("--all", action="store_true", help="检测所有章节")
    parser.add_argument("--file", help="直接检测指定正文文件")
    parser.add_argument("--bible", help="bible.json路径（--file模式下使用）")
    parser.add_argument("--json", action="store_true", help="输出JSON格式")
    args = parser.parse_args()

    if args.file:
        bible = {}
        if args.bible and os.path.exists(args.bible):
            with open(args.bible, "r", encoding="utf-8") as f:
                bible = json.load(f)
        whitelist = get_whitelist(bible)
        concept_allow = get_concept_allowlist(bible, whitelist)
        register_mode = resolve_register_mode(bible)
        with open(args.file, "r", encoding="utf-8") as f:
            prose = f.read()
        alerts = check_prose(prose, whitelist, os.path.basename(args.file),
                             concept_allow, register_mode)
    elif args.book:
        bible = load_bible(args.book)
        whitelist = get_whitelist(bible)
        concept_allow = get_concept_allowlist(bible, whitelist)
        register_mode = resolve_register_mode(bible)
        alerts = []
        if args.all:
            prose_dir = os.path.join(args.book, "正文", "第一卷")
            if not os.path.exists(prose_dir):
                prose_dir = os.path.join(args.book, "正文")
            files = sorted([f for f in os.listdir(prose_dir) if f.endswith(".md") and "第" in f])
            for f in files:
                ch_match = re.search(r"第(\d+)章", f)
                ch = ch_match.group(1) if ch_match else f
                with open(os.path.join(prose_dir, f), "r", encoding="utf-8") as fh:
                    prose = fh.read()
                alerts.extend(check_prose(prose, whitelist, ch, concept_allow, register_mode))
        elif args.chapter:
            fpath = find_chapter_file(args.book, args.chapter)
            if not fpath:
                print(f"未找到第{args.chapter}章文件", file=sys.stderr)
                sys.exit(1)
            with open(fpath, "r", encoding="utf-8") as f:
                prose = f.read()
            alerts = check_prose(prose, whitelist, str(args.chapter), concept_allow, register_mode)
        else:
            parser.print_help()
            sys.exit(1)
    else:
        parser.print_help()
        sys.exit(1)

    if args.json:
        print(json.dumps({"alerts": alerts, "count": len(alerts)}, ensure_ascii=False, indent=2))
    else:
        if not alerts:
            print("✓ 未发现时代器物词穿帮")
        else:
            print(f"发现 {len(alerts)} 处时代器物词告警：\n")
            for a in alerts:
                print(f"  [{a['severity']}][{a.get('kind', 'modern_item')}][{a.get('channel', '')}] 第{a['chapter']}章 「{a['item']}」")
                print(f"    上下文: ...{a['context']}...")
                print(f"    原因: {a['reason']}")
                print()
        print(f"白名单（主角随身）: {whitelist}")


if __name__ == "__main__":
    main()
