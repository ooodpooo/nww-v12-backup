#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nww_post_write.py — 写章后单入口编排脚本（V12.5，2026-09-06）。

把宿主 LLM 生成正文后的所有确定性步骤封装为一个入口，减少 LLM 决策点，
规避"选择性执行"风险。宿主 LLM 只需要做两件事：①生成正文 ②调用本脚本。

流程（确定性，不可跳过）：
  Step 1  nww_enforce.py       机械层修复（负向词/代价/crack/必种件/境界守卫）
  Step 1.5 nww_paragraph_gate.py  段落节奏 BLOCK（移植 detect_internal I-J 并校准）
  Step 2  nww_track_commit.py  追踪提交 + A-1/A-2/A-3/A-4 闸门（不通过则 BLOCK）
  Step 3  审稿闭环指令输出       由宿主 LLM 执行（nww_review_cycle.py 指导，含 I-I 细纲承诺兑现度强制对照）
  Step 4  nww_cross_chapter_check.py  跨章重复/段落漂移/标题疲劳（advisory）

设计原则：
- 纯编排，不重复实现任何子步骤逻辑
- 每一步通过 subprocess 调用，加载最新代码（无需重启宿主）
- commit 失败立即 BLOCK，不继续后续步骤
- enforce / cross_chapter 失败仅 WARNING，不阻断
- 写入 meta.enforced=true 到事务，供 commit 时校验流程完整性
- 向后兼容：不调用本脚本也能走原来的分步流程（meta 校验为兼容模式）
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import nww_gate_policy as _gate  # V13 道闸策略层（2026-11-13）；env NWW_GATE_MODE=strict 回滚

# 子脚本路径
ENFORCE_SCRIPT = SCRIPT_DIR / "nww_enforce.py"
PARAGRAPH_GATE_SCRIPT = SCRIPT_DIR / "nww_paragraph_gate.py"
TRACK_COMMIT_SCRIPT = SCRIPT_DIR / "nww_track_commit.py"
CROSS_CHAPTER_SCRIPT = SCRIPT_DIR / "nww_cross_chapter_check.py"
REVIEW_CYCLE_SCRIPT = SCRIPT_DIR / "nww_review_cycle.py"
FORESHADOW_POLICY_SCRIPT = SCRIPT_DIR / "nww_foreshadow_policy.py"
VOLUME_REVIEW_SCRIPT = SCRIPT_DIR / "nww_volume_review.py"
LOGIC_ANTI_NPC_SCRIPT = SCRIPT_DIR / "nww_logic_and_anti_npc_audit.py"

# 超时（秒）
ENFORCE_TIMEOUT = 60
PARAGRAPH_GATE_TIMEOUT = 15
COMMIT_TIMEOUT = 120
CROSS_CHAPTER_TIMEOUT = 30


def _run(
    script: Path,
    args: list[str],
    timeout: int,
    *,
    label: str,
) -> dict[str, Any]:
    """运行子脚本，返回结构化结果。"""
    cmd = [sys.executable, str(script), *args]
    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(SCRIPT_DIR),
        )
        elapsed = round(time.monotonic() - start, 2)
        return {
            "label": label,
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "elapsed_seconds": elapsed,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
        }
    except subprocess.TimeoutExpired:
        return {
            "label": label,
            "ok": False,
            "returncode": -1,
            "elapsed_seconds": timeout,
            "stdout": "",
            "stderr": f"TIMEOUT after {timeout}s",
        }
    except Exception as exc:
        return {
            "label": label,
            "ok": False,
            "returncode": -2,
            "elapsed_seconds": round(time.monotonic() - start, 2),
            "stdout": "",
            "stderr": f"EXEC_ERROR: {exc}",
        }


def resolve_prose_file(project: Path, chapter: int) -> Path | None:
    """定位正文文件，复用 track_commit 的契约。"""
    try:
        from nww_track_commit import resolve_chapter_file

        return resolve_chapter_file(project, chapter)
    except Exception:
        pass
    # 回退：手动定位
    # ★ 2026-10-12 修复：原回退路径只排除 `_原稿_`、**未排除 _review_snapshots**，
    #   一旦 `nww_track_commit` 导入失败（本函数的 try 分支），写后链路就会定位到
    #   被退回的 round1 快照。此处补齐与 nww_track_commit._is_non_prose 同口径的过滤，
    #   并改为锚定解析章号（不再靠「三位 pattern 优先」的顺序巧合）。
    body = project / "正文"
    if not body.is_dir():
        return None

    def _non_prose(p) -> bool:
        if p.name.startswith("."):
            return True
        if any(d in p.parts for d in ("_review_snapshots", "_archive")):
            return True
        return any(m in p.name for m in ("_原稿_", ".bak", "_损坏_", "_复读_"))

    cands = []
    for p in body.rglob("第*章*.md"):
        if _non_prose(p):
            continue
        m = re.match(r"第0*(\d+)章", p.name)
        if m and int(m.group(1)) == chapter:
            cands.append(p)
    if not cands:
        return None
    try:
        cands.sort(key=lambda q: q.stat().st_mtime_ns)
    except OSError:
        cands.sort(key=lambda q: q.as_posix())
    return cands[-1]


def resolve_transaction_file(project: Path, chapter: int) -> Path | None:
    """定位事务 JSON 文件（delta 草稿）。"""
    tracking = project / "追踪"
    if not tracking.is_dir():
        return None
    # 优先：delta_第N章.json
    candidate = tracking / f"delta_第{chapter:03d}章.json"
    if candidate.is_file():
        return candidate
    # 回退：.tmp_事务N.json
    for p in tracking.glob(f".tmp_事务{chapter}*.json"):
        return p
    for p in tracking.glob(f"*事务*{chapter}*.json"):
        return p
    return None


def load_transaction(path: Path) -> dict[str, Any] | None:
    """读取事务 JSON，兼容外层包装 {"chapter": N, "transaction": {...}}。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(data, dict) and "transaction" in data:
        return data["transaction"]
    return data


def write_meta_enforced(transaction_path: Path) -> bool:
    """在事务 JSON 中写入 meta.enforced=true，供 commit 校验流程完整性。"""
    try:
        data = json.loads(transaction_path.read_text(encoding="utf-8"))
        # 定位 transaction 对象
        tx = data.get("transaction", data) if isinstance(data, dict) else data
        if not isinstance(tx, dict):
            return False
        delta = tx.setdefault("delta", {})
        if not isinstance(delta, dict):
            delta = {}
            tx["delta"] = delta
        quality_meta = delta.setdefault("quality_meta", {})
        if not isinstance(quality_meta, dict):
            quality_meta = {}
            delta["quality_meta"] = quality_meta
        quality_meta["enforced"] = True
        quality_meta["enforced_at"] = int(time.time())
        # 写回
        transaction_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return True
    except Exception:
        return False


def step_enforce(project: Path, prose: Path) -> dict[str, Any]:
    """Step 1: 机械层修复。"""
    args = [
        "--book", str(project),
        "--file", str(prose),
    ]
    return _run(ENFORCE_SCRIPT, args, ENFORCE_TIMEOUT, label="enforce")


def step_paragraph_gate(prose: Path) -> dict[str, Any]:
    """Step 1.5: 段落节奏 BLOCK（精确移植 detect_internal I-J）。"""
    args = ["--prose", str(prose)]
    return _run(PARAGRAPH_GATE_SCRIPT, args, PARAGRAPH_GATE_TIMEOUT, label="paragraph_gate")


def step_commit(project: Path, transaction: Path, quality_dir: Path | None = None) -> dict[str, Any]:
    """Step 2: 追踪提交 + A 闸。

    2026-10-12 新增 quality_dir（可选）：给出时透传 --quality-dir 给 nww_track_commit.py，
    供其 A-2 质量门定位 detect_internal.py。**注意：是否真的跑 A-2 由 NWW_A2_MODE 决定
    （默认 off），所以仅仅“接线”不改变任何生产行为。** 缺省 None → 命令行与改动前完全一致。
    """
    args = [
        "commit",
        "--project", str(project),
        "--input", str(transaction),
    ]
    if quality_dir is not None:
        args += ["--quality-dir", str(quality_dir)]
    return _run(TRACK_COMMIT_SCRIPT, args, COMMIT_TIMEOUT, label="commit")


def step_cross_chapter(project: Path, chapter: int) -> dict[str, Any]:
    """Step 4: 跨章检测。"""
    args = [
        "--book", str(project),
        "--chapter", str(chapter),
        "--json",
    ]
    return _run(CROSS_CHAPTER_SCRIPT, args, CROSS_CHAPTER_TIMEOUT, label="cross_chapter")


def print_review_instruction(project: Path, chapter: int, prose: Path) -> None:
    """Step 3: 输出审稿闭环指令，由宿主 LLM 执行。"""
    print("\n" + "=" * 60)
    print("【Step 3】审稿闭环（宿主 LLM 执行，nww_review_cycle.py 指导）")
    print("=" * 60)
    print(f"  项目: {project}")
    print(f"  章号: 第{chapter}章")
    print(f"  正文: {prose}")
    print()
    print("  执行步骤:")
    print("  1. 调用 nww_review_cycle.py --project <项目> --chapter <章号> --phase score")
    print("     → 输出审稿 prompt，宿主 LLM 按 prompt 打分（I-A~I-I + 综合分）")
    print("     【I-I 强制】必须先读本章细纲，逐条对照节拍/收益/钩子/纪律的兑现情况，")
    print("              未兑现项挂正文证据，禁止只给总分不挂证据。I-I<70 分回细纲补结构。")
    print("  2. 综合分 >= 85 → 审稿通过，写入 meta.reviewed=true + score")
    print("  3. 综合分 < 85 → 调用 nww_review_cycle.py --phase revise")
    print("     → 输出修订指令，宿主 LLM 按指令修订正文")
    print("  4. 修订后重新打分（第2轮），净提升 < 3 分即停")
    print("  5. 保留最高分版本（best-snapshot 原则，修砸了回退）")
    print("  6. 最多 2 轮，完成后调用 nww_review_cycle.py --phase finalize")
    print()
    print("  注意: 审稿闭环是质量底线，不可跳过。如宿主 LLM 跳过，")
    print("        commit 时 meta.reviewed 缺失会触发 WARNING（兼容模式）。")
    print("=" * 60)


def print_report(results: list[dict[str, Any]], blocked: bool) -> None:
    """输出汇总报告。"""
    print("\n" + "=" * 60)
    print("【post_write 汇总报告】")
    print("=" * 60)
    for r in results:
        status = "PASS" if r["ok"] else ("BLOCK" if r["label"] in ("commit", "paragraph_gate") else "WARN")
        print(f"  [{status}] {r['label']:20s} ({r['elapsed_seconds']}s)")
        if not r["ok"]:
            stderr = r["stderr"][:200] if r["stderr"] else "(no stderr)"
            stdout = r["stdout"][:200] if r["stdout"] else ""
            print(f"         {stderr}")
            if stdout:
                print(f"         {stdout}")
    print()
    if blocked:
        print("  结果: BLOCK — commit 闸门未通过，后续步骤已停止")
        print("  行动: 修复 BLOCK 原因后重新调用 nww_post_write.py")
    else:
        print("  结果: 完成 — 确定性步骤全部通过，审稿闭环由宿主 LLM 接续执行")
    print("=" * 60)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="写章后单入口编排：enforce → commit → 审稿闭环 → 跨章检测"
    )
    ap.add_argument("--project", type=Path, required=True, help="书项目根（含 追踪/ 正文/）")
    ap.add_argument("--chapter", type=int, required=True, help="当前章号")
    ap.add_argument("--prose", type=Path, default=None, help="正文文件路径（默认自动定位）")
    ap.add_argument("--transaction", type=Path, default=None, help="事务 JSON 路径（默认自动定位）")
    ap.add_argument("--skip-enforce", action="store_true", help="跳过机械层（调试用）")
    ap.add_argument("--skip-paragraph-gate", action="store_true", help="跳段落节奏门（调试用）")
    ap.add_argument("--skip-cross-chapter", action="store_true", help="跳过跨章检测（调试用）")
    ap.add_argument("--json", action="store_true", help="输出 JSON 格式报告")
    ap.add_argument(
        "--quality-dir",
        type=Path,
        default=None,
        help="REPO 根的 quality/ 目录（含 detect_internal.py）。给出时透传给 commit 子命令；"
             "2026-11-05 起未给出时由 nww_track_commit 从仓库根自动定位（固定资产），"
             "a2_mode=on 且质量门缺失会 fail-closed，不再把「未执行」当通过。"
             "是否真正执行由 NWW_A2_MODE / bible.policy.a2_mode 决定。",
    )
    args = ap.parse_args()

    project = args.project.resolve()
    if not project.is_dir():
        print(f"ERROR: project not found: {project}", file=sys.stderr)
        return 2

    # 定位正文
    prose = args.prose.resolve() if args.prose else resolve_prose_file(project, args.chapter)
    if prose is None or not prose.is_file():
        print(f"ERROR: prose file not found for chapter {args.chapter} in {project}", file=sys.stderr)
        return 2

    # 定位事务
    transaction = args.transaction.resolve() if args.transaction else resolve_transaction_file(project, args.chapter)
    if transaction is None or not transaction.is_file():
        print(f"ERROR: transaction JSON not found for chapter {args.chapter} in {project}/追踪/", file=sys.stderr)
        print("       请先生成 delta 事务草稿（宿主 LLM 按 long-write Phase 2 生成）", file=sys.stderr)
        return 2

    results: list[dict[str, Any]] = []
    blocked = False

    # Step 1: enforce
    if not args.skip_enforce:
        r = step_enforce(project, prose)
        results.append(r)
        if not r["ok"]:
            print(f"[WARN] enforce 失败（继续）: {r['stderr'][:150]}", file=sys.stderr)
        # 写入 meta.enforced
        write_meta_enforced(transaction)
    else:
        # 2026-10-26 R2-8：当调用方已前置跑过 enforce（传 --skip-enforce 防二次改写破坏 final_hash）时，
        # 确保 transaction 内的 quality_meta.enforced 标记同步到位，避免 A-5 误判未经过 enforce。
        write_meta_enforced(transaction)

    # Step 1.5: paragraph_gate（V13 道闸：质量闸只报告；strict 保持 BLOCK）
    if not getattr(args, "skip_paragraph_gate", False):
        r = step_paragraph_gate(prose)
        results.append(r)
        if not r["ok"]:
            if _gate.is_v13():
                # V13 事实卡：判词原样留痕，不阻断后续 commit（裁决权交作者）。
                _detail = r["stdout"] or r["stderr"] or "paragraph_gate 未通过"
                _gate.record(project, args.chapter, "paragraph_gate", _detail,
                             source="post_write")
                _gate.note("paragraph_gate", _detail)
                print("[GATE][V13] paragraph_gate 未通过：已记录事实卡，不阻断后续步骤",
                      file=sys.stderr)
                if r["stdout"]:
                    print(r["stdout"])  # 完整 violation 详情仍透传给 pipeline / 作者
                # 不设 blocked、不 return 2 —— 继续 Step 2 commit
            else:
                blocked = True
                # 完整 violation 详情 → stdout（pipeline 消费 stdout 拿报告）；summary → stderr（shell 用户可见）
                print("[BLOCK] paragraph_gate 未通过，停止后续步骤", file=sys.stderr)
                if r["stdout"]:
                    print(r["stdout"])  # 完整，无截断；pipeline 会读到 violation 详情
                print_report(results, blocked)  # 汇总报告也写 stdout（被 pipeline 同时看到 summary + 详情）
                return 2  # 硬信号契约：BLOCK → exit 2

    # Step 2: commit（BLOCK 点）
    r = step_commit(project, transaction, getattr(args, "quality_dir", None))
    results.append(r)
    if not r["ok"]:
        blocked = True
        print(f"[BLOCK] commit 失败，停止后续步骤: {r['stderr'][:300]}", file=sys.stderr)
        if r["stdout"]:
            print(r["stdout"][:500], file=sys.stderr)
        print_report(results, blocked)
        return 2  # 硬信号契约：BLOCK → exit 2

    # Step 2.5: voice_print 后置挂载（2026-11-13）——事实卡生成阶段只读统计。
    # 统计引号内对话的低频锚点次数与段落/行位置；不判决、不拦截、不改正文。
    try:
        import nww_voice_print as _voice_print
        _vp = _voice_print.record_dialogue_marker_facts(project, args.chapter, prose)
        if _vp.get("status") == "recorded":
            _gate.note("voice_print/markers", _vp.get("detail", ""))
    except Exception as _vp_exc:  # noqa: BLE001 — 零阻断：统计失败不影响提交结果
        print(f"[VOICE_PRINT] facts skipped: {_vp_exc}", file=sys.stderr)

    # Step 3: 审稿闭环指令（宿主 LLM 执行）
    print_review_instruction(project, args.chapter, prose)

    # Step 4: cross_chapter
    if not args.skip_cross_chapter:
        r = step_cross_chapter(project, args.chapter)
        results.append(r)
        if not r["ok"]:
            print(f"[WARN] cross_chapter 失败（不阻断）: {r['stderr'][:150]}", file=sys.stderr)
        elif r["stdout"]:
            try:
                data = json.loads(r["stdout"])
                findings = data.get("findings", [])
                if findings:
                    print(f"\n[跨章检测] 发现 {len(findings)} 项 advisory:")
                    for f in findings:
                        print(f"  - [{f.get('severity', '?')}] {f.get('rule', '?')}: {f.get('description', '')[:120]}")
            except json.JSONDecodeError:
                pass

    # Step 4.4 (2026-10-26 R2-8): 反NPC化与逻辑严谨性审计门禁
    if LOGIC_ANTI_NPC_SCRIPT.is_file():
        r_npc = _run(
            LOGIC_ANTI_NPC_SCRIPT,
            ["--book", str(project), "--chapter", str(args.chapter), "--prose", str(prose), "--json"],
            20,
            label="logic_anti_npc_audit",
        )
        results.append(r_npc)
        if not r_npc["ok"] and r_npc["stdout"]:
            print(f"\n[WARN] 反NPC与逻辑严谨度发现违规: {r_npc['stdout'][:300]}", file=sys.stderr)

    # Step 4.5 (2026-10-26 接线 D2/D4): 伏笔治理策略检查 + 卷末章自动触发卷末复盘门禁
    if FORESHADOW_POLICY_SCRIPT.is_file():
        r_fp = _run(FORESHADOW_POLICY_SCRIPT, ["--project", str(project)], 15, label="foreshadow_policy")
        results.append(r_fp)
        if r_fp["stdout"]:
            print("\n" + r_fp["stdout"])
    if VOLUME_REVIEW_SCRIPT.is_file():
        outline_dir = project / "大纲"
        if outline_dir.is_dir():
            import re as _re
            for vf in sorted(outline_dir.glob("卷纲_第*卷.md")):
                m_v = _re.search(r"卷纲_第(\d+)卷", vf.name)
                if not m_v:
                    continue
                try:
                    vtxt = vf.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                m_rng = _re.search(r"章节范围.*?第\s*(\d+)\s*[–—\-~～至]\s*(\d+)\s*章", vtxt)
                if m_rng and int(m_rng.group(2)) == args.chapter:
                    vol_no = int(m_v.group(1))
                    r_vr = _run(
                        VOLUME_REVIEW_SCRIPT,
                        ["--project", str(project), "--volume", str(vol_no)],
                        30,
                        label=f"volume_review_v{vol_no}",
                    )
                    results.append(r_vr)
                    if r_vr["stdout"]:
                        print(f"\n[卷末复盘门禁 · 第{vol_no}卷] {r_vr['stdout']}")
                    break

    print_report(results, blocked)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
