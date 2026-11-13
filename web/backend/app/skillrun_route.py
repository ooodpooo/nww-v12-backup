"""skillrun_route.py — Web 端 skill 执行宿主端点（外脑收敛方案）。

挂载条件：NWW_SKILL_EXEC_ENABLED=1（灰度/快速回滚；默认 0 关闭，关闭时不改变旧行为）。
仅做：按契约锁步执行，不持有业务世界观。复用 skillrunner + 现有 agents_runtime。
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException

from . import skillrunner
from .auth import get_current_user
from .dataplane import projects_root
from .fs import book_root

router = APIRouter()

ENABLED = os.environ.get("NWW_SKILL_EXEC_ENABLED", "0") == "1"


@router.post("/skill/{skill_name}/execute")
def api_skill_execute(skill_name: str, payload: dict, user=Depends(get_current_user)) -> dict:
    """按契约执行一类 skill 流程。body: {mode: atomic|sequential, book: 书名, args: {...}}"""
    if not ENABLED:
        raise HTTPException(status_code=404, detail="skill exec 未启用")
    mode = str(payload.get("mode") or "sequential")
    book = str(payload.get("book") or "")
    args = payload.get("args") or {}
    ctx = dict(args)
    root = projects_root(user["user_id"])
    try:
        bk = book_root(root, book)
    except FileNotFoundError:
        bk = root / book
    ctx["project_root"] = ctx.get("project_root") or bk
    ctx["output_root"] = ctx.get("output_root") or bk
    if mode == "atomic":
        return skillrunner.run_atomic(skill_name, ctx)
    if mode == "sequential":
        return skillrunner.run_sequential(skill_name, ctx)
    raise HTTPException(status_code=400, detail=f"未知 mode: {mode}")