#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nww_gate_policy.py — V13 道闸策略层（单一事实源，2026-11-13）。

移植自 /Users/oodpoo/NWW_V13 的道闸哲学，证据：

* ``src/novel_app/validators.py:626,848-855`` —— 只读事实卡「只陈述数值，不做任何
  阻断/判决」，且由 ``tests/test_style_fact_card.py:36`` 硬禁 ok/block/level/score/pass。
* ``src/novel_app/pipeline.py:842-844`` —— ``check_chapter`` 的结果只记录，不用于拦截落盘。
* ``src/novel_app/ledger.py:9`` / ``context.py:172`` —— 零阻断、fail-open、异常只留一行诊断。
* ``src/continuity_engine/SKILL.md:125-130`` —— fail-open 保证（异常降级，主链路返回值不变）。
* ``USAGE.md:700-709`` —— 弹性闸：撞闸「带着结论停」，不锁死。
* V13 运行时源码中 ``stop_and_report`` / 熔断 / 锁死章节：精确检索零命中。

本模块负责「质量/语义/流程闭环」闸的**阻断决策**，不实现任何闸的判据：

* ``v13``（生产默认）：闸只记录事实 + 留痕 + 一行诊断，动作与落盘继续；裁决权交作者。
* ``strict``（``NWW_GATE_MODE=strict``）：原样抛出调用方异常，完全回到 V12 现状。

pytest 下若无显式 env 则缺省 ``strict``：存量闸门回归断言（大量断言 BLOCK / exit 2）
必须保持原义；新增的 v13 专项测试用 monkeypatch 显式切到 ``v13``，两条路径都被覆盖。
显式 env 永远优先，因此部署环境可强制指定，不存在「测试才生效」的隐藏分叉。

「产物有效性」闸（空正文、结构非法、文件缺失、写盘失败、事务版本真冲突）不在本模块
范围内，调用方不得用 ``guard`` 包裹它们 —— 与 V13 ``pipeline.py:799-821`` 一致：
空/回显/占位仍不覆盖已有章节。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable

MODE_ENV = "NWW_GATE_MODE"
REPORT_DIR = "闸报告"
# 第二阶段（2026-11-13）：作者裁决台账。与事实卡同目录，绝不写进事实卡本体，
# 保证「事实卡只陈述事实」的 V13 纪律不被裁决状态污染。
DECISION_FILE = "裁决.json"
DECISION_ACTIONS = ("accept", "recheck", "rollback")
DECISION_EXTRA_KEYS = ("check_ok", "check_detail", "rollback_to", "recheck_mode")
# V13 事实卡纪律：报告里不出现任何判决字段（tests/test_style_fact_card.py:36）
JUDGEMENT_KEYS = ("ok", "block", "level", "score", "pass", "passed", "verdict")
_MAX_FINDINGS = 200
_MAX_DETAIL = 2000


def gate_mode() -> str:
    """返回 'v13' 或 'strict'。显式 env 优先；未指定时 pytest 走 strict、生产走 v13。"""
    raw = os.environ.get(MODE_ENV)
    if raw is not None and str(raw).strip():
        value = str(raw).strip().lower()
        return value if value in ("v13", "strict") else "strict"  # 非法值 fail-closed
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return "strict"
    return "v13"


def is_v13() -> bool:
    return gate_mode() == "v13"


def _report_path(project: Path, chapter: int) -> Path:
    return Path(project) / "追踪" / REPORT_DIR / f"第{int(chapter):03d}章.json"


def report(project: Path, chapter: int) -> dict[str, Any]:
    """只读事实卡：返回本章闸事实，无报告时返回 status=empty。绝不抛异常。"""
    try:
        path = _report_path(project, chapter)
        if not path.is_file():
            return _empty_card(chapter)
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _empty_card(chapter)
        return data
    except Exception as exc:  # noqa: BLE001 — fail-open：读失败等同无报告
        print(f"[GATE] report read failed: {exc}", file=sys.stderr)
        return _empty_card(chapter)


def _empty_card(chapter: int) -> dict[str, Any]:
    return {"chapter": int(chapter), "mode": "v13", "status": "empty",
            "count": 0, "findings": []}


def record(project: Path, chapter: int, code: str, detail: Any,
           *, source: str = "gate", replace_code: bool = False) -> dict[str, Any] | None:
    """追加一条闸事实到 追踪/闸报告/第N章.json。原子替换；异常只留一行诊断。

    replace_code=True 时先移除同 code 的旧事实再追加（供「同一事实每轮重算」的观测，
    如 voice_print 的锚点统计），默认 False 保持既有只追加语义。
    """
    if not is_v13():
        return None
    try:
        path = _report_path(project, chapter)
        path.parent.mkdir(parents=True, exist_ok=True)
        card = report(project, chapter)
        if card.get("status") == "empty":
            card = {"chapter": int(chapter), "mode": "v13", "status": "reported",
                    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "count": 0, "findings": []}
        findings = [f for f in card.get("findings", []) if isinstance(f, dict)]
        if replace_code:
            findings = [f for f in findings if f.get("code") != str(code)]
        findings.append({
            "code": str(code),
            "source": str(source),
            "detail": str(detail or "")[:_MAX_DETAIL],
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
        card["findings"] = findings[-_MAX_FINDINGS:]
        card["count"] = len(card["findings"])
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return card
    except Exception as exc:  # noqa: BLE001 — 零阻断：报告写失败不改变任何动作结果
        print(f"[GATE] report write failed: {exc}", file=sys.stderr)
        return None


def note(code: str, detail: Any) -> None:
    """一行诊断（stderr），让闸事实在 CLI / 回执 detail 中可见。"""
    print(f"[GATE][V13] {code}: {str(detail or '')[:300]}", file=sys.stderr)


def guard(project: Path, chapter: int, code: str, fn: Callable[..., Any],
          *args: Any, **kwargs: Any) -> Any:
    """执行一道质量闸。strict 原样抛；v13 记录事实后继续（返回 None）。

    判据、调用顺序、参数一律不变 —— 只把「抛异常」变成「可降级的抛异常」。
    """
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 — 闸的违规异常（TrackingError 等）
        if not is_v13():
            raise
        record(project, chapter, code, str(exc))
        note(code, str(exc))
        return None


def gate_block(project: Path, chapter: int, code: str, message: str,
               exc_cls: type[BaseException] = ValueError) -> None:
    """显式 BLOCK 点（调用方原本 raise 的位置）。strict 抛原异常类；v13 记录后继续。"""
    if not is_v13():
        raise exc_cls(message)
    record(project, chapter, code, message)
    note(code, message)


# ---------------------------------------------------------------------------
# 第二阶段：事实卡可见 + 作者裁决（2026-11-13）
#
# 用户质疑「道闸放开后还有作用吗」的正面回答：闸不替作者下结论，但必须让作者
# 看得见、并且能把自己的结论记下来。裁决不是「通过/不通过」的质量判定，而是
# 「作者已知悉这批事实并做了什么」的一等状态。
#
# 关键纪律：裁决绑定事实卡指纹。事实卡内容一变（新一轮写/改又产生了闸事实），
# 旧裁决立刻变 stale → 重新回到待裁决，杜绝「点过一次接受就永远绿灯」的假绿。
# ---------------------------------------------------------------------------

def card_fingerprint(card: dict[str, Any] | None) -> str:
    """事实卡指纹：只由 findings 的 (code/source/detail/at) 决定，与裁决、时间无关。"""
    findings = [f for f in (card or {}).get("findings", []) if isinstance(f, dict)]
    parts = [
        "\x1f".join(str(f.get(k) or "") for k in ("code", "source", "detail", "at"))
        for f in findings
    ]
    return hashlib.sha256("\x1e".join(parts).encode("utf-8")).hexdigest()[:16]


def would_block(card: dict[str, Any] | None) -> list[str]:
    """strict 口径下会 BLOCK 的闸码（去重保序）。

    事实卡里每一条 finding 都对应一次「v13 记录并继续、strict 会抛异常」的闸，
    因此这张清单就是硬闸口径下的阻断项，不重新跑判据、不臆造结论。

    source=voice_print 的条目是只读统计事实（方言/口癖词次数与位置），不对应任何
    闸判据，须排除，否则会把统计陈述误报成 strict 阻断项。
    """
    out: list[str] = []
    for f in (card or {}).get("findings", []):
        if isinstance(f, dict):
            if str(f.get("source") or "") == "voice_print":
                continue
            code = str(f.get("code") or "")
            if code and code not in out:
                out.append(code)
    return out


def adjudication_status(card: dict[str, Any] | None,
                        decision_row: dict[str, Any] | None) -> str:
    """裁决与当前事实卡是否仍匹配。无裁决=pending；事实变了=stale。"""
    if not isinstance(decision_row, dict):
        return "pending"
    if decision_row.get("card_fingerprint") != card_fingerprint(card):
        return "stale"
    return str(decision_row.get("action") or "pending")


def _decision_path(project: Path) -> Path:
    return Path(project) / "追踪" / REPORT_DIR / DECISION_FILE


def decisions(project: Path) -> dict[str, Any]:
    """只读裁决台账；缺失/损坏一律返回空，绝不抛异常（fail-open）。"""
    try:
        path = _decision_path(project)
        if not path.is_file():
            return {"version": 1, "decisions": {}}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("decisions"), dict):
            return {"version": 1, "decisions": {}}
        return data
    except Exception as exc:  # noqa: BLE001
        print(f"[GATE] decisions read failed: {exc}", file=sys.stderr)
        return {"version": 1, "decisions": {}}


def decision(project: Path, chapter: int) -> dict[str, Any] | None:
    """取某一章的当前裁决记录；无记录返回 None。"""
    row = decisions(project)["decisions"].get(str(int(chapter)))
    return row if isinstance(row, dict) else None


def decide(project: Path, chapter: int, action: str, *, note_text: str = "",
           by: str = "author", **extra: Any) -> dict[str, Any] | None:
    """落一条作者裁决（原子替换），失败只留一行诊断并返回 None，绝不阻断调用方。

    只允许 DECISION_EXTRA_KEYS 里的附加字段，避免把任意外部输入写进台账；
    裁决本体字段不可被 extra 覆盖（先构造 row，再挑白名单赋值亦不覆盖已有键）。
    """
    if action not in DECISION_ACTIONS:
        return None
    try:
        path = _decision_path(project)
        path.parent.mkdir(parents=True, exist_ok=True)
        root = decisions(project)
        table = dict(root.get("decisions") or {})
        card = report(project, chapter)
        row: dict[str, Any] = {
            "chapter": int(chapter),
            "action": action,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "by": str(by or "author")[:80],
            "note": str(note_text or "")[:500],
            "card_fingerprint": card_fingerprint(card),
            "card_count": int(card.get("count") or 0),
            "strict_would_block": would_block(card),
        }
        for key in DECISION_EXTRA_KEYS:
            if key in extra and key not in row:
                row[key] = extra[key]
        table[str(int(chapter))] = row
        payload = {"version": 1, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "decisions": table}
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return row
    except Exception as exc:  # noqa: BLE001
        print(f"[GATE] decision write failed: {exc}", file=sys.stderr)
        return None


_REPORT_NAME_RE = re.compile(r"^第0*(\d+)章\.json$")


def pending(project: Path, *, last_committed: int | None = None) -> dict[str, Any]:
    """只读：扫描事实卡目录，产出「闸事实 / 待裁决章」列表。

    - count=0 的空卡不进列表；「没有事实卡」明确回报 available=true + note 说明，
      绝不把「没查到」渲染成「全部通过」（守护中心既有铁律）。
    - status 来自 adjudication_status：pending / stale 计为待裁决，
      accept / recheck / rollback 保留展示（修复后变色不消失）。
    """
    try:
        folder = Path(project) / "追踪" / REPORT_DIR
        rows: list[dict[str, Any]] = []
        if folder.is_dir():
            for path in sorted(folder.glob("第*章.json")):
                m = _REPORT_NAME_RE.match(path.name)
                if not m:
                    continue
                ch = int(m.group(1))
                try:
                    card = json.loads(path.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001 — 单张卡损坏不影响其它章
                    continue
                if not isinstance(card, dict) or int(card.get("count") or 0) <= 0:
                    continue
                findings = [f for f in card.get("findings", []) if isinstance(f, dict)]
                dec = decision(project, ch)
                rows.append({
                    "chapter": ch,
                    "count": int(card.get("count") or 0),
                    "codes": would_block(card),
                    "generated_at": card.get("generated_at"),
                    "last_at": str(findings[-1].get("at") or "") if findings else None,
                    "status": adjudication_status(card, dec),
                    "decision": dec,
                    "committed": (last_committed is not None
                                  and ch <= int(last_committed)),
                })
        rows.sort(key=lambda r: r["chapter"])
        return {
            "available": True,
            "mode": gate_mode(),
            "chapters": rows,
            "pending_count": sum(1 for r in rows if r["status"] in ("pending", "stale")),
            "note": ("闸事实由 v13 道闸记录、不阻断；作者裁决按事实卡指纹绑定，"
                     "事实一变旧裁决自动失效。无事实卡仅表示本轮未产生闸事实，不等于检查通过。"),
        }
    except Exception as exc:  # noqa: BLE001
        print(f"[GATE] pending scan failed: {exc}", file=sys.stderr)
        return {"available": False, "mode": gate_mode(), "chapters": [],
                "pending_count": 0, "note": f"闸事实读取失败：{exc}"}
