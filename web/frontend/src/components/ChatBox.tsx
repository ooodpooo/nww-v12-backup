import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { FileText, MoreHorizontal } from 'lucide-react'
import { api, type ModelConfig, type PoolModel, type ProposedAction, type ProposedUpdate, type AuditFinding, type ExecNotice, type BookDraft, type SupervisorGoal, type AgentChatReply } from '../api'
import { chapterFromPath, REWRITE_LEVELS, type RewriteLevel } from '../utils/chapter'
import { SupervisorBar, GoalCard } from './SupervisorPanel'
import { CREATE_FLOW_SEND_EVENT, emitCreateFlow, type CreateFlowSendDetail } from './createFlow'

/* ─────────────────────────  Agent 建书对话窗口（chatbox）  ───────────────────────── */

interface ChatMsg {
  role: 'user' | 'assistant'
  content: string
  error?: boolean
  /** 模型思考内容（<think> 剥离），前端用「思考中」折叠块展示 */
  thinking?: string | null
  /** agent 提案的设定/大纲文件增补（前后端 <apply> 协议产物） */
  updates?: ProposedUpdate[] | null
  /** agent 派单的 pipeline 执行指令（前后端 <execute> 协议产物） */
  actions?: ProposedAction[] | null
  /** 结构化执行事件（P0-C：动作归一化 / 指令被拒），与 reply 正文隔离，独立提示位展示 */
  notice?: ExecNotice[] | null
  /** 建书草案（DRAFT_JSON 协议产物，仅未绑书态；刷新后不恢复，见 loadMsgs 剥离） */
  draft?: BookDraft | null
  /** 监督目标提案（GOAL_JSON 协议产物，仅绑书态；确认后由监督器自动推进） */
  goal?: SupervisorGoal | null
  /** 审计报告路径（追踪/脱纲审计/…md）：审计完成消息携带，渲染「窗口打开/另开窗口」入口 */
  audit_report?: string | null
  /** 本条回复实际使用的模型名（后端回传溯源；旧会话消息无此字段则不显示） */
  model?: string | null
}

const WELCOME: ChatMsg = {
  role: 'assistant',
  content:
    '你好，我是建书助手。用几个问题帮你把新书立起来：\n\n1/7 — 这本书最想表达什么？试着用一句话说说核心命题（比如「小人物靠规则漏洞逆袭」）。\n\n也可以直接丢给我一个灵感片段，我来替你追问。',
}

/** 每本书独立会话，持久化到 localStorage。
 *  无书态（建书会话）用 createNonce 隔离：每次点「＋新建书籍」开一个全新会话，
 *  旧草案不跨会话继承（2026-10-23 外脑终裁 C）。 */
function chatKey(bookId: string | null, nonce?: number): string {
  return bookId ? `nww_v12_chat_${bookId}` : `nww_v12_chat_default_${nonce ?? 0}`
}

function loadMsgs(key: string): ChatMsg[] {
  try {
    const raw = localStorage.getItem(key)
    if (raw) {
      const arr = JSON.parse(raw)
      if (Array.isArray(arr) && arr.length > 0) {
        // 刷新/重进不自动恢复未确认草案（终裁 C）：draft 字段只活在当前内存会话
        return (arr as ChatMsg[]).map((m) => ({ ...m, draft: null }))
      }
    }
  } catch { /* ignore */ }
  return [WELCOME]
}

function renderText(text: string): string {
  const div = document.createElement('div')
  div.textContent = text
  return div.innerHTML.replace(/\n/g, '<br/>')
}

/** 执行动作的中文短标签（P0-C notice 提示用；audit 无单章概念） */
const REGISTER_LABELS: Record<string, string> = {
  strict: '修真正典（修仙界叙述，现代词只经主角）',
  balanced: '修主现辅（修真为主，现代词点缀）',
  modern: '现代语感（现代口吻描绘修仙界）',
}

function execActionLabel(action?: string, chapter?: number, register?: string): string {
  if (action === 'set_register') {
    return `锁定语域档位：${REGISTER_LABELS[register ?? ''] ?? register ?? ''}`
  }
  const verb =
    action === 'rewrite' ? '重写' : action === 'write' ? '写' : action === 'deslop' ? '润色' : action === 'revise' ? '修订' : action === 'audit' ? '一致性审计' : action || '执行'
  return chapter != null ? `${verb}第${chapter}章` : verb
}

/** 快捷任务动作的中文名（审计 / 优化 / 重写(Lx) / 写章） */
function quickActionLabel(action: string, level?: RewriteLevel): string {
  return action === 'audit'
    ? '审计'
    : action === 'polish'
      ? '优化'
      : action === 'write'
        ? '写章'
        : `重写(${level ?? 'L3'})`
}

/* ── 快捷操作（审计/优化/重写/写章四按钮）共享的阶段文案与轮询器 ── */

/** pipeline 环节的中文名（异步任务进度展示用；write/rewrite/polish/deslop/revise/audit 六链共用） */
const PHASE_LABELS: Record<string, string> = {
  queued: '排队中',
  prealign: '预对齐',
  backup: '备份旧稿',
  packet: '打包 Writing Packet',
  writer: '主笔写作',
  analyze: '质量分析+合同快照',
  rewrite_pass: '润色改写',
  polish: '去AI味润色',
  deslop: '去AI味清理',
  normalize: '正文归一（提取/清洗/去重）',
  wordcount: '字数收敛',
  contract_guard: '合同守卫校验',
  delta_draft: 'delta 草稿',
  delta_semantic: 'delta 语义补全',
  consistency: '一致性审查',
  review_cycle: '审稿闭环',
  post_write: '追踪提交/闸门校验',
  revise: '定点修订（只增不删）',
  revise_diff: '只增不删核对',
  rescan: 'AI味复扫',
}

/** 失败决策载荷（后端 nwwmvp._with_decision 派生，纯增量字段） */
type DecisionPayload = {
  needs_decision?: boolean
  failure?: { stage: string; detail: string; gate: string; rolled_back: boolean }
  facts?: { body_chars?: number; outline_target?: number; required_floor?: number; body_path?: string; outline_path?: string }
  decision_options?: Array<{ id: string; label: string; hint: string; risk: string; action: string }>
  receipt_path?: string
  stages?: Array<{ name: string; ok: boolean; detail: string }>
  /** 写章结果（C-WRITE-RESULT@1）：pending_author_review = 正文已落盘但未进权威游标 */
  commit_status?: string
  degraded_pass?: boolean
  warnings?: string[]
}

/** 闸门类型 → 中文标签（决策卡展示；llm_unavailable 是基础设施故障而非内容问题） */
const GATE_LABELS: Record<string, string> = {
  llm_unavailable: '模型端点不可用（非内容问题）',
  length_short: '字数欠账',
  time_drift: '时间事实漂移',
  fact_drift: '事实漂移',
  evidence_missing: '证据不足',
  beat_missing: '节拍未落地',
  generic: '通用',
}

/** 把失败回执压成一段可注入会话的结构化文本（「上报对话请示」用） */
function receiptToPrompt(d: DecisionPayload, chapter: number, actionLabel: string): string {
  const f = d.failure ?? { stage: '', detail: '', gate: '', rolled_back: false }
  const fa = d.facts ?? {}
  const opts = (d.decision_options ?? []).map((o) => `- ${o.label}（${o.action}）：${o.hint}`).join('\n')
  return [
    `【执行失败回执 · 请给出下一步决策】`,
    `动作：${actionLabel}`,
    `章节：第${chapter}章`,
    `失败环节：${f.stage}`,
    `闸门类型：${f.gate}`,
    `失败原因：${f.detail}`,
    `是否已回滚：${f.rolled_back ? '是（正文已还原为执行前）' : '否'}`,
    fa.body_chars != null ? `正文当前字数：${fa.body_chars}` : '',
    fa.outline_target != null ? `细纲字数目标：${fa.outline_target}` : '',
    fa.required_floor != null ? `字数下限（70%）：${fa.required_floor}` : '',
    d.receipt_path ? `回执文件：${d.receipt_path}` : '',
    ``,
    `可选决策：`,
    opts,
    ``,
    `请分析失败根因并给出明确的下一步建议（选哪一项、以及执行要点），不要直接改文件。`,
  ].filter((l) => l !== '').join('\n')
}

/** 失败决策卡片：把静默失败变成「可诊断 + 可选下一步」的闭环入口 */
function DecisionCard({
  d, chapter, actionLabel, closed, onApply, onClose,
}: {
  d: DecisionPayload | null | undefined
  chapter: number
  actionLabel: string
  closed?: boolean
  onApply: (opt: { id: string; label: string; hint: string; risk: string; action: string }) => void
  onClose: () => void
}) {
  if (!d?.needs_decision || closed) return null
  const f = d.failure ?? { stage: '', detail: '', gate: '', rolled_back: false }
  const fa = d.facts ?? {}
  const opts = d.decision_options ?? []
  return (
    <div className="mt-1.5 rounded-md border border-red-900/60 bg-[#1a1214] px-2 py-1.5">
      <div className="flex items-center gap-1.5">
        <span className="text-[12px] font-medium text-red-300" title={actionLabel}>
          第{chapter}章 · 执行失败 · 待决策
        </span>
        <span className="flex-1" />
        <button type="button" onClick={onClose} title="收起" className="text-[13px] leading-none text-[#666] hover:text-[#aaa]">×</button>
      </div>
      <div className="mt-0.5 text-[12px] text-[#999] break-words">
        环节 {f.stage} · 闸门 {GATE_LABELS[f.gate] ?? f.gate}
        {f.rolled_back && <span className="ml-1 text-amber-400">· 正文已回滚</span>}
      </div>
      {(fa.body_chars != null || fa.outline_target != null) && (
        <div className="mt-0.5 text-[12px] text-[#999]">
          正文 {fa.body_chars ?? '?'} 字 / 细纲目标 {fa.outline_target ?? '?'} 字
          {fa.required_floor != null && <span> / 下限 {fa.required_floor} 字</span>}
        </div>
      )}
      {f.detail && <div className="mt-0.5 text-[12px] text-[#888] break-words leading-relaxed">{f.detail}</div>}
      {d.receipt_path && <div className="mt-0.5 text-[11px] text-[#666] font-mono break-all">回执：{d.receipt_path}</div>}
      {opts.length > 0 && (
        <div className="mt-1.5 flex flex-wrap gap-1">
          {opts.map((o) => (
            <button
              key={o.id}
              type="button"
              title={o.hint}
              onClick={() => onApply(o)}
              className={`px-2 py-0.5 rounded border text-[11px] transition-colors ${
                o.risk === 'high'
                  ? 'border-amber-700/60 text-amber-300 hover:bg-amber-500/10'
                  : o.action === 'escalate'
                    ? 'border-[#f59e0b]/50 text-[#f59e0b] hover:bg-[#f59e0b]/10'
                    : 'border-[#3a3a3a] text-[#bbb] hover:border-[#666]'
              }`}
            >
              {o.label}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

/** 阶段 → 中文进度文案（audit 链动态 audit_chN / report 模式优先于静态表） */
function quickPhaseLabel(phase: string): string {
  const am = /^audit_ch(\d+)$/.exec(phase)
  if (am) return `审计第${am[1]}章`
  if (phase === 'report') return '生成审计报告'
  return PHASE_LABELS[phase] ?? phase
}

/** 轮询兜底预算：后端未下发 deadline_ts 时的上限（30 分钟） */
const POLL_FALLBACK_BUDGET_MS = 30 * 60 * 1000
/**
 * 无进展看门狗：这么久 status/phase/phase_detail 毫无变化 → 判疑似卡住。
 * ★必须明显大于后端单章预算（NWW_AUDIT_PER_CHAPTER_S 默认 300s）：
 *   一章审计期间 phase 不变是**正常**的，看门狗若小于它就会把"慢慢跑"误判成"卡死"。
 *   取 10 分钟 = 2× 单章预算，真卡死（线程静默死亡）仍能在 10 分钟内发现。
 */
const POLL_STALL_LIMIT_MS = 10 * 60 * 1000

/**
 * 轮询终止判据：返回 null = 继续轮询；返回字符串 = 终止并报该错误。
 *
 * 取代原先写死的「600 次 × 1.5s ≈ 15 分钟」：区间审计实测 2.5~4 分钟/章，
 * 28 章需 70~110 分钟——常量预算套在 O(N) 线性工作量上必然误报超时。
 * 现在预算由后端按「章数 × 每章预算」下发（deadline_ts），随工作量伸缩；
 * 另加无进展看门狗，真卡住时仍能在 5 分钟内发现。
 */
function pollBreach(st: any, startedAt: number, lastProgressAt: number): string | null {
  const now = Date.now()
  const dl = typeof st?.deadline_ts === 'number' ? st.deadline_ts * 1000 : 0
  const budgetMs = dl > startedAt ? dl - startedAt : POLL_FALLBACK_BUDGET_MS
  if (now - startedAt > budgetMs) {
    return `执行超时（${Math.round(budgetMs / 60000)} 分钟）：已完成的章已落盘，再次提交将从未完成章续跑`
  }
  if (now - lastProgressAt > POLL_STALL_LIMIT_MS) {
    return `疑似卡住：${Math.round(
      POLL_STALL_LIMIT_MS / 60000,
    )} 分钟无任何进展（当前环节：${st?.phase ?? '未知'}）`
  }
  return null
}

/** 统一轮询器：轮询 task 直至终态；writer 流式 / 环节切换回调（快捷操作与 agent 派单共用） */
async function pollAgentTask(
  taskId: string,
  handlers?: { onPhase?: (phase: string) => void; onWriter?: (text: string) => void },
): Promise<{ status: 'done' | 'error' | 'cancelled'; ok?: boolean; result?: any; error?: string }> {
  let shownPhase = ''
  const startedAt = Date.now()
  let lastProgressAt = Date.now()
  let lastSignature = ''
  for (;;) {
    const st = await api.getAgentTask(taskId)
    const sig = `${st.status}|${st.phase ?? ''}|${st.phase_detail ?? ''}`
    if (sig !== lastSignature) {
      lastSignature = sig
      lastProgressAt = Date.now()
    }
    if (st.phase === 'writer' && st.stream_text) {
      handlers?.onWriter?.(st.stream_text)
      lastProgressAt = Date.now() // 流式出字也算进展，不触发看门狗
    } else if (st.phase && st.phase !== shownPhase) {
      shownPhase = st.phase
      handlers?.onPhase?.(st.phase)
    }
    if (st.status === 'done') return { status: 'done', ok: st.ok || !!st.result?.ok, result: st.result }
    if (st.status === 'cancelled') return { status: 'cancelled', result: st.result }
    if (st.status === 'error') return { status: 'error', error: st.error ?? '未知错误' }
    const breach = pollBreach(st, startedAt, lastProgressAt)
    if (breach) return { status: 'error', error: breach }
    await new Promise((r) => setTimeout(r, st.phase === 'writer' ? 800 : 1500))
  }
}

/** 首个失败环节 → 失败摘要（轮询终态为 error 时展示） */
function failedStageText(result: any): string {
  const failed = (result?.stages ?? []).find((s: any) => !s.ok)
  return failed ? `${failed.name} — ${failed.detail}` : ''
}

/** audit 完成汇总：P0/P1/P2 计数 + 建议动作分布（分级兼容 level/severity：后端实际产出 severity，2026-09-20） */
function auditSummary(ch: any): { total: number; p0: number; p1: number; p2: number; suggests: string } {
  const fs: AuditFinding[] = ch?.findings ?? []
  const count = (lv: string) => fs.filter((f) => (f.level ?? f.severity) === lv).length
  const dist = new Map<string, number>()
  for (const f of fs) dist.set(f.suggestion, (dist.get(f.suggestion) ?? 0) + 1)
  const suggests = [...dist.entries()].map(([k, v]) => `${k}×${v}`).join(' / ') || '无建议'
  return { total: fs.length, p0: count('P0'), p1: count('P1'), p2: count('P2'), suggests }
}

/** 审计发现 → 修复派单：按报告建议的确定性路由聚合。
 *  rewrite_L1/L2/L3 → 章级重写（同章多条取最高等级，重写覆盖 patch）；
 *  patch → revise（只增不删）；accept → 不派单（人工背书偏移）。
 *  章内聚合而非一条 finding 一个动作：重写是章级操作，同章拆多条会重复消耗并互相覆盖。 */
function fixActionsFromAudit(r: any): ProposedAction[] {
  const byChapter = new Map<number, { level: number; notes: string[]; patchNotes: string[] }>()
  for (const c of r?.chapters ?? []) {
    const chNum = c?.chapter
    if (!Number.isInteger(chNum)) continue
    let e = byChapter.get(chNum)
    if (!e) { e = { level: 0, notes: [], patchNotes: [] }; byChapter.set(chNum, e) }
    for (const f of c.findings ?? []) {
      const s = String(f?.suggestion ?? 'accept')
      if (s === 'accept') continue
      const sev = (f?.level ?? f?.severity ?? 'P2') as string
      const brief = `[${sev}/${f?.type ?? '?'}] ${String(f?.detail ?? '').slice(0, 120)}`
      const m = /^rewrite_(L[123])$/.exec(s)
      if (m) {
        const lv = Number(m[1][1])
        if (lv > e.level) e.level = lv
        e.notes.push(brief)
      } else if (s === 'patch') {
        e.patchNotes.push(brief)
      }
    }
  }
  const out: ProposedAction[] = []
  for (const [chNum, e] of [...byChapter.entries()].sort((x, y) => x[0] - y[0])) {
    if (e.level > 0) {
      const note = (e.notes.length > 1 ? `共 ${e.notes.length} 项发现，按最高等级 L${e.level} 重写：\n` : '') + e.notes.slice(0, 5).join('\n')
      out.push({ action: 'rewrite', chapter: chNum, level: `L${e.level}` as RewriteLevel, note })
    } else if (e.patchNotes.length > 0) {
      const note = `共 ${e.patchNotes.length} 项发现（revise 只增不删）：\n` + e.patchNotes.slice(0, 5).join('\n')
      out.push({ action: 'revise', chapter: chNum, note })
    }
  }
  return out
}

/** 报告 md → 弹窗 HTML：审计汇总的已知形态（h1/h2、段落、**粗体**、`code`、无序嵌套列表、
 *  管线表格）。自带转义；md-utils 的 mdToHtml 不支持表格（网文正文不需要），报告表格占大头，
 *  故此处用独立的轻量渲染器而不改共用件。 */
function reportMdToHtml(md: string): string {
  const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
  const inline = (s: string) => esc(s).replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>').replace(/`([^`]+)`/g, '<code>$1</code>')
  const out: string[] = []
  let listDepth = 0
  let inTable = false
  const closeList = () => { while (listDepth > 0) { out.push('</ul>'); listDepth-- } }
  const closeTable = () => { if (inTable) { out.push('</tbody></table>'); inTable = false } }
  for (const raw of md.split('\n')) {
    const t = raw.trim()
    if (!t) { closeTable(); continue }
    if (t.startsWith('|') && t.endsWith('|')) {
      const cells = t.slice(1, -1).split('|').map((c) => c.trim())
      if (cells.every((c) => /^:?-{2,}:?$/.test(c))) continue // 表格分隔行
      if (!inTable) {
        closeList()
        out.push('<table><thead><tr>' + cells.map((c) => `<th>${inline(c)}</th>`).join('') + '</tr></thead><tbody>')
        inTable = true
      } else {
        out.push('<tr>' + cells.map((c) => `<td>${inline(c)}</td>`).join('') + '</tr>')
      }
      continue
    }
    closeTable()
    if (t.startsWith('### ')) { closeList(); out.push(`<h3>${inline(t.slice(4))}</h3>`); continue }
    if (t.startsWith('## ')) { closeList(); out.push(`<h2>${inline(t.slice(3))}</h2>`); continue }
    if (t.startsWith('# ')) { closeList(); out.push(`<h1>${inline(t.slice(2))}</h1>`); continue }
    const li = /^(\s*)-\s+(.*)$/.exec(t)
    if (li) {
      const depth = (li[1] ?? '').length >= 2 ? 2 : 1
      while (listDepth > depth) { out.push('</ul>'); listDepth-- }
      if (listDepth < depth) { out.push('<ul>'); listDepth++ }
      out.push(`<li>${inline(li[2])}</li>`)
      continue
    }
    closeList()
    out.push(`<p>${inline(t)}</p>`)
  }
  closeList()
  closeTable()
  return out.join('\n')
}

/** 报告入口行：窗口内打开（编辑器只读视图）/ 另开浏览器窗口（渲染后的 md） */
function ReportLinkRow({ path, onOpen, onExternal, compact }: {
  path: string
  onOpen: () => void
  onExternal: () => void
  compact?: boolean
}) {
  const name = path.split('/').pop() || path
  return (
    <div className={`bubble-max flex items-center gap-2 flex-wrap ${compact ? '' : 'pl-0.5'} text-[12px]`}>
      <span className="text-[#777] shrink-0 flex items-center gap-1"><FileText className="h-3.5 w-3.5" strokeWidth={1.8} aria-hidden />审计报告</span>
      <span className="min-w-0 truncate text-[#666] font-mono" title={path}>{name}</span>
      <button
        type="button"
        onClick={onOpen}
        className="shrink-0 px-2 py-0.5 rounded-md border border-[#3a3a3a] text-[#bbb] hover:border-[#f59e0b]/50 hover:text-[#f59e0b] transition-colors"
      >
        在窗口打开
      </button>
      <button
        type="button"
        onClick={onExternal}
        className="shrink-0 px-2 py-0.5 rounded-md border border-[#3a3a3a] text-[#bbb] hover:border-[#f59e0b]/50 hover:text-[#f59e0b] transition-colors"
        title="在独立浏览器窗口中阅读渲染后的报告"
      >
        另开窗口 ↗
      </button>
    </div>
  )
}

/* ── 气泡悬浮操作图标 ── */
function EditIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M12 20h9" />
      <path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z" />
    </svg>
  )
}

function CopyIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <rect x="9" y="9" width="13" height="13" rx="2" />
      <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
    </svg>
  )
}

function CheckIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M20 6 9 17l-5-5" />
    </svg>
  )
}

/** Gemini 式输入栏图标：加号（附件/工具入口） */
function PlusIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M12 5v14M5 12h14" />
    </svg>
  )
}

/** Gemini 式输入栏图标：下拉箭头 */
function ChevronIcon() {
  return (
    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="m6 9 6 6 6-6" />
    </svg>
  )
}

/** Gemini 式输入栏图标：文件 */
function FileIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z" />
      <path d="M14 2v6h6" />
    </svg>
  )
}

/** 输入栏「更多」：统一 lucide 线性图标 */
function DotsIcon() {
  return <MoreHorizontal className="h-[14px] w-[14px]" strokeWidth={1.8} aria-hidden />
}

/** Gemini 式输入栏图标：发送（向上箭头） */
function ArrowUpIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M12 19V5" />
      <path d="m5 12 7-7 7 7" />
    </svg>
  )
}

/** 附件（上传文件注入对话）：单文件截断上限，与后端 MAX_CONTENT_CHARS=8000 对齐 */
const ATTACH_MAX_CHARS = 3000
const ATTACH_MAX_COUNT = 3

/** 气泡右侧 / 左侧的悬浮操作按钮统一样式 */
const BUBBLE_ACTION_CLS =
  'shrink-0 mb-0.5 w-6 h-6 flex items-center justify-center rounded-md text-[#6a6a6a] hover:text-[#f59e0b] hover:bg-[#f59e0b]/10 disabled:cursor-not-allowed transition-colors ' +
  // 按钮用负外边距移出正常流，不再占用行宽，气泡才能吃到 95% 上限；默认不拦截点击，hover 时才可点
  'pointer-events-none group-hover:pointer-events-auto'

/** 建书草案确认卡（DRAFT_JSON 协议产物，仅未绑书态）：8 字段可编辑 + 一键创建。
 *  本地状态机 ready → creating → created / failed（failed 可重试，不吞草案）。 */
function DraftCard({
  draft,
  onCreate,
  busy,
}: {
  draft: BookDraft
  onCreate?: (d: BookDraft) => Promise<{ ok: boolean; error?: string }>
  busy: boolean
}) {
  const [edited, setEdited] = useState<BookDraft>(draft)
  const [status, setStatus] = useState<'ready' | 'creating' | 'created' | 'failed'>('ready')
  const [error, setError] = useState<string | null>(null)

  const FIELDS: Array<{ key: keyof BookDraft; label: string; multiline?: boolean }> = [
    { key: 'title', label: '书名' },
    { key: 'genre', label: '类型' },
    { key: 'synopsis', label: '一句话简介' },
    { key: 'core_theme', label: '核心命题', multiline: true },
    { key: 'protagonist', label: '主角与金手指', multiline: true },
    { key: 'conflict', label: '冲突结构', multiline: true },
    { key: 'differentiation', label: '差异化', multiline: true },
    { key: 'scale', label: '篇幅规划', multiline: true },
  ]

  const setField = (k: keyof BookDraft, v: string) => {
    if (status === 'created') return
    setEdited((prev) => ({ ...prev, [k]: v }))
    if (status === 'failed') { setStatus('ready'); setError(null) }
  }

  const confirm = async () => {
    if (!onCreate || status === 'creating' || status === 'created') return
    const title = edited.title.trim()
    if (!title) { setStatus('failed'); setError('书名不能为空'); return }
    setStatus('creating')
    setError(null)
    const res = await onCreate({ ...edited, title })
    if (res.ok) setStatus('created')
    else { setStatus('failed'); setError(res.error || '创建失败') }
  }

  const inputCls =
    'w-full bg-[#1a1a1a] border border-[#333] rounded px-2 py-1 text-[13px] text-[#ccc] outline-none focus:border-[#f59e0b]/60 transition-colors disabled:opacity-60'

  return (
    <div className="bubble-max rounded-xl border border-[#f59e0b]/30 bg-[#161616] px-3 py-2.5">
      <div className="flex items-center justify-between gap-2 mb-2">
        <span className="text-[13px] text-[#f59e0b] font-medium">建书草案 · 确认后创建</span>
        {status === 'created' && <span className="text-[12px] text-emerald-400">已创建 · 追踪未初始化</span>}
      </div>
      <div className="grid grid-cols-2 gap-1.5 mb-2">
        {FIELDS.map((f) => (
          <label key={f.key} className={`block ${f.multiline ? 'col-span-2' : ''}`}>
            <span className="block text-[11px] text-[#777] mb-0.5">{f.label}</span>
            {f.multiline ? (
              <textarea
                value={edited[f.key]}
                onChange={(e) => setField(f.key, e.target.value)}
                rows={2}
                disabled={status === 'created'}
                className={`${inputCls} resize-none leading-relaxed`}
              />
            ) : (
              <input
                value={edited[f.key]}
                onChange={(e) => setField(f.key, e.target.value)}
                disabled={status === 'created'}
                className={inputCls}
              />
            )}
          </label>
        ))}
      </div>
      {error && <div className="text-[12px] text-red-400 mb-1.5">{error}</div>}
      <div className="flex items-center justify-end gap-2">
        {status !== 'created' && (
          <button
            type="button"
            onClick={confirm}
            disabled={!onCreate || busy || status === 'creating'}
            title={!onCreate ? '当前界面不支持一键创建' : busy ? 'agent 回复中，请稍候' : '按草案创建新书'}
            className="px-3 py-1 rounded-lg text-[13px] bg-[#f59e0b] text-[#1a1a1a] font-medium hover:bg-[#fbbf24] disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {status === 'creating' ? '创建中…' : status === 'failed' ? '重试创建' : '确认创建新书'}
          </button>
        )}
      </div>
    </div>
  )
}

export default function ChatBox({
  bookId,
  activePath,
  onNavigate,
  onChapterUpdated,
  onAgentTaskStarted,
  editorDirty = false,
  createNonce = 0,
  onCreateBook,
  createMode = false,
}: {
  bookId: string | null
  /** 当前在编辑器打开的文件路径（正文第N章/大纲/设定/追踪），用于告诉 agent 用户正看着哪个文件 */
  activePath?: string | null
  /** 章节开写时自动跳转编辑器到对应正文文件（可选） */
  onNavigate?: (bookId: string, path: string) => void
  /** 快捷操作（润色/重写）改动当前章正文后，通知 App 刷新编辑器（可选） */
  onChapterUpdated?: () => void
  /** 任一异步 agent 任务（重写/写/润色/修订/审计）成功创建后回调（驱动 AI 实况面板自动展开） */
  onAgentTaskStarted?: (taskId: string, mode: string) => void
  /** 编辑器脏状态（未保存修改）：快捷「优化/重写」禁用（防后台落盘覆盖丢稿）；审计不受限（只读） */
  editorDirty?: boolean
  /** 建书会话隔离号（App 每次点「＋新建书籍」递增）：无书态会话按 nonce 分键，旧草案不继承 */
  createNonce?: number
  /** 草案卡「确认创建」回调（App 编排：createBook → 写建书摘要.md → 刷新书架） */
  onCreateBook?: (d: BookDraft) => Promise<{ ok: boolean; error?: string }>
  /** 建书三列工作台激活态：仅此时广播会话快照给中间「建书流程」卡（2026-11-07） */
  createMode?: boolean
}) {
  const [msgs, setMsgs] = useState<ChatMsg[]>(() => loadMsgs(chatKey(bookId, createNonce)))
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  /** 思考进程（SSE progress 实时行）与已耗时：等待回复期间的动态指示（2026-10-25） */
  const [thinkProgress, setThinkProgress] = useState<string | null>(null)
  const [elapsed, setElapsed] = useState(0)
  const scrollRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const [copiedIdx, setCopiedIdx] = useState<number | null>(null)
  /** 本轮生成的控制句柄：busy 期间可 abort 终止 */
  const abortRef = useRef<AbortController | null>(null)

  /** 每条「应用」按钮的落盘状态（key = 消息序号-条序号） */
  type ApplyStatus = 'idle' | 'applying' | 'done' | 'error'
  const [applyState, setApplyState] = useState<Record<string, ApplyStatus>>({})

  /** 每条「一键执行」的 pipeline 状态与结果（key = 消息序号-条序号） */
  type ExecStatus = 'idle' | 'running' | 'done' | 'error' | 'cancelled'
  const [execState, setExecState] = useState<Record<string, ExecStatus>>({})
  const [execResult, setExecResult] = useState<Record<string, string>>({})
  /** 失败决策载荷（后端 _with_decision 派生：failure/facts/decision_options/receipt_path） */
  const [execDecision, setExecDecision] = useState<Record<string, DecisionPayload>>({})
  /** 已关闭（用户已决策）的卡片，避免重复打扰 */
  const [decisionClosed, setDecisionClosed] = useState<Record<string, boolean>>({})
  /** 进行中任务的 task_id（终止用） */
  const taskIdRef = useRef<Record<string, string>>({})

  /** 已跳转章节去重 + 正文文件树缓存（chapter→path，避免每章重复拉取） */
  const navSetRef = useRef<Set<number>>(new Set())
  const treeRef = useRef<Record<number, string>>({})

  /** 批量执行：UI 状态（触发 re-render）+ 流程锁（防重复启动）+ 中止控制 */
  const [batchRunning, setBatchRunning] = useState(false)
  const batchRunningRef = useRef(false)
  const batchAbortRef = useRef(false)

  /* ── Gemini 式输入栏：模型选择 + 附件 + 弹层 ── */
  /** 模型池（模型中心配置）；空 = 未配置 */
  const [poolModels, setPoolModels] = useState<PoolModel[]>([])
  /** 当前对话模型 = 服务端 agent_routes['book-coach']（唯一真源）；'' = 跟随全局默认（后端 book-coach 链） */
  const [poolId, setPoolId] = useState<string>('')
  /** 全局默认池模型 id（用于「跟随全局默认」文案解析） */
  const [defaultPoolId, setDefaultPoolId] = useState<string>('')
  /** 远端完整配置快照：写回时全量 PUT，避免丢 providers/pool/其它 agent_routes */
  const cfgRef = useRef<ModelConfig | null>(null)
  /** 已挂附件（上传文件的文本内容，发送时随消息注入） */
  const [attachments, setAttachments] = useState<Array<{ name: string; content: string }>>([])
  /** 当前打开的弹层：attach=附件菜单 / more=更多工具二级 / model=模型选择 */
  const [openMenu, setOpenMenu] = useState<'attach' | 'more' | 'model' | null>(null)
  const barRef = useRef<HTMLDivElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  /* ── 快捷操作（输入窗口上方四按钮：审计/优化/重写/写章）── */
  /** 当前编辑器章节（从 activePath 解析；非正文/未定位=null → 改用「写第N章」首写入口） */
  const activeChapter = chapterFromPath(activePath)
  /** 单任务快捷执行槽（四按钮共用一条执行进度） */
  type QuickStatus = 'idle' | 'running' | 'done' | 'error' | 'cancelled'
  interface QuickTask {
    action: 'audit' | 'polish' | 'rewrite' | 'write'
    chapter: number
    level: RewriteLevel
    status: QuickStatus
    phase: string
    resultText: string
    taskId: string
    note?: string
    result?: unknown
  }
  const [quick, setQuick] = useState<QuickTask | null>(null)
  const quickTaskIdRef = useRef<string | null>(null)
  /** 快捷任务的决策卡片是否已收起（提交新任务时重置） */
  const [quickDecisionClosed, setQuickDecisionClosed] = useState(false)
  /** 重写等级选择（点击「重写第N章」展开；确认后按 L1/L2/L3 提交） */
  const [rewriteDlg, setRewriteDlg] = useState<{ chapter: number; level: RewriteLevel; note: string } | null>(null)
  // 润色意见输入框（点击「优化第N章」展开；2026-11-02 润色意见全链打通——此前 polish 按钮裸跑，
  // 用户填写的润色意见在前端/端点/执行链三层全被丢弃，润色跑的是自动诊断与意见无关）
  const [polishDlg, setPolishDlg] = useState<{ chapter: number; note: string } | null>(null)

  /* ── 正文存在性（首写导向 + 重写守卫）──
   *  /chapters 只列「实际已落盘的正文文件」，与 MvpConsole 的 existing 同口径。
   *  拉取只在 切书 / 页面动作发生时做一次性查询，**不做轮询、不自动重试**：
   *  用户可能先在编辑器删掉正文再回来点旧卡片，动作前必须再复核一次。 */
  const [existingChapters, setExistingChapters] = useState<Set<number> | null>(null)
  /** 书的权威游标（最近已提交章号）；用于区分「已提交章」与「未提交章」，
   *  对验收提交按钮做提示/引导，避免作者误以为已提交章的手动修改会被重新验收落账（2026-11-08）。 */
  const [lastCommitted, setLastCommitted] = useState<number | null>(null)
  /** 首写导向：未打开正文时「下一待写章」（细纲存在但正文缺失的最小章号；否则最大已有章+1） */
  const [nextWriteChapter, setNextWriteChapter] = useState<number | null>(null)
  /** 守卫提示：正文不存在 / 无法确认时显示，提示期间**不发起任何请求** */
  const [chapterGuardMsg, setChapterGuardMsg] = useState<string | null>(null)
  /** 「验收提交」常驻入口：同步端点，不占四按钮共用的 agent 任务槽。
   *  后端验收链含审稿闭环多轮 LLM + 外脑评审（单次可等 600s），故前端必须有计时/超时/放弃等待，
   *  否则一次网络挂起就会让按钮永远停在「验收中…」。 */
  const [settleBusy, setSettleBusy] = useState(false)
  const [settleOut, setSettleOut] = useState<{ ok: boolean; chapter: number; text: string } | null>(null)
  const [settleStartedAt, setSettleStartedAt] = useState<number | null>(null)
  const [settleElapsed, setSettleElapsed] = useState(0)
  const settleCancelRef = useRef<(() => void) | null>(null)

  const refreshExistingChapters = useCallback(async (): Promise<Set<number> | null> => {
    if (!bookId) {
      setExistingChapters(null)
      return null
    }
    try {
      const list = await api.chapters(bookId)
      const set = new Set(list.chapters.map((c) => c.chapter))
      setExistingChapters(set)
      setLastCommitted(list.last_committed_chapter ?? null)
      return set
    } catch {
      // 清单读取失败 → 返回 null（调用方 fail-closed，不凭旧缓存猜）
      setExistingChapters(null)
      return null
    }
  }, [bookId])

  /** 动作前即时复核：true=正文存在 / false=不存在 / null=无法确认（清单读取失败） */
  const verifyChapterExists = useCallback(async (chapter: number): Promise<boolean | null> => {
    const set = await refreshExistingChapters()
    return set ? set.has(chapter) : null
  }, [refreshExistingChapters])

  /** 推导「写下一章」目标章：优先「有细纲、无正文」的最小章号；无细纲时退化为「最大已有章+1」。
   *  返回值供派单使用；正文清单读取失败 → null（fail-closed，不凭旧缓存猜章号）。 */
  const resolveNextWriteChapter = useCallback(async (): Promise<number | null> => {
    if (!bookId) { setNextWriteChapter(null); return null }
    const existing = await refreshExistingChapters()
    if (!existing) { setNextWriteChapter(null); return null }
    let pending: number[] = []
    try {
      const tree = await api.tree(bookId)
      const sec = tree.sections.find((s) => s.section === '大纲')
      const outlined = new Set<number>()
      for (const f of sec?.files ?? []) {
        const m = /^细纲_第0*(\d+)章/.exec(f.name)
        if (m) outlined.add(parseInt(m[1], 10))
      }
      pending = [...outlined].filter((n) => !existing.has(n)).sort((a, b) => a - b)
    } catch { /* 大纲不可读：仅按已有正文推断，不阻断 */ }
    const maxExisting = existing.size > 0 ? Math.max(...existing) : 0
    const next = pending.length > 0 ? pending[0] : maxExisting + 1
    setNextWriteChapter(next)
    return next
  }, [bookId, refreshExistingChapters])

  /** 「写下一章」在打开正文时也要可用：切书/切章后重算目标章（内部同时刷新正文存在性，
   *  顺带驱动「审计/优化/重写」的正文守卫）。依赖用 primitive 章号：activeChapter 每帧都是新对象。 */
  const activeChapterNum = activeChapter?.num ?? null
  useEffect(() => {
    if (!bookId) return
    void resolveNextWriteChapter()
  }, [bookId, activeChapterNum, resolveNextWriteChapter])

  /** 当前编辑器章节的正文是否存在（未加载时按「未知」→ 按钮保持可用，点击时再复核） */
  const activeChapterExists = activeChapterNum != null && existingChapters?.has(activeChapterNum) === true

  /** 当前章是否已提交（在权威游标内）。已提交章的「验收提交」是幂等 no-op，
   *  手动修改需走重写才能落账——按钮/结果须明确提示，避免误导作者。 */
  const activeChapterCommitted =
    activeChapterNum != null && lastCommitted != null && activeChapterNum <= lastCommitted

  /** 从服务端配置同步 ChatBox 状态（pool / 全局默认 / 当前对话模型） */
  const applyCfg = useCallback((cfg: ModelConfig) => {
    cfgRef.current = cfg
    setPoolModels(cfg.pool ?? [])
    setDefaultPoolId(cfg.default ?? '')
    setPoolId(cfg.agent_routes?.['book-coach'] || '')
  }, [])

  // 拉取模型中心配置（失败静默：未登录/未配置时选择器退化为仅「跟随全局默认」）
  useEffect(() => {
    let alive = true
    api.getModelConfig()
      .then((cfg) => { if (alive) applyCfg(cfg) })
      .catch(() => { /* ignore */ })
    return () => { alive = false }
  }, [applyCfg])

  // 与 ModelCenter 双向同步：任一侧保存配置后广播 → 重取服务端真源
  useEffect(() => {
    const onChanged = () => {
      api.getModelConfig().then(applyCfg).catch(() => { /* ignore */ })
    }
    window.addEventListener('nww-model-config-changed', onChanged)
    return () => window.removeEventListener('nww-model-config-changed', onChanged)
  }, [applyCfg])

  // 模型池刷新后，已存选择失效（条目被删）→ 回退「跟随全局默认」
  useEffect(() => {
    if (poolId && poolModels.length > 0 && !poolModels.some((m) => m.id === poolId)) setPoolId('')
  }, [poolModels, poolId])

  // 点击输入栏外部时收起弹层
  useEffect(() => {
    if (!openMenu) return
    const onDown = (e: MouseEvent) => {
      if (barRef.current && !barRef.current.contains(e.target as Node)) setOpenMenu(null)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [openMenu])

  const selectedPool = poolModels.find((m) => m.id === poolId) ?? null
  const defaultPool = poolModels.find((m) => m.id === defaultPoolId) ?? null
  const defaultLabel = defaultPool ? (defaultPool.label || defaultPool.model) : (defaultPoolId || '环境变量路由')
  const modelLabel = selectedPool ? (selectedPool.label || selectedPool.model) : `跟随全局默认（${defaultLabel}）`

  /** 选择对话模型：写服务端 agent_routes['book-coach']（唯一真源），成功后广播同步 ModelCenter */
  const pickModel = useCallback(async (id: string) => {
    setOpenMenu(null)
    setPoolId(id)
    const cur = cfgRef.current
    if (!cur) return
    try {
      const routes = { ...(cur.agent_routes || {}) }
      if (id) routes['book-coach'] = id
      else delete routes['book-coach']
      const next = await api.saveModelConfig({ ...cur, agent_routes: routes })
      applyCfg(next)
      window.dispatchEvent(new Event('nww-model-config-changed'))
    } catch {
      // 写失败：回退到服务端真值，避免本地状态与服务端分叉
      api.getModelConfig().then(applyCfg).catch(() => { /* ignore */ })
    }
  }, [applyCfg])

  /** 上传文件：读文本内容挂为附件（限 .md/.txt/.json，单文件截断 ATTACH_MAX_CHARS） */
  const pickFiles = useCallback(async (files: FileList | null) => {
    if (!files) return
    const next = [...attachments]
    for (const f of Array.from(files)) {
      if (next.length >= ATTACH_MAX_COUNT) break
      try {
        const text = (await f.text()).slice(0, ATTACH_MAX_CHARS)
        next.push({ name: f.name, content: text })
      } catch { /* 读取失败跳过 */ }
    }
    setAttachments(next)
  }, [attachments])

  /** 更多工具：清空当前书的会话（回退到欢迎语） */
  const clearChat = useCallback(() => {
    setMsgs([WELCOME])
    setOpenMenu(null)
  }, [])

  /** 终止执行中的任务（协作式：后端在流式回调/环节边界退出） */
  const cancelExec = useCallback(async (i: number, ai: number) => {
    const key = `${i}-${ai}`
    const tid = taskIdRef.current[key]
    if (tid) {
      try { await api.cancelAgentTask(tid) } catch { /* ignore */ }
    }
  }, [])

  /** 已发过「审计结果+修复派单」消息的任务（单飞复用：派单卡与快捷卡会轮询同一 task，
   *  双路都到 done 时只追加一条，不重复刷屏） */
  const auditEmittedRef = useRef<Set<string>>(new Set())

  /** 报告在窗口内打开：复用编辑器（追踪分区只读视图），与写作期章节跳转同通道 */
  const openReport = useCallback((path: string) => {
    if (bookId) onNavigate?.(bookId, path)
  }, [bookId, onNavigate])

  /** 报告另开浏览器窗口：拉取内容 → 独立窗口渲染（用户点名：别让人去本地翻文件）。
   *  弹窗被拦截/拉取失败 → 回退窗口内打开，保证入口永远可达。 */
  const openReportExternal = useCallback(async (path: string) => {
    if (!bookId) return
    try {
      const f = await api.file(bookId, path)
      const w = window.open('', '_blank')
      if (!w) { onNavigate?.(bookId, path); return }
      const title = path.split('/').pop() || path
      w.document.write(`<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${title}</title>
<style>
  body { background:#141414; color:#c8c8c8; font:14px/1.75 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
         max-width:920px; margin:0 auto; padding:32px 24px 64px; }
  h1 { color:#fbbf24; font-size:20px; border-bottom:1px solid #333; padding-bottom:10px; }
  h2 { color:#f59e0b; font-size:16px; margin-top:28px; }
  h3 { color:#ddd; font-size:14px; }
  p { margin:6px 0; }
  code { background:#262626; border-radius:4px; padding:1px 5px; font-size:12.5px; color:#dcdcaa; }
  table { border-collapse:collapse; margin:14px 0; width:100%; font-size:13px; }
  th, td { border:1px solid #3a3a3a; padding:5px 10px; text-align:left; }
  th { background:#222; color:#eee; }
  ul { margin:6px 0; padding-left:22px; }
  li { margin:3px 0; }
  ul ul { margin:2px 0; }
  strong { color:#eee; }
</style></head><body>${reportMdToHtml(f.content ?? '（报告内容为空）')}</body></html>`)
      w.document.close()
    } catch {
      onNavigate?.(bookId, path)
    }
  }, [bookId, onNavigate])

  /** 审计完成 → 对话流自动追加「结果 + 修复派单」消息（主动推进的落点）：
   *  派单动作由报告建议的确定性路由生成（fixActionsFromAudit），复用既有批量执行 UI，
   *  用户从「去本地翻报告自己想下一步」变成「窗口内看到结构化发现 + 一键全部执行」。 */
  const emitAuditFollowup = useCallback((taskId: string, r: any, start: number, end: number) => {
    if (auditEmittedRef.current.has(taskId)) return
    auditEmittedRef.current.add(taskId)
    const chapters = r?.chapters ?? []
    const cnt = (lv: string) => chapters.reduce(
      (n: number, c: any) => n + (c.findings ?? []).filter((f: any) => (f.level ?? f.severity) === lv).length, 0)
    const total = r?.total_findings ?? chapters.reduce((n: number, c: any) => n + (c.findings ?? []).length, 0)
    const actions = fixActionsFromAudit(r)
    const head = `审计完成（只读诊断，未改正文）：第${start}-${end}章共发现 ${total} 项（P0×${cnt('P0')} P1×${cnt('P1')} P2×${cnt('P2')}）。`
    const body = actions.length > 0
      ? `${head}\n已按报告建议生成修复派单（下方动作卡，可逐条「一键执行」或「全部执行」；「接受偏移」类发现不派单）。完整报告可点击下方入口在窗口内或另开浏览器窗口阅读。`
      : `${head}\n均为「接受偏移」类或无修复项，未生成修复派单。完整报告可点击下方入口查看。`
    setMsgs((prev) => [...prev, {
      role: 'assistant',
      content: body,
      actions: actions.length > 0 ? actions : null,
      audit_report: r?.report_md ?? null,
    }])
  }, [])

  /** pipeline 环节的中文名（异步任务进度展示用；静态表在模块级 PHASE_LABELS，六链共用） */
  const PHASE_LABEL = PHASE_LABELS

  /** 一键执行：提交异步任务 → 轮询进度（实时显示当前环节） → 返回最终 ExecStatus */
  const execAction = useCallback(
    async (i: number, ai: number, a: ProposedAction): Promise<ExecStatus> => {
      if (!bookId) return 'error' as ExecStatus
      const key = `${i}-${ai}`
      // 执行前复核（T11）：动作卡可能在正文被删除后仍留在会话里。rewrite 语义是「改已有正文」，
      // 正文不存在时不发起请求、也不静默改写成 write —— 只提示用户改点「写下一章」。
      if (a.action === 'rewrite') {
        const exists = await verifyChapterExists(a.chapter)
        if (exists !== true) {
          const msg = exists === false
            ? `第${a.chapter}章尚无已提交正文，请先点「写下一章」（写章会正常生成并落盘）。本次未发起重写请求。`
            : `无法确认第${a.chapter}章正文是否存在（正文清单读取失败），已取消重写，请稍后重试。`
          setChapterGuardMsg(msg)
          setExecState((prev) => ({ ...prev, [key]: 'error' }))
          setExecResult((prev) => ({ ...prev, [key]: msg }))
          return 'error' as ExecStatus
        }
      }
      setExecState((prev) => ({ ...prev, [key]: 'running' }))
      setExecResult((prev) => ({ ...prev, [key]: '提交任务…' }))
      try {
        // 语域档位设置：同步小写入，无长任务轮询，直接以回执文案收尾
        if (a.action === 'set_register') {
          const r = await api.agentsSetRegister(bookId, a.register ?? 'strict')
          setExecResult((prev) => ({ ...prev, [key]: r.message || '语域档位已更新' }))
          setExecState((prev) => ({ ...prev, [key]: 'done' }))
          return 'done' as ExecStatus
        }
        // 贴文直接润色（deslop_text）：同步调用引擎改写，无长任务轮询；
        // 改后全文作为新气泡追加（不改任何文件）——调度台只派单，正文改写发生在引擎内。
        if (a.action === 'deslop_text') {
          const r = await api.agentsDeslopText(bookId, a.text ?? '', a.note ?? '')
          if (r?.result?.ok) {
            const res = r.result
            setExecState((prev) => ({ ...prev, [key]: 'done' }))
            setExecResult((prev) => ({
              ...prev,
              [key]: `贴文润色完成：${res.chars_before ?? 0}字 → ${res.chars_after ?? 0}字（引擎改写，未改动你的文件）`,
            }))
            setMsgs((prev) => [
              ...prev,
              {
                role: 'assistant',
                content:
                  '（以下为引擎去AI味润色后的全文，由 pipeline 改写生成，未改动你的任何文件；可直接复制使用。' +
                  '若希望落盘为正式章节，请用「重写第N章 / 润色第N章」动作。）\n\n' +
                  (res.polished ?? ''),
              },
            ])
            return 'done' as ExecStatus
          }
          setExecState((prev) => ({ ...prev, [key]: 'error' }))
          setExecResult((prev) => ({ ...prev, [key]: `润色失败：${r?.result?.error ?? '未知错误'}` }))
          return 'error' as ExecStatus
        }
        const task = a.action === 'rewrite'
          ? await api.agentsRewriteAsync(bookId, a.chapter, a.note, a.level)
          : a.action === 'deslop'
            ? await api.agentsDeslopAsync(bookId, a.chapter)
            : a.action === 'revise'
              ? await api.agentsReviseAsync(bookId, a.chapter, a.note)
              : a.action === 'audit'
                ? await api.agentsAuditAsync(bookId, a.start ?? a.chapter, a.end ?? a.chapter)
                : await api.agentsWriteAsync(bookId, a.chapter)
        taskIdRef.current[key] = task.task_id
        onAgentTaskStarted?.(task.task_id, a.action)
        // 单飞互斥命中：该书已有审计任务在跑，后端直接复用它、不再起第二个线程
        // （否则两个长任务会串行争抢桥接锁与同一个 Chrome 页面 → 串会话 → 502）
        const auditTask = task as { reused?: boolean; range?: number[] }
        if (a.action === 'audit' && auditTask.reused) {
          const r = auditTask.range ?? []
          setExecResult((prev) => ({
            ...prev,
            [key]: `已有审计任务在跑（第${r[0] ?? '?'}-${r[1] ?? '?'}章），已接管其进度，未重复启动`,
          }))
        }
        let shownPhase = ''
        let navigatedThis = false
        const startedAt = Date.now()
        let lastProgressAt = Date.now()
        let lastSignature = ''
        for (;;) {
          const st = await api.getAgentTask(task.task_id)
          const sig = `${st.status}|${st.phase ?? ''}|${st.phase_detail ?? ''}`
          if (sig !== lastSignature) {
            lastSignature = sig
            lastProgressAt = Date.now()
          }
          if (st.phase === 'writer' && st.stream_text) {
            lastProgressAt = Date.now() // 流式出字也算进展，不触发看门狗
            setExecResult((prev) => ({ ...prev, [key]: `主笔写作中…\n${st.stream_text!.slice(-900)}` }))
            // 该章开写即跳转编辑器到对应正文文件（利用 !bookId 已在 guard；tree 按需拉取 + 缓存）
            if (!navigatedThis && onNavigate && !navSetRef.current.has(a.chapter)) {
              navigatedThis = true
              navSetRef.current.add(a.chapter)
              let path = treeRef.current[a.chapter]
              if (!path && !Object.prototype.hasOwnProperty.call(treeRef.current, a.chapter)) {
                try {
                  const tree = await api.tree(bookId)
                  for (const s of tree.sections) {
                    if (s.section !== '正文') continue
                    for (const f of s.files) {
                      const m = f.path.match(/^第0*(\d+)章/)
                      if (m && parseInt(m[1], 10) === a.chapter) { path = f.path; break }
                    }
                    if (path) break
                  }
                  treeRef.current[a.chapter] = path ?? ''
                } catch { /* 拉取失败不阻断写作 */ }
              }
              if (path && bookId) onNavigate(bookId, path)
            }
          } else if (st.phase && st.phase !== shownPhase) {
            shownPhase = st.phase
            // audit 链的 phase 是动态的（audit_ch36 / report），模式匹配优先于静态表
            const am = /^audit_ch(\d+)$/.exec(st.phase)
            const label = am ? `审计第${am[1]}章` : st.phase === 'report' ? '生成审计报告' : (PHASE_LABEL[st.phase as string] ?? st.phase)
            setExecResult((prev) => ({ ...prev, [key]: `执行中：${label}…` }))
          }
          if (st.status === 'done') {
            if (st.ok || st.result?.ok) {
              setExecState((prev) => ({ ...prev, [key]: 'done' }))
              if (a.action === 'audit') {
                const r = st.result
                const chapters = r?.chapters ?? []
                const total = chapters.reduce((n, c) => n + (c.findings?.length ?? 0), 0)
                const cnt = (lv: string) => chapters.reduce((n, c) => n + (c.findings ?? []).filter((f) => (f.level ?? f.severity) === lv).length, 0)
                // 主动推进：修复派单消息（含报告入口与一键执行动作卡）由 emitAuditFollowup 追加到对话流
                emitAuditFollowup(task.task_id, r, a.start ?? a.chapter, a.end ?? a.chapter)
                setExecResult((prev) => ({ ...prev, [key]: `审计完成（只读诊断，未改正文）：第${a.start ?? a.chapter}-${a.end ?? a.chapter}章，共发现 ${r?.total_findings ?? total} 项（P0×${cnt('P0')} P1×${cnt('P1')} P2×${cnt('P2')}）；修复派单已生成，见对话下方动作卡` }))
              } else {
                setExecResult((prev) => ({ ...prev, [key]: `第${a.chapter}章${a.action === 'rewrite' ? '重写' : a.action === 'deslop' ? '润色' : a.action === 'revise' ? '修订' : '写入'}完成（${(st.result?.stages ?? []).length} 环节）` }))
                // 正文可能新增/改写：刷新存在性，后续动作卡守卫据此判断（一次性查询，不轮询）
                if (a.action === 'write' || a.action === 'rewrite') void refreshExistingChapters()
              }
            } else {
              setExecState((prev) => ({ ...prev, [key]: 'error' }))
              const failed = (st.result?.stages ?? []).find((s) => !s.ok)
              setExecResult((prev) => ({ ...prev, [key]: `执行失败${failed ? `：${failed.name} — ${failed.detail}` : st.error ? `：${st.error}` : ''}` }))
              // 失败不再静默：存下决策载荷（后端增量字段，任务结果类型未声明 → 显式断言）
              const decPayload = st.result as unknown as DecisionPayload | undefined
              if (decPayload?.needs_decision) {
                setExecDecision((prev) => ({ ...prev, [key]: decPayload }))
                setDecisionClosed((prev) => ({ ...prev, [key]: false }))
              }
            }
            return (st.ok || st.result?.ok ? 'done' : 'error') as ExecStatus
          }
          if (st.status === 'cancelled') {
            delete taskIdRef.current[key]
            setExecState((prev) => ({ ...prev, [key]: 'cancelled' }))
            setExecResult((prev) => ({ ...prev, [key]: st.error || '已终止' }))
            return 'cancelled' as ExecStatus
          }
          if (st.status === 'error') {
            delete taskIdRef.current[key]
            setExecState((prev) => ({ ...prev, [key]: 'error' }))
            setExecResult((prev) => ({ ...prev, [key]: `执行失败：${st.error ?? '未知错误'}` }))
            return 'error' as ExecStatus
          }
          const breach = pollBreach(st, startedAt, lastProgressAt)
          if (breach) {
            delete taskIdRef.current[key]
            setExecState((prev) => ({ ...prev, [key]: 'error' }))
            setExecResult((prev) => ({ ...prev, [key]: breach }))
            return 'error' as ExecStatus
          }
          await new Promise((r) => setTimeout(r, st.phase === 'writer' ? 800 : 1500))
        }
      } catch (e) {
        delete taskIdRef.current[key]
        setExecState((prev) => ({ ...prev, [key]: 'error' }))
        setExecResult((prev) => ({ ...prev, [key]: String(e) }))
        return 'error' as ExecStatus
      }
      // 不可达：for(;;) 只通过上面的 return 或 throw 退出。保留以满足 TS 的
      // 「所有路径都有返回值」检查（缺失会导致编译报错）。
      return 'error' as ExecStatus
    },
    [bookId, onNavigate, emitAuditFollowup, verifyChapterExists, refreshExistingChapters],
  )

  /** 一键落盘：把 agent 提案的设定/大纲增补写入对应文件（走既有安全写通道） */
  const applyUpdate = useCallback(
    async (i: number, ui: number, u: ProposedUpdate) => {
      if (!bookId) return
      const key = `${i}-${ui}`
      setApplyState((prev) => ({ ...prev, [key]: 'applying' }))
      try {
        await api.writeFile(bookId, u.path, u.content)
        setApplyState((prev) => ({ ...prev, [key]: 'done' }))
        // 提案落盘后通知 App 刷新编辑器/树（与 execAction / 快捷重写落盘同源）
        onChapterUpdated?.()
      } catch {
        setApplyState((prev) => ({ ...prev, [key]: 'error' }))
      }
    },
    [bookId, onChapterUpdated],
  )

  /** 批量执行：按 actions 顺序串行，execAction 返回值判断成功/失败；finally 释放锁 */
  const runBatch = useCallback(
    async (i: number, actions: ProposedAction[]) => {
      if (!bookId || actions.length === 0) return
      if (batchRunningRef.current) return // 防重复启动
      batchRunningRef.current = true
      batchAbortRef.current = false
      setBatchRunning(true)
      try {
        for (let ai = 0; ai < actions.length; ai++) {
          if (batchAbortRef.current) return
          const cur = execState[`${i}-${ai}`]
          if (cur === 'done') continue
          if (cur === 'error' || cur === 'cancelled') continue // 留给「重试」「继续后续」处理
          const status = await execAction(i, ai, actions[ai])
          if (batchAbortRef.current) return
          if (status !== 'done') return // 失败/取消立即停止
        }
      } finally {
        batchRunningRef.current = false
        setBatchRunning(false)
      }
    },
    [bookId, execState, execAction],
  )

  /** 批量中止：设中止标志 + 终止当前 running 那条；runBatch 下一轮退出 */
  const cancelBatch = useCallback(
    async (i: number, actions: ProposedAction[]) => {
      batchAbortRef.current = true
      for (let ai = 0; ai < actions.length; ai++) {
        if (execState[`${i}-${ai}`] === 'running') { await cancelExec(i, ai); break }
      }
    },
    [execState, cancelExec],
  )

  /** 继续后续：跳过所有 error/cancelled 条目，从第一个 pending 开始 */
  const continueBatch = useCallback(
    async (i: number, actions: ProposedAction[]) => { await runBatch(i, actions) },
    [runBatch],
  )

  /** 重试当前：重新执行失败那条，成功后继续后续 */
  const retryAndContinue = useCallback(
    async (i: number, ai: number, a: ProposedAction, actions: ProposedAction[]) => {
      if (!bookId) return
      const status = await execAction(i, ai, a)
      if (status !== 'done') return
      await runBatch(i, actions)
    },
    [bookId, execAction, runBatch],
  )

  /** 快捷操作：审计/润色/重写/写章 单任务执行（四按钮共用一条执行槽）。
   *  职责边界（2026-09-20 裁决，勿破）：audit=只读诊断不改正文；polish=保剧情提表达（合同守卫）；
   *  rewrite=章节级重新生产（L1 保持剧情 / L2 结构重排 / L3 章节再生产）；
   *  write=首写/补写无正文的章节（正文已存在时不覆盖，避免误伤）。 */
  const runQuick = useCallback(
    async (action: 'audit' | 'polish' | 'rewrite' | 'write', chapter: number, level: RewriteLevel = 'L3', note = '') => {
      if (!bookId || quick?.status === 'running' || busy) return
      if (action !== 'audit' && editorDirty) return // 未保存修改：禁止后台落盘覆盖
      // 首写/重写守卫（T11）：提交前即时复核正文存在性，不凭旧缓存、不静默改写用户意图。
      if (action === 'write' || action === 'rewrite') {
        const exists = await verifyChapterExists(chapter)
        if (action === 'write' && exists === true) {
          setChapterGuardMsg(`第${chapter}章已有正文，写章不会覆盖；如需改稿请用「重写第${chapter}章」`)
          return
        }
        if (action === 'write' && exists === null) {
          setChapterGuardMsg('无法确认正文清单（读取失败），已取消写章，请稍后重试')
          return
        }
        if (action === 'rewrite' && exists !== true) {
          setChapterGuardMsg(exists === false
            ? `第${chapter}章尚无已提交正文，请先「写下一章」（写章会正常生成并落盘）`
            : `无法确认第${chapter}章正文是否存在（正文清单读取失败），已取消重写，请稍后重试`)
          return
        }
      }
      setChapterGuardMsg(null)
      setQuickDecisionClosed(false)
      setQuick({ action, chapter, level, status: 'running', phase: 'queued', resultText: '提交任务…', taskId: '', note })
      try {
        const task = action === 'audit'
          ? await api.agentsAuditAsync(bookId, chapter, chapter)
          : action === 'polish'
            ? await api.agentsPolishAsync(bookId, chapter, note || undefined)
            : action === 'write'
              ? await api.agentsWriteAsync(bookId, chapter)
              : await api.agentsRewriteAsync(bookId, chapter, note || undefined, level)
        quickTaskIdRef.current = task.task_id
        onAgentTaskStarted?.(task.task_id, action)
        setQuick((prev) => (prev && prev.status === 'running' ? { ...prev, taskId: task.task_id, resultText: '执行中…' } : prev))
        const out = await pollAgentTask(task.task_id, {
          onPhase: (phase) => {
            setQuick((prev) => (prev && prev.status === 'running' ? { ...prev, phase, resultText: `执行中：${quickPhaseLabel(phase)}…` } : prev))
          },
          onWriter: (text) => {
            setQuick((prev) => (prev && prev.status === 'running' ? { ...prev, phase: 'writer', resultText: `主笔写作中…\n${text.slice(-700)}` } : prev))
          },
        })
        if (out.status === 'done' && out.ok) {
          const r = out.result
          if (action === 'audit') {
            const ch = (r?.chapters ?? []).find((c: any) => c.chapter === chapter)
            const sum = auditSummary(ch)
            // 主动推进：与派单路径同源——审计结果转修复派单消息追加到对话流（task_id 去重防重复）
            emitAuditFollowup(task.task_id, r, chapter, chapter)
            setQuick((prev) => (prev ? { ...prev, status: 'done', phase: 'report', resultText: `审计完成（只读）：第${chapter}章 共 ${sum.total} 项（P0×${sum.p0} P1×${sum.p1} P2×${sum.p2}）；建议：${sum.suggests}\n修复派单与报告入口见对话下方消息`, result: r } : prev))
          } else {
            const verb = action === 'write' ? '写章' : action === 'polish' ? '润色' : `重写（${level}）`
            const pendingReview = action === 'write' && (r as DecisionPayload | null)?.commit_status === 'pending_author_review'
            const landing = action !== 'write'
              ? ''
              : pendingReview ? '，正文已落盘但未进权威游标（待作者裁决）' : '，正文已落盘'
            setQuick((prev) => (prev ? { ...prev, status: 'done', phase: 'post_write', resultText: `第${chapter}章${verb}完成（${(r?.stages ?? []).length} 环节）${landing}`, result: r } : prev))
            // 当前章正文被改动 → 通知 App 刷新编辑器
            if (activeChapterNum === chapter) onChapterUpdated?.()
            // 写章新增正文 → 重算下一待写章；重写覆盖正文 → 刷新存在性（均为一次性查询，不轮询）
            if (action === 'write') void resolveNextWriteChapter()
            else if (action === 'rewrite') void refreshExistingChapters()
          }
        } else if (out.status === 'cancelled') {
          setQuick((prev) => (prev ? { ...prev, status: 'cancelled', resultText: '已按用户要求终止' } : prev))
        } else {
          // 保留失败载荷：决策卡片需要 failure/facts/decision_options
          setQuick((prev) => (prev ? { ...prev, status: 'error', result: out.result ?? null, resultText: `执行失败：${failedStageText(out.result) || out.error || '未知错误'}` } : prev))
        }
      } catch (e) {
        setQuick((prev) => (prev ? { ...prev, status: 'error', resultText: String(e) } : prev))
      } finally {
        quickTaskIdRef.current = null
      }
    },
    [bookId, quick?.status, busy, editorDirty, activeChapterNum, onChapterUpdated, emitAuditFollowup, verifyChapterExists, refreshExistingChapters, resolveNextWriteChapter],
  )

  /** 验收等待计时：只在等后端时运行，让用户能区分「在跑」与「卡死」 */
  useEffect(() => {
    if (settleStartedAt === null) return
    const t = window.setInterval(() => {
      setSettleElapsed(Math.floor((Date.now() - settleStartedAt) / 1000))
    }, 1000)
    return () => window.clearInterval(t)
  }, [settleStartedAt])

  /** 验收提交（常驻入口）：对当前章补跑完整验收后按原闸正式落账。
   *  语义 = 作者人工确认提交：enforce → 审稿闭环真打分 → 一致性检测+自动闭环 → commit。
   *  不重写正文、不降标准；后端幂等（已提交章返回 already_committed）。
   *  与 quick 槽解耦：acceptance-settle 是同步端点，不是 agent 异步任务，故不占用四按钮共用的进度槽。 */
  const runAcceptanceSettle = async (chapter: number) => {
    if (!bookId || settleBusy || busy || quick?.status === 'running') return
    if (editorDirty) {
      setSettleOut({ ok: false, chapter, text: '编辑器有未保存修改，请先保存再验收提交（防止后台落盘覆盖丢稿）' })
      return
    }
    // 动作前即时复核正文存在性：与重写/写章同口径，不凭旧缓存猜
    const exists = await verifyChapterExists(chapter)
    if (exists !== true) {
      setSettleOut({ ok: false, chapter, text: exists === false
        ? `第${chapter}章尚无正文，无可验收提交（请先用「写下一章」生成正文）`
        : `无法确认第${chapter}章正文是否存在（清单读取失败），请稍后重试` })
      return
    }
    setSettleBusy(true)
    setSettleOut(null)
    setSettleElapsed(0)
    const startedAt = Date.now()
    setSettleStartedAt(startedAt)
    // 后端是同步长链（审稿闭环最多 2 轮 + 外脑评审单次 600s + post_write 300s），
    // 单次网络挂起不能让 UI 永远转圈：前端 10 分钟兜底 + 作者可主动放弃等待。
    const WAIT_MS = 10 * 60 * 1000
    let timer: number | undefined
    const guard = new Promise<never>((_, reject) => {
      settleCancelRef.current = () => reject(new Error('__ABORT_WAIT__'))
      timer = window.setTimeout(() => reject(new Error('__WAIT_TIMEOUT__')), WAIT_MS)
    })
    try {
      const r = await Promise.race([
        api.acceptanceSettle(bookId, chapter, '聊天页常驻「验收提交」入口'),
        guard,
      ])
      if (r.already_committed) {
        const lc = r.last_committed_chapter ?? r.last_after ?? '?'
        const head = `第${chapter}章已在权威游标内（已提交至第${lc}章），无需重复提交。`
        const tail = activeChapterCommitted
          ? `\n注意：你对该章的手动修改不会被验收提交重新落账。如需纳入改动，请点「重写第${chapter}章」(revision) 走全流程重写后重新提交。`
          : ''
        setSettleOut({ ok: true, chapter, text: head + tail })
      } else if (r.committed) {
        const steps = r.steps?.length ? `\n补跑环节：${r.steps.join(' → ')}` : ''
        setSettleOut({ ok: true, chapter, text: `第${chapter}章验收通过并已提交（权威游标 ${r.last_before ?? '?'} → ${r.last_after ?? '?'}）${steps}\n现在可以点「写下一章」继续。` })
        onChapterUpdated?.()
        void resolveNextWriteChapter()
      } else {
        setSettleOut({ ok: false, chapter, text: `第${chapter}章未提交：${r.detail || '验收未达过闸标准（不降标准、不伪造闭环）'}` })
      }
    } catch (e) {
      const msg = String(e)
      if (msg.includes('__ABORT_WAIT__')) {
        setSettleOut({ ok: false, chapter, text: `已放弃等待：后端验收任务不会因此停止，可能仍在后台运行。请勿重复点击（会并发提交）；稍后刷新页面查看第${chapter}章是否已提交。` })
      } else if (msg.includes('__WAIT_TIMEOUT__')) {
        setSettleOut({ ok: false, chapter, text: `已等待 10 分钟仍未返回：验收链可能卡在外部评审或审稿重试。前端已停止等待，但后端任务仍在跑。请勿重复点击；稍后刷新页面查看第${chapter}章是否已提交，或看后端日志 .nww-local/backend.log。` })
      } else {
        setSettleOut({ ok: false, chapter, text: `第${chapter}章验收提交失败：${msg}` })
      }
    } finally {
      if (timer) window.clearTimeout(timer)
      settleCancelRef.current = null
      setSettleBusy(false)
      setSettleStartedAt(null)
    }
  }

  /** 终止快捷任务（协作式：后端在环节边界退出） */
  const cancelQuick = useCallback(async () => {
    const tid = quickTaskIdRef.current
    if (tid) {
      try { await api.cancelAgentTask(tid) } catch { /* ignore */ }
    }
  }, [])

  /** 失败决策执行器：把后端派生的 decision_options 落成实际动作。
   *  重试/扩写/修订/下调目标/接受/上报对话 六条路径，高风险项（改细纲）走二次确认。 */
  const applyDecision = useCallback(
    async (
      opt: { id: string; label: string; hint: string; risk: string; action: string },
      ctx: {
        chapter: number
        actionLabel: string
        d: DecisionPayload
        onRetry: () => void
        onClose: () => void
        onMessage: (t: string) => void
        onEscalate: (t: string) => void
      },
    ) => {
      if (!bookId) return
      const { chapter, actionLabel, d, onRetry, onClose, onMessage, onEscalate } = ctx
      if (opt.action === 'escalate') {
        onEscalate(receiptToPrompt(d, chapter, actionLabel))
        onClose()
        return
      }
      if (opt.action === 'accept') {
        onClose()
        onMessage('已接受现状：保留回滚后的正文，该失败标记为已处理')
        return
      }
      if (opt.action === 'check_endpoint') {
        // 模型端点连通性自检（2026-10-25）：从失败明细解析 [base::model]，一键测试
        const m = /\[(https?:\/\/[^\s\]:]+)::([^\]\s]+)\]/.exec(d.failure?.detail ?? '')
        if (!m) {
          onMessage('未能从失败明细解析出端点，请到模型中心手动测试连通性')
          return
        }
        onMessage(`正在测试 ${m[2]}（${m[1]}）…`)
        try {
          const r = await api.testModelConnection(m[1], '', m[2])
          onMessage(r.ok
            ? `✓ ${m[2]} 连通正常——端点已恢复，可点「重跑本环节」`
            : `✗ ${m[2]} 不可用：${r.detail || '未知原因'}。检查桥接/Chrome 会话后重跑，或到模型中心更换默认模型。`)
        } catch (e) {
          onMessage(`连通性测试失败：${e instanceof Error ? e.message : String(e)}`)
        }
        return
      }
      if (opt.action === 'retry') {
        onRetry()
        return
      }
      if (opt.action === 'expand' || opt.action === 'revise') {
        const floor = d.facts?.required_floor
        const note = opt.action === 'expand' && floor
          ? `在现有正文基础上补写，目标可见字数不低于 ${floor} 字；保留原剧情与结构，只增不删，不得注水凑字`
          : `按失败明细做定点修订：${(d.failure?.detail ?? '').slice(0, 120)}`
        try {
          onMessage(`第${chapter}章${opt.label}执行中…`)
          const t = await api.agentsReviseAsync(bookId, chapter, note)
          const out = await pollAgentTask(t.task_id)
          onMessage(out.status === 'done' && (out.ok || out.result?.ok)
            ? `✓ 第${chapter}章${opt.label}完成`
            : `第${chapter}章${opt.label}失败：${failedStageText(out.result) || out.error || '未知错误'}`)
          onChapterUpdated?.()
        } catch (e) {
          onMessage(String(e))
        }
        return
      }
      if (opt.action === 'lower_target') {
        const opath = d.facts?.outline_path
        const floor = d.facts?.required_floor ?? 0
        if (!opath) {
          onMessage('未定位到细纲文件，无法自动下调字数目标（请手动编辑细纲后重试）')
          return
        }
        const nv = window.prompt(
          `下调细纲字数目标（当前 ${d.facts?.outline_target ?? '?'}，硬下限 ${floor}）。\n此操作会改写 ${opath}，确认请输入新的目标字数：`,
          String(floor),
        )
        if (!nv) return
        const nn = Number.parseInt(nv, 10)
        if (!nn || nn < floor) {
          onMessage(`目标字数须为不小于 ${floor} 的整数，已取消`)
          return
        }
        try {
          const meta = await api.file(bookId, opath)
          const src = meta.content ?? ''
          if (!src) {
            onMessage('细纲读取为空，已取消（未做任何改动）')
            return
          }
          const next = /^\**\s*字数目标\**\s*[:：].*$/m.test(src)
            ? src.replace(/^\**\s*字数目标\**\s*[:：].*$/m, `字数目标：${nn}`)
            : `字数目标：${nn}\n字数口径：visible_chars_v1\n\n${src}`
          await api.writeFile(bookId, opath, next)
          onMessage(`细纲字数目标已下调为 ${nn}，正在重试…`)
          onRetry()
        } catch (e) {
          onMessage(String(e))
        }
      }
    },
    [bookId, onChapterUpdated],
  )

  /** 重新编辑发送：终止进行中的 agent → 把该条读者消息载回输入框，并从该条起截断会话（其后回复一并作废） */
  const editResend = useCallback(
    (idx: number) => {
      // 忙时点击：先终止 agent，且不追加终止提示（避免污染回退后的会话）
      const ac = abortRef.current
      abortRef.current = null
      ac?.abort()
      const text = msgs[idx]?.content ?? ''
      setMsgs((prev) => prev.slice(0, idx))
      setInput(text)
      requestAnimationFrame(() => inputRef.current?.focus())
    },
    [msgs],
  )

  /** 复制 agent 回复到剪贴板 */
  const copyMsg = useCallback(async (idx: number, text: string) => {
    try {
      await navigator.clipboard.writeText(text)
    } catch {
      const ta = document.createElement('textarea')
      ta.value = text
      ta.style.position = 'fixed'
      ta.style.opacity = '0'
      document.body.appendChild(ta)
      ta.select()
      try { document.execCommand('copy') } catch { /* ignore */ }
      ta.remove()
    }
    setCopiedIdx(idx)
    window.setTimeout(() => setCopiedIdx((c) => (c === idx ? null : c)), 1500)
  }, [])

  // 切书 / 点「＋新建书籍」（createNonce 变化）时加载对应会话
  useEffect(() => {
    setMsgs(loadMsgs(chatKey(bookId, createNonce)))
  }, [bookId, createNonce])

  // 持久化 + 滚到底
  useEffect(() => {
    try { localStorage.setItem(chatKey(bookId, createNonce), JSON.stringify(msgs.slice(-80))) } catch { /* ignore */ }
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [msgs, bookId, createNonce])

  // 思考计时器：busy 期间每秒刷新已等待时长（思考指示器的「动态」之一）
  useEffect(() => {
    if (!busy) return
    const t0 = Date.now()
    setElapsed(0)
    const iv = window.setInterval(() => setElapsed(Math.floor((Date.now() - t0) / 1000)), 1000)
    return () => window.clearInterval(iv)
  }, [busy])

  // 输入区高度自适应（多行输入）：内容变化时按 scrollHeight 伸缩，最高 120px 溢出滚动
  useEffect(() => {
    const ta = inputRef.current
    if (ta) {
      ta.style.height = 'auto'
      ta.style.height = Math.min(ta.scrollHeight, 120) + 'px'
    }
  }, [input])

  /** 会话发送内核：给定最终文本 → 追加为 user 消息 → 请求 agent → 追加回复 */
  const dispatch = useCallback(
    async (text: string) => {
      if (!text || busy) return
      const history = [...msgs, { role: 'user' as const, content: text }]
      setMsgs(history)
      setBusy(true)
      setThinkProgress(null)
      const ac = new AbortController()
      abortRef.current = ac
      try {
        // 历史过滤：错误消息不回传；空 content 消息（如只有协议块被剥离、只剩目标卡的
        // 回复）也不回传——后端 ChatMessage 要求 content ≥1 字符，带上会整包 422
        const outbound = history.filter((m) => !m.error && m.content.trim())
        // 优先走 SSE 流（思考进程实时上报）；旧后端无此端点（404）自动回退同步版
        let reply: AgentChatReply
        try {
          reply = await api.agentChatStream(
            bookId, activePath ?? null, outbound,
            { onEvent: (e) => { if (e.type === 'progress') setThinkProgress(e.detail) } },
            ac.signal, poolId || null)
        } catch (e) {
          const aborted = e instanceof DOMException && e.name === 'AbortError'
          const msg = e instanceof Error ? e.message : String(e)
          if (aborted || !/not found|404/i.test(msg)) throw e
          reply = await api.agentChat(bookId, activePath ?? null, outbound, ac.signal, poolId || null)
        }
        setMsgs((prev) => [...prev, { role: 'assistant', content: reply.reply, thinking: reply.thinking ?? null, updates: reply.proposed_updates ?? null, actions: reply.proposed_actions ?? null, notice: reply.execution_notice ?? null, draft: reply.book_draft ?? null, goal: reply.proposed_goal ?? null, model: reply.model ?? null }])
      } catch (err) {
        // 主动终止：stop 按钮触发补一条提示；编辑回退触发（abortRef 已置空）静默处理
        const aborted = err instanceof DOMException ? err.name === 'AbortError' : false
        if (aborted) {
          if (abortRef.current === ac) {
            setMsgs((prev) => [...prev, { role: 'assistant', content: '（本轮生成已终止）' }])
          }
        } else {
          setMsgs((prev) => [...prev, { role: 'assistant', content: String(err instanceof Error ? err.message : err), error: true }])
        }
      } finally {
        if (abortRef.current === ac) abortRef.current = null
        setBusy(false)
        setThinkProgress(null)
      }
    },
    [busy, msgs, bookId, activePath, poolId],
  )

  /** 输入框发送：拼装附件后走 dispatch */
  const send = useCallback(
    async (e?: FormEvent) => {
      e?.preventDefault()
      // 附件注入：用户输入在前，附件文本追加在后；整体截断对齐后端 MAX_CONTENT_CHARS=8000
      const body = input.trim()
      const attachText = attachments.map((a) => `【附件：${a.name}】\n${a.content}`).join('\n\n')
      const text = (attachText ? `${body}\n\n${attachText}` : body).slice(0, 7800).trim()
      if (!text || busy) return
      setInput('')
      setAttachments([])
      await dispatch(text)
    },
    [input, busy, attachments, dispatch],
  )

  /** 终止本轮生成（发出后 LLM 正在推理时） */
  const stop = useCallback(() => {
    abortRef.current?.abort()
  }, [])

  /* ── 建书三列工作台接线（2026-11-07，只增不改） ──
     ① 广播本会话真实快照 → 中间「建书流程」卡推导成果/缺口/进度；
     ② 接收中间卡的方向选择 → 作为一条普通用户消息走既有 dispatch，
        不新增端点、不改后端协议。仅在 createMode 且未绑书时生效。 */
  useEffect(() => {
    if (!createMode || bookId !== null) return
    const latestDraft = [...msgs].reverse().find((m) => m.role === 'assistant' && m.draft)?.draft ?? null
    emitCreateFlow({
      nonce: createNonce,
      busy,
      thinking: thinkProgress,
      messages: msgs
        .filter((m) => !m.error && m.content.trim())
        .map((m) => ({ role: m.role, content: m.content })),
      draft: latestDraft,
    })
  }, [createMode, bookId, createNonce, busy, thinkProgress, msgs])

  useEffect(() => {
    if (!createMode || bookId !== null) return
    const onSend = (e: Event) => {
      const detail = (e as CustomEvent<CreateFlowSendDetail>).detail
      if (!detail || detail.nonce !== createNonce || !detail.text) return
      void dispatch(detail.text)
    }
    window.addEventListener(CREATE_FLOW_SEND_EVENT, onSend)
    return () => window.removeEventListener(CREATE_FLOW_SEND_EVENT, onSend)
  }, [createMode, bookId, createNonce, dispatch])

  return (
    <div className="h-full flex flex-col min-h-0">

      {/* 消息区：三张卡片统一内边距 card-pad（左右 12px）；仅顶部加渐隐遮罩（底部不需要） */}
      <div className="flex-1 min-h-0 relative">
        <div ref={scrollRef} className="h-full card-scroll space-y-3">
          {msgs.map((m, i) => {
            const isUser = m.role === 'user'
            const copied = copiedIdx === i
            const updates = !isUser && !m.error ? (m.updates ?? null) : null
            const actions = !isUser && !m.error ? (m.actions ?? null) : null
            const notices = !isUser && !m.error ? (m.notice ?? null) : null
            const draftMsg = !isUser && !m.error ? (m.draft ?? null) : null
            const goalMsg = !isUser && !m.error ? (m.goal ?? null) : null
            return (
              <div key={i} className="flex flex-col gap-1.5">
                {/* 模型思考内容：独立「思考中」折叠块（与正文内容区分展示） */}
                {!isUser && !m.error && m.thinking && (
                  <div className="bubble-max rounded-xl border border-[#333]/60 bg-[#141414] overflow-hidden">
                    <details className="group">
                      <summary className="flex items-center gap-1.5 px-2 py-1.5 text-[12px] text-[#888] cursor-pointer select-none list-none">
                        <span className="flex-1">思考内容</span>
                        <svg className={`w-3 h-3 shrink-0 transition-transform group-open:rotate-180`} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                          <path d="m6 9 6 6 6-6" />
                        </svg>
                      </summary>
                      <div className="px-2 pb-2 pt-1.5 text-[12px] text-[#777] whitespace-pre-wrap break-words max-h-40 overflow-y-auto leading-relaxed border-t border-[#2a2a2a]">
                        {m.thinking}
                      </div>
                    </details>
                  </div>
                )}
                <div className={`group flex items-end gap-1.5 ${isUser ? 'justify-end' : 'justify-start'}`}>
                  {/* 读者气泡：重新编辑发送 */}
                  {isUser && (
                    <button
                      type="button"
                      onClick={() => editResend(i)}
                      title={busy ? '终止 agent 并回退到此条重新编辑' : '重新编辑发送'}
                      className={`${BUBBLE_ACTION_CLS} z-10 opacity-0 group-hover:opacity-100`}
                    >
                      <EditIcon />
                    </button>
                  )}

                  <div
                    className={`bubble-max px-3 py-2 rounded-xl border text-[14px] leading-relaxed whitespace-pre-wrap break-words ${
                      m.error
                        ? 'border-red-900/50 text-red-300'
                        : isUser
                          ? 'border-[#f59e0b]/30 text-[#e8d5b5]'
                          : 'border-[#333] text-[#c8c8c8]'
                    }`}
                    dangerouslySetInnerHTML={{ __html: renderText(m.content) }}
                    hidden={!isUser && !m.content}
                  />

                  {/* agent 气泡：复制 */}
                  {!isUser && !m.error && (
                    <button
                      type="button"
                      onClick={() => copyMsg(i, m.content)}
                      title={copied ? '已复制' : '复制'}
                      className={`${BUBBLE_ACTION_CLS} sticky top-2 self-start ${
                        copied ? 'opacity-100 text-[#f59e0b]' : 'opacity-0 group-hover:opacity-100'
                      }`}
                    >
                      {copied ? <CheckIcon /> : <CopyIcon />}
                    </button>
                  )}
                </div>

                {/* 本条回复的生成模型（后端回传实际命中端点；旧会话无此字段不显示） */}
                {!isUser && !m.error && m.model && (
                  <div className="bubble-max flex items-baseline gap-1.5 pl-0.5 text-[11px] leading-relaxed">
                    <span className="text-[#555]">生成模型</span>
                    <code className="font-mono text-[#777] break-all" title={`本条回复由 ${m.model} 生成`}>{m.model}</code>
                  </div>
                )}

                {/* 审计报告入口：窗口内打开（编辑器只读）/ 另开浏览器窗口（渲染视图） */}
                {!isUser && !m.error && m.audit_report && (
                  <ReportLinkRow path={m.audit_report} onOpen={() => openReport(m.audit_report!)} onExternal={() => { void openReportExternal(m.audit_report!) }} />
                )}

                {/* 结构化执行事件（P0-C）：动作归一化 / 指令被拒，独立提示位，不混入正文 */}
                {notices && notices.length > 0 && (
                  <div className="flex flex-col gap-1 pl-0.5">
                    {notices.map((nt, ni) => {
                      const normalized = nt.type === 'action_normalized'
                      const rebound = nt.type === 'action_chapter_rebound'
                      const why =
                        nt.normalized_action === 'write'
                          ? '该章节当前没有正文'
                          : nt.normalized_action === 'rewrite'
                            ? '该章节已有正文'
                            : ''
                      const tone = rebound
                        ? 'border-violet-500/30 bg-[#161022] text-violet-300'
                        : normalized
                          ? 'border-sky-500/30 bg-[#101a22] text-sky-300'
                          : 'border-amber-600/40 bg-[#1c1408] text-amber-300'
                      return (
                        <div
                          key={ni}
                          className={`bubble-max rounded-xl px-3 py-1.5 text-[12px] leading-relaxed border ${tone}`}
                        >
                          {rebound
                            ? `已按「当前打开章节」校正执行目标：原第${nt.from_chapter}章 → 第${nt.chapter}章（本条消息未指定章号）。`
                            : normalized
                              ? `系统已将「${execActionLabel(nt.requested_action, nt.chapter)}」自动调整为「${execActionLabel(nt.normalized_action, nt.chapter)}」${why ? `（${why}）` : ''}。`
                              : `执行动作未生成：${nt.reason || '指令未通过校验'}。`}
                        </div>
                      )
                    })}
                  </div>
                )}

                {/* 建书草案确认卡（DRAFT_JSON 协议产物，仅未绑书态下发） */}
                {draftMsg && <DraftCard draft={draftMsg} onCreate={onCreateBook} busy={busy} />}

                {/* 监督目标确认卡（GOAL_JSON 协议产物，仅绑书态下发）：点「开始监督」后监督器自动连续推进 */}
                {goalMsg && <GoalCard goal={goalMsg} bookId={bookId} busy={busy} />}

                {/* agent 提案的设定/大纲文件增补：逐条「预览 + 应用」 */}
                {updates && updates.length > 0 && (
                  <div className="flex flex-col gap-1.5 pl-0.5">
                    {updates.map((u, ui) => {
                      const st = applyState[`${i}-${ui}`] ?? 'idle'
                      return (
                        <div key={ui} className="bubble-max border border-[#3a3a3a]/70 rounded-xl px-3 py-2 bg-[#161616]">
                          <div className="flex items-center justify-between gap-3">
                            <span className="min-w-0 text-[13px] text-[#a8a8a8] font-mono truncate" title={u.path}>
                              {u.path}
                            </span>
                            <button
                              type="button"
                              disabled={st === 'applying' || st === 'done'}
                              onClick={() => applyUpdate(i, ui, u)}
                              className={`shrink-0 px-2.5 py-1 rounded-md border text-[12px] transition-colors ${
                                st === 'done'
                                  ? 'border-emerald-500/40 text-emerald-400 cursor-default'
                                  : st === 'error'
                                    ? 'border-red-800/50 text-red-300 hover:border-red-500'
                                    : st === 'applying'
                                      ? 'border-[#3a3a3a] text-[#888] cursor-wait'
                                      : 'border-[#f59e0b]/30 text-[#f59e0b] hover:bg-[#f59e0b]/10'
                              }`}
                            >
                              {st === 'done' ? '已写入' : st === 'error' ? '失败 · 重试' : st === 'applying' ? '写入中…' : '应用'}
                            </button>
                          </div>
                          <details className="mt-1">
                            <summary className="text-[12px] text-[#666] cursor-pointer select-none">预览将追加的内容</summary>
                            <pre className="mt-1.5 text-[12px] whitespace-pre-wrap break-words text-[#999] max-h-40 overflow-y-auto leading-relaxed">
                              {u.preview}
                            </pre>
                          </details>
                        </div>
                      )
                    })}
                  </div>
                )}
                {/* agent 派单的 pipeline 执行：逐条「一键执行」 */}
                {actions && actions.length > 0 && (
                  <div className="flex flex-col gap-1.5 pl-0.5">
                    {/* 批量执行控制条：≥2 条才显示；运行判断用 batchRunning state，进度从 execState 推导 */}
                    {actions.length >= 2 && (() => {
                      const states = actions.map((_, ai) => execState[`${i}-${ai}`] ?? 'idle')
                      const doneCount = states.filter((s) => s === 'done').length
                      const errIdx = states.findIndex((s) => s === 'error' || s === 'cancelled')
                      const total = actions.length
                      const fullBar = states
                        .map((s, ai) => `${actions[ai].action === 'set_register' ? '档' : actions[ai].action === 'deslop_text' ? '润' : actions[ai].chapter}${s === 'done' ? '✓' : s === 'running' ? '●' : s === 'error' || s === 'cancelled' ? '✗' : '○'}`)
                        .join(' ')
                      return (
                        <div className="bubble-max border border-[#2a5f2a]/40 rounded-xl px-3 py-2 bg-[#142014]">
                          <div className="flex items-center gap-3 flex-wrap">
                            {!batchRunning && errIdx < 0 && doneCount < total && (
                              <button
                                type="button"
                                onClick={() => runBatch(i, actions)}
                                className="shrink-0 px-3 py-1 rounded-md border border-emerald-500/50 text-emerald-400 hover:bg-emerald-500/10 text-[12px] font-medium transition-colors"
                              >
                                全部执行（{total}）
                              </button>
                            )}
                            {batchRunning && (
                              <>
                                <span className="text-[12px] text-amber-400 font-medium">
                                  批量执行中 {doneCount}/{total}
                                </span>
                                <button
                                  type="button"
                                  onClick={() => cancelBatch(i, actions)}
                                  className="px-2 py-1 rounded-md border border-red-800/50 text-red-300 hover:border-red-500 text-[12px] transition-colors"
                                  title="中止批量执行"
                                >
                                  批量中止
                                </button>
                              </>
                            )}
                            {!batchRunning && errIdx >= 0 && (
                              <>
                                <span className="text-[12px] text-red-400 font-medium">
                                  批量已停止：第{actions[errIdx].chapter}章失败（{doneCount}/{total} 成功）
                                </span>
                                <button
                                  type="button"
                                  onClick={() => retryAndContinue(i, errIdx, actions[errIdx], actions)}
                                  className="px-2 py-1 rounded-md border border-[#f59e0b]/40 text-[#f59e0b] hover:bg-[#f59e0b]/10 text-[12px] transition-colors"
                                >
                                  重试第{actions[errIdx].chapter}章
                                </button>
                                {errIdx < total - 1 && (
                                  <button
                                    type="button"
                                    onClick={() => continueBatch(i, actions)}
                                    className="px-2 py-1 rounded-md border border-emerald-500/50 text-emerald-400 hover:bg-emerald-500/10 text-[12px] transition-colors"
                                  >
                                    继续后续（{total - errIdx - 1}章）
                                  </button>
                                )}
                              </>
                            )}
                            {!batchRunning && errIdx < 0 && doneCount === total && (
                              <span className="text-[12px] text-emerald-400 font-medium">全部执行完成：{doneCount}/{total}</span>
                            )}
                            <span className="flex-1" />
                            <code className="text-[11px] text-[#666] font-mono">{fullBar}</code>
                          </div>
                        </div>
                      )
                    })()}
                    {actions.map((a, ai) => {
                      const st = execState[`${i}-${ai}`] ?? 'idle'
                      const res = execResult[`${i}-${ai}`] ?? ''
                      const dec = execDecision[`${i}-${ai}`] ?? null
                      const label = a.action === 'set_register'
                        ? execActionLabel(a.action, undefined, a.register)
                        : a.action === 'rewrite' ? `重写第${a.chapter}章${a.level ? `（${a.level}）` : ''}` : a.action === 'deslop' ? `润色第${a.chapter}章` : a.action === 'deslop_text' ? '润色贴文（引擎改写）' : a.action === 'revise' ? `修订第${a.chapter}章` : a.action === 'audit' ? `一致性审计第${a.start ?? a.chapter}-${a.end ?? a.chapter}章（只读诊断）` : `写第${a.chapter}章`
                      return (
                        <div key={ai} className="bubble-max border border-[#f59e0b]/25 rounded-xl px-3 py-2 bg-[#161616]">
                          <div className="flex items-center justify-between gap-3">
                            <span className="min-w-0 text-[13px] text-[#f59e0b] font-medium truncate">{label}</span>
                            <div className="flex items-center gap-1.5 shrink-0">
                              {st === 'running' && (
                                <button
                                  type="button"
                                  onClick={() => cancelExec(i, ai)}
                                  className="px-2 py-1 rounded-md border border-red-800/50 text-red-300 hover:border-red-500 text-[12px] transition-colors"
                                  title="终止执行（协作式，当前环节完成后退出）"
                                >
                                  终止
                                </button>
                              )}
                              <button
                                type="button"
                                disabled={batchRunning || st === 'running' || st === 'done'}
                                onClick={() => execAction(i, ai, a)}
                                className={`shrink-0 px-2.5 py-1 rounded-md border text-[12px] transition-colors ${
                                  st === 'done'
                                    ? 'border-emerald-500/40 text-emerald-400 cursor-default'
                                    : st === 'error' || st === 'cancelled'
                                      ? 'border-red-800/50 text-red-300 hover:border-red-500'
                                      : st === 'running'
                                        ? 'border-[#3a3a3a] text-[#888] cursor-wait'
                                        : 'border-[#f59e0b]/40 text-[#f59e0b] hover:bg-[#f59e0b]/10'
                                }`}
                              >
                                {st === 'done' ? '已执行' : st === 'error' ? '失败 · 重试' : st === 'cancelled' ? '已终止 · 重试' : st === 'running' ? '执行中…' : '一键执行'}
                              </button>
                            </div>
                          </div>
                          {a.note && <div className="mt-1 text-[12px] text-[#999] whitespace-pre-wrap break-words leading-relaxed">{a.note}</div>}
                          {res && <div className={`mt-1 text-[12px] break-words ${st === 'error' ? 'text-red-400' : 'text-emerald-400'}`}>{res}</div>}
                          <DecisionCard
                            d={dec}
                            chapter={a.chapter}
                            actionLabel={label}
                            closed={decisionClosed[`${i}-${ai}`]}
                            onApply={(o) => void applyDecision(o, {
                              chapter: a.chapter,
                              actionLabel: label,
                              d: dec as DecisionPayload,
                              onRetry: () => { void execAction(i, ai, a) },
                              onClose: () => setDecisionClosed((prev) => ({ ...prev, [`${i}-${ai}`]: true })),
                              onMessage: (t) => setExecResult((prev) => ({ ...prev, [`${i}-${ai}`]: t })),
                              onEscalate: (t) => { void dispatch(t) },
                            })}
                            onClose={() => setDecisionClosed((prev) => ({ ...prev, [`${i}-${ai}`]: true }))}
                          />
                        </div>
                      )
                    })}
                  </div>
                )}
              </div>
            )
          })}
          {busy && (
            <div className="flex justify-start">
              <div className="px-3 py-2 rounded-xl border border-[#333] text-[#777] text-[14px]">
                <span className="inline-flex items-center gap-2">
                  思考中
                  {/* 呼吸点动画：错峰 pulse，让等待状态「活着」 */}
                  <span className="inline-flex items-center gap-1" aria-hidden>
                    <span className="w-1.5 h-1.5 rounded-full bg-[#8a8a8a] animate-pulse" />
                    <span className="w-1.5 h-1.5 rounded-full bg-[#8a8a8a] animate-pulse" style={{ animationDelay: '300ms' }} />
                    <span className="w-1.5 h-1.5 rounded-full bg-[#8a8a8a] animate-pulse" style={{ animationDelay: '600ms' }} />
                  </span>
                  <span className="text-[12px] text-[#666] tabular-nums">{elapsed}s</span>
                </span>
                {/* 思考进程行（SSE progress 实时上报：第几轮生成/自主取材/纠偏重试） */}
                {thinkProgress && (
                  <div className="mt-1 text-[12px] text-[#8a8a8a] max-w-[260px] truncate" title={thinkProgress}>
                    {thinkProgress}
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
        <div className="edge-fade edge-fade-top" />
      </div>

      {/* 输入区：Gemini 式结构 —— 圆角卡片内含 textarea + 底部工具行（附件钮 ×N ｜ 模型选择器 ▽ ｜ 发送 ⬆） */}
      <form onSubmit={send} className="shrink-0 card-pad">

        {/* 监督状态条（绑书态）：目标进度 / 在跑任务 / 待办闸门 / 暂停恢复；后端未开监督器时自动隐藏 */}
        {bookId && <SupervisorBar bookId={bookId} />}

        {/* 快捷操作条（输入窗口上方）：审计/优化/重写/写章 —— 章号随编辑器当前章节动态（chapterFromPath） */}
        {bookId && (
          <div className="mb-2 flex flex-col gap-1.5">
            {activeChapter ? (
              <div className="flex items-center gap-1.5 flex-wrap">
                <button
                  type="button"
                  disabled={busy || quick?.status === 'running' || settleBusy}
                  onClick={() => void runQuick('audit', activeChapter.num)}
                  title="只读诊断：A 细纲 / B 世界事实 / C 角色 / D 伏笔漂移 → P0/P1/P2 报告+建议动作（绝不改正文）"
                  className="flex items-center gap-1 h-7 px-2.5 rounded-lg border border-[#a78bfa]/35 text-[#c4b5fd] hover:bg-[#a78bfa]/10 disabled:opacity-40 text-[12px] transition-colors"
                >
                  审计第{activeChapter.num}章
                  <span className="text-[10px] text-[#7c6fb3]">只读</span>
                </button>
                <button
                  type="button"
                  disabled={busy || quick?.status === 'running' || editorDirty || settleBusy}
                  onClick={() => setPolishDlg({ chapter: activeChapter.num, note: '' })}
                  title={editorDirty ? '编辑器有未保存修改，请先保存（防止后台落盘覆盖丢稿）' : '保剧情提表达：行文/场景/语言逻辑 + 阅读节奏 + 去AI味；可填润色意见（最高优先级执行）；章节合同守卫防漂移'}
                  className="flex items-center gap-1 h-7 px-2.5 rounded-lg border border-[#34d399]/35 text-[#6ee7b7] hover:bg-[#34d399]/10 disabled:opacity-40 text-[12px] transition-colors"
                >
                  优化第{activeChapter.num}章
                  <span className="text-[10px] text-[#2f8f66]">保剧情</span>
                </button>
                <button
                  type="button"
                  disabled={busy || quick?.status === 'running' || editorDirty || settleBusy}
                  onClick={() => { void (async () => {
                    // 重写快捷守卫：正文不存在时不展开等级选择、不发起请求，直接提示改点「写下一章」
                    const exists = await verifyChapterExists(activeChapter.num)
                    if (exists !== true) {
                      setChapterGuardMsg(exists === false
                        ? `第${activeChapter.num}章尚无已提交正文，请先「写下一章」（写章会正常生成并落盘）`
                        : `无法确认第${activeChapter.num}章正文是否存在（正文清单读取失败），请稍后重试`)
                      setRewriteDlg(null)
                      return
                    }
                    setChapterGuardMsg(null)
                    setRewriteDlg({ chapter: activeChapter.num, level: 'L3', note: '' })
                  })() }}
                  title={editorDirty ? '编辑器有未保存修改，请先保存（防止后台落盘覆盖丢稿）' : '章节级重新生产：L1 保持剧情重写 / L2 章节结构重写 / L3 章节再生产（全量重进写作链）；仅限已有正文的章节'}
                  className="flex items-center gap-1 h-7 px-2.5 rounded-lg border border-[#f59e0b]/35 text-[#fbbf24] hover:bg-[#f59e0b]/10 disabled:opacity-40 text-[12px] transition-colors"
                >
                  重写第{activeChapter.num}章
                  <span className="text-[10px] text-[#8a5a14]">L1-L3</span>
                </button>
                <button
                  type="button"
                  disabled={busy || quick?.status === 'running' || editorDirty || settleBusy}
                  onClick={() => void runAcceptanceSettle(activeChapter.num)}
                  title={editorDirty
                    ? '编辑器有未保存修改，请先保存再验收提交'
                    : activeChapterCommitted
                      ? `第${activeChapter.num}章已验收提交（已提交至第${lastCommitted}章）。验收提交对已提交章是幂等跳过，**不会**重新验收你的手动修改；如需纳入改动，请点「重写第${activeChapter.num}章」(revision) 走全流程重写后重新落账。`
                      : `对第${activeChapter.num}章补跑完整验收（delta 草稿 → 机械层 → 审稿闭环真打分 → 一致性检测与闭环 → 原闸提交）后正式落账；不重写正文、不降标准；未提交章才真正落账`}
                  className="flex items-center gap-1 h-7 px-2.5 rounded-lg border border-[#94a3b8]/40 text-[#cbd5e1] hover:bg-[#94a3b8]/10 disabled:opacity-40 text-[12px] transition-colors"
                >
                  {settleBusy
                    ? `验收中… ${String(Math.floor(settleElapsed / 60)).padStart(2, '0')}:${String(settleElapsed % 60).padStart(2, '0')}`
                    : '验收提交'}
                  <span className="text-[10px] text-[#64748b]">第{activeChapter.num}章</span>
                </button>
                {settleBusy && (
                  <button
                    type="button"
                    onClick={() => settleCancelRef.current?.()}
                    title="仅停止前端等待；后端验收任务不会终止，请勿重复点击"
                    className="h-7 px-2 rounded-lg border border-[#94a3b8]/30 text-[#94a3b8] hover:bg-[#94a3b8]/10 text-[11px] transition-colors"
                  >
                    放弃等待
                  </button>
                )}
                <button
                  type="button"
                  disabled={busy || quick?.status === 'running' || editorDirty || settleBusy}
                  onClick={() => { void (async () => {
                    // 「写下一章」点击时重算目标章：正文可能刚落盘，用打开正文时的旧缓存
                    // 会派到已有正文的章并被后端 409 防覆盖闸拒绝。清单读失败则不派单。
                    const next = await resolveNextWriteChapter()
                    if (next === null) {
                      setChapterGuardMsg('无法确认正文清单（读取失败），已取消写下一章，请稍后重试')
                      return
                    }
                    await runQuick('write', next)
                  })() }}
                  title={editorDirty
                    ? '编辑器有未保存修改，请先保存再写章'
                    : `按细纲顺序写下一个待写章（第${nextWriteChapter ?? '…'}章）；${activeChapterExists ? `当前打开的第${activeChapter.num}章已有正文，不会被覆盖` : '已有正文的章不会被覆盖'}`}
                  className="flex items-center gap-1 h-7 px-2.5 rounded-lg border border-[#38bdf8]/35 text-[#7dd3fc] hover:bg-[#38bdf8]/10 disabled:opacity-40 text-[12px] transition-colors"
                >
                  写下一章
                  <span className="text-[10px] text-[#2e7f99]">{nextWriteChapter !== null ? `第${nextWriteChapter}章` : '待确认'}</span>
                </button>
                {editorDirty && (
                  <span className="text-[11px] text-[#b45309]" title="编辑器有未保存修改，先保存再优化/重写/写章（审计只读不受限）">
                    ⚠ 未保存 · 请先保存
                  </span>
                )}

                {/* 快捷任务进度/结果（单槽：三按钮共用） */}
                {quick && (
                  <div className={`w-full bubble-max rounded-lg px-2.5 py-1.5 text-[12px] border ${
                    quick.status === 'error' ? 'border-red-900/50 text-red-300'
                    : quick.status === 'cancelled' ? 'border-[#3a3a3a] text-[#999]'
                    : quick.status === 'done' ? 'border-emerald-600/40 text-emerald-300'
                    : 'border-[#3a3a3a] text-[#ccc]'
                  }`}>
                    <div className="flex items-center gap-2">
                      <span className="font-medium shrink-0">
                        {quick.status === 'running'
                          ? `${quickActionLabel(quick.action, quick.level)}第${quick.chapter}章 · ${quickPhaseLabel(quick.phase)}`
                          : quick.status === 'done'
                            ? `✓ ${quickActionLabel(quick.action, quick.level)}第${quick.chapter}章完成`
                            : quick.status === 'cancelled' ? '已终止' : '执行失败'}
                      </span>
                      <span className="flex-1" />
                      {quick.status === 'running' && (
                        <button
                          type="button"
                          onClick={() => void cancelQuick()}
                          className="shrink-0 px-2 py-0.5 rounded border border-red-800/50 text-red-300 hover:border-red-500 text-[11px] transition-colors"
                        >
                          终止
                        </button>
                      )}
                      {(quick.status === 'done' || quick.status === 'error' || quick.status === 'cancelled') && (
                        <button
                          type="button"
                          onClick={() => setQuick(null)}
                          className="shrink-0 text-[#666] hover:text-[#aaa] text-[13px] leading-none"
                        >
                          ×
                        </button>
                      )}
                    </div>
                    {quick.resultText && (
                      <div className="mt-1 whitespace-pre-wrap break-words leading-relaxed">{quick.resultText}</div>
                    )}
                    {/* 写章落盘但未提交（C-WRITE-RESULT@1）：作者必须看得见「待裁决」而不是笼统成功 */}
                    {quick.status === 'done'
                      && quick.action === 'write'
                      && (quick.result as DecisionPayload | null)?.commit_status === 'pending_author_review' && (
                      <div className="mt-1.5 rounded border border-amber-700/50 bg-[#1a1408] px-2 py-1.5">
                        <div className="flex items-center gap-1.5 flex-wrap">
                          <span className="px-1 rounded bg-amber-900/40 text-amber-300 text-[11px]">已落盘 · 待作者裁决</span>
                          <span className="text-[11px] text-[#b9a273]">正文在盘上，未进权威游标，不计为已提交</span>
                          {(quick.result as DecisionPayload).degraded_pass && (
                            <span className="text-[11px] text-[#b9a273]">· v13 降级翻正（机械层未过）</span>
                          )}
                        </div>
                        {((quick.result as DecisionPayload).warnings ?? []).length > 0 && (
                          <ul className="mt-1 flex flex-col gap-0.5">
                            {((quick.result as DecisionPayload).warnings ?? []).map((w, i) => (
                              <li key={i} className="text-[11px] text-[#c9b48a] break-words leading-snug">· {w}</li>
                            ))}
                          </ul>
                        )}
                        <div className="mt-1 text-[11px] text-[#8a7a55]">事实卡与裁决入口见下方决策卡 / 千章守护中心。</div>
                      </div>
                    )}
                    <DecisionCard
                      d={quick.result as DecisionPayload | null}
                      chapter={quick.chapter}
                      actionLabel={`${quickActionLabel(quick.action, quick.level)}第${quick.chapter}章`}
                      closed={quickDecisionClosed}
                      onApply={(o) => void applyDecision(o, {
                        chapter: quick.chapter,
                        actionLabel: `${quickActionLabel(quick.action, quick.level)}第${quick.chapter}章`,
                        d: quick.result as DecisionPayload,
                        onRetry: () => { void runQuick(quick.action, quick.chapter, quick.level, quick.note ?? '') },
                        onClose: () => setQuickDecisionClosed(true),
                        onMessage: (t) => setQuick((prev) => (prev ? { ...prev, resultText: t } : prev)),
                        onEscalate: (t) => { void dispatch(t) },
                      })}
                      onClose={() => setQuickDecisionClosed(true)}
                    />
                    {/* audit 完成：渲染前 6 条发现（等级徽标 + 详情 + 建议动作） */}
                    {quick.status === 'done' && quick.action === 'audit' && quick.result != null && (
                      <div className="mt-1.5 flex flex-col gap-0.5">
                        {((quick.result as any).chapters ?? []).flatMap((c: any) => c.findings ?? []).slice(0, 6).map((f: AuditFinding) => {
                          const sev = f.level ?? f.severity
                          return (
                            <div key={f.issue_id} className="flex items-start gap-1.5 text-[11px] leading-snug">
                              <span className={`shrink-0 px-1 rounded ${
                                sev === 'P0' ? 'bg-red-900/40 text-red-300'
                                : sev === 'P1' ? 'bg-amber-900/40 text-amber-300'
                                : 'bg-[#3a3a3a] text-[#999]'
                              }`}>{sev}</span>
                              <span className="min-w-0 break-words text-[#bbb]">{f.detail}</span>
                              <span className="shrink-0 text-[#f59e0b]/80">{f.suggestion}</span>
                            </div>
                          )
                        })}
                        {(quick.result as any).report_md && (
                          <div className="mt-1">
                            <ReportLinkRow compact
                              path={(quick.result as any).report_md as string}
                              onOpen={() => openReport((quick.result as any).report_md as string)}
                              onExternal={() => { void openReportExternal((quick.result as any).report_md as string) }}
                            />
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                )}
              </div>
            ) : (
              <div className="flex items-center gap-1.5 flex-wrap">
                <span className="text-[11px] text-[#555]">在正文树打开「第N章」文件后，可对此章执行 审计 / 优化 / 重写；</span>
                {nextWriteChapter !== null && (
                  <button
                    type="button"
                    disabled={busy || quick?.status === 'running' || editorDirty || settleBusy}
                    onClick={() => void runQuick('write', nextWriteChapter)}
                    title={editorDirty ? '编辑器有未保存修改，请先保存 再写章' : '首写入口：按细纲/合同生成并落盘该章正文（正文不存在时可用）'}
                    className="flex items-center gap-1 h-7 px-2.5 rounded-lg border border-[#38bdf8]/35 text-[#7dd3fc] hover:bg-[#38bdf8]/10 disabled:opacity-40 text-[12px] transition-colors"
                  >
                    写第{nextWriteChapter}章
                    <span className="text-[10px] text-[#2e7f99]">首写</span>
                  </button>
                )}
              </div>
            )}

            {/* 首写/重写守卫提示（正文不存在或无法确认时显示；提示期间未发起任何请求） */}
            {chapterGuardMsg && (
              <div className="w-full bubble-max rounded-lg px-2.5 py-1.5 text-[12px] border border-amber-700/50 text-amber-300 bg-[#1a1408] flex items-start gap-2">
                <span className="flex-1 break-words leading-relaxed">{chapterGuardMsg}</span>
                <button
                  type="button"
                  onClick={() => setChapterGuardMsg(null)}
                  className="shrink-0 text-[#8a6a2a] hover:text-[#fbbf24] text-[13px] leading-none"
                  title="关闭提示"
                >
                  ×
                </button>
              </div>
            )}

            {/* 验收提交结果（常驻入口反馈；同步端点，不占 agent 任务槽）。结果绑定章号，切章自动隐藏 */}
            {settleOut && settleOut.chapter === activeChapterNum && (
              <div className={`w-full bubble-max rounded-lg px-2.5 py-1.5 text-[12px] border flex items-start gap-2 ${
                settleOut.ok ? 'border-emerald-700/50 text-emerald-300 bg-[#0e1a12]' : 'border-red-900/50 text-red-300 bg-[#1a1214]'
              }`}>
                <span className="flex-1 whitespace-pre-wrap break-words leading-relaxed">{settleOut.text}</span>
                <button
                  type="button"
                  onClick={() => setSettleOut(null)}
                  className="shrink-0 text-[#777] hover:text-[#ccc] text-[13px] leading-none"
                  title="关闭提示"
                >
                  ×
                </button>
              </div>
            )}

            {/* 重写等级选择（点击「重写第N章」展开） */}
            {rewriteDlg && activeChapter && rewriteDlg.chapter === activeChapter.num && (
              <div className="bubble-max border border-[#f59e0b]/30 rounded-xl px-3 py-2 bg-[#181510]">
                <div className="flex items-center justify-between mb-1.5">
                  <span className="text-[12px] text-[#fbbf24] font-medium">重写第{rewriteDlg.chapter}章 · 选择等级</span>
                  <button type="button" onClick={() => setRewriteDlg(null)} className="text-[#666] hover:text-[#aaa] text-[14px] leading-none">×</button>
                </div>
                <div className="flex flex-col gap-1">
                  {REWRITE_LEVELS.map((lv) => (
                    <button
                      key={lv.id}
                      type="button"
                      onClick={() => setRewriteDlg({ ...rewriteDlg, level: lv.id })}
                      className={`flex items-start gap-2 px-2.5 py-1.5 rounded-lg border text-left transition-colors ${
                        rewriteDlg.level === lv.id ? 'border-[#f59e0b]/60 bg-[#f59e0b]/10' : 'border-[#3a3a3a] hover:border-[#555]'
                      }`}
                    >
                      <span className={`shrink-0 w-6 h-6 flex items-center justify-center rounded-md text-[11px] font-bold ${
                        rewriteDlg.level === lv.id ? 'bg-[#f59e0b] text-black' : 'bg-[#2a2a2a] text-[#888]'
                      }`}>{lv.id}</span>
                      <span className="min-w-0">
                        <span className={`block text-[12px] font-medium ${rewriteDlg.level === lv.id ? 'text-[#fbbf24]' : 'text-[#bbb]'}`}>{lv.name}</span>
                        <span className="block text-[11px] text-[#777] leading-snug">{lv.desc}</span>
                      </span>
                    </button>
                  ))}
                </div>
                <div className="mt-2 flex items-center gap-2">
                  <input
                    value={rewriteDlg.note}
                    onChange={(e) => setRewriteDlg({ ...rewriteDlg, note: e.target.value.slice(0, 500) })}
                    placeholder="可选：执行要点（衔接 / 字数 / 重点段；L3 留空=按合同全量重写）"
                    className="flex-1 min-w-0 bg-transparent border border-[#3a3a3a] rounded-lg px-2.5 py-1.5 text-[12px] text-[#ccc] placeholder:text-[#555] outline-none focus:border-[#f59e0b]/40 transition-colors"
                  />
                  <button
                    type="button"
                    disabled={quick?.status === 'running' || editorDirty}
                    onClick={() => { void runQuick('rewrite', rewriteDlg.chapter, rewriteDlg.level, rewriteDlg.note); setRewriteDlg(null) }}
                    className="shrink-0 h-8 px-3 rounded-lg bg-[#f59e0b] text-black text-[12px] font-medium hover:bg-[#fbbf24] disabled:opacity-40 transition-colors"
                  >
                    确认重写
                  </button>
                </div>
              </div>
            )}

            {/* 润色意见输入（点击「优化第N章」展开；留空=自动诊断润色） */}
            {polishDlg && activeChapter && polishDlg.chapter === activeChapter.num && (
              <div className="bubble-max border border-[#34d399]/30 rounded-xl px-3 py-2 bg-[#101a15]">
                <div className="flex items-center justify-between mb-1.5">
                  <span className="text-[12px] text-[#6ee7b7] font-medium">润色第{polishDlg.chapter}章 · 润色意见</span>
                  <button type="button" onClick={() => setPolishDlg(null)} className="text-[#666] hover:text-[#aaa] text-[14px] leading-none">×</button>
                </div>
                <div className="flex items-center gap-2">
                  <input
                    value={polishDlg.note}
                    onChange={(e) => setPolishDlg({ ...polishDlg, note: e.target.value.slice(0, 500) })}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && !(quick?.status === 'running' || editorDirty)) {
                        void runQuick('polish', polishDlg.chapter, 'L3', polishDlg.note)
                        setPolishDlg(null)
                      }
                    }}
                    placeholder="可选：润色意见（视角 / 用词 / 去书面化…；留空=自动诊断润色），最高优先级执行"
                    className="flex-1 min-w-0 bg-transparent border border-[#3a3a3a] rounded-lg px-2.5 py-1.5 text-[12px] text-[#ccc] placeholder:text-[#555] outline-none focus:border-[#34d399]/40 transition-colors"
                  />
                  <button
                    type="button"
                    disabled={quick?.status === 'running' || editorDirty}
                    onClick={() => { void runQuick('polish', polishDlg.chapter, 'L3', polishDlg.note); setPolishDlg(null) }}
                    className="shrink-0 h-8 px-3 rounded-lg bg-[#34d399] text-black text-[12px] font-medium hover:bg-[#6ee7b7] disabled:opacity-40 transition-colors"
                  >
                    确认润色
                  </button>
                </div>
              </div>
            )}
          </div>
        )}

        <div ref={barRef} className="relative bg-[#1a1a1a] border border-[#3a3a3a] rounded-2xl transition-colors focus-within:border-[#f59e0b]/40">

          {/* 附件 chips：上传文件的文本已挂载，随下一条消息注入 */}
          {attachments.length > 0 && (
            <div className="flex flex-wrap gap-1.5 px-3 pt-2.5">
              {attachments.map((a, i) => (
                <span key={i} className="flex items-center gap-1 max-w-[180px] pl-2 pr-1 py-0.5 rounded-md border border-[#3a3a3a] bg-[#222] text-[12px] text-[#aaa]">
                  <span className="truncate" title={a.name}>{a.name}</span>
                  <button
                    type="button"
                    onClick={() => setAttachments((prev) => prev.filter((_, j) => j !== i))}
                    className="shrink-0 w-4 h-4 flex items-center justify-center rounded text-[#666] hover:text-red-300 transition-colors"
                    title="移除附件"
                  >
                    ×
                  </button>
                </span>
              ))}
            </div>
          )}

          <textarea
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder={bookId ? '和建书助手聊聊…（回车发送，Shift+回车换行）' : '说说你的新书灵感，我来帮你把书立起来…（回车发送，Shift+回车换行）'}
            disabled={busy}
            rows={1}
            onKeyDown={(e) => {
              // 回车=发送；输入法组词确认的回车（isComposing）不发送；Shift+回车=换行
              if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault()
                send()
              }
            }}
            className="w-full bg-transparent px-4 pt-3 pb-1 text-[14px] text-[#ccc] placeholder:text-[#555] outline-none resize-none overflow-y-auto leading-relaxed disabled:opacity-50"
          />

          {/* 工具行：左附件钮（带 ×N 计数）｜ 占位 ｜ 模型选择器 ▽ ｜ 发送 ⬆ */}
          <div className="flex items-center gap-1 px-2 pb-2">
            {/* 附件/工具入口 + 下拉（已精简：只保留 上传文件 / 更多工具） */}
            <div className="relative">
              <button
                type="button"
                onClick={() => setOpenMenu(openMenu === 'attach' || openMenu === 'more' ? null : 'attach')}
                className={`flex items-center gap-0.5 h-8 px-2 rounded-lg text-[#999] hover:text-[#ccc] hover:bg-[#2a2a2a] transition-colors ${
                  openMenu === 'attach' || openMenu === 'more' ? 'bg-[#2a2a2a] text-[#ccc]' : ''
                }`}
                title="附件与工具"
              >
                <PlusIcon />
                {attachments.length > 0 && <span className="text-[11px] text-[#888]">×{attachments.length}</span>}
              </button>
              {(openMenu === 'attach' || openMenu === 'more') && (
                <div className="absolute left-0 bottom-full mb-2 w-48 rounded-xl border border-[#3a3a3a] bg-[#1e1e1e] shadow-xl shadow-black/40 py-1 z-20">
                  {openMenu === 'attach' ? (
                    <>
                      <button
                        type="button"
                        onClick={() => { setOpenMenu(null); fileRef.current?.click() }}
                        className="w-full flex items-center gap-2 px-3 py-2 text-[13px] text-[#bbb] hover:bg-[#2a2a2a] transition-colors"
                      >
                        <FileIcon />
                        <span>上传文件</span>
                      </button>
                      <div className="mx-2 my-1 border-t border-[#2a2a2a]" />
                      <button
                        type="button"
                        onClick={() => setOpenMenu('more')}
                        className="w-full flex items-center gap-2 px-3 py-2 text-[13px] text-[#bbb] hover:bg-[#2a2a2a] transition-colors"
                      >
                        <DotsIcon />
                        <span className="flex-1 text-left">更多工具</span>
                        <span className="text-[#666]">›</span>
                      </button>
                    </>
                  ) : (
                    <>
                      <div className="px-3 py-1.5 text-[11px] text-[#666]">更多工具</div>
                      <button
                        type="button"
                        onClick={clearChat}
                        className="w-full flex items-center gap-2 px-3 py-2 text-[13px] text-[#bbb] hover:bg-[#2a2a2a] transition-colors"
                      >
                        <span className="flex-1 text-left">清空当前会话</span>
                      </button>
                      <button
                        type="button"
                        onClick={() => setOpenMenu('attach')}
                        className="w-full flex items-center gap-2 px-3 py-2 text-[13px] text-[#777] hover:bg-[#2a2a2a] transition-colors"
                      >
                        <span className="flex-1 text-left">‹ 返回</span>
                      </button>
                    </>
                  )}
                </div>
              )}
              <input
                ref={fileRef}
                type="file"
                multiple
                accept=".md,.markdown,.txt,.json"
                className="hidden"
                onChange={(e) => { void pickFiles(e.target.files); e.target.value = '' }}
              />
            </div>

            <div className="flex-1" />

            {/* 模型选择器：''=跟随全局默认（book-coach 链）；选中池模型则本轮直连该模型（均写服务端真源） */}
            <div className="relative">
              <button
                type="button"
                onClick={() => setOpenMenu(openMenu === 'model' ? null : 'model')}
                className={`flex items-center gap-1 h-8 px-2 rounded-lg text-[12px] text-[#999] hover:text-[#ccc] hover:bg-[#2a2a2a] transition-colors max-w-[160px] ${
                  openMenu === 'model' ? 'bg-[#2a2a2a] text-[#ccc]' : ''
                }`}
                title={selectedPool ? `模型：${selectedPool.model}` : `跟随全局默认（${defaultLabel}）`}
              >
                <span className="truncate">{modelLabel}</span>
                <ChevronIcon />
              </button>
              {openMenu === 'model' && (
                <div className="absolute right-0 bottom-full mb-2 w-60 rounded-xl border border-[#3a3a3a] bg-[#1e1e1e] shadow-xl shadow-black/40 py-1 z-20 max-h-64 overflow-y-auto">
                  <div className="px-3 py-1.5 text-[11px] text-[#666]">选择本轮对话模型</div>
                  <button
                    type="button"
                    onClick={() => pickModel('')}
                    className="w-full flex items-center gap-2 px-3 py-2 text-[13px] text-[#bbb] hover:bg-[#2a2a2a] transition-colors"
                  >
                    <span className="flex-1 text-left truncate">跟随全局默认（{defaultLabel}）</span>
                    {!poolId && <span className="text-[#f59e0b]"><CheckIcon /></span>}
                  </button>
                  {poolModels.length > 0 && <div className="mx-2 my-1 border-t border-[#2a2a2a]" />}
                  {poolModels.map((m) => (
                    <button
                      key={m.id}
                      type="button"
                      onClick={() => pickModel(m.id)}
                      className="w-full flex items-center gap-2 px-3 py-2 text-[13px] text-[#bbb] hover:bg-[#2a2a2a] transition-colors"
                      title={m.model}
                    >
                      <span className="flex-1 text-left truncate">{m.label || m.model}</span>
                      {poolId === m.id && <span className="text-[#f59e0b]"><CheckIcon /></span>}
                    </button>
                  ))}
                  {poolModels.length === 0 && (
                    <div className="px-3 py-2 text-[12px] text-[#666]">模型中心暂无模型池条目</div>
                  )}
                </div>
              )}
            </div>

            {/* 发送 ⬆ / 停止 ■ */}
            {busy ? (
              <button
                type="button"
                onClick={stop}
                className="shrink-0 w-8 h-8 rounded-full bg-[#7f1d1d] text-white flex items-center justify-center hover:bg-[#991b1b] transition-colors"
                title="停止生成"
              >
                <svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
                  <rect x="5" y="5" width="14" height="14" rx="2" />
                </svg>
              </button>
            ) : (
              <button
                type="submit"
                disabled={!input.trim() && attachments.length === 0}
                className="shrink-0 w-8 h-8 rounded-full bg-[#f59e0b] text-black font-medium flex items-center justify-center hover:bg-[#fbbf24] disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                title="发送"
              >
                <ArrowUpIcon />
              </button>
            )}
          </div>
        </div>
      </form>
    </div>
  )
}
