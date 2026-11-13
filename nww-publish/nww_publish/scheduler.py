"""调度计算：把章节队列映射成计划发布时间，并为守护循环计算下次唤醒点。"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import List


def compute_schedule(
    count: int,
    start_at: datetime,
    interval_sec: int,
    jitter_sec: int = 0,
) -> List[datetime]:
    """为 count 个章节生成计划时间：start + i*interval + 随机抖动。"""
    out = []
    for i in range(count):
        base = start_at + timedelta(seconds=interval_sec * i)
        if jitter_sec > 0:
            base = base + timedelta(seconds=random.uniform(-jitter_sec, jitter_sec))
        out.append(base)
    return out


def next_wake(due: List[dict], now: datetime | None = None) -> datetime | None:
    """返回最近一个待发章节的计划时间；没有则 None（供 daemon sleep）。"""
    now = now or datetime.now(timezone.utc).astimezone()
    candidates = []
    for ch in due:
        try:
            candidates.append(datetime.fromisoformat(ch["scheduled_at"]))
        except Exception:
            candidates.append(now)
    if not candidates:
        return None
    return min(candidates)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def parse_start(s: str | None) -> datetime:
    """解析 --start：支持 'now'、'HH:MM'（今天该时刻）、ISO 字符串。"""
    if not s or s == "now":
        return datetime.now(timezone.utc).astimezone()
    if re_hhmm := __import__("re").match(r"^(\d{1,2}):(\d{2})$", s):
        h, m = int(re_hhmm.group(1)), int(re_hhmm.group(2))
        base = datetime.now(timezone.utc).astimezone().replace(hour=h, minute=m, second=0, microsecond=0)
        if base <= datetime.now(timezone.utc).astimezone():
            base += timedelta(days=1)
        return base
    return datetime.fromisoformat(s)
