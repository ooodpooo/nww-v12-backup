"""Playwright 适配器基类：持久会话、登录守护、选择器校准、通用发布流程。

设计要点
--------
- 用 launch_persistent_context 持有一个真实 Chromium 用户目录，登录态（cookie/localStorage）
  在多次运行间自动保留；起点微信扫码只需在会话失效时重来一次。
- SPA 的 DOM 选择器易变，全部外置到 <platform>.selectors.json，由用户用 calibrate 校准。
- 反爬：使用真实 profile + 拟人节奏（随机停顿），但仍属「自己的账号发自己的书」的合规用途；
  请勿批量灌水，遵守平台节奏限制。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from playwright.async_api import async_playwright, BrowserContext, Page


class BaseAdapter:
    name = "base"

    #: 定时的最小提前量（分钟）。目标时刻距现在不足这个量 → 退化为「立即发布」。
    MIN_SCHEDULE_LEAD_MIN = 30

    def __init__(self, config: Dict[str, Any], selectors: Dict[str, Any], user_data_dir: Path, headless: bool = False):
        self.cfg = config
        self.sel = selectors
        self.user_data_dir = Path(user_data_dir)
        self.headless = headless
        self._pw = None
        self.context: Optional[BrowserContext] = None

    # ---- lifecycle ----
    async def __aenter__(self):
        self._pw = await async_playwright().start()
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self.context = await self._pw.chromium.launch_persistent_context(
            str(self.user_data_dir),
            headless=self.headless,
            viewport={"width": 1366, "height": 900},
            locale="zh-CN",
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            args=["--disable-blink-features=AutomationControlled"],
        )
        return self

    async def __aexit__(self, *exc):
        if self.context:
            await self.context.close()
        if self._pw:
            await self._pw.stop()

    async def new_page(self) -> Page:
        return await self.context.new_page()

    # ---- login ----
    async def ensure_session(self, page: Page, manual_ack: bool = False) -> bool:
        """确保已登录。未登录则等用户手动完成登录。返回是否最终已登录。"""
        await page.goto(self.cfg["login_url"], wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)
        if await self.is_logged_in(page):
            return True
        print(f"[{(self.name)}] 未检测到登录态，请在打开的浏览器中手动登录（起点需微信扫码）。")
        if manual_ack or not self.sel.get("logged_in_selector"):
            input("登录完成后按回车继续…")
        else:
            print(f"[{(self.name)}] 等待出现登录成功标识（{self.sel['logged_in_selector']}）…")
            try:
                await page.wait_for_selector(self.sel["logged_in_selector"], timeout=300_000)
            except Exception:
                print("[warn] 等待登录超时，请检查 logged_in_selector 配置。")
                input("登录完成后按回车继续…")
        return await self.is_logged_in(page)

    # 未登录时平台会把页面重定向到登录页，用 URL 特征兜底判定。
    # 起点 write.qq.com/portal/login、番茄 fanqienovel.com/main/writer/login、
    # 以及通用的 passport / sso 登录域。
    LOGIN_URL_HINTS = ("/login", "login.html", "passport", "sso.", "/signin")

    async def is_logged_in(self, page: Page) -> bool:
        """判定当前会话是否已登录。

        优先级：显式选择器（校准后最准）→ URL 兜底。
        URL 兜底是**必须的**：选择器未校准时若直接默认返回 True，
        「扫码登录」会在用户扫码之前就判定成功并关掉浏览器窗口，
        导致永远登不进来。
        """
        if self.sel.get("logged_in_selector"):
            try:
                await page.wait_for_selector(self.sel["logged_in_selector"], timeout=3000)
                return True
            except Exception:
                return False
        if self.sel.get("login_form_selector"):
            try:
                await page.wait_for_selector(self.sel["login_form_selector"], timeout=3000)
                return False
            except Exception:
                return True

        # 无选择器配置 → 用当前 URL 判定是否被踢到登录页
        url = (page.url or "").lower()
        if any(h in url for h in self.LOGIN_URL_HINTS):
            return False
        return True

    # ---- calibrate ----
    async def calibrate(self, page: Page, new_chapter_url: Optional[str] = None) -> Dict[str, Any]:
        """进入「新建章节」页，dump 候选元素，便于回填选择器。"""
        target = new_chapter_url or self.cfg.get("new_chapter_url") or self.cfg["login_url"]
        await page.goto(target, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        await self.ensure_session(page)
        print("[calibrate] 已进入目标页，正在 dump 候选元素（在浏览器里手动走到「新建章节」编辑页后，按回车重新 dump）…")
        input("走到新建章节编辑页后按回车…")
        elements = await self._dump_elements(page)
        out = self.user_data_dir.parent.parent / f"{self.name}.calibration.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(elements, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[calibrate] 已写出候选元素到: {out}")
        print(json.dumps(elements[:25], ensure_ascii=False, indent=2))
        return elements

    async def _dump_elements(self, page: Page) -> List[Dict[str, Any]]:
        return await page.evaluate(
            """() => {
                const pick = (el) => {
                    const cls = el.className && el.className.baseVal !== undefined ? el.className.baseVal : el.className;
                    return {
                        tag: el.tagName.toLowerCase(),
                        id: el.id || null,
                        cls: (typeof cls === 'string' ? cls : '').slice(0, 80) || null,
                        text: (el.innerText || el.value || '').trim().slice(0, 40) || null,
                        placeholder: el.placeholder || null,
                        contenteditable: el.getAttribute('contenteditable') || null,
                        href: el.getAttribute('href') || null,
                    };
                };
                const sels = ['input','textarea','[contenteditable="true"]','[contenteditable]','button','a'];
                const out = [];
                for (const s of sels) {
                    for (const el of document.querySelectorAll(s)) {
                        const p = pick(el);
                        if (p.text || p.placeholder || p.id || p.cls) out.push(p);
                    }
                }
                return out.slice(0, 60);
            }"""
        )

    # ---- books ----
    async def list_books(self, page: Page) -> List[Dict[str, Any]]:
        """通用：进入书籍管理页，dump 疑似书籍链接。子类可覆盖。"""
        await page.goto(self.cfg["dashboard_url"], wait_until="domcontentloaded")
        await self.ensure_session(page)
        await page.wait_for_timeout(2000)
        links = await page.evaluate(
            """() => Array.from(document.querySelectorAll('a[href]'))
                .map(a => ({text: (a.innerText||'').trim().slice(0,40), href: a.getAttribute('href')}))
                .filter(x => x.text && /书|作品|book/i.test(x.text + x.href))"""
        )
        return links

    # ---- 对账：读取平台上该书已发布到第几章 ----
    async def fetch_published_progress(
        self, page: Page, book_ref: Optional[str] = None, book_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """进入该书的章节管理页，解析「已发布到第几章」，供排期前对账。

        返回 {ok, published_up_to, candidates, sample, url, error}。
        **解析不到就明确报错，绝不返回猜测值**——排期基准错一位就是重复发布。
        定位顺序：配了 chapter_list_url 且知道 book_ref → 直接进章节页；
                  否则进作品管理页，按书名点到该书，再扫进度。
        抽取优先级：chapter_rows 选择器逐行 → 整页文本正则兜底（第N章）。
        注意：形如「共 N 章」的是**章节总数**，不是进度，会被排除。
        """
        entered_via = ""
        target = self.cfg.get("chapter_list_url")
        if target and book_ref:
            target = target.replace("{book_id}", str(book_ref))
        else:
            target = self.cfg.get("dashboard_url")

        if not target:
            return {"ok": False, "error": "未配置可访问的平台页面（config.chapter_list_url / dashboard_url）"}

        # 部分站点（如起点作家专区 write.qq.com）**必须先经 login_url 跳一次**才会激活登录态；
        # 直接访问目标页会退化成未登录门户。多一跳无副作用，统一做。
        warm = self.cfg.get("login_url")
        if warm and warm != target:
            try:
                await page.goto(warm, wait_until="domcontentloaded")
                await page.wait_for_timeout(1200)
            except Exception:
                pass

        await page.goto(target, wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)

        # 这里不能用 ensure_session()：它在未登录时会 input() 等人工按回车，会挂死后台请求
        if not await self.is_logged_in(page):
            return {"ok": False, "error": f"{self.name} 会话已失效，请先点击「扫码登录」", "url": page.url}

        # 未直接进章节页时，尝试按书名在作品管理页找到该书并点进去
        if not (self.cfg.get("chapter_list_url") and book_ref) and book_name:
            try:
                link = page.locator(f'a:has-text("{book_name}")').first
                if await link.count() > 0:
                    await link.click()
                    await page.wait_for_timeout(2500)
                    entered_via = f"按书名「{book_name}」进入"
            except Exception:
                pass

        texts: List[str] = []

        # 优先：按书名圈出「这一本」的卡片。
        # 作品管理页往往同页列出多本书（如本书第24章、另一本第26章），
        # 整页扫「第N章」会把别的书算进来，取 max 直接错位 → 必须限定范围。
        if book_name:
            try:
                card = await page.evaluate(
                    """(name) => {
                        const cands = Array.from(document.querySelectorAll('body *'))
                            .filter(e => {
                                const t = (e.innerText || '').trim();
                                return t && t.length < 80 && t.includes(name);
                            });
                        if (!cands.length) return null;
                        // 取最深的命中节点，向上几层拿到该作品的卡片容器
                        let el = cands[cands.length - 1];
                        for (let i = 0; i < 3 && el.parentElement; i++) el = el.parentElement;
                        return el.innerText || null;
                    }""",
                    book_name,
                )
                if card:
                    texts = [card]
            except Exception:
                texts = []

        rows_sel = self.sel.get("chapter_rows")
        if not texts and rows_sel:
            try:
                texts = await page.eval_on_selector_all(
                    rows_sel, "els => els.map(e => (e.innerText || '').trim()).filter(Boolean)"
                )
            except Exception:
                texts = []
        if not texts:
            whole = await page.evaluate("() => (document.body && document.body.innerText) || ''")
            texts = [whole]

        nums: List[int] = []
        for t in texts:
            for line in str(t).splitlines():
                if re.search(r"共\s*\d+\s*章", line):
                    continue  # 章节总数，不是已发进度
                for m in re.finditer(r"第\s*(\d+)\s*章", line):
                    nums.append(int(m.group(1)))

        if not nums:
            return {
                "ok": False,
                "error": "未能从页面解析出「第 N 章」。请运行 calibrate 填入 chapter_rows 选择器，或手工填写基准。",
                "sample": ("\n".join(texts))[:300],
                "url": page.url,
            }

        return {
            "ok": True,
            "published_up_to": max(nums),
            "candidates": sorted(set(nums))[-5:],
            "sample": ("\n".join(texts))[:300],
            "entered_via": entered_via,
            "url": page.url,
        }

    # ---- publish ----
    async def publish_chapter(
        self,
        page: Page,
        book_id: Optional[str],
        title: str,
        body: str,
        draft: bool = False,
        schedule_at: Optional[datetime] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """通用发布流程（按 selectors.json 驱动）。返回 {ok, chapter_id, url, error, ...}。

        schedule_at : 传 datetime 则走「定时发布」（起点：发布→确认章节信息弹窗→勾定时→设时间→设日期）；
                      为 None 则立即发布，行为与旧版完全一致。
        dry_run     : True 时**只走到弹窗并把状态读回来，绝不点提交**（校验选择器/排期用）。
        """
        s = self.sel
        if not (s.get("title_input") and s.get("content_editor") and (s.get("publish_button") or s.get("draft_button"))):
            return {"ok": False, "error": "选择器未校准：请先运行 calibrate 并填写 selectors.json 的 title_input/content_editor/publish_button"}

        # 登录态预热：起点（write.qq.com）直接访问写作页拿不到登录态，
        # 必须先过 /portal/login 激活一次。与 fetch_published_progress 同源同因。
        warm = self.cfg.get("login_url")
        if warm:
            try:
                await page.goto(warm, wait_until="domcontentloaded")
                await page.wait_for_timeout(1200)
            except Exception:
                pass

        if self.cfg.get("new_chapter_url"):
            url = self.cfg["new_chapter_url"]
            if book_id:
                url = url.replace("{book_id}", str(book_id))
            await page.goto(url, wait_until="domcontentloaded")
        elif s.get("new_chapter_button"):
            await page.click(s["new_chapter_button"])
        else:
            return {"ok": False, "error": "缺少 new_chapter_url 或 new_chapter_button 选择器"}

        # 编辑器初始化（TinyMCE/tinymce.editors 需要时间就绪）
        await page.wait_for_timeout(3000)
        # 标题
        await self._fill(page, s["title_input"], title)
        # 正文
        await self._fill(page, s["content_editor"], body, mode=s.get("content_editor_mode"))
        await page.wait_for_timeout(1500)

        # 干跑：只验证到定时弹窗并读回状态，**绝不提交**
        if dry_run:
            probe = schedule_at or (datetime.now() + timedelta(hours=1))
            out = await self._publish_scheduled(page, title, probe, dry_run=True)
            out["mode"] = "dry_run"
            out["mode_reason"] = "干跑：只走到定时弹窗、读回状态后取消"
            return out

        plan = self.resolve_publish_mode(schedule_at)
        if plan["mode"] == "schedule":
            out = await self._publish_scheduled(page, title, plan["when"], dry_run=False)
        else:
            out = await self._publish_immediate(page, title, draft)
        out["mode"] = plan["mode"]
        out["mode_reason"] = plan["reason"]
        return out

    def resolve_publish_mode(self, when: Optional[datetime]) -> Dict[str, Any]:
        """按用户约定决定「立即发布」还是「定时发布」（2026-09-12 用户明确）。

        - 目标日期是**当天** → 不需要定时，直接发布；
        - 需要定时时，定时时刻必须在**当前时间 30 分钟以后**。
        命中以上任一情形都退化为立即发布，避免把该立刻发的章误排到未来。
        """
        now = datetime.now()
        if when is None:
            return {"mode": "immediate", "when": None, "reason": "未指定定时时间"}
        if when.date() == now.date():
            return {"mode": "immediate", "when": None, "reason": "目标为当天 → 不需要定时，直接发布"}
        lead_sec = (when - now).total_seconds()
        if lead_sec < self.MIN_SCHEDULE_LEAD_MIN * 60:
            return {
                "mode": "immediate",
                "when": None,
                "reason": f"距目标仅 {int(lead_sec // 60)} 分钟（要求 ≥{self.MIN_SCHEDULE_LEAD_MIN} 分钟）→ 直接发布",
            }
        return {"mode": "schedule", "when": when, "reason": None}

    async def _publish_immediate(self, page: Page, title: str, draft: bool) -> Dict[str, Any]:
        """立即发布（勾选定时前的原始路径）。"""
        s = self.sel
        btn = s["draft_button"] if (draft and s.get("draft_button")) else s["publish_button"]
        await page.click(btn)
        await page.wait_for_timeout(2000)

        ok = True
        chapter_id = None
        url_out = page.url
        err = None
        if s.get("success_indicator"):
            try:
                await page.wait_for_selector(s["success_indicator"], timeout=8000)
            except Exception:
                ok = False
                err = "未检测到发布成功标识"
        if ok and s.get("close_or_confirm"):
            try:
                await page.click(s["close_or_confirm"])
            except Exception:
                pass
        return {"ok": ok, "scheduled": False, "chapter_id": chapter_id,
                "url": url_out, "error": err}

    # ---- 定时发布（起点「确认章节信息」弹窗）----
    #
    # 起点流程：写作页填标题正文 → 点发布 → 弹 div.publish-form-dialog →
    #   勾 #sendTimimg → 设时间 → 设日期 → 点 button.btn-send-timing。
    #
    # ⚠️ 两个必须记住的坑（均为实测）：
    #  1. 只有勾了「定时发布」才会出现 button.btn-send-timing；未勾时是 button.btn-send-sure（确认发布）。
    #  2. **必须先设时间、再设日期**：日期弹层一打开就会盖住「常设时间」，
    #     此时点预设一定 Timeout。顺序颠倒 = 时间设不上。
    #  3. 日期/时间弹层是 append 到 body 的（父节点 class 形如 fz-m skin-light），
    #     **不在 .publish-form-dialog 内** → 选择器必须带 div.ui-date-container 前缀走全局。

    async def _publish_scheduled(
        self,
        page: Page,
        title: str,
        when: Optional[datetime],
        dry_run: bool,
    ) -> Dict[str, Any]:
        """走定时发布弹窗。dry_run=True 时读回状态后取消，绝不提交。"""
        s = self.sel.get("schedule") or {}
        res: Dict[str, Any] = {
            "ok": False,
            "scheduled": False,
            "dry_run": dry_run,
            "title": title,
            "target": when.strftime("%Y-%m-%d %H:%M") if when else None,
            "steps": {},
            "dialog": None,
            "url": page.url,
            "error": None,
        }
        if not s.get("toggle"):
            res["error"] = "selectors.json 未配置 schedule 段（无法定时）"
            return res

        # 提交前自检：标题必须已落入**页面状态**（不是 DOM value 而已）。
        # 若标题为空，点发布会只弹前端校验提示、弹窗根本不出现 —— 先记下来便于定位。
        res["precheck"] = await page.evaluate(
            """([tSel]) => {
                const t = document.querySelector(tSel);
                const m = document.body.innerText.match(/本章字数[：:]\\s*(\\d+)/);
                return {title: t ? t.value : null, word_count: m ? m[1] : null};
            }""",
            [self.sel["title_input"]],
        )

        # 点发布 → 轮询等确认弹窗（不用 wait_for_selector，失败时好抓诊断）
        await page.click(self.sel["publish_button"])
        appeared = False
        for _ in range(12):
            await page.wait_for_timeout(700)
            if await page.locator(s["dialog"]).count():
                appeared = True
                break
        if not appeared:
            res["error"] = ("确认弹窗未出现：多为标题/正文未落入页面状态，被前端校验拦下"
                            "（检查 precheck.title）")
            res["screen"] = await page.evaluate(
                """() => {
                    const vis = el => { const r = el.getBoundingClientRect();
                      const cs = getComputedStyle(el);
                      return r.width > 0 && r.height > 0 && cs.display !== 'none'
                             && cs.visibility !== 'hidden'; };
                    const out = {visibleDialogs: [], toasts: []};
                    for (const el of document.querySelectorAll(
                            'div[class*=dialog],div[class*=modal],div[class*=toast],div[class*=tip]')) {
                        if (!vis(el)) continue;
                        const t = (el.innerText || '').replace(/\\n+/g, ' | ').slice(0, 140);
                        if (t) out.visibleDialogs.push({cls: String(el.className||'').slice(0,80), t});
                    }
                    return out;
                }""",
            )
            return res
        await page.wait_for_timeout(600)

        # 勾「定时发布」——check() 会被 label 挡住而超时，必须 force click
        try:
            await page.click(s["toggle"], force=True)
        except Exception:
            await page.evaluate(
                "(sel) => { const c = document.querySelector(sel); if (c) {"
                " c.checked = true;"
                " c.dispatchEvent(new MouseEvent('click', {bubbles:true}));"
                " c.dispatchEvent(new Event('change', {bubbles:true})); } }",
                s["toggle"],
            )
        await page.wait_for_timeout(1200)

        if when is not None:
            res["steps"] = await self._set_schedule_datetime(page, when, s)

        state = await self._read_schedule_state(page, s)
        res["dialog"] = state

        if dry_run:
            try:
                await page.click(s["cancel"], timeout=5000)
            except Exception:
                pass
            res["ok"] = bool(state.get("date") and state.get("time"))
            res["error"] = None if res["ok"] else "定时控件读回失败（date/time 为空）"
            return res

        await page.click(s["submit"])
        await page.wait_for_timeout(3000)
        res["ok"] = True
        res["scheduled"] = True
        res["url"] = page.url
        return res

    async def _set_schedule_datetime(self, page: Page, when: datetime, s: Dict[str, Any]) -> Dict[str, Any]:
        """设置定时时间点。返回各步骤结果，便于诊断（不抛异常）。"""
        hhmm = when.strftime("%H:%M")
        date_str = when.strftime("%Y-%m-%d")
        steps: Dict[str, Any] = {"target_time": hhmm, "target_date": date_str}

        # ① 时间（务必先于日期）
        steps["preset"] = await self._set_time_by_preset(page, s, hhmm)
        steps["time_set"] = steps["preset"]
        if not steps["preset"]:
            steps["picked_cols"] = await self._set_time_by_columns(page, s, hhmm)
            steps["time_set"] = steps["picked_cols"]
        steps["time_read"] = await self._read_time(page, s)

        # ② 日期
        steps["date_set"] = await self._set_date(page, s, date_str)
        steps["date_read"] = await self._read_date(page, s)
        return steps

    async def _set_time_by_preset(self, page: Page, s: Dict[str, Any], hhmm: str) -> bool:
        """优先点「常设时间」预设（目标时刻恰在其中时最快最稳）。"""
        hit = await page.evaluate(
            """([sel, v]) => {
                const a = Array.from(document.querySelectorAll(sel))
                  .find(e => (e.innerText || '').trim() === v);
                if (!a) return false;
                a.click();
                a.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
                return true;
            }""",
            [s["preset"], hhmm],
        )
        if not hit:
            return False
        await page.wait_for_timeout(1000)
        return await self._read_time(page, s) == hhmm

    async def _set_time_by_columns(self, page: Page, s: Dict[str, Any], hhmm: str) -> bool:
        """任意时间：开时间面板 → 点小时列 + 分钟列。"""
        hh, mm = hhmm.split(":")
        try:
            await page.click(s["time_trigger"], force=True)
        except Exception:
            return False
        await page.wait_for_timeout(1200)
        picked = await page.evaluate(
            """([hSel, mSel, hh, mm]) => {
                const pick = (sel, v) => {
                    const it = Array.from(document.querySelectorAll(sel))
                      .find(e => (e.innerText || '').trim() === v);
                    if (!it) return false;
                    it.scrollIntoView({block: 'center'});
                    it.click();
                    it.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
                    return true;
                };
                return {h: pick(hSel, hh), m: pick(mSel, mm)};
            }""",
            [s["hour_items"], s["minute_items"], hh, mm],
        )
        await page.wait_for_timeout(1200)
        if not (picked.get("h") and picked.get("m")):
            return False
        return await self._read_time(page, s) == hhmm

    async def _set_date(self, page: Page, s: Dict[str, Any], date_str: str) -> bool:
        """开日历点目标日。**仅支持当前月份**内的日期（跨月翻页未实测，返回 False）。"""
        try:
            await page.click(s["date_trigger"], force=True)
        except Exception:
            return False
        await page.wait_for_timeout(1200)
        sel = f'{s["date_items"]}[data-date="{date_str}"]'
        try:
            if await page.locator(sel).count() == 0:
                return False
            await page.click(sel, force=True)
        except Exception:
            return False
        await page.wait_for_timeout(1000)
        return await self._read_date(page, s) == date_str

    async def _read_schedule_state(self, page: Page, s: Dict[str, Any]) -> Dict[str, Any]:
        """读回定时弹窗当前状态（日期/时间/提交按钮）。"""
        return await page.evaluate(
            """(cfg) => {
                const q = (sel) => document.querySelector(sel);
                const d = q(cfg.date_input), t = q(cfg.time_input), sub = q(cfg.submit);
                return {
                    date: d ? d.value : null,
                    time: t ? t.value : null,
                    submit_label: sub ? (sub.innerText || '').trim() : null,
                    submit_visible: sub ? sub.getBoundingClientRect().width > 0 : false,
                };
            }""",
            s,
        )

    async def _read_time(self, page: Page, s: Dict[str, Any]) -> Optional[str]:
        return (await self._read_schedule_state(page, s)).get("time")

    async def _read_date(self, page: Page, s: Dict[str, Any]) -> Optional[str]:
        return (await self._read_schedule_state(page, s)).get("date")

    async def _fill(self, page: Page, selector: str, text: str, mode: Optional[str] = None) -> None:
        """自适应填充：textarea 设 value，contenteditable 设 innerText（保留换行）。

        mode="tinymce" 时改走编辑器实例 API（见 _fill_tinymce）。
        """
        if mode == "tinymce":
            await self._fill_tinymce(page, text)
            return
        tag = await page.evaluate(
            """(sel) => {
                const el = document.querySelector(sel);
                if (!el) return 'missing';
                if (el.tagName === 'TEXTAREA') return 'textarea';
                if (el.tagName === 'INPUT') return 'input';
                if (el.getAttribute('contenteditable') !== null) return 'contenteditable';
                return el.tagName.toLowerCase();
            }""",
            selector,
        )
        if tag == "missing":
            raise RuntimeError(f"找不到元素: {selector}")
        if tag in ("textarea", "input"):
            # 优先用 Playwright 原生 fill：它会走真实的键盘/事件链路，框架能正确感知；
            # **纯 JS 写 el.value 会让页面的内部状态仍是空**，表现为点「发布」被前端校验拦下、
            # 弹窗根本不出现（2026-09-12 实测踩过）。原生 fill 失败才退回 JS。
            try:
                await page.fill(selector, text)
            except Exception:
                # Playwright Python 的 page.evaluate 只接一个参数 → 多参数必须打包成 list
                await page.evaluate(
                    """([sel, v]) => {
                        const el = document.querySelector(sel);
                        el.focus();
                        el.value = v;
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                    }""",
                    [selector, text],
                )
        else:  # contenteditable
            html = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
            await page.evaluate(
                """([sel, html]) => {
                    const el = document.querySelector(sel);
                    el.focus();
                    el.innerHTML = html;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                }""",
                [selector, html],
            )

    async def _fill_tinymce(self, page: Page, text: str) -> None:
        """TinyMCE 正文填充（起点写作页）。

        为什么不能直接 fill：`#mce_0` 是 display:none 的隐藏 textarea，真实编辑区在
        `#mce_0_ifr` iframe 里 → `page.fill('#mce_0', ...)` 会 Timeout（实测踩过）。
        必须走编辑器实例，并**补 fire input/change/keyup**，否则页面「本章字数」仍显示 0，
        提交时会被平台拦下（「请输入1~20000字以内的章节内容 !」）。
        """
        html = "".join(
            "<p>{}</p>".format(
                ln.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            )
            for ln in text.splitlines()
            if ln.strip()
        )
        ok = await page.evaluate(
            """(html) => {
                if (typeof tinymce === 'undefined' || !tinymce.editors || !tinymce.editors.length) {
                    return false;
                }
                const ed = tinymce.editors[0];
                ed.setContent(html);
                ed.fire('input');
                ed.fire('change');
                ed.fire('keyup');
                return true;
            }""",
            html,
        )
        if not ok:
            raise RuntimeError("TinyMCE 未就绪：tinymce.editors 为空（编辑器还没初始化完？）")
