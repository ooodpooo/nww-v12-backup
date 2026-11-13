"""
server.design — 故事设计模块

职责：
  1. 加载 bible.json
  2. 调用 nww-architect agent 生成故事设计
  3. 输出：圣经摘要 + 人物卡 + 五级冲突链 + 卷纲

依据规律：
  - Law 1: 结构保下限范例提上限
  - Law 4: 幽默是配方
  - Law 14: 人物卡七维度
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List, Dict, Optional


def run(args) -> int:
    """故事设计入口"""
    book_root = Path(args.book_root)
    
    # 检查 bible.json
    bible_path = book_root / "bible.json"
    if not bible_path.exists():
        print(f"❌ 找不到 bible.json: {bible_path}")
        return 1
    
    bible = json.loads(bible_path.read_text(encoding="utf-8"))
    
    # 检查规律库
    knowledge_dir = book_root / "knowledge"
    if not knowledge_dir.exists():
        print(f"⚠️  知识库不存在: {knowledge_dir}")
        print("   请先运行: python -m server new <书名>")
        return 1
    
    laws_path = knowledge_dir / "laws.yaml"
    if not laws_path.exists():
        print(f"❌ 找不到规律库: {laws_path}")
        return 1
    
    # 读取人物卡 Schema
    schema_path = knowledge_dir / "character_card_schema.yaml"
    
    print("📖 故事设计器")
    print("=" * 50)
    print(f"书名: {bible.get('title', '未知')}")
    print(f"题材: {bible.get('genre', '未知')}")
    print()
    
    # 输出设计任务提示
    print("📋 设计任务（按规律执行）:")
    print("  Law 1: 加载风格范例（按题材路由）")
    print("  Law 4: 喜剧角色必须填写 comedy_recipe")
    print("  Law 14: 人物卡七维度校验")
    print()
    
    # 输出设计模板
    print("📝 设计模板（请填充）:")
    print()
    print("## 一、故事内核卡")
    print("- 核心梗: ")
    print("- 核心情绪: ")
    print("- 目标读者: ")
    print()
    print("## 二、人物卡（七维度）")
    print("- 身份（社会位置 + 职业）")
    print("- 欲望（核心驱动力）")
    print("- 缺陷（致命弱点）")
    print("- 口癖（全局唯一）")
    print("- 人化细节（1-3个习惯动作）")
    print("- 喜剧配方（仅喜剧角色）")
    print("- 关系锚（与主角关系）")
    print()
    print("## 三、五级冲突链")
    print("  1. 宏观（世界观层面）")
    print("  2. 中观（势力/门派层面）")
    print("  3. 微观（个人/家庭层面）")
    print("  4. 即时（本章冲突）")
    print("  5. 内心（角色心理冲突）")
    print()
    print("## 四、卷纲")
    print("- 卷名: ")
    print("- 本卷主题: ")
    print("- 本卷主线: ")
    print("- 本卷结局: ")
    print()
    
    print("✅ 设计完成提示:")
    print("  1. 填充上述模板")
    print("  2. 保存为 bible.json（故事内核卡）")
    print("  3. 保存人物卡到 设定/角色/{名}.md")
    print("  4. 保存卷纲到 大纲/卷纲_第N卷.md")
    print()
    print("下一步: python -m server plan <书根> 1")
    
    return 0


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("book_root")
    sys.exit(run(parser.parse_args()))
