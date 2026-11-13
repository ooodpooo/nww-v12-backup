"""发书执行器：桥接根目录 nww-publish 自动化引擎与 Playwright 适配器"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional
from datetime import datetime
import aiosqlite

REPO_ROOT = Path(__file__).resolve().parents[3]
NWW_PUBLISH_ROOT = REPO_ROOT / "nww-publish"
if str(NWW_PUBLISH_ROOT) not in sys.path:
    sys.path.insert(0, str(NWW_PUBLISH_ROOT))

from nww_publish.config import Config
from nww_publish.adapters import get_adapter
from .database import BASE_DIR, read_chapter_content

logger = logging.getLogger("publisher.login")

_platform_locks: Dict[str, asyncio.Lock] = {}
_browser_busy: bool = False


def get_platform_lock(platform: str) -> asyncio.Lock:
    if platform not in _platform_locks:
        _platform_locks[platform] = asyncio.Lock()
    return _platform_locks[platform]


def is_browser_busy() -> bool:
    return _browser_busy


def get_publish_config() -> Config:
    return Config()


# 各平台「登录票据」cookie 的特征名（小写，命中任一即视为已登录）。
# 注意：设备/统计类 cookie（ZQuuid、ttwid、Hm_*、serial_uuid…）**不构成登录态**。
# 旧实现只判 profile 目录非空，会把「只建了统计 cookie 的空目录」误报成「会话有效」。
_LOGIN_COOKIE_HINTS: Dict[str, tuple] = {
    "qidian": ("ywkey", "ywguid", "ywopenid", "ywpassword", "logintype", "qidian_user_id"),
    "fanqie": ("sessionid", "sessionid_ss", "sid_tt", "odin_tt", "uid_tt", "passport_csrf_token"),
}


def check_platform_login_profile(platform: str) -> bool:
    """检查平台是否**真的持有登录票据**（读 Chromium Cookie 库，不是只看目录存在）。

    Cookie 的 host_key / name 是明文，只有 value 加密——足以判定登录态，
    无需解密、无需启动浏览器。
    """
    cfg = get_publish_config()
    try:
        cookie_db = cfg.abs_user_data_dir(platform) / "Default" / "Cookies"
        if not cookie_db.exists():
            return False
        names: set = set()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "Cookies"
            shutil.copy(cookie_db, tmp)  # 库可能被 Chromium 独占，拷副本读
            con = sqlite3.connect(str(tmp))
            try:
                for (_host, name) in con.execute("SELECT host_key, name FROM cookies"):
                    names.add(str(name).lower())
            finally:
                con.close()
        hints = _LOGIN_COOKIE_HINTS.get(platform, ())
        return bool(names & set(hints))
    except Exception:
        return False


async def trigger_platform_login(platform: str, timeout_sec: int = 120) -> Dict[str, Any]:
    """后台弹出有头浏览器，供用户手动扫码登录"""
    global _browser_busy
    lock = get_platform_lock(platform)
    if lock.locked():
        return {"success": False, "message": f"{platform} 当前正有任务占用浏览器，请稍候再试"}

    async with lock:
        _browser_busy = True
        try:
            cfg = get_publish_config()
            base_dir = NWW_PUBLISH_ROOT / "nww_publish"
            plat_cfg = cfg.platform(platform).copy()
            plat_cfg["headless"] = False  # 扫码必须有界面

            adapter = get_adapter(platform, plat_cfg, base_dir)
            async with adapter:
                page = await adapter.new_page()
                await page.goto(adapter.cfg["login_url"], wait_until="domcontentloaded")

                logger.info(f"已唤起 {platform} 登录窗口，等待扫码（{timeout_sec}s）：{page.url}")

                start_time = asyncio.get_event_loop().time()
                logged_in = False
                last_slot = -1
                while asyncio.get_event_loop().time() - start_time < timeout_sec:
                    if await adapter.is_logged_in(page):
                        logged_in = True
                        break
                    # 每 15 秒打一次心跳：便于区分「用户还没扫」与「扫了但页面没跳转」
                    elapsed = int(asyncio.get_event_loop().time() - start_time)
                    if elapsed // 15 != last_slot:
                        last_slot = elapsed // 15
                        logger.info(f"等待 {platform} 扫码中… 已 {elapsed}s，当前 URL: {page.url}")
                    await asyncio.sleep(2)

                if logged_in:
                    logger.info(f"{platform} 登录成功，会话已持久化到 profile")
                    return {"success": True, "message": f"{platform} 登录成功，会话已持久化"}
                logger.warning(f"{platform} 登录等待超时（{timeout_sec}s），当前 URL: {page.url}")
                return {"success": False, "message": f"{platform} 登录等待超时，请重新扫码"}
        except Exception as e:
            logger.error(f"唤起 {platform} 登录异常: {e}", exc_info=True)
            return {"success": False, "message": f"唤起登录异常: {str(e)}"}
        finally:
            _browser_busy = False


async def fetch_platform_progress(
    platform: str, platform_book_id: Optional[str] = None, book_name: Optional[str] = None
) -> Dict[str, Any]:
    """打开平台后台读取该书「已发布到第几章」，供排期前对账。

    只读取、不写库、不动章节状态；解析失败一律返回 ok=False，绝不返回猜测值。
    """
    global _browser_busy
    lock = get_platform_lock(platform)
    if lock.locked():
        return {"ok": False, "error": f"{platform} 当前有任务占用浏览器，请稍候再核对"}
    if _browser_busy:
        return {"ok": False, "error": "浏览器正忙（可能有发布任务在执行），请稍候"}

    async with lock:
        _browser_busy = True
        try:
            cfg = get_publish_config()
            base_dir = NWW_PUBLISH_ROOT / "nww_publish"
            plat_cfg = cfg.platform(platform).copy()
            # 核对是只读操作，用有头模式便于用户看到页面、必要时人工干预
            adapter = get_adapter(platform, plat_cfg, base_dir)
            async with adapter:
                page = await adapter.new_page()
                return await adapter.fetch_published_progress(
                    page, book_ref=platform_book_id or None, book_name=book_name or None
                )
        except Exception as e:
            return {"ok": False, "error": f"核对异常: {e}"}
        finally:
            _browser_busy = False


async def execute_publish_record(record_id: int, db: aiosqlite.Connection) -> Dict[str, Any]:
    """执行单条待发布记录"""
    global _browser_busy
    cursor = await db.execute("""
        SELECT pr.id, pr.chapter_id, pr.platform, pr.retry_count,
               ch.title as chapter_title, ch.file_path, ch.book_id,
               bp.platform_book_id
        FROM publish_records pr
        JOIN chapters ch ON pr.chapter_id = ch.id
        LEFT JOIN book_platforms bp ON bp.book_id = ch.book_id AND bp.platform = pr.platform
        WHERE pr.id = ?
    """, (record_id,))
    record = await cursor.fetchone()
    if not record:
        return {"success": False, "error": "发布记录不存在"}

    platform = record["platform"]
    chapter_title = record["chapter_title"]
    raw_path = record["file_path"]
    
    # 优先在 REPO_ROOT 查找，兜底在 BASE_DIR 查找
    file_path = REPO_ROOT / raw_path
    if not file_path.exists():
        file_path = BASE_DIR / raw_path

    platform_book_id = record["platform_book_id"] or None

    if not file_path.exists():
        err = f"章节文件不存在: {raw_path}"
        await db.execute(
            "UPDATE publish_records SET status='failed', error_message=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (err, record_id)
        )
        await db.commit()
        return {"success": False, "error": err}

    body = read_chapter_content(str(file_path))

    await db.execute(
        "UPDATE publish_records SET status='publishing', updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (record_id,)
    )
    await db.commit()

    lock = get_platform_lock(platform)
    async with lock:
        _browser_busy = True
        try:
            cfg = get_publish_config()
            base_dir = NWW_PUBLISH_ROOT / "nww_publish"
            plat_cfg = cfg.platform(platform).copy()

            cursor = await db.execute("SELECT value FROM settings WHERE key='browser_headless'")
            s_row = await cursor.fetchone()
            if s_row and s_row[0]:
                plat_cfg["headless"] = s_row[0].lower() in ("true", "1", "yes")

            adapter = get_adapter(platform, plat_cfg, base_dir)
            async with adapter:
                page = await adapter.new_page()
                is_login = await adapter.is_logged_in(page)
                if not is_login:
                    await page.goto(adapter.cfg["login_url"], wait_until="domcontentloaded")
                    await asyncio.sleep(1.5)
                    is_login = await adapter.is_logged_in(page)

                if not is_login:
                    raise RuntimeError(f"{platform} 平台会话已失效，请重新扫码登录")

                res = await adapter.publish_chapter(
                    page=page,
                    book_id=platform_book_id,
                    title=chapter_title,
                    body=body,
                    draft=False,
                )

            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if res.get("ok"):
                await db.execute("""
                    UPDATE publish_records
                    SET status='published', actual_time=?, error_message='', updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                """, (now_str, record_id))
                await db.execute(
                    "UPDATE chapters SET status='published' WHERE id=?",
                    (record["chapter_id"],)
                )
                await db.execute("""
                    UPDATE book_platforms
                    SET last_published_at=?
                    WHERE book_id=? AND platform=?
                """, (now_str, record["book_id"], platform))
                await db.commit()
                return {"success": True, "url": res.get("url")}
            else:
                err = res.get("error", "发布失败")
                await db.execute("""
                    UPDATE publish_records
                    SET status='failed', error_message=?, updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                """, (err, record_id))
                await db.commit()
                return {"success": False, "error": err}

        except Exception as e:
            err = str(e)
            await db.execute("""
                UPDATE publish_records
                SET status='failed', error_message=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, (err, record_id))
            await db.commit()
            return {"success": False, "error": err}
        finally:
            _browser_busy = False
