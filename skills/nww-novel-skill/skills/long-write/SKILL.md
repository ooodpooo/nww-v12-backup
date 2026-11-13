---
name: nww-long-write
version: 1.0.0
description: |
  NWW-V12 写章技能（生成方法论，纯 skill 重表达自 scene_writer.py）。构造硬事实锚 → LLM 按细纲写章 →
  nww_enforce 机械层 → 去AI味 → 一致性检查 → 追踪事务提交 → 摘要+状态 deep-merge。
  触发：/nww-long-write、/写章、「写第N章」。
metadata: {"openclaw":{"source":"NWW-V12 scene_writer.py (重表达) + nww-novel-skill nww-long-write"}}
---

# nww-long-write — 写一章

把 V12 `scene_writer.py` 的「一次成章 + 归档」方法论重表达为 skill 驱动流程。LLM 是写手，确定性脚本是护栏，**绝不信任模型自带的连续性**。

> 所有确定性脚本在 `{SKILL}/../scripts/`（即 `skills/nww-novel-skill/scripts/`）。`{BOOK}` = 书项目根（含 `设定/大纲/正文/追踪`）。

## 核心哲学（继承 V12 A–F 修复）

1. **硬事实锚优先**：正文一切数字/人名/境界/代价刻度，必须先于写作锁死在 prompt 里，禁止模型自创。
2. **必种件不静默放弃**：动机锚点 / 代价刻度 / 专属比喻 / 反派败相缺则机械兜底注入（不靠 LLM 重生成兜底）。
3. **主角境界守卫只补不压**：state 缺主角境界先从 bible.json 主角 identity 兜底取，仍缺用正文主角语境峰值补入；已有则只做矛盾检查，绝不擅改已有状态。
4. **crack meter 只进不退**：正文计数低于历史值抬回。
5. **状态 deep-merge**：以旧状态为基，只覆盖模型返回字段，缺失字段保留旧值（不再整段丢弃）。
6. **确定性层 fail-closed**：机械修复失败宁可丢泄漏末段，也不依赖 LLM。

---

## V12.5 推荐流程 · 单入口封装（post_write，规避选择性执行）

> **2026-09-06 新增**。把 Phase 3–5 的确定性步骤封装为单入口 `nww_post_write.py`，宿主 LLM 只需要做两件事：①生成正文 ②调用 post_write。减少 LLM 决策点，规避"选择性执行"风险。

**推荐流程**：

```
Phase 0–2（不变）：准备 → 硬事实锚 → 生成正文 + delta 事务草稿
    ↓
调用 nww_post_write.py --project {BOOK} --chapter {N}
    ↓
脚本内部确定性执行（不可跳过）：
  Step 1  nww_enforce.py          机械层修复
  Step 2  nww_track_commit.py commit  A-1/A-2/A-3/A-4/A-5 闸门
  Step 3  审稿闭环指令输出           宿主 LLM 按 nww_review_cycle.py 指导执行
  Step 4  nww_cross_chapter_check.py  跨章重复/段落漂移/标题疲劳
```

**调用方式**：

```bash
python3 {SKILL}/../scripts/nww_post_write.py \
  --project {BOOK} \
  --chapter {N} \
  --prose {正文文件路径} \
  --transaction {delta事务JSON路径}
```

- `--prose` / `--transaction` 可省略，脚本自动定位（正文在 `正文/`，事务在 `追踪/delta_第N章.json`）
- commit 失败立即 BLOCK，不继续后续步骤
- enforce / cross_chapter 失败仅 WARNING，不阻断

**审稿闭环（Step 3）由宿主 LLM 执行**，`nww_review_cycle.py` 提供确定性流程控制：

```bash
# 第1轮打分
python3 nww_review_cycle.py --project {BOOK} --chapter {N} --phase score --round 1
# 宿主 LLM 按输出的 prompt 打分后（scores 为打分 JSON 中的 scores 字段）：
python3 nww_review_cycle.py --project {BOOK} --chapter {N} --phase after-score --score <综合分> --round 1 --scores '<scores_json>'
# 分数 < 85 → 输出修订指令，宿主 LLM 修订后重新打分（第2轮）
# 最终：
python3 nww_review_cycle.py --project {BOOK} --chapter {N} --phase finalize
```

- 最多 2 轮，净提升 < 3 分即停，保留最高分版本（best-snapshot）
- 评分落盘到 `追踪/评分/第N章.json`，供 `check` 输出质量趋势

**A-5 流程完整性闸**：commit 时校验 `delta.quality_meta.enforced` / `reviewed`，缺失则 WARNING（兼容模式）或 BLOCK（`bible.commit_gates.meta_check=true`）。

**向后兼容**：不调用 post_write 也能走原来的 Phase 3–6 分步流程（A-5 为兼容模式 WARNING，不阻断）。存量书不受影响。

---

## Phase 0 · 准备

- **packet v2 契约预检（写前硬闸，2026-10-27 Task 7 / C-P1-2 根修）**：进入任何细纲加工/Phase 1 拼装**之前**，必须先跑 `python3 skills/nww-novel-skill/scripts/nww_skill_preflight.py --book <书根> --chapter <N>`。该命令内部强制执行与 CLI/Web 同源的 `nww_packet_builder_v2`（packet_v2 契约）+ `repair_packet_contract`（角色卡/facts 兜底闸）+ 内容存在性硬闸（global/主角境界/position，章>10 BLOCK）；退出码非 0（stdout JSON `ok:false` 含 problems）→ **禁止写正文**，先按 problems 修复细纲/状态再重跑。成功后写 `追踪/_preflight_ok_ch{N}` 标记。**禁止跳过本闸用「Phase 1 方法论拼装」替代 packet 契约**——硬事实锚仍按 Phase 1 构造（`硬事实锚优先`），但结构化 packet 必须经此单一来源。
- 确认 `{BOOK}/追踪/_tracking-state.json` 已存在（无则先跑 `/nww-setup` 的 `nww_track_commit.py init`）。
- 读取本章细纲 `大纲/细纲_第{NNN}章.md`（= V12 的 blueprint；命名须为 `细纲_第N章.md`，写前守卫与字数闭环都按此匹配）。缺细纲先退回规划，不许裸写。
- **voice 前置闸门（硬阻断）**：细纲必须含**独立小节** `## 出场角色voice锚点`（标题逐字、H2 层级），正文为 `- **角色名**：①…②…` 结构化条目，每个出场角色至少一条、建议 3-5 条不可违反规则（认知标尺/句法节奏/情绪变调/低频锚点状态约束）；解析器按「角色名 + 至少一条规则」判定，仅出现「voice」字样、或只有写在 `#### 人物关系和出场顺序` 下的旧加粗字段行（历史兼容形态：仍可读取，但不再作为输出要求）均不算通过。逐个核对出场角色——`设定/角色/{名}.md` 不存在的新角色，必须先调 nww-character-designer 建卡（含四维voice档案（认知标尺/句法节奏/情绪变调/低频锚点）+禁用表达列表），再补细纲voice锚点，否则不许进入 Phase 1。已有角色卡须含 `禁用表达` 列表，缺则补全后再写。
- **本章目标统率字段（硬阻断，2026-09-09 升级，收编原「收益兑现」字段）**：细纲必须在章节名之后、出场角色voice锚点之前含 `本章目标` 字段，四行统率结构：
  1. `主爽点`：本章核心爽点一句话（决定 E4 打什么）
  2. `收益兑现`：本章主角拿到什么可感知的收益（能力/信息/资源/关系/声望/代价免除等；原「本章收益兑现」硬字段并入此处，语义不变）
  3. `章尾钩子`：类型 + 内容（原独立字段并入，语义不变）
  4. `身份耦合本章兑现项`（可选）：主角职业/身份能力在本章如何参与金手指或主线（如巷道活地图/载具/时刻敏感）；无兑现项的章不得出现职业铺陈
  缺此字段的细纲视为未完成，退回补写后再进入 Phase 1。存量书细纲缺此字段时按旧「收益兑现+章尾钩子」双字段验收并输出 WARNING 提示迁移。该字段是 I-I 承诺兑现度审稿维度的第一对照锚。
- **节拍可验收规则（硬阻断，2026-09-09 新增）**：细纲每个节拍必须含【叙事目标】，且目标必须可验收——含以下至少一项：字数/位置上限（如「≤250 字」「异常须在正文前 300 字内」）、必须埋的种子（如「埋 X 伏笔/耦合」）、排他禁令（如「禁写 X」）。纯形容词目标（「营造氛围」「蓄积势能」「强化张力」）视为未写，退回补写后再进入 Phase 1。
- **铺陈删除测试（硬闸门，2026-09-09 新增，通用）**：任何与金手指、主线零耦合的铺陈段/句必须删除——写手自检标准：「删掉这句，金手指/主线是否零损失」；零损失即删。开篇「正常世界一瞥」≤250 字，第一个异常/钩子事件必须在正文前 300 字内出现（题材无异常设定的书，替换为第一个冲突/钩子事件）。
- **导播式节拍结构（推荐范式）**：每个节拍由三部分组成——【原设事件】（发生什么）+【叙事目标】（可验收）+【镜头焦点与排他准则】（拍什么、不拍什么）。
- 读 `{BOOK}/追踪/_tracking-state.json`（唯一权威状态，含代价计数 `cost` 段；V12 legacy 的 `{BOOK}/state.json` 有则兼容读取、无则按空状态处理）+ `{BOOK}/bible.json`（世界观/角色/代价系统/签名比喻/反派败相）。
- **结构指纹门禁（写前硬闸，2026-09-09 接入）**：进入 Phase 1 前必须跑 `python3 skills/nww-novel-skill/scripts/nww_trope_guard.py check --chapter <章号> --project <书根>`。退出码 `0` = PASS / SKIPPED 放行；退出码 `1` = BLOCKING（CD 冷却未结束 / 冷却恢复期变奏未通过 / SimHash 语义碰撞 / 组合碰撞 / 枚举越界）→ 回细纲按输出的 `suggestions` 换维度或调整章序，改完重跑，**不许带着 BLOCKING 写正文**。零侵入：细纲无 `structural_fingerprint` 区块时返回 SKIPPED（退出码 0），存量书不阻断。**前提（2026-09-09 实测）**：① 本闸依赖细纲带 `structural_fingerprint` 区块——当前 190 份存量细纲无一含该字段，本闸对其全部 SKIPPED（等于没开），只有 nww-architect 新产出的细纲才生效；② 指纹历史由 commit 时 A-5b 自动 `register` 累积，**存量章不会自动补历史**（无 backfill 类命令，存量细纲补指纹只能由 LLM 按 trope-taxonomy 枚举改写细纲后逐章 register）。

## Phase 1 · 构造硬事实锚（写进写作 prompt 的「禁止偏离」段）

从 `bible.json` + `state.json` 抽取，拼成一段「【硬性事实锚】」：

- **境界数字锚**：`主角当前境界：{state.protagonist.cultivation}。正文中一切境界数字必须与此及细纲一致，禁止自创。` state 缺 `protagonist.cultivation` 时，从 `bible.json` 主角（role==主角）的 `identity` 兜底抽取（如「炼气三层」）作为初始境界锚；两处都缺时以正文主角语境峰值补入（`nww_enforce.py` 只补不压），禁止凭空自创境界数字。
- **角色事实**：每个角色 `身份` 里抽到的 `炼气X层` + `自称「{voice}」`；带 `anchor` 的角色附动机锚点（第1章必须落成一句独白，此后绝境回响）。
- **12层角色行为约束（Hard Constraints Anchor，非旁路，默认注入）**：从 `bible.characters[]` 读每个出场角色的 12 层关键字段，拼进「【硬性事实锚】」角色子段，writer 必须遵守：
  - `hard_constraints.always` → 拼"该角色永远会做什么：{每条}"——writer 必须在行为中体现，不写反
  - `hard_constraints.never` → 拼"该角色绝对禁止做什么：{每条}"——writer 硬约束，命中即不合格
  - `backstory.events[]` → 拼"该角色过去经历：{每条的核心经历 + 形成的信念 + 形成的恐惧}"——writer 构建动机、内心独白、选择逻辑时必须参考，不许违背角色信念
  - `ability.strengths` → 拼"该角色擅长什么：{每条}"——writer 不能写出角色做不出的事
  - `ability.weaknesses` → 拼"该角色不擅长什么：{每条}"——writer 不能让角色在弱点上表现过强
  - `ability.capability_boundary.can` → 拼"该角色可以做到：{每条}"
  - `hard_constraints` / `backstory` / `ability` 缺失时跳过该字段注入（不报错），但 bible_check 已在上游做过 WARNING 提醒
- **出场角色voice不可违反规则（写进「禁止偏离」段）**：从细纲 `出场角色voice锚点` + 角色卡 `禁用表达` 列表抽取，每个出场角色3-5条硬规则（认知标尺/句法节奏/情绪变调/低频锚点状态约束）。例：青袍人——禁用「好自为之」、不坐不喝茶、无表情描写、路过温故不侧头不出声。LLM写章时必须遵守，违反即视为不合格。
- **人物动力锚（Character Dynamics Anchor，可选旁路）**：仅当 `{BOOK}/.character-dynamics.enabled` 存在时启用，否则跳过零注入。从 `bible.characters[]` 读每个出场角色的 `flaw / anchor / lifecycle / break_rules`，拼进写作 prompt：
  - `flaw` + `anchor` → 转成两句"人格动力锚"：本章该角色最想守住什么、最怕变成什么。主角做选择时，这两条在拉扯他——不是善良，是怕。**注意：`anchor` 是他内心反复回响的独白（"他在心里说了一句……"），不是说出口的台词；说出口的破例台词写在 `break_rules[].effect` 里，两者不要写成同一句话，否则配额永远用不出去。**
  - `lifecycle` → **当前阶段从 `追踪/_tracking-state.json` 的 `global.position.protagonist_stages.{角色名}` 读**（按角色名区分，如 `global.position.protagonist_stages.陈渡`；若不存在则默认 `lifecycle[0]`）。拼："他现在处于「{stage}」阶段，信的是：{belief}"。**作者在阶段切换那章 commit 时，通过 `global_change.position.protagonist_stages.{名}` 显式更新到下一阶段**；不要靠写手自己判断"上一章算不算 exit_when 事件"。
  - `break_rules` → 拼"稀有破例提醒"：每条列出 `name / trigger / effect / meaning / quota`，并标注"全书仅 {quota} 次，不要在普通场景提前用掉"。**`effect` 是他说出口的台词（区别于内心独白的 anchor）；trigger 未在本章发生时，不得让该角色破例。** **当前剩余配额从 `tracking/_tracking-state.json` 的 `global.used_breaks.{角色名}.{rule_name}` 读**（已用次数），拼："已用 {used}/{quota} 次，剩余 {quota-used} 次"；已用完（used >= quota）时标注"已用完，禁止再用"。写章后跑 `nww_break_check.py` 自动检测本章是否触发了新的破例，commit 时把结果写进 `global_change.used_breaks`。
  - 硬规则：本章主角的关键选择必须能从 `flaw` 推出（他为什么这么选，而不是随便一个人都这么选）；`break_rules` 的 trigger 未在本章发生时，不得让该角色破例（如陈渡不得突然说长句）；`anchor` 每章最多回响一次，不要每章都让他心里默念同一句话。
  - `emotion_pattern` → 拼"该角色的情绪反应模式"：`happy.expression` → "开心时会…"；`angry.trigger/reaction` → "被触发时会…通常反应是…"；`afraid.hidden_fear/behavior` → "隐藏恐惧是…害怕时会…"。writer 写情绪场景时必须按此模式反应，不许随意替换情绪表达通道。
  - `thinking` → 拼"该角色遇到事情时的第一反应"：`first_reaction_question` → "遇到事情时他第一反应想的是…"；`thinking_style` → "他的思考风格是…"。writer 写内心独白时必须按此风格，不许让角色用不属于他的思考方式推理。
  - 默认关闭（无标记文件不注入，不改变现有 Phase 1 行为）。
- **语言生成锚（Voice Generation Anchor，可选旁路）**：仅当 `{BOOK}/.voice-generation.enabled` 存在时启用，否则跳过零注入。从 `bible.characters[]` 读每个出场角色的 `voice_generation` 字段，拼进写作 prompt：
  - `sentence_pattern.default` → "他怎么组织句子"（如：主语省略、动词先行、少形容词、短句为主）。
  - `sentence_pattern.examples` → 附 2-3 句典型台词作风格参照。
  - `sentence_pattern.avoid` → "他不会这样说话"（如：不写长因果解释、不直接说谢谢/对不起）。
  - `information_focus` → "他听别人说话时关注什么"（如：数量/距离/风险/出口；或规则/逻辑漏洞/隐藏目的）。别人说一句话，他追问的方向由这个列表决定。
  - 硬规则：三个角色遇到同一场景，不得输出同一句提醒/同一种反应——必须从各自的 `information_focus` 推出不同的追问方向。
  - 默认关闭（无标记文件不注入，不改变现有 Phase 1 行为）。
- **代价刻度（必种硬锚）**：`{device}——{rule}；本章发力结束后必须按「{counter_format}」写一行（只进不退）`。
- **叙事视角**：`bible.pov == "first"` → 叙述段用「我」；`"third"` → 用角色名/「他」，禁止叙述段用「我」。
- **倒计时 / 机制设定**：`state.countdown` 每条、`bible.mechanics` 每条（呈现现象不解释）。
- **大主线锚**：读 `大纲/卷纲.md`（本卷主题/本卷主线步进/长期伏笔），拼成「本章落在本卷主线第几步、为哪条长期伏笔做铺垫」；每章必须推进大主线或铺设长期伏笔，禁止写成不服务于主线的孤立单元剧。缺卷纲则省略该锚并提示补卷纲。
   - **本卷重心排期读取**：额外读卷纲「本卷重心排期」三张表（人物重心表含机制认知行／支线生命周期表／商品职责分配表）与「细纲执行字段」（voice／群像／章首续震）。凡本章命中的重心项，必须写进本章细纲承接；表格里标 `【待补】/【待拍板】` 的项，本章遇冲突时按"能补则补、不能补则显式带过并留冲"处理，不得无声忽略。
- **全局状态锚（Global State Anchor，千章长篇核心）**：读 `追踪/_tracking-state.json` 的 `global` 字段（作品当前运行时状态的唯一全局快照），拼成「【全局状态锚】」段，包含：
  - `global.position`：当前章 / 纪元 / 卷 / 核心冲突 / 发动机(type,phase) / 主题
  - `global.world`：世界阶段 / 活跃规则 / 已发生的世界变化（最近3条）
  - `global.factions`：主要势力当前阶段与立场
  - `global.next_trigger`：下一次全局状态切换的触发条件
  写作硬规则：本章必须与全局状态一致（核心冲突/发动机阶段/世界规则不得与 global 矛盾）；本章若推进了全局状态（如世界规则变化、势力立场转变、发动机阶段切换），必须在 Phase 6 状态更新的 `global_change` 中显式提交增量（只返回本章变化的字段，deep-merge 入已有 global）；`global` 为空时不得省略全局状态锚（空则 BLOCK）；章>10 必须先补 global_change / 初始化 global 再写章。
- **创作原则锚（Creative Anchor，V12.2 新增，主题不散护栏）**：读 `bible.json` 的 `creative` 字段（全书创作原则，可选字段），拼成「【创作原则锚】」段，包含：
  - `creative.core_question`：全书核心命题（如"如果世界逐渐失去规则，人还能依靠什么存在"）
  - `creative.emotional_engine`：情感发动机（主角情感弧线总纲，如"沈缺从被世界选择，到主动选择世界"）
  - `creative.novelty_pillars`：新颖性支柱（每条规则事件必须满足的创新要求，如"规则消失必须改变社会结构""力量获得必须伴随代价"）
  - `creative.forbidden_patterns`：禁用模式（如"重复规则吸收""纯升级路线""无代价获得力量"）
  写作硬规则：本章必须与创作原则一致——核心命题不得漂移、情感发动机不得中断、新颖性支柱不得违反、禁用模式不得出现；本章若获得力量/规则事件，必须检查是否伴随代价、是否改变主角认知、是否与 novelty_pillars 一致；`creative` 为空（未启用）时省略该锚，不影响原有流程。该锚为 advisory 级提醒，不刚性阻断提交（由 Phase 5 `nww_enforce --quality-advisory` 做质量告警）。
- **专属意象**：`bible.signature_metaphor.seed` 作为意象基调，可自然变体承载（动作/声音/气味/环境反馈/角色化比喻），不必照抄原词也不复用固定「像XX」句；每章用 `core_feeling`（须传递的情绪功能）立意，`possible_forms` 为可选方向，`avoid`（如固定比喻句复读）为禁止表达。
- **反派败相三拍**：`bible.villain_down_beats` 至少落 2 个关键词 + 给反派一小段视角写「他不明白自己怎么输的」。
- **推理·证据呈现引导**：主角是智力型/数据型人设时（bible.mechanics 含推算/统计类机制），正文必须把「他怎么得出的」写成读者可核验的证据链——异常事实 → 对照样本 → 结论，至少一段显式推理过程；禁止只写结论、跳过过程（避免「开挂感」）。此引导随 mechanics 出现，不强制加给非智力型题材。
- **前情**：上一章 `摘要` + `状态快照` 摘要。
- **休眠角色回溯（退役不删除）**：写前扫描本章细纲提到的角色名，命中 `追踪/休眠角色.md`（退役角色池）时，
  把该角色的表格行（身份/退役章/退役原因）注入「角色回溯」段，并从 `_tracking-state.json` 的
  `dormant_characters.{名}.snapshot` 取完整快照作为 voice/状态锚。复活的章在 Phase 5 事务中
  用 `delta.revived_characters` + 该角色全新 `character_snapshots` 提交（commit 会回显退役元数据）。
- **记忆包（可选旁路，默认关闭）**：仅当 `{BOOK}/.memory-pack.enabled` 存在时启用，先运行
  `{PYTHON} {SKILL}/../scripts/nww_memory_pack.py --project {BOOK} --chapter {N} --task write --json`，
  把输出的三层记忆包（working/episodic/semantic）作为「优先级选材」并入写作上下文——working 给当前位置与本章承诺，
  episodic 给近章/时间线事件，semantic 给写作铁律/连贯性风险/角色状态/活跃伏笔（按细纲关键词相关度排序）。
  默认关闭（无标记文件不注入，不改变现有 Phase 1 行为）；开启后需按「盲评验证产出质量不降」再决定保留。
- **对标锚（Benchmark Anchor，可选旁路，默认关闭）**：仅当 `{BOOK}/.benchmark.enabled` 存在时启用，先运行
  `{PYTHON} {SKILL}/../scripts/nww_benchmark_recall.py --book {BOOK} --name {对标名} --chapter {N} --out json`，
  把输出的【对标锚】分段（题材卡锚 + 情绪模块锚 + 节奏锚 + 文风锚，advisory 级）并入写作上下文。
  对标名缺省取书项目目录名，匹配 `拆文库/{对标名}/` 下的拆文产物（`剧情/情绪模块.md`、`剧情/节奏.md`、`文风.md`）；
  题材卡按 `bible.json` 的 `genre` 标签 best-effort 匹配 `references/agent-references/genre-prose-cards/`。
  硬边界：对标锚一律 advisory，冲突时硬事实锚/voice 锚/代价刻度/全局状态锚优先；只借读读者期待与写法节奏，
  禁止复制对标桥段、专名、台词与文风中标注「不可模仿」项。
  默认关闭（无标记文件不注入，不改变现有 Phase 1 行为）；开启后需按「盲评验证产出质量不降」再决定保留。
- **排版锚（Format Anchor，V12.6 新增，默认启用非旁路）**：读 `{BOOK}/设定/文风.md` 的「句式与排版节奏」节，拼成「【排版锚】」段，作为 Output Contract 注入写作 prompt，声明优先级与裁决关系：
  1. **单屏阅读体验**：手机滑动，一段一个视线落点；常规单段 ≤60 字（约 3 行），高危动作单句成段。
  2. **信息分层**：动作 / 感官 / 心理 / 规则解释必须分层呈现；禁止一个自然段同时承担 ≥2 个信息层（同一瞬间的感知-反应可同段，视为同一层）。
  3. **戏剧连续性**：同一戏剧事件保持连续，但「连续 ≠ 同段」——与 format-and-structure.md 的「不拆层」冲突时，按拆信息层级执行：禁止把多层信息焊进一段。
  4. **语言节奏**：长短交错，重拍极短（1-2 行）、呼吸段稍长（4-5 行，80-150 字）。
  **呼吸段与推理链是合法的，禁止为凑短段切碎**：氛围铺陈（世界定格/声音剥离）、完整推理链（算账/识别/计数）、情绪蓄力段允许保留 80-150 字长段，全章占比 ≤20%，与短段重拍交织；「单段 ≤60 字」是常规叙述基准，不是硬上限——判断标准是"这段是否是一个完整的信息层/氛围单元"，是则保留完整，混入多个信息层才拆。
  写章自检：每段写完对照排版锚自查（单段是否混入多层信息 / 是否把完整氛围或推理切碎了），违者重排再进 checkpoint。
- **表达记忆锚（Expression Memory，默认启用）**：写前读取 `{BOOK}/追踪/expression_memory.json`（最近5章滑动窗口），
  抽取当前 HIGH/MEDIUM 告警，生成 ≤800 字的「【表达记忆提醒】」注入写作 prompt。
  运行：
  ```bash
  {PYTHON} {SKILL}/../scripts/nww_expression_audit.py --book {BOOK} --preview-next {N}
  ```
  **硬边界**：
  - CANON（功法/招式/机制/专名/产品）完全豁免，不干预
  - SIGNATURE（人物口头禅/标志动作/核心信念）只观察不阻断
  - 仅 VARIABLE 表达模板（TPL-A内心强撑/TPL-B通用反应/TPL-C空间定位）连续≥3章时提醒
  - 提醒只建议「换表达通道」（动作/生理/注意力/环境/对话），**禁止建议同义词替换**
  - 提示含免责声明：「不要求机械替换，如该通道是当前场景最自然的方式可继续使用」
  - 无 HIGH/MEDIUM 时输出「近期无高优先级表达模板告警，正常写作」，不注入多余内容
  记忆文件不存在时跳过（首次写章前无历史，正常）。

## Phase 2 · 写章（多段 + 每段 checkpoint 强制收敛）

- 温度 0.9（爽感/节奏），max_tokens 3000。
- LLM 严格按细纲写，**不写标题行、不写 ``` 围栏**，输出纯正文（空行分段，对话独立成行）。
- 把细纲批准情节点按连续顺序切 3-4 段。逐段写入累计临时文件 `{BOOK}/.tmp/seg_{NNN}.md`（不存在则创建），每写完一段立即跑一次写中 checkpoint：
  ```bash
  {PYTHON} {SKILL}/../scripts/nww_ctl.py wordcount checkpoint --file {BOOK}/.tmp/seg_{NNN}.md --target {细纲字数目标} --chapter {N}
  ```
  把返回的 `actual / remaining_user_range` 连同下一段情节点交给 narrative-writer；本段预算落在剩余区间内，逼近/超出上限即进入收敛模式（只补未落地批准情节点、删冗余过渡），绝不越带；明显低于下限则按补足模式在已批准情节点内加三维度详写。
- 全部段完成拼成 `prose`，删除 `{BOOK}/.tmp/seg_{NNN}.md`，去掉可能的标题/围栏。

## Phase 3 · 写后确定性机械层（模型无关，必须先跑）

```bash
{PYTHON} {SKILL}/../scripts/nww_enforce.py --book {BOOK} --file {BOOK}/正文/第{NNN}章*.md
```

该脚本（自洽、不依赖 server/）执行：负向词删除 → markdown 清洗 → 括号对白转引号 → 代价不可逆修复 → crack meter 纠偏 → 必种件兜底注入 → 主角境界守卫（只补不压）。stdout 打 findings JSON，供你汇报改了什么。

## Phase 4 · 去 AI 味

调用 `/nww-deslop`（或直接套用 `skills/deslop`）：先 `node {SKILL}/../scripts/check-ai-patterns.js --check --fail-on=blocking` 确定性预检，再按 A–G 七门禁 + 三遍法改写。保留剧情功能，改最少字。

**最小改动原则（改写前必须判定）**：不是每句话都需要改。改写前先判断原句是否属于以下三类，只有命中才改：
1. **模板化表达**：命中 TPL-A（心理直述+转折+外表镇定）、TPL-B（通用角色反应）、TPL-C（机械空间定位）等 Expression Memory 告警模板
2. **AI味表达**：check-ai-patterns 检出的 blocking 问题（排比堆砌、模板比喻、情绪直接命名等）
3. **负向词**：nww_enforce 检出的可删除类负向词

**以下情况不改写**：
- 已经是简洁动作/状态陈述（如"她没睡好""他走了"），不含心理直述词，不在模板范围内 → 保留原文
- SIGNATURE 人物标志表达（口头禅、标志动作、核心信念句）→ 保留
- CANON 固定表达（功法、招式、专有名词）→ 保留
- 细纲"复沓锚句"字段要求的原话回响 → 保留

**改写时禁止**：为了"具体化"而自发引入新物品/新场景（如把"她没睡好"扩写成"她躺在折叠床上翻来覆去"）。换通道优先使用已有场景元素，不新增世界观外物品。

## Phase 4.6 · 审稿-修订自动闭环（V12.5，千章质量不漂移核心）

deslop 完成后、进入 Phase 5 之前，执行一轮 LLM 审稿-修订循环（**不再依赖人工触发**）：

1. **审稿**：按 score90 外部维度（E1 章首钩子 / E2 设定绑定 / E3 节奏张力与认知落差 / E4 爽点与成长动力 / E5 人物与对话双职能）+ 内部维度（I-A~I-I，其中 **I-I 细纲承诺兑现度为 2026-09-08 新增强制维度**）对本章正文打 0-100 分，**每条扣分必须挂正文证据**（文件:行/引文），输出结构化 issues。
   - **I-I 细纲承诺兑现度（强制）**：审稿时必须先读取本章细纲，逐条对照①「本章目标」统率字段逐行验收（主爽点是否成立/收益兑现是否在正文交付/章尾钩子类型与内容是否兑现/身份耦合兑现项是否落地——存量书按旧「收益兑现+章尾钩子」双字段对照）②节拍表每条承诺（含叙事目标中的量化指标：字数上限/位置/种子/禁令）是否在正文落地 ③纪律项与铺陈删除测试是否遵守。未兑现项必须挂证据「细纲第X条: <原文> → 正文: <缺失/偏离/位置>」，禁止只给总分不挂证据。I-I 低于 70 分视为结构层缺口，必须回细纲补对应项后重写受影响段落（属 `structure` repair_scope），不允许靠文笔修订蒙混。
2. **判定**：`overallScore >= 85` 且无 S1 级 issue → 通过，进入 Phase 5。
3. **修订**：低于 85 分 → 按 issues 修订。**先分 repair_scope 再动手**：
   - `structure`（缺钩子/缺绑定/缺落差/缺卡点/**I-I 细纲承诺未兑现**）→ 不改词句，回细纲补对应结构项后重写受影响段落；
   - `prose`（文笔/AI 味/节奏病）→ 走 deslop 局部修订（最小改动原则）。
4. **最多 2 轮**：每轮修订后重新审稿打分；**净提升 <3 分即停**，保留历史最高分版本（best-snapshot 原则——修砸了回退，不硬撑）。
5. 全部轮次用尽仍 <85 分 → 带分数与未决 issues 进入 Phase 5 提交（commit 侧 A-2 只拦 I-G/I-H，此处分数供作者复盘，不假性阻断）。
6. **评分落盘（Phase 5 commit 成功后执行）**：把最终采纳版本的审稿结果写入追踪层，作为质量趋势数据地基（`check` 会输出最近 10 章均分/最低分/低于 85 计数，≥4 章低于 85 追加 QUALITY WARNING）：
   ```bash
   {PYTHON} {SKILL}/../scripts/nww_track_commit.py score --project {BOOK} --input 评分.json
   ```
   `评分.json`：`{"chapter": N, "score": 0-100, "issues": ["扣分项（挂正文证据）", ...], "rounds": 实际修订轮数}`。只允许给**已提交**章节打分（超前章/非法分数/未知字段均 exit 2），rollback 到第 N 章时 >N 的评分文件随快照差集清理自动移除。

**跨章检测（advisory，同轮执行）**：
```bash
{PYTHON} {SKILL}/../scripts/nww_cross_chapter_check.py --book {BOOK} --chapter {N}
```
报告跨章 6 字短语重复 / 段落密度漂移（电报体回潮）/ 近 5 章标题撞词。只提示不改稿、不阻断；
WARNING 项在本轮修订中一并处理（属 `prose` scope）。

**风格指纹（定量可比，同轮执行）**：
```bash
{PYTHON} {SKILL}/../scripts/nww_style_fingerprint.py --book {BOOK} --chapter {N} --save
```
输出本章句长分布（均值/中位/P90/CV）、段落节奏（均长/短段占比/段长CV）、对话占比、比喻密度、段均句数，并入 `追踪/风格指纹.json` 归档；与前 ≥3 章均值偏离超 50% 的指标打漂移 NOTE（只提示不判定好坏）。修订前后各跑一次可用数字对比确认「改而不漂」。

## Phase 5 · 一致性 + 追踪提交

1. **一致性门**：调用 `/nww-consistency`（只读 S1–S4 报告）。
2. **voice 一致性门（确定性，必须先跑）**：
   ```bash
   {PYTHON} {SKILL}/../scripts/nww_voice_check.py --book {BOOK} --file {BOOK}/正文/第{NNN}章*.md
   ```
   读角色卡 `禁用表达` 列表，扫描正文该角色台词/动作，命中禁用表达直接报错（和 enforce 同层级，确定性规则匹配，不依赖 LLM 判断）。存在命中 → 必须修，重跑至零命中；零命中 → 通过。
2.5. **破例触发检查（确定性，仅当 `.character-dynamics.enabled` 存在时跑）**：
   ```bash
   {PYTHON} {SKILL}/../scripts/nww_break_check.py --book {BOOK} --file {BOOK}/正文/第{NNN}章*.md
   ```
   从 bible.json 读每个角色的 `break_rules[].effect`（说出口的台词），扫描正文检测是否出现在引号内（说出口）。输出 `triggered` JSON（哪些角色触发了哪个破例）。**此结果必须带入 Phase 6 的 `global_change.used_breaks`**——每个触发的 `{角色名}.{rule_name}` 计数 +1（deep-merge 会自动累加）。标记文件不存在则跳过，不影响现有流程。
3. **闭环核验**：按 `nww-setup/references/agent-references/consistency-closure.md` 模板对 S1/S2 做闭环——修复（重跑一致性确认冲突消失）或人工裁决（记录裁决理由，作者/编排者确认）。**存在未闭环的 S1/S2 → 禁止进入下方提交**；S3/S4 记待办，不阻断。
3. **字数闭环**：
   ```bash
   {PYTHON} {SKILL}/../scripts/nww_ctl.py chapter check   --project {BOOK} --chapter {N}
   {PYTHON} {SKILL}/../scripts/nww_ctl.py chapter commit  --project {BOOK} --chapter {N} --input {逐章事务.json}
   ```
   只接受「用户带」内章节；`under` 不自动补写，`over` 返回净删型 `compress-once`。
4. **追踪事务**：`nww_track_commit.py commit` 原子写入 `_tracking-state.json` + 重建派生视图（上下文.md / 角色状态 / 伏笔.md / 时间线）。`check` 校验逐字一致。**退役角色必须带原因**：`delta.retired_characters` 中的每个角色在 `delta.retirement_reasons`（`{"名": "原因"}`）登记原因——退役角色进入休眠池（`dormant_characters`，退役不删除），第 500 章复活有据可查。**复活休眠角色**：`delta.revived_characters: ["名"]` + 该角色全新 `character_snapshots` 快照，commit 校验其在池且非活跃。
   **delta 草稿预填（降低漏写）**：手写 delta 前先跑
   ```bash
   {PYTHON} {SKILL}/../scripts/nww_delta_draft.py --project {BOOK} --chapter {N} --out {BOOK}/草稿/第{N}章-delta.json
   ```
   脚本与 A-4 共用提取器，从正文确定性预填：死亡→`retired_characters`+原因占位、境界/能力变化→`character_changes`+当前快照副本、在场角色→`context.active_character_names`；受伤/活跃伏笔提及进 `_draft_hints` 提示段。LLM 在草稿上补语义（`result`/`chapter_type`/`cost_change`/伏笔变更等，`【草稿】`占位必须全部消除），然后直接送 `commit`（`_` 前缀注解段随行不校验）——A-4 闸在 commit 侧终审，草稿只是降漏写脚手架，不是免检通行证。
**本章发生代价变化（如天机反噬+1、裂纹计数上涨）必须写进逐章事务 `delta.cost_change`（`{"device": ..., "count": ...}`），随 commit 原子提交到 `_tracking-state.json` 的 `cost` 段——这是代价计数的唯一权威存储，只进不退（merge 取历史 max）**。
**章节类型登记（卷级疲劳数据地基）**：每章在逐章事务 `delta.chapter_type` 写入本章类型（≤24B，如 `战斗`/`日常`/`交易`/`调查`/`爆发`/`过渡`）；`check` 检测尾部同型连续 ≥4 章时输出 FATIGUE WARNING，提示下一章切换类型。
5. **表达审计（Expression Memory，只记录不修改）**：正文落盘后运行
   ```bash
   {PYTHON} {SKILL}/../scripts/nww_expression_audit.py --book {BOOK} --chapter {N} --prose {BOOK}/正文/第{NNN}章*.md
   ```
   脚本执行：段落切分 → CANON/SIGNATURE过滤 → 7通道识别 → TPL-A/B/C/D/E模板识别 → 更新最近5章滑动窗口 `expression_memory.json` → 生成 HIGH/MEDIUM/LOW 告警。
   **硬边界**：只记录和更新记忆，**不自动修改正文**，不触发重写，不阻断提交。告警结果供下一章 Phase 1 写前提醒使用。stdout 打 JSON（template_summary / channel_distribution / alerts_updated），供汇报。

6. **时代器物词检测（世界观一致性护栏，只告警不修改）**：正文落盘后运行
   ```bash
   {PYTHON} {SKILL}/../scripts/nww_era_check.py --book {BOOK} --chapter {N}
   ```
   脚本执行：扫描现代工业制品词库 → 过滤主角随身白名单（`bible.json era_constraints.protagonist_brought`）→ 过滤有来源解释的上下文（"她带来的""从行李箱里翻出"）→ 过滤内心独白 → 物理世界中无来源现代物品 → HIGH告警；对话中出现 → MEDIUM告警。
   **硬边界**：只告警，**不自动删除**——可能是剧情需要（如主角拿出打火机）。HIGH告警需人工确认后修改。白名单从 `bible.json` 读取，穿越题材主角随身物品合理不告警。

7. **创作质量告警（Quality Advisory，V12.3 新增，advisory 不阻断）**：正文落盘后运行
   ```bash
   {PYTHON} {SKILL}/../scripts/nww_enforce.py --book {BOOK} --quality-advisory
   ```
   输出三项 QUALITY WARNING（不阻断提交，仅供作者参考）：
   - 主角主动性：最近3章连续被动 → 建议增加主动目标
   - 声明与观察不一致：声明主动但未检测到主动行为关键词 → 建议核实
   - 规则获取重复：最近3次规则事件获取方式相同 → 建议变化获取方式
   **硬边界**：advisory 级，只提醒不阻断，不触发重写，不影响 commit。作者可选择采纳或忽略。

8. **伏笔治理策略检查（K5 debt 元数据，2026-10-23 新增，随写后审计执行）**：正文落盘且追踪提交成功后运行
   ```bash
   {PYTHON} {SKILL}/../scripts/nww_foreshadow_policy.py --project {BOOK}
   ```
   检查项：① 已埋伏笔缺 `planned_resolution_chapter`（回收无期=悬账）② 核心伏笔缺 `half_life_chapters` ③ 核心伏笔超半衰期未推进（复用 K5 官方判定，不双报）④ 孤儿引用与活跃负载计数（advisory）。
   退出码 `0` = advisory 通过（报告输出到 stdout，不阻断）；退出码 `2` = BLOCKING（仅当 `{BOOK}/.foreshadow-strict` 标记文件存在**且**存在缺字段项持续 ≥3 章，新埋伏笔有 3 章补齐宽限）→ 必须回逐章事务补 `foreshadow_changes` 元数据后重跑至通过；退出码 `1` = 环境错误（state 缺失/损坏）。
   **硬边界**：脚本对 `_tracking-state.json` 只读、绝不写盘；默认（无标记文件）不阻断，不改变现有流程行为。

9. **卷末复盘门禁（2026-10-23 新增）**：本章为卷末章（章号等于某 `大纲/卷纲_第N卷.md` 章节范围终点）时，追踪提交成功后**必须**运行
   ```bash
   {PYTHON} {SKILL}/../scripts/nww_volume_review.py --project {BOOK} --volume {N}
   ```
   生成 `追踪/卷末复盘/卷N_复盘.md`：确定性层（伏笔账 / 逾期与缺字段 / 跨卷继承清单）由脚本计算，含账务不一致告警（逐章记录出现但 state 未登记的伏笔 ID）；研判层（卷纲核心目标逐条核对 / 三事件弧落地三态）为留白模板，由人工/LLM 填写，脚本不下任何叙事判断。
   **硬边界**：脚本只读 state/大纲/逐章记录，唯一写盘=复盘 md，不阻断 commit；卷未写完也可运行（自动转期中复盘）。研判层建议在下卷开写前填完，作为下卷细纲的对照锚。

## Phase 6 · 摘要 + 状态更新（低温、deep-merge）

- **摘要**（温度 0.3，≤300 字）：只陈述正文明确事实，数字原样照抄，禁推测。存 `追踪/逐章记录/第{NNN}章.md`（紧凑变化，≤1536 字节）。
- **状态更新**（温度 0.1）：模型按 `{STATE_PROMPT}` 返回 JSON；**deep-merge** 入旧 `state.json`（缺失字段保留旧值）；再跑 `_realm_guard`（只补不压）。**不再写回 `crack_count` 到 `state.json`**——代价计数权威在 `_tracking-state.json` 的 `cost` 段（由 Phase 5 的 `cost_change` 事务维护），`nww_enforce.py crack_meter_correct` 自动从那里读权威值纠偏，双轨一致。
  - **全局状态增量（global_change）**：本章若推进了全局状态（世界规则变化、势力立场转变、发动机阶段切换、纪元/卷切换等），必须在逐章事务 JSON 中增加 `global_change` 字段，只返回本章变化的 global 子字段（如 `{"position": {"engine": {"phase": "成熟期"}}, "world": {"changes": ["R002: ..."]}}`）；`nww_track_commit.py commit` 会将其 deep-merge 入 `_tracking-state.json` 的 `global` 段，未提及的字段保持不变。本章无全局状态变化时省略该字段（不传 `global_change` 即表示无变化）。
    - **角色生命周期阶段（protagonist_stages）**：本章若发生了 lifecycle 里某个阶段的 `exit_when` 事件，必须在 `global_change.position.protagonist_stages.{角色名}` 写入新阶段名（如 `{"position": {"protagonist_stages": {"陈渡": "河界"}}}`）。系统不自动判断阶段切换，由作者在 commit 时根据细纲/正文确认。
    - **破例使用计数（used_breaks）**：本章若 Phase 5.5 的 break_check 检测到触发，必须在 `global_change.used_breaks.{角色名}.{rule_name}` 写入累加后的总数（如 `{"used_breaks": {"豆豆": {"第一次完整表达": 1}}}`）。deep-merge 会自动累加到 global.used_breaks；写前 Phase 1 会读此值拼剩余配额。
  - **质量元数据（quality_meta，V12.3 新增，可选）**：逐章事务 JSON 中增加 `delta.quality_meta` 字段，含 `declared`（模型声明，`protagonist_initiative` 为 bool）和 `observed`（留空 `{}`，commit 时由系统自动填充主动行为关键词命中数/全局状态变化/关系变化）。用于 Phase 5 quality-advisory 质量告警。本章无特殊质量标注时省略。
  - **规则丢失事件（rule_loss_event，V12.4 新增，可选）**：本章若发生规则丢失/承载事件，逐章事务 JSON 中增加 `delta.rule_loss_event` 字段，含 `rule_id`（必填）、`rule_name`（可选）、`actual`（必填，含 method/cost/impact/emotion）。commit 时写入 `追踪/档案/rule_history.json`（独立历史档案，不污染 global）。本章无规则丢失事件时省略。

> 完整 `STATE_PROMPT` 结构见 `scripts/nww_enforce.py` 顶部的 V12 原版契约：`time/location/protagonist(含cultivation)/inventory/knowledge_table/countdown/open_threads`。

---

## 与 V12 旧管线的对应（审计可追溯）

| V12 scene_writer.py | 本 skill 落点 |
|---|---|
| `enforce()` 机械删除 | `scripts/nww_enforce.py` |
| `_patch_plants` 必种件兜底 | `nww_enforce.py: plant_fallback`（机械注入，fail-closed） |
| `_realm_guard` 只补不压 | `nww_enforce.py: realm_guard` + Phase 6 |
| `crack_count` meter 纠偏 | `nww_enforce.py: crack_meter_correct` |
| `_merge_state` deep-merge | Phase 6 状态更新 |
| `rule_checker` 七类 findings | `nww_enforce.py` findings + `deslop` 脚本 + `consistency` agent |
| `assembler.build_writer_messages` | Phase 1 硬事实锚 + Phase 2 |
| `style_exemplars`（诡异民俗/轻松搞笑修仙） | `references/style_exemplars/*.yaml`，写前作为文风基准 |

**结论**：V12 已验证的 A–F 修复在纯 skill 模式下由 `nww_enforce.py` + 硬事实锚 + deep-merge 完整继承，未丢能力。
