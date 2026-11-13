"""
server.write — 写章薄封装（唯一实现 = Web 生产链）

2026-11-01 统一：CLI 不再维护第二套写章实现。原先 Phase 2 直连 LLM（自带 prompt /
温度 / max_tokens / 先落盘后跑门禁 / deslop rewriter=None）的 CLI 侧调优全部关闭，
改为直接调用 web/backend/app/agents_runtime/pipeline.run_write_pipeline —— 与 WebUI
完全同一条链：

  悬空章闸 → 自动补细纲 / 自动规划 → packet(v2) → 主笔生成（输出完整性 + 字数门禁）
  → normalize/落盘 → 代词闸 → 字数收敛 → delta → 一致性审查 → deslop → enforce
  → 审稿闭环 → commit（事务 / 恢复台账）

环境变量：
  NWW_CLI_WRITE=0  硬关：拒绝执行 CLI 写章（提示改用 Web 端），运维禁用用。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_WEB_BACKEND = _REPO_ROOT / "web" / "backend"
_DISABLE_ENV = "NWW_CLI_WRITE"
_DETAIL_LIMIT = 300


def _disabled() -> bool:
    """NWW_CLI_WRITE=0 → 禁用 CLI 写章（未设置或其他值一律启用）。"""
    return os.environ.get(_DISABLE_ENV, "").strip().lower() == "0"


def _load_pipeline():
    """导入 Web 生产链编排器（web/backend 挂到 sys.path；失败抛异常由 run 报错）。"""
    if str(_WEB_BACKEND) not in sys.path:
        sys.path.insert(0, str(_WEB_BACKEND))
    from app.agents_runtime import pipeline  # type: ignore  # noqa: E402

    return pipeline


def _print_recovery_hint() -> None:
    """失败后的恢复指引（与 Web 端一致：模型中心 + 页面重写 + 恢复台账）。"""
    print("  处理建议:")
    print("    1. 模型中心（WebUI）确认已设置全局默认模型（CLI 与 Web 读同一份配置）")
    print("    2. 按上方 ❌ 环节定位：审稿未过线 / 门禁 BLOCK 的章请在 Web 端「重写」该章")
    print("    3. 提交中断时查询 追踪/failed_transactions/chapter_*.json 与 追踪/_pending_commit.json")


def _print_stages(stages) -> None:
    """打印 pipeline 环节报告（list[dict]：name/ok/detail）。"""
    for s in stages or []:
        if not isinstance(s, dict):
            continue
        mark = "✅" if s.get("ok") else "❌"
        detail = str(s.get("detail") or "").replace("\n", " ").strip()
        if len(detail) > _DETAIL_LIMIT:
            detail = detail[:_DETAIL_LIMIT] + "…"
        print(f"  {mark} {s.get('name')}: {detail}")


def _dry_run_preview(pipeline, book_root: Path, chapter_id: int) -> int:
    """--dry-run：只构建 Packet（step_packet --no-sidecar，不调 LLM、正式目录零变化）。"""
    print("📋 Packet 预览（--dry-run：不调用 LLM，不写正文）")
    try:
        outline, packet = pipeline.step_packet(book_root, chapter_id, dry_run=True)
    except Exception as exc:  # noqa: BLE001 — 细纲/打包失败按可读原因返回
        print(f"  ❌ Packet 构建失败: {exc}")
        print()
        print("  请先确认细纲已按七要素模板填写（python -m server plan）")
        return 1

    try:
        rel = outline.relative_to(book_root).as_posix()
    except ValueError:
        rel = outline.name
    print(f"  ✅ 细纲: {rel}")
    print(f"  📦 Token 预估: ~{int(len(json.dumps(packet, ensure_ascii=False)) * 1.5)}")

    beat = packet.get("beat")
    if not beat:
        scene_chain = packet.get("scene_chain") or []
        beat = (scene_chain[0].get("summary") if scene_chain and isinstance(scene_chain[0], dict) else None) or "N/A"
    print(f"     核心事件: {str(beat)[:50]}...")
    chars = packet.get("active_characters") or packet.get("character_contract") or []
    print(f"     出场角色: {[c.get('name') for c in chars if isinstance(c, dict)]}")
    print()
    print("💡 去掉 --dry-run 参数即执行完整写章链（与 Web 端同一流程）")
    return 0


def run(args) -> int:
    """写作流水线入口（薄封装 Web pipeline）。"""
    book_root = Path(args.book_root)
    chapter_id = args.chapter_id
    dry_run = getattr(args, "dry_run", False)

    print("✍️  写作流水线（Web pipeline 薄封装）")
    print("=" * 60)
    print(f"书名: {book_root.name}")
    print(f"章节: 第 {chapter_id} 章")
    print()

    if _disabled():
        print(f"  ⛔ {_DISABLE_ENV}=0：CLI 写章已禁用，请在 Web 端执行写章（同一份模型中心配置）。")
        return 1

    try:
        pipeline = _load_pipeline()
    except Exception as exc:  # noqa: BLE001 — 依赖缺失 / 安全启动闸给出可操作提示
        msg = str(exc)
        print(f"  ❌ 无法加载 Web 生产链: {msg}")
        if "NWW_WEB_SECRET" in msg:
            print()
            print("  CLI 与 Web 服务共用同一套安全启动校验（web/backend/app/auth.py）：")
            print("    生产：设置环境变量 NWW_WEB_SECRET（32+ 随机串，与 Web 服务同一值）")
            print("    本地联调：设置 NWW_DEV_WEAK_SECRET=1 后重试")
        else:
            print("     请确认 web/backend 依赖已安装（fastapi / httpx / pydantic），或改用 Web 端写作。")
        return 1

    if dry_run:
        return _dry_run_preview(pipeline, book_root, chapter_id)

    print("📋 写章链：packet → 主笔 → 归一/门禁 → 审稿 → commit（耗时较长，请勿中断）")
    print()
    try:
        result = pipeline.run_write_pipeline(book_root, chapter_id)
    except KeyboardInterrupt:
        print()
        print("  ⛔ 已中断：未 commit 的内容不会进入权威状态")
        return 2
    except Exception as exc:  # noqa: BLE001 — 编排器兜底异常
        print(f"  ❌ 写章链异常: {exc}")
        print()
        _print_recovery_hint()
        return 1

    print("📋 环节报告")
    _print_stages(result.get("stages") or [])
    print()

    if not result.get("ok"):
        print("❌ 写章未完成（上方首个 ❌ 环节即阻断点）")
        print()
        _print_recovery_hint()
        return 2

    print("✅ 写章完成（已提交）")
    return 0


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("book_root")
    parser.add_argument("chapter_id", type=int)
    parser.add_argument("--dry-run", action="store_true", help="只生成 Packet，不写正文")
    sys.exit(run(parser.parse_args()))
