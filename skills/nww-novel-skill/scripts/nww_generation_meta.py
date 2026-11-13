#!/usr/bin/env python3
"""
nww_generation_meta.py — 章节归档脚本（Law 8）

职责：
  1. 记录每章生成的元数据（规律版本、范例来源、温度参数）
  2. 保证每章可精确复现
  3. 支持后续回归测试

输出：
  {book_root}/追踪/generation_meta/ch{NNNN}.json
"""

import json
import sys
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional

# 规律库路径
LAW_FILE = Path(__file__).parent.parent / "knowledge" / "laws.yaml"
SCHEMA_FILE = Path(__file__).parent.parent / "knowledge" / "character_card_schema.yaml"
BLUEPRINT_SCHEMA = Path(__file__).parent.parent / "knowledge" / "outline_blueprint_schema.yaml"

def load_law_version() -> str:
    """加载规律库版本号"""
    if LAW_FILE.exists():
        content = LAW_FILE.read_text(encoding="utf-8")
        # 简单提取版本号（第一行的 version 字段）
        match = __import__('re').search(r'version:\s*"([^"]+)"', content)
        if match:
            return match.group(1)
    return "unknown"

def load_schema_version() -> str:
    """加载 Schema 版本号"""
    versions = []
    for schema_file in [SCHEMA_FILE, BLUEPRINT_SCHEMA]:
        if schema_file.exists():
            content = schema_file.read_text(encoding="utf-8")
            match = __import__('re').search(r'Version:\s*(\d+\.\d+\.\d+)', content)
            if match:
                versions.append(match.group(1))
    return "+".join(versions) if versions else "unknown"

def generate_meta(
    book_root: str,
    chapter_id: int,
    blueprint_path: str,
    prose_path: str,
    word_count: int,
    model_used: str,
    temperature: float,
    law_version: str,
    schema_version: str,
    style_exemplar: Optional[str] = None,
    mechanical_check_passed: bool = True,
    mechanical_issues: list = None,
) -> Dict[str, Any]:
    """生成章节元数据"""
    
    meta = {
        "chapter_id": chapter_id,
        "generated_at": datetime.now().isoformat(),
        "law_version": law_version,
        "schema_version": schema_version,
        "model_used": model_used,
        "temperature": temperature,
        "word_count": word_count,
        "files": {
            "blueprint": blueprint_path,
            "prose": prose_path,
        },
        "quality": {
            "mechanical_check_passed": mechanical_check_passed,
            "mechanical_issues_count": len(mechanical_issues or []),
        }
    }
    
    # 可选字段
    if style_exemplar:
        meta["style_exemplar"] = style_exemplar
    
    if mechanical_issues:
        meta["quality"]["mechanical_issues"] = mechanical_issues
    
    return meta

def save_meta(book_root: str, meta: Dict[str, Any]) -> Path:
    """保存元数据到追踪目录"""
    tracking_dir = Path(book_root) / "追踪"
    meta_dir = tracking_dir / "generation_meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    
    filepath = meta_dir / f"ch{meta['chapter_id']:04d}.json"
    filepath.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    
    return filepath

def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="NWW-V12 章节归档脚本")
    parser.add_argument("book_root", help="书项目根目录")
    parser.add_argument("chapter_id", type=int, help="章节号")
    parser.add_argument("--blueprint", required=True, help="蓝图文件路径")
    parser.add_argument("--prose", required=True, help="正文文件路径")
    parser.add_argument("--word-count", type=int, required=True, help="正文字数")
    parser.add_argument("--model", required=True, help="使用的模型")
    parser.add_argument("--temperature", type=float, required=True, help="温度参数")
    parser.add_argument("--style-exemplar", help="风格范例来源")
    parser.add_argument("--check-passed", action="store_true", help="机械自检是否通过")
    parser.add_argument("--issues", help="机械自检问题 JSON 文件（可选）")
    parser.add_argument("--output", help="输出路径（默认自动计算）")
    
    args = parser.parse_args()
    
    # 加载版本信息
    law_version = load_law_version()
    schema_version = load_schema_version()
    
    # 加载机械自检问题
    mechanical_issues = None
    if args.issues and Path(args.issues).exists():
        with open(args.issues, "r", encoding="utf-8") as f:
            mechanical_issues = json.load(f)
    
    # 生成元数据
    meta = generate_meta(
        book_root=args.book_root,
        chapter_id=args.chapter_id,
        blueprint_path=args.blueprint,
        prose_path=args.prose,
        word_count=args.word_count,
        model_used=args.model,
        temperature=args.temperature,
        law_version=law_version,
        schema_version=schema_version,
        style_exemplar=args.style_exemplar,
        mechanical_check_passed=args.check_passed,
        mechanical_issues=mechanical_issues,
    )
    
    # 保存
    if args.output:
        filepath = Path(args.output)
        filepath.parent.mkdir(parents=True, exist_ok=True)
    else:
        filepath = save_meta(args.book_root, meta)
    
    filepath.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ 元数据已保存: {filepath}")
    print(f"   规律版本: {law_version}")
    print(f"   Schema 版本: {schema_version}")
    print(f"   模型: {args.model} @ temp={args.temperature}")
    print(f"   字数: {args.word_count}")
    print(f"   机械自检: {'通过' if args.check_passed else '未通过'}")

if __name__ == "__main__":
    main()
