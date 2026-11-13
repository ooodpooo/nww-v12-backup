#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nww_review_cycle.py — 审稿闭环确定性流程控制（V12.5，2026-09-06）。

把 long-write Phase 4.6 审稿闭环从"宿主 LLM 自觉执行"变成"确定性流程控制"。
脚本负责流程控制（必须2轮、必须打分、净提升<3分停、best-snapshot），
宿主 LLM 只负责"打分"和"修订"两个单一动作，无法选择性跳过。

三个 phase：
  score    输出审稿 prompt + 评分表（含 I-I 细纲承诺兑现度强制对照），宿主 LLM 按 prompt 打分，写入 追踪/评分/第N章.json
  revise   输出修订指令（基于上一轮低分维度），宿主 LLM 按指令修订正文
  finalize 写入 meta.reviewed=true + 最终分数 + 轮次记录，完成闭环

I-I 维度（2026-09-08 新增，承诺兑现验收门）：
  审稿时必须读取本章细纲，逐条对照节拍/收益/钩子/纪律的兑现情况。
  未兑现项必须挂正文证据，禁止只给总分不挂证据。
  这是 NWW 流程"细纲承诺→正文交付"之间的结构化验收门，
  解决"细纲写了但正文没兑现却通过质检"的缺口。

设计原则：
- 流程控制是确定性的（轮次上限、停手阈值、best-snapshot 由脚本判定）
- 打分内容由 LLM 做（语义理解不可替代），但打分格式由脚本强制（结构化 JSON）
- 每一轮的分数落盘到 追踪/评分/第N章.json，供 check 时输出质量趋势
- best-snapshot：每轮打分后备份正文，最终保留最高分版本
- 兼容模式：meta.reviewed 缺失时 commit 仅 WARNING，不 BLOCK（存量书兼容）
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

# S5 命名消歧：此处 I-A~I-J 是 LLM 打分维度（审稿闭环），与 quality/detect_internal.py 的
# 确定性检测器同名不同义。仅 I-G（解释性独白）和 I-H（标题核验）语义重合。
# 映射表见 docs/评分体系融合方案_20260913.md。
REVIEW_DIMENSIONS = [
    ("I-A", "截断/工程词", "正文是否被截断、是否残留工程词（TODO/FIXME/占位符）"),
    ("I-B", "复读检测", "是否存在句内/段内/跨段机械复读，以及同一信息换措辞重复（同一观察/判断/决定出现 ≥2 次即扣分，须挂位置证据）"),
    ("I-C", "毒句式", "是否存在AI味毒句式（'不禁'/'仿佛'/'似乎'泛滥等）"),
    ("I-D", "负向词", "是否命中负向词表（nww_enforce.py 同词表）"),
    ("I-E", "时代器物词", "是否存在时代错配的器物/词汇"),
    ("I-F", "声纹一致性", "角色对话声纹是否与角色卡一致"),
    ("I-G", "解释性独白", "是否存在大段解释性独白（show don't tell 违反）"),
    ("I-H", "标题行核验", "标题格式/内容是否与细纲一致"),
    ("I-I", "细纲承诺兑现度", "对照细纲节拍/爽点/收益/钩子逐条检查正文是否兑现；未兑现项必须挂正文证据（细纲条目 vs 正文位置），禁止只给总分不挂证据"),
    ("I-J", "伏笔卫生", "追踪/伏笔.md 中 status='需处理' 的伏笔若连续≥2章未在正文或规划层出现，扣分并挂证据；无'需处理'伏笔时本维满分"),
    # 2026-10-25 P1-6（外部审稿归因）：行为合理性与因果链——此前十维全为机械指纹，
    # 「反派降智/逃命者细致化妆/巧合推进剧情/转折像按开关」全链零覆盖。
    ("I-K", "行为合理性", "关键配角/反派的行动是否由其身份、利益、性格驱动：受挫后反应与身份不符（该上报/追问却轻易退场）、危机中做与处境优先级脱节的事（如逃命者细致化妆）、为剧情需要降智 = 扣分并挂证据（人物+行为+应有反应）"),
    ("I-L", "因果与巧合", "无因果铺垫的巧合（恰好撞入/恰好触发/恰好失效）≥2 处、关键转折发生在逻辑必然时点之外（精确如按开关）、本章启用的关键规则/道具/能力无任何前置铺垫突然生效 = 扣分并挂证据"),
]

# 创作价值观察轴（第二刀 2026-09 添加）。独立于 I-A~I-I，不入 PASS_SCORE。
# 作用：把 revision 从"只纠错"升级为"也识别不可替代价值"。它回答：
#   "删掉本章最有价值部分，读者是否损失明显？" 并指出 记忆点/情绪峰值/人物变化。
# 当前是观察报告项（不扣分、不改变总分与通过逻辑），高层/人工策略可依据它决定是否增强。
CREATIVE_VALUE_CHECK = [
    ("E-A", "章节记忆点", "本章是否存在一个读者会记住的不可替代场景/画面/冲突？"),
    ("E-B", "情绪峰值", "本章是否有明确、有落点的情绪最高点？"),
    ("E-C", "人物变化", "本章是否产生人物认知/关系/状态可见变化？"),
]

# 阈值
PASS_SCORE = 85
MAX_ROUNDS = 2
MIN_IMPROVEMENT = 3  # 净提升 < 3 分即停
# S4 维度地板（rubric_90score_v3.md）：任一维度 <70 时即使总分通过也需修订。
# 2026-10-19 外脑裁决（边界项3）定为硬约束：分数达标但维度触底 = 硬约束失败，
# 提交边界（finalize 不标 reviewed + commit 评分门 BLOCK）必须阻断，不能被 best >= 85 覆盖。
# 提升为模块级常量（原为 phase_after_score 内局部变量）：nww_track_commit 侧
# _review_dimension_floor() 延迟导入此值作单一来源，防止三处常量漂移。
DIMENSION_FLOOR = 70


def resolve_prose(project: Path, chapter: int, body_path: Path | None = None) -> Path | None:
    """定位正文文件。dry_run 时 body_path 优先。"""
    if body_path is not None:
        return body_path
    try:
        from nww_track_commit import resolve_chapter_file

        return resolve_chapter_file(project, chapter)
    except Exception:
        pass
    body = project / "正文"
    if not body.is_dir():
        return None
    # 快照目录不是正文：快照文件名（第027章_round1.md）看不出身份，只能按目录层排除。
    for p in body.rglob(f"第{chapter:03d}章*.md"):
        if "_原稿_" in p.name or "_review_snapshots" in p.parts:
            continue
        return p
    return None


def score_dir(project: Path, dry_run_root: Path | None = None) -> Path:
    """评分目录。

    dry_run_root 显式传入（dry_run 预览）→ 落本次 execution 的 artifact root（如
    追踪/dry_run/<exec_id>/评分），绝不触碰正式 追踪/；缺省 None → 原正式路径（行为不变）。
    """
    if dry_run_root is not None:
        return dry_run_root / "评分"
    return project / "追踪" / "评分"


def load_scores(project: Path, chapter: int, dry_run_root: Path | None = None) -> list[dict[str, Any]]:
    """加载历史评分（按轮次排序）。dry_run_root 显式传入时读同一 artifact root。"""
    path = score_dir(project, dry_run_root) / f"第{chapter:03d}章.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "rounds" in data:
            return data["rounds"]
    except Exception:
        pass
    return []


def save_scores(project: Path, chapter: int, rounds: list[dict[str, Any]],
                dry_run_root: Path | None = None) -> None:
    """保存评分到 追踪/评分/第N章.json（dry_run_root 传入时改写 artifact root）。"""
    d = score_dir(project, dry_run_root)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"第{chapter:03d}章.json"
    payload = {
        "chapter": chapter,
        "rounds": rounds,
        "best_score": max((r.get("total", 0) for r in rounds), default=0),
        "final_round": len(rounds),
        "updated_at": int(time.time()),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def backup_prose(prose: Path, chapter: int, round_num: int,
                 review_id: str | None = None, overwrite: bool = False,
                 snapshot_root: Path | None = None) -> Path | None:
    """备份正文为 best-snapshot。
    review_id 提供时写入 _review_snapshots/{review_id}/{章}_round{N}.md（不同 review run 隔离）；
    缺省回退旧平铺路径（兼容存量）。
    同 review_id+round 已存在且未显式 overwrite → 拒绝，不静默覆盖历史证据（P0-3）。
    """
    if not prose.is_file():
        return None
    # 基准 = 正文实际所在目录（分卷书是 正文/第一卷/）。与 _snapshot_dir 必须同源，
    # 否则写入端与读取端会指向不同目录（2026-09-13 实测过这个漂移）。
    # snapshot_root 显式传入时（dry_run）→ 落本次 execution 的 artifact root，
    # 绝不写正式 _review_snapshots/；缺省 None → 原语义（正文实际所在目录同级）。
    backup_dir = snapshot_root if snapshot_root is not None else (prose.parent / "_review_snapshots")
    if review_id:
        backup_dir = backup_dir / review_id
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"第{chapter:03d}章_round{round_num}.md"
    if backup_path.exists() and not overwrite:
        print(f"WARN: snapshot 已存在且未显式覆盖，拒绝写入: {backup_path}", file=sys.stderr)
        return None
    shutil.copy2(prose, backup_path)
    return backup_path


def print_score_prompt(project: Path, chapter: int, prose: Path, round_num: int) -> None:
    """输出审稿打分 prompt（宿主 LLM 按此打分）。"""
    print("=" * 60)
    print(f"【审稿闭环 · 第 {round_num} 轮 · 打分】")
    print("=" * 60)
    print(f"  项目: {project}")
    print(f"  章号: 第{chapter}章")
    print(f"  正文: {prose}")
    print()
    # 定位细纲文件（I-I 维度必须对照）
    outline_dir = project / "大纲"
    outline_file = None
    if outline_dir.is_dir():
        for p in sorted(outline_dir.glob(f"细纲_第{chapter:03d}章*.md")):
            outline_file = p
            break
        if outline_file is None:
            for p in sorted(outline_dir.glob(f"细纲_第{chapter}章*.md")):
                outline_file = p
                break
    print("  【I-I 维度强制要求】必须先读取本章细纲，逐条对照正文兑现情况：")
    if outline_file:
        print(f"  细纲路径: {outline_file}")
    else:
        print(f"  细纲路径: 未找到（大纲/细纲_第{chapter:03d}章*.md）—— I-I 维度按 0 分计并标记 BLOCK")
    print("  对照清单: ① CCv2 章级字段逐行验收（Chapter Core/Chapter Goal/Ending State/Emotion Curve/Information Boundary；存量书按旧「收益兑现+章尾钩子」双字段）")
    print("            ②节拍表每条承诺（含量化指标：字数上限/位置/种子/禁令）是否在正文落地 ③纪律项与铺陈删除测试是否遵守")
    print("  未兑现项必须挂证据: 「细纲第X条: <原文> → 正文: <缺失/偏离/位置>」")
    print("  禁止只给 I-I 总分不挂逐条证据——无证据的 I-I 评分视为无效。")
    print("  【数字承诺防洗白（2026-10-25 P3-10）】凡细纲含具体数字的承诺（价格/数量/天数/限购/库存），")
    print("  delivered=true 必须给出「正文数字=细纲数字」的逐字证据；正文出现细纲没有的数字")
    print("  （凭空的单价/库存/周期/份额）= I-I 扣分并在 promise_audit 标 delivered=false。")
    print("  禁止用正文反推细纲承诺（正文写了十块就把细纲的两块判为已兑现=洗白，按失守处理）。")
    print()
    # I-E 时代器物词：声道制 × 语域三档（2026-10-25 用户裁决）。
    # 档位权威 = bible.era_constraints.register_mode（strict/balanced/modern，缺省 strict）。
    # strict   修真正典：旁白零现代词；主角内心+对白可现代词（禁 gloss）；土著禁概念词
    # balanced 修主现辅：修真语域为主，现代词点缀合法（密度/效果由审稿裁量）
    # modern   现代语感：现代词是风格不是穿帮；审的是世界自洽与风格一致性
    _era = {}
    try:
        _bible_p = project / "bible.json"
        if _bible_p.is_file():
            _era = (json.loads(_bible_p.read_text(encoding="utf-8")).get("era_constraints") or {})
    except Exception:  # noqa: BLE001 — bible 缺失/损坏按 strict 口径
        _era = {}
    _rmode = str(_era.get("register_mode") or "strict").lower()
    _rmode = _rmode if _rmode in ("strict", "balanced", "modern") else "strict"
    print(f"  【I-E 维度强制要求 · 语域档位 = {_rmode}（bible.era_constraints.register_mode 为权威）】")
    if _rmode == "strict":
        print("  打分前必须先读 bible.json 的 era_constraints 与本章细纲，按声道判定：")
        print("  ① 旁白/公共描述出现现代词（器物或概念词）= 违规扣分（如叙述里写「双标」")
        print("    「KPI」「沙发」）——修仙界叙述只允许修真界语域；")
        print("  ② 现代概念词出现在主角（穿越者）对白或内心独白 = 合法，不扣分；但正文若")
        print("    给它加了修仙界式标注/解释（如「双标，即修真界所谓…」）= 违规（禁 gloss）；")
        print("  ③ 现代概念词出现在修仙界土著的对白或内心 = 严重违规，重扣；")
        print("  ④ 命名传播链（现代物品名/诙谐现代词）：首次命名权归主角——旁白（主角视角）")
        print("    与主角对白可指名；其他角色必须已亲历主角传播（本章或前章在场听到/看到")
        print("    价牌，对照细纲命名节拍核验）才能使用该词，未传播的新客/旁观者凭空说出")
        print("    商品名或诙谐词=违规（应使用替代词：黑水/凡膏/红条等）；传播中可有学舌")
        print("    痕迹（「辣…辣条？」）= 合法。金手指系统文本与章名（标题行）合法。")
    elif _rmode == "balanced":
        print("  本书为「修主现辅」档：修仙界语域是整体基调，现代词允许适当点缀。判定：")
        print("  ① 主基调检查：叙述、比喻、度量衡、称呼整体应是修真界语域；现代词点缀")
        print("    （低频、有对照/喜剧效果）合法不扣分；")
        print("  ② 密度裁量：现代概念词在旁白高频出现（大体量、无效果目的的堆砌）= 扣分；")
        print("  ③ 主角（穿越者）对白/内心用现代词 = 合法；修仙界土著满口现代概念词 = 扣分")
        print("    （土著个别「学舌」桥段可豁免，须有剧情支撑）；")
        print("  ④ 无来源的现代器物（叙述里凭空出现沙发/手机类实物）= 世界一致性问题，扣分；")
        print("    设定内商品名可指名交易，旁白禁做现代原理/来历解说。")
    else:  # modern
        print("  本书为「现代语感」档：用现代语感描绘铺垫修仙界（宗门当大厂、修士当打工人")
        print("    式行文）。现代概念词是风格选择，不是穿帮，不得因此扣分。判定：")
        print("  ① 风格一致性：整体现代语感应贯穿（若忽而现代忽而仿古的语感漂移 = 扣分）；")
        print("  ② 世界自洽：物理世界仍是修仙界（灵石/符箓/飞剑/宗门结构），无来源的真实")
        print("    科技产物（角色凭空掏出手机且无金手指设定支撑）= 世界一致性问题，扣分；")
        print("  ③ 设定内商品/金手指文本照旧合法。")
    print("  每个扣分项必须挂证据：词 + 正文位置 + 声道（旁白/主角/土著）+ 说话人。")
    print("  无逐项证据的 I-E 评分视为无效，不得低于 90。")
    print("  era_evidence 数组每条含 word/location/channel/speaker/why（channel ∈ 旁白|主角对白|主角内心|土著对白|土著内心）。")
    print()
    # I-J 伏笔卫生：注入需处理伏笔清单
    foreshadow_path = project / "追踪" / "伏笔.md"
    pending_foreshadows = []
    if foreshadow_path.exists():
        for line in foreshadow_path.read_text(encoding="utf-8").splitlines():
            if "需处理" in line:
                pending_foreshadows.append(line.strip())
    print("  【I-J 维度强制要求】检查追踪/伏笔.md 中 status='需处理' 的伏笔：")
    if pending_foreshadows:
        print(f"  需处理伏笔（{len(pending_foreshadows)} 条）：")
        for p in pending_foreshadows[:5]:
            print(f"    {p}")
        print("  打分标准：若连续≥2章未在正文或规划层出现 → 扣分并挂证据（伏笔ID+缺失章数）")
        print("  若本章已安排回收/推进/显式延期 → 满分")
    else:
        print("  无'需处理'伏笔 → 本维满分")
    print()
    print("  请按以下维度逐项打分（0-100），并给出综合分：")
    print()
    for code, name, desc in REVIEW_DIMENSIONS:
        print(f"  {code}  {name:12s}  {desc}")
    print()
    print("  创作价值观察（独立报告项，不进入综合分/不扣分）：")
    print('    请额外输出 creative_value 字段（若删掉本章最有价值部分，读者是否损失明显？）')
    for code, name, desc in CREATIVE_VALUE_CHECK:
        print(f"    {code}  {name:10s}  {desc}")
    print("    creative_value 是观察报告（记忆点/情绪峰值/人物变化），供后续可选增强使用，不参与通过判定。")
    print()
    print("  输出格式（必须是合法 JSON，写入 追踪/评分/第N章.json 的 rounds 数组）：")
    print("  {")
    print('    "round": <轮次>,')
    print('    "scores": {"I-A": <分>, "I-B": <分>, ..., "I-L": <分>},')
    print('    "promise_audit": [{"outline_item": "<细纲条目原文>", "delivered": true/false, "evidence": "<正文位置或缺失说明>"}],')
    print('    "era_evidence": [{"word": "<现代词>", "location": "<正文位置>", "channel": "<旁白|主角对白|主角内心|土著对白|土著内心>", "speaker": "<说话人，非对白为空>", "why": "<违反哪条声道规则>"}],  # I-E<90 时必填，逐项证据')
    print('    "total": <综合分 0-100>,')
    print('    "weak_dimensions": ["<低分维度代码>", ...],')
    print('    "creative_value": {"memory_point": "...", "emotion_peak": "...", "character_shift": "..."},')
    print('    "comment": "<一句话总评>"')
    print("  }")
    print()
    print(f"  通过线: 综合分 >= {PASS_SCORE}")
    print(f"  停手线: 与上一轮净提升 < {MIN_IMPROVEMENT} 分即停（保留最高分版本）")
    print(f"  最大轮次: {MAX_ROUNDS} 轮")
    print()
    print("  打分完成后，调用（scores_json 为上方 JSON 中的 scores 字段，用法见 --scores 参数说明）:")
    print(f"    python3 nww_review_cycle.py --project {project} --chapter {chapter} --phase after-score --score <综合分> --round {round_num} --scores '<scores_json>'")
    print("=" * 60)


def print_revise_prompt(project: Path, chapter: int, prose: Path, round_num: int, weak_dims: list[str],
                        failed_promises: list[dict[str, Any]] | None = None) -> None:
    """输出修订指令（基于低分维度）。

    2026-09-13 小九/老赵事故复盘补：修订指令必须注入「重写路径核原则」——
    voice 名单 / 角色卡三字段 / 禁造实体称呼。修订是重写的一种（改已有正文），
    与整段重写同危险等级：不注入细纲+角色卡约束，修订 LLM 会沿旧稿漂移
    （造未登记称呼/角色、代词翻转）。"""
    print("=" * 60)
    print(f"【审稿闭环 · 第 {round_num} 轮 · 修订】")
    print("=" * 60)
    print(f"  综合分 < {PASS_SCORE}，需要修订。")
    print(f"  低分维度: {', '.join(weak_dims) if weak_dims else '(未指定，按综合评语修订)'}")
    print()
    print("  修订原则:")
    print("  1. 只针对低分维度修订，不改动已达标部分")
    print("  2. 修订后字数波动不超过 ±15%；但当前正文低于细纲字数目标下限时，")
    print("     修订必须扩写至目标区间（欠字扩写不受 ±15% 上浮限制，禁止缩写）")
    print("  3. 保留原章节标题和核心事件")
    print("  4. 修订完成后重新打分（第2轮）")
    print("  5. 【时代语域（bible.era_constraints：register_mode 三档 + policy）】")
    print("     修订必须维持本书语域档位：strict=旁白零现代词、主角内心/对白可用且禁")
    print("     gloss、土著禁现代概念词（土著说了「双标」类词必须改成修真界语域）；")
    print("     balanced=修真语域为主基调、现代词点缀不得扩成堆砌；modern=现代语感保持")
    print("     贯穿不漂移。【命名传播链】商品名/诙谐词首次命名权归主角：未传播的新客")
    print("     对白必须改成替代词（黑水/凡膏/红条等），已传播角色与主角台词保持原名，")
    print("     禁止把土著台词里的商品名简单删掉而不补替代指称。")
    print()
    # ---- 重写路径核原则（事故复盘补）----
    print("  【重写路径核原则 · 修订动笔前必读】")
    print("  修订 = 对已有正文的再加工，与整段重写同危险等级。修订前必须完成:")
    print()
    print("  ① 重读 大纲/细纲_第N章.md 的「出场角色voice锚点」名单。修订后正文里")
    print("     出现的每个人名/称呼必须属于: voice名单 或 bible/设定已登记。")
    print("     两者都不是 → 自造实体, 禁止写入, 改回名单内写法或删掉。")
    print("  ② 重读视角角色的 设定/角色/{角色}.md 三字段: 性别/身份/他人称呼。")
    print("     视角段主体代词必须与卡的性别一致(女子=她)。他人对白唤她只能用")
    print("     已登记称呼, 不得造新称呼(如 小九/老赵 类)。")
    print("  ③ 旧稿里的代词翻转/设定矛盾随修订一并修回(老宋头'卖劣质丹药'类),")
    print("     不得原样保留。判定依据 = bible.json + 设定/** + 前章已提交正文,")
    print("     不是旧稿本身。")
    print("  ④ 修订交付前自检: 正文里每个 老X/小X/阿X/X掌柜 式称呼与每个首次出现")
    print("     的人名, 过一遍 ①②。命中'无登记来源'一律改回或删掉。")
    print("  ⑤ 修订落盘后, 父流程必须重跑 nww_name_check.py --mode commit,")
    print("     nickname_drift / role_drift 闸 ERROR 不处置不得 finalize。")
    print()
    dim_names = {code: name for code, name, _ in REVIEW_DIMENSIONS}
    for code in weak_dims:
        if code in dim_names:
            print(f"  - {code} {dim_names[code]}: 重点修订此维度")
    print()
    # 闸2·细纲层：未兑现承诺逐条进修订指令（证据验收，指名道姓修那条）
    if failed_promises:
        print("  【细纲承诺未兑现 · 必须逐条修复（证据验收层，与分数无关）】")
        for _p in failed_promises:
            print(f"  - 细纲承诺: {_p.get('outline_item')}")
            print(f"    评审证据: {_p.get('evidence') or '(未给出，请对照细纲原文定位)'}")
        print("  修复后每条必须可在正文中指认对应内容，禁止只调叙述不改实质。")
        print()
    print("  创作价值增强（可选，非强制）：若正文缺不可替代价值，可在不改变")
    print("  核心事件、人物状态、伏笔、因果与字数范围的前提下，做轻增强——")
    print("    优先增强：一个动作细节 / 一个关键反应 / 一个场景落点 / 一处情绪递进。")
    print("    禁止：新增剧情、修改因果、添加设定、扩写失控。")
    print("    请基于 creative_value 观察（记忆点/情绪峰值/人物变化）给出具体增强建议，")
    print("    避免只说'加强描写/增加细节/提升感染力'这类空泛话。")
    print()
    print("  修订完成后，调用:")
    print(f"    python3 nww_review_cycle.py --project {project} --chapter {chapter} --phase score --round {round_num + 1}")
    print("=" * 60)


def print_finalize(project: Path, chapter: int, rounds: list[dict[str, Any]], passed: bool, stopped_reason: str) -> None:
    """输出最终结果。"""
    best = max((r.get("total", 0) for r in rounds), default=0)
    print("=" * 60)
    print("【审稿闭环 · 完成】")
    print("=" * 60)
    print(f"  章号: 第{chapter}章")
    print(f"  轮次: {len(rounds)} / {MAX_ROUNDS}")
    print(f"  最高分: {best}")
    print(f"  最终分: {rounds[-1].get('total', 0) if rounds else 0}")
    print(f"  是否通过: {'是' if passed else '否'}")
    print(f"  停止原因: {stopped_reason}")
    print()
    print("  各轮分数:")
    for r in rounds:
        print(f"    第{r.get('round', '?')}轮: {r.get('total', 0)}  弱维度: {r.get('weak_dimensions', [])}")
    print()
    print("  meta.reviewed 已写入事务（finalize 阶段自动处理）")
    print("=" * 60)


def phase_score(project: Path, chapter: int, round_num: int, body_path: Path | None = None) -> int:
    """score phase: 输出打分 prompt。"""
    prose = resolve_prose(project, chapter, body_path=body_path)
    if prose is None:
        print(f"ERROR: prose not found for chapter {chapter}", file=sys.stderr)
        return 2
    print_score_prompt(project, chapter, prose, round_num)
    return 0


def _promise_failures_of(promise_audit: Any) -> list[dict[str, Any]]:
    """提取 promise_audit 中未兑现条目（闸2·细纲层判定核心）。

    只有显式 delivered=false 的条目才算未兑现（评审必须明确断言，格式噪声不闸人）；
    非列表/缺 outline_item/缺 delivered 键的畸形输入一律忽略——证据验收闸
    宁可漏判一次（下轮修订还会再核），不可拿垃圾数据误杀合格章。
    """
    if not isinstance(promise_audit, list):
        return []
    return [p for p in promise_audit
            if isinstance(p, dict) and p.get("outline_item") and p.get("delivered") is False]


# ---------- 2026-11-07：finalize best 轮「先过闸再比高分」（只增不改 + env 回滚） ----------
_GATE_FIRST_SWITCH = "NWW_FINALIZE_GATE_FIRST"


def _gate_first_enabled() -> bool:
    """best 轮选择开关：闸优先（默认）还是纯最高分（旧行为）。

    env NWW_FINALIZE_GATE_FIRST：未设置或其它值 → 开启新行为（先过滤合格轮次再取最高分）；
    取 0/false/off/no（忽略大小写与首尾空白）→ 回旧行为（纯按 total 取最大）。
    """
    import os

    return os.getenv(_GATE_FIRST_SWITCH, "1").strip().lower() not in {"0", "false", "off", "no"}


def _round_gate_ok(round_entry: Any) -> bool:
    """判某轮是否有资格成为 best：维度地板通过 + 细纲承诺全兑现。

    复用 _promise_failures_of 与 DIMENSION_FLOOR，不另立一套判定：
      - scores 为 dict：任一数值维度 < DIMENSION_FLOOR → 不合格；
        scores 非 dict（存量旧格式，无法判定维度）→ 不因此否决，与 finalize 的
        floor_bad「无明细则不闸」兼容语义一致；
      - 存在显式 delivered=false 的细纲承诺 → 不合格；
        无 promise_audit（存量旧格式）→ 不因此否决。
    非 dict 轮次记录视为不合格（仅会在回退池里按旧逻辑参与 max，与旧版一致）。
    """
    if not isinstance(round_entry, dict):
        return False
    scores = round_entry.get("scores")
    if isinstance(scores, dict):
        for value in scores.values():
            if (isinstance(value, (int, float)) and not isinstance(value, bool)
                    and value < DIMENSION_FLOOR):
                return False
    if _promise_failures_of(round_entry.get("promise_audit")):
        return False
    return True


def phase_after_score(project: Path, chapter: int, score: float, round_num: int,
                      review_id: str | None = None, overwrite: bool = False,
                      scores: dict | None = None, body_path: Path | None = None,
                      dry_run_root: Path | None = None,
                      promise_audit: list | None = None) -> int:
    """after-score phase: 记录分数，判定是否需要修订/停止。
    P0-4：score/round 做输入合法性校验，防止脏评分进入闭环。
    """
    # 输入校验
    if not (0 <= score <= 100):
        print(f"ERROR: score {score} 超出 [0,100]", file=sys.stderr)
        return 2
    if not (1 <= round_num <= MAX_ROUNDS):
        print(f"ERROR: round {round_num} 超出 [1,{MAX_ROUNDS}]", file=sys.stderr)
        return 2

    rounds = load_scores(project, chapter, dry_run_root)
    # R5（外脑 2026-10-07 裁定）：输入正文始终取当前执行上下文指定的 body（dry_run=预览正文），
    # 不得退回 canonical 正式正文 —— 否则 dry_run 快照会写到正式 _review_snapshots/。
    prose = resolve_prose(project, chapter, body_path=body_path)

    # 备份当前正文为 best-snapshot（dry_run 时显式落 artifact root，绝不写正式目录）
    if prose:
        _snap_root = (dry_run_root / "review_snapshots") if dry_run_root is not None else None
        backup_prose(prose, chapter, round_num, review_id, overwrite, snapshot_root=_snap_root)

    # 构造本轮评分。
    # scores：逐维度分 dict（如 {"I-A": 100, "I-I": 88}），由宿主经 --scores 传入。
    # 有 scores 时据 s < PASS_SCORE 推导 weak_dimensions，修复「逐维分从未持久化、
    # weak_dimensions 恒空 → 定向修订 prompt 拿不到弱项」的黑盒问题（finding#3）。
    # 不传 scores 时退化为旧行为（scores=None + weak_dimensions=[]），向后兼容。
    # review_id 一并落盘：评分文件是「按章累积」的，跨任务会混存
    # （实测 round1=新任务 0.0/90.0 与 round2=旧任务 92.0 并存），
    # 不带 review_id 就无法在 finalize 时区分「本次任务的分」与「历史任务的分」，
    # 会让 best_round 指到历史任务那一轮 → 把更旧的快照落回正文。

    # P0-4 强化（2026-09-13 第30章 85 脏分事故复盘）：total ↔ scores 一致性校验。
    # 事故形态：评分 LLM 输出 total=85 但 10 个维度分全部 0~10（均值 4），量纲错乱，
    # 旧代码只校验 total ∈ [0,100] 便原样落盘 → best_score=85 误导为「黄色档可定稿」。
    # 规则：scores 非空时，total 必须落在 [维度均值*0.8, 维度均值*1.2] ∪ [维度min, 维度max]
    # 的合理包络内；越界 → 拒收本轮（return 2），提示宿主重评，禁止落脏数。
    # 仅「带 --scores 的评审」启用；纯 total 评审（无维度分）保持旧行为。
    scores_dict = dict(scores) if isinstance(scores, dict) else None
    if scores_dict:
        valid = [s for s in scores_dict.values()
                 if isinstance(s, (int, float)) and not isinstance(s, bool)]
        if valid:
            lo, hi = min(valid), max(valid)
            mean = sum(valid) / len(valid)
            lo_env, hi_env = lo, max(hi, mean * 1.2)
            if not (lo_env <= score <= hi_env):
                print(f"ERROR: total={score} 与维度分不一致（维度区间 {lo}~{hi}，"
                      f"均值 {mean:.1f}，合理包络 {lo_env:.1f}~{hi_env:.1f}）。"
                      f"疑似评分 LLM 量纲错乱，拒收本轮，请宿主重评。", file=sys.stderr)
                return 2

    weak = (
        [code for code, s in scores_dict.items()
         if isinstance(s, (int, float)) and not isinstance(s, bool) and s < PASS_SCORE]
        if scores_dict else []
    )
    # S4: 维度地板——<70 的维度也加入 weak，确保修订 prompt 包含触底项（模块级 DIMENSION_FLOOR）
    if scores_dict:
        for code, s in scores_dict.items():
            if isinstance(s, (int, float)) and not isinstance(s, bool) and s < DIMENSION_FLOOR and code not in weak:
                weak.append(code)
    round_entry = {
        "round": round_num,
        "total": score,
        "scores": scores_dict,
        "weak_dimensions": weak,
        # 闸2·细纲层（2026-10-25 三层证据验收裁决）：逐条承诺核对随轮次落盘，
        # 全兑现也留证据链；finalize 据 best 轮此字段判定细纲是否兑现。
        "promise_audit": promise_audit if isinstance(promise_audit, list) else None,
        "comment": "",
        "scored_at": int(time.time()),
        "review_id": review_id,
    }
    # 同一 review 的同号轮次才算「已有」——否则会覆盖别的 review 的记录
    existing = next(
        (r for r in rounds if r.get("round") == round_num and r.get("review_id") == review_id),
        None,
    )
    if existing:
        existing.update(round_entry)
    else:
        rounds.append(round_entry)
    save_scores(project, chapter, rounds, dry_run_root)

    # 判定
    # S4: 维度地板——任一维度 <70 时即使总分通过也需修订（rubric_90score_v3.md，模块级 DIMENSION_FLOOR）
    floor_violations = []
    if scores:
        for dim, dim_score in scores.items():
            if isinstance(dim_score, (int, float)) and dim_score < DIMENSION_FLOOR:
                floor_violations.append(f"{dim}={dim_score}")
    # 闸2·细纲层：逐条承诺核对（2026-10-25 三层证据验收裁决）——细纲义务
    # 不与印象分加权混算，delivered=false 即证据不通过，即使总分过线也拦。
    promise_failures = _promise_failures_of(promise_audit)
    if score >= PASS_SCORE and not floor_violations and not promise_failures:
        print_finalize(project, chapter, rounds, passed=True, stopped_reason=f"综合分 {score} >= {PASS_SCORE}，通过")
        return 0
    if score >= PASS_SCORE and (floor_violations or promise_failures):
        # 总分通过但有维度触底/细纲未兑现——仍需修订，但不扣分，只标记弱项
        pass  # fall through to 继续修订

    if round_num >= MAX_ROUNDS:
        print_finalize(project, chapter, rounds, passed=False, stopped_reason=f"已达最大轮次 {MAX_ROUNDS}，未通过")
        return 0

    # 检查净提升 —— 只与同一 review run 的上一轮（round_num-1）比较。
    # 评分文件按章累积、跨 review 混存，rounds[-2] 会取到历史 review 的轮次：
    # 重写链新 review 第 1 轮被旧 review 终分当基准（2026-10-25 实测 78 vs 旧 85
    # → -7 < 3 即停，一轮修订都没跑）。null id 存量兼容：同轮多条时取 scored_at 最新者。
    prev_entry = max(
        (r for r in rounds
         if r.get("round") == round_num - 1 and r.get("review_id") == review_id),
        key=lambda r: r.get("scored_at", 0) or 0,
        default=None,
    )
    if prev_entry is not None:
        improvement = score - prev_entry.get("total", 0)
        if improvement < MIN_IMPROVEMENT:
            print_finalize(project, chapter, rounds, passed=False,
                           stopped_reason=f"净提升 {improvement:.1f} < {MIN_IMPROVEMENT}，停止（保留最高分版本）")
            return 0

    # 需要修订（weak = 本轮弱维度，构造 round_entry 时已算好；
    # 不再从 rounds[-1] 回读——混存文件里下标会指到别的轮次）
    print_revise_prompt(project, chapter, prose or Path("."), round_num, weak,
                        failed_promises=promise_failures)
    return 0


def _write_review_meta(project: Path, chapter: int, *,
                       review_score: float, review_rounds: int, review_id: str | None,
                       finalized: bool = True,
                       reviewed_hash: str | None = None,
                       final_hash: str | None = None) -> None:
    """把 finalize 结果写入 delta.quality_meta（对齐 nww_post_write 的事务定位逻辑）。

    定位 delta 草稿：优先 追踪/delta_第NNN章.json，回退 .tmp 事务文件。
    写入字段：reviewed/review_score/review_rounds/review_id/finalized/finalized_at。
    只在 hash 校验通过后调用（finalize 才会真正落盘 + 标 finalized）。

    P0-3a 三哈希绑定（2026-10-19 外脑裁决）：
      reviewed_hash = 审稿通过时被审正文（best snapshot）的 SHA-256（原始文件字节，无规范化）
      final_hash    = finalize 冻结候选正文（copy 后重读校验通过的正文）的 SHA-256
    commit 侧（nww_track_commit.verify_post_write_meta）对实际待提交正文现算 commit_hash，
    三者一致才证明「审的=冻结的=交的」。新增字段必须同步
    nww_track_commit.normalize_quality_meta 的 require_known_keys 白名单，否则 commit 报
    unsupported fields BLOCK。
    """
    def _meta_path() -> Path | None:
        base = project / "追踪"
        cand = base / f"delta_第{chapter:03d}章.json"
        if cand.is_file():
            return cand
        for p in base.glob(f"*事务*{chapter}*.json"):
            return p
        for p in base.glob(f".tmp_事务{chapter}*.json"):
            return p
        return None

    fpath = _meta_path()
    if fpath is None:
        print("WARN: 未找到 delta 草稿，无法写 quality_meta", file=sys.stderr)
        return
    try:
        data = json.loads(fpath.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"WARN: 读取 delta 失败，无法写 quality_meta: {exc}", file=sys.stderr)
        return
    tx = data.get("transaction", data) if isinstance(data, dict) else data
    delta = tx.setdefault("delta", {})
    if not isinstance(delta, dict):
        delta = {}
        tx["delta"] = delta
    qm = delta.setdefault("quality_meta", {})
    if not isinstance(qm, dict):
        qm = {}
        delta["quality_meta"] = qm
    # D1.1: target_body_md5 = finalize 时正文 md5（review+revise pipeline 的最终版本）
    prose = resolve_prose(project, chapter)
    target_md5 = None
    if prose and prose.is_file():
        import hashlib
        target_md5 = hashlib.md5(prose.read_bytes()).hexdigest()
    qm.update({
        "reviewed": True,
        "review_score": int(review_score),
        "review_rounds": review_rounds,
        "review_id": review_id,
        "finalized": finalized,
        "finalized_at": int(time.time()),
        "target_body_md5": target_md5,
    })
    # P0-3a：三哈希绑定字段（仅在有值时写入；缺省缺失 = 存量路径，commit 侧不启用哈希闸）
    if reviewed_hash:
        qm["reviewed_hash"] = reviewed_hash
    if final_hash:
        qm["final_hash"] = final_hash
    fpath.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _snapshot_dir(project: Path, chapter: int, dry_run_root: Path | None = None) -> Path:
    """快照目录基准：**必须与 backup_prose 同源**（都取「正文实际所在目录」）。

    dry_run_root 显式传入时（dry_run）→ 落本次 execution 的 artifact root
    （<dry_run_root>/review_snapshots），与 backup_prose 的 snapshot_root 同源，
    绝不写正式 _review_snapshots/。

    backup_prose 用的是 ``prose.parent / "_review_snapshots"``；这里若硬编码
    ``project / "正文" / "_review_snapshots"``，在分卷结构（正文/第一卷/第N章.md）
    下就会漏掉卷层目录 → snapshot 永远定位不到 → phase_finalize 恒 return 2，
    P0-1 的「唯一落盘收口」静默失效。2026-09-13 实测于《用泡面征服修仙界》
    （正文/第一卷/）—— 平铺书恰好掩盖了这个差异，所以沙盒测试没暴露。
    """
    if dry_run_root is not None:
        return dry_run_root / "review_snapshots"
    prose = resolve_prose(project, chapter)
    base = prose.parent if prose else project / "正文"
    return base / "_review_snapshots"


def _best_snapshot_path(project: Path, chapter: int, best_round: int,
                        review_id: str | None = None,
                        dry_run_root: Path | None = None) -> Path | None:
    """定位 best round 快照。

    review_id 提供时**只认该 review 自己的隔离目录**：本次 review 没备份过这一轮
    → 直接返回 None（fail-closed），**绝不回退平铺快照**。平铺目录里放的是别的
    review 的产物，回退 = 把更旧的版本覆盖回正文。

    2026-09-13 17:36 真实踩过这条路：`追踪/评分/第027章.json` 是 legacy 格式
    （rounds 不带 review_id，round1=90@17:36 / round2=92@07:57 跨任务混存），
    finalize 取 rounds_all 的最高分 → best_round=2 → 平铺 `第027章_round2.md`
    （07:57 旧稿）→ **把 17:35 的新稿覆盖成 07:57 旧稿**（实测正文 3062 字
    == round2 内容，bytes 差 134 = 行数差 134，纯换行增量）。
    只有 review_id 缺省（存量只读兼容路径）才允许走平铺。
    """
    d = _snapshot_dir(project, chapter, dry_run_root)
    if review_id:
        cand = d / review_id / f"第{chapter:03d}章_round{best_round}.md"
        return cand if cand.is_file() else None
    cand = d / f"第{chapter:03d}章_round{best_round}.md"
    return cand if cand.is_file() else None


# ---------- 2026-11-05 P0/P1：best→final 恢复的字数观测与守卫（只增不改） ----------
_WC_TRACE_SWITCH = "NWW_WC_TRACE"
_WC_SNAPSHOT_GUARD_SWITCH = "NWW_WC_SNAPSHOT_GUARD"


def _wc_snapshot_observe(project: Path, chapter: int, prose: Path, snap: Path) -> dict[str, Any]:
    """恢复前对「当前正文」与「候选 best 快照」做字数带判定。

    P0：无论是否拦截都返回两侧可见字数/status，供落盘完整性日志与 stage detail 消费。
    P1：满足任一条件 → blocked=True（调用方跳过恢复、保留当前正文）：
      ① 当前正文带内、快照带外（审定计划原判据：带内→带外）；
      ② 快照比当前正文更短且快照欠字。执行期代码勘察发现：区间目标（如 2500-2750）
         无 ±10% 容差，本次事故两侧 status 都是 under，判据①不会触发，但高分旧快照
         仍会吃掉字数收敛补上去的字，故补此判据覆盖同型事故。
    任何异常 / 目标不可解析 / 文件缺失 → blocked=True，保留当前正文
    （fail-safe：恢复快照是可能吃掉字数的有损操作，不确定时不得默认恢复）。
    env NWW_WC_SNAPSHOT_GUARD=0 关闭守卫；NWW_WC_TRACE=0 关闭观测日志。
    """
    import os

    rec: dict[str, Any] = {
        "kind": "wc_snapshot_restore", "chapter": chapter,
        "guard_enabled": os.getenv(_WC_SNAPSHOT_GUARD_SWITCH, "1") != "0",
        "trace_enabled": os.getenv(_WC_TRACE_SWITCH, "1") != "0",
        "blocked": False, "restored": True,
    }
    try:
        from nww_wordcount import evaluate_wordcount, find_chapter_file, parse_target_spec, target_from_outline
        outline = find_chapter_file(project / "大纲", chapter, outline=True)
        if outline is None:
            # fail-safe（2026-11-06）：没有细纲就无法判定字数带；恢复 best 快照是可能
            # 吃掉字数的有损操作，不确定时必须保留当前正文，不能默认「照常恢复」。
            rec.update(blocked=True, restored=False,
                       reason="细纲缺失，无法判定字数带，保留当前正文（fail-safe）")
            return rec
        outline_text = outline.read_text(encoding="utf-8")
        try:
            spec = target_from_outline(outline_text)
        except Exception as _te:  # noqa: BLE001 — 存量细纲常缺「字数口径」行/多章合一
            # 兼容回退：与 pipeline._extract_wc_target 同款的宽松提取。原实现直接抛到
            # 外层 except → blocked=False → 照常恢复，导致字数收敛补上的字被短快照吃掉
            # （2026-11-06 第 1 章实测：细纲缺「字数口径：visible_chars_v1」行）。
            import re as _re

            _m = _re.search(r"字数目标\s*[：:]?\s*(\d+(?:\s*[-~～至到]\s*\d+)?)", outline_text)
            if not _m:
                rec.update(blocked=True, restored=False,
                           reason=f"细纲字数目标不可解析（{_te}），保留当前正文（fail-safe）")
                return rec
            spec = parse_target_spec(_re.sub(r"\s+", "", _m.group(1)))
        cur = evaluate_wordcount(prose.read_text(encoding="utf-8"), spec)
        snp = evaluate_wordcount(snap.read_text(encoding="utf-8"), spec)
        rec.update({
            "target": spec.get("raw"), "cur_actual": cur.get("actual"),
            "cur_status": cur.get("status"), "snap_actual": snp.get("actual"),
            "snap_status": snp.get("status"),
        })
        if not rec["guard_enabled"]:
            rec["reason"] = "env 关闭守卫（照常恢复）"
            return rec
        _in_band = {"internal_pass", "borderline"}
        _rule_band = cur.get("status") in _in_band and snp.get("status") in ("under", "over")
        _rule_shorter_under = (
            isinstance(snp.get("actual"), int) and isinstance(cur.get("actual"), int)
            and snp["actual"] < cur["actual"] and snp.get("status") == "under"
        )
        if _rule_band or _rule_shorter_under:
            rec["blocked"] = True
            rec["restored"] = False
            rec["reason"] = "带内→带外" if _rule_band else "更短且欠字"
    except Exception as exc:  # noqa: BLE001 — 守卫异常必须 fail-safe，不得默认恢复
        rec.update(blocked=True, restored=False,
                   reason=f"字数守卫异常，保留当前正文（fail-safe）: {exc}")
    return rec


def _wc_log_snapshot_observe(project: Path, rec: dict[str, Any]) -> None:
    """P0：写入 追踪/落盘完整性.jsonl + 打 [WC_TRACE] 行（web 侧解析进 stage detail）。"""
    if not rec.get("trace_enabled"):
        return
    rec = {**rec, "ts": int(time.time())}
    try:
        d = project / "追踪"
        d.mkdir(parents=True, exist_ok=True)
        with (d / "落盘完整性.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — 观测写失败绝不挂主链
        pass
    print(f"[WC_TRACE] snapshot-restore {json.dumps(rec, ensure_ascii=False)}", flush=True)


def phase_finalize(project: Path, chapter: int,
                   review_id: str | None = None, overwrite: bool = False,
                   allow_below_threshold: bool = False, body_path: Path | None = None,
                   dry_run_root: Path | None = None) -> int:
    """finalize phase: 唯一 best→final 物理落盘点（P0-1）。

    1) 从评分历史取 best_round：默认在「维度地板通过且细纲承诺全兑现」的合格轮次里
       取 argmax(round.total)；无合格轮次（或 env NWW_FINALIZE_GATE_FIRST=0）时
       回退全量 argmax(round.total)（旧行为）。不依赖数组下标，round 语义保留
    2) 定位 best snapshot（review_id 隔离优先，回退旧平铺只读）
    3) copy 回正文 → 重读 hash 校验一致
    4) best >= PASS_SCORE 时写 quality_meta {reviewed, review_score, ...}
       best < PASS_SCORE 时仅恢复快照，不写 reviewed（兑现"保留最高分版本"承诺）

    dry_run_root 显式传入（预览）→ 本函数**只做判定与报告，绝不落盘**：不 copy2 覆盖正文、
    不写正式 delta/quality_meta（外脑 2026-10-07 裁定 R4）。评分与快照均从 artifact root 读，
    正式 追踪/、正文/ 零变化。
    """
    import hashlib

    rounds_all = load_scores(project, chapter, dry_run_root)
    if not rounds_all:
        print("ERROR: no review scores found, cannot finalize", file=sys.stderr)
        return 2
    # 未传 review_id 时**不允许**在「新格式评分」上跑：评分里已经带了 review_id，
    # 说明本次任务本该有身份，缺省会退化成「平铺查找 + 全量 round 取最高分」，
    # 正是 17:36 覆盖事故的通道。老书（评分通篇无 review_id）不受影响。
    if review_id is None and any(r.get("review_id") for r in rounds_all):
        print("ERROR: 评分文件已带 review_id（新格式），finalize 必须显式传 --review-id；"
              "缺省会退化为平铺查找，可能把更旧的快照落回正文", file=sys.stderr)
        return 2
    # 只取本次 review 的轮次：评分文件按章累积、跨任务混存，
    # 不过滤会让 best_round 指到历史任务的某一轮（→ 把更旧的快照落回正文）。
    rounds = [r for r in rounds_all if r.get("review_id") == review_id] if review_id else rounds_all
    if not rounds:
        rounds = rounds_all      # 兼容：无 review_id 标记的历史评分
    # best 轮选择（2026-11-07 修复）：旧版纯按 total 取最大，会选中「分更高但细纲
    # 承诺未兑现 / 维度触底」的轮次 → promise_bad/floor_bad 非空 → 不写 reviewed →
    # 下游 commit A-5 必失败，而更合规的低分轮永远提交不了。现改为：先过滤出
    # 「_round_gate_ok」合格轮次，再在合格池内取最高分；合格池为空 → 回退全量纯
    # 最高分（与旧版完全一致）。env NWW_FINALIZE_GATE_FIRST=0 回旧行为（见上）。
    # 注意：best 语义已是「被选中轮次的分数」，后续 below_threshold/floor_bad/
    # promise_bad 复核、报告与落盘 review_score 都以这一轮为准。
    if _gate_first_enabled():
        _pool = [r for r in rounds if _round_gate_ok(r)]
        if not _pool:
            _pool = rounds   # 无任何闸合格轮次 → 回退纯最高分（行为与旧版完全一致）
    else:
        _pool = rounds
    best_entry = max(_pool, key=lambda r: r.get("total", 0))
    best_round = best_entry.get("round")
    best = best_entry.get("total", 0)
    below_threshold = best < PASS_SCORE
    # 维度地板=硬约束（外脑 2026-10-19 边界项3 裁决）：best 达标但 best 轮任一维度 < DIMENSION_FLOOR
    # → 「分数通过但硬约束不通过」→ 不得作为已验收版本（不写 reviewed；commit 侧评分门拦截）。
    # best 轮无 scores 明细（存量旧格式）→ 无法判定维度地板，维持旧行为（仅分数判定，兼容）。
    _best_scores = best_entry.get("scores")
    floor_bad = sorted(
        f"{k}={v:g}" for k, v in _best_scores.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v < DIMENSION_FLOOR
    ) if isinstance(_best_scores, dict) else []
    # 闸2·细纲层：best 轮存在 delivered=false 的承诺 → 证据验收不通过（对齐 floor_bad：
    # 恢复快照但不标 reviewed；best 轮无 promise_audit（存量旧格式）→ 无法判定，兼容）。
    promise_bad = _promise_failures_of(best_entry.get("promise_audit"))
    if dry_run_root is not None:
        # ★dry_run（外脑 2026-10-07 裁定 R4）：只做判定与报告，绝不 finalize 落盘 ——
        #   不 copy2 覆盖正文、不写正式 delta/quality_meta。
        _dry_passed = not (below_threshold or floor_bad or promise_bad)
        _dry_why = ("通过" if _dry_passed else
                    "未通过（低于通过线）" if below_threshold
                    else f"未通过（维度地板硬约束: {','.join(floor_bad)}）" if floor_bad
                    else f"未通过（细纲承诺未兑现: {promise_bad[0].get('outline_item', '')} 等 {len(promise_bad)} 条）")
        print_finalize(project, chapter, rounds, passed=_dry_passed,
                       stopped_reason=f"dry-run：best snapshot (round={best_round}, {best}分) 判定为"
                       + _dry_why + "；预览模式不落盘、不改正文、不写 quality_meta")
        return 0
    if below_threshold and not allow_below_threshold:
        print(f"ERROR: 最高分 {best} < {PASS_SCORE}，不落盘 final（业务规则：仅通过质量门槛才收口）", file=sys.stderr)
        return 2
    prose = resolve_prose(project, chapter, body_path=body_path)
    snap = _best_snapshot_path(project, chapter, best_round, review_id, dry_run_root)
    if snap is None or prose is None:
        print(f"ERROR: best snapshot 或 prose 缺失 (round={best_round})", file=sys.stderr)
        return 2
    # P0/P1（2026-11-05，只增不改 + env 回滚）：恢复前观测两侧字数；字数守卫命中时
    # 不恢复 best 快照、保留当前正文。此时「被审版本≠交付版本」，故不写 reviewed/
    # finalized/hash 留痕，交下游 commit 闸终审（A-5 字段缺失按兼容模式 WARNING）。
    _wc_rec = _wc_snapshot_observe(project, chapter, prose, snap)
    _wc_rec.update({"best_round": best_round, "review_id": review_id or ""})
    _wc_guard_blocked = bool(_wc_rec.get("blocked"))
    _wc_log_snapshot_observe(project, _wc_rec)
    if _wc_guard_blocked:
        before = hashlib.sha256(prose.read_bytes()).hexdigest()  # 以保留的当前正文为基准
    else:
        before = hashlib.sha256(snap.read_bytes()).hexdigest()
        shutil.copy2(snap, prose)                   # 强制恢复 best → final（唯一落盘点）
    after = hashlib.sha256(prose.read_bytes()).hexdigest()
    if before != after:
        print("ERROR: final prose 与 best snapshot hash 不一致，未标记 finalized", file=sys.stderr)
        return 2
    if _wc_guard_blocked:
        print(f"[WC_GUARD] 字数带守卫命中（{_wc_rec.get('reason')}）：未恢复 best 快照，"
              f"保留当前正文 {_wc_rec.get('cur_actual')} 字（快照 {_wc_rec.get('snap_actual')} 字 / "
              f"{_wc_rec.get('snap_status')}），本轮不写 reviewed（交付版本≠被审版本，需重新审稿）",
              flush=True)
    if not below_threshold and not floor_bad and not promise_bad and not _wc_guard_blocked:
        # P0-3a：before=best snapshot（被审正文）哈希，after=copy 后冻结正文哈希；
        # :602-604 已校验两者一致，此处落库留痕供 commit 侧三哈希校验。
        # floor_bad（维度地板硬约束失败）时不写 reviewed —— 分数达标≠硬约束通过，
        # 外脑 2026-10-19 边界项3：失败版本不得标已验收（快照仍恢复=保留最高分版本）。
        # promise_bad（细纲未兑现，2026-10-25 闸2）同语义：证据验收不通过不标 reviewed。
        _write_review_meta(project, chapter, review_score=best, review_rounds=len(rounds),
                           review_id=review_id, finalized=True,
                           reviewed_hash=before, final_hash=after)
    print_finalize(project, chapter, rounds,
                   passed=not (below_threshold or floor_bad or promise_bad or _wc_guard_blocked),
                   stopped_reason=f"best snapshot (round={best_round}, {best}分) 已落盘"
                   + ("（低于通过线，仅恢复快照不标 reviewed）" if below_threshold else
                      f"（维度地板硬约束未达标: {','.join(floor_bad)}，仅恢复快照不标 reviewed）" if floor_bad
                      else f"（细纲承诺未兑现: {promise_bad[0].get('outline_item', '')} 等 {len(promise_bad)} 条，仅恢复快照不标 reviewed）" if promise_bad
                      else "并 hash 校验通过")
                   + ("（字数带守卫：未恢复快照、保留当前正文、本轮不标 reviewed）" if _wc_guard_blocked else ""))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="审稿闭环确定性流程控制")
    ap.add_argument("--project", type=Path, required=True)
    ap.add_argument("--chapter", type=int, required=True)
    ap.add_argument("--phase", choices=["score", "after-score", "revise", "finalize"], required=True)
    ap.add_argument("--round", type=int, default=1, help="当前轮次（默认1）")
    ap.add_argument("--score", type=float, default=None, help="after-score 阶段的综合分")
    ap.add_argument("--scores", default=None, help="after-score 阶段的逐维度分 JSON，如 '{\"I-A\":100,\"I-I\":88}'；传入后据分推导 weak_dimensions")
    ap.add_argument("--promise-audit", dest="promise_audit", default=None,
                    help="after-score 阶段的细纲承诺核对 JSON 数组，如 "
                         "'[{\"outline_item\":\"开篇钩子\",\"delivered\":true,\"evidence\":\"第1段\"}]'；"
                         "delivered=false 的条目=细纲未兑现（证据验收硬闸，与分数无关）")
    ap.add_argument("--review-id", default=None, help="本次 review run 身份（异步=task_id，同步=run 入口生成一次）")
    ap.add_argument("--body", type=Path, default=None, help="dry_run 预览正文路径")
    ap.add_argument("--dry-run-root", type=Path, default=None,
                    help="dry_run 预览：评分/快照/判定产物落本次 execution 的 artifact root，"
                         "正式 追踪/、正文/ 零变化；缺省 None 走原正式路径")
    ap.add_argument("--overwrite", action="store_true", help="显式允许覆盖已存在的 snapshot")
    ap.add_argument("--allow-below-threshold", action="store_true",
                    help="finalize 时允许 best < PASS_SCORE（仅恢复快照，不标 reviewed）")
    args = ap.parse_args()

    project = args.project.resolve()
    if not project.is_dir():
        print(f"ERROR: project not found: {project}", file=sys.stderr)
        return 2

    if args.phase == "score":
        return phase_score(project, args.chapter, args.round, body_path=args.body)
    elif args.phase == "after-score":
        if args.score is None:
            print("ERROR: --score required for after-score phase", file=sys.stderr)
            return 2
        scores_arg = json.loads(args.scores) if args.scores else None
        try:
            promise_arg = json.loads(args.promise_audit) if args.promise_audit else None
        except json.JSONDecodeError:
            print(f"WARN: --promise-audit 不是合法 JSON，视为缺席（不闸）: {args.promise_audit[:100]}",
                  file=sys.stderr)
            promise_arg = None
        return phase_after_score(project, args.chapter, args.score, args.round,
                                 args.review_id, args.overwrite, scores_arg,
                                 body_path=args.body, dry_run_root=args.dry_run_root,
                                 promise_audit=promise_arg)
    elif args.phase == "revise":
        prose = resolve_prose(project, args.chapter, body_path=args.body)
        # P0-2（外脑 2026-10-07 裁定）：读身份须与写身份同源 —— dry_run 传入 --dry-run-root 时，
        # 评分历史从同一 artifact root 读，不得回退正式 追踪/评分。
        rounds = load_scores(project, args.chapter, args.dry_run_root)
        # 弱维度只取本次 review 当前轮（round + review_id 定位）；混存文件里
        # rounds[-1] 会指到历史 review 的轮次，修订 prompt 拿错靶子。
        # 无匹配时回退 rounds[-1]（存量无 review_id 路径行为不变）。
        own = [
            r for r in rounds
            if r.get("round") == args.round
            and (not args.review_id or r.get("review_id") == args.review_id)
        ]
        own_entry = own[-1] if own else (rounds[-1] if rounds else None)
        weak = own_entry.get("weak_dimensions", []) if own_entry else []
        failed_promises = _promise_failures_of(own_entry.get("promise_audit")) if own_entry else []
        print_revise_prompt(project, args.chapter, prose or Path("."), args.round, weak,
                            failed_promises=failed_promises)
        return 0
    elif args.phase == "finalize":
        return phase_finalize(project, args.chapter, args.review_id, args.overwrite,
                              args.allow_below_threshold, body_path=args.body,
                              dry_run_root=args.dry_run_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
