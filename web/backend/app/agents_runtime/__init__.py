"""agents_runtime 包 — 后端原生 Agent 运行时（2026-09-10 新增，纯增量，零耦合）。

把 skills/nww-novel-skill 的多环节独立 agent 体系搬进 WebUI 后端：
  - registry：扫描 skills/nww-novel-skill/agents/*.md → 各 agent 定义（method/模型档位）
  - toolbox：确定性护栏脚本执行器（subprocess，白名单，fail-closed，注入用户书根）
  - runtime：单 agent 执行器（agent 方法论文档 → system prompt → 模型路由调用）
  - pipeline：写章闭环编排（packet → writer → delta → post_write → deslop）

约束（遵守项目铁则）：
  - 不改任何 skill 脚本 / 原 engine 脚本；方法论文档原文读取，零复制
  - 所有 subprocess 注入 {user_root} 绝对路径，杜绝跨用户访问
  - LLM 只产出文本，落盘/脚本调度全部由 pipeline 控制（fail-closed）
"""