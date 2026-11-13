"""live_feed.py — 全站 AI 实况统一采集（2026-11-08）。

目标：AI 实况不再只覆盖「写章 / 重写」两条 pipeline，而是覆盖**所有执行流程**：
任何一次模型调用都必须进流，任何一段长流程都可以标出环节。

三级结构（前端按此渲染）：
    run（流程一次执行，如「写章 · 第3章」「建书立项」「编辑器改写」）
      └─ stage（流程内环节，如 prealign / packet / writer；也可手工标记）
           └─ call（一次物理模型调用：agent、模型、prompt、流式输出、思考、Token、耗时）

设计约束：
  1. 只读观测层：任何采集失败都不得影响主链（全部 try/except 兜底）。
  2. 上下文用 contextvars：pipeline 在后台线程里跑，进入线程时显式绑定 run。
  3. 纯内存 + 环形保留（默认 30 分钟 / 30 个 run），不落盘、不进持久化日志。
  4. 前端轮询增量 seq，首屏拿物化快照，避免重放整段历史。
"""
from __future__ import annotations

import contextlib
import contextvars
import threading
import time
import uuid
from typing import Any, Optional
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------

_LOCK = threading.RLock()
_RUNS: dict[str, dict] = {}
_ORDER: list[str] = []
_EVENTS: list[dict] = []
_SEQ = 0

_RETENTION_S = 30 * 60
_MAX_RUNS = 30
_MAX_EVENTS = 4000
_MAX_CALLS_PER_RUN = 60
_MAX_STAGES_PER_RUN = 80

_PROMPT_CAP = 6000
_OUTPUT_CAP = 12000
_REASONING_CAP = 6000
_EVENT_PROMPT_CAP = 3000
_EVENT_OUTPUT_CAP = 6000
_SNAPSHOT_PROMPT_CAP = 1200
_SNAPSHOT_OUTPUT_CAP = 2500
_DELTA_THROTTLE_S = 0.25
_DELTA_PREVIEW_CAP = 1500

_RUN_CTX: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("nww_live_run", default=None)
_STAGE_CTX: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("nww_live_stage", default=None)
_STAGE_NAME_CTX: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("nww_live_stage_name", default=None)
_AGENT_CTX: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("nww_live_agent", default=None)


def _now() -> float:
    return time.time()


def _next_seq() -> int:
    global _SEQ
    _SEQ += 1
    return _SEQ


def _emit(kind: str, payload: dict) -> None:
    """广播一条事件（供 SSE / 增量轮询）。"""
    try:
        with _LOCK:
            ev = {"seq": _next_seq(), "type": kind, "at": _now(), **payload}
            _EVENTS.append(ev)
            if len(_EVENTS) > _MAX_EVENTS:
                del _EVENTS[: len(_EVENTS) - _MAX_EVENTS]
    except Exception:  # noqa: BLE001 — 观测层绝不穿透主链
        pass


def _provider_label(base_url: Optional[str]) -> str:
    """把端点 URL 归一成人类可读的来源标签（不暴露 key）。"""
    if not base_url:
        return ""
    try:
        parsed = urlparse(base_url)
        host = (parsed.hostname or "").lower()
        port = str(parsed.port or "")
        if host in ("127.0.0.1", "localhost", "::1"):
            if port in ("8788", "8789"):
                return f"本地桥接 :{port}"
            return f"本地 :{port}" if port else "本地"
        if port:
            return f"{host}:{port}"
        return host or base_url[:80]
    except Exception:  # noqa: BLE001
        return base_url[:80]


# ---------------------------------------------------------------------------
# run / stage 上下文
# ---------------------------------------------------------------------------


def _prune_locked() -> None:
    """按保留时长淘汰旧 run（调用方持锁）。"""
    cutoff = _now() - _RETENTION_S
    drop: list[str] = []
    for rid in _ORDER:
        rec = _RUNS.get(rid)
        if rec is None:
            drop.append(rid)
            continue
        if rec.get("status") != "running" and float(rec.get("ended_at") or rec.get("started_at") or 0) < cutoff:
            drop.append(rid)
    for rid in drop:
        _RUNS.pop(rid, None)
    if drop:
        _ORDER[:] = [r for r in _ORDER if r in _RUNS]
    while len(_ORDER) > _MAX_RUNS:
        old = _ORDER.pop(0)
        _RUNS.pop(old, None)


def _run_view(rec: dict, *, prompt_cap: int = _PROMPT_CAP, output_cap: int = _OUTPUT_CAP) -> dict:
    out = dict(rec)
    calls = []
    for call in rec.get("calls") or []:
        c = dict(call)
        c["prompt"] = str(c.get("prompt") or "")[:prompt_cap]
        c["output"] = str(c.get("output") or "")[:output_cap]
        c["reasoning"] = str(c.get("reasoning") or "")[: min(reasoning_cap(), 2000)]
        calls.append(c)
    out["calls"] = calls
    return out


def reasoning_cap() -> int:
    return _REASONING_CAP


def _create_run(
    label: str,
    *,
    mode: Optional[str] = None,
    book_id: Optional[str] = None,
    chapter_id: Any = None,
    task_id: Optional[str] = None,
    run_id: Optional[str] = None,
    kind: str = "flow",
    user_id: Optional[str] = None,
) -> dict:
    rid = str(run_id or f"run-{uuid.uuid4().hex[:12]}")
    rec = {
        "run_id": rid,
        "kind": kind,
        "label": str(label or "AI 执行"),
        "mode": mode or None,
        "book_id": book_id or None,
        "chapter_id": chapter_id,
        "task_id": task_id or None,
        "user_id": user_id or None,
        "status": "running",
        "started_at": _now(),
        "ended_at": None,
        "error": None,
        "stages": [],
        "calls": [],
        "call_count": 0,
    }
    with _LOCK:
        _prune_locked()
        _RUNS[rid] = rec
        if rid in _ORDER:
            _ORDER.remove(rid)
        _ORDER.append(rid)
    _emit("run", {"run": _run_view(rec)})
    return rec


def _finish_run(run_id: str, status: str, error: Optional[str] = None) -> None:
    with _LOCK:
        rec = _RUNS.get(run_id)
        if rec is None:
            return
        if rec.get("status") != "running":
            return
        rec["status"] = status
        rec["ended_at"] = _now()
        if error:
            rec["error"] = str(error)[:500]
        # 未收尾的环节一律标记（进程级异常/取消时保证前端不悬挂「执行中」卡片）
        for st in rec.get("stages") or []:
            if st.get("status") == "running":
                st["status"] = "failed" if status == "error" else "cancelled"
                st["ended_at"] = st.get("ended_at") or _now()
    _emit("run", {"run": _run_view(rec)})


@contextlib.contextmanager
def begin_run(
    label: str,
    *,
    mode: Optional[str] = None,
    book_id: Optional[str] = None,
    chapter_id: Any = None,
    task_id: Optional[str] = None,
    run_id: Optional[str] = None,
    kind: str = "flow",
    user_id: Optional[str] = None,
):
    """开启一次「流程执行」（run）。退出时自动收尾；异常记为 error。"""
    rec = _create_run(
        label, mode=mode, book_id=book_id, chapter_id=chapter_id,
        task_id=task_id, run_id=run_id, kind=kind, user_id=user_id,
    )
    run_token = _RUN_CTX.set(rec["run_id"])
    stage_token = _STAGE_CTX.set(None)
    try:
        yield rec
        _finish_run(rec["run_id"], "done")
    except BaseException as exc:  # noqa: BLE001 — 必须原样上抛
        status = "cancelled" if isinstance(exc, GeneratorExit) else "error"
        _finish_run(rec["run_id"], status, str(exc))
        raise
    finally:
        _STAGE_CTX.reset(stage_token)
        _RUN_CTX.reset(run_token)


def current_run_id() -> Optional[str]:
    return _RUN_CTX.get()


def current_stage_id() -> Optional[str]:
    return _STAGE_CTX.get()


def bind_run(run_id: str) -> contextvars.Token:
    """在别的线程里复用已有 run（如 pipeline 的 bounded 线程）。"""
    return _RUN_CTX.set(run_id)


def unbind_run(token: contextvars.Token) -> None:
    try:
        _RUN_CTX.reset(token)
    except Exception:  # noqa: BLE001
        pass


@contextlib.contextmanager
def agent_scope(name: Optional[str], session: str = ""):
    """给当前线程标上 agent 名（模型调用记录据此显示「哪个 agent」）。"""
    token = _AGENT_CTX.set(f"{name} · {session}" if session else (name or ""))
    try:
        yield
    finally:
        _AGENT_CTX.reset(token)


_STAGE_LABELS = {
    "prealign": "写前对齐",
    "packet": "打包上下文",
    "writer": "主笔生成",
    "normalize": "归一清洗",
    "wordcount": "字数闸",
    "deslop": "去AI味",
    "review_cycle": "审稿闭环",
    "post_write": "落盘提交",
    "commit": "提交落账",
    "delta_semantic": "语义补全",
    "delta": "追踪增量",
    "consistency": "一致性检测",
    "acceptance": "验收落账",
    "acceptance_commit": "验收提交",
    "editor_rewrite": "选段改写",
    "repair_plan": "修复方案",
    "repair_execute": "修复执行",
    "closure": "闭环确认",
    "initialize": "初始化追踪",
    "deslop_text": "贴文去AI味",
    "call": "模型调用",
    "report": "审计报告",
}


def label_for_stage(name: str) -> str:
    """环节中文标签（未知环节回退原名；audit_chN 归一为第N章审计）。"""
    if not name:
        return "环节"
    if name.startswith("audit_ch"):
        tail = name[len("audit_ch"):]
        return f"第 {tail} 章审计" if tail.isdigit() else "逐章审计"
    return _STAGE_LABELS.get(name, name)


class StageHandle:
    """环节句柄：允许中间补充 detail / 完整产物。"""

    __slots__ = ("detail", "output_text", "error")

    def __init__(self, detail: str = "") -> None:
        self.detail = detail
        self.output_text = ""
        self.error: Optional[str] = None

    def detail_set(self, text: str) -> None:
        self.detail = str(text or "")

    def output(self, text: str) -> None:
        self.output_text = str(text or "")

    def fail(self, error: object) -> None:
        self.error = str(error or "")


def _find_stage(run: dict, stage_id: str) -> Optional[dict]:
    for st in run.get("stages") or []:
        if st.get("id") == stage_id:
            return st
    return None


def stage_begin(name: str, detail: str = "", label: Optional[str] = None) -> Optional[str]:
    """标记环节开始，返回 stage_id（无 run 上下文时返回 None，静默忽略）。"""
    run_id = _RUN_CTX.get()
    if not run_id:
        return None
    try:
        with _LOCK:
            rec = _RUNS.get(run_id)
            if rec is None:
                return None
            if len(rec.get("stages") or []) >= _MAX_STAGES_PER_RUN:
                return None
            stage_id = f"{run_id}:{name}:{uuid.uuid4().hex[:6]}"
            st = {
                "id": stage_id,
                "name": name,
                "label": label or label_for_stage(name),
                "status": "running",
                "detail": str(detail or "")[:600],
                "output": "",
                "started_at": _now(),
                "ended_at": None,
                "duration_ms": None,
                "ok": None,
            }
            rec.setdefault("stages", []).append(st)
        _STAGE_CTX.set(stage_id)
        _STAGE_NAME_CTX.set(name)
        _emit("stage", {"run_id": run_id, "stage": dict(st)})
        return stage_id
    except Exception:  # noqa: BLE001
        return None


def stage_end(
    name: str,
    ok: bool = True,
    detail: str = "",
    output: str = "",
    *,
    stage_id: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """标记环节完成（stage_id 为空时按 name 取当前/最近一个同名运行中环节）。"""
    run_id = _RUN_CTX.get()
    if not run_id:
        return
    try:
        with _LOCK:
            rec = _RUNS.get(run_id)
            if rec is None:
                return
            sid = stage_id or _STAGE_CTX.get()
            st = _find_stage(rec, sid) if sid else None
            # 优先按「同名且仍在执行」匹配：环节 begin 由调用方逐次推送，未必走上下文
            for cand in reversed(rec.get("stages") or []):
                if cand.get("name") == name and cand.get("status") == "running":
                    st = cand
                    break
            if st is None:
                # 仅上报完成、从未上报开始的环节（审计逐章回执等）：补一张已完成卡片，保证可见
                now = _now()
                st = {
                    "id": f"{run_id}:{name}:{uuid.uuid4().hex[:6]}",
                    "name": name,
                    "label": label_for_stage(name),
                    "status": "done" if ok else "failed",
                    "detail": str(detail or "")[:600],
                    "output": str(output or "")[: _OUTPUT_CAP],
                    "started_at": now,
                    "ended_at": now,
                    "duration_ms": None,
                    "ok": bool(ok),
                }
                rec.setdefault("stages", []).append(st)
                _emit("stage", {"run_id": run_id, "stage": dict(st)})
                return
            st["status"] = "done" if ok else "failed"
            st["ok"] = bool(ok)
            st["ended_at"] = _now()
            st["duration_ms"] = int(max(0, (st["ended_at"] - float(st.get("started_at") or st["ended_at"])) * 1000))
            if detail:
                st["detail"] = str(detail)[:600]
            if output:
                st["output"] = str(output)[: _OUTPUT_CAP]
            if error:
                st["error"] = str(error)[:500]
        _emit("stage", {"run_id": run_id, "stage": dict(st)})
    except Exception:  # noqa: BLE001
        pass


@contextlib.contextmanager
def stage(name: str, detail: str = "", label: Optional[str] = None):
    """环节上下文：进入即推送「执行中」卡片，退出推送完成/失败卡片。"""
    sid = stage_begin(name, detail=detail, label=label)
    handle = StageHandle(detail)
    try:
        yield handle
    except BaseException as exc:  # noqa: BLE001
        stage_end(name, False, handle.detail or detail, handle.output_text, stage_id=sid, error=str(exc))
        raise
    else:
        stage_end(name, handle.error is None, handle.detail or detail, handle.output_text, stage_id=sid, error=handle.error)


# ---------------------------------------------------------------------------
# 模型调用
# ---------------------------------------------------------------------------


class LlmCall:
    """一次物理模型调用（端点级，含降级链中的每一次尝试）。"""

    __slots__ = (
        "call_id", "started_at", "_last_delta_at", "_chars", "_reasoning_chars",
        "_output", "_reasoning", "_closed",
    )

    def __init__(self, call_id: str) -> None:
        self.call_id = call_id
        self.started_at = _now()
        self._last_delta_at = 0.0
        self._chars = 0
        self._reasoning_chars = 0
        self._output = ""
        self._reasoning = ""
        self._closed = False

    def delta(self, chunk: str) -> None:
        if not chunk:
            return
        self._output += chunk
        if len(self._output) > _OUTPUT_CAP * 2:
            self._output = self._output[-_OUTPUT_CAP:]
        self._chars += len(chunk)
        self._push_delta()

    def reasoning(self, chunk: str) -> None:
        if not chunk:
            return
        self._reasoning += chunk
        if len(self._reasoning) > _REASONING_CAP * 2:
            self._reasoning = self._reasoning[-_REASONING_CAP:]
        self._reasoning_chars += len(chunk)
        self._push_delta(force=False)

    def finish(self, output: Optional[str] = None) -> None:
        if output:
            self._output = str(output)
            self._chars = len(self._output)

    def _push_delta(self, force: bool = True) -> None:
        now = _now()
        if not force and now - self._last_delta_at < _DELTA_THROTTLE_S:
            return
        if now - self._last_delta_at < _DELTA_THROTTLE_S:
            return
        self._last_delta_at = now
        _emit("call_delta", {
            "run_id": _RUN_CTX.get(),
            "call_id": self.call_id,
            "chars": self._chars,
            "reasoning_chars": self._reasoning_chars,
            "preview": self._output[-_DELTA_PREVIEW_CAP:],
        })


def _call_record(system_prompt: str, user_prompt: str, *, model: Optional[str], base_url: Optional[str], stream: bool) -> tuple[dict, str]:
    run_id = _RUN_CTX.get()
    stage_id = _STAGE_CTX.get()
    agent = _AGENT_CTX.get() or ""
    prompt = ""
    if system_prompt:
        prompt += "【system】\n" + str(system_prompt)
    if user_prompt:
        prompt += ("\n\n" if prompt else "") + "【user】\n" + str(user_prompt)
    call_id = f"call-{uuid.uuid4().hex[:12]}"
    rec = {
        "id": call_id,
        "run_id": run_id,
        "stage": stage_id,
        "stage_name": _STAGE_NAME_CTX.get(),
        "agent": agent,
        "model": model or "",
        "provider": _provider_label(base_url),
        "base_url": (base_url or "")[:200],
        "stream": bool(stream),
        "prompt": prompt[:_PROMPT_CAP],
        "prompt_chars": len(prompt),
        "output": "",
        "reasoning": "",
        "chars": 0,
        "reasoning_chars": 0,
        "status": "running",
        "error": None,
        "started_at": _now(),
        "ended_at": None,
        "duration_ms": None,
        "tokens": None,
    }
    return rec, prompt


@contextlib.contextmanager
def track_call(
    system_prompt: str,
    user_prompt: str,
    *,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    stream: bool = False,
):
    """包裹一次真实模型调用：开始即入流，结束时带完整产物/耗时/状态收尾。"""
    rec, _prompt = _call_record(system_prompt, user_prompt, model=model, base_url=base_url, stream=stream)
    call = LlmCall(rec["id"])
    run_id = rec["run_id"]
    auto_run = None
    auto_token = None
    if not run_id:
        # 兜底：没有任何 run 上下文（零散/后台 AI 调用）也必须有归属，否则实况不可见。
        # 每次调用自建一个最小 run（一个环节一张卡），保证「所有 AI 调用都进实况」。
        auto_run = _create_run(
            f"AI 调用 · {rec.get('agent') or rec.get('model') or '未归属流程'}",
            kind="flow", mode="call",
        )
        auto_token = _RUN_CTX.set(auto_run["run_id"])
        rec["run_id"] = auto_run["run_id"]
        run_id = rec["run_id"]
        stage_begin("call", detail=str(rec.get("agent") or ""), label=str(rec.get("agent") or "模型调用"))
        rec["stage"] = _STAGE_CTX.get()
        rec["stage_name"] = "call"
    emitted = False
    try:
        if run_id:
            with _LOCK:
                run = _RUNS.get(run_id)
                if run is not None and len(run.get("calls") or []) < _MAX_CALLS_PER_RUN:
                    run.setdefault("calls", []).append(rec)
                    run["call_count"] = int(run.get("call_count") or 0) + 1
                    emitted = True
        if emitted or not run_id:
            _emit("call", {"run_id": run_id, "call": dict(rec, prompt=rec["prompt"][:_EVENT_PROMPT_CAP])})
    except Exception:  # noqa: BLE001
        pass
    try:
        yield call
    except BaseException as exc:  # noqa: BLE001
        _close_call(rec, call, "error", error=str(exc))
        _close_auto_run(auto_run, auto_token, rec, "error", str(exc))
        raise
    else:
        _close_call(rec, call, "done")
        _close_auto_run(auto_run, auto_token, rec, "done", None)


def _close_call(rec: dict, call: LlmCall, status: str, error: Optional[str] = None) -> None:
    try:
        now = _now()
        rec["output"] = call._output
        rec["reasoning"] = call._reasoning
        rec["chars"] = call._chars
        rec["reasoning_chars"] = call._reasoning_chars
        rec["status"] = status
        rec["error"] = (str(error)[:400] if error else None)
        rec["ended_at"] = now
        rec["duration_ms"] = int(max(0, (now - float(rec.get("started_at") or now)) * 1000))
        run_id = rec.get("run_id")
        if run_id:
            with _LOCK:
                run = _RUNS.get(run_id)
                if run is not None:
                    for i, cand in enumerate(run.get("calls") or []):
                        if cand.get("id") == rec["id"]:
                            run["calls"][i] = rec
                            break
        _emit("call", {
            "run_id": run_id,
            "call": dict(
                rec,
                prompt=rec["prompt"][:_EVENT_PROMPT_CAP],
                output=rec["output"][:_EVENT_OUTPUT_CAP],
                reasoning=rec["reasoning"][:2000],
            ),
        })
    except Exception:  # noqa: BLE001
        pass


def _close_auto_run(
    auto_run: Optional[dict],
    auto_token,
    rec: dict,
    status: str,
    error: Optional[str],
) -> None:
    """收尾「无归属调用」自建的临时 run。"""
    if not auto_run:
        return
    try:
        ok = status == "done"
        stage_end("call", ok, str(rec.get("agent") or ""), str(rec.get("output") or "")[:2000], error=error)
        _finish_run(auto_run["run_id"], "done" if ok else "error", error)
    except Exception:  # noqa: BLE001
        pass
    finally:
        if auto_token is not None:
            unbind_run(auto_token)


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


def _snapshot_runs_locked() -> list[dict]:
    out = []
    for rid in _ORDER[-10:]:
        rec = _RUNS.get(rid)
        if rec is None:
            continue
        view = _run_view(rec, prompt_cap=_SNAPSHOT_PROMPT_CAP, output_cap=_SNAPSHOT_OUTPUT_CAP)
        view["calls"] = view.get("calls", [])[-25:]
        out.append(view)
    return out


def snapshot(since: int = 0, *, user_id: Optional[str] = None) -> dict:
    """首屏（since<=0）返回物化快照；否则返回 since 之后的增量事件。"""
    with _LOCK:
        _prune_locked()
        last_seq = _SEQ
        if since and since > 0:
            events = [e for e in _EVENTS if int(e.get("seq") or 0) > since]
            if user_id:
                keep = {rid for rid, rec in _RUNS.items() if rec.get("user_id") in (None, user_id)}
                events = [e for e in events if (e.get("run_id") in keep) or (e.get("run") or {}).get("run_id") in keep]
            return {"seq": last_seq, "reset": False, "events": events}
        runs = _snapshot_runs_locked()
        if user_id:
            runs = [r for r in runs if r.get("user_id") in (None, user_id)]
        return {"seq": last_seq, "reset": True, "runs": runs}


def running_runs() -> list[dict]:
    with _LOCK:
        return [
            {"run_id": rid, "label": _RUNS[rid].get("label"), "status": _RUNS[rid].get("status")}
            for rid in _ORDER
            if _RUNS.get(rid, {}).get("status") == "running"
        ]
