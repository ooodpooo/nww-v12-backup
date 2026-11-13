#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nww_bible_check.py — bible.json 契约校验（只读，不改文件）

独立新模块，与既有流水线零耦合：建书（/nww-setup）后手动或脚本调用，
校验 bible.json 是否满足下游消费端（nww_enforce.py / long-write Phase 1）
的字段契约，防止缺字段导致的「静默失败」（无比喻注入 / 无代价锚 / 无反派败相）。

分级：
  ERROR   —— 必填缺失或非法，下游会静默失效，必须修复后才能开写（exit 2）
  WARNING —— 推荐字段缺失，不影响机械层兜底，但会削弱产出质量（exit 0）

用法：
  python nww_bible_check.py --book <书项目根> [--json]
  --json 时以 JSON 输出 findings（供脚本消费），否则打印人类可读报告。
"""
import argparse
import json
import os
import sys

REQUIRED_TOP = ("world_summary", "genre", "pov", "characters", "cost_system",
                "mechanics", "signature_metaphor", "villain_down_beats")
POV_VALUES = ("first", "third")
REQUIRED_CHAR = ("name", "role", "identity", "voice")
GENDER_VALUES = ("男", "女", "无性别")
REQUIRED_COST = ("device", "rule", "counter_format", "meter_regex", "current")


def _s(value):
    """非空字符串判定：None/空串/纯空白 视为缺失。"""
    return isinstance(value, str) and value.strip() != ""


def _check_bible(bible: dict) -> list[dict]:
    findings: list[dict] = []
    def err(msg):
        findings.append({"level": "ERROR", "message": msg})
    def warn(msg):
        findings.append({"level": "WARNING", "message": msg})

    # 顶层必填
    for key in REQUIRED_TOP:
        if key not in bible:
            err(f"缺少顶层必填字段 {key}")
        elif key == "characters" and not isinstance(bible[key], list):
            err(f"characters 必须是数组，实际 {type(bible[key]).__name__}")
        elif key == "mechanics" and not isinstance(bible[key], list):
            err(f"mechanics 必须是数组，实际 {type(bible[key]).__name__}")
        elif key == "villain_down_beats" and not isinstance(bible[key], list):
            err(f"villain_down_beats 必须是数组，实际 {type(bible[key]).__name__}")

    pov = bible.get("pov")
    if pov is not None and pov not in POV_VALUES:
        err(f"pov 必须是 {'/'.join(POV_VALUES)}，实际 {pov!r}")

    # 角色契约
    chars = bible.get("characters") or []
    protagonists = [c for c in chars if isinstance(c, dict) and c.get("role") == "主角"]
    if not protagonists:
        err("characters 中必须存在 role=主角 的角色（narrative-writer/enforce 的主视角锚）")
    if len(protagonists) > 1:
        err(f"role=主角 的角色多于 1 个（{len(protagonists)}），enforce 只认第一个")
    for i, c in enumerate(chars):
        if not isinstance(c, dict):
            err(f"characters[{i}] 必须是对象")
            continue
        label = c.get("name") or f"characters[{i}]"
        if not _s(c.get("age")) and not (isinstance(c.get("age"), (int, float)) and c.get("age") > 0):
            warn(f"角色 {label} 缺 age（设定年龄，用于校验身份与修为的匹配合理性）")
        for key in REQUIRED_CHAR:
            if not _s(c.get(key)):
                err(f"角色 {label} 缺少必填字段 {key}")
        # 【2026-09-14】性别字段校验：必须显式填写，供代词闸/F视角检查消费
        gender = c.get("gender")
        if not _s(gender):
            warn(f"角色 {label} 缺 gender（性别=角色性别，供代词闸/F视角检查消费，不可省略。值域：男/女/无性别）")
        elif isinstance(gender, str) and gender.strip() not in GENDER_VALUES:
            warn(f"角色 {label} 的 gender 值「{gender}」不在值域 {GENDER_VALUES} 内")
        if not _s(c.get("anchor")):
            warn(f"角色 {label} 缺 anchor（动机锚点，第 1 章须落成一句独白）")
        if not _s(c.get("flaw")):
            warn(f"角色 {label} 缺 flaw（致命弱点=性格缺陷，会让角色犯错，非能力缺陷）")
        if not _s(c.get("arc")):
            warn(f"角色 {label} 缺 arc（人物弧光=从什么状态到什么状态，群像化核心，配角单拎出来有自己的线）")
        if not _s(c.get("fate_metaphor")):
            warn(f"角色 {label} 缺 fate_metaphor（命运隐喻=一句话预言这个角色的结局，如『车的宿命：最强的子最先被换掉』）")
        # 【2026-09-14】12层角色系统校验
        # Layer 4: 性格硬约束（权重30%，主角强烈推荐）
        hc = c.get("hard_constraints")
        if c.get("role") == "主角":
            if not isinstance(hc, dict):
                warn(f"角色 {label} 缺 hard_constraints（性格硬约束=Layer 4，权重30%，主角强烈推荐。包含 always/never 两数组）")
            else:
                always = hc.get("always") or []
                never = hc.get("never") or []
                if not isinstance(always, list) or len(always) < 2:
                    warn(f"角色 {label} 的 hard_constraints.always 少于 2 条，至少需要'永远不会做什么'2条具体行为约束")
                if not isinstance(never, list) or len(never) < 2:
                    warn(f"角色 {label} 的 hard_constraints.never 少于 2 条，至少需要'绝对禁止行为'2条硬性红线")
        # Layer 5: 过去经历（权重25%，主角强烈推荐）
        bs = c.get("backstory")
        if c.get("role") == "主角":
            if not isinstance(bs, dict):
                warn(f"角色 {label} 缺 backstory（过去经历=Layer 5，权重25%，主角强烈推荐。包含 events 数组）")
            else:
                events = bs.get("events") or []
                if not isinstance(events, list) or len(events) < 1:
                    warn(f"角色 {label} 的 backstory.events 少于 1 个核心经历事件")
        # Layer 6: 能力系统（推荐）
        ab = c.get("ability")
        if c.get("role") == "主角":
            if not isinstance(ab, dict):
                warn(f"角色 {label} 缺 ability（能力系统=Layer 6，推荐。包含 strengths/weaknesses/resources/limitations）")
        # Layer 9: 语言系统（voice 必填；catchphrases 可选——低频锚点非必用、不设次数）
        voice = c.get("voice") or ""
        catchphrases = c.get("catchphrases")
        if catchphrases is not None and not isinstance(catchphrases, list):
            warn(f"角色 {label} 的 catchphrases 必须是数组")
        # anchor 与 break_rules.effect 重复检查：anchor 是内心独白，effect 是说出口的台词，不能写同一句
        anchor_text = (c.get("anchor") or "").strip().rstrip("。，")
        for j, br in enumerate(c.get("break_rules", []) or []):
            if not isinstance(br, dict):
                continue
            eff = (br.get("effect") or "").strip().rstrip("。，")
            if anchor_text and eff and anchor_text == eff:
                warn(f"角色 {label} 的 anchor 与 break_rules[{j}].effect 写成同一句话：「{eff}」。anchor 是内心独白，effect 是说出口的台词，应分开写，否则配额永远用不出去")
        # 【2026-09-09】lifecycle 至少 2 个阶段才有意义
        lc = c.get("lifecycle")
        if lc is not None and isinstance(lc, list) and len(lc) < 2:
            warn(f"角色 {label} 的 lifecycle 只有 {len(lc)} 个阶段，至少 2 个阶段才有成长意义（单阶段不如不写）")

    # 代价系统契约
    cs = bible.get("cost_system")
    if isinstance(cs, dict):
        for key in REQUIRED_COST:
            if not _s(cs.get(key)) if key != "current" else not isinstance(cs.get(key), int):
                err(f"cost_system 缺少必填字段 {key}")
        if not _s(cs.get("meter_template")):
            warn("cost_system 缺 meter_template（缺省将从 device 名派生，如「天机反噬：{n}」）")
    elif cs is not None:
        err("cost_system 必须是对象")

    # 签名比喻契约（A型兼容 + B型扩展，2026-09 外脑裁决：seed 作为意象基调，不再强制原词/固定句）
    sm = bible.get("signature_metaphor")
    if isinstance(sm, dict):
        if not _s(sm.get("seed")):
            err("signature_metaphor 缺少必填字段 seed")
        # B型：seed + core_feeling/possible_forms/avoid 意象描述；A型：seed + mapping/metaphor_template 固定句。
        # 二者皆可，至少提供一种立意（mapping 或 metaphor_template 或 core_feeling），缺则提示。
        # core_feeling/possible_forms 为数组（非空判据），avoid 可为字符串或数组。
        def _nonempty(v):
            if isinstance(v, str):
                return v.strip() != ""
            if isinstance(v, list):
                return len(v) > 0
            return bool(v)
        has_btype = _nonempty(sm.get("core_feeling")) or _nonempty(sm.get("possible_forms"))
        if not _s(sm.get("mapping")) and not _s(sm.get("metaphor_template")) and not has_btype:
            err("signature_metaphor 缺 mapping/metaphor_template（A型）与 core_feeling/possible_forms（B型）任一，需至少提供一种意象立意")
        elif not _s(sm.get("metaphor_template")) and not has_btype:
            warn("signature_metaphor 缺 metaphor_template（plant_fallback 注入用；缺省则不注入固定比喻句）")
        # legacy_template：兼容保留，仅存档不主动注入（若同时存在 A 型模板且未关闭注入，fence 关闭时 enforce 不注入）
    elif sm is not None:
        err("signature_metaphor 必须是对象")

    # 反派败相
    vdb = bible.get("villain_down_beats")
    if isinstance(vdb, list):
        if not vdb:
            err("villain_down_beats 不能为空数组（写前机械兜底需要至少 1 句败相）")
        for i, b in enumerate(vdb):
            if not _s(b):
                err(f"villain_down_beats[{i}] 为空")
    if isinstance(bible.get("mechanics"), list) and len(bible.get("mechanics") or []) < 2:
        warn("mechanics 少于 2 条，硬事实锚的可呈现机制偏薄")

    # creative 创作原则（可选字段，V12.2 新增；存在则校验格式）
    creative = bible.get("creative")
    if creative is not None:
        if not isinstance(creative, dict):
            err("creative 必须是对象")
        else:
            if not _s(creative.get("core_question")):
                warn("creative 缺 core_question（全书核心命题=一句话脊柱，prealign E2 检查每章冲突是否服务于此命题）")
            if not _s(creative.get("emotional_engine")):
                warn("creative 缺 emotional_engine（情感发动机，主角情感弧线总纲）")
            if not _s(creative.get("goldfinger_interaction")):
                warn("creative 缺 goldfinger_interaction（金手指互动方式=金手指与主角的博弈方式，不是『给什么能力』而是『怎么互相塑造』；prealign G9 检查金手指是否推动剧情）")
            np = creative.get("novelty_pillars")
            if np is not None and not isinstance(np, list):
                err("creative.novelty_pillars 必须是数组")
            fp = creative.get("forbidden_patterns")
            if fp is not None and not isinstance(fp, list):
                err("creative.forbidden_patterns 必须是数组")

    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--book", required=True, help="书项目根（含 bible.json）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出 findings")
    args = ap.parse_args()

    path = os.path.join(args.book, "bible.json")
    if not os.path.exists(path):
        if args.json:
            print(json.dumps({"missing": True, "findings": [
                {"level": "ERROR", "message": f"bible.json 不存在：{path}"}
            ]}, ensure_ascii=False))
        else:
            print(f"ERROR: bible.json 不存在：{path}")
        return 2
    try:
        with open(path, "r", encoding="utf-8") as f:
            bible = json.load(f)
    except json.JSONDecodeError as exc:
        if args.json:
            print(json.dumps({"missing": False, "findings": [
                {"level": "ERROR", "message": f"bible.json 解析失败：{exc}"}
            ]}, ensure_ascii=False))
        else:
            print(f"ERROR: bible.json 解析失败：{exc}")
        return 2

    findings = _check_bible(bible)
    errors = [x for x in findings if x["level"] == "ERROR"]
    warnings = [x for x in findings if x["level"] == "WARNING"]
    if args.json:
        print(json.dumps({"missing": False, "errors": len(errors),
                          "warnings": len(warnings), "findings": findings},
                         ensure_ascii=False, indent=2))
    else:
        for x in findings:
            print(f"[{x['level']}] {x['message']}")
        if not findings:
            print("OK: bible.json 契约完整")
        else:
            print(f"共 {len(errors)} ERROR / {len(warnings)} WARNING")
    return 2 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
