import { useState, type FormEvent } from 'react'
import { api, type SearchHit } from '../api'

export default function SearchView() {
  const [q, setQ] = useState('')
  const [hits, setHits] = useState<SearchHit[] | null>(null)
  const [searching, setSearching] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const run = (e: FormEvent) => {
    e.preventDefault()
    if (!q.trim()) return
    setSearching(true)
    setError(null)
    api
      .search(q.trim())
      .then(setHits)
      .catch((e) => setError(String(e)))
      .finally(() => setSearching(false))
  }

  return (
    <div className="h-full overflow-auto p-6">
      <div className="max-w-3xl mx-auto">
        <h1 className="text-[14px] font-semibold text-zinc-100 mb-1">全文搜索</h1>
        <p className="text-[14px] text-zinc-500 mb-5">跨全部书检索标题与正文内容</p>
        <form onSubmit={run} className="flex gap-2 mb-5">
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="搜索关键词（跨全部书）…"
            className="flex-1 bg-zinc-900 border border-zinc-700 rounded-lg px-3.5 py-2 text-sm text-zinc-200 placeholder:text-zinc-600 outline-none hover:border-zinc-600 focus:border-amber-600 focus:ring-2 focus:ring-amber-600/20 transition-all"
          />
          <button
            type="submit"
            disabled={searching || !q.trim()}
            className="px-4 py-2 rounded-lg text-sm bg-amber-600 text-zinc-950 font-medium disabled:opacity-40 hover:bg-amber-500 transition-colors"
          >
            {searching ? '搜索中…' : '搜索'}
          </button>
        </form>

        {error && <div className="text-sm text-red-400 mb-4">{error}</div>}

        {hits && (
          <div className="space-y-1">
            {hits.length === 0 && <div className="text-sm text-zinc-500 py-8 text-center">无结果</div>}
            {hits.map((h) => (
              <div
                key={`${h.book}-${h.path}`}
                className="px-3.5 py-2.5 rounded-lg bg-zinc-900/60 border border-zinc-800 text-sm hover:border-zinc-700 hover:bg-zinc-900 transition-colors"
              >
                <span className="text-amber-400 font-medium mr-2">{h.book}</span>
                <span className="text-zinc-300">{h.name}</span>
                <span className="text-zinc-600 text-[14px] ml-2 font-mono">{h.path}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
