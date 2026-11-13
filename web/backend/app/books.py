"""书架扫描与书内容（章节/目录树/追踪状态）读取。复用 nww_wordcount 权威字数口径。"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SKILL_SCRIPTS = _REPO_ROOT / "skills" / "nww-novel-skill" / "scripts"
if str(_SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SKILL_SCRIPTS))
import nww_wordcount  # noqa: E402  确定性字数核心，与流水线同源
import nww_track_commit  # noqa: E402  正式追踪状态唯一初始化入口
import nww_policy  # noqa: E402  commit 闸集合的唯一事实源（避免建书侧再写一份常量）

from .fs import book_root, is_writable as fs_is_writable, resolve_path  # noqa: E402

CHAPTER_RE = re.compile(r"^第0*(\d+)章")
OUTLINE_RE = re.compile(r"^细纲_第0*(\d+)章")

# 书籍身份标记（2026-11-07 副本识别闸）：create_book 落盘到书根目录，
# 形如 {"book_id": ..., "title": ..., "origin": "api", "created_at": ..., "marker_schema": 1}。
# 书架扫描时用它区分「真实书」与「shutil.copytree 复制出来的同名目录」——副本不会带这个文件。
BOOK_MARKER = ".nww-book.json"

# 副本识别闸开关：默认开启；设为 0/false/no 时关闭判定（回滚到旧行为）。
_COPY_GUARD_OFF_VALUES = {"0", "false", "no"}

# 辅助目录不是正文：_review_snapshots/ 存 review round 快照（第027章_round1.md），
# 文件名与正文章节同形，会被 rglob("第*章*.md") 一并捞出来 —— 章节列表里就会
# 凭空多出「第027章_round1」。只能按路径部件排除（文件名里没有可识别标记）。
_NON_PROSE_DIRS = ("_review_snapshots", "_archive")


def _is_non_prose(p: "Path") -> bool:
    """路径是否属于「非正文」（快照 / 归档 / 备份 / 损坏副本）。"""
    name = p.name
    if any(d in p.parts for d in _NON_PROSE_DIRS) or "_原稿_" in name:
        return True
    # 排除备份/损坏/复读副本：形如 .bak、.md.bak_xxx、_损坏_、_复读_
    if ".bak" in name or "_损坏_" in name or "_复读_" in name:
        return True
    return False

VOLUME_PLAN_RE = re.compile(r"^卷纲_第(\d+)卷\.md$")
CHAPTER_RANGE_RE = re.compile(r"第\s*(\d+)\s*[-–—]\s*(\d+)\s*章")


def _cn_num(n: int) -> str:
    """阿拉伯数字转中文数字（1-99），用于匹配正文目录名如「第一卷」。"""
    digits = "零一二三四五六七八九"
    if n < 10:
        return digits[n]
    if n < 20:
        return "十" + (digits[n % 10] if n % 10 else "")
    if n < 100:
        return digits[n // 10] + "十" + (digits[n % 10] if n % 10 else "")
    return str(n)


def parse_volume_plans(book: Path) -> tuple[int, dict[str, int], int]:
    """解析大纲目录的卷纲文件，返回 (卷总数, {卷目录名: 规划章节数}, 总规划章数)。

    卷纲文件名：卷纲_第N卷.md
    章节区间来源：文件内「本卷章节区间」段落中的「第 X-Y 章」。
    卷目录名映射：第1卷 → 第一卷（与正文/第一卷 目录名一致）。
    总规划章数：所有卷的章数之和，用于不分卷模式下显示"总X章"。
    """
    outline_dir = book / "大纲"
    if not outline_dir.is_dir():
        return 0, {}, 0
    plans: dict[str, int] = {}
    count = 0
    total = 0
    for f in sorted(outline_dir.glob("卷纲_第*卷.md")):
        m = VOLUME_PLAN_RE.match(f.name)
        if not m:
            continue
        count += 1
        vol_num = int(m.group(1))
        vol_name = f"第{_cn_num(vol_num)}卷"
        try:
            content = f.read_text(encoding="utf-8")
            range_match = CHAPTER_RANGE_RE.search(content)
            if range_match:
                start = int(range_match.group(1))
                end = int(range_match.group(2))
                if end >= start:
                    ch_count = end - start + 1
                    plans[vol_name] = ch_count
                    total += ch_count
        except (OSError, UnicodeError):
            pass
    return count, plans, total


def load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def tracking_path(book: Path) -> Path:
    return book / "追踪" / "_tracking-state.json"


def is_v12_book(book: Path) -> bool:
    """识别书籍项目：V12 格式（追踪/_tracking-state.json）或旧格式（idea.txt）。"""
    if not book.is_dir():
        return False
    if tracking_path(book).is_file():
        return True
    # 旧格式兼容：V9/V10 项目以 idea.txt 为根标记
    if (book / "idea.txt").is_file():
        return True
    return False


def is_uninitialized_v12_book(book: Path) -> bool:
    """识别「有 V12 目录结构但尚未 init」的书：换 IDE 工作时 init 中断的常见残骸。

    判定：目录存在 + （有 bible.json 或 有 正文/ 或 有 大纲/ 或 有 设定/）+ 无 tracking state + 无 idea.txt。
    这类书在 Web 面板中标记为「未初始化」，用户在书籍管理界面点「初始化本书」即可由服务端重新初始化。
    """
    if not book.is_dir():
        return False
    if tracking_path(book).is_file():
        return False
    if (book / "idea.txt").is_file():
        return False
    has_structure = (
        (book / "bible.json").is_file()
        or (book / "正文").is_dir()
        or (book / "大纲").is_dir()
        or (book / "设定").is_dir()
    )
    return has_structure


def initialize_book(book: Path) -> dict:
    """界面内初始化 / 重建追踪状态（2026-11-07 根治 CLI 命令串反模式）。

    按调用方已解析好的 book 目录执行（路径由服务端按当前登录用户解析），绝不回传绝对路径
    或命令字符串。决策：
    - 无 tracking：直接 initialize（重建 schema5 空状态）。
    - 残留 stub tracking（schema1 占位，无有效数据）：先删 stub 再 initialize（删除安全，与 create_book 注释一致）。
    - 已存在非 stub tracking（含 schema≠5 异常态）：拒绝覆盖有效数据，返回 manual_required 由人工核对。
    只写 追踪/_tracking-state.json，不触碰正文/大纲/设定。
    """
    tp = tracking_path(book)
    if tp.is_file():
        try:
            tracking = json.loads(tp.read_text(encoding="utf-8"))
        except Exception:
            tracking = {}
        if _is_legacy_stub_state(tracking):
            tp.unlink()
            mode = "repair_stub"
        else:
            return {
                "ok": False, "action": "manual_required", "book_id": book.name,
                "detail": "追踪状态已存在且非 stub（schema 异常或正常状态），为避免覆盖有效数据未做任何改动；"
                          "如为 schema 异常请人工核对 timeline/foreshadow/character_snapshots 后再处理。",
            }
    else:
        mode = "initialize"
    bible = load_json(book / "bible.json") or {}
    title = bible.get("book_title") or book.name
    init_doc = load_json(book / "初始化事务.json")
    if not init_doc:
        init_doc = _new_book_init_document(title)
    nww_track_commit.initialize(book, init_doc)
    state = load_json(tracking_path(book)) or {}
    return {
        "ok": True, "action": mode, "book_id": book.name,
        "state_revision": state.get("state_revision"),
        "last_committed_chapter": state.get("last_committed_chapter"),
        "detail": "已按当前登录用户重新初始化追踪状态（schema 5 空状态），不触碰正文/大纲/设定。",
    }


def is_legacy_format(book: Path) -> bool:
    """旧格式判定：无追踪文件但有 idea.txt + chapters/ 目录。"""
    return (book / "idea.txt").is_file() and not tracking_path(book).is_file()


def chapter_number_from_name(name: str) -> int | None:
    m = CHAPTER_RE.match(name)
    return int(m.group(1)) if m else None


def outline_number_from_name(name: str) -> int | None:
    m = OUTLINE_RE.match(name)
    return int(m.group(1)) if m else None


def _collect_chapter_files(book: Path) -> list[Path]:
    """统一收集正文章节文件：V12 从 正文/第*章*.md，旧格式从 chapters/NN/final.md。"""
    if is_legacy_format(book):
        chapters_dir = book / "chapters"
        if not chapters_dir.is_dir():
            return []
        files = []
        for ch_dir in sorted(chapters_dir.iterdir()):
            if ch_dir.is_dir() and ch_dir.name.isdigit():
                final = ch_dir / "final.md"
                if final.is_file():
                    files.append(final)
        return files
    body_dir = book / "正文"
    if not body_dir.is_dir():
        return []
    # rglob 兼容平铺（正文/第N章*.md）与卷目录（正文/第一卷/第N章*.md）
    return sorted((p for p in body_dir.rglob("第*章*.md") if not _is_non_prose(p)),
                  key=lambda p: (chapter_number_from_name(p.name) or 0, p.name))


def _chapter_number_from_path(book: Path, p: Path) -> int | None:
    """从文件路径推断章节号：V12 从文件名，旧格式从父目录名。"""
    if is_legacy_format(book):
        try:
            return int(p.parent.name)
        except ValueError:
            return None
    return chapter_number_from_name(p.name)


def _is_legacy_stub_state(tracking: object) -> bool:
    """精确匹配旧 create_book 伪造的 stub 结构（B15，2026-10-19 外脑裁决 F4）。

    stub 形态 = 恰好五个字段 {book_title, schema_version:1, state_revision:0,
    last_committed_chapter:None, created_at}。多一个字段都可能含有效追踪数据
    （schema≠5 但非 stub 的异常 state）→ 不算 stub，保留原文件报 needs_repair，
    绝不按「schema 不等于 5」泛化清理（外脑：不确定时保留 + 报错，不能冒险）。
    """
    return (isinstance(tracking, dict)
            and set(tracking.keys()) == {"book_title", "schema_version", "state_revision",
                                         "last_committed_chapter", "created_at"}
            and tracking.get("schema_version") == 1
            and tracking.get("state_revision") == 0
            and tracking.get("last_committed_chapter") is None)


def _copy_guard_enabled() -> bool:
    """副本识别闸是否启用：默认开启，NWW_BOOK_COPY_GUARD=0/false/no 关闭。"""
    return os.environ.get("NWW_BOOK_COPY_GUARD", "").strip().lower() not in _COPY_GUARD_OFF_VALUES


def _has_valid_marker(book_dir: Path, book_id: str) -> bool:
    """书目录是否带有**有效**身份标记（created_by 是 create_book 且未被复制走样）。

    有效条件：标记文件存在 + 可解析为 dict + `book_id` 与目录名一致。
      - copytree 复制已带标记的新书时，标记文件会被一并复制，但副本目录名变了、
        内容里的 book_id 仍是原书目录名 → 视为无效（否则副本会冒充真书，闸失效）；
      - 非法 JSON / 非 dict / 缺 book_id 的损坏标记同样视为无标记，不做任何猜测。
    """
    marker = load_json(book_dir / BOOK_MARKER)
    if not isinstance(marker, dict):
        return False
    return marker.get("book_id") == book_id


def _scan_books(books_root: Path) -> list[dict]:
    """扫描书库目录，产出一条目录一条记录的原始 entry 列表（不做副本判定）。"""
    books = []
    for d in sorted(books_root.iterdir()):
        if d.name.startswith("."):
            continue
        initialized = is_v12_book(d)
        uninitialized = (not initialized) and is_uninitialized_v12_book(d)
        if not initialized and not uninitialized:
            continue
        tracking = load_json(tracking_path(d))
        bible = load_json(d / "bible.json")
        chapters = _collect_chapter_files(d) if initialized else []
        total_chars = 0
        for c in chapters:
            try:
                total_chars += nww_wordcount.count_visible_chars(c.read_text(encoding="utf-8"))
            except (OSError, UnicodeError):
                pass
        # 标题：已 init 从 tracking 取；未 init 从 bible.json 取，兜底目录名
        title = tracking.get("book_title") or bible.get("book_title")
        if not title:
            idea = (d / "idea.txt").read_text(encoding="utf-8", errors="replace").strip() if (d / "idea.txt").is_file() else ""
            title = idea[:40] if idea else d.name
        entry = {
            "id": d.name,
            "title": title,
            "root": d.name,
            "genre": bible.get("genre", ""),
            "synopsis": bible.get("synopsis") or bible.get("world_summary", ""),
            "chapter_count": len(chapters),
            "total_chars": total_chars,
            "last_committed_chapter": tracking.get("last_committed_chapter"),
            "state_revision": tracking.get("state_revision"),
            "schema_version": tracking.get("schema_version"),
            "initialized": initialized,
        }
        # B15（2026-10-19 外脑裁决 F4）：needs_repair 明确指向修复操作，不只显示警告。
        # ① 存量 stub（精确结构匹配）→ 删除 stub 后重跑 init；
        # ② schema≠5 且非 stub（含 tracking 损坏解析为 {}）→ 可能含有效追踪数据，
        #    保留原文件，人工核对（normalize_state 对 schema≠5 本就拒绝，书不可 commit）。
        if initialized:
            if _is_legacy_stub_state(tracking):
                entry["needs_repair"] = True
                entry["init_action"] = "repair_stub"
                entry["repair_hint"] = (
                    "检测到建书残留的 stub 追踪状态（schema 1 占位，无有效追踪数据）。"
                    "可在书籍管理界面点「重建追踪状态」按钮，由服务端按当前登录用户重新初始化"
                    "（先删除 stub 再 init，不触碰正文/大纲/设定），无需在终端手敲命令。")
            elif tracking.get("schema_version") != 5:
                entry["needs_repair"] = True
                entry["repair_hint"] = (
                    f"追踪状态异常：schema_version={tracking.get('schema_version')}（应为 5）"
                    "且不匹配 stub 结构，可能含有效追踪数据——已保留原文件未做任何改动，"
                    "请人工核对其中的 timeline/foreshadow/character_snapshots 后修复或迁移")
        if uninitialized:
            # 未 init 的书：统计正文章节数（不依赖 tracking），给出 init 提示
            body_dir = d / "正文"
            raw_chapters = []
            if body_dir.is_dir():
                raw_chapters = sorted((p for p in body_dir.rglob("第*章*.md") if not _is_non_prose(p)),
                                      key=lambda p: (chapter_number_from_name(p.name) or 0, p.name))
            entry["chapter_count"] = len(raw_chapters)
            entry["total_chars"] = 0
            for c in raw_chapters:
                try:
                    entry["total_chars"] += nww_wordcount.count_visible_chars(c.read_text(encoding="utf-8"))
                except (OSError, UnicodeError):
                    pass
            entry["init_action"] = "initialize"
            entry["init_hint"] = (
                "追踪状态未初始化：换 IDE 工作时 init 中断，跨章一致性/伏笔/数值锚全部盲区。"
                "可在书籍管理界面点「初始化本书」按钮，由服务端按当前登录用户重新初始化，无需在终端手敲命令。")
        books.append(entry)
    return books


def list_books_with_diagnostics(books_root: Path) -> tuple[list[dict], list[dict]]:
    """书架扫描 + 副本识别：返回 (正常书籍列表, 被忽略的疑似副本列表)。

    判定规则（2026-11-07 副本识别闸；同日修正全无标记误伤与「复制继承标记」绕过）：
      - 以 title 分组；组内 >1 个目录说明出现了同名书；
      - 标记是否有效由 `_has_valid_marker` 决定：文件存在、JSON 可解析为 dict、
        且 `book_id` 与目录名一致才算有效。copytree 复制已带标记的新书时标记文件
        会一并被复制，但内容里的 book_id 仍是原书目录名 → 副本视为无有效标记；
      - **仅当组内至少存在 1 个有效标记的目录时**，才把该组内**无有效标记**的
        目录判为疑似复制副本——历史上 AI 验证脚本用 shutil.copytree 复制真书、
        未改 book_title，副本不会带有效标记，而同组真书带有效标记。
      - 若同名组内**全部无有效标记**（存量书、手工复制的存量书、损坏标记等），
        无法区分真伪，则**全部保留照常返回**，ignored 为空——绝不猜测、不误伤；
      - 带有效标记的目录永远保留；
      - 疑似副本不进入书架（不进第一项返回值），只在第二项 ignored 中给出
        {"id": 目录名, "title": 标题, "reason": "duplicate_title_no_marker"}，不含绝对路径。
      - 无有效标记但 title 唯一的目录照常返回（不误伤用户手工建的正常书）。
      - NWW_BOOK_COPY_GUARD=0/false/no 时关闭判定，全部按旧行为返回、ignored 为空。
    """
    books = _scan_books(books_root)
    if not _copy_guard_enabled():
        return books, []

    # 每个目录只读一次标记文件，后续分组判定全部复用该缓存。
    marker_flags = {
        entry["id"]: _has_valid_marker(books_root / entry["id"], entry["id"])
        for entry in books
    }
    by_title: dict[str, list[dict]] = {}
    for entry in books:
        by_title.setdefault(entry.get("title", ""), []).append(entry)
    # 只有组内出现过带标记的目录，才有「真书基准」可用来识别同组无标记副本；
    # 全组无标记时保持原样返回，避免把两本存量书一起隐藏。
    group_has_marker = {
        title: any(marker_flags[item["id"]] for item in group)
        for title, group in by_title.items()
    }

    kept: list[dict] = []
    ignored: list[dict] = []
    for entry in books:
        title = entry.get("title", "")
        group = by_title.get(title, ())
        is_suspect_copy = (
            len(group) > 1
            and group_has_marker.get(title, False)
            and not marker_flags[entry["id"]]
        )
        if is_suspect_copy:
            ignored.append({
                "id": entry["id"],
                "title": title,
                "reason": "duplicate_title_no_marker",
            })
        else:
            kept.append(entry)
    return kept, ignored


def list_books(books_root: Path) -> list[dict]:
    """书架列表：保持旧签名/返回结构，内部走带副本判定的扫描。"""
    books, _ignored = list_books_with_diagnostics(books_root)
    return books


def list_chapters(book: Path) -> list[dict]:
    tracking = load_json(tracking_path(book))
    wc_records = tracking.get("wordcount_records", {})
    recent = {r.get("chapter"): r.get("summary") for r in tracking.get("context", {}).get("recent_chapters", [])}

    legacy = is_legacy_format(book)
    outline_targets: dict[int, int | None] = {}

    if legacy:
        # 旧格式：chapters/NN/final.md
        chapters_dir = book / "chapters"
        chapters = []
        if chapters_dir.is_dir():
            for ch_dir in sorted(chapters_dir.iterdir()):
                if not ch_dir.is_dir() or not ch_dir.name.isdigit():
                    continue
                n = int(ch_dir.name)
                final = ch_dir / "final.md"
                if not final.is_file():
                    continue
                try:
                    body = final.read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    continue
                actual = nww_wordcount.count_visible_chars(body)
                rec = wc_records.get(str(n)) or {}
                target = rec.get("target")
                status = rec.get("status")
                if target is not None:
                    try:
                        status = nww_wordcount.evaluate_wordcount(body, target)["status"]
                    except Exception:
                        pass
                summary = ""
                summary_file = ch_dir / "summary.txt"
                if summary_file.is_file():
                    summary = summary_file.read_text(encoding="utf-8", errors="replace").strip()
                chapters.append({
                    "chapter": n,
                    "file": f"chapters/{n:02d}/final.md",
                    "volume": None,
                    "title": f"第{n}章",
                    "target": target,
                    "actual": actual,
                    "status": status,
                    "summary": summary or recent.get(n, ""),
                })
        return chapters

    body_dir = book / "正文"
    outline_dir = book / "大纲"
    if outline_dir.is_dir():
        for o in outline_dir.glob("细纲_第*章*.md"):
            n = outline_number_from_name(o.name)
            if n is None:
                continue
            try:
                outline_targets[n] = nww_wordcount.target_from_outline(o.read_text(encoding="utf-8"))
            except Exception:
                outline_targets[n] = None

    chapters = []
    if body_dir.is_dir():
        # rglob 兼容平铺（正文/第N章*.md）与卷目录（正文/第一卷/第N章*.md）
        for f in sorted((p for p in body_dir.rglob("第*章*.md") if not _is_non_prose(p)),
                        key=lambda p: (chapter_number_from_name(p.name) or 0, p.name)):
            n = chapter_number_from_name(f.name)
            if n is None:
                continue
            try:
                body = f.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            actual = nww_wordcount.count_visible_chars(body)
            rec = wc_records.get(str(n)) or {}
            target = rec.get("target") or outline_targets.get(n)
            status = None
            if target is not None:
                try:
                    status = nww_wordcount.evaluate_wordcount(body, target)["status"]
                except Exception:
                    status = rec.get("status")
            title = f.name[:-3]
            m = re.match(r"^第0*\d+章[_\-\s　]*(.*)$", title)
            if m and m.group(1):
                title = m.group(1)
            # 卷目录支持：正文/第一卷/第N章.md 时 volume=第一卷；平铺时为 None
            vol = f.parent.name if f.parent.name != "正文" else None
            chapters.append({
                "chapter": n,
                "file": f.name,
                "volume": vol,
                "title": title,
                "target": target,
                "actual": actual,
                "status": status,
                "summary": recent.get(n, ""),
            })
    return chapters


def load_chapter_scores(book: Path) -> dict[int, float]:
    """扫描 追踪/评分/第NNN章.json → {章号: best_score}，即侧边栏显示的**章节健康分**。

    评分文件由审稿闭环（nww_review_cycle）写出，结构形如：
        {"chapter": 27, "rounds": [...], "best_score": 96.0, "updated_at": ...}
    取 best_score（历史最高轮次分），与追踪面板同源。

    命名口径（2026-09-13 拍板）：这叫**章节健康分**，不是「章节质量分」。
    它由单章审稿闭环的 I-A~I-I 九项决定 —— 八项是「查错」（截断/复读/毒句式/负向词/
    时代词/声纹/独白/标题）+ 一项细纲兑现，**没有任何一项衡量「好不好看」**
    → 无技术瑕疵 + 细纲兑现即逼近满分，96 分是**口径决定的**，不是质量异常。
    商业可读性评审是**另一套**：卷级 nww-score90（E0 黄金三章 + E1-E5），不可互相引用。

    容错策略：目录缺失 / 单个文件损坏 / 值非数字 一律跳过 —— 侧边栏是只读展示，
    不能让一个坏文件把整棵树拖垮。没有评分的章节直接不填，前端不渲染该项。
    """
    scores: dict[int, float] = {}
    score_dir = book / "追踪" / "评分"
    if not score_dir.is_dir():
        return scores
    for p in sorted(score_dir.glob("第*章.json")):
        m = CHAPTER_RE.match(p.name)
        if not m:
            continue
        best = load_json(p).get("best_score")
        # bool 是 int 子类，显式排除，避免 JSON 里的 true/false 被当成 1/0 分
        if isinstance(best, (int, float)) and not isinstance(best, bool):
            scores[int(m.group(1))] = float(best)
    return scores


def build_tree(book: Path) -> dict:
    """目录树：V12 按 正文/大纲/设定/追踪 四区；旧格式按 chapters + 根文件组织。"""
    tracking = load_json(tracking_path(book))
    title = tracking.get("book_title")
    if not title:
        idea = (book / "idea.txt").read_text(encoding="utf-8", errors="replace").strip() if (book / "idea.txt").is_file() else ""
        title = idea[:40] if idea else book.name
    tree = {"title": title, "sections": []}

    if is_legacy_format(book):
        # 旧格式：chapters 区 + 根文件区
        chapters_dir = book / "chapters"
        if chapters_dir.is_dir():
            files = []
            for p in sorted(chapters_dir.rglob("*")):
                if p.is_file() and not p.name.startswith(".") and not _is_non_prose(p):
                    rel = p.relative_to(book).as_posix()
                    files.append({"path": rel, "name": p.name, "writable": fs_is_writable(book, p)})
            tree["sections"].append({"section": "chapters", "files": files})
        # 根目录文件（idea/kernel/bible/state 等）
        root_files = []
        for p in sorted(book.iterdir()):
            if p.is_file() and not p.name.startswith("."):
                rel = p.name
                root_files.append({"path": rel, "name": p.name, "writable": fs_is_writable(book, p)})
        if root_files:
            tree["sections"].append({"section": "根文件", "files": root_files})
        return tree

    # 解析卷纲规划（仅正文分区使用）
    volume_count, volume_plans, total_planned_chapters = parse_volume_plans(book)

    # 读取 bible.json 中的 volume_mode 配置（系统级分卷/不分卷设置）
    # "split" = 分卷模式（正文/第一卷/第N章.md）
    # "flat"  = 不分卷模式（正文/第N章.md）
    # 未配置时自动检测：正文目录下有子目录则 split，否则 flat
    bible = load_json(book / "bible.json")
    volume_mode = bible.get("volume_mode")
    if volume_mode not in ("split", "flat"):
        body_dir = book / "正文"
        has_subdirs = body_dir.is_dir() and any(
            p.is_dir() and not p.name.startswith(".") for p in body_dir.iterdir()
        )
        volume_mode = "split" if has_subdirs else "flat"

    # 章节评分索引：循环外扫一次，循环内 O(1) 取用（避免每章都读目录）
    chapter_scores = load_chapter_scores(book)

    for section in ("正文", "大纲", "设定", "追踪"):
        base = book / section
        if not base.is_dir():
            continue
        files = []
        # 正文分区：flat 模式只收集根目录文件，不递归子目录；split 模式递归全部
        if section == "正文" and volume_mode == "flat":
            iterator = (p for p in sorted(base.iterdir()) if p.is_file())
        else:
            iterator = (p for p in sorted(base.rglob("*")) if p.is_file())
        # 统一过滤：隐藏文件（.DS_Store 等）与辅助目录（_review_snapshots / _archive）。
        # 快照文件名与章节同形（第027章_round1.md），不过滤会在侧边栏伪装成新章节。
        iterator = (p for p in iterator if not p.name.startswith(".") and not _is_non_prose(p))
        for p in iterator:
            # 正文分区只应出现章节 .md 文件；备份/损坏副本即使绕过 _is_non_prose，后缀不符也会在此被剔除
            if section == "正文" and p.suffix.lower() != ".md":
                continue
            rel = p.relative_to(book).as_posix()
            entry = {"path": rel, "name": p.name, "writable": fs_is_writable(book, p)}
            # 正文章节文件附带可见字数 + 评分 + 落盘时间，供侧边栏章节副行显示
            if section == "正文" and p.suffix.lower() == ".md":
                try:
                    entry["chars"] = nww_wordcount.count_visible_chars(p.read_text(encoding="utf-8"))
                except (OSError, UnicodeError):
                    entry["chars"] = 0
                # 章节健康分：审稿闭环写入的 best_score；无评分文件则不填（前端不渲染该项）
                chapter_match = CHAPTER_RE.match(p.name)
                if chapter_match:
                    best = chapter_scores.get(int(chapter_match.group(1)))
                    if best is not None:
                        entry["score"] = best
                # 落盘时间：正文文件 mtime。这里刻意**不用** quality_meta.finalized_at ——
                # mtime 不依赖追踪链路走完（commit 闸门 BLOCK 时正文其实也已落盘，
                # 2026-09-13 第27章就是这个情形），所以它对用户更诚实。
                try:
                    entry["mtime"] = int(p.stat().st_mtime)
                except OSError:
                    pass
            files.append(entry)
        sec_entry: dict = {"section": section, "files": files}
        # 正文分区附带卷纲规划数据：卷模式 + 卷总数 + 各卷规划章节数 + 总规划章数
        if section == "正文":
            sec_entry["volume_mode"] = volume_mode
            sec_entry["volume_count"] = volume_count
            sec_entry["volume_plans"] = volume_plans
            sec_entry["total_planned_chapters"] = total_planned_chapters
        tree["sections"].append(sec_entry)
    return tree


def get_tracking(book: Path) -> dict:
    """追踪状态结构化：直接返回解析后的 _tracking-state.json + 派生视图文件（只读）。"""
    tracking = load_json(tracking_path(book))
    derived = []
    base = book / "追踪"
    if base.is_dir():
        for p in sorted(base.rglob("*")):
            if p.is_file():
                rel = p.relative_to(book).as_posix()
                derived.append({
                    "path": rel,
                    "name": p.name,
                    "section": p.parent.name if p.parent != base else "",
                    "chars": len(p.read_text(encoding="utf-8", errors="replace")),
                    "writable": False,
                })
    return {"tracking": tracking, "derived": derived}


TRASH_DIR = ".trash"


def _fill_bible_from_draft(bible: dict, draft: dict) -> None:
    """建书草案（八项）→ bible.json 引擎 schema 字段（2026-10-23 建书补全方案 Step1）。

    保留 book_title/genre/synopsis（list_books 等旧读取方兼容）；只补草案能
    确定性给出的字段，characters/cost_system 等需创作判断的字段留给
    bootstrap 生成文件与作者后续完善，不在此伪造。
    """
    def s(key: str) -> str:
        v = draft.get(key)
        return v.strip() if isinstance(v, str) else ""

    bible["title"] = s("title") or bible.get("book_title", "")
    if s("synopsis"):
        bible["world_summary"] = s("synopsis")
    prota = s("protagonist")
    if prota:
        # 尽量拆名字：形如「沈砚，……」/「沈砚：……」开头 2-4 字中文名
        m = re.match(r"^([\u4e00-\u9fa5·]{2,4})(?=[，,、：:（(])", prota)
        bible["protagonist"] = {"name": m.group(1) if m else "", "description": prota}
    core = s("core_theme")
    if core:
        creative = bible.get("creative")
        if not isinstance(creative, dict):
            creative = {}
        creative["core_question"] = core
        bible["creative"] = creative
    for key in ("conflict", "differentiation", "scale"):
        v = s(key)
        if v:
            bible[key] = v
    bible["platform"] = "番茄"
    # 字数规范（用户裁决 2026-10-25）：基准 2500 字可上浮 10% → 2500-2750；
    # 3000 字以内观测容忍。废除按平台表（番茄 2000-2500）推导的旧默认。
    bible["wordcount_target"] = "2500-2750"
    # 叙事人称（2026-11-04 用户裁决）：故事设定必须含「第几人称视角」维度，默认第三人称。
    # 建书草案可显式带 pov 覆盖；缺失则落默认。这是全书叙事人称的唯一权威来源，
    # 文风（dataplane 建书 bootstrap）与写章链（packet narrative_pov）都从这里读，不再各自发挥。
    _pov = s("pov")
    bible["pov"] = _pov if _pov else _DEFAULT_POV


# 叙事人称默认值（2026-11-04 用户裁决：故事设定必须含第几人称视角维度，默认第三人称）。
_DEFAULT_POV = "第三人称"


def _default_policy() -> dict:
    """新书 commit 闸政策（对齐 skills/nww-novel-skill/skills/setup/SKILL.md Step 1.1 模板）。

    根因修复（2026-11-04）：此前建书不写 policy 段，nww_policy.load_policy 对「无 policy」
    的书 fail_closed 缺省 True、缺闸键按 True，导致一致性闸（A-3）从兼容放行意外变成硬 BLOCK，
    而新书又从未跑过 /nww-consistency 闭环，写第 1 章 commit 即卡死。

    显式落盘完整 policy：fail_closed=true + a2_mode=on + 五闸全开（新书默认进入完整生产流，
    与 setup 模板一致）。行为可预期，不再依赖「缺省 True」隐式开闸；nww_scan_commit_gates.py
    也不再报 missing_policy 高危。
    """
    # 2026-11-05 治本：闸键集合从 nww_policy.REQUIRED_NEW_BOOK_GATES 派生，
    # 消除「建书写 5 个键、policy 认 7 个键、缺键由 fail_closed 静默补 True」的
    # 双事实源。缺键补 True 会让作者在 bible 里根本找不到 cross_chapter /
    # foreshadow_overdue 两个开关，却被硬闸拦住。
    gates: dict = {}
    for name, enabled in nww_policy.REQUIRED_NEW_BOOK_GATES.items():
        # fact_provenance 在 PolicyBundle._coerce_gate_value 里按 dict.enabled 读取
        gates[name] = {"enabled": bool(enabled)} if name == "fact_provenance" else bool(enabled)
    return {
        "fail_closed": True,
        "a2_mode": "on",
        "commit_gates": gates,
        "overrides": [],
    }


def _complete_policy_gates(policy: object) -> None:
    """把缺闸键显式补齐（幂等），不让 fail_closed 隐式补 True。"""
    if not isinstance(policy, dict):
        return
    gates = policy.get("commit_gates")
    if not isinstance(gates, dict):
        gates = {}
        policy["commit_gates"] = gates
    for name, enabled in nww_policy.REQUIRED_NEW_BOOK_GATES.items():
        if name not in gates:
            gates[name] = {"enabled": bool(enabled)} if name == "fact_provenance" else bool(enabled)


def _new_book_init_document(title: str) -> dict:
    """新书的合法追踪初始化输入；只描述第 0 章空状态，不伪造任何故事事实。"""
    return {
        "schema_version": 1,
        "book_title": title,
        "last_chapter": 0,
        "context": {
            "position": {
                "volume": "第一卷",
                "volume_start_chapter": 1,
                "story_time": "故事开始前",
                "scene": "未开始",
            },
            "long_term_constraints": [],
            "active_character_names": [],
            "continuity_risks": [],
            "recent_chapters": [],
            "next_chapter_commitments": [],
        },
        "character_snapshots": {},
        "foreshadow": [],
        "timeline_events": [],
        "global": {},
    }


def create_book(books_root: Path, book_id: str, title: str, genre: str = "", synopsis: str = "",
                draft: dict | None = None) -> dict:
    """创建 V12 标准书项目：四区目录 + bible.json + 正式追踪状态。

    B15（2026-10-19 外脑裁决 F4）：不得伪造 schema 1 stub tracking state——
    init 是正式 tracking state 的唯一创建者（init never overwrites 拒覆盖保护不放宽，
    伪造 stub 会让该书的 init 永远被拒 = 死锁）。

    2026-11-04 根因修复：此前只建目录、bible，完全依赖人工补跑 init；写章入口又把
    「无 tracking」当新书放行，直到正文落盘后的 delta_draft 才报 tracking state missing。
    现在建书即生成可审计的《初始化事务.json》，并调用同一个正式 initialize 创建
    schema 5 / last_chapter=0 的空状态；不虚构角色、伏笔、时间线或全局事实。

    draft（可选，2026-10-23 建书补全）：chatbox 建书草案八项
    {title,genre,synopsis,core_theme,protagonist,conflict,differentiation,scale}，
    传入时 bible.json 按引擎 schema 补全；不传 = 行为与旧版完全一致。
    """
    target = books_root / book_id
    if target.exists():
        raise FileExistsError(f"书籍已存在：{book_id}")
    # 标准四区
    for section in ("正文", "大纲", "设定", "追踪"):
        (target / section).mkdir(parents=True, exist_ok=True)
    # bible（题材/简介，list_books 会读）
    bible = {"book_title": title, "genre": genre, "synopsis": synopsis}
    if isinstance(draft, dict) and draft:
        _fill_bible_from_draft(bible, draft)
    # 显式写入 commit 闸政策（根因修复 2026-11-04）：避免「无 policy → fail_closed 缺省 True」
    # 让一致性闸隐式硬 BLOCK。draft 补全若已带 policy 则保留，否则落默认完整 policy。
    _policy = bible.setdefault("policy", _default_policy())
    _complete_policy_gates(_policy)
    # 叙事人称默认（2026-11-04）：无 draft 时也要落 pov，确保每本书都有确定的人称权威来源。
    bible.setdefault("pov", _DEFAULT_POV)
    (target / "bible.json").write_text(
        json.dumps(bible, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    init_document = _new_book_init_document(title)
    (target / "初始化事务.json").write_text(
        json.dumps(init_document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    nww_track_commit.initialize(target, init_document)
    # IDE 规则文件软链接（指向项目根目录 .cursorrules，单一事实源，Cursor/Trae/Windsurf/Claude 通用）
    _repo_root = Path(__file__).resolve().parents[3]
    _rules_src = _repo_root / ".cursorrules"
    if _rules_src.is_file():
        import os as _os
        _rel = _os.path.relpath(_rules_src, target)
        for _rule_name in (".cursorrules", ".traerules", ".windsurfrules", "CLAUDE.md"):
            _link = target / _rule_name
            if not _link.exists():
                _link.symlink_to(_rel)
    # 书籍身份标记（2026-11-07 副本识别闸）：create_book 成功创建后落盘，
    # 供 list_books_with_diagnostics 区分真实书与无标记的同名复制目录。
    marker = {
        "book_id": book_id,
        "title": title,
        "origin": "api",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "marker_schema": 1,
    }
    (target / BOOK_MARKER).write_text(
        json.dumps(marker, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"id": book_id, "title": title, "path": str(target.relative_to(books_root))}


def delete_book(books_root: Path, book_id: str) -> dict:
    """删除书。默认移入回收站 projects/.trash/{book_id}__{stamp}；设 NWW_DELETE_PURGE=1 时物理删除。"""
    root = book_root(books_root, book_id)
    purge = os.environ.get("NWW_DELETE_PURGE", "").strip().lower() in {"1", "true", "yes"}
    if purge:
        shutil.rmtree(root)
        return {"ok": True, "mode": "purge", "book_id": book_id}
    trash = books_root / TRASH_DIR
    trash.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = trash / f"{book_id}__{stamp}"
    root.rename(dest)
    return {
        "ok": True,
        "mode": "trash",
        "book_id": book_id,
        "trash_path": str(dest.relative_to(books_root)),
    }


def delete_file(books_root: Path, book_id: str, file_path: str) -> dict:
    """删除单篇文章/文件。默认移入回收站 projects/.trash/{book_id}__{stamp}/{相对路径}；NWW_DELETE_PURGE=1 时物理删除。"""
    root = book_root(books_root, book_id)
    target = resolve_path(root, file_path)
    if not target.is_file():
        raise FileNotFoundError(f"文件不存在：{file_path}")
    if not fs_is_writable(root, target):
        raise PermissionError(f"只读路径，禁止删除：{file_path}")
    purge = os.environ.get("NWW_DELETE_PURGE", "").strip().lower() in {"1", "true", "yes"}
    if purge:
        target.unlink()
        return {"ok": True, "mode": "purge", "book_id": book_id, "path": file_path}
    trash = books_root / TRASH_DIR
    trash.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    rel = target.relative_to(root)
    dest = trash / f"{book_id}__{stamp}" / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    target.rename(dest)
    return {
        "ok": True,
        "mode": "trash",
        "book_id": book_id,
        "path": file_path,
        "trash_path": str(dest.relative_to(books_root)),
    }
