"""toolbox.py — 确定性护栏脚本执行器（白名单 + 用户书根注入 + fail-closed）。

复用 skills/nww-novel-skill/scripts/ 的护栏脚本（模型无关、fail-closed）：
  - nww_packet_builder.py       打包（细纲 → Writing Packet）
  - nww_delta_draft.py          逐章事务 delta 草稿预填（确定性提取）
  - nww_post_write.py           写章后单入口：enforce → commit → 审稿 → cross_chapter
  - check-ai-patterns.js        写后机械层（节点拍/禁用词/解释腔）
  - check-degeneration.js       模型退化检测（复读/截断/占位符）
  - normalize-punctuation.js    标点归一
  - nww_backup.py               写前原稿备份（重写/改写前幂等备份到 .backup/）
  - nww_cross_chapter_check.py  跨章重复/段落漂移/标题疲劳

铁则：所有脚本一律通过 subprocess 加载最新代码；任何参数中出现的「绝对路径」
必须位于 user_root 内，逃逸即拒绝（多用户隔离硬闸）。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any
import shutil

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SCRIPTS = _REPO_ROOT / "skills" / "nww-novel-skill" / "scripts"
# 部分脚本（如 nww_packet_builder.py）在 engine/scripts/ 提供，白名单命中后按
# skill → engine 顺序定位，保证不改动任何原脚本（纯执行器视角的路径回退）。
_ENGINE_SCRIPTS = _REPO_ROOT / "engine" / "scripts"
_PY = sys.executable


def scripts_dir() -> Path:
    """共用脚本根（单一事实源）：V13 道闸策略层等模块从此处加载，避免路径漂移。"""
    return _SCRIPTS


def _resolve_node() -> str:
    """定位 node 可执行文件（供 run_script 跑 .js 检测器）。

    裸 "node" 依赖服务进程的 PATH —— 后端被桌面端 / 计划任务拉起时 PATH 常常
    与交互式 shell 不同，node 找不到会让 step_deslop 的三个 .js 检测器
    （check-ai-patterns / check-degeneration / normalize-punctuation）
    静默全失败：advisory 不报错，但等于没跑。
    故先用 shutil.which，再退回 NWW 环境约定的托管路径，最后才用裸名。
    """
    found = shutil.which("node")
    if found:
        return found
    versions = Path.home() / ".workbuddy" / "binaries" / "node" / "versions"
    if versions.is_dir():
        for p in sorted(versions.glob("*/bin/node"), reverse=True):
            if p.is_file():
                return str(p)
    return "node"


_NODE = _resolve_node()

# 白名单：只允许这些脚本被执行（fail-closed 的「闭」在未列入一律拒绝）
_ALLOWED = {
    "nww_packet_builder.py",
    "nww_packet_builder_v2.py",
    "nww_delta_draft.py",
    "nww_post_write.py",
    "nww_cross_chapter_check.py",
    "nww_causal_audit.py",
    "nww_chapter_tail_gate.py",
    "nww_track_commit.py",
    "nww_enforce.py",
    "nww_review_cycle.py",
    "nww_ctl.py",
    "nww_backup.py",

    "nww_rewrite_fuse.py",
    "check-ai-patterns.js",
    "check-degeneration.js",
    "normalize-punctuation.js",
    "check-outline-copy.js",
}

# 2026-11-05 治本：engine/scripts 与 skills/.../scripts 存在两份同名脚本且已漂移
# （nww_track_commit.py 差 474 行；engine 版缺 A-8/A-9/policy，五闸还读旧 bible 结构）。
# 单靠「记得同步两份」无法根治。这里做两件事：
#   ① resolve_script 作为全仓唯一路径解析入口，消除三条加载口径；
#   ② 回退命中 engine 副本时做能力闸：缺生产必需能力 → 明确拒绝，绝不静默放行。
# NWW_SCRIPT_FALLBACK_GUARD=0 可回到「回退不校验」的旧行为。
_SCRIPT_FALLBACK_GUARD = "NWW_SCRIPT_FALLBACK_GUARD"
_SCRIPT_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "nww_track_commit.py": (
        "def verify_cross_chapter_repeats",
        "def verify_foreshadow_overdue",
        "load_policy",
    ),
}


def _assert_fallback_capabilities(script: Path, script_name: str) -> None:
    needed = _SCRIPT_CAPABILITIES.get(script_name)
    if not needed:
        return
    try:
        text = script.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        raise PermissionError(f"[SCRIPT DRIFT] 无法读取回退副本 {script}: {exc}") from exc
    missing = [sym for sym in needed if sym not in text]
    if missing:
        raise PermissionError(
            f"[SCRIPT DRIFT] 回退副本 {script} 缺少生产必需能力 {missing}；"
            "拒绝静默回退（否则 A-8/A-9/policy 闸会全部失效）。"
            f"请以 skills/ 权威副本为准，或设 {_SCRIPT_FALLBACK_GUARD}=0 显式放行。"
        )


def resolve_script(script_name: str) -> Path | None:
    """全仓唯一脚本路径解析：skills 权威副本优先，engine 仅在能力校验通过后回退。"""
    if script_name not in _ALLOWED:
        raise PermissionError(f"脚本不在白名单: {script_name}")
    primary = _SCRIPTS / script_name
    if primary.is_file():
        return primary
    fallback = _ENGINE_SCRIPTS / script_name
    if fallback.is_file():
        if os.environ.get(_SCRIPT_FALLBACK_GUARD, "1") != "0":
            _assert_fallback_capabilities(fallback, script_name)
        return fallback
    return None


# stdout 返回上限（v2 packet 结构化后 JSON 增大）
_MAX_STDOUT = 100_000

# 绝对路径标记前缀：解析出可能为路径的参数做逃逸校验
_PATH_ARG_HINTS = ("--project", "--book", "--prose", "--out", "--input")


def _assert_paths_within(args: list[str], user_root: Path) -> None:
    """防御式路径隔离：args 中出现的绝对路径必须位于 user_root 内。

    对含路径前缀的标志参数及裸参数做 resolve 校验；相对路径（脚本自身定位用）
    不校验（不构成跨用户）。逃逸 → PermissionError，绝不传给子进程。
    """
    root = user_root.resolve()
    n = len(args)
    for i, a in enumerate(args):
        prev = args[i - 1] if i > 0 else ""
        candidate = a if prev in _PATH_ARG_HINTS or not a.startswith("-") else None
        if candidate is None:
            continue
        p = Path(candidate).expanduser()
        if p.is_absolute() or p.parts and p.parts[0] in {".", ".."}:
            resolved = p.resolve()
            if not resolved.is_relative_to(root):
                raise PermissionError(f"路径逃逸用户工作区，拒绝执行: {candidate}")


def run_script(
    script_name: str,
    args: list[str],
    user_root: Path,
    *,
    timeout: int = 300,
) -> dict[str, Any]:
    """执行白名单脚本。返回 {ok, returncode, stdout, stderr, elapsed}。"""
    script = resolve_script(script_name)
    if script is None:
        return {"ok": False, "returncode": -1, "stdout": "", "stderr": f"脚本缺失: {script_name}", "elapsed": 0}

    _assert_paths_within(args, user_root)

    if script.suffix == ".py":
        cmd = [_PY, str(script), *args]
    elif script.suffix == ".js":
        cmd = [_NODE, str(script), *args]
    else:
        raise PermissionError(f"不支持脚本类型: {script.suffix}")

    import time

    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(_SCRIPTS),
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            # P2: v2.2 packet 结构化后 JSON 超 8000 字符，输出上限扩容（8000→100_000）
            "stdout": proc.stdout.strip()[:_MAX_STDOUT],
            "stderr": proc.stderr.strip()[:4000],
            "elapsed": round(time.monotonic() - start, 2),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": -1, "stdout": "", "stderr": f"TIMEOUT after {timeout}s", "elapsed": timeout}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "returncode": -2, "stdout": "", "stderr": f"EXEC_ERROR: {exc}", "elapsed": round(time.monotonic() - start, 2)}