import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ChevronDown as ChevronDownIcon } from 'lucide-react'
import { api } from '../api'
import { countVisibleChars } from '../utils/wordcount'
import ScrollArea from './ScrollArea'
import { parseChapterName, scoreTone, formatScore, fullMtime } from './AssetTree'
import PhonePreview from './PhonePreview'
import PrevTailHook from './PrevTailHook'
import { MdView } from './md'

interface Props {
  bookId: string
  path?: string
  readFirst?: boolean
  onNavigate?: (path: string) => void
  onDeleted?: () => void
  onSaved?: () => void
  /** 外部请求重载当前文件：快捷「优化/重写」完成后 App 递增 refreshKey 触发（放弃本地未保存修改） */
  refreshKey?: number
  /** 编辑器脏状态上报：未保存修改时快捷「优化/重写」应禁用，防止后台落盘覆盖丢稿 */
  onDirtyChange?: (dirty: boolean) => void
}

type Mode = 'read' | 'edit'
type ExportFormat = 'md' | 'txt'
type ExportScope = 'current' | 'book' | 'range'

/* ─────────────────────  图标  ───────────────────── */
const Icon = {
  Eye: () => (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  ),
  Pencil: () => (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 20h9" />
      <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" />
    </svg>
  ),
  Copy: () => (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
      <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
    </svg>
  ),
  Download: () => (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
      <polyline points="7 10 12 15 17 10" />
      <line x1="12" y1="15" x2="12" y2="3" />
    </svg>
  ),
  Save: () => (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z" />
      <polyline points="17 21 17 13 7 13 7 21" />
      <polyline points="7 3 7 8 15 8" />
    </svg>
  ),
  FileText: () => (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <polyline points="14 2 14 8 20 8" />
      <line x1="16" y1="13" x2="8" y2="13" />
      <line x1="16" y1="17" x2="8" y2="17" />
    </svg>
  ),
  Check: () => (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="20 6 9 17 4 12" />
    </svg>
  ),
  ChevronDown: () => (
    <ChevronDownIcon className="h-3 w-3" strokeWidth={1.8} aria-hidden />
  ),
  ChevronLeft: () => (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="15 18 9 12 15 6" />
    </svg>
  ),
  ChevronRight: () => (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="9 18 15 12 9 6" />
    </svg>
  ),
  Bold: () => (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M6 4h8a4 4 0 0 1 4 4 4 4 0 0 1-4 4H6z" />
      <path d="M6 12h9a4 4 0 0 1 4 4 4 4 0 0 1-4 4H6z" />
    </svg>
  ),
  Italic: () => (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
      <line x1="19" y1="4" x2="10" y2="4" />
      <line x1="14" y1="20" x2="5" y2="20" />
      <line x1="15" y1="4" x2="9" y2="20" />
    </svg>
  ),
  Underline: () => (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M6 3v7a6 6 0 0 0 6 6 6 6 0 0 0 6-6V3" />
      <line x1="4" y1="21" x2="20" y2="21" />
    </svg>
  ),
  Type: () => (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="4 7 4 4 20 4 20 7" />
      <line x1="9" y1="20" x2="15" y2="20" />
      <line x1="12" y1="4" x2="12" y2="20" />
    </svg>
  ),
  Trash: () => (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 6h18" />
      <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" />
      <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
    </svg>
  ),
  Phone: () => (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <rect x="6" y="2" width="12" height="20" rx="2.5" />
      <line x1="10.5" y1="18.5" x2="13.5" y2="18.5" />
    </svg>
  ),
}

/* ─────────────────────  字体选项  ───────────────────── */
const FONT_OPTIONS = [
  { label: '系统默认', value: '' },
  { label: '等线体', value: '"DengXian", "PingFang SC", "Microsoft YaHei", sans-serif' },
  { label: '宋体', value: '"SimSun", "Songti SC", "STSong", serif' },
  { label: '黑体', value: '"SimHei", "Heiti SC", "STHeiti", sans-serif' },
  { label: '楷体', value: '"KaiTi", "Kaiti SC", "STKaiti", serif' },
  { label: '仿宋', value: '"FangSong", "STFangsong", serif' },
  { label: '微软雅黑', value: '"Microsoft YaHei", "PingFang SC", sans-serif' },
  { label: '苹方', value: '"PingFang SC", "Helvetica Neue", sans-serif' },
]


/* ─────────────────────  章节标题工具（文件名 ↔ 标题） ───────────────────── */

/** 由原文件名推导新文件名：保留原章号写法（含零填充）与分隔符，只替换标题部分。
 *  非章节命名（不匹配 第N章）返回 null → 不允许改名，避免破坏导航/排序规则。 */
function buildChapterFileName(name: string, newTitle: string): string | null {
  const m = name.match(/^(第0*\d+章)([_\s　-]*)(.+?)(?:[_\s-]*\d+\s*字)?\.(md|txt)$/)
  if (!m) return null
  const [, prefix, sep, , ext] = m
  const safe = newTitle.replace(/[\\/:*?"<>|]/g, '_').trim()
  if (!safe) return null
  return `${prefix}${sep || '_'}${safe}.${ext}`
}

/** 把正文首个标题行的「标题部分」替换为新标题：保留 # 级数、章号写法与分隔符。
 *  文件没有标题行时不擅自插入（避免改变正文结构），返回原样。 */
function replaceChapterTitleInMd(md: string, newTitle: string): string {
  const lines = md.split('\n')
  const RE = /^(\s*#{1,3}\s*第\s*0*\d+\s*章[\s　]*)(.*)$/
  for (let i = 0; i < lines.length; i++) {
    if (RE.test(lines[i])) {
      lines[i] = lines[i].replace(RE, (_m, head: string) => head + newTitle)
      return lines.join('\n')
    }
  }
  return md
}

/* ─────────────────────  章节头部标注（标题 + 健康分 / 落盘时间） ───────────────────── */
function ChapterHeader({
  path, score, mtime, editable, onCommitTitle, notice,
}: {
  path: string
  score?: number
  mtime?: number
  /** 编辑态：标题部分变为输入框（章号固定，避免打乱排序/导航） */
  editable?: boolean
  onCommitTitle?: (title: string) => void | Promise<void>
  notice?: string | null
}) {
  const name = path.split('/').pop() ?? ''
  const parsed = parseChapterName(name)
  const numLabel = parsed ? `第${parseInt(parsed.num, 10)}章` : ''
  const titleText = parsed ? parsed.title : name
  const [draft, setDraft] = useState(titleText)
  const [busy, setBusy] = useState(false)
  // 切章节 / 改名后，外部值变化 → 同步输入框
  useEffect(() => { setDraft(titleText) }, [titleText])

  // 只有标准章节命名（第N章）才开放编辑
  const canEdit = !!editable && !!parsed

  const commit = async () => {
    const next = draft.trim()
    if (!canEdit || busy) return
    if (!next || next === titleText) { setDraft(titleText); return }
    setBusy(true)
    try { await onCommitTitle?.(next) } finally { setBusy(false) }
  }

  return (
    <div className="mb-7">
      <div className="flex items-baseline gap-2">
        {numLabel && (
          <span className="text-[1.5em] font-semibold text-[#e0e0e0] select-none shrink-0">{numLabel}</span>
        )}
        {canEdit ? (
          <input
            value={draft}
            disabled={busy}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={commit}
            onKeyDown={(e) => {
              if (e.key === 'Enter') { e.preventDefault(); (e.target as HTMLInputElement).blur() }
              else if (e.key === 'Escape') { setDraft(titleText); (e.target as HTMLInputElement).blur() }
            }}
            placeholder="章节标题"
            title="回车或失焦保存；Esc 取消"
            className="flex-1 min-w-0 bg-transparent border-b border-dashed border-[#3a3a3a] hover:border-[#555] focus:border-[#f59e0b] outline-none text-[1.5em] font-semibold text-[#e0e0e0] pb-0.5 transition-colors disabled:opacity-50"
          />
        ) : (
          <h1 className="text-[1.5em] font-semibold text-[#e0e0e0] select-none">{titleText}</h1>
        )}
      </div>
      <div className="mt-1 flex items-center gap-2 text-[11px] text-[#777] tabular-nums">
        {score != null && (
          <span className="font-medium" style={{ color: scoreTone(score) }}>
            健康 {formatScore(score)}
          </span>
        )}
        {mtime != null && <span>{fullMtime(mtime)}</span>}
        {score == null && mtime == null && <span className="text-[#555]">未审稿</span>}
        {notice && <span className={notice.startsWith('标题保存失败') ? 'text-red-400' : 'text-[#f59e0b]'}>{notice}</span>}
      </div>
    </div>
  )
}

/* ─────────────────────  导出弹窗  ───────────────────── */
function ExportMenu({
  bookId, currentPath, content, onClose,
}: {
  bookId: string
  currentPath: string
  content: string
  onClose: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [done, setDone] = useState<string | null>(null)

  const download = (filename: string, text: string) => {
    const blob = new Blob([text], { type: 'text/plain;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  }

  const [startChapter, setStartChapter] = useState('')
  const [endChapter, setEndChapter] = useState('')

  const getSortedChapters = async () => {
    const tree = await api.tree(bookId)
    const bodySection = tree.sections.find((s) => s.section === '正文')
    // 只保留“第N章”命名的真实章节文件，过滤 .DS_Store 等垃圾文件
    const files = (bodySection?.files ?? []).filter((f) => /^第0*\d+章/.test(f.name))
    const sorted = [...files].sort((a, b) => {
      const na = a.name.match(/^第0*(\d+)章/)!
      const nb = b.name.match(/^第0*(\d+)章/)!
      return parseInt(na[1], 10) - parseInt(nb[1], 10)
    })
    return { tree, sorted }
  }

  const doExport = async (scope: ExportScope, format: ExportFormat) => {
    setBusy(true)
    setDone(null)
    try {
      if (scope === 'current') {
        const name = currentPath.split('/').pop()?.replace(/\.(md|txt)$/, '') || 'chapter'
        download(`${name}.${format}`, content)
        setDone(`已导出 ${name}.${format}`)
        return
      }

      const { tree, sorted } = await getSortedChapters()
      let targetFiles = sorted
      let label = `整本书 ${sorted.length} 章`

      if (scope === 'range') {
        const start = parseInt(startChapter, 10)
        const end = parseInt(endChapter, 10)
        if (isNaN(start) || isNaN(end) || start < 1 || end < start) {
          setDone('请输入有效的章节范围（如 1 到 4）')
          setBusy(false)
          return
        }
        targetFiles = sorted.filter((f) => {
          const m = f.name.match(/^第0*(\d+)章/)
          if (!m) return false
          const n = parseInt(m[1], 10)
          return n >= start && n <= end
        })
        if (targetFiles.length === 0) {
          setDone(`第 ${start}-${end} 章范围内没有找到章节`)
          setBusy(false)
          return
        }
        label = `第 ${start}-${end} 章 共 ${targetFiles.length} 章`
      }

      const parts: string[] = []
      for (const f of targetFiles) {
        const meta = await api.file(bookId, f.path)
        if (meta.content != null) {
          parts.push(`# ${f.name}\n\n${meta.content}`)
        }
      }
      const full = parts.join('\n\n---\n\n')
      const bookTitle = tree.title || 'book'
      let suffix = ''
      if (scope === 'book') {
        suffix = `_全${sorted.length}章`
      } else if (scope === 'range') {
        suffix = `_第${startChapter}-${endChapter}章`
      }
      download(`${bookTitle}${suffix}.${format}`, full)
      setDone(`已导出 ${label} → ${bookTitle}${suffix}.${format}`)
    } catch (e) {
      setDone(`导出失败：${String(e)}`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="absolute top-full mt-2 right-0 z-30 w-64 rounded-lg border border-[#3a3a3a] bg-[#1e1e1e] shadow-xl shadow-black/50 overflow-hidden">
      <div className="px-3 py-2 border-b border-[#2a2a2a] text-[14px] font-medium text-[#ccc]">导出</div>
      <div className="py-1">
        <div className="px-3 py-1 text-[14px] uppercase tracking-wider text-[#666]">当前章节</div>
        <button onClick={() => doExport('current', 'md')} disabled={busy}
          className="w-full text-left px-3 py-1.5 text-[14px] text-[#bbb] hover:bg-[#2a2a2a] disabled:opacity-40 transition-colors">
          导出为 Markdown (.md)
        </button>
        <button onClick={() => doExport('current', 'txt')} disabled={busy}
          className="w-full text-left px-3 py-1.5 text-[14px] text-[#bbb] hover:bg-[#2a2a2a] disabled:opacity-40 transition-colors">
          导出为纯文本 (.txt)
        </button>
        <div className="border-t border-[#2a2a2a] my-1" />
        <div className="px-3 py-1 text-[14px] uppercase tracking-wider text-[#666]">整本书</div>
        <button onClick={() => doExport('book', 'md')} disabled={busy}
          className="w-full text-left px-3 py-1.5 text-[14px] text-[#bbb] hover:bg-[#2a2a2a] disabled:opacity-40 transition-colors">
          整合全部章节 (.md)
        </button>
        <button onClick={() => doExport('book', 'txt')} disabled={busy}
          className="w-full text-left px-3 py-1.5 text-[14px] text-[#bbb] hover:bg-[#2a2a2a] disabled:opacity-40 transition-colors">
          整合全部章节 (.txt)
        </button>

        <div className="border-t border-[#2a2a2a] my-1" />
        <div className="px-3 py-1 text-[14px] uppercase tracking-wider text-[#666]">按章节范围导出</div>
        <div className="px-3 py-2 flex items-center gap-2">
          <input
            type="number" min={1} placeholder="起始"
            value={startChapter}
            onChange={(e) => setStartChapter(e.target.value)}
            className="w-full bg-[#1a1a1a] border border-[#333] rounded-md px-2 py-1 text-[14px] text-[#ccc] placeholder:text-[#555] outline-none focus:border-[#f59e0b] transition-colors"
          />
          <span className="text-[14px] text-[#666]">到</span>
          <input
            type="number" min={1} placeholder="结束"
            value={endChapter}
            onChange={(e) => setEndChapter(e.target.value)}
            className="w-full bg-[#1a1a1a] border border-[#333] rounded-md px-2 py-1 text-[14px] text-[#ccc] placeholder:text-[#555] outline-none focus:border-[#f59e0b] transition-colors"
          />
        </div>
        <div className="px-3 pb-1 flex gap-2">
          <button onClick={() => doExport('range', 'md')} disabled={busy}
            className="flex-1 text-center px-2 py-1.5 text-[14px] text-[#bbb] hover:bg-[#2a2a2a] disabled:opacity-40 rounded-md border border-[#333] transition-colors">
            导出 .md
          </button>
          <button onClick={() => doExport('range', 'txt')} disabled={busy}
            className="flex-1 text-center px-2 py-1.5 text-[14px] text-[#bbb] hover:bg-[#2a2a2a] disabled:opacity-40 rounded-md border border-[#333] transition-colors">
            导出 .txt
          </button>
        </div>
      </div>
      {done && <div className="px-3 py-2 border-t border-[#2a2a2a] text-[14px] text-[#f59e0b]">{done}</div>}
      <button onClick={onClose} className="w-full px-3 py-1.5 text-[14px] text-[#666] hover:text-[#999] border-t border-[#2a2a2a] transition-colors">
        关闭
      </button>
    </div>
  )
}

/* ─────────────────────  悬浮工具栏（全图标加长版）  ───────────────────── */
function FloatingToolbar({
  mode, setMode, fontSize, setFontSize, fontFamily, setFontFamily,
  onSave, dirty, saving, saveMsg,
  readonly, chars, content, bookId, path,
  onPrev, onNext, hasPrev, hasNext,
  onDelete, deleting, deleteError, setDeleteError,
  mobileView, onToggleMobile,
}: {
  mode: Mode
  setMode: (m: Mode) => void
  fontSize: number
  setFontSize: (n: number) => void
  fontFamily: string
  setFontFamily: (f: string) => void
  onSave: () => void
  dirty: boolean
  saving: boolean
  saveMsg: string | null
  readonly: boolean
  chars: number
  content: string
  bookId: string
  path: string
  onPrev: () => void
  onNext: () => void
  hasPrev: boolean
  hasNext: boolean
  onDelete: () => void
  deleting: boolean
  deleteError: string | null
  setDeleteError: (e: string | null) => void
  mobileView: boolean
  onToggleMobile: () => void
}) {
  const [copied, setCopied] = useState(false)
  const [exportOpen, setExportOpen] = useState(false)
  const [deleteConfirm, setDeleteConfirm] = useState(false)
  const exportRef = useRef<HTMLDivElement>(null)

  // 点击外部关闭导出菜单
  useEffect(() => {
    if (!exportOpen) return
    const handler = (e: MouseEvent) => {
      if (exportRef.current && !exportRef.current.contains(e.target as Node)) setExportOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [exportOpen])

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(content)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      // fallback
      const ta = document.createElement('textarea')
      ta.value = content
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    }
  }

  const tbBtn = "w-8 h-8 flex items-center justify-center rounded-full text-[#b0b0b0] hover:bg-[#333] hover:text-white transition-colors"
  const tbBtnActive = "bg-[#3a3a3a] text-white"

  return (
    <div className="flex items-center justify-center">
      <div className="flex items-center gap-0.5 px-3 py-1.5 rounded-full bg-[#1a1a1a]/92 backdrop-blur border border-[#3a3a3a] shadow-lg shadow-black/40">
        {/* 字号控制：A− [滑块] A+ */}
        <button onClick={() => setFontSize(Math.max(12, fontSize - 1))} className={tbBtn} title="减小字号">
          <span className="text-[14px] font-bold">A−</span>
        </button>
        <input
          type="range" min={12} max={28} value={fontSize}
          onChange={(e) => setFontSize(parseInt(e.target.value, 10))}
          className="w-16 h-1 mx-1 appearance-none bg-[#3a3a3a] rounded-full cursor-pointer [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:w-3 [&::-webkit-slider-thumb]:h-3 [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-[#f59e0b]"
        />
        <button onClick={() => setFontSize(Math.min(28, fontSize + 1))} className={tbBtn} title="增大字号">
          <span className="text-[14px] font-bold">A+</span>
        </button>

        <div className="w-px h-5 bg-[#3a3a3a] mx-1" />

        {/* 字体选择 */}
        <div className="relative">
          <select
            value={fontFamily}
            onChange={(e) => setFontFamily(e.target.value)}
            className="appearance-none bg-[#252525] border border-[#333] rounded-full pl-3 pr-8 h-8 text-[14px] text-[#b0b0b0] outline-none focus:border-[#f59e0b] cursor-pointer"
            style={{ fontFamily: fontFamily || undefined }}
            title="字体"
          >
            {FONT_OPTIONS.map((f) => (
              <option key={f.label} value={f.value} style={{ fontFamily: f.value || undefined }}>{f.label}</option>
            ))}
          </select>
          <ChevronDownIcon className="absolute right-1.5 top-1/2 -translate-y-1/2 pointer-events-none text-[#666] h-3 w-3" strokeWidth={1.8} aria-hidden />
        </div>

        <div className="w-px h-5 bg-[#3a3a3a] mx-1" />

        {/* 上一章 */}
        <button onClick={onPrev} disabled={!hasPrev} className="h-8 w-auto px-2.5 flex items-center gap-1 rounded-full text-[#b0b0b0] hover:bg-[#333] hover:text-white transition-colors disabled:opacity-30 disabled:cursor-not-allowed" title="上一章">
          <span className="text-[14px] font-semibold tracking-wide">last</span>
          <Icon.ChevronLeft />
        </button>

        {/* 字数（替换原分割线位置） */}
        <span className="text-[12px] text-[#888] tabular-nums px-1 select-none">{chars.toLocaleString()}</span>

        {/* 下一章 */}
        <button onClick={onNext} disabled={!hasNext} className="h-8 w-auto px-2.5 flex items-center gap-1 rounded-full text-[#b0b0b0] hover:bg-[#333] hover:text-white transition-colors disabled:opacity-30 disabled:cursor-not-allowed" title="下一章">
          <Icon.ChevronRight />
          <span className="text-[14px] font-semibold tracking-wide">next</span>
        </button>

        <div className="w-px h-5 bg-[#3a3a3a] mx-1" />

        {/* 复制 */}
        <button onClick={handleCopy} className={`${tbBtn} relative`} title="复制全文">
          {copied ? <span className="text-emerald-400"><Icon.Check /></span> : <Icon.Copy />}
        </button>

        {/* 导出 */}
        <div className="relative" ref={exportRef}>
          <button onClick={() => setExportOpen((v) => !v)} className={`${tbBtn} ${exportOpen ? 'bg-[#333] text-white' : ''}`} title="导出">
            <Icon.Download />
          </button>
          {exportOpen && (
            <ExportMenu bookId={bookId} currentPath={path} content={content} onClose={() => setExportOpen(false)} />
          )}
        </div>

        <div className="w-px h-5 bg-[#3a3a3a] mx-1" />

        {/* 阅读/编辑切换（图标） */}
        {!readonly && (
          <div className="flex rounded-full bg-[#252525] p-0.5">
            <button onClick={() => setMode('read')} className={`w-8 h-8 flex items-center justify-center rounded-full transition-colors ${mode === 'read' ? tbBtnActive : 'text-[#b0b0b0] hover:text-white'}`} title="阅读模式">
              <Icon.Eye />
            </button>
            <button onClick={() => setMode('edit')} className={`w-8 h-8 flex items-center justify-center rounded-full transition-colors ${mode === 'edit' ? tbBtnActive : 'text-[#b0b0b0] hover:text-white'}`} title="编辑模式">
              <Icon.Pencil />
            </button>
          </div>
        )}

        <div className="w-px h-5 bg-[#3a3a3a] mx-1" />

        {/* 保存 */}
        {!readonly && (
          <button onClick={onSave} disabled={!dirty || saving}
            className="w-8 h-8 flex items-center justify-center rounded-full bg-[#f59e0b] text-black disabled:opacity-30 disabled:cursor-not-allowed hover:bg-[#fbbf24] transition-colors"
            title={dirty ? '保存' : '已保存'}>
            <Icon.Save />
          </button>
        )}

        {/* 删除当前篇章 */}
        {!readonly && (
          <button onClick={() => { setDeleteError(null); setDeleteConfirm(true) }} disabled={mobileView}
            className="w-8 h-8 flex items-center justify-center rounded-full text-[#b0b0b0] hover:bg-red-950/40 hover:text-red-400 transition-colors disabled:opacity-30 disabled:cursor-not-allowed disabled:hover:bg-transparent disabled:hover:text-[#b0b0b0]"
            title={mobileView ? '手机预览中，退出后再删除' : '删除当前篇章'}>
            <Icon.Trash />
          </button>
        )}

        <div className="w-px h-5 bg-[#3a3a3a] mx-1" />

        {/* 手机预览模式切换 */}
        <button onClick={onToggleMobile} className={`${tbBtn} ${mobileView ? 'bg-[#f59e0b] text-black hover:bg-[#fbbf24]' : ''}`}
          title={mobileView ? '退出手机预览（Esc / Cmd+Shift+M）' : '手机预览模式（Cmd+Shift+M）'}>
          <Icon.Phone />
        </button>
      </div>

      {/* 保存提示 */}
      {saveMsg && (
        <div className={`absolute top-full mt-1.5 left-1/2 -translate-x-1/2 text-[14px] px-2 py-0.5 rounded ${saveMsg.startsWith('失败') ? 'text-red-400' : 'text-[#f59e0b]'}`}>
          {saveMsg}
        </div>
      )}

      {/* 删除确认弹窗 */}
      {deleteConfirm && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
          onClick={() => !deleting && setDeleteConfirm(false)}
        >
          <div
            className="w-[300px] rounded-lg border border-[#3a3a3a] bg-[#1e1e1e] shadow-2xl shadow-black/60"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="px-4 py-3 border-b border-[#2a2a2a]">
              <div className="text-[14px] font-medium text-white">删除篇章</div>
            </div>
            <div className="px-4 py-4 text-[14px] text-[#aaa] leading-5">
              确定删除
              <span className="text-amber-300 font-medium">「{path.split('/').pop() ?? ''}」</span>
              ？
              <div className="mt-2 text-[14px] text-[#666]">删除后将移入回收站 projects/.trash/（可恢复），目录将同步更新。</div>
              {deleteError && <div className="mt-2 text-[14px] text-red-400">{deleteError}</div>}
            </div>
            <div className="px-4 py-3 flex justify-end gap-2 border-t border-[#2a2a2a]">
              <button
                onClick={() => setDeleteConfirm(false)}
                disabled={deleting}
                className="px-3 py-1 rounded-md text-[14px] bg-[#2a2a2a] hover:bg-[#333] text-[#ccc] border border-[#3a3a3a] disabled:opacity-40 transition-colors"
              >
                取消
              </button>
              <button
                onClick={() => { setDeleteConfirm(false); onDelete() }}
                disabled={deleting}
                className="px-3 py-1 rounded-md text-[14px] bg-red-600 hover:bg-red-500 text-white font-medium disabled:opacity-40 transition-colors"
              >
                {deleting ? '删除中…' : '确认删除'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

/* ─────────────────────  编辑器面板  ───────────────────── */

const READ_CLS = 'editor-content mx-auto px-[25px] py-16'

export default function EditorPane({ bookId, path, readFirst = false, onNavigate, onDeleted, onSaved, refreshKey = 0, onDirtyChange }: Props) {
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [meta, setMeta] = useState<{ writable: boolean; name: string } | null>(null)
  const [content, setContent] = useState('')
  const [mode, setMode] = useState<Mode>(readFirst ? 'read' : 'edit')
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [saveMsg, setSaveMsg] = useState<string | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  /** 手机预览模式：切换后桌面正文隐藏，正文只显示在居中手机容器内 */
  const [mobileView, setMobileView] = useState(false)
  const [fontSize, setFontSize] = useState(() => {
    try {
      const saved = localStorage.getItem('nww_v12_fontsize')
      return saved ? Math.max(12, Math.min(28, parseInt(saved, 10))) : 16
    } catch { return 16 }
  })
  const [fontFamily, setFontFamily] = useState(() => {
    try { return localStorage.getItem('nww_v12_fontfamily') ?? '' } catch { return '' }
  })
  // 章节健康分 / 落盘时间 / 字数（来自 tree 接口，与侧栏同源），用于正文阅读区章头标注
  const [metaMap, setMetaMap] = useState<Record<string, { score?: number; mtime?: number; chars?: number }>>({})
  // 当前书籍名称（来自 tree 接口，供手机预览头栏展示）
  const [bookTitle, setBookTitle] = useState('')

  // 字号持久化
  useEffect(() => {
    try { localStorage.setItem('nww_v12_fontsize', String(fontSize)) } catch { /* ignore */ }
  }, [fontSize])
  // 字体持久化
  useEffect(() => {
    try { localStorage.setItem('nww_v12_fontfamily', fontFamily) } catch { /* ignore */ }
  }, [fontFamily])

  // 章节列表（用于上下章导航）
  const [chapters, setChapters] = useState<{ path: string; name: string }[]>([])
  useEffect(() => {
    if (!bookId) { setChapters([]); setMetaMap({}); setBookTitle(''); return }
    api.tree(bookId).then((tree) => {
      setBookTitle(tree.title ?? '')
      const body = tree.sections.find((s) => s.section === '正文')
      const all = body?.files ?? []
      // 只保留真正的章节文件（文件名匹配 第N章，且路径不含 review/backup/snapshots 子目录）
      const CHAPTER_RE = /^第0*\d+章/
      const SKIP_DIRS = ['_review_snapshots', '.backup', '_设定', '_笔记', '_大纲', '_细纲', '_草稿', '_废弃', '_储备']
      const files = all.filter((f) => {
        if (!CHAPTER_RE.test(f.name)) return false
        // 路径里不能含跳过目录（防止 review 快照/备份混进翻页）
        if (SKIP_DIRS.some((d) => f.path.includes('/' + d + '/'))) return false
        return true
      })
      const sorted = [...files].sort((a, b) => {
        const na = a.name.match(/^第0*(\d+)章/)
        const nb = b.name.match(/^第0*(\d+)章/)
        if (na && nb) return parseInt(na[1]) - parseInt(nb[1])
        return a.name.localeCompare(b.name, 'zh-CN')
      })
      setChapters(sorted.map((f) => ({ path: f.path, name: f.name })))
      const mm: Record<string, { score?: number; mtime?: number; chars?: number }> = {}
      for (const f of files) {
        if (f.score != null || f.mtime != null || f.chars != null) {
          mm[f.path] = { score: f.score, mtime: f.mtime, chars: f.chars }
        }
      }
      setMetaMap(mm)
    }).catch(() => { setChapters([]); setMetaMap({}); setBookTitle('') })
  }, [bookId, refreshKey])

  // 上下章导航
  const currentIdx = path ? chapters.findIndex((c) => c.path === path) : -1
  const hasPrev = currentIdx > 0
  const hasNext = currentIdx >= 0 && currentIdx < chapters.length - 1
  const goPrev = useCallback(() => {
    if (hasPrev && onNavigate) onNavigate(chapters[currentIdx - 1].path)
  }, [hasPrev, currentIdx, chapters, onNavigate])
  const goNext = useCallback(() => {
    if (hasNext && onNavigate) onNavigate(chapters[currentIdx + 1].path)
  }, [hasNext, currentIdx, chapters, onNavigate])

  useEffect(() => {
    if (!path) {
      setLoading(false)
      setError(null)
      setMeta(null)
      setContent('')
      setDirty(false)
      return
    }
    let cancelled = false
    setLoading(true); setError(null); setDirty(false); setSaveMsg(null)
    // 改名后切到新路径会触发本 effect 重载：此时保留当前编辑态，不要被 readFirst 踢回阅读模式
    if (keepModeRef.current) keepModeRef.current = false
    else setMode(readFirst ? 'read' : 'edit')
    api.file(bookId, path)
      .then((f) => { if (cancelled) return; setMeta({ writable: f.writable, name: f.name }); setContent(f.content ?? ''); if (f.content === null) setError('二进制文件不支持编辑') })
      .catch((e) => { if (!cancelled) setError(String(e)) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [bookId, path, readFirst, refreshKey])

  // 脏状态上报：App 据此禁用快捷「优化/重写」，防止未保存修改被后台落盘覆盖
  useEffect(() => {
    onDirtyChange?.(dirty)
  }, [dirty, onDirtyChange])

  const chars = useMemo(() => countVisibleChars(content), [content])

  const save = useCallback(async () => {
    if (!path || !meta?.writable || !dirty) return
    setSaving(true); setSaveMsg(null)
    try {
      const res = await api.writeFile(bookId, path, content, 'editor')
      setDirty(false)
      // queued 只代表「改稿快照已入队」，提炼在后台异步进行，不得宣称已吸收
      setSaveMsg(res.style_feedback?.queued ? '已保存 · 本次改稿已记录' : '已保存')
      onSaved?.()
    }
    catch (e) { setSaveMsg(`保存失败：${String(e)}`) }
    finally { setSaving(false); setTimeout(() => setSaveMsg(null), 2500) }
  }, [bookId, path, content, dirty, meta, onSaved])

  /** AI 候选采纳后自动落盘（V14 行为对齐）：直接用回调里的 md，不依赖异步 state。
   *  失败向上抛，由 MdView 显示「写入/保存失败」并保留候选，绝不伪装成已保存。 */
  const commitAiEdit = useCallback(async (md: string) => {
    if (!path || !meta?.writable) throw new Error('当前文件不可写，无法自动保存 AI 采纳结果')
    setContent(md)
    setSaving(true)
    try {
      const res = await api.writeFile(bookId, path, md, 'editor-ai')
      setDirty(false)
      setSaveMsg(res.style_feedback?.queued ? 'AI 采纳已保存 · 本次改稿已记录' : 'AI 采纳已保存')
      onSaved?.()
    } catch (e) {
      setSaveMsg(`AI 采纳保存失败：${String(e)}`)
      throw e
    } finally {
      setSaving(false)
      setTimeout(() => setSaveMsg(null), 2500)
    }
  }, [bookId, path, meta, onSaved])

  /** 章节标题落地：① 正文首行标题行同步 ② 文件重命名（侧栏/上下章/导出随之同步） */
  const [titleMsg, setTitleMsg] = useState<string | null>(null)
  /** 改名引发的重载不重置阅读/编辑模式 */
  const keepModeRef = useRef(false)
  const commitTitle = useCallback(async (newTitle: string) => {
    if (!path || !meta?.writable) return
    const name = path.split('/').pop() ?? ''
    const parsed = parseChapterName(name)
    if (!parsed || parsed.title === newTitle) return
    const newName = buildChapterFileName(name, newTitle)
    if (!newName) { setTitleMsg('标题未保存：文件名不符合「第N章」命名'); return }
    try {
      // 先写正文（含当前未保存修改，避免丢稿），再改名
      const nextContent = replaceChapterTitleInMd(content, newTitle)
      if (nextContent !== content) {
        await api.writeFile(bookId, path, nextContent)
        setContent(nextContent)
      }
      const res = await api.renameFile(bookId, path, newName)
      setDirty(false)
      setTitleMsg('标题已更新')
      onSaved?.()
      // 导航到新路径：侧栏刷新 + 重新加载（内容即刚写入的）；保持当前编辑态
      keepModeRef.current = mode === 'edit'
      onNavigate?.(res.path)
    } catch (e) {
      setTitleMsg(`标题保存失败：${String(e)}`)
    } finally {
      setTimeout(() => setTitleMsg(null), 2500)
    }
  }, [path, meta, content, bookId, mode, onSaved, onNavigate])

  const handleDelete = useCallback(async () => {
    if (!path || !meta?.writable) return
    setDeleting(true); setDeleteError(null)
    try {
      await api.deleteFile(bookId, path)
      // 删除成功：优先导航到上一章，否则下一章，都没有则清空
      if (hasPrev && onNavigate) {
        onNavigate(chapters[currentIdx - 1].path)
      } else if (hasNext && onNavigate) {
        onNavigate(chapters[currentIdx + 1].path)
      } else if (onNavigate) {
        onNavigate('')
      }
      onDeleted?.()
    } catch (e) {
      setDeleteError(`删除失败：${String(e)}`)
    } finally {
      setDeleting(false)
    }
  }, [bookId, path, meta, hasPrev, hasNext, currentIdx, chapters, onNavigate, onDeleted])

  const readonly = !meta?.writable
  const hasContent = !!path
  // 工具栏仅在章节正文时显示
  const showToolbar = !!path && path.startsWith('正文/')
  // 是否为章节正文（决定显示章头、隐藏正文自带的首个 h1 标题）
  const isChapterBody = !!path && path.startsWith('正文/')
  // 上一章尾勾提示：仅章节正文且存在上一章时显示（第 1 章无上一章 → 不渲染）
  const prevChapterPath = isChapterBody && hasPrev ? chapters[currentIdx - 1].path : ''

  // 手机预览切换：进入时自动切阅读模式（阅读器形态，左右/上下键直接翻章），退出保持用户选择的模式
  const toggleMobile = useCallback(() => {
    setMobileView((v) => {
      if (!v) setMode('read')
      return !v
    })
  }, [])

  // 手机预览：章节标题 / 位置标签
  const phoneParsed = useMemo(() => parseChapterName(path?.split('/').pop() ?? ''), [path])
  const phoneTitle = phoneParsed ? `第${parseInt(phoneParsed.num, 10)}章 ${phoneParsed.title}` : (path?.split('/').pop() ?? '')
  const phoneNum = phoneParsed ? `第${parseInt(phoneParsed.num, 10)}章` : ''
  const phoneTitleText = phoneParsed?.title ?? ''
  const phonePos = chapters.length > 0 && currentIdx >= 0 ? `第 ${currentIdx + 1} / ${chapters.length} 章` : '正文'

  // 键盘翻页快捷键 —— 绑定 window 全局监听，不依赖焦点
  // （翻页后 activePath 变化导致组件重新挂载，焦点会丢失，元素级监听会失效）
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (!hasContent) return
      // 手机预览切换：Cmd/Ctrl+Shift+M（编辑中也可切换，故放在输入框拦截之前）
      if ((e.metaKey || e.ctrlKey) && e.shiftKey && (e.key === 'M' || e.key === 'm')) {
        e.preventDefault()
        toggleMobile()
        return
      }
      // 焦点在输入框/可编辑元素上时不拦截，避免与编辑光标、搜索框冲突
      const target = e.target as HTMLElement | null
      if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable)) return
      const isReadMode = mode === 'read'

      if (e.key === 'PageUp') { e.preventDefault(); goPrev(); return }
      if (e.key === 'PageDown') { e.preventDefault(); goNext(); return }

      if (isReadMode) {
        if (e.key === 'ArrowUp' || e.key === 'ArrowLeft') { e.preventDefault(); goPrev() }
        else if (e.key === 'ArrowDown' || e.key === 'ArrowRight') { e.preventDefault(); goNext() }
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [hasContent, mode, goPrev, goNext])

  if (loading) return <div className="h-full flex items-center justify-center text-[14px] text-[#555] bg-[#141414]">加载中…</div>
  if (error) return <div className="h-full flex items-center justify-center text-[14px] text-red-400 bg-[#141414]">{error}<pre className="sr-only">{path}</pre></div>
  return (
    <div className="h-full flex flex-col overflow-hidden outline-none">
      {/* 工具栏 —— 仅章节正文显示 */}
      {showToolbar && (
        <div className="shrink-0 pt-0 pb-3 px-4">
          <FloatingToolbar
            mode={mode} setMode={setMode}
            fontSize={fontSize} setFontSize={setFontSize}
            fontFamily={fontFamily} setFontFamily={setFontFamily}
            onSave={save} dirty={dirty} saving={saving} saveMsg={saveMsg}
            readonly={readonly} chars={chars} content={content} bookId={bookId} path={path ?? ''}
            onPrev={goPrev} onNext={goNext} hasPrev={hasPrev} hasNext={hasNext}
            onDelete={handleDelete} deleting={deleting} deleteError={deleteError} setDeleteError={setDeleteError}
            mobileView={mobileView} onToggleMobile={toggleMobile}
          />
        </div>
      )}

      {/* 手机预览模式：桌面正文隐藏，正文在居中手机容器内阅读/编辑（即编辑器本身） */}
      {mobileView && hasContent ? (
        <PhonePreview
          bookTitle={bookTitle}
          title={phoneTitle}
          content={content}
          editable={mode === 'edit' && !readonly}
          onInput={(md) => { setContent(md); setDirty(true) }}
          titleNum={phoneNum}
          titleText={phoneTitleText}
          onTitleChange={mode === 'edit' && !readonly ? commitTitle : undefined}
          fontSize={fontSize}
          fontFamily={fontFamily}
          posLabel={phonePos}
          chapters={chapters}
          currentPath={path ?? ''}
          onNavigate={onNavigate ?? (() => {})}
          hasPrev={hasPrev}
          hasNext={hasNext}
          onPrev={goPrev}
          onNext={goNext}
          onExit={() => setMobileView(false)}
        />
      ) : (
        <ScrollArea className="flex-1 min-h-0 pb-10" style={{ fontSize: `${fontSize}px`, fontFamily: fontFamily || undefined }}>
          {hasContent ? (
            <div className={READ_CLS + (isChapterBody ? ' [&>div.space-y-3>h1:first-child]:hidden' : '')}>
              {isChapterBody && (
                <ChapterHeader
                  path={path ?? ''}
                  score={metaMap[path ?? '']?.score}
                  mtime={metaMap[path ?? '']?.mtime}
                  editable={mode === 'edit' && !readonly}
                  onCommitTitle={commitTitle}
                  notice={titleMsg}
                />
              )}
              {prevChapterPath && (
                <PrevTailHook
                  bookId={bookId}
                  prevPath={prevChapterPath}
                  onOpenPrev={onNavigate ? goPrev : undefined}
                />
              )}
              <MdView text={content} editable={mode === 'edit' && !readonly}
                onInput={(md) => { setContent(md); setDirty(true) }} onCommit={commitAiEdit} />
            </div>
          ) : (
            <div className="h-full flex items-center justify-center">
              <span className="text-[14px] text-[#555]">从左侧选择文件</span>
            </div>
          )}
        </ScrollArea>
      )}
    </div>
  )
}
