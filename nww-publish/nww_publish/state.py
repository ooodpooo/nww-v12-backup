"""发布状态机：跟踪每本书、每章的发布进度，支持断点续发与每日上限计数。

状态流转：
  pending → publishing → published
                    └──────→ failed
  (skipped 由用户显式跳过)

存储：<workspace>/state/publish-state.json
结构：
{
  "version": 1,
  "books": { "<platform>:<book_key>": { platform, book_key, book_id, chapters: [...] } },
  "daily_counts": { "2026-09-09": 3 }
}
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

STATUS_PENDING = "pending"
STATUS_PUBLISHING = "publishing"
STATUS_PUBLISHED = "published"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _today_local() -> str:
    return date.today().isoformat()


class PublishState:
    def __init__(self, workspace: Path):
        self.dir = workspace / "state"
        self.path = self.dir / "publish-state.json"
        self.data: Dict[str, Any] = {"version": 1, "books": {}, "daily_counts": {}}
        self.load()

    # ---- IO ----
    def load(self) -> None:
        if self.path.exists():
            with open(self.path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
            self.data.setdefault("version", 1)
            self.data.setdefault("books", {})
            self.data.setdefault("daily_counts", {})

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    # ---- book / chapter helpers ----
    def _book_key(self, platform: str, book_key: str) -> str:
        return f"{platform}:{book_key}"

    def get_book(self, platform: str, book_key: str) -> Dict[str, Any]:
        key = self._book_key(platform, book_key)
        if key not in self.data["books"]:
            self.data["books"][key] = {
                "platform": platform,
                "book_key": book_key,
                "book_id": None,
                "chapters": [],
            }
        return self.data["books"][key]

    def set_book_id(self, platform: str, book_key: str, book_id: str) -> None:
        self.get_book(platform, book_key)["book_id"] = book_id
        self.save()

    def add_chapter(
        self,
        platform: str,
        book_key: str,
        chapter_file: str,
        title: str,
        scheduled_at: str,
        chapter_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        book = self.get_book(platform, book_key)
        cid = chapter_id or f"{platform}:{book_key}:{len(book['chapters']) + 1:03d}"
        # 去重：同一 chapter_file 已存在则覆盖计划时间
        for ch in book["chapters"]:
            if ch["chapter_file"] == chapter_file:
                ch["scheduled_at"] = scheduled_at
                ch["title"] = title
                ch["status"] = STATUS_PENDING
                ch["error"] = None
                self.save()
                return ch
        ch: Dict[str, Any] = {
            "id": cid,
            "platform": platform,
            "book_key": book_key,
            "chapter_file": chapter_file,
            "title": title,
            "status": STATUS_PENDING,
            "scheduled_at": scheduled_at,
            "published_at": None,
            "platform_chapter_id": None,
            "platform_url": None,
            "error": None,
            "attempts": 0,
        }
        book["chapters"].append(ch)
        self.save()
        return ch

    def get_chapter(self, chapter_id: str) -> Optional[Dict[str, Any]]:
        for book in self.data["books"].values():
            for ch in book["chapters"]:
                if ch["id"] == chapter_id:
                    return ch
        return None

    def mark(self, chapter_id: str, **fields) -> None:
        ch = self.get_chapter(chapter_id)
        if ch is None:
            return
        ch.update(fields)
        if "status" in fields and fields["status"] == STATUS_PUBLISHED:
            ch["published_at"] = _now_iso()
            self._bump_daily()
        self.save()

    def _bump_daily(self) -> None:
        day = _today_local()
        self.data["daily_counts"][day] = self.data["daily_counts"].get(day, 0) + 1

    def daily_count(self, day: Optional[str] = None) -> int:
        return self.data["daily_counts"].get(day or _today_local(), 0)

    # ---- queries ----
    def due_chapters(self, now: Optional[datetime] = None, include_failed: bool = False) -> List[Dict[str, Any]]:
        now = now or datetime.now(timezone.utc).astimezone()
        out = []
        for book in self.data["books"].values():
            for ch in book["chapters"]:
                if ch["status"] == STATUS_PUBLISHED or ch["status"] == STATUS_SKIPPED:
                    continue
                if ch["status"] == STATUS_FAILED and not include_failed:
                    continue
                try:
                    scheduled = datetime.fromisoformat(ch["scheduled_at"])
                except Exception:
                    scheduled = now
                if scheduled <= now:
                    out.append(ch)
        return out

    def all_chapters(self) -> List[Dict[str, Any]]:
        out = []
        for book in self.data["books"].values():
            out.extend(book["chapters"])
        return out

    def summary(self) -> Dict[str, int]:
        counts = {STATUS_PENDING: 0, STATUS_PUBLISHING: 0, STATUS_PUBLISHED: 0, STATUS_FAILED: 0, STATUS_SKIPPED: 0}
        for ch in self.all_chapters():
            counts[ch["status"]] = counts.get(ch["status"], 0) + 1
        return counts
