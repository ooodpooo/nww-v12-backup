"""skillrunner.py — 通用薄 Runner（外脑收敛方案落地）。

职责（Web 只是 nww-novel-skill 的确定性执行宿主，不持有任何业务/方法论）：
  - 读取 nww-novel-skill/execution-contract/*.json（机器执行契约，仅声明 阶段顺序/依赖/执行器/产物）
  - 按契约 锁步执行 并 机器验证产物，阻止"宿主 LLM 跳步骤"
  - 只支持两种模式：atomic（原子一步）/ sequential（顺序多阶段）

约束：
  - 不含 `if workflow=="analyze": ...` 之类业务分支；执行内容完全由契约字段驱动。
  - 执行靠复用现有能力：runtime.run_agent / toolbox.run_script / cloud_ops。
  - 不回写、不覆盖现有写章主链与任何既定脚本/方法论。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from .agents_runtime import registry, runtime
from .agents_runtime import toolbox

_SKILL_ROOT = Path(os.environ.get(
    "NWW_SKILL_ROOT", Path(__file__).resolve().parents[3] / "skills" / "nww-novel-skill"
))
_CONTRACT_DIR = _SKILL_ROOT / "execution-contract"


class ContractError(Exception):
    pass


class StepValidationError(Exception):
    """某阶段产物校验失败（fail-closed，不自动进入下一步）。"""


def load_contract(name: str) -> dict:
    """读取契约；不存在/非法 JSON → ContractError。"""
    path = _CONTRACT_DIR / f"{name}.json"
    if not path.is_file():
        raise ContractError(f"契约不存在: {name}.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ContractError(f"契约非法: {name}.json: {exc}") from exc
    if not isinstance(data, dict) or "steps" not in data:
        raise ContractError(f"契约缺少 steps: {name}.json")
    return data


def _execute_step(step: dict, ctx: dict) -> str:
    """执行单个阶段。executor 由契约声明，Runner 不解释业务。"""
    ex: dict = step.get("executor") or {}
    etype = ex.get("type")
    if etype == "agent":
        agent = registry.get_agent(ex.get("agent") or "")
        return runtime.run_agent(
            agent,
            ex.get("session", "阶段执行"),
            ctx.get("context") or ctx.get("instructions") or "",
            temperature=ctx.get("temperature", 0.5),
        )
    if etype == "script":
        script = ex.get("script")
        base = ctx.get("project_root")
        res = toolbox.run_script(script, [str(base)] if base else [], base or Path.cwd(), timeout=ctx.get("timeout", 300))
        if not res["ok"]:
            raise StepValidationError(res["stderr"] or res["stdout"])
        return res["stdout"]
    if etype == "cloud":
        from . import cloud_ops
        action = ex.get("action")
        fn = getattr(cloud_ops, action)
        return json.dumps(fn(*[ctx.get("args") or []]), ensure_ascii=False)
    raise ContractError(f"未知 executor 类型: {etype}")


def _validate(step: dict, ctx: dict) -> Optional[str]:
    """机器验证产物（fail-closed）：契约声明的产出路径存在即可视为该阶段完成。"""
    if step.get("validate", "output_exists") != "output_exists":
        return None
    base = ctx.get("output_root")
    missing = []
    for rel in step.get("outputs") or []:
        p = (base / rel) if base else Path(rel)
        if not p.exists():
            missing.append(rel)
    return "缺失产物: " + ", ".join(missing) if missing else None


def run_atomic(name: str, ctx: dict) -> dict:
    """Mode A：单步原子执行（单脚本 / 云端 action / 写作主链单入口）。"""
    contract = load_contract(name)
    steps = contract["steps"]
    step = steps[0] if isinstance(steps, list) and steps else None
    if step is None:
        raise ContractError(f"原子契约缺少唯一 step: {name}")
    result = _execute_step(step, ctx or {})
    err = _validate(step, ctx or {})
    return {"mode": "atomic", "workflow": name, "step": step["id"], "ok": err is None, "error": err, "output": result}


def run_sequential(name: str, ctx: dict) -> dict:
    """Mode B：顺序锁步。stage N 未完成/未验证通过 → 不进入 stage N+1。"""
    contract = load_contract(name)
    done: list[str] = []
    for step in contract["steps"]:
        sid = step["id"]
        for dep in step.get("requires") or []:
            if dep not in done:
                return {"mode": "sequential", "workflow": name, "ok": False, "error": f"跳步被阻止: {sid} 依赖未完成的 {dep}", "step": sid, "done": done}
        try:
            _execute_step(step, ctx or {})
        except Exception as exc:  # noqa: BLE001 — fail-closed
            return {"mode": "sequential", "workflow": name, "ok": False, "error": f"{sid} 执行失败: {exc}", "step": sid, "done": done}
        verr = _validate(step, ctx or {})
        if verr:
            return {"mode": "sequential", "workflow": name, "ok": False, "error": f"{sid} 验证失败: {verr}", "step": sid, "done": done}
        done.append(sid)
    return {"mode": "sequential", "workflow": name, "ok": True, "done": done}