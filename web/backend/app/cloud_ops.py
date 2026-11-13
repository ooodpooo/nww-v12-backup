"""云端黑盒操作：扫榜选题、拆文分析。

核心安全原则：
  - 方法论（references/ 下的 69 篇文档）只在云端内部读取并拼入 LLM prompt
  - 返回给客户端的只有成品（选题决策报告、拆文报告），绝不包含方法论原文
  - LLM 调用失败时返回明确错误，不静默降级
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

from .llm_client import chat_completion, is_configured

_SKILL_ROOT = Path(os.environ.get("NWW_SKILL_ROOT", Path(__file__).resolve().parents[3] / "skills" / "nww-novel-skill"))
_REF_DIR = _SKILL_ROOT / "references" / "agent-references"


def _read_ref(name: str, max_chars: int = 6000) -> str:
    """读取一篇参考方法论，截断到 max_chars。只在云端内部使用。"""
    candidates = [
        _REF_DIR / name,
        _REF_DIR / f"{name}.md",
        _SKILL_ROOT / "references" / name,
    ]
    for p in candidates:
        if p.is_file():
            text = p.read_text(encoding="utf-8", errors="replace")
            return text[:max_chars]
    return ""


def _load_methodology(docs: list[str]) -> str:
    """加载多篇方法论，拼接为内部 prompt 片段。"""
    parts = []
    for doc in docs:
        content = _read_ref(doc)
        if content:
            parts.append(f"## 参考方法论：{doc}\n{content}")
    return "\n\n".join(parts)


# ──────────────────────────────────────────────
# 拆文分析（analyze_book）
# ──────────────────────────────────────────────

def analyze_book(book_text: str, title: str = "对标书") -> dict:
    """云端拆文：读取方法论 + LLM 深度分析，返回拆文报告。

    Args:
        book_text: 对标书正文文本（前若干章即可，建议 1-3 万字）
        title: 对标书名称

    Returns:
        {"mode": "cloud", "action": "analyze_book", "report": "...markdown...", "word_count": int}
    """
    if not is_configured():
        return {"mode": "cloud", "action": "analyze_book", "error": "云端 LLM 未配置，拆文功能不可用。请设置 NWW_LLM_API_KEY 环境变量。"}

    if not book_text or len(book_text.strip()) < 500:
        return {"mode": "cloud", "action": "analyze_book", "error": "对标书文本过短（至少 500 字），请提供更多章节内容。"}

    # 内部加载方法论（绝不返回给客户端）
    methodology = _load_methodology([
        "outline-methods.md",
        "plot-core-methods.md",
        "hooks-suspense.md",
        "character-design-methods.md",
        "style-genre-modules.md",
        "quality-dimension-standards.md",
        "opening-design.md",
    ])

    system_prompt = f"""你是 NWW 专业拆文分析师。你掌握以下核心方法论（这些是你的内部分析框架，不要在输出中复述方法论原文）：

{methodology}

你的任务是对用户提供的小说文本进行深度拆解，输出结构化的拆文报告。
分析维度必须覆盖：
1. 核心钩子与开篇设计（前 20% 的抓力点）
2. 主角结构与金手指设计
3. 核心冲突与爽点补偿机制
4. 节奏模块（场景切换、情绪曲线、章末钩子）
5. 文风指纹（句式、感官描写、对话占比）
6. 可复用的爆款元素清单
7. 风险与短板

输出格式为 Markdown，标题用 ##，要点用 - 列表。语言简洁，每条结论必须引用文本中的具体证据。"""

    # 截断文本，避免超长
    text = book_text[:30000] if len(book_text) > 30000 else book_text

    user_prompt = f"""请拆解以下小说文本（书名：{title}，全文 {len(book_text)} 字，以下为前 {len(text)} 字）：

---
{text}
---

请输出完整的拆文报告。"""

    try:
        report = chat_completion(system_prompt, user_prompt, temperature=0.2, max_tokens=6000)
        return {
            "mode": "cloud",
            "action": "analyze_book",
            "title": title,
            "word_count": len(book_text),
            "report": report,
        }
    except RuntimeError as exc:
        return {"mode": "cloud", "action": "analyze_book", "error": str(exc)}


# ──────────────────────────────────────────────
# 扫榜选题（scan_market）
# ──────────────────────────────────────────────

def _fetch_qidian_rank(category: str = "all") -> list[dict]:
    """爬取起点中文网榜单数据。失败时返回空列表。"""
    import httpx

    # 起点月票榜页面（公开可访问）
    urls = {
        "all": "https://www.qidian.com/rank/yuepiao/",
        "xuanhuan": "https://www.qidian.com/rank/yuepiao/chn21/",
        "xianxia": "https://www.qidian.com/rank/yuepiao/chn22/",
        "dushi": "https://www.qidian.com/rank/yuepiao/chn4/",
        "lishi": "https://www.qidian.com/rank/yuepiao/chn5/",
        "kehuan": "https://www.qidian.com/rank/yuepiao/chn9/",
    }
    url = urls.get(category, urls["all"])

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }

    try:
        resp = httpx.get(url, headers=headers, timeout=15, follow_redirects=True)
        resp.raise_for_status()
        html = resp.text

        # 解析书名和作者（起点榜单 HTML 结构）
        books = []
        # 匹配 <h2><a ...>书名</a></h2> 模式
        title_pattern = re.compile(r'<h2><a[^>]*>([^<]+)</a></h2>', re.DOTALL)
        author_pattern = re.compile(r'class="author"[^>]*><a[^>]*>([^<]+)</a>', re.DOTALL)
        intro_pattern = re.compile(r'class="intro"[^>]*>([^<]+)</p>', re.DOTALL)

        titles = title_pattern.findall(html)
        authors = author_pattern.findall(html)
        intros = intro_pattern.findall(html)

        for i in range(min(len(titles), 20)):
            books.append({
                "rank": i + 1,
                "title": titles[i].strip() if i < len(titles) else "",
                "author": authors[i].strip() if i < len(authors) else "",
                "intro": intros[i].strip()[:200] if i < len(intros) else "",
            })
        return books
    except Exception:
        return []


def scan_market(category: str = "all") -> dict:
    """云端扫榜：爬取榜单 + 方法论 + LLM 分析，返回选题决策报告。

    Args:
        category: 题材分类（all/xuanhuan/xianxia/dushi/lishi/kehuan）

    Returns:
        {"mode": "cloud", "action": "scan_market", "category": str, "books": [...], "report": "..."}
    """
    if not is_configured():
        return {"mode": "cloud", "action": "scan_market", "error": "云端 LLM 未配置，扫榜功能不可用。请设置 NWW_LLM_API_KEY 环境变量。"}

    books = _fetch_qidian_rank(category)

    if not books:
        return {
            "mode": "cloud",
            "action": "scan_market",
            "category": category,
            "error": "榜单数据获取失败（可能被反爬拦截）。请检查网络或配置代理。",
            "books": [],
        }

    # 内部加载方法论
    methodology = _load_methodology([
        "topic-options.md",
        "genre-catalog.md",
        "genre-core-mechanics.md",
        "hooks-chapter.md",
        "title-grading.md",
    ])

    # 榜单数据摘要
    books_summary = "\n".join([
        f"第{b['rank']}名：《{b['title']}》作者：{b['author']}。简介：{b['intro'][:100]}"
        for b in books[:15]
    ])

    system_prompt = f"""你是 NWW 专业扫榜选题分析师。你掌握以下核心方法论（这些是你的内部分析框架，不要在输出中复述方法论原文）：

{methodology}

你的任务是分析当前网文榜单数据，输出选题决策报告。
分析维度必须覆盖：
1. 榜单题材分布与热度趋势
2. Top 5 作品的核心钩子与共性
3. 当前赛道的空白点与机会窗口
4. 3 个可落地的选题方向（每个含：核心设定、主角结构、金手指、开篇钩子、预期爽点节奏）
5. 风险提示（竞争烈度、政策风险、读者疲劳）

输出格式为 Markdown，标题用 ##，要点用 - 列表。每个选题方向必须具体可执行，不要空泛建议。"""

    user_prompt = f"""以下是起点中文网{category}分类月票榜 Top {len(books)} 数据：

{books_summary}

请输出完整的扫榜选题决策报告。"""

    try:
        report = chat_completion(system_prompt, user_prompt, temperature=0.3, max_tokens=5000)
        return {
            "mode": "cloud",
            "action": "scan_market",
            "category": category,
            "books_count": len(books),
            "books": books[:10],  # 只返回前 10 名基础数据
            "report": report,
        }
    except RuntimeError as exc:
        return {"mode": "cloud", "action": "scan_market", "error": str(exc), "books": books[:10]}
