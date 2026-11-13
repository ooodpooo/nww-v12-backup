#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nww_policy.py — PolicyBundle / fail-closed 默认（Task 2 / C-P0-3）。

打破「缺字段静默 False」：无 policy 段时 fail_closed=True，缺闸键按 True。
存量顶层 commit_gates 仍可读；显式 override 必须带 reason + expires_at。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REQUIRED_NEW_BOOK_GATES: dict[str, bool] = {
    "consistency": True,
    "delta_audit": True,
    "meta_check": True,
    "settlement_anchor": True,
    "fact_provenance": True,  # → enabled
    "cross_chapter": True,
    "foreshadow_overdue": True,
}

_VALID_A2 = frozenset({"off", "shadow", "on"})


class PolicyError(ValueError):
    """Invalid policy / override configuration."""


@dataclass(frozen=True)
class GateOverride:
    gate: str
    reason: str
    expires_at: str  # ISO8601；过期视为无 override


@dataclass(frozen=True)
class PolicyBundle:
    fail_closed: bool
    gates: dict[str, bool]  # consistency, delta_audit, meta_check, settlement_anchor, fact_provenance
    a2_mode: str  # off|shadow|on
    overrides: list[GateOverride] = field(default_factory=list)

    def effective(self, name: str) -> bool:
        """override 未过期 → False（放行/关闸）；否则 gates[name]；fail_closed 下缺键 → True。"""
        for ov in self.overrides:
            if ov.gate == name and not _override_expired(ov.expires_at):
                return False
        if name in self.gates:
            return bool(self.gates[name])
        return True if self.fail_closed else False


def _override_expired(expires_at: str) -> bool:
    try:
        raw = expires_at.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        when = datetime.fromisoformat(raw)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when <= datetime.now(timezone.utc)
    except Exception:  # noqa: BLE001
        # 无法解析的过期时间视为已过期（不放行）
        return True


def _coerce_gate_value(name: str, raw: Any) -> bool | None:
    """返回 bool；无法识别时返回 None（交给 fail_closed 补缺）。"""
    if raw is None:
        return None
    if name == "fact_provenance":
        if isinstance(raw, dict):
            if "enabled" not in raw:
                return None
            return bool(raw.get("enabled"))
        return bool(raw)
    if isinstance(raw, dict):
        # 意外对象：不静默 True/False
        return None
    return bool(raw)


def _parse_overrides(raw: Any) -> list[GateOverride]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PolicyError("policy.overrides must be a list")
    out: list[GateOverride] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise PolicyError(f"policy.overrides[{i}] must be an object")
        gate = item.get("gate")
        if not gate or not isinstance(gate, str):
            raise PolicyError(f"policy.overrides[{i}] missing gate")
        reason = item.get("reason")
        expires = item.get("expires_at")
        missing = []
        if not (isinstance(reason, str) and reason.strip()):
            missing.append("reason")
        if not (isinstance(expires, str) and expires.strip()):
            missing.append("expires_at")
        if missing:
            raise PolicyError(
                f"policy.overrides[{i}] for gate={gate!r} requires "
                + " and ".join(missing)
            )
        out.append(GateOverride(gate=gate, reason=str(reason).strip(), expires_at=str(expires).strip()))
    return out


def _load_bible(book: Path) -> dict[str, Any]:
    path = Path(book) / "bible.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def load_policy(book: Path) -> PolicyBundle:
    """bible.policy.fail_closed 缺省 True；无 policy 段且无 overrides → fail_closed=True。

    存量迁移：若仅有旧顶层 commit_gates，仍可读旧字段；CLI scan 对缺 policy 报警。
    """
    bible = _load_bible(book)
    policy = bible.get("policy")
    has_policy = isinstance(policy, dict)
    policy_obj: dict[str, Any] = policy if has_policy else {}

    if "fail_closed" in policy_obj:
        fail_closed = bool(policy_obj.get("fail_closed"))
    else:
        # 打破静默 False：缺省 True（新书 / 无 policy 段）
        fail_closed = True

    overrides = _parse_overrides(policy_obj.get("overrides"))

    # commit_gates：优先 policy.commit_gates，否则顶层旧字段
    raw_gates = policy_obj.get("commit_gates")
    if raw_gates is None:
        raw_gates = bible.get("commit_gates")
    if not isinstance(raw_gates, dict):
        raw_gates = {}

    gates: dict[str, bool] = {}
    for name in REQUIRED_NEW_BOOK_GATES:
        coerced = _coerce_gate_value(name, raw_gates.get(name))
        if coerced is None:
            gates[name] = True if fail_closed else False
        else:
            gates[name] = coerced

    a2_raw = policy_obj.get("a2_mode")
    if isinstance(a2_raw, str) and a2_raw.strip().lower() in _VALID_A2:
        a2_mode = a2_raw.strip().lower()
    elif has_policy:
        # policy 段已落盘但未写 a2_mode → 新书 fail-closed 默认 on（对齐 setup 模板）
        a2_mode = "on"
    else:
        # 纯存量无 policy：保持历史 A-2 off（接线 ≠ 启用）；迁移后显式写 policy.a2_mode
        a2_mode = "off"

    return PolicyBundle(
        fail_closed=fail_closed,
        gates=gates,
        a2_mode=a2_mode,
        overrides=overrides,
    )
