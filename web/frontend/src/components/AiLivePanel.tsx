import { useEffect, useRef, useState, type CSSProperties } from 'react'
import { api, type AgentTaskRecord, type AgentTaskStatus, type AgentTaskStage, type LiveCall, type LiveEvent, type LiveRun } from '../api'

/* ─────────────────────  AI 创作实况 / LIVE LOG  ─────────────────────
 * 琥珀金暗色主题。每环节独立圆角卡片，点击展开详情；新环节自动聚焦。
 * 120s 自动关闭；点击外部遮罩关闭。
 */

const PHASE_MAP: Record<string, string> = {
  queued: '排队中',
  prealign: '写前对齐（预补细纲）',
  backup: '备份旧稿',
  packet: '打包 Writing Packet',
  writer: '主笔写作',
  analyze: '质量分析 + 合同快照',
  plan: 'L3 再生产计划',
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
  post_write: '追踪提交 / 闸门校验',
  revise: '定点修订（只增不删）',
  revise_diff: '只增不删核对',
  rescan: 'AI味复扫',
  pronoun_gate: '代词性别机械闸',
  content_load: '加载上下文素材',
  plan_outline: '生成章节细纲',
  // 重写链补充
  pronoun_gate_final: '代词性别闸（终检）',
  pronoun_gate_block: '代词性别闸（阻断）',
  paragraph_120: '段落 120 字复检',
  dialogue_split: '对话拆段复检',
  author_requirements: '作者要求验收',
  paragraph_recheck: '段落二次复检',
  paragraph_fix: '拆段修复',
  causal_audit: '因果审计',
  tail_gate: '章尾闸门',
  quality_observe: '质量观察',
  finalize_integrity: '收尾完整性观测',
  pre_review_enforce: '审稿前 enforce 前移',
  // 润色链补充
  polish_effect_check: '润色效果复检',
  // 修订链补充
  redirect: '诉求改派判定',
  dry_run: 'dry-run 沙箱校验',
  rollback_on_cancel: '取消事务回滚',
}

/* ── 各 mode 的参考 stage 顺序（从后端 pipeline._finish 调用顺序反推）
 * 用于渲染"未执行"参考卡片。条件触发的 advisory stage 可能因开关关闭而不出现，
 * 以实际 live_stages 为准；这里只是给用户一个全貌参考。 ── */
const MODE_STAGE_TEMPLATES: Record<string, string[]> = {
  write: [
    'prealign', 'plan_outline', 'packet', 'content_load',
    'writer', 'normalize', 'pronoun_gate', 'wordcount',
    'delta_draft', 'delta_semantic', 'consistency', 'deslop',
    'pre_review_enforce', 'review_cycle', 'quality_observe',
    'finalize_integrity', 'post_write', 'causal_audit', 'tail_gate',
  ],
  rewrite: [
    'backup', 'prealign', 'packet', 'analyze', 'plan',
    'writer', 'pronoun_gate', 'contract_guard', 'normalize',
    'pronoun_gate_final', 'wordcount', 'paragraph_120', 'dialogue_split',
    'delta_draft', 'delta_semantic', 'consistency', 'deslop',
    'pre_review_enforce', 'author_requirements', 'review_cycle',
    'paragraph_recheck', 'post_write', 'rescan',
  ],
  polish: [
    'backup', 'analyze', 'rewrite_pass', 'deslop', 'normalize',
    'contract_guard', 'paragraph_recheck',
    'delta_draft', 'delta_semantic', 'consistency',
    'post_write', 'rescan',
  ],
  deslop: [
    'backup', 'polish', 'polish_effect_check', 'normalize',
    'delta_draft', 'delta_semantic', 'consistency',
    'post_write', 'rescan',
  ],
  revise: [
    'redirect', 'backup', 'revise', 'revise_diff', 'normalize',
    'delta_draft', 'delta_semantic', 'post_write', 'rescan',
  ],
}

/** 把 writer_retry1 / normalize_retry2 归一到主 stage 名，避免重试被算成新步骤 */
function normalizeStageName(name: string): string {
  const m = /^(writer|normalize|pronoun_gate)_retry\d+$/.exec(name)
  return m ? m[1] : name
}

function phaseLabel(name: string): string {
  if (PHASE_MAP[name]) return PHASE_MAP[name]
  const m = /^(writer|normalize|pronoun_gate)_retry(\d+)$/.exec(name)
  if (m) return `${PHASE_MAP[m[1]] ?? m[1]}（重试 ${m[2]}）`
  const am = /^audit_ch(\d+)$/.exec(name)
  if (am) return `审计第${am[1]}章`
  if (name === 'report') return '生成审计报告'
  return name
}

const MODE_LABEL: Record<string, string> = {
  rewrite: '重写', write: '写章', polish: '润色', deslop: '去AI味', revise: '修订', audit: '一致性审计',
}

type Stage = AgentTaskStage
/** 任务快照：字段契约见 api.ts AgentTaskRecord（后端只增字段，既有字段语义不变） */
type Snapshot = AgentTaskRecord

/* ── 琥珀金色板 ── */
const C = {
  panelBg: '#1c1408',
  panelBorder: '#b88020',
  title: '#e8a830',
  desc: '#9a7a40',
  cardBg: '#1a1208',
  cardBorder: '#8a6020',
  cardTitle: '#e8a830',
  cardSub: '#8a7040',
  arrow: '#c89030',
  badgeBorder: '#a07020',
  badgeText: '#e0a040',
  divider: '#3a2a10',
  detailBg: '#0a0805',
  detailText: '#b09060',
  streamBg: '#0a0805',
  streamText: '#c8a860',
}

function applyLiveEvents(prev: Record<string, LiveRun>, events: LiveEvent[]): Record<string, LiveRun> {
  const next: Record<string, LiveRun> = { ...prev }
  for (const ev of events) {
    if (ev.type === 'run' && ev.run) {
      const cur = next[ev.run.run_id]
      const stages = (ev.run.stages?.length ?? 0) >= (cur?.stages?.length ?? 0) ? ev.run.stages : cur?.stages ?? []
      const calls = (ev.run.calls?.length ?? 0) >= (cur?.calls?.length ?? 0) ? ev.run.calls : cur?.calls ?? []
      next[ev.run.run_id] = { ...(cur ?? ev.run), ...ev.run, stages, calls }
    } else if (ev.type === 'stage' && ev.stage && ev.run_id) {
      const r = next[ev.run_id]
      if (!r) continue
      const stages = [...(r.stages ?? [])]
      const i = stages.findIndex((st) => st.id === ev.stage!.id)
      if (i >= 0) stages[i] = { ...stages[i], ...ev.stage }
      else stages.push(ev.stage)
      next[ev.run_id] = { ...r, stages }
    } else if (ev.type === 'call' && ev.call) {
      const rid = ev.call.run_id ?? ev.run_id
      const r = rid ? next[rid] : undefined
      if (!r) continue
      const calls = [...(r.calls ?? [])]
      const i = calls.findIndex((c) => c.id === ev.call!.id)
      if (i >= 0) calls[i] = { ...calls[i], ...ev.call }
      else calls.push(ev.call)
      next[r.run_id] = { ...r, calls, call_count: calls.length }
    } else if (ev.type === 'call_delta' && ev.call_id) {
      const target = (ev.run_id && next[ev.run_id])
        ? ev.run_id
        : Object.keys(next).find((k) => (next[k].calls ?? []).some((c) => c.id === ev.call_id))
      if (!target) continue
      const r = next[target]
      next[target] = {
        ...r,
        calls: (r.calls ?? []).map((c) => (c.id === ev.call_id
          ? { ...c, status: 'running' as const, chars: ev.chars ?? c.chars,
              reasoning_chars: ev.reasoning_chars ?? c.reasoning_chars,
              output: ev.preview ?? c.output }
          : c)),
      }
    }
  }
  return next
}

function callsForStage(calls: LiveCall[] | undefined, stageName: string): LiveCall[] {
  if (!calls || !calls.length) return []
  return calls.filter((c) => (c.stage_name ?? '') === stageName)
}

function liveStageToTaskStage(st: LiveRun['stages'][number]): AgentTaskStage & { active?: boolean } {
  return {
    name: st.name,
    ok: st.status === 'done' || st.status === 'running' ? st.ok !== false : false,
    detail: st.detail ?? '',
    output: st.output,
    ts: st.ended_at ?? st.started_at,
  }
}

function formatDuration(ms: number | null | undefined): string {
  if (ms == null || ms < 0) return '—'
  if (ms < 1000) return `${Math.round(ms)} ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} 秒`
  const m = Math.floor(ms / 60_000)
  const s = Math.round((ms % 60_000) / 1000)
  return `${m} 分 ${s} 秒`
}

/** 顶部总状态徽章 */
function TopStatusBadge({ status }: { status: AgentTaskStatus | 'idle' }) {
  const map: Record<string, { t: string }> = {
    idle: { t: '等待生成' },
    running: { t: '进行中' },
    interrupted: { t: '进程中断' },
    done: { t: '已完成' },
    error: { t: '生成失败' },
    cancelled: { t: '已取消' },
  }
  const v = map[status] ?? map.idle
  return (
    <span
      className="px-2.5 py-0.5 rounded text-[12px] font-medium whitespace-nowrap"
      style={{ color: C.title, background: `${C.badgeBorder}22`, border: `1px solid ${C.badgeBorder}` }}
    >
      {v.t}
    </span>
  )
}

/** 卡片右侧小徽章（统一金色） */
function CardBadge({ state }: { state: 'done' | 'failed' | 'active' }) {
  const map = { done: '已完成', failed: '执行失败', active: '执行中' } as const
  return (
    <span
      className="px-2 py-0.5 rounded-full text-[11px] font-medium whitespace-nowrap inline-block"
      style={{ color: C.badgeText, background: 'transparent', border: `1px solid ${C.badgeBorder}` }}
    >
      {state === 'active' && (
        <span className="inline-block w-1.5 h-1.5 rounded-full bg-current animate-pulse mr-1 align-middle" />
      )}
      {map[state]}
    </span>
  )
}

/** 单个环节卡片（独立圆角矩形） */
function CallItem({ call }: { call: LiveCall }) {
  const [open, setOpen] = useState(false)
  const running = call.status === 'running'
  const failed = call.status === 'error'
  const durMs = call.duration_ms ?? (running && call.started_at ? (Date.now() / 1000 - call.started_at) * 1000 : null)
  const label = call.agent ? call.agent : '模型调用'
  return (
    <div className="rounded border" style={{ borderColor: C.divider, background: 'rgba(0,0,0,0.22)' }}>
      <button
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center gap-2 px-2 py-1.5 text-left"
      >
        <span
          className={`inline-block w-1.5 h-1.5 rounded-full shrink-0 ${running ? 'animate-pulse' : ''}`}
          style={{ background: failed ? '#e87040' : running ? C.badgeText : '#63b06a' }}
        />
        <span className="text-[11px] font-semibold truncate" style={{ color: C.cardTitle }} title={label}>
          {label}
        </span>
        <span className="text-[10px] font-mono truncate max-w-[40%]" style={{ color: C.cardSub }} title={call.model}>
          {call.model}
        </span>
        {call.provider && (
          <span className="text-[10px] font-mono shrink-0" style={{ color: C.cardSub }}>· {call.provider}</span>
        )}
        {call.stream && <span className="text-[9px] px-1 rounded shrink-0" style={{ color: C.badgeText, border: `1px solid ${C.badgeBorder}` }}>流式</span>}
        <div className="flex-1" />
        {(call.chars ?? 0) > 0 && (
          <span className="text-[10px] font-mono shrink-0" style={{ color: C.cardSub }}>{call.chars} 字</span>
        )}
        {(call.reasoning_chars ?? 0) > 0 && (
          <span className="text-[10px] font-mono shrink-0" style={{ color: C.cardSub }}>思考 {call.reasoning_chars} 字</span>
        )}
        <span className="text-[10px] font-mono shrink-0" style={{ color: C.cardSub }}>{formatDuration(durMs)}</span>
        {failed && <span className="text-[10px] shrink-0" style={{ color: '#e87040' }}>失败</span>}
      </button>
      {open && (
        <div className="px-2 pb-2">
          {call.prompt && (
            <>
              <div className="text-[10px] font-semibold mt-1 mb-1" style={{ color: C.badgeText }}>
                ── 提示词 · {call.prompt_chars ?? call.prompt.length} 字 ──
              </div>
              <pre
                className="text-[11px] leading-relaxed whitespace-pre-wrap break-words font-mono max-h-48 overflow-auto rounded p-2"
                style={{ margin: 0, color: C.detailText, background: C.streamBg }}
              >
                {call.prompt}
              </pre>
            </>
          )}
          {!!call.reasoning && (
            <>
              <div className="text-[10px] font-semibold mt-2 mb-1" style={{ color: C.badgeText }}>
                ── 思考过程 ──
              </div>
              <pre
                className="text-[11px] leading-relaxed whitespace-pre-wrap break-words font-mono max-h-40 overflow-auto rounded p-2"
                style={{ margin: 0, color: C.detailText, background: C.streamBg }}
              >
                {call.reasoning.slice(-3000)}
              </pre>
            </>
          )}
          {!!call.output && (
            <>
              <div className="text-[10px] font-semibold mt-2 mb-1" style={{ color: C.badgeText }}>
                ── {running ? '实时输出' : '产物'} · {call.output.length} 字 ──
              </div>
              <pre
                className="text-[11px] leading-relaxed whitespace-pre-wrap break-words font-mono max-h-56 overflow-auto rounded p-2"
                style={{ margin: 0, color: C.streamText, background: C.streamBg }}
              >
                {call.output.slice(-6000)}
              </pre>
            </>
          )}
          {call.tokens && (
            <div className="text-[10px] font-mono mt-1" style={{ color: C.cardSub }}>
              tokens：prompt {call.tokens.prompt ?? '—'} / completion {call.tokens.completion ?? '—'}
            </div>
          )}
          {call.error && (
            <div className="text-[11px] mt-1 break-all" style={{ color: '#e87040' }}>错误：{call.error}</div>
          )}
        </div>
      )}
    </div>
  )
}


function StageCard({
  stage,
  durationMs,
  isActive,
  defaultOpen,
  streamContent,
  streamCharCount,
  calls,
  title,
}: {
  stage: Stage & { active?: boolean }
  durationMs: number | null
  isActive?: boolean
  defaultOpen?: boolean
  streamContent?: string
  streamCharCount?: number
  calls?: LiveCall[]
  title?: string
}) {
  const [open, setOpen] = useState<boolean>(!!defaultOpen)
  const failed = !stage.ok && !isActive

  return (
    <div
      className="rounded-lg border overflow-hidden"
      style={{
        background: C.cardBg,
        borderColor: C.cardBorder,
        borderWidth: 1,
      }}
    >
      {/* 卡片头（可点击展开） */}
      <button
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center gap-2 px-3 py-2.5 text-left hover:bg-black/20 transition-colors"
      >
        {/* 展开箭头 */}
        <svg
          className="h-4 w-4 shrink-0 transition-transform"
          style={{ color: C.arrow, transform: open ? 'rotate(90deg)' : 'none' }}
          viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
          strokeLinecap="round" strokeLinejoin="round"
        >
          <path d="m9 18 6-6-6-6" />
        </svg>

        {/* 中间：标题 + 厂商/模型行 */}
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5 flex-wrap">
            <span className="text-[13px] font-semibold leading-tight" style={{ color: C.cardTitle }}>
              {title ?? phaseLabel(stage.name)}
            </span>
            <span className="text-[11px] font-mono truncate" style={{ color: C.cardSub }} title={stage.name}>
              · {stage.name}
            </span>
          </div>
          {stage.detail && !open && (
            <div className="text-[11px] mt-0.5 truncate leading-snug" style={{ color: C.cardSub }}>
              {stage.detail}
            </div>
          )}
        </div>

        {/* 右侧：耗时 + 状态徽章 */}
        <div className="flex items-center gap-3 shrink-0">
          {!!calls?.length && (
            <span className="text-[11px] font-mono" style={{ color: C.badgeText }}>
              {calls.length} 次调用
            </span>
          )}
          <span className="text-[12px] font-mono" style={{ color: C.cardSub }}>
            耗时：{formatDuration(durationMs)}
          </span>
          <CardBadge state={isActive ? 'active' : failed ? 'failed' : 'done'} />
        </div>
      </button>

      {/* 展开详情 */}
      {open && (
        <div className="px-3 py-2 pl-9" style={{ background: C.detailBg, borderTop: `1px solid ${C.divider}` }}>
          {stage.detail && (
            <pre
              className="text-[11px] leading-relaxed whitespace-pre-wrap break-words font-mono mt-1"
              style={{ margin: 0, color: C.detailText }}
            >
              {stage.detail}
            </pre>
          )}
          {streamContent && (
            <>
              <div className="text-[10px] font-semibold mt-2 mb-1" style={{ color: C.badgeText }}>
                ── 实时输出 · {streamCharCount ?? streamContent.length} 字 ──
              </div>
              <pre
                className="text-[11px] leading-relaxed whitespace-pre-wrap break-words font-mono max-h-64 overflow-auto rounded p-2"
                style={{ margin: 0, color: C.streamText, background: C.streamBg }}
              >
                {streamContent.slice(-6000)}
              </pre>
            </>
          )}
          {stage.output && stage.output !== stage.detail && (
            <>
              <div className="text-[10px] font-semibold mt-2 mb-1" style={{ color: C.badgeText }}>
                ── 完整产物 · {stage.output.length} 字 ──
              </div>
              <pre
                className="text-[11px] leading-relaxed whitespace-pre-wrap break-words font-mono max-h-64 overflow-auto rounded p-2"
                style={{ margin: 0, color: C.streamText, background: C.streamBg }}
              >
                {stage.output}
              </pre>
            </>
          )}
          {!!calls?.length && (
            <>
              <div className="text-[10px] font-semibold mt-2 mb-1" style={{ color: C.badgeText }}>
                ── 模型调用 · {calls.length} 次（点开看提示词/思考/产物）──
              </div>
              <div className="space-y-1">
                {calls.map((c) => <CallItem key={c.id} call={c} />)}
              </div>
            </>
          )}
        </div>
      )}
    </div>
  )
}

function FlowRunBlock({ run }: { run: LiveRun }) {
  const [open, setOpen] = useState(true)
  const running = run.status === 'running'
  const stages = run.stages ?? []
  const calls = run.calls ?? []
  const knownNames = new Set(stages.map((st) => st.name))
  const looseCalls = calls.filter((c) => !c.stage_name || !knownNames.has(c.stage_name))
  const started = new Date(run.started_at * 1000).toLocaleTimeString('zh-CN', {
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
  return (
    <div
      className="rounded-lg border overflow-hidden"
      style={{ background: 'rgba(255,255,255,0.02)', borderColor: C.cardBorder, borderWidth: 1 }}
    >
      <button
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-black/20 transition-colors"
      >
        <span
          className={`inline-block w-1.5 h-1.5 rounded-full shrink-0 ${running ? 'animate-pulse' : ''}`}
          style={{ background: run.status === 'error' ? '#e87040' : running ? C.badgeText : '#63b06a' }}
        />
        <span className="text-[12px] font-semibold truncate" style={{ color: C.cardTitle }} title={run.label}>
          {run.label}
        </span>
        <span className="text-[10px] font-mono shrink-0" style={{ color: C.cardSub }}>{started}</span>
        <div className="flex-1" />
        {!!calls.length && (
          <span className="text-[10px] font-mono shrink-0" style={{ color: C.badgeText }}>{calls.length} 次调用</span>
        )}
        <span
          className="px-1.5 py-0.5 rounded text-[10px] shrink-0"
          style={{ color: running ? C.badgeText : C.cardSub, border: `1px solid ${running ? C.badgeBorder : C.divider}` }}
        >
          {running ? '执行中' : run.status === 'done' ? '完成' : run.status === 'error' ? '失败' : '已取消'}
        </span>
      </button>
      {open && (
        <div
          className="px-2 pb-2 pt-1.5 space-y-1.5"
          style={{ borderTop: `1px solid ${C.divider}`, background: 'rgba(0,0,0,0.14)' }}
        >
          {stages.length === 0 && running && looseCalls.length === 0 && (
            <div className="px-1 py-1 text-[11px]" style={{ color: C.cardSub }}>执行中 · 等待第一个环节…</div>
          )}
          {stages.map((st) => (
            <StageCard
              key={st.id}
              stage={liveStageToTaskStage(st)}
              durationMs={st.duration_ms ?? null}
              isActive={st.status === 'running'}
              defaultOpen={st.status !== 'running'}
              calls={callsForStage(calls, st.name)}
              title={st.label || st.name}
            />
          ))}
          {!!looseCalls.length && (
            <>
              <div className="text-[10px] font-semibold px-1 pt-1" style={{ color: C.badgeText }}>
                ── 本流程的模型调用 · {looseCalls.length} 次 ──
              </div>
              <div className="space-y-1">
                {looseCalls.map((c) => <CallItem key={c.id} call={c} />)}
              </div>
            </>
          )}
          {run.error && (
            <div className="px-1 text-[11px] break-all" style={{ color: '#e87040' }}>错误：{run.error}</div>
          )}
        </div>
      )}
    </div>
  )
}


export default function AiLivePanel({
  taskId,
  mode,
  open,
  onOpenChange,
  onRunningChange,
  currentBookId,
}: {
  taskId: string | null
  mode?: string
  open: boolean
  onOpenChange: (open: boolean) => void
  onRunningChange?: (running: boolean) => void
  /** 书架当前书：AI 实况是全局最近任务，不随书架切换，需显式标注归属避免误判 */
  currentBookId?: string | null
}) {
  const [snap, setSnap] = useState<Snapshot | null>(null)
  const [compact, setCompact] = useState(false)
  const [pos, setPos] = useState<{ x: number; y: number } | null>(null)
  const [following, setFollowing] = useState(true)
  // 当前跟踪的 task_id：props 传入初值；「续跑本任务」成功后切到后端返回的新 id，
  // 继续沿用同一轮询节奏（不做断点续传，见 resume_scope='chapter'）。
  const [activeTaskId, setActiveTaskId] = useState<string | null>(taskId)
  const [resuming, setResuming] = useState(false)
  const [resumeError, setResumeError] = useState<string | null>(null)
  // ── AI 实况统一流（2026-11-08）：所有流程的 run/stage/call 增量（与任务面板并存）──
  const [feedRuns, setFeedRuns] = useState<Record<string, LiveRun>>({})
  const [feedRunning, setFeedRunning] = useState(false)
  // 实况焦点：面板主体只渲染「一个流程」，新流程启动时自动切过去（不把别的流程塞进当前任务里）
  const [focusIdState, setFocusIdState] = useState<string | null>(null)
  const feedRunsRef = useRef<Record<string, LiveRun>>({})
  const feedSeqRef = useRef(0)
  const feedRunningRef = useRef(false)
  const taskRunningRef = useRef(false)
  const lastRunningRef = useRef(false)

  // 顶部图标/脉冲 = 当前任务在跑 或 任一其他流程在跑（统一口径，避免「任务结束就灭灯」）
  const syncRunning = () => {
    const v = taskRunningRef.current || feedRunningRef.current
    if (v !== lastRunningRef.current) {
      lastRunningRef.current = v
      onRunningChange?.(v)
    }
  }
  const streamWrapRef = useRef<HTMLDivElement | null>(null)
  const listWrapRef = useRef<HTMLDivElement | null>(null)
  const clearRef = useRef<{ stages: number; stream: number } | null>(null)
  const followingRef = useRef(true)
  followingRef.current = following
  // phase_detail 变化追踪（仅在文本变化时刷新基线，超过 5 分钟无变化提示「长时间无进展」）
  const phaseDetailRef = useRef<{ value: string; since: number }>({ value: '', since: Date.now() })

  // 外部换任务（父组件给新 taskId）时同步本地跟踪目标；续跑后的本地新 id 不会被覆盖
  useEffect(() => { setActiveTaskId(taskId) }, [taskId])

  // 轮询任务时间线：面板关闭时保留记录不清空；新一轮任务（taskId 变）才重置
  const lastTaskRef = useRef<string | null>(null)
  const wasRunningRef = useRef(false)
  useEffect(() => {
    if (!activeTaskId) { setSnap(null); lastTaskRef.current = null; return }
    if (lastTaskRef.current !== activeTaskId) {
      // 新一轮任务：清空旧记录，重置前台基线
      setSnap(null)
      clearRef.current = null
      setResumeError(null)
      lastTaskRef.current = activeTaskId
      wasRunningRef.current = true
      taskRunningRef.current = true
      syncRunning()
    }
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined
    if (open) setFollowing(true)
    const tick = async () => {
      if (cancelled) return
      try {
        const st = await api.getAgentTask(activeTaskId)
        if (cancelled) return
        if (open) {
          setSnap(st)
          // 轮询成功即持久化 taskId，刷新后可恢复
          try { localStorage.setItem('nww_live_task', JSON.stringify({ id: activeTaskId, mode: mode ?? '' })) } catch {}
        }
        const isRunning = st.status === 'running'
        if (isRunning !== wasRunningRef.current) {
          wasRunningRef.current = isRunning
          taskRunningRef.current = isRunning
          syncRunning()
        }
        if (isRunning) {
          timer = setTimeout(tick, open ? (st.stream_text ? 400 : 800) : 2000)
        }
      } catch {
        if (!cancelled) timer = setTimeout(tick, open ? 1500 : 4000)
      }
    }
    tick()
    return () => { cancelled = true; if (timer) clearTimeout(timer) }
  }, [open, activeTaskId])

  // ── 统一实况轮询（与任务轮询并行）：since=0 快照，之后按 seq 取增量 ──
  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined
    const tick = async () => {
      if (cancelled) return
      try {
        const since = feedSeqRef.current
        let res = await api.getLiveFeed(since)
        if (cancelled) return
        if (!res.reset && res.seq < since) {
          // 后端重启导致 seq 回退：重建快照，避免增量断档
          res = await api.getLiveFeed(0)
          if (cancelled) return
        }
        let next: Record<string, LiveRun> = {}
        if (res.reset) {
          for (const r of res.runs ?? []) next[r.run_id] = r
        } else {
          next = applyLiveEvents(feedRunsRef.current, res.events ?? [])
        }
        const list = Object.values(next)
          .sort((a, b) => (b.started_at ?? 0) - (a.started_at ?? 0))
          .slice(0, 12)
        next = Object.fromEntries(list.map((r) => [r.run_id, r]))
        feedRunsRef.current = next
        feedSeqRef.current = res.seq
        setFeedRuns(next)
        const running = list.some((r) => r.status === 'running')
        feedRunningRef.current = running
        setFeedRunning(running)
        syncRunning()
      } catch {
        /* 实况接口异常不影响任务面板 */
      }
      timer = setTimeout(tick, open ? 700 : 2500)
    }
    tick()
    return () => { cancelled = true; if (timer) clearTimeout(timer) }
  }, [open])

  const feedList = Object.values(feedRuns).sort((a, b) => (b.started_at ?? 0) - (a.started_at ?? 0))
  const runningFeedRun = feedList.find((r) => r.status === 'running')
  const latestFeedRun = feedList[0]
  const activeFeedRun = activeTaskId ? feedRuns[activeTaskId] : undefined

  // 有新流程开始跑：解除手动锁定，自动切到它（保证「现在是哪个流程」永远指向真正在跑的）
  const topRunningIdRef = useRef<string | null>(null)
  useEffect(() => {
    const id = runningFeedRun?.run_id ?? null
    if (id && id !== topRunningIdRef.current) {
      topRunningIdRef.current = id
      setFocusIdState(null)
    }
    if (!id) topRunningIdRef.current = null
  }, [runningFeedRun?.run_id])

  // 流式文本跟随
  useEffect(() => {
    if (!followingRef.current || !streamWrapRef.current) return
    streamWrapRef.current.scrollTop = streamWrapRef.current.scrollHeight
  }, [snap?.stream_text])

  // 焦点规则：有流程在跑 → 跟最新的那个（新流程启动会自动接管面板）；
  // 没有在跑 → 跟最近一次执行；用户点切换条可手动锁定某个流程。
  const autoFocusId = runningFeedRun?.run_id ?? latestFeedRun?.run_id ?? activeTaskId ?? null
  const focusId = focusIdState === activeTaskId && activeTaskId
    ? activeTaskId
    : (focusIdState && feedRuns[focusIdState] ? focusIdState : autoFocusId)
  const focusRun = focusId ? feedRuns[focusId] : undefined
  const focusIsTask = !!focusId && !!activeTaskId && focusId === activeTaskId
  const knownStageNames = new Set((activeFeedRun?.stages ?? []).map((st) => st.name))
  const looseTaskCalls = (activeFeedRun?.calls ?? []).filter((c) => !c.stage_name || !knownStageNames.has(c.stage_name))

  const stages = snap?.live_stages ?? []
  const base = clearRef.current
  const visibleStages = base ? stages.slice(base.stages) : stages
  const streamText = snap?.stream_text ?? ''
  const visibleStream = base ? streamText.slice(base.stream) : streamText
  // status="done" 但 result.ok=false = pipeline 走完了但某环节失败，按失败展示
  const rawStatus: AgentTaskStatus | 'idle' = snap?.status ?? 'idle'
  const pipelineFailed = rawStatus === 'done' && (snap?.ok === false || snap?.result?.ok === false)
  const status: AgentTaskStatus | 'idle' = pipelineFailed ? 'error' : rawStatus

  // 120s 自动关闭：仅在任务终态（done/error/cancelled）后才倒计时；
  // running 与 interrupted（等待作者决定是否续跑）都不自动关
  useEffect(() => {
    if (!open) return
    if (status === 'running' || status === 'interrupted' || feedRunning) return
    const t = setTimeout(() => onOpenChange(false), 120_000)
    return () => clearTimeout(t)
  }, [open, status, feedRunning, onOpenChange])

  const currentPhase = status === 'running' ? snap?.phase : undefined
  const lastCompletedName = visibleStages.length > 0 ? visibleStages[visibleStages.length - 1].name : null
  const activeStageName = currentPhase && currentPhase !== lastCompletedName ? currentPhase : null
  const activeStageDetail = status === 'running' ? snap?.phase_detail : undefined

  /* ── 统计：基于 mode 参考模板算 总数/完成/失败/进行中/未执行 ── */
  const template = MODE_STAGE_TEMPLATES[mode ?? ''] ?? []
  const occurredNames = new Set(visibleStages.map((s) => normalizeStageName(s.name)))
  const activeNorm = activeStageName ? normalizeStageName(activeStageName) : null
  if (activeNorm) occurredNames.add(activeNorm)
  const doneCount = visibleStages.filter((s) => s.ok).length
  const failedCount = visibleStages.filter((s) => !s.ok).length
  const runningCount = activeStageName ? 1 : 0
  // 找最后一个已发生 stage 在模板里的位置；未执行 = 该位置之后且未发生的 stage
  let lastOccurredIdx = -1
  template.forEach((n, i) => { if (occurredNames.has(n)) lastOccurredIdx = i })
  const pendingStages = template
    .map((n, i) => ({ name: n, idx: i }))
    .filter(({ name, idx }) => idx > lastOccurredIdx && !occurredNames.has(name))
    .map(({ name }) => name)
  const pendingCount = pendingStages.length
  const totalCount = template.length

  // 如果任务在跑但没有进行中卡片（后端只在 stage 完成后才推送），
  // 最后一个 stage 完成超过 3 秒后显示"正在处理下一步…"脉冲提示
  const lastStageTs = visibleStages.length > 0 ? (visibleStages[visibleStages.length - 1].ts ?? null) : null
  const [now, setNow] = useState(Date.now() / 1000)
  useEffect(() => {
    if (status !== 'running') return
    const t = setInterval(() => setNow(Date.now() / 1000), 1000)
    return () => clearInterval(t)
  }, [status])
  const waitingNext = status === 'running' && !activeStageName && !visibleStream && lastStageTs !== null && (now - lastStageTs) > 10

  // phase_detail 超过 5 分钟未变化：只做提示，绝不自动取消或重跑（产品硬约束）
  useEffect(() => {
    const v = snap?.phase_detail ?? ''
    if (phaseDetailRef.current.value !== v) {
      phaseDetailRef.current = { value: v, since: Date.now() }
    }
  }, [snap?.phase_detail])
  const stalled = status === 'running' && !!snap?.phase_detail && now * 1000 - phaseDetailRef.current.since > 5 * 60_000

  // 僵尸判定（D7）：process_restarted = 进程重启导致的进程中断，不是「长时间无更新」，
  // 不得显示僵尸文案；thread_lost_timeout 或原因缺失才是真僵尸。
  // 查询侧把 running 降级为 interrupted 之前（running 且 stale=true）也按僵尸处理。
  const isZombie = snap?.stale === true && snap?.interrupt_reason !== 'process_restarted'

  // 续跑本任务：调用章级续跑端点，成功后把跟踪 id 切到新 task_id，沿用同一轮询节奏
  const onResume = async () => {
    const tid = activeTaskId
    if (!tid || resuming) return
    setResuming(true)
    setResumeError(null)
    try {
      const r = await api.resumeAgentTask(tid)
      // 旧记录已被后端置为 cancelled；这里直接跟踪新任务（mode/book/chapter 与旧任务同源）
      setActiveTaskId(r.task_id)
      setSnap(null)
      clearRef.current = null
      setFollowing(true)
      wasRunningRef.current = true
      taskRunningRef.current = true
      syncRunning()
    } catch (e) {
      // 409/404 等错误文案原样展示（如「该任务不可续跑」「同章已有在途操作」）
      setResumeError(e instanceof Error ? e.message : String(e))
    } finally {
      setResuming(false)
    }
  }

  // 自动滚动到最新卡片
  useEffect(() => {
    if (!followingRef.current || !listWrapRef.current) return
    listWrapRef.current.scrollTop = listWrapRef.current.scrollHeight
  }, [visibleStages.length, activeStageName])

  // 拖拽
  const dragState = useRef<{ dx: number; dy: number } | null>(null)
  const onHeaderPointerDown = (e: React.PointerEvent) => {
    const rect = (e.currentTarget.parentElement as HTMLElement).getBoundingClientRect()
    dragState.current = { dx: e.clientX - rect.left, dy: e.clientY - rect.top }
    ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
  }
  const onHeaderPointerMove = (e: React.PointerEvent) => {
    if (!dragState.current) return
    const x = Math.max(8, Math.min(window.innerWidth - 60, e.clientX - dragState.current.dx))
    const y = Math.max(8, Math.min(window.innerHeight - 40, e.clientY - dragState.current.dy))
    setPos({ x, y })
  }
  const onHeaderPointerUp = (e: React.PointerEvent) => {
    dragState.current = null
    try { (e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId) } catch { /* noop */ }
  }

  if (!open) return null

  const panelStyle: CSSProperties = pos
      ? { left: pos.x, top: pos.y, width: 'min(560px, 94vw)', maxHeight: 'min(80vh, 980px)' }
      : { left: 72, top: 64, width: 'min(560px, 94vw)', maxHeight: 'min(80vh, 980px)' }

  const taskStart = snap?.started_at ?? null
  /* 任务标题派生（只读取任务记录，不推断）：书名 + 章号/审计范围 + 任务类型。
     连接中（snap 未到）用 props.mode 兜底，避免标题空白。 */
  const taskMode = snap?.mode ?? mode ?? ''
  const taskAction = MODE_LABEL[taskMode] ?? taskMode
  const taskBook = snap?.book_id ?? null
  const taskRange = snap?.range ?? null
  const taskScope = snap?.chapter_id != null
    ? `第 ${snap.chapter_id} 章`
    : taskRange && taskRange.length >= 2
      ? `第 ${taskRange[0]}–${taskRange[taskRange.length - 1]} 章`
      : ''
  const taskNote = (snap?.note ?? '').trim()
  const taskNoteBrief = taskNote.length > 90 ? `${taskNote.slice(0, 90)}…` : taskNote
  const taskIdShort = (snap?.task_id ?? activeTaskId ?? '').slice(0, 12)
  const startedText = snap?.started_at
    ? new Date(snap.started_at * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
    : null
  const durations: Array<number | null> = visibleStages.map((s, i) => {
    if (i === 0) {
      // 第一个 stage：从任务启动到第一个 stage 完成
      if (taskStart && s.ts) return (s.ts - taskStart) * 1000
      return null
    }
    const prevTs = visibleStages[i - 1]?.ts
    if (!prevTs || !s.ts) return null
    return (s.ts - prevTs) * 1000
  })

  // 切换条：任务 + 最近流程（一次只渲染一个，点哪个看哪个）
  const switchItems: Array<{ id: string; label: string; status: string }> = []
  if (activeTaskId && !feedRuns[activeTaskId]) {
    switchItems.push({
      id: activeTaskId,
      label: taskAction ? `${taskBook ?? '任务'} · ${taskAction}` : `任务 #${taskIdShort ?? ''}`,
      status,
    })
  }
  for (const r of feedList.slice(0, 8)) switchItems.push({ id: r.run_id, label: r.label, status: r.status })

  return (
    <>
      <div
          className="fixed inset-0 z-30"
          style={{ background: 'rgba(0,0,0,0.3)' }}
          onClick={() => onOpenChange(false)}
      />
      <div
        className="fixed z-40 flex flex-col rounded-lg border overflow-hidden"
        style={{
          ...panelStyle,
          background: C.panelBg,
          borderColor: C.panelBorder,
          borderWidth: 1,
          boxShadow: '0 25px 50px -12px rgba(0,0,0,0.6)',
          color: C.desc,
        }}
      >
        {/* 顶栏 */}
        <div
          onPointerDown={onHeaderPointerDown}
          onPointerMove={onHeaderPointerMove}
          onPointerUp={onHeaderPointerUp}
          className="shrink-0 flex items-center gap-2 px-4 py-3 cursor-move select-none touch-none"
          style={{ borderBottom: `1px solid ${C.divider}` }}
        >
          <span className="text-[15px] font-bold" style={{ color: C.title }}>AI 创作实况</span>
          <span className="text-[12px] font-medium" style={{ color: C.desc }}>LIVE LOG</span>
          <div className="flex-1" />
          <TopStatusBadge status={focusIsTask ? status : ((focusRun?.status as AgentTaskStatus | undefined) ?? status)} />
          <button
            onClick={() => setCompact((c) => !c)}
            className="px-2 py-0.5 rounded text-[12px] border"
            style={{ color: C.badgeText, borderColor: C.badgeBorder }}
          >
            {compact ? '详细' : '简略'}
          </button>
          <button
            onClick={() => { clearRef.current = { stages: stages.length, stream: streamText.length }; setFollowing(true) }}
            className="px-2 py-0.5 rounded text-[12px] border"
            style={{ color: C.badgeText, borderColor: C.badgeBorder }}
          >
            清空前台
          </button>
        </div>

        {/* 当前任务标题（2026-11-07）：一眼看清「哪一本书 · 哪一章 · 什么任务」。
            本面板跟踪全站最近一个任务、不随书架换书切换，故必须用标题级信息显式标出归属，
            否则会出现「点了第1章却看到第2章」的误判。数据全部来自任务记录，不做本地推断。 */}
        <div className="shrink-0 px-4 py-2.5 border-b" style={{ borderColor: C.divider, background: '#241a0a' }}>
          <div className="flex items-center gap-2 flex-wrap mb-1">
            <span className="px-1.5 py-0.5 rounded text-[10px] tracking-wider" style={{ color: C.desc, border: `1px solid ${C.divider}` }}>
              {focusIsTask ? '当前任务' : '当前流程'}
            </span>
            {focusIsTask && taskIdShort && <span className="text-[10px] font-mono" style={{ color: C.cardSub }}>#{taskIdShort}</span>}
            {focusIsTask && startedText && <span className="text-[10px]" style={{ color: C.cardSub }}>· {startedText} 发起</span>}
            {!focusIsTask && focusRun && (
              <span className="text-[10px]" style={{ color: C.cardSub }}>
                · {new Date(focusRun.started_at * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })} 发起
                · {focusRun.calls?.length ?? 0} 次模型调用
              </span>
            )}
          </div>
          <div className="text-[14px] font-bold leading-6 break-words" style={{ color: C.title }}>
            {!focusIsTask
              ? <>{focusRun?.label ?? '暂无进行中的流程'}</>
              : (!snap
                ? <>{taskAction ? `${taskAction} · ` : ''}正在连接任务…</>
                : <>
                  {taskBook ? `《${taskBook}》` : '未指定书籍'}
                  {taskScope && <> · {taskScope}</>}
                  {taskAction && <> · {taskAction}</>}
                </>)}
          </div>
          {focusIsTask && taskNote && (
            <div className="mt-1 text-[11px] leading-5 break-words" style={{ color: C.cardSub }} title={taskNote}>
              任务要求：{taskNoteBrief}
            </div>
          )}
          <div className="mt-1 text-[11px] leading-5" style={{ color: C.desc }}>
            {focusIsTask
              ? '这条进度属于上方任务 · 面板跟踪全站最近一次任务，不随书架切换'
              : '本面板一次只跟一个流程 · 下方切换条可切到最近其他流程（不会把别的流程塞进当前任务里）'}
          </div>
          {focusIsTask && currentBookId && taskBook && taskBook !== currentBookId && (
            <div className="mt-1 inline-block px-1.5 py-0.5 rounded text-[11px]" style={{ color: '#f59e0b', border: '1px solid #f59e0b55' }}>
              ⚠ 不是书架当前书（{currentBookId}）的任务 — 这是另一本书的独立进度，与本次点击无关
            </div>
          )}
        </div>

        {/* 说明条 */}
        <div className="shrink-0 px-4 py-2 text-[12px] leading-5" style={{ color: C.desc }}>
          每个环节独立卡片展示，点开可见该环节的每次模型调用（提示词 / 思考 / 产物）；所有 AI 流程都进这里；清空只影响当前窗口
        </div>

        {switchItems.length > 0 && (
          <div
            className="shrink-0 px-3 py-1.5 flex items-center gap-1.5 overflow-x-auto"
            style={{ borderTop: `1px solid ${C.divider}`, borderBottom: `1px solid ${C.divider}`, background: 'rgba(0,0,0,0.12)' }}
          >
            {switchItems.map((item) => {
              const active = item.id === focusId
              const isRunning = item.status === 'running'
              return (
                <button
                  key={item.id}
                  onClick={() => setFocusIdState(item.id)}
                  className="shrink-0 flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] border whitespace-nowrap"
                  title={item.label}
                  style={{
                    color: active ? C.title : C.cardSub,
                    borderColor: active ? C.badgeBorder : C.divider,
                    background: active ? 'rgba(255,255,255,0.06)' : 'transparent',
                  }}
                >
                  <span
                    className={`inline-block w-1.5 h-1.5 rounded-full ${isRunning ? 'animate-pulse' : ''}`}
                    style={{ background: isRunning ? C.badgeText : item.status === 'error' ? '#e87040' : '#63b06a' }}
                  />
                  {item.label}
                </button>
              )
            })}
          </div>
        )}

        {/* 统计胶囊条 */}
        {template.length > 0 && (activeTaskId ? true : false) && (
          <div
            className="shrink-0 px-4 py-1.5 flex items-center gap-3 text-[11px] flex-wrap"
            style={{ borderTop: `1px solid ${C.divider}`, borderBottom: `1px solid ${C.divider}`, background: 'rgba(0,0,0,0.18)' }}
          >
            <span style={{ color: C.desc }}>
              共 <b style={{ color: C.title }}>{totalCount}</b> 步
            </span>
            <span style={{ color: C.badgeText }}>✓ 完成 {doneCount}</span>
            <span style={{ color: failedCount > 0 ? '#e87040' : C.cardSub }}>✗ 失败 {failedCount}</span>
            {runningCount > 0 && (
              <span style={{ color: C.badgeText }}>
                <span className="inline-block w-1.5 h-1.5 rounded-full bg-current animate-pulse mr-1 align-middle" />
                进行中 {runningCount}
              </span>
            )}
            <span style={{ color: C.cardSub }}>◯ 未执行 {pendingCount}</span>
          </div>
        )}

        {/* 主体 */}
        <div className="flex-1 min-h-0 flex flex-col">
          {focusIsTask && status === 'idle' && (
            <div className="flex-1 flex items-center justify-center text-[12px]" style={{ color: C.desc }}>
              连接中 · 等待任务开始…
            </div>
          )}

          {focusIsTask && status !== 'idle' && (
            <>
              {/* 环节卡片列表 */}
              <div
                ref={listWrapRef}
                className="flex-1 min-h-0 overflow-auto px-3 py-2 space-y-2"
                onScroll={(e) => {
                  const el = e.currentTarget
                  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 32
                  if (!atBottom) setFollowing(false)
                }}
              >
                {visibleStages.length === 0 && !activeStageName && (
                  <div className="px-1 py-2 text-[12px]" style={{ color: C.cardSub }}>
                    尚无环节回执…
                  </div>
                )}

                {visibleStages.map((s, i) => (
                  <StageCard
                    key={i}
                    stage={s}
                    durationMs={durations[i]}
                    defaultOpen={!s.ok}
                    calls={callsForStage(activeFeedRun?.calls, s.name)}
                  />
                ))}

                {activeStageName && (
                  <StageCard
                    stage={{ name: activeStageName, ok: true, detail: activeStageDetail ?? '', ts: Date.now() / 1000, active: true }}
                    durationMs={null}
                    isActive
                    defaultOpen
                    streamContent={visibleStream || undefined}
                    streamCharCount={visibleStream.length || undefined}
                    calls={callsForStage(activeFeedRun?.calls, activeStageName)}
                  />
                )}

                {waitingNext && (
                  <div className="px-3 py-2 flex items-center gap-2 text-[12px]" style={{ color: C.badgeText }}>
                    <span className="inline-block w-2 h-2 rounded-full animate-pulse" style={{ background: C.badgeText }} />
                    正在处理下一步…
                  </div>
                )}

                {/* 未执行的参考 stage（淡灰锁定卡片，不可展开）——仅在终态显示；运行中只是"还没轮到" */}
                {pendingStages.length > 0 && status !== 'running' && (
                  <>
                    <div className="px-1 pt-2 pb-1 text-[10px] uppercase tracking-wider" style={{ color: C.cardSub }}>
                      ── {status === 'error' || status === 'interrupted' ? '中断后未执行' : '后续未执行'} · {pendingStages.length} 个环节 ──
                    </div>
                    {pendingStages.map((name) => (
                      <div
                        key={`pending-${name}`}
                        className="rounded-lg border px-3 py-2 flex items-center gap-2"
                        style={{
                          background: 'transparent',
                          borderColor: C.divider,
                          borderWidth: 1,
                          opacity: 0.55,
                        }}
                      >
                        <svg
                          className="h-3.5 w-3.5 shrink-0"
                          style={{ color: C.cardSub }}
                          viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
                          strokeLinecap="round" strokeLinejoin="round"
                        >
                          <rect width="18" height="11" x="3" y="11" rx="2" ry="2" />
                          <path d="M7 11V7a5 5 0 0 1 10 0v4" />
                        </svg>
                        <span className="text-[12px] font-medium" style={{ color: C.cardSub }}>
                          {phaseLabel(name)}
                        </span>
                        <span className="text-[10px] font-mono truncate" style={{ color: C.cardSub }}>
                          · {name}
                        </span>
                        <div className="flex-1" />
                        <span
                          className="px-2 py-0.5 rounded-full text-[10px] whitespace-nowrap"
                          style={{ color: C.cardSub, border: `1px solid ${C.divider}` }}
                        >
                          未执行
                        </span>
                      </div>
                    ))}
                  </>
                )}

                {!!looseTaskCalls.length && (
                  <div className="rounded-lg border px-2 py-2 space-y-1" style={{ borderColor: C.cardBorder, background: 'rgba(255,255,255,0.02)' }}>
                    <div className="text-[10px] font-semibold" style={{ color: C.badgeText }}>
                      ── 本任务的模型调用（未绑定到具体环节）· {looseTaskCalls.length} 次 ──
                    </div>
                    {looseTaskCalls.map((c) => <CallItem key={c.id} call={c} />)}
                  </div>
                )}

              </div>

              {/* 进程中断 · 可续跑（不是失败）：stale 追加僵尸提示，续跑须作者手动点击 */}
              {status === 'interrupted' && (
                <div
                  className="shrink-0 px-3 py-2 mx-3 mb-2 rounded text-[12px] leading-5"
                  style={{ background: 'rgba(200,140,30,0.14)', border: '1px solid rgba(220,160,50,0.45)', color: '#e0b060' }}
                >
                  <div className="flex items-center gap-2 flex-wrap mb-1">
                    <span className="font-semibold">进程中断 · 可续跑（按章重跑）</span>
                    {isZombie && (
                      <span
                        className="px-2 py-0.5 rounded-full text-[10px] whitespace-nowrap"
                        style={{ color: '#e08050', border: '1px solid rgba(220,120,60,0.6)' }}
                      >
                        僵尸任务（长时间无更新）
                      </span>
                    )}
                  </div>
                  <div className="break-all">
                    {snap?.interrupt_reason === 'process_restarted'
                      ? '后端进程重启/代码热载导致任务中断（非逻辑失败）。'
                      : snap?.interrupt_reason === 'thread_lost_timeout'
                        ? '任务线程已丢失且长时间无更新（非逻辑失败）。'
                        : '任务未完成（非逻辑失败）。'}
                    续跑会按当前书/章重跑整条链路（不是断点续传）；重写链自带事务回滚，已提交章节不受影响。
                  </div>
                  {snap?.resumable === false && (
                    <div className="mt-1">该任务缺少书/章参数，无法续跑；请在助手重新发起。</div>
                  )}
                  {resumeError && <div className="mt-1 break-all" style={{ color: '#e89050' }}>{resumeError}</div>}
                  <button
                    onClick={onResume}
                    disabled={resuming || snap?.resumable === false}
                    className="mt-2 px-3 py-1 rounded text-[12px] border disabled:opacity-50 disabled:cursor-not-allowed"
                    style={{ color: C.badgeText, borderColor: C.badgeBorder }}
                  >
                    {resuming ? '续跑中…' : '续跑本任务'}
                  </button>
                </div>
              )}

              {/* 查询侧僵尸降级前：running 且 stale=true 也按僵尸提示（process_restarted 已由 isZombie 排除） */}
              {status === 'running' && isZombie && (
                <div
                  className="shrink-0 px-3 py-2 mx-3 mb-2 rounded text-[12px] leading-5"
                  style={{ background: 'rgba(200,140,30,0.10)', border: '1px solid rgba(220,120,60,0.45)', color: '#e08050' }}
                >
                  僵尸任务（长时间无更新）：任务状态仍显示运行中，但执行线程已丢失且长时间无更新。仅提示，不会自动取消或重跑。
                </div>
              )}

              {/* 长时间无进展：仅提示，绝不自动取消或重跑（僵尸已单独提示，避免重复） */}
              {stalled && !isZombie && (
                <div
                  className="shrink-0 px-3 py-2 mx-3 mb-2 rounded text-[12px] leading-5"
                  style={{ background: 'rgba(200,140,30,0.10)', border: '1px solid rgba(220,160,50,0.35)', color: '#d8a850' }}
                >
                  长时间无进展：当前环节已超过 5 分钟无变化。任务仍在运行，这里只做提示，不会自动取消或重跑。
                </div>
              )}

              {/* 失败错误横幅 */}
              {status === 'error' && snap?.error && (
                <div className="shrink-0 px-3 py-2 mx-3 mb-2 rounded text-[12px] leading-5"
                  style={{ background: 'rgba(180,60,30,0.15)', border: '1px solid rgba(200,80,40,0.5)', color: '#e8a070' }}>
                  <div className="font-semibold mb-1">执行失败</div>
                  <div className="break-all">{snap.error}</div>
                </div>
              )}

              {/* 简略模式底部 */}
              {compact && visibleStages.length > 0 && (
                <div className="shrink-0 px-4 py-1.5 text-[11px]" style={{ color: C.desc }}>
                  {activeStageName
                    ? `${phaseLabel(activeStageName)} 执行中…`
                    : status === 'done'
                      ? `完成（${visibleStages.filter((s) => s.ok).length}/${visibleStages.length} 步通过）`
                      : status === 'error'
                        ? `失败：${snap?.error ?? '未知'}`
                        : status === 'cancelled'
                          ? '已取消'
                          : status === 'interrupted'
                            ? '进程中断 · 可续跑（按章重跑）'
                            : '等待中'}
                </div>
              )}
            </>
          )}

          {!focusIsTask && focusRun && (
            <div className="flex-1 min-h-0 overflow-auto px-3 py-2">
              <FlowRunBlock run={focusRun} />
            </div>
          )}

          {!focusIsTask && !focusRun && (
            <div className="flex-1 flex items-center justify-center text-[12px] px-4 text-center" style={{ color: C.cardSub }}>
              等待生成 —— 任何 AI 动作（写章 / 重写 / 润色 / 修订 / 去AI味 / 一致性审计 / 建书立项 / 大纲设定 / 编辑器改写 / 对话）
              都会在这里逐环节铺开进度、模型调用与产物
            </div>
          )}
        </div>
      </div>
    </>
  )
}
