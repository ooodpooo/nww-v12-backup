import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertTriangle, BookOpen, RefreshCw } from 'lucide-react'
import { api, type Book, type BookDraft, type IgnoredBook } from './api'
import { getToken, getStoredRole, AUTH_CHANGED } from './auth'
import Login from './components/Login'
import AccountCenter from './components/AccountCenter'
import UserMgmt from './components/UserMgmt'
import ActivityBar, { type ActivityId } from './components/ActivityBar'
import ResizablePanels, { type PanelDef } from './components/ResizablePanels'
import AssetPanel from './components/AssetPanel'
import EditorPane from './components/EditorPane'
import TrackingView from './components/TrackingView'
import VerticalSplit from './components/VerticalSplit'
import ChatBox from './components/ChatBox'
import AiLivePanel from './components/AiLivePanel'
import ScrollArea from './components/ScrollArea'
import NovelPublisher from './components/NovelPublisher'
import MvpConsole from './components/MvpConsole'
import ModelCenter from './components/ModelCenter'
import GuardConsole from './components/GuardConsole'
import CreateFlowPanel from './components/CreateFlowPanel'
import type { CreateFlowExternal } from './components/createFlow'

/**
 * 2026-09-06 恢复书架版式（用户要求：本地 5173 = 书架，不是账号管理界面）。
 *
 * 双模式自适应（一套 dist，本地/远程通用）：
 * - 登录后探测 /api/books：有书（本地 NWW_DATA_PLANE=1）→ 书架模式（原版 UI）；
 * - 恒空 {"books":[]}（远程数据面关闭，架构定稿：远程无书）→ 账号面板模式。
 * - 请求失败 → 仍进书架模式并显示「后端不可用」（原版行为）。
 */

/** 2026-11-07 自动发布中心「固定两列」组合开关（默认开）。
 *  VITE_PUBLISH_FIXED_2COL=0 可回滚到旧的「只替换 main 卡内容、仍是三卡骨架」行为。 */
const PUBLISH_FIXED_2COL = import.meta.env.VITE_PUBLISH_FIXED_2COL !== '0'

/** 2026-11-07 千章守护中心「固定两列」组合开关（默认开）。
 *  VITE_GUARD_FIXED_2COL=0 可回滚到旧的「只替换 main 卡内容、仍是三卡骨架」行为
 *  （回滚后 GuardConsole 恢复内置选书器，不读 linkedBookId）。 */
const GUARD_FIXED_2COL = import.meta.env.VITE_GUARD_FIXED_2COL !== '0'

/* ─────────────────────  疑似复制副本告警条（2026-11-07）  ───────────────────── */
/** 后端副本识别闸把「无身份标记 + 标题重复」的目录判为疑似副本并从书架排除（GET /api/books 的 ignored）。
 *  这里只做告知与一键清理：清理 = 逐条 deleteBook → 后端移入回收站（可恢复，不直接删除）。
 *  ignored 为空时不渲染；清理失败的错误就地显示在条内，不弹窗阻塞。 */
function IgnoredBooksBanner({ items, cleaning, error, onClean }: {
  items: IgnoredBook[]
  cleaning: boolean
  error: string | null
  onClean: () => void
}) {
  if (items.length === 0) return null
  const preview = items.slice(0, 3).map((b) => b.title).join('、')
  return (
    <div className="shrink-0 mx-2 mt-2 rounded border border-[#f59e0b]/40 bg-[#f59e0b]/10 px-2 py-1.5">
      <div className="flex items-start gap-2">
        <AlertTriangle className="h-3.5 w-3.5 text-[#f59e0b] mt-0.5 shrink-0" strokeWidth={1.8} aria-hidden />
        <div className="min-w-0 flex-1">
          <div className="text-[12px] leading-relaxed text-[#e0b25a]">
            检测到 {items.length} 个疑似复制副本，已从书架隐藏：{preview}{items.length > 3 ? ' 等' : ''}
          </div>
          {error && (
            <div className="mt-1 text-[11px] leading-relaxed text-[#e0685f] break-words">清理失败：{error}</div>
          )}
        </div>
        <button
          onClick={onClean}
          disabled={cleaning}
          title="移入回收站（可在 .trash 中恢复）"
          className="v14-badge v14-badge-amber shrink-0 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          {cleaning ? '清理中…' : '清理'}
        </button>
      </div>
    </div>
  )
}

/* ─────────────────────  侧边栏：书架（V14 分组行排版）  ───────────────────── */
function SidebarShelf({ books, activeBook, onOpen, onReload }: { books: Book[]; activeBook: string | null; onOpen: (id: string) => void; onReload: () => void }) {
  const uninitializedCount = books.filter((b) => b.initialized === false).length
  // 界面内初始化：按当前登录用户执行，不脱离界面、不暴露路径/命令（根治 CLI 命令串反模式）
  const [initState, setInitState] = useState<Record<string, { busy: boolean; ok?: boolean; msg?: string }>>({})
  const runInit = useCallback(async (b: Book) => {
    if (!b.init_action) return
    setInitState((s) => ({ ...s, [b.id]: { busy: true } }))
    try {
      const r = await api.initializeBook(b.id)
      setInitState((s) => ({ ...s, [b.id]: { busy: false, ok: !!r.ok, msg: r.detail || (r.ok ? '已初始化' : '未能初始化') } }))
      onReload()
    } catch (e: any) {
      setInitState((s) => ({ ...s, [b.id]: { busy: false, ok: false, msg: e?.message || String(e) } }))
    }
  }, [onReload])
  return (
    <div className="h-full flex flex-col">
      <div className="v14-group-title flex items-center justify-between">
        <span>
          {books.length} 本书{uninitializedCount > 0 && <span className="text-[#f59e0b]"> · {uninitializedCount} 本未初始化</span>}
        </span>
        <button
          onClick={onReload}
          title="刷新书架"
          className="text-[#777] hover:text-[#f59e0b] transition-colors"
        >
          <RefreshCw className="h-3.5 w-3.5" strokeWidth={1.8} aria-hidden />
        </button>
      </div>
      <ScrollArea className="flex-1 px-2 pb-3">
        {books.length === 0 && <div className="px-3 py-4 text-[13px] text-[#666]">暂无书</div>}
        <div className="space-y-1">
          {books.map((b) => {
            const active = b.id === activeBook
            const uninitialized = b.initialized === false
            return (
              <button
                key={b.id}
                onClick={() => onOpen(b.id)}
                className={`v14-nav-row items-start ${active ? 'is-active' : ''}`}
              >
                <BookOpen className="v14-nav-icon mt-0.5" strokeWidth={1.8} aria-hidden />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    <span className={`text-[13px] truncate ${active ? 'text-white' : 'text-[#cccccc]'} ${uninitialized ? 'text-[#888]' : ''}`}>{b.title}</span>
                    {uninitialized && (
                      <span className="v14-badge v14-badge-amber" title={b.init_hint}>未初始化</span>
                    )}
                  </div>
                  <div className="text-[11px] text-[#777] mt-0.5 tabular-nums">
                    {b.chapter_count} 章 · {(b.total_chars / 10000).toFixed(1)} 万字
                    {uninitialized && <span className="text-[#f59e0b] ml-1">· 追踪状态缺失</span>}
                  </div>
                  {uninitialized && b.init_hint && (
                    <div className="text-[12px] text-[#666] mt-1 leading-relaxed border-l-2 border-[#f59e0b]/40 pl-2">
                      {b.init_hint}
                      {b.init_action && (
                        <div className="mt-1.5">
                          <button
                            onClick={(e) => { e.stopPropagation(); runInit(b) }}
                            disabled={initState[b.id]?.busy}
                            className="v14-badge v14-badge-amber disabled:opacity-40 disabled:cursor-not-allowed"
                            title="由服务端按当前登录用户重新初始化追踪状态，不触碰正文/大纲/设定"
                          >
                            {initState[b.id]?.busy
                              ? '处理中…'
                              : b.init_action === 'repair_stub' ? '重建追踪状态' : '初始化本书'}
                          </button>
                          {initState[b.id]?.msg && (
                            <div className={`mt-1 text-[11px] leading-relaxed ${initState[b.id]?.ok ? 'text-[#22c55e]' : 'text-[#e0685f]'} break-words`}>
                              {initState[b.id]?.msg}
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              </button>
            )
          })}
        </div>
      </ScrollArea>
    </div>
  )
}

/* 2026-11-07：左侧「查找」按钮及搜索侧栏已下线（导航仅保留创作 / 系统两组）。
   api.search 底层能力保留，暂不提供 UI 入口；如需恢复，从 .bk_navregroup_20261107 取回本组件。 */

/* ─────────────────────  书架模式（本地 5173 主界面，原版 UI）  ───────────────────── */

/** 建书草案 → 「设定/建书摘要.md」全文（八项全量，2026-10-23 外脑终裁 B） */
function buildDraftMd(d: BookDraft): string {
  return [
    '# 建书摘要',
    '',
    `- 书名：${d.title}`,
    `- 类型：${d.genre}`,
    `- 一句话简介：${d.synopsis}`,
    `- 核心命题：${d.core_theme}`,
    `- 主角与金手指：${d.protagonist}`,
    `- 冲突结构：${d.conflict}`,
    `- 差异化：${d.differentiation}`,
    `- 篇幅规划：${d.scale}`,
    '',
  ].join('\n')
}

/** 建书会话持久态（2026-10-23 修复刷新丢会话）：done=false 时刷新恢复原建书会话
 *  （聊天记录/草案卡/重试载荷原样回来）；done=true 或无状态才开全新会话。 */
const CREATE_STATE_KEY = 'nww_v12_create_state'

interface CreateState {
  nonce: number
  done: boolean
  pendingMdRetry?: { bookId: string; draft: BookDraft } | null
  pendingBootstrapRetry?: { bookId: string; draft: BookDraft } | null
}

function loadCreateState(): CreateState | null {
  try {
    const raw = localStorage.getItem(CREATE_STATE_KEY)
    if (!raw) return null
    const s = JSON.parse(raw) as CreateState
    return typeof s.nonce === 'number' ? s : null
  } catch {
    return null
  }
}

/** 刷新位置记忆（2026-10-23 用户拍板：刷新不回默认页）。
 *  sessionStorage 语义 = 刷新存活、新开标签页不留 → 新会话仍默认落新建书籍页。 */
const POS_KEY = 'nww-ui-pos'

interface UiPos {
  activeBook: string | null
  activePath: string | null
  activity: ActivityId
  creatingBook: boolean
}

/** 当前仍存在的导航视图白名单（2026-11-07：「search」已下线）。 */
const VALID_ACTIVITIES: ActivityId[] = ['shelf', 'edit', 'publish', 'mvp', 'model', 'guard', 'account']

function loadPos(): UiPos | null {
  try {
    const raw = sessionStorage.getItem(POS_KEY)
    if (!raw) return null
    const p = JSON.parse(raw) as UiPos
    if (typeof p.creatingBook !== 'boolean') return null
    // 旧标签页可能残留已下线的 activity（如 search）→ 回落「编辑」，
    // 否则主卡标题为 undefined 且三元链会误落到账号中心。
    if (!VALID_ACTIVITIES.includes(p.activity)) p.activity = 'edit'
    return p
  } catch {
    return null
  }
}

function ShelfApp({ initialBooks, onOpenAdmin }: { initialBooks: Book[] | null; onOpenAdmin: () => void }) {
  // 打开站点默认页 = 新建书籍页（仅新会话）；刷新恢复当前位置（书/视图/建书态），不回首页。
  // 建书未完成（done=false）时刷新恢复原会话与重试载荷（不清聊天记录）。
  const [pos] = useState<UiPos | null>(() => loadPos())
  const [createInit] = useState<CreateState>(() => {
    const s = loadCreateState()
    if (s && !s.done) return s
    return { nonce: (s?.nonce ?? 0) + 1, done: false, pendingMdRetry: null, pendingBootstrapRetry: null }
  })
  const [activity, setActivity] = useState<ActivityId>(pos?.activity ?? 'edit')
  const [books, setBooks] = useState<Book[]>(initialBooks ?? [])
  /** 疑似复制副本（后端副本识别闸排除、不进书架的目录）：仅用于告警条展示 */
  const [ignoredBooks, setIgnoredBooks] = useState<IgnoredBook[]>([])
  /** 清理疑似副本进行中：按钮置灰防重复点击 */
  const [cleaningIgnored, setCleaningIgnored] = useState(false)
  /** 清理失败原因：就地显示在告警条内（不 alert 阻塞） */
  const [cleanIgnoredError, setCleanIgnoredError] = useState<string | null>(null)
  const [activeBook, setActiveBook] = useState<string | null>(pos?.activeBook ?? null)
  const [activePath, setActivePath] = useState<string | null>(pos?.activePath ?? null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [treeVersion, setTreeVersion] = useState(0)
  /** 编辑器重载信号：快捷「优化/重写」落盘后递增，EditorPane 重新拉取当前文件 */
  const [editorRefreshKey, setEditorRefreshKey] = useState(0)
  /** 编辑器脏状态（未保存修改）：快捷「优化/重写」据此禁用，防止后台落盘覆盖丢稿 */
  const [editorDirty, setEditorDirty] = useState(false)
  /** 建书会话隔离号：未完成建书时刷新沿用 createInit.nonce（恢复会话）；
   *  已完成或点「＋新建书籍」换新号开全新空会话。无书态 chatKey 随 nonce 隔离 */
  const [createNonce, setCreateNonce] = useState(createInit.nonce)
  /** AI 实况面板：当前正在观察的 agent 任务 + 是否展开（重写/写/润色/修订/审计自动展开）。
   *  持久化到 localStorage：刷新页面后恢复上次任务，记录不丢。 */
  const [liveTask, setLiveTask] = useState<{ id: string; mode: string } | null>(() => {
    try {
      const raw = localStorage.getItem('nww_live_task')
      return raw ? JSON.parse(raw) : null
    } catch { return null }
  })
  const [liveOpen, setLiveOpen] = useState(false)
  const [liveIsRunning, setLiveIsRunning] = useState(false)
  /** 面板打开时若无 taskId，自动从后端恢复最近任务（刷新后不丢记录） */
  useEffect(() => {
    if (!liveOpen || liveTask) return
    let cancelled = false
    api.getRecentAgentTask()
      .then((r) => {
        if (cancelled || !r.task_id) return
        const t = { id: r.task_id, mode: r.mode || '' }
        setLiveTask(t)
        try { localStorage.setItem('nww_live_task', JSON.stringify(t)) } catch {}
      })
      .catch(() => {})
    return () => { cancelled = true }
  }, [liveOpen, liveTask])
  /** 本次建书会话是否全流程完成（完成后再刷新 = 全新会话） */
  const [createDone, setCreateDone] = useState(false)
  /** 建书空白态：新会话默认 true（站点默认页=新建书籍页）；刷新按 pos 恢复。
   *  true 时禁止「自动选第一本书」效应把用户弹回选书态 */
  const [creatingBook, setCreatingBook] = useState(pos ? pos.creatingBook : true)
  /** 书已创建但「建书摘要.md」写入失败的补写载荷（草案卡据此重试，只补写不重建；随 create_state 持久化） */
  const [pendingMdRetry, setPendingMdRetry] = useState(createInit.pendingMdRetry ?? null)
  /** 书已创建但 bootstrap（卷纲/世界观/主角）生成失败的载荷（草案卡据此重试，只重生成不重建；随 create_state 持久化） */
  const [pendingBootstrapRetry, setPendingBootstrapRetry] = useState(createInit.pendingBootstrapRetry ?? null)
  /* 建书流程卡（2026-11-07）：以下 5 项全部是「真实回执」，中间卡只按回执点亮成果，不自行记进度 */
  const [createBusy, setCreateBusy] = useState(false)
  const [createdTitle, setCreatedTitle] = useState<string | null>(null)
  const [mdWritten, setMdWritten] = useState(false)
  const [bootstrapWritten, setBootstrapWritten] = useState<string[]>([])
  const [bootstrapSkipped, setBootstrapSkipped] = useState<string[]>([])
  /** 刷新恢复后的「继续建书」横幅状态（草案卡按终裁 C 不随消息恢复） */
  const [createRetryBusy, setCreateRetryBusy] = useState(false)
  const [createRetryErr, setCreateRetryErr] = useState<string | null>(null)

  const loadBooks = useCallback(() => {
    // 2026-11-07：改用诊断接口同时拿 ignored（疑似复制副本）；旧后端无该字段时由 api 层回落 []
    api.booksWithDiagnostics()
      .then(({ books: bs, ignored }) => {
        setBooks(bs)
        setIgnoredBooks(ignored)
        setLoadError(null)
      })
      .catch((e) => setLoadError(String(e)))
  }, [])

  /** 一键清理疑似副本：逐条 deleteBook（后端移入 .trash，可恢复）。
   *  全部成功 → 清空本地 ignored 并刷新书架；失败 → 错误就地显示在告警条内（不 alert）。
   *  用 allSettled 而非 all：单条失败不阻断其余条目，并保留失败明细。 */
  const cleanIgnoredBooks = useCallback(async () => {
    if (cleaningIgnored || ignoredBooks.length === 0) return
    setCleaningIgnored(true)
    setCleanIgnoredError(null)
    const targets = ignoredBooks
    const results = await Promise.allSettled(targets.map((b) => api.deleteBook(b.id)))
    const failures: string[] = []
    results.forEach((r, i) => {
      if (r.status === 'rejected') {
        const detail = r.reason instanceof Error ? r.reason.message : String(r.reason)
        failures.push(`《${targets[i].title}》${detail}`)
      }
    })
    if (failures.length === 0) {
      setIgnoredBooks([])
    } else {
      const shown = failures.slice(0, 3).join('；')
      setCleanIgnoredError(failures.length > 3 ? `${shown} 等 ${failures.length} 个` : shown)
    }
    // 无论全部成功还是部分失败都刷新诊断：已移入回收站的条目会从 ignored 消失
    loadBooks()
    setCleaningIgnored(false)
  }, [cleaningIgnored, ignoredBooks, loadBooks])

  // 挂载时统一走一次 booksWithDiagnostics：登录探测只用 api.books()（不含 ignored），
  // 若沿用「initialBooks !== null 就不拉」的旧条件，首次进入书架时告警条拿不到疑似副本列表。
  useEffect(() => { loadBooks() }, [loadBooks])

  // 位置落库（sessionStorage）：刷新恢复当前位置；新开标签页无此态 → 默认新建书籍页
  useEffect(() => {
    try {
      sessionStorage.setItem(POS_KEY, JSON.stringify({ activeBook, activePath, activity, creatingBook }))
    } catch { /* ignore */ }
  }, [activeBook, activePath, activity, creatingBook])

  // 清理历史建书会话键：只保留当前会话（恢复态=未完成的原会话，新会话=新 nonce），防无限堆积
  useEffect(() => {
    try {
      const keep = `nww_v12_chat_default_${createNonce}`
      for (let i = localStorage.length - 1; i >= 0; i--) {
        const k = localStorage.key(i)
        if (k && k.startsWith('nww_v12_chat_default_') && k !== keep) localStorage.removeItem(k)
      }
    } catch { /* ignore */ }
  }, [createNonce])

  // 建书会话态落库：nonce/done/重试载荷全程持久化，刷新可恢复
  useEffect(() => {
    try {
      localStorage.setItem(CREATE_STATE_KEY, JSON.stringify({
        nonce: createNonce, done: createDone,
        pendingMdRetry: pendingMdRetry ?? null, pendingBootstrapRetry: pendingBootstrapRetry ?? null,
      }))
    } catch { /* ignore */ }
  }, [createNonce, createDone, pendingMdRetry, pendingBootstrapRetry])

  // 刚创建、尚未出现在 books 列表里的书（列表接口延迟/缓存），保持选中待其出现，避免被自动选书兜底抢选到其它书
  const justCreatedRef = useRef<string | null>(null)

  // books 加载后：若已选书仍在列表中则保持，否则选第一本
  // 建书空白态（creatingBook，站点默认即此态）例外：不自动选书，否则默认新建页会被立刻弹回选书态
  useEffect(() => {
    if (books.length === 0) return
    if (creatingBook) return
    if (activeBook && books.some((b) => b.id === activeBook)) return
    // 刚创建的书可能尚未出现在列表返回值中，保持选中，避免被回退到其它书（一旦出现于列表即解除守卫）
    if (justCreatedRef.current && activeBook === justCreatedRef.current) {
      if (books.some((b) => b.id === justCreatedRef.current)) justCreatedRef.current = null
      return
    }
    setActiveBook(books[0].id)
    setActivePath(null)
  }, [books, activeBook, creatingBook])

  // 一旦回到选书态（任何路径设置了 activeBook），退出建书空白守卫
  useEffect(() => { if (activeBook) setCreatingBook(false) }, [activeBook])

  /** 「＋新建书籍」：进入全新空白建书态（旧会话/旧草案不继承，重试载荷作废）。
   *  2026-11-07 裁决：不与第 2 个「编辑」按钮做关联 —— 不再 setActivity('edit')，
   *  建书态走自己独立的三列布局（create-flow | create-chat），不借用编辑视图状态。 */
  const startCreateBook = useCallback(() => {
    setCreatingBook(true)
    setActiveBook(null)
    setActivePath(null)
    setCreateDone(false)
    setPendingMdRetry(null)
    setPendingBootstrapRetry(null)
    setCreatedTitle(null)
    setMdWritten(false)
    setBootstrapWritten([])
    setBootstrapSkipped([])
    setCreateRetryErr(null)
    setCreateNonce((n) => n + 1)
  }, [])

  /** 草案卡「确认创建」编排：createBook(全量草案) → 写 设定/建书摘要.md → bootstrap 生成卷纲/世界观/主角 → 刷新书架并选中新书。
   *  分段失败：保留已创建书 + 对应重试载荷，返回失败态供草案卡重试（不把创建成功误报为初始化完成）。 */
  const handleCreateBook = useCallback(async (d: BookDraft): Promise<{ ok: boolean; error?: string }> => {
    setCreatedTitle(d.title)
    const finish = (bookId: string) => {
      setCreateDone(true)
      justCreatedRef.current = bookId
      loadBooks()
      setActiveBook(bookId)
      setActivePath(null)
      // 退出建书独立布局后归位到编辑视图：这是「走向新书」的既有行为，
      // 不是把新建书籍按钮与编辑按钮绑定（建书过程中 activity 全程不被读写）。
      setActivity('edit')
      setTreeVersion((v) => v + 1)
    }
    /** bootstrap 调用：成功清重试载荷返回 null；失败记载荷返回错误串（可达数分钟，桥接慢） */
    const runBootstrap = async (bookId: string): Promise<string | null> => {
      try {
        const res = await api.bootstrapBook(bookId)
        if (res.ok) {
          setPendingBootstrapRetry(null)
          setBootstrapWritten(res.written ?? [])
          setBootstrapSkipped(res.skipped ?? [])
          return null
        }
        setPendingBootstrapRetry({ bookId, draft: d })
        return res.error || '生成失败'
      } catch (e) {
        setPendingBootstrapRetry({ bookId, draft: d })
        return e instanceof Error ? e.message : String(e)
      }
    }
    // 重试A：摘要补写（书已建、md 写失败）→ 补写成功后继续 bootstrap
    if (pendingMdRetry && pendingMdRetry.draft.title === d.title) {
      const bookId = pendingMdRetry.bookId
      try {
        await api.writeFile(bookId, '设定/建书摘要.md', buildDraftMd(d))
        setPendingMdRetry(null)
        setMdWritten(true)
      } catch (e) {
        setMdWritten(false)
        return { ok: false, error: `建书摘要补写仍失败：${e instanceof Error ? e.message : String(e)}` }
      }
      const bootErr = await runBootstrap(bookId)
      if (bootErr) { loadBooks(); return { ok: false, error: `书已创建，但开书资料（卷纲/世界观/主角）生成失败（${bootErr}），点击重试生成` } }
      finish(bookId)
      return { ok: true }
    }
    // 重试B：bootstrap 重生成（md 已写、生成失败）
    if (pendingBootstrapRetry && pendingBootstrapRetry.draft.title === d.title) {
      const bookId = pendingBootstrapRetry.bookId
      const bootErr = await runBootstrap(bookId)
      if (bootErr) return { ok: false, error: `开书资料生成仍失败（${bootErr}），点击重试生成` }
      finish(bookId)
      return { ok: true }
    }
    // 正常流程。同名书已存在（上次刷新中断/重复点击）→ 不重建，直接续跑摘要+bootstrap（均幂等）
    let newId: string
    const existing = books.find((b) => b.title === d.title)
    if (existing) {
      newId = existing.id
    } else {
      try {
        const res = await api.createBook(d.title, d.genre || undefined, d.synopsis || undefined, d)
        newId = res.id
      } catch (e) {
        return { ok: false, error: e instanceof Error ? e.message : String(e) }
      }
    }
    try {
      await api.writeFile(newId, '设定/建书摘要.md', buildDraftMd(d))
      setMdWritten(true)
    } catch (e) {
      setMdWritten(false)
      setPendingMdRetry({ bookId: newId, draft: d })
      loadBooks()
      return { ok: false, error: `书已创建，但建书摘要写入失败（${e instanceof Error ? e.message : String(e)}），点击重试补写` }
    }
    const bootErr = await runBootstrap(newId)
    if (bootErr) { loadBooks(); return { ok: false, error: `书已创建，但开书资料（卷纲/世界观/主角）生成失败（${bootErr}），点击重试生成` } }
    finish(newId)
    return { ok: true }
  }, [pendingMdRetry, pendingBootstrapRetry, loadBooks, books])

  /** 刷新恢复后的「继续建书」横幅：草案卡按终裁C不随消息恢复，横幅作为持久化重试载荷的入口 */
  const pendingCreate = pendingMdRetry ?? pendingBootstrapRetry
  const retryPendingCreate = useCallback(async () => {
    if (!pendingCreate || createRetryBusy) return
    setCreateRetryBusy(true)
    setCreateBusy(true)
    setCreateRetryErr(null)
    const r = await handleCreateBook(pendingCreate.draft)
    setCreateBusy(false)
    setCreateRetryBusy(false)
    if (!r.ok) setCreateRetryErr(r.error ?? '继续建书失败')
  }, [pendingCreate, createRetryBusy, handleCreateBook])

  const openBook = (id: string) => {
    setActiveBook(id)
    setActivePath(null)
    setActivity('edit')
  }

  /** 建书聊天框（建书模式全高 / 编辑态底部 dock 复用同一实例配置） */
  const chatBoxEl = (
    <ChatBox bookId={activeBook} activePath={activePath} editorDirty={editorDirty} createNonce={createNonce} createMode={creatingBook}
      onCreateBook={async (d) => {
        setCreateBusy(true)
        try { return await handleCreateBook(d) }
        finally { setCreateBusy(false) }
      }}
      onNavigate={(bookId, path) => { setActiveBook(bookId); setActivePath(path); setActivity('edit'); setTreeVersion((v) => v + 1) }}
      onChapterUpdated={() => { setEditorRefreshKey((k) => k + 1); setTreeVersion((v) => v + 1) }}
      onAgentTaskStarted={(id, m) => { const t = { id, mode: m }; setLiveTask(t); try { localStorage.setItem('nww_live_task', JSON.stringify(t)) } catch {} }} />
  )

  /** 当前书书名：用于追踪卡头「追踪面板 · 书名」 */
  const activeBookTitle = books.find((b) => b.id === activeBook)?.title ?? null

  /** 2026-11-07 写作台工作台（单一实例定义）：三列组合下作为最右「工作台」卡。
   *  书源受控于 activeBook（= 中间书架卡当前书），隐藏内置书列表避免两套选中态；
   *  从书架卡换书会同步换掉这里的细纲/章节/Packet。 */
  const mvpConsoleEl = (
    <MvpConsole
      linkedBookId={activeBook}
      hideBookList
      onSelectBook={(id) => { setActiveBook(id); setActivePath(null) }}
      onOpenFile={(bookId, path) => { setActiveBook(bookId); setActivePath(path); setActivity('edit'); setTreeVersion((v) => v + 1) }}
      onReloadBooks={loadBooks}
    />
  )

  /** 2026-11-07 自动发布工作台（单一实例）：内容区两张独立卡中的最右「工作台」卡。
   *  与写作台同款：书源受控于 activeBook（= 中间书架卡当前书），按目录名精确匹配发布库；
   *  在工作台内换书也会回写全局 activeBook，保持书架高亮 / 工作台 / 排期表单同源。 */
  const publishConsoleEl = (
    <NovelPublisher
      linkedBookId={activeBook}
      linkedBookTitle={activeBookTitle}
      onSelectBook={(id) => {
        // 发布库含书架之外的书（旧工程 / 非 v12 目录）：只回写书架里确实存在的书。
        // 否则 App 的「已选书不在列表 → 选第一本」兜底会把 activeBook 改到别的书，造成状态跳变。
        if (books.some((b) => b.id === id)) { setActiveBook(id); setActivePath(null) }
      }}
    />
  )

  /** 2026-11-07 千章守护中心（单一受控实例）：书源恒等于 activeBook（= 中间书架卡当前书）。
   *  书架换书 → linkedBookId 变化 → overview / runs / story-risk / risk 全部按新 book_id 重拉
   *  （后端 /api/guard/* 按 book_id 隔离，见 guard.py:206/235/456/628）；内置选书器隐藏，
   *  全页只保留书架卡一个选书入口，避免两套选中态。 */
  const guardConsoleEl = (
    <GuardConsole
      books={books}
      activeBook={activeBook}
      linkedBookId={activeBook}
      onReloadBooks={loadBooks}
      onOpenFile={(bookId, path) => { setActiveBook(bookId); setActivePath(path); setActivity('edit'); setTreeVersion((v) => v + 1) }}
      onOpenRun={(taskId, mode) => { setLiveTask({ id: taskId, mode }); setLiveOpen(true) }}
    />
  )

  /* ★ 单一页面骨架（2026-11-05 用户裁决）：三张卡片 id 恒为 sidebar|main|right。
     点最左列按钮只替换 main 卡的内容，不再切整页、不再把左栏换成书架/搜索/模型等
     不同资源、不再重建布局。id 恒定 → ResizablePanels 的 panelSignature 不变 →
     列宽/顺序/折叠态跨视图保留，左侧两列形态永远统一。
     例外（2026-11-07 新裁决）：「写作台」按钮固定关联自己的两卡组合（isMvpLayout）；
     「自动发布」按钮固定关联「icon 工具列 | 书架卡 | 工作台卡」三独立卡 / 内容区两列（isPublishLayout）。 */
  const sidebarPanel: PanelDef = {
    id: 'sidebar',
    title: '资产',
    shortLabel: '资产',
    // 左二列恒为同一张资产卡（V14 分组目录），任何视图都不替换。
    // 2026-11-07：资产卡顶部挂「疑似复制副本」告警条（ignored 非空才出现）；AssetPanel 内部渲染逻辑不变。
    content: (
      <div className="h-full flex flex-col min-h-0">
        {ignoredBooks.length > 0 && (
          <IgnoredBooksBanner
            items={ignoredBooks}
            cleaning={cleaningIgnored}
            error={cleanIgnoredError}
            onClean={cleanIgnoredBooks}
          />
        )}
        <div className="flex-1 min-h-0">
          <AssetPanel books={books} activeBook={activeBook} activePath={activePath} treeVersion={treeVersion} onSelectBook={(id) => { setActiveBook(id); setActivePath(null) }} onNavigate={setActivePath} onReloadBooks={loadBooks} />
        </div>
      </div>
    ),
    designWidth: 320,
    bg: '#141414',
    hideHeader: true,
  }

  /** 中间主卡的标题：视图切换只改标题与内容，卡片本身（id/宽度/位置）不变 */
  const MAIN_TITLE: Record<ActivityId, string> = {
    edit: '正文', publish: '自动发布', mvp: 'MVP 写作台', guard: '千章守护',
    model: '模型', shelf: '书架', account: '账号',
  }
  const MAIN_SHORT: Record<ActivityId, string> = {
    edit: '正文', publish: '发布', mvp: '写作台', guard: '守护',
    model: '模型', shelf: '书架', account: '账号',
  }

  /** 主卡内容：原来「全屏整页」的 MVP 写作台 / 千章守护 / 账号中心全部落进这张卡片。
   *  千章守护分支仅在 VITE_GUARD_FIXED_2COL=0 回滚时生效：保留内置选书器与三卡骨架；
   *  默认态由 isGuardLayout 的 guardWorkbenchPanel 渲染受控版 guardConsoleEl。 */
  /* 2026-11-07：建书态改走独立三列布局（create-flow | create-chat），
     主卡此分支不再渲染，也不再挂第二个 ChatBox 实例；「继续建书」横幅移到流程卡。 */
  const mainContent = creatingBook
    ? null
    : activity === 'edit'
      ? activeBook
        ? <EditorPane key={activeBook} bookId={activeBook} path={activePath ?? undefined} readFirst={activePath?.startsWith('正文/') ?? false} onNavigate={setActivePath} onDeleted={() => { setTreeVersion((v) => v + 1); loadBooks() }} onSaved={() => setTreeVersion((v) => v + 1)} refreshKey={editorRefreshKey} onDirtyChange={setEditorDirty} />
        : <div className="h-full flex flex-col items-center justify-center gap-2 text-[14px] text-[#555] bg-[#141414]">
            <div>尚未选择书籍</div>
            <div className="text-[12px] text-[#666]">点最左列顶部「＋」新建书籍，与助手聊天即可建书</div>
          </div>
      : activity === 'publish'
        ? <NovelPublisher />
      : activity === 'mvp'
        ? mvpConsoleEl
        : activity === 'guard'
            ? <GuardConsole
                books={books}
                activeBook={activeBook}
                onReloadBooks={loadBooks}
                onOpenFile={(bookId, path) => { setActiveBook(bookId); setActivePath(path); setActivity('edit'); setTreeVersion((v) => v + 1) }}
                onOpenRun={(taskId, mode) => { setLiveTask({ id: taskId, mode }); setLiveOpen(true) }}
              />
            : activity === 'model'
              ? <ModelCenter />
              : activity === 'shelf'
                  ? <SidebarShelf books={books} activeBook={activeBook} onOpen={openBook} onReload={loadBooks} />
                  : <AccountCenter onOpenAdmin={onOpenAdmin} />

  const mainPanel: PanelDef = {
    id: 'main',
    title: creatingBook ? '新建书籍 · 建书助手' : MAIN_TITLE[activity],
    shortLabel: creatingBook ? '建书' : MAIN_SHORT[activity],
    content: mainContent,
    designWidth: null,
    fill: true,
    hideHeader: true,
    bg: '#141414',
    // 正文视图保留 800px 文本块硬底线；其它控制台视图不设硬宽，避免三卡骨架被撑破
    minWidth: !creatingBook && activity === 'edit' ? 800 : 0,
  }

  // 右栏：上下两栏 —— 上「追踪面板 · 书名」/ 下「AI写作助手」，可上下拖拽分割。
  // 骨架恒定；建书态下主卡已全高打开建书助手，右栏下栏改占位避免渲染第二个 ChatBox 实例。
  const rightPanel: PanelDef = {
    id: 'right',
    title: '助手',
    shortLabel: '助手',
    content: (
      <VerticalSplit
        storageKey="nww_v12_right_split_v2"
        topTitle={activeBookTitle ? `追踪面板 · ${activeBookTitle}` : '追踪面板'}
        bottomTitle="AI写作助手"
        initialRatio={0.5}
        top={activeBook ? <TrackingView key={activeBook} bookId={activeBook} /> : <div className="h-full flex items-center justify-center text-[14px] text-[#666]">请先选择书</div>}
        bottom={creatingBook
          ? <div className="h-full flex items-center justify-center p-4 text-center text-[12px] text-[#666] leading-relaxed">建书助手已在中间卡片打开</div>
          : chatBoxEl}
      />
    ),
    designWidth: 480,
    bg: '#141414',
    hideHeader: true,
    unframed: true,
    dragStripOverlay: true, // 拖拽条悬浮：首张卡片顶到面板上沿，上边不留 6px 空隙
  }

  /* ★ 2026-11-07 用户裁决：写作台（创作组第 3 个按钮）= 一组固定关联卡片。
     点它自动打开「最左 icon 工具列 | 中间 书架卡 | 最右 工作台」三列，
     工作台书源恒等于书架当前书（activeBook）；点书架里的书只换书、不跳编辑页。
     其余视图仍走 11-05 单一页面骨架（sidebar|main|right），三卡 id/顺序/宽度/折叠态不变。
     每组布局的 panelSignature 不同 → ResizablePanels 各自持久化，互不覆盖。
     自动发布（创作组第 4 个按钮）同理（2026-11-07 裁决，含 15:03 澄清）：
     最左 icon 工具列 + 中间书架卡 + 最右工作台卡 = 三个独立卡片，内容区固定 2 列；
     中间与右侧必须是两张独立卡（各有自己的边框/宽度/折叠态），不合并成一整张。
     工作台书源同样恒等于 activeBook。
     千章守护（系统组第 3 个按钮）同款「icon 工具列 | 书架卡 | 守护中心卡」三独立卡（2026-11-07 裁决）：
     点书架里的书只换书、不跳编辑页，守护中心全部研判按该书 book_id 重拉。 */
  const isMvpLayout = !creatingBook && activity === 'mvp'
  const isPublishLayout = PUBLISH_FIXED_2COL && !creatingBook && activity === 'publish'
  const isGuardLayout = GUARD_FIXED_2COL && !creatingBook && activity === 'guard'

  const mvpShelfPanel: PanelDef = {
    id: 'mvp-shelf',
    title: '书架',
    shortLabel: '书架',
    // 复用现成 V14 书架行列表：当前书高亮 / 刷新 / 未初始化提示，与全局 activeBook 同源
    content: <SidebarShelf books={books} activeBook={activeBook} onOpen={(id) => { setActiveBook(id); setActivePath(null) }} onReload={loadBooks} />,
    designWidth: 300,
    bg: '#141414',
    hideHeader: true,
  }

  const mvpWorkbenchPanel: PanelDef = {
    // 独立 id：不继承正文主卡的历史折叠态，进入写作台必定整卡展开（同时仍可拖拽调宽）
    id: 'mvp-workbench',
    title: '工作台',
    shortLabel: '工作台',
    content: mvpConsoleEl,
    designWidth: null,
    fill: true,
    hideHeader: true,
    bg: '#141414',
    minWidth: 0, // 工作台自带左栏+主区，不套用正文 800px 硬底线
  }

  /** 自动发布「书架」卡：与右侧工作台卡彼此独立（各自边框 / 宽度 / 折叠 / 持久化）。
   *  复用 V14 书架行列表，与全局 activeBook 同源；点书只换书、不跳编辑页。 */
  const publishShelfPanel: PanelDef = {
    id: 'publish-shelf',
    title: '书架',
    shortLabel: '书架',
    content: <SidebarShelf books={books} activeBook={activeBook} onOpen={(id) => { setActiveBook(id); setActivePath(null) }} onReload={loadBooks} />,
    designWidth: 300,
    bg: '#141414',
    hideHeader: true,
  }

  /** 自动发布「工作台」卡：独立 id，不继承正文主卡历史折叠态；仍可拖拽调宽。 */
  const publishWorkbenchPanel: PanelDef = {
    id: 'publish-workbench',
    title: '工作台',
    shortLabel: '工作台',
    content: publishConsoleEl,
    designWidth: null,
    fill: true,
    hideHeader: true,
    bg: '#141414',
    minWidth: 0, // 发布控制台自带多列栅格，不套用正文 800px 硬底线
  }

  /** 千章守护「书架」卡：与右侧守护中心卡彼此独立（各自边框 / 宽度 / 折叠 / 持久化）。
   *  复用 V14 书架行列表，与全局 activeBook 同源；点书只换书、不跳编辑页。 */
  const guardShelfPanel: PanelDef = {
    id: 'guard-shelf',
    title: '书架',
    shortLabel: '书架',
    content: <SidebarShelf books={books} activeBook={activeBook} onOpen={(id) => { setActiveBook(id); setActivePath(null) }} onReload={loadBooks} />,
    designWidth: 300,
    bg: '#141414',
    hideHeader: true,
  }

  /** 千章守护「守护中心」卡：独立 id，不继承正文主卡历史折叠态；仍可拖拽调宽。 */
  const guardWorkbenchPanel: PanelDef = {
    id: 'guard-workbench',
    title: '千章守护中心',
    shortLabel: '守护中心',
    content: guardConsoleEl,
    designWidth: null,
    fill: true,
    hideHeader: true,
    bg: '#141414',
    minWidth: 0, // 守护中心自带三栏百分比栅格，不套用正文 800px 硬底线
  }

  /* ★ 2026-11-07 裁决：「新建书籍」（创作组第 1 个按钮）不与第 2 个「编辑」按钮关联。
     固定三列 = 最左 icon 工具列 | 中间 建书流程实况卡 | 最右 建书 chatbox。
     建书态不读写 activity、不渲染资产卡/追踪面板、不复用 sidebar|main|right 骨架；
     独立 panel id → 与写作台 / 自动发布 / 正文三栏的列宽、顺序、折叠态各自持久化。 */
  const createFlowExternal: CreateFlowExternal = {
    directionId: null, // 由流程卡从 localStorage 读取本会话选择后自行合并
    done: createDone,
    createdTitle: createdTitle ?? pendingCreate?.draft.title ?? null,
    mdWritten: mdWritten || (createDone && !pendingMdRetry),
    written: bootstrapWritten,
    skipped: bootstrapSkipped,
    busy: createBusy || createRetryBusy,
    error: createRetryErr ?? (pendingCreate
      ? `建书未完成：《${pendingCreate.draft.title}》${pendingMdRetry ? '建书摘要待补写' : '开书资料待生成'}`
      : null),
  }

  const createFlowPanel: PanelDef = {
    id: 'create-flow',
    title: '建书流程',
    shortLabel: '流程',
    content: <CreateFlowPanel key={createNonce} nonce={createNonce} external={createFlowExternal} onRetry={pendingCreate ? retryPendingCreate : undefined} />,
    // 固定像素宽：不随窗口缩放、也不继承其它布局的百分比宽度（「固定三列」语义）
    // designWidth=null 避免初始化逻辑再给它算一份百分比，与 fixedWidth 冲突
    designWidth: null,
    fixedWidth: 420,
    bg: '#141414',
    hideHeader: true,
  }

  const createChatPanel: PanelDef = {
    id: 'create-chat',
    title: '建书助手',
    shortLabel: '建书',
    content: (
      <div className="h-full flex flex-col min-h-0">
        <div className="shrink-0 flex items-center justify-between gap-2 card-pad-x py-2 border-b border-[#252525]">
          <span className="text-[12px] text-[#ddd]">建书助手</span>
          <span className="text-[11px] text-[#777]">与中间流程卡实时联动</span>
        </div>
        <div className="flex-1 min-h-0">{chatBoxEl}</div>
      </div>
    ),
    designWidth: null,
    fill: true,
    hideHeader: true,
    bg: '#141414',
    minWidth: 0,
  }

  const panels: PanelDef[] = creatingBook
    ? [createFlowPanel, createChatPanel]
    : isMvpLayout
      ? [mvpShelfPanel, mvpWorkbenchPanel]
      : isPublishLayout
        ? [publishShelfPanel, publishWorkbenchPanel]
        : isGuardLayout
          ? [guardShelfPanel, guardWorkbenchPanel]
          : [sidebarPanel, mainPanel, rightPanel]

  return (
    /* 骨架：最左图标栏 + 卡片区永远在同一层，共五套独立布局：
       ① 新建书籍（2026-11-07）：icon 列 | 建书流程实况卡 | 建书 chatbox，与「编辑」按钮解耦；
       ② 写作台：icon 列 | 书架卡 | 工作台；③ 自动发布：icon 列 | 书架卡 | 工作台卡；
       ④ 千章守护：icon 列 | 书架卡 | 守护中心卡；
       ⑤ 其余导航：11-05 单一骨架 sidebar|main|right，只替换 main 卡内容。
       五组 panelSignature 不同 → 列宽/顺序/折叠态各自持久化，互不覆盖。 */
    <div className="h-full w-full flex p-3 gap-3 bg-black box-border overflow-hidden">
      <ActivityBar active={activity} onChange={(id) => {
        setCreatingBook(false)
        setActivity(id)
        // 写作台 / 自动发布 / 千章守护：点按钮即自动关联书架当前书籍；尚无当前书时取第一本，工作台不空转
        if ((id === 'mvp' || (PUBLISH_FIXED_2COL && id === 'publish') || (GUARD_FIXED_2COL && id === 'guard')) && !activeBook && books.length > 0) {
          setActiveBook(books[0].id); setActivePath(null)
        }
      }} error={loadError} onCreateBook={startCreateBook} creating={creatingBook} liveOpen={liveOpen} liveActive={liveIsRunning} onToggleLive={() => setLiveOpen((o) => !o)} />
      <ResizablePanels panels={panels} />
      <AiLivePanel taskId={liveTask?.id ?? null} mode={liveTask?.mode} open={liveOpen} onOpenChange={setLiveOpen} onRunningChange={setLiveIsRunning} currentBookId={activeBook} />
    </div>
  )
}

/* ─────────────────────  主应用：登录门 + 双模式  ───────────────────── */

type Mode = 'detecting' | 'shelf' | 'account'

export default function App() {
  const [authed, setAuthed] = useState(() => !!getToken())
  const [mode, setMode] = useState<Mode>('detecting')
  const [initialBooks, setInitialBooks] = useState<Book[] | null>(null)
  const [showUserMgmt, setShowUserMgmt] = useState(false)
  const isAdmin = getStoredRole() === 'admin'

  const sync = () => setAuthed(!!getToken())

  useEffect(() => {
    window.addEventListener(AUTH_CHANGED, sync)
    return () => window.removeEventListener(AUTH_CHANGED, sync)
  }, [])

  // 登录后探测数据面：有书=书架（本地），恒空=账号面板（远程）
  useEffect(() => {
    if (!authed) { setMode('detecting'); setInitialBooks(null); return }
    let dead = false
    api.books()
      .then((bs) => { if (!dead) { setInitialBooks(bs); setMode(bs.length > 0 ? 'shelf' : 'account') } })
      .catch(() => { if (!dead) { setInitialBooks(null); setMode('shelf') } })
    return () => { dead = true }
  }, [authed])

  if (!authed) {
    return (
      <div className="h-full w-full bg-[#0d0d0d]">
        <Login onLoggedIn={sync} />
      </div>
    )
  }

  if (mode === 'detecting') {
    return (
      <div className="h-full w-full bg-[#0d0d0d] flex items-center justify-center">
        <span className="text-[14px] text-[#666]">加载中…</span>
      </div>
    )
  }

  if (mode === 'account') {
    return (
      <>
        <AccountCenter onOpenAdmin={() => setShowUserMgmt(true)} />
        {showUserMgmt && isAdmin && <UserMgmt onClose={() => setShowUserMgmt(false)} />}
      </>
    )
  }

  // 书架模式（本地）：账号中心已是 main 卡内容；用户管理仍是独立模态弹窗
  return (
    <>
      <ShelfApp initialBooks={initialBooks} onOpenAdmin={() => setShowUserMgmt(true)} />
      {showUserMgmt && isAdmin && <UserMgmt onClose={() => setShowUserMgmt(false)} />}
    </>
  )
}
