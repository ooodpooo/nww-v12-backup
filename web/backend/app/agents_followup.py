"""P1-A 失败自动跟进（外脑 2026-10-31 裁决 GO/A+）：终态事件钩子 → 单次受限研判
→ 最多派 1 个 revise 修复单 → 人工决策卡闭环。

三条硬边界（外脑裁决，实现必须逐条可验证）：
1. 只分析失败，不直接写正文：研判输出严格 JSON（action 仅 dispatch_fix/hold），
   模型建议不等于执行授权——执行只走预定义 revise 派单入口。
2. 只修一层，不递归：agent 派出的修复任务 origin=agent_followup，永不二次触发；
   每个原始失败任务只允许一次研判、一次派单（followup_key 全局唯一关联）。
3. 旁路不伤主链：本模块任何异常都不得改变原任务的 ok/failure/rolled_back 结果；
   研判超时、模型异常、状态写入失败一律降级为人工决策卡；409 章槽冲突视为
   冲突不绕过（降级 hold）。

幂等与恢复（外脑 A+ 方案）：
- 独立状态文件 runtime/agent_followup/{task_id}.json（原子写），
  状态机 pending → analyzing → dispatching → completed/failed。
- followup_key = "agent_followup:" + original_task_id，写入任务登记并落盘
  （agent_tasks/*.json 是已实测的可靠登记边界：register 同步落盘且先于线程启动）。
- 崩溃恢复：boot 后首次钩子触发时扫描 dispatching 残留——按 followup_key 查到
  已登记任务则补写 completed 不重派；查不到则置 failed 降级人工
  （不自动重派：外脑红线"无法确认是否已派单时不批准自动重派"）。

默认关闭：环境变量 NWW_AGENT_FOLLOWUP=1 才启用。supervisor 与既有 revise
HTTP 端点零改动。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from .llm_client import chat_completion_robust

# ── 常量 ──────────────────────────────────────────────────────────────────────

_FOLLOWUP_DIR = Path(__file__).resolve().parents[3] / "runtime" / "agent_followup"
# 与 nwwmvp._AGENT_TASKS_DIR 同级（parents[3] = 工程根），崩溃恢复时按 followup_key
# 扫描 agent_tasks 落盘登记（实测 22 个真实文件证明该登记可靠）。

# 触发集合：write 链失败归监督器管辖（其失败重试语义已闭环），不进本钩子
_FOLLOWUP_MODES = {"rewrite", "polish", "deslop", "revise"}

# 防递归：本模块派出的修复任务 origin 标记，钩子见此 origin 直接跳过
_AGENT_FOLLOWUP_ORIGIN = "agent_followup"

_NOTE_MAX = 500  # 与 revise 端点约束一致（nwwmvp api_agents_revise_async）

_SYS_PROMPT = (
    "你是网文生产线的失败研判器。根据给定的任务失败信息，判断是否应自动派发一次"
    "「局部修订」（revise）修复单。只输出一个 JSON 对象，不要输出任何其他文字：\n"
    '{"action": "dispatch_fix 或 hold", "note": "定点修复要点（revise 指令，'
    '必须具体可执行、≤500字）", "reason": "选择依据（必填）"}\n'
    "规则：失败原因明确、且能用一次局部修订（保留现有正文，只修订指定位点）解决时"
    "才 dispatch_fix；归因未确认、需要重写/扩写整章、需要作者决策、或证据不足时一律 hold。"
)

_VERDICT_NOTE_PREFIX = "自动跟进研判："


# ── 状态持久化（原子写） ──────────────────────────────────────────────────────

def _state_path(task_id: str) -> Path:
    return _FOLLOWUP_DIR / f"{task_id}.json"


def _load_state(task_id: str) -> dict | None:
    try:
        return json.loads(_state_path(task_id).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — 读不到/损坏=无状态
        return None


def _save_state(task_id: str, state: dict) -> None:
    """原子写（tmp+replace，与 nwwmvp._task_persist 同款模式）。失败静默——
    旁路不得伤主链；dispatching 写失败由 boot 恢复的"查不到→failed"兜底。"""
    try:
        _FOLLOWUP_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _FOLLOWUP_DIR / f".{task_id}.{uuid.uuid4().hex[:6]}.tmp"
        tmp.write_text(json.dumps({"task_id": task_id, **state}, ensure_ascii=False, default=str),
                       encoding="utf-8")
        tmp.replace(_state_path(task_id))
    except Exception:  # noqa: BLE001
        pass


def _enabled() -> bool:
    return os.environ.get("NWW_AGENT_FOLLOWUP", "").strip() == "1"


# ── 并发独占权（研判权唯一）：O_CREAT|O_EXCL 原子创建锁文件 ───────────────────

def _claim(task_id: str) -> bool:
    """同 task_id 并发钩子只允许一个获得研判权（验收场景 6）。"""
    try:
        _FOLLOWUP_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(_FOLLOWUP_DIR / f".{task_id}.claim"),
                     os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except Exception:  # noqa: BLE001 — 锁异常按"拿不到"处理，降级人工
        return False


# ── 研判（受限：只输出结构化判定，不执行） ────────────────────────────────────

def _analyze_failure(*, task_id: str, book_id: str, chapter_id: int, mode: str,
                     failure: dict, facts: dict, receipt_path: str,
                     attribution_confirmed: bool) -> dict:
    """单次 LLM 研判。输入字段固定（外脑约束）；非法输出一律降级 hold。"""
    attribution = "已确认（pipeline fatal_phase 实证）" if attribution_confirmed \
        else "未确认（回退口径取第一个失败环节，可能非致死环节——不得当作已证实事实）"
    user_prompt = json.dumps({
        "task_id": task_id,
        "book_id": book_id,
        "chapter_id": chapter_id,
        "mode": mode,
        "fatal_phase": failure.get("stage"),
        "failure_detail": failure.get("detail"),
        "receipt_summary": {"gate": failure.get("gate"), "rolled_back": failure.get("rolled_back"),
                            "facts": facts, "receipt_path": receipt_path},
        "attribution": attribution,
    }, ensure_ascii=False)
    try:
        from .llm_client import chat_completion
        from .nwwmvp import _cfg_default_chain, _resolve_write_endpoint
        chain = _cfg_default_chain()
        if chain:
            # 模型中心模式：沿端点链调用（与 nwwmvp 修复规划器同款惯例）
            text = chat_completion_robust(_SYS_PROMPT, user_prompt, chain,
                                          temperature=0.1, max_tokens=800, timeout=120)
        else:
            # 直连环境变量模式（运行中后端实测：NWW_LLM_BASE_URL/MODEL/API_KEY）——
            # 冒烟发现缺此兜底会全部假 hold，与既有消费方惯例对齐
            base_url, model, api_key, timeout = _resolve_write_endpoint()
            text = chat_completion(_SYS_PROMPT, user_prompt, temperature=0.1, max_tokens=800,
                                   base_url=base_url, model=model, api_key=api_key,
                                   timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — 研判异常降级 hold（验收场景 3）
        return {"action": "hold", "note": "", "reason": f"研判调用异常降级人工: {exc}"}
    return _parse_verdict(text)


def _parse_verdict(text: str) -> dict:
    """严格 JSON 解析：非法/未知 action/字段缺失/超限 → hold（外脑红线）。
    真实冒烟（2026-10-31）发现模型回复常带解释文字或多段 JSON，find/rfind 截取
    会截错段——改为 JSONDecoder.raw_decode 逐 '{' 候选解析，取第一个 action
    合法者；全部失败仍降级 hold，严格性不变。"""
    import json as _json
    decoder = _json.JSONDecoder()
    fallback_reason = ""
    for idx, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            v, _ = decoder.raw_decode(text[idx:])
        except Exception:  # noqa: BLE001 — 该候选不是完整 JSON，继续找下一个
            continue
        if not isinstance(v, dict):
            continue
        action = v.get("action")
        note = str(v.get("note", "") or "").strip()
        reason = str(v.get("reason", "") or "").strip()
        if action not in ("dispatch_fix", "hold"):
            fallback_reason = f"未知 action={action}，降级人工"
            continue
        if action == "dispatch_fix" and (not note or not reason):
            fallback_reason = "dispatch_fix 缺 note/reason，降级人工"
            continue
        return {"action": action, "note": note[:_NOTE_MAX], "reason": reason[:500]}
    if fallback_reason:
        return {"action": "hold", "note": "", "reason": fallback_reason}
    return {"action": "hold", "note": "", "reason": "研判输出无可解析的合法 verdict JSON，降级人工"}


# ── 派单（复用 revise 既有守卫，不绕过 409） ─────────────────────────────────

class FollowupConflict(Exception):
    """409 章槽冲突——外脑规则：视为冲突不绕过，上层降级 hold。"""


def _spawn_followup_revise(*, book: Path, book_id: str, chapter_id: int, note: str,
                           followup_key: str, source_task_id: str,
                           user_id: str | None = None) -> str:
    """内部派 revise：409 守卫 → 登记（含 followup_key，同步落盘）→ 落盘校验 → 起线程。
    登记是派单成功的可靠边界（实测：_task_register 先落盘后 th.start()），
    落盘校验不过则不起线程（不确定边界→上层降级人工，不赌）。"""
    import threading as _th
    from .nwwmvp import _task_run, _try_acquire_op, _op_busy_detail, _task_register, _AGENT_TASKS_DIR, _AGENT_TASKS

    task_id = uuid.uuid4().hex[:12]
    thread = _th.Thread(target=_task_run,
                        args=(task_id, book_id, book, chapter_id, "revise", False, note, False, False),
                        daemon=True)
    busy = _try_acquire_op(book_id, chapter_id, "revise", task_id, thread=thread)
    if busy:
        raise FollowupConflict(_op_busy_detail(book_id, chapter_id, busy))
    _task_register(task_id, {"status": "running", "phase": "queued", "chapter_id": chapter_id,
                             "mode": "revise", "note": note, "book_id": book_id,
                             "origin": _AGENT_FOLLOWUP_ORIGIN,
                             "followup_key": followup_key,
                             "source_task_id": source_task_id},
                  user_id=user_id)
    # 落盘校验：登记必须已持久化且含 followup_key，才允许起线程（派单可靠边界）
    try:
        rec = json.loads((_AGENT_TASKS_DIR / f"{task_id}.json").read_text(encoding="utf-8"))
        if rec.get("followup_key") != followup_key:
            raise ValueError("登记落盘缺少 followup_key")
    except Exception as exc:  # noqa: BLE001 — 不确定边界：不起线程，降级人工
        raise RuntimeError(f"登记落盘校验失败，不起线程: {exc}") from exc
    thread.start()
    return task_id


# ── boot 恢复（dispatching 残留，验收场景 7） ────────────────────────────────

def _recover_dispatching_residue() -> None:
    """扫描 dispatching 状态残留：按 followup_key 查 agent_tasks 落盘登记——
    查到 → 补写 completed 不重派；查不到 → failed 降级人工（不自动重派）。"""
    try:
        from .nwwmvp import _AGENT_TASKS_DIR
        if not _FOLLOWUP_DIR.is_dir():
            return
        index: dict[str, str] = {}  # followup_key -> task_id（来自落盘登记）
        for f in _AGENT_TASKS_DIR.glob("*.json"):
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
                key = rec.get("followup_key")
                if key:
                    index[str(key)] = str(rec.get("task_id") or f.stem)
            except Exception:  # noqa: BLE001
                continue
        for f in _FOLLOWUP_DIR.glob("*.json"):
            try:
                st = json.loads(f.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if st.get("status") != "dispatching":
                continue
            tid = str(st.get("task_id") or "")
            key = str(st.get("followup_key") or f"agent_followup:{tid}")
            hit = index.get(key)
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if hit:
                _save_state(tid, {**st, "status": "completed", "followup_task_id": hit,
                                  "recovered": True, "updated_at": now})
            else:
                _save_state(tid, {**st, "status": "failed",
                                  "error": "boot 恢复未找到已派任务，降级人工决策卡（不自动重派）",
                                  "updated_at": now})
    except Exception:  # noqa: BLE001 — 恢复失败不影响主链
        pass


_BOOT_RECOVERED = False


def _boot_recover_once() -> None:
    global _BOOT_RECOVERED
    if _BOOT_RECOVERED:
        return
    _BOOT_RECOVERED = True
    _recover_dispatching_residue()


# ── 主流程（后台线程，绝不抛出） ──────────────────────────────────────────────

def maybe_followup(task_id: str, book_id: str, book: Path, chapter_id: int, mode: str,
                   result: dict, dry_run: bool = False) -> None:
    """_task_run 终态钩子入口（nwwmvp 一行挂载）。同步部分只做廉价判断，
    实际处理在独立 daemon 线程——绝不阻塞/破坏主链收尾。"""
    try:
        if not _enabled():
            return
        # dry-run（预览）任务必须零副作用：失败也不自动派真改正文的 revise
        # （2026-10-31 自主验收发现并修复：派单硬编码 dry_run=False，预览失败会误派实改）
        if dry_run:
            return
        if mode not in _FOLLOWUP_MODES:
            return
        if not result.get("needs_decision"):
            return
        # 防递归：agent_followup 派生的任务失败不再触发（外脑边界 2）
        try:
            from .nwwmvp import _AGENT_TASKS, _AGENT_TASKS_LOCK
            with _AGENT_TASKS_LOCK:
                origin = str(_AGENT_TASKS.get(task_id, {}).get("origin") or "manual")
        except Exception:  # noqa: BLE001
            origin = "manual"
        if origin == _AGENT_FOLLOWUP_ORIGIN:
            return
        threading.Thread(target=handle_failure,
                         args=(task_id, book_id, book, chapter_id, mode, dict(result)),
                         daemon=True).start()
    except Exception:  # noqa: BLE001 — 钩子异常绝不外泄
        pass


def handle_failure(task_id: str, book_id: str, book: Path, chapter_id: int, mode: str,
                   result: dict) -> None:
    """状态机主流程：pending → analyzing → (dispatching → completed) / completed(hold) / failed。
    幂等：已 completed/failed 的 task_id 直接跳过（验收场景 5）。"""
    try:
        _boot_recover_once()
        prev = _load_state(task_id)
        if prev and prev.get("status") in ("completed", "failed"):
            return
        if not _claim(task_id):
            return
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _save_state(task_id, {"status": "analyzing", "mode": mode, "chapter_id": chapter_id,
                              "created_at": now, "updated_at": now})

        failure = result.get("failure") or {}
        # P0-1 口径：归因未确认时 detail 带「［归因未确认…］」前缀——此处显式化布尔
        attribution_confirmed = not str(failure.get("detail") or "").startswith("［归因未确认")

        verdict = _analyze_failure(task_id=task_id, book_id=book_id, chapter_id=chapter_id,
                                   mode=mode, failure=failure, facts=result.get("facts") or {},
                                   receipt_path=str(result.get("receipt_path") or ""),
                                   attribution_confirmed=attribution_confirmed)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if verdict.get("action") != "dispatch_fix":
            _save_state(task_id, {"status": "completed", "action": "hold",
                                  "reason": verdict.get("reason"), "mode": mode,
                                  "chapter_id": chapter_id, "updated_at": now})
            return

        followup_key = f"agent_followup:{task_id}"
        # 外脑要求：派单前先持久化 dispatching（跨崩溃幂等的关键一手状态）
        _save_state(task_id, {"status": "dispatching", "action": "dispatch_fix",
                              "note": verdict.get("note"), "followup_key": followup_key,
                              "mode": mode, "chapter_id": chapter_id, "updated_at": now})
        try:
            # user_id 从源任务记录继承（2026-11-07 多用户隔离根治）：源任务已带 user_id
            # 必须在此作用域内显式导入：`_spawn_followup_revise` 内的局部 import 不会
            # 泄漏到 handle_failure（此前 NameError 使 dispatch_fix 一律降级 failed）。
            from .nwwmvp import _AGENT_TASKS
            _src_user = _AGENT_TASKS.get(task_id, {}).get("user_id")
            followup_task_id = _spawn_followup_revise(
                book=book, book_id=book_id, chapter_id=chapter_id,
                note=str(verdict.get("note") or ""), followup_key=followup_key,
                source_task_id=task_id, user_id=_src_user)
        except FollowupConflict as exc:  # 409 不绕过 → hold 降级人工（验收场景 9）
            _save_state(task_id, {"status": "completed", "action": "hold",
                                  "reason": f"章槽占用冲突，降级人工: {exc}",
                                  "followup_key": followup_key, "mode": mode,
                                  "chapter_id": chapter_id, "updated_at": now})
            return
        except Exception as exc:  # 登记落盘校验失败等不确定边界 → failed 人工兜底
            _save_state(task_id, {"status": "failed", "followup_key": followup_key,
                                  "error": f"派单未完成（不确定边界，降级人工）: {exc}",
                                  "mode": mode, "chapter_id": chapter_id, "updated_at": now})
            return
        _save_state(task_id, {"status": "completed", "action": "dispatch_fix",
                              "followup_key": followup_key, "followup_task_id": followup_task_id,
                              "note": verdict.get("note"), "mode": mode,
                              "chapter_id": chapter_id, "updated_at": now})
    except Exception as exc:  # noqa: BLE001 — 兜底：任何异常都降级人工，不外泄
        try:
            _save_state(task_id, {"status": "failed",
                                  "error": f"跟进处理异常降级人工: {exc}",
                                  "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        except Exception:  # noqa: BLE001
            pass
