---
name: nww-novel-skill
version: 1.2.0
description: |
  NWW-V12 网文生成总控 skill（纯 skill 模式）。把 V12 管线重构为 Claude Code 插件式 skill 包：
  千章不崩（追踪事务 + 一致性检查 + 写前/写后确定性门禁）+ 产文质量（去AI味 deslop）+ V12 生成方法论。
  v1.1.0 新增：I-I 细纲承诺兑现度审稿维度（承诺-兑现结构化验收门）+ 细纲「本章收益兑现」必填字段。
  v1.2.0 新增：细纲「本章目标」统率字段（收编收益兑现/章尾钩子）+ 节拍目标可验收规则 + 铺陈删除测试（金手指×身份耦合）+ 导播式节拍范式。
  路由：开新项目→/nww-setup；选题策划→/nww-topic；写章→/nww-long-write；去AI味→/nww-deslop；一致性审查→/nww-consistency。

---

# NWW-V12 — 网文生成总控

把「Python 管线」重构为「Skill + Agent + 确定性脚本 + Hooks」的插件包。生成由 LLM 按本包方法论驱动，确定性护栏走脚本与 hooks（模型无关、fail-closed）。

## 包内路由

| 你要做什么 | 调哪个 skill | 说明 |
|---|---|---|
| 开一本新书 / 部署 hooks+agents | `skills/setup` (`/nww-setup`) | 初始化 `设定/大纲/正文/追踪` 布局 + 装门禁 |
| 选题策划（想题材/评估市场） | `skills/topic` (`/nww-topic`) | 市场分析 + 3–5 个选题 + 金手指/卖点/可行性评估 |
| 扫榜（市场扫描/选题决策） | `skills/scan` (`/nww-scan`) | 平台榜单采集 + 扫榜报告 + `选题决策.md`（能爆原因先当假设，拆文后回填） |
| 写前对齐（评分前置） | `skills/prealign` (`/nww-prealign`) | 结构层检查缺钩子/绑定/落差/卡点，缺则回细纲补（long-write Phase 1 内嵌） |
| 写一章 | `skills/long-write` (`/nww-long-write`) | V12 硬事实锚 + 必种件 + 写后机械层 + 追踪提交 |
| 给已有正文去 AI 味 | `skills/deslop` (`/nww-deslop`) | 七门禁 + 三遍法 + 3 个确定性脚本 |
| 审一致性（设定/时间线/伏笔） | `skills/consistency` (`/nww-consistency`) | 只读 S1–S4 冲突报告 |
| 90分评分（卷/整本） | `skills/score90` (`/nww-score90`) | 外部 E0-E5 定调 + 内部实证，输出综合分与90分底线判定 |
| 章节动机修订 | `skills/revision` (`/nww-revision`) | 诊断行动跳跃/选前压力/选后代价，只增不删局部修订（vE1，默认不激活，显式触发） |
| 拆文（对标拆解） | `skills/analyze` (`/nww-analyze`) | 深度拆文管道 Stage 0-6，产物落盘 `拆文库/{书名}/`（节奏/情绪模块/文风），供对标锚消费 |

## 书项目布局（纯 skill 新书统一用此布局）

```
projects/{book}/
  设定/
    角色/{角色名}.md
    世界观/{力量体系,地名}.md
  大纲/
    卷纲_第1卷.md ...          # 每卷一份（卷纲.md 单文件卷的老书亦兼容）
    细纲_第001章.md ...       # 每章一份细纲（= V12 的 blueprint；命名须为 细纲_第N章.md）
  正文/
    第一卷/第001章_章名.md ... # 成章产物，推荐按卷分子目录（正文/{卷名}/）
                               # 旧书平铺 正文/第001章_章名.md 同样被脚本兼容
  追踪/
    _tracking-state.json      # 唯一权威状态（nww_track_commit.py 管理）
    上下文.md / 角色状态/ / 伏笔.md / 时间线/
```

> 既有 `projects/{book}/chapters/NN/final.md + state.json + bible.json`（V12 旧管线产物）作为 legacy 保留，不强行迁移；新书走上面布局。

## 确定性护栏（模型无关）

- **写前阻断门** `hooks/guard-outline-before-prose.sh`：强制先有细纲 + 追踪检查点（顺序/state 一致性），否则拦下写正文。
- **写后兜底网** `hooks/check-prose-after-write.sh`：截断/工程词/复读/毒句式，模型无关。
- **跨批连续性** `hooks/detect-story-gaps.sh` + `nww_hook_cli.js continuity`。
- **V12 机械层** `scripts/nww_enforce.py`：负向词删除 + 代价不可逆修复 + crack meter 纠偏 + 必种件兜底 + 主角境界守卫（只补不压）。
- **追踪事务** `scripts/nww_track_commit.py`：单提交点、锁、派生视图重渲染校验（`check`），保证千章不崩。

## 与旧 Python 管线的关系

V12 早期以 `server/`（Python 管线：scene_writer.py / rule_checker.py / assembler / llm / storage）实现，并配 `experiments/` 离线调参/对照脚本（直接调本地模型端点）。该套代码**不属本 skill 包、不与运行时耦合**，已于 2026-08-29 整体归档至 `_archive/server-legacy-20260829/` 与 `_archive/experiments-legacy-20260829/`（仅作研究历史留存，不再视为 skill 功能或设计）。本包**自洽运行、不 import 任何模型客户端**；运行时模型即宿主 LLM（Claude），skill 只写"如何指挥它"的方法论与确定性护栏。
