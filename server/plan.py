"""
server.plan — 章纲规划模块

职责：
  1. 加载细纲模板（七要素）
  2. 调用 nww-architect agent 生成蓝图
  3. 校验七要素完整性
  4. 输出细纲文件

依据规律：
  - Law 2: 蓝图颗粒度决定剧情正确性（七要素）
  - Law 9: 开篇即戏
  - Law 10: 情绪弧线可验收
  - Law 12: 章尾钩子威胁性
  - Law 15: 道具三次出现
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import List, Dict, Optional


# 七要素蓝图模板
BLUEPRINT_TEMPLATE = """# 第{chapter_id}章 {chapter_title}

---
structural_fingerprint:
  conflict_pattern: "TODO"
  core_trope: "TODO"
  golden_finger_mode: "TODO"
  resolution_cost: "TODO"
  character_dynamic: "TODO"
  genre_context: "{genre}"
  core_plot_summary: "TODO"
  repeat_intent: "none"
---

## 七要素蓝图

### 1. 任务
- {task}

### 2. 地点
- {location}

### 3. 人物所求
- {characters_desire}

### 4. 冲突
- {conflict}

### 5. 转折
- {twist}

### 6. 结果
- {result}

### 7. 反派动作
- {villain_action}

## 元数据

- 字数目标：{wordcount_target}
- 字数口径：visible_chars_v1
- 目标情绪：{target_emotion}
- 核心事件：{core_event}
- 逻辑线：{logic_line}

## 出场角色 voice 锚点

{voice_anchors}

## 必种件清单

- [ ] {must_keep}

## 章尾钩子

- 类型：{hook_type}
- 内容：{hook_content}

## 道具编排表（如有贯穿道具）

| 出现次数 | 章节 | 场景 | 意义 |
|----------|------|------|------|
| 1st | | | |
| 2nd | | | |
| 3rd | | | |
"""


def validate_seven_elements(outline: str) -> List[Dict]:
    """校验七要素完整性"""
    issues = []
    
    # 检查各要素是否存在
    elements = {
        "任务": r"###\s*1\.\s*任务",
        "地点": r"###\s*2\.\s*地点",
        "人物所求": r"###\s*3\.\s*人物所求",
        "冲突": r"###\s*4\.\s*冲突",
        "转折": r"###\s*5\.\s*转折",
        "结果": r"###\s*6\.\s*结果",
    }
    
    for name, pattern in elements.items():
        if not re.search(pattern, outline):
            issues.append({
                "element": name,
                "severity": "S1",
                "message": f"缺少七要素: {name}"
            })
    
    # 检查反派动作（可选但推荐）
    if "### 7. 反派动作" not in outline:
        issues.append({
            "element": "反派动作",
            "severity": "S3",
            "message": "建议添加反派动作（如有对立势力）"
        })
    
    # 检查必种件清单
    if "## 必种件清单" not in outline:
        issues.append({
            "element": "必种件清单",
            "severity": "S2",
            "message": "建议添加必种件清单"
        })
    
    return issues


def generate_blueprint(
    book_root: Path,
    chapter_id: int,
    title: str = "",
    task: str = "",
    location: str = "",
    characters_desire: str = "",
    conflict: str = "",
    twist: str = "",
    result: str = "",
    villain_action: str = "",
    wordcount_target: str = "2000-2500",
    target_emotion: str = "",
    core_event: str = "",
    logic_line: str = "",
    voice_anchors: str = "",
    must_keep: str = "",
    hook_type: str = "",
    hook_content: str = ""
) -> str:
    """生成七要素蓝图"""
    
    # 读取 bible.json 获取题材
    bible_path = book_root / "bible.json"
    genre = "未知"
    if bible_path.exists():
        bible = json.loads(bible_path.read_text(encoding="utf-8"))
        genre = bible.get("genre", "未知")
    
    # 填充模板
    blueprint = BLUEPRINT_TEMPLATE.format(
        chapter_id=chapter_id,
        chapter_title=title or f"第{chapter_id}章",
        genre=genre,
        task=task or "TODO",
        location=location or "TODO",
        characters_desire=characters_desire or "TODO",
        conflict=conflict or "TODO",
        twist=twist or "TODO",
        result=result or "TODO",
        villain_action=villain_action or "TODO",
        wordcount_target=wordcount_target,
        target_emotion=target_emotion or "TODO",
        core_event=core_event or "TODO",
        logic_line=logic_line or "TODO",
        voice_anchors=voice_anchors or "TODO",
        must_keep=must_keep or "TODO",
        hook_type=hook_type or "TODO",
        hook_content=hook_content or "TODO"
    )
    
    return blueprint


def run(args) -> int:
    """章纲规划入口"""
    book_root = Path(args.book_root)
    chapter_id = args.chapter_id
    
    # 检查项目结构
    bible_path = book_root / "bible.json"
    if not bible_path.exists():
        print(f"❌ 找不到 bible.json: {bible_path}")
        return 1
    
    # 检查细纲是否已存在
    outline_dir = book_root / "大纲"
    outline_path = outline_dir / f"细纲_第{chapter_id}章.md"
    
    if outline_path.exists():
        print(f"⚠️  细纲已存在: {outline_path}")
        overwrite = input("是否覆盖？(y/N): ").strip().lower()
        if overwrite != "y":
            print("取消操作")
            return 0
    
    # 读取 bible.json
    bible = json.loads(bible_path.read_text(encoding="utf-8"))
    
    # 输出规划提示
    print("📐 章纲规划器")
    print("=" * 50)
    print(f"书名: {bible.get('title', '未知')}")
    print(f"章节: 第 {chapter_id} 章")
    print()
    
    # 显示七要素模板
    print("📋 请按以下七要素填写细纲:")
    print()
    print(BLUEPRINT_TEMPLATE.format(
        chapter_id=chapter_id,
        chapter_title=f"第{chapter_id}章",
        genre=bible.get('genre', '未知'),
        task="[任务]",
        location="[地点]",
        characters_desire="[人物所求]",
        conflict="[冲突]",
        twist="[转折]",
        result="[结果]",
        villain_action="[反派动作]",
        wordcount_target=bible.get('wordcount_target', '2000-2500'),
        target_emotion="[目标情绪]",
        core_event="[核心事件]",
        logic_line="[逻辑线]",
        voice_anchors="[voice锚点]",
        must_keep="[必种件]",
        hook_type="[钩子类型]",
        hook_content="[钩子内容]"
    ))
    print()
    
    # 提示保存路径
    print(f"💾 保存路径: {outline_path}")
    print()
    print("下一步: python -m server write <书根> <章节号>")
    
    return 0


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("book_root")
    parser.add_argument("chapter_id", type=int)
    parser.add_argument("--outline", help="细纲文件路径（可选）")
    sys.exit(run(parser.parse_args()))
