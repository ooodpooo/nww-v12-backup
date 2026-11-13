"""跨进程章级互斥（契约 C-LOCK@1）。

锁文件 `<book>/追踪/.locks/chapter_{chapter_id:03d}.lock`：首字节为哨兵，
其后是持有人元数据 JSON。互斥由文件锁承担——POSIX 用
`fcntl.flock(LOCK_EX|LOCK_NB)`，Windows 用 `msvcrt.locking(LK_NBLCK)` 锁首字节。
两个平台都不可用时降级为进程内锁，`available()` 随之返回 False。

进程退出时文件锁随 fd 关闭由操作系统释放，崩溃不留死锁；锁文件里残留的
元数据由 `probe` 的 pid 存活判定剔除。

消费方（`nwwmvp` 的章操作入口）：

    handle = chapter_lock.acquire(book, chapter_id, "write", task_id)
    if handle is None:
        busy = chapter_lock.probe(book, chapter_id)   # {mode, task_id, pid, acquired_at}
        ...
    try:
        ... 正文覆写 + delta + commit ...
    finally:
        handle.release()
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

LOCK_DIR_REL = ("追踪", ".locks")
LOCK_FILE_SUFFIX = ".lock"

# 锁文件首字节：Windows 的 msvcrt.locking 需要锁一个实际存在的字节区间。
META_SENTINEL = b"x"

try:
    import fcntl as _fcntl
except ImportError:  # 平台分支：Windows 无 fcntl
    _fcntl = None

try:
    import msvcrt as _msvcrt
except ImportError:  # 平台分支：POSIX 无 msvcrt
    _msvcrt = None

_BACKEND = "fcntl" if _fcntl is not None else ("msvcrt" if _msvcrt is not None else "inproc")

# 降级模式的进程内互斥表，键为锁文件绝对路径。
_INPROC_LOCKS: dict[str, threading.Lock] = {}
_INPROC_GUARD = threading.Lock()


class LockHandle:
    """已持有的章锁。`release()` 幂等，重复调用与并发调用都安全。"""

    __slots__ = ("_path", "_meta", "_fd", "_inproc", "_released", "_guard")

    def __init__(self, path: Path, meta: dict[str, Any], fd: int,
                 inproc: threading.Lock | None) -> None:
        self._path = path
        self._meta = dict(meta)
        self._fd = fd
        self._inproc = inproc
        self._released = False
        self._guard = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def mode(self) -> str:
        return str(self._meta.get("mode") or "")

    @property
    def task_id(self) -> str:
        return str(self._meta.get("task_id") or "")

    @property
    def pid(self) -> int:
        return int(self._meta.get("pid") or 0)

    @property
    def acquired_at(self) -> str:
        return str(self._meta.get("acquired_at") or "")

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "task_id": self.task_id,
            "pid": self.pid,
            "acquired_at": self.acquired_at,
        }

    def release(self) -> None:
        with self._guard:
            if self._released:
                return
            self._released = True
            fd, self._fd = self._fd, -1
            if fd >= 0:
                try:
                    _clear_meta(fd)
                except OSError:
                    pass
                try:
                    _file_unlock(fd)
                except OSError:
                    pass
                try:
                    os.close(fd)
                except OSError:
                    pass
            if self._inproc is not None:
                self._inproc.release()
                self._inproc = None

    def __enter__(self) -> "LockHandle":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        self.release()
        return False


def available() -> bool:
    """True 表示互斥覆盖到进程之外；False 表示仅进程内降级锁。"""
    return _BACKEND != "inproc"


def backend() -> str:
    """当前实现："fcntl" / "msvcrt" / "inproc"，供诊断与测试断言。"""
    return _BACKEND


def lock_path(book: Path, chapter_id: int) -> Path:
    return Path(book).joinpath(*LOCK_DIR_REL, f"chapter_{int(chapter_id):03d}{LOCK_FILE_SUFFIX}")


def acquire(book: Path, chapter_id: int, mode: str, task_id: str) -> LockHandle | None:
    """非阻塞占用章锁：成功返回句柄，被占返回 None。"""
    path = lock_path(book, chapter_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "mode": str(mode),
        "task_id": str(task_id),
        "pid": os.getpid(),
        "acquired_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    inproc: threading.Lock | None = None
    if _BACKEND == "inproc":
        inproc = _inproc_lock(path)
        if not inproc.acquire(blocking=False):
            return None
    fd = -1
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        _ensure_sentinel(fd)
        if not _file_lock(fd):
            os.close(fd)
            fd = -1
            if inproc is not None:
                inproc.release()
            return None
    except OSError:
        if fd >= 0:
            os.close(fd)
        if inproc is not None:
            inproc.release()
        return None
    try:
        _write_meta(fd, meta)
    except OSError:
        # 元数据只服务于 probe 的诊断输出，写失败不影响已到手的互斥。
        pass
    return LockHandle(path=path, meta=meta, fd=fd, inproc=inproc)


def probe(book: Path, chapter_id: int) -> dict | None:
    """返回当前持有人 `{mode, task_id, pid, acquired_at}`；无持有人返回 None。"""
    meta = _read_meta(lock_path(book, chapter_id))
    if meta is None:
        return None
    if not _holder_alive(meta.get("pid")):
        return None
    return {
        "mode": meta.get("mode"),
        "task_id": meta.get("task_id"),
        "pid": meta.get("pid"),
        "acquired_at": meta.get("acquired_at"),
    }


def _inproc_lock(path: Path) -> threading.Lock:
    key = os.path.abspath(str(path))
    with _INPROC_GUARD:
        lock = _INPROC_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _INPROC_LOCKS[key] = lock
        return lock


def _ensure_sentinel(fd: int) -> None:
    if os.fstat(fd).st_size < len(META_SENTINEL):
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, META_SENTINEL)


def _file_lock(fd: int) -> bool:
    if _BACKEND == "fcntl":
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            return True
        except OSError:
            return False
    if _BACKEND == "msvcrt":
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            _msvcrt.locking(fd, _msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    return True


def _file_unlock(fd: int) -> None:
    if _BACKEND == "fcntl":
        _fcntl.flock(fd, _fcntl.LOCK_UN)
    elif _BACKEND == "msvcrt":
        os.lseek(fd, 0, os.SEEK_SET)
        _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)


def _write_meta(fd: int, meta: dict[str, Any]) -> None:
    payload = json.dumps(meta, ensure_ascii=False).encode("utf-8")
    os.lseek(fd, len(META_SENTINEL), os.SEEK_SET)
    os.write(fd, payload)
    os.ftruncate(fd, len(META_SENTINEL) + len(payload))


def _clear_meta(fd: int) -> None:
    os.ftruncate(fd, len(META_SENTINEL))


def _read_meta(path: Path) -> dict | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if not raw.startswith(META_SENTINEL):
        return None
    body = raw[len(META_SENTINEL):].strip()
    if not body:
        return None
    try:
        meta = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return meta if isinstance(meta, dict) else None


def _holder_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    # os.kill(pid, 0) 只在 POSIX 是纯探测；Windows 上任何 sig 都会终止目标进程。
    if _BACKEND != "fcntl":
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True