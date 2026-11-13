---
name: nww-rhythm
version: 0.1.0
description: |
  NWW-V12 叙事节奏计划（Narrative Rhythm Layer / NRL）。两层：
  （1）Chapter Sequence Planner：跨章动力规划（卷级状态 sequence_rhythm.json，管理连续章节不重复）；
  （2）单章 Rhythm Planner：生成章节节奏中间表示 chapter_rhythm.json，把「发生什么」（细纲）转换为
  「读者如何经历」（节奏计划），作为 Writer 的优先级设计输入。触发：/nww-rhythm、/节奏计划，
  或由 /nww-long-write Phase 1 在 .nrl.enabled 存在时自动调用。
metadata: {"openclaw":{"source":"NRL V0 Schema（NRL_V0_schema.md，V0 冻结 2026-08-28）+ CSP 跨章层（2026-08-28）"}}
---

# nww-rhythm — 叙事节奏计划（NRL）

**定位**：节奏决策中间表示层，不是规则库。职责是把「发生什么」转换为「读者如何经历」。
不写句子写法、不设动作/对白/心理比例——那些属风格层/执行层，本层不收。

## 章序列规划（Chapter Sequence Planning，先跑）

> 升级：NRL 从「单章 Rhythm Planner」升级为「跨章 Chapter Sequence Planner」。
> 职责：管理连续章节之间的动力不重复（中长期叙事规划层），再进单章节奏规划。
> 架构修正：CSP 只管 **Layer 1 生成约束**，结果指标（天机占比等）留给 **Layer 2 审计**，禁止 Planner 为凑比例机械删改。

### 流程

```
ChapterDNA → Chapter Sequence Planner → Chapter Rhythm Planner → Writer
```

### 输入（卷级状态）
- 跑 `{PYTHON} {SKILL}/../scripts/nww_sequence_audit.py <正文目录>` 得前 N-1 章硬计数（天机/算盘/观星/章尾标记）
- 读 `{BOOK}/sequence_rhythm.json`（若已存在，卷级历史 pattern）
- 本章细纲 + 卷纲（本卷步进：觉醒→立威→惊动→入局→掀桌）

### Layer 1 · 生成约束（CSP 负责）

1. **章尾动力类型轮换**：给本章定章尾类型 A 代价结算 / B 认知变化 / C 外部压力 / D 关系变化；约束 `last_3` 禁 AA、AAA（允许 ABA、ACB、DAC）。**天机反噬是「代价结算机制」，不是章尾类型本身**，不单独计数约束。
2. **主动力轮换**：给本章标 `chapter_drive`（investigation/verification/confrontation/entry/archive/prediction/promotion）+ `drive_transition`；约束 `current_drive != previous_drive`（必要时 `!= previous_2_drive`）。
3. **重复动力锚点限制（Recurring Motivation Anchor Constraint，名称冻结）**：约束「同一心理启动动作不能连续成为剧情发动机」。字段 `motivation_anchor`（如 abacus_calculation / interpersonal_pressure / archive_trigger）+ `anchor_role`（trigger/response）。审计统计**连续章节同 anchor_role**，不是简单 count。
   **锚下限（Pilot 实证 2026-08-28）**：约束只禁「trigger 角色连续复用」，不禁「锚存在」。主角签名内心锚（如算盘）允许每章 1-2 次作「锚」存在（细纲常要求章首/章尾各落一次），audit 目标 ≤3 次/章仍适用；砍到 0 会丢主角签名，属过砍。
4. **主角主动性约束**：检查过去 N 章主动决策比例，`< 40%` 时本章置 `protagonist_choice_required=true`（优先生成主角主动选择/设局/反将）。
5. **反噬冷却（Antidote Cooldown，2026-08-28 ch6-10 盲评实证）**：重大信息获得后**不立即反噬**——先安排 ≥1 次「主动收益」（得到优势/建立关系/解决局部/留下新主动权），再进入代价。**十章周期「大反噬」≤3 次**。防主角长期被更高层压制、削弱爽感。
6. **Footer 冷却（same_terminal_motif_cooldown = 2，2026-08-28 全卷裁定实证）**：同一收束动作（灯下烧纸 / 灰压灯台 / 独坐到天明 / 睁眼不睡 / 周敬亭视角收尾）**禁连续 2 章**；同款 terminal motif 至少隔 2 章再用。
7. **主动收益检查（protagonist_gain_before_backlash，2026-08-28 生产护栏）**：`chapter_end` 若含 backlash（代价/更大棋局），必须满足 `protagonist_gain_before_backlash=true`——反噬前有主角主动收益 beat；否则回退到旧模板（发现→被动→代价）。
   **surface motif 频率**：「灯下烧信/纸条」「心里过账/又过了一遍」「赏即锁/门即笼/线头」等主题词，禁连续 2 章同款落章尾；同款 motif 至少隔 2 章再用。
8. **Decision Spine（decision_spine_execution，2026-08-28 Test-01 跨题材实证 + 剂量验证 + ch11-20 生产观察修正）**：每章完成一次有效决策「局势判断 Situation Assessment → 主动选择 Protagonist Choice → 代价/后果 Cost/Consequence」。目标：**主角推动剧情，而不是剧情推动主角**。
   - **剂量（dosage 验证 2026-08-28，yindech ch5 D0-D3 梯度盲排）**：**D2=一次完整决策链最优**（主动9.2/节奏8.8/信息9.0/独白8.8 最均衡）。D3 多处决策链主动 9.4 但节奏 7.2/信息 7.8/独白 6.5——解释过量是负收益（「把角色决策写成作者解释角色为什么聪明」）。
   - **三层修正（ch11-20 生产观察期实证 2026-08-28）**：原版「每章一个 D2」会退化为「章节内固定动作模板」（认知→备选→排除→选择→代价 固定段落形状 + 8/10 章值房单场景 + 专业动作密度下降）。**CSP-08 原版不建议冻结，但不需要推翻 CSP**——升级为三层约束：
     - **R1 认知动作约束（D2 是叙事功能，不是段落模板）**：每章**最多一次显式 D2**；D2 的表现形式**必须由场景决定**，允许：对话中直接拍板 / 行动中被迫选择 / 证据逼迫选择 / 利益交换形成选择 / 犹豫后选择 / 错误选择后付代价 / 他人替他做选择 / 无内心解释直接行动。**禁止**固定「认知→备选→排除→选择→代价」段落形状；禁连续 2 章同形态。
     - **R2 剧情载体轮换（连续 3 章不得用同一种推进载体）**：四类载体——①证据推进（账/档/数字/文书/物证）②关系推进（郑直/周敬亭/三皇子等）③行动推进（查/跟/拿/送/设局/试探）④认知推进（发现新规则/新敌人/新解释）。**空间须承担叙事功能**：户部=证据推进、值房=关系/身份压力、三皇子府=政治交易、钦天监内部=未知与危险；禁止连续 3 章「安全空间→获信息→内心判断→选择→夜里总结」的同构。
     - **R3 题材动作保真（每 2-3 章至少一次不可被普通权谋替代的专业动作）**：本书=对账/核数/底账外档比对/漕运损耗计算/天象与粮数对应/呈报逻辑核验/笔迹誊本比对/算盘计算/灾异数据与现实结果互证。防「专业行动密度下降、退化成纯权谋内心戏」。
   - **约束分级（2026-08-28 ch11-20 修正，D2 降级为监测软指标）**：ch11-20 证明 Writer 一旦把「D2 ≤1/章、100-200字」理解成「每章必须写一个 D2 段落」，就会自动制造「掂一遍→两个选项→排除→必须…」固定模板。故分级：
     - **硬约束（生成器必守）**：每章**最多 1 个显式 D2**；本章**必须存在一次有效状态选择**（局势变化→人物处理→形成/被迫选择→即时后果）。
     - **软指标（监测用，非生成硬目标）**：D2 展开量、认知动作类型、载体分布——**禁止把软指标当硬目标**（否则退回模板化）。
     - **硬防线**：R2 连续载体不得同构（连续 3 章不得同载体）；R3 专业动作不得长期缺失（每 2-3 章至少一次）。
   - **结构化规格（decision_spine_execution）**：
     ```yaml
     CSP-08:
       name: decision_spine_execution   # 核心=主角必须执行决策链（非展示层）
       purpose: 增强主角主动性，不增加解释负担
       hard_constraints:               # 硬约束（生成器必守）
         max_explicit_D2_per_chapter: 1
         valid_state_choice_required: true   # 本章必须有一次有效状态选择
       soft_metrics:                   # 软指标（监测用，禁当硬目标）
         - d2_expansion_amount
         - cognitive_action_type
         - carrier_distribution
       hard_defenses:                  # 硬防线
         r2_carrier_no_same_3_consecutive: true
         r3_genre_action_no_long_gap: true
       r1_cognitive_action:
         # D2 是叙事功能不是段落模板；表现形式由场景决定
         forms_allowed:
           - 对话中直接拍板
           - 行动中被迫选择
           - 证据逼迫选择
           - 利益交换形成选择
           - 犹豫后选择
           - 错误选择后付代价
           - 他人替他做选择
           - 无内心解释直接行动
         forbid:
           - fixed_paragraph_shape   # 禁固定「认知→备选→排除→选择→代价」段落形状
           - same_form_2_consecutive_chapters
       r2_plot_carrier_rotation:
         # 连续 3 章不得用同一种推进载体；空间须承担叙事功能
         carriers: [evidence, relationship, action, cognition]
         constraint: no_same_carrier_3_consecutive_chapters
       r3_genre_action_fidelity:
         # 每 2-3 章至少一次不可被普通权谋替代的专业动作
         examples:
           - 对账/核数
           - 底账/外档比对
           - 漕运损耗计算
           - 天象与粮数对应
           - 呈报逻辑核验
           - 笔迹/誊本比对
           - 算盘计算
           - 灾异数据与现实结果互证
         constraint: genre_action_at_least_once_per_2-3_chapters
     ```
   - 职责边界：CSP-08 管「增强主动性」，I-G 门禁管「防解释膨胀」——两职责无冲突，**无需新增 decision_spine_overuse_penalty**（max_expansion=1 已约束 + I-G 已兜底）。
   - 状态：`production_candidate`（ch11-20 观察期修正后；非绝对规则，随生产动态校准）。

### 输出（卷级状态）
`{BOOK}/sequence_rhythm.json`（职责与单章 chapter_rhythm.json 分离）：

```json
{
  "book_id": "", "volume_id": "", "chapter_range": "",
  "history_pattern": {
    "recent_drives": [],
    "ending_types": [],
    "motivation_anchors": []
  },
  "current_constraints": {
    "required_drive_change": true,
    "ending_type_rotation": true,
    "avoid_anchor_repeat": true,
    "require_protagonist_choice": false
  }
}
```

### Layer 2 · 审计指标（验证用，非生成约束）
`nww_sequence_audit.py` 输出比对目标（长链验证时用）：
天机章尾占比 / 同构动力连续 / 重复动力锚点均值 / 主动章节占比 / 章尾类型分布。
**Planner 不背这些指标，只背 Layer 1 约束。**

## Schema

V0 冻结 Schema 见项目根 `{BOOK}/NRL_V0_schema.md`（本文件只重复最小契约，schema 修订以该文件为准）。

```json
{
  "chapter": "第N章 章名",
  "curve": "全章张力走向一句话",
  "climax": "峰值落点 {beat序号, 峰值类型}",
  "aftertaste": "章末余韵/收束要求",
  "beats": [
    {
      "event": "该 beat 事件一句话",
      "function": "叙事功能（继承细纲功能标签）",
      "tension": "节奏状态（快/紧/松/爆/余韵）",
      "reveal": "信息释放：揭示什么 + 承载物 + 读者/角色/主角各自知道什么",
      "breath": "呼吸标记：是否安排恢复窗口 + 一句安排"
    }
  ]
}
```

## 输入

- 本章细纲 `{BOOK}/大纲/细纲_第{N}章.md`（情节点序列表，或仅逻辑线的极简形态 → 先重建 beat）
- 可选：`{BOOK}/bible.json` 的 `character_growth` 主动度阶段（A/B/C/D），定本章行动节奏基准
- 可选：前情摘要（节奏须承接上一章末尾钩子）
- 卷级：`{BOOK}/sequence_rhythm.json`（CSP 产物，本章的跨章约束）

## 输出

1. `{BOOK}/chapter_rhythm.json`（V0 Schema，平铺于项目根）
2. 渲染文本【本章叙事节奏计划】（由 long-write Phase 1 并入写作上下文，进【硬性事实锚】）

## 生成步骤

0. **CSP 跨章约束（先跑）**：读 `sequence_rhythm.json`（无则先建）+ 跑 `nww_sequence_audit.py` → 生成本章 `current_constraints`（required_drive_change / ending_type_rotation / avoid_anchor_repeat / require_protagonist_choice），作为本章节奏计划的**顶层约束**。
1. **切 beat**：把细纲情节点表/逻辑线切成 beats，每 beat = 一个完整叙事单元（场景/冲突/反应/揭示/落子）。
2. **标 tension**：逐 beat 判「读者此刻的呼吸节奏」——快=事件突入、紧=冲突蓄力、松=释放兑现、爆=状态改变、余韵=沉淀。用读者体验判断，不用字数。
3. **写 reveal**：逐 beat 标注三层知情差（读者/角色/主角各自知道什么）+ 揭示顺序 + 承载物（通过什么动作/物件/对白释放）。先现象后原因、先遮挡后揭示，禁止「知道答案→直接解释」。
4. **插 breath**：连续高压 beat（≥3 个紧/爆）后必须安排 ≥1 处恢复窗口（关系变化/环境定格/动作收束/旁观反应）。
5. **定章级**：curve（全章张力序列）、climax（真正改变状态的峰值 beat，不是中间过程）、aftertaste（收在哪：动作/画面/钩子）。
6. **落盘**：写 `chapter_rhythm.json` + 渲染【本章叙事节奏计划】文本。

## 生成后自检

- [ ] 每个 beat 有 tension，且能说清「为什么这样排」（不是贴标签）
- [ ] 每个 beat 的 reveal 回答了「读者此刻知道什么」「何时该藏」
- [ ] 连续高压段后确有 breath 恢复窗口
- [ ] climax 落在真正改变状态的 beat
- [ ] 能否仅凭本计划看出「这章为何会拖」「信息何时该藏/该放」「峰值该在哪」

> 若只能产出「快/慢/张/弛」标签、却说不出上述判断依据 → 视为未达标，重写再交付。
> 交付摘要 ≤200 字：落盘路径 + beats 数 + climax/curve 一句话。

## 边界

- 只生成节奏计划，**不写正文、不改细纲、不改 bible/state/追踪**。
- 与 `/nww-long-write`：Phase 1 在 `.nrl.enabled` 存在时调用本 skill；默认关闭不注入，不影响原有 Phase 1 行为。
- 回滚：删除 `{BOOK}/.nrl.enabled` 与 `{BOOK}/chapter_rhythm.json` 即完全恢复原路径。
