"""nww_delta_schema.py — delta 投影/留痕白名单的唯一来源（C-DELTA-SCHEMA@1）。

消费方：
  - nww_track_commit.normalize_delta：require_known_keys(delta, DELTA_ALLOWED, "delta")
  - web/backend/app/agents_runtime/pipeline.py::_repair_delta：顶层投影
  - pipeline._carry_system_quality_meta / _rebind_body_quality_meta
  - nww_track_commit.normalize_quality_meta：QUALITY_META_ALLOWED（C-QUALITY-META@1）

两份字面量曾各自维护（2026-11-13 审计 R7），改动一处漏另一处即产生
「web 侧投影保留、commit 侧拒收」的静默分叉。此处收敛为单源。
"""
from __future__ import annotations

# delta 顶层合法键（17 键，2026-11-13 现状逐键一致）。
# 这是**投影白名单**：LLM 输出的 delta 只保留这些键，其余一律丢弃。
# 为什么是投影而不是逐个 pop：pop 只能堵已知的洞，LLM 换个自造键就再 BLOCK 一次
# （2026-09-14 实测：先 character_changes[0].character，再 delta.global_change）。
DELTA_ALLOWED: frozenset[str] = frozenset({
    "result", "character_changes", "foreshadow_changes", "timeline_events", "constraints",
    "next_chapter_commitments", "retired_context_items", "retired_characters", "cost_change",
    "quality_meta", "rule_loss_event", "revived_characters", "retirement_reasons", "chapter_type",
    # P0-2 事实契约：web 只透传、不处理（gate/merge 在 commit 侧）
    "contract_state", "candidate_facts", "fact_changes",
})

# quality_meta 允许键（C-QUALITY-META@1，2026-11-13 现状逐键一致）。
# 这是**白名单**：未知键在 nww_track_commit.normalize_quality_meta 里被剥离并打印
# WARNING；代码侧有意新增字段时必须同步改这里（否则 commit 侧拒收该字段）。
QUALITY_META_ALLOWED: frozenset[str] = frozenset({
    "declared", "observed", "enforced", "enforced_at", "enforced_attempted",
    "reviewed", "reviewed_at", "review_score", "review_rounds",
    "finalized", "finalized_at", "review_id",
    "target_body_md5", "review_status", "reviewed_hash", "final_hash",
})

# quality_meta 中「审稿/验收留痕」类的系统键（nww_review_cycle finalize 写入 +
# _rebind_body_quality_meta 重绑）。这类键描述的是「哪一版正文过了审」，
# 不能由 LLM 誊写、也不能被 delta 重建抹掉 —— 见 pipeline._carry_system_quality_meta。
# 刻意不含 enforced/enforced_at：那是**本次运行**的机械层留痕，由 post_write 每轮重写。
# enforced 每轮由 pre_review_enforce 覆盖，且 commit 侧 A-5 依其真实性（enforced=false 即拒收）
# 判定，携带上一轮的 true 会掩盖本轮漏跑（fail-open）。
# enforced_attempted 反其道而行：由 step_pre_review_enforce 每轮无条件覆盖为 True，
# 携带上一轮值不构成掩盖；它的作用是证明「本轮确实执行过 enforce 动作」，
# 与 enforced（执行结果）成对出现，避免「漏跑」与「跑失败」在 meta 上不可区分。
SYS_QUALITY_META_KEYS: tuple[str, ...] = (
    "reviewed", "reviewed_at", "review_score", "review_rounds",
    "finalized", "finalized_at", "review_id",
    "reviewed_hash", "final_hash", "target_body_md5", "review_status",
    "enforced_attempted",
)