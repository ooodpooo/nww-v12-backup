import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { api, type TrackingPayload } from '../api'
import ScrollArea from './ScrollArea'

interface Props {
  bookId: string
}

function Card({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="rounded-xl border border-zinc-800 bg-zinc-900/50 overflow-hidden">
      <div className="px-4 py-2.5 text-[14px] font-semibold text-zinc-400 border-b border-zinc-800 bg-zinc-900/80">{title}</div>
      <div className="p-4 text-sm text-zinc-300">{children}</div>
    </div>
  )
}

export default function TrackingView({ bookId }: Props) {
  const [data, setData] = useState<TrackingPayload | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api
      .tracking(bookId)
      .then(setData)
      .catch((e) => setError(String(e)))
  }, [bookId])

  const t = data?.tracking as Record<string, any> | undefined

  const stats = useMemo(() => {
    if (!t) return []
    const wc = (t.wordcount_records ?? {}) as Record<string, any>
    const chapters = Object.keys(wc)
    const total = chapters.reduce((s, k) => s + (wc[k]?.actual ?? 0), 0)
    const pass = chapters.filter((k) => wc[k]?.status === 'internal_pass').length
    const cost = (t.cost ?? {}) as Record<string, any>
    return [
      { label: '已提交章节', value: String(t.last_committed_chapter ?? 0) },
      { label: '总字数', value: total > 10000 ? `${(total / 10000).toFixed(2)} 万` : `${total}` },
      { label: '字数达标', value: `${pass}/${chapters.length}` },
      { label: '状态修订', value: String(t.state_revision ?? 0) },
      ...Object.entries(cost).map(([k, v]) => ({ label: k, value: String(v) })),
    ]
  }, [t])

  if (error) return <div className="h-full flex items-center justify-center text-sm text-red-400">{error}</div>
  if (!data || !t) return <div className="h-full flex items-center justify-center text-sm text-zinc-500">加载中…</div>

  const context = (t.context ?? {}) as Record<string, any>
  const position = (context.position ?? {}) as Record<string, any>
  const characters = (t.characters ?? {}) as Record<string, any>
  const foreshadow = (t.foreshadow ?? {}) as Record<string, any>
  const timeline = (t.timeline ?? {}) as Record<string, any>
  const recent: { chapter: number; summary: string }[] = context.recent_chapters ?? []

  return (
    <ScrollArea className="card-scroll">
      <div className="space-y-4">
        {/* 数据行 */}
        <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
          {stats.map((s) => (
            <div
              key={s.label}
              className="rounded-xl border border-zinc-800 bg-zinc-900/50 px-4 py-3 hover:border-zinc-700 transition-colors"
            >
              <div className="text-[14px] text-[#777] uppercase tracking-wide">{s.label}</div>
              <div className="text-lg font-semibold text-zinc-100 tabular-nums mt-0.5">{s.value}</div>
            </div>
          ))}
        </div>

        {/* 当前位置 */}
        <Card title="当前位置">
          <div className="space-y-1">
            <div><span className="text-zinc-500">卷：</span>{position.volume ?? '—'}</div>
            <div><span className="text-zinc-500">场景：</span>{position.scene ?? '—'}</div>
            <div><span className="text-zinc-500">故事时间：</span>{position.story_time ?? '—'}</div>
          </div>
        </Card>

        {/* 最近章节 */}
        <Card title="最近章节摘要">
          <div className="space-y-2">
            {recent.length === 0 && <div className="text-zinc-500">无</div>}
            {recent.map((r) => (
              <div key={r.chapter} className="text-[14px] leading-5 text-zinc-400">
                <span className="text-amber-400 font-medium">第{r.chapter}章</span> {r.summary}
              </div>
            ))}
          </div>
        </Card>

        {/* 角色状态 */}
        <Card title="角色状态">
          <div className="space-y-3">
            {Object.entries(characters).map(([name, c]: [string, any]) => (
              <div key={name}>
                <div className="font-medium text-zinc-100 mb-0.5">
                  {name}
                  <span className="text-zinc-500 font-normal text-[14px] ml-2">{c.identity}</span>
                </div>
                <div className="text-[14px] leading-5 text-zinc-400">{c.state}</div>
              </div>
            ))}
          </div>
        </Card>

        {/* 伏笔 */}
        <Card title={`伏笔（${Object.keys(foreshadow).length}）`}>
          <div className="space-y-2">
            {Object.entries(foreshadow).map(([id, f]: [string, any]) => (
              <div key={id} className="text-[14px] leading-5 text-zinc-400">
                <span className="text-amber-400 font-medium">
                  {id} · {f.importance} · 第{f.planted_chapter}章埋
                </span>{' '}
                {f.summary}
              </div>
            ))}
          </div>
        </Card>

        {/* 时间线 */}
        <Card title={`时间线（${Object.keys(timeline).length}）`}>
          <div className="space-y-2">
            {Object.entries(timeline).map(([id, e]: [string, any]) => (
              <div key={id} className="text-[14px] leading-5 text-zinc-400">
                <span className="text-amber-400 font-medium">
                  {id} · 第{e.reveal_chapter}章揭示 · {e.reveal_status}
                </span>
                <div>{e.objective_fact}</div>
              </div>
            ))}
          </div>
        </Card>

        {/* 连续性风险 + 下章承诺 */}
        <div className="grid md:grid-cols-2 gap-4">
          <Card title="连续性风险">
            <ul className="space-y-1.5 list-disc pl-4 text-[14px] leading-5 text-zinc-400">
              {(context.continuity_risks ?? []).map((r: string, i: number) => (
                <li key={i}>{r}</li>
              ))}
            </ul>
          </Card>
          <Card title="下章承诺">
            <ul className="space-y-1.5 list-disc pl-4 text-[14px] leading-5 text-zinc-400">
              {(context.next_chapter_commitments ?? []).map((r: string, i: number) => (
                <li key={i}>{r}</li>
              ))}
            </ul>
          </Card>
        </div>

        {/* 派生视图文件（只读） */}
        <Card title={`派生视图文件（${data.derived.length} · 只读）`}>
          <div className="flex flex-wrap gap-2">
            {data.derived.length === 0 && <div className="text-zinc-500 text-[14px]">无</div>}
            {data.derived.map((d) => (
              <span
                key={d.path}
                className="text-[14px] px-2 py-1 rounded-md bg-zinc-800 text-zinc-400 hover:bg-zinc-700 transition-colors"
                title={d.path}
              >
                {d.name} · {d.chars} 字
              </span>
            ))}
          </div>
        </Card>
      </div>
    </ScrollArea>
  )
}
