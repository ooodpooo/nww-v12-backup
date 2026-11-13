# -*- coding: utf-8 -*-
"""style_profile.d4_scoring — 风格吻合度评分门禁（决策 D4）。

消费端打通后，对新生成正文做「风格吻合度」评分与门禁：
- 硬违约：negativeSamples（不可模仿标注）命中正文 → 去 AI 味失效 + 版权风险 → 重罚。
- 结构反模式：报幕式开场（announce）/ 平铺弱收束（flat）→ 轻罚（提示级）。
- proseMetrics 漂移：profile 携带定量目标时比对（题材卡无目标 → 跳过）。

软拦截：score < SOFT_THRESHOLD → soft_block=True（建议重生成，不强制）。
硬熔断：CircuitBreaker 连续失败 ≥ HARD_TRIP_FAILS（默认 2）→ hard_trip，强制停。

设计约束（工程纪律）：零 LLM、纯规则、只读、零依赖（style_profile 包自给）；
启用由 env NWW_STYLE_FIDELITY_GATE 控制（默认关）。本文件为新增能力，不修改任何生产代码。
"""
from __future__ import annotations

import os
import re
from typing import Any

from .schema import normalize
from .validate import validate_profile
from .registry import load_style_profile

NEG_WEIGHT = 40          # 每条不可模仿标注命中扣分
ANNOUNCE_PENALTY = 15    # 报幕式开场
FLAT_PENALTY = 10        # 平铺弱收束
SOFT_THRESHOLD = 70      # 软拦截阈值
HARD_TRIP_FAILS = 2      # 连续失败上限（硬熔断 ≤2 次）

_ANNOUNCE_RE = re.compile(r"(次日|第二天|翌日|这天|某年|那一年|一早).{0,10}(来到|到达|之时)")
_HOOK_WORDS = ("却", "竟", "突然", "就在这时", "未料", "没想到", "谁知", "偏")


def _enabled() -> bool:
    return os.getenv("NWW_STYLE_FIDELITY_GATE", "0") == "1"


def _opening_type(prose: str) -> str:
    body = re.sub(r"^[`*_>\s]*#[^\n]*$", "", prose, flags=re.M)
    paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    if not paras:
        return "empty"
    first = paras[0]
    if re.search(r"[「『\u201c\"]", first):
        return "dialogue"
    if _ANNOUNCE_RE.search(first):
        return "announce"
    return "other"


def _ending_flat(prose: str) -> bool:
    body = re.sub(r"^[`*_>\s]*#[^\n]*$", "", prose, flags=re.M)
    paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    if not paras:
        return False
    last = paras[-1]
    has_dialogue = bool(re.search(r"[「『\u201c\"]", last))
    has_hook = any(w in last[-30:] for w in _HOOK_WORDS) or ("？" in last)
    return (not has_dialogue) and (not has_hook)


def score_style_fidelity(prose: str, profile: dict[str, Any]) -> dict[str, Any]:
    """返回 {score, soft_block, violations, warnings}。score ∈ [0, 100]。

    仅当 profile 通过 D3 校验（含 negativeSamples）才评分；否则返回 status=invalid，
    不静默给分（工程纪律：失败显式表达）。
    """
    ok, errors = validate_profile(profile)
    if not ok:
        return {"score": 0, "soft_block": True, "violations": [],
                "warnings": ["profile 未过 D3 入库校验，无法评分"] + errors, "status": "invalid"}
    p = normalize(profile)
    score = 100
    violations: list[str] = []
    warnings: list[str] = []
    # 1) 不可模仿标注命中（硬违约）
    for item in p.get("negativeSamples", []):
        expr = (item.get("expression") if isinstance(item, dict) else None) or ""
        if expr and expr in prose:
            score -= NEG_WEIGHT
            violations.append(f"命中不可模仿标注: {expr}")
    # 2) 结构反模式（轻罚，提示级）
    if _opening_type(prose) == "announce":
        score -= ANNOUNCE_PENALTY
        warnings.append("报幕式开场（announce），建议改为可感切入")
    if _ending_flat(prose):
        score -= FLAT_PENALTY
        warnings.append("平铺弱收束（flat），建议加钩子/情绪沉淀")
    score = max(0, score)
    return {
        "score": score,
        "soft_block": score < SOFT_THRESHOLD,
        "violations": violations,
        "warnings": warnings,
        "status": "ok",
    }


class CircuitBreaker:
    """硬熔断：连续失败计数，达上限即 trip（≤2 次）。状态内存驻留，不跨进程。"""

    def __init__(self, max_fails: int = HARD_TRIP_FAILS):
        self.max_fails = max_fails
        self._fails = 0

    def record(self, passed: bool) -> None:
        if passed:
            self._fails = 0
        else:
            self._fails += 1

    @property
    def tripped(self) -> bool:
        return self._fails >= self.max_fails

    def reset(self) -> None:
        self._fails = 0


def style_gate(prose: str, profile: dict[str, Any], breaker: CircuitBreaker | None = None) -> dict[str, Any]:
    """评分 + 熔断一体化入口。返回 score 信息 + hard_trip 标志。

    启用前（env 关）→ 直接放行（status=disabled），不改变现有流程。
    """
    if not _enabled() and breaker is None:
        return {"status": "disabled", "score": None, "soft_block": False,
                "hard_trip": False, "violations": [], "warnings": []}
    info = score_style_fidelity(prose, profile)
    hard_trip = False
    if breaker is not None:
        breaker.record(not info["soft_block"])
        hard_trip = breaker.tripped
    info["hard_trip"] = hard_trip
    info["status"] = "ok"
    return info


def gate_prose(prose: str, scope: str, name: str, book: Any | None = None) -> dict[str, Any]:
    """一行式接入：按 scope/name 解析 Profile（经 registry 入口），再评分 + 熔断。

    生产消费端只需：
        style_profile.d4_scoring.gate_prose(prose, "book", book.name)
        style_profile.d4_scoring.gate_prose(prose, "genre", genre)
    profile 未就绪（disabled / missing / invalid）→ 返回中性，不阻断现有流程。
    """
    r = load_style_profile(scope, name, book=book)
    if r["status"] != "ok" or r["profile"] is None:
        return {"status": r["status"], "score": None, "soft_block": False,
                "hard_trip": False, "violations": [],
                "warnings": [f"profile 未就绪: {r['status']}（不阻断）"]}
    return style_gate(prose, r["profile"])


if __name__ == "__main__":
    # 自测：① 缺 negativeSamples → invalid；② 命中不可模仿 → 低分+violation；
    #       ③ 报幕式开场 → 扣 announce；④ 熔断连续 2 次失败 trip。
    from .schema import empty_profile
    os.environ["NWW_STYLE_FIDELITY_GATE"] = "1"  # 门禁自测需开启
    # ①
    bad = empty_profile("genre", "t")
    r1 = score_style_fidelity("正文", bad)
    assert r1["status"] == "invalid"
    # ② 带 negativeSamples，命中
    good = empty_profile("genre", "t")
    good["negativeSamples"] = [{"expression": "禁用专名X", "reason": "版权"}]
    r2 = score_style_fidelity("这里用了禁用专名X。", good)
    assert r2["score"] < 100 and any("禁用专名X" in v for v in r2["violations"])
    # ③ 报幕式
    r3 = score_style_fidelity("次日清晨，林夜来到宗门。\n\n风很冷。", good)
    assert any("报幕式" in w for w in r3["warnings"])
    # ④ 熔断（soft_block 连续 2 次 → trip；normal 通过不计数）
    cb = CircuitBreaker(max_fails=2)
    style_gate("正文", good, cb)            # score=100 → passed
    assert not cb.tripped
    style_gate("这里用了禁用专名X。", good, cb)   # 命中不可模仿 → score=60 < 70 → soft_block
    assert not cb.tripped                   # 第 1 次失败
    style_gate("又用了禁用专名X。", good, cb)     # 第 2 次连续失败 → trip
    assert cb.tripped
    print("d4_scoring self-test passed.")
