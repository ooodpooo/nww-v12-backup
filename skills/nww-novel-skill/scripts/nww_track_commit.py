#!/usr/bin/env python3
"""Maintain one structured story state and its deterministic Markdown views.

The language model supplies compact semantic JSON.  This tool validates and
merges that input in memory, renders every derived view, then atomically writes
``_tracking-state.json`` last as the single commit point. Per-project locking
serializes concurrent writers before revision and wordcount checks.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
from nww_policy import load_policy, _load_bible  # Task 2 PolicyBundle; Task3 scale bible
from nww_content_gates import check_global_presence  # Task 3 content presence
import nww_gate_policy as _gate  # V13 道闸策略层（2026-11-13）；env NWW_GATE_MODE=strict 回滚
# C-DELTA-SCHEMA@1：delta 投影白名单的唯一来源在 nww_delta_schema.py（与 pipeline 同一模块）。
# C-QUALITY-META@1：quality_meta 允许键同源（QUALITY_META_ALLOWED）。
from nww_delta_schema import DELTA_ALLOWED as _DELTA_ALLOWED
from nww_delta_schema import QUALITY_META_ALLOWED as _QUALITY_META_ALLOWED
# C-REPEAT@1：跨章逐字复读的切句与阈值唯一来源在 nww_state_checks.py。
from nww_state_checks import cross_chapter_repeats as _shared_cross_chapter_repeats

# 跨章检测回看章数（与 nww_cross_chapter_check.run_check 默认窗口一致）
_CROSS_CHAPTER_WINDOW = 5


def _review_pass_score() -> int:
    """P0-3b（2026-10-19 外脑裁决）：A-5 评分门的通过线，单一来源取自 nww_review_cycle.PASS_SCORE。

    延迟导入 + 兜底：nww_review_cycle 若随部署缺失，本模块必须保持独立可用，
    回退到与 PASS_SCORE 对齐的 85（与 pipeline._REVIEW_PASS_SCORE 同值同源约定）。
    """
    try:
        from nww_review_cycle import PASS_SCORE
        return int(PASS_SCORE)
    except Exception:
        return 85


def _review_dimension_floor() -> int:
    """维度地板硬约束线（外脑 2026-10-19 边界项3 裁决）：best 达标但任一维度触底 = 硬约束失败。

    单一来源取自 nww_review_cycle.DIMENSION_FLOOR（模块级常量）；延迟导入 + 兜底 70，
    与 _review_pass_score 同模式：nww_review_cycle 缺失时本模块必须保持独立可用。
    """
    try:
        from nww_review_cycle import DIMENSION_FLOOR
        return int(DIMENSION_FLOOR)
    except Exception:
        return 70


_WORDCOUNT_CORE_PATH = Path(__file__).with_name("nww_wordcount.py")
if not _WORDCOUNT_CORE_PATH.is_file():  # pragma: no cover - broken deployment
    raise RuntimeError("TOOL_UNAVAILABLE: nww_wordcount.py")
_WORDCOUNT_CORE_SPEC = importlib.util.spec_from_file_location(
    "nww_wordcount", _WORDCOUNT_CORE_PATH
)
if _WORDCOUNT_CORE_SPEC is None or _WORDCOUNT_CORE_SPEC.loader is None:  # pragma: no cover
    raise RuntimeError("unable to load wordcount core")
nww_wordcount = importlib.util.module_from_spec(_WORDCOUNT_CORE_SPEC)
_WORDCOUNT_CORE_SPEC.loader.exec_module(nww_wordcount)


INPUT_SCHEMA_VERSION = 1
TRACKING_SCHEMA_VERSION = 5
DELTA_TARGET_BYTES = 1536
DELTA_MAX_BYTES = 3072
CONTEXT_TARGET_BYTES = 8192
CONTEXT_MAX_BYTES = 12288
SNAPSHOT_TARGET_BYTES = 4096
SNAPSHOT_MAX_BYTES = 8192

CONTEXT_HEADINGS = (
    "## 当前位置",
    "## 全局状态",
    "## 长期约束",
    "## 核心角色状态",
    "## 活跃伏笔",
    "## 近三章速记",
    "## 下一章承诺",
    "## 连贯性风险",
)
FORESHADOW_STATUSES = ("已埋", "需处理", "已回收", "已过期", "放弃")
FORESHADOW_IMPORTANCE = ("高", "中", "低")
REVEAL_STATUSES = ("未揭示", "部分揭示", "已揭示")
INVALID_FILE_CHARS = re.compile(r"[<>:\"/\\|?*\x00-\x1f]")
FORESHADOW_ID = re.compile(r"^F\d{3,}$")
EVENT_ID = re.compile(r"^E\d{3,}$")
STORY_EVENT_ID = re.compile(r"^SE-\d{3,}$")
# P0-1 事实来源契约（Fact Provenance Contract v1.0）：事实分层常量。
# fact_type 六类（状态/事件二分）：事件流水归 timeline，稳定状态归 facts。
FACT_TYPES = ("character_event", "character_identity", "world_event", "relationship_event", "ability_state", "item_state")
CONFIDENCE_LEVELS = ("low", "medium", "high")
CONTRACT_STATES = ("schema_only", "active")
SOURCE_TYPES = ("chapter", "outline", "bible")
FACT_ID = re.compile(r"^T\d{3,}$")
BELIEF_ID = re.compile(r"^B\d{3,}$")
# beliefs 预算（角色认知层）：总预算 ≤ 快照 15%（SNAPSHOT_MAX_BYTES=8192 → 约1200B），超限仅 WARNING 不 BLOCK。
BELIEFS_MAX_ITEMS = 5
BELIEF_ID_MAX_BYTES = 8
BELIEF_TEXT_MAX_BYTES = 120
BELIEF_SOURCE_MAX_BYTES = 64
BELIEFS_TOTAL_BUDGET = 1200
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
RETIRED_TRACKING_PATHS = (
    "_tracking-meta.json",
    "阶段摘要.md",
    "角色状态.md",
    "时间线.md",
    "摘要",
    "时间线/事件库.json",
)
RETIRED_ARCHIVE_DIR = "_旧追踪存档"


class TrackingError(ValueError):
    """Expected validation or tracking-state error."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise TrackingError(message)


def wordcount_value(function: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return function(*args, **kwargs)
    except nww_wordcount.WordcountError as exc:
        raise TrackingError(str(exc)) from exc


def as_mapping(value: object, label: str) -> dict[str, Any]:
    require(isinstance(value, dict), f"{label} must be a JSON object")
    return value


def as_list(value: object, label: str) -> list[Any]:
    require(isinstance(value, list), f"{label} must be a JSON array")
    return value


def as_int(value: object, label: str, *, minimum: int = 0) -> int:
    require(isinstance(value, int) and not isinstance(value, bool), f"{label} must be an integer")
    require(value >= minimum, f"{label} must be >= {minimum}")
    return value


def require_known_keys(mapping: dict[str, Any], allowed: set[str] | frozenset[str], label: str) -> None:
    unknown = set(mapping) - allowed
    require(not unknown, f"{label} contains unsupported fields: {', '.join(sorted(unknown))}")


def clean_text(value: object, label: str, *, allow_empty: bool = False, max_bytes: int = 768) -> str:
    require(isinstance(value, str), f"{label} must be a string")
    cleaned = " ".join(value.replace("|", "｜").split())
    require(allow_empty or bool(cleaned), f"{label} must not be empty")
    require(len(cleaned.encode("utf-8")) <= max_bytes, f"{label} exceeds {max_bytes} bytes")
    return cleaned


def _truncate_bytes(value: str, max_bytes: int) -> str:
    """UTF-8-safe 有损 bounded projection：按字节截断，不抛错。

    与 clean_text 的「超限即拒绝（require-raise）」语义区分：
    本函数用于 delta.result(≤480B) → recent_chapters.summary(≤360B) 的
    有损投影，语义是「截断」，而非「拒绝」。
    """
    if len(value.encode("utf-8")) <= max_bytes:
        return value
    return value.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


def clean_string_list(
    value: object,
    label: str,
    *,
    maximum: int | None = None,
    item_max_bytes: int = 384,
) -> list[str]:
    values = as_list(value, label)
    if maximum is not None:
        require(len(values) <= maximum, f"{label} may contain at most {maximum} items")
    return [clean_text(item, f"{label}[{index}]", max_bytes=item_max_bytes) for index, item in enumerate(values)]


_DELETE_MARKER = "__delete__"


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并 override 到 base；dict 子树递归合并，非 dict 值直接覆盖。

    用于 global 状态的逐章增量更新：模型只返回本章变化的字段，
    与已有 global 状态递归合并，未提及的字段保持不变。

    墓碑删除：override 中 "__delete__" 键对应的值若为字符串列表，
    则从合并结果中移除这些键（支持退休角色清理等场景）。
    """
    result = copy.deepcopy(base)
    # 处理墓碑删除
    deletes = override.get(_DELETE_MARKER)
    if isinstance(deletes, list):
        for del_key in deletes:
            result.pop(del_key, None)
    for key, value in override.items():
        if key == _DELETE_MARKER:
            continue
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def normalize_global_state(value: object, label: str = "tracking state.global") -> dict[str, Any]:
    """全局运行时状态：作品当前处于什么状态（纪元/卷/核心冲突/发动机/世界变化/势力/下一切换点）。

    只做顶层结构校验，子字段自由定义（不同项目的 global 结构可不同）。
    缺省（init 未提供）返回空 dict，由后续 commit 逐步填充。
    """
    if value is None:
        return {}
    mapping = as_mapping(value, label)
    # 允许任意子字段；只校验值必须是可 JSON 序列化的基本类型
    for key, val in mapping.items():
        require(
            isinstance(val, (str, int, float, bool, list, dict)) or val is None,
            f"{label}.{key} must be a JSON-serializable value",
        )
    return copy.deepcopy(mapping)


def safe_file_component(value: object, label: str) -> str:
    name = unicodedata.normalize("NFC", clean_text(value, label, max_bytes=180))
    require(not INVALID_FILE_CHARS.search(name), f"{label} contains an invalid filename character")
    require(name not in {".", ".."} and not name.endswith((".", " ")), f"{label} is not a safe filename")
    require(name.split(".", 1)[0].upper() not in WINDOWS_RESERVED_NAMES, f"{label} is reserved on Windows")
    return name


def portable_name_key(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def byte_size(text: str) -> int:
    return len(text.encode("utf-8"))


def emit(text: str, *, error: bool = False) -> None:
    """Write UTF-8 bytes directly.

    Windows 的文本 stdout 是 cp1252（含中文即 UnicodeEncodeError），stderr 默认
    backslashreplace（中文被转义成反斜杠码位，作者看不懂）。两条路都要绕开。
    """
    stream = sys.stderr if error else sys.stdout
    stream.flush()
    stream.buffer.write((text + "\n").encode("utf-8"))
    stream.buffer.flush()


def read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TrackingError(f"unable to read JSON {path}: {exc}") from exc


def json_payload(document: object) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def atomic_write_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_if_changed(path: Path, payload: str) -> None:
    try:
        if path.read_text(encoding="utf-8") == payload:
            return
    except FileNotFoundError:
        pass
    atomic_write_text(path, payload)


def tracking_root(project: Path) -> Path:
    return project.resolve() / "追踪"


def state_path(project: Path) -> Path:
    return tracking_root(project) / "_tracking-state.json"


@contextmanager
def project_write_lock(project: Path, *, timeout_seconds: float = 10.0):
    tracking = tracking_root(project)
    tracking.mkdir(parents=True, exist_ok=True)
    path = tracking / ".tracking-commit.lock"
    deadline = time.monotonic() + timeout_seconds
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TrackingError(
                    "tracking commit lock is busy or stale; retry, or remove 追踪/.tracking-commit.lock after confirming no commit is running"
                )
            time.sleep(0.05)
    try:
        os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
        os.fsync(descriptor)
        yield
    finally:
        os.close(descriptor)
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def delta_path(tracking: Path, chapter: int) -> Path:
    width = max(3, len(str(chapter)))
    return tracking / "逐章记录" / f"第{chapter:0{width}d}章.md"


def find_retired_tracking_paths(tracking: Path) -> list[str]:
    found = [relative for relative in RETIRED_TRACKING_PATHS if (tracking / relative).exists()]
    found.extend(sorted(path.name for path in tracking.glob("基线_截至第*章.md")))
    return found


def require_no_retired_tracking_paths(tracking: Path) -> None:
    found = find_retired_tracking_paths(tracking)
    require(not found, f"retired tracking files are not supported: {', '.join(found)}")


def archive_retired_tracking_paths(tracking: Path) -> list[str]:
    """Move a pre-transaction 追踪/ aside so init can build the current protocol in place.

    Nothing is parsed or converted: the old files are kept verbatim for the author to
    consult, and the new state is reconstructed from the init document alone.
    """
    retired = find_retired_tracking_paths(tracking)
    if not retired:
        return []
    archive = tracking / RETIRED_ARCHIVE_DIR
    for relative in retired:
        require(
            not (archive / relative).exists(),
            f"追踪/{RETIRED_ARCHIVE_DIR}/{relative} already exists; move it away before initializing",
        )
    # 先全量校验再搬运；中断后重跑时已搬走的条目不再出现在待搬列表里，可直接续做。
    for relative in retired:
        target = archive / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tracking / relative, target)
    return retired


def validate_position(value: object, label: str = "context.position") -> dict[str, Any]:
    position = as_mapping(value, label)
    require_known_keys(position, {"volume", "volume_start_chapter", "story_time", "scene"}, label)
    return {
        "volume": safe_file_component(position.get("volume"), f"{label}.volume"),
        "volume_start_chapter": as_int(
            position.get("volume_start_chapter"), f"{label}.volume_start_chapter", minimum=1
        ),
        "story_time": clean_text(position.get("story_time"), f"{label}.story_time", max_bytes=240),
        "scene": clean_text(position.get("scene"), f"{label}.scene", max_bytes=240),
    }


def normalize_source(value: object, label: str) -> dict[str, Any] | None:
    """P0-1 事实来源契约：source 定位器（locator 轻量持久化；evidence 审计时动态抽取，不落库）。"""
    if value is None:
        return None
    src = as_mapping(value, label)
    require_known_keys(src, {"type", "chapter", "span"}, label)
    stype = clean_text(src.get("type"), f"{label}.type", max_bytes=24)
    require(stype in SOURCE_TYPES, f"{label}.type must be one of {SOURCE_TYPES}")
    result: dict[str, Any] = {"type": stype}
    if src.get("chapter") is not None:
        result["chapter"] = as_int(src["chapter"], f"{label}.chapter", minimum=1)
    if src.get("span") is not None:
        result["span"] = clean_text(src["span"], f"{label}.span", max_bytes=120, allow_empty=True)
    return result


def normalize_beliefs(value: object, label: str) -> list[dict[str, Any]]:
    """P0-1 角色认知层（Character Belief）：角色认为可能正确的东西，非世界事实。

    预算：≤ BELIEFS_MAX_ITEMS 条；id/belief/source 字节上限；超限在 warn_sizes 提示，不 BLOCK。
    """
    items: list[dict[str, Any]] = []
    for index, raw in enumerate(as_list(value, label)):
        row = as_mapping(raw, f"{label}[{index}]")
        require_known_keys(row, {"id", "belief", "confidence", "source", "recorded_chapter"}, f"{label}[{index}]")
        bid = clean_text(row.get("id"), f"{label}[{index}].id", max_bytes=24)
        require(BELIEF_ID.fullmatch(bid) is not None, f"{label}[{index}].id must look like B001")
        confidence = clean_text(row.get("confidence"), f"{label}[{index}].confidence", max_bytes=24)
        require(confidence in CONFIDENCE_LEVELS, f"{label}[{index}].confidence must be one of {CONFIDENCE_LEVELS}")
        items.append(
            {
                "id": bid,
                "belief": clean_text(row.get("belief"), f"{label}[{index}].belief", max_bytes=BELIEF_TEXT_MAX_BYTES),
                "confidence": confidence,
                "source": normalize_source(row.get("source"), f"{label}[{index}].source"),
                "recorded_chapter": (
                    None
                    if row.get("recorded_chapter") is None
                    else as_int(row.get("recorded_chapter"), f"{label}[{index}].recorded_chapter", minimum=1)
                ),
            }
        )
    require(len({item["id"] for item in items}) == len(items), f"{label} contains duplicate belief IDs")
    return items


def normalize_snapshot(value: object, label: str) -> dict[str, Any]:
    snapshot = as_mapping(value, label)
    require_known_keys(
        snapshot,
        {"identity", "location", "goal", "state", "abilities_resources", "relationships", "knowledge",
         "open_threads", "beliefs", "_lifecycle"},
        label,
    )
    lifecycle_raw = snapshot.get("_lifecycle")
    lifecycle: dict[str, Any] | None = None
    if lifecycle_raw is not None:
        lm = as_mapping(lifecycle_raw, f"{label}._lifecycle")
        require_known_keys(
            lm,
            {"registered_from", "confirmed_by", "confirmed_at", "entity_type", "classification", "card_required"},
            f"{label}._lifecycle",
        )
        lifecycle = {
            "registered_from": clean_text(lm.get("registered_from"), f"{label}._lifecycle.registered_from", max_bytes=60),
            "confirmed_by": clean_text(lm.get("confirmed_by"), f"{label}._lifecycle.confirmed_by", max_bytes=60),
            "confirmed_at": clean_text(lm.get("confirmed_at"), f"{label}._lifecycle.confirmed_at", max_bytes=40),
            "entity_type": clean_text(lm.get("entity_type"), f"{label}._lifecycle.entity_type", max_bytes=40),
            "classification": clean_text(lm.get("classification"), f"{label}._lifecycle.classification", max_bytes=40),
            "card_required": clean_text(lm.get("card_required"), f"{label}._lifecycle.card_required", max_bytes=40),
        }
    return {
        "identity": clean_text(snapshot.get("identity"), f"{label}.identity", max_bytes=240),
        "location": clean_text(snapshot.get("location"), f"{label}.location", max_bytes=240),
        "goal": clean_text(snapshot.get("goal"), f"{label}.goal", max_bytes=300),
        "state": clean_text(snapshot.get("state"), f"{label}.state", max_bytes=300),
        "abilities_resources": clean_string_list(
            snapshot.get("abilities_resources", []), f"{label}.abilities_resources"
        ),
        "relationships": clean_string_list(snapshot.get("relationships", []), f"{label}.relationships"),
        "knowledge": clean_string_list(snapshot.get("knowledge", []), f"{label}.knowledge"),
        "open_threads": clean_string_list(snapshot.get("open_threads", []), f"{label}.open_threads"),
        "beliefs": normalize_beliefs(snapshot.get("beliefs", []), f"{label}.beliefs"),
        "_lifecycle": lifecycle,
    }


def normalize_dormant_characters(
    value: object, label: str = "tracking state.dormant_characters"
) -> dict[str, dict[str, Any]]:
    """休眠角色池：完整快照 + 退役元数据。退役不删除，复活时整条移回热状态。"""
    entries = as_mapping(value, label)
    normalized: dict[str, dict[str, Any]] = {}
    for raw_name, raw_entry in entries.items():
        name = safe_file_component(raw_name, f"{label} character name")
        entry = as_mapping(raw_entry, f"{label}.{name}")
        require_known_keys(entry, {"snapshot", "retired_chapter", "retirement_reason"}, f"{label}.{name}")
        normalized[name] = {
            "snapshot": normalize_snapshot(entry.get("snapshot"), f"{label}.{name}.snapshot"),
            "retired_chapter": as_int(entry.get("retired_chapter"), f"{label}.{name}.retired_chapter", minimum=1),
            "retirement_reason": clean_text(
                entry.get("retirement_reason", ""), f"{label}.{name}.retirement_reason",
                allow_empty=True, max_bytes=240,
            ),
        }
    return normalized


def normalize_snapshots(value: object, label: str = "character_snapshots") -> dict[str, dict[str, Any]]:
    snapshots = as_mapping(value, label)
    normalized: dict[str, dict[str, Any]] = {}
    portable_names: set[str] = set()
    for raw_name, raw_snapshot in snapshots.items():
        name = safe_file_component(raw_name, f"{label} character name")
        key = portable_name_key(name)
        require(key not in portable_names, f"{label} contains a cross-platform duplicate character {name}")
        portable_names.add(key)
        normalized[name] = normalize_snapshot(raw_snapshot, f"{label}.{name}")
    return normalized


def normalize_cost(value: object, label: str = "tracking state.cost") -> dict[str, int]:
    """可选代价计数权威段：{device: count}，count 非负整数，只进不退由 merge 取 max 保证。

    旧 state 无此字段时按 {} 处理，向后兼容。
    """
    costs = as_mapping(value, label)
    normalized: dict[str, int] = {}
    for raw_name, raw_count in costs.items():
        name = clean_text(raw_name, f"{label} device", max_bytes=64)
        require(name not in normalized, f"{label} contains duplicate device {name}")
        normalized[name] = as_int(raw_count, f"{label}.{name}", minimum=0)
    return normalized


def normalize_cost_change(value: object, label: str = "delta.cost_change") -> dict[str, Any] | None:
    """逐章可选代价变化：{device, count}。缺省 None 表示本章无代价变化。"""
    if value is None:
        return None
    change = as_mapping(value, label)
    require_known_keys(change, {"device", "count"}, label)
    return {
        "device": clean_text(change.get("device"), f"{label}.device", max_bytes=64),
        "count": as_int(change.get("count"), f"{label}.count", minimum=0),
    }


def render_snapshot(name: str, snapshot: dict[str, Any], through_chapter: int, revision: int) -> str:
    def section(title: str, values: list[str]) -> list[str]:
        return [f"## {title}", *(f"- {item}" for item in values or ["无"]), ""]

    lines = [
        f"# {name}｜当前状态",
        "",
        f"- 状态修订：{revision}",
        f"- 截至章节：第{through_chapter}章",
        f"- 身份：{snapshot['identity']}",
        f"- 位置：{snapshot['location']}",
        f"- 当前目标：{snapshot['goal']}",
        f"- 身心状态：{snapshot['state']}",
        "",
    ]
    lines.extend(section("能力与资源", snapshot["abilities_resources"]))
    lines.extend(section("关键关系", snapshot["relationships"]))
    lines.extend(section("已知信息", snapshot["knowledge"]))
    lines.extend(section("未结事项", snapshot["open_threads"]))
    payload = "\n".join(lines).rstrip() + "\n"
    require(
        byte_size(payload) <= SNAPSHOT_MAX_BYTES,
        f"character snapshot {name} exceeds hard cap of {SNAPSHOT_MAX_BYTES} bytes",
    )
    return payload


def normalize_foreshadow_change(
    value: object,
    label: str,
    *,
    allow_delete: bool,
    through_chapter: int,
) -> dict[str, Any]:
    row = as_mapping(value, label)
    require_known_keys(
        row,
        {"action", "id", "summary", "planted_chapter", "planned_resolution_chapter", "status", "importance",
         "core_hook", "half_life_chapters", "depends_on", "parent_id"},
        label,
    )
    action = clean_text(row.get("action", "upsert"), f"{label}.action", max_bytes=24)
    require(action in ({"upsert", "delete"} if allow_delete else {"upsert"}), f"{label}.action is invalid")
    identifier = clean_text(row.get("id"), f"{label}.id", max_bytes=24)
    require(FORESHADOW_ID.fullmatch(identifier) is not None, f"{label}.id must look like F001")
    if action == "delete":
        return {"action": action, "id": identifier}
    planted_chapter = as_int(row.get("planted_chapter"), f"{label}.planted_chapter", minimum=1)
    require(planted_chapter <= through_chapter, f"{label}.planted_chapter cannot be in the future")
    planned_raw = row.get("planned_resolution_chapter")
    planned_chapter = (
        None if planned_raw is None else as_int(planned_raw, f"{label}.planned_resolution_chapter", minimum=1)
    )
    require(
        planned_chapter is None or planned_chapter >= planted_chapter,
        f"{label}.planned_resolution_chapter cannot precede planted_chapter",
    )
    status = clean_text(row.get("status"), f"{label}.status", max_bytes=24)
    importance = clean_text(row.get("importance"), f"{label}.importance", max_bytes=12)
    require(status in FORESHADOW_STATUSES, f"{label}.status must be one of {FORESHADOW_STATUSES}")
    require(importance in FORESHADOW_IMPORTANCE, f"{label}.importance must be one of {FORESHADOW_IMPORTANCE}")
    # debt 元数据（2026-09-05）：core_hook 主线承重 / half_life 过期升级 / depends_on 因果链 /
    # parent_id 复杂伏笔拆分。全部可选，缺省即旧行为；summary 仍是一行硬上限。
    core_hook = row.get("core_hook", False)
    require(isinstance(core_hook, bool), f"{label}.core_hook must be a boolean")
    half_life_raw = row.get("half_life_chapters")
    half_life = None if half_life_raw is None else as_int(half_life_raw, f"{label}.half_life_chapters", minimum=1)
    require(
        core_hook is False or half_life is not None or status != "已埋",
        f"{label}: core_hook=true 的已埋伏笔必须提供 half_life_chapters（否则过期检查无从谈起）",
    )
    depends_on_raw = row.get("depends_on", [])
    depends_on: list[str] = []
    for index, dep in enumerate(as_list(depends_on_raw, f"{label}.depends_on")):
        dep_id = clean_text(dep, f"{label}.depends_on[{index}]", max_bytes=24)
        require(FORESHADOW_ID.fullmatch(dep_id) is not None, f"{label}.depends_on[{index}] must look like F001")
        require(dep_id != identifier, f"{label}.depends_on cannot reference itself")
        if dep_id not in depends_on:
            depends_on.append(dep_id)
    parent_raw = row.get("parent_id")
    parent_id = None if parent_raw is None else clean_text(parent_raw, f"{label}.parent_id", max_bytes=24)
    if parent_id is not None:
        require(FORESHADOW_ID.fullmatch(parent_id) is not None, f"{label}.parent_id must look like F001")
        require(parent_id != identifier, f"{label}.parent_id cannot reference itself")
    return {
        "action": action,
        "id": identifier,
        "summary": clean_text(row.get("summary"), f"{label}.summary", max_bytes=360),
        "planted_chapter": planted_chapter,
        "planned_resolution_chapter": planned_chapter,
        "status": status,
        "importance": importance,
        "core_hook": core_hook,
        "half_life_chapters": half_life,
        "depends_on": depends_on,
        "parent_id": parent_id,
    }


def normalize_foreshadow_state(value: object, last_chapter: int) -> dict[str, dict[str, Any]]:
    rows = as_mapping(value, "tracking state.foreshadow")
    normalized: dict[str, dict[str, Any]] = {}
    for raw_identifier, raw_row in rows.items():
        identifier = clean_text(raw_identifier, "tracking state.foreshadow ID", max_bytes=24)
        row = as_mapping(raw_row, f"tracking state.foreshadow.{identifier}")
        require_known_keys(
            row,
            {"id", "summary", "planted_chapter", "planned_resolution_chapter", "status", "importance", "updated_chapter",
             "core_hook", "half_life_chapters", "depends_on", "parent_id"},
            f"tracking state.foreshadow.{identifier}",
        )
        require(row.get("id") == identifier, f"tracking state.foreshadow.{identifier}.id does not match its key")
        change = normalize_foreshadow_change(
            {
                "action": "upsert",
                **{key: value for key, value in row.items() if key != "updated_chapter"},
            },
            f"tracking state.foreshadow.{identifier}",
            allow_delete=False,
            through_chapter=last_chapter,
        )
        change.pop("action")
        updated = as_int(row.get("updated_chapter"), f"tracking state.foreshadow.{identifier}.updated_chapter", minimum=1)
        require(updated <= last_chapter, f"foreshadow {identifier} updates after current chapter")
        change["updated_chapter"] = updated
        normalized[identifier] = change
    return normalized


def render_foreshadow(rows: dict[str, dict[str, Any]], revision: int) -> str:
    lines = [
        "# 伏笔当前状态",
        "",
        f"> 状态修订：{revision}。每个 ID 只保留一行当前状态；历史变化见 `逐章记录/`。",
        "",
        "| ID | 内容 | 埋设章 | 计划回收章 | 状态 | 重要度 | 最近变更章 | 核心 | 半衰期 | 依赖 | 父伏笔 |",
        "|---|---|---:|---:|---|---|---:|---|---:|---|---|",
    ]
    for identifier in sorted(rows):
        row = rows[identifier]
        planned = f"第{row['planned_resolution_chapter']}章" if row["planned_resolution_chapter"] else "—"
        core = "是" if row.get("core_hook") else ""
        half = str(row["half_life_chapters"]) if row.get("half_life_chapters") else ""
        deps = " ".join(row.get("depends_on") or []) or ""
        parent = row.get("parent_id") or ""
        lines.append(
            f"| {identifier} | {row['summary']} | 第{row['planted_chapter']}章 | {planned} | "
            f"{row['status']} | {row['importance']} | 第{row['updated_chapter']}章 | {core} | {half} | {deps} | {parent} |"
        )
    return "\n".join(lines) + "\n"


def normalize_identity_match(value: object, label: str) -> dict[str, Any]:
    """P2：timeline 记录的 identity_match 规范化（人工/自动判定结果，含 provenance）。

    status 与 human decision 分离（外脑硬化修正）：禁止 EXACT_MATCH + DIFFERENT_EVENT 矛盾态。
    """
    m = as_mapping(value, label)
    require_known_keys(
        m,
        {"status", "story_event_id", "matched_identity_key", "confirmed_by", "confirmed_at", "decision"},
        label,
    )
    status = clean_text(m.get("status"), f"{label}.status", max_bytes=24)
    require(status in {"NEW", "EXACT_MATCH", "UNRESOLVED", "POSSIBLE_MATCH"}, f"{label}.status is invalid")
    raw_sid = m.get("story_event_id")
    sid = None if raw_sid is None else clean_text(raw_sid, f"{label}.story_event_id", max_bytes=16)
    if sid is not None:
        require(STORY_EVENT_ID.fullmatch(sid) is not None, f"{label}.story_event_id must look like SE-001")
    raw_key = m.get("matched_identity_key")
    mkey = None if raw_key is None else clean_text(raw_key, f"{label}.matched_identity_key", max_bytes=480)
    out: dict[str, Any] = {"status": status, "story_event_id": sid, "matched_identity_key": mkey}
    for field in ("confirmed_by", "confirmed_at", "decision"):
        raw = m.get(field)
        if raw is not None:
            out[field] = clean_text(raw, f"{label}.{field}", max_bytes=64)
    # 矛盾态禁止：EXACT_MATCH 不可能由 DIFFERENT_EVENT 产生；NEW 不可能由 SAME_EVENT 产生
    require(
        not (out["status"] == "EXACT_MATCH" and out.get("decision") == "DIFFERENT_EVENT"),
        f"{label}: EXACT_MATCH with decision=DIFFERENT_EVENT is contradictory",
    )
    require(
        not (out["status"] == "NEW" and out.get("decision") == "SAME_EVENT"),
        f"{label}: NEW with decision=SAME_EVENT is contradictory",
    )
    return out


def normalize_timeline_change(
    value: object,
    label: str,
    *,
    allow_delete: bool,
    through_chapter: int,
) -> dict[str, Any]:
    event = as_mapping(value, label)
    require_known_keys(
        event,
        {"action", "id", "story_time", "objective_fact", "reader_knowledge", "reveal_status", "reveal_chapter",
         "characters", "event_identity", "identity_status", "identity_match"},
        label,
    )
    action = clean_text(event.get("action", "upsert"), f"{label}.action", max_bytes=24)
    require(action in ({"upsert", "delete"} if allow_delete else {"upsert"}), f"{label}.action is invalid")
    identifier = clean_text(event.get("id"), f"{label}.id", max_bytes=24)
    require(EVENT_ID.fullmatch(identifier) is not None, f"{label}.id must look like E001")
    if action == "delete":
        return {"action": action, "id": identifier}
    reveal_status = clean_text(event.get("reveal_status"), f"{label}.reveal_status", max_bytes=24)
    require(reveal_status in REVEAL_STATUSES, f"{label}.reveal_status must be one of {REVEAL_STATUSES}")
    reveal_raw = event.get("reveal_chapter")
    reveal_chapter = None if reveal_raw is None else as_int(reveal_raw, f"{label}.reveal_chapter", minimum=1)
    if reveal_status == "未揭示":
        require(reveal_chapter is None, f"{label} must not put a future reveal chapter in established timeline facts")
    else:
        require(reveal_chapter is not None, f"{label}.reveal_chapter is required once revealed")
        require(reveal_chapter <= through_chapter, f"{label}.reveal_chapter cannot be in the future")
    # —— P1 Event Identity（2026-09-23 外脑裁决）——
    # 职责边界：LLM 提供受约束结构化候选（event_identity）；机械层只做 canonicalization + identity_key，
    # 不猜语义、不判重（EXACT/POSSIBLE 属 P2）。无法可靠解析 → event_identity=None + identity_status=UNRESOLVED。
    # identity_core = subject|action|object|state_change；location 为消歧字段，不进 core（样本验证后可调整）。
    identity_raw = event.get("event_identity")
    if identity_raw is None:
        identity, identity_status = None, "UNRESOLVED"
    else:
        identity_map = as_mapping(identity_raw, f"{label}.event_identity")
        # identity_key 允许回读（merge→normalize_state 会重跑本函数），但 canonical 始终由机械层重算。
        require_known_keys(
            identity_map,
            {"subject", "action", "object", "state_change", "location", "identity_key"},
            f"{label}.event_identity",
        )
        def _id_field(v: object, name: str, mb: int) -> str | None:
            return None if v is None else clean_text(v, f"{label}.event_identity.{name}", max_bytes=mb, allow_empty=True)

        i_subject = _id_field(identity_map.get("subject"), "subject", 60) or ""
        i_action = _id_field(identity_map.get("action"), "action", 60) or ""
        i_object = _id_field(identity_map.get("object"), "object", 120)
        i_state = _id_field(identity_map.get("state_change"), "state_change", 160) or ""
        i_location = _id_field(identity_map.get("location"), "location", 60)
        if i_subject and i_action and i_state:
            identity = {
                "subject": i_subject,
                "action": i_action,
                "object": i_object or None,
                "state_change": i_state,
                "location": i_location or None,
                # key 必须与存储层同一次清洗（clean_text 将 | 归一为 ｜），
                # 否则 checkpoint_record 输出与 normalize 重跑、SE 索引三方不一致。
                "identity_key": clean_text(
                    f"{i_subject}|{i_action}|{i_object or ''}|{i_state}",
                    f"{label}.event_identity.identity_key", max_bytes=480,
                ),
            }
            identity_status = "RESOLVED"
        else:
            identity, identity_status = None, "UNRESOLVED"
    return {
        "action": action,
        "id": identifier,
        "story_time": clean_text(event.get("story_time"), f"{label}.story_time", max_bytes=240),
        "objective_fact": clean_text(event.get("objective_fact"), f"{label}.objective_fact", max_bytes=480),
        "reader_knowledge": clean_text(event.get("reader_knowledge"), f"{label}.reader_knowledge", max_bytes=480),
        "reveal_status": reveal_status,
        "reveal_chapter": reveal_chapter,
        "characters": clean_string_list(event.get("characters", []), f"{label}.characters", maximum=12, item_max_bytes=120),
        "event_identity": identity,
        "identity_status": identity_status,
        "identity_match": (
            normalize_identity_match(event["identity_match"], f"{label}.identity_match")
            if event.get("identity_match") is not None
            else None
        ),
    }


def normalize_timeline_state(value: object, last_chapter: int) -> dict[str, dict[str, Any]]:
    events = as_mapping(value, "tracking state.timeline")
    normalized: dict[str, dict[str, Any]] = {}
    for raw_identifier, raw_event in events.items():
        identifier = clean_text(raw_identifier, "tracking state.timeline ID", max_bytes=24)
        event = as_mapping(raw_event, f"tracking state.timeline.{identifier}")
        require_known_keys(
            event,
            {
                "id", "story_time", "objective_fact", "reader_knowledge", "reveal_status", "reveal_chapter",
                "characters", "event_identity", "identity_status", "identity_match",
                "first_recorded_chapter", "updated_chapter",
            },
            f"tracking state.timeline.{identifier}",
        )
        require(event.get("id") == identifier, f"tracking state.timeline.{identifier}.id does not match its key")
        change = normalize_timeline_change(
            {
                "action": "upsert",
                **{
                    key: value
                    for key, value in event.items()
                    if key not in {"first_recorded_chapter", "updated_chapter"}
                },
            },
            f"tracking state.timeline.{identifier}",
            allow_delete=False,
            through_chapter=last_chapter,
        )
        change.pop("action")
        first = as_int(event.get("first_recorded_chapter"), f"tracking state.timeline.{identifier}.first_recorded_chapter", minimum=1)
        updated = as_int(event.get("updated_chapter"), f"tracking state.timeline.{identifier}.updated_chapter", minimum=1)
        require(first <= last_chapter, f"timeline event {identifier} starts after current chapter")
        require(updated <= last_chapter, f"timeline event {identifier} updates after current chapter")
        change["first_recorded_chapter"] = first
        change["updated_chapter"] = updated
        normalized[identifier] = change
    return normalized


def render_timeline_views(events: dict[str, dict[str, Any]], revision: int) -> tuple[str, str]:
    author_lines = [
        "# 作者真相时间线",
        "",
        f"> 状态修订：{revision}。客观事实与读者认知的权威对照；未来揭示计划仍留在大纲。",
        "",
        "| ID | 首次登记章 | 故事时间 | 客观事实 | 读者当前认知 | 揭示状态 | 实际揭示章 | 事件身份 |",
        "|---|---:|---|---|---|---|---:|---|",
    ]
    reader_lines = [
        "# 读者已知时间线",
        "",
        f"> 状态修订：{revision}。只呈现读者截至当前章节已经知道或相信的内容，不泄露作者侧客观真相。",
        "",
        "| ID | 读者当前认知 | 认知截至章 |",
        "|---|---|---:|",
    ]
    for identifier in sorted(events):
        event = events[identifier]
        reveal = f"第{event['reveal_chapter']}章" if event.get("reveal_chapter") else "—"
        characters = "、".join(event.get("characters", []))
        objective = event["objective_fact"] + (f"（涉及：{characters}）" if characters else "")
        identity_disp = event.get("identity_status", "UNRESOLVED")
        if event.get("event_identity"):
            identity_disp += "：" + event["event_identity"].get("identity_key", "")
        author_lines.append(
            f"| {identifier} | 第{event['first_recorded_chapter']}章 | {event['story_time']} | {objective} | "
            f"{event['reader_knowledge']} | {event['reveal_status']} | {reveal} | {identity_disp} |"
        )
        reader_lines.append(f"| {identifier} | {event['reader_knowledge']} | 第{event['updated_chapter']}章 |")
    return "\n".join(author_lines) + "\n", "\n".join(reader_lines) + "\n"


def validate_context_input(value: object, *, include_initial_fields: bool) -> dict[str, Any]:
    context = as_mapping(value, "context")
    allowed = {"position", "long_term_constraints", "active_character_names", "continuity_risks", "archived_summaries"}
    if include_initial_fields:
        allowed.update({"recent_chapters", "next_chapter_commitments"})
    require_known_keys(context, allowed, "context")
    normalized: dict[str, Any] = {
        "position": validate_position(context.get("position")),
        "long_term_constraints": clean_string_list(
            context.get("long_term_constraints", []), "context.long_term_constraints", maximum=12
        ),
        "active_character_names": [
            safe_file_component(name, f"context.active_character_names[{index}]")
            for index, name in enumerate(as_list(context.get("active_character_names", []), "context.active_character_names"))
        ],
        "continuity_risks": clean_string_list(
            context.get("continuity_risks", []), "context.continuity_risks", maximum=5
        ),
    }
    require(len(normalized["active_character_names"]) <= 6, "context.active_character_names may contain at most 6 names")
    require(
        len({portable_name_key(name) for name in normalized["active_character_names"]})
        == len(normalized["active_character_names"]),
        "context.active_character_names contains cross-platform duplicates",
    )
    if include_initial_fields:
        recent: list[dict[str, Any]] = []
        for index, raw_item in enumerate(as_list(context.get("recent_chapters", []), "context.recent_chapters")):
            item = as_mapping(raw_item, f"context.recent_chapters[{index}]")
            require_known_keys(item, {"chapter", "summary"}, f"context.recent_chapters[{index}]")
            recent.append(
                {
                    "chapter": as_int(item.get("chapter"), f"context.recent_chapters[{index}].chapter", minimum=1),
                    "summary": clean_text(item.get("summary"), f"context.recent_chapters[{index}].summary", max_bytes=360),
                }
            )
        require(len(recent) <= 3, "context.recent_chapters may contain at most 3 items")
        normalized["recent_chapters"] = recent
        normalized["next_chapter_commitments"] = clean_string_list(
            context.get("next_chapter_commitments", []), "context.next_chapter_commitments", maximum=5
        )
    # K4: 归档摘要——被 recent_chapters 滑窗截断的章节摘要折叠到这里，上限 50 条
    raw_archived = context.get("archived_summaries", [])
    if isinstance(raw_archived, list):
        archived = []
        for index, item in enumerate(raw_archived[:50]):
            if isinstance(item, dict):
                archived.append({
                    "chapter": as_int(item.get("chapter"), f"context.archived_summaries[{index}].chapter", minimum=1),
                    "summary": clean_text(item.get("summary", ""), f"context.archived_summaries[{index}].summary", max_bytes=360),
                })
        normalized["archived_summaries"] = archived
    else:
        normalized["archived_summaries"] = []
    return normalized


def stale_core_hook_lines(foreshadow: dict[str, dict[str, Any]], current_chapter: int) -> list[str]:
    """核心承重伏笔超半衰期未更新 → 升级告警行（注入上下文连贯性风险 + check 报告）。

    以 updated_chapter（最近一次实质变更）起算：埋下/推进后 half_life 章无任何
    变更即告警。仅 core_hook 生效，普通伏笔仍走 consistency S4 的超期扫描，不双报。
    """
    lines: list[str] = []
    for identifier in sorted(foreshadow):
        row = foreshadow[identifier]
        if not row.get("core_hook") or row.get("status") != "已埋":
            continue
        half_life = row.get("half_life_chapters")
        if not half_life:
            continue
        stale = current_chapter - row.get("updated_chapter", row["planted_chapter"])
        if stale >= half_life:
            lines.append(
                f"核心伏笔 {identifier} 已 {stale} 章未推进（半衰期 {half_life}）：{row['summary']}"
            )
    return lines


def orphan_hook_reference_lines(foreshadow: dict[str, dict[str, Any]]) -> list[str]:
    """depends_on / parent_id 指向不存在或已回收的伏笔 → 提示行（仅 check 报告）。"""
    lines: list[str] = []
    for identifier in sorted(foreshadow):
        row = foreshadow[identifier]
        for dep in row.get("depends_on") or []:
            target = foreshadow.get(dep)
            if target is None or target.get("status") != "已埋":
                lines.append(f"{identifier}.depends_on → {dep} 不存在或已不在池中")
        parent = row.get("parent_id")
        if parent and (parent not in foreshadow or foreshadow[parent].get("status") != "已埋"):
            lines.append(f"{identifier}.parent_id → {parent} 不存在或已不在池中")
    return lines


def active_foreshadow_lines(rows: dict[str, dict[str, Any]]) -> list[str]:
    importance = {value: index for index, value in enumerate(FORESHADOW_IMPORTANCE)}
    # K5：已埋 + 需处理 均为活跃伏笔（需处理 = 半衰期超期，需在近章安排回收/推进/显式延期）
    candidates = [row for row in rows.values() if row["status"] in ("已埋", "需处理")]
    candidates.sort(
        key=lambda row: (
            not row.get("core_hook", False),  # 核心承重伏笔永远排最前，不被挤出热上下文
            importance[row["importance"]], row["planned_resolution_chapter"] or 10**12, row["id"],
        )
    )
    result = []
    for row in candidates[:8]:
        planned = f"第{row['planned_resolution_chapter']}章" if row["planned_resolution_chapter"] else "回收章未定"
        mark = "★" if row.get("core_hook") else ""
        if row["status"] == "需处理":
            mark += "⚠"
        result.append(f"{row['id']}{mark}｜{row['summary']}｜埋第{row['planted_chapter']}章｜{planned}｜{row['importance']}")
    return result


def _global_context_lines(global_state: dict[str, Any]) -> list[str]:
    """从 global 状态提取紧凑的上下文行（用于 上下文.md 的「全局状态」段）。

    global 结构由项目自由定义，这里做通用提取：
    - position 子字段逐行显示
    - world.phase / world.changes（最近3条）
    - factions 逐行显示 phase/stance
    - next_trigger 单独一行
    - 其他顶层字段也做通用显示

    注意：所有字典键按字母顺序遍历，与 json.dumps(sort_keys=True) 保持一致，
    确保 init 渲染与 check 重渲染的输出逐字一致。
    """
    if not global_state:
        return ["（尚未写入全局状态）"]
    lines: list[str] = []
    # 按固定优先级 + 字母顺序遍历顶层键
    priority = ["position", "world", "factions", "next_trigger"]
    ordered_keys = priority + sorted(k for k in global_state if k not in priority)
    for key in ordered_keys:
        if key not in global_state:
            continue
        value = global_state[key]
        if key == "position" and isinstance(value, dict):
            for sub_key in sorted(value):
                sub_value = value[sub_key]
                if isinstance(sub_value, dict):
                    parts = [f"{k}={v}" for k, v in sorted(sub_value.items())]
                    lines.append(f"position.{sub_key}：{'；'.join(parts)}")
                else:
                    lines.append(f"position.{sub_key}：{sub_value}")
        elif key == "world" and isinstance(value, dict):
            if value.get("phase"):
                lines.append(f"world.phase：{value['phase']}")
            changes = value.get("changes", [])
            if isinstance(changes, list) and changes:
                recent = changes[-3:]
                lines.append(f"world.changes（最近{len(recent)}条）：")
                for change in recent:
                    lines.append(f"  - {change}")
        elif key == "factions" and isinstance(value, dict) and value:
            lines.append("factions：")
            for name in sorted(value):
                info = value[name]
                if isinstance(info, dict):
                    parts = [f"{k}={v}" for k, v in sorted(info.items())]
                    lines.append(f"  - {name}：{'；'.join(parts)}")
                else:
                    lines.append(f"  - {name}：{info}")
        elif key == "next_trigger" and value:
            lines.append(f"next_trigger：{value}")
        else:
            # 其他未被上述分类覆盖的顶层字段
            if isinstance(value, dict):
                parts = [f"{k}={v}" for k, v in sorted(value.items()) if not isinstance(v, (dict, list))]
                lines.append(f"{key}：{'；'.join(parts) if parts else str(value)}")
            elif isinstance(value, list):
                lines.append(f"{key}：{'；'.join(str(v) for v in value[:5])}")
            else:
                lines.append(f"{key}：{value}")
    return lines


def render_context(state: dict[str, Any]) -> str:
    context = state["context"]
    position = context["position"]
    current_chapter = (
        "尚未开篇" if state["last_committed_chapter"] == 0 else f"第{state['last_committed_chapter']}章"
    )
    character_lines = [
        f"{name}｜{state['characters'][name]['identity']}｜{state['characters'][name]['state']}｜"
        f"目标：{state['characters'][name]['goal']}"
        for name in context["active_character_names"]
    ]
    sections: list[tuple[str, list[str]]] = [
        (
            "## 当前位置",
            [
                f"当前章：{current_chapter}",
                f"卷：{position['volume']}（始于第{position['volume_start_chapter']}章）",
                f"故事时间：{position['story_time']}",
                f"场景：{position['scene']}",
            ],
        ),
        (
            "## 全局状态",
            _global_context_lines(state.get("global", {})),
        ),
        ("## 长期约束", context["long_term_constraints"]),
        ("## 核心角色状态", character_lines),
        ("## 活跃伏笔", active_foreshadow_lines(state["foreshadow"])),
        ("## 近三章速记", [f"第{item['chapter']}章｜{item['summary']}" for item in context["recent_chapters"]]),
        ("## 下一章承诺", context["next_chapter_commitments"]),
        ("## 连贯性风险", [
            *context["continuity_risks"],
            *stale_core_hook_lines(state["foreshadow"], state["last_committed_chapter"]),
        ]),
    ]
    lines = [
        f"# 写作连续性上下文 — {state['book_title']}",
        "",
        f"> 状态修订：{state['state_revision']}。截至当前章的续写状态卡，只放下一章真正需要的连续性状态。",
        "",
    ]
    for heading, values in sections:
        lines.append(heading)
        lines.extend(f"- {value}" for value in values or ["无"])
        lines.append("")
    payload = "\n".join(lines).rstrip() + "\n"
    headings = tuple(line for line in payload.splitlines() if line.startswith("## "))
    require(headings == CONTEXT_HEADINGS, "generated context headings do not match the seven-section schema")
    require(byte_size(payload) <= CONTEXT_MAX_BYTES, f"hot context exceeds {CONTEXT_MAX_BYTES} bytes")
    return payload


def render_global_view(state: dict[str, Any]) -> str:
    """渲染全局运行时状态视图：作品当前处于什么状态（纪元/卷/核心冲突/发动机/世界变化/势力/下一切换点）。

    global 字段结构由项目自由定义，这里做通用的层级化 Markdown 渲染。
    global 为空时输出空态提示，不阻断。
    """
    global_state = state.get("global", {})
    revision = state["state_revision"]
    lines = [
        "# 全局运行时状态",
        "",
        f"> 状态修订：{revision}。截至第{state['last_committed_chapter']}章的作品全局快照——"
        "只记录「现在已经变成什么」，不记录设定（设定文件的事）和未来（大纲的事）。",
        "",
    ]
    if not global_state:
        lines.append("（尚未写入全局状态；首次 commit 时通过 global_change 增量填充。）")
        return "\n".join(lines) + "\n"

    def render_section(title: str, value: Any, depth: int = 2) -> list[str]:
        prefix = "#" * depth
        out = [f"{prefix} {title}", ""]
        if isinstance(value, dict):
            for k in sorted(value):
                v = value[k]
                if isinstance(v, (dict, list)):
                    out.extend(render_section(k, v, depth + 1))
                else:
                    out.append(f"- **{k}**：{v}")
            out.append("")
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    # 列表中的 dict 用一行紧凑表示
                    parts = [f"{kk}={vv}" for kk, vv in sorted(item.items())]
                    out.append(f"- {'；'.join(parts)}")
                else:
                    out.append(f"- {item}")
            out.append("")
        else:
            out.append(f"{value}")
            out.append("")
        return out

    for key in sorted(global_state):
        lines.extend(render_section(key, global_state[key]))

    payload = "\n".join(lines).rstrip() + "\n"
    require(byte_size(payload) <= 16384, f"global view exceeds hard cap of 16384 bytes")
    return payload


def normalize_quality_meta(value: object) -> dict[str, Any] | None:
    """V12.3 新增：逐章质量元数据（declared 模型声明 + observed 系统自动生成）。

    可选字段；不存在返回 None。declared 由模型填写，observed 在 commit 时由系统自动填充。
    V12.5 新增：post_write 流程完整性元数据（enforced/reviewed），由 nww_post_write.py /
    nww_review_cycle.py 写入，供 A-5 闸门校验流程完整性。
    """
    if value is None:
        return None
    meta = as_mapping(value, "delta.quality_meta")
    # ★ 容错（2026-11-01 修复）：quality_meta 含 LLM 在 delta 语义补全阶段产出的字段，
    #   非确定性模型偶发把字数/可见字等无关字段（如 actual_visible_chars_note /
    #   word_count_target）塞进 quality_meta。这些是「可选元数据」，缺失或多余都不影响
    #   「流程完整性」校验，不应 BLOCK 合法提交——实测：deslop/润色整轮跑完、正文已改，
    #   却因两个无关字段 commit 被拒 → 事务回滚 → 用户侧「什么都没变」。
    #   处置：剥离未知字段 + 打印 WARNING（可观测），白名单字段的既有校验语义保持不变。
    #   （白名单仍作为「代码侧**有意**新增字段须同步」的契约文档；此处只放行
    #   LLM/历史残留的未知键，不纵容代码侧漏改白名单。）
    # C-QUALITY-META@1：允许键单源在 nww_delta_schema.QUALITY_META_ALLOWED，
    # 新增字段只改那一处，避免本文件与 web 侧各维护一份清单。
    _QM_ALLOWED = _QUALITY_META_ALLOWED
    _qm_unknown = set(meta) - _QM_ALLOWED
    if _qm_unknown:
        print(f"[QUALITY_META] 剥离 quality_meta 未知字段（不阻断提交）: {sorted(_qm_unknown)}", flush=True)
        meta = {k: v for k, v in meta.items() if k in _QM_ALLOWED}
    declared_raw = meta.get("declared", {})
    declared = as_mapping(declared_raw, "delta.quality_meta.declared")
    require_known_keys(declared, {"protagonist_initiative"}, "delta.quality_meta.declared")
    initiative = declared.get("protagonist_initiative")
    if initiative is not None:
        require(isinstance(initiative, bool), "delta.quality_meta.declared.protagonist_initiative must be boolean")
    observed_raw = meta.get("observed", {})
    observed = as_mapping(observed_raw, "delta.quality_meta.observed")
    # observed 由系统在 commit 时自动填充；模型提交时允许留空 {}。
    result: dict[str, Any] = {"declared": dict(declared), "observed": dict(observed)}
    # V12.5 post_write 流程完整性元数据（可选，A-5 闸门校验）
    if "enforced" in meta:
        require(isinstance(meta["enforced"], bool), "delta.quality_meta.enforced must be boolean")
        result["enforced"] = meta["enforced"]
    if "enforced_at" in meta:
        result["enforced_at"] = as_int(meta["enforced_at"], "delta.quality_meta.enforced_at", minimum=0)
    # C-ENFORCE-TRUTH@1（2026-11-13 R5）：enforced_attempted 证明「本轮确实执行过 enforce」。
    # 白名单（nww_delta_schema.QUALITY_META_ALLOWED）只负责剥离未知键，值必须在此显式拷入
    # result —— 只放行不拷入等于静默丢弃（与 reviewed_hash/final_hash 同一类缺陷）。
    if "enforced_attempted" in meta:
        require(isinstance(meta["enforced_attempted"], bool),
                "delta.quality_meta.enforced_attempted must be boolean")
        result["enforced_attempted"] = meta["enforced_attempted"]
    if "reviewed" in meta:
        require(isinstance(meta["reviewed"], bool), "delta.quality_meta.reviewed must be boolean")
        result["reviewed"] = meta["reviewed"]
    if "reviewed_at" in meta:
        result["reviewed_at"] = as_int(meta["reviewed_at"], "delta.quality_meta.reviewed_at", minimum=0)
    if "review_score" in meta:
        result["review_score"] = as_int(meta["review_score"], "delta.quality_meta.review_score", minimum=0)
    if "review_rounds" in meta:
        result["review_rounds"] = as_int(meta["review_rounds"], "delta.quality_meta.review_rounds", minimum=0)
    # V12.5 P0-1：finalize 留痕（review_id 可为 null —— 存量/无身份调用时）
    if "finalized" in meta:
        require(isinstance(meta["finalized"], bool), "delta.quality_meta.finalized must be boolean")
        result["finalized"] = meta["finalized"]
    if "finalized_at" in meta:
        result["finalized_at"] = as_int(meta["finalized_at"], "delta.quality_meta.finalized_at", minimum=0)
    if "review_id" in meta:
        rid = meta["review_id"]
        require(
            rid is None or isinstance(rid, str),
            "delta.quality_meta.review_id must be a string or null",
        )
        result["review_id"] = rid
    # P0-3a：finalize 冻结哈希（SHA-256 hex64）。白名单放行 + result 拷入缺一不可：
    # 只放行不拷入 → A-5 拿不到哈希 → 交付身份校验静默失效。
    if "reviewed_hash" in meta:
        result["reviewed_hash"] = _normalize_frozen_hash(meta["reviewed_hash"], "delta.quality_meta.reviewed_hash")
    if "final_hash" in meta:
        result["final_hash"] = _normalize_frozen_hash(meta["final_hash"], "delta.quality_meta.final_hash")
    return result


def _normalize_frozen_hash(value: object, label: str) -> str:
    """P0-3a：finalize 冻结哈希格式校验（64 位小写 hex 的 SHA-256）。"""
    require(
        isinstance(value, str) and len(value) == 64
        and all(c in "0123456789abcdef" for c in value.lower()),
        f"{label} must be a 64-char lowercase hex sha256",
    )
    return value.lower()


def normalize_rule_loss_event(value: object) -> dict[str, Any] | None:
    """V12.4 新增：规则丢失事件（写入 archive/rule_history.json，不污染 global）。

    可选字段；不存在返回 None。只记录 actual（实际发生），不记录 planned。
    """
    if value is None:
        return None
    event = as_mapping(value, "delta.rule_loss_event")
    require_known_keys(event, {"rule_id", "rule_name", "actual"}, "delta.rule_loss_event")
    rule_id = clean_text(event.get("rule_id"), "delta.rule_loss_event.rule_id", max_bytes=64)
    require(rule_id != "", "delta.rule_loss_event.rule_id is required")
    rule_name = clean_text(event.get("rule_name") or "", "delta.rule_loss_event.rule_name", max_bytes=64)
    actual_raw = event.get("actual", {})
    actual = as_mapping(actual_raw, "delta.rule_loss_event.actual")
    require_known_keys(actual, {"method", "cost", "impact", "emotion"}, "delta.rule_loss_event.actual")
    normalized_actual = {
        "method": clean_text(actual.get("method") or "", "delta.rule_loss_event.actual.method", max_bytes=120),
        "cost": clean_text(actual.get("cost") or "", "delta.rule_loss_event.actual.cost", max_bytes=120),
        "impact": clean_text(actual.get("impact") or "", "delta.rule_loss_event.actual.impact", max_bytes=240),
        "emotion": clean_text(actual.get("emotion") or "", "delta.rule_loss_event.actual.emotion", max_bytes=64),
    }
    return {"rule_id": rule_id, "rule_name": rule_name, "actual": normalized_actual}


def normalize_candidate_facts(value: object, label: str = "delta.candidate_facts") -> list[dict[str, Any]]:
    """P0-1 候选事实层：LLM 只能产出 candidate，禁止直接产出 confirmed。

    provenance_gate（P0-2）负责将 candidate 分流为 confirmed / belief / hint。
    """
    items: list[dict[str, Any]] = []
    for index, raw in enumerate(as_list(value, label)):
        row = as_mapping(raw, f"{label}[{index}]")
        require_known_keys(row, {"text", "candidate_type", "confidence", "source_hint"}, f"{label}[{index}]")
        ctype = clean_text(row.get("candidate_type"), f"{label}[{index}].candidate_type", max_bytes=24)
        require(ctype in FACT_TYPES, f"{label}[{index}].candidate_type must be one of {FACT_TYPES}")
        confidence = clean_text(row.get("confidence"), f"{label}[{index}].confidence", max_bytes=24)
        require(confidence in CONFIDENCE_LEVELS, f"{label}[{index}].confidence must be one of {CONFIDENCE_LEVELS}")
        items.append(
            {
                "text": clean_text(row.get("text"), f"{label}[{index}].text", max_bytes=240),
                "candidate_type": ctype,
                "confidence": confidence,
                "source_hint": normalize_source(row.get("source_hint"), f"{label}[{index}].source_hint"),
            }
        )
    return items


def normalize_fact_changes(value: object, label: str = "delta.fact_changes") -> dict[str, Any]:
    """P0-1/2 事实变更段：confirmed（add）/ hints（schema-only，P0-3 消费）/ retire（按 id 回收）。

    P0-1 仅 schema 接收与结构校验；P0-2 起 merge 消费 provenance_gate 审批后的
    confirmed/retire，hints 保留 schema 不落库（属 writer 辅助记忆层，留待 P0-3）。
    """
    root = as_mapping(value, label)
    require_known_keys(root, {"confirmed", "hints", "retire"}, label)
    confirmed: list[dict[str, Any]] = []
    for index, raw in enumerate(as_list(root.get("confirmed", []), f"{label}.confirmed")):
        row = as_mapping(raw, f"{label}.confirmed[{index}]")
        require_known_keys(row, {"id", "fact", "fact_type", "source"}, f"{label}.confirmed[{index}]")
        fid = clean_text(row.get("id"), f"{label}.confirmed[{index}].id", max_bytes=24)
        require(FACT_ID.fullmatch(fid) is not None, f"{label}.confirmed[{index}].id must look like T001")
        fact_type = clean_text(row.get("fact_type"), f"{label}.confirmed[{index}].fact_type", max_bytes=24)
        require(fact_type in FACT_TYPES, f"{label}.confirmed[{index}].fact_type must be one of {FACT_TYPES}")
        source = normalize_source(row.get("source"), f"{label}.confirmed[{index}].source")
        require(source is not None, f"{label}.confirmed[{index}].source is required (confirmed 必须有来源)")
        confirmed.append(
            {
                "id": fid,
                "fact": clean_text(row.get("fact"), f"{label}.confirmed[{index}].fact", max_bytes=240),
                "fact_type": fact_type,
                "source": source,
            }
        )
    hints: list[dict[str, Any]] = []
    for index, raw in enumerate(as_list(root.get("hints", []), f"{label}.hints")):
        row = as_mapping(raw, f"{label}.hints[{index}]")
        require_known_keys(row, {"id", "hint"}, f"{label}.hints[{index}]")
        hid = clean_text(row.get("id"), f"{label}.hints[{index}].id", max_bytes=24)
        require(FACT_ID.fullmatch(hid) is not None, f"{label}.hints[{index}].id must look like T001")
        hints.append(
            {
                "id": hid,
                "hint": clean_text(row.get("hint"), f"{label}.hints[{index}].hint", max_bytes=240),
            }
        )
    retire = [
        clean_text(item, f"{label}.retire[{index}]", max_bytes=24)
        for index, item in enumerate(as_list(root.get("retire", []), f"{label}.retire"))
    ]
    for index, fid in enumerate(retire):
        require(FACT_ID.fullmatch(fid) is not None, f"{label}.retire[{index}] must look like T001")
    require(len(set(retire)) == len(retire), f"{label}.retire contains duplicate IDs")
    require(len({item["id"] for item in confirmed}) == len(confirmed), f"{label}.confirmed contains duplicate IDs")
    require(len({item["id"] for item in hints}) == len(hints), f"{label}.hints contains duplicate IDs")
    return {"confirmed": confirmed, "hints": hints, "retire": retire}


def _bible_fact_provenance_gate(project: Path) -> bool:
    """读 PolicyBundle：fact_provenance；缺字段按 policy.fail_closed（默认 True）。"""
    return load_policy(project).effective("fact_provenance")


def verify_fact_provenance(project: Path, transaction: dict[str, Any], state: dict[str, Any]) -> None:
    """P0-2 事实来源审查闸：插在 normalize_transaction 与 merge_transaction 之间，纯确定性、零 LLM。

    分工（外部决策层冻结）：
    - normalize_delta 保证结构合法（source 必填、类型合法、ID 格式、去重）；
    - 本闸判断业务合法（candidate 背书 / 来源不得指向未来章 / 禁止直接修改 confirmed /
      retire 须已有 contract / 同事务不重叠）；
    - merge_transaction 只负责把 gate 批准的 fact_changes 机械落库。

    effective_contract_state（active/schema_only）由系统能力派生：
    bible.commit_gates.fact_provenance.enabled=true → active，否则 schema_only。
    writer 的 delta.contract_state 仅作审计参考，不决定状态。
    结果写回 delta["_contract_state_effective"]（_ 前缀注解命名空间，不参与校验与合并），
    供 render_delta 展示。

    门控与 A-3/A-4 同一兼容模式语义：enabled=true → 违规 BLOCK；缺失/false → 仅 WARNING 不阻断。
    """
    gate = _bible_fact_provenance_gate(project)
    effective = "active" if gate else "schema_only"
    delta = transaction["delta"]
    # 审计告警仅针对 writer 的显式声明：旧形态 delta 未声明 contract_state 时 gate 零动作零噪音
    # （对齐冻结裁决「gate 无动作 / writer 写什么才审计」；normalize 对未声明置 None）。
    if delta.get("contract_state") is not None and delta.get("contract_state") != effective:
        emit(
            f"[FACT PROVENANCE WARNING] delta.contract_state={delta.get('contract_state')} "
            f"与系统实际能力 {effective} 不一致"
            "（契约状态由 commit_gates.fact_provenance.enabled 决定，writer 自报仅供参考）。",
            error=True,
        )

    changes = delta.get("fact_changes") or {}
    confirmed = changes.get("confirmed") or []
    retire = changes.get("retire") or []
    hints = changes.get("hints") or []
    if hints:
        emit(
            "[FACT PROVENANCE NOTE] fact_changes.hints 本期仅保留 schema，不落库（P0-3 接入）。",
            error=True,
        )
    delta["_contract_state_effective"] = effective
    if not confirmed and not retire:
        # 无事实变更动作：gate 无动作（candidate 仅是提案，未批准不落库）
        return

    violations: list[str] = []
    existing_confirmed_ids = {f["id"] for f in (state.get("facts") or {}).get("confirmed", [])}

    # G1 candidate 背书：confirmed 必须来自同 delta 的 approved candidate。
    # candidate 是提案、fact_changes 是批准动作、merge 是执行动作；writer 不得直接写 confirmed。
    # G1 仅豁免 backfill：对已完成人工/外脑裁决的历史事实做 canonical 回填，本身非
    # writer candidate→confirmed 晋升流程；严禁被 append/revision 复用（外脑裁决，只锁 mode==backfill）。
    if transaction.get("mode") != "backfill":
        cand_keys = {(c["text"], c["candidate_type"]) for c in (delta.get("candidate_facts") or [])}
        for row in confirmed:
            if (row["fact"], row["fact_type"]) not in cand_keys:
                violations.append(
                    f"confirmed {row['id']}（{row['fact']}）没有同 delta 的 approved candidate 背书"
                    "（writer 不得直接写入 confirmed facts）"
                )

    # G2 来源不得指向未来章（禁止未来信息污染）。
    current_chapter = transaction["chapter"]
    for row in confirmed:
        src = row.get("source") or {}
        if src.get("type") == "chapter" and src.get("chapter", 0) > current_chapter:
            violations.append(
                f"confirmed {row['id']} source.chapter={src.get('chapter')} 超过当前章 {current_chapter}"
                "（来源不得指向未来章）"
            )

    # G3 bible 来源可定位：source.type=bible 时 bible.json 必须存在。
    # （key 级定位需 source schema 增加定位字段，属 P0-3 消费层，本期 schema 不扩展。）
    for row in confirmed:
        src = row.get("source") or {}
        if src.get("type") == "bible" and not (project / "bible.json").is_file():
            violations.append(f"confirmed {row['id']} source.type=bible 但 bible.json 不存在")

    # G4 禁止直接修改 confirmed facts：id 已存在于 state.facts.confirmed → 先 retire 再 add。
    for row in confirmed:
        if row["id"] in existing_confirmed_ids:
            violations.append(
                f"confirmed {row['id']} 已存在于 state.facts.confirmed"
                "（禁止直接修改 confirmed facts；需先 retire 再 add）"
            )

    # G5 retire 须已有 contract：id 必须存在于 state.facts.confirmed。
    for fid in retire:
        if fid not in existing_confirmed_ids:
            violations.append(f"retire {fid} 不在 state.facts.confirmed 中（无 contract 可 retire）")

    # G6 同事务内 confirmed 与 retire 不得引用同一 id。
    overlap = {row["id"] for row in confirmed} & set(retire)
    if overlap:
        violations.append(f"同事务内 confirmed 与 retire 重复引用：{sorted(overlap)}")

    if violations:
        message = "[FACT PROVENANCE] 事实契约审查未通过：\n- " + "\n- ".join(violations)
        if gate:
            raise TrackingError(message + "\n修复：按 candidate → approved → fact_changes 闭环补齐后重新提交。")
        emit(message + "（兼容模式，不阻断提交）", error=True)


def normalize_delta(
    value: object,
    *,
    through_chapter: int,
    snapshots: dict[str, dict[str, Any]],
    existing_core_names: dict[str, str],
) -> dict[str, Any]:
    delta = as_mapping(value, "delta")
    # 白名单取自 nww_delta_schema.DELTA_ALLOWED（C-DELTA-SCHEMA@1）：与 pipeline 侧投影同一来源。
    require_known_keys(delta, _DELTA_ALLOWED, "delta")
    # P0-1/2 事实来源契约：contract_state 由 provenance_gate 按系统能力派生
    # （gate enabled → active，否则 schema_only）。此处仅收 writer 的**显式声明**作
    # 审计参考；缺省**不注入**——否则归一化后 gate 无法区分「显式声明」与「缺省」，
    # 旧形态 delta 会对 active 书产生每次 commit 的告警噪音（冻结裁决：gate 无动作）。
    contract_state: str | None = None
    if "contract_state" in delta:
        contract_state = clean_text(delta.get("contract_state"), "delta.contract_state", max_bytes=24)
        require(contract_state in CONTRACT_STATES, f"delta.contract_state must be one of {CONTRACT_STATES}")
    candidate_facts = normalize_candidate_facts(delta.get("candidate_facts", []))
    fact_changes = normalize_fact_changes(delta.get("fact_changes", {}))
    retired_characters = [
        safe_file_component(name, f"delta.retired_characters[{index}]")
        for index, name in enumerate(as_list(delta.get("retired_characters", []), "delta.retired_characters"))
    ]
    retired_keys = [portable_name_key(name) for name in retired_characters]
    require(len(retired_keys) == len(set(retired_keys)), "delta.retired_characters contains duplicate characters")
    retiring = set(retired_keys)
    revived_characters = [
        safe_file_component(name, f"delta.revived_characters[{index}]")
        for index, name in enumerate(as_list(delta.get("revived_characters", []), "delta.revived_characters"))
    ]
    revived_keys = [portable_name_key(name) for name in revived_characters]
    require(len(revived_keys) == len(set(revived_keys)), "delta.revived_characters contains duplicate characters")
    require(
        not (retiring & set(revived_keys)),
        "delta cannot retire and revive the same character in one transaction",
    )
    retirement_reasons: dict[str, str] = {}
    raw_reasons = as_mapping(delta.get("retirement_reasons", {}), "delta.retirement_reasons")
    for raw_name, raw_reason in raw_reasons.items():
        name = safe_file_component(raw_name, "delta.retirement_reasons key")
        require(
            portable_name_key(name) in retiring,
            f"delta.retirement_reasons[{name}] does not match any retired character",
        )
        retirement_reasons[name] = clean_text(raw_reason, f"delta.retirement_reasons[{name}]", max_bytes=240)
    character_changes: list[dict[str, Any]] = []
    for index, raw_change in enumerate(as_list(delta.get("character_changes", []), "delta.character_changes")):
        change = as_mapping(raw_change, f"delta.character_changes[{index}]")
        require_known_keys(change, {"name", "change"}, f"delta.character_changes[{index}]")
        name = safe_file_component(change.get("name"), f"delta.character_changes[{index}].name")
        existing = existing_core_names.get(portable_name_key(name))
        is_core = name in snapshots or existing is not None
        # 本章退役的角色记录最后一次变化即可，不必再交一份马上要删的快照。
        require(
            not is_core or name in snapshots or portable_name_key(name) in retiring,
            f"core character {name} changed but has no current snapshot",
        )
        character_changes.append(
            {"name": name, "change": clean_text(change.get("change"), f"delta.character_changes[{index}].change", max_bytes=360)}
        )
    character_keys = [portable_name_key(item["name"]) for item in character_changes]
    require(len(character_keys) == len(set(character_keys)), "delta.character_changes contains duplicate characters")
    foreshadow_changes = [
        normalize_foreshadow_change(
            raw, f"delta.foreshadow_changes[{index}]", allow_delete=True, through_chapter=through_chapter
        )
        for index, raw in enumerate(as_list(delta.get("foreshadow_changes", []), "delta.foreshadow_changes"))
    ]
    timeline_events = [
        normalize_timeline_change(
            raw, f"delta.timeline_events[{index}]", allow_delete=True, through_chapter=through_chapter
        )
        for index, raw in enumerate(as_list(delta.get("timeline_events", []), "delta.timeline_events"))
    ]
    require(
        len({item["id"] for item in foreshadow_changes}) == len(foreshadow_changes),
        "delta.foreshadow_changes contains duplicate IDs",
    )
    require(
        len({item["id"] for item in timeline_events}) == len(timeline_events),
        "delta.timeline_events contains duplicate IDs",
    )
    require(
        set(snapshots).issubset({item["name"] for item in character_changes}),
        "character_snapshots must contain exactly the core characters changed by this transaction",
    )
    return {
        "result": clean_text(delta.get("result"), "delta.result", max_bytes=480),
        "character_changes": character_changes,
        "foreshadow_changes": foreshadow_changes,
        "timeline_events": timeline_events,
        "constraints": clean_string_list(delta.get("constraints", []), "delta.constraints", maximum=12),
        "next_chapter_commitments": clean_string_list(
            delta.get("next_chapter_commitments", []), "delta.next_chapter_commitments", maximum=5
        ),
        "retired_context_items": clean_string_list(
            delta.get("retired_context_items", []), "delta.retired_context_items", maximum=11
        ),
        "retired_characters": retired_characters,
        "revived_characters": revived_characters,
        "retirement_reasons": retirement_reasons,
        "cost_change": normalize_cost_change(delta.get("cost_change")),
        "quality_meta": normalize_quality_meta(delta.get("quality_meta")),
        "rule_loss_event": normalize_rule_loss_event(delta.get("rule_loss_event")),
        # 章节类型（如 战斗/日常/交易/爆发），≤24B；省略表示不登记
        "chapter_type": (
            None if delta.get("chapter_type") is None
            else clean_text(delta.get("chapter_type"), "delta.chapter_type", max_bytes=24)
        ),
        # P0-1/2 事实来源契约：字段随事务传递；P0-2 起 merge 消费 gate 审批后的 fact_changes
        "contract_state": contract_state,
        "candidate_facts": candidate_facts,
        "fact_changes": fact_changes,
    }


def render_delta(chapter: int, title: str, delta: dict[str, Any], core_names: set[str]) -> str:
    lines = [
        f"# 第{chapter:03d}章 · {title}",
        f"- 结果：{delta['result']}",
        "- 下一章承诺：" + ("；".join(delta["next_chapter_commitments"]) or "无"),
        "",
        "## 角色变化",
    ]
    lines.extend(
        f"- {item['name']}｜{'核心' if item['name'] in core_names else '临时'}｜{item['change']}"
        for item in delta["character_changes"]
    )
    if not delta["character_changes"]:
        lines.append("- 无")
    lines.extend(["", "## 伏笔变化"])
    for item in delta["foreshadow_changes"]:
        if item["action"] == "delete":
            lines.append(f"- {item['id']}｜删除当前登记")
        else:
            planned = f"第{item['planned_resolution_chapter']}章" if item["planned_resolution_chapter"] else "未定"
            lines.append(f"- {item['id']}｜{item['status']}｜{item['summary']}｜回收{planned}")
    if not delta["foreshadow_changes"]:
        lines.append("- 无")
    lines.extend(["", "## 时间与揭示"])
    for item in delta["timeline_events"]:
        if item["action"] == "delete":
            lines.append(f"- {item['id']}｜删除当前登记")
        else:
            lines.append(
                f"- {item['id']}｜{item['story_time']}｜事实：{item['objective_fact']}｜"
                f"读者：{item['reader_knowledge']}｜{item['reveal_status']}"
            )
    if not delta["timeline_events"]:
        lines.append("- 无")
    lines.extend(["", "## 代价变化"])
    cost_change = delta.get("cost_change")
    if cost_change is not None:
        lines.append(f"- {cost_change['device']}｜{cost_change['count']}（只进不退，权威计数取历史 max）")
    else:
        lines.append("- 无")
    # V12.3：质量元数据（declared 模型声明 + observed 系统统计）
    quality_meta = delta.get("quality_meta")
    if quality_meta is not None:
        lines.extend(["", "## 质量元数据"])
        declared = quality_meta.get("declared", {})
        if declared.get("protagonist_initiative") is not None:
            lines.append(f"- 主角主动性（声明）：{'是' if declared['protagonist_initiative'] else '否'}")
        observed = quality_meta.get("observed", {})
        if observed:
            lines.append(f"- 主动行为关键词命中：{observed.get('initiative_keywords', 0)} 处")
            lines.append(f"- 有全局状态变化：{'是' if observed.get('has_state_change') else '否'}")
            lines.append(f"- 有关系变化：{'是' if observed.get('has_relationship_change') else '否'}")
    # V12.4：规则丢失事件（写入 archive，逐章记录留痕）
    rule_loss_event = delta.get("rule_loss_event")
    if rule_loss_event is not None:
        lines.extend(["", "## 规则丢失事件"])
        lines.append(f"- 规则ID：{rule_loss_event['rule_id']}")
        if rule_loss_event.get("rule_name"):
            lines.append(f"- 规则名：{rule_loss_event['rule_name']}")
        actual = rule_loss_event.get("actual", {})
        if actual.get("method"):
            lines.append(f"- 获取方式：{actual['method']}")
        if actual.get("cost"):
            lines.append(f"- 代价：{actual['cost']}")
        if actual.get("impact"):
            lines.append(f"- 世界影响：{actual['impact']}")
        if actual.get("emotion"):
            lines.append(f"- 情绪基调：{actual['emotion']}")
    lines.extend(["", "## 连贯性约束"])
    lines.extend(f"- {item}" for item in delta["constraints"])
    if not delta["constraints"]:
        lines.append("- 无")
    retired = delta.get("retired_context_items", []) + [
        f"角色状态：{name}" for name in delta.get("retired_characters", [])
    ]
    if retired:
        # 退役条目在此留档，续写状态卡收缩后仍可回查当初撤下了什么。
        lines.extend(["", "## 本章退役登记"])
        lines.extend(f"- {item}" for item in retired)
    # P0-1/2 事实来源契约：中间态留痕（仅在有候选/变更数据时渲染，存量书零变化）。
    # contract_state 展示 provenance_gate 派生的 effective 值（active/schema_only）。
    contract_state = delta.get("_contract_state_effective") or delta.get("contract_state") or "schema_only"
    candidate_facts = delta.get("candidate_facts") or []
    fact_changes = delta.get("fact_changes") or {}
    has_contract = bool(candidate_facts) or bool(
        fact_changes.get("confirmed") or fact_changes.get("hints") or fact_changes.get("retire")
    )
    if has_contract:
        lines.extend(["", "## 事实来源契约"])
        lines.append(f"- 契约状态：{contract_state or 'schema_only'}")
        lines.append(f"- 候选事实：{len(candidate_facts)} 条")
        lines.append(
            f"- 事实变更：confirmed {len(fact_changes.get('confirmed', []))} / "
            f"hints {len(fact_changes.get('hints', []))} / retire {len(fact_changes.get('retire', []))}"
        )
    payload = "\n".join(lines) + "\n"
    size = byte_size(payload)
    require(size <= DELTA_MAX_BYTES, f"chapter delta is {size} bytes; hard cap is {DELTA_MAX_BYTES}")
    return payload


def normalize_chapter_types(value: object, last_chapter: int) -> dict[str, str]:
    """章节类型登记表：{"3": "战斗", "4": "战斗", ...}（chapter_type streak 数据地基）。"""
    records = as_mapping(value, "tracking state.chapter_types")
    normalized: dict[str, str] = {}
    for raw_chapter, raw_type in records.items():
        require(
            isinstance(raw_chapter, str) and re.fullmatch(r"[1-9]\d*", raw_chapter) is not None,
            "chapter_types chapter key is invalid",
        )
        chapter = int(raw_chapter)
        require(chapter <= last_chapter, "chapter_types entry exceeds last committed chapter")
        normalized[raw_chapter] = clean_text(raw_type, f"chapter_types[{raw_chapter}]", max_bytes=24)
    return normalized


def normalize_wordcount_records(value: object, last_chapter: int) -> dict[str, dict[str, Any]]:
    records = as_mapping(value, "tracking state.wordcount_records")
    normalized: dict[str, dict[str, Any]] = {}
    for raw_chapter, raw_record in records.items():
        require(isinstance(raw_chapter, str) and re.fullmatch(r"[1-9]\d*", raw_chapter) is not None,
                "wordcount record chapter key is invalid")
        chapter = int(raw_chapter)
        require(chapter <= last_chapter, "wordcount record exceeds last committed chapter")
        normalized[raw_chapter] = wordcount_value(nww_wordcount.normalize_wordcount_record, raw_record)
    return normalized


def normalize_facts(value: object, label: str = "tracking state.facts") -> dict[str, Any]:
    """P0-1/2 世界事实层（Confirmed Facts + Writer Hints）：state 顶层全量校验。

    P0-1 仅 schema 接收与结构校验；P0-2 起 facts 写入由 provenance_gate 审批 + merge 落库，
    hints 段保留 schema（P0-3 消费）。
    """
    root = as_mapping(value, label)
    require_known_keys(root, {"confirmed", "hints"}, label)
    confirmed: list[dict[str, Any]] = []
    for index, raw in enumerate(as_list(root.get("confirmed", []), f"{label}.confirmed")):
        row = as_mapping(raw, f"{label}.confirmed[{index}]")
        require_known_keys(
            row,
            {"id", "fact", "fact_type", "tier", "source", "recorded_chapter"},
            f"{label}.confirmed[{index}]",
        )
        fid = clean_text(row.get("id"), f"{label}.confirmed[{index}].id", max_bytes=24)
        require(FACT_ID.fullmatch(fid) is not None, f"{label}.confirmed[{index}].id must look like T001")
        fact_type = clean_text(row.get("fact_type"), f"{label}.confirmed[{index}].fact_type", max_bytes=24)
        require(fact_type in FACT_TYPES, f"{label}.confirmed[{index}].fact_type must be one of {FACT_TYPES}")
        tier = clean_text(row.get("tier"), f"{label}.confirmed[{index}].tier", max_bytes=24)
        require(tier == "confirmed", f"{label}.confirmed[{index}].tier must be 'confirmed'")
        source = normalize_source(row.get("source"), f"{label}.confirmed[{index}].source")
        require(source is not None, f"{label}.confirmed[{index}].source is required")
        confirmed.append(
            {
                "id": fid,
                "fact": clean_text(row.get("fact"), f"{label}.confirmed[{index}].fact", max_bytes=240),
                "fact_type": fact_type,
                "tier": tier,
                "source": source,
                "recorded_chapter": (
                    None
                    if row.get("recorded_chapter") is None
                    else as_int(row.get("recorded_chapter"), f"{label}.confirmed[{index}].recorded_chapter", minimum=1)
                ),
            }
        )
    require(len({item["id"] for item in confirmed}) == len(confirmed), f"{label}.confirmed contains duplicate IDs")
    hints: list[dict[str, Any]] = []
    for index, raw in enumerate(as_list(root.get("hints", []), f"{label}.hints")):
        row = as_mapping(raw, f"{label}.hints[{index}]")
        require_known_keys(row, {"id", "hint", "visibility", "created_chapter"}, f"{label}.hints[{index}]")
        hid = clean_text(row.get("id"), f"{label}.hints[{index}].id", max_bytes=24)
        require(FACT_ID.fullmatch(hid) is not None, f"{label}.hints[{index}].id must look like T001")
        visibility = clean_text(row.get("visibility") or "writer_only", f"{label}.hints[{index}].visibility", max_bytes=24)
        require(visibility == "writer_only", f"{label}.hints[{index}].visibility must be 'writer_only'")
        hints.append(
            {
                "id": hid,
                "hint": clean_text(row.get("hint"), f"{label}.hints[{index}].hint", max_bytes=240),
                "visibility": visibility,
                "created_chapter": (
                    None
                    if row.get("created_chapter") is None
                    else as_int(row.get("created_chapter"), f"{label}.hints[{index}].created_chapter", minimum=1)
                ),
            }
        )
    require(len({item["id"] for item in hints}) == len(hints), f"{label}.hints contains duplicate IDs")
    return {"confirmed": confirmed, "hints": hints}


def _next_story_event_id(se_index: dict[str, Any]) -> str:
    highest = 0
    for sid in se_index:
        try:
            highest = max(highest, int(sid[len("SE-"):]))
        except ValueError:
            pass
    return f"SE-{highest + 1:03d}"


def normalize_story_events(value: object, label: str = "tracking state.story_events") -> dict[str, dict[str, Any]]:
    """P2：Story Event 注册表（SE 本体）。occurrences 只存 E-ID 引用，不复制 Timeline Record 内容。"""
    se = as_mapping(value, label)
    normalized: dict[str, dict[str, Any]] = {}
    for raw_sid, raw_row in se.items():
        sid = clean_text(raw_sid, f"{label}.id", max_bytes=16)
        require(STORY_EVENT_ID.fullmatch(sid) is not None, f"{label}.{sid} must look like SE-001")
        row = as_mapping(raw_row, f"{label}.{sid}")
        require_known_keys(
            row,
            {"identity_key", "subject", "action", "object", "state_change", "created_chapter", "occurrences"},
            f"{label}.{sid}",
        )
        occurrences = clean_string_list(
            row.get("occurrences", []), f"{label}.{sid}.occurrences", maximum=200, item_max_bytes=16
        )
        for oid in occurrences:
            require(EVENT_ID.fullmatch(oid) is not None, f"{label}.{sid}.occurrences must be E-IDs")
        require(len(set(occurrences)) == len(occurrences), f"{label}.{sid}.occurrences contains duplicates")
        normalized[sid] = {
            "identity_key": clean_text(row.get("identity_key"), f"{label}.{sid}.identity_key", max_bytes=480),
            "subject": clean_text(row.get("subject"), f"{label}.{sid}.subject", max_bytes=60),
            "action": clean_text(row.get("action"), f"{label}.{sid}.action", max_bytes=60),
            "object": clean_text(row.get("object"), f"{label}.{sid}.object", max_bytes=120, allow_empty=True) or None,
            "state_change": clean_text(row.get("state_change"), f"{label}.{sid}.state_change", max_bytes=160),
            "created_chapter": as_int(row.get("created_chapter"), f"{label}.{sid}.created_chapter", minimum=1),
            "occurrences": occurrences,
        }
    return normalized


def normalize_state(document: object) -> dict[str, Any]:
    root = as_mapping(document, "tracking state")
    require_known_keys(
        root,
        {
            "schema_version", "book_title", "last_committed_chapter", "imported_through_chapter",
            "state_revision", "context", "characters", "dormant_characters", "foreshadow", "timeline",
            "story_events", "wordcount_records", "cost", "global", "chapter_types", "facts",
        },
        "tracking state",
    )
    require(root.get("schema_version") == TRACKING_SCHEMA_VERSION, "tracking state schema is unsupported")
    last_chapter = as_int(root.get("last_committed_chapter"), "tracking state.last_committed_chapter")
    imported_through = as_int(root.get("imported_through_chapter"), "tracking state.imported_through_chapter")
    require(imported_through <= last_chapter, "imported chapter cutoff exceeds current chapter")
    context = validate_context_input(root.get("context"), include_initial_fields=True)
    require(
        context["position"]["volume_start_chapter"] <= max(1, last_chapter),
        "context.position.volume_start_chapter is after the current writing position",
    )
    recent_numbers = [item["chapter"] for item in context["recent_chapters"]]
    require(recent_numbers == sorted(recent_numbers), "context.recent_chapters must be ordered")
    require(len(recent_numbers) == len(set(recent_numbers)), "context.recent_chapters contains duplicates")
    require(all(chapter <= last_chapter for chapter in recent_numbers), "context.recent_chapters cannot include future chapters")
    characters = normalize_snapshots(root.get("characters", {}), "tracking state.characters")
    dormant = normalize_dormant_characters(root.get("dormant_characters", {}))
    for name in context["active_character_names"]:
        require(name in characters, f"active core character {name} has no current snapshot")
    foreshadow = normalize_foreshadow_state(root.get("foreshadow", {}), last_chapter)
    timeline = normalize_timeline_state(root.get("timeline", {}), last_chapter)
    story_events = normalize_story_events(root.get("story_events", {}))
    facts = normalize_facts(root.get("facts", {}))
    if last_chapter == 0:
        require(not foreshadow, "a chapter-0 project cannot have planted foreshadow facts")
        require(not timeline, "a chapter-0 project cannot have established timeline facts")
    state_revision = as_int(root.get("state_revision"), "tracking state.state_revision")
    wordcount_records = normalize_wordcount_records(root.get("wordcount_records", {}), last_chapter)
    return {
        "schema_version": TRACKING_SCHEMA_VERSION,
        "book_title": clean_text(root.get("book_title"), "tracking state.book_title", max_bytes=240),
        "last_committed_chapter": last_chapter,
        "imported_through_chapter": imported_through,
        "state_revision": state_revision,
        "context": context,
        "characters": characters,
        "dormant_characters": dormant,
        "foreshadow": foreshadow,
        "timeline": timeline,
        "story_events": story_events,
        "facts": facts,
        "wordcount_records": wordcount_records,
        "cost": normalize_cost(root.get("cost", {})),
        "global": normalize_global_state(root.get("global", {})),
        "chapter_types": normalize_chapter_types(root.get("chapter_types", {}), last_chapter),
    }


def story_event_confirm(
    project: Path, candidate: str, decision: str, target: str | None, by: str = "human"
) -> dict[str, Any]:
    """P2 人工确认入口（独立于 Role Lifecycle，两套生命周期不混用）。

    SAME_EVENT     → identity_match.status=EXACT_MATCH + story_event_id=目标 SE + decision=SAME_EVENT
    DIFFERENT_EVENT → 新建 SE + identity_match.status=NEW + decision=DIFFERENT_EVENT
    禁止 EXACT_MATCH + DIFFERENT_EVENT 矛盾态（normalize_identity_match 兜底）。

    F5（2026-10-19 外脑裁决）：确认是 state 读改写事务——全程持有与 commit 同一把
    project_write_lock（跨进程 O_EXCL 文件锁），从读取最新 state 之前覆盖到原子写完成，
    消灭「读 → 并发 commit → stale 写回」丢更新窗口（与 F1/R1 同一并发协议）。
    锁内任一步失败（require/normalize）异常穿出 → 锁释放、state 零变化
    （state 写是最后一步原子替换，V10 场景③「确认自身失败权威状态保持有效」）。
    """
    require(decision in {"SAME_EVENT", "DIFFERENT_EVENT"}, "decision must be SAME_EVENT or DIFFERENT_EVENT")
    require(EVENT_ID.fullmatch(candidate) is not None, f"candidate must look like E-ID, got {candidate}")
    with project_write_lock(project):
        state = load_state(project)
        require(candidate in state["timeline"], f"candidate {candidate} not in state.timeline")
        rec = state["timeline"][candidate]
        require(rec.get("event_identity") is not None, f"candidate {candidate} is UNRESOLVED; cannot confirm")
        key = rec["event_identity"]["identity_key"]
        se_index = state.setdefault("story_events", {})
        confirmed_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        if decision == "SAME_EVENT":
            require(target is not None, "SAME_EVENT requires --target SE-ID")
            require(STORY_EVENT_ID.fullmatch(target) is not None, f"target must look like SE-ID, got {target}")
            require(target in se_index, f"target {target} not in state.story_events")
            if candidate not in se_index[target]["occurrences"]:
                se_index[target]["occurrences"].append(candidate)
            rec["identity_match"] = {
                "status": "EXACT_MATCH", "story_event_id": target, "matched_identity_key": key,
                "confirmed_by": by, "confirmed_at": confirmed_at, "decision": "SAME_EVENT",
            }
        else:
            require(target is None, "DIFFERENT_EVENT does not take --target")
            new_sid = _next_story_event_id(se_index)
            se_index[new_sid] = {
                "identity_key": key,
                "subject": rec["event_identity"].get("subject"),
                "action": rec["event_identity"].get("action"),
                "object": rec["event_identity"].get("object"),
                "state_change": rec["event_identity"].get("state_change"),
                "created_chapter": rec.get("first_recorded_chapter", 1),
                "occurrences": [candidate],
            }
            rec["identity_match"] = {
                "status": "NEW", "story_event_id": new_sid, "matched_identity_key": key,
                "confirmed_by": by, "confirmed_at": confirmed_at, "decision": "DIFFERENT_EVENT",
            }
        state["timeline"][candidate] = rec
        state = normalize_state(state)  # 校验（含矛盾态兜底）后再写
        views = render_views(state)
        write_views(project / "追踪", views)
        atomic_write_text(state_path(project), json_payload(state))
    emit(f"OK: {candidate} → {rec['identity_match']['status']} / {decision} / {rec['identity_match']['story_event_id']}")
    return state


def load_state(project: Path) -> dict[str, Any]:
    path = state_path(project)
    require(path.exists(), "tracking state is missing; run init first")
    return normalize_state(read_json(path))


def normalize_initial_document(document: object) -> dict[str, Any]:
    root = as_mapping(document, "init input")
    require_known_keys(
        root,
        {
            "schema_version", "book_title", "last_chapter", "context", "character_snapshots",
            "foreshadow", "timeline_events", "cost", "global",
        },
        "init input",
    )
    require(root.get("schema_version") == INPUT_SCHEMA_VERSION, "init input schema_version is unsupported")
    last_chapter = as_int(root.get("last_chapter"), "last_chapter")
    context = validate_context_input(root.get("context"), include_initial_fields=True)
    snapshots = normalize_snapshots(root.get("character_snapshots", {}))
    foreshadow: dict[str, dict[str, Any]] = {}
    for index, raw_row in enumerate(as_list(root.get("foreshadow", []), "foreshadow")):
        row = normalize_foreshadow_change(
            raw_row, f"foreshadow[{index}]", allow_delete=False, through_chapter=last_chapter
        )
        require(row["id"] not in foreshadow, f"duplicate foreshadow ID {row['id']}")
        row.pop("action")
        row["updated_chapter"] = max(1, last_chapter)
        foreshadow[row["id"]] = row
    timeline: dict[str, dict[str, Any]] = {}
    for index, raw_event in enumerate(as_list(root.get("timeline_events", []), "timeline_events")):
        event = normalize_timeline_change(
            raw_event, f"timeline_events[{index}]", allow_delete=False, through_chapter=last_chapter
        )
        require(event["id"] not in timeline, f"duplicate timeline event ID {event['id']}")
        event.pop("action")
        event["first_recorded_chapter"] = max(1, last_chapter)
        event["updated_chapter"] = max(1, last_chapter)
        timeline[event["id"]] = event
    return normalize_state(
        {
            "schema_version": TRACKING_SCHEMA_VERSION,
            "book_title": clean_text(root.get("book_title"), "book_title", max_bytes=240),
            "last_committed_chapter": last_chapter,
            "imported_through_chapter": last_chapter,
            "state_revision": 0,
            "context": context,
            "characters": snapshots,
            "foreshadow": foreshadow,
            "timeline": timeline,
            "wordcount_records": {},
            "cost": normalize_cost(root.get("cost", {})),
            "global": normalize_global_state(root.get("global", {})),
            "chapter_types": {},
        }
    )


def normalize_transaction(project: Path, state: dict[str, Any], document: object) -> dict[str, Any]:
    root = as_mapping(document, "transaction")
    # `_` 前缀 = 注解命名空间（如 nww_delta_draft.py 的 _draft_hints）：
    # 随草稿同行不被拒绝，但不参与任何校验与合并；真实键的严格校验不受影响。
    root = {key: value for key, value in root.items() if not str(key).startswith("_")}
    require_known_keys(
        root,
        {
            "schema_version", "mode", "chapter", "chapter_title", "expected_state_revision",
            "delta", "context", "character_snapshots", "wordcount", "global_change",
        },
        "transaction",
    )
    require(root.get("schema_version") == INPUT_SCHEMA_VERSION, "transaction schema_version is unsupported")
    mode = clean_text(root.get("mode"), "mode", max_bytes=24)
    require(mode in {"append", "revision", "backfill"}, "mode must be append, revision, or backfill")
    chapter = as_int(root.get("chapter"), "chapter", minimum=1)
    expected_revision = as_int(root.get("expected_state_revision"), "expected_state_revision")
    wordcount_input = root.get("wordcount")
    require(expected_revision == state["state_revision"], "tracking state changed since this transaction was prepared")
    last = state["last_committed_chapter"]
    if mode == "append":
        require(chapter == last + 1, f"append chapter must be {last + 1}, got {chapter}")
    elif mode == "revision":
        require(chapter <= last, f"cannot revise unwritten chapter {chapter}; last committed chapter is {last}")
    elif mode == "backfill":
        # backfill：对已完成历史审计、外脑/人工裁决的事实做 canonical 回填。
        # 不受 append 的 chapter==last+1 连续章节限制；chapter=确认章（事实写入 canonical 的章）。
        require(chapter >= 1, "backfill confirmation chapter must be >= 1")
    context = validate_context_input(root.get("context"), include_initial_fields=False)
    snapshots = normalize_snapshots(root.get("character_snapshots", {}))
    existing_names = {portable_name_key(name): name for name in state["characters"]}
    for name in snapshots:
        existing = existing_names.get(portable_name_key(name))
        require(existing is None or existing == name, f"character {name} conflicts with existing character {existing}")
    through_chapter = chapter if mode == "append" else last
    delta = normalize_delta(
        root.get("delta"),
        through_chapter=through_chapter,
        snapshots=snapshots,
        existing_core_names=existing_names,
    )
    wordcount = None
    if wordcount_input is not None:
        wordcount = wordcount_value(
            nww_wordcount.validate_current_wordcount_record, project, chapter, wordcount_input
        )
    global_change = root.get("global_change")
    if global_change is not None:
        global_change = normalize_global_state(global_change, "transaction.global_change")
    return {
        "mode": mode,
        "chapter": chapter,
        "title": clean_text(root.get("chapter_title"), "chapter_title", max_bytes=240),
        "delta": delta,
        "context": context,
        "snapshots": snapshots,
        "wordcount": wordcount,
        "global_change": global_change,
    }


def checkpoint_record(
    change: dict[str, Any], chapter: int, previous: dict[str, Any] | None, *, keep_first_chapter: bool = False
) -> dict[str, Any]:
    current = {key: value for key, value in change.items() if key != "action"}
    current["updated_chapter"] = max(previous["updated_chapter"] if previous else chapter, chapter)
    if keep_first_chapter:
        current["first_recorded_chapter"] = previous["first_recorded_chapter"] if previous else chapter
    return current


def merge_transaction(state: dict[str, Any], transaction: dict[str, Any]) -> dict[str, Any]:
    next_state = copy.deepcopy(state)
    chapter = transaction["chapter"]
    if transaction["mode"] == "append":
        next_state["last_committed_chapter"] = chapter
    next_state["state_revision"] += 1

    # V12.3：自动填充 quality_meta.observed（系统统计层，不依赖模型自报）
    quality_meta = transaction["delta"].get("quality_meta")
    if quality_meta is not None:
        char_changes = transaction["delta"].get("character_changes", [])
        initiative_kw = ("主动", "决定", "选择", "发现", "尝试", "调查", "寻找")
        initiative_count = sum(
            1 for c in char_changes if any(kw in c.get("change", "") for kw in initiative_kw)
        )
        has_relationship = any("关系" in c.get("change", "") for c in char_changes)
        quality_meta["observed"] = {
            "initiative_keywords": initiative_count,
            "has_state_change": transaction.get("global_change") is not None,
            "has_relationship_change": has_relationship,
        }

    # 全局状态增量合并：模型只返回本章变化的 global 字段，与已有状态递归合并。
    # 未提及的字段保持不变；global_change 为 None 表示本章无全局状态变化。
    if transaction.get("global_change") is not None:
        next_state["global"] = deep_merge(next_state.get("global", {}), transaction["global_change"])
    next_state["characters"].update(transaction["snapshots"])
    if transaction["wordcount"] is not None:
        next_state["wordcount_records"][str(chapter)] = transaction["wordcount"]
    # 章节类型登记：append/revision 都可更新（revision 修正类型也算数），省略则保留旧值
    if transaction["delta"].get("chapter_type"):
        next_state.setdefault("chapter_types", {})[str(chapter)] = transaction["delta"]["chapter_type"]
    # 代价计数只进不退：以历史 max 为权威，绝不让回退值覆盖已发生的代价
    if transaction["delta"]["cost_change"] is not None:
        change = transaction["delta"]["cost_change"]
        next_state["cost"][change["device"]] = max(
            next_state["cost"].get(change["device"], 0), change["count"]
        )

    for name in transaction["delta"]["revived_characters"]:
        dormant_pool = next_state.setdefault("dormant_characters", {})
        require(name in dormant_pool, f"revived character {name} is not in the dormant pool")
        require(
            name in transaction["snapshots"],
            f"revived character {name} must provide an updated snapshot in character_snapshots",
        )
        require(name not in state["characters"], f"revived character {name} is already active (not retired)")
        dormant_entry = dormant_pool.pop(name)
        emit(
            f"NOTE: 休眠角色 {name} 于第{dormant_entry['retired_chapter']}章退役，本章复活"
            f"（退役原因：{dormant_entry['retirement_reason'] or '未记录'}）"
        )

    next_context = transaction["context"]
    # 退役说的是「从此刻起离开当前状态」，只有 append 的逐章记录代表此刻；
    # 修订记录属于被改写的旧章，落在那里会谎报退役发生的章节。
    is_revision = transaction["mode"] == "revision"
    require(
        not (is_revision and transaction["delta"]["retired_characters"]),
        "retired_characters must be committed in an append transaction, not a revision",
    )
    for name in transaction["delta"]["retired_characters"]:
        require(name in next_state["characters"], f"retired character {name} has no current snapshot")
        require(
            name not in transaction["snapshots"],
            f"character {name} cannot be retired and updated in the same transaction",
        )
        require(
            name not in next_context["active_character_names"],
            f"retired character {name} is still listed in context.active_character_names",
        )
        snapshot = next_state["characters"].pop(name)
        # 退役不删除：移入休眠池（保留完整快照 + 退役元数据），第500章复活有据可查
        next_state.setdefault("dormant_characters", {})[name] = {
            "snapshot": snapshot,
            "retired_chapter": chapter,
            "retirement_reason": transaction["delta"]["retirement_reasons"].get(name, ""),
        }

    # 上下文条目是整份提交的；漏写会静默丢历史裁定，因此掉落必须显式声明。
    previous_items = set(state["context"]["long_term_constraints"]) | set(state["context"]["continuity_risks"])
    dropped = previous_items - (set(next_context["long_term_constraints"]) | set(next_context["continuity_risks"]))
    require(
        not (is_revision and dropped),
        "a revision must resubmit every current context item; retire them in an append transaction instead: "
        + "；".join(sorted(dropped)),
    )
    undeclared = sorted(dropped - set(transaction["delta"]["retired_context_items"]))
    require(
        not undeclared,
        "context items were dropped without being declared in delta.retired_context_items: "
        + "；".join(undeclared),
    )
    transaction["delta"]["retired_context_items"] = sorted(dropped)

    for change in transaction["delta"]["foreshadow_changes"]:
        if change["action"] == "delete":
            next_state["foreshadow"].pop(change["id"], None)
        else:
            next_state["foreshadow"][change["id"]] = checkpoint_record(
                change, chapter, next_state["foreshadow"].get(change["id"])
            )
    # K5: 伏笔半衰期自动处置——超期的已埋伏笔自动升级为"需处理"
    for fid, row in list(next_state["foreshadow"].items()):
        if row.get("status") != "已埋":
            continue
        half_life = row.get("half_life_chapters")
        if not half_life:
            continue
        stale = chapter - row.get("updated_chapter", row.get("planted_chapter", 0))
        if stale >= half_life:
            row["status"] = "需处理"
    # —— P2 Event Identity 关系判定（2026-09-23 外脑裁决）——
    # 同一 next_state 内维护：timeline 记录 + story_events 注册表（事务原子，无旁路状态源）。
    # NEW = "当前确定性 identity_key 索引中无完全相同 SE"（不是证明全新事件）；
    # EXACT_MATCH = identity_key 完全一致 → occurrence 幂等追加（阻止创建第二个 SE，不阻止 Timeline Record）；
    # UNRESOLVED = 无 event_identity，不参与自动匹配；POSSIBLE_MATCH 为人工治理入口，v1 不自动产生。
    # 人工已确认（identity_match 含 decision）的记录不被自动覆盖，保留 provenance。
    se_index = next_state.setdefault("story_events", {})
    for change in transaction["delta"]["timeline_events"]:
        if change["action"] == "delete":
            next_state["timeline"].pop(change["id"], None)
        else:
            rec = checkpoint_record(
                change, chapter, next_state["timeline"].get(change["id"]), keep_first_chapter=True
            )
            identity = rec.get("event_identity")
            existing_match = rec.get("identity_match")
            if identity is not None and rec.get("identity_status") == "RESOLVED" and not (
                existing_match is not None and existing_match.get("decision") is not None
            ):
                key = identity["identity_key"]
                found = next((sid for sid, se in se_index.items() if se.get("identity_key") == key), None)
                if found is not None:
                    if rec["id"] not in se_index[found]["occurrences"]:
                        se_index[found]["occurrences"].append(rec["id"])
                    rec["identity_match"] = {
                        "status": "EXACT_MATCH", "story_event_id": found, "matched_identity_key": key,
                    }
                else:
                    new_sid = _next_story_event_id(se_index)
                    se_index[new_sid] = {
                        "identity_key": key,
                        "subject": identity.get("subject"),
                        "action": identity.get("action"),
                        "object": identity.get("object"),
                        "state_change": identity.get("state_change"),
                        "created_chapter": chapter,
                        "occurrences": [rec["id"]],
                    }
                    rec["identity_match"] = {
                        "status": "NEW", "story_event_id": new_sid, "matched_identity_key": key,
                    }
            elif rec.get("identity_match") is None:
                rec["identity_match"] = {
                    "status": "UNRESOLVED", "story_event_id": None, "matched_identity_key": None,
                }
            next_state["timeline"][rec["id"]] = rec

    recent_by_chapter = {item["chapter"]: item for item in state["context"]["recent_chapters"]}
    if chapter in recent_by_chapter or transaction["mode"] == "append":
        recent_by_chapter[chapter] = {"chapter": chapter, "summary": _truncate_bytes(transaction["delta"]["result"], 360)}
    all_recent = sorted(recent_by_chapter.values(), key=lambda item: item["chapter"])
    recent = all_recent[-3:]
    # K4: 被截断的章节摘要折叠进 archived_summaries（上限 50 条，防无限增长）
    dropped = [item for item in all_recent[:-3] if item["chapter"] < (all_recent[-3]["chapter"] if len(all_recent) > 3 else 0)]
    if dropped:
        existing_archived = state["context"].get("archived_summaries", [])
        if not isinstance(existing_archived, list):
            existing_archived = []
        archived_chapters = {a["chapter"] for a in existing_archived}
        for item in dropped:
            if item["chapter"] not in archived_chapters:
                existing_archived.append(item)
        existing_archived.sort(key=lambda x: x["chapter"])
        existing_archived = existing_archived[-50:]  # 上限 50 条
    else:
        existing_archived = state["context"].get("archived_summaries", [])
    current_last = next_state["last_committed_chapter"]
    next_commitments = (
        transaction["delta"]["next_chapter_commitments"]
        if transaction["mode"] == "append" or chapter == current_last
        else state["context"]["next_chapter_commitments"]
    )
    next_state["context"] = {
        **next_context,
        "recent_chapters": recent,
        "archived_summaries": existing_archived,
        "next_chapter_commitments": next_commitments,
    }

    # P0-2 事实契约落库：只消费 provenance_gate 审批后的 fact_changes（confirmed add / retire）。
    # 绝不读取 candidate_facts（merge 不自动升级 candidate）；本段只做机械落库，
    # 业务不变式由 gate 判定；结构性错误（如 confirmed 重复 id）仍会被末尾 normalize_state
    # 拦截，事务不落盘。
    fact_changes = transaction["delta"].get("fact_changes") or {}
    confirmed_rows = fact_changes.get("confirmed") or []
    retire_ids = fact_changes.get("retire") or []
    if confirmed_rows or retire_ids:
        facts = next_state.setdefault("facts", {})
        confirmed = facts.setdefault("confirmed", [])
        for row in confirmed_rows:
            confirmed.append(
                {
                    "id": row["id"],
                    "fact": row["fact"],
                    "fact_type": row["fact_type"],
                    "tier": "confirmed",
                    "source": row["source"],
                    "recorded_chapter": chapter,
                }
            )
        if retire_ids:
            retire_set = set(retire_ids)
            confirmed[:] = [f for f in confirmed if f["id"] not in retire_set]

    return normalize_state(next_state)


def render_story_events(story_events: dict[str, dict[str, Any]], revision: int) -> str:
    """事件注册表.md：SE 本体索引（occurrence 只列 E-ID 引用，不复制 Timeline Record 内容）。"""
    lines = [
        "# 故事事件注册表",
        "",
        f"> 状态修订：{revision}。Story Event 本体；occurrence 引用 timeline E-ID，详情见作者真相时间线。",
        "",
        "| SE | 首次章 | 事件身份 | occurrence |",
        "|---|---|---|--:|",
    ]
    for sid in sorted(story_events):
        row = story_events[sid]
        lines.append(
            f"| {sid} | 第{row['created_chapter']}章 | {row['identity_key']} | "
            f"{', '.join(row['occurrences'])} |"
        )
    return "\n".join(lines).rstrip() + "\n"


def render_dormant(dormant: dict[str, dict[str, Any]], revision: int) -> str:
    """休眠角色.md：一行摘要（名字/身份/退役章/退役原因），不渲染完整快照控制大小。

    完整快照仍在 _tracking-state.json 的 dormant_characters 段，复活时原样取回。
    """
    lines = [
        f"# 休眠角色池",
        "",
        f"> 状态修订：{revision}。退役不删除；复活时由 delta.revived_characters + 全新快照触发。",
        "",
        "| 角色 | 身份 | 退役章 | 退役原因 |",
        "| --- | --- | --- | --- |",
    ]
    for name in sorted(dormant):
        entry = dormant[name]
        lines.append(
            f"| {name} | {entry['snapshot']['identity']} | 第{entry['retired_chapter']}章 "
            f"| {entry['retirement_reason'] or '未记录'} |"
        )
    return "\n".join(lines).rstrip() + "\n"


def render_views(state: dict[str, Any]) -> dict[str, str]:
    revision = state["state_revision"]
    views = {
        "上下文.md": render_context(state),
        "伏笔.md": render_foreshadow(state["foreshadow"], revision),
        "事件注册表.md": render_story_events(state.get("story_events", {}), revision),
        "全局状态.md": render_global_view(state),
    }
    # 休眠角色池：仅在非空时渲染（存量书无该视图，check 兼容不破坏）
    if state.get("dormant_characters"):
        views["休眠角色.md"] = render_dormant(state["dormant_characters"], revision)
    author, reader = render_timeline_views(state["timeline"], revision)
    views["时间线/作者真相.md"] = author
    views["时间线/读者已知.md"] = reader
    for name, snapshot in state["characters"].items():
        views[f"角色状态/{name}.md"] = render_snapshot(
            name, snapshot, state["last_committed_chapter"], revision
        )
    return views


def write_views(tracking: Path, views: dict[str, str]) -> None:
    # 上下文携带 next revision，先写它；任何后续失败都会让 hook/check 发现
    # 上下文 revision 与最后提交的 _tracking-state.json 不一致。
    write_if_changed(tracking / "上下文.md", views["上下文.md"])
    for relative in sorted(path for path in views if path != "上下文.md"):
        write_if_changed(tracking / relative, views[relative])
    expected_character_files = {
        Path(relative).name for relative in views if relative.startswith("角色状态/")
    }
    character_dir = tracking / "角色状态"
    character_dir.mkdir(parents=True, exist_ok=True)
    for path in character_dir.glob("*.md"):
        if path.name not in expected_character_files:
            path.unlink()
    if "休眠角色.md" not in views:
        stale_dormant = tracking / "休眠角色.md"
        if stale_dormant.exists():
            stale_dormant.unlink()
    # P2 修复（2026-10-20 外脑裁决 Q3）：任何成功的完整视图重建都使失效标记过期——
    # 视图已与 state 一致。正式 commit 路径从未写过标记，此处清除为 no-op，行为不变。
    # 清除失败 = 维护未完全收口 → 显式报错（外脑 TestH：不得静默）。
    # TrackingError 非 OSError：r1_track_commit 的 except OSError 不会误捕，正确穿出。
    stale_marker = views_stale_marker(tracking)
    if stale_marker.is_file():
        try:
            stale_marker.unlink()
        except OSError as exc:
            raise TrackingError(
                f"派生视图已全部重建成功（与当前权威 state 一致），但失效标记清除失败（{exc}）"
                f"——维护未完全收口；重跑同一事务或下一次正式 commit 后自愈"
            ) from exc


def views_stale_marker(tracking: Path) -> Path:
    """派生视图失效标记路径：视图重建失败时写入，check 显式报「视图失效」防伪装一致。"""
    return tracking / ".views-stale.json"


def r1_track_commit(project: Path, chapter: int) -> dict[str, Any]:
    """R1 轻量 revision 事务的权威实现（P2 修复：R1 bump 后同步重建派生视图）。

    背景：R1 不改剧情/角色/事实语义字段，仅 bump state_revision；此前 bump 后不重建
    派生视图，而 render_views 的全部视图都携带 revision 戳 → 下一次 CLI check 必然
    「derived view differs」失配（2026-10-20 源码审计实锤，外脑 P2 裁决方案 A）。

    时序（锁内，单一来源；调用方 nwwmvp._r1_track_commit 只在锁外写 patch ledger）。
    外脑协议（2026-10-20 brain3 裁决）：失效标记先于权威 state 落盘——标记创建失败
    不得继续提交新 state；state 已写入而视图未完成时，持久失效证据必须已存在。
      ① load_state → rev+1（内存）
      ② render_views(new_state) 纯内存计算——失败异常穿出，零落盘，零半完成态
      ③ 写 追踪/.views-stale.json 失效标记（视图更新意图持久化）——失败 → TrackingError，
         state 停留旧 rev 零变更（TestG 语义：不得继续写新 state 并返回成功）
      ④ atomic_write_text(state)
      ⑤ write_views（成功即清失效标记；失败 → 标记已在 → check 显式报「视图失效」不伪装
         一致；重跑同一 R1 或下一次正式 commit 成功后自动恢复）
      ⑥ 同步第 chapter 章快照的 state_revision（原 R1 行为保留，同锁内）
    并发：标记写/清全程在 project_write_lock 内，与 commit/其他 R1 串行化——旧版本
    重建不可能清掉新版本产生的标记（外脑 Q3 revision 过期规则的锁内等效实现）。
    """
    require(chapter >= 1, "R1 requires a positive chapter id")
    with project_write_lock(project):
        tracking = tracking_root(project)
        state = load_state(project)
        old_rev = state["state_revision"]
        new_rev = old_rev + 1
        state["state_revision"] = new_rev
        views = render_views(state)
        marker = {
            "stale_since_revision": new_rev,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "hint": "R1 视图重建中或未收口；check 显式报失效防伪装一致。重跑同一 R1 或下一次正式 commit 成功后自动清除",
        }
        try:
            atomic_write_text(views_stale_marker(tracking), json.dumps(marker, ensure_ascii=False, indent=2))
        except OSError as exc:
            raise TrackingError(
                f"R1 失效标记无法持久化（{exc}）——拒绝继续提交新 state；"
                f"权威状态停留在 rev={old_rev}，本次事务零变更"
            ) from exc
        atomic_write_text(state_path(project), json.dumps(state, ensure_ascii=False, indent=2))
        try:
            write_views(tracking, views)
        except OSError as exc:
            raise TrackingError(
                f"R1 state 已推进到 rev={new_rev} 但派生视图重建失败: {exc}；"
                f"失效标记已先于 state 落盘，check 将显式报「视图失效」而非静默失配；"
                f"重跑同一 R1 修复或下一次正式 commit 成功后自动恢复"
            ) from exc
        sn_path = tracking / "快照" / f"第{chapter:03d}章.json"
        if sn_path.is_file():
            sn = json.loads(sn_path.read_text(encoding="utf-8"))
            sn["state_revision"] = new_rev
            atomic_write_text(sn_path, json.dumps(sn, ensure_ascii=False, indent=2))
        return {"old_rev": old_rev, "new_rev": new_rev}


def warn_sizes(
    views: dict[str, str],
    delta_payload: str | None = None,
    state: dict[str, Any] | None = None,
) -> None:
    if delta_payload is not None and byte_size(delta_payload) > DELTA_TARGET_BYTES:
        emit(
            f"WARNING: chapter delta is {byte_size(delta_payload)} bytes; target is <= {DELTA_TARGET_BYTES}",
            error=True,
        )
    context_size = byte_size(views["上下文.md"])
    if context_size > CONTEXT_TARGET_BYTES:
        emit(f"WARNING: hot context is {context_size} bytes; target is <= {CONTEXT_TARGET_BYTES}", error=True)
    for relative, payload in views.items():
        if not relative.startswith("角色状态/"):
            continue
        size = byte_size(payload)
        if size > SNAPSHOT_TARGET_BYTES:
            emit(
                f"WARNING: character snapshot {Path(relative).stem} is {size} bytes; target is <= {SNAPSHOT_TARGET_BYTES}",
                error=True,
            )
    # P0-1 事实来源契约：beliefs（角色认知层）预算检查，超限仅 WARNING 不 BLOCK。
    # 角色认知是动态数据，不应阻断正文生产；仅提示膨胀。
    if state is not None:
        for name, snapshot in state.get("characters", {}).items():
            beliefs = snapshot.get("beliefs") or []
            if len(beliefs) > BELIEFS_MAX_ITEMS:
                emit(
                    f"WARNING: character {name} has {len(beliefs)} beliefs; target is <= {BELIEFS_MAX_ITEMS}",
                    error=True,
                )
            total = sum(byte_size(json.dumps(b, ensure_ascii=False)) for b in beliefs)
            if total > BELIEFS_TOTAL_BUDGET:
                emit(
                    f"WARNING: character {name} beliefs total {total} bytes; target is <= {BELIEFS_TOTAL_BUDGET}",
                    error=True,
                )


def _initialize_locked(project: Path, document: object) -> dict[str, Any]:
    tracking = tracking_root(project)
    require(not state_path(project).exists(), "tracking state already exists; init never overwrites project state")
    state = normalize_initial_document(document)
    views = render_views(state)
    state_payload = json_payload(state)

    # 输入全部校验通过后才动用户文件，失败的 init 不会挪走任何东西。
    archived = archive_retired_tracking_paths(tracking)
    for directory in (tracking / "逐章记录", tracking / "角色状态", tracking / "时间线"):
        directory.mkdir(parents=True, exist_ok=True)
    write_views(tracking, views)
    atomic_write_text(state_path(project), state_payload)
    warn_sizes(views)
    if archived:
        emit(
            f"NOTE: 旧追踪结构已原样移入 追踪/{RETIRED_ARCHIVE_DIR}/：{', '.join(archived)}；"
            "当前状态以本次 init 输入为准，旧文件不参与解析。",
            error=True,
        )
    return state


def initialize(project: Path, document: object) -> dict[str, Any]:
    with project_write_lock(project):
        return _initialize_locked(project, document)


def _apply_transaction_locked(
    project: Path, document: object, quality_dir: str | None = None
) -> dict[str, Any]:
    tracking = tracking_root(project)
    require_no_retired_tracking_paths(tracking)
    state = load_state(project)
    transaction = normalize_transaction(project, state, document)
    # ------------------------------------------------------------------
    # V13 道闸策略层（2026-11-13）：以下 12 道「质量/语义/流程闭环」闸的判据、
    # 参数与调用顺序完全不变，只把「阻断」交给 nww_gate_policy 决策：
    #   v13（生产默认）→ 记录事实卡 + 一行诊断，事务继续落盘，裁决权交作者；
    #   strict（env NWW_GATE_MODE=strict）→ 原样抛 TrackingError，回到 V12 现状。
    # 产物有效性（normalize / merge / 写盘 / 结构校验）不在包裹范围内。
    # ------------------------------------------------------------------
    _ch = int(transaction["chapter"])
    # P0-2 事实来源审查闸：normalize 保证结构合法 → gate 判断业务合法 → merge 只负责落库。
    _gate.guard(project, _ch, "A-P0-2/fact_provenance",
                verify_fact_provenance, project, transaction, state)
    # A-7 timeline id 冲突闸（2026-09-16 外脑裁定）：append 模式下 delta timeline_events 的 id 集合
    # 与 state.timeline 已有 id 集合有交集 → BLOCK。根因：writer 每章从 E001 起编，
    # merge L1931 keep_first_chapter=True 只保护 first_recorded_chapter 字段不保护内容字段，
    # 导致每章 commit 覆盖上一章同 id 事件的 objective_fact/reader_knowledge。
    # revision/backfill 模式放行（重写同章应覆盖自己、backfill 本就是修正历史）。
    _gate.guard(project, _ch, "A-7/timeline_id_conflict",
                verify_timeline_id_conflict, state, transaction)
    next_state = merge_transaction(state, transaction)
    # A-6 Settlement 互锚闸（2026-09-15）：正文 ↔ Settlement 反向互锚，防「delta 声称无正文依据」
    # 的空想写入。位置对齐外脑裁决流程图：normalize→provenance→merge_transaction（内存态）→A-6→A-1...
    # 检查的是 previous_state + current_delta + chapter_final_body（transaction 即当前 delta，正文即 chapter），
    # 不拿合并后的 next_state 反推。bible.commit_gates.settlement_anchor=true → 有 block 即 raise；
    # 缺失/false → WARNING。override（带 reason）仅解除本闸。
    _gate.guard(project, _ch, "A-6/settlement_anchor",
                _run_settlement_anchor_guard, project, transaction)
    # Task 3 / C-P0-1：内容存在性硬闸（global / 主角境界 / scale 关键键）；block → TrackingError
    # bible.scale 驱动 position.engine / world 阈值；缺 bible.json → {}（跳过 scale 键闸）
    _cg = _gate.guard(project, _ch, "ContentGate/presence",
                      check_global_presence, next_state, _ch,
                      bible=_load_bible(project))
    if _cg is not None and _cg.level == "block":
        _gate.gate_block(project, _ch, "ContentGate/presence",
                         "[ContentGate BLOCK] " + "; ".join(_cg.reasons or ["content presence failed"]),
                         TrackingError)
    if _cg is not None and _cg.level == "warning":
        emit(
            "[ContentGate WARNING] " + "; ".join(_cg.reasons or ["content presence warning"]),
            error=True,
        )
    # A-1 硬闸（append 章）：正文代价计数必须 == merge 后追踪权威，不一致即状态污染，
    # 拒绝本次 commit（raise → exit 2），不静默论断任何一侧。
    _gate.guard(project, _ch, "A-1/meter_consistency",
                verify_meter_consistency, project, transaction["chapter"],
                next_state["cost"], transaction["mode"])
    # A-2 硬质量门（append 章）：I-G/I-H fail → 拒绝 commit；warning 记录不阻断。
    _gate.guard(project, _ch, "A-2/quality",
                run_phase45_gate, project, transaction["chapter"], quality_dir, transaction["mode"])
    # A-3 一致性闭环闸：bible.commit_gates.consistency 开启时缺记录/unclosed → 拒绝 commit；
    # 未开启（存量兼容）→ 仅 WARNING 不阻断。对 append 与 revision 一并生效。
    _gate.guard(project, _ch, "A-3/consistency_closure",
                verify_consistency_closure, project, transaction["chapter"])
    # A-4 压缩质量闸（2026-09-05）：正文实际事件 vs delta 声称事件交叉审计。
    # 漏写关键状态变化 = 压缩失真 = 不可逆状态污染。提取纯确定性（nww_delta_audit.py），
    # BLOCK 仅限「被追踪角色 × 强变化关键词同句」；门控与 A-3 同一兼容模式语义。
    _gate.guard(project, _ch, "A-4/delta_audit",
                verify_delta_consistency, project, transaction, next_state)
    # A-5 流程完整性闸（2026-09-06）：校验 delta.quality_meta.enforced / reviewed，
    # 规避宿主 LLM 选择性执行 post_write 封装的确定性步骤。门控与 A-3/A-4 同语义。
    _gate.guard(project, _ch, "A-5/post_write_meta",
                verify_post_write_meta, project, transaction)
    # A-5b 结构指纹登记（2026-09-09）：确定性自动登记，不再依赖宿主 LLM 记得调用 register。
    _gate.guard(project, _ch, "A-5b/trope_register",
                register_trope_fingerprint, project, transaction["chapter"], transaction["mode"])
    # A-8 跨章复读硬闸（2026-11-05）：commit 前检测跨章重复/段落漂移/标题疲劳
    _gate.guard(project, _ch, "A-8/cross_chapter_repeats",
                verify_cross_chapter_repeats, project, _ch, transaction["mode"])
    # A-9 伏笔逾期硬闸（2026-11-05）：core_hook 伏笔超 half_life 未回收 → 阻断
    _gate.guard(project, _ch, "A-9/foreshadow_overdue",
                verify_foreshadow_overdue, state, _ch, transaction["mode"], project)

    delta_payload = render_delta(
        transaction["chapter"],
        transaction["title"],
        transaction["delta"],
        # 本章退役的角色在 next_state 里已被删除，但本章记录里仍应标为核心。
        set(next_state["characters"]) | set(transaction["delta"]["retired_characters"]),
    )
    views = render_views(next_state)
    next_state_payload = json_payload(next_state)
    path = delta_path(tracking, transaction["chapter"])
    if transaction["mode"] == "append" and path.exists():
        require(
            path.read_text(encoding="utf-8") == delta_payload,
            f"chapter delta {transaction['chapter']} already exists with different content",
        )

    write_if_changed(path, delta_payload)
    write_views(tracking, views)

    # V12.4：写入 archive/rule_history.json（规则丢失历史档案，独立于 global）
    rule_loss_event = transaction["delta"].get("rule_loss_event")
    if rule_loss_event is not None:
        archive_dir = tracking / "档案"
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = archive_dir / "rule_history.json"
        try:
            existing = json.loads(archive_path.read_text(encoding="utf-8")) if archive_path.exists() else {}
        except (OSError, UnicodeError, json.JSONDecodeError):
            existing = {}
        existing[rule_loss_event["rule_id"]] = {
            "rule_name": rule_loss_event["rule_name"],
            "lost_chapter": transaction["chapter"],
            "actual": rule_loss_event["actual"],
        }
        atomic_write_text(archive_path, json.dumps(existing, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    # 唯一权威文件最后落盘；在此之前失败可用同一事务直接重跑。
    # P0-2 合同 gap 修复：落盘前做最终状态校验。若 next_state 含任何非法形态
    # （如 recent_chapters.summary > 360B），normalize_state 在此抛错拒绝落盘，
    # 保证「任何成功 commit 的 state 都能被下一次 load_state 重读」的 invariant。
    final_state = normalize_state(next_state)
    final_payload = json_payload(final_state)
    atomic_write_text(state_path(project), final_payload)
    warn_sizes(views, delta_payload, next_state)
    # 每章快照（append）：影子 git 提交 追踪/ 全量；fail-open，绝不反噬提交。
    snapshot_book_state(project, transaction["chapter"], next_state["state_revision"], transaction["mode"])
    # K3: foundation snapshot — 完整状态快照到 追踪/快照/，支持任意章节回溯
    _foundation_snapshot(project, transaction["chapter"], next_state_payload)
    return next_state


def apply_transaction(
    project: Path, document: object, quality_dir: str | None = None
) -> dict[str, Any]:
    with project_write_lock(project):
        return _apply_transaction_locked(project, document, quality_dir)


# ---------- A-1 正文计数 vs 追踪权威一致性（2026-08-28，commit 硬闸） ----------
# 正文代价计数（如「裂纹：N」）与 _tracking-state.json 的 cost 段是同一状态的两份投影，
# 必须事务一致：模型漏写/误写 delta.cost_change 会让两者脱钩且全程无人报错。
# 本闸在 commit 写盘前做最后一道硬校验，不一致即拒绝（raise TrackingError → exit 2），
# 不静默拉平任何一侧（tracking 侧 merge max 不可逆是设计；正文侧不偷偷改）。
_NON_PROSE_KW = ("_设定", "_笔记", "_大纲", "_细纲", "_草稿", "_废弃", "_储备")
# 辅助目录不是正文：_review_snapshots/ 存的是 review cycle 的 round 快照。
# 必须按「路径部件」判断 —— 快照文件名形如「第027章_round1.md」，
# 文件名里没有任何可识别标记（_NON_PROSE_KW 全是文件名关键词，拦不住它），
# 只有目录层能区分。2026-09-13 实测：resolve_chapter_file 在 candidates 里
# 按 mtime 取最新，快照 mtime 比正文新时被当成正文选中 →
# ① backup_prose 以快照目录为基准再套一层，产出 _review_snapshots/_review_snapshots/；
# ② finalize 的路径基准随之错位。
_NON_PROSE_DIRS = ("_review_snapshots", "_archive")


def _is_non_prose(p: Path) -> bool:
    """路径是否属于「非正文」：命中辅助目录，或文件名带备份/草稿关键词。"""
    return (
        any(d in p.parts for d in _NON_PROSE_DIRS)
        or "_原稿_" in p.name
        or any(kw in p.name for kw in _NON_PROSE_KW)
    )


def resolve_chapter_file(project: Path, chapter: int) -> Path | None:
    """章号 → 唯一正文文件路径（『正文/』树；排除 _原稿_ 备份与非正文件）。

    结构上独立成函数：当前为 glob 定位（兼容平铺/卷目录/0 填充章名），
    未来替换为索引/元数据 resolver 时只动本函数，不改变调用方与门禁语义。
    定位不到正文文件（revision 改名/备份、正文尚未落盘）返回 None —— 无法校验则不阻断。
    """
    body = project / "正文"
    if not body.is_dir():
        return None
    candidates = []
    for p in body.rglob("第*章*.md"):
        if _is_non_prose(p):
            continue
        m = re.match(r"第0*(\d+)章", p.name)
        if m and int(m.group(1)) == chapter:
            candidates.append(p)
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime_ns)
    return candidates[-1]


def extract_prose_meter(path: Path, devices: list[str]) -> dict[str, int | None]:
    """从正文提取各代价 device 的最后一次计数值；正文无该行返回 None。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return {}
    found: dict[str, int | None] = {}
    for dev in devices:
        rx = re.compile(re.escape(dev) + r"[：:]?\s*(\d+)")
        hits = [int(m.group(1)) for m in rx.finditer(text)]
        found[dev] = hits[-1] if hits else None
    return found


def verify_meter_consistency(
    project: Path, chapter: int, authoritative: dict[str, int], mode: str
) -> None:
    """A-1 硬闸（append 章）：正文计数值 != merge 后追踪权威 → 拒绝 commit。

    仅对 append（首建新章）严格相等校验：该章结尾的代价计数 == tracking 提交后权威。
    revision（老章回炉）跳过：老章正文计数表达该章时间点的代价，与全书最新权威
    （merge max 单调不减）本就可低，严格相等必假阳性 —— 与 crack_meter_correct 的
    章位感知护栏（allow_lift 仅新章抬升）同一语义。
    正文无 meter 行 / 无权威 device / 文件不可得 → 静默跳过（不强求每章带计数）。
    """
    if mode != "append" or not authoritative:
        return
    path = resolve_chapter_file(project, chapter)
    if path is None:
        return
    meters = extract_prose_meter(path, list(authoritative))
    issues = []
    for dev, auth in authoritative.items():
        val = meters.get(dev)
        if val is not None and val != auth:
            issues.append(f"正文 {dev}={val}（{path.name}）≠ 追踪权威={auth}")
    if issues:
        raise TrackingError(
            "[A-1 BLOCK] 正文代价计数与追踪状态不一致：\n- "
            + "\n- ".join(issues)
            + "\n修复：本章发生代价变化请写全 delta.cost_change；未变化则把正文计数行对齐权威值后再提交。"
        )


# ---------- A-2 灰度开关（2026-10-12 新增，只增不改；默认零行为变化） ----------
#   NWW_A2_MODE = off（默认）| shadow | on
#     off    ：不跑，与 2026-08-28 至今的实际运行行为一致（fail-open）。
#              注意：即使调用方传了 --quality-dir，off 也不跑 —— 这样「接线」与「启用」解耦，
#              接线可以先落地而不改变任何生产行为。
#     shadow ：正常跑门禁，但 fail 只 emit WARNING，不拒绝 commit（用于量化真实误伤率）。
#     on     ：正常跑门禁，fail 即 raise TrackingError（真正硬阻断）。
#   NWW_A2_EXCLUDE = 策略排除项 CSV，默认 "I-J"（置空字符串 = 全部计入，恢复 2026-08-28 语义）。
#     I-J 已由 nww_paragraph_gate.py 在生产闸以校准后的阈值独立执行（J1 BLOCK=220，160 仅
#     WARNING），而 detect_internal.py 的 J1 fail=160 已被判定为「门禁失据」——详见
#     nww_paragraph_gate.py 模块 docstring。若 A-2 继续用 160 阻断，会导致同一段落在两个闸门
#     得到互相矛盾的判定（一边 WARNING 一边 BLOCK）。
_A2_MODE_SWITCH = "NWW_A2_MODE"
_A2_EXCLUDE_SWITCH = "NWW_A2_EXCLUDE"
# 2026-11-05 治本：detect_internal.py 是仓库内固定资产，不该依赖调用方记得传
# --quality-dir。旧契约下 web 链从未传参 → policy.a2_mode=on 也 100% fail-open，
# 面板显示「已开启」而检测零执行。设 0 可回到「必须由调用方传参」的旧行为。
_A2_AUTO_QUALITY_DIR_SWITCH = "NWW_A2_AUTO_QUALITY_DIR"


def _a2_repo_quality_dir() -> str | None:
    """从本文件位置向上定位仓库根 quality/（含 detect_internal.py）。"""
    if os.environ.get(_A2_AUTO_QUALITY_DIR_SWITCH, "1") == "0":
        return None
    for parent in list(Path(__file__).resolve().parents)[:6]:
        cand = parent / "quality" / "detect_internal.py"
        if cand.is_file():
            return str(cand.parent)
    return None


def _a2_mode(project: Path | None = None) -> str:
    """A-2 模式：env NWW_A2_MODE 显式覆盖优先；否则读 PolicyBundle.a2_mode（新书 on）。

    无 project 且无 env 时保持历史默认 off（接线 ≠ 启用；兼容既有单测）。
    """
    if _A2_MODE_SWITCH in os.environ:
        raw = (os.environ.get(_A2_MODE_SWITCH) or "off").strip().lower()
        return raw if raw in ("off", "shadow", "on") else "off"
    if project is not None:
        mode = load_policy(project).a2_mode
        return mode if mode in ("off", "shadow", "on") else "on"
    return "off"


def _a2_exclude_items() -> str:
    """外脑 D1 裁决（2026-10-12）：**保留** "I-J" 硬编码默认值。
    理由：NWW 存在「配置缺失即放行」的历史模式（见 G5：钦天监 bible.json 的 commit_gates = null）。
    若此处去掉默认值，一旦部署层漏配 env，I-J 会带着未经校准的 160 阈值重新进入阻断面，
    实测误伤从 2% 回到 23%。代价（未来新增 I-K 时不知为何默认排除）由下面的 POLICY 日志对冲。
    """
    if _A2_EXCLUDE_SWITCH in os.environ:
        return (os.environ.get(_A2_EXCLUDE_SWITCH) or "").strip()
    return "I-J"


def _a2_config_source(switch: str) -> str:
    """区分「配置默认值」与「环境变量显式覆盖值」（外脑 D1 执行要求）。"""
    return "env" if switch in os.environ else "default"


_A2_SCHEMA = "a2_result_v1"
_A2_ALLOWED_STATUS = ("pass", "warn", "fail", "usage_error", "execution_error")
# status ↔ returncode 权威映射，必须与 detect_internal.py::STATUS_RC 一致
_A2_STATUS_RC = {"pass": 0, "warn": 2, "fail": 1, "usage_error": 3, "execution_error": 4}


def _a2_exec_error(kind: str, detail: str, out: str = "", err: str = "",
                   fail_items=None) -> dict:
    """统一的「门禁没给出可信结论」结果（fail-closed 的唯一出口）。"""
    return {"status": "execution_error", "fail_items": fail_items or [],
            "errors": [{"kind": kind, "detail": detail}],
            "raw": out, "stderr": err, "files_scanned": None, "files_failed": None}


def _coerce_text(value: object) -> str:
    """子进程输出可能是 bytes（TimeoutExpired 携带的就是 bytes）——统一解码，绝不让它炸。"""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _a2_verify_result(payload: dict, returncode: int) -> list[str]:
    """★ 结构化结果的可信性不变量（外脑 P0）。

    根治思路：不是逐个场景加 if，而是规定「什么样的结果才算可信」，其余一律 fail-closed。
    返回违规原因列表；空列表 = 可信。任意一条不满足 → 调用方按 execution_error 处理，
    **绝不接受一个自相矛盾或来路不明的结果**（尤其杜绝「进程非零退出却自报 pass」）。
    """
    why: list[str] = []
    if payload.get("schema") != _A2_SCHEMA:
        why.append(f"schema 不是 {_A2_SCHEMA}（实为 {payload.get('schema')!r}）")
    status = payload.get("status")
    if status not in _A2_ALLOWED_STATUS:
        why.append(f"status 不在允许集合（实为 {status!r}）")
    rc = payload.get("returncode")
    if isinstance(rc, bool) or not isinstance(rc, int):
        why.append(f"returncode 非整数（实为 {rc!r}）")
    else:
        # ① 内部自洽：status 与自报 returncode 必须对应
        if status in _A2_STATUS_RC and rc != _A2_STATUS_RC[status]:
            why.append(f"status={status} 与 returncode={rc} 不自洽（应为 {_A2_STATUS_RC[status]}）")
        # ② 外部自洽：自报 returncode 必须与进程真实退出码一致
        if rc != returncode:
            why.append(f"自报 returncode={rc} 与进程退出码 {returncode} 不一致")
    if not isinstance(payload.get("fail_items"), list) or \
            any(not isinstance(x, str) for x in payload.get("fail_items") or []):
        why.append("fail_items 不是字符串列表")
    if not isinstance(payload.get("errors"), list) or \
            any(not isinstance(x, dict) for x in payload.get("errors") or []):
        why.append("errors 不是对象列表")
    return why


def _a2_run_gate(gate: Path, path: Path, exclude_items: str) -> dict:
    """执行门禁并**解析且校验结构化结果**（外脑 D5/D6 + P0 根治要求）。

    根治点：调用方不再靠退出码猜语义，也不再盲信 stdout 里的 JSON。
    拿不到结果 / 结果不可信（超时、起不来、不可解析、schema 错、状态与退出码矛盾）
    一律判为 execution_error —— **门禁没给出可信结论，绝不等同于检测通过**。
    """
    args = [sys.executable, str(gate)]
    if exclude_items:
        args += ["--exclude", exclude_items]
    args += ["--json", str(path)]
    # encoding/errors 显式指定：避免 text=True 走 locale 默认编码，
    # 子进程输出含非法字节时在解码阶段抛异常（会被误分类成 spawn_error）。
    try:
        proc = subprocess.run(args, capture_output=True, timeout=120,
                              encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired as exc:
        # exc.stdout/stderr 可能是 bytes —— 保留诊断输出（外脑 Q5-3）
        return _a2_exec_error(
            "timeout", "门禁执行超时（120s）",
            _coerce_text(getattr(exc, "stdout", None)),
            _coerce_text(getattr(exc, "stderr", None)))
    except Exception as exc:  # noqa: BLE001 —— 进程起不来/被信号杀等
        return _a2_exec_error("spawn_error", f"{type(exc).__name__}: {exc}")
    out = _coerce_text(proc.stdout)
    err = _coerce_text(proc.stderr)
    payload: object = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("[A2-JSON] "):
            try:
                payload = json.loads(line[len("[A2-JSON] "):])
            except Exception:  # noqa: BLE001
                payload = None
            break
    if not isinstance(payload, dict):
        return _a2_exec_error(
            "unparsable_result",
            f"未拿到结构化结果（returncode={proc.returncode}）", out, err)
    why = _a2_verify_result(payload, proc.returncode)
    if why:
        return _a2_exec_error(
            "untrusted_result",
            f"结构化结果未通过可信性校验：{'; '.join(why)}（returncode={proc.returncode}）",
            out, err, fail_items=payload.get("fail_items") if isinstance(payload.get("fail_items"), list) else None)
    payload["raw"] = out
    payload["stderr"] = err[-2000:]  # 诊断留痕（完整 traceback 走 stderr）
    return payload


def run_phase45_gate(
    project: Path, chapter: int, quality_dir: str | None, mode: str
) -> None:
    """A-2 硬质量门（append 章）：对目标正文跑 detect_internal.py（I-A~I-H）。

    返回码契约（detect_internal.py）：0=全 pass；2=仅 warning（记录不阻断）；
    1=存在 fail（I-G/I-H）→ 拒绝 commit。
    2026-11-05 治本：quality_dir 未传时自动定位仓库根 quality/（固定资产）。
    a2_mode=on 且质量门配置缺失 → fail-closed 抛错，绝不把「未执行」当通过；
    shadow 仍保留原 fail-open；正文定位失败属数据侧缺口，保留原行为但日志带 status。
    只调用不改规则：阈值/扫描逻辑完全保持 detect_internal.py 现状（范围冻结）。

    2026-10-12 新增（只增不改）：
    - 是否执行由 NWW_A2_MODE 决定，默认 off（= 与本轮改动前完全一致）。
    - 允许通过 NWW_A2_EXCLUDE 把「已有更准的独立实现负责」的项从**阻断面**摘出去：
      被排除项仍会被 detect_internal.py 检测并打印（可观测性不丢），只是不抬升返回码。
      默认排除 I-J，理由见上方注释。这是**调用方策略**，不动 detect_internal.py 的任何阈值。
    """
    a2_mode = _a2_mode(project)
    if a2_mode == "off":
        # 与改动前完全一致：零输出、零副作用（V3 已证 100 章逐字节等同）
        return
    # 执行侧自动定位固定资产：显式传参优先，未传时不再把「接线」当成「启用」的前置条件。
    if not quality_dir:
        auto_dir = _a2_repo_quality_dir()
        if auto_dir:
            quality_dir = auto_dir
            emit(f"[A-2 CONFIG] 自动定位质量门目录 {auto_dir}"
                 f"（{_A2_AUTO_QUALITY_DIR_SWITCH}=0 可回到旧契约）")
    exclude_items = _a2_exclude_items()
    mode_source = _a2_config_source(_A2_MODE_SWITCH)
    exclude_source = _a2_config_source(_A2_EXCLUDE_SWITCH)

    def _policy(why: str) -> None:
        """可审计的生效策略日志（外脑 D1 执行要求：必须区分默认值 / env 显式覆盖值）。"""
        emit(
            f"[A-2 POLICY] mode={a2_mode}({mode_source}) "
            f"exclude={exclude_items or '无'}({exclude_source}) "
            f"quality_dir={quality_dir or '（未提供）'} — {why}"
        )

    if mode != "append":
        _policy(f"事务模式={mode} 非 append，A-2 未执行（既有豁免，行为不变）")
        return
    def _config_gap(why: str) -> None:
        """策略说要跑却跑不了：on 模式必须 fail-closed，shadow 保持旧行为。"""
        _policy(f"status=config_error — {why}")
        if a2_mode == "on":
            raise TrackingError(
                f"[A-2 CONFIG] 策略 a2_mode=on 但质量门不可用：{why}。"
                "拒绝以「未执行」伪装通过；设 NWW_A2_MODE=off 可显式回退。"
            )

    if not quality_dir:
        _config_gap("未提供 --quality-dir 且仓库内未部署 quality/detect_internal.py")
        return
    gate = Path(quality_dir) / "detect_internal.py"
    if not gate.is_file():
        _config_gap(f"门禁脚本不存在（{gate}）")
        return
    path = resolve_chapter_file(project, chapter)
    if path is None:
        _policy("status=skipped — 无法定位正文，A-2 未执行（数据侧缺口，保留既有 fail-open）")
        return
    _policy("开始执行")
    res = _a2_run_gate(gate, path, exclude_items)
    status = res.get("status") or "execution_error"
    fail_items = res.get("fail_items") or []
    errors = res.get("errors") or []
    out = (res.get("raw") or "").strip()
    emit(f"[A-2 RESULT] status={status} fail_items={fail_items or '无'} "
         f"errors={len(errors)} scanned={res.get('files_scanned')} "
         f"failed={res.get('files_failed')} empty={res.get('files_empty')}")

    def _block(title: str, body: str) -> None:
        """shadow 只记录；on 才阻断。"""
        if a2_mode == "shadow":
            emit(f"[A-2 SHADOW] {title}（shadow 模式不阻断）：\n{body}")
            return
        raise TrackingError(f"[A-2 BLOCK] {title}：\n{body}")

    if status == "pass":
        return
    if status == "warn":
        if out:
            emit(f"[A-2 WARNING] 质量门禁存在 warning（记录不阻断）:\n{out}")
        return
    if status == "fail":
        # 门禁**已完成检测**，确实存在质量问题 —— 这才是真正的「正文质量不合格」
        _block(
            "正文质量不合格（门禁已完成检测）",
            f"命中项：{', '.join(fail_items) or '未知'}；策略排除：{exclude_items or '无'}\n"
            + (out or "<无输出>")
            + "\n修复：按长写细则 4.5 删减/修正后重跑本 skill 至净，再提交。",
        )
        return
    if status == "usage_error":
        # 门禁没跑起来 —— 是配置/调用问题，不是正文质量问题
        _block(
            "A-2 门禁调用或配置错误（门禁未执行）",
            "这是配置问题，**不是**正文质量问题。\n" + (out or "<无输出>"),
        )
        return
    # execution_error 及任何未知状态：门没跑完，绝不能等同于通过
    # 外脑 Q2 建议：即便判为执行失败，已检测出的 fail_items 必须一并输出，
    # 不能因为「没跑完」就把已经抓到的质量证据丢掉（可观测性，不改阻断结论）。
    _block(
        "质量门执行失败（未得出质量结论，不等于通过）",
        f"错误明细：{errors}\n"
        + (f"已检测出的疑似质量问题（未经完整检测，仅供参考）：{', '.join(fail_items)}\n"
           if fail_items else "")
        + (f"门禁 stderr：{res.get('stderr')}\n" if res.get("stderr") else "")
        + (out or "<无输出>"),
    )


# ---------- A-3 一致性闭环 commit 闸（2026-08-28） ----------
# 契约：{BOOK}/追踪/一致性核验/第{N}章.json
#   {"chapter": N, "closed": true, "open_s1_s2": [], "updated_at": "..."}
# 由 consistency skill 闭环核验完成后落盘（人工裁决也须记录 open_s1_s2 为空）。
# 行为（按 book bible.json 的 commit_gates.consistency）：
#   false（默认，存量兼容）：缺记录→固定 WARNING 不阻断；有记录→校验但不阻断。
#   true（新书模板 / 迁移完成）：缺记录→拒绝 commit；closed=false 或 open_s1_s2 非空→拒绝。
_A3_CLOSURE_DIR = "一致性核验"
_A3_WARN_TEMPLATE = (
    "[A-3 WARNING] Chapter {ch} has no consistency closure record.\n"
    "Current commit gate is compatibility mode.\n"
    "Enable commit_gates.consistency after migration."
)


def _bible_consistency_gate(project: Path) -> bool:
    """读 PolicyBundle：consistency；缺字段按 policy.fail_closed（默认 True）。"""
    return load_policy(project).effective("consistency")


def verify_timeline_id_conflict(state: dict[str, Any], transaction: dict[str, Any]) -> None:
    """A-7 timeline id 冲突闸（2026-09-16 外脑裁定）：append 模式下 delta timeline_events 的 id
    集合与 state.timeline 已有 id 集合有交集 → BLOCK。

    根因：writer 每章从 E001 起编（delta_draft 预填空数组、prompt 无编号约束），
    merge 的 keep_first_chapter=True 只保护 first_recorded_chapter 字段不保护内容字段，
    导致每章 commit 覆盖上一章同 id 事件的 objective_fact/reader_knowledge。

    放行模式：revision（重写同章 timeline 应覆盖自己之前的版本）、
    backfill（本就是修正历史的事务模式）。
    """
    mode = transaction.get("mode", "append")
    if mode not in ("append",):  # revision / backfill / 其他非 append 模式放行
        return
    delta_events = transaction.get("delta", {}).get("timeline_events", [])
    if not delta_events:
        return
    state_timeline = state.get("timeline", {})
    if not state_timeline:
        return
    delta_ids = {e["id"] for e in delta_events if isinstance(e, dict) and "id" in e}
    existing_ids = set(state_timeline.keys())
    conflict_ids = sorted(delta_ids & existing_ids)
    if conflict_ids:
        raise TrackingError(
            f"[A-7 BLOCK] timeline id 冲突：{', '.join(conflict_ids)} 已存在于状态层，"
            f"append 模式禁止复用已有 id（会覆盖历史事件内容）。"
            f"请将 timeline_events 的 id 从当前最大 id+1 开始连续编号。"
        )


def _load_closure_record(project: Path, chapter: int) -> dict[str, Any] | None:
    # 兼容 第001章.json（补零，Web 端标准）与 第1章.json（存量裸数字）
    for name in (f"第{chapter:03d}章.json", f"第{chapter}章.json"):
        try:
            rec = project / "追踪" / _A3_CLOSURE_DIR / name
            data = json.loads(rec.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            continue
    return None


# ---------- C-GATE-READBACK@1：闸判据失败出口的裁决回流（2026-11-13） ----------
# 作者对事实卡的裁决（追踪/闸报告/裁决.json）在 commit 判据处生效：
# accept 且指纹与当前事实卡一致 → 放行并留痕；rollback → 携带 rollback_to 阻断；
# 指纹不符（事实卡已被新事实改写）或台账缺失 → 维持原阻断文案。

def _gate_decision_row(project: Path, chapter: int, action: str) -> dict[str, Any] | None:
    row = _gate.decision(project, chapter)
    if not isinstance(row, dict) or str(row.get("action") or "") != action:
        return None
    return row


def _gate_decision_fresh(project: Path, chapter: int, row: dict[str, Any] | None) -> bool:
    if not isinstance(row, dict):
        return False
    return str(row.get("card_fingerprint") or "") == _gate.card_fingerprint(
        _gate.report(project, chapter))


def _gate_failure_exit(project: Path, chapter: int, code: str, message: str) -> None:
    """闸判据的唯一失败出口：先读作者裁决，再决定放行 / 带目标回滚 / 原样阻断。"""
    accepted = _gate_decision_row(project, chapter, "accept")
    if _gate_decision_fresh(project, chapter, accepted):
        emit(
            f"[GATE ACCEPT] {code} · 第{chapter}章：事实卡已由作者接受，本次提交放行"
            f" by {accepted.get('by') or 'author'}"
            f"（card_fingerprint={accepted.get('card_fingerprint')}）。"
        )
        return
    rolled = _gate_decision_row(project, chapter, "rollback")
    if isinstance(rolled, dict):
        target = str(rolled.get("rollback_to") or "").strip() or "未指定"
        stale = "" if _gate_decision_fresh(project, chapter, rolled) else "（裁决指纹与当前事实卡不同）"
        raise TrackingError(
            message
            + f"\n[GATE ROLLBACK]{stale} 作者裁决回滚：{rolled.get('note') or '无备注'}"
            f"（by {rolled.get('by') or 'author'}）；rollback_to={target}"
        )
    raise TrackingError(message)


# ---------- C-CLOSURE@2 读侧：闭环来源与检测证据（2026-11-13） ----------
# 闭环保留双来源：system_autoclose 必须能把记录指回当时的检测证据；author_confirm
# 只由作者确认覆盖 closed 与 open_s1_s2。存量记录无 source_kind → 按 author_confirm 处理并留一行提示。
_A3_REVIEW_DIR = "一致性审查"
_A3_EVIDENCE_KEYS = ("review_record", "review_record_sha256", "recheck_open_s1_s2")
_A3_SOURCE_KINDS = ("system_autoclose", "author_confirm")


def _review_record_file(project: Path, chapter: int) -> Path | None:
    """定位本章一致性审查记录（流水线写裸章号，Web 端另有补零形态）。"""
    for name in (f"第{chapter}章.json", f"第{chapter:03d}章.json"):
        path = project / "追踪" / _A3_REVIEW_DIR / name
        if path.is_file():
            return path
    return None


def _closure_evidence_problem(project: Path, chapter: int, rec: dict[str, Any]) -> str | None:
    """自动闭环记录与当前审查记录对不上时返回原因；对得上返回 None。"""
    evidence = rec.get("evidence")
    if not isinstance(evidence, dict):
        return "闭环记录缺少 evidence 段"
    missing = [key for key in _A3_EVIDENCE_KEYS if key not in evidence]
    if missing:
        return f"evidence 缺键：{', '.join(missing)}"
    try:
        recheck = int(evidence.get("recheck_open_s1_s2"))
    except (TypeError, ValueError):
        return f"evidence.recheck_open_s1_s2 取值非法：{evidence.get('recheck_open_s1_s2')!r}"
    if recheck != 0:
        return f"evidence.recheck_open_s1_s2={recheck}，复检未清零"
    recorded = str(evidence.get("review_record_sha256") or "").strip().lower()
    if not recorded:
        return "evidence.review_record_sha256 为空"
    review_path = _review_record_file(project, chapter)
    if review_path is None:
        return "追踪/一致性审查/ 下无本章审查记录，证据无处核对"
    try:
        current = hashlib.sha256(review_path.read_bytes()).hexdigest()
    except OSError as exc:
        return f"审查记录无法读取：{exc}"
    if current != recorded:
        return (f"审查记录已变化（记录 {recorded[:12]}…，当前 {current[:12]}…，"
                f"源 {evidence.get('review_record')}）")
    return None


def verify_consistency_closure(project: Path, chapter: int) -> None:
    gate = _bible_consistency_gate(project)
    rec = _load_closure_record(project, chapter)
    if rec is None:
        if not gate:
            # 兼容模式：只报告，不阻断。
            emit(_A3_WARN_TEMPLATE.format(ch=chapter))
            return
        _gate_failure_exit(
            project, chapter, "A-3/consistency_closure",
            f"[A-3 BLOCK] Chapter {chapter} has no consistency closure record"
            "（commit_gates.consistency=true）。"
            "先运行 /nww-consistency 并对 S1/S2 闭环，落盘 追踪/一致性核验/第{ch}章.json 后再提交。".format(ch=chapter),
        )
        return
    open_items = rec.get("open_s1_s2") or []
    # 兼容模式沿用旧口径（缺 closed 键按已闭环）；硬闸口径缺键即按未闭环处置。
    closed_default = not gate
    if not rec.get("closed", closed_default) or open_items:
        # Task 8 / C-P1-3：记录内显式 override（未过期 + reason）→ 放行并 WARN 留痕。
        # 运行时豁免的权威审计在 追踪/overrides.jsonl（nww_override_audit.py）；
        # 此处只认闭环记录内嵌 override（与该章冲突绑定，天然带章节范围）。
        ov = rec.get("override") or {}
        if isinstance(ov, dict) and ov.get("gate") == "consistency" and (ov.get("reason") or "").strip():
            from nww_policy import _override_expired
            if not _override_expired(str(ov.get("expires_at") or "")):
                emit(
                    "[A-3 OVERRIDE] Chapter {ch} consistency open conflict 放行：{reason}"
                    "（expires_at={exp}；豁免须同时登记 追踪/overrides.jsonl 审计）".format(
                        ch=chapter, reason=ov.get("reason"), exp=ov.get("expires_at"))
                )
                return
        message = (
            f"[A-3 BLOCK] Chapter {chapter} consistency not closed"
            f"（closed={rec.get('closed', closed_default)}，open_s1_s2={open_items}）。"
            "修复/裁决全部 S1/S2 冲突后重写闭环记录再提交；"
            "确需带伤提交须登记 override（nww_override_audit.py add + 闭环记录 override 字段，"
            "带 reason 与 expires_at）。"
        )
        if not gate:
            emit(
                f"[A-3 WARNING] Chapter {chapter} consistency record has open conflict"
                f"（closed={rec.get('closed', closed_default)}，open_s1_s2={open_items}）"
                "（compatibility mode，不阻断提交）。"
            )
            return
        _gate_failure_exit(project, chapter, "A-3/consistency_closure", message)
        return
    # 已闭环且 open_s1_s2 为空 → 按来源分流校验（C-CLOSURE@2 读侧）。
    source_kind = str(rec.get("source_kind") or "").strip()
    if source_kind == "system_autoclose":
        problem = _closure_evidence_problem(project, chapter, rec)
        if problem is not None:
            message = (
                f"[A-3 BLOCK] closure evidence stale：Chapter {chapter} 自动闭环记录与当前检测证据"
                f"对不上（{problem}）。重跑一致性审查并重新闭环落盘后再提交。"
            )
            if not gate:
                emit(f"[A-3 WARNING] {message}（compatibility mode，不阻断提交）。", error=True)
                return
            _gate_failure_exit(project, chapter, "A-3/consistency_closure", message)
        return
    if source_kind == "author_confirm":
        return
    unknown = "" if not source_kind else f"（未知取值 {source_kind}）"
    emit(
        f"[A-3 WARNING] Chapter {chapter} 闭环记录无 source_kind{unknown}，按 author_confirm 口径"
        "只校验 closed 与 open_s1_s2；该记录与检测证据的对应关系需人工回查。"
    )


# ---------- A-4 正文-delta 交叉审计闸（2026-09-05，压缩质量硬闸） ----------
# 契约：提取逻辑在 nww_delta_audit.py（纯确定性、零 LLM、只读）。
# BLOCK 刻意收窄：仅「被追踪角色 × 强变化关键词（突破/死亡）同句」；
# 受伤/纯出场/伏笔提及/delta 多写均 WARNING。
# 门控与 A-3 同一兼容模式语义：bible.commit_gates.delta_audit=true → BLOCK；
# 缺失/false（存量书）→ 仅 WARNING 不阻断。对 append 与 revision 一并生效。
def _bible_delta_audit_gate(project: Path) -> bool:
    """读 PolicyBundle：delta_audit；缺字段按 policy.fail_closed（默认 True）。"""
    return load_policy(project).effective("delta_audit")


def verify_delta_consistency(
    project: Path, transaction: dict[str, Any], next_state: dict[str, Any]
) -> None:
    try:
        from nww_delta_audit import collect_delta_issues
    except Exception as exc:  # 审计模块损坏时不反噬提交主流程（fail-open）
        emit(f"[A-4 WARNING] nww_delta_audit.py 不可用，跳过交叉审计：{exc}", error=True)
        return
    try:
        issues = collect_delta_issues(project, transaction, next_state)
    except Exception as exc:
        emit(f"[A-4 WARNING] 交叉审计执行异常，跳过（fail-open）：{exc}", error=True)
        return
    if not issues:
        return
    blocks = [i for i in issues if i.get("severity") == "block"]
    warns = [i for i in issues if i.get("severity") == "warning"]
    for item in warns:
        emit(f"[A-4 WARNING] {item['rule']}：{item['description']}", error=True)
    if not blocks:
        return
    if _bible_delta_audit_gate(project):
        _gate_failure_exit(
            project, int(transaction["chapter"]), "A-4/delta_audit",
            "[A-4 BLOCK] 正文-delta 交叉审计发现漏写（commit_gates.delta_audit=true）：\n- "
            + "\n- ".join(i["description"] for i in blocks)
            + "\n修复：把漏写的状态变化补进对应 delta 字段（character_changes / retired_characters）"
            "后重新提交；若正文表述实为误伤，复核正文后在 bible.json 显式关闭 commit_gates.delta_audit。",
        )
        return
    emit(
        "[A-4 WARNING] 检测到疑似 delta 漏写（当前为兼容模式不阻断；"
        "确认规则稳定后在 bible.json 设 commit_gates.delta_audit=true 启用硬闸）：\n- "
        + "\n- ".join(i["description"] for i in blocks),
        error=True,
    )


# ---------- A-6 Settlement 互锚闸（2026-09-15，Settlement↔正文互锚校准） ----------
# 背景：Settlement 写入链是单向「正文 → LLM 自由推断 → 落库」，缺反向互锚。
# delta 的语义字段（result/timeline_events/character_changes/commitments/global）完全由
# 宿主 LLM 推断生成，A-1~A-5 均不含「delta 声称事实能否在正文明文找到依据」校验 →
# Settlement 可凭空写入正文未发生的事件/数值/时序（ch25 奶茶、ch29 箱壁纹路、ch26 烟值72、
# ch28 吴长老「未出场」、ch29「已获知初一」、ch31「已受理」）。
# 本闸做反向互锚：只检查「可机械验证的高置信事实」，不试图理解文学潜台词/推断/心理。
# 门控与 A-3/A-4/A-5 同语义：bible.commit_gates.settlement_anchor=true → BLOCK；
# 缺失/false（存量书）→ 仅 WARNING 不阻断。override 只解除本闸，其它闸照常。

_SETTLEMENT_ANCHOR_OFF = ("override", "down")

def _bible_settlement_anchor_gate(project: Path) -> bool:
    """读 PolicyBundle：settlement_anchor；缺字段按 policy.fail_closed（默认 True）。"""
    return load_policy(project).effective("settlement_anchor")


def _settlement_override(delta: dict[str, Any]) -> tuple[bool, str, bool]:
    """读取 delta 的 settlement_override。返回 (enabled, reason, valid)。
    valid = enabled 时必须带非空 reason；否则 override 无效 → 仍应 BLOCK。"""
    ov = delta.get("settlement_override") or {}
    if isinstance(ov, bool):
        enabled, reason = ov, ""
    elif isinstance(ov, dict):
        enabled, reason = bool(ov.get("enabled")), str(ov.get("reason") or "").strip()
    else:
        return False, "", True
    reason_ok = bool(reason)
    valid = (not enabled) or reason_ok
    return enabled, reason, valid


def _prose_for_chapter(project: Path, chapter: int) -> str | None:
    """定位章正文明文（复用 resolve_chapter_file），定位不到返回 None。"""
    try:
        path = resolve_chapter_file(project, chapter)
    except Exception:
        return None
    if path is None:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


# ============ 中文数字归一化（2026-10-11 修复：闸"双向失效"的第一半）============
# 背景：_extract_anchor_from_prose 的 any_nums 只抓阿拉伯数字 \d。
# 实测后果（tests/test_hardfault_gate_matrix.py）：
#   C3 误杀 —— 正文写「五百份」，delta 按 500 声称 → 正文中无阿拉伯 500 → BLOCK（正确内容被拦）
#   C1 漏检 —— 正文写「二十出头」「三十载」，中文数字不在任何锚点 → 矛盾不可见
# 归一化让"写法"不再决定"真伪"。回滚开关：NWW_ANCHOR_CN_NORMALIZE=0
_CN_NUM_NORMALIZE = os.environ.get("NWW_ANCHOR_CN_NORMALIZE", "1") != "0"

_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}
# 中文数字串（不含阿拉伯数字），用于从正文抽取
_CN_NUM_RE = re.compile(r"[零一两二三四五六七八九十百千万]{1,8}")


def _cn_to_int(s: str) -> int | None:
    """中文数字串 → int。保守实现：歧义形式（如「七八十」这类约数）返回 None，不猜。

    >>> _cn_to_int("二十") -> 20 ; _cn_to_int("五百") -> 500 ; _cn_to_int("七八十") -> None
    """
    if not s:
        return None
    total = 0
    cur = 0
    seen_digit = False
    prev_was_digit = False
    for ch in s:
        if ch in _CN_DIGITS:
            if prev_was_digit:
                # 连续两个数字字符（如「七八」）→ 约数写法，不猜，整体判为不确定
                return None
            cur = _CN_DIGITS[ch]
            seen_digit = True
            prev_was_digit = True
            continue
        if ch in _CN_UNITS:
            u = _CN_UNITS[ch]
            if u >= 10000:
                total = (total + (cur or 1)) * u
            else:
                total += (cur if seen_digit else 1) * u
            cur = 0
            seen_digit = False
            prev_was_digit = False
            continue
        return None
    return total + cur if (total or cur or s == "零") else None


def _extract_cn_numbers(prose: str) -> set[int]:
    """抽取正文中的中文数字并归一化（保守，歧义形式自动跳过）。"""
    out: set[int] = set()
    if not _CN_NUM_NORMALIZE:
        return out
    for m in _CN_NUM_RE.finditer(prose):
        v = _cn_to_int(m.group(0))
        if v is not None and 0 <= v < 10_000_000:
            out.add(v)
    return out


def _extract_anchor_from_prose(prose: str) -> dict[str, Any]:
    """从正文明文确定性提取可验证事实锚点（零 LLM）。

    提取的锚点用于「delta 声称 vs 正文」反向核对：
    - meter：正文出现的「名称+数字」计数值（如烟火值、灵石等），取每次出现；
    - power/death：强变化关键词所在句（复用 nww_delta_audit 正则思路，此处内联避免重写）；
    - present：章节内确实登场/被提名的角色/关键词集合（供「正文没有→ delta 凭空新增」判定）。
    """
    # 复用 nww_delta_audit 的权威正则，避免两处定义漂移。
    # 注意：nww_delta_audit 反向 import 本模块（循环依赖），故必须延迟局部导入。
    from nww_delta_audit import _POWER_GAIN_RE, _DEATH_RE
    anchors: dict[str, Any] = {
        "meter": {},      # {名称: [数值,...]} 仅识别「名:数字」格式（如烟火值：70）
        "any_nums": set(re.findall(r"\d{1,7}", prose)),  # 全文任意数字集合（覆盖「从67到70」等非冒号书写）
        # 2026-10-11 新增（扩展键，不改变 any_nums 原语义）：中文数字归一化结果
        "any_nums_cn": _extract_cn_numbers(prose),
        "power_sents": [],  # 触发强状态变化关键词的句子
        "death_sents": [],
        "positive_words": set(),  # 正文明文出现的关键实体词（去标点去空格，用于宽松锚定）
        "positive_chars": set(re.findall(r"[\u4e00-\u9fa5]", prose)),  # 正文全部中文字符（侧写关联判定用）
    }
    for sent in re.split(r"[。！？!?\n]", prose):
        if not sent.strip():
            continue
        # 数值计费：形如「烟火值：70」
        for m in re.finditer(r"([\u4e00-\u9fa5A-Za-z]{1,8})[：:]\s*(\d{1,7})", sent):
            name, val = m.group(1), m.group(2)
            anchors["meter"].setdefault(name, []).append(val)
        # 强状态变化句
        if _POWER_GAIN_RE.search(sent):
            anchors["power_sents"].append(sent.strip())
        if _DEATH_RE.search(sent):
            anchors["death_sents"].append(sent.strip())
    # 正文明文实体词集合（宽松锚定：去空格去标点）。
    # 注意：贪婪窗口 {2,8} 可能把专名切断（如「他凝神炼成了烈焰诀」被切成
    # 「他凝神炼成了烈」+「焰诀」，断在专名中间）→ 追加一档「去标点全文本」兜底，
    # 使 _check_named_anchor 的子串匹配能命中正文任何连续性实名的完整形式（外脑 B 修案）。
    anchors["positive_words"] = set(re.findall(r"[\u4e00-\u9fa5]{2,8}", prose))
    anchors["positive_words"].add(
        re.sub(r"[\s，。；：、！？!?（）()「」『』“”\"'·…\n]+", "", prose)
    )
    return anchors


def _check_numeric_anchor(
    prose_anchors: dict[str, Any], sent: str, label: str
) -> tuple[bool, str | None]:
    """若 delta 声称句含数值，要求正文在不限格式的任意数字集中能锚定同一数值（覆盖「从67到70」书写）。
    返回 (ok, detail)。数值层只做「正文必须出现该数值」的宽松核对；delta 凭空多写的数值即 BLOCK。"""
    nums = [n for n in re.findall(r"\d{1,7}", sent)]
    if not nums:
        return True, None  # 该句无明确数值，数值层不过问
    any_nums = set(prose_anchors.get("any_nums") or [])
    # 2026-10-11：并入中文数字归一化结果 → 「五百份」可锚定 500，消除写法导致的误杀（C3）
    if _CN_NUM_NORMALIZE:
        any_nums |= {str(v) for v in (prose_anchors.get("any_nums_cn") or ())}
    missing = [n for n in nums if n not in any_nums]
    if not missing:
        return True, None
    return False, f"{label} 声称数值 {missing} 在正文明文无对应数值锚点"


# 人称代词集合：正文强状态句用代词指代角色（如「他凝神炼成了…」）时视为有效锚（外脑 B 修案）。
_PRONOUN_RE = re.compile(r"(他|她|它|其|此)")


def _check_strong_state_anchor(
    prose_anchors: dict[str, Any], change: dict[str, Any]
) -> tuple[str, str]:
    """强状态变化反向锚：delta 声称角色强变化/死亡，正文须有对应强变化句。

    返回值 (verdict, detail)，verdict ∈ {"pass","warning","block","skip"}：
      - skip：变更既非死亡也非强变化（普通变更）→ 不进强状态锚（外脑 A 修案，修案1 失败1）。
      - pass：正文存在强变化/死亡句，且句内含该角色名 或 人称代词（外脑 B 修案，修案2 失败2）。
      - block：正文完全无对应强变化/死亡句（强状态多写）。
      - warning：正文确有强变化句但仍存在强变化，无法机械确定角色归属 → 不强行 BLOCK。
    宽松匹配：只要正文存在任一强变化句且句内含角色名/人称代词即可（避免过度误伤）。"""
    name = str(change.get("name", ""))
    chg = str(change.get("change", "") or change.get("summary", "") or "")
    # 复用权威正则（延迟局部导入，避开 nww_delta_audit ↔ 本模块 循环依赖）
    from nww_delta_audit import _POWER_GAIN_RE, _DEATH_RE
    is_death = bool(_DEATH_RE.search(chg))
    is_power = bool(_POWER_GAIN_RE.search(chg))
    if not name:
        return "pass", ""
    if not is_death and not is_power:
        return "skip", ""  # 普通变更不进强状态锚（A 修案）
    pools = prose_anchors["death_sents"] if is_death else prose_anchors["power_sents"]
    if not pools:
        # delta 声称强变化/死亡，正文连对应强变化句都没有 → 可疑多写
        kind = "死亡" if is_death else "强状态变化"
        return ("block",
                f"角色「{name}」被 delta 声明{kind}，但正文明文无对应强变化/死亡句")
    for s in pools:
        if name in s or _PRONOUN_RE.search(s):
            return "pass", ""
    # 正文确有强变化句但句中既无角色名也无代词（无法确定归属）→ 降为 WARNING（B 修案）
    return ("warning",
            f"角色「{name}」被 delta 声明强变化，正文有强变化句但无法机械确定其归属")


# ---------- A-6 高风险新增事实的「实名锚点」判定（2026-09-15，外脑三档） ----------
# 对齐外脑裁决：A-6 只检查「可机械验证的高置信新高风险事实」，不证明 Settlement 语义归纳正确。
# 三档：
#   ① 强实名新增：delta 声称某具体实名词（物品/人物/地点/组织/强状态实体）「已经发生」，
#     但正文完全找不到该实名锚 → BLOCK；
#   ② 强状态/数字/时间：必须正文有对应显式锚点（数值已在 _check_numeric_anchor 覆盖）；
#   ③ 正文无同名词但有部分字（疑似侧写/抽象表达）→ WARNING，不强行 BLOCK；
#    无法提取实名的普通摘要/推断 → 不审查（外脑裁决「普通描述不做全语义审查」）。
# 机械层不判断「侧写是不是等价表达」，只看「实名/强锚点存在性」。

# 高风险「新增/变化」触发动作词：声称句必须是这类事实，才进实名锚审查（否则视为普通摘要不审）。
# 外脑失败4拍板：维持“普通物品描述默认不审”，仅补极小高风险物品状态动作词
# （获得 拿到 得到 取出 收起 带回 带走 放入 放进 解锁 开启 新增）。「煮好」是物品处理/使用过程，
# 不属“新增物品事实”，不入审查（case7 维持 PASS）。
_HIGH_RISK_ACTION_RE = re.compile(
    r"(获得|拿到|得到|取出|收起|带回|带走|放入|放进|解锁|开启|新增"
    r"|领取|收下|入手|炼成|签收|开出|出现|发现"
    r"|突破|晋升|晋级|领悟|习得|死亡|陨落|毙命|认可|受理|接受|迎娶|结盟|遇袭|重伤|离场)"
)

# 动作词边界：作为声称句切分锚点（C 修案——把「获得/解锁」等动词当边界切段，避免把
# 专名+动词+宾语切成垃圾实名，如「林小凡获得聚气丹」不得切出「林小凡获/得聚气丹」）。
# 必须与 _HIGH_RISK_ACTION_RE 词表严格一致：凡触发「收集审查」的动词，也必须是切段边界，
# 否则声称句被收集但切不出宾语 → 空候选静默 pass（曾导致 ch25「发现…瓶装奶茶」幻写漏放）。
_NAMED_BOUNDARY_RE = re.compile(
    r"(获得|拿到|得到|取出|收起|带回|带走|放入|放进|解锁|开启|新增"
    r"|领取|收下|入手|签收|开出|出现|发现"
    r"|突破|晋升|晋级|领悟|习得|死亡|陨落|毙命|认可|受理|接受|炼成|迎娶|结盟|遇袭|重伤|离场)"
)

# 从声称句中剔除的停用词（动词/虚词/通用主体词，避免把「获得」当实名词锚）
_NAMED_STOPWORDS = frozenset(
    "获得 得到 解锁 领取 收下 入手 炼成 签收 新增 开出 出现 发现 突破 晋升 晋级 "
    "领悟 习得 死亡 陨落 毙命 认可 受理 接受 迎娶 结盟 遇袭 重伤 离场 自己 他们 我们 你们 众人 全场 对方 "
    "此刻 忽然 突然 已经 开始 仿佛 似乎 整个 面前 心中 身后 远处 一场 一名 一起 完全 直接 终于 基本 "
    "这 那 个 的 了 是 有 在 与 和 也 都 就 又 并 但 而 或 及 被 把 让 给 为 从 对 向 朝 地 着 过 呢 么 啊 吧".split()
)


def _split_named_tokens(sent: str) -> list[str]:
    """从声称句提取「动作词之后」的高风险新增宾语实名候选（去停用词）。C 修案（外脑裁决）。

    核心：A-6 实名锚针对的是「新增了什么」——即强动作词（获得/解锁/炼成…）之后的宾语
    （物品/功法/人物等），而非动作施动者主语（如「林小凡获得聚气丹」，新增物=聚气丹，
    「林小凡」是施动者，不应作为新增实体被审）。

    实现：用 _NAMED_BOUNDARY_RE 找出声称句里所有强动作词，取其**之后**的子串段，
    对每段提取 2-4 字连续中文候选；主语段（动作词之前）不计入。

    只保留长度 2..4 的连续中文串，按停用词过滤；去重保序。
    抽象短语（「箱壁出现异常」「局势发生变化」中的动补结构）不会被当实名锚。"""
    # 找所有动作词出现位置 → 动作词之后到下一个动作词/标点之间 = 宾语段
    segments: list[str] = []
    for m in _NAMED_BOUNDARY_RE.finditer(sent):
        tail = sent[m.end():]
        for seg in re.split(r"[\s，。；：、！？,.;:!?\n“”\"'（）()·…]+", tail):
            seg = seg.strip()
            if seg:
                segments.append(seg)
    seen: list[str] = []
    used = set()
    for seg in segments:
        # 片段本身若为 2-4 字且非停用词，优先整段作为候选（保住完整专名如「聚气丹」）
        if 2 <= len(seg) <= 4 and seg not in _NAMED_STOPWORDS:
            if seg not in used:
                used.add(seg)
                seen.append(seg)
            continue
        # 否则在片段内取 2-4 字窗口候选
        for tok in re.findall(r"[\u4e00-\u9fa5]{2,4}", seg):
            if tok in _NAMED_STOPWORDS or any(s in tok for s in _NAMED_STOPWORDS if len(s) <= 1):
                continue
            if len(tok) < 2:
                continue
            if any(tok in w for w in _NAMED_STOPWORDS):
                continue  # 词是停用词的子串（如「受」），避免抓抽象
            if tok not in used:
                used.add(tok)
                seen.append(tok)
    return seen


def _collect_high_risk_named_lines(delta: dict[str, Any]) -> list[tuple[str, str]]:
    """收集 delta 中属于「高风险新增/变化」的声称句（含强动作词），返回 [(句, 来源)]。

    来源标记：character / timeline / result。纯摘要句（无强动作词）不收集 → 不审查。"""
    lines: list[tuple[str, str]] = []
    for chg in (delta.get("character_changes", []) or []):
        for field in ("change", "summary"):
            txt = str(chg.get(field) or "").strip()
            if txt and _HIGH_RISK_ACTION_RE.search(txt):
                lines.append((txt, "Character"))
    for ev in (delta.get("timeline_events", []) or []):
        for field in ("content", "objective_fact"):
            txt = str(ev.get(field) or "").strip()
            if txt and _HIGH_RISK_ACTION_RE.search(txt):
                lines.append((txt, "Timeline"))
    r_txt = str(delta.get("result") or "").strip()
    if r_txt and _HIGH_RISK_ACTION_RE.search(r_txt):
        lines.append((r_txt, "Result"))
    return lines


def _check_named_anchor(prose_anchors: dict[str, Any], sent: str) -> tuple[str, str]:
    """对一个高风险声称句做实名锚判定。返回 (verdict, detail)。

    - 句中所有实名候选都完整出现在正文 → pass；
    - 存在某个实名候选正文无完整词、但命中正文部分字（疑似侧写）→ warning；
    - 存在某个实名候选正文连部分字都没有 → block。"""
    toks = _split_named_tokens(sent)
    if not toks:
        return "pass", ""  # 无实名声称可判定（纯摘要/描述），不审查
    body_words = prose_anchors.get("positive_words") or set()
    body_chars = prose_anchors.get("positive_chars") or set()
    for tok in toks:
        # 正文完整实名命中：短词直接命中，或作为长词子串（positive_words 是贪婪窗口，
        # 实体名可能被包含在长词内，如「聚气丹」含在「他收好那枚聚气丹」）→ 有据
        if tok in body_words or any(tok in w for w in body_words):
            continue
        # 正文无完整实名 → 看是否有部分字（疑似侧写）
        hit_partial = any(ch in body_chars for ch in tok)
        if hit_partial:
            return ("warning",
                    f"delta 声称高风险实名词「{tok}」正文无完整锚点但含部分字（疑似侧写，无法机械定性，请人工复核）")
        return ("block",
                f"delta 声称高风险新增实名词「{tok}」在正文明文完全无对应锚点")
    return "pass", ""


# 离场/否定断言反向锚（外脑案例：ch28 吴长老「未出场」，正文明确出场）
_ABSENCE_PATTERNS = (
    ("未出场", "␣"), ("没出场", "␣"), ("没有出现", "␣"), ("未登场", "␣"),
    ("不在场", "␣"), ("缺席", "␣"), ("没露面", "␣"), ("消失", "␣"), ("未见", "␣"),
    ("无用武之地", "␣"), ("没有他的戏份", "␣"),
)


def _check_absence_anchor(prose_anchors: dict[str, Any], delta: dict[str, Any]) -> tuple[str, str]:
    """delta 声称某角色「未出场/消失」，但正文明确含该角色名 → BLOCK。

    反向互锚：正文明确的发生，delta 却断言未发生 = 把正文事实写错了。
    注意：positive_words 是贪婪窗口，角色名可能被包含在长词内（如「吴长老一挥衣袖」）
    且可能被标点切断；故用「角色名是某窗口子串 或 角色名直接命中」判定「在场」。"""
    body_words = prose_anchors.get("positive_words") or set()
    for chg in (delta.get("character_changes", []) or []):
        name = str(chg.get("name") or "")
        txt = str(chg.get("change") or chg.get("summary") or "")
        if not name or not txt:
            continue
        if "未出场" not in txt and "消失" not in txt and "不在场" not in txt \
                and "没有出现" not in txt and "缺席" not in txt and "弄丢" not in txt and "未见" not in txt:
            continue
        if name in body_words or any(name in w for w in body_words):  # 正文明确该角色在章内出现
            return ("block",
                    f"正文明确「{name}」在场，但 delta 声称其「未出场/消失」")
    # 也检查 result 里对核心角色的离场断言
    r_txt = str(delta.get("result") or "")
    for name in [chg.get("name") for chg in (delta.get("character_changes", []) or [])]:
        if not name or name not in r_txt:
            continue
        if ("未出场" in r_txt or "没有他的戏份" in r_txt) and (
                name in body_words or any(name in w for w in body_words)):
            return ("block", f"正文明确「{name}」在场，但 delta.result 声称其「未出场」")
    return "pass", ""


def verify_settlement_anchor(project: Path, transaction: dict[str, Any]) -> dict[str, Any]:
    """A-6 Settlement 互锚闸：检查 transaction 本次新增/修改的可验证事实能否在正文明文找到依据。

    只读、纯确定性、零 LLM。返回审计结果 dict（供 meta.settlement_anchor 记录）：
      {"status": "pass"|"warning"|"block", "checked": N, "warnings": N, "blocks": N, "details": [...]}
    门控：bible.commit_gates.settlement_anchor=true → 有 block 即 raise TrackingError；
    缺失/false → 仅 emit WARNING 不阻断（存量兼容）。
    override 仅解除本闸；override 无 reason 视为无效，仍 BLOCK。
    """
    delta = transaction.get("delta") or {}
    chapter = transaction.get("chapter")
    prose = _prose_for_chapter(project, int(chapter)) if chapter else None
    detail: list[tuple[str, str, str]] = []  # (verdict, rule, desc)
    checked = 0
    if not prose:
        emit("[A-6 WARNING] 无法定位正文明文，跳过 Settlement 互锚审计（fail-open）", error=True)
        return {"status": "warning", "checked": 0, "warnings": 1, "blocks": 0, "details": []}
    anchors = _extract_anchor_from_prose(prose)

    # 用闭包逐项累计判定（counts 以可变 dict 承载，避免嵌套 def 里 nonlocal 的槽位问题）
    counts = {"checked": 0, "warnings": 0, "blocks": 0}

    def add_issue(verdict: str, rule: str, desc: str) -> None:
        detail.append((verdict, rule, desc))
        counts["checked"] += 1
        if verdict == "warning":
            counts["warnings"] += 1
        elif verdict == "block":
            counts["blocks"] += 1

    # 1) 强状态变化 + 死亡（character_changes / retired）
    for change in delta.get("character_changes", []) or []:
        verdict, why = _check_strong_state_anchor(anchors, change)
        if verdict == "skip":
            continue  # 普通变更不进强状态锚（外脑 A 修案，避免误伤）
        if verdict == "pass":
            add_issue("pass", "A-6 强变化锚定", f"角色 {change.get('name')} 强变化在正文明文有据")
        elif verdict == "block":
            add_issue("block", "A-6 强变化多写", why)
        elif verdict == "warning":
            add_issue("warning", "A-6 强变化锚定", why)  # 无法确定角色归属 → 不 BLOCK 仅 WARNING
    for name in delta.get("retired_characters", []) or []:
        verdict, why = _check_strong_state_anchor(anchors, {"name": name, "change": "死亡"})
        if verdict == "skip":
            continue
        if verdict == "pass":
            add_issue("pass", "A-6 死亡锚定", f"角色 {name} 死亡/退役在正文有据")
        elif verdict == "block":
            add_issue("block", "A-6 死亡多写", why)
        elif verdict == "warning":
            add_issue("warning", "A-6 死亡锚定", why)

    # 2) 明确数值（timeline_events / foreshadow / result 中的数值句）
    for ev in delta.get("timeline_events", []) or []:
        sent = str(ev.get("content") or ev.get("objective_fact") or "")
        ok, why = _check_numeric_anchor(anchors, sent, f"timeline {ev.get('id')}")
        if ok:
            add_issue("pass", "A-6 数值锚定", f"timeline {ev.get('id')} 数值与正文一致")
        else:
            add_issue("block", "A-6 数值多写", why)

    # 3) 高风险新增事实的实名锚点（外脑三档：实名新增 / 状态数字 / 侧写）。
    #    强实名新增（物品/人物/地点/组织实名词）在正文完全无锚 → BLOCK；
    #    正文含部分字（疑似侧写）但无完整实名 → WARNING；
    #    无法提取实名的普通摘要/推断 → 不审查（外脑裁决「普通描述不做全语义审查」）。
    for raw_line, kind_src in _collect_high_risk_named_lines(delta):
        verdict, why = _check_named_anchor(anchors, raw_line)
        if verdict != "pass":
            add_issue(verdict, f"A-6 {kind_src}实名锚定", why)

    # 3b) 离场/否定断言反向锚：delta 声称某角色「未出场/消失」，正文却明确有该角色 → BLOCK。
    verdict, why = _check_absence_anchor(anchors, delta)
    if verdict != "pass":
        add_issue(verdict, "A-6 离场断言锚定", why)

    # 汇总
    status = "pass"
    if counts["blocks"]:
        status = "block"
    elif counts["warnings"]:
        status = "warning"

    return {"status": status, "checked": counts["checked"],
            "warnings": counts["warnings"], "blocks": counts["blocks"], "details": detail}


def _run_settlement_anchor_guard(project: Path, transaction: dict[str, Any]) -> None:
    """A-6 门控接线：执行互锚审计并按门控语义处理结果。

    - 审计结果无论 pass/warning 都写入 delta 的 meta.settlement_anchor（供人工检索/复盘）。
    - 门控：bible.commit_gates.settlement_anchor=true 且存在 block → raise TrackingError（拒绝 commit）；
      未开启（存量兼容）→ 有 block 仅 emit WARNING 不阻断。
    - override（settlement_override=enabled，须带 reason）仅解除本闸；override 无 reason 视为无效仍 BLOCK。
    - revision 模式豁免（与 A-5 一致，修订章不走全流程门控）。
    """
    if transaction.get("mode") != "append":
        return  # revision 模式豁免
    gate_on = _bible_settlement_anchor_gate(project)
    ov_enabled, ov_reason, ov_valid = _settlement_override(transaction.get("delta") or {})
    audit = verify_settlement_anchor(project, transaction)
    # 记录审计结果到 delta 的 meta，便于追踪与复盘（不落库失败）
    # 对齐外脑裁决：delta.meta 需含 settlement_anchored: true + 完整审计结果
    try:
        meta = (transaction.get("delta") or {}).setdefault("meta", {})
        meta["settlement_anchored"] = True
        meta["settlement_anchor"] = {
            "status": audit["status"],
            "checked": audit["checked"],
            "warnings": audit["warnings"],
            "blocks": audit["blocks"],
            "details": [list(d) for d in audit["details"]],
        }
    except Exception:
        pass  # meta 记录失败不影响门控主判定
    if audit["status"] != "block":
        return  # 无 block：pass 或 warning 均放行
    # 有 block → 判断 override / 门控
    if ov_enabled and ov_valid:
        emit(
            f"[A-6 OVERRIDE] 忽略 {audit['blocks']} 个互锚 BLOCK（reason: {ov_reason}）。"
            f"override 仅解除本闸，其它闸照常。",
            error=True,
        )
        return
    blocks = "；".join(f"{v}/{r}: {d}" for v, r, d in audit["details"] if v == "block")
    if not ov_valid and (ov_enabled or ov_reason):
        emit(
            "[A-6 WARNING] 检测到 settlement_override 但缺少 reason，override 无效，本次 BLOCK 生效。",
            error=True,
        )
    if gate_on:
        raise TrackingError(
            "[A-6 BLOCK] Settlement 声称事实在正文明文无依据（commit_gates.settlement_anchor=true）：\n- "
            + blocks
            + "\n修复：把 body 里确实发生的事实对齐 delta，或补充正文，或显式带 reason 设置 delta.settlement_override。"
        )
    emit(
        f"[A-6 WARNING] Settlement 声称 {audit['blocks']} 处事实在正文明文无依据（当前为兼容模式不阻断；"
        "确认规则稳定后设 bible.commit_gates.settlement_anchor=true 启用硬闸）：\n- "
        + blocks,
        error=True,
    )


# ---------- A-5 流程完整性闸（2026-09-06，post_write 封装配套） ----------
# 规避宿主 LLM 选择性执行：enforce / 审稿闭环 是质量底线，不可跳过。
# 门控与 A-3/A-4 同一兼容模式语义：bible.commit_gates.meta_check=true → BLOCK；
# 缺失/false（存量书）→ 仅 WARNING 不阻断。revision 模式豁免（修订章不需要全流程）。

def _bible_meta_check_gate(project: Path) -> bool:
    """读 PolicyBundle：meta_check；缺字段按 policy.fail_closed（默认 True）。"""
    return load_policy(project).effective("meta_check")


def _verify_frozen_body_hash(project: Path, transaction: dict[str, Any],
                             quality_meta: dict[str, Any]) -> str | None:
    """A-5 扩展（P0-3a，2026-10-19 外脑裁决）：交付正文身份校验（三哈希绑定）。

    reviewed_hash（审稿通过正文）/ final_hash（finalize 冻结候选正文）由
    nww_review_cycle._write_review_meta 在 phase_finalize 落盘时写入（SHA-256，
    原始文件字节，无任何换行/编码规范化）；本函数对实际待提交正文现算 commit_hash。
    三者一致 ⇔ 「审的 = 冻结的 = 交的」。

    启用边界（存量兼容，零行为变化）：quality_meta.reviewed=true 且至少带一个哈希字段
    （即本章确实走过新 finalize 冻结路径）才校验。存量书 / 未走 finalize 的路径无哈希
    字段 → 返回 None，维持原 A-5 布尔校验语义。哈希字段只能由真实 finalize 路径产生，
    调用方无法伪造「已验收」——伪造需同时伪造与正文字节一致的哈希，等价于真的重新验收。

    返回违规说明（None=通过）。违规一律确定性 BLOCK（无 bible 门控）：
    哈希字段的存在本身就证明本章走了新流程，阻断不影响任何存量行为。
    """
    if not quality_meta.get("reviewed"):
        return None
    reviewed_hash = quality_meta.get("reviewed_hash")
    final_hash = quality_meta.get("final_hash")
    if not reviewed_hash and not final_hash:
        return None  # 存量/未冻结路径：无绑定可校验，交回原 A-5 布尔校验
    chapter = transaction.get("chapter")
    try:
        prose = resolve_chapter_file(project, int(chapter))
    except Exception:
        prose = None
    if prose is None or not prose.is_file():
        return (f"交付正文身份校验失败：无法定位第 {chapter} 章正文以计算 commit_hash"
                f"（reviewed_hash={reviewed_hash}，final_hash={final_hash}）")
    commit_hash = hashlib.sha256(prose.read_bytes()).hexdigest()
    if reviewed_hash and final_hash and reviewed_hash != final_hash:
        return (f"冻结候选正文与审稿通过正文不一致（reviewed_hash≠final_hash，审X冻结Y）："
                f"reviewed={str(reviewed_hash)[:12]}… final={str(final_hash)[:12]}… "
                f"commit={commit_hash[:12]}… 正文={prose}")
    bound = final_hash or reviewed_hash
    if commit_hash != bound:
        return (f"finalize 冻结后正文已被改写且未重新验收（审X交付Y）："
                f"reviewed={str(reviewed_hash)[:12]}… final={str(final_hash)[:12]}… "
                f"commit={commit_hash[:12]}… 正文={prose}。"
                f"修复：对第 {chapter} 章重新执行审稿闭环 finalize（重绑冻结哈希）后再 commit")
    return None


def _verify_review_score_gate(project: Path, chapter: int,
                              quality_meta: dict[str, Any]) -> str | None:
    """A-5 扩展（P0-3b，2026-10-19 外脑裁决）：审稿评分独立门——低分章/未收口章不得提交。

    V5 实证缺口：finalize 拒绝低分（rc=2）后调用方继续 commit，低分章进入权威状态；
    Web 路径 R-QC1 还会给未过线章写 reviewed=true。本门在 commit 侧独立复核评分记录，
    不依赖上游返回码。

    启用边界（存量兼容，零行为变化）：追踪/评分/第NNN章.json **存在**（本章确实走过
    审稿闭环）才校验；完全无评分记录（存量书/未接审稿路径）→ None，维持原 A-5 布尔语义。
    校验规则（fail-closed）：
      ① 评分文件存在但不可解析 → BLOCK（评分文件由系统写入，损坏=状态不可信）；
      ② 历史最高分 < PASS_SCORE → BLOCK（评分不达标）；
      ③ 最高分达标但 best 轮任一维度 < DIMENSION_FLOOR → BLOCK（维度地板=硬约束，
         外脑 2026-10-19 边界项3：「高分但硬约束失败」不得作为已验收版本提交；
         best 轮无 scores 明细的存量旧格式无法判定，维持旧行为兼容）；
      ④ 最高分达标但 quality_meta.reviewed != true → BLOCK（finalize 未成功收口，
         含「多轮混存时旧 run 高分掩盖新 run 低分」——此时 reviewed 必缺，④兜底拦截）。
    """
    ch = int(chapter)
    score_file = project / "追踪" / "评分" / f"第{ch:03d}章.json"
    if not score_file.is_file():
        return None  # 无评分记录：存量路径，维持原 A-5 布尔校验语义
    pass_score = _review_pass_score()
    try:
        data = json.loads(score_file.read_text(encoding="utf-8"))
        rounds = data.get("rounds") if isinstance(data, dict) else data
        if not isinstance(rounds, list):
            raise ValueError("rounds 不是数组")
        best = max((float(r.get("total", 0)) for r in rounds if isinstance(r, dict)), default=0.0)
    except Exception as exc:  # noqa: BLE001 — fail-closed：损坏的评分记录不可放行
        return (f"评分记录存在但无法解析（{score_file.name}: {exc}）——审稿状态不可信，"
                f"请修复评分文件或重跑审稿闭环")
    if best < pass_score:
        return (f"审稿评分不达标：历史最高分 {best:g} < {pass_score}"
                f"（{score_file.name}）。低分章不得交付，请按审稿闭环修订并重新 finalize")
    # 维度地板=硬约束（外脑 2026-10-19 边界项3）：分数达标但 best 轮任一维度 < DIMENSION_FLOOR
    # → 「高分但硬约束失败」→ 不得作为已验收版本提交（不被 best >= 85 覆盖）。
    # best 轮无 scores 明细（存量旧格式）→ 无法判定，维持旧行为（兼容）。
    _gate_best = max((r for r in rounds if isinstance(r, dict)),
                     key=lambda r: float(r.get("total", 0)), default=None)
    if _gate_best is not None:
        _scores = _gate_best.get("scores")
        if isinstance(_scores, dict):
            floor = _review_dimension_floor()
            _bad = sorted(f"{k}={v:g}" for k, v in _scores.items()
                          if isinstance(v, (int, float)) and not isinstance(v, bool) and v < floor)
            if _bad:
                return (f"审稿维度地板未达标（硬约束）：{', '.join(_bad)} < {floor}"
                        f"（{score_file.name}）。分数达标不等于硬约束通过，"
                        f"请按审稿闭环修订触底维度并重新 finalize")
    if not quality_meta.get("reviewed"):
        return ("评分记录存在（最高分达标）但 delta.quality_meta.reviewed != true"
                "——finalize 未成功收口（未通过审稿闭环落盘），请重跑审稿闭环 finalize")
    return None


def verify_post_write_meta(project: Path, transaction: dict[str, Any]) -> None:
    """A-5：校验 delta.quality_meta.enforced / reviewed，规避选择性执行。"""
    if transaction.get("mode") != "append":
        return  # revision 模式豁免
    quality_meta = (transaction.get("delta") or {}).get("quality_meta") or {}
    missing: list[str] = []
    if not quality_meta.get("enforced"):
        missing.append("enforced（未经过 nww_enforce.py 机械层）")
    if not quality_meta.get("reviewed"):
        missing.append("reviewed（未经过审稿闭环 nww_review_cycle.py）")
    # P0-3a：交付正文身份校验（独立于布尔缺失校验；违规确定性 BLOCK）
    hash_violation = _verify_frozen_body_hash(project, transaction, quality_meta)
    if hash_violation is not None:
        # 冻结正文在 finalize 后被改写且未重新验收 → 验收状态失效，拒绝提交。
        _gate_failure_exit(
            project, int(transaction["chapter"]), "A-5/body_hash",
            f"[A-5 BLOCK] 交付正文身份校验失败：{hash_violation}",
        )
        return
    # P0-3b：审稿评分独立门（低分/未收口/损坏记录 → 确定性 BLOCK）
    score_violation = _verify_review_score_gate(project, int(transaction["chapter"]), quality_meta)
    if score_violation is not None:
        _gate_failure_exit(
            project, int(transaction["chapter"]), "A-5/review_score",
            f"[A-5 BLOCK] 审稿评分门未达门槛：{score_violation}",
        )
        return
    if not missing:
        return
    msg = (
        "A-5 流程完整性：本章 delta.quality_meta 缺少 "
        + "、".join(missing)
        + "。宿主 LLM 可能跳过了 post_write 封装的确定性步骤。"
        + "修复：调用 nww_post_write.py --project <项目> --chapter <章号> 走完整流程。"
    )
    if _bible_meta_check_gate(project):
        _gate_failure_exit(project, int(transaction["chapter"]), "A-5/post_write_meta", f"[A-5 BLOCK] {msg}")
        return
    emit(f"[A-5 WARNING] {msg}（当前为兼容模式不阻断；确认流程稳定后在 bible.json 设 commit_gates.meta_check=true 启用硬闸）", error=True)


# ---------- A-8 跨章复读硬闸（2026-11-05，L1-6 升级 advisory→active） ----------
# 把 nww_cross_chapter_check.run_check() 从 commit 后 advisory 前移为 commit 前硬闸。
# 门控与 A-3/A-4 同一兼容模式语义：bible.commit_gates.cross_chapter=true → BLOCK；
# 缺失/false（存量书）→ 仅 WARNING 不阻断。
def _bible_cross_chapter_gate(project: Path) -> bool:
    """读 PolicyBundle：cross_chapter；缺字段按 policy.fail_closed（默认 True）。"""
    return load_policy(project).effective("cross_chapter")


def _verbatim_repeat_sentences(project: Path, chapter: int,
                               window: int = _CROSS_CHAPTER_WINDOW) -> list[str]:
    """当前章与近 window 章的逐字复读句（C-REPEAT@1，与 guard 同切句同阈值）。"""
    try:
        from nww_cross_chapter_check import chapter_files, read_text
    except ImportError:
        return []
    files = chapter_files(project)
    target = next((p for num, p in files if num == chapter), None)
    if target is None:
        return []
    recent = [p for num, p in files if num < chapter][-window:]
    chapters = [(p.stem, read_text(p)) for p in recent]
    chapters.append((target.stem, read_text(target)))
    current_idx = len(chapters) - 1
    try:
        repeats = _shared_cross_chapter_repeats(chapters)
    except Exception:  # noqa: BLE001 — 复读判定异常不得阻断提交
        return []
    return [sentence for sentence, idxs in repeats.items() if current_idx in idxs]


def verify_cross_chapter_repeats(project: Path, chapter: int, mode: str) -> None:
    """A-8：commit 前跑跨章复读检测，命中硬阈值则拒绝提交。

    检测项（来自 nww_cross_chapter_check.run_check）：
    - 跨章重复：≥3 个 6 字短语在近 5 章也出现
    - 段落密度漂移：平均段长缩水 >45% 且短段占比抬升 >25%
    - 标题疲劳：近 5 章标题反复撞同一 token（≥3 章）

    另加逐字复读：≥20 字整句在近 5 章与本章完全相同（判定来自 nww_state_checks，
    与 guard.py 同一组切句规则与阈值）。
    """
    if mode != "append":
        return  # revision 模式豁免
    try:
        from nww_cross_chapter_check import run_check
    except ImportError:
        return  # 脚本未部署时静默跳过
    result = run_check(project, chapter)
    findings = result.get("findings") or []
    verbatim = _verbatim_repeat_sentences(project, chapter)
    if not findings and not verbatim:
        return
    gate = _bible_cross_chapter_gate(project)
    details_parts = [f"{f['rule']}：{f['description']}" for f in findings]
    if verbatim:
        sample = "、".join(f"「{sentence[:30]}」" for sentence in verbatim[:2])
        details_parts.append(f"逐字复读：{len(verbatim)} 句与近章完全相同（示例：{sample}）")
    details = "；".join(details_parts)
    if gate:
        _gate_failure_exit(
            project, chapter, "A-8/cross_chapter_repeats",
            f"[A-8 BLOCK] Chapter {chapter} 跨章质量检测未达门槛：{details}"
            "（commit_gates.cross_chapter=true）。变换重复用语、恢复段落层次、轮换标题类型后重写再提交。",
        )
        return
    emit(f"[A-8 WARNING] Chapter {chapter} 跨章检测发现：{details}（兼容模式不阻断）", error=True)


# ---------- A-9 伏笔逾期硬闸（2026-11-05，L1-5 升级 advisory→active） ----------
# core_hook=true 的已埋伏笔超过 half_life_chapters 未回收 → WARNING/BLOCK。
# 门控与 A-3/A-4 同一兼容模式语义。
def _bible_foreshadow_gate(project: Path) -> bool:
    """读 PolicyBundle：foreshadow_overdue；缺字段按 policy.fail_closed（默认 True）。"""
    return load_policy(project).effective("foreshadow_overdue")


def verify_foreshadow_overdue(state: dict[str, Any], chapter: int, mode: str,
                              project: Path) -> None:
    """A-9：检查 core_hook 伏笔是否超过 half_life 未回收。

    2026-11-05 根因修复：门控必须读**书项目**的 policy，不能读 Path.cwd()。
    toolbox.run_script 的子进程 cwd 是 skills/.../scripts（无 bible.json），
    旧实现会命中 load_policy 的 fail_closed 缺省 True，使作者在 bible 里
    显式关闭 foreshadow_overdue 也无效（A-8 一直正确传 project）。
    """
    if mode != "append":
        return
    foreshadow = state.get("foreshadow")
    if not isinstance(foreshadow, dict):
        return
    overdue: list[str] = []
    for fid, fdata in foreshadow.items():
        if not isinstance(fdata, dict):
            continue
        if not fdata.get("core_hook"):
            continue
        if fdata.get("status") != "已埋":
            continue
        half_life = fdata.get("half_life_chapters")
        if not half_life:
            continue
        planted = int(fdata.get("planted_chapter") or 0)
        if chapter - planted >= int(half_life):
            summary = str(fdata.get("summary") or fid)[:40]
            overdue.append(f"{fid}(第{planted}章埋,已过{chapter - planted}章/{half_life}章)「{summary}」")
    if not overdue:
        return
    gate = _bible_foreshadow_gate(project)
    details = "；".join(overdue)
    if gate:
        _gate_failure_exit(
            project, chapter, "A-9/foreshadow_overdue",
            f"[A-9 BLOCK] {len(overdue)} 个核心伏笔超过半衰期未回收：{details}"
            "（commit_gates.foreshadow_overdue=true）。在本章回收或推进这些伏笔，或登记 override。",
        )
        return
    emit(f"[A-9 WARNING] 伏笔逾期：{details}（兼容模式不阻断）", error=True)


# ---------- A-5b 结构指纹登记闸（2026-09-09，trope_guard 闭环配套） ----------
# 背景：nww_trope_guard.py 的 CD 冷却 / 变奏 / SimHash 碰撞检查全部依赖 registry 历史，
# 而 register 此前无任何自动调用方 → 追踪/trope_registry/ 恒空 → check 恒 PASS（1313 行空转）。
# 实测：全库 trope_registry 目录数 = 0，10+ 本书无一产生过指纹库。
# 修复：commit 时确定性自动登记。零侵入保持——细纲无 structural_fingerprint 时
# trope_guard 返回 SKIPPED（退出码 0），本闸 no-op，存量书零影响。

TROPE_GUARD_SCRIPT = Path(__file__).resolve().parent / "nww_trope_guard.py"


def register_trope_fingerprint(project: Path, chapter: int, mode: str) -> None:
    """A-5b：commit 时确定性登记本章结构指纹，填充 trope_guard 的历史库。"""
    if mode != "append":
        return  # revision 模式豁免（与 A-5 一致）
    if not TROPE_GUARD_SCRIPT.exists():
        return  # 未随 skill 部署时静默跳过
    try:
        proc = subprocess.run(
            [
                sys.executable, str(TROPE_GUARD_SCRIPT), "register",
                "--chapter", str(chapter),
                "--project", str(project),
                "--json",
            ],
            capture_output=True, text=True, timeout=60,
        )
    except Exception:
        # 脚本异常不得阻断 commit——指纹是质量增强项，不是事务正确性前提
        return
    # 注意：trope_guard 的 check 与 register 对 SKIPPED 的退出码语义不一致——
    # check 把 SKIPPED 当 PASS（返回 0），register 把 SKIPPED 当失败（返回 1）。
    # 因此不能直接看 returncode，必须解析 JSON verdict，否则存量书每次 commit 都会刷假 WARNING。
    verdict = ""
    try:
        verdict = str((json.loads(proc.stdout or "{}")).get("verdict", "")).upper()
    except Exception:
        verdict = ""
    if verdict == "PASS":
        return
    if verdict == "SKIPPED" or not verdict:
        return  # 无 structural_fingerprint 的存量细纲：零侵入，静默跳过
    msg = (
        f"A-5b 结构指纹登记未通过（第 {chapter} 章）："
        f"{(proc.stdout or proc.stderr or '').strip()[:400]}"
        "。修复：按 nww_trope_guard.py check 输出的 suggestions 调整细纲 "
        "structural_fingerprint 维度后重跑。"
    )
    if _bible_meta_check_gate(project):
        raise TrackingError(f"[A-5b BLOCK] {msg}")
    emit(f"[A-5b WARNING] {msg}（当前为兼容模式不阻断）", error=True)


def check_project(project: Path) -> dict[str, Any]:
    tracking = tracking_root(project)
    require_no_retired_tracking_paths(tracking)
    # P2 修复（2026-10-20）：失效标记存在 = 此前视图重建失败，磁盘视图不保证与权威状态
    # 一致 → 显式报「视图失效」并指引恢复，而不是假装跑完逐文件比对（防伪装一致）。
    stale_marker = views_stale_marker(tracking)
    if stale_marker.is_file():
        # 外脑裁决：优先报失效标记，并尽可能保留可诊断信息（标记内容含 stale_since_revision 等）
        try:
            marker_detail = stale_marker.read_text(encoding="utf-8")[:400]
        except OSError:
            marker_detail = "<标记内容不可读>"
        raise TrackingError(
            "[VIEWS STALE] 派生视图失效（此前视图重建失败或未收口，视图内容不保证与权威状态一致）: "
            f"{stale_marker}；标记内容: {marker_detail}；"
            "恢复：重跑同一 R1 修复，或执行下一次正式 commit 成功重写视图后重试 check"
        )
    state = load_state(project)
    last_chapter = state["last_committed_chapter"]
    required_delta_start = state["imported_through_chapter"] + 1
    for chapter in range(required_delta_start, last_chapter + 1):
        require(delta_path(tracking, chapter).exists(), f"chapter delta {chapter} is missing")
    for path in (tracking / "逐章记录").glob("第*章.md"):
        match = re.fullmatch(r"第(\d+)章\.md", path.name)
        require(match is not None, f"chapter delta has an invalid filename: {path.name}")
        chapter = as_int(int(match.group(1)), f"chapter delta {path.name}", minimum=1)
        require(path == delta_path(tracking, chapter), f"chapter delta {chapter} filename is not canonical")
        require(chapter <= last_chapter, f"chapter delta {chapter} exceeds last_committed_chapter")
        require(path.stat().st_size <= DELTA_MAX_BYTES, f"chapter delta {chapter} exceeds {DELTA_MAX_BYTES} bytes")

    for line in stale_core_hook_lines(state["foreshadow"], last_chapter):
        emit(f"[FORESHADOW DEBT WARNING] {line}", error=True)
    for line in orphan_hook_reference_lines(state["foreshadow"]):
        emit(f"[FORESHADOW REF NOTE] {line}", error=True)
    expected_views = render_views(state)
    for relative, expected in expected_views.items():
        path = tracking / relative
        require(path.exists(), f"derived view is missing: {relative}")
        require(
            path.read_text(encoding="utf-8") == expected,
            f"derived view differs from _tracking-state.json: {relative}",
        )
    expected_character_files = {
        Path(relative).name for relative in expected_views if relative.startswith("角色状态/")
    }
    actual_character_files = {path.name for path in (tracking / "角色状态").glob("*.md")}
    require(actual_character_files == expected_character_files, "character snapshot files differ from tracking state")
    for line in quality_trend_lines(tracking, last_chapter):
        emit(line)
    for line in chapter_type_streak_lines(state):
        emit(line)
    return state


# ---------- 评分结构化落盘（2026-09-05，质量趋势数据地基） ----------
# 设计：
#  - 评分由 quality-evaluator 产出后用 `score` 子命令落盘为 追踪/评分/第N章.json，
#    让 check 能输出逐章质量趋势（均分/最低分/低于85计数），支撑 Phase 4.6 修复环
#    和卷级疲劳分析的数据地基。
#  - 评分是外部记录（非派生视图），不参与 render_views 一致性校验；损坏文件只
#    NOTE 跳过，不阻断 check（fail-open 与快照同一原则）。
#  - rollback 的快照差集清理（cur_files - target_files）天然覆盖 追踪/评分/，
#    回滚后未来章的评分文件会被一并清掉，无需额外逻辑。
SCORE_MIN = 0
SCORE_MAX = 100
PASS_SCORE = 85
SCORE_ISSUE_MAX = 20
SCORE_ISSUE_BYTES = 256


def _as_score(value: object, label: str) -> float:
    """分数允许 int 或 float，范围 [0, 100]。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TrackingError(f"{label} must be a number between {SCORE_MIN} and {SCORE_MAX}")
    score = float(value)
    if not (SCORE_MIN <= score <= SCORE_MAX):
        raise TrackingError(f"{label} must be between {SCORE_MIN} and {SCORE_MAX}, got {score}")
    return score


def score_project(project: Path, document: object) -> dict[str, Any]:
    """校验并落盘一章的结构化评分：追踪/评分/第{N}章.json。"""
    state = load_state(project)
    tracking = tracking_root(project)
    record = as_mapping(document, "score input")
    require_known_keys(record, {"chapter", "score", "issues", "rounds"}, "score input")
    chapter = as_int(record.get("chapter"), "score input.chapter", minimum=1)
    last_chapter = state["last_committed_chapter"]
    require(
        chapter <= last_chapter,
        f"score chapter {chapter} exceeds last_committed_chapter {last_chapter}（评分只针对已提交章节）",
    )
    score = _as_score(record.get("score"), "score input.score")
    issues_raw = record.get("issues", [])
    issues: list[str] = []
    if issues_raw is not None:
        for index, item in enumerate(as_list(issues_raw, "score input.issues")):
            issues.append(clean_text(item, f"score input.issues[{index}]", max_bytes=SCORE_ISSUE_BYTES))
        require(
            len(issues) <= SCORE_ISSUE_MAX,
            f"score input.issues exceeds {SCORE_ISSUE_MAX} items",
        )
    rounds_raw = record.get("rounds", 0)
    rounds = as_int(rounds_raw if rounds_raw is not None else 0, "score input.rounds", minimum=0)
    require(rounds <= 10, "score input.rounds must be <= 10")

    payload = json_payload(
        {
            "chapter": chapter,
            "score": round(score, 1),
            "issues": issues,
            "rounds": rounds,
        }
    )
    target = tracking / "评分" / f"第{chapter}章.json"
    atomic_write_text(target, payload)
    emit(
        json.dumps(
            {
                "scored_chapter": chapter,
                "score": round(score, 1),
                "path": str(target),
            },
            ensure_ascii=False,
        )
    )
    return state


def quality_trend_lines(tracking: Path, last_chapter: int) -> list[str]:
    """最近 10 章评分趋势：均分/最低分/低于85计数。fail-open：坏文件只 NOTE。"""
    score_dir = tracking / "评分"
    if not score_dir.is_dir():
        return []
    records: list[tuple[int, dict[str, Any]]] = []
    for path in score_dir.glob("第*章.json"):
        match = re.fullmatch(r"第(\d+)章\.json", path.name)
        if match is None:
            emit(f"NOTE: 评分目录存在非规范文件名，已跳过：{path.name}", error=True)
            continue
        chapter = int(match.group(1))
        if chapter > last_chapter:
            emit(f"NOTE: 评分文件超出 last_committed_chapter，已跳过：{path.name}", error=True)
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            record = as_mapping(document, f"score record {path.name}")
            score = _as_score(record.get("score"), f"score record {path.name}.score")
        except (TrackingError, OSError, UnicodeError, json.JSONDecodeError) as exc:
            emit(f"NOTE: 评分文件损坏已跳过：{path.name}（{exc}）", error=True)
            continue
        records.append((chapter, score))
    if not records:
        return []
    records.sort(key=lambda pair: pair[0])
    window = records[-10:]
    scores = [score for _, score in window]
    average = sum(scores) / len(scores)
    minimum = min(scores)
    below = sum(1 for score in scores if score < PASS_SCORE)
    summary = {
        "quality_trend": {
            "window": [window[0][0], window[-1][0]],
            "count": len(scores),
            "average": round(average, 1),
            "min": round(minimum, 1),
            "below_pass": below,
            "pass_score": PASS_SCORE,
        }
    }
    lines = [json.dumps(summary, ensure_ascii=False)]
    if below >= 4:
        lines.append(
            f"[QUALITY WARNING] 最近 {len(scores)} 章有 {below} 章低于 {PASS_SCORE} 分，"
            "建议回到 Phase 4.6 修复环或检查 chapter_type 连同型疲劳"
        )
    return lines


CHAPTER_TYPE_STREAK_WARN = 4


def chapter_type_streak_lines(state: dict[str, Any]) -> list[str]:
    """尾部同型章节连续 ≥4 → 卷级疲劳 advisory（数据来自 delta.chapter_type 登记表）。"""
    last_chapter = state["last_committed_chapter"]
    types = state.get("chapter_types", {})
    streak_type: str | None = None
    streak = 0
    for chapter in range(last_chapter, 0, -1):
        chapter_type = types.get(str(chapter))
        if chapter_type is None:
            break  # 未登记类型的章节打断统计（不猜测）
        if streak_type is None:
            streak_type = chapter_type
            streak = 1
        elif chapter_type == streak_type:
            streak += 1
        else:
            break
    if streak_type is None or streak < CHAPTER_TYPE_STREAK_WARN:
        return []
    return [
        json.dumps(
            {
                "chapter_type_streak": {
                    "type": streak_type,
                    "streak": streak,
                    "window": [last_chapter - streak + 1, last_chapter],
                    "warn_at": CHAPTER_TYPE_STREAK_WARN,
                }
            },
            ensure_ascii=False,
        ),
        f"[FATIGUE WARNING] 最近 {streak} 章连续为「{streak_type}」型（≥{CHAPTER_TYPE_STREAK_WARN}），"
        "同型疲劳风险：下一章建议切换章节类型（如 战斗→日常/交易/调查）",
    ]


# ---------- 每章快照 + 一键回滚（2026-09-05，git 化 O(Δ) 方案） ----------
# 设计：
#  - 每本书独立影子仓库 {BOOK}/.snapshots（--git-dir），--work-tree 指向书根：
#    不污染上层主仓库（projects/ 被主仓库有意忽略）；每章 append 成功后 add+commit
#    追踪/ 全量，git 自行压缩，存储 O(Δ) 而非逐章 copytree 的 O(n²)。
#  - 快照是纯增益：git 缺失/初始化失败 → NOTE 后跳过，绝不阻断写作主流程（fail-open）。
#  - rollback 恢复 追踪/ 到目标章快照 + 把 >N 的正文/逐章记录移入
#    {BOOK}/_archive/rollback_第N章后/（移动不删除，随时可人工找回）。
_SNAPSHOT_GIT_DIR = ".snapshots"
_SNAPSHOT_MSG_TAG = "[nww-snapshot]"


def _git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str] | None:
    git = shutil.which("git")
    if git is None:
        return None
    try:
        return subprocess.run(
            [git, *args], cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _snapshot_args(project: Path) -> list[str]:
    return [
        "--git-dir", str(project / _SNAPSHOT_GIT_DIR),
        "--work-tree", str(project),
    ]


def _foundation_snapshot(project: Path, chapter: int, state_payload: str) -> None:
    """K3: 每次 commit 后保存完整状态快照到 追踪/快照/第{NNN}章.json。

    与影子 git 互补：git 存增量 diff，foundation snapshot 存完整 JSON，
    支持任意章节回溯（无需 git checkout）。fail-open，失败只 NOTE 不阻断。
    """
    try:
        snap_dir = project / "追踪" / "快照"
        snap_dir.mkdir(parents=True, exist_ok=True)
        snap_file = snap_dir / f"第{chapter:03d}章.json"
        atomic_write_text(snap_file, state_payload)
    except Exception as exc:  # noqa: BLE001
        emit(f"NOTE: foundation snapshot 写入失败 ch{chapter}: {exc}", error=True)


def snapshot_book_state(project: Path, chapter: int, revision: int, mode: str) -> None:
    """append 提交成功后对 追踪/ 做影子 git 快照；任何失败只 NOTE 不阻断。"""
    if mode != "append":
        return
    base = _snapshot_args(project)
    gitdir = project / _SNAPSHOT_GIT_DIR
    if not gitdir.is_dir():
        init = _git(["init", "--bare", "--quiet", str(gitdir)])
        if init is None or init.returncode != 0:
            emit("NOTE: 影子快照仓库初始化失败（git 缺失或不可写），本章跳过快照", error=True)
            return
    add = _git([*base, "add", "--", "追踪"], cwd=project)
    if add is None or add.returncode != 0:
        emit(f"NOTE: 快照 add 失败，跳过本章快照：{(add.stderr if add else '')[:160]}", error=True)
        return
    commit = _git([
        *base, "-c", "user.name=nww-snapshot", "-c", "user.email=nww@local",
        "commit", "--quiet", "-m", f"{_SNAPSHOT_MSG_TAG} chapter={chapter} revision={revision}",
    ])
    if commit is not None and commit.returncode != 0:
        combined = ((commit.stdout or "") + (commit.stderr or "")).strip()
        if "nothing to commit" not in combined:
            emit(f"NOTE: 快照 commit 失败（不影响本次提交）：{combined[:160]}", error=True)


def _chapter_file_number(name: str) -> int | None:
    m = re.match(r"第0*(\d+)章", name)
    return int(m.group(1)) if m else None


def rollback_project(project: Path, chapter: int) -> dict[str, Any]:
    """把 追踪/ 恢复到第 chapter 章快照，并把 >chapter 的正文/逐章记录移入归档。

    F6（2026-10-20 外脑裁决：有条件批准）：
    - 全程持 project_write_lock（与 commit / story-event-confirm 互斥，防并发读写交错）；
    - checkout 前预检归档冲突：dest 已存在时拒绝（shutil.move 遇 dest 已存在会静默覆盖，
      POSIX os.rename 语义，丢失历史归档证据）；
    - checkout 后失败显式报告已完成/未完成步骤与恢复路径（同一命令重跑幂等续行）。
    """
    require(state_path(project).exists(), "tracking state missing; nothing to roll back")
    with project_write_lock(project):
        return _rollback_project_locked(project, chapter)


def _rollback_project_locked(project: Path, chapter: int) -> dict[str, Any]:
    """F6: rollback 主体（调用方必须已持 project_write_lock，锁不可重入）。"""
    tracking = tracking_root(project)
    base = _snapshot_args(project)
    gitdir = project / _SNAPSHOT_GIT_DIR
    require(gitdir.is_dir(), "snapshot repo missing（.snapshots/）; 无法回滚")

    log = _git([*base, "log", "--format=%H%x09%s"], cwd=project)
    require(log is not None and log.returncode == 0, f"snapshot log failed: {(log.stderr if log else '')[:160]}")
    picks = [
        line for line in (log.stdout or "").splitlines()
        if f"{_SNAPSHOT_MSG_TAG} chapter={chapter} " in line
    ]
    require(picks, f"no snapshot for chapter {chapter}; 无法回滚到该章")
    sha, subject = picks[0].split("\t", 1)

    # F6: checkout 前预检 —— 待归档文件的 dest 冲突检测。
    # 只检「本次待移动集合」：重跑补齐时已归档文件不在工作树、不在集合内 → 不误报，幂等可续行。
    archive = project / "_archive" / f"rollback_第{chapter:03d}章后"
    body = project / "正文"
    pending_prose: list[tuple[Path, Path]] = []
    if body.is_dir():
        for p in sorted(body.rglob("第*章*.md")):
            if _is_non_prose(p):
                continue
            num = _chapter_file_number(p.name)
            if num is None or num <= chapter:
                continue
            pending_prose.append((p, archive / "正文" / p.relative_to(body)))
    pending_delta: list[tuple[Path, Path]] = []
    conflicts = [str(dest) for _, dest in pending_prose if dest.exists()]
    require(
        not conflicts,
        f"archive conflict: 归档目标已存在（此前可能回滚过同名章节，覆盖将丢失归档证据）: "
        f"{'; '.join(conflicts[:4])}；请人工核对 _archive/ 后重试。追踪/ 与正文尚未被任何修改",
    )

    done: list[str] = []
    # 恢复快照中存在的 追踪/ 文件
    co = _git([*base, "checkout", sha, "--", "追踪"], cwd=project)
    require(
        co is not None and co.returncode == 0,
        f"snapshot checkout failed: {((co.stderr if co else '') or '')[:200]}"
        "；若 追踪/ 已被部分覆盖，重跑同一 rollback 命令即可恢复到目标快照（幂等）",
    )
    done.append(f"checkout→第{chapter:03d}章快照")

    # 清掉快照之后新增、但目标快照里不存在的 追踪/ 文件（如后章角色卡/评分）。
    # 差集清单排除 逐章记录/（其归档语义见下方 move 批次：移动不删除，随时可人工找回）。
    # F6 修复存量缺陷：git ls-files 默认 core.quotepath=true 会把中文路径输出为带引号的
    # 八进制转义，原实现拿该转义串拼路径 unlink 全部 FileNotFoundError 且被静默 pass 吞掉
    # （差集清理从未对中文路径生效）→ 加 -c core.quotepath=false 使路径真实可解析。
    # F6: 清理失败从静默 pass 改为显式 warnings（保持 fail-open 不阻断；重跑差集可重算补清）。
    cur = _git(["-c", "core.quotepath=false", *base, "ls-files", "--", "追踪"], cwd=project)
    target = _git(["-c", "core.quotepath=false", *base, "ls-tree", "-r", "--name-only", sha, "--", "追踪"], cwd=project)
    cur_files = set((cur.stdout or "").splitlines()) if cur and cur.returncode == 0 else set()
    target_files = set((target.stdout or "").splitlines()) if target and target.returncode == 0 else set()
    removed_extra = 0
    warnings: list[str] = []
    for rel in sorted(cur_files - target_files):
        if rel.startswith("逐章记录/"):
            continue
        p = project / rel
        try:
            p.unlink()
            removed_extra += 1
        except FileNotFoundError:
            # 幂等重跑：index 保留条目（checkout 不删多余条目）但文件已在上一轮清理删除
            # → 目标状态已达成，计成功而非误报「清理失败」
            removed_extra += 1
        except OSError as exc:
            warnings.append(f"追踪差集文件清理失败（已保留原文件，重跑同一 rollback 命令可补清）: {rel}: {exc}")
    if removed_extra or warnings:
        done.append(f"追踪差集清理 ok={removed_extra} failed={len(warnings)}")

    # F6: delta 待归档集合在差集清理后收集（差集清理已排除 逐章记录/，其全部经下方
    # move 批次归档——「移动不删除」语义与设计注释一致）；dest 冲突在任何 move 之前拒绝，
    # 此时 checkout 可能已发生 → 按 F6 残余边界 N4 记录：delta 冲突非 checkout 前拒绝。
    delta_dir = tracking / "逐章记录"
    if delta_dir.is_dir():
        for p in sorted(delta_dir.glob("第*章.md")):
            num = _chapter_file_number(p.name)
            if num is None or num <= chapter:
                continue
            pending_delta.append((p, archive / "逐章记录" / p.name))
    delta_conflicts = [str(dest) for _, dest in pending_delta if dest.exists()]
    require(
        not delta_conflicts,
        f"archive conflict: 归档目标已存在: {'; '.join(delta_conflicts[:4])}；"
        f"已完成: {'; '.join(done) or '无'}；恢复路径: 重跑同一 rollback 命令幂等续行",
    )

    # >chapter 的正文与逐章记录移入归档（移动不删除；正文 dest 冲突已在 checkout 前预检，
    # delta dest 冲突已在上方任何 move 之前拒绝）。
    # F6: move 失败不再裸抛 OSError，改为显式报告已完成/未完成步骤 + 恢复路径。
    def _move_batch(pairs: list[tuple[Path, Path]], label: str) -> int:
        moved = 0
        for src, dest in pairs:
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dest))
            except OSError as exc:
                completed = "; ".join(done) if done else "无"
                if moved:
                    completed += f"；{label}已移{moved}项"
                raise TrackingError(
                    f"rollback 部分完成: {label} 于 {src} → {dest} 失败: {exc}；"
                    f"已完成: {completed}；未完成: {label} 剩余 {len(pairs) - moved} 项"
                    f"（已完成项已入归档、未完成项原地保留，均无丢失）；"
                    f"恢复路径: 重跑同一 rollback 命令幂等续行（已归档项不冲突）"
                ) from exc
            moved += 1
        return moved

    moved_prose = _move_batch(pending_prose, "正文归档")
    if moved_prose:
        done.append(f"正文归档×{moved_prose}")
    moved_delta = _move_batch(pending_delta, "逐章记录归档")
    if moved_delta:
        done.append(f"逐章记录归档×{moved_delta}")

    restored = load_state(project)
    require(
        restored["last_committed_chapter"] == chapter,
        f"restored state last_committed_chapter={restored['last_committed_chapter']} != {chapter}; snapshot integrity check failed",
    )
    emit(json.dumps({
        "rolled_back_to": chapter,
        "snapshot": subject,
        "state_revision": restored["state_revision"],
        "archived_prose": moved_prose,
        "archived_deltas": moved_delta,
        "removed_extra_tracking_files": removed_extra,
        "archive_dir": str(archive),
        "warnings": warnings,
    }, ensure_ascii=False))
    return restored


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("init",):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--project", type=Path, required=True, help="book project root containing 追踪/")
        subparser.add_argument("--input", type=Path, required=True, help="UTF-8 JSON input document")
    commit_parser = subparsers.add_parser("commit")
    commit_parser.add_argument("--project", type=Path, required=True, help="book project root containing 追踪/")
    commit_parser.add_argument("--input", type=Path, required=True, help="UTF-8 JSON input document")
    commit_parser.add_argument(
        "--quality-dir",
        default=None,
        help="REPO 根的 quality/ 目录（含 detect_internal.py）。给出时对目标正文跑 I-A~I-H 门禁："
        "I-G/I-H fail 拒绝 commit；warning 记录不阻断。缺省不跑（fail-open）。",
    )
    check_parser = subparsers.add_parser("check")
    check_parser.add_argument("--project", type=Path, required=True, help="book project root containing 追踪/")
    score_parser = subparsers.add_parser(
        "score", help="把一章的结构化评分落盘为 追踪/评分/第N章.json（check 时输出质量趋势）"
    )
    score_parser.add_argument("--project", type=Path, required=True, help="book project root containing 追踪/")
    score_parser.add_argument(
        "--input", type=Path, required=True,
        help='UTF-8 JSON：{"chapter": N, "score": 0-100, "issues": [...], "rounds": 0}',
    )
    rollback_parser = subparsers.add_parser(
        "rollback", help="把 追踪/ 回滚到第 N 章快照；>N 的正文/逐章记录移入 _archive/"
    )
    rollback_parser.add_argument("--project", type=Path, required=True, help="book project root containing 追踪/")
    rollback_parser.add_argument("--chapter", type=int, required=True, help="回滚目标章（该章必须已有快照）")
    confirm_parser = subparsers.add_parser(
        "story-event-confirm",
        help="P2 人工确认：SAME_EVENT → 归入既有 SE；DIFFERENT_EVENT → 新建 SE（status/decision 分离，含 provenance）",
    )
    confirm_parser.add_argument("--project", type=Path, required=True, help="book project root containing 追踪/")
    confirm_parser.add_argument("--candidate", required=True, help="候选 timeline E-ID（须 RESOLVED）")
    confirm_parser.add_argument(
        "--decision", choices=("SAME_EVENT", "DIFFERENT_EVENT"), required=True,
        help="SAME_EVENT=同一 story event；DIFFERENT_EVENT=不同 story event",
    )
    confirm_parser.add_argument("--target", default=None, help="SAME_EVENT 时目标 SE-ID")
    confirm_parser.add_argument("--by", default="human", help="确认人标识（默认 human）")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "init":
            result = initialize(args.project, read_json(args.input))
        elif args.command == "commit":
            result = apply_transaction(
                args.project, read_json(args.input), quality_dir=args.quality_dir
            )
        elif args.command == "rollback":
            result = rollback_project(args.project, args.chapter)
        elif args.command == "score":
            result = score_project(args.project, read_json(args.input))
        elif args.command == "story-event-confirm":
            result = story_event_confirm(
                args.project, args.candidate, args.decision, target=args.target, by=args.by
            )
        else:
            result = check_project(args.project)
    except (TrackingError, OSError, UnicodeError) as exc:
        emit(f"ERROR: {exc}", error=True)
        return 2
    emit(
        json.dumps(
            {
                "last_committed_chapter": result["last_committed_chapter"],
                "state_revision": result["state_revision"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
