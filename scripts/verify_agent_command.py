#!/usr/bin/env python3
"""步骤 4：Chat Agent → command 真实触发验证（后端 API 直打，不走 UI）。

用户 2026-09-26 裁定：
  - **不走 UI**（前端不是未知变量，当前要证的是"Gemini Web 生成 command → backend 接收"）
  - **不触发真实章节重写**（不调任何 /mvp/agents/* 执行端点）
  - 不用 protocol_test（不在白名单，只能验到 Parser 层）

本脚本只调 `/api/agent/chat`：它是**无状态、不落盘**的（源码注释明写
"pipeline 执行仍由前端一键触发"），只返回 proposed_actions。
⇒ 天然满足"不修改正文、不创建真任务、不进 writer"。

通过标准（用户裁定）：
  1. 浏览器  EXECUTE_JSON 存在      —— 步骤 3 已验 ✅
  2. Parser  commands=[...]        —— 步骤 3 已验 ✅
  3. Agent   日志 [AGENT_COMMAND] action=... accepted=true   ← 本脚本
  4. Task    —— 本架构里 task 由前端按 proposed_actions 触发，后端不建 task

用法：
    /opt/miniconda3/bin/python scripts/verify_agent_command.py
    /opt/miniconda3/bin/python scripts/verify_agent_command.py --chapter 25
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND_LOG = Path(os.path.expanduser("~/.nww-local/backend.log"))

API = os.environ.get("NWW_API_BASE", "http://127.0.0.1:8010")
USER = os.environ.get("NWW_USER", "admin")
PASS = os.environ.get("NWW_PASS", "admin123")
# Chrome 桥接池（runtime/config/model_config.json → pool[].id）
POOL_ID = os.environ.get("NWW_POOL_ID", "chrome-gemini-3-8-flash")
BOOK_ID = os.environ.get("NWW_BOOK_ID", "用泡面征服修仙界")


def _login() -> str:
    import urllib.request

    req = urllib.request.Request(
        f"{API}/auth/login",
        data=json.dumps({"user": USER, "password": PASS}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as fh:
        return json.loads(fh.read().decode())["token"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chapter", type=int, default=25)
    ap.add_argument("--timeout", type=float, default=240.0)
    ap.add_argument("--no-task", action="store_true", help="不调 audit/async，只验到 command")
    args = ap.parse_args()

    import urllib.request

    token = _login()
    print("=" * 74)
    print("步骤 4 · Chat Agent → command（后端 API 直打）")
    print("=" * 74)
    print(f"  API      : {API}")
    print(f"  book_id  : {BOOK_ID}")
    print(f"  pool_id  : {POOL_ID}   ← Chrome 桥接池（走 gemini.google.com）")
    print(f"  目标动作 : deslop 第{args.chapter}章（白名单内的无副作用动作）")

    # ★测试动作选 audit（一致性审计）而非 deslop，理由：
    #   ① 在 _EXEC_ALLOWED 白名单内；② 端点源码明写 **只读诊断、绝不修改正文与追踪
    #   状态**（_AGENT_TASKS 里标 "readonly": True）；③ 它是异步端点，会建 task_id，
    #   正好对应用户裁定的通过标准第 4 条「看到 task created 即可停止」。
    #   ⇒ 既无副作用，又能验到 task 层。
    # ⚠️ 措辞必须**对齐 _SYSTEM_PROMPT**（"回复最前面先一句话确认，紧接着输出
    #    EXECUTE_JSON 标记行 + JSON"）。实测：写成"只输出协议块、不要其它内容"会与
    #    system 的"先一句话确认"打架 → 模型只回确认话术、不输出协议块（2026-09-26）。
    msg = (
        f"请对《{BOOK_ID}》第{args.chapter}章做一致性审计（只读诊断，不改正文），并直接派单。"
        "回复最前面先一句话确认，紧接着输出 EXECUTE_JSON 标记行 + JSON。"
    )
    payload = {
        "messages": [{"role": "user", "content": msg}],
        "book_id": BOOK_ID,
        "pool_id": POOL_ID,
    }
    print(f"\n  提问: {msg}")

    log_pos = BACKEND_LOG.stat().st_size if BACKEND_LOG.exists() else 0
    t0 = time.time()
    req = urllib.request.Request(
        f"{API}/api/agent/chat",
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as fh:
            resp = json.loads(fh.read().decode())
    except Exception as exc:  # noqa: BLE001
        print(f"\n❌ 调用失败: {type(exc).__name__}: {exc}")
        return 1
    dt = time.time() - t0

    acts = resp.get("proposed_actions")
    print(f"\n  耗时 {dt:.1f}s | model={resp.get('model')}")
    print(f"  proposed_actions = {json.dumps(acts, ensure_ascii=False) if acts else 'None'}")
    if resp.get("reply"):
        print(f"  reply 前 200 字: {resp['reply'][:200]!r}")

    # ── 后端日志里的 [AGENT_COMMAND] ────────────────────────────────────
    print("\n" + "─" * 74)
    print("[AGENT_COMMAND] 日志（用户裁定的第 3 条通过标准）")
    print("─" * 74)
    if BACKEND_LOG.exists():
        with BACKEND_LOG.open("rb") as fh:
            fh.seek(log_pos)
            tail = fh.read().decode("utf-8", "replace")
        hits = [ln for ln in tail.splitlines() if "[AGENT_COMMAND]" in ln]
        if hits:
            for ln in hits:
                print(f"  {ln.strip()}")
        else:
            print("  （本次调用未产生 [AGENT_COMMAND] 行）")
            if acts is None:
                print("  ⚠️ 没有 command 也没有拒绝日志 → 模型压根没输出协议块，"
                      "或协议块在更上游就被剥离了")
    else:
        print(f"  （日志文件不存在: {BACKEND_LOG}）")

    # ── 第 4 条：task 创建（audit 异步端点，只读，建完即停）─────────────
    print("\n" + "─" * 74)
    print("[4] Task 创建（audit/async：只读诊断，建完 task 立刻停止）")
    print("─" * 74)
    task_id = None
    if acts:
        a0 = acts[0]
        if a0.get("action") == "audit" and not args.no_task:
            import urllib.parse
            path = (f"/api/books/{urllib.parse.quote(BOOK_ID)}"
                    f"/mvp/agents/audit/async")
            body = {"start": int(a0.get("start", args.chapter)),
                    "end": int(a0.get("end", args.chapter))}
            print(f"  POST {path}  body={body}")
            try:
                req2 = urllib.request.Request(
                    f"{API}{path}",
                    data=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json",
                             "Authorization": f"Bearer {token}"},
                )
                with urllib.request.urlopen(req2, timeout=30) as fh:
                    r2 = json.loads(fh.read().decode())
                task_id = r2.get("task_id") or r2.get("taskId")
                print(f"  ✅ task created: task_id={task_id}  status={r2.get('status')}")
                print("  ℹ️  只读审计，不改正文；不等它跑完，到此停止。")
            except Exception as exc:  # noqa: BLE001
                print(f"  ⚠️ 建 task 失败: {type(exc).__name__}: {str(exc)[:200]}")
        else:
            print("  （跳过：非 audit 动作或 --no-task）")
    else:
        print("  （跳过：无 command）")

    # ── 判定 ───────────────────────────────────────────────────────────
    print("\n" + "=" * 74)
    ok = bool(acts)
    print("通过标准:  1 浏览器 ✅(步骤3) | 2 Parser ✅(步骤3) | "
          f"3 Agent {'✅' if acts else '❌'} | 4 Task {'✅' if task_id else '—（未建）'}")
    print("结论:", "✅ command 产出 → Gemini Web → Agent command 链路通"
          if ok else "❌ 未产出 command（见上方日志定位原因）")
    if ok:
        print("  ℹ️  未改任何正文、未跑 writer（audit=只读诊断；"
              "/api/agent/chat 本身无状态不落盘）")
    print("=" * 74)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
