"""配置管理：加载/保存 nww-publish 的全局配置。

配置默认放在 ~/.nww/publish/config.json，可通过环境变量 NWW_PUBLISH_HOME 覆盖。
所有含 ~ 的路径都会被展开。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

DEFAULT_HOME = os.path.expanduser("~/.nww/publish")

DEFAULT_CONFIG: Dict[str, Any] = {
    "workspace": DEFAULT_HOME,
    "platforms": {
        "qidian": {
            "enabled": True,
            "headless": False,
            "user_data_dir": os.path.join(DEFAULT_HOME, "profiles", "qidian"),
            "selectors_file": "adapters/qidian.selectors.json",
            # 作品管理页：登录态激活后可直接打开，页面每本书带「第N章 xxx」即已发进度，
            # 可直接作为对账来源（无需再进章节页）。
            "dashboard_url": "https://write.qq.com/portal/dashboard/books",
            # 「新建章节」写作页。{book_id} 占位 = **作家专区 CBID**（不是公开书号！）。
            # 例：五行归源 CBID=36799582707161807（公开书号是 1050707296，两者不可混用）。
            "new_chapter_url": "https://write.qq.com/portal/booknovels/chaptertmp/CBID/{book_id}/addType/1.html",
            # 对账用：该书的章节管理页。留空则退回 dashboard_url 整页扫「第N章」。
            # 填入时可用 {book_id} 占位（会被 platform_book_id 替换）。地址需自行核实，勿猜。
            "chapter_list_url": None,
            # ⚠️ 起点作家专区（write.qq.com）**必须经此页跳一次才会激活登录态**：
            # 直接访问 dashboard 首页/作品管理页都拿不到登录态（页面会退化成未登录门户）。
            # 所以 fetch_published_progress 会先访问本 URL 预热，再跳目标页。
            "login_url": "https://write.qq.com/portal/login",
            "logged_in_selector": None,
            "login_form_selector": None,
        },
        "fanqie": {
            "enabled": True,
            "headless": False,
            "user_data_dir": os.path.join(DEFAULT_HOME, "profiles", "fanqie"),
            "selectors_file": "adapters/fanqie.selectors.json",
            "dashboard_url": "https://fanqienovel.com/main/writer/book-manage",
            "new_chapter_url": None,
            # 同 qidian.chapter_list_url：留空则退回 dashboard_url 整页扫「第N章」
            "chapter_list_url": None,
            # 未登录访问 book-manage 会被**自动重定向**到本 URL（2026-09-12 实测），
            # 所以这里直接写真实登录页，别写 book-manage（能跳但多一跳且语义混乱）。
            "login_url": "https://fanqienovel.com/main/writer/login",
            "logged_in_selector": None,
            "login_form_selector": None,
        },
    },
    "safety": {
        "min_interval_sec": 30,
        "jitter_sec": 60,
        "max_chapters_per_day": 5,
        "dry_run_default": False,
        "publish_mode": "publish",  # "publish" | "draft"
    },
    "content": {
        "default_root": "books",
        "chapter_ext": ".md",
    },
}


class Config:
    def __init__(self, home: str | None = None):
        self.home = Path(os.path.expanduser(home or os.environ.get("NWW_PUBLISH_HOME", DEFAULT_HOME)))
        self.path = self.home / "config.json"
        self.data: Dict[str, Any] = {}
        self.load()

    # ---- IO ----
    def load(self) -> None:
        if self.path.exists():
            with open(self.path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
            # 合并缺省，保证新增字段有兜底
            self.data = _deep_merge(DEFAULT_CONFIG, self.data)
        else:
            self.data = json.loads(json.dumps(DEFAULT_CONFIG))
            self.save()
        # 确保 workspace / profiles 目录存在
        self.home.mkdir(parents=True, exist_ok=True)

    def save(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    # ---- accessors ----
    @property
    def safety(self) -> Dict[str, Any]:
        return self.data.setdefault("safety", DEFAULT_CONFIG["safety"])

    def platform(self, name: str) -> Dict[str, Any]:
        return self.data["platforms"][name]

    def resolve(self, p: str) -> Path:
        """把相对 workspace 的路径解析为绝对路径；绝对/~/路径原样展开。"""
        if os.path.isabs(p) or p.startswith("~"):
            return Path(os.path.expanduser(p))
        return (self.home / p).resolve()

    def abs_user_data_dir(self, name: str) -> Path:
        return self.resolve(self.platform(name)["user_data_dir"])

    def abs_selectors_file(self, name: str) -> Path:
        return self.resolve(self.platform(name)["selectors_file"])

    def set(self, dotted: str, value: Any) -> None:
        """通过点路径设置配置项，例如 set('safety.max_chapters_per_day', 3)。"""
        node = self.data
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
        self.save()


def _deep_merge(base: Dict, override: Dict) -> Dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out
