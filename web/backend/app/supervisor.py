"""流水线监督器（2026-10-25）：目标驱动的常驻监督中枢。

解决「说一句动一下」：作者下一次目标（如「写到第 30 章」「今天 3 章」），监督器在
后端常驻线程里连续派单推进到目标/硬闸为止；进度、失败、待确认闸门全部落收件箱，
浏览器关了也照跑（launchd 守护后端进程）。

设计原则：
- 规则引擎是**确定性纯函数**（decide）：不烧 LLM、可预测、可单测；LLM 只用于
  ChatBox 对话解释监督状态，不参与派单决策。
- fail-closed 不加宽：write 防覆盖闸、章级操作槽、A-3 闭环、修复授权等既有闸门
  全部保留；监督器只在「ready_to_write / plan_needed(规划器开)」两态派 write。
- 硬闸必停：需要作者点头的动作（闭环确认/修复授权/下调目标/重写L2L3）一律进
  收件箱督促，绝不自动确认。
- 熔断兜底：单章自动重试 1 次；连续失败 ≥2 次、每日上限、目标达成 → 停并汇报。

状态落盘：projects/{user}/{book}/追踪/监督/supervisor.json + events.jsonl（收件箱）。
环境开关：NWW_SUPERVISOR_ENABLED=1 挂载路由并起线程；NWW_SUPERVISOR_INTERVAL
（tick 秒，默认 20）；NWW_SUPERVISOR_STALE_S（任务停滞判定，默认 1800）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from . import books
from .auth import get_current_user
from .dataplane import DEFAULT_PROJECTS_ROOT, projects_root
from .fs import book_root

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PHASE_SCRIPT = _REPO_ROOT / "skills" / "nww-novel-skill" / "scripts" / "nww_phase.py"

_SUPERVISOR_DIR = "监督"           # book/追踪/监督/
_STATE_FILE = "supervisor.json"
_EVENTS_FILE = "events.jsonl"
_EVENT_CAP = 200                    # 收件箱常规容量；超过 CAP+60 时裁剪到 CAP
_EVENT_SLACK = 60
_EVENT_READ_LIMIT = 50              # state 接口回给前端的最近事件条数

_INTERVAL_S = float(os.environ.get("NWW_SUPERVISOR_INTERVAL", "20") or "20")
_STALE_S = float(os.environ.get("NWW_SUPERVISOR_STALE_S", "1800") or "1800")
_PHASE_TIMEOUT_S = 90.0

_DAILY_CHAPTER_CAP = 12             # 两种目标共用的每日硬上限（防跑飞烧配额）
_MAX_CONSECUTIVE_FAILURES = 2       # 熔断阈值
_RETRY_PER_CHAPTER = 1              # 单章失败自动重试次数（原参数）
# 基础设施故障（LLM 端点 502/超时）不是内容问题：不烧熔断、不耗章节重试额度，
# 退避等待端点恢复后自动续跑；超过上限才停下交作者处置（2026-10-25）。
_INFRA_BACKOFF_S = 90.0
_INFRA_RETRY_CAP = 10

router = APIRouter()

# ---------------------------------------------------------------------------
# 状态存储（每书一把锁；状态 JSON 与事件 jsonl 的读写都走锁 + 原子写）
# ---------------------------------------------------------------------------

_META_LOCK = threading.Lock()
_BOOK_LOCKS: dict[str, threading.Lock] = {}


def _book_lock(book: Path) -> threading.Lock():
    key = str(book)
    with _META_LOCK:
        lk = _BOOK_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            _BOOK_LOCKS[key] = lk
        return lk


def _sup_dir(book: Path) -> Path:
    return book / "追踪" / _SUPERVISOR_DIR


def _default_state() -> dict:
    return {
        "schema_version": 1,
        "goal": None,
        "policy": {
            "paused": False,
            "daily_chapter_cap": _DAILY_CHAPTER_CAP,
            "max_consecutive_failures": _MAX_CONSECUTIVE_FAILURES,
            "retry_per_chapter": _RETRY_PER_CHAPTER,
        },
        "runtime": {
            "day": datetime.now().strftime("%Y-%m-%d"),
            "consecutive_failures": 0,
            "chapters_done_today": 0,
            "retried": {},            # {chapter: 已重试次数}
            "last_failure": None,     # {chapter, error, ts}
            "stop_reason": None,      # goal_reached / daily_cap / circuit_breaker / book_missing / llm_unavailable
            "active_task_id": None,   # 监督器派出的在跑任务
            "task_sig": None,         # 看门狗：上次观测到的任务签名
            "task_sig_ts": 0.0,
            "last_seen_committed": None,
            "infra_retries": 0,       # 端点不可用的退避重试计数（独立于章节重试/熔断）
            "infra_wait_until": 0.0,  # 退避冷却截止（epoch 秒）
            "last_tick": 0.0,
        },
    }


def load_state(book: Path) -> dict:
    """读监督状态（无文件 → 默认态；损坏 → 默认态兜底，绝不让监督器挂掉）。"""
    p = _sup_dir(book) / _STATE_FILE
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _default_state()
    if not isinstance(data, dict):
        return _default_state()
    base = _default_state()
    base.update({k: data[k] for k in ("goal", "policy", "runtime") if k in data and isinstance(data[k], dict)})
    base["goal"] = data.get("goal") if data.get("goal") is None or isinstance(data.get("goal"), dict) else None
    # policy/runtime 与默认值合并：新增字段对旧状态文件向后兼容
    base["policy"] = {**_default_state()["policy"], **(base.get("policy") or {})}
    base["runtime"] = {**_default_state()["runtime"], **(base.get("runtime") or {})}
    return base


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def save_state(book: Path, state: dict) -> None:
    with _book_lock(book):
        _atomic_write(_sup_dir(book) / _STATE_FILE,
                      json.dumps(state, ensure_ascii=False, indent=2))


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _read_events(book: Path) -> list[dict]:
    p = _sup_dir(book) / _EVENTS_FILE
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            ev = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict):
            out.append(ev)
    return out


def append_event(book: Path, kind: str, level: str, title: str, detail: str = "",
                 chapter: int | None = None, dedupe_key: str | None = None) -> dict | None:
    """追加收件箱事件（level: info/gate/fail/done）。

    dedupe_key 非空时：已存在**未读**的同 key 事件 → 跳过（闸门类事件不刷屏，
    作者读过之后若问题仍在，下个 tick 会再提醒一次）。返回事件 dict；去重跳过返回 None。
    """
    with _book_lock(book):
        events = _read_events(book)
        if dedupe_key:
            for ev in events:
                if not ev.get("read") and ev.get("dedupe_key") == dedupe_key:
                    return None
        ev = {
            "id": f"{time.time():.0f}-{uuid.uuid4().hex[:8]}",
            "ts": _now_str(),
            "kind": kind,
            "level": level,
            "title": title,
            "detail": str(detail or "")[:600],
            "chapter": chapter,
            "dedupe_key": dedupe_key,
            "read": False,
        }
        events.append(ev)
        if len(events) > _EVENT_CAP + _EVENT_SLACK:
            events = events[-_EVENT_CAP:]
        _atomic_write(_sup_dir(book) / _EVENTS_FILE,
                      "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n")
        return ev


def unread_events(book: Path, limit: int = _EVENT_READ_LIMIT) -> list[dict]:
    evs = [e for e in _read_events(book) if not e.get("read")]
    return evs[-limit:]


def mark_event_read(book: Path, event_id: str) -> bool:
    with _book_lock(book):
        events = _read_events(book)
        hit = False
        for ev in events:
            if ev.get("id") == event_id and not ev.get("read"):
                ev["read"] = True
                hit = True
        if hit:
            _atomic_write(_sup_dir(book) / _EVENTS_FILE,
                          "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n")
        return hit


# ---------------------------------------------------------------------------
# 目标（goal）校验：Web API 与 ChatBox GOAL_JSON 共用同一份判据
# ---------------------------------------------------------------------------

def validate_goal(payload: dict) -> dict:
    """校验并归一化目标载荷；非法 raise ValueError（调用方转 400 / fail-closed 丢弃）。

    两种目标：
      {"type": "write_to", "target_chapter": N}  —— 连续写到提交至第 N 章
      {"type": "daily",    "daily_chapters": N}   —— 今日完成 N 章
    可选 note ≤200 字（作者叮嘱，进事件与 chat 上下文）。"""
    if not isinstance(payload, dict):
        raise ValueError("goal 必须是对象")
    gtype = payload.get("type")
    note = str(payload.get("note", "") or "").strip()
    if len(note) > 200:
        raise ValueError("note 超过 200 字上限")
    if gtype == "write_to":
        target = payload.get("target_chapter")
        if not isinstance(target, int) or isinstance(target, bool) or not (1 <= target <= 99999):
            raise ValueError("write_to 目标必须提供 1~99999 的整数 target_chapter")
        return {"type": "write_to", "target_chapter": target, "note": note}
    if gtype == "daily":
        n = payload.get("daily_chapters")
        if not isinstance(n, int) or isinstance(n, bool) or not (1 <= n <= 12):
            raise ValueError("daily 目标必须提供 1~12 的整数 daily_chapters")
        return {"type": "daily", "daily_chapters": n, "note": note}
    raise ValueError("goal.type 只能是 write_to / daily")


def _goal_desc(goal: dict | None) -> str:
    if not goal:
        return "无目标"
    if goal.get("type") == "write_to":
        return f"写到第{goal['target_chapter']}章"
    return f"今日完成 {goal.get('daily_chapters')} 章"


# ---------------------------------------------------------------------------
# 观察层：阶段机（subprocess 只读）+ 在跑任务快照
# ---------------------------------------------------------------------------

# (user_id, book_id) -> {"ts", "phase", "target_chapter", "last_committed", "next_action"}
_OBS_CACHE: dict[tuple[str, str], dict] = {}
_OBS_CACHE_LOCK = threading.Lock()


def _cache_obs(user_id: str, book_id: str, obs: dict) -> None:
    with _OBS_CACHE_LOCK:
        _OBS_CACHE[(user_id, book_id)] = {**obs, "ts": time.time()}


def cached_obs(user_id: str, book_id: str, max_age_s: float = 300.0) -> dict | None:
    with _OBS_CACHE_LOCK:
        o = _OBS_CACHE.get((user_id, book_id))
    if not o or time.time() - float(o.get("ts") or 0) > max_age_s:
        return None
    return o


def resolve_phase(book: Path) -> dict:
    """subprocess 调 nww_phase.py（只读阶段机，story-phase/v1）。

    失败/超时 → {"phase": "error", ...}（规则引擎按 error 闸处理，绝不挂循环）。
    """
    try:
        cp = subprocess.run(
            [sys.executable, str(_PHASE_SCRIPT), "--project", str(book), "--json"],
            capture_output=True, text=True, encoding="utf-8", timeout=_PHASE_TIMEOUT_S, check=False,
        )
        line = next((ln for ln in (cp.stdout or "").splitlines() if ln.strip()), "")
        data = json.loads(line)
        if isinstance(data, dict) and data.get("phase"):
            return data
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, StopIteration, ValueError):
        pass
    return {"phase": "error", "target_chapter": None, "last_committed_chapter": None,
            "blocking": ["阶段解析失败"], "next_action": "检查 nww_phase.py 可用性"}


def _planner_enabled() -> bool:
    """与 pipeline._planner_enabled 同口径：NWW_PLANNER=0 才关，默认开。"""
    return os.environ.get("NWW_PLANNER", "1") != "0"


def _running_tasks(book_id: str) -> list[dict]:
    """该书在跑任务快照（含作者手动派的；含线程死亡判定，僵尸不算在跑）。"""
    try:
        from .nwwmvp import _AGENT_TASKS, _AGENT_TASKS_LOCK, _AGENT_THREADS
    except Exception:  # noqa: BLE001 — nwwmvp 未挂载时（NWW_MVP_ENABLED=0）无任务可观察
        return []
    out: list[dict] = []
    with _AGENT_TASKS_LOCK:
        for tid, t in _AGENT_TASKS.items():
            if t.get("book_id") != book_id or t.get("status") != "running":
                continue
            th = _AGENT_THREADS.get(tid)
            if th is not None and not th.is_alive():
                continue  # 僵尸任务：线程已死但状态未终（_task_restore/看门狗路径兜底）
            out.append({"task_id": tid, "mode": t.get("mode"), "chapter_id": t.get("chapter_id"),
                        "range": t.get("range"), "phase": t.get("phase"),
                        "origin": t.get("origin", "manual")})
    return out


def _task_snapshot(task_id: str) -> dict | None:
    try:
        from .nwwmvp import _AGENT_TASKS, _AGENT_TASKS_LOCK
    except Exception:  # noqa: BLE001
        return None
    with _AGENT_TASKS_LOCK:
        t = _AGENT_TASKS.get(task_id)
        return dict(t) if t else None


def _request_cancel(task_id: str) -> None:
    try:
        from .nwwmvp import _AGENT_TASKS, _AGENT_TASKS_LOCK
        with _AGENT_TASKS_LOCK:
            t = _AGENT_TASKS.get(task_id)
            if t and t.get("status") == "running":
                t["cancel_requested"] = True
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 规则引擎（纯函数，可单测）：decide(obs, state) -> 决策
#
# obs 字段：phase / target_chapter / last_committed / blocking / next_action /
#           running（在跑任务快照或 None）/ planner_enabled
# 决策 act：
#   noop    —— 无事可做（无目标/已暂停/已熔断待作者 resume）
#   wait    —— 有任务在跑（含作者手动任务），等下个 tick
#   dispatch —— 派 write（mode/chapter/auto_plan）
#   gate    —— 需作者处置（kind/title/detail/chapter），不停止目标，事件去重
#   stop    —— 终止推进（reason/level/title），置 runtime.stop_reason 待 resume/换目标
# ---------------------------------------------------------------------------

def decide(obs: dict, state: dict) -> dict:
    goal = state.get("goal")
    policy = state.get("policy") or {}
    runtime = state.get("runtime") or {}
    if not goal:
        return {"act": "noop", "reason": "no_goal"}
    if policy.get("paused"):
        return {"act": "noop", "reason": "paused"}
    if runtime.get("stop_reason"):
        return {"act": "noop", "reason": str(runtime["stop_reason"])}

    running = obs.get("running")
    if running:
        return {"act": "wait", "reason": "task_running", "task": running}

    phase = obs.get("phase") or "error"
    target = obs.get("target_chapter")
    last = obs.get("last_committed") or 0
    done_today = int(runtime.get("chapters_done_today") or 0)

    # --- 目标达成 / 配额 / 熔断（先于派单判定）---
    if goal.get("type") == "write_to" and isinstance(goal.get("target_chapter"), int) \
            and last >= goal["target_chapter"]:
        return {"act": "stop", "reason": "goal_reached", "level": "done",
                "title": f"目标达成：已提交至第{goal['target_chapter']}章"}
    if goal.get("type") == "daily" and done_today >= int(goal.get("daily_chapters") or 0):
        return {"act": "stop", "reason": "goal_reached", "level": "done",
                "title": f"今日目标达成：已完成 {goal.get('daily_chapters')} 章"}
    if done_today >= int(policy.get("daily_chapter_cap", _DAILY_CHAPTER_CAP)):
        return {"act": "stop", "reason": "daily_cap", "level": "gate",
                "title": f"已达每日硬上限（{policy.get('daily_chapter_cap', _DAILY_CHAPTER_CAP)} 章），停止自动派单"}
    if int(runtime.get("consecutive_failures") or 0) >= int(
            policy.get("max_consecutive_failures", _MAX_CONSECUTIVE_FAILURES)):
        return {"act": "stop", "reason": "circuit_breaker", "level": "fail",
                "title": "熔断：连续多章失败，等待作者处置后 resume"}

    # --- 阶段分派 ---
    if phase == "views_dirty":
        return {"act": "gate", "kind": "views_dirty", "chapter": target,
                "title": f"第{target}章前派生视图与追踪状态不一致，禁止续写",
                "detail": "；".join(obs.get("warnings") or []) or obs.get("next_action") or "前派生视图与追踪状态不一致，已禁止续写；可在面板查看一致性校验差异并处置（由服务端执行校验，无需在终端手敲命令），处置后自动续跑"}
    if phase == "inconsistent":
        return {"act": "gate", "kind": "inconsistent", "chapter": target,
                "title": f"第{target}章数据异常（delta 已存在但追踪未同步）",
                "detail": obs.get("next_action") or "核对 _tracking-state.json 后修复"}
    if phase == "draft_in_progress":
        # 有正文未提交：可能来自崩溃残留/手动写作/写链被闸 BLOCK。仅提交端点要求
        # closed=true 闭环确认 → 硬闸，绝不自动确认，督促作者走决策卡。
        return {"act": "gate", "kind": "needs_author", "chapter": target,
                "title": f"第{target}章有正文但未提交（需人工闭环）",
                "detail": "请在面板处置：确认一致性闭环后「仅提交」，或派重写/修订，处理后自动续跑"}
    if phase in ("plan_needed",):
        if obs.get("planner_enabled"):
            return {"act": "dispatch", "mode": "write", "chapter": target, "auto_plan": True}
        return {"act": "gate", "kind": "planner_off", "chapter": target,
                "title": f"第{target}章缺细纲且自动规划未开（NWW_PLANNER=0）",
                "detail": "开启 NWW_PLANNER=1 或先补 大纲/细纲_第{target}章.md"}
    if phase == "ready_to_write":
        # 重试耗尽仍停在待写态 → 该章已烧完自动重试，交作者决策
        lf = runtime.get("last_failure") or {}
        retried = int((runtime.get("retried") or {}).get(target, 0))
        if lf.get("chapter") == target and retried >= int(policy.get("retry_per_chapter", _RETRY_PER_CHAPTER)):
            return {"act": "gate", "kind": "chapter_failed", "chapter": target,
                    "title": f"第{target}章写作失败（已自动重试 {retried} 次），等待作者决策",
                    "detail": str(lf.get("error") or "")[:300] or "见收件箱失败事件与执行回执"}
        return {"act": "dispatch", "mode": "write", "chapter": target}
    if phase == "no_project":
        return {"act": "gate", "kind": "no_project", "chapter": 1,
                "title": "追踪状态缺失（书未 init）",
                "detail": "请到书籍管理界面点「初始化本书」按钮完成初始化后再下目标（由服务端按当前登录用户执行，无需在终端手敲命令）"}
    return {"act": "gate", "kind": "observe_error", "chapter": target,
            "title": "阶段解析失败，暂缓派单",
            "detail": "；".join(obs.get("blocking") or []) or "稍后自动重试观察"}


# ---------------------------------------------------------------------------
# 执行层：tick 一本书（观察 → 任务终态处理 → 看门狗 → decide → 派发）
# ---------------------------------------------------------------------------

def _apply_dispatch(user_id: str, book_id: str, book: Path, state: dict, dec: dict) -> None:
    """执行 dispatch 决策：派 write，登记 active_task_id；HTTPException 转收件箱/停止。"""
    runtime = state["runtime"]
    chapter = dec.get("chapter")
    try:
        from .nwwmvp import spawn_write_task
        ret = spawn_write_task(user_id, book_id, chapter, origin="supervisor")
    except ImportError:
        runtime["stop_reason"] = "book_missing"
        append_event(book, "stopped", "fail", "监督器无法调用写章执行器（nwwmvp 未挂载）",
                     "检查 NWW_MVP_ENABLED / NWW_AGENTS_ENABLED 开关")
        return
    except HTTPException as exc:
        code, detail = exc.status_code, str(exc.detail)
        if code == 409:  # 章槽被占（多半是作者手动操作）→ 等待，不计数
            append_event(book, "wait", "info", f"第{chapter}章有进行中操作，监督器等待",
                         detail, chapter=chapter, dedupe_key=f"busy:{chapter}")
        elif code == 404:
            runtime["stop_reason"] = "book_missing"
            append_event(book, "stopped", "fail", "书籍目录不存在，目标终止", detail)
        elif code == 400 and "已存在" in detail:
            pass  # 观察口径滞后（正文刚落盘）：下个 tick 会观察到 draft_in_progress 并走闸
        else:
            append_event(book, "dispatch_fail", "fail", f"第{chapter}章派单被拒（{code}）",
                         detail, chapter=chapter, dedupe_key=f"dispatch_reject:{chapter}")
        return
    except Exception as exc:  # noqa: BLE001 — 派单异常不挂循环
        append_event(book, "dispatch_fail", "fail", f"第{chapter}章派单异常",
                     str(exc)[:300], chapter=chapter, dedupe_key=f"dispatch_error:{chapter}")
        return
    runtime["active_task_id"] = ret["task_id"]
    runtime["task_sig"] = None
    runtime["task_sig_ts"] = time.time()
    append_event(book, "dispatch", "info",
                 f"监督器派单：{'自动补细纲并写' if dec.get('auto_plan') else '写'}第{chapter}章",
                 f"目标：{_goal_desc(state.get('goal'))}；任务 {ret['task_id']}", chapter=chapter)


def _is_infra_failure(err_text: str, result: dict) -> bool:
    """基础设施故障判定：端点全挂（LLM 调用全部失败）或回执闸门 llm_unavailable。

    与内容闸门（字数/漂移）不同：infra 失败重跑同一动作是正确策略，且端点恢复前
    重试必然失败——应退避等待而非烧熔断。
    """
    if "LLM 调用全部失败" in err_text:
        return True
    return (result.get("failure") or {}).get("gate") == "llm_unavailable"


def _process_terminal_task(book: Path, state: dict, task: dict) -> None:
    """监督器派出的任务到终态：成功 → 汇报；失败 → 自动重试一次，否则计入熔断。"""
    runtime = state["runtime"]
    tid = task.get("task_id")
    chapter = task.get("chapter_id")
    if tid != runtime.get("active_task_id"):
        return  # 非当前活跃任务（看门狗已处理过 / 旧任务残留）——不重复计数
    runtime["active_task_id"] = None
    runtime["task_sig"] = None
    status = task.get("status")
    if status == "done" and task.get("ok"):
        runtime["consecutive_failures"] = 0
        (runtime.setdefault("retried", {}) or {}).pop(chapter, None)
        runtime["last_failure"] = None
        runtime["infra_retries"] = 0
        runtime["infra_wait_until"] = 0.0
        # 今日完成数不在这里累加：统一走 _tick_book 的观察口径（已提交章增长），
        # 避免与观察计数双重累计，同时把作者手动写作也计入每日目标。
        append_event(book, "chapter_done", "done", f"第{chapter}章写作完成",
                     f"目标：{_goal_desc(state.get('goal'))}", chapter=chapter)
        return
    if status == "cancelled":
        append_event(book, "task_cancelled", "info", f"第{chapter}章任务已终止", chapter=chapter)
        return
    # 失败（error / done 但 needs_decision）
    result = task.get("result") or {}
    failure = result.get("failure") or {}
    err = task.get("error") or failure.get("detail") or "未知失败"
    runtime["last_failure"] = {"chapter": chapter, "error": str(err)[:200], "ts": _now_str()}

    # 基础设施故障（端点 502/超时）：退避等待恢复，不烧章节重试额度、不进熔断计数
    if _is_infra_failure(str(err), result):
        n = int(runtime.get("infra_retries") or 0) + 1
        if n <= _INFRA_RETRY_CAP:
            runtime["infra_retries"] = n
            runtime["infra_wait_until"] = time.time() + _INFRA_BACKOFF_S
            append_event(book, "infra_retry", "info",
                         f"模型端点不可用，退避 {int(_INFRA_BACKOFF_S)}s 后自动重试（{n}/{_INFRA_RETRY_CAP}）",
                         f"第{chapter}章 {str(err)[:200]}", chapter=chapter)
            return
        runtime["stop_reason"] = "llm_unavailable"
        append_event(book, "stopped", "gate",
                     "模型端点持续不可用，自动推进暂停",
                     f"已退避重试 {n - 1} 次仍失败；恢复端点（检查桥接/Chrome 会话或更换默认模型）后点「继续」")
        return

    retried = int((runtime.setdefault("retried", {}) or {}).get(chapter, 0))
    retry_cap = int((state.get("policy") or {}).get("retry_per_chapter", _RETRY_PER_CHAPTER))
    if retried < retry_cap:
        runtime["retried"][chapter] = retried + 1
        append_event(book, "auto_retry", "info",
                     f"第{chapter}章失败，自动重试（{retried + 1}/{retry_cap}）",
                     str(err)[:300], chapter=chapter)
        return  # 状态回到待写/待处置，由下个 tick 的 decide 重新派
    runtime["consecutive_failures"] = int(runtime.get("consecutive_failures") or 0) + 1
    opts = [o.get("label") for o in (result.get("decision_options") or []) if isinstance(o, dict)]
    detail = str(err)[:300]
    if opts:
        detail += f"；可选处置：{' / '.join(opts[:4])}"
    if result.get("receipt_path"):
        detail += f"；回执：{result['receipt_path']}"
    append_event(book, "task_failed", "fail",
                 f"第{chapter}章失败且重试已用尽（连续失败 {runtime['consecutive_failures']}）",
                 detail, chapter=chapter)


def _watchdog(book: Path, state: dict, running: list[dict]) -> None:
    """任务停滞看门狗：签名（状态+环节+明细）长时间不变 → 协作式取消并计失败。

    前端轮询看门狗只在页面开着时生效；这里是服务端兜底，浏览器关了也能终止僵死任务。"""
    runtime = state["runtime"]
    tid = runtime.get("active_task_id")
    if not tid:
        return
    task = next((t for t in running if t.get("task_id") == tid), None)
    if task is None:
        # 活跃任务不在「在跑」集合：要么已终态（下步 _process_terminal_task 处理），
        # 要么线程死亡未落终态——按停滞超时口径处理
        if time.time() - float(runtime.get("task_sig_ts") or 0) > _STALE_S:
            _request_cancel(tid)
        return
    sig = json.dumps([task.get("phase"), task.get("mode"), task.get("chapter_id")],
                     ensure_ascii=False, default=str)
    if sig != runtime.get("task_sig"):
        runtime["task_sig"] = sig
        runtime["task_sig_ts"] = time.time()
        return
    if time.time() - float(runtime.get("task_sig_ts") or 0) > _STALE_S:
        chapter = task.get("chapter_id")
        _request_cancel(tid)
        runtime["active_task_id"] = None
        runtime["task_sig"] = None
        runtime["last_failure"] = {"chapter": chapter,
                                   "error": f"任务长时间无进展（>{int(_STALE_S)}s），已终止", "ts": _now_str()}
        retried = int((runtime.setdefault("retried", {}) or {}).get(chapter, 0))
        retry_cap = int((state.get("policy") or {}).get("retry_per_chapter", _RETRY_PER_CHAPTER))
        if retried < retry_cap:
            runtime["retried"][chapter] = retried + 1
        else:
            runtime["consecutive_failures"] = int(runtime.get("consecutive_failures") or 0) + 1
        append_event(book, "watchdog", "fail",
                     f"第{chapter}章任务停滞超时，已终止",
                     f"环节 {task.get('phase')} 超过 {int(_STALE_S)}s 无进展", chapter=chapter)


def _tick_book(user_id: str, book_id: str, book: Path) -> None:
    state = load_state(book)
    runtime = state["runtime"]

    # 日切：每日计数清零
    today = datetime.now().strftime("%Y-%m-%d")
    if runtime.get("day") != today:
        runtime["day"] = today
        runtime["chapters_done_today"] = 0

    running = _running_tasks(book_id)

    # ① 终态任务处理 + 看门狗（无论有无目标都要把在跑的监督任务收尾）
    tid = runtime.get("active_task_id")
    if tid:
        snap = _task_snapshot(tid)
        if snap and snap.get("status") in ("done", "error", "cancelled"):
            _process_terminal_task(book, state, snap)
        else:
            _watchdog(book, state, running)

    # ② 无目标 → 只在有收尾动作（终态任务处理/看门狗）或首次建档时落盘，
    #    不打扰没启用监督的书（否则每个 tick 都会摸一遍全书架的状态文件）
    if not state.get("goal"):
        if tid or not (_sup_dir(book) / _STATE_FILE).is_file():
            runtime["last_tick"] = time.time()
            save_state(book, state)
        return
    # ②.5 端点退避冷却（infra 失败后）：冷却期内不观察不派单，到点自动续跑
    if float(runtime.get("infra_wait_until") or 0) > time.time():
        runtime["last_tick"] = time.time()
        save_state(book, state)
        return
    # ③ 有任务在跑（含作者手动任务）→ 等待
    running = _running_tasks(book_id)  # 终态处理后刷新
    if running:
        runtime["last_tick"] = time.time()
        save_state(book, state)
        return

    # ④ 观察（阶段机只读）+ 进度计数（按已提交章推进，手动写作也计入）
    phase = resolve_phase(book)
    obs = {
        "phase": phase.get("phase"),
        "target_chapter": phase.get("target_chapter"),
        "last_committed": phase.get("last_committed_chapter") or 0,
        "blocking": phase.get("blocking") or [],
        "warnings": phase.get("warnings") or [],
        "next_action": phase.get("next_action") or "",
        "running": None,
        "planner_enabled": _planner_enabled(),
    }
    _cache_obs(user_id, book_id, obs)
    last = obs["last_committed"]
    prev = runtime.get("last_seen_committed")
    if prev is None:
        runtime["last_seen_committed"] = last
    elif isinstance(last, int) and last > int(prev or 0):
        runtime["chapters_done_today"] = int(runtime.get("chapters_done_today") or 0) + (last - int(prev))
        runtime["last_seen_committed"] = last
        append_event(book, "progress", "done", f"进度推进：已提交至第{last}章",
                     f"目标：{_goal_desc(state.get('goal'))}；今日完成 {runtime['chapters_done_today']} 章")

    # ⑤ 决策 + 执行
    dec = decide(obs, state)
    act = dec.get("act")
    if act == "dispatch":
        _apply_dispatch(user_id, book_id, book, state, dec)
    elif act == "gate":
        append_event(book, f"gate:{dec.get('kind')}", "gate", dec.get("title") or "需作者处置",
                     dec.get("detail") or "", chapter=dec.get("chapter"),
                     dedupe_key=f"gate:{dec.get('kind')}:{dec.get('chapter')}")
    elif act == "stop":
        runtime["stop_reason"] = dec.get("reason")
        append_event(book, "stopped", dec.get("level") or "info",
                     dec.get("title") or f"停止推进：{dec.get('reason')}",
                     f"目标：{_goal_desc(state.get('goal'))}")
    # noop / wait：无事可做

    runtime["last_tick"] = time.time()
    save_state(book, state)


def _iter_user_books() -> list[tuple[str, str, Path]]:
    """枚举全部用户的全部书（(user_id, book_id, book_path)）。监督器在服务端常驻，
    不挂在某个请求的鉴权上下文里，直接扫 projects 根目录。"""
    base = Path(os.environ.get("NWW_PROJECTS_ROOT", str(DEFAULT_PROJECTS_ROOT)))
    out: list[tuple[str, str, Path]] = []
    if not base.is_dir():
        return out
    for udir in sorted(base.iterdir()):
        if not udir.is_dir() or udir.name.startswith("."):
            continue
        try:
            entries = books.list_books(udir)
        except Exception:  # noqa: BLE001 — 单用户枚举失败不影响其他用户
            continue
        for e in entries:
            out.append((udir.name, e["id"], udir / e["id"]))
    return out


# ---------------------------------------------------------------------------
# daemon 循环（仿 publisher scheduler 范式：常驻线程 + 异常兜底 + 优雅退出）
# ---------------------------------------------------------------------------

_THREAD: threading.Thread | None = None
_STOP = threading.Event()


def is_running() -> bool:
    return _THREAD is not None and _THREAD.is_alive()


def _loop() -> None:
    while not _STOP.is_set():
        for user_id, book_id, book in _iter_user_books():
            if _STOP.is_set():
                break
            try:
                _tick_book(user_id, book_id, book)
            except Exception as exc:  # noqa: BLE001 — 单书异常绝不拖垮监督循环
                try:
                    append_event(book, "tick_error", "fail", "监督器 tick 异常", str(exc)[:300])
                except Exception:  # noqa: BLE001
                    pass
        _STOP.wait(_INTERVAL_S)


def start_supervisor() -> bool:
    """启动监督线程（幂等）。返回是否真的启动了。"""
    global _THREAD
    if is_running():
        return False
    _STOP.clear()
    _THREAD = threading.Thread(target=_loop, name="nww-supervisor", daemon=True)
    _THREAD.start()
    return True


def stop_supervisor() -> None:
    _STOP.set()


# ---------------------------------------------------------------------------
# ChatBox 上下文注入：【监督状态】块（后端实算，模型采信，与【进度判定】同款口径）
# ---------------------------------------------------------------------------

def _running_task_label(t: dict) -> str:
    """在跑任务的紧凑中文标签（audit 是区间任务无单章号，防「第None章」占位）。"""
    if t.get("mode") == "audit":
        rng = t.get("range") or []
        return f"审计第{rng[0]}-{rng[1]}章" if len(rng) == 2 and rng[0] else "一致性审计"
    ch = t.get("chapter_id")
    mode = {"write": "写", "rewrite": "重写", "deslop": "去AI味", "revise": "修订", "polish": "润色"}.get(
        str(t.get("mode")), str(t.get("mode") or "?"))
    return f"{mode}第{ch}章" if ch is not None else mode


def supervisor_chat_context(user_id: str, book_id: str) -> str | None:
    """给 book-coach 对话注入监督状态摘要。任何异常 → None（绝不影响对话主链）。"""
    try:
        book = book_root(projects_root(user_id), book_id)
    except Exception:  # noqa: BLE001 — 未绑书/书目录缺失 → 不注入
        return None
    try:
        state = load_state(book)
        goal = state.get("goal")
        runtime = state.get("runtime") or {}
        policy = state.get("policy") or {}
        running = _running_tasks(book_id)
        obs = cached_obs(user_id, book_id)
        unread = unread_events(book, limit=10)
        if not goal and not running and not unread:
            return None  # 没目标没动静没待办：不注入，省提示词

        lines = ["【监督状态】（后端实算的流水线监督摘要，直接采信，不要自行推导）"]
        if goal:
            last = obs.get("last_committed") if obs else None
            head = f"当前目标：{_goal_desc(goal)}"
            if goal.get("type") == "write_to" and isinstance(last, int):
                head += f"（已提交至第{last}章，剩 {max(goal['target_chapter'] - last, 0)} 章）"
            if goal.get("note"):
                head += f"；作者叮嘱：{goal['note']}"
            lines.append(head)
            if policy.get("paused"):
                lines.append("状态：已暂停（不派单，等待作者恢复）")
            elif runtime.get("stop_reason"):
                lines.append(f"状态：已停止——{runtime['stop_reason']}（等待作者 resume 或换目标）")
        if running:
            for t in running:
                src = "监督器" if t.get("origin") == "supervisor" else "作者手动"
                lines.append(f"在跑任务：{_running_task_label(t)}"
                             f"（环节 {t.get('phase')}，{src}，任务 {t.get('task_id')}）")
        elif obs:
            lines.append(f"阶段：{obs.get('phase')}（下一章＝第{obs.get('target_chapter')}章）")
        if runtime.get("consecutive_failures"):
            lines.append(f"注意：当前连续失败 {runtime['consecutive_failures']} 章（熔断阈值 "
                         f"{policy.get('max_consecutive_failures', _MAX_CONSECUTIVE_FAILURES)}）")
        if unread:
            lines.append(f"待作者处理（未读 {len(unread)} 条）：")
            for ev in unread[-5:]:
                lines.append(f"  - [{ev.get('level')}] {ev.get('title')}"
                             + (f"：{(ev.get('detail') or '')[:80]}" if ev.get("detail") else ""))
        return "\n".join(lines)
    except Exception:  # noqa: BLE001 — 注入失败不影响对话
        return None


# ---------------------------------------------------------------------------
# HTTP API（/api/supervisor/*，走既有 AuthMiddleware 与用户隔离）
# ---------------------------------------------------------------------------

def _book_or_404(user_id: str, book_id: str) -> Path:
    try:
        return book_root(projects_root(user_id), book_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/supervisor/overview")
def api_supervisor_overview(user=Depends(get_current_user)) -> dict:
    """书架级总览：每书一行（目标/进度/在跑/未读/阶段缓存）。轮询友好（只读缓存）。"""
    entries = books.list_books(projects_root(user["user_id"]))
    out = []
    for e in entries:
        book_id = e["id"]
        book = projects_root(user["user_id"]) / book_id
        state = load_state(book)
        runtime = state.get("runtime") or {}
        goal = state.get("goal")
        obs = cached_obs(user["user_id"], book_id) or {}
        running = _running_tasks(book_id)
        unread = [ev for ev in _read_events(book) if not ev.get("read")]
        row = {
            "book_id": book_id,
            "title": e.get("title") or book_id,
            "goal": goal,
            "paused": bool((state.get("policy") or {}).get("paused")),
            "stop_reason": runtime.get("stop_reason"),
            "phase": obs.get("phase"),
            "target_chapter": obs.get("target_chapter"),
            "last_committed": e.get("last_committed_chapter") or 0,
            "chapters_done_today": runtime.get("chapters_done_today") or 0,
            "consecutive_failures": runtime.get("consecutive_failures") or 0,
            "running": running[0] if running else None,
            "unread": len(unread),
            "last_event": unread[-1] if unread else None,
        }
        out.append(row)
    return {"books": out, "supervisor_running": is_running(),
            "interval_s": _INTERVAL_S}


@router.get("/supervisor/books/{book_id}/state")
def api_supervisor_state(book_id: str, user=Depends(get_current_user)) -> dict:
    """单书监督详情：目标/策略/运行时 + 最近事件（收件箱）+ 在跑任务。"""
    book = _book_or_404(user["user_id"], book_id)
    state = load_state(book)
    events = _read_events(book)
    obs = cached_obs(user["user_id"], book_id) or {}
    return {
        "book_id": book_id,
        "goal": state.get("goal"),
        "policy": state.get("policy"),
        "runtime": {
            k: state["runtime"].get(k) for k in (
                "day", "chapters_done_today", "consecutive_failures",
                "stop_reason", "active_task_id", "last_failure", "last_tick")
            if k in (state.get("runtime") or {})
        },
        "phase": obs.get("phase"),
        "target_chapter": obs.get("target_chapter"),
        "next_action": obs.get("next_action"),
        "running": _running_tasks(book_id),
        "events": events[-_EVENT_READ_LIMIT:],
        "unread": sum(1 for e in events if not e.get("read")),
    }


@router.post("/supervisor/books/{book_id}/goal")
def api_supervisor_goal(book_id: str, payload: dict, user=Depends(get_current_user)) -> dict:
    """设定/清除目标：{"goal": {...}} 或 {"goal": null}。设定即重置熔断与重试账本。

    write_to 目标章必须大于当前已提交章（否则即刻「已达成」，无意义）。"""
    book = _book_or_404(user["user_id"], book_id)
    raw = payload.get("goal")
    if raw is None:
        state = load_state(book)
        had = state.get("goal")
        state["goal"] = None
        state["runtime"].update({"stop_reason": None, "consecutive_failures": 0,
                                 "retried": {}, "last_failure": None, "active_task_id": None})
        save_state(book, state)
        if had:
            append_event(book, "goal_cleared", "info", f"目标已清除（原：{_goal_desc(had)}）")
        return {"ok": True, "goal": None}

    try:
        goal = validate_goal(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    state = load_state(book)
    if goal["type"] == "write_to":
        tracking_last = (books.get_tracking(book) or {}).get("tracking", {}).get("last_committed_chapter")
        if isinstance(tracking_last, int) and goal["target_chapter"] <= tracking_last:
            raise HTTPException(
                status_code=400,
                detail=f"目标章（第{goal['target_chapter']}章）不大于当前已提交章（第{tracking_last}章）")
    state["goal"] = goal
    runtime = state["runtime"]
    today = datetime.now().strftime("%Y-%m-%d")
    if runtime.get("day") != today:
        runtime["day"] = today
        runtime["chapters_done_today"] = 0
    runtime.update({"stop_reason": None, "consecutive_failures": 0, "retried": {},
                    "last_failure": None,
                    "last_seen_committed": None,   # 下个 tick 以当前进度重置基线
                    "active_task_id": None})
    save_state(book, state)
    append_event(book, "goal_set", "info", f"目标设定：{_goal_desc(goal)}",
                 goal.get("note") or "监督器开始自动推进")
    return {"ok": True, "goal": goal}


@router.post("/supervisor/books/{book_id}/policy")
def api_supervisor_policy(book_id: str, payload: dict, user=Depends(get_current_user)) -> dict:
    """暂停/恢复派单：{"paused": true|false}。

    暂停 = 停止新派单 + 取消监督器派出的在跑任务（作者手动派的不动）；恢复只恢复派单，
    不清除熔断（清熔断用 /resume）。"""
    book = _book_or_404(user["user_id"], book_id)
    paused = payload.get("paused")
    if not isinstance(paused, bool):
        raise HTTPException(status_code=400, detail="paused 必须是布尔值")
    state = load_state(book)
    state.setdefault("policy", {})["paused"] = paused
    if paused:
        tid = (state.get("runtime") or {}).get("active_task_id")
        if tid:
            snap = _task_snapshot(tid)
            if snap and snap.get("status") == "running" and snap.get("origin") == "supervisor":
                _request_cancel(tid)
    save_state(book, state)
    append_event(book, "paused" if paused else "resumed", "info",
                 "监督已暂停（不派单；监督器派出的在跑任务已请求终止）" if paused else "监督已恢复派单")
    return {"ok": True, "paused": paused}


@router.post("/supervisor/books/{book_id}/resume")
def api_supervisor_resume(book_id: str, user=Depends(get_current_user)) -> dict:
    """清除停止态/熔断账本，继续推进当前目标（目标不变）。"""
    book = _book_or_404(user["user_id"], book_id)
    state = load_state(book)
    if not state.get("goal"):
        raise HTTPException(status_code=400, detail="没有进行中的目标，先设定目标")
    runtime = state["runtime"]
    runtime.update({"stop_reason": None, "consecutive_failures": 0,
                    "retried": {}, "last_failure": None,
                    "infra_retries": 0, "infra_wait_until": 0.0})
    save_state(book, state)
    append_event(book, "resumed", "info", f"作者已解除停止态，继续推进：{_goal_desc(state['goal'])}")
    return {"ok": True}


@router.post("/supervisor/books/{book_id}/events/{event_id}/read")
def api_supervisor_event_read(book_id: str, event_id: str, user=Depends(get_current_user)) -> dict:
    book = _book_or_404(user["user_id"], book_id)
    if not mark_event_read(book, event_id):
        raise HTTPException(status_code=404, detail="event not found or already read")
    return {"ok": True}
