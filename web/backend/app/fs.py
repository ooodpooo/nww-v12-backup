"""文件访问：路径安全 + V12 编辑权限（追踪/bible/事务文件只读）。"""
from __future__ import annotations

import re
from pathlib import Path

TRACKING_DIR = "追踪"
READONLY_TOP_LEVEL = {"bible.json", "init_tx.json", "state.json"}
# 事务/备份类文件一律只读，禁止网页端手改
READONLY_NAME_RES = [
    re.compile(r".*_tx\.json$"),      # ch1_tx.json / ch4_tx.json / .nww_ch3_tx.json
    re.compile(r"^_tracking-state\.json$"),
]

MAX_FILE_BYTES = 2 * 1024 * 1024  # 2MB 上限


def book_root(books_root: Path, book_id: str) -> Path:
    """把 book_id 解析为书目录，禁止目录穿越。"""
    root = (books_root / book_id).resolve()
    if not root.is_relative_to(books_root.resolve()):
        raise ValueError("invalid book id")
    if not root.is_dir():
        raise FileNotFoundError(f"book not found: {book_id}")
    return root


def resolve_path(book_root: Path, rel: str) -> Path:
    """把相对路径解析到书目录内，禁止越界。"""
    target = (book_root / rel).resolve()
    if not target.is_relative_to(book_root):
        raise ValueError("path escapes book directory")
    return target


def is_writable(book_root: Path, target: Path) -> bool:
    """V12 权限模型：追踪/** 与顶层契约/事务文件只读，其余可写。"""
    try:
        rel = target.relative_to(book_root)
    except ValueError:
        return False
    parts = rel.parts
    if not parts:
        return False
    if any(p.startswith(".") for p in parts):  # 隐藏文件/目录
        return False
    if parts[0] == TRACKING_DIR:
        return False
    if parts[0] in READONLY_TOP_LEVEL:
        return False
    name = parts[-1]
    if any(rx.fullmatch(name) for rx in READONLY_NAME_RES):
        return False
    return True


def read_file(book_root: Path, rel: str) -> tuple[Path, bytes]:
    target = resolve_path(book_root, rel)
    if not target.is_file():
        raise FileNotFoundError(f"file not found: {rel}")
    data = target.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("file too large")
    return target, data


# 文件名非法字符（Windows/POSIX 通配）：统一替换为下划线，避免写盘失败
_UNSAFE_NAME_RE = re.compile(r'[\\/:*?"<>|\r\n\t]')


def sanitize_name(name: str) -> str:
    """清洗文件名：去路径分隔符与控制字符，压缩首尾空白。"""
    return _UNSAFE_NAME_RE.sub("_", (name or "").strip()).strip()


def rename_file(book_root: Path, rel: str, new_name: str) -> tuple[Path, str]:
    """同目录内重命名单个文件（章节改标题用），返回 (新路径, 新相对路径)。

    约束（任一不满足即抛错，由路由层转成 4xx）：
      - 源/目标都必须可写（沿用 is_writable 权限模型）
      - new_name 只能是纯文件名，禁止目录穿越
      - 后缀必须与源文件一致（防止 .md 变 .py 之类的扩展名跳跃）
      - 目标不得已存在（不覆盖）
    """
    src = resolve_path(book_root, rel)
    if not src.is_file():
        raise FileNotFoundError(f"file not found: {rel}")
    if not is_writable(book_root, src):
        raise PermissionError(f"只读路径，禁止重命名：{rel}")

    clean = sanitize_name(new_name)
    if not clean or clean in {".", ".."}:
        raise ValueError("invalid new file name")
    if "/" in clean or "\\" in clean:
        raise ValueError("new file name must not contain path separators")
    if Path(clean).suffix.lower() != src.suffix.lower():
        raise ValueError(f"不允许改变文件后缀（原为 {src.suffix or '无后缀'}）")

    dest = src.parent / clean
    if not is_writable(book_root, dest):
        raise PermissionError(f"只读路径，禁止重命名到：{clean}")
    if dest.exists():
        raise FileExistsError(f"同名文件已存在：{clean}")

    src.rename(dest)
    new_rel = dest.relative_to(book_root).as_posix()
    return dest, new_rel
