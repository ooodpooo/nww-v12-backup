"""后台原生异步调度器：负责定时巡检、到期发书与排期入库"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
import aiosqlite

from .database import DB_PATH
from .executor import execute_publish_record
from .planner import get_group_days, seed_group_schedules

logger = logging.getLogger("publisher.scheduler")

_scheduler_task: asyncio.Task | None = None
_running: bool = False


def is_scheduler_running() -> bool:
    return _running and _scheduler_task is not None and not _scheduler_task.done()


async def _scheduler_loop():
    """常驻后台循环，周期性巡检并触发到期任务"""
    global _running
    _running = True
    logger.info("自动发布定时调度器已启动")

    while _running:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                await _tick(db)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"调度器轮询异常: {e}", exc_info=True)

        try:
            await asyncio.sleep(20)  # 每 20 秒轮询一次
        except asyncio.CancelledError:
            break

    _running = False
    logger.info("自动发布定时调度器已退出")


async def _tick(db: aiosqlite.Connection):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 1. 查询所有已到期的发布记录（status 为 pending 或 scheduled，且 scheduled_time <= now）
    cursor = await db.execute("""
        SELECT pr.id, pr.chapter_id, pr.platform, pr.scheduled_time,
               ch.book_id, ch.chapter_number
        FROM publish_records pr
        JOIN chapters ch ON ch.id = pr.chapter_id
        WHERE pr.status IN ('pending', 'scheduled')
          AND (pr.scheduled_time IS NULL OR pr.scheduled_time <= ?)
        ORDER BY pr.scheduled_time ASC
        LIMIT 5
    """, (now_str,))
    due_records = await cursor.fetchall()

    # 绑定状态缓存：(book_id, platform) -> ("unreconciled" | "ok" | "nobinding", floor)
    floor_cache: dict[tuple[int, str], tuple[str, int | None]] = {}

    async def _block(record_id: int, reason: str):
        """把记录标记为 skipped —— 不再被 _tick 选中，避免重复发布污染平台。"""
        logger.warning(f"拦截发布记录 #{record_id}：{reason}")
        await db.execute(
            "UPDATE publish_records SET status='skipped', error_message=?, "
            "updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (reason, record_id),
        )
        await db.commit()

    for row in due_records:
        record_id = row["id"]
        platform = row["platform"]
        book_id = row["book_id"]
        chapter_number = row["chapter_number"] or 0

        key = (book_id, platform)
        if key not in floor_cache:
            cur = await db.execute(
                "SELECT published_up_to, progress_checked_at FROM book_platforms "
                "WHERE book_id = ? AND platform = ?",
                key,
            )
            b = await cur.fetchone()
            if not b:
                # 没有绑定就无从对账（保留原有行为，不拦）
                floor_cache[key] = ("nobinding", None)
            elif not b["progress_checked_at"]:
                floor_cache[key] = ("unreconciled", None)
            else:
                floor_cache[key] = ("ok", b["published_up_to"] or 0)

        state, floor = floor_cache[key]

        # 护栏一：该平台尚未核对已发进度 → 不知道平台发到第几章，发出去可能就是重复
        if state == "unreconciled":
            await _block(
                record_id,
                "该平台尚未核对已发进度，为避免重复发布已阻止执行（请先在发布面板点「核对平台进度」）",
            )
            continue

        # 护栏二：章号 ≤ 平台已发章号 → 该章早已在平台上，再发一次即重复发布
        if state == "ok" and floor is not None and chapter_number and chapter_number <= floor:
            await _block(
                record_id,
                f"该章（第{chapter_number}章）不高于平台已发至第{floor}章，已阻止重复发布",
            )
            continue

        try:
            logger.info(f"触发发布记录 #{record_id}，平台: {platform}")
            await execute_publish_record(record_id, db)
        except Exception as ex:
            logger.error(f"执行发布记录 #{record_id} 发生未捕获异常: {ex}")

    # 2. 检查是否有启用的书籍平台绑定，需要自动生成下一章的排期计划
    await _auto_schedule_next_chapters(db)


async def _auto_schedule_next_chapters(db: aiosqlite.Connection):
    """按组续排：未来已排天数 < group_days 时补一整组。

    每组 group_days 天，每天累积章节到「每日更新字数门槛」（不够就在同一天多发几篇）。
    仅对**已核对平台进度**的绑定生效——否则会从第 1 章重发。
    """
    cursor = await db.execute("""
        SELECT bp.id, bp.book_id, bp.platform, bp.schedule, bp.chapters_per_run,
               bp.published_up_to, bp.progress_checked_at
        FROM book_platforms bp
        WHERE bp.enabled = 1
    """)
    bindings = await cursor.fetchall()

    group_days = await get_group_days(db)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for bind in bindings:
        book_id = bind["book_id"]
        platform = bind["platform"]
        schedule = (bind["schedule"] or "").strip()

        # 对账铁律：没有核对过平台进度就不自动续排，否则会从第 1 章重发
        if not bind["progress_checked_at"]:
            logger.warning(
                f"书 {book_id} 平台 {platform} 未核对平台进度，已跳过自动续排（请先在面板点「核对平台进度」）"
            )
            continue
        floor = bind["published_up_to"] or 0

        # 未来还有几天排期？够一组就不动
        cur = await db.execute("""
            SELECT COUNT(DISTINCT date(pr.scheduled_time)) AS n
            FROM publish_records pr
            JOIN chapters ch ON pr.chapter_id = ch.id
            WHERE ch.book_id = ? AND pr.platform = ?
              AND pr.status IN ('scheduled', 'pending', 'publishing')
              AND pr.scheduled_time IS NOT NULL AND pr.scheduled_time >= ?
        """, (book_id, platform, now_str))
        row = await cur.fetchone()
        future_days = (row["n"] if hasattr(row, "keys") else row[0]) or 0
        if future_days >= group_days:
            continue

        plan = await seed_group_schedules(book_id, platform, schedule, floor, db)
        if plan["scheduled"]:
            logger.info(
                f"书 {book_id} 平台 {platform} 续排一组：新增 {plan['scheduled']} 条"
                f"（补前未来已排 {future_days}/{group_days} 天，门槛 {plan['quota']} 字/天）"
            )
        elif plan["chapters_to_write"]:
            # 缺口只做提示，绝不自动凑字数
            logger.info(
                f"书 {book_id} 平台 {platform} 字数不足门槛：还差 {plan['next_group_shortfall_words']} 字，"
                f"按本书均字 {plan['avg_chapter_words']} 估算需再写约 {plan['chapters_to_write']} 篇"
            )


def start_scheduler():
    global _scheduler_task, _running
    if _scheduler_task is None or _scheduler_task.done():
        _running = True
        _scheduler_task = asyncio.create_task(_scheduler_loop())


async def stop_scheduler():
    global _scheduler_task, _running
    _running = False
    if _scheduler_task:
        _scheduler_task.cancel()
        try:
            await _scheduler_task
        except asyncio.CancelledError:
            pass
        _scheduler_task = None
