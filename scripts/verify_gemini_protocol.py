#!/usr/bin/env python3
"""Gemini Web EXECUTE_JSON 最小闭环验证（真实浏览器，2026-09-26）。

范围（用户裁定）：**只验证入口通路的协议闭环**，不跑 rewrite、不碰 pipeline、
不改 prompt 体系、不验证 AI Studio failover —— 三个变量一起动就无法定位。

验证三层：
  1. 浏览器层：页面 DOM 里 `EXECUTE_JSON` 标记是否存活（vs 只有 JSON 载荷）。
     根因背景：gemini.google.com 的 renderer 会把 `<execute>` 当 HTML 标签 sanitize 掉，
     DOM 里连元素节点都不剩 → innerText/textContent 都取不到标签名。
  2. Parser 层：`_extract_marked_json` 能否从真实文本里抽出 JSON 载荷。
  3. Agent 层：解析结果是否产出 command（而非当普通 assistant_message）。

⚠️⚠️ 铁律：**绝不要**对 connect_over_cdp 得到的 browser 调用 close()。
   Playwright 对连进来的 Chromium 调 close() 会真的关掉页面（实测事故：6 页变 5 页）。

用法：
    /opt/miniconda3/bin/python scripts/verify_gemini_protocol.py
    /opt/miniconda3/bin/python scripts/verify_gemini_protocol.py --timeout 180
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "web" / "backend"))

os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost")

# 固定测试提问（用户裁定原文）
TEST_PROMPT = """请执行协议测试。
只输出：
EXECUTE_JSON:
{
"action":"protocol_test",
"value":"gemini_web"
}"""

# 白名单动作（仅解析、不执行 —— _parse_execute_block 只返回 command，不触发跑链）
ALLOWLIST_PROMPT = """请执行协议测试。
只输出：
EXECUTE_JSON
[{"action":"deslop","chapter":25,"note":"协议连通性测试，无需实际改动"}]"""

BOOK = ROOT / "projects" / "admin" / "用泡面征服修仙界"


def _dom_probe() -> dict:
    """只读体检：抓页面上最后一个 model-response，对比 textContent / innerText。"""
    from playwright.sync_api import sync_playwright

    js = """() => {
      const rs = Array.from(document.querySelectorAll('model-response'));
      if (!rs.length) return {found: 0};
      const r = rs[rs.length - 1];
      const md = r.querySelector('.markdown, .markdown-main-panel') || r;
      return {
        found: rs.length,
        textContent: (md.textContent || '').slice(0, 600),
        innerText:   (md.innerText   || '').slice(0, 600),
        innerHTML:   (md.innerHTML   || '').slice(0, 900),
        tagCount: r.querySelectorAll('*').length,
      };
    }"""
    port = int(os.environ.get("NWW_CHROME_CDP_PORT", "9222"))
    out: dict = {}
    with sync_playwright() as pw:
        br = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        for ctx in br.contexts:
            for pg in ctx.pages:
                if "gemini.google.com" not in (pg.url or ""):
                    continue
                try:
                    info = pg.evaluate(js)
                except Exception as exc:  # noqa: BLE001
                    continue
                if not info.get("found"):
                    continue
                out[f"tab::{pg.url[-24:]}"] = info
        # 不调 br.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=150.0)
    args = ap.parse_args()

    import app.chrome_bridge as cb
    from app.agent import (
        _M_EXECUTE, _extract_marked_json, _parse_execute_block,
    )

    print("=" * 74)
    print("Gemini Web · EXECUTE_JSON 最小闭环验证")
    print("=" * 74)
    print(f"默认站点序列: {cb._SITE_ORDER} | 活跃站点: {cb._ACTIVE_SITE_KEY}")
    print(f"提问 ({len(TEST_PROMPT)} 字符):")
    for line in TEST_PROMPT.splitlines():
        print(f"    {line}")

    # ── 第 1 步：真实发问 ────────────────────────────────────────────────
    print("\n" + "─" * 74)
    print("[1] 浏览器层：经 chrome_bridge 发问，取回原始文本")
    print("─" * 74)
    try:
        raw = cb.chat("", TEST_PROMPT, "gemini-web", timeout=args.timeout,
                      session_id="protocol_test")
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 桥接失败: {type(exc).__name__}: {exc}")
        return 1

    print(f"取回 {len(raw)} 字符。原始文本（repr，看真实转义）:")
    print("    " + repr(raw[:500]))
    print("\n原文渲染:")
    for line in raw.splitlines():
        print(f"    {line}")

    marker_alive = "EXECUTE_JSON" in raw
    brace_alive = "{" in raw
    print(f"\n  标记 EXECUTE_JSON 存活 : {marker_alive}")
    print(f"  载荷 {{...}}      存活 : {brace_alive}")
    if marker_alive and not brace_alive:
        print("  ⚠️ 只有标记没有载荷 → 模型没按格式输出（或被截断）")
    if brace_alive and not marker_alive:
        print("  ❌ 标记被 renderer 吞掉了 —— 与 <execute> 同型，协议必须再改")

    # ── 第 1b 步：DOM 层对比 ─────────────────────────────────────────────
    print("\n  -- DOM 取值对比（textContent vs innerText）--")
    try:
        doms = _dom_probe()
        if not doms:
            print("  （未找到含 model-response 的 Gemini 页，跳过 DOM 对比）")
        for name, info in doms.items():
            tc = info.get("textContent", "")
            it = info.get("innerText", "")
            print(f"  {name}: nodes={info.get('tagCount')}")
            print(f"    textContent 含标记: {'EXECUTE_JSON' in tc}")
            print(f"    innerText   含标记: {'EXECUTE_JSON' in it}")
            if "EXECUTE_JSON" in (info.get("innerHTML") or ""):
                print("    innerHTML   含标记: True")
    except Exception as exc:  # noqa: BLE001
        print(f"  （DOM 体检跳过: {exc}）")

    # ── 第 2 步：Parser 层 ───────────────────────────────────────────────
    print("\n" + "─" * 74)
    print("[2] Parser 层：_extract_marked_json 抽取")
    print("─" * 74)
    payload, clean = _extract_marked_json(raw, _M_EXECUTE)
    print(f"  抽出载荷: {payload!r}")
    print(f"  剥离后正文: {clean[:200]!r}")
    parsed = None
    if payload:
        try:
            parsed = json.loads(payload)
            print(f"  ✅ json.loads 成功: {parsed}")
        except json.JSONDecodeError as exc:
            print(f"  ❌ json.loads 失败: {exc}")
    else:
        print("  ❌ 未抽出载荷")

    # 旧尖括号形态对照（证明双形态都在）
    from app.agent import _EXEC_RE
    legacy = '好的。\n<execute>\n[{"action":"deslop","chapter":25}]\n</execute>\n完成。'
    lp, _ = _extract_marked_json(legacy, _M_EXECUTE)
    has_legacy = bool(_EXEC_RE.search(legacy))
    print(f"  旧 <execute> 形态仍在正则里: {has_legacy}（AI Studio/直连池仍可用）")

    # ── 第 3 步：Agent 层 ────────────────────────────────────────────────
    print("\n" + "─" * 74)
    print("[3] Agent 层：_parse_execute_block → command")
    print("─" * 74)
    if BOOK.is_dir():
        _c, acts = _parse_execute_block(BOOK, raw)
        print(f"  protocol_test 动作 → 产出 command: {acts}")
        if not acts:
            print("  ℹ️  预期行为：protocol_test 不在动作白名单"
                  "（write/rewrite/deslop/revise）→ fail-closed 丢弃，这是设计不是故障。")
        # 白名单动作对照（仅解析，不执行）
        _c2, acts2 = _parse_execute_block(
            BOOK, '确认。\nEXECUTE_JSON\n[{"action":"deslop","chapter":25}]')
        print(f"  白名单动作 deslop → 产出 command: {acts2}")
        print(f"  ✅ 能进 command 通道" if acts2 else "  ❌ 白名单动作也没产出 command")
    else:
        print(f"  （跳过：书目录不存在 {BOOK}）")

    # ── 结论 ────────────────────────────────────────────────────────────
    print("\n" + "=" * 74)
    ok = marker_alive and parsed is not None
    print("结论:", "✅ 浏览器层标记存活 + Parser 层解析成功 → 协议闭环成立"
          if ok else "❌ 协议闭环未成立（见上方各层判据）")
    print("=" * 74)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
