#!/usr/bin/env python3
"""Merge current nww-setup Claude hooks without preserving stale matchers.

Only registrations whose command invokes a known nww-setup hook script are
managed. They are removed from their historical event/matcher block before the
current template is appended; unrelated hooks and top-level settings survive.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any


MANAGED_HOOK_SCRIPTS = frozenset(
    {
        "check-naming-after-write.sh",
        "check-prose-after-write.sh",
        "detect-story-gaps.sh",
        "guard-outline-before-prose.sh",
        "post-compact.sh",
        "pre-compact.sh",
        "session-end.sh",
        "session-start.sh",
        "validate-story-commit.sh",
    }
)

# 部署模式下的 Claude Code hooks 注册模板（{PROJECT} 占位，部署时替换为绝对路径）。
# 事件映射与 hooks.json 部署模板一致；重复部署时由
# strip_managed_registrations 先移除旧注册再合并，幂等。
DEPLOY_TEMPLATE = {
    "hooks": {
        "SessionStart": [
            {
                "hooks": [
                    {"type": "command", "command": "bash {PROJECT}/.claude/hooks/session-start.sh", "timeout": 10},
                    {"type": "command", "command": "bash {PROJECT}/.claude/hooks/detect-story-gaps.sh", "timeout": 10},
                ]
            }
        ],
        "SessionEnd": [
            {
                "hooks": [
                    {"type": "command", "command": "bash {PROJECT}/.claude/hooks/session-end.sh", "timeout": 5}
                ]
            }
        ],
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [
                    {
                        "type": "command",
                        "command": "bash {PROJECT}/.claude/hooks/validate-story-commit.sh",
                        "timeout": 15,
                        "if": "Bash(git commit*)",
                    }
                ],
            },
            {
                "matcher": "Bash|Write|Edit|MultiEdit",
                "hooks": [
                    {
                        "type": "command",
                        "command": "bash {PROJECT}/.claude/hooks/guard-outline-before-prose.sh",
                        "timeout": 10,
                    }
                ],
            },
        ],
        "PostToolUse": [
            {
                "matcher": "Write|Edit|MultiEdit",
                "hooks": [
                    {
                        "type": "command",
                        "command": "bash {PROJECT}/.claude/hooks/check-prose-after-write.sh",
                        "timeout": 15,
                    },
                    {
                        "type": "command",
                        "command": "bash {PROJECT}/.claude/hooks/check-naming-after-write.sh",
                        "timeout": 15,
                    },
                ],
            }
        ],
        "PreCompact": [
            {
                "hooks": [
                    {"type": "command", "command": "bash {PROJECT}/.claude/hooks/pre-compact.sh", "timeout": 10}
                ]
            }
        ],
        "PostCompact": [
            {
                "hooks": [
                    {"type": "command", "command": "bash {PROJECT}/.claude/hooks/post-compact.sh", "timeout": 10}
                ]
            }
        ],
    }
}

DEPLOY_HOOK_FILES = [
    "check-naming-after-write.sh",
    "check-prose-after-write.sh",
    "detect-story-gaps.sh",
    "guard-outline-before-prose.sh",
    "post-compact.sh",
    "pre-compact.sh",
    "session-end.sh",
    "session-start.sh",
    "validate-story-commit.sh",
]
DEPLOY_CLI_FILES = [
    "nww_hook_cli.js",
    "nww_hook_core.js",
    "nww_name_check.py",
    # V12.5（2026-09-13）：check-prose-after-write.sh 的段落门需要这个脚本，
    # 且该脚本 import nww_text（段落切分的单一权威实现），两者都必须一起部署。
    # 之前漏部署 → hook 里 `[ -f "$PG_SCRIPT" ]` 恒假 → 改写段那段静默跳过，
    # 宿主侧等于没有段落门。
    "nww_paragraph_gate.py",
    "nww_text.py",
]
DEPLOY_LIB_FILES = ["common.sh", "sentinel.sh"]


class MergeError(ValueError):
    pass


def is_story_setup_hook(hook: object) -> bool:
    if not isinstance(hook, dict) or not isinstance(hook.get("command"), str):
        return False
    normalized = hook["command"].replace("\\", "/")
    return any(f"/.claude/hooks/{name}" in normalized for name in MANAGED_HOOK_SCRIPTS)


def require_hooks(document: object, label: str) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise MergeError(f"{label} root must be a JSON object")
    hooks = document.get("hooks", {})
    if not isinstance(hooks, dict):
        raise MergeError(f"{label}.hooks must be a JSON object")
    return hooks


def strip_managed_registrations(hooks: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for event, raw_blocks in hooks.items():
        if not isinstance(raw_blocks, list):
            cleaned[event] = copy.deepcopy(raw_blocks)
            continue
        blocks: list[Any] = []
        for raw_block in raw_blocks:
            if not isinstance(raw_block, dict) or not isinstance(raw_block.get("hooks"), list):
                blocks.append(copy.deepcopy(raw_block))
                continue
            block = copy.deepcopy(raw_block)
            block["hooks"] = [hook for hook in block["hooks"] if not is_story_setup_hook(hook)]
            if not block["hooks"]:
                continue
            blocks.append(block)
        if blocks:
            cleaned[event] = blocks
    return cleaned


def merge_documents(existing: object, template: object) -> dict[str, Any]:
    existing_hooks = require_hooks(existing, "existing")
    template_hooks = require_hooks(template, "template")
    for event, blocks in template_hooks.items():
        if not isinstance(blocks, list):
            raise MergeError(f"template.hooks.{event} must be an array")

    result = copy.deepcopy(existing)
    assert isinstance(result, dict)
    merged = strip_managed_registrations(existing_hooks)
    for event, blocks in template_hooks.items():
        if event in merged and not isinstance(merged[event], list):
            raise MergeError(f"existing.hooks.{event} must be an array")
        merged.setdefault(event, []).extend(copy.deepcopy(blocks))
    result["hooks"] = merged
    return result


def read_json(path: Path, *, missing_ok: bool = False) -> object:
    if missing_ok and not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MergeError(f"unable to read {path}: {exc}") from exc


def atomic_write_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    payload = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def deploy_hooks(project: Path, hooks_dir: Path, lib_dir: Path, cli_dir: Path) -> None:
    """把 hooks 脚本、node 共享核与 lib 部署到 {project}/.claude/hooks/，并把
    hooks 注册模板合并进 {project}/.claude/settings.local.json，最后写 .nww-deployed。
    幂等：重复执行覆盖同名托管文件、注册先 strip 再合并。
    """
    target = project / ".claude" / "hooks"
    target.mkdir(parents=True, exist_ok=True)

    missing: list[str] = []
    for name in DEPLOY_HOOK_FILES:
        src = hooks_dir / name
        if not src.exists():
            missing.append(str(src))
            continue
        shutil.copy2(src, target / name)
    for name in DEPLOY_CLI_FILES:
        src = cli_dir / name
        if not src.exists():
            missing.append(str(src))
            continue
        shutil.copy2(src, target / name)
    lib_target = target / "lib"
    lib_target.mkdir(exist_ok=True)
    for name in DEPLOY_LIB_FILES:
        src = lib_dir / name
        if not src.exists():
            missing.append(str(src))
            continue
        shutil.copy2(src, lib_target / name)
    if missing:
        raise MergeError("部署源缺失: " + ", ".join(missing))

    project_abs = str(project.resolve())
    template = json.loads(json.dumps(DEPLOY_TEMPLATE).replace("{PROJECT}", project_abs))
    settings_path = project / ".claude" / "settings.local.json"
    atomic_write_json(
        settings_path,
        merge_documents(read_json(settings_path, missing_ok=True), template),
    )
    (project / ".nww-deployed").write_text("agents_version: 25\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--existing", type=Path)
    parser.add_argument("--template", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--project", type=Path, help="部署目标书根；给出时进入部署模式")
    parser.add_argument("--hooks-dir", type=Path, help="源 hooks 目录（含 8 个 .sh）")
    parser.add_argument("--lib-dir", type=Path, help="源 lib 目录（缺省取 hooks-dir 同级 scripts/lib）")
    parser.add_argument("--cli-dir", type=Path, help="源 node 共享核目录（缺省取 hooks-dir 同级 scripts）")
    args = parser.parse_args()
    try:
        if args.project is not None:
            if args.hooks_dir is None:
                raise MergeError("部署模式需要 --hooks-dir")
            lib_dir = args.lib_dir or (args.hooks_dir.parent / "scripts" / "lib")
            cli_dir = args.cli_dir or (args.hooks_dir.parent / "scripts")
            deploy_hooks(args.project, args.hooks_dir, lib_dir, cli_dir)
            return 0
        if args.existing is None or args.template is None or args.output is None:
            parser.print_usage(sys.stderr)
            return 2
        atomic_write_json(
            args.output,
            merge_documents(read_json(args.existing, missing_ok=True), read_json(args.template)),
        )
    except MergeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
