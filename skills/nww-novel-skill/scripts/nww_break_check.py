#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nww_break_check.py — NWW-V12 破例规则触发检查（确定性扫描）

职责：
  1. 从 bible.json 读每个角色的 break_rules[].effect（说出口的台词）
  2. 扫描正文，检查 effect 台词是否出现在**引号内**（说出口的）
  3. 引号外（内心独白/叙述）不算触发——因为 anchor 是心里想的，break_rule 是嘴上说的
  4. 输出 JSON：哪些 break_rule 被触发了，供 commit 时写入 global.used_breaks

设计原则：
  - 确定性规则匹配，不依赖 LLM 判断
  - 和 nww_voice_check.py 同层级，写后跑
  - 只检查"说出口的台词"，不检查 trigger 语义（trigger 由作者判断）

用法：
  python nww_break_check.py --book <书项目根> --file <正文.md> [--json]
"""
import argparse
import json
import os
import re
import sys


def load_bible(book):
    """读 bible.json，提取每个角色的 break_rules。"""
    path = os.path.join(book, "bible.json")
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        bible = json.load(f)
    # {角色名: [break_rule, ...]}
    result = {}
    for char in bible.get("characters", []):
        if not isinstance(char, dict):
            continue
        name = char.get("name", "")
        rules = char.get("break_rules", [])
        if name and rules:
            result[name] = rules
    return result


def is_in_quotes(line, pos):
    """检查 pos 位置是否在引号内。"""
    quote_chars = ['"', '「', '『', '“', '”']
    for qc in quote_chars:
        if line[:pos].count(qc) % 2 == 1:
            return True
    return False


def is_inner_monologue(line, pos):
    """检查 pos 位置是否在内心独白里（不是说出口的）。

    匹配点前 15 字内有这些词，说明是心里想的，不算破例触发。
    例外：如果"心里"后面跟着"说/道/问/喊"等对话动词，说明是说出口的（"她心里一紧，说：你别死"）。
    """
    prefix = line[max(0, pos - 15):pos]
    inner_markers = [
        "默念", "暗想", "想起", "心想", "暗自",
        "他在心里", "她在心里", "心中", "心头",
    ]
    # 先查精确的内心独白标记词
    for m in inner_markers:
        if m in prefix:
            return True
    # "心里"单独出现时，检查后面有没有对话动词
    if "心里" in prefix:
        # 找"心里"后面最近的字，如果是对话动词，说明是说出口的
        idx = prefix.rfind("心里")
        after = prefix[idx + 2:idx + 5]
        if any(v in after for v in ["说", "道", "问", "喊", "叫", "笑"]):
            return False  # 说出口的，不是独白
        return True
    return False


def normalize_punct(s):
    """归一化标点：去掉句号/逗号/顿号，用于模糊匹配。"""
    import re
    return re.sub(r'[。，、！？；：""「」‘’]', '', s)


def check_prose(prose_path, break_rules_by_char):
    """扫描正文，检查 break_rule.effect 是否出现在引号内（说出口的）。

    返回：{角色名: [触发的 break_rule name 列表]}
    """
    with open(prose_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    triggered = {}

    for char_name, rules in break_rules_by_char.items():
        for rule in rules:
            effect = rule.get("effect", "").strip()
            if not effect:
                continue
            rule_name = rule.get("name", effect[:10])
            effect_norm = normalize_punct(effect)

            for i, line in enumerate(lines):
                # 先在原始行精确匹配（标点对齐时直接命中）
                pos = line.find(effect)
                if pos == -1:
                    # 标点不对齐时，归一化后匹配
                    line_norm = normalize_punct(line)
                    effect_norm = normalize_punct(effect)
                    if effect_norm and line_norm.find(effect_norm) != -1:
                        # 归一化匹配到了，但位置映射不准——用原始行的近似位置
                        pos = line.find(effect[:4]) if len(effect) >= 4 else 0
                    else:
                        continue
                if pos == -1:
                    continue

                # 内心独白不算触发
                if is_inner_monologue(line, pos):
                    continue

                # 只算引号内（说出口的），引号外是叙述
                if is_in_quotes(line, pos):
                    if char_name not in triggered:
                        triggered[char_name] = []
                    entry = {
                        "rule_name": rule_name,
                        "effect": effect,
                        "line": i + 1,
                        "excerpt": line.strip()[:80],
                    }
                    if entry not in triggered[char_name]:
                        triggered[char_name].append(entry)

    return triggered


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", required=True, help="书项目根（含 bible.json）")
    ap.add_argument("--file", required=True, help="待检查正文 md 路径")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args()

    break_rules = load_bible(args.book)
    if not break_rules:
        print(json.dumps({
            "status": "skip",
            "reason": "bible.json 中无 break_rules 字段",
            "triggered": {},
        }, ensure_ascii=False, indent=2))
        return

    triggered = check_prose(args.file, break_rules)

    # 统计触发数
    total = sum(len(v) for v in triggered.values())

    output = {
        "file": args.file,
        "characters_checked": len(break_rules),
        "triggered": triggered,
        "trigger_count": total,
    }

    if total > 0:
        output["status"] = "triggered"
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        output["status"] = "clean"
        print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
