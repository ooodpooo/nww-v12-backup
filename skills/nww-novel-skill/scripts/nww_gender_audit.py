#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nww_gender_audit.py — 角色卡性别声明审计（只读，不改文件）

独立新模块，与既有流水线零耦合（2026-10-30 外脑裁决 P1-3）：
由 nww_book_audit.py 的 run() 聚合，也可单独命令行调用或被 commit hook / CI 复用。

背景：主角角色卡缺性别字段时，代词性别机械闸（pronoun_gate）静默跳过，
写手侧默认按男性代词书写正文（实例：用泡面征服修仙界·林小凡）。
本模块把「性别声明缺失/冲突」变成显式可见的审计信号。

审计规则（与 nww_gates_shared.protagonist_gender 的识别口径一致）：
  扫描 设定/角色/*.md（兼容 设定/角色卡/、设定/人物/）；
  仅对含姓名字段（名字/姓名/名称/name）的角色卡生效；
  检查卡头 8 行是否存在可识别性别声明：
    标准字段  性别：男 / 性别：女（全角/半角冒号、前置列表符号均可）
    旧标注    女主 / 女主角 / （女）；男主 / 男主角 / （男）
  缺失 → WARNING；声明冲突（显式字段 vs 旧标注、或双旧标注）→ WARNING。
  本轮统一 WARNING 不 BLOCK（外脑裁决：先让问题可见，稳定后再收紧）。

用法：
  python nww_gender_audit.py --book <书项目根> [--json]
  --json 时输出结构化 findings（供脚本消费），否则打印人类可读报告。
"""
import argparse
import json
import re
import sys
from pathlib import Path

HEAD_LINES = 8
NAME_FIELD_RE = r"^(?:[\s　])*[-*]?\s*(?:名字|姓名|名称|name)(?:[\s　])*(?:：|:)"
EXPLICIT_RE = r"性别\s*[：:]\s*(男|女)"
LEGACY_FEMALE_RE = r"女主|（女）|女主角"
LEGACY_MALE_RE = r"男主|（男）|男主角"
CARD_DIRS = ("设定/角色", "设定/角色卡", "设定/人物")
# 认知误认登记（2026-10-30 外脑第五轮裁决三：独立轻量文件方案A——commit 白名单
# 会丢弃 _tracking-state.json 未知字段，认知态走独立文件、schema 独立管理）
COGNITIVE_FILE = "追踪/_cognitive-state.json"
PROSE_DIR = "正文"


def audit_gender_declaration(book_path) -> dict:
    """审计书根下角色卡的性别声明，返回结构化结果（只读）。"""
    book = Path(book_path)
    findings: list[dict] = []
    cards: list[str] = []

    card_root = None
    for d in CARD_DIRS:
        cand = book / d
        if cand.is_dir():
            card_root = cand
            break
    if card_root is None:
        return {
            "cards": 0,
            "findings": [],
            "summary": "未找到角色卡目录（设定/角色 等），跳过性别声明审计",
        }

    for f in sorted(card_root.glob("*.md")):
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except Exception as exc:  # noqa: BLE001 — 读不动的卡报 WARNING 而非中断
            findings.append({
                "level": "WARNING", "file": str(f.relative_to(book)),
                "character": f.stem,
                "reason": f"角色卡读取失败：{type(exc).__name__}",
            })
            continue
        head = "\n".join(lines[:HEAD_LINES])
        has_name = any(re.search(NAME_FIELD_RE, ln) for ln in lines[:HEAD_LINES])
        # 标题行带角色名（# 林小凡（主角））也算有姓名信息：stem 即角色名
        if not has_name and not f.stem.strip():
            continue
        cards.append(f.stem)

        explicit_m = re.search(EXPLICIT_RE, head)
        legacy_female = re.search(LEGACY_FEMALE_RE, head)
        legacy_male = re.search(LEGACY_MALE_RE, head)
        explicit = explicit_m.group(1) if explicit_m else ""
        declared = [x for x in (explicit, "女" if legacy_female else "", "男" if legacy_male else "") if x]
        if len(set(declared)) > 1:
            findings.append({
                "level": "WARNING", "file": str(f.relative_to(book)),
                "character": f.stem,
                "reason": f"性别声明冲突：{ '/'.join(declared) }，代词闸按缺失处理，请统一为单一声明",
            })
        elif not declared:
            findings.append({
                "level": "WARNING", "file": str(f.relative_to(book)),
                "character": f.stem,
                "reason": (
                    f"头 {HEAD_LINES} 行缺性别声明（性别：男/女 或 女主/男主角/（女）/（男）），代词闸不生效"
                    " [原则:显式优于隐式,拒绝弱证据推断]"
                ),
            })

    cognitive = audit_cognitive_state(book_path)
    return {
        "cards": len(cards),
        "findings": findings,
        "cognitive_findings": cognitive,
        "summary": (
            f"扫描角色卡 {len(cards)} 张，性别声明缺失/冲突 {len(findings)} 处"
            if cards else "角色卡目录为空，跳过性别声明审计"
        ),
    }


def audit_cognitive_state(book_path) -> list:
    """认知误认登记审计（2026-10-30 外脑第五轮裁决三，只读）。

    读 追踪/_cognitive-state.json（独立文件，commit 流程零触碰）：
      登记未揭晓（reveal_chapter=null）→ INFO 提示；
      已揭晓 → 扫描 ≥ reveal_chapter 各章，主角段误认代词 >0 → WARNING（不 BLOCK，
      口径复用 nww_gates_shared.protagonist_pronoun_counts 的 Local_Actor 段落归属）。
    文件缺失/为空 → 静默返回 []（大多数书无此登记，属正常）。"""
    book = Path(book_path)
    path = book / COGNITIVE_FILE
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — 解析失败显式 WARNING 而非中断
        return [{
            "level": "WARNING", "file": COGNITIVE_FILE, "character": "",
            "reason": f"认知状态文件解析失败：{type(exc).__name__}",
        }]
    cs = data.get("cognitive_state") or {}
    if not isinstance(cs, dict):
        return [{
            "level": "WARNING", "file": COGNITIVE_FILE, "character": "",
            "reason": "cognitive_state 字段不是对象，请核对文件结构（schema_version 独立管理，当前为 1）",
        }]
    findings: list[dict] = []
    for proto, entry in cs.items():
        if not isinstance(entry, dict):
            findings.append({
                "level": "WARNING", "file": COGNITIVE_FILE, "character": str(proto),
                "reason": "登记条目不是对象，请核对结构：{misidentified_as, by_roles, reveal_chapter, note}",
            })
            continue
        mis = str(entry.get("misidentified_as") or "").strip().lower()
        if mis not in ("male", "female"):
            findings.append({
                "level": "WARNING", "file": COGNITIVE_FILE, "character": str(proto),
                "reason": f"misidentified_as 取值异常（{mis or '空'}），值域 male/female",
            })
            continue
        by = "、".join(str(x) for x in (entry.get("by_roles") or [])) or "未登记误认方"
        reveal = entry.get("reveal_chapter")
        if reveal is None:
            findings.append({
                "level": "INFO", "file": COGNITIVE_FILE, "character": str(proto),
                "reason": f"认知误认登记未揭晓（误认方：{by}）；揭晓章确定后回填 reveal_chapter，正文代词须切回真实性别",
            })
            continue
        try:
            reveal_n = int(reveal)
        except (TypeError, ValueError):
            findings.append({
                "level": "WARNING", "file": COGNITIVE_FILE, "character": str(proto),
                "reason": f"reveal_chapter 非整数（{reveal!r}），无法执行揭晓后代词扫描",
            })
            continue
        try:
            from nww_gates_shared import (  # 延迟导入：口径与代词闸完全一致
                protagonist_gender, protagonist_pronoun_counts,
                resolve_protagonist_anchor,
            )
            proto_names, _src, _card = resolve_protagonist_anchor(book)
            gender = protagonist_gender(book)
        except Exception as exc:  # noqa: BLE001
            findings.append({
                "level": "WARNING", "file": COGNITIVE_FILE, "character": str(proto),
                "reason": f"代词闸口径模块不可用：{type(exc).__name__}，揭晓后扫描跳过",
            })
            continue
        if not proto_names or not gender:
            findings.append({
                "level": "WARNING", "file": COGNITIVE_FILE, "character": str(proto),
                "reason": "主角锚或性别声明缺失，揭晓后误认代词扫描跳过（先完成性别登记）",
            })
            continue
        wrong_char = "他" if mis == "male" else "她"
        for f in sorted((book / PROSE_DIR).glob("第*章*.md")):
            m = re.search(r"第(\d+)章", f.name)
            if not m or int(m.group(1)) < reveal_n:
                continue
            ch = int(m.group(1))
            try:
                text = f.read_text(encoding="utf-8")
            except Exception as exc:  # noqa: BLE001
                findings.append({
                    "level": "WARNING", "file": str(f.relative_to(book)), "character": str(proto),
                    "reason": f"正文读取失败：{type(exc).__name__}，该章跳过",
                })
                continue
            he, she = protagonist_pronoun_counts(
                book, ch, text, proto_names=proto_names, gender=gender)
            wrong_n = he if mis == "male" else she
            if wrong_n > 0:
                findings.append({
                    "level": "WARNING", "file": str(f.relative_to(book)), "character": str(proto),
                    "reason": (
                        f"已于第 {reveal_n} 章揭晓性别，第 {ch} 章主角段仍出现误认代词"
                        f"「{wrong_char}」×{wrong_n}，请切回真实性别对应代词"
                    ),
                })
    return findings


def render(result: dict) -> str:
    lines = [
        "### 性别声明审计（nww_gender_audit）",
        f"- {result.get('summary', '')}",
    ]
    for fd in result.get("findings", []):
        lines.append(f"  * [{fd['level']}] {fd.get('file', '')} · {fd.get('character', '')}：{fd.get('reason', '')}")
    if not result.get("findings"):
        lines.append("  * 无缺失/冲突，代词性别闸可正常生效")
    cognitive = result.get("cognitive_findings") or []
    if cognitive:
        lines.append("  * 认知误认登记（追踪/_cognitive-state.json）：")
        for fd in cognitive:
            lines.append(f"    - [{fd['level']}] {fd.get('character', '')}：{fd.get('reason', '')}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="NWW 角色卡性别声明审计（只读）")
    parser.add_argument("--book", required=True, help="书项目根目录")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出 findings")
    args = parser.parse_args()
    result = audit_gender_declaration(args.book)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render(result))
    # 本轮统一 WARNING：不阻断（exit 0）；未来升级硬闸时在此处按 ERROR 计数退出
    sys.exit(0)


if __name__ == "__main__":
    main()
