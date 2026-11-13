"""起点作家适配器。URL 在 config 中，元素选择器在 qidian.selectors.json 中校准。"""
from __future__ import annotations

from typing import Any, Dict

from .base import BaseAdapter


class QidianAdapter(BaseAdapter):
    name = "qidian"

    async def list_books(self, page) -> list[Dict[str, Any]]:
        # 起点书籍管理页为 SPA，登录后 dump 疑似书籍链接；校准后可覆盖为精确逻辑
        return await super().list_books(page)
