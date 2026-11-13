"""
server.develop — 整体开发流程模块

职责：
  1. 加载项目状态
  2. 显示当前进度
  3. 提供下一步操作建议

使用方式：
  python -m server develop <书根>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, List, Optional


def run(args) -> int:
    """整体开发流程入口"""
    book_root = Path(args.book_root)
    
    print("🏗️  NWW-V12 开发控制台")
    print("=" * 60)
    print()
    
    # 检查项目结构
    checks = {
        "bible.json": book_root / "bible.json",
        "追踪状态": book_root / "追踪" / "_tracking-state.json",
        "规律库": book_root / "knowledge" / "laws.yaml",
        "人物卡 Schema": book_root / "knowledge" / "character_card_schema.yaml",
        "蓝图 Schema": book_root / "knowledge" / "outline_blueprint_schema.yaml",
    }
    
    print("📊 项目状态检查:")
    all_ok = True
    for name, path in checks.items():
        if path.exists():
            print(f"  ✅ {name}")
        else:
            print(f"  ❌ {name}")
            all_ok = False
    
    print()
    
    # 检查章节进度
    outline_dir = book_root / "大纲"
    prose_dir = book_root / "正文"
    
    outlines = sorted(outline_dir.glob("细纲_第*.md")) if outline_dir.exists() else []
    prose_files = sorted(prose_dir.rglob("*.md")) if prose_dir.exists() else []
    
    print(f"📚 章节进度:")
    print(f"  细纲: {len(outlines)} 章")
    print(f"  正文: {len(prose_files)} 章")
    print()
    
    # 显示下一步建议
    print("📋 下一步操作建议:")
    print()
    
    if not (book_root / "bible.json").exists():
        print("  1. 新建项目:")
        print(f"     python -m server new <书名> [--genre <题材>] [--synopsis <简介>]")
    elif not outlines:
        print("  1. 设计故事:")
        print(f"     python -m server design {book_root}")
        print()
        print("  2. 规划第一章:")
        print(f"     python -m server plan {book_root} 1")
    elif not prose_files:
        print("  1. 规划第一章:")
        print(f"     python -m server plan {book_root} 1")
        print()
        print("  2. 开始写作:")
        print(f"     python -m server write {book_root} 1")
    else:
        # 找出最后一章
        last_chapter = max([int(f.name.split('_')[0].replace('第', '').replace('章', '')) for f in prose_files]) if prose_files else 0
        next_chapter = last_chapter + 1
        
        print(f"  当前进度: 已完成第 {last_chapter} 章")
        print()
        print(f"  1. 规划下一章:")
        print(f"     python -m server plan {book_root} {next_chapter}")
        print()
        print(f"  2. 写作下一章:")
        print(f"     python -m server write {book_root} {next_chapter}")
    
    print()
    print("📖 参考文档:")
    print("  docs/02-系统方案v0.2-基于实验修订.md")
    print("  docs/03-Agent能力深化-完成报告.md")
    print()
    
    return 0


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("book_root")
    sys.exit(run(parser.parse_args()))
