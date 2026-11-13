#!/usr/bin/env python3
"""Small deterministic wordcount core shared by nww_ctl and tracking."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any


WORDCOUNT_SCHEMA = "story-wordcount-result/v1"
MEASUREMENT_SCHEMA = "story-wordcount-measurement/v1"
CHECKPOINT_SCHEMA = "story-wordcount-checkpoint/v1"
METRIC = "visible_chars_v1"
RESOLUTIONS = frozenset({"within_user_band", "accepted_current_length"})
UNLIMITED = 2**63 - 1

_WHITE_SPACE_CODEPOINTS = frozenset(
    [*range(0x0009, 0x000E)]
    + [
        0x0020, 0x0085, 0x00A0, 0x1680, *range(0x2000, 0x200B),
        0x2028, 0x2029, 0x202F, 0x205F, 0x3000,
    ]
)
_FRONTMATTER_KEY_RE = re.compile(r"^[A-Za-z_\u3400-\u9FFF][^:\n]{0,80}:[ \t]*(?:.*)$")
_LEADING_BLANK_RE = re.compile(r"^[\u0009\u0020\u3000]*$")
_ATX_HEADING_RE = re.compile(r"^[\u0009\u0020]{0,3}#{1,6}[\u0009\u0020]+\S")
_POSITIVE_INTEGER_RE = re.compile(r"^[1-9]\d*$")
_TARGET_LINE_RE = re.compile(r"^[ \t>*-]*字数目标[ \t]*[:：][ \t]*([1-9]\d*)[ \t]*(?:字)?[ \t]*$", re.MULTILINE)
_RANGE_TARGET_LINE_RE = re.compile(
    r"^[ \t>*-]*字数目标[ \t]*[:：][ \t]*([1-9]\d*)[ \t]*[-~～至到][ \t]*([1-9]\d*)[ \t]*(?:字)?[ \t]*$",
    re.MULTILINE,
)
_METRIC_LINE_RE = re.compile(r"^[ \t>*-]*字数口径[ \t]*[:：][ \t]*([A-Za-z0-9_-]+)[ \t]*$", re.MULTILINE)


class WordcountError(ValueError):
    """Expected deterministic wordcount contract failure."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise WordcountError(message)


def normalize_newlines(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def strip_recognizable_frontmatter(value: str) -> str:
    lines = value.split("\n")
    if not lines or lines[0] != "---":
        return value
    closing = next((index for index in range(1, min(len(lines), 201)) if lines[index] in {"---", "..."}), -1)
    if closing < 2 or not any(_FRONTMATTER_KEY_RE.match(line) for line in lines[1:closing]):
        return value
    return "\n".join(lines[closing + 1 :])


def visible_body(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("body must be a string")
    text = normalize_newlines(value)
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = strip_recognizable_frontmatter(text).split("\n")
    while lines and _LEADING_BLANK_RE.match(lines[0]):
        lines.pop(0)
    if lines and _ATX_HEADING_RE.match(lines[0]):
        lines.pop(0)
    return "\n".join(lines)


def count_visible_chars(value: str) -> int:
    return sum(ord(character) not in _WHITE_SPACE_CODEPOINTS for character in visible_body(value))


def parse_target(value: Any) -> int:
    raw = str(value if value is not None else "")
    if not _POSITIVE_INTEGER_RE.fullmatch(raw):
        raise WordcountError("target must be a positive integer")
    target = int(raw)
    require(target <= 9_007_199_254_740_991, "target exceeds Number.MAX_SAFE_INTEGER")
    return target


_RANGE_SPEC_RE = re.compile(r"^\s*([1-9]\d*)\s*[-~～至到]\s*([1-9]\d*)\s*$")


def parse_target_spec(value: Any) -> dict[str, Any]:
    """解析字数目标规格，支持单值('3500')与范围('4000-5000')。

    返回 dict: {min: int, max: int, raw: str, is_range: bool}
    单值时 max = UNLIMITED（上限放开）；范围时 max 为指定上限。
    """
    if isinstance(value, dict) and "min" in value and "max" in value:
        return value
    raw = str(value if value is not None else "").strip()
    range_match = _RANGE_SPEC_RE.fullmatch(raw)
    if range_match:
        min_val = int(range_match.group(1))
        max_val = int(range_match.group(2))
        require(min_val <= max_val, "target range min must not exceed max")
        require(max_val <= UNLIMITED, "target max exceeds safe integer limit")
        return {"min": min_val, "max": max_val, "raw": f"{min_val}-{max_val}", "is_range": True}
    target = parse_target(value)
    return {"min": target, "max": UNLIMITED, "raw": str(target), "is_range": False}


# 2026-11-05（M2 口径统一，用户裁决 ±10% 为唯一权威带）：单值目标浮动系数。
# 唯一权威源 = band_pct()（运行时读 env NWW_WC_BAND_PCT，缺省 0.10；置 0.08 回滚
# 旧 ±8% 口径）。下游（pipeline._apply_wc_note_override 等）必须经 band_pct()
# 同源消费，禁止另设默认值——读取失败须显式报错，不得静默回落另一套系数。
_BAND_PCT_DEFAULT = "0.10"


def band_pct() -> float:
    """单值目标浮动带系数（唯一权威源，运行时读 env）。"""
    return float(os.getenv("NWW_WC_BAND_PCT", _BAND_PCT_DEFAULT))


def compute_wordcount_bands(spec_or_value: Any) -> dict[str, dict[str, int]]:
    spec = parse_target_spec(spec_or_value)
    if spec["is_range"]:
        # 范围格式：直接取 [min, max]
        band_min, band_max = spec["min"], spec["max"]
    else:
        # 单值格式：基础目标上下浮动 ±10%（2026-11-05 M2 统一；旧 ±8% 可 env 回滚）
        _pct = band_pct()
        band_min = int(spec["min"] * (1 - _pct))
        band_max = int(spec["min"] * (1 + _pct))
    return {
        "internal": {"min": band_min, "max": band_max},
        "user": {"min": band_min, "max": band_max},
    }


def invalid_wordcount_result(
    reason: str, *, chapter: Any = None, case_id: Any = None,
    target: Any = None, actual: int | None = None,
) -> dict[str, Any]:
    if isinstance(target, dict) and "raw" in target:
        target_raw = target["raw"]
        target_min = target.get("min")
        target_max = target.get("max")
    else:
        target_raw = target
        target_min = None
        target_max = None
    return {
        "schema": WORDCOUNT_SCHEMA, "metric": METRIC, "chapter": chapter, "case_id": case_id,
        "target": target_raw, "target_min": target_min, "target_max": target_max,
        "actual": actual, "internal_band": None, "user_band": None,
        "signed_error_pct": None, "absolute_error_pct": None,
        "status": "invalid", "invalid_reason": reason,
    }


def measure_wordcount(value: str, *, chapter: Any = None, case_id: Any = None) -> dict[str, Any]:
    try:
        actual = count_visible_chars(value)
    except (TypeError, ValueError):
        return {
            "schema": MEASUREMENT_SCHEMA, "metric": METRIC, "chapter": chapter,
            "case_id": case_id, "actual": None, "status": "invalid", "invalid_reason": "INVALID_BODY",
        }
    return {
        "schema": MEASUREMENT_SCHEMA, "metric": METRIC, "chapter": chapter,
        "case_id": case_id, "actual": actual, "status": "measured", "invalid_reason": None,
    }


def evaluate_wordcount(
    value: str, target_value: Any, *, chapter: Any = None, case_id: Any = None,
) -> dict[str, Any]:
    try:
        spec = parse_target_spec(target_value)
    except (TypeError, ValueError):
        return invalid_wordcount_result("INVALID_TARGET", chapter=chapter, case_id=case_id)
    try:
        actual = count_visible_chars(value)
    except (TypeError, ValueError):
        return invalid_wordcount_result("INVALID_BODY", chapter=chapter, case_id=case_id, target=spec)
    if actual == 0:
        return invalid_wordcount_result("EMPTY_BODY", chapter=chapter, case_id=case_id, target=spec, actual=actual)
    bands = compute_wordcount_bands(spec)
    internal_pass = bands["internal"]["min"] <= actual <= bands["internal"]["max"]
    user_pass = bands["user"]["min"] <= actual <= bands["user"]["max"]
    status = "internal_pass" if internal_pass else (
        "borderline" if user_pass else ("under" if actual < bands["user"]["min"] else "over")
    )
    reference = spec["min"]
    signed_error = (actual - reference) / reference
    return {
        "schema": WORDCOUNT_SCHEMA, "metric": METRIC, "chapter": chapter, "case_id": case_id,
        "target": spec["raw"], "target_min": spec["min"], "target_max": spec["max"],
        "actual": actual,
        "internal_band": {**bands["internal"], "status": "pass" if internal_pass else "fail"},
        "user_band": {**bands["user"], "status": "pass" if user_pass else "fail"},
        "signed_error_pct": signed_error, "absolute_error_pct": abs(signed_error),
        "status": status, "invalid_reason": None,
    }


def checkpoint_wordcount(
    value: str, target_value: Any, *, chapter: Any = None, case_id: Any = None,
) -> dict[str, Any]:
    spec = parse_target_spec(target_value)
    actual = count_visible_chars(value)
    user = compute_wordcount_bands(spec)["user"]
    return {
        "schema": CHECKPOINT_SCHEMA, "metric": METRIC, "chapter": chapter, "case_id": case_id,
        "target": spec["raw"], "target_min": spec["min"], "target_max": spec["max"],
        "actual": actual, "user_band": user,
        "remaining_user_range": {
            "min": max(0, user["min"] - actual),
            "max": max(0, user["max"] - actual),
        },
    }


def target_from_outline(value: str) -> dict[str, Any]:
    text = normalize_newlines(value)
    range_targets = list(dict.fromkeys(_RANGE_TARGET_LINE_RE.findall(text)))
    single_targets = list(dict.fromkeys(_TARGET_LINE_RE.findall(text)))
    metrics = list(dict.fromkeys(_METRIC_LINE_RE.findall(text)))
    require(metrics == [METRIC], f"字数口径 must appear exactly once as {METRIC}")
    if range_targets:
        require(len(range_targets) == 1, "字数目标(range) must appear exactly once")
        require(not single_targets, "字数目标 cannot mix range and single value")
        min_val, max_val = range_targets[0]
        return parse_target_spec(f"{min_val}-{max_val}")
    require(len(single_targets) == 1, "字数目标 must appear exactly once with one value")
    return parse_target_spec(single_targets[0])


def _chapter_number_from_name(name: str, *, outline: bool) -> int | None:
    pattern = r"^细纲_第0*(\d+)章.*\.md$" if outline else r"^第0*(\d+)章(?:[_\- 　].*)?\.md$"
    match = re.match(pattern, name)
    return int(match.group(1)) if match else None


def find_chapter_file(directory: Path, chapter: int, *, outline: bool) -> Path:
    require(isinstance(chapter, int) and not isinstance(chapter, bool) and chapter >= 1, "chapter must be >= 1")
    require(directory.is_dir(), f"chapter directory is missing: {directory}")
    # 正文支持卷目录（正文/第一卷/第N章*.md），细纲保持平铺；rglob 对两种结构兼容。
    # 排除非正文目录（2026-10-25 修复）：_review_snapshots/ 存审稿快照（第001章_round1.md
    # 同样命中 第0*(\d+)章[_\- ].*\.md 模式），被算进正文候选 → "must have exactly one
    # body file" 必挂（与 pipeline._NON_PROSE_DIRS / nww_track_commit._is_non_prose 同口径）。
    non_prose_dirs = {"_review_snapshots", "_archive", ".backup", "_原稿"}
    matches = sorted(
        path for path in directory.rglob("*.md")
        if path.is_file()
        and not (non_prose_dirs & set(path.parts))
        and _chapter_number_from_name(path.name, outline=outline) == chapter
    )
    label = "outline" if outline else "body"
    require(len(matches) == 1, f"chapter {chapter} must have exactly one {label} file")
    return matches[0]


def build_project_wordcount_record(project: Path, chapter: int, *, resolution: str) -> dict[str, Any]:
    require(resolution in RESOLUTIONS, f"unsupported wordcount resolution: {resolution}")
    root = project.resolve()
    outline_path = find_chapter_file(root / "大纲", chapter, outline=True)
    body_path = find_chapter_file(root / "正文", chapter, outline=False)
    try:
        target = target_from_outline(outline_path.read_text(encoding="utf-8"))
        body_bytes = body_path.read_bytes()
        body = body_bytes.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise WordcountError(f"unable to read chapter files: {exc}") from exc
    result = evaluate_wordcount(body, target, chapter=chapter)
    require(result["status"] != "invalid", f"invalid chapter wordcount: {result['invalid_reason']}")
    in_user_band = result["status"] in {"internal_pass", "borderline"}
    require(
        (resolution == "within_user_band" and in_user_band)
        or (resolution == "accepted_current_length" and not in_user_band),
        "wordcount resolution does not match the current length",
    )
    return {
        "metric": METRIC,
        "target": result["target"],
        "target_min": result["target_min"],
        "target_max": result["target_max"],
        "actual": result["actual"],
        "status": result["status"],
        "resolution": resolution,
        "body_sha256": hashlib.sha256(body_bytes).hexdigest(),
    }


def normalize_wordcount_record(value: object) -> dict[str, Any]:
    require(isinstance(value, dict), "wordcount record must be an object")
    required = {"metric", "target", "actual", "status", "resolution", "body_sha256"}
    require(required.issubset(set(value)), "wordcount record missing required fields")
    require(value.get("metric") == METRIC, "wordcount metric is unsupported")
    spec = parse_target_spec(value.get("target"))
    actual = value.get("actual")
    require(isinstance(actual, int) and not isinstance(actual, bool) and actual >= 1, "wordcount actual is invalid")
    # Legacy records (created before band logic change, no target_min field) skip status
    # re-validation against current band logic — their status was valid at commit time.
    is_legacy = "target_min" not in value
    if not is_legacy:
        expected = evaluate_wordcount("字" * actual, spec)
        require(value.get("status") == expected["status"], "wordcount status does not match target and actual")
    resolution = value.get("resolution")
    require(resolution in RESOLUTIONS, "wordcount resolution is unsupported")
    if not is_legacy:
        expected = evaluate_wordcount("字" * actual, spec)
        in_user_band = expected["status"] in {"internal_pass", "borderline"}
        require((resolution == "within_user_band") == in_user_band, "wordcount resolution does not match status")
    digest = value.get("body_sha256")
    require(isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) is not None, "body_sha256 is invalid")
    return dict(value)


def validate_current_wordcount_record(project: Path, chapter: int, value: object) -> dict[str, Any]:
    record = normalize_wordcount_record(value)
    current = build_project_wordcount_record(project, chapter, resolution=record["resolution"])
    require(record == current, "wordcount record is stale for the current body or target")
    return record
