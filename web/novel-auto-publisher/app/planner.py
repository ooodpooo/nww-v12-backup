"""排期规划：字数门槛、按组累积排期、缺口计算。

独立于 main.py 与 scheduler.py，供两者共用（main 导入 scheduler，
若把规划逻辑留在 main 会形成循环导入）。

核心约束（2026-09-12 用户拍板）：
  · 每个自然日的更新字数**只能超门槛，不可少**；
  · 单章字数不够就当天多发几篇（依次发到平台），由真实字数决定发 2 篇还是 3 篇；
  · **绝不凑数字**：字数一律取 chapters.word_count 真实值，缺口只做「需补写 N 篇」提示，
    不生成填充文本、不篡改字数、不拆分或合并章节正文。
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Tuple

import aiosqlite

# 门槛口径说明（2026-09-12 核实平台官方规则 + 用户拍板）：
#   起点中文网：全勤/扶持要求「当日 VIP 有效更新字数 ≥ 4000 字」；
#               且「单章 1000–1999 字按 1000 字计入」——短章会被打折，别指望凑。
#   番茄小说：官方帮助中心「章节字数至少 1000 字，至多 50000 字」；
#               平台日更门槛按用户目标取 6000 字。
#
# 两层口径必须分开看（用户 2026-09-12 明确）：
#   · **单章**：≥ chapter_min_words（默认 2000）就算达标——章本身没有「不达标」一说；
#   · **平台发布字数**：单章往往够不上平台日更门槛，所以需要**多篇来凑**，
#     逐日累积章节直到 Σ word_count ≥ min_words_*（只能超，不可少）。
DEFAULT_SETTINGS: Dict[str, str] = {
    "min_words_qidian": "4000",
    "min_words_fanqie": "6000",
    "chapter_min_words": "2000",   # 单章合格线：够这条就算这一章达标
    "schedule_group_days": "4",    # 每 4 天为一个排期组
}


async def get_setting(db: aiosqlite.Connection, key: str) -> str:
    cur = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = await cur.fetchone()
    if row is not None:
        val = row["value"] if hasattr(row, "keys") else row[0]
        if val not in (None, ""):
            return str(val)
    return DEFAULT_SETTINGS.get(key, "")


async def get_min_words(db: aiosqlite.Connection, platform: str) -> int:
    """每个自然日要凑够的更新字数（只可超、不可少）。"""
    raw = await get_setting(db, f"min_words_{platform}")
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return int(DEFAULT_SETTINGS.get(f"min_words_{platform}", "4000"))


async def get_group_days(db: aiosqlite.Connection) -> int:
    raw = await get_setting(db, "schedule_group_days")
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return int(DEFAULT_SETTINGS["schedule_group_days"])


async def get_chapter_min_words(db: aiosqlite.Connection) -> int:
    """单章合格线（默认 2000）。够这条就算这一章达标——章层面不存在「不达标」。"""
    raw = await get_setting(db, "chapter_min_words")
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return int(DEFAULT_SETTINGS["chapter_min_words"])


def parse_schedule_hm(schedule: str) -> Tuple[int, int]:
    m = re.search(r"(\d{1,2}):(\d{2})", schedule or "")
    return (int(m.group(1)), int(m.group(2))) if m else (20, 0)


async def plan_day_buckets(
    book_id: int,
    platform: str,
    floor: int,
    quota: int,
    group_days: int,
    db: aiosqlite.Connection,
) -> Tuple[List[dict], List[dict]]:
    """把「章号 > floor 且未排期/未发布」的章节，按章号顺序累积成「每天一桶」。

    每桶累计字数必须 ≥ quota（**只能超，不可少**）。
    收尾凑不满门槛的那一天整天不排，章节原样退回待写池（不凑数字、不拆章节）。
    返回 (buckets, tail)。
    """
    cur = await db.execute("""
        SELECT id, title, chapter_number, COALESCE(word_count, 0) AS wc
        FROM chapters
        WHERE book_id = ? AND status = 'pending'
          AND COALESCE(chapter_number, 0) > ?
          AND id NOT IN (
              SELECT chapter_id FROM publish_records
              WHERE platform = ? AND status IN ('scheduled', 'pending', 'publishing', 'published')
          )
        ORDER BY chapter_number ASC, id ASC
    """, (book_id, floor, platform))
    pool = [dict(r) for r in await cur.fetchall()]

    buckets: List[dict] = []
    idx = 0
    for _ in range(group_days):
        if idx >= len(pool):
            break
        picked: List[dict] = []
        total = 0
        # 累积到达标为止；quota<=0 时退化为「一章一条」
        while idx < len(pool) and (not picked or total < quota):
            ch = pool[idx]
            picked.append(ch)
            total += ch["wc"]
            idx += 1
        if quota > 0 and total < quota:
            idx -= len(picked)  # 凑不满 → 整天不排，章节退回
            break
        buckets.append({"chapters": picked, "words": total})

    return buckets, pool[idx:]


async def build_plan(
    book_id: int, platform: str, schedule: str, floor: int, db: aiosqlite.Connection
) -> Dict[str, Any]:
    """生成排期方案（不落库）：每天的分组、未分配的余量、下一组还差多少字/几篇。"""
    quota = await get_min_words(db, platform)
    group_days = await get_group_days(db)
    chapter_min = await get_chapter_min_words(db)
    buckets, tail = await plan_day_buckets(book_id, platform, floor, quota, group_days, db)

    hour, minute = parse_schedule_hm(schedule)
    start_day = date.today()
    # 今天的定点时刻若已过，顺延到明天——否则刚建的任务会被立刻判为到期并马上执行
    if datetime(start_day.year, start_day.month, start_day.day, hour, minute, 0) <= datetime.now():
        start_day = start_day + timedelta(days=1)

    days = []
    for i, b in enumerate(buckets):
        target_day = start_day + timedelta(days=i)
        days.append({
            "date": target_day.isoformat(),
            "time": datetime(
                target_day.year, target_day.month, target_day.day, hour, minute, 0
            ).strftime("%Y-%m-%d %H:%M:%S"),
            "start_day_offset": i,
            "chapter_ids": [c["id"] for c in b["chapters"]],
            "chapter_numbers": [c["chapter_number"] for c in b["chapters"]],
            "chapter_titles": [c["title"] for c in b["chapters"]],
            "chapter_words": [c["wc"] for c in b["chapters"]],
            "words": b["words"],
            "quota": quota,
            "met": quota <= 0 or b["words"] >= quota,
        })

    tail_words = sum(c["wc"] for c in tail)
    shortfall = max(0, quota - tail_words) if quota > 0 else 0

    # 平均单章字数一律取真实 word_count，绝不假设、更不凑
    cur = await db.execute(
        "SELECT AVG(COALESCE(word_count, 0)) AS a FROM chapters WHERE book_id = ?", (book_id,)
    )
    row = await cur.fetchone()
    avg_raw = (row["a"] if hasattr(row, "keys") else row[0]) or 0
    avg_i = int(avg_raw)
    chapters_to_write = (shortfall + avg_i - 1) // avg_i if (shortfall > 0 and avg_i > 0) else 0

    return {
        "book_id": book_id,
        "platform": platform,
        "quota": quota,
        "chapter_min_words": chapter_min,
        "group_days": group_days,
        "avg_chapter_words": avg_i,
        "days": days,
        "unassigned": [
            {"chapter_number": c["chapter_number"], "title": c["title"], "words": c["wc"]}
            for c in tail
        ],
        "unassigned_count": len(tail),
        "unassigned_words": tail_words,
        "next_group_shortfall_words": shortfall,
        "chapters_to_write": chapters_to_write,
    }


async def seed_group_schedules(
    book_id: int, platform: str, schedule: str, floor: int, db: aiosqlite.Connection
) -> Dict[str, Any]:
    """落库排期：按「每 group_days 天一组、每天累积到达字数门槛」写入 publish_records。

    每个自然日可能排入 1–3 章（字数不够就多发几篇），这些章会依次发到平台。
    """
    plan = await build_plan(book_id, platform, schedule, floor, db)

    scheduled = 0
    for day in plan["days"]:
        for ch_id in day["chapter_ids"]:
            await db.execute(
                "INSERT INTO publish_records (chapter_id, platform, status, scheduled_time) "
                "VALUES (?, ?, 'scheduled', ?)",
                (ch_id, platform, day["time"]),
            )
            scheduled += 1
    await db.commit()

    plan["scheduled"] = scheduled
    return plan
