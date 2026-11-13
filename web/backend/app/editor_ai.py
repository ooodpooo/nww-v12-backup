"""编辑器选区 / 光标 AI 统一网关（V14 工具条能力对齐层）。

背景（2026-11-13 实机核实）：
  旧的 ``POST /api/editor/rewrite`` 用 ``llm_client.is_configured()`` 卡关，只认
  ``NWW_LLM_API_KEY``；而本机 launchd 把该变量设为空串，模型与 Key 实际配置在
  ``runtime/config/model_config.json``（default=agnes）。结果是编辑器里 6 个选段
  AI 按钮稳定返回「LLM 未配置」，章级 AI 却因走模型中心而正常。

本模块提供唯一一条生产通道：
  ``llm_config.endpoint_chain(...)`` → ``llm_client.chat_completion_robust(...)``
（自带端点链重试与 fallback、live_feed 采集），并向上提供两类真实 AI 能力：
  - POST /api/editor/ai/revision     选区改写：5 预设 + 自定义 + 校对，返回 2~3 个候选
  - POST /api/editor/ai/continuation 光标续写：6 个方向，返回 2~3 个只含新增正文的候选
  - GET  /api/editor/ai/health       只读自检：端点链是否可用、动作清单

设计约束（项目铁律）：
  1. 只增不改：旧 ``/api/editor/rewrite`` 原样保留，仅在显式 env 下转调本网关。
  2. env 回滚：``NWW_EDITOR_AI=0`` 时不挂载本路由；前端 404 自动回落旧端点。
  3. 禁静默降级：失败一律返回 ``ok=False`` + 明确 ``status``/``error``，不返回空 ``{}``。
  4. diff 由后端单点计算（LCS 分词），前端只做渲染与按勾选应用，避免两套 parser。
  5. 候选协议不依赖 provider 的 ``response_format``：请求 JSON，但按纯文本做容错解析，
     解析不出时把整段输出降级为单候选并显式标 ``degraded=True``。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from . import llm_client, llm_config, live_feed

router = APIRouter()

_MAX_SELECTED_CHARS = 6000
_MAX_CONTEXT_CHARS = 6000
_MAX_DIFF_TOKENS = 4000

# 选区动作：V14 的 5 个预设 + custom，另补 V12 需要的「校对」。
REVISION_OPERATIONS: dict[str, tuple[str, str]] = {
    "polish": ("优化表达", "润色语句使其更流畅自然、有文学感，保留原意和叙事节奏，不要堆砌辞藻，不要 AI 腔。"),
    "expand": ("扩写", "在保持原意的基础上补充动作细节、感官描写或心理活动，让画面更丰满，不要注水。"),
    "compress": ("精简", "压缩冗余表述，去掉重复的修饰和废话，让句子更紧凑有力，保留核心信息和情绪。"),
    "emotion": ("强化情绪", "强化情绪张力，放大人物的感受和内心波动，让读者更有代入感，但不要夸张。"),
    "conflict": ("强化冲突", "强化矛盾和对抗感，让冲突更尖锐、节奏更紧，读者更想往下看。"),
    "proofread": ("校对", "只修正错别字、标点误用、病句和明显的事实/称谓前后不一致，不改写文风、不增删情节。"),
    "custom": ("自定义改写", "严格按用户给出的要求改写，不做要求之外的发挥。"),
}

# 光标动作：对齐 V14 CURSOR_OPERATION_LABELS（6 项）。
CURSOR_OPERATIONS: dict[str, str] = {
    "continue": "继续写下一段",
    "transition": "生成过渡段",
    "dialogue": "增加对话",
    "description": "增加环境描写",
    "inner_thought": "增加心理描写",
    "conflict": "推进冲突",
}

_REVISION_SYSTEM = """你是中文网络小说正文编辑器里的局部改写助手。

必须返回严格 JSON（不要 Markdown 代码块、不要解释、不要 JSON 之外的任何文字）：
{"candidates":[{"label":"短标签","content":"改写后的正文","summary":"一句话说明","rationale":"改法理由"}]}

给出 2 到 3 个可直接比较的候选改写版本。每个 content 只含改写后的正文片段本身，
不得包含原文之外的前后文、标题、引号包裹或说明文字。

任务边界：
1. 只改写用户选中的片段，不要扩到前后文。
2. 改写必须贴合前后文的语气、人物状态和叙事节奏。
3. 不要输出 Markdown、分析过程或候选以外的额外文本。

硬性约束：
1. 不改剧情事实。
2. 不改变人称和叙事视角。
3. 不新增未授权设定。
4. 尽量保留原段核心信息与上下文承接。
5. 不要把文本改得明显像模板化 AI 文风。
6. 保持网文阅读节奏，句子不要太长。"""

_CONTINUATION_SYSTEM = """你是中文网络小说正文编辑器里的光标续写助手。

必须返回严格 JSON（不要 Markdown 代码块、不要解释、不要 JSON 之外的任何文字）：
{"candidates":[{"label":"短标签","content":"只含新增的正文","summary":"一句话说明","rationale":"写法理由"}]}

给出 2 到 3 个可直接插入光标处的候选片段。每个 content 只写光标之后要新增的正文，
不要重复光标前后的原文，不要加段首标题、解释或 Markdown。

任务边界：
1. 候选要能直接接在光标处，优先具体动作、对话和可感知的细节。
2. 每个候选控制在 1 到 3 个自然段。
3. 不要输出分析过程或候选以外的额外文本。

硬性约束：
1. 保留已知剧情事实、人物关系、人称和叙事视角。
2. 不新增未授权世界设定，不替用户做重大剧情跳跃。
3. 不重复最近一句已经完成的动作或信息。
4. 续写要推进当前场景或形成自然过渡，避免空泛总结和模板化 AI 文风。"""


class RevisionRequest(BaseModel):
    selected_text: str = Field("", description="选中文本（选区改写必填）")
    before_context: str = Field("", description="前文上下文")
    after_context: str = Field("", description="后文上下文")
    operation: str = Field("polish", description="polish/expand/compress/emotion/conflict/proofread/custom")
    custom_instruction: str = Field("", description="operation=custom 时的自定义指令")
    chapter_goal: str = Field("", description="可选的本章目标/风格摘要")


class ContinuationRequest(BaseModel):
    before_context: str = Field("", description="光标前的上下文")
    after_context: str = Field("", description="光标后的上下文")
    operation: str = Field("continue", description="continue/transition/dialogue/description/inner_thought/conflict")
    instruction: str = Field("", description="补充要求")
    chapter_goal: str = Field("", description="可选的本章目标/风格摘要")


class Candidate(BaseModel):
    id: str
    label: str
    content: str
    summary: str = ""
    rationale: str = ""
    diff_chunks: list[dict[str, Any]] = Field(default_factory=list)


def _strip_fence(text: str) -> str:
    """去掉模型常见的 ```json / ``` 包裹与首尾引号（不改变正文本体）。"""
    out = (text or "").strip()
    if out.startswith("```"):
        lines = [ln for ln in out.split("\n") if not ln.strip().startswith("```")]
        out = "\n".join(lines).strip()
    if len(out) >= 2 and out[0] == out[-1] and out[0] in ('"', "'", "“"):
        out = out[1:-1].strip()
    return out


def _try_json(text: str) -> Any:
    """按顺序尝试：整段 JSON → 截取首个 {...} 块 → 截取首个 [...] 块。"""
    for candidate in (text,):
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            pass
    obj = re.search(r"\{[\s\S]*\}", text)
    if obj:
        try:
            return json.loads(obj.group(0))
        except (json.JSONDecodeError, TypeError):
            pass
    arr = re.search(r"\[[\s\S]*\]", text)
    if arr:
        try:
            return json.loads(arr.group(0))
        except (json.JSONDecodeError, TypeError):
            pass
    return None


def _clean_content(text: str) -> str:
    out = _strip_fence(str(text or ""))
    # 模型有时会给每个候选套一层引号
    if len(out) >= 2 and out[0] == out[-1] and out[0] in ('"', "“"):
        out = out[1:-1].strip()
    return out.strip()


def parse_candidates(raw: str) -> tuple[list[dict[str, str]], bool]:
    """解析模型输出为 2~3 个候选；解析不出的纯文本降级为单候选并标 degraded。

    返回 ``(candidates, degraded)``；``degraded`` 为真表示「不是结构化多候选」，
    调用方必须在响应里如实标注，禁止把降级结果伪装成正常结果。
    """
    text = _strip_fence(raw)
    parsed = _try_json(text)
    items: Any = None
    if isinstance(parsed, dict):
        items = parsed.get("candidates") or parsed.get("results") or parsed.get("versions")
    elif isinstance(parsed, list):
        items = parsed

    if not isinstance(items, list) or not items:
        fallback = _clean_content(text)
        if not fallback:
            raise ValueError("模型返回为空，无法生成候选")
        return ([{
            "id": "candidate-1", "label": "候选 1", "content": fallback,
            "summary": "模型未按结构化格式返回，已按纯文本降级为单候选", "rationale": "",
        }], True)

    out: list[dict[str, str]] = []
    for index, item in enumerate(items[:3]):
        if isinstance(item, str):
            content, label, summary, rationale = item, f"候选 {index + 1}", "", ""
        elif isinstance(item, dict):
            content = str(item.get("content") or item.get("text") or item.get("body") or "")
            label = str(item.get("label") or f"候选 {index + 1}")
            summary = str(item.get("summary") or "")
            rationale = str(item.get("rationale") or "")
        else:
            continue
        content = _clean_content(content)
        if not content:
            continue
        out.append({
            "id": f"candidate-{len(out) + 1}",
            "label": label[:40],
            "content": content,
            "summary": summary[:300],
            "rationale": rationale[:300],
        })
    if not out:
        raise ValueError("模型输出中没有任何可用候选")
    return out, len(out) < 2


# ---- diff：LCS 分词，与 V14 chapterEditorDiff.ts 同一算法，后端单点实现 ----
_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]|[A-Za-z0-9_]+|\s+|[^\sA-Za-z0-9_\u4e00-\u9fff]")


def build_diff_chunks(original: str, rewritten: str) -> tuple[list[dict[str, str]], bool]:
    """生成 equal/insert/delete chunks；超长文本退化为「整段替换」并标 truncated。"""
    source = _TOKEN_RE.findall(original.replace("\r\n", "\n"))
    target = _TOKEN_RE.findall(rewritten.replace("\r\n", "\n"))
    if len(source) * len(target) > _MAX_DIFF_TOKENS * _MAX_DIFF_TOKENS:
        chunks: list[dict[str, str]] = []
        _push_chunk(chunks, "delete", original)
        _push_chunk(chunks, "insert", rewritten)
        return chunks, True

    n, m = len(source), len(target)
    lcs = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        row, next_row = lcs[i], lcs[i + 1]
        for j in range(m - 1, -1, -1):
            row[j] = next_row[j + 1] + 1 if source[i] == target[j] else max(next_row[j], row[j + 1])

    chunks = []
    i = j = 0
    while i < n and j < m:
        if source[i] == target[j]:
            _push_chunk(chunks, "equal", source[i]); i += 1; j += 1
        elif lcs[i + 1][j] >= lcs[i][j + 1]:
            _push_chunk(chunks, "delete", source[i]); i += 1
        else:
            _push_chunk(chunks, "insert", target[j]); j += 1
    while i < n:
        _push_chunk(chunks, "delete", source[i]); i += 1
    while j < m:
        _push_chunk(chunks, "insert", target[j]); j += 1
    return chunks, False


def _push_chunk(chunks: list[dict[str, str]], kind: str, text: str) -> None:
    if not text:
        return
    if chunks and chunks[-1]["type"] == kind:
        chunks[-1]["text"] += text
        return
    chunks.append({"id": f"chunk-{len(chunks) + 1}", "type": kind, "text": text})


def _endpoint_chain() -> list[tuple[str, str, str]]:
    """统一供电：模型中心端点链（含 fallback + 全局兜底）。

    默认走全局 default；需要给编辑器单独配模型时，设 NWW_EDITOR_AI_AGENT=<agent_routes 键>。
    不读 NWW_LLM_API_KEY —— 那正是旧链路失效的根因。
    """
    agent = os.environ.get("NWW_EDITOR_AI_AGENT", "").strip()
    if agent:
        return llm_config.endpoint_chain(agent)
    return llm_config.endpoint_chain(default=True)


def _call_llm(system_prompt: str, user_prompt: str, *, temperature: float, label: str) -> tuple[str, dict]:
    chain = _endpoint_chain()
    if not chain:
        raise RuntimeError("模型中心没有可用端点：请在「模型中心」启用并配置默认模型。")
    report: dict[str, str] = {}
    with live_feed.begin_run(f"编辑器 AI · {label}", mode="editor", kind="flow"):
        with live_feed.stage("editor_ai", label, label=label) as stage:
            raw = llm_client.chat_completion_robust(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                chain=chain,
                temperature=temperature,
                max_tokens=4096,
                retries=1,
                report=report,
            )
            stage.output(raw[:4000])
    return raw, report


def _ok(mode: str, operation: str, label: str, candidates: list[Candidate],
        report: dict, degraded: bool, **extra: Any) -> dict:
    return {
        "ok": True,
        "status": "degraded" if degraded else "ok",
        "mode": mode,
        "operation": operation,
        "operation_label": label,
        "candidates": [c.model_dump() for c in candidates],
        "model": report.get("model", ""),
        "base_url": report.get("base_url", ""),
        "degraded": degraded,
        **extra,
    }


def _fail(status: str, message: str, **extra: Any) -> dict:
    """失败一律带可判定的 status，禁止静默空对象。"""
    return {
        "ok": False,
        "status": status,
        "error": message,
        "candidates": [],
        "model": "",
        "base_url": "",
        "degraded": False,
        **extra,
    }


def _context_blocks(before: str, after: str) -> list[str]:
    parts: list[str] = []
    if before.strip():
        parts += ["【前文片段】", before.strip()[:_MAX_CONTEXT_CHARS], ""]
    if after.strip():
        parts += ["【后文片段】", after.strip()[:_MAX_CONTEXT_CHARS], ""]
    return parts


def run_revision(req: RevisionRequest) -> dict:
    """选区改写：2~3 个候选 + 逐字 diff（后端单点计算）。"""
    selected = (req.selected_text or "").strip()
    if not selected:
        return _fail("invalid_selection", "选中文本不能为空，请重新选择。")
    if len(selected) > _MAX_SELECTED_CHARS:
        return _fail("selection_too_long", f"选段过长（{len(selected)} 字），请选 {_MAX_SELECTED_CHARS} 字以内的片段。")

    op = (req.operation or "polish").strip()
    if op not in REVISION_OPERATIONS:
        op = "polish"
    label, direction = REVISION_OPERATIONS[op]
    if op == "custom":
        direction = (req.custom_instruction or "").strip() or "按用户要求优化这段表达"

    parts = [f"【改写类型】{label}", f"【改写方向】{direction}", ""]
    if (req.chapter_goal or "").strip():
        parts += ["【本章目标/风格】", req.chapter_goal.strip()[:1000], ""]
    parts += _context_blocks(req.before_context, req.after_context)
    parts += ["【待改写原文】", selected, "", "请只返回 JSON，candidates 里给出 2 到 3 个候选。"]
    user_prompt = "\n".join(parts)

    try:
        raw, report = _call_llm(_REVISION_SYSTEM, user_prompt, temperature=0.45, label=label)
        parsed, parse_degraded = parse_candidates(raw)
    except Exception as exc:  # noqa: BLE001 — 统一转成带 status 的失败，不伪装成功
        return _fail("llm_failed", f"改写失败：{exc}", operation=op, operation_label=label)

    candidates: list[Candidate] = []
    diff_truncated = False
    for item in parsed:
        chunks, truncated = build_diff_chunks(selected, item["content"])
        diff_truncated = diff_truncated or truncated
        candidates.append(Candidate(diff_chunks=chunks, **item))
    degraded = parse_degraded or len(candidates) < 2
    payload = _ok("revision", op, label, candidates, report, degraded,
                  original_text=selected, original_length=len(selected))
    if diff_truncated:
        payload["diff_truncated"] = True
    return payload


def run_continuation(req: ContinuationRequest) -> dict:
    """光标续写：2~3 个只含新增正文的候选（不重复上下文）。"""
    op = (req.operation or "continue").strip()
    if op not in CURSOR_OPERATIONS:
        op = "continue"
    label = CURSOR_OPERATIONS[op]
    before = (req.before_context or "").strip()
    after = (req.after_context or "").strip()
    if not before and not after:
        return _fail("invalid_cursor", "光标上下文为空，请在正文里定位光标后重试。")

    parts = [f"【本次续写方向】{label}",
             f"【用户补充要求】{(req.instruction or '').strip() or '无'}", ""]
    if (req.chapter_goal or "").strip():
        parts += ["【本章目标/风格】", req.chapter_goal.strip()[:1000], ""]
    parts += ["【光标前的上下文】", before[:_MAX_CONTEXT_CHARS] or "无（光标位于正文开头）", ""]
    parts += ["【光标后的上下文】", after[:_MAX_CONTEXT_CHARS] or "无（光标位于正文结尾）", ""]
    parts += ["请只返回 JSON，每个候选的 content 只写要新增的正文。"]
    user_prompt = "\n".join(parts)

    try:
        raw, report = _call_llm(_CONTINUATION_SYSTEM, user_prompt, temperature=0.55, label=label)
        parsed, parse_degraded = parse_candidates(raw)
    except Exception as exc:  # noqa: BLE001
        return _fail("llm_failed", f"续写失败：{exc}", operation=op, operation_label=label)

    candidates: list[Candidate] = []
    for item in parsed:
        # 续写是纯插入：整段为 insert，无 delete
        chunks, truncated = build_diff_chunks("", item["content"])
        candidates.append(Candidate(diff_chunks=chunks, **item))
        if truncated:
            continue
    degraded = parse_degraded or len(candidates) < 2
    return _ok("continuation", op, label, candidates, report, degraded)


@router.post("/editor/ai/revision")
def api_editor_ai_revision(req: RevisionRequest) -> dict:
    return run_revision(req)


@router.post("/editor/ai/continuation")
def api_editor_ai_continuation(req: ContinuationRequest) -> dict:
    return run_continuation(req)


@router.get("/editor/ai/health")
def api_editor_ai_health() -> dict:
    """只读自检：不回显 API Key，供前端判断是否需要回落旧端点。"""
    try:
        chain = _endpoint_chain()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": "config_error", "error": str(exc), "chain_size": 0}
    return {
        "ok": True,
        "status": "ok" if chain else "no_endpoint",
        "chain_size": len(chain),
        "revision_operations": sorted(REVISION_OPERATIONS),
        "cursor_operations": sorted(CURSOR_OPERATIONS),
        "error": "" if chain else "模型中心没有可用端点，请在模型中心配置默认模型。",
    }
