"""runtime.py — 单 agent 执行器。

职责：把一个 agent 的方法论文档（.md 正文）转成 system prompt，组装上下文后
按模型路由调 LLM：
  - 模型中心已配置（model_config.json）→ agent_routes[name] → 全局默认池模型
  - 未配置 → 沿用原档位路由：local-write → 本地 27B；local-small → 本地 9B；
    cloud → 云端 DeepSeek（NWW_LLM_* 环境变量；未配置回退本地小模型）

LLM 只产文本，不落盘；落盘/脚本调度统一由 pipeline 控制（fail-closed）。
"""
from __future__ import annotations

import os
from typing import Optional

from ..llm_client import chat_completion, chat_completion_robust, chat_completion_stream
from ..llm_config import agent_endpoint as _cfg_agent_endpoint, agent_endpoint_chain as _cfg_agent_chain, public_view as _cfg_view
from .registry import AgentDef, CLOUD, LOCAL_SMALL, LOCAL_WRITE


class AgentCancelled(Exception):
    """任务取消信号：流式回调/环节检查中抛出，用于协作式终止后台任务。"""

# 本地模型直连（独立命名空间，复用 nwwmvp 的 NWW_MVP_* 配置；仅在模型中心未启用时使用）
_LOCAL_BASE = os.environ.get("NWW_MVP_LLM_BASE_URL", "http://127.0.0.1:8080/v1").rstrip("/")
_LOCAL_MODEL_WRITE = os.environ.get("NWW_MVP_LLM_MODEL", "Qwen3.8-27B-oQ4e-fp16-mtp")
_LOCAL_MODEL_SMALL = os.environ.get("NWW_MVP_LLM_MODEL_SMALL", "Qwen3.8-9B-mlx-6Bit")
_API_KEY = os.environ.get("NWW_MVP_LLM_API_KEY", "")
_TIMEOUT = float(os.environ.get("NWW_MVP_LLM_TIMEOUT", "600"))

# 云端 DeepSeek（复用 llm_client 默认配置；仅在模型中心未启用时使用）
_CLOUD_BASE = os.environ.get("NWW_LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
_CLOUD_MODEL = os.environ.get("NWW_LLM_MODEL", "deepseek-chat")
_CLOUD_KEY = os.environ.get("NWW_LLM_API_KEY", "")


def _route_endpoint(agent: AgentDef) -> tuple[str, str, str, float, int]:
    """(base_url, model, api_key, timeout, max_tokens)

    模型中心优先（agent_routes → default）；未启用回退原 env 档位路由。
    """
    ep = _cfg_agent_endpoint(agent.name)
    if ep:
        base_url, model, key = ep
        return base_url, model, key, _TIMEOUT, 8192
    routing = agent.routing
    if routing == LOCAL_WRITE:
        return _LOCAL_BASE, _LOCAL_MODEL_WRITE, _API_KEY, _TIMEOUT, 8192
    if routing == CLOUD:
        if _CLOUD_KEY:
            return _CLOUD_BASE, _CLOUD_MODEL, _CLOUD_KEY, _TIMEOUT, 8192
        # 云端未配置 → 回退本地小模型（不静默降级：调用方在日志 WARN）
        return _LOCAL_BASE, _LOCAL_MODEL_SMALL, _API_KEY, _TIMEOUT, 8192
    return _LOCAL_BASE, _LOCAL_MODEL_SMALL, _API_KEY, _TIMEOUT, 8192


def _run_agent_impl(
    agent: AgentDef,
    session: str,  # 环节说明（写正文 / 补语义 / 审查）
    user_prompt: str,
    *,
    temperature: float = 0.6,
    max_tokens: Optional[int] = None,
    timeout: Optional[float] = None,
) -> str:
    """执行一个 agent 会话。返回模型输出文本；失败抛 RuntimeError（由端点转 502）。

    模型中心已启用 → 沿端点链调用（主 + fallbacks 自动降级）；
    未启用 → 走原 env 档位单端点（行为与旧版完全一致）。
    timeout（2026-10-25 防卡死）：显式指定时覆盖路由默认（_TIMEOUT=600s）。辅助环节
    （prealign/红队/回补）传短超时用；注意 chrome 桥接端点在 llm_client 内有 900s
    下限（max(timeout, 900)），对它要靠 pipeline 的 _run_agent_bounded 硬上限兜底。
    """
    system = f"{session}\n\n【agent 方法论】\n{agent.method}"
    cfg_chain = _cfg_agent_chain(agent.name)
    if cfg_chain:
        return chat_completion_robust(
            system,
            user_prompt,
            cfg_chain,
            temperature=temperature,
            max_tokens=max_tokens or 8192,
            timeout=timeout if timeout is not None else _TIMEOUT,
        )
    base_url, model, key, default_timeout, mdefault = _route_endpoint(agent)
    out = chat_completion(
        system,
        user_prompt,
        temperature=temperature,
        max_tokens=max_tokens or mdefault,
        base_url=base_url,
        model=model,
        api_key=key,
        timeout=timeout if timeout is not None else default_timeout,
    )
    return out


def run_agent(
    agent: AgentDef,
    session: str,
    user_prompt: str,
    *,
    temperature: float = 0.6,
    max_tokens: Optional[int] = None,
    timeout: Optional[float] = None,
) -> str:
    """对外入口（2026-11-08 起带 AI 实况 agent 标注；真实实现见 _run_agent_impl）。"""
    from .. import live_feed
    with live_feed.agent_scope(agent.name, session):
        return _run_agent_impl(
            agent, session, user_prompt,
            temperature=temperature, max_tokens=max_tokens, timeout=timeout,
        )


def _run_agent_stream_impl(
    agent: AgentDef,
    session: str,  # 环节说明（写正文 / 补语义 / 审查）
    user_prompt: str,
    *,
    on_token=None,
    temperature: float = 0.6,
    max_tokens: Optional[int] = None,
) -> str:
    """流式执行一个 agent 会话：逐 token 回调 on_token(chunk)，返回完整文本。

    流式取 agent 路由主端点（agent_routes[name] → default → env 档位）。
    2026-10-25：主端点基础故障（5xx/429/超时/网络，即 llm_client 的 RuntimeError 族）
    → 降级为**非流式**沿端点链其余端点完成本环节（预览停更，但章节不因单端点挂掉而
    夭折；实测 webui-chrome-gemini 502 时主笔直接失败，重试也只会再撞同一端点）。
    链上没有备选（模型中心未启用）时保持原行为：异常上抛。
    """
    system = f"{session}\n\n【agent 方法论】\n{agent.method}"
    base_url, model, key, timeout, mdefault = _route_endpoint(agent)
    try:
        full: list[str] = []
        for chunk in chat_completion_stream(
            system,
            user_prompt,
            temperature=temperature,
            max_tokens=max_tokens or mdefault,
            base_url=base_url,
            model=model,
            api_key=key,
            timeout=timeout,
        ):
            full.append(chunk)
            if on_token is not None:
                try:
                    on_token(chunk)
                except AgentCancelled:
                    raise
                except Exception:  # noqa: BLE001 — 流式回调失败不影响主链
                    pass
        return "".join(full)
    except AgentCancelled:
        raise
    except (RuntimeError, OSError) as exc:
        cfg_chain = _cfg_agent_chain(agent.name)
        rest = [e for e in cfg_chain if tuple(e[:3]) != (base_url, model, key)]
        if not rest:
            raise
        print(
            f"[RUN_AGENT_STREAM] 主端点故障（{base_url}::{model}: {str(exc)[:120]}），"
            f"降级非流式链（{len(rest)} 个备选）继续",
            flush=True,
        )
        return chat_completion_robust(
            system,
            user_prompt,
            rest,
            temperature=temperature,
            max_tokens=max_tokens or mdefault,
            timeout=timeout,
        )


def run_agent_stream(
    agent: AgentDef,
    session: str,
    user_prompt: str,
    *,
    on_token=None,
    temperature: float = 0.6,
    max_tokens: Optional[int] = None,
) -> str:
    """对外入口（2026-11-08 起带 AI 实况 agent 标注；真实实现见 _run_agent_stream_impl）。"""
    from .. import live_feed
    with live_feed.agent_scope(agent.name, session):
        yield from _run_agent_stream_impl(
            agent, session, user_prompt,
            on_token=on_token, temperature=temperature, max_tokens=max_tokens,
        )


def describe_routing() -> dict:
    """返回当前路由配置（供诊断/前端展示）。

    模型中心已启用 → 返回配置视图 + 全局默认；未启用 → 返回原 env 档位。
    保留原有字段（cloud_configured/local_write/local_small/cloud_model）兼容旧前端。
    """
    info: dict = {
        "cloud_configured": bool(_CLOUD_KEY),
        "local_write": _LOCAL_MODEL_WRITE,
        "local_small": _LOCAL_MODEL_SMALL,
        "cloud_model": _CLOUD_MODEL if _CLOUD_KEY else "(未配置,回退本地小模型)",
        "config_configured": False,
        "default": "",
        "providers": [],
        "pool": [],
        "agent_routes": {},
    }
    view = _cfg_view()
    if view.get("configured"):
        info["config_configured"] = True
        info["default"] = view.get("default", "")
        info["providers"] = view.get("providers", [])
        info["pool"] = view.get("pool", [])
        info["agent_routes"] = view.get("agent_routes", {})
    return info