#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nww_voice_check.py — NWW-V12 角色 voice 一致性确定性检查

职责（模型无关、确定性、fail-closed 兜底）：
  1. 读 {BOOK}/设定/角色/*.md，提取每个角色的「禁用表达」列表
  2. 扫描正文，检查禁用表达是否出现在该角色相关段落（角色名前后3行内）
  3. 命中即报错，输出 JSON 报告（文件名、行号、角色、命中词、上下文）
  4. 叙述行归因（2026-09-08）：非引号内命中且角色名不在命中行本身（仅在场）→ 降为 advisory
  5. 正面验证（--check-presence）：检查角色口头禅是否在正文对话中出现（advisory）

设计原则：
  - 确定性规则匹配，不依赖 LLM 判断
  - 和 nww_enforce.py 同层级，写后必须先跑
  - 零命中 = pass；有命中 = fail，必须修

用法：
  python nww_voice_check.py --book <书项目根> --file <正文.md>
  python nww_voice_check.py --book <书项目根> --file <正文.md> --check-presence
"""
import argparse
import json
import os
import re
import sys


def extract_character_name(filepath):
    """从角色卡文件名提取角色名（去掉.md后缀）。"""
    basename = os.path.basename(filepath)
    name = re.sub(r'\.md$', '', basename)
    return name


def extract_banned_expressions(filepath):
    """从角色卡提取「禁用表达」列表。

    支持两种格式：
    1. ## 禁用表达 标题下的 - 开头列表项
    2. 禁用表达：xxx 行内格式（逗号/顿号分隔）

    提取规则：
    - 去掉行末括号解释（（...）或(...)）
    - 去掉行首分类前缀（如「自称」「表情描写」等）
    - 按「」、引号、逗号、顿号分割成单个词
    - 过滤掉2字以下的词
    """
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception:
        return []

    banned = []
    raw_lines = []

    # 格式1：## 禁用表达 标题下的列表
    in_section = False
    for line in content.splitlines():
        stripped = line.strip()
        if re.match(r'^#+\s*禁用表达', stripped):
            in_section = True
            continue
        if in_section and re.match(r'^#+\s', stripped):
            in_section = False
            continue
        if in_section and stripped.startswith('-'):
            raw_lines.append(stripped.lstrip('-').strip())

    # 格式2：行内 禁用表达：xxx
    m = re.search(r'禁用表达[：:]\s*(.+)', content)
    if m:
        raw_lines.append(m.group(1).strip())

    # 从每行提取禁用词
    for raw in raw_lines:
        # 去掉行末括号解释
        cleaned = re.sub(r'[（(][^）)]*[）)]', '', raw).strip()
        # 去掉行首分类前缀（如「自称」「表情描写」「有温度的提醒/劝诫」等）
        # 只保留「」或引号内的词，以及顿号/逗号分隔的词
        # 提取「」/『』/“”/""内的词（2026-10-26 R2-4：补齐中文弯引号“”与‘’支持）
        for m2 in re.finditer(r'「([^」]+)」|『([^』]+)』|“([^”]+)”|‘([^’]+)’|"([^"]+)"', cleaned):
            word = next((g for g in m2.groups() if g is not None), "").strip()
            if len(word) >= 2 and word not in banned:
                banned.append(word)
        # 如果没有引号，按顿号/逗号分割
        if not re.search(r'[「『“‘"]', cleaned):
            for part in re.split(r'[，,、；;]', cleaned):
                part = part.strip().strip('「」『』“”‘’"\'')
                if len(part) >= 2 and part not in banned:
                    banned.append(part)

    return banned


def load_character_voices(book):
    """加载所有角色卡的禁用表达。

    返回：{角色名: [禁用表达列表]}
    """
    char_dir = os.path.join(book, '设定', '角色')
    voices = {}
    if not os.path.isdir(char_dir):
        return voices

    for fname in os.listdir(char_dir):
        if not fname.endswith('.md'):
            continue
        fpath = os.path.join(char_dir, fname)
        name = extract_character_name(fpath)
        banned = extract_banned_expressions(fpath)
        if banned:
            voices[name] = banned

    return voices


def extract_catchphrases(filepath):
    """从角色卡提取口头禅列表。

    支持格式：
    1. 口头禅/标志动作：xxx 行内格式
    2. ## 口头禅 标题下的列表项
    3. 角色卡模板填空格式：口头禅/标志动作：（xxx）

    提取规则：
    - 提取「」和引号内的词
    - 去掉括号解释
    - 过滤掉 2 字以下的词
    """
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception:
        return []

    catchphrases = []

    # 格式1：行内 口头禅/标志动作：xxx
    for m in re.finditer(r'口头禅/标志动作[：:]\s*(.+)', content):
        raw = m.group(1).strip()
        cleaned = re.sub(r'[（(][^）)]*[）)]', '', raw).strip()
        for m2 in re.finditer(r'「([^」]+)」|『([^』]+)』|“([^”]+)”|‘([^’]+)’|"([^"]+)"', cleaned):
            word = next((g for g in m2.groups() if g is not None), "").strip()
            if len(word) >= 2 and word not in catchphrases:
                catchphrases.append(word)
        if not re.search(r'[「『“‘"]', cleaned):
            for part in re.split(r'[，,、；;]', cleaned):
                part = part.strip().strip('「」『』“”‘’"\'')
                if len(part) >= 2 and part not in catchphrases:
                    catchphrases.append(part)

    # 格式2：## 口头禅 标题下的列表
    in_section = False
    for line in content.splitlines():
        stripped = line.strip()
        if re.match(r'^#+\s*口头禅', stripped):
            in_section = True
            continue
        if in_section and re.match(r'^#+\s', stripped):
            in_section = False
            continue
        if in_section and stripped.startswith('-'):
            raw = stripped.lstrip('-').strip()
            cleaned = re.sub(r'[（(][^）)]*[）)]', '', raw).strip()
            for m2 in re.finditer(r'「([^」]+)」|『([^』]+)』|“([^”]+)”|‘([^’]+)’|"([^"]+)"', cleaned):
                word = next((g for g in m2.groups() if g is not None), "").strip()
                if len(word) >= 2 and word not in catchphrases:
                    catchphrases.append(word)
            if not re.search(r'[「『“‘"]', cleaned):
                for part in re.split(r'[，,、；;]', cleaned):
                    part = part.strip().strip('「」『』“”‘’"\'')
                    if len(part) >= 2 and part not in catchphrases:
                        catchphrases.append(part)

    return catchphrases


def load_character_catchphrases(book):
    """加载所有角色卡的口头禅。

    返回：{角色名: [口头禅列表]}
    """
    char_dir = os.path.join(book, '设定', '角色')
    result = {}
    if not os.path.isdir(char_dir):
        return result

    for fname in os.listdir(char_dir):
        if not fname.endswith('.md'):
            continue
        fpath = os.path.join(char_dir, fname)
        name = extract_character_name(fpath)
        catchphrases = extract_catchphrases(fpath)
        if catchphrases:
            result[name] = catchphrases

    return result


def extract_forbidden_behaviors(filepath):
    """从角色卡提取 Layer 11 角色禁区列表。

    读取 `## 12层角色行为约束` → `### Layer 11: 角色禁区` 下的：
    - 不符合人格的行为
    - 不符合身份的语言
    - 剧情层面禁止

    返回：[禁区描述列表]
    """
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception:
        return []

    forbidden = []

    # 找到 Layer 11 区块
    in_layer11 = False
    in_subsection = False
    for line in content.splitlines():
        stripped = line.strip()
        # 进入 Layer 11
        if re.match(r'^#+\s*Layer\s*11', stripped):
            in_layer11 = True
            continue
        # 离开 Layer 11（遇到下一个 Layer 或章节标题）
        if in_layer11 and re.match(r'^#+\s*(Layer|##\s)', stripped) and not re.match(r'^#+\s*Layer\s*11', stripped):
            in_layer11 = False
            continue
        if not in_layer11:
            continue

        # 提取 - 开头的列表项
        if stripped.startswith('-'):
            item = stripped.lstrip('-').strip()
            # 去掉冒号前的分类标签（如「不符合人格的行为：」）
            item_cleaned = re.sub(r'^[^：:]*[：:]\s*', '', item)
            if len(item_cleaned) >= 4 and item_cleaned not in forbidden:
                forbidden.append(item_cleaned)

    return forbidden


def load_forbidden_behaviors(book):
    """加载所有角色卡的 Layer 11 禁区。

    返回：{角色名: [禁区描述列表]}
    """
    char_dir = os.path.join(book, '设定', '角色')
    result = {}
    if not os.path.isdir(char_dir):
        return result

    for fname in os.listdir(char_dir):
        if not fname.endswith('.md'):
            continue
        fpath = os.path.join(char_dir, fname)
        name = extract_character_name(fpath)
        forbidden = extract_forbidden_behaviors(fpath)
        if forbidden:
            result[name] = forbidden

    return result


def check_catchphrase_presence(prose_path, catchphrases_map):
    """检查角色口头禅是否在正文对话中出现。

    对每个角色的每条口头禅，扫描正文引号内对话：
    - 命中 → 记为 present
    - 未命中 → 记为 missing（advisory，不阻断）

    返回：advisories 列表（missing 的口头禅）
    """
    try:
        with open(prose_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except Exception as e:
        return [{"error": f"无法读取正文: {e}"}]

    advisories = []

    for char_name, catchphrases in catchphrases_map.items():
        for cp in catchphrases:
            found = False
            for i, line in enumerate(lines):
                # 只检查引号内的对话（兼容成对引号及未闭合引号）
                in_quote = bool(re.search(r'["“”「」『』]', line))
                if not in_quote:
                    continue

                # 检查口头禅是否在该行对话中
                if cp in line:
                    # 验证说话人是否是该角色
                    speaker = get_line_speaker(line)
                    if speaker and char_name not in speaker and speaker not in char_name:
                        continue
                    # 也检查前后3行是否有角色名（对话可能跨行）
                    context_start = max(0, i - 3)
                    context_end = min(len(lines), i + 4)
                    context = ''.join(lines[context_start:context_end])
                    if char_name in context:
                        found = True
                        break

            if not found:
                advisories.append({
                    "character": char_name,
                    "catchphrase": cp,
                    "line": 0,
                    "excerpt": "",
                    "note": f"口头禅「{cp}」未在 {char_name} 的对话中出现",
                })

    return advisories


def check_forbidden_behaviors(prose_path, forbidden_map):
    """扫描正文，检查角色禁区违反（Layer 11）。

    对每个角色的每条禁区描述，提取关键词并扫描正文：
    - 命中且角色在上下文中 → 违反（violation）
    - 命中但角色不在上下文中 → advisory

    返回：{violations: [...], advisories: [...]}
    """
    try:
        with open(prose_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except Exception as e:
        return {"error": f"无法读取正文: {e}", "violations": [], "advisories": []}

    violations = []
    advisories = []

    # 禁区关键词提取：从禁区描述中提取核心动作/行为词
    def _extract_action_keywords(forbidden_text):
        """从禁区描述中提取可扫描的行为关键词。"""
        keywords = []
        # 提取「」内的词（跳过单字如「我」，避免第一人称误报）
        for m in re.finditer(r'「([^」]+)」', forbidden_text):
            kw = m.group(1).strip()
            if len(kw) >= 3:
                keywords.append(kw)
        # 提取引号内的词（跳过单字）
        for m in re.finditer(r'"([^"]+)"', forbidden_text):
            kw = m.group(1).strip()
            if len(kw) >= 3:
                keywords.append(kw)
        # 提取动作关键词（动词+宾语模式）
        action_kws = re.findall(r'(?:绝对禁止|不得|不能|禁止|不做)\s*[：:]?\s*(.{3,10}?)(?:[。，]|$)', forbidden_text)
        for kw in action_kws:
            kw = kw.strip().strip('「」""')
            if len(kw) >= 3 and kw not in keywords:
                keywords.append(kw)
        # 提取顿号分隔的具体行为项（如「主动社交、长篇解释、暴露身份」）
        # 只取>=4字的项，避免过短误报
        for part in re.split(r'[、，,]', forbidden_text):
            part = part.strip().strip('（）()')
            if len(part) >= 4 and not part.startswith(('不是', '他是', '她是', '他没有', '她没有')):
                if part not in keywords:
                    keywords.append(part)
        return keywords[:8]  # 最多取8个关键词

    for char_name, forbidden_list in forbidden_map.items():
        for forbidden_text in forbidden_list:
            keywords = _extract_action_keywords(forbidden_text)
            if not keywords:
                continue

            for i, line in enumerate(lines):
                for kw in keywords:
                    if kw not in line:
                        continue

                    # 检查角色名是否在前后3行内
                    context_start = max(0, i - 3)
                    context_end = min(len(lines), i + 4)
                    context = ''.join(lines[context_start:context_end])
                    char_in_context = char_name in context

                    entry = {
                        "character": char_name,
                        "forbidden": forbidden_text[:60],
                        "keyword": kw,
                        "line": i + 1,
                        "excerpt": line.strip()[:80],
                    }

                    if char_in_context:
                        violations.append(entry)
                    else:
                        advisories.append(entry)

    return {
        "violations": violations,
        "advisories": advisories,
        "violation_count": len(violations),
        "advisory_count": len(advisories),
    }


def is_negated(line, banned, pos):
    """检查禁用词是否被否定（前面有没/不/无/未等否定词）。

    pos 是 banned 在 line 中的起始位置。
    检查 pos 前10个字符内是否有否定词。
    """
    prefix = line[max(0, pos - 10):pos]
    negation_words = ['没', '不', '无', '未', '别', '莫', '勿']
    for nw in negation_words:
        if nw in prefix:
            return True
    return False


def get_line_speaker(line):
    """提取该行的说话人（如果有对话标签）。

    匹配模式：XX说/道/问/答/笑了/乐了/开口/出声："..."
    返回说话人名字，无则返回None。
    """
    # 匹配 "角色名 + 说/道/问/答/笑/乐/开口/出声/喊/叫 + ：" 模式
    m = re.match(r'^([^，。："]{2,8}?)(?:被[^，。："]*?)?(?:说|道|问|答|笑|乐|开口|出声|喊|叫|接|回)[^：]*[：:]', line.strip())
    if m:
        return m.group(1)
    # 匹配 "角色名：" 直接模式
    m = re.match(r'^([^，。："]{2,8}?)[：:]', line.strip())
    if m:
        return m.group(1)
    return None


def check_prose(prose_path, voices):
    """扫描正文，检查禁用表达。

    判定逻辑：禁用表达出现在某行，且该角色名在前后3行内出现 → 命中。
    否定句豁免：禁用词前有没/不/无/未等否定词时不算违规。
    对话归属豁免：禁用词在引号内且该行说话人不是该角色时不算violation（降为advisory）。
    """
    try:
        with open(prose_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except Exception as e:
        return {"error": f"无法读取正文: {e}", "violations": []}

    violations = []
    advisories = []

    for char_name, banned_list in voices.items():
        for banned in banned_list:
            for i, line in enumerate(lines):
                # 查找禁用词在行中的所有出现位置
                start = 0
                while True:
                    pos = line.find(banned, start)
                    if pos == -1:
                        break
                    start = pos + len(banned)

                    # 否定句豁免
                    if is_negated(line, banned, pos):
                        continue

                    # 检查禁用词是否在引号内（支持 ASCII 双引号、中文弯引号“”与直角引号「」『』）
                    in_quote = False
                    before_pos = line[:pos]
                    if (before_pos.count('"') % 2 == 1
                            or before_pos.count('“') > before_pos.count('”')
                            or before_pos.count('「') > before_pos.count('」')
                            or before_pos.count('『') > before_pos.count('』')):
                        in_quote = True

                    # 如果在引号内，检查说话人
                    speaker = None
                    if in_quote:
                        speaker = get_line_speaker(line)
                        # 如果说话人明确且不是该角色，降为advisory
                        if speaker and char_name not in speaker and speaker not in char_name:
                            context_start = max(0, i - 3)
                            context_end = min(len(lines), i + 4)
                            context = ''.join(lines[context_start:context_end])
                            if char_name in context:
                                advisories.append({
                                    "character": char_name,
                                    "banned": banned,
                                    "line": i + 1,
                                    "excerpt": line.strip()[:80],
                                    "note": f"说话人是'{speaker}'，非{char_name}",
                                })
                                continue

                    # 检查角色名是否在前后3行内
                    context_start = max(0, i - 3)
                    context_end = min(len(lines), i + 4)
                    context = ''.join(lines[context_start:context_end])
                    char_in_context = char_name in context

                    entry = {
                        "character": char_name,
                        "banned": banned,
                        "line": i + 1,
                        "excerpt": line.strip()[:80],
                    }

                    if char_in_context:
                        # 叙述行归因修复（2026-09-08）：
                        # 非引号内的命中，区分两种误报：
                        # ① 角色名不在命中行本身（仅邻近行在场）→ advisory
                        # ② 命中行是 POV 内心叙述（含"他/她心里/想/知道"等标记），
                        #    意象归属 POV 角色，其他角色即使名字在该行（列举项/修饰语）
                        #    也降为 advisory
                        # 引号内命中已有 speaker 检测，不受此规则影响。
                        is_pov_inner = False
                        if not in_quote:
                            # 检测命中行及前一行是否有第三人称内心叙述标记
                            pov_markers = [
                                # 第三人称内心/心理叙述标记
                                "他心里", "她心里", "心里",  # "心里那架算盘""放在心里"
                                "他想", "她想", "暗想", "心想", "暗自想",
                                "他知道", "她知道", "他明白", "她明白",
                                "他记得", "她记得", "他发现", "她发现",
                                "他觉得", "她觉得", "他认为", "她认为", "他以为", "她以为",
                                "他感到", "她感到", "他清楚", "她清楚",
                                "他望见", "她望见", "他听见", "她听见",
                                "他看着", "她看着", "他望着", "她望着",
                                "他算着", "她算着", "他拨着", "她拨着",  # 心理动作
                            ]
                            # 只检查命中行本身：POV 内心叙述标记必须出现在命中行，
                            # 不蔓延到前一行（避免前一行的内心叙述把后一行的真实动作也降为advisory）
                            if any(m in line for m in pov_markers):
                                is_pov_inner = True

                        if not in_quote and (char_name not in line or is_pov_inner):
                            reason = "POV内心叙述，意象归属叙述主体" if is_pov_inner else "仅在场（邻近行），非命中行叙述主体"
                            entry["note"] = f"叙述行命中，{char_name}{reason}，降为advisory"
                            advisories.append(entry)
                        else:
                            violations.append(entry)
                    else:
                        advisories.append(entry)

    return {
        "violations": violations,
        "advisories": advisories,
        "violation_count": len(violations),
        "advisory_count": len(advisories),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", required=True, help="书项目根（含 设定/角色/）")
    ap.add_argument("--file", required=True, help="待检查正文 md 路径")
    ap.add_argument("--check-presence", action="store_true",
                    help="额外检查角色口头禅是否在正文对话中出现（advisory）")
    args = ap.parse_args()

    voices = load_character_voices(args.book)

    if not voices:
        print(json.dumps({
            "status": "skip",
            "reason": "未找到含「禁用表达」的角色卡",
            "characters_checked": 0,
            "violations": [],
            "advisories": [],
        }, ensure_ascii=False, indent=2))
        return

    result = check_prose(args.file, voices)

    # 正面验证：口头禅出现检查
    if args.check_presence:
        catchphrases_map = load_character_catchphrases(args.book)
        if catchphrases_map:
            cp_advisories = check_catchphrase_presence(args.file, catchphrases_map)
            result["advisories"].extend(cp_advisories)
            result["advisory_count"] = len(result["advisories"])

    # Layer 11 角色禁区检查
    forbidden_map = load_forbidden_behaviors(args.book)
    if forbidden_map:
        fb_result = check_forbidden_behaviors(args.file, forbidden_map)
        if "error" not in fb_result:
            result["violations"].extend(fb_result.get("violations", []))
            result["advisories"].extend(fb_result.get("advisories", []))
            result["violation_count"] = len(result["violations"])
            result["advisory_count"] = len(result["advisories"])

    output = {
        "file": args.file,
        "characters_checked": len(voices),
        "characters": list(voices.keys()),
        **result,
    }

    if result.get("violation_count", 0) > 0:
        output["status"] = "fail"
        print(json.dumps(output, ensure_ascii=False, indent=2))
        sys.exit(1)
    else:
        output["status"] = "pass"
        print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
