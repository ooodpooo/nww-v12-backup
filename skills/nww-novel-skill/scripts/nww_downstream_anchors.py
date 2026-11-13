#!/usr/bin/env python3
"""重写前冻结：为「第 N 章」产出 protected_anchors（下游依赖锚点）——只读，不写任何正文。

为什么要有它（2026-09-26 二外脑裁决 + 用户拍板）：
    重写第 N 章时，"不能动"的不是原文文字，而是**下游章节已经消费过的具体事实**。
    这些事实的粒度远细于细纲（物件归属、时点、数值、角色 location/state、伏笔状态），
    细纲里没有，只有把状态层 + 下游正文反向扫一遍才知道哪些被咬住了。

本脚本做三件事（全部确定性，无 LLM）：
  1) Freeze：从 追踪/_tracking-state.json（+ bible.json）冻结第 N 章相关的状态快照
     —— 时间线事件 / 伏笔 / 出场角色 location·state / 已确认事实 / 全局资源数值；
  2) Extract：把每条状态翻译成**可检索的表面词元**（数字+单位、人名、地名/机构名）；
  3) Scan：在 N+1..latest 的正文中回查这些词元 → 得出 referenced_by。

产物：
    protected_anchors      被下游引用过的锚点（重写必须原样保留）
    unreferenced_anchors   本章状态但下游没引用（可放宽，但仍不可改状态机）
    dangling_references    下游在引用、本章/状态层却找不到的东西（红色警报，先查再动）

用法：
    python nww_downstream_anchors.py --book <书目录> --chapter 29
    python nww_downstream_anchors.py --book <书目录> --chapter 29 --window 12 --json
    python nww_downstream_anchors.py --book <书目录> --chapter 29 --out 追踪/rewrite_anchors/第029章.json
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from pathlib import Path

# ---- 表面词元抽取（确定性，无词典也能跑） ----

_CN_NUM = "〇零一二两三四五六七八九十百千万"
_UNITS = (
    "点|份|块灵石|枚灵石|灵石|枚|块|天|日|月|年|刻|息|盏|碗|瓶|张|条|把|口|间|间铺|"
    "成|次|层|档|级|重|遍|斤|两|里|丈|尺|寸|人|名|位|倍|成利|折"
)
_NUM_RE = re.compile(rf"(?:[0-9]+|[{_CN_NUM}]+)\s*(?:{_UNITS})")
_PLACE_RE = re.compile(r"[\u4e00-\u9fa5]{2,6}(?:坊市|宗|铺|堂|阁|门|府|院|巷|街|楼|殿|塔|墟|废墟)")
_QUOTED_RE = re.compile(r"[“\"]([^”\"]{2,12})[”\"]")

# 硬值单位：只有这些才够格当"下游会咬住的事实"（灵石/份/烟火值/日期…）。
# 层/次/口/个/下 这类量词会在任何一章出现上千次，当锚点只会制造噪音。
_HARD_UNITS = (
    "块灵石|枚灵石|灵石|枚|份|点|瓶|碗|斤|成|档|条|张|天|日|月|年|刻|息|两|里|丈"
)
_NUM_HARD_RE = re.compile(rf"(?:[0-9]+|[{_CN_NUM}]+)\s*(?:{_HARD_UNITS})")
# 小数量词（一张/两条/三份…）到处都是，不是事实，只会灌水
_SOFT_NUM = {"一", "两", "几", "二", "三", "四", "五"}
_SOFT_UNIT = {"点", "层", "次", "口", "个", "下", "遍", "眼", "声", "步",
              "张", "条", "份", "块", "枚", "碗", "瓶"}
_STOP_TOKENS = {"一个", "一下", "一点", "一口", "一天", "一年", "一次", "一名", "一位", "两口"}


def _hard_tokens(text: str) -> list[str]:
    """只留"硬值"词元：数字+硬单位；排除 一下/一层 这类量词噪声。"""
    out = []
    for m in _NUM_HARD_RE.finditer(text or ""):
        t = m.group(0).strip()
        if not t or t in _STOP_TOKENS:
            continue
        mm = re.match(rf"([0-9]+|[{_CN_NUM}]+)\s*(\S+)", t)
        if mm and mm.group(1) in _SOFT_NUM and mm.group(2) in _SOFT_UNIT:
            continue
        out.append(t)
    seen, uniq = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def _tokens(text: str) -> list[str]:
    out: list[str] = []
    for m in _NUM_RE.finditer(text or ""):
        t = m.group(0).strip()
        if t and t not in _STOP_TOKENS and len(t) >= 2:
            out.append(t)
    for m in _PLACE_RE.finditer(text or ""):
        out.append(m.group(0))
    for m in _QUOTED_RE.finditer(text or ""):
        out.append(m.group(1))
    # 去重保序
    seen, uniq = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def _rare_phrases(fact: str, corpus: list[str], topk: int = 6) -> list[str]:
    """从一段事实描述里挑"稀有短语"当检索词元（无词典、纯统计）。

    为什么需要它：伏笔/时间线的关键常常不是数字也不是地名，而是「初一」「放东西」
    「残匾」「背篓」这种普通词——只靠"数字+单位/地名"抽取会把它们整条漏掉
    （实测 F008「每月初一…有人定期放置物品」就被判成未被引用，而"初一"在第30章还在用）。
    做法：对事实文本取 2~4 字 CJK 连续片段，按"在全书出现过的章数"排序，留稀有的。
    df=0 的片段没有任何检索价值（下游不可能命中），df 过大的片段是通用词，两者都丢。
    """
    n_ch = max(1, len(corpus))
    text = re.sub(r"[^\u4e00-\u9fa5]", "", fact or "")
    cands: dict[str, int] = {}
    for n in (4, 3, 2):
        for i in range(len(text) - n + 1):
            g = text[i:i + n]
            cands.setdefault(g, sum(1 for c in corpus if g in c))
    # 只留"稀有但确实存在"的：df ∈ [1, 40% 章节数]
    hi = max(2, int(0.4 * n_ch))
    keep = [(g, df) for g, df in cands.items() if 1 <= df <= hi]
    keep.sort(key=lambda x: (x[1], -len(x[0])))
    out: list[str] = []
    for g, _df in keep:
        # 已被更长片段覆盖的短片段丢掉（"初一" vs "月初一"）
        if any(g in o for o in out):
            continue
        out.append(g)
        if len(out) >= topk:
            break
    return out


def _load_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default if default is not None else {}


def _iter_bodies(book: Path):
    """遍历正文文件：跳过 `_` 开头的辅助目录（_review_snapshots 里的旧稿会伪装成同一章）。"""
    for p in sorted((book / "正文").rglob("第*章*.md")):
        if any(part.startswith("_") for part in p.relative_to(book / "正文").parts):
            continue
        if ".bak_" in p.name or p.name.endswith((".orig.md", ".bak")):
            continue
        yield p


def _chapter_body(book: Path, n: int) -> tuple[Path | None, str]:
    for p in _iter_bodies(book):
        m = re.match(r"第0*(\d+)章", p.name)
        if m and int(m.group(1)) == n:
            return p, p.read_text(encoding="utf-8")
    return None, ""


def _later_chapters(book: Path, n: int, window: int) -> list[tuple[int, Path, str]]:
    out = []
    for p in _iter_bodies(book):
        m = re.match(r"第0*(\d+)章", p.name)
        if not m:
            continue
        c = int(m.group(1))
        if c <= n:
            continue
        if window and c > n + window:
            continue
        try:
            out.append((c, p, p.read_text(encoding="utf-8")))
        except OSError:
            continue
    out.sort(key=lambda x: x[0])
    return out


def _chapter_record(book: Path, n: int) -> dict:
    """读 追踪/逐章记录/第NNN章.md：权威的「本章结果」与「下一章承诺」。

    章尾钩子/落点是下游第一章直接接续的东西，比正文里的任何一句话都更不可变，
    必须单独成锚（旧版只扫状态层，会把它漏掉）。
    """
    for name in (f"第{n:03d}章.md", f"第{n}章.md"):
        p = book / "追踪" / "逐章记录" / name
        if p.exists():
            break
    else:
        return {}
    txt = p.read_text(encoding="utf-8")
    out: dict = {}
    m = re.search(r"-\s*结果[：:]\s*(.+)", txt)
    if m:
        out["result"] = m.group(1).strip()
    m = re.search(r"-\s*下一章承诺[：:]\s*(.+)", txt)
    if m:
        out["next_commitments"] = m.group(1).strip()
    return out


_LOC_STATE_RE = re.compile(r'当前\s*location\s*=\s*"([^"]*)"[，,]\s*state\s*=\s*"([^"]*)"')


def _chapter_character_changes(book: Path, n: int) -> dict[str, str]:
    """读 追踪/逐章记录/第NNN章.md 的「## 角色变化」段 —— **本章时点**的角色状态。

    ⚠️ 为什么不能用 state["characters"][x]["state"]：那是**最新章的全局快照**。
    实测：对第 10/20/35/40 章分别扫描，扫出的林小凡状态**一字不差**，全是
    「…烟火值141；账本第一次不记灵石」——那是第 46 章的值，还混进了全局资源数。
    重写第 N 章要冻结的是**第 N 章时点**的角色状态，不是最新值。
    """
    for name in (f"第{n:03d}章.md", f"第{n}章.md"):
        p = book / "追踪" / "逐章记录" / name
        if p.exists():
            break
    else:
        return {}
    txt = p.read_text(encoding="utf-8")
    m = re.search(r"##\s*角色变化\s*\n(.*?)(?=\n##\s|\Z)", txt, re.S)
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        parts = [x.strip() for x in line[2:].split("｜")]
        if len(parts) < 2 or not parts[0]:
            continue
        v = "｜".join(parts[2:]) if len(parts) >= 3 else parts[1]
        # 「【状态检查】…请根据正文判断…」是模板注入的提示词，不是事实 →
        # 只从里面抢救 location/state 真值，救不出来就当本章无有效变化。
        if "【状态检查】" in v:
            mm = _LOC_STATE_RE.search(v)
            if not mm:
                continue
            v = f"位置 {mm.group(1)}｜状态 {mm.group(2)}"
        v = v.strip("｜ ").strip()
        if v:
            out[parts[0]] = v
    return out


def _character_vocab(state: dict, bible: dict) -> list[str]:
    names = set(state.get("characters", {}).keys())
    for c in (bible.get("characters") or []):
        if isinstance(c, str):
            names.add(c)
        elif isinstance(c, dict):
            for k in ("name", "名称", "姓名"):
                if c.get(k):
                    names.add(str(c[k]))
    return sorted(names, key=len, reverse=True)


def build_anchors(book: Path, chapter: int, window: int = 0) -> dict:
    state = _load_json(book / "追踪" / "_tracking-state.json", {})
    bible = _load_json(book / "bible.json", {})
    body_path, body = _chapter_body(book, chapter)
    vocab = _character_vocab(state, bible)

    anchors: list[dict] = []

    corpus = [t for _c, _p, t in _later_chapters(book, chapter, window)] + [body or ""]

    def add(kind: str, aid: str, fact: str, extra_keys: list[str] | None = None,
            keys_only: list[str] | None = None, name_keys: bool = True):
        # keys_only：锚点只认这一个词元（数值锚点必须精确匹配，
        # 否则会被同一片段里的兄弟词元误判成"下游引用了"）。
        if keys_only:
            # 精确锚点：不再并入人名等松散词元，否则"十块灵石"会因为片段里
            # 提到了"林小凡"而被判成"几乎每章都引用"。
            anchors.append({"kind": kind, "id": aid, "fact": (fact or "").strip()[:220],
                            "keys": list(keys_only)})
            return
        keys = _tokens(fact) + [k for k in (extra_keys or []) if k]
        # 稀有短语：补上"初一/残匾/背篓"这类非数字非地名的关键实词
        keys += _rare_phrases(fact, corpus)
        if name_keys:
            # ⚠️ 人名是"每章都出现"的通用词。角色状态锚点若并入人名，
            # 会被判成"11~46 章全引用、紧耦合"而霸榜——那不是依赖，是噪音。
            for nm in vocab:
                if nm and nm in (fact or ""):
                    keys.append(nm)
        seen, uniq = set(), []
        for k in keys:
            if k and k not in seen:
                seen.add(k)
                uniq.append(k)
        if not uniq:
            return
        anchors.append({"kind": kind, "id": aid, "fact": (fact or "").strip()[:220], "keys": uniq})

    # 0) 章尾钩子 / 本章结果（权威来源：逐章记录）——优先于一切
    rec = _chapter_record(book, chapter)
    if rec.get("next_commitments"):
        add("ending_hook", "HOOK", rec["next_commitments"])
    if rec.get("result"):
        add("chapter_result", "RESULT", rec["result"])

    # 1) 本章登记的时间线事件
    for eid, ev in (state.get("timeline") or {}).items():
        if not isinstance(ev, dict):
            continue
        if int(ev.get("first_recorded_chapter") or -1) == chapter:
            add("timeline", eid, ev.get("objective_fact") or "")

    # 2) 本章埋设/变更的伏笔
    for fid, fs in (state.get("foreshadow") or {}).items():
        if not isinstance(fs, dict):
            continue
        if int(fs.get("planted_chapter") or -1) == chapter or int(fs.get("updated_chapter") or -1) == chapter:
            add("foreshadow", fid, fs.get("summary") or "")

    # 3) 本章记录的已确认事实
    for f in (state.get("facts", {}).get("confirmed") or []):
        if not isinstance(f, dict):
            continue
        src = (f.get("source") or {}).get("chapter")
        if src is not None and int(src) == chapter:
            add("fact", str(f.get("id") or ""), f.get("fact") or "")

    # 4) 角色状态：优先取**逐章记录的本章时点** → 其次取 `追踪/快照/第{chapter:03d}章.json` 历史快照 → 最后才兜底最新 state
    chg = _chapter_character_changes(book, chapter)
    snap_chars = {}
    for _sname in (f"第{chapter:03d}章.json", f"第{chapter:02d}章.json", f"第{chapter}章.json"):
        _sp = book / "追踪" / "快照" / _sname
        if _sp.is_file():
            try:
                snap_chars = (json.loads(_sp.read_text(encoding="utf-8")) or {}).get("characters") or {}
                break
            except Exception:
                pass
    appearing = [nm for nm in vocab if nm and nm in (body or "")]
    ch_state = {}
    for nm in list(chg.keys()) + [x for x in appearing if x not in chg]:
        sc = snap_chars.get(nm) if isinstance(snap_chars, dict) else None
        c = sc if isinstance(sc, dict) else ((state.get("characters") or {}).get(nm) or {})
        loc = c.get("location") or c.get("position") or ""
        st = c.get("state") or c.get("current_state") or c.get("身心状态") or ""
        tp_label = f"第{chapter}章快照时点" if isinstance(sc, dict) else "⚠️最新章快照(非本章时点，仅供参考)"
        if chg.get(nm):
            ch_state[nm] = {"change": chg[nm], "timepoint": f"第{chapter}章时点(逐章记录权威)",
                            "snapshot_location": loc, "snapshot_state": st}
            add("character_change", nm, f"{nm}｜{chg[nm]}", name_keys=False)
        else:
            ch_state[nm] = {"location": loc, "state": st, "timepoint": tp_label}
            if loc or st:
                add("character_state", nm, f"{nm}｜位置 {loc}｜状态 {st}", name_keys=False)

    # 5) 正文里的数值类事实（细纲不写、但下游会引用）——只取硬值词元
    for t in _hard_tokens(body or ""):
        idx = (body or "").find(t)
        snippet = ((body or "")[max(0, idx - 20): idx + 30].replace("\n", " ")) if idx >= 0 else ""
        add("numeric", f"N-{t}", snippet or t, keys_only=[t])

    # 6) 全局资源当前值（烟火值 = 141 是"截至最新章"的值，不是第 N 章的值）
    #    ⚠️ 不能当 protected：重写第29章不该被逼着保住第46章的 141。
    #    单独列出仅供人看，不参与"必须保留"判定。
    global_current = [
        {"kind": "global_current", "id": f"cost.{k}", "fact": f"{k} = {v}"}
        for k, v in (state.get("cost") or {}).items()
    ]

    # ---- 下游回查 ----
    later = _later_chapters(book, chapter, window)
    later_texts = [(c, t) for c, _p, t in later]
    n_later = max(1, len(later_texts))

    _df_cache: dict[str, int] = {}

    def df_of(k: str) -> int:
        if k not in _df_cache:
            _df_cache[k] = sum(1 for _c, t in later_texts if k in t)
        return _df_cache[k]

    # ⚠️ 通用词门槛：在 ≥30% 下游章都出现的词（坊市/杂货铺/铺子…）命中**不算依赖**。
    # 修复前：只要任一词元命中就记一章引用 → 第20章 RESULT 的证据是"坊市…"、
    # 第35章是"街角的巷子里排着长龙"，与 fact 毫无关系，protected 全是水分。
    DF_MAX = max(2, int(0.3 * n_later))

    protected, unreferenced = [], []
    for a in anchors:
        refs, seen_ch = [], set()
        for cno, _p, text in later:
            hit = [k for k in a["keys"] if k and k in text]
            # 只认"稀有词元"命中；清一色通用词命中 → 判为未被引用
            rare = [k for k in hit if 1 <= df_of(k) <= DF_MAX]
            if not rare:
                continue
            if cno in seen_ch:
                continue
            seen_ch.add(cno)
            i = text.find(rare[0])
            refs.append({
                "chapter": cno,
                "matched": rare[:3],
                "evidence": text[max(0, i - 24): i + 36].replace("\n", " "),
            })
        a["referenced_by"] = [r["chapter"] for r in refs]
        a["evidence"] = refs[:5]
        a["ref_count"] = len(refs)
        # 紧耦合：重写章节之后的 3 章内就引用了 —— 这类改写即断，优先级最高
        a["tight"] = [c for c in a["referenced_by"] if c <= chapter + 3]
        # 精度分层：数值/物件逐字咬合 = high；主题性词元延续 = medium；
        # 角色名必然出现在所有章，只能算 low（要看的是 location/state 值，不是人名）
        if a["kind"] in ("ending_hook", "chapter_result"):
            a["precision"] = "authoritative(逐章记录权威来源)"
        elif a["kind"] == "numeric":
            a["precision"] = "high(逐字咬合)"
        elif a["kind"] == "character_change":
            a["precision"] = "medium(第%d章时点角色变化)" % chapter
        elif a["kind"] in ("timeline", "foreshadow", "fact"):
            a["precision"] = "medium(主题词元延续，需人工确认不可变点)"
        elif a["kind"] == "character_state":
            a["precision"] = "low(最新章快照，非本章时点，仅供参考)"
        else:
            a["precision"] = "low(仅供定位)"
        (protected if refs else unreferenced).append(a)

    protected.sort(key=lambda x: (-len(x["tight"]), -x["ref_count"], x["kind"]))
    unreferenced.sort(key=lambda x: x["kind"])

    # ---- 悬空引用：下游在用，本章/状态层却找不到 ----
    src_text = (body or "") + "\n" + "\n".join(a["fact"] for a in anchors)
    dangling = []
    for cno, _p, text in later:
        for t in (_hard_tokens(text) + _PLACE_RE.findall(text or "")):
            if t and t not in src_text:
                dangling.append({"chapter": cno, "token": t})
    # 只保留在 ≥2 个下游章出现过的词元，降噪
    counts: dict[str, set[int]] = {}
    for d in dangling:
        counts.setdefault(d["token"], set()).add(d["chapter"])
    # 到处都出现的词（在 ≥80% 下游章出现）是通用词不是悬空引用，丢掉
    n_later = max(1, len(later))
    dangling = [
        {"token": t, "chapters": sorted(c)}
        for t, c in counts.items()
        if 2 <= len(c) < 0.8 * n_later
    ]
    dangling.sort(key=lambda x: -len(x["chapters"]))

    return {
        "book": str(book),
        "chapter": chapter,
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "source_body": str(body_path.relative_to(book)) if body_path else None,
        "scan_range": [later[0][0], later[-1][0]] if later else None,
        "state_revision": state.get("state_revision"),
        "last_committed_chapter": state.get("last_committed_chapter"),
        "appearing_characters": ch_state,
        "summary": {
            "anchors_total": len(anchors),
            "protected": len(protected),
            "unreferenced": len(unreferenced),
            "dangling": len(dangling),
        },
        "protected_anchors": protected,
        "unreferenced_anchors": unreferenced,
        "global_current": global_current,
        "dangling_references": dangling[:30],
    }


def _render_for_writer(res: dict) -> str:
    """把 protected_anchors 压成可直接塞进 writer prompt 的「不可变清单」。

    立场（用户 2026-09-26 拍板）：重写＝**全盘重写**；只有章尾钩子、已入账伏笔
    不可动。其余（句子/段落/节奏/对话/描写/场景组织）全部放开。

    因此清单**分两级、且刻意做减法**——宁可少锁一条，也不多锁一条：
      · 一级（必锁）：章尾钩子 / 已入账伏笔 / 本章结果态（结果同、表达可换）
      · 二级（仅紧耦合才锁）：下游 3 章内真的咬住的数值·时间线事件
    ⚠️ 角色变化**不进清单**：那是"剧情怎么写"的范畴，交给细纲，锁了就等于不许重写。
    """
    n = res["chapter"]
    by_kind: dict[str, list[dict]] = {}
    for a in res["protected_anchors"]:
        by_kind.setdefault(a["kind"], []).append(a)

    def fmt(a: dict) -> str:
        chs = a["referenced_by"]
        tail = f"（下游第 {chs[0]} 章起引用）" if chs else "（下游未直接引用，但已入账）"
        tight = f" ⚠️紧耦合{ a['tight'][:3] }" if a.get("tight") else ""
        return f"  - {a['fact'][:110]}{tail}{tight}"

    L = [f"【第{n}章 · 重写不可变清单】（确定性生成：状态层+下游反向扫描，非人工填写）"]
    if by_kind.get("ending_hook"):
        L.append("■ 章尾钩子（下一章直接接续点，必须保留落点）")
        L += [fmt(a) for a in by_kind["ending_hook"]]
    fs = by_kind.get("foreshadow", [])
    if fs:
        L.append("■ 已入账伏笔（不得改内容、不得提前回收、不得悄悄作废）")
        L += [fmt(a) for a in fs]
    if by_kind.get("chapter_result"):
        L.append("■ 本章结果态（必须落到同一结果，表达可全换）")
        L += [fmt(a) for a in by_kind["chapter_result"]]

    soft: list[dict] = []
    for k in ("numeric", "timeline", "fact"):
        for a in by_kind.get(k, []):
            if not a.get("tight"):
                continue
            if k == "numeric":
                # ⚠️ numeric 的 fact 是"正文上下文片段"。若整段塞进不可变清单，
                # 指令就变成"这段描写逐字保留"——与"全盘重写"正面冲突。
                # 只锁词元本身：下游提到「二十年前」时它得还是「二十年前」，
                # 至于那句话怎么写，随便。
                tok = a["id"][2:] if a["id"].startswith("N-") else a["fact"]
                soft.append({**a, "fact": f"数值「{tok}」"})
            else:
                soft.append(a)
    if soft:
        L.append("■ 下游 3 章内咬住的具体值/事件（值本身不可改；承载它的句子照常重写）")
        L += [fmt(a) for a in soft]

    L.append("■ 除以上之外：句子、段落、节奏、对话、描写、场景组织**全部放开**，按本章细纲重写。")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="重写前冻结：下游依赖锚点扫描（只读）")
    ap.add_argument("--book", required=True)
    ap.add_argument("--chapter", type=int, required=True)
    ap.add_argument("--window", type=int, default=0, help="只扫后续 N 章（0=全部）")
    ap.add_argument("--out", help="输出 JSON 路径（缺省不落盘）")
    ap.add_argument("--json", action="store_true", help="stdout 输出 JSON")
    ap.add_argument("--for-writer", action="store_true",
                    help="输出可直接注入 writer prompt 的「不可变清单」")
    args = ap.parse_args()

    book = Path(args.book).resolve()
    if not book.is_dir():
        print(f"❌ 书目录不存在: {book}", file=sys.stderr)
        return 2
    res = build_anchors(book, args.chapter, args.window)

    if args.out:
        out = Path(args.out)
        if not out.is_absolute():
            out = book / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"✅ 已写入 {out}")

    if args.for_writer:
        print(_render_for_writer(res))
        return 0

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    s = res["summary"]
    print(f"第{args.chapter:03d}章 · 下游依赖扫描（状态修订 {res['state_revision']}，已提交至 {res['last_committed_chapter']} 章）")
    print(f"扫描范围: 第{res['scan_range'][0]}章 ~ 第{res['scan_range'][1]}章" if res["scan_range"] else "扫描范围: 无后续章节")
    print(f"锚点 {s['anchors_total']} 条 → 被下游引用 {s['protected']} / 未被引用 {s['unreferenced']}；悬空引用 {s['dangling']} 项")
    print("\n== protected_anchors（重写必须原样保留，按引用章数排序）==")
    for a in res["protected_anchors"][:20]:
        chs = ",".join(str(c) for c in a["referenced_by"][:8])
        tight = f"  紧耦合: {a['tight']}" if a.get("tight") else ""
        print(f"  [{a['kind']}] {a['id']}  引用章: {chs}{'…' if len(a['referenced_by']) > 8 else ''}{tight}")
        print(f"      fact: {a['fact'][:90]}")
        if a["evidence"]:
            e = a["evidence"][0]
            print(f"      证据 ch{e['chapter']}: …{e['evidence'][:60]}…")
    if res["dangling_references"]:
        print("\n== dangling（下游在用、本章/状态层查不到，先查再动）==")
        for d in res["dangling_references"][:10]:
            print(f"  「{d['token']}」 出现在 {d['chapters']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
