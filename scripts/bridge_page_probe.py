#!/usr/bin/env python3
"""chrome_bridge 页面体检（只读，不点击/不输入/不消耗额度）。

为什么要有它：桥接报错（「已入列但未开始生成」「生成超时」「答非所问」）绝大多数不是
代码坏了，而是 AI Studio 页面处于某个状态。肉眼盯着看只能看到"有/没有字"，看不到
turn 数、正文节点数、Run 按钮 label 这些真正决定判定的信号。

⚠️⚠️ 铁律：**绝不要**对 connect_over_cdp 得到的 browser 调用 close()。
   Playwright 对连进来的 Chromium 调 close() 会真的把页面关掉（实测：一次体检后
   AI Studio 专用标签页消失、6 页变 5 页）。本脚本只做 evaluate/locate，不持有、
   不关闭任何东西。

用法：
    NO_PROXY='127.0.0.1,localhost' /opt/miniconda3/bin/python scripts/bridge_page_probe.py
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost")

CDP_PORT = int(os.environ.get("NWW_CHROME_CDP_PORT", "9222"))

# 与 chrome_bridge._RUN_SELECTORS 保持同源；一致才有诊断价值
_RUN_SELECTORS = (
    "ms-run-button button",
    "button[aria-label='Run' i]",
    "button[aria-label='Run prompt' i]",
    "button[aria-label*='Send' i]",
    "run-button button",
)

_AI_STUDIO_JS = """(sels) => {
  const turns = Array.from(document.querySelectorAll('ms-chat-turn'));
  const dump = turns.slice(-6).map((t, i) => {
    const idx = turns.length - Math.min(6, turns.length) + i;
    const txt = (t.innerText || '').trim();
    return {
      idx,
      len: txt.length,
      nodes: t.querySelectorAll('*').length,
      thoughtHost: !!t.querySelector('thought-activity-host,ms-thought-activity'),
      head: txt.slice(0, 100).replace(/\\s+/g, ' '),
      tail: txt.slice(-100).replace(/\\s+/g, ' '),
    };
  });
  const buttons = [];
  for (const sel of sels) {
    const arr = Array.from(document.querySelectorAll(sel));
    if (!arr.length) continue;
    for (const b of arr) {
      buttons.push({
        sel,
        aria: b.getAttribute('aria-label'),
        text: (b.innerText || '').trim().slice(0, 40).replace(/\\s+/g, ' '),
        disabled: !!b.disabled,
      });
    }
    break;  // 命中首个存在的候选即定论（与 _run_ready 同策略）
  }
  const body = (document.body.innerText || '');
  return {
    turnCount: turns.length,
    turns: dump,
    buttons,
    spinner: !!document.querySelector('mat-spinner, ms-progress-spinner, [class*=spinner]'),
    bodyTail: body.slice(-160).replace(/\\s+/g, ' '),
    errWords: ['internal error', 'error has occurred', 'rate limit', 'quota',
               'something went wrong', 'too many requests']
        .filter(w => body.toLowerCase().includes(w)),
  };
}"""

_GEMINI_JS = """() => {
  const msgs = Array.from(document.querySelectorAll('message-content, [class*="message-content"]'))
      .map((m, i) => ({i, len: (m.innerText || '').trim().length,
                       head: (m.innerText || '').trim().slice(0, 120).replace(/\\s+/g, ' ')}));
  const body = document.body.innerText || '';
  return {
    title: document.title, url: location.href, bodyLen: body.length,
    msgCount: msgs.length, msgs: msgs.slice(-6),
    bodyTail: body.slice(-200).replace(/\\s+/g, ' '),
    errWords: ['something went wrong', '出了点问题', '停止生成', '无法回答']
        .filter(w => body.toLowerCase().includes(w.toLowerCase())),
  };
}"""


def main() -> int:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        br = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
        found = 0
        for ctx in br.contexts:
            for pg in ctx.pages:
                url = pg.url or ""
                if "aistudio.google.com" in url:
                    found += 1
                    print("=" * 72)
                    print("[AI Studio]", url[:100])
                    info = pg.evaluate(_AI_STUDIO_JS, list(_RUN_SELECTORS))
                    print("  turn 总数:", info["turnCount"], "| spinner:", info["spinner"])
                    print("  Run 按钮（首个命中选择器）:", json.dumps(info["buttons"], ensure_ascii=False))
                    print("  body 末尾:", info["bodyTail"])
                    print("  错误关键词:", info["errWords"] or "无")
                    for t in info["turns"]:
                        print(f"  turn[{t['idx']}] len={t['len']:>6} nodes={t['nodes']:>5} "
                              f"thoughtHost={int(t['thoughtHost'])}")
                        print(f"      head: {t['head']}")
                        print(f"      tail: {t['tail']}")
                elif "gemini.google.com" in url:
                    found += 1
                    print("=" * 72)
                    info = pg.evaluate(_GEMINI_JS)
                    print("[Gemini App]", info["title"], "|", url[:80])
                    print("  body 长度:", info["bodyLen"], "| 消息块:", info["msgCount"])
                    for m in info["msgs"]:
                        print(f"   msg[{m['i']}] len={m['len']:>6} :: {m['head']}")
                    print("  错误关键词:", info["errWords"] or "无")
                    print("  body 末尾:", info["bodyTail"])
        if not found:
            print("❌ 既没有 AI Studio 页，也没有 Gemini 页。"
                  "（若刚做过体检，检查是否被误关；桥接下次调用会自行重建）")
        # 不调 br.close()：那会关掉连进来的真实页面
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
