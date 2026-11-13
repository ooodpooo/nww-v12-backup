#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nww_pronoun_gate.py — 代词性别机械闸的唯一实现（C-PRONOUN@1）。

语义取自 Phase 1 前 ``web/backend/app/agents_runtime/pipeline.py::_pronoun_gate_check``
（2026-11-13 下沉）：主角锚回退链、性别声明解析、段落归属（含 Local_Actor 机制）与
阈值全部在此维护，web pipeline 与脚本侧 ``nww_gates_shared`` 只做转发。

模块不反向 import web 侧代码：主角锚、性别、角色名集合、未注册实体代称词表
都在本文件内自带实现。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

PRONOUN_WRONG_MIN = 8
PRONOUN_WRONG_RATIO = 0.5

# 未注册实体代称词表（2026-10-30 外脑第四轮裁决三：Local_Actor 段落归属）。
# 已注册角色（有卡/入追踪 state）不进词表，由段落归属的 present 机制处理。
# 群体通称（「修士」「众人」等）故意不入表：误触发面过大，会削弱主角代词统计效力。
# 书级扩展：追踪 state 的 local_actor_terms 可选字段，见 local_actor_terms()。
LOCAL_ACTOR_TERMS = frozenset({
    "散修", "老者", "老翁", "老妪", "老妇", "童子", "少年", "少女", "青年",
    "汉子", "大汉", "婆子", "妇人", "孩子", "小孩", "书生", "乞丐",
    "道人", "道姑", "僧人", "尼姑", "摊主", "掌柜", "伙计", "小二",
    "路人", "行商", "货郎", "车夫", "樵夫", "猎户", "铁匠", "郎中",
    "侍女", "婢女", "小厮", "护卫", "镖师", "管事",
})

# LOCAL 段落归属哨兵：该区域代词从主角统计池剔除（subject 不在主角锚集合）
LOCAL_ACTOR_SENTINEL = "__local_actor__"

_CARD_DIR = ("设定", "角色")
_STATE_PATH = ("追踪", "_tracking-state.json")


def _card_dir(book: Path) -> Path:
    return book.joinpath(*_CARD_DIR)


def _state_path(book: Path) -> Path:
    return book.joinpath(*_STATE_PATH)


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — 缺文件/损坏按无数据继续
        return None


def _bible_protagonist_name(book: Path) -> str:
    """bible.json protagonist.name（去掉前导「主角」）；缺失/损坏返回 ""。"""
    bible = _load_json(book / "bible.json")
    if not isinstance(bible, dict):
        return ""
    protagonist = bible.get("protagonist")
    if not isinstance(protagonist, dict):
        return ""
    return re.sub(r"^主角", "", str(protagonist.get("name") or "")).strip()


def _bible_protagonist_description_name(book: Path) -> str:
    """从 ``bible.protagonist.description`` 抽取「主角X」里的显式名字；无则 ""。

    只认「主角」+ 2-4 字中文 + 紧随标点/空白/结尾的形态，避免把「主角是一位…」
    误抽成名字。
    """
    bible = _load_json(book / "bible.json")
    if not isinstance(bible, dict):
        return ""
    protagonist = bible.get("protagonist")
    if not isinstance(protagonist, dict):
        return ""
    desc = str(protagonist.get("description") or "")
    m = re.search(r"主角\s*[：:]?\s*([\u4e00-\u9fa5]{2,4})"
                  r"(?=[，,。；;、\s）)]|$)", desc)
    if not m:
        return ""
    name = m.group(1).strip()
    if re.match(r"^(是|为|叫|名|的|在|从|与)", name):
        return ""
    return name


def _card_explicit_name(head: str) -> str:
    """主角卡头 8 行显式姓名字段（姓名/名字/角色名/名称）；无则空。"""
    m = re.search(r"(?:姓名|名字|角色名|名称)\s*[：:]\s*(\S[^\n]{0,30})", head)
    return m.group(1).strip() if m else ""


def _card_has_gender_decl(head: str) -> bool:
    """卡头是否含任一性别声明（显式字段或旧标注，只判定存在性）。"""
    return bool(
        re.search(r"性别\s*[：:]\s*(男|女)", head)
        or re.search(r"女主|（女）|女主角", head)
        or re.search(r"男主|（男）|男主角", head)
    )


def _card_for_name(book: Path, name: str) -> Path | None:
    """按名字定位角色卡（stem 互相包含即视为命中，与旧回退链同口径）。"""
    if not name:
        return None
    card_dir = _card_dir(book)
    if not card_dir.is_dir():
        return None
    for p in sorted(card_dir.glob("*.md")):
        if name in p.stem or p.stem in name:
            return p
    return None


def _anchor_from_cards(book: Path) -> tuple[set[str], str, Path | None]:
    """追踪 state 印证唯一的卡 → 唯一带性别声明的卡；失败返回空集。"""
    card_dir = _card_dir(book)
    cards = sorted(card_dir.glob("*.md")) if card_dir.is_dir() else []
    state_names: set[str] = set()
    state = _load_json(_state_path(book))
    if isinstance(state, dict) and isinstance(state.get("characters"), dict):
        state_names = {str(k) for k in state["characters"] if k}

    anchor_card: Path | None = None
    source = "none"
    if state_names:
        matched = [p for p in cards if p.stem in state_names]
        if len(matched) == 1:
            anchor_card, source = matched[0], "trace_state"
    if anchor_card is None:
        gendered = []
        for p in cards:
            try:
                head = "\n".join(p.read_text(encoding="utf-8").splitlines()[:8])
            except Exception:  # noqa: BLE001
                continue
            if _card_has_gender_decl(head):
                gendered.append(p)
        if len(gendered) == 1:
            anchor_card, source = gendered[0], "gender_card"
    if anchor_card is None:
        return set(), "none", None
    try:
        head = "\n".join(anchor_card.read_text(encoding="utf-8").splitlines()[:8])
    except Exception:  # noqa: BLE001
        return set(), source, anchor_card
    name = _card_explicit_name(head)
    return ({name} if name else {anchor_card.stem}), source, anchor_card


def resolve_protagonist_anchor(book: Path) -> tuple[set[str], str, Path | None]:
    """主角锚解析（返回主角名集合、锚来源、主角卡或 None）。

    回退链（只认作者显式声明）：
      1. bible.json protagonist.name；
      2. 追踪 state characters ∩ 卡 stem 唯一者 → 头 8 行含性别声明的唯一卡；
      3. bible.protagonist.description 的「主角X」。
    全部失败返回 ({}, "none", None)，代词闸按锚缺失警示跳过。
    """
    name = _bible_protagonist_name(book)
    if name:
        return {name}, "bible", _card_for_name(book, name)
    names, source, card = _anchor_from_cards(book)
    if names:
        return names, source, card
    desc_name = _bible_protagonist_description_name(book)
    if desc_name:
        return {desc_name}, "bible_description", _card_for_name(book, desc_name)
    return set(), "none", None


def protagonist_gender(book: Path) -> str:
    """主角性别声明（只认主角卡头 8 行的显式标注；无声明返回空）。

    识别顺序：标准字段「性别：男/女」→ 旧标注 女主/女主角/（女）、男主/男主角/（男）。
    显式字段与旧标注冲突、或同时命中男女旧标注 → 返回空（闸按缺失警示跳过）。
    """
    names, _source, card = resolve_protagonist_anchor(book)
    if card is None and names:
        card_dir = _card_dir(book)
        for name in sorted(names):
            candidate = card_dir / f"{name}.md"
            if candidate.is_file():
                card = candidate
                break
    if card is None:
        return ""
    try:
        head = "\n".join(card.read_text(encoding="utf-8").splitlines()[:8])
    except Exception:  # noqa: BLE001
        return ""
    explicit_m = re.search(r"性别\s*[：:]\s*(男|女)", head)
    legacy_female = re.search(r"女主|（女）|女主角", head)
    legacy_male = re.search(r"男主|（男）|男主角", head)
    if legacy_female and legacy_male:
        return ""
    explicit = {"男": "male", "女": "female"}.get(explicit_m.group(1)) if explicit_m else ""
    if explicit:
        if (explicit == "female" and legacy_male) or (explicit == "male" and legacy_female):
            return ""
        return explicit
    if legacy_female:
        return "female"
    if legacy_male:
        return "male"
    return ""


def contract_sidecar_path(book: Path, chapter_id: int) -> Path | None:
    """本章写作合同 sidecar 路径（双命名变体统一收口）。"""
    for name in (
        f"细纲_第{chapter_id:03d}章.contract.json",
        f"细纲_{chapter_id:03d}章.contract.json",
    ):
        p = book / "大纲" / name
        if p.is_file():
            return p
    return None


def known_names(book: Path, chapter_id: int) -> set[str]:
    """闸用角色名集合：主角卡名 + 追踪 state characters + 本章 sidecar 出场角色。"""
    names: set[str] = set()
    pname = _bible_protagonist_name(book)
    if pname:
        names.add(pname)
    state = _load_json(_state_path(book))
    if isinstance(state, dict) and isinstance(state.get("characters"), dict):
        for k in state["characters"]:
            if k:
                names.add(str(k))
    sidecar = contract_sidecar_path(book, chapter_id)
    if sidecar is not None:
        doc = _load_json(sidecar)
        if isinstance(doc, dict):
            for c in (doc.get("characters") or []):
                if isinstance(c, dict) and c.get("name"):
                    names.add(str(c["name"]))
    return names


def local_actor_terms(book: Path) -> frozenset[str]:
    """未注册实体代称词表：内置 LOCAL_ACTOR_TERMS + 书级扩展。"""
    terms = set(LOCAL_ACTOR_TERMS)
    state = _load_json(_state_path(book))
    if isinstance(state, dict):
        for t in (state.get("local_actor_terms") or []):
            if str(t).strip():
                terms.add(str(t).strip())
    return frozenset(terms)


def protagonist_pronoun_counts(
    book: Path, chapter_id: int, prose_text: str,
    proto_names: set[str] | None = None, gender: str | None = None,
) -> tuple[int, int]:
    """主角段代词计数，返回 subject ∈ 主角锚 的段落 (他, 她) 计数。

    段落归属规则（Local_Actor 机制 + 实测修正）：
      段落含唯一已注册名 → subject=该名；含多个 → 跳过（归属歧义）；
      含 0 个且含未注册实体代称 → LOCAL 接管剔除，但段内出现主角正向代词时
      视为「主角未点名的延续交错段」计入主角；LOCAL 跨段沿用，直至段落再次
      出现已注册名才重置；段落同时含主角名与实体代称（歧义）→ 计入主角。
    """
    body = re.sub(r"^[#\s]*第\s*\d+\s*章.*$", "", prose_text or "", flags=re.M)
    body = re.sub(r"“[^”]{0,400}”|「[^」]{0,400}」|\"[^\"]{0,400}\"", "", body)
    if proto_names is None:
        proto_names, _source, _card = resolve_protagonist_anchor(book)
    if gender is None:
        gender = protagonist_gender(book)
    names = known_names(book, chapter_id)
    names.update(proto_names)
    terms = local_actor_terms(book)
    forward = "她" if gender == "female" else "他"
    he_p = she_p = 0
    subject = ""
    for para in re.split(r"\n+", body):
        para = para.strip()
        if not para:
            continue
        present = [n for n in names if n and n in para]
        if len(present) == 1:
            subject = present[0]
        elif len(present) > 1:
            continue  # 多角色同段，代词归属歧义
        elif terms and any(t in para for t in terms) and forward not in para:
            subject = LOCAL_ACTOR_SENTINEL  # 未注册实体代称且无主角正向代词 → LOCAL 接管
        if subject in proto_names:
            he_p += len(re.findall(r"他", para))
            she_p += len(re.findall(r"她", para))
    return he_p, she_p


def check(book: Path, chapter_id: int, prose_text: str) -> tuple[bool, str]:
    """代词性别机械闸：叙述层代词按主语归属统计 vs 角色卡性别声明。

    ``NWW_PRONOUN_GATE=0`` 关闭；主角锚或性别缺失时返回 (True, WARNING 文案)。
    判否条件：``wrong_p >= PRONOUN_WRONG_MIN and wrong_p > right_p * PRONOUN_WRONG_RATIO``。
    """
    if os.environ.get("NWW_PRONOUN_GATE", "1") == "0":
        return True, "代词闸已关闭"
    proto_names, anchor_source, _anchor_card = resolve_protagonist_anchor(book)
    gender = protagonist_gender(book)
    if not proto_names:
        return True, "主角锚缺失，代词闸跳过（WARNING：无法确定主角名，请检查 bible.json protagonist.name 或 设定/角色/ 主角卡命名，否则代词闸不生效）"
    if not gender:
        return True, "无角色卡性别声明，代词闸跳过（WARNING：主角性别未登记，请在 设定/角色/ 主角卡头 8 行补「性别：男/女」，否则代词闸不生效）"
    he_p, she_p = protagonist_pronoun_counts(
        book, chapter_id, prose_text, proto_names=proto_names, gender=gender)
    proto_name = sorted(proto_names)[0]
    label = "女性" if gender == "female" else "男性"
    wrong = "他" if gender == "female" else "她"
    right = "她" if gender == "female" else "他"
    wrong_p = he_p if gender == "female" else she_p
    right_p = she_p if gender == "female" else he_p
    if wrong_p >= PRONOUN_WRONG_MIN and wrong_p > right_p * PRONOUN_WRONG_RATIO:
        return False, (
            f"叙述层代词性别错位：视角主角（{proto_name}，角色卡判定{label}）"
            f"被以「{wrong}」指称 ×{wrong_p}，而「{right}」×{right_p}"
            "——请核查视角角色代词（对照 设定/角色/ 性别声明）"
        )
    return True, f"代词性别一致（{label}主角{proto_name or ''}，锚={anchor_source}；{wrong}×{wrong_p}/{right}×{right_p}）"