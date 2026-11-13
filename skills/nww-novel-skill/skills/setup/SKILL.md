---
name: nww-setup
version: 1.0.0
description: |
  NWW-V12 开书与部署。初始化书项目布局（设定/大纲/正文/追踪），部署写前/写后确定性门禁 hooks
  与 agents 到 .claude/（或 .opencode/.codex/），并初始化追踪事务。触发：/nww-setup、/开书、「部署护栏」。
metadata: {"openclaw":{"source":"nww-novel-skill nww-setup + NWW-V12 适配"}}
---

# nww-setup — 开书与部署

把一个书项目初始化成 NWW-V12 skill 包可驱动的结构，并把「千章不崩」门禁装到位。

## Step 1 · 建书布局

在 `projects/{book}/` 下建：

```
设定/角色/  设定/世界观/
大纲/        （卷纲_第1卷.md + 细纲_第001章.md ...）   # 每卷一份卷纲、每章一份细纲，命名见下
正文/第一卷/  （第001章_章名.md ...）                  # 正文推荐按卷分子目录（正文/{卷名}/）
追踪/
```

正文按卷分目录为推荐结构（`正文/第一卷/第001章_章名.md`）；旧书平铺 `正文/第001章_章名.md` 仍被全部脚本兼容（细纲/卷纲一律平铺在 `大纲/`）。

细纲文件名统一为 `细纲_第{N}章.md`（允许前导零与标题后缀，如 `细纲_第1章_开篇.md`）——guard 与 nww_ctl 的字数闭环都按此命名匹配，`细纲_001.md` 会被写前守卫误判为缺失。

**细纲/卷纲内容规范（2026-09-09 起，与 prealign / long-write 硬阻断对齐）**：

- 每份细纲必须在章节名之后含「本章目标」统率字段（四行：主爽点/收益兑现/章尾钩子/身份耦合本章兑现项）；每个节拍用导播三件套（【原设事件】+可验收【叙事目标】+【镜头焦点/排他准则】），纯形容词目标视为未写。
- 每份卷纲建议含「铺陈纪律」段：铺陈删除测试（零耦合铺陈必删、开篇一瞥≤250 字、钩子前 300 字内）+ 金手指×身份耦合逐章兑现排期。
- 生成细纲/卷纲时即按此规范产出，不要等 prealign 补——生产端带规则，验收端才不返工。

把 `bible.json`（角色/世界观/代价系统/签名比喻/反派败相）放到 `{BOOK}/bible.json`（沿用 V12 字段；或由 `设定/` 推导后生成）。**新书在 bible.json 写入 commit 闸配置 + 选题三要素**：

```json
"policy": {
  "fail_closed": true,
  "a2_mode": "on",
  "commit_gates": {
    "consistency": true,
    "delta_audit": true,
    "meta_check": true,
    "settlement_anchor": true,
    "fact_provenance": { "enabled": true }
  },
  "overrides": []
},
"platform": "番茄",
"scale": { "total_words": "200万", "volumes": 4, "daily_words": 9000, "update_freq": "日更" }
```

- `platform`：发布平台（起点/番茄/知乎/刺猬猫/七猫/飞卢/晋江/纵横等），由 `/nww-topic` 发布平台确定环节锁定，供 long-write 校准节奏与书名风格，并**联动章节字数目标校准**（`references/agent-references/platform-wordcount.md`：番茄等区间平台细纲写区间格式如 2000-2500，起点等单值口径写单值，系统硬下限 2000 字）。
- `scale`：量级配置（全书预估字数/卷数/日更字数/更新频率），由 `/nww-topic` 量级确定环节锁定，供卷纲拆分与细纲密度参考。
- `pov`：叙事视角（first/third），沿用已有字段，由 `/nww-topic` 文风确定环节锁定。

含义：新书直接进入完整生产流——`nww_track_commit.py commit` 会机械校验「正文代价计数 == 追踪权威（A-1）」「I-G/I-H fail 拒绝（A-2，需 `--quality-dir`）」「无一致性闭环记录拒绝（A-3）」「正文-delta交叉审计（A-4）」「流程完整性（A-5，仅 append 模式强制，revision 模式豁免）」。新书经 `policy.fail_closed=true` 缺字段不再静默 False；存量书须显式 `policy.overrides`（reason + expires_at）或落盘完整 `policy.commit_gates`，并用 `nww_scan_commit_gates.py --book <书根>` 扫描缺 policy / 缺闸。

**A-5 强制规则（V12.5）**：`meta_check=true` 时，append 新模式必须经过 `nww_post_write.py` 全流程（enforce + 审稿闭环），缺少 `delta.quality_meta.enforced` 或 `reviewed` 直接 BLOCK。revision 修订模式自动豁免——局部修改不需要走完整审稿流程，可单独跑 enforce/deslop 等子步骤。

### Step 1.2 · 文风文件（建书标配）

在 `{BOOK}/设定/文风.md` 创建本书文风档案，由 `/nww-topic` 文风确定环节产出初稿，建书时落盘。文件结构：

```markdown
## 文风档案
- 语言风格：{白描/华丽/冷硬/幽默/沉郁/轻快/市井/文气}
- 叙事视角：{第一人称/第三人称限制/第三人称全知}
- 句式节奏：{短句为主/长短交错/长句舒缓}；段落长度倾向
- 对标参考：{主对标书或作者文风，仅作文风参考}
- 文风锚点片段：{2-3 段示范文字，体现本书笔调}
- 文风禁忌：{本书不能出现的腔调，如不能有解释腔/不能网络梗泛滥}
```

`设定/文风.md` 是 long-write Phase 4 写章与 deslop 去AI味的必读输入；缺失时 writer 退化为通用文风，deslop 无法做本书笔调校准。

### Step 1.5 · 角色命名注册（建书硬闸）

`bible.json` 中每个 `characters.name` 在建书阶段**必须**按顺序通过以下四拍；任一拍未完成即视为命名未过系统，**不得进入 Step 3 追踪初始化与后续写章**：

1. **定位先行**：先写清角色功能 / 身份 / 弧线（角色卡草案），禁止先名后凑设定（NR-001 规则 1）。
2. **规则校验**：主角过 `references/agent-references/naming-rules.md` 的 NR-001（无网名感 / 支持身份升级 / 非模板名），配角过 NR-002，势力/组织过 NR-005；逐条对照正例反例与毒点。
3. **取材落名**：从 `references/agent-references/naming-library.md` 对应题材档（都市/悬疑/干练感等）组合取名；避免撞名、高危谐音、主角名在正文高频出现拖节奏。
4. **登记校验（硬闸）**：姓名写入 `bible.json` 后立即跑 `nww_name_check.py --mode build --book {BOOK}`——**存在 ERROR 必须打回重命名，直到 0 ERROR 才允许继续**；WARNING（如缺 role/identity）按提示补齐后再开写。命名定稿作为建书 checkpoint，写入 `书根/.nww-named`（内容为提交 build 检查通过的姓名清单），供 Step 4 与后续审计核对。

### Step 1.5.5 · 角色性别登记（建书必填，2026-10-30）

> 根因实证（用泡面征服修仙界·林小凡）：角色卡无性别字段 → 代词性别机械闸
> （pronoun_gate）静默跳过 → 写手默认按男性代词写正文（第 1 章通篇「他」）。

每个角色的性别在建书阶段**必须双写**，缺一即视为登记未过：

1. `bible.json`：`characters[].gender` 必填，值域 `男 / 女 / 无性别`（`nww_bible_check.py` 已校验）；
2. 角色卡 `设定/角色/{角色名}.md`：**卡头 8 行内**必须含以下任一显式声明（机械闸只认头 8 行，写在卡尾不生效）：
   - 标准字段：`性别：男` / `性别：女`（全角/半角冒号、前置列表符号均可）
   - 旧标注：`女主` / `女主角` / `（女）`；`男主` / `男主角` / `（男）`
3. 同一卡内声明冲突（如 `性别：男` 与 `女主` 并存）= 未过闸，须统一为单一声明；
4. 建书后可跑 `python skills/nww-novel-skill/scripts/nww_gender_audit.py --book {BOOK}` 自检，输出缺失/冲突清单（WARNING 级，不阻断）。

### Step 1.5.6 · 代词与未注册配角写作规范（2026-10-30 外脑第四轮裁决）

配套机械闸：pronoun_gate 段落归属含 **Local_Actor 机制**——未注册实体（无角色卡、不入追踪
state 的无名配角）所在段落，其代词自动从主角统计池剔除。写作时遵守以下两条防呆规则：

1. **未注册配角出场，必须先实体名词、后动作**：严禁以无名代词「他/她」直接开启未注册
   配角的动作句（否则该句代词会被沿用归属到上一段主角名下，推高主角代词误计）。
   - 反例：`他蹲在墙根……`（前文未出现该角色时直接 BLOCK 风险）
   - 正例：`那散修蹲在墙根……` / `一个灰袍人蹲在墙根……`
2. **认知误认（如女扮男装被称「小兄弟」）**：对话引语内的误认称谓不参与代词闸（引语
   剥离）；揭晓身份的章节起，正文对该角色的代词必须切回真实性别；误认方若后续入卡，
   卡头 8 行照常登记性别，误认桥段不豁免其卡内声明。

书级扩展：书特殊代称（如专有身份词）可写入 `追踪/_tracking-state.json` 顶层可选字段
`local_actor_terms: ["词1", "词2"]`，与内置词表合并生效（已注册角色不进词表）。

> 【注意】male 主角书籍中，未注册配角与主角同段交错时，配角「他」可能计入主角
> 统计池，导致 right_p 虚高（2026-10-30 外脑第五轮裁决二：接受该弱化方向）。
> 写作时应严格遵守「未注册配角先实体名后动作」规范，通过拆段避免同段歧义。

## Step 1.6 · 底蕴资产（建书厚度硬要求，2026-10-25）

> 根因实证（仙门杂货铺书）：全书设定资产仅 3029 字 vs 三章细纲 14475 字——只有姜禾一张 507 字
> 散卡，配角无卡、势力无卡、商品无档案。writer 手里没有"人"只有"本集任务"，于是所有角色
> 按功能演出（审计结论「角色都在执行功能，而不是活着」）。本步骤把厚度做成**建书必产资产**。

**四条厚度机制**（写任何设定条目时逐条自检）：
1. **来历链**：谁造的/从哪来/花了多少/用了多久——每个势力、地图、道具、制度都要有"从哪来"。
2. **制度压顶**：先于主角存在、主角无法撼动的规矩，每条绑定明确代价（处罚梯度写死，禁止临场发挥）。
3. **他者终局**：每个配角入书前都已完成一件自己的大事（他凭什么坐今天这个位置）。
4. **时间分层**：设定文字里叠时间尺度（百年前/近年/昨天/此刻），世界不是为主角刚生成的。

**三类必产资产**（建书 checkpoint 前落盘，缺者按缺失计）：

1. `设定/角色/{角色名}.md` —— 按 `knowledge/character_card_schema.yaml` 12 层 schema 产出，
   **禁写 500 字散卡**。主角卡 ≥2000 字、CORE 层（identity 含 gender / behavior_constraint /
   language）必填，HIGH 层（personality 含 emotion_pattern / backstory 因果链 / arc /
   relationship）必填。配角（有台词的）也须有卡，≥800 字。其中三小节是写章合同直接消费项：
   - `personality.emotion_pattern`：默认应激方式 + **高压解除后的迟钝期写法**（手抖/反复确认/突然哭）；
   - `language.emotion_shifts`：紧张/愤怒/撒谎时语言如何退化（变短/重复/答非所问）；
   - `backstory.events`：事件→心理影响→行为习惯→未来触发 的因果链（不是流水账）。
2. `设定/势力/{势力名}.md` —— 每个入戏势力一张：沿革时间线（谁建/为什么/几次大事+距今年限）、
   制度压顶（规矩+代价梯度）、经济生态（物价锚：一块灵石对外门弟子意味着什么）、行话黑话、
   与其他势力的恩怨。没有势力的题材建 `设定/势力/地理与社群.md` 承载地图/社群来历。
3. `设定/商品/`（或 `设定/道具/`）—— 每件入戏商品/道具一个条目：来历链、功能与**限制**
   （"挡不住神识细查"这类限制写进档案，是剧情资源不是漏洞）、等级与解锁路径、每次重大使用的状态变化。

写章时这三类资产由写前包自动召回（`world_context` 动态发现 设定/ 子目录、`character_depth`
整卡注入），**写得越厚，注入越准**；反过来，建书偷懒的设定，写章时 writer 只能临场编。

## Step 2 · 部署 hooks + agents

把本包的 hooks 与 agents 接到运行时。优先 Claude Code：

```bash
# 写前阻断门 + 写后兜底网 + 跨批连续性 + 会话钩子
# 部署模式：拷 hooks/*.sh + node 共享核 + lib 到 {BOOK}/.claude/hooks/，
# 生成 hooks 注册模板合并进 {BOOK}/.claude/settings.local.json，写 .nww-deployed。
{PYTHON} {SKILL}/../scripts/merge-claude-settings.py \
    --project {BOOK} --hooks-dir {SKILL}/../hooks \
    --lib-dir {SKILL}/../scripts/lib --cli-dir {SKILL}/../scripts
# 同步 agents 到 {BOOK}/.claude/agents/
cp -r {SKILL}/../agents {BOOK}/.claude/agents

# V12.5：安装 git pre-commit 兜底闸（终极兜底，覆盖LLM完全不调用脚本的极端情况）
# 只要 git commit 正文文件，自动跑 nww_track_commit.py check，不通过拒绝commit。
# fail-open：非NWW项目/脚本不可用 → 静默放行，不阻塞正常commit。
mkdir -p {BOOK}/.git/hooks
cp {SKILL}/../hooks/pre-commit {BOOK}/.git/hooks/pre-commit
chmod +x {BOOK}/.git/hooks/pre-commit
```

- 若运行时是 OpenCode/Codex：`merge-codex-hooks.py` 生成 `.opencode/` / `.codex/` 等价配置；ZCode 3.3.4 不执行项目 custom agents → 降级 solo/direct（报告 `Fallback: ... -> solo`）。
- hooks 通过 `nww_hook_cli.js` 复用共享核（`tracking-checkpoint` / `prose-command-guard` / `prose-net` / `prose-toxic` / `continuity`）。node 缺席时对应面降级放行（fail-open 不反噬流程，但失去该面保护——须提示作者补装 Node）。

## Step 3 · 初始化追踪事务

```bash
{PYTHON} {SKILL}/../scripts/nww_track_commit.py init \
    --project {BOOK} --input {初始化事务.json}
```

`初始化事务.json` 结构见 `references/tracking-transaction.md`（schema_version=1，last_chapter=0 新书；导入已有书则 last_chapter=N）。`init` 只在 `_tracking-state.json` 不存在时执行，绝不覆盖。

**全局运行时状态（global，千章长篇核心）**：初始化事务可传入 `"global": {}`（空对象，表示尚未写入全局状态），或直接传入初始 global 结构（推荐含 `position`/`world`/`factions`/`next_trigger` 四个子段）。`global` 是作品当前运行时状态的唯一全局快照——只记录「现在已经变成什么」，不记录设定（设定文件的事）和未来（大纲的事）。后续每章通过逐章事务的 `global_change` 字段增量更新（deep-merge），未提及的字段保持不变。短篇/不需要世界演化追踪的项目可始终保持 `global: {}`，不影响原有流程。

## Step 4 · 校验

```bash
{PYTHON} {SKILL}/../scripts/nww_track_commit.py check --project {BOOK}
{PYTHON} {SKILL}/../scripts/nww_bible_check.py --book {BOOK}
{PYTHON} {SKILL}/../scripts/nww_name_check.py --mode build --book {BOOK}
test -f {BOOK}/设定/文风.md && echo "文风文件: OK" || echo "WARNING: 缺失 设定/文风.md"
```

`nww_track_commit.py check` 必须 `VERDICT: APPROVE` / 无 `derived view differs` 才算部署完成。`nww_bible_check.py` 校验 bible.json 契约（角色/代价系统/签名比喻/反派败相必填字段，防下游静默失效）：**存在 ERROR 须修复后才能开写**；WARNING 仅提示（如角色缺致命弱点/动机锚点、代价模板缺失、platform/scale 字段缺失），不阻断。
`nww_name_check.py --mode build` 校验命名规则（重名/缺名/高危确定性谐音）：**存在 ERROR 须修复后才能开写**；WARNING（如角色缺 role/identity）仅提示，不阻断。**此命令即 Step 1.5 命名硬闸的复核点：Step 1.5 未落 `.nww-named` 或 build 检查非 0 ERROR 的书，禁止进入 Step 3 的 init 与任何写章动作。**
`设定/文风.md` 为建书标配文件：缺失时仅 WARNING 不阻断，但 long-write 与 deslop 将退化为通用文风，建议建书时补齐。

## 钩子职责速查

| hook | 面 | 作用 |
|---|---|---|
| `guard-outline-before-prose.sh` | PreToolUse(Write/Edit/Bash) | 写正文前强制先有细纲 + 追踪检查点门 |
| `check-prose-after-write.sh` | PostToolUse(Write/Edit) | 截断/工程词/复读/毒句式兜底 |
| `check-naming-after-write.sh` | PostToolUse(Write/Edit) | 正文落盘后命名引用一致性 + 疑似新角色名（WARNING advisory） |
| `detect-story-gaps.sh` | 跨批 | 缺口 + 跨批连续性 |
| `pre-compact.sh` / `post-compact.sh` | 压缩 | 续写状态卡保鲜 |
| `session-start.sh` / `session-end.sh` / `validate-story-commit.sh` | 会话 | 提交校验 |

## 部署后标记

在 `{BOOK}/.nww-deployed` 写 `agents_version: 25`（与 `deslop` 对齐），供各 skill 探测 agent 包版本。

> 详细协议与事务 JSON 见 `references/tracking-transaction.md`、`references/state-tracking.md`。
