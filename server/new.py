"""
server.new — 新建项目模块

职责：
  1. 创建书项目目录结构
  2. 初始化 bible.json（世界观设定）
  3. 初始化追踪目录
  4. 部署 hooks + agents

布局规范（V12 统一）：
  projects/{book}/
    设定/
      角色/{角色名}.md
      世界观/{力量体系,地名}.md
    大纲/
      卷纲_第1卷.md
      细纲_第001章.md
    正文/
      第一卷/第001章_章名.md
    追踪/
      _tracking-state.json
      上下文.md
      角色状态/
      伏笔.md
      时间线/
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional


# 默认 bible.json 模板
DEFAULT_BIBLE = {
    "title": "",
    "genre": "",
    "synopsis": "",
    "platform": "番茄",  # 发布平台
    "wordcount_target": "2000-2500",  # 按平台规范
    "pov": "third",  # first / third
    "protagonist": {
        "name": "",
        "identity": "",
        "desire": "",
        "flaw": ""
    },
    "cost_system": {
        "enabled": True,
        "devices": [],
        "rule": ""
    },
    "creative": {
        "core_question": "",
        "emotional_engine": "",
        "novelty_pillars": [],
        "forbidden_patterns": []
    },
    "villain_down_beats": [],
    "signature_metaphor": {
        "seed": "",
        "mapping": ""
    },
    "mechanics": [],
    "era_constraints": {
        "protagonist_brought": []
    }
}


def run(args) -> int:
    """新建项目入口"""
    title = args.title.strip()
    if not title:
        print("❌ 书名不能为空")
        return 1

    genre = args.genre.strip() or "未知题材"
    synopsis = args.synopsis.strip()

    # 确定输出路径
    output_dir = Path(args.output)
    book_dir = output_dir / title

    if book_dir.exists():
        print(f"❌ 项目已存在: {book_dir}")
        return 1

    # 创建目录结构
    dirs = [
        book_dir / "设定" / "角色",
        book_dir / "设定" / "世界观",
        book_dir / "大纲",
        book_dir / "正文",
        book_dir / "追踪" / "角色状态",
        book_dir / "追踪" / "时间线",
        book_dir / "草稿",
    ]

    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)

    # 创建 bible.json
    bible = DEFAULT_BIBLE.copy()
    bible["title"] = title
    bible["genre"] = genre
    bible["synopsis"] = synopsis
    bible["protagonist"]["name"] = "主角"  # 默认，需手动修改

    bible_path = book_dir / "bible.json"
    bible_path.write_text(
        json.dumps(bible, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    # 创建初始追踪状态
    tracking_state = {
        "book_title": title,
        "chapter_types": {},
        "characters": {},
        "cost": {"count": 0, "devices": [], "rule": ""},
        "protagonist": {"cultivation": "未知"},
        "global": {
            "position": {
                "chapter": 0,
                "volume": 0,
                "epoch": 1,
                "core_conflict": "",
                "engine": {"type": "", "phase": ""},
                "theme": ""
            },
            "world": {
                "stage": "",
                "active_rules": [],
                "changes": []
            },
            "factions": {}
        },
        "dormant_characters": {},
        "quality_meta": {}
    }

    tracking_path = book_dir / "追踪" / "_tracking-state.json"
    tracking_path.write_text(
        json.dumps(tracking_state, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    # 创建占位文件
    (book_dir / "设定" / "世界观" / "力量体系.md").write_text(
        "# 力量体系\n\n## 等级划分\n\n## 核心规则\n",
        encoding="utf-8"
    )
    (book_dir / "追踪" / "上下文.md").write_text(
        "# 上下文\n\n> 本章关键信息摘要\n",
        encoding="utf-8"
    )
    (book_dir / "追踪" / "伏笔.md").write_text(
        "# 伏笔记录\n\n| 伏笔ID | 内容 | 埋设章 | 预期回收章 | 状态 |\n|--------|------|--------|------------|------|\n",
        encoding="utf-8"
    )
    (book_dir / "追踪" / "时间线.md").write_text(
        "# 时间线\n\n| 章号 | 时间 | 事件 |\n|------|------|------|\n",
        encoding="utf-8"
    )

    # 复制规律库
    knowledge_src = Path(__file__).parents[1] / "skills" / "nww-novel-skill" / "knowledge"
    knowledge_dst = book_dir / "knowledge"
    if knowledge_src.exists():
        import shutil
        shutil.copytree(knowledge_src, knowledge_dst, dirs_exist_ok=True)

    print(f"✅ 项目创建成功: {book_dir}")
    print(f"   书名: {title}")
    print(f"   题材: {genre}")
    print(f"   bible.json: {bible_path}")
    print(f"   追踪状态: {tracking_path}")
    print()
    print("下一步:")
    print(f"  python -m server design {book_dir}")
    print(f"  python -m server plan {book_dir} 1")
    print(f"  python -m server write {book_dir} 1")

    return 0


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("title")
    parser.add_argument("--genre", "-g", default="")
    parser.add_argument("--synopsis", "-s", default="")
    parser.add_argument("--output", "-o", default="projects")
    sys.exit(run(parser.parse_args()))
