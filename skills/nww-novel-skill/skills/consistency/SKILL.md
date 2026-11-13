***

name: nww-consistency
version: 1.0.0
description: |
NWW-V12 一致性审查（千章不崩子系统之一）。只读调用 consistency-checker agent，检测设定矛盾/时间线冲突/
伏笔断线/角色属性不一致/规则边界悖论/代价一致性，输出 S1-S4 分级报告。不做创作判断。
触发：/nww-consistency、/一致性、「审一下这章连贯性」。
metadata: {"openclaw":{"source":"nww-novel-skill consistency-checker + NWW-V12 适配"}}
-----------------------------------------------------------------------------------------

# nww-consistency — 一致性审查

只读检查，不修改任何文件，不做创作判断。千章不崩的「事实层兜底」。

## 调用协议

用 `Agent(subagent_type: "consistency-checker")` 调 `{SKILL}/../agents/consistency-checker.md`。该 agent 已声明 `tools: [Read, Glob, Grep]`、`disallowedTools: [Write, Edit, Bash]`、`model: haiku`，纯只读。

## 检查前准备（主会话做）

1. 跑 `nww_track_commit.py check --project {BOOK}`，拿到 `last_committed_chapter` 与 `state_revision`。
2. 把这两个值随 prompt 传给 agent（完整 `_tracking-state.json` 不进 prompt，读取量不随章数增长）。
3. 提示 agent：先扫 `设定/角色/`、`设定/世界观/`、`大纲/细纲_*.md`、`追踪/上下文.md`、`追踪/伏笔.md`、`追踪/角色状态/`、`追踪/时间线/`，动态构建检查词表（不硬编码题材术语）。

## 输出格式（S1–S4）

```
VERDICT: APPROVE / CONCERNS / REJECT
CONFLICTS:
- [S1] 硬矛盾：文件:正文/第NNN章.md:行
- [S2] 隐性矛盾（因果/代价/位置）:文件:...
- [S3] 细节不一致（数字/外貌）:文件:...
- [S4] 伏笔超期/密度建议/格式不统一:文件:追踪/伏笔.md
- [S2][rule_boundary] 前提/规则：...；触发事件：...；矛盾点：...；需裁决：...
```

## 分级处理

- **S1 (Critical)**：直接矛盾硬伤 → 必须修，回炉对应章。

- **S2 (Major)**：隐性矛盾破坏叙事逻辑 → 必须修。

  - 子类·高吸引力伏笔搁置：`importance=高` 且播种时即高期待的伏笔，跨 1 个大卷仍无正文触碰/回应、又无规划层「交代方向/回收节点」登记 → 触发 S2。修复动作＝要求规划层登记交代方向（允许 `planned_resolution_chapter=null`，不强制填数值，不自动改伏笔状态）；S4「伏笔超期>50章」扫描须排除 importance=高，避免双报；另须排除 `core_hook=true` 的伏笔（其超期由 track_commit 的 FORESHADOW DEBT WARNING 按半衰期接管，S4 再报即双报）。

- **S3 (Minor)**：细节不一致 → 建议修。

- **S4 (Advisory)**：伏笔超期/密度/格式 → 记待办，非阻断。

## 核验闭环（S1/S2 不闭环不提交）

审查输出后，用 `nww-setup/references/agent-references/consistency-closure.md` 模板对 S1/S2 冲突做闭环核验：

- **闭环方式**：修复（改正文/细纲/设定，重跑一致性确认冲突消失）或人工裁决（记录裁决理由，作者/编排者确认）。

- **落盘闭环记录（A-3 机械闸输入）**：闭环核验完成后，把机器可读记录写入 `{BOOK}/追踪/一致性核验/第{N}章.json`：

  ```json
  { "chapter": 5, "closed": true, "open_s1_s2": [], "updated_at": "2026-08-28T12:00:00+08:00" }
  ```

  - `open_s1_s2`：未闭环冲突列表，`[]` 表示全部已修复/已裁决；`closed` 为 false 或列表中非空 = 未闭环。

  - 人工裁决同样须落盘（open\_s1\_s2 为 \[]，裁决理由记入裁决记录）。

- **提交闸门**：存在未闭环的 S1/S2 → 禁止进入 `nww_track_commit.py commit`；全部 S1/S2 已「已修复」或「已裁决」才允许提交。

  - 机械侧：`nww_track_commit.py commit` 读上述闭环记录。`policy.fail_closed=true`（缺省，Task 2）或 `commit_gates.consistency=true`（新书模板默认）时缺记录/未闭环 → 拒绝 commit；存量书未开启 → 仅 WARNING 不阻断（兼容迁移模式，迁移完成后再置 true）。

- **带伤提交 override（Task 8 / C-P1-3，禁止静默放行）**：确需带着未闭环 S1/S2 提交（如作者拍板下章修），必须双落盘，缺一即仍 BLOCK：
  1. 闭环记录内嵌 `"override": {"gate":"consistency","reason":"…","expires_at":"2026-11-01T00:00:00+08:00"}`（与该章冲突绑定；未过期才放行，放行时 commit 输出 `[A-3 OVERRIDE]` 留痕）；
  2. 审计台账追加一行：`python3 skills/nww-novel-skill/scripts/nww_override_audit.py add --book <书根> --gate consistency --chapter N --reason '…' --expires 2026-11-01`（只增不改写 `追踪/overrides.jsonl`，含 actor/ts，供 `nww_override_audit.py list` 定期审计）。
  过期 override 自动失效（视为无 override，恢复 BLOCK）。

- S3/S4 记待办，不阻断。

## 边界

- 不做「写得好不好」的创作判断（那是 narrative-writer / nww-architect 的活）。

- 不做修改建议（只报冲突事实）。

> 详细检查清单见 `agents/consistency-checker.md` 与 `references/state-tracking.md`。

