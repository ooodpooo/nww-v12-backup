"""适配器工厂：按平台名加载对应适配器与已校准的选择器。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from .base import BaseAdapter
from .qidian import QidianAdapter
from .fanqie import FanqieAdapter

_REGISTRY = {"qidian": QidianAdapter, "fanqie": FanqieAdapter}


def load_selectors(path: Path) -> Dict[str, Any]:
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if not k.startswith("_")}
    return {}


def get_adapter(name: str, config, base_dir: Path) -> BaseAdapter:
    """name: qidian | fanqie。config: 该平台的配置 dict。base_dir: 选择器文件相对基准（workspace）。"""
    if name not in _REGISTRY:
        raise ValueError(f"未知平台: {name}")
    sel_path = (base_dir / config["selectors_file"]) if not Path(config["selectors_file"]).is_absolute() else Path(config["selectors_file"])
    sel = load_selectors(sel_path)
    user_data_dir = config["user_data_dir"]
    headless = config.get("headless", False)
    return _REGISTRY[name](config, sel, Path(user_data_dir), headless=headless)
