"""番茄作家适配器。URL 在 config 中，元素选择器在 fanqie.selectors.json 中校准。"""
from __future__ import annotations

from typing import Any, Dict

from .base import BaseAdapter


class FanqieAdapter(BaseAdapter):
    name = "fanqie"

    async def list_books(self, page) -> list[Dict[str, Any]]:
        return await super().list_books(page)
