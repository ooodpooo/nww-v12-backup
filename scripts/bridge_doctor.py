#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════════
# ⚠️ 已废弃（2026-10-23 标注）：本体检针对**老栈 8787**（gemini_chrome_proxy.py），
#    桥接已迁往 openai-webui-api（8788 默认 / 8789 --ide）。
#    排障请用新栈：`cd openai-webui-api && python -m chrome_llm_bridge.doctor`
#    （出口部分另见 `python -m chrome_llm_bridge.egress`，端口不写死）。
#    保留仅因历史引用；本文件默认查的 7897/8787 在当前机器上均可能已不存在。
# ═══════════════════════════════════════════════════════════════════
"""chrome_bridge 体检：按顺序检查桥接依赖的四个外部条件。

为什么要这个脚本：桥接报的错（"内部错误"/"未找到输入框"/"生成超时"）几乎都
不是桥接代码坏了，而是下面某一层外部环境掉了。按依赖顺序查，别一上来就改代码。

  1) Clash Verge（系统代理 127.0.0.1:7897）—— 外网出口
  2) Chrome CDP 调试端口 9222         —— 桥接控制通道
  3) Chrome 里 AI Studio 的登录态      —— VIP 会话
  4) 代理 8787 / NWW 后端 8010         —— 本地服务

用法：
    python scripts/bridge_doctor.py
退出码 0 = 全部通过；非 0 = 有未通过项（按输出提示修复）。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys

# 本机开了系统级 HTTP 代理（127.0.0.1:7897），它会把 localhost 请求也送去网关 -> 502。
# 桥接走的是 127.0.0.1:9222 的 CDP，必须显式豁免。
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost")

CDP_PORT = int(os.environ.get("NWW_CHROME_CDP_PORT", "9222"))
PROXY_PORT = int(os.environ.get("GEMINI_PROXY_PORT", "8787"))
VPN_PORT = int(os.environ.get("NWW_VPN_PORT", "7897"))
BACKEND_PORT = int(os.environ.get("NWW_BACKEND_PORT", "8010"))

OK = "✅"
BAD = "❌"
WARN = "⚠️ "

RESULTS: list[bool] = []


def _listen(port: int) -> list[str]:
    """返回在 port 上 LISTEN 的进程名列表（不含端口占用者之外的连接）。"""
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpn"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception:  # noqa: BLE001
        return []
    procs, cur = [], ""
    for line in out.splitlines():
        if line.startswith("p"):
            cur = line[1:]
        elif line.startswith("n") and cur:
            procs.append(cur)
    names = []
    for pid in dict.fromkeys(procs):
        try:
            names.append(subprocess.run(
                ["ps", "-p", pid, "-o", "comm="], capture_output=True, text=True, timeout=5
            ).stdout.strip())
        except Exception:  # noqa: BLE001
            pass
    return names


def _http_code(url: str, proxy: str | None = None, timeout: int = 12) -> int:
    cmd = ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", str(timeout)]
    if proxy:
        cmd += ["-x", proxy]
    cmd.append(url)
    try:
        return int(subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5).stdout.strip() or 0)
    except Exception:  # noqa: BLE001
        return 0


def check_1_vpn() -> bool:
    print(f"\n【1】外网出口：系统代理 127.0.0.1:{VPN_PORT}")
    procs = _listen(VPN_PORT)
    if not procs:
        print(f"  {BAD} {VPN_PORT} 无人 LISTEN —— Clash Verge 没运行，所有外网 HTTPS 全废")
        print("     修复：open -a \"Clash Verge\"")
        return False
    print(f"  {OK} 监听进程: {', '.join(procs)}")
    if not any("verge" in p.lower() or "clash" in p.lower() or "mihomo" in p.lower() for p in procs):
        print(f"  {WARN} 占用者不像代理软件（可能是微信等），需确认")
    code = _http_code("https://aistudio.google.com/", proxy=f"http://127.0.0.1:{VPN_PORT}")
    if code in (200, 301, 302):
        print(f"  {OK} 经代理访问 aistudio -> {code}")
        return True
    print(f"  {BAD} 经代理访问 aistudio -> {code}（代理在但上游不通，检查节点/订阅）")
    return False


def check_2_cdp() -> bool:
    print(f"\n【2】桥接控制通道：Chrome CDP {CDP_PORT}")
    if not _listen(CDP_PORT):
        print(f"  {BAD} {CDP_PORT} 未监听 —— Chrome 没带调试端口启动")
        print("     修复：先完全退出 Chrome，再 ./launch_chrome_debug.sh")
        print("     注意：Chrome 152+ 需要 --user-data-dir 且不能是默认目录")
        return False
    print(f"  {OK} {CDP_PORT} 已监听")
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=5) as r:
            info = json.load(r)
        print(f"  {OK} 浏览器: {info.get('Browser')}")
    except Exception as exc:  # noqa: BLE001
        print(f"  {WARN} 读取版本失败: {type(exc).__name__}")
    return True


def check_3_login() -> bool:
    print("\n【3】AI Studio 登录态（VIP）")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(f"  {WARN} 缺少 playwright，跳过（用托管 python 运行本脚本）")
        return True
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
            ctx = browser.contexts[0]
            pg = ctx.new_page()
            try:
                # 判据用 /prompts/new_chat：已登录会停留在该页，未登录会被踢到
                # /welcome（落地页显示 "login / Get started"）。
                # ⚠️ 不要只判断"URL 里有没有 signin" —— 未登录时访问
                # https://aistudio.google.com/ 会停在 /welcome 而不跳转，
                # 只看 URL 会误判为已登录（踩过）。
                pg.goto("https://aistudio.google.com/prompts/new_chat",
                        wait_until="domcontentloaded", timeout=60000)
                pg.wait_for_timeout(7000)
                url = pg.url or ""
                if "accounts.google.com" in url or "/welcome" in url:
                    print(f"  {BAD} 未登录（被重定向到 {url[:70]}）")
                    print("     修复：在该 Chrome 窗口里登录一次 Google 账号（一次性，之后持久）")
                    print("     注：复制原 profile 无效 —— 新版 Chrome cookie 与设备绑定")
                    return False
                print(f"  {OK} 已登录，当前页 {url[:70]}")
                n = pg.locator("textarea, ms-autosize-textarea").count()
                print(f"  {OK} 输入框数量: {n}" if n else f"  {WARN} 未找到输入框（页面结构可能已变）")
                return bool(n)
            finally:
                pg.close()
    except Exception as exc:  # noqa: BLE001
        print(f"  {BAD} 检查失败: {type(exc).__name__}: {str(exc)[:120]}")
        return False


def check_4_services() -> bool:
    print("\n【4】本地服务")
    ok = True
    for port, name, hint in (
        (PROXY_PORT, "OpenAI 兼容代理", "python gemini_chrome_proxy.py"),
        (BACKEND_PORT, "NWW 后端", "launchctl kickstart -k gui/$(id -u)/com.nww.local-backend"),
    ):
        if _listen(port):
            print(f"  {OK} {name} :{port}")
        else:
            print(f"  {WARN} {name} :{port} 未启动 —— {hint}")
            ok = False
    return ok


def main() -> int:
    print("=" * 60)
    print("chrome_bridge 体检")
    print("=" * 60)
    RESULTS.append(check_1_vpn())
    RESULTS.append(check_2_cdp())
    RESULTS.append(check_3_login())
    RESULTS.append(check_4_services())
    passed = sum(RESULTS)
    print("\n" + "=" * 60)
    print(f"结果：{passed}/{len(RESULTS)} 项通过")
    if passed == len(RESULTS):
        print("桥接依赖齐备，可以调用。")
    else:
        print("按上面 ❌ 项的修复提示处理；顺序为 外网 → CDP → 登录 → 服务。")
    print("=" * 60)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
