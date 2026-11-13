"""书稿解析：把「每章一个 .md」的文件夹解析为有序章节列表。

标题优先级：
  1. 文件 frontmatter 的 title:
  2. 正文首个 # 标题行
  3. 文件名（去掉前导序号与扩展名）
排序：按文件名自然排序（001_..., 002_... / 第1章, 第2章 等）。
可选 manifest.json 覆盖顺序与标题。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class Chapter:
    file: str          # 相对书根的路径
    abs_path: Path
    title: str
    body: str


def _natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _parse_frontmatter(text: str):
    """返回 (frontmatter_dict, body)。无 frontmatter 时 dict 为空。"""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            fm_text = text[3:end].strip("\n")
            body = text[end + 4:].lstrip("\n")
            fm = {}
            for line in fm_text.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    fm[k.strip()] = v.strip()
            return fm, body
    return {}, text


def _extract_title(fm: dict, body: str, filename: str) -> str:
    if fm.get("title"):
        return fm["title"].strip().strip('"').strip("'")
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    # 回退到文件名：去掉前导序号
    name = re.sub(r"^\d+[_\-\s]*", "", filename)
    name = re.sub(r"\.(md|markdown)$", "", name, flags=re.IGNORECASE)
    return name


def read_book(book_root: Path, ext: str = ".md") -> List[Chapter]:
    """读取一本书（文件夹）为有序章节列表。忽略 manifest.json / README。"""
    if not book_root.exists() or not book_root.is_dir():
        raise FileNotFoundError(f"书根不存在或不是文件夹: {book_root}")
    files = sorted(
        [p for p in book_root.iterdir() if p.is_file() and p.suffix.lower() == ext.lower() and p.name.lower() != "manifest.json"],
        key=lambda p: _natural_key(p.name),
    )
    manifest = _load_manifest(book_root)
    chapters: List[Chapter] = []
    for p in files:
        text = p.read_text(encoding="utf-8")
        fm, body = _parse_frontmatter(text)
        title = _extract_title(fm, body, p.name)
        if manifest and p.name in manifest:
            title = manifest[p.name].get("title", title)
        chapters.append(Chapter(file=str(p.name), abs_path=p, title=title, body=body.strip()))
    # manifest 可强制重排
    if manifest and "_order" in manifest:
        order = manifest["_order"]
        by_name = {c.file: c for c in chapters}
        ordered = [by_name[n] for n in order if n in by_name]
        rest = [c for c in chapters if c.file not in set(order)]
        chapters = ordered + rest
    return chapters


def _load_manifest(book_root: Path) -> Optional[dict]:
    m = book_root / "manifest.json"
    if m.exists():
        try:
            return json.loads(m.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def find_book_folders(content_root: Path) -> List[Path]:
    """列出 content_root 下的书文件夹（含 .md 的子目录）。"""
    if not content_root.exists():
        return []
    out = []
    for p in sorted(content_root.iterdir(), key=lambda x: _natural_key(x.name)):
        if p.is_dir() and any(c.suffix.lower() == ".md" for c in p.iterdir() if c.is_file()):
            out.append(p)
    return out
