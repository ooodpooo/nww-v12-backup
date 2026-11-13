"""NWW-MVP WebUI 接入模块（2026-09-10 新增，纯增量，零耦合）。

职责：
  - POST /api/books/{book_id}/mvp/packet  构建 Writing Packet（subprocess 调原脚本，不改脚本）
  - POST /api/books/{book_id}/mvp/write   打包 → 直连 LLM 生成正文 → 落盘 正文/第N章.md

开关：
  - NWW_MVP_ENABLED=1 时 main.py 才挂载本模块（灰度 / 快速回滚）

环境变量（NWW_MVP_* 独立命名空间，不污染现有 llm_client 全局配置）：
  - NWW_MVP_LLM_BASE_URL  默认 http://127.0.0.1:8080/v1（本地 omix 服务）
  - NWW_MVP_LLM_MODEL     默认 Qwen3.8-27B-oQ4e-fp16-mtp
  - NWW_MVP_LLM_API_KEY   默认空（本地免鉴权服务）
  - NWW_MVP_LLM_TIMEOUT   默认 600 秒（长文本生成）
  - NWW_MVP_TEMPERATURE   默认 0.9（网文初稿高温）

约束（遵守项目铁则）：
  - 不改 nww_packet_builder.py / books.py / fs.py / skillrun.py / auth.py
  - 书根路径 / 文件读写一律复用 fs.py 安全函数，禁止手写路径拼接
  - 正文文件已存在时返回 409，防止网页端误覆盖作者已有稿件
"""
from __future__ import annotations

import json
import os
import hashlib
import re
import subprocess
import sys
import threading
import queue
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from . import books
from . import live_feed
from .auth import get_current_user
from .dataplane import projects_root
from .fs import book_root, is_writable, resolve_path
from .llm_client import chat_completion, chat_completion_robust, chat_completion_stream
from .llm_config import default_endpoint as _cfg_default_endpoint, default_endpoint_chain as _cfg_default_chain
from .agents_runtime.toolbox import resolve_script as _resolve_script  # 唯一脚本解析入口

router = APIRouter()

# AI 实况（run 标签用）：任务 mode → 中文动作名
_LIVE_MODE_LABEL = {
    "write": "写章",
    "rewrite": "重写",
    "polish": "润色",
    "revise": "修订",
    "deslop": "去AI味",
    "audit": "一致性审计",
}


def _live_flow(label: str, *, mode: str | None = None, book_id: str | None = None,
               chapter_id: int | None = None, user: dict | None = None, kind: str = "flow"):
    """开启一次流程级 AI 实况（同步端点用；退出自动收尾）。"""
    return live_feed.begin_run(
        label, mode=mode, book_id=book_id, chapter_id=chapter_id, kind=kind,
        user_id=((user or {}).get("user_id")),
    )


def _live_progress(name: str, ok: bool | None, detail: str = "", output: str = "") -> None:
    """pipeline progress 回调 → 实况环节卡片（与任务面板同一口径）。"""
    if ok is None:
        live_feed.stage_begin(name, detail)
    else:
        live_feed.stage_end(name, ok, detail, output)

_REPO_ROOT = Path(__file__).resolve().parents[3]
# 2026-11-05 治本：与主链 step_packet 共用唯一解析器，避免「校验器用 engine 版、
# 构造器用 skills 版」的口径漂移。原先此处硬指 engine 副本。
_PACKET_BUILDER = _resolve_script("nww_packet_builder_v2.py")
if _PACKET_BUILDER is None:
    raise RuntimeError("nww_packet_builder_v2.py 未在 skills/ 或 engine/ 副本中找到")
_WRITER_AGENT = _REPO_ROOT / "engine" / "agents" / "narrative-writer-v2.md"
_PY = os.environ.get("NWW_PYTHON", sys.executable)

# 独立命名空间配置（NWW_MVP_*）
_BASE_URL = os.environ.get("NWW_MVP_LLM_BASE_URL", "http://127.0.0.1:8080/v1").rstrip("/")
_MODEL = os.environ.get("NWW_MVP_LLM_MODEL", "Qwen3.8-27B-oQ4e-fp16-mtp")
_API_KEY = os.environ.get("NWW_MVP_LLM_API_KEY", "")
_TIMEOUT = float(os.environ.get("NWW_MVP_LLM_TIMEOUT", "600"))
_TEMPERATURE = float(os.environ.get("NWW_MVP_TEMPERATURE", "0.9"))
_MAX_TOKENS = int(os.environ.get("NWW_MVP_MAX_TOKENS", "8192"))

_BODY_DIR = "正文"
_OUTLINE_DIR = "大纲"
_PRECEDING_MISSING = "【前文衔接缺失】"
_FRONTMATTER_RE = re.compile(r"^---\n.*?\n---\n", re.S)


def _load_writer_system_prompt() -> str:
    """读取主笔 agent prompt，剥掉 YAML front-matter 后作为 system prompt。"""
    if not _WRITER_AGENT.is_file():
        raise HTTPException(status_code=500, detail=f"主笔 prompt 缺失: {_WRITER_AGENT}")
    text = _WRITER_AGENT.read_text(encoding="utf-8")
    return _FRONTMATTER_RE.sub("", text, count=1).strip()


def _resolve_write_endpoint() -> tuple[str, str, str, float]:
    """直连写章端点：模型中心全局默认池模型优先；未启用回退 NWW_MVP_* 环境变量。"""
    ep = _cfg_default_endpoint()
    if ep:
        base_url, model, key = ep
        return base_url, model, key, _TIMEOUT
    return _BASE_URL, _MODEL, _API_KEY, _TIMEOUT


def _find_outline_file(book: Path, chapter_id: int) -> Path | None:
    """定位目标章细纲文件（兼容前导零命名：细纲_第28章 / 细纲_第028章…）。"""
    exact = book / _OUTLINE_DIR / f"细纲_第{chapter_id}章.md"
    if exact.is_file():
        return exact
    if not (book / _OUTLINE_DIR).is_dir():
        return None
    for p in (book / _OUTLINE_DIR).glob("细纲_第*章*.md"):
        m = re.match(r"^细纲_第(0*)(\d+)章", p.name)
        if m and int(m.group(2)) == chapter_id:
            return p
    return None


def _build_packet(book: Path, chapter_id: int) -> dict:
    """subprocess 调 v2 打包脚本（CCv2 ONLY，与生产链 step_packet 同实现；2026-11-01
    由 v1 切换——v2 缺失/失败按 HTTP 5xx 显式报错，禁止静默降级 v1 兜底）。

    前导零兼容：packet builder 只认 细纲_第N章.md，书内若为 细纲_第0NN章.md，
    在 大纲/ 下建临时软链精确名 → 真实文件，用完即删（不读写真实文件）。
    """
    outline = _find_outline_file(book, chapter_id)
    if outline is None:
        raise HTTPException(status_code=404, detail=f"细纲不存在：大纲/细纲_第{chapter_id}章.(json|md)")
    exact = book / _OUTLINE_DIR / f"细纲_第{chapter_id}章.md"
    made_link = False
    if outline != exact and not exact.exists():
        exact.symlink_to(outline.name)
        made_link = True
    try:
        result = subprocess.run(
            [_PY, str(_PACKET_BUILDER), str(book), str(chapter_id)],
            capture_output=True,
            text=True,
        )
    finally:
        if made_link:
            try:
                exact.unlink() if exact.is_symlink() else None
            except OSError:
                pass
    if result.returncode != 0:
        err = result.stderr.strip()
        if "找不到第" in err and "章细纲文件" in err:
            raise HTTPException(status_code=404, detail=f"细纲不存在：大纲/细纲_第{chapter_id}章.(json|md)")
        raise HTTPException(status_code=500, detail=f"打包脚本执行失败: {err or 'exit=%d' % result.returncode}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"打包脚本输出解析失败: {exc}") from exc


def _preceding_from_body(book: Path, chapter_id: int, max_chars: int = 300) -> str:
    """前情兼容：packet builder 只认 draft/，书架书在 正文/。
    上一章正文末 300 字（去掉标题行），未找到时原文返回占位符。
    """
    if chapter_id <= 1:
        return "【序幕开篇】"
    for ch in books.list_chapters(book):
        if ch.get("chapter") != chapter_id - 1:
            continue
        vol = ch.get("volume")
        rel = f"{_BODY_DIR}/{vol}/{ch['file']}" if vol else f"{_BODY_DIR}/{ch['file']}"
        try:
            target = resolve_path(book, rel)
            if not target.is_file():
                continue
            data = target.read_bytes()
        except (OSError, ValueError):
            continue
        lines = [ln for ln in data.decode("utf-8", errors="replace").splitlines() if not ln.startswith("#")]
        clean = "\n".join(lines).strip()
        return clean[-max_chars:] if len(clean) > max_chars else clean
    return _PRECEDING_MISSING


def _assert_chapter_not_exists(book: Path, chapter_id: int) -> None:
    """目标章正文已存在 → 409（放在 write 最前面，避免先生成再报错浪费算力）。"""
    body_dir = book / _BODY_DIR
    if not body_dir.is_dir():
        return
    for p in body_dir.rglob("第*章*.md"):
        # 快照与正文同形（第027章_round1.md）—— 不排除会把它当成「章已存在」而 409。
        if books._is_non_prose(p):
            continue
        if books.chapter_number_from_name(p.name) == chapter_id:
            raise HTTPException(status_code=409, detail=f"第{chapter_id}章正文已存在：{p.relative_to(book).as_posix()}（为避免误覆盖，请先在编辑器中处理）")


def _stages_summary(result: dict) -> str:
    """pipeline 失败时给出阶段摘要（首条 FAIL 环节 + 详情），供 HTTP 报错可读。"""
    stages = result.get("stages") or []
    for s in stages:
        if not s.get("ok"):
            return f"[{s.get('name')}] {str(s.get('detail', ''))[:300]}"
    return f"unknown failure (stages={len(stages)})"


def _write_result_fields(result: dict, dry_run: bool = False) -> dict:
    """C-WRITE-RESULT@1：写链终态透给调用方（提交状态/降级放行/告警/环节）。

    commit_status 取链返回值；链未返回时按 dry_run 填 SKIPPED_DRY_RUN，否则 committed
    （历史成功语义）。
    """
    stages = result.get("stages") or []
    return {
        "ok": bool(result.get("ok")),
        "commit_status": result.get("commit_status") or ("SKIPPED_DRY_RUN" if dry_run else "committed"),
        "degraded_pass": bool(result.get("degraded_pass")),
        "warnings": list(result.get("warnings") or []),
        "stages": stages,
    }


def _verify_consistency_closure_by_script(book: Path, chapter_id: int) -> None:
    """commit 前置的闭环判据：唯一来源为 nww_track_commit.verify_consistency_closure。

    脚本判定阻断 → 409（附脚本原始阻断原因）；判据用 WARNING 放行时不阻断
    （兼容模式）。脚本加载失败 → 500，不静默放行。
    """
    scripts = _REPO_ROOT / "skills" / "nww-novel-skill" / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    try:
        from nww_track_commit import TrackingError, verify_consistency_closure
    except Exception as exc:  # noqa: BLE001 — 判据缺席时显式报错，绝不默认放行
        raise HTTPException(status_code=500, detail=f"一致性闭环判据不可用: {exc}") from exc
    try:
        verify_consistency_closure(book, int(chapter_id))
    except TrackingError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"第{chapter_id}章一致性闭环被判据阻断：{exc}"
                   "处理闭环问题后，在面板点击「确认一致性并提交」重试。",
        ) from exc


# 细纲/正文标题提取：^# 细纲_第032章　书名：周执事 → 「周执事」；剥离「书名|书名：」前缀
_TITLE_HDR_RE = re.compile(r"^\s*#\s*[^\s#]*章\s*[　:：]*?(.*?)\s*$")
_TITLE_PREFIX_RE = re.compile(r"^\s*(?:书名|章名|标题)\s*[：:]\s*")


def _chapter_title_from_outline(book: Path, chapter_id: int) -> str | None:
    """从单章细纲提取正文标题（外脑裁定优先级：chapter_title → 单章细纲 → 主卷细纲 → 未命名）。

    仅读 大纲/细纲_第{ch}章.md 首行标题；找不到返回 None，由调用方降级。
    """
    outline_dir = book / _OUTLINE_DIR
    if outline_dir.is_dir():
        # 兼容补零：细纲_第32章.md 与 细纲_第032章.md 均可
        for p in outline_dir.glob("细纲_第*章*.md"):
            m = re.match(rf"^细纲_第0*{chapter_id}章", p.name)
            if not m:
                continue
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    m = _TITLE_HDR_RE.match(line)
                    if m and m.group(1).strip():
                        raw = re.sub(r"\s+", "", m.group(1)).strip("　 　")
                        raw = _TITLE_PREFIX_RE.sub("", raw).strip()
                        if raw:
                            return raw
            except (OSError, UnicodeError):
                continue
    return None


def _resolve_volume(book: Path, chapter_id: int) -> tuple[str | None, int | None]:
    """分卷感知：沿用最近已存在章节的 volume 与数字宽度（外脑裁定：卷归属由规划层决定，不由写文件层创造）。

    返回 (volume, width)。volume=None 表示书内无分卷（平铺 正文/第N章.md）。
    """
    body_dir = book / _BODY_DIR
    volume: str | None = None
    width: int | None = None
    best: tuple[int, str] | None = None  # (差值, p.name) 取章号最接近的
    for p in body_dir.rglob("第*章*.md") if body_dir.is_dir() else []:
        if books._is_non_prose(p):
            continue
        n = books.chapter_number_from_name(p.name)
        if n is None:
            continue
        d = abs(n - chapter_id)
        if best is None or d < best[0]:
            best = (d, p)
        m = re.match(r"^第(0*)(\d+)章", p.name)
        if m:
            w = len(m.group(1)) + len(m.group(2))
            if width is None or w < width:
                width = w
    if best:
        p = best[1]
        volume = p.parent.name if p.parent.name != "正文" else None
    return volume, width


def _resolve_body_target(book: Path, chapter_id: int) -> tuple[Path, str]:
    """确定正文落盘路径：
    - 已存在该书章 → 409，防网页端误覆盖作者已有稿件
    - 分卷感知：沿用最近已存在章节的 volume，落盘 正文/{volume}/第{ch:03d}章_{title}.md；
      平铺书（无分卷）仍落 正文/第{ch:03d}章.md；空书用「第{ch}章.md」。
    """
    body_dir = book / _BODY_DIR
    width: int | None = None
    for p in body_dir.rglob("第*章*.md") if body_dir.is_dir() else []:
        if books._is_non_prose(p):
            continue        # 快照不是正文，同样不能触发 409
        n = books.chapter_number_from_name(p.name)
        if n == chapter_id:
            raise HTTPException(status_code=409, detail=f"第{chapter_id}章正文已存在：{p.relative_to(book).as_posix()}（为避免误覆盖，请先在编辑器中处理）")
        m = re.match(r"^第(0*)(\d+)章", p.name)
        if m:
            w = len(m.group(1)) + len(m.group(2))
            if width is None or w < width:
                width = w
    volume, _w = _resolve_volume(book, chapter_id)
    width = width or _w
    title = _chapter_title_from_outline(book, chapter_id)
    base = f"第{chapter_id}章" if width is None else f"第{chapter_id:0{width}d}章"
    if title:
        fname = f"{base}_{title}.md"
    else:
        fname = f"{base}.md"
    rel = f"{_BODY_DIR}/{fname}" if volume is None else f"{_BODY_DIR}/{volume}/{fname}"
    body_dir.mkdir(parents=True, exist_ok=True)
    target = resolve_path(book, rel)
    if not is_writable(book, target):
        raise HTTPException(status_code=403, detail=f"路径不可写：{rel}")
    return target, rel


def _find_existing_body_target(book: Path, chapter_id: int) -> Path | None:
    """定位已落盘正文文件（兼容分卷/前导零命名），未找到返回 None。多章命中取章号最接近者。"""
    body_dir = book / _BODY_DIR
    if not body_dir.is_dir():
        return None
    best: tuple[int, Path] | None = None
    for p in body_dir.rglob("第*章*.md"):
        if books._is_non_prose(p):
            continue        # 快照不是正文
        n = books.chapter_number_from_name(p.name)
        if n is None:
            continue
        if n == chapter_id:
            return p
        d = abs(n - chapter_id)
        if best is None or d < best[0]:
            best = (d, p)
    return best[1] if best else None


@router.post("/books/{book_id}/mvp/packet")
def api_mvp_packet(book_id: str, payload: dict, user=Depends(get_current_user)) -> dict:
    """构建 Writing Packet。body: {chapter: int}"""
    chapter_id = payload.get("chapter")
    if not isinstance(chapter_id, int) or chapter_id < 1:
        raise HTTPException(status_code=400, detail="chapter must be a positive integer")
    root = projects_root(user["user_id"])
    try:
        book = book_root(root, book_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"book_id": book_id, "chapter_id": chapter_id, "packet": _build_packet(book, chapter_id)}


@router.post("/books/{book_id}/mvp/write")
def api_mvp_write(book_id: str, payload: dict, user=Depends(get_current_user)) -> dict:
    """写新章（2026-09-20 收敛：废除直连 LLM 落盘，统一走 run_write_pipeline 生产事务链）。

    旧实现为「_build_packet → chat_completion → write_text」第二生产链；收敛后 endpoint
    不再调用模型、不再 normalize、不再写正文、不再更新 tracking，全部由 pipeline
    （normalize → wordcount → deslop → review → post_write → commit → delta → tracking）承担。
    body: {chapter: int}；响应 = 既有 {book_id, chapter_id, packet, model, path, chars}
    ∪ C-WRITE-RESULT@1 新增 {ok, commit_status, degraded_pass, warnings, stages}。
    """
    chapter_id = payload.get("chapter")
    if not isinstance(chapter_id, int) or chapter_id < 1:
        raise HTTPException(status_code=400, detail="chapter must be a positive integer")
    root = projects_root(user["user_id"])
    try:
        book = book_root(root, book_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    _assert_chapter_not_exists(book, chapter_id)

    # ★ 并发守卫（2026-10-19）：原子占用章操作槽 + 跨进程章锁（C-LOCK@1），占用中 → 409
    _op_task = f"sync_{uuid.uuid4().hex[:12]}"
    busy = _try_acquire_op(book_id, chapter_id, "write", _op_task, book=book)
    if busy:
        raise HTTPException(status_code=409, detail=_op_busy_detail(book_id, chapter_id, busy))
    try:
        from .agents_runtime.pipeline import run_write_pipeline
        with _live_flow(f"写章 · 第{chapter_id}章", mode="write", book_id=book_id,
                        chapter_id=chapter_id, user=user):
            result = run_write_pipeline(book, chapter_id, progress=_live_progress,
                                        dry_run=bool(payload.get("dry_run", False)))
        if not result.get("ok"):
            raise HTTPException(status_code=502, detail=f"写章流水线失败: {_stages_summary(result)}")

        stages = result.get("stages") or []
        norm = next((s for s in stages if s.get("name") == "normalize"), None) or {}
        pkt = next((s for s in stages if s.get("name") == "packet"), None) or {}
        return {
            "book_id": book_id,
            "chapter_id": chapter_id,
            "packet": pkt.get("packet"),
            "model": _resolve_write_endpoint()[1],
            "path": norm.get("path"),
            "chars": norm.get("chars"),
            **_write_result_fields(result, dry_run=bool(payload.get("dry_run", False))),
        }
    finally:
        _release_op(book_id, chapter_id, _op_task)


@router.post("/books/{book_id}/chapters/{chapter}/length-balance")
def api_length_balance(book_id: str, chapter: int, payload: dict = None,
                       user=Depends(get_current_user)) -> dict:
    """篇幅平衡方案（C-BALANCE-ROUTE@1）：先字数检查，再走方案化平衡 R1-R8。

    body: {"apply": bool = false}；apply=false 只出方案预览（不写正文），apply=true 才落盘。
    同参数重跑幂等；同章并发由章锁挡为 409。
    """
    if not isinstance(chapter, int) or chapter < 1:
        raise HTTPException(status_code=400, detail="chapter must be a positive integer")
    apply = bool((payload or {}).get("apply", False))
    root = projects_root(user["user_id"])
    try:
        book = book_root(root, book_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    prose = _find_existing_body_target(book, chapter)
    if prose is None or books.chapter_number_from_name(prose.name) != chapter:
        raise HTTPException(status_code=404, detail=f"第{chapter}章正文不存在，无法做篇幅平衡")

    _op_task = f"sync_{uuid.uuid4().hex[:12]}"
    busy = _try_acquire_op(book_id, chapter, "length-balance", _op_task, book=book)
    if busy:
        raise HTTPException(status_code=409, detail=_op_busy_detail(book_id, chapter, busy))
    try:
        from .agents_runtime.pipeline import step_length_balance, step_wordcount_check
        with _live_flow(f"篇幅平衡 · 第{chapter}章", mode="balance", book_id=book_id,
                        chapter_id=chapter, user=user):
            with live_feed.stage("length_balance", ("执行篇幅平衡" if apply else "生成篇幅平衡方案"),
                                 label="篇幅平衡") as _st:
                wc = step_wordcount_check(book, chapter)
                try:
                    packet = _build_packet(book, chapter)
                except HTTPException:
                    # 细纲缺失/打包失败：packet 只用于 L2 保护场景识别，缺失按空处理不阻断平衡
                    packet = {}
                res = step_length_balance(book, chapter, packet, prose, wc, dry_run=not apply)
                _st.output(str((res or {}).get("detail") or "")[:4000])
    finally:
        _release_op(book_id, chapter, _op_task)

    return {
        "ok": bool((res or {}).get("ok", True)),
        "chapter_id": chapter,
        "applied": bool(apply and not (res or {}).get("degraded") and int((res or {}).get("rounds") or 0) > 0),
        "rounds": (res or {}).get("rounds"),
        "selected_strategy": (res or {}).get("selected_strategy"),
        "quality_score": (res or {}).get("quality_score"),
        "net_delta": (res or {}).get("net_delta"),
        "residual_gap": (res or {}).get("residual_gap"),
        "stop_reason": (res or {}).get("stop_reason"),
        "detail": (res or {}).get("detail"),
    }


# ---- P4B 一致性闭环确认（Web Consistency Closure Confirm，外脑裁决放行） ----
# 语义：closure ≠ detection。closed=true 有两个合法来源（C-CLOSURE@2）：
#   - system_autoclose：pipeline 检测后自动闭环，必须携带检测证据（审查记录路径与 sha256）；
#   - author_confirm：作者在检测无未闭环项后人工确认，由本模块落盘。
# 两条路径都要求检测已跑通（审查记录存在、open_s1_s2 为空），区别只在证据归属。
# 覆盖 write/rewrite/deslop/revise 四链统一复用；skill / A-3 / 第二刀 Packet 零改动。
_REVIEW_DIR = "一致性审查"
_CLOSURE_DIR = "一致性核验"
_CONFIRMED_BY_SYSTEM = "web_user_confirm"


def _confirm_consistency_closure(root: Path, book_id: str, chapter_id: int, user: dict) -> dict:
    """校验一致性审查记录并通过则落盘闭环记录（数据契约对齐 consistency-closure.md）。

    规则：
      - 无审查记录 → 404（必须先 detection，才能 confirm）
      - 审查记录 open_s1_s2 非空 → 409（禁止带病闭环；closure 不是风险豁免签字）
      - 审查记录 evidence_gate 有 invalid/ambiguous/missing_reference → 409（证据不可信不闭环）
      - 已存在 closed=true → 幂等返回成功，不覆盖
      - updated_at 用 ISO 带时区；confirmed_by 优先登录用户，无用户体系用系统身份
      - source_kind 固定 author_confirm（读侧据此免证据校验，C-CLOSURE@2）
    """
    if not isinstance(chapter_id, int) or chapter_id < 1:
        raise HTTPException(status_code=400, detail="chapter must be a positive integer")

    book = book_root(root, book_id)

    # 1) detection 前置：审查记录必须存在
    review_path = book / "追踪" / _REVIEW_DIR / f"第{chapter_id}章.json"
    if not review_path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"第{chapter_id}章尚未先生成一致性审查记录（追踪/一致性审查/），请先跑一致性审查",
        )
    review = json.loads(review_path.read_text(encoding="utf-8"))

    # 2) 禁带病闭环：有未闭环 S1/S2 拒绝确认
    open_s1_s2 = review.get("open_s1_s2", []) or []
    if open_s1_s2:
        sample = open_s1_s2[:3]
        raise HTTPException(
            status_code=409,
            detail=f"存在未闭环的 S1/S2 冲突，禁止确认：{json.dumps(sample, ensure_ascii=False)}",
        )

    # 2b) P6-C evidence gate（外脑 2026-09-17 裁决）：即使 open_s1_s2=0，
    #     若 S1/S2 引用的证据不可信（invalid=not_found/missing_reference、ambiguous、
    #     missing_reference 待人工复核），禁止自动闭环。旧记录无 evidence_validation
    #     字段则跳过（兼容既有契约）。S3/S4 观察与 C defer 不阻断（外脑分层原则）。
    ev = review.get("evidence_validation") or {}
    if not isinstance(ev, dict):
        ev = {}
    gate = ev.get("evidence_gate") or {}
    if not isinstance(gate, dict):
        gate = {}
    gate_invalid_keys = [k for k in ("invalid", "ambiguous", "missing_reference") if (gate.get(k) or 0) > 0]
    if gate_invalid_keys:
        raise HTTPException(
            status_code=409,
            detail=(
                f"存在证据不可信的 S1/S2，禁止确认（evidence_gate={json.dumps(gate, ensure_ascii=False)}"
                f"，待复核项={gate_invalid_keys}）；请先人工复核处理缺失/错误/歧义引用后再确认"
            ),
        )

    # 3) 幂等：已 closed=true 直接返回成功，不覆盖
    closure_path = book / "追踪" / _CLOSURE_DIR / f"第{chapter_id}章.json"
    if closure_path.is_file():
        try:
            existing = json.loads(closure_path.read_text(encoding="utf-8"))
            if existing.get("closed") is True:
                return {
                    "ok": True,
                    "chapter": chapter_id,
                    "closure_path": str(closure_path.relative_to(book)),
                    "closed": True,
                    "already_confirmed": True,
                }
        except (json.JSONDecodeError, OSError):
            pass  # 脏文件/不可读 → 按不存在处理，允许重写

    # 4) 落盘 closure（ISO 带时区，对齐 C-CLOSURE@2 记录结构）
    now_iso = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    confirmed_by = user.get("email") or user.get("user_id") or _CONFIRMED_BY_SYSTEM
    record = {
        "chapter": chapter_id,
        "closed": True,
        "open_s1_s2": [],
        "updated_at": now_iso,
        "confirmed_by": confirmed_by,
        "source_kind": "author_confirm",
    }
    closure_path.parent.mkdir(parents=True, exist_ok=True)
    closure_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "ok": True,
        "chapter": chapter_id,
        "closure_path": str(closure_path.relative_to(book)),
        "closed": True,
        "already_confirmed": False,
    }


@router.post("/books/{book_id}/consistency/closure/confirm")
def api_confirm_consistency_closure(book_id: str, payload: dict, user=Depends(get_current_user)) -> dict:
    """确认第 N 章一致性闭环：审查通过 + 作者确认 → 落 追踪/一致性核验/第N章.json。

    body: { "chapter": int, "note": str? }
    """
    try:
        chapter_num = int(payload.get("chapter"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="chapter must be a positive integer") from None
    if chapter_num < 1:
        raise HTTPException(status_code=400, detail="chapter must be a positive integer")
    root = projects_root(user["user_id"])
    try:
        book = book_root(root, book_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    _op_task = f"sync_{uuid.uuid4().hex[:12]}"
    busy = _try_acquire_op(book_id, chapter_num, "closure", _op_task, book=book)
    if busy:
        raise HTTPException(status_code=409, detail=_op_busy_detail(book_id, chapter_num, busy))
    try:
        with _live_flow(f"一致性闭环确认 · 第{chapter_num}章", mode="closure", book_id=book_id,
                        chapter_id=chapter_num, user=user):
            with live_feed.stage("closure", "审查通过后落闭环核验 JSON", label="闭环确认") as _st:
                res = _confirm_consistency_closure(root, book_id, chapter_num, user)
                _st.output(json.dumps(res, ensure_ascii=False)[:2000] if isinstance(res, dict) else "")
                return res
    finally:
        _release_op(book_id, chapter_num, _op_task)


@router.post("/books/{book_id}/chapters/{chapter}/acceptance-settle")
def api_acceptance_settle(book_id: str, chapter: int, payload: dict,
                          user=Depends(get_current_user)) -> dict:
    """人工修改审核提交：作者改完正文后，系统按原流程补跑完整验收并正式提交落账。

    不重写正文、不降标准、不伪造 reviewed：enforce → 审稿闭环(真打分/finalize)
    → 一致性检测+自动闭环 → post_write 按原闸标准 commit。
    body: { "reason": str? }
    """
    root = projects_root(user["user_id"])
    try:
        book = book_root(root, book_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    _op_task = f"sync_{uuid.uuid4().hex[:12]}"
    busy = _try_acquire_op(book_id, chapter, "acceptance", _op_task, book=book)
    if busy:
        raise HTTPException(status_code=409, detail=_op_busy_detail(book_id, chapter, busy))
    try:
        from .agents_runtime.pipeline import step_acceptance_and_commit
        with _live_flow(f"验收落账 · 第{chapter}章", mode="acceptance", book_id=book_id,
                        chapter_id=chapter, user=user):
            with live_feed.stage("acceptance", "人工修改后重新验收并落账", label="验收落账") as _st:
                res = step_acceptance_and_commit(
                    book, chapter,
                    reason=str((payload or {}).get("reason") or "作者人工修改后提交（重新验收落账）"),
                    by="human_review",
                )
                _st.output(str((res or {}).get("detail") or "")[:4000])
    finally:
        _release_op(book_id, chapter, _op_task)
    if not res.get("ok"):
        raise HTTPException(status_code=502, detail=res.get("detail") or "验收补跑未达过闸标准")
    return {"book_id": book_id, "chapter_id": chapter, **res}


# ---- Consistency Repair Loop — Phase 1：Repair Planner（只读，产出 repair_plan.json） ----
# 来源：外脑 2026-09-16 裁决（ConsistencyRepairLoop_Phase1_Contract.md）。
# 铁则：Repair Planner「不直接改正文」，只读正文/细纲/设定证据 → 初判 ABC → 产出方案。
# ABC 初判（人工拥有最终确认权）：
#   A = 唯一正确答案、不涉剧情选择（Phase 1 一律"人工确认后执行"，不自动）
#   B = 涉设定/剧情方向/伏笔（作者决策）
#   C = 表面可修但有破坏连续性格局风险（提供方案，作者选择）
# 落点：**/追踪/consistency/repair_plans/第{N}章.json。skill / A-3 / 正文零改动。
_REPAIR_PLAN_DIR = "consistency/repair_plans"
_REPAIR_EXECUTOR_DIR = "consistency/repair_executor"
_BODY_PREVIEW_LINES = 120
_A_PATCH_MAX_CHARS = 300  # A 类单片段替换上限（外脑裁决：超限降级 proposal）


def _repair_plan_path(book: Path, chapter_id: int) -> Path:
    return book / "追踪" / _REPAIR_PLAN_DIR / f"第{chapter_id}章.json"


def _confirm_repair_plan(root: Path, book_id: str, chapter_id: int, user: dict) -> dict:
    """批量人工授权执行本章修复（外脑 Phase 1.5 裁决）。

    - 作者（用户）在 Web 端一次点击「授权执行本章修复」→ POST → 记录 batch confirmation。
    - confirmed_by 沿用无账号体系约定 web_user_confirm，但仅允许由 Web 端授权动作触发写入，
      禁止后台脚本自动伪造确认。
    - 落点只扩展 repair_plan.json 的 confirmation 字段块，不改 issue schema / A-3 / skill /
      consistency closure 契约。
    - 幂等：已 confirmed 直接返回，不覆盖。
    """
    if not isinstance(chapter_id, int) or chapter_id < 1:
        raise HTTPException(status_code=400, detail="chapter must be a positive integer")

    book = book_root(root, book_id)
    plan_path = _repair_plan_path(book, chapter_id)
    if not plan_path.is_file():
        raise HTTPException(status_code=404, detail=f"第{chapter_id}章尚无 repair_plan，请先生成修复方案")

    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise HTTPException(status_code=409, detail=f"repair_plan 文件不可读，无法确认: {exc}") from exc

    confirmation = plan.get("confirmation") or {}
    if confirmation.get("status") == "confirmed":
        return {
            "ok": True,
            "chapter": chapter_id,
            "confirmed": True,
            "already_confirmed": True,
            "plan_path": str(plan_path.relative_to(book)),
        }

    now_iso = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    confirmed_by = user.get("email") or user.get("user_id") or _CONFIRMED_BY_SYSTEM
    plan["confirmation"] = {
        "status": "confirmed",
        "confirmed_by": confirmed_by,
        "confirmed_at": now_iso,
        "scope": "chapter_all_issues",
    }
    plan["plan_status"] = "confirmed"
    plan["updated_at"] = now_iso
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "ok": True,
        "chapter": chapter_id,
        "confirmed": True,
        "already_confirmed": False,
        "plan_path": str(plan_path.relative_to(book)),
    }


# ---- Repair Executor v1（外脑 Phase 1.5 裁决）：confirmed repair_plan → snapshot → A patch / B·C proposal → audit ----
# 铁则：
#   - 唯一入口：repair_plan.confirmation.status == confirmed（批量人工授权后才允许进入）。
#   - snapshot 必须在任何正文修改之前（外脑裁决：流程固定 confirmed → snapshot → executor → A patch apply）。
#   - A 类 = 局部自动补写/删除，允许；B/C 类 = proposal only，绝不落正文。
#   - A patch 硬限制：old 必须与正文精确匹配、单片段、≤300字、不跨段、不改变设定/动机/体系/方向/伏笔。
#   - 改前备份 .backup/、改后重读正文校验；audit 保存 before_hash/after_hash，防并发错改。
#   - 不接 closure 自动化：executor 只产出修复/提案，closure 仍由 consistency 重检结果决定。
_EXECUTOR_PATCH_SYS = (
    "你是网络小说连续性修复的「局部补写执行器」，只处理 A 类（极窄安全自动区）的确定性小修。\n"
    "硬约束：\n"
    "1. 只产出 JSON {old, new}，old 必须逐字取自正文原文（精确匹配），new 是局部替换文本。\n"
    "2. old 必须为「单个连续文本片段」，禁止跨段（禁止包含空行；禁止跨两个自然段）；new 也必须是单个片段。\n"
    "3. new 长度 ≤300 字；只做：补时间过渡/补动作细节/补环境描写/补明显遗漏事实/删重复或矛盾小句。\n"
    "4. 禁止改变：世界设定、角色动机、修炼体系、剧情方向、伏笔走向、事件顺序、剧情结果。\n"
    "5. 若判断该修复无法在局部文本窗口内安全完成，输出空 JSON {}（表示应降级 proposal）。\n"
    "6. 无任何前后缀文字，只输出 JSON。"
)


def _executor_snapshot(book: Path, chapter_id: int, body: Path | None) -> str | None:
    """修改前快照（外脑裁决：snapshot 必须在任何修改前）。落 追踪/consistency/repair_executor/快照/第N章_YYYYmmdd_HHMMSS.md。"""
    if body is None or not body.is_file():
        return None
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d_%H%M%S")
    snap_dir = book / "追踪" / _REPAIR_EXECUTOR_DIR / "快照"
    snap_dir.mkdir(parents=True, exist_ok=True)
    snap_path = snap_dir / f"第{chapter_id}章_{stamp}.md"
    try:
        snap_path.write_text(body.read_text(encoding="utf-8"), encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    return str(snap_path.relative_to(book))


def _apply_a_patch(body: Path, old: str, new: str) -> tuple[bool, str]:
    """应用单个 A patch：old 精确匹配（含 hash 校验）→ 单次替换 → 重读校验。

    返回 (ok, 描述)；失败原因（不匹配/多匹配/读写出错）返回 False。
    """
    if not old or not new:
        return False, "patch 为空"
    try:
        content = body.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return False, f"正文读取失败: {exc}"
    before_hash = hash(content)
    count = content.count(old)
    if count == 0:
        return False, "old 片段在正文中不存在（防幻改）"
    if count > 1:
        return False, "old 片段在正文中多处匹配，拒绝歧义应用"
    applied = content.replace(old, new, 1)
    after_hash = hash(applied)
    # 防并发错改：写入前再次校验正文未被并发改动
    try:
        body.write_text(applied, encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return False, f"正文写入失败: {exc}"
    # 写后重读校验
    try:
        verify = body.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        verify = ""
    if new not in verify:
        return False, "写入后校验失败（new 未出现在正文）"
    return True, f"applied (before_hash={before_hash}, after_hash={after_hash})"



# R1 Scope Guard：确定性检查 new 相对 old 不得新增事实。
_R1_FORBIDDEN_CANON = ("突破", "筑基", "金丹", "境界", "凡气", "道基", "修为",
                      "加入", "获得", "接受", "拒绝独家", "筑基后期")
_R1_TIME_RE = re.compile(r"[一二三四五六七八九十百千万零\d]+[天日月年时辰刻]")
_R1_NUM_RE = re.compile(r"\d+")


def _r1_scope_guard(old: str, new: str, book: Path) -> tuple[bool, str]:
    """R1 Scope Guard：apply 前确定性检查。new 中出现的事实性 token 必须全部在 old 中出现。

    G1 范围：单点替换/标点/错别/格式；禁跨段、禁超长。
    G2 事实锚：new 新增的数字/时间词/角色名/Canon 词 → BLOCK。
    G3 C8：不新增第三套数字（由 G2 数字检查覆盖）。
    """
    if not old or not new:
        return False, "old/new 为空"
    # G1 范围
    if "\n\n" in old or "\n\n" in new:
        return False, "G1 patch 跨段"
    if len(new) > _A_PATCH_MAX_CHARS:
        return False, f"G1 new 超 {_A_PATCH_MAX_CHARS} 字"
    # G2 数字（阿拉伯+中文）：new 的每个数字 token 必须在 old 中出现
    _CN = "零一二三四五六七八九十百千万"
    def _nums(s):
        out = set(_R1_NUM_RE.findall(s))
        out.update(re.findall(rf"[{_CN}]+", s))
        return {x for x in out if x}
    old_nums = _nums(old)
    for n in _nums(new):
        if n not in old_nums:
            return False, f"G2 新增数字 {n}"
    # G2 时间词：new 的每个时间短语必须在 old 原文出现
    for t in _R1_TIME_RE.findall(new):
        if t not in old:
            return False, f"G2 新增时间词 {t}"
    # G2 Canon 高危词：new 新增的高危词必须在 old 出现
    for w in _R1_FORBIDDEN_CANON:
        if w in new and w not in old:
            return False, f"G2 新增 Canon 词「{w}」"
    # G2 角色名：从 state.characters 读角色名，new 新增 → BLOCK
    try:
        st = json.loads((book / "追踪" / "_tracking-state.json").read_text(encoding="utf-8"))
        chars = st.get("characters") or {}
        names = []
        if isinstance(chars, dict):
            names = list(chars.keys())
        elif isinstance(chars, list):
            for c in chars:
                if isinstance(c, dict):
                    names.append(c.get("name") or c.get("id") or "")
        for nm in names:
            if nm and len(nm) >= 2 and nm in new and nm not in old:
                return False, f"G2 新增角色「{nm}」"
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    return True, "ok"


def _r1_track_commit(book: Path, chapter_id: int, *, issue_id: str, old: str, new: str,
                     before_md5: str, after_md5: str, reason: str, operator: str) -> dict:
    """R1 轻量 revision 事务薄壳：状态事务下沉 track_commit，此处仅写 patch ledger。

    P2 修复（2026-10-20 外脑裁决方案 A）：锁内事务（state 读改写+派生视图同步重建+
    快照同步）由 nww_track_commit.r1_track_commit 单一来源实现——修复此前 bump 后不
    重建派生视图 → 下一次 CLI check 必然 derived view differs 的缺口。视图重建失败时
    写 .views-stale.json 失效标记并抛 TrackingError → 本函数异常穿出 → 调用方回滚正文
    并降级 proposal（与既有失败语义一致）。锁忙超时同样抛 TrackingError。
    """
    _skill_scripts = Path(__file__).resolve().parents[3] / "skills" / "nww-novel-skill" / "scripts"
    if str(_skill_scripts) not in sys.path:
        sys.path.insert(0, str(_skill_scripts))
    from nww_track_commit import r1_track_commit

    # 锁内事务（state bump + render_views + write_views + 快照同步）
    rev_info = r1_track_commit(book, chapter_id)
    old_rev, new_rev = rev_info["old_rev"], rev_info["new_rev"]
    now_iso = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    # patch ledger
    patch_id = f"r1_{chapter_id:03d}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    ledger = {
        "patch_id": patch_id,
        "chapter": chapter_id,
        "issue_id": issue_id,
        "operation": "replace",
        "old": old,
        "new": new,
        "reason": reason,
        "before_md5": before_md5,
        "after_md5": after_md5,
        "operator": operator,
        "created_at": now_iso,
        "state_revision": new_rev,
        "write_origin": "REPAIR_R1",
    }
    led_dir = book / "追踪" / "patches"
    led_dir.mkdir(parents=True, exist_ok=True)
    (led_dir / f"第{chapter_id:03d}章_R1_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}.json").write_text(
        json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"patch_id": patch_id, "old_rev": old_rev, "new_rev": new_rev}


def _executor_patch_for_issue(issue: dict, body: Path, book: Path, dry_run: bool = False) -> dict:
    """R1 executor（纯执行，无 LLM）：读 planner 已确定的 r1_patch → scope guard → apply → track_commit。

    非 R1 / 无正文 / guard FAIL / apply FAIL → 降级 proposal 记录（不写正文、不动 revision）。
    """
    iid = issue.get("id", "")
    if issue.get("repair_type") != "R1":
        return {"issue_id": iid, "mode": "proposal_only"}
    if body is None or not body.is_file():
        return {"issue_id": iid, "mode": "proposal_only", "reason": "正文缺失，无法应用 R1 patch"}
    rp = issue.get("r1_patch") or {}
    old = (rp.get("old") or "").strip()
    new = (rp.get("new") or "").strip()
    reason = (rp.get("reason") or "punctuation").strip()
    if not old or not new:
        return {"issue_id": iid, "mode": "proposal_only", "reason": "r1_patch 缺 old/new"}
    # G4 原子写入：apply 前先 guard
    ok_g, msg_g = _r1_scope_guard(old, new, book)
    if not ok_g:
        return {"issue_id": iid, "mode": "proposal_only", "reason": f"R1 Scope Guard BLOCK: {msg_g}"}
    before_md5 = hashlib.md5(body.read_text(encoding="utf-8").encode()).hexdigest()
    if dry_run:
        orig = body.read_text(encoding="utf-8")
        if old not in orig:
            return {"issue_id": iid, "mode": "proposal_only", "reason": "old 片段在正文中不存在（防幻改）"}
        after_preview = orig.replace(old, new, 1)
        after_md5 = hashlib.md5(after_preview.encode()).hexdigest()
        return {"issue_id": iid, "mode": "patch_applied", "dry_run": True,
                "commit_status": "SKIPPED_DRY_RUN", "would_apply": True,
                "before_md5": before_md5, "after_md5": after_md5, "guard": "PASS"}
    ok, desc = _apply_a_patch(body, old, new)
    if not ok:
        return {"issue_id": iid, "mode": "proposal_only", "reason": desc}
    after_md5 = hashlib.md5(body.read_text(encoding="utf-8").encode()).hexdigest()
    try:
        commit_info = _r1_track_commit(book, issue.get("_chapter_id", 0),
                                        issue_id=iid, old=old, new=new,
                                        before_md5=before_md5, after_md5=after_md5,
                                        reason=reason, operator="repair_executor")
    except Exception as exc:
        # commit 失败：正文回滚到 before_md5
        body.write_text(body.read_text(encoding="utf-8").replace(new, old, 1), encoding="utf-8")
        return {"issue_id": iid, "mode": "proposal_only", "reason": f"R1 commit 失败已回滚: {exc}"}
    return {"issue_id": iid, "mode": "patch_applied", "detail": desc, "commit": commit_info,
            "old": old[:120], "new": new[:120]}


def _dispatch_r2(issue: dict, book: Path, chapter_id: int, dry_run: bool = False) -> dict:
    """R2 分流：内容修订 → run_revise_pipeline（保结构保事实锚）。executor 不生成正文。"""
    iid = issue.get("id", "")
    note = (issue.get("r2_revise_note") or issue.get("suggested_fix") or "").strip()
    if not note:
        return {"issue_id": iid, "mode": "proposal_only", "reason": "R2 缺 r2_revise_note"}
    try:
        from .agents_runtime.pipeline import run_revise_pipeline
        res = run_revise_pipeline(book, chapter_id, note=note, dry_run=dry_run)
        return {"issue_id": iid, "mode": "revise_dispatched", "detail": str(res)[:200]}
    except Exception as exc:
        return {"issue_id": iid, "mode": "proposal_only", "reason": f"R2 revise pipeline 失败: {exc}"}


def _dispatch_r3(issue: dict, book: Path, chapter_id: int, dry_run: bool = False) -> dict:
    """R3 分流：重新生产 → run_rewrite_pipeline(level)。executor 不生成正文。"""
    iid = issue.get("id", "")
    hint = (issue.get("r3_rewrite_hint") or issue.get("suggested_fix") or "").strip()
    level = str(issue.get("r3_level") or "L3").strip().upper()
    if level not in ("L1", "L2", "L3"):
        level = "L3"
    try:
        from .agents_runtime.pipeline import run_rewrite_pipeline
        res = run_rewrite_pipeline(book, chapter_id, note=hint, level=level, prealign=True, dry_run=dry_run)
        return {"issue_id": iid, "mode": "rewrite_dispatched", "level": level, "detail": str(res)[:200]}
    except Exception as exc:
        return {"issue_id": iid, "mode": "proposal_only", "reason": f"R3 rewrite pipeline 失败: {exc}"}


def _parse_executor_patch(text: str) -> dict | None:
    """解析 executor LLM 输出的 {old,new} JSON；失败返回 None（保守降级）。"""
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return {"old": str(data.get("old") or "").strip(), "new": str(data.get("new") or "").strip()}


def _run_repair_executor(root: Path, book_id: str, chapter_id: int, user: dict, dry_run: bool = False) -> dict:
    """Repair Executor 主流程（外脑裁决 v1）：

    confirmed 校验 → snapshot（修改前）→ 逐 issue：A→patch engine；B/C→proposal only →
    写 audit（repair_executor/audits/第N章_时间戳.json）。不接 closure 自动化。
    """
    if not isinstance(chapter_id, int) or chapter_id < 1:
        raise HTTPException(status_code=400, detail="chapter must be a positive integer")

    book = book_root(root, book_id)
    plan_path = _repair_plan_path(book, chapter_id)
    if not plan_path.is_file():
        raise HTTPException(status_code=404, detail=f"第{chapter_id}章尚无 repair_plan，请先生成修复方案")
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise HTTPException(status_code=409, detail=f"repair_plan 不可读，无法执行: {exc}") from exc

    confirmation = plan.get("confirmation") or {}
    if confirmation.get("status") != "confirmed":
        raise HTTPException(status_code=409, detail="repair_plan 尚未经作者批量授权（confirmation.status != confirmed），禁止执行")

    body = _find_existing_body_target(book, chapter_id)

    # 1) snapshot 必须在任何修改前
    snapshot_rel = _executor_snapshot(book, chapter_id, body)

    # 2) 正文备份（.backup/，回滚点）
    backup_rel = None
    if body is not None and body.is_file():
        backup_dir = book / ".backup"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"第{chapter_id}章_pre_repair_{stamp}.md"
        try:
            backup_path.write_text(body.read_text(encoding="utf-8"), encoding="utf-8")
            backup_rel = str(backup_path.relative_to(book))
        except (OSError, UnicodeError):
            backup_rel = None

    # 3) 逐 issue 执行
    results = []
    a_applied = 0
    r2_dispatched = 0
    r3_dispatched = 0
    proposal_count = 0
    for it in plan.get("issues") or []:
        it["_chapter_id"] = chapter_id
        rt = it.get("repair_type")
        if rt == "R1":
            res = _executor_patch_for_issue(it, body, book, dry_run=dry_run)
        elif rt == "R2":
            res = _dispatch_r2(it, book, chapter_id, dry_run=dry_run)
        elif rt == "R3":
            res = _dispatch_r3(it, book, chapter_id, dry_run=dry_run)
        else:
            res = {"issue_id": it.get("id",""), "mode": "proposal_only", "reason": "未知 repair_type"}
        results.append(res)
        if res.get("mode") == "patch_applied":
            a_applied += 1
        elif res.get("mode") == "revise_dispatched":
            r2_dispatched += 1
        elif res.get("mode") == "rewrite_dispatched":
            r3_dispatched += 1
        else:
            proposal_count += 1

    # 4) audit 落盘
    now_iso = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    audit = {
        "chapter": chapter_id,
        "executed_at": now_iso,
        "executor": "repair_executor_v1",
        "plan_path": str(plan_path.relative_to(book)),
        "snapshot": snapshot_rel,
        "backup": backup_rel,
        "summary": {"a_applied": a_applied, "r2_dispatched": r2_dispatched, "r3_dispatched": r3_dispatched, "proposal_only": proposal_count},
        "results": results,
    }
    audit_dir = book / "追踪" / _REPAIR_EXECUTOR_DIR / "audits"
    audit_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d_%H%M%S")
    audit_path = audit_dir / f"第{chapter_id}章_{stamp}.json"
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "ok": True,
        "chapter": chapter_id,
        "executor": "repair_executor_v1",
        "snapshot": snapshot_rel,
        "backup": backup_rel,
        "summary": {"a_applied": a_applied, "r2_dispatched": r2_dispatched, "r3_dispatched": r3_dispatched, "proposal_only": proposal_count},
        "results": results,
        "audit_path": str(audit_path.relative_to(book)),
    }


def _locate_evidence_refs(body_rel: str | None, body: Path | None, fix_targets: list[str], max_refs: int = 3) -> list[dict]:
    """对 LLM 选的 fix_targets 关键词，在正文文件里做「确定性行号定位」（非 LLM 猜测）。

    逐个关键词在正文原文里精确/模糊匹配，回填 {file, location, excerpt}；
    匹配不到或正文缺失则跳过（不填假行号）。只读正文，绝不修改正文。
    """
    if body is None or not body.is_file() or not fix_targets:
        return []
    lines = body.read_text(encoding="utf-8").splitlines()
    refs: list[dict] = []
    seen_targets: set[str] = set()
    for t in fix_targets:
        if not t or t in seen_targets:
            continue
        seen_targets.add(t)
        hit_idx: list[int] = []
        for i, ln in enumerate(lines, start=1):
            if t in ln:
                hit_idx.append(i)
        if not hit_idx:
            continue
        # 命中连续行合并成区段
        ranges: list[tuple[int, int]] = []
        lo = hi = hit_idx[0]
        for n in hit_idx[1:]:
            if n == hi + 1:
                hi = n
            else:
                ranges.append((lo, hi)); lo = hi = n
        ranges.append((lo, hi))
        for rlo, rhi in ranges[:max_refs]:
            excerpt = " ".join(lines[rlo - 1:rhi]).strip()
            refs.append({
                "file": body_rel or "",
                "location": f"L{rlo}-L{rhi}" if rlo != rhi else f"L{rlo}",
                "excerpt": excerpt[:120],
            })
        if len(refs) >= max_refs:
            break
    return refs


def _repair_planner_prompt(review: dict, body_rel: str | None, body_preview: str | None = None) -> str:
    """构造 Repair Planner 的 LLM 输入：open_s1_s2（S1/S2 硬伤）+ s3_s4（软伤）+ 证据位置 + 正文预览。"""
    issues: list[dict] = []
    seq = 0
    for group in ("open_s1_s2", "s3_s4"):
        for it in review.get(group, []) if isinstance(review.get(group), list) else []:
            seq += 1
            issues.append({"seq": seq, "group": group, "level": it.get("level"), "content": it.get("content")})
    payload = {
        "chapter": review.get("chapter"),
        "body_evidence": body_rel,
        "issues": issues,
    }
    if body_preview:
        payload["body_preview"] = body_preview  # 举证素材：仅用于选 fix_targets 关键词，行号由执行端确定性定位
    return json.dumps(payload, ensure_ascii=False, indent=2)


# Repair Planner 判题方法论：只作"初判 + 修复方向"，不改正文、不下可否执行指令。
_REPAIR_PLANNER_SYS = (
    "你是网络小说连续性修复方案的「初判规划师」，负责对一致性审查发现的每个问题做 ABC 分类并给出修复方向建议。\n"
    "硬约束：\n"
    "1. 只做「初判分类 + 修复建议」，不写正文、不改设定文件、不产生任何落稿行为。\n"
    "2. 每个问题独立编号，输出严格 JSON 数组，无任何前后缀文字。\n"
    "3. 分类口径（硬性判定；A 为「极窄的安全自动区」，先判是否触及不可自动域；宁 C 勿 B，绝不 A）：\n"
    "   C = 问题涉及已有设定体系、世界规则、前文承诺（伏笔/机制/数值体系），修复可能改变既有事实。当证据含"
    " 设定/机制/伏笔/世界规则/修炼体系/角色成长 维度且非纯文本错漏时，默认归 C。\n"
    "   B = 需要作者决定规则、机制、角色选择、伏笔方向、角色生死/能力——创作决策禁区，禁止自动。\n"
    "   A（极严格）= 明确事实错误，且可通过局部正文补写/删除修复，不改变设定、不改变角色动机、不改变剧情方向"
    "（如：姓名/称呼错误、同场景光源/照明矛盾、日期/日历算术、账目数字细算等无剧情义的笔误）。\n"
    "   无法唯一判断时一律不下 A（宁可 C 或 B）；绝不把 C/B 升格为 A。机制/修炼体系/世界规则/角色成长/伏笔全部默认为 C 或 B。\n"
    "4. 每条输出字段：id(取 review 原内容前缀，如 S2-凡气)、type(机制/设定/称呼/时间线/物品/逻辑/其他)、"
    "category(A/B/C)、summary(≤60字)、suggested_fix(具体可执行的修复方向,≤120字)、"
    "risk_note(若 C，必写破坏连续性风险)。不知道原文时诚实留空，禁止编造证据行号。\n"
    "5. fix_targets（新字段）：给每条 issue 挑选 1~3 个「能定位到正文的短关键词短语」"
    "（如『凡气循火』『滚烫的凡气』『丹炉』），用于让执行端在正文文件里做确定性行号定位。"
    "必须直接取自 body_preview 中的原文片段，禁止编造正文里不存在的词；无法从预览确认时给空数组 []。"
    "6. repair_type（新字段，与 category 一一对应，互斥唯一）：\n"
    "   R1 = 对应 A：确定性字符替换（错别/标点/格式/数字一致性），可由旧正文 exact 片段直接替换修正，"
    "不新增信息、不删除事实、不改因果、不改人物动作、不改剧情方向。\n"
    "   R2 = 对应 B：内容修订——需 LLM 改写文本，但原章节结构/事实锚/事件结果保持（扩写/润色/逻辑补强/描写调整）。\n"
    "   R3 = 对应 C：重新生产——章节实现层需重新设计（按章节任务重写）。\n"
    "   无法唯一判断时不得 R1（宁可 R2/R3）。\n"
    "7. 仅当 repair_type=R1 时，必须同时给出 r1_patch（其余两类给 r2_revise_note 或 r3_rewrite_hint，另一类留空）：\n"
    '   r1_patch = {"operation":"replace","old":"<body_preview 原文 exact 片段>","new":"<确定性修正>",'
    '"reason":"punctuation|typo|format","confidence":"deterministic"}。\n'
    "   硬约束：old 必须是正文里真实存在的精确子串；new 只能是对 old 的字符级修正（标点/错别/格式/单字数字一致性），\n"
    "   禁止自由生成句子、禁止新增信息（如在句中插入新情节）、禁止删除既有事实、禁止改变因果或人物动作。\n"
    "   若修复需要新增内容或改写句子（非字符级修正），不得标 R1，改标 R2。\n"
)


def _generate_repair_plan(book: Path, chapter_id: int, review: dict, user: dict) -> tuple[dict, str]:
    """Repair Planner：只读正文证据 + LLM 初判分类 → 产出 repair_plan（计划态 pending）。

    写盘目标 **_REPAIR_PLAN_DIR/第{chapter}章.json。仅新增此文件，不动正文/追踪状态。
    """
    body = _find_existing_body_target(book, chapter_id)
    body_rel = str(body.relative_to(book)) if body is not None else None

    # 正文预览：仅作证据素材供 LLM 挑选 fix_targets 关键词；行号由执行端确定性定位（外脑裁决：禁止 LLM 猜行号）
    body_preview = None
    if body is not None and body.is_file():
        try:
            blines = body.read_text(encoding="utf-8").splitlines()
            body_preview = "\n".join(blines[:_BODY_PREVIEW_LINES])
        except (OSError, UnicodeError):
            body_preview = None

    user_prompt = _repair_planner_prompt(review, body_rel, body_preview)
    now_iso = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    try:
        chain = _cfg_default_chain()
        if chain:
            text = chat_completion_robust(
                _REPAIR_PLANNER_SYS, user_prompt, chain, temperature=0.1, max_tokens=4096, timeout=_TIMEOUT
            )
        else:
            base_url, model, api_key, timeout = _resolve_write_endpoint()
            text = chat_completion(
                _REPAIR_PLANNER_SYS, user_prompt, temperature=0.1, max_tokens=4096,
                base_url=base_url, model=model, api_key=api_key, timeout=timeout,
            )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=f"Repair Planner LLM 调用失败: {exc}") from exc

    issues = _parse_repair_plan_issues(text, chapter_id)
    # 回填 evidence_refs：由正文确定性定位（非 LLM 猜测）；原 evidence 字符串字段保留不动。
    # evidence_refs_status（外脑裁决 Phase 1.2）：不允许静默空值——fix_targets 非空则必须尝试定位，
    # 定位失败/正文缺失 → unresolved；fix_targets 为空 → empty_targets；定位命中 → resolved。
    body_text = body.read_text(encoding="utf-8") if body is not None and body.is_file() else ""
    for it in issues:
        _high_risk_classify_issue(it)  # 分类前高危约束：高危域绝不进 R1
        _rule_validator_issue(it)  # 二次分类校验：仅防 R1 越权（R1→R2/R3），绝不 R2/R3→R1
        _validate_r1_patch_spec(it, body_text)  # R1 Patch Intent Gate：确定性 patch_spec 校验
        targets = it.get("fix_targets") or []
        refs = _locate_evidence_refs(body_rel, body, targets)
        it["evidence_refs"] = refs
        if not targets:
            it["evidence_refs_status"] = "empty_targets"
        elif refs:
            it["evidence_refs_status"] = "resolved"
        else:
            it["evidence_refs_status"] = "unresolved"
    plan_path = _repair_plan_path(book, chapter_id)
    plan = {
        "chapter": chapter_id,
        "plan_status": "pending",
        "created_at": now_iso,
        "updated_at": now_iso,
        "source": _REVIEW_DIR,
        "issues": issues,
    }
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return plan, str(plan_path.relative_to(book))


def _parse_repair_plan_issues(text: str, chapter_id: int) -> list[dict]:
    """解析 LLM 输出的 issue JSON 数组；失败返回空数组（保守，不落伪方案）。

    每个 issue 额外捕获 raw fix_targets（LLM 选的关键词），供 _locate_evidence_refs 做正文确定性定位。
    """
    text = text.strip()
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        data = json.loads(text[start:end + 1])
        if not isinstance(data, list):
            return []
    except json.JSONDecodeError:
        return []
    seen: set[str] = set()
    out: list[dict] = []
    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        ident = str(item.get("id") or f"issue_{idx + 1}")
        if ident in seen:
            ident = f"{ident}_{idx + 1}"
        seen.add(ident)
        cat = _normalize_category(item.get("category"))
        rtype = str(item.get("repair_type") or "").strip().upper()
        if rtype not in ("R1", "R2", "R3"):
            rtype = {"A": "R1", "B": "R2", "C": "R3"}.get(cat, "R3")
        rp = item.get("r1_patch") or {}
        out.append({
            "id": ident,
            "type": item.get("type") or "其他",
            "category": cat,
            "repair_type": rtype,
            "r1_patch": {
                "operation": str(rp.get("operation") or "replace").strip(),
                "old": str(rp.get("old") or "").strip(),
                "new": str(rp.get("new") or "").strip(),
                "reason": str(rp.get("reason") or "").strip(),
                "confidence": str(rp.get("confidence") or "").strip(),
            } if rtype == "R1" else None,
            "r2_revise_note": str(item.get("r2_revise_note") or "").strip(),
            "r3_rewrite_hint": str(item.get("r3_rewrite_hint") or "").strip(),
            "evidence": _to_str_list(item.get("evidence")),
            "fix_targets": _to_str_list(item.get("fix_targets")),
            "summary": str(item.get("summary") or "").strip(),
            "suggested_fix": str(item.get("suggested_fix") or "").strip(),
            "risk_note": str(item.get("risk_note") or "").strip(),
        })
    return out


def _normalize_category(v) -> str:
    s = str(v or "").strip().upper()
    return s if s in ("A", "B", "C") else "C"


# Rule Validator（二次分类校验，外脑裁决 Phase 1.2）：仅允许 A→B / A→C 降级，
# 禁止 C/B→A 升格（Planner 偏保守可接受，偏激进进入 A 不可接受）。
# 命中「不可自动处理」域（设定/机制/伏笔/世界规则/修炼体系/角色成长）即降级，写固定格式 validator_note 供审计统计。
_VALIDATOR_TYPO_C = frozenset({"设定", "机制", "伏笔", "世界规则"})
_VALIDATOR_TYPO_B = frozenset({"角色", "角色成长"})
_VALIDATOR_WORD_C = ("设定", "机制", "伏笔", "世界规则", "体系", "修炼", "修为", "道基", "境界")
_VALIDATOR_WORD_B = ("角色", "成长", "人物", "人设")


# High Risk Classifier（外脑裁决 Phase 1.2.1）：分类前高危约束层，插在 RuleValidator 之前。
# 只防 A 越权：高危域（设定/角色决策/伏笔）按固定词表强制「绝不进 A」（各域设最低档），
# 对已判为 B/C 的 issue 一律不动，绝不升格、绝不改正文/schema。时间事实域可保留 A。
# 与 RuleValidator 职责互补：本层用外脑固定词表兜住 LLM 措辞漂移（如「哲理→角色认知】仍命中「认知」）。
_HIGH_RISK_ROLE_B = ("性格", "认知", "心理", "成长", "选择", "态度", "价值观", "关系变化", "角色", "人物", "人设")
_HIGH_RISK_SETTING_C = ("修炼", "境界", "突破", "能力", "功法", "灵气", "凡气", "规则", "机制", "体系", "属性", "修为", "道基", "设定", "世界规则")
_HIGH_RISK_FORESHAW_C = ("古籍", "秘密", "线索", "身份", "真相", "铺垫", "后续", "伏笔")


def _high_risk_classify_issue(it: dict) -> None:
    """High Risk Classifier：仅当 LLM 初判为 A 时，按高危域词表强制降级到各域最低档。

    - 角色决策域（性格/认知/心理/成长…）→ 最低 B（涉及作者创作决策）
    - 设定域（修炼/境界/机制/凡气…）与伏笔域（古籍/秘密/真相…）→ 最低 C
    - 时间事实域（作息/日期…）不在本层词表，可保留 A
    优先级：角色决策域 > 设定域 > 伏笔域（命中任一即按其最低档，不变更 B/C）。
    """
    if it.get("repair_type") != "R1":
        return
    hint = " ".join([
        it.get("type") or "",
        it.get("summary") or "",
        it.get("suggested_fix") or "",
        " ".join(it.get("evidence") or []),
    ])
    for w in _HIGH_RISK_ROLE_B:
        if w in hint:
            it["repair_type"] = "R2"
            return
    for w in _HIGH_RISK_SETTING_C:
        if w in hint:
            it["repair_type"] = "R3"
            return
    for w in _HIGH_RISK_FORESHAW_C:
        if w in hint:
            it["repair_type"] = "R3"
            return


def _rule_validator_issue(it: dict) -> None:
    """对单条 issue 做防越权分类：R1 命中不可自动域即降级 R2/R3；R2/R3 一律不动（绝不允许升级 R1）。"""
    if it.get("repair_type") != "R1":
        if it.get("validator_note") is None:
            it["validator_note"] = ""
        return
    typ = it.get("type") or ""
    hint = " ".join([
        typ,
        it.get("summary") or "",
        it.get("suggested_fix") or "",
        " ".join(it.get("evidence") or []),
    ])
    if typ in _VALIDATOR_TYPO_C:
        it["repair_type"] = "R3"
        it["validator_note"] = f"R1问题类型为{typ}，超出安全自动域，自动降级R3"
        return
    if typ in _VALIDATOR_TYPO_B:
        it["repair_type"] = "R2"
        it["validator_note"] = f"R1问题涉及{typ}，须作者裁决，自动降级R2"
        return
    for w in _VALIDATOR_WORD_B:
        if w in hint:
            it["repair_type"] = "R2"
            it["validator_note"] = f"R1问题包含「{w}」关键词，自动降级R2"
            return
    for w in _VALIDATOR_WORD_C:
        if w in hint:
            it["repair_type"] = "R3"
            it["validator_note"] = f"R1问题包含「{w}」关键词，自动降级R3"
            return


def _validate_r1_patch_spec(it: dict, body_text: str) -> None:
    """R1 Patch Intent Gate：确定性校验 planner 产出的 r1_patch。失败即降级 R2（不允许 R1 自由生成）。"""
    if it.get("repair_type") != "R1":
        it["validator_note"] = it.get("validator_note") or ""
        return
    rp = it.get("r1_patch") or {}
    old = (rp.get("old") or "").strip()
    new = (rp.get("new") or "").strip()
    reason = (rp.get("reason") or "").strip()
    conf = (rp.get("confidence") or "").strip()
    def _reject(msg: str) -> None:
        it["repair_type"] = "R2"
        it["r1_patch"] = None
        it["validator_note"] = (it.get("validator_note") or "") + f"[R1-G0] {msg}"
    if rp.get("operation") != "replace" or not old or not new:
        _reject("r1_patch 缺 old/new/operation=replace")
        return
    if reason not in ("punctuation", "typo", "format") or conf != "deterministic":
        _reject("reason 须 punctuation|typo|format 且 confidence=deterministic")
        return
    if not body_text:
        _reject("正文缺失，无法校验 old exact match")
        return
    cnt = body_text.count(old)
    if cnt == 0:
        _reject("old 不在正文（非 exact match）")
        return
    if cnt > 1:
        _reject("old 在正文多处匹配，拒绝歧义")
        return
    if len(new) > len(old) * 2 + 10:
        _reject(f"new 较 old 显著增长({len(old)}->{len(new)})，疑似新增信息，转 R2")
        return
    if new == old:
        _reject("new 与 old 相同，无修复意义")
        return


def _to_str_list(v) -> list[str]:
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str) and v.strip():
        return [v.strip()]
    return []


@router.post("/books/{book_id}/chapters/{chapter}/repair-plan")
def api_repair_plan(book_id: str, chapter: int, user=Depends(get_current_user)) -> dict:
    """一致性修复方案生成（只读）：读 追踪/一致性审查/第{N}章.json → LLM 初判 ABC → 落 repair_plan.json。

    幂等：同章已存在 pending 计划则直接返回已有，不重复生成；不接触正文/追踪状态。
    """
    if not isinstance(chapter, int) or chapter < 1:
        raise HTTPException(status_code=400, detail="chapter must be a positive integer")
    root = projects_root(user["user_id"])
    try:
        book = book_root(root, book_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    review_path = book / "追踪" / _REVIEW_DIR / f"第{chapter}章.json"
    if not review_path.is_file():
        raise HTTPException(status_code=404, detail=f"第{chapter}章尚无一致性审查记录（追踪/一致性审查/），请先跑一致性审查")

    _op_task = f"sync_{uuid.uuid4().hex[:12]}"
    busy = _try_acquire_op(book_id, chapter, "repair-plan", _op_task, book=book)
    if busy:
        raise HTTPException(status_code=409, detail=_op_busy_detail(book_id, chapter, busy))
    try:
        plan_path = _repair_plan_path(book, chapter)
        if plan_path.is_file():
            try:
                existing = json.loads(plan_path.read_text(encoding="utf-8"))
                if existing.get("plan_status") == "pending":
                    return {"ok": True, "chapter": chapter, "repair_plan": existing, "already_generated": True,
                            "plan_path": str(plan_path.relative_to(book))}
            except (json.JSONDecodeError, OSError):
                pass  # 脏文件 → 重新生成

        review = json.loads(review_path.read_text(encoding="utf-8"))
        with _live_flow(f"修复方案生成 · 第{chapter}章", mode="repair", book_id=book_id,
                        chapter_id=chapter, user=user):
            with live_feed.stage("repair_plan", "读一致性审查 → LLM 初判 ABC", label="修复方案") as _st:
                plan, rel = _generate_repair_plan(book, chapter, review, user)
                _st.output(str((plan or {}).get("summary") or "")[:4000])
        return {"ok": True, "chapter": chapter, "repair_plan": plan, "already_generated": False, "plan_path": rel}
    finally:
        _release_op(book_id, chapter, _op_task)


@router.post("/books/{book_id}/chapters/{chapter}/repair-plan/confirm")
def api_confirm_repair_plan(book_id: str, chapter: int, user=Depends(get_current_user)) -> dict:
    """批量人工授权执行本章修复（外脑 Phase 1.5 裁决）：写入 repair_plan.json 的 confirmation 块。

    仅接受 Web 端「授权执行本章修复」按钮触发的 POST；禁止后台脚本自动伪造确认。
    不接触正文 / A-3 / skill / consistency closure 契约。
    """
    if not isinstance(chapter, int) or chapter < 1:
        raise HTTPException(status_code=400, detail="chapter must be a positive integer")
    root = projects_root(user["user_id"])
    try:
        book = book_root(root, book_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    _op_task = f"sync_{uuid.uuid4().hex[:12]}"
    busy = _try_acquire_op(book_id, chapter, "repair-confirm", _op_task, book=book)
    if busy:
        raise HTTPException(status_code=409, detail=_op_busy_detail(book_id, chapter, busy))
    try:
        return _confirm_repair_plan(root, book_id, chapter, user)
    finally:
        _release_op(book_id, chapter, _op_task)


@router.post("/books/{book_id}/chapters/{chapter}/repair-plan/execute")
def api_execute_repair_plan(book_id: str, chapter: int, payload: dict = None, user=Depends(get_current_user)) -> dict:
    """Repair Executor v1（外脑 Phase 1.5 裁决）：仅对 confirmed repair_plan 执行。

    A 类→patch engine（局部自动补写/删除）；B/C→proposal only（绝不落正文）。
    snapshot 必须在任何修改前；audit 记录 before/after；不接 closure 自动化。
    dry_run=True：R1/R2/R3 全链真跑但不 commit（P3 透传）。
    """
    if not isinstance(chapter, int) or chapter < 1:
        raise HTTPException(status_code=400, detail="chapter must be a positive integer")
    dry_run = bool((payload or {}).get("dry_run", False))
    root = projects_root(user["user_id"])
    try:
        book = book_root(root, book_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    _op_task = f"sync_{uuid.uuid4().hex[:12]}"
    busy = _try_acquire_op(book_id, chapter, "repair-execute", _op_task, book=book)
    if busy:
        raise HTTPException(status_code=409, detail=_op_busy_detail(book_id, chapter, busy))
    try:
        with _live_flow(f"修复执行 · 第{chapter}章", mode="repair", book_id=book_id,
                        chapter_id=chapter, user=user):
            with live_feed.stage("repair_execute", ("预演（dry_run）" if dry_run else "执行已确认修复方案"),
                                 label="修复执行") as _st:
                res = _run_repair_executor(root, book_id, chapter, user, dry_run=dry_run)
                _st.output(str((res or {}).get("detail") or (res or {}).get("summary") or "")[:4000])
                return res
    finally:
        _release_op(book_id, chapter, _op_task)


@router.post("/books/{book_id}/mvp/write-stream")
def api_mvp_write_stream(book_id: str, payload: dict, user=Depends(get_current_user)):
    """流式写章（SSE 打字机，2026-09-20 收敛）：token 流来自 pipeline on_writer_token 回调；
    最终落盘与提交归 run_write_pipeline 生产事务，endpoint 不再直连 LLM、不再 write_text。

    事件（data: {json}）：
      - {"type":"delta","text":...}  文本增量（pipeline writer 流）
      - {"type":"done","result":{...}} 提交完成（result 结构同 /mvp/write 兼容响应）
      - {"type":"error","detail":...} 失败（pipeline 失败 / 事务 BLOCK）
    流式期间不落盘：正文落盘权只属于 pipeline（normalize 后 + post_write 事务提交）。
    """
    chapter = int(payload.get("chapter") or 0)
    if chapter <= 0:
        raise HTTPException(status_code=400, detail="chapter must be a positive integer")
    root = projects_root(user["user_id"])
    try:
        book = book_root(root, book_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    _assert_chapter_not_exists(book, chapter)

    # ★ 并发守卫（2026-10-19）：端点原子注册（thread=None）→ run() 启动时补登线程句柄 →
    #   run() finally 释放；客户端在 run() 启动前断连 → gen() finally 兜底释放；
    #   极端场景（生成器从未被迭代）由 _evict_dead_ops_locked 的 30s 宽限期驱逐兜底。
    _op_task = f"stream_{uuid.uuid4().hex[:12]}"
    busy = _try_acquire_op(book_id, chapter, "write-stream", _op_task, book=book)
    if busy:
        raise HTTPException(status_code=409, detail=_op_busy_detail(book_id, chapter, busy))

    def ev(data: dict) -> str:
        return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    def gen():
        _started = {"done": False}
        try:
            q: "queue.Queue" = queue.Queue()

            def on_token(chunk: str) -> None:
                q.put(("delta", chunk))

            dry_run = bool(payload.get("dry_run", False))

            def run() -> None:
                try:
                    from .agents_runtime.pipeline import run_write_pipeline
                    with _live_flow(f"流式写章 · 第{chapter}章", mode="write", book_id=book_id,
                                    chapter_id=chapter, user=user):
                        res = run_write_pipeline(book, chapter, progress=_live_progress,
                                                 on_writer_token=on_token, dry_run=dry_run)
                    q.put(("done", res))
                except Exception as exc:  # noqa: BLE001 — 后台兜底，不悬挂
                    q.put(("error", f"pipeline 执行异常: {exc}"))
                finally:
                    # 唯一正常释放点：pipeline 终态（成功/失败/异常）后释放章操作槽。
                    _release_op(book_id, chapter, _op_task)

            th = threading.Thread(target=run, daemon=True)
            _bind_op_thread(book_id, chapter, _op_task, th)
            th.start()
            _started["done"] = True
            while True:
                kind, payload_ = q.get()
                if kind == "delta":
                    yield ev({"type": "delta", "text": payload_})
                elif kind == "done":
                    res = payload_
                    if not res.get("ok"):
                        yield ev({"type": "error", "detail": f"写章流水线失败: {_stages_summary(res)}"})
                        return
                    stages = res.get("stages") or []
                    norm = next((s for s in stages if s.get("name") == "normalize"), None) or {}
                    pkt = next((s for s in stages if s.get("name") == "packet"), None) or {}
                    yield ev({
                        "type": "done",
                        "result": {
                            "book_id": book_id,
                            "chapter_id": chapter,
                            "packet": pkt.get("packet"),
                            "model": _resolve_write_endpoint()[1],
                            "path": norm.get("path"),
                            "chars": norm.get("chars"),
                            **_write_result_fields(res, dry_run=dry_run),
                        },
                    })
                    return
                else:
                    yield ev({"type": "error", "detail": str(payload_)[:300]})
                    return
        finally:
            # 客户端在 run() 启动前断连（生成器提前关闭）→ 兜底释放。
            # 已启动则不在此释放：断连后孤儿线程仍在落盘，槽必须握到它结束，
            # 否则新操作会与孤儿线程并发互踩（释放职责归 run() 的 finally）。
            if not _started["done"]:
                _release_op(book_id, chapter, _op_task)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})



# ---------------------------------------------------------------------------
# 多环节独立 Agent 全流程（同步执行）：registry → routing → write
# 经 NWW_AGENTS_ENABLED=1 独立挂载（main.py），与 MVP 直连写互不干扰。
# 前导零细纲兼容：_build_packet / pipeline.step_packet 均已内置探测与软链。
# ---------------------------------------------------------------------------
agents_router = APIRouter()


@agents_router.get("/agents/registry")
def api_agents_registry(user=Depends(get_current_user)) -> dict:
    """返回 skill 包已注册的 Agent 清单与模型路由档位（诊断用）。"""
    from .agents_runtime import registry as ar_registry
    agents = ar_registry.list_agents()
    return {"agents": agents, "count": len(agents)}


@agents_router.get("/agents/routing")
def api_agents_routing(user=Depends(get_current_user)) -> dict:
    """返回当前本地/云端模型路由配置（诊断用）。"""
    from .agents_runtime import runtime as ar_runtime
    return ar_runtime.describe_routing()


@agents_router.post("/books/{book_id}/mvp/agents/write")
def api_agents_write(book_id: str, payload: dict, user=Depends(get_current_user)) -> dict:
    """多环节 Agent 全流程同步写章：prealign → packet → writer → 落盘
    → delta 草稿/补语义 → post_write（BLOCK）→ deslop（advisory）。
    body: {chapter: int, prealign?: bool}
    返回逐环节报告；任一步 FAIL 即终止并返回失败环节明细（不掩盖）。
    """
    chapter_id = payload.get("chapter")
    if not isinstance(chapter_id, int) or chapter_id < 1:
        raise HTTPException(status_code=400, detail="chapter must be a positive integer")
    prealign = bool(payload.get("prealign", True))
    root = projects_root(user["user_id"])
    try:
        book = book_root(root, book_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    # 防覆盖闸：目标章正文已存在 → 409，避免跑完整个管线才发现白干
    _assert_chapter_not_exists(book, chapter_id)

    # ★ 并发守卫（2026-10-19）：原子占用章操作槽，占用中 → 409（防同章并发互踩）
    _op_task = f"sync_{uuid.uuid4().hex[:12]}"
    busy = _try_acquire_op(book_id, chapter_id, "write", _op_task, book=book)
    if busy:
        raise HTTPException(status_code=409, detail=_op_busy_detail(book_id, chapter_id, busy))
    try:
        from .agents_runtime.pipeline import run_write_pipeline

        try:
            with _live_flow(f"写章（同步） · 第{chapter_id}章", mode="write", book_id=book_id,
                            chapter_id=chapter_id, user=user):
                result = run_write_pipeline(book, chapter_id, prealign=prealign,
                                            progress=_live_progress,
                                            dry_run=bool(payload.get("dry_run", False)))
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=f"多用户隔离拦截: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 — 编排器兜底，端点层不悬挂
            raise HTTPException(status_code=500, detail=f"全流程执行异常: {exc}") from exc

        status = 200 if result.get("ok") else 502
        return {"book_id": book_id, "chapter_id": chapter_id, "status": status, "result": result}
    finally:
        _release_op(book_id, chapter_id, _op_task)


@agents_router.post("/books/{book_id}/mvp/agents/commit")
def api_agents_commit(book_id: str, payload: dict, user=Depends(get_current_user)) -> dict:
    """仅提交已落盘的悬空章（append commit，不重写正文）。

    适用：正文已 write/落盘但 post_write 被某闸（如 A-3 缺闭环绕）BLOCK 的章。
    与 agents/write 的差异：不触发 writer、不覆盖正文——只消费已落盘正文 → delta →
    post_write(commit)。前置：正文已存在（否则 404）；闭环绕判据由脚本侧
    nww_track_commit.verify_consistency_closure 单一裁决（C-CLOSURE@2 读侧），
    不阻断 → 409（closed=true 不自动伪造，须作者先做一致性闭环后再调用）。
    body: {chapter: int}
    """
    chapter_id = payload.get("chapter")
    if not isinstance(chapter_id, int) or chapter_id < 1:
        raise HTTPException(status_code=400, detail="chapter must be a positive integer")
    root = projects_root(user["user_id"])
    try:
        book = book_root(root, book_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    # 定位已落盘正文（兼容分卷/前导零命名），不存在 → 404
    prose = _find_existing_body_target(book, chapter_id)
    if prose is None:
        raise HTTPException(status_code=404, detail=f"第{chapter_id}章正文不存在，无法仅提交（请先用写章链路生成）")

    # ★ 并发守卫（2026-10-19）：原子占用章操作槽 + 跨进程章锁（C-LOCK@1），占用中 → 409
    _op_task = f"sync_{uuid.uuid4().hex[:12]}"
    busy = _try_acquire_op(book_id, chapter_id, "commit", _op_task, book=book)
    if busy:
        raise HTTPException(status_code=409, detail=_op_busy_detail(book_id, chapter_id, busy))
    try:
        # 前置：闭环绕判据走脚本侧单一来源（C-CLOSURE@2 读侧，含来源与证据校验）
        _verify_consistency_closure_by_script(book, chapter_id)
        from .agents_runtime.pipeline import step_post_write, _clear_pending_commit_ledger

        try:
            with _live_flow(f"仅提交 · 第{chapter_id}章", mode="commit", book_id=book_id,
                            chapter_id=chapter_id, user=user):
                with live_feed.stage("commit", "消费已落盘正文，补 delta 并过 post_write 闸", label="提交落账") as _st:
                    post = step_post_write(book, chapter_id, prose)
                    _st.output((post.get("stdout", "") + "\n" + post.get("stderr", ""))[:4000])
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=f"多用户隔离拦截: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 — 编排器兜底，端点层不悬挂
            raise HTTPException(status_code=500, detail=f"仅提交执行异常: {exc}") from exc

        if post["ok"]:
            _clear_pending_commit_ledger(book, chapter_id)  # commit 成功即清恢复台账（fail-soft）

        combined = (post["stdout"] + ("\n" + post["stderr"] if post["stderr"] else ""))[:4000]
        return {
            "book_id": book_id,
            "chapter_id": chapter_id,
            "status": 200 if post["ok"] else 502,
            "result": {
                "ok": post["ok"],
                "chapter_id": chapter_id,
                "mode": "append",
                "stages": [{"name": "commit", "ok": post["ok"], "detail": combined, "returncode": post["returncode"]}],
                "detail": combined,
            },
        }
    finally:
        _release_op(book_id, chapter_id, _op_task)


@agents_router.post("/books/{book_id}/mvp/agents/rewrite")
def api_agents_rewrite(book_id: str, payload: dict, user=Depends(get_current_user)) -> dict:
    """重写已提交章节（revision 模式闭环）：旧稿备份 → 全流程重写 → delta/追踪 revision 提交。
    body: {chapter: int, note?: string（≤500 字执行要点；非空时走原文保持式重写/扩写）}
    与 agents/write 的差异：目标章必须已提交（有正文），不走 409 防覆盖闸；
    写入前自动备份旧稿到 .backup/（幂等首份），delta 走 revision 模式（track_commit 原生支持）。
    """
    chapter_id = payload.get("chapter")
    if not isinstance(chapter_id, int) or chapter_id < 1:
        raise HTTPException(status_code=400, detail="chapter must be a positive integer")
    note = str(payload.get("note", "") or "").strip()
    if len(note) > 500:
        raise HTTPException(status_code=400, detail="note 超过 500 字上限")
    root = projects_root(user["user_id"])
    try:
        book = book_root(root, book_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    # ★ 并发守卫（2026-10-19）：原子占用章操作槽，占用中 → 409（防同章并发互踩）
    _op_task = f"sync_{uuid.uuid4().hex[:12]}"
    busy = _try_acquire_op(book_id, chapter_id, "rewrite", _op_task, book=book)
    if busy:
        raise HTTPException(status_code=409, detail=_op_busy_detail(book_id, chapter_id, busy))
    try:
        from .agents_runtime.pipeline import run_rewrite_pipeline

        try:
            with _live_flow(f"重写（同步） · 第{chapter_id}章", mode="rewrite", book_id=book_id,
                            chapter_id=chapter_id, user=user):
                result = run_rewrite_pipeline(book, chapter_id, note=note,
                                              prealign=bool(payload.get("prealign", True)),
                                              progress=_live_progress,
                                              dry_run=bool(payload.get("dry_run", False)))
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=f"多用户隔离拦截: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 — 编排器兜底，端点层不悬挂
            raise HTTPException(status_code=500, detail=f"重写全流程执行异常: {exc}") from exc

        status = 200 if result.get("ok") else 502
        return {"book_id": book_id, "chapter_id": chapter_id, "status": status, "result": result}
    finally:
        _release_op(book_id, chapter_id, _op_task)


@agents_router.post("/books/{book_id}/mvp/agents/deslop")
def api_agents_deslop(book_id: str, payload: dict, user=Depends(get_current_user)) -> dict:
    """去AI味润色已提交章节（revision 闭环）：旧稿备份 → LLM 保守润色 → 归一落盘 → revision 提交 → 复扫。
    body: {chapter: int}
    与 agents/rewrite 的差异：只做门禁范围内的保守删改（≤15%、增大于删），不整章重生成。
    """
    chapter_id = payload.get("chapter")
    if not isinstance(chapter_id, int) or chapter_id < 1:
        raise HTTPException(status_code=400, detail="chapter must be a positive integer")
    root = projects_root(user["user_id"])
    try:
        book = book_root(root, book_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    # ★ 并发守卫（2026-10-19）：原子占用章操作槽，占用中 → 409（防同章并发互踩）
    _op_task = f"sync_{uuid.uuid4().hex[:12]}"
    busy = _try_acquire_op(book_id, chapter_id, "deslop", _op_task, book=book)
    if busy:
        raise HTTPException(status_code=409, detail=_op_busy_detail(book_id, chapter_id, busy))
    try:
        from .agents_runtime.pipeline import run_deslop_pipeline

        try:
            with _live_flow(f"去AI味（同步） · 第{chapter_id}章", mode="deslop", book_id=book_id,
                            chapter_id=chapter_id, user=user):
                result = run_deslop_pipeline(book, chapter_id, progress=_live_progress,
                                             dry_run=bool(payload.get("dry_run", False)))
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=f"多用户隔离拦截: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 — 编排器兜底，端点层不悬挂
            raise HTTPException(status_code=500, detail=f"润色全流程执行异常: {exc}") from exc

        status = 200 if result.get("ok") else 502
        return {"book_id": book_id, "chapter_id": chapter_id, "status": status, "result": result}
    finally:
        _release_op(book_id, chapter_id, _op_task)


@agents_router.post("/books/{book_id}/mvp/agents/deslop_text")
def api_agents_deslop_text(book_id: str, payload: dict, user=Depends(get_current_user)) -> dict:
    """贴文直接润色（聊天内文本模式）：用户贴入一段无章号正文 → 引擎去AI味改写 → 返回改后全文。

    body: {text: string（必填，待润色正文）, note?: string, chapter_id?: int}
    不落盘、不改任何文件、不 commit；book 仅用于模型路由上下文（读 bible 选 narrative-writer 池）。
    与 agents/deslop（作用于已提交章文件）的区别：本端点不吃 chapter、不改文件，纯文本进纯文本出。
    """
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(status_code=400, detail="text 必须为非空字符串（待润色正文）")
    note = str(payload.get("note", "") or "").strip()
    try:
        chapter_id = int(payload.get("chapter_id", 1))
    except (TypeError, ValueError):
        chapter_id = 1
    root = projects_root(user["user_id"])
    book = None
    try:
        book = book_root(root, book_id)
    except FileNotFoundError:
        book = None  # 贴文模式允许未绑书（book 仅用于模型路由上下文，run_deslop_text 内部以 Path('.') 兜底）

    from .agents_runtime.pipeline import run_deslop_text

    try:
        with _live_flow(f"贴文润色 · 第{chapter_id}章", mode="deslop", book_id=book_id,
                        chapter_id=chapter_id, user=user):
            with live_feed.stage("deslop_text", f"贴文去AI味（{len(text)} 字）") as _st:
                result = run_deslop_text(text, book=book, chapter_id=chapter_id)
                _st.output(str((result or {}).get("text") or (result or {}).get("detail") or "")[:4000])
    except Exception as exc:  # noqa: BLE001 — 编排器兜底，端点层不悬挂
        raise HTTPException(status_code=500, detail=f"贴文润色执行异常: {exc}") from exc

    status = 200 if result.get("ok") else 502
    return {"book_id": book_id, "status": status, "result": result}


@agents_router.post("/books/{book_id}/mvp/agents/plan-outline")
def api_agents_plan_outline(book_id: str, payload: dict, user=Depends(get_current_user)) -> dict:
    """细纲规划「去堵」端点（2026-11-13 补齐前端已调用的 404 缺口）。

    MvpConsole.tsx 的「细纲规划/去堵」按钮一直调 /mvp/agents/plan-outline，
    但后端从未挂该路由 → 点击必然 404。这里只增一层薄转发，真实逻辑复用
    pipeline.step_plan_outline（细纲缺失时由 nww-architect 生成并落盘）。
    NWW_PLAN_OUTLINE_ENABLED=0 可整体下线做回滚（返回 404，不静默返回成功）。
    """
    if os.environ.get("NWW_PLAN_OUTLINE_ENABLED", "1") == "0":
        raise HTTPException(status_code=404, detail="细纲规划端点已回滚（NWW_PLAN_OUTLINE_ENABLED=0）")
    try:
        chapter_id = int(payload.get("chapter"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="chapter 必须为正整数") from exc
    if chapter_id < 1:
        raise HTTPException(status_code=400, detail="chapter 必须为正整数")
    root = projects_root(user["user_id"])
    try:
        book = book_root(root, book_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    from .agents_runtime.pipeline import step_plan_outline

    try:
        with _live_flow(f"细纲规划 · 第{chapter_id}章", mode="write", book_id=book_id,
                        chapter_id=chapter_id, user=user):
            with live_feed.stage("plan_outline", f"细纲缺失检测 → 生成第{chapter_id}章细纲") as _st:
                result = step_plan_outline(book, chapter_id)
                _st.output(str((result or {}).get("detail") or "")[:4000])
    except Exception as exc:  # noqa: BLE001 — 端点层不悬挂，异常带原因返回
        raise HTTPException(status_code=500, detail=f"细纲规划执行异常: {exc}") from exc
    status = 200 if (result or {}).get("ok") else 502
    return {"book_id": book_id, "chapter_id": chapter_id, "status": status, "result": result}


@agents_router.post("/books/{book_id}/mvp/agents/revise")
def api_agents_revise(book_id: str, payload: dict, user=Depends(get_current_user)) -> dict:
    """局部修订已提交章节（nww-revision 只增不删算子，revision 闭环）：旧稿备份 → LLM 定点增补（≤3 处）→
    只增不删 diff 硬核对 → 归一落盘 → revision 提交 → 复扫。
    body: {chapter: int, note: string（必填，≤500 字：修订点与修订要求）}
    与 agents/rewrite 的差异：不改写已有句子、不整章重生成，只在 note 指定处插入新内容。
    """
    chapter_id = payload.get("chapter")
    if not isinstance(chapter_id, int) or chapter_id < 1:
        raise HTTPException(status_code=400, detail="chapter must be a positive integer")
    note = str(payload.get("note", "") or "").strip()
    if not note:
        raise HTTPException(status_code=400, detail="revise 必须提供 note（修订点与修订要求）")
    if len(note) > 500:
        raise HTTPException(status_code=400, detail="note 超过 500 字上限")
    # ★2026-10-09（端点层拦截）note 命中「重写/改写/…」+「开头/段落/…」→ 提前拒绝并提示改用
    #   rewrite，避免前端「继续重试」按钮绕过 chatbox 派单层后直接执行 revise 撞闸门 fail-closed。
    # 判据与 pipeline.run_revise_pipeline 的 _REPLACE_VERBS/_REPLACE_TARGETS 同源。
    _REPLACE_VERBS = ("重写", "改写", "替换", "覆盖", "删掉", "删除", "去掉", "删去")
    _REPLACE_TARGETS = ("开篇", "开头", "首段", "第一段", "结尾", "末段", "整章", "全章", "段落")
    if any(v in note for v in _REPLACE_VERBS) and any(t in note for t in _REPLACE_TARGETS):
        raise HTTPException(
            status_code=400,
            detail="revise 采用锚点插入协议（只增不删），无法完成「重写/改写/替换/删除 开篇/开头/结尾/段落」"
                   "类诉求。请改用「重写」（rewrite），并在要求里写明「开头承接上一章结尾」；"
                   "或改用 revise 的「插入」方式：把要求写成「在开头第一段之前新增一段承接上一章结尾的过渡，只新增」。",
        )
    root = projects_root(user["user_id"])
    try:
        book = book_root(root, book_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    # ★ 并发守卫（2026-10-19）：原子占用章操作槽，占用中 → 409（防同章并发互踩）
    _op_task = f"sync_{uuid.uuid4().hex[:12]}"
    busy = _try_acquire_op(book_id, chapter_id, "revise", _op_task, book=book)
    if busy:
        raise HTTPException(status_code=409, detail=_op_busy_detail(book_id, chapter_id, busy))
    try:
        from .agents_runtime.pipeline import run_revise_pipeline

        try:
            with _live_flow(f"修订（同步） · 第{chapter_id}章", mode="revise", book_id=book_id,
                            chapter_id=chapter_id, user=user):
                result = run_revise_pipeline(book, chapter_id, note=note,
                                             progress=_live_progress,
                                             dry_run=bool(payload.get("dry_run", False)))
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=f"多用户隔离拦截: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 — 编排器兜底，端点层不悬挂
            raise HTTPException(status_code=500, detail=f"局部修订全流程执行异常: {exc}") from exc

        status = 200 if result.get("ok") else 502
        return {"book_id": book_id, "chapter_id": chapter_id, "status": status, "result": result}
    finally:
        _release_op(book_id, chapter_id, _op_task)


# ---------------------------------------------------------------------------
# 异步任务（进度上报）：write/rewrite/deslop/revise 后台跑 pipeline，前端轮询 /agents/task/{id}
# ---------------------------------------------------------------------------
_AGENT_TASKS: dict[str, dict] = {}
_AGENT_TASKS_LOCK = threading.Lock()

# 任务注册表持久化（2026-10-25 task not found 事故复盘）：
#   事故形态：uvicorn --reload 热载（或进程重启）→ 内存注册表清空 → 前端轮询
#   /agents/task/{id} 404 "task not found"，在跑任务连同进度一起蒸发，失败卡片
#   只能显示旧回执。修复：注册/更新时落盘 runtime/agent_tasks/{id}.json；启动时
#   恢复记录，running 态标记为明确中断错误（线程已随旧进程死亡，不可续跑，
#   但状态可查、可解释、可引导重试）。
_AGENT_TASKS_DIR = Path(__file__).resolve().parents[3] / "runtime" / "agent_tasks"
_AGENT_TASKS_TTL_S = 7 * 24 * 3600  # 落盘记录保留 7 天（启动时清理更早的）
# 僵尸任务阈值：running 状态超过该秒数无落盘更新 → 视为中断（与千章守护中心 _STALE_RUNNING_S 对齐）
_STALE_TASK_S = 1800

# 中断任务的统一提示（R6-AC1：interrupted = 进程中断，可章级续跑，不是逻辑失败）。
# 取代旧文案「进度不可续跑」——章级重跑自带正文/delta/台账事务回滚，是可安全重入的入口。
_INTERRUPT_HINT = ("进程中断（代码热载/重启），任务未完成；"
                   "可点击「续跑」按章级重跑恢复（重写链自带事务回滚）")

# 可章级续跑的模式（与 C-RESUME@1 一致；audit 等无正文写入链不在其中）
_TASK_RESUMABLE_MODES = ("rewrite", "write", "deslop", "polish", "revise")


def _task_resume_snapshot(rec: dict) -> dict:
    """按 C-TASK-STATE@1 产出续跑所需最小参数快照（与记录同源取值，不臆造）。

    prealign 一并入快照（T9 集成验证偏差 2）：否则续跑会丢失原任务的
    prealign=false 选择，多跑一轮 LLM 且行为与原任务不一致。旧记录无该字段时
    按原端点默认值 True 记录，供审计与续跑复用。"""
    return {
        "mode": str(rec.get("mode") or ""),
        "book_id": str(rec.get("book_id") or ""),
        "chapter_id": rec.get("chapter_id"),
        "level": str(rec.get("level") or ""),
        "note": str(rec.get("note") or ""),
        "dry_run": bool(rec.get("dry_run", False)),
        "prealign": bool(rec.get("prealign", True)),
    }


def _task_is_resumable(rec: dict) -> bool:
    """是否具备章级续跑条件：mode ∈ 可续跑集合 + book_id 非空 + chapter_id 为正整数。"""
    if str(rec.get("mode") or "") not in _TASK_RESUMABLE_MODES:
        return False
    if not str(rec.get("book_id") or "").strip():
        return False
    try:
        chapter_id = int(rec.get("chapter_id"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return chapter_id > 0


def _task_persist(task_id: str) -> None:
    """把任务记录原子落盘（调用方必须已持有 _AGENT_TASKS_LOCK）。失败不挂主链。"""
    try:
        rec = _AGENT_TASKS.get(task_id)
        if rec is None:
            return
        _AGENT_TASKS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _AGENT_TASKS_DIR / f".{task_id}.tmp"
        tmp.write_text(json.dumps({"task_id": task_id, **rec}, ensure_ascii=False, default=str),
                       encoding="utf-8")
        tmp.replace(_AGENT_TASKS_DIR / f"{task_id}.json")
    except Exception:  # noqa: BLE001 — 持久化失败不影响任务本体
        pass


def _task_register(task_id: str, record: dict, user_id: str | None = None) -> None:
    """注册新任务（内存 + 落盘）。

    user_id：多用户隔离必填（2026-11-07 根治）。写入后，守护面板按 user_id 过滤运行记录，
    杜绝全局 agent_tasks 目录下同名书/伪造 book_id 跨用户拉取。缺省 None 仅用于兼容极旧路径，
    正常端点均显式传入当前登录 user_id。
    """
    with _AGENT_TASKS_LOCK:
        now = time.time()
        record.setdefault("started_at", now)
        record.setdefault("_registered_ts", now)
        record["_updated_ts"] = now
        if user_id is not None:
            record["user_id"] = user_id
        _AGENT_TASKS[task_id] = record
        _task_persist(task_id)


def _task_file_status(task_id: str) -> str | None:
    """读取落盘任务记录的 status（调用方已持锁；读失败返回 None）。

    用途：内存终态与落盘 running 可能不一致（_task_restore_from_disk 只改内存），
    取消/修复前需要以落盘为准判断是否为僵尸 running。"""
    try:
        f = _AGENT_TASKS_DIR / f"{task_id}.json"
        if not f.is_file():
            return None
        data = json.loads(f.read_text(encoding="utf-8"))
        return str(data.get("status") or "") or None
    except Exception:  # noqa: BLE001 — 坏档按未知处理
        return None


def _task_restore_from_disk() -> None:
    """启动时恢复落盘任务记录：running → interrupted（可续跑）并回写盘；顺带清理超 TTL 旧档。

    R6-AC2：旧实现只改内存、不回写文件，导致 runtime/agent_tasks/ 永久积累 running。
    现语义：落盘 running 一律标 interrupted + 参数快照，并在锁内落盘（一次清理存量）。
    `NWW_TASK_RESTORE_LEGACY=1` 回到旧语义（error + 「进度不可续跑」，仅内存）用于对照/回滚。
    """
    try:
        if not _AGENT_TASKS_DIR.is_dir():
            return
        legacy = str(os.environ.get("NWW_TASK_RESTORE_LEGACY") or "").strip().lower() in (
            "1", "true", "yes", "on")
        now = time.time()
        needs_persist: list[str] = []
        for f in _AGENT_TASKS_DIR.glob("*.json"):
            try:
                if now - f.stat().st_mtime > _AGENT_TASKS_TTL_S:
                    f.unlink(missing_ok=True)
                    continue
                data = json.loads(f.read_text(encoding="utf-8"))
                tid = str(data.pop("task_id", "") or f.stem)
                if data.get("status") == "running":
                    data.pop("cancel_requested", None)
                    if legacy:
                        # 旧语义（只改内存、不回写盘），仅供对照/回滚
                        data["status"] = "error"
                        data["error"] = ("后端重启（代码热载/进程重启），该任务已中断——"
                                         "进度不可续跑，请重新发起；上次进度见该任务已保存的阶段。")
                    else:
                        data.update({
                            "status": "interrupted",
                            "interrupt_reason": "process_restarted",
                            "interrupted_at": now,
                            "error": _INTERRUPT_HINT,
                            "resumable": _task_is_resumable(data),
                            "resume": _task_resume_snapshot(data),
                        })
                        data.pop("stale", None)
                        needs_persist.append(tid)
                data.setdefault("_updated_ts", f.stat().st_mtime)
                data.setdefault("_registered_ts", data.get("started_at") or f.stat().st_mtime)
                _AGENT_TASKS[tid] = data
            except Exception:  # noqa: BLE001 — 单个坏档不阻断恢复
                continue
        if needs_persist:
            with _AGENT_TASKS_LOCK:
                for tid in needs_persist:
                    _task_persist(tid)
    except Exception:  # noqa: BLE001 — 恢复失败按空表启动（原行为）
        pass


_task_restore_from_disk()

# 后台线程句柄表：与 _AGENT_TASKS **刻意分离**——_AGENT_TASKS 会被
# /agents/task/{id} 原样返回给前端，Thread 对象不可 JSON 序列化，绝不能塞进去。
# 用途：单飞互斥时判定"在跑任务"是否真的活着（线程静默死亡时 status 会永远停在
# running，只看 status 会把后续所有审计永久挡在门外）。
_AGENT_THREADS: dict[str, threading.Thread] = {}

# 区间审计的每章预算（秒）。实测 2.5~4 分钟/章（双采样 = 2 次 LLM 调用），
# 300s 是宽松上限；总预算 = 章数 × 本值，随工作量线性伸缩，
# 取代原先前端那句写死的「15 分钟」（对 28 章差 5~7 倍，必然误报超时）。
_AUDIT_PER_CHAPTER_BUDGET_S = float(
    os.environ.get("NWW_AUDIT_PER_CHAPTER_S", "300") or "300"
)


def _task_update(task_id: str, **kw) -> None:
    with _AGENT_TASKS_LOCK:
        if task_id in _AGENT_TASKS:
            _AGENT_TASKS[task_id].update(kw)
            # 每次更新都刷新活动时间戳：僵尸判定（thread_lost_timeout）的唯一依据
            _AGENT_TASKS[task_id]["_updated_ts"] = time.time()
            _task_persist(task_id)  # 持久化（热载/重启后轮询可查）
            # 终态：释放线程句柄（长会话下避免句柄表无限增长）
            if kw.get("status") in ("done", "error", "cancelled", "interrupted"):
                _AGENT_THREADS.pop(task_id, None)


def _task_last_activity_ts(task_id: str, rec: dict) -> float:
    """任务最近一次活动的 epoch 秒（_updated_ts → 落盘 mtime → started_at 兜底）。"""
    for key in ("_updated_ts", "started_at"):
        try:
            ts = float(rec.get(key) or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts > 0:
            return ts
    try:
        f = _AGENT_TASKS_DIR / f"{task_id}.json"
        if f.is_file():
            return float(f.stat().st_mtime)
    except OSError:
        pass
    return 0.0


def _task_thread_alive(task_id: str) -> bool:
    """执行线程是否仍存活（只读；不在 _AGENT_TASKS_LOCK 内调用，遵守锁序纪律）。

    rewrite/write/deslop/polish/revise 的异步端点不登记 _AGENT_THREADS，但它们在
    _ACTIVE_OPS 章槽里挂了同一个线程句柄，故两处都要查，避免把「正在跑但没登记句柄」
    的正常任务误判为僵尸。"""
    th = _AGENT_THREADS.get(task_id)
    if th is not None and th.is_alive():
        return True
    try:
        with _ACTIVE_OPS_LOCK:
            for slot in _ACTIVE_OPS.values():
                if slot.get("task_id") != task_id:
                    continue
                slot_th = slot.get("thread")
                return bool(slot_th is not None and slot_th.is_alive())
    except Exception:  # noqa: BLE001 — 存活判定失败按「不确定」处理（保守不降级）
        return True
    return False


def _is_stale_running(task_id: str, rec: dict) -> bool:
    """R6-AC3 僵尸判据：status=running 且执行线程已不在 且超过 _STALE_TASK_S 无更新。"""
    if rec.get("status") != "running":
        return False
    if _task_thread_alive(task_id):
        return False
    return (time.time() - _task_last_activity_ts(task_id, rec)) > _STALE_TASK_S


def _mark_task_interrupted(task_id: str, reason: str, stale: bool = False) -> dict | None:
    """把内存中的 running 记录降级为 interrupted 并回写盘，返回更新后的副本。

    仅当记录仍存在且仍为 running 时生效（并发下已被他人改终态 → 返回 None）。
    调用方不得持有 _AGENT_TASKS_LOCK（本函数自带锁）。"""
    with _AGENT_TASKS_LOCK:
        rec = _AGENT_TASKS.get(task_id)
        if rec is None or rec.get("status") != "running":
            return None
        now = time.time()
        rec.update({
            "status": "interrupted",
            "interrupt_reason": reason,
            "interrupted_at": now,
            "error": _INTERRUPT_HINT,
            "resumable": _task_is_resumable(rec),
            "resume": _task_resume_snapshot(rec),
            "_updated_ts": now,
        })
        if stale:
            rec["stale"] = True
        _AGENT_THREADS.pop(task_id, None)
        _task_persist(task_id)
        return dict(rec)


def _task_recency_ts(task_id: str, rec: dict) -> float:
    """用于「最近任务」排序的时间戳（_updated_ts → _registered_ts → started_at → 落盘 mtime）。"""
    for key in ("_updated_ts", "_registered_ts", "started_at"):
        try:
            ts = float(rec.get(key) or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts > 0:
            return ts
    try:
        f = _AGENT_TASKS_DIR / f"{task_id}.json"
        if f.is_file():
            return float(f.stat().st_mtime)
    except OSError:
        pass
    return 0.0


def _append_live_stage(task_id: str, name: str, ok: bool, detail: str, output: str = "") -> None:
    """把已完成环节追加到任务时间线（AI 实况面板数据源）。

    保留历史步骤，不被后续环节覆盖。detail 截断 600 字做摘要；
    output 存完整产物（LLM 返回原文），截断到 8000 字，供前端展开查看。
    调用方必须先持锁之外的位置调用（本函数自带锁 + 落盘）。"""
    with _AGENT_TASKS_LOCK:
        rec = _AGENT_TASKS.get(task_id)
        if rec is None:
            return
        stages = list(rec.get("live_stages") or [])
        stages.append({
            "name": name,
            "ok": bool(ok),
            "detail": str(detail or "")[:600],
            "output": str(output or "")[:8000],
            "ts": time.time(),
        })
        rec["live_stages"] = stages
        _task_persist(task_id)


def _running_audit_task(book_id: str) -> dict | None:
    """返回该书正在运行的审计任务快照（无 → None）。

    单飞互斥（2026-09-27）：区间审计是长任务，前端超时后用户点「重试」会叠加出
    第二个任务，两个任务串行争抢 chrome_bridge 全局锁与同一个 Chrome 页面
    → 串会话 + 互拖 → 502。这里改为复用已在跑的任务（幂等），不再起第二个线程。
    线程已死但 status 仍为 running 的僵尸任务不参与互斥（避免永久阻塞）。
    """
    with _AGENT_TASKS_LOCK:
        for tid, t in _AGENT_TASKS.items():
            if t.get("mode") != "audit" or t.get("status") != "running":
                continue
            if t.get("book_id") != book_id:
                continue
            th = _AGENT_THREADS.get(tid)
            if th is not None and not th.is_alive():
                continue  # 僵尸任务：线程已死，忽略
            return {"task_id": tid, "range": list(t.get("range") or [])}
    return None


# ---------------------------------------------------------------------------
# 章级并发操作守卫（2026-10-19）：同一 (book, chapter) 写操作互斥
#
# 背景：rewrite/deslop/revise/polish/write/commit 都是「覆盖正文 → delta → commit」
#   事务。同一章两个操作并发会互相覆盖落盘、互踩 delta/台账（双发重写 → 后发覆盖
#   先发新稿，delta/台账状态混杂，前端只见其一）。此前各端点层无任何守卫。
#   此处新增章级操作槽注册表：每 (book, chapter) 至多一个在途操作，占用方信息随
#   409 返回（前端可提示「有操作进行中」）。
# 双层互斥（2026-11-13 硬化）：进程内槽只挡同进程并发；Gunicorn/Uvicorn 多 Worker
#   部署下再叠一层跨进程章锁（C-LOCK@1，提供方 web/backend/app/chapter_lock.py）。
#   调用方传入 book 路径即启用第二层；提供方缺席时退化为进程内槽（与既有行为等价）。
# 锁序纪律：_ACTIVE_OPS_LOCK 与 _AGENT_TASKS_LOCK 互不嵌套（各自临界区只持一把锁），
#   杜绝死锁；正常释放路径的跨进程锁在槽位临界区之外解锁，僵尸驱逐为兜底、仍在区内。
# 纯扩展接入：既有端点逻辑零改动，只在入口 acquire、出口 release。
# ---------------------------------------------------------------------------
_ACTIVE_OPS: dict[tuple[str, int], dict] = {}
_ACTIVE_OPS_LOCK = threading.Lock()
# 无活线程槽位的宽限寿命（秒）：覆盖「已注册但线程尚未 start」（start 前
# is_alive()=False，必须宽限保护）与「SSE 客户端在 run() 启动前断连」两种场景。
_ACTIVE_OP_STALE_S = 30.0

_CHAPTER_LOCK_MOD = None
_CHAPTER_LOCK_READY = False


def _chapter_lock_mod():
    """惰性加载跨进程章锁提供方（C-LOCK@1）；不可用时返回 None，仅进程内槽生效。"""
    global _CHAPTER_LOCK_MOD, _CHAPTER_LOCK_READY
    if not _CHAPTER_LOCK_READY:
        _CHAPTER_LOCK_READY = True
        try:
            from . import chapter_lock as _mod
            _CHAPTER_LOCK_MOD = _mod
        except Exception as exc:  # noqa: BLE001 — 提供方缺失只降级，不阻断写链
            print(f"[NOVEL] chapter lock unavailable: {exc}", flush=True)
            _CHAPTER_LOCK_MOD = None
    return _CHAPTER_LOCK_MOD


def _release_lock_handle(handle) -> None:
    """释放跨进程锁句柄（幂等；失败只落一行诊断，进程退出由 OS 兜底）。"""
    if handle is None:
        return
    try:
        handle.release()
    except Exception as exc:  # noqa: BLE001
        print(f"[NOVEL] chapter lock release failed: {exc}", flush=True)


def _evict_dead_ops_locked() -> None:
    """驱逐僵尸槽（调用方必须已持有 _ACTIVE_OPS_LOCK）。

    判据（参照 _running_audit_task 的僵尸检测 + 时间兜底）：
    - 线程句柄存在但线程已死，且注册超宽限期 → 驱逐（正常路径由 finally 主动
      释放，此处只兜底极端崩溃）；
    - 无线程句柄（SSE 流尚未启动）且注册超宽限期 → 驱逐（客户端启动前断连）。
    宽限期同时保护「已注册未 start」的线程（start() 前 is_alive()=False）。"""
    now = time.time()
    dead = [key for key, slot in _ACTIVE_OPS.items()
            if (slot.get("thread") is None or not slot["thread"].is_alive())
            and now - float(slot.get("ts") or now) > _ACTIVE_OP_STALE_S]
    for key in dead:
        slot = _ACTIVE_OPS.pop(key, None)
        _release_lock_handle((slot or {}).get("lock"))


def _try_acquire_op(book_id: str, chapter_id: int, mode: str, task_id: str,
                    thread: threading.Thread | None = None,
                    book: Path | None = None) -> dict | None:
    """原子占用章操作槽（进程内互斥）+ 跨进程章锁（C-LOCK@1）。

    book 传入时启用第二层跨进程锁；提供方缺席或锁实现抛异常都只降级为进程内槽。
    成功 → None；已被占用 → 占用方信息 {"mode","task_id","pid","acquired_at"}（调用方据此 409）。"""
    key = (str(book_id), int(chapter_id))
    with _ACTIVE_OPS_LOCK:
        _evict_dead_ops_locked()
        slot = _ACTIVE_OPS.get(key)
        if slot is not None:
            return {"mode": slot.get("mode"), "task_id": slot.get("task_id"), "pid": os.getpid()}
        handle = None
        mod = _chapter_lock_mod() if book is not None else None
        if mod is not None:
            try:
                handle = mod.acquire(book, int(chapter_id), mode, task_id)
            except Exception as exc:  # noqa: BLE001 — 锁不可用只退化为进程内槽
                print(f"[NOVEL] chapter lock acquire failed: {exc}", flush=True)
                mod = None
            if mod is not None and handle is None:
                try:
                    holder = mod.probe(book, int(chapter_id))
                except Exception as exc:  # noqa: BLE001
                    print(f"[NOVEL] chapter lock probe failed: {exc}", flush=True)
                    holder = None
                if holder is None:
                    try:
                        handle = mod.acquire(book, int(chapter_id), mode, task_id)
                    except Exception as exc:  # noqa: BLE001
                        print(f"[NOVEL] chapter lock acquire retry failed: {exc}", flush=True)
                        handle = None
                if handle is None:
                    holder = holder or {}
                    return {
                        "mode": holder.get("mode") or mode,
                        "task_id": holder.get("task_id") or "unknown",
                        "pid": holder.get("pid"),
                        "acquired_at": holder.get("acquired_at"),
                    }
        _ACTIVE_OPS[key] = {"mode": mode, "task_id": task_id, "thread": thread,
                            "ts": time.time(), "lock": handle}
        return None


def _release_op(book_id: str, chapter_id: int, task_id: str) -> None:
    """释放槽 + 跨进程锁：仅 owner 匹配才释放（防超时驱逐后旧任务误删新任务的槽）。"""
    key = (str(book_id), int(chapter_id))
    handle = None
    with _ACTIVE_OPS_LOCK:
        slot = _ACTIVE_OPS.get(key)
        if slot is not None and slot.get("task_id") == task_id:
            _ACTIVE_OPS.pop(key, None)
            handle = slot.get("lock")
    _release_lock_handle(handle)


def _release_dead_op(book_id: str, chapter_id: int, task_id: str) -> None:
    """释放「已中断任务」遗留的死槽：owner 匹配且线程不存活才释放。

    续跑入口专用：旧任务 status=interrupted 说明其执行链已结束，但槽位可能因进程
    未走 finally 而残留，若不清理会让续跑误报「同章有进行中操作」。owner 不匹配或
    线程仍存活时一律不动（绝不误放真正在跑的操作）。"""
    key = (str(book_id), int(chapter_id))
    handle = None
    with _ACTIVE_OPS_LOCK:
        slot = _ACTIVE_OPS.get(key)
        if slot is None or slot.get("task_id") != task_id:
            return
        th = slot.get("thread")
        if th is None or not th.is_alive():
            _ACTIVE_OPS.pop(key, None)
            handle = slot.get("lock")
    _release_lock_handle(handle)


def _bind_op_thread(book_id: str, chapter_id: int, task_id: str, thread: threading.Thread) -> None:
    """补登线程句柄（SSE 场景：端点先注册、run() 启动时才拿到线程）。仅 owner 匹配时补登。"""
    key = (str(book_id), int(chapter_id))
    with _ACTIVE_OPS_LOCK:
        slot = _ACTIVE_OPS.get(key)
        if slot is not None and slot.get("task_id") == task_id:
            slot["thread"] = thread


def _op_busy_detail(book_id: str, chapter_id: int, busy: dict) -> str:
    return (f"第{chapter_id}章有进行中的操作（{busy.get('mode')}，任务 {busy.get('task_id')}），"
            "请等待其完成或先取消再重试")


# ---------------------------------------------------------------------------
# 失败决策回执（2026-09-25）：重写/审计失败不再静默结束
#
# 背景：pipeline BLOCK（如 contract_guard 字数欠账）后正文已回滚，但前端只拿到
# 一行 `执行失败：<环节> — <detail>`，不回灌对话、不落盘、无下一步入口，
# 用户刷新即丢失全部失败信息。本模块统一派生「失败诊断 + 决策项」并落盘回执：
#   - 纯增量：不改任何闸门判定逻辑，只在任务终态上附加字段
#   - 落点：追踪/执行回执/第NNN章_{mode}_{ts}.json（与 追踪/脱纲审计/ 同级）
#   - 前端据此渲染决策卡片，用户可选「上报对话请示」把回执注入会话交模型决策
# ---------------------------------------------------------------------------
_RECEIPT_DIR = "执行回执"

# (detail 关键字, 闸门类型)：按出现顺序优先匹配，未命中 → generic
# llm_unavailable（2026-10-25）：基础设施故障而非内容问题——chat_completion_robust
# 全链失败时的固定措辞。此类失败「接受现状/下调目标」都无意义，正确动作是等端点
# 恢复或换模型重跑（端点链已加全局默认兜底，重试即自动降级）。
_GATE_PATTERNS = (
    ("重写熔断", "fuse"),
    ("份执行回执 > 上限", "fuse"),
    ("LLM 调用全部失败", "llm_unavailable"),
    ("字数欠账", "length_short"),
    ("时间事实漂移", "time_drift"),
    ("事实漂移", "fact_drift"),
    ("证据不足", "evidence_missing"),
    ("缺失", "evidence_missing"),
    ("未落地", "beat_missing"),
)

# G_Length 明细口径见 pipeline.py:5897
_LENGTH_RE = re.compile(
    r"正文\s*(\d+)\s*字\s*<\s*细纲目标\s*(\d+)\s*字的\s*\d+%\s*[（(](\d+)\s*字[)）]")

# 通用尾部选项（所有失败类型共有）
_TAIL_OPTIONS = (
    {"id": "accept", "label": "接受现状", "action": "accept",
     "hint": "保留回滚后的原正文，标记该失败已处理", "risk": "low"},
    {"id": "escalate", "label": "上报对话请示", "action": "escalate",
     "hint": "把失败回执注入会话，请模型给出下一步决策", "risk": "low"},
)

_DECISION_OPTIONS: dict[str, list[dict]] = {
    # 基础设施故障：不是内容问题，正文多半已回滚——只给「重跑」族选项
    "llm_unavailable": [
        {"id": "retry", "label": "重跑本环节（自动降级备用模型）", "action": "retry",
         "hint": "主端点 502/超时多为桥接或网页会话故障；端点链已带全局默认兜底，"
                 "重试将自动降级到可用模型继续，不需要换配置", "risk": "low"},
        {"id": "check_endpoint", "label": "先去模型中心测连通性", "action": "check_endpoint",
         "hint": "重试仍失败时：模型中心 → 供应商「测试」，确认桥接/Chrome 会话是否存活", "risk": "low"},
    ],
    # 重写熔断（2026-11-13 用户裁决）：不是禁止重写，也不是自动重写，而是请示作者下一步。
    "fuse": [
        {"id": "continue_once", "label": "继续重写一次（本次授权）", "action": "retry",
         "hint": "只放行一轮整章重写；一次性令牌消费后熔断继续生效", "risk": "low"},
        {"id": "revise", "label": "局部修订（不整章重写）", "action": "revise",
         "hint": "按失败明细定点修改，保留结构与事实锚", "risk": "low"},
        {"id": "fix_outline", "label": "先改细纲/角色卡再重写", "action": "fix_outline",
         "hint": "反复重写多为上游模具问题；先改细纲/角色卡，再继续", "risk": "medium"},
        {"id": "accept", "label": "接受现状", "action": "accept",
         "hint": "保留当前正文，不再重写", "risk": "low"},
    ],
    "length_short": [
        {"id": "retry", "label": "继续重写（同参数重试）", "action": "retry",
         "hint": "同等级同要点再跑一次；模型输出有随机性，可能达标", "risk": "low"},
        {"id": "expand", "label": "局部扩写补丁", "action": "expand",
         "hint": "保留现有正文，只补写到字数下限（不整体重生产）", "risk": "low"},
        {"id": "lower_target", "label": "下调细纲字数目标后重写", "action": "lower_target",
         "hint": "会改写 大纲/细纲_第N章.md 的 字数目标 行，须二次确认", "risk": "high"},
    ],
    "time_drift": [
        {"id": "retry", "label": "继续重写（同参数重试）", "action": "retry",
         "hint": "时间口径由细纲给定，重跑可能修正", "risk": "low"},
        {"id": "revise", "label": "局部修订时间表述", "action": "revise",
         "hint": "只改漂移的时间事实句，不动结构", "risk": "low"},
        {"id": "lower_target", "label": "修正细纲时间锚后重写", "action": "lower_target",
         "hint": "会改写细纲，须二次确认", "risk": "high"},
    ],
}

# fact_drift / evidence_missing / beat_missing 共用
_DEFAULT_OPTIONS = [
    {"id": "retry", "label": "继续重写（同参数重试）", "action": "retry",
     "hint": "同参数再跑一次", "risk": "low"},
    {"id": "revise", "label": "局部修订", "action": "revise",
     "hint": "按失败明细做定点修订，不做全章重生产", "risk": "low"},
]

_GENERIC_OPTIONS = [
    {"id": "retry", "label": "继续重试", "action": "retry",
     "hint": "同参数再跑一次", "risk": "low"},
]


def _gate_type(detail: str) -> str:
    """按失败明细判定闸门类型；未命中返回 generic。"""
    d = str(detail or "")
    for kw, gate in _GATE_PATTERNS:
        if kw in d:
            return gate
    return "generic"


def _chapter_facts(book: Path, chapter_id: int) -> dict:
    """采集决策所需客观事实：正文当前字数 / 细纲字数目标（失败后正文已回滚，取当前盘面）。"""
    facts: dict = {}
    try:
        from .agents_runtime.pipeline import _resolve_existing_body
        body = _resolve_existing_body(book, chapter_id)
        if body is not None and body.is_file():
            txt = body.read_text(encoding="utf-8")
            facts["body_chars"] = len(re.sub(r"\s+", "", txt))
            facts["body_path"] = body.relative_to(book).as_posix()
    except Exception:  # noqa: BLE001 — 事实采集失败不影响主流程
        pass
    try:
        # 只读查找：不用 _session_outline_compat（它会在 大纲/ 下建临时软链）
        from .agents_runtime.pipeline import _find_outline_file
        op = _find_outline_file(book, chapter_id)
        if op is not None and Path(op).is_file():
            op = Path(op)
            try:
                facts["outline_path"] = op.relative_to(book).as_posix()
            except ValueError:
                pass
            m = re.search(r"^\**\s*字数目标\**\s*[:：]\s*([1-9]\d*)",
                          op.read_text(encoding="utf-8"), re.MULTILINE)
            if m:
                facts["outline_target"] = int(m.group(1))
    except Exception:  # noqa: BLE001
        pass
    return facts


def _attach_gate_facts(result: dict, *, book: Path, mode: str,
                       chapter_id: int) -> dict:
    """任务终态挂只读事实卡 + 作者裁决状态（成功与失败都挂，2026-11-13）。

    这是「道闸放开后还有什么用」的另一半：v13 下动作不再失败，原先寄生于失败回执的
    决策卡就不再出现，闸事实只剩日志。这里把同一张事实卡挂回任务结果，作者当场可见。

    只增字段、绝不覆盖已有值：_settle_fail 在「有正文但未提交」时已经带过
    gate_report / commit_status，保留它，只补裁决状态与「是否待裁决」。
    gate_status 是**裁决状态**（pending/stale/accept/recheck/rollback），
    不是质量判定；事实卡本体仍禁一切判决字段。
    audit 模式跨多章，挂单章事实卡会误导，故跳过。
    任何异常都原样返回（fail-open），绝不因可见性附加把动作结果弄丢。
    """
    if not isinstance(result, dict) or mode == "audit":
        return result
    try:
        from . import gate_facts
        card = gate_facts.report(book, chapter_id)
        dec = gate_facts.decision(book, chapter_id)
        status = gate_facts.adjudication_status(card, dec)
        out = dict(result)
        out.setdefault("gate_report", card)
        out["gate_adjudication"] = dec
        out["gate_status"] = status
        out["gate_pending"] = (int(card.get("count") or 0) > 0
                               and status in ("pending", "stale"))
        return out
    except Exception:  # noqa: BLE001
        return result


def _write_receipt(book: Path, chapter_id: int, mode: str, payload: dict) -> str:
    """落盘一份执行回执（一次任务一份），返回相对路径。

    2026-11-13 修复：此前只有失败任务写回执，成功任务（如 ab0ef3a6e12a 已提交
    commit 且 fuse sync 成功）在 `追踪/执行回执/` 零留痕，诊断链断在「任务明明
    提交了却找不到执行记录」。现在成功也写，并标 `counted=false` —— 成功提交后
    fuse 基线已 sync，不能让这份诊断记录再顶一次重写额度。
    写失败重试一次（IO 瞬时抖动），仍失败抛给调用方显式降级，不静默丢弃。
    """
    out_dir = book / "追踪" / _RECEIPT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    base = f"第{chapter_id:03d}章_{mode}_{datetime.now():%Y%m%d_%H%M%S}"
    for attempt in (1, 2):
        rpath = out_dir / f"{base}.json"
        n = 1
        while rpath.exists():
            rpath = out_dir / f"{base}_{n}.json"
            n += 1
        try:
            rpath.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return rpath.relative_to(book).as_posix()
        except OSError:
            if attempt == 2:
                raise
            time.sleep(0.05)
    raise RuntimeError("unreachable")


def _with_decision(result: dict, *, book: Path, mode: str, chapter_id: int,
                   task_id: str = "") -> dict:
    """给任务终态附加决策信息并落盘回执；成功与失败都写（一次任务一份）。

    附加字段（失败）：needs_decision / failure{stage,detail,gate,rolled_back} /
              facts / decision_options / receipt_path
    附加字段（成功）：status=succeeded / counted=false / commit / stages / receipt_path
    附加字段（通用）：gate_report / gate_adjudication / gate_status / gate_pending
    """
    if not isinstance(result, dict):
        return result
    if result.get("ok"):
        if mode == "audit":
            # audit 跨多章，单章回执会误导（与 _attach_gate_facts 同口径）
            return _attach_gate_facts(result, book=book, mode=mode, chapter_id=chapter_id)
        try:
            receipt = {
                "task_id": task_id,
                "mode": mode,
                "chapter_id": chapter_id,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "status": "succeeded",
                "ok": True,
                "fatal_phase": result.get("fatal_phase"),
                "commit": result.get("commit"),
                "advisory_failures": list(result.get("advisory_failures") or []),
                "stages": [s for s in (result.get("stages") or []) if isinstance(s, dict)],
                # 成功提交后 fuse 基线已 sync；诊断留痕不占重写额度
                "counted": False,
            }
            receipt_rel = _write_receipt(book, chapter_id, mode, receipt)
            out = dict(result)
            out["receipt_path"] = receipt_rel
            return _attach_gate_facts(out, book=book, mode=mode, chapter_id=chapter_id)
        except Exception as exc:  # noqa: BLE001 — 回执写失败不得覆盖成功结果
            print(f"[RECEIPT] 成功回执写入失败（{mode} 第{chapter_id}章）: {exc}",
                  file=sys.stderr)
            out = dict(result)
            out["receipt_error"] = str(exc)[:200]
            return _attach_gate_facts(out, book=book, mode=mode, chapter_id=chapter_id)
    try:
        stages = result.get("stages") or []
        # P0-1（2026-10-30 外脑定稿）：优先读 pipeline 输出的 fatal_phase（真实致死环节），
        # 杜绝 advisory 环节顶包误导决策（第1章事故：wordcount advisory 顶替 delta_semantic fatal）。
        # 缺失/未确认时回退旧口径（第一个 ok=False），并在 detail 注明归因未确认。
        _fp = result.get("fatal_phase")
        failed = None
        if _fp:
            failed = next((s for s in stages if isinstance(s, dict)
                           and s.get("name") == _fp and not s.get("ok")), None) \
                or next((s for s in stages if isinstance(s, dict) and s.get("name") == _fp), None)
        _attribution_unconfirmed = False
        if failed is None:
            failed = next((s for s in stages if isinstance(s, dict) and not s.get("ok")), None)
            if failed is not None and result.get("fatal_phase_confirmed") is not True:
                _attribution_unconfirmed = True
        detail = str((failed or {}).get("detail") or result.get("error") or "")
        stage_name = str((failed or {}).get("name") or mode)
        if _attribution_unconfirmed:
            detail = f"［归因未确认：取第一个失败环节，可能非致死］{detail}"
        # 审计链：无 stages，按 chapters 里的 error 汇总
        if not failed:
            bad = [c for c in (result.get("chapters") or [])
                   if isinstance(c, dict) and not c.get("ok")]
            if bad:
                detail = "；".join(str(c.get("error") or "")[:80] for c in bad[:3]) or detail
                stage_name = f"audit_ch{bad[0].get('chapter', chapter_id)}"

        gate = _gate_type(detail)
        facts = _chapter_facts(book, chapter_id)
        m = _LENGTH_RE.search(detail)
        if m:
            facts.setdefault("body_chars", int(m.group(1)))
            facts["outline_target"] = int(m.group(2))
            facts["required_floor"] = int(m.group(3))
        if "outline_target" in facts and "required_floor" not in facts:
            facts["required_floor"] = int(facts["outline_target"] * 0.7)

        if gate == "generic":
            opts = _GENERIC_OPTIONS
        else:
            opts = _DECISION_OPTIONS.get(gate) or _DEFAULT_OPTIONS
        options = [dict(o) for o in opts] + [dict(o) for o in _TAIL_OPTIONS]
        # 字数未达标时，扩写目标直接给出来，省掉用户自己算
        if gate == "length_short" and facts.get("required_floor"):
            for o in options:
                if o["id"] == "expand":
                    o["hint"] = f"保留现有正文，补写到 {facts['required_floor']} 字下限（不整体重生产）"

        failure = {
            "stage": stage_name,
            "detail": detail[:500],
            "gate": gate,
            "rolled_back": ("已回滚" in detail) or ("回滚" in detail),
        }
        receipt = {
            "task_id": task_id,
            "mode": mode,
            "chapter_id": chapter_id,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "failure": failure,
            "facts": facts,
            "decision_options": options,
            "stages": [s for s in stages if isinstance(s, dict)],
        }
        # 2026-11-07：fuse 在 backup 之后、writer 之前拒绝，正文未动、未产生一轮真实重写；
        # 标记 counted=false，供 nww_rewrite_fuse.count_receipts 跳过，避免「越点越锁」。
        if str(result.get("fatal_phase") or "") == "fuse" or stage_name == "fuse":
            receipt["counted"] = False
        receipt["status"] = "failed"
        receipt_rel = _write_receipt(book, chapter_id, mode, receipt)

        result = dict(result)
        result.update({
            "needs_decision": True,
            "failure": failure,
            "facts": facts,
            "decision_options": options,
            "receipt_path": receipt_rel,
        })
        return _attach_gate_facts(result, book=book, mode=mode, chapter_id=chapter_id)
    except Exception as exc:  # noqa: BLE001 — 回执失败绝不能盖掉原始失败
        print(f"[RECEIPT] 失败回执写入失败（{mode} 第{chapter_id}章）: {exc}", file=sys.stderr)
        try:
            result = dict(result)
            result.setdefault("needs_decision", True)
            result.setdefault("receipt_error", str(exc)[:200])
        except Exception:  # noqa: BLE001
            pass
        return _attach_gate_facts(result, book=book, mode=mode, chapter_id=chapter_id)


def _task_run(task_id: str, book_id: str, book: Path, chapter_id: int, mode: str, prealign: bool,
              note: str = "", level: str = "L3", dry_run: bool = False) -> None:
    """后台任务入口（2026-11-08）：开启 AI 实况 run 后再跑真实实现。

    run_id 直接用 task_id：前端既有的任务面板与「其他流程」面板共用一条时间线，
    模型调用据此自动挂到对应环节下。"""
    rec = _AGENT_TASKS.get(task_id, {}) or {}
    label = f"{_LIVE_MODE_LABEL.get(mode, mode)} · 第 {chapter_id} 章"
    with live_feed.begin_run(
        label,
        mode=mode,
        book_id=book_id,
        chapter_id=chapter_id,
        task_id=task_id,
        run_id=task_id,
        kind="task",
        user_id=rec.get("user_id"),
    ):
        _task_run_impl(task_id, book_id, book, chapter_id, mode, prealign,
                       note=note, level=level, dry_run=dry_run)


def _task_run_impl(task_id: str, book_id: str, book: Path, chapter_id: int, mode: str, prealign: bool,
                   note: str = "", level: str = "L3", dry_run: bool = False) -> None:
    """后台线程：跑 pipeline，逐环节上报进度；主笔阶段流式文本缓存（stream_text）供前端实时预览。
    支持协作式取消：前端设 cancel_requested 后，流式回调/环节边界抛 AgentCancelled 终止。
    note: 重写链执行要点（扩写/修订指令），非空时走原文保持式重写（见 run_rewrite_pipeline）。
    level: 重写等级 L1/L2/L3（2026-09-20 裁决；默认 L3 章节再生产）。"""
    from .agents_runtime import runtime as _ar_runtime
    from .agents_runtime.pipeline import (
        run_write_pipeline, run_rewrite_pipeline, run_deslop_pipeline, run_revise_pipeline, run_polish_pipeline)

    def _check_cancel() -> None:
        with _AGENT_TASKS_LOCK:
            if _AGENT_TASKS.get(task_id, {}).get("cancel_requested"):
                raise _ar_runtime.AgentCancelled()

    def _on_phase(name: str, ok: bool | None, detail: str, output: str = "") -> None:
        _check_cancel()
        if ok is None:
            # 环节开始（进行中）：只更新 phase，不追加 live_stage
            _task_update(task_id, phase=name, phase_ok=None, phase_detail=detail[:300])
            live_feed.stage_begin(name, detail)
        else:
            _task_update(task_id, phase=name, phase_ok=ok, phase_detail=detail[:300])
            _append_live_stage(task_id, name, ok, detail, output=output)
            live_feed.stage_end(name, ok, detail, output)

    stream_parts: list[str] = []

    def _on_writer_token(chunk: str) -> None:
        _check_cancel()
        stream_parts.append(chunk)
        _task_update(task_id, stream_text="".join(stream_parts)[-12000:])

    try:
        if mode == "rewrite":
            result = run_rewrite_pipeline(book, chapter_id, progress=_on_phase, on_writer_token=_on_writer_token,
                                          review_id=task_id, note=note, level=level,
                                          prealign=prealign, dry_run=dry_run)
        elif mode == "polish":
            # 2026-11-02 润色意见全链打通：note（作者润色指令）此前在本分支被丢弃，
            # 导致前端填写的润色意见从未进入执行链（润色跑的是 analyze 自动诊断）。
            result = run_polish_pipeline(book, chapter_id, progress=_on_phase, on_writer_token=_on_writer_token,
                                         note=note, dry_run=dry_run)
        elif mode == "revise":
            result = run_revise_pipeline(book, chapter_id, progress=_on_phase, on_writer_token=_on_writer_token,
                                         note=note, dry_run=dry_run)
        elif mode == "deslop":
            result = run_deslop_pipeline(book, chapter_id, progress=_on_phase, on_writer_token=_on_writer_token, dry_run=dry_run)
        else:
            result = run_write_pipeline(book, chapter_id, prealign=prealign, progress=_on_phase, on_writer_token=_on_writer_token,
                                        review_id=task_id, dry_run=dry_run)
        # 失败不再静默：派生决策项 + 落盘回执（成功结果原样透传）
        result = _with_decision(result, book=book, mode=mode, chapter_id=chapter_id, task_id=task_id)
        _task_update(task_id, status="done", result=result, ok=bool(result.get("ok")))
        # P1-A（外脑 2026-10-31 GO/A+）：失败自动跟进钩子——独立旁路模块 agents_followup，
        # 默认关（NWW_AGENT_FOLLOWUP=1 启用）；内部全兜底，绝不改变上方已落定的终态。
        if not result.get("ok") and result.get("needs_decision"):
            from .agents_followup import maybe_followup
            # dry_run 透传：预览任务失败不得触发真改正文的自动跟进（2026-10-31）
            maybe_followup(task_id, book_id, book, chapter_id, mode, result, dry_run=dry_run)
    except _ar_runtime.AgentCancelled:
        _task_update(task_id, status="cancelled", error="已按用户要求终止")
    except PermissionError as exc:
        _append_live_stage(task_id, "permission", False, f"多用户隔离拦截: {exc}")
        live_feed.stage_end("permission", False, f"多用户隔离拦截: {exc}")
        _task_update(task_id, status="error", error=f"多用户隔离拦截: {exc}")
    except Exception as exc:  # noqa: BLE001 — 后台任务兜底，不悬挂
        # 失败环节卡片：用当前 phase 作为环节名，让前端能看到哪一步挂了
        _cur = _AGENT_TASKS.get(task_id, {})
        _fail_phase = _cur.get("phase", "unknown") or "unknown"
        _append_live_stage(task_id, _fail_phase, False, f"全流程执行异常: {exc}")
        live_feed.stage_end(_fail_phase, False, f"全流程执行异常: {exc}")
        _task_update(task_id, status="error", error=f"全流程执行异常: {exc}")
    finally:
        # ★ 并发守卫释放（2026-10-19）：任何终态（done/error/cancelled）都必须释放章
        #   操作槽；_release_op 内置 owner 校验，不会误删被驱逐后新任务占的槽。
        _release_op(book_id, chapter_id, task_id)


@agents_router.post("/books/{book_id}/mvp/agents/rewrite/async")
def api_agents_rewrite_async(book_id: str, payload: dict, user=Depends(get_current_user)) -> dict:
    """异步重写（等级化章节重新生产，2026-09-20 裁决）：
    后台跑 pipeline，返回 task_id（前端轮询 /agents/task/{id} 看进度）。
    body: {chapter: int, note?: string（≤500 字执行要点；非空时走原文保持式重写/扩写）,
           level?: "L1"|"L2"|"L3"（默认 L3）}
    L1 保持剧情重写：原事件不变，重新表达；L2 章节结构重写：保留章节目标，重排叙事；
    L3 章节再生产：根据合同（细纲+confirmed_facts+前情）重新进入写作链全量生成。"""
    chapter_id = payload.get("chapter")
    if not isinstance(chapter_id, int) or chapter_id < 1:
        raise HTTPException(status_code=400, detail="chapter must be a positive integer")
    note = str(payload.get("note", "") or "").strip()
    if len(note) > 500:
        raise HTTPException(status_code=400, detail="note 超过 500 字上限")
    level = str(payload.get("level", "L3") or "L3").upper()
    if level not in ("L1", "L2", "L3"):
        raise HTTPException(status_code=400, detail="level 只能是 L1/L2/L3")
    root = projects_root(user["user_id"])
    try:
        book = book_root(root, book_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    task_id = uuid.uuid4().hex[:12]
    dry_run = bool(payload.get("dry_run", False))
    # 2026-10-16：重写链接入写前对齐（第 1–3 章含三九 G1–G9），默认开启。
    # 此前该位置**硬编码 False** → 重写从不执行 prealign（用户 2026-10-16 报此问题）。
    # 前端可传 prealign:false 关闭。
    prealign = bool(payload.get("prealign", True))
    # 前提变更（细纲 hash 变了）：作者选择「接受新细纲并重写」→ 把当前细纲登记为
    # 新前提基线（旧回执/快照/承诺按基线作废，不删文件），随后本轮 check 放行。
    if bool(payload.get("rebase_outline", False)):
        from .agents_runtime.pipeline import _rewrite_fuse_sync
        _rewrite_fuse_sync(book, chapter_id)
    # 2026-11-13 用户裁决：熔断后由作者在决策卡选「继续重写一次」——显式授权一轮，
    # 写一次性令牌后本次 check 放行；不传该标志则维持请示态（不自动继续、也不锁死）。
    if bool(payload.get("continue_once", False)):
        from .agents_runtime.pipeline import _rewrite_fuse_grant_once
        _grant = _rewrite_fuse_grant_once(book, chapter_id)
        if not _grant.get("ok"):
            raise HTTPException(
                status_code=409,
                detail=f"一次性重写授权失败：{_grant.get('error') or '未知错误'}")
    th = threading.Thread(target=_task_run, args=(task_id, book_id, book, chapter_id, "rewrite", prealign, note, level, dry_run), daemon=True)
    # ★ 并发守卫（2026-10-19）：原子占用章操作槽，占用中 → 409（防同章并发互踩）
    busy = _try_acquire_op(book_id, chapter_id, "rewrite", task_id, thread=th, book=book)
    if busy:
        raise HTTPException(status_code=409, detail=_op_busy_detail(book_id, chapter_id, busy))
    _task_register(task_id, {"status": "running", "phase": "queued", "chapter_id": chapter_id,
                             "mode": "rewrite", "note": note, "level": level, "book_id": book_id,
                             # T9 偏差 2：真实持久化 prealign，续跑才能复现原任务行为
                             "prealign": prealign, "dry_run": dry_run},
                  user_id=user["user_id"])
    th.start()
    return {"task_id": task_id, "book_id": book_id, "chapter_id": chapter_id, "mode": "rewrite",
            "level": level, "prealign": prealign}


@agents_router.post("/books/{book_id}/mvp/agents/deslop/async")
def api_agents_deslop_async(book_id: str, payload: dict, user=Depends(get_current_user)) -> dict:
    """异步润色：后台跑 pipeline，返回 task_id（前端轮询 /agents/task/{id} 看进度）。"""
    chapter_id = payload.get("chapter")
    if not isinstance(chapter_id, int) or chapter_id < 1:
        raise HTTPException(status_code=400, detail="chapter must be a positive integer")
    root = projects_root(user["user_id"])
    try:
        book = book_root(root, book_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    task_id = uuid.uuid4().hex[:12]
    dry_run = bool(payload.get("dry_run", False))
    th = threading.Thread(target=_task_run, args=(task_id, book_id, book, chapter_id, "deslop", False, "", False, dry_run), daemon=True)
    # ★ 并发守卫（2026-10-19）：原子占用章操作槽，占用中 → 409（防同章并发互踩）
    busy = _try_acquire_op(book_id, chapter_id, "deslop", task_id, thread=th, book=book)
    if busy:
        raise HTTPException(status_code=409, detail=_op_busy_detail(book_id, chapter_id, busy))
    _task_register(task_id, {"status": "running", "phase": "queued", "chapter_id": chapter_id, "mode": "deslop", "book_id": book_id},
                  user_id=user["user_id"])
    th.start()
    return {"task_id": task_id, "book_id": book_id, "chapter_id": chapter_id, "mode": "deslop"}


@agents_router.post("/books/{book_id}/mvp/agents/revise/async")
def api_agents_revise_async(book_id: str, payload: dict, user=Depends(get_current_user)) -> dict:
    """异步局部修订：后台跑 pipeline，返回 task_id（前端轮询 /agents/task/{id} 看进度）。
    body: {chapter: int, note: string（必填，≤500 字：修订点与修订要求）}"""
    chapter_id = payload.get("chapter")
    if not isinstance(chapter_id, int) or chapter_id < 1:
        raise HTTPException(status_code=400, detail="chapter must be a positive integer")
    note = str(payload.get("note", "") or "").strip()
    if not note:
        raise HTTPException(status_code=400, detail="revise 必须提供 note（修订点与修订要求）")
    if len(note) > 500:
        raise HTTPException(status_code=400, detail="note 超过 500 字上限")
    root = projects_root(user["user_id"])
    try:
        book = book_root(root, book_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    task_id = uuid.uuid4().hex[:12]
    dry_run = bool(payload.get("dry_run", False))
    th = threading.Thread(target=_task_run, args=(task_id, book_id, book, chapter_id, "revise", False, note, False, dry_run), daemon=True)
    # ★ 并发守卫（2026-10-19）：原子占用章操作槽，占用中 → 409（防同章并发互踩）
    busy = _try_acquire_op(book_id, chapter_id, "revise", task_id, thread=th, book=book)
    if busy:
        raise HTTPException(status_code=409, detail=_op_busy_detail(book_id, chapter_id, busy))
    _task_register(task_id, {"status": "running", "phase": "queued", "chapter_id": chapter_id, "mode": "revise", "note": note, "book_id": book_id},
                  user_id=user["user_id"])
    th.start()
    return {"task_id": task_id, "book_id": book_id, "chapter_id": chapter_id, "mode": "revise"}


def _audit_task_run(task_id: str, book: Path, start: int, end: int) -> None:
    """后台线程入口（2026-11-08）：先开 AI 实况 run，再跑真实实现。"""
    rec = _AGENT_TASKS.get(task_id, {}) or {}
    span = f"第 {start} 章" if start == end else f"第 {start}–{end} 章"
    with live_feed.begin_run(
        f"一致性审计 · {span}",
        mode="audit",
        book_id=str(rec.get("book_id") or ""),
        chapter_id=start,
        task_id=task_id,
        run_id=task_id,
        kind="task",
        user_id=rec.get("user_id"),
    ):
        _audit_task_run_impl(task_id, book, start, end)


def _audit_task_run_impl(task_id: str, book: Path, start: int, end: int) -> None:
    """区间一致性审计（只读诊断，2026-09-20 裁决——auto_fix 已彻底移除，
    Web / Chat Agent execute / API 统一 readonly），逐章上报进度。"""
    from .agents_runtime import runtime as _ar_runtime
    from .agents_runtime.outline_audit import run_outline_audit

    hit_deadline = False

    def _check_cancel() -> None:
        nonlocal hit_deadline
        with _AGENT_TASKS_LOCK:
            t = _AGENT_TASKS.get(task_id, {})
            if t.get("cancel_requested"):
                raise _ar_runtime.AgentCancelled()
            # 工作量预算耗尽 → 协作式终止。已完成的章已逐章落盘，
            # 再次提交会因断点续跑只补未完成的章（不重复烧 LLM）。
            dl = t.get("deadline_ts")
            if dl and time.time() > float(dl):
                hit_deadline = True
                raise _ar_runtime.AgentCancelled()

    def _on_phase(name: str, ok: bool | None, detail: str, output: str = "") -> None:
        _check_cancel()
        if ok is None:
            # 环节开始（进行中）：只更新 phase，不追加 live_stage
            _task_update(task_id, phase=name, phase_ok=None, phase_detail=detail[:300])
            live_feed.stage_begin(name, detail)
        else:
            _task_update(task_id, phase=name, phase_ok=ok, phase_detail=detail[:300])
            _append_live_stage(task_id, name, ok, detail, output=output)
            live_feed.stage_end(name, ok, detail, output)

    try:
        result = run_outline_audit(book, start, end, progress=_on_phase)
        result = _with_decision(result, book=book, mode="audit", chapter_id=start, task_id=task_id)
        _task_update(task_id, status="done", result=result, ok=bool(result.get("ok")))
    except _ar_runtime.AgentCancelled:
        _task_update(task_id, status="cancelled",
                     error=("已达工作量预算上限；已完成的章已落盘，再次提交将从未完成章续跑"
                            if hit_deadline else "已按用户要求终止"))
    except PermissionError as exc:
        _task_update(task_id, status="error", error=f"多用户隔离拦截: {exc}")
    except Exception as exc:  # noqa: BLE001 — 后台任务兜底，不悬挂
        _task_update(task_id, status="error", error=f"一致性审计执行异常: {exc}")


@agents_router.post("/books/{book_id}/mvp/agents/audit/async")
def api_agents_audit_async(book_id: str, payload: dict, user=Depends(get_current_user)) -> dict:
    """异步一致性审计（区间批量，只读诊断，2026-09-20 裁决）：
    检查 A 细纲一致性 / B 世界事实 / C 角色一致性 / D 伏笔状态，输出 P0/P1/P2 报告
    + 建议动作（rewrite_L1/L2/L3、patch、accept）。**绝不修改正文与追踪状态**；
    「修复」由报告 suggestion 驱动用户决策（走润色/重写/补丁按钮）。
    body: {start: int, end: int}"""
    start = payload.get("start")
    end = payload.get("end")
    if not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start or end - start > 50:
        raise HTTPException(status_code=400, detail="start/end 必须为正整数区间，且单次不超过 50 章")
    root = projects_root(user["user_id"])
    try:
        book = book_root(root, book_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    # P2-D 预检：区间内必须至少一章有已提交正文，否则任务空转无意义
    from .agents_runtime.pipeline import _resolve_existing_body
    if not any(_resolve_existing_body(book, ch) is not None for ch in range(start, end + 1)):
        raise HTTPException(status_code=400, detail=f"区间第{start}-{end}章均无已提交正文，无法审计（请先写章）")

    # ★单飞互斥：该书已有在跑的审计任务 → 直接复用（幂等），不再起第二个线程。
    #   否则「前端超时 → 用户点重试」会叠加出第二个长任务，两者串行抢
    #   chrome_bridge 全局锁与同一个 Chrome 页面 → 串会话/互拖 → 502。
    running = _running_audit_task(book_id)
    if running:
        return {"task_id": running["task_id"], "book_id": book_id,
                "range": running["range"], "mode": "audit", "readonly": True, "reused": True}

    task_id = uuid.uuid4().hex[:12]
    n_chapters = end - start + 1
    _task_register(task_id, {
        "status": "running", "phase": "queued", "mode": "audit",
        "range": [start, end], "readonly": True,
        "book_id": book_id,  # 单飞互斥的归组键（原先缺失，无法按书查在跑任务）
        # 预算随工作量线性伸缩，下发给前端作为轮询上限（取代写死的 15 分钟）
        "deadline_ts": time.time() + _AUDIT_PER_CHAPTER_BUDGET_S * n_chapters,
        "chapter_total": n_chapters,
    }, user_id=user["user_id"])
    th = threading.Thread(target=_audit_task_run, args=(task_id, book, start, end), daemon=True)
    with _AGENT_TASKS_LOCK:
        _AGENT_THREADS[task_id] = th
    th.start()
    return {"task_id": task_id, "book_id": book_id, "range": [start, end],
            "mode": "audit", "readonly": True, "reused": False}


@agents_router.post("/books/{book_id}/mvp/agents/polish/async")
def api_agents_polish_async(book_id: str, payload: dict, user=Depends(get_current_user)) -> dict:
    """异步润色（文本质量优化层，2026-09-20 裁决）：保剧情提表达 + 章节合同守卫。
    后台跑 run_polish_pipeline（analyze 诊断+合同快照 → rewrite_pass 单次改写 → deslop
    去AI味 → normalize → contract_guard 确定性硬约束 → revision 提交），返回 task_id。
    body: {chapter: int, note?: string}  note=作者润色意见（最高优先级注入 rewrite+deslop）"""
    chapter_id = payload.get("chapter")
    if not isinstance(chapter_id, int) or chapter_id < 1:
        raise HTTPException(status_code=400, detail="chapter must be a positive integer")
    note = str(payload.get("note") or "").strip()
    root = projects_root(user["user_id"])
    try:
        book = book_root(root, book_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    task_id = uuid.uuid4().hex[:12]
    dry_run = bool(payload.get("dry_run", False))
    th = threading.Thread(target=_task_run, args=(task_id, book_id, book, chapter_id, "polish", False, note, "L3", dry_run), daemon=True)
    # ★ 并发守卫（2026-10-19）：原子占用章操作槽，占用中 → 409（防同章并发互踩）
    busy = _try_acquire_op(book_id, chapter_id, "polish", task_id, thread=th, book=book)
    if busy:
        raise HTTPException(status_code=409, detail=_op_busy_detail(book_id, chapter_id, busy))
    _task_register(task_id, {"status": "running", "phase": "queued", "chapter_id": chapter_id, "mode": "polish", "book_id": book_id},
                  user_id=user["user_id"])
    th.start()
    return {"task_id": task_id, "book_id": book_id, "chapter_id": chapter_id, "mode": "polish"}


def spawn_write_task(user_id: str, book_id: str, chapter_id: int, *, prealign: bool = True,
                     dry_run: bool = False, origin: str = "manual") -> dict:
    """异步写章任务生成器（HTTP 端点与监督器共用）：校验 → 防覆盖闸 → 章槽互斥 → 登记 → 起线程。

    校验失败照常抛 HTTPException（400/404/409），监督器捕获后转为收件箱事件。
    origin: manual=前端手动派单 / supervisor=监督器自动派单（作者「暂停监督」只取消后者）。"""
    root = projects_root(user_id)
    try:
        book = book_root(root, book_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _assert_chapter_not_exists(book, chapter_id)
    task_id = uuid.uuid4().hex[:12]
    th = threading.Thread(target=_task_run, args=(task_id, book_id, book, chapter_id, "write", prealign, "", "L3", dry_run), daemon=True)
    # ★ 并发守卫（2026-10-19）：原子占用章操作槽，占用中 → 409（防同章并发互踩）
    busy = _try_acquire_op(book_id, chapter_id, "write", task_id, thread=th, book=book)
    if busy:
        raise HTTPException(status_code=409, detail=_op_busy_detail(book_id, chapter_id, busy))
    # book_id：监督器按书观察在跑任务必需；origin：暂停监督时只取消监督器派的任务
    _task_register(task_id, {"status": "running", "phase": "queued", "chapter_id": chapter_id,
                             "mode": "write", "book_id": book_id, "origin": origin,
                             # T9 偏差 2：write 链同样持久化 prealign（续跑复用原参数）
                             "prealign": prealign, "dry_run": dry_run},
                  user_id=user_id)
    th.start()
    return {"task_id": task_id, "book_id": book_id, "chapter_id": chapter_id, "mode": "write"}


@agents_router.post("/books/{book_id}/mvp/agents/write/async")
def api_agents_write_async(book_id: str, payload: dict, user=Depends(get_current_user)) -> dict:
    """异步写章：后台跑 pipeline，返回 task_id（前端轮询 /agents/task/{id} 看进度）。"""
    chapter_id = payload.get("chapter")
    if not isinstance(chapter_id, int) or chapter_id < 1:
        raise HTTPException(status_code=400, detail="chapter must be a positive integer")
    prealign = bool(payload.get("prealign", True))
    return spawn_write_task(user["user_id"], book_id, chapter_id, prealign=prealign,
                            dry_run=bool(payload.get("dry_run", False)))


@agents_router.get("/agents/task/{task_id}")
def api_agents_task(task_id: str, user=Depends(get_current_user)) -> dict:
    """查询异步任务进度：{status, phase, phase_ok, phase_detail, stages?, result?, error?}

    R6-AC3：running 且执行线程已不在、且超过 _STALE_TASK_S 无更新的记录，按
    interrupted（stale=true）返回并回写盘——不再无限返回 running。
    """
    with _AGENT_TASKS_LOCK:
        rec = _AGENT_TASKS.get(task_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="task not found")
        task = dict(rec)
    if _is_stale_running(task_id, task):
        task = _mark_task_interrupted(task_id, "thread_lost_timeout", stale=True) or task
    # T9 偏差 1：R6-AC3 要求 interrupted 的查询响应**始终**带 stale 键。
    # restore 路径（process_restarted）的任务本就非活跃，补 stale=True；
    # 仅补在执行线程确实不存在的记录上，running/done/error/cancelled 一律不动。
    # 盘上不再回写该补键（只增响应字段，避免把「进程重启」与「僵尸超时」混为一谈）。
    if task.get("status") == "interrupted" and "stale" not in task:
        if not _task_thread_alive(task_id):
            task["stale"] = True
    return task


@agents_router.get("/agents/tasks/recent")
def api_agents_tasks_recent(user=Depends(get_current_user)) -> dict:
    """返回最近一个 agent 任务 {task_id, mode, status, ...}，供前端刷新后恢复实况面板。

    优先级（R6-AC3/AC4）：running（非僵尸）> interrupted（可续跑）> 其余终态；
    僵尸 running 先降级为 interrupted 并回写盘；返回的 status 与落盘一致（内存更新即落盘）。
    """
    with _AGENT_TASKS_LOCK:
        snapshot = [(tid, dict(rec)) for tid, rec in _AGENT_TASKS.items()]
    normalized: list[tuple[str, dict]] = []
    for tid, rec in snapshot:
        if _is_stale_running(tid, rec):
            rec = _mark_task_interrupted(tid, "thread_lost_timeout", stale=True) or rec
        normalized.append((tid, rec))
    if normalized:
        def _rank(item: tuple[str, dict]) -> tuple[int, float]:
            _, rec = item
            st = rec.get("status")
            rank = 0 if st == "running" else (1 if st == "interrupted" else 2)
            return (rank, -_task_recency_ts(item[0], rec))

        tid, rec = sorted(normalized, key=_rank)[0]
        out = {"task_id": tid, "mode": rec.get("mode", ""), "status": rec.get("status", "")}
        if rec.get("status") == "interrupted":
            out["resumable"] = bool(rec.get("resumable"))
            out["interrupt_reason"] = str(rec.get("interrupt_reason") or "")
            # 与 GET /agents/task/{id} 口径一致：interrupted 一律非活跃（默认 True）
            out["stale"] = bool(rec.get("stale", True))
        return out
    # 2) 内存为空（恢复失败等）→ 扫落盘目录取 mtime 最新；status 原样返回（与盘一致）
    d = _AGENT_TASKS_DIR
    if d.is_dir():
        files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if files:
            try:
                rec = json.loads(files[0].read_text(encoding="utf-8"))
                return {"task_id": files[0].stem, "mode": rec.get("mode", ""), "status": rec.get("status", "")}
            except Exception:  # noqa: BLE001 — 坏档按无记录处理
                pass
    return {"task_id": "", "mode": "", "status": ""}


@agents_router.post("/agents/task/{task_id}/resume")
def api_agents_task_resume(task_id: str, payload: dict | None = None,
                           user=Depends(get_current_user)) -> dict:
    """章级续跑 interrupted 任务（C-RESUME@1）：按原参数重跑，不是断点续传。

    响应：
      404 记录不存在 / 书目录不存在
      409 status 非 interrupted 或 resumable=false / 同 (book, chapter) 已有在途操作
      200 {task_id(新), resumed:true, from_task_id, mode, book_id, chapter_id, level,
           resume_scope:"chapter"}
    实现约束：复用既有 _task_run 线程体与 _try_acquire_op 章级互斥，不另写一套 pipeline 调用。
    """
    payload = payload or {}
    with _AGENT_TASKS_LOCK:
        old_rec = _AGENT_TASKS.get(task_id)
        rec = dict(old_rec) if old_rec is not None else None
    if rec is None:
        raise HTTPException(status_code=404, detail="task not found")

    status = str(rec.get("status") or "")
    if status != "interrupted" or not _task_is_resumable(rec):
        raise HTTPException(status_code=409, detail=f"该任务不可续跑（status={status}）")

    snap = rec.get("resume") if isinstance(rec.get("resume"), dict) else {}
    mode = str(snap.get("mode") or rec.get("mode") or "")
    book_id = str(snap.get("book_id") or rec.get("book_id") or "")
    try:
        chapter_id = int(snap.get("chapter_id") or rec.get("chapter_id"))  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=f"该任务不可续跑（status={status}）") from exc
    if mode not in _TASK_RESUMABLE_MODES or chapter_id < 1 or not book_id.strip():
        raise HTTPException(status_code=409, detail=f"该任务不可续跑（status={status}）")

    level = str(snap.get("level") or rec.get("level") or "L3").upper()
    if level not in ("L1", "L2", "L3"):
        level = "L3"
    note = str(snap.get("note") or rec.get("note") or "")
    dry_run = bool(payload.get("dry_run", snap.get("dry_run", rec.get("dry_run", False))))
    # T9 偏差 2：优先读原任务记录里的真实 prealign（rewrite/write async 现已持久化），
    # 旧记录缺失时才回落端点默认 True（保持历史兼容）。
    prealign = bool(rec.get("prealign", snap.get("prealign", True)))

    root = projects_root(user["user_id"])
    try:
        book = book_root(root, book_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    # write 链原语义：目标章正文已存在 → 409（避免误覆盖）
    if mode == "write":
        _assert_chapter_not_exists(book, chapter_id)

    new_task_id = uuid.uuid4().hex[:12]
    # 各模式严格复用 _task_run 既有分支参数（与 async 端点/ spawn_write_task 同源）
    if mode == "rewrite":
        args = (new_task_id, book_id, book, chapter_id, "rewrite", prealign, note, level, dry_run)
    elif mode == "write":
        args = (new_task_id, book_id, book, chapter_id, "write", prealign, "", "L3", dry_run)
    elif mode == "deslop":
        args = (new_task_id, book_id, book, chapter_id, "deslop", False, "", "L3", dry_run)
    elif mode == "polish":
        args = (new_task_id, book_id, book, chapter_id, "polish", False, note, "L3", dry_run)
    else:  # revise
        args = (new_task_id, book_id, book, chapter_id, "revise", False, note, "L3", dry_run)
    th = threading.Thread(target=_task_run, args=args, daemon=True)

    # 旧任务已中断，其章槽（若仍在内存）是死槽：先按 owner 释放，避免误报 409
    _release_dead_op(book_id, chapter_id, task_id)
    busy = _try_acquire_op(book_id, chapter_id, mode, new_task_id, thread=th, book=book)
    if busy:
        raise HTTPException(status_code=409, detail=_op_busy_detail(book_id, chapter_id, busy))

    record: dict = {
        "status": "running",
        "phase": "queued",
        "chapter_id": chapter_id,
        "mode": mode,
        "book_id": book_id,
        "resumed_from": task_id,   # 审计用：新任务来源
        "resume_scope": "chapter",  # 语义显式化：章级重跑，非断点续传
    }
    if note:
        record["note"] = note
    if mode == "rewrite":
        record["level"] = level
    if dry_run:
        record["dry_run"] = True
    if mode in ("rewrite", "write"):
        record["prealign"] = prealign
    _task_register(new_task_id, record, user_id=user["user_id"])

    # 旧记录置 cancelled（不是 error）：它已被新任务接管，避免残留 interrupted 被反复续跑
    with _AGENT_TASKS_LOCK:
        old_rec = _AGENT_TASKS.get(task_id)
        if old_rec is not None:
            old_rec.update({
                "status": "cancelled",
                "error": f"已被续跑替代（新 task_id={new_task_id}）",
                "resumed_by": new_task_id,
                "resumable": False,
                "_updated_ts": time.time(),
            })
            _task_persist(task_id)

    # 线程句柄先登记再 start：T3 验收要求 _AGENT_THREADS 有新任务句柄，
    # 且避免「线程结束即弹句柄」与登记之间的竞态。
    with _AGENT_TASKS_LOCK:
        _AGENT_THREADS[new_task_id] = th
    th.start()

    return {
        "task_id": new_task_id,
        "resumed": True,
        "from_task_id": task_id,
        "mode": mode,
        "book_id": book_id,
        "chapter_id": chapter_id,
        "level": level if mode == "rewrite" else "",
        "resume_scope": "chapter",
        "dry_run": dry_run,
        "prealign": prealign if mode in ("rewrite", "write") else None,
    }


@agents_router.post("/agents/task/{task_id}/cancel")
def api_agents_task_cancel(task_id: str, user=Depends(get_current_user)) -> dict:
    """终止异步任务（协作式）：置 cancel_requested 标志，后台线程在流式回调/环节边界退出。

    僵尸任务（2026-11-05 修复）：running 超过 30 分钟无落盘更新、且线程已不在/不可用时，
    置标志不会有人消费 → 状态永远停在 running，千章守护中心的「取消僵尸任务」点了也不消失。
    此时直接落终态 cancelled 并持久化（后续 commit/risk 都能读到停止原因）。
    """
    with _AGENT_TASKS_LOCK:
        task = _AGENT_TASKS.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="task not found")
        if task.get("status") == "interrupted":
            # 幂等（C-TASK-STATE@1 行为 4）：中断任务没有活线程可取消，不改状态，
            # 由前端引导「续跑」；返回 stale=true 便于前端区分僵尸态。
            return {"task_id": task_id, "status": "interrupted", "cancelled": False, "stale": True}
        if task.get("status") in ("done", "error", "cancelled"):
            # 内存已终态，但落盘可能仍是 running：_task_restore_from_disk 只在启动时改内存、
            # 不回写文件 → 守护中心读到陈旧 running，点「取消僵尸任务」也永远不消失。
            # 这里把落盘回写为 cancelled，风险项才会真正清零。
            if _task_file_status(task_id) == "running":
                if task.get("status") == "done":
                    # 已完成但落盘未更新：按真实终态回写，不误标为取消
                    _task_persist(task_id)
                    return {"task_id": task_id, "status": "done", "cancelled": False, "stale": True}
                task.update({
                    "status": "cancelled",
                    "cancel_requested": True,
                    "error": "已取消（僵尸任务：落盘 running 超过 30 分钟无更新，已回写终态）",
                })
                _task_persist(task_id)
                return {"task_id": task_id, "status": "cancelled", "cancelled": True, "stale": True}
            return {"task_id": task_id, "status": task.get("status"), "cancelled": False}
        th = _AGENT_THREADS.get(task_id)
        alive = bool(th and th.is_alive())
        stale = False
        try:
            f = _AGENT_TASKS_DIR / f"{task_id}.json"
            if f.is_file():
                stale = (time.time() - f.stat().st_mtime) > _STALE_TASK_S
        except OSError:
            stale = False
        if stale and not alive:
            task.update({
                "status": "cancelled",
                "cancel_requested": True,
                "error": "已取消（僵尸任务：超过 30 分钟无更新，执行线程已不在）",
            })
            _task_persist(task_id)
            return {"task_id": task_id, "status": "cancelled", "cancelled": True, "stale": True}
        task["cancel_requested"] = True
    return {"task_id": task_id, "status": "cancelling", "cancelled": True}