import { useState } from 'react'
import { api, type Book } from '../api'

interface Props {
  books: Book[]
  onOpen: (id: string) => void
  onReload: () => void
}

export default function ShelfView({ books, onOpen, onReload }: Props) {
  const [deleting, setDeleting] = useState<Book | null>(null)
  const [creating, setCreating] = useState(false)
  const [newTitle, setNewTitle] = useState('')
  const [newGenre, setNewGenre] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const confirmDelete = async () => {
    if (!deleting) return
    setBusy(true)
    setError(null)
    try {
      await api.deleteBook(deleting.id)
      setDeleting(null)
      onReload()
    } catch (e) {
      setError(`删除失败：${String(e)}`)
      setDeleting(null)
    } finally {
      setBusy(false)
    }
  }

  const confirmCreate = async () => {
    const title = newTitle.trim()
    if (!title) {
      setError('书名不能为空')
      return
    }
    setBusy(true)
    setError(null)
    try {
      await api.createBook(title, newGenre.trim() || undefined)
      setCreating(false)
      setNewTitle('')
      setNewGenre('')
      onReload()
    } catch (e) {
      setError(`创建失败：${String(e)}`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="h-full overflow-auto p-6">
      <div className="max-w-5xl mx-auto">
        <div className="flex items-center justify-between mb-6">
          <div>
            <h1 className="text-[14px] font-semibold text-zinc-100">书架</h1>
            <p className="text-[14px] text-zinc-500 mt-1">{books.length} 本 V12 项目</p>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => { setCreating(true); setError(null) }}
              className="px-3.5 py-1.5 rounded-lg text-sm bg-amber-600/90 hover:bg-amber-500 text-white font-medium transition-colors"
            >
              新建书籍
            </button>
            <button
              onClick={onReload}
              className="px-3.5 py-1.5 rounded-lg text-sm bg-zinc-800/80 hover:bg-zinc-700 text-zinc-300 border border-zinc-700 transition-colors"
            >
              刷新
            </button>
          </div>
        </div>

        {error && (
          <div className="mb-4 px-3 py-2 rounded-lg bg-red-950/40 border border-red-900 text-sm text-red-400">{error}</div>
        )}

        {books.length === 0 && (
          <div className="text-zinc-500 text-sm py-16 text-center border border-dashed border-zinc-800 rounded-xl">
            暂无书（需存在 {`追踪/_tracking-state.json`} 的 V12 项目）
          </div>
        )}

        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {books.map((b) => (
            <div
              key={b.id}
              onClick={() => onOpen(b.id)}
              className="relative text-left rounded-xl border border-zinc-800 bg-zinc-900/60 hover:bg-zinc-900 hover:border-zinc-600 hover:-translate-y-0.5 hover:shadow-xl hover:shadow-black/40 transition-all duration-200 p-4 flex flex-col gap-3 cursor-pointer group"
            >
              <button
                title="删除此书"
                aria-label={`删除 ${b.title}`}
                onClick={(e) => {
                  e.stopPropagation()
                  e.preventDefault()
                  setDeleting(b)
                }}
                className="absolute top-2.5 right-2.5 w-6 h-6 rounded-md flex items-center justify-center text-zinc-600 opacity-0 group-hover:opacity-100 hover:text-red-400 hover:bg-red-950/40 transition-all"
              >
                🗑
              </button>

              <div>
                <div className="text-[14px] font-medium text-zinc-100 mb-1 pr-6 truncate">{b.title}</div>
                {b.genre && (
                  <span className="inline-block text-[14px] px-1.5 py-0.5 rounded bg-amber-950/40 text-amber-400/90 mb-1.5">
                    {b.genre}
                  </span>
                )}
                <div className="text-[14px] text-zinc-500 truncate font-mono">{b.id}</div>
              </div>
              <div className="flex items-center justify-between text-[14px] text-zinc-400 pt-1 border-t border-zinc-800/80">
                <div className="flex items-center gap-3">
                  <span className="tabular-nums">{b.chapter_count} 章</span>
                  <span className="tabular-nums">{(b.total_chars / 10000).toFixed(2)} 万字</span>
                </div>
                {b.last_committed_chapter != null && (
                  <span className="px-1.5 py-0.5 rounded bg-zinc-800 text-zinc-400 tabular-nums">
                    已提交至 {b.last_committed_chapter} 章
                  </span>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* 删除确认弹窗（按钮确认） */}
      {deleting && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
          onClick={() => setDeleting(null)}
        >
          <div
            className="w-[360px] rounded-xl border border-zinc-700 bg-zinc-900 shadow-2xl shadow-black/60"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="px-5 py-3.5 border-b border-zinc-800 font-medium text-zinc-100 text-sm">删除书</div>
            <div className="px-5 py-5 text-sm text-zinc-300 leading-6">
              确定删除
              <span className="text-amber-300 font-medium">「{deleting.title}」</span>？
              <div className="mt-2 text-[14px] text-zinc-500">删除后将移入回收站 projects/.trash/（可从书架移除，可恢复）。</div>
            </div>
            {error && <div className="px-5 pb-2 text-[14px] text-red-400">{error}</div>}
            <div className="px-5 py-3.5 flex justify-end gap-2 border-t border-zinc-800">
              <button
                onClick={() => setDeleting(null)}
                disabled={busy}
                className="px-3.5 py-1.5 rounded-lg text-sm bg-zinc-800 hover:bg-zinc-700 text-zinc-300 border border-zinc-700 disabled:opacity-40 transition-colors"
              >
                取消
              </button>
              <button
                onClick={confirmDelete}
                disabled={busy}
                className="px-3.5 py-1.5 rounded-lg text-sm bg-red-600 hover:bg-red-500 text-white font-medium disabled:opacity-40 transition-colors"
              >
                {busy ? '删除中…' : '确认删除'}
              </button>
            </div>
          </div>
        </div>
      )}
      {/* 新建书籍弹窗 */}
      {creating && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
          onClick={() => setCreating(false)}
        >
          <div
            className="w-[400px] rounded-xl border border-zinc-700 bg-zinc-900 shadow-2xl shadow-black/60"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="px-5 py-3.5 border-b border-zinc-800 font-medium text-zinc-100 text-sm">新建书籍</div>
            <div className="px-5 py-4 space-y-3">
              <div>
                <label className="block text-[14px] text-zinc-400 mb-1">书名 *</label>
                <input
                  autoFocus
                  value={newTitle}
                  onChange={(e) => setNewTitle(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && confirmCreate()}
                  placeholder="例如：盗天藏书客"
                  className="w-full px-3 py-2 rounded-lg bg-zinc-800 border border-zinc-700 text-sm text-zinc-100 placeholder-zinc-600 focus:outline-none focus:border-amber-500"
                />
              </div>
              <div>
                <label className="block text-[14px] text-zinc-400 mb-1">题材（可选）</label>
                <input
                  value={newGenre}
                  onChange={(e) => setNewGenre(e.target.value)}
                  placeholder="例如：修仙/文道/苟道"
                  className="w-full px-3 py-2 rounded-lg bg-zinc-800 border border-zinc-700 text-sm text-zinc-100 placeholder-zinc-600 focus:outline-none focus:border-amber-500"
                />
              </div>
              {error && <div className="text-[14px] text-red-400">{error}</div>}
            </div>
            <div className="px-5 py-3.5 flex justify-end gap-2 border-t border-zinc-800">
              <button
                onClick={() => setCreating(false)}
                disabled={busy}
                className="px-3.5 py-1.5 rounded-lg text-sm bg-zinc-800 hover:bg-zinc-700 text-zinc-300 border border-zinc-700 disabled:opacity-40 transition-colors"
              >
                取消
              </button>
              <button
                onClick={confirmCreate}
                disabled={busy}
                className="px-3.5 py-1.5 rounded-lg text-sm bg-amber-600 hover:bg-amber-500 text-white font-medium disabled:opacity-40 transition-colors"
              >
                {busy ? '创建中…' : '创建'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
