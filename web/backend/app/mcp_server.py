"""NWW MCP Server：单一入口 nww_novel_skill，内部按 action 分发到确定性脚本。

架构原则（与 /api/skill/run 一致）：
  - 云端不落盘书稿：zip 模式下临时目录跑完即删；无 zip 时从本地 workspace 打包后同样删临时文件
  - 无 LLM 调用：纯确定性脚本执行
  - 业务逻辑完全复用 skillrun.run_skill_payload()，零重复

分发模式：
  - 单脚本 action：action 名映射到一个脚本（如 wordcount → nww_wordcount.py）
  - 复合链路 action：action 名映射到脚本列表，按顺序执行并合并结果（如 audit → 字数→AI味→格式→表达）
  - 直通模式：action 直接传脚本文件名（.py/.js），原样执行
"""
from __future__ import annotations

import base64
import io
import json
import os
import traceback
import zipfile
from pathlib import Path
from typing import Optional

from .skillrun import run_skill_payload
from .auth import get_user, user_active

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_PROJECTS = _REPO_ROOT / "projects"
_SKILL_ROOT = Path(os.environ.get("NWW_SKILL_ROOT", _REPO_ROOT / "skills" / "nww-novel-skill"))

# 单脚本 action → 脚本名
_ACTION_MAP = {
    "wordcount": "nww_wordcount.py",
    "worldview": "nww_bible_check.py",
    "cross_chapter": "nww_cross_chapter_check.py",
    "delta_audit": "nww_delta_audit.py",
    "ai_flavor": "check-ai-patterns.js",
    "degeneration": "check-degeneration.js",
    "normalize_punctuation": "normalize-punctuation.js",
    "expression": "nww_expression_audit.py",
    "name_check": "nww_name_check.py",
    "sequence": "nww_sequence_audit.py",
    "tail_gate": "nww_chapter_tail_gate.py",
    "style_fingerprint": "nww_style_fingerprint.py",
    "voice": "nww_voice_check.py",
    "era": "nww_era_check.py",
    "commit": "nww_track_commit.py",
    "memory_pack": "nww_memory_pack.py",
    "phase": "nww_phase.py",
    "rule_enforce": "nww_enforce.py",
    "trope_guard": "nww_trope_guard.py",
}

# 复合链路 action → 脚本列表（按顺序执行）
_PIPELINE_MAP = {
    "audit": [
        ("wordcount", "nww_wordcount.py"),
        ("ai_flavor", "check-ai-patterns.js"),
        ("rule_enforce", "nww_enforce.py"),
        ("expression", "nww_expression_audit.py"),
    ],
    "pre_write": [
        ("worldview", "nww_bible_check.py"),
        ("cross_chapter", "nww_cross_chapter_check.py"),
    ],
    "finalize": [
        ("wordcount", "nww_wordcount.py"),
        ("ai_flavor", "check-ai-patterns.js"),
        ("rule_enforce", "nww_enforce.py"),
        ("expression", "nww_expression_audit.py"),
        ("commit", "nww_track_commit.py"),
        ("memory_pack", "nww_memory_pack.py"),
    ],
    "full_check": [
        ("worldview", "nww_bible_check.py"),
        ("cross_chapter", "nww_cross_chapter_check.py"),
        ("wordcount", "nww_wordcount.py"),
        ("ai_flavor", "check-ai-patterns.js"),
        ("rule_enforce", "nww_enforce.py"),
        ("expression", "nww_expression_audit.py"),
        ("name_check", "nww_name_check.py"),
        ("sequence", "nww_sequence_audit.py"),
        ("voice", "nww_voice_check.py"),
        ("era", "nww_era_check.py"),
    ],
}


def _auto_zip_workspace(user_id: str, book: str) -> str:
    """从本地 projects/{user_id}/{book}/ 打包为 base64 zip。"""
    base = Path(os.environ.get("NWW_PROJECTS_ROOT", _DEFAULT_PROJECTS))
    book_dir = base / user_id / book
    if not book_dir.is_dir():
        raise FileNotFoundError(f"本地 workspace 不存在：{book_dir}")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(book_dir.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(book_dir).as_posix())
    return base64.b64encode(buf.getvalue()).decode()


def _run_one(book: str, script: str, args: list, zip_b64: Optional[str], user_id: str) -> dict:
    """执行单个脚本，无 zip 时自动打包本地 workspace。"""
    if not zip_b64:
        zip_b64 = _auto_zip_workspace(user_id, book)
    payload = {
        "book": book,
        "script": script,
        "args": args or [],
        "zip": zip_b64,
    }
    return run_skill_payload(payload)


def _resolve_action(action: str) -> tuple[str, list[tuple[str, str]]]:
    """解析 action，返回 (模式, [(label, script), ...])。
    模式：pipeline（复合链路）/ single（单脚本）/ direct（直通脚本名）。
    """
    if action in _PIPELINE_MAP:
        return "pipeline", _PIPELINE_MAP[action]
    if action in _ACTION_MAP:
        return "single", [(action, _ACTION_MAP[action])]
    if action.endswith(".py") or action.endswith(".js"):
        return "direct", [(action, action)]
    raise ValueError(
        f"未知 action: {action}。可用单脚本: {', '.join(sorted(_ACTION_MAP.keys()))}；"
        f"可用链路: {', '.join(sorted(_PIPELINE_MAP.keys()))}；"
        f"云端操作: scan_market（扫榜选题）、analyze_book（拆文分析）；或直接传脚本文件名。"
    )


def _dispatch(book: str, action: str, args: list, zip_b64: Optional[str], user_id: str) -> dict:
    """按 action 分发执行，返回统一结果结构。"""
    # 云端黑盒操作：扫榜/拆文，方法论只在云端内部使用，只返回成品
    if action == "scan_market":
        from .cloud_ops import scan_market
        category = str(args[0]) if args else "all"
        return scan_market(category)
    if action == "analyze_book":
        from .cloud_ops import analyze_book
        text = str(args[0]) if args else ""
        title = str(args[1]) if len(args) > 1 else "对标书"
        return analyze_book(text, title)

    mode, steps = _resolve_action(action)

    if mode == "pipeline":
        # 复合链路：按顺序执行，第一个脚本用自动打包的 zip，后续复用同一个 zip（避免重复打包）
        results = []
        shared_zip = zip_b64  # 调用方传了就复用，没传第一个脚本会自动打包
        all_outputs = {}
        for label, script in steps:
            r = _run_one(book, script, args, shared_zip, user_id)
            if not shared_zip:
                # 第一个脚本自动打包后，后续复用同一个 zip 内容（需要重新打包因为临时目录已删）
                # 实际上每次 _run_one 都会重新打包，因为 run_skill_payload 跑完即删临时目录
                pass
            results.append({"action": label, "script": script, "exit_code": r["exit_code"],
                            "stdout": r["stdout"][-3000:], "stderr": r["stderr"][-1000:]})
            if r.get("output_files"):
                for f in r["output_files"]:
                    all_outputs[f] = r.get("outputs_zip")
        return {
            "mode": "pipeline",
            "action": action,
            "steps": len(steps),
            "results": results,
            "all_passed": all(r["exit_code"] == 0 for r in results),
        }
    else:
        label, script = steps[0]
        r = _run_one(book, script, args, zip_b64, user_id)
        return {
            "mode": mode,
            "action": action,
            "script": script,
            "exit_code": r["exit_code"],
            "stdout": r["stdout"],
            "stderr": r["stderr"],
            "outputs_zip": r.get("outputs_zip"),
            "output_files": r.get("output_files", []),
        }


def build_mcp_server():
    """构建 MCP Server 实例。单一入口 nww_novel_skill。"""
    from mcp.server.mcpserver import MCPServer

    mcp = MCPServer("NWW-V12", title="NWW-V12 小说创作引擎")

    @mcp.tool()
    def nww_novel_skill(
        book: str,
        action: str = "audit",
        args: list[str] | None = None,
        zip_b64: str | None = None,
        user_id: str = "admin",
    ) -> str:
        """NWW 小说创作引擎统一入口。本地 LLM 负责写正文，本工具负责确定性质检与状态追踪。

        【写新书流程】
        1. 本地用 nww-local skill 的 scan/analyze 方法论扫榜、拆文、立项
        2. 本地创建书籍目录结构（正文/大纲/设定/追踪）
        3. 每章写前：调用 action=pre_write 做世界观+跨章校验
        4. 本地 LLM 按方法论写正文
        5. 每章写完：调用 action=audit 做四步质检（字数→AI味→格式→表达）
        6. 根据质检结果在本地返修，直到 audit 通过
        7. 定稿：调用 action=finalize 提交状态+记忆打包

        【续写老书流程】
        1. 本地读取大纲/细纲/追踪状态
        2. 调用 action=pre_write 做写前校验
        3. 本地 LLM 写正文
        4. 调用 action=audit 质检 → 返修 → action=finalize 定稿

        【何时调用本工具】
        - 写前校验：action=pre_write
        - 写完质检：action=audit（最常用，四步链路）
        - 单章定稿：action=finalize（质检→提交→记忆）
        - 全量检查：action=full_check（十项全查）
        - 单项检查：action=wordcount/ai_flavor/worldview/cross_chapter 等

        Args:
            book: 书籍项目名称（对应 projects/{user_id}/{book}/ 目录）
            action: 操作类型。
                复合链路：audit（写完章节质检：字数→AI味→格式→表达）、
                        pre_write（写前校验：世界观→跨章）、
                        finalize（定稿：质检→提交→记忆）、
                        full_check（全量十项检查）。
                单脚本：wordcount / worldview / cross_chapter / ai_flavor / rule_enforce /
                        expression / name_check / sequence / voice / era / commit / memory_pack /
                        phase / delta_audit / degeneration / normalize_punctuation / style_fingerprint /
                        trope_guard（剧情结构指纹门禁：CD冷却+变奏检查+SimHash碰撞，参数 --chapter N --project 书根）。
                直通：直接传脚本文件名（如 nww_wordcount.py、check-ai-patterns.js）。
            args: 脚本命令行参数列表（可选）
            zip_b64: 可选。书项目的 base64 zip。不传时自动从本地 workspace 打包。
            user_id: 用户 ID，用于定位 workspace 目录，默认 admin
        """
        try:
            # 套餐校验：身份在连接时已认证，这里检查权限/时长
            u = get_user(user_id)
            if not u:
                return json.dumps({"error": "用户不存在", "blocked": True}, ensure_ascii=False)
            if not user_active(u):
                expires = u.get("expires_at", "未知")
                return json.dumps({
                    "error": f"您的 NWW 写作套餐已于 {expires} 到期，本次操作已拦截。请前往官网续费（续费后无需更换 API Key，刷新即可继续使用）。",
                    "blocked": True,
                    "plan_expired": True,
                    "expires_at": expires,
                }, ensure_ascii=False)
            result = _dispatch(book, action, args or [], zip_b64, user_id)
            return json.dumps(result, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({
                "error": str(exc),
                "traceback": traceback.format_exc()[-2000:],
            }, ensure_ascii=False)

    return mcp
