import { useState, useEffect } from 'react'
import { ChevronRight, Search } from 'lucide-react'

// ─────────────────────  API 请求封装  ─────────────────────

async function publisherApi<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`/publisher-api${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail || `API ${res.status}`)
  }
  return res.json()
}

// ─────────────────────  类型定义  ─────────────────────

interface PlatformInfo {
  id: string
  name: string
  color: string
  logged_in: boolean
  last_check: string
  today_published: number
  total_scheduled: number
}

interface TaskCard {
  platform: string
  platform_name: string
  book_id: number
  book_name: string
  total_chapters: number
  last_published_chapter: number
  streak_days: number
  today_write_chapters: number[]
  today_published_chapters: number[]
}

interface DayPublishItem {
  record_id: number
  platform: string
  platform_name: string
  chapters: number[]
  chapter_title: string
  book_name: string
  status: string
  time: string
}

interface CalendarDay {
  write?: { book_id: number; book_name: string; chapters: number[] } | null
  publish?: DayPublishItem[]
}

interface CalendarData {
  year: number
  month: number
  days: Record<string, CalendarDay>
}

interface BookPlatformBinding {
  platform: string
  schedule: string
  enabled: number
  published_up_to: number | null
  progress_checked_at: string | null
  progress_source: string
}

interface Book {
  id: number
  name: string
  draft_dir: string
  pending_count: number
  total_words: number
  total_chapters: number
  platforms: BookPlatformBinding[]
}

/** 自动核对平台的返回（后端 base.fetch_published_progress 的结构） */
interface ProgressCheckResult {
  ok: boolean
  published_up_to?: number | null
  candidates?: number[]
  sample?: string
  entered_via?: string
  url?: string
  error?: string
  book_name?: string
  platform?: string
}

/** 归类任务列表里的一条发布明细 */
interface TaskGroupItem {
  id: number
  status: string
  scheduled_time: string | null
  error_message: string
  chapter_number: number | null
  title: string
  words: number
}

/** 同一天要发的若干章（字数累计到门槛才成组） */
interface PublishDay {
  date: string
  time: string
  words: number
  quota: number
  met: boolean
  items: TaskGroupItem[]
}

interface WrittenItem {
  chapter_number: number | null
  title: string
  words: number
}

/** 「平台 × 书籍」一组：发布任务 + 撰写任务 + 字数缺口 */
interface TaskGroup {
  id: number
  platform: string
  platform_name: string
  book_id: number
  book_name: string
  schedule: string
  enabled: number
  published_up_to: number | null
  progress_checked_at: string | null
  progress_source: string
  reconciled: boolean
  quota: number
  /** 单章合格线：≥ 这条就算这一章达标（章层面没有「不达标」一说） */
  chapter_min_words: number
  group_days: number
  avg_chapter_words: number
  publish_days: PublishDay[]
  written_today: WrittenItem[]
  pending_chapters: number
  pending_words: number
  chapters_to_write: number
  next_group_shortfall_words: number
}

interface AppSettings {
  min_words_qidian: string
  min_words_fanqie: string
  chapter_min_words: string
  schedule_group_days: string
}

// ─────────────────────  状态映射与配色  ─────────────────────

const statusMap: Record<string, { label: string; cls: string }> = {
  pending: { label: '待发布', cls: 'bg-zinc-800 text-zinc-300' },
  scheduled: { label: '已定时', cls: 'bg-amber-950/60 text-amber-400 border border-amber-800/60' },
  publishing: { label: '发布中…', cls: 'bg-blue-950/60 text-blue-400 animate-pulse' },
  published: { label: '已发布', cls: 'bg-emerald-950/60 text-emerald-400 border border-emerald-800/60' },
  failed: { label: '失败', cls: 'bg-red-950/60 text-red-400' },
  // 对账护栏拦下的重复发布（章号 ≤ 平台已发章号），不再参与调度
  skipped: { label: '已拦截·防重复', cls: 'bg-zinc-800/80 text-zinc-500 border border-zinc-700' },
}

const platformLabel: Record<string, string> = { qidian: '起点', fanqie: '番茄' }
const platformColor: Record<string, string> = { qidian: '#e94560', fanqie: '#ff6b35' }

// ─────────────────────  任务卡片  ─────────────────────

function TaskCardItem({ card }: { card: TaskCard }) {
  const color = platformColor[card.platform] || '#888'
  const writeText = card.today_write_chapters.length
    ? `第 ${card.today_write_chapters.join('、')} 章`
    : '无'
  const pubText = card.today_published_chapters.length
    ? `已发第 ${card.today_published_chapters.join('、')} 章`
    : '今日未发'

  return (
    <div className="rounded-xl border border-zinc-800 bg-zinc-900/70 p-4 transition hover:border-zinc-700">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <span
            className="w-7 h-7 rounded-lg flex items-center justify-center text-xs font-bold"
            style={{ backgroundColor: `${color}25`, color }}
          >
            {card.platform === 'qidian' ? '起' : '番'}
          </span>
          <span className="text-sm font-medium text-zinc-100">{card.platform_name}</span>
        </div>
        <span className="text-[13px] text-zinc-400 font-medium truncate max-w-32">{card.book_name}</span>
      </div>
      <div className="space-y-1.5 text-[13px]">
        <div className="flex justify-between text-zinc-400">
          <span>章节进度</span>
          <span className="text-zinc-200 tabular-nums">共 {card.total_chapters} 章 · 发至第 {card.last_published_chapter} 章</span>
        </div>
        <div className="flex justify-between text-zinc-400">
          <span>连续发布</span>
          <span className="text-amber-400 font-medium tabular-nums">🔥 {card.streak_days} 天</span>
        </div>
        <div className="flex justify-between text-zinc-400">
          <span>今日撰写</span>
          <span className="text-blue-400 tabular-nums">✍️ {writeText}</span>
        </div>
        <div className="flex justify-between text-zinc-400">
          <span>今日发布</span>
          <span className={card.today_published_chapters.length ? 'text-emerald-400 tabular-nums' : 'text-zinc-500'}>
            📤 {pubText}
          </span>
        </div>
      </div>
    </div>
  )
}

// ─────────────────────  平台状态卡片  ─────────────────────

function PlatformCard({
  platform,
  onLogin,
  isLoggingIn,
}: {
  platform: PlatformInfo
  onLogin: (id: string) => void
  isLoggingIn: boolean
}) {
  return (
    <div className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-4 flex items-center justify-between">
      <div className="flex items-center gap-3">
        <div
          className="w-9 h-9 rounded-lg flex items-center justify-center text-sm font-bold"
          style={{ backgroundColor: `${platform.color}20`, color: platform.color }}
        >
          {platform.id === 'qidian' ? '起' : '番'}
        </div>
        <div>
          <div className="text-sm font-medium text-zinc-100">{platform.name}</div>
          <div className="text-[12px] text-zinc-400 mt-0.5">
            今日已发 <span className="text-zinc-200 font-medium tabular-nums">{platform.today_published}</span> 章 · 待定时 <span className="text-zinc-200 font-medium tabular-nums">{platform.total_scheduled}</span> 篇
          </div>
        </div>
      </div>
      <div className="flex items-center gap-3">
        <span
          className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium ${
            platform.logged_in ? 'bg-emerald-950/50 text-emerald-400 border border-emerald-800/40' : 'bg-red-950/50 text-red-400 border border-red-800/40'
          }`}
        >
          <span className={`w-1.5 h-1.5 rounded-full ${platform.logged_in ? 'bg-emerald-400' : 'bg-red-400'}`} />
          {platform.logged_in ? '会话有效' : '未登录'}
        </span>
        {!platform.logged_in && (
          <button
            disabled={isLoggingIn}
            onClick={() => onLogin(platform.id)}
            className={`px-3 py-1 rounded text-xs font-medium transition ${
              isLoggingIn
                ? 'bg-zinc-800 text-zinc-500 cursor-not-allowed'
                : 'bg-amber-600 hover:bg-amber-500 text-white shadow'
            }`}
          >
            {isLoggingIn ? '扫码中…' : '扫码登录'}
          </button>
        )}
      </div>
    </div>
  )
}

// ─────────────────────  发布记录行  ─────────────────────

function TaskItemRow({
  item,
  platform,
  bookName,
  chapterMin,
  onRetry,
  onPublishNow,
  onDelete,
}: {
  item: TaskGroupItem
  platform: string
  bookName: string
  /** 单章合格线：≥ 这条就算这一章达标 */
  chapterMin: number
  onRetry: (id: number) => void
  onPublishNow: (id: number) => void
  onDelete: (id: number) => void
}) {
  const st = statusMap[item.status] ?? { label: item.status, cls: 'bg-zinc-800 text-zinc-400' }
  const chapterOk = chapterMin <= 0 || item.words >= chapterMin
  return (
    <div className="flex items-center justify-between gap-3 px-3 py-2 rounded-lg bg-zinc-900/60 border border-zinc-800/80 hover:border-zinc-700 transition">
      <div className="flex items-center gap-2.5 min-w-0">
        <span
          className="text-[11px] px-1.5 py-0.5 rounded font-bold shrink-0 tabular-nums"
          style={{
            backgroundColor: `${platformColor[platform] || '#888'}20`,
            color: platformColor[platform] || '#aaa',
          }}
        >
          第{item.chapter_number ?? '?'}章
        </span>
        <div className="min-w-0">
          <div className="text-[13px] text-zinc-200 truncate">{item.title}</div>
          <div className="text-[11px] text-zinc-500 truncate">
            {item.words.toLocaleString()} 字 ·{' '}
            <span className={chapterOk ? 'text-emerald-500/80' : 'text-zinc-400'}>
              {chapterOk ? '单章达标' : '单章偏短'}
            </span>{' '}
            · {bookName}
            {item.error_message ? <span> · {item.error_message}</span> : null}
          </div>
        </div>
      </div>
      <div className="flex items-center gap-2 shrink-0">
        <span className={`text-[11px] px-2 py-0.5 rounded-full font-medium ${st.cls}`}>{st.label}</span>
        {item.status === 'scheduled' && (
          <button
            onClick={() => onPublishNow(item.id)}
            className="px-2 py-0.5 rounded text-[11px] bg-zinc-800 hover:bg-zinc-700 text-amber-400 transition"
          >
            立即发
          </button>
        )}
        {item.status === 'failed' && (
          <button
            onClick={() => onRetry(item.id)}
            className="px-2 py-0.5 rounded text-[11px] bg-amber-600/90 hover:bg-amber-500 text-white transition"
          >
            重试
          </button>
        )}
        <button
          onClick={() => onDelete(item.id)}
          title="删除这条排期记录"
          className="px-2 py-0.5 rounded text-[11px] bg-zinc-800/80 hover:bg-red-950/70 text-zinc-400 hover:text-red-400 border border-zinc-700 hover:border-red-900 transition"
        >
          删除
        </button>
      </div>
    </div>
  )
}

// ─────────────────────  主组件  ─────────────────────

interface NovelPublisherProps {
  /** 2026-11-07 固定两列组合：外部书架卡受控的当前书 id（= 项目目录名）。
   *  发布库 /api/books 的 name 由 database.py:195 同步自同一目录名，可精确匹配，
   *  与 web/backend/app/books.py:225 的 Book.id 同源 —— 不需要书名模糊匹配。 */
  linkedBookId?: string | null
  /** 关联书的显示名（优先展示，缺省回落目录名） */
  linkedBookTitle?: string | null
  /** 工作台内换书回写全局 activeBook：书架高亮 / 工作台 / 排期表单三者同源 */
  onSelectBook?: (bookId: string) => void
}

export default function NovelPublisher({ linkedBookId, linkedBookTitle, onSelectBook }: NovelPublisherProps) {
  /** 受控态（传了 linkedBookId，含 null）时书源由外层书架决定；不传则保留旧「默认第一本」行为 */
  const isLinked = linkedBookId !== undefined
  const [taskCards, setTaskCards] = useState<TaskCard[]>([])
  const [platforms, setPlatforms] = useState<PlatformInfo[]>([])
  const [calendar, setCalendar] = useState<CalendarData | null>(null)
  const [calYear, setCalYear] = useState(new Date().getFullYear())
  const [calMonth, setCalMonth] = useState(new Date().getMonth() + 1)
  const [selectedDay, setSelectedDay] = useState<number>(new Date().getDate())
  const [books, setBooks] = useState<Book[]>([])
  /** 外部书架当前书 → 发布库数字 id。name 与 Book.id 同为项目目录名，精确匹配 */
  const linkedPubBookId = books.find((b) => b.name === linkedBookId)?.id ?? null
  const [taskGroups, setTaskGroups] = useState<TaskGroup[]>([])
  const [groupDays, setGroupDays] = useState<number>(4)
  const [appSettings, setAppSettings] = useState<AppSettings | null>(null)
  const [settingsMsg, setSettingsMsg] = useState<string | null>(null)
  const [savingSettings, setSavingSettings] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [loggingInPlatform, setLoggingInPlatform] = useState<string | null>(null)

  // 任务制定表单
  const [formBook, setFormBook] = useState<number | ''>('')
  const [formPlatform, setFormPlatform] = useState<'both' | 'qidian' | 'fanqie'>('both')
  const [formTime, setFormTime] = useState('20:00')
  const [submitting, setSubmitting] = useState(false)
  const [actionMsg, setActionMsg] = useState<string | null>(null)

  // ── 平台进度对账（排期前置条件）──
  const [reconciling, setReconciling] = useState<string | null>(null)
  const [checkResult, setCheckResult] = useState<Record<string, ProgressCheckResult>>({})
  const [manualChapter, setManualChapter] = useState<Record<string, string>>({})
  const [manualOpen, setManualOpen] = useState<Record<string, boolean>>({})
  const [staleInfo, setStaleInfo] = useState<Record<string, number>>({})
  const [taskMsg, setTaskMsg] = useState<string | null>(null)

  const loadData = async (y = calYear, m = calMonth) => {
    setError(null)
    try {
      const [cardsRes, platRes, calRes, booksRes, groupsRes, settingsRes] = await Promise.all([
        publisherApi<{ cards: TaskCard[] }>('/api/task-cards'),
        publisherApi<{ platforms: PlatformInfo[] }>('/api/platforms/status'),
        publisherApi<CalendarData>(`/api/calendar?year=${y}&month=${m}`),
        publisherApi<{ books: Book[] }>('/api/books'),
        publisherApi<{ groups: TaskGroup[]; group_days: number }>('/api/task-groups'),
        publisherApi<{ settings: AppSettings; defaults: AppSettings }>('/api/settings'),
      ])
      setTaskCards(cardsRes.cards)
      setPlatforms(platRes.platforms)
      setCalendar(calRes)
      setBooks(booksRes.books)
      setTaskGroups(groupsRes.groups)
      setGroupDays(groupsRes.group_days || 4)
      setAppSettings(settingsRes.settings)
      if (!formBook && booksRes.books.length > 0) {
        if (isLinked) {
          // 受控态只认书架当前书；该书未同步到发布库时保持空，
          // 绝不回落到第一本 —— 否则会把别的书的平台绑定/排期误挂到新书。
          const linked = booksRes.books.find((b) => b.name === linkedBookId)
          if (linked) setFormBook(linked.id)
        } else {
          setFormBook(booksRes.books[0].id)
        }
      }
    } catch (e) {
      setError(`加载数据异常：${String(e)}`)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    loadData(calYear, calMonth)
    const interval = setInterval(() => loadData(calYear, calMonth), 20000)
    return () => clearInterval(interval)
  }, [calYear, calMonth])

  /** 书架 → 工作台：外部当前书一变，排期表单 / 平台对账 / 存稿目录立刻切到同一本。
   *  formBook 不进依赖，避免 20s 轮询刷新 books 时把下拉里的即时选择反复拉回。
   *  该书未同步到发布库时清空工作台书源（平台绑定区不渲染），防止误操作落到上一本书。 */
  useEffect(() => {
    if (!isLinked) return
    if (linkedPubBookId != null) {
      setFormBook((cur) => (cur === linkedPubBookId ? cur : linkedPubBookId))
    } else if (linkedBookId && books.length > 0) {
      setFormBook('')
    }
  }, [isLinked, linkedBookId, linkedPubBookId, books.length])

  // 换书时清掉上一本的对账中间态，避免把 A 书的核对结果误当成 B 书的
  useEffect(() => {
    setCheckResult({})
    setManualChapter({})
    setManualOpen({})
    setStaleInfo({})
    setTaskMsg(null)
  }, [formBook])

  const changeMonth = (delta: number) => {
    let nextM = calMonth + delta
    let nextY = calYear
    if (nextM > 12) {
      nextM = 1
      nextY++
    } else if (nextM < 1) {
      nextM = 12
      nextY--
    }
    setCalYear(nextY)
    setCalMonth(nextM)
    loadData(nextY, nextM)
  }

  const handleLogin = async (platformId: string) => {
    try {
      setLoggingInPlatform(platformId)
      const res = await publisherApi<{ success: boolean; message: string }>(
        `/api/platforms/${platformId}/login`,
        { method: 'POST' },
      )
      alert(res.message || `已启动 ${platformId} 扫码登录窗口，请在弹出的浏览器中操作`)
      let count = 0
      const timer = setInterval(async () => {
        count++
        await loadData()
        if (count >= 15) {
          clearInterval(timer)
          setLoggingInPlatform(null)
        }
      }, 3000)
    } catch (e) {
      alert(`唤起登录失败：${String(e)}`)
      setLoggingInPlatform(null)
    }
  }

  const handlePublishNow = async (recordId: number) => {
    if (!confirm('确定要立即将该章节发布到平台后台吗？')) return
    try {
      setActionMsg('正在拉起浏览器发书中…')
      const res = await publisherApi<{ success: boolean; message: string }>(
        `/api/records/${recordId}/publish-now`,
        { method: 'POST' }
      )
      alert(res.message || '发布成功！')
      loadData()
    } catch (e) {
      alert(`发布失败：${String(e)}`)
    } finally {
      setActionMsg(null)
    }
  }

  const handleCancelRecord = async (recordId: number) => {
    if (!confirm('确定取消该条排期任务吗？')) return
    try {
      await publisherApi(`/api/records/${recordId}/cancel`, { method: 'POST' })
      loadData()
    } catch (e) {
      alert(`取消失败：${String(e)}`)
    }
  }

  const handleRetry = async (recordId: number) => {
    try {
      await publisherApi(`/api/records/${recordId}/retry`, { method: 'POST' })
      loadData()
    } catch (e) {
      console.error('重试失败', e)
    }
  }

  const handleDeleteRecord = async (recordId: number) => {
    if (!confirm('确定删除这条发布记录吗？删除后不再参与调度。')) return
    try {
      await publisherApi(`/api/records/${recordId}`, { method: 'DELETE' })
      loadData()
    } catch (e) {
      alert(`删除失败：${e instanceof Error ? e.message : String(e)}`)
    }
  }

  // ───────── 平台进度对账 ─────────

  /** 落库对账基准，并回报「有多少既有排期落在这个基准之下」（= 重复发布隐患） */
  const saveProgress = async (platform: string, value: number, source: 'auto' | 'manual') => {
    const bid = Number(formBook)
    const res = await publisherApi<{
      success: boolean
      published_up_to: number
      checked_at: string
      stale_scheduled: number
    }>(`/api/books/${bid}/platforms/${platform}/progress`, {
      method: 'POST',
      body: JSON.stringify({ published_up_to: value, source }),
    })
    setStaleInfo((p) => ({ ...p, [platform]: res.stale_scheduled }))
    setTaskMsg(
      res.stale_scheduled > 0
        ? `基准已确立：${platformLabel[platform]} 已发至第 ${value} 章；另有 ${res.stale_scheduled} 条排期在该章号之下（重复发布隐患），请清理。`
        : `基准已确立：${platformLabel[platform]} 已发至第 ${value} 章`,
    )
    loadData()
  }

  const handleAutoCheck = async (platform: string) => {
    const bid = Number(formBook)
    if (!bid) {
      alert('请先选择书稿')
      return
    }
    setReconciling(platform)
    setTaskMsg(`正在打开${platformLabel[platform]}后台读取已发进度…`)
    try {
      const res = await publisherApi<ProgressCheckResult>(
        `/api/books/${bid}/platforms/${platform}/check-progress`,
        { method: 'POST', body: JSON.stringify({}) },
      )
      setCheckResult((p) => ({ ...p, [platform]: res }))
      if (res.ok && res.published_up_to != null) {
        const adopt = confirm(
          `已从「${platformLabel[platform]}」后台解析到：已发布至第 ${res.published_up_to} 章。\n\n` +
            `是否将其确立为对账基准？（之后只会排期比它更大的章号）`,
        )
        if (adopt) await saveProgress(platform, res.published_up_to, 'auto')
      } else {
        setManualOpen((p) => ({ ...p, [platform]: true }))
        setTaskMsg(`自动核对未成功，请手工录入：${res.error || '未知原因'}`)
      }
    } catch (e) {
      setTaskMsg(`自动核对失败：${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setReconciling(null)
    }
  }

  const handleManualSave = async (platform: string) => {
    const raw = (manualChapter[platform] ?? '').trim()
    const n = Number(raw)
    if (!raw || !Number.isInteger(n) || n < 0) {
      alert('请填写非负整数的「已发至第几章」')
      return
    }
    try {
      await saveProgress(platform, n, 'manual')
      setManualOpen((p) => ({ ...p, [platform]: false }))
    } catch (e) {
      alert(`保存失败：${e instanceof Error ? e.message : String(e)}`)
    }
  }

  /** 清空「该书 × 该平台」的未发布排期（保留已发布留痕） */
  const handleClearRecords = async (platform: string) => {
    const bid = Number(formBook)
    try {
      const info = await publisherApi<{ active: number; total: number }>(
        `/api/books/${bid}/platforms/${platform}/records`,
      )
      if (info.active === 0) {
        alert(`【${currentSelectedBook?.name}】在${platformLabel[platform]}没有未发布的排期。`)
        return
      }
      const stillOn = currentSelectedBook?.platforms.find((x) => x.platform === platform)?.enabled
      if (
        !confirm(
          `将清空【${currentSelectedBook?.name} × ${platformLabel[platform]}】的 ${info.active} 条未发布排期（已发布记录保留）。\n\n` +
            (stillOn
              ? '⚠️ 该平台的定时任务仍处于启用状态，清空后后台会在约 20 秒内自动重新排期。若要彻底停止，请用「删除定时任务」。\n\n'
              : '') +
            '确定继续？',
        )
      )
        return
      const res = await publisherApi<{ deleted: number }>(
        `/api/books/${bid}/platforms/${platform}/records`,
        { method: 'DELETE' },
      )
      setStaleInfo((p) => ({ ...p, [platform]: 0 }))
      setTaskMsg(`已清空 ${res.deleted} 条排期`)
      loadData()
    } catch (e) {
      alert(`清空失败：${e instanceof Error ? e.message : String(e)}`)
    }
  }

  /** 删除定时发布任务本身（解绑 + 清掉其未发布排期） */
  const handleUnbind = async (platform: string) => {
    const bid = Number(formBook)
    if (
      !confirm(
        `确定删除【${currentSelectedBook?.name} × ${platformLabel[platform]}】的定时发布任务吗？\n\n` +
          '会同时清掉它尚未发布的排期；已发布记录保留留痕。',
      )
    )
      return
    try {
      const res = await publisherApi<{ removed_binding: boolean; purged_records: number }>(
        `/api/books/${bid}/platforms/${platform}`,
        { method: 'DELETE' },
      )
      setTaskMsg(`已删除定时任务（清理排期 ${res.purged_records} 条）`)
      setStaleInfo((p) => ({ ...p, [platform]: 0 }))
      loadData()
    } catch (e) {
      alert(`删除失败：${e instanceof Error ? e.message : String(e)}`)
    }
  }

  /** 保存字数门槛与组天数。改口径只影响**之后新排的组**，已排期的任务不会自动重排。 */
  const handleSaveSettings = async () => {
    if (!appSettings) return
    const q = Number(appSettings.min_words_qidian)
    const f = Number(appSettings.min_words_fanqie)
    const c = Number(appSettings.chapter_min_words)
    const g = Number(appSettings.schedule_group_days)
    if (!Number.isInteger(q) || q < 0 || !Number.isInteger(f) || f < 0) {
      alert('平台日门槛字数必须是非负整数')
      return
    }
    if (!Number.isInteger(c) || c < 0) {
      alert('单章合格线必须是非负整数')
      return
    }
    if (!Number.isInteger(g) || g < 1 || g > 30) {
      alert('组天数必须是 1–30 的整数')
      return
    }
    setSavingSettings(true)
    try {
      await publisherApi('/api/settings', {
        method: 'POST',
        body: JSON.stringify({ settings: { ...appSettings } }),
      })
      setSettingsMsg('已保存。已排期的任务不会自动重排，如需按新门槛重排请先「清空排期」再排一次。')
      loadData()
    } catch (e) {
      alert(`保存失败：${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setSavingSettings(false)
    }
  }

  const handleCreateTask = async () => {
    if (!formBook) {
      alert('请选择书籍')
      return
    }
    setSubmitting(true)
    try {
      const selectedPlatforms = formPlatform === 'both' ? ['qidian', 'fanqie'] : [formPlatform]
      const platformName: Record<string, string> = { qidian: '起点中文网', fanqie: '番茄小说' }
      const results: string[] = []
      for (const p of selectedPlatforms) {
        const res = await publisherApi<{
          scheduled: number
          quota: number
          chapters_to_write: number
          next_group_shortfall_words: number
        }>(`/api/books/${formBook}/platforms`, {
          method: 'POST',
          body: JSON.stringify({
            platform: p,
            schedule: `每天 ${formTime}`,
            chapters_per_run: 1,
            ai_content: false,
            enabled: true,
          }),
        })
        if (res.scheduled > 0) {
          results.push(`✓ ${platformName[p]}：成功排期 ${res.scheduled} 条（每日门槛 ${res.quota} 字）`)
        } else if (res.next_group_shortfall_words > 0) {
          results.push(
            `⚠ ${platformName[p]}：未排期 — 剩余章节凑不足每日 ${res.quota} 字门槛，还需补约 ${res.next_group_shortfall_words} 字（约 ${res.chapters_to_write} 章）`,
          )
        } else {
          results.push(`⚠ ${platformName[p]}：未排期 — 没有可排的待发章节（章节状态已是 published 或无正文）`)
        }
      }
      const bName = books.find((b) => b.id === Number(formBook))?.name || ''
      alert(`【${bName}】排期结果：\n\n${results.join('\n')}`)
      loadData()
    } catch (e) {
      alert(`创建任务失败：${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setSubmitting(false)
    }
  }

  // 计算选中的书籍信息
  const currentSelectedBook = books.find((b) => b.id === Number(formBook))

  // 计算日历网格
  const firstDay = new Date(calYear, calMonth - 1, 1).getDay()
  const daysInMonth = new Date(calYear, calMonth, 0).getDate()
  const cells: (number | null)[] = []
  for (let i = 0; i < firstDay; i++) cells.push(null)
  for (let d = 1; d <= daysInMonth; d++) cells.push(d)
  while (cells.length % 7 !== 0) cells.push(null)
  const weekdays = ['日', '一', '二', '三', '四', '五', '六']

  const today = new Date()
  const isCurrentMonth = calYear === today.getFullYear() && calMonth === today.getMonth() + 1
  const activeDayData = calendar?.days[String(selectedDay)]

  if (loading) {
    return (
      <div className="h-full flex items-center justify-center text-sm text-zinc-500">正在同步书稿与计划…</div>
    )
  }

  return (
    <div className="h-full overflow-auto p-3 bg-[#141414]">
      <div className="max-w-6xl mx-auto space-y-6">
        {/* 顶部标题栏 */}
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-lg font-bold text-zinc-100 flex items-center gap-2.5">
              <span>自动发布控制中心</span>
              <span className="text-xs font-normal px-2 py-0.5 rounded bg-emerald-950/60 text-emerald-400 border border-emerald-800/40">
                后台守护中
              </span>
            </h1>
            <p className="text-xs text-zinc-400 mt-1">
              起点 × 番茄 双平台定时发布 · 实时关联 projects 本地工程正文存稿
              {isLinked && (
                <span className="ml-2 px-1.5 py-0.5 rounded bg-amber-950/60 text-amber-400 border border-amber-800/40">
                  工作台关联：{currentSelectedBook?.name || linkedBookTitle || linkedBookId || '未选择书籍'}
                  {linkedBookId && linkedPubBookId == null && <span className="ml-1 text-zinc-500">（书架该书未同步到发布库 · 工作台不显示其它书的排期）</span>}
                  {currentSelectedBook && linkedBookId && currentSelectedBook.name !== linkedBookId && (
                    <span className="ml-1 text-zinc-500">· 书架：{linkedBookTitle || linkedBookId}</span>
                  )}
                </span>
              )}
            </p>
          </div>
          <div className="flex items-center gap-3">
            {actionMsg && <span className="text-xs text-amber-400 animate-pulse font-medium">{actionMsg}</span>}
            <button
              onClick={() => loadData()}
              className="px-3.5 py-1.5 rounded-lg text-xs font-medium bg-zinc-800 hover:bg-zinc-700 text-zinc-200 border border-zinc-700 transition"
            >
              🔄 立即同步
            </button>
          </div>
        </div>

        {error && (
          <div className="px-4 py-2.5 rounded-lg bg-red-950/40 border border-red-900 text-sm text-red-400">
            {error}
          </div>
        )}

        {/* 1. 任务监控卡片 */}
        <div>
          <h2 className="text-xs font-semibold text-zinc-400 uppercase tracking-wider mb-3">发布进度追踪</h2>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-3">
            {taskCards.map((c, i) => (
              <TaskCardItem key={i} card={c} />
            ))}
          </div>
        </div>

        {/* 2. 平台会话状态 */}
        <div>
          <h2 className="text-xs font-semibold text-zinc-400 uppercase tracking-wider mb-3">平台登录与会话</h2>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {platforms.map((p) => (
              <PlatformCard
                key={p.id}
                platform={p}
                onLogin={handleLogin}
                isLoggingIn={loggingInPlatform === p.id}
              />
            ))}
          </div>
        </div>

        {/* 3. 撰写与发布排期日历 (带深度交互) */}
        <div>
          <div className="flex items-center justify-between mb-3">
            <div className="flex items-center gap-3">
              <h2 className="text-xs font-semibold text-zinc-400 uppercase tracking-wider">
                发书排期日历
              </h2>
              <span className="text-sm font-bold text-zinc-200 tabular-nums">
                {calYear} 年 {calMonth} 月
              </span>
            </div>
            <div className="flex items-center gap-1.5 bg-zinc-900 border border-zinc-800 rounded-lg p-1">
              <button
                onClick={() => changeMonth(-1)}
                className="px-2 py-0.5 rounded text-xs text-zinc-400 hover:text-white hover:bg-zinc-800 transition"
              >
                ◀ 上月
              </button>
              <button
                onClick={() => {
                  setCalYear(today.getFullYear())
                  setCalMonth(today.getMonth() + 1)
                  setSelectedDay(today.getDate())
                }}
                className="px-2 py-0.5 rounded text-xs text-amber-400 hover:bg-zinc-800 transition font-medium"
              >
                今天
              </button>
              <button
                onClick={() => changeMonth(1)}
                className="px-2 py-0.5 rounded text-xs text-zinc-400 hover:text-white hover:bg-zinc-800 transition"
              >
                下月 <ChevronRight className="inline h-3 w-3" strokeWidth={1.8} aria-hidden />
              </button>
            </div>
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
            {/* 左侧大日历网格 */}
            <div className="lg:col-span-2 rounded-xl border border-zinc-800 bg-zinc-900/50 p-4">
              <div className="grid grid-cols-7 gap-1.5 mb-2">
                {weekdays.map((w) => (
                  <div key={w} className="text-center text-xs text-zinc-500 font-medium py-1">
                    {w}
                  </div>
                ))}
              </div>
              <div className="grid grid-cols-7 gap-1.5">
                {cells.map((day, idx) => {
                  if (day === null) return <div key={idx} className="min-h-[72px] rounded-lg bg-zinc-950/20" />
                  const dayData = calendar?.days[String(day)]
                  const isToday = isCurrentMonth && day === today.getDate()
                  const isSelected = selectedDay === day
                  const hasWrite = dayData?.write
                  const pubList = dayData?.publish || []

                  return (
                    <div
                      key={idx}
                      onClick={() => setSelectedDay(day)}
                      className={`min-h-[72px] rounded-lg border p-1.5 flex flex-col justify-between cursor-pointer transition select-none ${
                        isSelected
                          ? 'border-amber-500 bg-amber-950/30 ring-1 ring-amber-500'
                          : isToday
                          ? 'border-amber-500/50 bg-zinc-900/80'
                          : 'border-zinc-800/80 bg-zinc-900/40 hover:border-zinc-700'
                      }`}
                    >
                      <div className="flex items-center justify-between">
                        <span className={`text-xs font-bold tabular-nums ${isToday ? 'text-amber-400' : isSelected ? 'text-white' : 'text-zinc-400'}`}>
                          {day}
                        </span>
                        {isToday && (
                          <span className="w-1.5 h-1.5 rounded-full bg-amber-400" />
                        )}
                      </div>

                      <div className="flex flex-col gap-1 my-1">
                        {hasWrite && (
                          <div className="text-[10px] text-blue-400 truncate leading-tight flex items-center gap-0.5">
                            <span>✍</span>
                            <span>{hasWrite.chapters.length}章修改</span>
                          </div>
                        )}
                        {pubList.length > 0 && (
                          <div className="flex flex-wrap gap-1">
                            {pubList.map((p, i) => (
                              <span
                                key={i}
                                className={`text-[9px] px-1 py-0.2 rounded font-medium truncate ${
                                  p.status === 'published'
                                    ? 'bg-emerald-950/70 text-emerald-400 border border-emerald-800/50'
                                    : 'bg-amber-950/70 text-amber-400 border border-amber-800/50'
                                }`}
                              >
                                {p.platform_name[0]}{p.chapters[0] ? `·第${p.chapters[0]}章` : ''}
                              </span>
                            ))}
                          </div>
                        )}
                      </div>
                    </div>
                  )
                })}
              </div>
            </div>

            {/* 右侧：所选日期具体排期与实操面板 */}
            <div className="rounded-xl border border-zinc-800 bg-zinc-900/70 p-4 flex flex-col justify-between">
              <div>
                <div className="flex items-center justify-between pb-3 border-b border-zinc-800 mb-3">
                  <h3 className="text-sm font-bold text-zinc-100 flex items-center gap-2">
                    <span>📅 {calMonth}月{selectedDay}日 详细安排</span>
                  </h3>
                  <span className="text-xs text-zinc-400">
                    {isCurrentMonth && selectedDay === today.getDate() ? '今天' : ''}
                  </span>
                </div>

                {/* 写作动态 */}
                {activeDayData?.write && (
                  <div className="mb-3 p-2.5 rounded-lg bg-blue-950/20 border border-blue-900/30 text-xs text-blue-300">
                    <div className="font-semibold mb-1 flex items-center gap-1.5 text-blue-400">
                      <span>✍️ 书稿变动</span>
                      <span>· {activeDayData.write.book_name}</span>
                    </div>
                    <div>当日完成/修改：第 {activeDayData.write.chapters.join('、')} 章</div>
                  </div>
                )}

                {/* 排期任务 */}
                {(!activeDayData?.publish || activeDayData.publish.length === 0) ? (
                  <div className="py-10 text-center text-xs text-zinc-500">
                    该日暂无发布排期
                    <div className="mt-1 text-[11px] text-zinc-600">可在下方「制定任务」为后续自动排期</div>
                  </div>
                ) : (
                  <div className="space-y-2.5">
                    {activeDayData.publish.map((p) => (
                      <div
                        key={p.record_id}
                        className="p-2.5 rounded-lg bg-zinc-800/60 border border-zinc-700/60 flex flex-col gap-1.5"
                      >
                        <div className="flex items-center justify-between">
                          <span
                            className="text-[11px] px-1.5 py-0.5 rounded font-bold"
                            style={{ backgroundColor: `${platformColor[p.platform] || '#888'}25`, color: platformColor[p.platform] || '#bbb' }}
                          >
                            {p.platform_name} · 定时发布
                          </span>
                          <span className={`text-[11px] px-1.5 py-0.5 rounded font-medium ${statusMap[p.status]?.cls || 'text-zinc-400'}`}>
                            {statusMap[p.status]?.label || p.status}
                          </span>
                        </div>
                        <div className="text-xs text-zinc-200 font-medium truncate">{p.chapter_title}</div>
                        <div className="flex items-center justify-between text-[11px] text-zinc-400">
                          <span>拟发时间: {p.time || '未指定'}</span>
                          <div className="flex items-center gap-2">
                            {p.status === 'scheduled' && (
                              <button
                                onClick={() => handlePublishNow(p.record_id)}
                                className="text-amber-400 hover:underline font-medium"
                              >
                                立即触发
                              </button>
                            )}
                            <button
                              onClick={() => handleCancelRecord(p.record_id)}
                              className="text-zinc-500 hover:text-red-400 transition"
                            >
                              移除
                            </button>
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              <div className="pt-3 border-t border-zinc-800 text-[11px] text-zinc-500 text-center">
                💡 后台调度器会在到点前自动执行发布
              </div>
            </div>
          </div>
        </div>

        {/* 4. 制定发书任务与排期 (与真实书籍完全联动) */}
        <div>
          <h2 className="text-xs font-semibold text-zinc-400 uppercase tracking-wider mb-3">
            制定发布任务 · 平台对账
          </h2>
          <div className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-5">
            <div className="grid grid-cols-1 md:grid-cols-4 gap-4 items-end">
              {/* 选择书籍 */}
              <div className="md:col-span-2">
                <label className="block text-xs font-medium text-zinc-300 mb-1.5">
                  选择书稿工程 ({books.length} 本可用)
                  {isLinked && <span className="ml-1.5 font-normal text-[11px] text-amber-400/80">· 与左侧书架联动</span>}
                </label>
                <select
                  value={formBook}
                  onChange={(e) => {
                    const val = e.target.value
                    setFormBook(val ? Number(val) : '')
                    // 工作台内换书也回写全局书架：避免两套选中态口径漂移
                    const hit = books.find((b) => String(b.id) === val)
                    if (hit) onSelectBook?.(hit.name)
                  }}
                  className="w-full px-3 py-2 rounded-lg bg-zinc-800 border border-zinc-700 text-sm text-zinc-100 focus:outline-none focus:border-amber-500"
                >
                  <option value="">-- 请选择在写书籍 --</option>
                  {books.map((b) => (
                    <option key={b.id} value={b.id}>
                      《{b.name}》（共 {b.total_chapters} 章 · {b.pending_count} 章待发）
                    </option>
                  ))}
                </select>
              </div>

              {/* 投放平台 */}
              <div>
                <label className="block text-xs font-medium text-zinc-300 mb-1.5">投放平台</label>
                <select
                  value={formPlatform}
                  onChange={(e) => setFormPlatform(e.target.value as any)}
                  className="w-full px-3 py-2 rounded-lg bg-zinc-800 border border-zinc-700 text-sm text-zinc-100 focus:outline-none focus:border-amber-500"
                >
                  <option value="both">双平台同发 (起点+番茄)</option>
                  <option value="qidian">仅起点中文网</option>
                  <option value="fanqie">仅番茄小说</option>
                </select>
              </div>

              {/* 每日定时时刻 */}
              <div>
                <label className="block text-xs font-medium text-zinc-300 mb-1.5">每日定时时刻</label>
                <input
                  type="time"
                  value={formTime}
                  onChange={(e) => setFormTime(e.target.value)}
                  className="w-full px-3 py-2 rounded-lg bg-zinc-800 border border-zinc-700 text-sm text-zinc-100 focus:outline-none focus:border-amber-500"
                />
              </div>
            </div>

            {currentSelectedBook && (
              <div className="mt-4 pt-4 border-t border-zinc-800">
                <div className="flex items-start justify-between gap-3 mb-2.5">
                  <div>
                    <div className="text-xs font-semibold text-zinc-200">
                      ① 平台已发进度核对
                      <span className="ml-2 font-normal text-[11px] text-zinc-500">
                        未核对即排期会被直接拒绝 —— 否则排期从第 1 章开始，重复发布
                      </span>
                    </div>
                  </div>
                  {taskMsg && (
                    <span className="text-[11px] text-amber-400/90 text-right max-w-md leading-snug shrink-0">
                      {taskMsg}
                    </span>
                  )}
                </div>

                <div className="grid grid-cols-1 md:grid-cols-2 gap-2.5">
                  {(['qidian', 'fanqie'] as const).map((p) => {
                    const bind = currentSelectedBook.platforms.find((x) => x.platform === p)
                    const checked = !!bind?.progress_checked_at
                    const res = checkResult[p]
                    const stale = staleInfo[p] || 0
                    const color = platformColor[p]
                    return (
                      <div key={p} className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-3">
                        <div className="flex items-center justify-between mb-2">
                          <span className="flex items-center gap-2">
                            <span
                              className="w-6 h-6 rounded flex items-center justify-center text-[11px] font-bold"
                              style={{ backgroundColor: `${color}22`, color }}
                            >
                              {p === 'qidian' ? '起' : '番'}
                            </span>
                            <span className="text-xs font-medium text-zinc-200">{platformLabel[p]}</span>
                          </span>
                          {checked ? (
                            <span className="text-[11px] px-1.5 py-0.5 rounded-full bg-emerald-950/50 text-emerald-400 border border-emerald-800/40">
                              已发至第 {bind?.published_up_to ?? 0} 章
                            </span>
                          ) : (
                            <span className="text-[11px] px-1.5 py-0.5 rounded-full bg-amber-950/50 text-amber-400 border border-amber-800/40">
                              未核对 · 禁止排期
                            </span>
                          )}
                        </div>

                        <div className="text-[11px] text-zinc-500 mb-2 leading-snug">
                          {checked ? (
                            <>
                              基准于 {bind?.progress_checked_at}（
                              {bind?.progress_source === 'auto' ? '自动抓取' : '手工录入'}）确立
                            </>
                          ) : bind ? (
                            <>该平台已有定时任务，但尚无对账基准</>
                          ) : (
                            <>该平台尚未创建定时任务</>
                          )}
                        </div>

                        {res && !res.ok && (
                          <div className="text-[11px] text-red-400/90 bg-red-950/30 border border-red-900/50 rounded px-2 py-1 mb-2 leading-snug break-all">
                            {res.error}
                            {res.url ? <div className="text-zinc-500 mt-0.5">落点：{res.url}</div> : null}
                          </div>
                        )}

                        {stale > 0 && (
                          <div className="text-[11px] text-amber-300 bg-amber-950/30 border border-amber-800/50 rounded px-2 py-1 mb-2 leading-snug">
                            ⚠️ 有 {stale} 条排期不高于该基准章号，属重复发布隐患。
                            <button
                              onClick={() => handleClearRecords(p)}
                              className="ml-1 underline hover:text-white"
                            >
                              立即清理
                            </button>
                          </div>
                        )}

                        {manualOpen[p] && (
                          <div className="flex items-center gap-1.5 mb-2">
                            <input
                              type="number"
                              min={0}
                              value={manualChapter[p] ?? ''}
                              onChange={(e) => setManualChapter((prev) => ({ ...prev, [p]: e.target.value }))}
                              placeholder="已发至第几章"
                              className="w-28 px-2 py-1 rounded bg-zinc-800 border border-zinc-700 text-xs text-zinc-100 focus:outline-none focus:border-amber-500"
                            />
                            <button
                              onClick={() => handleManualSave(p)}
                              className="px-2 py-1 rounded text-[11px] bg-emerald-700 hover:bg-emerald-600 text-white transition"
                            >
                              保存基准
                            </button>
                          </div>
                        )}

                        <div className="flex items-center gap-1.5 flex-wrap">
                          <button
                            onClick={() => handleAutoCheck(p)}
                            disabled={reconciling === p}
                            className={`px-2 py-1 rounded text-[11px] border transition ${
                              reconciling === p
                                ? 'bg-zinc-800 text-zinc-500 border-zinc-700 cursor-not-allowed'
                                : 'bg-zinc-800 hover:bg-zinc-700 text-zinc-200 border-zinc-700'
                            }`}
                          >
                            {reconciling === p ? '核对中…' : <span className="inline-flex items-center gap-1"><Search className="h-3 w-3" strokeWidth={1.8} aria-hidden />自动核对</span>}
                          </button>
                          <button
                            onClick={() => setManualOpen((prev) => ({ ...prev, [p]: !prev[p] }))}
                            className="px-2 py-1 rounded text-[11px] bg-zinc-800 hover:bg-zinc-700 text-zinc-200 border border-zinc-700 transition"
                          >
                            ✎ 手工录入
                          </button>
                          {bind && (
                            <>
                              <button
                                onClick={() => handleClearRecords(p)}
                                className="px-2 py-1 rounded text-[11px] bg-zinc-800 hover:bg-zinc-700 text-zinc-300 border border-zinc-700 transition"
                              >
                                清空排期
                              </button>
                              <button
                                onClick={() => handleUnbind(p)}
                                className="px-2 py-1 rounded text-[11px] bg-zinc-800/80 hover:bg-red-950/70 text-zinc-400 hover:text-red-400 border border-zinc-700 hover:border-red-900 transition"
                              >
                                删除定时任务
                              </button>
                            </>
                          )}
                        </div>
                      </div>
                    )
                  })}
                </div>

                <div className="mt-3 pt-3 border-t border-zinc-800 flex items-center justify-between gap-3 text-xs text-zinc-400">
                  <span className="truncate">
                    当前选定：《{currentSelectedBook.name}》· 存稿目录:{' '}
                    <span className="text-zinc-300 font-mono">{currentSelectedBook.draft_dir}</span>
                  </span>
                  <button
                    onClick={handleCreateTask}
                    disabled={submitting}
                    className={`px-5 py-2 rounded-lg text-xs font-bold transition shadow shrink-0 ${
                      submitting
                        ? 'bg-zinc-800 text-zinc-500 cursor-not-allowed'
                        : 'bg-amber-600 hover:bg-amber-500 text-white'
                    }`}
                  >
                    {submitting ? '排期中…' : '🚀 确定排期并激活定时任务'}
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>

        {/* 5. 字数门槛与分组配置 */}
        {appSettings && (
          <div>
            <h2 className="text-xs font-semibold text-zinc-400 uppercase tracking-wider mb-3">
              字数门槛配置
            </h2>
            <div className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-4">
              <div className="grid grid-cols-1 md:grid-cols-5 gap-3 items-end">
                <div>
                  <label className="block text-xs font-medium text-zinc-300 mb-1.5">
                    起点 · 平台日门槛（字）
                  </label>
                  <input
                    type="number"
                    min={0}
                    value={appSettings.min_words_qidian}
                    onChange={(e) => setAppSettings({ ...appSettings, min_words_qidian: e.target.value })}
                    className="w-full px-3 py-2 rounded-lg bg-zinc-800 border border-zinc-700 text-sm text-zinc-100 focus:outline-none focus:border-amber-500"
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium text-zinc-300 mb-1.5">
                    番茄 · 平台日门槛（字）
                  </label>
                  <input
                    type="number"
                    min={0}
                    value={appSettings.min_words_fanqie}
                    onChange={(e) => setAppSettings({ ...appSettings, min_words_fanqie: e.target.value })}
                    className="w-full px-3 py-2 rounded-lg bg-zinc-800 border border-zinc-700 text-sm text-zinc-100 focus:outline-none focus:border-amber-500"
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium text-zinc-300 mb-1.5">
                    单章合格线（字）
                  </label>
                  <input
                    type="number"
                    min={0}
                    value={appSettings.chapter_min_words}
                    onChange={(e) => setAppSettings({ ...appSettings, chapter_min_words: e.target.value })}
                    className="w-full px-3 py-2 rounded-lg bg-zinc-800 border border-zinc-700 text-sm text-zinc-100 focus:outline-none focus:border-amber-500"
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium text-zinc-300 mb-1.5">
                    每组天数
                  </label>
                  <input
                    type="number"
                    min={1}
                    max={30}
                    value={appSettings.schedule_group_days}
                    onChange={(e) => setAppSettings({ ...appSettings, schedule_group_days: e.target.value })}
                    className="w-full px-3 py-2 rounded-lg bg-zinc-800 border border-zinc-700 text-sm text-zinc-100 focus:outline-none focus:border-amber-500"
                  />
                </div>
                <button
                  onClick={handleSaveSettings}
                  disabled={savingSettings}
                  className={`px-5 py-2 rounded-lg text-xs font-bold transition shadow ${
                    savingSettings
                      ? 'bg-zinc-800 text-zinc-500 cursor-not-allowed'
                      : 'bg-amber-600 hover:bg-amber-500 text-white'
                  }`}
                >
                  {savingSettings ? '保存中…' : '保存门槛'}
                </button>
              </div>
              <div className="mt-3 flex items-start justify-between gap-3 text-[11px] text-zinc-500 leading-snug">
                <span>
                  <b className="text-zinc-400">两层口径分开看</b>：① <b className="text-zinc-400">单章</b>
                  {' '}≥ 单章合格线即算达标，章本身没有「不达标」的说法；②
                  {' '}<b className="text-zinc-400">平台发布字数</b>够不上日门槛就把多篇凑在一起发（只可超、不可少）。
                  口径来源：起点官方要求当日 VIP 有效更新 ≥ 4000 字，且单章 1000–1999 字按 1000 字计入；
                  番茄官方单章下限 1000 字（上限 50000）。番茄 6000 按你的更新目标设定。
                </span>
                {settingsMsg && (
                  <span className="text-amber-400 shrink-0 text-right max-w-xs">{settingsMsg}</span>
                )}
              </div>
            </div>
          </div>
        )}

        {/* 6. 任务总览：按「平台 × 书籍」归类，明确区分发布任务与撰写任务 */}
        <div>
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-xs font-semibold text-zinc-400 uppercase tracking-wider">
              发布任务总览 · 按平台 × 书籍归类
            </h2>
            <span className="text-[11px] text-zinc-500">
              每 {groupDays} 天一组 · 章≥合格线即达标，单章够不上平台日门槛就多篇来凑（只可超不可少）
            </span>
          </div>

          {taskGroups.length === 0 ? (
            <div className="rounded-xl border border-zinc-800 bg-zinc-900/50 text-zinc-500 text-xs py-8 text-center">
              还没有任何发布任务。先在上面「制定发布任务」里选书稿 + 平台 + 时间。
            </div>
          ) : (
            <div className="space-y-3">
              {taskGroups.map((g) => {
                const color = platformColor[g.platform] || '#888'
                const totalWords = g.publish_days.reduce((s, d) => s + d.words, 0)
                const totalItems = g.publish_days.reduce((s, d) => s + d.items.length, 0)
                return (
                  <div key={g.id} className="rounded-xl border border-zinc-800 bg-zinc-900/50 overflow-hidden">
                    {/* 组头：明确是哪个平台、哪本书 */}
                    <div className="flex items-center justify-between gap-3 px-4 py-3 bg-zinc-900/80 border-b border-zinc-800">
                      <div className="flex items-center gap-2.5 min-w-0">
                        <span
                          className="w-7 h-7 rounded-lg flex items-center justify-center text-xs font-bold shrink-0"
                          style={{ backgroundColor: `${color}25`, color }}
                        >
                          {g.platform === 'qidian' ? '起' : '番'}
                        </span>
                        <span className="text-sm font-medium text-zinc-100 shrink-0">{g.platform_name}</span>
                        <span className="text-zinc-600">·</span>
                        <span className="text-sm font-bold text-zinc-100 truncate" title={g.book_name}>
                          《{g.book_name}》
                        </span>
                        <span className="text-[11px] text-zinc-500 shrink-0">
                          {g.schedule || '未设定时'}
                          {g.enabled ? '' : '（已停用）'}
                        </span>
                      </div>
                      <div className="flex items-center gap-2 shrink-0">
                        {g.reconciled ? (
                          <span className="text-[11px] px-2 py-0.5 rounded-full bg-emerald-950/50 text-emerald-400 border border-emerald-800/40">
                            基准 已发至第 {g.published_up_to ?? 0} 章
                          </span>
                        ) : (
                          <span className="text-[11px] px-2 py-0.5 rounded-full bg-amber-950/50 text-amber-400 border border-amber-800/40">
                            未核对基准
                          </span>
                        )}
                      </div>
                    </div>

                    <div className="p-4 space-y-4">
                      {/* 发布任务 */}
                      <div>
                        <div className="text-[11px] font-semibold text-zinc-300 mb-2">
                          📤 发布任务
                          <span className="ml-2 font-normal text-zinc-500">
                            共 {totalItems} 篇 · 合计 {totalWords.toLocaleString()} 字 · 平台日门槛{' '}
                            {g.quota.toLocaleString()} 字（单章合格线 {g.chapter_min_words.toLocaleString()} 字）
                          </span>
                        </div>
                        {g.publish_days.length === 0 ? (
                          <div className="text-[11px] text-zinc-500 py-3 text-center bg-zinc-950/30 rounded-lg">
                            暂无排期
                          </div>
                        ) : (
                          <div className="space-y-2">
                            {g.publish_days.map((d) => (
                              <div key={d.date} className="rounded-lg border border-zinc-800/70 bg-zinc-950/30 p-2.5">
                                <div className="flex items-center justify-between mb-2">
                                  <span className="text-[11px] text-zinc-300 tabular-nums">
                                    {d.date} {d.time}
                                    <span className="ml-2 text-zinc-500">
                                      {d.items.length} 篇 · 合计 {d.words.toLocaleString()} 字
                                    </span>
                                  </span>
                                  <span
                                    className={`text-[11px] px-2 py-0.5 rounded-full font-medium ${
                                      d.met
                                        ? 'bg-emerald-950/50 text-emerald-400 border border-emerald-800/40'
                                        : 'bg-red-950/50 text-red-400 border border-red-800/40'
                                    }`}
                                  >
                                    {d.met
                                      ? `✓ 平台发布字数达标（超 ${(d.words - d.quota).toLocaleString()} 字）`
                                      : `需再凑 ${(d.quota - d.words).toLocaleString()} 字`}
                                  </span>
                                </div>
                                <div className="space-y-1.5">
                                  {d.items.map((it) => (
                                    <TaskItemRow
                                      key={it.id}
                                      item={it}
                                      platform={g.platform}
                                      bookName={g.book_name}
                                      chapterMin={g.chapter_min_words}
                                      onRetry={handleRetry}
                                      onPublishNow={handlePublishNow}
                                      onDelete={handleDeleteRecord}
                                    />
                                  ))}
                                </div>
                              </div>
                            ))}
                          </div>
                        )}
                      </div>

                      {/* 撰写任务 */}
                      <div>
                        <div className="text-[11px] font-semibold text-zinc-300 mb-2">
                          ✍️ 撰写任务
                          <span className="ml-2 font-normal text-zinc-500">
                            今日书稿变动 {g.written_today.length} 章 · 待发存稿 {g.pending_chapters} 章 /{' '}
                            {g.pending_words.toLocaleString()} 字
                          </span>
                        </div>
                        {g.written_today.length === 0 ? (
                          <div className="text-[11px] text-zinc-500 px-3 py-2 bg-zinc-950/30 rounded-lg">
                            今日该书没有书稿改动
                          </div>
                        ) : (
                          <div className="flex flex-wrap gap-1.5">
                            {g.written_today.map((w, i) => (
                              <span
                                key={i}
                                className="text-[11px] px-2 py-1 rounded bg-blue-950/30 text-blue-300 border border-blue-900/40"
                              >
                                第{w.chapter_number ?? '?'}章 {w.title}
                                <span className="text-blue-400/60 ml-1">{w.words.toLocaleString()}字</span>
                              </span>
                            ))}
                          </div>
                        )}
                      </div>

                      {/* 字数缺口：只提示，绝不自动凑数字 */}
                      {g.chapters_to_write > 0 && (
                        <div className="text-[11px] text-amber-300 bg-amber-950/25 border border-amber-800/50 rounded-lg px-3 py-2 leading-snug">
                          ⚠️ 下一组还差 <b>{g.next_group_shortfall_words.toLocaleString()}</b> 字才够
                          平台日门槛；按本书均字 {g.avg_chapter_words.toLocaleString()} 字估算，
                          <b>需再写约 {g.chapters_to_write} 篇</b>来凑（按真实字数判定，不自动凑数字）。
                        </div>
                      )}
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
