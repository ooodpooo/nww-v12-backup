#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nww_trope_guard.py — 剧情结构指纹门禁（P0 Step 1）
=====================================================
核心机制：CD冷却 + 变奏检查 + SimHash语义碰撞 + repeat_intent白名单豁免。

设计原则：
- 纯Python标准库，零外部依赖，毫秒级响应
- 模型无关，确定性脚本，fail-closed
- 不修改正文，只读取细纲指纹 + 读写追踪/桥段库

用法：
  python3 nww_trope_guard.py check --chapter <细纲路径或章号> --project <书项目根>
  python3 nww_trope_guard.py register --chapter <细纲路径或章号> --project <书项目根>
  python3 nww_trope_guard.py status --project <书项目根>
  python3 nww_trope_guard.py inspect --project <书项目根> [--window 20] [--threshold 3]

  指纹历史由 nww_track_commit.py 的 A-5b 在 commit 时自动 register 累积；
  无「补历史」命令——存量细纲补指纹需由 LLM 按 trope-taxonomy 枚举改写细纲后逐章 register。

存储结构：
  追踪/trope_registry/
    _index.json    # 轻量索引（每章一级标签+simhash，常驻秒级扫描）
    volume_{N}.json # 卷级详情（完整指纹）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

# ============================================================
# 一、枚举库（与 trope-taxonomy.md 同步，v1.0）
# ============================================================

ENUM_CONFLICT_PATTERN = {
    "PROVOKE_SLAP", "MORAL_DILEMMA", "RESOURCES_SNATCH", "BETRAYAL_REVENGE",
    "ASYMMETRIC_INFO_DEFENSE", "TRAP_ENTRAPMENT", "IDENTITY_THREAT", "POWER_STRUGGLE",
    "SURVIVAL_CRISIS", "TRUTH_UNCOVER", "DEAL_NEGOTIATION", "PUBLIC_HUMILIATION",
    "HIDDEN_ENEMY_STRIKE", "COMPETITION_ELIMINATION", "FORCED_CHOICE", "RESCUE_OPERATION",
    "CONSPIRACY_EXPOSE", "TERRITORY_DEFENSE", "REPUTATION_CRISIS", "FORBIDDEN_KNOWLEDGE",
    "TIME_PRESSURE", "ALLIANCE_FRAGMENT", "STATUS_QUO_EXPOSITION",
}

# STATUS_QUO_EXPOSITION = 日常交代/过渡/清点/世界观展开章，无主冲突，自动豁免代价阻断

ENUM_CORE_TROPE = {
    "AUCTION_BIDDING", "ARENA_DUEL", "BANQUET_CONFRONTATION", "FALSE_FLAG_INVESTIGATION",
    "SECRET_EXPOSURE", "UNDERCOVER_PROBE", "DESPERATE_ESCAPE", "TREASURE_HUNT",
    "DUNGEON_CLEAR", "MEDICAL_CURE", "ALCHEMY_CRAFT", "ACADEMY_EXAM",
    "RECRUITMENT_DRIVE", "ASSASSINATION_ATTEMPT", "HOSTAGE_SITUATION", "POISONING_MYSTERY",
    "INHERITANCE_DISPUTE", "MARRIAGE_ALLIANCE", "DIVINE_PUNISHMENT", "DEMON_POSSESSION",
    "TIME_LOOP", "MEMORY_LOSS", "BODY_SWAP", "PARALLEL_WORLD", "REGRESSION_RETURN",
    "SYSTEM_UNLOCK", "LEVEL_BREAKTHROUGH", "FORBIDDEN_TECHNIQUE", "ANCIENT_RUINS",
    "DRAGON_VEIN", "IMPERIAL_EXAMINATION", "COURT_INTRIGUE", "BANDIT_AMBUSH",
    "NATURAL_DISASTER", "PLAGUE_OUTBREAK", "BUSINESS_WAR", "STOCK_MANIPULATION",
    "CYBER_HACK", "COURTROOM_TRIAL", "JOURNALIST_INVESTIGATION", "ESPIONAGE_EXCHANGE",
    "MAFIA_WAR", "RACING_COMPETITION", "COOKING_CONTEST", "MUSIC_BATTLE",
}

ENUM_GOLDEN_FINGER_MODE = {
    "WEAKNESS_EXPOSE", "OVERCLOCK_BURST", "RULE_BYPASS", "RESOURCE_SYNTHESIS",
    "INFORMATION_EXTRACTION", "PROBABILITY_MANIPULATION", "TIME_REWIND", "SPACE_TELEPORT",
    "MIND_CONTROL", "HEALING_OVERDRIVE", "SUMMON_BEAST", "TRANSFORMATION_SHIFT",
    "ABSORPTION_DRAIN", "ILLUSION_CAST", "BARRIER_SHIELD", "ENCHANTMENT_BOOST",
    "PARALLEL_THINKING", "FUTURE_SIGHT",
}

ENUM_RESOLUTION_COST = {
    "BODY_DAMAGE", "IDENTITY_EXPOSED", "TRUMP_CARD_CONSUMED", "BOND_SEVERED",
    "LIFESPAN_DEDUCT", "RESOURCE_DEPLETION", "REPUTATION_DAMAGE", "TERRITORY_LOST",
    "ALLY_INJURED", "SECRET_LEAKED", "CURSE_INFLICTED", "NONE", "NOT_APPLICABLE",
}

# NONE = 有冲突解决但主角零代价白嫖（参与连续无代价检测）
# NOT_APPLICABLE = 本章无冲突解决事件（日常/过渡/清点），跳过所有代价相关检查，不参与连续无代价计数

ENUM_CHARACTER_DYNAMIC = {
    "NEW_RIVAL_SUSPECT", "SUDDEN_BETRAYAL", "HOSTILE_ALLIANCE", "SURVIVAL_MUTUAL_TRUST",
    "MENTOR_PROTEGE", "RIVAL_TO_ALLY", "ALLY_TO_RIVAL", "UNREQUITED_LOVE",
    "LOVE_HATE_TOGGLE", "FAMILY_REUNION", "FAMILY_BETRAYAL", "POWER_IMPOSITIONS",
    "UNDERLING_DEFECTION", "MYSTERIOUS_BENEFACTOR", "STALKER_OBSESSION",
    "RIVALRY_ESCALATION", "PEACE_TREATY", "THIRD_PARTY_MEDIATION",
}

ENUM_REPEAT_INTENT = {
    "none", "foreshadow_payoff", "theme_variation",
    "character_growth_parallel", "genre_convention",
}

# ============================================================
# 二、CD冷却配置（与 trope-taxonomy.md 第七节同步）
# ============================================================

CD_CONFIG = {
    "core_trope":           {"safe_cd": 70,  "label": "核心桥段"},
    "conflict_pattern":     {"safe_cd": 18,  "label": "冲突模式"},
    "golden_finger_mode":   {"safe_cd": 12,  "label": "金手指用法"},
    "resolution_cost":      {"safe_cd": 30,  "label": "解决代价"},
    "character_dynamic":    {"safe_cd": 20,  "label": "角色交互模式"},
}

# SimHash配置
SIMHASH_BITS = 64
SIMHASH_NGRAM = 3
SIMHASH_HAMMING_THRESHOLD = 3
SIMHASH_WINDOW = 100  # 近100章

# 组合硬阻断配置
COMBO_COLLISION_WINDOW = 30   # 近30章 [conflict_pattern + golden_finger_mode]
TRIPLE_COLLISION_WINDOW = 50  # 近50章 [conflict_pattern + core_trope + resolution_cost]
NO_COST_STREAK = 3            # 连续N章无代价
CHAR_DYNAMIC_WINDOW = 10      # 近10章
CHAR_DYNAMIC_THRESHOLD = 4    # 同一character_dynamic出现≥N次

# ============================================================
# 三、YAML Frontmatter 解析器（极简实现，零依赖）
# ============================================================

def parse_yaml_frontmatter(text: str) -> dict[str, Any]:
    """解析markdown文件顶部的YAML frontmatter（---包裹）。
    只支持：嵌套字典、字符串值（带/不带引号）、注释。
    返回空字典如果没有frontmatter。
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}

    # 找到结束的---
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        return {}

    yaml_lines = lines[1:end_idx]
    return _parse_yaml_dict(yaml_lines, 0)[0]


def _parse_yaml_dict(lines: list[str], indent: int) -> tuple[dict[str, Any], int]:
    """解析指定缩进级别的字典，返回(字典, 消耗的行数)。"""
    result: dict[str, Any] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # 空行或注释
        if not stripped or stripped.startswith("#"):
            i += 1
            continue

        # 计算缩进
        current_indent = len(line) - len(line.lstrip())
        if current_indent < indent:
            break
        if current_indent > indent:
            # 不应该发生（父级应该处理）
            i += 1
            continue

        # 解析 key: value
        if ":" not in stripped:
            i += 1
            continue

        colon_idx = stripped.index(":")
        key = stripped[:colon_idx].strip()
        value_part = stripped[colon_idx + 1:].strip()

        if value_part:
            # 内联值
            result[key] = _parse_yaml_value(value_part)
            i += 1
        else:
            # 嵌套字典
            sub_dict, consumed = _parse_yaml_dict(lines[i + 1:], indent + 2)
            result[key] = sub_dict
            i += 1 + consumed

    return result, i


def _parse_yaml_value(value: str) -> str:
    """解析YAML标量值，去除引号。"""
    value = value.strip()
    # 去除行尾注释（简单处理：#前有空格才是注释）
    if " #" in value:
        value = value[:value.index(" #")].strip()
    # 去除引号
    if (value.startswith('"') and value.endswith('"')) or \
       (value.startswith("'") and value.endswith("'")):
        value = value[1:-1]
    return value


# ============================================================
# 四、SimHash（64-bit，字符3-gram，零依赖）
# ============================================================

def _string_to_64bit_hash(s: str) -> int:
    """用md5的前8字节生成64位哈希。"""
    h = hashlib.md5(s.encode("utf-8")).digest()
    return int.from_bytes(h[:8], "big")


def compute_simhash(text: str) -> int:
    """计算64位SimHash。
    输入：core_plot_summary（核心情节一句话总结）
    预处理：去除标点空白，纯中文字符串
    特征：连续3字符子串（3-gram）
    """
    # 预处理：只保留中文字符和字母数字
    cleaned = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", text)
    if len(cleaned) < SIMHASH_NGRAM:
        return 0

    # 提取3-gram并统计
    vector = [0] * SIMHASH_BITS
    for i in range(len(cleaned) - SIMHASH_NGRAM + 1):
        gram = cleaned[i:i + SIMHASH_NGRAM]
        h = _string_to_64bit_hash(gram)
        for bit in range(SIMHASH_BITS):
            if h & (1 << bit):
                vector[bit] += 1
            else:
                vector[bit] -= 1

    # 生成SimHash
    result = 0
    for bit in range(SIMHASH_BITS):
        if vector[bit] > 0:
            result |= (1 << bit)
    return result


def hamming_distance(a: int, b: int) -> int:
    """计算两个64位整数的汉明距离。"""
    xor = a ^ b
    return bin(xor).count("1")


# ============================================================
# 五、存储管理（分区存储：_index.json + volume_{N}.json）
# ============================================================

class TropeRegistry:
    """桥段指纹库，分区存储。"""

    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.registry_dir = project_root / "追踪" / "trope_registry"
        self.index_path = self.registry_dir / "_index.json"

    def _ensure_dir(self) -> None:
        self.registry_dir.mkdir(parents=True, exist_ok=True)

    def load_index(self) -> dict[str, Any]:
        """加载轻量索引。不存在则返回空结构。"""
        if not self.index_path.exists():
            return {"schema_version": 1, "chapters": {}, "last_chapter": 0}
        try:
            with open(self.index_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {"schema_version": 1, "chapters": {}, "last_chapter": 0}

    def save_index(self, index: dict[str, Any]) -> None:
        """保存轻量索引。"""
        self._ensure_dir()
        with open(self.index_path, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)

    def load_volume(self, volume_id: int) -> dict[str, Any]:
        """加载卷级详情。不存在则返回空结构。"""
        path = self.registry_dir / f"volume_{volume_id}.json"
        if not path.exists():
            return {"schema_version": 1, "volume_id": volume_id, "chapters": {}}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {"schema_version": 1, "volume_id": volume_id, "chapters": {}}

    def save_volume(self, volume_id: int, data: dict[str, Any]) -> None:
        """保存卷级详情。"""
        self._ensure_dir()
        path = self.registry_dir / f"volume_{volume_id}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def get_recent_chapters(self, count: int, before_chapter: int | None = None) -> list[dict[str, Any]]:
        """获取最近N章的索引记录（按章号倒序）。
        before_chapter: 只获取章号小于此值的章（用于检查当前章之前的历史）。
        """
        index = self.load_index()
        chapters = []
        for ch_str, data in index.get("chapters", {}).items():
            ch_num = int(ch_str)
            if before_chapter is not None and ch_num >= before_chapter:
                continue
            chapters.append({"chapter": ch_num, **data})
        chapters.sort(key=lambda x: x["chapter"], reverse=True)
        return chapters[:count]

    def register(self, chapter: int, volume_id: int, fingerprint: dict[str, Any], simhash: int) -> None:
        """登记一章的指纹到索引和卷级详情。"""
        # 更新索引
        index = self.load_index()
        index["chapters"][str(chapter)] = {
            "volume_id": volume_id,
            "conflict_pattern": fingerprint.get("conflict_pattern", ""),
            "core_trope": fingerprint.get("core_trope", ""),
            "golden_finger_mode": fingerprint.get("golden_finger_mode", ""),
            "resolution_cost": fingerprint.get("resolution_cost", ""),
            "character_dynamic": fingerprint.get("character_dynamic", ""),
            "trope_variant": fingerprint.get("trope_variant", ""),
            "simhash": simhash,
            "repeat_intent": fingerprint.get("repeat_intent", "none"),
            "registered_at": _now_iso(),
        }
        if chapter > index.get("last_chapter", 0):
            index["last_chapter"] = chapter
        self.save_index(index)

        # 更新卷级详情
        vol = self.load_volume(volume_id)
        vol["chapters"][str(chapter)] = {
            "fingerprint": fingerprint,
            "simhash": simhash,
            "registered_at": _now_iso(),
        }
        self.save_volume(volume_id, vol)

    def is_registered(self, chapter: int) -> bool:
        """检查某章是否已登记。"""
        index = self.load_index()
        return str(chapter) in index.get("chapters", {})


def _now_iso() -> str:
    """返回当前时间ISO格式（不引入datetime依赖的复杂写法）。"""
    import datetime
    return datetime.datetime.now().isoformat(timespec="seconds")


# ============================================================
# 六、细纲解析
# ============================================================

def resolve_chapter_path(project_root: Path, chapter_arg: str) -> Path | None:
    """解析章号或路径为细纲文件路径。"""
    # 如果是已存在的路径，直接返回
    p = Path(chapter_arg)
    if p.is_file():
        return p

    # 尝试解析为章号
    m = re.search(r"(\d+)", chapter_arg)
    if not m:
        return None
    chapter_num = int(m.group(1))

    # 在大纲目录中查找
    outline_dir = project_root / "大纲"
    if not outline_dir.is_dir():
        return None

    # 匹配 细纲_第N章*.md（容忍前导零和标题后缀）
    for f in sorted(outline_dir.glob("细纲_第*章*.md")):
        fm = re.match(r"细纲_第0*(\d+)章", f.name)
        if fm and int(fm.group(1)) == chapter_num:
            return f
    return None


def extract_chapter_number(path: Path) -> int:
    """从细纲文件名提取章号。"""
    m = re.search(r"第0*(\d+)章", path.name)
    return int(m.group(1)) if m else 0


def extract_volume_id(project_root: Path, chapter: int) -> int:
    """根据章号推断卷号。
    优先从卷纲文件推断；简单策略：每50章一卷。
    """
    # 尝试从卷纲推断
    outline_dir = project_root / "大纲"
    if outline_dir.is_dir():
        for f in sorted(outline_dir.glob("卷纲_第*卷*.md")):
            vm = re.search(r"第0*(\d+)卷", f.name)
            if vm:
                # 简单策略：卷号 = (章号-1)//50 + 1
                pass
    # 默认：每50章一卷
    return (chapter - 1) // 50 + 1


def parse_fingerprint_from_outline(outline_path: Path) -> dict[str, Any]:
    """从细纲文件解析structural_fingerprint。
    返回完整的fingerprint字典，如果没有则返回空字典。
    """
    try:
        text = outline_path.read_text(encoding="utf-8")
    except OSError:
        return {}

    fm = parse_yaml_frontmatter(text)
    fp = fm.get("structural_fingerprint", {})
    if not isinstance(fp, dict):
        return {}
    return fp


# ============================================================
# 七、枚举校验
# ============================================================

def validate_enums(fingerprint: dict[str, Any]) -> list[dict[str, str]]:
    """校验指纹中的枚举值是否合法。返回错误列表。"""
    errors = []
    enum_checks = [
        ("conflict_pattern", ENUM_CONFLICT_PATTERN, "冲突模式"),
        ("core_trope", ENUM_CORE_TROPE, "核心桥段"),
        ("golden_finger_mode", ENUM_GOLDEN_FINGER_MODE, "金手指用法"),
        ("resolution_cost", ENUM_RESOLUTION_COST, "解决代价"),
        ("character_dynamic", ENUM_CHARACTER_DYNAMIC, "角色交互模式"),
    ]

    for field, enum_set, label in enum_checks:
        value = fingerprint.get(field, "")
        if not value:
            errors.append({
                "severity": "blocking",
                "rule": "枚举缺失",
                "field": field,
                "description": f"{label}（{field}）为空，细纲必须填写所有一级枚举标签",
                "suggestions": [
                    f"在细纲 YAML frontmatter 的 structural_fingerprint 中填写 {field} 字段",
                    f"可选值请参考 trope-taxonomy.md 中 {label} 的枚举列表（共{len(enum_set)}种）",
                ],
            })
        elif value not in enum_set:
            errors.append({
                "severity": "blocking",
                "rule": "枚举非法",
                "field": field,
                "description": f"{label}（{field}）值 '{value}' 不在枚举库中，请参考 trope-taxonomy.md 选择合法枚举值",
                "suggestions": [
                    f"将 {field} 的值改为 trope-taxonomy.md 中 {label} 部分列出的合法枚举值",
                    f"如确需新增枚举值，请先在 trope-taxonomy.md 中补充定义和正反例，再在本脚本 ENUM_{field.upper()} 集合中添加",
                    f"当前可选值（前10个）：{', '.join(sorted(enum_set)[:10])}",
                ],
            })

    # repeat_intent校验（可选字段）
    ri = fingerprint.get("repeat_intent", "none")
    if ri and ri not in ENUM_REPEAT_INTENT:
        errors.append({
            "severity": "blocking",
            "rule": "枚举非法",
            "field": "repeat_intent",
            "description": f"repeat_intent 值 '{ri}' 不合法，可选：{', '.join(sorted(ENUM_REPEAT_INTENT))}",
        })

    # core_plot_summary校验（用于SimHash）
    cps = fingerprint.get("core_plot_summary", "")
    if not cps:
        errors.append({
            "severity": "blocking",
            "rule": "字段缺失",
            "field": "core_plot_summary",
            "description": "核心情节一句话总结（core_plot_summary）为空，必须填写（≤80字），用于SimHash语义碰撞检测",
        })
    elif len(cps) > 200:
        errors.append({
            "severity": "advisory",
            "rule": "字段过长",
            "field": "core_plot_summary",
            "description": f"core_plot_summary 过长（{len(cps)}字），建议≤80字，过长会降低SimHash精度",
        })

    return errors


# ============================================================
# 八、CD冷却检查 + 变奏检查
# ============================================================

def check_cd_cooldown(
    fingerprint: dict[str, Any],
    chapter: int,
    registry: TropeRegistry,
) -> list[dict[str, Any]]:
    """检查各维度的CD冷却。返回finding列表。"""
    findings = []
    repeat_intent = fingerprint.get("repeat_intent", "none")
    exempt = repeat_intent != "none"

    # field → 枚举集合的映射，用于suggestions中的可选值数量
    field_enum_map = {
        "conflict_pattern": ENUM_CONFLICT_PATTERN,
        "core_trope": ENUM_CORE_TROPE,
        "golden_finger_mode": ENUM_GOLDEN_FINGER_MODE,
        "resolution_cost": ENUM_RESOLUTION_COST,
        "character_dynamic": ENUM_CHARACTER_DYNAMIC,
    }

    for field, config in CD_CONFIG.items():
        current_value = fingerprint.get(field, "")
        if not current_value:
            continue

        safe_cd = config["safe_cd"]
        label = config["label"]

        # 获取近2*safe_cd章的历史（足够覆盖冷却恢复期）
        recent = registry.get_recent_chapters(2 * safe_cd, before_chapter=chapter)

        # 找到最近一次使用同值的章
        last_use = None
        for rec in recent:
            if rec.get(field) == current_value:
                last_use = rec
                break

        if last_use is None:
            continue  # 历史中没有使用过，安全

        distance = chapter - last_use["chapter"]

        if distance < safe_cd:
            # 绝对红线期
            remaining = safe_cd - distance
            finding = {
                "severity": "blocking",
                "rule": "CD冷却未结束",
                "field": field,
                "value": current_value,
                "distance": distance,
                "last_chapter": last_use["chapter"],
                "description": (
                    f"{label} '{current_value}' 冷却中："
                    f"第{last_use['chapter']}章刚用过，距今仅{distance}章，"
                    f"安全CD为{safe_cd}章，还需{remaining}章。"
                ),
                "suggestions": [
                    f"更换 {field}：从 trope-taxonomy.md 中选择另一种{label}（当前库有{len(field_enum_map.get(field, []))}种可选）",
                    f"如确需复用 '{current_value}'，请将本章调整到第{chapter + remaining}章及以后使用",
                    f"如本章为伏笔回收/主题变奏等有意重复，可设置 repeat_intent 字段（foreshadow_payoff/theme_variation/character_growth_parallel/genre_convention），硬阻断将降级为软告警",
                ],
            }
            if exempt:
                finding["severity"] = "advisory"
                finding["description"] += f" [已豁免：repeat_intent={repeat_intent}]"
            findings.append(finding)

        elif distance < 2 * safe_cd:
            # 冷却恢复期：变奏检查
            variation_pass = False
            variation_reason = ""

            # 条件1：trope_variant不同（仅对core_trope检查）
            if field == "core_trope":
                current_variant = fingerprint.get("trope_variant", "").strip()
                last_variant = last_use.get("trope_variant", "").strip()
                if current_variant and last_variant and current_variant != last_variant:
                    variation_pass = True
                    variation_reason = f"变奏不同（本次：{current_variant[:30]}；上次：{last_variant[:30]}）"
                elif not current_variant:
                    variation_reason = "trope_variant为空，无法证明变奏"
                elif current_variant == last_variant:
                    variation_reason = f"trope_variant与上次完全相同（{current_variant[:30]}）"

            # 条件2：golden_finger_mode不同（对所有维度都检查解法变化）
            current_gfm = fingerprint.get("golden_finger_mode", "")
            last_gfm = last_use.get("golden_finger_mode", "")
            if current_gfm and last_gfm and current_gfm != last_gfm:
                variation_pass = True
                if variation_reason:
                    variation_reason += "；"
                variation_reason += f"解法不同（本次：{current_gfm}；上次：{last_gfm}）"

            if not variation_pass:
                finding = {
                    "severity": "blocking",
                    "rule": "冷却恢复期变奏检查未通过",
                    "field": field,
                    "value": current_value,
                    "distance": distance,
                    "last_chapter": last_use["chapter"],
                    "description": (
                        f"{label} '{current_value}' 处于冷却恢复期（距上次{distance}章，"
                        f"安全CD={safe_cd}，恢复期至{2*safe_cd}章）。"
                        f"允许复用但必须变奏：{variation_reason}。"
                        f"请调整 trope_variant（切入角度/角色反转/尺度升级）或更换 golden_finger_mode（解法）。"
                    ),
                    "suggestions": [
                        "修改 trope_variant：与上次使用时的切入角度实质性不同（角色反转：从买家变卖家/从被动变做局；逆向款：以为捡漏结果是陷阱；尺度升级：从市井暗市到跨界神魔巨头拍卖）",
                        "更换 golden_finger_mode：用不同的解法解决同一桥段（如上次用 WEAKNESS_EXPOSE 破绽解析，这次用 OVERCLOCK_BURST 过载爆发或 RULE_BYPASS 规则绕过）",
                        "如无法变奏，可将本章调整到第{chapter + (2*safe_cd - distance)}章及以后（记忆遗忘期，完全解冻）",
                    ],
                }
                if exempt:
                    finding["severity"] = "advisory"
                    finding["description"] += f" [已豁免：repeat_intent={repeat_intent}]"
                findings.append(finding)
            else:
                findings.append({
                    "severity": "advisory",
                    "rule": "冷却恢复期合法变奏",
                    "field": field,
                    "value": current_value,
                    "distance": distance,
                    "last_chapter": last_use["chapter"],
                    "description": f"{label} '{current_value}' 合理复用（距上次{distance}章），变奏检查通过：{variation_reason}",
                })

    return findings


# ============================================================
# 九、组合硬阻断检查
# ============================================================

def check_combo_collisions(
    fingerprint: dict[str, Any],
    chapter: int,
    registry: TropeRegistry,
) -> list[dict[str, Any]]:
    """检查组合硬阻断规则。"""
    findings = []
    repeat_intent = fingerprint.get("repeat_intent", "none")
    exempt = repeat_intent != "none"

    cp = fingerprint.get("conflict_pattern", "")
    gfm = fingerprint.get("golden_finger_mode", "")
    ct = fingerprint.get("core_trope", "")
    rc = fingerprint.get("resolution_cost", "")
    cd = fingerprint.get("character_dynamic", "")

    # 规则1：近30章 [conflict_pattern + golden_finger_mode] 完全一致
    if cp and gfm:
        recent = registry.get_recent_chapters(COMBO_COLLISION_WINDOW, before_chapter=chapter)
        for rec in recent:
            if rec.get("conflict_pattern") == cp and rec.get("golden_finger_mode") == gfm:
                finding = {
                    "severity": "blocking",
                    "rule": "组合碰撞（冲突模式+解法）",
                    "distance": chapter - rec["chapter"],
                    "last_chapter": rec["chapter"],
                    "description": (
                        f"近{COMBO_COLLISION_WINDOW}章内，冲突模式 '{cp}' + 解法 '{gfm}' "
                        f"与第{rec['chapter']}章完全一致——本质是同一章的换皮。"
                        f"请更换冲突模式或解法。"
                    ),
                    "suggestions": [
                        f"更换 conflict_pattern：从 trope-taxonomy.md 中选择另一种冲突模式（当前库有{len(ENUM_CONFLICT_PATTERN)}种可选）",
                        f"更换 golden_finger_mode：用不同的解法（当前库有{len(ENUM_GOLDEN_FINGER_MODE)}种可选）",
                        "如确为有意重复，设置 repeat_intent 字段，硬阻断将降级为软告警",
                    ],
                }
                if exempt:
                    finding["severity"] = "advisory"
                    finding["description"] += f" [已豁免：repeat_intent={repeat_intent}]"
                findings.append(finding)
                break

    # 规则2：近50章 [conflict_pattern + core_trope + resolution_cost] 完全一致
    if cp and ct and rc:
        recent = registry.get_recent_chapters(TRIPLE_COLLISION_WINDOW, before_chapter=chapter)
        for rec in recent:
            if (rec.get("conflict_pattern") == cp and
                    rec.get("core_trope") == ct and
                    rec.get("resolution_cost") == rc):
                finding = {
                    "severity": "blocking",
                    "rule": "三维雷同（冲突+桥段+代价）",
                    "distance": chapter - rec["chapter"],
                    "last_chapter": rec["chapter"],
                    "description": (
                        f"近{TRIPLE_COLLISION_WINDOW}章内，冲突 '{cp}' + 桥段 '{ct}' + 代价 '{rc}' "
                        f"与第{rec['chapter']}章完全一致——读者会强烈感到'这章我看过'。"
                        f"请至少更换其中一个维度。"
                    ),
                    "suggestions": [
                        "更换 conflict_pattern（冲突模式）：从 trope-taxonomy.md 选择另一种",
                        "更换 core_trope（核心桥段）：从 trope-taxonomy.md 选择另一种桥段",
                        "更换 resolution_cost（解决代价）：让主角付出不同类型的代价",
                        "如确为有意重复，设置 repeat_intent 字段，硬阻断将降级为软告警",
                    ],
                }
                if exempt:
                    finding["severity"] = "advisory"
                    finding["description"] += f" [已豁免：repeat_intent={repeat_intent}]"
                findings.append(finding)
                break

    # 规则3：连续N章无代价（升级为BLOCKING）
    # 精确触发条件：
    #   - 当前章 resolution_cost == NONE（有冲突但白嫖）
    #   - 当前章 conflict_pattern != STATUS_QUO_EXPOSITION（非日常交代章）
    #   - 前两章 resolution_cost 都 == NONE（不含 NOT_APPLICABLE，NOT_APPLICABLE 不参与计数）
    if rc == "NONE" and cp != "STATUS_QUO_EXPOSITION":
        recent = registry.get_recent_chapters(NO_COST_STREAK, before_chapter=chapter)
        streak = 0
        for rec in recent:
            rec_rc = rec.get("resolution_cost", "")
            if rec_rc == "NONE":
                streak += 1
            elif rec_rc == "NOT_APPLICABLE":
                # NOT_APPLICABLE 不参与计数，也不中断计数（跳过）
                continue
            else:
                break
        if streak >= NO_COST_STREAK - 1:  # 当前章是第N章
            findings.append({
                "severity": "blocking",
                "rule": "连续无代价",
                "streak": streak + 1,
                "description": (
                    f"本章将是连续第{streak + 1}章有冲突但零代价白嫖（resolution_cost=NONE）。"
                    f"无痛解题是爽点贬值的核心来源——主角每次遇到麻烦都像踩死蚂蚁，读者会迅速疲劳。"
                    f"（日常交代章 resolution_cost=NOT_APPLICABLE 不参与计数，已自动排除）"
                ),
                "suggestions": [
                    "为本章主角安排具体代价：BODY_DAMAGE（重伤/经脉受损）、TRUMP_CARD_CONSUMED（底牌/法宝消耗）、IDENTITY_EXPOSED（身份/实力暴露）、LIFESPAN_DEDUCT（寿元损耗）",
                    "或引入次生危机：解决当前冲突后惹上新仇家/暴露弱点/资源耗尽，为下一章制造压力",
                    "如本章确为日常过渡/战利品清点/功法领悟，请将 conflict_pattern 改为 STATUS_QUO_EXPOSITION 且 resolution_cost 改为 NOT_APPLICABLE，系统将自动豁免",
                ],
            })

    # 规则4：近10章同一character_dynamic出现≥N次
    if cd:
        recent = registry.get_recent_chapters(CHAR_DYNAMIC_WINDOW, before_chapter=chapter)
        count = sum(1 for rec in recent if rec.get("character_dynamic") == cd)
        if count >= CHAR_DYNAMIC_THRESHOLD - 1:  # 当前章是第N次
            findings.append({
                "severity": "advisory",
                "rule": "角色交互模式固化",
                "count": count + 1,
                "window": CHAR_DYNAMIC_WINDOW,
                "description": (
                    f"近{CHAR_DYNAMIC_WINDOW}章内，角色交互模式 '{cd}' 将出现第{count + 1}次"
                    f"（阈值{CHAR_DYNAMIC_THRESHOLD}）。建议轮换角色交互模式。"
                ),
            })

    return findings


# ============================================================
# 十、SimHash语义碰撞检查
# ============================================================

def check_simhash_collision(
    current_simhash: int,
    chapter: int,
    registry: TropeRegistry,
) -> list[dict[str, Any]]:
    """检查SimHash语义碰撞（近100章，汉明距离≤3）。
    不受repeat_intent豁免影响。
    """
    if current_simhash == 0:
        return []

    findings = []
    recent = registry.get_recent_chapters(SIMHASH_WINDOW, before_chapter=chapter)

    collisions = []
    for rec in recent:
        rec_simhash = rec.get("simhash", 0)
        if rec_simhash == 0:
            continue
        dist = hamming_distance(current_simhash, rec_simhash)
        if dist <= SIMHASH_HAMMING_THRESHOLD:
            collisions.append({
                "chapter": rec["chapter"],
                "distance": dist,
                "core_trope": rec.get("core_trope", ""),
                "conflict_pattern": rec.get("conflict_pattern", ""),
            })

    if collisions:
        # 按汉明距离排序（越近越相似）
        collisions.sort(key=lambda x: x["distance"])
        top = collisions[:3]
        desc_parts = []
        for c in top:
            desc_parts.append(
                f"第{c['chapter']}章（汉明距离{c['distance']}，"
                f"冲突={c['conflict_pattern']}，桥段={c['core_trope']}）"
            )
        findings.append({
            "severity": "advisory",
            "rule": "SimHash语义碰撞",
            "collisions": collisions,
            "description": (
                f"核心情节与近{SIMHASH_WINDOW}章中{len(collisions)}章语义高度相似"
                f"（汉明距离≤{SIMHASH_HAMMING_THRESHOLD}）："
                + "；".join(desc_parts)
                + "。可能是换皮重复，请人工确认。注意：即使是有意重复（伏笔回收等），也不应与某章文字级雷同。"
            ),
        })

    return findings


# ============================================================
# 十一、repeat_intent 白名单豁免校验
# ============================================================

def validate_repeat_intent(
    fingerprint: dict[str, Any],
    project_root: Path,
    chapter: int,
) -> list[dict[str, Any]]:
    """校验repeat_intent白名单。非none时必须通过对应校验，否则按正常规则执行。"""
    ri = fingerprint.get("repeat_intent", "none")
    if ri == "none":
        return []

    findings = []

    if ri == "foreshadow_payoff":
        # 检查追踪/伏笔.md中是否有对应伏笔
        foreshadow_path = project_root / "追踪" / "伏笔.md"
        if not foreshadow_path.exists():
            findings.append({
                "severity": "blocking",
                "rule": "豁免校验失败",
                "repeat_intent": ri,
                "description": "repeat_intent=foreshadow_payoff，但追踪/伏笔.md不存在，无法验证伏笔回收。豁免不通过，按正常冷却规则执行。",
            })
        else:
            # 简单检查：伏笔文件中是否有未回收的伏笔
            try:
                content = foreshadow_path.read_text(encoding="utf-8")
                if "未回收" not in content and "active" not in content.lower() and "埋设" not in content:
                    findings.append({
                        "severity": "advisory",
                        "rule": "豁免校验提示",
                        "repeat_intent": ri,
                        "description": "repeat_intent=foreshadow_payoff，但追踪/伏笔.md中未找到明确的未回收伏笔标记。请确认本章确实是伏笔回收节点。",
                    })
            except OSError:
                pass

    elif ri == "theme_variation":
        # 检查bible.json是否有creative字段
        bible_path = project_root / "bible.json"
        if not bible_path.exists():
            findings.append({
                "severity": "blocking",
                "rule": "豁免校验失败",
                "repeat_intent": ri,
                "description": "repeat_intent=theme_variation，但bible.json不存在，无法验证主题变奏。豁免不通过。",
            })
        else:
            try:
                bible = json.loads(bible_path.read_text(encoding="utf-8"))
                creative = bible.get("creative", {})
                if not creative or not creative.get("core_question"):
                    findings.append({
                        "severity": "advisory",
                        "rule": "豁免校验提示",
                        "repeat_intent": ri,
                        "description": "repeat_intent=theme_variation，但bible.json的creative字段缺少core_question。建议补充全书核心命题以支持主题变奏豁免。",
                    })
            except (json.JSONDecodeError, OSError):
                pass

    elif ri == "character_growth_parallel":
        # 检查是否有对应角色卡（简单提示，不强制）
        findings.append({
            "severity": "advisory",
            "rule": "豁免校验提示",
            "repeat_intent": ri,
            "description": "repeat_intent=character_growth_parallel：请确认本章是某角色成长线的对照节点（对应角色卡中应有成长线记录）。",
        })

    elif ri == "genre_convention":
        # 检查bible.json是否有genre_convention_patterns
        bible_path = project_root / "bible.json"
        if bible_path.exists():
            try:
                bible = json.loads(bible_path.read_text(encoding="utf-8"))
                patterns = bible.get("genre_convention_patterns", [])
                ct = fingerprint.get("core_trope", "")
                if patterns and ct not in patterns:
                    findings.append({
                        "severity": "advisory",
                        "rule": "豁免校验提示",
                        "repeat_intent": ri,
                        "description": f"repeat_intent=genre_convention，但core_trope '{ct}' 不在bible.json的genre_convention_patterns列表中。请确认该桥段确实是题材惯例。",
                    })
            except (json.JSONDecodeError, OSError):
                pass

    return findings


# ============================================================
# 十二、主检查流程
# ============================================================

def run_check(project_root: Path, chapter_arg: str) -> dict[str, Any]:
    """执行完整的trope guard检查。返回结构化结果。"""
    # 1. 解析细纲路径
    outline_path = resolve_chapter_path(project_root, chapter_arg)
    if outline_path is None:
        return {
            "verdict": "ERROR",
            "error": f"无法找到细纲：{chapter_arg}。请提供细纲文件路径或章号。",
        }

    chapter = extract_chapter_number(outline_path)
    if chapter == 0:
        return {
            "verdict": "ERROR",
            "error": f"无法从文件名提取章号：{outline_path.name}",
        }

    # 2. 解析指纹
    fingerprint = parse_fingerprint_from_outline(outline_path)
    if not fingerprint:
        # 零侵入：无指纹章节静默放行（SKIPPED），不阻断流程。
        # 只有主动声明了指纹，系统才介入核验。旧项目迁移/作者手写随笔不受影响。
        return {
            "verdict": "SKIPPED",
            "chapter": chapter,
            "outline_path": str(outline_path),
            "reason": "细纲中未找到 structural_fingerprint YAML frontmatter 区块，按零侵入原则静默放行。如需启用门禁，请在细纲顶部添加结构指纹区块。",
            "findings": [],
        }

    # 3. 枚举校验
    enum_errors = validate_enums(fingerprint)
    blocking_enum = [e for e in enum_errors if e["severity"] == "blocking"]

    # 4. 初始化注册表
    registry = TropeRegistry(project_root)

    # 5. repeat_intent豁免校验
    intent_findings = validate_repeat_intent(fingerprint, project_root, chapter)

    # 6. CD冷却检查（如果枚举有blocking错误，跳过冷却检查，因为值不可信）
    cd_findings = []
    combo_findings = []
    simhash_findings = []
    if not blocking_enum:
        cd_findings = check_cd_cooldown(fingerprint, chapter, registry)
        combo_findings = check_combo_collisions(fingerprint, chapter, registry)

        # SimHash
        cps = fingerprint.get("core_plot_summary", "")
        if cps:
            current_simhash = compute_simhash(cps)
            simhash_findings = check_simhash_collision(current_simhash, chapter, registry)

    # 7. 汇总
    all_findings = enum_errors + intent_findings + cd_findings + combo_findings + simhash_findings
    blocking = [f for f in all_findings if f["severity"] == "blocking"]
    advisory = [f for f in all_findings if f["severity"] == "advisory"]

    verdict = "PASS" if not blocking else "BLOCKING"

    return {
        "verdict": verdict,
        "chapter": chapter,
        "outline_path": str(outline_path),
        "fingerprint": fingerprint,
        "summary": {
            "total": len(all_findings),
            "blocking": len(blocking),
            "advisory": len(advisory),
        },
        "blocking_findings": blocking,
        "advisory_findings": advisory,
        "registered": registry.is_registered(chapter),
    }


def run_register(project_root: Path, chapter_arg: str, force: bool = False) -> dict[str, Any]:
    """登记一章的指纹到桥段库。先执行check，通过后才登记。"""
    # 先check
    check_result = run_check(project_root, chapter_arg)
    if check_result["verdict"] == "BLOCKING":
        return {
            "verdict": "BLOCKING",
            "action": "register_skipped",
            "reason": "检查未通过，存在blocking级问题，不予登记。请先修复细纲。",
            "check_result": check_result,
        }
    if check_result["verdict"] == "ERROR":
        return check_result
    if check_result["verdict"] == "SKIPPED":
        return {
            "verdict": "SKIPPED",
            "action": "register_skipped",
            "chapter": check_result.get("chapter"),
            "reason": "细纲中未找到 structural_fingerprint 区块，按零侵入原则跳过登记。",
        }

    chapter = check_result["chapter"]
    fingerprint = check_result["fingerprint"]
    registry = TropeRegistry(project_root)

    # 检查是否已登记
    if registry.is_registered(chapter) and not force:
        return {
            "verdict": "PASS",
            "action": "already_registered",
            "chapter": chapter,
            "message": f"第{chapter}章指纹已登记。使用 --force 覆盖更新。",
        }

    # 计算SimHash
    cps = fingerprint.get("core_plot_summary", "")
    simhash = compute_simhash(cps) if cps else 0

    # 推断卷号
    volume_id = extract_volume_id(project_root, chapter)

    # 登记
    registry.register(chapter, volume_id, fingerprint, simhash)

    return {
        "verdict": "PASS",
        "action": "registered",
        "chapter": chapter,
        "volume_id": volume_id,
        "simhash": simhash,
        "message": f"第{chapter}章指纹已登记到桥段库（卷{volume_id}）。",
    }


def run_status(project_root: Path) -> dict[str, Any]:
    """查看桥段库状态。"""
    registry = TropeRegistry(project_root)
    index = registry.load_index()
    chapters = index.get("chapters", {})

    # 统计各维度使用频率
    stats = {
        "conflict_pattern": {},
        "core_trope": {},
        "golden_finger_mode": {},
        "resolution_cost": {},
        "character_dynamic": {},
    }

    for ch_str, data in chapters.items():
        for field in stats:
            val = data.get(field, "")
            if val:
                stats[field][val] = stats[field].get(val, 0) + 1

    # 找出最近使用的各维度值（用于CD状态查看）
    recent = registry.get_recent_chapters(20)

    return {
        "total_registered": len(chapters),
        "last_chapter": index.get("last_chapter", 0),
        "stats": {k: dict(sorted(v.items(), key=lambda x: -x[1])) for k, v in stats.items()},
        "recent_chapters": [
            {
                "chapter": r["chapter"],
                "conflict_pattern": r.get("conflict_pattern", ""),
                "core_trope": r.get("core_trope", ""),
                "golden_finger_mode": r.get("golden_finger_mode", ""),
                "resolution_cost": r.get("resolution_cost", ""),
            }
            for r in recent
        ],
    }


# ============================================================
# 十三、CLI
# ============================================================

def print_result(result: dict[str, Any], verbose: bool = False) -> None:
    """打印检查结果。"""
    verdict = result.get("verdict", "UNKNOWN")
    verdict_icon = {"PASS": "✅", "BLOCKING": "⛔", "ERROR": "❌", "SKIPPED": "⏭️"}.get(verdict, "❓")

    print(f"\n{verdict_icon} Trope Guard — {verdict}")

    if "error" in result:
        print(f"  错误：{result['error']}")
        return

    if "reason" in result:
        print(f"  原因：{result['reason']}")

    if "chapter" in result:
        print(f"  章节：第{result['chapter']}章")
    if "outline_path" in result:
        print(f"  细纲：{result['outline_path']}")

    if "summary" in result:
        s = result["summary"]
        print(f"  统计：共{s['total']}项（blocking={s['blocking']}，advisory={s['advisory']}）")

    # 打印指纹
    if "fingerprint" in result and result["fingerprint"]:
        fp = result["fingerprint"]
        print(f"\n  📋 结构指纹：")
        for field in ["conflict_pattern", "core_trope", "golden_finger_mode",
                       "resolution_cost", "character_dynamic", "repeat_intent"]:
            if field in fp:
                print(f"    {field}: {fp[field]}")
        if "core_plot_summary" in fp:
            print(f"    core_plot_summary: {fp['core_plot_summary'][:60]}...")

    # 打印blocking
    if result.get("blocking_findings"):
        print(f"\n⛔ BLOCKING（必须修复）：")
        for i, f in enumerate(result["blocking_findings"], 1):
            print(f"  {i}. [{f.get('rule', '')}] {f.get('description', '')}")

    # 打印advisory
    if result.get("advisory_findings"):
        print(f"\n⚠️  ADVISORY（建议关注）：")
        for i, f in enumerate(result["advisory_findings"], 1):
            print(f"  {i}. [{f.get('rule', '')}] {f.get('description', '')}")

    # 登记状态
    if "registered" in result:
        status = "已登记" if result["registered"] else "未登记"
        print(f"\n  桥段库状态：{status}")

    if "action" in result:
        print(f"\n  操作：{result['action']}")
        if "message" in result:
            print(f"  消息：{result['message']}")

    print()


def run_inspect(project_root: Path, window: int = 20, threshold: int = 3) -> dict[str, Any]:
    """极简滑动窗口诊断：近N章中，哪个维度的值出现了≥threshold次，标红列出。
    不算熵、不算σ、不做高斯，纯频次计数，给人看。
    """
    registry = TropeRegistry(project_root)
    recent = registry.get_recent_chapters(window)

    if not recent:
        return {
            "window": window,
            "threshold": threshold,
            "chapter_count": 0,
            "note": f"桥段库为空或不足{window}章，暂无数据",
            "hotspots": [],
        }

    dimensions = [
        ("conflict_pattern", "冲突模式"),
        ("core_trope", "核心桥段"),
        ("golden_finger_mode", "金手指用法"),
        ("resolution_cost", "解决代价"),
        ("character_dynamic", "角色交互模式"),
    ]

    hotspots = []
    for field, label in dimensions:
        counts: dict[str, list[int]] = {}
        for rec in recent:
            val = rec.get(field, "")
            if not val or val == "NOT_APPLICABLE":
                continue
            if val not in counts:
                counts[val] = []
            counts[val].append(rec["chapter"])
        for val, chapters in sorted(counts.items(), key=lambda x: -len(x[1])):
            if len(chapters) >= threshold:
                hotspots.append({
                    "dimension": field,
                    "dimension_label": label,
                    "value": val,
                    "count": len(chapters),
                    "chapters": sorted(chapters),
                    "risk": "高" if len(chapters) >= threshold * 2 else "中",
                })

    # 按出现次数排序
    hotspots.sort(key=lambda x: -x["count"])

    return {
        "window": window,
        "threshold": threshold,
        "chapter_count": len(recent),
        "chapter_range": f"第{recent[-1]['chapter']}章 ~ 第{recent[0]['chapter']}章" if recent else "无",
        "hotspot_count": len(hotspots),
        "hotspots": hotspots,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="NWW-V12 剧情结构指纹门禁（CD冷却+变奏检查+SimHash碰撞）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python3 nww_trope_guard.py check --chapter 15 --project /path/to/book
  python3 nww_trope_guard.py check --chapter 大纲/细纲_第015章.md --project /path/to/book
  python3 nww_trope_guard.py register --chapter 15 --project /path/to/book
  python3 nww_trope_guard.py register --chapter 15 --project /path/to/book --force
  python3 nww_trope_guard.py status --project /path/to/book
        """,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # check
    check_parser = subparsers.add_parser("check", help="检查细纲指纹（只读，不写入）")
    check_parser.add_argument("--chapter", required=True, help="细纲文件路径或章号")
    check_parser.add_argument("--project", required=True, help="书项目根目录")
    check_parser.add_argument("--json", action="store_true", help="输出JSON格式")

    # register
    reg_parser = subparsers.add_parser("register", help="登记细纲指纹到桥段库（先check后登记）")
    reg_parser.add_argument("--chapter", required=True, help="细纲文件路径或章号")
    reg_parser.add_argument("--project", required=True, help="书项目根目录")
    reg_parser.add_argument("--force", action="store_true", help="覆盖已登记的指纹")
    reg_parser.add_argument("--json", action="store_true", help="输出JSON格式")

    # status
    status_parser = subparsers.add_parser("status", help="查看桥段库状态")
    status_parser.add_argument("--project", required=True, help="书项目根目录")
    status_parser.add_argument("--json", action="store_true", help="输出JSON格式")

    # inspect
    inspect_parser = subparsers.add_parser("inspect", help="极简滑动窗口诊断：近N章高频值标红（纯频次，不算熵）")
    inspect_parser.add_argument("--project", required=True, help="书项目根目录")
    inspect_parser.add_argument("--window", type=int, default=20, help="回看章数（默认20）")
    inspect_parser.add_argument("--threshold", type=int, default=3, help="标红阈值（默认≥3次）")
    inspect_parser.add_argument("--json", action="store_true", help="输出JSON格式")

    args = parser.parse_args()
    project_root = Path(args.project).resolve()

    if args.command == "check":
        result = run_check(project_root, args.chapter)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print_result(result)
        # PASS 和 SKIPPED 都返回0（不阻断），BLOCKING返回1
        return 0 if result["verdict"] in ("PASS", "SKIPPED") else 1

    elif args.command == "register":
        result = run_register(project_root, args.chapter, force=args.force)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print_result(result)
        return 0 if result["verdict"] == "PASS" else 1

    elif args.command == "status":
        result = run_status(project_root)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"\n📊 Trope Registry 状态")
            print(f"  已登记章节：{result['total_registered']}")
            print(f"  最新章号：{result['last_chapter']}")
            print(f"\n  各维度使用统计：")
            for field, counts in result["stats"].items():
                if counts:
                    top = list(counts.items())[:5]
                    print(f"    {field}: " + ", ".join(f"{k}({v})" for k, v in top))
            if result["recent_chapters"]:
                print(f"\n  最近{len(result['recent_chapters'])}章：")
                for r in result["recent_chapters"]:
                    print(f"    第{r['chapter']}章: {r['conflict_pattern']} / {r['core_trope']} / {r['golden_finger_mode']}")
            print()
        return 0

    elif args.command == "inspect":
        result = run_inspect(project_root, window=args.window, threshold=args.threshold)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"\n🔍 Trope Guard Inspect — 近{result['window']}章滑动窗口诊断")
            print(f"  章范围：{result['chapter_range']}（共{result['chapter_count']}章）")
            print(f"  标红阈值：出现≥{result['threshold']}次")
            print(f"  高频项数：{result['hotspot_count']}")
            if result.get("note"):
                print(f"  提示：{result['note']}")
            if result["hotspots"]:
                print(f"\n⚠️  高频值（可能导致套路感/疲劳）：")
                for i, h in enumerate(result["hotspots"], 1):
                    risk_icon = "🔴" if h["risk"] == "高" else "🟡"
                    print(f"  {i}. {risk_icon} [{h['dimension_label']}] {h['value']} — 出现{h['count']}次（章：{', '.join(map(str, h['chapters']))}）")
                print(f"\n  建议：以上高频值在近{result['window']}章内出现过密，考虑在后续章节中轮换。")
                print(f"  注意：这是诊断参考，不是硬阻断。具体是否需要调整由作者判断。")
            else:
                print(f"\n✅ 近{result['window']}章内无≥{result['threshold']}次的高频值，节奏健康。")
            print()
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
