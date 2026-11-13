"""改稿规律提炼（P0 捕获 / P1 提炼）—— 只做旁路，不改保存与生成语义。

链路：
    编辑器保存(source=editor, 正文/*.md)
      → dataplane 覆写前回读旧稿
      → enqueue_diff：按 (书, 章) 防抖 60s、变化 <50 字丢弃
      → 设定/笔记反馈/raw/*.json 快照（cap 30，按章合并 old→final）
      → daemon 线程：LLM 只提炼文笔类规律，合并进 设定/笔记反馈/rules.json

约束（2026-10-24 源码审计结论）：
- 保存请求内**不做任何 LLM 调用**，只入队；提炼失败静默，绝不冒泡到保存；
- 只收「句式/节奏/感官/口癖/收尾/视角」六类，剧情事实不进本通道（那是 追踪/ 的职责）；
- 润色/deslop 链走 step_polish(packet 不参与)，本文件不假设它们会消费 rules.json。

开关：
- NWW_STYLE_FEEDBACK_CAPTURE（默认 1）：是否落 raw 快照（纯本地、零模型成本）
- NWW_STYLE_FEEDBACK_EXTRACT（默认 0）：是否调用 LLM 提炼（A/B 通过前保持关闭）
"""
from __future__ import annotations

import difflib
import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from . import llm_client, llm_config

ENV_CAPTURE = "NWW_STYLE_FEEDBACK_CAPTURE"
ENV_EXTRACT = "NWW_STYLE_FEEDBACK_EXTRACT"

DEBOUNCE_S = 60.0
MIN_CHANGED_CHARS = 50
MAX_RULES = 15
MAX_RAW = 30
MAX_RULE_CHARS = 60
MAX_READ_CHARS = 200_000

KINDS = ("句式", "节奏", "感官", "口癖", "收尾", "视角")

_RULE_DIR = "笔记反馈"

_PENDING: dict[tuple[str, str], dict] = {}
_PENDING_LOCK = threading.Lock()
_BOOK_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_LOCK = threading.Lock()


# ─────────────────────────── 开关 ───────────────────────────

def _flag(name: str, default: str) -> bool:
    return (os.environ.get(name) or default).strip().lower() in ("1", "true", "yes")


def capture_enabled() -> bool:
    return _flag(ENV_CAPTURE, "1")


def extract_enabled() -> bool:
    return _flag(ENV_EXTRACT, "0")


# ─────────────────────────── 入口 ───────────────────────────

def should_capture(rel_path: str, source: str) -> bool:
    """只学「作者在编辑器里手改正文」的修改（chatbox APPLY / 建书草稿不算口味）。"""
    rel = (rel_path or "").replace("\\", "/")
    return rel.startswith("正文/") and (source or "") == "editor"


def enqueue_diff(book_root: Path, rel_path: str, old: str, new: str, source: str) -> bool:
    """保存后调用：入队一次提炼。**永不抛错**，返回值仅用于前端提示。"""
    try:
        if not capture_enabled() or not should_capture(rel_path, source):
            return False
        if old == new:
            return False
        key = (str(book_root), Path(rel_path).stem)
        now = time.time()
        with _PENDING_LOCK:
            slot = _PENDING.get(key)
            if slot is None:
                slot = {"old": old, "new": new, "rel": rel_path,
                        "ts": now, "running": False}
                _PENDING[key] = slot
            else:
                slot["new"] = new          # 同章连续保存：只保留最终态
                slot["ts"] = now
                slot["rel"] = rel_path
            if slot["running"]:
                return True
            slot["running"] = True
        threading.Thread(target=_flush_loop, args=(key,), daemon=True).start()
        return True
    except Exception:
        return False


def _flush_loop(key: tuple[str, str]) -> None:
    slot: Optional[dict] = None
    while True:
        with _PENDING_LOCK:
            cur = _PENDING.get(key)
            if cur is None:
                return
            wait = cur["ts"] + DEBOUNCE_S - time.time()
            if wait <= 0.5:
                slot = _PENDING.pop(key, None)
                break
        time.sleep(min(max(wait, 0.1), 5.0))
    if slot is None:
        return
    try:
        _flush(key[0], key[1], slot)
    except Exception:
        # 提炼是旁路能力：任何异常都不允许影响保存与生成
        pass


def _flush(book: str, chapter: str, slot: dict) -> None:
    old, new = slot["old"], slot["new"]
    if _changed_chars(old, new) < MIN_CHANGED_CHARS:
        return
    root = Path(book)
    _write_raw(root, chapter, slot["rel"], old, new)
    if not extract_enabled():
        return
    _extract_and_merge(root, chapter, old, new)


# ─────────────────────────── 工具 ───────────────────────────

def _changed_chars(old: str, new: str) -> int:
    """估算改动体量：replace/insert/delete 的最大片段长度之和。"""
    a, b = old[:MAX_READ_CHARS], new[:MAX_READ_CHARS]
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    total = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        total += max(i2 - i1, j2 - j1)
    return total


def _state_dir(book: Path) -> Path:
    return book / "设定" / _RULE_DIR


def rules_path(book: Path) -> Path:
    return _state_dir(book) / "rules.json"


def _book_lock(book: Path) -> threading.Lock:
    with _LOCKS_LOCK:
        lock = _BOOK_LOCKS.get(str(book))
        if lock is None:
            lock = threading.Lock()
            _BOOK_LOCKS[str(book)] = lock
        return lock


def _atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_rules(book: Path) -> dict:
    p = rules_path(book)
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(obj, dict) and isinstance(obj.get("rules"), list):
            return obj
    except (OSError, ValueError):
        pass
    return {"version": 1, "updated_at": "", "rules": []}


def _write_raw(book: Path, chapter: str, rel_path: str, old: str, new: str) -> None:
    d = _state_dir(book) / "raw"
    d.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", chapter)[:60] or "chapter"
    name = f"{safe}-{time.time_ns()}.json"   # 毫秒级会撞名覆盖，用纳秒保证同章连存不丢快照
    _atomic_write_json(d / name, {
        "chapter": chapter,
        "path": rel_path,
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "old": old[:MAX_READ_CHARS],
        "new": new[:MAX_READ_CHARS],
    })
    files = sorted(d.glob("*.json"), key=lambda p: p.name)
    for stale in files[:max(0, len(files) - MAX_RAW)]:
        try:
            stale.unlink()
        except OSError:
            pass


def _norm_rule(text: Any) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s[:MAX_RULE_CHARS]


def _near(a: str, b: str) -> bool:
    if a == b:
        return True
    # 短规则不做模糊匹配：3~4 字条目相似度天然偏高（「规律1」vs「规律10」=0.86），
    # 合并会吞掉本来不同的短句。只有 ≥10 字的规律才允许 0.85 近似归并。
    if len(a) < 10 or len(b) < 10:
        return False
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() >= 0.85


def merge_rules(existing: list[dict], items: list[dict], chapter: str) -> list[dict]:
    """把 LLM 给出的动作合并进规律表（纯函数，便于单测）。"""
    rules = [dict(r) for r in existing if isinstance(r, dict) and r.get("rule")]
    for item in items or []:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action") or "add").strip().lower()
        target = _norm_rule(item.get("target"))
        rule = _norm_rule(item.get("rule")) or target
        if not rule:
            continue
        hit = next((r for r in rules if _near(str(r.get("rule") or ""), target or rule)), None)
        if action == "retire":
            if hit is not None:
                rules.remove(hit)
            continue
        kind = str(item.get("kind") or "").strip()
        if kind not in KINDS:
            kind = "句式"
        evidence = str(item.get("evidence") or "")[:200]
        if hit is not None:
            hit["count"] = int(hit.get("count") or 1) + 1
            hit["last_chapter"] = chapter
            if evidence:
                hit["evidence"] = evidence
            continue
        rules.append({
            "rule": rule,
            "kind": kind,
            "evidence": evidence,
            "count": 1,
            "last_chapter": chapter,
        })
    rules.sort(key=lambda r: (int(r.get("count") or 1), str(r.get("last_chapter") or "")),
               reverse=True)
    return rules[:MAX_RULES]


# ─────────────────────────── 提炼 ───────────────────────────

_EXTRACT_SYSTEM = """你是网文编辑。任务：从作者对正文的改稿 diff 中提炼「文笔偏好规律」。

只允许六类：句式、节奏、感官、口癖、收尾、视角。
禁止记录剧情事实（人物关系、境界等级、物品归属、时间地点变更等）——那是别的系统职责。

要求：
1. 每条规律 ≤40 字，写成可执行的写法方向，例如「抽象心理独白改为具体肢体动作」；
2. evidence 必须引用改前→改后的原文片段（各 ≤20 字），没有对照就不输出该条；
3. 只输出确实重复出现或明显有意的改动；偶发错字、排版调整不要提炼；
4. 最多输出 5 条，宁缺毋滥。
5. 句式/词类规律必须覆盖完整通道，不能只点名一两个词——只禁字面词会被同义替换规避：
   例「禁用比喻（含仿佛/宛如/像…一样）」优于「禁用仿佛」。

输出严格 JSON（不要加解释、不要加代码围栏）：
{"rules":[{"action":"add|reinforce|retire","rule":"规律","kind":"句式|节奏|感官|口癖|收尾|视角","evidence":"原文→改后","target":"reinforce/retire 时填已有规律的原文"}]}"""


def _extract_and_merge(book: Path, chapter: str, old: str, new: str) -> None:
    diff = "\n".join(difflib.unified_diff(
        old[:MAX_READ_CHARS].splitlines(),
        new[:MAX_READ_CHARS].splitlines(),
        fromfile="改前", tofile="改后", n=2, lineterm=""))
    if not diff.strip():
        return
    if len(diff) > 8000:
        diff = diff[:8000] + "\n…（diff 已截断）"
    chain = (llm_config.agent_endpoint_chain("nww-reviewer")
             or llm_config.agent_endpoint_chain("book-coach"))
    if not chain:
        return
    current = load_rules(book)
    existing_lines = "\n".join(
        f"- {r.get('rule')}（{r.get('kind')}，已命中 {r.get('count')} 次）"
        for r in current["rules"]) or "（暂无）"
    user = f"【已有规律】\n{existing_lines}\n\n【本次改稿 diff】\n{diff}"
    text = llm_client.chat_completion_robust(
        _EXTRACT_SYSTEM, user, chain,
        temperature=0.2, max_tokens=1200, timeout=180,
    )
    items = _parse_items(text)
    if not items:
        return
    with _book_lock(book):
        latest = load_rules(book)
        latest["rules"] = merge_rules(latest["rules"], items, chapter)
        latest["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        _atomic_write_json(rules_path(book), latest)


def _parse_items(text: str) -> list[dict]:
    s = (text or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else ""
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    start = s.find("{")
    if start < 0:
        return []
    try:
        obj, _ = json.JSONDecoder().raw_decode(s, start)
    except ValueError:
        return []
    items = obj.get("rules") if isinstance(obj, dict) else None
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []
