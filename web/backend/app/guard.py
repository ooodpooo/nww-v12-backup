"""千章守护中心 · 聚合路由（P0 影子控制台 + 受控修复动作）

职责边界（与 V14 一致：管理面默认只读，不写权威状态、不建第二套事实）：
  - GET  /api/guard/registry  守护维度登记册（真实状态快照 + 运行时 env 覆盖）
  - GET  /api/guard/overview  单书健康总览（tracking 存在性、章节数、相关 env 开关、维度计数）
  - GET  /api/guard/runs      运行记录（只读扫描 runtime/agent_tasks/*.json，按书过滤、按时间倒序）
  - GET  /api/guard/fuse-state 各章**当前**重写锁状态（生产 --check 只读结果，2026-11-07 新增）
  - POST /api/guard/fix/fuse-baseline   界面内「解除重写锁」（2026-11-07 新增）
  - POST /api/guard/fix/fuse-sync-hash  界面内「同步细纲前提」（2026-11-07 新增）
  - POST /api/guard/fix/global-state    界面内「一键补建全局状态基线」（2026-11-07 新增）
  - GET  /api/guard/gate-decisions      V13 道闸事实卡 / 待裁决章列表（2026-11-13 新增）
  - POST /api/guard/gate/decide         作者裁决：接受 / strict 口径复核 / 回滚快照（同上）

设计铁律：
  - 只增不改；本文件不 import 任何**生成链**模块（pipeline / packet_builder / writer），
    避免与生成链产生耦合；所有事实来自既有落盘文件。道闸事实/裁决走
    ``gate_facts`` 适配层只读加载策略模块（不复制判据与路径规则）。
  - 任何读取失败都降级为空/缺省，绝不抛 500 阻断前端（fail-soft）。
  - 模块总开关 NWW_GUARD_ENABLED=0 时 main.py 根本不挂载本路由（整体下线）。
  - ★ 多用户隔离（2026-11-07 用户裁决）：修复动作的路径**一律由服务端按登录 user_id
    解析**（projects_root(user_id)/book_id），前端只传 book_id + chapter；不接收、
    不回传任何文件系统路径、用户目录名或部署结构。绝不把终端命令显示到界面上。
  - ★ 2026-11-13 新增的 ``/guard/gate/decide`` 也属受控写例外：
      accept/recheck 只写 ``追踪/闸报告/裁决.json``（作者结论，不写事实卡、不动正文/追踪权威状态）；
      rollback 调生产快照回滚（持生产锁 + 归档冲突预检），须显式 ``confirm=true`` 二次确认。
      四者均由 ``NWW_GATE_FIX_ENABLED=0`` 整体回滚；rollback 另有
      ``NWW_GATE_ROLLBACK=0`` 单独回滚。
  - ★ 原有三个 POST 是历史写例外：
      ① fuse-baseline 只写 追踪/rewrite_fuse.json（计数基线），不删改执行回执、不动正文；
      ② fuse-sync-hash 只写 追踪/rewrite_fuse.json（细纲前提基线），同上；
      ③ global-state 写 追踪/_tracking-state.json 的 global 段 + 重建生产派生视图
         （写前唯一化备份、全程生产锁、写后生产校验、失败自动回滚），
         只投影已提交的 context.position，绝不臆造剧情、不调模型、不动正文/大纲/回执。
    三者均由 NWW_GUARD_FIX_ENABLED=0 整体回滚；global-state 另有
    NWW_GUARD_GLOBAL_REPAIR=0 单独回滚。
  - ★ 状态判定与动作结果必须同源：解锁不删历史回执（设计铁律），所以「是否还锁着」
    一律以 GET /guard/fuse-state 的生产 --check 结果为准，禁止再用运行记录反推当前锁态。
  - ★ 「没有检查项」≠「检查通过」：GET /guard/story-risk 显式返回 assessable，
    0 章 / 缺基础资产时 assessable=false 并附缺失清单（复用生产 check_foundation_assets），
    前端禁止把 checks=[] 渲染成绿色「未发现崩塌信号」。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from .auth import get_current_user

router = APIRouter()

# 登记册与运行记录均来自既有落盘，不依赖生产链
_REGISTRY_PATH = Path(__file__).resolve().parent / "guard_registry.json"
_AGENT_TASKS_DIR = Path(__file__).resolve().parents[3] / "runtime" / "agent_tasks"

# 确保 NRL_MODE 在运行时可用（与 start.sh 默认值一致；--reload 重载时即生效）
os.environ.setdefault("NWW_NRL_MODE", "shadow")

# 2026-11-05 治本：登记册的 mode 过去是手写声明，出现过「NRL shadow 不注入 packet
# 却声明 active / 已注入」的假绿灯。改为加载时用运行时事实校验：
#   ① 证据里的源码符号是否真的存在；② 依赖的 env 开关实际值是否达到启用条件。
# 校验不通过即降级 mode（前端颜色随之变化），并附 runtime_reason 说明原因。
# 只增不改；NWW_GUARD_RUNTIME_PROBE=0 回到纯静态声明（回滚）。
_RUNTIME_PROBE_SWITCH = "NWW_GUARD_RUNTIME_PROBE"
_PROBE_REPO_ROOT = Path(__file__).resolve().parents[3]


def _book_dir(book_id: str | None, user_id: str | None) -> Path | None:
    """书稿根与 dataplane.projects_root 同源（NWW_PROJECTS_ROOT 或 <repo>/projects/<user_id>）。
    懒导入避免导入顺序耦合；拒绝路径分隔符；失败降级 None（不阻断只读面板）。"""
    if not book_id or not user_id:
        return None
    if any(c in book_id for c in ("/", "\\")) or any(c in user_id for c in ("/", "\\")):
        return None
    try:
        from .dataplane import projects_root
        return projects_root(user_id) / book_id
    except Exception:
        try:
            base = Path(os.environ.get("NWW_PROJECTS_ROOT", str(Path(__file__).resolve().parents[3] / "projects")))
            return base / user_id / book_id
        except Exception:
            return None

# 与本模块相关的运行时开关（只读展示，不修改）
_ENV_KEYS = (
    "NWW_GUARD_ENABLED", "NWW_MVP_ENABLED", "NWW_AGENTS_ENABLED",
    "NWW_NRL_MODE", "NWW_SUPERVISOR_ENABLED", "NWW_DATA_PLANE",
)


@lru_cache(maxsize=32)
def _probe_source(rel: str) -> str | None:
    """读源码文本（带缓存）；缺失返回 None，探针绝不抛异常。"""
    try:
        p = (_PROBE_REPO_ROOT / rel)
        return p.read_text(encoding="utf-8") if p.is_file() else None
    except Exception:
        return None


def _probe_dimension(dim: dict) -> dict:
    """单维度运行时校验，返回可审计的安全子集（只读 / fail-soft）。"""
    probe = dim.get("probe")
    if not isinstance(probe, dict):
        return {"runtime_verified": False,
                "runtime_reason": "未配置运行时探针：该维度的能力状态仅为人工声明"}

    for item in probe.get("symbols", []):
        rel = str(item.get("file") or "")
        text = item.get("text")
        src = _probe_source(rel) if rel else None
        if src is None:
            return {"runtime_verified": False, "runtime_mode": "gap",
                    "runtime_reason": f"证据源码缺失：{rel}"}
        if text and str(text) not in src:
            return {"runtime_verified": False, "runtime_mode": "gap",
                    "runtime_reason": f"证据符号不存在：{rel} :: {text}"}

    for cond in probe.get("env", []):
        key = str(cond.get("key") or "")
        if not key:
            continue
        actual = os.environ.get(key, cond.get("default", "(unset)"))
        enabled = [str(v).strip().lower() for v in cond.get("enabled", [])]
        if str(actual).strip().lower() not in enabled:
            return {"runtime_verified": True,
                    "runtime_mode": str(cond.get("degrade") or "shadow"),
                    "runtime_reason": (f"{key}={actual}，未达启用条件 {enabled}；"
                                       f"能力声明已降级为 {cond.get('degrade') or 'shadow'}")}
    return {"runtime_verified": True,
            "runtime_reason": "运行时探针通过（证据符号存在、依赖开关已启用）"}


def _apply_runtime_probes(dims: list) -> list:
    if os.environ.get(_RUNTIME_PROBE_SWITCH, "1") == "0":
        return dims
    out = []
    for d in dims:
        nd = dict(d)
        res = _probe_dimension(nd)
        nd.update(res)
        if res.get("runtime_mode"):
            nd["declared_mode"] = nd.get("mode")
            nd["mode"] = res["runtime_mode"]  # 前端颜色/计数直接跟真实状态
        out.append(nd)
    return out


def _load_registry() -> dict:
    try:
        reg = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"dimensions": [], "layers": {}, "note": "登记册读取失败"}
    # 统一在加载处校验 → /guard/registry 与 /guard/risk 两处都不会再显示假绿灯
    reg["dimensions"] = _apply_runtime_probes(reg.get("dimensions") or [])
    reg["probe_enabled"] = os.environ.get(_RUNTIME_PROBE_SWITCH, "1") != "0"
    return reg


def _env_snapshot() -> dict:
    out = {}
    for k in _ENV_KEYS:
        out[k] = os.environ.get(k, "") or "(unset)"
    return out


def _scan_runs(book_id: str | None, limit: int, user_id: str | None = None) -> list[dict]:
    """只读扫描 agent_tasks 落盘，返回安全子集（不触碰线程句柄/大对象）。

    user_id：多用户隔离过滤（2026-11-07 根治）。agent_tasks 是全局共享目录，
    任务记录必须归属当前登录用户；旧任务无 user_id 一律不返回（安全默认，杜绝同名书/伪造
    book_id 跨用户拉取运行记录）。
    """
    if not _AGENT_TASKS_DIR.is_dir():
        return []
    rows: list[dict] = []
    try:
        files = sorted(_AGENT_TASKS_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    except Exception:
        return []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        tid = str(data.pop("task_id", "") or f.stem)
        # 按书过滤（book_id 可能为空 → 不过滤）
        if book_id and str(data.get("book_id", "")) != str(book_id):
            continue
        # 多用户隔离：带 user_id 且归属他人的任务一律排除；无 user_id 的旧任务按「书归属当前用户」
        # 处理（guard 端点已用 _book_dir 把 book_id 限制在当前用户 workspace 内，等同书籍所有权边界），
        # 既不丢历史运行记录，又堵住跨用户泄漏。
        if user_id:
            rec_user = data.get("user_id")
            if rec_user and str(rec_user) != str(user_id):
                continue
        stages = list(data.get("live_stages") or [])
        last = stages[-1] if stages else None
        rows.append({
            "task_id": tid,
            "mode": data.get("mode"),
            "book_id": data.get("book_id"),
            "chapter_id": data.get("chapter_id"),
            "status": data.get("status"),
            "stage_count": len(stages),
            "last_stage": (last or {}).get("name") if isinstance(last, dict) else None,
            "error": (str(data.get("error") or "") or None),
            "mtime": int(f.stat().st_mtime),
        })
        if limit and len(rows) >= limit:
            break
    return rows


@router.get("/guard/registry")
def api_guard_registry(_user=Depends(get_current_user)) -> dict:
    """守护维度登记册：真实状态快照 + 运行时 env 覆盖。只读。"""
    reg = _load_registry()
    dims = reg.get("dimensions", [])
    counts: dict[str, dict[str, int]] = {}
    for d in dims:
        layer = d.get("layer", "?")
        mode = d.get("mode", "unknown")
        counts.setdefault(layer, {}).setdefault(mode, 0)
        counts[layer][mode] += 1
    return {
        "module": reg.get("module"),
        "version": reg.get("version"),
        "updated": reg.get("updated"),
        "layers": reg.get("layers", {}),
        "dimensions": dims,
        "counts": counts,
        "env": _env_snapshot(),
        "probe_enabled": reg.get("probe_enabled", False),
        "note": reg.get("note"),
    }


@router.get("/guard/overview")
def api_guard_overview(book_id: str = Query(...), user=Depends(get_current_user)) -> dict:
    """单书健康总览：tracking 存在性、章节数、env 开关、维度计数。只读。"""
    book = _book_dir(book_id, user.get("user_id"))
    tracking_present = False
    chapter_count = 0
    if book and book.is_dir():
        # tracking-state 文件名可能带 .json / 无扩展名，放宽匹配
        tfiles = list(book.glob("_tracking-state*")) + list(book.glob("**/_tracking-state*"))
        tracking_present = any(f.is_file() for f in tfiles)
        ch = book / "正文"
        if ch.is_dir():
            chapter_count = len([p for p in ch.glob("*.md") if p.is_file()])
    reg = _load_registry()
    dims = reg.get("dimensions", [])
    mode_counts: dict[str, int] = {}
    for d in dims:
        mode_counts[d.get("mode", "unknown")] = mode_counts.get(d.get("mode", "unknown"), 0) + 1
    return {
        "book_id": book_id,
        "tracking_present": tracking_present,
        "chapter_count": chapter_count,
        "dimension_total": len(dims),
        "dimension_mode_counts": mode_counts,
        "env": _env_snapshot(),
        "guard_module_enabled": os.environ.get("NWW_GUARD_ENABLED", "1") != "0",
    }


@router.get("/guard/runs")
def api_guard_runs(
    book_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    _user=Depends(get_current_user),
) -> dict:
    """运行记录：只读扫描 agent_tasks。V14「运行记录」等价实现（持久任务 vs 实时实况分离）。"""
    rows = _scan_runs(book_id, int(limit), _user.get("user_id"))
    return {
        "book_id": book_id,
        "limit": int(limit),
        "count": len(rows),
        "runs": rows,
    }


# ─────────────────────  风险研判（当前状态判断 + 修正建议）  ─────────────────────
# 仅基于真实可得信号（维度 mode / 书追踪状态 / 章节数 / env 开关 / 运行记录健康）做
# 配置级研判，明确标注为「配置与状态研判」而非语义级深度分析；所有结论可追溯到具体信号。
_RISK_LABEL = {"high": "高崩塌风险", "medium": "存在风险", "low": "低风险", "ok": "健康"}
_RISK_ORDER = {"ok": 0, "low": 1, "medium": 2, "high": 3}

_DIM_RISK = {
    "gap": ("high", "该维度尚无实现或未验收，对应崩塌面完全无防护"),
    "disabled": ("high", "开关默认关闭，等于未启用该防护"),
    "shadow": ("high", "已埋点但未启用，不实际拦截"),
    "partial": ("low", "已接线有防护在跑，覆盖可逐步加固"),
    "advisory": ("low", "已有检查在跑，可逐步前移为硬闸"),
    "active": ("ok", "提交前硬闸已生效，千章不崩防护到位"),
}
_DIM_SUGGEST = {
    "gap": "先落地该维度最小硬闸（见备注 P1 计划），再纳入滚动审计",
    "disabled": "将对应 NWW_* 开关置 1 并重启后端",
    "shadow": "把对应维度由 shadow 切到 advisory→enforce（保留 env 回滚）",
    "partial": "补全该书对应资产（如 bible.resource_ledger / 卷纲事件弧 / 角色卡），提升填充率",
    "advisory": "将其前移为提交前硬闸（shadow→enforce 三阶段）",
    "active": "保持现状，纳入卷末/整书滚动审计",
}

_STALE_RUNNING_S = 1800  # running 超 30 分钟无更新 → 疑似僵尸

# 维度 → 可执行的修复动作（2026-11-05）：
#   工程能力是「常规指标」：永远展示，风险项必须能点。资产类维度给「打开资产」，
#   没有可落盘依赖的维度给「重新研判」（修复动作完成后再刷新判定）。
_DIM_FIX: dict[str, dict] = {
    "L0-1": {"kind": "file", "label": "打开卷纲", "glob": "大纲/卷纲*.md"},
    "L0-2": {"kind": "file", "label": "打开细纲", "glob": "大纲/细纲*.md"},
    "L1-2": {"kind": "file", "label": "打开角色卡", "glob": "设定/角色/*.md"},
    "L1-3": {"kind": "file", "label": "打开事件注册表", "glob": "追踪/事件注册表.md"},
    "L1-4": {"kind": "recheck", "label": "重新研判（数值/资源/道具）"},
    "L1-5": {"kind": "file", "label": "打开伏笔台账", "glob": "追踪/伏笔.md"},
    "L1-6": {"kind": "file", "label": "打开上下文", "glob": "追踪/上下文.md"},
    "L2-1": {"kind": "file", "label": "打开细纲", "glob": "大纲/细纲*.md"},
    "L2-2": {"kind": "file", "label": "打开文风设定", "glob": "设定/文风.md"},
    "L3-1": {"kind": "file", "label": "打开追踪状态", "glob": "追踪/_tracking-state.json"},
    "L3-2": {"kind": "recheck", "label": "重新研判"},
}


def _dim_action(dim_id: str | None, book: Path | None) -> dict | None:
    """维度修复动作：能定位到资产文件就打开它，否则退化为重新研判；active 维度无动作。"""
    spec = _DIM_FIX.get(dim_id or "")
    if not spec:
        return None
    if spec["kind"] == "file":
        if book and book.is_dir():
            try:
                for f in sorted(book.glob(spec["glob"])):
                    if f.is_file():
                        return {"code": "open_file", "label": spec["label"], "risk": "none",
                                "path": f.relative_to(book).as_posix()}
            except Exception:  # noqa: BLE001 — 资产定位失败不阻断研判
                pass
    return {"code": "recheck", "label": "重新研判", "risk": "none"}


def _env_metrics(env: dict) -> list[dict]:
    """工程能力中的运行期开关：无论开/关都作为常规指标返回（关=风险，开=健康）。"""
    prod_on = env.get("NWW_MVP_ENABLED") == "1" or env.get("NWW_AGENTS_ENABLED") == "1"
    nrl = (env.get("NWW_NRL_MODE") or "off").strip().lower()
    # NRL 只有 enforce 才真正写入 packet 参与逐章硬决策；shadow 仅生成校验计划但不注入，
    # off 完全关闭。此前把 shadow/off/空 一律判为「已启用、参与逐章决策」是假绿灯
    # （nww_rhythm_planner.attach：shadow 分支 receipt.injected=False，绝不注入 packet）。
    nrl_enforced = nrl == "enforce"
    nrl_shadow = nrl == "shadow"
    sup_on = env.get("NWW_SUPERVISOR_ENABLED") == "1"
    return [
        {
            "id": "ENV-PROD", "kind": "env", "layer": "ENV", "name": "生产链（MVP/Agents）",
            "level": "ok" if prod_on else "high",
            "detail": "MVP/Agents 已启用，流水线可产出守护数据" if prod_on
            else "MVP/Agents 开关均关闭，流水线不运行，无法产生任何守护数据",
            "suggestion": "保持现状" if prod_on
            else "启动后端时设 NWW_MVP_ENABLED=1（及所需 NWW_AGENTS_ENABLED=1）",
            "action": None if prod_on else {
                "code": "copy_command", "label": "复制启动命令",
                "command": "NWW_MVP_ENABLED=1 NWW_AGENTS_ENABLED=1",
                "note": "复制不改变运行中的进程；写入启动环境并重启后才生效",
            },
        },
        {
            "id": "ENV-NRL", "kind": "env", "layer": "ENV", "name": "整体规划硬约束（NRL）",
            "level": "ok" if nrl_enforced else ("low" if nrl_shadow else "medium"),
            "detail": ("NRL 已 enforce：节奏计划通过校验后注入 Writing Packet，作为逐章硬决策依据"
                       if nrl_enforced
                       else ("NRL 为 shadow：仅生成并校验节奏计划、不注入正文，规划不进入逐章硬决策"
                             if nrl_shadow
                             else "NRL 默认 off，整体发展规划未成为逐章硬决策依据")),
            "suggestion": "保持现状" if nrl_enforced else "若需规划驱动章节职能契约，设 NWW_NRL_MODE=enforce",
            "action": None if nrl_enforced else {
                "code": "copy_command", "label": "复制启动命令",
                "command": "NWW_NRL_MODE=enforce",
                "note": "复制不改变运行中的进程；写入启动环境并重启后才生效（enforce 才注入 packet）",
            },
        },
        {
            "id": "ENV-SUP", "kind": "env", "layer": "ENV", "name": "自动监督派单",
            "level": "ok" if sup_on else "low",
            "detail": "监督器已启用，目标驱动派单/收件箱督促生效" if sup_on
            else "监督器未启用，目标驱动派单/收件箱督促缺失",
            "suggestion": "保持现状" if sup_on else "设 NWW_SUPERVISOR_ENABLED=1 启用监督器",
            "action": None if sup_on else {
                "code": "copy_command", "label": "复制启动命令",
                "command": "NWW_SUPERVISOR_ENABLED=1",
                "note": "复制不改变运行中的进程；写入启动环境并重启后才生效",
            },
        },
    ]


def _compute_risk(book_id: str | None, user_id: str | None = None) -> dict:
    reg = _load_registry()
    dims = reg.get("dimensions", [])
    env = _env_snapshot()

    book = _book_dir(book_id, user_id)
    tracking_present = False
    chapter_count = 0
    if book and book.is_dir():
        tfiles = list(book.glob("_tracking-state*")) + list(book.glob("**/_tracking-state*"))
        tracking_present = any(f.is_file() for f in tfiles)
        ch = book / "正文"
        if ch.is_dir():
            chapter_count = len([p for p in ch.glob("*.md") if p.is_file()])

    runs = _scan_runs(book_id, 200, user_id)
    now = time.time()
    error_runs = [r for r in runs if r.get("status") == "error"]
    stale_running = [r for r in runs if r.get("status") == "running" and (now - r.get("mtime", 0)) > _STALE_RUNNING_S]

    book_risks: list[tuple[str, str, str, str, dict | None]] = []
    if book_id:
        if not tracking_present:
            # 2026-11-07：修正两处会误导用户的旧口径——
            # ① bootstrap 只补开书资料，**不创建** _tracking-state.json，旧 label「一键重建追踪状态」是假按钮；
            # ② api_bootstrap_book 开头就读 bible.json，缺 bible 直接 404，故必须按资产体检分流。
            fnd = _foundation_snapshot(book) if book else None
            can_bootstrap = bool(fnd and fnd.get("bible_present") and fnd.get("bootstrap_available"))
            act = ({"code": "bootstrap_book", "label": "一键补全开书资料", "risk": "medium", "confirm": True}
                   if can_bootstrap else None)
            sug = ("先补齐世界观/卷纲/主角卡；追踪状态 _tracking-state.json 只能由建书 init 或首次 commit 生成，补全开书资料不会创建它"
                   if can_bootstrap else
                   "缺 bible.json（基础设定档案）时补全开书资料无法执行，系统也不会替作者发明设定；需先由作者补齐基础设定，再走建书 init 生成追踪状态")
            book_risks.append(("high", "本书无追踪状态",
                               f"未找到 _tracking-state，跨章一致性/伏笔/数值锚全部盲区（{chapter_count} 章）", sug, act))
        else:
            book_risks.append(("low", "追踪状态存在", f"已建立追踪状态，{chapter_count} 章", "保持，纳入滚动审计", None))
        if error_runs:
            latest_err = error_runs[0]
            book_risks.append(("medium", f"{len(error_runs)} 次运行失败", "近期有任务以 error 结束，可能留半稿或未提交状态", "查看失败原因并重跑", {"code": "open_run", "label": "查看最新失败", "risk": "none", "task_id": latest_err.get("task_id"), "mode": latest_err.get("mode") or "write"}))
        if stale_running:
            book_risks.append(("medium", f"{len(stale_running)} 个疑似僵尸运行", "running 状态超过 30 分钟无更新，可能已中断", "取消该任务并重跑", {"code": "cancel_task", "label": "取消僵尸任务", "risk": "low", "confirm": True, "task_ids": [r.get("task_id") for r in stale_running]}))

    # 工程能力 = 常规指标：维度 + 运行期开关全部返回（含 ok），风险项再给修复动作。
    metrics: list[dict] = []
    dim_risks = []
    for d in dims:
        lvl, reason = _DIM_RISK.get(d.get("mode"), ("medium", "未知模式"))
        action = _dim_action(d.get("id"), book)
        metrics.append({
            "id": d.get("id"), "kind": "dim", "layer": d.get("layer"),
            "name": d.get("name"), "level": lvl, "mode": d.get("mode"),
            "detail": reason, "suggestion": _DIM_SUGGEST.get(d.get("mode"), "人工评估"),
            "action": action,
        })
        dim_risks.append({
            "id": d.get("id"), "name": d.get("name"), "layer": d.get("layer"),
            "level": lvl, "reason": reason,
            "suggestion": _DIM_SUGGEST.get(d.get("mode"), "人工评估"),
            "mode": d.get("mode"), "action": action,
        })
    metrics.extend(_env_metrics(env))

    all_levels = [m["level"] for m in metrics] + [x[0] for x in book_risks]
    overall_level = "ok"
    for l in ("high", "medium", "low"):
        if l in all_levels:
            overall_level = l
            break
    counts = {
        "high": all_levels.count("high"),
        "medium": all_levels.count("medium"),
        "low": all_levels.count("low"),
        "ok": all_levels.count("ok"),
    }
    if not book_id:
        summary = (f"系统层面 {len(metrics)} 项工程能力：高危 {counts['high']} · 中风险 {counts['medium']}"
                   f" · 低风险 {counts['low']} · 健康 {counts['ok']}。选择书后叠加本书追踪/运行研判。")
    elif not tracking_present:
        summary = (f"高优先级：本书（{chapter_count} 章）无追踪状态，跨章一致性全盲；"
                   "须先补齐基础设定并完成建书 init，补全开书资料（bootstrap）不创建追踪状态。")
    elif counts["high"]:
        summary = f"存在 {counts['high']} 项高危信号：千章不崩的若干防护面当前无有效拦截，须优先处置。"
    elif counts["medium"]:
        summary = f"存在 {counts['medium']} 项中风险：防护已接线但不全或为提示级，建议前移为硬闸并补全资产。"
    elif counts["low"]:
        summary = f"无高危/中风险；{counts['low']} 项低风险（已生效但仍可加固），保持滚动审计。"
    else:
        summary = f"全部 {len(metrics)} 项工程能力健康，保持滚动审计即可。"

    return {
        "book_id": book_id,
        "overall": {"level": overall_level, "label": _RISK_LABEL[overall_level], "summary": summary},
        "counts": counts,
        # system = 仅风险项（兼容旧消费者）；system_metrics = 全量常规指标（含健康）
        "system": [{"level": m["level"], "title": m["name"], "detail": m["detail"], "suggestion": m["suggestion"], "action": m["action"]}
                   for m in metrics if m["level"] != "ok"],
        "system_metrics": metrics,
        "book": [{"level": l, "title": t, "detail": d, "suggestion": s, "action": a} for (l, t, d, s, a) in book_risks],
        "dimensions": dim_risks,
    }


@router.get("/guard/risk")
def api_guard_risk(book_id: str | None = Query(None), user=Depends(get_current_user)) -> dict:
    """工程层（能力/配置）研判：维度/系统/本书三层面的崩塌风险判定 + 修正建议。只读。

    book_id 可省略：工程能力是系统级常规指标，未选书也应完整返回（仅缺本书研判）。
    """
    return _compute_risk(book_id, user.get("user_id"))


# ─────────────────────  故事层（内容）风险研判  ─────────────────────
# 与「工程层」严格区分：工程层判「防护机械是否建成/开启」；故事层读正文/追踪真实资产，
# 判「这本书本身是否在崩」（空/重复章、同质化、跨章复读、节奏碎句、追踪资产盲区）。
# 全部为可核实的内容指标，启发式阈值，明确标注而非语义级判定。

_STATE_CHECKS_FILE = (_PROBE_REPO_ROOT / "skills" / "nww-novel-skill" / "scripts"
                      / "nww_state_checks.py")
_STATE_CHECKS_MOD: object | None = None
_STATE_CHECKS_LOAD_FAILED = False


def _state_checks() -> object | None:
    """只读加载共享判据模块：空判与跨章复读的唯一实现（C-STATE-EMPTY@1 / C-REPEAT@1）。

    本模块不复制判据算法；加载失败返回 None 并只告警一次（与本模块其余读取同为 fail-soft）。
    """
    global _STATE_CHECKS_MOD, _STATE_CHECKS_LOAD_FAILED
    if _STATE_CHECKS_MOD is None and not _STATE_CHECKS_LOAD_FAILED:
        try:
            spec = importlib.util.spec_from_file_location("nww_state_checks_guard",
                                                          _STATE_CHECKS_FILE)
            if spec is None or spec.loader is None:
                raise RuntimeError(f"spec unavailable: {_STATE_CHECKS_FILE.name}")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _STATE_CHECKS_MOD = mod
        except Exception as exc:  # noqa: BLE001 — 判据缺失不阻断只读面板
            print(f"[GUARD] state checks unavailable: {exc}", file=sys.stderr)
            _STATE_CHECKS_LOAD_FAILED = True
    return _STATE_CHECKS_MOD


def _iter_chapters(book: Path) -> list[tuple[str, str]]:
    ch = book / "正文"
    if not ch.is_dir():
        return []
    out = []
    for f in sorted(ch.glob("*.md")):
        try:
            out.append((f.name, f.read_text(encoding="utf-8")))
        except Exception:
            out.append((f.name, ""))
    return out


def _ch_num(name: str) -> int | None:
    m = re.search(r"第\s*0*(\d+)\s*章", name)
    return int(m.group(1)) if m else None


def _rewrite_action(num: int | None, name: str) -> dict:
    """内容问题首选真实修复：重写该章（生产流水线，会备份旧稿）；无法解析章号则退化为打开该章。"""
    if num is not None:
        return {"code": "rewrite_chapter", "label": f"重写第{num}章", "risk": "high", "confirm": True, "chapter_id": num}
    return {"code": "open_chapter", "label": "打开该章", "risk": "none", "path": f"正文/{name}"}


# ---- 研判可判定性（assessable）与资产事实源（2026-11-07）----
# 用户报障：书籍管理里「设定/大纲/正文/追踪都不全」的书，千章守护却显示
# 「未发现明显内容级崩塌信号」。根因是前端把 checks=[] 当成「检查通过」，
# 而后端在 0 章时也只返回空 checks、没有任何「能不能研判」的标记。
# 本层只补「事实 + 可判定性」，不改变有正文书的任何既有判据（零误伤）。
# 资产口径复用生产 check_foundation_assets，不自造正则 / 不列第二套清单。
_ASSET_LABELS = {
    "body": "正文/（章节稿）",
    "tracking": "追踪/_tracking-state.json",
    "bible": "bible.json",
    "world": "设定/世界观/世界观.md",
    "volume": "大纲/卷纲_第1卷.md",
    "character_cards": "设定/角色/<主角名>.md",
}


def _foundation_snapshot(book: Path) -> dict | None:
    """复用生产资产体检（dataplane.check_foundation_assets）：只读、只回相对路径。
    懒导入 + fail-soft（与 _book_dir 同一模式）；数据面不可用时降级 None，
    卡片仍会如实显示「无法研判」，只是不列资产明细，绝不假绿。"""
    try:
        from .dataplane import check_foundation_assets
        r = check_foundation_assets(book)
    except Exception:
        return None
    assets = r.get("assets") or {}

    def _st(key: str) -> str:
        v = assets.get(key)
        return str((v or {}).get("status") or "missing")

    return {
        "ok": bool(r.get("ok")),
        "code": r.get("code"),
        "summary": str(r.get("summary") or ""),
        "bible_present": bool(r.get("bible_present")),
        "bootstrap_available": bool(r.get("bootstrap_available")),
        "missing_for_bootstrap": [str(x) for x in (r.get("missing_for_bootstrap") or [])],
        "missing_extras": [str(x) for x in (r.get("missing_extras") or [])],
        "status": {k: _st(k) for k in ("bible", "world", "volume", "character_cards")},
    }


def _missing_assets(book: Path, foundation: dict | None) -> list[dict]:
    """只列落盘事实：哪些必备资产 missing / thin。不判优劣、不臆造、不回绝对路径。"""
    out: list[dict] = []
    ch = book / "正文"
    body_n = len([p for p in ch.glob("*.md") if p.is_file()]) if ch.is_dir() else 0
    if body_n == 0:
        out.append({"key": "body", "label": _ASSET_LABELS["body"], "state": "missing"})
    if not (book / "追踪" / "_tracking-state.json").is_file():
        out.append({"key": "tracking", "label": _ASSET_LABELS["tracking"], "state": "missing"})
    if foundation:
        st = foundation.get("status") or {}
        for key in ("bible", "world", "volume", "character_cards"):
            s = str(st.get(key) or "missing")
            if s != "ok":
                out.append({"key": key, "label": _ASSET_LABELS[key], "state": s})
    elif not (book / "bible.json").is_file():
        # 体检不可用时只补最基础的一项事实，其余不编造
        out.append({"key": "bible", "label": _ASSET_LABELS["bible"], "state": "missing"})
    return out


def _compute_story_risk(book: Path | None) -> dict:
    if not book or not book.is_dir():
        return {
            "overall": {"level": "medium", "label": "无法研判", "summary": "未找到书稿目录，无法做故事层研判。"},
            "metrics": {}, "checks": [],
            "assessable": False,
            "assess_reason": "未找到书稿目录：长度/复读/节奏/追踪四项检查没有任何样本可执行。",
            "missing_assets": [], "foundation": None,
        }
    chapters = _iter_chapters(book)
    n = len(chapters)
    foundation = _foundation_snapshot(book)
    if n == 0:
        return {
            "overall": {"level": "medium", "label": "无法研判",
                        "summary": "该书缺少正文与基础资产，当前无法给出「千章不崩」结论。"},
            "metrics": {"chapter_count": 0},
            "checks": [],
            "assessable": False,
            "assess_reason": ("长度、跨章复读、段落节奏、追踪覆盖这四项都要以正文为样本；"
                              "本书 0 章，四项一项都没跑，所以不能判定「未发现崩塌信号」。"),
            "missing_assets": _missing_assets(book, foundation),
            "foundation": foundation,
        }
    char_counts = [len(t.strip()) for _, t in chapters]
    state_mod = _state_checks()
    checks: list[dict] = []

    # 1) 空/过短章节
    short = [name for (name, t) in chapters if len(t.strip()) < 800]
    if short:
        checks.append({"level": "high", "title": f"{len(short)} 章过短(<800字)",
                       "detail": "可能截断或未写完：" + "、".join(short[:5]) + ("…" if len(short) > 5 else ""),
                       "suggestion": "重写/续写这些章，避免残缺稿入库",
                       "action": _rewrite_action(_ch_num(short[0]), short[0])})

    # 2) 完全重复章节（md5 逐字相同）
    seen: dict[str, str] = {}
    dups: list[tuple[str, str]] = []
    for name, t in chapters:
        h = hashlib.md5(t.strip().encode("utf-8")).hexdigest()
        if h in seen:
            dups.append((seen[h], name))
        else:
            seen[h] = name
    if dups:
        checks.append({"level": "high", "title": f"{len(dups)} 组章节完全重复",
                       "detail": "内容逐字相同：" + "；".join(f"{a} = {b}" for a, b in dups[:3]),
                       "suggestion": "删除重复稿并重写，排查流水线是否复读了同一章",
                       "action": _rewrite_action(_ch_num(dups[0][1]), dups[0][1])})

    # 3) 长度高度一致 → 模板化/同质化嫌疑
    cnt = Counter(char_counts)
    same_len = [(l, c) for l, c in cnt.items() if c >= 3 and l > 0]
    if same_len:
        l, c = max(same_len, key=lambda x: x[1])
        sev = "high" if (c >= 5 and c / n >= 0.5) else "medium"
        first_name = next((nm for (nm, t) in chapters if len(t.strip()) == l), chapters[0][0])
        checks.append({"level": sev, "title": f"{c} 章长度完全相同（各 {l} 字）",
                       "detail": "疑似模板化生成/整本同质化，章节结构雷同、缺乏推进" if sev == "high" else "疑似模板化生成/同质化，章节可能结构雷同、缺乏推进",
                       "suggestion": "抽查这几章是否只是换词重演；按细纲重写差异化内容",
                       "action": _rewrite_action(_ch_num(first_name), first_name)})

    # 4) 跨章逐字复读（句子级，>=20 字且出现在 >=2 章）
    # 2026-11-05 治本：原 `n <= 300` 会让第 301 章起「千章守护」最该做的检查
    # 自我失效。改为全量扫描（正文由 _iter_chapters 全量读入）。
    # 切句规则与阈值唯一实现在 nww_state_checks.cross_chapter_repeats（C-REPEAT@1），
    # 与 commit 侧同一组规则，不再各自维护一份。
    if n >= 2 and state_mod is not None:
        try:
            rep = state_mod.cross_chapter_repeats(chapters, min_len=20)
        except Exception as exc:  # noqa: BLE001 — 判据异常不阻断只读面板
            print(f"[GUARD] cross chapter repeats failed: {exc}", file=sys.stderr)
            rep = {}
        if rep:
            ex = list(rep.keys())[:2]
            first_idx = sorted(next(iter(rep.values())))[0]
            first_name = chapters[first_idx][0]
            checks.append({"level": "medium", "title": f"{len(rep)} 句跨章重复",
                           "detail": "同一长句出现在多章：" + " / ".join(x[:30] for x in ex),
                           "suggestion": "定位并改写重复句，避免跨章复读",
                           "action": _rewrite_action(_ch_num(first_name), first_name)})

    # 5) 节奏：段落过碎（电文体）/过长（堆砌）
    all_paras: list[str] = []
    for _n, t in chapters:
        all_paras += [p.strip() for p in re.split(r"\n", t) if p.strip()]
    if all_paras:
        lens = [len(p) for p in all_paras]
        avg_para = sum(lens) / len(lens)
        short_ratio = sum(1 for x in lens if x < 15) / len(lens)
        if short_ratio > 0.5:
            checks.append({"level": "medium", "title": f"段落过碎（{int(short_ratio*100)}% 段落 <15 字）",
                           "detail": f"平均段长 {avg_para:.0f} 字，疑似电文体/碎句堆叠",
                           "suggestion": "合并碎句、补足场景与动作，避免电报体"})
        elif avg_para > 200:
            checks.append({"level": "medium", "title": f"段落过长（均 {avg_para:.0f} 字/段）",
                           "detail": "存在大段堆砌，阅读压抑", "suggestion": "按动作/视角/情绪切分长段"})

    # 6) 追踪资产盲区
    ts = book / "追踪" / "_tracking-state.json"
    gs = book / "追踪" / "全局状态.md"
    rec_dir = book / "追踪" / "逐章记录"
    rec_n = len([p for p in rec_dir.glob("*") if p.is_file()]) if rec_dir.is_dir() else 0
    if not ts.is_file():
        checks.append({"level": "high", "title": "无 _tracking-state",
                       "detail": ("跨章一致性/伏笔/数值锚无权威状态；此文件只由建书 init 或首次 commit 创建，"
                                  "补全开书资料（bootstrap）只写设定/大纲文档、不会生成它"),
                       "suggestion": ("先由作者补齐基础设定档案，再走建书 init 生成第 0 章空状态，"
                                      "之后写章并 commit 累积；本项不派发点了达不到宣称效果的写动作")})
    else:
        # 2026-11-07：同时核对「权威状态 global 段」与「派生视图」，避免只扫视图造成假绿/漏报。
        # global 为空＝存量书在 global_change 回流修复前提交，或该章模型未产出增量；
        # 视图空态而 global 非空＝派生视图过期。两种都由同一个受控修复动作处理。
        global_empty: bool | None = None
        state_canonical = False
        try:
            state_doc = json.loads(ts.read_text(encoding="utf-8"))
            if isinstance(state_doc, dict):
                # 空判唯一实现在 nww_state_checks.is_empty_global（C-STATE-EMPTY@1），
                # 与内容门 nww_content_gates._is_empty_global 同一个函数。
                global_empty = (bool(state_mod.is_empty_global(state_doc.get("global")))
                                if state_mod is not None else None)
                # 补建脚本走生产 load_state/normalize_state，只能处理 V12 规范状态。
                # 旧 schema / stub 书（无 context / state_revision）一律不派发该动作，
                # 否则界面会出现「点了必然失败」的假按钮。
                state_canonical = (
                    isinstance(state_doc.get("context"), dict)
                    and isinstance(state_doc.get("state_revision"), int)
                    and "last_committed_chapter" in state_doc
                )
        except Exception:
            global_empty = None  # 状态读不懂时不臆断
        try:
            gtext = gs.read_text(encoding="utf-8") if gs.is_file() else ""
        except Exception:
            gtext = ""
        view_empty = (not gs.is_file()) or ("尚未写入" in gtext)
        if state_canonical and (global_empty is True or view_empty):
            cause = (
                "权威状态 _tracking-state.json 的 global 段为空（存量 commit 未产出 global_change）"
                if global_empty is True else
                ("派生视图 全局状态.md 仍是空态或缺失（权威状态已有内容）"
                 if global_empty is False else
                 "派生视图 全局状态.md 仍是空态或缺失（global 段判据降级，未参与判定）"))
            checks.append({"level": "high", "title": "全局状态未填充",
                           "detail": f"{cause} → 跨章位置/阶段/核心冲突没有承载体，章>10 还会被内容门 BLOCK",
                           "suggestion": "从已提交的 context.position 补建全局状态基线，并用生产渲染器重建派生视图；语义字段由后续 commit 的 global_change 增量补齐",
                           "action": {"code": "rebuild_global_state", "label": "一键补建全局状态基线",
                                      "risk": "medium", "confirm": True}})
        if n >= 2 and rec_n < n:
            checks.append({"level": "medium", "title": f"逐章记录覆盖不足（{rec_n}/{n}）",
                           "detail": "部分章节缺追踪记录，跨章比对样本不全",
                           "suggestion": "补齐逐章记录或重跑 commit"})

    # 7) 开书资料缺失（2026-11-07）：只在「有 bible 且 bootstrap 确实可执行」时派发。
    #    ★ 遵 2026-11-07 教训：扩大检测口径前先确认修复动作对该类对象可执行——
    #      api_bootstrap_book 第一件事就是读 bible.json，缺 bible 会直接 404；
    #      bootstrap 也**不创建** _tracking-state.json，不能拿它冒充「重建追踪状态」。
    #      故 MISSING_BIBLE 一律不派发动作，只在上面「无法研判」卡里列事实。
    if foundation and foundation.get("code") == "MISSING_FOUNDATION" and foundation.get("bootstrap_available"):
        miss = "、".join(foundation.get("missing_for_bootstrap") or []) or "开书资料"
        checks.append({
            "level": "medium", "title": "开书资料缺失或过薄",
            "detail": f"缺少：{miss}。世界观 / 卷纲 / 主角卡是跨章一致性的事实锚，缺了后续章节容易设定漂移。",
            "suggestion": "按现有 bible.json 生成缺失的开书资料（幂等，已存在的不覆盖）；系统不会替作者发明设定。",
            "action": {"code": "bootstrap_book", "label": "一键补全开书资料", "risk": "medium", "confirm": True},
        })

    levels = [c["level"] for c in checks]
    overall = "ok"
    for l in ("high", "medium", "low"):
        if l in levels:
            overall = l
            break
    label = _RISK_LABEL.get(overall, "健康")
    hi = levels.count("high")
    md = levels.count("medium")
    if not checks:
        summary = f"{n} 章未发现明显内容级崩塌信号（长度/复读/节奏/追踪）。"
    elif hi:
        summary = f"存在 {hi} 项内容高危信号（空/重复章、全局状态缺失等），故事层有崩塌风险，须优先处置。"
    else:
        summary = f"存在 {md} 项内容中风险（同质/复读/节奏/追踪覆盖），建议按建议修正。"
    return {
        "overall": {"level": overall, "label": label, "summary": summary},
        "metrics": {
            "chapter_count": n,
            "avg_chars": round(sum(char_counts) / n, 1),
            "min_chars": min(char_counts),
            "max_chars": max(char_counts),
        },
        "checks": checks,
        "assessable": True,
        "assess_reason": "",
        "missing_assets": [],
        "foundation": foundation,
    }


@router.get("/guard/story-risk")
def api_guard_story_risk(book_id: str = Query(...), user=Depends(get_current_user)) -> dict:
    """故事层（内容）风险研判：读正文/追踪真实资产，判断这本书本身是否在崩。只读。"""
    book = _book_dir(book_id, user.get("user_id"))
    return _compute_story_risk(book)


# ---- 熔断状态与受控修复动作（2026-11-07）----
_GUARD_FIX_SWITCH = "NWW_GUARD_FIX_ENABLED"
_FUSE_SCRIPT = _PROBE_REPO_ROOT / "skills" / "nww-novel-skill" / "scripts" / "nww_rewrite_fuse.py"
_GLOBAL_REPAIR_SWITCH = "NWW_GUARD_GLOBAL_REPAIR"
_GLOBAL_REPAIR_SCRIPT = _PROBE_REPO_ROOT / "skills" / "nww-novel-skill" / "scripts" / "nww_global_state_rebuild.py"


def _fuse_check(book: Path, chapter: int) -> dict:
    """只读读取该章**当前**熔断状态：调生产脚本 --check，绝不写盘。

    --check 的 returncode=2 表示「被锁住」而不是执行失败，必须解析 stdout 判定。
    失败/缺失一律 available=False，前端据此回退到历史记录判定，绝不假绿。
    """
    if not _FUSE_SCRIPT.is_file():
        return {"chapter": chapter, "available": False, "locked": None, "note": "熔断脚本缺失"}
    try:
        proc = subprocess.run(
            [sys.executable, str(_FUSE_SCRIPT), "--book", str(book),
             "--chapter", str(chapter), "--check"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except subprocess.TimeoutExpired:
        return {"chapter": chapter, "available": False, "locked": None, "note": "熔断状态读取超时"}
    try:
        out = json.loads(proc.stdout or "{}")
    except Exception:
        out = {}
    if not out or "ok" not in out:
        return {"chapter": chapter, "available": False, "locked": None,
                "note": "熔断状态无法解析（脚本未返回结果）"}
    reason = str(out.get("reason") or "")
    if not out.get("ok"):
        kind = "outline" if ("outline_hash" in reason or "前提已变更" in reason or "细纲" in reason) else "receipts"
    else:
        kind = "none"
    return {
        "chapter": chapter,
        "available": True,
        "ok": bool(out.get("ok")),
        "locked": not bool(out.get("ok")),
        "action": out.get("action"),
        "lock_kind": kind,
        "reason": reason[:300] or None,
        "receipts": out.get("receipts"),
        "receipts_total": out.get("receipts_total"),
        "baseline": out.get("baseline"),
        "max_rewrites": out.get("max_rewrites"),
    }


@router.get("/guard/fuse-state")
def api_guard_fuse_state(book_id: str = Query(...), user=Depends(get_current_user)) -> dict:
    """只读：这本书各章**当前**是否还被重写熔断锁住。

    2026-11-07 修复：前端此前只看「历史上有没有 fuse 失败回执」，而解锁动作按设计
    不删改回执，导致点完解锁卡片仍显示「重写被熔断锁住」。此处改为读落盘的
    rewrite_fuse.json 基线 + 生产 --check 的真实结果，判定与动作结果同源。

    章号来源：熔断状态文件的键 + 运行记录里被 fuse 拦下/已结束的重写任务；
    前端不需要先知道章号，也不传任何路径。
    """
    book = _book_dir(book_id, user.get("user_id"))
    if not book or not book.is_dir():
        raise HTTPException(status_code=404, detail="未找到该书（或不属于当前账号）")
    chapters: set[int] = set()
    fuse_file = book / "追踪" / "rewrite_fuse.json"
    if fuse_file.is_file():
        try:
            data = json.loads(fuse_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for key in data:
                    if str(key).isdigit():
                        n = int(key)
                        if 1 <= n <= 9999:
                            chapters.add(n)
        except Exception:
            pass
    for row in _scan_runs(book_id, 200, user.get("user_id")):
        if row.get("last_stage") == "fuse" or (row.get("mode") == "rewrite" and row.get("status") == "done"):
            n = row.get("chapter_id")
            if isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= 9999:
                chapters.add(n)
    states = [_fuse_check(book, n) for n in sorted(chapters)[:12]]
    return {
        "ok": True,
        "book_id": book_id,
        "available": bool(states) and all(s.get("available") for s in states),
        "chapters": states,
        "any_locked": any(s.get("locked") for s in states),
        "note": "状态来自该书熔断基线文件与生产 --check 只读结果；历史执行回执按设计保留，不作为当前是否锁住的依据。",
    }


@router.post("/guard/fix/fuse-baseline")
def api_guard_fuse_baseline(payload: dict, user=Depends(get_current_user)) -> dict:
    """界面内「解除重写锁」：把该书该章的历史失败回执一次性登记为基线。

    多用户隔离：路径一律由服务端按登录 user_id 解析（projects_root(user_id)/book_id），
    前端只传 book_id + chapter；不接收、不回传任何文件系统路径 / 用户目录 / 部署结构。
    仅写 追踪/rewrite_fuse.json；不删改执行回执、不动正文、不写权威追踪状态。
    """
    if os.environ.get(_GUARD_FIX_SWITCH, "1") == "0":
        raise HTTPException(status_code=403, detail="守护修复动作已被 NWW_GUARD_FIX_ENABLED=0 关闭")
    book_id = str(payload.get("book_id") or "")
    chapter = payload.get("chapter")
    if not book_id:
        raise HTTPException(status_code=400, detail="book_id 必填")
    if not isinstance(chapter, int) or isinstance(chapter, bool) or chapter < 1:
        raise HTTPException(status_code=400, detail="chapter 必须是正整数")
    # _book_dir 已拒绝路径分隔符；再校验目录真实存在，防止对不存在的书写文件或探测他人目录
    book = _book_dir(book_id, user.get("user_id"))
    if not book or not book.is_dir():
        raise HTTPException(status_code=404, detail="未找到该书（或不属于当前账号）")
    if not _FUSE_SCRIPT.is_file():
        raise HTTPException(status_code=503, detail="熔断脚本缺失，无法登记基线")
    try:
        proc = subprocess.run(
            [sys.executable, str(_FUSE_SCRIPT), "--book", str(book),
             "--chapter", str(chapter), "--init-baseline"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="登记基线超时") from None
    try:
        out = json.loads(proc.stdout or "{}")
    except Exception:
        out = {}
    if proc.returncode != 0 or not out.get("ok"):
        detail = (proc.stderr or out.get("note") or "未知原因")
        raise HTTPException(status_code=500, detail=f"登记基线失败：{str(detail)[:300]}")
    # 只回业务字段：绝不回传 book 绝对路径 / 用户目录名 / 服务端部署结构
    return {
        "ok": True,
        "action": out.get("action"),
        "book_id": book_id,
        "chapter": chapter,
        "receipts_total": out.get("receipts_total"),
        "previous_baseline": out.get("previous_baseline"),
        "effective": out.get("effective"),
        # 动作执行完立刻用生产 --check 复核「当前是否还锁着」，界面据此更新卡片状态
        "after": _fuse_check(book, chapter),
        "note": "已把历史失败回执登记为基线；未删除/修改任何执行回执，未改动正文。之后重写额度重新计。",
    }


@router.post("/guard/fix/fuse-sync-hash")
def api_guard_fuse_sync_hash(payload: dict, user=Depends(get_current_user)) -> dict:
    """界面内「同步细纲前提」：解锁因细纲 outline_hash 变更造成的重写熔断。

    登记计数基线（fuse-baseline）**不能**解决这一类锁——它不改 outline_hash，
    点了状态仍不会变。这里调生产脚本 --sync-hash 把当前细纲登记为前提基线，
    同时按生产语义重置该章计数基线。与 fuse-baseline 同一套多用户隔离与回滚开关。
    """
    if os.environ.get(_GUARD_FIX_SWITCH, "1") == "0":
        raise HTTPException(status_code=403, detail="守护修复动作已被 NWW_GUARD_FIX_ENABLED=0 关闭")
    book_id = str(payload.get("book_id") or "")
    chapter = payload.get("chapter")
    if not book_id:
        raise HTTPException(status_code=400, detail="book_id 必填")
    if not isinstance(chapter, int) or isinstance(chapter, bool) or chapter < 1:
        raise HTTPException(status_code=400, detail="chapter 必须是正整数")
    book = _book_dir(book_id, user.get("user_id"))
    if not book or not book.is_dir():
        raise HTTPException(status_code=404, detail="未找到该书（或不属于当前账号）")
    if not _FUSE_SCRIPT.is_file():
        raise HTTPException(status_code=503, detail="熔断脚本缺失，无法同步细纲前提")
    try:
        proc = subprocess.run(
            [sys.executable, str(_FUSE_SCRIPT), "--book", str(book),
             "--chapter", str(chapter), "--sync-hash"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="同步细纲前提超时") from None
    try:
        out = json.loads(proc.stdout or "{}")
    except Exception:
        out = {}
    if proc.returncode != 0 or not out.get("ok"):
        detail = str(proc.stderr or out.get("note") or "未知原因").replace(str(book), "该书目录")
        raise HTTPException(status_code=500, detail=f"同步细纲前提失败：{detail[:300]}")
    return {
        "ok": True,
        "action": out.get("action"),
        "book_id": book_id,
        "chapter": chapter,
        "invalidated_previous": out.get("invalidated_previous"),
        "after": _fuse_check(book, chapter),
        "note": "已把当前细纲登记为新的前提基线，并按生产语义重置该章计数；未删除/修改任何执行回执，未改动正文。",
    }


@router.post("/guard/fix/global-state")
def api_guard_global_state(payload: dict, user=Depends(get_current_user)) -> dict:
    """界面内「一键补建全局状态基线」：修复 全局状态.md 恒为「尚未写入」。

    事实链：render_global_view(state) 在 state["global"] 为空时输出空态；该段只由
    commit 事务的 global_change 经 deep_merge 写入，存量书的 commit 从未产出增量。
    受控脚本只投影**已提交的** context.position，并用生产渲染器重建派生视图，
    不调用模型、不臆造剧情、不显示或回传任何文件系统路径。

    多用户隔离：路径由服务端按登录 user_id 解析；前端只传 book_id，连章号都由
    服务端从权威状态读取，杜绝跨用户探测与路径拼接。
    """
    if os.environ.get(_GUARD_FIX_SWITCH, "1") == "0":
        raise HTTPException(status_code=403, detail="守护修复动作已被 NWW_GUARD_FIX_ENABLED=0 关闭")
    if os.environ.get(_GLOBAL_REPAIR_SWITCH, "1") == "0":
        raise HTTPException(status_code=403, detail="全局状态补建已被 NWW_GUARD_GLOBAL_REPAIR=0 关闭")
    book_id = str(payload.get("book_id") or "")
    if not book_id:
        raise HTTPException(status_code=400, detail="book_id 必填")
    book = _book_dir(book_id, user.get("user_id"))
    if not book or not book.is_dir():
        raise HTTPException(status_code=404, detail="未找到该书（或不属于当前账号）")
    if not _GLOBAL_REPAIR_SCRIPT.is_file():
        raise HTTPException(status_code=503, detail="补建脚本缺失，无法修复全局状态")
    try:
        proc = subprocess.run(
            [sys.executable, str(_GLOBAL_REPAIR_SCRIPT), "--book", str(book), "--apply"],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="补建全局状态超时") from None
    try:
        out = json.loads(proc.stdout or "{}")
    except Exception:
        out = {}
    if proc.returncode != 0 or not out.get("ok"):
        detail = str(out.get("note") or proc.stderr or "未知原因")
        # 子进程异常文本可能夹带绝对路径：统一抹掉，绝不回传部署结构
        detail = detail.replace(str(book), "该书目录")[:300]
        raise HTTPException(status_code=422, detail=f"补建未完成：{detail}")
    # 白名单式响应：只回业务字段，绝不回传 book 绝对路径 / 用户目录名 / 命令
    return {
        "ok": True,
        "action": out.get("action"),
        "book_id": book_id,
        "chapter": out.get("chapter"),
        "fields": out.get("fields") or [],
        "position": out.get("position") or {},
        "revision_before": out.get("revision_before"),
        "revision_after": out.get("revision_after"),
        "views_written": out.get("views_written", 0),
        "backup_name": out.get("backup_name") or "",
        "verified": bool(out.get("verified")),
        "precheck_ok": out.get("precheck_ok"),
        "check_ok": out.get("check_ok"),
        "note": str(out.get("note") or ""),
    }


# ---------------------------------------------------------------------------
# V13 道闸第二阶段：事实卡可见 + 作者裁决（2026-11-13）
#
# 用户质疑「道闸都放开了，闸还有作用吗」——第一阶段的答案只有「仍记录、仍留痕」，
# 作者看不到、也没法把自己的结论记下来，闸事实上退化成日志。这里补上另一半：
#   GET  待裁决章列表（事实卡 + 裁决状态，事实变了旧裁决自动失效）
#   POST 三个一等动作：accept（作者接受）/ recheck（strict 口径只读复核）/
#        rollback（生产快照回滚，需二次确认）
# 与第一阶段一致：不恢复任何硬拦、不改任何判据、不替作者下质量结论。
# ---------------------------------------------------------------------------
_GATE_FIX_SWITCH = "NWW_GATE_FIX_ENABLED"
_GATE_ROLLBACK_SWITCH = "NWW_GATE_ROLLBACK"
_TRACK_COMMIT_SCRIPT = (_PROBE_REPO_ROOT / "skills" / "nww-novel-skill"
                        / "scripts" / "nww_track_commit.py")


def _gate_book(book_id: str, user: dict) -> Path:
    """与既有修复动作同源：按登录 user_id 解析，绝不接收前端传来的路径。"""
    book = _book_dir(book_id, user.get("user_id"))
    if not book or not book.is_dir():
        raise HTTPException(status_code=404, detail="未找到该书（或不属于当前账号）")
    return book


def _gate_last_committed(book: Path) -> int | None:
    """权威游标只读；读不到返回 None（前端据此不显示「已提交」标记，绝不假绿）。"""
    try:
        from . import books
        state = books.load_json(books.tracking_path(book))
        value = state.get("last_committed_chapter") if isinstance(state, dict) else None
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    except Exception:  # noqa: BLE001
        pass
    return None


def _gate_snapshot(book: Path) -> dict:
    """动作执行完立刻重读落盘事实，界面据此更新（不靠前端自己打勾）。"""
    from . import gate_facts
    return gate_facts.pending(book, last_committed=_gate_last_committed(book))


def _gate_mask(text: str, book: Path) -> str:
    """子进程输出可能夹带绝对路径：统一抹掉，绝不回传部署结构。"""
    return str(text or "").replace(str(book), "该书目录")[:600]


@router.get("/guard/gate-decisions")
def api_guard_gate_decisions(book_id: str = Query(...),
                             user=Depends(get_current_user)) -> dict:
    """只读：这本书有哪些章产生了闸事实、其中哪些还等作者裁决。

    事实卡由 v13 道闸写入（记录不阻断）；裁决台账与事实卡指纹绑定，事实一变
    status 立刻变 stale。「没有事实卡」明确回报 available=true + note，
    绝不把「没查到」渲染成「全部通过」。
    """
    book = _gate_book(book_id, user)
    from . import gate_facts
    snap = gate_facts.pending(book, last_committed=_gate_last_committed(book))
    return {"ok": True, "book_id": book_id, **snap}


@router.post("/guard/gate/decide")
def api_guard_gate_decide(payload: dict, user=Depends(get_current_user)) -> dict:
    """作者裁决：accept / recheck / rollback。

    只传 book_id + chapter + action（+ rollback 的 to_chapter），书路径一律服务端
    按登录账号解析；响应白名单化，不回传绝对路径 / 用户目录 / 命令行。
    """
    if os.environ.get(_GATE_FIX_SWITCH, "1") == "0":
        raise HTTPException(status_code=403, detail="道闸裁决动作已被 NWW_GATE_FIX_ENABLED=0 关闭")
    book_id = str(payload.get("book_id") or "")
    chapter = payload.get("chapter")
    action = str(payload.get("action") or "")
    if not book_id:
        raise HTTPException(status_code=400, detail="book_id 必填")
    if not isinstance(chapter, int) or isinstance(chapter, bool) or chapter < 1:
        raise HTTPException(status_code=400, detail="chapter 必须是正整数")
    if action not in ("accept", "recheck", "rollback"):
        raise HTTPException(status_code=400, detail="action 必须是 accept / recheck / rollback")
    book = _gate_book(book_id, user)
    from . import gate_facts
    note_text = str(payload.get("note") or "")[:300]
    card = gate_facts.report(book, chapter)

    # ① 接受：只记作者结论，不写事实卡、不动正文、不动权威追踪状态。
    if action == "accept":
        row = gate_facts.decide(book, chapter, "accept", note_text=note_text)
        if row is None:
            raise HTTPException(status_code=500, detail="裁决未落盘；动作未生效，请重试")
        return {
            "ok": True, "action": "accept", "book_id": book_id, "chapter": chapter,
            "decision": row, "after": _gate_snapshot(book),
            "note": "已记录作者裁决：本轮闸事实按现状接受。系统未替你判定质量，"
                    "未改动正文/追踪权威状态；若该章再产生新闸事实，本裁决自动失效并重新待裁决。",
        }

    # ② strict 口径只读复核：明确「不重跑生成、不重放提交」，避免伪装成重验。
    #    两件可核实的事：a) 事实卡里哪些闸在 strict 下会 BLOCK（即硬闸口径的阻断项）；
    #    b) 生产 nww_track_commit.py check 对当前追踪/派生视图的只读一致性复核。
    if action == "recheck":
        if not _TRACK_COMMIT_SCRIPT.is_file():
            raise HTTPException(status_code=503, detail="生产追踪校验脚本缺失，无法只读复核")
        try:
            proc = subprocess.run(
                [sys.executable, str(_TRACK_COMMIT_SCRIPT), "check", "--project", str(book)],
                capture_output=True, text=True, timeout=30, check=False,
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=504, detail="只读复核超时") from None
        detail = _gate_mask((proc.stdout or "") + (proc.stderr or ""), book)
        check_ok = proc.returncode == 0
        codes = gate_facts.would_block(card)
        row = gate_facts.decide(book, chapter, "recheck", note_text=note_text,
                                check_ok=check_ok, check_detail=detail,
                                recheck_mode="strict-readonly")
        if row is None:
            raise HTTPException(status_code=500, detail="复核结论未落盘；动作未生效，请重试")
        return {
            "ok": True, "action": "recheck", "book_id": book_id, "chapter": chapter,
            "recheck_mode": "strict-readonly",
            "strict_would_block": codes,
            "check_ok": check_ok,
            "check_detail": detail,
            "decision": row, "after": _gate_snapshot(book),
            "note": ("这是 strict 口径的**只读复核**：不重跑生成、不重放提交、不改任何文件。"
                     "「会 BLOCK 的闸」来自本轮事实卡（每条事实都对应 strict 会抛异常的一道闸）；"
                     "追踪一致性来自生产 check 的只读输出。要真正重放硬闸需一次隔离副本提交，属独立能力。"),
        }

    # ③ 回滚快照：真实生产动作，必须显式二次确认 + 独立开关。
    if os.environ.get(_GATE_ROLLBACK_SWITCH, "1") == "0":
        raise HTTPException(status_code=403, detail="快照回滚已被 NWW_GATE_ROLLBACK=0 关闭")
    if payload.get("confirm") is not True:
        raise HTTPException(status_code=400, detail="回滚快照需显式二次确认（confirm=true）")
    to_chapter = payload.get("to_chapter")
    if (not isinstance(to_chapter, int) or isinstance(to_chapter, bool)
            or to_chapter < 1 or to_chapter >= chapter):
        raise HTTPException(status_code=400, detail="to_chapter 必须是小于当前章号的正整数")
    if not _TRACK_COMMIT_SCRIPT.is_file():
        raise HTTPException(status_code=503, detail="生产快照回滚脚本缺失，无法执行")
    try:
        proc = subprocess.run(
            [sys.executable, str(_TRACK_COMMIT_SCRIPT), "rollback", "--project", str(book),
             "--chapter", str(to_chapter)],
            capture_output=True, text=True, timeout=120, check=False,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="快照回滚超时，请核对生产锁与归档目录") from None
    detail = _gate_mask((proc.stdout or "") + (proc.stderr or ""), book)
    if proc.returncode != 0:
        raise HTTPException(status_code=422, detail=f"快照回滚未完成：{detail}")
    row = gate_facts.decide(book, chapter, "rollback", note_text=note_text,
                            rollback_to=to_chapter)
    if row is None:
        raise HTTPException(status_code=500, detail="回滚已执行但裁决未落盘；请在守护中心复核当前盘面")
    return {
        "ok": True, "action": "rollback", "book_id": book_id, "chapter": chapter,
        "rollback_to": to_chapter,
        "check_detail": detail,
        "decision": row, "after": _gate_snapshot(book),
        "note": ("已用生产快照回滚（持生产写锁、归档冲突预检、>目标章正文移入 _archive/）。"
                 "这是不可逆动作，请核对归档目录后再继续写作。"),
    }
