"""
NWW-V12 Server — 网文创作引擎核心模块

M1 工程化范围：
  new     — 新建项目
  design  — 故事设计（圣经 + 人物卡 + 冲突链 + 卷纲）
  plan    — 章纲规划（蓝图七要素）
  write   — 写作流水线（Web pipeline 薄封装）
  develop — 整体开发流程调度

使用方式：
  python -m server new <书名> [--genre <题材>]
  python -m server design <书根>
  python -m server plan <书根> <章节号>
  python -m server write <书根> <章节号>
  python -m server develop <书根>
"""
from __future__ import annotations

import sys
import argparse
from pathlib import Path

# 项目根目录（server/ 的上两级）
_SERVER_ROOT = Path(__file__).resolve().parents[1]
_SKILL_ROOT = _SERVER_ROOT / "skills" / "nww-novel-skill"
_KNOWLEDGE_DIR = _SKILL_ROOT / "knowledge"

# 确保 skill 在 Python 路径中
sys.path.insert(0, str(_SKILL_ROOT))


def main():
    parser = argparse.ArgumentParser(
        prog="server",
        description="NWW-V12 网文创作引擎",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # ── new ──
    new_parser = subparsers.add_parser("new", help="新建项目")
    new_parser.add_argument("title", help="书名")
    new_parser.add_argument("--genre", "-g", default="", help="题材（如：玄幻、都市、诡异）")
    new_parser.add_argument("--synopsis", "-s", default="", help="简介")
    new_parser.add_argument("--output", "-o", default="projects", help="输出目录")

    # ── design ──
    design_parser = subparsers.add_parser("design", help="故事设计")
    design_parser.add_argument("book_root", help="书项目根目录")

    # ── plan ──
    plan_parser = subparsers.add_parser("plan", help="章纲规划")
    plan_parser.add_argument("book_root", help="书项目根目录")
    plan_parser.add_argument("chapter_id", type=int, help="章节号")
    plan_parser.add_argument("--outline", help="细纲文件路径（可选）")

    # ── write ──
    write_parser = subparsers.add_parser("write", help="写作流水线（与 Web 端同一套 pipeline）")
    write_parser.add_argument("book_root", help="书项目根目录")
    write_parser.add_argument("chapter_id", type=int, help="章节号")
    write_parser.add_argument("--dry-run", action="store_true", help="只生成 Packet，不写正文")

    # ── develop ──
    develop_parser = subparsers.add_parser("develop", help="整体开发流程")
    develop_parser.add_argument("book_root", help="书项目根目录")

    args = parser.parse_args()

    # 路由到对应模块（透传退出码：失败命令返回非 0）
    if args.command == "new":
        from server.new import run as run_new
        sys.exit(run_new(args))
    elif args.command == "design":
        from server.design import run as run_design
        sys.exit(run_design(args))
    elif args.command == "plan":
        from server.plan import run as run_plan
        sys.exit(run_plan(args))
    elif args.command == "write":
        from server.write import run as run_write
        sys.exit(run_write(args))
    elif args.command == "develop":
        from server.develop import run as run_develop
        sys.exit(run_develop(args))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
