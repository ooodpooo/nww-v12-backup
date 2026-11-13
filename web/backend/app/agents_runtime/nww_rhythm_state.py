"""nww_rhythm_state.py — 跨章节奏状态读取（NRL Layer 1：确定性、零 LLM、只读）。

## 定位
节奏层「跨章」那一半：回答「前面几章已经用掉了哪些动力/章尾类型，本章该避开什么」。
只做**读取与计算**，不写任何文件；不解释剧情；不做 LLM 判断。

## 权威来源（硬约定）
1. **已提交游标**：`{BOOK}/追踪/_tracking-state.json` 的 `last_committed_chapter`。
   与 `pipeline.py::_last_committed_chapter` 同口径。**不得**用「正文文件是否存在」
   推断章节已提交（draft/重写/快照都会造成误判）。
2. **章尾类型**：复用既有确定性判据 `skills/nww-novel-skill/scripts/nww_chapter_tail_gate.py`
   （已在 toolbox 白名单内），经 `toolbox.run_script` 跨包调用，与
   `nww_packet_builder_v2.py` 的调用方式一致。
   ★ 该脚本会剥离 `【XX：N】` 系统计数器后再判定——不剥离会 100% 误判为 A 型
   （2026-10-11 的「27/27 = A」就是这么来的，已作废）。
   ★ 该脚本自报 `unclassified_rate`；未分类占比高时**结论必须打折看待**，本模块
   原样透传该比率，由上层决定是否采信禁则。

## 失败语义
任何一步失败都返回 `errors` 列表 + 对应的空/降级值，**绝不猜、绝不伪造类型**。
调用方（planner）据此决定 status。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import toolbox

STATE_SCHEMA = "nrl-state/1"
_TRACKING_DIR = "追踪"
_TAIL_GATE = "nww_chapter_tail_gate.py"

# 章尾类型禁则：连续同型到第几段才禁。rhythm/SKILL.md:34 约束 last_3 禁 AA、AAA
# （允许 ABA、ACB、DAC）→ 连续 2 段即进入禁则，连续 3 段为硬违规。
_RUN_WARN = 2
_RUN_HARD = 3


def _state_file(book: Path) -> Path:
    return book / _TRACKING_DIR / "_tracking-state.json"


def canonical_json(obj) -> str:
    """规范化序列化（指纹唯一口径）。排序键 + 紧凑分隔符 + 不转义中文。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def committed_cursor(book: Path) -> tuple[int | None, list[str]]:
    """读已提交游标。返回 (cursor, errors)。

    cursor=None 表示**无法确定**（不是「0 章已提交」）——调用方不得把 None 当 0 用。
    """
    f = _state_file(book)
    if not f.is_file():
        return None, [f"STATE_MISSING: {f}"]
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return None, [f"STATE_UNREADABLE: {e}"]
    val = data.get("last_committed_chapter")
    if not isinstance(val, int) or val <= 0:
        return None, ["STATE_CURSOR_INVALID: last_committed_chapter 缺失或非法"]
    return val, []


def tail_types(book: Path, upto: int | None = None) -> tuple[dict[int, str], dict, list[str]]:
    """逐章章尾类型。返回 (types, meta, errors)。

    types: {章号: "A"|"B"|"C"|"D"|"U"}
    meta : 分类器自报的可信度信息（unclassified_rate / counters_stripped / violations）
    """
    args = [str(book), "--json"]
    if upto is not None:
        args += ["--upto", str(int(upto))]
    try:
        res = toolbox.run_script(_TAIL_GATE, args, book, timeout=120)
    except Exception as e:  # noqa: BLE001 — 跨包执行器故障不得炸穿上游
        return {}, {}, [f"TAIL_GATE_EXEC_ERROR: {e}"]
    if not res.get("ok") and res.get("returncode") == 2:
        # 用法/输入错误（书目录不存在、无 正文/）——不是"没有数据"，是执行失败
        return {}, {}, [f"TAIL_GATE_USAGE_ERROR: {(res.get('stderr') or '').strip()[:300]}"]
    out = (res.get("stdout") or "").strip()
    if not out:
        return {}, {}, ["TAIL_GATE_EMPTY_OUTPUT"]
    try:
        data = json.loads(out)
    except json.JSONDecodeError as e:
        return {}, {}, [f"TAIL_GATE_BAD_JSON: {e}"]
    rows = data.get("rows") or []
    types = {int(r["chapter"]): r.get("type", "U") for r in rows if r.get("chapter") is not None}
    meta = {
        "chapter_count": data.get("chapter_count"),
        "type_distribution": data.get("type_distribution") or {},
        "unclassified_rate": data.get("unclassified_rate"),
        "counters_stripped": data.get("counters_stripped"),
        "violations": data.get("violations") or [],
    }
    return types, meta, []


def next_constraints(book: Path, chapter_id: int) -> dict:
    """计算本章跨章禁则。返回 dict，永不含 None 语义歧义。

    返回结构：
      {
        "forbidden_endings": [...],   # 禁用的章尾类型
        "forbidden_drives":  [],      # 禁用的主动力（细纲无该字段 → 恒为空，不猜）
        "basis": {...},               # 可审计依据：哪几章、各判为哪型、run 长度
        "evidence_quality": {...},    # 分类器可信度（U 占比等）
        "errors": [...],              # 状态缺失/执行失败 → 非空时禁则为空且不得采信
        "target_is_committed": bool,  # 目标章是否已被视为已提交（rewrite/重跑区分）
      }
    """
    cursor, errs = committed_cursor(book)
    if cursor is None:
        return {
            "forbidden_endings": [], "forbidden_drives": [],
            "basis": {}, "evidence_quality": {}, "errors": errs,
            "target_is_committed": False,
        }
    types, meta, terrs = tail_types(book, upto=cursor)
    errs = errs + terrs
    if errs:
        return {
            "forbidden_endings": [], "forbidden_drives": [],
            "basis": {}, "evidence_quality": meta, "errors": errs,
            "target_is_committed": bool(chapter_id <= cursor),
        }

    ordered = [types[c] for c in sorted(types) if c <= cursor]
    forbidden: list[str] = []
    basis: dict = {"tail_run": [], "cursor": cursor}
    if ordered:
        # 取末尾连续同型段（run）
        last = ordered[-1]
        run_len = 1
        for t in reversed(ordered[:-1]):
            if t == last:
                run_len += 1
            else:
                break
        basis["tail_run"] = [{"type": last, "run_len": run_len}]
        # U 不参与禁则（未分类 ≠ 一种类型；把 U 当类型会误禁）
        if last != "U" and run_len >= _RUN_WARN:
            forbidden.append(last)

    return {
        "forbidden_endings": forbidden,
        "forbidden_drives": [],  # 细纲 drive 字段仅 ch16–25 有，缺失即空，不猜
        "basis": basis,
        "evidence_quality": meta,
        "errors": [],
        "target_is_committed": bool(chapter_id <= cursor),
    }


def fingerprint(book: Path, chapter_id: int) -> tuple[str | None, list[str]]:
    """跨章状态内容指纹。**不用 mtime**（外脑 R5 明令禁止）。

    指纹输入 = 已提交游标 + 已提交章的章尾类型序列 + 已提交章正文内容。
    任一环节不可读 → 返回 (None, errors)，由调用方按状态错误处理（不是"可选输入"）。
    """
    cursor, errs = committed_cursor(book)
    if cursor is None:
        return None, errs
    types, _meta, terrs = tail_types(book, upto=cursor)
    if terrs:
        return None, terrs
    parts = {
        "schema": STATE_SCHEMA,
        "cursor": cursor,
        "types": {str(k): v for k, v in sorted(types.items())},
        "bodies": {},
    }
    body_dir = book / "正文"
    if body_dir.is_dir():
        for p in sorted(body_dir.rglob("第*章*.md")):
            if p.name.startswith("_"):
                continue
            try:
                parts["bodies"][p.name] = hashlib.md5(p.read_bytes()).hexdigest()
            except OSError:
                # 单章读不出 → 记 sentinel，不静默跳过（否则指纹会悄悄变松）
                parts["bodies"][p.name] = "UNREADABLE"
    return hashlib.md5(canonical_json(parts).encode("utf-8")).hexdigest(), []
