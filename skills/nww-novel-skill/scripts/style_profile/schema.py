# -*- coding: utf-8 -*-
"""style_profile.schema — NWW 统一风格资产 Profile 定义（决策 D2）。

落地双脑共识（ChatGPT + Gemini 一致）：
- 风格资产统一为结构化 JSON Profile，替代散落的 md / yaml / 内存 json。
- 必含字段：scope / sampleIdentifier / negativeSamples（D3 硬卡口）。
- 叙事逻辑三指标（D1 首波）：sceneOpeningTypes / transitionRatios / chapterEndingTypes，
  均为规则可提取、零依存树；因果展开 / 信息释放 / 视角跳跃暂缓，待解析器支持。

设计约束（工程纪律）：纯数据 + 校验，零 LLM、零副作用。本包为新增能力，
不修改任何现有生产代码；是否启用由 env NWW_STYLE_PROFILE_ENABLED 控制（默认关）。
"""
from __future__ import annotations

from typing import Any

# 双脑共识必含字段；negativeSamples 为入库硬卡口（缺则拒绝入库，见 D3）。
REQUIRED_FIELDS = ("scope", "sampleIdentifier", "negativeSamples")
# 可选字段（normalize 兜底补全结构，避免消费端 KeyError，不静默吞错误）。
OPTIONAL_FIELDS = (
    "proseMetrics", "narrativeLogic", "positiveSamples",
    "compiledRules", "validatedEvidence", "source", "atmosphere",
)
VALID_SCOPES = ("global", "genre", "workflow", "book")

# 叙事逻辑三指标（D1 首波，规则可提取、零依存树）
NARRATIVE_LOGIC_KEYS = ("sceneOpeningTypes", "transitionRatios", "chapterEndingTypes")

# 现有 14 个表层指纹字段（来自 nww_style_fingerprint.py:95-116），
# 作为 proseMetrics 的约定键集合（允许超集，不强制穷尽）。
PROSE_METRIC_KEYS = (
    "chars", "para_count", "sentence_count", "sentence_len_mean",
    "sentence_len_median", "sentence_len_p90", "sentence_len_cv",
    "short_sentence_ratio", "para_len_mean", "para_len_max", "para_len_cv",
    "short_para_ratio", "dialogue_char_ratio", "dialogue_para_ratio",
    "simile_per_kilo_chars", "sentences_per_para",
)


def empty_profile(scope: str, sample_identifier: str, source: str = "") -> dict[str, Any]:
    """构造骨架 Profile（negativeSamples 仍为空 → validate 会失败，逼补 D3）。"""
    if scope not in VALID_SCOPES:
        raise ValueError(f"非法 scope: {scope!r}（须为 {VALID_SCOPES} 之一）")
    return {
        "schema_version": 1,
        "scope": scope,
        "sampleIdentifier": sample_identifier,
        "source": source,
        "proseMetrics": {},
        "narrativeLogic": {k: {} for k in NARRATIVE_LOGIC_KEYS},
        "positiveSamples": [],
        "negativeSamples": [],  # 必填：缺则拒绝入库
        "compiledRules": [],
        "validatedEvidence": {},
    }


def normalize(profile: dict[str, Any]) -> dict[str, Any]:
    """兜底补全可选字段，避免消费端 KeyError（只补结构，不静默吞错误）。"""
    p = dict(profile)
    for f in OPTIONAL_FIELDS:
        if f not in p:
            p[f] = {} if f in ("proseMetrics", "narrativeLogic", "validatedEvidence") else []
    p.setdefault("schema_version", 1)
    return p
