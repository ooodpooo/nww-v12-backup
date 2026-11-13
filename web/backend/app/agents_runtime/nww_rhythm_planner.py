"""nww_rhythm_planner.py — 单章节奏计划（NRL Layer 2）+ A 通道接线入口。

## 定位
把「发生什么」（细纲 / Writing Packet）转换为「读者如何经历」（节奏计划），
作为 Writer 的额外输入。**只增不改**：off 模式下对 packet 零改动。

## 分层（外脑 Q1 裁决：条件批准 B）
- **确定性层（本模块）**：跨章状态读取、事件与顺序锁定、相邻章禁则、输入指纹、
  Schema 与语义校验。**不得**用「位置 → 张力」的机械映射（rhythm/SKILL.md R1
  明令禁止固定段落形状——D2 就是这么被做成模板的）。
- **语义层（LLM，可选）**：curve / tension / reveal / breath / climax / ending_type。
  由 `NWW_NRL_SEMANTIC=llm` 显式开启；关闭时不产出计划（status=error），
  由 mode 决定是降级（off/shadow）还是阻断（enforce）。

## 模式（NWW_NRL_MODE，默认 off）
- off     ：完全走原路径，packet 逐字节不变。
- shadow  ：生成并校验计划 + 执行收据，**不注入 packet**、不影响正文。
- enforce ：计划通过全部校验后才注入 packet["rhythm_plan"]；否则阻断。

## 失败语义
`attach()` **永不抛出**（内部全捕获）。enforce 下任何失败 → status=error +
error_code，由调用方 raise 成 BLOCK，不静默退回旧路径。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from . import nww_rhythm_state as _state

PLAN_SCHEMA = "nrl-plan/1"
_TENSIONS = ("快", "紧", "松", "爆", "余韵")
_ENDINGS = ("A", "B", "C", "D")


def mode() -> str:
    """当前模式（off|shadow|enforce），默认 off。非法值一律按 off 处理（安全侧）。"""
    m = (os.environ.get("NWW_NRL_MODE", "off") or "off").strip().lower()
    return m if m in ("off", "shadow", "enforce") else "off"


def semantic_mode() -> str:
    m = (os.environ.get("NWW_NRL_SEMANTIC", "off") or "off").strip().lower()
    return m if m in ("off", "llm") else "off"


def max_tries() -> int:
    """Q-C（外脑 round-3）：**有限**重试预算，默认 2（首次 + 1 次重试）。

    shadow/enforce 用**同一个**预算：
      · shadow：预算耗尽 → 记录错误，**不注入**任何计划，不阻断，走原生产路径。
      · enforce：预算耗尽 → 返回 status=error，由 pipeline 阻断当前生成，
        **不得降级到「无计划」路径**（外脑明确禁止静默降级）。
    非法值（非数字 / <=0）取 1 —— 安全侧偏严：宁可少试，不可因配置笔误变成无限重试。
    """
    raw = os.environ.get("NWW_NRL_MAX_TRIES")
    if raw is None:
        return 2
    try:
        n = int((raw or "").strip())
    except (TypeError, ValueError):
        return 1
    return n if n >= 1 else 1


def agent_name() -> str:
    return (os.environ.get("NWW_NRL_AGENT", "nww-architect") or "nww-architect").strip()


# ——— 语义层接缝：测试可 monkeypatch 成本地桩，生产走真实 agent 路由 ———
def _llm_complete(system: str, user: str) -> str:
    from . import registry, runtime  # 延迟导入：off 模式下不触碰注册表
    # 防卡死（2026-10-25）：enforce 模式下语义层在写章主链，挂死即拖死全链
    # （chrome 桥 900s 超时下限）。有界执行：超时返回空 → planner 按「计划无效」
    # 既有失败分支处理（enforce BLOCK 报错可重试，off/shadow 降级）。
    from .pipeline import _run_agent_bounded, _ADV_TIMEOUT_S
    return _run_agent_bounded(registry.get_agent(agent_name()), system, user,
                              timeout=max(_ADV_TIMEOUT_S, 420.0),
                              temperature=0.4, max_tokens=4096)


def canonical_json(obj) -> str:
    return _state.canonical_json(obj)


def _locked_events(packet: dict) -> list[dict]:
    """从 packet.scene_chain 锁定事件与顺序（确定性，不解释剧情）。"""
    out = []
    for sc in (packet.get("scene_chain") or []):
        if not isinstance(sc, dict):
            continue
        sid = sc.get("scene_id") or sc.get("act") or ""
        raw = sc.get("beats_raw") or {}
        goal = ((sc.get("driver_goal") or {}).get("goal") or "").strip()
        event = (raw.get("情节点") or goal or "").strip()
        func = (raw.get("功能") or (sc.get("craft_notes") or [""])[0] or "").strip()
        out.append({"scene_id": str(sid), "event": event, "function": func})
    return [e for e in out if e["scene_id"] or e["event"]]


def _source_hashes(book: Path, packet: dict, outline: Path | None, chapter_id: int) -> tuple[dict, list[str]]:
    """输入指纹。**必须在注入前计算**（否则 packet 自指）。"""
    errs: list[str] = []
    h: dict = {
        "packet": hashlib.md5(canonical_json(packet).encode("utf-8")).hexdigest(),
        "outline": None,
        "prior_state": None,
    }
    if outline is not None and Path(outline).is_file():
        try:
            h["outline"] = hashlib.md5(Path(outline).read_bytes()).hexdigest()
        except OSError as e:
            errs.append(f"OUTLINE_UNREADABLE: {e}")
    else:
        errs.append("OUTLINE_MISSING")
    fp, ferrs = _state.fingerprint(book, chapter_id)
    h["prior_state"] = fp
    errs += ferrs
    return h, errs


def _build_skeleton(book: Path, packet: dict, chapter_id: int, outline: Path | None) -> dict:
    """确定性骨架（不含语义字段）。"""
    locked = _locked_events(packet)
    cons = _state.next_constraints(book, chapter_id)
    hashes, herrs = _source_hashes(book, packet, outline, chapter_id)
    return {
        "schema_version": PLAN_SCHEMA,
        "chapter_id": int(chapter_id),
        "source_hashes": hashes,
        "beats": [{"beat_id": e["scene_id"], "event_ref": e["event"],
                   "function": e["function"]} for e in locked],
        "constraints": {
            "forbidden_drives": cons.get("forbidden_drives") or [],
            "forbidden_endings": cons.get("forbidden_endings") or [],
            "locked_events": [e["event"] for e in locked],
            "locked_order": True,
        },
        "_basis": cons.get("basis") or {},
        "_evidence_quality": cons.get("evidence_quality") or {},
        "_state_errors": (cons.get("errors") or []) + herrs,
        "_target_is_committed": bool(cons.get("target_is_committed")),
    }


def _semantic_prompt(skel: dict) -> tuple[str, str]:
    system = (
        "你是长篇网文的叙事节奏设计师。给你一章**已锁定的事件序列**（事件与顺序不得增删改），"
        "你要设计的是「读者如何经历」：全章张力走向、每个事件的张力状态、信息释放时机、"
        "呼吸窗口、真正的峰值落点、章尾动力类型。\n"
        "硬约束：\n"
        "1. 只输出一个 JSON 对象，不要任何解释文字、不要代码块围栏。\n"
        "2. beats 必须与给定事件**一一对应**，beat_id 原样沿用，不得增删、不得改顺序。\n"
        "3. tension 只能取：快 / 紧 / 松 / 爆 / 余韵。不得全部写成同一强度。\n"
        "4. 连续 3 个及以上「紧/爆」之后必须安排至少一处 breath=true 的恢复窗口。\n"
        "5. climax 必须落在**真正改变状态**的那个事件上，不是中间过程、不是最后一个。\n"
        "6. 禁止套用固定形状（如「铺垫→转折→高潮→回落」机械四段）；形状必须由本章事件本身决定。\n"
        "7. ending_type 只能取 A 代价结算 / B 认知变化 / C 外部压力 / D 关系变化，"
        "且不得取 forbidden_endings 中的值（那是为了与邻近章节形成差异）。\n"
        "8. reveal 写清：揭示什么 + 承载物（动作/物件/对白）+ 读者/角色/主角各自知道什么。"
        "先现象后原因、先遮挡后揭示；禁止「知道答案 → 直接解释」。"
    )
    payload = {
        "chapter_id": skel["chapter_id"],
        "locked_beats": skel["beats"],
        "forbidden_endings": skel["constraints"]["forbidden_endings"],
        "forbidden_drives": skel["constraints"]["forbidden_drives"],
        "output_schema": {
            "curve": "全章张力走向一句话",
            "ending_type": "A|B|C|D",
            "climax": {"beat_id": "<某个 beat_id>", "type": "状态改变——什么变了"},
            "beats": [{"beat_id": "<沿用>", "tension": "快|紧|松|爆|余韵",
                       "reveal": "…", "breath": False}],
        },
    }
    return system, json.dumps(payload, ensure_ascii=False, indent=2)


def _extract_json(text: str) -> dict | None:
    """从 LLM 输出中取出 JSON 对象（容忍围栏与前后噪音）。失败返回 None。"""
    if not text:
        return None
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    start = s.find("{")
    if start < 0:
        return None
    depth, end = 0, -1
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        return None
    try:
        return json.loads(s[start:end])
    except json.JSONDecodeError:
        return None


def _validate(skel: dict, sem: dict) -> tuple[dict | None, list[str]]:
    """Schema + 语义校验。返回 (plan, errors)。errors 非空 → 计划无效。"""
    errs: list[str] = list(skel.get("_state_errors") or [])
    beats = sem.get("beats")
    if not isinstance(beats, list) or not beats:
        return None, errs + ["BEATS_EMPTY"]
    skel_ids = [b["beat_id"] for b in skel["beats"]]
    sem_ids = [str(b.get("beat_id", "")) for b in beats]
    if sem_ids != skel_ids:
        return None, errs + [f"BEATS_MISMATCH: sem={sem_ids} locked={skel_ids}"]

    norm_beats = []
    high_run = 0
    max_run = 0
    has_breath = False
    for b, sb in zip(beats, skel["beats"]):
        t = str(b.get("tension", "")).strip()
        if t not in _TENSIONS:
            return None, errs + [f"TENSION_INVALID: beat_id={sb['beat_id']} tension={t!r}"]
        reveal = str(b.get("reveal", "")).strip()
        if not reveal:
            return None, errs + [f"REVEAL_EMPTY: beat_id={sb['beat_id']}"]
        breath = bool(b.get("breath"))
        if breath:
            has_breath = True
        if t in ("紧", "爆"):
            high_run += 1
            max_run = max(max_run, high_run)
        else:
            high_run = 0
        norm_beats.append({"beat_id": sb["beat_id"], "event_ref": sb["event_ref"],
                           "function": sb["function"], "tension": t,
                           "reveal": reveal, "breath": breath})
    # 外脑：breath 要做场景关联校验 —— 连续 ≥3 个高压 beat 必须有恢复窗口
    if max_run >= 3 and not has_breath:
        return None, errs + [f"NO_BREATH_WINDOW: 连续高压 {max_run} 段无恢复窗口"]

    ending = str(sem.get("ending_type", "")).strip().upper()
    if ending not in _ENDINGS:
        return None, errs + [f"ENDING_TYPE_INVALID: {ending!r}"]
    forbidden = skel["constraints"]["forbidden_endings"] or []
    if ending in forbidden:
        return None, errs + [f"ENDING_FORBIDDEN: {ending}（与邻近章节同型）"]

    # 外脑：climax.beat_id 要做场景存在性 + 状态变化证据校验，不能只验 JSON 结构
    cl = sem.get("climax") or {}
    cl_id = str(cl.get("beat_id", "")).strip()
    if cl_id not in skel_ids:
        return None, errs + [f"CLIMAX_BEAT_UNKNOWN: {cl_id!r} 不在锁定事件中"]
    cl_type = str(cl.get("type", "")).strip()
    if len(cl_type) < 4:
        return None, errs + ["CLIMAX_TYPE_WEAK: 未说明改变的是什么状态（需 ≥4 字）"]
    if cl_id == skel_ids[-1] and len(skel_ids) > 1:
        # 峰值落在最后一个事件：多数情况是"收束"而非"状态改变"，需显式说明
        if "收" in cl_type or "余韵" in cl_type:
            return None, errs + ["CLIMAX_AT_TAIL: 峰值落在末位且自述为收束/余韵"]

    curve = str(sem.get("curve", "")).strip()
    if len(curve) < 8:
        return None, errs + ["CURVE_TOO_SHORT"]

    plan = {
        "schema_version": PLAN_SCHEMA,
        "chapter_id": skel["chapter_id"],
        "source_hashes": skel["source_hashes"],
        "curve": curve,
        "ending_type": ending,
        "climax": {"beat_id": cl_id, "type": cl_type},
        "beats": norm_beats,
        "constraints": {
            "forbidden_drives": skel["constraints"]["forbidden_drives"],
            "forbidden_endings": forbidden,
            "locked_events": skel["constraints"]["locked_events"],
            "locked_order": True,
        },
        # 外脑 Q4-④：locked_order 只保证「事件集合与顺序不被节奏层改写」，
        # 不保证正文未漏写/未改写结果/未违因果——那是既有细纲契约的职责。
        "verification_scope": {
            "guarantees": ["beats 与 packet.scene_chain 一一对应", "节奏层不增删改事件及顺序"],
            "does_not_guarantee": ["正文未漏写事件", "结果未被改写", "因果关系未被违反"],
        },
        "_basis": skel.get("_basis") or {},
        "_evidence_quality": skel.get("_evidence_quality") or {},
        "_target_is_committed": skel.get("_target_is_committed"),
    }
    return plan, errs


def attach(book: Path, packet: dict, chapter_id: int, outline=None) -> dict:
    """唯一对外入口。**永不抛出**。

    返回 {"mode", "status": ok|error|disabled, "error_code", "plan", "shadow_plan",
          "receipt", "errors"}
    off/shadow 不修改 packet；enforce 且 status=ok 时写入 packet["rhythm_plan"]。

    收据（receipt）除 mode/status 外**必须**带 error_code 与 errors 摘要——
    外脑 R5：每次运行都要能按执行上下文追溯「失败原因」，不能只留一个 status。
    """
    res = _attach_inner(book, packet, chapter_id, outline)
    res["receipt"]["error_code"] = res.get("error_code")
    res["receipt"]["errors"] = (res.get("errors") or [])[:5]
    return res


def _attach_inner(book: Path, packet: dict, chapter_id: int, outline=None) -> dict:
    m = mode()
    receipt = {"mode": m, "schema": PLAN_SCHEMA, "chapter_id": int(chapter_id)}
    if m == "off":
        receipt.update(status="disabled", injected=False, reason="NWW_NRL_MODE=off")
        return {"mode": m, "status": "disabled", "error_code": None,
                "plan": None, "shadow_plan": None, "receipt": receipt, "errors": []}

    try:
        if not isinstance(packet, dict):
            raise ValueError("PACKET_NOT_DICT")
        skel = _build_skeleton(book, packet, chapter_id, outline)
        receipt["source_hashes"] = skel["source_hashes"]
        receipt["evidence_quality"] = skel.get("_evidence_quality") or {}
        receipt["target_is_committed"] = skel.get("_target_is_committed")
        if not skel["beats"]:
            return {"mode": m, "status": "error", "error_code": "NO_LOCKED_EVENTS",
                    "plan": None, "shadow_plan": None, "receipt": receipt,
                    "errors": ["packet.scene_chain 为空，无法锁定事件序列"]}

        if semantic_mode() != "llm":
            receipt.update(status="error", injected=False)
            return {"mode": m, "status": "error", "error_code": "SEMANTIC_DISABLED",
                    "plan": None, "shadow_plan": None, "receipt": receipt,
                    "errors": ["NWW_NRL_SEMANTIC!=llm：语义层未开启，不产出计划（不猜、不机械赋值）"]}

        # ---- Q-C 有限重试预算（只重试「LLM 依赖」环节）----
        # _build_skeleton 的失败是确定性状态错误，重试必然同结果，故不在预算内。
        sys_p, usr_p = _semantic_prompt(skel)
        budget = max_tries()
        attempt_log: list[dict] = []
        last_code = "SEMANTIC_BAD_JSON"
        last_errs: list[str] = ["未尝试：预算 < 1"]
        # ★ 成功必须显式标记：_validate 可能返回 (非 None 的 plan, 非空 errs)
        #   （例如跨章状态错误被并进 errs），只判 plan is None 会把失败当成功。
        ok = False
        plan = None
        for _att in range(1, budget + 1):
            plan = None
            raw_out = ""
            try:
                raw_out = _llm_complete(sys_p, usr_p)
                sem = _extract_json(raw_out)
            except Exception as e:  # noqa: BLE001 — 单次尝试失败不得中断整个预算
                attempt_log.append({"attempt": _att, "error_code": "LLM_CALL_FAILED",
                                    "errors": [f"{type(e).__name__}: {e}"]})
                last_code, last_errs = "LLM_CALL_FAILED", attempt_log[-1]["errors"]
                continue
            if sem is None:
                attempt_log.append({"attempt": _att, "error_code": "SEMANTIC_BAD_JSON",
                                    "errors": ["语义层输出无法解析为 JSON 对象"],
                                    "raw_tail": str(raw_out)[-200:]})
                last_code, last_errs = "SEMANTIC_BAD_JSON", attempt_log[-1]["errors"]
                continue
            _p, errs = _validate(skel, sem)
            if _p is None or errs:
                attempt_log.append({"attempt": _att, "error_code": "PLAN_INVALID", "errors": errs})
                last_code, last_errs = "PLAN_INVALID", errs
                plan = None
                continue
            attempt_log.append({"attempt": _att, "error_code": None, "errors": []})
            plan, ok = _p, True
            break

        receipt["attempts"] = len(attempt_log)
        receipt["attempt_log"] = attempt_log
        if not ok or plan is None:
            # ★ 预算耗尽：无论 shadow/enforce 都**不注入**计划；是否阻断由 mode 决定。
            receipt.update(status="error", injected=False)
            return {"mode": m, "status": "error", "error_code": last_code,
                    "plan": None, "shadow_plan": None, "receipt": receipt,
                    "errors": last_errs}

        if m == "shadow":
            # ★ shadow 绝不注入 packet
            receipt.update(status="ok", injected=False)
            return {"mode": m, "status": "ok", "error_code": None,
                    "plan": None, "shadow_plan": plan, "receipt": receipt, "errors": []}

        packet["rhythm_plan"] = plan          # enforce：注入（Writer 输入 = json.dumps(packet)）
        receipt.update(status="ok", injected=True)
        return {"mode": m, "status": "ok", "error_code": None,
                "plan": plan, "shadow_plan": None, "receipt": receipt, "errors": []}
    except Exception as e:  # noqa: BLE001 — attach 契约：永不抛出
        receipt.update(status="error", injected=False)
        return {"mode": m, "status": "error", "error_code": "PLANNER_EXCEPTION",
                "plan": None, "shadow_plan": None, "receipt": receipt,
                "errors": [f"{type(e).__name__}: {e}"]}
