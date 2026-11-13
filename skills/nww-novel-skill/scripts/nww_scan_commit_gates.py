#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nww_scan_commit_gates.py — 存量 commit_gates / policy 扫描（report-only）。

扫描书根：缺 policy、缺闸、commit_gates null、过期 override → 报告。
另：NWW_HOOK_ADVISORY_ONLY 若在进程环境或书根样本/启动包装脚本中启用 → HIGH。
高危（缺 policy / null gates / 缺 REQUIRED 闸 / advisory-only 逃逸）→ exit 1。
本扫描 report-only：不改 bible、不改 deslop。

Operator note: incomplete bibles (missing policy / missing REQUIRED gates) fail-closed
on hard commits — add policy.overrides (reason+expires_at) or migrate commit_gates
before enabling hard commits on存量 books. Run this scanner first.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from nww_policy import REQUIRED_NEW_BOOK_GATES, _override_expired, load_policy  # noqa: E402

HIGH = "high"
WARN = "warn"

ADVISORY_ONLY_ENV = "NWW_HOOK_ADVISORY_ONLY"
_FALSEY = frozenset({"", "0", "false", "no", "off", "unset"})

# Samples / wrappers only — never read real `.env` (secrets).
_ADVISORY_SAMPLE_NAMES = frozenset({
    ".env.example",
    ".env.sample",
    ".env.template",
    ".env.production.example",
    ".env.prod.example",
})
_ADVISORY_WRAPPER_SUFFIXES = (".sh", ".bash", ".zsh", ".env.example")
_ADVISORY_WRAPPER_PREFIXES = ("start", "run", "deploy", "launch", "wrapper")

# export VAR=1 / VAR=1 / VAR: 1 (yaml-ish); ignore comment-only mentions.
_ADVISORY_ASSIGN_RE = re.compile(
    r"(?m)^[ \t]*(?:export[ \t]+)?"
    + re.escape(ADVISORY_ONLY_ENV)
    + r"[ \t]*[=:][ \t]*(\S+)"
)


def _advisory_value_enabled(raw: str | None) -> bool:
    if raw is None:
        return False
    v = raw.strip().strip("\"'").rstrip(",").lower()
    return v not in _FALSEY


def _iter_advisory_profile_files(book: Path) -> list[Path]:
    """Shallow scan under book root for env samples / shell wrappers."""
    out: list[Path] = []
    if not book.is_dir():
        return out
    try:
        entries = list(book.iterdir())
    except OSError:
        return out
    for p in entries:
        if not p.is_file():
            continue
        name = p.name
        # Explicit skip: real secret env files
        if name == ".env" or name.startswith(".env.") and name.endswith(".local"):
            continue
        if name in {".env", ".env.local", ".env.production", ".env.prod"}:
            continue
        if name in _ADVISORY_SAMPLE_NAMES:
            out.append(p)
            continue
        lower = name.lower()
        if any(lower.endswith(suf) for suf in _ADVISORY_WRAPPER_SUFFIXES):
            if lower.endswith(".env.example") or any(
                lower.startswith(pref) for pref in _ADVISORY_WRAPPER_PREFIXES
            ):
                out.append(p)
    return out


def scan_hook_advisory_only(book: Path) -> list[dict[str, Any]]:
    """Report-only HIGH if test-only hook escape is enabled in process or profiles.

    Does not mutate bibles. Does not read `.env` secret files.
    """
    findings: list[dict[str, Any]] = []
    book_s = str(book)

    if _advisory_value_enabled(os.environ.get(ADVISORY_ONLY_ENV)):
        findings.append({
            "severity": HIGH,
            "code": "hook_advisory_only_enabled",
            "book": book_s,
            "msg": (
                f"{ADVISORY_ONLY_ENV} is set in process env "
                "(test-only escape; forbidden in production)"
            ),
        })

    for path in _iter_advisory_profile_files(book):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            findings.append({
                "severity": WARN,
                "code": "advisory_profile_unreadable",
                "book": book_s,
                "msg": f"could not read {path.name}: {exc}",
            })
            continue
        for m in _ADVISORY_ASSIGN_RE.finditer(text):
            if _advisory_value_enabled(m.group(1)):
                findings.append({
                    "severity": HIGH,
                    "code": "hook_advisory_only_in_profile",
                    "book": book_s,
                    "msg": (
                        f"{ADVISORY_ONLY_ENV} enabled in {path.name} "
                        "(production profiles / wrappers must not export this)"
                    ),
                })
                break
    return findings


def scan_book(book: Path) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    findings.extend(scan_hook_advisory_only(book))
    bible_path = book / "bible.json"
    if not bible_path.is_file():
        findings.append({
            "severity": HIGH,
            "code": "missing_bible",
            "book": str(book),
            "msg": "bible.json missing",
        })
        return findings

    try:
        bible = json.loads(bible_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        findings.append({
            "severity": HIGH,
            "code": "bible_unreadable",
            "book": str(book),
            "msg": f"bible.json unreadable: {exc}",
        })
        return findings

    if not isinstance(bible, dict):
        findings.append({
            "severity": HIGH,
            "code": "bible_not_object",
            "book": str(book),
            "msg": "bible.json root is not an object",
        })
        return findings

    policy = bible.get("policy")
    if not isinstance(policy, dict):
        findings.append({
            "severity": HIGH,
            "code": "missing_policy",
            "book": str(book),
            "msg": "bible.policy missing — migrate to policy.fail_closed + commit_gates",
        })

    top_gates = bible.get("commit_gates")
    pol_gates = policy.get("commit_gates") if isinstance(policy, dict) else None

    if top_gates is None and pol_gates is None:
        findings.append({
            "severity": HIGH,
            "code": "missing_commit_gates",
            "book": str(book),
            "msg": "commit_gates missing at top-level and under policy",
        })
    elif top_gates is None and "commit_gates" in bible:
        findings.append({
            "severity": HIGH,
            "code": "null_commit_gates",
            "book": str(book),
            "msg": "top-level commit_gates is null",
        })

    effective_gates = pol_gates if isinstance(pol_gates, dict) else (
        top_gates if isinstance(top_gates, dict) else {}
    )
    for name in REQUIRED_NEW_BOOK_GATES:
        if name not in effective_gates:
            findings.append({
                "severity": HIGH,
                "code": "missing_gate",
                "book": str(book),
                "gate": name,
                "msg": f"gate {name!r} missing from commit_gates",
            })
        elif name == "fact_provenance":
            fp = effective_gates.get(name)
            if isinstance(fp, dict) and "enabled" not in fp:
                findings.append({
                    "severity": HIGH,
                    "code": "missing_gate",
                    "book": str(book),
                    "gate": name,
                    "msg": "fact_provenance.enabled missing",
                })

    if isinstance(policy, dict):
        for i, ov in enumerate(policy.get("overrides") or []):
            if not isinstance(ov, dict):
                findings.append({
                    "severity": HIGH,
                    "code": "bad_override",
                    "book": str(book),
                    "msg": f"overrides[{i}] not an object",
                })
                continue
            exp = ov.get("expires_at")
            if isinstance(exp, str) and _override_expired(exp):
                findings.append({
                    "severity": WARN,
                    "code": "expired_override",
                    "book": str(book),
                    "gate": ov.get("gate"),
                    "msg": f"override for {ov.get('gate')!r} expired at {exp}",
                })
            if not (isinstance(ov.get("reason"), str) and str(ov.get("reason")).strip()):
                findings.append({
                    "severity": HIGH,
                    "code": "override_missing_reason",
                    "book": str(book),
                    "msg": f"overrides[{i}] missing reason",
                })
            if not (isinstance(ov.get("expires_at"), str) and str(ov.get("expires_at")).strip()):
                findings.append({
                    "severity": HIGH,
                    "code": "override_missing_expires",
                    "book": str(book),
                    "msg": f"overrides[{i}] missing expires_at",
                })

    # Smoke-load policy（覆盖解析错误）
    try:
        load_policy(book)
    except Exception as exc:  # noqa: BLE001
        findings.append({
            "severity": HIGH,
            "code": "policy_load_error",
            "book": str(book),
            "msg": str(exc),
        })

    return findings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--book",
        action="append",
        dest="books",
        required=True,
        help="书根路径（可重复）",
    )
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args(argv)

    all_findings: list[dict[str, Any]] = []
    for raw in args.books:
        book = Path(raw).expanduser().resolve()
        all_findings.extend(scan_book(book))

    high = [f for f in all_findings if f.get("severity") == HIGH]
    if args.json:
        print(json.dumps({"findings": all_findings, "high_count": len(high)}, ensure_ascii=False, indent=2))
    else:
        if not all_findings:
            print("OK: no policy / commit_gates issues")
        for f in all_findings:
            gate = f.get("gate")
            extra = f" gate={gate}" if gate else ""
            print(f"[{f['severity'].upper()}] {f['code']}{extra}: {f['msg']} ({f['book']})")
        print(f"summary: {len(all_findings)} finding(s), {len(high)} high")

    return 1 if high else 0


if __name__ == "__main__":
    raise SystemExit(main())
