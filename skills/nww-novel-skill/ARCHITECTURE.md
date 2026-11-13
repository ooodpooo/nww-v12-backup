# NWW-V12 架构文档（ARCHITECTURE）

> 适用范围：`skills/nww-novel-skill/` 插件包（Claude Code / OpenCode / Codex 可加载）。
> 版本：1.0.0。最后更新：2026-09-06（新增市场与对标子系统：scan/analyze/对标锚旁路）。上次更新：2026-08-29（旧 Python 管线 `server/` 与 `experiments/` 已归档，见文末）。

---

## 1. 核心设计原则

| 原则 | 含义 |
|---|---|
| **Skill 驱动，模型无关** | 生成由宿主 LLM（Claude/兼容运行时）按本包方法论驱动；本包**不 import 任何模型客户端、不发起任何 HTTP 模型请求**。 |
| **确定性护栏优先** | 所有连续性、命名、AI 味、追踪一致性由 bash/python 确定性脚本与 hooks 兜底（fail-closed），**绝不信任模型自带的连续性**。 |
| **失败即阻断（fail-closed）** | 机械层修复失败宁可丢泄漏末段 / 拦下写作，也不依赖 LLM 重生成兜底。 |
| **可审计的状态单一权威** | 千章不崩依赖一个权威状态文件 + 派生视图重渲染校验，不靠模型记忆。 |

> 关键事实：运行时"模型"就是加载本 skill 的宿主 LLM 本身。skill 只写"如何指挥它"的方法论与护栏，因此**不存在 skill 直接调用模型的情况**。早期 `server/llm.py`（写死本地端点 `127.0.0.1:8000`）与 `experiments/` 离线调参脚本已于 2026-08-29 整体归档至 `_archive/`，不再属于本包。

---

## 2. 分层架构

```
┌──────────────────────────────────────────────────────────────┐
│  宿主运行时（Claude / OpenCode / Codex）— 即"模型"            │
│  读取 SKILL.md + 子 skill + agents + references，驱动写作       │
└──────────────────────────────────────────────────────────────┘
        │ 调用
────────────────────────────────────────────────────────────────
├─ ① 路由层        SKILL.md + skills/*（setup/topic/prealign/    │
│                  long-write/deslop/consistency/score90/       │
│                  revision/rhythm/analyze/scan）               │
├─ ② Agent 层      agents/*.md（subagent 声明 + model 指派）     │
│                  consistency-checker / narrative-writer /     │
│                  nww-architect / nww-researcher / ...          │
├─ ③ 确定性脚本层  scripts/*.py + scripts/*.js（模型无关）        │
│                  nww_enforce / nww_track_commit / nww_wordcount│
│                  / nww_hook_core / check-degeneration ...      │
├─ ④ Hooks 门禁层  hooks/*.sh（写前/写后确定性拦截）              │
│                  guard-outline-before-prose / check-prose-     │
│                  after-write / detect-story-gaps ...           │
├─ ⑤ 知识层        references/（laws.yaml / banned-words /       │
│                  anti-ai-writing / style_exemplars /          │
│                  negative_patterns / tracking-transaction）   │
└────────────────────────────────────────────────────────────────
```

### ① 路由层（总控）
`skills/nww-novel-skill/SKILL.md` 定义包内路由表，把用户意图分发到子 skill。子 skill 各自独立、可单独触发（如 `/nww-long-write`、`/nww-deslop`）。当前 11 个子 skill：`setup` / `topic` / `prealign` / `long-write` / `deslop` / `consistency` / `score90` / `revision` / `rhythm`（`.nrl.enabled` 时写章内嵌启用）/ `analyze`（拆文管道）/ `scan`（扫榜）。其中 `analyze`、`scan` 与 long-write 内的对标锚旁路组成本包「市场与对标子系统」，与写章/一致性/追踪链路零耦合。

### ② Agent 层（子代理）
`agents/` 下 7 个 agent 定义（`.md` frontmatter 声明 `model:` 与 `tools:`/`disallowedTools:`）：
- `nww-architect`(opus) / `nww-researcher`(sonnet) / `nww-character-designer`(sonnet) / `narrative-writer`(sonnet) / `nww-explorer`(haiku) / `nww-chapter-extractor`(haiku) / `consistency-checker`(haiku，纯只读 `Read,Glob,Grep`，禁 `Write,Edit,Bash`)。
- Agent 的 `model:` 只是告诉运行时"用哪个子代理模型"，属于运行时调度配置，不是 skill 自身发起模型请求。

### ③ 确定性脚本层（护栏主体）
`scripts/` 下纯 python/node 工具，**无模型依赖、可独立运行**：
- `nww_enforce.py` — V12 机械执行层：负向词删除 + 代价不可逆修复 + crack meter 纠偏 + 必种件兜底 + 主角境界守卫（只补不压）。
- `nww_track_commit.py` — 追踪事务：单提交点、锁、派生视图重渲染校验（`check`），千章不崩核心。
- `nww_wordcount.py` / `nww_ctl.py`（写中/写后字数闭环）/ `nww_name_check.py` / `nww_bible_check.py` / `nww_sequence_audit.py` / `nww_memory_pack.py` — 字数/命名/bible/序列/记忆包校验。
- `nww_benchmark_recall.py` — 对标锚召回（可选旁路，默认关闭）：`.benchmark.enabled` 存在时读 `拆文库/` + 题材卡，输出【对标锚】advisory 段；无标记文件零注入。
- `nww_hook_core.js` / `nww_hook_cli.js` — hooks 共享核（写前阻断判定、跨批连续性）。
- `check-degeneration.js` / `check-ai-patterns.js` / `check-outline-copy.js` / `normalize-punctuation.js` — AI 味/退化指纹/大纲抄袭/标点归一化检测。
- `merge-claude-settings.py` / `merge-codex-hooks.py` — 部署时将 hooks/agents 注册进运行时配置。

### ④ Hooks 门禁层（写前/写后拦截）
`hooks/*.sh` 由运行时在 Write/Edit/commit/compact 等时机触发：
- `guard-outline-before-prose.sh` — **写前阻断**：无细纲 + 追踪检查点（顺序/state 一致）则拦下写正文。
- `check-prose-after-write.sh` — **写后兜底**：截断/工程词/复读/毒句式（模型无关）。
- `detect-story-gaps.sh` + `nww_hook_cli.js continuity` — 跨批连续性。
- `check-naming-after-write.sh` — 命名规则门禁。
- `validate-story-commit.sh` / `session-start|end.sh` / `pre|post-compact.sh` — 提交校验、会话/压缩期状态看护。

### ⑤ 知识层（方法论来源）
`references/` 提供机器可读的规律与范例：
- `laws.yaml` — 素材规律（命名须为 `细纲_第N章.md` 等）。
- `banned-words.md` / `negative_patterns.yaml` — 负向词与毒句式。
- `anti-ai-writing.md` — 去 AI 味问题模式目录。
- `style_exemplars/`、`references/agent-references/genre-prose-cards.md`（题材卡索引）+ `references/agent-references/genre-prose-cards/`（单卡目录，32 张题材卡）— 文风范例与题材技法卡。
- `tracking-transaction.md` — 追踪事务协议。
- `agent-references/` — 各 agent 的参考输入（含 `topic-options.md` 等）。

---

## 3. 书项目布局（纯 skill 新书统一结构）

```
projects/{book}/
  设定/
    角色/{角色名}.md
    世界观/{力量体系,地名}.md
  大纲/
    卷纲_第1卷.md ...          # 每卷一份
    细纲_第001章.md ...         # 每章一份（= V12 blueprint；命名须为 细纲_第N章.md）
  正文/
    第一卷/第001章_章名.md ...  # 成章产物（旧书平铺 正文/第001章_章名.md 亦兼容）
  追踪/
    _tracking-state.json       # 唯一权威状态（nww_track_commit.py 管理）
    上下文.md / 角色状态/ / 伏笔.md / 时间线/
```

> 既有 `projects/{book}/chapters/NN/final.md + state.json + bible.json`（V12 旧管线产物）作为 legacy 保留，不强行迁移。

> 对标的拆文库（`拆文库/{书名}/`：节奏/情绪模块/文风/拆文报告/_progress）为**仓库级目录**，不属于书项目的 `projects/{book}/` 布局；写章对标锚旁路只读它，不写入。

---

## 4. 关键机制

### 4.1 写一章的数据流（long-write）
```
细纲 → [Phase 1 写前对齐 prealign：钩子/绑定/落差/卡点缺则回细纲补]
     → 构造硬事实锚（数字/人名/境界/代价刻度锁死进 prompt）
     → LLM 按细纲写章（纯正文，不写标题行/围栏）
     → nww_enforce 机械层（必种件兜底/代价修复/crack 纠偏/境界守卫）
     → 去 AI 味（deslop：七门禁 + 三遍法）
     → check-prose-after-write 写后门禁
     → 一致性检查（consistency agent，只读）
     → nww_track_commit 追踪事务提交（_tracking-state.json 单一权威）
     → 摘要 + 状态 deep-merge
```

### 4.2 千章不崩（追踪事务）
权威状态 `_tracking-state.json` 由 `nww_track_commit.py` 管理：单提交点、加锁、派生视图（角色状态/伏笔/时间线）重渲染校验。写前 `guard-outline-before-prose` 强制先有细纲与检查点；写后 `detect-story-gaps` 查跨批断裂。模型不参与连续性保证。

### 4.3 产文质量（deslop）
`nww-deslop` 子 skill：七门禁 + 三遍法 + 3 个确定性脚本（`check-degeneration.js` 等）。模型只做"改写自然"，判定与兜底走确定性层；命中硬规则即重写，tier2 仅提示交人/LLM 判。

### 4.4 评分体系（score90，模型无关）
`nww-score90`：外部维度由运行 skill 的 Agent 按 `quality/rubric_90score_v3.md` 执行；内部证据（I-A~I-F）由确定性脚本 `quality/detect_internal.py` 提供。卷/整本评分，不对单章拦截。

### 4.5 运行时兼容
按 `.claude/agents/{agent}.md` → `.opencode/agents/{agent}.md` → `.codex/agents/{agent}.toml` 顺序查找 agent；若无 custom agent registry（如 ZCode），降级为 solo/direct 并报告 `Fallback: ... -> solo`。

### 4.6 市场与对标子系统（scan / analyze / 对标锚，零耦合独立模块）

```
/nww-scan ──► 平台榜单采集脚本（起点/番茄/七猫/晋江/刺猬猫，SSR 优先）
   │          └─► 扫榜报告.md → 选题决策.md（「能爆的原因」先当假设，标 待拆文验证）
   ▼
/nww-analyze ─► 拆文管道 Stage 0-6（Stage 2 spawn chapter-extractor 并行提取）
   │           └─► 拆文库/{书名}/（剧情/节奏.md、剧情/情绪模块.md、文风.md、拆文报告.md、_progress.md）
   │               └─► Stage 5 把拆解支撑回填选题决策.md 的 待拆文验证 项
   ▼
写章旁路 ──► {BOOK}/.benchmark.enabled 存在时
              nww_benchmark_recall.py 召回【对标锚】（题材卡 + 情绪模块 + 节奏 + 文风，advisory）
              └─► 并入 nww-long-write Phase 1 写作 prompt
```

- **独立性**：该三模块只读 `拆文库/` 与题材卡，不触碰 `_tracking-state.json`、hooks、deslop/consistency 链路；标记录入仅 `plugin.json` 注册 + SKILL.md 路由。
- **开关**：对标锚是写章旁路，`.benchmark.enabled` 缺失即零注入（零行为变化）；`scan`/`analyze` 为独立触发 skill，不进入写章主链路。
- **产物目录**：`拆文库/{书名}/` 为仓库级目录（不在 `projects/{book}/` 内），写作侧按需只读。
- **复用**：Stage 2 逐章提取复用 `nww-chapter-extractor`（haiku，质量失败 sonnet 升级重试 1 次，agent 不可用降级串行）。

---

## 5. 与旧 Python 管线的关系（已归档）

V12 早期实现 `server/`（scene_writer / rule_checker / assembler / llm / storage）与 `experiments/`（离线调参/对照，直接调本地模型端点）。该套代码**不属本 skill 包、不与运行时耦合**，已于 2026-08-29 归档：
- `_archive/server-legacy-20260829/`
- `_archive/experiments-legacy-20260829/`

本包自洽运行、不依赖上述代码。校准数据（`experiments/results/E1`、`kimi_run` 产物）随实验归档，由 `quality/run_calibration.py` 从 `_archive/` 读取，质量体系仍可用。

---

## 6. 目录速查

```
skills/nww-novel-skill/
├── SKILL.md                 # 总控 + 路由表
├── plugin.json              # 插件元信息
├── agents/                  # 7 个子代理定义（含 model 指派）
├── skills/                  # 11 个子 skill（setup/topic/prealign/long-write/
│                           #   deslop/consistency/score90/revision/rhythm/
│                           #   analyze/scan）
├── scripts/                 # 确定性脚本（py/js，模型无关）
├── hooks/                   # 写前/写后门禁（sh）
├── references/              # 知识层（laws/banned-words/anti-ai/style_exemplars）
├── ARCHITECTURE.md          # 本文
└── PRODUCT.md               # 产品说明

拆文库/{书名}/               # 仓库级：analyze 产物（节奏/情绪模块/文风），benchmark 召回只读
```
