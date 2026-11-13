#!/usr/bin/env python3
"""
nww_mechanical_check.py — 机械自检（纯正则扫描，无 LLM）

职责：
  1. 扫描 AI 味特征（禁用词、模板句式、口癖重复）
  2. 扫描 prompt 泄漏（蓝图原话命中）
  3. 扫描结构问题（段落长度、句长分布）
  4. 输出 S1-S4 分级问题报告

不做什么：
  - 不自动修改正文
  - 不调用 LLM
  - 只做扫描和报告

依据规律：
  - Law 5: 口癖排他性
  - Law 6: 模板比喻禁用
  - Law 7: Prompt 泄漏检测
  - Law 9: 开篇即戏
  - Law 16: 疏密分配
"""

import re
import sys
import json
from pathlib import Path
from typing import List, Dict, Tuple

# ═══════════════════════════════════════════════════════════
# 严重等级定义
# ═══════════════════════════════════════════════════════════
S1 = "S1"  # Blocking：必须修改
S2 = "S2"  # Warning：建议修改
S3 = "S3"  # Advisory：可选参考
S4 = "S4"  # Info：信息提示

# ═══════════════════════════════════════════════════════════
# 正则黑名单（Law 6: 模板比喻禁用）
# ═══════════════════════════════════════════════════════════
TEMPLATE_PATTERNS = [
    # 最毒句式（Law 6）
    (r"仿佛.*一般", "模板比喻「仿佛...一般」", S1),
    (r"犹如.*般", "模板比喻「犹如...般」", S1),
    (r"宛若.*似的", "模板比喻「宛若...似的」", S1),
    (r"像潮水般", "模板比喻「像潮水般」", S1),
    (r"如闪电般", "模板比喻「如闪电般」", S1),
    (r"仿佛春风", "模板比喻「仿佛春风」", S1),
    
    # 禁用词（banned-words.md）
    (r"深吸一口气", "禁用词「深吸一口气」", S1),
    (r"不由得", "禁用词「不由得」", S2),
    (r"心中涌起一股", "禁用词「心中涌起一股」", S1),
    (r"心头一震", "禁用词「心头一震」", S1),
    (r"眼中闪过一丝", "禁用词「眼中闪过一丝」", S1),
    (r"嘴角勾起一抹", "禁用词「嘴角勾起一抹」", S1),
    (r"眉头微皱", "禁用词「眉头微皱」", S2),
    
    # 判断类（banned-words.md）
    (r"不容置疑", "禁用词「不容置疑」", S2),
    (r"显而易见", "禁用词「显而易见」", S2),
    (r"毫无疑问", "禁用词「毫无疑问」", S2),
    
    # 过渡类（banned-words.md）
    (r"不由自主", "禁用词「不由自主」", S2),
    (r"情不自禁", "禁用词「情不自禁」", S2),
    (r"自然而然", "禁用词「自然而然」", S2),
    
    # 总结句式（Law 7: Prompt 泄漏相关）
    (r"他终于明白", "总结句式「他终于明白」", S1),
    (r"这才意识到", "总结句式「这才意识到」", S1),
    (r"命运.*齿轮", "总结句式「命运的齿轮」", S1),
    (r"反击才刚刚开始", "总结句式「反击才刚刚开始」", S1),
]

# ═══════════════════════════════════════════════════════════
# 段落/句长检查规则（Law 16: 疏密分配）
# ═══════════════════════════════════════════════════════════
PARAGRAPH_RULES = [
    (r"\n\n.{150,}\n\n", "超长段（>150字）", S2, 3),  # 单章≤3处
    (r"(?<!\n)\n(?!\n)", "碎段（独立短句）", S3, None),  # advisory only
]

# ═══════════════════════════════════════════════════════════
# 检测器注册表
# ═══════════════════════════════════════════════════════════
class Detector:
    def __init__(self, name: str, pattern: str, message: str, severity: str, max_count: int = None):
        self.name = name
        self.pattern = pattern
        self.message = message
        self.severity = severity
        self.max_count = max_count

detectors = [
    # 模板比喻
    Detector("模板比喻", r"仿佛.*一般", "模板比喻「仿佛...一般」", S1),
    Detector("模板比喻", r"犹如.*般", "模板比喻「犹如...般」", S1),
    Detector("模板比喻", r"宛若.*似的", "模板比喻「宛若...似的」", S1),
    
    # 禁用词
    Detector("禁用词", r"深吸一口气", "禁用词「深吸一口气」", S1),
    Detector("禁用词", r"不由得", "禁用词「不由得」", S2),
    Detector("禁用词", r"心中涌起一股", "禁用词「心中涌起一股」", S1),
    Detector("禁用词", r"心头一震", "禁用词「心头一震」", S1),
    Detector("禁用词", r"眼中闪过一丝", "禁用词「眼中闪过一丝」", S1),
    Detector("禁用词", r"嘴角勾起一抹", "禁用词「嘴角勾起一抹」", S1),
    
    # 总结句式
    Detector("总结句式", r"他终于明白", "总结句式「他终于明白」", S1),
    Detector("总结句式", r"这才意识到", "总结句式「这才意识到」", S1),
    Detector("总结句式", r"命运.*齿轮", "总结句式「命运的齿轮」", S1),
    
    # 段落检查
    Detector("超长段", r"\n\n.{150,}\n\n", "超长段（>150字）", S2, 3),
]

def scan_file(filepath: Path) -> List[Dict]:
    """扫描单个文件，返回问题列表"""
    if not filepath.exists():
        return [{"error": f"文件不存在: {filepath}"}]
    
    content = filepath.read_text(encoding="utf-8")
    issues = []
    
    for detector in detectors:
        matches = list(re.finditer(detector.pattern, content, re.S))
        if matches:
            count = len(matches)
            # 检查是否超过最大允许次数
            if detector.max_count and count > detector.max_count:
                severity = S1 if detector.severity == S1 else S2
            else:
                severity = detector.severity
            
            # 只记录前 N 个匹配（避免报告过长）
            max_report = 5
            for match in matches[:max_report]:
                line_num = content[:match.start()].count('\n') + 1
                context = content[max(0, match.start()-20):match.end()+20].replace('\n', ' ').strip()
                issues.append({
                    "detector": detector.name,
                    "pattern": detector.pattern,
                    "severity": severity,
                    "line": line_num,
                    "context": f"...{context}...",
                    "match": match.group()
                })
    
    return issues

def scan_blueprint_leak(blueprint: str, prose: str) -> List[Dict]:
    """检测蓝图原话泄漏到正文（Law 7）"""
    issues = []
    
    # 提取蓝图中的关键句
    blueprint_sentences = re.split(r'[。！？\n]', blueprint)
    blueprint_sentences = [s.strip() for s in blueprint_sentences if len(s.strip()) > 10]
    
    # 检查正文中是否有高度相似的句子
    for sentence in blueprint_sentences[:20]:  # 只检查前20句
        # 去除标点
        clean_sentence = re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9]', '', sentence)
        if len(clean_sentence) < 15:
            continue
        
        # 在正文中搜索相似片段
        pattern = re.compile(re.escape(clean_sentence[:15]), re.S)
        matches = list(pattern.finditer(prose))
        
        if matches:
            # 匹配度 >80% 视为泄漏
            for match in matches:
                matched_text = match.group()
                similarity = len(set(matched_text) & set(clean_sentence)) / max(len(matched_text), len(clean_sentence))
                if similarity > 0.8:
                    issues.append({
                        "detector": "Prompt泄漏",
                        "severity": S1,
                        "line": prose[:match.start()].count('\n') + 1,
                        "context": f"蓝图原话命中: {matched_text}",
                        "blueprint_sentence": sentence[:50] + "..."
                    })
    
    return issues

def scan_voice_uniqueness(characters: List[Dict], prose: str) -> List[Dict]:
    """检测口癖重复（Law 5）"""
    issues = []
    
    # 收集所有口癖
    voice_map = {}
    for char in characters:
        name = char.get("name", "")
        anchors = char.get("voice_anchors", [])
        for anchor in anchors:
            if isinstance(anchor, dict):
                content = anchor.get("content", "")
            else:
                content = str(anchor)
            if content in voice_map:
                voice_map[content].append(name)
            else:
                voice_map[content] = [name]
    
    # 检测重复
    for voice, chars in voice_map.items():
        if len(chars) > 1:
            issues.append({
                "detector": "口癖重复",
                "severity": S1,
                "message": f"口癖「{voice[:20]}...」在 {', '.join(chars)} 之间重复",
                "chars": chars
            })
    
    return issues

def generate_report(issues: List[Dict], filepath: str = "") -> str:
    """生成问题报告"""
    if not issues:
        return f"✅ {filepath} 自检通过，无问题"
    
    # 按严重等级分组
    by_severity = {S1: [], S2: [], S3: [], S4: []}
    for issue in issues:
        severity = issue.get("severity", S4)
        by_severity[severity].append(issue)
    
    lines = [f"📋 自检报告: {filepath}", f"总计 {len(issues)} 个问题", ""]
    
    for severity in [S1, S2, S3, S4]:
        items = by_severity[severity]
        if not items:
            continue
        
        icon = {"S1": "🔴", "S2": "🟠", "S3": "🟡", "S4": "⚪"}[severity]
        label = {"S1": "Blocking", "S2": "Warning", "S3": "Advisory", "S4": "Info"}[severity]
        lines.append(f"{icon} {label} ({len(items)}个)")
        
        for i, issue in enumerate(items[:10], 1):  # 最多显示10个
            if "line" in issue:
                lines.append(f"  {i}. 第{issue['line']}行: {issue.get('message', issue.get('context', ''))}")
            else:
                lines.append(f"  {i}. {issue.get('message', issue.get('context', ''))}")
        
        if len(items) > 10:
            lines.append(f"  ... 还有 {len(items) - 10} 个同类问题")
        lines.append("")
    
    return "\n".join(lines)

def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="NWW-V12 机械自检（纯正则扫描）")
    parser.add_argument("file", help="要扫描的正文文件路径")
    parser.add_argument("--blueprint", help="蓝图文件路径（用于泄漏检测）")
    parser.add_argument("--characters", help="角色列表 JSON 文件（用于口癖检测）")
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式")
    parser.add_argument("--exit-code", action="store_true", help="根据 S1 问题数量设置退出码")
    
    args = parser.parse_args()
    
    issues = []
    
    # 扫描正文
    filepath = Path(args.file)
    issues.extend(scan_file(filepath))
    
    # 扫描蓝图泄漏
    if args.blueprint:
        blueprint_path = Path(args.blueprint)
        if blueprint_path.exists():
            prose_content = filepath.read_text(encoding="utf-8")
            blueprint_content = blueprint_path.read_text(encoding="utf-8")
            issues.extend(scan_blueprint_leak(blueprint_content, prose_content))
    
    # 扫描口癖重复
    if args.characters:
        chars_path = Path(args.characters)
        if chars_path.exists():
            with open(chars_path, "r", encoding="utf-8") as f:
                characters = json.load(f)
            prose_content = filepath.read_text(encoding="utf-8")
            issues.extend(scan_voice_uniqueness(characters, prose_content))
    
    # 输出结果
    if args.json:
        print(json.dumps({"file": args.file, "issues": issues, "total": len(issues)}, ensure_ascii=False, indent=2))
    else:
        print(generate_report(issues, args.file))
    
    # 设置退出码
    if args.exit_code:
        s1_count = sum(1 for i in issues if i.get("severity") == S1)
        sys.exit(1 if s1_count > 0 else 0)

if __name__ == "__main__":
    main()
