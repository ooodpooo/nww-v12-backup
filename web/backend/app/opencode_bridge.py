"""OpenCode 免费层桥接：经 `opencode run` 子进程调用 Zen `*-free` 模型。

背景（2026-09-23 验证）：
  Zen 网关对 free 层模型强制 "只能从 OpenCode 客户端内部调用"——
  匿名 / 带 Zen key / 伪造指纹 header 的直连 HTTP 一律返回
  403 FreeTierError；只有 `opencode run` 本体可用。

职责边界：
  - 仅当 base_url 指向 opencode.ai/zen 且模型为 free 层时启用（is_opencode_free_endpoint）
  - 子进程在临时目录执行，加载内置 no-tool agent（llm-bridge），避免读取宿主项目
    上下文、避免模型调用工具；temperature/max_tokens 不被 `opencode run` 支持，
    忽略
  - 普通 HTTP 路径（本地 omlx / agnes / 付费 Zen）完全不受影响

Raises:
    RuntimeError: opencode 二进制缺失、子进程失败、超时或无文本输出。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

# no-tool agent：permissions 为空 = 全部工具拒绝，模型只产出文本
_AGENT_MD = """---
description: Pure text completion bridge. No tools allowed.
mode: primary
permissions: []
---
You are a pure text completion engine. Never call tools, never read files.
Answer only with the requested content, following the instructions given.
"""

_AGENT_NAME = "llm-bridge"


def _resolve_opencode_bin() -> str:
    """定位 opencode 可执行文件：PATH 优先，兜底常见安装路径。"""
    found = shutil.which("opencode")
    if found:
        return found
    for cand in (
        Path.home() / ".local" / "bin" / "opencode",
        Path.home() / ".opencode" / "bin" / "opencode",
    ):
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    raise RuntimeError("未找到 opencode 可执行文件（PATH / ~/.local/bin / ~/.opencode/bin）")


def is_opencode_free_endpoint(base_url: str, model: str) -> bool:
    """判断端点是否为 OpenCode Zen 免费层（须走子进程桥接）。

    规则：base_url 指向 opencode.ai/zen，且模型 id 以 `-free` 结尾
    或为已知免费别名 `big-pickle`。
    """
    if "opencode.ai/zen" not in (base_url or "").lower():
        return False
    m = (model or "").strip().lower()
    return m.endswith("-free") or m == "big-pickle"


def _bridge_env(workdir: str) -> dict:
    """子进程环境：PATH 补 ~/.local/bin，并将 PWD 指向工作目录。

    关键：opencode（Node）优先读 env PWD 而非 cwd(2)——若继承调用方 PWD
    （如宿主项目目录），会加载错误项目上下文导致 UnknownError。
    """
    env = os.environ.copy()
    local_bin = str(Path.home() / ".local" / "bin")
    if local_bin not in env.get("PATH", ""):
        env["PATH"] = local_bin + os.pathsep + env.get("PATH", "")
    env["PWD"] = workdir
    return env


def _make_workdir() -> tempfile.TemporaryDirectory:
    """创建带 no-tool agent 的临时工作目录（隔离宿主项目上下文）。"""
    tmp = tempfile.TemporaryDirectory(prefix="nww_oc_bridge_")
    agent_dir = Path(tmp.name) / ".opencode" / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / f"{_AGENT_NAME}.md").write_text(_AGENT_MD, encoding="utf-8")
    return tmp


def _build_message(system_prompt: str, user_prompt: str) -> str:
    """合并 system/user 为单条消息（`opencode run` CLI 无独立 system 参数）。"""
    sys_p = (system_prompt or "").strip()
    usr_p = (user_prompt or "").strip()
    if sys_p and usr_p:
        return f"[SYSTEM]\n{sys_p}\n[/SYSTEM]\n\n[USER]\n{usr_p}\n[/USER]"
    return sys_p or usr_p


def _cmd(model: str, message: str) -> list[str]:
    """组装 `opencode run` 命令行（--pure 跳过外部插件，--format json 供解析）。"""
    return [
        _resolve_opencode_bin(),
        "run",
        "--pure",
        "--agent", _AGENT_NAME,
        "--model", f"opencode/{model}",
        "--format", "json",
        message,
    ]


def _parse_text_events(stdout_text: str) -> str:
    """从 --format json 的 stdout 提取全部 type==text 文本并拼接。"""
    chunks: list[str] = []
    for raw in stdout_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if evt.get("type") != "text":
            continue
        text = (evt.get("part") or {}).get("text")
        if text:
            chunks.append(text)
    return "".join(chunks)


def _extract_error(stdout_text: str) -> str:
    """从 --format json stdout 提取 type==error 事件的错误信息（失败诊断用）。"""
    for raw in (stdout_text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if evt.get("type") != "error":
            continue
        err = evt.get("error") or {}
        data = err.get("data") or {}
        msg = data.get("message") or err.get("message") or ""
        if msg:
            return str(msg)[:500]
    return ""


def _iter_text_events(proc: subprocess.Popen, errors: list[str]) -> Iterator[str]:
    """逐行读取子进程 stdout，产出 type==text 文本增量；error 事件记入 errors。"""
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = evt.get("type")
        if etype == "error":
            err = evt.get("error") or {}
            data = err.get("data") or {}
            errors.append(str(data.get("message") or err.get("message") or "unknown error"))
            continue
        if etype != "text":
            continue
        text = (evt.get("part") or {}).get("text")
        if text:
            yield text


def chat(
    system_prompt: str,
    user_prompt: str,
    model: str,
    timeout: float = 300,
) -> str:
    """经 opencode 子进程做一次性补全，返回全部文本。

    Args:
        system_prompt: 系统提示（与 user 合并为单条消息）。
        user_prompt: 用户提示。
        model: free 层模型 id（如 mimo-v2.6-flash-free），自动加 opencode/ 前缀。
        timeout: 子进程超时秒数。

    Returns:
        模型输出全文。

    Raises:
        RuntimeError: 找不到二进制、超时、或输出为空。
    """
    message = _build_message(system_prompt, user_prompt)
    with _make_workdir() as tmp:
        try:
            proc = subprocess.run(
                _cmd(model, message),
                cwd=tmp,
                env=_bridge_env(tmp),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"opencode 桥接超时（{timeout}s）") from exc
        result = _parse_text_events(proc.stdout or "").strip()
        if result:
            return result
        err = (proc.stderr or "").strip()[-500:] or _extract_error(proc.stdout or "")
        raise RuntimeError(
            f"opencode 桥接无输出（exit={proc.returncode}）：{err or '未知错误'}"
        )


def chat_stream(
    system_prompt: str,
    user_prompt: str,
    model: str,
    timeout: float = 300,
) -> Iterator[str]:
    """经 opencode 子进程做流式补全，逐段 yield 文本增量。

    Args:
        system_prompt: 系统提示。
        user_prompt: 用户提示。
        model: free 层模型 id。
        timeout: 子进程整体超时秒数。

    Yields:
        文本增量。

    Raises:
        RuntimeError: 找不到二进制、启动失败、超时或全程无输出。
    """
    message = _build_message(system_prompt, user_prompt)
    with _make_workdir() as tmp:
        # stderr 写文件而非 PIPE：不消费的 PIPE 会填满缓冲导致子进程死锁
        # 注意：TemporaryDirectory 作为 context manager 时 with-as 拿到的是路径 str
        err_path = Path(tmp) / "bridge.stderr"
        err_f = err_path.open("w", encoding="utf-8")
        try:
            try:
                proc = subprocess.Popen(
                    _cmd(model, message),
                    cwd=tmp,
                    env=_bridge_env(tmp),
                    stdout=subprocess.PIPE,
                    stderr=err_f,
                    text=True,
                )
            except OSError as exc:
                raise RuntimeError(f"opencode 桥接启动失败: {exc}") from exc
            emitted = False
            stream_errors: list[str] = []
            try:
                for chunk in _iter_text_events(proc, stream_errors):
                    emitted = True
                    yield chunk
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                proc.kill()
                raise RuntimeError(f"opencode 桥接超时（{timeout}s）") from exc
            finally:
                if proc.poll() is None:
                    proc.kill()
            if not emitted:
                err = stream_errors[0] if stream_errors else ""
                if not err:
                    try:
                        err = err_path.read_text(encoding="utf-8").strip()[-500:]
                    except OSError:
                        pass
                raise RuntimeError(
                    f"opencode 桥接无输出（exit={proc.returncode}）：{err or '未知错误'}"
                )
        finally:
            err_f.close()
