"""era_check_adapter.py — era_consistency observation adapter（P6-E，Step3，零耦合新模块）。

职责：
  在 step_consistency_check 写盘前，把时代合理性观察结果注入 record["era_consistency"]，
  作为与 S1-S4 平行的独立观察维度（world era plausibility）。

  - 仅观察（observe）：不 BLOCK、不自动改文、不影响 S1-S4/closure/writer/prompt/bible。
  - 复用已有 nww_era_check.check_prose / load_bible / get_whitelist / has_source_explanation，
    不重写词库逻辑。
  - 配置开关：model_config.json 内 era_check.{enabled, mode}。mode 仅取 observe 观察语义；
    关闭或配置缺失 → 返回 {}，不注入。
  - 任一异常返回 {}，绝不悬挂主链（参照 pipeline.review_observe 的异常隔离先例）。
  - 禁止伪造检测能力：只输出现有检测的 raw_severity + adapter 补充的 source_check，
    不输出 category / confidence / era_conflict_score。
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

__all__ = ["era_check_observe"]


def _model_config_path() -> Path:
    """model_config.json 定位（与 llm_config._config_path 同规则）：env > runtime/config > legacy。

    env：NWW_MODEL_CONFIG_FILE；新路径：仓库根/runtime/config/model_config.json；
    兜底：遗留 web/backend/app/model_config.json。
    """
    env = os.environ.get("NWW_MODEL_CONFIG_FILE", "").strip()
    if env:
        return Path(env)
    repo_root = Path(__file__).resolve().parents[4]
    new_path = repo_root / "runtime" / "config" / "model_config.json"
    if new_path.is_file():
        return new_path
    legacy = Path(__file__).resolve().parents[1] / "model_config.json"
    return legacy if legacy.is_file() else new_path


def _load_era_config() -> dict:
    """读取 era_check 配置段；缺失/损坏 → {}。"""
    try:
        cfg = json.loads(_model_config_path().read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            return {}
        era = cfg.get("era_check")
        return era if isinstance(era, dict) else {}
    except Exception:  # noqa: BLE001 — 配置读取失败按未启用处理
        return {}


def _import_era_check():
    """把 nww_era_check.py 所在目录临时加入 sys.path 后 import（无模块级副作用才可控）。

    脚本位于 仓库根/skills/nww-novel-skill/scripts/nww_era_check.py，CLI 在 main()，
    import 不触发执行；只复用顶层函数 check_prose/load_bible/get_whitelist/has_source_explanation。
    """
    scripts = Path(__file__).resolve().parents[4] / "skills" / "nww-novel-skill" / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import nww_era_check  # type: ignore

    return nww_era_check


def _worldview_banned_items(book: Path) -> list[str]:
    """P1-7（2026-10-25）：从 设定/世界观/*.md 的「禁用」条款提取本书专属禁用器物词。

    根因实证：仙门书世界观红线「可乐→玄玉灵泉瓶、玻璃/塑料/铝/橡胶等现代材质词
    一律禁用」，但静态 MODERN_ITEMS 无「拉环/罐身」等词 → 正文「拉开易拉罐的拉环」
    以 I-E 95 分过审。本函数把设定层禁用词接入检测：取含「禁用」的行，按分隔符
    切词，滤停用词与超长短语，每书上限 30 个。"""
    stop = {"现代", "材质", "器物", "本土", "一律", "禁用", "可见", "实物", "工业",
            "词语", "词", "描述", "呈现", "等", "细节", "品牌", "维度", "全球", "使用"}
    # 单字材质词白名单（「玻璃、塑料、铝、橡胶」枚举中的单字）
    single_ok = {"铝", "塑", "漆", "锌"}
    out: list[str] = []
    seen: set[str] = set()
    for folder in ("设定/世界观", "设定"):
        d = book / folder
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.md")):
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001
                continue
            for ln in text.splitlines():
                m = re.search(r"禁用|禁止", ln)
                if not m:
                    continue
                # 只取「禁用/禁止」之后的片段：行首主语（如「系统载体（行李箱…）」）
                # 是设定主体不是禁用对象，整行切词会把金手指载体误列为禁用词
                for tok in re.split(r"[、，,/（）()\[\]「」\s；;：:]", ln[m.end():]):
                    tok = tok.strip("。，,.")
                    # 截「等/之类」后缀：「橡胶等现代材质词…」→「橡胶」
                    tok = re.split(r"等|之类", tok)[0].strip("。，,.")
                    if not tok or tok in stop or len(tok) > 6 or "→" in tok or "改为" in tok:
                        continue
                    if len(tok) < 2 and tok not in single_ok:
                        continue
                    if tok in seen:
                        continue
                    seen.add(tok)
                    out.append(tok)
            if len(out) >= 30:
                break
    return out[:30]


def era_check_observe(book: Path, chapter_id: int, prose_text: str) -> dict:
    """era_consistency observation。异常/关闭 → {}，绝不悬挂主链。

    返回：{enabled, mode, hits:[{term, raw_severity, source_check}], decision:"observe"}。
    source_check 由 adapter 按 nww_era_check 同款来源判断补充（不伪造检测能力）。
    """
    try:
        cfg = _load_era_config()
        enabled = bool(cfg.get("enabled", False))
        if not enabled or not prose_text or not prose_text.strip():
            return {}
        mode = str(cfg.get("mode") or "observe").lower()

        ec = _import_era_check()
        bible = ec.load_bible(str(book))
        whitelist = ec.get_whitelist(bible)
        concept_allow = ec.get_concept_allowlist(bible, whitelist)
        register_mode = ec.resolve_register_mode(bible)
        alerts = ec.check_prose(prose_text, whitelist, str(chapter_id),
                                concept_allow, register_mode,
                                extra_items=_worldview_banned_items(book))

        hits = []
        for a in alerts:
            term = str(a.get("item") or "")
            if not term:
                continue
            # 定位首个出现位置，用同款来源判断补充 source_check（事实性，不虚构）
            pos = prose_text.find(term)
            src = {
                "protagonist_brought": term in whitelist,
                "context_explanation": ec.has_source_explanation(prose_text, pos, term) if pos >= 0 else False,
            }
            hits.append(
                {
                    "term": term,
                    "kind": str(a.get("kind") or "modern_item"),
                    "channel": str(a.get("channel") or ""),
                    "raw_severity": str(a.get("severity") or "MEDIUM").upper(),
                    "reason": str(a.get("reason") or ""),
                    "source_check": src,
                }
            )

        return {
            "enabled": enabled,
            "mode": mode,
            "register_mode": register_mode,
            "hits": hits,
            "decision": "observe",
        }
    except Exception:  # noqa: BLE001 — 观察层失败不影响主链
        return {}