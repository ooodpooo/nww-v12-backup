"""live_routes.py — AI 实况查询面（只读）。

GET /api/live/feed?since=<seq>
  - since<=0：返回最近 run 的物化快照（首屏）
  - since>0 ：返回该 seq 之后的增量事件（前端轮询，按 seq 去重/合并）

不写任何业务状态；只读内存中的观测数据。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from . import live_feed
from .auth import get_current_user

router = APIRouter()


@router.get("/live/feed")
def api_live_feed(
    since: int = Query(0, ge=0),
    user=Depends(get_current_user),
) -> dict:
    payload = live_feed.snapshot(since, user_id=str(user.get("user_id") or "") or None)
    payload["running"] = live_feed.running_runs()
    return payload
