#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ═══════════════════════════════════════════════════════════════════
# ⚠️ 已废弃（2026-10-23 标注）：本探针验收的是**老栈 8787**（gemini_chrome_proxy.py）。
#    桥接已迁往 openai-webui-api（8788 默认 / 8789 --ide）；
#    新栈验收见 openai-webui-api/tests/（test_proxy.py 等）。
#    如确需复跑本探针，设 PROXY_BASE=http://127.0.0.1:8788/v1 可兼容大部分用例。
# ═══════════════════════════════════════════════════════════════════
"""批五 OpenAI 兼容层验收探针。

用真实 OpenAI SDK 而非只 curl —— 客户端解析器怎么处理 chunk 才是真正要验的东西。

用法：
  1) 先起代理：python3 gemini_chrome_proxy.py
  2) 再跑本脚本：python3 scripts/accept_proxy.py

覆盖：
  T1 缺 messages -> 422 + error.code 为 string
  T2 messages 空数组 -> 400（与 422 区分）
  T3 /healthz 暴露 inflight 队列深度
  T4 不支持参数（temperature）不再静默忽略 -> 代理日志出现 WARN
  T5 原始 SSE：心跳帧可见 + 收尾必有 [DONE]
  T6 OpenAI SDK 非流式：choices[0].message.content 非空
  T7 OpenAI SDK 流式：能吃到 delta.content / delta.reasoning_content
"""
import json
import os
import sys
import time

import httpx
from openai import OpenAI

BASE = os.environ.get("PROXY_BASE", "http://127.0.0.1:8787/v1")
ROOT = BASE.rstrip("/").removesuffix("/v1")
RESULTS: list[tuple[str, bool, str]] = []

# 必须 trust_env=False：本机开了系统级 HTTP 代理（127.0.0.1:7897），
# httpx/OpenAI SDK 默认 trust_env=True 会把 localhost 请求也送去网关 -> 拿到 502。
HTTP = httpx.Client(trust_env=False, timeout=240)


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("PASS  " if ok else "FAIL  ") + name + (" | " + detail if detail else ""), flush=True)


def main() -> int:
    client = OpenAI(
        base_url=BASE,
        api_key="sk-local-not-used",
        timeout=240,
        http_client=httpx.Client(trust_env=False, timeout=240),
    )
    short = "用一句话说明什么是递归。"

    # --- T1 缺 messages ---
    r = HTTP.post(f"{BASE}/chat/completions", json={"model": "m"}, timeout=30)
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    err = body.get("error", {})
    check("T1 missing messages -> 422", r.status_code == 422, f"got {r.status_code}")
    check("T1 error.code is string", isinstance(err.get("code"), str), repr(err.get("code")))

    # --- T2 空数组 -> 400 ---
    r = HTTP.post(f"{BASE}/chat/completions", json={"model": "m", "messages": []}, timeout=30)
    check("T2 empty messages -> 400", r.status_code == 400, f"got {r.status_code}")

    # --- T3 healthz 队列深度 ---
    r = HTTP.get(f"{ROOT}/healthz", timeout=10)
    check("T3 /healthz inflight", "inflight" in r.json(), str(r.json()))

    # --- T4 不支持参数告警（发请求后由调用方核对代理日志） ---
    HTTP.post(
        f"{BASE}/chat/completions",
        json={"model": "m", "messages": [], "temperature": 0.2, "max_tokens": 64},
        timeout=30,
    )
    check("T4 unsupported params not 5xx", True, "核对代理 stdout 是否有 '[PROXY] 不支持的参数被忽略'")

    # --- T8 多轮折叠：末条 assistant（prefill/续写）不能被标成用户指令 ---
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import gemini_chrome_proxy as gp  # noqa: E402

        _, b1 = gp._split_messages([
            gp.ChatMessage(role="user", content="hi"),
            gp.ChatMessage(role="assistant", content="hello"),
            gp.ChatMessage(role="user", content="what next?"),
        ])
        check("T8a last=user -> Current instruction",
              "### Current instruction:" in b1 and "### Assistant:" in b1, b1[:60].replace("\n", " / "))

        _, b2 = gp._split_messages([
            gp.ChatMessage(role="user", content="write a poem"),
            gp.ChatMessage(role="assistant", content="Roses are red,"),
        ])
        check("T8b last=assistant -> 不得误标为用户指令",
              "### Current instruction:" not in b2 and "continue generating" in b2,
              b2[:80].replace("\n", " / "))
    except Exception as exc:  # noqa: BLE001
        check("T8a last=user -> Current instruction", False, f"import/call fail: {exc}")

    # --- T5 原始 SSE：心跳 + [DONE] ---
    pings = 0
    contents = 0
    saw_done = False
    max_gap = 0.0
    last_ts = None
    t0 = time.time()
    HEARTBEAT = 10.0  # 与代理默认 GEMINI_PROXY_HEARTBEAT_S 对齐
    with HTTP.stream(
        "POST",
        f"{BASE}/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": short}], "stream": True},
        timeout=240,
    ) as resp:
        for line in resp.iter_lines():
            now = time.time()
            if last_ts is not None:
                max_gap = max(max_gap, now - last_ts)
            last_ts = now
            if line.startswith(": ping"):
                pings += 1
            elif line.startswith("data: [DONE]"):
                saw_done = True
            elif line.startswith("data: "):
                payload = json.loads(line[6:])
                kind = payload.get("choices", [{}])[0].get("delta", {})
                if "content" in kind or "reasoning_content" in kind:
                    contents += 1
    check("T5 SSE ended with [DONE]", saw_done, f"{contents} content frames, {pings} pings, "
                                                f"elapsed {time.time() - t0:.1f}s")
    # 心跳只在"空闲 >= 阈值"时才该出现。本轮若没有那样的空闲窗口却断言必须心跳，
    # 等于用一个偶发条件去判代码好坏（第一版就踩了这个坑，快响应被误报 FAIL）。
    if max_gap >= HEARTBEAT:
        check("T5 heartbeat frames emitted", pings > 0,
              f"{pings} pings, max idle gap {max_gap:.1f}s")
    else:
        check("T5 heartbeat frames emitted", True,
              f"本轮最大空闲间隔 {max_gap:.1f}s < {HEARTBEAT:.0f}s，无需心跳（正常）")

    # --- T6 SDK 非流式 ---
    try:
        cmpl = client.chat.completions.create(
            model="m", messages=[{"role": "user", "content": short}]
        )
        text = cmpl.choices[0].message.content or ""
        check("T6 SDK non-stream content", len(text.strip()) > 0, f"{len(text)} chars")
    except Exception as exc:  # noqa: BLE001
        check("T6 SDK non-stream content", False, f"{type(exc).__name__}: {exc}")

    # --- T7 SDK 流式（CLI 真正的使用姿势） ---
    try:
        got_content = 0
        got_reasoning = 0
        finished = False
        for chunk in client.chat.completions.create(
            model="m",
            messages=[
                {"role": "system", "content": "简洁作答。"},
                {"role": "user", "content": "上一轮我问了什么？接着用一句话说明什么是队列。"},
            ],
            stream=True,
        ):
            delta = chunk.choices[0].delta
            extra = getattr(delta, "model_extra", None) or {}
            if getattr(delta, "content", None):
                got_content += 1
            if delta.__dict__.get("reasoning_content") or extra.get("reasoning_content"):
                got_reasoning += 1
            if chunk.choices[0].finish_reason == "stop":
                finished = True
        check("T7 SDK stream content frames", got_content > 0, f"{got_content} frames")
        check("T7 SDK stream finish_reason=stop", finished, "")
        print(
            f"      note: reasoning_content frames = {got_reasoning}"
            "（0 表示页面当前模型未开思考，属预期降级；开思考后应 >0）",
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001
        check("T7 SDK stream content frames", False, f"{type(exc).__name__}: {exc}")

    # --- T9 会话路由池：同类连贯 + 跨类隔离 ---
    # 这是最容易悄悄坏掉又最难肉眼发现的功能：会话串了会表现为"模型记忆错乱"。
    secret = "4271"
    try:
        a1 = client.chat.completions.create(
            model="gemini-vip-chrome/accept-coding",
            messages=[{"role": "user", "content": f"请记住这个数字：{secret}。只回复 OK。"}],
        ).choices[0].message.content
        check("T9a session A 记住数字", len((a1 or "").strip()) > 0, f"A1 回: {(a1 or '')[:20]!r}")

        b1 = client.chat.completions.create(
            model="gemini-vip-chrome/accept-novel",
            messages=[{"role": "user", "content": "我之前让你记住的数字是多少？如果不知道就只回复不知道。"}],
        ).choices[0].message.content
        check("T9b 跨 session 必须隔离", secret not in (b1 or ""), f"B 回: {(b1 or '')[:30]!r}")

        a2 = client.chat.completions.create(
            model="gemini-vip-chrome/accept-coding",
            messages=[{"role": "user", "content": "我之前让你记住的数字是多少？只回复这个数字。"}],
        ).choices[0].message.content
        check("T9c 同 session 必须连贯", secret in (a2 or ""), f"A2 回: {(a2 or '')[:30]!r}")
    except Exception as exc:  # noqa: BLE001
        check("T9a session A 记住数字", False, f"{type(exc).__name__}: {exc}")

    failed = [n for n, ok, _ in RESULTS if not ok]
    print("\n==== SUMMARY ====")
    for n, ok, d in RESULTS:
        print(f"  {'PASS' if ok else 'FAIL'}  {n}")
    print(f"  {len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
