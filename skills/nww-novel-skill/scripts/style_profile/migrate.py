# -*- coding: utf-8 -*-
"""style_profile.migrate — 四态散落资产收敛到统一 styles/ 存储（决策 D5）。

把 32 题材卡（genre-prose-cards/*.md）+ 2 yaml 范例 + 单书 设定/文风.md
收敛为统一 Profile JSON。D3 硬卡口（negativeSamples 必填）如实拦截：
- status=ok 的资产 → 写入 styles/{name}.json（注册中心优先读取）
- status=invalid（缺 negativeSamples）→ 不写入，仅汇总到待补全清单，由人工补「不可模仿标注」

零 LLM、只读源资产、只写 styles/（新增目录，不触碰任何现有生产代码）。
用法：
    python -m style_profile.migrate [--book /path/to/book]
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .registry import _STYLES_STORE, list_genres, load_style_profile
from .validate import validate_profile

os.environ.setdefault("NWW_STYLE_PROFILE_ENABLED", "1")


def _slug(name: str) -> str:
    return name


def migrate_genres() -> dict[str, int]:
    """收敛全部 genre 资产。返回 {ok, invalid, missing} 计数。"""
    counts = {"ok": 0, "invalid": 0, "missing": 0}
    needs: list[str] = []
    _STYLES_STORE.mkdir(parents=True, exist_ok=True)
    for name in list_genres():
        r = load_style_profile("genre", name)
        if r["status"] == "missing":
            counts["missing"] += 1
            continue
        if r["status"] == "ok" and r["profile"] is not None:
            payload = {
                "schema_version": 1,
                "scope": "genre",
                "sampleIdentifier": name,
                "profile": r["profile"],
                "source": r["source"],
            }
            (_STYLES_STORE / f"{_slug(name)}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            counts["ok"] += 1
        else:
            counts["invalid"] += 1
            needs.append(name)
    if needs:
        (_STYLES_STORE / "_NEEDS_NEGATIVE_SAMPLES.txt").write_text(
            "\n".join(needs) + "\n", encoding="utf-8"
        )
    return counts


def migrate_book(book: Path) -> dict[str, Any]:
    """收敛单书 设定/文风.md → styles/{book}.json（需带 negativeSamples 才 ok）。"""
    r = load_style_profile("book", book.name, book=book)
    if r["status"] in ("missing", "invalid") or r["profile"] is None:
        return {"status": r["status"], "errors": r["errors"], "path": None}
    _STYLES_STORE.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "scope": "book",
        "sampleIdentifier": book.name,
        "profile": r["profile"],
        "source": r["source"],
    }
    out = _STYLES_STORE / f"{book.name}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"status": "ok", "errors": [], "path": str(out)}


if __name__ == "__main__":
    book_arg = None
    if "--book" in sys.argv:
        idx = sys.argv.index("--book")
        if idx + 1 < len(sys.argv):
            book_arg = Path(sys.argv[idx + 1])
    g = migrate_genres()
    print(f"[genre] ok={g['ok']} invalid(缺negativeSamples)={g['invalid']} missing={g['missing']}")
    if book_arg:
        b = migrate_book(book_arg)
        print(f"[book] {book_arg.name} → status={b['status']} path={b['path']}")
        if b["errors"]:
            print("  errors:", b["errors"])
    if g["invalid"]:
        print(f"待人工补全 negativeSamples 的资产清单见：{_STYLES_STORE / '_NEEDS_NEGATIVE_SAMPLES.txt'}")
