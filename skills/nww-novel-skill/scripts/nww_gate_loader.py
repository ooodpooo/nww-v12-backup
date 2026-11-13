#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nww_gate_loader.py — 道闸策略模块的唯一加载入口（C-GATE-LOADER@1）。

web 后端（`pipeline._gate` / `gate_facts.policy`）与脚本侧都不共享 sys.path，
故按 ``scripts_dir`` 里的同一份 ``nww_gate_policy.py`` 用 importlib 加载。
加载、缓存、可用性判定与降级状态只在此处维护。

降级状态：
  * ``ready``            真实模块可用；
  * ``fallback_strict``  生成侧口径：策略缺失按 strict 语义继续（fail-closed）；
  * ``fallback_empty``   展示侧口径：策略缺失按空事实继续（fail-open）。

``load`` 自身永不抛异常；调用方据 status 把降级事实写进自己的产物
（生成侧 stage detail、展示侧事实卡），方向由调用方决定，本模块不统一方向。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

POLICY_FILENAME = "nww_gate_policy.py"

STATUS_READY = "ready"
STATUS_FALLBACK_STRICT = "fallback_strict"
STATUS_FALLBACK_EMPTY = "fallback_empty"
DEGRADED_STATUSES = frozenset({STATUS_FALLBACK_STRICT, STATUS_FALLBACK_EMPTY})

# 阳性对照：缺任一符号即视为策略层不可用，绝不半加载后静默给出错误形状
REQUIRED_SYMBOLS: tuple[str, ...] = (
    "report", "decision", "decide", "pending", "card_fingerprint",
    "would_block", "adjudication_status", "gate_mode",
)

_CACHE_MAX_ENTRIES = 64
_NOT_CACHED = object()


def _empty_card(chapter: Any = 0) -> dict[str, Any]:
    try:
        chapter_no = int(chapter)
    except (TypeError, ValueError):
        chapter_no = 0
    return {"chapter": chapter_no, "mode": "unknown", "status": "empty",
            "count": 0, "findings": []}


class GateFallback:
    """策略层不可用时的替身：strict 与 empty 两种口径共用同一形状。

    ``is_v13()->False`` 让生成侧保持 V12 硬闸语义；``report`` 返回空卡让展示侧
    得到可渲染的空事实。其余符号按空值返回，避免调用方在降级路径上再分叉。
    """

    @staticmethod
    def is_v13() -> bool:
        return False

    @staticmethod
    def record(*args: Any, **kwargs: Any) -> None:
        return None

    @staticmethod
    def note(*args: Any, **kwargs: Any) -> None:
        return None

    @staticmethod
    def report(project: Any = None, chapter: Any = 0) -> dict[str, Any]:
        return _empty_card(chapter)

    @staticmethod
    def decision(*args: Any, **kwargs: Any) -> None:
        return None

    @staticmethod
    def decide(*args: Any, **kwargs: Any) -> None:
        return None

    @staticmethod
    def pending(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"available": False, "mode": "unknown", "chapters": [],
                "pending_count": 0}

    @staticmethod
    def card_fingerprint(card: dict[str, Any] | None) -> str:
        return ""

    @staticmethod
    def would_block(card: dict[str, Any] | None) -> list[str]:
        return []

    @staticmethod
    def adjudication_status(card: dict[str, Any] | None,
                            row: dict[str, Any] | None) -> str:
        return "pending"

    @staticmethod
    def gate_mode() -> str:
        return "unknown"


_FALLBACK = GateFallback

# 缓存键 (resolved_path, st_mtime_ns, st_size)；值为模块对象或 None（不可用）
_CACHE: dict[tuple[str, int, int], object | None] = {}


def policy_path(scripts_dir: Path | str) -> Path:
    return Path(scripts_dir) / POLICY_FILENAME


def _cache_key(path: Path) -> tuple[str, int, int]:
    try:
        st = path.stat()
    except OSError:
        return (str(path.resolve()), -1, -1)
    return (str(path.resolve()), st.st_mtime_ns, st.st_size)


def _import_policy(path: Path) -> object | None:
    """加载并校验模块；任何失败返回 None（诊断已打印）。"""
    try:
        if not path.is_file():
            raise FileNotFoundError(path.name)
        spec = importlib.util.spec_from_file_location("nww_gate_policy_shared", path)
        if spec is None or spec.loader is None:
            raise RuntimeError("spec_from_file_location returned empty spec")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        missing = [name for name in REQUIRED_SYMBOLS
                   if not callable(getattr(module, name, None))]
        if missing:
            raise RuntimeError(f"missing symbols: {', '.join(missing)}")
        return module
    except Exception as exc:  # noqa: BLE001 — 加载失败按降级处理，不向上抛
        print(f"[GATE] policy module unavailable: {exc}", file=sys.stderr)
        return None


def _normalize_fallback(fallback: str | None, mode: str | None) -> str:
    kind = str(mode if mode is not None else (fallback or "strict")).strip().lower()
    if kind in ("empty", STATUS_FALLBACK_EMPTY):
        return STATUS_FALLBACK_EMPTY
    return STATUS_FALLBACK_STRICT


def load(scripts_dir: Path | str, *, fallback: str = "strict",
         mode: str | None = None) -> tuple[object, str]:
    """加载（并按 mtime/size 缓存）策略模块，返回 ``(module, status)``。

    ``fallback``（别名 ``mode``）只决定降级时的 status 口径：``"strict"`` →
    ``fallback_strict``，``"empty"`` → ``fallback_empty``；模块可用时恒为 ``ready``。
    """
    fallback_status = _normalize_fallback(fallback, mode)
    try:
        path = policy_path(scripts_dir)
        key = _cache_key(path)
        cached = _CACHE.get(key, _NOT_CACHED)
        if cached is _NOT_CACHED:
            cached = _import_policy(path)
            if len(_CACHE) >= _CACHE_MAX_ENTRIES:
                _CACHE.clear()
            _CACHE[key] = cached
        if cached is None:
            return _FALLBACK, fallback_status
        return cached, STATUS_READY
    except Exception as exc:  # noqa: BLE001 — 契约要求 load 永不抛
        print(f"[GATE] policy load failed: {exc}", file=sys.stderr)
        return _FALLBACK, fallback_status