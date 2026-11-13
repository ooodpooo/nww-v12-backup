"""Chrome 网页版 VIP 桥接：经 CDP 操控本机 Chrome 的 AI Studio 页面做补全。

背景（2026-09-23）：
  Google API（generativelanguage.googleapis.com）共享配额易限流（429/503）；
  本机日常 Chrome 已登录 AI Studio VIP。llm_client 侧把 chrome:// 端点路由到本模块，
  经 Chrome DevTools Protocol 复用该登录态发 prompt / 抓回复，彻底绕开 API 限流。

标签页生命周期（2026-09-24 按用户要求定稿——专用窗口、只开一次、与用户窗口零冲突）：
  - 桥接只使用自己首开的专用页：优先经 CDP Target.createTarget 开独立新窗口
    （与用户已有 gemini/aistudio 窗口物理隔离、互不干扰；开窗失败退回普通新标签）；
  - 专用页身份按 CDP targetId 跨调用记忆，之后每次调用只找它——
    绝不扫描/触碰用户手头的任何已有标签；
  - 用完不关闭（finally 只 pw.stop() 断开 CDP 连接），下次按 targetId 找回；
    专用页被用户手动关闭则下次自动重建（自愈）；
  - 已知取舍：后端进程重启后旧专用页变无主页面（无法再识别）会重开一扇，
    旧页面可手动关闭，不影响功能。

职责边界（对齐 opencode_bridge 桥接模式）：
  - 仅当 base_url 以 chrome:// 开头时启用（is_chrome_vip_endpoint）
  - 每次调用在专用页内新建会话（回首页 + 点 New chat = 干净上下文），防章间上下文串味
  - 全局锁串行化：sync Playwright 非线程安全，FastAPI 线程池并发请求排队执行
  - temperature/max_tokens 网页版无对应参数，忽略（与 opencode_bridge 同策略）
  - 模型沿用标签页当前选中（初版不做 UI 下拉自动化，脆弱）

依赖：playwright（函数内延迟 import；缺失时报明确错误，模块顶层零重依赖，
不影响 llm_client 对其的常规 import —— 不破坏现有任何链路）。

 Raises:
    RuntimeError: playwright 缺失、CDP 端口未监听、页面元素定位失败、生成超时。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Iterator, NamedTuple

# CDP 调试端口：与 launch_chrome_debug.sh 一致，可用环境变量覆盖
_CDP_PORT = int(os.environ.get("NWW_CHROME_CDP_PORT", "9222") or "9222")

# 防系统代理劫持 localhost CDP 连接：127.0.0.1 直连不走任何代理
# （实测：未隔离时 playwright connect_over_cdp 被 http_proxy 劫持返回 502；
#   增量追加不覆盖用户已有 NO_PROXY，与 start.sh 的 NO_PROXY=* 共存）
_no_proxy = os.environ.get("NO_PROXY", "")
if "127.0.0.1" not in _no_proxy:
    os.environ["NO_PROXY"] = f"{_no_proxy},127.0.0.1,localhost".lstrip(",")
    os.environ["no_proxy"] = os.environ["NO_PROXY"]

# ── 站点适配层（2026-09-26 起支持多站点自动切换）───────────────────────────
# 背景：AI Studio 会触顶/报 internal error，而 gemini.google.com 仍可用。故把
# 「站点」抽成档案：每个站点一套首页 URL + 选择器。切换站点 = 重新绑定下面这组
# 模块级常量 —— 这样 _send_prompt / _run_ready / _iter_reply 等既有函数全部
# 不用改签名，风险最低。
#
# ★2026-09-26 用户裁决：**只用 gemini.google.com，停用 AI Studio**。
#   AI Studio 档案保留（便于日后一行 env 回退），但 ready=False ⇒
#   既不进默认站点序列，显式指定也会被拒绝（见 _select_site）。
#
# ready=False 的站点不参与自动切换（选择器还没探活，或已被裁决停用）。
class _Site(NamedTuple):
    key: str
    home: str
    url_hint: str          # 在已有标签里识别该站点的 URL 子串
    ready: bool
    prompt: tuple
    run: tuple
    new_chat: tuple
    output: tuple
    thought: tuple
    # 「正在生成」的判据词（小写子串）。AI Studio 是英文 "progress_activity Stop"；
    # Gemini 消费端是中文「停止」，只认 stop 会永远判不出"生成中" —— 必须按站点配。
    stop_markers: tuple = ("stop",)
    # 一轮对话的容器（轮次计数 / 基线 / 取正文都靠它）。
    # AI Studio 一个 ms-chat-turn 即一轮；Gemini 是 user-query + model-response 交替，
    # 两者都算轮次节点（文档顺序即时间顺序）。
    turn: tuple = ("ms-chat-turn",)
    # 模型回应节点的标签名（用于"本轮是否已出回应"判据）。AI Studio 无独立标签 → 空串。
    resp: str = ""
    # turn 内承载正文的节点（AI Studio: ms-cmark-node/.markdown；
    # Gemini: .markdown-main-panel）。
    body: tuple = ("ms-cmark-node", ".markdown")
    # 输入框形态：textarea 可用 input_value()；contenteditable（Gemini 的 Quill
    # 富文本）没有 value，只能用 inner_text()。
    input_kind: str = "textarea"
    # 独立于发送按钮的「停止生成」控件选择器。
    # Gemini 输入清空后发送按钮直接消失，生成中另起一个「停止」按钮 —— 不配就判不出生
    # 成中（_run_ready 恒 True → started 永远置不上 → 只能干等到硬超时）。
    stop: tuple = ()
    # 角色判定方式：True = 按标签名（user-query / model-response，明确无歧义）；
    # False = 退化为看 turn 头部徽章文本（AI Studio 只有 ms-chat-turn 一种标签）。
    role_by_tag: bool = False
    # 错误占位标记（站点专属）。留空则回退到通用英文标记 _BASE_ERROR_MARKERS。
    # 为什么不把中文标记塞进通用表：NWW 写的是中文小说，正文里出现"出了点问题"
    # 这类对白完全正常，无差别匹配会误杀正文并触发冷却重发链。
    error_markers: tuple = ()
    # ★配额/用量上限类错误标记（站点专属，2026-09-26 新增）。
    #   命中 → 抛 _StudioQuotaExceeded（_StudioErrorTurn 子类）→ _site_failover_run
    #   换到下一个站点。这正是"Gemini 配额不够时切 AI Studio"的触发判据。
    #   ⚠️ 必须与 error_markers **分开列**：两者处置动作根本不同 ——
    #     内部错误原地冷却重发多半能自愈；配额错误重发一万次也不会好，只能换通道。
    #     合并进 error_markers 就会退化成"原地重发到超时"，等于没做 failover。
    quota_markers: tuple = ()


# ★2026-09-26 同一天两次改口（08:45「去掉 aistudio」→ 22:20「要它作备用通路」）。
#   → 做成**显式 env 开关**，默认关闭，避免再改口时又要动代码：
#     NWW_BRIDGE_AISTUDIO=1 启用 aistudio 档案（仅此还不够，还得进站点序列，见
#     NWW_BRIDGE_SITES）。两道开关刻意分开：前者=档案可用，后者=参与 failover。
#   为什么默认关闭：① 两站点 DOM 完全不同，选择器未实机探活就启用 = 静默失效；
#   ② 切站点会让会话池里旧站点的 Tab 失配（已用站点维度 session key 隔离，见
#      _session_key）；③ 用户当天已推翻过一次双配置。
_AISTUDIO_READY = os.environ.get("NWW_BRIDGE_AISTUDIO", "0").strip().lower() in (
    "1", "true", "yes", "on",
)
_AISTUDIO = _Site(
    key="aistudio",
    home="https://aistudio.google.com",
    url_hint="aistudio.google.com",
    ready=_AISTUDIO_READY,
    prompt=(
        "textarea[aria-label='Enter a prompt' i]",
        "textarea[aria-label*='prompt' i]",
        "textarea[placeholder*='prompt' i]",
        "ms-autosize-textarea textarea",
        "textarea",
        "div[contenteditable='true']",
    ),
    run=(
        "ms-run-button button",
        "button[aria-label='Run' i]",
        "button[aria-label='Run prompt' i]",
        "button[aria-label*='Send' i]",
        "run-button button",
    ),
    new_chat=(
        "button[aria-label='New chat' i]",
        "button[aria-label*='New chat' i]",
        "button[aria-label*='新建聊天']",
        "button[aria-label*='新聊天']",
    ),
    output=(
        "ms-chat-turn ms-cmark-node",
        "ms-chat-turn .markdown",
        ".model-response-text .markdown",
        "ms-text-chunk",
        "main .markdown",
    ),
    thought=(
        "ms-thought-chunk",
        ".thought-container",
        ".model-thought",
    ),
    quota_markers=(
        "quota", "usage limit", "rate limit", "reached your limit",
        "exceeded your", "plan limit", "upgrade to",
    ),
)

# Gemini 消费端（gemini.google.com）。
# 选择器均于 2026-09-26 实机探活（见 docs/记忆）：
#   输入框 = Quill 富文本 div.ql-editor[role=textbox]（**不是 textarea**，
#            也没有 input_value()，取值要走 inner_text，见 _box_text）
#   发送   = button[aria-label='发送']（空输入时不存在，有输入才渲染出来）
#   新对话 = a[aria-label='发起新对话']
#   轮次   = user-query / model-response 两大类（文档顺序即时间顺序）
#   正文   = model-response message-content .markdown-main-panel
_GEMINI = _Site(
    key="gemini",
    home="https://gemini.google.com",
    url_hint="gemini.google.com",
    ready=True,
    prompt=(
        "div.ql-editor[role='textbox']",
        "rich-textarea div[contenteditable='true']",
        "div[contenteditable='true'][aria-label*='Gemini' i]",
        "div[contenteditable='true']",
    ),
    run=(
        "button[aria-label='发送' i]",
        "button[aria-label*='发送' i]",
        "button[aria-label*='Send' i]",
        "button.send-button",
    ),
    new_chat=(
        "a[aria-label='发起新对话' i]",
        "a[aria-label*='新对话' i]",
        "a[aria-label*='New chat' i]",
    ),
    # ⚠️ 实测（2026-09-26）两个反直觉点：
    #   1) `.markdown-main-panel` 与 `.markdown` 是**同一个元素**
    #      （class="markdown markdown-main-panel md-content …"），都是干净的正文；
    #   2) 真正带界面徽章的是外层 `.response-content`（"Gemini 说\n\n5"，比正文多 10 字）。
    #      它在文档顺序上排在 `.markdown` **之前**，而 body 选择器是逗号合并后按文档
    #      顺序取第一个非空节点 —— 列了它就必然先命中它，正文被混进 UI 文字。
    #      所以 `.response-content` 一律不得进正文候选。
    output=(
        "model-response .markdown",
        "message-content .markdown",
        "model-response .markdown-main-panel",
    ),
    thought=(
        ".thinking-indicator",
        "thoughts-container",
        ".model-thought-container",
    ),
    stop_markers=("停止", "stop", "停止生成", "停止响应"),
    turn=("user-query, model-response",),
    resp="model-response",
    body=(".markdown", ".markdown-main-panel"),
    input_kind="contenteditable",
    stop=(
        "button[aria-label*='停止' i]",
        "button:has-text('停止')",
    ),
    role_by_tag=True,
    error_markers=(
        "internal error", "an error has occurred", "something went wrong",
        "出现问题", "出了点问题", "发生错误", "出了错", "无法生成",
        "请稍后再试", "响应出错", "生成失败",
        # ★safety filter 拒答文案（2026-09-26 实测三例）：
        #   「我只是一个语言模型，无法提供这方面的帮助。」（19.4k 脱纲审计）
        #   「我是一个语言模型，这超出了我的设计用途。」（3k 对照组）
        #   「身为一个语言模型，我没办法提供这方面的帮助。」（3k 复测）
        #   实验证明拒答是**概率性**的（同结构 prompt 时过时不拒）→ 重发有意义，
        #   故纳入 error_markers 走冷却重发链，而不是当正文返回。
        # ⚠️ 用「一个语言模型」同时覆盖「我是一个/我只是一个/身为一个」三种开头。
        #   误杀风险评估：NWW 是修仙小说，正文出现这些句子的概率≈0；
        #   且判定有 200 字短文本护栏（_ERROR_PLACEHOLDER_MAX_LEN）兜底。
        "一个语言模型", "无法提供这方面的帮助", "没办法提供",
        "超出了我的设计用途", "违反内容政策", "无法协助", "不能协助",
    ),
    # ★配额/用量上限（Gemini 消费端文案为中文）。命中即切 AI Studio ——
    #   依据 BRIDGE.md §9：AI Pro 订阅者在 **AI Studio 网页**自动获得更高额度，
    #   所以"Gemini 触顶 → AI Studio"是本账户唯一正确的换通道方向（反向无收益）。
    #   ⚠️ 判定时同样叠加"文本很短"护栏（见 _latest_turn_quota_exceeded），
    #   否则正文里出现「额度」二字就会误触发切换。
    quota_markers=(
        "用量上限", "已达上限", "使用量已达", "今日限额", "已达今日",
        "配额已用尽", "额度已用尽", "超出配额", "暂时无法使用",
        "升级到 google ai", "google ai pro", "google ai ultra",
        "quota", "usage limit", "reached your limit", "rate limit",
    ),
)

_SITES: dict[str, _Site] = {s.key: s for s in (_AISTUDIO, _GEMINI)}

# 站点优先顺序：env NWW_BRIDGE_SITES 逗号分隔（例 "gemini,aistudio"）。
# ★默认只走 gemini（用户 2026-09-26 裁决）。想回退 AI Studio：
#   NWW_BRIDGE_SITES=aistudio —— 但需先把 _AISTUDIO.ready 改回 True，否则会被拒绝。
_SITE_ORDER_RAW = os.environ.get("NWW_BRIDGE_SITES", "gemini")
_SITE_ORDER = tuple(
    s.strip() for s in _SITE_ORDER_RAW.split(",")
    if s.strip() in _SITES and _SITES[s.strip()].ready
)
_UNKNOWN_SITES = tuple(
    s.strip() for s in _SITE_ORDER_RAW.split(",")
    if s.strip() and s.strip() not in _SITES
)
# 配了没适配/拼错的站点不能默默忽略 —— 那是"以为开了双站点其实没有"的陷阱
_SITE_NOT_READY = tuple(
    s.strip() for s in _SITE_ORDER_RAW.split(",")
    if s.strip() in _SITES and not _SITES[s.strip()].ready
)

# 当前生效站点（由 _select_site 维护）
_ACTIVE_SITE_KEY = "gemini"
# 当前站点的「正在生成」判据词（由 _select_site 绑定；Gemini 是中文「停止」）
_STOP_MARKERS = _GEMINI.stop_markers

# 首页 = 新会话入口（具体值由 _select_site 按站点绑定）
_STUDIO_HOME = _GEMINI.home

# sync Playwright 非线程安全：全局锁串行化全部页面操作（含流式轮询）
_LOCK = threading.Lock()

# ---- 页面选择器（ARIA 语义优先 + 常见类名 fallback；页面改版时在此校准） ----
# 以下常量会被 _select_site() 按当前站点重新绑定。
# ⚠️ 初值取自 _AISTUDIO（历史沿革，单测依赖），但**默认站点已是 gemini**：
#    真实链路一律先过 _site_failover_run → _select_site，故初值不会被用到。
#    单测直接调这些函数时若依赖选择器，请显式 _select_site() 后再调。
# 输入框候选：AI Studio 的 prompt 输入区
# （2026-09 实测：ms-autosize-textarea 已下线，现为 div.text-wrapper > textarea，
#   aria-label="Enter a prompt"）
_PROMPT_SELECTORS = _AISTUDIO.prompt
# 发送按钮候选
# （2026-09 实测坑：真实 Run 按钮无 aria-label、位于 ms-run-button 组件内；
#   aria-label 子串匹配 'Run' 会误中 "Toggle run settings panel"，必须精确匹配）
_RUN_SELECTORS = _AISTUDIO.run
# New chat 按钮候选：同标签内开新会话（不新开/关闭标签）；中英界面全覆盖——
# 点不中会静默退化为沿用当前页，旧会话内容有串味风险，故语言 variant 要全
_NEW_CHAT_SELECTORS = _AISTUDIO.new_chat
# 输出块候选：生成完成后抓取最后一个匹配的文本容器
# （2026-09 实测：模型回复 = ms-chat-turn 内部正文（ms-cmark-node / .markdown），
#   turn 的 inner_text 是 UI 徽章（edit/成功/thumb_up）不是正文；
#   ms-chat-turn-container 选择器不存在，实际元素名是 ms-chat-turn）
_OUTPUT_SELECTORS = _AISTUDIO.output
# 生成错误标记：正文/末个 turn 命中即视为 AI Studio 返回错误占位。
# 实测（2026-09-25）：该错误不会自愈（连续观测 75s 仍为 error），必须由
# _iter_reply_with_resend 主动冷却重发——旧注释"AI Studio 会自动重试"已证伪。
# 这是**通用英文基线**；站点专属标记见 _Site.error_markers（当前生效值 = _ERROR_MARKERS，
# 由 _select_site 按站点绑定）。
_BASE_ERROR_MARKERS = (
    "internal error",
    "an error has occurred",
    "failed to get response",
    "something went wrong",
    "try again",
)
_ERROR_MARKERS = _BASE_ERROR_MARKERS

# 配额/用量上限标记（当前生效值，由 _select_site 按站点绑定）。
# 空 tuple = 该站点未配配额判据 → 不触发换站点（保守：宁可不切，不可乱切）。
_QUOTA_MARKERS: tuple[str, ...] = ()

# 错误占位必须叠加「正文很短」辅助判据（P0-3）。
# 上面这些标记词本身会出现在模型正文里（例如答复中写 "you can try again"），
# 仅对整段 turn.inner_text() 做子串匹配会误杀正常回答并触发 30s 冷却重发。
_ERROR_PLACEHOLDER_MAX_LEN = 200

# 思考（Thoughts）独立容器候选。
# 为什么必须下沉到元素级：Gemini 2.0/2.5 真实渲染里，思考与正文**常常同处一个
# ms-chat-turn**（上方折叠思考面板 + 下方 Markdown 正文）。此时 turn 头部会印出
# 类似 "Model 10:20 Thought for 5 seconds" 的字样，若只靠 head 里有没有 "thought"
# 来做 turn 级二分，整条 turn 会被判成"仅思考"，于是正文被 `not c.is_thought`
# 整段过滤掉 —— 最终只能收到思考、正文永远为 None，一路静默拖到硬超时。
# ⚠️ 不要加 `div[class*='thought']` 这类宽泛选择器：实测（2026-09-25）它会命中
# 思考面板的**外壳容器**，取到 "正在思考 / Expand to view more" 这类界面提示语，
# 并被当成推理内容交付出去（非流式还会被包进 <think>）—— 比取不到更糟。
_THOUGHT_SELECTORS = _AISTUDIO.thought
# 轮次容器（按站点绑定）
_TURN_SELECTORS = _AISTUDIO.turn
# 模型回应节点标签（按站点绑定；空串 = 该站点无独立回应标签）
_RESP_TURN_SELECTOR = _AISTUDIO.resp
# turn 内正文节点（按站点绑定）
_BODY_SELECTORS = _AISTUDIO.body
# 输入框形态（按站点绑定）：textarea 可 input_value()，contenteditable 只能 inner_text()
_INPUT_KIND = _AISTUDIO.input_kind
# 独立停止按钮（按站点绑定；AI Studio 无 —— 它的 Run 按钮自己会变成 Stop）
_STOP_SELECTORS = _AISTUDIO.stop
# 角色判定方式（按站点绑定）
_ROLE_BY_TAG = _AISTUDIO.role_by_tag

# 界面装饰语：命中即判定"取到的不是推理内容"，丢弃。
_THOUGHT_UI_MARKERS = (
    "expand to view", "expand to see", "collapse", "show thinking",
    "hide thinking", "show more", "more_vert", "more_horiz",
    "正在思考", "展开查看", "查看全部", "收起", "思考了", "thought for",
)

# 出错恢复链（2026-09-25 外脑裁决 Q2 定稿）：检测到错误 turn / 回显污染后
# 主动恢复，上限 _ERROR_RESEND_MAX 次重发（= 总尝试 3 次），耗尽立即抛错不空转。
# 冷却时长保持 30s 不下调：实测 30s 以下（5/10/15/20s）恢复率明显劣化。
_ERROR_RESEND_MAX = 2
_ERROR_RESEND_WAIT_S = 30.0   # 首次重发前冷却（同会话重发，覆盖服务端惩罚窗口）
_ERROR_RESEND_WAIT2_S = 45.0  # 第二次重发前冷却（重开新会话重发，最后的兜底）

# 正文候选最小长度：过滤空节点残留。
# ⚠️ 曾设为 2，代价是**极短答案永远取不到**：实测 "1+1=? 只给数字" -> 答案 "2"
# 长度 1，被这条过滤掉，`_latest_output_text` 恒为 None，于是明明答案早已渲染
# 在页面上，桥接仍一路空等到硬超时（实测 300s ReadTimeout）。数字/是否/选项类
# 短回答是 CLI 工具的常见形态，必须放行。
# 设为 1 后会不会把 UI 徽章当正文？不会：取值只认 ms-cmark-node / .markdown 且
# 文本非空；而交付完成另有 `started`（见过 Stop 态）+ stable 连续稳定 +
# `_run_ready` 三道闸，生成中的光标/占位符过不了闸。
_MIN_CANDIDATE_LEN = 1

# 回显污染检测（P1）：输出开头 _ECHO_PREFIX_RATIO 内与提问连续匹配
# _ECHO_MIN_MATCH 字符即判异常（实测污染样本 = 短答复 + 提问原文，md5 可复现）
_ECHO_MIN_MATCH = 20
_ECHO_PREFIX_RATIO = 0.3
# 命中片段须覆盖输出正文的比例（见 _is_echo_polluted 文档：旧判据缺这条导致误杀）
_ECHO_COVER_RATIO = 0.5

# 生成完成判定：输出文本连续 N 次轮询不变（DOM 稳定）
# （2026-09-25 提速：3×1000ms → 2×500ms，尾部固定等待 3~4s 降到 ~1s，
#   仍保留"连续两次不变"以防长文生成中途停顿被误判完成）
_STABLE_POLLS = 2
_POLL_INTERVAL_MS = 500
# 物理发送开关（2026-09-26）：默认开启 —— 提问经 CDP Input.insertText 走浏览器
# 输入管线注入、并以真实回车提交，而非 fill() 直接改 DOM value + 合成点击。
# 出问题时设 NWW_BRIDGE_PHYSICAL_SEND=0 可立即退回旧行为。
_PHYSICAL_SEND = os.environ.get("NWW_BRIDGE_PHYSICAL_SEND", "1") not in ("0", "false", "no")

# 「生成已结束但始终没有正文」的宽限轮询数（10 × 500ms = 5s）。
# 为什么需要：AI Studio 生成结束后 DOM 渲染/流式收尾还要一点时间，刚停下的瞬间
# 取不到正文是正常现象，必须给宽限；但连续这么多次仍无正文就是**静默空响应**
# —— 旧实现对此没有分支，只能一路 continue 空转到硬超时（实测干等 900s 才报错）。
_EMPTY_GRACE_POLLS = 10
# 连接 CDP 的握手超时（毫秒）；实测：Chrome 启动恢复多标签时握手可达 10s+，
# 10s 会间歇性超时，提到 30s
_CONNECT_TIMEOUT_MS = 30_000


def is_chrome_vip_endpoint(base_url: str, model: str) -> bool:
    """判断端点是否为 Chrome 网页版 VIP 专线（chrome:// 前缀即命中）。"""
    return (base_url or "").startswith("chrome://")


def _build_message(system_prompt: str, user_prompt: str) -> str:
    """合并 system/user 为网页单轮提问（网页对话无独立 system 通道）。"""
    sys_p = (system_prompt or "").strip()
    usr_p = (user_prompt or "").strip()
    if sys_p and usr_p:
        return f"{sys_p}\n\n---\n\n{usr_p}"
    return sys_p or usr_p


def _connect_browser(pw):
    """连接本机 Chrome（CDP），失败时附启动脚本指引。"""
    try:
        return pw.chromium.connect_over_cdp(
            f"http://127.0.0.1:{_CDP_PORT}", timeout=_CONNECT_TIMEOUT_MS
        )
    except Exception as exc:  # noqa: BLE001 — 连接类错误统一转清晰指引
        raise RuntimeError(
            f"CDP 连接失败（127.0.0.1:{_CDP_PORT}）：{exc}。"
            "请先在项目根目录运行 ./launch_chrome_debug.sh 开启带调试端口的 Chrome，"
            f"并在浏览器中登录 {_STUDIO_HOME}"
        ) from exc


# 桥接专用页的 CDP targetId（跨调用记忆；纯字符串，无线程安全问题。
# 不持久化 playwright 连接的原因：sync API 绑定创建线程，FastAPI 线程池
# 跨线程复用会报错，故每次调用重新 connect，仅靠 targetId 识别专用页）
# 会话路由池（2026-09-25）：session_id -> targetId。
# 同类话题固定打到同一扇 Tab 续聊（上下文连贯 + 复用后秒回），
# 不同 session_id 各自独立 Tab（物理隔离，互不污染）。
_SESSION_TARGETS: dict[str, str] = {}
# LRU 时间戳，用于超上限时淘汰最久未用的会话
_SESSION_LAST_USED: dict[str, float] = {}
# 池上限，防止 Tab 无限堆积 —— 审计 P1-2 记录过 32 页永不回收的泄漏。
# 设为 0 表示不限制（调试用）。
_SESSION_MAX = int(os.environ.get("NWW_BRIDGE_MAX_SESSIONS", "8") or "8")

# ── 性能改造开关（2026-09-29，详见 docs/chrome_bridge效率优化方案_20260929.md）──
# 两个都默认开启，且都能用 env 秒退回旧行为，避免"改坏了只能回滚文件"。
#
# 背景：实测每轮固定开销 35s，其中「整页 goto 冷启动」2.3s（慢网 30s+ 并 TimeoutError
#   重试 2 次）＋「新会话首请求惩罚」30s。而页面已就绪时 SPA 内点 New chat 只要 0.13s
#   且完全不白屏；会话池 8 条里 7 条是死条目，导致每次新 session_id 都要 createTarget
#   开一个新 OS 窗口（用户反馈的「每次都新开一个 Chrome 窗口」）。
_SPA_NEWCHAT = os.environ.get("NWW_BRIDGE_SPA_NEWCHAT", "1").strip().lower() not in (
    "0", "false", "no", "off",
)
_REUSE_COLD_TAB = os.environ.get("NWW_BRIDGE_REUSE_COLD_TAB", "1").strip().lower() not in (
    "0", "false", "no", "off",
)
# 向后兼容旧命名：等价于 default 会话的 targetId
_BRIDGE_TARGET_ID: str | None = None

# 会话池**持久化**（2026-09-25）：targetId 只存内存时，进程一重启就全部失忆，
# 于是每次重启都新开一批 Tab，旧的变孤儿 —— 实测已堆积 24 个 AI Studio 页。
# 落盘后跨进程重启可复用同一批专用页，从根上止住泄漏。
_SESSION_STORE = os.environ.get(
    "NWW_BRIDGE_SESSION_STORE",
    os.path.join(os.path.expanduser("~"), ".nww-local", "bridge_sessions.json"),
)
_SESSIONS_LOADED = False


# 站点维度会话 key 的默认归属站点（旧格式条目迁移用）。
# 固定写 "gemini" 而非读 _ACTIVE_SITE_KEY：后者在切站点后会变，拿它迁移历史条目
# 会把 gemini 的 Tab 误登记给 aistudio。
_SESSION_LEGACY_SITE = "gemini"


def _session_key(session_id: str) -> str:
    """会话池 key = 「站点:session_id」（2026-09-26 加站点维度）。

    ⚠️ 为什么必须带站点：切站点后若继续复用同一 session_id 的旧 Tab，那扇页还停在
      **旧站点**（例：gemini.google.com），而选择器已被 _select_site 切成新站点的
      （AI Studio）→ 所有选择器静默落空，表现为"切了站点却完全没生效"，极难排查
      （BRIDGE.md §10 记过同族事故：旧会话条目把用户自己开着的页面导航到新站点）。
      带站点维度后两站点各自独立 Tab，天然隔离，
      也免掉了"切站点必须手清 bridge_sessions.json"这个易漏的人工步骤。
    """
    return f"{_ACTIVE_SITE_KEY}:{session_id}"


def _load_session_targets() -> None:
    """首次使用时从磁盘恢复会话池（文件不存在/损坏 → 按空池处理）。"""
    global _SESSIONS_LOADED
    if _SESSIONS_LOADED:
        return
    _SESSIONS_LOADED = True
    try:
        with open(_SESSION_STORE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            # 磁盘条目的"最后使用时间"用文件 mtime 归一到单调时钟：
            # 它们确实都很久没用了，但必须**有**时间戳 —— 见下方 LRU 说明。
            try:
                age = max(0.0, time.monotonic() - os.path.getmtime(_SESSION_STORE))
            except OSError:
                age = 0.0
            loaded_at = time.monotonic() - age
            for k, v in data.items():
                kk = str(k)
                # 旧格式（无 "站点:" 前缀）统一迁移到默认站点，否则历史条目
                # 既查不到又不会被 LRU 淘汰 → 变成永不回收的孤儿 Tab。
                key = kk if ":" in kk else f"{_SESSION_LEGACY_SITE}:{kk}"
                _SESSION_TARGETS[key] = str(v)
                # ★必须同步补 _SESSION_LAST_USED：两个 dict 的 key 集合一旦不一致，
                #   _evict_sessions_if_needed 会选中"唯一有时间戳的那条"——
                #   也就是**刚创建的会话自己**（实测 2026-09-26：新建 gemini:protocol_test
                #   后立刻被自己淘汰，随后 goto 到已关闭的页 → TargetClosedError）。
                _SESSION_LAST_USED.setdefault(key, loaded_at)
    except Exception:  # noqa: BLE001 — 首次运行或文件损坏都按空池处理
        pass


def _save_session_targets() -> None:
    try:
        os.makedirs(os.path.dirname(_SESSION_STORE), exist_ok=True)
        with open(_SESSION_STORE, "w", encoding="utf-8") as fh:
            json.dump(_SESSION_TARGETS, fh, ensure_ascii=False)
    except Exception:  # noqa: BLE001 — 落盘失败不影响主流程
        pass


def _page_target_id(page) -> str | None:
    """读取页的 CDP targetId（识别专用页身份用；读取失败返回 None 不阻断主流程）。"""
    try:
        sess = page.context.new_cdp_session(page)
        info = sess.send("Target.getTargetInfo")
        return ((info or {}).get("targetInfo") or {}).get("targetId")
    except Exception:  # noqa: BLE001
        return None


def _find_page_by_target_id(browser, target_id: str):
    """按 targetId 在全部上下文中找专用页；找不到/已关闭返回 None。"""
    for ctx in browser.contexts:
        for pg in ctx.pages:
            try:
                if not pg.is_closed() and _page_target_id(pg) == target_id:
                    return pg
            except Exception:  # noqa: BLE001 — 单个标签状态异常跳过，继续找
                continue
    return None


def _live_target_ids(browser) -> set:
    """一次遍历收集所有存活页的 targetId（供池自净 / 冷 Tab 复用比对用）。

    为什么不逐条调 `_find_page_by_target_id`：它每查一个 id 就把全部页面遍历一遍
    （实测 0.06s/次），池里 8 条就是 ~0.5s —— 每轮都付太贵。这里一次遍历搞定。
    """
    ids = set()
    for ctx in browser.contexts:
        for pg in ctx.pages:
            try:
                if not pg.is_closed():
                    tid = _page_target_id(pg)
                    if tid:
                        ids.add(tid)
            except Exception:  # noqa: BLE001 — 单页读取异常跳过
                continue
    return ids


def _page_is_ready(page) -> bool:
    """桥接页是否**真的可用**（在当前站点 + 输入框已渲染）。

    ★2026-09-29：命中 targetId ≠ 可用。实测有一扇 Tab 卡在
    https://gemini.google.com/（根域营销落地页）—— 它能通过 `_url_mismatch`
    （`url_hint` 只是域名子串，根域 URL 里也有），但那里**根本没有输入框**，
    直接送进去用就会报「未找到 gemini 输入框」。
    更糟的是这种坏页一旦被登记进池，之后每次调用都命中它 → **持续复现**。
    """
    if _url_mismatch(page):
        return False
    try:
        return _locate(page, _PROMPT_SELECTORS) is not None
    except Exception:  # noqa: BLE001 — 判定失效按不可用处理（下游会自愈）
        return False


def _prune_dead_sessions(browser) -> int:
    """剔除池里的死条目（Tab 已被关闭 / Chrome 重启过），返回剔除条数。

    ★2026-09-29：实测池里 8 条有 7 条是死条目。它们白占 _SESSION_MAX 名额，
    于是每个新 session_id 都会触发「LRU 淘汰 + createTarget 开一个新 OS 窗口」
    —— 这正是用户反馈的「每次都新开一个 Chrome 窗口」。
    """
    live = _live_target_ids(browser)
    dead = [k for k, tid in list(_SESSION_TARGETS.items()) if tid not in live]
    for k in dead:
        _SESSION_TARGETS.pop(k, None)
        _SESSION_LAST_USED.pop(k, None)
    if dead:
        _save_session_targets()
        print(
            f"[CHROME_BRIDGE] 会话池自净：剔除 {len(dead)} 条失效条目 {dead}",
            flush=True,
        )
    return len(dead)


def _take_coldest_live_page(browser):
    """把池里最久未用的**存活** Tab 过户给新 session（调用方负责清空上下文）。

    为什么：池满之后旧逻辑是「关掉最久未用的 → 下次再 createTarget 开一个新 OS
    窗口」= 反复开关窗、窗口越堆越多。改成直接把那扇冷 Tab **过户**给新 session，
    Tab 总数恒定，不再新开窗口。
    ⚠️ 只过户存活的页；找不到返回 None（调用方退回 _create_dedicated_page）。
    """
    live = _live_target_ids(browser)
    cands = [(k, t) for k, t in _SESSION_TARGETS.items() if t in live]
    if not cands:
        return None
    oldest = min(cands, key=lambda kv: _SESSION_LAST_USED.get(kv[0], 0.0))[0]
    tid = _SESSION_TARGETS.pop(oldest, None)
    _SESSION_LAST_USED.pop(oldest, None)
    _save_session_targets()
    return _find_page_by_target_id(browser, tid)


def _create_dedicated_page(browser):
    """首开桥接专用页：优先 CDP 原生独立新窗口（与用户窗口隔离），失败退回新标签。

    Returns:
        (page, target_id)；target_id 读取失败时为 None（本次仍可用，
        下次无法找回会重建并打印警告）。
    """
    if not browser.contexts:
        raise RuntimeError("CDP 连接成功但未找到浏览器上下文（Chrome 可能刚启动，请重试）")
    ctx = browser.contexts[0]
    # 优先：Target.createTarget(newWindow=True, background=True) 开独立 OS 窗口
    # background=True 尽量后台打开不抢焦点；不支持/失败则走下方标签兜底
    try:
        sess = browser.new_browser_cdp_session()
        target_id = ((sess.send("Target.createTarget", {
            "url": _STUDIO_HOME, "newWindow": True, "background": True,
        }) or {}).get("targetId"))
        if target_id:
            # playwright 自动 attach 新 target 有延迟，轮询等它出现在 pages 里
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                pg = _find_page_by_target_id(browser, target_id)
                if pg is not None:
                    return pg, target_id
                time.sleep(0.3)
            # playwright 始终未能接管该 target：关掉这扇窗避免孤儿，走标签兜底
            try:
                sess.send("Target.closeTarget", {"targetId": target_id})
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001 — newWindow 不支持/失败，退回普通新标签
        pass
    pg = ctx.new_page()
    _goto_studio(pg)
    return pg, _page_target_id(pg)


def _get_bridge_page(browser, session_id: str = "default"):
    """返回桥接专用页：只认自己首开的那一扇（targetId 记忆），与用户已有窗口零冲突。

    复用策略（2026-09-24 用户定稿）：专用页常驻 Chrome，用完不关；
    被手动关闭/身份失效 → 自动重开一扇新的专用页（自愈）。

    会话路由池（2026-09-25 升级）：
      - 原实现只有**一个** targetId，所有调用挤在同一扇 Tab：要么每次 new session
        （每次交 30s 首请求惩罚），要么全部共用（不同话题上下文糊在一起）。
      - 现在按 session_id 维护多扇 Tab：同一 session_id 的请求打到同一扇并在其中续聊，
        不同 session_id 各自独立 Tab，**物理隔离**。
      - 好处：① 同类话题上下文天然连贯；② 30s 首请求惩罚只在该分类**首次建 Tab**
        时付一次，之后全部秒回；③ 写小说 / 改代码 / 日常问答互不污染。
      - Tab 被手动关闭或崩溃 → 自愈重开；池超过 _SESSION_MAX 时按 LRU 关闭最久未用的。
    """
    _load_session_targets()
    skey = _session_key(session_id)
    target_id = _SESSION_TARGETS.get(skey)
    if target_id:
        pg = _find_page_by_target_id(browser, target_id)
        if pg is not None:
            _SESSION_LAST_USED[skey] = time.monotonic()
            # ★2026-09-29：命中 targetId 不等于可用（见 _page_is_ready）。
            #   卡在根域营销落地页的 Tab 会被误判成"可复用"，送进去就报
            #   「未找到 gemini 输入框」并持续复现 —— 这里先自愈再交出去。
            if not _page_is_ready(pg):
                print(
                    f"[CHROME_BRIDGE] 会话 {skey} 的标签不在就绪态，"
                    "先拉回就绪（不新开窗口）",
                    flush=True,
                )
                _new_session(pg)
            return pg
        _SESSION_TARGETS.pop(skey, None)  # 该 Tab 已不在，下面重建
        _save_session_targets()
        # 顺带把其它失效条目一并清掉：它们白占 _SESSION_MAX 名额，是"每次都
        # 新开一个 Chrome 窗口"的直接成因（实测 8 条里 7 条是死条目）。
        _prune_dead_sessions(browser)

    # ★2026-09-29：池已满时优先把最冷的活 Tab 过户给本 session，
    #   而不是「关掉再 createTarget 开一个新 OS 窗口」—— Tab 总数从此恒定。
    pg = None
    reused = False
    if _REUSE_COLD_TAB and _SESSION_MAX > 0 and len(_SESSION_TARGETS) >= _SESSION_MAX:
        pg = _take_coldest_live_page(browser)
        if pg is not None:
            reused = True
            # 过户前必须清空上下文（这扇页原先属于别的 session）
            _new_session(pg)
            print(
                f"[CHROME_BRIDGE] 会话池已满，复用最久未用的标签给 {skey}（不新开窗口）",
                flush=True,
            )
    if pg is None:
        pg, target_id = _create_dedicated_page(browser)
    else:
        target_id = _page_target_id(pg)
    if target_id:
        _SESSION_TARGETS[skey] = target_id
        _SESSION_LAST_USED[skey] = time.monotonic()
        if session_id == "default":
            _BRIDGE_TARGET_ID = target_id  # 保持旧字段语义一致
        _save_session_targets()
        _evict_sessions_if_needed(browser)
        # 新开的 Tab 必须显式 New chat：其一，保证上下文干净（见 _new_session 实测注释）；
        # 其二，这个 goto+New chat 会把页面拉到就绪态，之后续聊不必再付一次。
        # （过户来的页在上面已经 new_session 过，不重复付一次。）
        if not reused:
            _new_session(pg)
    else:
        print("[CHROME_BRIDGE] 警告：专用页 targetId 读取失败，下次调用将重建专用页", flush=True)
    return pg


def _evict_sessions_if_needed(browser) -> None:
    """池超过上限时关闭最久未用的 Tab（不要在 session_id 无限增长时把 Chrome 拖垮）。

    ★2026-09-26 修死循环 + 自我淘汰（实测事故）：
      ① 两个 dict 的 key 集合本来可能不一致（磁盘加载的条目没有 LRU 时间戳），
         于是 `min(_SESSION_LAST_USED)` 只会选中**刚创建的会话自己** → 建完立刻自毁，
         随后 goto 到已关闭的页 → TargetClosedError。已在 _load_session_targets
         侧补齐时间戳，这里再补两道硬防护：
      ② 候选必须**同时**在 _SESSION_TARGETS 里，否则 pop 不掉、长度不降 → 死循环；
      ③ 迭代次数封顶，任何异常都不得把主线程卡在 while 里。
    """
    if _SESSION_MAX <= 0:
        return
    for _ in range(len(_SESSION_TARGETS) + 1):
        if len(_SESSION_TARGETS) <= _SESSION_MAX:
            return
        cands = [k for k in _SESSION_LAST_USED if k in _SESSION_TARGETS]
        if not cands:
            # 时间戳表整体缺失（异常态）：退化为按 _SESSION_TARGETS 顺序淘汰，
            # 绝不能因此卡住不动 —— 那会让 Tab 无限堆积。
            oldest = next(iter(_SESSION_TARGETS))
        else:
            oldest = min(cands, key=_SESSION_LAST_USED.get)
        tid = _SESSION_TARGETS.pop(oldest, None)
        _SESSION_LAST_USED.pop(oldest, None)
        _save_session_targets()
        try:
            pg = _find_page_by_target_id(browser, tid)
            if pg is not None:
                pg.close()
        except Exception:  # noqa: BLE001 — 关闭失败不影响主流程
            pass
        print(
            f"[CHROME_BRIDGE] 会话池超过上限 {_SESSION_MAX}，"
            f"已关闭最久未用的会话: {oldest}",
            flush=True,
        )


# goto 失败重试：本机必须开着系统代理才能访问 aistudio.google.com，链路偶发
# net::ERR_CONNECTION_CLOSED / ERR_TIMED_OUT / ERR_NETWORK_CHANGED。旧实现一次
# goto 失败就整次调用崩掉（实测 NWW 侧报 "Page.goto: net::ERR_CONNECTION_CLOSED"，
# 用户只能手工重试）。这里做线性退避重试，偶发抖动自愈。
_GOTO_ATTEMPTS = 3
_GOTO_BACKOFF_S = (2.0, 5.0)


def _goto_studio(page) -> None:
    """导航到当前站点首页，网络抖动时退避重试。"""
    last: Exception | None = None
    for i in range(_GOTO_ATTEMPTS):
        try:
            page.goto(_STUDIO_HOME, wait_until="domcontentloaded")
            return
        except Exception as exc:  # noqa: BLE001 — 网络类异常统一重试
            last = exc
            if i < _GOTO_ATTEMPTS - 1:
                wait = _GOTO_BACKOFF_S[min(i, len(_GOTO_BACKOFF_S) - 1)]
                print(
                    f"[CHROME_BRIDGE] goto 失败（{type(exc).__name__}），"
                    f"{wait}s 后第 {i + 2} 次重试",
                    flush=True,
                )
                time.sleep(wait)
    raise RuntimeError(
        f"无法打开桥接站点页面（{_ACTIVE_SITE_KEY}，已重试 {_GOTO_ATTEMPTS} 次）: {last}。"
        f"请检查系统代理是否开启、Chrome 是否可访问 {_STUDIO_HOME}"
    ) from last


def _try_click_new_chat(page):
    """尝试点一次 New chat 按钮，返回**三态**（区分"没渲染"和"点不动"很关键）。

    Returns:
        True  = 真的点到了
        False = 候选存在但 disabled / 点击失败 → **定论，立即停止轮询**。
                这是 2026-09-25 实测病根：前两个候选命中同一按钮，旧实现挨个
                click 各空等 30s 默认超时 → 单次调用白耗 60s。
        None  = 一个候选都还没渲染出来（goto 后 SPA 尚在渲染）→ 调用方继续轮询。
    """
    for sel in _NEW_CHAT_SELECTORS:
        try:
            btn = page.locator(sel).first
            if btn.count() == 0:
                continue
        except Exception:  # noqa: BLE001 — 单个候选异常继续尝试下一个
            continue
        try:
            if btn.is_enabled():
                btn.click(timeout=1500)
                page.wait_for_timeout(1500)
                return True
        except Exception:  # noqa: BLE001 — 点击失败不阻断，交由下游 ready 检查
            pass
        return False
    return None


def _poll_click_new_chat(page, budget_s: float = 5.0) -> bool:
    """轮询等待 New chat 按钮渲染并点击（上限 budget_s），返回是否点到。

    goto 之后 SPA 尚在渲染，**一次性定位会漏掉按钮**而静默跳过（2026-09-25 实测：
    新会话未真正开启 → 上下文串味），所以无论走不走 goto 都必须轮询。
    """
    deadline = time.monotonic() + budget_s
    # 迭代硬上限（5s/500ms 正常只需 10 次）：时钟被 mock 或系统时钟跳变时，
    # `monotonic() < deadline` 可能永不成立 → 死循环把整个桥接锁死。留 64 次封顶。
    for _ in range(64):
        if time.monotonic() >= deadline:
            break
        res = _try_click_new_chat(page)
        if res is not None:      # True/False 都是定论，不再空等
            return bool(res)
        page.wait_for_timeout(500)
    return False


def _new_session(page) -> None:
    """同一标签内开新会话：**默认 SPA 内点 New chat，不整页导航**。

    实测教训（2026-09-24）：new_chat 页是服务端共享会话，仅 goto 可能恢复
    本浏览器上次未命名对话的残留内容——必须显式点 New chat 保证干净上下文。
    实测（2026-09-25）：新版 UI 的 New chat 按钮为 aria-disabled=true，
    btn.click() 未传 timeout 时会空等 Playwright 默认 30s 动作性超时，且
    _NEW_CHAT_SELECTORS 前两个候选命中同一按钮 → 连续两次 30s，单次调用
    白耗约 60s（分段实测 T3 = 62.41s，占单次调用 73.9s 的 84%）。
    故：命中首个存在的候选即定论（不再试同按钮的子串候选）；点击前先判
    is_enabled，disabled 直接跳过；click 收紧到 1.5s。

    ★2026-09-29 性能改造（开关 `NWW_BRIDGE_SPA_NEWCHAT=0` 秒退回旧行为）：
      旧实现无条件 `_goto_studio(page)` —— 每轮把整个 Gemini SPA 从零加载一遍
      （实测好网 2.32s；慢网下 Playwright 默认 30s 导航超时并退避重试 2 次，
      backend.log 实证），这正是用户反馈的「每次发任务都先空窗白屏」。
      页面已就绪时 SPA 内点 New chat 只要 0.13s 且**完全不白屏**。

      ⚠️ 不能无脑省掉 goto：New chat 按钮可能因 UI 改版而定位不到，此时页面
      还留着上一轮对话 → 上下文串味。故兜底是「没点到按钮 **且** 页面上确有
      历史轮次」才退回整页 goto —— **宁可慢，不可脏**。turn_count == 0 说明
      本来就是空会话，不必为它付冷启动。
    """
    # 页面是否已在当前站点且输入框已渲染 → 是就不必整页导航
    ready = False
    if _SPA_NEWCHAT:
        try:
            ready = (not _url_mismatch(page)) and (
                _locate(page, _PROMPT_SELECTORS) is not None
            )
        except Exception:  # noqa: BLE001 — 判定失效按"不就绪"处理（退回 goto）
            ready = False

    if not ready:
        # 页面不在站点/输入框都没渲染 → 只能整页导航（冷启动是最后手段）
        _goto_studio(page)
        _poll_click_new_chat(page)
        return

    # 页面已就绪：SPA 内点 New chat，**不整页导航**
    if _poll_click_new_chat(page):
        return

    # 没点到按钮：SPA 路径没能保证干净上下文 —— 看页面脏不脏再决定兜底
    try:
        dirty = _turn_count(page) > 0
    except Exception:  # noqa: BLE001 — 取不到轮次按"脏"处理，走 goto 兜底
        dirty = True
    if dirty:
        print(
            "[CHROME_BRIDGE] New chat 按钮未命中且页面留有历史轮次，"
            "退回整页 goto 以保证上下文干净",
            flush=True,
        )
        _goto_studio(page)
        _poll_click_new_chat(page)
    # 空会话（dirty=False）：沿用当前页面，与旧行为一致，不阻断主流程


def _locate(page, selectors: tuple[str, ...]):
    """按候选链找第一个存在的元素定位器；找不到返回 None。"""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                return loc
        except Exception:  # noqa: BLE001 — 单个候选异常继续尝试下一个
            continue
    return None


def _wait_for_prompt_box(page, timeout_s: float = 30.0):
    """轮询等待输入框渲染完成（goto 后 SPA 需数秒，一次性定位会漏）。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        box = _locate(page, _PROMPT_SELECTORS)
        if box is not None:
            return box
        page.wait_for_timeout(500)
    return None



def _send_prompt(page, message: str) -> None:
    """填入 prompt 并触发发送。

    注入方式（2026-09-26 改为物理优先）：
      旧实现只有 `box.fill(message)` —— 那是**直接给 DOM 元素赋 value 再派发一个
      input 事件**，属于程序化改写，不经过浏览器输入管线（没有 beforeinput /
      composition / 真实按键序列）。人工敲同样的内容完全正常、桥接发就报
      "An internal error has occurred." 时，这条差异是首要嫌疑，故改为
      `page.keyboard.insert_text()`（CDP Input.insertText，语义等同 IME 上屏，
      事件链更接近真人），并**以输入框实际拿到的内容为准**做校验，失败才退回 fill。

    提交方式：真实回车优先（页面提示 "Run ⏎"，键盘事件而非合成点击），
    以"输入框是否清空"为准校验；未清空再退回点 Run 按钮、最后 Control+Enter。

    回滚开关：NWW_BRIDGE_PHYSICAL_SEND=0 时恢复旧的 fill + 点按钮行为。
    """
    box = _wait_for_prompt_box(page)
    if box is None:
        # 页面崩溃/关闭时 url/title 读取本身会抛错，绝不能让诊断信息掩盖主错误
        try:
            where = f"{page.url}（标题: {page.title()}）"
        except Exception:  # noqa: BLE001
            where = "<页面已崩溃或关闭>"
        raise RuntimeError(
            f"未找到 {_ACTIVE_SITE_KEY} 输入框（页面结构可能已变更）。"
            f"当前页面: {where}。"
            f"请在 Chrome 手动打开 {_STUDIO_HOME} 确认页面形态后重试"
        )

    if _PHYSICAL_SEND:
        mode = _inject_prompt_physical(page, box, message)
    else:
        box.fill(message)
        mode = "fill（已由 NWW_BRIDGE_PHYSICAL_SEND=0 指定）"
    # 提问长度是 internal error 的头号相关变量，落日志便于事后归因
    print(f"[CHROME_BRIDGE] 提问 {len(message)} 字符（{mode}）", flush=True)

    if not _PHYSICAL_SEND:
        btn = _locate(page, _RUN_SELECTORS)
        if btn is not None:
            try:
                if btn.is_enabled():
                    btn.click()
                    return
            except Exception:  # noqa: BLE001 — 点击失败退回快捷键
                pass
        box.press("Control+Enter")
        page.wait_for_timeout(800)
        try:
            if box.input_value().strip():
                box.press("Enter")
        except Exception:  # noqa: BLE001 — 读取失败不阻断，交给后续轮询判超时
            pass
        return

    # ── 物理提交：真实回车优先 ──
    try:
        box.press("Enter")
        page.wait_for_timeout(800)
        if _input_cleared(box):
            return
    except Exception:  # noqa: BLE001 — 回车失败退回点按钮
        pass
    # 兜底 1：点 Run 按钮（旧主路径）
    btn = _locate(page, _RUN_SELECTORS)
    if btn is not None:
        try:
            if btn.is_enabled():
                btn.click(timeout=3000)
                page.wait_for_timeout(500)
                if _input_cleared(box):
                    return
        except Exception:  # noqa: BLE001
            pass
    # 兜底 2：Control+Enter（旧版约定）
    try:
        box.press("Control+Enter")
    except Exception:  # noqa: BLE001 — 读取失败不阻断，交给后续轮询判超时
        pass


def _inject_prompt_physical(page, box, message: str) -> str:
    """经浏览器输入管线注入提问（CDP Input.insertText），失败退回 fill。

    Returns:
        "物理注入" 或 "fill 兜底"（仅用于日志归因）。
    """
    try:
        box.fill("")                 # 清空（确定性操作，不涉及输入管线）
        box.click(timeout=3000)      # 确保焦点落在输入框，insert_text 才会落进去
        page.keyboard.insert_text(message)
        page.wait_for_timeout(300)
        got = _box_text(box)
        # 只要求拿到绝大部分：超长文本可能被前端做轻微规范化（换行/空白）
        if len(got) >= max(1, int(len(message) * 0.9)):
            return "物理注入"
        print(
            f"[CHROME_BRIDGE] 物理注入只拿到 {len(got)}/{len(message)} 字符，退回 fill",
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001 — 注入失败一律退回 fill
        print(f"[CHROME_BRIDGE] 物理注入失败（{type(exc).__name__}），退回 fill", flush=True)
    box.fill(message)
    return "fill 兜底"


def _box_text(box) -> str:
    """读输入框当前内容（跨站点：textarea 与 contenteditable 取值方式不同）。

    Gemini 消费端的输入框是 Quill 富文本 `div.ql-editor[contenteditable]`，
    **没有 value 属性**，`input_value()` 直接抛错 —— 一律走 inner_text()。
    AI Studio 是 textarea，优先 input_value()（更准，不受渲染影响）。
    """
    if _INPUT_KIND != "contenteditable":
        try:
            return box.input_value() or ""
        except Exception:  # noqa: BLE001 — textarea 读不到就退化到 inner_text
            pass
    try:
        return box.inner_text() or ""
    except Exception:  # noqa: BLE001
        return ""


def _input_cleared(box) -> bool:
    """输入框是否已清空（= 提问已真正发出的判据）。读不到就当已发出，交给下游判定。"""
    try:
        return not _box_text(box).strip()
    except Exception:  # noqa: BLE001
        return True


def _turn_locator(page):
    """轮次容器定位器（按当前站点绑定）。"""
    return page.locator(", ".join(_TURN_SELECTORS) or "ms-chat-turn")


def _turn_count(page) -> int:
    """当前页面轮次节点数（基线/提交判据用）。取不到按 0 处理。"""
    try:
        return _turn_locator(page).count()
    except Exception:  # noqa: BLE001
        return 0


def _url_mismatch(page) -> bool:
    """当前页面是否**不在**当前站点（站点刚切换过，页面还没跟过去）。

    为什么必须查：会话复用（reuse=True）分支不会调 _new_session，页面就一直停在
    上一个站点。站点切换后所有选择器（输入框/轮次/正文）都是新站点的，落在旧
    页面上一个都命中不了 —— 表现为"切了站点但依然失败"，而且失败原因被藏起来。
    取不到 URL 时按"匹配"处理（不额外导航，交下游判定）。
    """
    site = _SITES.get(_ACTIVE_SITE_KEY)
    if site is None:
        return False
    try:
        url = page.url or ""
    except Exception:  # noqa: BLE001 — 页面崩溃/关闭时读 url 会抛
        return False
    return site.url_hint not in url


class TurnCandidate(NamedTuple):
    """单个 turn 的正文候选（P0 正文候选评分模型的输入）。

    Attributes:
        text: 该 turn 内最后一个非空正文节点文本（空 turn 为 ""）。
        position: turn 在页面上的顺序下标（越大越新）。
        role_hint: "user" / "model" / ""（无角色标记，实测答案 turn 可能是这种）。
        is_thought: turn 是否为思考（Thoughts）区块，思考草稿不得当正文交付。
        is_error: turn 是否命中服务端错误占位标记。
        length: text 长度（过滤 UI 徽章/空节点残留）。
    """

    text: str
    position: int
    role_hint: str
    is_thought: bool
    is_error: bool
    length: int


def _turn_scan(page) -> list[tuple[bool, str, str]]:
    """一次 evaluate 扫描全部轮次节点，返回 [(is_thought, text_outside, role), ...]。

    结构性判据（外脑评审 #4 隐患 3）：turn 内存在思考容器 **且** 没有任何正文节点
    位于思考容器之外 ⇒ 纯思考 turn（True）；否则即使头部印着 "Thought for 5s"
    也判 False —— 因为那是「思考 + 正文同 turn」，正文必须能取到。

    第二个返回值 text_outside = 该 turn 内**位于思考容器之外**的首个非空正文节点
    文本。为什么需要它：思考面板在 DOM 里排在正文之前，只取「首个非空 cmark」
    会拿到思考草稿当正文交付（合成 DOM 实测：turn1 正文被取成"推理草稿：应回答 42"）。
    取不到时返回空串，调用方回退到原 Python 取值路径，行为不变。

    第三个返回值 role = "user" / "model" / ""。判定方式由站点决定：
      - Gemini（_ROLE_BY_TAG）：按标签名 user-query / model-response，明确无歧义；
      - AI Studio：只有 ms-chat-turn 一种标签，只能看 turn 头部徽章文本。

    ⚠️ 选择器全部由 Python 侧按站点传入（不再写死在 JS 里）：Gemini 的轮次容器是
    user-query / model-response、正文节点是 .markdown-main-panel，写死就永远扫不到。
    """
    js = """
    (args) => {
      const turnSel = args.turnSel, thoughtSels = args.thoughtSels;
      const bodySels = args.bodySels, roleByTag = args.roleByTag;
      const turns = document.querySelectorAll(turnSel || 'ms-chat-turn');
      const out = [];
      for (const t of turns) {
        let hasTh = false;
        for (const s of thoughtSels) { if (t.querySelector(s)) { hasTh = true; break; } }
        let outside = 0, firstOutside = '';
        const nodes = t.querySelectorAll(bodySels.join(', '));
        for (const n of nodes) {
          let inTh = false;
          if (hasTh) {
            for (const s of thoughtSels) {
              const a = n.closest(s);
              if (a && t.contains(a)) { inTh = true; break; }
            }
          }
          if (!inTh) {
            outside++;
            const txt = (n.innerText || '').trim();
            if (txt && !firstOutside) { firstOutside = txt; }
          }
        }
        let role = '';
        const head = (t.innerText || '').slice(0, 120).toLowerCase();
        if (roleByTag) {
          const tag = (t.tagName || '').toLowerCase();
          if (tag === 'user-query') { role = 'user'; }
          else if (tag === 'model-response') { role = 'model'; }
        }
        if (!role) {
          role = head.indexOf('user') >= 0 ? 'user'
               : (head.indexOf('model') >= 0 ? 'model' : '');
        }
        out.push([hasTh && outside === 0, firstOutside, role]);
      }
      return out;
    }
    """
    try:
        raw = page.evaluate(js, {
            "turnSel": ", ".join(_TURN_SELECTORS) or "ms-chat-turn",
            "thoughtSels": list(_THOUGHT_SELECTORS),
            "bodySels": list(_BODY_SELECTORS),
            "roleByTag": bool(_ROLE_BY_TAG),
        })
    except Exception:  # noqa: BLE001 — 页面切换瞬间可能失效，交由调用方回退
        return []
    if not isinstance(raw, list):
        return []
    out: list[tuple[bool, str, str]] = []
    for item in raw:
        if isinstance(item, (list, tuple)) and len(item) >= 3:
            out.append((bool(item[0]), str(item[1] or ""), str(item[2] or "")))
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            # 兼容旧形态（无 role）：交由调用方按标签文本兜底判定
            out.append((bool(item[0]), str(item[1] or ""), ""))
        else:
            out.append((False, "", ""))
    return out


def _turn_thought_flags(page) -> list[bool]:
    """结构性判定每个 ms-chat-turn 是否为「纯思考 turn」，返回按页面顺序的布尔表。

    为什么不用 head 文本 heuristics（旧实现 `"thought" in head`）：
      AI Studio 把思考与正文**渲染进同一个 turn** 时，turn 头部同样印着
      "Model 10:20 Thought for 5 seconds" —— 文本判定会把这条含正文的 turn
      整条判成思考，`_latest_output_text` 的 `not c.is_thought` 于是把正文
      整段过滤掉，只能静默拖到硬超时（外脑评审 #4 隐患 3）。

    结构性判据：turn 内存在思考容器 **且** 没有任何正文节点位于思考容器之外。
      - 纯思考 turn   → 思考容器有、容器外无正文节点 ⇒ True（不得当正文交付）
      - 思考+正文同 turn → 容器外有正文节点           ⇒ False（正文必须能取到）
    取不到（页面改版 / evaluate 失败）时返回空表，调用方回退到文本判定。
    """
    return [item[0] for item in _turn_scan(page)]


def _collect_turn_candidates(page, baseline: int = 0) -> list[TurnCandidate]:
    """遍历 ms-chat-turn 收集正文候选（按页面顺序，一个 turn 一条）。

    Args:
        baseline: 轮次基线 —— 下标小于 baseline 的 turn 属本轮**之前**的历史轮次，
            一律跳过。**会话复用时必须传**：否则新答复尚未渲染时，
            `eligible[-1]` 会选中上一轮的旧回答，把历史答案直接当本轮结果返回。
            隔离模式（每次 new session）页面里只有本轮 turn，基线恒为 0。
    """
    turns = _turn_locator(page)
    n_turns = turns.count()
    # 结构性扫描优先（见 _turn_scan 文档）；取不到才回退文本判定。
    scan = _turn_scan(page)
    out: list[TurnCandidate] = []
    for i in range(max(0, baseline), n_turns):
        turn = turns.nth(i)
        full = turn.inner_text() or ""
        low = full.lower()
        head = low[:120]  # 只看头部徽章区：正文内容不在 turn.inner_text 里
        # 角色：优先用扫描结果（Gemini 按标签名，无歧义），取不到才看头部文本
        role = scan[i][2] if i < len(scan) else ""
        if not role:
            role = "user" if "user" in head else ("model" if "model" in head else "")
        is_thought = scan[i][0] if i < len(scan) else ("thought" in head)
        outside_text = scan[i][1] if i < len(scan) else ""
        # P0-3：不再对整段 turn.inner_text()（含模型正文）做子串匹配判错，
        # 改为在取到正文后按「标记词 + 正文很短」双条件判定，见下方。
        is_err = False
        # 取 turn 内【最外层】非空正文节点 = 完整正文。
        # 嵌套 ms-cmark-node：最外层 inner_text 含全部子孙（= 完整答复），
        # 最深层只是叶子碎片。旧实现 range(count-1,-1,-1) 反向遍历落到叶子上，
        # 实测 1049 字正文只回 9 字（P0-1）。改为正向取 outermost。
        #
        # 快路径：_turn_scan 在同一次 evaluate 里已算出「思考容器之外的首个非空
        # 正文节点」文本（实测 6.8ms，而下方 locator 往返约 204ms；轮询间隔仅
        # 500ms，取值开销原本已超过间隔本身）。二者在 6 个真实会话 / 49 个 turn
        # 上逐条对比 0 不一致 → 有值即用。
        # 同时它天然修掉「思考 + 正文同 turn」：思考面板在 DOM 里排在正文之前，
        # 只取首个非空节点会拿到思考草稿当正文（合成 DOM 已复现）。
        # 纯思考 turn（全部 cmark 都在思考容器内）时它为空 → 走下方慢路径，
        # 行为与改动前完全一致。
        text = outside_text
        if not text:
            for sub in _BODY_SELECTORS:
                loc = turn.locator(sub)
                for j in range(loc.count()):
                    got = (loc.nth(j).inner_text() or "").strip()
                    if got:
                        text = got
                        break
                if text:
                    break
        # 错误占位判定：必须同时满足「命中错误标记」与「正文很短」。
        # 旧实现直接对整个 turn.inner_text()（含模型正文）做子串匹配，
        # 导致正文里出现这些词即被误判为服务端错误并触发重发链（实测 4/4 误杀）。
        if text and len(text) <= _ERROR_PLACEHOLDER_MAX_LEN:
            if any(m in text.lower() for m in _ERROR_MARKERS):
                is_err = True
        out.append(TurnCandidate(
            text=text,
            position=i,
            role_hint=role,
            is_thought=is_thought,
            is_error=is_err,
            length=len(text),
        ))
    return out


def _latest_output_text(page, baseline: int = 0) -> str | None:
    """抓取最新模型回复正文；尚未生成/生成中/仅思考/错误占位返回 None。

    实测（2026-09-25）两代取文缺陷，勿回退：
      1) 旧实现按"turn 头部含 Model"过滤 → 答案落在无角色标记的 turn 时被整段
         漏取，只能空等到硬超时（实测 180s 空等，答案其实早已渲染）；
      2) 思考（Thoughts）区块的草稿不得当作正式正文交付。
    故改为"正文候选评分"：取最后一个「非空 + 非 user turn + 非 Thoughts +
    非 error + 长度 ≥ _MIN_CANDIDATE_LEN」的候选；含 Model 仅作弱提示，
    不再作为过滤条件（无角色标记的答案 turn 必须能被取到）。
    """
    try:
        cands = _collect_turn_candidates(page, baseline=baseline)
    except Exception:  # noqa: BLE001 — 页面切换瞬间可能失效，交给轮询重试
        cands = []
    if cands:
        eligible = [
            c for c in cands
            if c.text
            and c.role_hint != "user"
            and not c.is_thought
            and not c.is_error
            and c.length >= _MIN_CANDIDATE_LEN
        ]
        if eligible:
            return eligible[-1].text  # 候选已按页面顺序，最后一条即最新回答
        # 无可用正文：仍在生成 / 仅思考内容 / 错误占位（由 _latest_turn_has_error 定夺）
        return None
    # fallback：通用候选链（页面改版时的兜底路径）
    for sel in _OUTPUT_SELECTORS:
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                text = (loc.nth(loc.count() - 1).inner_text() or "").strip()
                if text:
                    if any(m in text.lower() for m in _ERROR_MARKERS):
                        return None  # 错误占位不得当正文交付
                    return text
        except Exception:  # noqa: BLE001
            continue
    return None


def _clean_thought_text(txt: str) -> str:
    """过滤"看起来像界面装饰语"的思考文本，取不到真推理就宁可返回空。"""
    t = (txt or "").strip()
    if len(t) < 4:
        return ""
    low = t.lower()
    if any(m in low for m in _THOUGHT_UI_MARKERS):
        return ""
    return t


def _latest_thought_text(page, baseline: int = 0) -> str:
    """抓取最新「思考（Thoughts）」区块文本；无思考区返回 ""。

    刻意与 _latest_output_text 共用同一套候选：正文分拣取「非 is_thought」，
    这里专取 is_thought=True 的一支 —— 复用已验证的取值路径，不再自造选择器
    （否则 DOM 改版会再踩一次 P0-1 那种"取到最深层叶子"的坑）。

    为什么必须单独开这条路：旧逻辑把思考区块仅当作「不得当正文交付」的干扰过滤掉
    （_latest_output_text 里 `not c.is_thought`），等于思考内容对上层永久不可见。
    实际上它是可有价值的独立输出通道，应交给上层映射为 reasoning_content。
    """
    # ① 元素级优先：直接在基线之后的最后一个 turn 里找独立思考容器。
    #    思考与正文同 turn 时这一步能正交地只取出思考部分，不吃到正文。
    try:
        turns = _turn_locator(page)
        n = turns.count()
        if n > baseline:
            last_turn = turns.nth(n - 1)
            for sel in _THOUGHT_SELECTORS:
                loc = last_turn.locator(sel)
                if loc.count() > 0:
                    txt = _clean_thought_text(loc.first.inner_text() or "")
                    if txt:
                        return txt
    except Exception:  # noqa: BLE001 — 页面切换瞬间可能失效
        pass
    # ② 兜底：回退到候选器里被标记为 thought 的 turn（选择器未命中时仍能取到）
    try:
        cands = _collect_turn_candidates(page, baseline=baseline)
    except Exception:  # noqa: BLE001 — 页面切换瞬间可能失效
        cands = []
    for cand in reversed(cands):
        if cand.text and cand.is_thought and not cand.is_error:
            txt = _clean_thought_text(cand.text)
            if txt:
                return txt
    return ""


# ★协议块豁免（2026-09-26）：EXECUTE_JSON / APPLY_JSON / READ_JSON 的输出**天然**与
# prompt 里的格式样例高度重合 —— 模型严格按格式输出正是我们要的，但回显判定会把它
# 当成"回吐提问原文"（实测：78 字测试提问 → 回复就是一个协议块 → 覆盖率 100% →
# 判污染 → 触发 30s+45s 冷却重发 → 最终报"已接收但始终未开始生成"，而答案其实已生成）。
# 故回显判定必须先豁免有效协议块。这里只做"标记 + 紧跟 JSON 起始符"的轻量判据，
# 不 import app.agent（避免循环依赖），JSON 合法性由上层解析器把关。
_PROTOCOL_MARKERS_RE = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]*)?(?:\*\*)?[ \t]*"
    r"(?:EXECUTE_JSON|APPLY_JSON|READ_JSON)[ \t]*(?:\*\*)?[ \t]*"
    r"(?![A-Za-z])[ \t]*:?[ \t]*\r?\n?",
    re.MULTILINE | re.IGNORECASE,
)


def _has_protocol_block(text: str) -> bool:
    """文本是否含「协议标记 + 紧跟 JSON」块（有则不得判为回显污染）。"""
    m = _PROTOCOL_MARKERS_RE.search(text or "")
    if not m:
        return False
    rest = re.sub(r"^[ \t\r\n]*(?:```[A-Za-z0-9_+-]*[ \t]*\r?\n?)?", "",
                  (text or "")[m.end():])
    return rest[:1] in ("[", "{")


def _is_echo_polluted(text: str, prompt: str) -> bool:
    """判断输出是否把提问原文回吐（回显污染）。

    实测（2026-09-25）：上游退化输出时，末个模型 turn 的正文 = 短答复 + 提问原文
    （47 字 = 12 + 35），两次独立 run md5 完全一致——该文本不是可用正文。

    判据（2026-09-25 二期重写，旧判据误杀率过高）：
      ① 输出开头 _ECHO_PREFIX_RATIO(30%) 内出现与 prompt 连续匹配的片段；
      ② 且该片段长度 ≥ _ECHO_MIN_MATCH(20)；
      ③ **且**该片段覆盖输出正文的 ≥ _ECHO_COVER_RATIO(50%)。

    为什么必须加 ③：旧判据只要"任意 20 字命中"就判污染，而 NWW 的提示词又长又
    含书名/章节名/`<execute>` 样例，模型正经回答开头复述一句
    "全面重构《用泡面征服修仙界》第29章《冷水自沸，神仙懵了》"就必然命中 20 字 ——
    实测每次都触发 30s+45s 冷却重发链，最终报"连续返回内部错误"，而网页上答案
    其实已经生成。真污染的特征是"答复 ≈ 提问原文"（实测 47 字里 35 字来自提问，
    覆盖率 74%），不是"引用了几个词"。

    另：调用方只在**最终定稿**时调用本函数（流式首块常只有十几字，逐块判定必误杀）。
    """
    t = (text or "").strip()
    p = (prompt or "").strip()
    if len(t) < _ECHO_MIN_MATCH or len(p) < _ECHO_MIN_MATCH:
        return False
    # ★协议块豁免：含有效协议标记+JSON 的输出一律不判污染（理由见 _has_protocol_block）。
    #   否则"模型严格按格式派单"会被当成"回吐提问"而走冷却重发，把正确答案丢掉。
    if _has_protocol_block(t):
        return False
    limit = int(len(t) * _ECHO_PREFIX_RATIO)
    best = 0
    for i in range(0, min(limit, len(t) - _ECHO_MIN_MATCH) + 1):
        if t[i:i + _ECHO_MIN_MATCH] in p:
            # 命中起点后再尽量向后延伸，求最长连续命中长度
            j = _ECHO_MIN_MATCH
            while i + j < len(t) and t[i:i + j + 1] in p:
                j += 1
            if j > best:
                best = j
    if best < _ECHO_MIN_MATCH:
        return False
    return best >= _ECHO_COVER_RATIO * len(t)


def _latest_turn_has_error(page, baseline: int = 0) -> bool:
    """末个对话 turn 是否为 AI Studio 的错误占位（而非正常正文）。

    实测（2026-09-25）：错误 turn 的文本形如
    "edit more_vert Model 08:35 error An internal error has occurred."，
    且不会自愈——故据此判定"需重发"，而不是继续空等。

    ⚠️ P0-3 补齐（2026-09-25 二期）：必须叠加「文本很短」辅助判据，与
    _collect_turn_candidates 保持一致。旧版这里只做子串匹配、没有长度护栏，
    于是**长文本里只要出现 try again / something went wrong 就被判成错误占位**：
    带思考的模型推理了几百秒，思考草稿里一句 "let me try again" 就足以触发
    30s+45s 冷却重发链；重发耗尽后抛"连续返回内部错误"，而此刻网页上**答案其实
    已经生成**（用户实测：WebUI 里有完整正文，桥接却报错放弃）。
    错误占位是短句（实测约 55 字），长文本一律不认。
    """
    try:
        turns = _turn_locator(page)
        n = turns.count()
        # 会话复用时不能看基线之前的 turn：那是历史轮次（可能含有历史错误占位），
        # 否则一进新回合就被历史错误 turn 短路到重发链。
        if n <= baseline:
            return False
        txt = turns.nth(n - 1).inner_text() or ""
        if len(txt) > _ERROR_PLACEHOLDER_MAX_LEN:
            return False   # 长文本 = 正常内容，不是错误占位
        return any(m in txt.lower() for m in _ERROR_MARKERS)
    except Exception:  # noqa: BLE001 — 页面切换瞬间可能失效，按"无错误"处理
        return False


def _latest_turn_quota_exceeded(page, baseline: int = 0) -> bool:
    """末个 turn 是否为**配额/用量上限**占位（而非普通内部错误）。

    判据与 _latest_turn_has_error 完全同源（短文本护栏 + 站点专属标记表），
    只把标记表换成 _QUOTA_MARKERS。两者必须分开查：命中配额要**换站点**，
    命中内部错误只该**原地重发**——合并查就分不出处置动作。

    ⚠️ 短文本护栏同样必要：NWW 正文本身可能出现「额度」「配额」这类词，
    无长度门槛会把一段正常正文误判成"配额耗尽"而触发切站点。
    """
    if not _QUOTA_MARKERS:
        return False   # 该站点未配配额判据 → 一律不切（宁可不切，不可乱切）
    try:
        turns = _turn_locator(page)
        n = turns.count()
        if n <= baseline:
            return False
        txt = turns.nth(n - 1).inner_text() or ""
        if len(txt) > _ERROR_PLACEHOLDER_MAX_LEN:
            return False
        return any(m in txt.lower() for m in _QUOTA_MARKERS)
    except Exception:  # noqa: BLE001
        return False


def _any_stop_control(page) -> bool:
    """页面上是否存在任意「停止生成」控件（扫描全部按钮的可见文案）。

    为什么需要这条兜底：Gemini 的停止按钮选择器不一定稳定（UI 版本差异大），
    而它的发送按钮在输入清空后就消失了 —— 一旦停止按钮没被选择器命中，
    _control_generating 会恒为 False，_iter_reply 的 started 永远置不上，
    只能一路干等到硬超时（900s）。这里按站点判据词做全页扫描，
    不依赖任何具体选择器，是最后一道防线。
    """
    js = """
    (markers) => {
      try {
        const els = document.querySelectorAll('button, [role="button"]');
        for (const el of els) {
          const lbl = (((el.getAttribute && el.getAttribute('aria-label')) || '')
            + ' ' + (el.innerText || el.textContent || '')).toLowerCase();
          for (let i = 0; i < markers.length; i++) {
            const m = markers[i];
            if (m && lbl.indexOf(String(m).toLowerCase()) >= 0) { return true; }
          }
        }
      } catch (e) { /* 页面切换瞬间可能失效 */ }
      return false;
    }
    """
    try:
        return bool(page.evaluate(js, list(_STOP_MARKERS)))
    except Exception:  # noqa: BLE001 — 没有 evaluate（假页面）/页面失效 → 按"无"处理
        return False


def _control_generating(page) -> bool:
    """页面上是否存在「正在生成」的控制信号（Stop / 停止）。

    三级判据，按可靠性降序：
      ① 主按钮 label（AI Studio：Run ⏎ ↔ "progress_activity Stop"）—— 最可靠；
      ② 站点专属的独立停止按钮（Gemini：发送按钮消失后另起「停止」按钮）；
      ③ 全页按钮文案扫描（不依赖选择器，防 UI 改版）。
    """
    # ① _RUN_SELECTORS 首项即精确版 "ms-run-button button"，沿用其优先级顺序
    for sel in _RUN_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                label = (
                    (loc.get_attribute("aria-label") or "")
                    + " "
                    + (loc.inner_text() or "")
                ).lower()
                return any(m in label for m in _STOP_MARKERS)
        except Exception:  # noqa: BLE001 — 单个候选异常继续尝试下一个
            continue
    # ② 独立停止按钮
    for sel in _STOP_SELECTORS:
        try:
            if page.locator(sel).first.count() > 0:
                return True
        except Exception:  # noqa: BLE001
            continue
    # ③ 文案扫描兜底
    return _any_stop_control(page)


def _run_ready(page) -> bool:
    """上一轮生成是否已结束（= 可发起下一次生成）。

    判据（2026-09-25 依上游 UI 实测修订）：
      生成中 Run 按钮 label = "progress_activity Stop"；结束后 = "Run ⏎"。
      故以 label 是否含 "stop" 为唯一权威信号（跨站点后改为按 _STOP_MARKERS 匹配）。

    历史坑（两代问题，勿回退）：
      1) 初版仅用 is_enabled 判停 → 生成中按钮是 "Stop" 且仍可点击，会误判为已完成，
         长文本生成中途停顿 3s 即被截断；
      2) 修复版末行 return loc.is_enabled() → 新版 AI Studio 空闲态（输入框已清空）
         Run 按钮恒为 disabled，导致"生成完成"永远判不出来：实测回复已渲染、
         body 已出现 "Response ready"，本函数仍返回 False，全链路拖到硬超时并报
         "生成超时"（短文本也必现）。故 is_enabled 已不可用，仅保留 stop 判据。

    信号与失效兜底：找不到任何按钮时返回 True（退化为 _iter_reply 的纯 DOM 稳定判定）。
    """
    try:
        return not _control_generating(page)
    except Exception:  # noqa: BLE001 — 判定整体失效时保留旧语义（按"已结束"处理）
        return True


def _tail_turn_role(page, baseline: int = 0) -> str:
    """基线之后**最后一个**轮次节点的角色（"user"/"model"/""）。"""
    try:
        loc = _turn_locator(page)
        n = loc.count()
        if n <= baseline:
            return ""
        scan = _turn_scan(page)
        if n - 1 < len(scan):
            return scan[n - 1][2]
        # 扫描取不到：按站点标签名兜底
        return _tag_role(loc.nth(n - 1))
    except Exception:  # noqa: BLE001
        return ""


def _tag_role(el) -> str:
    """按元素标签名判角色（Gemini: user-query / model-response）。"""
    try:
        tag = (el.evaluate("e => (e.tagName || '').toLowerCase()") or "").strip()
    except Exception:  # noqa: BLE001
        return ""
    if tag == "user-query":
        return "user"
    if tag in ("model-response", "modelresponse"):
        return "model"
    return ""


def _generation_started(page, baseline: int = 0) -> bool:
    """本轮生成是否已启动（置 _iter_reply 的 started 硬证据）。

    主判据仍走 _run_ready（保持可 mock / 与既有测试语义一致）；额外补一条
    **按标签分角色的站点专用**信号：Gemini 的 model-response 节点一旦出现，
    就是"服务端已接手本轮"的 DOM 事实。

    ⚠️ 这条额外信号**只在 _ROLE_BY_TAG 站点启用**：AI Studio 只有 ms-chat-turn
    一种标签、头部徽章文本判角色不可靠，若在那里启用就等于把 P0-4/P0-5 的
    "必须见过 Stop 态"护栏拆掉（从未启动时也可能被误判为已启动）。
    """
    if not _run_ready(page):
        return True
    if _ROLE_BY_TAG and _tail_turn_role(page, baseline) == "model":
        return True
    return False


def _wait_generation_started(page, budget_s: float = 15.0, baseline: int = 0) -> bool:
    """发送后确认生成已真正启动（Run 按钮从 'Run ⏎' 切到 Stop 态）。

    Returns:
        True —— 已观察到生成中信号。

    ⚠️ 这个返回值必须传给 _iter_reply（started 初值），不能丢：二者之间有一次
    500ms 的轮询间隔，短答案完全可能在这中间生成完毕。若 _iter_reply 从
    started=False 重新起算，它再也等不到 Stop 态（已经结束了），完成条件
    `started and ...` 永远不成立 —— 明明答案已经渲染在页面上，却一路空转到硬
    超时（短文本必现）。这是 P0-4/P0-5 引入 started 护栏后新开的竞态窗口。

    P0-2 修复：_send_prompt 只点了发送，从不验证生成是否真的启动。
    实测（2026-09-25）：成功轮 Run→Stop 约 0.8s；失败轮 150s 始终为 'Run ⏎'
    （输入框未真正提交）。若直接进入 _iter_reply 轮询，会一路空转到硬超时才报错，
    或被误判为「已完成」而交付空/错内容。以「观察到 Stop」为启动的硬证据。

    失败分型（2026-09-25 23:15 修订，勿再合并）：
      超预算后**必须先判断提问到底提交成功没有**，否则会给出误导性诊断。
      实测反例（2026-09-25 23:12）：提问其实**已经入列** —— ms-chat-turn 数从 7
      涨到 8、应答容器已建出，但 AI Studio 侧从未进入生成：该 turn 连
      "Model xx:xx Thoughts" 标签都没有，页面无任何错误横幅、无 toast、无 spinner，
      末尾状态是 "Response ready."。此时报"输入框可能未真正提交"是**错的**，
      真因是服务端限流/额度触顶或内容侧静默拒绝。
        - 新 turn 已出现（count > baseline）→ 抛 _StudioNoGeneration，
          由 _iter_reply_with_resend 走既有冷却重发链（原实现直接 raise，
          把重发链整个绕过去了，只能人工重试）；
        - 新 turn 未出现             → 抛 RuntimeError，保留原「未提交」诊断。
    """
    deadline = time.monotonic() + budget_s
    while time.monotonic() < deadline:
        page.wait_for_timeout(_POLL_INTERVAL_MS)
        # 生成中判据按站点走（AI Studio 看 Run↔Stop；Gemini 看独立「停止」按钮）
        if _generation_started(page, baseline=baseline):
            return True
    # ---- 超预算：先分清「没提交」还是「提交了但服务端不生成」----
    submitted = False
    try:
        submitted = _turn_count(page) > baseline
    except Exception:  # noqa: BLE001 — 取不到就按"未提交"处理，保留原诊断
        submitted = False
    if submitted:
        # 错误占位可能回来得极快（实测 <15s），压根没进入过 Stop 态 —— 直接报
        # "未开始生成"是误诊（实测 2026-09-26 00:15：39.7k 提问 → Model 秒回
        # "An internal error has occurred."，Run 从未切到 Stop）。命中就按内部错误
        # 走，诊断准确；两者共用同一条重发链，处置动作不变。
        try:
            # ★先查配额再查内部错误：配额错误重发无用，必须换站点；
            #   顺序反了会把"配额耗尽"退化成"内部错误 → 原地重发到超时"。
            if _latest_turn_quota_exceeded(page, baseline=baseline):
                raise _StudioQuotaExceeded(
                    f"站点 {_ACTIVE_SITE_KEY} 返回配额/用量上限占位 → 切换备用站点"
                )
            if _latest_turn_has_error(page, baseline=baseline):
                raise _StudioErrorTurn("AI Studio 返回错误占位（internal error）")
        except _StudioErrorTurn:
            raise
        except Exception:  # noqa: BLE001 — 判定失效就退回"未开始生成"诊断
            pass
        raise _StudioNoGeneration(
            f"AI Studio 已接收提问但在 {budget_s:.0f}s 内未开始生成"
            "（Run 按钮始终未切到 Stop 态，应答 turn 为空）。"
            "常见原因是账户限流/额度触顶或内容侧静默拒绝 —— 这类失败页面上"
            "通常看不到任何错误提示，只有在 AI Studio 手动重试才能确认"
        )
    raise RuntimeError(
        "Chrome 桥接：发送后未检测到生成启动（Run 按钮未在 "
        f"{budget_s:.0f}s 内切到 Stop 态）。输入框可能未真正提交，"
        "可在 Chrome 手动确认 AI Studio 页面状态后重试"
    )



def _diff_gain(prev: str, cur: str) -> str:
    """求 prev -> cur 的流式增量。

    正常情形 cur 是 prev 的「前缀增长」，增量即尾部新增部分。
    但 DOM 重渲染 / 候选节点切换会让 cur 不再是 prev 的前缀 —— 此时若把整段 cur
    当增量发下去，已经投递过的部分会被**重复投递一遍**。
    实测（2026-09-25）：一句话回答被复制成了两份
    （"…网址（如 baidu.com）实" + "…网址（如 baidu.com）实时转换成…"）。
    故统一按最长公共前缀求增量：prev 与新文本重合的部分不再重发。
    """
    if cur.startswith(prev):
        return cur[len(prev):]
    if not prev:
        return cur
    n = 0
    m = min(len(prev), len(cur))
    while n < m and prev[n] == cur[n]:
        n += 1
    return cur[n:]


def _iter_reply(page, timeout: float, stream: bool, prompt: str = "", dual: bool = False,
                baseline: int = 0, stop_event: threading.Event | None = None,
                started: bool = False):
    """轮询生成结果。

    非流式：等待完成后一次性返回全文（yield 一个元素后结束）。
    流式：文本每增长一次 yield 一次增量。
    生成完成判定 = 输出文本连续 _STABLE_POLLS 次不变 且 Run 按钮恢复可用
                  且此前确实观察到过 Stop 态（started，见 P0-4/P0-5）。

    dual=True 切到「双通道」模式：yield 的不再是裸字符串，而是
    (kind, piece) 元组，kind ∈ {"thought", "text"}：
      - thought = AI Studio 思考（Thoughts）区块增量 → 上层映射为 reasoning_content
      - text    = 正式正文增量
    双通道一律增量输出（不再走"非流式一次性返回全文"分支）；dual=False 时
    行为与旧实现完全一致，现有调用不受影响。

        Args:
            prompt: 本轮提问原文；用于回显污染检测（P1），空串则跳过该检测。
            dual: 是否启用「思考 + 正文」双通道元组输出。
            baseline: 轮次基线，传给取文本/取思考的几个助手函数（见 _collect_turn_candidates）。
            stop_event: 外部中断信号（客户端 Ctrl+C / 读超时）。一旦被置位立即退出轮询。
                这是"并发假死"的命门：本函数在 `with _LOCK` 内运行，若客户端断开后
                这里仍在 page.wait_for_timeout 上慢慢磨到超时，全局锁就一直不释放，
                用户紧接着敲的下一条命令会卡在锁外直到上一轮跑完（数分钟）。
            started: 已由 _wait_generation_started 确认过的「本轮确实生成过」。
                必须传，否则短答案在两次轮询之间就生成完毕时，本函数再也等不到
                Stop 态 → 完成条件永不成立 → 明明有答案却空转到硬超时。
        """
    deadline = time.monotonic() + max(float(timeout or 0), 30.0)
    last_text = ""
    last_thought = ""
    stable = 0
    # started 初值来自上游确认结果（见参数文档）；False 时仍会在轮询中自己捕获。
    empty_done = 0       # 「已结束生成但仍无正文」的连续轮询计数（见 _EMPTY_GRACE_POLLS）
    warned_thought_only = False   # 思考-同 turn 告警只打一次，避免刷屏
    while time.monotonic() < deadline:
        # 客户端已断开：立刻收手。绝不能继续磨 —— 全局锁 _LOCK 正在外层持有，
        # 晚一秒释放，下一条请求就多卡一秒。
        if stop_event is not None and stop_event.is_set():
            print("[CHROME_BRIDGE] 检测到客户端已断开，立即终止轮询并释放锁", flush=True)
            return
        page.wait_for_timeout(_POLL_INTERVAL_MS)
        # P0-4/P0-5：把「观察到过 Stop」作为本轮真生成过的硬证据。
        # 否则从未启动时 DOM 也稳定、_run_ready 也为 True，会被误判为已完成，
        # 进而交付提问原文（实测 2.2s 就返回 prompt 的 49 字）。
        try:
            if _generation_started(page, baseline=baseline):
                started = True
        except Exception:  # noqa: BLE001 — 页面切换瞬间可能失效，忽略即可
            pass
        text = _latest_output_text(page, baseline=baseline)
        if dual:
            # 思考通道：与正文同一次轮询里独立取，作为 reasoning_content 的素材。
            # 只在 dual 模式下抓，避免普通调用白白多一次 DOM 遍历。
            thought = _latest_thought_text(page, baseline=baseline)
            # 正交性兜底：思考与正文互相包含 ⇒ 取到的其实还是正文（两者同 turn 时
            # 极易误取），丢弃。否则正文会被当成思考再投递一遍。
            if thought and text and (thought in text or text in thought):
                thought = ""
            if thought and thought != last_thought:
                yield "thought", _diff_gain(last_thought, thought)
                last_thought = thought
        if text is None:
            # 错误占位 turn：立即短路，交由 _iter_reply_with_resend 冷却重发
            # （实测 2026-09-25：服务端不自愈，继续等只会拖到硬超时）
            # 第二道闸：**生成中不得判错重发**。
            # 模型仍在推理时（_run_ready=False）页面只有思考 turn、正文为 None 是
            # 完全正常的状态。旧版此时仍会去查错误标记，一旦思考草稿里出现
            # "try again" 之类字眼就立刻重发 —— 在人家还在生成时重复提交是破坏性
            # 操作：污染上下文、白付 30s+45s 冷却、最终把已经生成的答案丢掉。
            generating = True
            try:
                generating = not _run_ready(page)
            except Exception:  # noqa: BLE001
                generating = True   # 取不到状态就按"仍在生成"保守处理
            if not last_text and not generating:
                if _latest_turn_quota_exceeded(page, baseline=baseline):
                    raise _StudioQuotaExceeded(
                        f"站点 {_ACTIVE_SITE_KEY} 返回配额/用量上限占位 → 切换备用站点"
                    )
                if _latest_turn_has_error(page, baseline=baseline):
                    raise _StudioErrorTurn("AI Studio 返回错误占位（internal error）")
            # 诊断：思考已出、正文迟迟为 None —— 极可能是"思考与正文同 turn"，
            # 该 turn 被 is_thought 命中后整条被正文过滤掉。旧版对此只能静默硬超时，
            # 这里留一条可检索的告警（只在 dual 模式下检查，避免无谓 DOM 开销）。
            if (dual and not last_text and not warned_thought_only
                    and _latest_thought_text(page, baseline=baseline)):
                warned_thought_only = True
                print(
                    "[CHROME_BRIDGE] 警告：已取到思考内容但正文为空 ——"
                    "疑似思考与正文同 turn，正文被 is_thought 过滤",
                    flush=True,
                )
            # 「生成已结束、应答 turn 却始终为空」= 静默空响应。
            # 实测（2026-09-26 00:0x）：Run 进入过 Stop（started=True），结束后应答
            # turn 只剩 edit/more_vert/thumb_up 徽章、0 个正文节点，页面上找不到任何
            # 错误文案 —— _latest_turn_has_error 判不出错，旧实现只能 continue 到硬
            # 超时（实测干等 900s）才报"生成超时"，用户白等 15 分钟还拿不到诊断。
            # 宽限 _EMPTY_GRACE_POLLS 次后判死，抛 _StudioNoGeneration 走重发链。
            if started and not generating:
                empty_done += 1
                if empty_done >= _EMPTY_GRACE_POLLS:
                    raise _StudioNoGeneration(
                        "AI Studio 已结束生成但应答 turn 始终为空"
                        "（无正文、无错误提示、无思考内容）。生成结束后又连续 "
                        f"{empty_done} 次轮询（约 "
                        f"{empty_done * _POLL_INTERVAL_MS / 1000:.0f}s）仍无内容，"
                        "多见于限流/额度触顶或内容侧静默拒绝"
                    )
            else:
                empty_done = 0
            continue  # 输出块未出现，继续等
        # 取到正文 ⇒ 不再是"空响应"状态
        empty_done = 0
        if text == last_text:
            stable += 1
            if stable >= _STABLE_POLLS and started and _run_ready(page):
                # 定稿却没有任何内容 ⇒ 同样是静默空响应。旧实现会静默 return 空串，
                # 上层只能报"未取得生成结果"，既不重发也没有诊断。这里统一判死。
                if not dual and not (text or "").strip():
                    raise _StudioNoGeneration(
                        "AI Studio 生成已定稿但正文为空（0 长度），判定为静默空响应"
                        "（多见于限流/额度触顶或内容侧静默拒绝）"
                    )
                # 回显污染只在**最终定稿**时判定。旧实现对每个流式分块都判，
                # 而首块往往只有十几字、且常是复述提问里的章节名/书名 ——
                # 覆盖率天然接近 100%，必然被误杀（实测每次都触发重发链）。
                if prompt and _is_echo_polluted(text, prompt):
                    # ★2026-09-27 根治：不再判死，改为警告 + 正常交付。
                    #
                    # 误杀实证（生产日志）：模型回确认语
                    #   「已确认：对《用泡面征服修仙界》第25章执行去AI味保守润色。」
                    # 被判回显 → 30s+45s 冷却重发 → 常以 502「执行失败」收场。
                    #
                    # 为什么不能靠调阈值解决：真污染样本同样是**极短输出**
                    # （47 字 = 12 字答复 + 35 字提问原文，覆盖率 74%），与确认语在
                    # 「覆盖率 / 长度」两个维度上完全重叠 ⇒ **任何阈值都无法干净区分**，
                    # 调高必漏真污染、调低必误杀确认语。
                    #
                    # 代价不对称 ⇒ 决策：误杀 = 整单失败(502) + 白等 75s；
                    #             漏检 = 交付一段可疑文本，**不落盘**（revise/rewrite
                    # 另有只增不删 + diff 兜底闸）、用户可见可重试。
                    # 故保留判定用于诊断，但不再中断链路。
                    print(
                        "[CHROME_BRIDGE] 警告：疑似回显污染（已交付、不判死），正文前 80 字: "
                        f"{text[:80]!r}",
                        flush=True,
                    )
                # 双通道一律增量输出，不走"非流式一次性全文"分支
                if not stream and not dual and last_text:
                    yield last_text
                return
        else:
            if stream or dual:
                # 单调递增检查：正常应为前一次的「前缀增长」。出现跳变说明取到了
                # 脏/错节点（重渲染/换候选），记警以便定位；增量本身交给
                # _diff_gain 按最长公共前缀裁剪，避免重复投递已发内容。
                if last_text and not text.startswith(last_text):
                    print(
                        "[CHROME_BRIDGE] 检测到非单调递增输出"
                        "（疑似取值节点跳变/碎片），已按最长公共前缀裁剪增量",
                        flush=True,
                    )
                gain = _diff_gain(last_text, text)
                yield ("text", gain) if dual else gain
            last_text = text
            stable = 0
    raise RuntimeError(f"Chrome 桥接生成超时（{timeout}s）。可稍后重试或在 Chrome 中检查页面状态")


class _StudioErrorTurn(RuntimeError):
    """AI Studio 返回错误占位 turn（内部错误/回显污染）：需走恢复链，而非继续空等。"""


class _StudioNoGeneration(_StudioErrorTurn):
    """提问已入列（新 turn 已出现）但 AI Studio 从未进入生成（无 Stop 态、应答为空）。

    与 _StudioErrorTurn 的区别：后者是"生成了，但产出的是错误占位"；
    本类是"压根没生成"。两者都该走同一条冷却重发链，故继承以便统一捕获。
    """


class _StudioQuotaExceeded(_StudioErrorTurn):
    """★配额/用量上限（2026-09-26 新增）：重发多少次都不会好，**只能换通道**。

    为什么单独一档而不是并进 _StudioErrorTurn：后者由 _iter_reply_with_resend
    就地冷却重发（内部错误多半自愈）；配额错误重发纯属浪费 timeout 预算，
    必须立刻让 _site_failover_run 切到下一个站点（gemini 触顶 → aistudio）。

    ⚠️ 继承 _StudioErrorTurn 是刻意的：这样现有 `except (_StudioErrorTurn,
    _StudioNoGeneration)` 的 failover 分支**零改动**即可捕获本类。
    """


def _iter_reply_with_resend(page, message: str, timeout: float, stream: bool,
                            dual: bool = False, baseline: int = 0, reuse: bool = False,
                            stop_event: threading.Event | None = None):
    """_iter_reply + 出错恢复链（2026-09-25 外脑裁决 Q2 定稿）。

    实测背景：AI Studio 对新会话首次请求返回 "An internal error has occurred"，
    且不会自愈（连续观测 75s 仍为 error）；同会话再次提交即成功（实测 2s 返回）。
    旧实现只把错误判为"未完成"继续轮询，结果空等到硬超时才报错
    （NWW 侧 600s / CLI 代理 300s），表现为"很慢且最终超时"。

    恢复链（A+B 混合，30s 冷却不下调——实测 20s 及以下恢复率明显劣化）：
      第 1 次：30s 冷却 → 同会话重发（首请求惩罚只在会话首轮，同会话重发已被实测验证）；
      第 2 次：45s 冷却 → _new_session() 开全新会话后重发（会话状态被污染时的兜底）。
    上限 _ERROR_RESEND_MAX(2) 次重发（= 总尝试 3 次），耗尽立即抛明确错误（不空转）。
    放在外层而非改 _iter_reply 职责，以便复用现有测试覆盖、保持职责隔离。
    """
    budget = time.monotonic() + max(float(timeout or 0), 30.0)   # ← 全链共享
    attempt = 0
    while True:
        # 断开后不再进入下一轮重发（否则又开始一轮 30s 冷却，锁更晚才释放）
        if stop_event is not None and stop_event.is_set():
            return
        # P0-6：旧实现每次重试各自重算 deadline，最坏可达 3×timeout + 75s
        #（timeout=600 时约 31 分钟）才最终报错。改由整条链共享一个预算封顶。
        remain = budget - time.monotonic()
        if remain <= 0:
            raise RuntimeError(
                f"Chrome 桥接总预算耗尽（{timeout}s，已重试 {attempt} 次）"
            )
        try:
            # 启动确认放进 try：未启动也是"要重发"的一类失败（见 _wait_generation_started
            # 的失败分型）。原先它在 _run_on_studio 里独立调用，抛错会绕过整条重发链。
            conf_started = _wait_generation_started(page, baseline=baseline)
            yield from _iter_reply(page, remain, stream=stream, prompt=message,
                                   dual=dual, baseline=baseline, stop_event=stop_event,
                                   started=conf_started)
            return
        except _StudioErrorTurn as exc:
            if attempt >= _ERROR_RESEND_MAX:
                if isinstance(exc, _StudioNoGeneration):
                    raise RuntimeError(
                        f"AI Studio 已接收提问但始终未开始生成（已冷却重发 {attempt} 次"
                        "仍无响应）。多见于账户限流/额度触顶 —— 请在 Chrome 打开 "
                        "AI Studio 手动发一条消息验证，若同样无响应即需等待额度刷新"
                    ) from exc
                raise RuntimeError(
                    f"AI Studio 连续返回内部错误（已冷却重发 {attempt} 次仍未成功）。"
                    "账户可能处于限流中，可稍后在 Chrome 手动确认 AI Studio 状态后重试"
                ) from exc
            attempt += 1
            if attempt == 1:
                wait_s, plan = _ERROR_RESEND_WAIT_S, "同会话重发"
            else:
                wait_s, plan = _ERROR_RESEND_WAIT2_S, "重开新会话重发"
            reason = (
                "提问已入列但未开始生成（疑似限流/额度）"
                if isinstance(exc, _StudioNoGeneration)
                else "返回错误占位/回显污染"
            )
            # ★站点名用当前生效站点，不要硬编码 "AI Studio"：
            #   双站点之后硬编码会直接误导排障（把 Gemini 的问题记成 AI Studio 的）。
            print(
                f"[CHROME_BRIDGE] {_ACTIVE_SITE_KEY} {reason}，"
                f"{wait_s:.0f}s 后第 {attempt} 次{plan}",
                flush=True,
            )
            page.wait_for_timeout(int(wait_s * 1000))
            # 会话复用时，会话状态本身就是嫌疑源（脏上下文也可能诱发错误占位），
            # 故一旦报错立刻隔离重开；隔离模式维持原策略（第 2 次重发才重开）。
            if attempt > 1 or reuse:
                _new_session(page)
            _send_prompt(page, message)
            # 下一轮循环回到 try 顶部，由 _wait_generation_started 再确认一次启动



def _select_site(key: str) -> _Site:
    """切换当前生效站点：把该站点的首页 URL 与选择器绑定到模块级常量。

    为什么用"重绑定常量"而不是给每个函数加 site 参数：现有 20+ 个函数
    （_send_prompt / _run_ready / _iter_reply / _collect_turn_candidates …）
    都直接读这组常量，改签名会牵一发动全身；重绑定只动一处，风险最低。

    必须在 _LOCK 内调用（常量是全局的，放锁外会被并发请求交叉污染）。
    """
    global _STUDIO_HOME, _PROMPT_SELECTORS, _RUN_SELECTORS
    global _NEW_CHAT_SELECTORS, _OUTPUT_SELECTORS, _THOUGHT_SELECTORS
    global _ACTIVE_SITE_KEY, _STOP_MARKERS
    global _TURN_SELECTORS, _RESP_TURN_SELECTOR, _BODY_SELECTORS
    global _INPUT_KIND, _STOP_SELECTORS, _ROLE_BY_TAG, _ERROR_MARKERS
    global _QUOTA_MARKERS

    site = _SITES.get(key)
    if site is None:
        raise RuntimeError(
            f"未登记的桥接站点: {key}（可用: {', '.join(sorted(_SITES))}）"
        )
    if not site.ready:
        raise RuntimeError(
            f"桥接站点 {key} 当前不可用：ready=False"
            "（未实机探活，或已被裁决停用 —— 例：aistudio 于 2026-09-26 停用）。"
            "确认要用请在 _SITES 中把该站点的 ready 置为 True"
        )
    _STUDIO_HOME = site.home
    _PROMPT_SELECTORS = site.prompt
    _RUN_SELECTORS = site.run
    _NEW_CHAT_SELECTORS = site.new_chat
    _OUTPUT_SELECTORS = site.output
    _THOUGHT_SELECTORS = site.thought
    _STOP_MARKERS = site.stop_markers
    _TURN_SELECTORS = site.turn
    _RESP_TURN_SELECTOR = site.resp
    _BODY_SELECTORS = site.body
    _INPUT_KIND = site.input_kind
    _STOP_SELECTORS = site.stop
    _ROLE_BY_TAG = site.role_by_tag
    # 站点专属错误标记优先；未配则用通用英文基线（中文标记不能进通用表 ——
    # 中文小说正文里出现"出了点问题"完全正常，无差别匹配会误杀正文）
    _ERROR_MARKERS = site.error_markers or _BASE_ERROR_MARKERS
    _QUOTA_MARKERS = site.quota_markers or ()
    _ACTIVE_SITE_KEY = site.key
    return site


def _site_failover_run(fn, *args, **kwargs):
    """按 _SITE_ORDER 依次尝试站点：当前站点"不可用"就换下一个。

    "不可用"判据（都是服务端侧失败，换站点才有意义）：
      - _StudioErrorTurn  ：internal error / 回显污染（AI Studio 触顶的典型表现）
      - _StudioNoGeneration：提问已入列但未生成、或生成完但应答为空
    其余错误（CDP 连不上、playwright 缺失、页面结构变了）属环境问题或代码问题，
    换站点也没用，直接上抛，避免把真正的原因藏起来。

    ⚠️ 已产出内容后**不再切换**：流式场景下半路换站点重跑会把同一段内容
    投递两遍，比失败更糟。

    锁的语义与原实现一致：整个生成器生命周期内持有 _LOCK。
    """
    last: BaseException | None = None
    order = _SITE_ORDER or ("aistudio",)
    for idx, key in enumerate(order):
        emitted = {"n": 0}
        try:
            with _LOCK:
                _select_site(key)
                for item in fn(*args, **kwargs):
                    emitted["n"] += 1
                    yield item
            return
        except (_StudioErrorTurn, _StudioNoGeneration) as exc:
            last = exc
            if emitted["n"] > 0:
                # 已经有内容下发给调用方了，换站点重跑 = 重复投递，直接收口
                raise
            nxt = order[idx + 1] if idx + 1 < len(order) else None
            if nxt:
                print(
                    f"[CHROME_BRIDGE] 站点 {key} 不可用（{exc}）；切换到 {nxt}",
                    flush=True,
                )
            else:
                print(
                    f"[CHROME_BRIDGE] 站点 {key} 不可用（{exc}）；已无后备站点",
                    flush=True,
                )
            continue
    raise RuntimeError(
        f"全部桥接站点均不可用（已试: {', '.join(order)}）。最后一次失败: {last}"
    ) from last


def _run_on_studio(message: str, timeout: float, stream: bool, dual: bool = False,
                   reuse: bool = False, session_id: str = "default", reset: bool = False,
                   stop_event: threading.Event | None = None):
    """一次完整页面会话：连接 → 复用/首开标签 → （可选）开新会话 → 发送 → 轮询回收。

    Args:
        reuse: 是否复用页面上已有会话（跳过 _new_session）。
            为什么要复用：AI Studio 对「全新会话的第一次提交」稳定返回 internal error，
            必须等 30s 冷却后重发才成功 —— 每次调用都 new session 就等于每次都交这笔
            ~30s 惩罚。复用后续聊即可免除。

            复用的代价与前提：页面里留有历史 turn，因此**必须**记录轮次基线 baseline，
            并把 baseline 贯穿到取正文/取思考/错误判定的各处 —— 否则新答复尚未渲染时
            会把上一轮的旧答案当本轮结果返回。这就是 baseline 与 reuse 必须成对出现的原因。

            本模块默认是隔离的（reuse=False）：NWW 写章节是逐章长文本，
            上下文串味代价远高于 30s。是否复用由调用方决定（代理层对 CLI 场景开启）。
        session_id: 会话分类标识。**同一 session_id 固定落在同一扇 Tab**（续聊、上下文连贯，
            并复用已有会话以跳过 30s 惩罚）；**不同 session_id 各自独立 Tab**（物理隔离、
            互不污染）。默认 "default"。例："aider-repoA" / "novel_vol_1" / "default"。
        reset: True = 本轮开始前先把**该**会话上下文重置（New chat），其它 session_id 的
            Tab 不受影响。用于"清空这个分类的历史"。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "未安装 playwright（chrome 桥接依赖）。请执行: pip install playwright"
        ) from exc
    pw = sync_playwright().start()
    try:
        browser = _connect_browser(pw)
        # 按 session_id 取专属 Tab：同类话题稳定落在同一扇，不同类互不污染
        page = _get_bridge_page(browser, session_id=session_id)
        baseline = 0
        if _url_mismatch(page):
            # 站点刚切换过（故障转移），页面还停在上一个站点 —— 必须先把标签拉到
            # 当前站点，否则后面所有选择器都会落空。会话复用分支不会走下面的
            # _new_session，这个洞只能在这里堵。
            _new_session(page)
        elif reset or not reuse:
            # 隔离模式 / 显式重置：开干净会话，页面只剩本轮 turn，基线为 0。
            # （_get_bridge_page 首次创建 Tab 时已 new_session 过；这里是隔离模式的
            #  每轮重置，或调用方显式要求重置上下文。）
            _new_session(page)
        else:
            # 续聊：不要 new session —— 那正是 30s 惩罚的来源。
            #
            # 前置防线：先确认上一轮已彻底停下再取基线。若上一次是客户端 Ctrl+C /
            # 异常中止，页面可能残留"仍在生成"的旧回合 —— 这时数出来的 baseline
            # 会把它圈进本轮范围，而它还会继续往那个 turn 追加文字，导致本轮拿到
            # 上一代的残留文本。等 Run 从 Stop 复位即可排除这种情况。
            try:
                settle_deadline = time.monotonic() + 10.0
                while time.monotonic() < settle_deadline and not _run_ready(page):
                    page.wait_for_timeout(_POLL_INTERVAL_MS)
            except Exception:  # noqa: BLE001 — 判定失效就按"已停下"继续
                pass
            try:
                baseline = _turn_count(page)
            except Exception:  # noqa: BLE001 — 取不到就退化为全量扫描（等价于隔离）
                baseline = 0
        _send_prompt(page, message)
        # P0-2 的「生成是否真的启动」确认已下沉进 _iter_reply_with_resend 的 try 内：
        # 失败分型后「已提交但未生成」属于要重发的一类，必须能被重发链捕获，
        # 放在这里抛会把整条链绕过去（2026-09-25 23:15 修复）。
        # 两种模式统一 yield from：生成器内 return 的值不会传给调用方，
        # 此前非流式用 return text 导致结果永远丢失（2026-09-23 实测修复）
        # 2026-09-25：外层接出错重发闭环（错误 turn 不自愈，必须主动重发）
        yield from _iter_reply_with_resend(page, message, timeout, stream=stream,
                                           dual=dual, baseline=baseline, reuse=reuse,
                                           stop_event=stop_event)
    except RuntimeError:
        raise  # 桥接自身的明确错误（CDP/定位/超时）原样上抛
    except Exception as exc:  # noqa: BLE001 — playwright TimeoutError 等
        # 关键：llm_client 三处（含模型中心降级链 chat_completion_robust）
        # 只 catch RuntimeError；playwright 异常继承自 Exception，不包一层会
        # 穿透降级链变成裸 500（GeneratorExit 是 BaseException，不受影响）
        raise RuntimeError(
            f"Chrome 桥接页面操作失败（{_ACTIVE_SITE_KEY}，{type(exc).__name__}）: {exc}。"
            f"可在 Chrome 中检查 {_STUDIO_HOME} 标签页状态后重试"
        ) from exc
    finally:
        # 关键（2026-09-24）：不关标签页——标签长期保留供下次复用；
        # pw.stop() 只断开 CDP 连接，不影响 Chrome 里的任何标签/窗口
        pw.stop()


def chat(
    system_prompt: str,
    user_prompt: str,
    model: str,
    timeout: float = 300,
    reuse_session: bool = False,
    session_id: str = "default",
    reset_session: bool = False,
) -> str:
    """经本机 Chrome（AI Studio VIP）做一次性补全，返回全部文本。

    Args:
        system_prompt: 系统提示（与 user 合并为单轮网页提问）。
        user_prompt: 用户提示。
        model: 池模型 id（沿用页面当前选中模型，此参数仅作标识透传）。
        timeout: 整体硬超时秒数。
        reuse_session: True = 复用页面已有会话（跳过 new session，免除 ~30s 首请求惩罚，
            但会继承页面上的历史上下文）；False = 每次开干净会话（默认，防上下文串味）。

    Returns:
        模型输出全文。

    Raises:
        RuntimeError: playwright 缺失、CDP 未监听、定位失败或生成超时。
    """
    message = _build_message(system_prompt, user_prompt)
    for text in _site_failover_run(
        _run_on_studio, message, timeout, stream=False, reuse=reuse_session,
        session_id=session_id, reset=reset_session,
    ):
        return text
    raise RuntimeError("Chrome 桥接未取得生成结果")


def chat_stream(
    system_prompt: str,
    user_prompt: str,
    model: str,
    timeout: float = 300,
    reuse_session: bool = False,
    session_id: str = "default",
    reset_session: bool = False,
) -> Iterator[str]:
    """经本机 Chrome（AI Studio VIP）做流式补全，逐段 yield 文本增量。

    与 chat 同一页面会话流程；生成期间轮询 DOM 增量输出。
    锁在生成器整个生命周期内持有（消费完毕/异常中断即释放）。

    Yields:
        文本增量。

    Raises:
        RuntimeError: playwright 缺失、CDP 未监听、定位失败或生成超时。
    """
    message = _build_message(system_prompt, user_prompt)
    yield from _site_failover_run(
        _run_on_studio, message, timeout, stream=True, reuse=reuse_session,
        session_id=session_id, reset=reset_session,
    )


def chat_stream_kinds(
    system_prompt: str,
    user_prompt: str,
    model: str,
    timeout: float = 300,
    reuse_session: bool = False,
    session_id: str = "default",
    reset_session: bool = False,
    stop_event: threading.Event | None = None,
) -> Iterator[tuple[str, str]]:
    """双通道流式：逐段 yield (kind, piece)，kind ∈ {"thought", "text"}。

    与 chat_stream 同一页面流程、同一把锁，区别只在输出形态：
      - ("thought", 增量) = AI Studio 思考区块 → 上层映射为 reasoning_content
      - ("text",    增量) = 正式正文         → 上层映射为 content

    用途：让 CLI 客户端能实时渲染折叠思考过程，而不是只拿到最后的正文。
    思考区块不存在时，本函数只会产出 "text" 通道，调用方无需特判。
    """
    message = _build_message(system_prompt, user_prompt)
    yield from _site_failover_run(
        _run_on_studio, message, timeout, stream=True, dual=True,
        reuse=reuse_session, session_id=session_id,
        reset=reset_session, stop_event=stop_event,
    )


def chat_detailed(
    system_prompt: str,
    user_prompt: str,
    model: str,
    timeout: float = 300,
    reuse_session: bool = False,
    session_id: str = "default",
    reset_session: bool = False,
    stop_event: threading.Event | None = None,
) -> tuple[str, str]:
    """一次性补全，返回 (思考全文, 正文全文)；未开启/无思考时前者为 ""。

    给非流式调用方用：需要把思考一并交付时（如拼 `<think>…</think>` 前缀），
    不必自己再走一遍流式。
    """
    thought_parts: list[str] = []
    text_parts: list[str] = []
    for kind, piece in chat_stream_kinds(system_prompt, user_prompt, model,
                                         timeout, reuse_session=reuse_session,
                                         session_id=session_id,
                                         reset_session=reset_session,
                                         stop_event=stop_event):
        (thought_parts if kind == "thought" else text_parts).append(piece)
    return "".join(thought_parts), "".join(text_parts)

