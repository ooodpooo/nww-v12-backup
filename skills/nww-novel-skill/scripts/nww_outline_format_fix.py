#!/usr/bin/env python3
"""nww_outline_format_fix.py — 存量细纲 voice 锚点归一 CLI（C-OUTLINE-FIX@1）

把兼容形态 `- **出场角色voice锚点**：`（可带缩进）确定性归一为权威形态
`## 出场角色voice锚点`，供 packet builder 的可解析条目稳定产出。

规则（严格确定性，零剧情改写）：
  - 只改行前缀/位置与子项缩进，不修改任何角色条目文字、不丢弃子项；
  - 块内已有权威标题 → 不动该块（幂等）；仍存在的兼容行记 noop-duplicate；
  - 多章合一细纲按 `#{1,3} 第N章` 章块隔离，--all 逐块处理；
  - 写盘前备份 `细纲文件名.bak_fmtfix_<unix_ts>`；--dry-run 零写入、零备份。

用法:
  python3 nww_outline_format_fix.py --book <book_dir> --chapter N [--all] [--dry-run]

退出码:
  0 成功（含"无需修改"） / 2 参数或环境错误 / 3 细纲存在但无第N章块

stdout（单行 JSON）:
  {"ok":bool,"book":str,"chapter":int,"changed":bool,
   "actions":[{"block":int,"from":"field","to":"heading|noop-duplicate","chars":int}],
   "backup":str|null,"dry_run":bool}

`actions[].chars` 为本次保留的 voice 段正文字符数（含归一后的子项行）。
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

OUTLINE_DIR = "大纲"

# 章块起点（与 nww_packet_builder_v2._RE_CHAPTER_BLOCK 一致）
_RE_CHAPTER = re.compile(r"^#{1,3}\s*第\s*(\d+)\s*章\b", re.M)
# 权威标题（与 builder 的 voice 标题识别一致）
_RE_HEADING = re.compile(r"^#{2,4}\s*出场角色[^\n]*voice\s*锚点.*$", re.M)
# 兼容字段行：允许缩进（实测第 2/3 章嵌在「人物关系和出场顺序」子项中）。
_RE_FIELD = re.compile(
    r"^([ \t]*)([-*])[ \t]*\*{0,2}出场角色[ \t]*voice[ \t]*锚点\*{0,2}[ \t]*[：:][ \t]*(.*)$",
    re.M)
_RE_BULLET = re.compile(r"^([ \t]*)[-*][ \t]*(.*)$")

_MD_SUFFIXES = (".md",)


def _indent_width(s: str) -> int:
    """缩进宽度（tab 按 4 空格折算）。"""
    m = re.match(r"[ \t]*", s)
    return len((m.group(0) if m else "").replace("\t", "    "))


def _usage() -> str:
    return (
        "用法: python3 nww_outline_format_fix.py --book <book_dir> --chapter N "
        "[--all] [--dry-run]"
    )


def _parse_args(argv: list[str]) -> tuple[str, int, bool, bool]:
    """解析 CLI 参数；参数错误直接返回给 main 统一报错（不抛 argparse 的 usage 噪声）。"""
    book = ""
    chapter: int | None = None
    all_blocks = False
    dry_run = False
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-h", "--help"):
            print(_usage())
            sys.exit(0)
        if arg == "--book":
            if i + 1 >= len(argv):
                raise ValueError("--book 缺少取值")
            book = argv[i + 1]
            i += 2
            continue
        if arg == "--chapter":
            if i + 1 >= len(argv):
                raise ValueError("--chapter 缺少取值")
            try:
                chapter = int(argv[i + 1])
            except ValueError as exc:
                raise ValueError(f"--chapter 需要整数: {argv[i + 1]}") from exc
            i += 2
            continue
        if arg == "--all":
            all_blocks = True
            i += 1
            continue
        if arg == "--dry-run":
            dry_run = True
            i += 1
            continue
        raise ValueError(f"未知参数: {arg}")
    if not book:
        raise ValueError("缺少 --book")
    if chapter is None:
        raise ValueError("缺少 --chapter")
    if chapter <= 0:
        raise ValueError("--chapter 必须为正整数")
    return book, chapter, all_blocks, dry_run


def _is_backup(name: str) -> bool:
    return ".bak" in name


def _find_outline(book: Path, chapter_id: int) -> Path | None:
    """按 build_packet_v2 的候选名顺序定位细纲（跳过备份文件）。"""
    outline_dir = book / OUTLINE_DIR
    names = (
        f"细纲_第{chapter_id}章.md",
        f"细纲_第{chapter_id:02d}章.md",
        f"细纲_第{chapter_id:03d}章.md",
        f"细纲_第{chapter_id}章.json",
        f"outline_{chapter_id}.md",
    )
    for name in names:
        p = outline_dir / name
        if p.exists() and p.is_file() and not _is_backup(p.name):
            return p
    if outline_dir.is_dir():
        for pat in (
            f"细纲_第{chapter_id}章*.md",
            f"细纲_第{chapter_id:02d}章*.md",
            f"细纲_第{chapter_id:03d}章*.md",
        ):
            cands = [
                f for f in sorted(outline_dir.glob(pat))
                if f.is_file() and not f.name.startswith(".") and not _is_backup(f.name)
            ]
            if cands:
                return cands[0]
    return None


def _chapter_blocks(text: str) -> list[re.Match]:
    return list(_RE_CHAPTER.finditer(text))


def _collect_field_items(lines: list[str], idx: int) -> tuple[int, int, list[str]]:
    """抽取兼容字段行的归一化子项。

    返回 (field_indent, end_idx, items)。end_idx 为字段块结束（不含）的行号；
    items 为 `- 原文…` 行（同行冒号值置顶，随后是更深缩进的子项，原样保留文字）。
    空白行若位于子项之间/之后会被并入该块，以便原位替换时保持段落分隔。
    """
    fm = _RE_FIELD.match(lines[idx])
    if not fm:
        return 0, idx + 1, []
    field_indent = _indent_width(fm.group(1))
    inline = (fm.group(3) or "").strip()
    items: list[str] = []
    if inline:
        items.append("- " + inline)
    j = idx + 1
    seen_child = bool(items)
    while j < len(lines):
        line = lines[j]
        if not line.strip():
            # 子项之间/之后的空行并入块；字段后立即空行且无子项时也停止。
            if seen_child or items:
                j += 1
                continue
            break
        bm = _RE_BULLET.match(line)
        if bm and _indent_width(bm.group(1)) > field_indent:
            items.append("- " + bm.group(1).strip() + bm.group(2).strip())
            seen_child = True
            j += 1
            continue
        break
    return field_indent, j, items


def _process_block(lines: list[str], block_no: int) -> tuple[list[str], list[dict], bool]:
    """处理单个章块（或单章全文）；返回 (新行, actions, changed)。"""
    field_idx = [i for i, ln in enumerate(lines) if _RE_FIELD.match(ln)]
    if not field_idx:
        return lines, [], False

    has_heading = any(_RE_HEADING.search(ln) for ln in lines)
    if has_heading:
        actions = []
        for i in field_idx:
            _, _, items = _collect_field_items(lines, i)
            actions.append({
                "block": block_no,
                "from": "field",
                "to": "noop-duplicate",
                "chars": len("\n".join(items)),
            })
        return lines, actions, False

    # 同一块内多余兼容行的动作先按原行号计算（后续 new_lines 行号会变化）。
    extra_actions = []
    for i in field_idx[1:]:
        _, _, dup_items = _collect_field_items(lines, i)
        extra_actions.append({
            "block": block_no,
            "from": "field",
            "to": "noop-duplicate",
            "chars": len("\n".join(dup_items)),
        })

    first = field_idx[0]
    field_indent, end, items = _collect_field_items(lines, first)
    voice_block = ["## 出场角色voice锚点"] + items
    trailing_blank = bool(end > first and not lines[end - 1].strip())
    leading_blank = bool(first > 0 and lines[first - 1].strip())
    if field_indent == 0:
        new_lines = (lines[:first] + ([""] if leading_blank else []) + voice_block
                     + ([""] if trailing_blank else []) + lines[end:])
    else:
        # 嵌套字段行：原位提升为 H2 会吞掉后续兄弟 bullet（如「情节细化」），
        # 故仅调整位置——移到本章块末尾，语义不变且解析边界稳定（R2-AC2 允许位置调整）。
        rest = lines[:first] + lines[end:]
        while rest and not rest[-1].strip():
            rest.pop()
        new_lines = rest + [""] + voice_block + [""]

    actions = [{
        "block": block_no,
        "from": "field",
        "to": "heading",
        "chars": len("\n".join(items)),
    }]
    actions.extend(extra_actions)
    return new_lines, actions, True


def _process_text(text: str, chapter_id: int, all_blocks: bool) -> tuple[str, list[dict], bool, bool]:
    """按章块处理细纲全文；返回 (新文本, actions, changed, chapter_block_missing)。"""
    blocks = _chapter_blocks(text)
    if not blocks:
        # 单章形态：全文即一个块，block 号取 --chapter（零迁移）。
        lines = text.split("\n")
        new_lines, actions, changed = _process_block(lines, chapter_id)
        return "\n".join(new_lines), actions, changed, False

    if not any(int(m.group(1)) == chapter_id for m in blocks):
        return text, [], False, True

    segments: list[str] = []
    actions: list[dict] = []
    changed = False
    prev = 0
    for idx, m in enumerate(blocks):
        segments.append(text[prev:m.start()])
        end = blocks[idx + 1].start() if idx + 1 < len(blocks) else len(text)
        block_text = text[m.start():end]
        block_no = int(m.group(1))
        if all_blocks or block_no == chapter_id:
            new_block, block_actions, block_changed = _process_block(
                block_text.split("\n"), block_no)
            block_text = "\n".join(new_block)
            actions.extend(block_actions)
            changed = changed or block_changed
        segments.append(block_text)
        prev = end
    return "".join(segments), actions, changed, False


def _emit(ok: bool, book: str, chapter: int, changed: bool, actions: list[dict],
          backup: str | None, dry_run: bool, error: str | None = None) -> None:
    payload = {
        "ok": ok,
        "book": book,
        "chapter": chapter,
        "changed": changed,
        "actions": actions,
        "backup": backup,
        "dry_run": dry_run,
    }
    if error:
        payload["error"] = error
    print(json.dumps(payload, ensure_ascii=False))


def _backup_path(outline: Path) -> Path:
    ts = int(time.time())
    while True:
        p = outline.with_name(f"{outline.name}.bak_fmtfix_{ts}")
        if not p.exists():
            return p
        ts += 1


def main(argv: list[str]) -> int:
    try:
        book_str, chapter_id, all_blocks, dry_run = _parse_args(argv)
    except ValueError as exc:
        sys.stderr.write(f"[ERROR] {exc}\n{_usage()}\n")
        _emit(False, "", 0, False, [], None, False, str(exc))
        return 2

    book = Path(book_str)
    if not book.is_dir():
        msg = f"书目录不存在: {book_str}"
        sys.stderr.write(f"[ERROR] {msg}\n")
        _emit(False, book_str, chapter_id, False, [], None, dry_run, msg)
        return 2

    outline = _find_outline(book, chapter_id)
    if outline is None:
        msg = f"找不到第 {chapter_id} 章细纲"
        sys.stderr.write(f"[ERROR] {msg}\n")
        _emit(False, book_str, chapter_id, False, [], None, dry_run, msg)
        return 2
    if outline.suffix.lower() not in _MD_SUFFIXES:
        msg = f"仅支持 Markdown 细纲: {outline}"
        sys.stderr.write(f"[ERROR] {msg}\n")
        _emit(False, book_str, chapter_id, False, [], None, dry_run, msg)
        return 2

    try:
        text = outline.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"读取细纲失败: {exc}"
        sys.stderr.write(f"[ERROR] {msg}\n")
        _emit(False, book_str, chapter_id, False, [], None, dry_run, msg)
        return 2

    new_text, actions, changed, missing_block = _process_text(text, chapter_id, all_blocks)
    if missing_block:
        msg = f"细纲文件存在但无第{chapter_id}章块: {outline}"
        sys.stderr.write(f"[ERROR] {msg}\n")
        _emit(False, book_str, chapter_id, False, [], None, dry_run, msg)
        return 3

    backup: str | None = None
    if changed and not dry_run:
        bak = _backup_path(outline)
        try:
            bak.write_text(text, encoding="utf-8")
            outline.write_text(new_text, encoding="utf-8")
            backup = str(bak)
        except OSError as exc:
            msg = f"写入细纲失败: {exc}"
            sys.stderr.write(f"[ERROR] {msg}\n")
            _emit(False, book_str, chapter_id, False, actions, None, dry_run, msg)
            return 2

    _emit(True, book_str, chapter_id, changed, actions, backup, dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))