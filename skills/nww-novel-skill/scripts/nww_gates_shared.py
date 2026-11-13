#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nww_gates_shared.py — 写章护栏共享库（Task 1 / C-P0-2）。

从 Web pipeline 下沉的确定性纯函数 + 对现有脚本的薄封装，供 CLI
`nww_write_chapter` 与 Web `pipeline.py` 共用，避免三入口护栏分裂。

禁止在此复制巨型 pipeline 业务体；LLM 步骤仍留在 Web。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

_REPO_ROOT = SCRIPT_DIR.parents[2]
_ENGINE_SCRIPTS = _REPO_ROOT / "engine" / "scripts"
_PY = sys.executable

ENFORCE_SCRIPT = SCRIPT_DIR / "nww_enforce.py"
POST_WRITE_SCRIPT = SCRIPT_DIR / "nww_post_write.py"
PARAGRAPH_GATE_SCRIPT = SCRIPT_DIR / "nww_paragraph_gate.py"
PACKET_V2_SKILL = SCRIPT_DIR / "nww_packet_builder_v2.py"
PACKET_V2_ENGINE = _ENGINE_SCRIPTS / "nww_packet_builder_v2.py"

# ---------------------------------------------------------------------------
# C-PRONOUN@1：代词性别闸的唯一实现在 nww_pronoun_gate.py
# ---------------------------------------------------------------------------
# 本模块保留原有符号名与返回结构，改为转发，避免同一算法与阈值维护两份。
from nww_pronoun_gate import (  # noqa: E402 — 脚本目录已在文件头前置进 sys.path
    LOCAL_ACTOR_TERMS,
    LOCAL_ACTOR_SENTINEL as _LOCAL_ACTOR,
    contract_sidecar_path,
    known_names as pronoun_gate_known_names,
    local_actor_terms,
    protagonist_gender,
    protagonist_pronoun_counts,
    resolve_protagonist_anchor,
)
from nww_pronoun_gate import check as _pronoun_check


# ---------------------------------------------------------------------------
# packet contract repair（纯函数，自 pipeline 下沉）
# ---------------------------------------------------------------------------

def character_contract_from_cards(book: Path, packet: dict) -> list[dict]:
    """character_contract 为空时从 设定/角色/*.md 兜底构建最小 voice 合同。"""
    card_dir = book / "设定" / "角色"
    if not card_dir.is_dir():
        return []
    ctx = json.dumps({"scene_chain": packet.get("scene_chain"),
                      "intent": packet.get("intent")}, ensure_ascii=False)
    out: list[dict] = []
    protagonist_name = ""
    try:
        _bible = json.loads((book / "bible.json").read_text(encoding="utf-8"))
        protagonist_name = re.sub(
            r"^主角", "", str((_bible.get("protagonist") or {}).get("name") or "")
        ).strip()
    except Exception:  # noqa: BLE001
        pass
    for p in sorted(card_dir.glob("*.md"))[:8]:
        name = p.stem
        try:
            lines = [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
        except Exception:  # noqa: BLE001
            continue
        if name not in ctx and not (protagonist_name and protagonist_name in name and not out):
            continue
        head = "\n".join(lines[:5])
        voice = [ln[:160] for ln in lines
                 if re.search(r"认知标尺|隐喻|句法节奏|语用|情绪变调|低频锚点|口头禅|口癖|句式|习惯动作|性格", ln)][:3]
        behavior = [ln[:160] for ln in lines if "禁" in ln][:2]
        out.append({
            "name": name,
            "role": "主角" if re.search(r"女主|主角|男主", head) else "",
            "voice_constraints": voice or [
                f"（角色卡兜底，详见 设定/角色/{name}.md 首段）{head[:120]}"
            ],
            "behavior_constraints": behavior,
            "state": "",
        })
    return out


def facts_from_bible(book: Path) -> list[dict]:
    """首章/无前情时从 bible.json 构建初始已认证事实。"""
    try:
        bible = json.loads((book / "bible.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    out: list[dict] = []
    protag = bible.get("protagonist") or {}
    pname = re.sub(r"^主角", "", str(protag.get("name") or "")).strip()
    desc = str(protag.get("description") or "").strip()
    if pname and desc:
        out.append({
            "id": "bible-protagonist", "subject": pname, "predicate": "身份设定",
            "value": desc[:200], "fact": f"{pname}：{desc[:200]}",
            "fact_type": "identity",
            "evidence": {"chapter": 0, "quote": "bible.json protagonist"},
        })
    syn = str(bible.get("synopsis") or bible.get("world_summary") or "").strip()
    if syn:
        out.append({
            "id": "bible-synopsis", "subject": "", "predicate": "世界观基线",
            "value": syn[:300], "fact": syn[:300], "fact_type": "worldview",
            "evidence": {"chapter": 0, "quote": "bible.json synopsis"},
        })
    return out


def repair_packet_contract(book: Path, chapter_id: int, packet: dict) -> dict:
    """packet 合同修复闸（P0-2，写章链 BLOCK 点）。

    ① character_contract 为空 → 设定/角色卡兜底注入；
    ② 兜底后仍为空 → BLOCK（NWW_PACKET_STRICT=0 降级 WARNING）；
    ③ facts.confirmed 为空 → bible 兜底注入。
    """
    meta = packet.get("meta") or {}
    strict = os.environ.get("NWW_PACKET_STRICT", "1") != "0"
    problems: list[str] = []
    if not packet.get("character_contract"):
        injected = character_contract_from_cards(book, packet)
        if injected:
            packet["character_contract"] = injected
            meta["missing"] = [m for m in (meta.get("missing") or []) if m != "出场角色voice锚点"]
            if meta.get("parse_status") not in (None, "FULL"):
                meta["parse_status"] = "REPAIRED"
            print(
                f"[P02-REPAIR] character_contract 为空 → 设定/角色卡兜底注入 {len(injected)} 条"
                f"（{[c['name'] for c in injected]}）",
                flush=True,
            )
        else:
            problems.append("出场角色voice锚点缺失，且 设定/角色/ 无本章角色卡可兜底")
    facts = packet.get("facts") or {}
    if not facts.get("confirmed"):
        bf = facts_from_bible(book)
        if bf:
            facts["confirmed"] = bf
            facts.setdefault("meta", {})["reason"] = "bible_fallback_no_confirmed_facts"
            packet["facts"] = facts
            print(f"[P02-REPAIR] facts.confirmed 为空 → bible 兜底注入 {len(bf)} 条", flush=True)
    packet["meta"] = meta
    if problems:
        msg = "；".join(problems)
        if strict:
            raise RuntimeError(
                f"[PACKET_GATE] BLOCK: {msg} —— 请在细纲补充「出场角色 voice 锚点」小节，"
                f"或在 设定/角色/ 为本章角色创建角色卡后重试（NWW_PACKET_STRICT=0 可临时降级放行）"
            )
        print(f"[PACKET_GATE][WARNING] {msg}（NWW_PACKET_STRICT=0 降级放行）", flush=True)
    return packet


# ---------------------------------------------------------------------------
# pronoun gate 转发（C-PRONOUN@1）
# ---------------------------------------------------------------------------

def pronoun_gate_check(book: Path, chapter_id: int, prose_text: str) -> tuple[bool, str]:
    """代词性别机械闸：转发 nww_pronoun_gate.check（算法与阈值单一实现）。"""
    return _pronoun_check(book, chapter_id, prose_text)


# ---------------------------------------------------------------------------
# 薄封装：调现有脚本（subprocess，加载最新代码）
# ---------------------------------------------------------------------------

# 路径标志（与 toolbox._assert_paths_within 对齐）：Web 多书隔离时传入 path_root
_PATH_ARG_HINTS = ("--project", "--book", "--prose", "--out", "--input", "--file")


def assert_paths_within(args: list[str], path_root: Path) -> None:
    """防御式路径隔离：args 中出现的绝对/相对逃逸路径必须位于 path_root 内。

    从 Web toolbox._assert_paths_within 移植；CLI 不传 path_root 时跳过校验。
    """
    root = Path(path_root).resolve()
    for i, a in enumerate(args):
        prev = args[i - 1] if i > 0 else ""
        candidate = a if prev in _PATH_ARG_HINTS or not str(a).startswith("-") else None
        if candidate is None:
            continue
        p = Path(candidate).expanduser()
        if p.is_absolute() or (p.parts and p.parts[0] in {".", ".."}):
            resolved = p.resolve()
            if not resolved.is_relative_to(root):
                raise PermissionError(f"路径逃逸工作区，拒绝执行: {candidate}")


def _run_script(
    script: Path,
    args: list[str],
    timeout: int,
    *,
    label: str,
    path_root: Path | None = None,
) -> dict[str, Any]:
    if path_root is not None:
        assert_paths_within(args, path_root)
    cmd = [_PY, str(script), *args]
    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(SCRIPT_DIR),
        )
        return {
            "label": label,
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "elapsed_seconds": round(time.monotonic() - start, 2),
            "stdout": (proc.stdout or "").strip(),
            "stderr": (proc.stderr or "").strip(),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "label": label,
            "ok": False,
            "returncode": -2,
            "elapsed_seconds": round(time.monotonic() - start, 2),
            "stdout": "",
            "stderr": f"EXEC_ERROR: {exc}",
        }


def run_paragraph_gate(prose: Path, *, path_root: Path | None = None) -> dict[str, Any]:
    """薄封装：nww_paragraph_gate.py。"""
    if not prose or not Path(prose).is_file():
        return {
            "label": "paragraph_gate",
            "ok": False,
            "returncode": -1,
            "stdout": "",
            "stderr": f"正文缺失: {prose}",
        }
    return _run_script(
        PARAGRAPH_GATE_SCRIPT,
        ["--prose", str(prose), "--json"],
        15,
        label="paragraph_gate",
        path_root=path_root,
    )


def run_enforce(book: Path, prose: Path, *, path_root: Path | None = None) -> dict[str, Any]:
    """薄封装：nww_enforce.py。"""
    if not prose or not Path(prose).is_file():
        return {
            "label": "enforce",
            "ok": False,
            "returncode": -1,
            "stdout": "",
            "stderr": f"正文缺失: {prose}",
        }
    return _run_script(
        ENFORCE_SCRIPT,
        ["--book", str(book), "--file", str(prose)],
        60,
        label="enforce",
        path_root=path_root,
    )


def run_post_write(
    book: Path,
    chapter_id: int,
    prose: Path | None,
    *,
    skip_enforce: bool = False,
    path_root: Path | None = None,
) -> dict[str, Any]:
    """薄封装：nww_post_write.py（paragraph_gate + commit + cross_chapter）。

    path_root：Web 传入书根以启用路径沙箱；CLI 省略。
    """
    if prose is None or not Path(prose).is_file():
        return {
            "label": "post_write",
            "ok": False,
            "returncode": 2,
            "stdout": "",
            "stderr": f"post_write BLOCK: 正文缺失 chapter={chapter_id} prose={prose}",
        }
    args = [
        "--project", str(book),
        "--chapter", str(chapter_id),
        "--prose", str(prose),
        "--json",
    ]
    if skip_enforce:
        args.append("--skip-enforce")
    return _run_script(
        POST_WRITE_SCRIPT, args, 300, label="post_write", path_root=path_root
    )


def run_deslop_advisory(book: Path, chapter_id: int, prose: Path | None) -> dict[str, Any]:
    """Task 5 接入前：deslop 仅 advisory / skip / ok 记录，不阻断。

    若正文缺失 → skip；否则尝试 check-ai-patterns.js --check（有 node 则跑，无则 skip）。
    """
    _ = book, chapter_id  # 预留 Task 5 quality_loop 签名对齐
    if prose is None or not Path(prose).is_file():
        return {"label": "deslop", "ok": True, "status": "skip", "stdout": "", "stderr": "no prose"}
    js = SCRIPT_DIR / "check-ai-patterns.js"
    if not js.is_file():
        return {"label": "deslop", "ok": True, "status": "skip", "stdout": "", "stderr": "script missing"}
    node = "node"
    try:
        proc = subprocess.run(
            [node, str(js), "--check", str(prose)],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(SCRIPT_DIR),
        )
        # advisory：非 0 不阻断；status 仅区分检测是否报出模式
        return {
            "label": "deslop",
            "ok": True,
            "status": "ok" if proc.returncode == 0 else "advisory",
            "returncode": proc.returncode,
            "stdout": (proc.stdout or "")[:1200],
            "stderr": (proc.stderr or "")[:300],
        }
    except FileNotFoundError:
        return {"label": "deslop", "ok": True, "status": "skip", "stdout": "", "stderr": "node not found"}
    except Exception as exc:  # noqa: BLE001
        return {"label": "deslop", "ok": True, "status": "skip", "stdout": "", "stderr": str(exc)}


def build_packet_v2(
    book: Path,
    chapter_id: int,
    *,
    no_sidecar: bool = False,
    path_root: Path | None = None,
) -> dict:
    """调用 nww_packet_builder_v2（subprocess，避免 builder 内 sys.exit 污染调用方）。

    优先 skills 副本，回退 engine/scripts。
    path_root：Web 传入书根启用路径沙箱；CLI 省略。
    """
    script = PACKET_V2_SKILL if PACKET_V2_SKILL.is_file() else PACKET_V2_ENGINE
    if not script.is_file():
        raise RuntimeError(f"packet builder v2 缺失: {script}")
    args = [str(book), str(chapter_id)]
    if no_sidecar:
        args.append("--no-sidecar")
    if path_root is not None:
        assert_paths_within(args, path_root)
    # CLI: python nww_packet_builder_v2.py <book> <chapter_id>
    proc = subprocess.run(
        [_PY, str(script), *args],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(script.parent),
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip() or f"exit={proc.returncode}"
        raise RuntimeError(f"packet_v2 失败: {err}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"packet_v2 输出非 JSON: {exc}") from exc
