import { useCallback, useEffect, useRef, useState } from 'react'
import { api, type SupervisorEvent, type SupervisorGoal, type SupervisorState } from '../api'

/* ─────────────────────────  流水线监督器（2026-10-25）  ─────────────────────────
 * 目标驱动的自动推进驾驶舱：监督器在后端常驻（浏览器关了也照跑），本组件只做
 * 展示与操作入口——目标进度、在跑任务、收件箱督促（闸门/失败/完成）、暂停/恢复。
 * 后端未开 NWW_SUPERVISOR_ENABLED 时接口 404 → 本组件整体隐藏，不留空壳。
 */

/** 任务环节中文标签（ChatBox.PHASE_LABELS 的子集，独立维护避免循环依赖） */
const PHASE_LABELS: Record<string, string> = {
  queued: '排队中',
  prealign: '预对齐',
  backup: '备份旧稿',
  packet: '打包 Writing Packet',
  writer: '主笔写作',
  normalize: '正文归一',
  wordcount: '字数收敛',
  consistency: '一致性审查',
  review_cycle: '审稿闭环',
  post_write: '追踪提交/闸门校验',
  deslop: '去AI味清理',
}

function phaseLabel(p?: string | null): string {
  if (!p) return '…'
  const am = /^audit_ch(\d+)$/.exec(p)
  if (am) return `审计第${am[1]}章`
  return PHASE_LABELS[p] ?? p
}

/** 在跑任务紧凑标签：audit 是区间任务（无单章号），防「audit第章」占位 */
function runningTaskLabel(t: { mode?: string; chapter_id?: number; range?: number[] | null }): string {
  if (t.mode === 'audit') {
    const r = t.range ?? []
    return r.length === 2 && r[0] ? `审计第${r[0]}-${r[1]}章` : '一致性审计'
  }
  const mode = ({ write: '写', rewrite: '重写', deslop: '去AI味', revise: '修订', polish: '润色' } as Record<string, string>)[t.mode ?? ''] ?? t.mode
  return t.chapter_id != null ? `${mode}第${t.chapter_id}章` : mode
}

const PHASE_LABELS_ZH: Record<string, string> = {
  no_project: '未初始化',
  plan_needed: '缺细纲',
  ready_to_write: '待写下一章',
  draft_in_progress: '有正文未提交',
  inconsistent: '数据异常',
  views_dirty: '派生视图不一致',
  error: '观察失败',
}

/** 监督事件等级 → 展示色 */
const LEVEL_CLS: Record<string, string> = {
  gate: 'border-amber-600/40 bg-[#1c1408] text-amber-300',
  fail: 'border-red-900/50 bg-[#1a1214] text-red-300',
  done: 'border-emerald-700/40 bg-[#142014] text-emerald-300',
  info: 'border-[#333] bg-[#161616] text-[#aaa]',
}

function goalText(goal: SupervisorGoal | null): string {
  if (!goal) return '未设目标'
  return goal.type === 'write_to'
    ? `写到第${goal.target_chapter}章`
    : `今日完成 ${goal.daily_chapters} 章`
}

/** 收件箱事件行（点击标已读） */
function EventRow({ ev, onRead }: { ev: SupervisorEvent; onRead: (id: string) => void }) {
  const unread = !ev.read
  return (
    <button
      type="button"
      onClick={() => unread && onRead(ev.id)}
      className={`w-full text-left rounded-lg border px-2.5 py-1.5 transition-colors ${LEVEL_CLS[ev.level] ?? LEVEL_CLS.info} ${unread ? '' : 'opacity-55'}`}
    >
      <div className="flex items-center gap-2">
        {unread && <span className="shrink-0 w-1.5 h-1.5 rounded-full bg-current" aria-hidden />}
        <span className="text-[12px] font-medium min-w-0 truncate">
          {ev.chapter != null ? `第${ev.chapter}章 · ` : ''}{ev.title}
        </span>
        <span className="flex-1" />
        <span className="shrink-0 text-[11px] text-[#666] font-mono">{ev.ts}</span>
      </div>
      {ev.detail && (
        <div className="mt-0.5 text-[11px] text-[#888] whitespace-pre-wrap break-words leading-relaxed">{ev.detail}</div>
      )}
    </button>
  )
}

/**
 * 监督状态条：ChatBox 输入区上方常驻（绑书态）。
 * 一行总览 + 展开抽屉（目标设定 / 在跑任务 / 收件箱 / 暂停·恢复·清除）。
 * 5s 轮询 /supervisor/state（只读缓存与状态文件，后端无压力）。
 */
export function SupervisorBar({ bookId }: { bookId: string }) {
  const [st, setSt] = useState<SupervisorState | null>(null)
  const [gone, setGone] = useState(false) // 后端未挂载监督器 → 整体隐藏
  const [open, setOpen] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const timerRef = useRef<number | null>(null)

  const refresh = useCallback(() => {
    api.supervisorState(bookId)
      .then((s) => { setSt(s); setGone(false) })
      .catch((e: unknown) => {
        const msg = e instanceof Error ? e.message : String(e)
        if (/404|not found/i.test(msg)) setGone(true)
      })
  }, [bookId])

  useEffect(() => {
    setSt(null)
    setOpen(false)
    setGone(false)
    refresh()
    timerRef.current = window.setInterval(refresh, 5000)
    const onChanged = () => refresh()
    window.addEventListener('nww-supervisor-changed', onChanged)
    return () => {
      if (timerRef.current) window.clearInterval(timerRef.current)
      window.removeEventListener('nww-supervisor-changed', onChanged)
    }
  }, [refresh])

  const act = async (fn: () => Promise<unknown>) => {
    setErr(null)
    try {
      await fn()
      refresh()
      window.dispatchEvent(new Event('nww-supervisor-changed'))
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    }
  }

  if (gone) return null

  const goal = st?.goal ?? null
  const paused = st?.policy.paused ?? false
  const stopReason = st?.runtime.stop_reason ?? null
  const running = st?.running?.[0] ?? null
  const unread = st?.unread ?? 0
  const events = st?.events ?? []
  const dailyDone = st?.runtime.chapters_done_today ?? 0
  const dailyText = goal?.type === 'daily' ? `今日 ${Math.min(dailyDone, goal.daily_chapters ?? 0)}/${goal.daily_chapters}` : null

  return (
    <div className="mb-2 rounded-xl border border-[#2f3a2f]/60 bg-[#131a13] px-2.5 py-1.5">
      {/* 总览行 */}
      <div className="flex items-center gap-2 flex-wrap text-[12px]">
        <span className={`font-medium ${goal ? 'text-emerald-300' : 'text-[#777]'}`} title="监督器：目标驱动的自动推进（后端常驻，页面关了也照跑）">
          监督
        </span>
        <span className="text-[#bbb]">{goalText(goal)}</span>
        {dailyText && <span className="text-[#999]">{dailyText}</span>}
        {goal?.type === 'write_to' && st?.target_chapter != null && (
          <span className="text-[#999]">下一章第{st.target_chapter}章</span>
        )}
        {st?.phase && <span className="text-[#777]">阶段：{PHASE_LABELS_ZH[st.phase] ?? st.phase}</span>}
        {running && (
          <span className="flex items-center gap-1 text-amber-300" title={`任务 ${running.task_id}（${running.origin === 'supervisor' ? '监督器派单' : '作者手动'}）`}>
            <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse" aria-hidden />
            {runningTaskLabel(running)} · {phaseLabel(running.phase)}
          </span>
        )}
        {paused && <span className="text-[#999] border border-[#3a3a3a] rounded px-1.5">已暂停</span>}
        {stopReason && !paused && (
          <span className="text-red-300" title="自动推进已停止：处理完待办后点「继续」，或重设目标">
            已停止：{stopReason === 'goal_reached' ? '目标达成' : stopReason === 'circuit_breaker' ? '熔断（连续失败）' : stopReason === 'daily_cap' ? '达每日上限' : stopReason === 'llm_unavailable' ? '模型端点持续不可用（恢复后点继续）' : stopReason}
          </span>
        )}
        <span className="flex-1" />
        {unread > 0 && (
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            className="px-1.5 py-0.5 rounded-md border border-amber-600/50 text-amber-300 hover:bg-amber-500/10 transition-colors"
            title="监督收件箱：闸门督促 / 失败回执 / 完成汇报"
          >
            待办 {unread}
          </button>
        )}
        {goal && (
          <button
            type="button"
            onClick={() => void act(() => api.supervisorSetPaused(bookId, !paused))}
            className={`px-1.5 py-0.5 rounded-md border transition-colors ${paused ? 'border-emerald-500/40 text-emerald-400 hover:bg-emerald-500/10' : 'border-[#3a3a3a] text-[#999] hover:border-[#666]'}`}
          >
            {paused ? '恢复派单' : '暂停'}
          </button>
        )}
        {goal && stopReason && (
          <button
            type="button"
            onClick={() => void act(() => api.supervisorResume(bookId))}
            className="px-1.5 py-0.5 rounded-md border border-[#f59e0b]/40 text-[#f59e0b] hover:bg-[#f59e0b]/10 transition-colors"
            title="清除停止态与熔断账本，继续当前目标"
          >
            继续
          </button>
        )}
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className="text-[#666] hover:text-[#aaa] transition-colors"
          title={open ? '收起监督面板' : '展开监督面板（目标设定 / 收件箱 / 事件流）'}
        >
          {open ? '▲' : '▼'}
        </button>
      </div>
      {err && <div className="mt-1 text-[11px] text-red-400 break-words">{err}</div>}

      {/* 展开抽屉：目标设定 + 收件箱 */}
      {open && st && (
        <div className="mt-2 pt-2 border-t border-[#243024] flex flex-col gap-2.5">
          <GoalSetter
            bookId={bookId}
            goal={goal}
            lastCommittedHint={st.target_chapter != null ? st.target_chapter - 1 : undefined}
            onDone={() => {
              refresh()
              window.dispatchEvent(new Event('nww-supervisor-changed'))
            }}
          />
          {goal && (
            <div className="flex items-center gap-2 text-[11px] text-[#777]">
              <span>连续失败 {st.runtime.consecutive_failures ?? 0}/{st.policy.max_consecutive_failures}（熔断即停）</span>
              <span>今日完成 {dailyDone}/{st.policy.daily_chapter_cap} 章硬上限</span>
              <span className="flex-1" />
              <button
                type="button"
                onClick={() => void act(() => api.supervisorSetGoal(bookId, null))}
                className="px-1.5 py-0.5 rounded-md border border-red-900/50 text-red-300 hover:border-red-500 transition-colors"
                title="清除目标并停止自动推进（不影响已写内容）"
              >
                清除目标
              </button>
            </div>
          )}
          <div className="flex flex-col gap-1 max-h-64 overflow-y-auto">
            {events.length === 0 && <div className="text-[11px] text-[#666]">收件箱为空：设定目标后，推进/失败/待闸事件会出现在这里。</div>}
            {events.slice().reverse().map((ev) => (
              <EventRow key={ev.id} ev={ev} onRead={(id) => void act(() => api.supervisorEventRead(bookId, id))} />
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

/** 目标设定器：两个快捷形态（写到第N章 / 今日N章）+ 叮嘱，后端校验 */
function GoalSetter({
  bookId, goal, lastCommittedHint, onDone,
}: {
  bookId: string
  goal: SupervisorGoal | null
  /** 便于默认值：当前已提交章（下一章-1），未知时不给默认 */
  lastCommittedHint?: number
  onDone: () => void
}) {
  const [target, setTarget] = useState<string>(goal?.type === 'write_to' ? String(goal.target_chapter ?? '') : '')
  const [daily, setDaily] = useState<string>(goal?.type === 'daily' ? String(goal.daily_chapters ?? 3) : '3')
  const [note, setNote] = useState<string>(goal?.note ?? '')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const inputCls = 'w-16 bg-[#1a1a1a] border border-[#333] rounded px-1.5 py-0.5 text-[12px] text-[#ccc] outline-none focus:border-[#f59e0b]/60 transition-colors'

  const setGoal = async (g: SupervisorGoal) => {
    setBusy(true)
    setErr(null)
    try {
      await api.supervisorSetGoal(bookId, { ...g, note: note.trim() || undefined })
      onDone()
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center gap-2 flex-wrap text-[12px] text-[#999]">
        <span>目标：</span>
        <label className="flex items-center gap-1">
          <span>写到第</span>
          <input
            className={inputCls}
            inputMode="numeric"
            placeholder={lastCommittedHint != null ? String(lastCommittedHint + 3) : '30'}
            value={target}
            onChange={(e) => setTarget(e.target.value.replace(/[^\d]/g, ''))}
          />
          <span>章</span>
        </label>
        <button
          type="button"
          disabled={busy || !target}
          onClick={() => void setGoal({ type: 'write_to', target_chapter: parseInt(target, 10) })}
          className="px-2 py-0.5 rounded-md border border-emerald-500/50 text-emerald-400 hover:bg-emerald-500/10 disabled:opacity-40 transition-colors"
          title="监督器连续写章到提交至目标章（硬闸处停并督促）"
        >
          开始监督
        </button>
        <span className="text-[#3a3a3a]">｜</span>
        <label className="flex items-center gap-1">
          <span>今日</span>
          <input
            className={inputCls}
            inputMode="numeric"
            value={daily}
            onChange={(e) => setDaily(e.target.value.replace(/[^\d]/g, ''))}
          />
          <span>章</span>
        </label>
        <button
          type="button"
          disabled={busy || !daily}
          onClick={() => void setGoal({ type: 'daily', daily_chapters: parseInt(daily, 10) })}
          className="px-2 py-0.5 rounded-md border border-emerald-500/50 text-emerald-400 hover:bg-emerald-500/10 disabled:opacity-40 transition-colors"
          title="监督器今日连续完成指定章数后自动停"
        >
          今日目标
        </button>
        <input
          className="flex-1 min-w-40 bg-[#1a1a1a] border border-[#333] rounded px-1.5 py-0.5 text-[12px] text-[#ccc] outline-none focus:border-[#f59e0b]/60 transition-colors"
          placeholder="叮嘱（可选，≤200字：节奏/字数/禁忌）"
          value={note}
          onChange={(e) => setNote(e.target.value.slice(0, 200))}
        />
      </div>
      {err && <div className="text-[11px] text-red-400 break-words">{err}</div>}
    </div>
  )
}

/**
 * 监督目标确认卡（GOAL_JSON 协议产物，绑书态）：模型提案 → 作者点「开始监督」。
 * 与 DraftCard 同款状态机 ready → starting → started / failed（failed 可改可重试）。
 */
export function GoalCard({
  goal, bookId, busy, lastCommittedHint,
}: {
  goal: SupervisorGoal
  bookId: string | null
  busy: boolean
  /** 当前已提交章（用于校验提示与默认展示；未知为 undefined） */
  lastCommittedHint?: number
}) {
  const [target, setTarget] = useState<string>(goal.type === 'write_to' ? String(goal.target_chapter ?? '') : '')
  const [daily, setDaily] = useState<string>(goal.type === 'daily' ? String(goal.daily_chapters ?? 3) : '3')
  const [note, setNote] = useState<string>(goal.note ?? '')
  const [status, setStatus] = useState<'ready' | 'starting' | 'started' | 'failed'>('ready')
  const [err, setErr] = useState<string | null>(null)

  const inputCls =
    'w-20 bg-[#1a1a1a] border border-[#333] rounded px-2 py-1 text-[13px] text-[#ccc] outline-none focus:border-[#f59e0b]/60 transition-colors disabled:opacity-60'

  const confirm = async () => {
    if (!bookId || status === 'starting' || status === 'started') return
    const g: SupervisorGoal | null =
      goal.type === 'write_to'
        ? { type: 'write_to', target_chapter: parseInt(target, 10), note: note.trim() || undefined }
        : { type: 'daily', daily_chapters: parseInt(daily, 10), note: note.trim() || undefined }
    if (!g || (g.type === 'write_to' && (!g.target_chapter || g.target_chapter < 1))) {
      setStatus('failed')
      setErr('目标章必须是正整数')
      return
    }
    setStatus('starting')
    setErr(null)
    try {
      await api.supervisorSetGoal(bookId, g)
      setStatus('started')
      window.dispatchEvent(new Event('nww-supervisor-changed'))
    } catch (e) {
      setStatus('failed')
      setErr(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <div className="bubble-max rounded-xl border border-emerald-700/40 bg-[#142014] px-3 py-2.5">
      <div className="flex items-center justify-between gap-2 mb-2">
        <span className="text-[13px] text-emerald-300 font-medium">监督目标 · 确认后自动推进</span>
        {status === 'started' && <span className="text-[12px] text-emerald-400">监督已启动</span>}
      </div>
      <div className="flex items-center gap-2 flex-wrap text-[13px] text-[#bbb] mb-2">
        {goal.type === 'write_to' ? (
          <label className="flex items-center gap-1.5">
            <span>连续写到提交至第</span>
            <input className={inputCls} inputMode="numeric" value={target} disabled={status === 'started'}
              onChange={(e) => setTarget(e.target.value.replace(/[^\d]/g, ''))} />
            <span>章{lastCommittedHint != null && <span className="text-[11px] text-[#777]">（当前已提交至第{lastCommittedHint}章）</span>}</span>
          </label>
        ) : (
          <label className="flex items-center gap-1.5">
            <span>今日完成</span>
            <input className={inputCls} inputMode="numeric" value={daily} disabled={status === 'started'}
              onChange={(e) => setDaily(e.target.value.replace(/[^\d]/g, ''))} />
            <span>章</span>
          </label>
        )}
        <input
          className="flex-1 min-w-48 bg-[#1a1a1a] border border-[#333] rounded px-2 py-1 text-[13px] text-[#ccc] outline-none focus:border-[#f59e0b]/60 transition-colors disabled:opacity-60"
          placeholder="叮嘱（可选）：节奏/字数/禁忌，随派单留档"
          value={note}
          disabled={status === 'started'}
          onChange={(e) => setNote(e.target.value.slice(0, 200))}
        />
      </div>
      <div className="text-[11px] text-[#777] mb-2 leading-relaxed">
        监督器将自动连续派单写章（失败自动重试一次），只在硬闸处停下并在收件箱督促你
        （一致性闭环、修复授权、字数下调等仍需你确认）；随时可在输入区上方的监督条暂停。
      </div>
      {err && <div className="text-[12px] text-red-400 mb-1.5">{err}</div>}
      <div className="flex items-center justify-end gap-2">
        {status !== 'started' && (
          <button
            type="button"
            onClick={confirm}
            disabled={!bookId || busy || status === 'starting'}
            title={!bookId ? '未绑定书籍' : busy ? 'agent 回复中，请稍候' : '确认目标并启动监督器'}
            className="px-3 py-1 rounded-lg text-[13px] bg-emerald-600 text-[#101510] font-medium hover:bg-emerald-500 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {status === 'starting' ? '启动中…' : status === 'failed' ? '重试启动' : '开始监督'}
          </button>
        )}
      </div>
    </div>
  )
}
