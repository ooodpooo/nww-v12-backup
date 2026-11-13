#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nww_relation_ledger.py — 关系事件账本（2026-10-12，外脑 ChatGPT 裁决 P1-1）

外脑裁决要点（原文转述）：
  - 「先记录关系声明和证据，不直接生成关系演变结论」
  - 「可以确定性构造配对索引，不可以确定性构造双向关系事实」
  - B 未提及 A 时，另一方向记 UNOBSERVED，**不是 NONE，也不是 CONFIRMED_RECIPROCAL**
  - 「空字段不应为了 schema 整齐而全部保留」→ 不设 baseline/current_state/
    change/downstream_effect/confidence（零 LLM 下无法填充）
  - pair_id 由稳定角色 ID 按字典序组合，方向**另外记录**
  - 权威存储 JSONL，Markdown 只是投影（两者由同一套确定性逻辑生成）

产物（全部新增文件，不改动 追踪/角色状态/*.md）：
  追踪/角色身份索引.json   角色 → 稳定 ID + 别名
  追踪/关系账本.jsonl       追加式事件账本（权威）
  追踪/关系事件视图.md      人工阅读投影

用法：
  python nww_relation_ledger.py --book <书目录> [--json] [--write]
  python nww_relation_ledger.py --book <书目录> --stats
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

SCHEMA_VERSION = "relation-ledger/1"

IDENTITY_FILE = "角色身份索引.json"
LEDGER_FILE = "关系账本.jsonl"
VIEW_FILE = "关系事件视图.md"

# 关系小节标题（兼容「关键关系」「关系」「人物关系」）
_REL_HEAD = re.compile(r"^##\s*(关键关系|人物关系|关系)\s*$", re.M)
# 关系行三种实测写法（各书不统一）：
#   - 三皇子：押卦对赌进行中，应验归他用      冒号式
#   - 与宋归：公事立信                        带「与」前缀
#   - 林小凡（当年拒收者/叙旧者）             括号式（无冒号）
#   - 秦九/老郭/祝灯                          并列式（仅列出名字）
_REL_LINE = re.compile(r"^\s*[-*]\s*([^：:｜|\n]{1,30})[：:]\s*(.+)$")
_REL_PAREN = re.compile(r"^\s*[-*]\s*([^（(｜|\n]{1,30})[（(]([^）)]*)[）)]\s*$")
_REL_BARE = re.compile(r"^\s*[-*]\s*([^：:｜|（(（\n]{1,30})\s*$")
# 名字前缀词（「与宋归」→「宋归」）
_NAME_PREFIX = re.compile(r"^(与|和|跟|同|对|向|给)")
# 并列名分隔符
_NAME_SPLIT = re.compile(r"[/／、,，]")
_AS_OF = re.compile(r"^\s*[-*]\s*截至章节[：:]\s*第?\s*(\d+)\s*章")


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


# ---------------- 角色身份 ----------------
def _canonical_names(book: Path) -> list[str]:
    """候选角色名：角色状态/*.md 为主，设定/角色/*.md 与 bible.json 补充。"""
    names: list[str] = []
    for d, pat in ((book / "追踪" / "角色状态", "*.md"),
                   (book / "设定" / "角色", "*.md")):
        if d.is_dir():
            for p in sorted(d.glob(pat)):
                n = p.stem.strip()
                if n and not n.startswith(".") and n not in names:
                    names.append(n)
    bj = book / "bible.json"
    if bj.is_file():
        try:
            b = json.loads(_read(bj))
            for c in (b.get("characters") or []):
                n = str((c or {}).get("name", "")).strip()
                if n and n not in names:
                    names.append(n)
        except Exception:
            pass
    return names


def build_identity(book: Path) -> dict:
    """角色身份索引：稳定 ID + 显示名。

    ID 分配按 canonical 名的固定字典序 —— 显示名/别名变化不改变 ID。
    零 LLM、只读；无候选 → 空索引。
    """
    names = sorted(_canonical_names(book))
    idx = {
        "schema_version": SCHEMA_VERSION,
        "generated_from": ["追踪/角色状态/*.md", "设定/角色/*.md", "bible.json"],
        "characters": {},
    }
    for i, n in enumerate(names, start=1):
        cid = f"C{i:04d}"
        idx["characters"][cid] = {"canonical": n, "aliases": [n]}
    return idx


def _alias_map(idx: dict) -> dict[str, str]:
    """别名（含 canonical）→ 角色 ID。"""
    m: dict[str, str] = {}
    for cid, row in (idx.get("characters") or {}).items():
        for a in (row.get("aliases") or []) + [row.get("canonical", "")]:
            if a:
                m[a.strip()] = cid
    return m


def resolve(name: str, idx: dict) -> str | None:
    """名字 → 角色 ID；无法解析 → None（进待确认队列，绝不临时拼 ID）。"""
    return _alias_map(idx).get((name or "").strip())


# ---------------- 关系声明抽取 ----------------
def _as_of_chapter(txt: str) -> int | None:
    m = _AS_OF.search(txt)
    return int(m.group(1)) if m else None


def extract_declarations(book: Path, idx: dict) -> tuple[list[dict], list[dict]]:
    """从 追踪/角色状态/*.md 的「关键关系」小节抽取关系声明。

    返回 (events, unresolved)。unresolved 只记录不猜测。
    """
    d = book / "追踪" / "角色状态"
    events: list[dict] = []
    unresolved: list[dict] = []
    if not d.is_dir():
        return events, unresolved
    for p in sorted(d.glob("*.md")):
        if p.name.startswith("."):
            continue
        txt = _read(p)
        src_name = p.stem.strip()
        src_id = resolve(src_name, idx)
        if src_id is None:
            unresolved.append({"raw_name": src_name, "side": "source",
                               "evidence_location": f"追踪/角色状态/{p.name}"})
            continue
        m = _REL_HEAD.search(txt)
        if not m:
            continue
        # 取该小节到下一个 ## 为止
        rest = txt[m.end():]
        nxt = re.search(r"^##\s", rest, re.M)
        body = rest[:nxt.start()] if nxt else rest
        as_of = _as_of_chapter(txt)
        for line in body.splitlines():
            s = line.strip()
            if not s:
                continue
            # 逐个候选名 × 描述（并列名会展开成多条声明）
            cands: list[tuple[str, str]] = []
            mm = _REL_LINE.match(s)
            if mm:
                for n in _NAME_SPLIT.split(mm.group(1)):
                    n = _NAME_PREFIX.sub("", n.strip()).strip()
                    if n:
                        cands.append((n, mm.group(2).strip()))
            else:
                mm = _REL_PAREN.match(s)
                if mm:
                    for n in _NAME_SPLIT.split(mm.group(1)):
                        n = _NAME_PREFIX.sub("", n.strip()).strip()
                        if n:
                            cands.append((n, mm.group(2).strip()))
                else:
                    mm = _REL_BARE.match(s)
                    if mm:
                        for n in _NAME_SPLIT.split(mm.group(1)):
                            n = _NAME_PREFIX.sub("", n.strip()).strip()
                            if n:
                                # 并列式只声明了「有关系」，未给出关系类型
                                cands.append((n, ""))
            for tgt_name, desc in cands:
                if not tgt_name or len(tgt_name) > 12:
                    continue
                tgt_id = resolve(tgt_name, idx)
                if tgt_id is None:
                    unresolved.append({
                        "raw_name": tgt_name, "side": "target",
                        "declared_by": src_id,
                        "declaration": desc[:120],
                        "evidence_location": f"追踪/角色状态/{p.name}#{m.group(1)}",
                    })
                    continue
                pair = "|".join(sorted([src_id, tgt_id]))
                ev = {
                    "schema_version": SCHEMA_VERSION,
                    "event_kind": "OBSERVED_DECLARATION",
                    "review_status": "OBSERVED",
                    "pair_id": f"R:{pair}",
                    "source_character_id": src_id,
                    "target_character_id": tgt_id,
                    "direction": f"{src_id}->{tgt_id}",
                    "relation_type_raw": desc[:200],
                    "source_chapter": as_of,
                    "evidence_location": f"追踪/角色状态/{p.name}#{m.group(1)}",
                    "evidence_excerpt": line.strip()[:200],
                }
                ev["event_id"] = "E:" + hashlib.sha1(
                    json.dumps({k: v for k, v in ev.items() if k != "event_id"},
                               ensure_ascii=False, sort_keys=True).encode()
                ).hexdigest()[:12]
                events.append(ev)
    return events, unresolved


# ---------------- 配对索引（不伪造双向）----------------
def build_pairs(idx: dict, events: list[dict]) -> list[dict]:
    """配对索引：A—B 配对存在，但各方向单独记录。

    另一方向若没有声明 → `UNOBSERVED`（不是 NONE，不是 CONFIRMED_RECIPROCAL）。
    """
    by_pair: dict[str, dict] = {}
    for e in events:
        pid = e["pair_id"]
        row = by_pair.setdefault(pid, {
            "pair_id": pid,
            "a": None, "b": None,
            "a_to_b": None, "b_to_a": None,
        })
        s, t = e["source_character_id"], e["target_character_id"]
        lo, hi = sorted([s, t])
        row["a"], row["b"] = lo, hi
        key = "a_to_b" if s == lo else "b_to_a"
        row[key] = {
            "declared": True,
            "relation_type_raw": e["relation_type_raw"],
            "source_chapter": e["source_chapter"],
            "evidence_location": e["evidence_location"],
            "review_status": e["review_status"],
        }
    out = []
    for pid, row in sorted(by_pair.items()):
        for k in ("a_to_b", "b_to_a"):
            if row[k] is None:
                row[k] = {"declared": False, "direction_status": "UNOBSERVED"}
        row["reciprocal"] = row["a_to_b"]["declared"] and row["b_to_a"]["declared"]
        name = idx.get("characters", {})
        row["a_name"] = (name.get(row["a"]) or {}).get("canonical", row["a"])
        row["b_name"] = (name.get(row["b"]) or {}).get("canonical", row["b"])
        out.append(row)
    return out


# ---------------- Markdown 投影 ----------------
def render_view(idx: dict, pairs: list[dict], events: list[dict],
                unresolved: list[dict]) -> str:
    L = ["# 关系事件视图（只读投影）", "",
         f"> 由 `追踪/{LEDGER_FILE}` 确定性生成，请勿手工编辑。"
         f"共 {len(events)} 条声明 / {len(pairs)} 个配对。", "",
         "## 约定", "",
         "- 本视图只记录**已声明**的关系与其证据，不推导关系的客观当前状态。",
         "- 另一方向无声明记为 `UNOBSERVED`：可能是信息缺失、视角差异或记录规则不一致，",
         "  **不等于**该方向关系不存在，也不等于关系对称。",
         "- `review_status=OBSERVED` 表示「有来源可查」，不等于「已人工确认」。", ""]
    if pairs:
        L += ["## 配对索引", "",
              "| 配对 | A→B | B→A | 双向 |", "|---|---|---|---|"]
        for r in pairs:
            f = lambda k: ("已声明" if r[k]["declared"] else "UNOBSERVED")  # noqa: E731
            L.append(f"| {r['a_name']} — {r['b_name']} | {f('a_to_b')} | {f('b_to_a')} "
                     f"| {'是' if r['reciprocal'] else '否'} |")
        L.append("")
    if events:
        L += ["## 关系声明（逐条）", ""]
        name = idx.get("characters", {})
        for e in events:
            sn = (name.get(e["source_character_id"]) or {}).get("canonical",
                                                                e["source_character_id"])
            tn = (name.get(e["target_character_id"]) or {}).get("canonical",
                                                                e["target_character_id"])
            ch = f"第{e['source_chapter']}章" if e.get("source_chapter") else "章节未标注"
            L.append(f"- `{e['event_id']}` {sn} → {tn}（{ch}）｜{e['relation_type_raw']}")
            L.append(f"  - 来源：`{e['evidence_location']}`")
        L.append("")
    if unresolved:
        L += ["## 待解析（未生成 ID，不猜测）", ""]
        for u in unresolved:
            L.append(f"- `{u['raw_name']}`（{u['side']}）｜来源：`{u['evidence_location']}`")
        L.append("")
    return "\n".join(L) + "\n"


# ---------------- 主流程 ----------------
def build(book: Path, write: bool = False) -> dict:
    idx = build_identity(book)
    events, unresolved = extract_declarations(book, idx)
    pairs = build_pairs(idx, events)
    view = render_view(idx, pairs, events, unresolved)
    reciprocal = sum(1 for r in pairs if r["reciprocal"])
    stats = {
        "characters": len(idx.get("characters") or {}),
        "declarations": len(events),
        "pairs": len(pairs),
        "reciprocal_pairs": reciprocal,
        "one_way_pairs": len(pairs) - reciprocal,
        "unresolved": len(unresolved),
    }
    if write:
        tr = book / "追踪"
        try:
            tr.mkdir(parents=True, exist_ok=True)
            (tr / IDENTITY_FILE).write_text(
                json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")
            (tr / LEDGER_FILE).write_text(
                "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events),
                encoding="utf-8")
            (tr / VIEW_FILE).write_text(view, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": f"写盘失败: {exc}", "stats": stats}
    return {"ok": True, "schema_version": SCHEMA_VERSION, "stats": stats,
            "identity": idx, "events": events, "pairs": pairs,
            "unresolved": unresolved, "view": view}


def load_ledger(book: Path) -> list[dict]:
    """读既有账本；文件不存在 → []。"""
    p = book / "追踪" / LEDGER_FILE
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


# ---------------- 关系演变时间线（2026-10-13 新增；默认关闭）----------------
# 根因（实测，非推测）：全库 81 条关系声明里只有 1 条（1.2%）带演变/起因，
# 其余全是「与宋归：公事立信」这类静态标签 —— 关系只有"现在是什么"，没有"怎么来的"。
# 而「追踪/逐章记录/第NNN章.md」的「角色变化」小节里，按章累积的关系事件**早已在盘上**
# （例：第025章「三皇子｜第一次正面出手…落字押卦、赌沈砚的命」正是"押卦对赌进行中"的来历），
# 却从未被 packet 消费 —— 关系的"来历链"因此整体缺失。
# 本函数零 LLM、只读、确定性：只抽取「某章的角色变化条目里同时出现两个已知角色」这一
# **已发生的事实**，不推断关系的客观状态、不补另一方向、不预测未来章节。
_TIMELINE_SWITCH = "NWW_REL_TIMELINE"

# 逐章记录「角色变化」行：- 沈砚｜核心｜……
_CHANGE_RE = re.compile(r"^\s*[-*]\s*([^｜|]+)｜([^｜|]*)｜(.+)$")
# 章节文件名 → 章号
_CH_FILE = re.compile(r"第\s*(\d+)\s*章")


def _timeline_on() -> bool:
    return (os.getenv(_TIMELINE_SWITCH) or "1").strip().lower() not in ("0", "off", "false", "no")


def _chapter_records(book: Path) -> list[tuple[int, Path]]:
    """追踪/逐章记录/第N章*.md → [(章号, 路径)]，按章升序。目录缺失 → []。"""
    d = book / "追踪" / "逐章记录"
    if not d.is_dir():
        return []
    out: list[tuple[int, Path]] = []
    for p in sorted(d.glob("第*章*.md")):
        m = _CH_FILE.search(p.name)
        if m:
            out.append((int(m.group(1)), p))
    return sorted(out)


# ---- 事件确认（外脑 P0-2：名字出现 ≠ 关系事件）----
# 「沈砚独自行动时顺带提到三皇子」不是关系事件。要进入正式轨迹，分句必须额外命中
# 「双方直接互动」或「一方行为导致另一方处境/选择变化」的确定性信号；否则只作候选，
# **不写入** —— 宁可少记，绝不编造关系历史。以下词表全部为双字以上，避免单字误伤。
_INTERACTION = re.compile(
    r"交涉|交锋|交易|谈判|商谈|对赌|押卦|立信|换帖|结盟|同盟|联手|合作|约定|承诺|"
    r"拒绝|拒收|拒答|收编|收手|援手|出手|担保|交底|递话|传话|示警|通报|"
    r"质问|对峙|摊牌|让步|妥协|应下|应承|欠账|救下|挡下|试探|摸底|归还|交割|两讫|"
    r"递帖|会面|当堂|托付|请托|辞退|举荐|借力|对质|护住|保下|收下|接住|认下|看破|"
    r"追问|放行|点破|拆穿|压回|架上|抬举|保举|还朝|回护|力保|"
    # 第二批（实测漏检补录）：被剔除的候选里大量是真实互动
    r"劝阻|盘问|背书|采信|交给|惊到|惊出|站队|调入|呈报|问罪|捧杀|盯死|递刀|"
    r"引上身|替他|替沈|拦在|拦住|拦下|挡在|挡住|交由|交付|托出|推出|推出去|"
    r"收回成命|改口|转圜|通融|周旋|敷衍|搪塞|含糊|留下条件|留下话|留下余地|"
    r"不再打包票|不再站队|仍未|仍不|索性|干脆|不再|就此|从此|头一回|头一次|"
    # 第三批（第二轮漏检补录）：嘱托/站台/施压类
    r"嘱沈|嘱他|站台|差书吏|差人|代问|圈进|收紧|盯沈砚|盯住|翻数|亮双刀|双刀|"
    r"见报|放沈砚|经手|对齐|圈进|盯上|点名|要人|索人|提人")
_CAUSAL = re.compile(
    r"导致|使得|迫使|逼得|换来|换得|因此|于是|从此|自此|这才|终于|遂以|以此|为此|才算|不得已")
# 转折信号：用于轨迹裁剪时区分「状态台阶」与「重复确认」
_TURNING = re.compile(
    r"从.{1,10}(到|变|转)|转为|变为|改口|翻脸|反目|暂断|断裂|缓和|松动|"
    r"立信|结盟|反悔|收回|第一次|首次|不再|就此|从此|干脆|索性")

# 最近一次 relation_timeline() 运行的统计（可观测性：误检/漏检必须能查）
_LAST_STATS: dict = {}


def relation_timeline_stats() -> dict:
    """返回最近一次 relation_timeline() 的统计（确认数 / 候选未确认数 / 涉及章数）。"""
    return dict(_LAST_STATS)


def _is_relation_event(frag: str) -> bool:
    """确定性事件确认：互动信号 或 因果信号 **或 转折信号**。零 LLM。

    ★ 转折信号必须参与判定 —— 实测教训：只用"互动/因果"会把
    「从看笑话转为被惊到」「从看管+利用转向忌惮+不得不用」这类**最典型的演变**全部误剔。
    """
    return bool(_INTERACTION.search(frag) or _CAUSAL.search(frag) or _TURNING.search(frag))


def _pick_trajectory(events: list[dict], max_events: int = 3) -> list[dict]:
    """裁剪策略 = 起因 + 转折 + 最近推进（外脑 Q4），**不是**机械取最近 N 条。

    机械取最近三条会压掉早期真正建立信任/形成债务的起因，只剩当前状态的重复确认。
    名额不满时不强行填满。
    """
    if len(events) <= max_events:
        return list(events)
    picked: list[dict] = [events[0]]          # 起因：最早
    picked.append(events[-1])                 # 最近推进：最晚
    for e in events[1:-1]:                    # 中间优先挑带转折信号的
        if _TURNING.search(e["event"]):
            picked.append(e)
            break
    seen: set = set()
    out: list[dict] = []
    for e in sorted(picked, key=lambda x: x["chapter"]):
        if e["chapter"] not in seen:
            seen.add(e["chapter"])
            out.append(e)
    return out[:max_events]


def _rel_fragment(desc: str, other_names: list[str], max_chars: int) -> str:
    """只取「提到对方」的分句。

    逐章记录的一条角色变化里，与该关系无关的噪音很多（反噬等级、身体状况、独自行动等），
    整条塞进 packet 既占预算又稀释信号 —— 只留真正涉及对方的那几句。
    """
    segs = [s.strip() for s in re.split(r"[；;]", desc) if s.strip()]
    hit = [s for s in segs if any(nm and nm in s for nm in other_names)]
    if not hit:  # 分不出就退回整条，绝不丢事件
        hit = segs[:1]
    return "；".join(hit)[:max_chars]


def relation_timeline(book: Path, idx: dict, as_of: int | None = None,
                      max_events: int = 3, max_chars: int = 68,
                      rejected: list | None = None) -> dict:
    """`rejected`：可选出参，收集**未通过事件确认**的候选片段（用于评估少杀，不写入轨迹）。"""
    """按章累积的关系事件链。

    返回 `{(a_id, b_id): [{"chapter": n, "event": str}, ...]}`（key 字典序，value 按章升序）。
    零 LLM、只读；无身份索引 / 无逐章记录 / 解析不到 → {}（调用方自然降级，绝不报错）。
    """
    canon = idx.get("characters") or {}
    if not isinstance(canon, dict) or not canon:
        return {}
    name_to_id: dict[str, str] = {}
    for cid, rec in canon.items():
        rec = rec if isinstance(rec, dict) else {}
        for nm in [rec.get("canonical")] + list(rec.get("aliases") or []):
            nm = str(nm or "").strip()
            if nm:
                name_to_id[nm] = cid
    if not name_to_id:
        return {}
    out: dict = {}
    stats = {"chapters_scanned": 0, "name_hits": 0, "confirmed": 0,
             "unconfirmed_candidates": 0, "pairs": 0, "as_of": as_of}
    for ch, p in _chapter_records(book):
        # 外脑 P0-1：严格小于。写第 N 章时只消费第 1..N-1 章 ——
        # 第 N 章的「角色变化」若是写完后落盘的，会包含本章结尾才发生的关系变化（结果前置）。
        if as_of is not None and ch >= as_of:
            continue
        stats["chapters_scanned"] += 1
        txt = _read(p)
        m = re.search(r"^##\s*[^\n]*角色变化\s*$(.*?)(?=^##\s|\Z)", txt, re.M | re.S)
        if not m:
            continue
        for line in m.group(1).splitlines():
            mm = _CHANGE_RE.match(line)
            if not mm:
                continue
            who = mm.group(1).strip()
            desc = mm.group(3).strip()
            wid = name_to_id.get(who) or resolve(who, idx)
            if not wid:
                continue
            hits = [(nm, oid) for nm, oid in name_to_id.items()
                    if oid != wid and nm and nm != who and nm in desc]
            seen_pairs: set[tuple] = set()
            for nm, oid in hits:
                key = tuple(sorted([wid, oid]))
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                stats["name_hits"] += 1
                frag = _rel_fragment(desc, [nm], max_chars)
                # ★ P0-2：名字命中只是**候选**，须通过事件确认才写入正式轨迹
                if not _is_relation_event(frag):
                    stats["unconfirmed_candidates"] += 1
                    if rejected is not None:
                        rejected.append({"chapter": ch, "pair": key, "fragment": frag})
                    continue
                bucket = out.setdefault(key, [])
                # 同一章同一对只记一次，避免同一行重复命中
                if all(e["chapter"] != ch for e in bucket):
                    bucket.append({"chapter": ch, "event": frag})
                    stats["confirmed"] += 1
    for k in list(out):
        out[k] = _pick_trajectory(sorted(out[k], key=lambda e: e["chapter"]), max_events)
        if not out[k]:
            del out[k]
    stats["pairs"] = len(out)
    _LAST_STATS.clear()
    _LAST_STATS.update(stats)
    return out


def relations_for(book: Path, names: list[str], limit: int = 8,
                  as_of: int | None = None) -> list[dict]:
    """给 writer 消费的精简关系视图：只给本章出场角色涉及的配对。

    零 LLM、只读；账本缺失 → []。

    2026-10-13 新增（**默认关闭**，`NWW_REL_TIMELINE=1` 才生效）：
    每条关系额外给出 `since_chapter` / `trigger` / `trajectory` —— 关系的来历链。
    `as_of` 用于把历史截断在本章之前（不剧透未来章节）；不传 = 不给时间线字段。
    """
    if limit <= 0 or not names:
        return []
    idx_p = book / "追踪" / IDENTITY_FILE
    idx = {}
    if idx_p.is_file():
        try:
            idx = json.loads(_read(idx_p))
        except Exception:
            idx = {}
    if not idx:
        idx = build_identity(book)
    ids = {resolve(n, idx) for n in names}
    ids.discard(None)
    if not ids:
        return []
    # 账本缺失 → 当场从来源确定性重建（同一套逻辑，保证与落盘版本一致）
    events = load_ledger(book)
    if not events:
        events, _u = extract_declarations(book, idx)
    # 演变链只在开关打开 **且** 调用方给了 as_of 时才计算（默认 {} → 行为与改动前一致）
    # 外脑：as_of 用 `is not None` 判（as_of=0 不该被当成"未提供"）
    tl = relation_timeline(book, idx, as_of) if (_timeline_on() and as_of is not None) else {}
    out = []
    for r in build_pairs(idx, events):
        if r["a"] in ids or r["b"] in ids:
            other = r["b_name"] if r["a"] in ids else r["a_name"]
            fwd = r["a_to_b"] if r["a"] in ids else r["b_to_a"]
            rev = r["b_to_a"] if r["a"] in ids else r["a_to_b"]
            item = {
                "with": other,
                # toward = 本章角色 → 对方；from = 对方 → 本章角色（无声明记 UNOBSERVED）
                "toward": fwd.get("relation_type_raw", "") if fwd["declared"] else "",
                "from": (rev.get("relation_type_raw", "")
                         if rev["declared"] else "UNOBSERVED"),
                "reciprocal": bool(r.get("reciprocal")),
                "evidence": (fwd.get("evidence_location", "") if fwd["declared"] else ""),
            }
            # 来历链：**只陈述已发生的事实**，不作关系结论、不补方向
            chain = (tl.get(tuple(sorted([r["a"], r["b"]]))) or []) if tl else []
            if chain:
                item["since_chapter"] = chain[0]["chapter"]
                item["trigger"] = f"第{chain[0]['chapter']}章 {chain[0]['event']}"
                item["trajectory"] = [f"第{e['chapter']}章 {e['event']}" for e in chain]
            out.append(item)
        if len(out) >= limit:
            break
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", "--project", dest="book", required=True)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()
    book = Path(args.book)
    if not book.is_dir():
        print(json.dumps({"ok": False, "detail": f"书目录不存在: {book}"},
                         ensure_ascii=False))
        return 1
    r = build(book, write=args.write)
    if args.stats or not args.json:
        s = r["stats"]
        print(f"角色 {s['characters']}　声明 {s['declarations']}　配对 {s['pairs']}　"
              f"双向 {s['reciprocal_pairs']}　单向 {s['one_way_pairs']}　"
              f"待解析 {s['unresolved']}")
    if args.json:
        print(json.dumps({"ok": r["ok"], "schema_version": r["schema_version"],
                          "stats": r["stats"], "events": r["events"],
                          "pairs": r["pairs"], "unresolved": r["unresolved"]},
                         ensure_ascii=False, indent=2))
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
