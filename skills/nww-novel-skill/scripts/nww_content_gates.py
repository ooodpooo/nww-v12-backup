#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nww_content_gates.py — 内容存在性硬闸（Task 3 / C-P0-1）。

禁止「空则省略」：章号过阈后 global / 主角境界 / position 关键键缺失 → warning|block。
写前（packet）与 commit 共用同一套判定。
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

# C-STATE-EMPTY@1：空判的唯一实现在 nww_state_checks（guard.py 与内容门同源）。
# 符号名保留 _is_empty_global，既有调用点与外部导入不变。
from nww_state_checks import is_empty_global as _is_empty_global  # noqa: E402


class ContentGateError(ValueError):
    """写前/组装侧内容锚缺失。"""


@dataclass
class ContentGateVerdict:
    level: str  # ok|warning|block
    reasons: list[str] = field(default_factory=list)


def _protagonist_cultivation(state: dict, bible: dict | None = None) -> str | None:
    """返回已填境界字符串；显式主角追踪存在但境界空 → 返回 \"\"；无法识别主角 → None（跳过）。"""
    protag = state.get("protagonist")
    if isinstance(protag, dict):
        cult = protag.get("cultivation")
        if isinstance(cult, str) and cult.strip():
            return cult.strip()
        # 显式 protagonist 块存在：视为应有境界（空串表示缺失）
        name = str(protag.get("name") or "").strip()
        chars = state.get("characters") if isinstance(state.get("characters"), dict) else {}
        if name and name in chars and isinstance(chars[name], dict):
            c2 = chars[name].get("cultivation")
            if isinstance(c2, str) and c2.strip():
                return c2.strip()
        return "" if ("cultivation" in protag or name) else None

    # 无 state.protagonist：尝试 bible 识别主角名，仅当 characters 侧已有 cultivation 键时才强制
    name = None
    if isinstance(bible, dict):
        bp = bible.get("protagonist")
        if isinstance(bp, dict):
            name = str(bp.get("name") or "").strip()
            name = re.sub(r"^主角", "", name).strip() or name
        if not name:
            for ch in bible.get("characters") or []:
                if isinstance(ch, dict) and ch.get("role") == "主角":
                    name = str(ch.get("name") or "").strip()
                    break
    chars = state.get("characters") if isinstance(state.get("characters"), dict) else {}
    if name and name in chars and isinstance(chars[name], dict) and "cultivation" in chars[name]:
        c2 = chars[name].get("cultivation")
        return c2.strip() if isinstance(c2, str) and c2.strip() else ""
    return None


def _scale_key_thresholds(bible: dict | None) -> tuple[int, int] | None:
    """按 bible.scale 返回 (warn_ch, block_ch) 供 position.engine / world 检查；无 scale → None（跳过）。"""
    if not isinstance(bible, dict):
        return None
    scale = bible.get("scale")
    if not isinstance(scale, dict) or not scale:
        return None
    volumes = scale.get("volumes")
    try:
        vol_n = int(volumes) if volumes is not None else 1
    except (TypeError, ValueError):
        vol_n = 1
    tw = str(scale.get("total_words") or "")
    # 长篇（多卷或明确「万」字量）用更紧阈值；短篇放宽
    long_form = vol_n >= 2 or ("万" in tw) or bool(re.search(r"\d{3,}", tw))
    if long_form:
        return (5, 10)
    return (10, 20)


def _position_world_reasons(
    global_state: dict, chapter_id: int, bible: dict | None
) -> tuple[list[str], list[str]]:
    """返回 (warnings, blocks) for position.engine / world keys."""
    thresholds = _scale_key_thresholds(bible)
    if thresholds is None or _is_empty_global(global_state):
        return [], []
    warn_ch, block_ch = thresholds
    warnings: list[str] = []
    blocks: list[str] = []
    pos = global_state.get("position")
    world = global_state.get("world")
    missing: list[str] = []
    if not isinstance(pos, dict) or not pos.get("engine"):
        missing.append("global.position.engine")
    if not isinstance(world, dict) or not world:
        missing.append("global.world")
    if not missing:
        return [], []
    msg = "缺少关键键: " + ", ".join(missing) + f"（bible.scale 阈值 warn>{warn_ch}/block>{block_ch}）"
    if chapter_id > block_ch:
        blocks.append(msg)
    elif chapter_id > warn_ch:
        warnings.append(msg)
    return warnings, blocks


def check_global_presence(
    state: dict,
    chapter_id: int,
    *,
    bible: dict | None = None,
) -> ContentGateVerdict:
    """章号>3 且 global 空 → warning；>10 → block。

    另：主角境界缺失（characters[protagonist].cultivation 或等价）章>5 → block。
    position.engine / world 关键键按书 bible.scale 阈值。
    """
    reasons: list[str] = []
    level = "ok"
    global_state = state.get("global", {}) if isinstance(state, dict) else {}

    if _is_empty_global(global_state):
        if chapter_id > 10:
            level = "block"
            reasons.append("global 为空（章>10）：禁止省略全局状态锚，须先补 global_change / 初始化 global")
        elif chapter_id > 3:
            level = "warning"
            reasons.append("global 为空（章>3）：应尽快初始化 global，勿走空则省略路径")

    cult = _protagonist_cultivation(state if isinstance(state, dict) else {}, bible)
    if cult is not None and not cult and chapter_id > 5:
        level = "block"
        reasons.append("主角境界缺失（cultivation/境界）章>5 → block")

    warn_keys, block_keys = _position_world_reasons(
        global_state if isinstance(global_state, dict) else {}, chapter_id, bible
    )
    if block_keys:
        level = "block"
        reasons.extend(block_keys)
    elif warn_keys:
        if level == "ok":
            level = "warning"
        reasons.extend(warn_keys)

    return ContentGateVerdict(level=level, reasons=reasons)


def _packet_has_global_summary(packet: dict) -> bool:
    if not isinstance(packet, dict):
        return False
    for key in ("global_summary", "global_state", "global"):
        val = packet.get(key)
        if isinstance(val, str) and val.strip():
            return True
        if isinstance(val, dict) and val:
            return True
        if isinstance(val, list) and val:
            return True
    macro = packet.get("macro_context")
    if isinstance(macro, dict):
        gs = macro.get("global_state")
        if isinstance(gs, list) and gs:
            return True
        if isinstance(gs, str) and gs.strip():
            return True
    return False


def _packet_has_position(packet: dict) -> bool:
    if not isinstance(packet, dict):
        return False
    pos = packet.get("position")
    if isinstance(pos, dict) and pos:
        return True
    if isinstance(pos, str) and pos.strip():
        return True
    macro = packet.get("macro_context")
    if isinstance(macro, dict):
        cp = macro.get("current_position")
        if isinstance(cp, list) and cp:
            return True
        if isinstance(cp, str) and cp.strip():
            return True
    st = packet.get("scene_time")
    if isinstance(st, dict) and (st.get("story_time") or st.get("scene")):
        return True
    g = packet.get("global")
    if isinstance(g, dict):
        p = g.get("position")
        if isinstance(p, dict) and p:
            return True
    return False


def assert_packet_content_anchors(packet: dict, chapter_id: int) -> None:
    """缺 global 摘要 / position 时 raise ContentGateError（写前）。章≤10 放行。"""
    if chapter_id <= 10:
        return
    missing: list[str] = []
    if not _packet_has_global_summary(packet):
        missing.append("global 摘要")
    if not _packet_has_position(packet):
        missing.append("position")
    if missing:
        raise ContentGateError(
            f"[ContentGate BLOCK] 章{chapter_id} 写前缺内容锚: {', '.join(missing)}（空则 BLOCK，禁止省略）"
        )


def enforce_state_global_for_packet(state: dict, chapter_id: int) -> None:
    """packet 组装入口：state.global 空且 chapter_id>10 → raise ContentGateError。"""
    global_state = (state or {}).get("global", {}) if isinstance(state, dict) else {}
    if chapter_id > 10 and _is_empty_global(global_state):
        raise ContentGateError(
            f"[ContentGate BLOCK] 章{chapter_id} state.global 为空：禁止空则省略；"
            "须先补 global_change / 初始化 global 再写章"
        )
