#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nww_quality_loop.py — 去 AI 味 / 退化检测编排闭环（Task 5 / A-2）。

检测脚本（check-ai-patterns.js / check-degeneration.js）保持 report-only；
本模块在编排层做 blocking detect → rewriter → re-scan，轮次用尽仍失败则
passed=False。rewriter=None 时不改写，直接按 --fail-on=blocking 判定（CI）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

SCRIPT_DIR = Path(__file__).resolve().parent
_AI_PATTERNS = SCRIPT_DIR / "check-ai-patterns.js"
_DEGENERATION = SCRIPT_DIR / "check-degeneration.js"
_DETECT_TIMEOUT = 180


@dataclass
class QualityLoopResult:
    passed: bool
    rounds: int
    findings: list[dict] = field(default_factory=list)
    blocked_reason: str | None = None


def _node_bin() -> str | None:
    return shutil.which("node")


def _parse_findings(stdout: str) -> list[dict]:
    text = (stdout or "").strip()
    if not text:
        return []
    # Prefer trailing JSON object (scripts may print prose then JSON).
    try:
        data = json.loads(text)
        if isinstance(data, dict) and isinstance(data.get("findings"), list):
            return list(data["findings"])
        if isinstance(data, list):
            return list(data)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            if isinstance(data, dict) and isinstance(data.get("findings"), list):
                return list(data["findings"])
        except json.JSONDecodeError:
            pass
    return []


def _run_detector(node: str, script: Path, prose: Path, *, fail_on_blocking: bool) -> tuple[int, list[dict], str]:
    args = [node, str(script), "--check", "--json"]
    if fail_on_blocking:
        args.append("--fail-on=blocking")
    args.append(str(prose))
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=_DETECT_TIMEOUT,
            cwd=str(SCRIPT_DIR),
        )
    except FileNotFoundError:
        return -1, [], "node not found"
    except subprocess.TimeoutExpired:
        return -1, [], "detector timeout"
    except OSError as exc:
        return -1, [], str(exc)
    findings = _parse_findings(proc.stdout or "")
    if not findings and (proc.stderr or "").strip():
        # Non-JSON failure path — keep stderr as synthetic finding for callers.
        findings = [{"type": "detector_stderr", "severity": "blocking", "message": (proc.stderr or "")[:500]}]
    return int(proc.returncode), findings, (proc.stderr or "")[:500]


def _scan(prose: Path) -> tuple[bool, list[dict], str | None]:
    """Run both detectors. Returns (ok, findings, unavailable_reason).

    ok=True only when both exit 0 under blocking policy.
    unavailable_reason set when node/scripts missing or detector crashed (-1).
    """
    node = _node_bin()
    if node is None:
        return False, [], "detector_unavailable"
    if not _AI_PATTERNS.is_file() or not _DEGENERATION.is_file():
        return False, [], "detector_unavailable"

    all_findings: list[dict] = []
    # Brief: check-ai-patterns.js --check --fail-on=blocking
    rc_ai, findings_ai, err_ai = _run_detector(node, _AI_PATTERNS, prose, fail_on_blocking=True)
    if rc_ai < 0:
        return False, findings_ai, "detector_unavailable"
    for f in findings_ai:
        f = dict(f)
        f.setdefault("detector", "check-ai-patterns")
        all_findings.append(f)

    # Brief: check-degeneration.js --check（默认 fail-on=all；显式 blocking 更稳）
    rc_deg, findings_deg, err_deg = _run_detector(
        node, _DEGENERATION, prose, fail_on_blocking=True
    )
    if rc_deg < 0:
        return False, all_findings + findings_deg, "detector_unavailable"
    for f in findings_deg:
        f = dict(f)
        f.setdefault("detector", "check-degeneration")
        all_findings.append(f)

    blocking = [f for f in all_findings if f.get("severity") == "blocking"]
    ok = rc_ai == 0 and rc_deg == 0
    if not ok and not blocking:
        # Exit non-zero without tagged blocking (e.g. fail-on=all advisory-only) —
        # still treat as not passed for closed-loop safety.
        if err_ai or err_deg:
            all_findings.append(
                {
                    "type": "detector_nonzero",
                    "severity": "blocking",
                    "message": (err_ai or err_deg)[:300],
                }
            )
    return ok, all_findings, None


def run_quality_loop(
    prose: Path,
    *,
    max_rounds: int = 3,
    rewriter: Callable[[Path, list[dict]], None] | None = None,
) -> QualityLoopResult:
    """blocking detect → rewriter → re-scan；仍失败 → passed=False。

    rewriter=None 时不改写，直接根据 --fail-on=blocking 判定（供 CI）。
    """
    prose = Path(prose)
    if max_rounds < 1:
        max_rounds = 1

    last_findings: list[dict] = []
    rounds_done = 0

    for round_i in range(1, max_rounds + 1):
        rounds_done = round_i
        ok, findings, unavailable = _scan(prose)
        last_findings = findings
        if unavailable:
            return QualityLoopResult(
                passed=False,
                rounds=rounds_done,
                findings=last_findings,
                blocked_reason="detector_unavailable",
            )
        if ok:
            return QualityLoopResult(
                passed=True,
                rounds=rounds_done,
                findings=last_findings,
                blocked_reason=None,
            )
        # Failed scan: rewriter path may retry; None → judge immediately (CI).
        if rewriter is None:
            return QualityLoopResult(
                passed=False,
                rounds=rounds_done,
                findings=last_findings,
                blocked_reason="blocking_findings",
            )
        if round_i >= max_rounds:
            break
        rewriter(prose, last_findings)

    return QualityLoopResult(
        passed=False,
        rounds=rounds_done,
        findings=last_findings,
        blocked_reason="blocking_findings",
    )


__all__ = ["QualityLoopResult", "run_quality_loop"]
