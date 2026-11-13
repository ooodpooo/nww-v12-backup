# -*- coding: utf-8 -*-
"""style_profile.validate — Profile 入库校验（决策 D2 + D3 硬卡口）。

D3 硬卡口：negativeSamples（不可模仿标注）必填且非空——缺则拒绝入库，
防止 AI 模仿时过拟合抄袭原著桥段 / 专名，去 AI 味失效且涉版权风险。
读取失败 / 缺失 → 返回显式 status，绝不静默返回 {}。
"""
from __future__ import annotations

from typing import Any

from .schema import NARRATIVE_LOGIC_KEYS, REQUIRED_FIELDS, VALID_SCOPES


def validate_profile(profile: Any) -> tuple[bool, list[str]]:
    """返回 (ok, errors)。negativeSamples 必填且非空 = 硬卡口（D3）。"""
    errors: list[str] = []
    if not isinstance(profile, dict):
        return False, ["profile 必须是 dict（顶层为 JSON Object）"]
    # 必含字段
    for f in REQUIRED_FIELDS:
        if f not in profile:
            errors.append(f"缺必填字段: {f}")
    # scope 合法性
    scope = profile.get("scope")
    if scope is not None and scope not in VALID_SCOPES:
        errors.append(f"非法 scope: {scope!r}（须为 {VALID_SCOPES}）")
    # D3 硬卡口：negativeSamples 必填且非空
    ns = profile.get("negativeSamples")
    if ns is None:
        errors.append("negativeSamples 必填（不可模仿标注为入库硬卡口）")
    elif not isinstance(ns, list) or len(ns) == 0:
        errors.append("negativeSamples 不能为空（至少一条不可模仿标注）")
    else:
        for i, item in enumerate(ns):
            if not isinstance(item, dict) or not str(item.get("expression") or "").strip():
                errors.append(f"negativeSamples[{i}] 须含 expression（不可模仿的表达/专名/桥段）")
    # narrativeLogic 结构完整性
    nl = profile.get("narrativeLogic")
    if nl is not None and isinstance(nl, dict):
        for k in NARRATIVE_LOGIC_KEYS:
            if k not in nl:
                errors.append(f"narrativeLogic 缺子键: {k}")
    return (len(errors) == 0, errors)


if __name__ == "__main__":
    # 自测：空骨架应失败（negativeSamples 空），补 D3 后应通过
    from .schema import empty_profile
    bad = empty_profile("genre", "test")
    ok1, e1 = validate_profile(bad)
    assert not ok1 and any("negativeSamples" in e for e in e1), "空骨架未拦截 negativeSamples"
    bad["negativeSamples"] = [{"expression": "禁用专名X", "reason": "版权"}]
    ok2, e2 = validate_profile(bad)
    assert ok2, f"补 negativeSamples 后仍失败: {e2}"
    print("validate self-test passed.")
