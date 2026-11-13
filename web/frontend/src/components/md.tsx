import { useCallback, useEffect, useRef, useState } from 'react'
import type { ClipboardEvent as ReactClipboardEvent, KeyboardEvent as ReactKeyboardEvent } from 'react'
import { PROSE_STYLE, mdToHtml, htmlToMd } from './md-utils'
import { api } from '../api'
import type { EditorAiCandidate, EditorAiResult } from '../api'
import {
  applySelectedDiff, defaultSelectedChangeIds, groupChanges,
  type DiffChunk,
} from '../editor/chapterDiff'

/**
 * 正文编辑器 + 选区/光标 AI 浮动工具条（2026-11-13 对齐 V14）。
 *
 * - 选区：5 预设 + 校对 + 自定义 → POST /api/editor/ai/revision
 * - 光标：6 个续写方向 + 补充要求 → POST /api/editor/ai/continuation
 * - 结果：2~3 个候选、后端单点计算的逐字 diff、按改动勾选采纳、再生成
 * - 采纳后自动落盘（onCommit），并保留一次性撤销点
 * - NWW_EDITOR_AI=0 时 health 探测 404 → 显式降级到旧单候选端点，不静默伪装
 *
 * 防选区丢失：点击工具条时 onMouseDown preventDefault；发起请求后锁住 target，
 * 不再因 selectionchange 清空 Range（V14 同款 activeElement / state 双保险）。
 */

type RevisionOperation = 'polish' | 'expand' | 'compress' | 'emotion' | 'conflict' | 'proofread' | 'custom'
type CursorOperation = 'continue' | 'transition' | 'dialogue' | 'description' | 'inner_thought' | 'conflict'

const REVISION_OPERATIONS: { key: RevisionOperation; label: string }[] = [
  { key: 'polish', label: 'AI 优化这段' },
  { key: 'expand', label: '扩写' },
  { key: 'compress', label: '精简' },
  { key: 'emotion', label: '强化情绪' },
  { key: 'conflict', label: '强化冲突' },
  { key: 'proofread', label: '校对' },
]

const CURSOR_OPERATIONS: { key: CursorOperation; label: string }[] = [
  { key: 'continue', label: '继续写下一段' },
  { key: 'transition', label: '生成过渡段' },
  { key: 'dialogue', label: '增加对话' },
  { key: 'description', label: '增加环境描写' },
  { key: 'inner_thought', label: '增加心理描写' },
  { key: 'conflict', label: '推进冲突' },
]

const PARA_TAGS = ['P', 'H1', 'H2', 'H3', 'BLOCKQUOTE']
const HIDDEN = { visible: false, top: 0, left: 0 }

interface AiTarget {
  mode: 'selection' | 'cursor'
  text: string
  range: Range
  before: string
  after: string
}

type Phase = 'idle' | 'loading' | 'preview' | 'applied' | 'error'

interface AiMeta {
  model: string
  degraded: boolean
  status: string
  label: string
  note: string
}

function paragraphs(container: HTMLElement): HTMLElement[] {
  return Array.from(container.children).filter(
    el => el.nodeType === Node.ELEMENT_NODE && PARA_TAGS.includes((el as HTMLElement).tagName),
  ) as HTMLElement[]
}

function findParagraph(container: HTMLElement, node: Node | null): HTMLElement | null {
  while (node && node !== container) {
    if (node.nodeType === Node.ELEMENT_NODE && PARA_TAGS.includes((node as HTMLElement).tagName)) {
      return node as HTMLElement
    }
    node = node.parentNode
  }
  return null
}

/** 基于 DOM 计算上下文：选区取前后各 2 段；光标取段内前后文 + 前后各 2 段。 */
function collectContext(container: HTMLElement, range: Range, mode: 'selection' | 'cursor') {
  const all = paragraphs(container)
  const anchor = findParagraph(container, range.startContainer)
  const idx = anchor ? all.indexOf(anchor) : -1
  const prev = idx > 0 ? all.slice(Math.max(0, idx - 2), idx).map(p => p.textContent || '') : []
  const next = idx >= 0 ? all.slice(idx + 1, idx + 3).map(p => p.textContent || '') : []
  if (mode === 'selection' || !anchor) {
    return { before: prev.join('\n\n'), after: next.join('\n\n') }
  }
  let currentBefore = ''
  let currentAfter = ''
  try {
    const pre = document.createRange()
    pre.selectNodeContents(anchor)
    pre.setEnd(range.startContainer, range.startOffset)
    currentBefore = pre.toString()
  } catch { /* 光标不在该段落内：退化为整段 */ }
  try {
    const post = document.createRange()
    post.selectNodeContents(anchor)
    post.setStart(range.startContainer, range.startOffset)
    currentAfter = post.toString()
  } catch { /* 同上 */ }
  return {
    before: [...prev, currentBefore].filter(Boolean).join('\n\n'),
    after: [currentAfter, ...next].filter(Boolean).join('\n\n'),
  }
}

export function MdView({ text, editable, onInput, onCommit }: {
  text: string
  editable?: boolean
  onInput?: (md: string) => void
  /** 采纳 AI 候选后自动落盘；不传时退回「仅改 DOM、需手动保存」。 */
  onCommit?: (md: string) => Promise<void> | void
}) {
  const ref = useRef<HTMLDivElement>(null)
  const knownText = useRef(text)
  const toolbarRef = useRef<HTMLDivElement>(null)

  const [toolbar, setToolbar] = useState(HIDDEN)
  const [target, setTarget] = useState<AiTarget | null>(null)
  const [phase, setPhase] = useState<Phase>('idle')
  const phaseRef = useRef<Phase>('idle')
  const [candidates, setCandidates] = useState<EditorAiCandidate[]>([])
  const [activeIndex, setActiveIndex] = useState(0)
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())
  const [meta, setMeta] = useState<AiMeta | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [showCustom, setShowCustom] = useState(false)
  const [customText, setCustomText] = useState('')
  const [legacyMode, setLegacyMode] = useState<boolean | null>(null)
  const [undoMd, setUndoMd] = useState<string | null>(null)
  const lastRequest = useRef<{ op: string; instruction?: string } | null>(null)

  useEffect(() => { phaseRef.current = phase }, [phase])

  // 编辑模式：仅当外部 text 与已知文本不同时（切章节/加载/撤销）才重设 DOM
  useEffect(() => {
    if (!editable || !ref.current) return
    if (text !== knownText.current) {
      ref.current.innerHTML = mdToHtml(text)
      knownText.current = text
    }
  }, [text, editable])

  // 阅读 ↔ 编辑 模式切换时，把当前文本同步进 DOM
  useEffect(() => {
    if (ref.current) {
      ref.current.innerHTML = mdToHtml(text)
      knownText.current = text
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editable])

  // 探测新网关是否挂载（NWW_EDITOR_AI=0 时 404 → 显式回落旧端点）
  useEffect(() => {
    if (!editable) return
    let cancelled = false
    api.editorAiHealth()
      .then(() => { if (!cancelled) setLegacyMode(false) })
      .catch(() => { if (!cancelled) setLegacyMode(true) })
    return () => { cancelled = true }
  }, [editable])

  const dismiss = useCallback((clearTarget = true) => {
    setToolbar(HIDDEN)
    setPhase('idle')
    setCandidates([])
    setMeta(null)
    setError(null)
    setShowCustom(false)
    setCustomText('')
    lastRequest.current = null
    if (clearTarget) setTarget(null)
  }, [])

  // 监听 selectionchange：选区浮出改写条，折叠光标浮出续写条
  useEffect(() => {
    if (!editable) return
    const handleSelectionChange = () => {
      // 请求中/预览中不重置目标——点击工具条或输入框导致选区变化时不能丢 Range
      if (phaseRef.current !== 'idle') return
      const container = ref.current
      const sel = window.getSelection()
      if (!container || !sel || sel.rangeCount === 0) { dismiss(); return }
      const active = document.activeElement
      if (active && active !== container && !container.contains(active)
        && !toolbarRef.current?.contains(active)) return
      const range = sel.getRangeAt(0)
      if (!container.contains(range.commonAncestorContainer)) { dismiss(); return }
      const mode: AiTarget['mode'] = sel.isCollapsed ? 'cursor' : 'selection'
      const selectedText = range.toString().trim()
      if (mode === 'selection' && !selectedText) { dismiss(); return }
      const ctx = collectContext(container, range, mode)
      if (mode === 'cursor' && !ctx.before && !ctx.after) { dismiss(); return }

      const rect = range.getBoundingClientRect()
      const containerRect = container.getBoundingClientRect()
      const toolbarHeight = 44
      const spaceBelow = window.innerHeight - rect.bottom
      const top = spaceBelow >= toolbarHeight + 40
        ? rect.bottom - containerRect.top + 8
        : rect.top - containerRect.top - toolbarHeight - 8
      const left = Math.min(
        Math.max(rect.left - containerRect.left, 8),
        Math.max(containerRect.width - 430, 8),
      )
      setTarget({ mode, text: selectedText, range: range.cloneRange(), ...ctx })
      setToolbar({ visible: true, top: Math.max(top, 8), left })
    }
    document.addEventListener('selectionchange', handleSelectionChange)
    return () => document.removeEventListener('selectionchange', handleSelectionChange)
  }, [editable, dismiss])

  // Esc 关闭（保留 undoMd，撤销条仍可用）
  useEffect(() => {
    if (phase === 'idle') return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') dismiss() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [phase, dismiss])

  const handlePaste = useCallback((e: ReactClipboardEvent) => {
    e.preventDefault()
    document.execCommand('insertText', false, e.clipboardData.getData('text/plain'))
  }, [])

  const handleInput = useCallback(() => {
    if (!ref.current || !onInput) return
    const md = htmlToMd(ref.current)
    knownText.current = md
    onInput(md)
  }, [onInput])

  const runAi = useCallback(async (op: string, instruction?: string) => {
    if (!target) return
    lastRequest.current = { op, instruction }
    setPhase('loading')
    setError(null)
    setShowCustom(false)
    const base = { before_context: target.before, after_context: target.after }
    let result: EditorAiResult
    try {
      if (target.mode === 'selection') {
        const body = {
          ...base,
          selected_text: target.text,
          operation: op,
          custom_instruction: instruction ?? '',
        }
        if (legacyMode === true) {
          const legacy = await api.editorRewriteLegacy(body)
          if (legacy.error || !legacy.candidate) throw new Error(legacy.error || '旧端点未返回候选')
          result = {
            ok: true, status: 'degraded', mode: 'revision', operation: op,
            operation_label: '改写', original_text: target.text, degraded: true,
            candidates: [{ id: 'legacy-1', label: '候选 1', content: legacy.candidate }],
          }
        } else {
          result = await api.editorAiRevision(body)
        }
      } else {
        if (legacyMode === true) throw new Error('当前后端未启用编辑器 AI 网关，光标续写不可用（NWW_EDITOR_AI=0）')
        result = await api.editorAiContinuation({ ...base, operation: op, instruction: instruction ?? '' })
      }
      if (!result.ok || !result.candidates?.length) {
        throw new Error(result.error || 'AI 未返回可用候选')
      }
      setCandidates(result.candidates)
      setActiveIndex(0)
      setSelectedIds(defaultSelectedChangeIds((result.candidates[0].diff_chunks ?? []) as DiffChunk[]))
      setMeta({
        model: result.model || '',
        degraded: !!result.degraded,
        status: result.status || '',
        label: result.operation_label || 'AI 结果',
        note: legacyMode === true ? '已回落到旧单候选端点（无逐字 diff）'
          : result.degraded ? '模型未按结构化格式返回，已降级为单候选' : '',
      })
      setPhase('preview')
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e))
      setPhase('error')
    }
  }, [target, legacyMode])

  const selectCandidate = useCallback((index: number) => {
    setActiveIndex(index)
    setSelectedIds(defaultSelectedChangeIds((candidates[index]?.diff_chunks ?? []) as DiffChunk[]))
  }, [candidates])

  const toggleChange = useCallback((id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id); else next.add(id)
      return next
    })
  }, [])

  const accept = useCallback(async () => {
    const cand = candidates[activeIndex]
    if (!cand || !target || !ref.current || !onInput) return
    const chunks = (cand.diff_chunks ?? []) as DiffChunk[]
    let replacement = cand.content
    if (chunks.length) {
      try {
        replacement = applySelectedDiff(target.text, cand.content, chunks, selectedIds)
      } catch (e) {
        setError(String(e instanceof Error ? e.message : e))
        setPhase('error')
        return
      }
    }
    const beforeMd = htmlToMd(ref.current)
    try {
      const range = target.range
      if (target.mode === 'selection') range.deleteContents()
      range.insertNode(document.createTextNode(replacement))
      handleInput()
      const md = knownText.current
      if (onCommit) await onCommit(md)
      setUndoMd(beforeMd)
      setError(null)
      setPhase('applied')
    } catch (e) {
      setError(`写入/保存失败：${String(e instanceof Error ? e.message : e)}`)
      setPhase('error')
    }
  }, [candidates, activeIndex, target, selectedIds, onInput, onCommit, handleInput])

  const undo = useCallback(async () => {
    if (!undoMd || !ref.current) return
    try {
      ref.current.innerHTML = mdToHtml(undoMd)
      knownText.current = undoMd
      onInput?.(undoMd)
      if (onCommit) await onCommit(undoMd)
      setUndoMd(null)
      dismiss()
    } catch (e) {
      setError(`撤销失败：${String(e instanceof Error ? e.message : e)}`)
      setPhase('error')
    }
  }, [undoMd, onInput, onCommit, dismiss])

  const handleKeyDown = useCallback((e: ReactKeyboardEvent) => {
    // Ctrl+A / Cmd+A 只选中编辑器内内容，不选全页面
    if ((e.ctrlKey || e.metaKey) && e.key === 'a') {
      e.preventDefault()
      e.stopPropagation()
      const el = ref.current
      if (el) {
        const range = document.createRange()
        range.selectNodeContents(el)
        const sel = window.getSelection()
        if (sel) { sel.removeAllRanges(); sel.addRange(range) }
      }
    }
  }, [])

  if (!editable) {
    return (
      <div ref={ref} tabIndex={0} onKeyDown={handleKeyDown}
        className={PROSE_STYLE + ' outline-none focus:outline-none focus:ring-0'}
        dangerouslySetInnerHTML={{ __html: mdToHtml(text) }} />
    )
  }

  const ops = target?.mode === 'cursor' ? CURSOR_OPERATIONS : REVISION_OPERATIONS
  const active = candidates[activeIndex]
  const changes = groupChanges((active?.diff_chunks ?? []) as DiffChunk[])
  const panelStyle = {
    top: toolbar.top,
    left: Math.min(toolbar.left, Math.max((ref.current?.clientWidth || 400) - 380, 8)),
  }

  return (
    <div className="relative">
      <div ref={ref} contentEditable suppressContentEditableWarning spellCheck={false}
        onInput={handleInput} onPaste={handlePaste} onKeyDown={handleKeyDown}
        style={{ outline: 'none', boxShadow: 'none' }}
        className={PROSE_STYLE + ' outline-none focus:outline-none focus:ring-0 caret-[#f59e0b]'} />

      {/* 选段 / 光标 AI 浮动工具条 */}
      {toolbar.visible && (phase === 'idle' || phase === 'loading' || phase === 'error') && (
        <div
          ref={toolbarRef}
          className="absolute z-30 rounded-xl border border-[#3a3a3a] bg-[#1e1e1e]/95 backdrop-blur shadow-lg shadow-black/40 p-1.5 flex flex-wrap items-center gap-1 max-w-[420px]"
          style={{ top: toolbar.top, left: toolbar.left }}
          onMouseDown={(e) => {
            // 防止点击按钮导致编辑器失焦、DOM 选区被清空
            if ((e.target as HTMLElement).closest('button')) e.preventDefault()
          }}
        >
          {ops.map(op => (
            <button
              key={op.key}
              onClick={() => runAi(op.key)}
              disabled={phase === 'loading'}
              className={`px-2.5 py-1 rounded-full text-[13px] transition-colors ${
                op.key === 'polish' || op.key === 'continue'
                  ? 'bg-[#f59e0b] text-black font-medium hover:bg-[#fbbf24]'
                  : 'bg-[#2a2a2a] text-[#ccc] hover:bg-[#3a3a3a] hover:text-white'
              } disabled:opacity-50 disabled:cursor-not-allowed`}
            >
              {op.label}
            </button>
          ))}
          {target?.mode === 'selection' && (
            <button
              onClick={() => runAi('custom', customText.trim() || undefined)}
              disabled={phase === 'loading'}
              className="px-2.5 py-1 rounded-full text-[13px] bg-[#2a2a2a] text-[#ccc] hover:bg-[#3a3a3a] hover:text-white transition-colors disabled:opacity-50"
            >
              按要求改…
            </button>
          )}
          <button
            onClick={() => setShowCustom(v => !v)}
            disabled={phase === 'loading'}
            className="px-2.5 py-1 rounded-full text-[13px] bg-[#2a2a2a] text-[#ccc] hover:bg-[#3a3a3a] hover:text-white transition-colors disabled:opacity-50"
          >
            补充要求…
          </button>

          {showCustom && (
            <div className="w-full flex items-center gap-1 mt-1">
              <input
                autoFocus
                value={customText}
                onChange={(e) => setCustomText(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && customText.trim()) {
                    e.preventDefault()
                    runAi(target?.mode === 'selection' ? 'custom' : 'continue', customText.trim())
                  }
                  if (e.key === 'Escape') { e.stopPropagation(); setShowCustom(false) }
                }}
                placeholder={target?.mode === 'cursor' ? '说一句续写要求，回车生成' : '说一句你想怎么改，回车生成'}
                maxLength={400}
                className="flex-1 h-7 px-2.5 rounded-full bg-[#252525] border border-[#3a3a3a] text-[13px] text-white outline-none focus:border-[#f59e0b]"
              />
              <button
                onClick={() => customText.trim() && runAi(target?.mode === 'selection' ? 'custom' : 'continue', customText.trim())}
                disabled={phase === 'loading' || !customText.trim()}
                className="h-7 px-3 rounded-full bg-[#f59e0b] text-black text-[13px] font-medium hover:bg-[#fbbf24] disabled:opacity-50"
              >
                生成
              </button>
            </div>
          )}

          {phase === 'loading' && (
            <div className="w-full text-center text-[12px] text-[#888] py-1">AI 正在生成候选…</div>
          )}
          {phase === 'error' && error && (
            <div className="w-full text-[12px] text-red-400 py-1 break-words">
              {error}
              <button onClick={() => setPhase('idle')} className="ml-2 underline text-[#aaa]">重试</button>
            </div>
          )}
          {legacyMode === true && (
            <div className="w-full text-[11px] text-amber-400/90 py-1">
              已回落到旧单候选端点（NWW_EDITOR_AI=0），续写与逐字 diff 不可用
            </div>
          )}
        </div>
      )}

      {/* 候选预览：切换候选 / 逐字 diff 勾选 / 再生成 / 采纳 */}
      {(phase === 'preview' || phase === 'error') && candidates.length > 0 && (
        <div
          className="absolute z-30 rounded-xl border border-[#3a3a3a] bg-[#1e1e1e]/95 backdrop-blur shadow-lg shadow-black/40 p-3 w-[360px] max-h-[420px] overflow-y-auto"
          style={panelStyle}
          onMouseDown={(e) => e.preventDefault()}
        >
          <div className="flex items-center justify-between mb-2">
            <div className="text-[12px] text-[#888]">{meta?.label || 'AI 候选'} · {candidates.length} 个</div>
            {meta?.model && <div className="text-[11px] text-[#666] truncate max-w-[170px]">{meta.model}</div>}
          </div>
          <div className="flex flex-wrap gap-1 mb-2">
            {candidates.map((c, i) => (
              <button key={c.id} onClick={() => selectCandidate(i)}
                className={`px-2 py-1 rounded text-[12px] ${i === activeIndex ? 'bg-[#f59e0b] text-black' : 'bg-[#2a2a2a] text-[#ccc]'}`}>
                {c.label || `候选 ${i + 1}`}
              </button>
            ))}
          </div>

          {error && <div className="text-[12px] text-red-400 mb-2 break-words">{error}</div>}
          {meta?.note && <div className="text-[11px] text-amber-400/90 mb-2">{meta.note}</div>}

          <div className="rounded-lg bg-[#252525] border border-[#3a3a3a] p-2.5 mb-2 max-h-[160px] overflow-y-auto text-[13px] leading-relaxed">
            {active?.diff_chunks?.length ? (
              active.diff_chunks.map(chunk => (
                <span key={chunk.id} className={
                  chunk.type === 'insert' ? 'bg-emerald-900/50 text-emerald-200 rounded-sm'
                    : chunk.type === 'delete' ? 'bg-red-900/40 text-red-300 line-through rounded-sm'
                      : 'text-[#ddd]'
                }>{chunk.text}</span>
              ))
            ) : (
              <span className="text-emerald-200/90 whitespace-pre-wrap">{active?.content}</span>
            )}
          </div>

          {changes.length > 0 && (
            <div className="mb-2">
              <div className="flex items-center justify-between text-[11px] text-[#888] mb-1">
                <span>逐条采纳（默认全选）</span>
                <span className="flex gap-2">
                  <button className="hover:text-white" onClick={() => setSelectedIds(new Set(changes.map(c => c.id)))}>全选</button>
                  <button className="hover:text-white" onClick={() => setSelectedIds(new Set())}>全不选</button>
                </span>
              </div>
              <div className="max-h-[110px] overflow-y-auto space-y-1">
                {changes.map(change => (
                  <label key={change.id} className="flex items-start gap-1.5 text-[11px] text-[#bbb] cursor-pointer">
                    <input type="checkbox" className="mt-0.5 accent-[#f59e0b]"
                      checked={selectedIds.has(change.id)} onChange={() => toggleChange(change.id)} />
                    <span className="break-all">
                      <span className="text-red-300/80">{change.originalText.slice(0, 30) || '（新增）'}</span>
                      {' → '}
                      <span className="text-emerald-300/90">{change.candidateText.slice(0, 30) || '（删除）'}</span>
                    </span>
                  </label>
                ))}
              </div>
            </div>
          )}

          <div className="flex items-center justify-between gap-2">
            <button onClick={() => dismiss()} className="px-3 py-1.5 rounded-lg text-[13px] bg-[#2a2a2a] text-[#ccc] hover:bg-[#3a3a3a]">放弃</button>
            <div className="flex gap-2">
              <button onClick={() => lastRequest.current && runAi(lastRequest.current.op, lastRequest.current.instruction)}
                className="px-3 py-1.5 rounded-lg text-[13px] bg-[#2a2a2a] text-[#ccc] hover:bg-[#3a3a3a]">再生成</button>
              <button onClick={accept} disabled={selectedIds.size === 0 && !!active?.diff_chunks?.length}
                className="px-4 py-1.5 rounded-lg text-[13px] bg-emerald-600 text-white font-medium hover:bg-emerald-500 disabled:opacity-50">
                {onCommit ? '接受并保存' : '接受并写入'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 已采纳：自动保存结果 + 一次性撤销 */}
      {phase === 'applied' && (
        <div className="absolute z-30 rounded-xl border border-emerald-900/60 bg-[#1e1e1e]/95 backdrop-blur shadow-lg p-3 w-[360px]"
          style={panelStyle} onMouseDown={(e) => e.preventDefault()}>
          <div className="text-[13px] text-emerald-300 mb-2">
            {onCommit ? '已采纳并自动保存' : '已采纳，请手动保存'}{meta?.model ? ` · ${meta.model}` : ''}
          </div>
          <div className="flex items-center justify-between gap-2">
            <button onClick={undo} className="px-3 py-1.5 rounded-lg text-[13px] bg-[#2a2a2a] text-[#ccc] hover:bg-[#3a3a3a]">撤销本次采纳</button>
            <button onClick={() => dismiss()} className="px-4 py-1.5 rounded-lg text-[13px] bg-emerald-600 text-white hover:bg-emerald-500">完成</button>
          </div>
        </div>
      )}

      {/* 关闭面板后仍保留一次性撤销入口 */}
      {phase === 'idle' && undoMd && (
        <div className="mt-2 flex items-center gap-2 text-[12px] text-[#888]">
          <span>上次 AI 采纳可撤销</span>
          <button onClick={undo} className="px-2 py-1 rounded border border-[#3a3a3a] hover:text-white hover:border-[#f59e0b]">撤销</button>
        </div>
      )}
    </div>
  )
}
