"""pipeline.py — 写章闭环编排器（多环节独立 agent + 确定性护栏 + fail-closed）。

流程：
  0. prealign（可选,advisory）：nww-architect 检查细纲结构层缺口
  1. packet          ：nww_packet_builder.py（含前导零细纲兼容）
  2. writer          ：narrative-writer agent 生成正文（本地大模型）
  3. 落盘            ：正文/第N章.md（样式跟随书内现有命名）
  4. delta 草稿      ：nww_delta_draft.py 确定性预填追踪/delta_第N章.json
  5. delta 补语义    ：LLM 补齐【草稿】留白字段（result/cost/commitments/timeline/global）
  6. post_write      ：nww_post_write.py（enforce → commit → cross_chapter）BLOCK 点
  7. deslop 机械层   ：check-ai-patterns / check-degeneration / normalize-punctuation（advisory）

每个环节独立执行、独立记录，任一步失败返回该步 detail 不掩盖。
"""
from __future__ import annotations

import contextvars
import difflib
import hashlib
import importlib.util
import json
import os
import re
import time
import uuid
from pathlib import Path

from .. import books
from ..fs import book_root, is_writable, resolve_path
from ..nwwmvp import (
    _find_outline_file,
    _preceding_from_body,
    _PRECEDING_MISSING,
    _resolve_body_target,
)
from . import registry, runtime, toolbox, writer_prompt
from . import nww_rhythm_planner
from .era_check_adapter import era_check_observe  # P6-E Step3：era_consistency observation adapter（零耦合）
_OUTLINE_DIR = "大纲"
_TRACKING_DIR = "追踪"
_DELTA_PREFIX = "delta_第"
_DRAFT_TAG = "【草稿】"
# 一致性自动闭环（2026-11-04）：detection+repair+recheck 清零后由系统自动落 一致性核验/第N章.json，
# 消除「检测修好却卡在 A-3 缺闭环记录」的流程断裂。env 回滚：NWW_AUTO_CLOSURE=0 关闭（回退旧行为）。
_AUTO_CLOSURE_SWITCH = "NWW_AUTO_CLOSURE"
_CONFIRMED_BY_AUTOCLOSE = "system_autoclose"

# ---------------------------------------------------------------------------
# V13 道闸策略层（2026-11-13）：唯一实现在 skills/nww-novel-skill/scripts/
# nww_gate_policy.py。加载 + 缓存 + 显式状态收敛到共享加载器 nww_gate_loader
# （C-GATE-LOADER@1），web 侧只消费，不再自持一份 importlib 与缓存。
#   v13（生产默认）：闸只记录事实 + 留痕，动作继续，裁决权交作者；
#   strict（env NWW_GATE_MODE=strict）：完全回到 V12 硬闸语义。
# 降级（fallback_strict / fallback_empty）一律用加载器给的 strict 面，绝不因策略层
# 缺失而崩主链；降级状态进该章 stage detail 的 gate_degraded，不再静默。
# ---------------------------------------------------------------------------
_GATE_MOD: object | None = None
_GATE_STATUS = "ready"
_GATE_LOADER_MOD: object | None = None


class _StrictFallback:
    """加载器本身不可用时的最小 strict 面（真实 fallback 由加载器提供）。"""

    @staticmethod
    def is_v13() -> bool:
        return False

    @staticmethod
    def record(*a, **k):
        return None

    @staticmethod
    def note(*a, **k):
        return None

    @staticmethod
    def report(project, chapter):
        return {"status": "empty", "count": 0, "findings": []}


def _gate_loader() -> object:
    """按仓库根加载共享判据加载器（唯一 load + 缓存 + 状态实现）。"""
    global _GATE_LOADER_MOD
    if _GATE_LOADER_MOD is None:
        _p = toolbox.scripts_dir() / "nww_gate_loader.py"
        _spec = importlib.util.spec_from_file_location("nww_gate_loader", _p)
        if _spec is None or _spec.loader is None:
            raise RuntimeError(f"spec unavailable: {_p}")
        _mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        _GATE_LOADER_MOD = _mod
    return _GATE_LOADER_MOD


def _gate_state() -> tuple[object, str]:
    """(策略模块, 加载状态)；状态 ∈ {ready, fallback_strict, fallback_empty}。"""
    global _GATE_MOD, _GATE_STATUS
    if _GATE_MOD is None:
        try:
            _GATE_MOD, _GATE_STATUS = _gate_loader().load(toolbox.scripts_dir())
        except Exception as exc:  # noqa: BLE001 — 加载器缺失不得拖垮写链
            print(f"[GATE] gate loader unavailable, fallback to strict: {exc}")
            _GATE_MOD, _GATE_STATUS = _StrictFallback(), "fallback_strict"
        if _GATE_STATUS != "ready":
            print(f"[GATE] policy degraded, fallback to strict: status={_GATE_STATUS}")
    return _GATE_MOD, _GATE_STATUS


def _gate() -> object:
    return _gate_state()[0]


def _gate_degraded_extra() -> dict:
    """降级状态进 stage detail：ready 时不加字段，保持既有 stage 形状。"""
    _mod, _status = _gate_state()
    return {} if _status == "ready" else {"gate_degraded": _status}


def _gate_record(book: Path, chapter_id: int, code: str, detail: str,
                 source: str = "pipeline") -> None:
    """V13 闸事实留痕（fail-open）。strict 模式下 record 本身是 no-op。"""
    try:
        if _gate().is_v13():  # type: ignore[attr-defined]
            _gate().record(book, chapter_id, code, detail, source=source)  # type: ignore[attr-defined]
            _gate().note(code, detail)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


def _rewrite_gate_v13_degrade(book: Path, chapter_id: int, code: str, reason: str) -> bool:
    """V13 道闸：重写链质量闸违规只记事实卡，不阻断、不回滚正文。

    与早期接入 V13 策略层的闸（0pre 悬空章 / 审稿评分 / A-5 quality_meta）同口径：
    闸只陈述事实，产物有效性由作者裁决。返回 True = 已按 V13 放行主链。
    strict（NWW_GATE_MODE=strict）返回 False，调用方保持 V12 原语义
    （BLOCK + 回滚执行前正文 + 不 commit / 不 revision / 不 delta）。
    """
    if not _gate().is_v13():  # type: ignore[attr-defined]
        return False
    _gate_record(book, chapter_id, code, reason, "pipeline/rewrite")
    return True


def _dry_run_sandbox(book: Path, execution_id: str) -> Path:
    """P3-R2：dry_run 隔离根。所有 dry_run 副作用写入 追踪/dry_run/<exec_id>/，
    正式 正文//追踪/ 零变化。返回 sandbox 根目录。"""
    sb = book / _TRACKING_DIR / "dry_run" / (execution_id or "anon")
    (sb / "body-preview").mkdir(parents=True, exist_ok=True)
    (sb / "delta-preview").mkdir(parents=True, exist_ok=True)
    return sb
# 契约来源：nww_track_commit.normalize_delta 的 clean_text(..., max_bytes=480)。
# 这里是**字节**上限，不是字符上限——中文 1 字 = 3 字节，别按字符截。
_RESULT_MAX_BYTES = 480
# C-DELTA-SCHEMA@1（2026-11-13 审计 R7）：delta 白名单与 quality_meta 系统键的唯一来源
# 是 skills/nww-novel-skill/scripts/nww_delta_schema.py —— commit 侧直接 import 它，
# web 侧按仓库根动态加载同一文件（与 _gate 同模式，避免两处字面量分叉）。
# 下方两份字面量只在模块加载失败时作回退副本（保持链可跑）。
_DELTA_ALLOWED = {
    "result", "character_changes", "foreshadow_changes", "timeline_events", "constraints",
    "next_chapter_commitments", "retired_context_items", "retired_characters", "cost_change",
    "quality_meta", "rule_loss_event", "revived_characters", "retirement_reasons", "chapter_type",
    "contract_state", "candidate_facts", "fact_changes",
}
_SYS_QUALITY_META_KEYS = (
    "reviewed", "reviewed_at", "review_score", "review_rounds",
    "finalized", "finalized_at", "review_id",
    "reviewed_hash", "final_hash", "target_body_md5", "review_status",
)
_DELTA_SCHEMA_MOD: object | None = None


def _delta_schema() -> object:
    """按仓库根加载 nww_delta_schema（不复制实现，避免两处白名单漂移）。"""
    global _DELTA_SCHEMA_MOD
    if _DELTA_SCHEMA_MOD is None:
        _p = toolbox.scripts_dir() / "nww_delta_schema.py"
        _spec = importlib.util.spec_from_file_location("nww_delta_schema", _p)
        if _spec is None or _spec.loader is None:
            raise RuntimeError(f"spec unavailable: {_p}")
        _mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        _DELTA_SCHEMA_MOD = _mod
    return _DELTA_SCHEMA_MOD


try:
    _schema = _delta_schema()
    _DELTA_ALLOWED = frozenset(_schema.DELTA_ALLOWED)  # type: ignore[attr-defined]
    _SYS_QUALITY_META_KEYS = tuple(_schema.SYS_QUALITY_META_KEYS)  # type: ignore[attr-defined]
except Exception as exc:  # noqa: BLE001 — 白名单模块缺失不得拖垮写链
    print(f"[DELTA_SCHEMA] nww_delta_schema 不可用，回退模块内副本: {exc}", flush=True)

_SYS_QUALITY_META_KEYS_CACHE: tuple[str, ...] | None = None


def _sys_quality_meta_keys() -> tuple[str, ...]:
    """quality_meta 系统键单源（C-QUALITY-META@1）：nww_delta_schema。

    首次求解后即缓存（含失败时的回退结果），避免每次消费都重试加载并重复告警。
    """
    global _SYS_QUALITY_META_KEYS_CACHE
    if _SYS_QUALITY_META_KEYS_CACHE is None:
        try:
            _SYS_QUALITY_META_KEYS_CACHE = tuple(_delta_schema().SYS_QUALITY_META_KEYS)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 — 系统键模块缺失不得拖垮写链
            print(f"[QUALITY_META] nww_delta_schema 不可用，回退模块内系统键: {exc}", flush=True)
            _SYS_QUALITY_META_KEYS_CACHE = _SYS_QUALITY_META_KEYS
    return _SYS_QUALITY_META_KEYS_CACHE
# 契约来源：nww_track_commit.clean_string_list(..., maximum=N) —— 超限是**硬拒**，不是警告。
_DELTA_LIST_CAPS = {"constraints": 12, "retired_context_items": 11, "next_chapter_commitments": 5}
# character_changes[i].change 上限：commit 是 clean_text(..., max_bytes=360)，同样是字节。
_CHANGE_MAX_BYTES = 360


class StageResult:
    """单环节结果。"""

    def __init__(self, name: str, ok: bool, detail: str = "", **extra):
        self.name = name
        self.ok = ok
        self.detail = detail
        self.extra = extra

    def to_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "detail": self.detail, **self.extra}


class AnalyzeBudgetExceeded(RuntimeError):
    """analyze 类环节（结构化 JSON 提取/规划）重试总预算耗尽。

    2026-11-01（用户裁决，修「重写一直报错、不断有新阻断」的耗时不可控面）：
    合同提取 3 次 / L3 Plan 3 次 / 润色分析 3 次各自内部重试，单看每次都有上限，
    叠起来却无上界——上游通道半死（每次空返回/截断都等到超时）时，一轮重写可以在
    analyze 阶段空转十几分钟，最后仍然失败，用户只看到「跑了很久又报错」。

    闸门语义（刻意保守，防误伤）：
      - **只在「准备再花一轮」之前**判定，首轮永不拦 —— 慢但一次成功的调用不受影响；
      - 耗尽即抛本异常，消息带「已尝试次数 + 累计耗时 + 最后一次错误」，可归因；
      - 不降级、不放宽任何合同/守卫口径，只缩短失败路径。
    预算 env `NWW_ANALYZE_BUDGET`（秒，默认 480，≤0 = 不限制）。
    """


def _analyze_budget_seconds() -> float:
    try:
        v = float(os.environ.get("NWW_ANALYZE_BUDGET", "480") or 480)
    except ValueError:
        v = 480.0
    return v if v > 0 else 0.0


def _analyze_retry_guard(t0: float, what: str, attempts_done: int,
                         last_error: object = None) -> None:
    """重试前预算闸：**仅**在重试前调用（首轮不判）。超预算 → AnalyzeBudgetExceeded。"""
    budget = _analyze_budget_seconds()
    if budget <= 0:
        return
    elapsed = time.monotonic() - t0
    if elapsed < budget:
        return
    raise AnalyzeBudgetExceeded(
        f"{what} 重试预算耗尽：已尝试 {attempts_done} 次、累计 {elapsed:.0f}s ≥ 预算 {budget:.0f}s，"
        f"不再重试（避免拖死主链）；最后一次错误：{last_error if last_error not in (None, '') else '无'}；"
        f"需要放宽时设 NWW_ANALYZE_BUDGET=<秒>（0=不限制）")


def _session_outline_compat(book: Path, chapter_id: int) -> Path:
    """前导零兼容：细纲以 细纲_第0NN章.md 命名时，packet builder 只认
    细纲_第N章.md。在不改动书目录的前提下，返回实际细纲路径；若精确名缺失，
    返回前导零文件的真实路径，由调用方按此喂给 builder。
    （builder 按 book_root 拼名，故这里改用「临时软链」方案：在 大纲/ 下建
    精确名软链指向真实文件，用完即删。）
    """
    exact = book / _OUTLINE_DIR / f"细纲_第{chapter_id}章.md"
    real = _find_outline_file(book, chapter_id)
    if real is None:
        raise FileNotFoundError(f"细纲不存在：大纲/细纲_第{chapter_id}章(.json|.md)")
    if real == exact:
        return exact  # 无需兼容
    # 软链精确名 → 真实文件（try/finally 由调用方删除）
    (book / _OUTLINE_DIR).mkdir(parents=True, exist_ok=True)
    if not exact.exists():
        exact.symlink_to(real.name)
    return exact


def _cleanup_outline_compat(book: Path, chapter_id: int) -> None:
    """删除前导零兼容临时软链（仅删软链本身，不碰真实文件）。"""
    exact = book / _OUTLINE_DIR / f"细纲_第{chapter_id}章.md"
    try:
        if exact.is_symlink():
            exact.unlink()
    except OSError:
        pass


# 卷级主细纲章头样式：`## 第32章 · 周执事`（分隔符可为「· / 。」或空格+连字符）
_VOL_HDR_RE = re.compile(r"^#+\s*第\s*(\d+)\s*章\s*[·.。,、:\s-]*(.*?)\s*$")


def _volume_outline_section(book: Path, chapter_id: int) -> tuple[str, str | None, Path] | None:
    """从卷级主细纲切出第 chapter_id 章段落。

    返回 (段落文本, 章标题, 所在卷细纲文件)，找不到该章段落返回 None。
    （外脑裁定：新写时独立单章细纲缺失，自动从卷级主细纲补——先规划后写，卷级细纲是唯一来源底限。）
    """
    o_dir = book / _OUTLINE_DIR
    if not o_dir.is_dir():
        return None
    for vp in sorted(o_dir.glob("第*卷细纲_*.md")):
        try:
            lines = vp.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        start = None
        title: str | None = None
        for i, ln in enumerate(lines):
            m = _VOL_HDR_RE.match(ln.strip())
            if m and int(m.group(1)) == chapter_id:
                start = i
                raw = m.group(2).strip()
                if raw:
                    title = raw.split()[0] if raw else None
                break
        if start is None:
            continue
        nxt = next((i for i in range(start + 1, len(lines)) if _VOL_HDR_RE.match(lines[i].strip())), len(lines))
        seg = "\n".join(lines[start:nxt]).strip()
        if not seg:
            return None
        return seg, title, vp
    return None


def _compute_projection_hash(seg: str) -> str:
    """计算章节投影的规范化 hash。
    
    对卷级主细纲中该章段落做最小规范化后计算 MD5。
    规范化：去掉首尾空白，统一换行符。
    """
    normalized = seg.strip().replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.md5(normalized.encode("utf-8")).hexdigest()


def _extract_source_hash(text: str) -> str | None:
    """从独立细纲头部提取 source_hash。
    
    格式：<!-- source_hash: xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx -->
    """
    m = re.search(r"source_hash:\s*([a-f0-9]{32})", text)
    return m.group(1) if m else None


# ---- 细纲字数两行补齐（2026-11-01） ----
# 事故背景（真机冒烟实测）：`step_plan_outline` 自动生成的细纲没有字数行 →
# `nww_wordcount.target_from_outline` 报 `CHECK_FAILED: 字数口径 must appear
# exactly once as visible_chars_v1` → 写章链的字数闸永远拿不到目标。
# 修复原则（外脑审查后修订）：**只做格式归一，不编造数值**。
#   - 目标来源按权威顺序：卷级主细纲该章段落 → bible.json.wordcount_target → 不补；
#   - 无来源时原样返回（既不补也不删，存量细纲零变化）；
#   - 有来源时先删除**所有形态**的「字数目标/字数口径」行（粗体 `**…**`、列表
#     `- `、引用 `> `、单值/区间、重复行），再在 H1 后插入唯一标准两行。
#     实测反例：只处理单值行时 `**字数目标**：2600` 会残留 → wordcount 侧
#     `字数目标 must appear exactly once with one value` FAIL。
_WC_VALUE_RE = re.compile(r"^([1-9]\d*)(?:[ \t]*[-~～至到][ \t]*([1-9]\d*))?[ \t]*字?$")
_CARD_DIR_REL = "设定/角色"


def _wc_line_kind(line: str) -> str:
    """判断细纲行是否字数元信息行：'target' / 'metric' / ''（markdown 装饰已剥离）。"""
    probe = line.replace("**", "").replace("*", "").strip()
    probe = re.sub(r"^[-+>#\s]+", "", probe)
    if re.match(r"^字数目标[ \t]*[:：]", probe):
        return "target"
    if re.match(r"^字数口径[ \t]*[:：]", probe):
        return "metric"
    return ""


def _wc_canonical_value(raw: object) -> str:
    """把任意写法（2600 / "2600" / "2600字" / "2000-2500" / 2500）归一为
    '2600' 或 '2000-2500'；不可解析/非法区间 → ''（不猜）。"""
    text = str(raw or "").strip().replace("**", "").strip()
    m = _WC_VALUE_RE.match(text)
    if not m:
        return ""
    lo = int(m.group(1))
    hi = int(m.group(2)) if m.group(2) else 0
    if hi and hi > lo:
        return f"{lo}-{hi}"
    return str(lo)


def _strip_wordcount_lines(text: str, *, include_metric: bool = True) -> str:
    """删除细纲中所有字数元信息行（换行结构原样保留其余内容）。"""
    keep: list[str] = []
    for line in text.splitlines():
        kind = _wc_line_kind(line)
        if kind == "target" or (include_metric and kind == "metric"):
            continue
        keep.append(line)
    return "\n".join(keep)


def _wc_target_from_text(text: str) -> str:
    """从一段文本（卷级该章段落 / bible 值）里取第一个可解析的字数目标。"""
    for line in text.splitlines():
        if _wc_line_kind(line) != "target":
            continue
        probe = line.replace("**", "").strip()
        probe = re.sub(r"^[-+>#\s]+", "", probe)
        probe = re.split(r"[:：]", probe, maxsplit=1)[-1].strip()
        value = _wc_canonical_value(probe)
        if value:
            return value
    return ""


def _bible_wordcount_target(book: Path) -> str:
    """bible.json.wordcount_target（缺省/非法 → ''）。"""
    try:
        bible = json.loads((book / "bible.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — 无 bible/损坏 → 不补
        return ""
    return _wc_canonical_value(bible.get("wordcount_target"))


def _outline_wordcount_target(book: Path, chapter_id: int) -> str:
    """该章字数目标的权威值：卷级主细纲该章段落 → bible.json → ''（无来源绝不编造）。"""
    hit = _volume_outline_section(book, chapter_id)
    if hit is not None:
        value = _wc_target_from_text(hit[0])
        if value:
            return value
    return _bible_wordcount_target(book)


def _ensure_outline_wordcount_lines(body: str, book: Path, chapter_id: int) -> str:
    """幂等补齐细纲的标准字数两行（`字数目标：X` + `字数口径：visible_chars_v1`）。

    - 无权威来源 → 返回原文（不补不删）；
    - 有来源 → 删除所有形态的旧字数行，再插到 H1 之后；
    - 幂等：对已合规文本重复调用，输出不变（行位置/数量都不变）。
    """
    target = _outline_wordcount_target(book, chapter_id)
    if not target:
        return body.strip()
    cleaned = _strip_wordcount_lines(body).strip()
    block = f"字数目标：{target}\n字数口径：visible_chars_v1"
    lines = cleaned.splitlines()
    h1_idx = next((i for i, line in enumerate(lines) if re.match(r"^#(?!#)\s*\S", line)), None)
    if h1_idx is None:
        return (block + "\n\n" + cleaned).strip()
    head = "\n".join(lines[:h1_idx + 1]).rstrip()
    tail = "\n".join(lines[h1_idx + 1:]).strip("\n")
    return (head + "\n\n" + block + ("\n\n" + tail if tail.strip() else "")).strip()


def _is_valid_chapter_outline(text: str) -> tuple[bool, str]:
    """检查独立细纲是否为有效 ChapterOutline。
    
    有效性标准：不能只有字数目标，必须包含章节任务/场景/冲突/结果等生产信息。
    返回 (is_valid, reason)。
    """
    # 提取正文（去掉标题行、front matter 和字数目标行）
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    body_lines = []
    for l in lines:
        if l.startswith("#"):
            continue
        if l.startswith("<!--") and "source_hash" in l:
            continue
        if re.match(r"^\**\s*字数目标\**\s*[:：]", l):
            continue
        if re.match(r"^\**\s*字数口径\**\s*[:：]", l):
            continue
        body_lines.append(l)
    
    body_text = "\n".join(body_lines)
    body_len = len(body_text)
    
    # 空壳判断：正文内容 < 200 字
    if body_len < 200:
        return False, f"TARGET_WORDS_ONLY（正文内容仅 {body_len} 字）"
    
    # 必须包含至少一个结构性标记
    structural_markers = [
        "章首钩子", "第一幕", "第二幕", "第三幕",
        "出场角色", "场景", "核心事件", "推进动作",
        "冲突", "阻碍", "结果", "Outcome", "钩子",
        "E1", "E2", "E3",
    ]
    has_marker = any(m in body_text for m in structural_markers)
    if not has_marker:
        return False, "NO_STRUCTURAL_MARKERS（无场景/冲突/结果等结构性标记）"
    
    return True, "VALID"


def _ensure_outline_for_write(book: Path, chapter_id: int, *, allow_body_exists: bool = False) -> tuple[Path | None, str | None]:
    """新写自动补细纲（外脑裁定）：独立单章细纲缺失/空壳时，从卷级主细纲切出该章段落并落盘。

    返回 (独立细纲路径, 章标题)。卷级主细纲也没有该章 → (None, 章缺失原因)。
    - 已有有效独立细纲 → 原样返回（不重复生成）。
    - 已有空壳/无效独立细纲 → 尝试从卷级主细纲重建。
    - 存在卷级主细纲该章 → 切分落盘 大纲/细纲_第{ch}章.md，供后续 packet/prealign/writer 消费。
    - 卷级也无 → 返回 (None, "卷级主细纲无该章"),由调用方按 BLOCK 处理，绝不凭空造。
    仅应在「新写」流程（run_write_pipeline）调用，重写路径不得调用。
    """
    existing = _find_outline_file(book, chapter_id)
    outline_log = {"chapter": chapter_id}
    
    if existing is not None:
        try:
            existing_text = existing.read_text(encoding="utf-8")
            is_valid, reason = _is_valid_chapter_outline(existing_text)
            outline_log["existing_status"] = "VALID" if is_valid else "INVALID"
            outline_log["existing_reason"] = reason
            
            if is_valid:
                # STALENESS CHECK：检查 source_hash
                existing_hash = _extract_source_hash(existing_text)
                if existing_hash is None:
                    # 历史 VALID 细纲无 source_hash → 兼容策略：USE_EXISTING
                    outline_log["action"] = "USE_EXISTING_NO_HASH"
                    print(f"[OUTLINE_GATE] ch{chapter_id}: existing=VALID reason={reason} action=USE_EXISTING_NO_HASH（历史细纲无 source_hash）")
                    return existing, None
                
                # 有 source_hash → 对比当前卷级投影 hash
                hit = _volume_outline_section(book, chapter_id)
                if hit is not None:
                    seg, title, vp = hit
                    # 2026-11-01：投影 hash 的口径必须与写入侧完全一致（剔除字数目标行），
                    # 否则卷级段落带 `**字数目标**：N` 时 stored 与 current 永久不等 →
                    # 每章都判 REBUILD_STALE 覆盖细纲（存量实测）。
                    current_hash = _compute_projection_hash(
                        _strip_wordcount_lines(seg, include_metric=False))
                    outline_log["existing_hash"] = existing_hash[:8]
                    outline_log["current_hash"] = current_hash[:8]
                    
                    if existing_hash == current_hash:
                        outline_log["action"] = "USE_EXISTING_FRESH"
                        print(f"[OUTLINE_GATE] ch{chapter_id}: existing=VALID reason={reason} action=USE_EXISTING_FRESH hash={current_hash[:8]}")
                        return existing, None
                    else:
                        outline_log["action"] = "REBUILD_STALE"
                        outline_log["stale_reason"] = "HASH_MISMATCH"
                        print(f"[OUTLINE_GATE] ch{chapter_id}: existing=VALID reason={reason} action=REBUILD_STALE old={existing_hash[:8]} new={current_hash[:8]}")
                        # STALE → 继续走重建逻辑（覆盖现有细纲）
                else:
                    # 卷级主细纲无此章 → 保留现有细纲
                    outline_log["action"] = "USE_EXISTING_NO_VOLUME"
                    print(f"[OUTLINE_GATE] ch{chapter_id}: existing=VALID reason={reason} action=USE_EXISTING_NO_VOLUME（卷级无此章）")
                    return existing, None
            else:
                outline_log["action"] = "REBUILD_INVALID"
                print(f"[OUTLINE_GATE] ch{chapter_id}: existing=INVALID reason={reason} action=REBUILD_INVALID")
        except Exception as e:
            is_valid = False
            reason = f"READ_ERROR: {e}"
            outline_log["existing_status"] = "INVALID"
            outline_log["existing_reason"] = reason
            outline_log["action"] = "REBUILD"
            print(f"[OUTLINE_GATE] ch{chapter_id}: existing=INVALID reason={reason} action=REBUILD")
    
    # 防误伤：正文文件已存在 → 该章已写过，不是新写 → 不得自动补细纲。
    # rewrite 通过 allow_body_exists=True 跳过此保护（已有正文是 rewrite 的正常前提）。
    # 用 _is_non_prose 排除 _review_snapshots/备份等非正文文件，避免把审稿快照误当正文。
    if not allow_body_exists:
        body_dir = book / "正文"
        if body_dir.is_dir():
            for p in body_dir.rglob("第*章*.md"):
                if not books._is_non_prose(p) and re.match(rf"^第0*{chapter_id}章", p.name):
                    return None, f"第{chapter_id}章正文已存在，属于已写章节，不走新写自动补细纲（请走重写流程）"
    
    hit = _volume_outline_section(book, chapter_id)
    if hit is None:
        outline_log["rebuild_result"] = "FAIL"
        outline_log["rebuild_reason"] = "VOLUME_OUTLINE_MISSING"
        print(f"[OUTLINE_GATE] ch{chapter_id}: rebuild_result=FAIL reason=VOLUME_OUTLINE_MISSING")
        return None, "卷级主细纲无该章（需先在规划层补卷纲，不能凭空生成细纲）"
    
    seg, title, vp = hit
    o_dir = book / _OUTLINE_DIR
    o_dir.mkdir(parents=True, exist_ok=True)
    out = o_dir / f"细纲_第{chapter_id}章.md"
    header = f"# 细纲_第{chapter_id}章　书名：{title or '未命名'}\n"
    # 字数两行归一（2026-11-01）：卷级段落是 `**字数目标**：N`（粗体）时 wordcount 只认
    # 标准两行 → 统一删除所有形态的旧行并补 `字数目标：X` + `字数口径：visible_chars_v1`。
    # 目标来源：卷级该章段落 → bible.json.wordcount_target；两者都无 → 不补（绝不凭空造）。
    body_text = _ensure_outline_wordcount_lines(seg, book, chapter_id)

    # 计算 source_hash（基于规范化后的卷级段落；剔除字数目标行，与历史 hash 口径一致）
    source_hash = _compute_projection_hash(_strip_wordcount_lines(seg, include_metric=False))
    source_volume_name = vp.name if vp else "unknown"
    front_matter = f"<!-- source_volume: {source_volume_name} source_hash: {source_hash} -->\n"
    
    new_content = front_matter + header + "\n" + body_text + "\n"
    out.write_text(new_content, encoding="utf-8")
    
    # P0-3：重建后必须再次验证
    rebuilt_valid, rebuilt_reason = _is_valid_chapter_outline(new_content)
    outline_log["rebuild_result"] = "VALID" if rebuilt_valid else "INVALID"
    outline_log["rebuild_reason"] = rebuilt_reason
    print(f"[OUTLINE_GATE] ch{chapter_id}: rebuild_result={outline_log['rebuild_result']} reason={rebuilt_reason}")
    
    if not rebuilt_valid:
        # P0-4：重建失败必须 BLOCK，不能继续 Writer
        outline_log["final_result"] = "FAIL"
        print(f"[OUTLINE_GATE] ch{chapter_id}: FINAL=FAIL reason={rebuilt_reason}")
        return None, f"重建细纲仍无效：{rebuilt_reason}（卷级主细纲该章内容不完整，需先补卷纲）"
    
    outline_log["final_result"] = "OK"
    return out, title


def _last_committed_chapter(book: Path) -> int | None:
    """读追踪权威的 last_committed_chapter；无追踪状态（新书/无追踪目录）→ None（不拦）。"""
    state_file = book / _TRACKING_DIR / "_tracking-state.json"
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        val = data.get("last_committed_chapter")
        return int(val) if isinstance(val, int) and val > 0 else None
    except Exception:  # noqa: BLE001 — 无追踪状态不是 gate 该炸的事
        return None


# ---- 0b 写前自动规划（2026-10-24 用户裁决：全流程自动生成 + 堵点播报） ----
# 缺口背景：_ensure_outline_for_write 只做「卷级主细纲 → 单章切分」的机械搬运，
# 而全仓库没有任何生产端（SKILL.md 规范布局只有 卷纲_第N卷.md + 独立细纲），
# 新书逐章写必然卡 BLOCK「卷级主细纲无该章」。本步把「从卷纲推进到逐章细纲」
# 接进链内：nww-architect 依据 卷纲(弧线) + 追踪上下文 + 上章事实弧 产出该章细纲。
# 外脑「不凭空造」铁律的接法：生成输入锚死在既有权威（卷纲弧线/追踪状态/上章
# delta），细纲落盘独立可见、可编辑、可重跑 prealign 检查 —— 规划产物仍在
# 大纲/ 目录里走既有验收（packet/prealign/audit），写链不跳过任何闸。
# env NWW_PLANNER=0 关闭（回到原 BLOCK 语义）。

_PLANNER_PROMPT_RULES = (
    "## 输出硬约束（逐条遵守）\n"
    "1. 直接输出该章细纲的完整 Markdown正文（无代码块、无解释、无前言）。\n"
    "2. 第一行必须是 `# 第{ch}章 <本章标题>`（标题不含书名、不带「第N章」外的前缀）。\n"
    "3. 必须且只能包含以下小节（顺序不变，一律 `## ` 二级标题）：\n"
    "   `## 出场角色voice锚点` / `## 章首钩子` / `## 第一幕（场景：地点/时段）。D2 事件` /\n"
    "   `## 第二幕（场景：…）。D2 事件` / `## 第三幕（场景：…）。D3 事件` / `## 收益兑现` /\n"
    "   `## 目标情绪` / `## 爽点` / `## 落点`。\n"
    "   其中 `## 出场角色voice锚点` 的条目形态固定为 `- **角色名**：①…②…`（角色名 +\n"
    "   至少一条规则：①口癖/句式 ②动作习惯 ③禁用表达 ④与谁硬性区分）；旧字段行\n"
    "   `- **出场角色voice锚点**：…` 仅作历史兼容读取，不再作为输出要求。\n"
    "4. 每个小节的正文格式完全照抄「格式样板」（同一份标点体系、同一种行文密度）。\n"
    "   幕内固定五要素：**A** 驱动/目标/限制/失败、**B** 冲突/压力/迫使、**D** 选择/代价/收益、"
    "**F** 旧状态/新状态/新增问题，再加一行 `- 本幕内容：…`。\n"
    "5. 落点小节固定五行：`- 章尾类型：…`、`- 主动力 drive：…`、`- 烟火值：高/中/低`、"
    "`- 伏笔埋设：F编号 …`（本章新埋伏笔才写；编号从既有最大 F 编号 +1 连续）、`- 章末钩子：…`。\n"
    "6. 全文对话示例一律用半角双引号 `\"`，禁用「」。正文口语但要具体：地名、商品名、"
    "人名必须与「书内事实锚」逐字一致，禁止发明新设定。\n"
    "7. 铁律：只推进「卷线弧线」授权的剧情；与「追踪事实锚」和「上章末事实」冲突一律以冲突源为准。\n"
)


# 2026-11-04（Step 2 · 只增不改 + env 回滚，默认关闭）。
# 细纲上下章义务字段 + 写前门禁开关。默认 "0"=关；置 "1" 开启：
# planner 产出 上章承接/下章铺垫/读者体验合同 三段、写前门禁校验、packet 解析、writer 硬约束。
def _outline_contract_enabled() -> bool:
    return os.environ.get("NWW_OUTLINE_CONTRACT", "0") != "0"


# Step 2 规划侧规则增量：开启时在固定小节之外额外要求三段上下章义务字段。
_PLANNER_OUTLINE_CONTRACT_RULES = (
    "\n8. 开启「上下章义务」模式时，在以上固定小节之外，额外允许且必须包含以下三个二级标题小节"
    "（这是写前门禁的必检项，缺失将被退回重规划）：\n"
    "   - `## 上章承接`：本章程必须回应/承接的上章钩子与未结承诺（逐条列出；可为空段落但小节须存在）。\n"
    "   - `## 下章铺垫`：本章结束时必须为下章铺设的入口状态（下章主角带着什么局面/情报/情绪开局）。\n"
    "   - `## 读者体验合同`：本章读者体验承诺，固定九行 bullet：\n"
    "     `- 读者问题：…`（本章要解答/抛出的读者疑问）\n"
    "     `- 可见回报：…`（本章给读者的爽点/兑现）\n"
    "     `- 回报层级：…`（微爽/中爽/大爽）\n"
    "     `- 主角欲望：…`（本章主角主动想要什么）\n"
    "     `- 主要阻力：…`（挡在主角面前的阻力）\n"
    "     `- 关键转折：…`（本章最关键的剧情转折）\n"
    "     `- 情绪位移：…`（读者情绪从→到）\n"
    "     `- 信息揭示：…`（本章揭示的关键信息）\n"
    "     `- 净变化：…`（本章结束相对开局的净推进）\n"
    "   三段内容必须具体、可校验，禁止空话（如「情节顺畅」）。\n"
)


def _planner_prompt_rules(chapter_id: int) -> str:
    """返回规划 prompt 的硬约束段；开启上下章义务时追加额外规则（基础段逐字节不变）。"""
    base = _PLANNER_PROMPT_RULES.replace("{ch}", str(chapter_id))
    if _outline_contract_enabled():
        return base + _PLANNER_OUTLINE_CONTRACT_RULES
    return base


# 写前门禁校验器（复用 packet builder 的 _validate_outline_contract，单一事实源，避免自造正则）。
_OUTLINE_CONTRACT_VALIDATOR = None
_OUTLINE_CONTRACT_VALIDATOR_LOADED = False


def _get_outline_contract_validator() -> "callable | None":
    """加载 packet builder 的 _validate_outline_contract。失败（环境缺模块）→ None，降级放行。"""
    global _OUTLINE_CONTRACT_VALIDATOR, _OUTLINE_CONTRACT_VALIDATOR_LOADED
    if _OUTLINE_CONTRACT_VALIDATOR_LOADED:
        return _OUTLINE_CONTRACT_VALIDATOR
    _OUTLINE_CONTRACT_VALIDATOR_LOADED = True
    try:
        import importlib.util
        # 2026-11-05 治本：与生成侧共用唯一解析器（原先优先 engine 副本、依赖 cwd，
        # 与 step_packet 走的 skills 副本不是同一份，审计结论不可复现）。
        _cand = toolbox.resolve_script("nww_packet_builder_v2.py")
        if _cand is not None and _cand.exists():
            _spec = importlib.util.spec_from_file_location("_nww_pb_oc", str(_cand))
            _mod = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)
            _OUTLINE_CONTRACT_VALIDATOR = getattr(_mod, "_validate_outline_contract", None)
    except Exception as _e:  # noqa: BLE001 — 降级放行，不阻塞规划
        print(f"[OUTLINE_CONTRACT] validator 加载失败，门禁降级放行: {_e}")
    return _OUTLINE_CONTRACT_VALIDATOR


def _read_vol_outline_text(book: Path) -> str:
    """读全部卷纲弧线（卷纲_*.md）拼接为规划输入；不存在 → 空串。"""
    parts: list[str] = []
    o_dir = book / _OUTLINE_DIR
    if o_dir.is_dir():
        for vp in sorted(o_dir.glob("卷纲_*.md")):
            try:
                parts.append(vp.read_text(encoding="utf-8"))
            except OSError:
                continue
    return "\n\n".join(parts)[:6000]


def _plan_outline_context(book: Path, chapter_id: int) -> tuple[str, str]:
    """(事实锚, 格式样板)：追踪上下文摘要 + 上章 delta 结果/伏笔 + 上章细纲样板。"""
    ctx_abs, prev_delta_abs = "", ""
    try:
        ctx = book / _TRACKING_DIR / "上下文.md"
        if ctx.is_file():
            ctx_abs = ctx.read_text(encoding="utf-8")[:2200]
    except OSError:
        pass
    prev = chapter_id - 1
    prev_outline = _find_outline_file(book, prev)
    _delta_facts = ""
    try:
        delta_path = book / _TRACKING_DIR / f"delta_第{prev:03d}章.json"
        if delta_path.is_file():
            raw = json.loads(delta_path.read_text(encoding="utf-8"))
            tx = raw.get("transaction", raw)
            delta = tx.get("delta", {}) or {}
            keep = {k: delta.get(k) for k in (
                "result", "next_chapter_commitments", "constraints") if delta.get(k)}
            _delta_facts = json.dumps(
                {"上章result": keep.get("result"),
                 "上章承诺": keep.get("next_chapter_commitments"),
                 "上章约束": keep.get("constraints")},
                ensure_ascii=False, indent=1)
            fs = (json.loads((book / _TRACKING_DIR / "_tracking-state.json")
                             .read_text(encoding="utf-8")).get("foreshadow") or {})
            ids = sorted(fs.keys(), key=lambda k: (len(k), k)) if isinstance(fs, dict) else []
            _delta_facts += f"\n既有伏笔编号最大值: {ids[-1] if ids else 'F000'}"
    except Exception:  # noqa: BLE001 — 追踪缺失时事实锚降级，不炸规划
        _delta_facts = ""
    sample = ""
    if prev_outline and Path(prev_outline).is_file():
        try:
            sample = Path(prev_outline).read_text(encoding="utf-8")[:3200]
        except OSError:
            sample = ""
    return (ctx_abs + ("\n\n" + _delta_facts if _delta_facts else ""))[:3500], sample


def _canonicalize_plan_body(chapter_id: int, body: str) -> str:
    """生成细纲的结构归一（2026-10-24）：模型偶发输出裸文本标题（缺 #/## 前缀），
    packet/prealign 解析即 PARTIAL。这里只补标题层级，正文一字不动：
      - 首 3 个非空行内，`第N章 …` 裸行 → `# 第N章 …`
      - 已知小节名裸行 → `## 小节名`（幂等：已带 # 前缀的原样保留）
      - `第N幕` 裸行（幕标题行）→ `## ` 前缀
    """
    lines = body.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    _seen_heading = 0          # 已见非空行数（限首行判定）
    _heading_done = False
    _in_voice = False
    for ln in lines:
        s = ln.strip()
        if not s:
            out.append(ln)
            continue
        # 小节游标（归一在补 ## 之前先记意图）：
        if s.startswith("#") and "出场角色" in s and "锚点" in s:
            _in_voice = True
        elif s.startswith("#"):
            _in_voice = False
        # C-CANON@1（2026-11-06）：兼容形态字段行 → 权威形态小节（幂等）。
        # 只改行前缀/位置，不改任何条目的一个字；归一后二次运行无字段行可改。
        _m_voice_field = re.match(
            r"^[-*]\s*\*{0,2}出场角色\s*voice\s*锚点\*{0,2}\s*[：:]\s*(.*)$", s)
        if _m_voice_field:
            out.append("## 出场角色voice锚点")
            _in_voice = True
            _voice_tail = _m_voice_field.group(1).strip()
            if _voice_tail:
                # 冒号后同行有内容 → 作为首条 `- ` 子项（内容原样保留）
                out.append(f"- {_voice_tail}")
            _seen_heading += 1
            continue
        # 幕标题缺「· 名」：`## 第X幕（场景：.*）…` → 用场景值作幕名（packet 正则要求
        # `第X幕 · 名` 形态，场景值不承载信息量，属格式补全而非内容创作）。
        _m_act = re.match(r"^(#{1,4})?\s*(第[一二三四五1-9]幕)\s*（场景：([^）]+)）\s*(.*)$", s)
        if _m_act and (s.startswith("#") or re.match(r"^第[一二三四五1-9]幕", s)):
            hashes = _m_act.group(1) or "##"
            act, scene, tail = _m_act.group(2), _m_act.group(3), _m_act.group(4)
            out.append(f"{hashes} {act} · {scene}（场景：{scene}）{tail}".replace("（）", ""))
            _in_voice = False
            _seen_heading += 1
            continue
        if not _heading_done and _seen_heading < 3:
            m = re.match(r"^第\s*0*(\d+)\s*章\b\s*(.*)$", s)
            if m and int(m.group(1)) == chapter_id:
                out.append(f"# 第{chapter_id}章 {m.group(2).strip()}")
                _heading_done = True
                _seen_heading += 1
                _in_voice = False
                continue
        _seen_heading += 1
        if s.startswith("#"):
            out.append(ln)
            continue
        known_sections = ("出场角色voice锚点", "章首钩子", "收益兑现", "目标情绪", "爽点", "落点")
        if s in known_sections:
            out.append(f"## {s}")
            _in_voice = s == "出场角色voice锚点"
            continue
        # voice 锚点条目缺 `- ` 列前缀（packet 只认「- <名>：<内容>」形态）：
        if _in_voice and re.match(r"^[^\-#].{0,24}：", s):
            out.append(f"- {s}")
            continue
        # 幕五要素条目：packet 只认 `- **A**：内容`（加粗+列前缀）；模型偶发输出裸 `A：`：
        m_pow = re.match(r"^([A-F])\s*[：:]\s*(.+)$", s)
        if m_pow:
            out.append(f"- **{m_pow.group(1)}**：{m_pow.group(2)}")
            continue
        if s.startswith("本幕内容：") and not s.startswith("- "):
            out.append(f"- {s}")
            continue
        if re.match(r"^第\s*[一二三1-9]\s*幕\b", s):
            out.append(f"## {s}")
            continue
        out.append(ln)
    return "\n".join(out)


def step_plan_outline(book: Path, chapter_id: int) -> dict:
    """0b 自动规划：细纲缺失时由 nww-architect 生成该章细纲并落盘。可独立调用（UI 去堵按钮）。"""
    _last = _last_committed_chapter(book)
    outline, err = _find_outline_file(book, chapter_id), None
    if outline is not None and outline.is_file():
        ok, _why = _is_valid_chapter_outline(outline.read_text(encoding="utf-8"))
        if ok:
            return {"ok": True, "detail": "细纲已存在且有效，无需规划", "path": str(outline)}
    vol_text = _read_vol_outline_text(book)
    if not vol_text.strip():
        return {"ok": False, "detail": "卷纲缺失：既无独立细纲也无可依据的卷纲弧线，规划层无法起步"}
    facts, sample = _plan_outline_context(book, chapter_id)
    agent = registry.get_agent("nww-architect")
    rules = _planner_prompt_rules(chapter_id)
    prompt = (
        f"书项目根: {book}\n目标章: 第{chapter_id}章（上次已提交到第{_last or 0}章）\n\n"
        "## 卷线弧线（唯一剧情来源，逐章细纲必须从中推进）\n" + vol_text + "\n\n"
        "## 书内事实锚（追踪上下文 + 上章 delta；禁止与它冲突）\n" + (facts or "（追踪缺失，先按第一卷弧线起步）") + "\n\n"
        "## 格式样板（上一章细纲，仅学格式与密度，严禁照抄剧情）\n" + (sample or "（无样板：按输出硬约束第3/4/5条的固定小节生成）") + "\n\n"
        + rules
    )

    _errs: list[str] = []

    def _ask(hint: str) -> str:
        # 防卡死（2026-10-25）：规划是产出型环节但非正文——给它比 advisory 略宽的
        # 有界上限（细纲 6000 tokens 需要生成时间），超时按产出为空走既有失败分支
        # （ok=False 提示重试），不挂死在 chrome 桥 900s 上。
        return _run_agent_bounded(agent, "写前规划（生成逐章细纲，只输出 Markdown）",
                                  prompt + hint,
                                  timeout=max(_ADV_TIMEOUT_S, 360.0),
                                  temperature=0.5, max_tokens=6000,
                                  err_out=_errs)

    body = ""
    for _i, hint in enumerate(("", "\n\n重申：只输出细纲 Markdown 本体；若你刚才输出了解释/清点/代码块，现在重新输出。")):
        try:
            body = _ask(hint)
            if _is_valid_chapter_outline(body)[0]:
                break
        except Exception as exc:  # noqa: BLE001 — 两次都失败才上抛
            if _i == 1:
                return {"ok": False, "detail": f"规划 LLM 调用失败: {exc}"}
            continue
    if not body.strip():
        # 2026-10-30：透传 _run_agent_bounded 收集的真实失败原因（端点/模型/HTTP 状态），
        # 不再只报「规划产出为空」——否则端点配置坏了也查不出方向。
        why = "；".join(dict.fromkeys(_errs)) if _errs else "两次调用均返回空"
        return {"ok": False, "detail": f"规划产出为空（{why}）"}
    body = _canonicalize_plan_body(chapter_id, body)
    # 2026-11-01：规划产物落盘前补齐标准字数两行（模型不保证输出，卷纲/bible 才是权威）。
    # 无权威来源时原样返回（不编造数值），由 wordcount advisory 照常报告。
    body = _ensure_outline_wordcount_lines(body, book, chapter_id)
    ok_why = _is_valid_chapter_outline(body)
    if not ok_why[0]:
        return {"ok": False, "detail": f"规划细纲结构无效: {ok_why[1]}（未落盘，可重试）"}
    # 2026-11-04（Step 2 写前门禁）：开启上下章义务时校验义务段齐备，缺失则退回重规划一次。
    if _outline_contract_enabled():
        _voc = _get_outline_contract_validator()
        if _voc is not None:
            _valid, _reason = _voc(body)
            if not _valid:
                _hint = (
                    f"\n\n写前门禁未过（{_reason}）。请补上 `## 上章承接` / `## 下章铺垫` / "
                    f"`## 读者体验合同`（九行 bullet）三段并填实，仅输出细纲 Markdown 本体。"
                )
                try:
                    body = _ask(_hint)
                    if not _is_valid_chapter_outline(body)[0]:
                        return {"ok": False, "detail": "规划细纲结构无效（门禁重规划后仍为空壳）"}
                    body = _canonicalize_plan_body(chapter_id, body)
                    body = _ensure_outline_wordcount_lines(body, book, chapter_id)
                    _valid2, _reason2 = _voc(body)
                    if not _valid2:
                        return {"ok": False, "detail": f"写前门禁未过（重规划后仍：{_reason2}）"}
                except Exception as exc:  # noqa: BLE001 — 重规划失败如实上抛
                    return {"ok": False, "detail": f"规划 LLM 调用失败（门禁重规划）: {exc}"}
    o_dir = book / _OUTLINE_DIR
    o_dir.mkdir(parents=True, exist_ok=True)
    out = o_dir / f"细纲_第{chapter_id:03d}章.md"
    out.write_text(body.strip() + "\n", encoding="utf-8")
    return {"ok": True, "detail": f"细纲已生成: {out.relative_to(book).as_posix()}（{len(body)} 字）",
            "path": str(out)}


def _planner_enabled() -> bool:
    """0b 自动规划开关：缺省开启；NWW_PLANNER=0 回到原 BLOCK 语义。"""
    return os.environ.get("NWW_PLANNER", "1") != "0"



def _pending_uncommitted_chapter(book: Path, chapter_id: int, last: int) -> int | None:
    """悬空章检测（P2.3-A 生产游标阻断）：返回前章未 commit 的章号。

    P2.3-A：前章（chapter_id-1）必须 COMMITTED，否则 BLOCK。
    长篇生产不是独立任务——前章未 commit，后章不应生成。
    不再跳过 REVIEW_FAILED artifact：REVIEW_FAILED 也是未 commit 状态。
    """
    for m in range(last + 1, chapter_id):
        if _resolve_existing_body(book, m) is not None:
            return m
    return None


def _check_failed_artifact_recovery(book: Path) -> list[dict]:
    """P2.3 修正：Recovery owner = System，不是用户修改。

    撤销原设计：body_md5 变化 → RECOVERY_PENDING（错误——用户修改 ≠ System Recovery）。
    正确：系统自动执行 Recovery Attempt（rewrite/revise），重新经过完整 Review Contract。
    """
    # 原 md5 变化检测逻辑已撤销，保留空函数避免调用方报错
    return []
    import hashlib as _hashlib
    ft_dir = book / _TRACKING_DIR / "failed_transactions"
    if not ft_dir.is_dir():
        return []
    recovered = []
    for meta_file in sorted(ft_dir.glob("chapter_*.json")):
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if meta.get("status") != "REVIEW_FAILED":
            continue
        if meta.get("canonical") is not False:
            continue
        ch = meta.get("chapter")
        if not ch:
            continue
        # 检查正文是否变化
        body = _resolve_existing_body(book, ch)
        if body is None:
            continue
        cur_md5 = _hashlib.md5(body.read_bytes()).hexdigest()
        orig_md5 = meta.get("body_md5", "")
        if cur_md5 != orig_md5:
            # body 已被修改 → RECOVERY_PENDING
            meta["recovery_status"] = "RECOVERY_PENDING"
            meta["body_md5_before_recovery"] = orig_md5
            meta["body_md5_current"] = cur_md5
            meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            recovered.append({"chapter": ch, "status": "RECOVERY_PENDING", "old_md5": orig_md5, "new_md5": cur_md5})
    return recovered


def _write_pending_commit_ledger(book: Path, chapter_id: int, reason: str) -> None:
    """P2（外脑裁定）：commit BLOCK 时落恢复台账到 追踪/pending_commits/，避免人工猜断点。fail-soft。"""
    try:
        d = book / _TRACKING_DIR / "pending_commits"
        d.mkdir(parents=True, exist_ok=True)
        # 2026-10-03 外脑裁定：台账必须自带「是否已过时」的判据，不能依赖调用方清除。
        # recorded_state_revision = 落台账那一刻的 state_revision；任何读取方只要发现
        # 当前 state_revision > 该值，即可判定「commit 已发生、本台账已失效」，
        # 无需依赖某个 pipeline 包装层恰好执行了清除动作。
        _rev_now = None
        try:
            _rev_now = json.loads(
                (book / _TRACKING_DIR / "_tracking-state.json").read_text(encoding="utf-8")
            ).get("state_revision")
        except Exception:  # noqa: BLE001 — 读不到就留空，判据退化为「无法判定」
            _rev_now = None
        payload = {
            "chapter": chapter_id,
            "delta": f"delta_第{chapter_id:03d}章.json",
            "reason": reason[:2000],
            "recoverable": True,
            "status": "pending",
            "recorded_state_revision": _rev_now,
            "stale_if_state_revision_gt": _rev_now,
            "retry_hint": f"在 Web 端对第{chapter_id}章执行「重写」：基于当前权威重新生成并提交；提交成功后本记录自动清除。",
            "recorded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        (d / f"第{chapter_id:03d}章.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001 — 台账写失败不影响主链报错
        pass


def _clear_pending_commit_ledger(book: Path, chapter_id: int) -> None:
    """commit 成功后清除该章恢复台账（失效即删）。fail-soft。"""
    try:
        p = book / _TRACKING_DIR / "pending_commits" / f"第{chapter_id:03d}章.json"
        if p.exists():
            p.unlink()
    except Exception:  # noqa: BLE001
        pass


def _env_flag_on(name: str, default: str = "1") -> bool:
    """环境变量布尔开关（默认开）。置 0/false/off/no 视为关。"""
    return (os.environ.get(name, default) or default).strip().lower() not in (
        "0", "false", "off", "no", "")


# E0 黄金三章链 G1–G9（= 三九方法论），仅第 1–3 章适用。
# ★唯一事实源 = skills/nww-novel-skill/skills/prealign/SKILL.md §1B：运行时**直接解析该文件**
#   取判据（_e0_items()）——SKILL.md 修订后无需改代码，也就不存在两侧漂移。
#   下方 _PREALIGN_E0_ITEMS 仅为**兜底副本**（SKILL.md 缺失/格式异常时才用），
#   其「G编号+名称」与 SKILL.md 的一致性由 tests/test_prealign_e0_repair.py 守护。
_PREALIGN_REPAIR_HEAD = "## 写前对齐补充（自动）"

_PREALIGN_E0_ITEMS = (
    "G1 章间升级（第1→2章、第2→3章的矛盾/资产/情绪/关系有推进，非重复）",
    "G2 章间落差（章与章之间有落差或新钩子，不是平铺三章同强度）",
    "G3 链完整（三章构成「钩住→立住→可追」完整链）",
    "G4 过渡自然（前章末钩子本章开头接住，无「摆好悬念转头忘」）",
    "G5 整体追读力（读完前三章有理由翻第4章，硬钩子/软钩子任一即可）",
    "G6 金手指基点闭环（前三章内完成「发现→验证→使用」，第3章结束前主角至少一次主动用金手指解决冲突）",
    "G7 开篇爽点密度（第1章1000字内出爽点或期待点；前三章每章至少一个可感知收益）",
    "G8 卖点显性化（第1章结束前读者能回答「这本书的卖点是什么」；第3章结束前完成一次展示→兑现）",
    "G9 金手指剧情引擎（剧情由金手指特性/限制/代价驱动，主角与金手指有互动，非「只给力量的死外挂」）",
)  # ← 兜底副本：判据文本请改 SKILL.md，勿在此手改


# E0 唯一事实源定位（与 registry/toolbox 同约定：parents[4] = 仓库根）。
_PREALIGN_SKILL_MD = (
    Path(__file__).resolve().parents[4]
    / "skills" / "nww-novel-skill" / "skills" / "prealign" / "SKILL.md"
)
_E0_CACHE: dict = {"key": None, "items": None}
# SKILL.md §1B 表格行：| **G<N> <名称>** | <检查内容> | <不通过→补进细纲> |
_E0_ROW_RE = re.compile(r"^\|\s*\*\*G(\d+)\s+([^|*]+?)\*\*\s*\|([^|]*)\|([^|]*)\|")
# §1B 段落（到下一个 ### / ## 标题为止）
_E0_SECTION_RE = re.compile(r"###\s*1B[^\n]*\n(.*?)(?=\n###\s|\n##\s|\Z)", re.S)


def _skill_prealign_path() -> Path:
    """prealign SKILL.md 路径。env `NWW_SKILL_DIR` 可覆盖 skill 包根（部署/测试用）。"""
    _root = os.environ.get("NWW_SKILL_DIR")
    if _root:
        return Path(_root) / "skills" / "prealign" / "SKILL.md"
    return _PREALIGN_SKILL_MD


def _parse_e0_from_skill_md(text: str) -> tuple[str, ...] | None:
    """从 SKILL.md §1B 解析 E0 判据 → ("G1 章间升级（判据；不通过→回补动作）", …)。

    严格校验：段落存在且 G 编号恰为 1–9；任一不符返回 None（调用方回落兜底副本），
    保证「解析不到就绝不用半成品污染 prompt」。"""
    m = _E0_SECTION_RE.search(text or "")
    if not m:
        return None
    rows: dict[int, tuple[str, str, str]] = {}
    for line in m.group(1).splitlines():
        r = _E0_ROW_RE.match(line.strip())
        if not r:
            continue
        rows[int(r.group(1))] = (
            r.group(2).strip(),
            re.sub(r"\s+", " ", r.group(3)).strip(),
            re.sub(r"\s+", " ", r.group(4)).strip(),
        )
    if set(rows) != set(range(1, 10)):
        return None
    out: list[str] = []
    for i in range(1, 10):
        _name, _check, _fix = rows[i]
        _s = f"G{i} {_name}（{_check}"
        if _fix:
            _s += f"；不通过→{_fix}"
        out.append(_s + "）")
    return tuple(out)


def _e0_items() -> tuple[str, ...]:
    """E0 判据（运行时取用）：优先实时解析 SKILL.md（唯一事实源）；失败回落内置兜底副本。

    按 (路径, mtime) 缓存 → SKILL.md 改动后自动重读（dev 热更即生效，无需重启）。
    绝不抛异常：读不到/解码失败/格式异常一律静默回落，保证写章链路永不因此中断。"""
    p = _skill_prealign_path()
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return _PREALIGN_E0_ITEMS
    key = f"{p}|{mtime}"
    if _E0_CACHE.get("key") == key and _E0_CACHE.get("items"):
        return _E0_CACHE["items"]
    try:
        parsed = _parse_e0_from_skill_md(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — 任何读取异常 → 回落，不阻断写章
        parsed = None
    items = parsed or _PREALIGN_E0_ITEMS
    _E0_CACHE["key"], _E0_CACHE["items"] = key, items
    return items


# —— 防卡死（2026-10-25 backup 卡 10 分钟事故）——
# 根因：prealign/红队/回补的 LLM 调用走 run_agent 默认 600s 超时；architect 路由到
# chrome WebUI 桥接端点（llm_client 内超时下限 900s）时，单次挂起即可让 stage 十几
# 分钟无推进（UI 停在上一环节 backup）。辅助环节是 advisory 语义，不允许拖死主链。
_ADV_TIMEOUT_S = float(os.environ.get("NWW_ADVISORY_LLM_TIMEOUT", "180") or 180)


def _run_agent_bounded(agent, session: str, prompt: str, *, timeout: float | None = None,
                       temperature: float = 0.6, max_tokens: int = 1600,
                       err_out: list | None = None) -> str:
    """有界执行辅助 agent 调用：daemon 线程跑 run_agent，主线程 join(timeout)。

    超时返回空串（调用方按「空产出/失败」既有路径处理，均不阻断主链）；线程继续
    在后台跑完但结果被丢弃——run_agent 只产文本不落盘，丢弃安全。这是对 chrome
    桥 900s 超时下限唯一的硬兜底（run_agent 的 timeout 参数对它不生效）。
    err_out（可选出参，2026-10-30）：传入 list 时把真实失败原因写入（err/超时各一条），
    供调用方透传到回执 detail——此前异常被吞成空串，回执只剩「产出为空」无法定位。
    默认 None 时行为与旧版完全一致（既有 12 个调用点零影响）。"""
    import threading
    limit = timeout if timeout is not None else _ADV_TIMEOUT_S
    box: dict = {"text": "", "err": ""}

    def _work():
        try:
            box["text"] = runtime.run_agent(agent, session, prompt,
                                            temperature=temperature, max_tokens=max_tokens,
                                            timeout=limit)
        except runtime.AgentCancelled:
            box["err"] = "cancelled"
        except Exception as exc:  # noqa: BLE001 — 异常也按空产出处理
            box["err"] = str(exc)[:200]

    # 2026-11-08：显式复制上下文，让辅助线程里的模型调用仍归到当前 AI 实况 run/stage
    import contextvars as _contextvars
    _ctx = _contextvars.copy_context()
    t = threading.Thread(target=_ctx.run, args=(_work,), daemon=True, name="adv-agent-bounded")
    t.start()
    t.join(limit)
    if t.is_alive():
        print(f"[ADV_BOUNDED] 辅助 LLM 调用超时（>{limit:.0f}s），放弃结果继续主链", flush=True)
        if err_out is not None:
            err_out.append(f"LLM 调用超时（>{limit:.0f}s）")
        return ""
    if box["err"]:
        print(f"[ADV_BOUNDED] 辅助 LLM 调用失败（按空产出处理）: {box['err']}", flush=True)
        if err_out is not None:
            err_out.append(box["err"])
        return ""
    return box["text"]


def step_prealign(book: Path, chapter_id: int, outline: Path) -> tuple[bool, str, dict]:
    """0. 写前对齐：architect 检查细纲结构层缺口，逐项 Pass/Fail。

    ★2026-10-16 接入三九方法论（E0 黄金三章链 G1–G9）：第 1–3 章额外检查九条，
    并注入前三章细纲提供**跨章视野**——G1/G2/G3/G4 是章间判据，只看单章细纲判不了。
    九条判据**实时取自 SKILL.md §1B**（`_e0_items()`，唯一事实源），改文档即生效。
    env `NWW_PREALIGN_E0=0` 关闭 G1–G9（回到仅 7 项）。

    返回 meta 新增 `fails`（FAIL 行清单），供 step_prealign_repair 自动回补消费。"""
    agent = registry.get_agent("nww-architect")
    text = outline.read_text(encoding="utf-8")
    _checklist = (
        "1. 章尾钩子（类型+内容） 2. Chapter Core（叙事总结/收益兑现） 3. Chapter Goal + 幕 Decision（决策脊柱/选择压力）\n"
        "4. 认知落差/爽点 5. 出场角色 character_contract（voice 锚点） 6. 字数目标 7. Emotion Curve（本章目标统率字段）"
    )
    _e0 = ""
    if chapter_id <= 3 and _env_flag_on("NWW_PREALIGN_E0", "1"):
        _segs: list[str] = []
        _existing_chs: list[int] = []
        _missing_chs: list[int] = []
        for _ch in (1, 2, 3):
            try:
                _p = _find_outline_file(book, _ch)
            except Exception:  # noqa: BLE001 — 缺某章细纲不阻断
                _p = None
            if _p and Path(_p).is_file():
                try:
                    _segs.append(f"---- 第{_ch}章细纲 ----\n{Path(_p).read_text(encoding='utf-8')[:2500]}")
                    _existing_chs.append(_ch)
                except Exception:  # noqa: BLE001
                    pass
            else:
                _missing_chs.append(_ch)
        if _segs:
            _e0 = (
                "\n8. ===== E0 黄金三章链（G1–G9，仅前三章适用）=====\n"
                + "\n".join(f"   {i}. {t}" for i, t in enumerate(_e0_items(), 1))
                + "\n（G1/G2/G3/G4 为跨章判据，请对照下方前三章细纲判定）\n"
                "\n---- 前三章细纲（跨章视野）----\n" + "\n\n".join(_segs) + "\n"
            )
    prompt = (
        f"书项目根: {book}\n目标章: 第{chapter_id}章\n\n"
        f"以下是本章细纲全文：\n{text}\n\n"
        "请按 90 分体系结构层做写前对齐检查，逐项 Pass/Fail：\n"
        + _checklist + _e0 +
        "\n输出格式：每项一行 `PASS <项名>` 或 `FAIL <项名>: <缺什么>`，最后一行总结\n"
        "`结论: READY` 或 `结论: GAP`。不要修改任何文件，只输出检查结果。"
    )
    out = _run_agent_bounded(agent, "写前对齐检查（advisory，不修改任何内容）", prompt,
                             temperature=0.2, max_tokens=1600)
    if not (out or "").strip():
        # P3-11（2026-10-25）：上游 LLM 故障曾致 prealign 静默「不阻断，继续写」
        # （第001章_rewrite_20261025_140735 回执：黄金三章链检查整轮跳过）。空产出重试一次。
        print("[PREALIGN] 首轮产出为空，重试一次", flush=True)
        out = _run_agent_bounded(agent, "写前对齐检查（重试）", prompt,
                                 temperature=0.2, max_tokens=1600)
    ready = "结论: READY" in out
    gap_lines = [ln.strip() for ln in (out or "").splitlines() if ln.strip().startswith("FAIL")]
    return ready, out, {"ready": ready, "gap_count": len(gap_lines), "fails": gap_lines}


def _clean_outline_yaml_codeblock(text: str) -> str:
    """清理细纲开头被模型误写成 markdown 代码块的 YAML frontmatter。

    模型有时把 structural_fingerprint 的 YAML frontmatter 套在 ```yaml ... ``` 里输出，
    甚至不闭合（只有开头 ```yaml 没有结尾 ```），导致整份细纲被渲染成代码块。
    本函数把开头的 ```yaml/```yml 外壳剥掉，只保留里面的 --- frontmatter。
    非代码块形态原样返回（零影响）。"""
    lines = text.splitlines()
    if not lines:
        return text
    # 只处理开头第一行就是 ```yaml/```yml 的情况
    first = lines[0].strip()
    if not (first.startswith("```yaml") or first.startswith("```yml")):
        return text
    # 剥掉开头的 ```yaml 行
    remaining = lines[1:]
    # 找结尾的 ``` 行（可选——模型可能不闭合）
    end_idx = -1
    for i, ln in enumerate(remaining):
        if ln.strip() == "```":
            end_idx = i
            break
    if end_idx >= 0:
        # 闭合代码块：剥掉开头和结尾两行，中间内容保留
        body_lines = remaining[:end_idx]
        tail = remaining[end_idx + 1:]
    else:
        # 不闭合代码块：只剥开头一行，后面全部保留（模型没写结尾 ```）
        body_lines = remaining
        tail = []
    # 组合：frontmatter 内容 + 空行 + 后面的正文
    result = "\n".join(body_lines).rstrip() + "\n"
    if tail:
        result += "\n" + "\n".join(tail).lstrip("\n")
    return result


# ---- C-VOICE-PARSE@1 的 pipeline 消费侧等价实现（2026-11-06）----
# 权威定义在 skills/nww-novel-skill/scripts/nww_packet_builder_v2.py（builder 为唯一事实源），
# 这里只做「与它同语义」的只读消费：pipeline 不再走 engine 副本、也不各自造变体。
# 允许写：权威形态；允许读：权威 + 兼容形态。
_RE_VOICE_HEADING = re.compile(r"^#{2,4}\s*出场角色[^\n]*voice\s*锚点.*$", re.M)
# 允许字段行带缩进（实测第 2/3 章把该字段行嵌在「人物关系和出场顺序」子项里）。
_RE_VOICE_FIELD = re.compile(
    r"^[ \t]*[-*][ \t]*\*{0,2}出场角色[ \t]*voice[ \t]*锚点\*{0,2}[ \t]*[：:][ \t]*(.*)$",
    re.M)
# 历史宽口径标题（`## 出场角色`，存量细纲/既有测试的锚定形态）：只作抽取兜底，
# 是否可用最终由「可解析条目」判据决定，不会把无条目段当锚点回填。
_RE_VOICE_HEADING_LEGACY = re.compile(r"^#{2,4}\s*出场角色[^\n]*$", re.M)
_RE_MD_HEADING_ANY = re.compile(r"^#{2,4}\s*", re.M)
# 条目正则与 builder `_parse_characters` 逐字一致（不得新增/放宽口径）。
_RE_VOICE_ITEM = re.compile(r"^[-*\s]*\*{0,2}(.+?)\*{0,2}[：:]\s*(.+)$", re.M)
_VOICE_BAD_NAME_WORDS = ("口癖", "动作习惯", "禁用", "注意", "要求")


def _indent_width(line: str) -> int:
    """行首缩进宽度（tab 按 4 空格折算，仅用于比较深浅）。"""
    m = re.match(r"[ \t]*", line)
    return len((m.group(0) if m else "").replace("\t", "    "))


def _voice_anchor_seg(text: str, *, allow_legacy: bool = False) -> str:
    """返回 voice 锚点正文段（不含标题/字段行）；两种形态都支持；找不到返回 ""。

    语义与 C-VOICE-PARSE@1（builder `_voice_anchor_seg`）逐条对齐：
      1. 权威形态：`## 出场角色voice锚点`（H2-H4，允许行尾说明）→ 标题行之后到下一个
         `#{2,4}` 标题或文件尾；
      2. 兼容形态：`- **出场角色voice锚点**：`（允许缩进 / 无加粗 / 带空格）。
         - 冒号后同行有内容 → 该内容 + 紧随其后**缩进更深**的子项行（`^[ \\t]+-\\s+`）；
         - 冒号后为空 → 优先收更深缩进的子项；无子项时才回落到「到下一个标题/文件尾」，
           避免把后续兄弟小节（如「情节细化」）误当角色条目；
      3. 历史宽口径 `## 出场角色…` 标题段：**默认不启用**（D2）。新 builder 只认
         权威/兼容形态，pipeline 的可解析判据必须与之一致；仅当调用方明确知道
         自己处理的是旧数据保值抽取（allow_legacy=True）时才启用。
    """
    if not text:
        return ""
    m = _RE_VOICE_HEADING.search(text)
    if m:
        start = m.end()
        nxt = _RE_MD_HEADING_ANY.search(text, start)
        return (text[start:nxt.start()] if nxt else text[start:]).strip()
    fm = _RE_VOICE_FIELD.search(text)
    if fm:
        line_start = text.rfind("\n", 0, fm.start()) + 1
        field_indent = _indent_width(text[line_start:fm.start() + 1])
        inline = (fm.group(1) or "").strip()
        tail_text = text[fm.end():]
        if tail_text.startswith("\n"):
            tail_text = tail_text[1:]  # 去掉字段行自身的行尾换行，真正的空行仍保留
        rest = tail_text.splitlines()
        if inline:
            # 同行有内容：内容即首条，随后只收缩进更深的子项；首个不更深/非子项行结束。
            body = ["- " + inline]
            for ln in rest:
                if not ln.strip():
                    break
                if re.match(r"^[ \t]+-\s+", ln) and _indent_width(ln) > field_indent:
                    body.append(ln.strip())
                    continue
                break
            return "\n".join(body).strip()
        # 冒号后为空：优先收更深缩进的子项（细纲实存写法）。
        body2: list[str] = []
        for ln in rest:
            if not ln.strip():
                if body2:
                    body2.append("")
                continue
            if re.match(r"^[ \t]+-\s+", ln) and _indent_width(ln) > field_indent:
                body2.append(ln.strip())
                continue
            break
        if body2:
            return "\n".join(body2).strip()
        # 无子项的兜底：按契约取到下一个标题行/文件尾（兼容纯段落写法）。
        tail = "\n".join(rest)
        nm = _RE_MD_HEADING_ANY.search(tail)
        return (tail[:nm.start()] if nm else tail).strip()
    if allow_legacy:
        mm = _RE_VOICE_HEADING_LEGACY.search(text)
        if mm:
            start = mm.end()
            nxt = _RE_MD_HEADING_ANY.search(text, start)
            return (text[start:nxt.start()] if nxt else text[start:]).strip()
    return ""


def _voice_parse_items(text: str) -> list[dict]:
    """按 C-VOICE-PARSE@1 条目规则解析 voice 段，返回结构化条目（name/voice/behavior）。

    条目正则、清洗规则、①② 与 `；;。` 拆分、`禁用/不得` 归 behavior —— 全部与 builder
    `_parse_characters` 一致；**不得**放宽到「全文出现 voice 字样即算有锚点」。
    """
    seg = _voice_anchor_seg(text)
    if not seg:
        return []
    chars: list[dict] = []
    for lm in _RE_VOICE_ITEM.finditer(seg):
        name, body = lm.group(1).strip(), lm.group(2).strip()
        # ①-⑩ 开头的是子字段行（口癖/动作习惯…），不是角色条目
        if re.search(r"[①-⑩]", name):
            continue
        name_clean = re.sub(r"（.*?）|\(.*?\)", "", name).strip()
        if (not name_clean or len(name_clean) > 12
                or any(w in name_clean for w in _VOICE_BAD_NAME_WORDS)):
            continue
        if "①" in body:
            parts = [p.strip() for p in re.split(r"[①-⑩]", body) if p.strip()]
        else:
            parts = [p.strip() for p in re.split(r"[；;。]", body) if p.strip()]
        if not parts:
            continue
        behavior = [p for p in parts
                    if ("禁用" in p or p.startswith("禁") or "不得" in p)]
        voice = [p for p in parts if p not in behavior]
        chars.append({"name": name_clean,
                      "voice_constraints": voice,
                      "behavior_constraints": behavior})
    return chars


def _extract_keep_blocks(src: str, *, allow_legacy: bool = False) -> dict:
    """从重写前的细纲中提取必须保值的机器块：structural_fingerprint、字数目标、voice锚点。

    这些块要么被门禁依赖（trope_guard 读 structural_fingerprint），要么是写作者硬约束
    （字数目标行、voice 锚点），重写时不能丢。

    C-KEEP-BLOCKS@1（2026-11-06）：voice 抽取改为双形态（权威标题 / 兼容字段行）。
    keep["voice"] 只存**条目正文段**（不含标题行），供回填时统一加权威标题，
    避免「标题 + 段内标题」重复。

    D2（2026-11-06）：历史 `## 出场角色` 宽口径兜底默认关闭（allow_legacy=False），
    与 builder 口径一致。仅回补链做**保值抽取**（保住旧数据）时显式开启；
    它不参与「新文本是否已可解析」判定，合同闸判定仍只认 voice_anchor_detected。
    """
    keep = {"fingerprint": "", "wordcount": "", "voice": ""}
    # 1) structural_fingerprint：兼容两种形态——顶部 ---YAML frontmatter--- 或正文 ```yaml 块
    fm = re.match(r"^---\n(.*?)\n---\n", src, re.S)
    if fm and "structural_fingerprint" in fm.group(1):
        keep["fingerprint"] = fm.group(0).rstrip()
    else:
        m = re.search(r"```yaml\s*\n(structural_fingerprint:.*?)\n```", src, re.S)
        if m:
            keep["fingerprint"] = "---\n" + m.group(1).strip() + "\n---"
    # 2) 字数目标行（全行锚定，如 "字数目标：2500-2750"）
    m = re.search(r"(?m)^(字数目标[：:].+)$", src)
    if m:
        keep["wordcount"] = m.group(1).strip()
    # 3) 出场角色 voice 锚点段（双形态，只取条目正文；legacy 仅旧数据保值抽取启用）
    keep["voice"] = _voice_anchor_seg(src, allow_legacy=allow_legacy)
    return keep


def _reinject_keep_blocks(new_text: str, keep: dict) -> tuple[str, list[str]]:
    """确定性保值回填（C-KEEP-BLOCKS@1）。返回 (新文本, 精确回填项列表)。

    回填判据不再用「正文小写后是否包含 voice 子串」这类关键字判据（正文出现
    voice/Voice 即永久不触发，RC-3 根因）；改为「新文本是否存在**可解析条目**」（与 builder 同口径）。
    追加时统一用权威形态 `## 出场角色voice锚点`，幂等：第二次运行已可解析 → 不再追加。
    """
    items: list[str] = []
    if keep.get("fingerprint") and "structural_fingerprint" not in new_text:
        # 插入到文件开头
        new_text = keep["fingerprint"] + "\n\n" + new_text
        items.append("fingerprint")
    if keep.get("wordcount") and not re.search(r"(?m)^字数目标[：:]", new_text):
        # 插入到标题后
        new_text = re.sub(r"(?m)^(#\s*.+)$",
                          lambda m: m.group(1) + "\n" + keep["wordcount"],
                          new_text, count=1)
        items.append("wordcount")
    if keep.get("voice") and not _voice_parse_items(new_text):
        body = str(keep["voice"]).strip()
        if body:
            new_text = (new_text.rstrip() + "\n\n## 出场角色voice锚点\n" + body + "\n")
            items.append("voice")
    return new_text, items


# 2026-11-06 细纲资产保护：回补模型偶发把「思考过程 + <tool_call> XML」或桥接
# 结果分区模板当作细纲全文输出，旧代码只检查非空就 write_text 覆盖，导致细纲变成
# 6478B 的思维链残稿、字数两行与结尾段全丢（packet 随即降级 wc_target=0 / PARTIAL）。
# 只认固定污染标记，不设长度/厚度阈值（避免误伤与另造量化指标）；命中即拒写，
# 保住盘上原文。env NWW_PREALIGN_CLEAN=0 回滚。
_PREALIGN_POLLUTION_MARKS = (
    "<tool_call", "<function=", "<think>", "开始执行任务",
    "让我重新检查", "════ 结果分区", "本轮未真实执行任何命令",
)


def _prealign_pollution_mark(text: str) -> str:
    """返回命中的「模型过程文本 / 桥接模板」标记；无命中返回空串。只检测不改写。"""
    s = str(text or "")
    return next((m for m in _PREALIGN_POLLUTION_MARKS if m in s), "")


def step_prealign_repair(book: Path, chapter_id: int, outline: Path,
                         fails: list[str]) -> tuple[bool, str]:
    """写前对齐**整章重写**：把 FAIL 清单交给 architect，基于原细纲重写整章，
    直接覆盖原文件（先备份）。不再追加"写前对齐补充"节。

    2026-10-31 重构：原"只增不改 append 补丁"模式导致正文与补丁自相矛盾、
    细纲里充斥 FAIL 编号/"覆盖旧条"元注释/工程黑话——写作者拿到的是一份
    修复日志而非创作蓝图。现改为重写整章：保留机器依赖块（fingerprint/字数/voice），
    其余按 FAIL 清单一次性修正正文，不再有"正文 vs 补丁"打架。

    env `NWW_PREALIGN_REPAIR=0` 关闭。"""
    if not _env_flag_on("NWW_PREALIGN_REPAIR", "1"):
        return False, "回补已关闭（NWW_PREALIGN_REPAIR=0）"
    agent = registry.get_agent("nww-architect")
    src = outline.read_text(encoding="utf-8")
    # 剥离旧的"写前对齐补充"节（历史遗留），只保留正文
    body_src = src.split(_PREALIGN_REPAIR_HEAD)[0].rstrip()
    # 回补链处理的是**存量细纲**：保值抽取允许历史 `## 出场角色` 宽口径（D2 收窄点），
    # 但下方回填判据 `_voice_parse_items` 仍严格只认权威/兼容形态，保证与合同闸同口径。
    keep = _extract_keep_blocks(src, allow_legacy=True)
    # 2026-11-06 根因修复：原 [:6000] 把 11547B 的细纲砍掉近半喂给 architect，
    # 回补产出必然残缺（实测第 3 情节点截断于「启动一」，细纲从 12721B 掉到 6478B）。
    # writer 链早已移除同类 [:6000] 截断（见 :2277 注释），此处对齐。
    # env NWW_PREALIGN_FULL_SRC=0 逐字回滚到改动前行为。
    _src_for_prompt = (body_src if _env_flag_on("NWW_PREALIGN_FULL_SRC", "1")
                       else body_src[:6000])

    prompt = (
        f"书项目根: {book}\n目标章: 第{chapter_id}章\n\n"
        "以下是本章现有细纲：\n"
        "===== 现有细纲开始 =====\n" + _src_for_prompt + "\n===== 现有细纲结束 =====\n\n"
        "审查发现以下问题需要修正：\n"
        + "\n".join(f"- {_f}" for _f in (fails or [])[:12]) +
        "\n\n"
        "请**重写整章细纲正文**（不是追加补丁，而是直接修正上面的问题），要求：\n"
        "1. 保留上面【必须保留块】中的内容，一个字都不要改：\n"
        "   - structural_fingerprint（YAML 头，门禁依赖）\n"
        "   - 字数目标行\n"
        "   - 出场角色 voice 锚点：必须输出独立二级小节 `## 出场角色voice锚点`，\n"
        "     条目形如 `- **角色名**：①…②…`（角色名 + 至少一条规则：\n"
        "     ①口癖/句式 ②动作习惯 ③禁用表达 ④与谁硬性区分）；\n"
        "     旧字段行 `- **出场角色voice锚点**：…` 仅作历史兼容读取，不得再作为输出形态。\n"
        "     ★ 若你不确定如何重排，原样保留整段条目文字，只保证标题是 `## 出场角色voice锚点`。\n"
        "2. 针对上面每条 FAIL，直接在正文里改掉对应问题——不要写「修复了XX」这种说明，\n"
        "   直接给出修正后的正确内容。\n"
        "3. **用干净的中文叙事写细纲**：\n"
        "   - 禁止出现 FAIL、G1-G9、覆盖旧条、继承上一版、本条继承、新增修复 等审查器元注释\n"
        "   - 禁止用代码块（```包裹任何内容）\n"
        "   - 禁止用 A/B/D/F 这种工程标签代替剧情描述——直接写谁在哪做了什么、发生了什么\n"
        "   - 禁止出现英文枚举值（如 PROVOKE_SLAP）——这些只在 fingerprint YAML 里保留\n"
        "4. 细纲就是给写作者看的「这一章写什么」，不是工程文档。写作者读完应该知道：\n"
        "   开场画面是什么、主角在哪遇到谁、发生了什么冲突、怎么解决、结尾落在哪个画面。\n"
        "5. 直接输出完整的新细纲全文（含必须保留块），不要输出任何解释、不要说「已修复」。\n"
    )
    if keep["fingerprint"] or keep["wordcount"] or keep["voice"]:
        prompt += "\n【必须保留块（原样写回，不得修改）】\n"
        if keep["fingerprint"]:
            prompt += keep["fingerprint"] + "\n\n"
        if keep["wordcount"]:
            prompt += keep["wordcount"] + "\n\n"
        if keep["voice"]:
            # C-KEEP-BLOCKS@1：keep 只存条目正文，这里补回权威标题，让「必须保留块」
            # 自描述（模型不会把裸条目行误认为普通列表）。
            prompt += "## 出场角色voice锚点\n" + keep["voice"] + "\n\n"

    # 2026-11-06 根因修复：原硬编码 3200 tokens 不足以回写整章细纲。实测
    # agnes-3.0-flash 接受 8192/12000/16000 且 finish_reason=stop（8192 产出
    # 3832 可见字）；按原稿字符数动态给、下限 8192。env NWW_PREALIGN_DYN_MAXTOK=0 回滚。
    _prealign_max_tokens = (max(8192, int(len(body_src) * 1.5))
                            if _env_flag_on("NWW_PREALIGN_DYN_MAXTOK", "1") else 3200)
    out = _run_agent_bounded(agent, "写前对齐重写整章", prompt,
                             timeout=max(_ADV_TIMEOUT_S, 300.0),
                             temperature=0.3, max_tokens=_prealign_max_tokens)
    new_text = (out or "").strip()
    # 2026-11-01 加固：LLM 偶发回显历史补丁段标记（## 写前对齐补充（自动））——
    # 重写契约下产出即「新细纲全文」，补丁头属于被废弃的 append 模式残留，必须剥离。
    if new_text.startswith(_PREALIGN_REPAIR_HEAD):
        new_text = new_text[len(_PREALIGN_REPAIR_HEAD):].lstrip("：: \n")
    if not new_text:
        return False, "重写产出为空"
    # 2026-11-06：产出是「思考过程 / 工具调用 XML / 桥接模板」时拒绝写盘。
    # 判据只认固定标记（零误伤），不判长度/必含块，故不影响既有回补契约测试。
    _pollution = _prealign_pollution_mark(new_text)
    if _pollution and _env_flag_on("NWW_PREALIGN_CLEAN", "1"):
        return False, f"回补产出含非细纲内容（{_pollution}），拒绝写盘以保护现有细纲"

    # 确定性保值闸（C-KEEP-BLOCKS@1）：LLM 重写丢了机器块时机械回填。
    # ★ RC-3 根因修复：voice 回填判据 = 新文本是否存在**可解析条目**（与 builder 同口径），
    #   不再用「正文小写后是否包含 voice 子串」这类关键字判据（含 voice 字样就永久不触发）。
    # ★ 幂等：回填统一追加权威形态 `## 出场角色voice锚点`；二次运行已可解析 → 不重复追加。
    new_text, _refilled = _reinject_keep_blocks(new_text, keep)
    if _refilled:
        print(f"[PREALIGN_REPAIR] 保值闸回填: {'、'.join(_refilled)}", flush=True)

    import time as _t
    _stamp = f"{int(_t.time())}"
    bk = outline.with_name(outline.name + f".bak_prealign_{_stamp}")
    _n = 1
    while bk.exists():
        _n += 1
        bk = outline.with_name(outline.name + f".bak_prealign_{_stamp}_{_n}")
    try:
        bk.write_text(src, encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    # 2026-11-06：清理模型误写成代码块的 YAML frontmatter（```yaml 外壳剥掉）
    new_text = _clean_outline_yaml_codeblock(new_text)
    try:
        outline.write_text(new_text + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return False, f"细纲写入失败: {exc}"
    return True, f"已重写整章修正 {len((fails or [])[:12])} 项 → {outline.name}（原文件备份 {bk.name}）"


def step_outline_redteam(book: Path, chapter_id: int, outline: Path) -> tuple[list[str], str]:
    """P1-5 细纲红队审查（2026-10-25）：以找茬者视角**只审逻辑不审文笔**。

    根因归因（第001章外部审稿）：「神识探查分辨不出皮肤表面膏体」「禁武令触发标准
    模糊」「反派被废真气后反应降智」「巧合密度过高」——这些设定级漏洞源头在细纲/
    世界观，而全链以纲证纲（I-I 对照细纲给分），细纲有漏洞时正文越忠实得分越高。
    机械层永远拦不住语义漏洞，唯一对症环节是写前逻辑红队。产出 FAIL 行（与
    prealign 同格式），交既有回补通道合并修复。NWW_OUTLINE_REDTEAM=0 关闭。"""
    if not _env_flag_on("NWW_OUTLINE_REDTEAM", "1"):
        return [], "红队审查关闭（NWW_OUTLINE_REDTEAM=0）"
    agent = registry.get_agent("nww-architect")
    text = outline.read_text(encoding="utf-8")
    prompt = (
        f"书项目根: {book}\n目标章: 第{chapter_id}章\n\n"
        f"以下是本章细纲全文：\n{text}\n\n"
        "你是敌意审稿人（红队）。忽略文笔与爽点结构（那是写前对齐的职责），只攻击**逻辑**，"
        "逐项检查并输出 FAIL 行：\n"
        "1. 规则/能力边界：本章启用或依赖的每条规则（金手指、店规、世界规则），追问三点——"
        "它为什么成立？判定标准是否明确（何时触发/何时不触发）？推到极限会不会被滥用或得出荒谬结论？"
        "（例：某手段能骗过某探查方式，需要论证该探查方式的感知边界，而不是一句话断言）\n"
        "2. 人物行为自利性：每个关键配角/反派的行动，是否由其身份、利益、性格驱动？"
        "凡「一个正常人/该身份的人不会这么做，只有作者需要他这么做」即 FAIL（含：受挫后反应与身份不符、"
        "该上报/追问却轻易退场、危机中做与处境优先级脱节的事）。\n"
        "3. 巧合密度：列出本章依赖「恰好/正好/来得巧」的事件链；无因果铺垫的巧合 ≥2 处即 FAIL。\n"
        "4. 能力与知识来源：主角每个超出其身份的判断/知识/技能，是否有本章之前已建立的来源？没有即 FAIL。\n"
        "5. 转折时机：关键转折（危机复发、钩子触发）是否发生在逻辑上必然的时间点？"
        "「精确到像按了开关」的转折即 FAIL，给出更自然的时点建议。\n"
        "输出格式：每条一行 `FAIL <问题>: <为什么是漏洞> → <修法建议>`；"
        "全部通过则输出 `结论: CLEAN`。不要修改任何文件。不要评论文笔。"
    )
    out = _run_agent_bounded(agent, "细纲红队审查（逻辑找茬）", prompt,
                             temperature=0.4, max_tokens=1400)
    if not (out or "").strip():
        # P3-11 同源：红队空产出重试一次
        out = _run_agent_bounded(agent, "细纲红队审查（重试）", prompt,
                                 temperature=0.4, max_tokens=1400)
    fails = [ln.strip() for ln in (out or "").splitlines() if ln.strip().startswith("FAIL")]
    return fails, out or ""


def _run_prealign_with_repair(book: Path, chapter_id: int, _finish) -> None:
    """写前对齐编排：检查 → 自动回补细纲 → 重跑，直到全 Pass 或次数用尽。

    用户 2026-10-16 裁决：「流水线发现问题要**纠正**，不是只报告或直接停——停了还得解决
    才能完成流程」。故此处不做纯 advisory，而是自动回补；但次数用尽仍不 Ready 时
    **绝不阻断主链**（带残留缺口继续写），保证流程一定能走完。
    循环上限 env `NWW_PREALIGN_MAX_REPAIR`（默认 2）。

    总预算 watchdog（2026-10-25 backup 卡 10 分钟事故）：检查+红队+回补全部走
    _run_agent_bounded，最坏叠加仍可达 30+ 分钟——编排层再设总预算
    `NWW_PREALIGN_BUDGET`（默认 480s），每步前检查剩余，耗尽即跳过剩余辅助步骤，
    保证写章主链不被辅助环节拖死（advisory 语义不得 BLOCK 主链，同样不得拖死主链）。

    语域感知（2026-10-25 感知-确认型裁决）：对齐前先跑 ensure_book_register——
    bible 缺 era_constraints 时按建书素材自动推断档位并锁定（高置信），
    低置信（跨空间题材等）按 strict 先行 + 待确认问题透出到 stage detail，
    作者在聊天里一句话即可改档。异常绝不阻断对齐主链。"""
    import time as _time_mod
    _t0 = _time_mod.monotonic()
    try:
        _budget = max(60.0, float(os.environ.get("NWW_PREALIGN_BUDGET", "480") or 480))
    except ValueError:
        _budget = 480.0

    def _budget_left() -> float:
        return _budget - (_time_mod.monotonic() - _t0)

    _era_note = ""
    try:
        from .era_inference import ensure_book_register
        _era = ensure_book_register(book)
        if _era.get("note"):
            _era_note = "；" + str(_era["note"])
    except Exception:  # noqa: BLE001 — 感知层失败不阻断写前对齐
        pass
    try:
        _ol = _find_outline_file(book, chapter_id)
        if not _ol:
            _finish("prealign", False, "细纲不存在，跳过")
            return
        _ol = Path(_ol)

        # 2026-11-05 黄金三章整体补齐：第1~3章写前对齐时，若后续章细纲缺失，
        # 自动调用 planner 生成，确保 G1/G2/G3/G6/G8/G9 跨章判据有依据可判。
        # 根因：原逻辑只有第1章细纲时，architect 对跨章判据判 FAIL（"无法判定"），
        # repair 循环只能改第1章，永远补不出第2/3章 → 两轮无用 repair 后带缺口跳过。
        # 修复：编排层在首次 prealign 前自动补齐缺失的后续章细纲，再做跨章检查。
        _triad_log: list[str] = []
        if chapter_id <= 3:
            for _ch_future in (2, 3):
                if _ch_future == chapter_id:
                    continue
                try:
                    _exists = _find_outline_file(book, _ch_future)
                except Exception:  # noqa: BLE001
                    _exists = None
                if _exists is not None:
                    continue
                if _budget_left() <= 60:
                    _triad_log.append(f"预算不足，第{_ch_future}章细纲未补齐")
                    break
                try:
                    _p_res = step_plan_outline(book, _ch_future)
                    if _p_res.get("ok"):
                        _triad_log.append(f"自动补齐第{_ch_future}章细纲")
                    else:
                        _triad_log.append(f"第{_ch_future}章细纲补齐失败: {str(_p_res.get('detail', ''))[:60]}")
                except Exception as exc:  # noqa: BLE001 — 补齐失败不阻断主链
                    _triad_log.append(f"第{_ch_future}章细纲补齐异常: {exc}")

        _ready, out, meta = step_prealign(book, chapter_id, _ol)
        # P1-5 细纲红队（逻辑找茬）：只跑一次，FAIL 并入回补清单（复用既有修复通道）；
        # 预算耗尽跳过（辅助环节，宁缺毋拖）
        _redteam_fails: list[str] = []
        _rt_err = ""
        if _budget_left() > 60:
            try:
                _redteam_fails, _rt_out = step_outline_redteam(book, chapter_id, _ol)
            except Exception as exc:  # noqa: BLE001 — 红队异常不阻断写前对齐主链
                _rt_err = str(exc)
        else:
            _rt_err = f"预算剩余不足（{max(0, _budget_left()):.0f}s），红队跳过"
        if _redteam_fails:
            # 红队缺口只并入回补清单，不并入 prealign 自身的 remaining_gaps 计数
            #（语义区分：remaining_gaps = 写前对齐结构层缺口；redteam_count = 逻辑缺口）
            _repair_fails = list(meta.get("fails") or []) + _redteam_fails
            _ready = False
        else:
            _repair_fails = list(meta.get("fails") or [])
        _left = max(0.0, _budget_left())
        _rounds, _log = 0, []
        try:
            _max = max(0, int(os.environ.get("NWW_PREALIGN_MAX_REPAIR", "2") or 2))
        except ValueError:
            _max = 2
        while (not _ready) and _rounds < _max and _budget_left() > 90:
            _rounds += 1
            _ok, _msg = step_prealign_repair(book, chapter_id, _ol, _repair_fails)
            _log.append(f"第{_rounds}轮{'成功' if _ok else '失败'}")
            if not _ok:
                break
            if _budget_left() <= 90:
                _log.append("预算耗尽，停止回补（带缺口继续）")
                break
            _ready, out, meta = step_prealign(book, chapter_id, _ol)
        _gaps = len(meta.get("fails") or [])
        _detail = ("写前对齐：" + ("READY" if _ready else f"仍有 {_gaps} 项缺口（已回补 {_rounds} 轮，带缺口继续写）")
                   + ("；" + " / ".join(_log) if _log else "")
                   + ("；" + " / ".join(_triad_log) if _triad_log else "")
                   + _era_note)
        if _redteam_fails:
            _detail += f"；红队逻辑审查：{len(_redteam_fails)}项缺口"
        if _rt_err:
            _detail += f"；红队: {_rt_err[:120]}"
        _detail += f"（辅助环节总预算 {_budget:.0f}s，剩余 {_left:.0f}s）"
        _finish("prealign", True, _detail, output=out or "", ready=_ready,
                gap_count=meta.get("gap_count", 0), repair_rounds=_rounds, remaining_gaps=_gaps,
                redteam_count=len(_redteam_fails))
    except Exception as exc:  # noqa: BLE001 — 写前对齐异常不阻断主链
        _finish("prealign", False, f"prealign 异常（不阻断，继续写）: {exc}")


# ---------------------------------------------------------------------------
# P0/P2 修复（2026-10-25 外部审稿归因）：packet 合同修复闸 + 代词性别闸 + 数字锚审计
# 根因（第001章 10 次生成回执实证）：
#   ① character_contract/facts.confirmed 全程为空，PARTIAL 不阻断 → voice 锚点/
#      角色卡从未进入 writer 输入（女作者笔下的女主通篇写成「他」）；
#   ② 细纲数字承诺（首单两块灵石）被 writer 改写（十块）且被 promise_audit 用正文
#      反推洗白 → 需要独立于 LLM 审稿的确定性数字锚比对。
# ---------------------------------------------------------------------------

_CAST_SENT_SPLIT = re.compile(r"[。；;\n]+")
# D3（2026-11-06）：出场名候选的噪声过滤。beats ID（B1/beat_1/beats_fallback）与纯编号
# 不是角色名；长句也不作为角色名（否则 R4-AC2 诊断里全是句子，且会误当"卡名⊂出场名"载体）。
_CAST_NOISE_RE = re.compile(r"^(?:[Bb]\d+|beat[_\- ]?\d+|beats?_[A-Za-z_]+|\d+)$")
_CAST_MAX_NAME_LEN = 24


def _bible_protagonist_name(book: Path) -> str:
    """bible.json protagonist.name（去掉前导「主角」）；缺失/损坏返回 ""。"""
    try:
        bible = json.loads((book / "bible.json").read_text(encoding="utf-8"))
        return re.sub(r"^主角", "",
                      str((bible.get("protagonist") or {}).get("name") or "")).strip()
    except Exception:  # noqa: BLE001 — bible 缺失按无主角锚处理
        return ""


def _bible_protagonist_description_name(book: Path) -> str:
    """从 `bible.protagonist.description` 抽取「主角X」里的显式名字；无则 ""。

    D1 补位（2026-11-06）：生产书 `bible.protagonist.name` 是空串，但 description 首句
    明写「主角林小凡，…」。这是**作者显式声明**的主角名，不是推断；只认「主角」+ 2-4
    字中文 + 紧随标点/空白/结尾的形态，避免把「主角是一位…」误抽成名字。
    """
    try:
        bible = json.loads((book / "bible.json").read_text(encoding="utf-8"))
        desc = str((bible.get("protagonist") or {}).get("description") or "")
    except Exception:  # noqa: BLE001 — bible 缺失/损坏按无声明处理
        return ""
    m = re.search(r"主角\s*[：:]?\s*([\u4e00-\u9fa5]{2,4})"
                  r"(?=[，,。；;、\s）)]|$)", desc)
    if not m:
        return ""
    name = m.group(1).strip()
    if re.match(r"^(是|为|叫|名|的|在|从|与)", name):
        return ""
    return name


def _protagonist_anchor_for_fallback(book: Path) -> tuple[set[str], str]:
    """兜底用主角锚：(名字集合, 来源)。

    D1 根因（2026-11-06）：生产书 `bible.protagonist.name` 是空串（只在 description
    写了「主角林小凡…」）→ 旧兜底只看 bible 名 → 第三级「主角无条件注入」恒 False。
    回退链（不猜、只认作者显式声明）：
      bible.name → 追踪 state 印证 → 唯一性别卡 → bible.description 的「主角X」
      → 全部失败返回空集（**不**把唯一角色卡擅自当主角，多书会误伤）。
    来源值：bible / trace_state / gender_card / bible_description / none。
    """
    name = _bible_protagonist_name(book)
    if name:
        return {name}, "bible"
    try:
        names, source, _card = _resolve_protagonist_anchor(book)
    except Exception:  # noqa: BLE001 — 锚链异常按无主角处理，不阻断兜底
        return set(), "none"
    clean = {str(n).strip() for n in (names or set()) if str(n or "").strip()}
    if clean:
        return clean, (source or "none")
    desc_name = _bible_protagonist_description_name(book)
    if desc_name:
        return {desc_name}, "bible_description"
    return set(), "none"


def _protagonist_names_for_fallback(book: Path) -> set[str]:
    """D1：主角名集合（bible 名优先；为空时回落 `_resolve_protagonist_anchor`）。"""
    return _protagonist_anchor_for_fallback(book)[0]


def _chapter_cast_names(packet: dict, chapter_id: int) -> list[str]:
    """本章出场角色名候选（C-CARD-FALLBACK@1）。

    来源按可信度排序：
      ① scene_chain 的结构化角色字段（characters/cast/角色/人物…）；
      ② intent / scene_chain 文本中的「出场顺序 / 出场角色」行（builder 已把细纲该行
        解析进 scene_chain/intent，故对这些文本扫描即可，不重复读细纲文件）；
      ③ 文本短句（作为「卡名 ⊂ 出场名」的载体，保持旧「卡名出现在本章上下文」语义）。
    D3（2026-11-06）：过滤 beats ID / 纯编号等非角色 token，且丢弃长度 >24 的整句；
    去重后最多 24 条，供匹配与失败诊断共用（诊断里不再出现 B1/beat_1/长句子）。
    """
    names: list[str] = []

    def _add(value: object) -> None:
        s = re.sub(r"^[\s\-*#>|]+|[\s\-*#>|]+$", "", str(value or "")).strip()
        s = re.sub(r"^[*\s]+|[*\s]+$", "", s)
        if not s or len(s) > _CAST_MAX_NAME_LEN:
            return
        if _CAST_NOISE_RE.match(s):
            return
        if s not in names:
            names.append(s)

    texts: list[str] = []
    scene_chain = packet.get("scene_chain") or []
    if isinstance(scene_chain, (list, tuple)):
        for item in scene_chain:
            if isinstance(item, dict):
                for key in ("characters", "characters_present", "cast",
                            "角色", "人物", "出场角色"):
                    v = item.get(key)
                    if isinstance(v, str) and v.strip():
                        texts.append(v)
                        for tok in re.split(r"[、，,;；/|]+", v):
                            _add(tok)
                    elif isinstance(v, (list, tuple)):
                        for x in v:
                            _add(x)
                for v in item.values():
                    if isinstance(v, str) and v.strip():
                        texts.append(v)
            elif isinstance(item, str):
                texts.append(item)
    elif isinstance(scene_chain, str):
        texts.append(scene_chain)

    intent = packet.get("intent") or {}
    if isinstance(intent, dict):
        for v in intent.values():
            if isinstance(v, str) and v.strip():
                texts.append(v)
            elif isinstance(v, (list, tuple)):
                texts.extend(str(x) for x in v if str(x).strip())
    elif isinstance(intent, str):
        texts.append(intent)
    for extra_key in ("_outline_excerpt", "outline_excerpt"):
        v = packet.get(extra_key)
        if isinstance(v, str) and v.strip():
            texts.append(v)

    blob = "\n".join(texts)
    # ②「出场顺序 / 出场角色」行：按顿号/逗号/斜杠切名（权威候选）
    for line in blob.splitlines():
        if re.search(r"出场(顺序|角色|人物|名单)", line):
            for tok in re.split(r"[、，,;；/|]+", line):
                _add(tok)
    # ③ 文本短句：卡名 ⊂ 短句即可命中（旧语义载体；长句与 beats ID 由 _add 过滤）
    for sent in _CAST_SENT_SPLIT.split(blob):
        _add(sent.strip())
    return names[:24]


def _card_hit(name: str, cast: list[str], protagonist_names: set[str],
              out_count: int = 0) -> bool:
    """三级匹配（C-CARD-FALLBACK@1，按序）：全等 → 卡名 ⊂ 出场名（≥2 字）→ 主角无条件 1 条。

    D1：第三级用**主角名集合**（bible 名 → tracking state → 唯一性别卡），而非只看
    bible.name；`in` 关系双向（卡名 ⊂ 主角名 / 主角名 ⊂ 卡名）。
    """
    if name in cast:
        return True
    if len(name) >= 2 and any(name in c for c in cast):
        return True
    if not out_count:
        for pn in (protagonist_names or set()):
            if pn and (pn in name or name in pn):
                return True
    return False


def _card_fallback_diagnosis(book: Path, packet: dict, chapter_id: int) -> dict:
    """角色卡兜底失败证据（R4-AC2）：卡目录存在性 / 现有卡名清单 / 本章出场名 / 命中项。

    供 C-GATE@1 写入 `meta.card_fallback`，前端任务详情据此展示「时点数据缺失」证据，
    而不是一句「无本章角色卡可兜底」。D1：额外带 `protagonist_names` /
    `protagonist_source`（bible/trace_state/gender_card/none），便于区分
    「真无主角锚」与「bible 名缺失但可由追踪/性别卡回落」。
    """
    card_dir = book / "设定" / "角色"
    card_names = sorted(p.stem for p in card_dir.glob("*.md")) if card_dir.is_dir() else []
    cast = _chapter_cast_names(packet, chapter_id)
    protagonist_names, protagonist_source = _protagonist_anchor_for_fallback(book)
    hits: list[str] = []
    for name in card_names:
        if _card_hit(name, cast, protagonist_names, len(hits)):
            hits.append(name)
    return {
        "card_dir_exists": card_dir.is_dir(),
        "card_names": card_names,
        "cast_names": cast,
        "hits": hits,
        "protagonist_names": sorted(protagonist_names),
        "protagonist_source": protagonist_source,
    }


def _character_contract_from_cards(book: Path, packet: dict,
                                   chapter_id: int = 0) -> list[dict]:
    """character_contract 为空时从 设定/角色/*.md 兜底构建最小 voice 合同。

    匹配规则（C-CARD-FALLBACK@1，按序，命中即注入，去重，最多 8 条）：
      1. 卡名全等于 `_chapter_cast_names` 任一项；
      2. 卡名出现在出场名项的子串中（卡名 ⊂ 出场名，长度 ≥2）；
      3. 主角名集合（bible 名 → 追踪 state → 唯一性别卡）与卡名 `in` 关系，
         且当前结果为空 → 无条件注入 1 条。
    卡内抽「口头禅/口癖/句式/习惯动作/性格」voice 锚点、「禁」字 behavior 锚点。
    纯确定性、只读（绝不写细纲 / 绝不建卡），无 LLM。
    """
    card_dir = book / "设定" / "角色"
    if not card_dir.is_dir():
        return []
    cast = _chapter_cast_names(packet, chapter_id)
    protagonist_names = _protagonist_names_for_fallback(book)
    out: list[dict] = []
    for p in sorted(card_dir.glob("*.md")):
        if len(out) >= 8:
            break
        name = p.stem
        if not _card_hit(name, cast, protagonist_names, len(out)):
            continue
        try:
            lines = [ln.strip() for ln in
                     p.read_text(encoding="utf-8").splitlines() if ln.strip()]
        except Exception:  # noqa: BLE001 — 单卡损坏不拖垮兜底
            continue
        head = "\n".join(lines[:5])
        voice = [ln[:160] for ln in lines
                 if re.search(r"口头禅|口癖|句式|习惯动作|性格", ln)][:3]
        behavior = [ln[:160] for ln in lines if "禁" in ln][:2]
        out.append({
            "name": name,
            "role": "主角" if re.search(r"女主|主角|男主", head) else "",
            "voice_constraints": voice or [
                f"（角色卡兜底，详见 设定/角色/{name}.md 首段）{head[:120]}"],
            "behavior_constraints": behavior,
            "state": "",
        })
    return out


def _facts_from_bible(book: Path) -> list[dict]:
    """首章/无前情时从 bible.json 构建初始已认证事实（与 _canon_projection 输出同形）。

    只登记确定性可读字段（主角身份设定/全书梗概），不做剧情推理；条目 tier 语义
    与 tracking facts.confirmed 对齐（fact_type=identity/worldview）。"""
    try:
        bible = json.loads((book / "bible.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — bible 缺失/损坏按无兜底处理
        return []
    out: list[dict] = []
    protag = bible.get("protagonist") or {}
    pname = re.sub(r"^主角", "", str(protag.get("name") or "")).strip()
    desc = str(protag.get("description") or "").strip()
    if pname and desc:
        out.append({
            "id": "bible-protagonist", "subject": pname, "predicate": "身份设定",
            "value": desc[:200], "fact": f"{pname}：{desc[:200]}",
            "fact_type": "identity",
            "evidence": {"chapter": 0, "quote": "bible.json protagonist"},
        })
    syn = str(bible.get("synopsis") or bible.get("world_summary") or "").strip()
    if syn:
        out.append({
            "id": "bible-synopsis", "subject": "", "predicate": "世界观基线",
            "value": syn[:300], "fact": syn[:300], "fact_type": "worldview",
            "evidence": {"chapter": 0, "quote": "bible.json synopsis"},
        })
    return out


def _repair_packet_contract(book: Path, chapter_id: int, packet: dict) -> dict:
    """packet 合同修复闸（P0-2，写章链 BLOCK 点；C-GATE@1 错误码化）。

    判定顺序（严格）：
      ① character_contract 非空 → 通过（不改任何 meta）；
      ② 为空 → `_character_contract_from_cards` 兜底注入；成功则 meta.missing 去掉
         「出场角色voice锚点」、meta.parse_status 置 REPAIRED（**禁止写 FULL**）；
      ③ 仍为空 → 按 meta 区分错误码：
         - `VOICE_ANCHOR_UNPARSEABLE`：细纲有锚点（voice_anchor_detected=True）但解析
           不出条目（char_count==0）——可自动化修复，文案指向 nww_outline_format_fix.py；
         - `VOICE_ANCHOR_ABSENT`：细纲与角色卡都没有可用 voice 锚点——需作者补内容；
         - meta 缺 voice_anchor_detected（旧 builder/异常路径）→ 按 ABSENT 并加
           `[meta-missing]` 前缀。
      失败时把 `_card_fallback_diagnosis` 写入 `meta.card_fallback`（R4-AC2 证据）。
      `NWW_PACKET_STRICT=0` 仍降级 WARNING 并放行；packet 为 None/非 dict 不在此抛。
    """
    if not isinstance(packet, dict):
        # 契约边界：packet 结构异常不在 gate 内抛，交由 step_packet 原样失败。
        return packet
    meta = packet.get("meta") or {}
    strict = os.environ.get("NWW_PACKET_STRICT", "1") != "0"
    code = ""
    msg = ""
    if not packet.get("character_contract"):
        injected = _character_contract_from_cards(book, packet, chapter_id=chapter_id)
        if injected:
            packet["character_contract"] = injected
            meta["missing"] = [m for m in (meta.get("missing") or [])
                               if m != "出场角色voice锚点"]
            # R4-AC4：兜底命中不得谎报 FULL；本就 FULL 的保持 FULL（不降级）。
            if meta.get("parse_status") != "FULL":
                meta["parse_status"] = "REPAIRED"
            print(f"[P02-REPAIR] character_contract 为空 → 设定/角色卡兜底注入 {len(injected)} 条"
                  f"（{[c['name'] for c in injected]}）", flush=True)
        else:
            diag = _card_fallback_diagnosis(book, packet, chapter_id)
            meta["card_fallback"] = diag
            _evidence = (f"本章出场名候选 {diag['cast_names'][:8] or '（空）'}；"
                         f"现有角色卡 {diag['card_names'][:8] or '（无）'}")
            _cnt = 0
            try:
                _cnt = int(meta.get("voice_anchor_char_count") or 0)
            except (TypeError, ValueError):
                _cnt = 0
            if "voice_anchor_detected" not in meta:
                code = "VOICE_ANCHOR_ABSENT"
                msg = (f"[meta-missing] 细纲与 设定/角色/ 都没有可用的 voice 锚点"
                       f"（builder 未产出 meta.voice_anchor_detected）；{_evidence}")
            elif meta.get("voice_anchor_detected") is True and _cnt == 0:
                code = "VOICE_ANCHOR_UNPARSEABLE"
                msg = ("细纲 voice 锚点格式无法解析（检测到锚点，但解析不出任何角色条目）；"
                       f"请运行 `python3 skills/nww-novel-skill/scripts/nww_outline_format_fix.py "
                       f"--book \"{book}\" --chapter {chapter_id}`，或把该段改为 "
                       "`## 出场角色voice锚点` 小节（条目形如 `- **角色名**：①…②…`）后重试")
            else:
                code = "VOICE_ANCHOR_ABSENT"
                msg = ("细纲与 设定/角色/ 都没有可用的 voice 锚点；请在细纲补 "
                       "`## 出场角色voice锚点` 小节（逐角色口癖/句式/禁用表达），"
                       f"或在 设定/角色/ 为本章角色建卡后重试；{_evidence}")
    facts = packet.get("facts") or {}
    if not facts.get("confirmed"):
        bf = _facts_from_bible(book)
        if bf:
            facts["confirmed"] = bf
            facts.setdefault("meta", {})["reason"] = "bible_fallback_no_confirmed_facts"
            packet["facts"] = facts
            print(f"[P02-REPAIR] facts.confirmed 为空 → bible 兜底注入 {len(bf)} 条", flush=True)
    packet["meta"] = meta
    if code:
        if strict:
            raise RuntimeError(
                f"[PACKET_GATE] BLOCK[{code}]: {msg} —— "
                "修复后重试；NWW_PACKET_STRICT=0 可临时降级放行")
        print(f"[PACKET_GATE][WARNING][{code}] {msg}（NWW_PACKET_STRICT=0 降级放行）",
              flush=True)
    return packet


def _contract_sidecar_path(book: Path, chapter_id: int) -> Path | None:
    """本章写作合同 sidecar 路径（双命名变体统一收口，2026-10-25）。

    builder 落盘名 = 细纲_第001章.contract.json（带「第」）；历史/兼容变体无「第」。
    所有读取点必须走本 helper，禁止各自拼路径——散拼的读取点漏写变体会静默读空。"""
    for name in (f"细纲_第{chapter_id:03d}章.contract.json",
                 f"细纲_{chapter_id:03d}章.contract.json"):
        p = book / "大纲" / name
        if p.is_file():
            return p
    return None



def _resolve_protagonist_anchor(book: Path) -> tuple[set[str], str, Path | None]:
    """主角锚解析：委托 nww_pronoun_gate.resolve_protagonist_anchor（C-PRONOUN-REUSE@1）。

    返回 (主角名集合, 锚来源, 主角卡路径或 None)，回退链（bible 名 → 追踪 state 印证 →
    唯一性别卡 → bible.description 的「主角X」）与阈值全部在单源实现内，本函数只转发。
    委托失败不回退私有算法：按锚缺失返回 ({}, "none", None) 并打印一行诊断。
    """
    try:
        return _pronoun_gate_module().resolve_protagonist_anchor(book)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 — 单源实现不可用按锚缺失处理，不复制算法
        print(f"[PRONOUN] nww_pronoun_gate 不可用，主角锚按缺失处理: {exc}", flush=True)
        return set(), "none", None


def _protagonist_gender(book: Path) -> str:
    """主角性别声明：委托 nww_pronoun_gate.protagonist_gender（C-PRONOUN-REUSE@1）。

    返回值语义不变：male / female / ""（无声明或声明冲突 → 空，闸按缺失警示跳过）。
    委托失败不回退私有算法：返回空值并打印一行诊断。
    """
    try:
        return str(_pronoun_gate_module().protagonist_gender(book) or "")  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 — 单源实现不可用按性别缺失处理，不复制算法
        print(f"[PRONOUN] nww_pronoun_gate 不可用，主角性别按缺失处理: {exc}", flush=True)
        return ""


_PRONOUN_GATE_MOD: object | None = None


def _pronoun_gate_module() -> object:
    """按仓库根加载代词闸唯一实现（C-PRONOUN@1）。"""
    global _PRONOUN_GATE_MOD
    if _PRONOUN_GATE_MOD is None:
        _p = toolbox.scripts_dir() / "nww_pronoun_gate.py"
        _spec = importlib.util.spec_from_file_location("nww_pronoun_gate", _p)
        if _spec is None or _spec.loader is None:
            raise RuntimeError(f"spec unavailable: {_p}")
        _mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        _PRONOUN_GATE_MOD = _mod
    return _PRONOUN_GATE_MOD


def _pronoun_gate_check(book: Path, chapter_id: int, prose_text: str) -> tuple[bool, str]:
    """代词性别机械闸（P0-3）：唯一实现在 nww_pronoun_gate.check。

    算法（归属统计 / Local_Actor 机制 / 阈值 8 与 0.5）与脚本侧脚本共用一份，
    本函数只是薄包装；加载或执行异常按原口径 fail-open（返回 True + WARNING），
    不因判据模块缺失而阻断写链。NWW_PRONOUN_GATE=0 的关闭语义在共享实现内。
    """
    try:
        _ok, _msg = _pronoun_gate_module().check(book, chapter_id, prose_text)  # type: ignore[attr-defined]
        return bool(_ok), str(_msg)
    except Exception as exc:  # noqa: BLE001
        return True, f"代词闸执行异常，已跳过: {exc}"


def _num_val(s: str) -> float | None:
    """中文/阿拉伯数字 → 数值（支持到百；两=2）。"""
    s = str(s).strip()
    if s.isdigit():
        return float(s)
    if "百" in s:
        parts = s.split("百")
        hundreds = _CN_NUM_TO_INT.get(parts[0], 1) if parts[0] else 1
        rest = parts[1] if len(parts) > 1 else ""
        return float(hundreds * 100 + (_cn_num_to_int(rest) or 0))
    v = _cn_num_to_int(s)
    return float(v) if v is not None else None


_NUM_ANCHOR_SPLIT = re.compile(
    r"([零一二两三四五六七八九十百千]+|\d+(?:\.\d+)?)\s*(.+)$")


def _numeric_anchor_violations(book: Path, chapter_id: int, prose: str) -> list[str]:
    """数字锚写后审计（P0-4）：sidecar 基线 vs 正文同量词族数字比对。纯确定性。

    细纲数字锚（首单两块下品灵石等）在正文必须存在等值表达；同量词族出现但数值
    全部不一致（两块→十块）或整个量词族缺失（限购3支整段没写）→ 计入 promise
    violations 与维度地板同级阻断。sidecar 缺失（旧书/关闭）→ 空列表兼容。"""
    sc = _contract_sidecar_path(book, chapter_id)
    if sc is None or not prose:
        return []
    try:
        data = json.loads(sc.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    violations: list[str] = []
    for anchor in data.get("numeric_anchors") or []:
        m = re.search(r"：\s*(.+?)（细纲原句", str(anchor))
        if not m:
            continue
        s = m.group(1).strip()
        am = _NUM_ANCHOR_SPLIT.match(s)
        if not am:
            continue
        anum = _num_val(am.group(1))
        if anum is None:
            continue
        # 剥前导量词与品位前缀：「块下品灵石」→「灵石」；日/天同族归一
        unit_key = re.sub(r"^[块枚颗]", "", am.group(2))
        unit_key = (unit_key.replace("下品", "").replace("中品", "")
                    .replace("上品", "").replace("极品", "").replace("天", "日"))
        unit_probe = unit_key[:2]
        matched_value = None
        for pm in re.finditer(
                rf"([零一二两三四五六七八九十百千]+|\d+(?:\.\d+)?)\s*(?:块|枚|颗)?\s*{re.escape(unit_probe)}",
                prose):
            pnum = _num_val(pm.group(1))
            if pnum == anum:
                matched_value = pnum
                break
        if matched_value is None:
            family = re.findall(
                rf"([零一二两三四五六七八九十百千]+|\d+(?:\.\d+)?)\s*(?:块|枚|颗)?\s*{re.escape(unit_probe)}",
                prose)
            if family:
                violations.append(
                    f"数字锚失守：细纲承诺「{s}」与正文数值不符（正文同单位出现：{family[:4]}）")
            else:
                violations.append(f"数字锚失守：细纲承诺「{s}」在正文中缺失（无同单位表达）")
    return violations


def _advisory_gates_report(book: Path, chapter_id: int, prose: str) -> list[str]:
    """修存量链（polish/deslop/revise）的 advisory 闸（2026-10-25 模式C补全）。

    与 write/rewrite 链的 BLOCK 语义刻意区分：这三条链的职责是修复**存量缺陷章**，
    BLOCK 会把带缺陷的存量章（如代词错位的第1/3章）永久锁死无法润色修复——
    因此只报告不阻断，结果进 stage detail 保证可见。write/rewrite 产新稿仍 BLOCK。"""
    notes: list[str] = []
    try:
        _ok, _msg = _pronoun_gate_check(book, chapter_id, prose)
        if not _ok:
            notes.append(f"[代词] {_msg}")
    except Exception:  # noqa: BLE001
        pass
    try:
        for v in _numeric_anchor_violations(book, chapter_id, prose):
            notes.append(f"[数字锚] {v}")
    except Exception:  # noqa: BLE001
        pass
    return notes


def _profile_todo(book: Path, kind: str, name: str, chapter_id: int,
                  *, first_appear: object = None, card_path: str | None = None,
                  hint: str = "") -> dict:
    """构造一条「设定待办」（角色卡缺失 / 性别未登记）。只描述现状 + 建议，不代填。"""
    card_rel = card_path or f"{_CARD_DIR_REL}/{name}.md"
    return {
        "kind": kind,
        "name": name,
        "card_path": card_rel,
        "card_exists": (book / card_rel).is_file(),
        "first_appear": first_appear,
        "hint": hint,
    }


def _record_profile_todos(book: Path, chapter_id: int, todos: list[dict],
                          *, dry_run: bool = False) -> list[str]:
    """把设定待办幂等合并进 `追踪/_profile_todos.json`（2026-11-01）。

    契约（审查后修订，全部 fail-open，绝不阻断生产）：
      - 幂等：同一 (kind, card_path) 只保留一条；再次命中只累加 occurrences /
        更新 last_seen_chapter，不重复建条目；
      - dry_run 不落盘（保持「dry-run 正式目录零变化」）；书还没有 追踪/ 目录时也不落盘
        （不给无追踪项目凭空造目录）；两种情况仍返回摘要供 stdout 播报；
      - 待办只是**记账**：不推断性别、不自动建卡、不写 bible（作者/前端自行处理）；
      - 落盘失败静默降级为摘要（待办记录失败不得挂写章链）。
    返回本次新增/更新条目的可读摘要。
    """
    if not todos:
        return []
    tracking_dir = book / _TRACKING_DIR
    summaries: list[str] = []
    for t in todos:
        summaries.append(f"{t.get('card_path')}：{t.get('hint') or '待补设定'}")
    if dry_run or not tracking_dir.is_dir():
        return summaries
    path = tracking_dir / "_profile_todos.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        rows = data.get("todos") if isinstance(data, dict) and isinstance(data.get("todos"), list) else []
        rows = [r for r in rows if isinstance(r, dict)]
        index = {(str(r.get("kind") or ""), str(r.get("card_path") or "")): r for r in rows}
        now = int(time.time())
        for t in todos:
            key = (str(t.get("kind") or ""), str(t.get("card_path") or ""))
            row = index.get(key)
            if row is None:
                row = dict(t, occurrences=1, first_seen_chapter=chapter_id,
                           last_seen_chapter=chapter_id, created_at=now, updated_at=now)
                rows.append(row)
                index[key] = row
            else:
                row["occurrences"] = int(row.get("occurrences") or 0) + 1
                row["last_seen_chapter"] = max(int(row.get("last_seen_chapter") or 0), chapter_id)
                row["card_exists"] = bool(t.get("card_exists", row.get("card_exists")))
                row["hint"] = t.get("hint") or row.get("hint")
                row["updated_at"] = now
            # 卡已补齐 → 待办自动标结（只标记，不删记录，留痕可审计）
            row["resolved"] = bool(row.get("card_exists")) and row.get("kind") == "character_card_missing"
        payload = {"schema_version": 1, "updated_at": now, "todos": rows}
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001 — 待办记账失败不得阻断写章
        return summaries + [f"待办未落盘（{exc}）"]
    return summaries


def _collect_role_lifecycle_todos(book: Path, chapter_id: int, packet: dict) -> list[dict]:
    """从 packet 的 role_lifecycle_report 收集「未注册长期角色」待办（含可操作建议）。

    判定与既有 WARNING 完全一致（classification=recurring AND bible_match=false AND
    card_required=full），只把纯打印升级为「打印 + 待办可追踪」，不改任何门禁语义。
    """
    todos: list[dict] = []
    for ent in (packet.get("role_lifecycle_report") or []):
        if not isinstance(ent, dict):
            continue
        if not (ent.get("classification") == "recurring"
                and not ent.get("bible_match")
                and ent.get("card_required") == "full"):
            continue
        name = str(ent.get("name") or "").strip()
        if not name:
            continue
        card_rel = f"{_CARD_DIR_REL}/{name}.md"
        if (book / card_rel).is_file():
            hint = (f"角色卡 {card_rel} 已存在但未在 bible 登记 → 在 bible.json 角色表补登记"
                    "（引擎按 bible 匹配，未登记会被判为未注册长期角色）")
        else:
            hint = (f"创建 {card_rel}（建议含：姓名 / 性别：男|女 / 年龄 / 说话口吻锚点 / "
                    "与主角关系），并在 bible.json 角色表登记")
        todos.append(_profile_todo(book, "character_card_missing", name, chapter_id,
                                   first_appear=ent.get("first_appear"),
                                   card_path=card_rel, hint=hint))
    return todos


def _collect_protagonist_gender_todo(book: Path, chapter_id: int) -> list[dict]:
    """主角卡缺「性别」声明 → 待办（代词闸会整闸跳过，不是小问题）。

    只在能定位到主角卡时记录（拿不到主角锚就不猜、不指错文件）。
    """
    try:
        if _protagonist_gender(book):
            return []
        _names, _source, card = _resolve_protagonist_anchor(book)
    except Exception:  # noqa: BLE001
        return []
    if card is None:
        return []
    try:
        card_rel = card.relative_to(book).as_posix()
    except ValueError:
        card_rel = str(card)
    name = card.stem
    return [_profile_todo(
        book, "character_gender_missing", name, chapter_id, card_path=card_rel,
        hint=f"在 {card_rel} 头 8 行补 `性别：男/女`（当前缺声明 → 代词闸整闸跳过，"
             "女主易被写成「他」穿过审稿）")]


def step_packet(book: Path, chapter_id: int, dry_run: bool = False) -> tuple[Path, dict]:
    """1. 打包：细纲 → Writing Packet v2.2（CCv2 语义合同，v2 builder 接入）。

    P2 切换：v1 builder → nww_packet_builder_v2.py（P1-1 语义升级版）。
    P2.6 迁移完成（2026-09-23）：_v1_aliases 双发射已删除，生产链 CCv2 ONLY。
    此处只做环境补全，不再重建剧情：
      ① 前情 fallback（builder 只认 正文/，draft/ 或软链场景缺失时从上一章正文补）；
      ② 运行环境元信息（_runtime，供审计溯源）。
    """
    outline = _session_outline_compat(book, chapter_id)
    real_outline = _find_outline_file(book, chapter_id)  # 真实文件（软链用于 builder，此处防清理后丢失）
    try:
        # P0-1：dry_run 沙箱预览须保持正式目录零变化 → --no-sidecar（CLI 参数，
        # 非 os.environ：环境变量是进程级全局，两书并发会互相覆盖，pipeline 禁用此模式）
        res = toolbox.run_script(
            "nww_packet_builder_v2.py",
            [str(book), str(chapter_id)] + (["--no-sidecar"] if dry_run else []),
            book, timeout=120,
        )
        if not res["ok"]:
            raise RuntimeError(res["stderr"] or res["stdout"])
        packet = json.loads(res["stdout"])
        # P0-2：合同修复闸（角色卡兜底 / 首章 facts 兜底 / voice 缺失 BLOCK）
        packet = _repair_packet_contract(book, chapter_id, packet)
        # ① 前情兼容（CCv2：读 v2 权威 context.preceding）：builder 只认 正文/；缺失时从上一章正文补
        if packet.get("context", {}).get("preceding", _PRECEDING_MISSING) == _PRECEDING_MISSING:
            fallback = _preceding_from_body(book, chapter_id)
            if fallback != _PRECEDING_MISSING:
                packet.setdefault("context", {})["preceding"] = fallback
        # ② 运行环境补全（不解释剧情）
        packet = _enrich_packet(book, packet, real_outline)
        # ②-NRL 叙事节奏计划（NRL）：off(默认) 对 packet 零改动；shadow 只生成不注入；
        #      enforce 校验通过后注入 packet["rhythm_plan"]（Writer 输入 = json.dumps(packet)）。
        # ★ 收据写进 packet["_runtime"]（执行上下文局部），禁止用模块级全局（并发/多书会互相覆盖）。
        _nrl_mode = nww_rhythm_planner.mode()
        try:
            _nrl = nww_rhythm_planner.attach(book, packet, chapter_id, outline=real_outline)
        except Exception as _e:
            # attach 契约是「永不抛出」；真抛出说明模块级故障。
            # enforce 下绝不吞（外脑 Q4-①），off/shadow 才允许降级。
            if _nrl_mode == "enforce":
                raise
            print(f"[NRL] attach crashed: {_e}", flush=True)
            _nrl = None
        if _nrl is not None:
            packet.setdefault("_runtime", {})["nrl"] = _nrl.get("receipt", {})
            print(f"[NRL] mode={_nrl['mode']} status={_nrl['status']} "
                  f"error={_nrl.get('error_code') or '-'} "
                  f"errors={(_nrl.get('errors') or [])[:3]}", flush=True)
            if _nrl.get("mode") == "enforce" and _nrl.get("status") != "ok":
                # fail-closed：enforce 下计划无效/生成失败 → 阻断，不静默退回旧路径
                raise RuntimeError(f"[NRL] BLOCK: {_nrl.get('error_code')} — {_nrl.get('errors')}")
        # ③ Role Lifecycle Contract v1 WARNING（R1，只告警不阻断）
        #    判定：classification=recurring AND bible_match=false AND card_required=full → 缺卡 WARNING
        #    2026-11-01（审查后修订）：告警升级为「打印 + 可追踪待办」，但不改门禁、不自动建卡、
        #    不推断性别 —— 原实现只打印一行，作者处理完就丢，同一角色下章继续告警且无进度。
        _role_todos = _collect_role_lifecycle_todos(book, chapter_id, packet)
        for todo in _role_todos:
            print(f"[ROLE-LIFECYCLE WARNING] 发现未注册长期角色：{todo['name']} "
                  f"（first_appear={todo.get('first_appear')}）。生产前生命周期 WARNING（不阻断）："
                  f"{todo['hint']}", flush=True)
        _role_todos += _collect_protagonist_gender_todo(book, chapter_id)
        if _role_todos:
            try:
                for _note in _record_profile_todos(book, chapter_id, _role_todos, dry_run=dry_run):
                    print(f"[ROLE-LIFECYCLE TODO] {_note}"
                          + ("（dry-run 未落盘）" if dry_run else ""), flush=True)
            except Exception as _e:  # noqa: BLE001 — 待办记账任何异常都不得挂写章链
                print(f"[ROLE-LIFECYCLE TODO] 待办记录跳过（{_e}）", flush=True)
    finally:
        _cleanup_outline_compat(book, chapter_id)
    return outline, packet


def _enrich_packet(book: Path, packet: dict, outline: Path) -> dict:
    """packet 运行环境补全（legacy compatibility layer，P2 裁决降级）。

    P2 裁决：只保留环境补全（前情 / tracking facts / runtime metadata / 事务信息）；
    禁止重新解释剧情——beat/logic_line/active_characters/chapter_intent 的旧语义重建已删除
    （这些由 v2 builder 解析进 scene_chain/character_contract/intent，此处再重建=第二个 parser）。
    """
    # 运行环境元信息（非剧情字段，供审计/溯源）
    packet["_runtime"] = {
        "builder": "nww_packet_builder_v2.py",
        "semantics": packet.get("meta", {}).get("schema_semantics", "chapter_contract_v2"),
        "pipeline": "P2.6: v2 builder 接入，CCv2 ONLY（_v1_aliases 已删除）",
    }
    # R4-AC2 证据：角色卡兜底诊断（卡目录/现有卡名/本章出场名/命中项）。
    # 放在 enrich（而不只在 gate 内）是为了让「builder + enrich」基线与 step_packet
    # 产物保持一致——NRL off 零改动契约（tests/test_nww_rhythm_pipeline.py）。
    # 与 _repair_packet_contract 写入的值同源、幂等。
    try:
        _cf_chapter = int(packet.get("chapter_id") or 0)
    except (TypeError, ValueError):
        _cf_chapter = 0
    packet.setdefault("meta", {})["card_fallback"] = _card_fallback_diagnosis(
        book, packet, _cf_chapter)
    return packet


def _writer_mode_label(level: str) -> str:
    """Writer 会话开头的任务标签。L1/L2/L3 都是重生产，不得标成「扩写」——
    实测 L3 被标「扩写」时 writer 复读原稿（第1章 1958 字 ≈ 原稿 1924 字，
    细纲要求替换的病号/玉牌交易情节原样保留，审稿 65 分）。"""
    return {"L1": "整章重写", "L2": "结构重写", "L3": "重新生产"}.get(level or "", "扩写")


# L3 事件组织执行契约（2026-11-13）：Plan 的 event_organization 是创作决定，不是参考。
# 实测：Plan N5 写「替换：…但未直接展示交换情节」，writer 仍原样保留旧稿的完整交易试探
# （「只这一根」「吃了就没了」「香」），细纲的信息释放边界被破坏 → 审稿 I-I=30、总分 65。
_L3_EVENT_ORG_CONTRACT = (
    "\n【Plan 事件组织执行（本轮硬约束）】3_new_dramatic_design.event_organization 是已批准的"
    "创作决定，必须逐条执行：type=新增 的事件必须写出；type=替换 的事件必须替换原稿对应情节，"
    "不得把被替换的旧情节原样保留（如 Plan 要求「未直接展示交换」时仍写完整交易试探）；"
    "type=删除 的事件不得出现；type=保留 的事件保持剧情事实、允许重新表达。"
    "原稿中 Plan 已列入 2_current_problems 且未被 event_organization 保留的情节/道具/角色，不得沿用。"
    "\n【信息推进·禁止复述】同一观察、判断、决定或动作只写一次；不得把已写过的信息换措辞"
    "再铺一遍。每段必须推进新信息，宁可少写也不复述——审稿 I-B 会按「同一信息重复 ≥2 次」扣分。"
)


def step_writer(book: Path, chapter_id: int, packet: dict, on_token=None,
                note: str = "", original_prose: str = "", level: str = "",
                l2_contract: dict | None = None,
                l3_plan: dict | None = None, c8_param: dict | None = None,
                l3_guard_hints: str = "", expand_mode: bool = False, fresh_write: bool = False,
                wc_target: str | int | None = None) -> str:
    """2. 主笔 agent 生成正文（本地大模型直连；on_token 可选回调做流式输出）。

    2026-09 创作增强基础修复：narrative-writer.md 是"多轮+工具"agent 定义，
    不再整份塞进 system（否则单轮无工具 LLM 倾向输出工具规划而非正文）。
    改用 writer_prompt.build_writer_system() 得到纯净正文写作方法论，配合
    session 直连流式生成；不改变 agent 定义/packet/tracking。

    note/original_prose（2026-09-16 补，修复「扩写语义塌缩」）：重写链从入口透传
    `note`（扩写/修订要点 ≤500 字）与目标章现有正文。二者齐全时进入「原文保持式
    重写」——主笔在原文基础上按 note 定点扩充/修订，保留原句、不重写稳定桥段，
    而不是按细纲整章重生成；note 为空则维持原行为（按 Writing Packet 全量生成）。

    level="L1"（2026-09-20 用户裁决，Writer Contract 修复）：L1 与「原文保持式
    重写」（扩写/定点修订语义）彻底分支。L1 走专属「整章重写」契约：
    <original> 仅作剧情事实基准（当前实际发生了什么/事件怎么完成/人物说了什么/
    章节落点），不是需要保留的文字模板；必须保持章节目标/核心事件及顺序/每个
    事件完成态/人物关系/关键数值/世界观事实/原章节最终结果，同时必须重新组织
    句子段落、重新表达对话动作描写、重处理节奏与信息释放、消除 AI 腔。
    禁止「原句一律保留」「不重写已有稳定桥段」类指令进入 L1 prompt（根因：该
    语义块曾导致 L1 writer 复读原文、3246→3248 仅 1 行 diff、20→25 制造时间线
    矛盾）。扩写/定点修订（非 L1）继续使用原保持式语义，不受影响。
    """
    session = (
        "你是主笔。基于 Writing Packet {}第{}章正文。\n"
        "输出格式（必须严格遵守）：正文内容放在 <prose> 与 </prose> 标签之间。\n"
        "标签前后不要写工具调用、读取或检查清单，直接把正文完整写出。\n"
        "正文必须完整、独立成章（以 `# 第N章　标题` 开头），不引用外部文件、不写「好的」类寒暄。\n"
        "【开头硬约束】开头第一段必须承接上一章结尾的既定情境（时间/地点/人物动作的落点，"
        "见 Writing Packet 的 context.preceding），开篇即戏：用动作、对话或事件切入；"
        "禁止复述上一章结尾的句子或场景，禁止用天气/风声/环境空镜起笔注水。\n"
        "【结尾硬约束】结尾必须落在本章独有的动作/细节/钩子上，禁止复用上一章结尾的句子或场景"
        "（如吹灯/灶台/底料/香气这类上一章已写过的收尾动作），禁止跨章逐字复读。"
    ).format(_writer_mode_label(level), chapter_id)
    if level == "L1":
        # L1 专属 Writer Contract（2026-09-20 用户裁决）：整章重新表达，原文=剧情事实基准非文字模板
        session += (
            "\n【L1 整章重写（本章执行契约，最高优先级）】<original> 是本章剧情事实基准"
            "（当前实际发生了什么、事件具体如何完成、人物实际说了什么、本章最终落点是什么），"
            "不是需要保留的文字模板。你必须重新生产整章正文："
            "\n必须保持：章节目标；核心事件及其顺序；每个核心事件的完成态；人物关系；"
            "关键数值；世界观事实；原章节最终结果。"
            "\n必须重新做：重新组织句子与段落；重新表达对话、动作与描写；重新处理叙事节奏"
            "与信息释放；消除原文的 AI 腔、机械表达与冗余。"
            "\n禁止把 <original> 当作文字模板逐句复制；禁止只做局部微调后原样返回全文；"
            "禁止为了制造变化而修改事实（如改动时间、数值、称谓）。"
        )
    elif level == "L2":
        # L2 专属 Writer Contract（2026-09-20 用户裁决）：结构重写语义。
        # 与 L1 分层：L1=保持剧情重新表达（不改叙事结构）；L2=保持剧情合同，允许重排结构。
        # 与「原文保持式」（扩写/定点修订）彻底分支：L2 不得"原句一律保留/不重写稳定桥段"。
        if l2_contract is not None:
            try:
                _outline, _pkt = step_packet(book, chapter_id)
                _plan = _l2_structure_plan(book, chapter_id, original_prose,
                                           outline=str(_outline or "")[:800],
                                           contract=l2_contract, on_token=on_token)
                if _plan:
                    session += (
                        "\n【L2 结构重排计划（已确定，最高优先级）】以下结构计划是本次任务已确定的重排方案，"
                        "你必须严格按此计划把 <original> 中同一组核心事件重新编译为正文；"
                        "不得偏离计划另行设计结构，也不得仅复述原文或做逐段近似改写。"
                        "计划内的事件顺序、场景分段、信息释放顺序、开场与收束方式决定正文的叙事组织：\n"
                        + _plan
                    )
            except Exception:  # noqa: BLE001 计划失败不阻塞正文链
                pass
        session += (
            "\n【L2 章节结构重写（本章执行契约，最高优先级）】本次任务的核心交付物是：对本章做出真实、"
            "可感知的结构重写（更换开场进入方式、调整信息揭示顺序、重组场景段落、重组对白与动作、"
            "调整冲突推进节奏、重新安排情绪递进、重新组织章节收束）。"
            "如果你的输出只是原文复述、逐段近似改写，或只做少量措辞替换，则视为未完成 L2 任务。"
            "<original> 是本章剧情与事实基准"
            "（当前实际发生了什么、事件如何完成、人物说了什么、章节最终结果是什么），"
            "不是需要保留的文字模板。你必须重新组织本章的叙事结构："
            "\n必须保持：章节目标；核心事件及其结果（事件本身与结果不可改变，"
            "不得把接受改成拒绝、合作改成对立，不得让事件消失）；人物关系；关键数值；"
            "世界观事实；原章节最终结果。"
            "\n必须重新做：重新组织段落结构；重新安排信息释放顺序；重新组织开场方式与场景进入方式；"
            "重新编排对话与动作；重新推进冲突与情绪递进；调整描写与对白比例、段落衔接、节奏与章节收束方式。"
            "\n禁止把 <original> 当作文字模板逐段改写；禁止以保留原句为目标；禁止复述原文；"
            "禁止为了制造变化而修改事件结果、关系或数值。"
            "\n【事件守恒（最高约束）】你可以重新编排事件的呈现方式（顺序、切入、详略、段落组织），"
            "但不得删除、合并、替换或新增核心事件。下方【L2 剧情骨架锁定】中列出的每一个核心事件，"
            "都必须在新稿中以可识别的叙事形式完成；事件的结果与完成状态必须保持不变。"
            "\n【反误解】「重排结构」不意味着重新设计剧情，不意味着为了制造新的冲突或钩子而把后续章节的主题"
            "提前引入本章；原文不是文字模板，但 Contract 中的核心事件是不可删除的剧情骨架。"
        )
    elif level == "L3":
        # L3 专属 Writer Contract（2026-09-20 用户裁决）：L3 = 章节再生产。
        # 与 L1/L2/原文保持式彻底分支：L3 Plan 已完成创作决策（事件组织/机制/信息释放/收束），
        # Writer 是纯编译层——按已批准 Plan 逐项编译成正文，不得重新规划剧情。
        # expand_mode（2026-09-22 字数治理）：守卫七门中仅 G_Length 字数欠账时，下一轮
        # 编译转「增量扩写」——上稿已过六门（剧情/锚点/时间/C8 全锁），缺的只是体量；
        # 重写模式实测体量锚定 ~2200（六次编译 1396→2222 天花板），单次重写突破不了
        # 细纲目标 70%，扩写语义=以上稿为基底展开细纲已有内容，增量递进补足。
        if l3_plan:
            session += (
                "\n【L3 重新生产计划（已批准，最高优先级）】以下是本次任务已批准的 L3 Reproduction Plan。"
                "你的职责是纯编译：按计划逐项把剧情编译成正文。不得重新规划剧情、不得偏离计划另行设计结构、"
                "不得新增计划之外的核心事件、不得改变计划的机制、事件结果与章节落点。\n"
                + json.dumps(_l3_c8_sanitize(l3_plan, c8_param), ensure_ascii=False, indent=1)[:6000]
            )
        # 2026-11-06：C8 注入段改走纯函数；fallback 幽灵数值不再进 writer prompt。
        session += _l3_c8_writer_block(c8_param)
        if fresh_write:
            # fresh_write（2026-09-22 字数治理·第二通道）：扩写轮实测服从度不稳定
            # （第 6 跑 base 1874→4261 成功 / 第 7 跑 base 2058→原样返回零变化），
            # 单通道命中率 ~50%。扩写失效（增量 <300 字）时切「无基底直写」：不注入
            # <original>，按 Plan+细纲从零组织正文 → 无输入锚定，输出长度锚定目标值。
            # 守卫基准（编译循环 _validate_l3_contract 的 _cur_base）不变，仍对齐上稿。
            try:
                _wt_int = int(str(wc_target).strip()) if wc_target else 0
            except (TypeError, ValueError):
                _wt_int = 0
            # Writer 扩写硬线（aspiration，默认 0.95）：要求尽量贴近细纲目标，推高产出质量。
            # 注意：这是「写的要求」，非 Gate G 的「查的底线」——Gate G 用 _g_length_floor
            # （安全网，放行达标章、只拦不可发布/塌缩），二者刻意解耦（2026-11-06 修复）。
            _hr = _wc_hardline_ratio()
            session += (
                "\n【L3 字数补足·无基底直写（本轮执行契约，最高优先级，替代默认重生产语义）】"
                "此前重写与扩写轮均未达到字数硬线。本轮不受任何旧稿篇幅影响："
                "按已批准的 L3 Plan 与细纲，从零组织并写出完整章节正文。"
                + (f"\n字数硬线：成稿可见字数必须达到 {_wt_int} 字目标的 {_hr:.0%}（≈{int(_wt_int * _hr)} 字）以上，"
                   f"目标 {_wt_int} 字——这是本轮任务的定义，不是参考值；写完自查字数，不足就继续展开细纲每一幕。"
                   if _wt_int > 0 else "")
                + "\n写法：以 Plan 的事件组织与 Required Outcomes 为骨架，细纲三幕逐幕充分展开——"
                "每一幕的事件过程写细（人物怎么做、周围什么反应）、概述性对话写成有来有往的对白、"
                "关键场景给可见细节（声音、气味、物件、旁人反应）；上稿违规清单逐条修复。"
                "\n禁止：注水（重复表述、空话、无信息量的环境堆砌）；新增细纲没有的剧情；"
                "提前消费后续章节内容；为凑字数写与剧情无关的内容。"
            )
            print(f"[WRITER] fresh_write=True target={_wt_int}字 hardline={int(_wt_int * _hr) if _wt_int else 0}", flush=True)
        elif expand_mode:
            try:
                _wt_int = int(str(wc_target).strip()) if wc_target else 0
            except (TypeError, ValueError):
                _wt_int = 0
            # Writer 扩写硬线（aspiration，默认 0.95，同 fresh_write）：推高产出，非 Gate 底线。
            _hr = _wc_hardline_ratio()
            # 量化对抗体体锚定（实测第 5 跑：基底 1865 → 1906 → 2075，模型输出长度锚定
            # 输入基底。契约显式计算「基底字数 / 硬线 / 必须新增量」，把扩写任务量化为
            # 「新增 ≥N 字实质内容」，框架从「扩写这份稿」反转为「产出 2940+ 字完整正文」。
            _base_len = len(re.sub(r"\s+", "", str(original_prose or "")))
            _hard_line = int(_wt_int * _hr) if _wt_int > 0 else 0
            _need_more = max(0, _hard_line - _base_len) if _wt_int > 0 else 0
            session += (
                "\n【L3 字数补足·增量扩写（本轮执行契约，最高优先级，替代默认重生产语义）】"
                "任务定义：产出一份完整的章节正文"
                + (f"，可见字数必须达到 {_wt_int} 字目标的 {_hr:.0%} 硬线（≈{_hard_line} 字）以上，目标 {_wt_int} 字" if _wt_int > 0 else f"，可见字数必须达到细纲目标 {_hr:.0%} 硬线以上")
                + f"。\n<original>（当前 {_base_len} 字）是基底草稿：它已通过剧情/锚点/时间守卫，"
                "内容骨架合格但体量不足。你必须在保留其全部内容骨架的基础上，"
                + (f"新增至少 {_need_more} 字的实质展开内容，把成稿写到硬线以上。" if _need_more > 0 else "继续展开细纲已有内容至目标体量。")
                + "\n必须保留：已有的事件及其顺序、全部对话内容与结果、人物动作、钩子与落点"
                "（可优化局部表达，禁止删减任何剧情、对话或场景来腾字数）。"
                "\n展开方向：把细纲每一幕的事件过程写细（人物怎么做、周围什么反应）；"
                "把概述性对话改写成有来有往的对白（追问、反驳、停顿、插话）；"
                "给关键场景补可见细节（声音、气味、物件、旁人反应）；"
                "细纲已规划而上稿未落地的段落（章末钩子/落点场景/硬依赖事件）必须完整补写出——"
                "以上稿违规清单为检查线，逐条补齐。"
                "\n展开量分配：三幕均匀受益，优先扩对话轮次、动作过程与场景反应，不要只改开头几段。"
                "\n禁止：注水（重复表述、空话、无信息量的环境堆砌）；新增细纲没有的剧情；"
                "把已有内容换个说法冒充扩写；删减内容后注水凑数；"
                + (f"输出与基底几乎相同、新增不足 {_need_more} 字的稿件（视为未执行扩写，守卫将打回）。" if _need_more > 0 else "输出与基底几乎相同的稿件。")
            )
            print(f"[WRITER] expand_mode=True base={_base_len}字 hardline={_hard_line} "
                  f"需新增≥{_need_more}字", flush=True)
        else:
            session += (
                "\n【L3 章节重新生产（本章执行契约，最高优先级）】<original> 是本章剧情事实基准，不是文字模板。"
                "你必须围绕已批准的 L3 Plan 重新生产整章正文：重新组织句子与段落、重新表达对话动作描写、"
                "重新处理叙事节奏与信息释放、消除 AI 腔与机械表达。"
                "\n必须保持：章节目标；Plan 的 Required Outcomes 全部落地；人物身份与既有关系；世界 Canon；"
                "关键剧情结果；前后章因果（不提前消费后续章节内容）。"
                "\n【时间连续性】以 Plan 合同为时间事实基准（与 L3 Guard 验收口径一致）："
                "Plan 合同声明的时间事实（含 Canon 中的历史时间，如「二十年前」）必须按合同落地；"
                "原文时间数值凡 Plan 合同亦认定者必须保留或等价表达；"
                "原文其他风味性时间措辞（背景叙述/修辞）允许在再生产中重组；"
                "不得引入 Plan 合同与原文都不存在的新时间数值。\n"
                "禁止：把 <original> 当作文字模板复制；只做局部微调后原样返回全文；重新发明一套与 Plan 不同的剧情；"
                "提前引入后续章节主题；改变 Canon 或人物既定事实；为了制造变化而修改事实（时间/数值/称谓）。"
            )
        session += _L3_EVENT_ORG_CONTRACT
        if l3_guard_hints:
            # 守卫清单前置（2026-09-22 用户裁决，评分标准前置）：L3 Guard 验收口径
            # 确定性提取后注入，首稿即对齐守卫（写的标准=查的标准）。
            session += "\n" + l3_guard_hints
    elif note and original_prose:
        session += (
            "\n【原文保持式重写（最高优先级，仅本次执行）】本章已有正文见 <original>，你必须在其基础上操作："
            "原句、原味、原人设口吻一律保留，不重写已有稳定桥段、不复述旧稿；按下方 note 指定的扩充点"
            "插入新内容/拉长动作与对话，两处扩充之间用原文自然衔接。<prose> 内无需标注修改痕迹，直接给完整成稿。"
        )
    if level == "L3" and c8_param:
        packet = _l3_c8_sanitize(packet, c8_param)
    # ---- 正文卫生铁律（2026-10-24 用户裁决：全等级/全模式统一收口）----
    # 实测事故：重写稿元文本「比第1章还猛」+ 无源促销认知（满赠试吃/第二包半价）
    # 直通审稿。此处把铁律与守卫 Gate H（确定性拦截）同口径前置。
    session += (
        "\n【正文卫生铁律（守卫将句面硬校验，违反即 BLOCK 打回重编译）】\n"
        "1. 叙述零元文本：正文中禁止出现「第N章／本章／上一章／读者／作者／写手／细纲/正文」"
        "任何写作者视角词，包括跨章比较（如「比第1章还…」）。叙事只能写画内之事。\n"
        "2. 人物信息纪律：人物只能知道剧情已建立的信息。人物对店铺商品的一切认知"
        "（名称/口味/促销/优惠方式）首次出现前，必须先写可见引动——亲眼看到货架/价牌/展示物，"
        "或从在场角色台词听说；严禁角色凭空报出没展示过的商品和优惠机制。\n"
        "3. 人物复现必须有新信息或事态升级，不许只重复同类催促台词。"
    )
    # ---- 时代语域铁律（2026-10-25 用户裁决：语域三档，读 bible，无条件不注入）----
    # 档位权威 = bible.json era_constraints.register_mode（strict/balanced/modern，
    # 缺省 strict）。bible 无 era_constraints 的书不注入任何文本（行为不变）。
    # 生成侧前置=写的标准=查的标准：确定性 era_check（按档调严重度）与审稿 I-E
    # prompt（按档给判定细则）同口径，三处共读同一 bible 字段。
    try:
        session += _era_register_directives(book)
    except Exception:  # noqa: BLE001 — 语域铁律注入失败不挂主链（bible 缺失/损坏按无宪法处理）
        pass
    prompt = json.dumps(packet, ensure_ascii=False, indent=2)
    if note:
        prompt += f"\n\n【执行要点 note】\n{note}"
    if original_prose:
        # 2026-09-22 字数治理：去除 [:6000] 截断——扩写轮基底/长章原文必须完整注入，
        # 否则后段内容静默丢失（截断隐患族修复，与润色链 [:9000] 同源同修）。
        prompt += f"\n\n<original>\n{original_prose}\n</original>"
    system = session + "\n\n" + writer_prompt.build_writer_system(
        style_feedback=packet.get("style_feedback") if isinstance(packet, dict) else None,
        outline_contract=packet.get("outline_contract") if isinstance(packet, dict) else None)
    # 2026-09-22 字数治理：按字数目标动态 max_tokens（≈3 tokens/字 裕量），长章目标
    # 4200 字需 12600 tokens，默认 8192 会输出截断丢后段；无 wc_target 时默认 8192 不变。
    _mt = 8192
    try:
        _wt_raw = int(str(wc_target).strip()) if wc_target else 0
    except (TypeError, ValueError):
        _wt_raw = 0
    if _wt_raw > 0:
        _mt = max(8192, _wt_raw * 3)
    # 2026-11-06 真机实测：writer 输出协议要求先写 <prewrite> 推演块（不计入正文、
    # 不参与字数校验，见 writer_prompt.py:242-259），8192 tokens 不够「推演块 +
    # 2500-2750 字正文」。切到 agnes 直连后第1章重写在正文中段（"王瘸子脸色骤变"）
    # 即 finish_reason=length，stream_text 已达 11287 字符 / 10728 可见字。
    # agnes-3.0-flash 实测接受 16384 / 20000。给足余量让模型正常收尾；
    # env NWW_WRITER_MAXTOK=0 逐字回滚到原 8192 行为。
    if _env_flag_on("NWW_WRITER_MAXTOK", "1"):
        _mt = max(_mt, 16384)
    text, finish_reason = _writer_stream(system, prompt, on_token=on_token, max_tokens=_mt, book=book, chapter_id=chapter_id)
    
    # Output Completeness Gate：确定性截断检测
    oc = check_output_completeness(text, finish_reason)
    if oc["status"] == "BLOCK":
        print(f"[OUTPUT_COMPLETENESS] status=BLOCK reason={oc['reason']} evidence={oc['evidence']}")
        raise RuntimeError(f"[OUTPUT_COMPLETENESS] BLOCK: {oc['reason']} — {oc['evidence']}")

    # —— 字数闸门（2026-10-12 外脑 Q4）：独立于上面那个闸门，只挂在成稿产地 ——
    # 计数对象 = _extract_prose(text)，即**真正会落盘的正文**（剥 <prose> 标签），
    # 与 step_normalize 同口径，避免把标签/标题/元信息算进字数。
    _wc = check_wordcount_gate(
        _extract_prose(text),
        wc_target=(wc_target or (packet or {}).get("wordcount", {}).get("target")),
        chapter_min=_WC_FALLBACK_MIN,
        wc_max=(packet or {}).get("wordcount", {}).get("max"),
        is_revision=bool(note or original_prose),
    )
    LAST_WC_GATE.clear()
    LAST_WC_GATE.update(_wc)
    print(f"[WC_GATE] status={_wc['status']} reason={_wc['reason']} "
          f"actual={_wc['actual']} target={_wc['target']} floor={_wc['floor']} "
          f"unit={_wc['unit']} mode={_wc['mode']}", flush=True)
    if _wc["status"] == "BLOCK" and _wc["mode"] != "off":
        raise RuntimeError(f"[WC_GATE] BLOCK: {_wc['reason']} — 可见字数 {_wc['actual']} "
                           f"< 残骸硬下限 {_wc['floor']}（{_wc['unit']}）")
    if _wc["status"] == "QUARANTINE":
        if _wc["mode"] == "strict":
            raise RuntimeError(f"[WC_GATE] QUARANTINE(strict): {_wc['reason']} — "
                               f"可见字数 {_wc['actual']} / 目标 {_wc['target']}，不得静默发布")
        print(f"[WC_GATE][QUARANTINE-OBSERVE] {_wc['reason']} — "
              f"{_wc['actual']}/{_wc['target']}（策略 {_wc['policy']}），本轮放行但已记录",
              flush=True)

    return text


def _l2_structure_plan(book: Path, chapter_id: int, original: str,
                        outline: str = "", contract: dict | None = None,
                        on_token=None) -> str:
    """L2 调用策略拆分 Stage 1（2026-09-20 用户裁决，L2-Split-01）：
    结构重排计划阶段。只产出计划、不写正文；Contract 作为不可破坏的 story skeleton。

    解决 L2 单次生成同时承担「决定结构 + 写正文 + 事实守恒」的过载：
    模型已证明能重排（L2#2/#3），但「重排 + 守恒」同时执行不稳定（复读/越界摆动）。
    计划阶段把「怎么重排」从正文生成中剥离，Writer 只做受计划约束的编译。
    不新增 Agent：复用同一 writer 流；Guard 三件套不动。
    """
    if not original:
        return ""
    events = []
    for ev in (contract or {}).get("core_events") or []:
        txt = (ev.get("text") or "").strip()
        if txt:
            events.append(f"- {txt}")
    skeleton = "\n".join(events[:12]) if events else "（Contract 未提取到核心事件，按原文实际发生的事件为骨架）"
    facts = []
    for n in (contract or {}).get("key_values") or []:
        facts.append(f"- {(n.get('context') or '')[:40]} = {n.get('value')}")
    facts_txt = "\n".join(facts[:8]) if facts else "（无显式关键数值，正文中实际出现的时间/数量/价格按原文保持）"
    rels = []
    for r in (contract or {}).get("relationships") or []:
        rels.append(f"- {r}")
    rels_txt = "\n".join(rels[:6]) if rels else "（无显式关系锁定，人物关系按原文保持）"
    system = (
        "你是长篇网文生产的章节结构规划师。本阶段你只负责输出「结构重排计划」，"
        "严禁写正文、严禁复述原文句子。"
    )
    prompt = (
        "任务：为本章设计一个结构重排计划，使同一组剧情在全新叙事组织下呈现。\n\n"
        f"章节目标：\n{outline[:400] or '（见细纲）'}\n\n"
        "不可破坏的剧情骨架（story skeleton，逐条必须在新结构中完成）：\n"
        f"{skeleton}\n\n关键事实（不得改变）：\n{facts_txt}\n\n人物关系（不得改变）：\n{rels_txt}\n\n"
        "<original>（本章原文，剧情与事实基准，不是文字模板）：\n"
        f"{original[:4000]}\n</original>\n\n"
        "请按以下编号输出结构重排计划（只列计划，不写正文）：\n"
        "1. 新开场：从哪个场景/画面进入本章？\n"
        "2. 信息释放：原有信息如何重新安排顺序释放？\n"
        "3. 场景分段：正文重新划分为哪些场景/段落，各自承担什么？\n"
        "4. 对白与动作：哪些对白/动作前置或后置，如何重新编排？\n"
        "5. 冲突推进：冲突如何重新递进到交易完成？\n"
        "6. 情绪曲线：情绪如何重新组织递进？\n"
        "7. 结尾收束：如何重新收束本章？\n"
        "约束：不得删除、合并、替换或新增骨架事件；不得改变事件结果、完成态、关系、关键数值与时间事实；"
        "不得把后续章节主题提前引入本章。"
    )
    try:
        raw, _fin = _writer_stream(system, prompt, on_token=on_token, temperature=0.5, book=book, chapter_id=chapter_id)
        # 2026-09-22 P1 修复：L2 计划输出完整性检测（对齐 step_writer :704 既有守卫）。
        # 截断/跑飞提前识别（此前靠 JSON 解析失败被动降级）；raise 走本函数
        # except 分支 → 降级空计划继续，与原降级路径行为一致。
        _oc = check_output_completeness(raw, _fin)
        if _oc["status"] == "BLOCK":
            raise RuntimeError(f"L2 计划输出不完整（{_oc['reason']}）— {_oc['evidence']}")
    except Exception:  # noqa: BLE001 计划失败不阻塞正文链，降级为空计划
        return ""
    txt = (raw or "").strip()
    return txt[:1500]


def _builtin_extractor_endpoint() -> tuple[str, str, str]:
    """合同提取内置默认端点（agent nww-contract-extractor）：模型中心 omlx provider + Ornith。

    配置被模型中心「发现模型+保存」全量覆盖时仍保通道可用（实测事故 2026-10-31 16:30）；
    omlx provider 缺失 → 回退 localhost:8080 默认（key 空）。返回 (base_url, model, api_key)。
    """
    model = "Ornith-1.5-35B-A3B-BigBang-oQ4e-mtp"
    try:
        from ..llm_config import _read
        cfg = _read() or {}
        prov = next((p for p in cfg.get("providers", []) if p.get("id") == "omlx"), None)
        if prov:
            base = str(prov.get("base_url") or "http://127.0.0.1:8080/v1").rstrip("/")
            return base, model, str(prov.get("api_key") or "")
    except Exception:  # noqa: BLE001 — 内置默认解析失败按缺省值兜底
        pass
    return "http://127.0.0.1:8080/v1", model, ""


def _writer_stream(system: str, user_prompt: str, *, on_token=None, temperature: float = 0.9, response_format: dict = None, max_tokens: int | None = None, book: Path | None = None, chapter_id: int | None = None, agent_name: str = "narrative-writer") -> tuple[str, str | None]:
    """纯净 writer 流式直连：解析端点 → chat_completion_stream。

    复用 runtime 的端点解析（模型中心优先，回退本地/env 档）与流式传输，
    但不走 run_agent_stream（避免把 agent.method 整份塞进 system）。
    temperature：写章 0.9（发散）；润色/修订 0.3（保守改动，见 step_polish）。
    max_tokens：输出上限（tokens），None=默认 8192；产出接近全稿的 pass
    （润色改写/deslop）按原文长度动态传入，防长章输出截断丢后段。
    agent_name（2026-10-31）：结构化提取类任务（L1/L2 合同）可指定专用路由
    （如 nww-contract-extractor → 专用提取模型），避免绑死网页桥——桥接 agent
    协议污染/桥故障曾致合同提取连续失败。默认值不变=既有调用方零影响；
    未注册回退 narrative-writer 并 WARN 显式标记（不静默伪装）。
    返回 (text, finish_reason)。
    """
    try:
        agent = registry.get_agent(agent_name)
    except ValueError:
        agent = registry.get_agent("narrative-writer")
        print(f"[CONTRACT_ROUTE] WARN: agent `{agent_name}` 未注册，"
              f"fallback → narrative-writer（实际模型非 {agent_name} 专用路由）")
    # 2026-10-31 二次修复：模型中心配置会被前端「发现模型+保存」全量覆盖（实测 16:30
    #   抹掉手写路由），合同提取通道不能依赖配置存续——路由缺失时使用内置默认
    #   （omlx provider + Ornith），配置命中则配置优先；两种情况都显式留痕，不静默。
    _override = None
    if agent_name != "narrative-writer":
        _ep = runtime._cfg_agent_endpoint(agent_name)
        if _ep:
            _override = (_ep[0], _ep[1], _ep[2])
            print(f"[CONTRACT_ROUTE] {agent_name} → 模型中心路由 {_override[0]} / {_override[1]}")
        else:
            _override = _builtin_extractor_endpoint()
            print(f"[CONTRACT_ROUTE] WARN: agent_routes 缺少 {agent_name} 路由（可能被模型中心保存覆盖），"
                  f"使用内置默认 {_override[0]} / {_override[1]}")
    if _override is not None:
        base_url, model, key = _override
        timeout, _max = runtime._TIMEOUT, 8192
    else:
        base_url, model, key, timeout, _max = runtime._route_endpoint(agent)
    _ = _max

    def _observe(*, attempt, status_code, raw_error) -> None:  # 纯旁路：仅观测，不改调用/返回值
        _record_llm_stream_observation(
            book=book, chapter_id=chapter_id, endpoint=base_url, model=model,
            attempt=attempt, status_code=status_code, raw_error=raw_error)

    parts: list[str] = []
    fr_holder: list = [None]  # type: ignore[list-item]
    for chunk in runtime.chat_completion_stream(
        system, user_prompt,
        temperature=temperature, max_tokens=max_tokens or 8192,
        base_url=base_url, model=model, api_key=key, timeout=timeout,
        response_format=response_format,
        finish_reason_holder=fr_holder,
        observation_callback=(_observe if (book is not None and chapter_id is not None) else None),
    ):
        parts.append(chunk)
        if on_token is not None:
            try:
                on_token(chunk)
            except runtime.AgentCancelled:
                raise
            except Exception:  # noqa: BLE001 — 流式回调失败不影响主链
                pass
    _full = "".join(parts)
    # 2026-11-01 OOM 错误识别：oMLX 本地模型内存爆了之后不返回 HTTP 错误，
    # 而是把错误信息当正文塞在 SSE 流里返回。客户端之前把它当正常内容收集，
    # 最后只生成几百字被字数闸 BLOCK，用户看到的是"字数不够"而不是"内存爆了"。
    # 现在识别常见的 OOM/中断错误标记，直接抛明确 RuntimeError。
    _oom_patterns = [
        r"process memory limit exceeded",
        r"Request aborted.*memory",
        r"abort threshold",
        r"dynamic ceiling",
        r"out of memory",
        r"OOM",
    ]
    for _pat in _oom_patterns:
        if re.search(_pat, _full, re.I):
            raise RuntimeError(
                f"本地模型内存超限（OOM）：生成被中途终止。\n"
                f"原始错误：{_full[:300]}\n"
                f"解决方法：1) 关闭其他应用释放内存；2) 切换到云端模型（模型中心）；"
                f"3) 或改用更小的本地量化模型。"
            )
    # 2026-11-02 错误报文透传识别（第1章 rewrite L3 实测）：上游网关在 SSE 流里返回
    #   错误 JSON（HTTP 200 + {"error":{"code":404,"message":"Request path not found."}}），
    #   被当正文收集 → 一路流到字数闸才被拦，用户看到的是误导性的
    #   「[WC_GATE] 可见字数 65 < 400」而非真实故障（PROSE_SANITY 只 OBSERVE 不阻断）。
    #   与 2026-11-01 OOM 识别同款模式。判据：剥空白后全文本身解析为 JSON **且顶层含
    #   "error" 键** → 必为错误报文（正文不可能整体是一个带顶层 error 键的 JSON；
    #   plan/合同类合法 JSON 输出均无顶层 error 键），确定性零误伤。
    _raise_if_upstream_error_payload(_full)
    # 2026-11-06：网页桥「结果分区 / 零执行」报告模板同样不是正文，须在字数闸之前
    # 明确归因（否则用户只会看到「可见字数 349 < 400」）。见下方函数注释。
    _raise_if_bridge_report(_full, endpoint=base_url, model=model)
    return _full, fr_holder[0]


def _raise_if_upstream_error_payload(text: str) -> None:
    """全文为「顶层含 error 键的 JSON」时抛明确 RuntimeError（上游故障透传，见 2026-11-02）。"""
    _stripped = str(text or "").strip()
    if not (_stripped.startswith("{") and _stripped.endswith("}")):
        return
    try:
        _j = json.loads(_stripped)
    except (json.JSONDecodeError, ValueError):
        return
    if isinstance(_j, dict) and "error" in _j:
        _msg = json.dumps(_j["error"], ensure_ascii=False)[:300]
        raise RuntimeError(
            f"上游模型返回错误报文（HTTP 200 但 SSE 流内透传错误 JSON），未生成正文：{_msg}\n"
            f"解决方法：1) 点「继续重试」（多为网关瞬时故障）；2) 到模型中心检查当前端点/模型是否可用。"
        )


# —— 网页桥「零执行任务报告」识别（2026-11-06 第1章 rewrite 实测）——
# 事故：agent_routes.narrative-writer 指向 webui-chrome-qwen 时，桥接交付层
#   （openai-webui-api/src/chrome_llm_bridge/proxy.py:570 生成模板、:3934 追加到
#   OpenAI 兼容 API 的响应；开关 BRIDGE_DELIVER_STRUCT 默认开）对**机器消费**的
#   writer 请求也追加面向 CLI 人类的「════ 结果分区」8 分区模板。收流得到 377 字符
#   / 349 可见字的零执行报告，无一行 <prose> 正文；既有 OOM / JSON-error /
#   PROSE_SANITY 三种识别均不命中，一路流到字数闸被误报成「可见字数 349 < 400」。
#   与 2026-11-02 上游错误报文透传同族，只是载荷换成了桥接模板。
# 判据 = 桥接模板的固定字面量，合法正文不可能含 → 确定性零误伤；**只检测不改写**，
#   因此 _writer_stream 的 10 个非正文调用方（JSON plan / 合同抽取 / 润色等）零影响。
# env NWW_BRIDGE_REPORT_GUARD=0 逐字回滚到改动前行为。
_BRIDGE_REPORT_MARKS = (
    "════ 结果分区（桥接整理，非模型自述）════",
    "本轮未真实执行任何命令（零执行）",
    "无（本轮零真实执行成功，桥接侧无证据）",
    "无（未验证：本轮零执行，结论无本地执行证据）",
)


def _raise_if_bridge_report(text: str, *, endpoint: str = "", model: str = "") -> None:
    """上游返回网页桥「结果分区 / 零执行」报告时抛明确故障（不改写正文、零 LLM）。"""
    if not _env_flag_on("NWW_BRIDGE_REPORT_GUARD", "1"):
        return
    s = str(text or "")
    hits = [m for m in _BRIDGE_REPORT_MARKS if m in s]
    if not hits:
        return
    raise RuntimeError(
        f"上游未产出正文：模型通道返回了网页桥的「结果分区 / 零执行」报告模板"
        f"（命中 {len(hits)} 个固定标记，全文 {len(s)} 字符）。这不是一章正文，"
        f"也不是字数不足。\n"
        f"当前端点：{endpoint or '未知'}；模型：{model or '未知'}\n"
        f"处置：1) 到模型中心把 narrative-writer 切到 API 直连"
        f"（如 agnes:agnes-3.0-flash）；2) 或在桥接服务设 BRIDGE_DELIVER_STRUCT=0"
        f" 关闭模板追加；3) 然后重试。"
    )


# —— 2026-10-12 外脑判定 4(b)：扩写路径的 scene_chain 摘要预算 ——
# 原实现（pipeline.py:907-911）只拼 decision.choice / driver_goal.goal，
#   且每条硬截断 80 字符、整体 600 字符 → beats 回退后实测：
#   「执行边界」与「功能标签」在该路径**完全不出现**，长情节点被砍掉后半段。
# 现改为：情节点 + 关键选择 + 执行边界 + 功能标签 全量拼接；
#   单条上限 300（防单条吃掉预算，非硬截断语义），整体上限 1800（env 可调）。
# 回滚（三者齐设 == 修复前逐字节等价）：
#   NWW_EXPAND_SC_CHARS=600 + NWW_EXPAND_SC_ITEM_CHARS=80 + NWW_EXPAND_SC_PARTS=goal
_EXPAND_SC_CHARS_SWITCH = "NWW_EXPAND_SC_CHARS"
_EXPAND_SC_ITEM_CHARS_SWITCH = "NWW_EXPAND_SC_ITEM_CHARS"
_EXPAND_SC_PARTS_SWITCH = "NWW_EXPAND_SC_PARTS"


def _scene_chain_outline(packet: dict) -> str:
    """scene_chain → 单行摘要（扩写/规划路径共用）。

    只增不改：原字段名与取值优先级不变（decision.choice 优先于 driver_goal.goal）；
    新增 constraint（执行边界）与 craft_notes（功能标签）两段，并放宽单条上限。
    """
    try:
        budget = int(os.environ.get(_EXPAND_SC_CHARS_SWITCH, "1800") or 1800)
    except (TypeError, ValueError):
        budget = 1800
    try:
        item_cap = int(os.environ.get(_EXPAND_SC_ITEM_CHARS_SWITCH, "300") or 300)
    except (TypeError, ValueError):
        item_cap = 300
    want = {w.strip() for w in
            str(os.environ.get(_EXPAND_SC_PARTS_SWITCH, "goal,constraint,craft") or "").split(",")
            if w.strip()}
    parts: list[str] = []
    for s in (packet.get("scene_chain") or []):
        if not isinstance(s, dict):
            continue
        dg = s.get("driver_goal") or {}
        ev = (s.get("decision", {}).get("choice")
              or (dg.get("goal") if isinstance(dg, dict) else "") or "")
        ev = str(ev or "")[:item_cap]
        if not ev:
            continue
        seg = ev
        if "constraint" in want:
            con = str((dg.get("constraint") if isinstance(dg, dict) else "") or "")[:item_cap]
            if con:
                seg += f"（边界：{con}）"
        if "craft" in want:
            notes = [str(x) for x in (s.get("craft_notes") or []) if str(x).strip()]
            if notes:
                seg += "〔" + "/".join(notes) + "〕"
        parts.append(seg)
    return "；".join(parts)[:budget]


def _segmented_expand(book: Path, chapter_id: int, packet: dict, base_text: str,
                      wc_target, on_token=None) -> str:
    """分块增量扩写（2026-09-22 字数治理 v2）。

    单次全稿扩写服从率 1/6（实测六次调用增量 +11/+18/+371/-211/0/+2367），
    失败形态多为「原样返回/微调」——LLM 对「保留全文+新增 N 字」的全稿任务
    服从度不稳定，且失败无结构性原因（纯随机）。分块策略把不可靠的全稿行为
    转化为可组合的局部行为：
    - 基底按空行分段聚合为 4 块，标题段原样保留不扩；
    - 每块独立调用：局部任务「把这段展开到 N 字」，输入小（数百字）→ 体量
      锚定弱、任务边界清晰，服从度显著高于全稿任务；
    - 块级保护回退：新块可见字 < 原块 80% → 用原块（扩写只许增不许减）；
    - 程序拼接成稿，守卫链全量复检（拼接稿过 G_Length/B/C/D/E 门）。
    """
    target = 0
    try:
        target = int(str(wc_target).strip()) if wc_target else 0
    except (TypeError, ValueError):
        target = 0
    hardline = int(target * 0.7) if target > 0 else int(len(re.sub(r"\s+", "", base_text)) * 1.6)
    # 标题段剥离（原样保留）
    paras = [p for p in base_text.split("\n\n") if p.strip()]
    head = ""
    if paras and paras[0].lstrip().startswith("#"):
        head = paras[0]
        paras = paras[1:]
    n = 4 if len(paras) >= 8 else max(1, len(paras) // 2)
    if len(paras) <= n:
        blocks = ["\n\n".join(paras)]
        n = 1
    else:
        per = len(paras) // n
        blocks = ["\n\n".join(paras[i * per:(i + 1) * per]) for i in range(n - 1)]
        blocks.append("\n\n".join(paras[(n - 1) * per:]))
    _ig = packet.get("intent", {}).get("goal", {}).get("value") or packet.get("intent", {}).get("goal", "") or ""
    _sc = _scene_chain_outline(packet)
    outline = str(_ig)[:900] + ("；" + _sc if _sc else "")[:2500]
    out_blocks: list[str] = []
    for i, blk in enumerate(blocks, 1):
        blk_vis = len(re.sub(r"\s+", "", blk))
        per_target = max(blk_vis + 200, hardline // max(n, 1))
        ctx = ("（上一段结尾：……" + re.sub(r"\s+", "", out_blocks[-1])[-120:] + "）\n\n") if out_blocks else ""
        system = (
            "你是顶级网络小说主笔。下面给你章节全文中的一段（以及全章细纲供参考），"
            f"你的唯一任务：在完全保留这段剧情、对话内容、事件顺序与结果的基础上，"
            f"把这段展开到约 {per_target} 字（当前 {blk_vis} 字）。\n"
            "展开手段：把概述性对话改成有来有往的多轮对白（追问、反驳、停顿、插话）；"
            "把动作过程写细（人物怎么做、对方什么反应）；补场景可见细节（声音、物件、旁人反应）。\n"
            "禁止：删减或合并这段的任何已有内容；发明这段之外的新剧情、新角色、新设定；"
            "注水（重复表述、空话、无信息量的环境堆砌）；总结感悟腔；使用……和——。\n"
            "输出：直接输出这一段展开后的正文，不含章节标题、不含任何说明或自评，保持空行分段。"
        )
        prompt = f"【全章细纲（展开方向的参考，不引入新剧情）】\n{outline}\n\n【上一段衔接】\n{ctx}【待展开段落】\n{blk}"
        try:
            text, _fin = _writer_stream(system, prompt, on_token=on_token,
                                        max_tokens=max(8192, per_target * 3),
                                        book=book, chapter_id=chapter_id)
        except Exception as _exc:  # noqa: BLE001 — 单块失败回退原块，整体继续
            print(f"[SEG_EXPAND] block{i} 调用异常: {_exc}，用原块", flush=True)
            out_blocks.append(blk)
            continue
        txt = (text or "").strip()
        # 块级保护回退：只许增不许减（新块 < 原块 80% → 用原块）
        txt_vis = len(re.sub(r"\s+", "", txt))
        if txt_vis < blk_vis * 0.8:
            print(f"[SEG_EXPAND] block{i} 塌缩 {blk_vis}→{txt_vis}，用原块", flush=True)
            out_blocks.append(blk)
        else:
            print(f"[SEG_EXPAND] block{i}: {blk_vis}→{txt_vis}字（目标~{per_target}）", flush=True)
            out_blocks.append(txt)
    result = ("\n\n".join(([head] if head else []) + out_blocks)).strip()
    print(f"[SEG_EXPAND] 完成: 基底 {len(re.sub(r' ', '', base_text))} → 成稿 {len(re.sub(r' ', '', result))} 字", flush=True)
    return result


def check_output_completeness(text: str, finish_reason: str | None) -> dict:
    """Writer Output Completeness Gate — 确定性截断检测。
    
    只检测高置信度异常，不做 NLP 级句子完整性判断。
    返回 {"status": "PASS"|"BLOCK", "reason": "...", "evidence": "..."}
    """
    if not text or not text.strip():
        return {"status": "BLOCK", "reason": "EMPTY_OUTPUT", "evidence": "正文为空"}
    
    # 第一层：finish_reason == length
    if finish_reason == "length":
        tail = text.strip()[-30:] if len(text) > 30 else text.strip()
        return {"status": "BLOCK", "reason": "FINISH_REASON_LENGTH", "evidence": f"finish_reason=length, tail=...{tail}"}
    
    # 第二层：明显的尾部标点冲突（中文停顿标点后立即出现句末标点）
    tail_text = text.rstrip()
    trailing_conflicts = [
        "，。", "，！", "，？", "，”", "，』", "，」",
        "、。", "、！", "、？",
    ]
    for conflict in trailing_conflicts:
        if tail_text.endswith(conflict):
            return {"status": "BLOCK", "reason": "TRAILING_PUNCTUATION_CONFLICT", "evidence": f"tail=...{tail_text[-20:]}"}
    
    # 第三层：末尾悬空标点（文本以停顿/开放标点结尾）
    trailing_open = ["，", "、", "：", "；", "（", "“", "《", "——"]
    for char in trailing_open:
        if tail_text.endswith(char):
            return {"status": "BLOCK", "reason": "TRAILING_OPEN_PUNCTUATION", "evidence": f"tail ends with '{char}': ...{tail_text[-20:]}"}

    # ★★ 第四层：成稿健全性（2026-10-12 A/B 实测确诊的两个洞，默认**只观测不阻断**）
    #   实测：同章同 packet 连跑 10 次，产物字数 229 ~ 13231（**57 倍**）；
    #   其中 229 字那一篇根本不是正文，而是上游 API 的**错误报文**
    #   （`{"error":{"message":"Incorrect content type...","code":"invalid_content_type"}}`），
    #   被当成成稿一路 PASS 下来。旧闸门只查「空/截断/尾部标点」，**两项都查不出来**。
    #   开关 NWW_PROSE_SANITY=1 才阻断；默认 0 → 只打印告警，行为与改动前完全一致。
    sanity = _prose_sanity(text)
    for i in sanity.get("issues") or []:
        # 超长类（blocking=False）永远只观测，不受 NWW_PROSE_SANITY 影响（外脑 Q6）
        if not i.get("blocking"):
            print(f"[PROSE_SANITY][OBSERVE-NONBLOCKING] {i['kind']} — {i['evidence']}",
                  flush=True)
    if not sanity["ok"]:
        if _prose_sanity_blocks():
            return {"status": "BLOCK", "reason": sanity["reason"],
                    "evidence": sanity["evidence"]}
        print(f"[PROSE_SANITY][OBSERVE] {sanity['reason']} — {sanity['evidence']}", flush=True)

    return {"status": "PASS", "reason": "OK", "evidence": ""}


# —— 成稿健全性（第四层）——
_PROSE_SANITY_SWITCH = "NWW_PROSE_SANITY"
# 错误报文特征：JSON 形态 + 上游错误码/错误键
_PROSE_ERROR_MARKS = (
    '"invalid_request_error"', '"bridge_error"', '"invalid_content_type"',
    '"error":', "'error':", "Incorrect content type", "RemoteProtocolError",
)
# 可见字数下限（默认 400；够低，只拦"明显不是一章"的残骸，不误伤短章）
_PROSE_MIN_CHARS = int(os.getenv("NWW_PROSE_MIN_CHARS", "400") or 400)
# ★ 外脑 Q5/Q6 裁决（2026-10-12）：
#   Q5 —— 400 只是"低成本残骸阈值"，**不是章节有效性标准**；应另建按细纲目标比例的闸门。
#          外脑明确说「70% 是实验起点，不是已证明的最优阈值」，故默认 0=关闭，待回放校准。
#   Q6 —— 超长要检测，但**现阶段不得硬阻断**（可能误伤有意的长章），先只观测告警。
_PROSE_MIN_RATIO = float(os.getenv("NWW_PROSE_MIN_RATIO", "0") or 0)   # 0=关
_PROSE_MAX_RATIO = float(os.getenv("NWW_PROSE_MAX_RATIO", "0") or 0)   # 0=关（超长，只观测）


def _prose_sanity_blocks() -> bool:
    """默认 False（只观测）。置 NWW_PROSE_SANITY=1 才真正阻断。"""
    return os.environ.get(_PROSE_SANITY_SWITCH, "0") == "1"


def _prose_sanity_issues(text: str, wc_target: str | int | None = None) -> list[dict]:
    """★ 外脑 Q7 要求：把「错误报文识别」与「字数判定」**拆开**分别评估，
    才能分别统计真阳性/假阳性/真阴性/假阴性 —— 混在一个布尔里无法判断误拦风险。

    返回 [{"kind": ..., "blocking": bool, "evidence": ...}]：
      kind ∈ EMPTY_PROSE / ERROR_PAYLOAD_AS_PROSE / PROSE_TOO_SHORT / PROSE_TOO_LONG
      blocking=True 表示该类问题**允许**在开关打开时被阻断；
      超长类永远是 blocking=False（外脑 Q6：现阶段不得硬阻断）。
    """
    out: list[dict] = []
    body = (text or "").strip()
    if not body:
        return [{"kind": "EMPTY_PROSE", "blocking": True, "evidence": "正文为空"}]
    # ① 上游错误报文被当作成稿（剥 markdown 代码围栏后再判 JSON）
    stripped = re.sub(r"^[`~]{3,}[A-Za-z]*\s*", "", body).strip()
    if stripped.startswith("{") and any(m in body for m in _PROSE_ERROR_MARKS):
        out.append({"kind": "ERROR_PAYLOAD_AS_PROSE", "blocking": True,
                    "evidence": f"疑似上游错误报文被当成正文: {stripped[:120]}"})
    # ② 字数判定
    #   口径统一走权威实现（同 check_wordcount_gate）：自造的 len(re.sub(r"\s+","",body))
    #   实测比权威 visible_chars_v1 偏高 10~16 字（不剥标题行），与下游不一致。
    visible, _src = _visible_chars(body)
    if visible < _PROSE_MIN_CHARS:
        out.append({"kind": "PROSE_TOO_SHORT", "blocking": True,
                    "evidence": f"可见字数 {visible} < 绝对下限 {_PROSE_MIN_CHARS}"})
    try:
        tgt = int(str(wc_target).strip()) if wc_target else 0
    except (TypeError, ValueError):
        tgt = 0
    if tgt > 0:
        if _PROSE_MIN_RATIO > 0 and visible < tgt * _PROSE_MIN_RATIO:
            out.append({"kind": "PROSE_BELOW_TARGET", "blocking": True,
                        "evidence": f"可见字数 {visible} < 细纲目标 {tgt}×{_PROSE_MIN_RATIO}"
                                    f"(≈{int(tgt*_PROSE_MIN_RATIO)})"})
        if _PROSE_MAX_RATIO > 0 and visible > tgt * _PROSE_MAX_RATIO:
            out.append({"kind": "PROSE_TOO_LONG", "blocking": False,   # ★ Q6：只观测
                        "evidence": f"可见字数 {visible} > 细纲目标 {tgt}×{_PROSE_MAX_RATIO}"
                                    f"(≈{int(tgt*_PROSE_MAX_RATIO)})"})
    return out


def _prose_sanity(text: str, wc_target: str | int | None = None) -> dict:
    """判定「这份内容到底是不是一章正文」。零 LLM、纯确定性、失败即放行。"""
    try:
        issues = _prose_sanity_issues(text, wc_target)
        blocking = [i for i in issues if i.get("blocking")]
        if blocking:
            i = blocking[0]
            return {"ok": False, "reason": i["kind"], "evidence": i["evidence"],
                    "issues": issues}
    except Exception:  # noqa: BLE001 — 观测逻辑绝不能反过来弄挂生产
        return {"ok": True, "reason": "", "evidence": ""}
    return {"ok": True, "reason": "", "evidence": "", "issues": issues if text else []}


# —— 字数闸门（Wordcount Gate，2026-10-12 外脑 Q4 裁决）——
# 裁决原文：「新增 check_wordcount_gate，在写作产物进入提交或下游消费之前调用；
#            不要直接把发布器的 2000 字标准塞进 check_output_completeness。」
#
# 为什么必须**独立**：全仓核实 check_output_completeness 共 4 个调用方，其中 3 个
#   （_l2_structure_plan / _rewrite_analyze_contract / _l3_reproduction_plan）
#   的产物按设计就是短的（结构计划 / 契约抽取 / 复现计划），塞 2000 字标准必误杀。
#   唯一真正的成稿产地是 step_writer —— 闸门只挂在那里。
#
# 计数口径 unit = "visible_chars_v1"
#   ★ 权威实现**只有一份**：skills/nww-novel-skill/scripts/nww_wordcount.py::count_visible_chars
#     （web 侧经 books.py:16 复用 —— 本文件已 `from .. import books`，直接取用即可）
#   ★ 实测否证（2026-10-12）：自造的 `len(re.sub(r"\s+","",text))` 与权威口径**并不相等** ——
#     对 100 章真实正文逐章比对，自造口径**系统性偏高 10~16 字，无一章相等**，
#     原因是权威口径 visible_body() 会剥掉首行 ATX 标题（`# 第N章　标题`）后再计数。
#     → 若闸门与下游 step_wordcount_check / books.py 树状图用不同口径，同一章会出现两个字数。
#       这正是本项目「三套评分」同型事故，故**禁止再造第二份实现**。
#   → 400（残骸下限）/ 2000（单章合格线）/ 3500（细纲目标）三者可直接比较，无需换算。
#
# 三态语义：
#   PASS       达标，放行
#   QUARANTINE 确实是正文但**未达标** → 不得静默发布，交修订策略（不硬阻断，Q6 同原则）
#   BLOCK      低于残骸硬下限 → 这不是一章（错误报文 / 截断残骸），必须拦
#
# ★ 400 与 2000/细纲目标**互相独立**：400 只判「是不是一章」，目标只判「够不够」。
#   二者不得合并成一个阈值，否则短章会被当成残骸杀掉，或残骸被当成短章放行。

_WC_GATE_SWITCH = "NWW_WC_GATE"
WC_UNIT = "visible_chars_v1"
# 无细纲目标时的兜底合格线（与发布器 chapter_min_words 同标准，env 可调）
_WC_FALLBACK_MIN = int(os.getenv("NWW_WC_FALLBACK_MIN", "2500") or 2500)  # 用户字数规范 2026-10-25：基准 2500
# 最近一次闸门结果（供下游读取，解决 step_writer 只返回 str 无法附带判定结果的问题）
LAST_WC_GATE: dict = {}


def _wc_gate_mode() -> str:
    """on（默认）= BLOCK 阻断 / QUARANTINE 观测；off = 全观测（回滚开关）；strict = 二者皆阻断。"""
    v = (os.environ.get(_WC_GATE_SWITCH, "on") or "on").strip().lower()
    return v if v in ("on", "off", "strict") else "on"


def _resolve_wc_target(*candidates) -> int:
    """把任意来源的字数目标归一成 int；无法解析 / 非正 → 0（表示「无目标」）。

    外脑 test_wordcount_invalid_target 要求：非法目标不得抛异常，也不得被当成
    「目标=0」去硬拦 —— 缺目标和「目标为零字」是两件事。
    """
    for cand in candidates:
        if cand is None:
            continue
        try:
            v = int(str(cand).strip())
        except (TypeError, ValueError):
            continue
        if v > 0:
            return v
    return 0


def _visible_chars(text: str) -> tuple[int, str]:
    """visible_chars_v1 计数。返回 (字数, 来源)。

    必须走**权威实现**（books.nww_wordcount），不得自己再算一遍 —— 理由见上方口径说明。
    来源回退到 "fallback" 属异常，测试会断言正常路径恒为 "nww_wordcount"。
    """
    try:
        return int(books.nww_wordcount.count_visible_chars(text)), "nww_wordcount"
    except Exception:  # noqa: BLE001 — 计数失败绝不能弄挂生产，回退等价近似
        return len(re.sub(r"\s+", "", (text or "").strip())), "fallback"


def check_wordcount_gate(text: str, *, wc_target: str | int | None = None,
                         chapter_min: str | int | None = None,
                         wc_max: str | int | None = None,
                         is_revision: bool = False) -> dict:
    """独立的成稿字数闸门。零 LLM、纯确定性、只读。

    参数
      wc_target   细纲字数目标（packet.wordcount.target，权威来源）
      chapter_min 兜底合格线（无细纲目标时用；发布器 chapter_min_words 标准）
      wc_max      声明的上限（仅在**严格大于** target 时才生效——单数字细纲会解析成
                  min==max==target，那种情况下「超过 target」是正常的，不得判超长）
      is_revision 本次是修订/重写轮（未达标走 revision 策略，不判 rewrite）

    返回 {"status","reason","actual","target","floor","unit","policy","metric_source"}
      status ∈ PASS / QUARANTINE / BLOCK
      policy ∈ "" / "revision" / "rewrite" / "trim"
      metric_source = "nww_wordcount"（正常） / "fallback"（异常，需告警）
    """
    try:
        actual, _src = _visible_chars(text)
        target = _resolve_wc_target(wc_target, chapter_min)
        floor = _PROSE_MIN_CHARS
        try:
            mx = int(str(wc_max).strip()) if wc_max else 0
        except (TypeError, ValueError):
            mx = 0
        res = {"status": "PASS", "reason": "OK", "actual": actual,
               "target": (target or None), "floor": floor, "unit": WC_UNIT,
               "policy": "", "mode": _wc_gate_mode(), "metric_source": _src}
        if _src != "nww_wordcount":
            print(f"[WC_GATE][WARN] 字数口径回退到近似实现（{_src}），"
                  f"与下游 step_wordcount_check 可能不一致", flush=True)

        # ① 残骸判定（只判「是不是一章」，与目标无关）
        if actual == 0:
            res.update(status="BLOCK", reason="EMPTY_PROSE", policy="rewrite")
            return res
        if actual < floor:
            res.update(status="BLOCK", reason="BELOW_HARD_FLOOR", policy="rewrite")
            return res

        # ② 无目标：不得判达标，也不得判未达标 —— 显式声明，禁止静默伪装成 PASS
        if target <= 0:
            res.update(status="PASS", reason="NO_TARGET_DECLARED")
            return res

        # ③ 超长：QUARANTINE，**永不为 BLOCK**（外脑 Q6：现阶段不得硬阻断长章）
        if mx > target and actual > mx:
            res.update(status="QUARANTINE", reason="ABOVE_DECLARED_MAX", policy="trim")
            return res

        # ④ 未达标：QUARANTINE（不得静默发布）
        if actual < target:
            res.update(status="QUARANTINE", reason="BELOW_TARGET",
                       policy=("revision" if is_revision else "rewrite"))
            return res

        return res
    except Exception as _exc:  # noqa: BLE001 — 闸门绝不能反过来弄挂生产
        # R10（2026-11-13）：异常 = 未得出结论，不得伪装成 PASS；不阻断（与闸门 fail-open 一致）。
        return {"status": "GATE_ERROR", "reason": f"GATE_ERROR:{_exc}", "actual": -1,
                "target": None, "floor": _PROSE_MIN_CHARS, "unit": WC_UNIT,
                "policy": "", "mode": _wc_gate_mode(), "metric_source": "error"}


def _wc_stage_note() -> str:
    """把最近一次字数闸门结果拼进 stage detail。

    LAST_WC_GATE 若只写不读 = 半个功能；这里让它真正到达前端 /api/agents/task/{id}
    的 stages[].detail —— 未达目标的章会显示「字数闸门 QUARANTINE：2000/3500」。
    """
    g = LAST_WC_GATE or {}
    if not g or g.get("status") == "PASS":
        return ""
    tgt = g.get("target")
    return (f"；字数闸门 {g.get('status')}（{g.get('reason')}）："
            f"{g.get('actual')}/{tgt if tgt else '无目标'} {g.get('unit')}")


# 生产质量遥测层（review_observe，2026-09 外脑裁定接入生产闭环）。
# 在 post_write(BLOCK) + deslop 之后、return 之前，以 NWW_QUALITY_OBSERVE=1 开关
# 灰度开启。只观察：跑 detect_internal（确定性 I-A~I-J）写"质量观察" JSON，不改正文/
# stages/return ok/tracking/评分，不触发 revision，异常隔离不影响章节提交。
_OBSERVE_SWITCH = "NWW_QUALITY_OBSERVE"


def review_observe(book: Path, chapter_id: int, body_path: Path,
                   artifact_dir: Path | None = None) -> dict:
    """跑确定性质量遥测（detect_internal I-A~I-J），写 {book}/追踪/质量观察/第N章.json。

    creative_value 第一阶段留 null 占位（未来接 LLM 观察），不引 LLM、不改正文。
    任何异常只返回空 dict，绝不影响主写章链。

    artifact_dir 显式传入（dry_run）→ 质量观察 JSON 落本次 execution 的 artifact root，
    绝不写正式 追踪/质量观察/；缺省 None → 原正式路径（行为不变）。
    """
    outcome: dict = {"chapter": chapter_id, "source": "post_write_quality_observe", "quality_observe": {}, "meta": {}}
    try:
        body_text = body_path.read_text(encoding="utf-8")
        if not body_text.strip():
            return outcome
        quality_dir = artifact_dir if artifact_dir is not None else (book / "追踪" / "质量观察")
        quality_dir.mkdir(parents=True, exist_ok=True)

        # detect_internal 位于 {repo}/quality/；临时加入 sys.path 以 import run_all
        repo_quality = Path(__file__).resolve().parents[4] / "quality"
        import json as _json
        internal = {}
        try:
            import sys as _sys
            if str(repo_quality) not in _sys.path:
                _sys.path.insert(0, str(repo_quality))
            from detect_internal import run_all
            from objective_scorer import clean_text
            cleaned = clean_text(body_text)
            if cleaned.strip():
                raw = run_all(cleaned)
                internal = {
                    k: {"status": v.get("status"), "score": v.get("score"), "detail": (v.get("detail") or "")[:200]}
                    for k, v in raw.items()
                }
        except Exception as _ie:  # noqa: BLE001 — detect 失败不阻断
            internal = {"_error": str(_ie)[:200]}

        out = {
            "chapter": chapter_id,
            "source": "post_write_quality_observe",
            "quality_observe": {
                "internal": internal,
                "creative_value": {"memory_point": None, "emotion_peak": None, "character_shift": None},
            },
            "meta": {"blocking": False, "revision_trigger": False, "observer_version": "v1"},
        }
        path = quality_dir / f"第{chapter_id:03d}章.json"
        path.write_text(_json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        return out
    except Exception as _exc:  # noqa: BLE001 — 遥测失败绝不悬挂主链
        return outcome


# 主笔输出协议：正文必须放在 <prose> 标签内（agent 自我界定边界，标签外不落盘）
_PROSE_RE = re.compile(r"<prose>(.*?)</prose>", re.DOTALL)


def _extract_prose(draft: str) -> str:
    """按 <prose> 协议提取正文：只有标签内的内容会被落盘。

    有 <prose> 块 → 取块内内容（多块按序拼接）；
    无 <prose> 块 → 整段回退（模型未遵守协议，交由 _sanitize_writer_output 兜底剥元信息）。
    """
    blocks = _PROSE_RE.findall(draft)
    if blocks:
        return "\n\n".join(b.strip() for b in blocks).strip()
    return draft


# ---- 自动跨章去重（流程闭环：检测到即自动修复，不再需要人工） ----
_SENT_SPLIT_RE = re.compile(r"[^。！？；\n]+[。！？；]?")


def _split_sentences(text: str) -> list[str]:
    """按中英文句末标点切句。"""
    return [s.strip() for s in _SENT_SPLIT_RE.findall(text) if s.strip()]


def _norm_sentence(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _read_prev_chapter_full(book: Path, chapter_id: int) -> str:
    """读上一章正文全文（去标题行）。"""
    if chapter_id <= 1:
        return ""
    body = book / "正文"
    if not body.is_dir():
        return ""
    for p in sorted(body.rglob("第*章*.md")):
        if _is_non_prose(p):
            continue        # 快照/备份不是正文，别拿它当「上一章」做去重比对
        m = re.match(r"^第0*(\d+)章", p.name)
        if m and int(m.group(1)) == chapter_id - 1:
            try:
                lines = [ln for ln in p.read_text(encoding="utf-8", errors="replace").splitlines() if not ln.lstrip().startswith("#")]
                return "\n".join(lines)
            except OSError:
                return ""
    return ""


def _auto_dedup_prev_chapter(book: Path, chapter_id: int, text: str) -> tuple[str, list[str]]:
    """确定性自动去重：删除正文中与上一章逐字相同的句子（跨章复制闭环兜底）。

    规则：与上一章任一句子标准化后完全相同且长度 ≥15 字的句子删除；
    被删句所在行重组（行内其余句保留）；返回 (去重后文本, 被删句子列表)。
    合法短句呼应（<15 字）不删，避免误伤正常钩子延续。
    """
    if not text or chapter_id <= 1:
        return text, []
    prev_full = _read_prev_chapter_full(book, chapter_id)
    if not prev_full:
        return text, []
    prev_set = {_norm_sentence(s) for s in _split_sentences(prev_full)}
    removed: list[str] = []
    out_lines: list[str] = []
    for raw in text.splitlines():
        if not raw.strip():
            out_lines.append("")
            continue
        sents = _split_sentences(raw)
        if not sents:
            out_lines.append(raw)
            continue
        kept = [s for s in sents if not (_norm_sentence(s) in prev_set and len(_norm_sentence(s)) >= 15)]
        if len(kept) == len(sents):
            out_lines.append(raw)
            continue
        removed.extend(s for s in sents if s not in kept)
        if kept:
            out_lines.append("".join(kept))
    # 清理连续空行
    clean: list[str] = []
    prev_blank = False
    for ln in out_lines:
        if not ln.strip():
            if not prev_blank:
                clean.append("")
            prev_blank = True
        else:
            clean.append(ln)
            prev_blank = False
    return "\n".join(clean).strip(), removed


def step_normalize(book: Path, chapter_id: int, draft: str) -> tuple[str, str]:
    """正文归一环节（独立正规环节，writer 只生成、本环节接手处理）：

    ① <prose> 协议提取（agent 自我界定正文边界）
    ② 元信息清洗（剥离英文思考/写作指示/工具调用/中段烟火值旁注/标题前残留）
    ③ 自动跨章去重（删除与上一章逐字相同的长句）
    返回 (正文文本, 报告文案)。
    """
    clean = _sanitize_writer_output(_extract_prose(draft))
    clean, removed = _auto_dedup_prev_chapter(book, chapter_id, clean)
    # 2026-10-24 扩展：新章（尚无既存正文）以单章细纲标题为权威源，防 writer 自造标题
    # 进入正文与文件名；既有正文的章一切行为不变（该函数在 existing 非空时直通）。
    clean = _apply_outline_title_for_new_chapter(book, chapter_id, clean)
    # 2026-09-15 根治：标题必须存在且三位置一（重写时以既有文件名为权威源回填）
    clean = _ensure_title_consistent(book, chapter_id, clean)
    rep = "prose 提取 + 元信息清洗 + 标题一致性回填"
    if removed:
        rep += f"；自动去重 {len(removed)} 句（与上一章重复）"
    return clean, rep


# ---- D1 字数收敛（复用 nww_ctl.py chapter check 判定，不新增自定义规则） ----
# 2026-10-25 加宽：兼容「字数目标：2000-2500」与「字数目标 2000-2500」（无冒号）——
# 通行细纲两种写法并存，单一冒号形态会让回退路径静默取不到 target（第001章事故同型）。
_WC_TARGET_RE = re.compile(r"字数目标\s*[：:]?\s*(\d+(?:\s*[-~]\s*\d+)?)")

# 扩写 note 里的“扩到N字”目标：优先匹配 扩到/扩至/扩写至/目标字数，单值或区间
_WC_NOTE_RANGE_RE = re.compile(r"(?:扩到|扩至|扩写至|目标字数|字数目标)\s*[:：]?\s*(\d{3,5})(?:\s*[~\-到]\s*(\d{3,5}))?\s*字?")


def _parse_wc_note(note: str) -> tuple[int, int] | None:
    """从扩写 note 提取目标字数区间 (lower, upper)。单值 → (n, n)；无匹配/非法 → None。"""
    m = _WC_NOTE_RANGE_RE.search(note or "")
    if not m:
        return None
    lower = int(m.group(1))
    upper = int(m.group(2)) if m.group(2) else lower
    if not (0 < lower <= upper <= 99999):
        return None
    return (lower, upper)


def _wc_band_pct() -> float:
    """±10% 带系数唯一权威源 = nww_wordcount.band_pct()（env NWW_WC_BAND_PCT）。

    2026-11-05 用户裁决：读取失败必须显式报错，禁止静默回落另一套默认值——
    否则 NWW_WC_BAND_PCT=0.08 回滚时会与本处回落默认 0.10 形成两套口径。
    """
    import sys as _sys  # noqa: PLC0415 — 惰性加载 skill 权威副本（同 step_wordcount_check 模式）
    _scripts_dir = str(toolbox._SCRIPTS)
    if _scripts_dir not in _sys.path:
        _sys.path.insert(0, _scripts_dir)
    try:
        from nww_wordcount import band_pct as _band_pct
    except ImportError as _exc:  # 权威源缺失 → 显式失败（经调用方 advisory 包装上报）
        raise RuntimeError(
            f"字数带系数权威源不可用（nww_wordcount.band_pct 导入失败）：{_exc}") from _exc
    return _band_pct()


def _wc_expand_on_band() -> bool:
    """M2（2026-11-05）：under 触发扩写采用带口径（status=under 即低于 ±10% 带下沿）。
    env NWW_WC_EXPAND_BAND=0 回滚旧硬线触发面（under 且 <0.95×目标下限）。"""
    return os.getenv("NWW_WC_EXPAND_BAND", "1") != "0"


def _apply_wc_note_override(wc: dict, note_range: tuple[int, int] | None) -> dict:
    """note 显式给出扩写目标字数时，覆盖 wordcount 判定目标（细纲 target 不再权威）。

    - actual 明显低于区间下界 → under（扩写不足，advisory 提示，不动作）；
    - actual 明显高于区间上界 → over → compress-once 回收到上界；
    - 落在区间 → ok → commit。
    保留 override=target/status/actions 供下游 compress-once 与前端展示。
    """
    if note_range is None or not wc.get("actual"):
        return wc
    lower, upper = note_range
    actual = int(wc["actual"])
    wc["note_override"] = True
    wc["target"] = f"{lower}-{upper}" if upper != lower else str(upper)
    _pct = _wc_band_pct()  # M2：与脚本带同源（env NWW_WC_BAND_PCT），禁另设默认值
    if actual < int(lower * (1 - _pct)):
        wc["status"] = "under"
        wc["actions"] = ["accept-current-length"]
    elif actual > int(upper * (1 + _pct)):
        wc["status"] = "over"
        wc["actions"] = ["compress-once"]
    else:
        wc["status"] = "ok"
        wc["actions"] = ["commit"]
    wc["compression"] = None
    return wc


def _extract_wc_target(outline_text: str) -> str | None:
    """从细纲提取字数目标（如 3500 或 2000-2500），供 wordcount check 回退使用。"""
    m = _WC_TARGET_RE.search(outline_text)
    if not m:
        return None
    return re.sub(r"\s+", "", m.group(1))


def step_wordcount_check(book: Path, chapter_id: int, body_path: Path | None = None) -> dict:
    """字数检查：优先 nww_ctl.py chapter check；失败时回退 wordcount check（细纲提取 target）。

    body_path: dry_run 预览正文路径（sandbox body-preview），缺省走 resolve_chapter_file。
    返回 {ok, status, actual, target, actions, compression, raw}。
    """
    book = book.resolve()
    if body_path is not None:
        # dry_run：直接用预览文件做字数检查
        outline = _find_outline_file(book, chapter_id)
        target = None
        if outline:
            try:
                target = _extract_wc_target(outline.read_text(encoding="utf-8"))
            except OSError:
                pass
        if not target:
            return {"ok": False, "detail": f"dry_run：无法从细纲提取字数目标 chapter={chapter_id}"}
        res = toolbox.run_script(
            "nww_ctl.py",
            ["wordcount", "check", "--file", str(body_path), "--target", str(target)],
            book, timeout=60,
        )
        if not res["ok"]:
            return {"ok": False, "detail": f"dry_run wordcount check 失败: {(res['stderr'] or res['stdout'])[:200]}"}
        try:
            data = json.loads(res["stdout"])
            return {"ok": True, "status": data.get("status", "?"), "actual": data.get("actual"),
                    "target": target, "actions": data.get("available_actions", []), "raw": data}
        except json.JSONDecodeError:
            return {"ok": True, "status": "ok", "actual": None, "target": target, "actions": [], "raw": {}}
    res = toolbox.run_script(
        "nww_ctl.py",
        ["chapter", "check", "--project", str(book), "--chapter", str(chapter_id)],
        book, timeout=120,
    )
    # P0-2（2026-10-30 外脑定稿）：nww_ctl chapter check 在 quality fail 时也 exit 2
    # （nww_ctl.py:299），退出码语义是「质量未过」而非「工具执行失败」，stdout 携带完整检查 JSON。
    # 故先按结构契约解析 stdout 采信主路径结果。采信范围仅限已知退出码 {0, 2}
    # （2 = quality fail 已确证语义；其余非零/负值 = 进程异常或被杀，stdout 视为不可信残留，
    # 落回退并保留真实原因——防 EXEC_ERROR 残留被误采信）。
    # JSON 残缺 / 缺 length / 状态非法 → 同样落回退。
    _cc_data = None
    if res.get("returncode") in (0, 2):
        try:
            _cc_data = json.loads(res["stdout"])
        except (json.JSONDecodeError, TypeError, ValueError):
            _cc_data = None
    _cc_length = _cc_data.get("length") if isinstance(_cc_data, dict) else None
    _cc_status = _cc_length.get("status") if isinstance(_cc_length, dict) else None
    if isinstance(_cc_data, dict) and _cc_status in {"internal_pass", "borderline", "under", "over"}:
        return {
            "ok": True,
            "status": _cc_status,
            "actual": _cc_length.get("actual"),
            "target": _cc_length.get("target"),
            "actions": _cc_data.get("available_actions", []),
            "compression": _cc_length.get("compression") or _cc_data.get("compression"),
            "raw": _cc_data,
        }

    # 回退：wordcount check（需从细纲提取 target）
    outline = _find_outline_file(book, chapter_id)
    target = None
    if outline:
        try:
            target = _extract_wc_target(outline.read_text(encoding="utf-8"))
        except OSError:
            pass
    if not target:
        # 兜底：细纲字数目标行丢失（prealign 回补剥离事故）→ 从 contract sidecar 取
        _sc = _contract_sidecar_path(book, chapter_id)
        if _sc is not None:
            try:
                _t = ((json.loads(_sc.read_text(encoding="utf-8")).get("content_load") or {}).get("wc_target") or 0)
                if int(_t) > 0:
                    target = str(int(_t))
                    print(f"[WC_FALLBACK] 细纲字数目标行缺失，从 sidecar 兜底 target={target}", flush=True)
            except Exception:  # noqa: BLE001
                pass
    if not target:
        return {"ok": False, "detail": f"chapter check 失败且无法提取字数目标: {(res['stderr'] or res['stdout'])[:200]}"}
    # 回退路径找正文文件：P0-3（2026-10-30 外脑定稿）复用 skill 权威解析器
    # nww_wordcount.find_chapter_file（正则 第0*(\d+)章 前导零归一 + 排除审稿快照/备份目录 +
    # 多候选 require 硬闸），替代旧 fnmatch 第{chapter_id:03d}章* 模式——
    # 该模式匹配不上无前导零命名（第1章_灰白纪年.md，2026-10-30 第1章"找不到正文文件"事故根因）。
    # 错误分型报告：正文目录缺失 / 多候选歧义 / 解析器异常；ImportError 时降级旧 glob 保底。
    body_path = None
    try:
        import sys as _sys  # noqa: PLC0415 — 惰性加载 skill 解析器（与 detect_internal 同款模式）
        _scripts_dir = str(toolbox._SCRIPTS)
        if _scripts_dir not in _sys.path:
            _sys.path.insert(0, _scripts_dir)
        from nww_wordcount import WordcountError as _WordcountError, find_chapter_file as _find_chapter_file
        body_path = _find_chapter_file(book / "正文", chapter_id, outline=False)
    except ImportError:
        body_path = next((p for p in (book / "正文").rglob(f"第{chapter_id:03d}章*")
                          if not _is_non_prose(p)), None)
        if body_path is None:
            return {"ok": False, "detail": f"回退模式：找不到正文文件 第{chapter_id:03d}章_*.md（解析器缺失，降级 glob 亦未命中）"}
    except _WordcountError as _exc:  # noqa: F821 — 依赖上方 import 成功路径
        _msg = str(_exc)
        if "directory is missing" in _msg:
            return {"ok": False, "detail": f"回退模式：正文目录缺失 {book / '正文'}"}
        if "exactly one" in _msg:
            return {"ok": False, "detail": f"回退模式：第{chapter_id}章存在多个正文候选，已拦截（防错算）——{_msg}"}
        return {"ok": False, "detail": f"回退模式：章节文件解析失败——{_msg}"}
    if body_path is None:
        return {"ok": False, "detail": f"回退模式：找不到正文文件 第{chapter_id:03d}章_*.md"}
    res2 = toolbox.run_script(
        "nww_ctl.py",
        ["wordcount", "check", "--file", str(body_path), "--target", target],
        book, timeout=60,
    )
    if not res2["ok"]:
        return {"ok": False, "detail": (res2["stderr"] or res2["stdout"])[:300]}
    try:
        data = json.loads(res2["stdout"])
    except json.JSONDecodeError as exc:
        return {"ok": False, "detail": f"wordcount check 输出非 JSON: {exc}"}
    status = data.get("status", "invalid")
    # 回退模式下无 available_actions/compression，按 status 推导
    actions = ["commit"] if status in ("internal_pass", "borderline") else (
        ["compress-once", "accept-current-length"] if status == "over" else ["accept-current-length"]
    )
    return {
        "ok": True,
        "status": status,
        "actual": data.get("actual"),
        "target": data.get("target"),
        "actions": actions,
        "compression": None,
        "raw": data,
    }


def _wc_target_band(target) -> tuple[int, int]:
    """把字数目标（"2500-2750" / "2500" / 2500）解析成 (min, max)；无法解析 → (0, 0)。"""
    s = str(target or "").strip()
    m = re.match(r"^([1-9]\d*)\s*[-~～至到]\s*([1-9]\d*)$", s)
    if m:
        return int(m.group(1)), int(m.group(2))
    if s.isdigit() and int(s) > 0:
        return int(s), int(s)
    return 0, 0


def _wc_plan_ops_prompt(plan_ops: "list[dict] | None", direction: str) -> str:
    """把篇幅平衡规划器锁定的 operations 注入既有融合版 prompt 的定点预算表段。

    返回空串（plan_ops=None）时调用方 prompt 逐字不变；非空时以换行结尾，
    直接插在「定点预算表」示例行之后，不新增独立 Writer/执行器。
    NWW_WC_BALANCE_PROMPT=0 的旧 prompt 分支不注入（回到旧 prompt，plan_ops 被忽略）。
    """
    if not plan_ops:
        return ""
    lines = ["# 规划器已锁定的定点操作（必须逐条执行，不得自行增删/改位；未被指派段落逐字保留）"]
    for i, op in enumerate(plan_ops, 1):
        if not isinstance(op, dict):
            continue
        delta = op.get("expected_delta")
        try:
            delta_txt = f"{int(delta):+d}"
        except (TypeError, ValueError):
            delta_txt = "±?"
        lines.append(
            f"{i}. {op.get('block_id') or '?'}（beat {op.get('beat_id') or '?'}）"
            f"｜{op.get('action') or direction}｜预计 {delta_txt} 字"
            f"｜定位：{op.get('where') or ''}｜手法：{op.get('how') or ''}"
            f"｜约束：{op.get('constraints') or ''}")
    return "\n".join(lines) + "\n"


def step_compress_once(book: Path, chapter_id: int, packet: dict, prose_path: Path, wc: dict,
                       *, plan_ops: "list[dict] | None" = None) -> tuple[str, str]:
    """compress-once：让 writer 基于现有正文压缩一次（删除冗余，保留核心情节）。

    仅在 length.status == over 时调用。复用 writer 方法论 + normalize 链，
    不新增压缩规则。压缩后正文覆盖落盘，返回 (新正文, 报告)。

    塌缩闸（2026-10-25 第1章 2890→916 事故复盘）：compress LLM 实测会无视
    「目标删减 N 字」把整章压成残骸（68% 内容丢失），旧版无校验直落盘 →
    916 字 husk 进审稿必然低分回滚。修复：压缩后可见字数低于细纲目标下限
    的硬线（target_min × NWW_WC_HARDLINE，同重写链 under 硬线口径）→
    拒绝落盘、恢复压前正文；超长按外脑 Q6 观测容忍（accept-current-length
    本就是 over 的合法 action，轻微超长远好于毁章）。目标不可解析时退化为
    「新稿 < 压前 50%」判定（砍半必然事故）。
    """
    current = prose_path.read_text(encoding="utf-8")
    _cur_vis = _visible_chars(current)[0]
    comp = wc.get("compression") or {}
    remove_range = comp.get("remove_to_user_band") or comp.get("remove_to_internal_band") or {}
    remove_min = remove_range.get("min", 0)
    remove_max = remove_range.get("max", remove_min)
    # 回退模式（compression=None）：从 actual - target_max 推算
    if remove_min == 0 and remove_max == 0:
        _tmin, _tmax = _wc_target_band(wc.get("target"))
        actual = wc.get("actual") or 0
        if actual and _tmax and actual > _tmax:
            remove_min = max(1, actual - _tmax)
            remove_max = remove_min + 200
    _tmin, _tmax = _wc_target_band(wc.get("target"))
    _floor_vis = int(_tmin * _wc_hardline_ratio()) if _tmin else int(_cur_vis * 0.5)
    agent = registry.get_agent("narrative-writer")
    # 2026-11-05（用户终审裁决·嵌入式篇幅平衡）：把「章节篇幅平衡 Prompt」（缩写三问
    # 自检 + 字数范围约束）注入修文入口 prompt。架构约束：不新建执行器/质量闸/提交闸，
    # Writer 仍走本函数既有 narrative-writer + _run_agent_bounded 调用与塌缩闸。
    # env NWW_WC_BALANCE_PROMPT=0 回滚旧 prompt（只增不改）。
    _band_txt = (f"{_tmin}-{_tmax}" if _tmax and _tmax != _tmin
                 else (str(_tmin) if _tmin else "细纲目标区间"))
    if os.getenv("NWW_WC_BALANCE_PROMPT", "1") != "0":
        # 2026-11-05 终极融合版（用户提供）：文风锚定 + 定点预算表 + 去水工具箱 B1-B4。
        # 字数/差额由程序算好传入（免 LLM 数数幻觉）；输出以 ===正文开始=== 分隔，
        # 调用侧切分提取正文。env NWW_WC_BALANCE_PROMPT=0 回滚原始旧 prompt。
        _delta_txt = (f"需缩写约 {remove_min}-{remove_max} 字" if remove_max
                      else "需缩写至合格区间内")
        # 2026-11-06（用户裁决）：规划器 operations 注入既有定点预算表段；
        # plan_ops=None → 空串 → prompt 逐字不变（旧路径零影响）。
        _plan_ops_txt = _wc_plan_ops_prompt(plan_ops, "condense")
        prompt = f'''你是顶尖中文网文精修编辑。请在不破坏原有剧情骨架、伏笔和人设的前提下，将第{chapter_id}章字数精准调整至目标区间。
# 输入信息
- 当前实际字数：{_cur_vis} 字（程序精确统计，不要自己数）
- 目标区间：{_band_txt} 字
- 需调整差额：{_delta_txt}
- 原文：
"""
{current}
"""
# 第一步：战前锚定与预算（极简输出，禁写长篇分析）
1. **文风锚定**：从原文摘出 2～3 句最能代表作者语感（句式长短、用词狠度、叙事节奏）的原句，后续改写必须严格对齐该语感，严禁出现 AI 书面腔。
2. **禁区锁定**：用一句话列出本章绝对不可删改的核心伏笔、关键转折与章末钩子。
3. **定点预算表**：严禁按自然段拆分，严禁全文平均用力！仅挑选 2～4 个【具体场景/交锋单元】分配字数任务，格式如下：
- [单元位置]｜[缩写]｜[预计增减 X 字]｜[采用的具体手法]
{_plan_ops_txt}# 第二步：定点动刀法则（缩写——抽干水分，严禁将名场面缩成梗概）
- **B1 砍解释性复述**：人物动作和台词已体现意图的，删掉后面"他之所以这么做是因为……""也就是说……"等解释性旁白。
- **B2 压过渡废戏**：赶路、换场、无关紧要的寒暄，3 句话以上的一律压缩为 1 句话带过。
- **B3 削冗余修饰与标签**：删掉密集的副词、叠词、形容词；将"他无比愤怒地大声吼道"精简为动作或短标签。
- **B4 合并同义心理**：连续两句意思相近的犹豫、震惊或感叹，直接二合一。
# 红线铁律
1. 严禁新增细纲外的人物、支线设定或改变事件因果链；不修改章节标题与核心事件结果。
2. 严禁使用加粗、删除线等任何标记符号污染正文。
3. 压缩后可见字数不得低于 {_floor_vis} 字（当前 {_cur_vis} 字，压过头=事故，塌缩闸会恢复原文）；不得把关键情节、名场面压成梗概。
4. 必须足额完成【需调整差额】的字数预算，不得半途而废。
# 输出格式要求
先输出【第一步：战前锚定与预算】，随后必须单独起一行输出分隔符 ===正文开始===，紧接着输出修改后的完整干净正文（正文结束后不要输出任何自检表或总结）。'''
    else:
        prompt = (
            f"你是资深连载编辑，做类人工的收束编辑（不是缩写）：压缩以下第{chapter_id}章正文，"
            f"删冗余描写、重复信息与注水段落，保留全部核心情节、对话、动作与伏笔。"
            f"目标删减 {remove_min or '少量'}-{remove_max or '适量'} 字。\n"
            f"约束：① 不改章节标题与核心事件 ② 保留所有对话与关键动作 ③ 只删不增 "
            f"④ 压缩后可见字数不得低于 {_floor_vis} 字（当前 {_cur_vis} 字，压过头=事故）"
            f"⑤ 删减优先级：重复信息 > 注水描写 > 冗余过渡 > 次要环境细节，禁止删情节节点 "
            f"⑥ 输出完整正文\n\n"
            f"{current}"
        )
    out = _run_agent_bounded(agent, "compress-once 正文压缩", prompt,
                             timeout=300.0, temperature=0.3, max_tokens=8192)
    # 融合版输出协议：切分取正文（缺分隔符时保底用全文，normalize + 塌缩闸兜底）
    if "===正文开始===" in out:
        _budget, _, _body = out.partition("===正文开始===")
        print(f"[WC_BALANCE] compress 定点预算：{_budget.strip()[:160]}", flush=True)
        out = _body.strip()
    clean, rep = step_normalize(book, chapter_id, out)
    _new_vis = _visible_chars(clean)[0]
    if _new_vis < _floor_vis:
        prose_path.write_text(current, encoding="utf-8")  # 恢复压前正文
        print(f"[COMPRESS] BLOCK 塌缩: {_cur_vis}→{_new_vis}字 < 硬线 {_floor_vis}"
              f"（目标下限 {_tmin or '?'}×{_wc_hardline_ratio():.0%}），恢复压前正文，"
              f"超长按观测容忍继续", flush=True)
        return current, (f"compress-once 塌缩闸拦截（{_cur_vis}→{_new_vis} < 硬线 {_floor_vis}），"
                         f"已恢复压前正文（{rep}）")
    prose_path.write_text(clean + "\n", encoding="utf-8")
    return clean, f"compress-once（{rep}）"


def _expand_redundancy_metrics(current: str, candidate: str) -> dict:
    """扩写候选的确定性复述指标（2026-11-13）。

    第1章三轮扩写把「黑布搏动→压住→平息」「选择静默」「轨迹偏折」各重复 2-3 次：
    字数达标但内容注水。语义重复须由审稿 I-B 判定；这里只做确定性可判的部分——
      - dup_sentences：候选内部完全相同的句子（去空白后 ≥12 字、出现 ≥2 次）；
      - self_repeat_4gram_ratio：候选自身 4-gram 重复比例（观测值）；
      - new_sentence_ratio：候选句不在当前稿中的比例（观测值，保留型扩写天然较高）。
    """
    def _sents(text: str) -> list[str]:
        return [s.strip() for s in re.split(r"[。！？\n]+", str(text or "")) if len(s.strip()) >= 12]

    _sent = _sents(candidate)
    _cur = set(_sents(current))
    _count: dict[str, int] = {}
    for s in _sent:
        _count[s] = _count.get(s, 0) + 1
    _dup = [s for s, n in _count.items() if n >= 2]
    _flat = re.sub(r"\s+", "", str(candidate or ""))
    _grams: dict[str, int] = {}
    for i in range(max(0, len(_flat) - 3)):
        k = _flat[i:i + 4]
        _grams[k] = _grams.get(k, 0) + 1
    _repeat = sum(n for n in _grams.values() if n >= 2) / max(1, len(_grams))
    return {
        "dup_sentences": len(_dup),
        "dup_sentence_samples": _dup[:3],
        "self_repeat_4gram_ratio": round(_repeat, 4),
        "new_sentence_ratio": round(sum(1 for s in _sent if s not in _cur) / max(1, len(_sent)), 4),
    }


def step_expand_once(book: Path, chapter_id: int, packet: dict, prose_path: Path,
                     wc: dict, need_more: int, *,
                     plan_ops: "list[dict] | None" = None) -> tuple[str, str]:
    """expand-once：让 writer 基于现有正文扩写一次（补细节/对话/动作，不新造情节）。

    仅在 wordcount under 时自动调用。与 compress-once 对称：超字自动压、欠字自动补，
    不需要用户手动重跑。扩写后 normalize 落盘，返回 (新正文, 报告)。
    """
    current = prose_path.read_text(encoding="utf-8")
    _cur_vis = _visible_chars(current)[0]
    agent = registry.get_agent("narrative-writer")
    outline_path = _find_outline_file(book, chapter_id)
    outline_text = ""
    if outline_path and Path(outline_path).is_file():
        try:
            outline_text = Path(outline_path).read_text(encoding="utf-8")[:3000]
        except Exception:
            pass
    _lo, _hi = _wc_target_band((wc or {}).get("target"))
    _band_txt = (f"{_lo}-{_hi}" if _hi and _hi != _lo
                 else (str(_lo) if _lo else "细纲目标区间"))
    if os.getenv("NWW_WC_BALANCE_PROMPT", "1") != "0":
        # 2026-11-05 终极融合版（用户提供）：文风锚定 + 定点预算表 + 交锋链扩写 A1-A4。
        # 字数/差额由程序算好传入；输出以 ===正文开始=== 分隔，调用侧切分提取正文。
        # 2026-11-06（用户裁决）：规划器 operations 注入既有定点预算表段；
        # plan_ops=None → 空串 → prompt 逐字不变（旧路径零影响）。
        _plan_ops_txt = _wc_plan_ops_prompt(plan_ops, "expand")
        prompt = f'''你是顶尖中文网文精修编辑。请在不破坏原有剧情骨架、伏笔和人设的前提下，将第{chapter_id}章字数精准调整至目标区间。
# 输入信息
- 当前实际字数：{_cur_vis} 字（程序精确统计，不要自己数）
- 目标区间：{_band_txt} 字
- 需调整差额：需扩写约 {need_more} 字
- 细纲参考（判断「细纲外」的依据，不要偏离）：
{outline_text}
- 原文：
"""
{current}
"""
# 第一步：战前锚定与预算（极简输出，禁写长篇分析）
1. **文风锚定**：从原文摘出 2～3 句最能代表作者语感（句式长短、用词狠度、叙事节奏）的原句，后续改写必须严格对齐该语感，严禁出现 AI 书面腔。
2. **禁区锁定**：用一句话列出本章绝对不可删改的核心伏笔、关键转折与章末钩子。
3. **定点预算表**：严禁按自然段拆分，严禁全文平均用力！仅挑选 2～4 个【具体场景/交锋单元】分配字数任务，格式如下：
- [单元位置]｜[扩写]｜[预计增减 X 字]｜[采用的具体手法]
{_plan_ops_txt}# 第二步：定点动刀法则（扩写——拉长博弈链，严禁堆砌辞藻）
- **A1 展开交锋回合（主力扩写法）**：将一笔带过的对话或冲突，按"抛出意图/试探 → 对方微表情与回避 → 潜台词拉扯/加码 → 局势升级或让步"拆开，增加 1～2 个推拉小回合。
- **A2 展开动作阻碍**：将概括性动作（如"他费力打开了箱子"）展开为"动作尝试 → 遭遇具体阻碍/反常细节 → 应对动作 → 结果"。
- **A3 触发式生理/念头反应**：在冲击性信息后，补充人物瞬间的手部小动作、生理本能反应与直觉算计（控制在 2 句内，禁写大段抒情独白）。
- **A4 转场空间锚点**：在场景切换过快处，补 1～2 句环境光影、声音或肢体状态，建立画面过渡。
# 红线铁律
1. 严禁新增细纲外的人物、支线设定或改变事件因果链；不凭空增加剧情、人物或设定凑字数。
2. 严禁使用加粗、删除线等任何标记符号污染正文。
3. 不删减任何已有内容（扩写只增不减），不改核心事件与结局。
4. 必须足额完成【需调整差额】的字数预算，不得半途而废。
5. 禁止复述：正文里已写过的动作、观察、判断、决定，不得换措辞再写一遍；每一处新增必须带来新信息（新动作、新物件、新反应、新推理）。同一信息出现第二次即视为注水。
# 输出格式要求
先输出【第一步：战前锚定与预算】，随后必须单独起一行输出分隔符 ===正文开始===，紧接着输出修改后的完整干净正文（正文结束后不要输出任何自检表或总结）。'''
    else:
        prompt = (
            f"你是资深连载编辑。以下是第{chapter_id}章正文，当前 {_cur_vis} 字，需要再补 "
            f"约 {need_more} 字到达目标。\n"
            f"扩写要求：① 不新造情节，只在现有场景内补细节（动作细化、感官描写、对话间的"
            f"情绪/动作过渡、环境氛围）② 不删任何已有内容 ③ 不改核心事件与结局 "
            f"④ 网文节奏，不注水——禁止把已写过的动作/观察/判断换措辞复述，"
            f"每一处新增必须带来新信息 ⑤ 输出完整正文。\n"
            f"细纲参考（知道本章该写什么，不要偏离）：\n{outline_text}\n\n"
            f"当前正文：\n{current}"
        )
    out = _run_agent_bounded(agent, "expand-once 自动扩写", prompt,
                             timeout=300.0, temperature=0.4, max_tokens=8192)
    # 融合版输出协议：切分取正文（缺分隔符时保底用全文，normalize + 塌缩闸兜底）
    if "===正文开始===" in out:
        _budget, _, _body = out.partition("===正文开始===")
        print(f"[WC_BALANCE] expand 定点预算：{_budget.strip()[:160]}", flush=True)
        out = _body.strip()
    clean, rep = step_normalize(book, chapter_id, out)
    # 塌缩复检（2026-11-01，对齐 step_compress_once 对称设计 + deslop 链落盘复检口径）：
    # 原「扩写后不复检」= 与 compress-once 塌缩事故同型的另一面——expand 的 LLM 偶发
    # 输出空/残稿或 normalize 砍漏时，短稿直接覆盖落盘且无任何防线。以**盘上正文**为准，
    # 低于 max(压前×0.8, 硬线下限) 拒绝覆盖并恢复；目标不可解析时退化为「< 压前 50%」。
    _hard = _wc_target_band((wc or {}).get("target"))[0]
    _floor = int(_cur_vis * 0.8) if not _hard else max(int(_cur_vis * 0.8), int(_hard * _wc_hardline_ratio()))
    _new_vis = _visible_chars(clean)[0]
    # 扩写注水闸（2026-11-13）：候选内部整句重复 = 确定性复述，直接拒绝落盘。
    # 语义级重复（换措辞复述同一信息）由扩写 prompt 的「禁止复述」与审稿 I-B 判定，这里不猜。
    _red = _expand_redundancy_metrics(current, clean)
    if os.getenv("NWW_WC_TRACE", "1") != "0":
        print(f"[EXPAND] 冗余指标: {_red}", flush=True)
    if os.getenv("NWW_EXPAND_DUP_GATE", "1") != "0" and _red["dup_sentences"] > 0:
        prose_path.write_text(current, encoding="utf-8")
        print(f"[EXPAND] BLOCK 注水: 候选含 {_red['dup_sentences']} 条重复句"
              f"（{_red['dup_sentence_samples']}），保留原稿", flush=True)
        return current, (f"expand-once 注水闸拦截（重复句 {_red['dup_sentences']} 条），"
                         f"已保留原稿（{rep}）")
    # 塌缩与未达标分治（2026-11-13，第1章实测）：此前「候选 < 目标硬线 2375」整稿丢弃，
    # 三轮候选 2105/1765/1925 全被 BLOCK → 字数停在 1240 并提交。候选若较当前稿有实质
    # 增量必须保留（只增不减），下一轮基于新稿继续扩；只有反向塌缩/零变化才恢复原稿。
    if _new_vis <= _cur_vis or _new_vis < int(_cur_vis * 0.8):
        prose_path.write_text(current, encoding="utf-8")  # 恢复扩写前正文
        print(f"[EXPAND] BLOCK 塌缩: {_cur_vis}→{_new_vis}字（≤扩写前或 <80%），恢复扩写前正文",
              flush=True)
        if os.getenv("NWW_WC_TRACE", "1") != "0":
            print(f"[WC_TRACE] expand-once 结果=BLOCK 前={_cur_vis} 后={_new_vis} 硬线={_floor} "
                  f"目标={_band_txt} 需补={need_more} 落盘=恢复原文", flush=True)
        return current, (f"expand-once 塌缩闸拦截（{_cur_vis}→{_new_vis} ≤扩写前或 <80%），"
                         f"已恢复扩写前正文（{rep}）")
    if _new_vis < _floor:
        prose_path.write_text(clean + "\n", encoding="utf-8")
        print(f"[EXPAND] 部分达成: {_cur_vis}→{_new_vis}字 < 目标硬线 {_floor}，保留增量继续下一轮",
              flush=True)
        if os.getenv("NWW_WC_TRACE", "1") != "0":
            print(f"[WC_TRACE] expand-once 结果=PARTIAL 前={_cur_vis} 后={_new_vis} 硬线={_floor} "
                  f"目标={_band_txt} 需补={need_more} 落盘=保留增量", flush=True)
        return clean, (f"expand-once 部分达成（{_cur_vis}→{_new_vis}，未达硬线 {_floor}，"
                       f"已保留增量）")
    prose_path.write_text(clean + "\n", encoding="utf-8")
    # P0（2026-11-05，只增不改）：成功落盘原先完全无日志，导致「纠偏跑了几轮、增量多少」
    # 在生产上不可证（回执只有 _acted 计数，回滚也计一轮）。env NWW_WC_TRACE=0 回滚。
    if os.getenv("NWW_WC_TRACE", "1") != "0":
        print(f"[WC_TRACE] expand-once 结果=OK 前={_cur_vis} 后={_new_vis} 硬线={_floor} "
              f"目标={_band_txt} 需补={need_more} 落盘=覆盖", flush=True)
    return clean, f"expand-once（{rep}，目标补 {need_more} 字）"


# ==================== 篇幅平衡 v1（方案化扩缩，2026-11-06 / T21） ====================
# 规格：.codeartsdoer/plans/length-balance-v1/spec.md
# 架构约束（继承 docs/NWW-V14_章节篇幅平衡规划器_执行规范.md §8 裁决）：
#   * 不新建独立 Writer / 质量闸 / 提交闸；Writer 仍复用 narrative-writer +
#     _run_agent_bounded，质量/事务复用既有闸门；
#   * 本段只新增「规划 → 确定性校验 → 质量优先择优 → 定向执行 → 质量优先验收
#     → 基于当前稿迭代」控制环，纯函数全部来自 nww_balance_plan（T20）。
# 回滚：NWW_WC_PLAN_MODE=0 时调用方完全走旧 2 轮盲扩/盲缩，本段代码不被调用。

_BALANCE_MOD_CACHE: dict = {"loaded": False, "mod": None, "error": ""}

# 候选方案的策略维度（spec R2 固定三档；N>3 时按同维度换定位，解析后按实质差异去重）。
_BALANCE_STRATEGIES: dict[str, list[tuple[str, str]]] = {
    "expand": [("gap_first", "缺口优先"), ("balanced", "均衡分配"), ("peak_boost", "峰值强化")],
    "condense": [("dedup_first", "纯冗余优先"), ("merge_notes", "合并说明"),
                 ("tighten_transitions", "收紧过场")],
}

_RE_BALANCE_BLANK = re.compile(r"\n\s*\n")

# L2 保护幕的关键词（章尾幕恒为 L2，另按 final_state/decision 文本确定性推导）。
_BALANCE_L2_KEYWORDS = ("伏笔", "钩子", "结局", "情绪峰值", "爽点", "反转")


def _wc_plan_mode() -> bool:
    """篇幅平衡 v1 总开关：NWW_WC_PLAN_MODE=0 → 回旧 2 轮盲扩/盲缩。"""
    return os.getenv("NWW_WC_PLAN_MODE", "1").strip() != "0"


def _wc_candidate_count() -> int:
    """每轮候选方案数（spec §4：默认 3，夹紧 2-5）。"""
    try:
        n = int(os.getenv("NWW_WC_CANDIDATES", "3").strip() or "3")
    except (TypeError, ValueError):
        n = 3
    return max(2, min(5, n))


def _wc_plan_rounds() -> int:
    """最多迭代轮次（spec §4：默认 3，夹紧 1-6）。"""
    try:
        n = int(os.getenv("NWW_WC_PLAN_ROUNDS", "3").strip() or "3")
    except (TypeError, ValueError):
        n = 3
    return max(1, min(6, n))


def _wc_stop_efficiency() -> float:
    """连续低效止损阈值（spec §4：默认 0.6，夹紧 0-1）。"""
    try:
        v = float(os.getenv("NWW_WC_STOP_EFFICIENCY", "0.6").strip() or "0.6")
    except (TypeError, ValueError):
        v = 0.6
    return max(0.0, min(1.0, v))


def _wc_quality_tie() -> float:
    """质量分差 ≤ 该值才比 residual_gap（spec §4：默认 5，夹紧 0-100）。"""
    try:
        v = float(os.getenv("NWW_WC_QUALITY_TIE", "5").strip() or "5")
    except (TypeError, ValueError):
        v = 5.0
    return max(0.0, min(100.0, v))


def _wc_quality_weight() -> float:
    """择优权重（spec §4：默认 0.7；仅影响同档比较的解释项）。"""
    try:
        v = float(os.getenv("NWW_WC_QUALITY_WEIGHT", "0.7").strip() or "0.7")
    except (TypeError, ValueError):
        v = 0.7
    return max(0.0, min(1.0, v))


def _balance_mod():
    """惰性加载纯函数模块 nww_balance_plan（T20，零 LLM/零 IO）。

    与 _wc_band_pct 同款 sys.path 注入方式；模块缺失/损坏 → None，调用方回旧逻辑
    （禁止在 pipeline 内复制一份实现）。
    """
    if _BALANCE_MOD_CACHE["loaded"]:
        return _BALANCE_MOD_CACHE["mod"]
    mod = None
    err = ""
    try:
        import sys as _sys  # noqa: PLC0415 — 惰性加载 skill 权威副本
        _scripts_dir = str(toolbox._SCRIPTS)
        if _scripts_dir not in _sys.path:
            _sys.path.insert(0, _scripts_dir)
        import nww_balance_plan as mod  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 — 模块缺失/语法错误均降级，不阻断主链
        err = str(exc)[:200]
        mod = None
    _BALANCE_MOD_CACHE.update({"loaded": True, "mod": mod, "error": err})
    if mod is None:
        print(f"[WC_PLAN] nww_balance_plan 不可用，回旧逻辑：{err}", flush=True)
    return mod


def _balance_ensure_nl(text: str) -> str:
    """正文落盘统一收尾：去掉多余尾部空行后补一个换行（与既有 expand/compress 同口径）。"""
    t = (text or "").rstrip("\n")
    return (t + "\n") if t else ""


def _balance_band(target, mod=None) -> tuple[int, int]:
    """基础字数区间：优先 nww_balance_plan.parse_band（范围不套 ±10%，单值套 band_pct）。"""
    if mod is not None:
        try:
            lo, hi = mod.parse_band(target, band_pct=_wc_band_pct())
            return int(lo), int(hi)
        except Exception:  # noqa: BLE001 — 解析失败退回旧口径（仅兜底）
            pass
    lo, hi = _wc_target_band(target)
    # 2026-11-06：回退路径也必须与 R1 同口径——范围目标直接用区间；
    # 单值目标套 band_pct（`_wc_target_band` 对单值返回 (n,n)，不能直接透传）。
    if lo > 0 and hi == lo:
        _p = _wc_band_pct()
        return int(lo * (1 - _p)), int(lo * (1 + _p))
    return lo, hi


def _balance_blocks(text: str) -> list[str]:
    """按空行切块（与 spec §3 的块级保护同一基底）；返回非空块列表。"""
    return [p.strip() for p in _RE_BALANCE_BLANK.split(text or "") if p.strip()]


def _balance_block_id(index: int) -> str:
    return f"block_{index + 1:03d}"


def _balance_preview(block: str, cap: int = 48) -> str:
    """块首预览（prompt 里的定位索引；空白归一后截断）。"""
    return re.sub(r"\s+", " ", block or "")[:cap]


def _balance_block_ids(text: str) -> set[str]:
    return {_balance_block_id(i) for i in range(len(_balance_blocks(text)))}


def _balance_beat_ids(packet) -> set[str]:
    ids: set[str] = set()
    for sc in ((packet or {}).get("scene_chain") or []):
        sid = str((sc or {}).get("scene_id") or "").strip() if isinstance(sc, dict) else ""
        if sid:
            ids.add(sid)
    return ids


def _balance_l2_beat_ids(packet) -> set[str]:
    """L2 保护 beat：章尾幕 + 文本含伏笔/钩子/情绪峰值/反转/结局的幕（确定性）。"""
    scenes = [s for s in ((packet or {}).get("scene_chain") or []) if isinstance(s, dict)]
    ids: set[str] = set()
    if scenes:
        _last = str(scenes[-1].get("scene_id") or "").strip()
        if _last:
            ids.add(_last)
    for sc in scenes:
        sid = str(sc.get("scene_id") or "").strip()
        if not sid:
            continue
        blob = (json.dumps(sc.get("final_state") or {}, ensure_ascii=False)
                + json.dumps(sc.get("decision") or {}, ensure_ascii=False))
        if any(k in blob for k in _BALANCE_L2_KEYWORDS):
            ids.add(sid)
    return ids


def _balance_outline_text(book: Path, chapter_id: int, *, cap: int = 3000) -> str:
    try:
        p = _find_outline_file(book, chapter_id)
        if p and Path(p).is_file():
            return Path(p).read_text(encoding="utf-8")[:cap]
    except Exception:  # noqa: BLE001 — 细纲读取失败只影响 prompt 参考，不阻断
        pass
    return ""


def _balance_voice_names(text: str) -> set[str]:
    try:
        return {it["name"] for it in _voice_parse_items(text or "") if it.get("name")}
    except Exception:  # noqa: BLE001 — 解析失败按空集（不额外收紧）
        return set()


def _balance_forbidden_brief(packet) -> str:
    """禁区文案：通用禁区 + packet 的 must_keep/must_not_invent/must_avoid 硬约束。"""
    base = "伏笔埋设/回收、章尾钩子、情绪峰值、核心设定揭示、章节结局"
    c = (packet or {}).get("canon_constraints") or {}
    items: list[str] = []
    for k in ("must_keep", "must_not_invent", "must_avoid"):
        v = c.get(k)
        if isinstance(v, list):
            items.extend(str(x)[:60] for x in v[:8] if str(x).strip())
        elif v:
            items.append(str(v)[:60])
    return base + ("；细纲硬约束：" + "；".join(items) if items else "")


def _balance_beats_brief(packet) -> str:
    lines: list[str] = []
    for sc in ((packet or {}).get("scene_chain") or []):
        if not isinstance(sc, dict):
            continue
        sid = str(sc.get("scene_id") or "").strip()
        if not sid:
            continue
        dg = str((sc.get("driver_goal") or {}).get("goal") or "")[:60]
        cp = str((sc.get("conflict") or {}).get("pressure") or "")[:60]
        dc = str((sc.get("decision") or {}).get("choice") or "")[:60]
        lines.append(f"- {sid}｜{str(sc.get('act') or '')[:40]}｜目标: {dg}｜冲突: {cp}｜决策: {dc}")
    return "\n".join(lines) or (
        '（本章 scene_chain 为空：beat_id 统一填 "unattributed"，按正文分块定位）')


def _balance_plan_key(plan: dict) -> tuple:
    """方案实质差异指纹（策略 + 各 operation 的块/动作/定位），用于去重与合并。"""
    ops = [o for o in (plan.get("operations") or []) if isinstance(o, dict)]
    sig = tuple(sorted(
        f"{o.get('block_id')}|{o.get('action')}|{str(o.get('where'))[:32]}" for o in ops))
    return (str(plan.get("strategy") or ""), sig)


def _balance_plan_quality(mod, plan: dict, actual: int, lo: int, hi: int) -> int | None:
    """方案质量分（确定性 score_plan）；异常返回 None。"""
    try:
        s = mod.score_plan(plan, actual=actual, lo=lo, hi=hi)
        if isinstance(s, dict):
            for k in ("quality", "quality_score", "total"):
                if s.get(k) is not None:
                    return int(round(float(s[k])))
    except Exception:  # noqa: BLE001 — 质量分缺失不应阻断（择优另有兜底）
        pass
    return None


def _balance_residual(actual: int, lo: int, hi: int) -> int:
    if actual < lo:
        return lo - actual
    if actual > hi:
        return actual - hi
    return 0


def _balance_block_coverage(block: str, cand: str) -> float:
    """原文块内容被候选块「保留」的比例（0-1）。

    不能用 SequenceMatcher.ratio()：扩写块天然比原块长，ratio 会因长度差被压到
    0.5 以下（实测原块 80 字 + 新增 240 字 → ratio≈0.4），误杀合法扩写。
    这里用 matching_blocks 覆盖「较短一侧」的比例：扩写侧分母=原块（原内容保留度），
    压缩侧分母=候选块（候选内容来源度）。
    """
    if not block or not cand:
        return 0.0
    if block == cand:
        return 1.0
    try:
        sm = difflib.SequenceMatcher(None, block, cand, autojunk=False)
        matched = sum(b.size for b in sm.get_matching_blocks())
        return matched / max(1, min(len(block), len(cand)))
    except Exception:  # noqa: BLE001
        return 0.0


def _balance_match_block(block: str, cand_blocks: list[str],
                         used: set[int]) -> tuple[int | None, float]:
    """在候选块中找与原文块最对应且未被占用的块；返回 (索引, 内容保留度)。"""
    best_i, best_r = None, 0.0
    for i, cb in enumerate(cand_blocks):
        if i in used:
            continue
        try:
            # quick_ratio 对长度差不敏感（字符集合口径），用于廉价预筛
            qr = difflib.SequenceMatcher(None, block, cb, autojunk=False).quick_ratio()
        except Exception:  # noqa: BLE001
            qr = 0.0
        if qr < 0.2:
            continue
        r = _balance_block_coverage(block, cb)
        if r > best_r:
            best_i, best_r = i, r
    return best_i, best_r


def _balance_op_block_violations(original: str, candidate: str, ops: list[dict]) -> list[str]:
    """逐 operation 复核块级保护（R5/§7.1）：扩写块 ≥ 原块、压缩块 ≤ 原块。"""
    orig_blocks = _balance_blocks(original)
    cand_blocks = _balance_blocks(candidate)
    if not orig_blocks or not cand_blocks:
        return []
    id_map = {_balance_block_id(i): i for i in range(len(orig_blocks))}
    used: set[int] = set()
    vio: list[str] = []
    for op in ops:
        if not isinstance(op, dict):
            continue
        bid = str(op.get("block_id") or "")
        oi = id_map.get(bid)
        if oi is None:
            vio.append(f"operation 指向不存在的块 {bid}")
            continue
        ob = orig_blocks[oi]
        ci, ratio = _balance_match_block(ob, cand_blocks, used)
        if ci is None or ratio < 0.5:
            vio.append(f"{bid} 在候选稿中丢失或被整体重写（相似度 {ratio:.2f}）")
            continue
        used.add(ci)
        ow = _visible_chars(ob)[0]
        cw = _visible_chars(cand_blocks[ci])[0]
        action = str(op.get("action") or "")
        if action == "expand" and cw < ow:
            vio.append(f"{bid} 扩写后字数 {cw} < 原块 {ow}")
        elif action == "condense" and cw > ow:
            vio.append(f"{bid} 压缩后字数 {cw} > 原块 {ow}")
    return vio


def _balance_quality_check(original: str, candidate: str, plan: dict, *,
                           mod, lo: int, hi: int) -> dict:
    """候选稿质量验收（确定性，spec R6-1）：先质量、后字数。

    ① diff_scope_ok（改动必须落在 operations 范围，防 Writer 自由重写）
    ② voice 锚点角色名集合不得减少（_voice_parse_items 原文/候选对比）
    ③ 方向保护：expand 总字数不得低于原文；condense 不得高于原文、不得跌破区间下限
    ④ operation 块级保护（expand 块 ≥ 原块 / condense 块 ≤ 原块）
    """
    vio: list[str] = []
    ops = [o for o in (plan.get("operations") or []) if isinstance(o, dict)]
    candidate = candidate or ""
    if not candidate.strip():
        return {"ok": False, "violations": ["候选稿为空"], "actual": 0}
    o_vis = _visible_chars(original)[0]
    c_vis = _visible_chars(candidate)[0]
    if o_vis and c_vis and candidate.strip() == (original or "").strip():
        vio.append("候选稿与原稿逐字一致（定向改动未发生）")
    try:
        vio.extend(mod.diff_scope_ok(original, candidate, ops))
    except Exception as exc:  # noqa: BLE001 — 校验器异常按违规处理（换备选）
        vio.append(f"改动范围校验异常: {exc}")
    orig_names = _balance_voice_names(original)
    cand_names = _balance_voice_names(candidate)
    if orig_names and not orig_names.issubset(cand_names):
        vio.append(f"voice 锚点角色集合减少: {sorted(orig_names - cand_names)}")
    direction = str(plan.get("direction") or "")
    if direction == "expand" and c_vis < o_vis:
        vio.append(f"扩写稿总字数低于原文（{o_vis}→{c_vis}）")
    if direction == "condense":
        if c_vis > o_vis:
            vio.append(f"压缩稿总字数高于原文（{o_vis}→{c_vis}）")
        if lo and c_vis < lo:
            vio.append(f"压缩过度：{c_vis} 字低于基础区间下限 {lo}（防塌缩）")
    vio.extend(_balance_op_block_violations(original, candidate, ops))
    return {"ok": not vio, "violations": vio[:12], "actual": c_vis}


def step_apply_balance_plan(book: Path, chapter_id: int, packet: dict,
                            prose_path: Path, plan: dict) -> tuple[str, str]:
    """篇幅平衡方案的执行适配层（薄封装，2026-11-06 用户裁决）。

    职责只有：整理 plan.operations → 调既有 step_expand_once / step_compress_once
    （带 plan_ops 关键字），返回其结果。**不**新建 Writer prompt、不复制
    输出分隔符解析 / normalize / 塌缩闸 / 落盘 / trace 逻辑——这些全部由
    既有扩缩写入口承担（单一执行入口，避免第二套平行执行器）。
    """
    ops = [o for o in (plan.get("operations") or []) if isinstance(o, dict)]
    if not ops:
        return prose_path.read_text(encoding="utf-8"), "方案无 operations，跳过定向执行"
    mod = _balance_mod()
    target = ((packet or {}).get("wordcount") or {}).get("target")
    if not target:
        target = _extract_wc_target(_balance_outline_text(book, chapter_id, cap=20000) or "")
    lo, hi = _balance_band(target, mod)
    current = prose_path.read_text(encoding="utf-8")
    cur_vis = _visible_chars(current)[0]
    # 差额优先取方案自身 Σexpected_delta（与注入 prompt 的 ops 预算一致），
    # 仅在方案增量缺失时退回基础区间缺口。
    plan_delta = sum(int(o.get("expected_delta") or 0) for o in ops
                     if isinstance(o.get("expected_delta"), int))
    if str(plan.get("direction")) == "condense":
        gap = max(0, cur_vis - hi) if hi else 0
        remove = max(1, abs(plan_delta) if plan_delta else gap)
        wc = {"ok": True, "status": "over", "actual": cur_vis, "target": target,
              "actions": ["compress-once"],
              "compression": {"remove_to_internal_band": {
                  "min": remove, "max": remove + 100}}}
        return step_compress_once(book, chapter_id, packet, prose_path, wc, plan_ops=ops)
    gap = max(0, lo - cur_vis) if lo else 0
    need = max(1, int(plan_delta) if plan_delta > 0 else int(gap))
    wc = {"ok": True, "status": "under", "actual": cur_vis, "target": target,
          "actions": []}
    return step_expand_once(book, chapter_id, packet, prose_path, wc, need, plan_ops=ops)


def _balance_plan_prompt(book: Path, chapter_id: int, packet: dict, current: str,
                         actual: int, lo: int, hi: int, n: int, direction: str,
                         *, feedback: str = "") -> str:
    """规划 LLM prompt：要求纯 JSON、N 套实质不同方案、策略维度固定。"""
    strategies = _BALANCE_STRATEGIES.get(direction) or _BALANCE_STRATEGIES["expand"]
    _strat_lines = "\n".join(f"- {sid}：{lab}" for sid, lab in strategies)
    gap = (lo - actual) if actual < lo else (actual - hi if (hi and actual > hi) else 0)
    gap = max(int(gap), 0)
    _block_index = "\n".join(
        f"- {_balance_block_id(i)}｜{_balance_preview(b, 46)}"
        for i, b in enumerate(_balance_blocks(current)))
    _sample_delta = gap if direction == "expand" else -gap
    _l2_txt = "、".join(sorted(_balance_l2_beat_ids(packet))) or "（无）"
    prompt = f'''你是中文网文「篇幅平衡规划师」。任务：为第{chapter_id}章生成 {n} 套**实质不同**的扩缩方案（只做规划，不写正文）。
# 硬信息（程序已精确统计，禁止自行计算/改写数字）
- 基础字数区间：{lo}-{hi} 字
- 当前实际字数：{actual} 字
- 需调整差额：{gap} 字（方向：{direction}）
# 策略维度（每套方案必须使用不同策略；方案之间必须在「改哪里/改什么」上有实质差异，禁止同义复述）
{_strat_lines}
# 本章 beat（beat_id 只能取下面列出的 scene_id；L2 保护 beat 禁止 condense）
{_balance_beats_brief(packet)}
- L2 保护 beat：{_l2_txt}
# 正文分块索引（block_id 只能取这里的编号，where 用它们定位）
{_block_index}
# 细纲参考
{_balance_outline_text(book, chapter_id, cap=2600)}
# 绝对禁区（不得列入任何 operation，也不得在 how 中要求改动）
{_balance_forbidden_brief(packet)}
# 输出要求（只输出 JSON，禁止任何解释文字/代码块围栏）
{{"schema":"nww_balance_plan/v1","plans":[
 {{"schema":"nww_balance_plan/v1","strategy":"gap_first 或 balanced 或 peak_boost 或 dedup_first 或 merge_notes 或 tighten_transitions","label":"缺口优先","direction":"{direction}",
   "operations":[{{"beat_id":"S1","block_id":"block_001","action":"{direction}","where":"具体场景/段落定位","how":"具体手法（拆对话回合/补动作阻碍/删重复交代/合并同类说明）","expected_delta":{_sample_delta},"protection_level":"L1","constraints":"禁止新增事件/角色/设定"}}],
   "residual_gap":0}}
]}}
# 规则（违反任一条会被确定性校验打回）
1. plans 数组必须恰好 {n} 套方案；每套 1-4 个 operation；expected_delta 符号必须与 action 一致（expand 为正、condense 为负）。
2. 同一 block_id 只能出现在一套方案的一个 operation 中（同块多次调整请合并为一个 operation）。
3. L2 保护 beat 禁止出现在 condense operation 中。
4. 不得为凑字数编造无内容价值的 operation；合法空间不足时宁可让 residual_gap > 0（不强制 Σexpected_delta 等于差额）。
5. residual_gap = max(下限 - (当前字数 + Σexpected_delta), 0, (当前字数 + Σexpected_delta) - 上限)。'''
    if feedback:
        prompt += ("\n# 上一轮方案被确定性校验打回（必须逐条修正后重新输出完整 JSON）\n"
                   + feedback[:1500] + "\n")
    return prompt


def _balance_parse_plans(raw: str) -> list[dict]:
    """解析规划 LLM 输出为方案列表；容错 ```json 围栏与前后缀解释文字。"""
    if not (raw or "").strip():
        return []
    txt = raw.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", txt, re.S)
    if m:
        txt = m.group(1).strip()
    data = None
    try:
        data = json.loads(txt)
    except (json.JSONDecodeError, TypeError, ValueError):
        s, e = txt.find("{"), txt.rfind("}")
        if 0 <= s < e:
            try:
                data = json.loads(txt[s:e + 1])
            except (json.JSONDecodeError, TypeError, ValueError):
                data = None
    if isinstance(data, dict):
        plans = data.get("plans") or data.get("candidates") or []
        if not plans and "operations" in data:
            plans = [data]
    elif isinstance(data, list):
        plans = data
    else:
        plans = []
    out: list[dict] = []
    for p in plans:
        if not isinstance(p, dict):
            continue
        ops = p.get("operations")
        if not isinstance(ops, list) or not ops:
            continue
        p.setdefault("schema", "nww_balance_plan/v1")
        out.append(p)
    return out


def _balance_dedupe_plans(plans: list[dict]) -> list[dict]:
    seen: set = set()
    out: list[dict] = []
    for p in plans:
        key = _balance_plan_key(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _balance_normalize_plans(plans: list[dict], mod, direction: str,
                             actual: int, lo: int, hi: int) -> list[dict]:
    """机械字段规范化：schema/direction/residual_gap 属派生字段，由本地补齐。

    residual_gap 用 nww_balance_plan.compute_residual_gap 同源计算（禁止复写公式），
    避免 LLM 算术误差把一份实质合法的方案无谓打回；方案内容（where/how/delta）不改。
    """
    for p in plans:
        p.setdefault("schema", "nww_balance_plan/v1")
        if p.get("direction") not in ("expand", "condense"):
            p["direction"] = direction
        ops = [o for o in (p.get("operations") or []) if isinstance(o, dict)]
        # residual_gap 是纯机械派生字段：统一用权威公式重算（禁止 LLM 算术误差把
        # 一份实质合法的方案无谓打回；方案内容 where/how/delta 一律不改）。
        try:
            p["residual_gap"] = int(mod.compute_residual_gap(actual, ops, lo, hi))
        except Exception:  # noqa: BLE001 — 计算失败交由 validate_plan 报缺失
            pass
    return plans


def _balance_plan_candidates(book: Path, chapter_id: int, packet: dict, current: str,
                             actual: int, lo: int, hi: int, n: int, direction: str,
                             *, feedback: str = "") -> list[dict]:
    """调用一次规划 LLM 生成候选方案；解析失败/实质重复 → 重试 1 次。"""
    agent = registry.get_agent("narrative-writer")
    mod = _balance_mod()
    prompt = _balance_plan_prompt(book, chapter_id, packet, current, actual, lo, hi,
                                  n, direction, feedback=feedback)
    plans: list[dict] = []
    for attempt in range(2):
        err: list[str] = []
        raw = _run_agent_bounded(agent, "篇幅平衡方案规划", prompt,
                                 timeout=_ADV_TIMEOUT_S, temperature=0.3,
                                 max_tokens=4096, err_out=err)
        plans = _balance_parse_plans(raw)
        if mod is not None:
            plans = _balance_normalize_plans(plans, mod, direction, actual, lo, hi)
        plans = _balance_dedupe_plans(plans)
        if len(plans) >= min(2, n):
            break
        _why = "；".join(err) if err else "输出无法解析或方案实质重复"
        print(f"[WC_PLAN] 规划输出不可用（attempt={attempt + 1}）：{_why}", flush=True)
        prompt = prompt + (
            "\n# 上一次输出不可用\n"
            f"原因：{_why}。必须严格只输出一个 JSON 对象，含 plans 数组且恰好 "
            f"{n} 套方案，方案之间 block_id/手法必须实质不同。\n")
    return plans


def _balance_validate_plans(mod, plans: list[dict], actual: int, lo: int, hi: int,
                            beat_ids: set[str], block_ids: set[str],
                            l2_ids: set[str]) -> tuple[list[dict], dict]:
    """确定性方案校验（spec R3）；返回 (合法方案, {方案标识: 违规清单})。"""
    valid: list[dict] = []
    viols: dict = {}
    for i, p in enumerate(plans):
        try:
            v = mod.validate_plan(p, actual=actual, lo=lo, hi=hi,
                                  beat_ids=beat_ids, block_ids=block_ids,
                                  l2_beat_ids=l2_ids)
        except Exception as exc:  # noqa: BLE001 — 校验器异常按违规处理（不阻断主链）
            v = [f"校验异常: {exc}"]
        if v:
            key = str(p.get("label") or p.get("strategy") or f"plan{i + 1}")
            viols[key] = [str(x)[:200] for x in list(v)[:8]]
        else:
            valid.append(p)
    return valid, viols


def _balance_execution_order(mod, valid: list[dict], selected: dict,
                             actual: int, lo: int, hi: int) -> list[dict]:
    """执行顺序：选中方案优先，其余备选按质量分降序（质量优先，spec R4）。"""
    def _q(p: dict) -> int:
        q = _balance_plan_quality(mod, p, actual, lo, hi)
        return q if q is not None else 0

    try:
        def _r(p: dict) -> int:
            return int(mod.compute_residual_gap(actual, p.get("operations") or [], lo, hi))
    except Exception:  # noqa: BLE001
        def _r(p: dict) -> int:
            predicted = actual + sum(int(o.get("expected_delta") or 0)
                                     for o in (p.get("operations") or [])
                                     if isinstance(o, dict))
            return _balance_residual(predicted, lo, hi)

    others = [p for p in valid if p is not selected]
    others.sort(key=lambda p: (-_q(p), _r(p)))
    return [selected] + others


def _balance_pick_plan(mod, valid: list[dict], actual: int, lo: int, hi: int) -> dict | None:
    """质量优先择优：优先 select_plan，异常/空结果时按 score_plan 兜底。"""
    try:
        choice = mod.select_plan(valid, actual=actual, lo=lo, hi=hi,
                                 quality_weight=_wc_quality_weight(), tie=_wc_quality_tie())
        if isinstance(choice, dict) and isinstance(choice.get("selected"), dict):
            return choice["selected"]
    except Exception as exc:  # noqa: BLE001 — 择优器异常退回本地确定性排序
        print(f"[WC_PLAN] select_plan 异常，改本地质量排序：{exc}", flush=True)
    if not valid:
        return None
    return sorted(valid, key=lambda p: (
        -(_balance_plan_quality(mod, p, actual, lo, hi) or 0),
        _balance_residual(actual + sum(int(o.get("expected_delta") or 0)
                                       for o in (p.get("operations") or [])
                                       if isinstance(o, dict)), lo, hi)))[0]


def _balance_trace_path(book: Path, chapter_id: int, prose_path: Path, *, dry_run: bool) -> Path:
    """轮次审计路径：正式 `追踪/篇幅平衡/第N章.json`；dry_run 落 sandbox。"""
    if not dry_run:
        return book / _TRACKING_DIR / "篇幅平衡" / f"第{chapter_id}章.json"
    if "body-preview" in prose_path.parts:
        return prose_path.parent.parent / "篇幅平衡" / f"第{chapter_id}章.json"
    sb = _dry_run_sandbox(book, f"balance-{chapter_id}")
    return sb / "篇幅平衡" / f"第{chapter_id}章.json"


def _balance_write_audit(path: Path, audit: dict) -> None:
    """每轮落审计 JSON；写失败只告警（审计不允许阻断主链）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(audit, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"[WC_PLAN] 篇幅平衡审计落盘失败（不阻断）: {exc}", flush=True)


def _balance_restore_body(prose_path: Path, text: str) -> None:
    """把正文恢复到本轮起始稿（候选质量不过/执行异常时的事务回滚）。"""
    try:
        prose_path.write_text(_balance_ensure_nl(text), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 — 恢复失败只告警，后续闸门仍会兜底
        print(f"[WC_PLAN] 候选稿回滚失败（盘上保留候选稿）：{exc}", flush=True)


def _balance_read_path(prose_path: Path, text: str, *, dry_run: bool,
                       tmp_paths: list[Path]) -> Path:
    """给定向执行提供「当前稿视图」：dry_run 且正文是生产文件时用临时副本（绝不改正式稿）。"""
    if not dry_run:
        return prose_path
    if "body-preview" in prose_path.parts or "dry_run" in prose_path.parts:
        prose_path.write_text(_balance_ensure_nl(text), encoding="utf-8")
        return prose_path
    import tempfile as _tf  # noqa: PLC0415
    fd, name = _tf.mkstemp(prefix="nww-bal-view-", suffix=".md")
    os.close(fd)
    p = Path(name)
    p.write_text(_balance_ensure_nl(text), encoding="utf-8")
    tmp_paths.append(p)
    return p


def _balance_wordcount_at(book: Path, chapter_id: int, prose_path: Path, text: str, *,
                          dry_run: bool, tmp_paths: list[Path]) -> dict:
    """质量通过后的字数复检（spec R6-3）：正常模式走 step_wordcount_check 读盘。

    dry_run 且正文为生产文件时落临时副本，用同一 step_wordcount_check(dry_run body_path)
    口径复检，绝不触碰生产正文。
    """
    if not dry_run:
        prose_path.write_text(_balance_ensure_nl(text), encoding="utf-8")
        return step_wordcount_check(book, chapter_id)
    if "body-preview" in prose_path.parts or "dry_run" in prose_path.parts:
        prose_path.write_text(_balance_ensure_nl(text), encoding="utf-8")
        return step_wordcount_check(book, chapter_id, body_path=prose_path)
    import tempfile as _tf  # noqa: PLC0415
    fd, name = _tf.mkstemp(prefix="nww-bal-wc-", suffix=".md")
    os.close(fd)
    p = Path(name)
    p.write_text(_balance_ensure_nl(text), encoding="utf-8")
    tmp_paths.append(p)
    return step_wordcount_check(book, chapter_id, body_path=p)


def _balance_stop_text(reason: str, lo: int, hi: int) -> str:
    return {
        "in_band": f"已进带 {lo}-{hi}",
        "budget_exhausted": "轮次预算用尽",
        "efficiency_guard": "连续低效止损",
        "quality_rejected": "候选方案均未过质量闸",
        "planner_failed": "规划失败（回旧逻辑）",
        "INSUFFICIENT_VALID_OPERATIONS": "无合法操作（INSUFFICIENT_VALID_OPERATIONS）",
        "no_selectable_plan": "无可执行方案",
        "no_target": "无基础字数区间",
        "note_override_under": "note 显式目标欠账（不自动扩）",
        "wordcount_check_failed": "字数复检失败",
        "exception": "异常降级（advisory）",
    }.get(reason, reason or "未知")


def _balance_detail_text(final_wc: dict, rounds: int, label: str,
                         quality: int | None, net: int, residual: int,
                         stop_reason: str, lo: int, hi: int) -> str:
    """wordcount stage detail 文案（spec R8：轮次/方案/质量分/净增/residual_gap）。"""
    status = final_wc.get("status", "?")
    actual = final_wc.get("actual")
    if rounds <= 0:
        return f"字数 {actual}（{status}），平衡 0 轮（{_balance_stop_text(stop_reason, lo, hi)}）"
    inner = f"净增 {net:+d}，残差 {residual}"
    if label:
        q_txt = f"，质量分 {quality}" if quality is not None else ""
        inner = f"方案: {label}{q_txt}，{inner}"
    tail = "" if stop_reason == "in_band" else f"（{_balance_stop_text(stop_reason, lo, hi)}）"
    return f"字数 {actual}（{status}），平衡 {rounds} 轮（{inner}）{tail}"


def step_length_balance(book: Path, chapter_id: int, packet: dict, prose_path: Path,
                        wc: dict, *, dry_run: bool, progress=None) -> dict:
    """方案化篇幅平衡（spec length-balance-v1 / T21）。

    流程：R1 基础区间 → R2 候选方案（N 套）→ R3 确定性校验（违规打回 1 次）
    → R4 质量优先择优 → R5 定向执行 → R6 先质量后字数（质量不过换备选，未进带
    基于当前稿继续下一轮）→ R7 连续 2 轮低效止损 → R8 每轮审计。

    返回：
      {"ok", "detail", "wordcount", "degraded", "rounds", "selected_strategy",
       "quality_score", "net_delta", "residual_gap", "stop_reason", "audit_path"}
    degraded=True 表示本环节未真正执行（无目标/模块缺失/规划解析失败），调用方回旧逻辑；
    运行中异常按 advisory 保留已采纳结果，不阻断主链（R9）。
    """
    _res: dict = {
        "ok": True, "detail": "", "wordcount": wc, "degraded": False,
        "rounds": 0, "selected_strategy": "", "quality_score": None,
        "net_delta": 0, "residual_gap": 0, "stop_reason": "", "audit_path": None,
    }
    mod = _balance_mod()
    if mod is None:
        _res.update(degraded=True,
                    detail=f"篇幅平衡模块不可用（{_BALANCE_MOD_CACHE.get('error') or '未落盘'}）")
        return _res

    target = (wc or {}).get("target")
    try:
        lo, hi = _balance_band(target, mod)
    except Exception as exc:  # noqa: BLE001
        _res.update(degraded=True, detail=f"字数目标解析异常（回旧逻辑）: {exc}")
        return _res
    # R1：无基础区间 / 无法解析 → 不自动扩缩（只出 advisory，交作者）
    if lo <= 0:
        _res.update(stop_reason="no_target",
                    detail=f"无基础字数区间（target={target!r}），不自动扩缩（advisory）")
        return _res
    actual = int((wc or {}).get("actual") or 0)
    if actual <= 0:
        _res.update(degraded=True, detail="实际字数为 0/未知，跳过篇幅平衡（回旧逻辑）")
        return _res
    if lo <= actual <= hi:
        _res.update(stop_reason="in_band",
                    detail=f"字数 {actual}（带内 {lo}-{hi}），平衡 0 轮（无需调整）")
        return _res
    # 既有语义（R9）：note 显式目标的 under 欠账由作者自担，不自动扩
    if (wc or {}).get("note_override") and (wc or {}).get("status") == "under":
        _res.update(stop_reason="note_override_under",
                    detail=f"字数 {actual}（under），note 显式目标欠账不自动扩（既有语义）")
        return _res

    n_cand = _wc_candidate_count()
    rounds_budget = _wc_plan_rounds()
    stop_eff = _wc_stop_efficiency()
    l2_ids = _balance_l2_beat_ids(packet)

    try:
        original_start = prose_path.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        _res.update(degraded=True, detail=f"正文读取失败（回旧逻辑）: {exc}")
        return _res

    current_text = original_start
    cur_actual = actual
    start_actual = actual
    history: list[dict] = []
    tmp_paths: list[Path] = []
    stop_reason = "budget_exhausted"
    _exc_note = ""
    low_eff_streak = 0
    last_label = ""
    last_quality: int | None = None
    audit = {
        "schema": "nww_length_balance_audit/v1",
        "chapter": chapter_id, "target": str(target or ""), "band": [lo, hi],
        "start_actual": start_actual, "dry_run": bool(dry_run),
        "candidates_per_round": n_cand, "rounds_budget": rounds_budget,
        "rounds": history, "final": {},
    }
    audit_path = _balance_trace_path(book, chapter_id, prose_path, dry_run=dry_run)

    try:
        for rnd in range(1, rounds_budget + 1):
            gap = _balance_residual(cur_actual, lo, hi)
            if gap <= 0:
                stop_reason = "in_band"
                break
            direction = "expand" if cur_actual < lo else "condense"
            if progress is not None:
                try:
                    progress("wordcount", None,
                             f"篇幅平衡 第{rnd}轮：缺口 {gap} 字，生成 {n_cand} 套方案…")
                except Exception:  # noqa: BLE001
                    pass

            # R2：候选方案（解析失败内部重试 1 次）
            plans = _balance_plan_candidates(book, chapter_id, packet, current_text,
                                             cur_actual, lo, hi, n_cand, direction)
            if not plans:
                stop_reason = "planner_failed"
                # 仅在「尚未采纳任何候选稿」时回旧逻辑；已有采纳则保留当前稿，
                # 避免旧 2 轮逻辑在既有增量上重复扩缩。
                if not history:
                    _res["degraded"] = True
                    _res["detail"] = "篇幅平衡规划输出不可用（回旧 2 轮逻辑）"
                _res["audit_path"] = str(audit_path)
                history.append({"round": rnd, "gap": gap, "direction": direction,
                                "candidates": [], "selected": None, "rejected": [],
                                "error": "规划输出不可用"})
                _balance_write_audit(audit_path, audit)
                break

            # R3：确定性校验；违规方案打回重生成 1 次（带 feedback）
            beat_ids = _balance_beat_ids(packet)
            block_ids = _balance_block_ids(current_text)
            valid, viols = _balance_validate_plans(
                mod, plans, cur_actual, lo, hi, beat_ids, block_ids, l2_ids)
            if viols:
                _fb = json.dumps(viols, ensure_ascii=False)[:1500]
                plans2 = _balance_plan_candidates(book, chapter_id, packet, current_text,
                                                  cur_actual, lo, hi, n_cand, direction,
                                                  feedback=_fb)
                valid2, _ = _balance_validate_plans(
                    mod, plans2, cur_actual, lo, hi,
                    _balance_beat_ids(packet), _balance_block_ids(current_text), l2_ids)
                _seen = {_balance_plan_key(p) for p in valid}
                valid = valid + [p for p in valid2 if _balance_plan_key(p) not in _seen]
            round_rec: dict = {
                "round": rnd, "gap": gap, "direction": direction,
                "candidates": plans, "violations": viols,
                "selected": None, "rejected": [],
            }
            if not valid:
                stop_reason = "INSUFFICIENT_VALID_OPERATIONS"
                history.append(round_rec)
                _balance_write_audit(audit_path, audit)
                break

            # R4：质量优先择优
            selected = _balance_pick_plan(mod, valid, cur_actual, lo, hi)
            if selected is None:
                stop_reason = "no_selectable_plan"
                history.append(round_rec)
                _balance_write_audit(audit_path, audit)
                break
            sel_label = str(selected.get("label") or selected.get("strategy") or "")
            sel_q = _balance_plan_quality(mod, selected, cur_actual, lo, hi)
            round_rec["selected"] = {
                "label": sel_label, "strategy": selected.get("strategy"),
                "quality": sel_q,
                "residual_gap": _balance_residual(
                    cur_actual + sum(int(o.get("expected_delta") or 0)
                                     for o in (selected.get("operations") or [])
                                     if isinstance(o, dict)), lo, hi),
                "operations": selected.get("operations") or [],
            }

            # R5/R6：定向执行；质量不过 → 换下一套备选（最多 N 套）
            order = _balance_execution_order(mod, valid, selected, cur_actual, lo, hi)
            adopted: dict | None = None
            prev_actual = cur_actual
            for cand_plan in order:
                view = None
                try:
                    view = _balance_read_path(prose_path, current_text,
                                              dry_run=dry_run, tmp_paths=tmp_paths)
                    cand_text, exec_rep = step_apply_balance_plan(
                        book, chapter_id, packet, view, cand_plan)
                except Exception as exc:  # noqa: BLE001 — 单方案执行异常 → 换备选
                    print(f"[WC_PLAN] 方案执行异常（换备选）：{exc}", flush=True)
                    # 既有扩缩写入口在收尾落盘；异常后必须恢复本轮起始稿（事务语义）
                    if view == prose_path:
                        _balance_restore_body(prose_path, current_text)
                    round_rec["rejected"].append({
                        "label": str(cand_plan.get("label") or ""),
                        "violations": [f"执行异常: {exc}"]})
                    continue
                quality = _balance_quality_check(
                    current_text, cand_text, cand_plan, mod=mod, lo=lo, hi=hi)
                if not quality.get("ok"):
                    # 质量不过 → 弃候选稿并恢复本轮起始稿，换下一套备选（最多 N 套）
                    if view == prose_path:
                        _balance_restore_body(prose_path, current_text)
                    round_rec["rejected"].append({
                        "label": str(cand_plan.get("label") or ""),
                        "violations": quality.get("violations") or [],
                        "report": exec_rep[:200]})
                    continue
                # 质量通过 → 字数复检（R6-3）；复检失败也采纳（质量已过）并停止
                new_wc = _balance_wordcount_at(book, chapter_id, prose_path, cand_text,
                                               dry_run=dry_run, tmp_paths=tmp_paths)
                new_actual = int(new_wc.get("actual") or _visible_chars(cand_text)[0])
                adopted = {
                    "plan": cand_plan, "text": cand_text, "actual": new_actual,
                    "quality": quality, "wc": new_wc, "report": exec_rep,
                    "label": str(cand_plan.get("label")
                                 or cand_plan.get("strategy") or ""),
                    "quality_score": _balance_plan_quality(
                        mod, cand_plan, prev_actual, lo, hi),
                }
                break

            if adopted is None:
                stop_reason = "quality_rejected"
                history.append(round_rec)
                _balance_write_audit(audit_path, audit)
                break

            current_text = adopted["text"]
            cur_actual = adopted["actual"]
            net = cur_actual - prev_actual
            last_label = adopted["label"] or sel_label
            last_quality = adopted["quality_score"]
            round_rec.update({
                "adopted": last_label,
                "exec_report": str(adopted.get("report") or "")[:300],
                "result_actual": cur_actual,
                "quality": {"ok": True,
                            "violations": adopted["quality"].get("violations") or []},
                "net_delta": net,
                "residual_gap": _balance_residual(cur_actual, lo, hi),
            })
            history.append(round_rec)
            _balance_write_audit(audit_path, audit)  # R8：每轮即时落审计

            if not adopted["wc"].get("ok"):
                stop_reason = "wordcount_check_failed"
                break
            if lo <= cur_actual <= hi:
                stop_reason = "in_band"
                break
            # R7：连续 2 轮净增/净减不足请求量 × 阈值 → 止损（采纳当前稿）
            if abs(net) < gap * stop_eff:
                low_eff_streak += 1
            else:
                low_eff_streak = 0
            if low_eff_streak >= 2:
                stop_reason = "efficiency_guard"
                break
            stop_reason = "budget_exhausted"
            _balance_write_audit(audit_path, audit)
    except runtime.AgentCancelled:
        raise
    except Exception as exc:  # noqa: BLE001 — R9：任何异常 advisory 降级，不阻断主链
        print(f"[WC_PLAN] 篇幅平衡异常（advisory 继续）：{exc}", flush=True)
        stop_reason = "exception"
        _exc_note = f"；异常降级：{str(exc)[:200]}"
    finally:
        for p in tmp_paths:
            try:
                p.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass

    if _res.get("degraded"):
        # 启动阶段失败（模块/目标/规划解析）→ 调用方回旧 2 轮逻辑，保留审计可见性
        _res.update(stop_reason=stop_reason, rounds=len(history),
                    audit_path=str(audit_path))
        return _res

    # 最终字检：以当前稿（已采纳/上轮稿）为准
    try:
        final_wc = _balance_wordcount_at(book, chapter_id, prose_path, current_text,
                                         dry_run=dry_run, tmp_paths=tmp_paths)
    except Exception as exc:  # noqa: BLE001
        final_wc = dict(wc or {})
        print(f"[WC_PLAN] 最终字检异常（沿用上次数值）：{exc}", flush=True)

    final_actual = int(final_wc.get("actual") or cur_actual or 0)
    residual = _balance_residual(final_actual, lo, hi)
    _res.update({
        "ok": True,
        "wordcount": final_wc,
        "rounds": len(history),
        "selected_strategy": last_label,
        "quality_score": last_quality,
        "net_delta": final_actual - start_actual,
        "residual_gap": residual,
        "stop_reason": stop_reason,
        "audit_path": str(audit_path),
        "detail": _balance_detail_text(final_wc, len(history), last_label,
                                       last_quality, final_actual - start_actual,
                                       residual, stop_reason, lo, hi) + _exc_note,
    })
    audit["final"] = {
        "actual": final_actual, "status": final_wc.get("status"),
        "remaining_gap": residual, "net_delta": final_actual - start_actual,
        "adopted": bool(history and any(r.get("result_actual") is not None for r in history)),
        "stop_reason": stop_reason, "selected_strategy": last_label,
        "quality_score": last_quality,
    }
    _balance_write_audit(audit_path, audit)
    print(f"[WC_PLAN] 完成：{_res['detail']}（审计 {audit_path}）", flush=True)
    return _res


# 主笔输出确定性清洗：剥离 LLM 元信息污染（英文思考/写作指示/工具调用/中段烟火值旁注）
_WRITER_TOOL_BLOCK_RE = re.compile(
    r"<(?:tool_call|function|Bash|shell|execute|read|apply|think)[^>]*>.*?</(?:tool_call|function|Bash|shell|execute|read|apply|think)>",
    re.S,
)
_WRITER_SMOKE_RE = re.compile(r"【烟火值：\d+】")
# 标题行：允许行首带 markdown 装饰符（` * _ >）——writer 偶发把标题写成
# `` `# 第28章 x` `` 或 `**# 第28章 x**`（实测 2026-09-13 第028章）。
# 严格 `^#` 锚定会让这类标题：① 被第 2 步当成普通行留下（ascii 占比低不剔）；
# ② 第 4/6/7 步全部失配；③ 落盘后 nww_delta_draft._TITLE_RE 也失配 →
# 逐章记录标题回填成「【草稿】请填写章节标题」，前端 markdown 也渲染不出 H1。
_TITLE_DECOR_RE = re.compile(r"^[`*_>\s]+")
_TITLE_LINE_RE = re.compile(r"^#+\s*第\s*\d+\s*章")


def _normalize_title_line(s: str) -> str | None:
    """若 `s` 经装饰剥离后是章节标题行 → 返回规范化的 `# 第N章 …`；否则 None。

    只对**疑似标题行**生效：普通正文行里的星号/反引号原样保留，零副作用。
    幂等：正常标题 `# 第28章　道心卡壳` 进去还是它自己。
    """
    stripped = _TITLE_DECOR_RE.sub("", s)
    if not _TITLE_LINE_RE.match(stripped):
        return None
    return re.sub(r"[`*_\s]+$", "", stripped).rstrip()


def _sanitize_writer_output(text: str) -> str:
    """主笔输出确定性清洗，落盘前调用（正文以中文为主，ASCII 主导行=LLM 元信息污染）。

    1. 剥离 XML 风格工具调用/思考块；
    2. 逐行过滤：ASCII 占比 > 0.55 且非章节标题 → 剔除（英文思考/写作指示/统计说明）；
    3. 中段【烟火值：N】旁注剔除：仅保留最后一个标记（章尾合法惯例），其余删行；
    4. 正文必须以 `# 第N章` 标题开头：标题前的残留（writer 旁注/续写）一律丢弃；
       标题行先经 `_normalize_title_line` 剥离 markdown 装饰（见其注释）；
    5. 段落协议归一：一段一行 + 段间空行（`\n\n`）。
    """
    text = _WRITER_TOOL_BLOCK_RE.sub("", text)
    keep: list[str] = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            # 空行本身不携带内容，段落结构由第 5 步统一重建，这里直接跳过。
            continue
        title = _normalize_title_line(s)
        if title is not None:
            keep.append(title)
            continue
        if re.match(r"^第\s*\d+\s*章", s):
            keep.append(s)
            continue
        ascii_ratio = sum(1 for c in s if ord(c) < 128) / max(len(s), 1)
        if ascii_ratio > 0.55:
            continue
        keep.append(s)
    # 5. 段落协议归一（V12.5，2026-09-13）
    #    长篇正文的唯一合法格式是「一段一行、段间空行」（见 writer_prompt.py 的
    #    正文格式要求）。主笔有时只给单换行——实测第 025-028 章整章单换行。
    #    旧实现在这里做的是 `"\n".join(keep)`，等于把空行删光后按单换行重拼：
    #      * 落盘文件变成「紧凑正文」→ paragraph_gate 只能靠兜底协议猜段；
    #      * nww_enforce 结尾泄漏清理取 paras[-1] = 整章，命中即整章被删；
    #      * 前端按空行渲染时整章挤成一坨。
    #    改为用 `\n\n` 重拼并压缩 3 个以上连续换行：**只动空白，不增删内容**。
    out = re.sub(r"\n{3,}", "\n\n", "\n\n".join(keep)).strip()
    smoke_idx = [i for i, ln in enumerate(out.splitlines()) if _WRITER_SMOKE_RE.search(ln)]
    if len(smoke_idx) > 1:
        last = smoke_idx[-1]
        lines = [ln for i, ln in enumerate(out.splitlines()) if i == last or not _WRITER_SMOKE_RE.search(ln)]
        out = "\n".join(lines).strip()
    # 6. 标题前残留丢弃：正文必须以 `# 第N章` 开头
    #    用 _TITLE_DECOR_RE 先剥装饰再判，与第 4 步同一口径。
    head_lines = out.lstrip("\n").splitlines()
    for i, ln in enumerate(head_lines):
        if _TITLE_LINE_RE.match(_TITLE_DECOR_RE.sub("", ln.strip())):
            out = "\n".join(head_lines[i:]).strip()
            break
    # 7. 标题行后补空行（markdown 分块要求，保证前端标题/正文区分字号）
    #    经第 5 步归一后，标题行后面本来就有空行 → 这里是幂等兜底，
    #    只为兼容「标题行是唯一一行」之类的退化输入。
    head_lines = out.splitlines()
    for i, ln in enumerate(head_lines):
        if _TITLE_LINE_RE.match(_TITLE_DECOR_RE.sub("", ln.strip())) and i + 1 < len(head_lines) and head_lines[i + 1].strip():
            head_lines.insert(i + 1, "")
            break
    return re.sub(r"\n{3,}", "\n\n", "\n".join(head_lines).strip())


def _ensure_title_consistent(book: Path, chapter_id: int, clean: str) -> str:
    """确定性标题回填（2026-09-15 根治）：正文首行标题必须存在且三位置一。

    _sanitize_writer_output 只做「剥标题前残留 + 保留标题行」，**从不校验标题是否
    存在、也从不补三位**——writer 直接输出正文时清洗后首行=正文，_chapter_title
    便回填成【草稿】（第027章重写实测：正文首行=「清晨的坊市…」，逐章记录被记为
    `# 第027章 · 【草稿】请填写章节标题`）。

    此处以「既有正文文件名」为权威标题源（`第0XX章_标题.md`）：revision 重写时
    旧稿仍在，_resolve_existing_body 能取到；纯新写（文件尚不存在）无权威标题，
    保持原样、不编造。标题行存在则幂等替换为 `# 第0XX章　标题`，缺位则前插。
    """
    existing = _resolve_existing_body(book, chapter_id)
    if existing is None:
        return clean
    m = re.match(r"^第0*(\d+)章[_-](.+?)\.md$", existing.name)
    if not m or not m.group(2).strip():
        return clean
    # 仅当文件名章号与目标章一致才回填（防跨章误匹配）
    if int(m.group(1)) != chapter_id:
        return clean
    target_title = f"# 第{chapter_id:03d}章　{m.group(2).strip()}"

    head = clean.splitlines()
    for i, ln in enumerate(head[:5]):
        if _TITLE_LINE_RE.match(_TITLE_DECOR_RE.sub("", ln.strip())):
            head[i] = target_title
            return re.sub(r"\n{3,}", "\n\n", "\n".join(head)).strip()
    # 无标题行 → 前插标准标题行
    return (target_title + "\n\n" + clean).strip()


_OUTLINE_TITLE_BAD_RE = re.compile(r"(细纲|大纲|章纲|提示|指令|输出格式|prompt)", re.I)


def _apply_outline_title_for_new_chapter(book: Path, chapter_id: int, clean: str) -> str:
    """新章标题权威源（2026-10-24 扩展）：尚无既存正文时，以单章细纲 H1 标题为准。

    为什么需要：_ensure_title_consistent 只认「既有正文文件名」，纯新写时 existing=None
    直接原样返回，writer 自造的标题（实测 `# 第2章　神仙快乐水`）便一路进正文与文件名。
    既有章节行为不变：只要正文文件已存在，本函数直接返回，交给原回填逻辑处理。
    """
    if _resolve_existing_body(book, chapter_id) is not None:
        return clean
    try:
        from app.nwwmvp import _chapter_title_from_outline

        title = (_chapter_title_from_outline(book, chapter_id) or "").strip()
    except Exception:  # noqa: BLE001 — 取标题失败不得阻断落盘
        return clean
    # 有效性检查：空串、异常长标题、含指令/纲目字样的一律不当作权威标题
    if not title or len(title) > 40 or _OUTLINE_TITLE_BAD_RE.search(title):
        return clean
    target_title = f"# 第{chapter_id:03d}章　{title}"
    lines = clean.splitlines()
    for i, ln in enumerate(lines[:5]):
        if _TITLE_LINE_RE.match(_TITLE_DECOR_RE.sub("", ln.strip())):
            lines[i] = target_title
            return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return (target_title + "\n\n" + clean).strip()


def step_delta_draft(book: Path, chapter_id: int, body_path: Path | None = None,
                     sandbox_dir: Path | None = None,
                     preserve_quality_meta: bool = False) -> Path:
    """4. delta 草稿预填（确定性抽取 → 追踪/delta_第N章.json）。

    body_path: dry_run 预览正文路径；sandbox_dir: dry_run delta-preview 目录。
    preserve_quality_meta（2026-11-01 实锤修复）：`nww_delta_draft.py` 是整份重建
    payload 后覆盖写入（脚本内无 quality_meta 逻辑），会把上一轮 post_write/审稿留下的
    `reviewed/review_score/reviewed_hash/final_hash/target_body_md5` 全部抹掉 ——
    实测 00:04 L2 审稿 86 分提交后，00:12 一次 deslop 就把指纹抹干净。
    后果（代码已证）：`_verify_frozen_body_hash` 见 `reviewed` 缺失直接 return None
    （不校验）；A-5 布尔校验只在 append 模式生效（`nww_track_commit.py:3766` revision
    豁免），且缺字段默认只 WARNING。即：留痕一擦，后续没有任何东西能证明这章审过。
    非审稿链（deslop/revise/polish）传 True：跑脚本后把旧 quality_meta 的**系统键**
    以 setdefault 合并回来（新值优先，不覆盖脚本产出）。异常静默跳过，不阻断流水线。
    """
    tracking = book / _TRACKING_DIR
    tracking.mkdir(parents=True, exist_ok=True)
    if sandbox_dir is not None:
        sandbox_dir.mkdir(parents=True, exist_ok=True)
        out_path = sandbox_dir / f"{_DELTA_PREFIX}{chapter_id:03d}章.json"
    else:
        out_path = tracking / f"{_DELTA_PREFIX}{chapter_id:03d}章.json"
    _prev_qm: dict | None = None
    if preserve_quality_meta and out_path.is_file():
        try:
            _prev = json.loads(out_path.read_text(encoding="utf-8"))
            _prev_tx = _prev.get("transaction", _prev) if isinstance(_prev, dict) else {}
            _prev_delta = _prev_tx.get("delta") if isinstance(_prev_tx, dict) else None
            _prev_qm_raw = _prev_delta.get("quality_meta") if isinstance(_prev_delta, dict) else None
            if isinstance(_prev_qm_raw, dict) and _prev_qm_raw:
                _prev_qm = dict(_prev_qm_raw)
        except Exception:  # noqa: BLE001
            _prev_qm = None
    cmd = ["--project", str(book), "--chapter", str(chapter_id), "--out", str(out_path)]
    if body_path is not None:
        cmd.extend(["--body", str(body_path)])
    res = toolbox.run_script(
        "nww_delta_draft.py",
        cmd,
        book,
        timeout=120,
    )
    if not res["ok"] or not out_path.is_file():
        raise RuntimeError(res["stderr"] or res["stdout"] or "delta 草稿生成失败")
    if _prev_qm:
        try:
            _data = json.loads(out_path.read_text(encoding="utf-8"))
            _tx = _data.get("transaction", _data) if isinstance(_data, dict) else _data
            _delta = _tx.setdefault("delta", {}) if isinstance(_tx, dict) else None
            if isinstance(_delta, dict):
                _qm = _delta.get("quality_meta")
                if not isinstance(_qm, dict):
                    _qm = {}
                    _delta["quality_meta"] = _qm
                for _k in _sys_quality_meta_keys():
                    if _k in _prev_qm:
                        _qm.setdefault(_k, _prev_qm[_k])
                out_path.write_text(
                    json.dumps(_data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            print(f"[QUALITY_META] 旧 quality_meta 合并失败（忽略，不阻断）: {exc}")
    return out_path


# LLM 拒答/答非所问的特征句（2026-09-29 实测样本：「我是一个文本 AI，这超出了我的能力范围。」
# 「由于程序代码的局限，我没法办到。」「拒绝提供帮助」「无法提供帮助」）
_JSON_REFUSAL_RE = re.compile(
    r"超出(?:我的|了我的)?能力范围|这超出了我的|无法(?:完成|处理|回答|提供)(?:该|此)?(?:请求|任务|问题|帮助)|"
    r"拒绝(?:提供|协助|回答|生成)|没法办到|办不到|程序代码的局限|不能(?:为你|为您)?(?:生成|规划|完成)|"
    r"I'?m (?:just )?a (?:text|language) model|I cannot|As an AI",
    re.I,
)


def _json_err_kind(s: str) -> str:
    """给「拿不到 JSON」定性，把 JSONDecodeError 翻译成人话。

    ★为什么必须定性（2026-09-29 第2章 L3 两次事故）：三种失败形态的**处置完全不同**——
      模型拒答 → 重试大概率还是拒答，该换通道；
      回显污染（回复里混进提问原文）→ 页面上其实生成好了，重试**有救**；
      真语法错/截断 → 调 prompt 或加 max_tokens。
    混成一句 `Expecting ',' delimiter: line 1 column N`，人工根本没法对症下药。
    """
    if not s or not s.strip():
        return "模型输出为空"
    if _JSON_REFUSAL_RE.search(s):
        return "疑似模型拒答"
    if s.find("{") >= 0 and s.rfind("}") < 0:
        return "输出被截断（有 { 无 }）"
    if s.find("{") < 0:
        return "未输出 JSON 对象"
    return "JSON 语法错误"


def _echo_pollution_hit(raw: str, prompt: str) -> bool:
    """检测「回复里混进了提问原文」（回显污染）。

    2026-09-29 第2章 L3 Plan 事故：chrome 桥接从 DOM 抓回复时把**输入框里的提问原文**
    一并抓了进来，插在 JSON 中间（`...言语极{"1_immutable_contract":{"canon":[...`），
    结构被切断 → 报 `Expecting ',' delimiter`。而 CDP 直读页面证明：模型生成得好好的。
    既有 `_ECHO_PREFIX_RATIO` 只查「开头回吐」，**中间插入型完全没覆盖**。

    判据：**同一个 ≥40 字的 prompt 片段在回复里出现 ≥2 次**。
    一次 = 正常引用（L3 的 canon/正文锚点本来就要照抄原文，出现 1 次是合规的）；
    两次 = 多出来的那次就是被抓进来的提问原文。这样既不误伤正常引用，又能抓住
    中间插入型（实测两次事故都是契约键 `"1_immutable_contract"` 被吐了第二遍）。
    """
    if not raw or not prompt:
        return False
    step = max(1, len(prompt) // 60)
    for i in range(0, len(prompt), step):
        frag = prompt[i:i + 40]
        if len(frag) >= 40 and raw.count(frag) >= 2:
            return True
    return False


def _loads_json_object(raw: str) -> dict:
    """从 LLM 输出里取 JSON 对象：剥 markdown 围栏 → 首个 `{` 到末个 `}` → 解析。

    为什么必须走它（2026-09-29 事故）：delta 语义补全的契约是「只输出 JSON 对象」，
    但宿主 LLM（尤其网页桥接的 Gemini）会偶发拒答/空输出 —— 实测返回 21 字
    「我是一个文本 AI，这超出了我的能力范围。」。裸 `json.loads` 把它变成
    `Expecting value: line 1 column 1 (char 0)`：既看不出是模型拒答还是 JSON 写错，
    也被上层包成「delta 处理失败」，白白烧掉前面整章的写章产出。

    返回值：dict。任何非 JSON / 非对象 → ValueError，消息带「拒答 or 空 or 语法错」
    定性 + 原文片段，供调用方决定是否重试、供人工一眼看懂。
    """
    s = (raw or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.S).strip()
    if not s:
        raise ValueError("模型输出为空")
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        raise ValueError(f"{_json_err_kind(s)}：{s[:200]!r}")
    try:
        data = json.loads(s[i:j + 1])
    except json.JSONDecodeError as exc:
        kind = "疑似模型拒答" if _JSON_REFUSAL_RE.search(s) else "JSON 语法错误"
        raise ValueError(f"{kind}（{exc}）：原文片段 {s[:200]!r}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"JSON 顶层不是对象（{type(data).__name__}）：{s[:200]!r}")
    return data


# 首次未拿到 JSON 时的追加指令（重发一次，仍失败才判环节失败）
_DELTA_JSON_RETRY_HINT = (
    "\n\n【重试·强制契约】上一次你没有输出可解析的 JSON（或输出了拒答/解释文本）。\n"
    "本次必须只输出一个 JSON 对象：以 { 开头、以 } 结尾，键名与上面字段契约完全一致。\n"
    "禁止任何前言、解释、道歉、markdown 代码块、省略号。\n"
    "若确实无法补全某字段，就保留草稿里的原值（含【草稿】标记），但整体仍必须是合法 JSON 对象。"
)


def _carry_system_quality_meta(filled: dict, draft: dict) -> dict:
    """把草稿里「系统写」的 quality_meta 键确定性回填进 LLM 产出（2026-11-01 实锤）。

    `step_delta_semantic` 收尾是 `tx["delta"] = filled`（整段替换），而 filled 来自 LLM；
    草稿里即使带着 reviewed/final_hash，LLM 是否原样抄回**不可控**（prompt 只写「已填
    字段原样保留」）。叠加 `nww_delta_draft.py` 整份重建 delta，就成了实测的
    「00:04 审稿 86 分提交 → 00:12 deslop 后 quality_meta 只剩 enforced」。
    这里只回填系统键：LLM 抄漏/改坏都不会再影响审稿指纹；模型声明的 declared/observed 不动。
    """
    src = draft.get("quality_meta") if isinstance(draft, dict) else None
    if not isinstance(src, dict) or not src:
        return filled
    out = filled.get("quality_meta")
    if not isinstance(out, dict):
        out = {}
    for k in _sys_quality_meta_keys():
        if k in src:
            out[k] = src[k]
    filled["quality_meta"] = out
    return filled


def step_delta_semantic(book: Path, chapter_id: int, draft_path: Path, prose: Path) -> None:
    """5. delta 补语义：LLM 补齐【草稿】留白语义字段（结果/代价/承诺/时间线/全局）。"""
    raw = json.loads(draft_path.read_text(encoding="utf-8"))
    tx = raw.get("transaction", raw)
    delta = tx.get("delta", {})
    # 定位章节正文（不含标题行）
    prose_lines = [ln for ln in prose.read_text(encoding="utf-8").splitlines() if not ln.startswith("#")]
    prose_text = "\n".join(prose_lines)
    outline = _find_outline_file(book, chapter_id)
    outline_text = outline.read_text(encoding="utf-8") if outline else "（细纲缺失）"
    # P0 fix: 提取 next_timeline_id 硬值，注入 prompt 让 LLM 不再从 E001 起编
    _hints = tx.get("_draft_hints") if isinstance(tx.get("_draft_hints"), dict) else {}
    _next_tl_id = str(_hints.get("next_timeline_id") or "E001")
    try:
        _second_num = int(_next_tl_id[1:]) + 1
    except (ValueError, IndexError):
        _second_num = 2
        _next_tl_id = "E001"

    agent = registry.get_agent("nww-reviewer")
    prompt = (
        f"书项目根: {book}\n目标章: 第{chapter_id}章\n\n"
        f"【本章细纲】\n{outline_text}\n\n"
        f"【本章正文】\n{prose_text[:6000]}\n\n"
        f"【delta 草稿（含【草稿】留白标记）】\n{json.dumps(delta, ensure_ascii=False, indent=2)}\n\n"
        "任务：补齐所有标记为【草稿】或空的语义字段，已填字段原样保留。字段契约（必须严格遵守）：\n"
        '- "result": 字符串，一两句概述本章结果（照抄正文明确事实，禁推测），不得含【草稿】\n'
        '- "chapter_type": 字符串（推进/铺垫/爽点/代价/转折…），不得为 null\n'
        '- "next_chapter_commitments": **纯字符串数组**（每项一言描述下一章要兑现的承诺），\n'
        '  绝对禁止输出对象/JSON 元素；无承诺则为空数组 []\n'
        '- "cost_change": 对象 {"device": "...", "count": 数字} 或 null\n'
        '- "timeline_events": 对象数组（每项含 id/story_time/objective_fact/reader_knowledge）；无则 []\n'
        f'  【A-7 闸 · 硬约束】id 必须从 **{_next_tl_id}** 开始连续递增：第一条用 {_next_tl_id}，第二条用 E{_second_num:03d}，以此类推。\n'
        '  绝对禁止从 E001 起编，绝对禁止复用状态层已有 id，否则 commit 会被 BLOCK。\n'
        '  同时禁止篡改已有伏笔的 importance/planted_chapter 字段（已有 id 的伏笔保留原值，\n'
        '  新伏笔分配新 id）。\n'
        '- "foreshadow_changes": 对象数组，每项必须含 id/summary/status/importance/planted_chapter：\n'
        '  status 用"已埋/已回收/已过期/放弃"，importance 用"高/中/低"，planted_chapter 为整数章号（默认本章）；\n'
        '  已有合法字段保留原值；无伏笔变更则 []\n'
        '- "character_changes": 对象数组，**每项只能有 name 和 change 两个键**\n'
        '  （禁止 character / character_name / description / summary 等别名）；草稿里已列出的角色一个都不能删，\n'
        '  本章有实质变化的角色可以补进来。\n'
        '  change 里标记了【境界/能力变化】的角色：必须在 character_snapshots 里更新对应字段。\n'
        '  change 里标记了【状态检查】的角色：请根据正文判断 location/state 是否有变化，\n'
        '  如需更新则在 character_snapshots 里改对应字段（如无变化保持原值）。character_snapshots\n'
        '  已预填了所有出场角色的当前快照副本，你在副本上改即可。\n'
        '  ★ 但 change **文本本身**必须重写：所有以【状态检查】/【境界/能力变化】开头、含\n'
        '  「该角色本章出场。当前 location=… state=…」的占位誊写，一律改写成一句 ≤50 字的\n'
        '  真实变化描述（只写本章实际发生的变化，不誊写快照全文）；所有 change 合计 ≤150 字。\n'
        '- 严禁输出上面没提到的任何顶层键。唯一例外：`global_change`——若本章产生了**跨章累积性**\n'
        '  的全局变化，在 delta 同级输出它（系统会自动挂到事务根级，不属于 delta 字段）；\n'
        '  没有就**省略这个键**（不要输出 null）。\n'
        '  `global_change` 判定标准（必须能在正文里指到明确出处，禁推测）：\n'
        '  世界/势力格局变了、核心冲突换了形态、主角所处阶段/身份变了、某条长线的主持方\n'
        '  或归属变了、某个跨章机制（如代价系统/规则）的阈值或运行方式变了。\n'
        '  反例（**不算**全局变化，写进 result/character_changes 即可）：单章情绪、单场胜负、\n'
        '  角色一时想法、与既有字段重复的描述。\n'
        '  结构：顶层级数 ≤2，最多 8 个顶层键，值用短字符串/数字/布尔/短数组；\n'
        '  沿用既有键名（如 world/engine/position 下的 core_conflict、phase、changes）以便递归合并，\n'
        '  未变化的字段**不要重复输出**（未提及字段保持不变）。总长度 ≤600 字。\n'
        '- 列表条数上限：constraints ≤12、retired_context_items ≤11、next_chapter_commitments ≤5\n'
        '- 总量预算：delta 全部字段渲染后累计约 2800 字节（中文 1 字=3 字节）。超预算时精简\n'
        '  timeline_events 的 objective_fact/reader_knowledge（各 ≤50 字）与 foreshadow_changes 的\n'
        '  summary（≤40 字），禁止为省字节删条目。\n'
        '- 其余字段原样保留\n'
        '直接输出一个 JSON 对象（就是 delta 对象本身），不要任何解释、markdown 代码块或额外文本。'
    )
    def _ask(extra_hint: str) -> dict:
        """一次「补语义 + 解析」。空输出属端点抖动 → 按 env 重试；非 JSON 一律 ValueError。

        2026-11-13 实测：第1章 L3 重写已到 2539 字、wordcount internal_pass，却在
        delta 语义补全连续两次「模型输出为空」→ 事务回滚，整链白跑。空响应是基础设施
        抖动（非内容失败），重试代价低，不应一次抖动就回滚整章。
        """
        try:
            _empty_tries = int(os.environ.get("NWW_DELTA_SEMANTIC_EMPTY_RETRY", "3") or 3)
        except ValueError:
            _empty_tries = 3
        _empty_tries = max(1, min(5, _empty_tries))
        last = ""
        for _attempt in range(_empty_tries):
            out = _run_agent_bounded(agent, "delta 语义补全（只输出 JSON）", prompt + extra_hint,
                                     timeout=420.0, temperature=0.2, max_tokens=4096)
            if str(out or "").strip():
                return _loads_json_object(out)
            last = f"第{_attempt + 1}次：模型输出为空"
            print(f"[DELTA_SEMANTIC] 第{chapter_id}章 {last}，"
                  f"{'重试' if _attempt + 1 < _empty_tries else '放弃'}", flush=True)
            if _attempt + 1 < _empty_tries:
                time.sleep(min(6.0, 1.5 * (_attempt + 1)))
        raise ValueError(last)

    try:
        filled = _ask("")
    except ValueError as first_err:
        # 网页桥接模型偶发拒答（21 字「我是一个文本 AI…」），重发一次带强制契约的 prompt。
        # 只重试一次：两次都拿不到 JSON，说明不是偶发抖动，快失败 + 明确报错优于死循环烧额度。
        print(f"[DELTA_SEMANTIC] 第{chapter_id}章 首次补语义未拿到 JSON，重试一次：{first_err}", flush=True)
        try:
            filled = _ask(_DELTA_JSON_RETRY_HINT)
        except ValueError as second_err:
            raise RuntimeError(
                f"delta 语义补全失败：模型两次均未输出合法 JSON。"
                f"第一次：{first_err}；第二次：{second_err}"
            ) from second_err
    # 既埋伏笔 id 集合（state.foreshadow 键）：foreshadow id 是 state 的键，
    # 归一非法 id 分配新号时必须避开既有编号，绝不覆盖/分裂既埋伏笔。
    _existing_fs: set[str] = set()
    _existing_tl: set[str] = set()
    _append_mode = chapter_id > (_last_committed_chapter(book) or 0)
    try:
        _state_file = book / _TRACKING_DIR / "_tracking-state.json"
        _state_rows = json.loads(_state_file.read_text(encoding="utf-8")) or {}
        _fs_rows = _state_rows.get("foreshadow") or {}
        _existing_fs = {str(k) for k in _fs_rows} if isinstance(_fs_rows, dict) else set()
        _tl_rows = _state_rows.get("timeline") or {}
        _existing_tl = {str(k) for k in _tl_rows} if isinstance(_tl_rows, dict) else set()
    except Exception:  # noqa: BLE001 — 读不到 state 则按空集分配，不影响既有校验
        _existing_fs = set()
        _existing_tl = set()
    filled = _repair_delta(
        filled, delta, prose_text, chapter_id,
        existing_foreshadow_ids=_existing_fs, existing_timeline_ids=_existing_tl,
        append_mode=_append_mode,
        title=str(raw.get("transaction", {}).get("chapter_title") or tx.get("chapter_title") or ""),
    )
    # P0 修复（2026-10-12）：global_change 提升到 transaction 根级。
    #   commit 侧 nww_track_commit.py:1958 白名单本就接受它并递归合并进 state["global"]，
    #   此前因无人提升而 100% 静默丢弃（实测 0/26）。默认开，NWW_GLOBAL_CHANGE=0 回滚。
    if _global_change_enabled():
        filled, _gc = _hoist_global_change(filled, tx, chapter_id)
        if _gc is not None:
            tx["global_change"] = _gc
    # quality_meta 的系统键以草稿为权威回填（LLM 漏抄/改坏都不再影响审稿指纹）
    filled = _carry_system_quality_meta(filled, delta)
    # 更新草稿中的 delta 段落（保留机械预填 + LLM 补全）
    tx["delta"] = filled
    # LLM 补出的角色变化需要配套快照，否则 commit 的「核心角色必须交快照」会 BLOCK
    _reconcile_snapshots(tx, book)
    merged = {"chapter": chapter_id, "transaction": tx} if "transaction" in raw else tx
    draft_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")


def _clip_utf8(text: str, max_bytes: int) -> str:
    """按 UTF-8 **字节**裁剪，且绝不切坏多字节字符。

    为什么需要它：NWW 的追踪契约（nww_track_commit.clean_text）用的是字节上限，
    而调用方习惯按字符截。中文 1 字 = 3 字节，`s[:240]` 得到 716 字节，
    对 480 字节的 delta.result 而言恒定超标 → commit BLOCK。
    2026-09-13 实测于《用泡面征服修仙界》第27章重写（enforce/paragraph_gate 全 PASS
    却卡在 `ERROR: delta.result exceeds 480 byte`）。
    截断落在多字节字符中间时，用 errors="ignore" 丢弃残字节（结果只会更短，不会超）。
    """
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes].decode("utf-8", errors="ignore")


_STATE_CHECK_PLACEHOLDER_RE = re.compile(r"【状态检查】该角色本章出场。当前 location=.*", re.S)


def _compress_state_check(change: str) -> str:
    """【状态检查】占位誊写压缩（2026-09-22 新增，第38章 L3 重写 commit BLOCK 根因）。

    delta_draft 给每个出场角色预填的【状态检查】条目是**快照誊写占位**
    （nww_delta_draft.py:113-118：『该角色本章出场。当前 location=… state=…』），
    期望 LLM 补语义时改写成真实变化描述。但 step_delta_semantic prompt 的
    「已填字段原样保留」指令会误导 LLM 原样保留 → 每条 ~380B，4 个角色即 1.5KB，
    叠加信息密集章（timeline 5 条 + 伏笔 4 条）后渲染总量超 commit 的
    DELTA_MAX_BYTES=3072 硬上限（nww_track_commit.render_delta:1538，实测
    『chapter delta is 4302 bytes; hard cap is 3072』）。

    压缩不丢信息：完整快照数据在 character_snapshots 段有独立预算随事务提交
    （render_snapshot，SNAPSHOT_MAX_BYTES=8192），delta.character_changes.change
    里只需保留「本章出场」事实。LLM 已改写的条目（无占位标记）原样返回。"""
    if "【状态检查】该角色本章出场。当前 location=" in change:
        compressed = _STATE_CHECK_PLACEHOLDER_RE.sub("【状态检查】本章出场", change)
        # 【草稿】是 draft 标记不是语义，顺手剥掉（同 0b 段草稿回补路径的既有先例）
        return compressed.replace("【草稿】", "").strip()
    return change


# 段落协议契约来源：nww_text.split_paragraphs:87 的 blank 分支（字面正则 + strip + 过滤空块）。
# ★ 复刻义务：nww_text 若改切段口径，此处必须同步。line 模式（单换行分段）不做拆段修复
#   ——归一才是正道（gate 语义），修复循环由 post_write 的 gate 复检兜底仲裁。
_PARA_BLANK_SPLIT_RE = re.compile(r"\n\s*\n")
# gate 阈值契约来源：nww_paragraph_gate.py:72-81（J2 墙 120 / J1 BLOCK 220 / 连片 70×3）
_PARA_J2_WALL_CHARS = 120
_PARA_J1_BLOCK_CHARS = 220
_PARA_J2_MIN_EACH = 70


def _split_wall_at_sentence(para: str) -> tuple[str, str] | None:
    """段内在句末标点近中点处二分。切点须距两端 ≥15 字（拆出太碎的半没意义）。"""
    ends = [m.end() for m in re.finditer(r"[。！？][」』”』)）…]?", para)]
    if not ends:
        return None
    mid = len(para) / 2
    best = min(ends, key=lambda e: abs(e - mid))
    if best < 15 or best > len(para) - 15:
        return None
    return para[:best], para[best:]


def _fix_paragraph_walls(prose: Path) -> list[str]:
    """paragraph_gate J1/J2 墙段的确定性拆段修复（2026-09-22 新增）。

    L3 重写实测：contract_guard/commit 全过后，post_write 的 paragraph_gate
    报 J2_long_chain（连续 3 段每段>70 且含 120+ 墙）BLOCK —— Writer 产出有
    段落墙是 LLM 概率性行为，重写链此前无修复路径，整链在最后一环失败。

    修复语义：只拆段落边界、**绝不增删改任何非空白字符**（同 nww_text 铁律）。
    J2：拆 run 内 >120 字的墙段（墙没了 → 连片条件破）；
    J1：拆 >220 字单段（拆到每片 ≤120，覆盖 ≤330 字段，更巨的由 gate 复检兜底）。
    原位正则替换（不动块外任何字节），修复后由重跑 post_write 的 gate 仲裁。"""
    text = prose.read_text(encoding="utf-8")
    blocks = [b.strip() for b in _PARA_BLANK_SPLIT_RE.split(text) if b.strip()]
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    # line 模式判定同 nww_text:88-94：单换行分段不做拆段（归一才是正道）
    if len(lines) >= 3 and (len(blocks) < 3 or len(lines) > 4 * len(blocks)):
        return []
    lens = [len(p) for p in blocks]
    # J2 连片窗口（复刻 nww_paragraph_gate.py:160-163：每段>70 且窗口含 120+ 墙）
    hit_windows = [
        i for i in range(len(lens) - 2)
        if all(x > _PARA_J2_MIN_EACH for x in lens[i:i + 3]) and max(lens[i:i + 3]) > _PARA_J2_WALL_CHARS
    ]
    walls = {j for i in hit_windows for j in range(i, i + 3) if lens[j] > _PARA_J2_WALL_CHARS}
    # J1 单段超长
    walls |= {i for i, x in enumerate(lens) if x > _PARA_J1_BLOCK_CHARS}
    if not walls:
        return []
    fixed: list[str] = []
    new_text = text
    for idx in sorted(walls):
        para = blocks[idx]
        # 迭代拆片到每片 ≤120（墙段拆一次即可破 J2；J1 巨段最多拆成 3 片）
        pieces = [para]
        while len(pieces) < 3 and any(len(p) > _PARA_J2_WALL_CHARS for p in pieces):
            k = next(i for i, p in enumerate(pieces) if len(p) > _PARA_J2_WALL_CHARS)
            halves = _split_wall_at_sentence(pieces[k])
            if not halves:
                break
            pieces[k:k + 1] = list(halves)
        if len(pieces) == 1:
            continue
        # 原位替换：只动这一块，块外字节零变化（para 段内无换行，M 模式行级匹配）
        pattern = re.compile(r"^[ \t]*" + re.escape(para) + r"[ \t]*$", re.M)
        new_text, n_sub = pattern.subn(lambda m: "\n\n".join(pieces), new_text, count=1)
        if n_sub:
            fixed.append(f"段{idx + 1}({lens[idx]}字→{len(pieces)}片)")
    if fixed:
        prose.write_text(new_text, encoding="utf-8")
    return fixed


def _enforce_para_max120(prose: Path) -> list[str]:
    """段落复检（2026-09-22 方案A 新增）：wordcount 后按「最长段 ≤120」验收口径
    复检全部段落，任何 >120 的孤立长段直接确定性拆段。

    缘由（38 章实测）：post_write 的 paragraph_gate BLOCK 线是 J1=220 / J2=连片
    （每段>70 且含 120+ 墙），孤立长段（>120、<220、不成连片）会穿过 gate 定型
    （38 章 12:09 重写产物：均 61 字、最长 172、3 段>120——gate 判 PASS 未拆段）。
    gate 本体属 detection 红线不可改，故在重写链 wordcount 之后加本复检兜住口径差。

    拆段原语与 _fix_paragraph_walls 完全同源（_split_wall_at_sentence 近中点句末
    二分 + 原位正则替换），同样零文字损失；每段最多拆成 4 片（覆盖 ≤480 字段），
    切不出句末的段跳过（极端无标点巨段留给 gate J1 兜底）。"""
    text = prose.read_text(encoding="utf-8")
    blocks = [b.strip() for b in _PARA_BLANK_SPLIT_RE.split(text) if b.strip()]
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    # line 模式判定同 _fix_paragraph_walls（nww_text:88-94）：单换行分段不做拆段
    if len(lines) >= 3 and (len(blocks) < 3 or len(lines) > 4 * len(blocks)):
        return []
    walls = [i for i, p in enumerate(blocks) if len(p) > _PARA_J2_WALL_CHARS]
    if not walls:
        return []
    fixed: list[str] = []
    new_text = text
    for idx in walls:
        para = blocks[idx]
        pieces = [para]
        while len(pieces) < 4 and any(len(p) > _PARA_J2_WALL_CHARS for p in pieces):
            k = next(i for i, p in enumerate(pieces) if len(p) > _PARA_J2_WALL_CHARS)
            halves = _split_wall_at_sentence(pieces[k])
            if not halves:
                break
            pieces[k:k + 1] = list(halves)
        if len(pieces) == 1:
            continue
        # 原位替换：只动这一块，块外字节零变化（同 _fix_paragraph_walls 模式）
        pattern = re.compile(r"^[ \t]*" + re.escape(para) + r"[ \t]*$", re.M)
        new_text, n_sub = pattern.subn(lambda m: "\n\n".join(pieces), new_text, count=1)
        if n_sub:
            fixed.append(f"段{idx + 1}({len(para)}字→{len(pieces)}片)")
    if fixed:
        prose.write_text(new_text, encoding="utf-8")
    return fixed


# 对话引号跨度（全角弯引号与直引号双兼容）；捕获组=说话内容
_DIALOGUE_SPAN_RE = re.compile(r'["\u201c]([^"\u201d]{1,300})["\u201d]')


def _enforce_dialogue_split(prose: Path) -> list[str]:
    """对话独立成段复检（2026-09-22 新增）：「叙述+对话」混合段按引号边界确定性拆分。

    用户裁决的网文格式标准：说话内容（双引号内）必须另起一段。混合段
    「老宋头压低声音："这事儿，你知我知。"说完他把纸角塞回袖口。」拆为
    引语叙述段 / 对话段 / 尾部叙述段三段，零文字损失（只加空行）。

    规则（保守 fail-safe）：
    - 只拆「以 ≥2 字非引号叙述开头且含对话」的段；纯对话段（以引号开头）不动；
    - 引号必须成对，捕获内容 ≤300 字；不成对/嵌套异常段跳过；
    - 引号内含引号嵌套（内层单引号）不受影响（跨度只认双引号字符）；
    - line 模式（单换行分段）不拆，同 paragraph_120 判定。"""
    text = prose.read_text(encoding="utf-8")
    blocks = [b.strip() for b in _PARA_BLANK_SPLIT_RE.split(text) if b.strip()]
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) >= 3 and (len(blocks) < 3 or len(lines) > 4 * len(blocks)):
        return []
    fixed: list[str] = []
    new_text = text
    for para in blocks:
        if para.startswith(("#", "---", "<")):
            continue
        # 段内引号成对性核对（不成对=嵌套/异常，跳过）：
        # 弯引号开闭计数必须相等；直引号总数须为偶数（每对贡献 1 个跨度）
        spans = list(_DIALOGUE_SPAN_RE.finditer(para))
        if not spans:
            continue
        n_open = para.count("\u201c")
        n_close = para.count("\u201d")
        n_straight = para.count('"')
        if n_open != n_close or n_straight % 2:
            continue
        if n_open + n_straight // 2 != len(spans):
            continue
        # 前缀叙述 <2 字（纯对话段/单字引导）不拆
        first_open = spans[0].start()
        prefix = para[:first_open]
        if len(re.sub(r"[：:\s]", "", prefix)) < 2:
            continue
        # 按引号边界切块：叙述0 | 对话1 | 叙述1 | 对话2 | … 全部独立成段
        pieces: list[str] = []
        cursor = 0
        for m in spans:
            if para[cursor:m.start()].strip():
                pieces.append(para[cursor:m.start()].strip())
            pieces.append(m.group(0))
            cursor = m.end()
        if para[cursor:].strip():
            pieces.append(para[cursor:].strip())
        if len(pieces) < 2:
            continue
        pattern = re.compile(r"^[ \t]*" + re.escape(para) + r"[ \t]*$", re.M)
        new_text, n_sub = pattern.subn(lambda m: "\n\n".join(pieces), new_text, count=1)
        if n_sub:
            fixed.append(f"段({len(para)}字→{len(pieces)}段)")
    if fixed:
        prose.write_text(new_text, encoding="utf-8")
    return fixed


def _estimate_delta_payload_bytes(delta: dict, chapter_id: int, title: str = "") -> int:
    """commit 渲染字节的保守估算（同构契约：nww_track_commit.render_delta:1441-1538）。

    仅用于提交前预算预检：目标 < 2900B（为硬限 3072B 留 172B 余量，title 用真实值，
    零头段标题/空行/小段按 240B 计），commit 仍是最终仲裁。
    行构造公式与 render_delta 一一对应（其余字段逐行累加）。"""
    def _b(s: object) -> int:
        return len(str(s).encode("utf-8"))
    total = _b(f"# 第{chapter_id:03d}章 · {title}")
    total += _b(f"- 结果：{delta.get('result') or ''}") + 1
    total += _b("- 下一章承诺：" + ("；".join(delta.get("next_chapter_commitments") or []) or "无")) + 1
    for it in delta.get("character_changes") or []:
        if isinstance(it, dict):
            total += _b(f"- {it.get('name')}｜核心｜{it.get('change')}") + 1
    for it in delta.get("foreshadow_changes") or []:
        if not isinstance(it, dict):
            continue
        if it.get("action") == "delete":
            total += _b(f"- {it.get('id')}｜删除当前登记") + 1
        else:
            planned = f"第{it.get('planned_resolution_chapter')}章" if it.get("planned_resolution_chapter") else "未定"
            total += _b(f"- {it.get('id')}｜{it.get('status')}｜{it.get('summary')}｜回收{planned}") + 1
    for it in delta.get("timeline_events") or []:
        if not isinstance(it, dict):
            continue
        if it.get("action") == "delete":
            total += _b(f"- {it.get('id')}｜删除当前登记") + 1
        else:
            total += _b(
                f"- {it.get('id')}｜{it.get('story_time')}｜事实：{it.get('objective_fact')}"
                f"｜读者：{it.get('reader_knowledge')}｜{it.get('reveal_status')}"
            ) + 1
    for c in delta.get("constraints") or []:
        total += _b(f"- {c}") + 1
    total += 240  # 零头：6 个段标题 + 空行 + 代价/质量元数据/退役/事实契约小段
    return total


def _repair_delta(
    filled: dict,
    draft: dict,
    prose_text: str,
    chapter_id: int,
    *,
    existing_foreshadow_ids: set[str] | None = None,
    existing_timeline_ids: set[str] | None = None,
    append_mode: bool = True,
    title: str = "",
) -> dict:
    """delta 语义补全的确定性加固层（纯本模块防御，不改任何 skill 脚本）。

    契约来源：nww_track_commit.verify_delta（next_chapter_commitments 为
    字符串数组；result 不得为【草稿】占位）。LLM 输出不可信 → 强制归一。
    """
    out = dict(filled)
    # 0. 顶层投影：只保留 commit 白名单内的键。
    #    （原实现是 `out.pop("global_change", None)` —— 只堵一个已知洞。global_change 是
    #    transaction **根级**字段、不在 delta 白名单里，投影后自然被丢弃，语义不变。）
    out = {key: value for key, value in filled.items() if key in _DELTA_ALLOWED}

    # 0b. character_changes：元素契约 {name, change}（commit:1102）。
    #     LLM 历史上用过 character 别名（实测报错 `delta.character_changes[0] contains
    #     unsupported fields: character`），也会用 description/summary 顶替 change。
    #     ★ 只做**形状归一**，不做权威收敛：草稿的 character_changes 只由 _POWER_GAIN_RE
    #     命中填充（nww_delta_draft.py:94-98），实测第030/031章均为 []；LLM 从零补出
    #     「非境界类」角色变化（关系/立场/认知）是本链路的正常路径，砍掉会丢真实信息。
    _NAME_KEYS = ("name", "character", "character_name", "role")
    _TEXT_KEYS = ("change", "description", "summary", "detail")
    _seen: set[str] = set()
    _changes: list[dict] = []
    for _raw in out.get("character_changes") or []:
        if not isinstance(_raw, dict):
            continue
        _name = next((str(_raw[_k]).strip() for _k in _NAME_KEYS if str(_raw.get(_k) or "").strip()), "")
        _text = next((str(_raw[_k]).strip() for _k in _TEXT_KEYS if str(_raw.get(_k) or "").strip()), "")
        if not _name or not _text or _name in _seen:
            continue
        _seen.add(_name)
        # LLM 保留【状态检查】占位誊写时确定性压缩（根因见 _compress_state_check 文档）
        _changes.append({"name": _name, "change": _clip_utf8(_compress_state_check(_text), _CHANGE_MAX_BYTES)})
    # 草稿已判定的角色变化不能被 LLM 丢干净：commit 要求
    # set(character_snapshots) ⊆ {character_changes.name}（:1136-1139），而快照由草稿按自己的
    # character_changes 生成（nww_delta_draft.py:105-108）→ 名字丢了就必然 BLOCK。补并集。
    for _base in draft.get("character_changes") or []:
        if not isinstance(_base, dict):
            continue
        _name = str(_base.get("name") or "").strip()
        if not _name or _name in _seen:
            continue
        _seen.add(_name)
        _text = str(_base.get("change") or "").strip().replace(_DRAFT_TAG, "").strip()
        # 同上：草稿占位誊写确定性压缩（占位语义是快照誊写，非变化描述）
        _text = _compress_state_check(_text)
        # change 不能为空（commit: clean_text 非空断言）→ 兜底文案，同 :810-812 的既有先例
        _changes.append({
            "name": _name,
            "change": _clip_utf8(_text or f"第{chapter_id}章：{_name}的状态推进（待人工补充）", _CHANGE_MAX_BYTES),
        })
    out["character_changes"] = _changes

    # 1. next_chapter_commitments：任何非字符串元素 → 抽取/丢弃，强制纯字符串数组
    ncc = out.get("next_chapter_commitments") or draft.get("next_chapter_commitments") or []
    norm: list[str] = []
    for item in ncc if isinstance(ncc, list) else [ncc]:
        if isinstance(item, dict):
            item = item.get("commitment") or item.get("text") or item.get("content") or ""
        if isinstance(item, str) and item.strip():
            norm.append(item.strip())
    # 契约：默认放行空数组；若草稿/LLM 都没写，不伪造
    out["next_chapter_commitments"] = norm or []

    # 2. result：禁【草稿】占位——仍占位时取正文前两行作为确定性兜底
    #    ★ 必须按「字节」裁剪：commit 契约是 nww_track_commit.clean_text(max_bytes=480)
    #    （见该文件 "delta.result"），而中文 1 字 = 3 字节。旧实现 `[:240]` 截的是
    #    240 个**字符** = 716 字节 → 恒定超出 480 → commit 必 BLOCK
    #    （2026-09-13 实测：《用泡面征服修仙界》第27章重写，enforce/paragraph_gate 全 PASS，
    #    卡在 `ERROR: delta.result exceeds 480 byte`）。
    result = str(out.get("result") or "")
    if _DRAFT_TAG in result or not result.strip():
        fallback = "\n".join(
            ln.strip() for ln in prose_text.splitlines() if ln.strip() and _DRAFT_TAG not in ln
        )
        out["result"] = _clip_utf8(fallback or result, _RESULT_MAX_BYTES)
    else:
        # LLM 自己也可能写出超长 result，同一口径裁剪，别把这个 BLOCK 留给 commit
        out["result"] = _clip_utf8(result, _RESULT_MAX_BYTES)

    # 3. foreshadow_changes / timeline_events：白名单字段过滤 + 契约缺省兜底。
    #    契约来源：nww_track_commit.normalize_foreshadow_change / normalize_timeline_change
    #    （require_known_keys——LLM 输出 type/content 等非法字段会直接 BLOCK commit）
    _FORESHADOW_KEYS = {
        "action", "id", "summary", "planted_chapter", "planned_resolution_chapter",
        "status", "importance", "core_hook", "half_life_chapters", "depends_on", "parent_id",
    }
    _TIMELINE_KEYS = {
        "action", "id", "story_time", "objective_fact", "reader_knowledge",
        "reveal_status", "reveal_chapter", "characters",
    }
    # LLM 常用简化状态（planted/deepened…）映射到契约枚举；未知 → "已埋"（本章关联即视为埋设/加深）
    _FS_STATUS_MAP = {
        "planted": "已埋", "deepened": "已埋", "new": "已埋", "used": "已埋",
        "resolved": "已回收", "closed": "已回收",
        "retired": "放弃", "expired": "已过期",
    }
    _FS_STATUS_VALID = {"已埋", "已回收", "已过期", "放弃"}
    _FS_IMPORTANCE_VALID = {"高", "中", "低"}
    _TL_REVEAL_VALID = {"未揭示", "部分揭示", "已揭示"}
    for field, allowed in (("foreshadow_changes", _FORESHADOW_KEYS), ("timeline_events", _TIMELINE_KEYS)):
        items = out.get(field) or []
        if not isinstance(items, list):
            continue
        kept: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            norm = {k: v for k, v in item.items() if k in allowed}
            if field == "foreshadow_changes" and norm.get("action") != "delete":
                # 契约强制字段兜底：planted_chapter(int)/status/importance/summary 缺省或非法一律归一，
                # 杜绝 "planted_chapter must be an integer" 类 BLOCK；合法值保留原样。
                planted = norm.get("planted_chapter")
                norm["planted_chapter"] = planted if isinstance(planted, int) and planted >= 1 else chapter_id
                status = norm.get("status")
                norm["status"] = status if status in _FS_STATUS_VALID else _FS_STATUS_MAP.get(status, "已埋")
                importance = norm.get("importance")
                norm["importance"] = importance if importance in _FS_IMPORTANCE_VALID else "中"
                # 2026-09-15 根治（对齐 timeline 的 E 逻辑）：foreshadow id 必须 `F\d{3,}`。
                # LLM 自造 `F_OLD_BOX` 这类语义 id 会让 commit 的
                # `delta.foreshadow_changes[i].id must look like F001` 硬拒（第28章实测）。
                # 合法 id 原样保留；非法 id 分配**避开既有 id** 的新编号，绝不覆盖/分裂既埋伏笔。
                #   - 注意与 timeline 的差异：foreshadow id 是 state.foreshadow 的**键**，
                #     若命中既有伏笔（LLM 想更新 F005 却写错）本应归位到原 id，但此处无可靠映射
                #     （_draft_hints.active_foreshadow_mentioned 仅含 `F\d+` 合规提及，F_OLD_BOX 不在其中），
                #     故保守分配新号，语义由 summary 保留、id 契约自洽。
                identifier = str(norm.get("id") or "").strip()
                if re.fullmatch(r"^F\d{3,}$", identifier):
                    norm["id"] = identifier
                else:
                    # 分配新号时**同时避开**既埋伏笔 id 与本事务已保留的合法 id，
                    # 防止同一事务内 F_OLD_BOX 与既有 F001 同时出现时撞号成两条 F001。
                    _taken = {str(i.get("id")) for i in kept if str(i.get("id") or "").startswith("F")} \
                        | set(existing_foreshadow_ids or ())
                    _n = 1
                    while f"F{_n:03d}" in _taken:
                        _n += 1
                    norm["id"] = f"F{_n:03d}"
                norm.setdefault("action", "upsert")
                norm.setdefault("summary", f"伏笔 {norm.get('id', '')} 本章关联更新")
                norm.setdefault("core_hook", False)
                norm.setdefault("half_life_chapters", None)
                norm.setdefault("depends_on", [])
                norm.setdefault("parent_id", None)
                norm.setdefault("planned_resolution_chapter", None)
            if field == "timeline_events" and norm.get("action") != "delete":
                # 契约兜底：id 必须 E\d{3,}；reveal_status 必填；
                # 未揭示 → reveal_chapter=None；部分/已揭示 → reveal_chapter 缺省补当前章。
                # ★ A-7 撞号防线（2026-09-16 修复：所有模式统一避让 state 既有 id）：
                #   根因：LLM 无视 next_timeline_id 提示从 E001 起编 → ch29-32 delta 全是 E001/E002/E003。
                #   原逻辑只在 append_mode 下重编（revision/backfill 撞号直接 upsert 覆盖历史 fact）。
                #   新逻辑：所有模式下，id 只要撞了 state 既有 id 就重编；
                #   本事务内部已分配 id 也避让（防 commit 重复 id 硬拒）。
                identifier = str(norm.get("id") or "")

                def _renumber_tl() -> str:
                    # 所有模式统一：从 state 最大编号 + 1 开始分配
                    _state_max = max(
                        (int(i[1:]) for i in (existing_timeline_ids or [])
                         if re.fullmatch(r"E\d{3,}", i)),
                        default=0,
                    )
                    # 避让 state 既有 id ∪ 本事务已分配 id
                    _taken = set(existing_timeline_ids or []) | {str(i.get("id")) for i in kept}
                    _n = _state_max + 1
                    while f"E{_n:03d}" in _taken:
                        _n += 1
                    return f"E{_n:03d}"

                if re.fullmatch(r"E\d{3,}", identifier):
                    _occupied = (set(existing_timeline_ids or [])
                                 | {str(i.get("id")) for i in kept})
                    # 统一撞号防线：只要撞了 state 既有 id 就重编（所有模式）；
                    # 本事务内部已分配 id 也避让（防 commit 重复 id 硬拒）。
                    norm["id"] = _renumber_tl() if identifier in _occupied else identifier
                else:
                    norm["id"] = _renumber_tl()
                rv = norm.get("reveal_status")
                norm["reveal_status"] = rv if rv in _TL_REVEAL_VALID else "未揭示"
                if norm["reveal_status"] == "未揭示":
                    norm["reveal_chapter"] = None
                else:
                    rc = norm.get("reveal_chapter")
                    norm["reveal_chapter"] = rc if isinstance(rc, int) and rc >= 1 else chapter_id
                norm.setdefault("action", "upsert")
                norm.setdefault("story_time", f"第{chapter_id}章事件")
                norm.setdefault("objective_fact", f"第{chapter_id}章情节推进（细节待人工补充）")
                norm.setdefault("reader_knowledge", f"读者获知第{chapter_id}章发生之事")
                norm.setdefault("characters", [])
            kept.append(norm)
        out[field] = kept

    # 4. cost_change：契约 {device, count}（nww_track_commit.normalize_cost_change:411）。
    #    LLM 只给 device 不给 count 会 BLOCK（`count must be an integer`）→ 形状不合法
    #    一律退回 None（= 本章无代价变化），不替它猜数字。
    _cost = out.get("cost_change")
    if _cost is not None:
        _device = str((_cost.get("device") if isinstance(_cost, dict) else "") or "").strip()
        _count = _cost.get("count") if isinstance(_cost, dict) else None
        out["cost_change"] = (
            {"device": _device, "count": _count}
            if _device and isinstance(_count, int) and not isinstance(_count, bool) and _count >= 0
            else None
        )

    # 5. 列表条数上限：commit 是硬拒（clean_string_list maximum=），这里截断而不是让它 BLOCK
    for _key, _cap in _DELTA_LIST_CAPS.items():
        _value = out.get(_key)
        if isinstance(_value, list) and len(_value) > _cap:
            out[_key] = _value[:_cap]

    # 6. 总量预算兜底（2026-09-22 新增，第38章 L3 重写实测根因）：commit 渲染 delta 摘要
    #    有 DELTA_MAX_BYTES=3072 硬上限（nww_track_commit.render_delta:1538），信息密集章
    #    （多条 timeline + 伏笔 + 全员角色变化）会超限 BLOCK——且 BLOCK 发生在 post_write
    #    末端，前面 enforce/paragraph_gate/contract_guard 全部白跑。此处按同构口径估算
    #    （_estimate_delta_payload_bytes，预算 2900B 为硬限留 172B 余量），超预算时先做
    #    3 轮递减文本收敛（只压长度、不删条目），仍超则截断尾部条目——与 commit 侧
    #    clean_string_list maximum 的既有容量截断同语义（第 5 步先例）。
    #    CC 占位誊写在第 0b 步已压缩，轮 1 的 60B 针对的是 LLM 写超长描述的场景。
    _budget_rounds = [
        # (cc_change, tl_story, tl_fact, tl_reader, fs_summary)
        (60, 60, 150, 120, 120),
        (30, 36, 90, 75, 75),
        (18, 24, 60, 45, 50),
    ]
    for _cc_cap, _story_cap, _fact_cap, _reader_cap, _fs_cap in _budget_rounds:
        if _estimate_delta_payload_bytes(out, chapter_id, title) <= 2900:
            break
        for it in out.get("character_changes") or []:
            if isinstance(it, dict):
                it["change"] = _clip_utf8(str(it.get("change") or ""), _cc_cap)
        for it in out.get("timeline_events") or []:
            if isinstance(it, dict) and it.get("action") != "delete":
                it["story_time"] = _clip_utf8(str(it.get("story_time") or ""), _story_cap)
                it["objective_fact"] = _clip_utf8(str(it.get("objective_fact") or ""), _fact_cap)
                it["reader_knowledge"] = _clip_utf8(str(it.get("reader_knowledge") or ""), _reader_cap)
        for it in out.get("foreshadow_changes") or []:
            if isinstance(it, dict) and it.get("action") != "delete":
                it["summary"] = _clip_utf8(str(it.get("summary") or ""), _fs_cap)
    if _estimate_delta_payload_bytes(out, chapter_id, title) > 2900:
        # 终极兜底：文本已压到轮 3 预算仍超 → 截断尾部条目（容量契约所迫，
        # 与 clean_string_list maximum 超条数即截断同语义）+ 收紧 result/constraints
        out["result"] = _clip_utf8(str(out.get("result") or ""), 180)
        if isinstance(out.get("constraints"), list) and len(out["constraints"]) > 3:
            out["constraints"] = out["constraints"][:3]
        for _field in ("timeline_events", "foreshadow_changes"):
            if not isinstance(out.get(_field), list):
                continue
            while _estimate_delta_payload_bytes(out, chapter_id, title) > 2900:
                _items = [it for it in out.get(_field) or []
                          if isinstance(it, dict) and it.get("action") != "delete"]
                if not _items:
                    break
                out[_field].remove(_items[-1])
    return out


def _reconcile_snapshots(tx: dict, book: Path) -> None:
    """给 character_changes 里「已有快照的核心角色」补齐所缺的当前快照。

    为什么必须补（2026-09-14 新增，实测可达）：commit 有两条硬约束——
      ① nww_track_commit.normalize_delta:1107-1110 —— 核心角色（已在 state.characters 或已有
         快照）发生变化，必须在本事务里提交当前快照，否则
         `ERROR: core character X changed but has no current snapshot`；
      ② 同函数 :1136-1139 —— set(character_snapshots) ⊆ {character_changes.name}。
    而后端**从不写 character_snapshots**（web/backend 全仓 grep 0 命中），它只来自草稿，
    且草稿按**自己的** character_changes 生成（nww_delta_draft.py:105-108）。草稿的
    character_changes 只由 _POWER_GAIN_RE 命中填充、经常为空（实测第030/031章均为 []），
    于是 LLM 补出的角色变化没有配套快照 → 必 BLOCK。

    补的是 state 里 current snapshot 的**原样副本**：与草稿同一来源、同一形态，
    在 merge_transaction 里是幂等写回（`next_state["characters"].update(snapshots)`），
    不改任何语义，只让契约自洽。

    ★ 临时角色（不在 state.characters）**不注入**：空快照反而触发
      `ERROR: character_snapshots.周执事.identity must be a string`，而且它们本来就不需要
      快照（normalize_delta 视 is_core=False，仅登记为临时角色）。
    ★ fail-soft：读不到 state 就跳过，绝不让本环节把整轮 pipeline 打挂。
    """
    state_file = book / _TRACKING_DIR / "_tracking-state.json"
    try:
        existing = (json.loads(state_file.read_text(encoding="utf-8")) or {}).get("characters") or {}
    except Exception:  # noqa: BLE001 — 状态缺失不是本环节该负责的事
        return
    if not isinstance(existing, dict):
        return
    snaps = tx.get("character_snapshots")
    if not isinstance(snaps, dict):
        snaps = {}
        tx["character_snapshots"] = snaps
    for item in (tx.get("delta") or {}).get("character_changes") or []:
        name = str(item.get("name") or "").strip() if isinstance(item, dict) else ""
        if name and name not in snaps and name in existing:
            snaps[name] = json.loads(json.dumps(existing[name], ensure_ascii=False))


def step_post_write(book: Path, chapter_id: int, prose: Path, skip_enforce: bool = False) -> dict:
    """6. 追踪提交 + 机械层 + 跨章检测（BLOCK 点）。

    ★ 2026-10-03 外脑裁定：pending ledger 的生命周期必须绑定到 **commit 成功这一事实本身**，
    而不是「调用方恰好走了某条 pipeline 包装层」。此前 `_clear_pending_commit_ledger`
    只散落在 4 条链的包装层与 `_try_auto_commit_dangling` 里 → 实测存在
    「commit SUCCESS（state_revision +1）但 pending ledger 仍处于 pending」的
    事务后置状态不一致（实际已提交 vs 台账仍显示未提交）。
    这里是所有 Python 侧 commit 的唯一收口，故把闭合放在此处；各链包装层的调用保留为冗余兜底。

    skip_enforce（P0-3a，2026-10-19 外脑裁决）：enforce 已由 step_pre_review_enforce
    前移到 review_cycle 之前执行时置 True，post_write 内不再重复跑（避免双跑），
    meta.enforced 由前移步骤写入。缺省 False → 与历史行为完全一致。
    """
    args = ["--project", str(book), "--chapter", str(chapter_id), "--prose", str(prose), "--json"]
    if skip_enforce:
        args.append("--skip-enforce")
    res = toolbox.run_script(
        "nww_post_write.py",
        args,
        book,
        timeout=300,
    )
    # commit 成功 → pending ledger 闭合（fail-soft，绝不影响提交结果本身）
    if isinstance(res, dict) and res.get("ok"):
        _clear_pending_commit_ledger(book, chapter_id)
    return res


# ---- 悬空章验收补跑（2026-11-04 用户裁决：系统自己按流程解决，达到过闸标准） ----
# 语义：正文已落盘但 last_committed_chapter 落后 = 这一章「写完了、没走完验收」。
# 系统不叫停、不放行、不降标准，而是把写章链缺的验收环节按原顺序补跑一遍：
#   enforce(真跑) → 审稿闭环(真打分/真 finalize/真写 reviewed) → 一致性检测+自动闭环
#   → post_write 按**原闸标准**正式 commit。
# 于是 delta 真的具备 enforced/reviewed，A-1~A-5 全部按原标准过闸，游标与 state_revision
# 正常推进，悬空自然消失。补跑仍不达标 → 如实返回「哪一步没达标、系统做了什么」，绝不伪造。
_BODY_SETTLE_SWITCH = "NWW_BODY_SETTLE"


def _body_settle_enabled() -> bool:
    return os.environ.get(_BODY_SETTLE_SWITCH, "1") != "0"


def _committed_chapter(book: Path) -> int:
    try:
        data = json.loads((book / _TRACKING_DIR / "_tracking-state.json").read_text(encoding="utf-8"))
        return int(data.get("last_committed_chapter") or 0)
    except Exception:
        return 0


def _delta_semantic_missing(delta: object) -> bool:
    """C-ACCEPT-DELTA@1：delta 语义是否仍为占位（判 True = 需要补语义）。

    保守判定（任一成立即视为需要补）：
      - 非 dict；
      - 任意字段仍含【草稿】占位标记；
      - result 为空；
      - chapter_type 为 None 或空串。
    语义完整 → False，调用方零 LLM 调用（R3-AC2）。
    """
    if not isinstance(delta, dict):
        return True
    try:
        if _DRAFT_TAG in json.dumps(delta, ensure_ascii=False):
            return True
    except (TypeError, ValueError):
        # 不可序列化 → 无法确认语义完整，保守判为需要补
        return True
    if str(delta.get("result") or "").strip() == "":
        return True
    if delta.get("chapter_type") is None:
        return True
    if str(delta.get("chapter_type") or "").strip() == "":
        return True
    return False


def _delta_quality_meta_ready(draft_path: Path) -> tuple[bool, str]:
    """提交前质量元完整性检查（T10，2026-11-07）：delta.quality_meta 是否已冻结交付身份。

    真实 finalize 通过时由 nww_review_cycle._write_review_meta 写入：
      - reviewed（审稿已收口）
      - reviewed_hash（被审 best 快照 hash）
      - final_hash（落盘冻结后的正文 hash）
    best 轮未通过承诺闸/维度地板闸时，finalize 只恢复 best 快照、**不写**上述哈希，
    因此这里缺任一字段即说明「审稿 finalize 未收口」，不得进入 commit（否则 A-5 会把
    审稿未收口误报成正文身份不符）。

    返回 (ready, reason)；读取/解析异常一律按不满足处理（保守，不提交）。
    """
    try:
        raw = json.loads(draft_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, f"delta 读取/解析失败（{exc}）"
    tx = raw.get("transaction") if isinstance(raw, dict) else None
    if not isinstance(tx, dict):
        tx = raw if isinstance(raw, dict) else {}
    delta = tx.get("delta") if isinstance(tx, dict) else None
    qm = delta.get("quality_meta") if isinstance(delta, dict) else None
    if not isinstance(qm, dict):
        return False, "delta 缺 quality_meta"
    if qm.get("reviewed") is not True:
        return False, "quality_meta.reviewed 非 True"
    missing = [k for k in ("reviewed_hash", "final_hash")
               if not str(qm.get(k) or "").strip()]
    if missing:
        return False, "quality_meta 缺 " + "/".join(missing)
    return True, ""


def step_acceptance_and_commit(book: Path, chapter_id: int, *, reason: str = "",
                               by: str = "system_autosettle",
                               max_review_attempts: int = 2,
                               progress=None) -> dict:
    """给「正文已落盘但未提交」的章补跑完整验收并按原闸标准正式提交。

    - 幂等：该章已在权威游标内 → no-op（正文回退到已提交版本，结果不变）。
    - 无正文成果 → 明确不处理（没有成果可验收，此时才是真失败）。
    - 不写 quality_meta 伪造标记：enforced/reviewed 全部由既有步骤真实产生。

    progress: 可选回调 `progress(stage_name, ok, detail)`，用于 AI 实况面板显示进度。
    """
    book = book.resolve()
    if not _body_settle_enabled():
        return {"ok": False, "committed": False, "detail": "验收补跑已关闭（NWW_BODY_SETTLE=0）"}
    prose = _resolve_existing_body(book, chapter_id)
    if prose is None:
        return {"ok": False, "committed": False, "detail": "本章无正文成果，无可验收提交"}
    last = _committed_chapter(book)
    if last >= chapter_id:
        return {"ok": True, "committed": False, "already_committed": True,
                "last_committed_chapter": last,
                "detail": f"第{chapter_id}章已在权威游标内（last={last}），正文结果不变"}
    draft = book / _TRACKING_DIR / f"{_DELTA_PREFIX}{chapter_id:03d}章.json"
    done: list[str] = []
    try:
        # ① 失败回滚会删 delta 草稿 → 确定性重建（不是伪造，是重新走预填）
        if progress:
            try:
                progress("acceptance_commit", None, f"补跑验收提交（第{chapter_id}章）：重建 delta 草稿…")
            except Exception:
                pass
        if not draft.is_file():
            step_delta_draft(book, chapter_id, prose, preserve_quality_meta=True)
            done.append("重建 delta 草稿")
        # ①.5 delta 语义补全（C-ACCEPT-DELTA@1）：重建的 delta 往往仍是【草稿】占位，
        #     必须在 ② enforce / ④ 审稿之前补齐语义，否则占位会随 post_write 写进追踪。
        #     语义完整时零 LLM 调用；补全失败或补后仍占位 → 拒绝提交（不伪造、不清台账）。
        def _draft_delta_now() -> object:
            """重新读盘解析 delta 语义体（不复用内存值）；解析失败按「缺失」处理。"""
            try:
                raw = json.loads(draft.read_text(encoding="utf-8")) or {}
            except (OSError, ValueError):
                return None
            tx = raw.get("transaction") if isinstance(raw, dict) else None
            if not isinstance(tx, dict):
                tx = raw if isinstance(raw, dict) else {}
            return tx.get("delta")

        if _delta_semantic_missing(_draft_delta_now()):
            if progress:
                try:
                    progress("delta_semantic", None, "delta 语义补全…")
                except Exception:
                    pass
            try:
                step_delta_semantic(book, chapter_id, draft, prose)
            except Exception as exc:  # noqa: BLE001 — 如实失败，绝不伪造
                return {"ok": False, "committed": False, "steps": done,
                        "detail": f"delta 语义补全失败：{exc}"}
            done.append("delta 语义补全")
            if progress:
                try:
                    progress("delta_semantic", True, "delta 语义补全完成")
                except Exception:
                    pass
            # 复核：重新读文件（不复用内存值），仍占位 → 拒绝提交
            if _delta_semantic_missing(_draft_delta_now()):
                return {"ok": False, "committed": False, "steps": done,
                        "detail": "delta 语义补全后 result/chapter_type 仍为【草稿】占位，拒绝提交"}
        # ② enforce 机械层真跑（写 quality_meta.enforced）
        if progress:
            try:
                progress("pre_review_enforce", None, "enforce 机械层清洗…")
            except Exception:
                pass
        step_pre_review_enforce(book, chapter_id, prose, draft)
        done.append("enforce 机械层")
        if progress:
            try:
                progress("pre_review_enforce", True, "enforce 机械层完成")
            except Exception:
                pass
        # ③ 一致性检测 + 清零自动闭环（A3 同构，已有闭环则幂等跳过）
        closure = book / _TRACKING_DIR / "一致性核验" / f"第{chapter_id:03d}章.json"
        need = True
        if closure.is_file():
            try:
                need = json.loads(closure.read_text(encoding="utf-8")).get("closed") is not True
            except Exception:
                need = True
        if need:
            if progress:
                try:
                    progress("consistency", None, "一致性检测 + 自动闭环…")
                except Exception:
                    pass
            cons = step_consistency_check(book, chapter_id, prose)
            cl = step_consistency_closure(book, chapter_id, cons)
            done.append("一致性检测+自动闭环" if cl.get("closed") else "一致性检测(未清零，不装假闭环)")
            if progress:
                try:
                    progress("consistency", True, f"一致性检测完成（闭环={cl.get('closed')}）")
                except Exception:
                    pass
        # ④ 审稿闭环真跑：分数/finalize/reviewed 全部由 nww_review_cycle 产生
        rv: dict = {}
        for attempt in range(1, max(1, max_review_attempts) + 1):
            if progress:
                try:
                    progress("review_cycle", None, f"审稿闭环第 {attempt}/{max_review_attempts} 次…")
                except Exception:
                    pass
            rv = step_review_cycle(book, chapter_id, prose, draft, None)
            if rv.get("ok") and rv.get("passed"):
                done.append(f"审稿闭环通过（第{attempt}次，最高分 {rv.get('best_score')}）")
                if progress:
                    try:
                        progress("review_cycle", True, f"审稿闭环通过（最高分 {rv.get('best_score')}）")
                    except Exception:
                        pass
                break
            done.append(f"审稿闭环第{attempt}次未达标（best={rv.get('best_score')}, ok={rv.get('ok')}）")
        else:
            _rv_detail = ("系统已自动补跑验收并重试，审稿闭环仍未达过闸标准："
                          + str(rv.get("detail") or "未通过")[:300]
                          + "。已如实报告，不降标准、不伪造 reviewed。")
            if _gate().is_v13():  # type: ignore[attr-defined]
                # V13：审稿/评分属「质量事实」，不是产物有效性。判词原样留痕后
                # 继续走 ⑤ 原闸 commit（commit 侧 A-5 已按同一策略记录而不阻断）。
                _gate_record(book, chapter_id, "review_cycle/score", _rv_detail,
                             "acceptance_settle")
                done.append("审稿闭环未达标（V13 事实卡留痕，继续按流程提交）")
            else:
                return {"ok": False, "committed": False, "steps": done,
                        "detail": _rv_detail}
        # ④.5 提交前质量元完整性检查（T10，2026-11-07）：
        # 真实 finalize 收口成功（stdout「是否通过: 是」）时会写 reviewed/reviewed_hash/
        # final_hash；best 轮未通过承诺闸或维度地板闸时，finalize 只恢复 best 快照、
        # 不写这三个字段。若直接进 ⑤ commit，A-5 会把「审稿未收口」误报成
        # 「交付正文身份校验失败」（reviewed=final=旧 hash vs commit=新正文 hash）。
        # 故此处必须复核 delta.quality_meta，缺任一字段即拒绝提交：
        # 不调 post_write、不清台账、不写 reviewed（保持 finalize 的如实状态）。
        # 仅对「本次真实解析到 finalize 终态」的审稿结果生效；外部调用桩/旧调用方
        # 无 finalize_passed 字段 → 保留既有路径（回滚口，不引入新失败面）。
        if rv.get("finalize_passed") is True:
            _qm_ready, _qm_why = _delta_quality_meta_ready(draft)
            if not _qm_ready:
                if _gate().is_v13():  # type: ignore[attr-defined]
                    _gate_record(book, chapter_id, "A-5/quality_meta",
                                 "审稿 finalize 未收口：" + str(_qm_why),
                                 "acceptance_settle")
                    done.append("quality_meta 未收口（V13 事实卡留痕，继续按流程提交）")
                else:
                    return {
                        "ok": False, "committed": False, "chapter": chapter_id,
                        "steps": done,
                        "quality_meta_issue": _qm_why,
                        "detail": "审稿 finalize 未收口：delta 缺 reviewed/reviewed_hash/final_hash"
                                  "（best 轮未通过承诺或维度地板），未提交；"
                                  "请复核正文后重跑验收或重写该章",
                    }
        # ⑤ 原闸 commit（不跳过任何闸）
        post = step_post_write(book, chapter_id, prose, skip_enforce=True)
        ok = bool(isinstance(post, dict) and post.get("ok"))
        return {
            "ok": ok, "committed": ok, "chapter": chapter_id,
            "last_before": last, "last_after": _committed_chapter(book),
            "steps": done, "by": by, "reason": str(reason or "")[:300],
            "detail": ("系统补跑完整验收后按原闸标准正式提交，悬空已消除" if ok else
                       "补跑验收后 commit 仍未过闸：" + str(post.get("stderr") or "")[:400]),
            "post_write": {"ok": ok, "stdout": str(post.get("stdout") or "")[:1200],
                           "stderr": str(post.get("stderr") or "")[:600]},
        }
    except Exception as exc:
        return {"ok": False, "committed": False, "steps": done, "detail": f"验收补跑异常: {exc}"}


def _settle_fail(book: Path, chapter_id: int, stages: list, out: dict,
                 dry_run: bool = False) -> dict:
    """所有链失败出口统一收口：有正文成果 → 系统补跑验收 → 真过闸才标记流程完成。"""
    # C-SETTLEF@1 字段固定：所有返回路径恒含 outcome / degraded_pass / commit_status。
    # 未翻正取失败缺省（outcome 保留调用方已有值，缺省 "failed"）；翻正与验收修复分支
    # 在自己的赋值处覆盖为现值。ok=True 的防御性早退按成功语义取缺省。
    _settled_ok = bool(out.get("ok"))
    out.setdefault("outcome", "committed" if _settled_ok else "failed")
    out.setdefault("degraded_pass", False)
    out.setdefault("commit_status", "committed" if _settled_ok else "failed")
    if dry_run or not _body_settle_enabled() or _settled_ok:
        return out
    failed = [s.to_dict() for s in stages if not s.ok]
    reason = (failed[-1].get("detail") if failed else out.get("fatal_phase")) or "流程未完整收口"
    # ★ 2026-11-04 实跑假阳性教训：该章已在权威游标内时，本轮只是「对已提交章节的
    #   重写失败」（如 L3 Plan 校验 3/3 BLOCK，正文根本没生成）。此时无悬空可消除，
    #   补跑的 already_committed **不是本轮成功** —— 绝不能把 ok 翻成 True。
    #   否则前端会显示「✓ 全流程通过」，把早期失败伪装成成功。
    if _committed_chapter(book) >= chapter_id:
        out["acceptance_repair"] = {
            "ok": True, "committed": False, "already_committed": True,
            "detail": "该章此前已提交，本轮未产出新成果、无悬空；本轮失败如实保留",
        }
        return out
    res = step_acceptance_and_commit(book, chapter_id, reason=str(reason)[:600])
    out["acceptance_repair"] = res
    if res.get("committed"):
        out["ok"] = True
        out["outcome"] = "acceptance_repaired"
        out["degraded_pass"] = False
        out["warnings"] = [
            f"本轮环节未达标，系统已自动补跑完整验收并修正：{str(reason)[:300]}",
            "本章按原闸标准正式提交，未降低任何标准。",
        ]
    # V13 道闸（2026-11-13）：补跑仍未提交，但本轮确有正文成果、且该章尚未进入权威
    # 游标时，不把「闸未过」伪装成「动作失败」——按 V13 只报告事实、裁决权交作者。
    # C-SETTLEF@1 门槛（R4）：事实卡必须非空（count > 0）才允许翻正 ——
    # 卡为空表示没有任何闸留下了可裁决的事实，此时翻成功等于免检放行。
    # 双重护栏：① 该章已提交（旧正文还在）不得翻成功（11-04 假阳性教训）；
    #           ② 无正文成果不得翻成功（没有产物可交付）。
    if _gate().is_v13() and not out.get("ok") and _committed_chapter(book) < chapter_id \
            and _resolve_existing_body(book, chapter_id) is not None:  # type: ignore[attr-defined]
        try:
            card = _gate().report(book, chapter_id)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — 事实卡读失败不影响动作结果
            card = {"status": "empty", "count": 0, "findings": []}
        if int(card.get("count") or 0) > 0:
            out["ok"] = True
            out["outcome"] = "gates_reported"
            out["degraded_pass"] = True
            out["commit_status"] = "pending_author_review"
            out["gate_report"] = card
            out["warnings"] = [
                "V13 道闸：闸事实已记录，系统未替作者下结论；本轮动作与正文落盘已完成。",
                f"事实卡 {card.get('count', 0)} 条，详见 追踪/闸报告/第{chapter_id:03d}章.json。",
                f"补跑验收结果：{str((res or {}).get('detail') or '未提交')[:200]}",
            ]
        else:
            out["gate_report"] = card
            out["warnings"] = [
                "V13 道闸：事实卡为空（0 条），无裁决依据，本轮按失败如实返回。",
                f"补跑验收结果：{str((res or {}).get('detail') or '未提交')[:200]}",
            ]
    return out


# C-ENFORCE@1 回滚口（2026-11-13）：置 1 时机械层失败回到 advisory。
_ENFORCE_ADVISORY_SWITCH = "NWW_ENFORCE_ADVISORY"


def _enforce_advisory_enabled() -> bool:
    """C-ENFORCE@1 回滚口：NWW_ENFORCE_ADVISORY=1 → enforce 失败回到 WARNING 继续（V12 旧行为）。"""
    return os.environ.get(_ENFORCE_ADVISORY_SWITCH, "").strip().lower() in (
        "1", "true", "on", "yes",
    )


def step_pre_review_enforce(book: Path, chapter_id: int, prose: Path, draft_path: Path) -> dict:
    """P0-3a（2026-10-19 外脑裁决）：enforce 前移到 review_cycle 之前的独立执行步。

    enforce 会改写正文（引号风格/markdown 剥离/结尾泄漏清理）；原顺序（post_write
    Step1 enforce 在 finalize 之后）导致「审的是 X、交付的是 Y」（V4 实证，SHA-256
    X≠Y 而 commit 照收）。前移后 finalize 冻结的版本即交付版本，commit 侧 A-5
    三哈希校验才有意义。

    语义镜像 nww_post_write.py Step1：失败仅 WARNING 继续，且 meta 三键每轮无条件写——
    enforced=本轮真实结果、enforced_at=本轮时间、enforced_attempted=True（--skip-enforce
    时 post_write 不再写，由本函数补位）。
    C-ENFORCE-TRUTH@1（2026-11-13 R5）：enforced 记录真实结果（失败即 False），不再无条件
    写 True；enforced_attempted 每轮覆盖，使「漏跑」与「跑失败」在 meta 上可区分。
    仅接入 write 链（append 提交）；rewrite/deslop/revise 链为 revision 模式
    （A-5 豁免），维持原顺序不动。
    """
    res = toolbox.run_script(
        "nww_enforce.py",
        ["--book", str(book), "--file", str(prose)],
        book,
        timeout=60,
    )
    try:
        _write_quality_meta(draft_path, enforced=bool(res.get("ok")),
                            enforced_at=int(time.time()), enforced_attempted=True)
    except Exception:  # noqa: BLE001 — enforced 写入尽力而为，与 post_write Step1 行为一致
        pass
    return res


# —— 落盘完整性观测（2026-10-12）——
# 背景（MEMORY REVIEW-FINALIZE ★新缺陷类）：phase_finalize 自述是「唯一 best→final
# 物理落盘点 + copy 后 hash 校验」，但 `step_post_write` 排在 `step_review_cycle`（内含
# finalize）**之后**，而 post_write 的 Step 1 是 `nww_enforce.py` —— 它会改写正文
# （引号风格「」→""、markdown 剥离、结尾泄漏清理）。→ finalize 之后仍有写盘者，
# 「唯一落盘点」在 Web 链路里只是名义的：**你审的是版本 X，交付的是版本 Y**。
#
# 本观测不改顺序（顺序契约改动的循环依赖未解），只把这件事变成可测的数字：
#   finalize 后取一次 sha256 → post_write 后再取一次 → 不一致即记录差异。
# 产出只写给 stage detail + 追踪/落盘完整性.jsonl，advisory，绝不阻断。
# 开关 NWW_FINALIZE_INTEGRITY（默认关，先在已完章节离线核对）。
_FINALIZE_INTEGRITY_SWITCH = "NWW_FINALIZE_INTEGRITY"


def _finalize_integrity_enabled() -> bool:
    return os.environ.get(_FINALIZE_INTEGRITY_SWITCH, "0") == "1"


def _sha256_of(path: Path) -> str:
    import hashlib
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        return ""


def _record_finalize_integrity(book: Path, chapter_id: int, review_id: str,
                               sha_after_review: str, sha_final: str) -> dict:
    """落盘完整性观测：finalize 之后的正文是否又被改写过。只记录，不阻断。"""
    same = bool(sha_after_review) and sha_after_review == sha_final
    rec = {
        "chapter": chapter_id, "review_id": review_id or "",
        "sha_after_review": sha_after_review, "sha_final": sha_final,
        "intact": same,
        "note": ("finalize 后正文未被改写" if same else
                 "finalize 后正文被 post_write(enforce/commit) 改写 —— 交付版本 ≠ 评审版本"),
    }
    try:
        d = book / _TRACKING_DIR
        d.mkdir(parents=True, exist_ok=True)
        with (d / "落盘完整性.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return rec


_CAUSAL_AUDIT_SWITCH = "NWW_CAUSAL_AUDIT"
_CAUSAL_AUDIT_SCRIPT = "nww_causal_audit.py"
_TAIL_GATE_SWITCH = "NWW_TAIL_GATE"
_TAIL_GATE_SCRIPT = "nww_chapter_tail_gate.py"


def _tail_gate_enabled() -> bool:
    """章尾动力类型轮换审计开关（默认关）。

    外脑 P1-4 裁决：定位为「必执行的质量审计器」，**不是第一版正文硬阻断器**；
    硬阻断策略需另经金标准验证后决定。默认关闭以便先在已完成章节离线核对。
    """
    return os.environ.get(_TAIL_GATE_SWITCH, "0") == "1"


def step_tail_gate(book: Path, chapter_id: int,
                   artifact_dir: Path | None = None) -> dict:
    """7.9 章尾动力类型轮换审计（advisory；外脑 ChatGPT 裁决 P1-4）。

    ★ 2026-10-12 修正：必须先剥离系统方括号计数器（bible cost_system 的
      「【XX：N】」）再分类。不剥离 → 计数器必然命中 A 类正则 → 全书 100% 判 A。
      2026-10-11 报出的「钦天监 27/27 = 100% A」即此测量错误，已作废。

    只产出审计结果与回执，**不改正文、不阻断 commit**。未分类率高时结论打折看待。
    """
    args = [str(book), "--json", "--upto", str(chapter_id)]
    try:
        res = toolbox.run_script(_TAIL_GATE_SCRIPT, args, book, timeout=120)
    except PermissionError:
        return {"ok": False, "detail": "脚本不在白名单（已登记？）"}
    if not res.get("ok") and not res.get("stdout"):
        return {"ok": False, "detail": (res.get("stderr") or "")[:400]}
    try:
        data = json.loads(res["stdout"] or "{}")
    except Exception:
        return {"ok": False, "detail": "章尾审计输出非 JSON"}
    # 只看最近 3 章（last_3 禁 AA/AAA），但保留全本分布供人工看趋势
    rows = data.get("rows") or []
    tail3 = [r.get("type") for r in rows[-3:]]
    data["last_3"] = tail3
    data["last_3_violation"] = (
        len(tail3) == 3 and tail3[0] == tail3[1] == tail3[2] and tail3[0] != "U"
    ) or (len(tail3) >= 2 and tail3[-1] == tail3[-2] and tail3[-1] != "U")
    if artifact_dir is not None:
        try:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / f"第{chapter_id:03d}章.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
    return data


# —— global_change 回流开关（2026-10-12 P0 修复）——
# 背景：实测 4 本书 26 份 delta 的 global_change 非空率 = 0/26（0.0%），
# 追踪/全局状态.md 因此恒为「尚未写入全局状态」（钦天监 27 章 109 字、五行归源 109 字），
# 「跨章累积 / 宏观发展」这一厚度维度在系统内**没有任何承载体**。
# 根因不在 commit（nww_track_commit.py:1958 的 transaction 白名单**本就接受** global_change，
# :1996-1998 递归合并），而在 pipeline 侧三处把产出掐死：
#   ① step_delta_semantic 的 prompt 曾显式禁止把全局变化写进输出对象
#   ② :1604 prompt「严禁输出上面没提到的任何顶层键」
#   ③ _repair_delta 的 delta 投影（正确：global_change 属 transaction 根级）
#      —— 但没有任何一步把它**提升**到根级，于是静默丢弃。
# 回滚只需 NWW_GLOBAL_CHANGE=0，行为退回修复前（恒 null）。
_GLOBAL_CHANGE_SWITCH = "NWW_GLOBAL_CHANGE"
# global_change 渲染后的字节硬帽（超了就丢弃，语义等同修复前的 null，绝不 commit BLOCK）
_GLOBAL_CHANGE_MAX_BYTES = 2048
_GLOBAL_CHANGE_MAX_KEYS = 8


def _global_change_enabled() -> bool:
    """global_change 回流开关（默认开；置 0 回滚到「恒 null」的旧行为）。"""
    return os.environ.get(_GLOBAL_CHANGE_SWITCH, "1") == "1"


def _hoist_global_change(filled: dict, tx: dict, chapter_id: int) -> tuple[dict, dict | None]:
    """把 LLM 补出的 global_change 从 delta 提升到 transaction 根级。

    只增不改：delta 白名单投影保持原样（global_change 仍不是合法 delta 键），
    本函数只负责「从 delta 里拿出来 → 校验 → 挂到 tx 根」。

    失败一律 fail-closed 到 None（= 修复前行为），绝不抛、绝不 commit BLOCK。
    """
    gc = filled.pop("global_change", None) if isinstance(filled, dict) else None
    if gc is None:
        return filled, None
    if not isinstance(gc, dict) or not gc:
        print(f"[GLOBAL_CHANGE] 第{chapter_id}章 丢弃：非对象或空对象", flush=True)
        return filled, None
    if len(gc) > _GLOBAL_CHANGE_MAX_KEYS:
        print(f"[GLOBAL_CHANGE] 第{chapter_id}章 丢弃：顶层键 {len(gc)} 超上限 "
              f"{_GLOBAL_CHANGE_MAX_KEYS}", flush=True)
        return filled, None
    # 值类型收敛：只接受 commit 侧 normalize_global_state 能安全递归合并的类型
    def _ok(v: object, depth: int = 0) -> bool:
        if depth > 3:
            return False
        if isinstance(v, (str, int, float, bool)) or v is None:
            return True
        if isinstance(v, list):
            return all(_ok(x, depth + 1) for x in v[:50])
        if isinstance(v, dict):
            return all(isinstance(k, str) and _ok(x, depth + 1) for k, x in list(v.items())[:30])
        return False
    clean = {str(k): v for k, v in gc.items() if isinstance(k, str) and _ok(v)}
    if not clean:
        print(f"[GLOBAL_CHANGE] 第{chapter_id}章 丢弃：无合法字段", flush=True)
        return filled, None
    try:
        size = len(json.dumps(clean, ensure_ascii=False).encode("utf-8"))
    except Exception:
        return filled, None
    if size > _GLOBAL_CHANGE_MAX_BYTES:
        print(f"[GLOBAL_CHANGE] 第{chapter_id}章 丢弃：{size}B 超上限 "
              f"{_GLOBAL_CHANGE_MAX_BYTES}B", flush=True)
        return filled, None
    print(f"[GLOBAL_CHANGE] 第{chapter_id}章 采纳 {len(clean)} 键 / {size}B："
          f"{sorted(clean)}", flush=True)
    return filled, clean


def _causal_audit_enabled() -> bool:
    """跨章因果审计灰度开关（默认关；置 1 开启）。

    外脑 P0-3：第一阶段只产出审计结果，不阻断。故默认关闭，先在已完成章节离线回放
    核对误报/漏报后再开启（与 quality_observe 同款灰度范式）。
    """
    return os.environ.get(_CAUSAL_AUDIT_SWITCH, "0") == "1"


def step_causal_audit(book: Path, chapter_id: int,
                      artifact_dir: Path | None = None) -> dict:
    """7.8 跨章因果审计（advisory；外脑 ChatGPT 裁决 P0-3）。

    审计四件事：① 哪些既有事实/事件影响了本章 ② 哪些关系/物件/势力状态变化
    ③ 本章建立了哪些后继承诺 ④ 哪些计划中的事件没发生或结果不同。

    硬约束：每个判断必须带证据，无法确认标 UNKNOWN；只用确定性 4-gram 探针，零 LLM；
    **不修改正文、不改变 commit 语义、不设硬失败**。dry_run 时产物落 artifact_dir。
    """
    out_dir = artifact_dir if artifact_dir is not None else (book / "追踪" / "因果审计")
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    args = ["--book", str(book), "--chapter", str(chapter_id), "--json"]
    if artifact_dir is None:
        args.append("--write")
    try:
        res = toolbox.run_script(_CAUSAL_AUDIT_SCRIPT, args, book, timeout=120)
    except PermissionError:
        return {"ok": False, "detail": "脚本不在白名单（已登记？）"}
    if not res.get("ok"):
        return {"ok": False, "detail": (res.get("stderr") or res.get("stdout") or "")[:400]}
    try:
        data = json.loads(res["stdout"] or "{}")
    except Exception:
        return {"ok": False, "detail": "审计输出非 JSON"}
    if artifact_dir is not None:
        # dry_run：正式目录零变化，产物只落本次 execution
        try:
            (out_dir / f"第{chapter_id:03d}章.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
    data["record_path"] = str(out_dir / f"第{chapter_id:03d}章.json")
    return data


def _era_register_directives(book: Path) -> str:
    """时代语域铁律（按 bible.era_constraints.register_mode 三档渲染）。

    三档（2026-10-25 用户裁决）：
      strict   修真正典：以修仙界为核心表述，公共描述零现代词；现代词只在主角
               内心/对白合法且禁 gloss；土著禁现代概念词；商品名可指名禁解说
      balanced 修主现辅：修真语域为主基调，现代词可适当点缀（低频、有效果目的）
      modern   现代语感：用现代语感描绘铺垫修仙界；现代词=风格；检测点转为
               世界自洽（物理世界仍是修仙界）与风格一致性（不漂移）
    bible 无 era_constraints → 返回 ""（不注入，行为不变）。与确定性 era_check
    （REGISTER_SEVERITY 矩阵）和审稿 I-E prompt 三处共读同一字段，写的=查的。
    """
    era = json.loads((book / "bible.json").read_text(encoding="utf-8")).get("era_constraints") or {}
    if not (isinstance(era, dict) and era.get("policy")):
        return ""
    mode = str(era.get("register_mode") or "strict").strip().lower()
    mode = mode if mode in ("strict", "balanced", "modern") else "strict"
    protag = str(era.get("protagonist") or "主角")
    goods = "、".join(str(x) for x in (era.get("protagonist_brought") or [])[:8]) or "细纲明示的铺内货品"
    head = f"\n【时代语域铁律（档位={mode}，bible.era_constraints 为权威，违反=审稿 I-E 维度重扣）】\n"
    if mode == "strict":
        return head + (
            f"0. 以修仙界世界为核心表述：旁白/公共描述（叙述、环境、动作、神态描写）禁用一切"
            f"现代词汇——现代概念词（双标/KPI/内卷/社畜/996/促销/性价比 等）与现代器物词"
            f"（沙发/手机/玻璃 等）都算；度量衡、比喻、称呼一律用修真界意象。\n"
            f"1. 现代词只允许出现在主角{protag}（穿越者）的内心独白与对白里；{protag}说现代词"
            f"时保持自然，禁止做修仙界式标注或解释（不得写「双标，即修真界所谓…」类 gloss，"
            f"也不解释来历原理）。\n"
            f"2. 修仙界土著角色（对白与内心）绝对禁止出现现代概念词——土著不知晓现代文化，"
            f"他们的焦虑/讽刺/比喻必须用修真界语域表达（如「双标」→「只许州官放火式的宗门规矩」"
            f"式的修真界说法）。写任何土著台词前自检：这句话里有没有现代词？\n"
            f"3. 命名传播链（bible.era_constraints.naming_propagation）：现代物品名与诙谐现代词"
            f"的首次命名权归{protag}——旁白（主角视角）与{protag}对白可指名；{goods}等商品名"
            f"其他角色必须已亲历{protag}传播（本章或前章在场听到/看到价牌）才能使用，"
            f"未传播的新客/旁观者凭空说出=无厘头违规，必须用替代词（黑水/凡膏/红条/软扑子等，"
            f"见细纲命名节拍与替代词注记）；传播中可写学舌痕迹（「辣…辣条？」）。"
            f"旁白禁止解说商品现代原理/来历（只许经主角视角给出）。\n"
        )
    if mode == "balanced":
        return head + (
            f"0. 整体基调=修仙界语域：叙述、比喻、度量衡、称呼以修真界意象为主；现代词"
            f"可作点缀（低频、有对照/喜剧/张力效果处才用，禁止常态化堆砌）。\n"
            f"1. 主角{protag}（穿越者）的内心独白与对白可自由使用现代词；土著台词原则上"
            f"用修真语域（个别「跟主角学舌」桥段须有剧情支撑）。\n"
            f"2. 无来源的现代器物不得凭空出现在物理世界（要出现须有主角/金手指来源）；"
            f"设定内商品名（{goods}）可指名交易，旁白禁做现代原理/来历解说。\n"
        )
    return head + (  # modern
        f"0. 用现代语感描绘铺垫修仙界（宗门当大厂、修士当打工人式行文）：现代概念词"
        f"（KPI/内卷/双标/996 等）是风格选择，可进入旁白与对白，不是穿帮。\n"
        f"1. 风格一致性：现代语感应贯穿全章，禁止忽而现代忽而仿古的语感漂移。\n"
        f"2. 世界自洽：物理世界仍是修仙界（灵石/符箓/飞剑/宗门结构）；真实科技产物"
        f"（手机/汽车等实物）仍须有主角/金手指来源，设定内商品名（{goods}）照旧合法。\n"
    )


def step_deslop(book: Path, chapter_id: int, prose: Path,
                check_only: bool = False) -> list[dict]:
    """7. 去AI味机械层（advisory 只报告）：AI 模式 / 退化 / 标点 / 跨章重复。

    check_only（2026-11-01 实锤修复）：True → normalize-punctuation.js 走 `--check`
    **只读**。post-commit 的「复扫」必须只读：该脚本不带 `--check` 时会直接写回
    （normalize-punctuation.js:75），实测它在 commit **之后**又改了一次正文
    （引号/标点归一），造成「已提交的正文 ≠ 盘上的正文」—— 这正是第1章反复出现的
    正文漂移，同时会把刚重绑的 final_hash 变成假值。
    write/rewrite 链的 deslop 仍在 commit 前调用（默认 False，保持改写语义）。

    引号口径（2026-10-24，用户裁决落地）：normalize-punctuation 显式
    `--quote-mode ascii`（番茄/红果默认对话用 `""`，禁「」，与 writer_prompt
    引号铁律同源）。此前 keep 模式 = 模型偷用「」时静默保留 → 第1/2章引号分裂。
    知乎盐言项目设 NWW_QUOTE_MODE=yan；要完全保留现状设 NWW_QUOTE_MODE=keep。
    """
    _quote_mode = os.environ.get("NWW_QUOTE_MODE", "ascii").strip() or "ascii"
    if _quote_mode not in ("keep", "ascii", "yan"):  # 非法值回退默认，防手抖打挂链
        _quote_mode = "ascii"
    reports = []
    for script in ("check-ai-patterns.js", "check-degeneration.js", "normalize-punctuation.js"):
        if script == "normalize-punctuation.js":
            args = (["--check"] if check_only else []) + [
                "--quote-mode", _quote_mode, str(prose)]
        else:
            args = ["--check", str(prose)]
        res = toolbox.run_script(
            script, args,
            book, timeout=180,
        )
        entry = {"script": script, "ok": res["ok"], "stdout": res["stdout"][:1200], "stderr": res["stderr"][:300]}
        # 比喻软gate透传（外脑决策 A，纯标记不阻断）：check-ai-patterns 命中
        # metaphor-cluster-tic（同段相邻比喻堆叠）或 metaphor-word-repeat-tic
        # （同一喻词单章复用过高）时，给下游 revision/前端一个"建议处理"信号。
        if script == "check-ai-patterns.js" and res.get("stdout"):
            if "metaphor-cluster-tic" in res["stdout"] or "metaphor-word-repeat-tic" in res["stdout"]:
                entry["suggest_revision"] = "metaphor-density"
        reports.append(entry)
    # 跨章重复检测（advisory）：与近 N 章逐字/短语雷同会在此报告，提示人工处理
    try:
        cc = toolbox.run_script(
            "nww_cross_chapter_check.py",
            ["--book", str(book), "--chapter", str(chapter_id), "--window", "5", "--json"],
            book, timeout=120,
        )
        if cc["ok"] and cc["stdout"].strip():
            reports.append({"script": "nww_cross_chapter_check.py", "ok": True, "stdout": cc["stdout"][:1200], "stderr": ""})
    except Exception:  # noqa: BLE001 — advisory 失败不阻断
        pass
    return reports


# ---- D2 一致性审查（consistency-checker agent 检测 + 写 追踪/一致性审查/第N章.json） ----
# 关键区分：detection ≠ closure
#   本函数只做 detection：调用 consistency-checker 产出 S1-S4 冲突事实，写审查记录
#   closure（closed=true）由既有闭环流程完成，A-3 闸（追踪/一致性核验/第N章.json）不受影响
#   审查记录格式 {"chapter": N, "open_s1_s2": [...], "s3_s4": [...], "updated_at": ts}
#   closed 字段绝不在此写入——closure 是人工裁决或后续修复后的独立动作
# Phase 1.5 外脑裁决 A（2026-09-17）：parser 最小兼容修复。
#   放宽原因：checker 输出展示格式不稳定（可能为 `- [S2] 描述`，也可能为加粗+tag 的
#   `- **[S2] [tag] 描述**`），原正则 `^-\s*\[S([1-4])\]` 无法匹配加粗前缀，导致
#   真实问题被解析为 0 findings（伪干净）→ A-3 误放行风险。
#   修复范围：仅解析层（正则 + content 清洗），不改 checker 提示词 / S1-S4 分类逻辑 /
#   consistency contract / A-3 gate。验收：新旧格式同一报告均能产出正确 open_s1_s2/s3_s4。
# Phase 1.6 回放验证发现（2026-09-17）：checker 输出格式进一步漂移为 `### [S2] 标题式`，
#   同上类问题家族——仅扩展正则前缀字符类（支持 # 标题），不改任何语义。
_A3_CONFLICT_RE = re.compile(r"^[\s\-*#]*\[S([1-4])\]\*{0,2}\s*(?:\[[^\]]+\]\s*)?(.+)$")
# Phase 1.6 第二阶段回放发现（2026-09-17）：checker 输出第三次漂移为 `**判定**：[S3] xx` 行内
#   判定式。外脑裁决 P6-A：扩展解析兼容层（限格式层面），但禁止从任意正文段落/引用内容/解释
#   文本中提取 [Sx]。本正则仅匹配行首 `判定：` / `**判定**：` 前缀（inline_label）。
_A3_CONFLICT_LABEL_RE = re.compile(r"^\s*\*{0,2}判定\*{0,2}\s*[：:]\s*\[S([1-4])\]\*{0,2}\s*(.*)$")

# 根治（2026-11-06）：一致性 checker 自身带「留白/需核对」限定语的条目，是它对
# 矛盾的**不确定性陈述**（「若细纲已设定…否则为证据不足提示」「需核对」「需裁决」
# 「证据不足」），并非已确认的硬矛盾。这类条目被原闸原样计入 open_s1_s2 硬伤数 →
# 只要细纲/设定实际已涵盖，便误杀合规新稿并回滚。判定标准：条目文本同时命中
# 「不确定」限定语 + 「需对照细纲/设定」语境 → 标记为 hedged（待作者裁决，不进硬 BLOCK）。
# 真矛盾（不含这些限定语、或明确断言「矛盾/断裂/漏洞」）仍正常拦。
_A3_HEDGED_RE = re.compile(
    r"(若.{0,12}未写明|若.{0,12}未设定|否则[^，。；]{0,12}(提示|不成立)|否则为|证据不足|"
    r"需核对|需裁决|待核对|需确认|需规划层登记|是否允许|不构成冲突|不构成矛盾|仅可作为)",
    re.DOTALL,
)

# ---- Phase 1.6 P6-1 分级稳定层常量（外脑 2026-09-17 裁决） ----
# 严重度顺序：S1 < S2 < S3 < S4（数字越小越严重，只升不降）。
_LEVEL_ORDER = {"S1": 0, "S2": 1, "S3": 2, "S4": 3}
# 主题词表：实体/领域词，用于"同一主题历史最高级别"匹配（与 content 做子串匹配）。
_ENTITY_WORDS = (
    "凡气", "灵气", "灵力", "丹田", "经脉",
    "汪万金", "林小凡", "温故", "陈执事",
    "账本", "灵石", "流水", "账目", "账",
    "时间线", "日期", "时间跨度", "时间", "天前",
    "聚灵堂", "杂货铺", "青云宗",
    "行李箱", "货格子", "马车", "帖子",
    "产能", "订单", "独家", "供货",
    "年龄", "修为", "婚龄", "开店", "寿命",
    "伏笔", "设定", "规则边界", "场景", "转场",
)


def step_consistency_check(book: Path, chapter_id: int, prose_path: Path,
                           artifact_dir: Path | None = None) -> dict:
    book = book.resolve()
    """一致性审查（detection only）：调用 consistency-checker agent，写审查记录。

    只做检测 + 记录，不裁决（closed 绝不由此写入）。审查记录路径：
    {book}/追踪/一致性审查/第{N}章.json。closure 目录（追踪/一致性核验/）由既有流程负责。

    artifact_dir 显式传入（dry_run）→ 审查记录 JSON 落本次 execution 的 artifact root，
    绝不写正式 追踪/一致性审查/；缺省 None → 原正式路径（行为不变）。
    """
    prose_text = prose_path.read_text(encoding="utf-8")
    agent = registry.get_agent("consistency-checker")
    prompt = (
        f"书项目根: {book}\n目标章: 第{chapter_id}章\n\n"
        f"以下是本章正文全文，请按你的方法论做事实一致性检查（S1-S4 分级）。\n"
        f"只输出检查报告，不修改任何文件。\n\n"
        f"{prose_text[:12000]}\n\n"
        "【输出形态铁律】本次运行你没有任何工具可用：禁止输出 <function=…>、"
        "<invoke>、<param>、<glob> 等任何工具调用语法或「我将调用…」的角色扮演；"
        "书内核查所需上下文已全部在下文给出。直接输出报告文本："
        "发现冲突逐条以「[S1]-[S4]」行式给出（每行一条），"
        "无冲突也要给出「未发现 S1-S4 冲突」的明确 VERDICT 行。空输出或工具泥沙 = 检查未完成。"
    )

    def _parse_findings(out: str) -> tuple[list[dict], list[dict], list[dict]]:
        """解析 checker 输出的 S1-S4 冲突行。

        P6-A（外脑 2026-09-17）：支持三类匹配模式，但严格限定在「冲突条目行首」，
        禁止从任意正文段落/引用内容/解释文本提取 [Sx]：
          - bullet          `- [S2] xx` / `- **[S2] [tag] xx**`
          - markdown_heading `### [S1] xx`
          - inline_label    `判定：[S3] xx` / `**判定**：[S3] xx`
        每条 issue 记录 parser_match_mode + raw_line 审计字段。

        返回 (open_s1_s2, s3_s4, hedged_s1_s2)；三个列表都在本函数内初始化，
        绝不引用外层同名变量（否则在 `_is_degenerate` 提前调用时会出现
        UnboundLocalError: cannot access free variable）。
        """
        open_s1_s2: list[dict] = []
        s3_s4: list[dict] = []
        hedged_s1_s2: list[dict] = []
        for line in out.splitlines():
            stripped = line.strip()
            m = _A3_CONFLICT_LABEL_RE.match(stripped)
            if m:
                mode = "inline_label"
            else:
                m = _A3_CONFLICT_RE.match(stripped)
                if not m:
                    continue
                mode = "markdown_heading" if stripped.startswith("#") else "bullet"
            level = f"S{m.group(1)}"
            item = {
                "level": level,
                "content": m.group(2).strip().rstrip("*").strip()[:300],
                "parser_match_mode": mode,
                "raw_line": stripped[:200],
            }
            if level in ("S1", "S2"):
                # 根治：checker 自身带「留白/需核对」限定语的条目 → hedged，
                # 不计入硬 BLOCK 数（仍落盘供作者裁决，见返回 dict 的 hedged_s1_s2）。
                # 既不放行真矛盾，也不再用 checker 的不确定陈述误杀合规新稿。
                if _A3_HEDGED_RE.search(item["content"]):
                    item["hedged"] = True
                    hedged_s1_s2.append(item)
                else:
                    open_s1_s2.append(item)
            else:
                s3_s4.append(item)
        return open_s1_s2, s3_s4, hedged_s1_s2

    def _is_degenerate(out: str) -> bool:
        """退化输出判定（外脑 Phase 1.5 裁决：最小可靠性保护，不改检测逻辑/checker 契约）。

        满足其一视为退化：① 含工具调用文本（checker 方法论要求 grep-first，但 runtime
        不执行工具，模型可能输出 tool_call 后即停）；② 无可解析 S 行且无契约报告结构
        （VERDICT/CONFLICTS/[S1-4]）。禁止「空输出 = 干净通过」。
        """
        if "<tool_call>" in out or re.search(r"<function[=> ]", out):
            return True
        open_s1_s2, s3_s4, _hedged = _parse_findings(out)
        if open_s1_s2 or s3_s4:
            return False
        return not re.search(r"VERDICT|CONFLICTS|\[S[1-4]\]", out)

    def _run_once() -> str:
        return _run_agent_bounded(agent, "一致性检查（只读）", prompt,
                                  timeout=300.0, temperature=0.2, max_tokens=4096)

    try:
        out = _run_once()
        if _is_degenerate(out):
            out = _run_once()
            if _is_degenerate(out):
                return {
                    "ok": False,
                    "detail": "consistency-checker 输出退化/无有效报告（重试后仍失败），检测未完成，未写审查记录",
                    "raw_report": out[:2000],
                }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"consistency-checker 调用失败: {exc}"}

    open_s1_s2, s3_s4, hedged_s1_s2 = _parse_findings(out)

    # ---- Phase 1.6 后处理（外脑 2026-09-17 裁决 P6-1/P6-2；不改 checker 原始结果，纯扩展字段） ----
    # P6-1 分级稳定层：只升不降。同主题历史最高级别 < 本轮 → effective_level = 历史最高级别。
    #   原则：不重新创造问题；跨轮降级（S2→S3/S4）不得造成闭环漏洞。
    # P6-2 evidence 验证状态：对 issue 引用的证据文件做存在性校验（宽容匹配路径简写），
    #   not_found → 不得进入 repair executor（外脑裁决：detector_note + evidence_invalid + close）。

    def _topic_keys(text: str) -> set:
        keys = {w for w in _ENTITY_WORDS if w in text}
        keys |= set(re.findall(r"\d{1,4}(?:月|天前|灵石|岁|年)", text))
        return keys

    def _history_records() -> list:
        """读 追踪/一致性审查/ 下历史记录（含当前章覆盖前旧值）。

        参与 stability floor 的条件：未闭环 或 已闭环但留有 retained_detection
        （defer/待复核项，问题未真正解决）。已闭环且无遗留项的章节不参与——
        避免已解决/已修复的历史问题被固化复活（外脑 P6-1 原则：不重新创造问题）。
        """
        recs = []
        review_dir = book / "追踪" / "一致性审查"
        closure_dir = book / "追踪" / "一致性核验"
        if review_dir.is_dir():
            for p in sorted(review_dir.glob("第*章.json")):
                try:
                    rec = json.loads(p.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                cp = closure_dir / p.name
                closed = False
                if cp.is_file():
                    try:
                        closed = json.loads(cp.read_text(encoding="utf-8")).get("closed") is True
                    except (json.JSONDecodeError, OSError):
                        closed = False
                if closed and not rec.get("retained_detection"):
                    continue
                recs.append(rec)
        return recs

    def _apply_stability_floor(items: list, history: list) -> list:
        """对每条 issue 计算 stability_floor：历史同主题最高级别（只升不降）。"""
        out_items = []
        for it in items:
            detected = it["level"]
            keys = _topic_keys(it["content"])
            floor = None
            for rec in history:
                for group in ("open_s1_s2", "s3_s4", "retained_detection"):
                    for h in rec.get(group, []):
                        if not isinstance(h, dict):
                            continue
                        if _topic_keys(h.get("content", "")) & keys:
                            lvl = h.get("level", "")
                            if lvl in _LEVEL_ORDER and (floor is None or _LEVEL_ORDER[lvl] < _LEVEL_ORDER[floor]):
                                floor = lvl
            if floor is None:
                out_items.append(dict(it))
                continue
            eff = floor if _LEVEL_ORDER[floor] < _LEVEL_ORDER[detected] else detected
            item = dict(it)
            if eff != detected:
                item["level"] = eff
                item["detected_level"] = detected
                item["stability_floor"] = floor
            out_items.append(item)
        return out_items

    def _normalized_ref_candidates(ref: str) -> list:
        """P6-B 修复（2026-09-21）：受控规范化候选（零填充 × 直接子目录组合）。

        仅生成有限候选，不做跨目录全库 rglob 模糊命中：
          a) 章节文件名零填充：`第38章.md` → `第038章.md`（惯例 3 位）；
          b) 直接子目录精确名：`正文/第038章.md` → `正文/第一卷/第038章.md`。
        命中判定由 _resolve_ref 统一做（精确 is_file + 候选目录内受控 stem 前缀）。
        """
        out: list = []
        p = Path(ref)
        name = p.name
        variants = [ref]
        m = re.fullmatch(r"(第)(\d+)(章\.md)", name)
        if m:
            padded = f"第{int(m.group(2)):03d}章.md"
            if padded != name:
                variants.append(str(p.with_name(padded)))
        for v in variants:
            vp = Path(v)
            d = vp.parent
            if str(d) != ".":
                base = book / d
                if base.is_dir():
                    for s in base.iterdir():
                        if s.is_dir():
                            cand = str(vp.parent / s.name / vp.name)
                            if cand != v:
                                out.append(cand)
        return out

    def _resolve_ref(ref: str) -> str:
        """匹配证据文件并返回 match_type。

        P6-B（外脑 2026-09-17 裁决）：严格优先级 精确路径 > 同目录 basename >
        同目录受控 stem；**禁止跨目录全库 rglob 模糊命中**（如 `系统.md` 误匹配到
        `NWW系统审核报告x.md` 的假阳性）。返回：
          exact / basename / stem / not_found
        """
        cand = book / ref
        if cand.is_file():
            return "exact"
        for c in _normalized_ref_candidates(ref):
            if (book / c).is_file():
                return "exact"
            cd = (book / c).parent
            cn = Path(c).name
            if cn.endswith(".md"):
                cstem = cn[:-3]
                if cstem and cd.is_dir() and any(f.stem.startswith(cstem) for f in cd.glob("*.md")):
                    return "exact"
        name = Path(ref).name
        if not name.endswith(".md"):
            return "not_found"
        own_dir = (book / ref).parent
        if not own_dir.is_dir():
            return "not_found"
        if list(own_dir.glob(name)):
            return "basename"
        stem = name[:-3]
        if stem:
            hits = [p for p in own_dir.glob("*.md") if p.stem.startswith(stem) or stem.startswith(p.stem)]
            if hits:
                return "stem"
        return "not_found"

    # match_type → issue 证据状态（P6-B：非 exact 不直接 verified）
    _MATCH_TO_STATUS = {"exact": "verified", "basename": "weak_verified", "stem": "ambiguous", "not_found": "not_found"}

    def _rank_status(s: str) -> int:
        return {"verified": 0, "weak_verified": 1, "ambiguous": 2, "not_found": 3,
                "missing_reference": 3, "no_reference": 2}.get(s, 4)

    def _evidence_validation(open_s1_s2: list, s3_s4: list, raw: str) -> dict:
        """P6-2 issue 级 evidence 验证 + P6-B 收窄 + P6-D 无引用处置。

        每条 issue 状态封闭（禁止 unknown）：
          - 有引用：按 refs 中最差 match_type 映射 verified/weak_verified/ambiguous/not_found
          - 无引用（P6-D）：S1/S2 → missing_reference + action=manual_review（不自动关闭）；
            S3/S4 → no_reference（观察级，不阻断）
        产出章级 status + checked_source（含 match_type）+ issues + evidence_gate（供 closure 用）。
        """
        raw_refs = set(re.findall(r"`([^`]+\.md)`", raw))
        checked: dict = {}      # ref -> {"match_type":.., "status":..}
        issues: list[dict] = []
        for it in list(open_s1_s2) + list(s3_s4):
            refs = set(re.findall(r"(?:文件|file)[：:]\s*([^\s,;，。:：]+\.md)(?::\d+)?", it.get("content", "")))
            ref_status: dict = {}
            for ref in sorted(refs):
                if ref not in checked:
                    mt = _resolve_ref(ref)
                    checked[ref] = {"match_type": mt, "status": _MATCH_TO_STATUS[mt]}
                ref_status[ref] = checked[ref]["status"]
            is_s1s2 = it.get("level") in ("S1", "S2")
            if not refs:
                status = "missing_reference" if is_s1s2 else "no_reference"
                issue = {
                    "level": it.get("level"),
                    "content": it.get("content", "")[:150],
                    "status": status,
                    "refs": [],
                    "ref_status": {},
                }
                if is_s1s2:
                    issue["action"] = "manual_review"
            else:
                worst = max(ref_status.values(), key=lambda s: _rank_status(s))
                issue = {
                    "level": it.get("level"),
                    "content": it.get("content", "")[:150],
                    "status": worst,
                    "refs": sorted(refs),
                    "ref_status": ref_status,
                }
            issues.append(issue)
        for ref in raw_refs:
            if ref not in checked:
                mt = _resolve_ref(ref)
                checked[ref] = {"match_type": mt, "status": _MATCH_TO_STATUS[mt]}
        chapter_status = "verified"
        for ref, info in checked.items():
            if _rank_status(info["status"]) > _rank_status(chapter_status):
                chapter_status = info["status"]
        # 供 P6-C closure 的 evidence_gate 汇总（仅 S1/S2 issue 视角）
        gate = {"invalid": 0, "ambiguous": 0, "missing_reference": 0}
        for it in issues:
            if it["level"] not in ("S1", "S2"):
                continue
            if it["status"] in ("not_found", "missing_reference"):
                gate["invalid"] += 1
            if it["status"] == "ambiguous":
                gate["ambiguous"] += 1
            if it["status"] == "missing_reference":
                gate["missing_reference"] += 1
        return {
            "status": chapter_status,
            "checked_source": {ref: info["status"] for ref, info in checked.items()},
            "match_type": {ref: info["match_type"] for ref, info in checked.items()},
            "issues": issues,
            "evidence_gate": gate,
        }

    history = _history_records()
    # 根治：hedged 条目（checker 自身留白/需核对）不参与分级稳定层与硬 BLOCK 计数，
    # 仅落盘备查；其「只升不降」无关，故单独保留原始 hedged 列表。
    open_s1_s2 = _apply_stability_floor(open_s1_s2, history)
    s3_s4 = _apply_stability_floor(s3_s4, history)
    overlay = [it for it in list(open_s1_s2) + list(s3_s4) if "detected_level" in it]
    ev = _evidence_validation(open_s1_s2, s3_s4, out)

    record = {
        "chapter": chapter_id,
        "open_s1_s2": open_s1_s2,
        "hedged_s1_s2": hedged_s1_s2,
        "s3_s4": s3_s4,
        "updated_at": int(time.time()),
    }
    if overlay:
        record["stability_overlay"] = [
            {
                "topic": sorted(_topic_keys(it["content"])),
                "detected_level": it["detected_level"],
                "stability_floor": it["stability_floor"],
                "effective_level": it["level"],
                "reason": "same_topic_previous_high_level",
            }
            for it in overlay
        ]
    record["evidence_validation"] = ev
    # P6-E Step3：era_consistency observation adapter（独立观察维度，不 BLOCK/不改文/可回滚）。
    # 异常或开关关闭由 adapter 内部隔离后返回 {}，此处不悬挂主链。
    _era_r = era_check_observe(book, chapter_id, prose_text)
    if _era_r:
        record["era_consistency"] = _era_r
    review_dir = artifact_dir if artifact_dir is not None else (book / "追踪" / "一致性审查")
    review_dir.mkdir(parents=True, exist_ok=True)
    rec_path = review_dir / f"第{chapter_id}章.json"
    rec_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "ok": True,
        "has_s1_s2": len(open_s1_s2) > 0,
        "open_s1_s2_count": len(open_s1_s2),
        "hedged_s1_s2_count": len(hedged_s1_s2),
        "s3_s4_count": len(s3_s4),
        "record_path": str(rec_path.relative_to(book)),
        "raw_report": out[:2000],
    }


def _auto_closure_enabled() -> bool:
    """NWW_AUTO_CLOSURE 开关（默认开）。=0 关闭，回退旧行为（不自动落闭环）。"""
    return os.environ.get(_AUTO_CLOSURE_SWITCH, "1") != "0"


def step_consistency_closure(book: Path, chapter_id: int, cons: dict) -> dict:
    """一致性自动闭环（2026-11-04，detection ≠ closure 边界内的独立步骤）。

    契约：{book}/追踪/一致性核验/第{N}章.json（对齐 consistency-closure.md + A-3 机械闸）。

    语义（与 nwwmvp._confirm_consistency_closure 的「作者确认」区分）：
      - 仅在「复检已清零」时落盘 closed=true —— 即 cons 为 ok 且 open_s1_s2_count == 0。
      - 未清零（open_s1_s2_count > 0）→ 绝不落盘，返回 ok=False（真硬伤须回滚，禁止装闭环）。
      - 检测未完成（cons.ok=False）→ 绝不落盘（检查都没跑完，不能当作已闭环）。
      - confirmed_by 标 system_autoclose，与作者 web_user_confirm 区分，审计可追溯。
      - source_kind 同为 system_autoclose；evidence 携带 {review_record,
        review_record_sha256, recheck_open_s1_s2}（C-CLOSURE@2）：自动来源必须能指回
        当次检测记录，读侧（nww_track_commit.verify_consistency_closure）在证据与
        当前记录不符时拒绝提交。
      - 幂等：已存在 closed=true 不覆盖（保持首次闭环的时间与身份）。

    返回 {"ok": bool, "closed": bool, "closure_path": str|None, "detail": str}。
    失败一律 fail-open（返回 ok=False，不抛异常，不阻断主链——未闭环时 A-3 会兜底）。
    """
    if not _auto_closure_enabled():
        return {"ok": False, "closed": False, "closure_path": None,
                "detail": "自动闭环已关闭（NWW_AUTO_CLOSURE=0）"}
    # 前置：检测必须完成，且 S1/S2 已清零
    if not cons.get("ok"):
        return {"ok": False, "closed": False, "closure_path": None,
                "detail": "一致性检测未完成，不落闭环"}
    open_count = int(cons.get("open_s1_s2_count") or 0)
    if open_count > 0:
        return {"ok": False, "closed": False, "closure_path": None,
                "detail": f"存在未闭环 S1/S2（{open_count}），禁止自动闭环"}
    closure_dir = book / _TRACKING_DIR / "一致性核验"
    closure_path = closure_dir / f"第{chapter_id:03d}章.json"
    # 幂等：已 closed=true 不覆盖
    if closure_path.is_file():
        try:
            existing = json.loads(closure_path.read_text(encoding="utf-8"))
            if isinstance(existing, dict) and existing.get("closed") is True:
                return {"ok": True, "closed": True,
                        "closure_path": str(closure_path.relative_to(book)),
                        "detail": "已闭环（幂等，不覆盖）"}
        except (json.JSONDecodeError, OSError):
            pass  # 脏文件 → 按不存在处理，允许重写
    # 落盘（ISO 带时区，对齐契约）
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    # C-CLOSURE@2（2026-11-13，R5）：自动来源必须能指回检测证据 —— review_record 统一为
    # 相对 book 的 posix 路径（绝对路径先 relative_to(book)，失败时原样保存），
    # sha256 取记录文件字节，recheck_open_s1_s2 取本次复检的 S1/S2 未闭环数。
    # 读侧（nww_track_commit.verify_consistency_closure）在证据与当前记录不符时拒绝提交。
    raw_rec = str(cons.get("record_path") or "")
    review_record = raw_rec.replace("\\", "/")
    if raw_rec:
        try:
            _rp = Path(raw_rec)
            if _rp.is_absolute():
                review_record = _rp.resolve().relative_to(book.resolve()).as_posix()
            else:
                review_record = _rp.as_posix().replace("\\", "/")
        except (ValueError, OSError):
            review_record = raw_rec.replace("\\", "/")
    record_sha = ""
    if review_record:
        try:
            record_sha = hashlib.sha256((book / review_record).read_bytes()).hexdigest()
        except OSError:
            record_sha = ""
    record = {
        "chapter": chapter_id,
        "closed": True,
        "open_s1_s2": [],
        "updated_at": now_iso,
        "confirmed_by": _CONFIRMED_BY_AUTOCLOSE,
        "source_kind": _CONFIRMED_BY_AUTOCLOSE,
        "evidence": {
            "review_record": review_record,
            "review_record_sha256": record_sha,
            "recheck_open_s1_s2": open_count,
        },
    }
    try:
        closure_dir.mkdir(parents=True, exist_ok=True)
        closure_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "closed": False, "closure_path": None,
                "detail": f"闭环落盘失败: {exc}"}
    return {"ok": True, "closed": True,
            "closure_path": str(closure_path.relative_to(book)),
            "detail": "自动闭环已落盘（system_autoclose）"}


# ---- D3 审稿闭环（score → 宿主 LLM 打分 → after-score → [revise] 循环终态 + quality_meta.reviewed 条件写入） ----
_REVIEW_PASS_SCORE = 85  # 与 nww_review_cycle.PASS_SCORE 对齐（脚本内硬编码，此处仅用于 Web 侧判定）
_REVIEW_DIMENSION_FLOOR = 70  # 与 nww_review_cycle.DIMENSION_FLOOR 对齐（维度地板=硬约束，外脑 2026-10-19 边界项3）
_REVIEW_MAX_ROUNDS = 2

# C-REVIEW-ORDER@1：审稿循环内 enforce 前置开关（默认开启）。
# 默认：每轮 revise 之后立刻 enforce，使下一轮 score 快照读到的就是清洗后正文，
# 且 finalize 冻结的版本必然已 enforce（finalize 之后不再写正文）。
# NWW_REVIEW_INLINE_ENFORCE ∈ {0,false,off,no} → 回到「循环结束后统一 enforce」旧顺序（回滚态）。
_FILTER_REVIEW_INLINE_ENFORCE = "NWW_REVIEW_INLINE_ENFORCE"


def _review_inline_enforce_enabled() -> bool:
    """审稿循环内 enforce 前置开关：置 0/false/off/no → 回到「循环后统一 enforce」旧顺序。"""
    return os.environ.get(_FILTER_REVIEW_INLINE_ENFORCE, "").strip().lower() not in (
        "0", "false", "off", "no",
    )


def _review_floor_violations(book: Path, chapter_id: int, review_id: str | None,
                             dry_run: bool) -> list[str]:
    """维度地板硬约束检测（外脑 2026-10-19 边界项3）：best 轮 scores 中任一维度 < 70。

    数据源 = 审稿评分文件（dry_run 时为本次 execution 的 artifact root，与
    nww_review_cycle.score_dir 同源）。review_id 存在时只看该 review 的轮次
    （与 finalize 的 rounds 过滤一致，防历史任务高分轮掩盖本次触底；过滤后为空
    回退全部，同 finalize 兼容语义）。best 轮无 scores 明细（存量旧格式）/
    文件缺失或损坏 → []（无法判定，维持旧行为——硬阻断由 commit 侧评分门兜底）。
    """
    if dry_run:
        score_file = _dry_run_sandbox(book, review_id) / "评分" / f"第{chapter_id:03d}章.json"
    else:
        score_file = book / "追踪" / "评分" / f"第{chapter_id:03d}章.json"
    try:
        data = json.loads(score_file.read_text(encoding="utf-8"))
        rounds = data.get("rounds") if isinstance(data, dict) else data
        if not isinstance(rounds, list):
            return []
        rounds = [r for r in rounds if isinstance(r, dict)
                  and (not review_id or r.get("review_id") == review_id)] \
            or [r for r in rounds if isinstance(r, dict)]
        if not rounds:
            return []
        best_entry = max(rounds, key=lambda r: float(r.get("total", 0)))
        scores = best_entry.get("scores")
        if not isinstance(scores, dict):
            return []
        return sorted(f"{k}={v:g}" for k, v in scores.items()
                      if isinstance(v, (int, float)) and not isinstance(v, bool)
                      and v < _REVIEW_DIMENSION_FLOOR)
    except Exception:
        return []


def _promise_violations(book: Path, chapter_id: int, review_id: str | None,
                        dry_run: bool) -> list[str]:
    """闸2·细纲层镜像（2026-10-25 三层证据验收裁决）：best 轮 promise_audit 中
    delivered=false 的承诺。数据源/过滤语义与 _review_floor_violations 完全同构
    （dry_run → artifact root；review_id 过滤防历史轮次混淆；缺数据 → [] 兼容）。"""
    if dry_run:
        score_file = _dry_run_sandbox(book, review_id) / "评分" / f"第{chapter_id:03d}章.json"
    else:
        score_file = book / "追踪" / "评分" / f"第{chapter_id:03d}章.json"
    try:
        data = json.loads(score_file.read_text(encoding="utf-8"))
        rounds = data.get("rounds") if isinstance(data, dict) else data
        if not isinstance(rounds, list):
            return []
        rounds = [r for r in rounds if isinstance(r, dict)
                  and (not review_id or r.get("review_id") == review_id)] \
            or [r for r in rounds if isinstance(r, dict)]
        if not rounds:
            return []
        best_entry = max(rounds, key=lambda r: float(r.get("total", 0)))
        pa = best_entry.get("promise_audit")
        if not isinstance(pa, list):
            return []
        return [f"{p.get('outline_item', '')[:50]}（证据: {p.get('evidence', '') or '无'}）"
                for p in pa if isinstance(p, dict) and p.get("outline_item") and p.get("delivered") is False]
    except Exception:
        return []


def _write_quality_meta(draft_path: Path, **fields) -> None:
    """写入 delta.quality_meta 字段（参照 nww_post_write.write_meta_enforced 的事务定位逻辑）。"""
    data = json.loads(draft_path.read_text(encoding="utf-8"))
    tx = data.get("transaction", data) if isinstance(data, dict) else data
    delta = tx.setdefault("delta", {})
    if not isinstance(delta, dict):
        delta = {}
        tx["delta"] = delta
    qm = delta.setdefault("quality_meta", {})
    if not isinstance(qm, dict):
        qm = {}
        delta["quality_meta"] = qm
    qm.update(fields)
    draft_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _rebind_body_quality_meta(draft_path: Path, body_path: Path) -> None:
    """非审稿链（deslop/polish/revise）改完正文后，把质量指纹重绑到当前正文。

    背景（2026-11-01 实锤）：第1章 00:04 L2 审稿 86 分提交，00:12 deslop 又改了正文
    （2126→2141→2124）。delta 草稿整份重建把审稿指纹抹干净 —— 留痕没了，就再没有
    任何东西能证明「这章审过、几分、审的是哪一版」。

    本函数只做「如实记账」，不伪造审稿结论：
      - final_hash / target_body_md5 重绑当前正文 → 指纹描述的就是盘上这一版；
        append 模式下 commit 现算 commit_hash 与 final_hash 不等即 BLOCK（闸门有效）；
        revision 模式 A-5 豁免（`nww_track_commit.py:3766`），这里的作用是「留痕不说谎」；
      - review_status="stale"（审稿分对应的是本次改动前的正文）；
      - 移除 reviewed_hash：它是旧正文的哈希，留着会让 append 侧判 reviewed≠final 而 BLOCK。
    未审稿过（reviewed 缺失）→ 什么都不做，保持存量行为；异常静默跳过，不阻断流水线。
    只使用 nww_track_commit.normalize_quality_meta 白名单里已有的键。

    ★调用时机：必须放在 `step_post_write` **成功之后**。post_write 内 Step1 的
    nww_enforce.py 会再改一次正文（引号风格/markdown 剥离/结尾泄漏清理），
    post-commit 的复扫（step_deslop）也必须走 `check_only=True` 只读 ——
    实测 2026-11-01 副本 e2e：重绑放在 post_write 之前、或复扫仍写回，
    终态 final_hash 都与盘上正文不等（闸门报「冻结后正文已被改写」）。
    """
    try:
        if not draft_path.is_file() or not body_path.is_file():
            return
        data = json.loads(draft_path.read_text(encoding="utf-8"))
        tx = data.get("transaction", data) if isinstance(data, dict) else data
        delta = tx.get("delta") if isinstance(tx, dict) else None
        if not isinstance(delta, dict):
            return
        qm = delta.get("quality_meta")
        if not isinstance(qm, dict) or not qm.get("reviewed"):
            return
        sha = _sha256_of(body_path)
        if not sha:
            return
        sys_keys = _sys_quality_meta_keys()
        _rebound = {
            "final_hash": sha,
            "target_body_md5": hashlib.md5(body_path.read_bytes()).hexdigest(),
            "review_status": "stale",
        }
        for _k, _v in _rebound.items():
            if _k in sys_keys:
                qm[_k] = _v
        if "reviewed_hash" in sys_keys:
            qm.pop("reviewed_hash", None)
        draft_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"[QUALITY_META] 正文指纹重绑失败（忽略，不阻断）: {exc}")


def _review_integrity_status(chapter_path: Path, quality_meta: dict | None) -> dict:
    """D1 Review Integrity（只读 audit，不写回）：判定某条 review 评分对应当前正文的可信性。

    fresh        : target_body_md5 == current_body_md5（这版正文就是被评正文）
    stale        : target_body_md5 存在但 != 当前正文（review 后正文已变）
    unverifiable : 有 review_score 但无 target_body_md5（历史记录，无法证明对应版本）
    no_review    : 无 review_score。
    纯计算，不修改任何文件；不进 commit gate。
    """
    qm = quality_meta or {}
    score = qm.get("review_score")
    target_md5 = qm.get("target_body_md5")
    cur_md5 = hashlib.md5(chapter_path.read_bytes()).hexdigest() if chapter_path.is_file() else ""
    if score is None:
        status, reason = "no_review", "no review_score"
    elif not target_md5:
        status, reason = "unverifiable", "missing target_body_md5"
    elif target_md5 == cur_md5:
        status, reason = "fresh", "match"
    else:
        status, reason = "stale", "body changed since review"
    return {"review_status": status, "target_body_md5": target_md5,
            "current_body_md5": cur_md5, "reason": reason}


def _score_reference_block(book: Path, chapter_id: int) -> str:
    """把审稿打分必需的参考材料**内联**进打分 prompt（2026-11-08）。

    score_prompt 由脚本生成，只给「细纲路径」与「bible.era_constraints」的引用；而打分通道
    是纯文本 LLM 调用（无 Read/Glob/Grep），审稿据此读不到任何文件 → I-I 细纲兑现度与
    promise_audit 无据可判。实测（第2章）审稿只能给保守 78 分，并在 comment 里自陈
    「因无法读取细纲，I-I 只能基于正文自洽性判定，证据不足」。写的是什么就按什么判：
    材料内联，缺失也不编造，只标注缺失事实。"""
    parts: list[str] = []

    outline_file = _find_outline_file(book, chapter_id)
    outline_text = ""
    if outline_file is not None and outline_file.is_file():
        try:
            outline_text = outline_file.read_text(encoding="utf-8")
        except OSError:
            outline_text = ""
    if outline_text.strip():
        parts.append("【本章细纲原文（已内联，无需读取任何文件；I-I 维度与 promise_audit 以此逐条核对）】\n"
                     f"{outline_text[:12000]}")
    else:
        parts.append("【本章细纲原文】未找到细纲文件——I-I 维度按 0 分计并标记 BLOCK。")

    try:
        bible = json.loads((book / "bible.json").read_text(encoding="utf-8"))
        era = bible.get("era_constraints") if isinstance(bible, dict) else None
    except (OSError, ValueError, TypeError):
        era = None
    if isinstance(era, dict) and era:
        parts.append("【bible.era_constraints（已内联；I-E 维度以此档位/规则判定）】\n"
                     f"{json.dumps(era, ensure_ascii=False)[:4000]}")
    return "\n\n".join(parts)


# 审稿打分失败的真实归因（2026-11-08）：无效样本此前只报「模型输出泥沙/截断」，
# 而实测第2章连续失败的真因是「agnes 端点网络错误」（代理瞬时故障）。通道错误
# 与模型输出错误是完全不同的处置方向（前者等通道恢复重跑，后者改 prompt/预算），
# 必须在回执里如实区分。用 ContextVar 承载，避免为诊断信息改动函数签名
# （tests/test_review_invalid_sample.py 等处按 5 个位置参数 monkeypatch 了这两个函数）。
_SCORE_DIAG: contextvars.ContextVar = contextvars.ContextVar("nww_score_diag", default=None)

# 通道级（非模型输出级）失败关键词：命中即按「基础设施故障」处置。
_TRANSPORT_ERR_MARKS = (
    "网络错误", "网络请求失败", "超时", "TIMEOUT", "Connection", "SSL",
    "HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504",
)


def _is_transport_error(err: str) -> bool:
    """该失败是否属于通道级（网络/代理/端点 HTTP 故障），而非模型输出不可解析。"""
    text = str(err or "")
    return any(m in text for m in _TRANSPORT_ERR_MARKS)


def _score_diag() -> dict:
    """取（必要时新建）当前上下文的打分诊断槽。"""
    diag = _SCORE_DIAG.get()
    if diag is None:
        diag = {}
        _SCORE_DIAG.set(diag)
    return diag


def _review_score_once(book: Path, chapter_id: int, prose_path: Path, score_prompt: str,
                       round_num: int) -> tuple[float | None, dict | None, list | None]:
    """让 LLM 按 score_prompt 打分，返回 (综合分 total, 逐维度分 dict | None, 细纲承诺核对 list | None)。

    total/维度分/承诺核对同源解析（同一份 LLM 输出）。scores 供 after-score --scores 透传 →
    weak_dimensions 定向修订；promise_audit 供 --promise-audit 透传 → 闸2·细纲层
    （delivered=false 即证据不通过，与分数无关）。LLM 未输出对应字段时为 None，退化为旧行为。

    ★ 2026-11-01（无效样本语义，外脑审查后修订）：两次都解析不出 total → 返回
    ``(None, None, None)``，**不再**返回 0.0。0.0 会让「模型没输出」被当成「正文 0 分」
    记进 after-score 的一轮分数，污染 best_score / review_rounds 记账，并把审稿失败
    混成质量失败。调用方（_review_score_stable / step_review_cycle）据 None 判无效样本。

    ★ 2026-11-08（失败归因）：每次失败都把**真实原因**（通道网络错误 / 超时 / 输出
    不可解析+尾部样本）写进 `_score_diag()["last_error"]`，供上层如实报因；通道级
    失败先短退避再重试（瞬时抖动立刻重试只会再撞同一堵墙）。"""
    prose_text = prose_path.read_text(encoding="utf-8")
    agent = registry.get_agent("nww-reviewer")
    prompt = (
        f"{score_prompt}\n\n"
        f"{_score_reference_block(book, chapter_id)}\n\n"
        f"【本章正文】\n{prose_text[:10000]}\n\n"
        "请按上述维度打分，只输出一个 JSON 对象：\n"
        "1. total：0-100 综合分（**必须是 JSON 的第一个字段**，写在 scores/promise_audit 之前；"
        "输出过长被截断时这是唯一能保住综合分的方式）；\n"
        "2. scores：各维度代码→0-100 数字，如 {\"I-A\": 85, \"I-I\": 80}；\n"
        "3. promise_audit：细纲承诺逐条核对数组，每条 "
        "{\"outline_item\": \"<细纲条目原文>\", \"delivered\": true/false, \"evidence\": \"<正文位置或缺失说明>\"}；"
        "对照打分 prompt 里列出的细纲逐条核对，禁止省略条目；"
        "4. era_evidence：I-E<90 时必须输出数组，每条 "
        "{\"word\": \"<现代词>\", \"location\": \"<正文位置>\", \"channel\": \"<旁白|主角对白|主角内心|土著对白|土著内心>\", "
        "\"speaker\": \"<说话人>\", \"why\": \"<违反哪条语域规则>\"}；"
        "判定口径 = 本书 bible.era_constraints 的 register_mode 档位（strict/balanced/modern）"
        "对应规则，详见打分 prompt 的 I-E 维度强制要求。\n\n"
        # 2026-11-08：nww-reviewer 方法论里写着 tools:[Read,Glob,Grep] 与「读取参考文件」，
        # 模型据此以为要先去读 bible/细纲/追踪，实测输出「我需要读取参考文件来进行全面的评分」
        # 纯文字、无 JSON（992 字符，finish_reason=stop）→ 被算作无效样本。打分不需要文件系统，
        # 必须显式堵住这条路径，否则每次打分都可能先烧掉一次空产出。
        "【执行约束·必须遵守】你现在**没有**任何文件读取/搜索权限（Read/Glob/Grep 均不可用），"
        "也不存在可读取的文件；打分所需的全部材料（维度、细纲条目、本章正文）已内联在本条消息中。"
        "禁止输出「我需要读取…」「让我先读取…」之类的过程说明，禁止任何工具调用语法，"
        "从第一个字符开始直接输出 JSON 对象。"
    )
    # 2026-10-24 稳定性：Ornith 偶发输出工具调用泥沙/无 JSON → 0.0 直判审稿失败
    # 全链回滚（第2章 note-fix 实测）。改为：解析失败先重试一次重申输出契约，仍失败才 0.0。
    def _ask(hint: str, err_out: list) -> str:
        return _run_agent_bounded(agent, f"审稿打分 第{round_num}轮",
                                  prompt + hint, timeout=480.0,
                                  temperature=0.2, max_tokens=4096, err_out=err_out)

    def _parse(text: str) -> tuple[float, dict | None, list | None] | None:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.S)
        data = None
        try:
            data = json.loads(cleaned)
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
        if isinstance(data, dict) and str(data.get("total", "")).strip():
            total = max(0.0, min(100.0, float(data.get("total", 0))))
            dims = None
            raw = data.get("scores")
            if isinstance(raw, dict):
                nums = {str(k): float(v) for k, v in raw.items()
                        if isinstance(v, (int, float)) and not isinstance(v, bool)}
                if nums:
                    dims = nums
            promise = None
            raw_pa = data.get("promise_audit")
            if isinstance(raw_pa, list) and raw_pa:
                promise = [p for p in raw_pa if isinstance(p, dict)] or None
            # 2026-11-08 空壳样本闸：实测模型被截断后重试会交出
            # {"total": 0, "scores": {}, "promise_audit": [], "era_evidence": []}（33 token），
            # 按旧逻辑这是一个「合法的 0 分」，会直接记进 rounds_scores 当质量 0 分。
            # 没有任何维度分且 total<=0 = 模型没做判定 → 判无效样本（None），不伪造分数。
            if total <= 0 and not dims:
                return None
            return total, dims, promise
        m = re.search(r'"total"\s*:\s*(\d+(?:\.\d+)?)', text)
        if m:
            _t = max(0.0, min(100.0, float(m.group(1))))
            return (None if _t <= 0 else (_t, None, None))
        return None

    for attempt in range(2):
        err_box: list[str] = []
        out = _ask("" if attempt == 0 else
                   "\n\n重申：直接输出单个 JSON 对象 {\"total\": <0-100>, "
                   "\"scores\": {\"<维度代码>\": <0-100>, ...}, "
                   "\"promise_audit\": [{\"outline_item\": \"...\", \"delivered\": true/false, \"evidence\": \"...\"}]}，"
                   "禁止任何工具调用语法（<function=…> 等），不要解释、不要分维度正文。",
                   err_box)
        parsed = _parse(out)
        if parsed is not None:
            return parsed
        # 如实记因：通道故障（err_box 有值）与输出不可解析必须区分开。
        diag = _score_diag()
        diag["last_error"] = (err_box[-1] if err_box else
                              f"输出不可解析（{len(out or '')} 字符，尾部样本：{(out or '')[-80:]!r}）")
        if attempt == 0 and _is_transport_error(diag["last_error"]):
            time.sleep(3.0)
    return None, None, None


def _review_score_stable(book: Path, chapter_id: int, prose_path: Path,
                         score_prompt: str, round_num: int) -> tuple[float | None, dict | None, list | None, dict]:
    """审稿打分稳定性采样（2026-10-24，全量重写第2章 84<85 差1分回滚案例）：
    单次 LLM 打分存在 ±3 分以上抖动（同稿实测 84 与 92 并存）。规则：
      首测 ≥ 及格线 或 ≤ 及格线-3（明确通过/明确不足）→ 单测即定；
      落入（及格线-3, 及格线）抖动带 → 再采 2 次取中位数。政策（85 线）不变。
    total/维度分/承诺核对取自同一份中位样本，保证 total↔scores 包络自洽（P0-4）。

    2026-11-01 无效样本语义（外脑审查后修订）：
      - 首测解析失败 → **同一 round_num 内**补测 1 次（不额外消耗审稿轮次，
        也不重复调用 after-score）；
      - 补测成功 → 单测定音（`rescued=True`，不再进抖动带二次采样，避免半死通道
        上叠加 480s×N 空转）；
      - 两次都无效 → 返回 total=None + `{"invalid": True}`，由 step_review_cycle
        直接判审稿失败（该样本不写进 rounds_scores）。"""
    def _score() -> tuple[float | None, dict | None, list | None]:
        return _review_score_once(book, chapter_id, prose_path, score_prompt, round_num)

    first, first_dims, first_pa = _score()
    if first is None:
        # 2026-11-08：首测无效若是通道级（网络/超时/5xx）→ 退避后再补测；瞬时抖动
        # 立刻重试会连撞同一堵墙（第2章实测：代理瞬断，两次立即重试全部失败）。
        _err1 = str(_score_diag().get("last_error") or "")
        if _is_transport_error(_err1):
            time.sleep(8.0)
        first, first_dims, first_pa = _score()
        if first is None:
            _err2 = str(_score_diag().get("last_error") or _err1)
            return None, None, None, {
                "samples": 0, "raw": [], "invalid": True,
                "reason": "同一轮连续 2 次打分均未产出可解析的 total",
                "last_error": _err2,
                "transport": _is_transport_error(_err2),
            }
        return first, first_dims, first_pa, {"samples": 1, "raw": [first], "rescued": True}
    if first >= _REVIEW_PASS_SCORE or first <= _REVIEW_PASS_SCORE - 3:
        return first, first_dims, first_pa, {"samples": 1, "raw": [first]}
    extra = [_score() for _ in range(2)]
    # 抖动带内若补采也全无效：保留首测单样本（不把 None 混进排序 / 中位数）
    valid_extra = [t for t in extra if t[0] is not None]
    samples = sorted([(first, first_dims, first_pa)] + valid_extra, key=lambda t: t[0])
    med_total, med_dims, med_pa = samples[len(samples) // 2]
    return med_total, med_dims, med_pa, {
        "samples": len(samples), "raw": [t[0] for t in samples],
        "dropped_invalid": len(extra) - len(valid_extra),
    }


def _scores_envelope_ok(total: float | None, dims: dict | None) -> bool:
    """镜像 nww_review_cycle P0-4 的 total↔scores 包络校验（同一公式）。
    不满足时不向 after-score 传 --scores（退化为仅 total 的旧行为），
    避免脚本侧 rc=2 拒收导致本轮分数未落盘、循环状态不一致。
    2026-11-01：total=None（无效样本）直接 False —— 不做 None 与数值比较。"""
    if total is None or not dims:
        return False
    valid = list(dims.values())
    lo, hi = min(valid), max(valid)
    mean = sum(valid) / len(valid)
    lo_env, hi_env = lo, max(hi, mean * 1.2)
    return lo_env <= total <= hi_env


_REVIEW_CONTROL_LINE = re.compile(
    r"^\s*(?:【审稿闭环[^】]*】|={10,}|-{10,})\s*$", re.MULTILINE)


def _strip_review_control_lines(text: str) -> str:
    """剥离修订 LLM 回显的审稿控制行（「【审稿闭环 · 第 N 轮 · …】」/分隔线）。

    实测《人间烟火道》第1章 L3 重写：修订输出把「【审稿闭环 · 第 2 轮 · 重新打分】」
    带进正文，step_normalize 不认这类中文控制行 → 污染随 commit 落盘，正文末尾
    出现系统元信息。这里在审稿修订写回前确定性剥离（只删整行控制行，不动正文）。
    """
    lines = [ln for ln in str(text or "").splitlines()
             if not _REVIEW_CONTROL_LINE.match(ln)]
    return "\n".join(lines).strip()


def _review_revise_once(book: Path, chapter_id: int, prose_path: Path, revise_prompt: str, round_num: int) -> None:
    """让 LLM 按 revise_prompt 修订正文，覆盖落盘。

    2026-09-22 字数治理：审稿修订实测会把 3988 字稿整体缩写成 1904 字（LLM 全稿改写
    体量锚定），且字数闸在 review_cycle 之前已查完 → 塌缩稿直入 commit。双层防线：
    ① prompt 篇幅铁律（修订=针对性修改，禁止缩写，≥90%）；② 落盘前确定性塌缩闸
    （新稿可见字 < 原稿 80% → 拒绝落盘保留原稿，本轮修订跳过，循环安全收敛）。
    """
    current = prose_path.read_text(encoding="utf-8")
    _cur_vis = len(re.sub(r"\s+", "", current))
    agent = registry.get_agent("nww-reviewer")
    prompt = (
        f"{revise_prompt}\n\n"
        f"【当前正文】\n{current[:10000]}\n\n"
        f"请按上述修订指令修订正文，输出完整修订后正文（以 # 第{chapter_id}章 开头）。\n"
        "【篇幅铁律】修订是针对性修改，不是缩写重写：修订后正文篇幅不得低于当前正文的 90%；"
        "禁止概括、合并、省略任何段落；未涉及修订指令的内容原样保留。"
    )
    out = _run_agent_bounded(agent, f"审稿修订 第{round_num}轮", prompt,
                             timeout=480.0, temperature=0.6,
                             max_tokens=max(8192, len(current) * 3))
    clean, _ = step_normalize(book, chapter_id, out)
    clean = _strip_review_control_lines(clean)
    _new_vis = len(re.sub(r"\s+", "", clean))
    if _new_vis < _cur_vis * 0.8:
        print(f"[REVIEW_REVISE] BLOCK 塌缩: {_cur_vis}→{_new_vis}字（<80%），保留原稿跳过本轮修订", flush=True)
        return
    prose_path.write_text(clean + "\n", encoding="utf-8")


def _llm_json_once(agent_name: str, task_label: str, prompt: str, max_tokens: int) -> object | None:
    """LLM 单次 JSON 调用（失败重试一次重申契约）；两次都解析不出 → None。"""
    agent = registry.get_agent(agent_name)

    def _ask(hint: str) -> str:
        return _run_agent_bounded(agent, task_label, prompt + hint,
                                  timeout=420.0, temperature=0.2, max_tokens=max_tokens)

    for attempt in range(2):
        out = _ask("" if attempt == 0 else
                   "\n\n重申：只输出合法 JSON（数组或对象），禁止任何工具调用语法（<function=…> 等），不要解释。")
        cleaned = out.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.S)
        try:
            data = json.loads(cleaned)
            if isinstance(data, (list, dict)):
                return data
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
    return None


def _acceptance_file(book: Path, chapter_id: int, dry_run: bool, review_id: str | None) -> Path:
    """作者要求验收文件：追踪/验收/第NNN章.json（dry_run → 本次 execution 的 sandbox，
    与评分/_review_snapshots 同一隔离原则：预览绝不写正式 追踪/）。"""
    root = _dry_run_sandbox(book, review_id) if dry_run else (book / _TRACKING_DIR)
    d = root / "验收"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"第{chapter_id:03d}章.json"


def _era_constitution_block(book: Path) -> str:
    """语域宪法块（作者要求拆条/验收的对照基准，2026-10-25 事故复盘）。

    事故形态：重写 note 里「防穿越规则（现代词仅内心OS）」被拆成「遮瑕膏/粉底膏
    不得出现在对话与旁白」——与本书前提（章名即遮瑕膏、细纲明写土著买遮瑕膏、
    bible 宪法明示商品名可指名交易）直接矛盾 → 每版正确遵循细纲的稿子必然挂闸。
    修复：拆条与验收都必须对照本块；条目与宪法冲突时按宪法校准口径。
    无 era_constraints → 空串（行为不变）。"""
    try:
        era = json.loads((book / "bible.json").read_text(encoding="utf-8")).get("era_constraints") or {}
    except Exception:  # noqa: BLE001
        return ""
    if not (isinstance(era, dict) and era.get("policy")):
        return ""
    mode = str(era.get("register_mode") or "strict")
    goods = "、".join(str(x) for x in (era.get("protagonist_brought") or [])[:12]) or "细纲明示的铺内货品"
    return (
        f"档位={mode}。命名传播链（bible.era_constraints.naming_propagation）：现代物品名与"
        f"诙谐现代词的首次命名权归主角——旁白（主角视角）与主角对白可指名；设定内商品名"
        f"（{goods}）同理：其他角色必须已亲历主角传播（本章或前章在场听到/看到价牌）才能"
        "使用该词，未传播的新客/旁观者凭空说出商品名或诙谐词=违规，应用替代词（黑水/凡膏/"
        "红条等，见 bible 替代词表）。宪法禁止现代概念词（双标/KPI/内卷等）与无来源现代器物"
        "进入旁白与土著口中，这些只允许主角内心独白与对白。金手指系统文本（面板/规章/采购表）"
        "用语合法。章名（标题行）属营销层可用商品名与诙谐词。"
    )


def _decompose_rewrite_note(book: Path, chapter_id: int, note: str, original_prose: str,
                            review_id: str | None, dry_run: bool) -> list[dict] | None:
    """闸2·作者要求层（一，2026-10-25 三层证据验收裁决）：把重写 note 拆成可核对的验收条目。

    规则：每条必须可对照正文判定做到/没做到（禁止「写得更好」类模糊表述），≤6 条；
    要求含糊时拆成最接近的可核对解释，宁窄勿宽。条目落 追踪/验收/第NNN章.json
    供写后验收与人工复核。LLM 拆解失败 → None（调用方 advisory 跳过：拆解失败
    不应杀掉整个重写，细纲/设定/手艺层仍在闸）。
    """
    _era = _era_constitution_block(book)
    _era_decompose = (
        f"\n【本书语域宪法（bible.era_constraints，条目涉「现代词/现代物品/穿帮/本土化」时必须按此校准）】\n{_era}\n"
        "规则：作者要求若与宪法或细纲前提冲突（例如把「防穿越」理解为禁止设定内商品名进入"
        "对话/旁白），必须按宪法把条目限定到正确口径——商品名合法可指名；约束对象是现代概念词"
        "与无来源现代器物（只许主角内心/对白）。禁止拆出与本书前提相矛盾的验收条目。\n"
    ) if _era else ""
    prompt = (
        f"书项目根: {book}\n目标章: 第{chapter_id}章\n\n"
        f"【作者重写要求（原文）】\n{note[:2000]}\n\n"
        f"【执行前正文（前 2000 字，仅供理解语境）】\n{original_prose[:2000]}\n"
        + _era_decompose +
        "\n任务：把作者的重写要求拆解为「写完后可逐条核对」的验收条目。规则：\n"
        "1. 每条必须可对照正文判定「做到/没做到」，禁止「写得更好」「更有张力」这类不可核对的表述；\n"
        "2. 最多 6 条，按重要性排序；要求含糊时拆成最接近的可核对解释，宁窄勿宽；\n"
        "3. 只输出 JSON 数组，每条形如 "
        "{\"item\": \"<可核对的验收条目>\", \"check\": \"<判定方法一句话>\"}。"
    )
    data = _llm_json_once("nww-reviewer", f"重写要求拆条目 第{chapter_id}章", prompt, 1536)
    items = None
    if isinstance(data, list):
        items = [
            {"item": str(d.get("item", "")).strip(), "check": str(d.get("check", "")).strip()}
            for d in data if isinstance(d, dict) and str(d.get("item", "")).strip()
        ][:6] or None
    # 确定性校准后处理（belt-and-braces，LLM 拆条仍可能照抄禁令口径）：
    # 条目文涉及「现代词/现代物品/器物」禁令且本书有商品白名单 → 附加传播链限定，
    # 防止「商品名不得出现在旁白/对话」这类与命名传播链口径矛盾的条目流入验收
    # （旁白=主角视角可指名；主角对白可命名；土著须已亲历传播，未传播新客用替代词）。
    if items and _era:
        _cal = ("（按语域宪法命名传播链校准：旁白=主角视角与主角对白可指名商品；其他角色须已亲历"
                "主角传播（本章/前章在场），未传播新客凭空说出商品名或诙谐词=违规、应用替代词；"
                "本条对旁白与主角台词不适用）")
        for it in items:
            t = it.get("item", "") + it.get("check", "")
            if any(k in t for k in ("现代词", "现代物品", "现代商品", "器物", "本土")) and \
                    any(b in t for b in ("禁", "不得", "不能", "未直接", "只允许", "仅")):
                it["item"] = it["item"] + _cal
    try:
        _acceptance_file(book, chapter_id, dry_run, review_id).write_text(json.dumps({
            "chapter": chapter_id, "note": note[:2000], "items": items,
            "review_id": review_id, "decomposed_at": int(time.time()),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001 — 记录失败不影响主链
        pass
    return items


def _count_prose_chars(path: Path) -> int:
    """正文纯字数（对齐编译 visible_chars_v1 口径）：去标题/ markdown 标记/ 空白。

    用于「字数」类作者要求验收的精确计数，替代把长文甩给 LLM 估算——LLM 数
    中文几千字误差可达 ~50%（曾误判 3000+ 字为 1600-1800，致整章误回滚）。
    """
    try:
        t = path.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return 0
    t = re.sub(r"^#.*$", "", t, flags=re.M)
    t = re.sub(r"[#>*`\-=_|]", "", t)
    t = re.sub(r"\s+", "", t)
    return len(t)


def _wordcount_target(it: dict) -> int | None:
    """若条目属「字数」类且能提取目标字数 N，返回 N；否则 None。"""
    text = f"{it.get('item', '')} {it.get('check', '')}"
    if "字数" not in text:
        return None
    m = re.search(r"(\d+)\s*字", text)
    return int(m.group(1)) if m else None


def _verify_rewrite_requirements(book: Path, chapter_id: int, prose_path: Path,
                                 items: list[dict], review_id: str | None,
                                 dry_run: bool) -> dict | None:
    """闸2·作者要求层（二）：逐条核对重写稿是否兑现作者要求。

    返回 None = 验证未完成（LLM 输出条数对不上等，调用方 fail-open advisory 继续，
    防格式噪声误杀——细纲/设定/手艺层仍在闸）；否则
    {"unaddressed": [{item, evidence}], "total": n, "addressed": m}。
    判定口径：正文有可指认的对应内容才算 addressed=true，沾边但没做到按 false。
    """
    items_block = "\n".join(
        f"{i + 1}. {it['item']}（判定方法: {it.get('check', '')}）"
        for i, it in enumerate(items))
    # 字数类条目：代码精确计数覆盖 LLM 估算（2026-11-04 修复误回滚）
    _prose_count = _count_prose_chars(prose_path)
    _wc_overrides: dict[int, tuple[bool, str]] = {}
    for i, it in enumerate(items):
        N = _wordcount_target(it)
        if N is not None and _prose_count > 0:
            # 字数类作者要求：±10% 浮动带（稳定规则，不随问题临时改动）。
            # 低于下限 → 需扩写；高于上限 → 需缩写。两侧都判「未兑现」并给出纠正方向，
            # 但不做硬阻断——硬阻断由独立字数闸门负责（上限仅 QUARANTINE/trim，不卡上线）。
            lo, hi = int(N * 0.9), int(N * 1.1)
            ok = lo <= _prose_count <= hi
            if not ok:
                action = "需扩写" if _prose_count < lo else "需缩写"
                verdict = f"未达标（{action}）"
            else:
                action = ""
                verdict = "达标"
            _wc_overrides[i] = (ok,
                f"代码精确计数：正文纯字数={_prose_count}，目标={N}±10%=[{lo},{hi}]，"
                f"{verdict}（已用代码计数覆盖 LLM 估算）")
    _era = _era_constitution_block(book)
    _era_verify = (
        f"\n【本书语域宪法（bible.era_constraints，判定口径基准）】\n{_era}\n"
        "冲突消解规则：某条验收条目若与宪法或细纲前提冲突（如要求设定内商品名不得出现在"
        "对话/旁白，而本书商品名按宪法可指名交易），该条按宪法口径判定——不得仅因商品名"
        "出现而判未兑现；并在 evidence 里注明「条目与语域宪法冲突，已按宪法校准口径」。\n"
    ) if _era else ""
    prompt = (
        f"书项目根: {book}\n目标章: 第{chapter_id}章\n\n"
        f"【重写验收条目（作者要求拆解）】\n{items_block}\n"
        + _era_verify +
        f"\n【重写后正文】\n{prose_path.read_text(encoding='utf-8')[:12000]}\n\n"
        "任务：逐条判定正文是否兑现作者要求。只输出 JSON 数组，条目顺序与验收条目一致、"
        "条数相同，每条形如 {\"item\": \"<原条目>\", \"addressed\": true/false, "
        "\"evidence\": \"<正文位置或缺失说明>\"}。"
        "判定标准：正文有可指认的对应内容才算 addressed=true；沾边但没做到按 false。"
    )
    data = _llm_json_once("nww-reviewer", f"重写要求验收 第{chapter_id}章", prompt, 2048)
    if not isinstance(data, list) or len(data) != len(items):
        return None
    # 字数类条目用代码精确计数结果覆盖 LLM 判定（保持条数一致，避免触发 None 保护）
    for i, (ok, ev) in _wc_overrides.items():
        if i < len(data) and isinstance(data[i], dict):
            data[i]["addressed"] = ok
            data[i]["evidence"] = ev
            data[i]["item"] = items[i]["item"]
    unaddressed = [
        {"item": str(d.get("item", "")), "evidence": str(d.get("evidence", ""))}
        for d in data if isinstance(d, dict) and not d.get("addressed")
    ]
    result = {"unaddressed": unaddressed, "total": len(items),
              "addressed": len(items) - len(unaddressed)}
    try:
        acc_path = _acceptance_file(book, chapter_id, dry_run, review_id)
        payload = json.loads(acc_path.read_text(encoding="utf-8")) if acc_path.is_file() else {}
        payload["verify"] = {**result, "verified_at": int(time.time())}
        acc_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return result


def _settings_context(book: Path, cap: int = 6000) -> str:
    """设定文件摘录（供一致性硬伤裁决对照）。世界观目录优先，总量截断，读失败忽略。"""
    parts: list[str] = []
    total = 0
    setting_files: list[Path] = []
    settings_dir = book / "设定"
    if settings_dir.is_dir():
        # 世界观/ 优先（硬伤多与规则/权限相关），其余设定次之
        setting_files = sorted(settings_dir.rglob("*.md"),
                               key=lambda p: ("世界观" not in p.parts, str(p)))
    for p in setting_files:
        try:
            t = p.read_text(encoding="utf-8")[:1500]
        except Exception:  # noqa: BLE001
            continue
        if total + len(t) > cap:
            break
        parts.append(f"--- {p.relative_to(book)} ---\n{t}")
        total += len(t)
    bible = book / "bible.json"
    if bible.is_file() and total < cap:
        try:
            t = bible.read_text(encoding="utf-8")[:2000]
            parts.append(f"--- bible.json ---\n{t}")
            total += len(t)
        except Exception:  # noqa: BLE001
            pass
    return "\n".join(parts) or "（设定目录为空或不可读）"


def _adjudicate_consistency_findings(book: Path, chapter_id: int,
                                     findings: list[dict]) -> list[dict] | None:
    """闸1 修复回路（一）：带设定原文复核检出条目，裁决 resolved / real。

    一致性 checker 运行时无工具，「待核对/证据不足」类条目由本步读设定裁决——
    系统检出的问题系统自己消解，不回抛给作者。裁决失败返回 None（调用方保守
    处理：全部按 real 修——宁可多修不可漏修）。"""
    findings_block = "\n".join(
        f"{i + 1}. [{f.get('level')}] {str(f.get('content', ''))[:400]}"
        for i, f in enumerate(findings))
    prompt = (
        f"书项目根: {book}\n目标章: 第{chapter_id}章\n\n"
        f"【设定文件摘录】\n{_settings_context(book)}\n\n"
        f"【一致性检查检出的硬伤条目】\n{findings_block}\n\n"
        "任务：逐条裁决真伪。判定标准：\n"
        "- resolved：依据设定摘录可证明无真矛盾（如「待核对/证据不足」类经核对已有依据）；\n"
        "- real：确属事实矛盾/规则漏洞/因果断裂，需要修改正文才能消除。\n"
        "拿不准按 real（宁可多修不可漏修）。只输出 JSON 对象 "
        "{\"judgments\": [{\"index\": 1, \"verdict\": \"resolved|real\", \"reason\": \"一句话\"}]}，"
        "index 对应条目序号，逐条都要给。"
    )
    data = _llm_json_once("nww-reviewer", f"一致性硬伤裁决 第{chapter_id}章", prompt, 1536)
    if not isinstance(data, dict):
        return None
    js = data.get("judgments")
    return js if isinstance(js, list) else None


def _repair_consistency_findings(book: Path, chapter_id: int, prose_path: Path,
                                 findings: list[dict], round_num: int) -> dict | None:
    """闸1 修复回路（二，2026-10-25 作者裁决：系统检出=系统修复，作者只输出意见）。

    一轮修复：① 裁决消解（_adjudicate_consistency_findings）；② 剩余 real 条目
    作为编号修复指令，LLM 定向修订正文（沿用 _review_revise_once 的塌缩闸）。
    返回 {"resolved": [...], "repaired": n}。裁决调用失败 → 保守全修。"""
    judgments = _adjudicate_consistency_findings(book, chapter_id, findings)
    real_idx: set[int] = set(range(len(findings)))
    resolved: list[str] = []
    if judgments is not None:
        real_idx = set()
        for j in judgments:
            if not isinstance(j, dict):
                continue
            try:
                idx = int(j.get("index", 0)) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(findings):
                if j.get("verdict") == "resolved":
                    resolved.append(f"{str(findings[idx].get('content', ''))[:80]}（{j.get('reason', '')}）")
                else:
                    real_idx.add(idx)
    instructions = []
    for i in sorted(real_idx):
        f = findings[i]
        instructions.append(f"{len(instructions) + 1}. [{f.get('level')}] {str(f.get('content', ''))[:500]}")
    repaired = 0
    if instructions:
        fix_prompt = (
            "【一致性硬伤修复指令（系统检出，必须逐条修复）】\n" + "\n".join(instructions)
            + "\n修复原则：最小改动修复矛盾，不改核心事件/人物状态/伏笔/因果；需要设定依据时"
            "按与现有设定最一致的方式保守处理；禁止缩写。")
        _review_revise_once(book, chapter_id, prose_path, fix_prompt, round_num)
        repaired = len(instructions)
    return {"resolved": resolved, "repaired": repaired}


def _draft_consistency_proposals(book: Path, chapter_id: int,
                                 findings: list[dict]) -> list[dict] | None:
    """闸1 修复回路（三）：修复轮耗尽仍未清零 → 系统为剩余硬伤起草可执行修复方案
    （含设定条目修订草案）。作者角色=对方案给意见/拍板；落笔仍由系统执行。
    设定文件绝不自动改写。失败返回 None（fail-open，剩余条目原文随报告可查）。"""
    findings_block = "\n".join(
        f"{i + 1}. [{f.get('level')}] {str(f.get('content', ''))[:400]}"
        for i, f in enumerate(findings))
    prompt = (
        f"书项目根: {book}\n目标章: 第{chapter_id}章\n\n"
        f"【设定文件摘录】\n{_settings_context(book)}\n\n"
        f"【自动修复 2 轮后仍存在的一致性硬伤】\n{findings_block}\n\n"
        "任务：为每条硬伤起草**可直接执行**的修复方案：\n"
        "- 正文可解的 → 给出修改要点（位置+改法）；\n"
        "- 需要设定依据的 → 给出设定条目修订文本草案（供作者审阅后由系统写入设定层）；\n"
        "- 涉及创作取舍的 → 给出 2 个候选方向及各自代价，供作者挑。\n"
        "只输出 JSON 数组 [{\"finding\": \"<条目摘要>\", \"proposal\": \"<可执行方案或设定修订草案>\"}]，"
        "逐条都要给。"
    )
    data = _llm_json_once("nww-reviewer", f"一致性修复方案起草 第{chapter_id}章", prompt, 2048)
    return data if isinstance(data, list) else None


def _append_consistency_repair_block(record_path: Path, trail: list[dict],
                                     proposals: list[dict] | None) -> None:
    """把修复轨迹与系统起草方案追加进审查记录（作者复核入口；失败静默）。"""
    try:
        data = json.loads(record_path.read_text(encoding="utf-8")) if record_path.is_file() else {}
        data["repair"] = {"trail": trail, "proposals": proposals,
                          "updated_at": int(time.time())}
        record_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def step_review_cycle(book: Path, chapter_id: int, prose_path: Path, draft_path: Path,
                      review_id: str | None = None, dry_run: bool = False) -> dict:
    book = book.resolve()
    """审稿闭环：score → 宿主 LLM 打分 → after-score → 需要修订则 revise，循环直至终态。

    流程控制（轮次上限/停手阈值/通过线）完全由 nww_review_cycle.py 判定，
    Web 不复制脚本阈值逻辑，只消费脚本输出。after-score 终态分支内部
    自动 print_finalize（打印结论）。
    P0-1：best→final 物理落盘唯一收口点在 phase_finalize；本函数只负责
    「判定应进入 finalize 时」触发它一次，并全程透传同一个 review_id，不自行 copy。
    """
    rounds_scores: list[float] = []
    score_meta: dict = {}
    passed = False
    # T10（2026-11-07）：是否已从 finalize 自身 stdout 解析到「是否通过: 是」。
    # 供 step_acceptance_and_commit 区分「真实 finalize 已收口」与「外部调用桩/旧调用方」，
    # 避免对未经本次终态解析的调用方新增失败面（旧路径保留）。
    finalize_passed = False
    # 外脑评审（2026-11-05 design review）：每轮自评后并行跑一次外脑独立打分，
    # 用于交叉验证自评偏差；外脑失败/超时不挂主链，降级为只用自评分。
    external_rounds: list[dict] = []
    # P0（2026-11-05）：finalize 的 best→final 字数观测记录，由 stdout 解析后透出给 stage。
    _wc_restore_rec: dict | None = None

    # —— 语域清扫（2026-10-25 shell 实测复盘：78<85，I-E=68 因旁白泄漏 KPI/新人）——
    # 审稿评分前跑确定性 era_check，对 HIGH 项（旁白现代词/无来源器物）做一次
    # **词级定向替换**修订。只给词级替换指令——小模型能可靠执行词级替换；
    # 多维整体修订会塌缩（04:44 实测 2688→874 被塌缩闸拦截，救援失效）。
    # 复用 _review_revise_once（内建塌缩闸）；MEDIUM 项（对话待判说话人）留给
    # 审稿 I-E 归属终判，不做预改。异常静默跳过（清扫失败不挂审稿主链）。
    try:
        from .era_check_adapter import era_check_observe
        _obs = era_check_observe(book, chapter_id, prose_path.read_text(encoding="utf-8"))
        _hi = [h for h in (_obs.get("hits") or []) if h.get("raw_severity") == "HIGH"]
        if _hi:
            _lines = []
            for h in _hi[:10]:
                _lines.append(f"- 「{h.get('term')}」（{h.get('channel')}，上下文：{str(h.get('reason'))[:60]}）")
            _era_fix = (
                "【语域清扫 · 词级定向替换（只改词，不动情节）】以下现代词出现在旁白/公共描述，"
                "违反修仙界语域，逐条替换为修真界说法：\n" + "\n".join(_lines) +
                "\n替换规则：职场/现代概念词→修真界等价说法（如 KPI→宗门考绩/画符定额；新人→新晋弟子；"
                "裁员→逐出宗门；加班→连轴苦修）；无来源现代器物→改写为修真界器物或删除该词。"
                "对话里的词一律不动（说话人合法性由审稿判定）。禁止删情节、禁止缩写、"
                "输出必须是完整正文且篇幅不低于原稿 95%。"
            )
            print(f"[ERA_CLEAN] {len(_hi)} 处旁白语域违规，执行词级清扫", flush=True)
            _review_revise_once(book, chapter_id, prose_path, _era_fix, 0)
    except Exception as _e:  # noqa: BLE001 — 清扫层异常不挂审稿主链
        print(f"[ERA_CLEAN] 跳过（{ _e }）", flush=True)

    for round_num in range(1, _REVIEW_MAX_ROUNDS + 1):
        # 1. score phase：获取打分 prompt
        _body_args = (["--body", str(prose_path)] if dry_run else [])
        # P2（外脑 2026-10-07 裁定）：dry_run 显式传入 artifact root，脚本不得从 --body 隐式推导。
        # 评分/快照/判定产物落本次 execution 的 sandbox，正式 追踪/ 零变化。
        _dry_run_args = (["--dry-run-root", str(_dry_run_sandbox(book, review_id))] if dry_run else [])
        r_score = toolbox.run_script(
            "nww_review_cycle.py",
            ["--project", str(book), "--chapter", str(chapter_id), "--phase", "score",
             "--round", str(round_num)]
            + ([f"--review-id", review_id] if review_id else [])
            + _body_args,
            book, timeout=60,
        )
        if not r_score["ok"]:
            return {"ok": False, "detail": f"score phase 失败: {r_score['stderr'][:200]}"}
        score_prompt = r_score["stdout"]

        # 2. LLM 打分（2026-10-24 稳定性采样：分数落在及格线 ±3 抖动带内 →
        #    再采 2 次取中位数，滤掉审稿 LLM 的单次方差；实测同稿 84/92/92。
        #    及格线 85 不变，不放宽政策，只滤噪声。）
        total, _rv_dims, _rv_promise, _rv_meta = _review_score_stable(book, chapter_id, prose_path, score_prompt, round_num)
        # 2026-11-01 无效样本（外脑审查后修订）：连续两次都解析不出 total（模型输出
        # 工具调用泥沙/通道截断）→ 直接判审稿失败，**不进 after-score**。否则一轮无效
        # 样本会被记成 0 分，既污染 best_score/rounds 记账，也把「审稿通道坏」误报成
        # 「正文质量 0 分」。已打过的轮次分数原样保留在返回值里，便于归因。
        if total is None:
            # 2026-11-08 如实归因：把真实失败原因（通道网络错误 / 输出不可解析+尾部样本）
            # 写进 detail。此前统一写「模型返回工具调用泥沙/截断」——第2章实测真因是
            # agnes 端点网络错误（代理瞬断），错误的归因会把排障方向引到模型侧。
            _why = str((_rv_meta or {}).get("last_error") or "")
            if _is_transport_error(_why) or (_rv_meta or {}).get("transport"):
                _hint = ("这是**评审通道故障**（网络/代理/端点不可达），不是正文质量问题；"
                         "正文未被判 0 分、未被标记 reviewed。通道恢复后重跑即可。")
            else:
                _hint = ("请检查 nww-reviewer 端点/输出（常见原因：模型返回工具调用泥沙、"
                         "通道截断、max_tokens 不足）。")
            return {
                "ok": False,
                "passed": passed,
                "best_score": max(rounds_scores, default=0),
                "rounds": len(rounds_scores),
                "scores": rounds_scores,
                "score_meta": _rv_meta,
                "detail": (f"审稿打分无效：第{round_num}轮连续 2 次均未产出可解析的 total"
                           f"（该无效样本未记账，已记分数={rounds_scores or '无'}）。"
                           f"真实原因：{_why or '未知'}。{_hint}"),
            }
        rounds_scores.append(total)
        score_meta = _rv_meta

        # 2.5 外脑独立评审（与 writer 不同模型家族）。失败降级，不挂主链。
        try:
            from .external_review import review_external
            _ext = review_external(score_prompt, prose_path.read_text(encoding="utf-8"),
                                   chapter_id=chapter_id)
            if _ext is not None:
                external_rounds.append({
                    "round": round_num,
                    "bridge": _ext.get("bridge"),
                    "pool_id": _ext.get("pool_id"),
                    "total": _ext.get("total"),
                    "scores": _ext.get("scores"),
                    "deductions": _ext.get("deductions") or [],
                    "promise_audit": _ext.get("promise_audit"),
                })
                print(f"[EXTERNAL_REVIEW] round={round_num} bridge={_ext.get('bridge')} "
                      f"total={_ext.get('total'):.1f} deductions={len(_ext.get('deductions') or [])}",
                      flush=True)
            else:
                print(f"[EXTERNAL_REVIEW] round={round_num} 所有外脑桥不可用，降级为只用自评分",
                      flush=True)
        except Exception as _e:  # noqa: BLE001 — 外脑失败不挂主链
            print(f"[EXTERNAL_REVIEW] round={round_num} 异常降级: {_e}", flush=True)

        # 3. after-score phase：脚本自动记录分数 + 判定终态/继续修订
        #    --scores 透传同源采样的逐维度分 → 落盘 scores/weak_dimensions →
        #    修订 prompt 定向弱维度。包络不过（total↔维度分量纲错乱）时不传，
        #    退化为仅 total 旧行为，不在 Web 链引入 rc=2 拒收新失败面。
        #    --promise-audit 透传细纲承诺核对 → 闸2·细纲层（脚本判定，delivered=false
        #    即不通过与分数无关）；畸形/缺席由脚本归一化为 None（不闸）。
        _scores_args = (["--scores", json.dumps(_rv_dims, ensure_ascii=False)]
                        if _scores_envelope_ok(total, _rv_dims) else [])
        _promise_args = (["--promise-audit", json.dumps(_rv_promise, ensure_ascii=False)]
                         if isinstance(_rv_promise, list) and _rv_promise else [])
        r_after = toolbox.run_script(
            "nww_review_cycle.py",
            ["--project", str(book), "--chapter", str(chapter_id), "--phase", "after-score",
             "--score", str(total), "--round", str(round_num)]
            + ([f"--review-id", review_id] if review_id else [])
            + _scores_args
            + _promise_args
            + _body_args
            + _dry_run_args,
            book, timeout=60,
        )
        after_out = r_after.get("stdout", "")

        # 终态判定：after-score 内部在 pass/max_rounds/stop 三种分支里都调了 print_finalize
        if "是否通过: 是" in after_out:
            passed = True
            # P0-1：唯一 best→final 物理落盘收口点在 phase_finalize。
            # 本函数只负责触发 finalize 一次（带同一 review_id），不做任何 copy。
            r_fin = toolbox.run_script(
                "nww_review_cycle.py",
                ["--project", str(book), "--chapter", str(chapter_id), "--phase", "finalize"]
                + ([f"--review-id", review_id] if review_id else [])
                + _body_args
                + _dry_run_args,
                book, timeout=60,
            )
            if not r_fin["ok"]:
                return {"ok": False, "passed": True, "best_score": max(rounds_scores, default=0),
                        "rounds": len(rounds_scores),
                        "detail": f"finalize（best→final 落盘）失败: {r_fin['stderr'][:200]}"}
            # T10（2026-11-07）终态判定修正：finalize 会按 **best 轮**（argmax(round.total)）
            # 重新复判承诺闸/维度地板闸；判否时它只恢复 best 快照，**不写**
            # reviewed_hash / final_hash / finalized_at（未冻结交付哈希）。
            # 旧实现只看 r_fin["ok"]（进程退出码），把「finalize 判否」误当通过 →
            # 后续 commit 的 A-5 报「交付正文身份校验失败」（reviewed=final=… vs commit=…），
            # 把「审稿未收口」误表达成「正文身份不符」。这里必须解析 finalize 自身 stdout。
            _fin_out = r_fin.get("stdout") or ""
            if "是否通过: 否" in _fin_out:
                # 停止原因取 finalize stdout 的「停止原因:」行（取不到则回退原文标记）。
                # 该分支直接返回、不落 reviewed：与 finalize「只恢复 best 快照不标 reviewed」保持一致。
                _stop_reason = ""
                for _ln in _fin_out.splitlines():
                    _ln_stripped = _ln.strip()
                    if _ln_stripped.startswith("停止原因:"):
                        _stop_reason = _ln_stripped.split(":", 1)[1].strip()
                        break
                return {
                    "ok": False,
                    "passed": False,
                    "best_score": max(rounds_scores, default=0),
                    "rounds": len(rounds_scores),
                    "detail": ("审稿 finalize 未收口：best 轮未通过承诺/地板闸（"
                               + (_stop_reason or "是否通过: 否")
                               + "），已按 best-snapshot 保留最高分稿但未冻结交付哈希"),
                }
            if "是否通过: 是" not in _fin_out:
                # 异常输出（无终态判定）：
                #   - 输出为空，或已出现 finalize 契约锚点（「【审稿闭环 · 完成】」/「停止原因:」）
                #     却缺「是否通过」行 → 输出被截断/异常，保守判失败，绝不假装已冻结交付哈希。
                #   - 完全不含契约锚点（旧版脚本/外部调用桩）→ 无法判定，维持既有成功路径，
                #     作为回滚口，不引入新的失败面。
                if (_fin_out.strip() == ""
                        or "【审稿闭环 · 完成】" in _fin_out
                        or "停止原因:" in _fin_out):
                    return {
                        "ok": False,
                        "passed": False,
                        "best_score": max(rounds_scores, default=0),
                        "rounds": len(rounds_scores),
                        "detail": "审稿 finalize 未收口：finalize 未输出终态判定"
                                  "（是否通过: 是/否 均缺失），无法确认已冻结交付哈希",
                    }
            # finalize 已明确输出「是否通过: 是」→ 标记本次终态解析成功（供验收链闸门使用）。
            # 旧路径（无任何契约锚点、无法判定）保持 finalize_passed=False，
            # 验收链据此走既有分支，不引入新的失败面。
            if "是否通过: 是" in _fin_out:
                finalize_passed = True
            # P0：把脚本侧 [WC_TRACE] snapshot-restore 记录透出（解析失败不影响审稿结果）
            for _ln in (r_fin.get("stdout") or "").splitlines():
                if _ln.startswith("[WC_TRACE] snapshot-restore "):
                    try:
                        _wc_restore_rec = json.loads(_ln.split(" ", 2)[2])
                    except (ValueError, IndexError):
                        pass
            break
        if "是否通过: 否" in after_out:
            break  # 脚本已判定未通过（终态：最大轮次或净提升不足），停止

        # 4. 需要修订（脚本输出修订指令）：revise phase → LLM 修订正文 → 下一轮 score
        #    P0-2（外脑 2026-10-07 裁定）：补 --body + --dry-run-root，使 revise 的读取身份
        #    （评分/正文基准）与写入身份（sandbox body-preview）同属一个 artifact namespace；
        #    normal 模式下 _body_args/_dry_run_args 均为 []，行为不变。
        r_revise = toolbox.run_script(
            "nww_review_cycle.py",
            ["--project", str(book), "--chapter", str(chapter_id), "--phase", "revise", "--round", str(round_num)]
            + ([f"--review-id", review_id] if review_id else [])
            + _body_args
            + _dry_run_args,
            book, timeout=60,
        )
        if r_revise["ok"]:
            _revise_prompt_text = r_revise["stdout"]
            # 追加外脑 deductions 清单（2026-11-05）：外脑（不同模型家族）给出的具体
            # 扣分位置/原因/改法，比自评的维度码更可执行。拼在 revise prompt 末尾，
            # 不改变脚本原有输出格式。
            if external_rounds:
                _latest_ext = external_rounds[-1]
                _deds = _latest_ext.get("deductions") or []
                if _deds:
                    _ext_lines = ["", "【外脑评审 · 具体扣分点与改法（必须逐条处理）】"]
                    for i, d in enumerate(_deds, 1):
                        if not isinstance(d, dict):
                            continue
                        _dim = d.get("dim", "?")
                        _loc = d.get("location", "?")
                        _reason = d.get("reason", "")
                        _fix = d.get("fix_hint", "")
                        _gain = d.get("expected_gain", "")
                        _ext_lines.append(
                            f"{i}. [{_dim}] 位置：{_loc}\n"
                            f"   扣分原因：{_reason}\n"
                            f"   改法：{_fix}（预计 +{_gain} 分）"
                        )
                    _revise_prompt_text += "\n" + "\n".join(_ext_lines)
                    print(f"[EXTERNAL_REVIEW] 已把 {len(_deds)} 条外脑扣分点拼入 revise prompt",
                          flush=True)
            # 硬约束检测：每轮 revise 前先查数字锚/语域/承诺违规，拼进修订指令
            # （此前这些检测只在审稿循环结束后做，检测到问题也修不了——脱节）
            try:
                _prose_txt = prose_path.read_text(encoding="utf-8") if prose_path.is_file() else ""
                _hard_violations = (_numeric_anchor_violations(book, chapter_id, _prose_txt)
                                    + _promise_violations(book, chapter_id, review_id, dry_run))
                if _hard_violations:
                    _hard_lines = ["", "【硬约束修复（必须逐条修正，否则本章无法提交）】"]
                    for i, v in enumerate(_hard_violations[:5], 1):
                        _hard_lines.append(f"{i}. {v}")
                    _revise_prompt_text += "\n" + "\n".join(_hard_lines)
                    print(f"[REVIEW_REVISE] 第{round_num}轮已把 {len(_hard_violations)} 条硬约束违规拼入 revise prompt",
                          flush=True)
            except Exception as _he:
                print(f"[REVIEW_REVISE] 硬约束检测失败（不阻断）: {_he}", flush=True)
            _review_revise_once(book, chapter_id, prose_path, _revise_prompt_text, round_num)
            # C-REVIEW-ORDER@1：同一轮末尾（for 迭代结束前）立即 enforce —— 下一轮
            # phase score 打的快照读到的就是清洗后正文；finalize 之前正文已定稿。
            if _review_inline_enforce_enabled():
                try:
                    step_pre_review_enforce(book, chapter_id, prose_path, draft_path)
                    print(f"[REVIEW_CYCLE] 第{round_num}轮修订后 enforce 完成", flush=True)
                except Exception as _e:  # noqa: BLE001 — enforce 失败不阻断审稿主链
                    print(f"[REVIEW_CYCLE] 第{round_num}轮修订后 enforce 失败（不阻断）: {_e}",
                          flush=True)

    # 循环结束后统一跑一次 enforce —— 仅回滚态（NWW_REVIEW_INLINE_ENFORCE=0）执行。
    # 默认路径已在每轮 revise 后 enforce，finalize 成功后不得再写 prose_path。
    if not _review_inline_enforce_enabled():
        try:
            step_pre_review_enforce(book, chapter_id, prose_path, draft_path)  # 旧顺序（回滚态）
            print("[REVIEW_CYCLE] 审稿循环结束后统一 enforce 完成（回滚态）", flush=True)
        except Exception as _e:  # noqa: BLE001
            print(f"[REVIEW_CYCLE] 循环后 enforce 失败（不阻断）: {_e}", flush=True)

    # 5. 只有 passed=true 才写 quality_meta.reviewed=true（A-5 闸语义：通过审稿才算流程完整）
    # 白名单字段（必须与 nww_track_commit.normalize_quality_meta 的 require_known_keys 同步，
    # 否则 commit 会以「unsupported fields」BLOCK）：
    #   declared/observed/enforced/enforced_at/reviewed/reviewed_at/review_score/review_rounds
    #   + finalized/finalized_at/review_id（后者由 nww_review_cycle._write_review_meta 在 finalize 写）
    best = max(rounds_scores) if rounds_scores else 0.0

    # 把外脑评审记录写进评分文件（与 rounds 同级，供 commit 闸门交叉验证）。
    # 幂等：读现有文件 → 追加 external_rounds 顶层字段 → 写回；失败静默降级（不挂主链）。
    if external_rounds:
        try:
            _score_file = book / "追踪" / "评分" / f"第{chapter_id:03d}章.json"
            if _score_file.is_file():
                _sd = json.loads(_score_file.read_text(encoding="utf-8"))
                if isinstance(_sd, dict):
                    _sd["external_rounds"] = external_rounds
                    _sd["external_best"] = max((r.get("total", 0) for r in external_rounds), default=0)
                    _score_file.write_text(json.dumps(_sd, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as _e:  # noqa: BLE001
            print(f"[EXTERNAL_REVIEW] 写回评分文件失败（不挂主链）: {_e}", flush=True)

    # R-QC1（P1 语义解耦，2026-09-15 外脑批准）：reviewed 语义 = 审稿循环已执行并产生合法结果，
    # 不再绑定 passed。是否通过由 review_score 对照 _REVIEW_PASS_SCORE=85 表达。
    #   审稿循环真实打过分（一轮及以上）→ 确已执行并产出结果 → reviewed=true + score；
    #   若连一轮 score 都没跑（rounds_scores 为空），说明循环未完成、无合法结果可登记 → 不写 reviewed。
    #   （正常代码路径在 score/finalize 失败时会提前 return，到此处 rounds_scores 均为非空。）
    if passed or (rounds_scores and not passed):
        try:
            # D1 Review Integrity：绑定本次评分对应的正文版本 md5。
            _review_target_md5 = hashlib.md5(
                prose_path.read_bytes() if prose_path.is_file() else b"").hexdigest()
            _write_quality_meta(
                draft_path,
                reviewed=True,
                review_score=int(best),
                review_rounds=len(rounds_scores),
                reviewed_at=int(time.time()),
                target_body_md5=_review_target_md5,
                review_status="fresh",
            )
        except Exception as exc:  # noqa: BLE001 — meta 写入失败不阻断，但标记失败
            return {
                "ok": False,
                "passed": passed,
                "best_score": int(best),
                "rounds": len(rounds_scores),
                "detail": f"quality_meta 写入失败: {exc}",
            }

    # 外脑交叉验证（2026-11-05 design review）：
    #   自评 <85 但外脑（不同模型家族）>=85 → 自评太严/自评偏差， escalated 放行，
    #   不看运气卡进度；外脑也 <85 且已达最大轮次 → needs_human_review，不 BLOCK 只打标。
    external_best = max((r.get("total", 0) for r in external_rounds), default=0.0)
    escalated = False
    needs_human_review = False
    if not passed and rounds_scores:
        if external_rounds and external_best >= _REVIEW_PASS_SCORE:
            escalated = True
            passed = True  # 外脑认为达标，放行但留 escalated 标记
            print(f"[EXTERNAL_REVIEW] 自评 best={best:.1f}<{_REVIEW_PASS_SCORE}，"
                  f"但外脑 best={external_best:.1f}>={_REVIEW_PASS_SCORE} → escalated 放行",
                  flush=True)
        elif best < _REVIEW_PASS_SCORE and len(rounds_scores) >= _REVIEW_MAX_ROUNDS:
            needs_human_review = True
            print(f"[EXTERNAL_REVIEW] 自评 best={best:.1f}、外脑 best={external_best:.1f} 均未达标"
                  f" → needs_human_review（不 BLOCK，标记待人工裁决）", flush=True)

    return {
        "ok": True,
        "passed": passed,
        # T10（2026-11-07）：本函数是否已解析到 finalize 的「是否通过: 是」。
        # 仅真实 finalize 收口成功时为 True；供 step_acceptance_and_commit 的提交前
        # 质量元完整性检查使用（外部调用桩/旧调用方无此字段 → 保留旧路径）。
        "finalize_passed": finalize_passed,
        "best_score": best,
        "rounds": len(rounds_scores),
        "scores": rounds_scores,
        # P0（2026-11-05）：best→final 恢复前后字数观测（无记录时为 None）
        "wc_restore": _wc_restore_rec,
        # 末轮打分采样 meta（samples/raw），调用方透出采样细节用
        "score_meta": score_meta,
        # 维度地板硬约束（外脑 2026-10-19 边界项3）：passed=False 且 best 达标时，
        # 调用方据本字段区分「净提升停手放行」与「维度触底阻断」。
        "floor_violations": _review_floor_violations(book, chapter_id, review_id, dry_run),
        # 闸2·细纲层（2026-10-25 三层证据验收）：best 轮未兑现承诺，调用方与
        # floor_violations 同级阻断（证据验收不与印象分混算）。
        # P0-4 并入数字锚写后审计（确定性）：细纲数字承诺 vs 正文同单位数值比对，
        # 防「两块灵石被写成十块后由 promise_audit 用正文反推洗白」。
        "promise_violations": (_promise_violations(book, chapter_id, review_id, dry_run)
                               + _numeric_anchor_violations(
                                   book, chapter_id,
                                   prose_path.read_text(encoding="utf-8") if prose_path.is_file() else "")),
        # 外脑独立评审记录（2026-11-05）：与 writer 不同模型家族的交叉打分 + deductions 清单。
        # 空列表 = 外脑桥全部不可用，已降级为只用自评分。
        "external_rounds": external_rounds,
        "external_best": external_best,
        "escalated": escalated,
        "needs_human_review": needs_human_review,
    }


def _model_config_path() -> Path:
    """定位 model_config.json（与 era_check_adapter 同规则）：env > 仓库根/runtime/config > legacy。"""
    env = os.environ.get("NWW_MODEL_CONFIG_FILE", "").strip()
    if env:
        return Path(env)
    repo_root = Path(__file__).resolve().parents[4]
    new_path = repo_root / "runtime" / "config" / "model_config.json"
    if new_path.is_file():
        return new_path
    legacy = Path(__file__).resolve().parents[1] / "model_config.json"
    return legacy if legacy.is_file() else new_path


_AUTO_COMMIT_SWITCH = "NWW_AUTO_COMMIT"


def _auto_commit_enabled() -> bool:
    """读取 auto_commit 开关（C-AUTOC-1@1 读取语义）。

    优先级：
      1) env `NWW_AUTO_COMMIT` ∈ {0,false,off,no}（忽略大小写/空白）→ False（强制关闭，
         优先于配置文件；用于运维一键回滚）；
      2) 配置 `auto_commit.enabled is True` → True（T3 落盘后默认生效）；
      3) 缺键 / 非 dict / 值非 True / 配置不可读 → False（与改造前一致）。
    """
    _env = os.environ.get(_AUTO_COMMIT_SWITCH, "").strip().lower()
    if _env in ("0", "false", "off", "no"):
        return False
    try:
        cfg = json.loads(_model_config_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — 配置读取失败按未启用处理
        return False
    if not isinstance(cfg, dict):
        return False
    ac = cfg.get("auto_commit")
    return isinstance(ac, dict) and ac.get("enabled") is True


def _revision_conflict(book: Path, chapter_id: int) -> tuple[bool, int | None, int | None]:
    """判断该章 delta 是否为 commit 事务版本冲突（expected_state_revision ≠ 当前 state_revision）。

    外脑裁决（2026-09-17）定义的唯一可恢复原因 recoverable_reason=["state_revision_changed"]：
    仅当此判定成立才允许自动恢复；其余失败（paragraph_gate/continuity/enforce 等质量或状态闸）
    一律不得自动提交，避免把质量闸变成绕过通道。读取失败 → (False, None, None)。
    """
    try:
        delta = book / _TRACKING_DIR / f"{_DELTA_PREFIX}{chapter_id:03d}章.json"
        if not delta.is_file():
            return False, None, None
        tx = json.loads(delta.read_text(encoding="utf-8"))
        state_file = book / _TRACKING_DIR / "_tracking-state.json"
        if not state_file.is_file():
            return False, None, None
        sdata = json.loads(state_file.read_text(encoding="utf-8"))
        expected = tx.get("expected_state_revision")
        state_rev = sdata.get("state_revision")
        if not isinstance(expected, int) or not isinstance(state_rev, int):
            return False, None, None
        return expected != state_rev, expected, state_rev
    except Exception:  # noqa: BLE001 — 判定失败按不可恢复处理（保守回退 BLOCK）
        return False, None, None


def _realign_delta_revision(book: Path, chapter_id: int, state_rev: int) -> bool:
    """把该章 delta.expected_state_revision 对齐到当前 state_revision（幂等）；读写失败→False。"""
    try:
        delta = book / _TRACKING_DIR / f"{_DELTA_PREFIX}{chapter_id:03d}章.json"
        tx = json.loads(delta.read_text(encoding="utf-8"))
        tx["expected_state_revision"] = state_rev
        delta.write_text(json.dumps(tx, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:  # noqa: BLE001 — 对齐失败则不可恢复
        return False


def _log_auto_commit_audit(book: Path, chapter_id: int, *, trigger: str, reason: str, retry: bool,
                           old_revision: int | None = None, new_revision: int | None = None,
                           result: str = "attempted") -> None:
    """自动恢复审计标记（外脑裁决 2026-09-17 建议，观察增强，不影响架构）。

    区分正常 commit / 自动恢复 commit / 人工 commit，供未来排查溯源。fail-soft。
    """
    try:
        d = book / _TRACKING_DIR / "auto_commit"
        d.mkdir(parents=True, exist_ok=True)
        payload = {
            "chapter": chapter_id,
            "auto_commit_recovery": {
                "trigger": trigger,
                "reason": reason,
                "retry": retry,
                "old_revision": old_revision,
                "new_revision": new_revision,
                "result": result,
            },
            "recorded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        (d / f"第{chapter_id:03d}章.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001 — 审计失败不影响主链
        pass


def _try_auto_commit_dangling(book: Path, pending: int, last: int, progress=None) -> tuple[bool, str]:
    """写前闸扩展：对卡死的悬空章尝试自动提交，成功返回 (True, detail)，失败返回 (False, 原因)。

    P1 外脑裁定扩展（2026-09-17）：用户长期诉求"commit 是系统动作，不留尾巴卡住下一步"。
    悬空章 = 正文已落盘但未 commit（此前写链 post_write 被 commit"state changed"等 BLOCK
    留下）。本函数把"提交恢复"这个本由用户手动对悬空章「重写」完成的动作接入系统。

    C-AUTOC-2@1 分支顺序（新）：
      ① auto_commit 开关开启（env 强制关闭优先于配置）；
      ② 悬空章正文可定位（无正文成果则无从提交）；
      ③ 判定是否为**真·事务版本冲突**（expected_state_revision ≠ 当前 state_revision）：
         - 冲突 → 保持既有 recovery：一致性闭环已确认 → delta 树存在 → 对齐 revision
           → step_post_write(skip_enforce=True) → 成功清台账，失败台账不动；
         - 非冲突（质量闸/审稿未闭环等）→ 走 step_acceptance_and_commit 补跑完整验收，
           **仅 committed is True 才清台账放行**；不得直接 step_post_write（会绕过审稿/一致性闸）。
    成功 → 清该章恢复台账；任何失败 → 恢复台账不动，返回 False 保持原 BLOCK。
    绝不修改 A-3 gate / nww_track_commit.py / closure 契约 / packet。

    progress: 可选回调 `progress(stage_name, ok, detail)`，用于 AI 实况面板显示进度。
    """
    try:
        # ① 开关（env NWW_AUTO_COMMIT=0 强制关闭，优先于配置）
        if not _auto_commit_enabled():
            return False, "auto_commit 开关未开启"
        # ② 正文定位
        prose = _resolve_existing_body(book, pending)
        if prose is None or not prose.is_file():
            return False, f"第{pending}章正文不可定位"
        # ③ 可恢复原因白名单（外脑裁决）：仅事务版本冲突（expected≠state）走旧 recovery；
        #    其余（质量/连续性/enforce/审稿未闭环等）交给验收补跑，仍按原闸标准过闸。
        _conflict, _exp, _rev = _revision_conflict(book, pending)
        if not _conflict:
            res = step_acceptance_and_commit(
                book, pending,
                reason="悬空章非事务版本冲突，写链未跑完验收",
                by="auto_commit_recovery",
                progress=progress,
            )
            if isinstance(res, dict) and res.get("committed") is True:
                _clear_pending_commit_ledger(book, pending)
                _log_auto_commit_audit(book, pending, trigger="dangling_chapter",
                                       reason="acceptance_repair", retry=True, result="success")
                return True, f"第{pending}章已自动补跑验收并提交（last→{res.get('last_after')}）"
            _log_auto_commit_audit(book, pending, trigger="dangling_chapter",
                                   reason="acceptance_repair", retry=True, result="failed")
            _repair_detail = str((res or {}).get("detail") or "")[:400] if isinstance(res, dict) else ""
            return False, f"第{pending}章验收补跑未通过：{_repair_detail}"
        # ④ 真·事务版本冲突 → 保持既有 recovery 路径（前置检查只在此分支生效）
        #    a) 一致性闭环绕已确认（A-3 同路径，不写入不改动）
        closure = book / _TRACKING_DIR / "一致性核验" / f"第{pending:03d}章.json"
        if not closure.is_file():
            return False, f"第{pending}章缺一致性闭环绕（{closure.relative_to(book)}）"
        cdata = json.loads(closure.read_text(encoding="utf-8"))
        if cdata.get("closed") is not True:
            return False, f"第{pending}章一致性闭环尚未确认（closed≠true）"
        #    b) delta 草稿存在（nww_post_write commit 依赖）
        delta = book / _TRACKING_DIR / f"{_DELTA_PREFIX}{pending:03d}章.json"
        if not delta.is_file():
            return False, f"第{pending}章缺 delta 草稿（{delta.relative_to(book)}）"
        #    c) delta revision 对齐到当前 state_revision（幂等）
        if not _realign_delta_revision(book, pending, _rev):
            return False, f"第{pending}章 delta revision 对齐失败，不自动提交"
        #    d) 执行提交（skip_enforce=True，P0-3a：悬空章正文在上次写链已 enforce 过——
        #       新顺序 enforce 在 finalize 之前；再跑会改写正文触发 A-5 冻结哈希失配 BLOCK）
        post = step_post_write(book, pending, prose, skip_enforce=True)
        _log_auto_commit_audit(book, pending, trigger="dangling_chapter",
                               reason="state_revision_changed", retry=True,
                               old_revision=_exp, new_revision=_rev,
                               result="success" if post["ok"] else "failed")
        if not post["ok"]:
            detail = (post["stdout"] + ("\n" + post["stderr"] if post["stderr"] else ""))[:600]
            return False, f"自动提交执行失败: {detail}"
        #    e) 成功 → 清台账并放行
        _clear_pending_commit_ledger(book, pending)
        return True, f"第{pending}章已自动提交（事务版本冲突 recovery，expected={_exp}→{_rev}）"
    except Exception as exc:  # noqa: BLE001 — 恢复失败仅回退 BLOCK，绝不悬挂主链
        return False, f"自动提交异常: {exc}"


def _dangling_block_detail(pending: int, last: int, *, auto_detail: str,
                           next_action: str = "写后续章节") -> str:
    """C-BLOCKMSG@1：悬空闸 BLOCK 文案（固定四段、全部中文、单行拼接）。

    1) 事实：第 pending 章已写正文但未提交，当前已提交到第 last 章；
    2) 自动提交失败原因：原样携带 _try_auto_commit_dangling 的 detail（含 commit
       stdout/stderr 摘要或验收补跑 detail）；空/空白时写「未尝试自动提交」；
    3) 双恢复路径：必须同时给出「人工修改后验收提交」与「重写」；
    4) 下一步动作：请先处理该章，再 {next_action}。
    """
    _auto = str(auto_detail or "").strip() or "未尝试自动提交"
    return (
        f"存在未提交章节：第{pending}章已写正文但尚未提交（当前已提交到第{last}章）。"
        f"自动提交未成功（{_auto}）。"
        "恢复路径：① 改正文后点「人工修改后验收提交」（系统按原闸补跑验收并提交）；"
        "② 或对该章执行「重写」，基于当前权威重新生成并提交。"
        f"请先处理第{pending}章，再{next_action}。"
    )


def run_write_pipeline(book: Path, chapter_id: int, *, prealign: bool = False, progress=None, on_writer_token=None,
                       review_id: str | None = None, dry_run: bool = False) -> dict:
    """执行写章全流程闭环，返回环节报告。任何一步抛异常 → 环节 FAIL 且不再继续。

    progress: 可选回调 (name, ok, detail) —— 每环节推进时调用（异步任务进度上报用）。
    on_writer_token: 可选回调 (chunk) —— 主笔流式输出逐 token 回调（异步任务实时预览用）。
    review_id: 一次 review run 的身份（异步=task_id；同步缺省由入口生成一次）。此后各 phase 透传，不重新生成。
    """
    book = book.resolve()
    review_id = review_id or f"sync_{uuid.uuid4().hex[:12]}"
    stages: list[StageResult] = []
    _fatal_stage: dict = {"name": None}  # P0-1（2026-10-30 外脑定稿）：记录最近一次失败环节，供 _fail 真实归因
    # C-ADVISORY@1（2026-11-13 R2/R3）：写链失败分支三档口径。
    #   Hard Block   = _finish(False) + return _settle_fail（动作终止，语义不变）；
    #   Degraded Pass = _finish(False) + _note_degraded(code, detail)（继续，章节级可机读）；
    #   Pure Telemetry = _finish(..., telemetry=...) 只进 stage extra，不改章节级状态。
    _degraded: dict = {"pass": False, "codes": []}

    def _note_degraded(code: str, detail: str) -> None:
        """Degraded Pass：真实失败已由 _finish 记录，这里只补章节级标记与告警码。"""
        _degraded["pass"] = True
        if all(known != code for known, _ in _degraded["codes"]):
            _degraded["codes"].append((code, str(detail)))

    def _note_telemetry(code: str, elapsed_ms: int, detail: str = "") -> dict:
        """Pure Telemetry：只返回该 stage 的 telemetry extra，不改 _degraded。"""
        return {"telemetry": {"code": code, "elapsed_ms": int(elapsed_ms), "detail": str(detail)}}

    def _degraded_payload() -> dict:
        """章节级降级结论（两键恒在）：degraded_pass + 机读 warnings。"""
        return {
            "degraded_pass": bool(_degraded["pass"]),
            "warnings": [f"{code}: {detail}" for code, detail in _degraded["codes"]],
        }

    def _finish(name: str, ok: bool, detail: str, output: str = "", **extra) -> None:
        stages.append(StageResult(name, ok, detail, **{**_gate_degraded_extra(), **extra}))
        if not ok:
            _fatal_stage["name"] = name
        if progress:
            try:
                progress(name, ok, detail, output=output)  # type: ignore[arg-type]
            except runtime.AgentCancelled:
                raise
            except Exception:  # noqa: BLE001 — 进度上报失败不影响主链
                pass

    def _begin(name: str, detail: str = "开始执行…") -> None:
        """环节开始时通知前端：progress(name, None, detail)。
        ok=None 表示进行中，_on_phase 据此更新 phase 但不追加 live_stage。"""
        if progress:
            try:
                progress(name, None, detail)  # type: ignore[arg-type]
            except runtime.AgentCancelled:
                raise
            except Exception:  # noqa: BLE001
                pass

    try:
        # 0pre. 写前连续性闸（P1 外脑裁定 2026-09-16）：存在悬空章（正文已落盘但未 commit）时，
        #      后续新写的 delta 必被判 revision、commit 必 BLOCK（2026-09-16 第33/34章事故根因），
        #      白烧整轮 LLM 调用。入口即拦 + 中文恢复指引。stage 复用 "packet"（与 0a 同约定，
        #      不新增前端进度契约）。
        #     P1 扩展（2026-09-17，auto_commit）：闸前先尝试自动提交悬空章（系统完成 commit，
        #      不留尾巴卡住下一步）；自动恢复成功即放行，失败才回退原 BLOCK。
        _last = _last_committed_chapter(book)
        if _last is not None:
            while True:
                _pending = _pending_uncommitted_chapter(book, chapter_id, _last)
                if _pending is None:
                    break  # 无悬空章，放行
                # dry_run（外脑 2026-10-07 裁定）：预览模式禁止自动提交悬空章。
                # 该恢复路径（_realign_delta_revision 写正式 delta / step_post_write 正式 commit /
                # _clear_pending_commit_ledger 删正式台账 / _log_auto_commit_audit 写正式审计）
                # 全程作用于正式 book，且执行时机早于沙箱建立（sb 在 L3113）——隔离穿透点。
                # 保持悬空闸原有 BLOCK 语义（不自提交、不绕过状态防线），仅去掉写正式目录的路径。
                if dry_run:
                    _auto_ok, _auto_detail = False, "dry_run 禁止自动提交悬空章"
                else:
                    _auto_ok, _auto_detail = _try_auto_commit_dangling(book, _pending, _last, progress=progress)
                if not _auto_ok:
                    _dangling = _dangling_block_detail(_pending, _last, auto_detail=_auto_detail)
                    if _gate().is_v13():  # type: ignore[attr-defined]
                        # V13：悬空章是「状态事实」不是产物有效性。自动恢复照常尝试过，
                        # 失败则原样留痕后继续主链（裁决权交作者），不再入口即拦。
                        _gate_record(book, chapter_id, "0pre/dangling_chapter",
                                     _dangling, "pipeline/write")
                        _finish("packet", True,
                                f"[V13 道闸] 悬空事实已记录，动作继续：{_dangling[:200]}")
                        break  # 跳出 while，避免同一悬空章被反复尝试自动提交
                    _finish("packet", False, _dangling)
                    return _settle_fail(book, chapter_id, stages, {"ok": False, "chapter_id": chapter_id, "stages": [s.to_dict() for s in stages], **_degraded_payload()}, dry_run=dry_run)
                # 自动提交成功 → 刷新已提交基准并继续，可能仍有更小的悬空章待恢复
                _last = _last_committed_chapter(book)
                if _last is None:
                    break

        # 0a. 新写自动补细纲（外脑裁定）：独立单章细纲缺失时，从卷级主细纲切出该章并落盘。
        #     不新增 stage（避免破坏既有进度契约）；补细纲结果由后续 packet 的 detail 呈现。
        #     重写路径 run_rewrite_pipeline 不调用本函数（已有正文/细纲），此处天然只影响新写。
        _outline, _outline_err = _ensure_outline_for_write(book, chapter_id)
        if _outline is None:
            # 0b. 写前自动规划（2026-10-24 用户裁决）：切分源也缺 → nww-architect 依据
            #     卷纲弧线 + 追踪事实锚直接生成该章细纲落盘；NWW_PLANNER=0 可关（回原 BLOCK）。
            #     不改 0a 合同：生成产物仍走既有 prealign/packet 验收，闸一个不少。
            if _planner_enabled():
                try:
                    plan = step_plan_outline(book, chapter_id)
                except Exception as exc:  # noqa: BLE001 — 规划失败按去堵播报呈现
                    plan = {"ok": False, "detail": f"自动规划异常: {exc}"}
                if plan.get("ok") and plan.get("path"):
                    _outline, _outline_err = Path(str(plan["path"])), None
                    _finish("plan_outline", True, str(plan.get("detail", "")))
                else:
                    _finish("plan_outline", False, str(plan.get("detail", ""))[:600])
                    _finish("packet", False, f"打包失败: {_outline_err}；{plan.get('detail', '')}")
                    return _settle_fail(book, chapter_id, stages, {"ok": False, "chapter_id": chapter_id, "stages": [s.to_dict() for s in stages], **_degraded_payload()}, dry_run=dry_run)
            else:
                _finish("packet", False, f"打包失败: {_outline_err}（本设置关闭了链内自动规划，"
                                         "请先在规划层补细纲，或开启 NWW_PLANNER=1）")
                return _settle_fail(book, chapter_id, stages, {"ok": False, "chapter_id": chapter_id, "stages": [s.to_dict() for s in stages], **_degraded_payload()}, dry_run=dry_run)

        # 0. prealign（可选）：检查 → 自动回补细纲 → 重跑（2026-10-16 接入三九）
        if prealign:
            _begin("prealign", "写前对齐：架构师审查细纲结构层缺口…")
            _run_prealign_with_repair(book, chapter_id, _finish)

        # 1. 打包
        try:
            _begin("packet", "打包 Writing Packet：拼装上下文…")
            outline, packet = step_packet(book, chapter_id, dry_run=dry_run)
            _finish("packet", True, f"细纲: {outline.relative_to(book).as_posix()}", packet=packet)
            # P2-9 内容超载透出（advisory，不阻断）：builder 已确定性估算
            _cl = (packet.get("meta") or {}).get("content_load") or {}
            if _cl.get("overload"):
                _cl_detail = (f"内容超载（advisory）：{_cl.get('beats', 0)}幕+{_cl.get('formal_points', 0)}个正式情节点"
                              f"≈{_cl.get('estimated_min_chars', '?')}字 > 目标{_cl.get('wc_target', '?')}×1.1 "
                              "—— 预期 writer 将静默丢铺垫/机制，建议拆章或上调字数")
                _finish("content_load", False, _cl_detail)
                _note_degraded("content_load_over_budget", _cl_detail)
        except runtime.AgentCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            _finish("packet", False, f"打包失败: {exc}")
            return _settle_fail(book, chapter_id, stages, {"ok": False, "chapter_id": chapter_id, "stages": [s.to_dict() for s in stages], **_degraded_payload()}, dry_run=dry_run)

        # 2. 主笔生成（只生成正文，提交原始输出给下一环节）
        try:
            _begin("writer", "主笔写作中…")
            draft = step_writer(book, chapter_id, packet, on_token=on_writer_token)
            _finish("writer", True, f"生成 {len(draft)} 字" + _wc_stage_note(), output=draft)
        except runtime.AgentCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            _finish("writer", False, f"主笔生成失败: {exc}")
            return _settle_fail(book, chapter_id, stages, {"ok": False, "chapter_id": chapter_id, "stages": [s.to_dict() for s in stages], **_degraded_payload()}, dry_run=dry_run)

        # 3. 正文归一环节（独立正规环节：prose 提取 + 元信息清洗 + 自动跨章去重）→ 落盘
        _ft_tel: dict = {}  # failed_transactions 解析遥测（2B C-TELEMETRY@1），非 dry_run 才可能赋值
        try:
            if dry_run:
                # dry_run 模式：跳过 409 检查，直接用 sandbox path
                sb = _dry_run_sandbox(book, review_id)
                target = sb / "body-preview" / f"第{chapter_id:03d}章.md"
                rel = target.relative_to(book).as_posix()
            else:
                # normal mode：检查是否为 ORPHANED artifact
                # 如果是 ORPHANED_AFTER_FAILED_PARENT，允许 recovery 覆盖
                ft_path = book / "追踪" / "failed_transactions" / f"chapter_{chapter_id:03d}.json"
                is_orphaned = False

                if ft_path.exists():
                    import json
                    _ft_t0 = time.perf_counter()
                    try:
                        ft = json.loads(ft_path.read_text(encoding="utf-8"))
                        if ft.get("status") == "ORPHANED_AFTER_FAILED_PARENT" and not ft.get("canonical"):
                            is_orphaned = True
                    except Exception as _fte:  # noqa: BLE001 — 解析失败按非 ORPHANED 处理，只留遥测
                        _ft_tel = _note_telemetry(
                            "failed_transactions_unparsed",
                            int((time.perf_counter() - _ft_t0) * 1000), str(_fte))
                if is_orphaned:
                    # ORPHANED：直接构造 path，跳过 409 检查
                    # 沿用旧文件路径
                    from app.nwwmvp import _resolve_volume, _chapter_title_from_outline, _BODY_DIR
                    body_dir = book / _BODY_DIR
                    volume, _w = _resolve_volume(book, chapter_id)
                    title = _chapter_title_from_outline(book, chapter_id)
                    base = f"第{chapter_id:03d}章"
                    fname = f"{base}_{title}.md" if title else f"{base}.md"
                    rel = f"{_BODY_DIR}/{volume}/{fname}" if volume else f"{_BODY_DIR}/{fname}"
                    from pathlib import Path as _Path
                    target = _Path(book) / rel
                else:
                    target, rel = _resolve_body_target(book, chapter_id)
            clean_draft, norm_report = step_normalize(book, chapter_id, draft)
            target.write_text(clean_draft + "\n", encoding="utf-8")
            if dry_run:
                _finish("normalize", True, f"dry-run 正文预览: {rel}（{len(clean_draft)} 字；{norm_report}）", path=rel, chars=len(clean_draft), **_ft_tel)
            else:
                # normalize 后自动重命名：从正文提取标题，更新文件名
                import re as _re
                title_match = _re.match(r"^#\s*第\d+[章\s　]+(.+)$", clean_draft.splitlines()[0] if clean_draft else "")
                if title_match:
                    new_title = title_match.group(1).strip()
                    old_fname = target.name
                    # 检查当前文件名是否已带标题
                    if f"_{new_title}.md" not in old_fname:
                        # 从当前路径提取 base
                        base_match = _re.match(r"^(第\d+章)", old_fname)
                        if base_match:
                            base = base_match.group(1)
                            new_fname = f"{base}_{new_title}.md"
                            new_target = target.parent / new_fname
                            if not new_target.exists():
                                target.rename(new_target)
                                target = new_target
                                rel = target.relative_to(book).as_posix()
                                norm_report += f"；重命名: {new_fname}"
                _finish("normalize", True, f"落盘: {rel}（{len(clean_draft)} 字；{norm_report}）", path=rel, chars=len(clean_draft), **_ft_tel)
        except runtime.AgentCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            _finish("normalize", False, f"正文归一/落盘失败: {exc}")
            return _settle_fail(book, chapter_id, stages, {"ok": False, "chapter_id": chapter_id, "stages": [s.to_dict() for s in stages], **_degraded_payload()}, dry_run=dry_run)

        # 3.1 代词性别机械闸（P0-3）：叙述层 他/她 分布 vs 角色卡性别声明（确定性）。
        #  根因实证：女主书 10 次生成通篇「他」穿过全部审稿轮。dry_run 下降级 advisory
        #  （预览不阻断），正常链路 BLOCK。
        try:
            _pg_ok, _pg_msg = _pronoun_gate_check(book, chapter_id, clean_draft)
            if not _pg_ok and dry_run:
                _finish("pronoun_gate", False, f"{_pg_msg}（dry-run advisory）")
                _note_degraded("pronoun_gate_dry_run", _pg_msg)
            else:
                _finish("pronoun_gate", _pg_ok, _pg_msg)
                if not _pg_ok:
                    return _settle_fail(book, chapter_id, stages, {"ok": False, "chapter_id": chapter_id,
                            "stages": [s.to_dict() for s in stages], **_degraded_payload()}, dry_run=dry_run)
        except runtime.AgentCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 — 闸自身异常：记真实失败 + 降级，不伪造通过
            _finish("pronoun_gate", False, f"代词闸执行异常: {exc}")
            _note_degraded("pronoun_gate_error", str(exc))

        # 3.5 字数收敛（D1，2026-11-05 闭环化）：带外 → 扩/缩写（纠偏预算 ≤2 轮）→
        # 每轮复检；进带或预算尽 → 如实标记交下游既有质量环节（质检无条件执行，
        # 不因字数跳过）。闭环B：settle 链 under 也走同一修文入口 step_expand_once
        # （与 revision 同接口）；触发面维持现语义（under 且低于硬线 0.95），扩面调整属 M2。
        try:
            _wc_body = target if dry_run else None
            wc = step_wordcount_check(book, chapter_id, body_path=_wc_body)
            if not wc["ok"]:
                _finish("wordcount", False, f"字数检查失败（advisory，继续）: {wc.get('detail', '')}")
                _note_degraded("wordcount_check_failed", str(wc.get("detail", "")))
            else:
                # 篇幅平衡 v1（T21）：NWW_WC_PLAN_MODE=1 且纯函数模块可用 → 方案化扩缩；
                # 其余（含 NWW_WC_PLAN_MODE=0）完全走下面旧 2 轮盲扩/盲缩（逐字不变）。
                _lb = None
                if _wc_plan_mode() and _balance_mod() is not None:
                    _lb = step_length_balance(book, chapter_id, packet, target, wc,
                                              dry_run=dry_run, progress=None)
                if _lb is not None and not _lb.get("degraded"):
                    _wc_final = _lb.get("wordcount") or wc
                    _st = _wc_final.get("status", "?")
                    _in_band = _st in ("internal_pass", "borderline", "ok")
                    _finish("wordcount", _in_band, _lb.get("detail") or "",
                            wordcount=_wc_final,
                            balance_plan={k: _lb.get(k) for k in (
                                "rounds", "selected_strategy", "quality_score",
                                "net_delta", "residual_gap", "stop_reason", "audit_path")})
                    if not _in_band:
                        _note_degraded("wordcount_out_of_band",
                                       f"篇幅平衡后仍带外 {_st}：{_lb.get('detail') or ''}")
                else:
                    _wc_final = wc
                    _acted = 0
                    _expand_n = 0
                    _compress_n = 0
                    _wc_start_actual = _wc_final.get("actual") or 0
                    for _attempt in range(2):
                        _acts = _wc_final.get("actions") or []
                        if "commit" in _acts:
                            break
                        if "compress-once" in _acts:
                            # 闭环A：仍 over → 再压，预算内迭代（旧版单次后即放行）
                            step_compress_once(book, chapter_id, packet, target, _wc_final)
                            _acted += 1
                            _compress_n += 1
                        elif _wc_final.get("status") == "under":
                            _s_tmin, _ = _wc_target_band(_wc_final.get("target"))
                            # M2：默认带外即扩（status=under 已按 ±10% 带判定）；
                            # env NWW_WC_EXPAND_BAND=0 回滚旧硬线触发面（<0.95×下限）
                            if _wc_expand_on_band() or (
                                    _s_tmin and (_wc_final.get("actual") or 0) < _s_tmin * _wc_hardline_ratio()):
                                _s_need = max(200, int(_s_tmin - (_wc_final.get("actual") or 0)))
                                step_expand_once(book, chapter_id, packet, target, _wc_final, _s_need)
                                _acted += 1
                                _expand_n += 1
                            else:
                                break  # 旧触发面下欠账未达硬线：不扩
                        else:
                            # accept-current-length / revise-outline-or-target / discard：
                            # 无自动纠偏空间，交用户/下游闸门
                            break
                        _wc_final = step_wordcount_check(book, chapter_id, body_path=_wc_body)
                        if not _wc_final.get("ok"):
                            break
                    _st = _wc_final.get("status", "?")
                    _in_band = _st in ("internal_pass", "borderline", "ok")
                    # 2026-11-06：把「扩写/压缩各跑了几轮、实际净增多少字」透出到 stage detail，
                    # 避免「纠偏 2 轮」看起来像什么都没做（扩写被后续快照恢复回滚时尤其误导）。
                    _wc_delta = (_wc_final.get("actual") or 0) - _wc_start_actual
                    _finish("wordcount", _in_band,
                            f"字数 {_wc_final.get('actual')}（{_st}），纠偏 {_acted} 轮"
                            f"（扩写 {_expand_n} / 压缩 {_compress_n}，净增 {_wc_delta:+d} 字）"
                            + ("" if _in_band else "（仍带外，如实标记；下游质检与 commit 闸照常执行）")
                            + (f"；篇幅平衡降级：{_lb.get('detail')}" if _lb is not None else ""),
                            wordcount=_wc_final)
                    if not _in_band:
                        _note_degraded("wordcount_out_of_band",
                                       f"纠偏 {_acted} 轮后仍带外 {_st}（{_wc_final.get('actual')} 字）")
        except runtime.AgentCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 — 字数收敛失败不阻断主链
            _finish("wordcount", False, f"字数收敛异常（继续）: {exc}")
            _note_degraded("wordcount_converge_error", str(exc))

        # 4-5. delta 草稿 + 补语义
        try:
            _dd_body = target if dry_run else None
            _dd_sb = (sb / "delta-preview") if dry_run else None
            draft_path = step_delta_draft(book, chapter_id, body_path=_dd_body, sandbox_dir=_dd_sb)
            _finish("delta_draft", True, f"草稿: {draft_path.relative_to(book).as_posix()}")
        except runtime.AgentCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            _finish("delta_draft", False, f"delta 草稿生成失败: {exc}")
            return _settle_fail(book, chapter_id, stages, {"ok": False, "chapter_id": chapter_id, "stages": [s.to_dict() for s in stages], **_degraded_payload()}, dry_run=dry_run)
        try:
            step_delta_semantic(book, chapter_id, draft_path, target)
            _finish("delta_semantic", True, "语义字段已补全")
        except runtime.AgentCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            _finish("delta_semantic", False, f"delta 补语义失败: {exc}")
            return _settle_fail(book, chapter_id, stages, {"ok": False, "chapter_id": chapter_id, "stages": [s.to_dict() for s in stages], **_degraded_payload()}, dry_run=dry_run)

        # 5.5 一致性审查（D2 detection）：commit 前产出审查记录；closure 由既有流程负责
        try:
            cons = step_consistency_check(book, chapter_id, target,
                                          artifact_dir=(sb / "一致性审查") if dry_run else None)
            if cons["ok"]:
                _detail = (f"审查记录: {cons['record_path']}（S1/S2={cons['open_s1_s2_count']}，S3/S4={cons['s3_s4_count']}）")
                # 复检清零 + 非 dry_run → 系统自动落闭环记录（2026-11-04）。
                # dry_run 审查记录落 sandbox，绝不落正式 一致性核验/；未清零由函数内拒绝。
                _closure = None
                if not dry_run:
                    _closure = step_consistency_closure(book, chapter_id, cons)
                    if _closure.get("closed"):
                        _detail += f"；自动闭环已落盘（{_closure.get('closure_path', '')}）"
                _finish("consistency", True, _detail, consistency=cons, closure=_closure)
            else:
                _cons_detail = f"一致性检查失败（advisory，继续）: {cons.get('detail', '')}"
                _finish("consistency", False, _cons_detail)
                _note_degraded("consistency_unclosed", _cons_detail)
        except runtime.AgentCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 — 审查失败不阻断（兼容模式 WARNING）
            _finish("consistency", False, f"一致性审查异常（继续）: {exc}")
            _note_degraded("consistency_unclosed", f"一致性审查异常: {exc}")

        # 5.6 deslop 机械层（advisory）—— R-WP1 前移（2026-09-15 外脑裁定选甲）：
        #    deslop 会改写正文（normalize-punctuation.js 以不带 --check 形态写回），须在
        #    review_cycle 与 commit 之前，确保审稿与 commit 消费的都是 normalize 后的同一版本。
        #    顺序：consistency → deslop → review_cycle → post_write(commit)。
        reports = step_deslop(book, chapter_id, target)
        _finish("deslop", True, f"脚本数: {len(reports)}", reports=reports)

        # 5.65 enforce 前移（P0-3a，2026-10-19 外脑裁决）：enforce 会改写正文，必须发生在
        #    finalize 冻结之前，确保「审的=交的」；post_write 内的 enforce 由 --skip-enforce
        #    关闭避免双跑。
        #    C-ENFORCE@1（2026-11-13，R3）：机械层未通过即链失败 —— 不能记为通过，也不能让
        #    后续 post_write(skip_enforce=True) 把机械层永久跳过。回滚口 NWW_ENFORCE_ADVISORY=1
        #    恢复旧 advisory（WARNING 继续，逐字旧行为）。
        enf: dict = {"ok": False, "returncode": None, "stderr": ""}
        try:
            enf = step_pre_review_enforce(book, chapter_id, target, draft_path)
        except runtime.AgentCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 — 异常同样按机械层未通过处理
            enf = {"ok": False, "returncode": -1, "stderr": f"enforce 前移执行异常: {exc}"}
        _enf_ok = bool(enf.get("ok"))
        _enf_err = str(enf.get("stderr") or enf.get("stdout") or "")[:400]
        if _enf_ok:
            _finish("pre_review_enforce", True, "已执行（finalize 前）",
                    returncode=enf.get("returncode"))
        elif _enforce_advisory_enabled():
            _finish("pre_review_enforce", False,
                    f"失败（NWW_ENFORCE_ADVISORY=1，WARNING 继续）: {_enf_err[:120]}",
                    returncode=enf.get("returncode"))
            _note_degraded("enforce_advisory_bypass", _enf_err[:200])
        else:
            _finish("pre_review_enforce", False,
                    f"enforce 机械层未通过，不进入提交: {_enf_err[:200]}",
                    returncode=enf.get("returncode"))
            _write_pending_commit_ledger(
                book, chapter_id, f"pre_review_enforce 失败:\n{_enf_err}")
            return _settle_fail(book, chapter_id, stages, {"ok": False, "chapter_id": chapter_id, "stages": [s.to_dict() for s in stages], **_degraded_payload()}, dry_run=dry_run)

        # 5.7 审稿闭环（D3，时序关键：commit 前把 reviewed 准备好供 A-5 闸消费）
        # P0-3b（2026-10-19 外脑裁决）：finalize 失败/审稿未过线必须进入失败分支——
        # V5 实证「低分章 advisory 继续后进入权威状态」。原 advisory 语义废除。
        # 例外：best ≥ 通过线但 floor 维度触底耗尽轮次（passed=False）→ 维持原 advisory
        # 继续（best-snapshot 既有语义「保留最高分版本」，超出本轮裁决范围不动）。
        try:
            _begin("review_cycle", "审稿闭环：LLM 多轮评分 + 自动修订 + finalize 冻结")
            rv = step_review_cycle(book, chapter_id, target, draft_path, review_id, dry_run=dry_run)
            _rv_best = float(rv.get("best_score") or 0)
            if not rv["ok"]:
                _finish("review_cycle", False, f"审稿闭环失败: {rv.get('detail', '')}")
                _write_pending_commit_ledger(
                    book, chapter_id, f"review_cycle 失败:\n{rv.get('detail', '')}")
                return _settle_fail(book, chapter_id, stages, {"ok": False, "chapter_id": chapter_id, "stages": [s.to_dict() for s in stages], **_degraded_payload()}, dry_run=dry_run)
            if not rv["passed"] and _rv_best < _REVIEW_PASS_SCORE:
                _finish("review_cycle", False,
                        f"审稿未通过（最高分 {_rv_best} < {_REVIEW_PASS_SCORE}，{rv.get('rounds', 0)} 轮）——低分章不得提交")
                _write_pending_commit_ledger(
                    book, chapter_id,
                    f"review_cycle 未过线: best={_rv_best} < {_REVIEW_PASS_SCORE}")
                return _settle_fail(book, chapter_id, stages, {"ok": False, "chapter_id": chapter_id, "stages": [s.to_dict() for s in stages], **_degraded_payload()}, dry_run=dry_run)
            # 维度地板=硬约束（外脑 2026-10-19 边界项3）：passed=False 且分数达标时，
            # 仅「净提升停手且无硬约束失败」按外脑双条件放行（best-snapshot 保留最高分版本）；
            # 维度触底/细纲未兑现（floor_violations/promise_violations 非空）= 证据硬约束
            # 失败 → 阻断，不被 best >= 85 覆盖（2026-10-25 闸2·细纲层接入写章链）。
            if not rv["passed"] and ((rv.get("floor_violations") or [])
                                     or (rv.get("promise_violations") or [])):
                _rv_floor = rv["floor_violations"]
                _rv_promise = rv.get("promise_violations") or []
                _fail_detail = ""
                if _rv_floor:
                    _fail_detail = f"维度地板未达标: {', '.join(_rv_floor)}"
                elif _rv_promise:
                    _fail_detail = f"细纲承诺未兑现: {'; '.join(_rv_promise[:5])}"
                _finish("review_cycle", False,
                        f"审稿未通过（最高分 {_rv_best} 达标但{_fail_detail}）——不得作为已验收版本提交")
                _write_pending_commit_ledger(
                    book, chapter_id,
                    f"review_cycle 维度地板硬约束未达标: best={_rv_best}, floor={_rv_floor}")
                return _settle_fail(book, chapter_id, stages, {"ok": False, "chapter_id": chapter_id, "stages": [s.to_dict() for s in stages], **_degraded_payload()}, dry_run=dry_run)
            # 审稿通过时透出采样细节（2026-10-24 自审：采样原始分需可复核）
            # meta 从 step_review_cycle 返回值取（原先读未定义名 _rv_meta，本函数无赋值，
            # 通过路径必抛 NameError → 被下方 except 吞成「审稿闭环异常」整链失败）
            _rv_detail = f"（最高分 {_rv_best}，{rv.get('rounds', 0)} 轮）"
            # P0（2026-11-05）：把 best→final 恢复的字数变化透出到 stage detail，
            # 断环不再「只写在脚本 stdout、前端与回执都看不见」。
            _wc_r = rv.get("wc_restore") or {}
            if _wc_r:
                _rv_detail += (f" [字数恢复: 当前{_wc_r.get('cur_actual')}({_wc_r.get('cur_status')}) → "
                               f"快照{_wc_r.get('snap_actual')}({_wc_r.get('snap_status')})，"
                               f"{'守卫保留当前正文' if _wc_r.get('blocked') else '已恢复快照'}]")
            _rv_meta = rv.get("score_meta") or {}
            if _rv_meta.get("samples", 1) > 1:
                _rv_detail += f" [采样{_rv_meta['samples']}次中位{_rv_best}（原始{_rv_meta['raw']}）]"
            _finish("review_cycle", True, _rv_detail, review=rv)
        except runtime.AgentCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 — P0-3b：审稿异常=无验收结果，进失败分支不再继续提交
            _finish("review_cycle", False, f"审稿闭环异常: {exc}")
            _write_pending_commit_ledger(book, chapter_id, f"review_cycle 异常:\n{exc}")
            return _settle_fail(book, chapter_id, stages, {"ok": False, "chapter_id": chapter_id, "stages": [s.to_dict() for s in stages], **_degraded_payload()}, dry_run=dry_run)

        # 7.5 生产质量遥测（仅观察，advisory；NWW_QUALITY_OBSERVE=1 开启，灰度；reading target 无需阻塞）
        if os.environ.get(_OBSERVE_SWITCH, "0") == "1":
            _qo_t0 = time.perf_counter()
            try:
                observe = review_observe(book, chapter_id, target,
                                         artifact_dir=(sb / "质量观察") if dry_run else None)
                _finish("quality_observe", True, "已写质量观察", observe=observe)
            except Exception as _oe:  # noqa: BLE001 — 遥测失败不阻断提交
                _finish("quality_observe", False, f"质量观察失败（advisory，继续）: {_oe}",
                        **_note_telemetry("quality_observe_error",
                                          int((time.perf_counter() - _qo_t0) * 1000), str(_oe)))

        # 6. post_write（BLOCK 点，commit 消费 delta 里已就绪的 reviewed/enforced）
        if dry_run:
            # P3（外脑 2026-10-07 裁定）：delta 已在 step_delta_draft(sandbox_dir=...) 直接写 sandbox，
            # 此处不得再从正式 追踪/ move —— 该残留会搬走/误动正式 delta（dry_run 正式目录须零变化）。
            _finish("dry_run", True, "write dry-run：正文/delta 已入 sandbox，正式目录零变化、未 commit")
            return {"ok": True, "chapter_id": chapter_id, "mode": "write", "dry_run": True,
                    "execution_id": review_id or "", "commit_status": "SKIPPED_DRY_RUN",
                    "sandbox": sb.relative_to(book).as_posix(),
                    "stages": [s_.to_dict() for s_ in stages], **_degraded_payload()}
        # 落盘完整性观测：finalize 之后（post_write 之前）先取一次指纹
        _sha_after_review = (_sha256_of(target)
                             if _finalize_integrity_enabled() else "")
        # skip_enforce=True（P0-3a）：enforce 已在 5.65 前移执行，post_write 内不再重复
        post = step_post_write(book, chapter_id, target, skip_enforce=True)
        if _finalize_integrity_enabled():
            _fi_t0 = time.perf_counter()
            try:
                _fi = _record_finalize_integrity(
                    book, chapter_id, review_id or "", _sha_after_review, _sha256_of(target))
                _finish("finalize_integrity", _fi["intact"],
                        _fi["note"], finalize_integrity=_fi)
                if not _fi["intact"]:
                    _note_degraded("finalize_integrity_mismatch", _fi["note"])
            except Exception as _fie:  # noqa: BLE001
                _finish("finalize_integrity", False, f"完整性观测失败（advisory）: {_fie}",
                        **_note_telemetry("finalize_integrity_error",
                                          int((time.perf_counter() - _fi_t0) * 1000), str(_fie)))
        # 第一层 commit 恢复（外脑裁决 2026-09-17）：post_write 失败若为事务版本冲突
        # （expected_state_revision ≠ state，即 commit "state changed"），对齐 revision 后
        # retry once；非版本冲突（paragraph_gate/continuity/enforce 等质量或状态闸）绝不
        # 自动重试，保持原 BLOCK。write 链唯一入口，rewrite/revise/deslop 不接入。
        if not post["ok"]:
            _conflict, _exp, _rev = _revision_conflict(book, chapter_id)
            if _conflict and _realign_delta_revision(book, chapter_id, _rev):
                post = step_post_write(book, chapter_id, target, skip_enforce=True)  # retry once
                _log_auto_commit_audit(book, chapter_id, trigger="commit_retry",
                                       reason="state_revision_changed", retry=True,
                                       old_revision=_exp, new_revision=_rev,
                                       result="success" if post["ok"] else "failed")
        # 同时消费 stdout + stderr：paragraph_gate BLOCK 详情走 stdout，commit/enforce 走 stderr
        _finish("post_write", post["ok"],
                (post["stdout"] + ("\n" + post["stderr"] if post["stderr"] else ""))[:2400],
                returncode=post["returncode"])
        if not post["ok"]:
            # P2（外脑裁定）：commit BLOCK → 落恢复台账，让"断在第几章、为什么断"可查，
            # 不再人工猜（2026-09-16 第32章悬空事故的直接教训）。
            _write_pending_commit_ledger(
                book, chapter_id,
                f"post_write BLOCK:\n{post['stdout']}\n{post['stderr']}")
            return _settle_fail(book, chapter_id, stages, {"ok": False, "chapter_id": chapter_id, "stages": [s.to_dict() for s in stages], **_degraded_payload()}, dry_run=dry_run)

        # P2：commit 成功 → 该章恢复台账失效即删
        _clear_pending_commit_ledger(book, chapter_id)

        # 7.8 跨章因果审计（外脑 ChatGPT 裁决 P0-3；advisory，绝不阻断）
        #    commit 之后跑（此时逐章记录/伏笔已更新，审计的是已提交事实）。
        #    只产出审计结果，不改正文、不改 commit 语义、不设硬失败。
        if _causal_audit_enabled():
            _ca_t0 = time.perf_counter()
            try:
                ca = step_causal_audit(book, chapter_id,
                                       artifact_dir=(sb / "因果审计") if dry_run else None)
                st = (ca.get("stats") or {})
                _finish(
                    "causal_audit", bool(ca.get("ok")),
                    (f"承诺承接 {st.get('prev_commitments_responded', 0)}/"
                     f"{st.get('prev_commitments', 0)}；未回收伏笔 {st.get('open_foreshadows', 0)}；"
                     f"告警 {st.get('warning_count', 0)}"
                     if ca.get("ok") else f"因果审计失败（advisory，继续）: {ca.get('detail', '')}"),
                    causal_audit=ca,
                )
                if not ca.get("ok"):
                    _note_degraded("causal_audit_failed", str(ca.get("detail", "")))
            except Exception as _cae:  # noqa: BLE001 — 审计失败绝不阻断
                _finish("causal_audit", False, f"因果审计异常（advisory，继续）: {_cae}",
                        **_note_telemetry("causal_audit_error",
                                          int((time.perf_counter() - _ca_t0) * 1000), str(_cae)))

        # 7.9 章尾动力类型轮换审计（外脑 P1-4；必执行但 advisory，绝不硬阻断）
        #    回执写进 stage detail，保证「看得到」——不重蹈状态卡/全局状态空转的覆辙。
        if _tail_gate_enabled():
            _tg_t0 = time.perf_counter()
            try:
                tg = step_tail_gate(book, chapter_id,
                                    artifact_dir=(sb / "章尾审计") if dry_run else None)
                if tg.get("ok") is False and not tg.get("rows"):
                    _finish("tail_gate", False,
                            f"章尾审计失败（advisory，继续）: {tg.get('detail', '')}",
                            **_note_telemetry("tail_gate_failed",
                                              int((time.perf_counter() - _tg_t0) * 1000),
                                              str(tg.get("detail", ""))))
                else:
                    _finish(
                        "tail_gate", True,
                        (f"last_3={''.join(tg.get('last_3') or []) or '—'}　"
                         f"{'⚠ 连续同类型' if tg.get('last_3_violation') else 'OK'}；"
                         f"分布 {tg.get('type_distribution')}；"
                         f"未分类 {tg.get('unclassified_rate', 0):.0%}"
                         f"（未分类率高时结论打折）"),
                        tail_gate=tg,
                    )
            except Exception as _tge:  # noqa: BLE001 — 审计失败绝不阻断
                _finish("tail_gate", False, f"章尾审计异常（advisory，继续）: {_tge}",
                        **_note_telemetry("tail_gate_error",
                                          int((time.perf_counter() - _tg_t0) * 1000), str(_tge)))

        return {"ok": True, "chapter_id": chapter_id, "stages": [s.to_dict() for s in stages],
                **_degraded_payload()}
    except runtime.AgentCancelled:
        raise
    except Exception as exc:  # noqa: BLE001 — 编排器兜底，绝不悬挂
        _finish("pipeline", False, f"未预期错误: {exc}")
        return _settle_fail(book, chapter_id, stages, {"ok": False, "chapter_id": chapter_id, "stages": [s.to_dict() for s in stages], **_degraded_payload()}, dry_run=dry_run)


# ---------------------------------------------------------------------------
# 重写已有章节（revision 模式）：备份旧稿 → packet → writer → 覆盖落盘
# → delta 草稿（对已提交章自动 mode=revision，见 nww_delta_draft.py）→ 补语义
# → post_write（revision 无「内容必须一致」闸，A-1 计量对 revision 跳过）
# 与 run_write_pipeline 的唯一差异：目标章必须已提交（有正文），不走 409 防覆盖。
# ---------------------------------------------------------------------------
_NON_PROSE_KW = ("_设定", "_笔记", "_大纲", "_细纲", "_草稿", "_废弃", "_储备")
# 辅助目录不是正文：_review_snapshots/ 存 review round 快照。快照文件名
# （第027章_round1.md）不含 _NON_PROSE_KW 里任何关键词，只能按**路径部件**排除。
# 不排除的后果：多候选按 mtime 取最新 → 快照比正文新时被当成本章正文
# （2026-09-13 实测于《用泡面征服修仙界》）。与 nww_track_commit._is_non_prose 同口径。
_NON_PROSE_DIRS = ("_review_snapshots", "_archive")


def _is_non_prose(p: Path) -> bool:
    """路径是否属于「非正文」：命中辅助目录，或文件名带备份/草稿关键词。"""
    return (
        any(d in p.parts for d in _NON_PROSE_DIRS)
        or "_原稿_" in p.name
        or any(kw in p.name for kw in _NON_PROSE_KW)
    )


# ---------------------------------------------------------------------------
# C-FUSE@1（2026-11-06）：重写轮次熔断 / 细纲前提变更作废。
# 脚本 nww_rewrite_fuse.py 本身是确定性的（零 LLM/零网络），此前全仓零调用点（RC-4），
# 导致「BLOCK → 点重写 → 回补 → 再 BLOCK」可以无限循环。这里只做接线：
#   · 重写前 check（position：backup 之后、prealign 之前，避免回补一轮后才熔断）；
#   · commit 成功后 sync_hash 登记下一轮前提基线；
#   · NWW_REWRITE_FUSE=0 关闭；脚本缺失/执行失败 → 跳过熔断（不阻断主链）。
# ---------------------------------------------------------------------------
_REWRITE_FUSE_SCRIPT = "nww_rewrite_fuse.py"


def _rewrite_fuse_enabled() -> bool:
    return os.environ.get("NWW_REWRITE_FUSE", "1") != "0"


def _rewrite_fuse_check(book: Path, chapter_id: int) -> dict:
    """调用 nww_rewrite_fuse.check 的确定性包装（C-FUSE@1）。

    返回 {"ok": bool, "action": str, "reason": str, "outline_hash": str,
          "receipts": int, "skipped": bool}。
    - NWW_REWRITE_FUSE=0 → {"ok": True, "skipped": True}；
    - 脚本缺失/执行失败/输出不可解析 → {"ok": True, "skipped": True, "error": ...}（不阻断）；
    - `ok=False` 只在脚本成功返回 stop_and_report 时出现（receipts 超上限 / outline_hash 变更）。
    """
    if not _rewrite_fuse_enabled():
        return {"ok": True, "action": "skip", "reason": "NWW_REWRITE_FUSE=0",
                "outline_hash": "", "receipts": 0, "skipped": True}
    args = ["--book", str(book), "--chapter", str(chapter_id), "--check"]
    _max = str(os.environ.get("NWW_REWRITE_FUSE_MAX", "3") or "3").strip()
    if _max:
        args += ["--max-rewrites", _max]
    try:
        res = toolbox.run_script(_REWRITE_FUSE_SCRIPT, args, book, timeout=60)
    except Exception as exc:  # noqa: BLE001 — 熔断不可用绝不阻断写作链
        print(f"[REWRITE_FUSE] 脚本执行异常，跳过熔断: {exc}", flush=True)
        return {"ok": True, "action": "skip", "reason": f"fuse 不可用: {exc}",
                "outline_hash": "", "receipts": 0, "skipped": True,
                "error": str(exc)[:400]}
    payload: dict | None = None
    try:
        _raw = (res.get("stdout") or "").strip()
        payload = json.loads(_raw) if _raw else None
    except Exception:  # noqa: BLE001 — 输出非 JSON 按脚本不可用处理
        payload = None
    if isinstance(payload, dict) and payload.get("action") in (
            "pass", "stop_and_report", "ask_author", "manual_continue_once"):
        out = {
            "ok": bool(payload.get("ok", True)),
            "action": str(payload.get("action")),
            "reason": str(payload.get("reason") or ""),
            "outline_hash": str(payload.get("outline_hash") or ""),
            "receipts": int(payload.get("receipts") or 0),
            "skipped": False,
        }
        # 请示态必须原样透出选项，否则上游会退化成「未知 action → 跳过熔断 → 自动继续」。
        for _k in ("needs_author_decision", "options", "authorized_once"):
            if _k in payload:
                out[_k] = payload[_k]
        print(f"[REWRITE_FUSE] action={out['action']} receipts={out['receipts']}"
              + (f" reason={out['reason'][:160]}" if out["reason"] else ""), flush=True)
        return out
    _err = (res.get("stderr") or res.get("stdout")
            or "脚本缺失或输出不可解析")
    print(f"[REWRITE_FUSE] 脚本不可用，跳过熔断: {str(_err)[:200]}", flush=True)
    return {"ok": True, "action": "skip", "reason": f"fuse 不可用: {str(_err)[:200]}",
            "outline_hash": "", "receipts": 0, "skipped": True,
            "error": str(_err)[:400]}


def _rewrite_fuse_grant_once(book: Path, chapter_id: int) -> dict:
    """作者显式授权「本次继续重写一次」（一次性令牌，check 时消费后失效）。

    2026-11-13 用户裁决：熔断是「请示作者下一步怎么改」，不是禁止重写、也不是自动重写。
    只写 `追踪/rewrite_fuse.json` 的一次性令牌，不删回执、不改基线、不动正文。
    """
    if not _rewrite_fuse_enabled():
        return {"ok": False, "action": "skip", "error": "NWW_REWRITE_FUSE=0"}
    try:
        res = toolbox.run_script(
            _REWRITE_FUSE_SCRIPT,
            ["--book", str(book), "--chapter", str(chapter_id), "--grant-once"],
            book, timeout=30,
        )
        _raw = (res.get("stdout") or "").strip()
        payload = json.loads(_raw) if _raw else {}
        if isinstance(payload, dict) and payload.get("ok"):
            print(f"[REWRITE_FUSE] grant_once OK chapter={chapter_id}", flush=True)
            return payload
        _err = (res.get("stderr") or res.get("stdout") or "grant_once 输出不可解析")
        print(f"[REWRITE_FUSE] grant_once 失败: {str(_err)[:200]}", flush=True)
        return {"ok": False, "action": "error", "error": str(_err)[:400]}
    except Exception as exc:  # noqa: BLE001 — 授权失败不阻断，由调用方提示
        print(f"[REWRITE_FUSE] grant_once 异常: {exc}", flush=True)
        return {"ok": False, "action": "error", "error": str(exc)[:400]}


def _rewrite_fuse_sync(book: Path, chapter_id: int) -> None:
    """commit 成功后登记当前 outline_hash（失败静默，只打印；绝不改提交结果）。"""
    if not _rewrite_fuse_enabled():
        return
    try:
        res = toolbox.run_script(
            _REWRITE_FUSE_SCRIPT,
            ["--book", str(book), "--chapter", str(chapter_id), "--sync-hash"],
            book, timeout=60)
    except Exception as exc:  # noqa: BLE001
        print(f"[REWRITE_FUSE] sync-hash 异常（不阻断）: {exc}", flush=True)
        return
    if res.get("ok"):
        print(f"[REWRITE_FUSE] sync-hash 已登记: {(res.get('stdout') or '')[:200]}", flush=True)
    else:
        print(f"[REWRITE_FUSE] sync-hash 失败（不阻断）: "
              f"{(res.get('stderr') or res.get('stdout') or '')[:200]}", flush=True)


def _resolve_existing_body(book: Path, chapter_id: int) -> Path | None:
    """定位已存在的本章正文文件（排除 _原稿_ 备份与非正文件；多候选按 mtime 取最新）。

    语义对齐 nww_track_commit.resolve_chapter_file（正文目标解析的唯一权威口径）。
    """
    body = book / "正文"
    if not body.is_dir():
        return None
    cands: list[Path] = []
    for p in body.rglob("第*章*.md"):
        if _is_non_prose(p):
            continue
        m = re.match(r"^第0*(\d+)章", p.name)
        if m and int(m.group(1)) == chapter_id:
            cands.append(p)
    if not cands:
        return None
    cands.sort(key=lambda p: p.stat().st_mtime_ns)
    return cands[-1]


def run_rewrite_pipeline(book: Path, chapter_id: int, progress=None, on_writer_token=None,
                         review_id: str | None = None, note: str = "", level: str = "L3",
                         prealign: bool = False,
                         dry_run: bool = False) -> dict:
    """重写已提交章节（revision 闭环）：旧稿备份 → 全流程重写 → revision 提交。

    prealign（2026-10-16 新增，对齐写章链）：为 True 时在细纲有效性闸门之后、打包之前
    插入「写前对齐 → 自动回补细纲 → 重跑」；第 1–3 章含三九（E0 黄金三章链 G1–G9）。
    此前 rewrite 链**没有** prealign 参数，三九在重写时从不执行。

    约定：只作用于「已有正文的已提交章」；正文不存在 → FAIL（新写走 agents/write）。
    progress: 可选回调 (name, ok, detail) —— 每环节推进时调用（异步任务进度上报用）。
    on_writer_token: 可选回调 (chunk) —— 主笔流式输出逐 token 回调（异步任务实时预览用）。
    review_id: 一次 review run 身份（异步=task_id；同步缺省由入口生成一次）。
    note（2026-09-16 补）：执行要点 ≤500 字（扩写目标字/衔接/保持原文/重点扩充段）。
        非空时主笔进入「原文保持式重写」（读本章正文注入提示词，见 step_writer），
        修复此前 note 被丢弃、扩写退化为整章重生成的语义塌缩。
    level（2026-09-20 用户裁决）：重写等级 L1/L2/L3（命名避免「表达/结构/剧情」歧义）——
        L1 保持剧情重写：原事件不变，重新表达（强制原文保持式，缺省注入 L1 执行要点）；
        L2 章节结构重写：保留章节目标，重排叙事（强制原文保持式，缺省注入 L2 执行要点）；
        L3 章节再生产：根据合同（细纲+confirmed_facts+前情）重新进入写作链全量生成（默认）。
    """
    book = book.resolve()
    level = str(level or "L3").upper()
    if level not in ("L1", "L2", "L3"):
        level = "L3"
    _LEVEL_DEFAULT_NOTE = {
        "L1": "保持剧情重写（L1）：本章所有事件、事件顺序、人物关系、数值与章节目标完全不变；"
              "只重写句子与段落表达，提升文笔、可读性与网文节奏，删除AI腔。",
        "L2": "章节结构重写（L2）：保留章节目标与全部关键事件及其结果，重排叙事结构与段落组织"
              "（如开场冲突→发展→情绪落点）；不得改变事件结果、人物关系与数值。",
    }
    review_id = review_id or f"sync_{uuid.uuid4().hex[:12]}"
    stages: list[StageResult] = []
    _fatal_stage: dict = {"name": None}  # P0-1（2026-10-30 外脑定稿）：记录最近一次失败环节，供 _fail 真实归因

    def _finish(name: str, ok: bool, detail: str, output: str = "", **extra) -> None:
        stages.append(StageResult(name, ok, detail, **{**_gate_degraded_extra(), **extra}))
        if not ok:
            _fatal_stage["name"] = name
        if progress:
            try:
                progress(name, ok, detail, output=output)  # type: ignore[arg-type]
            except runtime.AgentCancelled:
                raise
            except Exception:  # noqa: BLE001 — 进度上报失败不影响主链
                pass

    def _begin(name: str, detail: str = "开始执行…") -> None:
        """环节开始时通知前端：progress(name, None, detail)。
        ok=None 表示进行中，_on_phase 据此更新 phase 但不追加 live_stage。"""
        if progress:
            try:
                progress(name, None, detail)  # type: ignore[arg-type]
            except runtime.AgentCancelled:
                raise
            except Exception:  # noqa: BLE001
                pass

    def _fail(fatal_phase: str | None = None) -> dict:
        out = {"ok": False, "chapter_id": chapter_id, "mode": "revision", "level": level,
               "commit": "none",
               "advisory_failures": advisory,
               "stages": [s.to_dict() for s in stages]}
        # P0-1：显式传参优先；缺省取最近一次 _finish(False) 环节
        # （实测 62/62 个 return _fail() 调用点前置失败登记，记录即真实致死环节，非事后扫描推导）
        _fp = fatal_phase or _fatal_stage.get("name")
        if _fp:
            out["fatal_phase"] = _fp
            out["fatal_phase_confirmed"] = True
        # C-FUSE@1：熔断失败必须交作者显式决策（不再自动起下一轮）。
        # nwwmvp 的 _with_decision 本会 setdefault；这里直接透传，保证 pipeline
        # 单独调用/单测也能看到 needs_decision=True。
        if _fp == "fuse":
            out["needs_decision"] = True
        return _settle_fail(book, chapter_id, stages, out, dry_run=dry_run)

    # F1（2026-09-22 用户裁决）：normalize 覆盖落盘后的取消/异常出口统一回滚。
    # 此前乐观落盘后一旦 AgentCancelled/异常（守卫未拦截路径），新稿（可能为退化稿）
    # 残留在生产正文（00:04 残稿事故同型机制）。落盘标志 + 回滚闭包，失败出口统一回滚。
    body_written = {"done": False}

    # ★ 事务边界（2026-10-03 外脑裁定）：rollback 的定义不是「把正文恢复回去」，而是
    #   「把本次 rewrite 产生的全部持久化副作用恢复到 rewrite 开始之前」。
    #   管理对象：original body / new body / delta / ledger / state_revision /
    #   last_committed_chapter / projection。
    #   commit 是唯一推进 state 的动作，故 rollback 只发生在 commit 之前 → state 天然不变；
    #   这里仍保留只读断言 _state_unchanged()，防止未来新增阶段偷偷改状态却无人发现。
    _tx_delta = book / _TRACKING_DIR / f"{_DELTA_PREFIX}{chapter_id:03d}章.json"
    _tx_ledger = book / _TRACKING_DIR / "pending_commits" / f"第{chapter_id:03d}章.json"
    _tx_snap: dict[str, bytes | None] = {"delta": None, "ledger": None}
    _tx_state_before: dict[str, object] = {"revision": None, "last": None}
    # advisory 失败登记（③⑤⑥ 等）：语义上继续提交，但在最终结果里必须可观测。
    advisory: list[dict] = []
    # 前移拆条结果（3. 主笔生成前拆，6.9 验收复用；此处先声明防异常路径 NameError）
    _note_items: list[dict] | None = None

    def _rollback_body() -> str:
        """回滚 normalize 落盘的新稿到执行前正文；未落盘（标志 False）不动作。
        返回回滚结果描述（空串=无需回滚）。只做一次（回滚后复位标志）。"""
        if not body_written.get("done"):
            return ""
        try:
            target.write_text(original_prose, encoding="utf-8")
        except Exception:  # noqa: BLE001
            return "回滚失败，请人工从 .backup/ 恢复"
        body_written["done"] = False
        return "已回滚执行前正文"

    def _state_unchanged() -> bool:
        """只读断言：state_revision / last_committed_chapter 是否仍等于 rewrite 开始前。"""
        try:
            _s = json.loads((book / _TRACKING_DIR / "_tracking-state.json").read_text(encoding="utf-8"))
            return (_s.get("state_revision") == _tx_state_before.get("revision")
                    and _s.get("last_committed_chapter") == _tx_state_before.get("last"))
        except Exception:  # noqa: BLE001 — 读不到视为「无法证明未变」，保守报 False
            return False

    def _rollback_transaction() -> str:
        """本次 rewrite 的全部持久化副作用回滚：正文 + delta + 台账。

        与 _rollback_body 的区别：_rollback_body 只还原正文，会留下「无主 delta /
        悬空台账」这类半恢复态（实测 delta_draft 阶段就会先落 10KB delta，正文回滚
        后 delta 仍在 → 2026-10-03 外脑裁定必须一并清理）。
        幂等：rewrite 前不存在的文件删除；rewrite 前存在的文件按字节快照复原。
        返回回滚结果描述（空串=无需回滚）。"""
        parts: list[str] = []
        _rb = _rollback_body()
        if _rb:
            parts.append(_rb)
        for _k, _p in (("delta", _tx_delta), ("ledger", _tx_ledger)):
            try:
                _before = _tx_snap.get(_k)
                if _before is None:
                    if _p.is_file():
                        _p.unlink()
                        parts.append(f"已删除本次生成的{_k}")
                elif (not _p.is_file()) or _p.read_bytes() != _before:
                    _p.write_bytes(_before)
                    parts.append(f"已恢复 rewrite 前的{_k}")
            except Exception:  # noqa: BLE001 — 单项失败不阻断其余，但必须在 detail 里可见
                parts.append(f"{_k}回滚失败，请人工处理")
        if not _state_unchanged():
            parts.append("⚠ state_revision/last_committed 与 rewrite 前不一致（未回滚，需人工核对）")
        return "；".join(parts)

    def _rollback_on_cancel() -> None:
        """AgentCancelled 出口专用：回滚后仅入 stages（不经 progress 回调，
        避免取消路径再次触发回调异常）。
        2026-10-03：升级为事务回滚（正文 + delta + 台账），与失败出口同一语义。"""
        rb = _rollback_transaction()
        if rb:
            stages.append(StageResult("rollback_on_cancel", True, rb))

    def _advisory_fail(name: str, detail: str) -> None:
        """advisory 失败登记：不改变控制流（继续提交），只在最终结果里沉淀证据。

        ★ 语义边界：结果里的 ok=True 只表示「事务最终成功提交」，
        不表示「所有质量检查均通过」。advisory_failures 非空即代表存在降级项。"""
        advisory.append({"stage": name, "detail": str(detail or "")[:400]})

    try:
        # 0pre. 写前连续性闸（P1 外脑裁定 2026-09-16）：重写目标章之前若存在更小的悬空章
        #      （正文已落盘但未 commit），本链 delta 仍会错判 revision → commit BLOCK。
        #      目标章自身悬空 = 合法恢复路径（重写悬空章正是恢复手段），不拦。
        _last = _last_committed_chapter(book)
        if _last is not None:
            _pending = _pending_uncommitted_chapter(book, chapter_id, _last)
            if _pending is not None:
                _dangling = _dangling_block_detail(_pending, _last, auto_detail="",
                                                   next_action="重写后续章节")
                if _gate().is_v13():  # type: ignore[attr-defined]
                    _gate_record(book, chapter_id, "0pre/dangling_chapter",
                                 _dangling, "pipeline/rewrite")
                    _finish("packet", True,
                            f"[V13 道闸] 悬空事实已记录，动作继续：{_dangling[:200]}")
                else:
                    _finish("packet", False, _dangling)
                    return _fail()

        target = _resolve_existing_body(book, chapter_id)
        if target is None:
            _finish("backup", False, f"第{chapter_id}章无已提交正文（或正文已被备份改名）。新写请走 agents/write；无法重写不存在的章节。")
            return _fail()

        # ★ 事务快照（2026-10-03 外脑裁定）：记录 rewrite 开始前的 delta / 台账字节与
        #   状态版本，供 _rollback_transaction 精确复原（rewrite 前不存在的删除，存在的按字节复原）。
        for _k, _p in (("delta", _tx_delta), ("ledger", _tx_ledger)):
            try:
                _tx_snap[_k] = _p.read_bytes() if _p.is_file() else None
            except Exception:  # noqa: BLE001 — 快照失败按「rewrite 前不存在」处理（回滚即删除）
                _tx_snap[_k] = None
        try:
            _s = json.loads((book / _TRACKING_DIR / "_tracking-state.json").read_text(encoding="utf-8"))
            _tx_state_before["revision"] = _s.get("state_revision")
            _tx_state_before["last"] = _s.get("last_committed_chapter")
        except Exception:  # noqa: BLE001 — 读不到则断言恒 False，回滚 detail 会显式告警
            pass

        # 1. 写前备份（幂等：.backup/{章名}.orig.md 首份优先，已有不覆盖）；dry_run 不写正式 .backup/
        _begin("backup", "备份原稿…")
        if dry_run:
            _finish("backup", True, "dry-run：跳过正式 .backup 写入")
        else:
            bk = toolbox.run_script("nww_backup.py", ["--book", str(book), "--file", str(target)], book, timeout=60)
            if not bk["ok"]:
                _finish("backup", False, f"原稿备份失败: {bk['stderr'] or bk['stdout']}")
                return _fail()
            _finish("backup", True, bk["stdout"][:300], path=bk["stdout"].strip())
    except runtime.AgentCancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        _finish("backup", False, f"原稿备份失败: {exc}")
        return _fail()

    try:
        # 2.0 Outline Validity Gate（P0 接入 rewrite）：复用新写的细纲有效性/重建逻辑，
        #     但跳过「正文已存在」防误伤（rewrite 的正常前提就是已有正文）。
        _outline, _outline_err = _ensure_outline_for_write(book, chapter_id, allow_body_exists=True)
        if _outline is None:
            _finish("packet", False, f"Outline Validity Gate: {_outline_err}")
            return _fail()
        # 1.4 重写熔断（C-FUSE@1）：backup 之后、prealign 之前——细纲已确定、hash 可比，
        #     避免先跑一轮回补 LLM 才熔断。回执数超上限 / 细纲前提变更 → 停止并交作者决策。
        _fuse = _rewrite_fuse_check(book, chapter_id)
        if _fuse.get("ok") is False:
            _fuse_reason = _fuse.get("reason") or "重写熔断：需作者决定下一步"
            # 2026-11-13 用户裁决：熔断 = 请示作者下一步怎么改。
            # 既不自动继续重写（旧 V13 弹性闸），也不把章节锁死（旧 strict 硬停）。
            # 回执决策卡渲染：继续重写一次（一次性授权）/ 局部修订 / 先改细纲或角色卡 / 接受现状。
            _finish("fuse", False, _fuse_reason)
            return _fail("fuse")
        # 1.5 写前对齐（可选）：检查 → 自动回补细纲 → 重跑（2026-10-16 接入三九）
        #     放在 Outline Validity Gate 之后：先保证细纲有效，再补结构层缺口。
        if prealign:
            _begin("prealign", "写前对齐：架构师审查细纲结构层缺口…")
            _run_prealign_with_repair(book, chapter_id, _finish)
        # 2. 打包（同全流程：细纲 + 前情 + 细纲字段补强）
        _begin("packet", "打包 Writing Packet：拼装上下文…")
        outline, packet = step_packet(book, chapter_id)
        _finish("packet", True, f"细纲: {outline.relative_to(book).as_posix()}", packet=packet)
    except runtime.AgentCancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        _finish("packet", False, f"打包失败: {exc}")
        return _fail()

    try:
        # 3. 主笔生成（只生成，提交原始输出给下一环节）
        #    note 非空 → 原文保持式重写：读本章已提交正文注入提示词（扩写/定点修订用）
        #    note 含“扩到N字”→ 覆盖 packet 字数目标（主笔以此为权威），并供 wordcount 收敛用
        #    level L1/L2（2026-09-20 裁决）→ 强制原文保持式；note 缺省注入等级执行要点
        note_range = _parse_wc_note(note)
        if note_range is not None:
            # CCv2 单轨：note 覆盖只写 v2 wordcount.target（权威）
            _wc = packet.setdefault("wordcount", {})
            _wc["target"] = note_range[1]
            _wc["max"] = max(_wc.get("max") or 0, note_range[1])
        if level in ("L1", "L2", "L3"):
            original_prose = target.read_text(encoding="utf-8")
        else:
            original_prose = target.read_text(encoding="utf-8") if note else ""
        if level in ("L1", "L2") and not note:
            note = _LEVEL_DEFAULT_NOTE[level]
        # 拆条前移（2026-10-25 author_requirements 3/6 事故复盘）：note 非空时先拆出
        # 验收清单，主笔动笔前即拿到（写的标准=查的标准），写后 6.9 复用不再重拆。
        # 清单只拼进传给 step_writer 的 note 副本，原始 note 不动（字数/扩写解析
        # 等下游逻辑零污染）。拆条失败 → None（writer 用原 note，6.9 处再拆兜底）。
        _note_for_writer = note
        if note.strip():
            try:
                _note_items = _decompose_rewrite_note(book, chapter_id, note, original_prose, review_id, dry_run)
            except Exception:  # noqa: BLE001 — 前移拆条失败不阻断主链（6.9 处重拆兜底）
                _note_items = None
            if _note_items:
                _note_for_writer = note + "\n【作者要求验收清单（成稿将逐条核对，每条都要在正文中有着落）】\n" + \
                    "\n".join(f"{i + 1}. {it['item']}" for i, it in enumerate(_note_items))
        # L1/L2（2026-09-20 裁决）：合同快照从原文确定性提取（含事件完成态），
        #   注入 writer 提示词锁定 + normalize 落盘后 contract_guard 硬闸门校验。
        #   2026-10-02：改走 _extract_contract_with_retry（内部重试，见该函数）。
        l1_contract: dict | None = None
        if level == "L1":
            try:
                l1_contract, _attempt_no = _extract_contract_with_retry(
                    book, chapter_id, original_prose, "L1", on_token=None,
                    persist=not dry_run)
                # 2026-10-02：注入上限 3000 → 6000（对齐 L3 的 _l3_c8_sanitize[:6000]）。
                #   3000 会把长合同（实测第2章 12 事件/18 实体）截成半份，Writer 只按半份约束写，
                #   Guard 却按完整合同校验 —— 「写的标准 ≠ 查的标准」，BLOCK 率被人为抬高。
                # 2026-10-10 数值锁生成侧硬指令（用户裁决：管控强化在生成侧）：
                # 合同 JSON 里的 key_values 是裸数据，Writer 曾在重写中把「…二十天的货，」
                # 后的「五百份」整词省略（守卫按设计回滚，实测 395c327f8aee）。裸 JSON 无
                # 指令力，这里把每条数值锁转成显式中文硬指令追加在 note 末尾——
                # 写的标准=查的标准（与 _validate_rewrite_contract 逐字口径一致）。
                _kv_lock = ""
                try:
                    _kvl = [f"「{(k_.get('context') or '').strip()[-20:]}」后必须逐字紧跟「{(k_.get('value') or '').strip()}」"
                            for k_ in (l1_contract.get('key_values') or []) if (k_.get('value') or '').strip()]
                    if _kvl:
                        _kv_lock = ("\n\n【数值锁·硬性】以下每条 context 在新稿中必须原样出现，其后的数值必须逐字紧跟"
                                    "（禁止省略数值、禁止改写形态、禁止挪动位置）：" + "；".join(_kvl))
                except Exception:  # noqa: BLE001 — 指令拼装失败不阻断主流程
                    _kv_lock = ""
                # 2026-10-31 投递修复：合同必须注入 _note_for_writer（主笔实际接收的 note）。
                #   此前追加到 note，但 _note_for_writer 已在 6162 定型、主笔从未收到合同，
                #   导致守卫逐字校验大面积失守；改投 _note_for_writer 同时避免污染下游 note 解析。
                _note_for_writer = (_note_for_writer or "") + ("\n\n【L1 合同锁定（必须全部保持，含事件完成态）】\n"
                                       + json.dumps(l1_contract, ensure_ascii=False, indent=1))[:6000] + _kv_lock
                _finish("analyze", True,
                        f"L1 合同快照：事件 {len(l1_contract.get('core_events', []))}、"
                        f"实体 {len(l1_contract.get('entities', []))}、"
                        f"关系 {len(l1_contract.get('relationships', []))}、"
                        f"数值 {len(l1_contract.get('key_values', []))}"
                        + _contract_attempt_note(_attempt_no),
                        contract=l1_contract)
            except runtime.AgentCancelled:
                raise
            except Exception as _exc:  # noqa: BLE001
                _finish("analyze", False, f"L1 合同分析失败: {_exc}")
                return _fail()
        l2_contract: dict | None = None
        if level == "L2":
            try:
                l2_contract, _attempt_no = _extract_contract_with_retry(
                    book, chapter_id, original_prose, "L2", on_token=None,
                    persist=not dry_run)
                # 2026-10-02：注入上限 3000 → 6000（同 L1，见上）
                # 2026-10-31 投递修复（L2 守卫连败根因，同 L1）：骨架合同必须注入 _note_for_writer
                #   才能到达主笔。此前追加到 note 而 _note_for_writer 已定型 → 主笔盲排结构，
                #   锁定句/结果句大面积丢失（实测第1章两连败：结果句 8/28、事件句 8/29）。
                _note_for_writer = (_note_for_writer or "") + ("\n\n【L2 剧情骨架锁定（核心事件不可删除/合并/替换/新增；"
                                       "可重排呈现方式，事件结果与完成态必须保持）】\n"
                                       + json.dumps(l2_contract, ensure_ascii=False, indent=1))[:6000]
                _finish("analyze", True,
                        f"L2 合同快照：事件 {len(l2_contract.get('core_events', []))}、"
                        f"实体 {len(l2_contract.get('entities', []))}、"
                        f"关系 {len(l2_contract.get('relationships', []))}、"
                        f"数值 {len(l2_contract.get('key_values', []))}"
                        + _contract_attempt_note(_attempt_no),
                        contract=l2_contract)
            except runtime.AgentCancelled:
                raise
            except Exception as _exc:  # noqa: BLE001
                _finish("analyze", False, f"L2 合同分析失败: {_exc}")
                return _fail()
        l3_plan: dict | None = None
        l3_c8: dict | None = None
        if level == "L3":
            try:
                _ol = _find_outline_file(book, chapter_id)
                _outline = _ol.read_text(encoding="utf-8") if _ol else ""
                l3_c8 = _l3_c8_param(original_prose)
                # L3 Plan internal retry（最多 3 次）
                l3_plan = None
                plan_vio = None
                plan_feedback = ""
                plan_history: list[str] = []
                _plan_vio_all: list[str] = []
                _plan_t0 = time.monotonic()
                for plan_attempt in range(1, 4):
                    try:
                        # 2026-09-22 细纲全量注入（原 [:1500] 截断）：38 章细纲约 3.6k 字，
                        # 截断使 Plan 只见第一幕、第三幕/落点全盲——设计半章 Writer 必然
                        # 写不足字数目标（实测三次编译 1396→2167→1863 全 <2940 欠账线）。
                        # 全量后 P8/P9 细纲侧证据与追索集同步变全（方向均为更贴细纲）。
                        # 2026-11-01：不再向前端推 Plan JSON（结构化输出属预览污染，
                        # 前端只在 writer 阶段渲染 stream_text，推了只会挤掉正文预览）。
                        l3_plan = _l3_reproduction_plan(book, chapter_id, original_prose,
                                                         _outline,
                                                         on_token=None,
                                                         feedback=plan_feedback)
                        # P8 据短语确定性校准（2026-10-24）：验证前把近义改写的据短语
                        # 照抄替换为原文真实子串（P9 承接同款）；无近句仍交 P8 判造假。
                        _ju_fix = _repair_l3_ju_phrases(l3_plan, original_prose, _outline)
                        if _ju_fix:
                            plan_history.append(f"尝试{plan_attempt}: 据短语校准 "
                                                + "；".join(_ju_fix[:2]))
                        # 2026-11-06 确定性回修（对接 D/C 门误 BLOCK）：纠正 Planner 在
                        # hard_dependencies 的主体颠倒/凭空细节/悬念闭合，以及 R desc 未落细纲的
                        # 立场断言。fail-closed：只纠正事实，不削弱闸门。
                        _fact_fix = _l3_repair_continuity_facts(l3_plan, _outline)
                        _rneg_fix = _l3_repair_r_negation_grounding(l3_plan, _outline)
                        # 生成侧元语言剥离（C8 占位/执行参数/待落地）：这类条目 D 门
                        # 逐字核查时永远「未落实」，必须在写之前从硬事实清单清掉。
                        _hyg_fix = _l3_repair_hard_dep_hygiene(l3_plan)
                        if _fact_fix or _rneg_fix or _hyg_fix:
                            plan_history.append(f"尝试{plan_attempt}: 连续性/立场/契约卫生回修 "
                                                + "；".join((_fact_fix + _rneg_fix + _hyg_fix)[:2]))
                        plan_vio = _validate_l3_plan(l3_plan, original_prose, _outline)
                        if not plan_vio:
                            break
                        plan_history.append(f"尝试{plan_attempt}: " + "；".join(plan_vio[:3]))
                        if plan_attempt < 3:
                            # 2026-11-01 预算闸：重试前判定（首轮不拦）
                            _analyze_retry_guard(_plan_t0, "L3 Plan 生成", plan_attempt,
                                                 "；".join(plan_vio[:3]))
                            # 违规反馈注入重试（2026-09-22，对齐 Writer 编译循环）。
                            # 2026-10-24 升级为全尝试合并去重：此前只传末次违规，实测
                            # 三次尝试各死不同检查（P5→P3→P8）逐次打地鼠；合并清单
                            # 让模型在下一轮把已知问题一次修全。
                            for _v_ in plan_vio:
                                if _v_ not in _plan_vio_all:
                                    _plan_vio_all.append(_v_)
                            plan_feedback = "；".join(_plan_vio_all[:10])
                            continue
                    except AnalyzeBudgetExceeded:
                        raise  # 预算闸的终止信号，不得被下面的「生成异常」吞成一次普通重试
                    except Exception as _plan_exc:  # noqa: BLE001
                        plan_vio = [f"生成异常: {_plan_exc}"]
                        plan_history.append(f"尝试{plan_attempt}: " + "；".join(plan_vio[:3]))
                        if plan_attempt < 3:
                            # 2026-11-01 预算闸：重试前判定（首轮不拦）
                            _analyze_retry_guard(_plan_t0, "L3 Plan 生成", plan_attempt,
                                                 _plan_exc)
                            continue
                        raise
                if plan_vio:
                    # 全尝试过程透出（2026-09-22）：violations 每轮覆盖，只展示末次会掩盖
                    # 真实拦截链（实测：前两次 P8 拦截漂移被末次 P1 报错覆盖，诊断盲区）。
                    _tail = "；".join(plan_vio[:4])
                    _hist = " | ".join(plan_history)
                    _finish("plan", False,
                            f"L3 Plan 校验 BLOCK（{plan_attempt}/3）：{_tail}"
                            + (f"。全尝试过程：{_hist}" if _hist else ""))
                    return _fail()
                _finish("plan", True,
                        f"L3 Plan：事件 {len(((l3_plan.get('3_new_dramatic_design') or {}).get('event_organization') or []))} 个，"
                        f"R 覆盖 {len((l3_plan.get('4_outcome_mapping') or {}))}/7，"
                        f"C8 执行参数 {l3_c8.get('selected_variant')}（{l3_c8.get('source')}）",
                        plan=l3_plan, c8_param=l3_c8)
            except runtime.AgentCancelled:
                raise
            except Exception as _exc:  # noqa: BLE001
                _finish("plan", False, f"L3 Plan 生成/校验失败: {_exc}")
                return _fail()
        # L3 守卫清单前置（2026-09-22 用户裁决，评分标准前置）：把 L3 Guard 验收口径
        # 确定性提取后注入 Writer prompt，首稿即对齐守卫（写的标准=查的标准）。
        l3_hints = _l3_guard_checklist(l3_plan, original_prose, l3_c8,
                                       wc_target=packet.get("wordcount", {}).get("target"),
                                       outline=_outline) if (level == "L3" and l3_plan) else ""
        # L3 Writer 有界重试（2026-09-22 用户裁决）：Plan 有 3 次校验重试，Writer 此前
        # 一次守卫 BLOCK 即全链死。现引入「守卫违规反馈重编译」：BLOCK → violations 注入
        # hints → 重编译，最多 3 次编译（1 首稿 + 2 重试），末次仍 BLOCK → 回滚 + _fail
        # （原行为不变）。L1/L2 不重试（max_compile=1，单次路径与原直线流程等价）。
        # 2026-10-10：L1 有合同时给 2 次编译（见下方编译内守卫反馈重试），其余不变
        # 2026-10-31：L2 有合同同样 2 次编译——L2 守卫全等级最严（锁定句 2-gram≥0.5+
        #   完成态逐字+结果句≥0.35）却是唯一 max_compile=1 的等级，首稿 BLOCK 即全链死
        #   （实测第1章两连败：结果句 8/28、事件句 8/29），违规清单明确却无自愈通道。
        max_compile = 3 if (level == "L3" and l3_plan) else (2 if (level == "L1" and l1_contract) else (2 if (level == "L2" and l2_contract) else 1))
        l3_feedback = ""
        g_l3: dict | None = None
        draft = clean_draft = ""
        _pg_vio = ""  # P0-3 代词性别闸（重写链对齐）：本轮编译的违规，空=通过
        norm_report = ""
        rel = target.relative_to(book).as_posix()
        _cur_base = original_prose   # 本轮编译基底：字数欠账扩写时切为上稿；original_prose
        _expand_next = False         # 恒为执行前正文，4.3/_rollback_body 回滚语义不变
        _fresh_next = False          # 扩写失效（增量<300）→ 无基底直写通道
        _prev_base_vis = len(re.sub(r"\s+", "", str(original_prose or "")))
        for compile_attempt in range(1, max_compile + 1):
            _hints = l3_hints + ("" if compile_attempt == 1 else
                                 "\n\n【上稿 L3 Guard 违规清单（本次重编译必须逐条修复）】\n" + l3_feedback)
            _use_fresh = (level == "L3" and _fresh_next)
            _use_segexpand = (level == "L3" and l3_plan and _expand_next)
            _begin("writer", "主笔写作中…")
            if _use_segexpand:
                # 分块扩写（2026-09-22 字数治理 v2）：替代单次全稿扩写（服从率 1/6），
                # 每块独立局部调用+程序拼接+块级回退；守卫链在 normalize 后全量复检。
                draft = _segmented_expand(book, chapter_id, packet, _cur_base,
                                          packet.get("wordcount", {}).get("target"), on_token=on_writer_token)
            else:
                draft = step_writer(book, chapter_id, packet, on_token=on_writer_token,
                                    note=_note_for_writer,
                                    original_prose=("" if _use_fresh else _cur_base),
                                    level=level,
                                l2_contract=(l2_contract if level == "L2" else None),
                                l3_plan=(l3_plan if level == "L3" else None),
                                c8_param=(l3_c8 if level == "L3" else None),
                                l3_guard_hints=_hints,
                                expand_mode=(level == "L3" and _expand_next),
                                fresh_write=_use_fresh,
                                wc_target=(packet.get("wordcount", {}).get("target") if level == "L3" else None))
            _finish("writer" if compile_attempt == 1 else f"writer_retry{compile_attempt}", True,
                    f"生成 {len(draft)} 字（{level}，编译 {compile_attempt}/{max_compile}）"
                    + _wc_stage_note(), output=draft)

            # 4. 正文归一环节（独立正规环节：prose 提取 + 元信息清洗 + 自动跨章去重）→ 覆盖落盘
            #    （重写唯一允许覆盖的点，旧稿已备份）
            clean_draft, norm_report = step_normalize(book, chapter_id, draft)
            _ns = "normalize" if compile_attempt == 1 else f"normalize_retry{compile_attempt}"
            if dry_run:
                _finish(_ns, True,
                        f"dry-run 未落盘（{len(clean_draft)} 字；{norm_report}；编译 {compile_attempt}/{max_compile}）",
                        chars=len(clean_draft))
            else:
                target.write_text(clean_draft + "\n", encoding="utf-8")
                body_written["done"] = True
                _finish(_ns, True,
                        f"覆盖落盘: {rel}（{len(clean_draft)} 字；{norm_report}；编译 {compile_attempt}/{max_compile}）",
                        path=rel, chars=len(clean_draft))
            # 代词性别机械闸（P0-3 重写链对齐，2026-10-25）：每轮编译后确定性检查；
            # 违规合成一条编译违规注入各级守卫（L3/L1 走反馈重试），终判兜底见 4.05。
            _pg_vio = ""
            try:
                _pg_ok, _pg_msg = _pronoun_gate_check(book, chapter_id, clean_draft)
                _finish("pronoun_gate" if compile_attempt == 1 else f"pronoun_gate_retry{compile_attempt}",
                        _pg_ok, _pg_msg)
                if not _pg_ok:
                    _pg_vio = f"代词性别错位（机械闸）：{_pg_msg}"
            except runtime.AgentCancelled:
                raise
            except Exception as _pg_exc:  # noqa: BLE001 — 闸异常不挂编译主链
                print(f"[PRONOUN_GATE] 异常跳过: {_pg_exc}", flush=True)
            if level == "L3" and l3_plan:
                # L3 Guard 编译内校验（重试判定依据）；异常转违规，4.3 统一回滚处理。
                # 基准用 _cur_base：首轮=执行前正文（原行为），扩写轮=上稿（上稿已过
                # 六门，扩写稿须相对上稿保持锚点/时间/C8，语义自洽）。
                try:
                    g_l3 = _validate_l3_contract(_cur_base,
                                                 clean_draft if dry_run else target.read_text(encoding="utf-8"),
                                                 l3_plan, l3_c8, wc_target=packet.get("wordcount", {}).get("target"),
                                                 outline=_outline)
                except Exception as _g_exc:  # noqa: BLE001
                    g_l3 = {"violations": [f"L3 Guard 执行异常: {_g_exc}"], "gates": {"X_Internal": "BLOCK"}}
                if _pg_vio:
                    g_l3["violations"].append(_pg_vio)
                if not g_l3["violations"]:
                    break
                l3_feedback = "；".join(g_l3["violations"][:6])
                # 缺失型违规转增量扩写（2026-09-22 字数治理）：欠账稿的欠账段（章末钩子/
                # 落点/硬依赖场景）未写 → 违规必然伴随「证据不足/缺失/未落地」+「字数欠账」
                # 成簇出现（实测 attempt1: B 门证据不足 + D 门证据不足 + G_Length 三条）。
                # 缺失型=内容没写到，扩写补内容即消 → 保留上稿为扩写基底（不回滚），下一轮
                # expand_mode 递进补足；写歪型（漂移/混用/翻转/零变化）扩写会保留错误骨架，
                # 必须保持原行为（回滚执行前正文+反馈重编译）。
                def _is_expandable_violation(_v: str) -> bool:
                    return (("证据不足" in _v) or ("字数欠账" in _v)
                            or ("缺失" in _v) or ("未落地" in _v))
                _deficiency_only = bool(g_l3["violations"]) and all(
                    _is_expandable_violation(_v) for _v in g_l3["violations"])
                print(f"[L3_COMPILE] attempt={compile_attempt}/{max_compile} "
                      f"violations={len(g_l3['violations'])} deficiency_only={_deficiency_only} "
                      f"feedback={l3_feedback[:150]}", flush=True)
                if compile_attempt < max_compile and not dry_run:
                    if _deficiency_only:
                        # 扩写失效判定（2026-09-22 字数治理·第二通道）：本轮是扩写轮且
                        # 增量 <300 字（实测 +8/+18/原样返回）→ 单通道扩写不可信，下一轮
                        # 切 fresh_write 无基底直写（守卫基准 _cur_base 保持不变）；
                        # 增量 ≥300（扩写生效但未够）→ 继续以新上稿为基底扩写递进。
                        _this_base_vis = len(re.sub(r"\s+", "",
                                                     (target.read_text(encoding="utf-8") if not dry_run else clean_draft)))
                        _gain = _this_base_vis - _prev_base_vis
                        if _expand_next and _gain < 300:
                            _fresh_next = True
                            _expand_next = False
                            print(f"[L3_COMPILE] 扩写失效（增量 {_gain} 字 <300）→ 切 fresh_write 直写通道", flush=True)
                        else:
                            _expand_next = True
                            _fresh_next = False
                        _prev_base_vis = _this_base_vis
                        if not _fresh_next:
                            try:
                                _cur_base = target.read_text(encoding="utf-8")
                            except Exception:  # noqa: BLE001 — 上稿读取失败，退回原回滚路径
                                _cur_base = original_prose
                                _expand_next = False
                                _fresh_next = False
                                try:
                                    target.write_text(original_prose, encoding="utf-8")
                                    body_written["done"] = False
                                except Exception:  # noqa: BLE001
                                    pass
                    else:
                        _expand_next = False
                        _fresh_next = False
                        # 重编译前回滚：守卫失败稿不残留在生产正文
                        try:
                            target.write_text(original_prose, encoding="utf-8")
                            body_written["done"] = False
                        except Exception:  # noqa: BLE001 — 回滚失败继续重试，4.3 兜底
                            pass
                continue
            if level == "L1" and l1_contract:
                # 2026-10-10 L1 合同守卫编译内校验（对齐 L3 反馈重试先例，用户裁决管控强化）：
                # 此前 L1 合同守卫只在 4.1 终判（单发即回滚失败），Writer 偶发丢数值锁
                # （实测第2章两连跑丢「五百份」，395c327f8aee / d074ce662c04）无自愈通道。
                # 违规注入反馈重编译一次；仍 BLOCK 落到 4.1 终判（回滚+失败，原行为不变）。
                try:
                    _l1_vio = _validate_rewrite_contract(
                        original_prose,
                        clean_draft if dry_run else target.read_text(encoding="utf-8"),
                        l1_contract)
                except Exception as _g1_exc:  # noqa: BLE001 — 异常转违规，4.1 统一处置
                    _l1_vio = [f"L1 守卫执行异常: {_g1_exc}"]
                if _pg_vio:
                    _l1_vio.append(_pg_vio)
                if not _l1_vio:
                    break
                print(f"[L1_COMPILE] attempt={compile_attempt}/{max_compile} "
                      f"violations={len(_l1_vio)} feedback={';'.join(_l1_vio)[:150]}", flush=True)
                if compile_attempt < max_compile and not dry_run:
                    # 2026-10-31 投递修复：反馈须注入 _note_for_writer 才能到达主笔
                    #   （此前追加到 note，重编译稿收不到违规清单，反馈重试形同虚设）
                    _note_for_writer = (_note_for_writer or "") + ("\n【上一稿 L1 合同守卫违规（本轮必须逐条修复，"
                                            "数值锁必须逐字保留）】" + "；".join(_l1_vio[:6]))
                    # 重编译前回滚：守卫失败稿不残留在生产正文（同 L3 非缺失路径）
                    try:
                        target.write_text(original_prose, encoding="utf-8")
                        body_written["done"] = False
                    except Exception:  # noqa: BLE001
                        pass
                    continue
            if level == "L2" and l2_contract:
                # 2026-10-31 L2 合同守卫编译内校验（对齐 L1 反馈重试先例，用户裁决管控强化）：
                # L2 守卫全等级最严（锁定句 2-gram≥0.5 + 完成态逐字 + 结果句≥0.35），
                # 此前却是唯一 max_compile=1 的等级——首稿 BLOCK 即全链死（实测第1章两连败：
                # 结果句 8/28、事件句 8/29）。根因之一为合同 JSON 未送达主笔（见 6219 投递修复）；
                # 此处再补违规反馈重编译：BLOCK → violations 注入 _note_for_writer → 重编译一次；
                # 仍 BLOCK 落到 4.2 终判（回滚+失败，原行为不变）。口径与终判一致：合同+零变化+时间事实。
                try:
                    _l2_txt = clean_draft if dry_run else target.read_text(encoding="utf-8")
                    _l2_vio = _validate_rewrite_contract(original_prose, _l2_txt, l2_contract, level="L2")
                    if _l2_txt.rstrip("\n") == original_prose.rstrip("\n"):
                        _l2_vio.append("L2 结构重写未发生：输出与原正文逐字一致，必须重组叙事结构而非原样返回")
                    # 时间事实守卫（与 4.2 终判同口径；L2 路径 _outline 为 Path，先读文本）
                    _l2_outline_txt = ""
                    if isinstance(_outline, Path):
                        try:
                            _l2_outline_txt = _outline.read_text(encoding="utf-8")
                        except Exception:  # noqa: BLE001
                            _l2_outline_txt = ""
                    elif isinstance(_outline, str):
                        _l2_outline_txt = _outline
                    _l2_vio.extend(_check_time_facts(original_prose, _l2_txt, outline=_l2_outline_txt))
                except Exception as _g2_exc:  # noqa: BLE001 — 异常转违规，4.2 统一处置
                    _l2_vio = [f"L2 守卫执行异常: {_g2_exc}"]
                if _pg_vio:
                    _l2_vio.append(_pg_vio)
                if not _l2_vio:
                    break
                print(f"[L2_COMPILE] attempt={compile_attempt}/{max_compile} "
                      f"violations={len(_l2_vio)} feedback={';'.join(_l2_vio)[:150]}", flush=True)
                if compile_attempt < max_compile and not dry_run:
                    _note_for_writer = (_note_for_writer or "") + (
                        "\n【上一稿 L2 合同守卫违规（本轮必须逐条修复；锁定句/结果句/完成态词元必须"
                        "逐字或近字保留，事件不可删除/合并/替换/新增，不得原样返回原文）】"
                        + "；".join(_l2_vio[:6]))
                    # 重编译前回滚：守卫失败稿不残留在生产正文（同 L1/L3）
                    try:
                        target.write_text(original_prose, encoding="utf-8")
                        body_written["done"] = False
                    except Exception:  # noqa: BLE001
                        pass
                    continue
            break

        # 4.1 L1 合同守卫（2026-09-20 裁决）：确定性剧情保持硬闸门（fail-closed）。
        #     区分「事件存在」与「事件完成」——completion 完成态词元缺失即事件未完成 → BLOCK。
        #     越界 → 回滚 L1 执行前正文；不 commit、不产生新 revision/delta（后续阶段全部不跑）。
        if level == "L1" and l1_contract:
            try:
                final_text = target.read_text(encoding="utf-8")
                violations = _validate_rewrite_contract(original_prose, final_text, l1_contract)
                if violations:
                    try:
                        target.write_text(original_prose, encoding="utf-8")
                        rollback = "已回滚 L1 执行前正文"
                    except Exception:  # noqa: BLE001
                        rollback = "回滚失败，请人工从 .backup/ 恢复"
                    _finish("contract_guard", False,
                            f"L1 合同守卫 BLOCK（{len(violations)} 项）：" + "；".join(violations[:3]) + f"。{rollback}")
                    return _fail()
                _finish("contract_guard", True,
                        "L1 合同守卫通过（事件完成态/实体/关系/数值/章节目标全部锁定）")
            except runtime.AgentCancelled:
                _rollback_on_cancel()
                raise
            except Exception as exc:  # noqa: BLE001
                _rb = _rollback_body()
                _finish("contract_guard", False, f"L1 合同守卫异常: {exc}" + (f"；{_rb}" if _rb else ""))
                return _fail()
        if level == "L2" and l2_contract:
            try:
                final_text = target.read_text(encoding="utf-8")
                # 零变化检查：L2 要求结构重排，输出与原正文逐字一致 = 功能未发生 → BLOCK
                zero_change = final_text.rstrip("\n") == original_prose.rstrip("\n")
                violations = _validate_rewrite_contract(original_prose, final_text, l2_contract, level="L2")
                # 时间事实守卫（确定性，不依赖 LLM analyze）：数值+单位组合双向一致
                # 2026-10-31 类型修复（实测事故）：L2 路径下 _outline 是 Path
                #   （_ensure_outline_for_write 返回路径；仅 L3 分支在 6236 重赋为文本），
                #   直接传 Path 进正则 → TypeError「got 'PosixPath'」守卫异常回滚。
                _outline_text = ""
                if isinstance(_outline, Path):
                    try:
                        _outline_text = _outline.read_text(encoding="utf-8")
                    except Exception:  # noqa: BLE001 — 细纲读失败按无豁免处理，不误放行
                        _outline_text = ""
                elif isinstance(_outline, str):
                    _outline_text = _outline
                time_vio = _check_time_facts(original_prose, final_text, outline=_outline_text)
                if zero_change or violations or time_vio:
                    try:
                        target.write_text(original_prose, encoding="utf-8")
                        rollback = "已回滚 L2 执行前正文"
                    except Exception:  # noqa: BLE001
                        rollback = "回滚失败，请人工从 .backup/ 恢复"
                    if zero_change:
                        reason = "L2结构重写未发生：输出与原正文一致"
                    elif time_vio:
                        reason = "L2 时间事实守卫 BLOCK：" + "；".join(time_vio[:2])
                    else:
                        reason = f"L2 合同守卫 BLOCK（{len(violations)} 项）：" + "；".join(violations[:3])
                    _finish("contract_guard", False, f"{reason}。{rollback}")
                    return _fail()
                _finish("contract_guard", True,
                        "L2 合同守卫通过（事件结果/关系/数值/章节目标/最终结果锁定；结构重写已发生）")
            except runtime.AgentCancelled:
                _rollback_on_cancel()
                raise
            except Exception as exc:  # noqa: BLE001
                _rb = _rollback_body()
                _finish("contract_guard", False, f"L2 合同守卫异常: {exc}" + (f"；{_rb}" if _rb else ""))
                return _fail()
        if level == "L3" and l3_plan:
            # 4.3 L3 Guard 结果处理（守卫校验已在上文编译循环内完成，含 ≤2 次违规反馈重试）。
            #     末次仍违规 → 回滚 L3 执行前正文；不 commit、不产生新 revision/delta。
            if g_l3 and g_l3.get("violations"):
                _bad = "、".join(f"{k}={_gv}" for k, _gv in g_l3["gates"].items() if _gv != "PASS") or "Guard"
                _vio_txt = "；".join(g_l3["violations"][:4])
                reason = f"L3 Guard BLOCK（{_bad}）：{_vio_txt}"
                if _rewrite_gate_v13_degrade(book, chapter_id, "contract_guard/L3", reason):
                    _finish("contract_guard", True,
                            "L3 Guard 事实已记录（V13：不回滚、不阻断，裁决权交作者）：" + reason,
                            gates=(g_l3 or {}).get("gates", {}))
                elif dry_run:
                    _finish("contract_guard", False, reason + "（dry-run 未落盘，无需回滚）")
                    return _fail()
                else:
                    _rb = _rollback_body()
                    if _rb:
                        reason += "。" + _rb
                    _finish("contract_guard", False, reason)
                    return _fail()
            _finish("contract_guard", True,
                    "L3 Guard 通过：" + " ".join(f"{k}={_gv}" for k, _gv in (g_l3 or {}).get("gates", {}).items()),
                    gates=(g_l3 or {}).get("gates", {}))
        if dry_run:
            _finish("dry_run", True, "L3 dry-run 完成：未落盘、未 commit、零生产状态写入")
            return {"ok": True, "chapter_id": chapter_id, "mode": "revision", "level": level,
                    "dry_run": True, "stages": [s_.to_dict() for s_ in stages]}
    except runtime.AgentCancelled:
        # F1（2026-09-22）：取消时若新稿已覆盖正文 → 回滚执行前正文（不经 progress，防二次取消）
        _rollback_on_cancel()
        raise
    except Exception as exc:  # noqa: BLE001
        _rb = _rollback_body()
        _finish("normalize", False, f"主笔生成/正文归一失败: {exc}" + (f"；{_rb}" if _rb else ""))
        return _fail()

    # 4.05 代词性别机械闸终判（P0-3 重写链对齐）：编译循环已给 L3/L1 反馈重试通道，
    # 此处对最终落盘稿终判，兜底无合同守卫的等级（L2/默认路径）。
    if _pg_vio:
        try:
            _pg_final_ok, _pg_final_msg = _pronoun_gate_check(
                book, chapter_id, target.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            _pg_final_ok, _pg_final_msg = False, _pg_vio
        _finish("pronoun_gate_final", _pg_final_ok, _pg_final_msg)
        if not _pg_final_ok:
            _rb = _rollback_body()
            _finish("pronoun_gate_block", False,
                    f"代词性别机械闸 BLOCK：{_pg_final_msg}" + (f"；{_rb}" if _rb else ""))
            return _fail()

    # 4.5 字数收敛（D1，revision 同全流程；note 显式目标字 → override 判定目标）
    try:
        wc = step_wordcount_check(book, chapter_id)
        note_range = (_parse_wc_note(note) if note else None)
        if not wc["ok"]:
            _finish("wordcount", False, f"字数检查失败（advisory，继续）: {wc.get('detail', '')}")
        else:
            wc = _apply_wc_note_override(wc, note_range)
            # 2026-11-05 闭环化：纠偏预算 ≤2 轮（compress/expand 统一循环，每轮复检）；
            # 进带或预算尽 → 如实标记交下游质检（质检无条件执行）与 commit 闸裁决。
            # 触发面维持现语义：compress=脚本 over action；expand=under 且低于硬线 0.95
            # （note 显式目标的 under 仍归作者自担，不自动扩，2026-10-24 裁决保持）。
            # 篇幅平衡 v1（T21）：NWW_WC_PLAN_MODE=1 且纯函数模块可用 → 方案化扩缩；
            # 其余（含 NWW_WC_PLAN_MODE=0）完全走下面旧 2 轮盲扩/盲缩（逐字不变）。
            _lb = None
            if _wc_plan_mode() and _balance_mod() is not None:
                _lb = step_length_balance(book, chapter_id, packet, target, wc,
                                          dry_run=dry_run, progress=None)
            if _lb is not None and not _lb.get("degraded"):
                _wc_final = _lb.get("wordcount") or wc
                if note_range and _wc_final.get("ok"):
                    # note 显式目标口径与旧链一致：复检结果按 note 区间重判
                    _wc_final = _apply_wc_note_override(_wc_final, note_range)
                _st = _wc_final.get("status", "?")
                _in_band = _st in ("internal_pass", "borderline", "ok")
                _finish("wordcount", _in_band, _lb.get("detail") or "",
                        wordcount=_wc_final,
                        balance_plan={k: _lb.get(k) for k in (
                            "rounds", "selected_strategy", "quality_score",
                            "net_delta", "residual_gap", "stop_reason", "audit_path")})
            else:
                _wc_final = wc
                _acted = 0
                _expand_n = 0
                _compress_n = 0
                _wc_start_actual = _wc_final.get("actual") or 0
                for _attempt in range(2):
                    _acts = _wc_final.get("actions") or []
                    if "commit" in _acts:
                        break
                    if "compress-once" in _acts:
                        try:
                            step_compress_once(book, chapter_id, packet, target, _wc_final)
                            _acted += 1
                            _compress_n += 1
                        except Exception as cexp:  # noqa: BLE001
                            _finish("wordcount", False, f"compress-once 第{_attempt+1}轮失败: {cexp}")
                            break
                    elif _wc_final.get("status") == "under":
                        if note_range:
                            # note 显式目标-driven 的 under：作者明示的合理欠账，不自动扩
                            break
                        # 2026-10-31 修复沿用：细纲字数目标可能是 "2500-2750" 范围串，
                        # 复用 _wc_target_band（同 step_compress_once 口径）解析下限。
                        _m_target, _m_tmax = _wc_target_band(_wc_final.get("target"))
                        # M2：默认带外即扩（status=under 已按 ±10% 带判定）；
                        # env NWW_WC_EXPAND_BAND=0 回滚旧硬线触发面（<0.95×下限）
                        if _wc_expand_on_band() or (
                                _m_target and (_wc_final.get("actual") or 0) < _m_target * _wc_hardline_ratio()):
                            try:
                                _need = max(200, int(_m_target - (_wc_final.get("actual") or 0)))
                                step_expand_once(book, chapter_id, packet, target, _wc_final, _need)
                                _acted += 1
                                _expand_n += 1
                            except Exception as eexp:  # noqa: BLE001
                                _finish("wordcount", False, f"expand-once 第{_attempt+1}轮失败: {eexp}")
                                break
                        else:
                            break
                    else:
                        break
                    _wc_final = step_wordcount_check(book, chapter_id)
                    if _wc_final.get("ok") and note_range:
                        _wc_final = _apply_wc_note_override(_wc_final, note_range)
                    if not _wc_final.get("ok"):
                        break
                _st = _wc_final.get("status", "?")
                _in_band = _st in ("internal_pass", "borderline", "ok")
                # 2026-11-06：同 write 链，透出扩写/压缩轮数与净增字数，避免「纠偏 N 轮」
                # 看起来像什么都没做。
                _wc_delta = (_wc_final.get("actual") or 0) - _wc_start_actual
                _finish("wordcount", _in_band,
                        f"字数 {_wc_final.get('actual')}（{_st}），纠偏 {_acted} 轮"
                        f"（扩写 {_expand_n} / 压缩 {_compress_n}，净增 {_wc_delta:+d} 字）"
                        + ("" if _in_band else "（仍带外，如实标记；下游质检与 commit 闸照常执行）")
                        + (f"；篇幅平衡降级：{_lb.get('detail')}" if _lb is not None else ""),
                        wordcount=_wc_final)
    except runtime.AgentCancelled:
        _rollback_on_cancel()
        raise
    except Exception as exc:  # noqa: BLE001
        _finish("wordcount", False, f"字数收敛异常（继续）: {exc}")

    # 4.6 段落复检（2026-09-22 方案A 新增）：wordcount 后按「最长段≤120」验收口径
    #     复检，孤立长段（120-220 区间、不成连片，gate 拦不住）确定性拆段（零文字
    #     损失）。advisory：复检异常不 fail 链路，post_write 的 gate J1/J2 仍兜底。
    #     拆段只动段落边界，可见字数零变化，无需重跑 wordcount。
    try:
        _p120 = _enforce_para_max120(target)
        if _p120:
            _finish("paragraph_120", True, "复检拆段: " + "；".join(_p120[:8]))
        else:
            _finish("paragraph_120", True, "最长段 ≤120，无需拆段")
    except runtime.AgentCancelled:
        _rollback_on_cancel()
        raise
    except Exception as exc:  # noqa: BLE001
        _finish("paragraph_120", False, f"段落复检异常（继续）: {exc}")

    # 4.7 对话独立成段复检（2026-09-22 新增）：说话内容必须另起一段（用户裁决的
    #     网文格式标准）。「叙述+对话」混合段按引号边界确定性拆分（引语段/对话段/
    #     尾部叙述段），零文字损失，advisory 不阻断。
    try:
        _dlg = _enforce_dialogue_split(target)
        if _dlg:
            _finish("dialogue_split", True, f"对话拆段: {len(_dlg)}处 — " + "；".join(_dlg[:6]))
        else:
            _finish("dialogue_split", True, "对话均已独立成段")
    except runtime.AgentCancelled:
        _rollback_on_cancel()
        raise
    except Exception as exc:  # noqa: BLE001
        _finish("dialogue_split", False, f"对话复检异常（继续）: {exc}")

    try:
        # 5. delta 草稿（对已提交章自动 revision 模式）
        draft_path = step_delta_draft(book, chapter_id)
        _finish("delta_draft", True, f"草稿: {draft_path.relative_to(book).as_posix()}")
    except runtime.AgentCancelled:
        _rollback_on_cancel()
        raise
    except Exception as exc:  # noqa: BLE001
        # ①（2026-10-03 外脑裁定）：此前与 ② 共用一个 handler，正文新稿留在盘上、阶段名
        #   还被误标成 delta_semantic → 「正文=新稿 + 无 delta + 无台账」= UNDEFINED。
        #   现在独立成阶段 + 事务回滚（正文 + delta + 台账回到 rewrite 前）。
        _rb = _rollback_transaction()
        _finish("delta_draft", False, f"delta 草稿生成失败: {exc}" + (f"；{_rb}" if _rb else ""))
        return _fail()

    try:
        # 6. delta 补语义（重写链随后必走 review_cycle finalize 重写指纹，故不在此重绑）
        step_delta_semantic(book, chapter_id, draft_path, target)
        _finish("delta_semantic", True, "语义字段已补全")
    except runtime.AgentCancelled:
        _rollback_on_cancel()
        raise
    except Exception as exc:  # noqa: BLE001
        # ②（2026-10-03 外脑裁定）：最危险的一个 —— delta 已落盘（实测 10382B）但台账
        #   未建立，正文又是新稿 = 半恢复态。必须连 delta 一起回滚，不能只恢复正文。
        _rb = _rollback_transaction()
        _finish("delta_semantic", False, f"delta 语义补全失败: {exc}" + (f"；{_rb}" if _rb else ""))
        return _fail()

    # 6.5 一致性审查（D2 detection，revision 同全流程）→ 闸1·硬伤层闭环修复
    #     （2026-10-25 作者裁决：系统检出=系统修复，作者只输出意见，落笔由系统执行）：
    #     检出 S1/S2 → 裁决消解（带设定原文，破「待核对」类伪硬伤）→ 定向修复正文
    #     → 复检，≤2 轮；清零放行。耗尽 → 事务回滚，但报告携带系统起草的可执行修复
    #     方案（含设定修订草案，落审查记录 repair 块），作者对方案给意见后由系统执行
    #     ——设定文件绝不自动改写（创作权在作者，系统只起草）。S3/S4 瑕疵不拦。
    try:
        cons = step_consistency_check(book, chapter_id, target)
        _repair_trail: list[dict] = []
        if cons.get("ok") and int(cons.get("open_s1_s2_count") or 0) > 0:
            for _round in (1, 2):
                _findings = cons.get("open_s1_s2") or []
                _rep = _repair_consistency_findings(book, chapter_id, target, _findings, _round)
                if _rep is None:
                    break
                _repair_trail.append({
                    "round": _round, "findings": len(_findings),
                    "resolved": len(_rep.get("resolved", [])),
                    "repaired": _rep.get("repaired", 0),
                    "resolved_detail": _rep.get("resolved", []),
                })
                cons = step_consistency_check(book, chapter_id, target)
                if not cons.get("ok") or int(cons.get("open_s1_s2_count") or 0) == 0:
                    break
        if not cons.get("ok"):
            # 检测未完成 → advisory（检测工具自身故障不杀稿），原 ③ 语义
            _advisory_fail("consistency", f"一致性检查失败: {cons.get('detail', '')}")
            _finish("consistency", False, f"一致性检查失败（advisory，继续）: {cons.get('detail', '')}")
        elif int(cons.get("open_s1_s2_count") or 0) > 0:
            _rb = _rollback_transaction()
            _remaining = cons.get("open_s1_s2") or []
            _proposals = _draft_consistency_proposals(book, chapter_id, _remaining)
            if cons.get("record_path"):
                _append_consistency_repair_block(Path(cons["record_path"]), _repair_trail, _proposals)
            _finish(
                "consistency", False,
                f"一致性硬伤未清零（S1/S2={cons['open_s1_s2_count']}，系统修复 {len(_repair_trail)} 轮）"
                f"——事务回滚，不提交。系统已为剩余硬伤起草可执行修复方案"
                f"（{'含设定修订草案，' if _proposals else ''}见审查记录 repair 块）；"
                f"作者对方案给意见/拍板后由系统执行"
                + (f"；{_rb}" if _rb else ""),
                consistency=cons, repair_trail=_repair_trail, proposals=_proposals,
            )
            return _fail()
        else:
            _detail = (f"审查记录: {cons.get('record_path', '')}"
                       f"（S1/S2={cons.get('open_s1_s2_count', 0)}，S3/S4={cons.get('s3_s4_count', 0)}）")
            if _repair_trail:
                _detail += f"（系统修复 {len(_repair_trail)} 轮消解硬伤，轨迹见 repair 字段）"
            # 复检清零 → 系统自动落闭环记录（2026-11-04），消除「检测修好却卡 A-3」断裂。
            _closure = step_consistency_closure(book, chapter_id, cons)
            if _closure.get("closed"):
                _detail += f"；自动闭环已落盘（{_closure.get('closure_path', '')}）"
            _finish("consistency", True, _detail, consistency=cons, repair_trail=_repair_trail,
                    closure=_closure)
    except runtime.AgentCancelled:
        _rollback_on_cancel()
        raise
    except Exception as exc:  # noqa: BLE001
        _advisory_fail("consistency", f"一致性审查异常: {exc}")
        _finish("consistency", False, f"一致性审查异常（继续）: {exc}")

    # 6.6 deslop 机械层（advisory）—— R-WP1 前移（2026-09-15 外脑裁定选甲，与 write 链同一原则）：
    #    deslop 会改写正文，须在 review_cycle 与 commit 之前。顺序：consistency → deslop → review_cycle → post_write。
    try:
        reports = step_deslop(book, chapter_id, target)
        _finish("deslop", True, f"脚本数: {len(reports)}", reports=reports)
    except runtime.AgentCancelled:
        _rollback_on_cancel()
        raise
    except Exception as exc:  # noqa: BLE001
        # ④（2026-10-03 外脑裁定）：此处原本是**裸调用**，异常直接穿出 run_rewrite_pipeline
        #   —— 调用方拿不到 stages、看不到失败原因，盘上却已是「正文新稿 + delta 已落盘 +
        #   无台账」的半恢复态。统一失败出口 + 事务回滚，禁止裸异常逃逸。
        _rb = _rollback_transaction()
        _finish("deslop", False, f"deslop 异常（事务回滚）: {exc}" + (f"；{_rb}" if _rb else ""))
        return _fail()

    # 6.65 enforce 前移 + 作者要求验收前置（2026-10-26 修复重写链「审后改文」时序倒置）：
    #      任何会修改正文的步骤（enforce、author_requirements 定向修订）必须在 review_cycle
    #      finalize 冻结哈希之前完成，确保「审的 = 冻结的 = 交的」（与 write 链 5.65 对齐）。
    try:
        enf = step_pre_review_enforce(book, chapter_id, target, draft_path)
        _finish("pre_review_enforce", True,
                "已执行（finalize 前）" if enf.get("ok")
                else f"失败（WARNING 继续）: {str(enf.get('stderr', ''))[:120]}",
                returncode=enf.get("returncode"))
    except runtime.AgentCancelled:
        _rollback_on_cancel()
        raise
    except Exception as exc:  # noqa: BLE001
        _advisory_fail("pre_review_enforce", f"enforce 前移执行异常: {exc}")
        _finish("pre_review_enforce", False, f"enforce 前移执行异常（继续）: {exc}")

    # 6.68 作者要求验收（前移至 review_cycle 之前，避免 finalize 后 _review_revise_once 破坏冻结哈希）：
    if note.strip():
        try:
            _items = _note_items if _note_items is not None else \
                _decompose_rewrite_note(book, chapter_id, note, original_prose, review_id, dry_run)
            if _items is None:
                _advisory_fail("author_requirements", "重写要求拆条目失败（advisory，继续）")
                _finish("author_requirements", False, "拆条目失败（advisory，继续）")
            else:
                _ver = _verify_rewrite_requirements(book, chapter_id, target, _items, review_id, dry_run)
                if _ver is None:
                    _advisory_fail("author_requirements", "重写要求验收核对失败（advisory，继续）")
                    _finish("author_requirements", False,
                            f"验收核对失败（{len(_items)} 条目，advisory，继续）")
                elif _ver["unaddressed"]:
                    _fix_prompt = (
                        "【作者重写要求 · 未兑现条目（必须逐条落实）】\n"
                        + "\n".join(f"- 要求: {u['item']}｜核验说明: {u['evidence'] or '(未给出)'}"
                                    for u in _ver["unaddressed"])
                        + "\n只针对以上条目修订，不改动已达标部分，禁止缩写。")
                    _review_revise_once(book, chapter_id, target, _fix_prompt, 1)
                    _ver2 = _verify_rewrite_requirements(book, chapter_id, target, _items, review_id, dry_run)
                    if _ver2 is None or _ver2["unaddressed"]:
                        _left = _ver2["unaddressed"] if _ver2 else _ver["unaddressed"]
                        _rb = _rollback_transaction()
                        _finish("author_requirements", False,
                                f"作者要求未兑现（{len(_left)}/{_ver['total']} 条未着落）——事务回滚，不提交；"
                                f"条目与证据见 追踪/验收/第{chapter_id:03d}章.json"
                                + (f"；{_rb}" if _rb else ""),
                                unaddressed=_left)
                        return _fail()
                    _finish("author_requirements", True,
                            f"定向修订后全着落（{_ver['total']} 条）")
                else:
                    _finish("author_requirements", True,
                            f"{_ver['total']} 条全着落（追踪/验收/第{chapter_id:03d}章.json）")
        except runtime.AgentCancelled:
            _rollback_on_cancel()
            raise
        except Exception as exc:  # noqa: BLE001
            _advisory_fail("author_requirements", f"作者要求验收异常: {exc}")
            _finish("author_requirements", False, f"作者要求验收异常（advisory，继续）: {exc}")

    # 6.7 审稿闭环（D3，时序对齐全流程：commit 前把 reviewed 准备好）
    try:
        _begin("review_cycle", "审稿闭环：LLM 多轮评分 + 自动修订 + finalize 冻结")
        rv = step_review_cycle(book, chapter_id, target, draft_path, review_id)
        _rv_best = float(rv.get("best_score") or 0)
        _rv_floor = rv.get("floor_violations") or []
        _rv_promise = rv.get("promise_violations") or []
        if not rv["ok"]:
            _rb = _rollback_transaction()
            _finish("review_cycle", False,
                    f"审稿闭环失败（事务回滚，不提交；草稿证据见 _review_snapshots/ 与评分记录）: "
                    f"{rv.get('detail', '')}" + (f"；{_rb}" if _rb else ""))
            return _fail()
        if not rv["passed"] and (_rv_best < _REVIEW_PASS_SCORE or _rv_floor or _rv_promise):
            _reason = (f"最高分 {_rv_best} < {_REVIEW_PASS_SCORE}" if _rv_best < _REVIEW_PASS_SCORE
                       else f"最高分 {_rv_best} 达标但维度地板硬约束未达标: {', '.join(_rv_floor)}" if _rv_floor
                       else f"最高分 {_rv_best} 达标但细纲承诺未兑现（证据验收）: {'; '.join(_rv_promise[:3])}")
            if _rewrite_gate_v13_degrade(book, chapter_id, "review_cycle/score", _reason):
                _finish("review_cycle", True,
                        f"审稿未达过闸标准（V13 事实留痕，继续提交）：{_reason}，"
                        f"{rv.get('rounds', 0)} 轮",
                        review=rv)
            else:
                _rb = _rollback_transaction()
                _finish("review_cycle", False,
                        f"审稿未通过（{_reason}，{rv.get('rounds', 0)} 轮）——事务回滚，不提交"
                        + (f"；{_rb}" if _rb else ""))
                return _fail()
        _finish(
            "review_cycle", True,
            f"{'通过' if rv['passed'] else '未通过'}（最高分 {rv['best_score']}，{rv['rounds']} 轮）",
            review=rv,
        )
    except runtime.AgentCancelled:
        _rollback_on_cancel()
        raise
    except Exception as exc:  # noqa: BLE001 — P1 边界项2：审稿异常=无验收结果，事务回滚不提交
        _rb = _rollback_transaction()
        _finish("review_cycle", False,
                f"审稿闭环异常（事务回滚，不提交；草稿证据见 _review_snapshots/ 与评分记录）: {exc}"
                + (f"；{_rb}" if _rb else ""))
        return _fail()

    # 6.8 二次复检（若拆段改变了正文字节，同步重绑 draft_path 的 reviewed_hash/final_hash/target_body_md5）：
    try:
        _p120b = _enforce_para_max120(target)
        _dlgb = _enforce_dialogue_split(target)
        parts = []
        if _p120b:
            parts.append("长段拆: " + str(len(_p120b)) + "处")
        if _dlgb:
            parts.append("对话拆: " + str(len(_dlgb)) + "处")
        if parts and target.is_file() and draft_path.is_file():
            _new_sha = _sha256_of(target)
            _new_md5 = hashlib.md5(target.read_bytes()).hexdigest()
            _write_quality_meta(draft_path, reviewed_hash=_new_sha, final_hash=_new_sha, target_body_md5=_new_md5)
        _finish("paragraph_recheck", True, "；".join(parts) if parts else "干净（零残留）")
    except runtime.AgentCancelled:
        _rollback_on_cancel()
        raise
    except Exception as exc:  # noqa: BLE001
        _advisory_fail("paragraph_recheck", f"二次复检异常: {exc}")
        _finish("paragraph_recheck", False, f"二次复检异常（继续）: {exc}")

    # 7. post_write（skip_enforce=True，enforce 已在 6.65 前移执行，防改写冻结后的正文）
    try:
        post = step_post_write(book, chapter_id, target, skip_enforce=True)
    except runtime.AgentCancelled:
        _rollback_on_cancel()
        raise
    except Exception as exc:  # noqa: BLE001
        # 同 ④：post_write 原本也是裸调用。但它的异常可能发生在 commit **之后**
        #   （nww_post_write.py 是 enforce → gate → commit → cross_chapter 串跑），
        #   此时 state 已推进，回滚正文会把「已提交状态 + 旧正文」撕开 → 分两种情况：
        #   · state 未推进（commit 没发生）→ 事务回滚，不留半恢复态；
        #   · state 已推进 → 落 RECOVERY_PENDING 台账，交恢复闭环，绝不回滚正文。
        if _state_unchanged():
            _rb = _rollback_transaction()
            _finish("post_write", False, f"post_write 异常（事务回滚）: {exc}" + (f"；{_rb}" if _rb else ""))
        else:
            _write_pending_commit_ledger(
                book, chapter_id, f"post_write 异常且状态已推进（不可回滚）:\n{exc}")
            _finish("post_write", False,
                    f"post_write 异常（state 已推进，落 RECOVERY_PENDING 台账，未回滚正文）: {exc}")
        return _fail()
    # P3（2026-09-22）：paragraph_gate 墙段 BLOCK → 确定性拆段（零文字损失）→ 重跑一次。
    #    同 write 链 commit BLOCK 的「确定性修复+retry once」先例（:2835 模式）；
    #    gate 在重跑里做最终仲裁，仍 BLOCK 则维持 fail（fail-closed）。
    if not post["ok"] and ("J2_long_chain" in post["stdout"] or "J1_single_overlong" in post["stdout"]):
        _wall_fixes = _fix_paragraph_walls(target)
        if _wall_fixes:
            if target.is_file() and draft_path.is_file():
                _new_sha = _sha256_of(target)
                _new_md5 = hashlib.md5(target.read_bytes()).hexdigest()
                _write_quality_meta(draft_path, reviewed_hash=_new_sha, final_hash=_new_sha, target_body_md5=_new_md5)
            _finish("paragraph_fix", True, "拆段: " + "；".join(_wall_fixes))
            post = step_post_write(book, chapter_id, target, skip_enforce=True)
    # 同时消费 stdout + stderr：paragraph_gate BLOCK 详情走 stdout，commit/enforce 走 stderr
    _finish("post_write", post["ok"],
            (post["stdout"] + ("\n" + post["stderr"] if post["stderr"] else ""))[:2400],
            returncode=post["returncode"], mode="revision")
    if not post["ok"]:
        # P2（外脑裁定）：commit BLOCK → 落恢复台账（重写链同样可能 commit 失败悬空）
        _write_pending_commit_ledger(
            book, chapter_id,
            f"post_write BLOCK (rewrite):\n{post['stdout']}\n{post['stderr']}")
        return _fail()

    # P2：commit 成功 → 该章恢复台账失效即删（真正的清除点已下沉到 step_post_write，
    #     见 2026-10-03 外脑裁定「台账生命周期必须绑定到 commit 成功事实本身」；此处保留为冗余兜底）
    _clear_pending_commit_ledger(book, chapter_id)
    # C-FUSE@1 成功路径：登记当前 outline_hash 作为下一轮熔断前提基线。
    # 失败静默（只打印），绝不影响已提交结果。
    _rewrite_fuse_sync(book, chapter_id)
    return {"ok": True, "chapter_id": chapter_id, "mode": "revision", "level": level,
            "commit": "success",
            # ★ 语义边界（2026-10-03 外脑裁定）：ok=True 表示「事务最终成功提交」，
            #   **不**表示「所有质量检查均通过」。advisory_failures 非空 = 存在降级项。
            "advisory_failures": advisory,
            "stages": [s.to_dict() for s in stages]}


# ---------------------------------------------------------------------------
# 润色链（nww-deslop 的 Web 内联落地，2026-09-16 外脑裁定 + 用户拍板）：
#   chat <execute> action=deslop 一键派单 → 备份 → LLM 去AI味保守润色 →
#   归一落盘 → delta(revision) → post_write commit → 机械层复扫(advisory 反馈残留)。
#   与重写链差异：不跑 review_cycle（避免审稿修订把润色清空不净重写一遍）；
#   不走 wordcount compress（润色不改体量）。
# ---------------------------------------------------------------------------
_DESLOP_INLINE_SYSTEM = (
    "你是去AI味润色执行师。你的任务是**逐段检查并修改**命中门禁清单的句子，不是原样复制。\n"
    "【铁律】禁止原样返回原文——至少修改 5 处以上。如果某段真的没问题，跳过即可；但全章零修改视为失败。\n"
    "【保真边界】不改剧情、不改人物、不改事件顺序、不改数值；只改表达。\n"
    "每处修改必须让句子更顺口、更有画面感、更少AI味；整体增大于删；整章净删改 ≤15%。\n"
    "【门禁清单（逐条过，命中就改，不许放过）】\n"
    "1. 贅字贅词：删掉堆叠修饰、连续语气词、平白冗余句；\n"
    "2. 口癖句式：“不是…而是…”、“仿佛/宛如/好像”每出现一次就改成直接动作描写；\n"
    "3. 长句拆短：超过 25 字的连续叙述句，拆成长短交替；\n"
    "4. 镜头感：把“他感到/他觉得”这类转述，改成五官动作和具体画面；\n"
    "5. 修辞降频：比喻/排比/对仗只保留最有冲击力的一处，其余回到具体动作；\n"
    "6. 代词清晰：每个“他/这/那”必须能明确指向谁，模糊就改成名字或动作。\n"
    "【输出】正文放在 <prose> 与 </prose> 标签之间，自 `# 第N章　标题` 起输出完整成稿；"
    "标签前后不写任何说明。"
)


def step_polish(book: Path, chapter_id: int, prose_text: str, on_token=None, contract: dict | None = None,
                user_note: str = "") -> str:
    """去AI味 LLM 润色（nww-deslop 内联衔接步骤）：保真优先、删改 ≤15%、增大于删。

    2026-11-01 合同感知：contract 非空（polish 链传入）时，把锁定实体/数值/关系端点作为
    「逐字保留禁改清单」注入 prompt——防止去AI味把作者原稿的锁定内容（如时间垫词「两息/一瞬间」、
    复合实体「病号灰袍人」）当 AI 味剔掉/合并，那是 contract_guard 反复 BLOCK 的根因
    （deslop 此前盲跑、不接收合同）。纯 deslop 链（run_deslop_pipeline）不传 contract，保持通用行为。
    2026-11-02 user_note：作者本轮编辑指令（最高优先级），与合同锁冲突时以锁为准。
    """
    _keep = ""
    if contract:
        _ents = [e for e in (contract.get("locked_entities") or []) if e]
        _nums = [n for n in (contract.get("locked_numerics") or []) if isinstance(n, dict) and n.get("surface")]
        _rel_ends: set[str] = set()
        for _r in (contract.get("locked_relations") or []):
            for _part in str(_r).split("-"):
                if _part:
                    _rel_ends.add(_part)
        _items: list[str] = []
        for _e in _ents:
            _items.append(f"实体「{_e}」须原样出现、不得改名/合并/删除")
        for _n in _nums:
            _s = _n["surface"]; _ab = _n.get("anchor_before") or ""; _aa = _n.get("anchor_after") or ""
            _items.append(f"数值「{_ab}{_s}{_aa}」须整段逐字保留（含左右邻句，不得改写挪位）")
        for _r in sorted(_rel_ends):
            if _r not in set(_ents):
                _items.append(f"关系端点「{_r}」须原样出现")
        if _items:
            _keep = (
                "\n\n【合同锁定·逐字保留禁改】以下均来自本章合同锁（作者原稿事实），去AI味时"
                "**必须逐字保留、严禁改写/删除/合并/同义替换/挪动位置**（违反即视为破坏合同）：\n- "
                + "\n- ".join(_items)
            )
    prompt = (
        f"# 第{chapter_id}章润色任务\n"
        f"对下面 <original> 中的正文执行去AI味保守润色，把完成稿放在 <prose> 标签内。\n"
        f"<original>\n{prose_text}\n</original>{_keep}"
    )
    if str(user_note or "").strip():
        prompt += (
            "\n\n<author_directive（作者本轮编辑指令 · 最高优先级）>\n"
            f"{str(user_note).strip()}\n"
            "以上指令必须逐条落实、可见生效。唯一例外：与合同锁定清单冲突处（上方"
            "「逐字保留禁改」项），以合同锁为准；其余内容在锁约束内最大化执行指令。\n"
            "</author_directive>"
        )
    # 2026-09-22 去输入截断（[:8000] 会让超长章后段不被清理）+ 输出动态上限（同 rewrite_pass 口径）
    # 2026-11-01 temperature 0.3→0.5：0.3 太保守导致模型原样返回原文零修改
    # 2026-11-02 路由再修正：deslop 回归 narrative-writer（跟随全局默认）。v2/Hemmingway
    #   专属路由被 08:50 实测 507 证伪：Huihui（主链）驻留 oMLX 31.55GB + Hemmingway 28.36GB
    #   = 59.91GB > 动态上限 50.90GB，双模型争内存恒不可装；且显式键令 deslop **脱离用户
    #   全局默认**（用户三次换模型均对 deslop 无效）。v2 当初只为绕开宕机 strata 回落，
    #   该前提已消失（默认现由模型中心控制）。单模型链 = 全链跟用户默认走。
    text, _ = _writer_stream(_DESLOP_INLINE_SYSTEM, prompt, on_token=on_token, temperature=0.5,
                             max_tokens=max(8192, int(len(prose_text) * 2.5)),
                             book=book, chapter_id=chapter_id, agent_name="narrative-writer")
    return text


def run_deslop_pipeline(book: Path, chapter_id: int, progress=None, on_writer_token=None, dry_run: bool = False) -> dict:
    """润色已提交章节（revision 闭环）：旧稿备份 → LLM 去AI味润色 → 归一落盘 → revision 提交 → 复扫。

    约定：只作用于「已有正文的已提交章」；正文不存在 → FAIL。
    progress: 可选回调 (name, ok, detail) —— 每环节推进时调用（异步任务进度上报用）。
    on_writer_token: 可选回调 (chunk) —— 润色流式输出逐 token 回调（异步任务实时预览用）。
    """
    book = book.resolve()
    stages: list[StageResult] = []
    _fatal_stage: dict = {"name": None}  # P0-1（2026-10-30 外脑定稿）：记录最近一次失败环节，供 _fail 真实归因

    def _finish(name: str, ok: bool, detail: str, output: str = "", **extra) -> None:
        stages.append(StageResult(name, ok, detail, **{**_gate_degraded_extra(), **extra}))
        if not ok:
            _fatal_stage["name"] = name
        if progress:
            try:
                progress(name, ok, detail, output=output)  # type: ignore[arg-type]
            except runtime.AgentCancelled:
                raise
            except Exception:  # noqa: BLE001 — 进度上报失败不影响主链
                pass

    def _begin(name: str, detail: str = "开始执行…") -> None:
        """环节开始时通知前端：progress(name, None, detail)。
        ok=None 表示进行中，_on_phase 据此更新 phase 但不追加 live_stage。"""
        if progress:
            try:
                progress(name, None, detail)  # type: ignore[arg-type]
            except runtime.AgentCancelled:
                raise
            except Exception:  # noqa: BLE001
                pass

    # advisory 失败登记（rescan 等）：语义上继续成功，但在最终结果里必须可观测。
    advisory: list[dict] = []

    def _fail(fatal_phase: str | None = None) -> dict:
        out = {"ok": False, "chapter_id": chapter_id, "mode": "deslop",
               "advisory_failures": advisory,
               "stages": [s.to_dict() for s in stages]}
        _fp = fatal_phase or _fatal_stage.get("name")  # P0-1：显式传参优先；缺省取最近一次失败环节
        if _fp:
            out["fatal_phase"] = _fp
            out["fatal_phase_confirmed"] = True
        return _settle_fail(book, chapter_id, stages, out, dry_run=dry_run)

    # ★ 事务边界（同重写链，2026-10-03 外脑裁定；2026-10-19 同款移植 deslop 链）：
    #   rollback 的定义 =「把本次 deslop 产生的全部持久化副作用恢复到 deslop 开始之前」。
    #   管理对象：original body / new body / delta / ledger / state_revision /
    #   last_committed_chapter。commit 是唯一推进 state 的动作，故 rollback 只发生在
    #   commit 之前 → state 天然不变；仍保留只读断言 _state_unchanged() 防未来偷偷改状态。
    body_written = {"done": False}
    _tx_delta = book / _TRACKING_DIR / f"{_DELTA_PREFIX}{chapter_id:03d}章.json"
    _tx_ledger = book / _TRACKING_DIR / "pending_commits" / f"第{chapter_id:03d}章.json"
    _tx_snap: dict[str, bytes | None] = {"delta": None, "ledger": None}
    _tx_state_before: dict[str, object] = {"revision": None, "last": None}

    def _rollback_body() -> str:
        """回滚 normalize 落盘的新稿到执行前正文；未落盘（标志 False）不动作。
        返回回滚结果描述（空串=无需回滚）。只做一次（回滚后复位标志）。"""
        if not body_written.get("done"):
            return ""
        try:
            target.write_text(original, encoding="utf-8")
        except Exception:  # noqa: BLE001
            return "回滚失败，请人工从 .backup/ 恢复"
        body_written["done"] = False
        return "已回滚执行前正文"

    def _state_unchanged() -> bool:
        """只读断言：state_revision / last_committed_chapter 是否仍等于 deslop 开始前。"""
        try:
            _s = json.loads((book / _TRACKING_DIR / "_tracking-state.json").read_text(encoding="utf-8"))
            return (_s.get("state_revision") == _tx_state_before.get("revision")
                    and _s.get("last_committed_chapter") == _tx_state_before.get("last"))
        except Exception:  # noqa: BLE001 — 读不到视为「无法证明未变」，保守报 False
            return False

    def _rollback_transaction() -> str:
        """本次 deslop 的全部持久化副作用回滚：正文 + delta + 台账。
        幂等：deslop 前不存在的文件删除；deslop 前存在的文件按字节快照复原。
        返回回滚结果描述（空串=无需回滚）。"""
        parts: list[str] = []
        _rb = _rollback_body()
        if _rb:
            parts.append(_rb)
        for _k, _p in (("delta", _tx_delta), ("ledger", _tx_ledger)):
            try:
                _before = _tx_snap.get(_k)
                if _before is None:
                    if _p.is_file():
                        _p.unlink()
                        parts.append(f"已删除本次生成的{_k}")
                elif (not _p.is_file()) or _p.read_bytes() != _before:
                    _p.write_bytes(_before)
                    parts.append(f"已恢复 deslop 前的{_k}")
            except Exception:  # noqa: BLE001 — 单项失败不阻断其余，但必须在 detail 里可见
                parts.append(f"{_k}回滚失败，请人工处理")
        if not _state_unchanged():
            parts.append("⚠ state_revision/last_committed 与 deslop 前不一致（未回滚，需人工核对）")
        return "；".join(parts)

    def _rollback_on_cancel() -> None:
        """AgentCancelled 出口专用：回滚后仅入 stages（不经 progress 回调，
        避免取消路径再次触发回调异常）。语义=事务回滚（正文 + delta + 台账）。"""
        rb = _rollback_transaction()
        if rb:
            stages.append(StageResult("rollback_on_cancel", True, rb))

    def _advisory_fail(name: str, detail: str) -> None:
        """advisory 失败登记：不改变控制流，只在最终结果里沉淀证据。
        结果 ok=True 只表示「事务最终成功提交」，advisory_failures 非空=存在降级项。"""
        advisory.append({"stage": name, "detail": str(detail or "")[:400]})

    try:
        # 0pre. 写前连续性闸（同重写链）：目标章前存在更小悬空章 → 先处理，否则 delta 会错判 revision
        _last = _last_committed_chapter(book)
        if _last is not None:
            _pending = _pending_uncommitted_chapter(book, chapter_id, _last)
            if _pending is not None:
                _dangling = _dangling_block_detail(_pending, _last, auto_detail="",
                                                   next_action="润色后续章节")
                if _gate().is_v13():  # type: ignore[attr-defined]
                    _gate_record(book, chapter_id, "0pre/dangling_chapter",
                                 _dangling, "pipeline/polish")
                    _finish("backup", True,
                            f"[V13 道闸] 悬空事实已记录，动作继续：{_dangling[:200]}")
                else:
                    _finish("backup", False, _dangling)
                    return _fail()

        target = _resolve_existing_body(book, chapter_id)
        if target is None:
            _finish("backup", False, f"第{chapter_id}章无已提交正文（或正文已被备份改名）。无法润色不存在的章节。")
            return _fail()

        # ★ 事务快照（同重写链）：记录 deslop 开始前的 delta / 台账字节与状态版本，
        #   供 _rollback_transaction 精确复原（deslop 前不存在的删除，存在的按字节复原）。
        for _k, _p in (("delta", _tx_delta), ("ledger", _tx_ledger)):
            try:
                _tx_snap[_k] = _p.read_bytes() if _p.is_file() else None
            except Exception:  # noqa: BLE001 — 快照失败按「deslop 前不存在」处理（回滚即删除）
                _tx_snap[_k] = None
        try:
            _s = json.loads((book / _TRACKING_DIR / "_tracking-state.json").read_text(encoding="utf-8"))
            _tx_state_before["revision"] = _s.get("state_revision")
            _tx_state_before["last"] = _s.get("last_committed_chapter")
        except Exception:  # noqa: BLE001 — 读不到则断言恒 False，回滚 detail 会显式告警
            pass

        # 1. 写前备份（幂等：.backup/{章名}.orig.md 首份优先，已有不覆盖）；dry_run 不写正式 .backup/
        original = target.read_text(encoding="utf-8")
        if dry_run:
            _rid = f"deslop_{uuid.uuid4().hex[:12]}"
            _finish("backup", True, "dry-run：跳过正式 .backup 写入")
        else:
            bk = toolbox.run_script("nww_backup.py", ["--book", str(book), "--file", str(target)], book, timeout=60)
            if not bk["ok"]:
                _finish("backup", False, f"原稿备份失败: {bk['stderr'] or bk['stdout']}")
                return _fail()
            _finish("backup", True, bk["stdout"][:300], path=bk["stdout"].strip())
    except runtime.AgentCancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        _finish("backup", False, f"原稿备份失败: {exc}")
        return _fail()

    try:
        # 2. LLM 去AI味保守润色（输出 <prose> 成稿；流式 token 回调给前端预览）
        polished = step_polish(book, chapter_id, original, on_token=on_writer_token)
        delta_len = len(polished) - len(original)
        _finish("polish", True, f"润色完成：{len(original)} → {len(polished)} 字（净{'增' if delta_len >= 0 else '减'} {abs(delta_len)}）")
    except runtime.AgentCancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        _finish("polish", False, f"去AI味润色失败: {exc}")
        return _fail()

    try:
        # 3. 正文归一（<prose> 提取/元信息清洗/标题回填/跨章去重）→ dry_run 写 sandbox / 正式落盘
        clean, norm_report = step_normalize(book, chapter_id, polished)
        if dry_run:
            sb = _dry_run_sandbox(book, _rid)
            target = sb / "body-preview" / f"第{chapter_id:03d}章.md"
        rel = target.relative_to(book).as_posix()
        target.write_text(clean + "\n", encoding="utf-8")
        if not dry_run:
            body_written["done"] = True
        # 2026-11-01 润色效果校验：normalize 后和原文逐行比对，差异 <3% 说明模型原样返回
        _orig_lines = [l.strip() for l in original.splitlines() if l.strip()]
        _clean_lines = [l.strip() for l in clean.splitlines() if l.strip()]
        _common = set(_orig_lines) & set(_clean_lines)
        _unchanged_ratio = len(_common) / max(len(_orig_lines), 1)
        if _unchanged_ratio > 0.97:
            _rb = "" if dry_run else _rollback_transaction()
            _finish("polish_effect_check", False,
                    f"润色效果为零：{len(_clean_lines)} 行中有 {len(_common)} 行与原文完全相同"
                    f"（{_unchanged_ratio:.0%}）。模型未做任何实际修改。\n"
                    f"原因：本地模型能力不足或提示词太保守。\n"
                    f"解决：1) 切换到云端模型重试；2) 手动调整需要润色的段落。"
                    + (f"；{_rb}" if _rb else ""))
            return _fail()
        _finish("polish_effect_check", True,
                f"润色效果校验通过：{len(_clean_lines)} 行中修改了 {len(_clean_lines) - len(_common)} 行"
                f"（{1 - _unchanged_ratio:.0%} 行有变化）")
        # ★长度塌缩复检（2026-11-01 实锤，见 _collapse_after_normalize）：以**落盘正文**为准。
        #   00:18 真实 deslop：polish 2124→1304（61% 压线过）→ normalize → 932 字（44%），
        #   全程无复检；若 delta_semantic 没恰好失败，932 字残稿会被 commit。
        #   阈值 0.8（用户 2026-11-01 拍板）：对齐「删改 ≤15%」契约，61.4% 那一刀也拦得住。
        _collapse = _collapse_after_normalize(original, target)
        if _collapse:
            _rb = "" if dry_run else _rollback_transaction()
            _finish("normalize", False,
                    "长度塌缩 BLOCK：" + "；".join(_collapse)
                    + (f"；{_rb}" if _rb else "")
                    + ("（dry-run 预览，正式目录未变）" if dry_run else ""))
            return _fail()
        # 模式C（2026-10-25）：修存量链 advisory 闸——润色是全章表达重写，可能引入
        # 代词翻转/数字漂移；只报告不阻断（阻断会把存量缺陷章锁死无法润色修复）。
        _adv = _advisory_gates_report(book, chapter_id, clean)
        _finish("normalize", True,
                f"{'dry-run 正文预览' if dry_run else '覆盖落盘'}: {rel}（{len(clean)} 字；{norm_report}）"
                + ("；⚠ advisory 闸：" + "；".join(_adv) if _adv else ""),
                path=rel, chars=len(clean), advisory_gates=_adv)
    except runtime.AgentCancelled:
        _rollback_on_cancel()
        raise
    except Exception as exc:  # noqa: BLE001
        _rb = _rollback_transaction()
        _finish("normalize", False, f"正文归一/落盘失败（事务回滚）: {exc}" + (f"；{_rb}" if _rb else ""))
        return _fail()

    try:
        # 4. delta 草稿（对已提交章自动 revision 模式）+ 语义补全（post_write commit 消费）
        draft_path = step_delta_draft(book, chapter_id,
                                      sandbox_dir=(sb / "delta-preview") if dry_run else None,
                                      preserve_quality_meta=not dry_run)
        _finish("delta_draft", True, f"草稿: {draft_path.relative_to(book).as_posix()}")
        step_delta_semantic(book, chapter_id, draft_path, target)
        _finish("delta_semantic", True, "语义字段已补全")
    except runtime.AgentCancelled:
        _rollback_on_cancel()
        raise
    except Exception as exc:  # noqa: BLE001
        # 同重写链 ①②：delta 可能已部分落盘，只回滚正文会留「半恢复态」→ 事务回滚。
        _rb = _rollback_transaction()
        _finish("delta_semantic", False, f"delta 处理失败（事务回滚）: {exc}" + (f"；{_rb}" if _rb else ""))
        return _fail()

    # 5. post_write（BLOCK 点，revision 提交）
    if dry_run:
        # P3（外脑 2026-10-07 裁定）：delta 已由 step_delta_draft(sandbox_dir=...) 直写 sandbox，
        # 不得再从正式 追踪/ move（会搬走/误动正式 delta）。
        _finish("dry_run", True, "deslop dry-run：正文/delta 已入 sandbox，正式目录零变化、未 commit")
        return {"ok": True, "chapter_id": chapter_id, "mode": "deslop", "dry_run": True,
                "execution_id": _rid, "commit_status": "SKIPPED_DRY_RUN",
                "sandbox": sb.relative_to(book).as_posix(),
                "stages": [s_.to_dict() for s_ in stages]}

    # 4.5 一致性检测 + 清零自动闭环（2026-11-04）：
    #   润色链此前无一致性环节，直接 post_write → A-3 缺闭环记录硬 BLOCK（fail_closed=true）。
    #   润色只改表达、不改剧情/人物/事件/数值，理论上不引入新 S1/S2，但「零检测直接提交」
    #   会撞 A-3。此处补只读检测（不改正文），清零则自动落闭环；检出硬伤 → 自动闭环拒绝
    #   落盘 → A-3 兜底 BLOCK（守住「不装假闭环」）。非 dry_run（正式 commit 路径）才跑。
    try:
        _cons = step_consistency_check(book, chapter_id, target)
        if _cons.get("ok"):
            _closure = step_consistency_closure(book, chapter_id, _cons)
            if _closure.get("closed"):
                _finish("consistency", True,
                        f"审查记录: {_cons.get('record_path', '')}"
                        f"（S1/S2={_cons.get('open_s1_s2_count', 0)}，S3/S4={_cons.get('s3_s4_count', 0)}）"
                        f"；自动闭环已落盘（{_closure.get('closure_path', '')}）",
                        consistency=_cons, closure=_closure)
            else:
                _finish("consistency", False,
                        f"一致性未闭环（S1/S2={_cons.get('open_s1_s2_count', 0)}，未落闭环，A-3 将 BLOCK）",
                        consistency=_cons, closure=_closure)
        else:
            _finish("consistency", False, f"一致性检测未完成（不落闭环）: {_cons.get('detail', '')}")
    except runtime.AgentCancelled:
        _rollback_on_cancel()
        raise
    except Exception as exc:  # noqa: BLE001 — 检测异常不阻断润色主链；未闭环时 A-3 兜底
        _advisory_fail("consistency", f"一致性检测异常: {exc}")
        _finish("consistency", False, f"一致性检测异常（继续，A-3 兜底）: {exc}")

    try:
        post = step_post_write(book, chapter_id, target)
    except runtime.AgentCancelled:
        _rollback_on_cancel()
        raise
    except Exception as exc:  # noqa: BLE001
        # 同重写链：post_write 异常可能发生在 commit **之后**（enforce → gate → commit →
        #   cross_chapter 串跑）。state 未推进 → 事务回滚；已推进 → 落台账，绝不回滚正文。
        if _state_unchanged():
            _rb = _rollback_transaction()
            _finish("post_write", False, f"post_write 异常（事务回滚）: {exc}" + (f"；{_rb}" if _rb else ""))
        else:
            _write_pending_commit_ledger(
                book, chapter_id, f"post_write 异常且状态已推进（不可回滚）:\n{exc}")
            _finish("post_write", False,
                    f"post_write 异常（state 已推进，落 RECOVERY_PENDING 台账，未回滚正文）: {exc}")
        return _fail()
    _finish("post_write", post["ok"],
            (post["stdout"] + ("\n" + post["stderr"] if post["stderr"] else ""))[:2400],
            returncode=post["returncode"], mode="revision")
    if not post["ok"]:
        # P2 + 2026-11-01 修复：commit BLOCK 时 state 未推进 → 必须事务回滚（同 revise 链）。
        #   此前无条件只落恢复台账、不回滚正文：BLOCK 后盘上留着「已改未提交」的正文，
        #   下一轮会被 0pre 写前连续性闸判成「存在未提交章节」而卡死（实测：润色链 post_write
        #   BLOCK 后正文 md5 已变、commit 未发生）。已推进（commit 成功但后置环节失败）→
        #   绝不回滚正文，落台账交恢复闭环。
        if _state_unchanged():
            _rb = _rollback_transaction()
            stages.append(StageResult("rollback", True, _rb or "无需回滚"))
            return _fail()
        _write_pending_commit_ledger(
            book, chapter_id,
            f"post_write BLOCK (deslop):\n{post['stdout']}\n{post['stderr']}")
        return _fail()

    # commit 已成功。★必须在此重绑：post_write 内 Step1 的 nww_enforce.py 会**再改一次正文**
    #   （引号风格/markdown 剥离/结尾泄漏清理），任何在 post_write 之前算的哈希都描述不了
    #   交付版本。重绑后 delta 的 final_hash 恒等于盘上这一版 → 审稿留痕保留且不再说谎。
    _rebind_body_quality_meta(draft_path, target)

    # 6. 机械层复扫（advisory：复扫归零核对，残留只报不阻断，供前端展示）
    try:
        reports = step_deslop(book, chapter_id, target, check_only=True)
        _finish("rescan", True, f"复扫脚本数: {len(reports)}", reports=reports)
    except runtime.AgentCancelled:
        raise  # commit 已成功，取消不回滚（同重写链 commit 后语义）
    except Exception as exc:  # noqa: BLE001
        # commit 已成功的后置复扫异常：降级 advisory，不影响已提交结果。
        _advisory_fail("rescan", f"复扫异常: {exc}")
        _finish("rescan", False, f"复扫异常（advisory，不影响已提交结果）: {exc}")

    # P2：commit 成功 → 该章恢复台账失效即删
    _clear_pending_commit_ledger(book, chapter_id)
    return {"ok": True, "chapter_id": chapter_id, "mode": "deslop",
            # ★ 语义边界（同重写链）：ok=True 表示「事务最终成功提交」，
            #   **不**表示「所有质量检查均通过」。advisory_failures 非空 = 存在降级项。
            "advisory_failures": advisory,
            "stages": [s.to_dict() for s in stages]}


def run_deslop_text(text: str, book: Path | None = None, chapter_id: int = 1) -> dict:
    """文本模式去AI味润色（无文件、不落盘、不 commit）：LLM 保真保守润色 → prose 提取 + 清洗。

    用途：聊天贴文场景。调度台（agent.py）把用户贴入的正文派发给本函数，由引擎改写后返回改后全文，
    绝不返回「原始未改」的空跑。调度台只负责派单，改写动作永远发生在引擎内（执行=调度，非自己写自己改）。

    book 仅用于模型路由上下文（只读，narrative-writer 池模型选择）；缺省 Path(".") 走全局默认路由。
    不读取、不写入任何章节文件。返回 polished 为已清洗的改后全文。
    """
    if not text or not text.strip():
        return {"ok": False, "mode": "deslop_text", "error": "空文本"}
    _book = book.resolve() if book else Path(".")
    polished = step_polish(_book, chapter_id, text)
    if not polished or not polished.strip():
        return {"ok": False, "mode": "deslop_text", "error": "LLM 润色返回空"}
    clean = _sanitize_writer_output(_extract_prose(polished))
    if not clean.strip():
        # prose 提取失败（模型未守 <prose> 协议）：回退整段清洗，避免空跑吞掉结果
        clean = _sanitize_writer_output(polished)
    return {
        "ok": True, "mode": "deslop_text",
        "chars_before": len(text), "chars_after": len(clean),
        "polished": clean,
    }


# ---------------------------------------------------------------------------
# 局部修订链（nww-revision 只增不删算子 的 Web 落地，2026-09-16；
# 2026-09-18 重构为「锚点插入协议」，根因：ch36 实测模型把 4733 字原文逐字
# 抄回当成交差（echo 空转），全文回写协议对保守模型没有强制执行力）：
#   chat <execute> action=revise（须带 note 指定修订点）→ 备份 → LLM 输出插入指令
#   JSON [{anchor, position, content}] → 确定性锚点执行器插入（只增不删由构造保证，
#   模型没有改写/删除原文的通道）→ diff 兜底校验（removed 必须=0）→ 落盘 →
#   delta(revision) → post_write commit → 机械层复扫。
#   与 rewrite 差异：不改写已有句子、未新增情节，只在 note 指定处插入。
# ---------------------------------------------------------------------------
# ★协议上限常量**前置**定义——下面 _REVISE_ANCHOR_SYSTEM 直接引用它们（f-string），
# 保证「prompt 告诉模型的数」与「代码实际校验的数」是同一个值，不会两处漂移。
# （2026-09-27 修：原先 prompt 写 anchor「≥10 字」而代码 _REVISE_ANCHOR_MIN=6，
#   两处独立硬编码，改一处就静默不一致。）
_REVISE_OPS_MAX = 3           # 单章增补上限（对齐原算子语义）
_REVISE_CONTENT_MAX = 150     # 每处插入内容字数上限（≈2 句；prompt 用「≤2 句」表述）
_REVISE_ANCHOR_MIN = 6        # 锚句最小长度（唯一命中才是硬约束，下限只防过短锚/纯标点碎片）
# ★2026-10-07：10→6（外脑裁定 A）。第002章实跑：模型摘的锚「满街都是穿道袍的。」(9字)、
#   「门板轰然洞开。」(7字) 都是原文逐字存在且唯一命中(count=1)的合法锚，却被 10 字下限在
#   _parse_revise_ops 整单拒绝、两次尝试全败。长度只是启发式代理，「唯一命中」才是定位正确性
#   的不变量；6 覆盖实跑故障(7/9字)，同时保留极短碎片 sanity 下限。

_REVISE_ANCHOR_SYSTEM = (
    "你是定点修订执行师，执行写作引擎 nww-revision 局部修订算子（锚点插入协议）。\n"
    "【工作方式】\n"
    f"你不输出全文。只输出一个 JSON 数组，描述要在原文中插入的新内容（0-{_REVISE_OPS_MAX} 处）：\n"
    '[{"anchor": "原文中逐字存在的一句话", "position": "after", "content": "要插入的新内容"}]\n'
    "【硬约束（最高优先级）】\n"
    f"1. anchor 必须从 <original> 中**逐字摘录**（≥{_REVISE_ANCHOR_MIN} 字，含原标点，不得改写）；"
    "若该句在原文出现多次，必须摘录更长的上下文使锚点唯一。\n"
    "2. position 只能是 \"after\"（紧贴锚句之后插入）或 \"before\"（紧贴锚句之前插入）。\n"
    "3. content 是原文没有的新内容，用于落实 note 的修订要求：每处 ≤2 句，"
    "用原有人设口吻，补前因后果/动作细节/情绪/对话节奏；禁止引入原文没有的"
    "新情节、新人物、新设定；禁止重复原文已有信息。\n"
    "4. 你**没有**改写、删减、重排原文的通道——协议只执行插入，不要试图输出完整正文。\n"
    "5. 只输出 JSON 数组本身，前后不写任何说明、思考或 markdown 围栏。\n"
    # ★6 = no-op 出口（2026-09-27 加）：note 要求原文已满足时，「必须产出」与
    # 「不得重复」两条硬约束逻辑上不可同时满足，模型只能被逼着违反后者。
    # 给它一个合法退出口，它才不必靠换措辞复述来凑数。
    "6. ★**无需修订是合法结论**：若 note 的修订要求已由 <original> 中某句满足"
    "（意思相同即可，措辞不必逐字相同），输出空数组 `[]` 表达「无需修订」。"
    "**严禁**为了凑够插入数量而换措辞复述原文已有的意思——那会被判重拦截、整单失败。\n"
    "【自检（输出前必做）】"
    "(1) note 要求的那个意思，<original> 里是不是已经有了？已有 → 直接输出 `[]`。"
    "(2) 每条 anchor 在 <original> 里能逐字找到吗？"
    "(3) 每条 content 真的是原文没有的新句子吗？"
    "三项有一项答否，就修正后再输出。"
)


def _revise_retry_note(note: str, last_err: str) -> str:
    """第二次尝试的 note：原要求之外，回传「上一次为什么没过」+ **可执行的规避指引**。

    2026-09-27 修：原先只回传一句 last_err，信息量太低——模型既不知道自己撞的是
    原文哪一句，也不知道「无需修订」是合法出口，只能盲改措辞再撞一次，导致两次
    失败高度同质（用户实跑第25章所见）。这里按错误类型补针对性指引。
    """
    tail = f"\n\n【上一次未通过，原因】{last_err}\n"
    # ★2026-10-07 修（外脑裁定 D）：_parse_revise_ops 的「过短锚」报错用的是英文 anchor
    #   （「第N处 anchor 不足 N 字…」），而下面两个分支只认中文「重复」「锚点」→ 永不命中，
    #   该失败类此前是**盲重试**（只回传原错误一行，模型反而摘得更短）。此分支对症补指引。
    if "anchor" in last_err and "不足" in last_err:
        tail += (
            f"★锚点过短：你摘的 anchor 少于 {_REVISE_ANCHOR_MIN} 字，被拒。"
            f"请从 <original> 中重新**逐字摘录**一段至少 {_REVISE_ANCHOR_MIN} 字"
            "（含原标点）的连续原句作为 anchor，并确保它在本章原文里**只出现一次**；"
            "不得改写、拼接或虚构 anchor。\n")
    elif "重复" in last_err:
        tail += (
            "★这说明你写的那个意思原文里已经有了。请回头确认：note 要求的这个意思，"
            "<original> 里是不是本来就写着？\n"
            "· 是 → 直接输出空数组 `[]` 表达「无需修订」。这是合法的，也是正确的做法，"
            "不要为了凑数量换措辞复述。\n"
            "· 不是 → 换一个原文确实没有的角度来写，不要只是换个说法复述同一件事。\n")
    elif "锚点" in last_err:
        tail += (
            "★锚点问题：请从 <original> 重新逐字摘录一句**更长**的上下文，"
            "确保它在原文里只出现一次（同一句话可能在正文不同段落重复出现）。\n")
    tail += "请严格按锚点插入协议重新输出。"
    return note + tail


def step_revise(book: Path, chapter_id: int, prose_text: str, note: str, on_token=None) -> str:
    """定点修订 LLM pass：锚点插入协议（模型只输出插入指令 JSON，不回写全文）。"""
    # ★2026-10-07：note 涉及「与上一章衔接/承接/开头/结尾」时，注入上一章结尾片段，
    #   否则模型看不到上一章末尾，只能凭本章孤立上下文盲写 → 用户实跑「开头与上一章
    #   结尾接不上」。锚点仍必须从 <original> 摘（协议不变，只补前情）。
    _prev_block = ""
    if any(k in note for k in ("衔接", "承接", "接续", "上一章", "上章", "上回", "结尾", "开头", "开篇")):
        _prev_full = _read_prev_chapter_full(book, chapter_id)
        if _prev_full:
            _prev_block = (
                "\n\n以下 <prev_ending> 是上一章正文的结尾片段，用于保证本次修订与上一章结尾"
                "在情境/时间/人物状态上自然衔接；**锚点仍必须从 <original> 中逐字摘录**，"
                "不得从 <prev_ending> 摘：\n"
                f"<prev_ending>\n{_prev_full[-400:]}\n</prev_ending>"
            )
    prompt = (
        f"# 第{chapter_id}章定点修订\n"
        f"修订要求 note：\n{note}\n\n"
        f"以下 <original> 是本章现有正文（完整），锚点必须从这里逐字摘录：\n"
        f"<original>\n{prose_text}\n</original>{_prev_block}"
    )
    text, _ = _writer_stream(_REVISE_ANCHOR_SYSTEM, prompt, on_token=on_token, temperature=0.3, book=book, chapter_id=chapter_id)
    return text


def _parse_revise_ops(raw: str) -> list[dict]:
    """解析模型输出的插入指令 JSON → 校验结构/上限 → 标准 ops 列表。

    容错：剥 markdown 围栏、取首个 [ 到末个 ] 之间的子串。任何结构非法 → ValueError
    （调用方带原因重试一次）。

    ★空数组 `[]` 是**合法结果**而非错误（协议第 6 条：无需修订）——调用方需据此
    走「原稿未改动」的正常结单分支，而不是当成失败去重试。
    """
    s = (raw or "").strip()
    i, j = s.find("["), s.rfind("]")
    if i < 0 or j <= i:
        raise ValueError("模型未输出 JSON 数组（协议要求只输出 [{anchor, position, content}]）")
    try:
        data = json.loads(s[i:j + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 解析失败：{exc}") from exc
    if not isinstance(data, list):
        raise ValueError("JSON 不是数组")
    if not data:
        # ★合法 no-op（2026-09-27）：模型判定 note 的要求已由原文某句满足，
        # 按协议第 6 条输出 [] 表达「无需修订」。这不是协议违规，不重试。
        return []
    if len(data) > _REVISE_OPS_MAX:
        raise ValueError(f"插入指令 {len(data)} 处，超过单章上限 {_REVISE_OPS_MAX}")
    ops: list[dict] = []
    for idx, item in enumerate(data, 1):
        if not isinstance(item, dict):
            raise ValueError(f"第{idx}处不是对象")
        anchor = str(item.get("anchor") or "").strip()
        content = str(item.get("content") or "").strip()
        pos = str(item.get("position") or "after").strip()
        if len(anchor) < _REVISE_ANCHOR_MIN:
            raise ValueError(f"第{idx}处 anchor 不足 {_REVISE_ANCHOR_MIN} 字（须逐字摘录原文长句）")
        if pos not in ("after", "before"):
            raise ValueError(f"第{idx}处 position 非法：{pos}（只能 after/before）")
        if not content:
            raise ValueError(f"第{idx}处 content 为空")
        if len(content) > _REVISE_CONTENT_MAX:
            raise ValueError(f"第{idx}处 content {len(content)} 字，超过每处上限 {_REVISE_CONTENT_MAX}")
        ops.append({"anchor": anchor, "position": pos, "content": content})
    return ops


def _apply_anchored_insertions(original: str, ops: list[dict]) -> tuple[str, list[str]]:
    """锚点插入执行器（确定性）：anchor 逐字定位（必须唯一命中），content 紧贴锚句插入。

    只增不删由构造保证——纯字符串拼接，没有任何删除原文字符的通道。
    锚点未命中/多命中 → ValueError（调用方带原因重试一次，仍败则整单失败不落盘）。
    """
    text = original
    reports: list[str] = []
    for idx, op in enumerate(ops, 1):
        anchor, content, pos = op["anchor"], op["content"], op["position"]
        hits = text.count(anchor)
        if hits == 0:
            raise ValueError(f"第{idx}处锚点未命中（不是原文逐字摘录？）：「{anchor[:30]}…」")
        if hits > 1:
            raise ValueError(f"第{idx}处锚点不唯一（命中 {hits} 次），需摘更长上下文：「{anchor[:30]}…」")
        at = text.find(anchor) + (len(anchor) if pos == "after" else 0)
        text = text[:at] + content + text[at:]
        reports.append(f"第{idx}处：锚句「{anchor[:16]}…」{pos} 插入 {len(content)} 字")
    return text, reports


# 逐句判重的最小长度（归一化后字数）。低于此长度的片段不参与判重 ——
# 见 _validate_inserted_content 内的根因说明（中文引号碎片导致 revise 必挂）。
# 与「上一章判重」历史口径一致（原本就写的 15），此处统一成常量避免两处漂移。
_REVISE_MIN_DUP_CHARS = 15

# ★2026-10-07（写作元语言过滤）：模型把「这是原视角描写，需被后续插入的主动开门动作覆盖」
#   当作 content 落盘（第002章实跑），原协议只查「与原文/上一章重复」，拦不住这类自述。
#   命中即整单拒绝（带原因重试，仍败则不落盘），保证正文不出现写作元语言。
_REVISE_META_MARKERS = (
    "原视角", "后续插入", "待插入", "需被", "本段", "本处", "写作说明", "元语言", "注：",
)


def _validate_inserted_content(book: Path, chapter_id: int, ops: list[dict], original: str) -> None:
    """插入内容合规校验（确定性）：

    P1-B：多 op 互斥——任一 op 的 content 不得包含其他 op 的 anchor（防止前 op 的 content
    文本成为后 op 的「虚假命中」，命中计数偏差导致错位插入）。
    P1-C：逐句判重——content 切句后每句都不得在原文出现、不得在上一章（≥15 字）出现。
    整段 norm 判断对「多句拼接 content」不可靠，改逐句。
    """
    orig_norm = _norm_sentence(original)
    prev_full = _read_prev_chapter_full(book, chapter_id)
    prev_set = {_norm_sentence(s) for s in _split_sentences(prev_full)} if prev_full else set()
    anchors = [op["anchor"] for op in ops]
    for idx, op in enumerate(ops, 1):
        c = op["content"]
        # P1-D（2026-10-07）：写作元语言过滤——模型自述/指令不得混入正文
        _meta = next((mk for mk in _REVISE_META_MARKERS if mk in c), None)
        if _meta:
            raise ValueError(
                f"第{idx}处插入内容含写作元语言「{_meta}」（如「这是原视角描写，需被…覆盖」），"
                f"属模型自述而非正文，被拒：「{c[:24]}…」")
        # P1-B：content 不得包含其他 op 的 anchor
        for j, other_anchor in enumerate(anchors):
            if j != idx - 1 and other_anchor in c:
                raise ValueError(
                    f"第{idx}处 content 包含第{j+1}处锚点文本（op 互斥违规，"
                    f"前 op 插入会破坏后 op 定位）：「{other_anchor[:24]}…」")
        # P1-C：逐句判重
        for s in _split_sentences(c):
            ns = _norm_sentence(s)
            if not ns:
                continue
            # ★长度门槛（2026-09-27 修）：短片段一律跳过，不参与判重。
            #   根因实测：_split_sentences 会把中文引号结尾的 `」` 单独切成 **1 字碎片**
            #   （content「这玩意儿…能吊命。」→ 切出 18 字句 + 1 字「」），
            #   而 `」`、`“` 这类字符在有对话的正文里**必然出现** → 子串判重必然命中
            #   → 报「插入句已在原文出现（重复）：「」…」」→ 两次尝试全失败，
            #   原稿其实一个字都没动。小说对话大量带引号 ⇒ revise 动作几乎必挂。
            #   真正该拦的是**整句级**重复（≥15 字），与「上一章判重」口径一致；
            #   短语级重复（"泡面"/"林小凡"）属正常行文，拦了就是误杀。
            if len(ns) < _REVISE_MIN_DUP_CHARS:
                continue
            if ns in orig_norm:
                raise ValueError(f"第{idx}处插入句已在原文出现（重复）：「{s[:24]}…」")
            if ns in prev_set:
                raise ValueError(f"第{idx}处插入句与上一章逐字重复：「{s[:24]}…」")


def _sanitize_paragraphs(text: str) -> str:
    """段落归一（P1-A 修复）：一段一行 + 段间空行（对齐 _sanitize_writer_output 第 5 步）。

    锚点插入协议下原文已是合法段落协议（旧链 normalize 过），插入内容若跨段会在锚点处
    产生「新 content 紧贴下一段开头」的贴段。此处按「一段一行」重建：保留所有非空行，
    行间统一空行分隔。只动空白，不增删内容（对齐旧链「段落归一只动空白」的承诺）。
    """
    lines = [ln.rstrip() for ln in text.splitlines()]
    kept: list[str] = []
    for ln in lines:
        if ln.strip():
            kept.append(ln)
    return re.sub(r"\n{3,}", "\n\n", "\n\n".join(kept)).strip()


def _diff_guard_only_additions(original: str, revised: str, threshold: float = 0.06) -> dict:
    """只增不删硬核对：对比原文与修订稿的字符级增删（difflib）。

    返回 {ok, detail, inserted, removed}：removed 超过原文阈值 → 违反“只增不删”；inserted == 0 → 空转。
    上限放宽到 6% 容差以容忍 normalize 的跨章去重与标题回填带来的少量差异。
    """
    sm = difflib.SequenceMatcher(None, original, revised)
    removed = 0
    inserted = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "delete":
            removed += len(original[i1:i2])
        elif tag == "insert":
            inserted += len(revised[j1:j2])
        elif tag == "replace":
            removed += len(original[i1:i2])
            inserted += len(revised[j1:j2])
    cap = max(1, int(len(original) * threshold))
    if removed > cap:
        return {"ok": False, "detail": f"违反只增不删：删除 {removed} 字（上限 {cap}）", "inserted": inserted, "removed": removed}
    if inserted == 0:
        return {"ok": False, "detail": "未检测到任何新增内容（空转）", "inserted": 0, "removed": removed}
    return {"ok": True, "detail": f"增 {inserted} 字 / 删 {removed} 字", "inserted": inserted, "removed": removed}


def run_revise_pipeline(book: Path, chapter_id: int, progress=None, on_writer_token=None,
                        note: str = "", dry_run: bool = False) -> dict:
    """局部修订已提交章节（nww-revision 只增不删算子，revision 闭环）。

    约定：只作用于「已有正文的已提交章」；正文不存在 → FAIL；note 必填（修订点/修订要求）。
    progress: 可选回调 (name, ok, detail)；on_writer_token: 可选回调 (chunk)（前端实时预览）。
    """
    book = book.resolve()
    note = (note or "").strip()
    stages: list[StageResult] = []
    _pre_dedup: list[str] = []
    _fatal_stage: dict = {"name": None}  # P0-1（2026-10-30 外脑定稿）：记录最近一次失败环节，供 _fail 真实归因

    def _finish(name: str, ok: bool, detail: str, output: str = "", **extra) -> None:
        stages.append(StageResult(name, ok, detail, **{**_gate_degraded_extra(), **extra}))
        if not ok:
            _fatal_stage["name"] = name
        if progress:
            try:
                progress(name, ok, detail, output=output)  # type: ignore[arg-type]
            except runtime.AgentCancelled:
                raise
            except Exception:  # noqa: BLE001 — 进度上报失败不影响主链
                pass

    def _begin(name: str, detail: str = "开始执行…") -> None:
        """环节开始时通知前端：progress(name, None, detail)。
        ok=None 表示进行中，_on_phase 据此更新 phase 但不追加 live_stage。"""
        if progress:
            try:
                progress(name, None, detail)  # type: ignore[arg-type]
            except runtime.AgentCancelled:
                raise
            except Exception:  # noqa: BLE001
                pass

    def _fail(fatal_phase: str | None = None) -> dict:
        out = {"ok": False, "chapter_id": chapter_id, "mode": "revise",
               "stages": [s.to_dict() for s in stages]}
        _fp = fatal_phase or _fatal_stage.get("name")  # P0-1：显式传参优先；缺省取最近一次失败环节
        if _fp:
            out["fatal_phase"] = _fp
            out["fatal_phase_confirmed"] = True
        return _settle_fail(book, chapter_id, stages, out, dry_run=dry_run)

    # ★2026-10-07（Plan A，外脑裁定）revise 链原子性：此前 revise 在 post_write（BLOCK 点）
    #   之前就把新正文（target.write_text）与 delta（step_delta_draft 直写正式 追踪/）落盘，
    #   任何后续失败都**不回滚** → 退化稿/污染稿残留正式章，且正式 delta 被草稿覆盖
    #   （实跑第002章：插入并入锚段产生 221 字段 J1 BLOCK，正文与 delta 均已被改写且未复原，
    #    需人工剥离）。此处对齐 rewrite 链事务契约（pipeline.py L3413-3478）：
    #   落盘前建快照，失败出口按 _state_unchanged() 判定，把「正文 + delta + 台账」恢复
    #   到 revise 开始之前。commit 是唯一推进 state 的动作，故 BLOCK（未 commit）→ state 不变
    #   → 回滚；若 state 已推进（commit 已发生）→ 绝不回滚正文，改落 RECOVERY_PENDING 台账。
    body_written = {"done": False}
    _tx_delta = book / _TRACKING_DIR / f"{_DELTA_PREFIX}{chapter_id:03d}章.json"
    _tx_ledger = book / _TRACKING_DIR / "pending_commits" / f"第{chapter_id:03d}章.json"
    _tx_snap: dict[str, bytes | None] = {"delta": None, "ledger": None}
    _tx_state_before: dict[str, object] = {"revision": None, "last": None}
    _tx_body_before: dict[str, str | None] = {"text": None}

    def _rollback_body() -> str:
        """回滚落盘的新正文到 revise 执行前正文；未落盘（标志 False）不动作。
        返回回滚结果描述（空串=无需回滚）。只做一次（回滚后复位标志）。"""
        if not body_written.get("done"):
            return ""
        _before = _tx_body_before.get("text")
        if _before is None:
            return "回滚失败（缺执行前正文快照），请人工从 .backup/ 恢复"
        try:
            target.write_text(_before, encoding="utf-8")
        except Exception:  # noqa: BLE001
            return "回滚失败，请人工从 .backup/ 恢复"
        body_written["done"] = False
        return "已回滚执行前正文"

    def _state_unchanged() -> bool:
        """只读断言：state_revision / last_committed_chapter 是否仍等于 revise 开始前。"""
        try:
            _s = json.loads((book / _TRACKING_DIR / "_tracking-state.json").read_text(encoding="utf-8"))
            return (_s.get("state_revision") == _tx_state_before.get("revision")
                    and _s.get("last_committed_chapter") == _tx_state_before.get("last"))
        except Exception:  # noqa: BLE001 — 读不到视为「无法证明未变」，保守报 False
            return False

    def _rollback_transaction() -> str:
        """本次 revise 的全部持久化副作用回滚：正文 + delta + 台账。

        幂等：revise 前不存在的文件删除；revise 前存在的文件按字节快照复原。
        返回回滚结果描述（空串=无需回滚）。"""
        parts: list[str] = []
        _rb = _rollback_body()
        if _rb:
            parts.append(_rb)
        for _k, _p in (("delta", _tx_delta), ("ledger", _tx_ledger)):
            try:
                _before = _tx_snap.get(_k)
                if _before is None:
                    if _p.is_file():
                        _p.unlink()
                        parts.append(f"已删除本次生成的{_k}")
                elif (not _p.is_file()) or _p.read_bytes() != _before:
                    _p.write_bytes(_before)
                    parts.append(f"已恢复 revise 前的{_k}")
            except Exception:  # noqa: BLE001 — 单项失败不阻断其余，但必须在 detail 里可见
                parts.append(f"{_k}回滚失败，请人工处理")
        if not _state_unchanged():
            parts.append("⚠ state_revision/last_committed 与 revise 前不一致（未回滚，需人工核对）")
        return "；".join(parts)

    def _rollback_on_cancel() -> None:
        """AgentCancelled 出口专用：回滚后仅入 stages（不经 progress 回调，
        避免取消路径再次触发回调异常）。语义=事务回滚（正文 + delta + 台账）。
        2026-10-19 补齐：Plan A 只覆盖了异常出口，取消出口此前裸 raise 漏回滚。"""
        rb = _rollback_transaction()
        if rb:
            stages.append(StageResult("rollback_on_cancel", True, rb))

    if not note:
        _finish("revise", False, "定点修订必须带 note（指定修订点/修订要求），无 note 无法执行。请改用「重写」或补全修订要求。")
        return _fail()

    # ★2026-10-09（外脑裁决：方案 A 后端自动改派）：note 命中「重写/改写/…」+「开头/段落/…」
    #   时，不报错，而是自动改派 rewrite L1（保持剧情重写，只改表达不改事件），
    #   并在返回中标记 action_redirected 供前端识别。彻底解决「继续重试」按钮
    #   原样重发 revise 导致的死循环。
    _REPLACE_VERBS = ("重写", "改写", "替换", "覆盖", "删掉", "删除", "去掉", "删去")
    _REPLACE_TARGETS = ("开篇", "开头", "首段", "第一段", "结尾", "末段", "整章", "全章", "段落")
    if any(v in note for v in _REPLACE_VERBS) and any(t in note for t in _REPLACE_TARGETS):
        # ★2026-10-09 修正：改派标记必须 ok=True——端点回执构建器取「第一个失败 stage」
        #   当 failure（nwwmvp.py L1777），ok=False 的标记会把真实失败（如 analyze 截断）
        #   盖住，回执误导排查方向。
        _finish("redirect", True, "检测到「改写/替换/删除」类诉求，超出 revise 只增不删边界 → 自动改派 rewrite L1")
        # 自动改派 rewrite L1（保持剧情重写），确保只重写表达而不丢剧情
        rewrite_result = run_rewrite_pipeline(
            book, chapter_id, progress=progress, on_writer_token=on_writer_token,
            note=f"【自动改派自 revise】{note}。要求：保持原有剧情与结构不变，只重写开头段落使其承接上一章结尾；"
                 f"若需新增过渡段，仅在开头第一段之前插入，不动原文其他部分。",
            level="L1", dry_run=dry_run,
        )
        rewrite_result["action_redirected"] = True
        rewrite_result["redirected_from"] = "revise"
        rewrite_result["redirect_reason"] = "note 含「重写/改写/替换/删除」+「开头/段落」，超出 revise 只增不删能力边界"
        # 把 revise 的 stage 记录合并到 rewrite 的 stages 中，便于审计追溯
        rewrite_result["stages"] = [s.to_dict() for s in stages] + rewrite_result.get("stages", [])
        return rewrite_result

    try:
        # 0. 悬空闸 + 目标章须已提交
        _last = _last_committed_chapter(book)
        if _last is not None:
            _pending = _pending_uncommitted_chapter(book, chapter_id, _last)
            if _pending is not None:
                _dangling = (
                    f"存在未提交章节：第{_pending}章已写正文但尚未提交（当前已提交到第{_last}章）。"
                    f"请先对第{_pending}章执行「重写」完成提交，再修订后续章节。"
                )
                if _gate().is_v13():  # type: ignore[attr-defined]
                    _gate_record(book, chapter_id, "0pre/dangling_chapter",
                                 _dangling, "pipeline/revise")
                    _finish("backup", True,
                            f"[V13 道闸] 悬空事实已记录，动作继续：{_dangling[:200]}")
                else:
                    _finish("backup", False, _dangling)
                    return _fail()

        target = _resolve_existing_body(book, chapter_id)
        if target is None:
            _finish("backup", False, f"第{chapter_id}章无已提交正文（或正文已被备份改名）。无法修订不存在的章节。")
            return _fail()

        # 1. 写前备份（幂等）；dry_run 不写正式 .backup/
        original = target.read_text(encoding="utf-8")
        # ★2026-10-07 Plan A：落盘前建立事务快照——执行前正文原文（sanitize/去重之前，
        #   即磁盘真实状态）+ delta/台账字节 + 状态版本，供 _rollback_transaction 精确复原。
        _tx_body_before["text"] = original
        for _k, _p in (("delta", _tx_delta), ("ledger", _tx_ledger)):
            try:
                _tx_snap[_k] = _p.read_bytes() if _p.is_file() else None
            except Exception:  # noqa: BLE001 — 快照失败按「revise 前不存在」处理（回滚即删除）
                _tx_snap[_k] = None
        try:
            _s = json.loads((book / _TRACKING_DIR / "_tracking-state.json").read_text(encoding="utf-8"))
            _tx_state_before["revision"] = _s.get("state_revision")
            _tx_state_before["last"] = _s.get("last_committed_chapter")
        except Exception:  # noqa: BLE001 — 读不到则断言恒 False，回滚 detail 会显式告警
            pass
        if dry_run:
            _rid = f"revise_{uuid.uuid4().hex[:12]}"
            _finish("backup", True, "dry-run：跳过正式 .backup 写入")
        else:
            bk = toolbox.run_script("nww_backup.py", ["--book", str(book), "--file", str(target)], book, timeout=60)
            if not bk["ok"]:
                _finish("backup", False, f"原稿备份失败: {bk['stderr'] or bk['stdout']}")
                return _fail()
            _finish("backup", True, bk["stdout"][:300], path=bk["stdout"].strip())

        # 1b. 写前预处理：段落归一 + 跨章去重（P1-A 修复——去重必须在锚点插入之前做，
        #     否则插入后去重删除「与上一章逐字重复」的原文句会让「只增不删」diff 兜底误判拦截；
        #     先清原文再插入 = 插入的 content 不受去重影响，diff 基线是清洗后的 original）
        original = _sanitize_paragraphs(original)
        original, _pre_dedup = _auto_dedup_prev_chapter(book, chapter_id, original)
        original = _ensure_title_consistent(book, chapter_id, original)
    except runtime.AgentCancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        _finish("backup", False, f"原稿备份失败: {exc}")
        return _fail()

    # 2. LLM 生成插入指令（锚点协议）→ 确定性执行插入；失败带原因重试一次
    new_text = ""
    apply_reports: list[str] = []
    last_err = ""
    err_history: list[str] = []
    ops: list[dict] = []
    for attempt in (1, 2):
        try:
            attempt_note = note if attempt == 1 else _revise_retry_note(note, last_err)
            raw = step_revise(book, chapter_id, original, attempt_note, on_token=on_writer_token)
            ops = _parse_revise_ops(raw)
            if not ops:
                # ★协议第 6 条：模型判定 note 要求的意思原文已有 → 无需修订（合法结单）
                _finish("revise", True,
                        "无需修订：模型判定 note 要求的意思原文已存在，原稿未改动"
                        + ("（第 2 次尝试）" if attempt == 2 else ""),
                        attempts=attempt, no_op=True)
                return {"ok": True, "chapter_id": chapter_id, "mode": "revise",
                        "no_op": True, "stages": [s.to_dict() for s in stages]}
            new_text, apply_reports = _apply_anchored_insertions(original, ops)
            _validate_inserted_content(book, chapter_id, ops, original)
            _finish("revise", True,
                    f"插入指令 {len(ops)} 处（{'；'.join(apply_reports)}）" + ("（第 2 次尝试）" if attempt == 2 else ""),
                    attempts=attempt)
            break
        except runtime.AgentCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 — 协议违规可重试
            last_err = str(exc)
            # ★两次原因都留档（原先只报第 2 次，两条不同原因时第一条丢失，排查困难）
            err_history.append(f"第{attempt}次：{last_err}")
            continue
    else:
        _finish("revise", False,
                f"锚点插入两次尝试均失败（{' ｜ '.join(err_history)}）（修订稿未落盘，原稿未改动）")
        return _fail()

    # 2b. 插入后段落归一（锚点插入可能产生贴段；只动空白，不增删内容）
    new_text = _sanitize_paragraphs(new_text)

    try:
        # 3. 兜底 diff 校验（removed 必须=0：锚点执行器构造上只增不删，>0 即内部异常）
        guard = _diff_guard_only_additions(original, new_text, threshold=0.0)
        if not guard["ok"]:
            _finish("revise_diff", False, f"只增不删兜底校验失败：{guard['detail']}（修订稿未落盘，原稿未改动）", diff=guard)
            return _fail()
        _finish("revise_diff", True, f"只增不删构造保证 + 兜底校验通过（{guard['detail']}）", diff=guard)
        if dry_run:
            sb = _dry_run_sandbox(book, _rid)
            target = sb / "body-preview" / f"第{chapter_id:03d}章.md"
        rel = target.relative_to(book).as_posix()
        target.write_text(new_text + "\n", encoding="utf-8")
        if not dry_run:
            # ★2026-10-07 Plan A：标记正文已落盘，失败出口据此回滚（dry_run 写 sandbox，不回滚）
            body_written["done"] = True
        # 模式C（2026-10-25）：revise 链 advisory 闸（增补内容用错代词/数字 → 报告不阻断）
        _adv = _advisory_gates_report(book, chapter_id, new_text)
        _finish("normalize", True,
                f"{'dry-run 正文预览' if dry_run else '覆盖落盘'}: {rel}（{len(new_text)} 字；锚点插入 {len(ops)} 处；写前去重 {len(_pre_dedup)} 句；{guard['detail']}）"
                + ("；⚠ advisory 闸：" + "；".join(_adv) if _adv else ""),
                path=rel, chars=len(new_text), diff=guard, advisory_gates=_adv)
    except runtime.AgentCancelled:
        _rollback_on_cancel()
        raise
    except Exception as exc:  # noqa: BLE001
        _rb = _rollback_transaction()
        _finish("normalize", False, f"兜底校验/落盘失败: {exc}" + (f"；{_rb}" if _rb else ""))
        return _fail()

    try:
        # 4. delta 草稿（revision 模式）+ 语义补全
        draft_path = step_delta_draft(book, chapter_id,
                                      sandbox_dir=(sb / "delta-preview") if dry_run else None,
                                      preserve_quality_meta=not dry_run)
        _finish("delta_draft", True, f"草稿: {draft_path.relative_to(book).as_posix()}")
        step_delta_semantic(book, chapter_id, draft_path, target)
        _finish("delta_semantic", True, "语义字段已补全")
    except runtime.AgentCancelled:
        _rollback_on_cancel()
        raise
    except Exception as exc:  # noqa: BLE001
        # ★2026-10-07 Plan A：正文已落盘 + 正式 delta 可能已被草稿覆盖 → 必须连 delta 一起回滚
        _rb = _rollback_transaction()
        _finish("delta_semantic", False, f"delta 处理失败: {exc}" + (f"；{_rb}" if _rb else ""))
        return _fail()

    # 5. post_write（BLOCK 点，revision 提交）
    if dry_run:
        # P3（外脑 2026-10-07 裁定）：delta 已由 step_delta_draft(sandbox_dir=...) 直写 sandbox，
        # 不得再从正式 追踪/ move（会搬走/误动正式 delta）。
        _finish("dry_run", True, "revise dry-run：正文/delta 已入 sandbox，正式目录零变化、未 commit")
        return {"ok": True, "chapter_id": chapter_id, "mode": "revise", "dry_run": True,
                "execution_id": _rid, "commit_status": "SKIPPED_DRY_RUN",
                "sandbox": sb.relative_to(book).as_posix(),
                "stages": [s_.to_dict() for s_ in stages]}
    def _post_write_exc_fail(exc: Exception) -> dict:
        """post_write 异常统一出口（首次与 paragraph_fix 重试共用）。
        同重写链：异常可能发生在 commit **之后**（enforce → gate → commit → cross_chapter
        串跑）。state 未推进 → 事务回滚；已推进 → 落台账，绝不回滚正文。"""
        if _state_unchanged():
            _rb = _rollback_transaction()
            _finish("post_write", False, f"post_write 异常（事务回滚）: {exc}" + (f"；{_rb}" if _rb else ""))
        else:
            _write_pending_commit_ledger(
                book, chapter_id, f"post_write 异常且状态已推进（不可回滚）:\n{exc}")
            _finish("post_write", False,
                    f"post_write 异常（state 已推进，落 RECOVERY_PENDING 台账，未回滚正文）: {exc}")
        return _fail()

    try:
        post = step_post_write(book, chapter_id, target)
    except runtime.AgentCancelled:
        _rollback_on_cancel()
        raise
    except Exception as exc:  # noqa: BLE001
        return _post_write_exc_fail(exc)
    # ★2026-10-07（revise 链补齐 paragraph_gate 修复路径，同 rewrite 链 4059-4066 先例）：
    #   锚点插入内容会并入锚句所在段落，实跑第002章产生 221 字段 2 → [J1_single_overlong]
    #   BLOCK；revise 链此前无修复路径，整链在最后一环失败。此处 BLOCK 即确定性拆段
    #   （零文字损失）→ 重跑一次 post_write，仍 BLOCK 则 fail-closed。
    if not post["ok"] and ("J2_long_chain" in post["stdout"] or "J1_single_overlong" in post["stdout"]):
        _wall_fixes = _fix_paragraph_walls(target)
        if _wall_fixes:
            _finish("paragraph_fix", True, "拆段: " + "；".join(_wall_fixes))
            try:
                post = step_post_write(book, chapter_id, target)
            except runtime.AgentCancelled:
                _rollback_on_cancel()
                raise
            except Exception as exc:  # noqa: BLE001
                return _post_write_exc_fail(exc)
    _finish("post_write", post["ok"],
            (post["stdout"] + ("\n" + post["stderr"] if post["stderr"] else ""))[:2400],
            returncode=post["returncode"], mode="revision")
    if not post["ok"]:
        # ★2026-10-07 Plan A：post_write BLOCK（commit 未发生 → state 不变）→ 事务回滚，
        #   正文/delta/台账恢复到 revise 之前（等价「原稿未改动」）；若 commit 已发生但后续
        #   环节报错（state 已推进）→ 绝不回滚正文，落 RECOVERY_PENDING 台账交恢复闭环。
        if _state_unchanged():
            _rb = _rollback_transaction()
            stages.append(StageResult("rollback", True, _rb or "无需回滚"))
            return _fail()
        _write_pending_commit_ledger(
            book, chapter_id,
            f"post_write BLOCK (revise):\n{post['stdout']}\n{post['stderr']}")
        return _fail()

    # commit 已成功 → 指纹重绑最终正文（post_write 内 enforce 会再改正文，见 deslop 链注释）
    _rebind_body_quality_meta(draft_path, target)

    # 6. 机械层复扫（advisory）
    try:
        reports = step_deslop(book, chapter_id, target, check_only=True)
        _finish("rescan", True, f"复扫脚本数: {len(reports)}", reports=reports)
    except runtime.AgentCancelled:
        raise  # commit 已成功，取消不回滚（同重写链 commit 后语义）
    except Exception as exc:  # noqa: BLE001
        # commit 已成功的后置复扫异常：降级 advisory，不让裸异常逃逸（会丢 stages 且台账不清）。
        _finish("rescan", False, f"复扫异常（advisory，不影响已提交结果）: {exc}")

    _clear_pending_commit_ledger(book, chapter_id)
    return {"ok": True, "chapter_id": chapter_id, "mode": "revise", "stages": [s.to_dict() for s in stages]}
# ---------------------------------------------------------------------------
# 润色链（文本质量优化层，2026-09-20 用户裁决落地）：保剧情提表达、合同守卫防污染。
#   与 deslop 链的边界：deslop = 去AI味专项（内部环节/独立工具）；润色 = 四维检查
#   （行文逻辑/场景逻辑/语言逻辑/阅读节奏）+ 一次改写 pass + 去AI味清理 + 合同守卫。
#   流程：写前闸 → 备份 → analyze(诊断+章节合同快照) → rewrite_pass(单次改写) →
#   deslop(LLM 去AI味清理) → normalize 落盘 → contract_guard(确定性硬约束) →
#   delta → post_write commit → 机械层复扫。
#   与重写链差异：不跑 review_cycle（避免审稿修订把润色改掉重写一遍）；不走
#   wordcount compress（润色不改体量）；不重读 Writing Packet 重新生成（只基于本章正文）。
# ---------------------------------------------------------------------------
_POLISH_ANALYZE_SYSTEM = (
    "你是章节文本质量诊断师，为网文润色做改写前分析。\n"
    "【输入】<original> 本章正文、<goal> 本章目标（来自细纲）。\n"
    "【输出】只输出一个 JSON 对象（前后不写任何说明/围栏）：\n"
    "{\n"
    '  "diagnosis": ["问题：位置+问题+怎么改（≤40字/条，最多6条）"],\n'
    '  "contract": {\n'
    '    "locked_events": ["本章必须保留的剧情事件（骨架化短语：人物+动作+结果/变化，'
    '8~20字；只收动作/事件/状态变化/结果达成，不收静态描写、感官细节、比喻与修饰；'
    '例：❌温故看着自己手心那层淡淡的暖光，整个人僵住了 ✅温故发现手心发光，僵在原地）"],\n'
    '    "locked_entities": ["本章必须保留出场的角色/势力/物品名（必须是原文中出现的原词，禁止合并、缩写或造新词）"],\n'
    '    "locked_relations": ["角色-角色-关系（如 林小凡-温故-朋友）；**端点必须是原文逐字出现的角色/势力名，'
    '禁止拼接/造词**（如不得把「病号」+「灰袍人」拼成「病号灰袍人」——原文没有的词不得锁）"],\n'
    '    "locked_numerics": [{"surface": "原文中逐字出现的完整数值表达'
    '（阿拉伯数字/中文数词/约数/比例/折扣/数量+单位；逐字来自原文，禁止数学换算，'
    '如 半个月/两箱/三四十号人/八折 原样收录；禁止截断/数字化/同义替换）",'
    ' "occurrence": 1}],\n'
    '    "locked_goal": "本章目标短语"\n'
    "  }\n"
    "}\n"
    "【规则】contract 只能从原文整理，不得发明、不得概括过度；"
    "locked_events 骨架化提取（剔除修饰词，保留事实骨架），核心剧情事件宁多勿漏，"
    "描写句不入列（那是润色的正当改写对象）；"
    "locked_numerics 硬规则（必须机器可验证，任一条不合法整份合同作废）："
    "① surface 必须是**原文中逐字出现的完整数值表达**，逐字复制、不得截断、不得数字化、"
    "不得同义替换或规范化（如 三四十号人 不得写成 四十 或 30；半个月 不得写成 十五天；两箱 不得写成 二箱）；"
    "② occurrence 表示该 surface 在原文中第几次逐字出现（1-based），仅当同一 surface 多次出现时用于消歧；"
    "只出现一次或不确定时省略该字段（默认 1）；occurrence 超出实际出现次数即整份作废；"
    "③ 只输出 surface 与 occurrence 两个字段；锚点/定位文字由系统按原文确定性构造，无需你提供；"
    "④ surface 必须是**真正的数值表达**（数量/比例/折扣/约数/时间量/度量等原文实际出现的数字事实）；"
    "**严禁**把章节标题（如「第001章」「第N章」）、章节名、序号、或任何非数值的纯文本列为 locked_numerics——"
    "它们不是数值事实，列入即触发整份合同作废；"
    "正例：原文「，少说三四十号人，队伍歪歪扭扭…」→ "
    '{"surface":"三四十号人","occurrence":1}；'
    "反例：surface 写成「四十」（截断）/ 写成「30」（数字化）/ 写成「十五天」而原文是「半个月」（同义替换）；"
    "diagnosis 只针对表达质量（行文逻辑/场景逻辑/语言逻辑/阅读节奏/文笔/AI味），不评判剧情。"
)

_POLISH_REWRITE_SYSTEM = (
    "你是网文润色执行师（文本质量优化层）：保持剧情不变，提高表达质量。\n"
    "【最高约束·合同锁定】以下 <contract> 是本章不可变更的硬约束，必须全部保留：\n"
    "1. locked_events 中每个关键事件必须完整保留：事件句的关键内容词（人物/动作/物体/结果词）"
    "必须保留大半（≥一半），可微调措辞与语序，禁止整句重写、禁止替换核心物体或动作的说法；"
    "禁止删除、合并或改变事件结果；\n"
    "2. 禁止新增原文没有的事件、角色、关系、伏笔、设定；\n"
    "3. locked_entities 中的角色/势力/物品必须照常出场，禁止改名；\n"
    "4. locked_relations 中的关系不得改变；\n"
    "5. locked_numerics 中每条 surface 数值必须逐字保持（禁止任何形态变化，"
    "如 半个月→十五天、两箱→二箱、八折→八成）；其 anchor_before/anchor_after 是定位锚点，"
    "必须逐字保留，禁止把数值搬离原锚点位置；\n"
    "6. 不得改变章节目标、人物口吻、段落顺序与事件顺序。\n"
    "【允许】按 <diagnosis> 逐条修复：行文逻辑（视角/动作链/指代）、场景逻辑（位置/时间/因果/信息顺序）、"
    "人物语言逻辑（口吻/年龄/身份/性格）、网文阅读节奏（长句拆分/信息密度/爽点延迟/段落节奏）、"
    "文笔与AI味。可以改写表达，但事件内容与参与者必须完整保留。\n"
    "【输出】正文放在 <prose> 与 </prose> 标签之间，自 `# 第N章　标题` 起输出完整成稿；"
    "标签前后不写任何说明、不写修改清单。"
)

# 事件组件检查用的小虚词（不参与「事件内容词」权重的计分）
_POLISH_STOPWORDS = {
    "一个", "一些", "自己", "他们", "她们", "我们", "你们", "这个", "那个", "然后",
    "于是", "因为", "所以", "但是", "不过", "之后", "现在", "这时", "接着", "最终",
    "终于", "已经", "正在", "忽然", "突然", "起来", "出来", "下去", "什么", "怎么",
    "这样", "那样", "就是", "还是", "只是", "知道", "看见", "觉得", "发现", "开始",
}


def _build_numeric_anchors(locks: list, original: str) -> tuple[list[str], list[dict]]:
    """确定性锚点构造（2026-10-07 R 方案·外脑定稿）：

    LLM 只负责指出 surface + occurrence（找出数值事实）；锚点由本函数在原文中定位
    第 occurrence 次逐字出现后，**确定性截取**左右紧邻各 4 字（不足 4 取实际可用长度）。
    2026-11-01 边界修复：surface 紧贴正文首尾时，缺失的一侧是**位置性**的（非截断/越界错误），
    不再判该条 FAIL——否则章节标题『第001章』这类贴首 surface 会把整份合同判废。
    仅当 surface 位于正文中部却仍不足 2 字（真实截断/越界）才 FAIL；锚点始终确定性可校验。
    不做 trim、不做标点清洗、不做语义修正、不做数学换算；只在归一化文本（去空白）上定位。
    返回 (错误列表, 组装好的锁列表[surface/occurrence/anchor_before/anchor_after])。
    """
    def _n(s) -> str:
        return re.sub(r"\s+", "", str(s or ""))
    on = _n(original)
    errs: list[str] = []
    built: list[dict] = []
    for k, n in enumerate(locks or [], 1):
        if not isinstance(n, dict):
            errs.append(f"第{k}条数值锁不是对象")
            continue
        surface = _n(n.get("surface"))
        if not surface:
            errs.append(f"第{k}条数值锁缺少 surface")
            continue
        occ_raw = n.get("occurrence")
        if occ_raw is None or occ_raw == "":
            occ = 1  # 缺省=1（仅此一种走默认；显式 0/负数/非法一律 FAIL，不静默 clamp）
        else:
            try:
                occ = int(occ_raw)
            except (TypeError, ValueError):
                errs.append(f"第{k}条 occurrence「{occ_raw}」非法（须为 1-based 正整数）")
                continue
        if occ < 1:
            errs.append(f"第{k}条 occurrence={occ} 非法（须为 1-based 正整数）")
            continue
        start, pos = -1, 0
        for _i in range(occ):  # 逐字定位第 occurrence 次出现
            start = on.find(surface, pos)
            if start < 0:
                break
            pos = start + len(surface)
        if start < 0:
            errs.append(f"第{k}条 surface「{surface}」在原文逐字出现不足 {occ} 次，无法定位")
            continue
        end = start + len(surface)
        nb = on[max(0, start - 4):start]
        na = on[end:end + 4]
        # 2026-11-01 边界感知：surface 紧贴文首/文尾时，缺失一侧是**位置性**的
        # （如章节标题「第001章」前仅有 '#' 标记，或正文末的数值），非截断/越界错误，
        # 不再判该条 FAIL——否则会把整份合同判废。中部 surface 邻域恒 ≥2 字（循环上方保证），
        # 故仅当「既非文首边界却左 <2」或「既非文尾边界却右 <2」才 FAIL（真实截断/越界）。
        left_boundary = start <= 1              # 文首：前导仅 ≤1 字（通常 '#' 等位置性标记）
        right_boundary = (len(on) - end) <= 1   # 文尾：后随仅 ≤1 字
        if (not left_boundary and len(nb) < 2) or (not right_boundary and len(na) < 2):
            errs.append(
                f"第{k}条 surface「{surface}」左/右可用上下文不足 2 字"
                f"（{len(nb)}/{len(na)}），无法构造合法锚点")
            continue
        built.append({"surface": surface, "anchor_before": nb, "anchor_after": na, "occurrence": occ})
    return errs, built


def _norm_span(raw: str, norm_target: str):
    """在 raw 文本中按去空白归一化查找 norm_target，返回其对应 raw (start, end) 区间；找不到返回 None。"""
    hn = re.sub(r"\s+", "", raw)
    i = hn.find(norm_target)
    if i < 0:
        return None
    raw_s, norm_i = 0, 0
    while norm_i < i and raw_s < len(raw):
        if raw[raw_s].isspace():
            raw_s += 1
        else:
            norm_i += 1
            raw_s += 1
    raw_e = raw_s
    while norm_i < i + len(norm_target) and raw_e < len(raw):
        if raw[raw_e].isspace():
            raw_e += 1
        else:
            norm_i += 1
            raw_e += 1
    return raw_s, raw_e


def _raw_occurrence_span(raw: str, surface_norm: str, occ: int):
    """在 raw 文本中按归一化计数定位第 occ 次 surface_norm 出现的 raw (start, end) 区间；找不到返回 None。"""
    on = re.sub(r"\s+", "", raw)
    s, p = -1, 0
    for _i in range(occ):
        s = on.find(surface_norm, p)
        if s < 0:
            return None
        p = s + len(surface_norm)
    raw_s, ni = 0, 0
    while ni < s and raw_s < len(raw):
        if raw[raw_s].isspace():
            raw_s += 1
        else:
            ni += 1
            raw_s += 1
    raw_e = raw_s
    while ni < s + len(surface_norm) and raw_e < len(raw):
        if raw[raw_e].isspace():
            raw_e += 1
        else:
            ni += 1
            raw_e += 1
    return raw_s, raw_e


def _restore_locked_numerics(polished: str, contract: dict, original: str) -> tuple[str, list[str]]:
    """确定性兜底（2026-11-01）：rewrite/deslop 偶发丢弃或挪动锁定数值时，从原文把
    「anchor_before + surface + anchor_after」整段还原回润色稿——仅当该归一化锚点串不在稿中才插回。
    这是合同守卫的数值安全网：不依赖模型合规、不改写其它内容；实体/关系端点仍靠 prompt 约束。
    返回 (修复后正文, 修复项 surface 列表)。"""
    numerics = contract.get("locked_numerics") or []
    if not numerics:
        return polished, []
    out = polished
    pn = re.sub(r"\s+", "", out)
    restored: list[str] = []
    for n in numerics:
        if not isinstance(n, dict):
            continue
        surface = re.sub(r"\s+", "", str(n.get("surface") or ""))
        if not surface:
            continue
        # 2026-11-01 防重复加固：surface 已在稿中（归一化后）→ 绝不二次插回，
        # 避免产生重复句/碎片。邻域改写属真正违规，交由 contract_guard + retry 收敛。
        if surface in pn:
            continue
        ab = re.sub(r"\s+", "", str(n.get("anchor_before") or ""))
        aa = re.sub(r"\s+", "", str(n.get("anchor_after") or ""))
        target = ab + surface + aa
        if target and target in pn:
            continue  # 锚点串已在稿中，无需还原
        occ = int(n.get("occurrence") or 1)
        span = _raw_occurrence_span(original, surface, occ)
        if not span:
            continue
        rs, re_ = span
        margin = max(len(ab), len(aa)) + 8  # 宽松包住邻域
        snippet = original[max(0, rs - margin):min(len(original), re_ + margin)]
        ins = None
        if ab:
            m = _norm_span(out, ab)
            if m:
                ins = m[1]  # anchor_before 之后插入（邻域对齐）
        if ins is None:
            ins = len(out)  # 末尾兜底（surface 已确认不在稿中，不会重复）
        out = out[:ins] + snippet + out[ins:]
        pn = re.sub(r"\s+", "", out)
        restored.append(surface)
    return out, restored


def _prune_phantom_locks(contract: dict, original: str) -> list[str]:
    """2026-11-01 合同净化：analyze 偶发把原文不存在的名词/数值/关系端点锁进合同（幻觉增字）。

    这些 phantom 锁会让 contract_guard 永远无法满足、逼模型编造内容（如原文无「一瞬间」却锁
    「一瞬间」→ 模型硬造重复句）。冻结前与复用前按原文归一化逐一核验，缺席即丢弃。
    返回被丢弃项描述列表（用于日志/审计）。"""
    on = re.sub(r"\s+", "", original)
    dropped: list[str] = []

    nums = contract.get("locked_numerics") or []
    kept_n = []
    for n in nums:
        if not isinstance(n, dict):
            continue
        s = re.sub(r"\s+", "", str(n.get("surface") or ""))
        if s and s in on:
            kept_n.append(n)
        else:
            dropped.append(f"数值「{n.get('surface')}」(原文无)")
    contract["locked_numerics"] = kept_n

    ents = contract.get("locked_entities") or []
    kept_e = []
    for e in ents:
        s = re.sub(r"\s+", "", str(e or ""))
        if s and s in on:
            kept_e.append(e)
        else:
            dropped.append(f"实体「{e}」(原文无)")
    contract["locked_entities"] = kept_e

    rels = contract.get("locked_relations") or []
    kept_r = []
    for rel in rels:
        parts = [x.strip() for x in re.split(r"[-–—]", str(rel)) if x.strip()]
        ok = True
        for name in parts[:2]:
            sn = re.sub(r"\s+", "", name)
            if len(sn) >= 2 and sn and sn not in on:
                ok = False
                dropped.append(f"关系端点「{name}」(原文无)")
                break
        if ok:
            kept_r.append(rel)
    contract["locked_relations"] = kept_r
    return dropped


def _validate_numeric_locks(contract: dict, original: str) -> tuple[list[str], list[dict]]:
    """数值锁确定性校验（2026-10-07 用户裁决 + 外脑定稿，P0 误报修复）：

    口径：surface/anchor_before/anchor_after 必须**逐字命中原文**，且以
    「anchor_before+surface+anchor_after」能在原文唯一定位（同签名多处用 occurrence 区分）。
    R 方案下 anchor 由 `_build_numeric_anchors` 确定性构造，本函数作为二次独立校验保留。
    LLM 可以提取合同，但不能创造合同事实——校验不过 → analyze 失败（由调用方按失败处理，不进入 rewrite）。
    不做数学换算、不做模糊匹配、不在锚点附近找数字。返回 (错误列表, 归一化后的锁列表)。
    """
    def _n(s) -> str:
        return re.sub(r"\s+", "", str(s or ""))
    on = _n(original)
    errs: list[str] = []
    cleaned: list[dict] = []
    for k, n in enumerate(contract.get("locked_numerics") or [], 1):
        if not isinstance(n, dict):
            errs.append(f"第{k}条数值锁不是对象")
            continue
        surface = _n(n.get("surface"))
        nb = _n(n.get("anchor_before"))
        na = _n(n.get("anchor_after"))
        try:
            occ = int(n.get("occurrence") or 1)
        except (TypeError, ValueError):
            occ = 1
        if not surface:
            errs.append(f"第{k}条数值锁缺少 surface")
            continue
        if surface not in on:
            errs.append(f"第{k}条 surface「{surface}」未逐字命中原文")
            continue
        if nb and nb not in on:
            errs.append(f"第{k}条 anchor_before「{nb}」未逐字命中原文")
            continue
        if na and na not in on:
            errs.append(f"第{k}条 anchor_after「{na}」未逐字命中原文")
            continue
        # 2026-10-07 occurrence 语义修正（外脑裁定 iii）：occurrence 只按**surface 逐字出现次数**校验，
        # 不与 anchor_before+surface+anchor_after 签名绑定（签名几乎必然唯一，绑定会把「第 N 次 surface」
        # 误判为「同一签名出现 N 次」）。锚点签名在此只承担「该 occurrence 位置邻域真实性」证明。
        s_cnt = on.count(surface)
        if occ < 1 or occ > s_cnt:
            errs.append(f"第{k}条 occurrence={occ} 超出 surface「{surface}」在原文的逐字出现次数 {s_cnt}")
            continue
        start, pos = -1, 0
        for _i in range(occ):  # 定位第 occurrence 次 surface 出现
            start = on.find(surface, pos)
            if start < 0:
                break
            pos = start + len(surface)
        if start < 0:
            errs.append(f"第{k}条 surface「{surface}」在原文逐字出现不足 {occ} 次")
            continue
        end = start + len(surface)
        if on[max(0, start - len(nb)):start] != nb or on[end:end + len(na)] != na:
            errs.append(f"第{k}条第 {occ} 次 surface「{surface}」紧邻邻域与锚点不一致（锚点不成立）")
            continue
        cleaned.append({"surface": surface, "anchor_before": nb, "anchor_after": na, "occurrence": occ})
    return errs, cleaned


def _polish_analyze(book: Path, chapter_id: int, original: str, goal: str, on_token=None) -> dict:
    """润色前置分析：一次 LLM 调用产出 {diagnosis, contract(章节合同快照)}。

    合同快照从原文提取（近逐字），供 rewrite_pass 提示词锁定 + contract_guard 确定性校验。
    2026-10-07（R 方案·外脑定稿）：LLM 只产 {surface, occurrence}，锚点由 `_build_numeric_anchors`
    按原文确定性构造——从协议上消除「取错侧/幻觉增字/越界标点」整类锚点错误；抽取质量保障 =
    prompt + **有界重试 ≤3 次**。任一次产出通过「构造+`_validate_numeric_locks`」即采用；
    3 次均不合法 → ValueError（fail-closed，不做失败锁丢弃/降级，调用方按失败处理，原稿不动）。
    """
    prompt = (
        f"# 第{chapter_id}章润色前分析\n"
        f"<goal>\n{goal[:800]}\n</goal>\n\n"
        f"<original>\n{original}\n</original>"  # 2026-09-22 去截断：审计必须看到全文，否则后段事件不进合同锁（守卫失明）
    )
    _attempts: list[str] = []
    _t0 = time.monotonic()
    for _k in range(1, 4):
        try:
            return _polish_analyze_once(book, chapter_id, original, prompt, on_token)
        except ValueError as exc:
            _attempts.append(f"第{_k}次：{exc}")  # 全量尝试过程入报错（不得仅显示最后一次）
            if _k < 3:
                # 2026-11-01 预算闸：重试前判定（首轮不拦）
                _analyze_retry_guard(_t0, "润色前置分析", _k, exc)
    raise ValueError("润色分析 3 次尝试均未产出合法合同——" + "；".join(_attempts))


def _polish_analyze_once(book: Path, chapter_id: int, original: str, prompt: str,
                         on_token=None) -> dict:
    """单次抽取尝试：LLM 生成 → JSON 解析 → 确定性构造锚点 → 数值锁校验。任一环节不合法 → ValueError。"""
    raw, _ = _writer_stream(_POLISH_ANALYZE_SYSTEM, prompt, on_token=on_token, temperature=0.3, book=book, chapter_id=chapter_id)
    cleaned = raw.strip()
    i, j = cleaned.find("{"), cleaned.rfind("}")
    if i < 0 or j <= i:
        raise ValueError("润色分析未输出 JSON 对象")
    try:
        data = json.loads(cleaned[i:j + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"润色分析 JSON 解析失败: {exc}") from exc
    contract = data.get("contract") if isinstance(data.get("contract"), dict) else {}
    for key, default in (("locked_events", []), ("locked_entities", []), ("locked_relations", []),
                         ("locked_numerics", [])):
        contract.setdefault(key, default if isinstance(default, list) else {})
    contract.setdefault("locked_goal", "")
    if not contract["locked_events"]:
        raise ValueError("章节合同快照缺少 locked_events（无法执行合同守卫），请重试")
    # R 方案·外脑定稿：LLM 只产 {surface, occurrence}，锚点由代码确定性构造后再进校验
    _berrs, _nbuilt = _build_numeric_anchors(contract.get("locked_numerics") or [], original)
    if _berrs:
        raise ValueError("合同数值锁锚点构造失败：" + "；".join(_berrs[:5]))
    contract["locked_numerics"] = _nbuilt
    _nerrs, _ncleaned = _validate_numeric_locks(contract, original)
    if _nerrs:
        raise ValueError("合同数值锁校验失败：" + "；".join(_nerrs[:5]))
    contract["locked_numerics"] = _ncleaned
    _pd = _prune_phantom_locks(contract, original)
    if _pd:
        print(f"[CONTRACT_PRUNE] 丢弃 {len(_pd)} 条原文不存在的幻影锁：{'；'.join(_pd[:8])}", flush=True)
    return {"diagnosis": [str(d) for d in (data.get("diagnosis") or [])][:6], "contract": contract}


def _polish_analyze_frozen(book: Path, chapter_id: int, original: str, goal: str,
                           on_token=None, persist: bool = True) -> tuple[dict, int]:
    """润色前置分析 + 合同冻结（2026-11-01 用户裁决，与 L1/L2 同一套机制）。

    润色链此前每次跑都现提合同（temperature=0.3，无缓存）：同一正文两次润色拿到
    不同的 locked_events/entities/numerics，`_validate_polish_contract` 的松紧跟着变，
    失败不可复现。冻结键与重写链同源（正文+目标+等级+结构版本），等级命名空间用
    "POLISH"，与 L1/L2 互不顶替；diagnosis 一并存档，复用时不丢诊断。

    返回 (analyze_dict, attempt_no)：0=复用冻结，1=本次提取。
    """
    _src = _contract_source_hash(original, goal, "POLISH")
    _refresh = str(os.environ.get("NWW_CONTRACT_REFRESH", "")).strip().lower() not in ("", "0", "false", "no")
    if not _refresh:
        _frozen = _load_frozen_contract(book, chapter_id, "POLISH", _src)
        if _frozen is not None:
            _c, _meta = _frozen
            _pd = _prune_phantom_locks(_c, original)
            if _pd:
                print(f"[CONTRACT_PRUNE] 复用冻结合同时丢弃 {len(_pd)} 条原文不存在的幻影锁："
                      f"{'；'.join(_pd[:8])}", flush=True)
                # 把净化后的合同回写磁盘（保留诊断），下次复用即干净
                try:
                    _save_frozen_contract(book, chapter_id, "POLISH", _src, _c, goal,
                                         extra={"diagnosis": list(_meta.get("diagnosis") or [])})
                except Exception as _e:  # noqa: BLE001 — 回写失败仅内存生效，不阻断主链
                    print(f"[CONTRACT_PRUNE] 回写失败（仅内存生效）：{_e}", flush=True)
            print(f"[CONTRACT_FREEZE] ch{chapter_id} POLISH 复用冻结合同"
                  f"（{_meta.get('created_at') or '未知时间'}，hash {_src[:8]}，"
                  f"事件 {len(_c.get('locked_events') or [])}）", flush=True)
            return {"diagnosis": list(_meta.get("diagnosis") or []), "contract": _c}, 0
    _an = _polish_analyze(book, chapter_id, original, goal, on_token=on_token)
    if persist:
        _p = _save_frozen_contract(book, chapter_id, "POLISH", _src, _an["contract"], goal,
                                   extra={"diagnosis": list(_an.get("diagnosis") or [])})
        if _p is not None:
            print(f"[CONTRACT_FREEZE] ch{chapter_id} POLISH 已冻结 → "
                  f"{_p.relative_to(book).as_posix()}（hash {_src[:8]}）", flush=True)
    return _an, 1


def _wc_hardline_ratio() -> float:
    """塌缩/L3 字数硬线比例（2026-10-24 用户裁决：1846/2000≈92% 不接受提交）。
    缺省 0.95：低于细纲目标的 95% 视为「细纲执行不完整」。NWW_WC_HARDLINE 可覆盖。
    ★ 2026-11-05 M2 语义收敛后本函数只承担两类语义：① compress/expand 塌缩闸底线
    （:3299/:3360）② L3 硬线提示与 Gate G 判定（:11844/:12057）。「带外触发扩写」
    已迁移到脚本带口径（status=under，±10% 带经 nww_wordcount.band_pct() 唯一权威源）；
    env NWW_WC_EXPAND_BAND=0 可回滚旧硬线触发面。"""
    try:
        r = float(os.environ.get("NWW_WC_HARDLINE", "0.95"))
    except ValueError:
        r = 0.95
    return r if r > 0 else 0.7


# ---- G_Length 闸门下限（安全网；与 Writer 扩写硬线刻意解耦）----
# 设计（2026-11-06 收敛）：Writer 扩写硬线仍用 _wc_hardline_ratio()（默认 0.95，
#  aspiration——要求尽量贴近细纲目标）；但 Gate G 只承担「安全网」职责，拦两类：
#  ① 单章合格线 2000（项目常量 _PROSE_MIN_CHARS 之上、与 :2875 注释「单章合格线」一致；
#     用户裁决 1846/2000≈92% 不收 → 低于 2000 必拦）；
#  ② 严重欠账比 0.70（低于细纲目标 70% = 整体性塌缩，如第 38 章 3828→1227）。
# floor = max(target×0.70, min(target, 2000))：
#    目标 2500 → max(1750, 2000) = 2000（放行 ~2100 的达标章，拦 1846 事故）；
#    目标 1500（短章）→ max(1050, 1500) = 1500（不超过目标、不误伤短章）；
#    目标 3500 → max(2450, 2000) = 2450（高目标仍拦明显欠账）。
# NWW_WC_SEVERE_RATIO 覆盖严重欠账比；NWW_WC_PUBLISH_FLOOR 覆盖单章合格线。
_G_SEVERE_RATIO = float(os.getenv("NWW_WC_SEVERE_RATIO", "0.70") or 0.70)
_G_PUBLISH_FLOOR = int(os.getenv("NWW_WC_PUBLISH_FLOOR", "2000") or 2000)


def _g_length_floor(target: int) -> int:
    """G_Length 安全网下限：放行达标章、只拦「不可发布 / 整体塌缩」。

    target <= 0 返回 0（无目标路径零影响）。"""
    if target <= 0:
        return 0
    return max(int(target * _G_SEVERE_RATIO), min(target, _G_PUBLISH_FLOOR))


def _check_length_collapse(original: str, rewritten: str, floor_ratio: float = 0.6) -> list[str]:
    """长度塌缩闸（2026-09-22 用户裁决，P0 事故修复）：
    输出可见字符 < 原文 floor_ratio → BLOCK。堵「整章被静默压缩」类事故
    （第38章 3828→1227 字当时无任何拦截）。口径与守卫 _norm 一致（去空白）。
    原文为空/极短时不判（无基准）。"""
    def _vis(s: str) -> int:
        return len(re.sub(r"\s+", "", str(s or "")))
    a, b = _vis(original), _vis(rewritten)
    if a > 0 and b < a * floor_ratio:
        return [f"长度塌缩：输出 {b} 可见字 < 原文 {a} 的 {floor_ratio:.0%}（内容疑似整体丢失/压缩）"]
    return []


def _collapse_after_normalize(original: str, target: Path,
                              floor_ratio: float = 0.8) -> list[str]:
    """normalize 落盘后的长度塌缩复检（2026-11-01 实锤，deslop 链此前完全无守卫）。

    背景：`_check_length_collapse` 只作用于「LLM 输出」，而 normalize（prose 提取 /
    元信息清洗）会**再砍一刀**。实测 00:18 真实 deslop 回执：
        polish     : 2124 → 1304 字（净减 820，1304/2124 = 61.4%，压线过 0.6）
        normalize  : 覆盖落盘（**932 字**，44%）
        delta_semantic: False → 事务回滚
    这次是语义补全的 LLM 恰好返回空 JSON 才意外救回；若它成功，932 字残稿会被
    commit 成新 revision。polish 链有 `_validate_polish_contract`（读落盘正文）兜住，
    revise 链有「只增不删」diff 兜住 —— 只有 deslop 链两头都空。

    阈值 0.8（2026-11-01 用户拍板）：deslop 内联 prompt 的契约是「删改 ≤15%、增大于删」，
    合规稿应 ≥85%，0.8 留 5% 余量。0.6 只兜得住 normalize 那一刀，兜不住 polish 自己
    已砍 39% 的情况（正是事故里真实发生的第一刀：61.4% 放行）。真实 deslop 实测多为
    增字（2124→2141 / 2118→2135），正常润色不会触线。需要大砍的章节应走「重写」链。

    以**盘上正文**为对象再判一次；读盘失败按「无法判定」放行（不新增假 BLOCK）。
    """
    try:
        on_disk = target.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return []
    return _check_length_collapse(original, on_disk, floor_ratio)


def _validate_polish_contract(original: str, polished: str, contract: dict) -> list[str]:
    """合同守卫（确定性，fail-closed）：对照章节合同快照校验润色稿。

    硬约束（BLOCK）：
      1. locked_entities 每个角色/势力/物品名必须在润色稿中仍出现（缺失=角色被删/改名）；
      2. locked_relations 的每个关系端点（角色名）必须仍出现；
      3. locked_numerics 数值：条目级锚点对位——润色稿中 surface 逐字出现次数须 ≥ occurrence，
         且锁定位置的锚点签名（anchor_before+surface+anchor_after）仍存在（≥1）；不满足=BLOCK
         （fail-closed，不静默跳过）；
      4. locked_events 事件内容词覆盖度：事件短语的内容词（剔除虚词后）按长度加权，
         在润色稿中必须保留 ≥50%（缺失过半=事件被删/合并）。
    不做语义判断：情绪/描写/语言变化不属于硬约束（那是润色对象本身）。
    """
    def _norm(s: str) -> str:
        return re.sub(r"\s+", "", str(s or ""))

    pn = _norm(polished)
    violations: list[str] = []

    _ents = [_norm(e) for e in (contract.get("locked_entities") or []) if e]
    for e in contract.get("locked_entities") or []:
        s = _norm(e)
        if s and s not in pn:
            violations.append(f"锁定角色/势力/物品「{e}」在润色稿中缺失（被删或改名）")

    for rel in contract.get("locked_relations") or []:
        parts = [x.strip() for x in re.split(r"[-–—]", str(rel)) if x.strip()]
        for name in parts[:2]:
            s = _norm(name)
            if len(s) >= 2 and s:
                if s in pn:
                    continue
                # 2026-11-01 复合端点容错：analyze 偶发把「病号」+「灰袍人」拼成原文
                # 不存在的复合端点（如 病号灰袍人），导致该端点永远无法满足。若端点包含某
                # 锁定实体且该实体在场，视为关系端点仍在稿中（不弱化对真正被删端点的拦截）。
                if any(e and e in s and e in pn for e in _ents):
                    continue
                violations.append(f"锁定关系端点「{name}」在润色稿中缺失")

    for num in contract.get("locked_numerics") or []:
        if not isinstance(num, dict):
            continue
        # 2026-10-07 P0 误报修复（外脑定稿）：旧实现「ctx 后紧跟纯阿拉伯数字」模型
        # 对中文数词/量词不可达，且假设 context 与数值零距离相邻，导致原稿对照自身
        # 即恒定误报 + ctx 未命中时静默跳过(fail-open)。改为**条目级锚点对位**：
        #   surface 逐字保持 + anchor_before/anchor_after 唯一定位（occurrence 消歧）。
        # 位置优先于计数：不做全局多重集统计、不在锚点附近找数字、不做数学换算。
        # 2026-10-07 occurrence 语义修正（外脑裁定 iii）：occurrence 只对 **surface 逐字出现次数** 计数
        # （不绑定锚点签名——签名几乎必然唯一，绑定会把「第 N 次 surface」误判为「同一签名出现 N 次」）；
        # 锚点签名只需证明「该锁定位置仍存在」（≥1），承担邻域真实性证明。
        surface = _norm(num.get("surface"))
        if not surface:
            continue
        nb = _norm(num.get("anchor_before"))
        na = _norm(num.get("anchor_after"))
        try:
            occ = int(num.get("occurrence") or 1)
        except (TypeError, ValueError):
            occ = 1
        # 2026-11-01 放宽：出现次数判据由 count>=occ 改为 count>=1——核心意图是「数值不被整删」，
        # 重复数值偶发被合并/少一次不应 BLOCK 整轮；occ 仅作确定性定位用途，不再作次数硬门槛。
        if pn.count(surface) >= 1 and (
            pn.count(nb + surface + na) >= 1
            or (bool(nb) and nb in pn and bool(na) and na in pn)
        ):
            continue  # surface 在场（未被整删）且（精确锚点串存在 或 左右锚点各自在场）→ 合规；
                      # 后者容错 normalize/模型引入的标点/空白漂移，仍守住数值+语境
        violations.append(
            f"锁定数值「{surface}」（锚点 {nb or '∅'}…{na or '∅'}，occurrence {occ}）"
            f"在润色稿对应位置未逐字保留")

    for ev in contract.get("locked_events") or []:
        text = _norm(ev)
        if len(text) < 6:
            continue
        # 2026-09-22 分词口径修正（37 章四次 BLOCK 实测 0/15、0/21、0/16）：
        # 原实现把标点间连续汉字串整体当一个 token，逐字重现概率趋零 → 恒 0 必 BLOCK；
        # 2-gram 修正后仍拦同义改写（看着→盯着/手心→掌心断 gram），而润色的定义
        # 就是同义改写。词面法里唯一同时满足「整删拦截 + 措辞容忍」的口径是
        # 字符集覆盖率：事件唯一字符集在稿中出现的比例。实测分布：微调措辞
        # ~50-70%、同义改写 ~70%、事件整删 ~12-14%。阈值取 40% 留合法润色
        # 余量（整删远低于线，仍稳定拦截）。
        segs = [s for s in re.findall(r"[\u4e00-\u9fa5]{2,}", text) if s not in _POLISH_STOPWORDS]
        chars = set("".join(segs))
        total = len(chars)
        if total < 4:
            continue
        kept = sum(1 for c in chars if c in pn)
        if kept / total < 0.4:
            violations.append(f"锁定事件「{ev[:40]}」内容词缺失过半（{kept}/{total}），事件疑似被删/合并")

    violations.extend(_check_length_collapse(original, polished))
    return violations


def step_polish_rewrite(book: Path, chapter_id: int, original: str, analyze: dict,
                        on_token=None, violations: list[str] | None = None,
                        user_note: str = "") -> str:
    """润色改写 pass：单次 LLM 改写（合同锁定 + 诊断清单），输出 <prose> 成稿。
    violations 非空 = 上轮合同守卫违规反馈，注入后重写收敛（L3 Plan 反馈同款模式）。
    user_note 非空 = 作者本轮编辑指令（2026-11-02：此前润色意见从未进入执行链——
    前端只发 chapter、端点丢 note，链跑的是自动诊断 → 「按意见润色后一字未改」），
    以最高优先级注入；与合同锁冲突时合同锁优先（锁是作者原稿事实，不可违背）。"""
    contract = json.dumps(analyze.get("contract", {}), ensure_ascii=False, indent=1)
    diagnosis = "\n".join(f"- {d}" for d in analyze.get("diagnosis") or []) or "- 无明确问题：仅做整体表达优化"
    fb = ""
    if violations:
        fb = ("\n\n<上一轮违规反馈（本轮必须全部消除）>\n"
              + "\n".join(f"- {v}" for v in violations[:8])
              + "\n上轮改写被合同守卫拦截。本轮改写时：被点名的事件句必须把关键内容词"
                "（人物/动作/物体/结果词）保留大半——保留原句骨架、微调措辞语序，"
                "禁止整句重写、禁止替换核心物体与动作的说法。\n")
    note = ""
    if str(user_note or "").strip():
        note = (
            "\n<author_directive（作者本轮编辑指令 · 最高优先级）>\n"
            f"{str(user_note).strip()}\n"
            "以上指令必须逐条落实、可见生效（改到位，不是意思一下）。唯一例外：与"
            "<contract> 合同锁冲突处（锁定实体/数值/关系端点须逐字保留），以合同锁为准，"
            "其余内容在锁约束内最大化执行指令。\n</author_directive>\n"
        )
    prompt = (
        f"# 第{chapter_id}章润色\n"
        f"<contract>\n{contract}\n</contract>\n\n"
        f"<diagnosis>\n{diagnosis}\n</diagnosis>\n{fb}{note}\n\n"
        f"<original>\n{original}\n</original>"
    )
    # 2026-09-22 去输入截断 + 输出动态上限：原文全量传入（[:9000] 截断会让
    # 超 9000 字章后段事件既不进合同锁也无从保留→静默丢失/恒 BLOCK）；
    # 输出按原文长 ×2.5 token 动态放宽（中文≈1.5-2 token/字），防长章输出截断。
    text, _ = _writer_stream(_POLISH_REWRITE_SYSTEM, prompt, on_token=on_token, temperature=0.4,
                             max_tokens=max(8192, int(len(original) * 2.5)),
                             book=book, chapter_id=chapter_id)
    return text


def run_polish_pipeline(book: Path, chapter_id: int, progress=None, on_writer_token=None, dry_run: bool = False,
                        note: str = "") -> dict:
    """润色已提交章节（文本质量优化层，revision 闭环）。

    写前闸 → 备份 → analyze(诊断+章节合同快照) → rewrite_pass(单次改写) →
    deslop(LLM 去AI味清理) → normalize 落盘 → contract_guard(确定性硬约束, fail-closed) →
    delta → post_write commit → 机械层复扫。
    约定：只作用于「已有正文的已提交章」；正文不存在 → FAIL。
    progress: 可选回调 (name, ok, detail)；on_writer_token: 可选回调 (chunk)（流式预览）。
    note: 作者本轮编辑指令（2026-11-02：此前润色意见在前端/端点两层被丢弃，从未进入
    执行链——「按意见润色后一字未改」事故根因）。非空时以最高优先级注入 rewrite+deslop，
    与合同锁冲突处以合同锁为准。留空 = 自动诊断润色（原行为）。
    """
    book = book.resolve()
    _user_note = str(note or "").strip()
    stages: list[StageResult] = []
    _fatal_stage: dict = {"name": None}  # P0-1（2026-10-30 外脑定稿）：记录最近一次失败环节，供 _fail 真实归因

    def _finish(name: str, ok: bool, detail: str, output: str = "", **extra) -> None:
        stages.append(StageResult(name, ok, detail, **{**_gate_degraded_extra(), **extra}))
        if not ok:
            _fatal_stage["name"] = name
        if progress:
            try:
                progress(name, ok, detail, output=output)  # type: ignore[arg-type]
            except runtime.AgentCancelled:
                raise
            except Exception:  # noqa: BLE001 — 进度上报失败不影响主链
                pass

    def _begin(name: str, detail: str = "开始执行…") -> None:
        """环节开始时通知前端：progress(name, None, detail)。
        ok=None 表示进行中，_on_phase 据此更新 phase 但不追加 live_stage。"""
        if progress:
            try:
                progress(name, None, detail)  # type: ignore[arg-type]
            except runtime.AgentCancelled:
                raise
            except Exception:  # noqa: BLE001
                pass

    # advisory 失败登记（rescan 等）：语义上继续成功，但在最终结果里必须可观测。
    advisory: list[dict] = []

    def _fail(fatal_phase: str | None = None) -> dict:
        out = {"ok": False, "chapter_id": chapter_id, "mode": "polish",
               "advisory_failures": advisory,
               "stages": [s.to_dict() for s in stages]}
        _fp = fatal_phase or _fatal_stage.get("name")  # P0-1：显式传参优先；缺省取最近一次失败环节
        if _fp:
            out["fatal_phase"] = _fp
            out["fatal_phase_confirmed"] = True
        return _settle_fail(book, chapter_id, stages, out, dry_run=dry_run)

    # ★ 事务边界（同重写链，2026-10-03 外脑裁定；2026-10-19 同款移植 polish 链）：
    #   rollback 的定义 =「把本次 polish 产生的全部持久化副作用恢复到 polish 开始之前」。
    #   管理对象：original body / new body / delta / ledger / state_revision /
    #   last_committed_chapter。commit 是唯一推进 state 的动作，故 rollback 只发生在
    #   commit 之前 → state 天然不变；仍保留只读断言 _state_unchanged() 防未来偷偷改状态。
    body_written = {"done": False}
    _tx_delta = book / _TRACKING_DIR / f"{_DELTA_PREFIX}{chapter_id:03d}章.json"
    _tx_ledger = book / _TRACKING_DIR / "pending_commits" / f"第{chapter_id:03d}章.json"
    _tx_snap: dict[str, bytes | None] = {"delta": None, "ledger": None}
    _tx_state_before: dict[str, object] = {"revision": None, "last": None}

    def _rollback_body() -> str:
        """回滚 normalize 落盘的新稿到执行前正文；未落盘（标志 False）不动作。
        返回回滚结果描述（空串=无需回滚）。只做一次（回滚后复位标志）。"""
        if not body_written.get("done"):
            return ""
        try:
            target.write_text(original, encoding="utf-8")
        except Exception:  # noqa: BLE001
            return "回滚失败，请人工从 .backup/ 恢复"
        body_written["done"] = False
        return "已回滚执行前正文"

    def _state_unchanged() -> bool:
        """只读断言：state_revision / last_committed_chapter 是否仍等于 polish 开始前。"""
        try:
            _s = json.loads((book / _TRACKING_DIR / "_tracking-state.json").read_text(encoding="utf-8"))
            return (_s.get("state_revision") == _tx_state_before.get("revision")
                    and _s.get("last_committed_chapter") == _tx_state_before.get("last"))
        except Exception:  # noqa: BLE001 — 读不到视为「无法证明未变」，保守报 False
            return False

    def _rollback_transaction() -> str:
        """本次 polish 的全部持久化副作用回滚：正文 + delta + 台账。
        幂等：polish 前不存在的文件删除；polish 前存在的文件按字节快照复原。
        返回回滚结果描述（空串=无需回滚）。"""
        parts: list[str] = []
        _rb = _rollback_body()
        if _rb:
            parts.append(_rb)
        for _k, _p in (("delta", _tx_delta), ("ledger", _tx_ledger)):
            try:
                _before = _tx_snap.get(_k)
                if _before is None:
                    if _p.is_file():
                        _p.unlink()
                        parts.append(f"已删除本次生成的{_k}")
                elif (not _p.is_file()) or _p.read_bytes() != _before:
                    _p.write_bytes(_before)
                    parts.append(f"已恢复 polish 前的{_k}")
            except Exception:  # noqa: BLE001 — 单项失败不阻断其余，但必须在 detail 里可见
                parts.append(f"{_k}回滚失败，请人工处理")
        if not _state_unchanged():
            parts.append("⚠ state_revision/last_committed 与 polish 前不一致（未回滚，需人工核对）")
        return "；".join(parts)

    def _rollback_on_cancel() -> None:
        """AgentCancelled 出口专用：回滚后仅入 stages（不经 progress 回调，
        避免取消路径再次触发回调异常）。语义=事务回滚（正文 + delta + 台账）。"""
        rb = _rollback_transaction()
        if rb:
            stages.append(StageResult("rollback_on_cancel", True, rb))

    def _advisory_fail(name: str, detail: str) -> None:
        """advisory 失败登记：不改变控制流，只在最终结果里沉淀证据。
        结果 ok=True 只表示「事务最终成功提交」，advisory_failures 非空=存在降级项。"""
        advisory.append({"stage": name, "detail": str(detail or "")[:400]})

    try:
        # 0pre. 写前连续性闸（同重写/deslop 链）：目标章前存在更小悬空章 → 先处理
        _last = _last_committed_chapter(book)
        if _last is not None:
            _pending = _pending_uncommitted_chapter(book, chapter_id, _last)
            if _pending is not None:
                _dangling = _dangling_block_detail(_pending, _last, auto_detail="",
                                                   next_action="润色后续章节")
                if _gate().is_v13():  # type: ignore[attr-defined]
                    _gate_record(book, chapter_id, "0pre/dangling_chapter",
                                 _dangling, "pipeline/polish")
                    _finish("backup", True,
                            f"[V13 道闸] 悬空事实已记录，动作继续：{_dangling[:200]}")
                else:
                    _finish("backup", False, _dangling)
                    return _fail()

        target = _resolve_existing_body(book, chapter_id)
        if target is None:
            _finish("backup", False, f"第{chapter_id}章无已提交正文（或正文已被备份改名）。无法润色不存在的章节。")
            return _fail()
        original = target.read_text(encoding="utf-8")

        # ★ 事务快照（同重写链）：记录 polish 开始前的 delta / 台账字节与状态版本，
        #   供 _rollback_transaction 精确复原（polish 前不存在的删除，存在的按字节复原）。
        for _k, _p in (("delta", _tx_delta), ("ledger", _tx_ledger)):
            try:
                _tx_snap[_k] = _p.read_bytes() if _p.is_file() else None
            except Exception:  # noqa: BLE001 — 快照失败按「polish 前不存在」处理（回滚即删除）
                _tx_snap[_k] = None
        try:
            _s = json.loads((book / _TRACKING_DIR / "_tracking-state.json").read_text(encoding="utf-8"))
            _tx_state_before["revision"] = _s.get("state_revision")
            _tx_state_before["last"] = _s.get("last_committed_chapter")
        except Exception:  # noqa: BLE001 — 读不到则断言恒 False，回滚 detail 会显式告警
            pass

        # 1. 写前备份（幂等：.backup/{章名}.orig.md 首份优先，已有不覆盖）；dry_run 不写正式 .backup/
        if dry_run:
            _rid = f"polish_{uuid.uuid4().hex[:12]}"
            _finish("backup", True, "dry-run：跳过正式 .backup 写入")
        else:
            bk = toolbox.run_script("nww_backup.py", ["--book", str(book), "--file", str(target)], book, timeout=60)
            if not bk["ok"]:
                _finish("backup", False, f"原稿备份失败: {bk['stderr'] or bk['stdout']}")
                return _fail()
            _finish("backup", True, bk["stdout"][:300], path=bk["stdout"].strip())
    except runtime.AgentCancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        _finish("backup", False, f"原稿备份失败: {exc}")
        return _fail()

    try:
        # 2. analyze：诊断 + 章节合同快照（从原文提取硬约束；chapter goal 取自细纲）
        goal = ""
        outline = _find_outline_file(book, chapter_id)
        if outline:
            try:
                txt = outline.read_text(encoding="utf-8")
                m = re.search(r"##\s*(?:本章目标|目标|章目标)(.*?)(?=\n## |\n##\s|\Z)", txt, re.S)
                goal = (m.group(1).strip() if m else txt[:300])[:800]
            except OSError:
                goal = ""
        analyze, _p_att = _polish_analyze_frozen(book, chapter_id, original, goal,
                                                 on_token=None, persist=not dry_run)
        contract = analyze["contract"]
        _finish("analyze", True,
                f"诊断 {len(analyze.get('diagnosis') or [])} 项；合同快照：事件 {len(contract.get('locked_events', []))}、"
                f"实体 {len(contract.get('locked_entities', []))}、关系 {len(contract.get('locked_relations', []))}、"
                f"数值 {len(contract.get('locked_numerics', []))}"
                + _contract_attempt_note(_p_att),
                contract=contract)
    except runtime.AgentCancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        _finish("analyze", False, f"润色分析失败: {exc}")
        return _fail()

    # 3~6. 改写→去AI味→归一→合同守卫（≤2 次尝试，违规反馈重试——2026-09-22 新增：
    #     37 章两次实测 BLOCK（事件句 0/21 全失），根因是 rewrite_pass 单次失败即终判、
    #     无反馈收敛通道。第 1 次守卫 BLOCK → 违规项注入 prompt 第 2 次改写，仍 BLOCK
    #     才回滚终判。同款幂等模式已在 L3 Plan（P8 反馈）验证有效。）
    _feedback: list[str] = []
    for _try in (1, 2):
        try:
            polished = step_polish_rewrite(book, chapter_id, original, analyze,
                                           on_token=on_writer_token, violations=_feedback,
                                           user_note=_user_note)
            _finish("rewrite_pass", True,
                    f"改写完成（第{_try}次{'，带违规反馈' if _feedback else ''}）：{len(original)} → {len(polished)} 字")
        except runtime.AgentCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            _finish("rewrite_pass", False, f"润色改写失败: {exc}")
            return _fail()

        try:
            # 4. deslop：LLM 去AI味清理（nww-deslop 内联衔接步骤，保真优先）；
            #    2026-11-01 传入 contract → 让去AI味步感知锁定清单，避免盲跑毁锁（contract_guard 根因）
            polished = step_polish(book, chapter_id, polished, on_token=on_writer_token,
                                   contract=contract, user_note=_user_note)
            _finish("deslop", True, f"去AI味清理完成：{len(polished)} 字")
        except runtime.AgentCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            _finish("deslop", False, f"去AI味清理失败: {exc}")
            return _fail()

        try:
            # 5. normalize 落盘（旧稿已备份；润色是允许覆盖的点）；dry_run 写 sandbox
            clean, norm_report = step_normalize(book, chapter_id, polished)
            # 2026-11-01 数值确定性还原（合同守卫安全网）：normalize 可能把锚点邻域标点/格式漂掉，
            # 导致守卫「精确锚点串」误判。在落盘文本上还原锁定数值，直接作用于 contract_guard
            # 可见且最终提交的文本；仅当锚点串不在稿中才插回，不改写其它内容。
            clean, _restored = _restore_locked_numerics(clean, contract, original)
            if _restored:
                print(f"[NUMERIC_RESTORE] 从原文还原锁定数值：{_restored}", flush=True)
            if dry_run:
                sb = _dry_run_sandbox(book, _rid)
                target = sb / "body-preview" / f"第{chapter_id:03d}章.md"
            rel = target.relative_to(book).as_posix()
            target.write_text(clean + "\n", encoding="utf-8")
            if not dry_run:
                body_written["done"] = True
            # 模式C（2026-10-25）：polish 链（polish_rewrite 后续 deslop 通道）advisory 闸
            _adv = _advisory_gates_report(book, chapter_id, clean)
            _finish("normalize", True, f"{'dry-run 正文预览' if dry_run else '覆盖落盘'}: {rel}（{len(clean)} 字；{norm_report}）"
                    + ("；⚠ advisory 闸：" + "；".join(_adv) if _adv else ""),
                    path=rel, chars=len(clean), advisory_gates=_adv)
        except runtime.AgentCancelled:
            _rollback_on_cancel()
            raise
        except Exception as exc:  # noqa: BLE001
            _rb = _rollback_transaction()
            _finish("normalize", False, f"正文归一/落盘失败（事务回滚）: {exc}" + (f"；{_rb}" if _rb else ""))
            return _fail()

        try:
            # 6. contract_guard：确定性合同守卫（fail-closed）。越界 → 回滚原稿并 BLOCK，
            #    绝不带着被污染的正文进入 commit。
            # 2026-11-01：塌缩复检同口径并入（读盘上正文，_collapse_after_normalize 0.8 线，
            #   与 deslop 链同一闸）：normalize 的 prose 提取/元信息清洗可能把 2124 字压成
            #   932 字（第2条事故同款第二刀）；合同词锁拦的是「事件被删」，拦不住整章塌缩。
            #   violations 非空 → 走既有重试/回滚通道，零新控制流。
            final_text = target.read_text(encoding="utf-8")
            violations = _validate_polish_contract(original, final_text, contract)
            violations += _collapse_after_normalize(original, target)
            if not violations:
                _finish("contract_guard", True,
                        f"合同守卫通过（第{_try}次尝试，事件/实体/关系/数值全部锁定）")
                break
            if _try == 1:
                _finish("contract_guard", False,
                        f"合同守卫 BLOCK（{len(violations)} 项，违规注入重试 1 次）：" + "；".join(violations[:3]))
                _feedback = list(violations)
                # ★ 两次尝试循环间 body_written 不复位（2026-10-19 外脑裁定）：第 1 次尝试的
                #   新稿仍在盘上，第 2 次尝试还会再覆盖一次；标志必须持续为 True，否则
                #   循环间取消/异常会漏回滚（残留第 1 次尝试的稿）。
                continue
            try:
                target.write_text(original, encoding="utf-8")
                rollback = "已回滚原稿"
                body_written["done"] = False  # 原稿已复原到盘上，复位标志防重复回滚
            except Exception:  # noqa: BLE001
                rollback = "回滚失败，请人工从 .backup/ 恢复原稿"
            _finish("contract_guard", False,
                    f"合同守卫 BLOCK（{len(violations)} 项，重试后仍越界）：" + "；".join(violations[:3]) + f"。{rollback}")
            return _fail()
        except runtime.AgentCancelled:
            # ★ 真 bug 修复（2026-10-19）：此前裸 raise —— normalize 新稿已落盘，取消后
            #   残留「新稿 + 无 delta + 无台账」半恢复态。统一事务回滚。
            _rollback_on_cancel()
            raise
        except Exception as exc:  # noqa: BLE001
            # ★ 真 bug 修复（2026-10-19）：守卫异常时新稿已在盘上，裸 _fail 会留半恢复态
            #   （同重写链 L1/L2 守卫异常出口的 _rollback 语义，升级为事务回滚）。
            _rb = _rollback_transaction()
            _finish("contract_guard", False, f"合同守卫异常（事务回滚）: {exc}" + (f"；{_rb}" if _rb else ""))
            return _fail()

    # 6.5 段落复检（2026-09-22 新增，同重写链 4.6/4.7 口径）：润色改写后的行文格式
    #     与重写链同一标准——最长段≤120 拆段 + 对话独立成段。幂等，advisory 不阻断。
    try:
        _p120p = _enforce_para_max120(target)
        _dlgp = _enforce_dialogue_split(target)
        parts = []
        if _p120p:
            parts.append("长段拆: " + str(len(_p120p)) + "处")
        if _dlgp:
            parts.append("对话拆: " + str(len(_dlgp)) + "处")
        _finish("paragraph_recheck", True, "；".join(parts) if parts else "干净（最长段≤120，对话独立成段）")
    except runtime.AgentCancelled:
        _rollback_on_cancel()
        raise
    except Exception as exc:  # noqa: BLE001
        _finish("paragraph_recheck", False, f"段落复检异常（继续）: {exc}")

    try:
        # 7. delta 草稿（对已提交章自动 revision 模式）+ 语义补全（post_write commit 消费）
        draft_path = step_delta_draft(book, chapter_id,
                                      sandbox_dir=(sb / "delta-preview") if dry_run else None,
                                      preserve_quality_meta=not dry_run)
        _finish("delta_draft", True, f"草稿: {draft_path.relative_to(book).as_posix()}")
        step_delta_semantic(book, chapter_id, draft_path, target)
        _finish("delta_semantic", True, "语义字段已补全")
    except runtime.AgentCancelled:
        _rollback_on_cancel()
        raise
    except Exception as exc:  # noqa: BLE001
        # 同重写链 ①②：delta 可能已部分落盘，只回滚正文会留「半恢复态」→ 事务回滚。
        _rb = _rollback_transaction()
        _finish("delta_semantic", False, f"delta 处理失败（事务回滚）: {exc}" + (f"；{_rb}" if _rb else ""))
        return _fail()

    # 8. post_write（BLOCK 点，revision 提交）
    if dry_run:
        # P3（外脑 2026-10-07 裁定）：delta 已由 step_delta_draft(sandbox_dir=...) 直写 sandbox，
        # 不得再从正式 追踪/ move（会搬走/误动正式 delta）。
        _finish("dry_run", True, "polish dry-run：正文/delta 已入 sandbox，正式目录零变化、未 commit")
        return {"ok": True, "chapter_id": chapter_id, "mode": "polish", "dry_run": True,
                "execution_id": _rid, "commit_status": "SKIPPED_DRY_RUN",
                "sandbox": sb.relative_to(book).as_posix(),
                "stages": [s_.to_dict() for s_ in stages]}

    # 7.5 一致性检测 + 清零自动闭环（2026-11-04）：
    #   polish 链此前无一致性环节，直接 post_write → A-3 缺闭环记录硬 BLOCK（fail_closed=true）。
    #   润色只改表达、不改剧情/人物/事件/数值，理论上不引入新 S1/S2，但「零检测直接提交」
    #   会撞 A-3。此处补只读检测（不改正文），清零则自动落闭环；检出硬伤 → 自动闭环拒绝
    #   落盘 → A-3 兜底 BLOCK（守住「不装假闭环」）。非 dry_run（正式 commit 路径）才跑。
    try:
        _cons = step_consistency_check(book, chapter_id, target)
        if _cons.get("ok"):
            _closure = step_consistency_closure(book, chapter_id, _cons)
            if _closure.get("closed"):
                _finish("consistency", True,
                        f"审查记录: {_cons.get('record_path', '')}"
                        f"（S1/S2={_cons.get('open_s1_s2_count', 0)}，S3/S4={_cons.get('s3_s4_count', 0)}）"
                        f"；自动闭环已落盘（{_closure.get('closure_path', '')}）",
                        consistency=_cons, closure=_closure)
            else:
                _finish("consistency", False,
                        f"一致性未闭环（S1/S2={_cons.get('open_s1_s2_count', 0)}，未落闭环，A-3 将 BLOCK）",
                        consistency=_cons, closure=_closure)
        else:
            _finish("consistency", False, f"一致性检测未完成（不落闭环）: {_cons.get('detail', '')}")
    except runtime.AgentCancelled:
        _rollback_on_cancel()
        raise
    except Exception as exc:  # noqa: BLE001 — 检测异常不阻断润色主链；未闭环时 A-3 兜底
        _advisory_fail("consistency", f"一致性检测异常: {exc}")
        _finish("consistency", False, f"一致性检测异常（继续，A-3 兜底）: {exc}")

    try:
        post = step_post_write(book, chapter_id, target)
    except runtime.AgentCancelled:
        _rollback_on_cancel()
        raise
    except Exception as exc:  # noqa: BLE001
        # 同重写链：post_write 异常可能发生在 commit **之后**（enforce → gate → commit →
        #   cross_chapter 串跑）。state 未推进 → 事务回滚；已推进 → 落台账，绝不回滚正文。
        if _state_unchanged():
            _rb = _rollback_transaction()
            _finish("post_write", False, f"post_write 异常（事务回滚）: {exc}" + (f"；{_rb}" if _rb else ""))
        else:
            _write_pending_commit_ledger(
                book, chapter_id, f"post_write 异常且状态已推进（不可回滚）:\n{exc}")
            _finish("post_write", False,
                    f"post_write 异常（state 已推进，落 RECOVERY_PENDING 台账，未回滚正文）: {exc}")
        return _fail()
    _finish("post_write", post["ok"],
            (post["stdout"] + ("\n" + post["stderr"] if post["stderr"] else ""))[:2400],
            returncode=post["returncode"], mode="revision")
    if not post["ok"]:
        # 2026-11-01 修复（同 deslop 链）：BLOCK 且 state 未推进 → 事务回滚，不留「已改未提交」
        #   的悬空正文；已推进 → 落台账、绝不回滚正文。
        if _state_unchanged():
            _rb = _rollback_transaction()
            stages.append(StageResult("rollback", True, _rb or "无需回滚"))
            return _fail()
        _write_pending_commit_ledger(
            book, chapter_id,
            f"post_write BLOCK (polish):\n{post['stdout']}\n{post['stderr']}")
        return _fail()

    # commit 已成功 → 指纹重绑最终正文（post_write 内 enforce 会再改正文，见 deslop 链注释）
    _rebind_body_quality_meta(draft_path, target)

    # 9. 机械层复扫（advisory：残留只报不阻断）
    try:
        reports = step_deslop(book, chapter_id, target, check_only=True)
        _finish("rescan", True, f"复扫脚本数: {len(reports)}", reports=reports)
    except runtime.AgentCancelled:
        raise  # commit 已成功，取消不回滚（同重写链 commit 后语义）
    except Exception as exc:  # noqa: BLE001
        # commit 已成功的后置复扫异常：降级 advisory，不影响已提交结果。
        _advisory_fail("rescan", f"复扫异常: {exc}")
        _finish("rescan", False, f"复扫异常（advisory，不影响已提交结果）: {exc}")

    _clear_pending_commit_ledger(book, chapter_id)
    return {"ok": True, "chapter_id": chapter_id, "mode": "polish",
            # ★ 语义边界（同重写链）：ok=True 表示「事务最终成功提交」，
            #   **不**表示「所有质量检查均通过」。advisory_failures 非空 = 存在降级项。
            "advisory_failures": advisory,
            "stages": [s.to_dict() for s in stages]}


# ---------------------------------------------------------------------------
# L1 保持剧情重写的合同提取 + 合同守卫（2026-09-20 用户裁决）：
#   重写链 L1 缺「剧情保持硬闸门」——第35章真实反证（E5/E6 完成态丢失 + S1 场景重复仍 commit）。
#   L1 在 writer 前从原文确定性提取章节合同（含事件完成态词元），normalize 落盘后
#   由 contract_guard 确定性校验（fail-closed）：区分「事件存在」与「事件完成」——
#   完成态词元缺失即事件未完成 → BLOCK → 回滚 L1 执行前正文 → 不 commit/不 revision/不 delta。
#   与 polish 链的 _polish_analyze_contract/_validate_polish_contract 同思路，但
#   core_events 增加 presence/completion 双层词元（只保护「名字出现」已被证伪）。
# ---------------------------------------------------------------------------
# 原文「结果/收尾句」强完成词（确定性提取，不依赖 LLM 事件切分）：
#   含这些词的原文句承载事件完成态（接单/认账/交付/离开/拟稿收尾），Guard 逐句校验保留度。
_REWRITE_RESULT_SENT_RE = re.compile(
    r"(我认|全接|递上|派弟子|目送|掀帘|散进|转身往外走|开了价|定音|落槽|收进抽屉|玉牌|车轮声碾过|撞入)"
)
# 结果句「硬词」：事件完成态的核心标识，必须逐字保留（fail-closed，2026-09-20）。
#   覆盖 2-gram 校验的盲区：单点反转（「我认」→「我不认」2-gram 覆盖 86% 不降）与
#   完成态半句删除（保留前半句后覆盖仍 53%）。硬词丢失即 BLOCK；合规表达重写若替换
#   硬词（如「我认」→「我应了」）会误 BLOCK——按用户裁决「canon 守卫 fail closed」接受，
#   宁可作者微调重跑，不可放行污染。
_REWRITE_RESULT_HARD_RE = re.compile(
    r"(我认|全接|目送|散进|落槽|定音|派弟子|转身往外走)"
)


_REWRITE_ANALYZE_SYSTEM = (
    "你是章节合同提取师，为「保持剧情重写（L1）」从原文提取不可变更的章节合同。\n"
    "【输入】<original> 本章正文、<goal> 本章目标（来自细纲，仅参考）。\n"
    "【输出】只输出一个 JSON 对象（前后不写任何说明/围栏）：\n"
    "{\n"
    '  "chapter_goal": "章节目标短语（优先取 <goal>，缺失则从原文归纳）",\n'
    '  "core_events": [\n'
    '    {"id": "E1", "text": "事件一句话（从原文逐字/近原文摘录，含主体与关键动作）",\n'
    '     "presence": ["事件存在的关键名词/动词（2-5个，从原文逐字照抄）"],\n'
    '     "completion": ["事件完成态表达词（2-4个，从原文逐字照抄：完成动词/结果短语/收束表达，如 接受、同意、离开了、成交）"]},\n'
    "    ...\n"
    "  ],\n"
    '  "entities": ["本章必须保留出场的角色/势力/物品名（从原文逐字照抄）"],\n'
    '  "relationships": ["角色-角色-关系（如 林小凡-温故-朋友）"],\n'
    '  "key_values": [{"context": "数值所在短句（逐字摘录，不含数值本身）", "value": "数值"}],\n'
    "}\n"
    "【规则】\n"
    "1. core_events 只提取影响剧情走向的关键事件（同一场景的琐碎动作合并为一条），\n"
    "   最多 16 条——长章按场景粒度取舍，确保关键事件不漏但不逐段枚举（★2026-10-09：\n"
    "   此前「每段一个关键动作」在 230 段长章上产出 40-60 事件，输出超 8192 tokens 被\n"
    "   max_tokens 截断（FINISH_REASON_LENGTH），rewrite L1 必现失败）；\n"
    "2. completion 是「事件完成时正文里必须出现的词」，不是事件名本身：\n"
    "   必须包含结果性动词/收束表达（接受/答应/离开/给出/成交/点头/交付…），\n"
    "   禁止只放名词（如只放 陈执事/样品）、禁止放动作起始词（登门/提出/要求/询问/坐下）——\n"
    "   那只能证明事件存在/开始，不能证明事件完成；\n"
    "3. 所有词元必须从原文逐字照抄，不得发明、不得改写；\n"
    "4. 谈判收尾/离开类事件（如 E5/E6）必须给出 completion（如 接受、离开了）。"
)



def _norm_all(s: str) -> str:
    """去全部空白（合同词元/原文匹配用）。"""
    return re.sub(r"\s+", "", str(s or ""))



def _align_event_sentences(ev_text: str, presence: list, completion: list,
                         original_sents_norm: list, original_sents: list) -> list:
    """把事件语义对齐到原文句子（句级 2-gram 覆盖），返回原文逐字句列表。

    设计动机（2026-09-20 真实运行迭代）：原文完成态常为隐性表达（「我认」「目送他掀帘出去」
    = 接受/离开），LLM 概括的事件 text 与原文段落的字面 2-gram 重叠有限，窗口对齐不可靠。
    改为句级对齐：事件语义与原文逐句算 2-gram 覆盖，取覆盖最高的 1-2 句（原文逐字）；
    覆盖过低（<0.15，隐性表达语义鸿沟）时降级为「含 presence 词元的原文句」；
    仍无 → 空（该事件只靠实体/数值全局层保护）。Guard 校验这些句在重写稿中的保留度
    （2-gram 覆盖），完成态表达内化在句子里：句子被删 = 完成态必丢 → BLOCK。
    边界说明：这是「删除型」保护（事件段被删/合并）；「加法型」破坏（结尾循环重置重复段）
    由 consistency S1 检出（advisory），与本守卫职责分离（用户裁决：Guard=硬闸门，consistency=检测）。
    """
    def _norm(s: str) -> str:
        return re.sub(r"\s+", "", str(s or ""))

    t = _norm(ev_text)
    tb = {t[i:i + 2] for i in range(len(t) - 1)} if len(t) >= 2 else set()
    tb |= {t[i:i + 3] for i in range(len(t) - 2)} if len(t) >= 3 else set()
    pres_norm = [_norm(p) for p in (presence or []) if len(_norm(p)) >= 2]

    # 1) 完成态词元定位（最高优先）：completion 词元（净化后逐字在原文）所在句
    #    —— 如 E4 completion=['认'] → 锁「五成定金，月结，我认」；
    #       E6 completion=['目送','散进'] → 锁「…目送他掀帘出去，直到车轮声碾过石板，渐渐散进巷子深处」。
    #    直接锁定「事件完成态」的原文表达，Guard 校验该句保留度 → 完成态句被删/改 → BLOCK。
    comp_norm = [_norm(p) for p in (completion or []) if len(_norm(p)) >= 2]
    if comp_norm:
        hits = []
        for p in comp_norm:
            for i, sn in enumerate(original_sents_norm):
                if p in sn and original_sents[i] not in hits:
                    hits.append(original_sents[i])
                    break
            if len(hits) >= 2:
                break
        if hits:
            return hits[:2]
    scored = []
    for i, sn in enumerate(original_sents_norm):
        if len(sn) < 4:
            continue
        sb = {sn[j:j + 2] for j in range(len(sn) - 1)}
        sb |= {sn[j:j + 3] for j in range(len(sn) - 2)} if len(sn) >= 3 else set()
        inter = len(tb & sb)
        cov = inter / len(tb) if tb else 0.0
        if cov >= 0.10:
            scored.append((cov, i, inter))
    scored.sort(key=lambda x: (-x[0], x[1]))
    if scored:
        return [original_sents[i] for _, i, _ in scored[:2]]
    # 降级：含 presence 的原文句，按「与事件 tb 的交集数」排序取前 2（不用出现顺序）
    if pres_norm:
        hit = []
        for i, sn in enumerate(original_sents_norm):
            if any(p in sn for p in pres_norm):
                sb = {sn[j:j + 2] for j in range(len(sn) - 1)}
                hit.append((len(tb & sb), i))
        hit.sort(key=lambda x: (-x[0], x[1]))
        if hit:
            return [original_sents[i] for _, i in hit[:2]]
    return []


def _dump_llm_raw(book: Path, tag: str, raw: str, cleaned: str = "") -> str:
    """LLM 原始输出落盘（仅失败路径调用，2026-10-02 新增）。

    动机：L3 Plan 自 2026-09-22 起每次调用都落 raw（含失败），而 L1/L2 合同提取
    **一次都没落** —— 失败时无从判断「通道坏了（空返回）」还是「模型不听话（胡写）」，
    两者处置相反（换通道 vs 调提示词）。落 {book}/追踪/audit/{tag}_{ts}.txt。
    返回落盘路径；落盘失败返回空串（绝不因诊断动作影响主链）。
    """
    try:
        import time as _t
        p = book / "追踪" / "audit" / f"{tag}_{int(_t.time())}.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"RAW (len={len(raw)}):\n{raw}\n\nCLEANED (len={len(cleaned)}):\n{cleaned}",
                     encoding="utf-8")
        return str(p)
    except Exception:  # noqa: BLE001 — 诊断动作不影响主链
        return ""


def _record_llm_stream_observation(*, book: Path, chapter_id: int, endpoint: str,
                                   model: str, attempt: int, status_code,
                                   raw_error: str) -> None:
    """流式 LLM 尝试级旁路观测落盘（2026-10-07 P0；纯旁路，零行为耦合）。

    动机：第002章 polish 事故中任何持久化载体都不含 endpoint/model/attempt，失败只剩
    一行异常文本。本函数把**已发生异常**的那次 attempt 落 {book}/追踪/audit/llm_stream_{ts}.json。
    约束（外脑裁定）：绝不 raise、不改传入对象、不触碰任何业务/控制流；异常全吞掉。
    """
    try:
        payload = {
            "event": "llm_stream_error",
            "book_id": book.name,
            "chapter_id": chapter_id,
            "endpoint": endpoint,
            "model": model,
            "attempt": attempt,
            "status_code": status_code,
            "raw_error": (raw_error or "")[:500],
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        p = (book / _TRACKING_DIR / "audit"
             / f"llm_stream_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.json")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001 — 观测落盘失败绝不影响主链
        pass


def _rewrite_analyze_contract(book: Path, chapter_id: int, original: str, goal: str,
                              on_token=None, level: str = "L1") -> dict:
    """L1/L2 合同提取：一次 LLM 调用产出 {contract}。

    合同从原文提取（近逐字），含 core_events（presence=事件存在词元 /
    completion=事件完成态词元，区分「存在」与「完成」）、entities、relationships、
    key_values、chapter_goal。解析失败 → ValueError（调用方按失败处理，原稿未动）。

    level（2026-10-02）：调用方重写等级，仅用于错误文案消歧——同一函数被 L1/L2 复用，
    此前错误信息硬编码「L1」，跑 L2 时报「L1 合同提取…」造成误判。
    """
    prompt = (
        f"# 第{chapter_id}章 {level} 合同提取\n"
        f"<goal>\n{goal[:500]}\n</goal>\n\n"
        f"<original>\n{original}\n</original>"  # 2026-09-22 去截断：合同提取必须看到全文，超长章后段事件同样需要入锁
    )
    # 2026-10-31 路由切换（外脑批准）：合同提取是结构化提取任务，要求词元保真，
    #   路由到 nww-contract-extractor 专用通道（2026-11-01 键名与 agent_routes 对齐，
    #   此前请求名 contract-extractor 永不命中配置，一直走内置默认），不再绑死网页桥——
    #   桥接 agent 协议污染（回"零执行报告"）曾致本环节连续失败判死整链。
    raw, _fin = _writer_stream(_REWRITE_ANALYZE_SYSTEM, prompt, on_token=on_token, temperature=0.2, book=book, chapter_id=chapter_id, agent_name="nww-contract-extractor")
    # 2026-10-02 接入完整性闸门（对齐 step_writer / L3 Plan）：此前合同提取**丢弃 finish_reason**，
    #   输出被 max_tokens 截断时只能等 json.loads 失败报「JSON 解析失败」（指向提示词/服从度），
    #   真实原因（截断）被掩盖；空输出同理会被误诊为「模型不按格式输出」，实为通道故障。
    #   两类根因处置相反（换通道 vs 调提示词），必须先分型。
    _oc = check_output_completeness(raw, _fin)
    if _oc["status"] == "BLOCK":
        _hint = ("（疑似通道故障：模型端点未就绪/网页桥接未生成，不是格式问题）"
                 if _oc["reason"] == "EMPTY_OUTPUT" else "")
        raise ValueError(
            f"{level} 合同提取：输出不完整（{_oc['reason']}）{_hint} — {_oc['evidence']}")
    cleaned = raw.strip()
    i, j = cleaned.find("{"), cleaned.rfind("}")
    if i < 0 or j <= i:
        _diag = _dump_llm_raw(book, f"{level}_contract_raw", raw, cleaned)
        _tail = f"（raw={_diag}）" if _diag else ""
        if len(cleaned) <= 60:
            raise ValueError(
                f"{level} 合同提取未输出 JSON 对象（上游仅返回 {len(cleaned)} 字符，疑似通道故障）"
                f": {cleaned[:60]!r}{_tail}")
        raise ValueError(
            f"{level} 合同提取未输出 JSON 对象（上游返回 {len(cleaned)} 字符但无可解析 JSON 块）{_tail}")
    try:
        data = json.loads(cleaned[i:j + 1])
    except json.JSONDecodeError as exc:
        _diag = _dump_llm_raw(book, f"{level}_contract_raw", raw, cleaned)
        raise ValueError(f"{level} 合同提取 JSON 解析失败: {exc}"
                         + (f" (raw={_diag})" if _diag else "")) from exc
    contract = data if isinstance(data, dict) else {}
    contract.setdefault("chapter_goal", "")
    for key in ("core_events", "entities", "relationships", "key_values", "global_result_sents"):
        v = contract.get(key)
        if not isinstance(v, list):
            contract[key] = []
    # 原文按句切分（句级对齐用；标点分句保留原文逐字）
    _sents = [s.strip() for s in re.split(r"[。！？!?…\n]+", original) if s.strip()]
    _sents_norm = [_norm_all(s) for s in _sents]
    # 全局结果句（确定性完成态保护）：含强完成词的原文句，Guard 逐句校验保留度
    contract["global_result_sents"] = [s for s in _sents if _REWRITE_RESULT_SENT_RE.search(s)]
    # 确定性净化（2026-09-20，真实运行发现）：LLM 提取的 presence/completion 存在幻觉词元
    #   （不在原文却出现在合同里，如"提出/拒绝/同意"）→ 原文 vs 原文也会假 BLOCK。
    #   规则：所有词元必须逐字存在于原文（_norm 去空白后子串匹配），幻觉词元一律丢弃；
    #   实体名/关系端点/key_values 的 value 同样净化（不在原文=幻觉，无约束力）。
    norm_orig = _norm_all(original)

    def _purge(items):
        out = []
        for it in items or []:
            s = str(it).strip()
            ns = _norm_all(s)
            if ns and ns in norm_orig and ns not in out:
                out.append(s)
        return out

    contract["entities"] = _purge(contract.get("entities"))
    rels = []
    for r_ in contract.get("relationships") or []:
        parts = [x.strip() for x in re.split(r"[-–—]", str(r_)) if x.strip()]
        keep = [p for p in parts if len(_norm_all(p)) >= 2 and _norm_all(p) in norm_orig]
        if len(keep) >= 2:
            rels.append(keep[0] + "-" + keep[1] + "-" + str(r_).split("-")[-1] if "-" in str(r_) else str(r_))
    contract["relationships"] = rels
    kvs = []
    for num in contract.get("key_values") or []:
        ctx = str(num.get("context") or "").strip()
        val = str(num.get("value") or "").strip()
        nv = _norm_all(val)
        if len(_norm_all(ctx)) >= 4 and nv and nv in norm_orig:
            kvs.append({"context": ctx, "value": val})
    contract["key_values"] = kvs

    kept_events = []
    for idx, ev in enumerate(contract["core_events"]):
        if not isinstance(ev, dict):
            continue
        ev.setdefault("id", f"E{idx + 1}")
        ev.setdefault("text", "")
        ev["presence"] = _purge(ev.get("presence"))
        ev["completion"] = _purge(ev.get("completion"))
        # 事件句对齐：locked_sentences = 原文中事件所在句（逐字，含完成态隐性表达）。
        #   completion 词元并入 presence 参与定位，确保完成态句（如「我全接」「我认」）被优先锁定。
        ev["locked_sentences"] = _align_event_sentences(
            ev.get("text") or "", ev.get("presence"), ev.get("completion"), _sents_norm, _sents)
        if ev.get("text") and (ev["presence"] or ev["completion"] or ev["locked_sentences"]):
            kept_events.append(ev)
    contract["core_events"] = kept_events
    if not contract["core_events"]:
        raise ValueError(f"{level} 合同 core_events 经净化后为空（LLM 提取全为幻觉词元），请重试")
    return {"contract": contract}


_CONTRACT_FREEZE_DIR = "合同"            # 追踪/合同/第NNN章_{level}.json
_CONTRACT_FREEZE_HISTORY = "历史"        # 正文变更后旧冻结移入 追踪/合同/历史/（不删审计链）
_CONTRACT_FREEZE_SCHEMA = 1              # 合同结构/净化规则变化 → 递增，旧冻结自动失效


def _frozen_contract_path(book: Path, chapter_id: int, level: str) -> Path:
    """冻结合同落盘路径（本书唯一真源，读取/写入都走本 helper）。"""
    return book / _TRACKING_DIR / _CONTRACT_FREEZE_DIR / f"第{chapter_id:03d}章_{level}.json"


def _contract_source_hash(original: str, goal: str, level: str) -> str:
    """冻结键 = 正文（去空白）+ 目标 + 等级 + 结构版本。

    只认「正文本身」：同正文必得同合同（本轮修的就是这一点），正文一改（重写提交后）
    自动失效并重提。目标/等级入键：同一正文换目标或换重写等级是不同合同，不能互相顶替。
    """
    h = hashlib.sha256()
    for part in (_norm_all(original), goal, level, str(_CONTRACT_FREEZE_SCHEMA)):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _frozen_contract_usable(c: dict) -> bool:
    """冻结复用前提：合同必须至少锁住一条事件，否则是哑锁。

    L1/L2 合同用 `core_events`，润色合同用 `locked_events`；两者都空 = 该闸门退化为
    「永远放行」。空/坏合同绝不冻结复用，否则会变成「永远 BLOCK 且无法自愈」的死锁。
    """
    if not isinstance(c, dict):
        return False
    return bool(c.get("core_events") or c.get("locked_events") or [])


def _load_frozen_contract(book: Path, chapter_id: int, level: str,
                          source_hash: str) -> tuple[dict, dict] | None:
    """读冻结合同；不匹配/损坏/空合同一律返回 None（重提，不静默用坏合同）。"""
    p = _frozen_contract_path(book, chapter_id, level)
    if not p.is_file():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — 坏文件按「无冻结」处理，不阻断
        return None
    if not isinstance(d, dict):
        return None
    if d.get("schema") != _CONTRACT_FREEZE_SCHEMA or d.get("source_hash") != source_hash:
        return None
    c = d.get("contract")
    if not _frozen_contract_usable(c):
        return None
    return c, d


def _save_frozen_contract(book: Path, chapter_id: int, level: str, source_hash: str,
                          contract: dict, goal: str, extra: dict | None = None) -> Path | None:
    """原子落盘冻结合同。正文已变的旧冻结移入 历史/（保留审计链，不删除）。

    extra：随合同一起冻结的附加产物（如 polish 链的 diagnosis），复用时可原样取回。
    落盘失败只打日志、返回 None —— 冻结是「确定性增强」，不是主链依赖。
    """
    p = _frozen_contract_path(book, chapter_id, level)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.is_file():
            try:
                old = json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                old = {}
            if isinstance(old, dict) and old.get("source_hash") != source_hash:
                hist = p.parent / _CONTRACT_FREEZE_HISTORY
                hist.mkdir(parents=True, exist_ok=True)
                os.replace(p, hist / f"{p.stem}_{time.strftime('%Y%m%d_%H%M%S')}.json")
        payload = {
            "schema": _CONTRACT_FREEZE_SCHEMA,
            "chapter": chapter_id,
            "level": level,
            "source_hash": source_hash,
            "goal": str(goal or "")[:800],
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "contract": contract,
        }
        if extra:
            payload.update({k: v for k, v in extra.items() if k not in payload})
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)
        return p
    except Exception as exc:  # noqa: BLE001 — 冻结落盘失败不影响本次重写
        print(f"[CONTRACT_FREEZE] ch{chapter_id} {level} 冻结落盘失败: {exc}", flush=True)
        return None


def _contract_attempt_note(attempt_no: int) -> str:
    """analyze stage 明细后缀：0=复用冻结，1=首次提取，>1=重试成功。"""
    if attempt_no == 0:
        return "（复用已冻结合同）"
    if attempt_no > 1:
        return f"（第{attempt_no}次提取成功）"
    return ""


def _extract_contract_with_retry(book: Path, chapter_id: int, original: str, level: str,
                                  on_token=None, attempts: int = 3,
                                  persist: bool = True) -> tuple[dict, int]:
    """L1/L2 合同提取（带内部重试），返回 (contract, 第几次尝试成功)。

    2026-10-02：此前 L1/L2 只调用一次，通道瞬时故障（网页桥接空返回 / 输出被截断）
    直接整链 _fail()；与 L3 plan 的 3 次重试先例对齐（见 run_rewrite_pipeline 内 L3 分支）。
    设计取舍：
      - 第 1 次照常向前端推 token，重试静默（on_token=None），避免预览区重复堆叠文本；
      - 中间失败只 print 入日志，不进 stages（前端按 stage.ok 判定，避免误标红）；
      - AgentCancelled 透传不重试（用户主动取消即停）。

    2026-11-01 合同冻结（用户裁决，修「同一正文每次跑出不同合同」）：
      同一正文多次重写实测合同剧烈漂移（第1章回执：事件 7/9/12/13/14/16/20，词元 45~102），
      而合同同时驱动「写」与「查」两侧 —— 合同一变，闸门松紧跟着变，同一份稿这次过、
      下次不过，用户感知就是「一直报错、不断有新阻断」，且失败无法复现。
      现在按 `_contract_source_hash`（正文+目标+等级+结构版本）冻结到 追踪/合同/，
      同正文重跑直接复用（返回 attempt_no=0），正文一改自动失效重提。
      冻结是「确定性」而非「放宽」：合同词元已由 _purge 保证逐字存在于原文，
      冻住的是一份可审计、可复现的标准；确实要重提时置 NWW_CONTRACT_REFRESH=1。
      persist=False（dry_run 预览）只读冻结、不落盘，不污染正式目录。
    """
    goal = ""
    _ol = _find_outline_file(book, chapter_id)
    if _ol:
        try:
            _txt = _ol.read_text(encoding="utf-8")
            _m = re.search(r"##\s*(?:本章目标|目标|章目标)(.*?)(?=\n## |\n##\s|\Z)", _txt, re.S)
            goal = (_m.group(1).strip() if _m else _txt[:300])[:800]
        except OSError:
            goal = ""
    _src_hash = _contract_source_hash(original, goal, level)
    _refresh = str(os.environ.get("NWW_CONTRACT_REFRESH", "")).strip().lower() not in ("", "0", "false", "no")
    if not _refresh:
        _frozen = _load_frozen_contract(book, chapter_id, level, _src_hash)
        if _frozen is not None:
            _c, _meta = _frozen
            print(f"[CONTRACT_FREEZE] ch{chapter_id} {level} 复用冻结合同"
                  f"（{_meta.get('created_at') or '未知时间'}，hash {_src_hash[:8]}，"
                  f"事件 {len(_c.get('core_events') or [])}）", flush=True)
            return _c, 0
    last: Exception | None = None
    _t0 = time.monotonic()
    for att in range(1, attempts + 1):
        try:
            an = _rewrite_analyze_contract(book, chapter_id, original, goal,
                                           on_token=on_token if att == 1 else None,
                                           level=level)
            if persist:
                _p = _save_frozen_contract(book, chapter_id, level, _src_hash, an["contract"], goal)
                if _p is not None:
                    print(f"[CONTRACT_FREEZE] ch{chapter_id} {level} 已冻结 → "
                          f"{_p.relative_to(book).as_posix()}（hash {_src_hash[:8]}）", flush=True)
            return an["contract"], att
        except runtime.AgentCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            last = exc
            if att < attempts:
                # 2026-11-01 预算闸：重试前判定（首轮不拦），超预算抛明确错误终止该环节
                _analyze_retry_guard(_t0, f"{level} 合同提取", att, exc)
                print(f"[CONTRACT_RETRY] ch{chapter_id} {level} 第{att}次失败: {exc}；重试中",
                      flush=True)
                time.sleep(1.5 * att)
    raise last  # type: ignore[misc] — attempts >= 1，循环必赋值


def _validate_rewrite_contract(original: str, rewritten: str, contract: dict,
                               level: str = "L1") -> list[str]:
    """重写合同守卫（确定性，fail-closed）：L1/L2 共用，按 level 差异化阈值。

    两层校验：
      1. 事件存在层：presence 词元全在（事件主体被删 → BLOCK，L1/L2 同严）；
         entities 存在、relationships 端点存在、key_values 数值一致（与 polish 守卫同规则）。
      2. 事件完成层：
         - L1（保持剧情重写）：completion 词元**逐字全保留**；locked_sentences 2-gram≥0.5。
           表达改写极小，硬词丢失 = 结果反转/句删除 → BLOCK。
         - L2（结构重写，2026-10-31 实测事故修复）：completion 词元**全缺才 BLOCK**
           （≥1 个出现即视为事件完成态存在——L2 本来就改写句子，「撕开包装」→「撕开封口」
           语义等价，逐字要求把 LLM 锁死在「照抄原文」死路，与 zero_change 检查互斥）；
           locked_sentences 2-gram 阈值 0.5 → 0.3（整段删除仍≈0 被拦，合理改写放行）。
    不做语义判断：表达/节奏/文笔变化不属于硬约束。
    """
    def _norm(s: str) -> str:
        return re.sub(r"\s+", "", str(s or ""))

    rn = _norm(rewritten)
    violations: list[str] = []

    for e in contract.get("entities") or []:
        s = _norm(e)
        if s and s not in rn:
            # L1: 缺即 BLOCK。L2: 不 BLOCK——LLM 提取 entities 时会把场景短语
            #   （如「白墙高塔」「灰袍人」）误收为锁定实体；真正的人名/势力名
            #   （林小凡/太清都）LLM 不会改。事件真删由 locked_sentences≈0 +
            #   presence 全缺兜底。
            if level != "L2":
                violations.append(f"锁定角色/势力/物品「{e}」在重写稿中缺失（被删或改名）")

    for rel in contract.get("relationships") or []:
        parts = [x.strip() for x in re.split(r"[-–—]", str(rel)) if x.strip()]
        for name in parts[:2]:
            s = _norm(name)
            if len(s) >= 2 and s and s not in rn:
                violations.append(f"锁定关系端点「{name}」在重写稿中缺失")

    for num in contract.get("key_values") or []:
        ctx = _norm(num.get("context") or "")
        val = str(num.get("value") or "").strip()
        if len(ctx) < 4 or not val:
            continue
        # ★2026-10-10 修复数值锁两处确定性误伤（实跑第002章：锁「烟火值：」=8/10 必 BLOCK）：
        #   ① find(ctx) 只看首个出现——同 context 多次出现且值不同（早=4 / 晚=8/10）时，
        #      首个=4 → 误报「被改为 4」。改为扫描全部出现，任一出现后紧跟 val 即算保留。
        #   ② re.match(r"\d+(?:\.\d+)?") 提不出「8/10」复合值（斜杠不在模式内），
        #      "8" != "8/10" → 对准了也必 BLOCK。增加 tail 前缀逐字比较通道。
        #      语义只放宽不收紧：原单值路径（digits == val）行为不变。
        if ctx not in rn:
            continue
        _preserved = False
        _got: str | None = None
        _scan = 0
        while True:
            i = rn.find(ctx, _scan)
            if i < 0:
                break
            tail = rn[i + len(ctx): i + len(ctx) + 8]
            m = re.match(r"\d+(?:\.\d+)?", tail)
            if m and not _got:
                _got = m.group(0)
            if tail.startswith(val) or (m and m.group(0) == val):
                _preserved = True
                break
            _scan = i + 1
        if not _preserved:
            violations.append(f"锁定数值「{(num.get('context') or '')[:30]}」={val} 被改为 {_got or '缺失'}")

    # 全局结果句（确定性完成态闸门）：含强完成词的原文句逐句校验 2-gram 保留度。
    #   覆盖 <0.35 → 该完成态句被删/合并/改写 → BLOCK。阈值取 0.35 而非 0.5：
    #   L1 允许表达重写（如「五百份，我全接」→「五百份我都接了」，2-gram 保留 ~0.6 可过），
    #   但结果反转/句删除（共享 2-gram ≈0）必被拦截（2026-09-20 调参后定）。
    for gs in contract.get("global_result_sents") or []:
        gsn = _norm(gs)
        # 硬词校验：完成态核心词必须逐字保留（拦单点反转/完成态词消失）
        hard = _REWRITE_RESULT_HARD_RE.findall(gsn)
        if hard:
            miss = [w for w in hard if w not in rn]
            if miss:
                violations.append(
                    f"原文结果句完成态词缺失（{'、'.join(miss[:3])}）：「{gs[:36]}」—— 接单/认账/交付/离开等结果被反转或删除")
                continue
        if len(gsn) < 6:
            continue
        gb = [gsn[i:i + 2] for i in range(len(gsn) - 1)]
        gt = len(gb)
        gk = sum(1 for b in gb if b in rn)
        # L1: 0.35；L2: 0.20（结构重写必然改写叙述长句；整段删除时 2-gram≈0 仍拦。
        #   硬词 hard 校验在上面独立执行，结果反转仍被拦，这里只管"句子被删"）。
        _gs_thr = 0.20 if level == "L2" else 0.35
        if gk / gt < _gs_thr:
            violations.append(
                f"原文结果句保留不足（{gk}/{gt}）：「{gs[:36]}」—— 事件完成态（接单/认账/交付/离开/收尾）被删或改写")

    for ev in contract.get("core_events") or []:
        # 事件存在层：presence 词元（净化后逐字来自原文）全部出现 = 事件主体/关键动作保留。
        pres = [p for p in (ev.get("presence") or []) if len(_norm(p)) >= 2]
        if pres:
            missing = [p for p in pres if _norm(p) not in rn]
            # L1：presence 缺任一即 BLOCK（事件主体词被删/改名）。
            # L2：全缺才 BLOCK（≥1 个出现 = 事件仍在稿中；结构重写允许换说法，
            #   如「空气的味道」→「空气里什么味都没有」、「红色包装」→「那包辣条」，
            #   事件没删只是表达变了——实测第1章 22:54 两次 BLOCK 都是这类误拦）。
            if level == "L2":
                if missing and len(missing) == len(pres):
                    violations.append(
                        f"事件「{(ev.get('text') or '')[:30]}」存在词全缺（{len(missing)}/{len(pres)}）："
                        + "、".join(str(p) for p in missing[:3]) + " —— L2：事件主体词一个都没出现，疑似事件被删")
            else:
                if missing:
                    violations.append(
                        f"事件「{(ev.get('text') or '')[:30]}」存在词缺失（{len(missing)}/{len(pres)}）："
                        + "、".join(str(p) for p in missing[:3]) + " —— 事件主体疑似被删")
        # 事件完成层：completion 词元在重写稿中出现。
        #   L1：逐字全保留（缺任一 = 事件未完成 → BLOCK）。
        #   L2：不 BLOCK——completion 是 LLM 提取的动作描写短语（如「撑起身子」「摸口袋」），
        #   L2 结构重写必然换说法（→「坐起来」「掏口袋」），逐字匹配误拦率极高。
        #   事件真删由 locked_sentences 2-gram≈0 + presence 全缺兜底。
        comp = [c for c in (ev.get("completion") or []) if len(_norm(c)) >= 2]
        if comp and level != "L2":
            missing = [c for c in comp if _norm(c) not in rn]
            if missing:
                violations.append(
                    f"事件「{(ev.get('text') or '')[:30]}」完成态缺失（{len(missing)}/{len(comp)}）："
                    + "、".join(str(c) for c in missing[:3]) + " —— 事件未完成，不能仅凭词汇存在放行")
        # 事件句保留层（主保护）：locked_sentences 是事件所在原文句（逐字，含完成态隐性表达，
        #   如「我认」「目送他掀帘出去」）。
        #   L1：2-gram 覆盖 <0.5 → 事件段被删/合并 → BLOCK。
        #   L2：阈值 0.5 → 0.3（结构重写必然改写句子；整段删除时覆盖≈0 仍被拦，
        #        但「他咬咬牙撕开包装」→「他咬咬牙撕开封口」这类合理改写放行——
        #        实测第1章 L2 两次编译 13/27=48%、17/37=46%，卡在 0.5 阈值线下死循环）。
        lss = [_norm(s) for s in (ev.get("locked_sentences") or []) if len(_norm(s)) >= 8]
        if lss:
            lt = "".join(lss)
            lb = [lt[i:i + 2] for i in range(len(lt) - 1)]
            total = len(lb)
            kept = sum(1 for b in lb if b in rn)
            _lock_thr = 0.20 if level == "L2" else 0.50
            if kept / total < _lock_thr:
                violations.append(
                    f"事件「{(ev.get('text') or '')[:30]}」原文事件句保留不足（{kept}/{total}），"
                    "事件或完成态表达疑似被删/合并")

    violations.extend(_check_length_collapse(original, rewritten))
    return violations


# ---- L2 专属确定性时间事实保护（2026-09-20 用户裁决，最小闭环）----
# 不依赖 LLM analyze（其 key_values 可能为空 → Guard 对时间无保护）。
# 正则确定性提取「中文数值 + 时间单位」事实对；规则：双向存在性一致。
#   新稿出现原文不存在的时间数值组合（二十天→二十五天）→ 时间事实漂移 BLOCK；
#   原文时间数值组合在新稿完全消失 → 时间事实缺失 BLOCK。
_CN_NUM_CLS = "一二三四五六七八九十百千万零两"
_TIME_UNIT_CLS = "天日月年载时辰更"
_TIME_FACT_RE = re.compile(
    rf"(?<![第{_CN_NUM_CLS}])([{_CN_NUM_CLS}]+?)([{_TIME_UNIT_CLS}])"
)


# 时间表达归一化：一日/一天/1天 → canonical = (1, "天")
_CN_NUM_TO_INT = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
def _cn_num_to_int(s: str) -> int | None:
    """简单中文数字 → int（支持一到九十九）。"""
    if s.isdigit():
        return int(s)
    if s in _CN_NUM_TO_INT:
        return _CN_NUM_TO_INT[s]
    # 处理"二十"/"二十五"等
    if "十" in s:
        parts = s.split("十")
        tens = _CN_NUM_TO_INT.get(parts[0], 1) if parts[0] else 1
        ones = _CN_NUM_TO_INT.get(parts[1], 0) if len(parts) > 1 and parts[1] else 0
        return tens * 10 + ones
    return None

def _normalize_time_fact(v: str, u: str) -> tuple[str, str]:
    """归一化时间表达：一日/一天/1天 → ("1", "天")。统一返回 str 类型，避免 sorted 时 int < str 报错。"""
    n = _cn_num_to_int(v)
    if n is None:
        return (str(v), str(u))
    # 日/天/日 → 统一为"天"
    u_norm = "天" if u in ("日", "天", "天") else u
    return (str(n), u_norm)


def _int_to_cn(n: int) -> str:
    """int → 简单中文数字（0-99，与 _cn_num_to_int 互逆）。
    2026-09-22 新增：P9 确定性承接需要把归一化数字还原成文本形态去细纲中检索原句。"""
    if n < 0 or n > 99:
        return str(n)
    _d = "零一二三四五六七八九"
    if n < 10:
        return _d[n]
    if n == 10:
        return "十"
    tens, ones = divmod(n, 10)
    return ("十" if tens == 1 else _d[tens] + "十") + (_d[ones] if ones else "")


def _outline_time_sentence(outline: str, v: str, u: str) -> str:
    """在细纲中定位含时间数值 (v,u) 的最短原句（2026-09-22 新增，P9 确定性承接用）。
    归一化把「日」并入「天」，检索时还原本单位两种形态（天/日）。
    两轮检索（39 章实测：时间值可能只出现在小节标题行如「### 第一幕（场景：坊市/连续三天）」）：
    第一轮跳过 markdown 标题行取正文原句；第二轮退而求其次取标题行并剥离 # 前缀。
    返回空串 = 未找到（_miss9 取自 _extract_time_facts(outline)，正常必有）。"""
    if not outline:
        return ""
    forms = []
    if str(v).isdigit():
        n = int(v)
        forms.extend([_int_to_cn(n) + u, f"{v}{u}"])
        if u == "天":
            forms.extend([_int_to_cn(n) + "日", f"{v}日"])
    else:
        forms.append(f"{v}{u}")
    segs = [s.strip() for s in re.split(r"[。！？；\n]", outline)]
    for seg in segs:  # 第一轮：正文句优先
        if seg and not seg.startswith("#") and any(f in seg for f in forms):
            return seg
    for seg in segs:  # 第二轮：标题行剥离 # 前缀
        if seg and seg.startswith("#"):
            seg = re.sub(r"^#+\s*", "", seg).strip()
            if seg and any(f in seg for f in forms):
                return seg
    return ""

def _extract_time_facts(text: str) -> set:
    """确定性提取正文中的时间事实（归一化后）；排除「第二日」等序数。"""
    raw = set(_TIME_FACT_RE.findall(text or ""))
    return set(_normalize_time_fact(v, u) for v, u in raw)


def _check_time_facts(original: str, rewritten: str, c8_param: dict = None, outline: str = "") -> list[str]:
    """L2 时间事实守卫：原文/新稿的数值+单位组合双向一致。
    C8 交易参数（days）作为业务执行参数，不算剧情时间漂移。
    2026-10-31 细纲豁免（第1章实测事故）：细纲=章节任务权威层，Writer 忠实转述
    细纲时间值（如细纲「三日」新稿「3天」，归一化后同值）而原文恰无时间字面时，
    不再误杀式 BLOCK——口径与 L3 版 _check_time_facts_l3 的 outline 豁免一致。"""
    a = _extract_time_facts(original)
    b = _extract_time_facts(rewritten)
    o = _extract_time_facts(outline or "")
    out = []
    # C8 交易参数数值（不算剧情时间）
    c8_days = None
    if c8_param:
        v = (c8_param.get("values") or {})
        if v.get("days"):
            c8_days = v["days"]  # 现在是 int
    for v, u in sorted(b - a):
        # C8 days 交易参数不算漂移（v 现在是 str）
        if c8_days and u == "天" and str(v) == str(c8_days):
            continue
        if (v, u) in o:
            continue  # 细纲（任务权威层）声明的时间事实，忠实转述不算漂移
        out.append(f"时间事实漂移：新稿出现原文不存在的时间数值「{v}{u}」")
    for v, u in sorted(a - b):
        out.append(f"时间事实缺失：原文时间数值「{v}{u}」在新稿中消失")
    return out


def _l3_plan_time_facts(plan: dict) -> set:
    """从 L3 Plan 合同文本确定性提取时间事实值（口径同 _extract_time_facts）。
    覆盖：immutable_contract（canon/chapter_objective/required_outcomes.desc）
    + 5_continuity_check.hard_dependencies + 3_new_dramatic_design 全字段
    + 4_outcome_mapping.evidence_hint。Plan 合同 = L3 时间事实的权威锚。"""
    ic = (plan or {}).get("1_immutable_contract") or {}
    cc = (plan or {}).get("5_continuity_check") or {}
    nd = (plan or {}).get("3_new_dramatic_design") or {}
    om = (plan or {}).get("4_outcome_mapping") or {}
    parts: list[str] = []
    parts += [str(x) for x in (ic.get("canon") or [])]
    parts += [str(x) for x in (ic.get("chapter_objective") or [])]
    parts += [str(r.get("desc") or "") for r in (ic.get("required_outcomes") or []) if isinstance(r, dict)]
    parts += [str(x) for x in (cc.get("hard_dependencies") or cc.get("post_dependencies") or [])]
    parts += [str(nd.get(k) or "") for k in ("opening", "conflict", "escalation", "character_actions",
                                             "information_release", "turning_point", "ending")]
    parts += [str(e.get("desc") or "") for e in (nd.get("event_organization") or []) if isinstance(e, dict)]
    parts += [str(v.get("evidence_hint") or "") for v in om.values() if isinstance(v, dict)]
    return _extract_time_facts("\n".join(parts))


def _check_prose_hygiene(rewritten: str) -> list[str]:
    """Gate H 正文卫生（2026-10-24 用户裁决，第2章实测案例）：元文本/写作者视角词
    确定性句面拦截——重写稿出现「他推门进来的动作比第1章还猛。」直通 review/commit
    的事故封堵。只做字面模式，不做语义判断；返回违规清单（空=通过）。"""
    out: list[str] = []
    pat = re.compile(r"(第\s*[一二三1-9]\s*章|本章|上一章|前一章|读者|作者|写手|细纲)")
    for m in pat.finditer(rewritten or ""):
        ctx = (rewritten[max(0, m.start() - 14):m.end() + 14]).replace("\n", " ")
        out.append(f"正文元文本：出现写作者视角词「{m.group(0)}」（{ctx}）——叙事只能写画内之事，"
                   "跨章比较/章节自指必须改写为具体画面")
        if len(out) >= 4:
            break
    return out


def _check_time_facts_l3(original: str, rewritten: str, plan: dict, c8_param: dict = None, outline: str = "") -> list[str]:
    """L3 时间事实守卫（2026-09-22 用户裁决，L3 专属；L2 继续用 _check_time_facts）：
    L3 = 章节再生产，时间事实锚是「Plan 合同」而非「原文字面」：
      漂移：新稿时间值 ∈ Plan 合同时间值 → canon 忠实事实豁免（如 Plan canon
           「二十年前老宋头被灭口」，原正文无此字面，Writer 忠实编译不算漂移）；
           2026-10-24：细纲事实（Writer 的另一上游输入锚）同样豁免——writer 忠实
           转述细纲时间值而原文/Plan 恰未含时，不再误伤式 BLOCK；
           其余新值 → 漂移 BLOCK（fail-closed 不变）。
      缺失：原文时间值消失且 Plan 合同认定该值（合同亦含）→ 真剧情时间丢失 BLOCK；
           Plan 合同未认定的（风味/修辞时间词，如「百年前散修」「新的一天」）
           → L3 允许重新设计事件组织，不保留字面不算缺失。"""
    a = _extract_time_facts(original)
    b = _extract_time_facts(rewritten)
    p = _l3_plan_time_facts(plan)
    o = _extract_time_facts(outline or "")
    out = []
    c8_days = None
    if c8_param:
        v = (c8_param.get("values") or {})
        if v.get("days"):
            c8_days = v["days"]
    for v, u in sorted(b - a):
        if c8_days and u == "天" and str(v) == str(c8_days):
            continue
        if (v, u) in p or (v, u) in o:
            continue  # Plan 合同/细纲声明的时间事实（canon 忠实），不算漂移
        out.append(f"时间事实漂移：新稿出现原文与 Plan 合同均不存在的时间数值「{v}{u}」")
    for v, u in sorted(a - b):
        if (v, u) not in p:
            continue  # Plan 合同未认定的原文风味时间词，再生产允许不保留
        out.append(f"时间事实缺失：Plan 合同要求保留的时间数值「{v}{u}」在新稿中消失")
    return out



# ---------------------------------------------------------------------------
# L3 章节再生产生产链（2026-09-20 用户裁决，最小实现面）：
#   run_rewrite_pipeline(level="L3") = L3 Plan(Stage1) → Writer Compilation(Stage2)
#   → normalize → L3 Guard(Gate A~F) → (生产路径才继续 delta/consistency/review/post_write/commit)
#   L3 Guard 与 L1/L2 的本质差异：不逐事件比对旧正文，而是
#   Plan outcome_mapping / Required Outcomes → 新正文证据（L3 允许重新设计事件组织）。
#   六 Gate：A Canon / B Chapter Function / C Required Outcomes / D Continuity / E C8 / F Zero-change。
# ---------------------------------------------------------------------------

# 负向模式：Canon 破坏（身份/规则/关系反转）——L3 允许表达变化，只拦截事实性破坏。
_L3_CANON_NEG_PATTERNS = [
    (re.compile(r"林小凡.{0,15}(?:有灵根|筑基|炼气|金丹|元婴|修为)"), "林小凡身份（凡人无灵根）被改写"),
    (re.compile(r"烟火值.{0,8}(?:下降|降低|减少|清零|退回)"), "烟火值只进不退规则被破坏"),
    (re.compile(r"凡气.{0,15}(?:来源是|是因为|真相是|其实是)"), "凡货吸引力悬念被解释"),
    (re.compile(r"陈执事.{0,12}(?:反目|成仇|背叛|追杀)"), "陈执事关系反转"),
]
_L3_CONTINUITY_NEG = [
    (re.compile(r"汪万金.{0,20}(?:动摇|相信|认同|加入|突破)"), "第36章内容提前（汪万金应停在怀疑）"),
    (re.compile(r"温故.{0,12}(?:光晕|丹田|异样)"), "第36章内容提前（温故丹田异样）"),
    (re.compile(r"青云宗.{0,15}(?:邀约|邀请).{0,10}(?:接受|答应|拒绝|受邀)"), "F008 邀约结果被提前点破"),
]
_L3_CANON_TAG_SKIP = ("世界规则", "未决冲突", "本章", "冲突", "核心", "章节")
_L3_CANON_CONSTRAINT_HINT = ("不得", "不展开", "不提前", "未决", "registry", "冲突", "第", "兑现登门")



def _l3_c8_sanitize(obj, c8_param):
    """L3 Writer 输入 C8 隔离（2026-09-20 用户裁决，最小实施面）：
    把 packet/plan 文本中的 outline 冲突数值（三百份/八块灵石/二十五天）在内存中
    归一为执行参数值（选择权在执行层 AUDIT_OR_EXECUTION_PARAM；Writer 只看到单一体系）。
    只替换 C8 三组冲突数值，不触碰其他内容；不改细纲文件、不改 packet 存储。
    返回归一后的对象副本。"""
    if c8_param is None:
        return obj
    v = (c8_param or {}).get("values") or {}
    pairs = []
    if v.get("quantity") == 500:
        pairs.append(("三百份", "五百份"))
    if v.get("price") == 10:
        pairs.append(("八块灵石", "十块灵石"))
    if v.get("days") == 20:
        pairs.append(("二十五天", "二十天"))
    if not pairs:
        return obj

    def _walk(o):
        if isinstance(o, str):
            for a, b in pairs:
                o = o.replace(a, b)
            return o
        if isinstance(o, list):
            return [_walk(x) for x in o]
        if isinstance(o, dict):
            return {k: _walk(x) for k, x in o.items()}
        return o
    return _walk(obj)

def _l3_c8_param(original: str) -> dict:
    """C8 执行参数（AUDIT_OR_EXECUTION_PARAM）：从当前已提交正文确定性检测既有变体体系。
    选择者=执行层（延续当前生产正文既有体系），非 L3 Planner/Writer。
    本函数只读当前正文（外部事实），不裁决 Audit 冲突正确性。"""
    t = original or ""
    body = all(re.search(p, t) for p in (r"五百份", r"十块灵石", r"二十天"))
    outline = all(re.search(p, t) for p in (r"三百份", r"八块灵石", r"二十五天"))
    # ★detected=False 表示这两套数值体系在本章正文里都不存在：下面仍会兜底成 body_variant，
    #   但那是凭空注入的商战数值（500份/10块灵石/20天）。校验侧必须据此放行（fallback=True），
    #   否则非商战题材永远被要求落地不存在的数值 → 重试必败（2026-10-16《盗天藏书客》第1章）。
    detected = body or outline
    variant = "body_variant" if body else ("outline_variant" if outline else "body_variant")
    values = ({"days": 20, "quantity": 500, "price": 10} if variant == "body_variant"
              else {"days": 25, "quantity": 300, "price": 8})
    return {
        "source": "AUDIT_OR_EXECUTION_PARAM", "conflict_id": "C8", "status": "unresolved",
        "selected_variant": variant, "values": values,
        "detected": detected, "fallback": not detected,
        "decided_at": "pipeline_runtime", "basis": "current_committed_body",
        "note": "执行层注入；L3 Planner/Writer 不得裁决、不得更改、不得讨论来源",
    }


def _l3_c8_writer_block(c8_param: dict | None) -> str:
    """L3 Writer 的 C8 数值注入段（2026-11-06 抽为纯函数，便于行为级回归）。

    ★ 幽灵数值护栏：_l3_c8_param() 在本章正文两套数值体系都检测不到时
      （detected=False / fallback=True）仍兜底返回硬编码的 500份/10块灵石/20天；
      本文件 :11349-11351 与 :12266-12270 的注释已自证「凭空注入」「本章根本不存在」。
      此时若仍以「已确认的 Canonical Fact，必须在正文中正确写出」注入 Writer，
      就会凭空造出与本章结算事实冲突的台词——《用泡面征服修仙界》第1章
      「五百份×十块灵石」报价 vs 细纲/packet「实付 1 枚下品灵石」，
      一致性审查判 S2 硬伤、事务回滚（2026-11-06 task f32a57be1c2d）。

    fallback → 返回空串（不注入，也不追加任何替代指令，零跨书副作用）；
    detected=True（正文确有该数值体系）→ 返回文本与改动前逐字一致。
    env 回滚：NWW_L3_C8_NO_GHOST=0 恢复原「一律注入」行为。
    """
    if not c8_param:
        return ""
    _ghost = bool(c8_param.get("fallback")) or not bool(c8_param.get("detected", True))
    if _ghost and _env_flag_on("NWW_L3_C8_NO_GHOST", "1"):
        return ""
    v = c8_param.get("values") or {}
    if not v:
        return ""
    return (
        "\n【C8 执行参数（必须落地到正文）】以下数值是已确认的 Canonical Fact，你必须在正文中正确写出这些具体数字："
        "订单数量={quantity}份、单价={price}块灵石、周期={days}天。"
        "不得用'足量''长期''合作'等模糊表达替代；不得混用其他数值；不得解释参数来源。\n".format(
            quantity=v.get("quantity", "?"),
            price=v.get("price", "?"),
            days=v.get("days", "?"),
        )
    )


def _l3_c8_registry(original: str) -> dict:
    """构造 C8 冲突 registry（注入 L3 Plan 的不可破坏层）。

    ★ 幽灵数值护栏（与 _l3_c8_writer_block 同源）：当本章正文两套数值体系都检测不到
      （_l3_c8_param fallback=True）时，500/10/20、300/8/25 是凭空注入、本章根本不存在的
      数值。若仍把它们作为「未决冲突 variant」暴露给 Planner，Planner 会据此把「五百份/
      十块灵石/二十天」写进 Plan，Writer 再照搬到正文——这正是《用泡面征服修仙界》第1章
      重写出现「五百份×21」（task c006f15e7e20，stream_text 命中 21 次）的源头。

    fallback → 返回 variants=[] 的「无冲突」版本：status 仍 unresolved、adjudicator 仍
      AUDIT_DOMAIN（通过 P5 校验），但 Planner/Writer 看不到任何数字锚，不会写出本章
      不存在的数值。
    detected=True（正文确有该数值体系）→ 返回与改动前逐字一致的完整两 variant registry。
    env 回滚：NWW_L3_C8_NO_GHOST=0 时，fallback 也强制返回完整 registry（原行为）。
    """
    _c8 = _l3_c8_param(original)
    _no_ghost = _env_flag_on("NWW_L3_C8_NO_GHOST", "1")
    if _c8.get("fallback") and _no_ghost:
        return {
            "conflict_id": "C8", "type": "canon_conflict", "status": "unresolved",
            "adjudicator": "AUDIT_DOMAIN",
            "variants": [],
            "allowed_actions": [
                "preserve_one_existing_variant_for_internal_consistency_without_choosing",
                "do_not_introduce_new_variant",
            ],
            "forbidden_actions": [
                "adjudicate_correctness", "declare_one_variant_authoritative",
                "describe_conflict_as_current_problem", "introduce_third_numeric_set",
                "resolve_audit_conflict_as_part_of_l3_design",
            ],
            "numeric_execution_param": {
                "selector": "AUDIT_OR_EXECUTION_PARAM",
                "constraint": "none",
                "note": "本章正文无数值型冲突体系（fallback），C8 不激活，无 variant 暴露给 Planner/Writer",
            },
        }
    return {
        "conflict_id": "C8", "type": "canon_conflict", "status": "unresolved",
        "adjudicator": "AUDIT_DOMAIN",
        "variants": [
            {"id": "outline_variant", "values": {"days": 25, "quantity": 300, "price": 8}},
            {"id": "body_variant", "values": {"days": 20, "quantity": 500, "price": 10}},
        ],
        "allowed_actions": [
            "preserve_one_existing_variant_for_internal_consistency_without_choosing",
            "do_not_introduce_new_variant",
        ],
        "forbidden_actions": [
            "adjudicate_correctness", "declare_one_variant_authoritative",
            "describe_conflict_as_current_problem", "introduce_third_numeric_set",
            "resolve_audit_conflict_as_part_of_l3_design",
        ],
        "numeric_execution_param": {
            "selector": "AUDIT_OR_EXECUTION_PARAM",
            "constraint": "chapter_must_be_internally_consistent_with_exactly_one_variant",
            "note": "由执行层决定；L3 Planner 不读取具体 selector、不决定 variant",
        },
    }


def _l3_reproduction_plan(book: Path, chapter_id: int, original: str, outline: str = "",
                          on_token=None, feedback: str = "") -> dict:
    """L3 调用策略 Stage 1：结构/机制重设计计划（2026-09-20 用户裁决，与 L2 Split 同构）：
    只输出五部分 Plan JSON，不写正文。L3 Plan 允许重新设计具体事件组织/场景/冲突/机制
    （type=保留|替换|合并|新增 + serves），但不得越过 Canon / Chapter Objective /
    Required Outcomes / Causal Dependencies。C8 以结构化 conflict_registry 注入（S2.1），
    Planner 不得声明/选择/评价 C8 数值（数值由执行参数决定）。
    feedback（2026-09-22 新增，对齐 Writer 编译循环的违规反馈机制）：上稿 Plan 被校验器
    拒绝的违规清单，注入 prompt 供本次生成规避——此前 Plan 循环是盲重试（违规不反馈），
    LLM 每次随机重掷倾向重复同类漂移（实测 39 章：三次全部发明「地下渠道」，
    P8 连续拦截而 LLM 不自知）。feedback 为空时 prompt 与原行为完全一致。"""
    c8_registry = _l3_c8_registry(original)
    system = (
        "你是长篇网文生产的章节重新生产（L3）规划师。本阶段你只输出结构化 L3 Reproduction Plan（JSON），"
        "严禁写正文、严禁复述原文句子。"
        "你拥有重新设计剧情实现的自由度：具体事件可以替换、合并、拆解、新增；场景、冲突触发、信息释放、"
        "人物行动、对白、节奏、高潮、收束都可以重新设计。"
        "但你必须遵守不可破坏层：世界 Canon、人物既定事实、章节核心任务、章节必须落地的结果、前后章因果依赖。"
    )
    prompt = (
        f"任务：为第{chapter_id}章设计 L3 Reproduction Plan（重新生产计划）。\n"
        + (f"\n======== 上稿 Plan 被拒原因（本次生成必须逐条规避）========\n{feedback}\n\n"
           if feedback.strip() else "")
        + "本章需要重新生产（原章具体实现不理想），但章节任务与 Canon 必须保持。\n\n"
        "======== 不可破坏层（L3 Contract） ========\n"
        "【Canon】世界 Canon、人物既定事实、世界规则、章节功能、前后章因果依赖，以细纲/前情为准。\n"
        "【未决冲突 registry（外部事实，L3 无裁决权）】\n"
        + json.dumps(c8_registry, ensure_ascii=False, indent=1) + "\n\n"
        "【Chapter Objective】本章必须完成的核心任务、核心矛盾、必须产生的角色状态、必须完成的剧情功能。\n"
        "【Required Outcomes】本章必须全部落地的结果，至少完整覆盖以下 7 类，不得合并、裁剪或遗漏：\n"
        "  R1 本章核心事件结果（本章实际发生的主线动作落地；若本章涉及买卖/订单，则写明成交与否）；\n"
        "  R2 立场/原则结果（角色在本章做出的明确抉择或坚持，如拒绝/接受某个条件）；\n"
        "  R3 关键数值/条件结果（本章确有数值型事实——价格/数量/期限/境界等——才写明；"
        "本章若无数值型事实，desc 写「本章无数值型事实」并把 hard 置为 false）；\n"
        "  R4 伏笔推进；R5 章末钩子；R6 角色状态指标（如烟火值/境界/声望类变化）；\n"
        "  R7 后章依赖（近期待办/未了事项/悬而未决的压力，用「接下来/近期/随后」相对措辞，"
        "不新增「一天/一日/两天/七天」等计数时间词）。\n"
        "【Causal Dependencies】前置（上一章状态→本章）与后置（本章→下一章依赖）；"
        "严禁把后续章节内容提前引入本章。\n\n"
        f"======== 细纲要点 ========\n{outline[:800]}\n\n"
        "======== 原章正文（剧情与事实参考，不是文字模板） ========\n"
        f"<original>\n{original[:6000]}\n</original>\n\n"
        "======== 输出要求 ========\n"
        "只输出一个 JSON 对象（不要 markdown 代码块包裹），结构：\n"
        "【结构强制】下面五个部分——1_immutable_contract、2_current_problems、"
        "3_new_dramatic_design、4_outcome_mapping、5_continuity_check——"
        "必须是**同一层级的五个顶层键**。严禁把 2/3/4/5 写进 1_immutable_contract 里面，"
        "严禁逐层往内嵌套（形如 {…\"2_current_problems\":{…\"3_new_dramatic_design\":{…}}}）；"
        "写每个部分前先确认它前面那个部分的大括号已经闭合。结构错误会导致整个 Plan 被判不完整并重生成。\n"
        '{"1_immutable_contract":{"canon":[...],"chapter_objective":[...],'
        '"required_outcomes":[{"id":"R1","desc":"...","hard":true}],'
        '"causal_dependencies":{"pre":[...],"post":[...]},'
        '"conflict_registry":<原样引用上面的 C8 对象，不重写不增删>},'
        '"2_current_problems":{"allowed_sources":["prose_execution","dramatic_design","pacing","character_behavior","information_release"],'
        '"forbidden_sources":["unresolved_canon_conflicts","audit_conflicts"],'
        '"problems":[{"id":"CP01","source":"dramatic_design","severity":"P2","description":"..."}]},'
        '"3_new_dramatic_design":{"opening":"...","conflict":"...","escalation":"...","character_actions":"...",'
        '"information_release":"...","turning_point":"...","ending":"...",'
        '"event_organization":[{"id":"N1","type":"保留|替换|合并|新增","desc":"...","serves":["O1"]}]},'
        '"4_outcome_mapping":{"R1":{"node":"N4","evidence_hint":"..."}, "R2":{...}},'
        '"5_continuity_check":{"pre_dependencies":[...],"chapter_state_changes":[...],'
        '"hard_dependencies":["本章已建立、下一章必须承接的确定性事实，如：交付已发生/门外出现脚步声/赵执事已介入"],'
        '"narrative_dependencies":["剧情发展方向，如：危机升级/监视线索推进/压迫感增强"]}}\n\n'
        "约束（必须遵守）：\n"
        "1. outcome_mapping 必须覆盖本章全部 Required Outcomes（R1..Rn），每个 R 都有节点承载。\n"
        "   【类型强制】4_outcome_mapping 必须是 JSON object，键为 R1/R2 等 outcome ID：\n"
        '   正确：{"R1":{"node":"N4","evidence_hint":"..."}, "R2":{...}}\n'
        '   禁止：[{"R1":{...}}, {"R2":{...}}]（这是 array，不是 object）\n'
        "   类型要求：object，不是 array。\n"
        "2. event_organization 每项必须声明 serves（服务哪个 Objective/Outcome）。\n"
        "3. 【C8 纪律】L3 Plan 不得自行创造、选择、评价、写出任何 C8 具体数值（不得出现五百份/十块灵石/二十天等）。"
        "Plan 创作节（2/3/4/5）中不得出现具体数字；"
        "需要数值落地的 R（如 R1/R3），在 outcome_mapping 的 evidence_hint 中标注「需落地 C8 数值」即可，由 Writer 从执行参数读取并写入正文。\n"
        "4. 2_current_problems 的每个问题 source 必须来自 allowed_sources；"
        "不得把 C8 未决冲突、细纲与正文矛盾当作本章问题。\n"
        "5. 新增事件不得改变人物身份/既有关系事实/世界规则。\n"
        "6. 后章依赖不得删除、提前或新增承诺。\n""   【字段语义强制】5_continuity_check 必须区分：\n""   hard_dependencies：仅填写本章已经落地、下一章必须承接的确定性事实（具体事件/实体/状态变化），D_Continuity 将对此做硬校验。\n""   narrative_dependencies：填写剧情发展方向/危机升级/悬念/人物动作意图，Review 层负责评价，不进入硬 BLOCK。\n""   禁止：把叙事意图填入 hard_dependencies；禁止把确定事实填入 narrative_dependencies。\n"   "   禁止：hard_dependencies 中残留 C8/「需落地 C8 数值」等占位引用；数值未定的确定事实写为不含占位符的中文断言（如「一笔<本章交易>已完成并记录」，尖括号须替换为本章真实事件），具体数值由 Writer 按 C8 执行参数写入正文。\n"        "   【P10 硬事实锚定】hard_dependencies 每条都必须能在 <original> 或细纲中找到逐字或同义的事实依据（校验器用与章节功能相同的锚点口径逐条核验），且必须是陈述句事实断言：禁止嵌入角色台词的引号串（威胁/扬言/留话属 narrative_dependencies）、禁止把「谁告知谁」写成与细纲相反的施受主体、禁止「需落地/待定/由执行参数决定」等生成侧元语言。含占位符或凭空事实的条目会触发 P10，Plan 整体重生成。\n"        "6.5 补充（hard_dependencies 事实纪律，违者被 D_Continuity 硬校验拦截并重编译）：① 主体一致——收款/付款方、施动者/受动者必须与细纲/原章完全一致，禁止颠倒（如本章付款方是王瘸子而非林小凡）；② 禁止编造细纲与原章未出现的具物/数量/状态细节（如凭空出现的「半桶清水」）；③ 章尾钩子为开放悬念（验货队尚在路上/倒计时刚开始）时，hard_dependencies 必须写成开放态，禁止写成已闭合事实（如「已出现在巷口」「倒计时结束」）。\n"        "6.6 补充（Required Outcomes 立场断言纪律）：required_outcomes.desc 中的「拒绝/不接受/坚持不/绝不/不 +（独家/赊账/妥协/让步）」类立场断言，必须能在细纲中找到对应依据，禁止自由发挥新增细纲未要求的立场（如凭空加「拒绝妥协」）——否则 C_RequiredOutcomes 否定结果硬检查会误判正文。\n"        "7. 【章节功能事实锚定】chapter_objective（章节功能）必须从 <original> 本章既有剧情中归纳（本章已实际发生的功能/事件），"
        "每条末尾必须以（据：「……」）收尾——引号内为原文或细纲中逐字存在的短语（≤20字），作为该功能的证据锚点，"
        "示例：「确立<本章角色>的既定处事原则（据：「<原文逐字短语，≤20字>」）」；\n"
        "   【示例禁令】上面尖括号内的内容只用于说明格式：严禁把本提示中出现的任何示例角色名、"
        "示例实体、示例短语原样写进 Plan 的任何字段，一律从 <original> 与细纲取本章真实的人名与事实。\n"
        "禁止发明原文不存在的章节功能，尤其禁止把后续章节的功能提前引入本章；"
        "禁止用修辞成语/比喻/抽象概念充当据短语——校验器将逐字核验（据：…）短语是否真实存在于原文∪细纲，"
        "据短语造假、缺失或脱离原文事实的功能都会导致 Plan 整体重生成。\n"
        "   【据短语摘录禁令】据短语必须是原文或细纲中**连续的一整段**、逐字照抄："
        "禁止用「...」「…」等省略号把不相邻的两段拼成一个短语（如「别人修仙靠...他修仙靠捡拾」），"
        "禁止增删虚词与量词（原文「有几个长老在查什么异端」不得写成「有个长老在查什么异端」），"
        "禁止把原文的逗号改写成省略号；"
        "禁止同义替换指示代词（原文「这声钟响的方向不对」不得写成「那声钟响的方向不对」），"
        "禁止压缩改写（原文「直接钻进了他的左眼」不得压成「飞进左眼」）——"
        "以上任一改写都会被判为锚点造假并导致整体重生成。\n"
        "8. 【细纲时间事实承接】细纲要点中出现的剧情时间数值（如「练了三天」「修了四十年」），"
        "必须原样写入 Plan 合同字段（canon/chapter_objective/required_outcomes.desc/"
        "3_new_dramatic_design/5_continuity_check.hard_dependencies 等）——"
        "Writer 只被允许写出合同认定的时间事实，合同缺项会导致 Writer 与 Gate 冲突、Plan 整体重生成。\n"
    )
    raw, _fin = _writer_stream(system, prompt, on_token=on_token, temperature=0.5, response_format={"type": "json_object"}, book=book, chapter_id=chapter_id)
    # 2026-09-22 P1 修复：L3 Plan 输出完整性检测（对齐 step_writer :704 既有守卫）。
    # 此前 finish_reason 被丢弃，Plan 截断/跑飞只能靠 P1 校验事后兜底——实测 39 章
    # 第 3 次尝试输出只写到 1/2/3 部分即断，P1 报「缺 4/5」浪费一次重试机会且
    # 真实原因被掩盖。检测到截断直接 raise → 走 Plan 循环 except 分支，
    # 带真实原因进入违规反馈注入重试（而非盲重试）。
    _oc = check_output_completeness(raw, _fin)
    if _oc["status"] == "BLOCK":
        # 2026-10-02：EMPTY_OUTPUT 与「真截断」处置相反（换通道 vs 削 prompt），文案必须能区分。
        _hint = ("（疑似通道故障：模型端点未就绪/网页桥接未生成，不是提示词问题）"
                 if _oc["reason"] == "EMPTY_OUTPUT" else "")
        raise RuntimeError(f"Plan 输出不完整（{_oc['reason']}）{_hint} — {_oc['evidence']}")
    cleaned = raw.strip()
    # P2.3: 记录 raw output 用于诊断
    import time as _time
    _diag = book / "追踪/audit" / f"l3_plan_raw_{int(_time.time())}.txt"
    _diag.parent.mkdir(parents=True, exist_ok=True)
    _diag.write_text(f"RAW (len={len(raw)}):\n{raw}\n\nCLEANED (len={len(cleaned)}):\n{cleaned}", encoding="utf-8")
    i, j = cleaned.find("{"), cleaned.rfind("}")
    if i < 0 or j <= i:
        raise ValueError("L3 Plan 未输出 JSON 对象")
    try:
        plan = json.loads(cleaned[i:j + 1])
    except json.JSONDecodeError as exc:
        # ★碎片输出救援（2026-10-16）：先吐子对象再吐完整 Plan 时，find("{")→rfind("}")
        #   两端夹着碎片必然失败。此时打捞含 1_immutable_contract 的最长完整对象，
        #   打捞不到再走原报错路径（详见 _l3_extract_plan_json）。
        _rescued = _l3_extract_plan_json(cleaned)
        if _rescued is None:
            # 2026-09-29：定性 + 污染检测。拒答与回显污染的处置相反（换通道 vs 重试），
            # 混成一句 `Expecting ',' delimiter` 时人工无从判断。
            _kind = _json_err_kind(cleaned)
            if _echo_pollution_hit(raw, prompt):
                _kind += "＋提问原文回显污染（页面其实生成了，重试有救）"
            raise ValueError(f"L3 Plan JSON 解析失败（{_kind}）: {exc} (raw={_diag})") from exc
        plan = _rescued
    if not isinstance(plan, dict):
        raise ValueError("L3 Plan 不是对象")
    # ★五部分嵌套错位拍平（2026-10-16）：模型丢括号层级把 2/3/4/5 挂到 1 底下时，
    #   内容齐全却报 P1「缺 3/4/5」。顶层本就平铺则原样返回（零影响）。
    plan = _l3_flatten_plan(plan)
    # 2026-11-06 类型 sanitize：模型崩坏输出时某顶层部分可能写成字符串/数组而非对象，
    # 下游 repair/validate 统一用 `(plan[k] or {}).get(...)` 访问，truthy 字符串会穿过
    # `or {}` 直接炸 AttributeError。这里强制五部分都是 dict（不是 dict 就置空 {}），
    # P1 校验会因空 dict falsy 报「缺 k」走违规反馈重生成，而不是吞成异常重试。
    for _k in _L3_PLAN_PARTS:
        if not isinstance(plan.get(_k), dict):
            plan[_k] = {}
    return plan


_L3_JU_SPLIT_RE = re.compile(r"(?:\.{2,}|…|～|~)")

# L3 Plan 五个顶层部分（P1 完整性校验口径）
_L3_PLAN_PARTS = ("1_immutable_contract", "2_current_problems", "3_new_dramatic_design",
                  "4_outcome_mapping", "5_continuity_check")


def _l3_plan_flatten_enabled() -> bool:
    """L3 Plan「五部分嵌套错位」拍平开关（默认开）。置 0 即回到 2026-10-16 前行为（env 回滚）。"""
    return (os.environ.get("NWW_L3_PLAN_FLATTEN", "1") or "1").strip().lower() not in (
        "0", "false", "off", "no", "")


def _l3_flatten_plan(plan: dict) -> dict:
    """把模型「层层嵌套」写成链式的五部分拍平成五个顶层键（2026-10-16 新增，只增不改）。

    病因（实测《盗天藏书客》第 1 章 2026-10-16 14:56 重写）：
      模型生成长 JSON 时丢了括号层级，输出成
        {"1_immutable_contract": {…, "2_current_problems": {…, "3_new_dramatic_design": {…}}}}
      或 {"1_immutable_contract": {…}, "2_current_problems": {…, "3_…": {…, "4_…": {…}}}}
      **五部分内容全在、只是挂错层级** → P1 报「缺 3/4/5」→ 三次重试里两次栽在这。
    本函数只在顶层缺部分键时动手：深度下钻，把命中的部分键提升到顶层并从原父级摘除
    （摘除后继续下钻被提升的对象，故链式嵌套可逐层剥开）。
    1_immutable_contract 的合法子键（canon/chapter_objective/required_outcomes/
    causal_dependencies/conflict_registry）不是部分键，不受影响；
    顶层本就平铺时**原样返回**，行为零变化。"""
    if not _l3_plan_flatten_enabled() or not isinstance(plan, dict):
        return plan
    if all(k in plan for k in _L3_PLAN_PARTS):
        return plan  # 本就平铺
    found: dict = {}

    def dig(node, depth: int) -> None:
        if depth > 6 or not isinstance(node, dict):
            return
        for k in list(node.keys()):
            v = node.get(k)
            if k in _L3_PLAN_PARTS and k != "1_immutable_contract" and k not in plan:
                found.setdefault(k, v)
                node.pop(k, None)
                dig(v, depth + 1)  # 链式嵌套：继续剥
                continue
            dig(v, depth + 1)

    # 从整个 plan 下钻（不能只钻 1_immutable_contract：实测有 Plan 把 3/4/5 挂在
    # 顶层 2_current_problems 底下）。
    dig(plan, 0)
    for k in _L3_PLAN_PARTS:
        if k not in plan and k in found:
            plan[k] = found[k]
    # 次级修复：1_immutable_contract 的必需子键 conflict_registry 也被挂错层级
    # （实测被塞进 causal_dependencies）。仅当它存在但挂错时才提升，绝不凭空补造。
    _ic = plan.get("1_immutable_contract")
    if isinstance(_ic, dict) and not isinstance(_ic.get("conflict_registry"), dict):
        _cr = _l3_find_nested_key(_ic, "conflict_registry")
        if isinstance(_cr, dict):
            _ic["conflict_registry"] = _cr
    return plan


def _l3_find_nested_key(node, key: str, depth: int = 0):
    """在嵌套 dict 里找第一个名为 key 的 dict 值并**从原父级摘除**（返回 None 表示没有）。
    只服务 _l3_flatten_plan 的 conflict_registry 层级修复，不做通用用途。"""
    if depth > 6 or not isinstance(node, dict):
        return None
    for k in list(node.keys()):
        if k == key and isinstance(node.get(k), dict):
            return node.pop(k)
        r = _l3_find_nested_key(node.get(k), key, depth + 1)
        if r is not None:
            return r
    return None


def _l3_plan_json_rescue_enabled() -> bool:
    """L3 Plan JSON「碎片输出」救援开关（默认开）。置 0/off/false 即回到 2026-10-16 前的
    「第一个 { → 最后一个 }」单一解析行为（env 回滚通道）。"""
    return (os.environ.get("NWW_L3_PLAN_JSON_RESCUE", "1") or "1").strip().lower() not in (
        "0", "false", "off", "no", "")


def _l3_extract_plan_json(cleaned: str) -> dict | None:
    """从模型输出中打捞 L3 Plan 对象（2026-10-16 新增，只增不改）。

    原解析取 `cleaned.find("{") → cleaned.rfind("}")` 一刀切。当模型「边想边吐」——
    先零散输出若干子对象（canon/chapter_objective、allowed_sources、node/evidence_hint×6、
    opening/conflict…），最后才输出完整 Plan——两端之间夹着整段碎片，
    json.loads 必然语法错，白扔一次重试机会（实测 2026-10-16《盗天藏书客》第 1 章
    尝试1：raw 12112 字符里含 9 个片段 + 1 份完整 Plan，报
    「Expecting property name ... char 4606」）。

    本函数扫描全文所有可被 raw_decode 完整解析的顶层对象，取**含 1_immutable_contract
    且最长**的那一个。仅在主解析抛 JSONDecodeError 后调用：
    正常单对象输出走原路径，行为零变化。"""
    if not _l3_plan_json_rescue_enabled():
        return None
    dec = json.JSONDecoder()
    best_len, best_obj = 0, None
    pos, n = 0, len(cleaned or "")
    while pos < n:
        i = cleaned.find("{", pos)
        if i < 0:
            break
        try:
            obj, end = dec.raw_decode(cleaned, i)
        except Exception:  # noqa: BLE001
            pos = i + 1
            continue
        if isinstance(obj, dict) and "1_immutable_contract" in obj and (end - i) > best_len:
            best_len, best_obj = end - i, obj
        pos = end if end > i else i + 1
    return best_obj


def _l3_p8_segment_match_enabled() -> bool:
    """P8 据短语「跳接摘录」宽容校验开关（默认开）。置 0/off/false 即回到 2026-10-16 前
    的整串逐字子串匹配行为（env 回滚通道）。"""
    return (os.environ.get("NWW_L3_P8_SEGMENT_MATCH", "1") or "1").strip().lower() not in (
        "0", "false", "off", "no", "")


def _l3_ju_all_segments_present(jus: list[str], base_norm: str) -> bool:
    """P8 据短语「跳接摘录」核验（2026-10-16 新增，只增不改）。

    背景：模型习惯把原文两段用省略号连接当成一个据短语，例如
      「别人修仙靠争夺灵脉法宝...他修仙靠捡拾」
    而《盗天藏书客》第 1 章原文确有整句
      「别人修仙靠争夺灵脉法宝，他修仙靠捡拾被世人踩在脚下的废纸。」
    P8 原判据是「据短语整串逐字子串命中」，省略号跳接必然落空 → 判「锚点造假」→
    三次重试全废（实测 2026-10-16 第 1 章重写：尝试1 JSON 语法错、尝试2/3 均本项误杀）。

    本函数**只解跳接这一种形式**：按省略号切分后，要求**每一段都**在原文∪细纲命中才放行。
    纯发明（如「地下交易」「地下渠道」）任一段都找不到 → 维持 BLOCK，判据未放松。
    无分隔符的据短语（切分后仍是整串）走原语义，行为完全不变。"""
    if not _l3_p8_segment_match_enabled():
        return False
    for ju in jus or []:
        segs = [s for s in (x.strip() for x in _L3_JU_SPLIT_RE.split(str(ju or ""))) if len(s) >= 2]
        if len(segs) < 2:
            # 无跳接 → 不是本函数管辖的形式，交回原判据（= 不放行）
            continue
        if all(_l3_norm(s) in base_norm for s in segs):
            return True
    return False


# P8 据短语确定性校准（2026-10-24，对齐 P9「原句照抄承接」先例）：
# 模型近义改写（实测「店内三步以内禁武」vs 原文「铺里三步以内禁武」）在据短语
# 逐字核验下判造假，且三次尝试各死不同检查（P5→P3→P8）全链失败。处置：验证前
# 由系统在原文∪细纲中寻找相似度 ≥0.8 的真实连续子串，照抄替换据短语（映射回
# 原文字面，替换后按构造必然逐字存在）——P8 反幻觉判据零放松：纯发明无近句
# 可校准，维持 BLOCK。允许替换字符数 = 1（短）/ 2（≥8 字），硬上限防大改。
# NWW_P8_CALIBRATE=0 可关（回退纯逐字判据）。
def _l3_calib_enabled() -> bool:
    return os.environ.get("NWW_P8_CALIBRATE", "1") != "0"


def _l3_norm_map(s: str) -> tuple[str, list[int]]:
    """去空白归一化 + 每个归一化字符的原始下标映射（校准替换需还原原文字面）。"""
    _nc: list[str] = []
    _im: list[int] = []
    for _i, _ch in enumerate(str(s or "")):
        if not _ch.isspace():
            _nc.append(_ch)
            _im.append(_i)
    return "".join(_nc), _im


def _l3_closest_verbatim(phrase: str, corpus_raw: str) -> str | None:
    """在语料原文字面中定位与 phrase 最相近的连续子串（替换字符数口径）。
    窗口长度 ±1、步进 1；允许的字符替换数 = 1（短短语）/ 2（≥8 字短语），
    低于该重叠度（如纯发明短语）返回 None → 交 P8 原判 BLOCK。
    ★数字漂移硬闸（2026-10-24 自审）：若校准后的短语在数字位置/数值上与原始
    短语不一致（模型用「三天」近义改写「四天」），即使字面相似也拒绝校准，
    交 P8 判造假——数字/时间单位走 Gate D 而非 P8 缓冲，不能在此越权。"""
    _pn, _ = _l3_norm_map(phrase)
    if len(_pn) < 3:
        return None
    _cn, _im = _l3_norm_map(corpus_raw)
    if len(_pn) + 1 > len(_cn):
        return None
    _allowed = 2 if len(_pn) >= 8 else 1
    _best: str | None = None
    _bm = 10 ** 9
    for _wl in (len(_pn), len(_pn) + 1, len(_pn) - 1):
        if _wl < 3 or _wl > len(_cn):
            continue
        for _i in range(0, len(_cn) - _wl + 1):
            _w = _cn[_i:_i + _wl]
            _m = _l3_ju_match_total(_pn, _w)
            _mism = max(len(_pn), _wl) - _m
            if _mism < _bm:
                _bm = _mism
                _best = corpus_raw[_im[_i]:_im[_i + _wl - 1] + 1]
                if _mism == 0:
                    return _best
    if _bm > _allowed or _best is None:
        return None
    # ★ 安全守卫：替换集中在数字/时间词上 → 拒（不改事实语义）
    _ph_n, _ = _l3_norm_map(phrase)
    _be_n, _ = _l3_norm_map(_best)
    if _ph_n == _be_n:
        return None  # 逐字命中，已在上档 return，防重入
    # 计算两个字符串的差异字符集
    _diff_chars: set[str] = set()
    _min_len = min(len(_ph_n), len(_be_n))
    for _a, _b in zip(_ph_n, _be_n):
        if _a != _b:
            _diff_chars.add(_a)
            _diff_chars.add(_b)
    # 超长串取首尾截断后比：短对短
    if len(_ph_n) != len(_be_n):
        for _k in range(max(0, _min_len - 1), _min_len):
            if _ph_n[:_k] != _be_n[:_k]:
                _diff_chars.add(_ph_n[_k])
                _diff_chars.add(_be_n[_k])
                break
    # 若差异全落入数字/时间词范畴 → 拒
    _time_units = set("零一二三四五六七八九十百千万亿分秒时分天年月日夜日季春秋冬春夏秋黄昏凌晨午晚两日")
    _digits = set("0123456789")
    if all(c in (_time_units | _digits) for c in _diff_chars):
        return None  # 纯数字/时间单位替换 → 按 P8 原版 BLOCK，不校准
    return _best


def _l3_ju_match_total(a: str, b: str) -> int:
    """SequenceMatcher 匹配块总长（字符重叠度的绝对口径，配合替换数阈值用）。"""
    return sum(bl.size for bl in difflib.SequenceMatcher(None, a, b).get_matching_blocks())


def _repair_l3_ju_phrases(plan: dict, original: str, outline: str) -> list[str]:
    """据短语确定性校准：据短语在原文∪细纲逐字落空时，用 _l3_closest_verbatim
    找原文真实子串照抄替换 objective 里的该短语（只动据括记内短语，其余零改动）。
    返回校准记录（入 plan_history 供回执透出）。逐字已存在的据短语零触碰。"""
    if not _l3_calib_enabled() or not (original or outline):
        return []
    corpus = f"{original}\n{outline}" if outline else str(original)
    _nc_corpus, _ = _l3_norm_map(corpus)
    notes: list[str] = []
    _ic = plan.setdefault("1_immutable_contract", {})
    _objs = _ic.get("chapter_objective") or []
    for _oi, _os in enumerate(_objs):
        _os = str(_os)
        if not _os:
            continue
        _ju = (re.findall(r"据[：:]\s*[“”\"'「『]([^“”\"'」』]{2,30})[””\"'」』]", _os)
               or re.findall(r"据[：:]\s*([^“”\"'」』（）()，。；、\s]{2,30})(?=[）)，。；\s]|$)", _os))
        for _ph in _ju:
            if _l3_norm(_ph) in _nc_corpus:
                continue  # 逐字已在，无需校准
            _repl = _l3_closest_verbatim(_ph, corpus)
            if not _repl or _l3_norm(_repl) == _l3_norm(_ph):
                continue
            _new = _os.replace(_ph, _repl, 1)
            if _new == _os:
                continue
            _objs[_oi] = _new
            _os = _new
            notes.append(f"「{_ph[:14]}」→「{_repl[:14]}」（原文校准）")
    return notes


def _validate_l3_plan(plan: dict, original: str = "", outline: str = "") -> list[str]:
    """L3 Plan 确定性校验（P1~P9，S2.2 版）：
    P1 五部分完整 / P2 R 全覆盖 / P3 每事件 serves / P4 source 合法且不吸收 Audit /
    P5 conflict_registry unresolved 且创作节无数值泄漏无裁决 / P6 后章依赖 / P7 新增事件不破 Canon /
    P8 章节功能事实锚定（2026-09-22 新增）：chapter_objective 锚点在原文全部无据 → Plan 发明了
    原文不存在的剧情功能 → 违规触发 Plan 重生成，防止 Writer 收到「落地不存在的功能」矛盾指令
    （实测案例：38 章二次重写 Plan 随机生成「建立后续地下交易模式的必要性」，原文无「交易」，
    Writer 三次编译无法落地，第三次复读原文 → F_ZeroChange BLOCK 全链失败）。
    P9 细纲时间事实入合同（2026-09-22 新增）：细纲（章节任务权威层）中的剧情时间数值
    必须承接进 Plan 合同字段，否则守卫清单「允许写出的时间事实」缺项，Writer 按细纲
    行文必然撞 Gate D fail-closed（40 章实测：Plan p_t 为空集，Writer 三次编译均因
    3天/40年 漂移 BLOCK；上一版 Plan 有 3天/40年 即通过）。"""
    vio: list[str] = []
    for k in ("1_immutable_contract", "2_current_problems", "3_new_dramatic_design",
              "4_outcome_mapping", "5_continuity_check"):
        v = plan.get(k)
        if not v:
            vio.append(f"P1 缺 {k}")
        elif not isinstance(v, dict):
            # 2026-11-06 防护：模型崩坏输出时某部分可能写成字符串而非对象，
            # P1 只判 truthy 会放行，后续 .get() 连环炸 AttributeError。
            # 这里把类型错误拦在 P1，走违规反馈重生成，而不是吞成一次异常重试。
            vio.append(f"P1 {k} 类型错误：应为对象，实际为 {type(v).__name__}")
    if vio:
        return vio
    mm = plan["4_outcome_mapping"] or {}
    if not mm:
        vio.append("P2 outcome_mapping 为空：Required Outcomes 无节点承载")
    ro_raw = (plan["1_immutable_contract"] or {}).get("required_outcomes") or []
    # 2026-11-06 防护：模型崩坏输出时 required_outcomes 数组可能混入裸字符串
    # （如 ["R1", "R2", "desc", "hard"]），直接 .get() 会炸 AttributeError 吞掉整个 Plan 重试。
    # 过滤出 dict 元素；非 dict 条目计为违规（触发重生成，带反馈）。
    ro = [r for r in ro_raw if isinstance(r, dict)]
    if len(ro_raw) != len(ro):
        vio.append(f"P2 required_outcomes 含 {len(ro_raw) - len(ro)} 个非对象条目（必须是 {{id,desc,hard}} 对象数组）")
    if len(ro) < 5:
        vio.append(f"P2 Required Outcomes 过少（{len(ro)} < 5）：Plan 裁剪了必须落地的结果")
    for r in ro:
        rid = str(r.get("id") or "")
        if rid and rid not in mm:
            vio.append(f"P2 {rid} 无 outcome_mapping 节点")
    evs_raw = (plan["3_new_dramatic_design"] or {}).get("event_organization") or []
    evs = [e for e in evs_raw if isinstance(e, dict)]
    if len(evs_raw) != len(evs):
        vio.append(f"P3 event_organization 含 {len(evs_raw) - len(evs)} 个非对象条目（必须是 {{id,type,desc,serves}} 对象数组）")
    if not evs:
        vio.append("P3 event_organization 为空：L3 Plan 未设计事件组织")
    for e in evs:
        if not (e.get("serves")):
            vio.append(f"P3 事件 {e.get('id')} 无 serves")
    allowed = {"prose_execution", "dramatic_design", "pacing", "character_behavior", "information_release"}
    cp_raw = (plan["2_current_problems"] or {}).get("problems") or []
    cp = [p_ for p_ in cp_raw if isinstance(p_, dict)]
    if len(cp_raw) != len(cp):
        vio.append(f"P4 problems 含 {len(cp_raw) - len(cp)} 个非对象条目（必须是 {{id,source,severity,description}} 对象数组）")
    for p_ in cp:
        if p_.get("source") not in allowed:
            vio.append(f"P4 问题 {p_.get('id')} source 出界：{p_.get('source')}")
        if re.search(r"细纲与正文|数字冲突|体系混乱|三百份|五百份|八灵石|十块灵石|二十五天|二十天",
                     str(p_.get("description") or "")):
            vio.append(f"P4 问题 {p_.get('id')} 混入 C8/Audit 冲突")
    cr = (plan["1_immutable_contract"] or {}).get("conflict_registry")
    if not (isinstance(cr, dict) and cr.get("status") == "unresolved"
            and cr.get("adjudicator") == "AUDIT_DOMAIN"):
        vio.append("P5 conflict_registry 缺失或未 unresolved")
    scope = json.dumps({k: plan.get(k) for k in
                        ("2_current_problems", "3_new_dramatic_design", "4_outcome_mapping", "5_continuity_check")},
                       ensure_ascii=False)
    for pat in (r"三百份|五百份|300份|500份", r"八灵石|八块灵石|十灵石|十块灵石",
                r"二十五天|二十五日|二十天|二十日"):
        if re.search(pat, scope):
            vio.append("P5 Plan 创作节声明 C8 数值")
            break
    if re.search(r"(采用|选用|选择|以.{0,6}为准|确定用)[^，。]{0,12}(?:三百|五百|300|500|八|十)",
                 scope):
        vio.append("P5 Plan 出现 C8 裁决性表述")
    _cc = plan.get("5_continuity_check") or {}
    # 净化器幂等：校验前先剥离可确定性修复的元语言占位（与生成侧同口径），
    # P10 只判净化后仍不合格的条目 → 反馈列表精确、不重复阻塞已可修复项。
    _l3_repair_hard_dep_hygiene(plan)
    post = " ".join(str(x) for x in (_cc.get("hard_dependencies") or _cc.get("post_dependencies") or []))
    if not post:
        vio.append("P6 无 hard_dependencies/post_dependencies")
    # P10 硬事实契约锚定（2026-11-06）：hard_dependencies 会被 D 门逐条对照正文硬校验，
    # 其内容必须在生成侧就与「原文∪细纲」对齐、且不含 C8/执行参数/台词等不可落地成分。
    # 与 P8 同口径，不放宽 D 门；不合格条目回到 Plan 重生成（最多 3 次，带违规反馈）。
    vio.extend(_l3_hard_dep_grounding_violations(plan, original, outline))
    for e in evs:  # evs 已在上游过滤为纯 dict 列表
        if e.get("type") == "新增" and re.search(
                r"灵根|改宗|背叛|另一宗门|已有供货商|卧底|修为", str(e.get("desc") or "")):
            vio.append(f"P7 新增事件 {e.get('id')} 触碰 Canon")
    # P8 章节功能事实锚定（2026-09-22 新增；同日修复：锚定基准=原文∪细纲）：与 Gate B
    # 同口径（同 anchors 提取、同 _l3_anchor_ok、同 OR 语义），判定对象是「原文∪细纲」
    # 而非新稿——objective 的全部候选锚点在双源都判 BLOCK = 该功能脱离本章剧情事实
    # （多为把后续章功能提前引入），让 Plan 重新归纳，而不是压 Writer 编造不存在的剧情。
    # 基准修正缘由（40 章实测）：原稿跑题时（焦匾内容 vs 细纲「凡气能攻」），Plan 按细纲
    # 重新对齐是权威层正确行为，仅锚定原文必死锁；39 章「地下交易」案例原文与细纲皆无，
    # 双源锚定后仍被拦——反发明能力保留，原文-细纲脱节场景放行。
    if original:
        _on_p8 = _l3_norm(original + ("\n" + outline if outline else ""))
        for o in (plan.get("1_immutable_contract") or {}).get("chapter_objective") or []:
            _os8 = str(o)
            # 2026-10-10 据合同核验（用户裁决：P8 判据不放松，管控前置到生成侧）：
            # objective 必须自带（据：「原文/细纲逐字短语」）作为功能证据锚点。
            # 据短语造假 → BLOCK（新增，净更严）；据核验通过仅解决「提取器找不到锚」
            # 的误杀（如修辞成语），不可豁免锚点提取发现的发明（锚点全 BLOCK 仍拦）。
            _ju8 = (re.findall(r"据[：:]\s*[“”\"'「『]([^“”\"'」』]{2,30})[””\"'」』]", _os8)
                    or re.findall(r"据[：:]\s*([^“”\"'」』（）()，。；、\s]{2,30})(?=[）)，。；\s]|$)", _os8))
            if _ju8:
                if not any(_l3_norm(j) in _on_p8 for j in _ju8):
                    # ★跳接摘录不再判造假（2026-10-16）：整串落空时先按省略号切段核验，
                    #   每段都在原文∪细纲才放行；纯发明仍 BLOCK（详见 _l3_ju_all_segments_present）。
                    if not _l3_ju_all_segments_present(_ju8, _on_p8):
                        vio.append(f"P8 章节功能证据锚点造假（据短语不在原文/细纲，须逐字摘录）：「{_ju8[0][:20]}」")
                        continue
                # 据短语是引证不是主张：把整个（据：「…」）标记从锚点提取输入中剔除。
                # ①防「真据短语 rescue 发明主张」（据「独家手艺」会把「地下交易」发明
                # 的锚点集 OR 救活）；②防据： 的冒号触发提取器冒号切片，把据标记前的
                # objective 正文整体切丢（2026-10-10 实测：仅剔短语残留「据：「」」仍致
                # 切片，锚点集恒空、发明漏拦）。
                _os8_anchor = re.sub(r"[（(]?据[：:]\s*[“”\"'「『][^“”\"'」』]{2,30}[””\"'」』][）)]?", "", _os8)
                _os8_anchor = re.sub(r"[（(]?据[：:]\s*[^“”\"'」』（）()，。；、\s]{2,30}[）)]?", "", _os8_anchor)
            else:
                _os8_anchor = _os8
            anchors = _l3_outcome_anchors(_os8_anchor)
            if not anchors:
                # 2026-10-10 收紧（同上裁决）：无据也无提取锚点不再静默放行——该 objective
                # 无任何可核验证据，要求重生成并按格式补据（feedback 明示补救路径）。
                if not _ju8:
                    vio.append(f"P8 章节功能缺证据锚点（末尾补（据：「原文/细纲中的短语」））：「{_os8[:20]}」")
                continue
            if all(_l3_anchor_ok(a, _on_p8, _os8) == "BLOCK" for a in anchors):
                vio.append(f"P8 章节功能脱离原文事实（原文无任何证据锚点）：「{_os8[:20]}」")
    # P9 细纲时间事实入合同（2026-09-22 新增；同日修复：纯校验→确定性承接）：细纲是
    # 章节任务的权威层，其时间数值（如「练了三天」「修了四十年」「修了三十年杀气」）
    # 必须承接进 Plan 合同字段——否则守卫清单第 6 条「允许写出的 Plan 合同时间事实」
    # 缺项，Writer 按细纲行文必然撞 Gate D fail-closed。
    # 追索集 = 细纲时间事实（生成可见切片）- 原文时间事实 - Plan 合同时间事实；
    # conflict_registry 的 C8 days 变体排除（交易参数走 c8_param 执行口径，非剧情时间）。
    # 承接方式修正（40 章实测：三次尝试「三天/四十年」均承接、配角「三十年杀气」系统性
    # 回避，纯校验+重试无法收敛）：发现缺失时由系统直接从细纲提取含该时间数值的原句，
    # 以「时间事实（细纲承接）：{原句}」追加进合同 canon（原句照抄、语义零改变），
    # 注入后复检；仅在细纲中找不到原句（理论不可能）时才 BLOCK。幂等：注入后
    # _p_t9 含该值，重复调用不再追加；同句条目已存在时跳过。
    if outline:
        _ol_t9 = _extract_time_facts(outline)
        _p_t9 = _l3_plan_time_facts(plan)
        _a_t9 = _extract_time_facts(original)
        _cr9 = (plan.get("1_immutable_contract") or {}).get("conflict_registry") or {}
        _c8d9 = {str((v_.get("values") or {}).get("days"))
                 for v_ in (_cr9.get("variants") or []) if isinstance(v_, dict)}
        _miss9 = [(v_, u_) for (v_, u_) in sorted(_ol_t9 - _p_t9 - _a_t9)
                  if not (u_ == "天" and str(v_) in _c8d9)]
        if _miss9:
            _ic9 = plan.setdefault("1_immutable_contract", {})
            if not isinstance(_ic9.get("canon"), list):
                _ic9["canon"] = []
            for v_, u_ in _miss9:
                _sent9 = _outline_time_sentence(outline, v_, u_)
                if not _sent9:
                    continue
                _entry9 = f"时间事实（细纲承接）：{_sent9}"
                if not any(str(_c9) == _entry9 for _c9 in _ic9["canon"]):
                    _ic9["canon"].append(_entry9)
            _p_t9 = _l3_plan_time_facts(plan)
            _miss9 = [(v_, u_) for (v_, u_) in sorted(_ol_t9 - _p_t9 - _a_t9)
                      if not (u_ == "天" and str(v_) in _c8d9)]
            if _miss9:
                vio.append("P9 细纲时间事实未入 Plan 合同（合同字段必须原样承接细纲时间数值）："
                           + "、".join(f"{v_}{u_}" for v_, u_ in _miss9[:5]))
    return vio


def _l3_norm(s: str) -> str:
    return re.sub(r"\s+", "", str(s or ""))


# ---------------------------------------------------------------------------
# L3 Plan 确定性回修（2026-11-06，对接 D_Continuity / C_RequiredOutcomes 误 BLOCK）
# 根因：L3 Planner（agnes）生成的 5_continuity_check.hard_dependencies 与
# required_outcomes.desc 常出现三类事实错误，而这些错误会被 D / C 门正确拦截，
# 导致章节反复编译失败。此处做确定性回修（fail-closed：只纠正事实，不削弱闸门）。
#   ① hard_dependencies 主体颠倒：把「王瘸子付款」写成「林小凡支付」（收款/付款方倒置）；
#   ② hard_dependencies 凭空细节：编造细纲/原章未出现的具物或数量（如「半桶清水」）；
#   ③ hard_dependencies 悬念闭合：细纲章尾钩子为开放悬念（验货队尚在路上/倒计时刚开始），
#      hard_dependencies 却写成已闭合事实（已出现在巷口/倒计时结束）；
#   ④ required_outcomes.desc 立场断言：Planner 自由发挥加「拒绝妥协」等细纲从未要求的立场，
#      C 门否定结果硬检查在正文合法但未逐字写出「不妥协」时误 BLOCK。
# 这些回修只改「与细纲/原章事实不符」的表述，细纲确实声明的立场/事实原样保留。
# ---------------------------------------------------------------------------
_L3_NEG_PREFIX = r"(?:拒绝|不接受|坚持不|绝不|不)"
_L3_NEG_NOUN = r"(独家|赊账|妥协|让步)"
_L3_NEG_RE = re.compile(r"[，,。；]?\s*" + _L3_NEG_PREFIX + _L3_NEG_NOUN + r"\s*[，,。；]?")


def _l3_repair_r_negation_grounding(plan: dict, outline: str = "") -> list[str]:
    """剥离 R desc 中细纲（outline）未背书的「拒绝/不 X」立场断言从句（X∈独家/赊账/妥协/让步）。

    返回被剥离的条目清单（空=无需修复）。不削弱 C 门——细纲确实声明的立场断言原样保留，
    C 门的否定结果硬检查仍按原逻辑校验；只纠正 Planner 的自由发挥。"""
    fixed: list[str] = []
    out = outline or ""
    ro_raw = (plan.get("1_immutable_contract") or {}).get("required_outcomes") or []
    # 2026-11-06 防护：非 dict 条目跳过（崩坏输出时可能混入裸字符串）
    ros = [r for r in ro_raw if isinstance(r, dict)]
    for r in ros:
        desc = str(r.get("desc") or "")
        new_desc = desc
        for m in _L3_NEG_RE.finditer(desc):
            noun = m.group(1)
            # 细纲是否也声明了「拒绝/不 noun」？未声明 = Planner 自由发挥 → 剥离
            if not re.search(_L3_NEG_PREFIX + re.escape(noun), out):
                new_desc = new_desc.replace(m.group(0), "", 1)
                fixed.append(f"{r.get('id')}: 剥离未落细纲的否定断言「{m.group(0).strip()}」")
        if new_desc != desc:
            new_desc = re.sub(r"[，,。；]\s*[，,。；]", "，", new_desc)
            new_desc = re.sub(r"^\s*[，,。；]\s*", "", new_desc).strip()
            r["desc"] = new_desc
    return fixed


def _l3_repair_continuity_facts(plan: dict, outline: str = "") -> list[str]:
    """回修 5_continuity_check.hard_dependencies 的三类事实错误（①②③），返回修复清单。"""
    fixed: list[str] = []
    cc = plan.get("5_continuity_check") or {}
    hard = cc.get("hard_dependencies")
    if not isinstance(hard, list):
        return fixed
    out = outline or ""
    state = cc.get("chapter_state_changes") or []
    # ③ 开放悬念检测：细纲章尾钩子/结尾声明验货队/倒计时为开放态
    _open_hook = bool(re.search(r"集结途中|尚未抵达|开始|逼近|脚步声尚远|正沿.{0,4}逼近|集结", out))
    new_hard = []
    for h in hard:
        h0 = h
        # ① 付款主体颠倒：hard_dependencies 说林小凡支付，但细纲 R1 明确王瘸子支付和解费
        if re.search(r"林小凡[^，。；]*?(?:已支付|支付|付下品灵石|付款|付给|付了)", h) and \
           re.search(r"王瘸子[^，。；]*?(?:支付|付|掏出|丢|拍|拍在)", out):
            h = re.sub(r"林小凡(?:已支付|支付|付下品灵石|付款|付给|付了)", "王瘸子支付", h)
            fixed.append(f"hard_dependencies 付款主体回修：「{h0}」→「{h}」")
        # ② 凭空细节：剥离细纲/状态变化均未出现的具物细节（如「半桶清水」）
        if "半桶清水" in h and "半桶清水" not in out and not any("半桶清水" in s for s in state):
            h = h.replace("与半桶清水", "").replace("半桶清水", "")
            h = re.sub(r"[，,。；]\s*[，,。；]", "，", h).strip("，,。； ")
            if h != h0:
                fixed.append(f"hard_dependencies 剥离凭空细节：「{h0}」→「{h}」")
        # ③ 悬念闭合：细纲为开放态时，把已闭合断言改写为开放态
        if _open_hook:
            _h = h
            h = re.sub(r"已出现在巷口", "脚步声逼近巷口（悬念铺垫）", h)
            h = re.sub(r"已到达巷口", "脚步声逼近巷口（悬念铺垫）", h)
            h = re.sub(r"已出发并到达[^，。；]*", "已出发并正沿巷口方向逼近（悬念铺垫）", h)
            h = re.sub(r"倒计时结束", "倒计时开始", h)
            if h != _h:
                fixed.append(f"hard_dependencies 悬念开放化：「{h0}」→「{h}」")
        # ④ 方向/到达语义矛盾：「已移动至 X 方向」把进行态写成到达态，而「方向」本身
        #    只表朝向、不表抵达。D 门 LLM 逐字核查时会判「尚未到达」→ 误 BLOCK
        #    （实测《人间烟火道》第1章「主角已移动至长街西头方向」vs 正文「往长街西头走」）。
        #    只把「移动至/抵达 X 方向」改写为进行态，事件与目的地原样保留。
        _hd = h
        h = re.sub(r"已(?:移动|走|赶|行|移)至([^，。；]{1,16}?)方向", r"已向\1方向移动", h)
        h = re.sub(r"已(?:抵达|到达|走到)到?([^，。；]{1,16}?)方向", r"已向\1方向移动", h)
        if h != _hd:
            fixed.append(f"hard_dependencies 方向语义回修：「{_hd}」→「{h}」")
        new_hard.append(h)
    cc["hard_dependencies"] = new_hard
    return fixed


# ---------------------------------------------------------------------------
# hard_dependencies 契约卫生（2026-11-06，对接 D_Continuity 结构性误 BLOCK）
# 根因：5_continuity_check.hard_dependencies 是「本章已落地、下一章必须承接的
# 确定性事实」契约字段，D 门会逐条让外脑对照正文核查。但 _validate_l3_plan
# (P1~P9) 对它只做 P6「非空」检查，从不核内容——Planner 于是把生成侧元语言
# （「需落地 C8 数值」「由执行参数决定」）、主体颠倒与错配引语写进硬事实清单。
# 实测 task 6f2148a8a611：Writer 三次编译后 D 门正确判两条「未落实」→ 回滚，
# 重写失败；放过去等于删闸门，故根治点在 Plan 生成侧，不在 D 门。
# 做法（fail-closed，只收紧生成侧，D 门判定逻辑零改动）：
#   ① 确定性净化：剥离残留的 C8/执行参数/占位括号等元语言；
#   ② P10 结构卫生校验：逐条拦下「元语言占位 / 主体引语错配 / 凭空具名实体」，
#      不合格 → Plan 重生成（带违规反馈，最多 3 次）。语义事实是否落地仍由
#      D 门 LLM 核查（不用 2-gram 阈值，跨书实测会误杀合法同义改写）。
# ---------------------------------------------------------------------------
_L3_HARD_DEP_META_PAREN = re.compile(
    r"[（(][^)）]{0,40}?"
    r"(?:C8|执行参数|数值|待定|占位|待补|需落地|由 Writer|由执行|不预设|Plan)"
    r"[^)）]{0,40}?[)）]"
)
_L3_HARD_DEP_META_WORDS = re.compile(
    r"(需落地|待落地|待核对|需核对|待补|占位|由执行参数|由 Writer|不预设|"
    r"本章无数值|C8\s*不激活|具体.{0,6}数额由)"
)
# 错配引语：硬事实里嵌入角色台词的引号串（威胁/承诺/台词属 narrative，不是硬事实）
_L3_HARD_DEP_QUOTE = re.compile(r"[“”‘’\"'『』「」]([^“”‘’\"'『』「」]{4,40})[“”‘’\"'『』「」]")
# 尖括号示例占位（提示词示例被原样抄进 Plan，如「一笔<本章交易>已完成并记录」）
_L3_HARD_DEP_ANGLE_PLACEHOLDER = re.compile(r"<[^>]{1,20}>|＜[^＞]{1,20}＞")
# 凭空引入的具名实体/文书：hard_dependencies 里出现而「原文∪细纲」完全没有的
# 专有名词（宗门/人名/书面契约），是 Planner 发明新剧情的典型信号。纯语义改写
# 的事实不由本正则管，交给 D 门 LLM 判断（避免跨书风格差异导致假 BLOCK）。
_L3_HARD_DEP_NEW_ENTITY = re.compile(
    r"(青云宗|青禾宗|焚天阁|聚灵堂|白崇光|李四|赵五|书面订单|"
    r"书面契约|签署.{0,4}订单|签订.{0,4}契约|包月交易|记录在案|备案为)"
)
# 主体/引语错配：把「谁告知谁」写成与细纲相反的施受关系（实测：威胁由打手
# 代留，Planner 却写「林小凡已告知王瘸子」）。只对「告知/转述」+台词句生效，
# 不误伤普通的「王瘸子扬言…」类叙述。
_L3_HARD_DEP_TELL_INVERT = re.compile(r"(已告知|转告|转述|通知了|已通知)")


def _l3_repair_hard_dep_hygiene(plan: dict) -> list[str]:
    """剥离 hard_dependencies 中残留的生成侧元语言（占位括号/待落地词），返回修复清单。

    只删元语言，不动事实断言本身（有据/无据交由 P10 判定）；幂等。"""
    fixed: list[str] = []
    cc = plan.get("5_continuity_check") or {}
    hard = cc.get("hard_dependencies")
    if not isinstance(hard, list):
        return fixed
    new_hard = []
    for h in hard:
        h0 = str(h)
        h = _L3_HARD_DEP_META_PAREN.sub("", h0)
        h = _L3_HARD_DEP_META_WORDS.sub("", h)
        h = re.sub(r"[，,。；]\s*[，,。；]", "，", h).strip("，,。； ")
        if h != h0:
            fixed.append(f"hard_dependencies 剥离元语言占位：「{h0}」→「{h}」")
        new_hard.append(h)
    cc["hard_dependencies"] = new_hard
    return fixed


def _l3_hard_dep_grounding_violations(plan: dict, original: str, outline: str) -> list[str]:
    """P10：逐条核验 hard_dependencies 的契约结构卫生（原文∪细纲基准）。

    返回 P10 违规清单；空 = 契约合格。只判「生成侧不可落地的结构缺陷」：
    元语言占位、主体/引语错配、凭空具名实体。语义事实是否落地由 D 门 LLM
    核查，本函数不替代也不放宽 D 门。"""
    vio: list[str] = []
    cc = plan.get("5_continuity_check") or {}
    hard = cc.get("hard_dependencies") or cc.get("post_dependencies") or []
    base = _l3_norm((original or "") + "\n" + (outline or ""))
    for i, h in enumerate(hard, 1):
        hs = str(h or "").strip()
        if not hs:
            vio.append(f"P10 hard_dependencies #{i} 为空条目")
            continue
        # ① 元语言残留：这类条目 D 门逐字核查时永远「未落实」（占位符不可能写进正文）
        if (_L3_HARD_DEP_META_PAREN.search(hs) or _L3_HARD_DEP_META_WORDS.search(hs)
                or _L3_HARD_DEP_ANGLE_PLACEHOLDER.search(hs)):
            vio.append(f"P10 hard_dependencies #{i} 含生成侧元语言/占位：「{hs[:28]}」")
            continue
        # ② 主体/引语错配：角色台词是 narrative，写成硬事实会让 D 门去正文找这句
        #    台词；「林小凡已告知王瘸子'明早老子…'」实测主体与细纲相反（威胁由
        #    打手代留），D 门正确判「未落实」→ 回滚。只拦「告知/转述」类倒置 +
        #    引号台词，不误伤普通的「王瘸子扬言…」类叙述。
        _q = _L3_HARD_DEP_QUOTE.search(hs)
        if _q and (_L3_HARD_DEP_TELL_INVERT.search(hs)
                   or re.search(r"(威胁|台词|告知|转述|留话|声称|喊话)", hs)) \
                and re.search(r"(老子|明早|砸摊|带人)", hs):
            vio.append(f"P10 hard_dependencies #{i} 主体/引语错配（角色台词应入 narrative_dependencies）：「{hs[:28]}」")
            continue
        # ③ 凭空发明的具名实体/文书（原文∪细纲完全不存在）：这类硬事实 D 门
        #    逐字核查时必然「未落实」，是结构性死锁，必须在 Plan 阶段拦下重生成。
        #    纯语义改写的事实不做 2-gram 阈值判定（跨书实测会误杀合法同义改写），
        #    仍由 D 门 LLM 语义核查把关——P10 只补「生成侧契约卫生」，不替代 D 门。
        _m = _L3_HARD_DEP_NEW_ENTITY.search(hs)
        if _m and _l3_norm(_m.group(0)) not in base:
            vio.append(f"P10 hard_dependencies #{i} 凭空引入原文/细纲不存在的实体或文书：「{hs[:28]}」")
            continue
        # ④ 不做 P8 式「整条断言锚点全 BLOCK」判定：跨书实测（《人间烟火道》
        #    「主角当前持有辣条大半截」）合法的动作/状态同义改写会被误杀。
        #    语义是否落地仍由 D 门 LLM 逐条核查，P10 只补结构卫生、不替代 D 门。
    return vio


def _l3_cover2(seg: str, rn: str) -> float:
    """中文段 vs 归一化正文 2-gram 覆盖度（确定性，宽松：允许 L3 重新表达）。
    2026-09-22 修复（斜杠并列复合锚）：desc 中带引号的并列复合词（如 40 章 R1
    「确立'推/烧'无效的结论」）此前按整串做 2-gram——「推/」「/烧」在散文正文
    结构上不可能出现，覆盖恒 0.00，Writer 三次编译全部死锁、终判 C 门 BLOCK。
    斜杠在 desc 中是并列分隔（推、烧两种测试动作），按部分取最优覆盖：
    短部分（<3 字）做存在性判定（出现=1.0/缺失=0.0），长部分保持 2-gram 口径。
    非复合锚点路径行为不变（38/39 章已验证路径零影响）。"""
    s = _l3_norm(seg)
    if "/" in s or "／" in s:
        parts = [p for p in re.split(r"[/／]", s) if p]
        if len(parts) > 1:
            return max((1.0 if p in rn else 0.0) if len(p) < 3
                       else _l3_cover2_single(p, rn) for p in parts)
    return _l3_cover2_single(s, rn)


def _l3_cover2_single(s: str, rn: str) -> float:
    if len(s) < 3:
        # 2026-11-06 修复：短锚点必须做存在性判定（与 12011 文档契约及斜杠复合路径 1217 一致），
        # 旧实现无条件返回 1.0，导致任意 1~2 字锚点（如 买卖/成交/付/掏）即使正文完全未出现也判 PASS，
        # 使 B/C 门对短证据词彻底失守（OR 语义下等于送免费通关）。
        return 1.0 if s in rn else 0.0
    g = [s[i:i + 2] for i in range(len(s) - 1)]
    if not g:
        return 1.0
    return sum(1 for b in g if b in rn) / len(g)


def _l3_is_contrast_negative(q: str, s: str) -> bool:
    """对比修辞前项判定（2026-09-22 修复）：锚串处于「从X升级为Y / 从X变为Y /
    X不再是Y」结构的 X 位置 = 被否定/被替换的旧状态，不是本章证据。
    实例：objective「威胁从『同行竞争』升级为『组织灭杀』」——正文永不写『同行竞争』，
    它是被升级掉的旧威胁层级；此前把它当证据锚导致 B 门结构性误伤。"""
    for m in re.finditer(re.escape(q), s):
        # 剥离引号字符后判定前后文（锚点常处于「从『X』升级为『Y』」结构中，
        # 引号本身会挡住前缀「从」/后缀「升级为」的贴邻判断）
        _pre = re.sub(r"[“”‘’\"'『』「」]", "", s[max(0, m.start() - 3):m.start()])
        _post = re.sub(r"[“”‘’\"'『』「」]", "", s[m.end():m.end() + 7])
        if _pre.endswith("从") or re.match(r"^(升级为|变为|转为|改变为|变成|不再是|已不再是|退化为)", _post):
            return True
    return False


def _l3_outcome_anchors(desc: str) -> list[str]:
    """从 R.desc / Objective 提取确定性证据锚串（多候选，OR 语义由调用方判定）：
    取第一个冒号后的主体（标签词如「订单成立：/结果状态：」剥离），去括号注释，
    跳过以 不可/不得/必须/严禁/只 开头的约束段。
    2026-09-22 修复：①对比修辞前项（被否定的旧状态）过滤；
    ②引号锚点不再短路 return——与常规段锚点合并为多候选（备选证据，防单引号词绑架）。
    2026-11-05 修复：长句锚点拆核心事件词+关联词扩展，避免「首单交易已完成」
    整句 2-gram 匹配把正文已写但用词不同的事件误判为证据不足。"""
    s = str(desc or "")
    m = re.search(r"[：:]\s*([^：:]*)$", s)
    if m and len(m.group(1).strip()) >= 2:
        s = m.group(1)
    out: list[str] = []
    # Tier 1 硬证据：包含数值/具体名词/明确事件动词（2026-10-10 前移：引号路径同口径复用）
    _HARD_NUM = re.compile(r"[零一二三四五六七八九十百千万两\d]+[天日月年份块两成次件笔个声炷炷息刻]")
    _HARD_NOUNS = re.compile(r"(林小凡|温故|赵执事|陈执事|汪万金|青禾坊|杂货铺|百草堂|王瘸子|泡面|辣条|可乐|自热米饭|火锅|灵石|订单|契约|交易|合作|送货|收货|交付|采购|样品|定金|月结|供货|独家|联名|上书|聚灵堂|青禾宗|青云宗|焚天阁)")
    # 事件词→关联词扩展（长句锚点拆核心词后，正文用动作描写而非总结词时也能匹配）
    _EVENT_SYNONYMS = {
        "交易": ["买", "卖", "买卖", "成交", "生意", "开张", "第一单", "付", "掏"],
        "订单": ["下单", "订货", "合同", "契约"],
        "供货": ["送货", "交货", "补货", "发货"],
        "库存": ["存货", "最后一份", "剩", "没了", "用完"],
        "倒计时": ["开始", "启动", "一炷香", "时间", "期限"],
        "逃离": ["跑", "离开", "逃走", "消失", "走了"],
        "完成": ["做完", "搞定", "结束", "落地"],
        "支付": ["付钱", "付灵石", "掏灵石", "拍在", "放下"],
    }
    for q in re.findall(r"[“”‘’\"'『』「」]([^“”‘’\"'『』「」]{2,30})[“”‘’\"'『』「」]", s):
        if len(q) < 2 or _l3_is_contrast_negative(q, s) or q in out:
            continue
        # 2026-10-10 口径对齐（第2章 rewrite 实测）：引号锚点与常规段同 Tier-3 口径——
        # 纯修辞成语/抽象引号词（无数值无硬名词，如「守口如瓶」）不作为硬 Gate 锚点。
        if not (_HARD_NUM.search(q) or _HARD_NOUNS.search(q)):
            continue
        out.append(q)
        # 2026-11-06 修复：长引号锚点（>8字，含 Planner「（据：「整句」）」自引）与 seg 循环同口径——
        # 拆核心事件词+关联词扩展，避免整句 2-gram 把正文已写但用词不同的事件误判为证据不足。
        # （seg 循环虽有同构扩展，但本类整句以「完成/通过/展示」等抽象前缀开头会被 12100 的
        #  抽象目标段跳过，故引号路径必须自带扩展，否则锚点永远只能是逐字整句。）
        if len(q) > 8:
            _core = re.sub(r"(已完成|已记录|已消耗|已启动|已逃离|已支付|已交付|已发生|并记录|并交付|并确认|状态|结果)", "", q)
            _core = re.sub(r"^(一笔|一枚|一份|一个|最后|首单|第一|整个|全部)", "", _core)
            for _nm in re.finditer(r"(交易|订单|供货|库存|倒计时|逃离|支付|完成|合作|交付|灵石|泡面|订单)", _core):
                _n = _nm.group(1)
                if _n not in out:
                    out.append(_n)
                for _syn in _EVENT_SYNONYMS.get(_n, []):
                    if _syn not in out:
                        out.append(_syn)
    # 括号处理：含关键名词（数值/条件/结果）的括号保留（如 R3「（十块灵石/份）」「（五成）」），
    # 纯注释括号删除（如「（合作起点）」「（可优先供货换部分让步）」）
    def _keep_paren(m: "re.Match") -> str:
        inner = m.group(1)
        if re.search(r"(执行参数|保持一致|内部保持|由执行|严格停在|不得推进|不可提前)", inner):
            return ""
        if re.match(r"^(可|即|如|注|但|不得|严格|由|应|仅|只|另|参考|说明)", inner.strip()):
            return ""
        if re.search(r"(灵石|成|份|月结|天|块|两|定金|收货|交付|货物|次日|供货|订单|样品|比例|结算|采购|五百|三百|十块|八块|二十|二十五)", inner):
            return m.group(0)
        return ""
    s = re.sub(r"[（(]([^)）]*)[)）]", _keep_paren, s)
    for seg in re.findall(r"[\u4e00-\u9fff]{3,}", s):
        if re.match(r"^(不可|不得|必须|严禁|只)", seg):
            continue
        # 排除性表述（而非/不是/不靠/并非...）不是正向证据，跳过
        if re.match(r"^(而非|不是|不靠|并非|比起|相对于|区别于)", seg):
            continue
        # 抽象目标段（确立/展现/通过…开头）不是正文证据，跳过
        if re.match(r"^(确立|展现|展示|通过|完成|承接|奠定|留下|体现|彰显|强化|呼应|揭示|铺垫|证明|为后续|留出|确立并|的|达成|双方)", seg):
            continue
        # Tier 3：抽象叙事意图（无具体数值/实体/事件）→ 不作为硬 Gate
        has_num = bool(_HARD_NUM.search(seg))
        has_noun = bool(_HARD_NOUNS.search(seg))
        if not has_num and not has_noun:
            continue  # 纯抽象状态词，降级为 Review 维度，不 BLOCK
        if seg not in out:
            out.append(seg)
        # 长句锚点（>8字）额外拆核心事件词+关联词，避免整句 2-gram 误杀
        # （正文写了事件但用动作描写而非总结词，如「掏灵石拍案板」vs「首单交易已完成」）
        if len(seg) > 8:
            # 去掉状态描述词，提取核心事件名词
            _core = re.sub(r"(已完成|已记录|已消耗|已启动|已逃离|已支付|已交付|已发生|并记录|并交付|并确认|状态|结果)", "", seg)
            _core = re.sub(r"^(一笔|一枚|一份|一个|最后|首单|第一|整个|全部)", "", _core)
            # 从核心词里提取硬名词
            for noun_m in re.finditer(r"(交易|订单|供货|库存|倒计时|逃离|支付|完成|合作|交付|灵石|泡面|订单)", _core):
                _noun = noun_m.group(1)
                if _noun and _noun not in out:
                    out.append(_noun)
                # 关联词扩展：正文用动作描写时也能匹配
                for syn in _EVENT_SYNONYMS.get(_noun, []):
                    if syn not in out:
                        out.append(syn)
    return out


def _l3_guard_checklist(plan: dict, original: str, c8_param: dict, wc_target: str | int | None = None, outline: str = "") -> str:
    """L3 守卫清单前置（2026-09-22 用户裁决，评分标准前置）：
    从已批准 Plan 确定性提取 Writer 必须落地的验收清单，注入 Writer prompt，
    使第一次编译即对齐 L3 Guard 的验收口径（写的标准=查的标准）。
    提取口径与 _validate_l3_contract 同源：Gate A 实体提取、B/C/D 复用
    _l3_outcome_anchors（含对比前项过滤、OR 语义）、时间锁表复用
    _l3_plan_time_facts（Gate D L3 时间口径）、体量底线同 G_Length 可见字口径。
    wc_target（2026-09-22 字数治理）：细纲字数目标（packet.wordcount.target，CCv2 权威；
    note 扩写目标已在调用方覆盖为权威），注入清单第 7 条作为正文体量主锚——
    实测 38 章 1854/4200（44%）、40 章 2127/4300（50%）：清单只锚原文 60%
    时 Writer 以原文为体量基准，细纲三幕的展开度长期欠账。"""
    ic = (plan or {}).get("1_immutable_contract") or {}
    cc = (plan or {}).get("5_continuity_check") or {}
    lines: list[str] = ["【L3 守卫验收清单（产出正文将按此硬校验，编译前必读）】"]

    # 1. C8 执行数值（Gate E 字面口径：中文数字形式）
    v = (c8_param or {}).get("values") or {}
    if v:
        cn = {"quantity": {500: "五百份", 300: "三百份"}, "price": {10: "十块灵石", 8: "八块灵石"},
              "days": {20: "二十天", 25: "二十五天"}}
        words = [cn["quantity"].get(v.get("quantity"), f"{v.get('quantity')}份"),
                 cn["price"].get(v.get("price"), f"{v.get('price')}块灵石"),
                 cn["days"].get(v.get("days"), f"{v.get('days')}天")]
        lines.append("1.【C8 执行数值（正文中以中文数字原样写出，各至少一次）】" + "、".join(words)
                     + "；严禁出现其他数值组合（如三百份/八块灵石/二十五天）。")

    # 2. Canon 实体（Gate A 同口径）
    ents: list[str] = []
    for c in ic.get("canon") or []:
        cs = _l3_norm(c)
        if any(w in cs for w in _L3_CANON_CONSTRAINT_HINT):
            continue
        m = re.search(r"^C\d+[:：]?([\u4e00-\u9fffA-Za-z]{2,8})", cs)
        ent = m.group(1) if m else ""
        if ent in _L3_CANON_TAG_SKIP:
            m2 = re.search(r"[:：]([\u4e00-\u9fffA-Za-z]{2,8}?)(?=[只不并为与和、，。；的之])", cs)
            ent = m2.group(1) if m2 else ""
        if ent and ent not in ents:
            ents.append(ent)
    if ents:
        lines.append("2.【Canon 实体（每个都必须在正文出现）】" + "、".join(ents[:10]))

    # 3. B 门：objective 证据组（OR：每组候选词任一出现即可）
    b_groups = []
    for o in ic.get("chapter_objective") or []:
        anchors = _l3_outcome_anchors(str(o))[:3]
        if anchors:
            b_groups.append("/".join(a[:12] for a in anchors))
    if b_groups:
        lines.append("3.【章节功能证据（每组候选词任一在正文出现即可）】\n  - " + "\n  - ".join(b_groups[:6]))

    # 4. C 门：R 证据组（交易数值类 R 走 C8 清单，跳过）
    c_groups = []
    for r in ic.get("required_outcomes") or []:
        rid = str(r.get("id") or "R?")
        desc = str(r.get("desc") or "")
        if re.search(r"(单价|价格|灵石|定金|月结|结算|数量|比例|条件|供货|采购|订单|交易)", desc):
            continue
        anchors = _l3_outcome_anchors(desc)[:3]
        if anchors:
            c_groups.append(f"{rid}: " + "/".join(a[:12] for a in anchors))
    if c_groups:
        lines.append("4.【Required Outcomes 证据（每个 R 的候选词任一落地即可）】\n  - " + "\n  - ".join(c_groups[:8]))

    # 5. D 门：hard_dep 事实组（同 Gate D skip 规则）
    d_groups = []
    for post in (cc.get("hard_dependencies") or cc.get("post_dependencies") or []):
        anchors = [a for a in _l3_outcome_anchors(str(post))
                   if not re.match(r"^(次日|明日|第|当日|当天)", a)
                   and not re.match(r"^(可能|或|需|引发|产生|影响|其他|后续|为|近期|面临|带来|章需|青云宗内部|大宗门采购|凡人物品)", a)][:3]
        if anchors:
            d_groups.append("/".join(a[:12] for a in anchors))
    if d_groups:
        lines.append("5.【后章硬依赖事实（每组候选词任一可识别表述落地即可）】\n  - " + "\n  - ".join(d_groups[:6]))

    # 6. 时间锁表（Gate D L3 口径）——2026-10-24 白名单强化（第2章 rewrite 实测：
    #    模型无视「不得引入新时间数值」句式，3 连编译全漂移出 2天/20年）。
    #    改为确定性 finite 白名单：打印 原文∪Plan 合同∪细纲∪C8 全部合法「数字×单位」，
    #    并把禁止面从「除以上两组」收窄为「白名单之外的全部组合」。写手可执行的判断
    #    从开放创作收敛成closed-book 查表。
    a_t = _extract_time_facts(original)
    p_t = _l3_plan_time_facts(plan)
    o_t = _extract_time_facts(outline or "")
    _c8d = 0
    try:
        _c8d = int(((c8_param or {}).get("values") or {}).get("days") or 0)
    except (TypeError, ValueError):
        _c8d = 0
    keep = sorted(a_t & p_t)
    allowed = sorted((p_t - a_t) | o_t | (a_t - p_t))
    _tparts: list[str] = []
    if keep:
        _tparts.append("必须保留：" + "、".join(f"{v_}{u_}" for v_, u_ in keep))
    if allowed:
        _tparts.append("允许出现：" + "、".join(f"{v_}{u_}" for v_, u_ in allowed))
    if _c8d > 0:
        _tparts.append(f"C8 执行数值：{_int_to_cn(_c8d) if _c8d < 100 else str(_c8d)}天")
    if _tparts:
        lines.append("6.【时间纪律（「数字×时间单位」白名单，产出正文将按此硬校验）】"
                     + "；".join(_tparts) + "。"
                     "除以上白名单之外，任何「数字＋天/日/年/月」的组合一律不得写进正文；"
                     "需要时间概念而无法用白名单值表达时，改用无数字的模糊义表述"
                     "（如『许久』『近来』『翌日』『多时』），严禁把历史跨度写成具体数值。")

    # 7. 体量底线（G_Length 可见字口径）+ 非零变化（F）+ 细纲字数主锚（2026-09-22 字数治理）
    a_len = len(re.sub(r"\s+", "", str(original or "")))
    if a_len > 0:
        seg7 = (f"7.【体量】正文可见字数不得低于原文的 60%（原文约 {a_len} 字，下限约 {int(a_len * 0.6)} 字）；"
                "且不得与原文完全一致（必须真实再生产，非复读）。")
        try:
            _wt = int(str(wc_target).strip()) if wc_target else 0
        except (TypeError, ValueError):
            _wt = 0
        if _wt > 0:
            _hr = _wc_hardline_ratio()
            seg7 += (f" 更重要的：本章字数目标是 {_wt} 字（细纲字数目标，硬校验线为目标的 {_hr:.0%}≈{int(_wt * _hr)} 字，"
                     f"低于此线守卫将 BLOCK 打回重写）。写够字数靠对细纲每一幕、每个事件、每场对话的充分展开"
                     "（场景感、动作细节、你来我往的对白），不靠注水；不得新增细纲没有的剧情凑字。"
                     "写完自查字数，不足则继续展开细纲已有内容直至达标。")
        lines.append(seg7)
    return "\n".join(lines)


def _validate_l3_contract(original: str, rewritten: str, plan: dict, c8_param: dict, wc_target: str | int | None = None, outline: str = "") -> dict:
    """L3 Guard（确定性，fail-closed）。六 Gate：
      A Canon：实体存在 + 负向身份/规则/关系反转拦截；
      B Chapter Function：章节目标证据（宽松 2-gram）；
      C Required Outcomes：每个 R 的确定性证据锚串在正文中落地（Plan outcome → 正文证据）；
      D Continuity：时间事实（复用 _check_time_facts）+ 负向后续章节消费 + 后置依赖；
      E C8：只允许执行参数指定变体，无混用/无第三套/无数值裁决表述；
      F Zero-change：输出 == 输入 → 章节再生产未发生；
      G Length：① 输出可见字 < 原文 60% → 长度塌缩（整章压缩/丢失拦截）；
               ② 输出可见字 < 安全网下限（max(目标×70%, min(目标,2000))）→ 不可发布/整体塌缩
                  （与 Writer 扩写硬线 0.95 解耦，2026-11-06 修复达标章误 BLOCK）。
    不做逐事件比对（L3 允许重新设计事件组织）；evidence 锚点缺失的 R 记录为 BLOCK
    （驱动未来补 evidence_terms，S2.2 范畴）。"""
    rn = _l3_norm(rewritten)
    gates: dict[str, str] = {}
    vio: list[str] = []

    # ---- Gate A: Canon ----
    a_vio: list[str] = []
    seen: set[str] = set()
    for c in (plan.get("1_immutable_contract") or {}).get("canon") or []:
        cs = _l3_norm(c)
        if any(w in cs for w in _L3_CANON_CONSTRAINT_HINT):
            continue
        m = re.search(r"^C\d+[:：]?([\u4e00-\u9fffA-Za-z]{2,8})", cs)
        ent = m.group(1) if m else ""
        if ent in _L3_CANON_TAG_SKIP:
            m2 = re.search(r"[:：]([\u4e00-\u9fffA-Za-z]{2,8}?)(?=[只不并为与和、，。；的之])", cs)
            ent = m2.group(1) if m2 else ""
        if not ent or ent in seen:
            continue
        seen.add(ent)
        if ent not in rn:
            a_vio.append(f"Canon 实体「{ent}」缺失")
    # 否定词检测：检查匹配文本内是否包含否定词（如"没有灵根"）
    _NEG_WORDS = ("没", "无", "未", "不", "别", "莫", "非", "缺")
    _on = _l3_norm(original)
    for pat, name in _L3_CANON_NEG_PATTERNS:
        for m in pat.finditer(rn):
            matched_text = m.group(0)
            # 如果匹配文本本身包含否定词（如"没有灵根"），不算违规
            if any(neg in matched_text for neg in _NEG_WORDS):
                continue
            # 原文既成事实豁免（2026-09-22 修复）：同一模式原文亦命中 → 该反转/提前
            # 已是生产正文现实（前链 commit 已放行），L3 忠实 <original> 基准不算违规；
            # 只拦「原文没有、新稿新增」的 Canon 破坏（fail-closed 方向不放宽）。
            if pat.search(_on):
                continue
            a_vio.append(name)
            break
    gates["A_Canon"] = "PASS" if not a_vio else "BLOCK"
    vio.extend(a_vio)

    # ---- Gate B: Chapter Function ----
    b_vio: list[str] = []
    for o in (plan.get("1_immutable_contract") or {}).get("chapter_objective") or []:
        anchors = _l3_outcome_anchors(o)
        if not anchors:
            continue
        results = [_l3_anchor_ok(a, rn, str(o)) for a in anchors]
        # OR 语义（2026-09-22 修复）：objective 的多候选证据锚任一落地即算功能发生。
        # 此前 AND 语义要求全部锚点落地——plan 元语言词（功能描述措辞）逐一字面
        # 出现在散文正文，是 B 门结构性误伤源（如「同行竞争」）。
        if all(r == "BLOCK" for r in results):
            worst = min((_l3_cover2(a, rn), a) for a in anchors)
            b_vio.append(f"章节功能证据不足：「{worst[1][:20]}」")
    gates["B_ChapterFunction"] = "PASS" if not b_vio else "BLOCK"
    vio.extend(b_vio)

    # ---- Gate C: Required Outcomes ----
    c_vio: list[str] = []
    _L3_ABS_HEAD = r"^(确定|确立|达成|完成|暗示|展现|展示|通过|承接|奠定|留下|体现|彰显|强化|呼应|揭示|铺垫|证明|为后续|留出|的|双方|近期|需|引发|可能|或|酝酿|埋下|指向|本章|内部|并明确|明确|形成)"
    _L3_ABS_TAIL = r"(关系|意向|基调|逻辑|空间|利益|风险|压力|时间线|动态|舆论|审查|关注|态度|分歧|倾向|价值|评价|代价|局面)$"
    for r in (plan.get("1_immutable_contract") or {}).get("required_outcomes") or []:
        rid = str(r.get("id") or "R?")
        desc = str(r.get("desc") or "")
        num = re.search(r"(\d+)\s*(?:→|->|至|到|升到|升至|涨到)\s*(\d+)", desc)
        if num:
            if num.group(2) not in rn:
                c_vio.append(f"{rid} 数字目标 {num.group(2)} 未落地")
            continue
        if re.search(r"(单价|价格|灵石|定金|月结|结算|数量|比例|条件|供货|采购|订单|交易)", desc):
            _c8v = (c8_param or {}).get("values") or {}
            if not _c8v or (c8_param or {}).get("fallback"):
                # ★数值体系未在本章正文检测到（fallback）时，「要求落地交易条件数值」是
                # 不可能任务：骨架逼 Plan 写出「交易/订单」字样 → 必然命中本正则 →
                # 而 500份/10块灵石/20天 是兜底注入、本章根本不存在 → 重试必然重复 BLOCK
                # （2026-10-16 实测《盗天藏书客》第1章 R1/R3，Plan desc 自己写着「本章无交易」）。
                # fallback → 跳过数值校验；真检测到数值体系 → 校验语义完全不变。
                continue
            _c8a = []
            if _c8v.get("quantity") == 500:
                _c8a += ["五百份"]
            elif _c8v.get("quantity") == 300:
                _c8a += ["三百份"]
            if _c8v.get("price") == 10:
                _c8a += ["十块灵石", "十块"]
            elif _c8v.get("price") == 8:
                _c8a += ["八块灵石", "八块"]
            if _c8v.get("days") == 20:
                _c8a += ["二十天"]
            elif _c8v.get("days") == 25:
                _c8a += ["二十五天"]
            _c8a += ["定金", "月结", "供货"]
            if not any(a in rn for a in _c8a):
                c_vio.append(f"{rid} 交易条件数值未落地（C8 执行参数）")
            continue
        anchors = _l3_outcome_anchors(desc)
        if not anchors:
            # A1（2026-09-22 用户裁决）：抽象叙事意图类 Outcome（章末钩子/伏笔/状态）
            # 提取不到确定性硬锚点，降级为 Review 维度，不 BLOCK。
            continue
        # 否定结果硬检查：desc 声明「拒绝/不接受/坚持不」某结果时，
        # 正文必须保留该名词的任一否定形态（不独家/拒绝独家/不接受独家…），防结果反转
        for noun in re.findall(r"(?:拒绝|不接受|坚持不|绝不|不)(独家|赊账|妥协|让步)", desc):
            if not any(k in rn for k in (f"不{noun}", f"拒绝{noun}", f"不接受{noun}",
                                         f"坚持不{noun}", f"绝不{noun}")):
                c_vio.append(f"{rid} 否定结果反转：正文缺少「{noun}」的否定形态")
        # 锚点证据检查（统一 _l3_anchor_ok：具体证据词→抽象降级→2-gram→软性 advisory）
        # OR 语义（2026-09-22 修复）：R 的多候选证据锚（备选证据，如「脚步声/监视者」）
        # 任一落地即算结果发生；全部零证据才 BLOCK。
        results = [_l3_anchor_ok(a, rn, desc) for a in anchors]
        if all(r == "BLOCK" for r in results):
            worst = min((_l3_cover2(a, rn), a) for a in anchors)
            c_vio.append(f"{rid} 证据不足（覆盖 {worst[0]:.2f}）：「{worst[1][:24]}」")
    gates["C_RequiredOutcomes"] = "PASS" if not c_vio else "BLOCK"
    vio.extend(c_vio)

    # ---- Gate D: Continuity ----
    d_vio: list[str] = []
    # L3 时间守卫（2026-09-22 用户裁决）：时间事实锚 = Plan 合同（非原文字面）——
    # 漂移：Plan 合同声明的时间事实（canon 如「二十年前」）忠实编译豁免；
    # 缺失：Plan 合同未认定的原文风味时间词（修辞/背景）允许不保留；其余 fail-closed。
    d_vio.extend(_check_time_facts_l3(original, rewritten, plan, c8_param, outline=outline)[:3])
    _on = _l3_norm(original)
    for pat, name in _L3_CONTINUITY_NEG:
        if pat.search(rn):
            # 原文既成事实豁免（2026-09-22 修复）：同一模式原文亦命中（如第38章原文
            # 已含「温故……丹田」既成事实）→ L3 忠实剧情基准不算提前消费；
            # 只拦「原文没有、新稿新增」的后续章内容提前。
            if pat.search(_on):
                continue
            d_vio.append(name)
    # P0: 只读 hard_dependencies（新 schema），兼容旧 post_dependencies
    # 防御层：先确定性回修 hard_dependencies 的主体颠倒/凭空细节/悬念闭合，
    # 不削弱 D 门——只把事实对齐细纲与本章状态变化（Plan 生成侧已修，此处兜底）。
    _l3_repair_continuity_facts(plan, outline)
    # 兜底：历史/旁路 Plan 可能绕过 Plan 阶段净化，D 门核查前再剥离一次元语言占位
    # （幂等；已净化 Plan 零影响）。D 门判定口径不放宽，只保证核查对象是事实断言。
    _l3_repair_hard_dep_hygiene(plan)
    _cc = plan.get("5_continuity_check") or {}
    _hard_deps = _cc.get("hard_dependencies")
    if _hard_deps is None:
        # 兼容历史 Plan：旧 post_dependencies 做 projection，仅保留可确定识别为 hard fact 的部分
        _old_posts = _cc.get("post_dependencies") or []
        _hard_deps = [p for p in _old_posts if re.search(
            r"(已发生|已完成|已介入|已支付|已交付|脚步声|赵执事|陈执事|汪万金|林小凡|温故|焚天阁|杂货铺|订单|交易|定金|烟火值)", str(p))]
    # LLM 语义判断（2026-11-05 外脑裁决）：不再用 2-gram 死磕整句，
    # 改为让 LLM 判断「正文是否落实了这个事实」——零假 PASS。
    # L3 重写是低频高价值场景，多一次 LLM 调用换准确判断值。
    # 兜底：LLM 调用失败时回退到确定性 2-gram 匹配。
    if _hard_deps:
        try:
            _dep_list = "\n".join(f"{i+1}. {d}" for i, d in enumerate(_hard_deps))
            _judge_prompt = (
                "你是一个事实核查员。下面是本章正文和本章必须落地的确定性事实清单。\n"
                "请逐条判断：正文里是否**明确、具体**地落实了这条事实？\n"
                "要求：\n"
                "- 「落实」= 正文写了这个事件/状态，不是只提了个词\n"
                "- 正文用动作描写而不是总结词也算落实（如「掏灵石拍案板」=「首单交易已完成」）\n"
                "- 正文完全没提这个事件 = 不落实\n"
                "- 只提了个相关词但没写事件本身 = 不落实\n\n"
                f"【本章正文】\n{rn[:6000]}\n\n"
                f"【必须落地的事实清单】\n{_dep_list}\n\n"
                '只输出 JSON 数组，每个元素是 {"id": 序号, "passed": true/false, "reason": "一句话理由"}。不要其他解释。'
            )
            _judge_result = _llm_json_once("nww-reviewer", "D_Continuity 事实核查", _judge_prompt, 1000)
            if isinstance(_judge_result, list):
                for item in _judge_result:
                    if not isinstance(item, dict):
                        continue
                    _idx = item.get("id")
                    _passed = item.get("passed")
                    _reason = item.get("reason", "")
                    if isinstance(_idx, int) and not _passed:
                        _dep_text = str(_hard_deps[_idx - 1]) if 0 < _idx <= len(_hard_deps) else f"#{_idx}"
                        d_vio.append(f"硬连续性依赖未落实：「{_dep_text[:30]}」（{_reason[:50]}）")
            else:
                raise RuntimeError("LLM 未返回合法数组")
        except Exception as _e:
            print(f"[D_Continuity] LLM 判断失败，回退确定性匹配: {_e}", flush=True)
            # 兜底：回退到原来的 2-gram 匹配
            for post in _hard_deps:
                _post_anchors = [a for a in _l3_outcome_anchors(str(post))
                                  if not re.match(r"^(次日|明日|第|当日|当天)", a)
                                  and not re.match(r"^(可能|或|需|引发|产生|影响|其他|后续|为|近期|面临|带来|章需|青云宗内部|大宗门采购|凡人物品)", a)]
                if _post_anchors and all(_l3_cover2(a, rn) < 0.2 for a in _post_anchors):
                    d_vio.append(f"硬连续性依赖证据不足：「{_post_anchors[0][:20]}」")
    gates["D_Continuity"] = "PASS" if not d_vio else "BLOCK"
    vio.extend(d_vio)

    # ---- Gate E: C8 ----
    e_vio: list[str] = []
    exp = (c8_param or {}).get("selected_variant", "body_variant")
    elems = {
        "body_variant": (bool(re.search(r"五百份", rn)), bool(re.search(r"十(?:块)?灵石", rn)),
                         bool(re.search(r"二十天", rn))),
        "outline_variant": (bool(re.search(r"三百份", rn)), bool(re.search(r"八(?:块)?灵石", rn)),
                            bool(re.search(r"二十五天", rn))),
    }
    hit_variants = [v for v, (q, pr, d) in elems.items() if q or pr or d]
    if len(hit_variants) > 1:
        e_vio.append(f"C8 数值混用：{hit_variants}")
    elif hit_variants and hit_variants[0] != exp:
        e_vio.append(f"C8 变体不符：正文={hit_variants[0]}，执行参数={exp}")
    # 第三套：单价/月量的具体数值不在既有两变体
    for m in re.finditer(r"每份([0-9一二三四五六七八九十百千零两]+)(?:块)?灵石", rn):
        v = m.group(1)
        if not (v in ("八", "8", "十", "10")):
            e_vio.append(f"第三套单价：{v}灵石")
    for m in re.finditer(r"每月([0-9一二三四五六七八九十百千零两]+)份", rn):
        v = m.group(1)
        if not (v in ("三百", "300", "五百", "500")):
            e_vio.append(f"第三套数量：每月{v}份")
    if re.search(r"(采用|选用|以.{0,6}为准|确定用)[^，。]{0,12}(?:三百|五百|300|500|八|十)", rn):
        e_vio.append("正文出现 C8 裁决性表述")
    gates["E_C8"] = "PASS" if not e_vio else "BLOCK"
    vio.extend(e_vio)

    # ---- Gate F: Zero-change ----
    zero = _l3_norm(rewritten).rstrip("\n") == _l3_norm(original).rstrip("\n")
    gates["F_ZeroChange"] = "PASS" if not zero else "BLOCK"
    if zero:
        vio.append("L3 章节再生产未发生：输出与输入正文一致")

    g_vio = _check_length_collapse(original, rewritten)
    # 字数治理（G_Length 扩展，塌缩判定不变）：硬下限 = 安全网 floor
    # = max(细纲目标×严重欠账比 0.70, min(细纲目标, 单章合格线 2000))（见 _g_length_floor）。
    # ★ 与 Writer 扩写硬线（_wc_hardline_ratio，默认 0.95）刻意解耦（2026-11-06 修复）：
    #   Writer 仍「尽量贴近目标」地写（aspiration，推高产出）；Gate 只拦「不可发布 / 整体塌缩」。
    #   旧 95% 地板与 Writer 实测 ~85% 产出结构性不可达 → 达标章被 BLOCK（第1章 2168<2375）。
    #   改为安全网后：2000 单章合格线兜住平台底线、0.70 严重欠账比兜住塌缩，二者取大。
    # 计数与 Writer 字数闸门同口径（_visible_chars，剥首行 ATX 标题）。
    # BLOCK，Writer 编译循环带反馈重写（清单第 7 条已前置同口径）。wc_target 缺省时零影响。
    try:
        _wt = int(str(wc_target).strip()) if wc_target else 0
    except (TypeError, ValueError):
        _wt = 0
    if _wt > 0:
        _floor = _g_length_floor(_wt)
        # 与 Writer 字数闸门同口径（_visible_chars，剥首行 ATX 标题）
        r_len = _visible_chars(rewritten or "")[0]
        if r_len < _floor:
            g_vio.append(f"字数欠账：正文 {r_len} 字 < 细纲目标 {_wt} 字的安全网下限 {_floor} 字"
                         f"（单章合格线 2000 / 严重欠账比 70% 取大）——"
                         "细纲三幕未充分展开即收稿；展开细纲已有的事件/对话/场景细节补足，不得注水凑字")
    gates["G_Length"] = "PASS" if not g_vio else "BLOCK"
    vio.extend(g_vio)

    # ---- Gate H: Prose Hygiene（2026-10-24 用户裁决，第2章抄送案例）----
    # 实测事故：重写稿正文出现元文本「他推门进来的动作比第1章还猛。」跨章引用
    # 直通 review/commit。句面确定性拦截（写作者视角词），不做语义判断。
    h_vio = _check_prose_hygiene(rewritten)
    gates["H_Hygiene"] = "PASS" if not h_vio else "BLOCK"
    vio.extend(h_vio)

    return {"violations": vio, "gates": gates}


def _l3_anchor_ok(a: str, rn: str, ctx: str = "") -> str:
    """L3 锚点证据检查（B/C 共用，返回 PASS/BLOCK/ADVISORY）：
    1) 具体证据词（烟火值/灵石/定金…）在正文 → PASS；
    2) 抽象目标段（确立/暗示/侧面…开头）→ 名词存在性（核心名词在正文即 PASS）；
    3) 2-gram 覆盖 ≥0.25 → PASS；
    4) 其余：锚含软性语义词（暗示/伏笔/钩子/微妙…）→ ADVISORY（不阻断）；
       否则 BLOCK。"""
    _EVID = ("烟火值", "灵石", "定金", "月结", "供货", "供应", "采购", "订单", "样品", "货物",
             "青云宗", "林小凡", "陈执事", "汪万金", "凡气", "凡货", "凡俗", "自热米饭",
             "五成", "批量", "次日", "派弟子", "行李箱", "规矩", "怀疑", "离开", "登门",
             "独家", "非独家", "玉牌", "炉灶", "算盘", "月结")
    _ABS_HEAD = r"^(确定|确立|达成|完成|暗示|展现|展示|侧面|获取|通过|承接|奠定|留下|体现|彰显|强化|呼应|揭示|铺垫|证明|为后续|留出|的|双方|近期|需|引发|可能|或|酝酿|埋下|指向|本章|内部|并明确|明确|形成)"
    _ABS_TAIL = r"(关系|意向|基调|逻辑|空间|利益|风险|压力|时间线|动态|舆论|审查|关注|态度|分歧|倾向|价值|评价|代价|局面|预期|渗透力)$"
    _SOFT = r"(暗示|伏笔|钩子|微妙|隐性|依赖|潜在|氛围|铺垫|展示|预期|压力|渗透|象征|余韵|张力|动机|立场|态度|微涨|提升|稳固|巩固|增长|上升|满足)"
    # 意志/原则类锚点（「坚持将辣条作为」「主张保留…」）是叙事意图而非事件证据：
    # Planner 把处事原则写进 R.desc，锚点提取只能切出叙述碎片，正文按动作描写落地时
    # 2-gram 必然低（实测「坚持将辣条作为」覆盖 0.17）。按 A1 裁决（抽象叙事意图
    # 提取不到确定性硬锚点时降级 Review 维度）判 ADVISORY，不冒充已验证、也不阻断。
    _VOLITION_HEAD = r"^(坚持|主张|强调|选择|决定|保留|维持|避免|防止|不再|拒绝|视为|作为)"
    words = [w for w in _EVID if w in a]
    if words and any(w in rn for w in words):
        return "PASS"
    if re.match(_VOLITION_HEAD, a):
        return "ADVISORY"
    if re.match(_ABS_HEAD, a):
        nouns = []
        a2 = re.sub(_ABS_HEAD, "", a)
        a2 = re.sub(_ABS_TAIL, "", a2)
        for seg in re.findall(r"[\u4e00-\u9fff]{2,}", re.sub(r"(?:背后|的|或|与|潜在|复杂|初步|内部|外部|相关|进而|从而)", " ", a2)):
            if len(seg) >= 2 and seg not in nouns:
                nouns.append(seg)
        if any(n in rn for n in nouns if len(n) >= 2):
            return "PASS"
        return "ADVISORY" if re.search(_SOFT, ctx or a) else "BLOCK"
    if _l3_cover2(a, rn) >= 0.25:
        return "PASS"
    return "ADVISORY" if re.search(_SOFT, ctx or a) else "BLOCK"
