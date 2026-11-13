"""nww-novel-skill 远程无状态执行器。

架构约定（2026-09-06 用户定稿）：
  - 远程不存放任何书籍项目；本地 IDE/CLI 持有全部书稿与追踪状态
  - 本地把书项目（子集/全量）打包 zip 上传，远程在临时目录解压后执行白名单确定性脚本，
    返回 stdout/exit_code 以及指定产物的 base64 zip（如更新后的 追踪/_tracking-state.json）
  - 远程无任何 LLM 调用；写正文永远在本地完成

POST /api/skill/run  body:
  { "book": "<显示名/临时目录名>",
    "script": "<白名单脚本名>",
    "args": [...],
    "zip": "<base64 书项目 zip>",
    "outputs": ["追踪/_tracking-state.json", ...]   # 可选，跑完收集回传的相对路径
  }
resp:
  { "exit_code": int, "stdout": str, "stderr": str,
    "outputs_zip": "<base64 或 null>", "output_files": [相对路径...] }
"""
from __future__ import annotations

import base64
import binascii
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from fastapi import HTTPException

_SKILL_ROOT = Path(os.environ.get(
    "NWW_SKILL_ROOT",
    r"C:\nww\skills\nww-novel-skill",
))
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
_PY = os.environ.get("NWW_PYTHON", r"C:\Python311\python.exe")
_NODE = os.environ.get("NWW_NODE", "node")

# 允许被远程调用的脚本名（精确白名单，新增需改这里）
_ALLOWED = {
    "nww_track_commit.py",
    "nww_enforce.py",
    "nww_wordcount.py",
    "nww_phase.py",
    "nww_bible_check.py",
    "nww_cross_chapter_check.py",
    "nww_delta_audit.py",
    "nww_delta_draft.py",
    "nww_memory_pack.py",
    "nww_era_check.py",
    "nww_expression_audit.py",
    "nww_name_check.py",
    "nww_sequence_audit.py",
    "nww_chapter_tail_gate.py",  # 2026-10-11：章尾动力类型轮换闸（rhythm/SKILL.md 判据的实现）
    "nww_style_fingerprint.py",
    "nww_voice_check.py",
    "nww_author_memory.py",
    "nww_backup.py",
    "nww_trope_guard.py",
    "check-degeneration.js",
    "check-ai-patterns.js",
    "check-outline-copy.js",
    "normalize-punctuation.js",
}
# 这些脚本必须显式传 --project；解压根即项目根，缺参时自动补 --project .
_NEED_PROJECT = {
    "nww_track_commit.py",
    "nww_delta_draft.py",
    "nww_memory_pack.py",
    "nww_delta_audit.py",
}

_SAFE_BOOK = re.compile(r"^[\w\u4e00-\u9fff][\w\u4e00-\u9fff .-]{0,80}$")
_MAX_ARGS = 24
_TIMEOUT_S = 300
_MAX_ZIP_MB = 30
_MAX_OUTPUTS = 30


def _unzip_safe(data: bytes, dest: Path) -> int:
    """防 zip-slip 解压，返回文件数。"""
    import io
    count = 0
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or ".." in name.split("/"):
                raise HTTPException(status_code=400, detail=f"zip 内非法路径：{info.filename}")
            if info.is_dir():
                continue
            target = dest / name
            if not str(target.resolve()).startswith(str(dest.resolve())):
                raise HTTPException(status_code=400, detail=f"zip 内非法路径：{info.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(info))
            count += 1
    return count


def _zip_dir(src: Path) -> bytes:
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(src.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(src).as_posix())
    return buf.getvalue()


def run_skill_payload(payload: dict) -> dict:
    book = str(payload.get("book") or "project")
    script = str(payload.get("script") or "")
    args = payload.get("args") or []
    outputs = payload.get("outputs") or []

    if not _SAFE_BOOK.match(book):
        raise HTTPException(status_code=400, detail="非法书名")
    if script not in _ALLOWED:
        raise HTTPException(status_code=403, detail=f"脚本不在白名单：{script}")
    if not isinstance(args, list) or len(args) > _MAX_ARGS:
        raise HTTPException(status_code=400, detail="args 必须为字符串列表且 ≤24 个")
    args = [str(a) for a in args]
    if any(len(a) > 400 for a in args):
        raise HTTPException(status_code=400, detail="单个参数过长")
    if not isinstance(outputs, list) or len(outputs) > _MAX_OUTPUTS:
        raise HTTPException(status_code=400, detail="outputs ≤30 个相对路径")
    outputs = [str(o).replace("\\", "/").strip("/") for o in outputs if str(o).strip()]

    zip_b64 = payload.get("zip") or ""
    if not zip_b64:
        raise HTTPException(status_code=400, detail="缺少 zip（书项目包）")
    try:
        data = base64.b64decode(zip_b64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="zip 不是合法 base64")
    if len(data) > _MAX_ZIP_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"zip 超过 {_MAX_ZIP_MB}MB 上限")

    script_path = _SCRIPTS_DIR / script
    if not script_path.is_file():
        raise HTTPException(status_code=404, detail=f"服务器上没有该脚本：{script}")

    safe_book = re.sub(r"[^\w]", "_", book)[:30]
    workdir = Path(tempfile.mkdtemp(prefix=f"nwwrun_{safe_book}_"))
    try:
        try:
            _unzip_safe(data, workdir)
        except zipfile.BadZipFile:
            raise HTTPException(status_code=400, detail="zip 损坏无法解压")

        if script.endswith(".py"):
            cmd = [_PY, str(script_path), *args]
        else:
            cmd = [_NODE, str(script_path), *args]
        if script in _NEED_PROJECT and "--project" not in args:
            cmd += ["--project", "."]

        child_env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        try:
            proc = subprocess.run(
                cmd, cwd=str(workdir), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=_TIMEOUT_S, env=child_env,
            )
            stdout, stderr, code = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=504, detail=f"脚本执行超时（>{_TIMEOUT_S}s）：{script}")
        except FileNotFoundError as exc:
            raise HTTPException(status_code=500, detail=f"运行时缺失：{exc}")

        # 收集产物：显式 outputs + 追踪/ 目录全量（track_commit commit 后本地需覆盖回写）
        produced = []
        for rel in outputs:
            p = workdir / rel
            if p.is_file():
                produced.append(p)
        tracking_dir = workdir / "追踪"
        if tracking_dir.is_dir():
            for p in sorted(tracking_dir.rglob("*")):
                if p.is_file() and p not in produced:
                    produced.append(p)
        out_files, out_bytes = [], b""
        if produced:
            import io
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in produced:
                    zf.write(p, p.relative_to(workdir).as_posix())
                    out_files.append(p.relative_to(workdir).as_posix())
            out_bytes = buf.getvalue()

        return {
            "book": book,
            "script": script,
            "exit_code": code,
            "stdout": stdout[-20000:],
            "stderr": stderr[-8000:],
            "outputs_zip": base64.b64encode(out_bytes).decode() if out_bytes else None,
            "output_files": out_files,
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
