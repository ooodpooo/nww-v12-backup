#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nww_balance_plan.py — 篇幅平衡 v1 的纯函数模块（T20）。

约束（规格 §2.1）：
- 零 LLM、零 IO、无副作用：所有函数只依赖入参与标准库。
- 只做「区间解析 / 缺口计算 / 方案校验 / 确定性打分 / 择优 / 改动范围启发式」，
  不读取文件、不调用模型、不修改入参。
- 权威副本位于 ``skills/nww-novel-skill/scripts/``；``engine/scripts/`` 为逐字节
  等价的回退副本，二者必须完全一致（tests/test_balance_plan.py 有自检）。

与 pipeline 的契约边界：
- ``validate_plan`` 的违规清单是「确定性拒绝理由」，交由上层打回重生成；
- ``score_plan`` 的分数只由可核实信号构成（缺口命中度、方案完整度、风险项、
  保护级合规），不包含任何「文笔/读起来更好」的主观判断；
- ``select_plan`` 的语义是质量优先：硬过滤 → 质量分降序 → 仅当质量分差
  ≤ ``tie`` 时用 ``residual_gap`` 决胜。
"""
from __future__ import annotations

import difflib
import math
import re

SCHEMA = "nww_balance_plan/v1"
ACTIONS = {"expand", "condense"}
PROTECTION_LEVELS = {"L0", "L1", "L2"}

__all__ = [
    "SCHEMA",
    "ACTIONS",
    "PROTECTION_LEVELS",
    "parse_band",
    "compute_gap",
    "compute_residual_gap",
    "validate_plan",
    "score_plan",
    "select_plan",
    "diff_scope_ok",
]

# 范围格式：2500-2750（兼容全角破折号 / ~ / 至，以及结尾「字」）。
_RANGE_RE = re.compile(
    r"^\s*(\d+)\s*[-~—–至]\s*(\d+)\s*(?:字|words?)?\s*$", re.IGNORECASE
)
# 单值格式：2500（兼容结尾「字」）。
_SINGLE_RE = re.compile(r"^\s*(\d+)\s*(?:字|words?)?\s*$", re.IGNORECASE)

# 方案对象样例会带 schema；缺失或错配都视为非法（规格 §2.2）。

_REQUIRED_OP_FIELDS = (
    "beat_id",
    "block_id",
    "action",
    "where",
    "how",
    "expected_delta",
    "protection_level",
    "constraints",
)

# 禁区显式标记：规格 §2.2 的方案对象没有独立字段，这里只认「显式声明」，
# 不对 where/how 散文做关键词猜测，避免误伤（见任务报告中的契约歧义裁定）。
_FORBIDDEN_FLAGS = ("forbidden_hit", "hits_forbidden", "touches_forbidden")
_FORBIDDEN_ZONE_ALIASES = {
    "foreshadow": "伏笔",
    "chapter_hook": "章尾钩子",
    "emotion_peak": "情绪峰值",
    "core_setting": "核心设定",
    "ending": "结局",
}
_TRUE_STRINGS = {"1", "true", "yes", "y", "on"}

# score_plan 的固定权重（确定性，不随环境变化）。
_QUALITY_WEIGHTS = {
    "gap_fit": 0.45,
    "completeness": 0.25,
    "risk": 0.15,
    "protection": 0.15,
}

_DEFAULT_TIE = 5.0
_DEFAULT_QUALITY_WEIGHT = 0.7
_DEFAULT_MIN_RATIO = 0.3
_CHANGE_TOLERANCE_PER_OP = 2
_OP_COUNT_RISK_THRESHOLD = 5


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def _is_int(value: object) -> bool:
    """严格整数判断：排除 bool（bool 是 int 的子类，会污染增量语义）。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _as_int(value: object, default: int = 0) -> int:
    """尽力转 int；不可转时返回 default，避免上层因脏数据抛异常。"""
    if _is_int(value):
        return int(value)
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    return default


def _as_float(value: object, default: float | None = None) -> float | None:
    """尽力转有限 float；bool / 非数值 / nan / inf 返回 default。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        number = float(value)
        if math.isfinite(number):
            return number
    return default


def _nonempty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _band_valid(lo: int, hi: int) -> bool:
    """``(0,0)`` 表示「无基础区间」，此时不参与残差/缺口计算。"""
    return lo > 0 and hi >= lo


def _delta_of(op: object) -> int | None:
    """读取 operation 的 expected_delta；非严格整数返回 None（用于校验）。"""
    if not isinstance(op, dict):
        return None
    value = op.get("expected_delta")
    return int(value) if _is_int(value) else None


def _sum_delta(ops: list) -> int:
    """合法整数增量求和；脏值按 0 计（类型问题由 validate_plan 报出）。"""
    total = 0
    for op in ops:
        delta = _delta_of(op)
        if delta is not None:
            total += delta
    return total


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _dedup(items: list[str]) -> list[str]:
    """去重但保留首次出现顺序，保证违规清单稳定可读。"""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


# ---------------------------------------------------------------------------
# R1：基础字数区间
# ---------------------------------------------------------------------------


def parse_band(target, *, band_pct: float) -> tuple[int, int]:
    """解析细纲字数目标为基础区间。

    - 范围 ``"2500-2750"`` → ``(2500, 2750)``，**不套 ±band_pct**；
    - 单值 ``2500`` → ``(int(n*(1-p)), int(n*(1+p)))``；
    - 非法 / 无目标 / band_pct 越界 → ``(0, 0)``（上层据此不自动扩缩）。

    ``band_pct`` 必须落在 ``[0, 1)``；越界按非法处理（fail-closed，不猜）。
    """
    if isinstance(target, bool) or target is None:
        return (0, 0)

    number: int | None = None
    if _is_int(target):
        number = int(target)
    elif isinstance(target, float) and math.isfinite(target):
        number = int(target)
    elif isinstance(target, str):
        text = target.strip()
        if not text:
            return (0, 0)
        range_match = _RANGE_RE.match(text)
        if range_match:
            lo = int(range_match.group(1))
            hi = int(range_match.group(2))
            if lo <= 0 or hi <= 0:
                return (0, 0)
            # 逆序范围归一化，避免下游拿到 lo > hi。
            return (min(lo, hi), max(lo, hi))
        single_match = _SINGLE_RE.match(text)
        if not single_match:
            return (0, 0)
        number = int(single_match.group(1))
    else:
        return (0, 0)

    if number is None or number <= 0:
        return (0, 0)

    pct = _as_float(band_pct)
    if pct is None or not (0.0 <= pct < 1.0):
        return (0, 0)

    lo = int(number * (1.0 - pct))
    hi = int(number * (1.0 + pct))
    if hi < lo:
        return (0, 0)
    return (lo, hi)


# ---------------------------------------------------------------------------
# 缺口
# ---------------------------------------------------------------------------


def compute_gap(actual: int, lo: int, hi: int) -> int:
    """未进带的最小纠偏量（无符号）；带内或区间无效 → 0。"""
    current = _as_int(actual)
    low = _as_int(lo)
    high = _as_int(hi)
    if not _band_valid(low, high):
        return 0
    if current < low:
        return low - current
    if current > high:
        return current - high
    return 0


def compute_residual_gap(actual: int, ops: list[dict], lo: int, hi: int) -> int:
    """方向无关的预测残差。

    ``predicted = actual + Σexpected_delta``；
    ``residual = max(lo - predicted, 0, predicted - hi)``。

    该公式同时覆盖「expand 过冲到 over」与「condense 过冲到 under」。
    区间无效、actual 不可解析时返回 0（上层已由 R1 提前退出）。
    """
    current = _as_int(actual)
    low = _as_int(lo)
    high = _as_int(hi)
    if not _band_valid(low, high):
        return 0

    ops_list = ops if isinstance(ops, list) else []
    predicted = current + _sum_delta(ops_list)
    return max(low - predicted, 0, predicted - high)


# ---------------------------------------------------------------------------
# R3：确定性方案校验
# ---------------------------------------------------------------------------


def _forbidden_zone(op: dict) -> str:
    """返回命中的禁区名称；未显式命中返回空串。

    只认显式标记 / zone 字段，**不扫描 where/how 散文**：真实细纲常写
    「避开伏笔」，散文扫描会把正常方案误判为违规。
    """
    for flag in _FORBIDDEN_FLAGS:
        if flag in op:
            value = op.get(flag)
            if value is True:
                return "显式标记"
            if isinstance(value, str) and value.strip().lower() in _TRUE_STRINGS:
                return "显式标记"

    for field in ("protected_zone", "zone", "forbidden_zone"):
        raw = op.get(field)
        if not isinstance(raw, str):
            continue
        key = raw.strip().lower()
        if key in _FORBIDDEN_ZONE_ALIASES:
            return _FORBIDDEN_ZONE_ALIASES[key]
        for alias, chinese in _FORBIDDEN_ZONE_ALIASES.items():
            if alias and alias in key:
                return chinese
            if chinese and chinese in raw:
                return chinese

    zones = op.get("forbidden_zones", op.get("zones"))
    if isinstance(zones, (list, tuple)):
        hits: list[str] = []
        for zone in zones:
            if not isinstance(zone, str):
                continue
            key = zone.strip().lower()
            if key in _FORBIDDEN_ZONE_ALIASES:
                hits.append(_FORBIDDEN_ZONE_ALIASES[key])
                continue
            for alias, chinese in _FORBIDDEN_ZONE_ALIASES.items():
                if chinese in zone or alias in key:
                    hits.append(chinese)
                    break
        if hits:
            return "、".join(_dedup(hits))
    return ""


def validate_plan(
    plan: dict,
    *,
    actual: int,
    lo: int,
    hi: int,
    beat_ids: set[str],
    block_ids: set[str],
    l2_beat_ids: set[str],
) -> list[str]:
    """返回违规清单（空 = 合法），覆盖规格 R3 全部确定性条款。

    校验项：
    1. 方案结构：schema / direction / operations / residual_gap；
    2. operation 必填字段与类型（expected_delta 必须是严格整数）；
    3. beat_id / block_id 必须属于本章（调用方传空集合时跳过归属检查，
       供 ``select_plan`` 在缺少章节上下文时做硬过滤）；
    4. 方向一致性：expand 只增、condense 只减，Σexpected_delta 方向正确；
    5. L2 保护 beat 禁止 condense；
    6. 禁区显式命中禁止改动；
    7. residual_gap 必须等于方向无关公式的计算值。
    """
    violations: list[str] = []

    if not isinstance(plan, dict):
        return ["方案不是对象（应为 dict）"]

    schema = plan.get("schema")
    if "schema" not in plan:
        violations.append("缺少 schema 字段（应为 nww_balance_plan/v1）")
    elif schema != SCHEMA:
        violations.append(f"schema 不匹配：{schema!r} ≠ {SCHEMA}")

    direction = plan.get("direction")
    direction_valid = direction in ACTIONS
    if "direction" not in plan:
        violations.append("缺少 direction 字段（应 expand 或 condense）")
    elif not direction_valid:
        violations.append(f"direction 非法：{direction!r}（应为 expand 或 condense）")

    raw_ops = plan.get("operations")
    ops: list = []
    if "operations" not in plan:
        violations.append("缺少 operations 字段（应为非空列表）")
    elif not isinstance(raw_ops, list):
        violations.append("operations 类型错误（应为列表）")
    else:
        ops = raw_ops
        if not ops:
            violations.append("operations 为空：至少需要 1 个可执行 operation")

    band_ok = _band_valid(_as_int(lo), _as_int(hi))
    computed_residual = compute_residual_gap(actual, ops, lo, hi)
    if "residual_gap" not in plan:
        violations.append("缺少 residual_gap 字段（应由方向无关公式计算）")
    else:
        declared = plan.get("residual_gap")
        if not _is_int(declared):
            violations.append(
                f"residual_gap 类型错误：{declared!r}（应为整数）"
            )
        elif band_ok and declared != computed_residual:
            violations.append(
                f"residual_gap 与公式不一致：声明 {declared}，计算应为 {computed_residual}"
            )

    total_delta = 0
    deltas_complete = True
    for index, op in enumerate(ops):
        tag = f"operations[{index}]"
        if not isinstance(op, dict):
            violations.append(f"{tag} 不是对象（应为 dict）")
            deltas_complete = False
            continue

        for field in _REQUIRED_OP_FIELDS:
            if field not in op:
                violations.append(f"{tag} 缺少字段 {field}")

        beat_id = op.get("beat_id")
        if "beat_id" in op:
            if not _nonempty_str(beat_id):
                violations.append(
                    f"{tag} beat_id 类型错误：{beat_id!r}（应为非空字符串）"
                )
            elif beat_ids and beat_id not in beat_ids:
                violations.append(f"{tag} beat_id {beat_id!r} 不属于本章")

        block_id = op.get("block_id")
        if "block_id" in op:
            if not _nonempty_str(block_id):
                violations.append(
                    f"{tag} block_id 类型错误：{block_id!r}（应为非空字符串）"
                )
            elif block_ids and block_id not in block_ids:
                violations.append(f"{tag} block_id {block_id!r} 不属于本章")

        for field in ("where", "how", "constraints"):
            if field in op and not _nonempty_str(op.get(field)):
                violations.append(f"{tag} {field} 为空或非文本")

        action = op.get("action")
        action_valid = action in ACTIONS
        if "action" not in op:
            violations.append(f"{tag} 缺少字段 action")
        elif not action_valid:
            violations.append(f"{tag} action 非法：{action!r}（应为 expand 或 condense）")
        elif direction_valid and action != direction:
            violations.append(
                f"{tag} action={action!r} 与方案 direction={direction!r} 不一致"
            )

        protection_level = op.get("protection_level")
        if "protection_level" in op and protection_level not in PROTECTION_LEVELS:
            violations.append(
                f"{tag} protection_level 非法：{protection_level!r}"
                f"（应为 {'/'.join(sorted(PROTECTION_LEVELS))}）"
            )

        delta = _delta_of(op)
        if "expected_delta" not in op:
            deltas_complete = False
        elif delta is None:
            violations.append(
                f"{tag} expected_delta 类型错误：{op.get('expected_delta')!r}"
                "（应为整数）"
            )
            deltas_complete = False
        else:
            total_delta += delta
            if action == "expand" and delta <= 0:
                violations.append(
                    f"{tag} expected_delta={delta} 方向错误：expand 只增（应 > 0）"
                )
            elif action == "condense" and delta >= 0:
                violations.append(
                    f"{tag} expected_delta={delta} 方向错误：condense 只减（应 < 0）"
                )

        if action == "condense":
            if _nonempty_str(beat_id) and beat_id in l2_beat_ids:
                violations.append(f"L2 beat {beat_id} 禁止 condense")
            elif protection_level == "L2":
                violations.append(
                    f"{tag} protection_level=L2 的 operation 禁止 condense"
                )

        zone = _forbidden_zone(op)
        if zone:
            violations.append(f"{tag} 命中禁区「{zone}」：禁止改动受保护内容")

    if ops and deltas_complete and direction_valid:
        if direction == "expand":
            if total_delta < 0:
                violations.append(
                    f"Σexpected_delta 方向错误：expand 方案出现负增量（{total_delta}）"
                )
            elif total_delta == 0:
                violations.append("Σexpected_delta 方向错误：expand 方案净增为 0")
        else:  # condense
            if total_delta > 0:
                violations.append(
                    f"Σexpected_delta 方向错误：condense 方案出现正增量（+{total_delta}）"
                )
            elif total_delta == 0:
                violations.append("Σexpected_delta 方向错误：condense 方案净减为 0")

    return _dedup(violations)


# ---------------------------------------------------------------------------
# 确定性打分：不使用任何主观文笔信号
# ---------------------------------------------------------------------------


def _plan_ops(plan: dict) -> list:
    raw = plan.get("operations")
    return raw if isinstance(raw, list) else []


def _score_gap_fit(
    plan: dict, *, actual, lo, hi, reasons: list[str]
) -> int:
    current = _as_int(actual)
    low = _as_int(lo)
    high = _as_int(hi)
    if not _band_valid(low, high):
        reasons.append("基础字数区间无效 → gap_fit=0")
        return 0

    gap = compute_gap(current, low, high)
    residual = compute_residual_gap(current, _plan_ops(plan), low, high)
    if gap == 0 and residual == 0:
        reasons.append("当前已在带内且方案不制造残差 → gap_fit=100")
        return 100
    if gap == 0:
        reasons.append(f"当前已在带内但方案产生残差 {residual} → gap_fit=0")
        return 0

    fit = 100 - int(round(100.0 * residual / gap))
    fit = int(_clamp(fit, 0, 100))
    reasons.append(f"缺口 {gap}，预测残差 {residual} → gap_fit={fit}")
    return fit


def _op_complete(op: object) -> bool:
    if not isinstance(op, dict):
        return False
    for field in _REQUIRED_OP_FIELDS:
        if field not in op:
            return False
        if field in ("beat_id", "block_id", "where", "how", "constraints"):
            if not _nonempty_str(op.get(field)):
                return False
    return True


def _score_completeness(plan: dict, reasons: list[str]) -> int:
    ops = _plan_ops(plan)
    direction = plan.get("direction")

    checks = [
        plan.get("schema") == SCHEMA,
        direction in ACTIONS,
        bool(ops),
        _is_int(plan.get("residual_gap")),
        bool(ops) and all(_op_complete(op) for op in ops),
        bool(ops)
        and all(_nonempty_str(op.get("constraints")) for op in ops if isinstance(op, dict)),
        bool(ops)
        and len(ops) == len([op for op in ops if isinstance(op, dict)])
        and all(op.get("action") == direction for op in ops),
        bool(ops)
        and len(ops) == len([op for op in ops if isinstance(op, dict)])
        and all(_nonempty_str(op.get("where")) for op in ops),
    ]

    passed = sum(1 for item in checks if item)
    base = (passed / len(checks)) * 80.0
    depth = min(20.0, 10.0 * max(0, len(ops) - 1)) if ops else 0.0
    score = int(round(base + depth))
    score = int(_clamp(score, 0, 100))
    reasons.append(
        f"方案完整度 {score}/100（结构项 {passed}/{len(checks)}，操作深度加成 {int(depth)}）"
    )
    return score


def _score_risk(plan: dict, reasons: list[str]) -> int:
    ops = _plan_ops(plan)
    direction = plan.get("direction")
    score = 100

    if len(ops) > _OP_COUNT_RISK_THRESHOLD:
        score -= 10
        reasons.append(
            f"风险：operations 过多（{len(ops)} 个 > {_OP_COUNT_RISK_THRESHOLD}），"
            "改动面过大 (-10)"
        )

    for index, op in enumerate(ops):
        tag = f"operations[{index}]"
        if not isinstance(op, dict):
            score -= 20
            reasons.append(f"风险：{tag} 不是对象 (-20)")
            continue

        action = op.get("action")
        zone = _forbidden_zone(op)
        if zone:
            score -= 40
            reasons.append(f"风险：{tag} 命中禁区「{zone}」 (-40)")

        if action == "condense" and op.get("protection_level") == "L2":
            score -= 40
            reasons.append(f"风险：{tag} 对 L2 保护内容执行 condense (-40)")

        if action in ACTIONS and direction in ACTIONS and action != direction:
            score -= 15
            reasons.append(f"风险：{tag} action 与方案方向不一致 (-15)")

        delta = _delta_of(op)
        if delta is None:
            score -= 15
            reasons.append(f"风险：{tag} expected_delta 非整数 (-15)")
        elif action == "expand" and delta <= 0:
            score -= 15
            reasons.append(f"风险：{tag} expand 增量为非正数 (-15)")
        elif action == "condense" and delta >= 0:
            score -= 15
            reasons.append(f"风险：{tag} condense 增量为非负数 (-15)")

        if "constraints" in op and not _nonempty_str(op.get("constraints")):
            score -= 10
            reasons.append(f"风险：{tag} constraints 为空 (-10)")

    score = int(_clamp(score, 0, 100))
    reasons.append(f"风险项得分 {score}/100")
    return score


def _score_protection(plan: dict, reasons: list[str]) -> int:
    ops = _plan_ops(plan)
    score = 100
    for index, op in enumerate(ops):
        tag = f"operations[{index}]"
        if not isinstance(op, dict):
            score -= 20
            continue

        level = op.get("protection_level")
        if level not in PROTECTION_LEVELS:
            score -= 40
            reasons.append(f"保护级风险：{tag} protection_level 非法 (-40)")
        if op.get("action") == "condense" and level == "L2":
            score -= 50
            reasons.append(f"保护级风险：{tag} 对 L2 执行 condense (-50)")
        if "constraints" in op and not _nonempty_str(op.get("constraints")):
            score -= 20

    score = int(_clamp(score, 0, 100))
    reasons.append(f"保护级合规 {score}/100")
    return score


def score_plan(plan: dict, *, actual: int, lo: int, hi: int) -> dict:
    """按确定性信号给方案打分。

    返回 ``{"quality": 0-100, "gap_fit": 0-100, "total": float, "reasons": [...]}``。

    - ``quality`` 由缺口命中度、方案完整度、风险项、保护级合规加权得到；
    - ``gap_fit`` 单独暴露，便于审计「字数贴合度」；
    - ``total`` 与 ``quality`` 一致（保留字段以兼容规格契约）；
    - ``reasons`` 全部是可复算的事实，不评价文笔。
    """
    reasons: list[str] = []

    if not isinstance(plan, dict):
        return {
            "quality": 0,
            "gap_fit": 0,
            "total": 0.0,
            "reasons": ["方案不是对象（应为 dict）"],
        }

    gap_fit = _score_gap_fit(plan, actual=actual, lo=lo, hi=hi, reasons=reasons)
    completeness = _score_completeness(plan, reasons)
    risk = _score_risk(plan, reasons)
    protection = _score_protection(plan, reasons)

    quality = (
        _QUALITY_WEIGHTS["gap_fit"] * gap_fit
        + _QUALITY_WEIGHTS["completeness"] * completeness
        + _QUALITY_WEIGHTS["risk"] * risk
        + _QUALITY_WEIGHTS["protection"] * protection
    )
    quality = int(_clamp(int(round(quality)), 0, 100))
    reasons.append(
        f"质量分 {quality} = 0.45×gap_fit({gap_fit}) + 0.25×完整度({completeness})"
        f" + 0.15×风险({risk}) + 0.15×保护级({protection})"
    )
    return {
        "quality": quality,
        "gap_fit": gap_fit,
        "total": float(quality),
        "reasons": reasons,
    }


# ---------------------------------------------------------------------------
# R4：质量优先择优
# ---------------------------------------------------------------------------


def _plan_name(plan: dict, index: int) -> str:
    label = plan.get("label") or plan.get("strategy")
    if _nonempty_str(label):
        return str(label).strip()
    return f"方案#{index + 1}"


def _hard_filter(plan: dict, *, actual, lo, hi) -> list[str]:
    """缺少 beat/block/l2 上下文时仍可执行的硬过滤。

    归属类校验在 ``select_plan`` 层无法完成（签名未提供章节 ID），因此传空集合；
    L2 保护仍通过 operation 自带的 ``protection_level`` 命中。
    """
    return validate_plan(
        plan,
        actual=actual,
        lo=lo,
        hi=hi,
        beat_ids=set(),
        block_ids=set(),
        l2_beat_ids=set(),
    )


def select_plan(
    plans: list[dict],
    *,
    actual: int,
    lo: int,
    hi: int,
    quality_weight: float = 0.7,
    tie: float = 5.0,
) -> dict:
    """硬过滤 → 质量分降序 → 质量差 ≤ tie 时按 residual_gap 决胜。

    返回 ``{"selected": plan|None, "ranked": [plan, ...], "reason": "..."}``。

    语义要点（规格 R4）：**宁可字数差一点，也不选质量更差的方案**。因此
    ``quality_weight`` 只进入 reason 解释文本，不改变「质量优先」的排序语义。
    """
    if not isinstance(plans, list):
        return {
            "selected": None,
            "ranked": [],
            "reason": "候选方案不是列表，无法择优",
        }

    weight = _as_float(quality_weight, default=_DEFAULT_QUALITY_WEIGHT)
    assert weight is not None  # default 保证非 None
    weight = float(_clamp(weight, 0.0, 1.0))

    tie_value = _as_float(tie, default=_DEFAULT_TIE)
    assert tie_value is not None
    tie_value = max(0.0, float(tie_value))

    scored: list[dict] = []
    rejected: list[tuple[int, list[str]]] = []

    for index, plan in enumerate(plans):
        if not isinstance(plan, dict):
            rejected.append((index, ["方案不是对象（应为 dict）"]))
            continue
        violations = _hard_filter(plan, actual=actual, lo=lo, hi=hi)
        if violations:
            rejected.append((index, violations))
            continue
        result = score_plan(plan, actual=actual, lo=lo, hi=hi)
        residual = compute_residual_gap(actual, _plan_ops(plan), lo, hi)
        scored.append(
            {
                "plan": plan,
                "index": index,
                "quality": result["quality"],
                "gap_fit": result["gap_fit"],
                "residual": residual,
            }
        )

    filter_note = ""
    if rejected:
        first_index, first_violations = rejected[0]
        filter_note = (
            f"硬过滤 {len(rejected)} 套（首个为候选#{first_index + 1}："
            f"{first_violations[0]}）；"
        )

    if not scored:
        if rejected:
            reason = f"无合法候选：{filter_note.rstrip('；')}"
        else:
            reason = "无合法候选：候选方案为空"
        return {"selected": None, "ranked": [], "reason": reason}

    # 先按质量分降序建立「同档」；同档成员再按残差升序。
    scored.sort(key=lambda item: (-item["quality"], item["residual"], item["index"]))
    tiers: list[list[dict]] = []
    for item in scored:
        if tiers and (tiers[-1][0]["quality"] - item["quality"]) <= tie_value:
            tiers[-1].append(item)
        else:
            tiers.append([item])
    for tier in tiers:
        tier.sort(key=lambda item: (item["residual"], -item["quality"], item["index"]))

    ranked_entries = [item for tier in tiers for item in tier]
    chosen = ranked_entries[0]
    top = scored[0]
    quality_gap = top["quality"] - chosen["quality"]

    chosen_name = _plan_name(chosen["plan"], chosen["index"])
    if quality_gap > 0:
        top_name = _plan_name(top["plan"], top["index"])
        decision = (
            f"质量优先：选中「{chosen_name}」"
            f"(quality={chosen['quality']}, residual_gap={chosen['residual']})；"
            f"与最高分「{top_name}」(quality={top['quality']}, "
            f"residual_gap={top['residual']}) 的分差 {quality_gap} ≤ tie {tie_value:g}，"
            "按 residual_gap 决胜"
        )
    else:
        decision = (
            f"质量优先：选中「{chosen_name}」"
            f"(quality={chosen['quality']}, residual_gap={chosen['residual']})，"
            "质量分为候选最高"
        )

    reason = (
        f"{filter_note}{decision}；"
        f"quality_weight={weight:.2f}（仅解释项，不改变质量优先语义）"
    )
    return {
        "selected": chosen["plan"],
        "ranked": [item["plan"] for item in ranked_entries],
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# R6：改动范围启发式校验
# ---------------------------------------------------------------------------

_BLOCK_SPLIT_RE = re.compile(r"\n\s*\n")
_BLOCK_ID_RE = re.compile(r"(\d+)\s*$")


def _split_blocks(text: str) -> list[str]:
    """按空行切块并去掉空块；只做轻量归一（换行/首尾空白）。"""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    blocks = [block.strip() for block in _BLOCK_SPLIT_RE.split(normalized)]
    return [block for block in blocks if block]


def _addressed_blocks(ops: list, block_count: int) -> set[int]:
    """从 operation.block_id 提取序号（1 基），用于判断「未指派块」是否被删。

    例：``block_002`` → 2。无法解析的 block_id 忽略；返回空集表示无法映射，
    此时跳过「未指派块被删除」检查以免误伤（见模块文档的启发式声明）。
    """
    addressed: set[int] = set()
    for op in ops:
        if not isinstance(op, dict):
            continue
        block_id = op.get("block_id")
        if not _nonempty_str(block_id):
            continue
        match = _BLOCK_ID_RE.search(str(block_id))
        if not match:
            continue
        index = int(match.group(1))
        if 1 <= index <= block_count:
            addressed.add(index)
    return addressed


def diff_scope_ok(
    original: str,
    candidate: str,
    ops: list[dict],
    *,
    min_ratio: float = 0.3,
) -> list[str]:
    """改动范围启发式校验（不是精确 diff 语义），返回违规清单。

    启发式规则：
    1. 按空行切块，用 ``difflib.SequenceMatcher`` 统计变更块；
    2. 变更块数上限 = max(原稿块数 × min_ratio, operation 数 × 2)；
    3. 未被 operation 指派（按 block_id 数字后缀）的原稿块被删除 → 违规；
    4. 未指派任何 operation 却有差异 → 违规；
    5. expand 方案整体变短 / condense 方案整体变长 → 违规。

    ``block_id`` 与正文块的对应采用「数字后缀 = 1 基块序号」的工程约定；
    该约定不成立时应关闭此检查或调整 operation，而不是依赖本函数做精确判定。
    """
    if not isinstance(original, str) or not isinstance(candidate, str):
        return ["原文或候选稿不是文本"]

    ratio = _as_float(min_ratio, default=_DEFAULT_MIN_RATIO)
    assert ratio is not None
    ratio = float(_clamp(ratio, 0.0, 1.0))

    raw_ops = ops if isinstance(ops, list) else []
    ops_list = [op for op in raw_ops if isinstance(op, dict)]
    original_blocks = _split_blocks(original)
    candidate_blocks = _split_blocks(candidate)

    if not original_blocks:
        if candidate_blocks:
            return ["原稿为空但候选稿存在内容，无法界定改动范围"]
        return []
    if not candidate_blocks:
        return ["候选稿为空：正文块被整体删除"]
    if original == candidate:
        return []

    violations: list[str] = []
    if not ops_list:
        return ["未指派任何 operation，但候选稿与原稿存在差异"]

    actions = {op.get("action") for op in ops_list}
    if actions == {"expand"} and len(candidate) < len(original):
        violations.append(
            f"候选稿整体缩短（{len(candidate)} < {len(original)} 字符），"
            "与 expand 方案矛盾"
        )
    elif actions == {"condense"} and len(candidate) > len(original):
        violations.append(
            f"候选稿整体变长（{len(candidate)} > {len(original)} 字符），"
            "与 condense 方案矛盾"
        )

    matcher = difflib.SequenceMatcher(
        None, original_blocks, candidate_blocks, autojunk=False
    )
    changed_blocks = 0
    deleted_indices: list[int] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "delete":
            changed_blocks += i2 - i1
            deleted_indices.extend(range(i1, i2))
        elif tag == "insert":
            changed_blocks += j2 - j1
        else:  # replace：按新旧两侧较大者计变更块，保守统计
            changed_blocks += max(i2 - i1, j2 - j1)

    # 整章重写时 SequenceMatcher 可能给出 replace 而非 delete；变更块数会直接
    # 超过 allowed 上限，因此不会漏判。

    op_count = len(ops_list)
    allowed_by_ratio = max(1, math.ceil(len(original_blocks) * ratio))
    allowed_by_ops = max(1, op_count) * _CHANGE_TOLERANCE_PER_OP
    allowed = max(allowed_by_ratio, allowed_by_ops)
    if changed_blocks > allowed:
        violations.append(
            f"改动块数 {changed_blocks} 超出允许上限 {allowed}"
            f"（原稿 {len(original_blocks)} 块 / {op_count} 个 operation）"
        )

    addressed = _addressed_blocks(ops_list, len(original_blocks))
    if addressed:
        for deleted_index in deleted_indices:
            block_no = deleted_index + 1
            if block_no not in addressed:
                violations.append(
                    f"未指派的原稿块 block_{block_no:03d} 被删除"
                    f"（operation 仅覆盖 {sorted(addressed)}）"
                )

    return _dedup(violations)