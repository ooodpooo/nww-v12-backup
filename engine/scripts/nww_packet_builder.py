#!/usr/bin/env python3
"""
nww_packet_builder.py
极简上下文打包器：从大纲与状态文件中抽取 <= 1500 Token 的纯净写作小抄
"""

import os
import re
import sys
import json
from pathlib import Path

def load_json(path: Path) -> dict:
    """安全加载 JSON 文件"""
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def get_preceding_paragraph(book_root: Path, chapter_id: int, max_chars: int = 300) -> str:
    """读取上一章草稿末尾指定字数的纯文本"""
    if chapter_id <= 1:
        return "【序幕开篇】"
    
    # 尝试多种文件名格式
    possible_names = [
        f"ch{chapter_id - 1:04d}.md",
        f"ch{chapter_id - 1}.md",
        f"第{chapter_id - 1}章.md",
    ]
    
    for name in possible_names:
        prev_chapter_file = book_root / "draft" / name
        if prev_chapter_file.exists():
            text = prev_chapter_file.read_text(encoding="utf-8").strip()
            # 过滤掉标题行
            lines = [line for line in text.splitlines() if not line.startswith("#")]
            clean_text = "\n".join(lines).strip()
            return clean_text[-max_chars:] if len(clean_text) > max_chars else clean_text
    
    return "【前文衔接缺失】"

def extract_section(text: str, pattern: str) -> str:
    """从 Markdown 中提取指定章节内容（支持列表格式）"""
    # 尝试匹配 "- 核心事件：xxx" 或 "## 核心事件\nxxx" 格式
    match = re.search(
        rf"(?:^|\n)\s*[-*]\s*(?:{pattern})\s*[:：]\s*(.*?)(?=\n\s*[-*]|$)", 
        text, 
        re.S
    )
    if match:
        return match.group(1).strip()
    
    # 尝试匹配 "## 核心事件\nxxx" 格式
    match = re.search(
        rf"(?:###?|\*\*)\s*(?:{pattern})\s*[:：]?\s*(.*?)(?=\n(?:###?|\*\*)|$)", 
        text, 
        re.S
    )
    return match.group(1).strip() if match else ""

def extract_characters_from_text(text: str) -> list:
    """从文本中提取可能的角色名（2-4字中文名）"""
    if not text:
        return []
    
    # 常见非角色词（需要过滤）
    non_character_words = {"越发", "显然", "突然", "忽然", "于是", "因此", "然而", "但是", "而且", "或者", "以及", "虽然", "不过", "只是", "就是", "还是", "已经", "正在"}
    
    # 方法1：匹配 "沈砚说"、"沈砚道"、"沈砚笑" 等模式（2-4字名 + 动词）
    pattern1 = r"([\u4e00-\u9fa5]{2,4})(?=[说道笑叹问答想看听走跑站坐躺])"
    matches1 = re.findall(pattern1, text)
    
    # 方法2：匹配 "掌案郑直" 中的 "郑直"（职位 + 2字名）
    pattern2 = r"(?:掌案|大人|先生|姑娘|公子|少爷|小姐)([\u4e00-\u9fa5]{2,3})"
    matches2 = re.findall(pattern2, text)
    
    # 方法3：匹配句子开头的名字（如 "沈砚当值"）
    pattern3 = r"^([\u4e00-\u9fa5]{2,4})(?=[当借发现心里过])"
    matches3 = re.findall(pattern3, text, re.MULTILINE)
    
    # 合并所有匹配结果
    all_matches = matches1 + matches2 + matches3
    
    # 清理：移除 "郑直笑" 这种 "名字+动词" 的错误匹配
    cleaned_matches = []
    for name in all_matches:
        # 如果名字以动词结尾，可能是错误匹配（如 "郑直笑"）
        if name and name[-1] in "说道笑叹问答想看听走跑站坐躺":
            # 只取前2-3个字作为名字
            if len(name) > 2:
                cleaned_name = name[:-1]  # 移除最后一个动词字符
                if cleaned_name and len(cleaned_name) >= 2:
                    cleaned_matches.append(cleaned_name)
        else:
            cleaned_matches.append(name)
    
    # 去重并过滤非角色词
    seen = set()
    unique_names = []
    for name in cleaned_matches:
        # 跳过包含职位前缀的名字（如 "掌案郑直"）
        if name.startswith(("掌案", "大人", "先生", "姑娘", "公子", "少爷", "小姐")):
            continue
        if name not in seen and name not in non_character_words and len(name) >= 2:
            seen.add(name)
            unique_names.append(name)
    
    return unique_names[:5]

def _parse_outline_roster(content: str) -> list:
    """P4-B 第二刀·细纲数据形态（外脑裁定 A）：确定性解析「出场角色」小节。

    匹配细纲 md 中的结构化角色清单：
        `出场角色：` 或 `出场角色voice` 之前的纯 list 小节，格式为 `- {名字}` 或 `- **名字**`。
    - 仅取中文角色名（2-4字），只读、不推理、不做 NLP、不解析 voice 锚点规则。
    - 无「出场角色」小节或无有效行 → 返回 []（由调用方回退逻辑线/核心事件提取）。
    """
    if not content:
        return []
    m = re.search(
        r"^###?\s*出场角色(?:\s*[:：])?\s*$.*?(?=^###?\s*|\Z)",
        content,
        re.M | re.S,
    )
    if not m:
        return []
    roster: list[str] = []
    for line in m.group(0).splitlines():
        line = line.strip()
        line = re.sub(r"^\s*[-*•]\s*", "", line).strip()
        if not line:
            continue
        line = line.strip("*").strip()
        name = re.match(r"^([\u4e00-\u9fa5]{2,4})\b", line)
        if name:
            n = name.group(1)
            # 排除「角色名+冒号+规则」的 voice 锚点写法，只取纯名单
            if n not in roster:
                roster.append(n)
    return roster


def load_outline(book_root: Path, chapter_id: int) -> dict:
    """读取细纲文件（支持 JSON 和 Markdown）"""
    # 优先尝试 JSON
    outline_json = book_root / "大纲" / f"细纲_第{chapter_id}章.json"
    if outline_json.exists():
        return load_json(outline_json)
    
    # 尝试 Markdown 格式
    possible_md_names = [
        f"细纲_第{chapter_id}章.md",
        f"ch{chapter_id}_outline.md",
        f"outline_{chapter_id}.md",
    ]
    
    for name in possible_md_names:
        outline_md = book_root / "大纲" / name
        if outline_md.exists():
            content = outline_md.read_text(encoding="utf-8")
            
            # 提取核心事件（主要剧情）
            core_event = extract_section(content, "核心事件|剧情节拍|本章目标")
            
            # 提取逻辑线（用于理解剧情走向）
            logic_line = extract_section(content, "逻辑线|剧情线")
            
            # 提取人物关系变化
            relationship_change = extract_section(content, "人物关系变化|关系变化")
            
            # 提取字数目标
            wordcount_match = re.search(r"字数目标[：:]\s*(\d+)", content)
            wordcount = wordcount_match.group(1) if wordcount_match else "2000-2500"
            
            # 提取出场角色：优先读细纲「出场角色」小节（结构化 list，确定性、非 NLP）
            # P4-B 第二刀·细纲数据形态（外脑裁定 A）：出场角色字段存在 → 用该 list；
            # 缺失 → 回退现有 逻辑线→核心事件 提取（保证旧章零回归）。
            characters = _parse_outline_roster(content)
            if not characters:
                characters = extract_characters_from_text(logic_line)
            if not characters:
                characters = extract_characters_from_text(core_event)
            
            return {
                "核心事件": core_event,
                "逻辑线": logic_line,
                "人物关系变化": relationship_change,
                "出场角色": characters,
                "字数目标": wordcount
            }
    
    return {}

def compile_bible_constraints(book_root: Path, outline: dict) -> list:
    """P4-B 第二刀：只读编译 bible 硬约束，追加进 must_avoid。

    仅做「事实编译」，不做「规则生成器」：
    - S1 时代禁制：从 bible.era_constraints 提炼一条 Writer 可消费的硬禁制（不全量塞入）。
    - S2身份/S3 称谓：只对「细纲出场角色 ∩ bible.characters」编译 hard_constraints.never，
      并保持角色归属，绝不做成全局限词。
    - bible 缺失 / 无相关结构化约束 → 返回 []，不产生任何判断逻辑。
    """
    bible = load_json(book_root / "bible.json")
    if not bible:
        return []

    constraints: list[str] = []

    # S1 时代禁制：提炼一条硬禁制，不整段塞入
    era = bible.get("era_constraints") or {}
    era_name = era.get("era", "")
    if era_name or era.get("note"):
        # 保留 bible.note 语义：主角从现代带入的凡货合理；物理世界自然出现的工业制品为穿帮
        constraints.append(
            f"当前世界为{era_name or '修仙界'}，不存在现代工业制品；"
            "物证场景中不得出现日光灯、LED 等现代工业照明或电子设备，"
            "不得出现不属于当前世界技术层级的工业制品。"
        )

    # S2身份/S3 称谓：仅对本章出场角色 ∩ bible.characters 编译，保持角色归属
    active_names = set(outline.get("出场角色", []) or [])
    for ch in bible.get("characters", []) or []:
        name = ch.get("name", "")
        if not name or name not in active_names:
            continue
        never = (ch.get("hard_constraints") or {}).get("never") or []
        never = [x for x in never if x]
        if never:
            constraints.append(f"{name}：{'；'.join(never)}")

    return constraints


def build_packet(chapter_id: int, book_root_str: str) -> dict:
    """构建 Writing Packet"""
    book_root = Path(book_root_str)
    
    if not book_root.exists():
        sys.stderr.write(f"[ERROR] 工作区不存在: {book_root_str}\n")
        sys.exit(1)
    
    # 1. 读细纲
    outline = load_outline(book_root, chapter_id)
    if not outline:
        sys.stderr.write(f"[ERROR] 找不到第 {chapter_id} 章细纲文件\n")
        sys.exit(1)

    # 2. 读 tracking state
    state = load_json(book_root / "追踪" / "_tracking-state.json")

    # 2.1 读 bible（权威事实源：voice/catchphrases/hard_constraints/era）
    bible = load_json(book_root / "bible.json")
    
    # 3. 提取出场角色状态与台词锚点
    active_chars = []
    characters_db = state.get("characters", {})
    bible_chars_by_name = {
        bch.get("name"): bch
        for bch in bible.get("characters", [])
        if bch.get("name")
    }
    for char_name in outline.get("出场角色", []):
        char_info = characters_db.get(char_name, {})
        voice_anchors = char_info.get("voice_anchor", [])
        # P4-B 第二刀·active_characters 补齐（外脑裁决：上游权威源对齐）
        # voice_rules 从 bible.characters 投影 voice + catchphrases（结构化搬运，不改事实）。
        # 仅作角色表达参考，不进入 must_avoid；tracking 无口吻字段时为权威源回退。
        bch = bible_chars_by_name.get(char_name) or {}
        voice_parts = [
            x for x in [bch.get("voice"), ]
            + (bch.get("catchphrases") or []) if x
        ]
        if voice_anchors:
            voice_rules = voice_anchors[:3]
        else:
            voice_rules = voice_parts[:3]
        active_chars.append({
            "name": char_name,
            "current_state": char_info.get("state", char_info.get("current_state", "正常")),
            "voice_rules": voice_rules
        })

    # 4. 提取硬事实
    hard_facts = {
        "cultivation": state.get("protagonist", {}).get("cultivation", "未知"),
        "cost_count": state.get("cost", {}).get("count", 0),
        "cost_rule": state.get("cost", {}).get("rule", "无")
    }

    # 4.1 只读投影：canonical confirmed facts → packet.confirmed_facts
    # P0-B 最小 Resolver（外脑批准范围）：从 state["facts"]["confirmed"] 做只读、
    # bounded projection，使 Writer 获得「唯一已认证事实源」的认可入口。
    # 约束：
    #   - 只投影 5 字段（id/fact/fact_type/source/recorded_chapter），不复制整行 state
    #   - 最多 30 条；排序 recorded_chapter DESC → id 稳定排序（优先最近确认事实）
    #   - 不混入 belief/hint（hints 段本轮不输出）
    #   - 无 facts 键或空列表 → []; 纯只读，绝不修改 state、不改变 provenance/hard_facts
    CONFIRMED_FACT_LIMIT = 30
    confirmed_facts: list[dict] = []
    _facts = state.get("facts") or {}
    _confirmed = _facts.get("confirmed") if isinstance(_facts, dict) else None
    if isinstance(_confirmed, list):
        projected = []
        for row in _confirmed:
            if not isinstance(row, dict):
                continue
            # 仅透传最小投影字段；其余内部状态字段不进入 packet
            projected.append(
                {
                    "id": row.get("id", ""),
                    "fact": row.get("fact", ""),
                    "fact_type": row.get("fact_type", ""),
                    "source": row.get("source", ""),
                    "recorded_chapter": row.get("recorded_chapter"),
                }
            )
        # 优先最近确认：recorded_chapter DESC，同章按 id 稳定排序保证可测
        projected.sort(
            key=lambda r: (
                r["recorded_chapter"] if isinstance(r["recorded_chapter"], int) else -1,
                r["id"],
            ),
            reverse=True,
        )
        confirmed_facts = projected[:CONFIRMED_FACT_LIMIT]

    # 5. 上一章最后300字
    preceding = get_preceding_paragraph(book_root, chapter_id, max_chars=300)

    # 6. 组装 Packet
    # K5：注入需处理伏笔（半衰期超期，writer 应在本章安排回收/推进/显式延期）
    pending_fs = [
        {"id": fid, "summary": row.get("summary", ""), "importance": row.get("importance", "中"),
         "planted_chapter": row.get("planted_chapter", 0)}
        for fid, row in (state.get("foreshadow") or {}).items()
        if isinstance(row, dict) and row.get("status") == "需处理"
    ]
    packet = {
        "chapter_id": chapter_id,
        "beat": outline.get("核心事件", ""),
        "logic_line": outline.get("逻辑线", ""),
        "must_keep": [x for x in outline.get("必须落地", []) if x],
        "must_avoid": [x for x in outline.get("禁止提前释放", []) if x]
                     + compile_bible_constraints(book_root, outline),
        "active_characters": active_chars,
        "hard_facts": hard_facts,
        "confirmed_facts": confirmed_facts,
        "preceding_context": preceding,
        "wordcount_target": outline.get("字数目标", "2000-2500"),
        "pending_foreshadows": pending_fs,
    }

    # 7. 严格熔断检查（粗估：1中文字符 ≈ 1.5 ~ 2 Tokens）
    packet_str = json.dumps(packet, ensure_ascii=False)
    est_tokens = len(packet_str) * 1.5
    if est_tokens > 2000:
        sys.stderr.write(f"[WARN] Packet 偏大 (~{int(est_tokens)} tokens)，自动截短前情...\n")
        packet["preceding_context"] = packet["preceding_context"][-150:]

    return packet

if __name__ == "__main__":
    # Task 6 / C-P1-4：v1 生产弃用——打印弃用说明并 exit 3；NWW_ALLOW_V1=1 仅测试/回放放行。
    # v2 是唯一生产 packet：python3 engine/scripts/nww_packet_builder_v2.py <book_root> <chapter_id>
    if os.environ.get("NWW_ALLOW_V1") != "1":
        sys.stderr.write(
            "[DEPRECATED] nww_packet_builder.py (v1) 已弃用，禁止生产使用："
            "packet 统一走 v1→v2 契约（active foreshadows / cost_panel / 内容硬闸）。\n"
            "生产命令: python3 engine/scripts/nww_packet_builder_v2.py <book_root> <chapter_id>\n"
            "临时放行（仅测试/回放）: NWW_ALLOW_V1=1\n"
        )
        sys.exit(3)
    if len(sys.argv) < 3:
        print("Usage: python3 nww_packet_builder.py <book_root> <chapter_id>")
        print("Example: python3 nww_packet_builder.py workspaces/my-novel 37")
        sys.exit(1)
        
    try:
        chapter_id = int(sys.argv[2])
        res = build_packet(chapter_id, sys.argv[1])
        print(json.dumps(res, ensure_ascii=False, indent=2))
    except ValueError:
        sys.stderr.write(f"[ERROR] 章节号必须是整数: {sys.argv[2]}\n")
        sys.exit(1)
