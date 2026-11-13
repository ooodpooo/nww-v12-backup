#!/usr/bin/env python3
"""nww-publish CLI：网文平台（起点/番茄）定时发书助手。

命令
----
  login    手动登录并持久化会话（起点需微信扫码）
  books    列出账号下的书
  plan     扫描书稿文件夹，生成发布队列与计划时间
  status   查看发布进度
  run      发「已到点」的章节（--resume 重发失败项，--once 单轮，--dry-run 模拟）
  daemon   守护循环：到点自动发，受每日上限约束
  calibrate 启动浏览器 dump 候选元素，用于回填选择器
  config   查看/修改配置

任意 IDE 工具（WorkBuddy / Claude Code / Cursor / opencode）均可调用本 CLI，
或加载 skills/nww-publish/SKILL.md 让其理解用法。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .state import PublishState, STATUS_PUBLISHING, STATUS_PUBLISHED, STATUS_FAILED
from .content import read_book
from .scheduler import compute_schedule, parse_start, iso
from .adapters import get_adapter


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="nww-publish", description="网文平台定时发书助手")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("login", help="手动登录并持久化会话")
    sp.add_argument("--platform", required=True, choices=["qidian", "fanqie"])

    sp = sub.add_parser("books", help="列出账号下的书")
    sp.add_argument("--platform", required=True, choices=["qidian", "fanqie"])

    sp = sub.add_parser("plan", help="生成发布队列")
    sp.add_argument("--platform", required=True, choices=["qidian", "fanqie"])
    sp.add_argument("--book", required=True, help="书名/书键（自定义，用于状态归类）")
    sp.add_argument("--src", required=True, help="书稿文件夹（每章一个 .md）")
    sp.add_argument("--book-id", default=None, help="平台书籍 ID（可选，校准后可填）")
    sp.add_argument("--start", default="now", help="起始时间：now | HH:MM | ISO")
    sp.add_argument("--interval", type=int, default=3600, help="章节间隔秒（默认 3600）")
    sp.add_argument("--jitter", type=int, default=None, help="随机抖动秒（默认读 safety.jitter_sec）")

    sp = sub.add_parser("status", help="查看发布进度")
    sp.add_argument("--platform", default=None)
    sp.add_argument("--book", default=None)

    sp = sub.add_parser("run", help="发已到点的章节")
    sp.add_argument("--platform", default=None)
    sp.add_argument("--once", action="store_true", help="只处理一轮即退出（适合 cron）")
    sp.add_argument("--resume", action="store_true", help="同时重发 failed 章节")
    sp.add_argument("--dry-run", action="store_true", help="模拟发布，不启浏览器")

    sp = sub.add_parser("daemon", help="守护循环自动发")
    sp.add_argument("--platform", default=None)
    sp.add_argument("--resume", action="store_true")

    sp = sub.add_parser("calibrate", help="dump 元素以校准选择器")
    sp.add_argument("--platform", required=True, choices=["qidian", "fanqie"])

    sp = sub.add_parser("config", help="查看/修改配置")
    sp.add_argument("--get", default=None)
    sp.add_argument("--set", default=None, help="KEY=VALUE")
    return p


def _print_status(state: PublishState, platform=None, book=None):
    rows = state.all_chapters()
    if platform:
        rows = [r for r in rows if r["platform"] == platform]
    if book:
        rows = [r for r in rows if r["book_key"] == book]
    print(f"\n进度汇总: {state.summary()}")
    print(f"{'状态':<9}{'平台':<7}{'书':<12}{'计划时间':<22}{'标题'}")
    print("-" * 80)
    for r in sorted(rows, key=lambda x: x["scheduled_at"]):
        print(f"{r['status']:<9}{r['platform']:<7}{r['book_key']:<12}{r['scheduled_at']:<22}{r['title'][:30]}")
    print()


async def _publish_one(adapter, page, state: PublishState, ch: dict, safety: dict, draft: bool):
    state.mark(ch["id"], status=STATUS_PUBLISHING, attempts=ch.get("attempts", 0) + 1)
    try:
        body = Path(ch["chapter_file"]).read_text(encoding="utf-8").strip()
    except Exception as e:
        state.mark(ch["id"], status=STATUS_FAILED, error=f"读取正文失败: {e}")
        return
    book = state.get_book(ch["platform"], ch["book_key"])
    res = await adapter.publish_chapter(page, book.get("book_id"), ch["title"], body, draft=draft)
    if res["ok"]:
        state.mark(ch["id"], status=STATUS_PUBLISHED, platform_chapter_id=res.get("chapter_id"), platform_url=res.get("url"), error=None)
        print(f"  ✅ 已发布: {ch['title']}  -> {res.get('url')}")
    else:
        state.mark(ch["id"], status=STATUS_FAILED, error=res.get("error"))
        print(f"  ❌ 失败: {ch['title']}  -> {res.get('error')}")
    wait = safety["min_interval_sec"] * 1000 + random.randint(0, max(0, safety["jitter_sec"]) * 1000)
    await page.wait_for_timeout(wait)


async def _run_pipeline(cfg: Config, state: PublishState, platform=None, resume=False, dry_run=False):
    due = state.due_chapters(include_failed=resume)
    if platform:
        due = [c for c in due if c["platform"] == platform]
    if not due:
        print("[run] 当前没有待发（已到点）的章节。")
        return
    safety = cfg.safety
    draft = safety.get("publish_mode") == "draft"

    if dry_run:
        print("[run] dry-run 模式：不启浏览器，模拟发布。")
        for ch in due:
            if state.daily_count() >= safety["max_chapters_per_day"]:
                print("  ⏸ 已达今日上限，余下顺延至次日。")
                break
            state.mark(ch["id"], status=STATUS_PUBLISHED, platform_chapter_id="DRY-" + ch["id"], platform_url="dry-run://")
            print(f"  🟡 模拟发布: {ch['title']}")
        return

    # 按平台分组，各自开一个持久会话
    groups: dict[str, list] = {}
    for ch in due:
        groups.setdefault(ch["platform"], []).append(ch)
    for plat, chs in groups.items():
        adapter = get_adapter(plat, cfg.platform(plat), cfg.home)
        async with adapter as a:
            page = await a.new_page()
            await a.ensure_session(page)
            for ch in chs:
                if state.daily_count() >= safety["max_chapters_per_day"]:
                    print("  ⏸ 已达今日上限，余下顺延至次日（daemon 会自动续发）。")
                    break
                await _publish_one(a, page, state, ch, safety, draft)


async def _daemon(cfg: Config, state: PublishState, platform=None, resume=False):
    print("[daemon] 启动守护循环（Ctrl+C 退出）…")
    try:
        while True:
            due = state.due_chapters(include_failed=resume)
            if platform:
                due = [c for c in due if c["platform"] == platform]
            if due and state.daily_count() < cfg.safety["max_chapters_per_day"]:
                await _run_pipeline(cfg, state, platform=platform, resume=resume, dry_run=False)
            else:
                await asyncio.sleep(60)
    except KeyboardInterrupt:
        print("\n[daemon] 已停止。")


def main(argv=None):
    args = _build_parser().parse_args(argv)
    cfg = Config()
    state = PublishState(cfg.home)

    if args.cmd == "config":
        if args.get:
            keys = args.get.split(".")
            node = cfg.data
            for k in keys:
                node = node[k]
            print(json.dumps(node, ensure_ascii=False, indent=2) if isinstance(node, (dict, list)) else node)
        elif args.set:
            k, v = args.set.split("=", 1)
            try:
                vv = int(v)
            except ValueError:
                try:
                    vv = float(v)
                except ValueError:
                    vv = v
            cfg.set(k, vv)
            print(f"已设置 {k} = {vv}")
        else:
            print(json.dumps(cfg.data, ensure_ascii=False, indent=2))
        return

    if args.cmd == "login":
        async def _login():
            adapter = get_adapter(args.platform, cfg.platform(args.platform), cfg.home)
            async with adapter as a:
                page = await a.new_page()
                ok = await a.ensure_session(page, manual_ack=True)
                print(f"[{args.platform}] 登录态: {'已就绪' if ok else '未确认'}")
        asyncio.run(_login())
        return

    if args.cmd == "books":
        async def _books():
            adapter = get_adapter(args.platform, cfg.platform(args.platform), cfg.home)
            async with adapter as a:
                page = await a.new_page()
                await a.ensure_session(page, manual_ack=True)
                links = await a.list_books(page)
                print(f"[{args.platform}] 疑似书籍链接（校准后可精确化）：")
                for l in links:
                    print(f"  • {l.get('text')}  ->  {l.get('href')}")
        asyncio.run(_books())
        return

    if args.cmd == "calibrate":
        async def _cal():
            adapter = get_adapter(args.platform, cfg.platform(args.platform), cfg.home)
            async with adapter as a:
                page = await a.new_page()
                await a.calibrate(page, cfg.platform(args.platform).get("new_chapter_url"))
        asyncio.run(_cal())
        return

    if args.cmd == "plan":
        src = Path(args.src).expanduser().resolve()
        chapters = read_book(src, ext=cfg.data["content"]["chapter_ext"])
        if not chapters:
            print(f"[plan] 未在 {src} 找到 .md 章节。")
            return
        start = parse_start(args.start)
        jitter = args.jitter if args.jitter is not None else cfg.safety["jitter_sec"]
        times = compute_schedule(len(chapters), start, args.interval, jitter)
        if args.book_id:
            state.set_book_id(args.platform, args.book, args.book_id)
        for ch, t in zip(chapters, times):
            state.add_chapter(args.platform, args.book, str(ch.abs_path), ch.title, iso(t))
        print(f"[plan] 已规划 {len(chapters)} 章，起始 {iso(start)}，间隔 {args.interval}s，抖动 {jitter}s")
        _print_status(state, platform=args.platform, book=args.book)
        return

    if args.cmd == "status":
        _print_status(state, platform=args.platform, book=args.book)
        return

    if args.cmd == "run":
        asyncio.run(_run_pipeline(cfg, state, platform=args.platform, resume=args.resume, dry_run=args.dry_run or cfg.safety["dry_run_default"]))
        return

    if args.cmd == "daemon":
        asyncio.run(_daemon(cfg, state, platform=args.platform, resume=args.resume))
        return


if __name__ == "__main__":
    main()
