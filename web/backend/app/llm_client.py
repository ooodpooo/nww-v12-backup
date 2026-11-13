"""云端 LLM 客户端：OpenAI 兼容接口，支持 DeepSeek/Qwen/OpenAI 等。

环境变量配置：
  NWW_LLM_API_KEY   — API Key（必填，否则云端操作不可用）
  NWW_LLM_BASE_URL  — 默认 https://api.deepseek.com
  NWW_LLM_MODEL     — 默认 deepseek-chat

设计原则：
  - 方法论只在云端内部拼接进 prompt，绝不返回给客户端
  - 调用失败时返回明确错误，不静默降级
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional
from urllib.parse import urlparse

import httpx

from . import opencode_bridge
from . import chrome_bridge

# Chrome 网页版 VIP 桥接开关（2026-09-23 新增）：默认开启；
# 仅当端点 base_url 为 chrome:// 时才激活（双重隔离：不选该池模型 = 零影响）
_CHROME_BRIDGE_ENABLED = os.environ.get("NWW_CHROME_BRIDGE_ENABLED", "1") == "1"

_DEFAULT_BASE_URL = "https://api.deepseek.com"
_DEFAULT_MODEL = "deepseek-chat"
_TIMEOUT_S = float(os.environ.get("NWW_LLM_TIMEOUT_S", "120") or "120")

# Chrome 网页版桥接的生成预算下限（秒）。
# 为什么不能沿用 _TIMEOUT_S(120)：网页版是「人打字 + 等网页渲染」，且当前模型带思考
# —— 实测光推理就跑了 298s，120s 会在答案出来前就撞顶报错。写 2000-2500 字章节
# 属最重负载，预算必须给足。这只影响 chrome:// 端点，HTTP 端点行为不变。
# 需要调整用环境变量 NWW_CHROME_BRIDGE_TIMEOUT_S 覆盖。
_CHROME_BRIDGE_TIMEOUT_S = float(
    os.environ.get("NWW_CHROME_BRIDGE_TIMEOUT_S", "900") or "900"
)

# WebUI 桥接（chrome_llm_bridge 的 OpenAI 兼容代理，HTTP 端点）生成预算下限。
# 与 chrome:// 同理且同源：网页版=人打字+等渲染，实测首字 26~235s（出口抖动时），
# local 模式双轮可到 400s+。默认 120s 的 HTTP 预算会把健康的桥接调用拦腰砍断，
# 且桥接断连即让页面停止生成（半成品都拿不到）。
# 命中条件：模型名 webui-chrome-* 前缀，或 base_url 为 127.0.0.1:8788/8789。
# 需调整用环境变量 NWW_BRIDGE_HTTP_TIMEOUT_S 覆盖。
_BRIDGE_HTTP_TIMEOUT_S = float(
    os.environ.get("NWW_BRIDGE_HTTP_TIMEOUT_S", "600") or "600"
)
_BRIDGE_HTTP_PORTS = frozenset({"8788", "8789"})


def _is_bridge_http_endpoint(base_url: str, model: str) -> bool:
    """OpenAI-HTTP 桥接端点判定（区分于 chrome:// 的 in-app CDP 栈）。"""
    if (model or "").startswith("webui-chrome"):
        return True
    parsed = urlparse(base_url or "")
    host = (parsed.hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1") and str(parsed.port or "") in _BRIDGE_HTTP_PORTS


# 可重试/可降级状态码（限流、瞬时 5xx）；401/403 等配置类错误不重试
_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


def _is_local_host(url: str) -> bool:
    """本地回环地址（127.*/localhost/::1）判定：直连不走环境代理，避免被 http_proxy 劫持。

    远程地址保持默认行为（trust_env=True，继续走环境代理访问外网）。
    """
    host = (urlparse(url).hostname or "").lower()
    return host in ("localhost", "::1") or host.startswith("127.")


# 非流式调用的网络重试退避（2026-11-13）：端点/代理瞬断时立即重试几乎必然再失败
# （编辑器改写实测两条「网络错误」连发）。与流式重试的口径保持一致，默认 3s。
_NET_RETRY_DELAY_S = float(os.environ.get("NWW_LLM_NET_RETRY_DELAY_S", "3") or "3")
# HTTP 可重试状态退避（2026-11-13）：429 是上游限流，立即重试必然再 429。
# 第1章补字实测：两次 attempt 都 429、扩写零增量、字数停在 1711。429 用更长窗口，
# 其余 408/5xx 与网络退避同口径。env 设 0 回滚为「立即重试」。
_HTTP_RETRY_DELAY_S = float(os.environ.get("NWW_LLM_HTTP_RETRY_DELAY_S", "3") or "3")
_HTTP_429_RETRY_DELAY_S = float(os.environ.get("NWW_LLM_429_RETRY_DELAY_S", "10") or "10")


def _http_retry_delay(status_code: int | None) -> float:
    """可重试 HTTP 状态的重试前退避秒数（429 限流单独加长）。"""
    return _HTTP_429_RETRY_DELAY_S if status_code == 429 else _HTTP_RETRY_DELAY_S

# 环境代理故障时的直连回退（2026-11-13）：远程端点默认 trust_env=True 走 http_proxy，
# 本机代理节点抖动时 httpx 抛 ProxyError/ConnectError，两次重试全部落空。这类故障与
# 端点本身是否可达无关——国内可直连的端点直连一次即可恢复。境外端点（必须走代理）
# 直连只会失败，因此给直连探测独立短预算，避免白等整段生成预算。
_DIRECT_FALLBACK_ENABLED = os.environ.get("NWW_LLM_DIRECT_FALLBACK", "1") == "1"
_DIRECT_PROBE_TIMEOUT_S = float(
    os.environ.get("NWW_LLM_DIRECT_PROBE_TIMEOUT_S", "20") or "20"
)


def _proxy_env_present() -> bool:
    """环境里是否配置了代理（httpx trust_env 会读取这些变量）。"""
    keys = ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY",
            "all_proxy", "ALL_PROXY")
    return any((os.environ.get(k) or "").strip() for k in keys)


def _post_chat_completion(
    url: str, headers: dict, payload: dict, *, timeout: float, base_url: str,
) -> httpx.Response:
    """POST chat/completions：远程端点走环境代理，代理故障时直连重试一次。"""
    use_proxy = not _is_local_host(base_url)
    try:
        return httpx.post(url, headers=headers, json=payload,
                          timeout=timeout, trust_env=use_proxy)
    except httpx.RequestError as proxy_exc:
        if not (use_proxy and _DIRECT_FALLBACK_ENABLED and _proxy_env_present()):
            raise
        try:
            print(
                f"[LLM_ERROR] 环境代理失败，直连重试 base_url={base_url} "
                f"cause={type(proxy_exc).__name__}: {proxy_exc}"[:400],
                flush=True,
            )
        except Exception:
            pass
        return httpx.post(url, headers=headers, json=payload,
                          timeout=min(timeout, _DIRECT_PROBE_TIMEOUT_S),
                          trust_env=False)


def _safe_observe(callback, **fields) -> None:
    """观测回调 fail-safe 调用（2026-10-07 P0）：callback 为空返回；自身异常全部吞掉。

    可观测层绝不能变成新的故障源——观测失败不得影响原 LLM 调用的异常/控制流。
    """
    if callback is None:
        return
    try:
        callback(**fields)
    except Exception:  # noqa: BLE001 — 观测失败绝不穿透主链
        pass


class LLMStatusError(RuntimeError):
    """携带 HTTP 状态码的调用失败（继承 RuntimeError，旧调用方 catch RuntimeError 不受影响）。"""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class ReasoningOnlyError(RuntimeError):
    """思考型模型只回 reasoning_content、未产出 content（可按加倍 max_tokens 重试）。"""


def is_configured() -> bool:
    """检查 LLM 是否已配置（API Key 存在）。"""
    return bool(os.environ.get("NWW_LLM_API_KEY", "").strip())


def _chat_completion_impl(
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.3,
    max_tokens: int = 4096,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout: Optional[float] = None,
) -> str:
    """调用 LLM 聊天补全。方法论应通过 system_prompt 传入，绝不外泄。

    可选覆盖参数（缺省一律回退读取环境变量，行为与旧版完全一致）：
      - base_url / model / api_key / timeout：由调用方显式指定时优先，
        否则读取 NWW_LLM_BASE_URL / NWW_LLM_MODEL / NWW_LLM_API_KEY / 默认超时。
      本地免鉴权服务（如 omix llm: http://localhost:8000/v1）可传 api_key="" 绕过 Key 非空校验。

    Returns:
        LLM 返回的文本内容。

    Raises:
        RuntimeError: LLM 未配置或调用失败。
    """
    if api_key is None:
        api_key = os.environ.get("NWW_LLM_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(
                "LLM 未配置：请设置环境变量 NWW_LLM_API_KEY 或显式传入 api_key（本地免鉴权服务可传空串）。"
            )
    else:
        api_key = api_key.strip()

    if base_url is None:
        base_url = os.environ.get("NWW_LLM_BASE_URL", _DEFAULT_BASE_URL)
    base_url = base_url.rstrip("/")
    if model is None:
        model = os.environ.get("NWW_LLM_MODEL", _DEFAULT_MODEL)
    timeout = timeout if timeout is not None else _TIMEOUT_S

    # OpenCode Zen 免费层：HTTP 直连一律 403 FreeTierError，须经 opencode 子进程桥接
    if opencode_bridge.is_opencode_free_endpoint(base_url, model):
        try:
            text = opencode_bridge.chat(system_prompt, user_prompt, model, timeout=timeout)
        except RuntimeError as exc:
            raise LLMStatusError(f"LLM 调用失败 (opencode 桥接): {exc}", status_code=403) from exc
        try:
            print(
                f"[LLM_TRACE] model={model} bridge=opencode chars={len(text)} tail100={repr(text[-100:])}",
                flush=True,
            )
        except Exception:
            pass
        return text

    # Chrome 网页版 VIP 桥接：chrome:// 端点不走 HTTP，经 CDP 操控本机 Chrome（AI Studio）
    if _CHROME_BRIDGE_ENABLED and chrome_bridge.is_chrome_vip_endpoint(base_url, model):
        # 网页桥接的耗时量级与 HTTP API 完全不同，取「调用方指定值 与 桥接下限」的较大者
        bridge_timeout = max(timeout, _CHROME_BRIDGE_TIMEOUT_S)
        try:
            text = chrome_bridge.chat(system_prompt, user_prompt, model,
                                      timeout=bridge_timeout)
        except RuntimeError as exc:
            raise LLMStatusError(f"LLM 调用失败 (chrome 桥接): {exc}", status_code=503) from exc
        try:
            print(
                f"[LLM_TRACE] model={model} bridge=chrome chars={len(text)} tail100={repr(text[-100:])}",
                flush=True,
            )
        except Exception:
            pass
        return text

    # OpenAI-HTTP 桥接（8788/8789）：只抬高超时预算，不改分发/参数
    if _is_bridge_http_endpoint(base_url, model):
        timeout = max(timeout, _BRIDGE_HTTP_TIMEOUT_S)

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        resp = _post_chat_completion(
            f"{base_url}/chat/completions", headers, payload,
            timeout=timeout, base_url=base_url,
        )
        resp.raise_for_status()
        data = resp.json()
        choice = data["choices"][0]
        msg = choice["message"]
        # 【2026-10-24 修复】思考型模型护栏：LM Studio 的 reasoning 模型把 token 烧在
        # 思考里时 message 只带 reasoning_content、没有 content 键（实测 Qwen3.6-35B
        # finish_reason=length）。此前 KeyError 'content' 报错误导排障方向；现在明确报因。
        if not isinstance(msg, dict) or msg.get("content") is None:
            reasoning = (msg.get("reasoning_content") or "") if isinstance(msg, dict) else ""
            raise ReasoningOnlyError(
                f"模型未返回 content（finish_reason={choice.get('finish_reason')}，"
                f"reasoning_content={len(str(reasoning))} 字符）：思考型模型把 max_tokens "
                f"烧在思考里。请换非推理模型 / 关闭思考 / 加大 max_tokens。"
            )
        # 【P1 诊断】保留 finish_reason/usage/尾部样本，防止 length/stop/content_filter 截断被静默吞掉。
        # 只打日志不改行为；生产观察期保留，后续可改为结构化日志收集。
        try:
            tail = repr((msg["content"] or "")[-100:])
            print(
                f"[LLM_TRACE] model={model} finish_reason={choice.get('finish_reason')} "
                f"completion_tokens={(data.get('usage') or {}).get('completion_tokens')} "
                f"prompt_tokens={(data.get('usage') or {}).get('prompt_tokens')} "
                f"chars={len(msg['content'] or '')} tail100={tail}",
                flush=True,
            )
        except Exception:
            pass
        return msg["content"]
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:500] if exc.response is not None else str(exc)
        raise LLMStatusError(
            f"LLM 调用失败 (HTTP {exc.response.status_code}): {detail}",
            status_code=exc.response.status_code,
        ) from exc
    except httpx.RequestError as exc:
        # 保留异常类型与原始原因：旧版只透传 str(exc) 后又被上层替换成「网络错误」，
        # 排障时无法区分「代理连不上 / DNS / 连接被重置 / 读超时」。
        detail = f"{type(exc).__name__}: {exc}"[:300]
        try:
            print(
                f"[LLM_ERROR] 网络请求失败 base_url={base_url} model={model} {detail}",
                flush=True,
            )
        except Exception:
            pass
        raise RuntimeError(f"LLM 网络请求失败: {detail}") from exc
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"LLM 响应解析失败: {exc}") from exc


def _live_model_label(model: Optional[str], base_url: Optional[str]) -> tuple[str, str]:
    """解析「展示用」模型名/端点（不改变真实调用参数，只为实况标注）。"""
    _model = model if model is not None else os.environ.get("NWW_LLM_MODEL", _DEFAULT_MODEL)
    _base = base_url if base_url is not None else os.environ.get("NWW_LLM_BASE_URL", _DEFAULT_BASE_URL)
    return str(_model or ""), str(_base or "")


def chat_completion(
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.3,
    max_tokens: int = 4096,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout: Optional[float] = None,
) -> str:
    """对外入口：真实实现见 _chat_completion_impl。

    2026-11-08：包一层 AI 实况采集（所有流程的每一次非流式模型调用都进流）。
    采集层自身全部兜底，异常不改变原调用语义。
    """
    from . import live_feed
    _model, _base = _live_model_label(model, base_url)
    with live_feed.track_call(system_prompt, user_prompt, model=_model, base_url=_base) as call:
        out = _chat_completion_impl(
            system_prompt, user_prompt,
            temperature=temperature, max_tokens=max_tokens,
            base_url=base_url, model=model, api_key=api_key, timeout=timeout,
        )
        call.finish(out)
        return out


def chat_completion_robust(
    system_prompt: str,
    user_prompt: str,
    chain: list[tuple[str, str, str]],
    *,
    temperature: float = 0.3,
    max_tokens: int = 4096,
    timeout: float = 120,
    retries: int = 1,
    report: dict | None = None,
) -> str:
    """沿端点链调用（模型中心 fallback 降级）：(base_url, model, api_key) 依序尝试。

    - 可重试错误（429/408/5xx/网络/超时）：先重试本端点 retries 次，再切下一端点
    - 配置类错误（401/403 等）：不重试本端点，直接切下一端点
    - 全部失败：抛 RuntimeError（附各段摘要，供端点转 502）
    - chain 为空：抛未配置错误
    - report（可选出参）：成功时写入 {"model", "base_url"]}——链路回退后让调用方
      知道**实际**命中的端点（chatbox 每条回复标注生成模型用，2026-10-25）
    """
    if not chain:
        raise RuntimeError("LLM 未配置：端点链为空（模型中心未启用或没有可用池模型）。")
    errors: list[str] = []
    for base_url, model, api_key in chain:
        _budget = max_tokens
        _budget_doubled = False
        for attempt in range(retries + 1):
            try:
                out = chat_completion(
                    system_prompt, user_prompt,
                    temperature=temperature, max_tokens=_budget,
                    base_url=base_url, model=model, api_key=api_key, timeout=timeout,
                )
                if report is not None:
                    report["model"] = model
                    report["base_url"] = base_url
                return out
            except ReasoningOnlyError as exc:
                # 思考型模型把预算烧在思考里：原地加倍 max_tokens 重试一次（仅一次），
                # 再失败则按原语义记录并降级，不给死循环机会。
                if not _budget_doubled:
                    _budget_doubled = True
                    _budget = max(_budget, 512) * 2
                    errors.append(f"[{base_url}::{model}] 思考烧光预算→加倍重试({_budget})")
                    continue
                errors.append(f"[{base_url}::{model}] {exc}")
                break
            except LLMStatusError as exc:
                tag = f"[{base_url}::{model}]"
                if exc.status_code in _RETRYABLE_STATUS:
                    errors.append(f"{tag} HTTP {exc.status_code}（第{attempt + 1}次）")
                    # 429/5xx 重试前退避（2026-11-13）：限流立即重试必然再 429。
                    if attempt < retries:
                        _delay = _http_retry_delay(exc.status_code)
                        if _delay > 0:
                            time.sleep(_delay)
                    continue
                errors.append(f"{tag} HTTP {exc.status_code}")
                break
            except RuntimeError as exc:
                msg = str(exc)
                if "网络请求失败" in msg:
                    cause = msg.split("网络请求失败:", 1)[-1].strip()[:200]
                    errors.append(
                        f"[{base_url}::{model}] 网络错误（第{attempt + 1}次：{cause}）"
                    )
                    # 瞬断后立即重试几乎必然再失败；退避只加在「还有重试机会」时。
                    if attempt < retries and _NET_RETRY_DELAY_S > 0:
                        time.sleep(_NET_RETRY_DELAY_S)
                    continue
                errors.append(f"[{base_url}::{model}] {msg}")
                break
    raise RuntimeError("LLM 调用全部失败：" + " | ".join(errors))


def _chat_completion_stream_impl(
    system_prompt: str,
    user_prompt: str,
    *,
    temperature: float = 0.3,
    max_tokens: int = 4096,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout: Optional[float] = None,
    response_format: Optional[dict] = None,
    finish_reason_holder: Optional[list] = None,
    observation_callback=None,
):
    """流式聊天补全生成器：SSE 逐段 yield 文本增量（不含结束标记）。

    参数解析与 chat_completion 完全一致（env 回退 / 本地免鉴权空 key）；
    调用失败抛 LLMStatusError / RuntimeError（由调用方决定降级或转 SSE error 事件）。
    response_format: 可选，OpenAI 兼容 JSON mode，如 {"type":"json_object"}。
    finish_reason_holder: 可选，list[str]，完成后写入最终 finish_reason（如 "stop" / "length"）。
    """
    if api_key is None:
        api_key = os.environ.get("NWW_LLM_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(
                "LLM 未配置：请设置环境变量 NWW_LLM_API_KEY 或显式传入 api_key（本地免鉴权服务可传空串）。"
            )
    else:
        api_key = api_key.strip()

    if base_url is None:
        base_url = os.environ.get("NWW_LLM_BASE_URL", _DEFAULT_BASE_URL)
    base_url = base_url.rstrip("/")
    if model is None:
        model = os.environ.get("NWW_LLM_MODEL", _DEFAULT_MODEL)
    timeout = timeout if timeout is not None else _TIMEOUT_S

    # OpenCode Zen 免费层：HTTP 直连一律 403 FreeTierError，须经 opencode 子进程桥接
    if opencode_bridge.is_opencode_free_endpoint(base_url, model):
        try:
            yield from opencode_bridge.chat_stream(
                system_prompt, user_prompt, model, timeout=timeout
            )
        except RuntimeError as exc:
            raise LLMStatusError(
                f"LLM 流式调用失败 (opencode 桥接): {exc}", status_code=403
            ) from exc
        return

    # Chrome 网页版 VIP 桥接：chrome:// 端点不走 HTTP，经 CDP 操控本机 Chrome（AI Studio）
    # 写章主笔流式直连（agents_runtime/pipeline → chat_completion_stream）在此拦截，
    # 防 chrome:// 端点掉入 httpx UnsupportedProtocol 协议陷阱
    if _CHROME_BRIDGE_ENABLED and chrome_bridge.is_chrome_vip_endpoint(base_url, model):
        try:
            yield from chrome_bridge.chat_stream(
                system_prompt, user_prompt, model, timeout=timeout
            )
        except RuntimeError as exc:
            raise LLMStatusError(
                f"LLM 流式调用失败 (chrome 桥接): {exc}", status_code=503
            ) from exc
        return

    # OpenAI-HTTP 桥接（8788/8789）：与非流式同规则，只抬高超时预算
    if _is_bridge_http_endpoint(base_url, model):
        timeout = max(timeout, _BRIDGE_HTTP_TIMEOUT_S)

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
    }
    if response_format is not None:
        payload["response_format"] = response_format
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # 流式请求可重试错误：网络瞬断（RemoteProtocolError/ConnectError/ReadTimeout 等）
    # 均是 httpx.RequestError 子类。最多重试 2 次，间隔 3s。
    # 注意：因生成器无法"续传"已 yield 的内容，重试只在首次 yield 前发生的连接/协议
    # 错误时有意义；如果服务端在流式中途关闭（本次错误场景），重试会重新发起完整请求。
    _STREAM_RETRIES = int(os.environ.get("NWW_STREAM_RETRIES", "2"))
    _STREAM_RETRY_DELAY = float(os.environ.get("NWW_STREAM_RETRY_DELAY_S", "3"))

    last_exc: Exception | None = None
    for _attempt in range(_STREAM_RETRIES + 1):
        if _attempt > 0:
            print(
                f"[LLM_STREAM_RETRY] attempt={_attempt} delay={_STREAM_RETRY_DELAY}s"
                f" reason={last_exc!r}",
                flush=True,
            )
            time.sleep(_STREAM_RETRY_DELAY)
        try:
            with httpx.stream(
                "POST", f"{base_url}/chat/completions",
                headers=headers, json=payload, timeout=timeout,
                trust_env=not _is_local_host(base_url),
            ) as resp:
                # 2026-11-01 修复「响应体不可读」遮蔽真实原因（真实事故：第1章 deslop 回执
                # 只剩 `LLM 流式调用失败 (HTTP 400): <响应体不可读: StreamClosed(...)>`，
                # 真实原因是 oMLX 内存守卫拒绝 prefill，服务端日志里写得明明白白）。
                # 根因：`raise_for_status()` 在 with 之外被处理时，context manager 已
                # `close()` 响应，`resp.read()` 必抛 StreamClosed —— 上游的真实拒绝理由
                # （模型未加载/内存不足/非 chat 模型/上下文超长）被整段吞掉，用户无法归因。
                # 修法：状态码检查放进 with，**流仍打开时**先读体再按原状态码分类
                # （重试集合/次数/间隔/请求参数全部不变，只改「读错误体」这一步）。
                if not resp.is_success:
                    _status = resp.status_code
                    try:
                        resp.read()
                        _detail = resp.text[:500]
                    except Exception as _read_exc:  # noqa: BLE001 — 体读不到不阻断错误上报
                        _detail = (
                            f"<响应体不可读: {_read_exc!r}>"
                            "（上游拒绝请求；常见原因：内存/显存不足、模型未加载、"
                            "上下文超长。详见模型服务日志）"
                        )
                    _safe_observe(observation_callback, attempt=_attempt + 1,
                                  status_code=_status, raw_error=_detail)
                    _err = LLMStatusError(
                        f"LLM 流式调用失败 (HTTP {_status}): {_detail}",
                        status_code=_status,
                    )
                    if _status in _RETRYABLE_STATUS and _attempt < _STREAM_RETRIES:
                        last_exc = _err
                        continue  # 退出 with（关闭响应）后按原退避重试
                    raise _err
                for line in resp.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    try:
                        choice = chunk["choices"][0]
                        delta = choice["delta"].get("content")
                        fr = choice.get("finish_reason")
                        if fr and finish_reason_holder is not None:
                            finish_reason_holder[0] = fr
                    except (KeyError, IndexError):
                        continue
                    if delta:
                        yield delta
            return  # 成功完成，退出重试循环
        except httpx.HTTPStatusError as exc:
            # 2026-11-01 起上面已在流内判状态码并抛 LLMStatusError，本分支理论上不再命中；
            # 保留为兜底（防后续改动回退到 raise_for_status），行为与旧版完全一致。
            # 2026-10-07 修复：httpx.stream 下响应体尚未 read()，直接取 exc.response.text
            # 会抛 httpx.ResponseNotRead，把真实 HTTP 错误（502/403/400…）遮蔽成无意义报错。
            # 必须先 read() 再截断；体不可读也不影响原始状态码上报。只修这一层，
            # 重试次数/状态码分类/请求参数均保持不变（外脑裁定边界）。
            resp = exc.response
            status = resp.status_code if resp is not None else 0
            if resp is None:
                detail = str(exc)
            else:
                try:
                    resp.read()
                    detail = resp.text[:500]
                except Exception as _read_exc:  # noqa: BLE001 — 体读取失败不阻断错误上报
                    detail = f"<响应体不可读: {_read_exc!r}>"
            _safe_observe(observation_callback, attempt=_attempt + 1,
                          status_code=status, raw_error=detail)
            if status in _RETRYABLE_STATUS and _attempt < _STREAM_RETRIES:
                last_exc = exc
                continue
            raise LLMStatusError(
                f"LLM 流式调用失败 (HTTP {status}): {detail}",
                status_code=status,
            ) from exc
        except httpx.RequestError as exc:
            # 包括 RemoteProtocolError（incomplete chunked read / peer closed connection）
            _safe_observe(observation_callback, attempt=_attempt + 1,
                          status_code=None, raw_error=str(exc))
            if _attempt < _STREAM_RETRIES:
                last_exc = exc
                continue
            raise RuntimeError(f"LLM 流式网络请求失败: {exc}") from exc


def chat_completion_stream(
    system_prompt: str,
    user_prompt: str,
    *,
    temperature: float = 0.3,
    max_tokens: int = 4096,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout: Optional[float] = None,
    response_format: Optional[dict] = None,
    finish_reason_holder: Optional[list] = None,
    observation_callback=None,
):
    """对外入口：真实实现见 _chat_completion_stream_impl。

    2026-11-08：包一层 AI 实况采集（流式调用逐段进流，展开可见实时输出）。
    采集层自身全部兜底，异常不改变原调用语义。
    """
    from . import live_feed
    _model, _base = _live_model_label(model, base_url)
    with live_feed.track_call(
        system_prompt, user_prompt, model=_model, base_url=_base, stream=True,
    ) as call:
        for chunk in _chat_completion_stream_impl(
            system_prompt, user_prompt,
            temperature=temperature, max_tokens=max_tokens,
            base_url=base_url, model=model, api_key=api_key, timeout=timeout,
            response_format=response_format,
            finish_reason_holder=finish_reason_holder,
            observation_callback=observation_callback,
        ):
            call.delta(chunk)
            yield chunk
        call.finish()
