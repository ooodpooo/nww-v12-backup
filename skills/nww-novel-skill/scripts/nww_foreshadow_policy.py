#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nww_foreshadow_policy.py — 伏笔治理策略检查（P0-2，独立模块，零耦合接入）。

职责（对 _tracking-state.json 只读，绝不写盘）：
  1. 缺字段清单：已埋伏笔缺 debt 元数据
     - missing_planned：任何「已埋」缺 planned_resolution_chapter（回收无期 = 悬账）
     - missing_half_life：core_hook=true 的「已埋」缺 half_life_chapters
       （新事务已被 normalize 硬校验拦截，此处只可能命中存量数据）
  2. 逾期清单：复用官方 K5 stale_core_hook_lines（core_hook 超半衰期未推进），
     不重复实现、不双报。
  3. 收一埋一计数：活跃伏笔负载（已埋+需处理 总数 / 核心数 / 需处理数），advisory。
  4. 孤儿引用：复用官方 orphan_hook_reference_lines（depends_on/parent_id 悬空）。

门禁语义：
  - 默认 advisory：恒 exit 0，报告输出到 stdout（不挡写作）。
  - 严格模式：{BOOK}/.foreshadow-strict 标记文件存在，且存在缺字段项
    「持续 ≥3 章」（以 planted_chapter 为锚：last_committed_chapter - planted_chapter >= 3，
    给新埋伏笔 3 章补齐宽限）→ exit 2（BLOCKING）。
  - 环境错误（state 缺失/损坏）→ exit 1。

与现有链路关系：纯新增文件，不改任何既有 .py / hook；由 long-write SKILL.md 的
audit 收尾步骤文本级调用（阶段4接线）。K5 判定逻辑全部复用 nww_track_commit，
本脚本零复制。

用法：
  python3 nww_foreshadow_policy.py --project <book_root> [--json]
  python3 nww_foreshadow_policy.py --selftest
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nww_track_commit as tc

STRICT_MARKER = ".foreshadow-strict"
GRACE_CHAPTERS = 3  # 新埋伏笔补齐 debt 元数据的宽限章数


def collect_findings(state: dict) -> dict:
    """从 state 提取全部策略发现（纯函数，只读）。"""
    foreshadow = state.get("foreshadow") or {}
    last = int(state.get("last_committed_chapter") or 0)

    missing: list[dict] = []
    for fid in sorted(foreshadow):
        row = foreshadow[fid]
        if row.get("status") != "已埋":
            continue
        gaps = []
        if row.get("planned_resolution_chapter") is None:
            gaps.append("planned_resolution_chapter")
        if row.get("core_hook") and row.get("half_life_chapters") is None:
            gaps.append("half_life_chapters")
        if gaps:
            age = last - int(row.get("planted_chapter") or last)
            missing.append({
                "id": fid,
                "gaps": gaps,
                "planted_chapter": row.get("planted_chapter"),
                "age_chapters": age,
                "persisted": age >= GRACE_CHAPTERS,
                "summary": row.get("summary", ""),
            })

    stale = tc.stale_core_hook_lines(foreshadow, last)
    orphans = tc.orphan_hook_reference_lines(foreshadow)
    active = [r for r in foreshadow.values() if r.get("status") in ("已埋", "需处理")]
    resolved_all = [r for r in foreshadow.values() if r.get("status") == "已回收"]

    # 2026-10-26 新增：① 计划回收章逾期检测（overdue_lines）② 短/中/长三级分级统计 ③ 收一埋一动态平衡检查
    overdue_lines: list[str] = []
    tier_counts = {"short": 0, "mid": 0, "long": 0}
    for fid in sorted(foreshadow):
        row = foreshadow[fid]
        if row.get("status") not in ("已埋", "需处理"):
            continue
        prc = row.get("planned_resolution_chapter")
        plc = int(row.get("planted_chapter") or last)
        if prc is not None and int(prc) < last:
            overdue_lines.append(
                f"伏笔 {fid} 已超过计划回收章（计划第{prc}章，当前第{last}章，逾期 {last - int(prc)} 章）：{row.get('summary', '')}"
            )
        explicit_tier = str(row.get("tier") or "").lower()
        if explicit_tier in ("short", "短线"):
            tier_counts["short"] += 1
        elif explicit_tier in ("long", "长线"):
            tier_counts["long"] += 1
        elif explicit_tier in ("mid", "中线"):
            tier_counts["mid"] += 1
        elif prc is not None:
            span = int(prc) - plc
            if span <= 10:
                tier_counts["short"] += 1
            elif span <= 35:
                tier_counts["mid"] += 1
            else:
                tier_counts["long"] += 1
        else:
            tier_counts["long" if row.get("core_hook") else "mid"] += 1

    net_active = len(active)
    balance_warning = None
    if net_active > 12 and len(resolved_all) == 0:
        balance_warning = f"收一埋一失衡预警：当前活跃未收伏笔 {net_active} 条，累计已回收 0 条，请优先安排阶段性回收后再埋新线"
    elif net_active > 18:
        balance_warning = f"伏笔池过载预警：当前活跃未收伏笔达 {net_active} 条（建议上限 15 条），存在后期烂尾风险"

    return {
        "last_committed_chapter": last,
        "missing": missing,
        "stale_lines": stale,
        "overdue_lines": overdue_lines,
        "orphan_lines": orphans,
        "active_total": len(active),
        "resolved_total": len(resolved_all),
        "tier_counts": tier_counts,
        "balance_warning": balance_warning,
        "active_core": sum(1 for r in active if r.get("core_hook")),
        "needs_handling": sum(1 for r in active if r.get("status") == "需处理"),
    }


def evaluate(project: Path, state: dict) -> tuple[dict, bool, bool]:
    """返回 (findings, strict_enabled, blocking)。"""
    findings = collect_findings(state)
    strict = (project / STRICT_MARKER).is_file()
    blocking = strict and any(m["persisted"] for m in findings["missing"])
    return findings, strict, blocking


def render_report(findings: dict, strict: bool, blocking: bool) -> str:
    lines = ["=== 伏笔治理策略检查（advisory 层）==="]
    lines.append(
        f"活跃伏笔负载：{findings['active_total']}（核心 {findings['active_core']}，"
        f"需处理 {findings['needs_handling']}）｜严格模式：{'开' if strict else '关（.foreshadow-strict 不存在）'}"
    )
    if findings["missing"]:
        lines.append(f"【缺字段】{len(findings['missing'])} 条 debt 元数据不全：")
        for m in findings["missing"]:
            flag = "（已持续≥%d章，严格模式下阻断）" % GRACE_CHAPTERS if m["persisted"] else "（宽限期内）"
            lines.append(
                f"- {m['id']} 缺 {', '.join(m['gaps'])}｜埋第{m['planted_chapter']}章"
                f"｜已 {m['age_chapters']} 章{flag}：{m['summary']}"
            )
    else:
        lines.append("【缺字段】无——全部已埋伏笔 debt 元数据完整。")
    tc_dist = findings.get("tier_counts") or {}
    lines.append(
        f"三级伏笔分布：短线(≤10章)={tc_dist.get('short', 0)}｜中线(≤35章)={tc_dist.get('mid', 0)}｜长线(跨卷)={tc_dist.get('long', 0)}｜已回收={findings.get('resolved_total', 0)}"
    )
    if findings["stale_lines"]:
        lines.append(f"【逾期·半衰期】{len(findings['stale_lines'])} 条核心伏笔超半衰期（K5）：")
        lines.extend(f"- {s}" for s in findings["stale_lines"])
    else:
        lines.append("【逾期·半衰期】无核心伏笔超半衰期。")
    if findings.get("overdue_lines"):
        lines.append(f"【逾期·计划回收章】{len(findings['overdue_lines'])} 条伏笔超过计划回收章：")
        lines.extend(f"- {s}" for s in findings["overdue_lines"])
    if findings.get("balance_warning"):
        lines.append(f"【收一埋一平衡】{findings['balance_warning']}")
    if findings["orphan_lines"]:
        lines.append(f"【孤儿引用】{len(findings['orphan_lines'])} 条：")
        lines.extend(f"- {s}" for s in findings["orphan_lines"])
    if blocking:
        lines.append("VERDICT: BLOCK（严格模式 + 缺字段持续≥%d章）" % GRACE_CHAPTERS)
    else:
        lines.append("VERDICT: PASS（advisory）" if not strict else "VERDICT: PASS（严格模式，无持续缺字段）")
    return "\n".join(lines)


def run(project: Path, as_json: bool) -> int:
    state_path = project / "追踪" / "_tracking-state.json"
    if not state_path.is_file():
        print(f"ERROR: 追踪状态不存在：{state_path}", file=sys.stderr)
        return 1
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"ERROR: 追踪状态不可读：{exc}", file=sys.stderr)
        return 1
    findings, strict, blocking = evaluate(project, state)
    if as_json:
        print(json.dumps({
            "findings": findings, "strict_mode": strict, "blocking": blocking,
        }, ensure_ascii=False, indent=2))
    else:
        print(render_report(findings, strict, blocking))
    return 2 if blocking else 0


# ---------------- selftest：临时目录 fixture，不碰任何真实项目 ----------------
def _fixture(tmp: Path, foreshadow: dict, last: int, strict: bool) -> Path:
    book = tmp / "fixture_book"
    (book / "追踪").mkdir(parents=True, exist_ok=True)
    state = {"last_committed_chapter": last, "state_revision": 1, "foreshadow": foreshadow}
    (book / "追踪" / "_tracking-state.json").write_text(
        json.dumps(state, ensure_ascii=False), encoding="utf-8")
    if strict:
        (book / STRICT_MARKER).write_text("", encoding="utf-8")
    return book


def _row(fid, planted, **kw):
    base = {
        "id": fid, "summary": kw.pop("summary", f"{fid} 测试伏笔"), "planted_chapter": planted,
        "planned_resolution_chapter": None, "status": "已埋", "importance": "高",
        "core_hook": False, "half_life_chapters": None, "depends_on": [], "parent_id": None,
        "updated_chapter": planted,
    }
    base.update(kw)
    return base


def selftest() -> int:
    import tempfile
    ok = True

    def case(name, expect_rc, book):
        nonlocal ok
        rc = run(book, as_json=False)
        passed = rc == expect_rc
        ok = ok and passed
        print("  [%s] %s（期望 exit=%d，实际 exit=%d）" % ("PASS" if passed else "FAIL", name, expect_rc, rc))

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # S1 全字段齐备 → PASS
        book = _fixture(tmp / "s1", {"F001": _row("F001", 1, core_hook=True, half_life_chapters=20, planned_resolution_chapter=50)}, 10, strict=False)
        case("S1 元数据完整 → exit 0", 0, book)
        # S2 宽限期内缺字段 + 严格模式 → 仍 PASS
        book = _fixture(tmp / "s2", {"F001": _row("F001", 9)}, 10, strict=True)
        case("S2 宽限期内（age=1<3）严格模式 → exit 0", 0, book)
        # S3 缺字段持续≥3章 + 严格模式 → BLOCK
        book = _fixture(tmp / "s3", {"F001": _row("F001", 7)}, 10, strict=True)
        case("S3 持续缺字段（age=3）严格模式 → exit 2", 2, book)
        # S4 同 S3 但无严格标记 → advisory
        book = _fixture(tmp / "s4", {"F001": _row("F001", 7)}, 10, strict=False)
        case("S4 持续缺字段无标记 → exit 0", 0, book)
        # S5 核心伏笔超半衰期 → 逾期清单非空，仍 advisory（K5 处置在 commit 时）
        book = _fixture(tmp / "s5", {"F001": _row("F001", 1, core_hook=True, half_life_chapters=5, planned_resolution_chapter=50, updated_chapter=1)}, 20, strict=False)
        f, _, _ = evaluate(book, json.loads((book / "追踪" / "_tracking-state.json").read_text(encoding="utf-8")))
        case("S5 超半衰期 → exit 0 且逾期清单=1", 0, book)
        passed = len(f["stale_lines"]) == 1
        ok = ok and passed
        print("  [%s] S5 逾期清单条数=1（实际 %d）" % ("PASS" if passed else "FAIL", len(f["stale_lines"])))
        # S6 孤儿引用 → 报告含孤儿行，exit 0
        book = _fixture(tmp / "s6", {"F001": _row("F001", 1, depends_on=["F999"], planned_resolution_chapter=50)}, 10, strict=False)
        f, _, _ = evaluate(book, json.loads((book / "追踪" / "_tracking-state.json").read_text(encoding="utf-8")))
        case("S6 孤儿引用 → exit 0", 0, book)
        passed = len(f["orphan_lines"]) == 1
        ok = ok and passed
        print("  [%s] S6 孤儿引用条数=1（实际 %d）" % ("PASS" if passed else "FAIL", len(f["orphan_lines"])))

    print("selftest：%s" % ("ALL PASS" if ok else "存在 FAIL"))
    return 0 if ok else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=None, help="book project root（含 追踪/）")
    parser.add_argument("--json", action="store_true", help="JSON 输出（供审计管线消费）")
    parser.add_argument("--selftest", action="store_true", help="运行临时目录 fixture 自测")
    args = parser.parse_args()
    if args.selftest:
        return selftest()
    if args.project is None:
        parser.error("--project 必填（或用 --selftest）")
    return run(args.project, args.json)


if __name__ == "__main__":
    sys.exit(main())
