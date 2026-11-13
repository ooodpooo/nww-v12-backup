import type { BookDraft } from '../api'

/* ─────────────────────  建书流程实况：状态契约与纯推导（2026-11-07）  ─────────────────────
   目标：中间「建书流程」卡展示的每一条成果都必须是真数据，不臆造进度。
   数据来源只有三处，全部是既有生产链路：
     ① 建书 chatbox 的会话消息（ChatBox 通过事件总线广播）—— 用户说了什么、助手问到第几问；
     ② 后端 DRAFT_JSON 解析出的 BookDraft 八字段（agent.py:146 / api.ts:370）；
     ③ App 建书编排的真实回执 —— 建书摘要写入结果、bootstrapBook 的 written/skipped
        （dataplane.py:330-337 的 ordered 六文件）。
   V14 v2 参考：创作工作室「说想法 → 选择方向 → 阅读首章」；本流程把「选择方向」
   做成可选第 2 步，点选后真的把方向文本送进 chatbox，参与后续草案收敛。
─────────────────────────────────────────────────────────────────────────────────────── */

export const CREATE_FLOW_EVENT = 'nww:create-flow'
export const CREATE_FLOW_SEND_EVENT = 'nww:create-flow-send'

export interface CreateFlowSnapshot {
  nonce: number
  busy: boolean
  thinking: string | null
  messages: Array<{ role: 'user' | 'assistant'; content: string }>
  /** 后端 DRAFT_JSON 草案（8 字段全非空才下发，见 agent.py:1129 附近） */
  draft: BookDraft | null
}

export interface CreateFlowSendDetail {
  text: string
  nonce: number
}

/** 与后端 DRAFT_JSON 8 键、DraftCard 8 字段严格同源，禁止另造口径 */
export const DRAFT_FIELDS: Array<{ key: keyof BookDraft; label: string }> = [
  { key: 'title', label: '书名' },
  { key: 'genre', label: '类型' },
  { key: 'synopsis', label: '一句话简介' },
  { key: 'core_theme', label: '核心命题' },
  { key: 'protagonist', label: '主角与金手指' },
  { key: 'conflict', label: '冲突结构' },
  { key: 'differentiation', label: '差异化' },
  { key: 'scale', label: '篇幅规划' },
]

/** 后端 7 个真相问题（agent.py:108-114），用于显示「当前问到第几问」 */
export const TRUTH_QUESTIONS = [
  '核心命题：这本书最想表达的一句话道理',
  '题材与类型：背景 / 流派 / 目标平台',
  '主角：身份、处境、金手指及其代价',
  '冲突结构：对手、阻力、爽点来源',
  '差异化：与同类书不同在哪',
  '篇幅与节奏：字数、卷数、更新节奏',
  '书名与一句话简介：吸引点开的钩子',
]

export interface DirectionOption {
  id: string
  name: string
  desc: string
  tags: string[]
  /** 点选后注入 chatbox 的真实文本：方向成为后续摘要的约束，不是装饰 */
  prompt: string
}

/** 参考 V14 v2 创作工作室「选择方向」：给出可点的创作方向 + 让 AI 现场生成的入口 */
export const DIRECTIONS: DirectionOption[] = [
  {
    id: 'fanqie-system',
    name: '番茄男频 · 系统面板流',
    desc: '数据化升级 + 规则博弈，爽点前置、每章留钩子，节奏快。',
    tags: ['番茄', '系统/面板', '快节奏'],
    prompt:
      '我选择的创作方向是「番茄男频 · 系统面板流」：数据化升级与规则博弈，爽点前置、每章留钩子、节奏快。请把这个方向作为题材与平台基调，按 7 个真相问题继续追问；后续【建书摘要】与 DRAFT_JSON 里的类型、冲突结构、篇幅规划都要与它一致。',
  },
  {
    id: 'fanqie-slice',
    name: '番茄女频 · 经营反内卷',
    desc: '开店经营 + 群像轻喜，靠日常反差与人情味积累追读。',
    tags: ['番茄', '经营/群像', '轻喜'],
    prompt:
      '我选择的创作方向是「番茄女频 · 经营反内卷」：开店经营 + 群像轻喜，靠日常反差与人情味积累追读。请把这个方向作为题材与平台基调，按 7 个真相问题继续追问；后续【建书摘要】与 DRAFT_JSON 里的类型、冲突结构、篇幅规划都要与它一致。',
  },
  {
    id: 'qidian-slow',
    name: '起点风 · 长线群像',
    desc: '世界观厚、伏笔长线，慢热但结构完整，适合超长篇。',
    tags: ['起点', '群像', '长线伏笔'],
    prompt:
      '我选择的创作方向是「起点风 · 长线群像」：世界观厚、伏笔长线、慢热但结构完整，按超长篇规划。请把这个方向作为题材与平台基调，按 7 个真相问题继续追问；后续【建书摘要】与 DRAFT_JSON 里的类型、冲突结构、篇幅规划都要与它一致。',
  },
]

export const AI_DIRECTIONS_PROMPT =
  '请先别急着出草案：基于我刚才说的想法，给出 3 个可选的创作方向，每个都写明「题材定位 / 核心爽点 / 主线走向 / 目标平台与篇幅」。我会选一个，你再按 7 个真相问题继续往下问。'

/** dataplane.py:330-337 ordered：建书 bootstrap 逐文件生成的 6 份资产（主角卡路径动态） */
export const BOOTSTRAP_FILES: Array<{ path: string; label: string; dynamic?: boolean }> = [
  { path: '设定/世界观/世界观.md', label: '世界观' },
  { path: '设定/势力/主要势力.md', label: '主要势力' },
  { path: '设定/商品/商品档案.md', label: '商品档案' },
  { path: '设定/文风.md', label: '文风' },
  { path: '大纲/卷纲_第1卷.md', label: '第 1 卷卷纲' },
  { path: '设定/角色/<主角名>.md', label: '主角人物卡', dynamic: true },
]

/** App 建书编排侧的真实回执（不进事件总线，直接作为 props，避免两套状态） */
export interface CreateFlowExternal {
  /** localStorage 里本会话选中的方向 id */
  directionId: string | null
  /** handleCreateBook 全流程完成（createDone） */
  done: boolean
  /** 已创建的书名（books 列表 / pending 载荷） */
  createdTitle: string | null
  /** 建书摘要.md 是否已写入 */
  mdWritten: boolean
  /** bootstrapBook 返回的真实落盘清单 */
  written: string[]
  skipped: string[]
  /** 创建/生成进行中（含重试） */
  busy: boolean
  error: string | null
}

export interface FlowItem {
  label: string
  value: string
  ok: boolean
}

export interface FlowStep {
  id: string
  title: string
  goal: string
  status: 'done' | 'active' | 'pending'
  /** 0–100，只按真实回执推进，不做动画式假进度 */
  progress: number
  /** 成果：字段名 + 实际内容（有内容才标 ok） */
  items: FlowItem[]
  /** 缺口：还差几项、差哪些 */
  missing: string[]
  /** 当前动作（AI 实况行） */
  current: string
  note?: string
}

export const DIRECTION_KEY = (nonce: number) => `nww_v12_create_direction_${nonce}`
export const CHAT_KEY = (nonce: number) => `nww_v12_chat_default_${nonce}`

export function emitCreateFlow(snapshot: CreateFlowSnapshot): void {
  window.dispatchEvent(new CustomEvent(CREATE_FLOW_EVENT, { detail: snapshot }))
}

/** 中间卡 → chatbox：把方向/追问作为一条真实用户消息送进对话 */
export function emitCreateFlowSend(text: string, nonce: number): void {
  window.dispatchEvent(new CustomEvent(CREATE_FLOW_SEND_EVENT, { detail: { text, nonce } satisfies CreateFlowSendDetail }))
}

/** 刷新后从 chatbox 的 localStorage 会话重建初始快照（草案不恢复，与 ChatBox.loadMsgs 终裁 C 一致） */
export function readInitialSnapshot(nonce: number): CreateFlowSnapshot {
  try {
    const raw = localStorage.getItem(CHAT_KEY(nonce))
    const arr = raw ? JSON.parse(raw) : null
    if (Array.isArray(arr)) {
      const messages = arr
        .filter((m) => m && (m.role === 'user' || m.role === 'assistant') && typeof m.content === 'string')
        .map((m) => ({ role: m.role as 'user' | 'assistant', content: m.content as string }))
      return { nonce, busy: false, thinking: null, messages, draft: null }
    }
  } catch { /* ignore */ }
  return { nonce, busy: false, thinking: null, messages: [], draft: null }
}

export function readDirection(nonce: number): string | null {
  try { return localStorage.getItem(DIRECTION_KEY(nonce)) } catch { return null }
}

export function saveDirection(nonce: number, id: string): void {
  try { localStorage.setItem(DIRECTION_KEY(nonce), id) } catch { /* ignore */ }
}

function clip(s: string, n = 90): string {
  const one = s.replace(/\s+/g, ' ').trim()
  return one.length > n ? `${one.slice(0, n)}…` : one
}

/** 助手消息里的「n/7 ——」是后端 7 问引导的真实进度标记（WELCOME 为 1/7） */
export function questionIndex(messages: CreateFlowSnapshot['messages']): number {
  let q = 0
  for (const m of messages) {
    if (m.role !== 'assistant') continue
    const hit = /([1-7])\s*\/\s*7/.exec(m.content)
    if (hit) q = Math.max(q, Number(hit[1]))
  }
  return q
}

function bootstrapHit(path: string, ext: CreateFlowExternal): boolean {
  const all = [...ext.written, ...ext.skipped]
  if (all.includes(path)) return true
  // 主角卡按 bible 主角名动态命名：设定/角色/<姓名>.md
  if (path.startsWith('设定/角色/')) return all.some((p) => p.startsWith('设定/角色/') && p.endsWith('.md'))
  return false
}

/** 单一事实源：六步流程全部由「聊天快照 + App 回执」推导，UI 不自己记进度 */
export function deriveFlow(snap: CreateFlowSnapshot, ext: CreateFlowExternal): FlowStep[] {
  const draft = snap.draft
  const userMsgs = snap.messages.filter((m) => m.role === 'user' && m.content.trim())
  const q = questionIndex(snap.messages)
  const direction = DIRECTIONS.find((d) => d.id === ext.directionId) ?? null
  const draftFields: FlowItem[] = DRAFT_FIELDS.map((f) => ({
    label: f.label,
    value: draft ? clip(String(draft[f.key] ?? ''), 120) : '待补',
    ok: !!draft && String(draft[f.key] ?? '').trim().length > 0,
  }))

  // ① 搜集信息：有草案=8 项齐备；无草案则展示用户真实说过的线索 + 尚未进草案的字段
  const collectProgress = draft
    ? 100
    : Math.min(90, Math.max(q > 0 ? Math.round(((q - 1) / 7) * 100) : 0, userMsgs.length > 0 ? Math.min(85, userMsgs.length * 15) : 0))
  const collectItems: FlowItem[] = draft
    ? draftFields
    : userMsgs.map((m, i) => ({ label: `线索 ${i + 1}`, value: clip(m.content, 110), ok: true }))
  const collectCurrent = snap.busy
    ? (snap.thinking ? clip(snap.thinking, 90) : 'AI 正在整理你刚才的回答…')
    : draft
      ? '8 项主资料齐备，助手已交付草案'
      : userMsgs.length > 0
        ? `已收到 ${userMsgs.length} 条线索，等待继续补充`
        : '等待你在右侧说出想写的故事'

  // ② 选择方向（V14 v2）：可跳过，不阻塞草案。
  // 未选时候选方向由 DirectionPicker 卡片展示，此处不重复列一遍同样内容。
  const directionItems: FlowItem[] = direction
    ? [
      { label: '已选方向', value: direction.name, ok: true },
      { label: '方向基调', value: clip(direction.desc, 110), ok: true },
      { label: '标签', value: direction.tags.join(' / '), ok: true },
    ]
    : []

  // ③ 草案
  const draftReady = !!draft

  // ④ 创建书籍 + 写摘要
  const created = ext.done || ext.mdWritten || ext.written.length > 0 || ext.skipped.length > 0
  const createItems: FlowItem[] = [
    { label: '书名', value: ext.createdTitle ?? (draft?.title ?? '待确认'), ok: !!ext.createdTitle || !!draft?.title },
    { label: '设定/建书摘要.md', value: ext.mdWritten || ext.done ? '已写入（八项全量）' : '待写入', ok: ext.mdWritten || ext.done },
    { label: '书项目', value: created ? '已创建' : '待创建', ok: created },
  ]

  // ⑤ 开书资料：逐文件按 bootstrap 真实回执点亮
  const bootItems: FlowItem[] = BOOTSTRAP_FILES.map((f) => {
    const ok = bootstrapHit(f.path, ext)
    return { label: f.label, value: ok ? (ext.skipped.includes(f.path) ? '已存在 · 跳过生成' : '已生成') : '待生成', ok }
  })
  const bootOk = bootItems.filter((i) => i.ok).length
  const bootBusy = ext.busy && !ext.done

  // ⑥ 完成
  const finishItems: FlowItem[] = [
    { label: '建书结果', value: ext.done && ext.createdTitle ? `《${ext.createdTitle}》已就绪` : '待完成', ok: ext.done },
    { label: '下一步', value: ext.done ? '自动进入新书 · 可切写作台开写' : '确认草案后自动推进', ok: ext.done },
  ]

  const statusOf = (done: boolean, activeWhen: boolean): FlowStep['status'] => done ? 'done' : activeWhen ? 'active' : 'pending'

  return [
    {
      id: 'collect',
      title: '搜集信息',
      goal: '按 7 个真相问题收齐 8 项建书主资料',
      status: statusOf(!!draft, true),
      progress: collectProgress,
      items: collectItems,
      missing: draft ? [] : DRAFT_FIELDS.map((f) => f.label),
      current: collectCurrent,
      note: q > 0 ? `当前追问：第 ${q}/7 问 · ${TRUTH_QUESTIONS[q - 1] ?? ''}` : '助手会一次只问一个，答完再问下一个',
    },
    {
      id: 'direction',
      title: '选择方向',
      goal: '参考 V14 v2：先定题材 / 爽点 / 平台基调，再收敛草案',
      status: statusOf(!!direction, !!draft || userMsgs.length > 0),
      progress: direction ? 100 : 0,
      items: directionItems,
      missing: direction ? [] : ['从 3 个方向里选 1 个，或点「让 AI 给 3 个方向」'],
      current: direction ? '方向已注入右侧对话，参与后续摘要' : '可选步骤：点下方方向卡，选择会作为一条消息发给助手',
      note: '现在就能选；可跳过，不选也能由助手按 7 问直接收敛草案',
    },
    {
      id: 'draft',
      title: '建书摘要 · 草案',
      goal: '8 项齐备后同轮产出【建书摘要】+ DRAFT_JSON',
      status: statusOf(draftReady, !!draft || userMsgs.length > 0),
      progress: draftReady ? 100 : 0,
      items: draft ? draftFields : [{ label: '草案卡', value: '尚未下发', ok: false }],
      missing: draftReady ? [] : ['【建书摘要】文本块', 'DRAFT_JSON 八字段草案卡（可在右侧编辑后确认）'],
      current: draftReady ? '草案卡已在右侧聊天中，确认后即可创建' : '等待 8 项主资料齐备',
    },
    {
      id: 'create',
      title: '创建书籍 · 写入摘要',
      goal: '建书项目 + 八项全量写入 设定/建书摘要.md',
      status: statusOf(created, draftReady),
      progress: created ? 100 : 0,
      items: createItems,
      missing: created ? [] : ['确认右侧草案卡 → 创建书项目并写入建书摘要.md'],
      current: ext.busy && !created ? '正在创建书项目…' : created ? '书项目与建书摘要已落盘' : '等待确认草案',
    },
    {
      id: 'bootstrap',
      title: '生成开书资料',
      goal: 'bootstrap 逐文件产出 6 份开书资产',
      status: statusOf(bootOk === BOOTSTRAP_FILES.length, created),
      progress: Math.round((bootOk / BOOTSTRAP_FILES.length) * 100),
      items: bootItems,
      missing: bootItems.filter((i) => !i.ok).map((i) => i.label),
      current: bootBusy
        ? '生成中…（逐文件调用 LLM，可达数分钟）'
        : bootOk === BOOTSTRAP_FILES.length
          ? '6 份开书资产齐备'
          : created ? '等待生成结果' : '等待书项目创建',
      note: '清单来自 bootstrapBook 的 written / skipped 真实回执；未记录时不点亮',
    },
    {
      id: 'done',
      title: '完成 · 进入新书',
      goal: '全流程完成，自动切到新书',
      status: statusOf(ext.done, created),
      progress: ext.done ? 100 : 0,
      items: finishItems,
      missing: ext.done ? [] : ['以上 5 步全部完成'],
      current: ext.error ? `未完成：${clip(ext.error, 90)}` : ext.done ? '已完成，正在进入新书' : '流程进行中',
    },
  ]
}
