#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nww_style_fingerprint.py — 章节风格定量指纹（句长/段落节奏/对话占比/比喻密度）。

设计（2026-09-05，资产质量批次 Phase D）：
- 「文风漂移」之前只能靠语感说。本脚本把一章的风格变成可比数字：句长分布
  （均值/中位/P90/CV）、段落节奏（均长/短段占比/段长CV）、对话占比、
  比喻密度、段均句数。跨章对比时数字说话，不猜。
- 纯确定性、零 LLM、只读正文；--save 写 追踪/风格指纹.json（按章归档），
  供卷级疲劳/漂移分析取数。不修改正文、不阻断任何流程。
- 与 nww_cross_chapter_check.py 的段落密度漂移检测互补：那边只报警，
  这里给出完整的可比指纹。

用法：
    python nww_style_fingerprint.py --book BOOK --chapter N [--save] [--all]
    --chapter N   只算第 N 章
    --all         书内全部已有章节（忽略 --chapter）
    --save        把结果并入 追踪/风格指纹.json（缺省只打印 stdout）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

_SENT_SPLIT_RE = re.compile(r"[。！？!?…]+")
_DIALOGUE_RE = re.compile(r"「[^」]*」|『[^』]*』|\u201c[^\u201d]*\u201d|\"[^\"]*\"")
_SIMILE_RE = re.compile(r"(像[^。！？\n]{0,12}一样|如同|仿佛|好似|宛如|犹如|一般)")
_SHORT_PARA_CHARS = 30
_SHORT_SENT_CHARS = 12


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(ratio * (len(ordered) - 1))))
    return ordered[index]


def _cv(values: list[float]) -> float:
    """变异系数 = std/mean（总体标准差）；空/零均值 → 0。"""
    if not values:
        return 0.0
    avg = _mean(values)
    if avg == 0:
        return 0.0
    variance = sum((v - avg) ** 2 for v in values) / len(values)
    return (variance ** 0.5) / avg


def _round_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in metrics.items()}


def fingerprint_prose(prose: str) -> dict[str, Any]:
    """把一章正文变成风格指纹（纯数字，可与任何章对比）。"""
    # 标题行不是正文：剔除后再统计，避免污染句长/段落数
    # 行首允许 markdown 装饰（` * _ >）——writer 偶发的 `` `# 第N章 x` `` 也要剔除，
    # 否则标题会混进句长/句数统计（实测 2026-09-13 第028章）。
    body = re.sub(r"^[`*_>\s]*#[^\n]*$", "", prose, flags=re.M)
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    # 段内再按换行合并：Markdown 正文里同段可能被软换行拆开
    merged = [re.sub(r"\s*\n\s*", "", p) for p in paragraphs]
    # 句长只数可见字符（剔除句内空白）
    sentences = [re.sub(r"\s+", "", s) for s in _SENT_SPLIT_RE.split(body)]
    sentences = [s for s in sentences if s]

    sent_lens = [len(s) for s in sentences]
    para_lens = [len(p) for p in merged]
    dialogue_spans = _DIALOGUE_RE.findall(prose)
    dialogue_chars = sum(len(span) for span in dialogue_spans)
    total_chars = len(re.sub(r"\s", "", prose))
    dialogue_paras = sum(1 for p in merged if _DIALOGUE_RE.match(p))

    simile_count = len(_SIMILE_RE.findall(prose))
    return _round_metrics({
        "chars": total_chars,
        "para_count": len(merged),
        "sentence_count": len(sentences),
        "sentence_len_mean": _mean([float(x) for x in sent_lens]),
        "sentence_len_median": _median([float(x) for x in sent_lens]),
        "sentence_len_p90": _percentile([float(x) for x in sent_lens], 0.9),
        "sentence_len_cv": _cv([float(x) for x in sent_lens]),
        "short_sentence_ratio": (
            sum(1 for x in sent_lens if x <= _SHORT_SENT_CHARS) / len(sent_lens) if sent_lens else 0.0
        ),
        "para_len_mean": _mean([float(x) for x in para_lens]),
        "para_len_max": max(para_lens) if para_lens else 0,
        "para_len_cv": _cv([float(x) for x in para_lens]),
        "short_para_ratio": (
            sum(1 for x in para_lens if x <= _SHORT_PARA_CHARS) / len(para_lens) if para_lens else 0.0
        ),
        "dialogue_char_ratio": dialogue_chars / total_chars if total_chars else 0.0,
        "dialogue_para_ratio": dialogue_paras / len(merged) if merged else 0.0,
        "simile_per_kilo_chars": simile_count / total_chars * 1000 if total_chars else 0.0,
        "sentences_per_para": len(sentences) / len(merged) if merged else 0.0,
    })


def _is_non_prose(p: Path) -> bool:
    """路径是否属于「非正文」辅助目录。

    `正文/**/第*章*.md` 会**穿透** `_review_snapshots/`，而快照文件名与章节同形
    （`第027章_round1.md`）→ 无法按文件名识别，只能按路径部件排除。
    实测 2026-09-13：glob 顺序下 `_review_snapshots`（`_`）排在「第」之前，
    `candidates[0]` 恒定命中快照 → 风格指纹算的是**评审快照**而非正文
    （第27章正文 3320 字，指纹却报 3331 字）。
    与 nww_track_commit._is_non_prose 同口径。
    """
    return any(d in p.parts for d in ("_review_snapshots", "_archive")) or "_原稿_" in p.name


def resolve_chapter_file(book: Path, chapter: int) -> Path | None:
    """正文定位：正文/ 下任意层级的 第N章*.md（保留为公共工具函数）。"""
    candidates = [
        p for p in book.glob("正文/**/第*章*.md")
        if _chapter_number(p.name) == chapter and not _is_non_prose(p)
    ]
    return candidates[0] if candidates else None


def _chapter_number(name: str) -> int | None:
    m = re.match(r"第0*(\d+)章", name)
    return int(m.group(1)) if m else None


def collect_fingerprints(book: Path) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    # 同一章可能有「正文 + 评审快照」多个同形文件：正文优先，快照不得覆盖正文的指纹。
    prose_files = sorted(
        (p for p in book.glob("正文/**/第*章*.md") if not _is_non_prose(p)),
        key=lambda p: p.as_posix(),
    )
    for path in prose_files:
        number = _chapter_number(path.name)
        if number is None:
            continue
        prose = path.read_text(encoding="utf-8")
        result[number] = fingerprint_prose(prose)
    return dict(sorted(result.items()))


def evaluate_style_drift(fingerprints: dict[int, dict[str, Any]], chapter: int) -> dict[str, Any]:
    """2026-10-26 风格指纹两级漂移评估（修复计划 P1-3）：
    - 偏离 50%–80%：level = "ADVISORY"
    - 偏离 >80% 或 连续 ≥3 章同向偏离（>30%）：level = "ALERT_CONFIRM"（写前门禁显式告警+需人工确认）
    - 其余：level = "NORMAL"
    """
    prior_nums = sorted(n for n in fingerprints if n < chapter)
    if len(prior_nums) < 3 or chapter not in fingerprints:
        return {"level": "NORMAL", "items": [], "requires_confirm": False}
    prior = {n: fingerprints[n] for n in prior_nums}
    current = fingerprints[chapter]
    items: list[dict[str, Any]] = []
    requires_confirm = False

    for key, label in (
        ("sentence_len_mean", "平均句长"),
        ("dialogue_char_ratio", "对话占比"),
        ("para_len_mean", "平均段长"),
    ):
        baseline = _mean([float(fp[key]) for fp in prior.values()])
        if baseline <= 0:
            continue
        cur_val = float(current[key])
        signed_delta = (cur_val - baseline) / baseline
        abs_delta = abs(signed_delta)

        # 检查是否连续 3 章（prior[-2], prior[-1], current）同向偏离 > 30%
        streak_3 = False
        if len(prior_nums) >= 2:
            recent_3 = [float(prior[prior_nums[-2]][key]), float(prior[prior_nums[-1]][key]), cur_val]
            diffs = [(v - baseline) / baseline for v in recent_3]
            if all(d > 0.30 for d in diffs) or all(d < -0.30 for d in diffs):
                streak_3 = True

        if abs_delta > 0.80 or streak_3:
            lvl = "ALERT_CONFIRM"
            requires_confirm = True
        elif abs_delta > 0.50:
            lvl = "ADVISORY"
        else:
            continue
        items.append({
            "metric": key,
            "label": label,
            "current": current[key],
            "baseline": round(baseline, 2),
            "deviation_pct": round(abs_delta * 100),
            "direction": "up" if signed_delta >= 0 else "down",
            "consecutive_3ch_streak": streak_3,
            "level": lvl,
        })

    overall = "ALERT_CONFIRM" if requires_confirm else ("ADVISORY" if items else "NORMAL")
    return {"level": overall, "items": items, "requires_confirm": requires_confirm}


def trend_note(fingerprints: dict[int, dict[str, Any]], chapter: int) -> str | None:
    """与之前 ≥3 章的均值对比：支持 ADVISORY（50-80%）与 ALERT_CONFIRM（>80% 或连续3章同向漂移）两级输出。"""
    drift = evaluate_style_drift(fingerprints, chapter)
    if not drift["items"]:
        return None
    prior_count = len([n for n in fingerprints if n < chapter])
    notes: list[str] = []
    for it in drift["items"]:
        tag = "[需人工确认]" if it["level"] == "ALERT_CONFIRM" else "[提示]"
        streak_str = "（连续3章同向漂移）" if it.get("consecutive_3ch_streak") else ""
        notes.append(
            f"{tag}{it['label']} {it['current']} 对此前 {prior_count} 章均值 {it['baseline']} 偏离 {it['deviation_pct']}%{streak_str}"
        )
    prefix = "ALERT" if drift["requires_confirm"] else "NOTE"
    return f"{prefix}: 第{chapter}章风格指纹漂移（只提示不判定）：" + "；".join(notes)


def save_fingerprints(book: Path, fingerprints: dict[int, dict[str, Any]]) -> Path:
    target = book / "追踪" / "风格指纹.json"
    try:
        existing = json.loads(target.read_text(encoding="utf-8"))
        chapters = existing.get("chapters", {})
    except (OSError, UnicodeError, json.JSONDecodeError):
        chapters = {}
    merged = {**{int(k): v for k, v in chapters.items()}, **fingerprints}
    payload = json.dumps(
        {"schema_version": 1, "chapters": {str(n): merged[n] for n in sorted(merged)}},
        ensure_ascii=False, indent=2, sort_keys=True,
    ) + "\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload, encoding="utf-8")
    return target


# ── 叙事逻辑指纹（D1 首波三指标：规则可提取、零 LLM、零依存树）────────────
# 设计（2026-10-21，外脑双脑共识 D1）：场景切入 / 衔接配比 / 段章收束，
# 均为确定性规则提取，不依赖因果展开 / 信息释放 / 视角跳跃（暂缓，待解析器支持）。
# 与 fingerprint_prose（定量）互补：那边给数字，这边给「写法结构」可比指标。
_TRANSITION_MARKERS = (
    "与此同时", "就在这时", "话音未落", "回到", "另一边", "次日", "第二天", "过了",
    "不久", "此时", "转眼", "突然", "这时", "却说", "且说", "话说", "当夜", "半晌", "须臾",
)
_TIME_JUMP_MARKERS = (
    "次日", "第二天", "过了", "半月后", "月后", "年后", "三年后", "数日后", "几天后",
    "转年", "翌日", "半个月后", "一年后",
)
_OPEN_ENV_WORDS = (
    "天", "月", "风", "雨", "山", "雾", "光", "夜", "晨", "房", "院", "雪", "海", "林",
    "街", "城", "殿", "帐", "江", "河", "云",
)
_OPEN_INTERNAL_WORDS = ("记得", "那年", "当年", "想起来", "脑海", "回忆", "往事", "想起")
_ANNOUNCE_WORDS = ("次日", "第二天", "清晨", "这天", "某年", "那一年", "一早")
_HOOK_MARKERS = ("却", "竟", "突然", "就在这时", "未料", "没想到", "谁知", "偏")


def _first_paragraph(prose: str) -> str:
    body = re.sub(r"^[`*_>\s]*#[^\n]*$", "", prose, flags=re.M)
    paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    return paras[0] if paras else ""


def _last_paragraph(prose: str) -> str:
    body = re.sub(r"^[`*_>\s]*#[^\n]*$", "", prose, flags=re.M)
    paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    return paras[-1] if paras else ""


def _classify_opening(opening: str) -> dict[str, Any]:
    """场景切入类型：dialogue / internal / environment / action / announce（报幕式，负向）。"""
    if not opening:
        return {"dominant": "empty", "candidates": {}, "flagged_announce": False}
    if re.search(r"[「『“”\"]", opening):
        dom = "dialogue"
    elif any(w in opening for w in _OPEN_INTERNAL_WORDS):
        dom = "internal"
    elif any(w in opening for w in _ANNOUNCE_WORDS) and (
        "来到" in opening or "到达" in opening or "之时" in opening
    ):
        dom = "announce"
    elif any(w in opening for w in _OPEN_ENV_WORDS):
        dom = "environment"
    else:
        dom = "action"
    return {"dominant": dom, "candidates": {dom: 1.0}, "flagged_announce": dom == "announce"}


def _classify_transitions(paragraphs: list[str]) -> dict[str, Any]:
    """衔接配比：hard（硬切）/ soft（过渡词）/ time_jump（时间跳）占比。"""
    if len(paragraphs) < 2:
        return {"transitions": 0, "hard_ratio": 0.0, "soft_ratio": 0.0,
                "time_jump_ratio": 0.0, "dominant": "none"}
    hard = soft = time_jump = 0
    for prev, cur in zip(paragraphs, paragraphs[1:]):  # noqa: B007 (prev 仅占位)
        if any(m in cur for m in _TIME_JUMP_MARKERS):
            time_jump += 1
        elif any(m in cur for m in _TRANSITION_MARKERS):
            soft += 1
        else:
            hard += 1
    total = len(paragraphs) - 1
    return {
        "transitions": total,
        "hard_ratio": round(hard / total, 2),
        "soft_ratio": round(soft / total, 2),
        "time_jump_ratio": round(time_jump / total, 2),
        "dominant": "hard" if hard >= soft and hard >= time_jump else
                    ("time_jump" if time_jump >= soft else "soft"),
    }


def _classify_ending(ending: str) -> dict[str, Any]:
    """段章收束类型：hook（悬念钩子）/ dialogue / settled（情绪沉淀）/ action（平铺，弱）。"""
    if not ending:
        return {"dominant": "empty", "candidates": {}, "flagged_flat": False}
    has_dialogue = bool(re.search(r"[「『“”\"]", ending))
    if "？" in ending or "?" in ending:
        dom = "hook"
    elif has_dialogue:
        dom = "dialogue"
    elif any(m in ending[-30:] for m in _HOOK_MARKERS):
        dom = "hook"
    elif any(w in ending for w in _OPEN_ENV_WORDS):
        dom = "settled"
    else:
        dom = "action"
    flagged_flat = dom == "action" and not has_dialogue and not any(
        m in ending for m in _HOOK_MARKERS
    )
    return {"dominant": dom, "candidates": {dom: 1.0}, "flagged_flat": flagged_flat}


_CAUSE_MARKERS = ("因为", "由于", "既然", "只因", "之所以", "原是因为")
_EFFECT_MARKERS = ("所以", "因此", "以至于", "导致", "这才", "怪不得", "难怪")
_RULE_EXPLAIN_WORDS = ("规则", "规矩", "禁制", "阵法", "境界", "代价", "反噬", "制度", "律例", "天机", "灵气")


def _classify_causal_order(paragraphs: list[str]) -> dict[str, Any]:
    """行文逻辑维度4：因果展开顺序（先果后因/现象先行 vs 先因后果），附原文片段证据。"""
    cause_first = 0
    effect_first = 0
    evidence_samples: list[dict[str, str]] = []
    for p in paragraphs:
        c_pos = min((p.find(w) for w in _CAUSE_MARKERS if w in p), default=-1)
        e_pos = min((p.find(w) for w in _EFFECT_MARKERS if w in p), default=-1)
        if c_pos >= 0 and e_pos >= 0:
            if c_pos < e_pos:
                cause_first += 1
                if len(evidence_samples) < 3:
                    evidence_samples.append({"order": "cause_first", "quote": p[:60]})
            else:
                effect_first += 1
                if len(evidence_samples) < 3:
                    evidence_samples.append({"order": "effect_first", "quote": p[:60]})
        elif any(w in p for w in ("怪不得", "难怪", "这才明白", "原来是因为")):
            effect_first += 1
            if len(evidence_samples) < 3:
                evidence_samples.append({"order": "effect_first", "quote": p[:60]})
    total = cause_first + effect_first
    return {
        "total_causal_segments": total,
        "cause_first_ratio": round(cause_first / total, 2) if total else 0.0,
        "effect_first_ratio": round(effect_first / total, 2) if total else 0.0,
        "dominant": ("effect_first" if effect_first > cause_first
                     else ("cause_first" if cause_first > 0 else "implicit_action")),
        "evidence": evidence_samples,
    }


def _classify_explanation_path(paragraphs: list[str]) -> dict[str, Any]:
    """行文逻辑维度5：设定/规则解释路径（对话/动作自显 vs 旁白直陈占比），附原文片段证据。"""
    dialogue_or_action = 0
    narrator_direct = 0
    evidence_samples: list[dict[str, str]] = []
    for p in paragraphs:
        if not any(w in p for w in _RULE_EXPLAIN_WORDS):
            continue
        has_dlg = bool(re.search(r"[「『“\"]", p))
        if has_dlg:
            dialogue_or_action += 1
            if len(evidence_samples) < 3:
                evidence_samples.append({"path": "dialogue_action", "quote": p[:60]})
        else:
            narrator_direct += 1
            if len(evidence_samples) < 3:
                evidence_samples.append({"path": "narrator_direct", "quote": p[:60]})
    total = dialogue_or_action + narrator_direct
    return {
        "total_rule_paragraphs": total,
        "dialogue_action_ratio": round(dialogue_or_action / total, 2) if total else 0.0,
        "narrator_direct_ratio": round(narrator_direct / total, 2) if total else 0.0,
        "dominant": ("dialogue_action" if dialogue_or_action >= narrator_direct and total > 0
                     else ("narrator_direct" if total > 0 else "none")),
        "evidence": evidence_samples,
    }


def fingerprint_narrative_logic(prose: str, *, extended: bool = False) -> dict[str, Any]:
    """叙事逻辑指标（D1 基础三指标 + extended=True 五维完整版）：
    基础三指标（默认，保持 D1 键集合契约）：sceneOpeningTypes / transitionRatios / chapterEndingTypes
    扩展五维（extended=True）：增加 causalOrder（因果展开顺序）与 explanationPath（设定解释路径，均带原文证据）。
    """
    body = re.sub(r"^[`*_>\s]*#[^\n]*$", "", prose, flags=re.M)
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    base = {
        "sceneOpeningTypes": _classify_opening(_first_paragraph(prose)),
        "transitionRatios": _classify_transitions(paragraphs),
        "chapterEndingTypes": _classify_ending(_last_paragraph(prose)),
    }
    if extended:
        base["causalOrder"] = _classify_causal_order(paragraphs)
        base["explanationPath"] = _classify_explanation_path(paragraphs)
    return base


def fingerprint_full(prose: str, *, extended: bool = True) -> dict[str, Any]:
    """组合指纹：proseMetrics（定量数字）+ narrativeLogic（叙事逻辑五维结构）。"""
    return {
        "proseMetrics": fingerprint_prose(prose),
        "narrativeLogic": fingerprint_narrative_logic(prose, extended=extended),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="章节风格定量指纹（句长/段落/对话/比喻，只读不改稿）")
    parser.add_argument("--book", type=Path, required=True, help="book project root containing 正文/")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--chapter", type=int, help="只计算第 N 章")
    group.add_argument("--all", action="store_true", help="计算书内全部章节")
    parser.add_argument("--save", action="store_true", help="并入 追踪/风格指纹.json（缺省只打印）")
    args = parser.parse_args()

    if args.all:
        fingerprints = collect_fingerprints(args.book)
        note = None
    else:
        all_fp = collect_fingerprints(args.book)
        if args.chapter not in all_fp:
            print(f"ERROR: 无法定位第 {args.chapter} 章正文", file=sys.stderr)
            return 2
        fingerprints = {args.chapter: all_fp[args.chapter]}
        # 漂移基线需要前章数据：全量重算一次（只读、确定性，量级为单书章节数）
        note = trend_note(all_fp, args.chapter)

    payload = json.dumps(
        {"chapters": {str(n): fingerprints[n] for n in sorted(fingerprints)}},
        ensure_ascii=False, indent=2, sort_keys=True,
    ) + "\n"
    sys.stdout.buffer.write(payload.encode("utf-8"))

    if note:
        print(note)
    if args.save:
        target = save_fingerprints(args.book, fingerprints)
        print(f"saved: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
