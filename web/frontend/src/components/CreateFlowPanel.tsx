import { useEffect, useRef, useState } from 'react'
import { Activity, Check, CircleDashed, Sparkles } from 'lucide-react'
import {
  AI_DIRECTIONS_PROMPT,
  CREATE_FLOW_EVENT,
  DIRECTIONS,
  DRAFT_FIELDS,
  TRUTH_QUESTIONS,
  deriveFlow,
  emitCreateFlowSend,
  questionIndex,
  readDirection,
  readInitialSnapshot,
  saveDirection,
  type CreateFlowExternal,
  type CreateFlowSnapshot,
  type FlowStep,
} from './createFlow'

/* ─────────────────────  中间卡：建书流程提示 / AI 实况（2026-11-07）  ─────────────────────
   固定三列布局的「中间」：最左 icon 工具列 ｜ 本卡 ｜ 最右建书 chatbox。
   六步里每一步都展示：成果（真实内容）、缺口（还差几项 / 差哪些）、进度、当前 AI 动作。
   步骤定义/推导全在 createFlow.ts，UI 不自己记进度、不臆造已完成状态。
─────────────────────────────────────────────────────────────────────────────────────── */

interface LiveEvent {
  id: number
  time: string
  text: string
  kind: 'user' | 'assistant' | 'system' | 'progress'
}

let EVENT_SEQ = 0
const MAX_EVENTS = 60

function clip(s: string, n: number): string {
  const one = s.replace(/\s+/g, ' ').trim()
  return one.length > n ? `${one.slice(0, n)}…` : one
}

function clock(): string {
  const d = new Date()
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`
}

function event(text: string, kind: LiveEvent['kind'], time = clock()): LiveEvent {
  EVENT_SEQ += 1
  return { id: EVENT_SEQ, time, text, kind }
}

function seedEvents(nonce: number): LiveEvent[] {
  const snap = readInitialSnapshot(nonce)
  const out: LiveEvent[] = [event('建书流程已就绪，等待你先说一句想法', 'system', '—')]
  for (const m of snap.messages) {
    out.push(event(`${m.role === 'user' ? '你' : '助手'} · ${clip(m.content, 70)}`, m.role, '—'))
  }
  return out.slice(-MAX_EVENTS)
}

/** App 侧真实回执 → 实况条目：render 期纯派生（不用 effect setState，避免级联渲染），id 稳定 */
function receiptEvents(ext: CreateFlowExternal): LiveEvent[] {
  const out: LiveEvent[] = []
  if (ext.mdWritten) out.push({ id: -1, time: '—', text: '设定/建书摘要.md 已写入（八项全量）', kind: 'system' })
  const bootCount = ext.written.length + ext.skipped.length
  if (bootCount > 0) out.push({ id: -2, time: '—', text: `开书资料回执：${bootCount} 份文件已落盘 / 已存在`, kind: 'system' })
  if (ext.done) out.push({ id: -3, time: '—', text: '建书全流程完成，正在进入新书', kind: 'system' })
  if (ext.error) out.push({ id: -4, time: '—', text: `提示 · ${clip(ext.error, 80)}`, kind: 'progress' })
  return out
}

/** 从新旧快照差量生成实况事件：只报真实发生的消息/状态变化 */
function diffEvents(prev: CreateFlowSnapshot | null, next: CreateFlowSnapshot): LiveEvent[] {
  const out: LiveEvent[] = []
  const from = prev ? prev.messages.length : 0
  for (const m of next.messages.slice(from)) {
    out.push(event(`${m.role === 'user' ? '你' : '助手'} · ${clip(m.content, 70)}`, m.role))
  }
  if (!prev?.busy && next.busy) out.push(event('开始生成回复…', 'system'))
  if (prev?.busy && !next.busy) out.push(event('本轮回复已返回', 'system'))
  if (next.thinking && next.thinking !== prev?.thinking) out.push(event(`进度 · ${clip(next.thinking, 80)}`, 'progress'))
  if (!prev?.draft && next.draft) out.push(event('已交付【建书摘要】+ DRAFT_JSON 八字段草案卡', 'system'))
  return out
}

function StatusBadge({ status }: { status: FlowStep['status'] }) {
  if (status === 'done') {
    return (
      <span className="inline-flex items-center gap-1 shrink-0 text-[11px] text-emerald-400">
        <Check className="h-3 w-3" strokeWidth={2.2} aria-hidden />已完成
      </span>
    )
  }
  if (status === 'active') {
    return <span className="shrink-0 text-[11px] text-[#f59e0b]">进行中</span>
  }
  return (
    <span className="inline-flex items-center gap-1 shrink-0 text-[11px] text-[#666]">
      <CircleDashed className="h-3 w-3" strokeWidth={1.8} aria-hidden />未开始
    </span>
  )
}

function ProgressBar({ value, active }: { value: number; active: boolean }) {
  return (
    <div className="h-1 w-full rounded-full bg-[#2a2a2a] overflow-hidden">
      <div
        className={`h-full rounded-full transition-[width] duration-300 ${active ? 'bg-[#f59e0b]' : 'bg-[#4a7c59]'}`}
        style={{ width: `${Math.max(0, Math.min(100, value))}%` }}
      />
    </div>
  )
}

function DirectionPicker({
  nonce,
  selectedId,
  busy,
  onPick,
}: {
  nonce: number
  selectedId: string | null
  busy: boolean
  onPick: (id: string) => void
}) {
  const askAi = () => {
    emitCreateFlowSend(AI_DIRECTIONS_PROMPT, nonce)
  }
  return (
    <div className="space-y-1.5">
      <div className="grid gap-1.5">
        {DIRECTIONS.map((d) => {
          const picked = d.id === selectedId
          return (
            <button
              key={d.id}
              type="button"
              disabled={busy}
              onClick={() => onPick(d.id)}
              title={busy ? '助手回复中，稍后再选' : `选择方向：${d.name}`}
              className={`w-full text-left rounded-lg border px-2.5 py-2 transition-colors disabled:opacity-50 disabled:cursor-not-allowed ${
                picked
                  ? 'border-[#f59e0b]/50 bg-[#f59e0b]/10'
                  : 'border-[#2e2e2e] bg-[#191919] hover:border-[#f59e0b]/40 hover:bg-[#1d1b17]'
              }`}
            >
              <div className="flex items-center justify-between gap-2">
                <span className={`text-[12px] font-medium ${picked ? 'text-[#f59e0b]' : 'text-[#ddd]'}`}>{d.name}</span>
                <span className={`shrink-0 text-[11px] ${picked ? 'text-[#f59e0b]' : 'text-[#777]'}`}>{picked ? '已选' : '选择'}</span>
              </div>
              <div className="mt-0.5 text-[11px] leading-relaxed text-[#888]">{d.desc}</div>
              <div className="mt-1 flex flex-wrap gap-1">
                {d.tags.map((t) => (
                  <span key={t} className="rounded border border-[#333] px-1.5 py-0.5 text-[10px] text-[#888]">{t}</span>
                ))}
              </div>
            </button>
          )
        })}
      </div>
      <button
        type="button"
        disabled={busy}
        onClick={askAi}
        className="w-full rounded-lg border border-dashed border-[#3a3a3a] px-2.5 py-1.5 text-[11px] text-[#aaa] hover:border-[#f59e0b]/50 hover:text-[#f59e0b] disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
      >
        <Sparkles className="mr-1 inline h-3 w-3" strokeWidth={1.8} aria-hidden />
        让 AI 基于我的想法现给 3 个方向
      </button>
      <div className="text-[10px] leading-relaxed text-[#666]">选择会作为一条真实消息发到右侧对话，参与后续摘要；不选也可直接继续。</div>
    </div>
  )
}

function StepCard({
  step,
  index,
  nonce,
  directionId,
  busy,
  onPickDirection,
}: {
  step: FlowStep
  index: number
  nonce: number
  directionId: string | null
  busy: boolean
  onPickDirection: (id: string) => void
}) {
  const active = step.status === 'active'
  const done = step.status === 'done'
  const okCount = step.items.filter((i) => i.ok).length
  return (
    <section
      className={`rounded-lg border transition-colors ${
        active ? 'border-[#f59e0b]/45 bg-[#1b1813]' : done ? 'border-[#2a3a2e] bg-[#161a17]' : 'border-[#292929] bg-[#171717]'
      }`}
    >
      <div className="flex items-start gap-2.5 px-3 py-2.5">
        <span
          className={`mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-[11px] tabular-nums ${
            done ? 'bg-[#1f3a2a] text-emerald-400' : active ? 'bg-[#f59e0b] text-[#1a1a1a]' : 'bg-[#262626] text-[#777]'
          }`}
        >
          {done ? <Check className="h-3 w-3" strokeWidth={2.2} aria-hidden /> : index + 1}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center justify-between gap-2">
            <span className={`text-[13px] font-medium ${active ? 'text-[#f59e0b]' : done ? 'text-[#ddd]' : 'text-[#999]'}`}>{step.title}</span>
            <StatusBadge status={step.status} />
          </div>
          <div className="mt-0.5 text-[11px] leading-relaxed text-[#777]">{step.goal}</div>
          <div className="mt-2"><ProgressBar value={step.progress} active={active} /></div>
          <div className="mt-1 flex items-center justify-between text-[10px] tabular-nums text-[#777]">
            <span>进度 {step.progress}%</span>
            <span>成果 {step.items.length > 0 ? `${okCount}/${step.items.length}` : '0 项'}{step.missing.length > 0 ? ` · 还差 ${step.missing.length} 项` : ''}</span>
          </div>
        </div>
      </div>

      <div className="space-y-1.5 border-t border-[#252525] px-3 py-2.5">
        {step.items.length > 0 ? (
          <div className="space-y-1">
            {step.items.map((it, i) => (
              <div key={`${it.label}-${i}`} className="flex items-start gap-1.5 text-[11px] leading-relaxed">
                <span className={`mt-1 h-1.5 w-1.5 shrink-0 rounded-full ${it.ok ? 'bg-emerald-500' : 'bg-[#444]'}`} />
                <span className="shrink-0 text-[#888]">{it.label}</span>
                <span className={`min-w-0 flex-1 break-words ${it.ok ? 'text-[#ccc]' : 'text-[#666]'}`}>{it.value}</span>
              </div>
            ))}
          </div>
        ) : (
          <div className="text-[11px] text-[#666]">暂无成果 · 完成后此处会列出实际内容</div>
        )}
        {step.missing.length > 0 && (
          <div className="rounded border border-[#3a3020] bg-[#1f1a12] px-2 py-1.5 text-[11px] leading-relaxed text-[#d0a95a]">
            还差 {step.missing.length} 项：{step.missing.join('、')}
          </div>
        )}
        {step.note && <div className="text-[10px] leading-relaxed text-[#666]">{step.note}</div>}
        {step.id === 'direction' && (
          <DirectionPicker nonce={nonce} selectedId={directionId} busy={busy} onPick={onPickDirection} />
        )}
        <div className="flex items-start gap-1.5 pt-0.5 text-[11px] leading-relaxed text-[#999]">
          <Activity className="mt-0.5 h-3 w-3 shrink-0 text-[#f59e0b]" strokeWidth={1.8} aria-hidden />
          <span className="min-w-0 flex-1 break-words">{step.current}</span>
        </div>
      </div>
    </section>
  )
}

export default function CreateFlowPanel({
  nonce,
  external,
  onRetry,
}: {
  nonce: number
  external: CreateFlowExternal
  onRetry?: () => void
}) {
  const [snap, setSnap] = useState<CreateFlowSnapshot>(() => readInitialSnapshot(nonce))
  const lastSnapRef = useRef<CreateFlowSnapshot | null>(null)
  const [directionId, setDirectionId] = useState<string | null>(() => readDirection(nonce))
  const [events, setEvents] = useState<LiveEvent[]>(() => seedEvents(nonce))
  const logRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const onSnap = (e: Event) => {
      const next = (e as CustomEvent<CreateFlowSnapshot>).detail
      if (!next || next.nonce !== nonce) return
      const added = diffEvents(lastSnapRef.current, next)
      lastSnapRef.current = next
      setSnap(next)
      if (added.length > 0) setEvents((prev) => [...prev, ...added].slice(-MAX_EVENTS))
    }
    window.addEventListener(CREATE_FLOW_EVENT, onSnap)
    return () => window.removeEventListener(CREATE_FLOW_EVENT, onSnap)
  }, [nonce])

  const allEvents = [...events, ...receiptEvents(external)]

  useEffect(() => {
    const el = logRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [events])

  const pickDirection = (id: string) => {
    const d = DIRECTIONS.find((x) => x.id === id)
    if (!d || snap.busy) return
    saveDirection(nonce, id)
    setDirectionId(id)
    emitCreateFlowSend(d.prompt, nonce)
    setEvents((p) => [...p, event(`已选方向：${d.name}（已发到右侧对话）`, 'system')].slice(-MAX_EVENTS))
  }

  const flowExternal: CreateFlowExternal = { ...external, directionId }
  const steps = deriveFlow(snap, flowExternal)
  const doneCount = steps.filter((s) => s.status === 'done').length
  const overall = Math.round((steps.reduce((n, s) => n + s.progress, 0) / steps.length))
  const activeStep = steps.find((s) => s.status === 'active')
  const q = questionIndex(snap.messages)

  return (
    <div className="h-full flex flex-col min-h-0 bg-[#141414]">
      <header className="shrink-0 card-pad-x py-3 border-b border-[#252525]">
        <div className="flex items-center justify-between gap-2">
          <div className="min-w-0">
            <div className="text-[14px] font-medium text-[#eee]">建书流程</div>
            <div className="mt-0.5 text-[11px] text-[#777]">AI 实况 · 成果 / 缺口 / 进度均取自真实回执</div>
          </div>
          <span className="shrink-0 rounded border border-[#333] px-2 py-0.5 text-[11px] tabular-nums text-[#aaa]">
            {doneCount}/{steps.length} 步
          </span>
        </div>
        <div className="mt-2.5"><ProgressBar value={overall} active={!!activeStep} /></div>
        <div className="mt-1 flex items-center justify-between text-[10px] tabular-nums text-[#777]">
          <span>总进度 {overall}%</span>
          <span>{snap.busy ? '助手正在生成…' : activeStep ? `当前：${activeStep.title}` : '等待开始'}</span>
        </div>
      </header>

      {external.error && (
        <div className="shrink-0 flex items-center gap-2 border-b border-[#f59e0b]/30 bg-[#f59e0b]/10 px-3 py-2">
          <span className="min-w-0 flex-1 text-[11px] leading-relaxed text-[#f59e0b]">{external.error}</span>
          {onRetry && (
            <button
              type="button"
              onClick={onRetry}
              disabled={external.busy}
              className="shrink-0 rounded bg-[#f59e0b] px-2 py-1 text-[11px] text-black hover:bg-[#fbbf24] disabled:opacity-50 transition-colors"
            >
              {external.busy ? '生成中…' : '继续建书'}
            </button>
          )}
        </div>
      )}

      <div className="flex-1 min-h-0 card-scroll space-y-2">
        <div className="text-[10px] leading-relaxed text-[#666]">
          第 1 步按 7 个真相问题收齐 8 项主资料（{DRAFT_FIELDS.map((f) => f.label).join(' / ')}）；
          {q > 0
            ? `当前问到第 ${q}/${TRUTH_QUESTIONS.length} 问 · ${TRUTH_QUESTIONS[q - 1] ?? ''}`
            : '等待助手开始第一个问题'}
        </div>
        {steps.map((s, i) => (
          <StepCard
            key={s.id}
            step={s}
            index={i}
            nonce={nonce}
            directionId={directionId}
            busy={snap.busy}
            onPickDirection={pickDirection}
          />
        ))}
      </div>

      <div className="shrink-0 h-[142px] flex flex-col border-t border-[#252525]">
        <div className="shrink-0 flex items-center justify-between px-3 pt-2 pb-1">
          <span className="flex items-center gap-1.5 text-[11px] text-[#aaa]">
            <Activity className="h-3 w-3 text-[#f59e0b]" strokeWidth={1.8} aria-hidden />AI 实况
          </span>
          <span className="text-[10px] tabular-nums text-[#666]">{allEvents.length} 条</span>
        </div>
        <div ref={logRef} className="flex-1 min-h-0 overflow-y-auto overscroll-contain scroll-y px-3 pb-2 space-y-0.5">
          {allEvents.map((e) => (
            <div key={e.id} className="flex gap-2 text-[11px] leading-relaxed">
              <span className="shrink-0 tabular-nums text-[#555]">{e.time}</span>
              <span className={`min-w-0 flex-1 break-words ${
                e.kind === 'user' ? 'text-[#ddd]' : e.kind === 'assistant' ? 'text-[#a8a8a8]' : e.kind === 'progress' ? 'text-[#d0a95a]' : 'text-[#7f9c86]'
              }`}>{e.text}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
