"""网文自动发布系统 - FastAPI 后端"""
from __future__ import annotations

# ---- Starlette >= 0.45 与 FastAPI 版本兼容补丁 ----
import starlette.routing
_orig_router_init = starlette.routing.Router.__init__
def _patched_router_init(self, *args, **kwargs):
    kwargs.pop("on_startup", None)
    kwargs.pop("on_shutdown", None)
    return _orig_router_init(self, *args, **kwargs)
starlette.routing.Router.__init__ = _patched_router_init
# -------------------------------------------------

from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime, date, timedelta
import calendar as cal_module
import aiosqlite
import logging
import os
import re
from pathlib import Path

# 全局日志配置：没有这行，logging.getLogger(...).info() 没有 handler，
# 只有 WARNING 及以上才会由 lastResort 落到 stderr —— 表现为
# 「续排一组」「字数不足门槛」「登录过程」等关键 info 日志全部静默消失。
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

from .database import (
    init_db,
    get_db,
    scan_drafts,
    sync_all_projects,
    BASE_DIR,
    REPO_ROOT,
    PROJECTS_DIR,
)
from .executor import (
    trigger_platform_login,
    check_platform_login_profile,
    is_browser_busy,
    execute_publish_record,
    fetch_platform_progress,
)
from .scheduler import start_scheduler, stop_scheduler, is_scheduler_running
from .planner import (
    DEFAULT_SETTINGS,
    get_group_days,
    get_chapter_min_words,
    build_plan,
    seed_group_schedules,
)

app = FastAPI(title="网文自动发布系统", version="0.3.2")

@app.on_event("startup")
async def startup_event():
    await init_db()
    start_scheduler()

@app.on_event("shutdown")
async def shutdown_event():
    await stop_scheduler()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────  请求模型  ─────────────────────

class BookCreate(BaseModel):
    name: str
    draft_dir: str = ""


class PlatformBindingUpdate(BaseModel):
    platform: str
    platform_book_id: str = ""
    platform_book_name: str = ""
    schedule: str = ""
    chapters_per_run: int = 1
    ai_content: bool = False
    enabled: bool = True


class SettingUpdate(BaseModel):
    key: str
    value: str


class ProgressCheckRequest(BaseModel):
    """核对平台已发进度：platform_book_id 可选，留空则按书名在作品管理页定位"""
    platform_book_id: str = ""


class ProgressUpdate(BaseModel):
    """确认/录入对账基准"""
    published_up_to: int
    source: str = "auto"  # auto | manual
    platform_book_id: str = ""


class SettingsBulkUpdate(BaseModel):
    settings: Dict[str, str]


# ─────────────────────  发布门槛与分组配置  ─────────────────────
#
# 门槛口径、按组累积排期与缺口计算都在 app/planner.py
# （scheduler.py 也要用这套逻辑，留在 main 会形成循环导入）。

@app.get("/api/settings")
async def list_settings(db: aiosqlite.Connection = Depends(get_db)):
    cur = await db.execute("SELECT key, value FROM settings")
    stored = {r["key"]: r["value"] for r in await cur.fetchall()}
    return {"settings": {**DEFAULT_SETTINGS, **stored}, "defaults": DEFAULT_SETTINGS}


@app.post("/api/settings")
async def update_settings(payload: SettingsBulkUpdate, db: aiosqlite.Connection = Depends(get_db)):
    for k, v in payload.settings.items():
        await db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (k, str(v)),
        )
    await db.commit()
    return {"success": True}


# ─────────────────────  仪表盘与卡片 API  ─────────────────────

@app.get("/api/stats")
async def get_stats(db: aiosqlite.Connection = Depends(get_db)):
    cursor = await db.execute("SELECT COUNT(*) FROM chapters WHERE status='pending'")
    pending_chapters = (await cursor.fetchone())[0]

    cursor = await db.execute("SELECT COUNT(*) FROM publish_records WHERE status='scheduled'")
    scheduled_count = (await cursor.fetchone())[0]

    cursor = await db.execute("SELECT COUNT(*) FROM publish_records WHERE status='published'")
    published_count = (await cursor.fetchone())[0]

    cursor = await db.execute("SELECT COUNT(*) FROM publish_records WHERE status='failed'")
    failed_count = (await cursor.fetchone())[0]

    cursor = await db.execute("SELECT COALESCE(SUM(word_count), 0) FROM chapters")
    total_words = (await cursor.fetchone())[0]

    cursor = await db.execute("SELECT COUNT(*) FROM books")
    book_count = (await cursor.fetchone())[0]

    cursor = await db.execute("""
        SELECT DISTINCT date(actual_time) as pub_date
        FROM publish_records
        WHERE status='published' AND actual_time IS NOT NULL
        ORDER BY pub_date DESC
    """)
    pub_dates = [row["pub_date"] for row in await cursor.fetchall()]
    streak_days = 0
    if pub_dates:
        today_str = date.today().isoformat()
        yesterday_str = (date.today() - timedelta(days=1)).isoformat()
        current_check = date.today() if pub_dates[0] == today_str else (date.today() - timedelta(days=1))
        if pub_dates[0] in (today_str, yesterday_str):
            date_set = set(pub_dates)
            d = current_check
            while d.isoformat() in date_set:
                streak_days += 1
                d -= timedelta(days=1)

    return {
        "book_count": book_count,
        "pending_chapters": pending_chapters,
        "scheduled_count": scheduled_count,
        "published_count": published_count,
        "failed_count": failed_count,
        "total_words": total_words,
        "streak_days": streak_days,
    }


@app.get("/api/task-cards")
async def get_task_cards(db: aiosqlite.Connection = Depends(get_db)):
    """获取顶部的写/发任务状态卡片"""
    cursor = await db.execute("""
        SELECT bp.id, bp.book_id, bp.platform, bp.schedule, b.name as book_name
        FROM book_platforms bp
        JOIN books b ON bp.book_id = b.id
        WHERE bp.enabled = 1
        ORDER BY bp.id ASC
    """)
    bindings = [dict(r) for r in await cursor.fetchall()]

    if not bindings:
        cur = await db.execute("SELECT id, name FROM books ORDER BY id ASC LIMIT 2")
        books = await cur.fetchall()
        for i, b in enumerate(books):
            p = "qidian" if i == 0 else "fanqie"
            bindings.append({"book_id": b["id"], "book_name": b["name"], "platform": p})

    cards = []
    for bind in bindings:
        book_id = bind["book_id"]
        platform = bind["platform"]
        book_name = bind["book_name"]
        p_name = "起点中文网" if platform == "qidian" else "番茄小说"

        cur = await db.execute("SELECT COUNT(*) FROM chapters WHERE book_id = ?", (book_id,))
        total_chapters = (await cur.fetchone())[0]

        cur = await db.execute("""
            SELECT COALESCE(MAX(ch.chapter_number), 0)
            FROM publish_records pr
            JOIN chapters ch ON pr.chapter_id = ch.id
            WHERE ch.book_id = ? AND pr.platform = ? AND pr.status = 'published'
        """, (book_id, platform))
        last_published = (await cur.fetchone())[0]

        cur = await db.execute("""
            SELECT chapter_number FROM chapters
            WHERE book_id = ? AND date(file_mtime) = date('now', 'localtime')
            ORDER BY chapter_number ASC
        """, (book_id,))
        today_write = [r[0] for r in await cur.fetchall() if r[0] is not None]

        cur = await db.execute("""
            SELECT ch.chapter_number FROM publish_records pr
            JOIN chapters ch ON pr.chapter_id = ch.id
            WHERE ch.book_id = ? AND pr.platform = ? AND pr.status = 'published'
              AND date(pr.actual_time) = date('now', 'localtime')
            ORDER BY ch.chapter_number ASC
        """, (book_id, platform))
        today_published = [r[0] for r in await cur.fetchall() if r[0] is not None]

        cur = await db.execute("""
            SELECT DISTINCT date(actual_time) as d
            FROM publish_records
            WHERE platform = ? AND status = 'published'
            ORDER BY d DESC
        """, (platform,))
        p_dates = set(r[0] for r in await cur.fetchall())
        streak = 0
        check_d = date.today()
        while check_d.isoformat() in p_dates:
            streak += 1
            check_d -= timedelta(days=1)

        cards.append({
            "platform": platform,
            "platform_name": p_name,
            "book_id": book_id,
            "book_name": book_name,
            "total_chapters": total_chapters,
            "last_published_chapter": last_published,
            "streak_days": streak,
            "today_write_chapters": today_write,
            "today_published_chapters": today_published,
        })

    return {"cards": cards}


# ─────────────────────  日历详情 API  ─────────────────────

@app.get("/api/calendar")
async def get_calendar(
    year: Optional[int] = None,
    month: Optional[int] = None,
    db: aiosqlite.Connection = Depends(get_db)
):
    today = date.today()
    y = year or today.year
    m = month or today.month

    month_str = f"{y:04d}-{m:02d}"
    cur = await db.execute("""
        SELECT ch.chapter_number, ch.title, date(ch.file_mtime) as d, b.id as book_id, b.name as book_name
        FROM chapters ch
        JOIN books b ON ch.book_id = b.id
        WHERE strftime('%Y-%m', ch.file_mtime) = ?
        ORDER BY ch.chapter_number ASC
    """, (month_str,))
    writes_raw = await cur.fetchall()

    writes_by_day: Dict[int, Dict[str, Any]] = {}
    for r in writes_raw:
        if not r["d"]:
            continue
        day_num = int(r["d"].split("-")[2])
        if day_num not in writes_by_day:
            writes_by_day[day_num] = {
                "book_id": r["book_id"],
                "book_name": r["book_name"],
                "chapters": [],
            }
        if r["chapter_number"] and r["chapter_number"] not in writes_by_day[day_num]["chapters"]:
            writes_by_day[day_num]["chapters"].append(r["chapter_number"])

    cur = await db.execute("""
        SELECT pr.id, pr.platform, pr.status, pr.scheduled_time, pr.actual_time,
               ch.chapter_number, ch.title as chapter_title, b.name as book_name
        FROM publish_records pr
        JOIN chapters ch ON pr.chapter_id = ch.id
        JOIN books b ON ch.book_id = b.id
        WHERE strftime('%Y-%m', COALESCE(pr.scheduled_time, pr.actual_time, pr.created_at)) = ?
        ORDER BY pr.scheduled_time ASC
    """, (month_str,))
    pubs_raw = await cur.fetchall()

    pubs_by_day: Dict[int, List[Dict[str, Any]]] = {}
    for r in pubs_raw:
        target_time = r["scheduled_time"] or r["actual_time"]
        if not target_time:
            continue
        day_str = target_time.split(" ")[0]
        day_num = int(day_str.split("-")[2])
        if day_num not in pubs_by_day:
            pubs_by_day[day_num] = []

        p_label = "起点" if r["platform"] == "qidian" else "番茄"
        ch_num = r["chapter_number"] or 0
        pubs_by_day[day_num].append({
            "record_id": r["id"],
            "platform": r["platform"],
            "platform_name": p_label,
            "chapters": [ch_num],
            "chapter_title": r["chapter_title"],
            "book_name": r["book_name"],
            "status": r["status"],
            "time": target_time.split(" ")[1][:5] if " " in target_time else "",
        })

    days_data = {}
    _, last_day = cal_module.monthrange(y, m)
    for d in range(1, last_day + 1):
        day_dict = {}
        if d in writes_by_day:
            day_dict["write"] = writes_by_day[d]
        if d in pubs_by_day:
            day_dict["publish"] = pubs_by_day[d]
        if day_dict:
            days_data[str(d)] = day_dict

    return {
        "year": y,
        "month": m,
        "days": days_data,
    }


# ─────────────────────  书籍管理 API  ─────────────────────

@app.get("/api/books")
async def list_books(db: aiosqlite.Connection = Depends(get_db)):
    """获取所有书籍，自动保持与本地 projects/admin 同步"""
    await sync_all_projects(db)

    cursor = await db.execute("""
        SELECT b.id, b.name, b.draft_dir, b.created_at,
               (SELECT COUNT(*) FROM chapters ch WHERE ch.book_id = b.id AND ch.status = 'pending') as pending_count,
               (SELECT COALESCE(SUM(ch.word_count), 0) FROM chapters ch WHERE ch.book_id = b.id) as total_words,
               (SELECT COUNT(*) FROM chapters ch WHERE ch.book_id = b.id) as total_chapters
        FROM books b
        ORDER BY b.id ASC
    """)
    books = [dict(row) for row in await cursor.fetchall()]

    for book in books:
        cursor = await db.execute("""
            SELECT id, platform, platform_book_id, platform_book_name, schedule,
                   chapters_per_run, ai_content, enabled, last_published_at,
                   published_up_to, progress_checked_at, progress_source
            FROM book_platforms WHERE book_id = ?
        """, (book["id"],))
        book["platforms"] = [dict(row) for row in await cursor.fetchall()]

    return {"books": books}


@app.post("/api/books")
async def create_book(book: BookCreate, db: aiosqlite.Connection = Depends(get_db)):
    if not book.name.strip():
        raise HTTPException(status_code=400, detail="书名不能为空")

    draft_dir = book.draft_dir.strip() or f"projects/admin/{book.name.strip()}"
    cursor = await db.execute(
        "INSERT OR IGNORE INTO books (name, draft_dir) VALUES (?, ?)",
        (book.name.strip(), draft_dir)
    )
    await db.commit()
    await scan_drafts(cursor.lastrowid, draft_dir, db)
    return {"id": cursor.lastrowid, "name": book.name.strip(), "draft_dir": draft_dir}


# ─────────────────────  平台绑定与排期制定  ─────────────────────

@app.post("/api/books/{book_id}/platforms")
async def bind_platform(book_id: int, binding: PlatformBindingUpdate, db: aiosqlite.Connection = Depends(get_db)):
    cursor = await db.execute(
        "SELECT id FROM book_platforms WHERE book_id = ? AND platform = ?",
        (book_id, binding.platform)
    )
    existing = await cursor.fetchone()

    if existing:
        await db.execute("""
            UPDATE book_platforms
            SET platform_book_id=?, platform_book_name=?, schedule=?, 
                chapters_per_run=?, ai_content=?, enabled=?
            WHERE id=?
        """, (
            binding.platform_book_id, binding.platform_book_name,
            binding.schedule, binding.chapters_per_run,
            int(binding.ai_content), int(binding.enabled), existing["id"]
        ))
    else:
        await db.execute("""
            INSERT INTO book_platforms
            (book_id, platform, platform_book_id, platform_book_name, schedule, chapters_per_run, ai_content, enabled)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            book_id, binding.platform, binding.platform_book_id,
            binding.platform_book_name, binding.schedule,
            binding.chapters_per_run, int(binding.ai_content), int(binding.enabled)
        ))

    await db.commit()

    if binding.enabled and binding.schedule:
        floor = await _progress_floor(book_id, binding.platform, db)
        if floor is None:
            # 硬拦截：没有「平台已发到第几章」这个基准就排期，必然从第 1 章重发
            raise HTTPException(
                status_code=409,
                detail="尚未核对平台进度，已拒绝生成排期。请先点「核对平台进度」，确认该书已发到第几章后再排期（避免重复发布）。",
            )
        plan = await seed_group_schedules(book_id, binding.platform, binding.schedule, floor, db)
        return {
            "success": True,
            "scheduled": plan["scheduled"],
            "floor": floor,
            "quota": plan["quota"],
            "group_days": plan["group_days"],
            "days": plan["days"],
            "chapters_to_write": plan["chapters_to_write"],
            "next_group_shortfall_words": plan["next_group_shortfall_words"],
        }

    return {"success": True, "scheduled": 0}


@app.delete("/api/books/{book_id}/platforms/{platform}")
async def unbind_platform(
    book_id: int,
    platform: str,
    purge_records: bool = True,
    db: aiosqlite.Connection = Depends(get_db),
):
    """软删除「某书 × 某平台」的定时发布任务（解绑）。

    软删除语义：保留 book_platforms 行（含对账基准 published_up_to / progress_checked_at），
    仅把 enabled 置 0 —— 重新绑定时不会因为丢了对账基准被 409 拦截。

    purge_records（默认 True）会一并清掉该绑定的未发布排期：
    `_tick` 执行到期记录时并不依赖绑定，若不清理，残留排期仍会被发出去。
    已发布记录（published）一律保留留痕。
    """
    if platform not in ("qidian", "fanqie"):
        raise HTTPException(status_code=400, detail="不支持的发布平台")

    cur = await db.execute(
        "UPDATE book_platforms SET enabled = 0 WHERE book_id = ? AND platform = ?",
        (book_id, platform)
    )
    removed_binding = cur.rowcount > 0

    purged = 0
    if purge_records:
        cur = await db.execute("""
            DELETE FROM publish_records
            WHERE platform = ?
              AND status IN ('scheduled', 'pending', 'publishing')
              AND chapter_id IN (SELECT id FROM chapters WHERE book_id = ?)
        """, (platform, book_id))
        purged = cur.rowcount

    await db.commit()
    if not removed_binding and purged == 0:
        raise HTTPException(status_code=404, detail="该书尚未绑定该平台")
    return {"success": True, "removed_binding": removed_binding, "purged_records": purged}


async def _progress_floor(book_id: int, platform: str, db: aiosqlite.Connection) -> Optional[int]:
    """排期下界：平台上已发到第几章 与 本地已有 published 记录的最大章号 取较大者。

    返回 None 表示「尚未核对过平台进度」，调用方必须拒绝排期（硬拦截）。
    """
    cur = await db.execute(
        "SELECT published_up_to, progress_checked_at FROM book_platforms WHERE book_id = ? AND platform = ?",
        (book_id, platform),
    )
    row = await cur.fetchone()
    if not row or not row["progress_checked_at"]:
        return None

    platform_up_to = row["published_up_to"] or 0

    cur = await db.execute("""
        SELECT COALESCE(MAX(ch.chapter_number), 0)
        FROM publish_records pr JOIN chapters ch ON ch.id = pr.chapter_id
        WHERE ch.book_id = ? AND pr.platform = ? AND pr.status = 'published'
    """, (book_id, platform))
    local_published = (await cur.fetchone())[0] or 0

    return max(platform_up_to, local_published)


@app.get("/api/books/{book_id}/platforms/{platform}/plan")
async def get_platform_plan(
    book_id: int, platform: str, db: aiosqlite.Connection = Depends(get_db)
):
    """排期方案预览：每天要发哪几章、合计多少字、还差多少字要补写。

    纯只读，不落库；没有对账基准时 floor=已发布章号。
    """
    if platform not in ("qidian", "fanqie"):
        raise HTTPException(status_code=400, detail="不支持的发布平台")

    cur = await db.execute(
        "SELECT schedule, published_up_to, progress_checked_at FROM book_platforms "
        "WHERE book_id = ? AND platform = ?",
        (book_id, platform),
    )
    bind = await cur.fetchone()
    schedule = (bind["schedule"] if bind else "") or "每天 20:00"
    floor = (bind["published_up_to"] or 0) if (bind and bind["progress_checked_at"]) else 0

    plan = await build_plan(book_id, platform, schedule, floor, db)
    plan["reconciled"] = bool(bind and bind["progress_checked_at"])
    return plan


@app.get("/api/task-groups")
async def get_task_groups(db: aiosqlite.Connection = Depends(get_db)):
    """底部任务列表的数据源：按「平台 × 书籍」归类，明确区分发布任务与撰写任务。"""
    cur = await db.execute("""
        SELECT bp.id, bp.book_id, bp.platform, bp.schedule, bp.enabled,
               bp.published_up_to, bp.progress_checked_at, bp.progress_source,
               b.name AS book_name
        FROM book_platforms bp JOIN books b ON b.id = bp.book_id
        ORDER BY bp.platform ASC, bp.book_id ASC
    """)
    binds = [dict(r) for r in await cur.fetchall()]

    groups = []
    for bp in binds:
        book_id = bp["book_id"]
        platform = bp["platform"]
        reconciled = bool(bp["progress_checked_at"])
        floor = (bp["published_up_to"] or 0) if reconciled else 0
        schedule = bp["schedule"] or ""

        # ── 发布任务：未完工的排期，按日期分组 ──
        cur = await db.execute("""
            SELECT pr.id, pr.status, pr.scheduled_time, pr.error_message,
                   ch.chapter_number, ch.title, COALESCE(ch.word_count, 0) AS words
            FROM publish_records pr JOIN chapters ch ON ch.id = pr.chapter_id
            WHERE ch.book_id = ? AND pr.platform = ?
              AND pr.status IN ('scheduled', 'pending', 'publishing', 'failed', 'skipped')
            ORDER BY pr.scheduled_time ASC, ch.chapter_number ASC
        """, (book_id, platform))
        rows = [dict(r) for r in await cur.fetchall()]

        by_day: Dict[str, list] = {}
        for r in rows:
            d = (r["scheduled_time"] or "")[:10] or "未定日期"
            by_day.setdefault(d, []).append(r)

        plan = await build_plan(book_id, platform, schedule, floor, db)
        quota = plan["quota"]

        publish_days = []
        for d in sorted(by_day):
            items = by_day[d]
            words = sum(i["words"] for i in items)
            publish_days.append({
                "date": d,
                "time": (items[0]["scheduled_time"] or "")[11:16],
                "words": words,
                "quota": quota,
                "met": quota <= 0 or words >= quota,
                "items": items,
            })

        # ── 撰写任务：该书当日有改动的章节（沿用日历「书稿变动」口径） ──
        cur = await db.execute("""
            SELECT chapter_number, title, COALESCE(word_count, 0) AS words
            FROM chapters
            WHERE book_id = ? AND date(file_mtime) = date('now', 'localtime')
            ORDER BY chapter_number ASC
        """, (book_id,))
        written_today = [dict(r) for r in await cur.fetchall()]

        cur = await db.execute("""
            SELECT COUNT(*) AS n, COALESCE(SUM(COALESCE(word_count, 0)), 0) AS w
            FROM chapters WHERE book_id = ? AND status = 'pending'
        """, (book_id,))
        pend = dict(await cur.fetchone())

        groups.append({
            "id": bp["id"],
            "platform": platform,
            "platform_name": "起点中文网" if platform == "qidian" else "番茄小说",
            "book_id": book_id,
            "book_name": bp["book_name"],
            "schedule": schedule,
            "enabled": bp["enabled"],
            "published_up_to": bp["published_up_to"],
            "progress_checked_at": bp["progress_checked_at"],
            "progress_source": bp["progress_source"],
            "reconciled": reconciled,
            "quota": quota,
            "chapter_min_words": plan["chapter_min_words"],
            "group_days": plan["group_days"],
            "avg_chapter_words": plan["avg_chapter_words"],
            "publish_days": publish_days,
            "written_today": written_today,
            "pending_chapters": pend["n"],
            "pending_words": pend["w"],
            "chapters_to_write": plan["chapters_to_write"],
            "next_group_shortfall_words": plan["next_group_shortfall_words"],
        })

    return {"groups": groups, "group_days": await get_group_days(db)}



# ─────────────────────  对账：平台已发进度核对  ─────────────────────

@app.post("/api/books/{book_id}/platforms/{platform}/check-progress")
async def check_platform_progress(
    book_id: int, platform: str, req: ProgressCheckRequest, db: aiosqlite.Connection = Depends(get_db)
):
    """自动核对：打开平台后台读「已发布到第几章」。

    只读、不落库；解析不到就返回 ok=False 与原因（前端据此提示手工填写）。
    """
    if platform not in ("qidian", "fanqie"):
        raise HTTPException(status_code=400, detail="不支持的发布平台")

    cur = await db.execute("SELECT name FROM books WHERE id = ?", (book_id,))
    book = await cur.fetchone()
    if not book:
        raise HTTPException(status_code=404, detail="书籍不存在")

    pb_id = (req.platform_book_id or "").strip()
    if not pb_id:
        cur = await db.execute(
            "SELECT platform_book_id FROM book_platforms WHERE book_id = ? AND platform = ?",
            (book_id, platform),
        )
        bp = await cur.fetchone()
        pb_id = (bp["platform_book_id"] if bp else "") or ""

    if is_browser_busy():
        return {"ok": False, "error": "浏览器正忙（可能有发布任务在执行），请稍候再核对"}

    res = await fetch_platform_progress(platform, pb_id or None, book["name"])
    res["book_name"] = book["name"]
    res["platform"] = platform
    return res


@app.post("/api/books/{book_id}/platforms/{platform}/progress")
async def save_platform_progress(
    book_id: int, platform: str, req: ProgressUpdate, db: aiosqlite.Connection = Depends(get_db)
):
    """确立对账基准（自动核对结果确认后落库，或手工录入）。"""
    if platform not in ("qidian", "fanqie"):
        raise HTTPException(status_code=400, detail="不支持的发布平台")
    if req.published_up_to < 0:
        raise HTTPException(status_code=400, detail="已发章号不能为负数")

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    pb_id = (req.platform_book_id or "").strip()

    cur = await db.execute(
        "SELECT id FROM book_platforms WHERE book_id = ? AND platform = ?", (book_id, platform)
    )
    existing = await cur.fetchone()

    if existing:
        await db.execute("""
            UPDATE book_platforms
            SET published_up_to = ?, progress_checked_at = ?, progress_source = ?,
                platform_book_id = CASE WHEN ? <> '' THEN ? ELSE platform_book_id END
            WHERE id = ?
        """, (req.published_up_to, now_str, req.source, pb_id, pb_id, existing["id"]))
    else:
        await db.execute("""
            INSERT INTO book_platforms
            (book_id, platform, platform_book_id, schedule, chapters_per_run, ai_content, enabled,
             published_up_to, progress_checked_at, progress_source)
            VALUES (?, ?, ?, '', 1, 0, 0, ?, ?, ?)
        """, (book_id, platform, pb_id, req.published_up_to, now_str, req.source))
    await db.commit()

    # 基准之下的既有排期就是重复发布的隐患，回报给前端提示清理（不自动删）
    cur = await db.execute("""
        SELECT COUNT(*) FROM publish_records pr JOIN chapters ch ON ch.id = pr.chapter_id
        WHERE ch.book_id = ? AND pr.platform = ?
          AND pr.status IN ('scheduled', 'pending', 'publishing')
          AND COALESCE(ch.chapter_number, 0) <= ?
    """, (book_id, platform, req.published_up_to))
    stale = (await cur.fetchone())[0]

    return {
        "success": True,
        "published_up_to": req.published_up_to,
        "checked_at": now_str,
        "source": req.source,
        "stale_scheduled": stale,
    }


# ─────────────────────  发布记录与立即发书  ─────────────────────

@app.get("/api/publish-records")
async def list_publish_records(
    status: Optional[str] = None,
    limit: int = 20,
    db: aiosqlite.Connection = Depends(get_db)
):
    query = """
        SELECT pr.*, ch.title as chapter_title, ch.book_id, b.name as book_name
        FROM publish_records pr
        JOIN chapters ch ON pr.chapter_id = ch.id
        JOIN books b ON ch.book_id = b.id
        WHERE 1=1
    """
    params = []
    if status:
        query += " AND pr.status = ?"
        params.append(status)
    query += " ORDER BY pr.created_at DESC LIMIT ?"
    params.append(limit)

    cursor = await db.execute(query, params)
    records = [dict(row) for row in await cursor.fetchall()]
    return {"records": records}


@app.post("/api/records/{record_id}/publish-now")
async def publish_record_now(record_id: int, db: aiosqlite.Connection = Depends(get_db)):
    res = await execute_publish_record(record_id, db)
    if res.get("success"):
        return {"success": True, "message": "已成功发布该章节！", "url": res.get("url")}
    else:
        raise HTTPException(status_code=500, detail=res.get("error", "发布失败"))


@app.post("/api/records/{record_id}/retry")
async def retry_record(record_id: int, db: aiosqlite.Connection = Depends(get_db)):
    # 时区必须与排期/调度器一致：全链路用本地时间字符串。
    # 原先用 CURRENT_TIMESTAMP（UTC）会让记录在日历上前移一天（并立刻被判为到期）。
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        "UPDATE publish_records SET status = 'pending', scheduled_time = ?, retry_count = retry_count + 1 WHERE id = ?",
        (now_str, record_id)
    )
    await db.commit()
    return {"success": True, "message": "已重新加入队列"}


@app.post("/api/records/{record_id}/cancel")
async def cancel_record(record_id: int, db: aiosqlite.Connection = Depends(get_db)):
    await db.execute("DELETE FROM publish_records WHERE id = ?", (record_id,))
    await db.commit()
    return {"success": True}


@app.delete("/api/records/{record_id}")
async def delete_record(record_id: int, db: aiosqlite.Connection = Depends(get_db)):
    """删除单条发布记录（任何状态都可删；仅删排期，不动章节与绑定）"""
    cur = await db.execute("DELETE FROM publish_records WHERE id = ?", (record_id,))
    await db.commit()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="记录不存在")
    return {"success": True, "deleted": cur.rowcount}


@app.delete("/api/books/{book_id}/platforms/{platform}/records")
async def clear_book_platform_records(book_id: int, platform: str, db: aiosqlite.Connection = Depends(get_db)):
    """清空「某书 × 某平台」的全部未发布排期（scheduled / pending），保留已发布与失败留痕。

    keep_all=1 可连同失败记录一并清掉。
    """
    cur = await db.execute("""
        DELETE FROM publish_records
        WHERE platform = ?
          AND status IN ('scheduled', 'pending', 'publishing')
          AND chapter_id IN (SELECT id FROM chapters WHERE book_id = ?)
    """, (platform, book_id))
    await db.commit()
    return {"success": True, "deleted": cur.rowcount}


@app.get("/api/books/{book_id}/platforms/{platform}/records")
async def list_book_platform_records(book_id: int, platform: str, db: aiosqlite.Connection = Depends(get_db)):
    """该书该平台的全部记录（用于「清空排期」前的数量确认）"""
    cur = await db.execute("""
        SELECT pr.id, pr.status, pr.scheduled_time, ch.chapter_number, ch.title
        FROM publish_records pr JOIN chapters ch ON ch.id = pr.chapter_id
        WHERE pr.platform = ? AND ch.book_id = ?
        ORDER BY ch.chapter_number ASC
    """, (platform, book_id))
    rows = [dict(r) for r in await cur.fetchall()]
    active = sum(1 for r in rows if r["status"] in ("scheduled", "pending", "publishing"))
    return {"records": rows, "active": active, "total": len(rows)}


# ─────────────────────  平台登录与状态  ─────────────────────

@app.get("/api/platforms/status")
async def get_platform_status(db: aiosqlite.Connection = Depends(get_db)):
    platforms = []
    for pid, name, color in [("qidian", "起点中文网", "#e94560"), ("fanqie", "番茄小说", "#ff6b35")]:
        logged_in = check_platform_login_profile(pid)
        cursor = await db.execute(
            "SELECT COUNT(*) FROM publish_records WHERE platform=? AND status='published' AND date(actual_time) = date('now', 'localtime')",
            (pid,)
        )
        today_published = (await cursor.fetchone())[0]

        cursor = await db.execute(
            "SELECT COUNT(*) FROM publish_records WHERE platform=? AND status='scheduled'",
            (pid,)
        )
        total_scheduled = (await cursor.fetchone())[0]

        platforms.append({
            "id": pid,
            "name": name,
            "color": color,
            "logged_in": logged_in,
            "last_check": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "today_published": today_published,
            "total_scheduled": total_scheduled,
        })

    return {"platforms": platforms}


@app.post("/api/platforms/{platform}/login")
async def login_platform(platform: str, bg_tasks: BackgroundTasks):
    if platform not in ("qidian", "fanqie"):
        raise HTTPException(status_code=400, detail="不支持的发布平台")
    if is_browser_busy():
        raise HTTPException(status_code=409, detail="浏览器正被占用，请稍候")
    bg_tasks.add_task(trigger_platform_login, platform, 180)
    return {"success": True, "message": f"已启动 {platform} 登录界面，请在弹出的浏览器窗口中扫码"}


@app.get("/api/system/status")
async def get_system_status():
    return {
        "running": True,
        "version": "0.3.2",
        "scheduler_running": is_scheduler_running(),
        "browser_running": is_browser_busy(),
    }
