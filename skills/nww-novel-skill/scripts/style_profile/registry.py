# -*- coding: utf-8 -*-
"""style_profile.registry — 风格资产注册中心 / 统一入口（决策 D5）。

承接双脑共识（ChatGPT + Gemini）：
- 四态散落资产（style_exemplars/*.yaml、genre-prose-cards/*.md、单书 设定/文风.md、
  未来统一 styles/*.json）收敛到**单一入口** load_style_profile。
- scope:genre 保留 32 题材卡（genre-prose-cards/*.md，带 YAML frontmatter）；
  2 个 yaml 范例（style_exemplars/）作同源 genre 补充；统一 styles/*.json 优先。
- scope:book 读取 {book}/设定/文风.md（本书定版风格契约）。
- 读取失败 / 缺资产 → 返回显式 status，绝不静默返回 {}（工程纪律）。

启用：env NWW_STYLE_PROFILE_ENABLED（默认 0/关）。关闭时 load 返回 status="disabled"，
不改变任何现有流程。本文件为**新增能力**，零 LLM、只读、不修改任何生产代码。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .schema import (
    NARRATIVE_LOGIC_KEYS,
    REQUIRED_FIELDS,
    VALID_SCOPES,
    empty_profile,
    normalize,
)
from .validate import validate_profile

# ── 路径定位（本文件位于 scripts/style_profile/registry.py）─────────────────
_SKILL_ROOT = Path(__file__).resolve().parents[2]          # nww-novel-skill/
_SCRIPTS = _SKILL_ROOT / "scripts"
_REFERENCES = _SKILL_ROOT / "references"
_GENRE_CARDS = _REFERENCES / "agent-references" / "genre-prose-cards"
_EXEMPLARS = _REFERENCES / "style_exemplars"
_STYLES_STORE = _SKILL_ROOT / "styles"                     # 统一 JSON 收敛存储

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)
_MD_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.M)
_YAML_LIST_ITEM_RE = re.compile(r"^\s*-\s+(.+)$")


def _enabled() -> bool:
    return os.getenv("NWW_STYLE_PROFILE_ENABLED", "0") == "1"


def _parse_md_card(path: Path) -> dict[str, Any]:
    """解析 genre-prose-cards/*.md：YAML frontmatter + ## 节。

    返回 {meta: {...}, sections: {节名: 文本}}。纯正则，零依赖。
    """
    text = path.read_text(encoding="utf-8")
    meta: dict[str, Any] = {}
    body = text
    fm = _FRONTMATTER_RE.match(text)
    if fm:
        for line in fm.group(1).splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip()
        body = text[fm.end():]
    sections: dict[str, str] = {}
    parts = _MD_SECTION_RE.split(body)
    # parts[0] 为首个 ## 前的导言（标题/空），其后每两项为一对（节名, 内容）
    for i in range(1, len(parts), 2):
        name = parts[i].strip()
        content = parts[i + 1].strip() if i + 1 < len(parts) else ""
        sections[name] = content
    return {"meta": meta, "sections": sections}


def _parse_yaml_exemplar(path: Path) -> dict[str, Any]:
    """解析 style_exemplars/*.yaml（简单结构：标量 + 块列表）。

    优先用 PyYAML；缺失则回退到聚焦解析（仅处理本批 2 个文件的已知结构）。
    """
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    # 回退解析：genre/source/validated 标量；metaphor_examples/exemplars 块列表
    data: dict[str, Any] = {"metaphor_examples": [], "exemplars": []}
    cur_list = None
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z_]+):\s*(.*)$", line)
        if m and not line.startswith(" "):
            key, val = m.group(1), m.group(2).strip()
            if val == "":
                cur_list = key if key in ("metaphor_examples", "exemplars") else None
                data.setdefault(key, [] if cur_list else "")
            else:
                cur_list = None
                data[key] = val
        elif line.strip().startswith("- ") and cur_list:
            item = line.strip()[2:].strip()
            if cur_list == "exemplars":
                # exemplars 项是带 type/text 的多行块，这里只记首行 type，细节交给 load
                data["exemplars"].append(item)
            else:
                data[cur_list].append(item)
    return data


def _genre_card_to_profile(name: str, card: dict[str, Any]) -> dict[str, Any]:
    """把题材卡（md）结构化为 Profile（scope=genre）。"""
    meta = card.get("meta", {})
    sections = card.get("sections", {})
    p = empty_profile("genre", name, source=meta.get("source", "genre-prose-cards"))
    p["atmosphere"] = sections.get("正文提示词", "")
    p["compiledRules"] = [
        {"section": k, "text": v}
        for k, v in sections.items()
        if k not in ("正文提示词",)
    ]
    # D1 叙事逻辑三指标：题材卡天然含「开场抓手」「章尾钩子」（过渡配比留待 fingerprint 补）
    p["narrativeLogic"] = {
        "sceneOpeningTypes": {"source_section": "开场抓手", "text": sections.get("开场抓手", "")},
        "transitionRatios": {},  # 题材卡无显式过渡配比 → 留空，由 fingerprint 补充
        "chapterEndingTypes": {"source_section": "章尾钩子", "text": sections.get("章尾钩子", "")},
    }
    # 题材卡本身无 negativeSamples → 入库硬卡口会拦截，load 仅作只读预览
    return normalize(p)


def _exemplar_to_profile(name: str, data: dict[str, Any]) -> dict[str, Any]:
    p = empty_profile("genre", name, source=data.get("source", "style_exemplars"))
    p["positiveSamples"] = [
        {"expression": m} for m in data.get("metaphor_examples", []) if str(m).strip()
    ]
    p["compiledRules"] = [
        {"type": e.get("type") if isinstance(e, dict) else None,
         "text": e.get("text") if isinstance(e, dict) else str(e)}
        for e in data.get("exemplars", []) if e
    ]
    p["narrativeLogic"] = {k: {} for k in NARRATIVE_LOGIC_KEYS}
    return normalize(p)


def _book_doc_to_profile(book: Path) -> dict[str, Any]:
    f = book / "设定" / "文风.md"
    if not f.is_file():
        return {}
    text = f.read_text(encoding="utf-8", errors="ignore")
    p = empty_profile("book", book.name, source="设定/文风.md")
    p["atmosphere"] = text
    p["narrativeLogic"] = {k: {} for k in NARRATIVE_LOGIC_KEYS}
    return normalize(p)


def list_genres() -> list[str]:
    """列出所有可用 genre 名称（题材卡优先；yaml 范例补充）。"""
    names: list[str] = []
    if _GENRE_CARDS.is_dir():
        for p in sorted(_GENRE_CARDS.glob("*.md")):
            names.append(p.stem)
    if _EXEMPLARS.is_dir():
        for p in sorted(_EXEMPLARS.glob("*.yaml")):
            names.append(p.stem)
    # 统一存储 styles/*.json（若存在）覆盖同名
    if _STYLES_STORE.is_dir():
        for p in sorted(_STYLES_STORE.glob("*.json")):
            stem = p.stem
            if stem not in names:
                names.append(stem)
    return names


def load_style_profile(scope: str, name: str, book: Path | None = None) -> dict[str, Any]:
    """统一入口：加载风格资产 Profile。

    返回结构：
        {"status": "ok"|"disabled"|"missing"|"invalid",
         "profile": {...} | None,
         "errors": [...], "source": "..."}

    绝不静默返回 {}：任何失败都在 status/errors 显式表达。
    - scope 非法 → status="invalid"
    - 总开关关 → status="disabled"（仍返回空骨架让消费端明确知道未启用）
    - 资产缺失 → status="missing"
    - 资产存在但 validate 失败（如缺 negativeSamples）→ status="invalid" + errors
    """
    if scope not in VALID_SCOPES:
        return {"status": "invalid", "profile": None,
                "errors": [f"非法 scope: {scope!r}（须为 {VALID_SCOPES}）"], "source": None}
    if not _enabled():
        return {"status": "disabled", "profile": empty_profile(scope, name),
                "errors": ["NWW_STYLE_PROFILE_ENABLED=0（默认关）"], "source": None}

    profile: dict[str, Any] | None = None
    source: str | None = None

    if scope == "genre":
        # 1) 统一存储 styles/{name}.json（迁移后优先）
        json_path = _STYLES_STORE / f"{name}.json"
        if json_path.is_file():
            try:
                profile = normalize(json.loads(json_path.read_text(encoding="utf-8")))
                source = str(json_path)
            except Exception as e:
                return {"status": "invalid", "profile": None,
                        "errors": [f"styles/{name}.json 解析失败: {e}"], "source": str(json_path)}
        else:
            # 2) 题材卡 *.md
            md_path = _GENRE_CARDS / f"{name}.md"
            if md_path.is_file():
                card = _parse_md_card(md_path)
                profile = _genre_card_to_profile(name, card)
                source = str(md_path)
            else:
                # 3) yaml 范例
                yaml_path = _EXEMPLARS / f"{name}.yaml"
                if yaml_path.is_file():
                    data = _parse_yaml_exemplar(yaml_path)
                    profile = _exemplar_to_profile(name, data)
                    source = str(yaml_path)
    elif scope == "book":
        if book is None:
            return {"status": "invalid", "profile": None,
                    "errors": ["scope=book 必须提供 book 路径"], "source": None}
        profile = _book_doc_to_profile(Path(book))
        source = "设定/文风.md"
        if not profile:
            return {"status": "missing", "profile": None,
                    "errors": [f"{book}/设定/文风.md 不存在"], "source": source}

    if profile is None:
        return {"status": "missing", "profile": None,
                "errors": [f"未找到 {scope}={name} 的资产"], "source": source}

    ok, errors = validate_profile(profile)
    if not ok:
        # 资产存在但入库校验未过（如缺 negativeSamples）→ 仍返回 profile 供只读预览，
        # 但 status=invalid 让消费端明确知道「未达入库标准」。
        return {"status": "invalid", "profile": profile, "errors": errors, "source": source}
    return {"status": "ok", "profile": profile, "errors": [], "source": source}


if __name__ == "__main__":
    # 自测：列出题材卡数量；未启用时返回 disabled；启用后加载一个 genre 应 ok/invalid（缺 negativeSamples）。
    genres = list_genres()
    assert len(genres) >= 32, f"题材卡数量异常: {len(genres)}"
    # 未启用 → disabled
    r0 = load_style_profile("genre", genres[0])
    assert r0["status"] == "disabled", f"未启用却非 disabled: {r0['status']}"
    # 启用后：题材卡无 negativeSamples → 应为 invalid（D3 硬卡口拦截），但 profile 非空
    os.environ["NWW_STYLE_PROFILE_ENABLED"] = "1"
    r1 = load_style_profile("genre", genres[0])
    assert r1["profile"] is not None, "启用后 profile 为空"
    assert r1["status"] in ("ok", "invalid"), f"status 异常: {r1['status']}"
    assert r1["status"] == "invalid" and any("negativeSamples" in e for e in r1["errors"]), \
        "题材卡缺 negativeSamples 未被 D3 拦截"
    print(f"registry self-test passed. genres={len(genres)}, sample={genres[0]} status={r1['status']}")
