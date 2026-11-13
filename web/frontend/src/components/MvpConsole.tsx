import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, type AgentStage, type AgentsWriteResult, type Book, type Chapter, type LengthBalanceResult, type MvpPacket, type MvpWriteResult, type Tree } from '../api'
import ScrollArea from './ScrollArea'

/**
 * MVP 写作台（聚合页，2026-09-10 新增，纯增量组件）。
 * 四区一步全流程：选书/选章 → 构建 Packet → 写一章 → 打开正文。
 * 暗色风格与现有面板一致（#181818 卡片 / #f59e0b 琥珀高亮）。
 */
interface Props {
  onOpenFile: (bookId: string, path: string) => void
  onReloadBooks: () => void
  /** 2026-11-07 写作台三列组合：外部书架卡受控的当前书 id。
   *  传入（含 null）时工作台书源 = 书架当前书，不再自己默认选第一本。 */
  linkedBookId?: string | null
  /** 外层已有独立书架卡时隐藏内置书籍列表，避免两套选中态口径漂移 */
  hideBookList?: boolean
  /** 内置选书回写全局 activeBook（与书架卡联动；hideBookList 时不触发） */
  onSelectBook?: (id: string) => void
}

type Busy = 'packet' | 'write' | 'agents' | 'rewrite' | 'balance' | null

interface OutlineEntry {
  n: number
  name: string
  path: string
}

function parseOutlineChapter(name: string): number | null {
  const m = /细纲_第(\d+)章/.exec(name)
  return m ? parseInt(m[1], 10) : null
}

export default function MvpConsole({ onOpenFile, onReloadBooks, linkedBookId, hideBookList, onSelectBook }: Props) {
  const [books, setBooks] = useState<Book[]>([])
  /** 2026-11-07：写作台组合下书源受控于外层书架卡（linkedBookId），
   *  未传时保留原「默认选第一本」行为，保证旧调用兼容。单一真源，避免两套选中态。 */
  const isLinked = linkedBookId !== undefined
  const [internalBookId, setInternalBookId] = useState<string | null>(null)
  const bookId = isLinked ? (linkedBookId ?? null) : internalBookId
  const [tree, setTree] = useState<Tree | null>(null)
  const [chapters, setChapters] = useState<Chapter[]>([])
  const [selChapter, setSelChapter] = useState<number | null>(null)
  const [packet, setPacket] = useState<MvpPacket | null>(null)
  const [result, setResult] = useState<MvpWriteResult | null>(null)
  const [agentsResult, setAgentsResult] = useState<AgentsWriteResult | null>(null)
  const [prealign, setPrealign] = useState(true)
  const [busy, setBusy] = useState<Busy>(null)
  const [error, setError] = useState<string | null>(null)
  // 篇幅平衡方案（C-BALANCE-ROUTE@1）：balance 为最近一次预览/执行结果
  const [balance, setBalance] = useState<LengthBalanceResult | null>(null)
  const [balanceApplying, setBalanceApplying] = useState(false)
  const [balanceError, setBalanceError] = useState<string | null>(null)
  // 流式写章打字机：生成中的正文增量
  const [streamText, setStreamText] = useState<string | null>(null)

  const loadBooks = useCallback(() => {
    api.books().then((bs) => {
      setBooks(bs)
      // 受控态书源来自书架卡，不做「默认选第一本」，避免覆盖外层当前书
      if (!isLinked && bs.length > 0) setInternalBookId((cur) => cur ?? bs[0].id)
    }).catch((e) => setError(String(e)))
  }, [isLinked])

  useEffect(() => { loadBooks() }, [loadBooks])

  // 资产自动刷新（2026-10-24 用户裁决）：树/章节在 换书 / 任务完成 / 任务运行中周期 三路触发，
  // 链路中途落盘（0b 细纲、正文 commit、解卡按钮产物）无需手动刷新页面即可见。
  const reloadAssets = useCallback(() => {
    if (!bookId) return
    api.tree(bookId).then(setTree).catch(() => {})
    api.chapters(bookId).then((r) => setChapters(r.chapters)).catch(() => setChapters([]))
  }, [bookId])

  useEffect(() => { reloadAssets() }, [reloadAssets])

  // 长任务运行中每 8s 轮询一次资产（执行中的成链会中途落盘 细纲/正文/追踪）
  useEffect(() => {
    if (!busy) return
    const t = window.setInterval(reloadAssets, 8000)
    return () => window.clearInterval(t)
  }, [busy, reloadAssets])

  const outlines = useMemo<OutlineEntry[]>(() => {
    if (!tree) return []
    const sec = tree.sections.find((s) => s.section === '大纲')
    if (!sec) return []
    const list: OutlineEntry[] = []
    for (const f of sec.files) {
      const n = parseOutlineChapter(f.name)
      if (n !== null) list.push({ n, name: f.name, path: f.path })
    }
    list.sort((a, b) => a.n - b.n)
    return list
  }, [tree])

  const existing = useMemo<Set<number>>(
    () => new Set(chapters.map((c) => c.chapter)),
    [chapters],
  )

  /** 换书统一清理（章节 / Packet / 结果），受控与内部选书共用一份口径 */
  const resetForBook = useCallback(() => {
    setTree(null)
    setChapters([])
    setSelChapter(null)
    setPacket(null)
    setResult(null)
    setStreamText(null)
    setAgentsResult(null)
    setError(null)
    setBalance(null)
    setBalanceApplying(false)
    setBalanceError(null)
  }, [])

  const selectBook = useCallback((id: string) => {
    if (id === bookId) return
    if (isLinked) onSelectBook?.(id)
    else setInternalBookId(id)
    resetForBook()
  }, [bookId, isLinked, onSelectBook, resetForBook])

  /** 书架卡从外部换书（不经内置列表）时同样清掉上一本书的残留；
   *  tree/chapters 由下方 reloadAssets 的 bookId 依赖自动重拉。 */
  useEffect(() => {
    if (!isLinked) return
    resetForBook()
  }, [bookId, isLinked, resetForBook])

  // 默认选中第一个细纲章
  useEffect(() => {
    if (selChapter === null && outlines.length > 0) setSelChapter(outlines[0].n)
  }, [outlines, selChapter])

  const runPacket = () => {
    if (!bookId || selChapter === null) return
    setBusy('packet'); setError(null); setResult(null)
    api.mvpPacket(bookId, selChapter)
      .then((r) => setPacket(r.packet))
      .catch((e) => setError(String(e)))
      .finally(() => setBusy(null))
  }

  const runWrite = () => {
    if (!bookId || selChapter === null) return
    setBusy('write'); setError(null); setResult(null); setStreamText('')
    setBalance(null); setBalanceError(null)
    api.mvpWriteStream(bookId, selChapter, (ev) => {
      if (ev.type === 'delta') setStreamText((t) => (t ?? '') + ev.text)
      else if (ev.type === 'error') setError(ev.detail)
    })
      .then((r) => { setPacket(r.packet); setResult(r); onReloadBooks() })
      .catch((e) => setError(String(e)))
      .finally(() => { setBusy(null); reloadAssets() })
  }

  const runAgents = () => {
    if (!bookId || selChapter === null) return
    setBusy('agents'); setError(null); setAgentsResult(null)
    api.agentsWrite(bookId, selChapter, prealign)
      .then((r) => { setAgentsResult(r); onReloadBooks() })
      .catch((e) => setError(String(e)))
      .finally(() => { setBusy(null); reloadAssets() })
  }

  /** 重写核心（无确认）；runRewrite 与决策卡「同参重试」共用 */
  const runRewriteQuiet = () => {
    if (!bookId || selChapter === null) return
    if (!existing.has(selChapter)) {
      setError(`第 ${selChapter} 章尚无已提交正文，请用「③ Agent 全流程」写新章`)
      return
    }
    setBusy('rewrite'); setError(null); setAgentsResult(null)
    api.agentsRewrite(bookId, selChapter)
      .then((r) => { setAgentsResult(r); onReloadBooks() })
      .catch((e) => setError(String(e)))
      .finally(() => { setBusy(null); reloadAssets() })
  }

  const runRewrite = () => {
    if (!bookId || selChapter === null) return
    if (!existing.has(selChapter)) {
      setError(`第 ${selChapter} 章尚无已提交正文，请用「③ Agent 全流程」写新章`)
      return
    }
    if (!window.confirm(`重写第 ${selChapter} 章？旧稿将自动备份到 .backup/，然后整章全流程重写并重新提交追踪（revision 模式）。`)) return
    runRewriteQuiet()
  }

  /** 篇幅平衡方案：apply=false 只取方案预览（后端 dry_run，不写正文）；
   *  apply=true 按选中方案定向执行并由后端覆写正文（持章锁）。 */
  const runLengthBalance = (apply: boolean) => {
    if (!bookId || selChapter === null) return
    setBusy('balance'); setBalanceApplying(apply); setBalanceError(null)
    api.lengthBalance(bookId, selChapter, apply)
      .then((r) => {
        setBalance(r)
        if (r.applied) { onReloadBooks(); reloadAssets() }
      })
      .catch((e) => setBalanceError(String(e)))
      .finally(() => { setBusy(null); setBalanceApplying(false) })
  }

  // 书名优先取书架列表（进入即显示，不等 tree 异步返回），tree.title 兜底
  const selTitle = books.find((b) => b.id === bookId)?.title ?? (tree ? tree.title : '')
  const selBook = books.find((b) => b.id === bookId)

  return (
    <div className="h-full flex flex-col text-[14px]">
      {/* 顶栏：当前书 + 目录 + 章 + 状态徽标 */}
      <div className="shrink-0 h-10 flex items-center justify-between px-4 border-b border-[#2a2a2a]">
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-[14px] font-semibold text-white truncate">{selTitle || '未选择书'}</span>
          {isLinked && (
            <span className="shrink-0 px-1.5 py-0.5 rounded bg-[#f59e0b]/15 text-[#f59e0b] border border-[#f59e0b]/25 text-[11px]" title="工作台书源由中间书架卡的当前书决定">
              书架当前书
            </span>
          )}
          {selBook?.root && (
            <span className="shrink-0 px-1.5 py-0.5 rounded bg-[#2a2a2a] text-[#8a8a8a] text-[11px] tabular-nums" title="书目录：书项目根/projects/&lt;当前用户&gt;/">
              {selBook.root}/
            </span>
          )}
          {selChapter !== null && (
            <span className="shrink-0 px-1.5 py-0.5 rounded bg-[#f59e0b]/15 text-[#f59e0b] border border-[#f59e0b]/25 text-[14px]">
              第 {selChapter} 章
            </span>
          )}
          <span className="text-[14px] text-[#666]">细纲章 {outlines.length} 个</span>
        </div>
        {busy === 'agents' ? (
          <span className="shrink-0 text-[14px] text-[#f59e0b] animate-pulse">多 Agent 全流程执行中…（数分钟）</span>
        ) : busy === 'rewrite' ? (
          <span className="shrink-0 text-[14px] text-[#f59e0b] animate-pulse">Agent 重写执行中…（备份→重写→revision 提交）</span>
        ) : busy === 'write' ? (
          <span className="shrink-0 text-[14px] text-[#f59e0b] animate-pulse">本地模型写作中…（可数分钟）</span>
        ) : busy === 'packet' ? (
          <span className="shrink-0 text-[14px] text-[#f59e0b]">构建 Packet…</span>
        ) : busy === 'balance' ? (
          <span className="shrink-0 text-[14px] text-[#38bdf8] animate-pulse">
            {balanceApplying ? '篇幅平衡执行中…（定向改写正文）' : '篇幅平衡方案计算中…'}
          </span>
        ) : (
          <span className="shrink-0 text-[14px] text-[#555]">直连本地 omix LLM</span>
        )}
      </div>

      {/* 主体：左选书/选章 + 右聚合主区 */}
      <div className="flex-1 min-h-0 flex">
        {/* 左栏：细纲章节。写作台三列组合下外层已有独立书架卡，隐藏内置书列表避免重复选中态 */}
        <div className="w-60 shrink-0 border-r border-[#2a2a2a] flex flex-col">
          {!hideBookList && (
            <>
              <div className="shrink-0 px-3 pt-3 pb-1 text-[14px] text-[#666] uppercase tracking-wide">书籍</div>
              <ScrollArea className="flex-1 min-h-0">
                {books.length === 0 && <div className="px-3 py-2 text-[14px] text-[#666]">暂无书</div>}
                {books.map((b) => (
                  <button
                    key={b.id}
                    onClick={() => selectBook(b.id)}
                    className={`w-full text-left px-3 py-1.5 block ${b.id === bookId ? 'bg-[#37373d]' : 'hover:bg-[#2a2d2e]'}`}
                  >
                    <div className={`text-[14px] truncate ${b.id === bookId ? 'text-white' : 'text-[#ccc]'}`}>{b.title}</div>
                    <div className="text-[10px] text-[#777] tabular-nums">{b.chapter_count} 章 · {(b.total_chars / 10000).toFixed(1)} 万字</div>
                  </button>
                ))}
              </ScrollArea>
            </>
          )}
          <div className="shrink-0 px-3 pt-2 pb-1 text-[14px] text-[#666] uppercase tracking-wide">细纲章节</div>
          <ScrollArea className="flex-1 min-h-0">
            {!bookId && <div className="px-3 py-2 text-[14px] text-[#666]">先选书</div>}
            {bookId && outlines.length === 0 && (
              <div className="px-3 py-2 text-[14px] text-[#f59e0b]/70">大纲区未找到细纲_第N章 文件</div>
            )}
            {outlines.map((o) => {
              const active = o.n === selChapter
              const done = existing.has(o.n)
              return (
                <button
                  key={o.n}
                  onClick={() => { setSelChapter(o.n); setPacket(null); setResult(null); setError(null); setBalance(null); setBalanceError(null) }}
                  className={`w-full text-left px-3 py-1.5 block ${active ? 'bg-[#37373d]' : 'hover:bg-[#2a2d2e]'}`}
                >
                  <div className="flex items-center gap-1.5">
                    <span className={`text-[14px] ${active ? 'text-white' : 'text-[#ccc]'}`}>第 {o.n} 章</span>
                    {done ? (
                      <span className="ml-auto text-[14px] px-1 rounded bg-[#3a3a3a] text-[#888]">已有正文</span>
                    ) : (
                      <span className="ml-auto text-[14px] px-1 rounded bg-[#f59e0b]/15 text-[#f59e0b]">待写</span>
                    )}
                  </div>
                </button>
              )
            })}
          </ScrollArea>
        </div>

        {/* 右主区 */}
        <div className="flex-1 min-w-0 flex flex-col">
          <div className="flex-1 min-h-0 overflow-auto">
            <div className="p-4 flex flex-col gap-3">
              {/* 操作条 */}
              <div className="flex items-center gap-2">
                <button
                  onClick={runPacket}
                  disabled={!bookId || selChapter === null || busy !== null}
                  className="px-3 py-1.5 rounded border border-[#444] text-[#ccc] hover:border-[#666] hover:text-white disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                >
                  ① 构建 Packet
                </button>
                <button
                  onClick={runWrite}
                  disabled={!bookId || selChapter === null || busy !== null}
                  className="px-3 py-1.5 rounded bg-[#f59e0b]/90 text-black font-medium hover:bg-[#f59e0b] disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                >
                  ② 写一章
                </button>
                <button
                  onClick={runAgents}
                  disabled={!bookId || selChapter === null || busy !== null}
                  className="px-3 py-1.5 rounded bg-[#6ce3a1]/15 text-[#6ce3a1] border border-[#6ce3a1]/30 hover:border-[#6ce3a1] hover:text-white disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                >
                  ③ Agent 全流程
                </button>
                <button
                  onClick={runRewrite}
                  disabled={!bookId || selChapter === null || busy !== null}
                  className={`px-3 py-1.5 rounded border transition-colors disabled:opacity-40 disabled:cursor-not-allowed ${
                    selChapter !== null && existing.has(selChapter)
                      ? 'bg-[#f59e0b]/10 text-[#f59e0b] border-[#f59e0b]/30 hover:border-[#f59e0b] hover:text-white'
                      : 'border-[#3a3a3a] text-[#666]'
                  }`}
                  title={selChapter !== null && existing.has(selChapter) ? '重写已提交章节：备份旧稿 + revision 模式重写' : '仅已有正文的章节可用'}
                >
                  ④ Agent 重写
                </button>
                <button
                  onClick={() => runLengthBalance(false)}
                  disabled={!bookId || selChapter === null || busy !== null || !(selChapter !== null && existing.has(selChapter))}
                  className={`px-3 py-1.5 rounded border transition-colors disabled:opacity-40 disabled:cursor-not-allowed ${
                    selChapter !== null && existing.has(selChapter)
                      ? 'bg-[#38bdf8]/10 text-[#7dd3fc] border-[#38bdf8]/30 hover:border-[#38bdf8] hover:text-white'
                      : 'border-[#3a3a3a] text-[#666]'
                  }`}
                  title={selChapter !== null && existing.has(selChapter) ? '取篇幅平衡方案预览（不写正文），确认后再执行' : '仅已有正文的章节可生成篇幅平衡方案'}
                >
                  ⑤ 篇幅平衡方案
                </button>
                <label className="flex items-center gap-1.5 text-[14px] text-[#999] select-none cursor-pointer">
                  <input
                    type="checkbox"
                    checked={prealign}
                    onChange={(e) => setPrealign(e.target.checked)}
                    className="accent-[#6ce3a1]"
                  />
                  写前对齐
                </label>
                {selChapter !== null && existing.has(selChapter) && (
                  <span className="text-[14px] text-[#f59e0b]/80">第 {selChapter} 章已有正文：普通写章将拒绝覆盖，需用「④ Agent 重写」</span>
                )}
              </div>

              {error && (
                <div className="rounded border border-red-500/30 bg-red-500/10 text-red-300 px-3 py-2 text-[14px] whitespace-pre-wrap">
                  {error}
                </div>
              )}

              {/* Packet 预览 */}
              {packet && (
                <div className="rounded border border-[#2a2a2a] bg-[#1d1d1d]">
                  <div className="px-3 py-1.5 border-b border-[#2a2a2a] text-[14px] text-[#999]">
                    Writing Packet v{packet.schema_version ?? '2'} · 字数目标 {packet.wordcount?.target != null ? `${packet.wordcount.target} 字` : '—'}
                  </div>
                  <div className="p-3 flex flex-col gap-2">
                    <div>
                      <div className="text-[14px] text-[#666] mb-0.5">章首钩子</div>
                      <div className="text-[14px] text-[#ddd] leading-relaxed">{packet.intent?.hook || '—'}</div>
                    </div>
                    <div>
                      <div className="text-[14px] text-[#666] mb-0.5">核心目标</div>
                      <div className="text-[14px] text-[#ccc] leading-relaxed">{packet.intent?.goal?.value || '—'}</div>
                    </div>
                    {packet.scene_chain && packet.scene_chain.length > 0 && (
                      <div>
                        <div className="text-[14px] text-[#666] mb-0.5">情节链（{packet.scene_chain.length} 幕）</div>
                        <div className="text-[14px] text-[#ccc]">
                          {packet.scene_chain.map((s) => `· ${s.act ?? s.scene_id ?? ''}：${s.driver_goal?.goal || s.conflict?.pressure || '—'}`).join('\n')}
                        </div>
                      </div>
                    )}
                    <div className="grid grid-cols-2 gap-2">
                      <div>
                        <div className="text-[14px] text-[#666] mb-0.5">必须落地</div>
                        <div className="text-[14px] text-[#ccc]">{packet.canon_constraints?.must_keep?.length ? packet.canon_constraints.must_keep.map((x) => `· ${x}`).join('\n') : '—'}</div>
                      </div>
                      <div>
                        <div className="text-[14px] text-[#666] mb-0.5">禁止提前释放</div>
                        <div className="text-[14px] text-[#ccc]">{packet.canon_constraints?.must_avoid?.length ? packet.canon_constraints.must_avoid.map((x) => `· ${x}`).join('\n') : '—'}</div>
                      </div>
                    </div>
                    <div>
                      <div className="text-[14px] text-[#666] mb-0.5">前情（末段，节选）</div>
                      <div className="text-[14px] text-[#888] leading-relaxed">{(packet.context?.preceding ?? '').slice(0, 160)}{((packet.context?.preceding ?? '').length > 160) ? '…' : ''}</div>
                    </div>
                    {packet.character_contract && packet.character_contract.length > 0 && (
                      <div>
                        <div className="text-[14px] text-[#666] mb-0.5">出场角色</div>
                        <div className="text-[14px] text-[#ccc]">
                          {packet.character_contract.map((c) => `${c.name ?? '？'}（${c.state || c.function || '—'}）`).join('、')}
                        </div>
                      </div>
                    )}
                  </div>
                </div>
              )}

              {/* 流式写章打字机 */}
              {streamText !== null && busy === 'write' && (
                <div className="rounded border border-[#f59e0b]/30 bg-[#1d1d1d]">
                  <div className="px-3 py-1.5 border-b border-[#2a2a2a] flex items-center gap-2">
                    <span className="w-1.5 h-1.5 rounded-full bg-[#f59e0b] animate-pulse" />
                    <span className="text-[14px] text-[#f59e0b]">正在写作…</span>
                    <span className="text-[11px] text-[#666] tabular-nums">{streamText.length} 字</span>
                  </div>
                  <div className="p-3 text-[14px] text-[#bbb] leading-relaxed whitespace-pre-wrap max-h-96 overflow-auto">
                    {streamText || '…'}
                  </div>
                </div>
              )}

              {/* 写章结果 */}
              {result && (() => {
                const pendingReview = result.commit_status === 'pending_author_review'
                return (
                <div className={`rounded border bg-[#1d1d1d] ${pendingReview ? 'border-[#f59e0b]/40' : 'border-[#2a2a2a]'}`}>
                  <div className="px-3 py-1.5 border-b border-[#2a2a2a] flex items-center gap-3">
                    <span className={`text-[14px] ${pendingReview ? 'text-[#f59e0b]' : 'text-[#6ce3a1]'}`}>
                      {pendingReview ? '◐ 已落盘 · 待作者裁决' : '✓ 已生成'}
                    </span>
                    <span className="text-[14px] text-[#999]">{result.model}</span>
                    <span className="text-[11px] text-[#999] tabular-nums">{result.chars} 字</span>
                    <span className="text-[14px] text-[#666] truncate">{result.path}</span>
                    {result.degraded_pass && (
                      <span className="text-[11px] px-1.5 rounded bg-[#f59e0b]/15 text-[#f59e0b] border border-[#f59e0b]/25">v13 降级翻正</span>
                    )}
                    <div className="flex-1" />
                    <button
                      onClick={() => onOpenFile(result.book_id, result.path)}
                      className="px-2 py-0.5 rounded border border-[#444] text-[#ccc] hover:border-[#f59e0b] hover:text-[#f59e0b] text-[14px] transition-colors"
                    >
                      在编辑器中打开
                    </button>
                  </div>
                  <div className="p-3 text-[14px] text-[#bbb] leading-relaxed whitespace-pre-wrap max-h-72 overflow-auto">
                    {result.chars > 0 ? '正文已落盘，可用上方「在编辑器中打开」直接查看与修改。' : ''}
                    {pendingReview && (
                      <div className="mt-1 text-[#b9a273]">
                        本章未进权威游标：不按「已提交」看待，追踪/闸报告事实与裁决入口见千章守护中心。
                      </div>
                    )}
                  </div>
                  {pendingReview && (result.warnings ?? []).length > 0 && (
                    <div className="px-3 pb-2 flex flex-col gap-0.5">
                      {(result.warnings ?? []).map((w, i) => (
                        <div key={i} className="text-[12px] text-[#c9b48a] break-words">· {w}</div>
                      ))}
                    </div>
                  )}
                </div>
                )
              })()}

              {/* 篇幅平衡方案：预览（不写正文）→ 作者确认后执行 */}
              {balanceError && (
                <div className="rounded border border-red-500/30 bg-red-500/10 text-red-300 px-3 py-2 text-[14px] whitespace-pre-wrap">
                  篇幅平衡失败：{balanceError}
                </div>
              )}
              {balance && selChapter !== null && balance.chapter_id === selChapter && (
                <div className="rounded border border-[#38bdf8]/30 bg-[#1d1d1d]">
                  <div className="px-3 py-1.5 border-b border-[#2a2a2a] flex items-center gap-3 flex-wrap">
                    <span className={`text-[14px] ${balance.applied ? 'text-[#6ce3a1]' : 'text-[#7dd3fc]'}`}>
                      {balance.applied ? '✓ 篇幅平衡已执行' : '篇幅平衡方案（预览 · 未改正文）'}
                    </span>
                    <span className="text-[14px] text-[#999]">第 {balance.chapter_id} 章</span>
                    <span className="text-[11px] text-[#666] tabular-nums">轮次 {balance.rounds}</span>
                    <span className="text-[11px] text-[#666]">策略 {balance.selected_strategy || '—'}</span>
                    {balance.quality_score != null && (
                      <span className="text-[11px] text-[#666] tabular-nums">质量分 {balance.quality_score}</span>
                    )}
                    <span className="text-[11px] text-[#666] tabular-nums">
                      净变化 {balance.net_delta > 0 ? `+${balance.net_delta}` : balance.net_delta} 字 · 残留缺口 {balance.residual_gap}
                    </span>
                    {balance.stop_reason && <span className="text-[11px] text-[#888]">停止原因 {balance.stop_reason}</span>}
                    <div className="flex-1" />
                    {!balance.applied && (
                      <button
                        onClick={() => runLengthBalance(true)}
                        disabled={busy !== null}
                        className="px-2 py-0.5 rounded bg-[#38bdf8]/15 text-[#7dd3fc] border border-[#38bdf8]/40 text-[14px] hover:bg-[#38bdf8]/25 disabled:opacity-50 transition-colors"
                      >
                        {balanceApplying ? '执行中…' : '执行该方案（覆写正文）'}
                      </button>
                    )}
                    <button
                      onClick={() => setBalance(null)}
                      className="text-[#666] hover:text-[#aaa] text-[13px] leading-none"
                      title="关闭"
                    >
                      ×
                    </button>
                  </div>
                  <div className="p-3 text-[14px] text-[#bbb] leading-relaxed whitespace-pre-wrap">
                    {balance.detail || (balance.applied ? '已按方案定向改写并覆写正文。' : '预览未写入正文，点「执行该方案」后由后端按方案改写。')}
                    {!balance.ok && !balance.detail && <div className="mt-1 text-[#b9a273]">本轮未产出可用方案（ok=false）。</div>}
                  </div>
                </div>
              )}

              {/* 多 Agent 全流程结果 */}
              {agentsResult && (
                <AgentsResultView res={agentsResult} onOpenFile={onOpenFile} onAssetsChanged={reloadAssets}
                  onRetryRewrite={runRewriteQuiet} />
              )}

              {!packet && !result && !agentsResult && !error && (
                <div className="mt-8 text-center text-[14px] text-[#555] leading-relaxed">
                  {hideBookList ? '在中间书架卡选书、左侧选细纲章节' : '选择左侧书目与细纲章节'}，点「② 写一章」即可由本地模型生成正文并落盘；
                  <br />
                  或点「③ Agent 全流程」执行 prealign → packet → writer → 落盘 → delta → post_write → deslop 闭环。
                  <br />
                  已有正文的章节不会走普通写章（防覆盖闸拦截）——用「④ Agent 重写」：自动备份旧稿后全流程重写并以 revision 模式重新提交追踪。
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

/** 多 Agent 全流程环节中文名（诊断展示用） */
const STAGE_LABELS: Record<string, string> = {
  backup: '⓪ 旧稿备份（.backup/）',
  plan_outline: '⓪ 写前自动规划（LLM 生成细纲）',
  prealign: '① 写前对齐（缺项自动回补细纲）',
  packet: '② 打包 Writing Packet',
  writer: '③ 主笔生成 + 落盘',
  normalize: '③.5 正文归一（prose 提取 + 清洗 + 去重）',
  wordcount: '③.6 字数收敛（消费 chapter check available_actions）',
  delta_draft: '④ delta 草稿预填',
  delta_semantic: '⑤ delta 语义补全',
  consistency: '⑤.5 一致性审查（S1/S2 detection，不碰 closure）',
  review_cycle: '⑤.6 审稿闭环（passed=true 才写 reviewed，commit 前就绪供 A-5 消费）',
  post_write: '⑥ post_write 追踪提交（BLOCK，commit 消费 reviewed/enforced）',
  deslop: '⑦ 去 AI 味机械层（advisory）',
  quality_observe: '⑦.5 生产质量遥测（advisory）',
  pipeline: '⑧ 编排器兜底',
}

/** 多 Agent 全流程结果面板：逐环节 ok/FAIL + 详情 */
function AgentsResultView({
  res,
  onOpenFile,
  onAssetsChanged,
  onRetryRewrite,
}: {
  res: AgentsWriteResult
  onOpenFile: (bookId: string, path: string) => void
  onAssetsChanged?: () => void
  /** 失败决策卡（后端 _with_decision）：同参重试 / 接受现状；其余动作引导去对话页 */
  onRetryRewrite?: () => void
}) {
  const ok = res.result.ok
  const stages: AgentStage[] = res.result.stages || []
  const [decisionClosed, setDecisionClosed] = useState(false)
  const dec = res.result.needs_decision && !decisionClosed ? res.result : null
  const failed0 = (dec?.failure?.stage || stages.find((s) => !s.ok)?.name || '')
  const failed = stages.filter((s) => !s.ok)
  const failedDetail = (s: AgentStage): string => {
    const detail = s.detail || ''
    // packet 环节 detail 为路径；把 path 提出来
    const iface = s as unknown as Record<string, unknown>
    if (detail && !String(iface.packet ?? '').startsWith('{')) return detail
    return detail.slice(0, 1200)
  }
  // 确认并提交：作者确认一致性闭环 → 自动仅提交当前已落盘章（不重写正文）
  const a3Stage = stages.find((s) => s.name === 'post_write' && !s.ok && /A-3/.test(s.detail || ''))
  const [commitState, setCommitState] = useState<'idle' | 'confirming' | 'committing' | 'done' | 'error'>('idle')
  const [commitOut, setCommitOut] = useState<string | null>(null)
  const confirmAndCommit = () => {
    if (!res.book_id || commitState === 'confirming' || commitState === 'committing') return
    setCommitState('confirming'); setCommitOut(null)
    api
      .confirmConsistencyClosure(res.book_id, res.chapter_id)
      .then(() => {
        setCommitState('committing')
        return api.agentsCommitOnly(res.book_id, res.chapter_id)
      })
      .then((r) => {
        const ok = r.result?.ok ?? false
        const detail = r.result?.detail || JSON.stringify(r.result || {})
        setCommitState(ok ? 'done' : 'error')
        setCommitOut(ok ? `提交成功：${detail}` : `提交仍有 BLOCK：${detail}`)
        if (ok) onAssetsChanged?.()
      })
      .catch((e) => { setCommitState('error'); setCommitOut(String(e)) })
  }
  // 人工修改审核提交：系统补跑完整验收，达到原闸标准后正式落账（不降标准、不重写正文）
  const [settleState, setSettleState] = useState<'idle' | 'running' | 'done' | 'error'>('idle')
  const [settleOut, setSettleOut] = useState<string | null>(null)
  const acceptanceSettle = () => {
    if (!res.book_id || settleState === 'running') return
    setSettleState('running'); setSettleOut(null)
    api
      .acceptanceSettle(res.book_id, res.chapter_id, '作者人工修改后提交（前端审核入口）')
      .then((r) => {
        setSettleState('done')
        setSettleOut(
          `${r.detail || '提交完成'}` +
            (r.steps?.length ? `\n补跑环节：${r.steps.join(' → ')}` : '') +
            (r.last_after != null ? `\n权威游标：${r.last_before} → ${r.last_after}` : ''),
        )
        onAssetsChanged?.()
      })
      .catch((e) => { setSettleState('error'); setSettleOut(String(e)) })
  }
  return (
    <div className="rounded border border-[#2a2a2a] bg-[#1d1d1d]">
      <div className="px-3 py-1.5 border-b border-[#2a2a2a] flex items-center gap-3">
        <span className={`text-[14px] font-medium ${ok ? 'text-[#6ce3a1]' : 'text-red-400'}`}>
          {ok ? '✓ 全流程通过' : '✗ 流程中断'}
        </span>
        <span className="text-[14px] text-[#999]">第 {res.chapter_id} 章</span>
        {res.mode === 'revision' && <span className="text-[14px] px-1.5 rounded bg-[#f59e0b]/15 text-[#f59e0b] border border-[#f59e0b]/25">重写 · revision</span>}
        <span className="text-[11px] text-[#666] tabular-nums">环节 {stages.filter((s) => s.ok).length}/{stages.length}</span>
      </div>
      <div className="p-3 flex flex-col gap-1.5">
        {/* V13 道闸事实卡（2026-11-13）：v13 下动作不再失败，成功路径也必须看得见闸事实 */}
        {res.result.gate_report && res.result.gate_report.count > 0 && (
          <div className="rounded border border-[#38bdf844] bg-[#38bdf80a] px-2.5 py-2">
            <div className="flex items-center gap-2 mb-1 flex-wrap">
              <span className="text-[13px] font-medium text-[#38bdf8]">道闸事实（只记录 · 不阻断）</span>
              <span className="text-[11px] text-[#888]">{res.result.gate_report.count} 条</span>
              <span className="text-[11px]" style={{ color: res.result.gate_pending ? '#f59e0b' : '#22c55e' }}>
                {res.result.gate_pending ? '待作者裁决' : '作者已裁决'}
              </span>
              {res.result.commit_status === 'pending_author_review' && (
                <span className="text-[11px] text-[#f59e0b]">正文已落盘 · 尚未进入权威游标</span>
              )}
            </div>
            <div className="text-[12px] text-[#aaa]">
              {res.result.gate_report.findings.slice(-5).map((f, i) => (
                <div key={i} className="whitespace-pre-wrap break-all">· {f.code}：{String(f.detail).slice(0, 160)}</div>
              ))}
            </div>
            <div className="text-[11px] text-[#666] mt-1">
              系统未替你判定质量；可在「千章守护中心」接受 / 只读复核 / 回滚快照。事实卡：追踪/闸报告/第{String(res.chapter_id).padStart(3, '0')}章.json
            </div>
          </div>
        )}
        {stages.map((s) => (
          <div key={s.name} className="flex items-start gap-2 rounded border border-[#2a2a2a] bg-[#222] px-2.5 py-1.5">
            <span className={`shrink-0 text-[14px] ${s.ok ? 'text-[#6ce3a1]' : 'text-red-400'}`}>{s.ok ? '✓' : '✗'}</span>
            <div className="min-w-0">
              <div className="text-[14px] text-[#ddd]">{STAGE_LABELS[s.name] || s.name}</div>
              {s.ok ? (
                s.name === 'writer' && typeof s.path === 'string' ? (
                  <button
                    onClick={() => onOpenFile(res.book_id, s.path as string)}
                    className="text-[14px] text-[#f59e0b] hover:underline"
                  >
                    {s.detail}
                  </button>
                ) : (
                  <div className="text-[14px] text-[#888] whitespace-pre-wrap break-all">{s.detail}</div>
                )
              ) : (
                <div className="text-[14px] text-red-300/80 whitespace-pre-wrap break-all">{failedDetail(s)}</div>
              )}
              {s.name === 'post_write' && s.returncode !== undefined && (
                <div className="text-[14px] text-[#666]">returncode={String(s.returncode)}</div>
              )}
            </div>
          </div>
        ))}
        {failed.length > 0 && (
          <div className="mt-1 text-[14px] text-red-300/70">
            失败环节：{failed.map((s) => STAGE_LABELS[s.name] || s.name).join('、')}。已终止，可先修复对应输入后再重试。
          </div>
        )}
        {!ok && dec && (
          <div className="mt-2 rounded border border-red-900/50 bg-[#1a1214] px-3 py-2">
            <div className="flex items-center gap-2">
              <span className="text-[14px] font-medium text-red-300">决策：第 {res.chapter_id} 章执行失败</span>
              <span className="text-[12px] text-[#888]">环节 {dec.failure?.stage || failed0} · 闸门 {dec.failure?.gate || '—'}
                {dec.failure?.rolled_back && <span className="ml-1 text-amber-400"> · 正文已回滚</span>}
              </span>
              <span className="flex-1" />
              <button onClick={() => setDecisionClosed(true)} className="text-[13px] text-[#666] hover:text-[#aaa]">×</button>
            </div>
            {(dec as { receipt_path?: string }).receipt_path && (
              <div className="mt-0.5 text-[11px] text-[#666] font-mono break-all">回执：{(dec as { receipt_path?: string }).receipt_path}</div>
            )}
            <div className="mt-1.5 flex flex-wrap items-center gap-2">
              {(dec.decision_options ?? []).map((o) => {
                if (o.action === 'retry') {
                  return (
                    <button key={o.id} title={o.hint} onClick={() => { setDecisionClosed(true); onRetryRewrite?.() }}
                      className="px-2.5 py-1 rounded bg-[#2a4a6a]/40 text-[#7fb4e6] border border-[#2a4a6a]/60 text-[14px] hover:bg-[#2a4a6a]/60">
                      ↻ {o.label}
                    </button>
                  )
                }
                if (o.action === 'accept') {
                  return (
                    <button key={o.id} title={o.hint} onClick={() => { setDecisionClosed(true); onAssetsChanged?.() }}
                      className="px-2.5 py-1 rounded bg-[#222] text-[#bbb] border border-[#3a3a3a] text-[14px] hover:border-[#666]">
                      {o.label}
                    </button>
                  )
                }
                return (
                  <span key={o.id} title={`${o.hint}（对话页支持）`} className="px-2.5 py-1 rounded bg-[#222] text-[#777] border border-[#2a2a2a] text-[13px]">
                    {o.label}（去对话页执行）
                  </span>
                )
              })}
            </div>
          </div>
        )}
        {!ok && res.book_id != null && res.chapter_id != null && (
          <RepairPlanCard bookId={res.book_id} chapter={res.chapter_id} onOpenFile={onOpenFile} />
        )}
        {a3Stage && (
          <div className="mt-2 rounded border border-[#f59e0b]/30 bg-[#f59e0b]/5 px-3 py-2">
            <div className="text-[14px] text-[#f59e0b]">
              命中 A-3 一致性闭环闸：作者确认后自动提交当前章（不重写正文，仅补闭环绕 + commit）。
            </div>
            <div className="mt-1.5 flex items-center gap-2">
              <button
                onClick={confirmAndCommit}
                disabled={commitState === 'confirming' || commitState === 'committing'}
                className="px-2.5 py-1 rounded bg-[#f59e0b]/20 text-[#f59e0b] border border-[#f59e0b]/40 text-[14px] hover:bg-[#f59e0b]/30 disabled:opacity-50"
              >
                {commitState === 'confirming' ? '确认闭环中…' : commitState === 'committing' ? '提交中…' : '✓ 确认一致性并提交'}
              </button>
              {commitState !== 'done' && commitState !== 'error' && (
                <span className="text-[13px] text-[#888]">确认后自动提交当前章，无需重写正文。</span>
              )}
            </div>
            {commitOut && (
              <div className={`mt-1.5 text-[14px] whitespace-pre-wrap break-all ${commitState === 'done' ? 'text-[#6ce3a1]' : 'text-red-300/80'}`}>
                {commitOut}
              </div>
            )}
            {commitState === 'done' && (
              <div className="mt-1 text-[14px] text-[#6ce3a1]">提交完成，本章已写入追踪（state 已推进）。</div>
            )}
          </div>
        )}
        {!ok && res.book_id != null && res.chapter_id != null && (
          (() => {
            const outlineBlock = stages.find(
              (s) => !s.ok && /卷级主细纲无该章|需先在规划层|自动规划异常|细纲结构无效|规划 LLM 调用失败/.test(s.detail || ''))
            if (!outlineBlock) return null
            return (
              <UnblockOutlineCard
                bookId={res.book_id}
                chapter={res.chapter_id}
                detail={outlineBlock.detail || ''}
                onPlanned={onAssetsChanged}
              />
            )
          })()
        )}
        {!ok && !a3Stage && res.book_id != null && res.chapter_id != null && (
          <div className="mt-2 rounded border border-[#2a4a6a] bg-[#152233]/60 px-3 py-2">
            <div className="text-[14px] text-[#7fb4e6]">
              悬空章解卡：该章正文可能已落盘但未提交（链条断在提交前环节，如 delta 终稿/审查）。
              「仅提交」不重写正文，直接走 闭环确认 → 追踪提交；前置校验不过（缺一致性审查 /
              开放 S1/S2 未闭环）时会给出具体原因，不绕闸。
            </div>
            <div className="mt-1.5 flex items-center gap-2">
              <button
                onClick={confirmAndCommit}
                disabled={commitState === 'confirming' || commitState === 'committing'}
                className="px-2.5 py-1 rounded bg-[#2a4a6a]/40 text-[#7fb4e6] border border-[#2a4a6a]/60 text-[14px] hover:bg-[#2a4a6a]/60 disabled:opacity-50"
              >
                {commitState === 'confirming' ? '确认闭环中…' : commitState === 'committing' ? '仅提交中…' : '🔓 解卡：仅提交已落盘章'}
              </button>
              <span className="text-[13px] text-[#666]">正文已在盘上则不动正文。</span>
            </div>
            {commitOut && (
              <div className={`mt-1.5 text-[14px] whitespace-pre-wrap break-all ${commitState === 'done' ? 'text-[#6ce3a1]' : 'text-red-300/80'}`}>
                {commitOut}
              </div>
            )}
            {commitState === 'done' && (
              <div className="mt-1 text-[14px] text-[#6ce3a1]">提交完成，本章已写入追踪（state 已推进）。</div>
            )}
          </div>
        )}
        {!ok && res.book_id != null && res.chapter_id != null && (
          <div className="mt-2 rounded border border-[#3a5a3a] bg-[#18251b]/60 px-3 py-2">
            <div className="text-[14px] text-[#8fd6a4]">
              人工修改审核提交：改完正文后点这里，系统自动补跑完整验收（机械层 → 审稿闭环真打分 → 一致性闭环），
              达到过闸标准后按原闸正式提交落账；不重写正文、不降标准。
            </div>
            <div className="mt-1.5 flex items-center gap-2">
              <button
                onClick={acceptanceSettle}
                disabled={settleState === 'running'}
                className="px-2.5 py-1 rounded bg-[#2a5a3a]/40 text-[#8fd6a4] border border-[#3a5a3a]/60 text-[14px] hover:bg-[#2a5a3a]/60 disabled:opacity-50"
              >
                {settleState === 'running' ? '验收补跑中…（审稿需数分钟）' : '✓ 人工修改后验收提交'}
              </button>
              <span className="text-[13px] text-[#666]">未达标会如实报出原因，不会放行。</span>
            </div>
            {settleOut && (
              <div className={`mt-1.5 text-[14px] whitespace-pre-wrap break-all ${settleState === 'done' ? 'text-[#6ce3a1]' : 'text-red-300/80'}`}>
                {settleOut}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

/** 细纲堵点解卡卡（2026-10-24）：「卷级主细纲无该章」类 BLOCK → 一键自动规划落细纲。
 *  规划产物走既有 prealign/packet 验收，成功后提示用户重新点「新写」。 */
function UnblockOutlineCard({
  bookId,
  chapter,
  detail,
  onPlanned,
}: {
  bookId: string
  chapter: number
  detail: string
  onPlanned?: (ok: boolean) => void
}) {
  const [state, setState] = useState<'idle' | 'planning' | 'done' | 'error'>('idle')
  const [out, setOut] = useState<string | null>(null)
  const planOutline = () => {
    if (state === 'planning' || state === 'done') return
    setState('planning'); setOut(null)
    api
      .agentsPlanOutline(bookId, chapter)
      .then((r) => {
        const ok = r.result?.ok ?? false
        setOut(`细纲规划${ok ? '成功' : '失败'}：${r.result?.detail || ''}`)
        setState(ok ? 'done' : 'error')
        if (ok) onPlanned?.(true)
      })
      .catch((e) => { setState('error'); setOut(String(e)) })
  }
  if (state === 'done') {
    return (
      <div className="mt-2 rounded border border-[#2a4a6a] bg-[#152233]/60 px-3 py-2">
        <div className="text-[14px] text-[#6ce3a1]">✓ 细纲已生成落盘，请重新点「新写」继续（会照常跑 prealign/packet 验收）。</div>
      </div>
    )
  }
  return (
    <div className="mt-2 rounded border border-[#2a4a6a] bg-[#152233]/60 px-3 py-2">
      <div className="text-[14px] text-[#7fb4e6] whitespace-pre-wrap break-all">
        堵点在【细纲】：{"\n"}<span className="text-[#999]">{detail.slice(0, 400)}</span>
      </div>
      <div className="mt-1.5 flex items-center gap-2">
        <button
          onClick={planOutline}
          disabled={state === 'planning'}
          className="px-2.5 py-1 rounded bg-[#2a4a6a]/40 text-[#7fb4e6] border border-[#2a4a6a]/60 text-[14px] hover:bg-[#2a4a6a]/60 disabled:opacity-50"
        >
          {state === 'planning' ? '规划生成中…' : `🧩 一键自动规划第${chapter}章细纲`}
        </button>
        <span className="text-[13px] text-[#666]">生成后走既有写前验收，不绕闸。</span>
      </div>
      {out && <div className="mt-1.5 text-[14px] whitespace-pre-wrap break-all text-red-300/80">{out}</div>}
    </div>
  )
}

/** Consistency Repair Loop Phase 1：一致性问题 → 【生成修复方案】（只读产生 repair_plan.json，不改正文）。
 *  展示 issue / ABC 分类 / 证据 / 建议；分类为 Planner 初判，最终执行权在作者。 */
function RepairPlanCard({
  bookId,
  chapter,
  onOpenFile,
}: {
  bookId: string
  chapter: number
  onOpenFile: (bookId: string, path: string) => void
}) {
  const [state, setState] = useState<'idle' | 'loading' | 'done' | 'error'>('idle')
  const [plan, setPlan] = useState<{
    plan_status: string
    confirmation?: {
      status?: string
      confirmed_by?: string
      confirmed_at?: string
      scope?: string
    }
    created_at: string
    issues: Array<{
      id: string
      type: string
      category: string
      evidence: string[]
      fix_targets?: string[]
      evidence_refs?: Array<{ file: string; location: string; excerpt: string }>
      summary: string
      suggested_fix: string
      risk_note: string
    }>
  } | null>(null)
  const [out, setOut] = useState<string | null>(null)
  const [confirming, setConfirming] = useState(false)
  const [executing, setExecuting] = useState(false)
  const [execResult, setExecResult] = useState<{
    summary: { a_applied: number; proposal_only: number }
    results: Array<{ issue_id: string; mode: string; reason?: string; detail?: string; new?: string }>
    snapshot: string | null
    backup: string | null
    audit_path: string
  } | null>(null)

  const generate = () => {
    if (state === 'loading') return
    setState('loading'); setOut(null); setExecResult(null)
    api
      .repairPlan(bookId, chapter)
      .then((r) => { setPlan(r.repair_plan); setState('done') })
      .catch((e) => { setOut(String(e)); setState('error') })
  }

  const confirm = () => {
    if (confirming || !plan || plan.confirmation?.status === 'confirmed') return
    setConfirming(true); setOut(null)
    api
      .confirmRepairPlan(bookId, chapter)
      .then(() => {
        setPlan((p) => (p ? { ...p, plan_status: 'confirmed', confirmation: { status: 'confirmed', scope: 'chapter_all_issues' } } : p))
        setConfirming(false)
      })
      .catch((e) => { setOut(String(e)); setConfirming(false) })
  }

  const execute = () => {
    if (executing || !plan) return
    setExecuting(true); setOut(null); setExecResult(null)
    api
      .executeRepairPlan(bookId, chapter)
      .then((r) => { setExecResult(r); setExecuting(false) })
      .catch((e) => { setOut(String(e)); setExecuting(false) })
  }

  const catColor = (c: string) =>
    c === 'A' ? 'text-[#6ce3a1]' : c === 'B' ? 'text-[#f59e0b]' : 'text-[#e879f9]'

  return (
    <div className="mt-2 rounded border border-[#3a3a3a] bg-[#1a1a1a] px-3 py-2">
      <div className="flex items-center gap-3">
        <span className="text-[14px] text-[#ddd]">一致性修复方案（Planner 初判，不改正文）</span>
        <button
          onClick={generate}
          disabled={state === 'loading'}
          className="px-2.5 py-1 rounded bg-[#e879f9]/15 text-[#e879f9] border border-[#e879f9]/40 text-[14px] hover:bg-[#e879f9]/25 disabled:opacity-50"
        >
          {state === 'loading' ? '生成中…' : plan ? '重新生成' : '生成修复方案'}
        </button>
        {state === 'done' && plan && (
          <span className="text-[12px] text-[#999] tabular-nums">
            {plan.issues.length} 条 · 状态 {plan.plan_status}
            {plan.confirmation?.status === 'confirmed' && (
              <span className="ml-1.5 text-[#6ce3a1]">· 已授权</span>
            )}
          </span>
        )}
        {state === 'done' && plan && plan.issues.length > 0 && plan.confirmation?.status !== 'confirmed' && (
          <button
            onClick={confirm}
            disabled={confirming}
            className="ml-auto px-2.5 py-1 rounded bg-[#6ce3a1]/15 text-[#6ce3a1] border border-[#6ce3a1]/40 text-[14px] hover:bg-[#6ce3a1]/25 disabled:opacity-50"
          >
            {confirming ? '授权中…' : '授权执行本章修复'}
          </button>
        )}
        {state === 'done' && plan && plan.issues.length > 0 && plan.confirmation?.status === 'confirmed' && (
          <button
            onClick={execute}
            disabled={executing}
            className="ml-auto px-2.5 py-1 rounded bg-[#6ce3a1]/15 text-[#6ce3a1] border border-[#6ce3a1]/40 text-[14px] hover:bg-[#6ce3a1]/25 disabled:opacity-50"
          >
            {executing ? '执行中…' : '执行本章修复'}
          </button>
        )}
      </div>

      {execResult && (
        <div className="mt-2 rounded border border-[#2f2f2f] bg-[#222] px-2.5 py-1.5">
          <div className="flex items-center gap-2 text-[14px]">
            <span className="text-[#6ce3a1]">已自动修复：{execResult.summary.a_applied}</span>
            <span className="text-[#f59e0b]">待作者决策：{execResult.summary.proposal_only}</span>
            <span className="ml-auto text-[12px] text-[#666]">（A 类已局部落地，B/C 类未改正文，整章未闭环）</span>
          </div>
          {execResult.results.map((r, i) => (
            <div key={i} className="mt-1 text-[13px] text-[#999]">
              <span className={r.mode === 'patch_applied' ? 'text-[#6ce3a1]' : 'text-[#f59e0b]'}>
                [{r.mode === 'patch_applied' ? '已修复' : '待决策'}]
              </span>{' '}
              {r.issue_id}
              {r.reason && <span className="text-[#777]"> · {r.reason}</span>}
              {r.new && <div className="mt-0.5 text-[12px] text-[#888]">→ {r.new}</div>}
            </div>
          ))}
          {execResult.snapshot && (
            <div className="mt-1 text-[12px] text-[#555]">快照：{execResult.snapshot}</div>
          )}
          {execResult.backup && (
            <div className="text-[12px] text-[#555]">备份：{execResult.backup}</div>
          )}
        </div>
      )}

      {state === 'error' && <div className="mt-1.5 text-[14px] text-red-300/80 whitespace-pre-wrap break-all">{out}</div>}

      {state === 'done' && plan && plan.issues.length === 0 && (
        <div className="mt-1.5 text-[14px] text-[#999]">LLM 未产出分类条目（解析空），可重试或人工研判。</div>
      )}

      {state === 'done' && plan && plan.issues.length > 0 && (
        <div className="mt-2 flex flex-col gap-1.5">
          {plan.issues.map((it, i) => (
            <div key={i} className="rounded border border-[#2f2f2f] bg-[#222] px-2.5 py-1.5">
              <div className="flex items-center gap-2">
                <span className={`text-[14px] font-semibold ${catColor(it.category)}`}>[{it.category}]</span>
                <span className="text-[14px] text-[#ddd]">{it.id}</span>
                <span className="text-[12px] px-1.5 rounded bg-[#2a2a2a] text-[#999]">{it.type}</span>
                {it.category === 'A' && <span className="text-[11px] text-[#6ce3a1]">唯一答案 · 需人工确认后才执行</span>}
                {it.category === 'B' && <span className="text-[11px] text-[#f59e0b]">涉设定/剧情/伏笔 · 作者决策</span>}
                {it.category === 'C' && <span className="text-[11px] text-[#e879f9]">可修但有破坏风险 · 作者选择</span>}
              </div>
              <div className="mt-1 text-[14px] text-[#ccc]">{it.summary}</div>
              {it.suggested_fix && <div className="mt-1 text-[14px] text-[#999]">建议：{it.suggested_fix}</div>}
              {it.risk_note && <div className="mt-1 text-[13px] text-[#888]">风险：{it.risk_note}</div>}
              {it.evidence.length > 0 && (
                <div className="mt-1 flex flex-wrap gap-1.5">
                  {it.evidence.map((ev, j) =>
                    /\.md$/i.test(ev) ? (
                      <button
                        key={j}
                        onClick={() => onOpenFile(bookId, ev)}
                        className="text-[12px] text-[#6c9ef8] hover:underline break-all"
                      >
                        {ev}
                      </button>
                    ) : (
                      <span key={j} className="text-[12px] text-[#777] break-all">{ev}</span>
                    ),
                  )}
                </div>
              )}
              {it.evidence_refs && it.evidence_refs.length > 0 && (
                <div className="mt-1 flex flex-col gap-0.5">
                  {it.evidence_refs.map((ref, j) => (
                    <div key={j} className="flex items-start gap-2">
                      <button
                        onClick={() => onOpenFile(bookId, ref.file)}
                        className="text-[12px] text-[#6c9ef8] hover:underline shrink-0"
                      >
                        {ref.location}
                      </button>
                      <span className="text-[12px] text-[#666] break-all">…{ref.excerpt}…</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}