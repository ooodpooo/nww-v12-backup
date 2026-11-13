# -*- coding: utf-8 -*-
"""NWW delta audit helper and regex extractors."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, List, Set
from nww_track_commit import resolve_chapter_file

_POWER_GAIN_RE = re.compile(r"突破|晋升|结丹|筑基|领悟|觉醒|参透|习得|大成|圆满|进阶|升阶|开窍|结婴|化神|入道|通玄")
_DEATH_RE = re.compile(r"身陨|陨落|阵亡|毙命|断气|死于|斩杀|伏诛|咽气|气绝|暴毙|命丧|身亡")
_INJURY_RE = re.compile(r"重伤|咳血|负伤|吐血|骨折|受损|反噬|重创|挂彩|喋血|力竭")
_FORESHADOW_ID_RE = re.compile(r"\b(?:F\d+|FS-\d+|FSH-\d+)\b|[【\[](?:F\d+|FS-\d+|FSH-\d+)[\]】]")


def _load_prose(project: Path, chapter: int) -> str:
    p = resolve_chapter_file(project, chapter)
    if p and p.is_file():
        try:
            return p.read_text(encoding="utf-8")
        except Exception:
            return ""
    return ""


def _tracked_names(project: Path, state: dict) -> Set[str]:
    names = set((state.get("characters") or {}).keys())
    char_dir = project / "设定" / "角色"
    if char_dir.is_dir():
        for f in char_dir.glob("*.md"):
            cname = f.stem.replace("角色卡_", "").replace("人物卡_", "").strip()
            if cname:
                names.add(cname)
    return names


def _sentences(prose: str) -> List[str]:
    return [s.strip() for s in re.split(r"[。！？!?\n]", prose) if s.strip()]


# ---------- A-4 正文 ↔ delta 交叉审计（collect_delta_issues） ----------
# 2026-11-04：nww_track_commit.verify_delta_consistency 一直 import 本函数失败 →
# A-4 恒 fail-open 空转（日志 "nww_delta_audit.py 不可用"）。补齐确定性实现，
# 让这道闸真的跑起来。
#
# 边界（刻意保守，不制造新 BLOCK）：
#   - 只检测「正文里被追踪角色 × 强状态变化词同句，而 delta 完全没记」这一类漏写；
#   - 只产出 severity="warning"：A-4 设计上本来就只把审计结论交给调用方按 policy 裁决，
#     这里不擅自把历史可提交章判死（避免修复本身引入新卡点）；
#   - 纯确定性正则 + 集合判定，不调模型、不改任何文件。
def collect_delta_issues(project: Path, transaction: dict, next_state: dict) -> List[dict]:
    """返回 [{"severity","rule","description"}]；无问题返回 []。

    transaction 是 normalize_transaction 之后的 dict（delta 在 transaction["delta"]）。
    next_state 是 merge_transaction 之后的权威状态（提供被追踪角色名）。
    """
    try:
        chapter = int(transaction.get("chapter") or 0)
    except (TypeError, ValueError):
        return []
    if chapter <= 0:
        return []
    prose = _load_prose(project, chapter)
    if not prose:
        return []
    delta = transaction.get("delta") or {}
    if not isinstance(delta, dict):
        return []
    recorded_parts = [
        delta.get("character_changes"), delta.get("retired_characters"),
        delta.get("timeline_events"), delta.get("result"),
        delta.get("commitments"), delta.get("global_change"),
    ]
    recorded = " ".join(str(x) for x in recorded_parts if x)
    names = {n for n in _tracked_names(project, next_state or {}) if n and len(n) >= 2}
    if not names:
        return []
    issues: List[dict] = []
    seen = set()
    rules = (
        (_POWER_GAIN_RE, "境界能力跃迁"),
        (_DEATH_RE, "死亡退场"),
        (_INJURY_RE, "重伤代价"),
    )
    for sent in _sentences(prose):
        hit_name = next((n for n in names if n in sent), None)
        if hit_name is None:
            continue
        for regex, label in rules:
            m = regex.search(sent)
            if m is None:
                continue
            key = (hit_name, label, m.group())
            if key in seen:
                continue
            seen.add(key)
            # delta 里出现该角色名即视为已登记（不强求同词，自然语言改写不算漏写）
            if hit_name in recorded:
                continue
            issues.append({
                "severity": "warning",
                "rule": f"A4-{label}",
                "description": (
                    f"第{chapter}章正文出现「{hit_name}」的{label}（{m.group()}），"
                    f"但 delta 未登记该角色的状态变化：{sent[:80]}"
                ),
            })
    return issues
