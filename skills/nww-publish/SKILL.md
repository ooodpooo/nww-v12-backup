---
name: nww-publish
description: 网文平台（起点/番茄）定时自动发书助手。把「每章一个 .md」的书稿按计划时间自动发布到作家后台，支持手动扫码登录一次后持久会话、进度跟踪与断点续发。由任意 IDE 工具（WorkBuddy / Claude Code / Cursor / opencode）通过 CLI 调用。当用户说「定时发书」「自动发布章节」「起点/番茄 发书」「发文字到作家后台」「补发章节」「查发布进度」时使用。
---

# nww-publish · 网文定时发书助手

给 Agent 用的指令包：编排「书稿 → 计划 → 自动发布 → 进度跟踪 → 续发」的闭环。
底层是 Playwright 浏览器自动化，登录态持久化，**只用于用户自己的账号发自己的书稿**。

## 一、前置条件

- Python 3.10+，已装 `playwright` 与 Chromium（首次：`pip install -e .` 或 `pip install playwright && playwright install chromium`）。
- 运行位置：用户能**微信扫码**的机器（起点登录必须扫码）。默认 workspace：`~/.nww/publish`。
- 书稿格式：**每章一个 `.md` 文件**，放在一个书文件夹里。标题优先级：frontmatter `title:` > 首个 `# 标题` > 文件名。

## 二、CLI 入口

代码在 `nww-publish/`，入口 `nww-publish.py`。任意 IDE 直接这样调：

```bash
PYTHON=/Users/oodpoo/.workbuddy/binaries/python/envs/default/bin/python
NW=/Users/oodpoo/NWW-V12/nww-publish
$PYTHON $NW/nww-publish.py <command> [args]
```

## 三、标准工作流（Agent 应照此编排）

1. **校准选择器（首次/会话失效时必须）**
   ```bash
   $PYTHON $NW/nww-publish.py calibrate --platform qidian
   ```
   浏览器打开 → 用户手动微信扫码登录 → 走到「新建章节」编辑页 → 按回车 → 工具打印候选元素。
   把打印结果里的对应值填进 `nww-publish/nww_publish/adapters/qidian.selectors.json`：
   `title_input`(标题框) / `content_editor`(正文区) / `publish_button`(发布钮) /
   `draft_button`(存草稿,可选) / `success_indicator`(成功标识,可选)。
   番茄同理：`--platform fanqie` + `fanqie.selectors.json`。

2. **规划发布队列**
   ```bash
   $PYTHON $NW/nww-publish.py plan --platform qidian --book 山河五契 \
       --src /path/to/book_folder --start 09:00 --interval 86400
   ```
   - `--start`：`now` | `HH:MM`(今天该时刻，已过则顺延次日) | ISO。
   - `--interval`：章节间隔秒（默认 3600）。`--jitter`：随机抖动秒。
   - 每日上限由 `config` 的 `safety.max_chapters_per_day` 约束（默认 5）。

3. **查看进度**
   ```bash
   $PYTHON $NW/nww-publish.py status --platform qidian
   ```

4. **执行发布**
   - 单次（适合 cron / 手动）：`run --platform qidian [--once] [--resume] [--dry-run]`
   - 常驻守护：`daemon --platform qidian [--resume]`（到点自动发，受每日上限约束，次日续发）
   - `--resume`：把 `failed` 章节重新加入发布。
   - `--dry-run`：不启浏览器，模拟整条管道（用于验证解析/状态机/调度）。

5. **补发 / 续发**（核心需求：跟随进度补充发布）
   - 失败章节会自动标 `failed` 并保留错误；`run --resume` 重发。
   - 新增章节只需把 `.md` 放进书文件夹，再跑一次 `plan`（同 `--book` 会去重并更新计划时间），`status` 即可看到新章进入队列。

## 四、配置

```bash
$PYTHON $NW/nww-publish.py config                 # 查看全部
$PYTHON $NW/nww-publish.py config --get safety.max_chapters_per_day
$PYTHON $NW/nww-publish.py config --set safety.max_chapters_per_day=3
$PYTHON $NW/nww-publish.py config --set safety.publish_mode=draft   # 先存草稿不发布
```

## 五、重要提醒（务必转达用户）

- **起点微信扫码无法纯自动**：首次及会话失效时需用户在打开的浏览器里手动扫码；会话由 Chromium 用户目录持久化，一般可维持多日。
- **反爬风险**：用真实 Profile + 拟人节奏降低被识别概率，但平台风控仍在。请仅发自己的内容、控制节奏（每日上限、间隔），避免批量灌水导致封号。
- **SPA 选择器会随官网改版失效**：若某天发布失败，重新跑 `calibrate` 并回填 `*.selectors.json` 即可，无需改代码。
- **番茄同理**：手机/扫码登录，选择器独立校准。

## 六、状态文件

`<workspace>/state/publish-state.json` 记录每章：状态(pending/publishing/published/failed/skipped)、计划时间、平台章节ID、URL、错误、重试次数。这是「查阅进度并跟随进度补充发布」的真相来源。
