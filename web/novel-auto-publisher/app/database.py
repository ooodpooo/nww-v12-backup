"""数据库初始化与操作：支持项目正文递归扫描与多卷解析"""
from __future__ import annotations

import aiosqlite
import hashlib
import os
import re
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).parent.parent
REPO_ROOT = Path(__file__).resolve().parents[3]
# 发布器数据源须与主书架一致：主后端按 projects/{user_id} 组织，单用户默认 admin。
# 通过 NWW_PUBLISHER_USER 注入真实 user_id，避免多用户时串书（旧实现硬编码 admin）。
_PUBLISHER_USER = os.environ.get("NWW_PUBLISHER_USER", "admin")
PROJECTS_DIR = REPO_ROOT / "projects" / _PUBLISHER_USER

DB_PATH = BASE_DIR / "data" / "publisher.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    draft_dir   TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS book_platforms (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id          INTEGER NOT NULL,
    platform         TEXT NOT NULL,
    platform_book_id TEXT DEFAULT '',
    platform_book_name TEXT DEFAULT '',
    schedule         TEXT DEFAULT '',
    chapters_per_run INTEGER DEFAULT 1,
    ai_content       INTEGER DEFAULT 0,
    enabled          INTEGER DEFAULT 1,
    last_published_at TIMESTAMP,
    -- 对账基准：平台上该书已发布到第几章。排期必须从 published_up_to + 1 起，避免重复发布。
    published_up_to   INTEGER,
    progress_checked_at TIMESTAMP,
    progress_source   TEXT DEFAULT '',
    FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS chapters (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id         INTEGER NOT NULL,
    file_path       TEXT NOT NULL UNIQUE,
    file_name       TEXT NOT NULL,
    chapter_number  INTEGER,
    title           TEXT NOT NULL,
    word_count      INTEGER DEFAULT 0,
    content_hash    TEXT DEFAULT '',
    status          TEXT DEFAULT 'pending',
    file_mtime      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS publish_records (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    chapter_id        INTEGER NOT NULL,
    platform          TEXT NOT NULL,
    platform_chapter_id TEXT DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'pending',
    scheduled_time    TIMESTAMP,
    actual_time       TIMESTAMP,
    error_message     TEXT DEFAULT '',
    retry_count       INTEGER DEFAULT 0,
    screenshot_path   TEXT DEFAULT '',
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (chapter_id) REFERENCES chapters(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_chapters_book_status ON chapters(book_id, status);
CREATE INDEX IF NOT EXISTS idx_records_platform_status ON publish_records(platform, status);
CREATE INDEX IF NOT EXISTS idx_records_scheduled ON publish_records(scheduled_time);
"""


# 后续版本新增的列（CREATE TABLE IF NOT EXISTS 不会改动已存在的老表，必须显式补列）
COLUMN_MIGRATIONS: dict[str, dict[str, str]] = {
    "chapters": {"file_mtime": "TIMESTAMP"},
    "book_platforms": {
        "published_up_to": "INTEGER",
        "progress_checked_at": "TIMESTAMP",
        "progress_source": "TEXT DEFAULT ''",
    },
}


async def migrate_schema(db: aiosqlite.Connection):
    """为历史库补齐新增列，避免老表缺列导致写入静默失败"""
    for table, columns in COLUMN_MIGRATIONS.items():
        cursor = await db.execute(f"PRAGMA table_info({table})")
        existing = {row[1] for row in await cursor.fetchall()}
        if not existing:
            continue
        for column, decl in columns.items():
            if column not in existing:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


async def init_db():
    """初始化数据库表"""
    async with aiosqlite.connect(DB_PATH) as db:
        # sync_all_projects / scan_book_chapters 均按列名取字段，必须开启 Row 工厂，
        # 否则 fetchone() 返回 tuple，row["id"] 会抛 TypeError 直接卡死启动流程。
        db.row_factory = aiosqlite.Row
        await db.executescript(SCHEMA)
        await migrate_schema(db)
        await db.commit()
        # 启动时自动做一次全局书稿项目的同步发现
        await sync_all_projects(db)


async def get_db():
    """获取数据库连接"""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        yield db
    finally:
        await db.close()


# ─────────────────────  章节解析工具  ─────────────────────

def parse_chapter_filename(filename: str) -> tuple[int | None, str]:
    """从文件名解析章节号和标题，如：第001章_葬书洞里的金字.md -> (1, 葬书洞里的金字)"""
    name = os.path.splitext(filename)[0]
    # 匹配 "第001章_标题" / "第1章 标题"
    m = re.match(r'^第?(\d+)[章回节][\s_-]*(.*)$', name)
    if m:
        num = int(m.group(1))
        title = m.group(2).strip()
        if not title:
            title = name
        return num, title
    # 尝试 "001_标题" 模式
    m = re.match(r'^(\d+)[_\s-]+(.*)$', name)
    if m:
        return int(m.group(1)), m.group(2).strip()
    return None, name


def count_words(text: str) -> int:
    """统计字数（去除空白）"""
    return len(re.sub(r'\s+', '', text))


def file_md5(filepath: str) -> str:
    """计算文件 MD5"""
    h = hashlib.md5()
    with open(filepath, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    return h.hexdigest()


def read_chapter_content(filepath: str) -> str:
    """读取章节文件内容，去除 Markdown 标题等修饰"""
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    content = re.sub(r'^#+\s*', '', content, flags=re.MULTILINE)
    content = re.sub(r'\*\*(.*?)\*\*', r'\1', content)
    content = re.sub(r'\*(.*?)\*', r'\1', content)
    content = re.sub(r'`(.*?)`', r'\1', content)
    content = re.sub(r'^\s*[-*]\s+', '', content, flags=re.MULTILINE)
    return content.strip()


# ─────────────────────  同步与扫描  ─────────────────────

async def sync_all_projects(db: aiosqlite.Connection):
    """自动把 projects/admin 目录下的所有书籍项目扫描并同步到数据库"""
    if not PROJECTS_DIR.exists():
        return

    for book_dir in sorted(PROJECTS_DIR.iterdir()):
        if not book_dir.is_dir() or book_dir.name.startswith(('.', '_')):
            continue
        if book_dir.name in ('.trash', 'e2e_agents_test'):
            continue

        book_name = book_dir.name
        rel_draft_dir = str(book_dir.relative_to(REPO_ROOT))

        cursor = await db.execute("SELECT id FROM books WHERE name = ?", (book_name,))
        row = await cursor.fetchone()
        if row:
            book_id = row["id"]
            await db.execute("UPDATE books SET draft_dir = ? WHERE id = ?", (rel_draft_dir, book_id))
        else:
            cursor = await db.execute(
                "INSERT INTO books (name, draft_dir) VALUES (?, ?)",
                (book_name, rel_draft_dir)
            )
            book_id = cursor.lastrowid

        await scan_book_chapters(book_id, book_dir, db)

    await db.commit()


async def scan_book_chapters(book_id: int, book_dir: Path, db: aiosqlite.Connection):
    """扫描某本书目录下的所有正文 Markdown 章节（支持 正文/各卷/*.md 递归结构）"""
    content_dir = book_dir / "正文"
    if not content_dir.exists():
        content_dir = book_dir

    # 收集所有的 .md 文件
    files: list[Path] = []
    for root, _, fnames in os.walk(content_dir):
        for fn in fnames:
            if fn.endswith(('.md', '.txt')) and not fn.startswith('.'):
                files.append(Path(root) / fn)

    files.sort(key=lambda p: p.name)

    cursor = await db.execute(
        "SELECT file_path, content_hash FROM chapters WHERE book_id = ?",
        (book_id,)
    )
    existing = {row["file_path"]: row["content_hash"] for row in await cursor.fetchall()}

    for fpath in files:
        rel_path = str(fpath.relative_to(REPO_ROOT))
        try:
            current_hash = file_md5(str(fpath))
            mtime = datetime.fromtimestamp(fpath.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            chapter_num, title = parse_chapter_filename(fpath.name)
            content = read_chapter_content(str(fpath))
            word_count = count_words(content)

            if rel_path in existing:
                if existing[rel_path] != current_hash:
                    await db.execute("""
                        UPDATE chapters
                        SET title=?, word_count=?, content_hash=?, chapter_number=?, file_mtime=?
                        WHERE file_path=?
                    """, (title, word_count, current_hash, chapter_num, mtime, rel_path))
            else:
                await db.execute("""
                    INSERT INTO chapters
                    (book_id, file_path, file_name, chapter_number, title, word_count, content_hash, status, file_mtime)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """, (book_id, rel_path, fpath.name, chapter_num, title, word_count, current_hash, mtime))
        except Exception as exc:
            # 单个文件失败不中断整次扫描，但必须留痕，否则会出现「同步成功却 0 章」的静默故障
            print(f"[scan] 跳过 {fpath}: {type(exc).__name__}: {exc}", flush=True)
            continue


async def scan_drafts(book_id: int, draft_dir: str, db: aiosqlite.Connection):
    """对外暴露的手动扫描接口"""
    p = REPO_ROOT / draft_dir
    if not p.exists():
        p = BASE_DIR / draft_dir
    if not p.exists():
        return {"added": 0, "skipped": 0, "errors": [f"路径不存在: {draft_dir}"]}

    await scan_book_chapters(book_id, p, db)
    await db.commit()
    return {"added": 1, "skipped": 0, "errors": []}
