"""道闸事实卡 · web 侧统一适配层（2026-11-13，第二阶段「可见 + 裁决」）。

单一事实源 = ``skills/nww-novel-skill/scripts/nww_gate_policy.py``。本模块**不实现**
任何判据、不复制路径/格式规则，只做「动态加载同一份策略模块 + fail-open 包装」：

* ``dataplane.py``  章节详情 API 挂只读事实卡；
* ``nwwmvp.py``     5 条链的任务结果挂事实卡；
* ``guard.py``      守护中心待裁决列表与三个作者裁决动作。

为什么不直接 ``import nww_gate_policy``：web 后端与脚本子进程的 ``sys.path`` 不共享
（与 ``agents_runtime/pipeline.py::_gate()`` 遇到的约束相同），故按仓库根定位同一份文件。
加载、缓存与降级状态统一走 ``nww_gate_loader.load``（C-GATE-LOADER@1），本模块不再自持
一套 importlib 缓存。本模块不 import 任何生产链模块（pipeline / packet_builder / writer），
保持 guard.py「管理面与生成链解耦」的既有边界。

铁律：任何异常都降级为安全的空值，绝不向 web 路由抛出、绝不让章节详情/任务接口报错。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS_DIR = _REPO_ROOT / "skills" / "nww-novel-skill" / "scripts"
_GATE_LOADER_FILE = _SCRIPTS_DIR / "nww_gate_loader.py"

_MOD: Any = None
_STATUS = "ready"
_LOADER_MOD: Any = None


def _empty_card(chapter: int) -> dict[str, Any]:
    return {"chapter": int(chapter), "mode": "v13", "status": "empty",
            "count": 0, "findings": []}


class _Unavailable:
    """策略层不可用时全部降级为空/缺省；形状与真实策略层一致，前端无需分叉。"""

    @staticmethod
    def report(project: Path, chapter: int) -> dict[str, Any]:
        return _empty_card(chapter)

    @staticmethod
    def decision(project: Path, chapter: int) -> dict[str, Any] | None:
        return None

    @staticmethod
    def decide(*args: Any, **kwargs: Any) -> dict[str, Any] | None:
        return None

    @staticmethod
    def pending(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"available": False, "mode": "unknown", "chapters": [],
                "pending_count": 0, "note": "道闸策略层不可用；本页不据此判定通过与否"}

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


def _gate_loader() -> Any:
    """按仓库根加载共享判据加载器（唯一 load + 缓存 + 状态实现，C-GATE-LOADER@1）。"""
    global _LOADER_MOD
    if _LOADER_MOD is None:
        if not _GATE_LOADER_FILE.is_file():
            raise FileNotFoundError(_GATE_LOADER_FILE.name)
        spec = importlib.util.spec_from_file_location("nww_gate_loader_web", _GATE_LOADER_FILE)
        if spec is None or spec.loader is None:
            raise RuntimeError("spec_from_file_location returned empty spec")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if not callable(getattr(mod, "load", None)):
            raise RuntimeError("nww_gate_loader missing symbol: load")
        _LOADER_MOD = mod
    return _LOADER_MOD


def policy() -> Any:
    """经共享加载器取策略模块；非 ready 一律回落 _Unavailable（展示侧 fail-open）。"""
    global _MOD, _STATUS
    if _MOD is None:
        try:
            # 展示侧 fail-open：降级口径用 empty（空卡），与生成侧 strict 方向相反。
            mod, status = _gate_loader().load(_SCRIPTS_DIR, fallback="empty")
        except Exception as exc:  # noqa: BLE001
            print(f"[GATE] gate loader unavailable, fallback to empty facts: {exc}",
                  file=sys.stderr)
            mod, status = _Unavailable(), "fallback_empty"
        _STATUS = str(status)
        if _STATUS != "ready":
            print(f"[GATE] web policy degraded: status={_STATUS}", file=sys.stderr)
            _MOD = _Unavailable()
        else:
            _MOD = mod
    return _MOD


def load_status() -> str:
    """当前加载状态：ready / fallback_strict / fallback_empty（降级可见，便于排查）。"""
    policy()
    return _STATUS


def _mark_degraded(card: dict[str, Any]) -> dict[str, Any]:
    """降级事实随卡片外显：展示侧仍返回空卡，但必须能看出「读不到」而非「没事实」。"""
    if _STATUS != "ready":
        card["degraded"] = True
        card["degraded_reason"] = _STATUS
    return card


def report(project: Path, chapter: int) -> dict[str, Any]:
    try:
        out = policy().report(project, chapter)
        return _mark_degraded(out if isinstance(out, dict) else _empty_card(chapter))
    except Exception as exc:  # noqa: BLE001
        print(f"[GATE] report failed: {exc}", file=sys.stderr)
        return _mark_degraded(_empty_card(chapter))


def decision(project: Path, chapter: int) -> dict[str, Any] | None:
    try:
        out = policy().decision(project, chapter)
        return out if isinstance(out, dict) else None
    except Exception as exc:  # noqa: BLE001
        print(f"[GATE] decision read failed: {exc}", file=sys.stderr)
        return None


def decide(project: Path, chapter: int, action: str, *,
           note_text: str = "", by: str = "author",
           **extra: Any) -> dict[str, Any] | None:
    try:
        return policy().decide(project, chapter, action,
                               note_text=note_text, by=by, **extra)
    except Exception as exc:  # noqa: BLE001
        print(f"[GATE] decide failed: {exc}", file=sys.stderr)
        return None


def pending(project: Path, *, last_committed: int | None = None) -> dict[str, Any]:
    try:
        out = policy().pending(project, last_committed=last_committed)
        if isinstance(out, dict) and isinstance(out.get("chapters"), list):
            return out
    except Exception as exc:  # noqa: BLE001
        print(f"[GATE] pending failed: {exc}", file=sys.stderr)
    return {"available": False, "mode": "unknown", "chapters": [],
            "pending_count": 0, "note": "闸事实读取失败；本页不据此判定通过与否"}


def adjudication_status(card: dict[str, Any] | None,
                        row: dict[str, Any] | None) -> str:
    try:
        return str(policy().adjudication_status(card, row))
    except Exception:  # noqa: BLE001
        return "pending"


def would_block(card: dict[str, Any] | None) -> list[str]:
    try:
        out = policy().would_block(card)
        return [str(x) for x in out] if isinstance(out, list) else []
    except Exception:  # noqa: BLE001
        return []


def gate_mode() -> str:
    try:
        return str(policy().gate_mode())
    except Exception:  # noqa: BLE001
        return "unknown"
