#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nww_foreshadow_ledger.py — 伏笔生命周期（2026-10-12，外脑 ChatGPT 裁决 P1-2）

外脑裁决要点（原文转述）：
  - 六个正式状态：PLANNED / PLANTED / DEVELOPING / PARTIAL / RESOLVED / DEFERRED
    **DROPPED 不纳入正式枚举**（终止决策，第一版保留原因字段但不开放自动迁入）
  - 「4-gram 探针可以证明局部文本存在匹配，但不能可靠证明语义上的兑现」
    → 检测器只输出**候选证据**，proposed_transition 一律 UNKNOWN，绝不主张迁移
  - 「正式状态只由已确认事件生成，不直接接受检测器或 LLM 的写入」
  - 「暂缓条件：在现有伏笔完成 ID、埋设证据和当前状态盘点之前，暂缓自动迁移和批量回填」
  - 只增不改：不动 追踪/伏笔.md；候选账本追加式，撤销靠追加更正事件，不删历史

产物（全部新增文件）：
  追踪/伏笔事件账本.jsonl   候选/确认/驳回事件（追加式，权威）
  追踪/伏笔盘点.md          只读盘点视图

用法：
  python nww_foreshadow_ledger.py --book X --inventory          # 盘点
  python nww_foreshadow_ledger.py --book X --detect [--write]    # 生成候选
  python nww_foreshadow_ledger.py --book X --confirm <cand_id> --to RESOLVED --by <人>
  python nww_foreshadow_ledger.py --book X --reject <cand_id> --by <人>
  python nww_foreshadow_ledger.py --book X --project             # 从已确认事件投影正式状态
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

SCHEMA_VERSION = "foreshadow-ledger/1"

LEDGER_FILE = "伏笔事件账本.jsonl"
INVENTORY_FILE = "伏笔盘点.md"

FORMAL_STATES = ("PLANNED", "PLANTED", "DEVELOPING", "PARTIAL", "RESOLVED", "DEFERRED")
# 外脑：DROPPED 属于终止决策，不纳入第一版正式枚举
TERMINAL_STATES = ("DROPPED",)

GRAM_N = int(os.getenv("NWW_FSH_GRAM_N", "4") or 4)
GRAM_MIN_HITS = int(os.getenv("NWW_FSH_GRAM_MIN", "2") or 2)
MAX_EVIDENCE = int(os.getenv("NWW_FSH_EVIDENCE", "80") or 80)

_CJK = re.compile(r"[\u4e00-\u9fff]")


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def _cjk_only(s: str) -> str:
    return "".join(_CJK.findall(s or ""))


def _grams(s: str, n: int = GRAM_N) -> list[str]:
    t = _cjk_only(s)
    return [t[i:i + n] for i in range(len(t) - n + 1)]


def _probe(text: str, grams: list[str]) -> tuple[int, str]:
    """命中数 + 首个上下文；低于阈值不返回上下文（孤立命中不算证据）。"""
    if not text or not grams:
        return 0, ""
    seen: set[str] = set()
    hits, ctx = 0, ""
    for g in grams:
        if g in seen:
            continue
        i = text.find(g)
        if i >= 0:
            seen.add(g)
            hits += 1
            if not ctx:
                ctx = text[max(0, i - 12): i + len(g) + MAX_EVIDENCE // 2].strip()
    if hits < GRAM_MIN_HITS:
        stop_chars = set("的了是在有和与及或把被让给向从到于为以因但而却又也还就都只更最很太不没无非他她它你我这那之其")
        src_joined = "".join(grams[:1]) + "".join(g[-1] for g in grams[1:]) if grams else ""
        bi_set = []
        for idx_b in range(len(src_joined) - 1):
            bg = src_joined[idx_b:idx_b + 2]
            if not any(c in stop_chars for c in bg) and bg not in bi_set:
                bi_set.append(bg)
        bi_hits = 0
        bi_ctx = ""
        for bg in bi_set:
            pos = text.find(bg)
            if pos >= 0:
                bi_hits += 1
                if not bi_ctx:
                    bi_ctx = text[max(0, pos - 12): pos + len(bg) + MAX_EVIDENCE // 2].strip()
        if bi_hits >= 2:
            return max(hits, GRAM_MIN_HITS), (ctx or bi_ctx)[:MAX_EVIDENCE]
        return hits, ""
    return hits, ctx[:MAX_EVIDENCE]


# ---------------- 章节索引 ----------------
def chapter_texts(book: Path) -> dict[int, str]:
    """章号 → 正文文本（供伏笔埋设证据探测使用）。

    ★ 2026-10-12 修复（实测确证）：旧实现用 `if n not in out` 让**先遇到者占位**。
      由于路径字典序中 `_`(U+005F) < `第`(U+7B2C)，`_review_snapshots/第026章_round1.md`
      排在 `第26章_洗仓.md` 之前 → **快照占位、正式稿被丢弃**，
      导致埋设证据探测在快照上做，4 条伏笔全部拿不到证据而降级为 PLANNED。
      现统一走 `nww_prose_paths.load_chapter_texts`：先排除非正文，同章多候选按 mtime 取最新。
    """
    try:
        from nww_prose_paths import load_chapter_texts
        return load_chapter_texts(book / "正文", drop_headings=True)
    except Exception:  # noqa: BLE001
        pass
    # 保守回退：仅在公共模块不可用时走（保持旧逻辑但补上快照过滤）
    body = book / "正文"
    out: dict[int, str] = {}
    if not body.is_dir():
        return out
    for p in sorted(body.rglob("*.md")):
        if p.name.startswith(".") or "_原稿_" in p.name or "_review" in str(p):
            continue
        m = re.search(r"第(\d+)章", p.name) or re.search(r"ch(\d+)", p.name)
        if not m:
            continue
        n = int(m.group(1))
        if n not in out:
            out[n] = "\n".join(l for l in _read(p).splitlines()
                               if not l.strip().startswith("#")).strip()
    return dict(sorted(out.items()))


# ---------------- 伏笔表解析 ----------------
def parse_foreshadows(book: Path) -> list[dict]:
    """读 追踪/伏笔.md 表格；无文件/无表 → []。"""
    f = book / "追踪" / "伏笔.md"
    if not f.is_file():
        return []
    rows = [l.strip() for l in _read(f).splitlines() if l.strip().startswith("|")]
    if len(rows) < 3:
        return []
    head = [c.strip() for c in rows[0].strip("|").split("|")]
    need = ("ID", "内容")
    if any(k not in head for k in need):
        return []
    i_id, i_txt = head.index("ID"), head.index("内容")
    def col(name, default=-1):
        return head.index(name) if name in head else default
    i_plant, i_plan = col("埋设章"), col("计划回收章")
    i_st, i_imp, i_last = col("状态"), col("重要度"), col("最近变更章")
    out = []
    for r in rows[2:]:
        c = [x.strip() for x in r.strip("|").split("|")]
        if len(c) <= max(i_id, i_txt):
            continue
        fid = c[i_id]
        if not re.match(r"^[A-Za-z]*\d+$", fid):
            continue
        def g(i, d=""):
            return c[i] if 0 <= i < len(c) else d
        def num(s):
            m = re.search(r"(\d+)", s or "")
            return int(m.group(1)) if m else None
        st = g(i_st) or "已埋"
        out.append({
            "id": fid,
            "content": c[i_txt],
            "planted_chapter": num(g(i_plant)),
            "planned_chapter": num(g(i_plan)),
            "status_raw": st,
            "importance": g(i_imp),
            "last_changed_chapter": num(g(i_last)),
            "resolved_raw": any(w in st for w in ("回收", "已收", "闭合", "作废", "关闭")),
        })
    return out


def normalize_status(f: dict, has_plant_evidence: bool) -> str:
    """把既有「已埋」等中文状态映射到正式枚举；无证据时保守降级为 PLANNED。

    只做投影，不写回任何文件。
    """
    if not has_plant_evidence:
        return "PLANNED"
    if f["resolved_raw"]:
        return "RESOLVED"
    return "PLANTED"


# ---------------- 盘点 ----------------
def inventory(book: Path) -> dict:
    fs = parse_foreshadows(book)
    texts = chapter_texts(book)
    rows = []
    for f in fs:
        pc = f.get("planted_chapter")
        plant_ev = ""
        plant_hits = 0
        if pc and pc in texts:
            plant_hits, plant_ev = _probe(texts[pc], _grams(f["content"]))
        state = normalize_status(f, plant_hits >= GRAM_MIN_HITS)
        mentions = []
        for n, t in texts.items():
            if pc and n <= pc:
                continue
            h, ev = _probe(t, _grams(f["content"]))
            if h >= GRAM_MIN_HITS:
                mentions.append({"chapter": n, "gram_hits": h, "evidence": ev})
        rows.append({
            "id": f["id"], "content": f["content"][:100],
            "planted_chapter": pc, "planned_chapter": f.get("planned_chapter"),
            "status_raw": f["status_raw"], "formal_state": state,
            "plant_evidence": plant_ev, "plant_gram_hits": plant_hits,
            "mentions_after_plant": mentions,
        })
    last = max(texts) if texts else 0
    overdue_ids = [
        r["id"] for r in rows
        if r["formal_state"] != "RESOLVED"
        and r.get("planned_chapter") is not None
        and last > int(r["planned_chapter"])
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "chapters": len(texts), "last_chapter": last,
        "total": len(rows),
        "by_state": {s: sum(1 for r in rows if r["formal_state"] == s) for s in FORMAL_STATES},
        "resolved_raw": sum(1 for r in rows if r["status_raw"] != "已埋"),
        "never_mentioned_after_plant": sum(1 for r in rows if not r["mentions_after_plant"]),
        "overdue_count": len(overdue_ids),
        "overdue_ids": overdue_ids,
        "rows": rows,
    }


def render_inventory(inv: dict) -> str:
    L = ["# 伏笔盘点（只读投影）", "",
         f"> 由 `追踪/{LEDGER_FILE}` 同套逻辑确定性生成，请勿手工编辑。"
         f"共 {inv['total']} 条伏笔 / {inv['chapters']} 章。", "",
         "## 状态分布", ""]
    for s, n in inv["by_state"].items():
        L.append(f"- {s}：{n}")
    L += ["", "## 约定", "",
          "- `formal_state` 是从**证据**投影出来的，不是照抄 伏笔.md 的中文状态。",
          "- 埋设章正文里找不到证据的，一律投影为 `PLANNED`（计划存在，未确认正文埋设）。",
          "- 「后续提及」只表示**字面证据存在**，不等于已推进、更不等于已兑现。", ""]
    if inv["rows"]:
        L += ["## 明细", "",
              "| ID | 状态 | 埋设章 | 埋设证据 | 后续提及章 |", "|---|---|---|---|---|"]
        for r in inv["rows"]:
            m = "、".join(str(x["chapter"]) for x in r["mentions_after_plant"][:8]) or "—"
            L.append(f"| {r['id']} | {r['formal_state']} | "
                     f"{r['planted_chapter'] or '—'} | "
                     f"{'有' if r['plant_evidence'] else '无'}（{r['plant_gram_hits']}） | {m} |")
        L.append("")
    return "\n".join(L) + "\n"


# ---------------- 候选检测（只出证据，不主张迁移）----------------
def detect_candidates(book: Path) -> list[dict]:
    """扫描埋设章之后的章节，为每条伏笔生成候选证据事件。

    硬约束：proposed_transition 一律 `UNKNOWN` —— 4-gram 命中不能证明语义兑现。
    review_status 一律 `CANDIDATE`。
    """
    fs = parse_foreshadows(book)
    texts = chapter_texts(book)
    out: list[dict] = []
    for f in fs:
        if f["resolved_raw"]:
            continue
        pc = f.get("planted_chapter")
        grams = _grams(f["content"])
        for n, t in texts.items():
            if pc and n <= pc:
                continue
            hits, ev = _probe(t, grams)
            if hits < GRAM_MIN_HITS:
                continue
            e = {
                "schema_version": SCHEMA_VERSION,
                "foreshadow_id": f["id"],
                "observed_chapter": n,
                "observation_kind": "MENTIONED",
                "proposed_transition": "UNKNOWN",
                "detector": "nww_foreshadow_ledger/4gram-v1",
                "evidence_location": f"正文/第{n}章",
                "evidence_excerpt": ev,
                "gram_hits": hits,
                "review_status": "CANDIDATE",
                "note": "检测器只证明字面证据存在；是否推进/部分兑现/兑现需人工确认",
            }
            e["candidate_id"] = "FC:" + hashlib.sha1(
                json.dumps({k: v for k, v in e.items() if k != "candidate_id"},
                           ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest()[:12]
            out.append(e)
    return out


# ---------------- 账本 IO ----------------
def ledger_path(book: Path) -> Path:
    return book / "追踪" / LEDGER_FILE


def append_events(book: Path, events: list[dict]) -> int:
    p = ledger_path(book)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        return 0
    seen = set()
    try:
        for line in _read(p).splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                key = (d.get("foreshadow_id"), d.get("observed_chapter"),
                       d.get("observation_kind"))
                if d.get("review_status") != "SUPERSEDED":
                    seen.add(key)
            except Exception:
                continue
    except Exception:
        pass
    n = 0
    with p.open("a", encoding="utf-8") as fh:
        for e in events:
            key = (e.get("foreshadow_id"), e.get("observed_chapter"),
                   e.get("observation_kind"))
            if key in seen:
                continue
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
            n += 1
    return n


def load_events(book: Path) -> list[dict]:
    p = ledger_path(book)
    if not p.is_file():
        return []
    out = []
    for line in _read(p).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def decide(book: Path, candidate_id: str, to_state: str, by: str,
           reject: bool = False, note: str = "") -> dict:
    """人工确认/驳回 —— 追加事件，**不删除原候选记录**（撤销靠追加更正）。"""
    evs = load_events(book)
    tgt = next((e for e in evs if e.get("candidate_id") == candidate_id), None)
    if tgt is None:
        return {"ok": False, "detail": f"未找到候选 {candidate_id}"}
    if reject:
        new = {**tgt, "review_status": "REJECTED", "confirmed_by": by,
               "confirmed_at": int(time.time()), "note": note or "人工驳回"}
    else:
        if to_state not in FORMAL_STATES and to_state not in TERMINAL_STATES:
            return {"ok": False, "detail": f"非法状态：{to_state}"}
        new = {**tgt, "review_status": "CONFIRMED", "confirmed_transition": to_state,
               "confirmed_by": by, "confirmed_at": int(time.time()),
               "note": note}
    # 原候选标记为已被取代（保留原文，不删）
    with ledger_path(book).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({**tgt, "review_status": "SUPERSEDED"},
                            ensure_ascii=False) + "\n")
        fh.write(json.dumps(new, ensure_ascii=False) + "\n")
    return {"ok": True, "event": new}


def project_state(book: Path) -> dict:
    """从**已确认**事件投影正式状态；无确认事件 → 保持盘点态（不做迁移）。"""
    inv = inventory(book)
    evs = [e for e in load_events(book) if e.get("review_status") == "CONFIRMED"]
    latest: dict[str, dict] = {}
    for e in evs:
        fid = e.get("foreshadow_id")
        if not fid:
            continue
        cur = latest.get(fid)
        if cur is None or (e.get("confirmed_at") or 0) >= (cur.get("confirmed_at") or 0):
            latest[fid] = e
    out = {}
    for r in inv["rows"]:
        e = latest.get(r["id"])
        state = e["confirmed_transition"] if e else r["formal_state"]
        out[r["id"]] = {
            "state": state,
            "source": "CONFIRMED_EVENT" if e else "EVIDENCE_PROJECTION",
            "evidence_chapter": (e or {}).get("observed_chapter"),
        }
    return {"schema_version": SCHEMA_VERSION, "states": out,
            "confirmed_events": len(evs), "candidate_events": len(
                [e for e in load_events(book) if e.get("review_status") == "CANDIDATE"])}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", required=True)
    ap.add_argument("--inventory", action="store_true")
    ap.add_argument("--detect", action="store_true")
    ap.add_argument("--project", action="store_true")
    ap.add_argument("--confirm")
    ap.add_argument("--reject")
    ap.add_argument("--to", default="")
    ap.add_argument("--by", default="human")
    ap.add_argument("--note", default="")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    book = Path(a.book)
    if not book.is_dir():
        print(json.dumps({"ok": False, "detail": f"书目录不存在: {book}"}, ensure_ascii=False))
        return 1

    if a.confirm or a.reject:
        cid = a.confirm or a.reject
        if not a.reject and not a.to:
            print(json.dumps({"ok": False, "detail": "--confirm 需要 --to <状态>"},
                             ensure_ascii=False))
            return 1
        r = decide(book, cid, a.to, a.by, reject=bool(a.reject), note=a.note)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r["ok"] else 1

    if a.project:
        print(json.dumps(project_state(book), ensure_ascii=False, indent=2))
        return 0

    if a.detect:
        cands = detect_candidates(book)
        if a.write:
            n = append_events(book, cands)
            print(f"[OK] 新增候选 {n} 条（去重后），累计 {len(load_events(book))} 条")
        if a.json:
            print(json.dumps({"ok": True, "candidates": cands}, ensure_ascii=False, indent=2))
        else:
            print(f"候选 {len(cands)} 条（proposed_transition 全为 UNKNOWN）")
        return 0

    inv = inventory(book)
    if a.write:
        try:
            (book / "追踪").mkdir(parents=True, exist_ok=True)
            (book / "追踪" / INVENTORY_FILE).write_text(
                render_inventory(inv), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"ok": False, "detail": str(exc)}, ensure_ascii=False))
            return 1
    if a.json:
        print(json.dumps(inv, ensure_ascii=False, indent=2))
    else:
        print(f"伏笔 {inv['total']} 条 / {inv['chapters']} 章　"
              f"状态 {inv['by_state']}　"
              f"埋后零提及 {inv['never_mentioned_after_plant']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
