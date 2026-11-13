import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, type Book, type GateSnapshot, type GuardFuseChapterState } from '../api'

/* ─────────────────────  千章守护中心（P0 影子控制台 + 风险修复）  ─────────────────────
 * 三层展示，数据与视觉分离：
 *  - 工程能力（系统级常规指标）：维度 mode + env 开关 + 运行健康，固定展示全部指标
 *    （健康/低/中/高危都显示；修复后只变色不消失），风险项在详情区给修复动作。
 *  - 作品层（本书内容）：正文/追踪真实资产（空章/重复/同质/复读/节奏/追踪），带修复动作。
 *  - 运行记录：runtime/agent_tasks 落盘事实，只读。
 */

const MODE_META: Record<string, { label: string; color: string }> = {
  active: { label: '硬闸生效', color: '#22c55e' },
  partial: { label: '已接线·不全', color: '#f59e0b' },
  advisory: { label: '仅提示', color: '#3b82f6' },
  shadow: { label: '已埋点·未启用', color: '#9ca3af' },
  gap: { label: '未实现/未验收', color: '#ef4444' },
  disabled: { label: '默认关闭', color: '#6b7280' },
}

const RISK_META: Record<string, { label: string; color: string }> = {
  high: { label: '高危', color: '#ef4444' },
  medium: { label: '中风险', color: '#f59e0b' },
  low: { label: '低风险', color: '#3b82f6' },
  ok: { label: '健康', color: '#22c55e' },
}

const LAYER_META: Record<string, { title: string; color: string }> = {
  L0: { title: 'L0 整体生命层', color: '#a78bfa' },
  L1: { title: 'L1 跨章连续性层', color: '#38bdf8' },
  L2: { title: 'L2 单章质量层', color: '#34d399' },
  L3: { title: 'L3 工程稳定层', color: '#fb923c' },
}

const MODE_LABEL: Record<string, string> = {
  rewrite: '重写', write: '写章', polish: '润色', deslop: '去AI味', revise: '修订', audit: '一致性审计',
}

// 工程层 / 故事层的视觉强调色（严格区分，一眼可辨）
const ENG_ACCENT = '#38bdf8'
const STORY_ACCENT = '#fb923c'

type Dim = {
  id: string; layer: string; name: string; prevents: string
  stage: string; mode: string; evidence: string; packet_key: string | null; note: string
  runtime_verified?: boolean; runtime_reason?: string; declared_mode?: string
  action?: RiskAction | null
}
type Run = {
  task_id: string; mode: string | null; book_id: string | null; chapter_id: number | null
  status: string | null; stage_count: number; last_stage: string | null; error: string | null; mtime: number
}
type RiskAction = {
  code: string; label: string; risk?: string; confirm?: boolean
  command?: string; note?: string; task_id?: string; task_ids?: string[]; mode?: string; path?: string
  chapter_id?: number
  /** 仅 gate_rollback：回滚到的快照章号（必须 < 当前章号） */
  to_chapter?: number
}
type RiskItem = { level: string; title: string; detail: string; suggestion: string; action?: RiskAction | null }
/** 工程能力常规指标：kind=dim（守护维度）| env（运行期开关）；level 含 ok，用于「修复后仍展示状态」 */
type Metric = {
  id: string; kind: 'dim' | 'env'; layer?: string; name: string; level: string
  mode?: string; detail: string; suggestion: string; action?: RiskAction | null
}
type RiskResp = {
  overall: { level: string; label: string; summary: string }
  counts: { high: number; medium: number; low: number; ok?: number }
  system: RiskItem[]
  system_metrics?: Metric[]
  book: RiskItem[]
  dimensions: Array<{ id: string; name: string; layer: string; level: string; reason: string; suggestion: string; module?: string; mode?: string; action?: RiskAction | null }>
}
type FoundationSnapshot = {
  ok: boolean
  code: string | null
  summary: string
  bible_present: boolean
  bootstrap_available: boolean
  missing_for_bootstrap: string[]
  missing_extras: string[]
  status: Record<string, string>
}
type StoryResp = {
  overall: { level: string; label: string; summary: string }
  metrics: { chapter_count?: number; avg_chars?: number; min_chars?: number; max_chars?: number }
  checks: RiskItem[]
  /** 2026-11-07：checks=[] 有两种含义——「四项都跑了且没问题」和「一项都没跑」。
   *  assessable=false 属后者，必须显示「无法研判」，绝不假绿。 */
  assessable?: boolean
  assess_reason?: string
  missing_assets?: Array<{ key: string; label: string; state: 'missing' | 'thin' | 'ok' }>
  foundation?: FoundationSnapshot | null
}

function timeAgo(ts: number): string {
  const d = Math.max(0, Math.floor(Date.now() / 1000 - ts))
  if (d < 60) return `${d}秒前`
  if (d < 3600) return `${Math.floor(d / 60)}分前`
  if (d < 86400) return `${Math.floor(d / 3600)}时前`
  return `${Math.floor(d / 86400)}天前`
}

/** 复制到剪贴板：优先 Clipboard API（带超时，避免窗口未聚焦时 Promise 悬挂），
 *  失败回退 execCommand（非聚焦窗口/非安全上下文） */
async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await Promise.race([
        navigator.clipboard.writeText(text),
        new Promise((_r, rej) => setTimeout(() => rej(new Error('clipboard timeout')), 800)),
      ])
      return true
    }
  } catch { /* 继续走回退 */ }
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    ta.style.pointerEvents = 'none'
    document.body.appendChild(ta)
    ta.focus()
    ta.select()
    const ok = document.execCommand('copy')
    ta.remove()
    return ok
  } catch {
    return false
  }
}

function RiskBadge({ level }: { level: string }) {
  const m = RISK_META[level] ?? RISK_META.low
  return (
    <span className="px-1.5 py-0.5 rounded text-[11px] font-medium" style={{ color: m.color, background: `${m.color}1a`, border: `1px solid ${m.color}55` }}>{m.label}</span>
  )
}

/* ─────────────────────  解决方案目录（2026-11-07）：把「只有建议」补成「能理解 + 能点」  ─────────────────────
   后端 /api/guard/story-risk 里部分检查只返回 suggestion、没有 action。此处按检查标题做确定性补全：
   ① 后端已给 action 的项一律优先用后端声明（避免口径漂移）；
   ② 补出的动作全部映射到**已存在**的生产 API（polish / acceptance-settle / bootstrap / open-file），不臆造能力；
   ③ 同时给一段「为什么这么修 / 会做什么 / 有什么影响」的白话说明。
────────────────────────────────────────────────────────────────────── */
/** action=null：有可理解的方案，但当前**没有真实可执行的写动作**。
 *  宁可明说「这一步要作者补基础设定」，也不派发点了必然失败的假按钮。 */
type Solution = { plan: string; impact: string; action: RiskAction | null }

function chapterFromText(text: string, fallback: number): number {
  const m = /第\s*0*(\d+)\s*章/.exec(text)
  const n = m ? parseInt(m[1], 10) : fallback
  return n > 0 ? n : 1
}

function deriveSolutionBase(item: RiskItem, chapterCount: number | undefined): Solution | null {
  const ch = chapterFromText(`${item.detail} ${item.title}`, chapterCount ?? 1)
  if (item.title.includes('段落过碎') || item.title.includes('段落过长')) {
    const focus = item.title.includes('过碎')
      ? '合并碎句、补足场景与动作，避免电报体；保持原剧情与人物不变'
      : '按动作/视角/情绪切分长段，避免大段堆砌；保持原剧情与人物不变'
    return {
      plan: `这一项说的是正文的「段落形状」不达标，不是内容缺失。做法：对第 ${ch} 章跑一次局部修订，把「${focus}」作为修订要求注入写作链的 rewrite 与 deslop 两个环节，让模型在保住剧情的前提下重排段落；跑完仍要过原有质量闸，不达标不会落盘。`,
      impact: `会先备份第 ${ch} 章旧稿再重跑生产链，耗时数分钟；失败不会覆盖原稿。修完这一章后，其余章可逐章重跑同一动作。`,
      action: { code: 'polish_chapter', label: `一键修段落（第${ch}章）`, risk: 'medium', confirm: true, chapter_id: ch, note: focus },
    }
  }
  if (item.title.includes('逐章记录覆盖不足')) {
    return {
      plan: `逐章记录是章节 commit 时写下的跨章比对样本，缺了它跨章一致性检查就拿不到样本。做法：对第 ${ch} 章跑「验收提交」——不重写正文、不降标准，只补跑 enforce → 一致性检测 → 审稿闭环 → commit，由 commit 生成该章追踪记录。`,
      impact: '不改动一个字的正文；若质量闸或一致性闸未通过会被 BLOCK，界面原样显示后端原因，绝不绕闸。',
      action: { code: 'acceptance_settle', label: `补建逐章记录（第${ch}章）`, risk: 'medium', confirm: true, chapter_id: ch },
    }
  }
  if (item.title.includes('无 _tracking-state')) {
    // 2026-11-07 纠错：bootstrap 只生成开书资料 md，**不创建** _tracking-state.json，
    // 旧版「一键重建追踪状态」是点了达不到宣称效果的假按钮。此处只给可理解方案，
    // 不派发动作（真正创建该文件的是建书 init / 首次 commit，当前无界面内写端点）。
    return {
      plan: '这本书还没有权威追踪状态，跨章一致性、伏笔、数值锚全是盲区。要恢复：先由作者补齐基础设定档案（bible.json）与开书资料，再走建书 init 生成第 0 章空状态；之后写章并 commit，状态才会逐章累积。注意「补全开书资料」只写设定/大纲下的 md 文件，不会创建这个文件。',
      impact: '本页不提供一键写动作：目前没有能安全创建权威追踪状态的界面内端点，造一个点了必然失败的按钮只会误导。补齐前置资产后，运行记录与研判会在刷新时恢复。',
      action: null,
    }
  }
  if (item.title.includes('开书资料缺失或过薄')) {
    return {
      plan: '世界观 / 卷纲 / 主角卡是跨章一致性的事实锚；缺了它们，后面章节的设定容易各写各的。做法：点下方「一键补全开书资料」，按这本书已有的 bible.json 生成缺失或过薄的开书资料（幂等，已存在的文件不覆盖）。系统不会替作者发明设定——bible.json 里没有的设定仍要由作者补充。',
      impact: '只写设定 / 大纲目录下的 Markdown 文档，不改动正文、不创建权威追踪状态。执行回执会列出真实写入与跳过的文件；补完本页自动重拉研判，这一项应消失。缺 bible.json 时本按钮不会出现，因为那样执行必然失败。',
      action: { code: 'bootstrap_book', label: '一键补全开书资料', risk: 'medium', confirm: true },
    }
  }
  if (item.title.includes('全局状态未填充')) {
    return {
      plan: '为什么会空：追踪/全局状态.md 是从权威状态 _tracking-state.json 的 global 段渲染出来的；这个字段只由每章 commit 的 global_change 增量写入，而机械草稿默认不替模型猜全局状态。存量书在「global_change 回流」修好之前提交，增量一直是 null，于是 global 恒为空、视图恒为「尚未写入」；跨章位置/阶段没有承载体，写到第 10 章以后还会被内容门拦住。怎么做：点下面的「一键补建全局状态基线」——系统把「已提交的权威事实」（当前卷 / 故事时间 / 场景 / 截至章）投影成基线写入 global，再用生产渲染器重建 全局状态.md 和 上下文.md，最后跑生产校验。不调用模型、不编造剧情。补建后，后续章节正常 commit 产出的 global_change 会继续增量丰富它（发动机类型 / 势力 / 下一切换点属于创作决策，由写作链补，不在这里代写）。',
      impact: '只改权威状态的 global 段并重建派生视图；写前自动唯一化备份、全程与正式 commit 同一把锁、写后校验，校验不过自动回滚。不改动正文、大纲、细纲与执行回执。本页会直接显示写入了哪些字段、修订号变化、备份名和校验结论，不需要切到别的界面。',
      action: { code: 'rebuild_global_state', label: '一键补建全局状态基线', risk: 'medium', confirm: true },
    }
  }
  return null
}

/** 后端已声明 action 时优先用后端动作（避免口径漂移），但解决方案说明仍然照常展示。
 *  2026-11-07：此前「有 action 就不给 solution」导致「全局状态未填充」只有导航按钮、没有方案。 */
function deriveSolution(item: RiskItem, chapterCount: number | undefined, existingAction?: RiskAction | null): Solution | null {
  const base = deriveSolutionBase(item, chapterCount)
  if (!base) return null
  return existingAction ? { ...base, action: existingAction } : base
}

/* 2026-11-07 用户裁决：解锁动作不得在界面显示终端命令或服务端绝对路径。
   路径一律由后端按登录 user_id 解析，前端只传 book_id + chapter（见 api.guardFuseBaseline）。 */
function RiskCard({ item, solution, onFix, busy, copied }: {
  item: RiskItem; solution?: Solution | null; onFix?: (a: RiskAction, s?: Solution | null) => void; busy?: boolean; copied?: boolean
}) {
  const m = RISK_META[item.level] ?? RISK_META.low
  const action = item.action ?? solution?.action ?? null
  return (
    <div className="rounded border border-[#2a2a2a] bg-[#0f0f0f] overflow-hidden">
      <div className="h-0.5" style={{ background: m.color }} />
      <div className="p-2.5">
        <div className="flex items-center gap-2 mb-1">
          <RiskBadge level={item.level} />
          <span className="text-[13px] font-medium text-[#e5e5e5] flex-1">{item.title}</span>
        </div>
        <div className="text-[12px] text-[#999] leading-relaxed mb-1.5">{item.detail}</div>
        <div className="flex gap-1.5 items-start mb-2">
          <span className="text-[11px] text-[#f59e0b] shrink-0 mt-0.5">建议</span>
          <span className="text-[12px] text-[#cfcfcf] leading-relaxed">{item.suggestion}</span>
        </div>
        {solution && (
          <div className="mb-2 rounded border border-[#38bdf833] bg-[#38bdf808] px-2 py-1.5">
            <div className="text-[11px] font-semibold text-[#38bdf8] mb-1">解决方案 · 怎么做</div>
            <div className="text-[12px] text-[#d5d5d5] leading-relaxed">{solution.plan}</div>
            <div className="text-[11px] text-[#999] leading-relaxed mt-1">影响：{solution.impact}</div>
          </div>
        )}
        {action && onFix && (
          <div className="flex items-center gap-2">
            <button
              onClick={() => onFix(action, solution)}
              disabled={busy}
              className="px-2.5 py-1 text-[12px] rounded border border-[#f59e0b55] text-[#f59e0b] hover:bg-[#f59e0b15] disabled:opacity-50"
            >
              {busy ? '执行中…' : copied ? '已复制 ✓' : action.label}
            </button>
            {action.note && !solution && <span className="text-[10px] text-[#666]">{action.note}</span>}
          </div>
        )}
        {!action && (
          <div className="text-[11px] text-[#666]">
            {solution
              ? '这一步的前置资产属于作者创作决策，系统不代写；本页不显示点了必然无效的按钮，补齐后会自动出现可点入口。'
              : '该项为只读观察项，处置方式见上方解决方案与证据定位。'}
          </div>
        )}
      </div>
    </div>
  )
}

/** 分层区块容器：左侧色条 + 标题 + 总徽章，工程层蓝 / 故事层橙，严格区分 */
function RiskSection({ accent, title, subtitle, badge, children }: {
  accent: string; title: string; subtitle: string; badge?: string | null; children: React.ReactNode
}) {
  return (
    <div className="rounded-lg border overflow-hidden" style={{ borderColor: `${accent}33`, background: `${accent}08` }}>
      <div className="px-3 py-2 flex items-center gap-2.5 border-b" style={{ borderColor: `${accent}2e`, background: `${accent}0f` }}>
        <span className="w-2.5 h-2.5 rounded-sm" style={{ background: accent }} />
        <span className="text-[13px] font-semibold" style={{ color: accent }}>{title}</span>
        {badge && <RiskBadge level={badge} />}
        <span className="text-[11px] text-[#666] truncate">{subtitle}</span>
      </div>
      <div className="p-3">{children}</div>
    </div>
  )
}

/** 当前重写锁状态（只读，来自生产 --check）；与历史运行记录严格分开 */
type FuseState = {
  available: boolean
  chapters: GuardFuseChapterState[]
  any_locked: boolean
}

/** 单书的书相关研判快照：id 与数据同源，换书时按 id 失效，绝不串显上一本书的内容 */
type BookSnapshot = {
  id: string
  overview: { tracking_present: boolean; chapter_count: number; dimension_mode_counts: Record<string, number>; guard_module_enabled: boolean }
  runs: Run[]
  story: StoryResp
  fuse: FuseState | null
  /** V13 道闸事实卡 / 待裁决章（2026-11-13）：与 fuse 同款按书 id 失效的快照 */
  gate: GateSnapshot | null
}

export default function GuardConsole({
  books,
  activeBook,
  linkedBookId,
  hideBookList,
  onSelectBook,
  onReloadBooks,
  onOpenFile,
  onOpenRun,
}: {
  books: Book[]
  activeBook: string | null
  /** 2026-11-07 千章守护三列组合：外层书架卡受控的当前书 id。
   *  传入（含 null）时书源 = 书架当前书，不再维护内置选中态。 */
  linkedBookId?: string | null
  /** 外层已有独立书架卡时隐藏内置书籍下拉，避免两套选中态口径漂移 */
  hideBookList?: boolean
  /** 内置选书回写全局 activeBook（受控态不使用） */
  onSelectBook?: (id: string) => void
  onReloadBooks: () => void
  onOpenFile: (bookId: string, path: string) => void
  onOpenRun: (taskId: string, mode: string) => void
}) {
  /** 受控态（传了 linkedBookId，含 null）书源由外层书架决定；
   *  未传时保留旧「内置下拉选书」行为，兼容 11-05 三卡骨架 / 回滚开关。 */
  const isLinked = linkedBookId !== undefined
  const [internalBookId, setInternalBookId] = useState<string | null>(activeBook)
  const bookId = isLinked ? (linkedBookId ?? null) : internalBookId
  const hideSelect = hideBookList || isLinked
  const currentBookTitle = books.find((b) => b.id === bookId)?.title ?? null
  const [registry, setRegistry] = useState<{ dimensions: Dim[]; layers: Record<string, string>; counts: Record<string, Record<string, number>>; note?: string } | null>(null)
  /** 书相关三块数据一次性写入带 id 的快照：渲染时只有 id 匹配当前书才可见，
   *  换书瞬间不会残留上一本书的 overview / 运行记录 / 故事层研判。 */
  const [bookSnap, setBookSnap] = useState<BookSnapshot | null>(null)
  const overview = bookSnap?.id === bookId ? bookSnap.overview : null
  const runs = bookSnap?.id === bookId ? bookSnap.runs : []
  const story = bookSnap?.id === bookId ? bookSnap.story : null
  /** 异步响应防串：换书后旧请求/旧轮询的返回一律丢弃（ref 同步 effect 声明在 loadBook 之前） */
  const bookIdRef = useRef(bookId)
  const [risk, setRisk] = useState<RiskResp | null>(null)
  const [selected, setSelected] = useState<Dim | null>(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [regErr, setRegErr] = useState<string | null>(null)
  const [fixBusy, setFixBusy] = useState(false)
  const [fixNote, setFixNote] = useState<string | null>(null)
  const [copiedCmd, setCopiedCmd] = useState<string | null>(null)
  /** 内嵌二次确认：替代浏览器原生 confirm（原生框会被抑制/一闪而过，取消时静默无反馈） */
  const [pending, setPending] = useState<{ action: RiskAction; solution: Solution | null } | null>(null)
  /** 动作结果（成功/失败 + 后端原文）：常驻显示，直到下一次动作才替换 */
  const [fixResult, setFixResult] = useState<{ ok: boolean; title: string; detail: string; taskId?: string } | null>(null)
  /** 正在跟踪终态的异步任务 id（用于「处理中」提示与防重复点击） */
  const [tracking, setTracking] = useState<string | null>(null)
  /** 本页刚解开的锁：服务端复核为「未锁住」时登记，供「已解除」状态卡使用。
   *  不能只靠历史 fuse 运行记录决定要不要显示绿卡——有些书的运行记录已被清理/换端解锁，
   *  那样点完解锁会「红卡消失了但结果条说已更新为已解除」，界面却没有任何状态卡。 */
  const [fuseResolved, setFuseResolved] = useState<{ bookId: string; chapter: number } | null>(null)

  const selectBook = useCallback((id: string | null) => {
    if (id === bookId) return
    if (isLinked) { if (id) onSelectBook?.(id) }
    else setInternalBookId(id)
  }, [bookId, isLinked, onSelectBook])

  const loadRegistry = useCallback(() => {
    setRegErr(null)
    api.guardRegistry().then((r) => {
      setRegistry({ dimensions: r.dimensions, layers: r.layers, counts: r.counts, note: r.note })
      if (r.dimensions.length) setSelected((s) => s ?? r.dimensions[0])
    }).catch((e) => {
      const msg = String(e)
      setRegErr(/not found|404/i.test(msg) ? '守护接口返回 404（后端未挂载 /api/guard/*）' : msg)
    })
  }, [])

  useEffect(() => { loadRegistry() }, [loadRegistry])

  // ref 同步必须先于下方 loadBook effect 执行（React 按声明顺序跑 effect），
  // 保证换书当次 loadBook 的防串判定用的是新 bookId
  // 「刚解锁」证据自带 bookId，用的时候比对即可；无需在 effect 里 setState 清空（会多一次渲染 + lint 告警）
  useEffect(() => { bookIdRef.current = bookId }, [bookId])

  const loadBook = useCallback(() => {
    setLoading(true)
    const alive = () => bookIdRef.current === bookId
    // 工程能力（系统级常规指标）与选书无关：未选书也要完整拉取
    api.guardRisk(bookId)
      .then((r) => { if (alive()) setRisk(r) })
      .catch((e) => { if (alive()) setErr(String(e)) })
    if (!bookId) {
      setBookSnap(null); setLoading(false); return
    }
    // 第四个请求是「当前重写锁状态」：独立于历史运行记录，解锁动作不删回执，
    // 所以卡片判定必须看这个真实落盘状态，否则点完解锁卡片永远不更新。
    // 第五个是 V13 道闸事实卡 / 待裁决章：与 fuse 同款单独降级，失败保留上次快照。
    Promise.all([
      api.guardOverview(bookId),
      api.guardRuns(bookId, 50),
      api.guardStoryRisk(bookId),
      api.guardFuseState(bookId).catch((): FuseState | null => null),
      api.guardGateDecisions(bookId).catch((): GateSnapshot | null => null),
    ])
      .then(([ov, rs, st, fs, gs]) => {
        if (!alive()) return
        // fuse 请求单独降级：失败时保留上一次已读到的锁态，避免刚解锁完因为一次
        // 网络抖动又回退到「历史回执 → 仍锁着」的旧判定。
        const nextFuse: FuseState | null = fs
          ? { available: fs.available, chapters: fs.chapters, any_locked: fs.any_locked }
          : null
        const nextGate: GateSnapshot | null = gs
          ? { available: gs.available, mode: gs.mode, chapters: gs.chapters,
              pending_count: gs.pending_count, note: gs.note }
          : null
        setBookSnap((prev) => ({
          id: bookId, overview: ov, runs: rs.runs, story: st,
          fuse: nextFuse ?? (prev && prev.id === bookId ? prev.fuse : null),
          gate: nextGate ?? (prev && prev.id === bookId ? prev.gate : null),
        }))
        setErr(null)
      })
      .catch((e) => { if (alive()) setErr(String(e)) })
      .finally(() => { if (alive()) setLoading(false) })
  }, [bookId])

  useEffect(() => { loadBook() }, [loadBook])

  useEffect(() => {
    const t = setInterval(() => {
      const alive = () => bookIdRef.current === bookId
      api.guardRisk(bookId).then((r) => { if (alive()) setRisk(r) }).catch(() => {})
      if (bookId) {
        api.guardRuns(bookId, 50).then((rs) => {
          if (!alive()) return
          setBookSnap((prev) => prev && prev.id === bookId ? { ...prev, runs: rs.runs } : prev)
        }).catch(() => {})
        // 别的端解锁/新回执触发重锁时，本页也要跟着变（状态始终以落盘为准）
        api.guardFuseState(bookId).then((fs) => {
          if (!alive()) return
          setBookSnap((prev) => prev && prev.id === bookId
            ? { ...prev, fuse: { available: fs.available, chapters: fs.chapters, any_locked: fs.any_locked } }
            : prev)
        }).catch(() => {})
        // 写/改动作在别的面板跑完也会产生新闸事实，本页 5s 内跟着刷新
        api.guardGateDecisions(bookId).then((gs) => {
          if (!alive()) return
          setBookSnap((prev) => prev && prev.id === bookId
            ? { ...prev, gate: { available: gs.available, mode: gs.mode, chapters: gs.chapters,
                pending_count: gs.pending_count, note: gs.note } }
            : prev)
        }).catch(() => {})
      }
    }, 5000)
    return () => clearInterval(t)
  }, [bookId])

  const grouped = useMemo(() => {
    const m: Record<string, Dim[]> = {}
    for (const d of registry?.dimensions ?? []) (m[d.layer] ??= []).push(d)
    return m
  }, [registry])

  const metrics = risk?.system_metrics ?? []
  const metricStats = useMemo(() => {
    const c: Record<string, number> = { high: 0, medium: 0, low: 0, ok: 0 }
    for (const m of metrics) c[m.level] = (c[m.level] ?? 0) + 1
    let worst = 'ok'
    const order = ['ok', 'low', 'medium', 'high']
    for (const m of metrics) if (order.indexOf(m.level) > order.indexOf(worst)) worst = m.level
    return { counts: c, worst }
  }, [metrics])
  const riskById = useMemo(() => {
    const m: Record<string, { level: string; reason: string; suggestion: string; action?: RiskAction | null }> = {}
    for (const d of risk?.dimensions ?? []) m[d.id] = { level: d.level, reason: d.reason, suggestion: d.suggestion, action: d.action ?? null }
    for (const x of risk?.system_metrics ?? []) {
      if (x.kind === 'env') m[x.id] = { level: x.level, reason: x.detail, suggestion: x.suggestion, action: x.action ?? null }
    }
    return m
  }, [risk])

  /** 点击工程能力指标：维度 → 左侧详情；运行期开关 → 合成一条只读详情 */
  const selectMetric = (m: Metric) => {
    if (m.kind === 'dim') {
      const d = registry?.dimensions.find((x) => x.id === m.id)
      if (d) setSelected({ ...d, action: m.action ?? null })
      return
    }
    setSelected({
      id: m.id, layer: 'ENV', name: m.name, prevents: m.detail,
      stage: '系统级运行开关', mode: '', evidence: '运行时环境快照（os.environ，只读）',
      packet_key: null, note: m.suggestion, action: m.action ?? null,
    })
  }

  const latestRun = runs.find((r) => r.status === 'running') ?? runs[0]
  const worstOf = (ly: string): string => {
    const order = ['ok', 'low', 'medium', 'high']
    let worst = 'ok'
    for (const d of grouped[ly] ?? []) {
      const lvl = riskById[d.id]?.level ?? 'ok'
      if (order.indexOf(lvl) > order.indexOf(worst)) worst = lvl
    }
    return worst
  }

  /** 历史运行记录：只用来给「查看被拦任务」入口，**不再**用来判断当前是否还锁着。
   *  2026-11-07 修复：解锁动作按设计不删改执行回执，历史 fuse 记录永远存在；
   *  若拿它当锁态判据，点完解锁卡片会永远显示「重写被熔断锁住」。
   *  runs 是每次渲染派生的新数组，这里直接 find（最多 50 条），不套 useMemo 以免依赖恒变。 */
  const fuseRun = runs.find((r) => r.last_stage === 'fuse' || (r.mode === 'rewrite' && r.status === 'done')) ?? null
  const fuseRunChapter = fuseRun?.chapter_id ?? 1
  const fuse = bookSnap?.id === bookId ? bookSnap.fuse : null
  /** V13 道闸事实卡：同样按书 id 失效，换书不留上一本的裁决 */
  const gate = bookSnap?.id === bookId ? bookSnap.gate : null
  /** 状态读取失败时回退到历史记录判定，并在卡片上标注「当前状态未知」，绝不假绿 */
  const fuseKnown = !!fuse && fuse.available
  const lockedFuse = fuseKnown ? (fuse!.chapters.find((c) => c.locked) ?? null) : null
  const fuseChapter = lockedFuse?.chapter ?? fuseRunChapter
  const fuseFallback = !fuseKnown && !!fuseRun
  const showFuseLocked = !!bookId && (!!lockedFuse || fuseFallback)
  const resolvedChapter = fuseResolved?.bookId === bookId ? fuseResolved.chapter : null
  const clearedChapter = resolvedChapter ?? fuseRunChapter
  const clearedFuse = fuseKnown ? (fuse!.chapters.find((c) => c.chapter === clearedChapter && !c.locked) ?? null) : null
  /** 已解锁：不再显示「解除重写锁」按钮，改为可核实的绿色状态卡（读的是落盘基线）。
   *  依据二选一：①历史上确有过被拦任务（刷新后仍稳定显示）；②本页刚执行了解锁/同步且服务端复核通过。
   *  绝不因为「没有历史回执」就把刚解开的状态藏起来。 */
  const showFuseCleared = !!bookId && !showFuseLocked && !!clearedFuse && (!!fuseRun || resolvedChapter != null)
  /** 2026-11-07：「没有检查项」≠「检查通过」。0 章 / 缺基础资产时一律显示「无法研判」，
   *  绝不把 checks=[] 渲染成绿色健康。资产口径由后端复用生产体检给出，前端不自造判据。 */
  const assessBlocked = !!story && story.assessable === false
  const foundation = story?.foundation ?? null
  const canBootstrap = !!foundation?.bible_present && !!foundation?.bootstrap_available
  const missingAssets = story?.missing_assets ?? []

  /** 异步任务终态跟踪：只「已发起」不问结果，正是「只提示不解决」的根因。
   *  轮询到 done/error/interrupted 就把后端判词写进结果条，最长跟踪 6 分钟。 */
  const trackTask = useCallback((taskId: string, label: string) => {
    setTracking(taskId)
    let n = 0
    const tick = async () => {
      n += 1
      try {
        const t = await api.getAgentTask(taskId)
        if (t.status !== 'running') {
          setTracking(null)
          const ok = t.status === 'done' && t.ok !== false
          setFixResult({
            ok,
            title: `${label} · ${ok ? '已完成' : '未通过'}`,
            detail: t.phase_detail || t.error || t.phase || '（后端未提供详情）',
            taskId,
          })
          loadBook()
          return
        }
      } catch { /* 网络抖动：继续轮询，不误报失败 */ }
      if (n < 240) window.setTimeout(tick, 1500)
      else {
        setTracking(null)
        setFixResult({ ok: false, title: `${label} · 仍在运行`, detail: '任务超过 6 分钟未结束，可在 AI 实况继续观察；本次不再自动刷新结果条', taskId })
      }
    }
    window.setTimeout(tick, 1200)
  }, [loadBook])

  /** 动作返回后立刻用「服务端复核的当前锁态」更新快照，卡片当帧就变；
   *  状态仍来自后端生产 --check，不是前端自己打勾（防假绿）。 */
  const applyFuseAfter = useCallback((after?: GuardFuseChapterState | null) => {
    if (!after || !after.available || !bookId) return
    if (!after.locked) setFuseResolved({ bookId, chapter: after.chapter })
    setBookSnap((prev) => {
      if (!prev || prev.id !== bookId || !prev.fuse) return prev
      const chapters = [...prev.fuse.chapters.filter((c) => c.chapter !== after.chapter), after]
        .sort((x, y) => x.chapter - y.chapter)
      return { ...prev, fuse: { available: true, chapters, any_locked: chapters.some((c) => c.locked) } }
    })
  }, [bookId])

  /** 裁决动作返回后立刻用「服务端重读的落盘快照」更新卡片，前端不自己打勾（防假绿）。 */
  const applyGateAfter = useCallback((after?: GateSnapshot | null) => {
    if (!after || !bookId) return
    setBookSnap((prev) => prev && prev.id === bookId ? { ...prev, gate: after } : prev)
  }, [bookId])

  /** 风险修复动作真正执行：bootstrap/cancel/copy/open/polish/settle/rewrite/裁决；执行后自动重拉研判 */
  const execFix = useCallback(async (a: RiskAction) => {
    setFixBusy(true)
    setFixNote(null)
    setFixResult(null)
    setPending(null)
    try {
      if (a.code === 'bootstrap_book') {
        if (!bookId) return
        const r = await api.bootstrapBook(bookId)
        setFixResult({
          ok: r.ok,
          title: `重建追踪状态 · ${r.ok ? '已完成' : '失败'}`,
          detail: r.ok
            ? `已写入：${(r.written ?? []).join('、') || '（无新增）'}${r.skipped?.length ? `；跳过已存在：${r.skipped.join('、')}` : ''}`
            : (r.error ?? '后端未给出原因'),
        })
      } else if (a.code === 'cancel_task') {
        const ids = (a.task_ids && a.task_ids.length ? a.task_ids : a.task_id ? [a.task_id] : [])
        let cancelled = 0
        let stale = 0
        const failed: string[] = []
        for (const id of ids) {
          try {
            const r = await api.cancelAgentTask(id)
            if (r.cancelled) cancelled += 1
            if (r.stale) stale += 1
          } catch { failed.push(id) }
        }
        const bits: string[] = []
        if (cancelled) bits.push(`已终止 ${cancelled} 个任务`)
        if (stale) bits.push(`${stale} 个僵尸任务记录已回写终态`)
        if (!cancelled && !stale) bits.push(`已请求取消 ${ids.length} 个任务（协作式退出）`)
        if (failed.length) bits.push(`${failed.length} 个失败：任务记录已不存在`)
        setFixResult({ ok: cancelled + stale > 0, title: '取消僵尸任务', detail: bits.join('；') + (stale ? ' — 风险项刷新后会自动消失' : '') })
      } else if (a.code === 'copy_command' && a.command) {
        const ok = await copyText(a.command)
        if (ok) {
          setCopiedCmd(a.command)
          setFixResult({ ok: true, title: '启动命令已复制', detail: `${a.command} — 复制不改变运行中的进程；写入启动环境并重启后端后，本项才会消失` })
        } else {
          setFixResult({ ok: false, title: '复制失败', detail: `浏览器拒绝剪贴板访问，请手动复制：${a.command}` })
        }
      } else if (a.code === 'unlock_fuse' && a.chapter_id != null) {
        if (!bookId) return
        const r = await api.guardFuseBaseline(bookId, a.chapter_id)
        applyFuseAfter(r.after)
        const stillLocked = !!(r.after?.available && r.after.locked)
        setFixResult({
          ok: !!r.ok && !stillLocked,
          title: stillLocked ? `解除重写锁（第 ${a.chapter_id} 章）· 复核仍未通过` : `已解除重写锁（第 ${a.chapter_id} 章）`,
          detail: [
            `历史失败回执 ${r.receipts_total ?? '—'} 份已登记为基线（此前基线 ${r.previous_baseline ?? '未登记'}），登记后有效计数 ${r.effective ?? 0}。`,
            r.after?.available
              ? `动作后生产复核：${stillLocked ? '仍被锁住' : '已通过'}；当前有效回执 ${r.after.receipts ?? '—'} / 上限 ${r.after.max_rewrites ?? 3}，基线 ${r.after.baseline ?? 0}。${r.after.reason ?? ''}`
              : '动作后生产复核不可用，本页会在刷新时重新读取落盘状态。',
            stillLocked
              ? '登记计数基线没能解除这一次的锁，卡片保持红卡；请按卡片给出的真实原因选择对应入口。'
              : '卡片已按生产复核的当前锁态更新为「已解除」；历史执行回执按设计保留，不会被删除。',
            r.note ?? '',
          ].filter(Boolean).join('\n'),
        })
      } else if (a.code === 'sync_fuse_hash' && a.chapter_id != null) {
        if (!bookId) return
        const r = await api.guardFuseSyncHash(bookId, a.chapter_id)
        applyFuseAfter(r.after)
        const stillLocked = !!(r.after?.available && r.after.locked)
        setFixResult({
          ok: !!r.ok && !stillLocked,
          title: stillLocked ? `同步细纲前提（第 ${a.chapter_id} 章）· 复核仍未通过` : `已同步细纲前提（第 ${a.chapter_id} 章）`,
          detail: [
            `已把当前细纲登记为新的前提基线${r.invalidated_previous ? '（旧前提已作废）' : ''}，并按生产语义重置该章计数基线。`,
            r.after?.available
              ? `动作后生产复核：${stillLocked ? '仍被锁住' : '已通过'}；当前有效回执 ${r.after.receipts ?? '—'} / 上限 ${r.after.max_rewrites ?? 3}。${r.after.reason ?? ''}`
              : '动作后生产复核不可用，本页会在刷新时重新读取落盘状态。',
            stillLocked ? '仍未解除，卡片保持红卡并显示真实原因。' : '卡片已按当前锁态更新为「已解除」。',
            r.note ?? '',
          ].filter(Boolean).join('\n'),
        })
      } else if (a.code === 'rebuild_global_state') {
        if (!bookId) return
        const r = await api.guardRebuildGlobalState(bookId)
        const fields = (r.fields ?? []).join('、') || '（无需新增）'
        const pos = r.position ?? {}
        const posText = [
          pos.volume ? `当前卷：${pos.volume}` : '',
          pos.scene ? `场景：${pos.scene}` : '',
          pos.story_time ? `故事时间：${pos.story_time}` : '',
        ].filter(Boolean).join('；')
        const title = r.action === 'already_ok' ? '全局状态 · 无需改动'
          : r.action === 'views_rebuilt' ? '全局状态 · 派生视图已重建'
            : '全局状态 · 基线已补建'
        setFixResult({
          ok: r.ok && !!r.verified,
          title,
          detail: [
            r.note,
            `写入字段：${fields}${posText ? `（依据：${posText}）` : ''}`,
            `截至第 ${r.chapter ?? '—'} 章；状态修订 ${r.revision_before ?? '—'} → ${r.revision_after ?? '—'}；重建派生视图 ${r.views_written ?? 0} 个`,
            `写入前备份：${r.backup_name || '（本次无需写盘）'}；目标校验：${r.verified ? '通过' : '未通过'}；生产完整校验：${r.check_ok === false ? '未通过' : '通过'}`,
            r.action === 'already_ok' ? '' : '补建完成后本页会自动重拉研判，这一项应消失；若仍在，说明权威状态与视图仍未对齐，可点结果条看到的具体结论再排查。',
          ].filter(Boolean).join('\n'),
        })
      } else if (a.code === 'rewrite_chapter' && a.chapter_id != null) {
        if (!bookId) return
        const r = await api.agentsRewriteAsync(bookId, a.chapter_id)
        setFixNote(`已发起重写第 ${a.chapter_id} 章；正在跟踪后台终态，结果会显示在本页（也可在 AI 实况看逐环节）`)
        onOpenRun(r.task_id, 'rewrite')
        trackTask(r.task_id, `重写第 ${a.chapter_id} 章`)
      } else if (a.code === 'polish_chapter' && a.chapter_id != null) {
        if (!bookId) return
        const r = await api.agentsPolishAsync(bookId, a.chapter_id, a.note ?? '')
        setFixNote(`已发起第 ${a.chapter_id} 章段落修订（修订要求已注入 rewrite + deslop）；正在跟踪后台终态`)
        onOpenRun(r.task_id, 'polish')
        trackTask(r.task_id, `修段落（第 ${a.chapter_id} 章）`)
      } else if (a.code === 'acceptance_settle' && a.chapter_id != null) {
        if (!bookId) return
        const r = await api.acceptanceSettle(bookId, a.chapter_id, '千章守护中心：补建逐章记录')
        setFixResult({
          ok: r.ok,
          title: `补建逐章记录（第 ${a.chapter_id} 章） · ${r.ok ? (r.committed ? '已提交' : '已完成') : '未通过'}`,
          detail: `${r.detail}${r.steps?.length ? `\n执行步骤：${r.steps.join(' → ')}` : ''}${r.already_committed ? '\n该章此前已提交，本次未重复落账。' : ''}`,
        })
      } else if (a.code === 'open_run' && a.task_id) {
        onOpenRun(a.task_id, a.mode ?? 'write')
      } else if (a.code === 'open_chapter' && a.path) {
        if (bookId) onOpenFile(bookId, a.path)
      } else if (a.code === 'open_file' && a.path) {
        if (bookId) onOpenFile(bookId, a.path)
        else setFixResult({ ok: false, title: '无法打开文件', detail: '请先选择一本书：资产文件位于该书目录内' })
      } else if (a.code === 'gate_accept' && a.chapter_id != null) {
        if (!bookId) return
        const r = await api.guardGateDecide(bookId, a.chapter_id, 'accept', { note: a.note ?? '' })
        applyGateAfter(r.after)
        setFixResult({
          ok: !!r.ok,
          title: `作者裁决 · 已接受第 ${a.chapter_id} 章闸事实`,
          detail: [
            `本轮事实 ${r.decision?.card_count ?? '—'} 条已按现状接受；系统未替你判定质量，未改动正文与追踪权威状态。`,
            r.note ?? '',
            `裁决指纹 ${r.decision?.card_fingerprint ?? '—'}：该章若再产生新闸事实，此裁决自动失效并重新待裁决。`,
          ].filter(Boolean).join('\n'),
        })
      } else if (a.code === 'gate_recheck' && a.chapter_id != null) {
        if (!bookId) return
        const r = await api.guardGateDecide(bookId, a.chapter_id, 'recheck', { note: a.note ?? '' })
        applyGateAfter(r.after)
        const codes = (r.strict_would_block ?? []).join('、') || '（无）'
        setFixResult({
          ok: !!r.ok,
          title: `作者裁决 · 第 ${a.chapter_id} 章 strict 口径只读复核`,
          detail: [
            `若硬闸仍开着，本轮会拦下 ${r.strict_would_block?.length ?? 0} 道闸：${codes}`,
            `生产追踪一致性只读复核：${r.check_ok ? '通过' : '未通过'}。${r.check_detail || ''}`,
            r.note ?? '',
          ].filter(Boolean).join('\n'),
        })
      } else if (a.code === 'gate_rollback' && a.chapter_id != null) {
        if (!bookId) return
        const to = a.to_chapter ?? (a.chapter_id - 1)
        const r = await api.guardGateDecide(bookId, a.chapter_id, 'rollback', {
          note: a.note ?? '', confirm: true, to_chapter: to,
        })
        applyGateAfter(r.after)
        setFixResult({
          ok: !!r.ok,
          title: `作者裁决 · 第 ${a.chapter_id} 章已回滚到第 ${to} 章快照`,
          detail: [
            `生产回滚输出：${r.check_detail || '（无）'}`,
            r.note ?? '',
            '这是不可逆动作：大于目标章的正文已移入 _archive/，请先核对归档目录再继续写作。',
          ].filter(Boolean).join('\n'),
        })
      } else if (a.code === 'recheck') {
        setFixResult({
          ok: true, title: '已重新研判当前状态',
          detail: '这一项属于「检测/配置类」：重新研判只重拉真实状态，不修改任何文件。要改变判定结果，需按中栏「证据（源码 file:line）」定位的配置或代码改完后，再点一次研判；若需要开开关，用对应的「复制启动命令」。',
        })
      }
    } catch (e) {
      setFixResult({ ok: false, title: `${a.label} · 执行失败`, detail: e instanceof Error ? e.message : String(e) })
    } finally {
      setFixBusy(false)
      loadBook()
    }
  }, [bookId, onOpenRun, onOpenFile, loadBook, trackTask, applyFuseAfter, applyGateAfter])

  /** 点击入口：需要确认的动作先展示「可理解的方案 + 影响」内嵌确认条，
   *  取代会被浏览器抑制、取消即静默无反馈的原生 window.confirm。 */
  const requestFix = useCallback((a: RiskAction, s?: Solution | null) => {
    if (a.confirm) {
      setPending({ action: a, solution: s ?? null })
      setFixNote(null)
      setFixResult(null)
      return
    }
    void execFix(a)
  }, [execFix])

  if (!registry) {
    return (
      <div className="h-full w-full flex flex-col bg-[#181818] text-[#d4d4d4]">
        <header className="h-10 shrink-0 flex items-center gap-3 px-4 border-b border-[#2a2a2a] bg-[#1c1c1c]">
          <span className="text-[14px] font-semibold text-white tracking-wide">千章守护中心</span>
          <span className="text-[10px] text-[#666] font-mono">GUARD</span>
          <div className="flex-1" />
          <button onClick={loadRegistry} className="px-2.5 py-1 text-[12px] rounded border border-[#f59e0b55] text-[#f59e0b] hover:bg-[#f59e0b15]">重新加载</button>
        </header>
        <div className="flex-1 flex flex-col items-center justify-center gap-3 px-6 text-center">
          {regErr ? (
            <>
              <div className="text-[15px] text-red-400 font-medium">无法加载守护登记册</div>
              <div className="text-[12px] text-[#999] max-w-md leading-relaxed">{regErr}</div>
              <div className="text-[11px] text-[#666] max-w-md leading-relaxed">排查：① 后端是否已重载（--reload）或重启；② 未设置 NWW_GUARD_ENABLED=0；③ 登录态是否有效。</div>
              <button onClick={loadRegistry} className="mt-1 px-3 py-1.5 text-[12px] rounded border border-[#f59e0b55] text-[#f59e0b] hover:bg-[#f59e0b15]">重试</button>
            </>
          ) : (
            <span className="text-[13px] text-[#666]">加载守护登记册…</span>
          )}
        </div>
      </div>
    )
  }

  return (
    <div className="h-full w-full flex flex-col bg-[#181818] text-[#d4d4d4]">
      {/* 顶栏：仅标题与全局操作（书籍选择器下移到工程能力条之后） */}
      <header className="h-10 shrink-0 flex items-center gap-3 px-4 border-b border-[#2a2a2a] bg-[#1c1c1c]">
        <span className="text-[14px] font-semibold text-white tracking-wide">千章守护中心</span>
        <span className="text-[10px] text-[#666] font-mono">GUARD · P0 只读</span>
        {isLinked && (
          <span
            className="px-1.5 py-0.5 rounded text-[11px] text-[#f59e0b] bg-[#f59e0b15] border border-[#f59e0b55] truncate max-w-[40%]"
            title="书源由中间书架卡决定，换书即重新研判该书"
          >
            书架当前书：{currentBookTitle ?? bookId ?? '未选择书籍'}
          </span>
        )}
        <div className="flex-1" />
        {overview && (
          <span className="text-[11px] text-[#888]">
            tracking {overview.tracking_present ? <span className="text-[#22c55e]">✓</span> : <span className="text-[#ef4444]">✗</span>}
            {' · '}{overview.chapter_count} 章
          </span>
        )}
        <button
          onClick={() => latestRun && onOpenRun(latestRun.task_id, latestRun.mode ?? 'write')}
          disabled={!latestRun}
          className="px-2.5 py-1 text-[12px] rounded border border-[#f59e0b55] text-[#f59e0b] hover:bg-[#f59e0b15] disabled:opacity-40 disabled:border-[#333] disabled:text-[#666]"
        >
          AI 实况
        </button>
      </header>

      {/* 系统层面 · 工程能力：常规指标（含健康态），固定位于标题下方、书籍选择器上方 */}
      {risk && (
        <div className="shrink-0 border-b border-[#222] px-4 py-2" style={{ background: `${ENG_ACCENT}08` }}>
          <div className="flex items-center gap-2 mb-1.5">
            <span className="text-[11px] font-semibold shrink-0" style={{ color: ENG_ACCENT }}>系统层面 · 工程能力</span>
            <RiskBadge level={metricStats.worst} />
            <span className="text-[11px] text-[#888]">
              {metrics.length} 项 · 高危 {metricStats.counts.high} · 中风险 {metricStats.counts.medium} · 低风险 {metricStats.counts.low} · 健康 {metricStats.counts.ok}
            </span>
            <span className="flex-1" />
            <span className="text-[11px] text-[#777] shrink-0">点击指标查看详情与修复动作</span>
          </div>
          <div className="flex flex-wrap gap-1.5">
            {metrics.map((m) => {
              const meta = RISK_META[m.level] ?? RISK_META.low
              const active = selected?.id === m.id
              return (
                <button
                  key={m.id}
                  onClick={() => selectMetric(m)}
                  title={`${m.name} — ${meta.label}${m.action ? ' · 点击查看修复' : ''}`}
                  className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded border text-[11px] transition-colors ${active ? 'border-[#555] bg-[#2a2a2a]' : 'border-[#2a2a2a] hover:bg-[#1f1f1f]'}`}
                >
                  <span className="w-1.5 h-1.5 rounded-full shrink-0" style={{ background: meta.color }} />
                  <span className="text-[#ccc]">{m.name}</span>
                  <span style={{ color: meta.color }}>{meta.label}</span>
                </button>
              )
            })}
          </div>
        </div>
      )}

      {/* 书籍行：受控态（外层已有书架卡）只显示书源，不再出现第二套选书器；
          非受控态保留内置下拉；右侧保留作品/资产（故事层）研判摘要 */}
      <div className="shrink-0 flex items-center gap-3 px-4 py-2 border-b border-[#222] bg-[#181818]">
        <span className="text-[11px] text-[#888] shrink-0">作品</span>
        {hideSelect ? (
          <>
            <span className="text-[13px] text-[#e5e5e5] truncate max-w-[30%]">{currentBookTitle ?? bookId ?? '— 未选择书籍 —'}</span>
            <span className="text-[11px] text-[#666] shrink-0">在中间书架卡选书</span>
          </>
        ) : (
          <select
            value={bookId ?? ''}
            onChange={(e) => selectBook(e.target.value || null)}
            className="bg-[#0f0f0f] border border-[#333] rounded px-2 py-1 text-[13px] text-[#ccc] outline-none focus:border-[#f59e0b]"
          >
            <option value="">— 选择书 —</option>
            {books.map((b) => <option key={b.id} value={b.id}>{b.title}</option>)}
          </select>
        )}
        <button onClick={() => { onReloadBooks(); loadBook() }} className="px-2 py-1 text-[12px] rounded border border-[#333] text-[#bbb] hover:border-[#555] hover:text-white">刷新</button>
        <div className="flex-1" />
        {story && (
          <>
            <span className="text-[11px] font-semibold shrink-0" style={{ color: STORY_ACCENT }}>作品/资产 · 千章不崩</span>
            <RiskBadge level={story.overall.level} />
            <span className="text-[11px] text-[#999] truncate max-w-[45%]">{story.overall.summary}</span>
          </>
        )}
      </div>

      {err && <div className="shrink-0 px-4 py-1.5 text-[12px] text-red-400 bg-red-500/10 border-b border-red-500/30">{err}</div>}
      {fixNote && <div className="shrink-0 px-4 py-1.5 text-[12px] text-[#f59e0b] bg-[#f59e0b10] border-b border-[#f59e0b30]">{fixNote}</div>}
      {tracking && <div className="shrink-0 px-4 py-1.5 text-[12px] text-[#38bdf8] bg-[#38bdf810] border-b border-[#38bdf830]">后台任务进行中（{tracking}）· 本页正在跟踪终态，成功或失败原因会自动显示在这里，无需刷新</div>}

      {/* 内嵌二次确认：写清「会做什么 / 影响什么」，取消也留可见回执 */}
      {pending && (
        <div className="shrink-0 px-4 py-2.5 border-b border-[#f59e0b40] bg-[#f59e0b12]">
          <div className="flex items-center gap-2 mb-1">
            <span className="text-[12px] font-semibold text-[#f59e0b]">待确认 · {pending.action.label}</span>
            <span className="text-[11px] text-[#999]">不确认就不会执行；执行后这里会显示真实结果</span>
          </div>
          {pending.solution ? (
            <>
              <div className="text-[12px] text-[#e5e5e5] leading-relaxed">{pending.solution.plan}</div>
              <div className="text-[11px] text-[#999] leading-relaxed mt-1">影响：{pending.solution.impact}</div>
            </>
          ) : (
            <div className="text-[12px] text-[#e5e5e5] leading-relaxed">{pending.action.note || '该操作会实际生效，执行后本页会显示成功或失败原因。'}</div>
          )}
          <div className="flex items-center gap-2 mt-2">
            <button
              onClick={() => { const a = pending.action; setPending(null); void execFix(a) }}
              disabled={fixBusy}
              className="px-2.5 py-1 text-[12px] rounded border border-[#f59e0b66] text-[#f59e0b] hover:bg-[#f59e0b20] disabled:opacity-50"
            >确认执行</button>
            <button
              onClick={() => { const label = pending.action.label; setPending(null); setFixResult({ ok: true, title: '已取消', detail: `未执行「${label}」：没有发起任何后台任务，也没有改动任何文件。` }) }}
              className="px-2.5 py-1 text-[12px] rounded border border-[#444] text-[#bbb] hover:border-[#666] hover:text-white"
            >取消</button>
          </div>
        </div>
      )}

      {/* 动作结果：常驻，含后端原文判词；失败也原样显示，不静默 */}
      {fixResult && (
        <div className="shrink-0 px-4 py-2 border-b" style={{ borderColor: fixResult.ok ? '#22c55e44' : '#ef444444', background: fixResult.ok ? '#22c55e0d' : '#ef44440d' }}>
          <div className="flex items-center gap-2">
            <span className="text-[12px] font-semibold" style={{ color: fixResult.ok ? '#22c55e' : '#ef4444' }}>{fixResult.ok ? '✓' : '✗'} {fixResult.title}</span>
            <div className="flex-1" />
            {fixResult.taskId && (
              <button onClick={() => onOpenRun(fixResult.taskId!, 'rewrite')} className="px-2 py-0.5 text-[11px] rounded border border-[#f59e0b55] text-[#f59e0b] hover:bg-[#f59e0b15]">查看 AI 实况</button>
            )}
            <button onClick={() => setFixResult(null)} className="px-2 py-0.5 text-[11px] rounded border border-[#444] text-[#999] hover:text-white">关闭</button>
          </div>
          <div className="text-[12px] text-[#ddd] leading-relaxed mt-1 whitespace-pre-wrap">{fixResult.detail}</div>
        </div>
      )}

      {/* 各层健康胶囊（按风险最差面着色） */}
      <div className="shrink-0 flex flex-wrap items-center gap-2 px-4 py-2 border-b border-[#222] bg-[#141414]">
        {Object.keys(LAYER_META).map((ly) => {
          const w = worstOf(ly)
          const dims = grouped[ly] ?? []
          return (
            <span key={ly} className="inline-flex items-center gap-1.5 px-2 py-1 rounded text-[11px] border border-[#2a2a2a]" style={{ color: LAYER_META[ly].color }}>
              {LAYER_META[ly].title}
              <span className="text-[#777]">({dims.length})</span>
              {w !== 'ok' && <RiskBadge level={w} />}
            </span>
          )
        })}
        {overview && !overview.guard_module_enabled && (
          <span className="text-[11px] text-[#f59e0b]">⚠ 后端 NWW_GUARD_ENABLED=0：接口未挂载，仅前端展示登记册</span>
        )}
      </div>

      {/* 主体：三栏百分比宽度 — 左=维度树 15%  中=维度详情 50%  右=系统层面(上)+作品/资产(中)+运行记录(下) 35% */}
      <div className="flex-1 min-h-0 flex">
        {/* 左栏：维度树（点色 = 风险等级）；宽度按浏览器宽度 15% 自适应 */}
        <div className="w-[15%] shrink-0 border-r border-[#222] overflow-auto bg-[#141414]">
          {Object.keys(LAYER_META).map((ly) => (
            <div key={ly} className="py-2">
              <div className="px-3 py-1 text-[11px] font-semibold tracking-wide" style={{ color: LAYER_META[ly].color }}>{LAYER_META[ly].title}</div>
              {(grouped[ly] ?? []).map((d) => {
                const sel = selected?.id === d.id
                const rk = riskById[d.id]
                const dot = rk ? RISK_META[rk.level].color : (MODE_META[d.mode] ?? { color: '#6b7280' }).color
                return (
                  <button
                    key={d.id}
                    onClick={() => setSelected(d)}
                    className={`w-full flex items-center gap-2 px-3 py-1.5 text-left transition-colors ${sel ? 'bg-[#2a2a2a]' : 'hover:bg-[#1f1f1f]'}`}
                  >
                    <span className="w-2 h-2 rounded-full shrink-0" style={{ background: dot }} />
                    <span className={`text-[13px] truncate ${sel ? 'text-white' : 'text-[#ccc]'}`}>{d.name}</span>
                  </button>
                )
              })}
              {(grouped[ly] ?? []).length === 0 && <div className="px-3 py-1 text-[12px] text-[#555]">（无）</div>}
            </div>
          ))}
        </div>

        {/* 中栏：维度详情（宽度自适应，占 50% 中轴） */}
        <div className="w-[50%] min-w-0 shrink-0 overflow-auto p-3">
          {!bookId && <div className="text-[13px] text-[#666] mb-3">{hideSelect ? '在中间书架卡选书' : '选择书'}后右侧显示该书故事层研判；维度配置级研判始终可见。点击左侧维度查看详情。</div>}

          {/* 选中维度 / 工程能力指标详情 */}
          {selected && (() => {
            const rk = riskById[selected.id]
            const action = rk?.action ?? selected.action ?? null
            const isEnv = selected.layer === 'ENV'
            return (
              <div className="w-full rounded-lg border border-[#2a2a2a] bg-[#141414] p-4">
                <div className="flex items-center gap-3 mb-3">
                  <span className="text-[18px] font-semibold text-white">{selected.name}</span>
                  <span className="text-[11px] text-[#666] font-mono">{selected.id}</span>
                  {rk && <RiskBadge level={rk.level} />}
                </div>
                {isEnv ? (
                  <>
                    <Field label="类型" value="系统级运行开关（全部书籍共享）" />
                    <Field label="当前状态说明" value={selected.prevents} />
                  </>
                ) : (
                  <>
                    <Field label="防什么崩" value={selected.prevents} />
                    <Field label="主环节" value={selected.stage} />
                    <Field label="当前能力状态" value={(MODE_META[selected.mode] ?? { label: selected.mode }).label} />
                    <Field label="运行时校验" value={selected.runtime_verified === undefined ? '—（探针未启用）' : (selected.runtime_reason ?? '—')} />
                    <Field label="packet 注入键（P1 将正式接入生成链）" value={selected.packet_key ?? '—（管理面观察项，不进 packet）'} mono={!!selected.packet_key} />
                    <div className="mb-3">
                      <div className="text-[12px] text-[#888] mb-1">证据（源码 file:line）</div>
                      <pre className="text-[12px] text-[#cfcfcf] bg-[#0f0f0f] border border-[#2a2a2a] rounded px-3 py-2 whitespace-pre-wrap break-words font-mono leading-relaxed">{selected.evidence}</pre>
                    </div>
                    <div className="mb-3">
                      <div className="text-[12px] text-[#888] mb-1">备注</div>
                      <div className="text-[13px] text-[#bbb] leading-relaxed">{selected.note}</div>
                    </div>
                  </>
                )}
                {rk && (
                  <div className="rounded border mt-2" style={{ borderColor: `${RISK_META[rk.level].color}55`, background: `${RISK_META[rk.level].color}0f` }}>
                    <div className="p-3">
                      <div className="flex items-center gap-2 mb-1">
                        <RiskBadge level={rk.level} />
                        <span className="text-[13px] font-medium text-[#e5e5e5]">该项能力研判</span>
                      </div>
                      <div className="text-[12px] text-[#999] leading-relaxed mb-1.5">{rk.reason}</div>
                      <div className="flex gap-1.5 items-start mb-2">
                        <span className="text-[11px] text-[#f59e0b] shrink-0 mt-0.5">建议</span>
                        <span className="text-[12px] text-[#cfcfcf] leading-relaxed">{rk.suggestion}</span>
                      </div>
                      {/* 工程能力项此前只有「重新研判」＝只提示不解决；这里给出可理解的处置路径 + 证据定位 */}
                      <div className="rounded border border-[#38bdf833] bg-[#38bdf808] px-2.5 py-2 mb-2">
                        <div className="text-[11px] font-semibold text-[#38bdf8] mb-1">解决方案 · 怎么改才能让它变绿</div>
                        <div className="text-[12px] text-[#d5d5d5] leading-relaxed">
                          {action?.code === 'copy_command'
                            ? `这一项由运行期开关控制：后端进程启动时没带上对应环境变量，能力才停在「${(MODE_META[selected.mode] ?? { label: selected.mode }).label}」。做法：点下方「${action.label}」复制启动命令 → 写进后端启动环境 → 重启 8010 后端 → 回到本页点「重新研判」看真实状态。只复制不重启不会生效。`
                            : action?.code === 'bootstrap_book'
                              ? '这一项缺的是开书资料（世界观 / 卷纲 / 主角卡）。做法：点下方按钮按现有 bible.json 补生成缺失文件，已存在的不覆盖；注意它不会创建 _tracking-state.json，追踪状态仍要由建书 init 或首次 commit 生成。完成后本页自动重拉研判，只写设定 / 大纲目录，不动正文。'
                              : action?.code === 'cancel_task'
                                ? '这一项是运行记录里的僵尸任务：线程已不在，状态却还停在 running。做法：点下方按钮把记录回写成终态；这是协作式取消，不会杀掉真在跑的任务。'
                                : action?.code === 'open_file' || action?.code === 'open_run'
                                  ? `这一项需要人工核对证据。做法：点下方「${action.label}」打开对应文件或任务，按下面的「证据（源码 file:line）」核对，处理完再回来点「重新研判」。`
                                  : `这一项当前是「${(MODE_META[selected.mode] ?? { label: selected.mode }).label}」，属于检测/配置类能力，没有一键开关。做法：按下面的「证据（源码 file:line）」定位实现位置，改配置或代码并重启后端，再点「重新研判」验证。重新研判只重拉真实状态，不会自行改文件——所以单点它不会让风险消失。`}
                        </div>
                        <div className="text-[11px] text-[#888] leading-relaxed mt-1">定位：{selected.evidence}</div>
                      </div>
                      {action ? (
                        <div className="flex items-center gap-2">
                          <button
                            onClick={() => requestFix(action)}
                            disabled={fixBusy}
                            className="px-2.5 py-1 text-[12px] rounded border border-[#f59e0b55] text-[#f59e0b] hover:bg-[#f59e0b15] disabled:opacity-50"
                          >
                            {fixBusy ? '执行中…' : action.code === 'copy_command' && copiedCmd === action.command ? '已复制 ✓' : action.label}
                          </button>
                          {action.note && <span className="text-[10px] text-[#666]">{action.note}</span>}
                        </div>
                      ) : (
                        <div className="text-[11px] text-[#666]">本项无一键入口，按上方「解决方案」的路径改完后点「重新研判」验证。</div>
                      )}
                    </div>
                  </div>
                )}
              </div>
            )
          })()}
        </div>

        {/* 右栏：宽度按浏览器宽度 35% 自适应 — 作品/资产(上) + 运行记录(下)，高度随内容 */}
        <div className="w-[35%] shrink-0 border-l border-[#222] flex flex-col bg-[#141414]">
          {/* 上：作品 / 资产 · 千章不崩（故事层）— 高度由内容决定，内容超长时本区滚动 */}
          <div className="shrink overflow-auto">
            <div className="p-3">
              {story ? (
                <RiskSection accent={STORY_ACCENT} title="作品 / 资产 · 千章不崩" subtitle="故事层 · 本书内容风险" badge={story.overall.level}>
                  {story.metrics.chapter_count != null && (
                    <div className="text-[11px] text-[#777] mb-2">
                      章数 {story.metrics.chapter_count}
                      {story.metrics.avg_chars != null
                        ? <> · 均 {story.metrics.avg_chars} 字 · 最短 {story.metrics.min_chars} · 最长 {story.metrics.max_chars}</>
                        : <span className="text-[#f59e0b]"> · 无正文本样，长度/复读/节奏/追踪四项本次均未执行</span>}
                    </div>
                  )}
                  {/* V13 道闸事实 · 作者裁决（2026-11-13）：闸只记录不阻断，
                      这里把事实摆到作者面前，并让「接受 / 只读复核 / 回滚」成为一等动作。
                      状态一律来自后端重读的落盘快照，前端不自己打勾。 */}
                  {bookId && (
                    <div className="mb-2.5 rounded border border-[#38bdf844] bg-[#38bdf80a] p-2.5">
                      <div className="flex items-center gap-2 mb-1.5">
                        <RiskBadge level={!gate || !gate.available ? 'medium' : gate.pending_count > 0 ? 'low' : 'ok'} />
                        <span className="text-[13px] font-medium text-[#e5e5e5]">道闸事实 · 待作者裁决</span>
                        <span className="text-[11px] text-[#888]">模式 {gate?.mode ?? '未知'}</span>
                      </div>
                      {!gate || !gate.available ? (
                        <div className="text-[12px] text-[#f59e0b]">状态未知：闸事实读取失败，不能判定为「已通过」。点右上刷新重试。</div>
                      ) : gate.chapters.length === 0 ? (
                        <div className="text-[12px] text-[#999]">本书尚未产生闸事实（v13 道闸只记录、不阻断）。这不等于检查全部通过。</div>
                      ) : (
                        <>
                          <div className="text-[11px] text-[#999] mb-1.5">
                            共 {gate.chapters.length} 章有闸事实 · 待裁决 {gate.pending_count} 章。事实一变，旧裁决自动失效并重新待裁决。
                          </div>
                          {gate.chapters.slice(0, 6).map((row) => {
                            const isPending = row.status === 'pending' || row.status === 'stale'
                            const statusLabel: Record<string, string> = {
                              pending: '待裁决', stale: '事实已变 · 需重裁', accept: '作者已接受',
                              recheck: '已做只读复核', rollback: '已回滚快照',
                            }
                            const to = row.chapter - 1
                            const btn = 'px-2 py-0.5 text-[11px] rounded border border-[#444] text-[#bbb] hover:border-[#777] hover:text-white disabled:opacity-50'
                            return (
                              <div key={row.chapter} className="mb-2 rounded border border-[#2a2a2a] bg-[#181818] p-2">
                                <div className="flex items-center gap-2 mb-1">
                                  <span className="text-[12px] text-[#e5e5e5]">第 {row.chapter} 章</span>
                                  <span className="text-[11px]" style={{ color: isPending ? '#f59e0b' : '#22c55e' }}>
                                    {statusLabel[row.status] ?? row.status}
                                  </span>
                                  <span className="text-[11px] text-[#777]">{row.count} 条 · {row.committed ? '已入权威游标' : '尚未提交'}</span>
                                </div>
                                <div className="text-[11px] text-[#999] mb-1.5 break-all">{row.codes.join('、') || '（无闸码）'}</div>
                                <div className="flex items-center gap-2 flex-wrap">
                                  <button
                                    disabled={fixBusy} className={btn}
                                    onClick={() => requestFix({ code: 'gate_accept', label: `接受第 ${row.chapter} 章闸事实`, chapter_id: row.chapter })}
                                  >接受事实</button>
                                  <button
                                    disabled={fixBusy} className={btn}
                                    onClick={() => requestFix({ code: 'gate_recheck', label: `第 ${row.chapter} 章 strict 口径只读复核`, chapter_id: row.chapter })}
                                  >strict 口径复核（只读）</button>
                                  {to >= 1 && (
                                    <button
                                      disabled={fixBusy} className="px-2 py-0.5 text-[11px] rounded border border-[#ef444455] text-[#ef4444] hover:bg-[#ef444415] disabled:opacity-50"
                                      onClick={() => {
                                        const act: RiskAction = {
                                          code: 'gate_rollback', label: `回滚第 ${row.chapter} 章到第 ${to} 章快照`,
                                          risk: 'high', confirm: true, chapter_id: row.chapter, to_chapter: to,
                                        }
                                        const sol: Solution = {
                                          plan: `按当前登录账号定位《${currentBookTitle ?? bookId}》，把追踪状态恢复到第 ${to} 章快照；第 ${row.chapter} 章及之后的正文与逐章记录移入 _archive/ 归档。`,
                                          impact: '不可逆动作：持生产写锁执行快照回滚，会改动该书追踪状态并移动正文文件；执行后需在归档目录人工核对。系统不会替你决定，必须点「确认执行」才会发生。',
                                          action: act,
                                        }
                                        requestFix(act, sol)
                                      }}
                                    >回滚到第 {to} 章快照</button>
                                  )}
                                </div>
                              </div>
                            )
                          })}
                          {gate.chapters.length > 6 && <div className="text-[11px] text-[#666]">仅显示前 6 章；逐条事实见该章章节详情。</div>}
                        </>
                      )}
                    </div>
                  )}
                  {/* 重写熔断：判定一律以生产 --check 的**当前**锁态为准。
                      历史 fuse 回执按设计不会被删除，不能拿它反推「现在还锁着」。 */}
                  {showFuseLocked && (
                    <div className="mb-2.5 rounded border border-[#ef444455] bg-[#ef44440d] p-2.5">
                      <div className="flex items-center gap-2 mb-1.5">
                        <RiskBadge level="high" />
                        <span className="text-[13px] font-medium text-[#e5e5e5]">重写被熔断锁住（第 {fuseChapter} 章）</span>
                      </div>
                      {lockedFuse && (
                        <div className="text-[11px] text-[#999] mb-1.5">
                          当前校验：有效回执 {lockedFuse.receipts ?? '—'} / 上限 {lockedFuse.max_rewrites ?? 3}
                          · 历史回执 {lockedFuse.receipts_total ?? '—'} · 已登记基线 {lockedFuse.baseline ?? 0}
                          {lockedFuse.reason ? ` · ${lockedFuse.reason}` : ''}
                        </div>
                      )}
                      {fuseFallback && (
                        <div className="text-[11px] text-[#f59e0b] mb-1.5">
                          ⚠ 当前锁态读取失败，暂按历史运行记录显示；此判定可能过期，请点右上「刷新」重试。
                        </div>
                      )}
                      {lockedFuse?.lock_kind === 'outline' ? (
                        <>
                          <div className="text-[12px] text-[#d5d5d5] leading-relaxed mb-1">
                            <span className="text-[#f59e0b] font-semibold">为什么锁：</span>细纲前提已变更（outline_hash 与上次登记不一致），旧回执/快照不能在新细纲上续写。<b>登记计数基线解决不了这一类锁</b>，点了状态也不会变。
                          </div>
                          <div className="text-[12px] text-[#d5d5d5] leading-relaxed mb-1.5">
                            <span className="text-[#38bdf8] font-semibold">怎么解：</span>点下方「同步细纲前提」——服务端按当前登录账号定位该书，把当前细纲登记为新的前提基线，并按生产语义重置该章计数；不删回执、不动正文。
                          </div>
                          <div className="flex items-center gap-2 flex-wrap">
                            <button
                              onClick={() => {
                                const act: RiskAction = { code: 'sync_fuse_hash', label: `同步细纲前提（第 ${fuseChapter} 章）`, risk: 'medium', confirm: true, chapter_id: fuseChapter }
                                const sol: Solution = {
                                  plan: `第 ${fuseChapter} 章这次被拦不是因为回执太多，而是细纲 outline_hash 变了。系统会读取当前细纲并登记为新的前提基线，同时按生产语义重置该章计数基线；全过程在服务端完成，界面不接触也不显示任何文件路径。`,
                                  impact: '只写该书追踪目录下的熔断状态文件（前提基线 + 计数基线）；不删除/修改任何执行回执、不改动正文、不影响其他账号的书。执行后本页立即用生产校验复核并更新这张卡片。',
                                  action: act,
                                }
                                requestFix(act, sol)
                              }}
                              disabled={fixBusy}
                              className="px-2.5 py-1 text-[12px] rounded border border-[#f59e0b55] text-[#f59e0b] hover:bg-[#f59e0b15] disabled:opacity-50"
                            >同步细纲前提（第 {fuseChapter} 章）</button>
                            {fuseRun && <button onClick={() => onOpenRun(fuseRun.task_id, fuseRun.mode ?? 'rewrite')} className="px-2.5 py-1 text-[12px] rounded border border-[#444] text-[#bbb] hover:border-[#666] hover:text-white">查看被拦任务</button>}
                          </div>
                        </>
                      ) : (
                        <>
                          <div className="text-[12px] text-[#d5d5d5] leading-relaxed mb-1">
                            <span className="text-[#f59e0b] font-semibold">为什么锁：</span>这一章新一轮失败回执超过上限。为防止「越重写越坏」，系统自动停下重写；历史存量不是本轮错误，需要一次性登记成基线。
                          </div>
                          <div className="text-[12px] text-[#d5d5d5] leading-relaxed mb-1.5">
                            <span className="text-[#38bdf8] font-semibold">怎么解：</span>把历史回执数登记为基线（只写 <span className="font-mono">追踪/rewrite_fuse.json</span>，<b>不删也不改任何执行回执、不动正文</b>），重写额度从 0 重新计。点下方「解除重写锁」就在本页完成：系统按当前登录账号定位这本书，<b>不需要打开终端，也不需要知道文件放在哪里</b>。执行后本页会用生产校验复核，卡片随之变为「已解除」。
                          </div>
                          <div className="flex items-center gap-2 flex-wrap">
                            <button
                              onClick={() => {
                                const act: RiskAction = { code: 'unlock_fuse', label: `解除重写锁（第 ${fuseChapter} 章）`, risk: 'medium', confirm: true, chapter_id: fuseChapter }
                                const sol: Solution = {
                                  plan: `系统会按当前登录账号找到《${currentBookTitle ?? bookId}》，把第 ${fuseChapter} 章的历史失败回执数登记为基线，之后重写额度重新计。整个过程在服务端完成，界面不接触也不显示任何文件路径。`,
                                  impact: '只写该书追踪目录下的熔断基线文件；不删除/修改任何执行回执、不改动正文、不影响其他账号的书。执行后本页立即复核当前锁态并更新卡片（历史回执仍保留在运行记录里，但不再代表「还锁着」）。',
                                  action: act,
                                }
                                requestFix(act, sol)
                              }}
                              disabled={fixBusy}
                              className="px-2.5 py-1 text-[12px] rounded border border-[#f59e0b55] text-[#f59e0b] hover:bg-[#f59e0b15] disabled:opacity-50"
                            >解除重写锁（第 {fuseChapter} 章）</button>
                            {fuseRun && <button onClick={() => onOpenRun(fuseRun.task_id, fuseRun.mode ?? 'rewrite')} className="px-2.5 py-1 text-[12px] rounded border border-[#444] text-[#bbb] hover:border-[#666] hover:text-white">查看被拦任务</button>}
                          </div>
                        </>
                      )}
                    </div>
                  )}
                  {showFuseCleared && clearedFuse && (
                    <div className="mb-2.5 rounded border border-[#22c55e55] bg-[#22c55e0d] p-2.5">
                      <div className="flex items-center gap-2 mb-1.5">
                        <RiskBadge level="ok" />
                        <span className="text-[13px] font-medium text-[#e5e5e5]">重写锁已解除（第 {clearedFuse.chapter} 章）</span>
                      </div>
                      <div className="text-[12px] text-[#d5d5d5] leading-relaxed mb-1">
                        当前生产校验通过：有效回执 <b>{clearedFuse.receipts ?? 0}</b> / 上限 {clearedFuse.max_rewrites ?? 3}
                        · 历史回执 {clearedFuse.receipts_total ?? '—'} · 已登记基线 <b>{clearedFuse.baseline ?? 0}</b>。重写额度已重新计。
                      </div>
                      <div className="text-[11px] text-[#999] leading-relaxed mb-1.5">
                        {fuseRun
                          ? '运行记录里仍能看到旧的被拦任务，这是设计如此——解锁按约定不删改任何执行回执；它只是历史，不代表当前还锁着。判定读的是落盘基线 + 生产校验，刷新页面后仍是这个状态。'
                          : '本次动作已由服务端用生产校验复核为「通过」；判定读的是落盘基线 + 生产校验，不依赖运行记录，之后刷新仍以当前锁态为准。'}
                      </div>
                      {fuseRun && (
                        <button onClick={() => onOpenRun(fuseRun.task_id, fuseRun.mode ?? 'rewrite')} className="px-2.5 py-1 text-[12px] rounded border border-[#444] text-[#bbb] hover:border-[#666] hover:text-white">查看历史被拦任务</button>
                      )}
                    </div>
                  )}
                  {assessBlocked && (
                    <div className="mb-2.5 rounded border border-[#f59e0b55] bg-[#f59e0b0d] p-2.5">
                      <div className="flex items-center gap-2 mb-1.5">
                        <RiskBadge level="medium" />
                        <span className="text-[13px] font-medium text-[#e5e5e5]">当前无法给出「千章不崩」结论</span>
                      </div>
                      <div className="text-[12px] text-[#d5d5d5] leading-relaxed mb-1.5">
                        <span className="text-[#f59e0b] font-semibold">为什么不能判健康：</span>
                        {story?.assess_reason || '缺少可研判的样本。'}
                        之前那句绿色的「未发现明显内容级崩塌信号」在这里不成立——它只在四项都实际跑过、且都没有命中时才会显示。
                      </div>
                      {missingAssets.length > 0 && (
                        <div className="mb-1.5 rounded border border-[#333] bg-[#00000030] px-2 py-1.5">
                          <div className="text-[11px] text-[#999] mb-1">这本书目前缺的资产（按落盘事实，不是推测）：</div>
                          <ul className="text-[11px] text-[#bbb] leading-relaxed space-y-0.5">
                            {missingAssets.map((a) => (
                              <li key={a.key}>
                                · <span className="font-mono">{a.label}</span>
                                <span className="text-[#f59e0b]">{a.state === 'thin' ? '（空壳，内容过薄）' : '（缺失）'}</span>
                              </li>
                            ))}
                          </ul>
                          {foundation?.summary && <div className="text-[11px] text-[#888] mt-1.5">资产体检结论：{foundation.summary}</div>}
                        </div>
                      )}
                      {!foundation && (
                        <div className="text-[11px] text-[#f59e0b] mb-1.5">⚠ 资产体检本次不可用，以上只列了基础事实；点「重新研判当前状态」重试。</div>
                      )}
                      <div className="text-[12px] text-[#d5d5d5] leading-relaxed mb-1.5">
                        <span className="text-[#38bdf8] font-semibold">怎么做：</span>
                        {canBootstrap
                          ? '基础设定档案已在。点「一键补全开书资料」按现有设定生成缺失的世界观 / 卷纲 / 主角卡（只补缺失，已存在的不覆盖）；随后写第 1 章并 commit 生成权威追踪状态，四项检查就会真正开始跑。'
                          : '系统不会替作者发明设定。第一步由你在书籍管理里补齐基础设定档案（bible.json），或用建书流程重建这本书；补齐后回到本页，「一键补全开书资料」会自动出现，用它生成世界观 / 卷纲 / 主角卡。再写第 1 章并 commit 生成追踪状态，长度 / 复读 / 节奏 / 追踪四项才会真正开跑。'}
                      </div>
                      <div className="text-[11px] text-[#999] leading-relaxed mb-2">
                        影响：本页按落盘事实判定，刷新、换书都不会把「没有样本」重新写成「健康」。
                        {canBootstrap
                          ? '补全开书资料只写设定 / 大纲下的文件，不改动正文、不创建追踪状态。'
                          : '这里没有一键按钮是有原因的：缺基础设定档案时补全开书资料会直接失败，给一个点了必然无效的入口只会误导。'}
                      </div>
                      <div className="flex items-center gap-2 flex-wrap">
                        {canBootstrap && (
                          <button
                            onClick={() => {
                              const act: RiskAction = { code: 'bootstrap_book', label: '一键补全开书资料', risk: 'medium', confirm: true }
                              const sol: Solution = {
                                plan: '按这本书已有的 bible.json 生成缺失的开书资料（世界观 / 卷纲 / 主角卡等），系统不会替作者发明设定；已存在的文件跳过不覆盖。',
                                impact: '只写设定 / 大纲目录下的文档，不改动正文、不创建权威追踪状态；执行结果会列出真实写入与跳过的文件。补完后本页会自动重拉研判。',
                                action: act,
                              }
                              requestFix(act, sol)
                            }}
                            disabled={fixBusy}
                            className="px-2.5 py-1 text-[12px] rounded border border-[#f59e0b55] text-[#f59e0b] hover:bg-[#f59e0b15] disabled:opacity-50"
                          >一键补全开书资料</button>
                        )}
                        <button
                          onClick={() => loadBook()}
                          disabled={fixBusy}
                          className="px-2.5 py-1 text-[12px] rounded border border-[#444] text-[#bbb] hover:border-[#666] hover:text-white disabled:opacity-50"
                        >重新研判当前状态</button>
                      </div>
                    </div>
                  )}
                  {story.checks.length === 0 && !assessBlocked && !showFuseLocked && !showFuseCleared ? (
                    <div className="text-[12px] text-[#22c55e]">未发现明显内容级崩塌信号（长度/复读/节奏/追踪）</div>
                  ) : (
                    <div className="grid grid-cols-1 gap-2.5">
                      {story.checks.map((c, i) => {
                        const sol = deriveSolution(c, story.metrics.chapter_count, c.action)
                        const act = sol?.action ?? c.action
                        return <RiskCard key={`st${i}`} item={c} solution={sol} onFix={requestFix} busy={fixBusy} copied={!!act?.command && act.command === copiedCmd} />
                      })}
                    </div>
                  )}
                </RiskSection>
              ) : bookId ? (
                <div className="text-[13px] text-[#666]">该书故事层研判加载中…</div>
              ) : (
                <div className="text-[13px] text-[#666]">选择书后显示故事层研判。</div>
              )}
            </div>
          </div>
          {/* 下：运行记录（图片箭头 2）— 自适应占满右栏剩余高度，条目多则内部滚动 */}
          <div className="flex-1 min-h-[180px] border-t border-[#222] flex flex-col bg-[#141414]">
            <div className="shrink-0 px-3 py-2 border-b border-[#222] flex items-center justify-between">
              <span className="text-[13px] text-[#ccc] font-medium">运行记录</span>
              <span className="text-[11px] text-[#666]">{runs.length} 条{loading ? ' · 加载中' : ''}</span>
            </div>
            {!bookId && <div className="p-3 text-[12px] text-[#666]">选择书后显示运行记录</div>}
            {bookId && runs.length === 0 && <div className="p-3 text-[12px] text-[#666]">该书暂无运行记录</div>}
            <div className="flex-1 overflow-auto">
              {runs.map((r) => (
                <button
                  key={r.task_id}
                  onClick={() => onOpenRun(r.task_id, r.mode ?? 'write')}
                  className="w-full text-left px-3 py-2 border-b border-[#1e1e1e] hover:bg-[#1f1f1f] transition-colors"
                >
                  <div className="flex items-center gap-2">
                    <span className={`text-[11px] px-1.5 py-0.5 rounded ${r.status === 'running' ? 'text-[#f59e0b] bg-[#f59e0b15]' : r.status === 'done' ? 'text-[#22c55e] bg-[#22c55e15]' : r.status === 'error' ? 'text-[#ef4444] bg-[#ef444415]' : 'text-[#999] bg-[#9991a]'}`}>{r.status ?? '—'}</span>
                    <span className="text-[12px] text-[#ccc]">{MODE_LABEL[r.mode ?? ''] ?? r.mode ?? '—'}</span>
                    {r.chapter_id != null && <span className="text-[11px] text-[#777]">第{r.chapter_id}章</span>}
                    <span className="flex-1" />
                    <span className="text-[10px] text-[#666] font-mono">{timeAgo(r.mtime)}</span>
                  </div>
                  <div className="text-[11px] text-[#777] mt-0.5 truncate">环节 {r.stage_count} · {r.last_stage ?? '—'}</div>
                  {r.error && <div className="text-[11px] text-[#ef4444] mt-0.5 truncate">{r.error}</div>}
                </button>
              ))}
            </div>
          </div>
        </div>
      </div>

      {/* 维度详情已移入中栏（见上：选中维度详情块），不再使用右侧抽屉 */}
    </div>
  )
}

function Field({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="mb-3">
      <div className="text-[12px] text-[#888] mb-1">{label}</div>
      <div className={`text-[13px] text-[#ddd] leading-relaxed ${mono ? 'font-mono text-[12px] text-[#cfcfcf] bg-[#0f0f0f] border border-[#2a2a2a] rounded px-2 py-1' : ''}`}>{value}</div>
    </div>
  )
}
