#!/usr/bin/env python3
"""清理 chrome_bridge 泄漏的 AI Studio 标签页。

背景：会话池的 targetId 曾只存在内存里，代理每次重启都会丢失记录并新开标签页，
历史页成了孤儿（实测累积到 24 个）。现已改为持久化到
~/.nww-local/bridge_sessions.json，本脚本用于回收**修复之前**已经泄漏的存量。

安全约定：
  - 默认 **dry-run**：只列出会关哪些、保留哪些，不动浏览器。
  - 必须显式加 --apply 才真正关闭。
  - 只处理 aistudio.google.com 的标签页，其它站点一律不碰。
  - 会话池里登记过的 targetId 一律保留，即使它当前不在活动页里。

用法：
    python scripts/bridge_cleanup_tabs.py                 # 只列
    python scripts/bridge_cleanup_tabs.py --apply         # 真的关
    python scripts/bridge_cleanup_tabs.py --apply --keep 3  # 保留最近 3 个（按页序末尾）
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# 本机开了系统级 HTTP 代理（127.0.0.1:7897），它会把 localhost 请求也送去网关 -> 502。
# 桥接走的是 127.0.0.1:9222 的 CDP，必须显式豁免。
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost")

CDP = os.environ.get("NWW_CDP_PORT", "9222")
STORE = os.path.expanduser("~/.nww-local/bridge_sessions.json")


def load_pool() -> dict[str, str]:
    try:
        with open(STORE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — 文件不存在/损坏都按空池处理
        return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的关闭（默认只列不改）")
    ap.add_argument("--keep", type=int, default=0,
                    help="除会话池外，额外保留最近 N 个标签页（按页序末尾计）")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("缺少 playwright：用 ~/.workbuddy/binaries/python/envs/default/bin/python 运行",
              file=sys.stderr)
        return 2

    pool = load_pool()
    keep_ids = set(pool.values())
    print(f"会话池登记 targetId（必保留）: {len(keep_ids)} 个")
    for k, v in pool.items():
        print(f"  - {k}: {v}")

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP}")
        except Exception as exc:  # noqa: BLE001
            print(f"连不上 CDP {CDP}（先跑 ./launch_chrome_debug.sh）: {exc}", file=sys.stderr)
            return 2

        pages: list[tuple[str, str, object, object]] = []  # (targetId, url, page, ctx)
        for ctx in browser.contexts:
            for pg in ctx.pages:
                try:
                    url = pg.url or ""
                except Exception:  # noqa: BLE001
                    continue
                if "aistudio" not in url:
                    continue
                tid = ""
                try:
                    sess = ctx.new_cdp_session(pg)
                    tid = sess.send("Target.getTargetInfo", {})["targetInfo"]["targetId"]
                except Exception:  # noqa: BLE001
                    pass
                pages.append((tid, url, pg, ctx))

        print(f"\nAI Studio 标签页共 {len(pages)} 个")

        # --keep：按页序保留末尾 N 个（越靠后越可能是最近在用的）
        extra_keep: set[str] = set()
        if args.keep > 0:
            for tid, _u, _pg, _c in pages[-args.keep:]:
                if tid:
                    extra_keep.add(tid)

        to_close: list[tuple[str, str, object]] = []
        kept = 0
        for tid, url, pg, _ctx in pages:
            if tid and (tid in keep_ids or tid in extra_keep):
                kept += 1
                continue
            to_close.append((tid, url, pg))

        print(f"保留 {kept} 个 / 待关闭 {len(to_close)} 个")
        for tid, url, _pg in to_close:
            print(f"  x {tid or '(无id)'}  {url[:80]}")

        if not args.apply:
            print("\n[dry-run] 未做任何改动。确认无误后加 --apply 执行。")
            return 0
        if not to_close:
            print("\n没有需要清理的标签页。")
            return 0

        closed = 0
        for tid, url, pg in to_close:
            try:
                pg.close()
                closed += 1
            except Exception as exc:  # noqa: BLE001
                print(f"  关闭失败 {url[:60]}: {type(exc).__name__}: {exc}")
        print(f"\n已关闭 {closed}/{len(to_close)} 个标签页。")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
