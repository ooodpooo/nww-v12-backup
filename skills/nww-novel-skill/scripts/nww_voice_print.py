#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nww_voice_print.py — 角色语言指纹可插拔模块（voice_print，2026-11-13）。

三档开关 ``NWW_VOICE_PRINT``：

* ``off``   关闭指纹注入，并从 character_contract 移除旧方言/口癖提示
* ``lite``  保留旧锚点、追加防滥用压制，不注入四维（存量书默认）
* ``smart`` 注入四维指纹（认知标尺 / 句法节奏 / 情绪变调 / 低频锚点）与状态触发约束

两个挂载点：

* 前置 ``build_character_contract(book, contract)`` —— Context 组装阶段改写 character_contract
* 后置 ``record_dialogue_marker_facts(project, chapter, prose_path)`` —— 事实卡生成阶段只读统计

纪律：不判决、不拦截、不写正文、不消耗 LLM；任何异常 fail-open。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

MODE_ENV = "NWW_VOICE_PRINT"
MODES = ("off", "lite", "smart")
DEFAULT_MODE = "lite"

# 旧方言/口癖提示的识别口径：只命中「表层词汇」描述，不碰句法/语用维度。
_LEGACY_MARKER_PATTERN = re.compile(
    r"口癖|口头禅|方言|侬|阿拉|晓得伐|沪语|乡音|本地话|腔调"
)

_LITE_SUPPRESSION = (
    "【语言指纹·防滥用压制】方言词与口头禅非必用、不设次数目标；"
    "只在角色放松、调侃、私下吐槽等情绪阈值被击穿时偶尔出现，"
    "正式场合与高压冲突改用标准利落的普通话。"
)

# 角色卡四维小节识别（标题可带后缀，如「语用策略与情绪变调」）。
_DIMENSION_SECTION_PATTERN = re.compile(
    r"^#+\s*(认知标尺|隐喻|句法节奏|语用|情绪变调|低频锚点)"
)
_DIMENSION_KEYS = ("cognitive", "syntax", "pragmatic", "marker")

_MARKER_FIELD_SPLIT_PATTERN = re.compile(r"[｜|]")
_MARKER_STATE_SPLIT_PATTERN = re.compile(r"[、，,；;]")
_MARKER_KEY_ALIASES = {
    "锚点": "marker",
    "可用状态": "enabled_states",
    "禁用状态": "disabled_states",
    "情绪功能": "emotional_function",
    "用法": "usage",
}

_DIALOGUE_PATTERN = re.compile(r'"([^"]*)"|“([^”]*)”|「([^」]*)」|『([^』]*)』')

# 角色卡未登记低频锚点时的兜底词表：只放强方言标记，避免普通话常用词误报。
_DEFAULT_DIALECT_MARKERS = ("侬", "阿拉", "晓得伐", "拎不清", "噱头", "吃相太难看")

_MAX_CONSTRAINT_CHARS = 200
_MAX_FACT_DETAIL_CHARS = 1600


def resolve_mode() -> str:
    """返回 off / lite / smart；未设置或非法值回落到 lite。"""
    raw = os.environ.get(MODE_ENV, "").strip().lower()
    if raw in MODES:
        return raw
    return DEFAULT_MODE


def _role_card_path(book: Path, name: str) -> Path:
    return Path(book) / "设定" / "角色" / f"{name}.md"


def _section_lines(text: str, header_pattern: str) -> list[str]:
    """取标题匹配后到下一个标题之间的非空行（不含标题行）。"""
    lines = text.splitlines()
    out: list[str] = []
    in_section = False
    for line in lines:
        stripped = line.strip()
        if re.match(header_pattern, stripped):
            in_section = True
            continue
        if in_section and re.match(r"^#+\s", stripped):
            in_section = False
            continue
        if in_section and stripped:
            out.append(stripped.lstrip("-").strip())
    return out


def extract_low_frequency_markers(card_text: str) -> list[dict[str, Any]]:
    """解析角色卡「低频锚点」小节。

    条目形态：
      - 锚点：拎不清｜可用状态：私下吐槽、对亲近同乡放松时｜禁用状态：正式谈判｜情绪功能：泄压｜用法：单独一句收尾
    """
    markers: list[dict[str, Any]] = []
    for line in _section_lines(card_text, r"^#+\s*低频锚点"):
        if "锚点" not in line:
            continue
        record: dict[str, Any] = {
            "marker": "",
            "enabled_states": [],
            "disabled_states": [],
            "emotional_function": "",
            "usage": "",
        }
        for part in _MARKER_FIELD_SPLIT_PATTERN.split(line):
            part = part.strip()
            if not part:
                continue
            matched = re.match(r"([^：:]+)[：:]\s*(.*)$", part)
            if not matched:
                continue
            key = _MARKER_KEY_ALIASES.get(matched.group(1).strip())
            if not key:
                continue
            value = matched.group(2).strip()
            if key in ("enabled_states", "disabled_states"):
                record[key] = [
                    item.strip()
                    for item in _MARKER_STATE_SPLIT_PATTERN.split(value)
                    if item.strip()
                ]
            else:
                record[key] = value
        if record["marker"]:
            markers.append(record)
    return markers


def extract_voice_dimensions(card_text: str) -> dict[str, list[str]]:
    """解析角色卡四维小节；缺失维度返回空列表。"""
    return {
        "cognitive": _section_lines(card_text, r"^#+\s*(认知标尺|隐喻)"),
        "syntax": _section_lines(card_text, r"^#+\s*句法节奏"),
        "pragmatic": _section_lines(card_text, r"^#+\s*(语用|情绪变调)"),
        "marker": _section_lines(card_text, r"^#+\s*低频锚点"),
    }


def _read_card(book: Path, name: str) -> str:
    path = _role_card_path(book, name)
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _is_legacy_marker_line(line: str) -> bool:
    return bool(_LEGACY_MARKER_PATTERN.search(str(line)))


def _strip_legacy_constraints(constraints: list[str]) -> list[str]:
    return [str(item) for item in constraints if not _is_legacy_marker_line(item)]


def _append_suppression(constraints: list[str]) -> list[str]:
    kept = [str(item) for item in constraints if str(item) != _LITE_SUPPRESSION]
    kept.append(_LITE_SUPPRESSION)
    return kept


def _format_marker(marker: dict[str, Any]) -> str:
    pieces = [f"锚点：{marker['marker']}"]
    if marker.get("enabled_states"):
        pieces.append("可用：" + "、".join(marker["enabled_states"]))
    if marker.get("disabled_states"):
        pieces.append("禁用于：" + "、".join(marker["disabled_states"]))
    if marker.get("emotional_function"):
        pieces.append("功能：" + str(marker["emotional_function"]))
    if marker.get("usage"):
        pieces.append("用法：" + str(marker["usage"]))
    text = "；".join(pieces)
    return text[:_MAX_CONSTRAINT_CHARS]


def _build_smart_constraints(dimensions: dict[str, list[str]],
                             markers: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    if dimensions.get("cognitive"):
        out.append("认知标尺：" + "；".join(dimensions["cognitive"][:2])[:_MAX_CONSTRAINT_CHARS])
    if dimensions.get("syntax"):
        out.append("句法节奏：" + "；".join(dimensions["syntax"][:2])[:_MAX_CONSTRAINT_CHARS])
    if dimensions.get("pragmatic"):
        out.append("情绪变调：" + "；".join(dimensions["pragmatic"][:3])[:_MAX_CONSTRAINT_CHARS])
    for marker in markers[:2]:
        out.append(_format_marker(marker))
    return out


def build_character_contract(book: Path, contract: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Context 组装阶段：按开关改写本章 character_contract。

    off   → 移除旧方言/口癖条目
    lite  → 保留旧条目并追加防滥用压制
    smart → 移除旧条目、注入四维与状态触发约束；角色卡无四维时退化为 lite
    """
    mode = resolve_mode()
    if not isinstance(contract, list):
        return contract

    rebuilt: list[dict[str, Any]] = []
    for character in contract:
        if not isinstance(character, dict):
            rebuilt.append(character)
            continue
        item = dict(character)
        constraints = list(item.get("voice_constraints") or [])
        if mode == "off":
            item["voice_constraints"] = _strip_legacy_constraints(constraints)
            item["voice_print_mode"] = "off"
        elif mode == "lite":
            item["voice_constraints"] = _append_suppression(constraints)
            item["voice_print_mode"] = "lite"
        else:
            card_text = _read_card(book, str(item.get("name") or ""))
            dimensions = extract_voice_dimensions(card_text)
            markers = extract_low_frequency_markers(card_text)
            smart = _build_smart_constraints(dimensions, markers)
            if smart:
                item["voice_constraints"] = _strip_legacy_constraints(constraints) + smart
                item["voice_print_mode"] = "smart"
            else:
                item["voice_constraints"] = _append_suppression(constraints)
                item["voice_print_mode"] = "smart_degraded"
        rebuilt.append(item)
    return rebuilt


def load_character_markers(book: Path) -> dict[str, list[dict[str, Any]]]:
    """读取全书角色卡的低频锚点；无登记时返回空 dict。"""
    card_dir = Path(book) / "设定" / "角色"
    result: dict[str, list[dict[str, Any]]] = {}
    if not card_dir.is_dir():
        return result
    for path in sorted(card_dir.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        markers = extract_low_frequency_markers(text)
        if markers:
            result[path.stem] = markers
    return result


def _flatten_marker_words(markers_by_character: dict[str, list[dict[str, Any]]]) -> list[str]:
    words: list[str] = []
    for markers in markers_by_character.values():
        for marker in markers:
            word = str(marker.get("marker") or "")
            if word and word not in words:
                words.append(word)
    return words


def _extract_dialogue_fragments(line: str) -> list[str]:
    fragments: list[str] = []
    for matched in _DIALOGUE_PATTERN.finditer(line):
        fragment = next((group for group in matched.groups() if group is not None), "")
        if fragment:
            fragments.append(fragment)
    return fragments


def collect_dialogue_marker_stats(prose_text: str,
                                  marker_words: list[str]) -> dict[str, Any]:
    """统计引号内对话中低频锚点的出现次数、段落序号与行号（只读陈述）。"""
    items: dict[str, dict[str, Any]] = {}
    paragraph_index = 1
    previous_blank = True

    for line_no, line in enumerate(prose_text.splitlines(), 1):
        if not line.strip():
            if not previous_blank:
                paragraph_index += 1
            previous_blank = True
            continue
        previous_blank = False

        fragments = _extract_dialogue_fragments(line)
        if not fragments:
            continue
        dialogue_text = "".join(fragments)
        for word in marker_words:
            count = dialogue_text.count(word)
            if not count:
                continue
            record = items.setdefault(word, {
                "marker": word,
                "count": 0,
                "paragraphs": [],
                "lines": [],
            })
            record["count"] += count
            if paragraph_index not in record["paragraphs"]:
                record["paragraphs"].append(paragraph_index)
            if line_no not in record["lines"]:
                record["lines"].append(line_no)

    ordered = sorted(items.values(), key=lambda row: (-row["count"], row["marker"]))
    return {
        "total": sum(row["count"] for row in ordered),
        "items": ordered,
    }


def format_marker_fact_detail(stats: dict[str, Any]) -> str:
    """把统计结果格式化为事实卡 detail（只陈述次数与位置）。"""
    pieces = [f"对话内低频锚点 {int(stats.get('total') or 0)} 次"]
    for row in stats.get("items") or []:
        paragraphs = "、".join(str(value) for value in row.get("paragraphs") or [])
        lines = "、".join(str(value) for value in row.get("lines") or [])
        pieces.append(
            f"{row['marker']} ×{row['count']}"
            f"（段 {paragraphs or '-'}；行 {lines or '-'}）"
        )
    return "｜".join(pieces)[:_MAX_FACT_DETAIL_CHARS]


def record_dialogue_marker_facts(project: Path, chapter: int,
                                 prose_path: Path) -> dict[str, Any]:
    """事实卡生成阶段：把方言/口癖词次数与位置写入只读事实卡。

    off 模式不记录；无登记锚点时退回内置方言词表；任何异常 fail-open。
    """
    mode = resolve_mode()
    if mode == "off":
        return {"status": "skip", "reason": "off"}

    try:
        prose_text = Path(prose_path).read_text(encoding="utf-8")
    except OSError as exc:
        return {"status": "skip", "reason": f"prose unreadable: {exc}"}

    markers_by_character = load_character_markers(project)
    marker_words = _flatten_marker_words(markers_by_character) or list(_DEFAULT_DIALECT_MARKERS)
    stats = collect_dialogue_marker_stats(prose_text, marker_words)
    if not stats["total"]:
        return {"status": "empty", "detail": "对话内低频锚点 0 次"}

    detail = format_marker_fact_detail(stats)
    try:
        import nww_gate_policy as gate
        gate.record(project, chapter, "voice_print/markers", detail,
                    source="voice_print", replace_code=True)
    except Exception as exc:  # noqa: BLE001 — 零阻断：事实卡写失败不改变动作结果
        print(f"[VOICE_PRINT] record skipped: {exc}", file=sys.stderr)
        return {"status": "error", "reason": str(exc)[:200], "detail": detail}

    return {"status": "recorded", "detail": detail, "stats": stats}