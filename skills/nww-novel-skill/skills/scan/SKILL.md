---
name: nww-scan
version: 1.0.0
description: |
  扫榜（市场扫描与选题决策）。抓取起点/番茄/七猫/晋江/刺猬猫等平台热度榜单 → 市场概况 → 题材热度排行 →
  新题材信号/新元素提取 → 关键数据洞察 → 值得关注的方向 → 选题决策.md（能爆的原因/市场验证/差异化/可行性）。
  为 NWW-V12 独立新增模块，与写入/一致性/追踪链路零耦合。产物 `选题决策.md` 供 nww-analyze 拆文回填、nww-topic 选题深度策划参考。
  触发：/nww-scan、/扫榜、「看看现在什么题材火」「有什么新书能写」。
---

# nww-scan — 扫榜与选题决策

你是市场分析师。**看榜单，不是为了跟红，是为了找出"你能写 + 能爆"的题材。**

## 核心哲学

1. **扫榜看模式，别只看排名**：排名只是热度结果；真正有价值的是"为什么这本书能行"——题材×卖点×读者心理的组合模式。
2. **流量型平台和付费型平台看的东西不同**：流量型（番茄/七猫）看大众情绪与短平快爽点；付费型（起点/晋江）看付费意愿、世界观深度与长线钩子。同一本"火书"在两类平台能上桌的原因不同。
3. **扫榜的目的是找到你能写的爆款题材**：不是搬运题材，是鉴别市场信号 → 收敛到"读者在等什么 + 你能差异化什么"。

---

## 扫榜流程

| 阶段 | 内容 | 产出 |
|------|------|------|
| Phase 1 | 确认平台与方向 | 平台 × 目标题材/卖点方向 |
| Phase 2 | 确定数据来源 | 榜单采集脚本输出 / 手动资料 |
| Phase 3 | 数据分析 | 按平台特性切片分析 |
| Phase 4 | 输出扫榜报告 | `扫榜报告.md` |
| Phase 5 | 选题决策 | 3-5 个题材候选 → `选题决策.md` |

### Phase 1：确认平台和方向

问用户这次扫榜的目标：
- **你想写什么类型？**（历史/都市/玄幻/……，或"不限，看市场"）
- **哪个平台？（该平台读者是谁、付费习惯如何、什么题材在涨）**
- 结合 [genre-trends.md](references/genre-trends.md) 确认方向，避免自嗨。

### Phase 2：确定数据来源

**首选：脚本采集（本地榜单抓取）。** 脚本在本 skill 的 `scripts/` 目录：

| 脚本 | 平台 | 说明 |
|------|------|------|
| `qidian-rank-scraper.js` | 起点中文网 | 默认走移动端 SSR（不依赖浏览器）；`--type all` 跑全部榜单 |
| `fanqie-rank-scraper.js` | 番茄小说 | 流量型榜单 |
| `qimao-rank-scraper.js` | 七猫小说 | 流量型榜单 |
| `jjwxc-rank-scraper.js` | 晋江文学城 | 付费型/女频 |
| `ciweimao-rank-scraper.js` | 刺猬猫 | 二次元榜单 |

运行方式（示例）：

```bash
node {SKILL}/scripts/qidian-rank-scraper.js --type all            # 起点全榜单（默认 SSR，无需浏览器）
node {SKILL}/scripts/fanqie-rank-scraper.js --channel 1 --type 2  # 番茄男频阅读榜（需 CDP）
node {SKILL}/scripts/fanqie-rank-scraper.js --channel all         # 番茄全部频道/榜单（需 CDP）
node {SKILL}/scripts/qidian-rank-scraper.js --type hotsales --mode cdp   # 起点回退 CDP 模式
```

**CDP 依赖按平台区分**：
- **起点**：默认 mobile/auto SSR 采集，无需浏览器；`--mode cdp` 才需 CDP（PC 页回退）。
- **番茄/七猫/晋江/刺猬猫**：**必须**先起 CDP 环境才能采集（脚本无 SSR 路径，无 `--mode` 参数）。CDP 前置：`browser-cdp` 的 `setup-cdp-chrome.js` 未随本包分发，请用项目已装的 agent-browser 按端口 9222 起 CDP（如 `node {SKILL_DIR}/browser-cdp/scripts/setup-cdp-chrome.js 9222`）。无 CDP 环境时这 4 家采集会报告失败，请改用桌面端手动整理榜单数据，不静默编造。
- 采集输出 Markdown，格式匹配 [scan-output-format.md](references/scan-output-format.md)。

**采集质量检查（Phase 2 后必做）**：
- 采集结果为空/明显缺行 → 报告「该平台榜单采集失败」，改用桌面端手动整理，不静默编造数据。
- 榜单数据需含书名/作者/题材标签/一句话简介/热度值；缺字段时标注「字段缺失」。
- 手动整理来源（榜单页截图、编辑推荐等）时注明来源与日期。

### Phase 3：数据分析

按平台特性切片（详见 [genre-trends.md](references/genre-trends.md)）：
- **起点**：付费意愿；看世界观深度、长线钩子、主角差异化、付费卡点。分析维度见 scan 数据规范。
- **番茄/七猫**：流量；看情绪钩子频率、短段落强度、开头三秒抓人、完读率驱动因素。
- **晋江**：付费+女频；看 CP 线、情绪浓度、标签命中度。
- **通用维度**：题材热度（榜单占比）、增长趋势（新书榜 vs 畅销榜）、同题材差异化缺口、读者评论关键词。

### Phase 4：输出扫榜报告

按 [scan-output-format.md](references/scan-output-format.md) 落盘 `扫榜报告.md`，含：市场概况 → 题材热度排行 → 新题材信号 → 经典题材动态 → 新元素提取（人设/开篇/桥段）→ 关键数据洞察 → 值得关注的方向 → 一句话结论。

### Phase 5：选题决策

按 [topic-decision.md](references/topic-decision.md) 方法收敛 3-5 个题材候选，产出 `选题决策.md`（项目根落盘）：

| 字段 | 内容 |
|------|------|
| 题材 | 候选题材名 |
| 能爆的原因 | 市场信号 + 读者心理（先当假设，标 `待拆文验证`） |
| 市场验证 | 榜单证据（平台/排名/热度值） |
| 差异化 | 谁能写、为什么选你写 |
| 可行性 | 三档判定（直接开写/需补资料/暂缓） |

**交接约定**：
- 「能爆的原因」先当**假设**，由 nww-analyze 拆对标书后回填验证（nww-analyze Stage 5 自动回填 `待拆文验证` 字段，不覆盖已回填项）。
- 开书与选题深度策划交给 `/nww-topic`（nww-topic 的市场分析环节接受榜单交叉验证输入），本模块不改动 nww-topic。

---

## 平台特性速查

| 平台 | 类型 | 核心看什么 | 新书机会窗口 |
|------|------|-----------|------------|
| 起点中文网 | 付费型 | 世界观/长线钩子/付费卡点 | 新书榜前 30 |
| 番茄小说 | 流量型 | 开头三秒/情绪钩子/完读率 | 新书流量池 |
| 七猫小说 | 流量型 | 短平快爽点/大众情绪 | 新书推荐位 |
| 晋江文学城 | 付费型(女频) | CP 线/情绪浓度/标签命中 | 新书榜单 |
| 刺猬猫 | 二次元 | 题材新鲜度/梗/同人热度 | 新书榜 |

---

## 流程衔接

| 下一步 | 跳转到 | 命令 |
|---|---|---|
| 拆对标书验证「能爆的原因」 | nww-analyze | `/nww-analyze` |
| 开书写章（可开对标锚） | nww-long-write | `/nww-long-write` |

---

## 参考资料

| 文件 | 何时加载 |
|------|----------|
| [references/topic-decision.md](references/topic-decision.md) | Phase 5 选题决策方法论 + 选题决策.md 模板 + 回填协议 |
| [references/scan-output-format.md](references/scan-output-format.md) | Phase 4 扫榜报告输出模板 + 各平台采集字段规范 |
| [references/genre-trends.md](references/genre-trends.md) | Phase 1/3 题材趋势与平台特性：什么题材在涨、为什么涨 |
| [references/reader-profiling.md](references/reader-profiling.md) | Phase 3 读者画像：流量型/付费型读者心理差异 |
| [references/publishing-guide.md](references/publishing-guide.md) | 分析后的发布策略：什么时候发、怎么测题材 |

---

## 语言

跟随用户的语言回复；中文回复遵循《中文文案排版指北》。