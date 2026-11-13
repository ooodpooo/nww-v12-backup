"""编辑器选段 AI 改写端点。

POST /api/editor/rewrite
入参：
  - selected_text: 选中文本（必填）
  - before_context: 前文上下文（最多3段，可选）
  - after_context: 后文上下文（最多3段，可选）
  - operation: polish | expand | compress | emotion | conflict | custom
  - custom_instruction: operation=custom 时的用户自定义指令

出参：
  - candidate: 改写后的候选文本
  - explanation: 一句话改法说明
"""
from __future__ import annotations

import os

from fastapi import APIRouter
from pydantic import BaseModel, Field

from . import llm_client
from . import live_feed

router = APIRouter()


class RewriteRequest(BaseModel):
    selected_text: str = Field(..., min_length=1, description="选中文本")
    before_context: str = Field("", description="前文上下文（最多3段）")
    after_context: str = Field("", description="后文上下文（最多3段）")
    operation: str = Field("polish", description="改写类型: polish/expand/compress/emotion/conflict/custom")
    custom_instruction: str = Field("", description="自定义改写指令")


_OPERATION_PROMPTS: dict[str, tuple[str, str]] = {
    "polish": (
        "优化表达",
        "润色语句使其更流畅自然、有文学感，但保留原意和叙事节奏，不要过度堆砌辞藻，不要出现AI腔。",
    ),
    "expand": (
        "扩写",
        "在保持原意的基础上扩写，补充动作细节、感官描写或心理活动，让画面更丰满，但不要注水。",
    ),
    "compress": (
        "精简",
        "压缩冗余表述，去掉重复的修饰和废话，让句子更紧凑有力，保留核心信息和情绪。",
    ),
    "emotion": (
        "强化情绪",
        "强化这段文字的情绪张力，放大人物的感受和内心波动，让读者更有代入感，但不要太夸张。",
    ),
    "conflict": (
        "强化冲突",
        "强化这段文字里的矛盾和对抗感，让冲突更尖锐、节奏更紧，读者更想往下看。",
    ),
}


SYSTEM_TEMPLATE = """你是中文网络小说编辑器里的局部改写助手。
你的职责是围绕用户选中的正文片段，给出一个高质量的改写版本。

任务边界：
1. 只改写用户选中的片段，不要扩到前后文。
2. 改写必须贴合前后文的语气、人物状态和叙事节奏。
3. 不要解释过程，不要输出 Markdown，不要输出候选以外的额外文本。
4. 直接输出改写后的正文文本。

硬性约束：
1. 不改剧情事实。
2. 不改变人称和叙事视角。
3. 不新增未授权设定。
4. 尽量保留原段核心信息与上下文承接。
5. 不要把文本改得明显像模板化 AI 文风。
6. 保持网文阅读节奏，句子不要太长。"""


@router.post("/editor/rewrite")
def editor_rewrite(req: RewriteRequest) -> dict:
    """选段 AI 改写：返回一个候选版本。"""
    selected = req.selected_text.strip()
    if not selected:
        return {"error": "选中文本不能为空"}

    # 2026-11-13 供电修复：旧链路只读 NWW_LLM_API_KEY（本机为空串），与模型中心脱节，
    # 导致编辑器 6 个按钮恒返回「LLM 未配置」。默认转调统一网关；
    # NWW_EDITOR_REWRITE_GATEWAY=0 可完整回滚到下面的原实现。
    if os.environ.get("NWW_EDITOR_REWRITE_GATEWAY", "1") != "0":
        try:
            from .editor_ai import RevisionRequest, run_revision
            result = run_revision(RevisionRequest(
                selected_text=selected,
                before_context=req.before_context,
                after_context=req.after_context,
                operation=req.operation,
                custom_instruction=req.custom_instruction,
            ))
            if not result.get("ok"):
                return {"error": result.get("error") or "改写失败（未知原因）"}
            candidates = result.get("candidates") or []
            if not candidates:
                return {"error": "改写失败：模型未返回候选"}
            first = candidates[0]
            return {
                "candidate": first.get("content", ""),
                "candidates": candidates,          # 新增字段，旧前端读 candidate 不变
                "operation": req.operation or "polish",
                "operation_label": result.get("operation_label", "改写"),
                "original_length": len(selected),
                "candidate_length": len(first.get("content", "")),
                "model": result.get("model", ""),
                "degraded": result.get("degraded", False),
            }
        except Exception as exc:  # noqa: BLE001 — 网关异常不吞，带原因返回
            return {"error": f"改写失败：{exc}"}

    if not llm_client.is_configured():
        return {"error": "LLM 未配置，请先在模型中心设置 API Key"}

    # 解析操作类型
    op = req.operation or "polish"
    if op == "custom":
        op_label = "自定义改写"
        op_direction = req.custom_instruction.strip() or "按用户要求改写"
    else:
        op_label, op_direction = _OPERATION_PROMPTS.get(op, _OPERATION_PROMPTS["polish"])

    # 组装 user prompt
    parts: list[str] = []
    parts.append(f"【改写类型】{op_label}")
    parts.append(f"【改写方向】{op_direction}")
    parts.append("")

    if req.before_context.strip():
        parts.append("【前文片段】")
        parts.append(req.before_context.strip())
        parts.append("")

    parts.append("【待改写原文】")
    parts.append(selected)
    parts.append("")

    if req.after_context.strip():
        parts.append("【后文片段】")
        parts.append(req.after_context.strip())
        parts.append("")

    parts.append("请直接输出改写后的正文，不要加任何解释。")
    user_prompt = "\n".join(parts)

    try:
        with live_feed.begin_run(
            f"编辑器改写 · {op_label}",
            mode="editor",
            kind="flow",
        ):
            with live_feed.stage("editor_rewrite", f"{op_label}（{len(selected)} 字选段）", label=op_label) as _st:
                result = llm_client.chat_completion(
                    system_prompt=SYSTEM_TEMPLATE,
                    user_prompt=user_prompt,
                    temperature=0.7,
                    max_tokens=2048,
                )
                _st.output(result[:4000])
        # 清理输出：去掉可能的 markdown 代码块包裹
        cleaned = result.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            cleaned = "\n".join(lines).strip()
        # 去掉首尾引号
        if cleaned.startswith('"') and cleaned.endswith('"'):
            cleaned = cleaned[1:-1].strip()

        return {
            "candidate": cleaned,
            "operation": op,
            "operation_label": op_label,
            "original_length": len(selected),
            "candidate_length": len(cleaned),
        }
    except Exception as e:
        return {"error": f"改写失败：{e}"}
