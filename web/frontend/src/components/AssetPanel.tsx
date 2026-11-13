import { useMemo, useState } from 'react'
import { ChevronDown, RefreshCw, Trash2 } from 'lucide-react'
import { api, type Book } from '../api'
import AssetTree from './AssetTree'

/* ─────────────────────  资产面板：V14 顶部标题栏 + 分组目录  ─────────────────────
   顶部对齐 V14 `NovelWorkspace.tsx` 的书名标题行（标题 + 右侧胶囊按钮），
   选书下拉保留但改为无边框标题样式；简介 + 目录树分区排版走 V14 分组口径。
   色值全部沿用 V12（#141414 / #2a2a2a / #f59e0b），不引入 V14 深蓝底。
────────────────────────────────────────────────────────────────────── */

interface Props {
  books: Book[]
  activeBook: string | null
  activePath: string | null
  treeVersion?: number
  onSelectBook: (id: string) => void
  onNavigate: (p: string) => void
  onReloadBooks: () => void
}

/** 故事简介：优先用 bible.world_summary，无则用题材极简兜底 */
function buildSynopsis(book: Book): string {
  if (book.synopsis && book.synopsis.trim()) return book.synopsis.trim()
  if (book.genre) return `一部${book.genre}题材的作品。`
  return '正在创作中的作品。'
}

export default function AssetPanel({ books, activeBook, activePath, treeVersion, onSelectBook, onNavigate, onReloadBooks }: Props) {
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [deleteError, setDeleteError] = useState<string | null>(null)

  const activeBookData = useMemo(() => books.find((b) => b.id === activeBook) ?? null, [books, activeBook])
  const synopsis = useMemo(() => (activeBookData ? buildSynopsis(activeBookData) : ''), [activeBookData])

  const confirmDelete = async () => {
    if (!activeBook) return
    setDeleting(true)
    setDeleteError(null)
    try {
      await api.deleteBook(activeBook)
      setShowDeleteConfirm(false)
      onReloadBooks()
      // 自动切换到剩余的第一本书
      const remaining = books.filter((b) => b.id !== activeBook)
      if (remaining.length > 0) onSelectBook(remaining[0].id)
    } catch (e) {
      setDeleteError(String(e))
    } finally {
      setDeleting(false)
    }
  }

  return (
    <div className="h-full flex flex-col relative">
      {/* 书名标题栏：无边框下拉 + 胶囊操作按钮（V14 顶部标题行排列） */}
      <div className="card-pad-x h-12 shrink-0 flex items-center gap-2 border-b border-[#1e1e1e]">
        <div className="relative min-w-0 flex-1">
          <select
            value={activeBook ?? ''}
            onChange={(e) => e.target.value && onSelectBook(e.target.value)}
            title="切换书籍"
            className="w-full appearance-none bg-transparent border-0 outline-none pr-5 text-[15px] font-semibold text-white truncate cursor-pointer focus-visible:outline-none"
          >
            <option value="" disabled>选择书籍…</option>
            {books.map((b) => (
              <option key={b.id} value={b.id} className="bg-[#1a1a1a] text-[#ccc]">{b.title}</option>
            ))}
          </select>
          <ChevronDown className="absolute right-0 top-1/2 -translate-y-1/2 pointer-events-none h-3.5 w-3.5 text-[#777]" strokeWidth={1.8} aria-hidden />
        </div>
        <button
          onClick={onReloadBooks}
          title="刷新书架"
          className="v14-badge gap-1 border-[#3a3a3a] text-[#9a9a9a] hover:border-[#f59e0b]/50 hover:text-[#f59e0b] transition-colors"
        >
          <RefreshCw className="h-3 w-3" strokeWidth={1.8} aria-hidden />
          刷新
        </button>
        <button
          onClick={() => { setDeleteError(null); setShowDeleteConfirm(true) }}
          disabled={!activeBook}
          title="删除当前书籍"
          className="h-7 w-7 shrink-0 flex items-center justify-center rounded-md text-[#7a7a7a] hover:bg-red-950/30 hover:text-red-400 transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
        >
          <Trash2 className="h-4 w-4" strokeWidth={1.8} aria-hidden />
        </button>
      </div>

      {/* 书籍故事简介 */}
      {activeBookData && (
        <div className="card-pad-x pt-2 pb-3">
          <p
            className="text-[12px] leading-[1.7] text-[#777] overflow-hidden"
            style={{ display: '-webkit-box', WebkitLineClamp: 4, WebkitBoxOrient: 'vertical' }}
            title={synopsis}
          >
            {synopsis}
          </p>
        </div>
      )}

      {/* 资产树（分组标题由 AssetTree 内部按 V14 口径渲染） */}
      <div className="flex-1 min-h-0 overflow-hidden">
        {activeBook ? (
          <AssetTree bookId={activeBook} activePath={activePath} treeVersion={treeVersion} onNavigate={onNavigate} />
        ) : (
          <div className="h-full flex items-center justify-center text-[14px] text-[#666]">请先选择书籍</div>
        )}
      </div>

      {/* 删除确认弹窗 */}
      {showDeleteConfirm && (
        <div
          className="absolute inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
          onClick={() => !deleting && setShowDeleteConfirm(false)}
        >
          <div
            className="w-[280px] rounded-lg border border-[#3a3a3a] bg-[#1e1e1e] shadow-2xl shadow-black/60"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="px-4 py-3 border-b border-[#2a2a2a]">
              <div className="text-[14px] font-medium text-white">删除书籍</div>
            </div>
            <div className="px-4 py-4 text-[14px] text-[#aaa] leading-5">
              确定删除
              <span className="text-amber-300 font-medium">「{activeBookData?.title ?? ''}」</span>
              ？
              <div className="mt-2 text-[14px] text-[#666]">删除后将移入回收站 projects/.trash/（可恢复）。</div>
              {deleteError && <div className="mt-2 text-[14px] text-red-400">{deleteError}</div>}
            </div>
            <div className="px-4 py-3 flex justify-end gap-2 border-t border-[#2a2a2a]">
              <button
                onClick={() => setShowDeleteConfirm(false)}
                disabled={deleting}
                className="px-3 py-1 rounded-md text-[14px] bg-[#2a2a2a] hover:bg-[#333] text-[#ccc] border border-[#3a3a3a] disabled:opacity-40 transition-colors"
              >
                取消
              </button>
              <button
                onClick={confirmDelete}
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
