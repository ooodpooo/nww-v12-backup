#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nww_world_context_derive.py — 全局上下文派生（2026-10-12，外脑 ChatGPT 裁决 P1-3；2026-10-26 补齐 as_of 历史重写时点隔离）

外脑裁决（原文转述）：
  - A 主 B 辅：确定性派生为主，修 LLM 填报只做诊断，不得成为唯一状态来源
  - packet 新键 `derived_world_context`
  - 每条派生记录携带 kind / source_asset / source_chapter / evidence / as_of_chapter
  - kind 至少区分 ESTABLISHED_FACT / CURRENT_STATE / PLAN / COMMITMENT / UNRESOLVED
  - **PLAN 和 COMMITMENT 不得进入已发生事实集合**
  - UNRESOLVED 表示现有资料不足以确认，不代表事情没有发生
  - 「最近三章适合做近期上下文，不足以代表整个世界当前状态」→
    正式角色现状 / 未解决伏笔 / 长期约束必须从各自正式资产读取，不能只靠三章重建
  - **不写 state["global"]，不覆盖 全局状态.md**；落盘只是可重建的只读视图
  - **2026-10-26 千章不崩加固（as_of 时点隔离）**：
    重写第 N 章时，事实读取截止边界严格受限于 `as_of = min(_last_committed(book), chapter - 1)`，
    时间线、逐章记录、角色状态、未决伏笔统一执行 `<= as_of` 截止过滤与历史快照回溯，杜绝未来信息泄漏。

产物（可选落盘，只读）：追踪/派生追踪/派生视图/全局上下文_第NNN章.md

用法：
  python nww_world_context_derive.py --book X --chapter N [--json] [--write]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

SCHEMA_VERSION = "derived-world-context/1"

KIND_FACT = "ESTABLISHED_FACT"
KIND_STATE = "CURRENT_STATE"
KIND_PLAN = "PLAN"
KIND_COMMIT = "COMMITMENT"
KIND_UNRES = "UNRESOLVED"

# 预算（env 可调，一键关闭）
# 按「整条记录序列化后」的体积计预算 —— 只算 text 会低估约 4.8×
# （每条还有 kind/source_asset/evidence/as_of_chapter 四个字段）。
CAP_TOTAL = int(os.getenv("NWW_DWC_CHARS", "2400") or 2400)
CAP_RECENT_CH = int(os.getenv("NWW_DWC_RECENT_CH", "3") or 3)
CAP_ROLES = int(os.getenv("NWW_DWC_ROLES", "5") or 5)
CAP_FSH = int(os.getenv("NWW_DWC_FSH", "5") or 5)
CAP_TIMELINE = int(os.getenv("NWW_DWC_TIMELINE", "3") or 3)
CAP_REC_CHARS = int(os.getenv("NWW_DWC_RECCHARS", "150") or 150)


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def _rel(book: Path, p: Path) -> str:
    try:
        return str(p.relative_to(book))
    except Exception:
        return p.name


def _ledger_file(book: Path, chapter: int) -> Path | None:
    d = book / "追踪" / "逐章记录"
    if not d.is_dir():
        return None
    for pat in (f"第{chapter:03d}章*.md", f"第{chapter:02d}章*.md", f"第{chapter}章*.md"):
        c = [x for x in sorted(d.glob(pat)) if not x.name.startswith(".")]
        if c:
            return c[-1]
    return None


def _last_committed(book: Path) -> int:
    """已提交章号：取逐章记录最大章号（若 _tracking-state.json 存在且有效，以权威 state 上限封顶防崩溃半写泄漏）。"""
    d = book / "追踪" / "逐章记录"
    if not d.is_dir():
        return 0
    nums = []
    for p in d.glob("第*章.md"):
        m = re.search(r"第(\d+)章", p.name)
        if m:
            nums.append(int(m.group(1)))
    max_ledger = max(nums) if nums else 0
    state_file = book / "追踪" / "_tracking-state.json"
    if state_file.is_file():
        try:
            st = json.loads(state_file.read_text(encoding="utf-8"))
            st_last = int(st.get("last_committed_chapter") or 0)
            if st_last > 0 and max_ledger > 0:
                return min(max_ledger, st_last)
        except Exception:
            pass
    return max_ledger


def _load_snapshot_as_of(book: Path, as_of: int) -> dict | None:
    """历史重写时点回溯：若存在 追踪/快照/第{as_of:03d}章.json，读取当时完整状态快照。"""
    if as_of <= 0:
        return None
    snap_dir = book / "追踪" / "快照"
    if not snap_dir.is_dir():
        return None
    for name in (f"第{as_of:03d}章.json", f"第{as_of:02d}章.json", f"第{as_of}章.json"):
        p = snap_dir / name
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
    return None


def _section(txt: str, title: str) -> str:
    m = re.search(rf"^##\s*{re.escape(title)}\s*$(.*?)(?=^##\s|\Z)", txt, re.M | re.S)
    return m.group(1) if m else ""


def _bullets(body: str) -> list[str]:
    return [l.strip("- ").strip() for l in body.splitlines()
            if l.strip().startswith("-")]


def _kv_line(txt: str, key: str) -> str:
    """取 `- 键：值`；兼容 markdown 加粗键 `- **键**：值`（实测卷纲全用加粗）。"""
    m = re.search(rf"^\s*[-*]\s*\**\s*{re.escape(key)}\s*\**\s*[：:]\s*(.+)$", txt, re.M)
    return m.group(1).strip() if m else ""


# ---------------- 各来源派生 ----------------
def _from_recent_ledgers(book: Path, as_of: int) -> list[dict]:
    """最近 N 章「结果」→ ESTABLISHED_FACT（近期事件，不是整个世界状态）。"""
    out = []
    for ch in range(as_of, max(0, as_of - CAP_RECENT_CH), -1):
        p = _ledger_file(book, ch)
        if p is None:
            continue
        txt = _read(p)
        v = _kv_line(txt, "结果")
        if v:
            out.append({"kind": KIND_FACT, "text": v[:CAP_REC_CHARS],
                        "source_asset": _rel(book, p), "source_chapter": ch,
                        "evidence": f"逐章记录/第{ch:03d}章.md#结果",
                        "as_of_chapter": ch})
        cost = _bullets(_section(txt, "代价变化"))
        cost = [c for c in cost if c and c != "无"]
        for c in cost[:2]:
            out.append({"kind": KIND_FACT, "text": c[:CAP_REC_CHARS],
                        "source_asset": _rel(book, p), "source_chapter": ch,
                        "evidence": f"逐章记录/第{ch:03d}章.md#代价变化",
                        "as_of_chapter": ch})
    return out


def _from_role_states(book: Path, as_of: int) -> list[dict]:
    """正式角色现状 → CURRENT_STATE（从各自正式资产读；历史重写时优先回溯 as_of 快照防未来状态泄漏）。"""
    d = book / "追踪" / "角色状态"
    if not d.is_dir():
        return []
    last_c = _last_committed(book)
    snap = _load_snapshot_as_of(book, as_of) if as_of < last_c else None
    snap_chars = (snap.get("characters") or {}) if isinstance(snap, dict) else {}

    out = []
    for p in sorted(d.glob("*.md"))[:CAP_ROLES]:
        if p.name.startswith("."):
            continue
        role_name = p.stem
        if snap_chars:
            if role_name not in snap_chars:
                # 该角色在 as_of 时点尚未登场或不在热状态池中，跳过以防泄漏未来角色
                continue
            sc = snap_chars[role_name] if isinstance(snap_chars[role_name], dict) else {}
            bits = []
            for label, field in (("位置", "location"), ("当前目标", "current_goal"), ("身心状态", "physical_mental_state")):
                v = str(sc.get(field) or "").strip()
                if v and v != "—":
                    bits.append(f"{label}={v}")
            if not bits:
                continue
            out.append({"kind": KIND_STATE, "text": f"{role_name}｜" + "；".join(bits)[:CAP_REC_CHARS],
                        "source_asset": _rel(book, p), "source_chapter": as_of,
                        "evidence": f"{_rel(book, p)}#当前状态",
                        "as_of_chapter": as_of})
            continue

        txt = _read(p)
        # 若无历史快照但角色文件标注了首次登场章/更新章 > as_of，做保守过滤
        first_m = re.search(r"首次登场(?:章)?[：:]\s*第?(\d+)", txt)
        if first_m and int(first_m.group(1)) > as_of:
            continue
        bits = []
        for label in ("位置", "当前目标", "身心状态"):
            v = _kv_line(txt, label)
            if v and v != "—":
                bits.append(f"{label}={v}")
        if not bits:
            continue
        out.append({"kind": KIND_STATE, "text": f"{role_name}｜" + "；".join(bits),
                    "source_asset": _rel(book, p), "source_chapter": as_of,
                    "evidence": f"{_rel(book, p)}#当前状态",
                    "as_of_chapter": as_of})
    return out


def _from_constraints(book: Path, as_of: int) -> list[dict]:
    """长期约束（连贯性约束）→ ESTABLISHED_FACT，从截至 as_of 的正式资产读。"""
    p = _ledger_file(book, as_of)
    if p is None:
        return []
    items = [x for x in _bullets(_section(_read(p), "连贯性约束")) if x][:3]
    return [{"kind": KIND_FACT, "text": x[:CAP_REC_CHARS],
             "source_asset": _rel(book, p), "source_chapter": as_of,
             "evidence": f"{_rel(book, p)}#连贯性约束",
             "as_of_chapter": as_of} for x in items]


def _from_timeline(book: Path, as_of: int | None = None) -> list[dict]:
    """读者已知时间线最新条目 → ESTABLISHED_FACT（严格按认知截至章 <= as_of 过滤）。"""
    f = book / "追踪" / "时间线" / "读者已知.md"
    if not f.is_file():
        return []
    rows = [l.strip() for l in _read(f).splitlines() if l.strip().startswith("|")]
    if len(rows) < 3:
        return []
    head = [c.strip() for c in rows[0].strip("|").split("|")]
    if "ID" not in head or "读者当前认知" not in head:
        return []
    i_id, i_txt = head.index("ID"), head.index("读者当前认知")
    i_ch = head.index("认知截至章") if "认知截至章" in head else -1
    candidates = []
    for r in rows[2:]:
        c = [x.strip() for x in r.strip("|").split("|")]
        if len(c) <= max(i_id, i_txt):
            continue
        ch = None
        if 0 <= i_ch < len(c):
            m = re.search(r"(\d+)", c[i_ch])
            ch = int(m.group(1)) if m else None
        # 严格截止过滤：认知截至章 > as_of 的未来揭示条目一律排除
        if as_of is not None and ch is not None and ch > as_of:
            continue
        eff_ch = ch if ch is not None else as_of
        candidates.append({"kind": KIND_FACT, "text": f"读者已知：{c[i_txt][:CAP_REC_CHARS]}",
                           "source_asset": _rel(book, f), "source_chapter": eff_ch,
                           "evidence": f"{_rel(book, f)}#{c[i_id]}",
                           "as_of_chapter": eff_ch})
    return candidates[-CAP_TIMELINE:]


def _from_volume_plan(book: Path, chapter: int) -> list[dict]:
    """卷纲当前卷目标 → PLAN（明确标记为计划，不得混入已发生事实）。"""
    d = book / "大纲"
    if not d.is_dir():
        return []
    target = None
    for f in sorted(d.glob("卷纲_第*卷.md")):
        m = re.search(r"卷纲_第(\d+)卷", f.name)
        if not m:
            continue
        t = _read(f)
        r = re.search(r"章节范围[^0-9]{0,16}第?[ \t]*(\d+)[ \t]*[–\-~～—至][ \t]*(\d+)", t)
        if r and int(r.group(1)) <= chapter <= int(r.group(2)):
            target = f
            break
    if target is None:
        return []
    txt = _read(target)
    out = []
    for key in ("卷目标", "本卷目标", "卷核心冲突", "核心冲突",
                "核心目标（卷末状态）", "本卷主题", "本卷主线步进"):
        v = _kv_line(txt, key)
        if v:
            out.append({"kind": KIND_PLAN, "text": f"{key}：{v[:CAP_REC_CHARS]}",
                        "source_asset": _rel(book, target), "source_chapter": chapter,
                        "evidence": f"{_rel(book, target)}#{key}",
                        "as_of_chapter": None})
    return out


def _from_open_items(book: Path, chapter: int, as_of: int | None = None) -> tuple[list[dict], list[dict]]:
    """上一章承诺 → COMMITMENT；未回收伏笔 → UNRESOLVED（严格按 as_of 截断未来埋设与未来变更）。"""
    cutoff = as_of if as_of is not None else max(0, chapter - 1)
    commits, unres = [], []
    if cutoff >= 1:
        p = _ledger_file(book, cutoff)
        if p is not None:
            raw = _kv_line(_read(p), "下一章承诺")
            if raw:
                for seg in re.split(r"[；;]", raw):
                    s = seg.strip()
                    if s:
                        commits.append({"kind": KIND_COMMIT, "text": s[:CAP_REC_CHARS],
                                        "source_asset": _rel(book, p),
                                        "source_chapter": cutoff,
                                        "evidence": f"{_rel(book, p)}#下一章承诺",
                                        "as_of_chapter": cutoff})
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import nww_foreshadow_ledger as fl  # noqa: E402
        last_c = _last_committed(book)
        snap = _load_snapshot_as_of(book, cutoff) if cutoff < last_c else None
        snap_fsh = (snap.get("foreshadow") or {}) if isinstance(snap, dict) else {}

        for f in fl.parse_foreshadows(book):
            pc = f.get("planted_chapter")
            # 埋设章在 cutoff 之后的未来伏笔绝不进入当前章输入
            if pc is not None and pc > cutoff:
                continue
            fid = f["id"]
            if snap_fsh:
                if fid not in snap_fsh:
                    continue
                srow = snap_fsh[fid] if isinstance(snap_fsh[fid], dict) else {}
                st_at_cutoff = str(srow.get("status") or "已埋")
                if any(w in st_at_cutoff for w in ("回收", "已收", "闭合", "作废", "关闭")):
                    continue
                upd_ch = int(srow.get("updated_chapter") or pc or cutoff)
                eff_as_of = min(upd_ch, cutoff)
            else:
                last_ch = f.get("last_changed_chapter")
                # 若伏笔在 cutoff 之后才被回收（resolved_raw=True 且 last_changed_chapter > cutoff），
                # 则在 cutoff 时点它仍处于未回收状态！
                if f["resolved_raw"]:
                    if last_ch is None or last_ch <= cutoff:
                        continue
                eff_as_of = min(last_ch, cutoff) if last_ch is not None else (pc or cutoff)

            unres.append({"kind": KIND_UNRES,
                          "text": f"{fid}｜{f['content'][:CAP_REC_CHARS]}",
                          "source_asset": "追踪/伏笔.md",
                          "source_chapter": pc if pc is not None else cutoff,
                          "evidence": f"追踪/伏笔.md#{fid}",
                          "as_of_chapter": eff_as_of})
            if len(unres) >= CAP_FSH:
                break
    except Exception:
        pass
    return commits, unres


# ---------------- 主派生 ----------------
def derive(book: Path, chapter: int) -> dict:
    if CAP_TOTAL <= 0:
        return {}
    last_c = _last_committed(book)
    if last_c <= 0:
        return {}
    # 2026-10-26 千章不崩时点隔离修复：
    # 事实截止章 as_of 严格取 min(last_committed, chapter - 1)。
    # 正常续写（chapter == last_c + 1）时 as_of == last_c，行为与原逻辑 100% 一致；
    # 历史重写第 N 章（chapter <= last_c）时 as_of == chapter - 1，阻断 >= N 章的未来信息泄漏。
    as_of = min(last_c, max(0, chapter - 1)) if chapter > 0 else last_c
    if as_of <= 0:
        return {}
    recs: list[dict] = []
    # 顺序 = 同类内的保留优先级（sort 稳定）。读者已知时间线是**信息权限边界**，
    # 比「最近三章结果」更全局，必须优先于近期事件保留，否则会被配额挤掉。
    recs += _from_timeline(book, as_of)
    recs += _from_constraints(book, as_of)
    recs += _from_recent_ledgers(book, as_of)
    recs += _from_role_states(book, as_of)
    recs += _from_volume_plan(book, chapter)
    c, u = _from_open_items(book, chapter, as_of)
    recs += c + u

    # 预算裁剪：**按 kind 分配额**，不是全局抢预算。
    QUOTA = {KIND_FACT: 0.34, KIND_STATE: 0.20, KIND_UNRES: 0.16,
             KIND_COMMIT: 0.15, KIND_PLAN: 0.15}
    order = {KIND_FACT: 0, KIND_STATE: 1, KIND_UNRES: 2, KIND_COMMIT: 3, KIND_PLAN: 4}
    recs.sort(key=lambda r: order.get(r["kind"], 9))
    kept: list[dict] = []
    used_by_kind: dict[str, int] = {}
    for r in recs:
        k = r["kind"]
        cap = int(CAP_TOTAL * QUOTA.get(k, 0.1))
        n = len(json.dumps(r, ensure_ascii=False))
        if used_by_kind.get(k, 0) + n > cap:
            continue
        used_by_kind[k] = used_by_kind.get(k, 0) + n
        kept.append(r)
    refs = []
    for r in kept:
        if r["source_asset"] not in refs:
            refs.append(r["source_asset"])
    return {
        "schema_version": SCHEMA_VERSION,
        "as_of_chapter": as_of,
        "records": kept,
        "source_refs": refs,
        "counts": {k: sum(1 for r in kept if r["kind"] == k)
                   for k in (KIND_FACT, KIND_STATE, KIND_PLAN, KIND_COMMIT, KIND_UNRES)},
    }


def render_view(d: dict, chapter: int) -> str:
    L = ["# 全局上下文派生视图（只读）", "",
         f"> 第{chapter:03d}章写作时派生；截至第{d.get('as_of_chapter')}章。"
         f"由确定性逻辑重建，**不覆盖正式资产**，请勿手工编辑。", "",
         "## 语义分区", "",
         f"- `{KIND_FACT}` 已发生事实　- `{KIND_STATE}` 当前状态",
         f"- `{KIND_PLAN}` 计划（**不是**已发生）　- `{KIND_COMMIT}` 承诺（**不是**已发生）",
         f"- `{KIND_UNRES}` 资料不足以确认（**不代表没发生**）", ""]
    for kind in (KIND_FACT, KIND_STATE, KIND_UNRES, KIND_COMMIT, KIND_PLAN):
        rows = [r for r in d.get("records", []) if r["kind"] == kind]
        if not rows:
            continue
        L += [f"## {kind}（{len(rows)}）", ""]
        for r in rows:
            ch = f"第{r['source_chapter']}章" if r.get("source_chapter") else "章未标注"
            L.append(f"- {r['text']}")
            L.append(f"  - 来源 `{r['evidence']}`（{ch}）")
        L.append("")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", required=True)
    ap.add_argument("--chapter", type=int, required=True)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    book = Path(a.book)
    if not book.is_dir():
        print(json.dumps({"ok": False, "detail": f"书目录不存在: {book}"}, ensure_ascii=False))
        return 1
    d = derive(book, a.chapter)
    if a.write and d:
        p = book / "追踪" / "派生追踪" / "派生视图" / f"全局上下文_第{a.chapter:03d}章.md"
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(render_view(d, a.chapter), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"ok": False, "detail": str(exc)}, ensure_ascii=False))
            return 1
    if a.json:
        print(json.dumps({"ok": True, **d}, ensure_ascii=False, indent=2))
    else:
        print(f"截至第{d.get('as_of_chapter')}章　记录 {len(d.get('records', []))} 条　"
              f"{d.get('counts')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
